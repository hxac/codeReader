# 生成损失与模型评估

> 对应第 5 章 5.1 节：Evaluating generative text models
> 讲义 id：`u5-l1`，依赖 `u4-l4`（简单自回归文本生成）

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚语言模型的训练目标「下一个 token 预测」为什么需要一个**可数值化的损失**，以及为什么这个损失是**交叉熵**。
2. 手推一遍交叉熵损失的来历：从「模型给目标 token 分配的概率」出发，经对数、求平均、取负，得到深度学习里要**最小化**的损失。
3. 读懂并解释 `calc_loss_batch` / `calc_loss_loader` 这一对工具函数，特别是 `logits.flatten(0, 1)` 这个把 batch 维和 token 维合并的「展平技巧」。
4. 理解 `evaluate_model` 如何用 `model.eval()` + `torch.no_grad()` 关闭训练副作用来干净地评估训练/验证损失。
5. 把损失换算成**困惑度（Perplexity）**，并能解释「为什么未训练模型的初始困惑度会接近词表大小」。

本讲只讲「怎么衡量模型好坏」，**不讲**怎么优化（训练循环 `train_model_simple` 是下一讲 `u5-l2` 的内容）。可以说，本讲是把 `u4-l4` 那个能跑、但只产出乱码的模型，变成一个「可以用一个标量分数来打分」的模型。

## 2. 前置知识

- **下一个 token 预测**：语言模型每一步都在做「给定前文，预测下一个 token」。复习 `u2-l3`：训练样本成对出现，`input` 是一段上下文，`target` 是它整体**右移一位**的序列。
- **softmax**：把任意实数向量（logits）变成一组和为 1 的概率分布。复习 `u3-l1`。
- **logits 与 `out_head`**：`GPTModel` 最后那层线性层（无偏置）输出形状为 `(batch, num_tokens, vocab_size)` 的 logits，每个位置一个长度为词表大小 \(V=50257\) 的向量（复习 `u4-l3`）。
- **PyTorch 训练范式雏形**：知道 `loss.backward()` 需要 `loss` 是个标量张量、且带梯度即可。本讲只算损失，还不调 `backward`。
- **`nn.Embedding` 与查表**：复习 `u2-l4`，理解 token ID 是词表里的整数下标——这一点在本讲很关键，因为「目标 token ID」正好就是 logits 向量里我们想**最大化**的那个下标。

一个直觉：评估语言模型好坏，本质就是问「模型给『正确答案』分了多少概率」。分得越多（越接近 1）越好；分得越少（越接近 0）越差。本讲所有公式都是在把这个直觉「打磨」成一个可以反向求导的标量。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的部分 |
|------|------|----------------|
| [ch05/01_main-chapter-code/gpt_train.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py) | 本章「汇总脚本」，把 notebook 里的训练/评估代码整理成可直接运行的 `.py` | `text_to_token_ids` / `token_ids_to_text` / `calc_loss_batch` / `calc_loss_loader` / `evaluate_model`，以及 `main()` 里的数据划分与设备选择 |
| [ch05/01_main-chapter-code/ch05.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/ch05.ipynb) | 正文逐行演进的 notebook | 5.1.2 节手推交叉熵与困惑度；5.1.3 节构造 train/val loader 并对未训练模型算损失 |
| [ch04/01_main-chapter-code/previous_chapters.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/previous_chapters.py) | 承接前几章的汇总模块 | 提供 `GPTModel` / `create_dataloader_v1` / `generate_text_simple`，被 `gpt_train.py` 顶部 `import` |

> 关于 `gpt_train.py` 的复用模式：它在第 14 行写了 `from previous_chapters import GPTModel, create_dataloader_v1, generate_text_simple`——这是 `u1-l3` 讲过的「依赖模块型」汇总脚本，必须和 `previous_chapters.py` 放在同一目录才能跑起来。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 交叉熵损失**：从直觉到公式，手推 next-token 交叉熵损失。
- **4.2 `calc_loss_batch` 与 `calc_loss_loader`**：把公式落成一个 batch / 整个 loader 的损失函数。
- **4.3 `evaluate_model`**：在训练循环里被周期性调用的「干净评估」入口。
- **4.4 困惑度（Perplexity）**：把损失翻译成「有效词表大小」这个更直观的量。

### 4.1 交叉熵损失：衡量「下一个 token」预测的好坏

#### 4.1.1 概念说明

`u4-l4` 里我们用未训练的模型生成文本，得到一堆乱码（如 `Every effort moves you rentingetic wasnم refres RexMeCHicular stren`）。肉眼一看就知道「不好」，但训练需要一个**标量分数**来告诉优化器「现在到底有多差」，这样才能用梯度下降一步步减小它。

