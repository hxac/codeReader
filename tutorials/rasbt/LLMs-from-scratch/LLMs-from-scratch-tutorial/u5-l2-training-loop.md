# 训练循环 train_model_simple

## 1. 本讲目标

上一讲（u5-l1）我们解决了「如何给语言模型打分」——用交叉熵损失把模型输出的好坏变成一个标量，并实现了 `calc_loss_loader` / `evaluate_model` 来度量训练损失与验证损失。但那时的模型仍然是只读乱码的「随机权重」状态，因为我们只评估、不更新。

本讲要把这个「分数」真正用起来，实现**完整的预训练循环** `train_model_simple`，让模型从随机初始化一步步学会在短篇故事《The Verdict》上预测下一个 token。学完后你应当能够：

- 说清标准 PyTorch 训练范式四件套 `optimizer.zero_grad() → 前向算 loss → loss.backward() → optimizer.step()` 各自的作用与不可省略的理由。
- 读懂 `train_model_simple` 的双层循环结构（外层 epoch、内层 batch），理解 `global_step`、`tokens_seen`、`eval_freq` 如何控制评估节奏。
- 理解训练循环里「数值评估（损失）」与「样本生成（看文本）」两条监控线如何互补。
- 用 `plot_losses` 画出双坐标轴的损失曲线，并从训练损失与验证损失的背离中**识别过拟合**。

## 2. 前置知识

本讲承接 u5-l1，假设你已掌握下列概念（若不熟可先回看）：

- **交叉熵损失**：把「下一个 token 预测」的好坏数值化为一个可最小化的标量（u5-l1）。
- **`calc_loss_batch` / `evaluate_model`**：前者对一个 batch 前向算出带梯度的损失，后者在 `model.eval()` + `torch.no_grad()` 下抽样求均，给出干净的评估损失（u5-l1）。
- **PyTorch 自动微分（autograd）**：在前向过程中，PyTorch 会自动记录计算图；调用 `loss.backward()` 后，它会沿计算图反向传播，把每个可学习参数的梯度填进参数的 `.grad` 属性（u8-l1 会系统讲，本讲只需会用）。
- **`GPTModel` 与 `create_dataloader_v1`**：模型与滑动窗口数据加载器，均来自前序章节的汇总文件 `previous_chapters.py`（u4-l3、u2-l3）。

本讲引入的新术语：**优化器（optimizer）**、**梯度清零（zero_grad）**、**反向传播（backward）**、**参数更新（step）**、**AdamW / 自适应学习率 / 解耦权重衰减（weight decay）**、**epoch / step / tokens seen**、**过拟合（overfitting）**。

## 3. 本讲源码地图

本讲全部核心代码集中在第 5 章的主代码目录，关键文件如下：

| 文件 | 作用 |
|------|------|
| [ch05/01_main-chapter-code/gpt_train.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py) | **本讲主角**。自包含的预训练脚本，把训练循环、评估、绘图、数据准备全串起来，末尾有可直接运行的 `main()` 入口。 |
| [ch05/01_main-chapter-code/ch05.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/ch05.ipynb) | 第 5 章正文 notebook，逐行演进 `train_model_simple`、`plot_losses`，并打印了 10 个 epoch 的真实训练输出（含过拟合讨论）。 |
| ch05/01_main-chapter-code/previous_chapters.py | 汇总器，提供 `GPTModel`、`create_dataloader_v1`、`generate_text_simple`（见 u1-l3 的汇总机制说明）。 |

> 提示：`gpt_train.py` 属于 u1-l3 介绍的「依赖模块」型 summary 脚本——开头第 14 行有 `from previous_chapters import ...`，因此运行它时必须与 `previous_chapters.py` 同目录。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先讲训练四件套（4.1），再用它拼出 `train_model_simple` 主循环（4.2），接着看循环里两条监控线（4.3），最后画曲线并判读过拟合（4.4）。

### 4.1 标准 PyTorch 训练范式：optimizer / zero_grad / backward / step

#### 4.1.1 概念说明

