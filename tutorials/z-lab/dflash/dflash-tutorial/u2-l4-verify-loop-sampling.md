# 验证接受循环与采样

## 1. 本讲目标

[u2-l1](u2-l1-spec-decoding-control-flow.md) 已经画好了 `dflash_generate` 的**全局控制流地图**：prefill → decode 循环（块起草 + 验证）→ 收尾，并且用「俯瞰图」讲清了每轮产出 `acceptance_length + 1` 个 token。但当时为了不分散注意力，故意把两样东西当黑盒留下了：

- `sample()` 这个被反复调用的小函数，到底怎么把 logits 变成 token？
- 「验证 + 接受 + 裁剪」这三步的**精确对齐**与**KV cache 裁剪**细节。

本讲就把这两个黑盒彻底打开。学完本讲你应该能够：

1. 逐行讲清 [`sample`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L48-L54) 的两个分支（贪心 `argmax` 与温度 `multinomial`），并说出它在 `dflash_generate` 里被调用了**三次**、每次用哪个 `temperature`。
2. 说清验证段里 `block_output_ids[:, 1:]` 与 `posterior[:, :-1]` **这两个切片为什么正好对齐**，以及 `cumprod` 如何求出最长公共前缀。
3. 解释那个「兜底 token」为什么一定来自 target、一定正确，并由此推出一个重要结论：**DFlash 的输出永远等于 target 的采样结果，质量不损失**。
4. 说清为什么接受后必须**裁剪两套 KV cache**（target 缓存用于回滚被拒草稿、draft 缓存用于维持干净上下文），以及不裁会发生什么。
5. 解释为什么**调高 `temperature` 会降低平均接受长度**（从而削弱加速），但不会降低生成质量——这正是本讲综合实践要验证的现象。

本讲承接 [u2-l3](u2-l3-attention-block-diffusion.md)：上一讲讲完了草稿模型一次前向**内部**如何拼接 context 与 noise、如何产出 `draft_logits`；本讲回到 `dflash_generate` 的 decode 循环，接上「`draft_logits` 出来之后」这一段。

## 2. 前置知识

阅读本讲前，建议先掌握以下概念（多数已在前面讲义建立）：

- **投机解码的「起草 + 验证」回路**（[u1-l1](u1-l1-project-overview.md) / [u2-l1](u2-l1-spec-decoding-control-flow.md)）：草稿快速猜一串候选，target 一次前向并行验证，保留猜对的前缀再补一个 target 的兜底 token。每轮产出 `a + 1` 个 token。
- **greedy vs 温度采样**：给定一个位置上的 logits（词表上的未归一化分数），「采样」就是从中挑一个 token id。最简单的是 `argmax`（取分数最高的，确定性、贪心）；要引入随机性，就把 logits 除以温度 `T` 再 softmax 成概率分布，按概率**抽样**（`multinomial`）。`T` 越大分布越平、越随机；`T → 0` 趋近贪心。
- **DynamicCache 与 crop**（[u2-l1](u2-l1-spec-decoding-control-flow.md) 2.2 节）：自回归生成时把每层算过的 Key/Value 存下来避免重算；`crop(up_to)` 把缓存截短到只保留前 `up_to` 个 token。DFlash 维护 **target / draft 两套** `DynamicCache`。
- **softmax 与 multinomial 的形状约定**：`torch.softmax(x, dim=-1)` 沿最后一维归一化；`torch.multinomial(probs, num_samples=1)` 要求 `probs` 是二维 `(行数, 词表)`，每行按概率抽 1 个样本，返回 `(行数, 1)` 的索引。

一句话复习衔接：在 decode 循环每一轮里，草稿模型拿到「投影好的上下文 `target_hidden`」和「本块 mask 嵌入 `noise_embedding`」做一次前向，产出 `draft_logits`（见 [u2-l3](u2-l3-attention-block-diffusion.md)）。本讲要回答的是：**这串 `draft_logits` 出来之后，怎么变成候选 token、怎么被 target 验证、接受后怎么写回与回滚。**

## 3. 本讲源码地图

本讲全部围绕 `dflash/model.py`，但在它的几个区段间反复跳转：

| 区段 | 行号 | 作用 |
| --- | --- | --- |
| `sample` | [`dflash/model.py:48-54`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L48-L54) | 本讲第一个主角：logits → token 的两个分支 |
| `sample` 的三个调用点 | [`dflash/model.py:97`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L97)、[`121`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L121)、[`134`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L134) | prefill 首 token / 草稿候选 / target 验证，注意各自用哪个 `temperature` |
| decode 循环 | [`dflash/model.py:107-148`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L107-L148) | 本讲第二个主角：验证 + 接受 + 裁剪 |
| 两处 crop | [`dflash/model.py:120`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L120)、[`139`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L139) | draft 缓存裁剪（用旧 start）/ target 缓存裁剪（用新 start） |
| benchmark 消费接受长度 | [`dflash/benchmark.py:120-132`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L120-L132) | 佐证 `acceptance_lengths` 的含义与直方图算法 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，正好对应规格里的三个模块：`sample` 函数、验证与接受长度计算、接受后输出更新与双 KV cache 裁剪。

