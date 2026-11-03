# u6-l1　Log-prob 与序列似然打分

## 1. 本讲目标

前两讲（第 4 章）我们一直在「从分布中取样」：温度重塑分布、top-p 砍掉长尾、multinomial 抽 token。本讲把视角反转过来——**文本已经给定，反过来问模型「你认为这段文本有多大概率？」**。这就是序列似然打分（sequence likelihood scoring），它把一个生成模型变成一个打分器。

学完本讲你应该能够：

1. 解释「一次前向即可给整段文本打分」的原理（teacher forcing），以及它与逐 token 生成循环的本质区别。
2. 读懂 `calc_next_token_probas` 与 `calc_next_token_logprobas`：如何用错位索引从全位置概率矩阵中抽出每个「真实 next token」的分数。
3. 说出为什么联合概率要用 log 表示（乘积变求和、避免下溢），以及 `torch.log_softmax` 相比 `log(softmax(x))` 的数值稳定性优势。
4. 掌握 `avg_logprob_answer` 的两个关键改造——只对答案区间打分、对逐 token 分数取均值——并理解**长度偏差**问题：为什么求和版本天然惩罚长答案，均值版本又丢失了什么信息。
5. 理解 `all_logprobas[t_idx, next_ids]` 这种「gather 式」配对索引与 `torch.gather` 的等价关系。

本讲是单元六（第 5 章「自我精炼」）的第一讲：打分器是自我精炼循环的裁判，下一讲的 `heuristic_score` 与本讲的 logprob 分数是两个可互换的裁判。

## 2. 前置知识

### 2.1 语言模型就是一张「下一 token 概率表」

在第 2、4 章我们已经反复见过：把 token 序列喂进模型，每个位置都输出一份覆盖全词表（151936 个 token）的 logits，经 softmax 后就是「下一个 token 的概率分布」。生成时我们只关心**最后一个位置**的分布，从中采样；本讲则要利用**每一个位置**的分布。

这里有一个来自 [u2-l5（KV Cache 与因果掩码）](u2-l5-kv-cache-and-mask.md)的关键性质：因果掩码保证位置 \(t\) 的输出分布只依赖 \(x_1 \dots x_t\)，看不到未来。所以对整段已知文本做一次前向后，**第 \(t\) 行 logits 天然就是「给定前文、预测 \(x_{t+1}\)」的合法分布**——逐 token 生成要跑 T 次前向才能拿到的所有分布，一次前向全部到手。这种「把真实文本喂入模型、在各位置核对预测」的做法通常叫 **teacher forcing**。

### 2.2 序列的联合概率：链式法则

一段文本 \(x_1, x_2, \dots, x_T\) 在模型参数 \(W\) 下的概率，按自回归因子分解（链式法则）：

\[
P(x_1, x_2, \dots, x_T \mid W)
= \prod_{t=1}^{T} P(x_t \mid x_{1:t-1}, W)
\]

即每个 token 的条件概率相乘。第 5 章主 notebook 的 5.4 节给出的正是这个式子。注意 \(P(x_1 \mid W)\) 对应「无前文预测第一个 token」，在实现里它由位置 0 的分布给出。

### 2.3 为什么要取 log：乘积会下溢

softmax 输出大多很小。主 notebook 实测（bfloat16）：句子 "The capital of Germany is Berlin" 的 5 个转移概率里就有 `6.15e-05` 这样的数，连乘后联合概率只剩 `5.9e-08`；把 Berlin 换成 Bridge 会得到 `1.05e-13`。序列一长，连乘会迅速逼近浮点数下限（bfloat16 只有 8 位指数），最终下溢为 0，无法比较。

对数把乘法变加法：

\[
\log P(x_1, \dots, x_T \mid W)
= \log \prod_{t=1}^{T} P(x_t \mid x_{1:t-1}, W)
= \sum_{t=1}^{T} \log P(x_t \mid x_{1:t-1}, W)
\]

每个 \(\log P \le 0\)，序列再长也只是负数相加，量级友好。同一组实测数据取 log 后：Berlin 的联合 log-prob 为 `-16.6`，Bridge 为 `-29.9`——都在人类可读的范围内，且大小关系与概率完全一致（log 是单调函数）。

### 2.4 `@torch.inference_mode()`

本讲三个函数都带这个装饰器：纯推理、不需要梯度，禁用 autograd 追踪可以省显存并加速。与第 2~4 章生成函数的做法一致。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [reasoning_from_scratch/ch05.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py) | 第 5 章的可复用实现。本讲精读开头三个打分函数（L46-L121）；后面的 `heuristic_score`（L14-L43）留给下一讲，`self_refinement_loop`（L148-L268）留给 u6-l3 |
| [ch05/01_main-chapter-code/ch05_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/01_main-chapter-code/ch05_main.ipynb) | 章节主 notebook。5.4 节演示概率打分，5.5 节引入 logprob，5.6 节手工推导答案区间均值后再封装成 `avg_logprob_answer`。本讲引用的实测数值均出自这里 |
| [tests/test_ch05.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch05.py) | 第 5 章 pytest 回归。其中 `test_avg_logprob_answer_uses_answer_token_logprobs`（L58-L81）用假模型 + 假分词器**无需下载权重**即可验证答案区间的切片边界，是本讲实践的重要依据 |

