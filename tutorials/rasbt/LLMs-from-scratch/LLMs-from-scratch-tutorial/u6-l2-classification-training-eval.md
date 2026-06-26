# 分类训练与评估（最后一个 token）

## 1. 本讲目标

上一讲（u6-l1）我们做完了两件事：把 SMS 垃圾短信数据集切成定长张量，并把 GPT 的 5 万维语言模型输出头换成了 2 维分类头、冻结了主干。本讲接着解决最后一个问题：**怎么训练这个分类头，怎么知道它学得好不好？**

学完本讲，你应当能够：

1. 说清楚**为什么分类时只取最后一个 token 的 logits**，这与第 5 章预训练时"每个 token 都参与"有何不同。
2. 读懂并实现 `calc_accuracy_loader`，知道为什么用"准确率"评估、却不能直接拿它去优化。
3. 读懂 `train_classifier_simple` 这一完整分类微调循环，并能动手跑出训练/验证损失与准确率曲线。
4. 在测试集上给出最终的分类准确率结论。

---

## 2. 前置知识

在进入本讲前，请确认你已经理解下面这些概念（它们都来自前面的讲义）：

- **因果掩码（causal mask）**：GPT 用上三角掩码让每个位置只能"看到"自己及之前的 token（见 u3-l2）。这是本讲"最后一个 token 信息最全"这一结论的直接来源。
- **logits 与 softmax/argmax**：模型输出的是未归一化的分数 logits，过 softmax 得概率，取 argmax 得预测类别（见 u5-l1、u5-l3）。注意 argmax 对 logits 本身做即可，softmax 是**可省略**的，因为 softmax 单调不减。
- **交叉熵损失**：把"预测对不对"变成可最小化的标量损失，\(\mathcal{L}_{\text{CE}}=-\frac{1}{N}\sum\log p_{\text{目标}}\)（见 u5-l1）。
- **PyTorch 训练四件套**：`zero_grad` → 前向算 loss → `backward` 反传 → `step` 更新（见 u5-l2）。
- **冻结主干 + 换头 + 选择性解冻**：本讲训练的对象，正是 u6-l1 这样改造出来的模型（只解冻最后一个 Transformer 块、`final_norm` 和新的 2 维 `out_head`）。

一个关键对比请记在心里：**第 5 章预训练时，损失对序列里每个 token 都计算**（输入与输出右移一位一一对应）；**本讲做分类时，我们只关心序列里某一个 token 的输出**——这就是"最后一个 token"。

---

## 3. 本讲源码地图

本讲涉及两个文件，它们是"汇总脚本 + 正文 notebook"的关系（约定见 u1-l3）：

| 文件 | 作用 |
| --- | --- |
| [ch06/01_main-chapter-code/gpt_class_finetune.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py) | 第 6 章的**自包含汇总脚本**。本讲用到的 `calc_accuracy_loader`、`calc_loss_batch`、`evaluate_model`、`train_classifier_simple` 全部收集在这里，行号清晰，是本讲的主要引用对象。 |
| [ch06/01_main-chapter-code/ch06.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/ch06.ipynb) | 第 6 章正文 notebook。它和上面的 `.py` 代码一致，但多了循序渐进的文字讲解与本讲引用的训练日志（6.5–6.7 节）。 |

本讲对应 notebook 的 **6.6 节（计算分类损失与准确率）** 与 **6.7 节（在有监督数据上微调）**。

---

## 4. 核心概念与源码讲解

### 4.1 用最后一个 token 的 logits 做分类

#### 4.1.1 概念说明

u6-l1 把 `out_head` 从 `Linear(768, 50257)` 换成了 `Linear(768, 2)`。于是模型对一条长度为 `T` 的短信，会输出形状为 `(1, T, 2)` 的张量——**每个 token 位置都产生一个 2 维向量**（spam / ham 的分数）。

问题来了：一条短信只需要**一个**分类结果，那我们该取 `T` 个位置里的哪一个？