### 4.1 `sample` 函数：logits → token 的两个分支

#### 4.1.1 概念说明

神经语言模型在每个位置输出的是一个长度为词表大小 \(V\) 的向量 **logits**（每个词的「原始分数」）。要把 logits 变成最终输出的 token id，需要一步**采样**。采样有两种截然不同的策略：

1. **贪心（greedy）**：直接取分数最大的那个词，`argmax(logits)`。结果是**确定性**的——同样的 logits 永远产出同一个 token。等价于温度 \(T=0\)。
2. **温度采样（stochastic）**：先把 logits 除以温度 \(T\)，再 softmax 成概率分布 \(p\)，最后按 \(p\) **随机抽**一个词（`multinomial`）。结果带随机性，适合需要多样性的场景（写作、对话）。

温度 \(T\) 的作用是调节分布的「尖锐程度」：

\[
p_i \;=\; \mathrm{softmax}\!\left(\frac{\mathrm{logits}}{T}\right)_i \;=\; \frac{\exp(\mathrm{logits}_i / T)}{\sum_j \exp(\mathrm{logits}_j / T)}
\]

- \(T\) 越小，最大分数的词概率越接近 1，分布越「尖」，采样越接近贪心；
- \(T\) 越大，分数被「压平」，分布越均匀，采样越随机。

`sample` 就是把这两种策略用一个函数统一封装，并用 `temperature` 的大小做分流。

#### 4.1.2 核心流程

```text
sample(logits, temperature):
  if temperature < 1e-5:           # 贪心分支
      return argmax(logits, dim=-1)
  else:                            # 温度采样分支
      (bsz, seq, V) = logits.shape
      logits = logits.reshape(bsz*seq, V) / temperature   # 缩放
      probs  = softmax(logits, dim=-1)                    # 归一化成概率
      return multinomial(probs, num_samples=1).reshape(bsz, seq)
```

两个分支的输入输出形状一致：输入 `(bsz, seq_len, V)`，输出 `(bsz, seq_len)`（每个位置一个 token id）。

为什么贪心要单独成支、而不是写 `softmax(logits/0)`？因为 \(T \to 0\) 时 `logits / T` 会数值爆炸（除以接近 0 的数），所以代码用 `temperature < 1e-5` 当门槛，直接走无需除法的 `argmax`，既正确又数值稳定。

#### 4.1.3 源码精读

[`sample` 的完整实现](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L48-L54)（这是本讲最短也最值得逐字读的函数）：

```python
def sample(logits: torch.Tensor, temperature: float = 0.0) -> torch.Tensor:
    if temperature < 1e-5:
        return torch.argmax(logits, dim=-1)
    bsz, seq_len, vocab_size = logits.shape
    logits = logits.view(-1, vocab_size) / temperature
    probs = torch.softmax(logits, dim=-1)
    return torch.multinomial(probs, num_samples=1).view(bsz, seq_len)
```

逐行：

- `temperature < 1e-5`：默认参数 `temperature=0.0`，所以**不传温度就等于贪心**——这条默认值是本讲后面解释「草稿永远贪心」的关键。
- `torch.argmax(logits, dim=-1)`：沿词表维（最后一维）取最大值索引，输入 `(bsz, seq, V)` → 输出 `(bsz, seq)`。
- `logits.view(-1, vocab_size) / temperature`：把三维展平成 `(bsz*seq, V)` 再除以温度。`multinomial` 只接受二维输入，所以必须先展平。
- `torch.softmax(logits, dim=-1)`：沿词表维归一化成概率。
- `torch.multinomial(probs, num_samples=1).view(bsz, seq_len)`：每行按概率抽 1 个样本，得 `(bsz*seq, 1)`，再 reshape 回 `(bsz, seq)`。

**`sample` 在 `dflash_generate` 里的三次调用**（这是本讲最重要的一张表）：

| 调用点 | 代码 | 用的温度 | 含义 |
| --- | --- | --- | --- |
| prefill 首 token | [`sample(output.logits, temperature)`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L97) | 用户的 `temperature` | prompt 之后第一个 token，由 target 决定 |
| **草稿候选** | [`sample(draft_logits)`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L121) | **默认 0.0（贪心！）** | 草稿块里的 B−1 个候选 token |
| target 验证 | [`sample(output.logits, temperature)`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L134) | 用户的 `temperature` | target 对每个位置的「判决」 |

请注意中间那一行：**草稿候选没有传 `temperature`，所以它永远是贪心 `argmax`**，无论用户设了多大的温度。这个不对称是本讲的「题眼」——它直接决定了 4.3 里「调高温度会降低接受长度」的机制，请先记住它。

#### 4.1.4 代码实践

**目标**：单独把 `sample` 当纯函数测，理解两个分支与温度缩放的效果。**这个实践不需要 GPU，也不需要加载模型**，CPU 即可跑。

**步骤 1**（示例代码，可直接复制运行）：