另外注意 [ch05.py:L5-L9](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L5-L9) 的 import：`extract_final_candidate`/`render_prompt` 来自 ch03，`generate_text_stream_concat_flex`/`generate_text_top_p_stream_cache` 来自 ch04——第 5 章代码直接站在第 3、4 章肩膀上，本讲的打分函数与 [u5-l2 讲过的 top_p 过滤器](u5-l2-top-p-filtering.md)同处一条 import 链。

## 4. 核心概念与源码讲解

### 4.1 概率打分：从「生成」到「评分」的第一步

#### 4.1.1 概念说明

`calc_next_token_probas` 回答的问题是：**「在模型眼里，这句话每个位置的真实下一个 token 有多大概率？」** 它是最朴素的打分器：整段文本（prompt + 想评的部分拼在一起）一次前向，取出每个位置上「真实发生了的那个 token」的概率。

它与生成函数的差别只在一处：生成函数每步只看末位分布并采样，本函数看全部位置的分布并**核对已知的答案**。这也解释了它为什么不需要 KV cache、不需要循环——没有未知 token 要生成。

#### 4.1.2 核心流程

```text
输入: prompt 文本（要评分的整段）
1. tokenizer.encode(prompt) → token_ids, 长度 T
2. model(token_ids) 一次前向 → logits, 形状 (T, vocab)
3. softmax → all_probas, 形状 (T, vocab)
4. 构造错位索引:
   t_idx    = [0, 1, ..., T-2]        # T-1 个「预测起点」位置
   next_ids = token_ids[1:]           # T-1 个「真实下一个 token」
5. 配对抽取: next_token_probas[i] = all_probas[t_idx[i], next_ids[i]]
6. 联合概率 = torch.prod(next_token_probas)   # 连乘
```

第 4 步的错位是本讲反复出现的核心手法，值得先在这里吃透：**位置 \(t\) 的分布预测的是 \(x_{t+1}\)**，所以「打分位置」与「被预测 token」天然错开一位。位置 T-1（最后一个 token）没有对应的「下一个 token」可核对，因此 `t_idx` 止步于 T-2。

#### 4.1.3 源码精读

函数位于 [reasoning_from_scratch/ch05.py:L46-L73](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L46-L73)：

```python
@torch.inference_mode()
def calc_next_token_probas(model, tokenizer, prompt, device):

    token_ids = torch.tensor(tokenizer.encode(prompt), device=device)

    # Get logits and probabilities similar to text generation functions
    logits = model(token_ids.unsqueeze(0)).squeeze(0)
    all_probas = torch.softmax(logits, dim=-1)

    # Positions we score (here: all)
    t_idx = torch.arange(0, token_ids.shape[0] - 1, device=device)

    # Since we have the text, we know the true next tokens
    next_ids = token_ids[1:]

    # Get probabilities for each next token
    next_token_probas = all_probas[t_idx, next_ids]
```

逐行说明：

- `token_ids.unsqueeze(0)` 把形状从 `(T,)` 升为 `(1, T)`（补 batch 维），前向后 `squeeze(0)` 再压回去，得到 `(T, vocab)`。
- `t_idx = torch.arange(0, token_ids.shape[0] - 1)` 生成打分位置 `[0 .. T-2]`，注释 "Positions we score (here: all)" 点明：**这里给全序列打分，4.3 节会把它换成答案区间**。
- `next_ids = token_ids[1:]` 是错位后的「标准答案」。
- `all_probas[t_idx, next_ids]` 是 PyTorch 的**配对高级索引**：两个等长索引向量按元素配对，结果第 `i` 项是 `all_probas[t_idx[i], next_ids[i]]`——一次取出所有「真实 next token」的概率，无需任何 Python 循环。它与 `torch.gather` 语义等价，详见 4.2.3。

函数末尾直接打印逐 token 概率与连乘结果（[L64-L73](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L64-L73)）：

```python
    print(
        "Next-token probabilities:",
        [p.item() for p in next_token_probas]
    )

    # Likelihood of the sequence is the product of the probability scores
    print(
        "Joint probability:",
        torch.prod(next_token_probas)
    )
```

一个容易踩的小坑：**包版本的这个函数只打印、不返回**（也没有 `show` 参数）。主 notebook 5.4 节里的同名函数多了 `show=True` 参数，`show=False` 时返回 `(next_token_probas, prod_next_token_probas)`。4.2 节的 `calc_next_token_logprobas` 则两个版本都带 `show` 开关——需要拿到返回值做后续计算时，请用它。

主 notebook 5.4 节用三个句子实测过这个函数（0.6B base 模型，bfloat16，CPU）：

| 句子 | 最后一个 token 的概率 | 联合概率 |
| --- | --- | --- |
| The capital of Germany is **Berlin** | 1.69e-01 | 5.94e-08 |
| The capital of Germany is **Hamburg** | 2.93e-02 | 1.03e-08 |
| The capital of Germany is **Bridge** | 2.98e-07 | 1.05e-13 |