「训练」的本质，就是反复做一件事：**根据当前模型在数据上的错误程度（损失），沿着让损失下降的方向，一点点调整模型权重。** 这个过程在 PyTorch 里被固化成一个几乎万能的「四步仪式」，任何用 PyTorch 训练的神经网络（从线性回归到 GPT）都遵循它：

1. **清零梯度** `optimizer.zero_grad()`
2. **前向算损失** `loss = calc_loss_batch(...)`
3. **反向传播** `loss.backward()`
4. **更新权重** `optimizer.step()`

优化器（optimizer）是「按梯度调整权重」的策略。本项目用的是 **AdamW**，它是 Adam 的改进版，也是当下训练 LLM 的事实标准之一。相比最朴素的 SGD（所有参数共用一个学习率、直接沿负梯度走），AdamW 有两点关键不同：

- **自适应学习率**：为**每个参数**单独维护一份学习率。它用梯度的「一阶矩（均值）」和「二阶矩（未去均值方差）」的滑动估计，对频繁大幅更新的参数减小步长、对几乎不更新的参数加大步长。直觉上：它给每个参数「按需」分配步幅，所以对学习率超参不那么敏感。
- **解耦权重衰减（decoupled weight decay）**：在更新时额外把权重本身按比例缩小，等价于一种正则化，能抑制权重膨胀、缓解过拟合。

AdamW 单步更新可近似写成（\(g_t\) 为当前梯度，\(\eta\) 为学习率，\(\lambda\) 为权重衰减系数）：

\[
\theta_t = \theta_{t-1} - \eta\left( \frac{\hat{m}_t}{\sqrt{\hat{v}_t}+\varepsilon} + \lambda\,\theta_{t-1} \right)
\]

其中第一项 \(\hat{m}_t/(\sqrt{\hat{v}_t}+\varepsilon)\) 是自适应方向，第二项 \(\lambda\theta_{t-1}\) 就是「解耦」的权重衰减。本项目里 \(\lambda\) 即 `weight_decay`（设为 0.1）。

#### 4.1.2 核心流程

四步的执行顺序与各自职责：

```
每个 batch：
  ① optimizer.zero_grad()   # 把上一步留在 .grad 里的旧梯度清零
  ② loss = calc_loss_batch()# 前向：模型输出 logits → 交叉熵损失（autograd 自动建计算图）
  ③ loss.backward()         # 反向：沿计算图把 ∂loss/∂θ 算出，写进每个参数的 .grad
  ④ optimizer.step()        # 更新：优化器读 .grad，按 AdamW 公式调整每个 θ
```

**为什么必须先 `zero_grad()`？** 因为 PyTorch 默认「梯度累加」——调用 `backward()` 时，新算出的梯度会**叠加**到 `.grad` 已有值上，而不是覆盖。这一设计是为了方便实现「梯度累加（gradient accumulation）」这类把多个小 batch 梯度攒成一个大 batch 的技巧。但常规训练每个 batch 应只反映**当前 batch** 的梯度，所以每轮开头必须手动清零，否则第 N 个 batch 的梯度会混进前 N-1 个 batch 的残留。

#### 4.1.3 源码精读

优化器的创建在 `main()` 里（注意 `weight_decay=0.1` 即解耦权重衰减，`lr` 来自配置）：

[创建 AdamW 优化器 —— gpt_train.py:158-160](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L158-L160)

```python
optimizer = torch.optim.AdamW(
    model.parameters(), lr=settings["learning_rate"], weight_decay=settings["weight_decay"]
)
```

四步仪式本身嵌在 `train_model_simple` 的内层循环里（本讲 4.2 会整体看，这里先聚焦这四行）：

[训练四件套 —— gpt_train.py:87-90](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L87-L90)

```python
optimizer.zero_grad()  # Reset loss gradients from previous batch iteration
loss = calc_loss_batch(input_batch, target_batch, model, device)
loss.backward()        # Calculate loss gradients
optimizer.step()       # Update model weights using loss gradients
```

> 配套的训练超参在脚本末尾：`lr=5e-4`、`weight_decay=0.1`、`num_epochs=10`、`batch_size=2`（见 [gpt_train.py:217-222](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L217-L222)）。

#### 4.1.4 代码实践