```python
import torch

# 为了不安装整个 dflash，这里把 sample 的 7 行定义原样贴出来测试
def sample(logits: torch.Tensor, temperature: float = 0.0) -> torch.Tensor:
    if temperature < 1e-5:
        return torch.argmax(logits, dim=-1)
    bsz, seq_len, vocab_size = logits.shape
    logits = logits.view(-1, vocab_size) / temperature
    probs = torch.softmax(logits, dim=-1)
    return torch.multinomial(probs, num_samples=1).view(bsz, seq_len)

# 构造 (1, 2, 3): batch=1, 2 个位置, 词表大小 3
logits = torch.tensor([[[1.0, 5.0, 2.0],   # 位置0: 词1 分数最高
                        [3.0, 0.5, 4.0]]])  # 位置1: 词2 分数最高
print("贪心 (T=0)  :", sample(logits).tolist())             # 期望 [[1, 2]]
print("T=0.5 抽样5次:")
for _ in range(5):
    print("          ", sample(logits, temperature=0.5).tolist())
```

**步骤 2**：若已按 [u1-l3](u1-l3-package-structure.md) 执行过 `uv pip install -e .`，可直接用真实函数替换上面的定义：

```python
from dflash.model import sample   # 会触发 transformers 导入, 但不需要 GPU
```

**需要观察的现象**：

- 贪心分支永远返回 `[[1, 2]]`（两个位置的 argmax），多次运行结果不变。
- `T=0.5` 时，由于分数差被放大后差距仍大，大多数抽样仍是 `[[1, 2]]`，但偶尔会在某个位置抽到别的词。
- 把温度调大到 `2.0` 再看：分布被压平后，抽样结果明显更随机、更多样。

**预期结果**：贪心确定性；温度越高，抽样越偏离 argmax、越随机。这正是后续「target 在高温下会偏离自己的 argmax」的微观机制。具体抽样结果「待本地验证」（`multinomial` 有随机性）。

#### 4.1.5 小练习与答案

**Q1**：如果不写 `if temperature < 1e-5` 这条分支，直接对 `temperature=0.0` 做 `logits / temperature` 会怎样？

**答案**：会除以 0，产生 `inf`/`nan`，`softmax` 后得到 `nan`，`multinomial` 报错或给出无意义结果。这条分支既是为了语义（贪心即温度为 0），也是为了**数值稳定**。

**Q2**：`temperature=0.0001` 会走哪个分支？为什么门槛用 `1e-5` 而不是严格的 `== 0`？

**答案**：走**温度采样分支**。因为 `0.0001 = 1e-4 > 1e-5`，条件 `temperature < 1e-5` 为假，于是进入 `else`。也就是说，只有严格小于 `1e-5`（如 `0.0`、`1e-6`）才算贪心。门槛设成一个很小的正数 `1e-5` 而非 `== 0`，是为了把「接近 0 的极小温度」也安全地归入无需除法的贪心分支，避免 `logits / 1e-7` 这种放大千万倍导致的浮点溢出。注意 `1e-4` 这种温度虽然走采样，但因分布极度尖锐，行为已非常接近贪心。

---

### 4.2 target 验证与 `cumprod` 接受长度（decode 循环核心）

#### 4.2.1 概念说明

草稿产出了 B−1 个候选 token（贪心 argmax 得到），但这串候选可能部分猜错。**验证**就是让 target 对这一整块做**一次前向**，给出每个位置「下一个 token 应该是什么」的判决；**接受**就是把草稿候选与 target 的判决逐位比对，找出**从头开始连续命中的最长前缀**，这个长度就是接受长度 \(a\)。

这里有一个和 [u2-l1](u2-l1-spec-decoding-control-flow.md) 不同的视角要强调：target 的判决本身也是一次**采样**（`posterior = sample(output.logits, temperature)`）。于是「命中」的精确定义是：草稿的贪心候选 token **等于** target 在该位置的采样结果。这个定义会引出本讲最重要的结论（见 4.2.2 末尾的洞察）。

#### 4.2.2 核心流程

设当前已提交到位置 `start`，块大小为 B。块的 B 个 token 是：

```text
block_output_ids = [ x_start,  d_{start+1},  d_{start+2},  ...,  d_{start+B-1} ]
                     锚点        草稿候选1     草稿候选2          草稿候选(B-1)
```

其中 `x_start` 是已提交的真锚点（不参与验证比对），后 B−1 个是草稿候选。target 一次前向处理整块，得到 `output.logits` 形状 `(1, B, V)`，采样后 `posterior` 形状 `(1, B)`。关键要弄清 `posterior` 每一位预测的是**哪个位置**的 token：

- `posterior[j]` 是 target 看完位置 `start+j` 后、对**下一个位置** `start+j+1` 的预测。

所以：

| posterior 索引 | 预测的位置 | 应与谁比对 |
| --- | --- | --- |
| `posterior[0]` | `start+1` | 草稿候选 `d_{start+1}` |
| `posterior[1]` | `start+2` | 草稿候选 `d_{start+2}` |
| … | … | … |
| `posterior[B-2]` | `start+B-1` | 草稿候选 `d_{start+B-1}` |
| `posterior[B-1]` | `start+B`（块外的新位置） | 不比对，作为「兜底」 |