语言模型的天然目标是：**最大化模型给「正确下一个 token」分配的概率**。设模型在某个位置给词表里每个 token 输出的概率为 \(p_1, p_2, \dots, p_V\)，而该位置真正的下一个 token 是第 \(t\) 个，那么这一步的「得分」就是 \(p_t\) —— 模型给正确答案分到的概率。

- \(p_t\) 越接近 1 越好（模型很有把握且猜对了）；
- \(p_t\) 越接近 0 越差（模型几乎没给正确答案留概率）。

为什么用**对数**？因为在数学优化里，最大化 \(\log p_t\) 比直接最大化 \(p_t\) 更稳定也更易求导（乘法变加法、避免下溢）。这是机器学习里的标准约定。

为什么最后要**取负**并变成**最小化**？因为深度学习社区习惯「最小化损失」。于是「最大化平均对数概率」就等价地写成「最小化平均负对数概率」，后者就是大名鼎鼎的**交叉熵损失（cross-entropy loss）**。

#### 4.1.2 核心流程

notebook 5.1.2 节用一组小例子把上面这条链路完整演示了一遍，流程是：

```
inputs (token IDs)  ──model──▶  logits        形状 (batch, num_tokens, V)
                                   │ softmax(dim=-1)
                                   ▼
                                probas         每个位置一组概率分布
                                   │ 在「目标位置」取出目标 token 对应的概率
                                   ▼
                            target_probas      模型给正确答案分的概率（很小）
                                   │ torch.log
                                   ▼
                             log_probas        都是负数
                                   │ mean
                                   ▼
                          avg_log_probas       一个标量（如 -10.79）
                                   │ × -1
                                   ▼
                          neg_avg_log_probas   交叉熵损失（如 10.79）
```

关键认知：**目标 token ID 本身就是 logits 向量里要最大化的那个下标**。所以「取出模型给正确答案的概率」其实就是高级索引 `probas[位置, 目标ID]`。

#### 4.1.3 源码精读

notebook 用 `inputs` / `targets` 两个张量演示：注意 `targets` 就是 `inputs` **右移一位**（复习 `u2-l3` 滑动窗口）——这正是「下一个 token 预测」的体现：

```python
# 示例代码（来自 ch05.ipynb 5.1.2 节，为讲解做了精简）
inputs  = torch.tensor([[16833, 3626, 6100],   # "every effort moves",
                        [40,    1107, 588]])   # "I really like"
targets = torch.tensor([[3626, 6100, 345  ],   # " effort moves you",
                        [1107,  588, 11311]])  # " really like chocolate"

with torch.no_grad():
    logits = model(inputs)              # (2, 3, 50257)
probas = torch.softmax(logits, dim=-1)  # 概率分布

# 取出模型给「正确下一个 token」分配的概率（高级索引）
target_probas_1 = probas[0, [0, 1, 2], targets[0]]   # tensor([7.45e-05, 3.11e-05, 1.16e-05])
```

可以看到，未训练模型给正确答案的概率在 \(10^{-5}\) 量级——非常小，所以损失会很大。接着 notebook 一步步算：

```python
# 示例代码（来自 ch05.ipynb 5.1.2 节）
log_probas = torch.log(torch.cat((target_probas_1, target_probas_2)))  # 全是负数
avg_log_probas = torch.mean(log_probas)        # tensor(-10.7940)
neg_avg_log_probas = avg_log_probas * -1       # tensor(10.7940)  ← 这就是交叉熵损失
```

> 上面这段是「示例代码」，目的是让你看清交叉熵损失的内部结构。注意：`neg_avg_log_probas = avg_log_probas * -1` 这一步对应公式里的「取负」——**这正是从「最大化对数似然」翻转为「最小化损失」的那一下**。

而 PyTorch 把上面这一切（softmax + log + 取目标位置 + 求平均 + 取负）打包成了一个函数，它内部会数值稳定地一次性算完，不需要你手动 `softmax` 再 `log`：

```python
# 示例代码（来自 ch05.ipynb 5.1.2 节）
loss = torch.nn.functional.cross_entropy(logits_flat, targets_flat)  # tensor(10.7940)
```

用数学语言写，对于一个 batch 里所有 \(N\) 个待预测位置，交叉熵损失为：

\[
\mathcal{L}_{\text{CE}} = -\frac{1}{N}\sum_{n=1}^{N} \log p_{t^{(n)}}
\]