1. **实践目标**：亲手体会「梯度累加」陷阱，理解 `zero_grad` 为何不可省。
2. **操作步骤**（纯 PyTorch 最小示例，不依赖本项目）：
   ```python
   import torch
   w = torch.tensor([1.0], requires_grad=True)
   for i in range(3):
       # 注释掉下一行，观察 w.grad 的变化
       # w.grad = None
       loss = (w - 3) ** 2     # 目标让 w 趋近 3
       loss.backward()
       print(i, "grad =", w.grad)   # 第一次运行前注释掉 w.grad=None
   ```
3. **需要观察的现象**：保留 `zero_grad`（用 `w.grad=None` 等效）时，三轮 `grad` 都是 `[-4]`；**去掉**清零时，`grad` 会变成 `[-4]`、`[-8]`、`[-12]` 逐轮累加。
4. **预期结果**：累加的梯度会让权重更新步长越来越大、训练发散——这就是 `train_model_simple` 每个 batch 都要先 `optimizer.zero_grad()` 的原因。
5. 运行结果「待本地验证」（取决于你的环境，但累加/不累加的对比是确定性的）。

#### 4.1.5 小练习与答案

**Q1**：把本项目优化器换成 `torch.optim.SGD(model.parameters(), lr=5e-4)`，训练还能收敛吗？为什么实践中不用 SGD 训练 LLM？

> **参考答案**：理论上可能勉强收敛，但 SGD 对所有参数用同一学习率、不归一化梯度幅度，在 124M 参数、深层 Transformer 上极易发散或收敛极慢。AdamW 的自适应学习率 + 权重衰减让大模型训练稳定得多，这也是业界几乎不用裸 SGD 训练 LLM 的原因。

**Q2**：如果只把 `optimizer.zero_grad()` 删掉（其余不变），最先出现的现象是什么？

> **参考答案**：梯度跨 batch 累加，等效学习率越跑越大，损失很快不降反升（训练发散 / loss 变成 NaN）。

---

### 4.2 train_model_simple 主循环结构

#### 4.2.1 概念说明

`train_model_simple` 是把上一节的四件套「打包」成一个可复用函数。它接收一个已建好的模型、训练/验证数据加载器、优化器、设备、以及一堆控制训练节奏的参数，跑完整个预训练后**返回三条历史曲线**（训练损失、验证损失、累计 token 数）供后续绘图。

它的结构是经典的「双层循环」：

- **外层**遍历 `num_epochs`：把整个训练集完整过一遍叫一个 epoch。过多次是为了让模型多次见到同一批数据（小数据集必需）。
- **内层**遍历 `train_loader` 的每个 batch：每个 batch 执行一次 4.1 的四件套。

函数名里的 `simple` 是有意的——它故意只实现最朴素的训练循环。notebook 里明确点出：若想加入学习率 warmup、余弦退火、梯度裁剪等进阶技巧，请参考附录 D（即本手册 u8-l2）。

#### 4.2.2 核心流程

```text
train_model_simple(model, train_loader, val_loader, optimizer, device,
                   num_epochs, eval_freq, eval_iter, start_context, tokenizer):

  初始化记录列表 train_losses / val_losses / track_tokens_seen
  tokens_seen = 0          # 累计处理过的 token 总数
  global_step = -1         # 跨 epoch 的 batch 计数器（从 -1 起，首个 batch 变 0）

  for epoch in range(num_epochs):          # 外层：逐轮 epoch
      model.train()                        # 开启 dropout（训练模式）
      for input_batch, target_batch in train_loader:   # 内层：逐 batch
          ① optimizer.zero_grad()
          ② loss = calc_loss_batch(...)
          ③ loss.backward()
          ④ optimizer.step()
          tokens_seen += input_batch.numel()   # 累计 token 数
          global_step += 1

          if global_step % eval_freq == 0:     # 每 eval_freq 个 batch 评估一次
              (train_loss, val_loss) = evaluate_model(...)   # 见 4.3
              记录三列表 + 打印一行进度

      generate_and_print_sample(...)          # 每个 epoch 末打印一段生成文本（见 4.3）

  return train_losses, val_losses, track_tokens_seen
```

几个值得注意的设计细节：