于是比对的两边天然对齐：草稿候选是 `block_output_ids[:, 1:]`（B−1 个，位置 `start+1..start+B-1`），target 判决是 `posterior[:, :-1]`（B−1 个，预测的也正是 `start+1..start+B-1`）。两个切片长度都是 B−1，逐位一一对应。

```text
# ── 验证 ──
posterior = sample(output.logits, temperature)          # (1, B)

# ── 接受长度（最长公共前缀）──
match = (block_output_ids[:, 1:] == posterior[:, :-1])  # (1, B-1) 布尔
a     = match.cumprod(dim=1).sum(dim=1)                 # 标量

# ── 提交：命中前缀 + 兜底 token ──
output_ids[start : start+a+1] = block_output_ids[:, :a+1]   # 锚点 + a 个命中候选
output_ids[start+a+1]         = posterior[:, a]             # 兜底（target 的判决）
start += a + 1
```

**`cumprod` 求最长公共前缀的数学表达**：令 \(m_i = \mathbb{1}[d_{start+i} = \text{posterior}[i-1]]\)（\(i=1,\dots,B-1\)）表示第 \(i\) 个候选是否命中，则

\[
a \;=\; \sum_{k=1}^{B-1}\;\prod_{i=1}^{k}\; m_i
\]

累积乘 \(\prod\) 的作用是：**一旦某个位置不命中（\(m_i=0\)），从该项起后面所有项全归零**，于是求和只数到第一个失配处，正好得到「从头开始的连续命中数」。\(a \in [0,\, B-1]\)。

**兜底 token 永远正确**：`posterior[:, a]` 是 target 对位置 `start+a+1` 的判决。若 \(a < B-1\)，说明第 \(a+1\) 个候选失配，用 target 的判决顶替它；若 \(a = B-1\)（全中），`posterior[:, B-1]` 预测的是块外全新的下一个位置。两种情况下它都直接来自 target，因此**一定被采纳、永不丢失**。这就是每轮产出 `a + 1` 里那个 `+1` 的来源（[u2-l1](u2-l1-spec-decoding-control-flow.md) 已点出，这里给出了精确的索引解释）。

> **关键洞察（本讲最重要的结论）**：因为草稿**永远贪心**采样（4.1.3），所以「候选被接受」蕴含「草稿的 argmax == target 的采样」。再注意到：被拒的位置写回的也是 target 的采样（兜底）。于是**每个被提交的 token 都等于 target 在该位置的采样结果**——DFlash 的输出序列与「单独用 target 按 `temperature` 采样」**完全等价**，只是当草稿恰好猜中时省下一次前向。换句话说，**DFlash 是纯粹的加速手段，不改变、也不损失生成质量**。`temperature` 只影响「草稿的 argmax 命中 target 采样的频率」，进而影响速度，而不影响输出分布。这个结论是 4.3 与综合实践的理论基石。

#### 4.2.3 源码精读

[验证 + 接受段](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L126-L140)：

```python
output = target(
    block_output_ids,
    position_ids=block_position_ids,
    past_key_values=past_key_values_target,   # target 缓存增长 B
    use_cache=True,
    output_hidden_states=block_size > 1,
)
posterior = sample(output.logits, temperature)
acceptance_length = (block_output_ids[:, 1:] == posterior[:, :-1]).cumprod(dim=1).sum(dim=1)[0].item()
output_ids[:, start : start + acceptance_length + 1] = block_output_ids[:, : acceptance_length + 1]
output_ids[:, start + acceptance_length + 1] = posterior[:, acceptance_length]
start += acceptance_length + 1
past_key_values_target.crop(start)
acceptance_lengths.append(acceptance_length + 1)
```

逐行（KV cache 的 `crop` 留到 4.3 讲，这里先聚焦验证与接受）：

- `target(block_output_ids, ...)`：target 对整块做**一次**前向，`output.logits` 形状 `(1, B, V)`，给出 B 个位置各自的 next-token 分布。这一步让 target 缓存增长了 B。
- `posterior = sample(output.logits, temperature)`：把每个位置的分布采样成一个 token，得 `(1, B)`。注意它用的是**用户的 `temperature`**。
- `acceptance_length = ...`：核心一行。`block_output_ids[:, 1:]`（B−1 个候选）对 `posterior[:, :-1]`（B−1 个 target 判决）逐位相等比较 → `cumprod(dim=1)` 取前缀 → `sum(dim=1)` 数长度 → `[0]` 取 batch 0（DFlash 固定 batch=1）→ `.item()` 转成 Python int。
- 写回命中前缀 `block_output_ids[:, : a+1]`（锚点 + a 个命中候选，锚点重复写无害）；兜底 `posterior[:, a]` 写到 `start+a+1`。
- `start += a + 1`；`acceptance_lengths.append(a + 1)` 记录**本轮产出**（注意存的是 `a + 1`，不是 `a`）。

**手动演算（示例代码，强调对齐与兜底，可与 [u2-l1](u2-l1-spec-decoding-control-flow.md) 4.3.3 的例子互补）**：

设 `block_size = 4`，`start = 10`，`block_output_ids = [锚点, 5, 7, 9]`，target 采样得 `posterior = [5, 7, 8, 2]`（四位分别预测位置 11/12/13/14）。