答案是：**取最后一个 token 的输出**。原因来自因果掩码：在因果注意力下，第 `i` 个位置只能聚合第 `1..i` 个 token 的信息；位置越靠后，能看到的前文越多。因此**最后一个 token 是唯一一个"看过整条短信"的位置**，它的 2 维 logits 最适合代表"这条短信整体的类别"。

> notebook 6.5 节的原话：最后一个 token 在所有 token 中包含的信息最多，因为它是唯一包含所有其他 token 信息的 token，所以我们特别关注它来做垃圾短信分类。

这一点也呼应了第 5 章生成时的逻辑（见 u4-l4）：自回归生成时我们也是取**末位 logits** 来预测下一个词——因为因果掩码下末位已看到全部上下文。分类只是把"预测下一个词"换成了"预测整段文本的类别"，取的位置是一样的。

#### 4.1.2 核心流程

从原始短信到一个分类预测，流程是：

```
短信文本
  │  (tiktoken 分词 + 填充到 max_length, 见 u6-l1)
  ▼
token ID 序列 (b, T)
  │  GPTModel 前向
  ▼
logits (b, T, 2)         ← 每个位置都有一个 2 维分数
  │  [:, -1, :]           ← 只取最后一个位置
  ▼
last_token_logits (b, 2) ← 整条短信的类别分数
  │  argmax(dim=-1)       ← 预测类别（softmax 可省略）
  ▼
predicted_label (b,)
```

训练时则把"取最后一个 token"这一步和交叉熵损失缝在一起：

\[\mathcal{L}_{\text{CE}} = -\frac{1}{N}\sum_{n=1}^{N}\log\,\mathrm{softmax}(z_n)_{\,y_n}\]

其中 \(z_n\) 是第 \(n\) 条样本**最后一个 token** 的 2 维 logits，\(y_n\in\{0,1\}\) 是该短信的真实标签。

> **关键对比（和第 5 章预训练的区别）**：第 5 章的 `calc_loss_batch` 用 `logits.flatten(0, 1)` 把 `(b, T, 词表)` 压成 `(N, 词表)`，让**每个位置**都贡献一个 next-token 预测损失；本讲的 `calc_loss_batch` 则只取 `[:, -1, :]`，让**每个样本只贡献一个分类损失**。损失的张量形状从 `(b*T, 词表)` 变成了 `(b, 2)`。

#### 4.1.3 源码精读

分类损失函数 [calc_loss_batch](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L149-L153) 是本讲最核心的一小段：

```python
def calc_loss_batch(input_batch, target_batch, model, device):
    input_batch, target_batch = input_batch.to(device), target_batch.to(device)
    logits = model(input_batch)[:, -1, :]  # Logits of last output token
    loss = torch.nn.functional.cross_entropy(logits, target_batch)
    return loss
```

逐行说明：

- 第 1 行把输入与标签搬到设备上。
- 第 2 行 `model(input_batch)` 得 `(b, T, 2)`，`[:, -1, :]` 切出最后一个位置 → `(b, 2)`。注释 `# Logits of last output token` 一语点题。
- 第 3 行 `cross_entropy(logits, target_batch)`：`logits` 形状 `(b, 2)`、`target_batch` 形状 `(b,)`，正好满足 `cross_entropy` 要求的 `(N, C)` + `(N,)` 约定，一次算出带梯度的标量损失。

注意这里和第 5 章版本**只有一行不同**——把"全部 token"换成了"最后一个 token"。这个一行之差，正是从"语言模型"切换到"分类器"的关键。

#### 4.1.4 代码实践

**实践目标**：亲手验证"模型对一条短信只取最后一个 token 的 logits 就能得到一个类别预测"。

**操作步骤**（假设你已按 u6-l1 加载好预训练 GPT-2 small 并替换了 `out_head`、把模型搬到 `device`）：