前三项概率完全相同（同一个前缀的必然结果），句子间的差异**全部来自最后一个 token**—— Bridge 荒谬，概率低 6 个数量级，联合概率直接被它拖垮。这正是「似然打分能分辨流畅与荒谬」的直观证据；同时也暴露了缺点：联合概率已经小到 `1e-13`，序列再长必然下溢。

#### 4.1.4 代码实践（阅读型 + 可选运行）

1. **实践目标**：在不跑模型的情况下，理解「错位索引」与「前缀共享」两个性质。
2. **操作步骤**：
   - 打开主 notebook 的 5.4 节（cell 内调用 `calc_next_token_probas` 的三处），对照上表。
   - 已知 "The capital of Germany is" 编码为 5 个 token、末位 token 分别为 Berlin/Hamburg/Bridge，回答：三个句子的 `next_token_probas` 向量前 4 项为什么完全一样？
   - 若本地已按 [u1-l2](u1-l2-setup-and-first-run.md) 装好环境且下载了 base 权重，可选运行 notebook 5.2 节的加载 cell 后执行 `calc_next_token_probas(model, tokenizer, "The capital of Germany is Berlin", device)`。
3. **需要观察的现象**：前 4 项概率在三个句子间逐一相等；只有第 5 项（末位 token 的概率）不同。
4. **预期结果**：联合概率的大小关系为 Berlin > Hamburg > Bridge，与语感一致。注意 notebook 5.4 节的提示：这些数接近 0，在 CPU/MPS/CUDA 上会略有差异。若不运行，以上结论可直接从上表核对。
5. 数值随设备浮动，具体小数位**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`t_idx = torch.arange(0, token_ids.shape[0] - 1)` 为什么止于 T-2，而不是 T-1？

**答案**：位置 T-1（最后一个 token）的分布预测的是「文本结束之后的下一个 token」，而文本中没有它的真值可核对；打分只覆盖文本中真实存在的 T-1 个转移（\(x_1 \to x_2\)，…，\(x_{T-1} \to x_T\)）。

**练习 2**：`calc_next_token_probas` 与第 2 章的 `generate_text_basic` 都调用了 `model(...)`，它们对输出 logits 的用法有何不同？

**答案**：`generate_text_basic` 每步只取**最后一个位置**的 logits，从中 argmax/采样出新 token，循环 T 次；`calc_next_token_probas` 一次前向后使用**全部位置**的 logits，逐位核对已知文本，不做任何采样。

**练习 3**：如果想比较「Berlin 句」和「Hamburg 句」的联合概率，是否必须算完整连乘？

**答案**：不必。两句前缀相同，联合概率只差最后一个因子 \(0.169 / 0.0293 \approx 5.8\) 倍，Berlin 句必胜——log 域里就是一项 `-1.78` 对 `-3.53` 之差。前缀共享时，比较可以只在差异 token 上进行。

### 4.2 取对数：`log_softmax` 与 gather 式索引（`calc_next_token_logprobas`）

#### 4.2.1 概念说明

`calc_next_token_logprobas` 是概率版的直接升级：把 `softmax` 换成 `log_softmax`、把连乘 `torch.prod` 换成求和 `torch.sum`，其余逻辑（一次前向、错位索引）完全不变。动机有二：

1. **数值稳定**：如 2.3 节所述，log 域把 `1e-13` 这类数变成 `-29.9`，不再有下溢风险。
2. **可组合**：求和形式的分数可以直接拆开看每一项的贡献（哪个 token「意外」了一眼便知），也为 4.3 节「只对一段子区间求和再取均值」铺路。

#### 4.2.2 核心流程

与 4.1.2 的流程只差两处（用 ⭐ 标出）：

```text
1. encode → token_ids (长度 T)
2. model 一次前向 → logits (T, vocab)
3. ⭐ log_softmax(logits, dim=-1) → all_logprobas (T, vocab)
4. t_idx = [0 .. T-2];  next_ids = token_ids[1:]
5. next_token_logprobas[i] = all_logprobas[t_idx[i], next_ids[i]]
6. ⭐ 联合 log-probability = torch.sum(next_token_logprobas)
```

数学上，第 3+6 步合起来实现的正是：

\[
\log P(x_1, \dots, x_T \mid W)
= \sum_{t=1}^{T-1} \log P\big(x_{t+1} \mid x_{1:t}, W\big)
\]

（实现从转移 \(x_1 \to x_2\) 开始计，故上界为 T-1 项。）

#### 4.2.3 源码精读

函数位于 [reasoning_from_scratch/ch05.py:L76-L96](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L76-L96)：