- 候选 `block[:, 1:] = [5, 7, 9]`（位置 11/12/13）；判决 `posterior[:, :-1] = [5, 7, 8]`（预测位置 11/12/13）。
- 相等比较 = `[1, 1, 0]`；`cumprod = [1, 1, 0]`；`sum = 2` → `a = 2`。
- 写回 `block[:, :3] = [锚点, 5, 7]`（位置 10/11/12）；兜底 `posterior[2] = 8` 写到位置 13。
- 本轮提交位置 11、12、13 三个新 token（5、7、8），`start = 10 + 3 = 13`。

注意位置 13：草稿候选是 `9`，target 判决是 `8`，失配 → 用 target 的 `8` 顶替。而 **`8` 正是 target 的采样结果**，符合「输出永远等于 target 采样」的洞察。`posterior[3] = 2` 预测的是块外位置 14，本轮因失配发生在更早处而未被用到。

**边界情形 \(a = B-1\)（全中）**：若上例 `posterior = [5, 7, 9, 2]`，则比较 = `[1,1,1]`、`a = 3`，写回 `block[:, :4] = [锚点,5,7,9]`，兜底 `posterior[3] = 2` 写到位置 14。本轮产出 4 个 token（5、7、9、2），全部来自 target 的采样（前三个恰好等于草稿的 argmax，第四个是块外兜底）。

**循环收尾两件事**（[`L142-L148`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L142-L148)）：把 `target_hidden` 更新为本轮 target 隐藏状态的前 `a+1` 个位置（只留命中部分作为下一轮草稿的上下文），并在命中 `stop_token_ids` 时 `break`。

#### 4.2.4 代码实践（源码阅读型，无需 GPU）

**目标**：用纸笔验证 `cumprod` 求前缀的逻辑，并确认「输出 == target 采样」的洞察。

**步骤**：自己造若干组 `block_output_ids`（含锚点）与 `posterior`，按下表逐组手算 `a`、写回区间、兜底 token，并核对「每个提交的新 token 是否都来自 `posterior`」。

| 组 | block_output_ids | posterior | a | 写回 | 兜底 | 提交的新 token |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | [A, 5, 7, 9] | [5, 7, 8, 2] | 2 | [A,5,7] | 8 | 5,7,8 |
| 2 | [A, 5, 7, 9] | [5, 7, 9, 2] | 3 | [A,5,7,9] | 2 | 5,7,9,2 |
| 3 | [A, 5, 7, 9] | [4, 7, 9, 2] | 0 | [A] | 4 | 4 |
| 4 | [A, 5, 7, 9] | [5, 8, 9, 2] | 1 | [A,5] | 8 | 5,8 |

**需要观察并解释的现象**：

- 第 3 组 `a=0`（第一个候选就错），本轮仍产出 1 个 token（兜底 `4`），不卡住。
- **每一组「提交的新 token」一列，全都出现在对应 `posterior` 里**——这印证了「输出永远等于 target 采样」的结论。

**预期结果**：上表各列手算值一致；最后一列恒为 `posterior` 的某个子序列。

#### 4.2.5 小练习与答案

**Q1**：`acceptance_length` 的取值范围是多少？它和 `block_size` 的关系？

**答案**：\(a \in [0,\, B-1]\)，因为草稿只给出 B−1 个候选。每轮产出 `a+1 ∈ [1, B]`，即「最多产出 B 个，最少产出 1 个」。

**Q2**：为什么比较的是 `posterior[:, :-1]` 而不是整个 `posterior`？

**答案**：`posterior` 有 B 位，但最后一位 `posterior[B-1]` 预测的是**块外**的下一个位置，没有对应的草稿候选可比对（草稿只有 B−1 个）。所以只用前 B−1 位去比对，正好和 `block[:, 1:]` 的 B−1 个候选对齐；最后一位留作全中时的兜底。

**Q3**：如果草稿不是贪心、而是也带温度随机采样（假设改动代码），4.2.2 末尾「输出等于 target 采样」的结论还成立吗？

**答案**：不成立。若草稿也随机采样，那么「候选被接受」只意味着「草稿的某次随机抽样恰好等于 target 的抽样」，被接受的 token 是**草稿的随机抽样**而非 target 的——它只是数值上碰巧相等，并不改变「等于 target 采样」这一性质……实际上仍相等。更准确的破坏点在于：被接受时提交的是草稿候选本身（`block[:, :a+1]`），若草稿随机，则该 token 来自草稿的 RNG 而非 target 的分布，整体输出分布会偏离 target。**正是「草稿贪心」这一选择，保证了被接受即等于 target 采样，从而输出分布与 target 一致。**

---

### 4.3 接受后更新输出与双 KV cache 裁剪

#### 4.3.1 概念说明

接受长度算出来、token 写回 `output_ids` 之后，还差最后一件关键事：**裁剪 KV cache**。这一步如果不做，生成结果会**静默地变错**甚至崩溃。