- **`global_step` 从 `-1` 起步**：内层循环一进来就 `+= 1`，所以第一个 batch 对应 `step=0`，使 `step % eval_freq == 0` 在第 0、5、10… 个 batch 触发评估。注意评估发生在 `optimizer.step()` **之后**，因此 `Ep 1 (Step 0)` 报告的损失，反映的是模型**已经过 1 个 batch 更新后**的状态。
- **`model.train()` 放在外层循环开头**：因为 4.3 的 `evaluate_model` 与 `generate_and_print_sample` 末尾都会调用 `model.train()` 之外的 `model.eval()`，在每个 epoch 开头再设回训练模式，确保 dropout 在该 epoch 始终开启。
- **`tokens_seen += input_batch.numel()`**：`numel()` 是元素总数 = `batch_size × num_tokens`。本项目 `batch_size=2`、`max_length=256`，故每个 batch 计 512 个 token；9 个 batch/epoch 共 4608，与 notebook 打印的 `Training tokens: 4608` 完全吻合——这是一处可以自验证的数值。
- **评估按 `step` 触发，而非按 `epoch`**：所以一条 epoch 里会评估多次（或零次），取决于 batch 数与 `eval_freq` 的关系。

#### 4.2.3 源码精读

[train_model_simple 主体 —— gpt_train.py:75-109](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L75-L109)

只看关键的「双循环 + 计数 + 评估调度」骨架（四件套已在 4.1 标注）：

```python
def train_model_simple(model, train_loader, val_loader, optimizer, device, num_epochs,
                       eval_freq, eval_iter, start_context, tokenizer):
    train_losses, val_losses, track_tokens_seen = [], [], []
    tokens_seen = 0
    global_step = -1

    for epoch in range(num_epochs):
        model.train()                       # 训练模式：dropout 开
        for input_batch, target_batch in train_loader:
            # ... 4.1 的四件套（zero_grad / loss / backward / step）...
            tokens_seen += input_batch.numel()
            global_step += 1

            if global_step % eval_freq == 0:           # 周期评估
                train_loss, val_loss = evaluate_model(
                    model, train_loader, val_loader, device, eval_iter)
                train_losses.append(train_loss)
                val_losses.append(val_loss)
                track_tokens_seen.append(tokens_seen)
                print(f"Ep {epoch+1} (Step {global_step:06d}): "
                      f"Train loss {train_loss:.3f}, Val loss {val_loss:.3f}")

        generate_and_print_sample(model, tokenizer, device, start_context)  # 每 epoch 末采样

    return train_losses, val_losses, track_tokens_seen
```

`main()` 里真正的调用现场（注意 `eval_freq=5`、`eval_iter=1`）：

[在 main 中调用训练循环 —— gpt_train.py:196-200](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L196-L200)

```python
train_losses, val_losses, tokens_seen = train_model_simple(
    model, train_loader, val_loader, optimizer, device,
    num_epochs=settings["num_epochs"], eval_freq=5, eval_iter=1,
    start_context="Every effort moves you", tokenizer=tokenizer
)
```

> 细节对比：脚本 `gpt_train.py` 用 `eval_iter=1`（每次评估只抽样 1 个 batch，求快）；notebook 正文用 `eval_iter=5`（抽样 5 个 batch 取均，曲线更平滑）。`eval_iter` 越大评估越准但越慢——这是「评估开销 vs 评估稳定性」的权衡。

#### 4.2.4 代码实践

1. **实践目标**：把双层循环「读活」，验证 `tokens_seen` 与 `global_step` 的计数是否符合预期。
2. **操作步骤**：在 `ch05/01_main-chapter-code/` 下阅读 `gpt_train.py` 的 `main()`（[第 131-202 行](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L131-L202)）。先回答两个预测题，再运行 `python gpt_train.py` 对比：
   - 已知训练集约 4608 个 token、`batch_size=2`、`max_length=256`，预测一个 epoch 有几个 batch？`num_epochs=10` 后 `global_step` 终值是多少？
   - 预测训练完成后 `tokens_seen` 的值。