```python
# 示例代码：取最后一个 token 的 logits 并预测类别
import torch

inputs = tokenizer.encode("Do you have time")
inputs = torch.tensor(inputs).unsqueeze(0)          # (1, T)

with torch.no_grad():
    outputs = model(inputs)                          # (1, T, 2)

print("全部输出形状:", outputs.shape)                # torch.Size([1, 4, 2])
last_token_logits = outputs[:, -1, :]               # (1, 2)
print("最后一个 token 的 logits:", last_token_logits)

# softmax 可省略：argmax 对 logits 直接做即可
label = torch.argmax(last_token_logits, dim=-1).item()
print("预测类别:", label)                            # 0=ham, 1=spam
```

**需要观察的现象**：

- `outputs` 形状是 `(1, T, 2)`，即每个 token 位置都有一对分数。
- 取 `[:, -1, :]` 后形状塌缩成 `(1, 2)`，只剩最后一个位置。
- `torch.argmax(outputs[:, -1, :], dim=-1)` 与 `torch.argmax(torch.softmax(outputs[:, -1, :], dim=-1), dim=-1)` 结果**完全相同**——印证 softmax 可省略。

**预期结果**：由于替换的 `out_head` 是新初始化的随机权重（还没训练），预测类别基本是随机的，这很正常。notebook 6.5 节给出的参考输出是 `outputs[:, -1, :] = tensor([[-3.5983, 3.9902]])`，`argmax` 得 `1`。**具体数值待本地验证**（取决于随机种子与权重）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `[:, -1, :]` 改成 `[:, 0, :]`（取第一个 token），会带来什么问题？

> **参考答案**：第一个 token 在因果掩码下只能看到自己，完全没看过短信的其余内容，用它做分类等于让模型在"只读了一个词"的情况下判断整条短信是否垃圾，信息严重不足，准确率会接近随机。

**练习 2**：为什么预测时可以跳过 softmax 直接对 logits 做 argmax？

> **参考答案**：softmax 是单调不减函数，不会改变元素之间的大小顺序；argmax 只关心"谁最大"，所以对 logits 和对 softmax(logits) 做 argmax 结果一致。预测阶段省掉 softmax 可以少一次计算。

---

### 4.2 分类准确率评估 calc_accuracy_loader

#### 4.2.1 概念说明

损失（交叉熵）是**可微的**，能用来反传梯度、更新权重；但人真正关心的指标是**准确率（accuracy）**——预测对了多少。准确率的定义很直白：