原因在于一个不变量：**KV cache 里每个位置的 K/V，必须是用「该位置最终采用的那个 token」算出来的**。但验证阶段 target 一次性处理了整块 B 个 token（含被否决的候选），它的缓存因此多记了一堆「用错误候选 token 算出来的 K/V」。接受后这些位置的 token 已经被改写（失配处换成了 target 的兜底），可缓存里存的还是旧候选的 K/V——**缓存与真实 token 序列不一致了**。裁剪就是把这段「脏缓存」丢掉。

DFlash 维护两套 cache（[u2-l1](u2-l1-spec-decoding-control-flow.md) 已建立），它们的裁剪**时机和目的不同**：

- **target 缓存**：验证后多记了被否决候选的 K/V，裁剪用于**回滚**到只保留已提交 token 的干净前沿。
- **draft 缓存**：起草时多记了整块 speculative 的 K/V，裁剪用于**丢弃投机尾巴**，让下一轮草稿只基于已提交的上下文推理。

#### 4.3.2 核心流程

两套缓存在一轮迭代里的「裁剪时刻」不同——这正是初读源码最容易看漏的地方：

```text
进入循环 (start 为本轮锚点位置):
 ├─ [块起草] draft 处理 (上下文 + 本块) → draft 缓存增长
 │           past_key_values_draft.crop(start)      ← 用【旧】start 裁剪
 ├─ 验证 target(整块) → target 缓存增长 B
 ├─ 计算 a, 写回命中前缀 + 兜底
 ├─ start += a + 1                                   ← start 此时更新
 └─ past_key_values_target.crop(start)               ← 用【新】start 裁剪
```

注意：**draft 的 crop 用的是更新前的旧 `start`，target 的 crop 用的是更新后的新 `start`**。它们裁到不同的长度，因为服务的目的不同：

- **target 裁到新 `start`（= 已提交前沿）**：丢掉所有被否决候选位置的 K/V。新前沿处的兜底 token 此刻**还没有**自己的 K/V（它是在写回时才确定的，target 这一轮前向时它还是候选身份）——所以下一轮它会作为锚点被 target **重新前向**一次，补上自己的 K/V。
- **draft 裁到旧 `start`**：丢掉本轮整块 speculative 的 K/V，只保留到旧前沿。配合「传给草稿的 `position_ids` 从 `get_seq_length()` 开始」（见 [u2-l3](u2-l3-attention-block-diffusion.md) 与 [`L115`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L115)），草稿缓存只累积**已提交 token** 的 K/V，从不保留投机结果，从而每轮都「从干净的已提交上下文」出发重新去噪。

**不变量**：裁剪之后，两套 cache 的长度都精确反映「已提交且采用正确 token 算出的 K/V」的前沿，target cache 反映到新前沿、draft cache 反映到旧前沿（本轮锚点之前）。

#### 4.3.3 源码精读

**输出写回与 target 裁剪**（接 4.2.3 的同一片段，聚焦裁剪）：

```python
output_ids[:, start : start + acceptance_length + 1] = block_output_ids[:, : acceptance_length + 1]
output_ids[:, start + acceptance_length + 1] = posterior[:, acceptance_length]
start += acceptance_length + 1
past_key_values_target.crop(start)          # 用新 start 裁 target 缓存
acceptance_lengths.append(acceptance_length + 1)
```

- 三行写回把命中前缀 + 兜底写进 `output_ids`；
- `start += a + 1` 更新前沿；
- [`past_key_values_target.crop(start)`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L139) 紧跟其后，用**新** `start` 把 target 缓存截到「只保留已提交 token」。被否决候选位置（新 `start` 及以后）的脏 K/V 被丢弃。

**draft 裁剪**（在块起草段，[`L120`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L120)）：

```python
draft_logits = target.lm_head(model(
    target_hidden=target_hidden,
    noise_embedding=noise_embedding,
    position_ids=position_ids[:, past_key_values_draft.get_seq_length(): start + block_size],
    past_key_values=past_key_values_draft,
    use_cache=True,
    is_causal=False,
)[:, 1 - block_size :, :])
past_key_values_draft.crop(start)           # 用旧 start 裁 draft 缓存
block_output_ids[:, 1:] = sample(draft_logits)
```

- 注意 `crop(start)` 出现在草稿前向**之后**、`start` 更新**之前**，所以用的是旧 `start`。
- 传给草稿的 `position_ids` 起点是 `past_key_values_draft.get_seq_length()`（当前缓存长度），这意味着草稿**增量地**只为尚未缓存的新位置算 K/V；`crop(start)` 再把投机尾巴截掉，保证 draft 缓存只含已提交上下文。

**如果不裁剪会怎样？（源码推理）**

- 去掉 `past_key_values_target.crop(start)`：target 缓存会保留被否决候选的 K/V。下一轮 target 处理新块时，那些位置的真实 token 已被改写成 target 的兜底，但缓存里还是旧候选的 K/V——**注意力基于错误的 K/V 计算，输出会静默变错**（不一定报错，但语义错乱）；同时缓存长度与 `position_ids` 失配，可能触发形状错误。
- 去掉 `past_key_values_draft.crop(start)`：draft 缓存会跨轮累积投机 K/V，`get_seq_length()` 不断膨胀，`position_ids` 切片起点错位，草稿基于「含投机尾巴」的上下文去噪，候选质量下降，且最终会因索引越界或长度失配而报错。

