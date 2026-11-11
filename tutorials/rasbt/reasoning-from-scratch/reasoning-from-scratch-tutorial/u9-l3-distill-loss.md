# 蒸馏损失：只对答案算交叉熵

> 所属单元：单元九　知识蒸馏（第 8 章）　|　前置：u9-l2（蒸馏数据管线）、u6-l1（Log-prob 与序列似然打分）

## 1. 本讲目标

上一讲（u9-l2）结束时，蒸馏数据已经被加工成每条只含两个字段的训练样本：

```python
{"token_ids": prompt_ids + answer_ids + [eos], "prompt_len": len(prompt_ids)}
```

本讲回答的问题是：**拿到这样一条样本后，训练信号到底怎么算？** 学完本讲你应当能够：

1. 独立实现「错位一位」的下一词预测目标构造：输入 `token_ids[:-1]`、目标 `token_ids[1:]`，并解释为什么这样切是对的。
2. 解释 `answer_start = max(prompt_len - 1, 0)` 这个切片锚点的数学含义：为什么减一、为什么取 max、为什么恰好让「每个答案 token（含 eos）各被监督一次、prompt 区一个都不监督」。
3. 说清楚同一个 `compute_example_loss` 在训练与验证中的两种身份：训练时返回带梯度的张量供 `backward()`，验证时被 `evaluate_examples` 包成无梯度的平均损失，并理解两者在曲线上的不同读法。

## 2. 前置知识

本讲用到的概念在前面讲义都出现过，这里快速复习：

- **下一词预测与 logits**（u2-l1、u3-l1）：模型每个输出位置给出全词表打分 `logits`，softmax 后即「下一个 token」的概率分布。因果掩码保证位置 `i` 的分布只依赖 `x_0..x_i`。
- **teacher forcing 与错位配对**（u6-l1）：想给一段**已经写好**的文本打分，不必逐 token 生成——把整段文本一次喂进模型，位置 `i` 的 logits 天然就是「对 `x_{i+1}` 的预测」。用 `gather`（或等价的花式索引）把每个真实 next token 的分数抽出来即可。第 6 章的 `sequence_logprob` 与本讲的损失函数共享这套机制。
- **交叉熵（cross-entropy）**：分类问题的标准损失。对「预测下一个 token」这个任务，它就是**平均负对数概率**：模型给真实 token 的概率越高，损失越低。`torch.nn.functional.cross_entropy` 接收原始 logits（内部自动做数值稳定的 `log_softmax`），默认对所有位置取平均。
- **计算图与梯度**（u7-l3）：只有「不带 `no_grad` 且沿途不 `detach`」的计算才会留下梯度轨迹 `grad_fn`，才能 `loss.backward()` 反传。打分函数（u6-l1）用 `@torch.inference_mode()` 并返回 float；本讲的损失函数**有意不这么做**——它就是梯度的源头。
- **train/eval 模式**（u7-l2、u7-l3）：`model.eval()` / `model.train()` 切换模块的训练期行为（如 dropout），不控制梯度；控制梯度的是 `no_grad` / `inference_mode` 装饰器。

一个符号约定贯穿本讲：记一条样本的完整序列为 `token_ids = [x_0, x_1, ..., x_{T-1}]`，总长 `T`；前 `prompt_len` 个是 prompt，后 `A = T - prompt_len` 个是答案 token（教师的 `<think>...</think>` 回答 + 末尾一个 eos）。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| [reasoning_from_scratch/ch08.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py) | **主战场**。L170-L190 `compute_example_loss`（错位构造 + answer 切片 + 交叉熵）；L193-L208 `evaluate_examples`（验证集平均损失）；另看 L119-L121 数据结构来源、L252-L273 训练循环中的两个消费点 |
| [reasoning_from_scratch/ch06.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py) | L111-L117 `sequence_logprob`：GRPO 里「同一个错位 + 同一个 `prompt_len - 1` 起点」的求和版对照 |
| [reasoning_from_scratch/ch05.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py) | L99-L121 `avg_logprob_answer`：u6-l1 的打分函数，其 `start = len(prompt_ids) - 1` 与本讲 `answer_start` 是同一条约定 |
| [ch08/01_main-chapter-code/ch08_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/01_main-chapter-code/ch08_main.ipynb) | 第 8.6 节：书中先算平均负 logprob（1.68）、再算交叉熵（1.68），验证两者数值等价的原始演示 |
| [ch08/04_train_with_distillation/distill_batched.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill_batched.py) | L212-L214：批处理版用「忽略索引掩码」实现与切片等价的答案区损失，是 u9-l4 的伏笔 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**4.1 错位构造**（input/target 怎么切、交叉熵在算什么）、**4.2 answer_start 切片**（为什么是 `prompt_len - 1`、为什么 prompt 不进损失）、**4.3 evaluate_examples**（验证集评估与训练损失的区别）。

### 4.1 错位构造：下一词预测的 input/target 与交叉熵