```python
@torch.inference_mode()
def calc_next_token_logprobas(model, tokenizer, prompt, device, show=True):

    token_ids = torch.tensor(tokenizer.encode(prompt), device=device)

    logits = model(token_ids.unsqueeze(0)).squeeze(0)
    # We now use log_softmax
    all_logprobas = torch.log_softmax(logits, dim=-1)

    t_idx = torch.arange(0, token_ids.shape[0] - 1, device=device)
    next_ids = token_ids[1:]
    next_token_logprobas = all_logprobas[t_idx, next_ids]

    # We replace the product with a sum
    sum_next_token_logprobas = torch.sum(next_token_logprobas)

    if show:
        print("Next-token log-probabilities:", next_token_logprobas)
        print("Joint log-probability:", sum_next_token_logprobas)
    else:
        return next_token_logprobas, sum_next_token_logprobas
```

三个讲解点：

- **`log_softmax` 而非 `log(softmax(x))`**。主 notebook 5.5 节先演示了 `torch.log(torch.softmax(logits))` 与 `torch.log_softmax(logits)` 在温和的 logits（`linspace(-2, 2)`）下结果一致，随后指出应使用后者，因为 "The logprobs are in a more reasonable range for numerically stable computing"。`log_softmax` 内部使用 logsumexp 技巧：先减去行最大值再做 exp/log，避免大 logits 直接 exp 上溢或小概率下溢成 0 后取 log 得 `-inf`。本仓库模型权重为 bfloat16，数值敏感性更高，这个细节不是锦上添花而是必需。
- **`show` 开关的设计**：`show=True` 打印（教学用），`show=False` 返回 `(逐 token logprobs, 总和)`（工程用）。这个二元组正是 4.3 节 `avg_logprob_answer` 手工推导时的原料。
- **gather 式配对索引**。`all_logprobas[t_idx, next_ids]` 是 PyTorch 高级索引语法：`t_idx` 与 `next_ids` 是两个等长的一维张量，结果第 `i` 项取「第 `t_idx[i]` 行、第 `next_ids[i]` 列」。它等价于 `torch.gather` 的如下写法（**示例代码**，仓库中并未这样调用，但语义相同）：

```python
# 示例代码：与 all_logprobas[t_idx, next_ids] 完全等价
gathered = torch.gather(
    all_logprobas,          # (T, vocab)
    dim=1,
    index=next_ids.unsqueeze(1),   # (T-1, 1)：每行给出要抽取的列号
).squeeze(1)               # (T-1,)
```

配对索引更简洁（不需要升维再降维），`gather` 则在需要保持批量维度时更通用——第 6 章和第 8 章的批处理代码里会见到类似形态的按行取列操作。

notebook 5.5 节的实测（与 4.1 表格同一组句子）：

| 句子 | 逐 token logprobs | 联合 log-prob |
| --- | --- | --- |
| …is **Berlin** | [-9.69, -0.77, -4.09, -0.30, **-1.78**] | **-16.63** |
| …is **Hamburg** | [-9.69, -0.77, -4.09, -0.30, **-3.53**] | -18.38 |
| …is **Bridge** | [-9.69, -0.77, -4.09, -0.30, **-15.00**] | -29.88 |

对照 4.1 的概率表：`log(1.69e-01) ≈ -1.78`、`log(2.98e-07) ≈ -15.0`，两表完全自洽；且现在能一眼看出 Bridge 句「差在哪一项」。

#### 4.2.4 代码实践（无需模型权重，纯 torch）

1. **实践目标**：亲手验证 (a) `log(softmax(x))` 在大 logits 下失稳而 `log_softmax` 稳定；(b) 配对索引与 `gather` 等价。
2. **操作步骤**：在装好 torch 的环境里运行下面这段**示例代码**（任何目录均可，不需要模型与分词器）：

```python
# 示例代码：数值稳定性与索引等价性验证
import torch

torch.set_printoptions(precision=4, sci_mode=False)

# (a) 数值稳定性：温和 logits 两者一致
x = torch.linspace(-2, 2, steps=7)
print(torch.log(torch.softmax(x, dim=-1)))
print(torch.log_softmax(x, dim=-1))          # 与上一行相同

# (b) 极端 logits：先 softmax 再 log 会失稳
big = torch.tensor([1000.0, 0.0, -1000.0])
print(torch.log(torch.softmax(big, dim=-1)))  # 含 -inf 或 nan
print(torch.log_softmax(big, dim=-1))         # [-0.0, -1000.0, -2000.0]

# (c) 配对索引 == gather
torch.manual_seed(0)
logprobs = torch.log_softmax(torch.randn(5, 10), dim=-1)  # (T=5, vocab=10)
t_idx = torch.arange(0, 4)
next_ids = torch.tensor([3, 1, 4, 1])
a = logprobs[t_idx, next_ids]
b = torch.gather(logprobs, dim=1, index=next_ids.unsqueeze(1)).squeeze(1)
print(a, b, torch.allclose(a, b))             # 预期 True
```

3. **需要观察的现象**：(a) 两组输出一致；(b) `log(softmax(big))` 中概率下溢为 0 的位置取 log 得 `-inf`（或 `nan`），而 `log_softmax` 给出有限的 `[-0, -1000, -2000]`；(c) `a` 与 `b` 逐项相等。
4. **预期结果**：`torch.allclose(a, b)` 返回 `True`。极端 logits 的具体失稳形态（`-inf` 还是 `nan`）依 dtype 与平台而异，**待本地验证**。
5. 第 (a) 步的输出应与主 notebook 5.5 节 `[-4.7109, -4.0442, -3.3776, -2.7109, -2.0442, -1.3776, -0.7109]` 一致（float32 下）。