**谁在消费 `acceptance_lengths`？** benchmark 正是把它聚合成「平均接受长度」和「接受长度直方图」来报告加速效果（[`benchmark.py:127-132`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L127-L132)）：

```python
mean_accept = np.mean([np.mean(r[block_size].acceptance_lengths) for r in responses])
acceptance_lengths = list(chain.from_iterable(r[block_size].acceptance_lengths for r in responses))
histogram = [acceptance_lengths.count(b) / len(acceptance_lengths) for b in range(block_size + 1)]
```

直方图统计的是「产出数 `a+1`」在 `0..block_size` 各桶里的占比（注意 `a+1 ≥ 1`，所以 0 桶恒为空）。这与本讲综合实践里要画的分布完全一致。

#### 4.3.4 代码实践

**目标**：通过「去掉 crop」的思想实验，加深对两套 cache 不变量的理解（源码阅读型；有 GPU 的同学可选实际验证）。

**步骤**：

1. 打开 [`model.py:107-140`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L107-L140)。
2. 假设把 [`L139`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L139) 的 `past_key_values_target.crop(start)` 注释掉。在笔记里预测：第二轮循环里，target 缓存的长度会比「已提交 token 数」多出多少？这些多出来的 K/V 对应的是哪些 token？
3. 再假设把 [`L120`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L120) 的 `past_key_values_draft.crop(start)` 注释掉。预测：第二轮里 `position_ids[:, get_seq_length(): start+block_size]` 这个切片会发生什么？

**需要观察/预测的现象**：

- 去掉 target crop：第二轮 target 缓存多出「上一轮被否决候选数」个脏 K/V；target 会基于错误历史算注意力，输出语义错乱或形状报错。
- 去掉 draft crop：`get_seq_length()` 偏大，切片起点超过 `start+block_size` 会得到空切片或负长度，报错。

**预期结果**：两套 cache 的 crop 各自不可省略，分别维持 target 回滚一致性与 draft 上下文干净性。实际运行结果「待本地验证」（需 GPU 加载模型）。

> 免改源码的交叉验证：保留 crop，用 `return_stats=True` 跑一次（见综合实践），确认正常输出且 `acceptance_lengths` 合理——反衬出 crop 是让这一切稳定运转的前提。

#### 4.3.5 小练习与答案

**Q1**：为什么 target 的 `crop` 用「新 start」、draft 的 `crop` 用「旧 start」？

**答案**：target 缓存要在「提交 + `start += a+1` 之后」裁到新前沿，才能精确丢掉被否决候选的脏 K/V，只保留已提交 token；而 draft 的 crop 发生在块起草段、`start` 尚未更新，用它裁掉本轮投机尾巴，保留旧前沿之前的已提交上下文。两者服务目的不同，所以裁剪时机与所用 `start` 不同。

**Q2**：兜底 token（`posterior[:, a]`）写回后，它的 K/V 在 target 缓存里吗？

**答案**：不在。target 这一轮前向处理的是块里的候选 token，兜底 token 是在采样判决后才确定写回的，并未参与前向；而 `crop(start)` 又恰好把它所在的新前沿位置截在缓存之外。所以下一轮它会作为新块的锚点被 target 重新前向一次，补上自己的 K/V。

**Q3**：`acceptance_lengths` 里存的是 `a` 还是 `a+1`？benchmark 直方图的桶为什么是 `range(block_size + 1)`？

**答案**：存的是 `a + 1`（即每轮**产出**的 token 数，见 [`L140`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L140)）。因为 `a+1 ∈ [1, B]`，直方图用 `range(block_size + 1)`（即 0..B）覆盖所有可能取值（0 桶恒为空，因为产出至少为 1）。

## 5. 综合实践

把本讲三个最小模块（`sample`、验证接受、cache 裁剪）串起来，完成规格要求的主实践：**收集每一步接受长度并打印分布，再对比 `temperature=0` 与 `temperature=0.8` 下接受长度的差异并解释**。

**任务**：用 `return_stats=True` 直接拿到 `acceptance_lengths`（无需改源码），分别在小温度与高温度下跑同一段 prompt，对比接受长度分布，并用本讲理论解释差异。

**操作步骤**：

1. 按 README 的 Transformers Quick Start 准备 `draft`、`target`、`tokenizer`、`input_ids`（需 GPU；参考 [u1-l4](u1-l4-first-generation.md)）。确保用带 `-DFlash` 后缀的草稿与对应 target。
2. 用内核函数跑两次（示例代码）：

   ```python
   from dflash.model import DFlashDraftModel, dflash_generate  # 示例代码

   common = dict(
       model=draft, target=target, input_ids=input_ids,
       max_new_tokens=128, stop_token_ids=[tokenizer.eos_token_id],
       return_stats=True,
   )
   res_greedy = dflash_generate(temperature=0.0, **common)
   res_hot    = dflash_generate(temperature=0.8, **common)
   ```