其中 \(t^{(n)}\) 是第 \(n\) 个位置的真实目标 token，\(p_{t^{(n)}}\) 是模型给它的概率。可以看到这和 notebook 手算的「平均负对数概率」完全一致。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `cross_entropy` 内部确实等价于「softmax → 取目标概率 → log → 平均 → 取负」。

**操作步骤**（在 `ch05/01_main-chapter-code/` 目录下开一个 Python 或 notebook）：

```python
# 示例代码：验证 cross_entropy 等价于手算的负对数概率
import torch
torch.manual_seed(123)

# 构造一个 6 类的小「词表」、3 个待预测位置，避免 50257 维太大
logits = torch.randn(3, 6)                 # (num_positions, fake_vocab)
targets = torch.tensor([0, 3, 5])          # 每个位置的正确答案下标

# 方法 A：手算「平均负对数概率」
probas = torch.softmax(logits, dim=-1)
target_probas = probas[range(3), targets]  # 取出每个位置目标 token 的概率
manual_loss = -torch.log(target_probas).mean()

# 方法 B：直接用 PyTorch 的 cross_entropy（cross_entropy 已经内部做了 softmax）
auto_loss = torch.nn.functional.cross_entropy(logits, targets)

print("手算损失：", manual_loss.item())
print("API 损失：", auto_loss.item())
```

**需要观察的现象**：两个数值应当**完全相等**（或仅差一个极小的浮点误差）。

**预期结果**：`手算损失` 与 `API 损失` 几乎一致，验证了 `cross_entropy` 就是「平均负对数概率」。

**待本地验证**：具体的随机数值取决于种子与运行环境，重点是**两者相等**这一关系，而非具体数字。

#### 4.1.5 小练习与答案

**练习 1**：为什么我们最大化的是 \(\log p_t\) 而不是 \(p_t\) 本身？两者单调性不是一样吗？

**参考答案**：单调性确实一致，最大化两者会得到同样的「最优解方向」。但用对数有两大好处：(1) 多个位置的联合概率是连乘 \(\prod p_{t^{(n)}}\)，取对数后变成连加 \(\sum \log p_{t^{(n)}}\)，求导和数值计算都更稳定；(2) 当 \(p_t\) 很小（如 \(10^{-5}\)）时连乘极易下溢为 0，而对数把它们变成 -11 左右的可表示实数。

**练习 2**：把 `neg_avg_log_probas = avg_log_probas * -1` 里的 `* -1` 去掉会怎样？

**参考答案**：你会得到「平均对数概率」（一个负数，如 -10.79）。此时它仍能衡量好坏（越大越接近 0 越好），但与深度学习「最小化损失」的约定相反——优化器若直接对它做梯度下降，反而会把模型往**更差**的方向推。取负是为了让「好」对应「小」，与 `optimizer.step()` 做**最小化**保持一致。

---

### 4.2 `calc_loss_batch` 与 `calc_loss_loader`：损失计算函数

#### 4.2.1 概念说明

4.1 里我们手算了一个小例子的损失。真实训练时，数据来自 `DataLoader`，每个 batch 的张量形状是 `(batch_size, num_tokens)`，模型输出是 `(batch_size, num_tokens, vocab_size)`。我们需要两个函数：

- **`calc_loss_batch`**：算**一个 batch** 的交叉熵损失（返回带梯度的标量张量，供 `backward` 用）。
- **`calc_loss_loader`**：遍历整个 loader，把每个 batch 的损失累加再求平均，得到**该数据集上的平均损失**。

两者是「单 batch」与「整批」的关系。注意一个微妙区别：`calc_loss_batch` 返回的是**张量**（保留计算图），因为它要喂给 `backward()`；而 `calc_loss_loader` 用 `.item()` 把每个 batch 损失**摘成普通 Python float** 再累加，因为它只用于**汇报**，不需要梯度。

#### 4.2.2 核心流程

`calc_loss_batch`：

```
input_batch, target_batch  (来自 DataLoader，都是 (batch, num_tokens) 的 token ID)
        │ .to(device)        ← 把数据搬到和模型相同的设备
        ▼
model(input_batch)  →  logits  (batch, num_tokens, vocab_size)
        │ flatten(0, 1) 把「batch 维」和「token 维」合并；targets.flatten() 同步展平
        ▼
cross_entropy(logits_flat, targets_flat)  →  标量 loss（带梯度）
```

`calc_loss_loader`：

```
对 loader 里的前 num_batches 个 batch：
    loss = calc_loss_batch(...)      # 算单 batch 损失
    total_loss += loss.item()        # .item() 摘成 float 累加（断开计算图）
return total_loss / num_batches      # 平均损失
```

#### 4.2.3 源码精读