#### 4.2.5 小练习与答案

**练习 1**：手算 `logits = [2.0, 0.0]` 的 `log_softmax`。

**答案**：`softmax = [e²/(e²+1), 1/(e²+1)] ≈ [0.881, 0.119]`；取 log 得 `[-0.127, -2.127]`。用 logsumexp 验证：`logsumexp = log(e² + e⁰) = 2 + log(1+e⁻²) ≈ 2.127`，`[2, 0] - 2.127 = [-0.127, -2.127]`。✓

**练习 2**：既然 `torch.prod` 和 `torch.sum` 都能比较大小，为什么还要换掉概率版？

**答案**：三个原因：(1) 概率连乘在长序列下必然下溢为 0，bfloat16 尤甚，届时所有句子分数同为 0 无法比较；(2) log 域每项可加、可拆、可平均（4.3 节正需要「对子区间求均值」），乘积域做不到；(3) log 域数值量级适合人读与浮点运算（`-16.6` vs `5.9e-08`）。

**练习 3**：把 `all_logprobas[t_idx, next_ids]` 改写成循环形式该怎么写？性能上有何差别？

**答案**：`next_token_logprobas = torch.stack([all_logprobas[t, nid] for t, nid in zip(t_idx.tolist(), next_ids.tolist())])`。语义相同，但 Python 循环逐元素触发张量下标、无法向量化；配对索引/`gather` 是一次内核调用。序列短时差别不明显，成百上千 token 时显著。

### 4.3 只评答案、取均值：`avg_logprob_answer` 与长度偏差

#### 4.3.1 概念说明

`avg_logprob_answer` 把打分器改造成可以直接插进后续流程的「回答分数」。相对 4.2 有两处关键改造（主 notebook 5.6 节明确总结了这两点）：

1. **只给答案区间打分，不给 prompt 打分**。prompt 是我们写的、人人都一样，给「Germany 的前几个 token」扣分毫无意义；能区分好坏回答的只有答案部分的似然。
2. **对逐 token 分数取均值而非求和**。这是为了消除**长度偏差**：每个 \(\log P \le 0\)，答案每多一个 token，求和分数就单调地更负一点——长回答哪怕同样流畅也会「输给」短回答。在本章的场景里（比较同一问题的不同候选回答、自我精炼前后两稿），长度差异动辄数百 token（主 notebook 5.2 节两个回答分别为 534 与 231 个 token），不求平均的话比较毫无意义。

代价也要说清：均值化**完全丢弃长度信息**——一个流畅的半截回答可能比完整回答分数更高。所以它度量的是「模型对这段文本的逐 token 置信度/流畅度」，**不是正确性**（Bridge 句再流畅也是错的）。这正是第 5 章同时提供规则打分器 `heuristic_score`（看 boxed、看长度）与 logprob 打分器的原因：两种信号互补。

#### 4.3.2 核心流程

```text
输入: prompt 文本, answer 文本
1. prompt_ids = encode(prompt);  answer_ids = encode(answer)   # 分开编码!
2. full_ids = tensor(prompt_ids + answer_ids), 长度 N
3. model 一次前向 → logprobs (N, vocab)
4. 切答案区间:
   start = len(prompt_ids) - 1          # 最后一个 prompt token 的位置
   end   = N - 1                        # 倒数第二个位置
   t_idx        = arange(start, end)    # 恰好 len(answer_ids) 个预测起点
   next_tokens  = full_ids[start+1 : end+1]   # 恰好是全部答案 token
5. 抽取 logprob 并取均值: mean(logprobs[t_idx, next_tokens]) → float
```

start 的取值是最容易错的地方，单独推导一下。设 prompt 有 \(p\) 个 token、答案有 \(a\) 个，拼接后 \(N = p + a\)。**第一个答案 token** \(x_{p+1}\) 是由**最后一个 prompt token** \(x_p\) 所在位置的分布预测的，所以打分必须从位置 \(p-1\)（0 起下标下 `len(prompt_ids) - 1`）开始；打到位置 \(N-2\)（预测最后一个 token \(x_N\)）。区间 \([p-1, N-2]\) 共 \(N-1-(p-1) = a\) 个位置——**恰好每个答案 token 贡献一项 logprob**，不多不少。被预测 token 切片 `full_ids[start+1 : end+1] = full_ids[p : N]` 正是答案的全部 token。

\[ \text{score} = \frac{1}{a} \sum_{t=p-1}^{N-2} \log P\big(x_{t+1} \mid x_{1:t}, W\big) \]

主 notebook 5.6 节先手工推导再封装：对 `prompt="What is the capital of Germany?"`（7 个 token）、`answer=" The capital of Germany is Berlin."`（7 个 token），先调用 `calc_next_token_logprobas(prompt+answer, show=False)` 得到 13 项，取**最后 7 项**（`next_token_logprobas[-7:]`）求均值得 `-0.2041`——随后 `avg_logprob_answer` 的输出与之一致，证明切片正确。