#### 4.1.1 概念说明

语言模型的训练目标是下一词预测：给定前缀，最大化真实下一个 token 的概率。但训练时我们手里**已经有完整文本**（prompt + 教师答案 + eos），不需要像生成时那样逐步解码——这就是 u6-l1 讲过的 **teacher forcing**：整段文本一次前向，`T` 个位置一次性得到 `T` 个「预测下一个 token」的分布。

于是「一条序列」可以改写成 `T-1` 个共享同一次前向的分类样本：

- 位置 `i` 的输入上下文是 `x_0..x_i`，模型对 `x_{i+1}` 给出分布；
- 输入侧因此取 `token_ids[:-1]`（丢掉最后一个——它没有「下一个」要预测）；
- 目标侧取 `token_ids[1:]`（丢掉第一个——它是被条件化的起点，不作为任何位置的预测目标）。

两侧**错位正好一位**，逐位置对齐后：`input[i] = x_i` 预测 `target[i] = x_{i+1}`。交叉熵就是这个对齐表上的平均负对数概率。它与 u6-l1 打分函数的唯一区别是符号与聚合方向：打分要「平均 logprob 越大越好」，训练要「损失越小越好」，而 `cross_entropy` 的内部实现恰好就是 `-(平均 logprob)`——notebook 8.6 节用真实模型验证过：同一个样本，手算平均负 logprob 得 1.68，`cross_entropy` 也得 1.68。

#### 4.1.2 核心流程

`compute_example_loss(model, example, device)` 对一条样本的完整流程：

1. 从 `example` 取出 `token_ids` 与 `prompt_len`（u9-l2 的 `build_examples` 产出，整个 pipeline 只存这两个字段）。
2. 构造输入：`token_ids[:-1]` 转成张量并 `unsqueeze(0)` 补上 batch 维，形状 `(1, T-1)`。
3. 构造目标：`token_ids[1:]` 转成一维张量，形状 `(T-1,)`（不需要 batch 维，`cross_entropy` 约定如此）。
4. 一次前向：`model(input_ids)` 得 `(1, T-1, V)` 的 logits，`squeeze(0)` 压成 `(T-1, V)`。注意这里**不用 KV cache**——文本已知，整段前向即可。
5. 切出答案区（详见 4.2）后做交叉熵：内部 `log_softmax` + 抽取目标位负 logprob + 对答案区位置取平均。

数学上，本讲实现的损失是：

\[
\mathcal{L}_{\text{CE}}
= -\frac{1}{N}\sum_{i=s}^{T-2}\log p_\theta\!\left(x_{i+1}\mid x_{\le i}\right),
\qquad
s = \text{prompt\_len}-1,\quad N = T-\text{prompt\_len}
\]

即「对 `s` 及之后的所有预测位置，取真实 next token logprob 的平均再取负」。`N` 恰好等于答案 token 数（教师答案 + eos）。

#### 4.1.3 源码精读

数据结构来自 u9-l2（回顾一眼）——`build_examples` 把 prompt、答案、eos 拼成一条序列，并记下 prompt 长度：[reasoning_from_scratch/ch08.py:119-121](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L119-L121) 把三段拼成 `token_ids`（末尾追加 `tokenizer.eos_token_id`）；[reasoning_from_scratch/ch08.py:127-130](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L127-L130) 存成 `{"token_ids": ..., "prompt_len": ...}` 两键字典。

本讲主角的开头是错位构造：[reasoning_from_scratch/ch08.py:174-179](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L174-L179)

```python
input_ids = torch.tensor(
    token_ids[:-1], dtype=torch.long, device=device
).unsqueeze(0)                      # (1, T-1)：补 batch 维
target_ids = torch.tensor(
    token_ids[1:], dtype=torch.long, device=device
)                                   # (T-1,)：错位一位的目标
```

输入侧 `[:-1]` 丢掉末位、目标侧 `[1:]` 丢掉首位，两条切法合起来实现「`x_i` 预测 `x_{i+1}`」的对齐；`unsqueeze(0)` 与不补维的差异不是风格问题——`cross_entropy` 期望 logits 是 `(N, C)`、目标是 `(N,)`，batch 维留着会把形状解释错（见 4.1.5 练习 3）。

随后一次前向拿全位置 logits：[reasoning_from_scratch/ch08.py:181](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L181)

```python
logits = model(input_ids).squeeze(0)   # (1, T-1, V) -> (T-1, V)
```

因果掩码（u2-l5）保证位置 `i` 的分布只看 `x_{\le i}`，所以这样「作弊」地一次看全文是合法的。最后一段切片与损失计算在 4.2 精读。

与第 6 章 GRPO 的 `sequence_logprob` 并排对照最能看清「同一机制、不同用法」——[reasoning_from_scratch/ch06.py:111-117](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L111-L117)：

```python
logprobs = torch.log_softmax(logits, dim=-1)
selected = logprobs[:-1].gather(
    1, token_ids[1:].unsqueeze(-1)
).squeeze(-1)
return torch.sum(selected[prompt_len - 1:])
```