先看单 batch 的实现 [ch05/01_main-chapter-code/gpt_train.py:28-32](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L28-L32)：

```python
def calc_loss_batch(input_batch, target_batch, model, device):
    input_batch, target_batch = input_batch.to(device), target_batch.to(device)
    logits = model(input_batch)
    loss = torch.nn.functional.cross_entropy(logits.flatten(0, 1), target_batch.flatten())
    return loss
```

逐行解读：

- 第 29 行把输入和目标都搬到 `device`。这是因为模型可能在 GPU 上，数据必须在**同一设备**才能参与同一张量运算（复习 `u1-l2` 提到的设备问题）。
- 第 30 行前向，得到 `(batch, num_tokens, vocab_size)` 的 logits。
- 第 31 行是**本讲最关键的一行**：`logits.flatten(0, 1)` 把第 0 维（batch）和第 1 维（num_tokens）**合并**成一个维度，`targets.flatten()` 同步展平。详见下面的「展平技巧」。

> **展平技巧**：PyTorch 的 `cross_entropy` 期望 `input` 形状为 `(N, C)`、`target` 形状为 `(N,)`，其中 `C` 是类别数（这里是词表大小）。但我们的 logits 是三维 `(batch, num_tokens, C)`。`flatten(0, 1)` 把前两维压成一维：`(2, 3, 50257)` → `(6, 50257)`，对应的 `targets` 从 `(2, 3)` → `(6,)`。这样就把「batch 里的 2 个句子 × 每句 3 个位置 = 6 个待预测位置」当作 6 个独立的分类问题一次性算出平均损失。这正是 4.1 公式里 \(N\) 的来源。

再看整批累加的实现 [ch05/01_main-chapter-code/gpt_train.py:35-49](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L35-L49)：

```python
def calc_loss_loader(data_loader, model, device, num_batches=None):
    total_loss = 0.
    if len(data_loader) == 0:
        return float("nan")
    elif num_batches is None:
        num_batches = len(data_loader)
    else:
        num_batches = min(num_batches, len(data_loader))
    for i, (input_batch, target_batch) in enumerate(data_loader):
        if i < num_batches:
            loss = calc_loss_batch(input_batch, target_batch, model, device)
            total_loss += loss.item()
        else:
            break
    return total_loss / num_batches
```

要点：

- 第 37-38 行：空 loader 直接返回 `nan`，避免除零。
- 第 39-42 行：`num_batches` 默认是整个 loader 的 batch 数；若用户传了更小的值，则用 `min` 截断——这服务于训练循环里「每隔几步只抽几个 batch 估一下损失」的提速需求（见 4.3）。
- 第 46 行 `loss.item()`：**这一步断开了计算图**，把带梯度的张量变成普通 float。因为累加损失只是为了汇报，不需要为它反传梯度；而且保留计算图会越积越大、爆显存。

#### 4.2.4 代码实践

**实践目标**：直观感受 `flatten(0, 1)` 做了什么，并确认它与「逐位置算损失再平均」等价。

**操作步骤**：

```python
# 示例代码：理解 flatten(0, 1) 的形状变换
import torch
batch, num_tokens, vocab = 2, 3, 6
logits = torch.randn(batch, num_tokens, vocab)
targets = torch.randint(0, vocab, (batch, num_tokens))

print("logits:", logits.shape, "→ flatten(0,1):", logits.flatten(0, 1).shape)
print("targets:", targets.shape, "→ flatten():", targets.flatten().shape)

# 一次性算
loss_all = torch.nn.functional.cross_entropy(logits.flatten(0, 1), targets.flatten())

# 拆成「逐位置」算再平均，应当与上面相等
losses = []
for b in range(batch):
    for t in range(num_tokens):
        losses.append(
            torch.nn.functional.cross_entropy(logits[b, t], targets[b, t])
        )
loss_manual = torch.stack(losses).mean()

print("一次性损失：", loss_all.item())
print("逐位置平均：", loss_manual.item())
```

**需要观察的现象**：`flatten(0,1)` 把 `(2,3,6)` 变成 `(6,6)`；两个损失值相等。

**预期结果**：两者数值一致，证明 `flatten(0,1)` 只是「把多个位置的预测摊平成一堆独立分类问题」，并不改变损失语义。

**待本地验证**：具体数值随机，重点看两者相等。

#### 4.2.5 小练习与答案

**练习 1**：`calc_loss_loader` 里为什么用 `loss.item()` 而不是直接 `total_loss += loss`？