#### 4.3.3 源码精读

函数位于 [reasoning_from_scratch/ch05.py:L99-L121](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L99-L121)：

```python
@torch.inference_mode()
def avg_logprob_answer(model, tokenizer, prompt, answer, device="cpu"):

    # Encode prompt and answer tokens separately to get the prompt length later
    prompt_ids = tokenizer.encode(prompt)
    answer_ids = tokenizer.encode(answer)
    full_ids = torch.tensor(prompt_ids + answer_ids, device=device)
```

- **分开编码是全部切片逻辑的地基**：只有分别拿到 `prompt_ids` 与 `answer_ids`，才知道答案从哪里开始。注意「先拼接再整体编码」不可行——BPE 分词结果依赖上下文，分开编码再拼接与整段编码的 token 序列**不保证一致**，但本函数自洽地使用同一套 `prompt_ids + answer_ids` 做前向与切片，边界因此是精确的。

```python
    # Same as in calc_next_token_logprobas before
    logits = model(full_ids.unsqueeze(0)).squeeze(0)
    logprobs = torch.log_softmax(logits, dim=-1)

    # Index range for positions corresponding to answer tokens
    start = len(prompt_ids) - 1
    end = full_ids.shape[0] - 1

    # Same as before, except for using start and end
    t_idx = torch.arange(start, end, device=device)
    next_tokens = full_ids[start + 1 : end + 1]
    next_token_logps = logprobs[t_idx, next_tokens]

    # Average over the answer token scores
    return torch.mean(next_token_logps).item()
```

- 与 4.2 的差别全部集中在两行索引上（注释 "Same as before, except for using start and end"）：`arange(0, T-1)` 换成 `arange(start, end)`，`token_ids[1:]` 换成 `full_ids[start+1 : end+1]`。
- **返回 `.item()`**：包版本把 0 维张量转成 Python float 返回（[L121](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L121)）；而 notebook 5.6 节版本直接 `return torch.mean(next_token_logps)`（输出形如 `tensor(-0.2041, dtype=torch.bfloat16)`）。对照 notebook 学习时别被这个表面差异迷惑——数值相同，只是返回类型不同。float 更适合存进字典、写日志、做比较。
- `device="cpu"` 是默认参数（另两个函数的 `device` 是必传位置参数）。