同样的 `[:-1]` 输入视角、同样的 `[1:]` 目标、同样的 `prompt_len - 1` 起点；区别只有三点：它显式 `log_softmax` 后用 `gather` 抽分数（本讲交给 `cross_entropy` 内部完成），它**求和**（联合 logprob，供优势计算）而本讲**平均**（消除长度差异），它 `.float()` 升精度而本讲沿用模型 dtype。notebook 8.6 节正是用 `sequence_logprob` 的和除以答案 token 数（1.68）与 `cross_entropy`（1.68）对上了账——书中并说明改用 `cross_entropy` 是因为它「better optimized for faster training」（融合内核）。

#### 4.1.4 代码实践

按规格构造迷你样本，用假模型把 `compute_example_loss` 的内部动作全部打印出来，并手工复算交叉熵验证一致（示例代码，纯 CPU、不依赖权重下载）：

1. **实践目标**：亲眼看到 `input_ids`/`target_ids` 的错位、`answer_start` 切片与参与损失的位置数，并验证函数输出 == 手工 `cross_entropy`。
2. **操作步骤**：

```python
# 示例代码：保存为任意 .py 文件在项目环境运行（如 tutorial_u9l3_demo.py）
import torch
import torch.nn as nn
from reasoning_from_scratch.ch08 import compute_example_loss

VOCAB = 20  # 迷你词表

class FixedLogitModel(nn.Module):
    """示例代码：假模型——每个位置都返回同一行固定 logits，与输入内容无关。"""
    def __init__(self, vocab=VOCAB):
        super().__init__()
        g = torch.Generator().manual_seed(0)
        self.register_buffer("row", torch.randn(vocab, generator=g))

    def forward(self, input_ids):            # input_ids: (b, t)
        b, t = input_ids.shape
        return self.row.view(1, 1, -1).expand(b, t, -1)

example = {"token_ids": [10, 11, 12, 13, 14], "prompt_len": 3}
model = FixedLogitModel()
device = torch.device("cpu")

loss = compute_example_loss(model, example, device)
print("loss =", loss.item())

# —— 手工复刻函数内部，逐项打印 ——
token_ids, prompt_len = example["token_ids"], example["prompt_len"]
input_ids  = torch.tensor(token_ids[:-1]).unsqueeze(0)
target_ids = torch.tensor(token_ids[1:])
logits = model(input_ids).squeeze(0)
answer_start = max(prompt_len - 1, 0)

print("input_ids :", input_ids.squeeze(0).tolist())
print("target_ids:", target_ids.tolist())
print("answer_start:", answer_start)
print("answer_targets:", target_ids[answer_start:].tolist())
print("参与损失的位置数:", len(target_ids) - answer_start)

manual = torch.nn.functional.cross_entropy(
    logits[answer_start:], target_ids[answer_start:]
)
print("manual CE:", manual.item())
print("与函数输出完全一致:", torch.equal(manual, loss.detach()))
```

3. **需要观察的现象**：打印出的各项内容与下表一致；`manual` 与 `loss` 数值逐位相等。
4. **预期结果**（错位与切片是纯 Python 切片逻辑，可静态推出；具体损失数值依赖随机种子，**待本地验证**）：

| 打印项 | 预期值 |
| --- | --- |
| `input_ids` | `[10, 11, 12, 13]`（长度 4 = T-1） |
| `target_ids` | `[11, 12, 13, 14]` |
| `answer_start` | `2`（= 3-1） |
| `answer_targets` | `[13, 14]`（第 2 行起的目标） |
| 参与损失的位置数 | `2`（= 5-3 = 答案 token 数） |
| `torch.equal(manual, loss.detach())` | `True`（两边是对同一批张量执行同一 op，确定性计算） |

5. 为什么手工复算能「逐位相等」而不是「近似相等」：函数内部与手工版对**同一份 logits 切片、同一份目标**调用同一个 `cross_entropy`，没有随机性介入；`loss.detach()` 只是把带梯度的张量降级成普通张量便于比较，不改数值。

#### 4.1.5 小练习与答案

1. **练习**：如果把目标误切成 `token_ids[2:]`（错开两位），损失还能正常算出数字吗？会带来什么问题？
   **答案**：能算出数字——长度 `T-2` 的目标与 `logits[:T-2]` 维度仍然对得上——但语义已错：位置 `i` 被要求预测 `x_{i+2}`。训练信号整体「串位」，模型学到的是隔位预测而非下一词预测。这类 bug 不会报错，只能靠核对对齐关系发现。
2. **练习**：既然 u6-l1 已经能手工算平均负 logprob，为什么训练改用 `cross_entropy`？
   **答案**：两者数值等价（notebook 8.6 验证 1.68 == 1.68），但 `cross_entropy` 把 `log_softmax`、目标抽取、求平均融合在一个优化过的算子里，数值稳定且更快；书中原话是「better optimized for faster training」。