**参考答案**：`loss` 是带计算图（`requires_grad`）的张量。若直接累加，`total_loss` 会持有整条计算图，循环越多图越大，最终既慢又可能爆显存；而且评估时我们根本不需要对历史 batch 反传梯度。`.item()` 把它摘成一个无梯度的 Python float，干净且省内存。

**练习 2**：如果 `num_batches` 传了 `100`，但 loader 实际只有 `9` 个 batch，会算错吗？

**参考答案**：不会。第 42 行 `num_batches = min(num_batches, len(data_loader))` 会把它截成 `9`，最后用 `9` 作分母求平均。这正是 `min` 防御的作用——避免「实际只累加了 9 个却被除以 100」的错误。

---

### 4.3 `evaluate_model`：周期性「干净评估」入口

#### 4.3.1 概念说明

训练时每隔几步就想知道「模型现在在训练集和验证集上分别表现如何」。这要求评估过程**不能污染训练状态**：评估时不要 dropout、不要记录梯度。`evaluate_model` 就是把这个「干净的、抽样的」评估封装成一个函数，在 `train_model_simple`（下一讲 `u5-l2`）里被周期性调用。

它做三件事：(1) 切到评估模式；(2) 在 `no_grad` 下分别算训练/验证损失；(3) 切回训练模式。

#### 4.3.2 核心流程

```
evaluate_model(model, train_loader, val_loader, device, eval_iter):
    model.eval()                              # 关闭 dropout 等
    with torch.no_grad():                     # 不建计算图、不算梯度（省算力）
        train_loss = calc_loss_loader(train_loader, model, device,
                                      num_batches=eval_iter)   # 只抽 eval_iter 个 batch
        val_loss   = calc_loss_loader(val_loader,   model, device,
                                      num_batches=eval_iter)
    model.train()                             # 恢复训练模式
    return train_loss, val_loss
```

#### 4.3.3 源码精读

完整实现在 [ch05/01_main-chapter-code/gpt_train.py:52-58](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L52-L58)：

```python
def evaluate_model(model, train_loader, val_loader, device, eval_iter):
    model.eval()
    with torch.no_grad():
        train_loss = calc_loss_loader(train_loader, model, device, num_batches=eval_iter)
        val_loss = calc_loss_loader(val_loader, model, device, num_batches=eval_iter)
    model.train()
    return train_loss, val_loss
```

三个细节值得记住：

- 第 53 行 `model.eval()`：把模型设为评估模式，最直接的效果是**关闭 dropout**（复习 `u4-l2`，`drop_shortcut` 与各处 dropout 都依赖训练模式），让评估结果稳定可复现。
- 第 54 行 `with torch.no_grad():`：在上下文里**不构建计算图**，既省内存又省算力——评估只需要前向的数值，不需要梯度。
- 第 55-56 行传入 `num_batches=eval_iter`：这就是 4.2 里 `calc_loss_loader` 那个 `min` 截断的用武之地。`gpt_train.py` 的 `main()` 调用时设的是 `eval_iter=1`（见 [gpt_train.py:198](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L198)），即每次评估只抽 1 个 batch——用少量样本快速估个趋势，不必遍历整个 loader，从而不拖慢训练。
- 第 57 行 `model.train()`：评估完**切回训练模式**，确保下一次真实训练步里 dropout 等重新生效。这种「成对出现」的 `eval()`/`train()` 是好习惯。

> 为什么 `evaluate_model` 返回 `train_loss` 也要抽样？因为在 `train_model_simple` 里它被高频调用（每 `eval_freq` 步一次），若每次都遍历整个 loader，评估开销会盖过训练开销。抽样评估是「用精度换速度」的工程取舍。

#### 4.3.4 代码实践

**实践目标**：观察 `model.eval()` 与 `torch.no_grad()` 对 dropout 输出的影响，理解「干净评估」的必要性。

**操作步骤**：

```python
# 示例代码：感受 eval() / no_grad() 的作用
import torch
import torch.nn as nn

torch.manual_seed(123)
# 一个带 dropout 的小模型：同输入下，train 模式输出会随机变，eval 模式输出稳定
m = nn.Sequential(nn.Linear(4, 4), nn.Dropout(0.5))
x = torch.ones(1, 4)

m.train()
print("train 模式两次前向（应有差异）：",
      torch.allclose(m(x), m(x)))   # 期望 False

m.eval()
with torch.no_grad():
    print("eval + no_grad 两次前向（应相同）：",
          torch.allclose(m(x), m(x)))   # 期望 True
```

**需要观察的现象**：训练模式下两次前向因 dropout 而不同；评估模式下两次前向完全一致。

