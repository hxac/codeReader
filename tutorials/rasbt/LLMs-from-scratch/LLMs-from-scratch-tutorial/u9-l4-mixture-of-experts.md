# 混合专家（MoE）

## 1. 本讲目标

本讲把第 4 章里那个「永远全员上岗」的稠密前馈层（FeedForward），替换成「按需调度」的**混合专家层（Mixture of Experts, MoE）**。学完本讲，你应当能够：

- 理解 MoE 为什么能在「总参数量」与「每次前向的计算量」之间解耦——用大模型容量换低推理成本。
- 读懂 `MoEFeedForward` 的完整实现：门控路由（gate）如何打分、top-k 如何选专家、softmax 概率如何加权。
- 掌握「按专家分组、稀疏计算、再聚拢」的实现技巧，并用 `index_add_` 把结果写回。
- 会动手实例化 `MoEFeedForward`、观察路由结果、并定量比较它与稠密 FFN 的「激活参数量」差异。

本讲是 **advanced** 阶段的内容，承接 u4-l2 的 `TransformerBlock` 与残差连接——MoE 只是把 Transformer 块里的那一个前馈子层换掉，其它结构原封不动。

## 2. 前置知识

阅读本讲前，建议你先掌握：

- **前馈网络（FFN）**（u4-l1）：Transformer 块里 `emb_dim → 4×emb_dim → emb_dim` 的两层瓶颈结构，是模型参数的重要来源。
- **TransformerBlock 与残差连接**（u4-l2）：注意力子层 + 前馈子层 + 两条残差捷径，输入输出形状恒为 `(batch, num_tokens, emb_dim)`，因此前馈层可以被「等形状替换」。
- **softmax 与 top-k**：本讲大量用到 `torch.topk`（取前 k 大）和 `torch.softmax`（归一化为概率）。
- **（可选）KV cache**（u9-l1）：本讲引用的 `gpt_with_kv_moe.py` 同时内置了 KV cache，但 MoE 本身与缓存无关，二者相互独立。

两个直觉先放在前面：

1. **稀疏（sparse）vs 稠密（dense）**：稠密层每次前向都激活全部参数；稀疏层只激活一小撮。MoE 是典型的稀疏结构。
2. **路由（routing）**：每个 token 不走相同的路，而是由一个轻量「门控」决定它该去找哪几位「专家」看病。

## 3. 本讲源码地图

本讲涉及的关键文件都集中在 `ch04/07_moe/` 目录：

| 文件 | 作用 |
| --- | --- |
| [ch04/07_moe/gpt_with_kv_moe.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/07_moe/gpt_with_kv_moe.py) | 本讲核心。把 GPT 的前馈层换成 MoE 的完整可运行脚本，含 `MoEFeedForward`、`TransformerBlock`、`GPTModel` 与带缓存的生成函数。 |
| [ch04/07_moe/README.md](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/07_moe/README.md) | 图文讲解 MoE 的稀疏激活动机、共享专家（shared expert）思想，以及稠密 FFN 与 MoE 的内存对比。 |
| [ch04/07_moe/memory_estimator_moe.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/07_moe/memory_estimator_moe.py) | 参数/内存估算工具，本讲综合实践会借用它的「按 top_k/num_experts 比例折算激活量」思路。 |

> 提示：本讲引用的代码行号均对应当前 HEAD `ff0b3d9`，永久链接已固定。`gpt_with_kv_moe.py` 是一个自包含脚本（参见 u1-l3 的「自包含 vs 依赖模块」分类），可单独运行。

## 4. 核心概念与源码讲解

### 4.1 MoEFeedForward 与稀疏激活思想

#### 4.1.1 概念说明

回顾 u4-l1/u4-l2：每个 Transformer 块里都有一层前馈网络 `FFN`，它把每个 token 的 `emb_dim` 维向量升维到 `hidden_dim`（通常 4 倍）再降回来。这层虽小，却往往是**整个 Transformer 块里参数最多的子层**，并且它在每个 Transformer 块里重复出现（GPT-2 small 重复 12 次，DeepSeek-V3 重复 61 次）。