3. **需要观察的现象**：脚本每 `eval_freq=5` 个 batch 打印一行 `Ep x (Step xxxxxx): Train loss ..., Val loss ...`，每个 epoch 末打印一段从 `Every effort moves you` 起的生成文本。
4. **预期结果**：每 epoch 约 9 个 batch，10 个 epoch 后 `global_step` 终值约 89；`tokens_seen` 约 9×10×512 ≈ 46080（与 4608 token/epoch × 10 吻合）。生成文本从最初的乱码逐渐变成接近《The Verdict》原文的句子。
5. 完整训练耗时取决于设备，CPU 上约几分钟，「待本地验证」具体耗时。

#### 4.2.5 小练习与答案

**Q1**：把 `global_step = -1` 改成 `global_step = 0`（且不改动自增语句的位置），会对训练逻辑产生什么影响？

> **参考答案**：首个 batch 后 `global_step` 变为 1，于是第一次 `step % eval_freq == 0`（`eval_freq=5`）要到第 5 个 batch 才触发，首条评估日志从 `Step 000000` 变成 `Step 000005`，且最终 step 偏移 1。训练本身（权重更新）不受影响，只是评估的时间点和编号错位。

**Q2**：为什么 `model.train()` 写在外层 epoch 循环开头，而不是写在 `train_model_simple` 函数最开头只调一次？

> **参考答案**：因为每个 epoch 内会调用 `evaluate_model` 和 `generate_and_print_sample`，它们都会 `model.eval()`。若不在每个 epoch 开头重新 `model.train()`，从第一次评估之后 dropout 就一直处于关闭状态，破坏了训练阶段的正则化。

---

### 4.3 周期评估与样本生成

#### 4.3.1 概念说明

光有训练四件套还不够——训练是「闭眼走路」，你必须定期「睁眼看看」走到哪了。`train_model_simple` 在循环里埋了两条互补的监控线：

- **数值线（`evaluate_model`）**：每隔 `eval_freq` 个 batch，在训练集与验证集上各算一次平均损失。这是**定量**监控，能画出曲线、判读趋势。
- **样本线（`generate_and_print_sample`）**：每个 epoch 末，用 `start_context` 作起始上下文，让模型自回归生成 50 个 token 并打印。这是**定性**监控，人眼直接看文本质量。

为什么要两条线？因为损失下降并不一定等于「模型变好」——这正是下一节过拟合的核心：训练损失一路走低，生成文本也越来越像样，但验证损失却停滞甚至反弹，说明模型只是在**背诵**训练集。数值线（尤其验证损失）能抓住这种「假繁荣」，样本线则让你直观感受模型的演变。

`evaluate_model` 本身已在 u5-l1 详解（`model.eval()` + `torch.no_grad()` + 抽样 `eval_iter` 个 batch 求均 + `model.train()` 切回），本节聚焦它在训练循环里的**角色**：周期性、轻量化的健康检查。

#### 4.3.2 核心流程

**评估（数值线）**——每个 `eval_freq` 步触发：

```text
evaluate_model(model, train_loader, val_loader, device, eval_iter):
  model.eval()                         # 关 dropout
  with torch.no_grad():                # 不建计算图，省内存省算力
      train_loss = calc_loss_loader(train_loader, model, device, num_batches=eval_iter)
      val_loss   = calc_loss_loader(val_loader,   model, device, num_batches=eval_iter)
  model.train()                        # 切回训练模式
  return train_loss, val_loss          # 返回两个标量
```

**采样（样本线）**——每个 epoch 末触发：

```text
generate_and_print_sample(model, tokenizer, device, start_context):
  model.eval()
  context_size = model.pos_emb.weight.shape[0]        # 从位置嵌入反推最大上下文长度
  encoded = text_to_token_ids(start_context, tokenizer).to(device)
  with torch.no_grad():
      token_ids = generate_text_simple(               # u4-l4 的贪心解码
          model=model, idx=encoded, max_new_tokens=50, context_size=context_size)
  decoded_text = token_ids_to_text(token_ids, tokenizer)
  print(decoded_text.replace("\n", " "))              # 紧凑打印
  model.train()
```

注意 `context_size` 不是写死的，而是**从模型自身的位置嵌入矩阵行数反推**出来的（`model.pos_emb.weight.shape[0]`）。回顾 u4-l3：位置嵌入 `nn.Embedding(context_length, emb_dim)` 的行数正是模型支持的最大序列长度，所以这里能直接读到 `context_length`，避免在多处重复维护同一个常量。