**预期结果**：第一处打印 `False`，第二处打印 `True`。这就解释了为什么 `evaluate_model` 必须 `model.eval()`——否则连「同一模型同一输入」都得不到稳定损失。

**待本地验证**：dropout 在不同操作系统/PyTorch 编译下行为可能略有差异（notebook 5.2 节也特别提到了这一点），但「eval 更稳定」的结论不变。

#### 4.3.5 小练习与答案

**练习 1**：如果删掉 `evaluate_model` 里的 `model.train()`（第 57 行），训练会出什么问题？

**参考答案**：评估后模型一直停留在 `eval` 模式，后续真实训练步里 **dropout 不会再生效**（它只在 `train` 模式下随机置零）。这等于悄悄改了模型结构，正则化消失，可能导致训练动态与预期不符。所以 `eval()` 与 `train()` 必须成对出现。

**练习 2**：`model.eval()` 和 `with torch.no_grad():` 是不是同一回事？

**参考答案**：不是，两者作用层面不同。`model.eval()` 改变**层的行为**（关 dropout、让 BatchNorm/LayerNorm 用统计量而非 batch 估计——本项目用的是自定义 LayerNorm 不受影响，但 dropout 受影响）；`torch.no_grad()` 只影响**自动微分**（不建图、不算梯度），不改变层行为。评估时两个都要：前者保证前向确定性，后者省去梯度开销。

---

### 4.4 困惑度（Perplexity）：把损失翻译成「有效词表大小」

#### 4.4.1 概念说明

交叉熵损失（比如 10.79）虽然能比大小，但它「不带单位」，很难直觉理解「10.79 到底算好还是差」。**困惑度（Perplexity, PPL）**就是对损失做一次指数，把它变成一个更有解释力的量：

\[
\mathrm{PPL} = \exp(\mathcal{L}_{\text{CE}})
\]

为什么指数有用？把 4.1 的损失公式代入：

\[
\mathrm{PPL} = \exp\!\left(-\frac{1}{N}\sum_{n=1}^{N}\log p_{t^{(n)}}\right)
            = \left(\prod_{n=1}^{N} \frac{1}{p_{t^{(n)}}}\right)^{1/N}
\]

也就是说，困惑度是「模型给正确答案分配的概率的**倒数**的几何平均」。它可以理解为：**模型在每个位置上，平均在多少个候选 token 之间「拿不准」**——即「有效词表大小」。困惑度越低，说明模型越「确信」，预测越准。

#### 4.4.2 核心流程

一个特别有启发性的极端情形：**未训练模型**。刚初始化的模型对词表里每个 token 给出的概率大致**均匀**，即 \(p_t \approx 1/V\)（\(V\) 是词表大小）。代入损失公式：

\[
\mathcal{L}_{\text{CE}} = -\log(1/V) = \log V
\qquad\Longrightarrow\qquad
\mathrm{PPL} = \exp(\log V) = V
\]

这就是本讲最核心的直觉结论：

> **未训练模型的初始损失 \(\approx \log(\text{词表大小})\)，初始困惑度 \(\approx\) 词表大小。**

对 GPT-2，\(V=50257\)，\(\log(50257) \approx 10.825\)。所以你会看到 notebook 里未训练模型的训练/验证损失都在 \(10.98\) 左右——略高于 \(10.825\)，困惑度约 \(5.9\times10^4\)，与 \(50257\) 同量级。这不是巧合，而是「均匀分布」的直接推论：模型还完全没学到东西，等于在 5 万多个 token 里**瞎蒙**，自然困惑度≈词表大小。

随着训练进行，模型逐渐把概率集中到正确 token 上，损失下降，困惑度也随之下降——一个训练得不错的模型困惑度会远小于词表大小。

#### 4.4.3 源码精读

notebook 5.1.2 节在算完 `cross_entropy` 后，紧接着用一行 `torch.exp` 得到困惑度（这里引用 notebook 的相关演示段落）：

```python
# 示例代码（来自 ch05.ipynb 5.1.2 节）
perplexity = torch.exp(loss)   # tensor(48725.8203)
```

对应的解释是：困惑度 ≈ 48,725，意思是模型在每个位置上「相当于在约 4.8 万个 token 之间犹豫」——几乎等于整个 50,257 的词表，印证了「未训练 = 均匀瞎蒙」。

而在 5.1.3 节，notebook 对**真正构造好的 train/val loader** 上的未训练模型算损失，输出为：

```
Training loss: 10.987583054436577
Validation loss: 10.98110580444336
```

这两个数都在 \(\log(50257)\approx 10.825\) 附近一点点上方。换成困惑度：