MoE 的核心想法很直白：**既然 FFN 占参数多，那就把它复制成多份（叫「专家」），但每次只让其中少数几份真正干活。**

- 把「一个 FFN」换成「若干个并列的 FFN（专家）」。
- 增加专家数量会让**总参数量**（即模型容量、可记忆的知识量）大幅上涨。
- 但每个 token 只激活其中 `top_k` 个专家，所以**每次前向的计算量与激活内存**几乎不变。

这就是 MoE 的「稀疏」：总参数很大，但单步激活很小。README 用 DeepSeek-V3 给了一个令人印象深刻的数字：

> DeepSeek-V3 每个 MoE 模块有 256 个专家，总参数 6710 亿；但推理时每个 token 只激活 9 个专家（1 个共享专家 + 路由器选出的 8 个），即每个 token 实际只用 370 亿参数。

其激活比例约为

\[
\frac{\text{激活参数}}{\text{总参数}} \approx \frac{\text{top\_k}}{\text{num\_experts}}
\]

这正是 MoE 的核心收益：**用大模型容量，付小推理代价。**

> 名词补注：README 还提到**共享专家（shared expert）**——一个对每个 token 永远在线的专家，负责学习「通用/重复模式」，把通用知识从各路由专家中剥离出去，让它们专注专业化。本项目未实现共享专家，但 `MoEFeedForward` 的结构与加上共享专家后的版本完全兼容。

#### 4.1.2 核心流程

把 MoE 放回 Transformer 块的视角，数据流几乎不变，只有前馈子层换了内部实现：

```
输入 x: (batch, seq_len, emb_dim)
        │
   ┌────┴──── 注意力子层（MultiHeadAttention，不变）
   │         + 残差捷径
   ▼
  中间态 x: (batch, seq_len, emb_dim)
        │
   ┌────┴──── 前馈子层（这里是关键差异！）
   │         稠密版：FFN(x)，全员上岗
   │         MoE 版 ：每个 token 经门控选出 top_k 个专家，
   │                 只算这 k 个专家，按概率加权求和
   ▼         + 残差捷径
  输出 x: (batch, seq_len, emb_dim)
```

MoE 前馈子层内部的更细流程：

1. **打分**：一个线性层 `gate` 把 `emb_dim` 维 token 映射成 `num_experts` 个分数。
2. **选 k**：对每个 token 取分数最高的 `top_k` 个专家。
3. **算概率**：对这 `top_k` 个分数做 softmax，得到归一化权重。
4. **稀疏计算**：把所有被路由到同一专家的 token 收拢成一批，一次性算该专家的 FFN。
5. **加权聚拢**：每个 token 的最终输出 = 它选中的 k 个专家输出按概率加权求和。

#### 4.1.3 源码精读：在 TransformerBlock 里「无缝替换」

MoE 之所以能即插即用，是因为它的输入输出形状和稠密 FFN 完全一致（都是 `(batch, seq_len, emb_dim)`）。`TransformerBlock` 用一行三元判断来切换：