#### 4.3.3 源码精读

[evaluate_model —— gpt_train.py:52-58](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L52-L58)

[generate_and_print_sample —— gpt_train.py:61-72](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L61-L72)

```python
def generate_and_print_sample(model, tokenizer, device, start_context):
    model.eval()
    context_size = model.pos_emb.weight.shape[0]   # 从位置嵌入行数反推 context_length
    encoded = text_to_token_ids(start_context, tokenizer).to(device)
    with torch.no_grad():
        token_ids = generate_text_simple(
            model=model, idx=encoded,
            max_new_tokens=50, context_size=context_size
        )
        decoded_text = token_ids_to_text(token_ids, tokenizer)
        print(decoded_text.replace("\n", " "))     # 把换行折叠成空格，单行打印
    model.train()
```

notebook 正文里给出了 10 个 epoch 的真实样本演变（节选自 [ch05.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/ch05.ipynb)）：

| Epoch | 生成文本节选 |
|------|------|
| 1 | `Every effort moves you,,,,,,,,,,,,.` |
| 3 | `Every effort moves you, and to the of the of the picture. Gis.` |
| 6 | `Every effort moves you know it was his pictures--I glanced after him...` |
| 10 | `Every effort moves you?" "Yes--quite insensible to the irony. She wanted him vindicated--and by me!"...` |

可以清楚看到文本从「乱码标点 → 语法雏形 → 接近原文的连贯句子」的演化——但注意第 10 轮那段几乎是《The Verdict》原文的逐字复刻，这正是过拟合的信号（4.4 详述）。

#### 4.3.4 代码实践

1. **实践目标**：体会「样本线」与「数值线」各自的监控价值。
2. **操作步骤**：运行 `python gpt_train.py`，重点看每个 epoch 末打印的那段生成文本（样本线），并把它与同一 epoch 附近打印的 `Train loss / Val loss`（数值线）对照。
3. **需要观察的现象**：随着 epoch 增加，生成文本越来越连贯（样本线变好），但留意后半段 `Val loss` 是否还在下降。
4. **预期结果**：样本文本质量持续提升，但 `Val loss` 在某个 epoch 后停滞在 6.x 附近甚至略升——这种「文本变好但验证损失不降」的背离，就是过拟合的典型征兆。
5. 具体损失数值「待本地验证」（受设备、PyTorch 版本、Dropout 跨平台差异影响，notebook 提示只要 train loss<1、val loss<7 即属正常范围）。

#### 4.3.5 小练习与答案

**Q1**：`evaluate_model` 为什么要传 `eval_iter`（而不是遍历整个 loader）？

> **参考答案**：完整遍历 loader 评估开销大，会拖慢训练。`eval_iter` 让评估只用 `eval_iter` 个 batch 抽样求均，用很小的代价得到一个有噪声但足够反映趋势的损失估计。训练中我们要的是「趋势」而非「精确值」。

**Q2**：`generate_and_print_sample` 里为什么必须包在 `with torch.no_grad():` 内，且调用前后切换 `eval()/train()`？

> **参考答案**：`no_grad()` 关闭自动微分，避免为纯推理建计算图、浪费内存；`eval()` 关闭 dropout，让每次贪心解码稳定可复现；结束后 `model.train()` 切回，确保下一个 batch 的 dropout 正常工作。

---

### 4.4 损失曲线绘制与过拟合判读

#### 4.4.1 概念说明

`train_model_simple` 返回三条历史数据，`plot_losses` 负责把它们画成一张可解读的图：训练损失与验证损失随训练进度（epoch / 累计 token 数）的变化。这张图是判断训练健康度的「体检报告」。

判读这张图，要理解**过拟合（overfitting）**：模型在训练集上表现越来越好，但在**没见过的验证集**上表现不再提升甚至变差。换句话说，模型从「学到规律」退化为「死记硬背训练样本」。在本项目里这几乎是注定的——因为训练数据只有一篇约 5145 个 token 的短篇小说《The Verdict》，却要被反复迭代 10 个 epoch，模型完全有能力把它逐字背下来。