\[
\exp(10.987) \approx 5.9\times 10^{4},\qquad \exp(10.981) \approx 5.9\times 10^{4}
\]

都和词表大小 \(50257\) 同量级——完全符合「初始困惑度 ≈ 词表大小」的预言。

> 数据划分背景：notebook（与 `gpt_train.py` 的 `main()`，[gpt_train.py:167-188](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L167-L188)）把 `the-verdict.txt` 按 `train_ratio = 0.90` 切成训练/验证两段，分别用 `create_dataloader_v1` 造 loader。`main()` 里设的是 `context_length=256`（见 [gpt_train.py:209](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L209)），整个文本只有约 5145 个 token，所以 loader 很小（训练 9 个 batch、验证 1 个 batch），目的是让训练能在笔记本上几分钟跑完。

#### 4.4.4 代码实践

**实践目标**：复现「未训练模型的初始困惑度 ≈ 词表大小」这一结论，并理解其含义。

**操作步骤**（在 `ch05/01_main-chapter-code/` 目录下，需有 `previous_chapters.py`）：

```python
# 示例代码：对未训练模型计算 train/val 损失并换算困惑度
import torch, tiktoken
from previous_chapters import GPTModel, create_dataloader_v1

GPT_CONFIG_124M = {
    "vocab_size": 50257, "context_length": 256, "emb_dim": 768,
    "n_heads": 12, "n_layers": 12, "drop_rate": 0.1, "qkv_bias": False
}
torch.manual_seed(123)
model = GPTModel(GPT_CONFIG_124M)
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
model.to(device)

# 读数据并按 0.90 划分（与 gpt_train.py main() 一致）
with open("the-verdict.txt", "r", encoding="utf-8") as f:   # 若无此文件见下方说明
    text_data = f.read()
split_idx = int(0.90 * len(text_data))
train_loader = create_dataloader_v1(text_data[:split_idx], batch_size=2,
        max_length=256, stride=256, drop_last=True, shuffle=True)
val_loader   = create_dataloader_v1(text_data[split_idx:], batch_size=2,
        max_length=256, stride=256, drop_last=False, shuffle=False)

# 复用本讲学到的函数（直接从 gpt_train.py 导入最省事）
from gpt_train import calc_loss_loader
with torch.no_grad():
    train_loss = calc_loss_loader(train_loader, model, device)
    val_loss   = calc_loss_loader(val_loader, model, device)

print(f"训练损失: {train_loss:.4f}  → 困惑度: {torch.exp(torch.tensor(train_loss)):.1f}")
print(f"验证损失: {val_loss:.4f}  → 困惑度: {torch.exp(torch.tensor(val_loss)):.1f}")
print(f"词表大小 V = 50257, log(V) = {torch.log(torch.tensor(50257.0)):.4f}")
```

> 说明：`the-verdict.txt` 位于 `ch02/01_main-chapter-code/the-verdict.txt`，可复制过来；或参考 `gpt_train.py` 的 `main()` 用 `requests` 从仓库 URL 下载（[gpt_train.py:140-151](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L140-L151)）。

**需要观察的现象**：训练/验证损失都在 \(10.8\sim 11.0\) 之间，非常接近 \(\log(50257)\approx 10.825\)；换算出的困惑度都在 \(5\sim 6\) 万量级，接近 \(50257\)。

**预期结果**：损失 \(\approx 10.98\)，困惑度 \(\approx 5.9\times10^4\)，与词表大小 \(50257\) 同量级。

**含义解读**：这正好说明未训练模型把概率**近乎均匀**地撒在 5 万多个 token 上，等于在整张词表上「瞎蒙」。训练的目标，就是把这个困惑度从「≈ 词表大小」一路压到远小于词表大小。

**待本地验证**：不同设备/PyTorch 版本下，因 `nn.Dropout` 行为差异，损失可能略有浮动（notebook 5.2 节有专门提示），但「接近 \(\log V\)、困惑度接近 \(V\)」的结论稳定成立。

#### 4.4.5 小练习与答案

**练习 1**：假设某个位置模型给正确 token 的概率是 \(0.5\)，该位置的损失是多少？困惑度是多少？

**参考答案**：损失 \(-\log(0.5) = \log 2 \approx 0.693\)；困惑度 \(\exp(0.693) = 2\)。直观含义：模型在这个位置「相当于在 2 个候选之间犹豫」——很有把握。

**练习 2**：为什么说困惑度是「有效词表大小」，而不是「错误率」？

