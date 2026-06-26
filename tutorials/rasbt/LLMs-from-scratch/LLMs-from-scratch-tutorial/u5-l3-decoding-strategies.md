# 解码策略：温度与 top-k 采样

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚「贪心解码」与「概率采样解码」的区别，以及为什么前者会让输出千篇一律。
- 用**温度缩放（temperature scaling）**在「确定」与「随机」之间调节，并写出带温度的 softmax 公式。
- 用 **top-k 采样**把候选词限制在最有把握的 k 个，避免高温下生成无意义文本。
- 读懂本项目的 `generate` 函数：它把 top-k、温度、多项式采样、eos 提前停止统一进一个自回归循环。
- 学会用命令行脚本 `gpt_generate.py` 一键生成文本，并能调参观察多样性变化。

## 2. 前置知识

本讲承接 [u5-l2 训练循环](./u5-l2-training-loop.md)，默认你已经：

- 能跑通一个 `GPTModel`，并理解它对「下一个 token」输出的是一组长度为词表大小（50257）的 **logits**（未归一化分数）。
- 理解上一讲的 `generate_text_simple`：它每步取 `argmax`（分数最高的那个 token）拼回序列，属于**贪心解码**。

如果你对 softmax、logits 这些词还陌生，可以先翻 [u5-l1 生成损失与模型评估](./u5-l1-generation-loss-eval.md) 中关于交叉熵与概率的部分。本讲用到的几个新术语：

| 术语 | 含义 |
| --- | --- |
| **logits** | 模型为词表每个 token 输出的原始分数，还没归一化 |
| **softmax** | 把 logits 变成一组和为 1 的概率 |
| **贪心解码 / argmax** | 永远选概率最高的那一个 token |
| **多项式采样 / multinomial** | 按概率分布「掷骰子」抽一个 token |
| **温度（temperature, T）** | 在 softmax 前把 logits 除以的一个正数，调节分布的「尖锐程度」 |
| **top-k** | 只在概率最高的 k 个 token 里采样，其余置零 |
| **eos (end-of-sequence)** | 表示「序列结束」的特殊 token，遇到就停止生成 |

## 3. 本讲源码地图

本讲主要围绕第 5 章的解码部分，涉及两个文件：

| 文件 | 作用 |
| --- | --- |
| [ch05/01_main-chapter-code/gpt_generate.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_generate.py) | 自包含脚本：包含成品版 `generate` 函数、文本↔token 转换工具、加载 OpenAI GPT-2 权重，以及命令行入口 `main()`。本讲源码精读以它为准（行号稳定）。 |
| [ch05/01_main-chapter-code/ch05.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/ch05.ipynb) | 第 5 章正文 notebook，5.3 节用「极小词表」逐步演示温度与 top-k 的直觉，并最终推导出 `generate` 函数。本讲用来对照直觉推导与真实输出。 |

> 阅读提示：notebook 的 5.3 节先用只有 9 个词的玩具词表演示原理（便于画图、数频次），最终才把它合并成 `gpt_generate.py` 里那个面向 50257 词表的成品函数。两者逻辑一致，本讲以 `.py` 的行号做精读锚点。

## 4. 核心概念与源码讲解

### 4.1 解码策略的动机：从贪心到概率采样

#### 4.1.1 概念说明

上一讲的 `generate_text_simple` 每一步都执行 `torch.argmax`，永远挑分数最高的 token。这叫**贪心解码**。它有两个问题：

1. **完全确定**：同一个提示跑多少遍，输出一字不差。模型再「有创意」也体现不出来。
2. **容易陷入重复**：贪心只看眼前最优，一旦走进一个局部最优的循环，就会反复吐同一个短语。

解决办法是把「挑最大」换成「**按概率抽一个**」：softmax 把 logits 变成概率，再用 `torch.multinomial` 依概率掷骰子。这样分数高的 token 被抽中的机会大，但分数稍低的也有机会，输出就有了多样性。

#### 4.1.2 核心流程

```
logits ──softmax──▶ 概率分布 probas ──multinomial(抽 1 个)──▶ 下一个 token
                                          ▲
                            （每次结果可能不同，因为带随机性）
```