3. **练习**：`logits` 为什么必须 `squeeze(0)`？
   **答案**：`cross_entropy` 的约定是 logits `(N, C)`、目标 `(N,)`。若保留 batch 维传 `(1, T-1, V)`，会被解释成「1 个样本、`T-1` 个类别」的 batch，与目标 `(T-1,)` 的形状直接冲突报错。

### 4.2 answer_start 切片：只监督答案区（含 eos）

#### 4.2.1 概念说明

错位对齐给了 `T-1` 个可训练的预测位置，但蒸馏**不是**每个位置都要学。这条样本的建模对象是条件概率 \( p_\theta(\text{answer} \mid \text{prompt}) \)——「给定题目，产出教师式回答」。prompt 部分是条件、是输入，不是学习目标。所以：

- **目标侧排除 prompt**：所有 `target[i] = x_{i+1}` 里，`x_{i+1}` 落在 prompt 区（`i+1 < prompt_len`）的位置不进损失；
- **桥接位置必须保留**：位置 `prompt_len - 1` 的输入是**最后一个 prompt token**，它的预测目标是**第一个答案 token**——这正是「看到完整题目后说出答案的第一个字」这次预测，是条件建模里最关键的一步，绝不能丢。

两句话合成一个下标规则：监督从 `i = prompt_len - 1` 开始，即 `answer_start = max(prompt_len - 1, 0)`。它带来一个漂亮的封闭性：**被监督的目标恰好是全部答案 token（含 eos），每个恰好一次，一个不多一个不少**——被监督位置数 = `(T-1) - (prompt_len-1) = T - prompt_len` = 答案 token 数。