notebook 对此有明确结论：训练后期生成的段落能在训练集里**逐字找到**——模型并非「理解」了语言，而是记住了原文。

> 重要观念：这里的过拟合是**教学设定**的副产物（数据太小、训练太久），不是实现 bug。真实缓解手段包括：用海量数据、减少 epoch、增大 dropout、早停（early stopping）、更强的权重衰减；而本项目的最终做法是——**与其从零训练，不如直接加载 OpenAI 的预训练权重**（u5-l4）。

#### 4.4.2 核心流程

过拟合在曲线上的判读规则：

```text
理想训练： train_loss ↓   val_loss ↓    （两条线一起下降，间距小）
开始过拟合：train_loss ↓   val_loss →/↑ （训练还在降，验证停滞或反弹）
严重过拟合：train_loss ↓↓  val_loss ↑↑  （剪刀差越拉越大）
```

用困惑度（perplexity，u5-l1）解释更直观：\(\text{PPL}=\exp(\text{loss})\)。本项目训练损失从约 10 降到 <1（PPL 从约 50000 降到 <3），意味着在训练集上模型「几乎确定」下一个词；而验证损失停在约 6（PPL≈400），说明在未见文本上仍高度不确定——剪刀差一目了然。

#### 4.4.3 源码精读

`plot_losses` 用了一个小技巧：**双 x 轴**。主轴 `ax1` 画「损失 vs epoch」，副轴 `ax2`（`ax1.twiny()`，共享 y 轴）画一条**不可见的**「损失 vs tokens_seen」曲线（`alpha=0`），唯一目的是让第二个 x 轴的刻度对齐到真实的 token 数，从而一张图同时读出 epoch 与 token 进度。

[plot_losses —— gpt_train.py:112-128](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L112-L128)

```python
def plot_losses(epochs_seen, tokens_seen, train_losses, val_losses):
    fig, ax1 = plt.subplots()
    ax1.plot(epochs_seen, train_losses, label="Training loss")
    ax1.plot(epochs_seen, val_losses, linestyle="-.", label="Validation loss")
    ax1.set_xlabel("Epochs"); ax1.set_ylabel("Loss"); ax1.legend(loc="upper right")

    ax2 = ax1.twiny()                              # 第二个 x 轴，共享 y 轴
    ax2.plot(tokens_seen, train_losses, alpha=0)   # 不可见曲线，只为对齐刻度
    ax2.set_xlabel("Tokens seen")
    fig.tight_layout()
```

`main()` 训练结束后调用它并保存成 `loss.pdf`（[gpt_train.py:235-237](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L235-L237)）：

```python
epochs_tensor = torch.linspace(0, OTHER_SETTINGS["num_epochs"], len(train_losses))
plot_losses(epochs_tensor, tokens_seen, train_losses, val_losses)
plt.savefig("loss.pdf")
```

> 注意 `torch.linspace(0, num_epochs, len(train_losses))` 把「评估点序号」线性映射到 `[0, num_epochs]` 区间——因为评估是按 `step` 触发的，评估点在 epoch 上并非均匀分布，这里用线性插值近似当作横坐标。

#### 4.4.4 代码实践

1. **实践目标**：亲手画出损失曲线，定位「开始过拟合」的 epoch。
2. **操作步骤**：运行 `python gpt_train.py`，训练结束后查看生成的 `loss.pdf`；或在 notebook 里运行对应 cell（[ch05.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/ch05.ipynb) 的 `plot_losses` 调用块）。
3. **需要观察的现象**：训练损失（实线）持续下降逼近 0；验证损失（点划线）在最初几轮下降后趋于平台（约 6），后半段甚至微微上翘。两条线在某个 epoch 后明显分叉。
4. **预期结果**：分叉点大约在第 2~3 个 epoch——这之前两条线同降（模型在学通用规律），这之后训练损失继续降而验证损失停滞（模型开始背诵训练集）。「开始过拟合的 epoch」就取分叉点附近。
5. 具体曲线形状「待本地验证」。

#### 4.4.5 小练习与答案