notebook 5.3.1 用一个 9 词玩具词表演示得很直观。假设输入 "every effort moves you"，模型给下一个 token 的 logits 是：

```
next_token_logits = [4.51, 0.89, -1.90, 6.75, 1.63, -1.62, -1.89, 6.28, 1.79]
# 对应词: closer, every, effort, forward, inches, moves, pizza, toward, you
```

softmax 后 `argmax` 永远选 "forward"；而 `multinomial` 重复 1000 次，频次大致正比于概率（这是 notebook 真实记录的输出）：

```
73 x closer       582 x forward       343 x toward        （其余为 0）
```

可以看到 "forward" 仍是最多的，但 "toward"（343 次）和 "closer"（73 次）也有机会，这正是多样性来源。

#### 4.1.3 源码精读

贪心解码来自上一讲的 `generate_text_simple`，关键就是 `argmax` 这一步（本项目把它收在 ch05 的汇总器里）：

[previous_chapters.py:215-238](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/previous_chapters.py#L215-L238) —— 这是 ch04 的贪心生成函数，第 233 行 `torch.argmax(logits, dim=-1, keepdim=True)` 永远取最高分 token，没有任何随机性。

成品 `generate` 函数里，纯贪心作为「温度=0 时的退化分支」被保留下来：

[gpt_generate.py:217-219](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_generate.py#L217-L219) —— 当 `temperature == 0.0` 时走 `torch.argmax`，与 `generate_text_simple` 行为完全一致；这正是「贪心解码」在新函数里的归宿。

#### 4.1.4 代码实践

**目标**：亲手感受「argmax 永远一致」与「multinomial 每次不同」。

**操作步骤**（示例代码，可直接在 Python 里跑）：

```python
import torch

# 玩具 logits：对应 [closer, every, effort, forward, inches, moves, pizza, toward, you]
next_token_logits = torch.tensor([4.51, 0.89, -1.90, 6.75, 1.63, -1.62, -1.89, 6.28, 1.79])
probas = torch.softmax(next_token_logits, dim=0)

# 1) 贪心：永远返回下标 3 (forward)
print("argmax:", torch.argmax(probas).item())

# 2) 采样：抽 1000 次，数频次
torch.manual_seed(123)
sample = [torch.multinomial(probas, num_samples=1).item() for _ in range(1000)]
print(torch.bincount(torch.tensor(sample), minlength=len(probas)))
```

**需要观察的现象**：`argmax` 每次都是同一个下标；`multinomial` 的频次向量里 forward 最多，但 toward、closer 也有非零计数。

**预期结果**：频次大致为 `tensor([73, 0, 0, 582, 2, 0, 0, 343, 0])`（与 notebook 5.3.1 的真实输出一致；不设种子或换设备会有微小波动）。

### 4.2 温度缩放（temperature scaling）

#### 4.2.1 概念说明

「温度缩放」名字很唬人，本质就一句话：**在 softmax 之前，把 logits 除以一个正数 T**。

带温度的 softmax 公式为：

\[
p_i = \frac{\exp(z_i / T)}{\sum_{j} \exp(z_j / T)}
\]

- **T = 1**：标准 softmax，分布不变。
- **T < 1（如 0.1）**：logits 被放大，差距拉得更开，softmax 后分布**更尖锐**，几乎总选最高分词 —— 趋近贪心。
- **T > 1（如 5）**：logits 被缩小，差距被压平，softmax 后分布**更均匀**，低分词也有机会 —— 更随机、更多样，但也更容易胡言乱语。

直观地理解 T 的作用：T 控制 softmax 的「软硬」。T 越小越「自信」，T 越大越「摸鱼」。

#### 4.2.2 核心流程

```
logits ──÷ T──▶ 缩放后 logits ──softmax──▶ 概率 ──multinomial──▶ token
                                                    
T↓ 分布尖锐(自信)            T↑ 分布平坦(随机)
```

notebook 用同一个 `next_token_logits` 对比 T=1 / 0.1 / 5，并各抽 1000 次（真实记录）：

| 温度 T | forward 次数 | toward 次数 | closer 次数 | pizza/其他 |
| --- | --- | --- | --- | --- |
| 1（原始） | 582 | 343 | 73 | 极少 |
| 0.1（尖锐） | 985 | 15 | 0 | 0 |
| 5（平坦） | 239 | 227 | 165 | pizza=32 等，几乎平均 |

可以看到 T=0.1 时基本只选 forward（接近贪心）；T=5 时连 "pizza" 这种语义不通的词都被选中（约 3.2%），于是 notebook 指出：「输入 every effort moves you，用高温采样可能得到 every effort moves you pizza」。

#### 4.2.3 源码精读

notebook 5.3.1 把温度原理抽成一个极简函数（注意 `scaled_logits = logits / temperature`）：

[ch05.ipynb（5.3.1 节，softmax_with_temperature）](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/ch05.ipynb) —— 这段教学代码 `def softmax_with_temperature(logits, temperature): scaled_logits = logits / temperature; return torch.softmax(scaled_logits, dim=0)` 正是上面公式的直接翻译。

成品 `generate` 里，温度缩放发生在 top-k 过滤**之后**：

[gpt_generate.py:203-205](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_generate.py#L203-L205) —— `if temperature > 0.0: logits = logits / temperature`，先除温度再 softmax。注意只在 `temperature > 0` 时才进入采样分支，否则走 4.1 讲的 argmax 退化路径。

紧跟着有一行**数值稳定技巧**：

[gpt_generate.py:207-209](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_generate.py#L207-L209) —— 注释明确写着 "not in book"（书里没有，是仓库补充的）：softmax 前先减去每行最大值 `logits = logits - logits.max(dim=-1, keepdim=True).values`。原理是 softmax 对常数平移不变（\(\exp(z_i-c)/\sum\exp(z_j-c)\) 与原式相等），但减去最大值能让最大的指数项变成 \(\exp(0)=1\)，避免大 logits 经 `exp` 溢出，从而在 Apple MPS 设备上得到与 CPU/CUDA 一致的结果。

#### 4.2.4 代码实践

**目标**：用同一个 logits 直观对比不同温度对分布形状的影响。

**操作步骤**（示例代码）：

```python
import torch

next_token_logits = torch.tensor([4.51, 0.89, -1.90, 6.75, 1.63, -1.62, -1.89, 6.28, 1.79])

def softmax_with_temperature(logits, temperature):
    return torch.softmax(logits / temperature, dim=0)

for T in [0.1, 1.0, 5.0]:
    p = softmax_with_temperature(next_token_logits, T)
    print(f"T={T}: 最大概率={p.max():.4f}, 熵(越大概率越平均)={-torch.sum(p*torch.log(p)):.4f}")
```

**需要观察的现象**：T=0.1 时最大概率接近 1（分布尖锐）；T=5 时最大概率明显下降、熵变大（分布平坦）。

**预期结果**：T 越小，最大概率越高、熵越低；T 越大则相反。这与上表「T=0.1 几乎只选 forward、T=5 接近平均」的结论一致。

#### 4.2.5 小练习与答案

**练习 1**：当 T 趋近于 0+ 时，带温度的 softmax 会退化成什么操作？

> **答案**：退化成 `argmax`。因为 logits/T 中最大的那个会趋于 +∞，softmax 后它的概率趋于 1，其余趋于 0。

**练习 2**：为什么项目里在 softmax 前先减去 `logits.max()` 不改变结果？

> **答案**：softmax 具有平移不变性，\(\text{softmax}(z-c)=\text{softmax}(z)\)（c 为常数）。减去最大值只是数值上更稳定，概率分布不变。

### 4.3 top-k 采样

#### 4.3.1 概念说明

温度调高了确实能增加多样性，但也会让「pizza」这种荒谬的低分词被选中。**top-k 采样**是对症的补丁：**只允许在概率最高的 k 个词里采样，把剩下 50257−k 个词的概率直接清零**。

这样即便用高温，模型也只能在「最有把握的 k 个候选」里发挥，既保留了随机性，又不会冒出完全离谱的词。它和温度是互补的：top-k 限制候选范围，温度决定在范围内选得多「软」。

#### 4.3.2 核心流程

```
logits ──torch.topk(logits, k)──▶ 找到第 k 大的分数 min_val
       ──把所有 < min_val 的位置置 -inf──▶ new_logits
       ──softmax──▶ 概率（被置 -inf 的位置自然变 0）
       ──multinomial──▶ token
```

关键技巧是 **−inf + softmax**：把不要的候选 logits 设成 \(-\infty\)，softmax 后 \(\exp(-\infty)=0\)，这些位置概率自然归零，剩下的自动重新归一化为和为 1，无需手动重算。

仍用玩具 logits、k=3：top-3 是 forward(6.75)、toward(6.28)、closer(4.51)；其余 6 个词被置 −inf，softmax 后只在它们三个里分配概率（notebook 5.3.2 真实输出）：

```
tensor([0.0615, 0, 0, 0.5775, 0, 0, 0, 0.3610, 0])
#        closer            forward            toward
```

注意 "pizza" 现在概率为 0，再高的温度也选不到它了。

#### 4.3.3 源码精读

notebook 5.3.2 先用玩具 logits 演示三步法：

[ch05.ipynb（5.3.2 节，top-k 三步法）](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/ch05.ipynb) —— `torch.topk(next_token_logits, top_k)` 取前 k 个分数与位置；`torch.where(next_token_logits < top_logits[-1], -inf, next_token_logits)` 把低于第 k 名的全置 −inf；再 softmax 即得只在 top-k 内归一化的概率。

成品 `generate` 把这套逻辑写成支持 batch 维的两行：

[gpt_generate.py:196-201](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_generate.py#L196-L201) —— `top_logits, _ = torch.topk(logits, top_k)` 拿到前 k 个分数；`min_val = top_logits[:, -1]` 是这 k 个里最小的（即「第 k 名的分数」当门槛）；`torch.where(logits < min_val, -inf, logits)` 把 batch 中每个位置低于门槛的 logits 全置 −inf。注意这里的 `.to(logits.device)`：−inf 张量要和 logits 在同一设备上，否则多 GPU/MPS 会报设备不一致错误。

> 实现细节：`torch.topk` 返回的是「按降序排列」的前 k 个，所以 `top_logits[:, -1]` 恰好是第 k 名的分数（前 k 名里最小的）。这正是 top-k 的门槛值。

#### 4.3.4 代码实践

**目标**：复现「置 −inf 后概率自动归零、并重新归一化」的效果。

**操作步骤**（示例代码）：

```python
import torch

next_token_logits = torch.tensor([4.51, 0.89, -1.90, 6.75, 1.63, -1.62, -1.89, 6.28, 1.79])
top_k = 3

top_logits, _ = torch.topk(next_token_logits, top_k)
min_val = top_logits[-1]                              # 门槛 = 4.51 (closer)
new_logits = torch.where(next_token_logits < min_val,
                         torch.tensor(float("-inf")),
                         next_token_logits)
probas = torch.softmax(new_logits, dim=0)

print("门槛值:", min_val.item())
print("top-k 概率:", probas)            # 只有 3 个非零，且和为 1
print("概率之和:", probas.sum().item())  # 应为 1.0
```

**需要观察的现象**：只有 closer/forward/toward 三个位置非零，其余为 0；三个非零概率相加正好是 1。

**预期结果**：`probas = [0.0615, 0, 0, 0.5775, 0, 0, 0, 0.3610, 0]`，和为 1.0（与 notebook 一致）。

#### 4.3.5 小练习与答案

**练习 1**：如果设 `top_k=1`，top-k 采样等价于哪种解码？

> **答案**：等价于贪心（argmax）。因为只剩 1 个候选，采样必然选它。

**练习 2**：为什么用 −inf 而不是直接用 0 来屏蔽不想要的 logits？

> **答案**：直接把 logits 置 0，softmax 后该位置概率是 \(\exp(0)/\sum\neq 0\)（正数），并不能清零；而 \(\exp(-\infty)=0\) 才能真正归零，且让剩余概率自动重新归一化。

### 4.4 统一的 generate 函数

#### 4.4.1 概念说明

notebook 5.3.3 把前面两节拼起来，升级 `generate_text_simple` 得到新的 `generate` 函数。它在一个自回归循环里依次做：**取末位 logits →（可选）top-k 过滤 →（可选）温度缩放 → softmax → multinomial 采样 → 拼接**，并保留 `temperature=0` 时的贪心退化分支。

#### 4.4.2 核心流程

```
for 每个要生成的新 token (共 max_new_tokens 次):
    1. 裁剪上下文 idx[:, -context_size:]        # 不超过位置嵌入上限
    2. logits = model(idx)[:, -1, :]            # 只看最后一个位置的预测
    3. if top_k:     把低于第 k 名的 logits 置 -inf
    4. if T > 0:     logits /= T; (减最大值); softmax; multinomial 采样
       else:         argmax                      # 退化为贪心
    5. if 采样到 eos_id: break                   # 提前停止
    6. idx = cat(idx, idx_next)                  # 拼回序列末尾
```

注意三个参数的协作：`top_k` 决定候选范围，`temperature` 决定采样软硬，`temperature=0` 时无论 `top_k` 设多少都退化为贪心（因为 argmax 一定落在 top-k 内）。

#### 4.4.3 源码精读

完整函数签名与循环骨架：

[gpt_generate.py:187-194](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_generate.py#L187-L194) —— 函数签名 `generate(model, idx, max_new_tokens, context_size, temperature=0.0, top_k=None, eos_id=None)`，默认 `temperature=0.0`、`top_k=None`，即默认就是贪心。循环里 `idx_cond = idx[:, -context_size:]` 裁剪上下文防越界，`logits = logits[:, -1, :]` 取最后一步预测——这与 `generate_text_simple` 完全一致。

top-k 过滤（详见 4.3）与温度采样（详见 4.2）依次作用：

[gpt_generate.py:196-215](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_generate.py#L196-L215) —— 先 top-k（196-201），再温度缩放、减最大值、softmax、`torch.multinomial(probs, num_samples=1)` 采样（203-215）。`multinomial` 返回 `(batch_size, 1)` 的采样结果。

贪心退化分支：

[gpt_generate.py:217-219](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_generate.py#L217-L219) —— `else: idx_next = torch.argmax(logits, dim=-1, keepdim=True)`。

拼接新 token（与上一讲相同）：

[gpt_generate.py:224-225](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_generate.py#L224-L225) —— `idx = torch.cat((idx, idx_next), dim=1)`，把新 token 接到序列末尾，进入下一轮自回归。

#### 4.4.4 代码实践

**目标**：在真实模型上体会「温度+top-k」组合对输出的影响（用 notebook 训练好的 124M 模型）。

**操作步骤**：在 `ch05.ipynb` 训练完模型后（或运行完 5.2 节得到 `model`），新增一个单元格（示例代码）：

```python
import torch
from previous_chapters import generate   # 或从 gpt_generate 导入

# 复用 notebook 里已定义的 text_to_token_ids / token_ids_to_text / tokenizer
torch.manual_seed(123)
token_ids = generate(
    model=model,
    idx=text_to_token_ids("Every effort moves you", tokenizer).to(inference_device),
    max_new_tokens=15,
    context_size=GPT_CONFIG_124M["context_length"],
    top_k=25,
    temperature=1.4,
)
print(token_ids_to_text(token_ids, tokenizer))
```

**需要观察的现象**：相比 4.1.4 的贪心输出，带 `temperature=1.4, top_k=25` 的输出会出现不同的续写；改 `torch.manual_seed` 或重跑会得到不同句子。

**预期结果**：notebook 5.3.3 在该参数下真实输出为：`Every effort moves you stand," she down." For Mrs. Gisburn! The women had`（与贪心输出的 `..."Yes--quite insensible to the irony...` 明显不同）。具体文本随种子与设备波动，属正常现象。

#### 4.4.5 小练习与答案

**练习 1**：调用 `generate(..., temperature=0.0, top_k=50)` 和 `generate(..., temperature=0.0, top_k=None)`，结果会一样吗？为什么？

> **答案**：一样。因为 `temperature=0` 走 argmax 分支，而 argmax 选中的最高分词一定落在 top-50 之内，所以 top_k 过滤不影响最终选择。

**练习 2**：为什么 `generate` 里「先 top-k、再温度」的顺序是合理的？反过来会怎样？

> **答案**：先 top-k 选出候选范围、再在范围内用温度调节软硬，符合直觉。顺序上其实温度（除以 T）不影响 top-k 的排名（正数 T 不改变 logits 大小顺序），所以两者互换在数学上结果相同；但工程上先 top-k 可以让后续 softmax 只对少量非 −inf 项运算，更清晰。

### 4.5 eos 提前停止与脚本入口

#### 4.5.1 概念说明

自回归生成默认跑满 `max_new_tokens` 步才停。但很多任务（如指令问答）希望模型说完话就停，这时可以传入 `eos_id`（结束符的 token ID）。一旦采样到 eos，循环立即 `break`，避免后面继续「画蛇添足」。

注意本项目用的是 GPT-2 词表，并没有在 `generate` 里默认启用 eos；`eos_id=None` 时该机制不生效，行为退化为「跑满步数」。

#### 4.5.2 核心流程

```
采样得到 idx_next
if eos_id is not None 且 idx_next == eos_id:
    break          # 提前结束，不再拼接、不再生成
否则:
    idx = cat(idx, idx_next)   # 正常拼接继续
```

#### 4.5.3 源码精读

提前停止判断就一行：

[gpt_generate.py:221-222](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_generate.py#L221-L222) —— `if idx_next == eos_id: break`。注意它放在拼接之前，所以 eos 本身不会被拼进输出序列；且只有调用方显式传了 `eos_id` 才会触发。

脚本入口 `main()` 演示了一次完整调用（下载并加载 OpenAI 权重 → 生成）：

[gpt_generate.py:242-249](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_generate.py#L242-L249) —— 这里用 `top_k=50, temperature=1.0` 调用 `generate`，是本项目推荐的「既有一定多样性、又不至于离谱」的常用组合。`max_new_tokens=25`，未传 `eos_id`（GPT-2 续写场景不需要提前停）。

命令行封装：

[gpt_generate.py:254-299](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_generate.py#L254-L299) —— `__main__` 用 `argparse` 暴露 `--prompt`（默认 `"Every effort moves you"`）和 `--device`（默认 `cpu`，可选 `cuda`/`mps`/`auto`），默认加载 `gpt2-small (124M)` 权重后生成。

#### 4.5.4 代码实践

**目标**：用现成脚本一键体验解码，并尝试改提示。

**操作步骤**（需要联网下载约 500MB 的 GPT-2 权重）：

```bash
cd ch05/01_main-chapter-code
python gpt_generate.py --prompt "Every effort moves you"
# 换设备（若有 GPU）：
# python gpt_generate.py --prompt "My favorite book is" --device cuda
```

**需要观察的现象**：脚本先打印 PyTorch 版本与设备，下载权重，再用 `top_k=50, temperature=1.0` 生成 25 个新 token。

**预期结果**：由于 `temperature=1.0` 带随机性，每次输出可能不同；模型应能生成语法通顺、与提示相关的英文续写。具体文本待本地验证（取决于随机种子与设备，脚本内 `torch.manual_seed(123)` 已固定，但不同设备/后端仍可能有微小差异）。

> 小提示：脚本默认 `top_k=50, temperature=1.0` 写在 `main()` 里（见上面 242-249 行）。若想对比不同解码策略，可以直接改这两行的参数重跑，或仿照 4.4.4 的写法自行调用 `generate`。

#### 4.5.5 小练习与答案

**练习 1**：`eos_id=None` 时，循环会在什么情况下停止？

> **答案**：跑满 `max_new_tokens` 步。因为 `idx_next == None` 永远为 False，`break` 不会触发。

**练习 2**：把 `max_new_tokens` 设得很大、又不设 `eos_id`，会有什么风险？

> **答案**：模型会一直生成到步数上限，可能产出冗长、重复或跑题的文本；而且序列一旦超过 `context_size`，每步都要靠 `idx[:, -context_size:]` 裁剪，计算量随步数线性增长。设置合理的 `eos_id` 可以让它在「说完」后自然停止。

## 5. 综合实践

把本讲四个模块串起来：用同一个提示，扫描不同 `(temperature, top_k)` 组合，观察「多样性 vs 连贯性」的权衡。

**背景**：你需要一个能生成连贯文本的模型。两条路任选其一：

- **路线 A（快）**：直接用 `gpt_generate.py`（下载 OpenAI GPT-2 权重），但它的解码参数写死在 `main()` 里。建议改为：把 `generate`、`text_to_token_ids`、`token_ids_to_text` 从 `gpt_generate` 导入，自己写一段扫描循环。
- **路线 B（本地）**：用 [u5-l2](./u5-l2-training-loop.md) 训练并保存的 `model.pth`（注意它在《The Verdict》上过拟合，输出会偏向原文，但仍能体现温度/top-k 的随机性控制）。

**任务步骤**（示例代码，基于路线 A 的导入）：

```python
import torch, tiktoken
from gpt_generate import generate, text_to_token_ids, token_ids_to_text, \
    download_and_load_gpt2, load_weights_into_gpt
from previous_chapters import GPTModel

# 1) 准备一个加载好权重的模型 gpt（参照 gpt_generate.py 的 main 流程，略）
tokenizer = tiktoken.get_encoding("gpt2")

# 2) 扫描不同解码参数
prompt = "Every effort moves you"
settings = [
    dict(temperature=0.0, top_k=None),   # 纯贪心，确定性
    dict(temperature=0.7, top_k=50),     # 温和多样性
    dict(temperature=1.0, top_k=50),     # 脚本默认组合
    dict(temperature=1.5, top_k=1),      # 高温但 top_k=1：会发生什么？
]

for s in settings:
    torch.manual_seed(123)
    ids = generate(model=gpt,
                   idx=text_to_token_ids(prompt, tokenizer),
                   max_new_tokens=25,
                   context_size=gpt.pos_emb.weight.shape[0],
                   **s)
    print(f"{s} -> {token_ids_to_text(ids, tokenizer)!r}")
```

**你要回答的问题**：

1. `temperature=0.0` 的输出是否每次完全一致？为什么？
2. 从 `temperature=0.0 → 0.7 → 1.0`，连贯性和多样性如何变化？
3. `temperature=1.5, top_k=1` 这一组的输出，和 `temperature=0.0, top_k=None` 是否一样？用本讲 4.4.5 练习 1 的结论解释。
4. 把同一组参数（如 `temperature=0.7, top_k=50`）不设种子连跑 3 次，输出是否相同？

**预期结论**（待本地验证具体文本）：贪心输出确定且每次一致；随温度升高，多样性增加但可能出现稍不通顺的搭配；`top_k=1` 配合任意温度都等价于贪心；带采样的组合不设种子时每次不同。

## 6. 本讲小结

- 贪心解码（`argmax`）完全确定、易重复；改成 `multinomial` 按概率采样可引入多样性。
- **温度缩放**：softmax 前把 logits 除以 T。T<1 分布更尖锐（接近贪心），T>1 更平坦（更随机但易出错）。
- **top-k 采样**：用 `torch.topk` 找门槛、`torch.where(...<min, -inf, ...)` 把非 top-k 候选置 −inf，softmax 后自动归零并重新归一化。
- 成品 `generate` 函数把「上下文裁剪 → top-k → 温度 → multinomial/argmax → 拼接」统一进一个自回归循环，`temperature=0` 时退化为贪心。
- softmax 前减去行最大值是仓库补充的数值稳定技巧（注释标注 "not in book"），用于 MPS 等设备结果对齐。
- `eos_id` 提供提前停止能力；`gpt_generate.py` 用 `argparse` 暴露 `--prompt`/`--device`，默认 `top_k=50, temperature=1.0`。

## 7. 下一步学习建议

- 下一讲 [u5-l4 权重保存/加载与加载 OpenAI GPT-2 权重](./u5-l4-weight-loading.md) 会讲 `gpt_generate.py` 里用到的 `download_and_load_gpt2` 和 `load_weights_into_gpt` 是怎么把 OpenAI 的 TensorFlow checkpoint 逐层搬进我们的 `GPTModel` 的——也就是本讲能生成连贯文本的前提。
- 想进一步了解推理加速，可以跳到 [u9-l1 KV Cache 加速推理](./u9-l1-kv-cache.md)，看 `generate` 的自回归循环如何用缓存避免重复计算。
- 对更现代的采样策略（如 top-p / nucleus sampling、min-p）感兴趣，可对照本讲的 top-k 思路自行扩展：把「按排名截断 k 个」换成「按累计概率截断」即可。