「prompt 不进损失」在别的代码库里常用 0/1 掩码实现（把 prompt 位置的目标替换成一个忽略索引再 `cross_entropy(ignore_index=...)`）；本仓库单样本版用**切片**实现同一语义——`logits[answer_start:]` 直接把 prompt 区整段裁掉，语义等价且省去构造掩码张量。批处理版则回到掩码路线：[ch08/04_train_with_distillation/distill_batched.py:212-214](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill_batched.py#L212-L214) 把每行 prompt 区的 label 设为 `IGNORE_INDEX`，推理时按活跃 token 数归一——详见 u9-l4。

`max(..., 0)` 是防御式编程：Python 切片的负下标有「从尾部数」的隐含义，若 `prompt_len = 0` 时没有这个保护，`logits[-1:]` 会静默地只监督最后 1 个位置——不报错、训练照跑、信号几乎全丢（4.2.4 实践会演示这个陷阱）。`build_examples` 产出的 prompt 经聊天模板编码必然非空，故该分支实际不会触发，但一行 `max` 挡住了一整类隐性 bug。

#### 4.2.2 核心流程

对齐表是本模块的全部要点。以实践用的迷你样本 `token_ids = [10, 11, 12, 13, 14]`、`prompt_len = 3` 为例（prompt = `[10,11,12]`，答案 = `[13,14]`，其中 14 扮演 eos）：

| 位置 `i` | 输入 `token_ids[i]` | 预测目标 `token_ids[i+1]` | 计入损失？ |
| --- | --- | --- | --- |
| 0 | 10 | 11 | 否（目标在 prompt 区） |
| 1 | 11 | 12 | 否 |
| 2 | **12（最后一个 prompt token）** | **13（第一个答案 token）** | **是 ← 桥接位置** |
| 3 | 13 | 14（此处为 eos） | 是 |

切片后的损失区间与语义：

```text
answer_start = prompt_len - 1                    # 从桥接位置起
answer_logits  = logits[answer_start:]           # 丢掉 prompt 区的预测行
answer_targets = target_ids[answer_start:]       # 只剩答案 token（含 eos）
loss = cross_entropy(answer_logits, answer_targets)   # 对该区取平均
```

被监督 token 数的核算：\( N = (T-1) - (prompt\_len - 1) = T - prompt\_len \)，即教师答案 token 数 + 1 个 eos。特别地，**eos 是最后一个监督目标**——模型由此学会「回答完了要停」，与 u3-l1 生成循环里的 EOS 提前停止首尾呼应：训练时学着输出 eos，推理时 eos 才能触发停止。

与 u6-l1 的对照值得再强调一次：[reasoning_from_scratch/ch05.py:112-118](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L112-L118) 的 `avg_logprob_answer` 里 `start = len(prompt_ids) - 1`、[reasoning_from_scratch/ch06.py:117](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L117) 的 `selected[prompt_len - 1:]`、本讲的 `answer_start`——全书三处「只看答案区」用的是**同一条下标约定**，学会了这里就贯通了那里。

#### 4.2.3 源码精读

`compute_example_loss` 的后半段，切片锚点与损失：[reasoning_from_scratch/ch08.py:183-189](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L183-L189)

```python
answer_start = max(prompt_len - 1, 0)
answer_logits = logits[answer_start:]     # 裁掉 prompt 区预测行
answer_targets = target_ids[answer_start:]  # 只留答案 token（含 eos）

loss = torch.nn.functional.cross_entropy(
    answer_logits, answer_targets         # 默认 reduction="mean"：对答案区取平均
)
return loss
```

四个要点逐条对应前文：`max(..., 0)` 防负索引；两行切片实现「prompt 不进损失」；`cross_entropy` 默认 `reduction="mean"`，返回值就是 4.1.2 公式里的平均负 logprob；返回的是**带 `grad_fn` 的张量**——函数上没有任何 `no_grad` 装饰器，这是它与 u6-l1 打分函数（`@torch.inference_mode()` + 返回 float）的本质分工：**打分器只读，损失器必须能反传**。

数据侧再确认 eos 的来源，[reasoning_from_scratch/ch08.py:119-121](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L119-L121)：`token_ids = prompt_ids + answer_ids + [tokenizer.eos_token_id]`。eos 拼在末尾、切片从桥接位置起，两个事实合起来才有「eos 恰好是最后一个监督目标」。

#### 4.2.4 代码实践

用纯 Python 切片做一次「下标审计」，覆盖多种 `prompt_len`，并亲手触发那个负索引陷阱（示例代码，不需要模型）：

1. **实践目标**：验证 `answer_start` 规则的三条性质——被监督目标恰为答案区、桥接位置保留、数量 = `len(token_ids) - prompt_len`；并看清没有 `max` 保护时的静默错误。
2. **操作步骤**：

```python
# 示例代码
token_ids = [10, 11, 12, 13, 14]

for prompt_len in [3, 1, 0]:
    answer_start = max(prompt_len - 1, 0)
    targets = token_ids[1:][answer_start:]
    bridge_in  = token_ids[answer_start]          # 桥接位置的输入
    bridge_out = token_ids[answer_start + 1]      # 桥接位置的目标
    n_rule  = len(token_ids) - prompt_len         # 公式核算
    n_slice = len(targets)                        # 切片实得
    print(f"prompt_len={prompt_len}: answer_start={answer_start}, "
          f"监督目标={targets}, 数量={n_slice} (公式={n_rule}), "
          f"桥接: {bridge_in} -> {bridge_out}")

# 负索引陷阱：若不写 max(..., 0)
bad_start = 0 - 1
print("无保护时的切片:", token_ids[1:][bad_start:])
```

3. **需要观察的现象**：三组参数下「数量」与「公式」两列始终相等；`prompt_len=0` 时桥接输入不再是 prompt 末 token 而是序列首 token；最后一行展示负索引的静默错误。
4. **预期结果**（纯切片逻辑，可静态推出；仍建议本地跑一遍确认）：

| `prompt_len` | `answer_start` | 监督目标 | 数量 = 公式 | 桥接 |
| --- | --- | --- | --- | --- |
| 3 | 2 | `[13, 14]` | 2 = 5-3 | 12 → 13 |
| 1 | 0 | `[11, 12, 13, 14]` | 4 = 5-1 | 10 → 11 |
| 0 | 0（被 max 保护） | `[11, 12, 13, 14]` | 4 = 5-0 | 10 → 11 |

   最后一行输出 `无保护时的切片: [14]`——`prompt_len=0` 且无保护时 `answer_start=-1`，Python 把 `[-1:]` 解释为「从最后一行起」，损失只会监督 1 个位置且**不报任何错**。这就是 `max(prompt_len - 1, 0)` 存在的理由。

5. 回答规格里的问题——**为什么 prompt 部分不计入蒸馏损失**：① 建模对象是 \( p(\text{answer}\mid\text{prompt}) \)，prompt 是条件不是目标，对它算损失等于让模型学「出题」而不是「答题」；② prompt 由 `render_prompt` 的固定模板渲染（u4-l1），对它计算损失只会把梯度耗在记忆模板与题目文本统计上，稀释真正的训练信号；③ 推理时 prompt 由用户给定，模型永远不需要自己生成它。

#### 4.2.5 小练习与答案

1. **练习**：一条样本 `len(token_ids) = 100`、`prompt_len = 40`，有多少个位置计入损失？分别是哪些 token？
   **答案**：`100 - 40 = 60` 个，即第 40 到第 99 个 token（59 个教师答案 token + 1 个 eos），每个恰好作为一次预测目标。
2. **练习**：如果把 `answer_start` 误写成 `prompt_len`（没有减一），丢掉的是什么？
   **答案**：丢掉桥接位置——模型再也不会因「看到完整 prompt」而被要求预测**第一个**答案 token（本例中的 `<think>` 起始 token）。生成时最关键的第一步就缺了训练信号。
3. **练习**：若 `build_examples` 不在末尾拼 eos，损失与推理各受什么影响？
   **答案**：损失少监督一个「何时停」的目标；推理时模型更难输出 eos，EOS 提前停止（u3-l1）难以触发，回答容易一直生成到 `max_new_tokens` 才截断。

### 4.3 evaluate_examples：验证集评估与训练损失的区别

#### 4.3.1 概念说明

`compute_example_loss` 是「单条样本的损失」，训练与评估都要用，但**用法不同**，这正是本讲第三个学习目标：

- **训练损失**：`train_distillation` 每步对**当前这一条**样本算损失（带梯度）→ `backward()` → 更新参数。写进 CSV 的 `train_loss` 列是单样本损失，**噪声很大**——不同题目难度不同，损失天然起伏。notebook 8.8 节原话：「Training loss is noisy because it's based on a single sample」。
- **验证损失**：`evaluate_examples` 在验证集上逐条复用同一个 `compute_example_loss`，取**宏平均**（每条样本等权，与其答案长短无关），且整个过程 `no_grad` + `eval` 模式。它平滑、可比较，是判断「是否收敛、是否过拟合」的曲线。

三处工程细节值得抠：

1. **`@torch.no_grad()` 而非修改函数本身**：梯度控制放在包装层，`compute_example_loss` 保持「可训练」的纯净身份——同一个函数，训练时留梯度、验证时被上下文摘掉梯度。
2. **模式的礼貌恢复**：进入时记下 `was_training`、切 `model.eval()`，退出时**仅当原本在训练**才切回 `model.train()`——评估不应该改变调用方的模式状态，调用者原来是什么模式，结束后还是什么模式。
3. **`.item()` 及早剥离计算图**：每条损失立刻转成 Python float 累加，循环 25 条也不会把图攒在显存里。

#### 4.3.2 核心流程

`evaluate_examples(model, examples, device)` 的流程：

```text
装饰器 @torch.no_grad()          # 全程不建计算图
├── was_training = model.training # 记住调用方模式
├── model.eval()                  # 切评估模式（dropout 等）
├── 对每条 example：
│     loss = compute_example_loss(model, example, device)
│     total_loss += loss.item()   # float 累加，不留图
│     num_examples += 1
├── 若 was_training：model.train() # 礼貌恢复
└── return total_loss / num_examples   # 宏平均
```

训练循环中的两个消费点（见 4.3.3 源码）：每步用 `compute_example_loss` 算带梯度的损失并反传；每 `log_every` 步用 `evaluate_examples` 算验证损失、随后**又**显式 `model.train()`——即使 `evaluate_examples` 已经会恢复，仍再切一次，双保险（也防住了 `evaluate_examples` 内部逻辑变化的风险）。

真实日志能看出验证损失为什么是「要盯的那条曲线」：notebook 8.8 节给出的完整训练结果中，DeepSeek-R1 教师数据的最终 val loss 0.5343 对应 MATH-500 准确率 33.6%，Qwen3-235B 教师数据的 0.4043 对应 45.0%——**验证损失越低、蒸馏效果越好，且教师质量（u9-l1 的「教师即学生上限」）直接写进这条曲线**。

#### 4.3.3 源码精读

验证函数全文只有 16 行，四个要点各就各位：[reasoning_from_scratch/ch08.py:193-196](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L193-L196) 是装饰器与「记忆 + 切换」：

```python
@torch.no_grad()
def evaluate_examples(model, examples, device):
    was_training = model.training
    model.eval()
```

[reasoning_from_scratch/ch08.py:200-203](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L200-L203) 逐条复用损失函数、`.item()` 剥图累加：

```python
for example in examples:
    loss = compute_example_loss(model, example, device)
    total_loss += loss.item()
    num_examples += 1
```

[reasoning_from_scratch/ch08.py:205-208](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L205-L206) 条件恢复 + 宏平均返回：

```python
if was_training:
    model.train()
return total_loss / num_examples
```

注意两点：恢复是**有条件的**（原本 `eval` 就保持 `eval`）；返回值没有空列表保护，`examples` 为空时 `0.0/0` 直接 `ZeroDivisionError`（见 4.3.5 练习 2）。

训练侧的两个消费点——[reasoning_from_scratch/ch08.py:252-255](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L252-L255) 训练路径（同一函数、带梯度身份）：

```python
loss = compute_example_loss(model, example, device)
loss.backward()
```

[reasoning_from_scratch/ch08.py:267-273](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L267-L273) 验证路径（无梯度身份 + 双保险切回训练）：

```python
val_loss = evaluate_examples(
    model=model, examples=val_examples, device=device,
)
model.train()
```

#### 4.3.4 代码实践

用假模型验证 `evaluate_examples` 的两个行为：**宏平均等于逐条手工复算**、**模式礼貌恢复**（示例代码，纯 CPU）：

1. **实践目标**：确认 (a) 返回值 = 各样本 `compute_example_loss` 的算术平均；(b) 调用前后 `model.training` 不被意外改变；(c) 原本 `eval` 的模型不会被切回 `train`。
2. **操作步骤**：

```python
# 示例代码：接着 4.1.4 的 FixedLogitModel 定义
import torch
from reasoning_from_scratch.ch08 import compute_example_loss, evaluate_examples

model = FixedLogitModel()
examples = [
    {"token_ids": [10, 11, 12, 13, 14], "prompt_len": 3},
    {"token_ids": [5, 6, 7],            "prompt_len": 1},
    {"token_ids": [8, 9, 10, 11],       "prompt_len": 2},
]
device = torch.device("cpu")

# (b) 原本 train：评估后应恢复为 train
model.train()
val = evaluate_examples(model, examples, device)
print("train -> eval -> ?", model.training, "| val_loss =", val)

# (c) 原本 eval：评估后应保持 eval
model.eval()
evaluate_examples(model, examples, device)
print("eval -> eval -> ?", model.training)

# (a) 手工复算宏平均
manual = sum(
    compute_example_loss(model, ex, device).item() for ex in examples
) / len(examples)
print("manual mean =", manual, "| 相等:", abs(manual - val) < 1e-12)
```

3. **需要观察的现象**：第一行打印 `model.training` 为 `True`（被恢复）；第二行为 `False`（保持 eval）；第三行手工均值与函数返回值相等。
4. **预期结果**：三个断言均成立。`val` 的具体数值依赖假模型的固定 logits，可复现但无参考意义（**待本地验证**）；相等性检查 `abs(manual - val) < 1e-12` 应通过（两边是同样的浮点累加顺序：逐条 `.item()` 求和再除以条数，仅浮点加法结合顺序一致，误差若出现也在 1e-12 量级以下）。
5. 若把 `examples` 换成空列表 `[]` 再调用，观察 `ZeroDivisionError: division by zero`——源码对空验证集没有防护，实际使用时要保证 `val_examples` 非空（notebook 的切分固定留了 25 条）。

#### 4.3.5 小练习与答案

1. **练习**：`evaluate_examples` 复用的就是 `compute_example_loss`，为什么验证时不会影响梯度、训练时又能反传？
   **答案**：梯度控制在**调用上下文**而不是函数里。验证路径被 `@torch.no_grad()` 包住，算子不记录 `grad_fn`；训练路径没有这个装饰器，同一个函数返回带计算图的张量，可直接 `backward()`。一个函数、两种身份。
2. **练习**：`evaluate_examples(model, [], device)` 会发生什么？这提示使用时要注意什么？
   **答案**：`total_loss / num_examples` 变成 `0.0 / 0`，抛 `ZeroDivisionError`。提示：验证集切分必须保证非空；书中流程固定取 `val_examples`（25 条）不存在此问题，但自定义小实验时容易踩到。
3. **练习**：为什么训练曲线（CSV 的 `train_loss` 列）毛刺很大、而 `val_loss` 平滑？读图时各看什么？
   **答案**：`train_loss` 是**当前单条**样本的损失，样本难度不同导致天然波动；`val_loss` 是固定 25 条验证样本的宏平均，平滑可比。读图主要盯 `val_loss`：它下降说明模型在验证数据上越来越像教师，走平提示收敛，回升则警惕过拟合——notebook 8.8 节的真实曲线正是「迅速下降后趋平」。

## 5. 综合实践

**任务：对比两种训练目标——answer-only（本讲实现）vs 全序列（answer_start=0）——在同一个迷你蒸馏任务上的表现。** 这个实验把三个模块串起来：错位构造、answer_start 切片、`evaluate_examples` 验证评估，并从实验角度回答「为什么 prompt 不进损失」。

1. **实践目标**：亲手看到「损失算在哪些位置，梯度就花在哪些位置」，理解切片锚点对训练目标的定向作用。
2. **操作步骤**：

```python
# 示例代码：纯 CPU、秒级训练
import copy, torch, torch.nn as nn
from reasoning_from_scratch.ch08 import compute_example_loss, evaluate_examples

VOCAB = 20

class TinyNextToken(nn.Module):
    """示例代码：只看当前 token 的迷你下一词预测模型。"""
    def __init__(self, vocab=VOCAB, dim=16):
        super().__init__()
        self.emb, self.head = nn.Embedding(vocab, dim), nn.Linear(dim, vocab)
    def forward(self, x):
        return self.head(self.emb(x))

def full_loss(model, example, device):
    """示例代码：复制 compute_example_loss，仅把 answer_start 改为 0（全序列损失）。"""
    token_ids = example["token_ids"]
    input_ids = torch.tensor(token_ids[:-1], dtype=torch.long, device=device).unsqueeze(0)
    target_ids = torch.tensor(token_ids[1:], dtype=torch.long, device=device)
    logits = model(input_ids).squeeze(0)
    return torch.nn.functional.cross_entropy(logits, target_ids)   # 不切片

# 6 条样本：同一 prompt 前缀 [10,11,12]，不同答案
examples = [
    {"token_ids": [10, 11, 12] + ans, "prompt_len": 3}
    for ans in ([13, 14], [13, 15, 14], [13, 14, 16], [15, 14], [16, 13, 14], [13, 16, 14])
]
train_ex, val_ex = examples[:4], examples[4:]
device = torch.device("cpu")

for name, loss_fn in [("answer-only", compute_example_loss), ("full-seq", full_loss)]:
    torch.manual_seed(0)
    model = TinyNextToken()
    opt = torch.optim.AdamW(model.parameters(), lr=1e-2)
    model.train()
    for step in range(301):
        opt.zero_grad()
        loss = loss_fn(model, train_ex[step % len(train_ex)], device)
        loss.backward()
        opt.step()
        if step % 100 == 0:
            val = evaluate_examples(model, val_ex, device)
            model.train()
            print(f"[{name}] step={step:3d} train_loss={loss.item():.4f} val={val:.4f}")
```

3. **需要观察的现象**：两种目标的 `val`（注意：统一用 answer-only 口径评估）都随训练下降；记录两者最终 `val` 的大小关系，以及 `full-seq` 版额外在哪些位置花了梯度（对照 4.2 的对齐表：`[10,11]` 两个 prompt 区预测位置）。
4. **预期结果**：两条曲线均下降；在小词表、短序列的玩具设定下两种目标差异可能不明显（**待本地验证**——请以实际跑出的数字为准）。思考题：若 prompt 前缀很长（比如 200 个 token 的固定模板）而答案很短，哪种目标的梯度会被「稀释」得更厉害？为什么？
5. **延伸**：把 `train_ex`/`val_ex` 换成 u9-l2 `build_examples` 的真实样本（需先下载数据与权重，建议 GPU），对同一样本分别打印 `compute_example_loss` 与 notebook 8.6 节的 `avg_logprob = -sequence_logprob / num_answer_tokens`，验证两者数值一致（书中为 1.68 对 1.68）。

## 6. 本讲小结

- **错位构造**：输入 `token_ids[:-1]`、目标 `token_ids[1:]`，teacher forcing 下一次前向即得全部 `T-1` 个「预测下一词」位置；`cross_entropy(logits, targets)` 内部完成 `log_softmax` + 抽取 + 平均，数值上就是答案区的平均负 logprob（notebook 验证 1.68 == 1.68），比手写版更快更稳。
- **answer_start 切片**：`answer_start = max(prompt_len - 1, 0)`——从「最后一个 prompt token 预测第一个答案 token」的桥接位置起算；被监督目标恰为全部答案 token（含 eos）各一次、`prompt_len - 1` 之前的目标全部排除；`max` 防的是负索引的静默切片错误，eos 在监督内教会模型「何时停」。
- **prompt 不进损失**：建模对象是 \( p(\text{answer}\mid\text{prompt}) \)；对模板化 prompt 算损失只会稀释训练信号。单样本版用切片实现，批处理版（u9-l4）用 `IGNORE_INDEX` 掩码实现，语义等价。
- **同一函数的两种身份**：训练路径 `compute_example_loss` 不加装饰器、返回带 `grad_fn` 的张量供 `backward()`；验证路径被 `@torch.no_grad()` 的 `evaluate_examples` 包住，`.item()` 剥图累加取宏平均（每样本等权），并用 `was_training` 礼貌恢复模式。
- **读曲线**：CSV 里 `train_loss` 是单样本损失、噪声大；`val_loss` 是验证集宏平均、平滑可比，是判断收敛与过拟合的主曲线——真实日志中 val loss 0.53 对应 MATH-500 33.6%、0.40 对应 45.0%，教师质量直接写进这条曲线。
- 全书「只看答案区」共用同一条下标约定：`ch05.avg_logprob_answer` 的 `start = len(prompt_ids) - 1`、`ch06.sequence_logprob` 的 `selected[prompt_len - 1:]`、本讲的 `answer_start`。

## 7. 下一步学习建议

本讲交付了蒸馏的训练信号，下一讲 **u9-l4（蒸馏训练循环：单样本与批处理）** 把它装进 `train_distillation` 的十步循环：优化器、梯度裁剪、epoch 检查点与 CSV 日志（其中 L252/L267 两个消费点本讲已预览）。阅读建议：

1. 精读 [reasoning_from_scratch/ch08.py:211-296](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L211-L296) 的 `train_distillation`，找出本讲的两个函数在循环里的位置，并与 u7-l4 的 GRPO 八阶段循环对比骨架异同。
2. 预读 [ch08/04_train_with_distillation/distill_batched.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill_batched.py) 的 `prepare_batch_tensors`：看 `IGNORE_INDEX` 掩码（L212-L214）如何把本讲的「切片」推广到「padding + 变长 batch」场景。
3. 想巩固交叉熵与下一词训练的通用背景，可回看 u6-l1 的三个打分函数与本讲 4.1 的对照表——「打分（推理模式、float）」与「损失（训练模式、张量）」的分野将贯穿所有后续训练代码。