\[\text{Acc} = \frac{\#\text{预测正确的样本}}{\#\text{总样本}}\]

问题是：准确率依赖 `argmax`，而 `argmax` **不可微**（梯度几乎处处为 0）。所以我们不能"直接最小化准确率"去训练，只能**最小化交叉熵损失作为代理**，再用准确率来"汇报"模型到底学得怎么样。这就是"训练用损失、评估用准确率"的分工。

`calc_accuracy_loader` 就是那个"汇报"函数：遍历整个 `data_loader`，对每条样本取最后一个 token 的 logits → argmax 得预测 → 和真实标签比对 → 统计正确比例。

#### 4.2.2 核心流程

```
对 data_loader 中的每个 batch (input_batch, target_batch):
    logits = model(input_batch)[:, -1, :]      # 只取最后一个 token，(b, 2)
    predicted = argmax(logits, dim=-1)          # (b,)
    累加样本数  += b
    累加正确数  += (predicted == target_batch).sum()
return 正确数 / 样本数
```

两个工程细节值得注意：

1. **评估环境要干净**：开头 `model.eval()` 关闭 dropout；整个前向包在 `torch.no_grad()` 里，既省内存又不建计算图。
2. **可以只算一部分 batch 提速**：参数 `num_batches` 为 `None` 时算全部，否则只算前 `min(num_batches, len(loader))` 个 batch。训练中为了快速看趋势，常只抽几个 batch（如 `eval_iter=5`）；最终汇报时则跑全量。

#### 4.2.3 源码精读

[calc_accuracy_loader](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L126-L146) 完整实现：

```python
def calc_accuracy_loader(data_loader, model, device, num_batches=None):
    model.eval()
    correct_predictions, num_examples = 0, 0

    if num_batches is None:
        num_batches = len(data_loader)
    else:
        num_batches = min(num_batches, len(data_loader))
    for i, (input_batch, target_batch) in enumerate(data_loader):
        if i < num_batches:
            input_batch, target_batch = input_batch.to(device), target_batch.to(device)

            with torch.no_grad():
                logits = model(input_batch)[:, -1, :]  # Logits of last output token
            predicted_labels = torch.argmax(logits, dim=-1)

            num_examples += predicted_labels.shape[0]
            correct_predictions += (predicted_labels == target_batch).sum().item()
        else:
            break
    return correct_predictions / num_examples
```

要点对应：

- `model.eval()`（[第 127 行](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L127)）：关闭 dropout，保证评估可复现。
- `num_batches = min(...)`（[第 133 行](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L133)）：防止传入的 `num_batches` 超过 loader 实际批数。
- `[:, -1, :]`（[第 139 行](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L139)）：与损失函数里完全相同的"取最后一个 token"切片。
- `torch.argmax(logits, dim=-1)`（[第 140 行](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L140)）：直接对 logits 取最大者下标，省略 softmax。
- `(predicted_labels == target_batch).sum().item()`（[第 143 行](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L143)）：逐元素比对得布尔张量，`.sum()` 数正确个数，`.item()` 取回 Python 标量。

#### 4.2.4 代码实践

**实践目标**：在**还没训练**时先量一次准确率，建立一个"基线"，体会"随机初始化的分类头≈瞎猜"。

**操作步骤**（模型已按 u6-l1 换好头并搬到 `device`）：

```python
# 示例代码：训练前的基线准确率
torch.manual_seed(123)
train_acc = calc_accuracy_loader(train_loader, model, device, num_batches=10)
val_acc   = calc_accuracy_loader(val_loader,   model, device, num_batches=10)
test_acc  = calc_accuracy_loader(test_loader,  model, device, num_batches=10)
print(f"Train {train_acc*100:.2f}% | Val {val_acc*100:.2f}% | Test {test_acc*100:.2f}%")
```

**需要观察的现象**：

- 三项准确率都应在 **50% 上下**（二分类瞎猜的水平）。
- 训练/验证/测试三者数值接近，说明模型此刻对三个集合"一视同仁地差"。

**预期结果**：notebook 6.6 节给出的参考值约为 `训练 46.25% / 验证 45.00% / 测试 48.75%`，确实在 50% 附近。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `calc_accuracy_loader` 里要写 `model.eval()`，却**没有**在函数末尾写 `model.train()` 恢复？

> **参考答案**：这是一个设计取舍。`train_classifier_simple` 在训练循环内部每个 epoch 开头都会显式调用 `model.train()`（见 4.3.3），所以即使这里不恢复，进入下一轮训练也会被重新设回训练模式。相比之下，`evaluate_model`（见 4.3.3）会自己 `model.eval()` ... `model.train()` 成对出现，更严谨。本函数省略末尾恢复，是依赖外层循环来兜底。

**练习 2**：把 `num_batches` 设成 `None` 和设成一个比 `len(loader)` 还大的数，结果会有区别吗？

> **参考答案**：没有区别。`num_batches is None` 时取 `len(loader)`；传大数时被 `min(num_batches, len(loader))` 截断成 `len(loader)`。两者都等于"跑完全部 batch"。

---

### 4.3 分类训练循环 train_classifier_simple

#### 4.3.1 概念说明

有了"取最后一个 token 的交叉熵损失"和"准确率评估"，训练循环就是把它们串起来的胶水。本项目的 `train_classifier_simple` 和第 5 章预训练用的 `train_model_simple` **几乎一模一样**（见 u5-l2），只有两处针对分类的改动：

1. **跟踪的是"看过多少样本"而不是"看过多少 token"**：变量 `examples_seen` 累加的是 `input_batch.shape[0]`（样本数），因为分类关心的单位是"一条短信"，而非"一个 token"。
2. **每个 epoch 末打印准确率而不是生成一段样本文本**：预训练用"生成一段话"看模型说人话了没；分类用"算一次准确率"看模型分对了没。

其余骨架——`zero_grad` → `calc_loss_batch` → `backward` → `step`、按 `eval_freq` 周期性算损失、双层循环——完全沿用第 5 章。

#### 4.3.2 核心流程

```
初始化: train_losses, val_losses, train_accs, val_accs = [], ... ; examples_seen=0; global_step=-1
for epoch in range(num_epochs):
    model.train()
    for (input_batch, target_batch) in train_loader:
        optimizer.zero_grad()
        loss = calc_loss_batch(...)        # 取最后一个 token 的交叉熵
        loss.backward()
        optimizer.step()
        examples_seen += batch大小
        global_step += 1
        if global_step % eval_freq == 0:   # 周期性汇报损失
            (tl, vl) = evaluate_model(...)  # 内部用 calc_loss_loader 抽样
            追加到 train_losses / val_losses
    # 每个 epoch 末算准确率
    train_acc = calc_accuracy_loader(..., num_batches=eval_iter)
    val_acc   = calc_accuracy_loader(..., num_batches=eval_iter)
    追加到 train_accs / val_accs
return 四条曲线 + examples_seen
```

其中 `evaluate_model` 是从第 5 章原样搬来的"干净评估"封装：`model.eval()` + `torch.no_grad()` 包住 `calc_loss_loader`，算完再 `model.train()` 切回。

#### 4.3.3 源码精读

主循环 [train_classifier_simple](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L182-L217)：

```python
def train_classifier_simple(model, train_loader, val_loader, optimizer, device, num_epochs,
                            eval_freq, eval_iter):
    train_losses, val_losses, train_accs, val_accs = [], [], [], []
    examples_seen, global_step = 0, -1

    for epoch in range(num_epochs):
        model.train()  # Set model to training mode

        for input_batch, target_batch in train_loader:
            optimizer.zero_grad()
            loss = calc_loss_batch(input_batch, target_batch, model, device)
            loss.backward()
            optimizer.step()
            examples_seen += input_batch.shape[0]  # New: track examples instead of tokens
            global_step += 1

            if global_step % eval_freq == 0:
                train_loss, val_loss = evaluate_model(
                    model, train_loader, val_loader, device, eval_iter)
                train_losses.append(train_loss)
                val_losses.append(val_loss)
                print(f"Ep {epoch+1} (Step {global_step:06d}): "
                      f"Train loss {train_loss:.3f}, Val loss {val_loss:.3f}")

        train_accuracy = calc_accuracy_loader(train_loader, model, device, num_batches=eval_iter)
        val_accuracy = calc_accuracy_loader(val_loader, model, device, num_batches=eval_iter)
        print(f"Training accuracy: {train_accuracy*100:.2f}% | ", end="")
        print(f"Validation accuracy: {val_accuracy*100:.2f}%")
        train_accs.append(train_accuracy)
        val_accs.append(val_accuracy)

    return train_losses, val_losses, train_accs, val_accs, examples_seen
```

几个关键点对应到行号：

- `examples_seen += input_batch.shape[0]`（[第 197 行](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L197)）：注释 `# New: track examples instead of tokens` 明确标出这是相对第 5 章的改动。
- `if global_step % eval_freq == 0`（[第 201 行](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L201)）：周期性损失评估，`global_step` 从 `-1` 起是为了第一步（`global_step=0`）也触发一次评估。
- `calc_accuracy_loader(..., num_batches=eval_iter)`（[第 210–211 行](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L210-L211)）：每个 epoch 末只抽 `eval_iter` 个 batch 快速估准确率，是"趋势观察"而非"最终结论"。

配套的损失评估封装 [evaluate_model](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L173-L179)（与第 5 章完全相同）：

```python
def evaluate_model(model, train_loader, val_loader, device, eval_iter):
    model.eval()
    with torch.no_grad():
        train_loss = calc_loss_loader(train_loader, model, device, num_batches=eval_iter)
        val_loss = calc_loss_loader(val_loader, model, device, num_batches=eval_iter)
    model.train()
    return train_loss, val_loss
```

它内部调用的 `calc_loss_loader`（[第 156–170 行](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L156-L170)）也只是把 `calc_loss_batch` 在若干 batch 上求平均（用 `.item()` 摘成 float），逻辑与第 5 章一致。

最后看一下脚本 `__main__` 里**真正调用**这段训练的配置（[第 409–418 行](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L409-L418)）：

```python
optimizer = torch.optim.AdamW(model.parameters(), lr=5e-5, weight_decay=0.1)
num_epochs = 5
train_losses, val_losses, train_accs, val_accs, examples_seen = train_classifier_simple(
    model, train_loader, val_loader, optimizer, device,
    num_epochs=num_epochs, eval_freq=50, eval_iter=5,
)
```

超参要点：优化器 `AdamW`（与第 5 章同），学习率 `5e-5`（比预训练的 `4e-4` 小一个量级，微调要更小心），`weight_decay=0.1`，`num_epochs=5`，每 50 步汇报一次损失（`eval_freq=50`），每次评估只抽 5 个 batch（`eval_iter=5`）。

#### 4.3.4 代码实践

**实践目标**：跑通分类微调，观察损失下降与准确率上升，并在**测试集**上给出最终结论。

**操作步骤（最快路径，推荐）**：

仓库提供了 `gpt_class_finetune.py` 这个自包含脚本，并带一个 `--test_mode` 开关，用极小的随机 GPT 模型在 CPU 上跑通整个流程（仍需联网下载约几百 KB 的 SMS 数据集，但**不需要**下载 GPT-2 权重）：

```bash
cd ch04/../ch06/01_main-chapter-code   # 即 ch06/01_main-chapter-code
python gpt_class_finetune.py --test_mode
```

> 注意：`--test_mode` 用的是 `emb_dim=12, n_layers=1, n_heads=2` 的玩具模型（见脚本 [第 336–348 行](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L336-L348)），目的是验证"流程跑通"，准确率不会高。要看真实效果，去掉 `--test_mode`（需联网下载 gpt2-small 权重，建议有 GPU/MPS）。

**需要观察的现象**：

- 终端按 `eval_freq` 打印 `Ep X (Step Y): Train loss ..., Val loss ...`，损失应**逐步下降**。
- 每个 epoch 末打印 `Training accuracy: ...% | Validation accuracy: ...%`，准确率应**逐步上升**。

**预期结果**（去掉 `--test_mode` 的完整训练，取自 notebook 6.7 节的日志）：

```
Ep 1 (Step 000000): Train loss 2.153, Val loss 2.392
...
Training accuracy: 70.00% | Validation accuracy: 72.50%
...
Ep 5 (Step 000600): Train loss 0.083, Val loss 0.074
Training accuracy: 100.00% | Validation accuracy: 97.50%
```

训练完成后，跑**全量**测试集（注意这次 `num_batches` 留空，算全部）：

```python
# 示例代码：最终全量评估
test_accuracy = calc_accuracy_loader(test_loader, model, device)   # 不传 num_batches = 跑全量
print(f"Test accuracy: {test_accuracy*100:.2f}%")
```

notebook 6.7 节的全量结果约为 `训练 97.21% / 验证 97.32% / 测试 95.67%`。测试准确率比训练/验证略低，属轻微过拟合，可通过调大 `drop_rate` 或 `weight_decay` 缓解。**具体数值待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`train_classifier_simple` 里每个 epoch 末用 `num_batches=eval_iter` 估准确率，为什么最后还要再用**不传** `num_batches` 的方式重算一次？

> **参考答案**：训练中为了快，只抽 `eval_iter`（如 5）个 batch 估准确率，是"趋势观察"，有抽样噪声；训练结束后要给模型下定论，必须跑**全量**测试集，`num_batches=None` 即 `len(loader)`，得到稳定可靠的最终准确率。

**练习 2**：如果把学习率从 `5e-5` 直接调到第 5 章预训练用的 `4e-4`，可能出现什么问题？

> **参考答案**：微调时模型主干已经加载了有意义的预训练权重，学习率过大会把好不容易学到的表示"冲坏"（catastrophic update），损失可能剧烈震荡甚至发散。所以微调学习率通常比预训练小一个量级。

---

## 5. 综合实践

把本讲三块知识串起来，完成一次"从基线到结论"的完整分类微调：

1. **建立基线**：模型换好头、搬到设备后，先用 `calc_accuracy_loader` 在训练集抽 10 个 batch 量一次准确率，记录这个"瞎猜水平"的数（应接近 50%）。
2. **跑训练**：用 `AdamW(lr=5e-5, weight_decay=0.1)` 调 `train_classifier_simple` 训练 5 个 epoch（无 GPU 可用 `--test_mode` 走通流程），观察终端打印的损失下降与准确率上升。
3. **画曲线**：用脚本里的 `plot_values`（[第 220–237 行](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/gpt_class_finetune.py#L220-L237)）分别绘制损失曲线和准确率曲线，看训练/验证两条线是否贴近（贴近=没明显过拟合）。
4. **下结论**：训练结束后，对**全量**训练/验证/测试集调用 `calc_accuracy_loader`（不传 `num_batches`），报告最终测试准确率，并判断是否过拟合。

**验收标准**：能说清"训练前≈50% → 训练后≈95%+"这个跃升，并解释为什么用损失训练、用准确率汇报。

---

## 6. 本讲小结

- 分类时**只取最后一个 token 的 logits** `[:, -1, :]`：因果掩码下它是唯一看过整条文本的位置，是天然的"整句表示"。这与第 5 章生成时取末位 logits 是同一个道理。
- `calc_loss_batch` 与第 5 章版本**仅一行之差**：把"全部 token 的 `flatten(0,1)`"换成"最后一个 token 的 `[:, -1, :]`"，损失张量从 `(b*T, 词表)` 变为 `(b, 2)`。
- **准确率不可微**，不能直接用来优化；故训练用可微的交叉熵损失作代理，评估用 `argmax` 统计的准确率汇报——这是"训练用损失、评估用准确率"分工的根因。
- `calc_accuracy_loader` 在干净的 `eval()`+`no_grad()` 环境下遍历 loader，用 `num_batches` 控制是"抽样看趋势"还是"全量下结论"。
- `train_classifier_simple` 与第 5 章 `train_model_simple` 几乎相同，只改了两处：跟踪"样本数"而非"token 数"，每个 epoch 末打印准确率而非生成样本文本。
- 微调超参比预训练更保守：`lr=5e-5`（预训练 `4e-4` 的约 1/8）、`weight_decay=0.1`、5 个 epoch；全量测试准确率约 95%+，比训练/验证略低属轻微过拟合。

---

## 7. 下一步学习建议

至此，你已经能在一个预训练 GPT 上做**分类微调**并评估。接下来可以：

- **横向巩固**：阅读 [ch06/01_main-chapter-code/ch06.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch06/01_main-chapter-code/ch06.ipynb) 的 6.8 节，看 `classify_review` 如何把 `SpamDataset` 的预处理（分词、截断、填充）封装成对单条文本的推理函数，并用 `torch.save` 存盘复用。
- **下一章（第 7 章）**：分类微调只能预测固定类别；指令微调（instruction finetuning）则让模型学会"按指令回答"。你会发现 `custom_collate_fn` 用 `ignore_index=-100` 给 prompt 部分做**损失掩码**——它和本讲"只取最后一个 token"是同一思想的两种体现：都是**只对序列里我们关心的位置计算损失**。
- **进阶（附录 E）**：如果你觉得"解冻最后一块 + 换头"参数还是太多，附录 E 的 LoRA 会用低秩适配器把可训练参数再砍掉一两个数量级，是本讲"参数高效微调"的自然延伸。