单元测试 [tests/test_ch05.py:L58-L81](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch05.py#L58-L81) 用两个假对象把这个函数的数学行为锁死了，**不需要下载任何权重**：

```python
def test_avg_logprob_answer_uses_answer_token_logprobs():
    tokenizer = DummyTokenizer(
        mapping={
            "prompt": [1, 2],
            "answer": [3, 4],
        }
    )
    base_logits = [0.0, 0.1, 0.2, 0.3, 0.4]
    model = DummyLogitModel(base_logits)

    expected_logprobs = torch.log_softmax(
        model.base_logits, dim=-1
    )
    expected = torch.mean(expected_logprobs[[3, 4]]).item()

    out = ch05.avg_logprob_answer(
        model=model, tokenizer=tokenizer,
        prompt="prompt", answer="answer", device="cpu",
    )
    assert math.isclose(out, expected, rel_tol=1e-6)
```

假模型的构造在 [tests/test_ch05.py:L23-L30](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch05.py#L23-L30)：`DummyLogitModel.__call__` 把同一行 5 维 logits 复制到每个位置。于是期望值手工可算：`start = 2-1 = 1`、`end = 3`，抽出的两项分别是 `log_softmax(base)[3]` 与 `log_softmax(base)[4]`（每行都相同）。这个测试同时验证了「切片起点是 `len(prompt_ids)-1`」与「配对索引」两件事——若 start 误写为 `len(prompt_ids)`，抽到的将是 `[4]` 一项之外再加错位，断言立刻失败。

最后看它在下游的用法。主 notebook 5.8 节用 `functools.partial` 把它包装成可插拔的 `score_fn`：

```python
avg_logprob_score = partial(
    avg_logprob_answer,
    model=model, tokenizer=tokenizer, device=device
)
```

随后传入 `self_refinement_loop(score_fn=avg_logprob_score, ...)`，精炼循环每轮用它给旧稿与新稿打分、择优接受（该循环在 [reasoning_from_scratch/ch05.py:L148-L268](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L148-L268)，是 u6-l3 的主角）。notebook 5.4 节还点明了设计取舍：这个分数本可以在第 4 章生成函数里顺带算出（更高效），但**单独做成函数是为了可插拔**——与 `heuristic_score` 同一接口，随换随用。

#### 4.3.4 代码实践（本讲主实践）

1. **实践目标**：对同一段 prompt+answer 亲眼看到「逐 token logprob → 求和 → 均值」三个量；再通过截断实验体会长度偏差与均值化的含义。
2. **操作步骤**：
   - 前置：按 [u1-l2](u1-l2-setup-and-first-run.md) 装好环境并下载 base 权重（`download_qwen3_small(kind="base")`，或由 `load_model_and_tokenizer` 自动触发）。
   - 新建脚本（**示例代码**），放在你的工作目录（不要写进仓库源码）：

```python
# 示例代码：u6-l1 主实践
import torch
from reasoning_from_scratch.ch03 import load_model_and_tokenizer
from reasoning_from_scratch.ch05 import (
    calc_next_token_logprobas, avg_logprob_answer
)

device = torch.device("cpu")  # 与 notebook 5.2 节一致，便于对照
model, tokenizer = load_model_and_tokenizer(
    which_model="base", device=device, use_compile=False
)

prompt = "What is the capital of Germany?"
answer = " The capital of Germany is Berlin."

# ① 逐 token logprob 与求和（show=False 拿返回值）
per_token, total = calc_next_token_logprobas(
    model, tokenizer, prompt + answer, device, show=False
)
print("逐 token logprob:", [round(v, 4) for v in per_token.tolist()])
print("求和（联合 log-prob）:", total.item())

# ② 答案区间均值
n_ans = len(tokenizer.encode(answer))
print("答案 token 数:", n_ans)
print("手工版: 对 per_token 后 %d 项取均值 = %.4f"
      % (n_ans, per_token[-n_ans:].mean().item()))
avg_full = avg_logprob_answer(model, tokenizer, prompt, answer, device)
print("avg_logprob_answer(完整答案) =", avg_full)

# ③ 截断一半答案，重算
half = tokenizer.decode(tokenizer.encode(answer)[: n_ans // 2])
avg_half = avg_logprob_answer(model, tokenizer, prompt, half, device)
print("截断后的 answer 文本:", repr(half))
print("avg_logprob_answer(半截答案) =", avg_half)
print("求和版（半截）:", per_token[-(n_ans // 2):].sum().item())
```

   - 运行：`python your_script.py`（CPU 可跑，分钟级）。
3. **需要观察的现象**：
   - ① 的输出应与 notebook 5.6 节一致：13 项 logprobs，其中 prompt 部分（前 6 项）含有 `-8.31` 这样的大负值，而答案部分（后 7 项）大多接近 0——答案复读了一遍问题，模型对它极度确定。
   - ② 手工版均值与 `avg_logprob_answer` 输出一致（notebook 参考值 `-0.2041`，bfloat16/CPU）。
   - ③ 对比三个数：半截答案的**均值**分数 vs 完整答案的均值分数，方向如何？同时看**求和**版：半截的求和一定**高于**（更接近 0）完整版吗？不，求和只会因项数变少而「少负一些」——本例中答案各项都接近 0，两个版本差异都不大；换一个答案（如 `" It is probably Paris."`）再截断，差异会更明显。
4. **预期结果**：手工版均值 = `avg_logprob_answer`（两者切片逻辑相同）；求和版满足「项数越少、数值越不负」的单调关系；均值版**没有**这种单调关系——方向取决于被截掉的那半段平均 logprob 是高还是低。这正是本实践要解释的：**求和分数混入了长度效应（越长越低），均值分数剔除了长度效应但也无视完整性**。参考数值（Berlin 例）：完整答案均值 ≈ `-0.204`；半截答案的具体数值**待本地验证**。
   - 加做一步（可选）：把 answer 换成 `" The capital of Germany is Bridge."`，均值应显著更负（notebook 5.6 节参考值 `-3.89`），验证「区间均值仍能识别荒谬 token」。
5. 无权重时的替代路线：运行 `pytest tests/test_ch05.py::test_avg_logprob_answer_uses_answer_token_logprobs -v`（秒级完成），然后参照 4.3.3 的讲解手算 `expected`，核对 `log_softmax([0.0,0.1,0.2,0.3,0.4])[[3,4]]` 的均值。

#### 4.3.5 小练习与答案

**练习 1**：prompt 编码后 7 个 token、答案 7 个 token。写出 `start`、`end`、`t_idx` 的取值范围与参与均值的项数。

**答案**：`start = 7-1 = 6`，`end = 14-1 = 13`，`t_idx = arange(6, 13) = [6..12]`（7 个位置），`next_tokens = full_ids[7:14]`（答案全部 7 个 token）。参与均值 7 项——每个答案 token 恰好一项。

**练习 2**：如果把 `start = len(prompt_ids) - 1` 误写成 `start = len(prompt_ids)`，后果是什么？

**答案**：第一个答案 token \(x_{p+1}\) 的 logprob 被漏掉（它由位置 \(p-1\) 预测），同时区间右端不变时整体少一项、错位一项——分数变成「从第二个答案 token 开始的均值」，边界处系统性偏差。`tests/test_ch05.py` 的期望值对这种错误敏感，断言会失败。

**练习 3**：自我精炼场景中，候选 A 是 500 token 的完整正确解答，候选 B 是 50 token 的流畅但错误解答。`avg_logprob_answer` 与求和版 `calc_next_token_logprobas` 各自会倾向谁？

**答案**：求和版天然偏向 B：500 项负数相加几乎必然更负，与内容质量无关（长度偏差）。均值版剔除了长度效应，只比逐 token 平均置信度——若 B 流畅而 A 包含多处模型不确定的推理步骤，均值版也可能偏向 B。所以第 5 章把 logprob 均值分数与 `heuristic_score`（检查 boxed、奖励简洁）并列作为可插拔打分器，两种信号互补使用。

## 5. 综合实践

**任务：把「打分器三连」跑成一条链，并让它变成可插拔的 `score_fn`。**

综合本讲全部内容（概率 → logprob → 答案区间均值 → 可插拔接口），分两级完成：

**A 级（无权重，CPU 秒级，必做）**：

1. 仿照 [tests/test_ch05.py:L12-L30](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch05.py#L12-L30) 自定义 `DummyTokenizer`（映射 `"Q"`→`[1,2,3]`、`"A"`→`[4,5,6,7]`）与 `DummyLogitModel`（任取 8 维基准 logits、逐位置复制）。
2. 手算期望分数：`mean(log_softmax(base)[[4,5,6,7]])`（注意：答案 4 个 token，从 `log_softmax` 的第 4、5、6、7 列取——想清楚为什么是这四列、与 `start=2` 的关系）。
3. 调用 `avg_logprob_answer` 断言一致（**示例代码**）：

```python
import math, torch
import reasoning_from_scratch.ch05 as ch05
# ...（DummyTokenizer / DummyLogitModel 定义同测试文件）
expected = torch.log_softmax(torch.tensor(base_logits), dim=-1)[[4,5,6,7]].mean().item()
out = ch05.avg_logprob_answer(model, tok, prompt="Q", answer="A", device="cpu")
assert math.isclose(out, expected, rel_tol=1e-6)
```

4. 再把你的 Dummy 映射改成 prompt 2 个 token、answer 4 个 token，重算一遍——确保切片公式你已亲手推过两轮。

**B 级（需 base 权重，选做）**：

1. 运行 4.3.4 的主实践脚本，记录完整答案 / 半截答案 / Bridge 答案三个均值分数。
2. 用 `functools.partial` 把 `avg_logprob_answer` 包装成 `score_fn`，对一个数学问题的两条手写答案打分并排序，体会「打分器即裁判」；这正是 u6-l3 中 `self_refinement_loop(score_fn=...)` 的接入口（对照 notebook 5.8 节用法）。

**验收标准**：能不假思索地回答——「一次前向为什么够」「start 为什么减一」「求和为什么惩罚长答案」「均值为什么仍会偏爱流畅的错答」。

## 6. 本讲小结

- **打分与生成是同一分布的两种用法**：生成只消费末位分布并采样；打分（teacher forcing）一次前向后用**每个位置**的分布核对真实 next token，因果掩码保证了各位置分布的合法性。
- **错位配对索引是通用手法**：`all_logprobas[t_idx, next_ids]`（`t_idx=[0..T-2]`，`next_ids=token_ids[1:]`）与 `torch.gather` 语义等价，一次抽出全部真实转移的 logprob。
- **log 域三重好处**：联合概率由连乘变为求和（\(\log\prod P = \sum\log P\)）、避免 bfloat16 下溢（`5.9e-08` → `-16.6`）、每项贡献可拆解查看；实现上必须用 `log_softmax` 而非 `log(softmax(x))`。
- **`avg_logprob_answer` 的两处改造**：切片 `arange(len(prompt_ids)-1, N-1)` 使每个答案 token 恰贡献一项 logprob；取均值剔除长度偏差——但代价是丢失长度/完整性信息，它度量的是流畅置信度而非正确性。
- **包版本与 notebook 版本的两处小差异**：`calc_next_token_probas` 包版本无 `show` 参数只打印；`avg_logprob_answer` 包版本返回 `.item()` 的 float。对照 notebook 输出时注意换算。
- **可插拔设计**：打分器刻意与生成分离（尽管生成时可顺带算出），与 `heuristic_score` 共享 `score_fn` 接口，供 Best-of-N 择优与自我精炼循环随换随用。

## 7. 下一步学习建议

- **下一讲 [u6-l2（Best-of-N 与启发式打分）](u6-l2-best-of-n-and-heuristic-score.md)**：精读 [ch05.py:L14-L43](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L14-L43) 的 `heuristic_score`——另一种不看概率、只看「有没有 boxed、可不可提取、够不够简洁」的规则打分器，以及 Best-of-N 如何用打分（而非 u5-l4 的投票）从 N 条采样中选优。读完可以回答：「logprob 均值 vs 规则分数，各自会偏爱什么样的回答？」
- **u6-l3（自我精炼循环）**：本讲的 `avg_logprob_answer` 经 `partial` 包装后正是 `self_refinement_loop` 的默认裁判，去看「批评 → 修改 → 打分择优接受」的完整闭环。
- **延伸阅读**：第 6 章主 notebook（ch06_main.ipynb）开头回顾了 `avg_logprob_answer`，随后在 rollout 采样基础上把它演进出 `sequence_logprob`——「生成时顺带记录 logprob」的高效版本，即本讲提到的那个「本可以在生成函数里算」的思路在强化学习阶段的落地，预告单元七的 GRPO 损失。