**参考答案**：困惑度衡量的是模型**概率分布的集中程度**（对正确答案的几何平均概率的倒数），而不是「猜没猜对」。一个困惑度为 2 的模型，并不代表「50% 猜对」，而是「平均每个位置它把概率集中得像在 2 个选项里挑」。它和交叉熵损失一一对应（指数关系），信息量完全相同，只是刻度更直觉。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「从零评估未训练 GPT」的小任务。

**任务**：仿照 `gpt_train.py` 的 `main()`（但不真的训练），只完成「造数据 → 建模型 → 算损失 → 算困惑度 → 解读」这一评估闭环，并回答一个问题：**为什么初始困惑度会接近词表大小？**

**操作步骤**：

1. 在 `ch05/01_main-chapter-code/` 目录下，确保 `previous_chapters.py` 存在（否则按 `u1-l3` 安装 `llms_from_scratch` 包替代）。
2. 准备 `the-verdict.txt`（从 `ch02/01_main-chapter-code/` 复制，或用 `gpt_train.py` 的下载逻辑）。
3. 写一个脚本，依次：用 `GPT_CONFIG_124M`（`context_length=256`）构建未训练 `GPTModel`；按 `train_ratio=0.90` 切分并构造 train/val loader；调用 `calc_loss_loader` 算两者损失；用 `torch.exp` 换算困惑度。
4. 打印：训练损失、验证损失、各自困惑度，以及 \(\log(50257)\) 和 \(50257\) 两个参照值。

**需要观察与回答**：

- 两个损失是否都接近 \(10.825\)？两个困惑度是否都接近 \(50257\)？
- 用一两句话解释「初始困惑度 ≈ 词表大小」的原因（提示：未训练模型输出近似均匀分布，\(p_t\approx 1/V\)，于是 \(\mathcal{L}\approx\log V\)、\(\mathrm{PPL}\approx V\)）。

**预期结果**：损失约 \(10.9\sim 11.0\)，困惑度约 \(5\sim 6\) 万，与词表大小同量级。这一结果验证了评估流水线正确，也说明模型「还没学到东西」——为下一讲 `u5-l2` 的训练循环（把这些损失压下去）做好了铺垫。

**待本地验证**：具体数值受运行环境影响，重点在于量级与解释，而非精确数字。

## 6. 本讲小结

- 语言模型的评估目标是「模型给**正确下一个 token** 分了多少概率」，取对数、求平均、再取负，就得到要**最小化**的**交叉熵损失** \(\mathcal{L}_{\text{CE}} = -\frac{1}{N}\sum \log p_{t^{(n)}}\)。
- `calc_loss_batch` 用 `logits.flatten(0, 1)` 把 `(batch, num_tokens, vocab)` 压成 `(N, vocab)`，交给 `cross_entropy` 一次算出平均损失；`calc_loss_loader` 遍历 loader 用 `.item()` 累加求均，供汇报用（不带梯度）。
- `evaluate_model` 用 `model.eval()` + `torch.no_grad()` 营造「干净评估」环境（关 dropout、不算梯度），并通过 `num_batches=eval_iter` 抽样评估以提速，最后 `model.train()` 切回训练态。
- **困惑度** \(\mathrm{PPL}=\exp(\mathcal{L}_{\text{CE}})\) 可理解为「有效词表大小」；**未训练模型**输出近似均匀，故初始损失 \(\approx\log V\)、初始困惑度 \(\approx V\)（GPT-2 约 50,257），这正是「还没学到东西」的数学签名。
- 本讲只解决「怎么打分」，尚未「怎么优化」——把损失 `backward()` 再 `optimizer.step()` 的训练循环，是下一讲 `u5-l2` 的主题。

## 7. 下一步学习建议

- **下一讲 `u5-l2`（训练循环 `train_model_simple`）**：看 `optimizer.zero_grad()` → `calc_loss_batch` → `loss.backward()` → `optimizer.step()` 这套标准四步如何把本讲的损失一步步压下去，并配合 `evaluate_model` 周期打点、`plot_losses` 画损失曲线、观察过拟合。
- **继续阅读的源码**：
  - [gpt_train.py:75-109](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L75-L109) 的 `train_model_simple`，体会本讲的 `evaluate_model` 是如何被嵌进训练循环的。
  - [gpt_train.py:131-202](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L131-L202) 的 `main()`，看设备选择、数据划分、优化器（AdamW）如何与本讲函数串成一个可运行脚本。
- **进阶方向**：若想给本讲的「干净评估」加更高级的训练技巧（学习率 warmup、余弦衰减、梯度裁剪），可跳到附录 D，对应后续 `u8-l2`。若想了解「损失还能怎么用于对齐」，可关注 `u11-l2` 的 DPO 损失——它正是建立在「对数概率差」这一本讲基础概念之上。