[ch04/07_moe/gpt_with_kv_moe.py:240](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/07_moe/gpt_with_kv_moe.py#L240) —— 当配置里 `num_experts > 0` 时用 `MoEFeedForward`，否则回退到普通 `FeedForward`：

```python
self.ff = MoEFeedForward(cfg) if cfg["num_experts"] > 0 else FeedForward(cfg)
```

注意同目录的稠密 `FeedForward` 仍用 GELU（见 [gpt_with_kv_moe.py:146-156](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/07_moe/gpt_with_kv_moe.py#L146-L156)），而 `MoEFeedForward` 用 SwiGLU（下文 4.3 详解）。README 也明确说明本目录两个脚本都采用 [SwiGLU](https://arxiv.org/abs/2002.05202) 前馈模块（GPT-2 传统上用 GELU）。

`MoEFeedForward` 的骨架见 [gpt_with_kv_moe.py:159-184](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/07_moe/gpt_with_kv_moe.py#L159-L184)：一个 `gate` 线性层 + 三组 `nn.ModuleList`（`fc1/fc2/fc3`），每组 `num_experts` 个并列线性层，分别扮演 SwiGLU 的三个权重矩阵。

模型配置通过命令行参数注入，见 [gpt_with_kv_moe.py:435-446](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/07_moe/gpt_with_kv_moe.py#L435-L446) 里的 `GPT_CONFIG_124M`，新增了 `num_experts` 与 `num_experts_per_tok`（即 top_k）两个字段。

#### 4.1.4 代码实践：最小实例化

**实践目标**：确认 `MoEFeedForward` 的形状与稠密 FFN 完全一致，可以无痛替换。

**操作步骤**（在 `ch04/07_moe/` 目录下运行，以便 `import`）：

```python
# 示例代码：保存为 ch04/07_moe/try_moe.py 并运行
import torch
from gpt_with_kv_moe import MoEFeedForward, FeedForward

cfg = {
    "emb_dim": 768,
    "hidden_dim": 768 * 4,
    "num_experts": 8,
    "num_experts_per_tok": 2,
}

torch.manual_seed(123)
moe = MoEFeedForward(cfg)
x = torch.randn(2, 6, 768)   # (batch, seq_len, emb_dim)
print("MoE 输入形状 :", tuple(x.shape))
print("MoE 输出形状 :", tuple(moe(x).shape))
```

**需要观察的现象**：输入 `(2, 6, 768)`，输出仍是 `(2, 6, 768)`，与稠密 `FeedForward` 同形——这正是它能「无缝替换」的前提。

**预期结果**：输出形状为 `(2, 6, 768)`。注意模型未训练，输出数值本身无意义。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `num_experts` 从 8 调到 16，而 `num_experts_per_tok` 保持 2，模型的「总参数量」和「单 token 激活参数量」分别怎么变？

**答案**：总参数量近似翻倍（专家多了一倍），但单 token 激活参数量几乎不变（仍只激活 2 个专家），于是激活比例从 2/8=25% 降到 2/16=12.5%。这正是「加专家不增算力」的体现。

**练习 2**：为什么说 MoE 「用大容量换小计算」？请用一句话概括。

**答案**：总参数随专家数线性增长（容量/知识变大），但每个 token 只走 top_k 个专家，单步激活量基本恒定（计算/显存变小）。

### 4.2 gate 路由与 top-k 选择

#### 4.2.1 概念说明

「路由」是 MoE 的灵魂。它需要回答一个问题：**对当前这个 token，该派给哪几个专家？**

实现上用一个非常轻量的线性层 `gate`（也叫 router）：把 `emb_dim` 维的 token 向量映射成 `num_experts` 个**分数（logits）**，分数越高表示该 token 越「适合」这位专家。然后：

- **top-k**：只取分数最高的 `k` 个专家，其余专家对这个 token 直接「不参与」。
- **softmax**：注意，softmax 只在这 k 个被选中的分数上做，得到 k 个相加为 1 的概率权重——稍后用它们加权聚合 k 个专家的输出。

为什么要「先 top-k 再 softmax」，而不是「先对所有专家 softmax 再选 top-k」？因为前者保证被选中专家的权重重新归一化到和为 1，输出量纲稳定；后者会让落选专家的概率被白白丢弃，权重量纲偏小。

#### 4.2.2 核心流程

对一个批次输入 `x: (batch, seq_len, emb_dim)`：

```
scores = gate(x)                      # (batch, seq_len, num_experts)
topk_scores, topk_indices = topk(scores, k, dim=-1)
                                      # 各取最高的 k 个：分数 + 它们的专家编号
topk_probs = softmax(topk_scores)     # 仅对这 k 个分数归一化
```

三行核心计算对应 [gpt_with_kv_moe.py:188-190](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/07_moe/gpt_with_kv_moe.py#L188-L190)。

#### 4.2.3 源码精读

[gpt_with_kv_moe.py:166](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/07_moe/gpt_with_kv_moe.py#L166) 定义门控层（无偏置）：

```python
self.gate = nn.Linear(cfg["emb_dim"], cfg["num_experts"], bias=False)
```

[gpt_with_kv_moe.py:186-190](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/07_moe/gpt_with_kv_moe.py#L186-L190) 完成打分、选 k、算概率：

```python
def forward(self, x):
    # x: (batch, seq_len, emb_dim)
    scores = self.gate(x)                                              # (b, seq_len, num_experts)
    topk_scores, topk_indices = torch.topk(scores, self.num_experts_per_tok, dim=-1)
    topk_probs = torch.softmax(topk_scores, dim=-1)
```

要点：

- `torch.topk(scores, k, dim=-1)` 返回两个张量：`topk_scores`（前 k 大的值）和 `topk_indices`（它们在专家维上的下标，即「是哪位专家」）。
- `torch.softmax(topk_scores, dim=-1)` 沿最后的 k 维归一化，得到 `topk_probs`。
- `gate` 本身的参数极少：仅 `emb_dim × num_experts`（例如 768×8≈6k），相比专家权重可忽略不计，但它却决定了整层的稀疏结构。

#### 4.2.4 代码实践：观察路由决策

**实践目标**：把路由过程「可视化」，看清每个 token 究竟被分给了哪些专家。

**操作步骤**：

```python
# 示例代码：紧接 4.1.4 的实例化
import torch
torch.manual_seed(123)
cfg = {"emb_dim": 768, "hidden_dim": 768*4, "num_experts": 8, "num_experts_per_tok": 2}
moe = MoEFeedForward(cfg)
x = torch.randn(2, 6, 768)

with torch.no_grad():
    scores = moe.gate(x)                                   # (2, 6, 8)
    topk_scores, topk_indices = torch.topk(scores, moe.num_experts_per_tok, dim=-1)
    topk_probs = torch.softmax(topk_scores, dim=-1)

print("第 0 个 token 选中的专家编号 :", topk_indices[0, 0].tolist())
print("对应的归一化概率         :", topk_probs[0, 0].round(decimals=3).tolist())
print("整批被激活过的不同专家   :", torch.unique(topk_indices).tolist())
print("概率和（应≈1.0）         :", topk_probs[0, 0].sum().item())
```

**需要观察的现象**：

- 每个 token 恰好选出 2 个专家编号（在 0~7 之间）。
- 两个概率之和约等于 1.0。
- 「整批被激活过的不同专家」数量介于 2 和 8 之间（12 个 token 各选 2 个专家，去重后可能没把 8 个专家都用到）。

**预期结果**：每个 token 选 2 个专家；概率和为 1.0；不同专家数 ≤ 8。**待本地验证**：具体选了哪些专家、整批去重后剩几个，取决于随机种子，需你本机运行确认。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `num_experts_per_tok` 设成等于 `num_experts`，MoE 退化成什么？

**答案**：每个 token 选全部专家，相当于所有专家都参与计算并按概率加权——失去了稀疏性，退化为一种「更贵的稠密层」（还多了一层门控开销）。

**练习 2**：为什么 `topk_probs` 要对「选中的 k 个分数」做 softmax，而不是对全部 `num_experts` 个分数做？

**答案**：只对选中项归一化能保证 k 个权重和为 1，使加权后的输出量纲稳定；若对全部专家归一化再丢弃落选者，权重和会小于 1，输出幅度被系统性压低。

### 4.3 专家稀疏计算与概率加权聚合

#### 4.3.1 概念说明

路由选好专家后，就要「真正算」了。朴素做法是：对每个 token，遍历它选中的 k 个专家，逐个调用对应 FFN。但这在 GPU 上很低效——大量小矩阵乘法无法利用并行。

本实现采用更高效的「**按专家分组**」策略：

- 把所有 token 摊平（`batch × seq_len` 个 token），找出「哪些 token 被路由到了专家 e」。
- 把这些 token 收拢成一批，**一次性**喂给专家 e 的 FFN（一次大矩阵乘法）。
- 对每个被激活的专家重复一遍。

每位专家的内部是一个 **SwiGLU** 前馈单元（门控线性单元 + Swish 激活）：

\[
\text{hidden} = \operatorname{SiLU}(W_1\,x)\ \odot\ (W_2\,x), \qquad
\text{out} = W_3\,\text{hidden}
\]

其中 \(\operatorname{SiLU}(z)=z\cdot\sigma(z)\)（也叫 Swish），\(\odot\) 是逐元素乘。`fc1/fc2/fc3` 分别对应 \(W_1/W_2/W_3\)，所以每位专家有 **3 个**权重矩阵（这是它与 GELU 版稠密 FFN「2 个矩阵」的区别，也是 `memory_estimator_moe.py` 里 `swiglu → 3 个矩阵` 的由来）。

最后，对每个 token，把选中的 k 个专家输出按门控概率加权求和：

\[
y = \sum_{e \in \text{top-k}(x)} p_e(x)\cdot \text{FFN}_e(x)
\]

因为 \(\sum_e p_e(x)=1\)，这其实是对 k 个专家输出的**加权平均**。

#### 4.3.2 核心流程

[gpt_with_kv_moe.py:192-227](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/07_moe/gpt_with_kv_moe.py#L192-L227) 的算法可拆成「摊平 → 遍历专家 → 收拢计算 → 散播回填」四步：

```
1. 摊平：x_flat = x.reshape(batch*seq_len, emb_dim)
         out_flat = 全零张量 (batch*seq_len, emb_dim)  # 用来累积各专家贡献
         topk_indices_flat, topk_probs_flat 也摊平成 (N, k)

2. 遍历被激活的专家（torch.unique 去重）:
   对每个专家 e:
     a) mask = (topk_indices_flat == e)        # 哪些 (token, 槽位) 选了 e
        token_mask = mask.any(dim=-1)          # 这些 token 里「至少有一个槽」选了 e
        selected_idx = token_mask 非零下标      # 被路由到 e 的 token 行号
     b) expert_input = x_flat[selected_idx]     # 收拢这批 token
        hidden = SiLU(fc1_e(input)) ⊙ fc2_e(input)
        expert_out = fc3_e(hidden)              # SwiGLU 一次算完
     c) 对每个选中 token，取出它在 top-k 里的概率 p_e
        out_flat[selected_idx] += expert_out * p_e   # 概率加权后散播回原位（index_add_）

3. 还原：return out_flat.reshape(batch, seq_len, emb_dim)
```

关键细节：

- **`index_add_`（原地累加）**：一个 token 若被路由到 2 个专家，两位专家的贡献会分别「加」到 `out_flat` 的同一行，天然实现了「加权求和」。
- **`slot_indices = argmax(mask)` 技巧**（[gpt_with_kv_moe.py:219-223](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/07_moe/gpt_with_kv_moe.py#L219-L223)）：某专家可能是某 token 的「第 1 选择」也可能是「第 2 选择」，这段代码定位它在 top-k 槽位中的位置，从而取出正确的概率权重。

#### 4.3.3 源码精读

[gpt_with_kv_moe.py:192-198](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/07_moe/gpt_with_kv_moe.py#L192-L198) 摊平并准备累加器：

```python
batch, seq_len, _ = x.shape
x_flat = x.reshape(batch * seq_len, -1)
out_flat = torch.zeros(batch * seq_len, self.emb_dim, device=x.device, dtype=x.dtype)

topk_indices_flat = topk_indices.reshape(-1, self.num_experts_per_tok)
topk_probs_flat = topk_probs.reshape(-1, self.num_experts_per_tok)
```

[gpt_with_kv_moe.py:199-217](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/07_moe/gpt_with_kv_moe.py#L199-L217) 是「按专家分组稀疏计算」的核心循环——注意 `torch.unique` 只遍历真正被激活的专家，SwiGLU 的三步运算在一行内完成：

```python
unique_experts = torch.unique(topk_indices_flat)

for expert_id_tensor in unique_experts:
    expert_id = int(expert_id_tensor.item())
    mask = topk_indices_flat == expert_id
    if not mask.any():
        continue
    token_mask = mask.any(dim=-1)
    selected_idx = token_mask.nonzero(as_tuple=False).squeeze(-1)
    ...
    expert_input = x_flat.index_select(0, selected_idx)
    hidden = torch.nn.functional.silu(self.fc1[expert_id](expert_input)) * self.fc2[expert_id](expert_input)
    expert_out = self.fc3[expert_id](hidden)
```

> 这里的 `silu(fc1(x)) * fc2(x)` 正是上文的 SwiGLU 公式。

[gpt_with_kv_moe.py:225](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/07_moe/gpt_with_kv_moe.py#L225) 用 `index_add_` 把加权结果累加回 `out_flat`：

```python
out_flat.index_add_(0, selected_idx, expert_out * selected_probs.unsqueeze(-1))
```

最后 [gpt_with_kv_moe.py:227](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/07_moe/gpt_with_kv_moe.py#L227) 把摊平的结果还原成 `(batch, seq_len, emb_dim)` 返回——形状与输入完全一致，故能被 `TransformerBlock` 当成普通前馈层使用。

#### 4.3.4 代码实践：定量比较激活参数量

**实践目标**：亲手算出 MoE 的「总参数」与「单 token 激活参数」，体会稀疏带来的算力节省。

**操作步骤**：

```python
# 示例代码：参数量对比
import torch
from gpt_with_kv_moe import MoEFeedForward, FeedForward

def num_params(m):
    return sum(p.numel() for p in m.parameters())

emb_dim, hidden_dim = 768, 768 * 4
num_experts, top_k = 8, 2

# MoE（SwiGLU：每专家 3 个矩阵）
moe = MoEFeedForward({
    "emb_dim": emb_dim, "hidden_dim": hidden_dim,
    "num_experts": num_experts, "num_experts_per_tok": top_k,
})
# 稠密 FFN（本文件里用 GELU：2 个矩阵）
dense = FeedForward({"emb_dim": emb_dim, "hidden_dim": hidden_dim})

# MoE 单 token 激活参数 ≈ 路由器 + top_k 个专家
per_expert = 3 * emb_dim * hidden_dim          # SwiGLU 每专家参数
router     = emb_dim * num_experts
moe_active = router + top_k * per_expert

print(f"稠密 FFN 参数量        : {num_params(dense):>12,}  (全部激活)")
print(f"MoE 总参数量           : {num_params(moe):>12,}")
print(f"MoE 单 token 激活参数  : {moe_active:>12,}")
print(f"激活比例 active/total  : {moe_active / num_params(moe):.1%}")
print(f"激活比例 ≈ top_k/N     : {top_k / num_experts:.1%}")
```

**需要观察的现象**：

- 稠密 FFN 参数全部激活。
- MoE 总参数远大于稠密 FFN（因为有 8 套专家权重）。
- MoE 单 token 激活参数 ≈ `router + 2 × 每专家参数`。
- 两个「激活比例」数字应非常接近（一个来自精确统计，一个来自 `top_k/num_experts` 近似）。

**预期结果**（待本地验证具体数值）：稠密 FFN ≈ 472 万参数；MoE 总参数 ≈ 5660 万；MoE 单 token 激活 ≈ 1416 万；激活比例约 25%（= 2/8）。

> **关键提醒**：这里 MoE 的「单 token 激活参数」仍大于稠密 FFN，是因为我们让每位专家的 `hidden_dim` 和稠密层一样大。真正的「省算力」对照方式是让 **MoE 总参数 ≈ 稠密 FFN 参数**（即缩小每位专家的 `hidden_dim`），再用 `top_k/num_experts` 折算——这正是综合实践里 `memory_estimator_moe.py --match_dense` 的做法。

#### 4.3.5 小练习与答案

**练习 1**：为什么用 `index_add_` 而不是直接赋值 `out_flat[selected_idx] = ...`？

**答案**：一个 token 可能被 2 个专家选中，两位专家都要把各自的加权贡献写入 `out_flat` 的同一行。`index_add_` 是「累加」，能把多个专家的贡献叠加成加权求和；直接赋值会相互覆盖，只剩最后一个写入的专家。

**练习 2**：SwiGLU 的 `silu(fc1(x)) * fc2(x)` 里，`fc2` 起到什么作用？

**答案**：`fc2` 是「门控」分支，它与 `fc1` 经 SiLU 后的值逐元素相乘，起到「按通道动态放行/抑制」的作用；`fc3` 再把门控后的中间表示投回 `emb_dim`。相比 GELU 版的两层 FFN，SwiGLU 多一个门控矩阵，通常表现更好。

## 5. 综合实践

把 4.1～4.3 串起来，完成本讲规格里的综合任务：**实例化 `MoEFeedForward`、跑一次前向并打印被激活的专家数量，再对比它与稠密 FFN 的激活参数量**；最后用官方工具验证「等容量下 MoE 更省」的结论。

**实践目标**：

1. 跑通 MoE 前向，统计本次前向实际激活了几个不同专家。
2. 定量对比 MoE 与稠密 FFN 的参数结构。
3. 用 `memory_estimator_moe.py --match_dense` 验证「相同总参数下，MoE 单 token 激活参数更少」。

**操作步骤**：

第 1 步——在 `ch04/07_moe/` 目录下创建并运行 `practice_moe.py`：

```python
# 示例代码：综合实践
import torch
from gpt_with_kv_moe import MoEFeedForward, FeedForward

torch.manual_seed(123)
emb_dim, hidden_dim = 768, 768 * 4
num_experts, top_k = 8, 2

moe = MoEFeedForward({
    "emb_dim": emb_dim, "hidden_dim": hidden_dim,
    "num_experts": num_experts, "num_experts_per_tok": top_k,
})
x = torch.randn(2, 6, emb_dim)   # 12 个 token

with torch.no_grad():
    out = moe(x)
    # 复现路由，统计本次前向激活的不同专家
    scores = moe.gate(x)
    _, topk_indices = torch.topk(scores, top_k, dim=-1)
    activated = torch.unique(topk_indices).tolist()

print("输出形状           :", tuple(out.shape))
print("被激活的不同专家数量:", len(activated), "其编号:", activated)

# 参数量对比
def num_params(m):
    return sum(p.numel() for p in m.parameters())

dense = FeedForward({"emb_dim": emb_dim, "hidden_dim": hidden_dim})
per_expert = 3 * emb_dim * hidden_dim
moe_active = emb_dim * num_experts + top_k * per_expert

print(f"稠密 FFN 参数量(全激活): {num_params(dense):,}")
print(f"MoE 总参数量           : {num_params(moe):,}")
print(f"MoE 单 token 激活参数  : {moe_active:,}")
print(f"激活比例 active/total  : {moe_active / num_params(moe):.1%}")
```

第 2 步——运行官方内存估算工具，做「等容量」对照（仍在 `ch04/07_moe/` 目录）：

```bash
uv run memory_estimator_moe.py \
    --emb_dim 768 --hidden_dim 3072 --ffn_type swiglu \
    --num_experts 8 --top_k 2 --match_dense
```

**需要观察的现象**：

- 第 1 步：`被激活的不同专家数量` 在 2~8 之间（12 个 token 各选 2 个专家，去重后的结果，受随机种子影响）；输出形状为 `(2, 6, 768)`。
- 第 1 步：稠密 FFN 参数全激活；MoE 总参数大得多，但单 token 只激活约 25%。
- 第 2 步：`--match_dense` 会自动缩小每位专家的 `moe_hidden_dim`，使 `MoE TOTAL params ≈ Dense FFN params`，此时 `MoE ACTIVE/Token` 明显小于 `Dense FFN params`——这就是「等容量下 MoE 更省算力」。

**预期结果**：

- 第 1 步输出形状 `(2, 6, 768)`；激活专家数量 ≤ 8；激活比例约 25%。具体专家编号**待本地验证**。
- 第 2 步会打印类似下面的结论（具体数值取决于参数）：`MoE TOTAL params` 与 `Dense FFN params` 接近，而 `MoE ACTIVE/Token` 约为 `Dense FFN params × (top_k/num_experts) ≈ 25%`。README 给出的 `emb_dim=7168` 大模型示例中，稠密 FFN ≈ 3.08 亿参数，而等容量 MoE 每 token 仅激活 ≈ 0.77 亿参数（节省约 4×）。

> 进阶观察：README 的实测还指出，MoE 用更小的激活内存换取了约 2 倍的前馈计算时间（路由开销 + 实现未必最优）。也就是说 MoE 的权衡是「省显存、加计算」，并非全方位免费午餐。

## 6. 本讲小结

- **MoE 的本质**：把 Transformer 块里的单个前馈层换成「多个并列专家 + 一个门控路由」，用大总参数量（高容量）换小单步激活量（低算力/显存），实现稀疏激活。
- **无缝替换**：`MoEFeedForward` 的输入输出形状与稠密 `FeedForward` 完全一致（都是 `(batch, seq_len, emb_dim)`），`TransformerBlock` 仅用一行三元判断即可切换，注意力与残差结构完全不动。
- **门控路由**：`gate` 线性层对每个 token 给 `num_experts` 个分数，`torch.topk` 选 `top_k` 个专家，再对这 k 个分数做 softmax 得到归一化概率（保证和为 1）。
- **稀疏计算技巧**：用 `torch.unique` 只遍历被激活的专家、把同专家的 token 收拢成一批一次算（SwiGLU：`silu(fc1(x))*fc2(x)` → `fc3`），再用 `index_add_` 把加权结果累加回原位——天然实现「k 个专家输出按概率加权求和」。
- **激活比例**：单 token 激活参数约占总参数的 `top_k/num_experts`（如 2/8=25%），加专家不增算力；真正「省算力」的对照要让 MoE 总参数 ≈ 稠密 FFN（`--match_dense`），再按此比例折算。
- **权衡**：MoE 以更低的激活内存/显存，换取更高的前馈计算开销（路由 + 调度），并带来「负载均衡」等训练难题（本讲未涉及）。

## 7. 下一步学习建议

本讲只替换了前馈层、且模型未训练。建议接着探索：

1. **看一个真正训练过的 MoE**：README 指向 [ch05/11_qwen3/standalone-qwen3-moe-plus-kvcache.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/11_qwen3/standalone-qwen3-moe-plus-kvcache.ipynb)，它把 MoE 与 KV cache 结合并训练，可观察 MoE 生成连贯文本的效果（对应 u10-l2 现代架构）。
2. **与其它注意力/前馈变体横向比较**：结合 u9-l3（GQA/MLA/SWA，关注 KV 内存）与本讲（MoE，关注 FFN 计算），理解现代 LLM 在「注意力层」和「前馈层」两条线上分别做的稀疏化。
3. **补全训练侧知识（进阶）**：本讲的 MoE 没有「负载均衡损失（load balancing loss）」——真实训练时若某些专家总被冷落，模型会退化。可自行检索 Switch Transformer / DeepSeek-V3 的辅助损失设计，理解如何让路由更均衡。
4. **回看架构主线**：把本讲放回 u4（GPT 模型）→ u9（高效推理）→ u10（现代架构）的脉络中，你会发现 MoE 正是从「教科书 GPT」走向「现代百亿/千亿模型」的关键一步。