**Q1**：若把 `num_epochs` 从 10 调到 1，验证损失通常会怎样？这说明了什么？

> **参考答案**：1 个 epoch 时模型还没来得及背诵训练集，训练损失与验证损失都还较高但差距小，过拟合不明显。这说明本项目的过拟合主要由「数据极小 + epoch 过多」共同导致，减少 epoch 是最直接的缓解手段（即早停思想）。

**Q2**：notebook 说训练后期生成的文本是训练集的逐字复刻，但 4.3 的样本线看起来「越来越好」，两者矛盾吗？

> **参考答案**：不矛盾。样本线只反映「在 `start_context` 这一个提示下的连贯度」，而该提示正是训练集里的句子，所以模型越接近原文，文本越连贯——这正是过拟合的表现，而非泛化能力提升。验证损失才是衡量泛化的标尺，它停滞恰恰揭示了真相。

---

## 5. 综合实践

**任务**：跑通一次完整预训练，画出损失曲线，定位过拟合拐点，并尝试用「早停」缓解。

1. 进入 `ch05/01_main-chapter-code/`，运行 `python gpt_train.py`，等待训练结束（会在当前目录生成 `loss.pdf` 与 `model.pth`）。
2. 打开 `loss.pdf`，在图上标出训练损失与验证损失开始明显分叉的 epoch（记为 \(e^\*\)）。
3. **改造实验**：编辑 `gpt_train.py` 末尾的 `OTHER_SETTINGS`，把 `num_epochs` 改为 \(e^\*\)（即在第 e* 个 epoch 提前停止），重新运行，对比两次的验证损失终值与生成文本。这就是最朴素的**早停（early stopping）**。
4. **进阶（选做）**：把 `drop_rate` 从 0.1 调到 0.3（[gpt_train.py:213](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L213)），观察更强的 dropout 是否能延缓过拟合（验证损失平台是否更低）。
5. 记录三次实验的「最终验证损失」与「生成文本片段」，写一段话总结：epoch 数、dropout、数据量三者如何共同决定过拟合程度。

> 说明：本实践修改的是 `OTHER_SETTINGS` 字典里的超参值，属于配置调整；若你希望严格保持源码原样，可改为新建一个脚本 import `gpt_train` 的函数并传入自定义参数。所有数值结果「待本地验证」。

## 6. 本讲小结

- **训练四件套** `optimizer.zero_grad() → 前向算 loss → loss.backward() → optimizer.step()` 是 PyTorch 万能训练范式；`zero_grad` 必不可省，因为 PyTorch 梯度默认累加。
- **AdamW 优化器** 用自适应学习率（梯度一/二阶矩估计）+ 解耦权重衰减（`weight_decay=0.1`）让大模型训练更稳定。
- **`train_model_simple`** 是「外层 epoch + 内层 batch」的双层循环；`global_step`（从 -1 起）跨 epoch 计数、按 `eval_freq` 调度评估；`tokens_seen` 累计处理 token 数（每 batch 计 `batch_size×num_tokens`）。
- **两条监控线**互补：数值线 `evaluate_model` 给定量损失趋势，样本线 `generate_and_print_sample` 给定性文本质量；`context_size` 从位置嵌入行数反推。
- **`plot_losses`** 用双 x 轴（epoch / tokens_seen）画曲线；训练损失持续降而验证损失停滞或反弹即**过拟合**，根因是训练数据极小（《The Verdict》仅 ~5145 token）却反复迭代，最终解法是加载预训练权重（u5-l4）。

## 7. 下一步学习建议

- **下一讲 u5-l3** 将讲解解码策略（温度缩放、top-k 采样），让你能控制生成文本的多样性与连贯性，并可部分缓解本讲看到的「逐字背诵」现象。
- **u5-l4** 讲解权重保存/加载与加载 OpenAI GPT-2 预训练权重——这是本项目走出「教学过拟合」、获得真实语言能力的正解。
- 想给 `train_model_simple` 加上学习率 warmup、余弦退火、梯度裁剪等进阶技巧，直接跳到 **u8-l2（附录 D）**。
- 若想系统补齐 PyTorch autograd、`nn.Module` 与训练范式的底层原理，阅读 **u8-l1（附录 A）**。