3. 打印接受长度分布（仿照 benchmark 的直方图算法）：

   ```python
   B = draft.block_size
   for name, res in [("T=0.0", res_greedy), ("T=0.8", res_hot)]:
       al = res.acceptance_lengths
       hist = [al.count(b) / len(al) for b in range(B + 1)]
       print(f"{name}: 轮数={len(al)} 平均接受长度={sum(al)/len(al):.2f}")
       print(f"   直方图(a+1 占比): {[f'{x*100:.1f}%' for x in hist]}")
   ```

4. 若无 GPU，退化为「源码阅读型实践」：跳过运行，直接基于本讲理论预测下表，并逐条写出理由。

**需要观察的现象与解释**：

| 观察 | 预期（T=0.0 vs T=0.8） | 依据（本讲理论） |
| --- | --- | --- |
| 平均接受长度 | T=0.0 **明显更大** | 见下 |
| 直方图重心 | T=0.0 更靠右（高产出的桶占比更高） | 同上 |
| 生成质量/输出分布 | 两者**都是 target 的正确采样**，质量不损失 | 4.2.2 洞察：输出恒等于 target 采样 |
| 轮数（= target 前向次数） | T=0.0 **更少**（更快） | 单轮产出更多 → 总轮数更少 |

**核心解释（务必写进你的实践报告）**：

- `temperature=0` 时，target 也走贪心 `argmax`（`posterior`）。草稿同样贪心。于是「接受」等价于「草稿的 argmax == target 的 argmax」——只要草稿训练得足够好、能逼近 target 的 argmax，命中率就高，平均接受长度大、加速明显。
- `temperature=0.8` 时，草稿**仍然贪心**（4.1.3：草稿调用 `sample` 不传温度），但 target 改为**按温度随机抽样**。此时即便草稿完美命中 target 的 argmax，target 也可能**抽样抽到别的词**——于是「草稿 argmax == target 采样」的频率随温度升高而下降，平均接受长度变短、加速削弱。
- 但无论温度高低，**输出都等于 target 的采样结果**（4.2.2 洞察），所以高温度换来的是「输出的多样性」而非「质量的下降」；代价仅是投机加速比变小。这是一个**速度 vs 多样性**的权衡，而非速度 vs 质量。

**预期结果**：`T=0.0` 的平均接受长度高于 `T=0.8`；两者的输出都应是合法、连贯的 target 采样结果。具体数值「待本地验证」（取决于草稿与 target 的吻合度、prompt、`block_size`）。

## 6. 本讲小结

- [`sample`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L48-L54) 用 `temperature < 1e-5` 分流：贪心走 `argmax`，否则 `logits / temperature → softmax → multinomial`；默认温度 0.0 即贪心。
- `sample` 在 `dflash_generate` 里被调三次：prefill 首 token 与 target 验证用**用户的 `temperature`**，**草稿候选永远贪心**（不传温度）——这个不对称是本讲关键。
- 验证段 `block_output_ids[:, 1:]`（B−1 个候选）与 `posterior[:, :-1]`（B−1 个 target 判决）天然对齐；`cumprod` 求最长公共前缀得接受长度 \(a \in [0, B-1]\)，兜底 token `posterior[:, a]` 永远来自 target、一定正确。
- **核心洞察**：因草稿贪心，「被接受即等于 target 采样」，且被拒处写回的也是 target 采样——故 **DFlash 输出与单独用 target 采样完全等价，不损失质量**，是纯粹的加速手段。
- 接受后必须裁剪两套 KV cache：target 缓存用**新 `start`** 裁，回滚被否决候选的脏 K/V；draft 缓存用**旧 `start`** 裁，丢弃投机尾巴、维持干净上下文。不裁会静默出错或崩溃。
- 调高 `temperature` 会让 target 偏离自己的 argmax，从而降低「草稿 argmax 命中 target 采样」的频率 → 平均接受长度下降、加速削弱；但输出分布不变，是速度与多样性的权衡。

## 7. 下一步学习建议

本讲把 [u2-l1](u2-l1-spec-decoding-control-flow.md) 留下的 `sample` 与「验证接受 + 裁剪」两个黑盒彻底打开，Transformers 参考实现（`model.py`）的主链路至此基本读完。接下来有两条路：

- **横向收尾第二单元**：[u2-l5（Transformers 集成与权重加载）](u2-l5-transformers-integration.md) 讲清 `DFlashDraftModel` 怎么继承 `Qwen3PreTrainedModel` 获得 `from_pretrained` 能力、`_no_split_modules` 的作用，以及 attention 实现（`flash_attention_2` / `sdpa`）的选择与回退——把「加载即用」的最后一环补上。
- **纵向对比 MLX 实现**：第三单元的 [u3-l1](u3-l1-mlx-draft-model.md)、[u3-l2](u3-l2-mlx-generation-cache.md) 讲 Apple MLX 版本的草稿模型与 `stream_generate`。建议重点对照本讲的「接受长度 + cache 裁剪」与 MLX 版的「`accepted` + `_trim_recent_cache` 回滚（`trim = bs - accepted - 1`）」——你会看到同一个验证接受思想在两套引擎里的不同实现，加深理解。

建议阅读顺序：u2-l5 → u3-l1 → u3-l2，把本讲建立的「验证接受循环」心智模型分别推广到「权重加载链路」与「另一套推理引擎」。
