# 训练主循环解剖：从 loss.backward 到 optimizer.step

## 1. 本讲目标

学完本讲，你应该能够：

1. 按执行顺序说出 `examples/toy_train.py` 中 `__main__` 入口做的 7 件事。
2. 画出 cosine warmup 学习率曲线，解释为什么第 0 步的学习率是 0、warmup 结束时恰好到达峰值。
3. 逐行解释训练循环里的 8 个动作：取 batch → 搬运到设备 → 前向 → 取 loss → 反向 → 参数更新 → 调学习率 → 清梯度 → 记日志。
4. 亲手验证「梯度是累加语义」这一事实：注释掉 `optimizer.zero_grad()` 后观察训练异常。
5. 给训练循环加上窗口平均 loss 与梯度范数的观测日志，为后续对比 Muon 与 AdamW 打好测量基础。

## 2. 前置知识

本讲假设你已经按 u1-l2 跑通过一次训练，并且知道：

- **tensor 与 autograd**：PyTorch 的张量参与运算时会记录计算图；调用 `loss.backward()` 会自动求导，把每个参数的梯度**累加**到 `p.grad` 里。注意是「累加」而不是「覆盖」——这是 `zero_grad()` 必须存在的根本原因。
- **优化器（optimizer）**：`optimizer.step()` 根据 `p.grad` 和当前学习率修改参数。优化器内部把参数分成若干个 `param_groups`（参数组），每组有自己的超参（如 `lr`）。本脚本里无论 AdamW 还是 Muon 都只有一个参数组，所以 `optimizer.param_groups[0]['lr']` 就是全局学习率。
- **学习率调度器（lr scheduler）**：它不直接碰参数，只在每次 `scheduler.step()` 时改写 `param_groups` 里的 `lr`。
- **epoch / step / batch**：一遍完整数据叫一个 epoch；处理一个 batch 叫一个 step。本脚本 `epoch = 1`，即只过一遍数据。
- **u1-l2 已建立的认知**：六个命令行参数的含义、loguru 双 sink 日志（终端 + `logs/` 文件）、初始 loss 理论值 \(\ln(151936)\approx 11.93\)，以及 `--wd` 被解析但并未转发给 `get_optimizer` 这个细节。本讲会在这条链路上继续深入。

## 3. 本讲源码地图

本仓库唯一的源码文件是 `examples/toy_train.py`，共 359 行，可以分为四段。本讲聚焦第四段：

| 行号区域 | 内容 | 归属讲义 |
| --- | --- | --- |
| [L16-L43](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L16-L43) | `MoonDataset`：数据集加载、分词与定长分块 | u1-l4 |
| [L46-L239](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L46-L239) | Newton-Schulz 正交化与 `Muon` 优化器类 | u2 系列 |
| [L242-L313](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L242-L313) | `get_model_and_dataloader` 与 `get_optimizer` 两个装配工厂 | u3-l2 / u2-l1 |
| [L316-L359](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L316-L359) | `__main__` 入口：参数解析、装配、调度器、训练循环 | **本讲** |

本讲会把 L316-L359 当作一条「装配线」来读：原料（模型、数据、优化器）如何就位，主循环如何一个 batch 一个 batch 地消费它们。

## 4. 核心概念与源码讲解

### 4.1 入口与 argparse：`__main__` 做了哪几件事

#### 4.1.1 概念说明

Python 脚本的入口是 `if __name__ == "__main__":` 块：直接执行 `python examples/toy_train.py` 时该块运行，而被 `import` 时不会。Moonlight 把所有「胶水逻辑」都放在这一个块里，顺序非常清晰：**解析参数 → 注册日志 → 装配模型与数据 → 装配优化器 → 选设备 → 建调度器 → 进循环**。

`argparse` 是标准库的命令行解析器：`add_argument` 声明参数名、类型与默认值，`parse_args()` 把 `--lr 0.02` 这样的字符串变成 `args.lr = 0.02`。

#### 4.1.2 核心流程

入口的执行顺序（伪代码）：

```text
1. 解析 6 个命令行参数 → args
2. logger.add(文件 sink)：日志同时输出到终端和 logs/train_<model>_<optimizer>_lr<lr>.log
3. model, train_loader = get_model_and_dataloader(...)   # 下载/分词数据 + 构造 Qwen2 + DataLoader
4. optimizer = get_optimizer(args.optimizer, model, lr=args.lr)
5. device = cuda 若可用，否则 cpu；model.to(device)；model.train()
6. lr_scheduler = cosine warmup（100 步热身，总步数 = len(train_loader)）
7. 双层 for 循环训练
```

#### 4.1.3 源码精读

入口与参数声明，注意 `import argparse` 是函数内的局部导入，只在直接运行脚本时才加载：

[examples/toy_train.py:L316-L327](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L316-L327) —— 声明 6 个参数（model/optimizer/lr/wd/dataset/hidden_size，默认值分别为 `qwen`/`adamw`/`1e-3`/`0.1`/`openwebtext-100k`/`1024`），解析后把日志额外写入 `logs/train_qwen_adamw_lr0.001.log` 这样命名的文件。loguru 默认的 stderr sink 不会被移除，所以终端依然能看到同样内容。

```python
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=str, default="qwen")
    # ... 共 6 个参数 ...
    args = parser.parse_args()
    logger.add(f"logs/train_{args.model}_{args.optimizer}_lr{args.lr}.log")
```

[examples/toy_train.py:L329-L334](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L329-L334) —— 调用两个装配工厂拿到模型、数据与优化器。注意 `get_optimizer` 只转发了 `lr`，**没有转发 `args.wd`**：无论命令行给多少 `--wd`，函数内部都用默认值 `wd=0.1`。这是 u1-l2 发现过的细节，在这里从调用处的代码再次得到确认。

```python
    model, train_loader = get_model_and_dataloader(
        args.model, args.dataset, args.hidden_size
    )
    optimizer = get_optimizer(
        args.optimizer, model, lr=args.lr
    )
```

#### 4.1.4 代码实践

1. **实践目标**：熟悉入口的参数与日志行为。
2. **操作步骤**：
   - 运行 `python examples/toy_train.py --help`，核对 6 个参数与默认值。
   - 依次运行 `--optimizer adamw` 与 `--optimizer muon` 各几十步后中断，然后 `ls logs/` 查看两个日志文件名是否只差优化器字段。若首次运行报找不到 `logs/` 目录的错误，先 `mkdir -p logs` 再重试（loguru 是否自动创建父目录待本地验证）。
3. **需要观察的现象**：终端与日志文件内容一致；两个实验的日志文件名不同，方便事后对比。
4. **预期结果**：得到 `train_qwen_adamw_lr0.001.log` 与 `train_qwen_muon_lr0.001.log` 两个文件。命令实际输出待本地验证。

#### 4.1.5 小练习与答案

1. **问**：把 `--wd` 从 0.1 改成 0.3，训练会受影响吗？
   **答**：完全不会。L332-L334 的调用没有传 `wd`，`get_optimizer` 用自己的默认值 0.1；`args.wd` 被解析后从未被使用（连日志文件名都不含 wd）。
2. **问**：`import argparse` 为什么写在 `__main__` 块里而不是文件顶部？
   **答**：功能上放顶部等价；写在这里表示「argparse 只服务于脚本直接运行的场景」，被 import 时不加载它，是小型示例脚本常见的紧凑写法。
3. **问**：日志文件名由哪几个参数决定？如果只改 `--lr`，会覆盖旧日志吗？
   **答**：由 model、optimizer、lr 三个字段决定。改 `--lr` 会生成新文件名（如 `lr0.02`），不会覆盖旧日志——这为学习率扫描实验（u3-l1）提供了便利。

### 4.2 模型与数据装配：主循环的「原料」如何就位

#### 4.2.1 概念说明

主循环消费三样东西：**模型**（`Qwen2ForCausalLM`）、**数据迭代器**（`DataLoader`）、**优化器**（`AdamW` 或 `Muon`）。它们分别由 `get_model_and_dataloader` 和 `get_optimizer` 两个工厂函数构造。本讲只关注「主循环视角下需要知道的属性」——数据如何分词、Qwen2Config 每个字段什么含义、Muon 如何分组参数，分别留给 u1-l4、u3-l2、u2-l1 精读。

另外两个概念：

- **device 选择**：`cuda` 不可用时回退 `cpu`，所以没有 GPU 也能跑（只是慢）。
- **train/eval 模式**：`model.train()` 把模型切到训练模式，影响 dropout 等层的 behaving；本模型配置了 `attention_dropout=0.0`，实际差异很小，但显式声明是好习惯。

#### 4.2.2 核心流程

```text
get_model_and_dataloader(model, dataset, hidden_size)
 ├─ load_dataset("Elriggs/openwebtext-100k")          # 下载/读缓存数据集
 ├─ Qwen2Tokenizer.from_pretrained("Qwen/Qwen2.5-0.5B")  # 分词器
 ├─ MoonDataset(...)                                    # 分词并缓存为 .bin，按 512 定长切块
 ├─ DataLoader(dataset, batch_size=16, shuffle=True)    # 每个 batch 形状 [16, 512]
 └─ Qwen2ForCausalLM(Qwen2Config(hidden_size=..., ...)) # 随机初始化的 Qwen2
get_optimizer(name, model, lr)
 ├─ "adamw" → torch.optim.AdamW(model.parameters(), ...)
 └─ "muon"  → Muon(muon_params=二维非嵌入参数, adamw_params=其余参数)
随后：model.to(device) → model.train()
```

#### 4.2.3 源码精读

[examples/toy_train.py:L254](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L254) —— DataLoader 以 `batch_size=16`、`shuffle=True` 组织数据：主循环每迭代一次拿到一个 `[16, 512]` 的 long 型张量（16 条样本、每条 512 个 token）。

```python
train_loader = DataLoader(train_dataset, batch_size=16, shuffle=True)
```

[examples/toy_train.py:L336-L339](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L336-L339) —— 装配完成后的三行收尾：选设备、搬模型、切训练模式。注意优化器在模型搬往 GPU **之前**创建——这没有问题，因为优化器状态（如动量缓冲）是在第一次 `step()` 时按参数所在设备惰性创建的。

```python
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)

    model.train()
```

#### 4.2.4 代码实践

1. **实践目标**：在跑训练前先了解「原料」的规模：参数量和总步数。
2. **操作步骤**：在 [L337](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L337)（`model.to(device)` 之后）插入下面两行（示例代码，非项目原有）：

```python
n_params = sum(p.numel() for p in model.parameters())
logger.info(f"params: {n_params/1e6:.1f}M, total steps: {len(train_loader)}")
```

3. **需要观察的现象**：启动后第一屏日志给出参数量与 batch 总数；`total steps` 恰好等于一个 epoch 的 batch 数。
4. **预期结果**：`hidden_size=1024` 时参数量约 3.85 亿（估算：tied 词嵌入 151936×1024 ≈ 1.56 亿，加 12 层 × 每层约 0.19 亿的注意力+MLP 权重；由于 `tie_word_embeddings=True`，词嵌入只计一次）。总步数取决于数据集 token 总量除以 512 再除以 16，具体数值待本地验证。

#### 4.2.5 小练习与答案

1. **问**：`model.train()` 在这个脚本里可以省略吗？
   **答**：功能上基本可以——新构造的模型默认就是训练模式，且 `attention_dropout=0.0` 使 dropout 形同虚设。但显式写出能避免「上游代码改过模式」的隐患，是标准做法。
2. **问**：优化器在 `model.to(device)` 之前创建，为什么不会出错？
   **答**：`torch.optim.Optimizer` 构造时只记录参数引用与超参，状态（AdamW 的动量、Muon 的 `momentum_buffer`）在第一次 `step()` 时才创建，会自动落在参数当时所在的设备上。
3. **问**：主循环拿到的 batch 形状是什么？dtype 是什么？
   **答**：`[16, 512]`，`torch.long`——因为 `MoonDataset.__getitem__` 返回 `torch.tensor(..., dtype=torch.long)`，DataLoader 再堆叠 16 条。

### 4.3 学习率调度：cosine warmup 的数学与机制

#### 4.3.1 概念说明

**为什么需要 warmup**：训练刚开始时参数是随机的，梯度方向噪声很大，大学习率容易把参数「推飞」。warmup 让学习率从 0 线性升到峰值，给模型一个热身阶段。**为什么需要余弦退火**：训练后期接近收敛，小步长更利于精细收敛，余弦曲线让学习率平滑降到接近 0。

transformers 提供的 `get_cosine_schedule_with_warmup` 返回一个 `LambdaLR`：它包装一个因子函数 \(\lambda(t)\)，每次 `scheduler.step()` 把 \(t\) 加一，并改写每个参数组的 `lr` 为 \(\text{lr}_t = \text{lr}_{\text{base}} \cdot \lambda(t)\)。

#### 4.3.2 核心流程

设热身步数 \(W=100\)，总步数 \(T=\text{len(train\_loader)} \times \text{epoch}\)，`num_cycles=0.5` 恰好对应半个余弦周期：

\[
\lambda(t) =
\begin{cases}
\dfrac{t}{W} & t < W \\[6pt]
\dfrac{1}{2}\left(1 + \cos\left(\pi \cdot \dfrac{t - W}{T - W}\right)\right) & t \ge W
\end{cases}
\]

三个可以直接推出的性质：

1. \(\lambda(0) = 0\)：调度器**构造时**就会执行一次 \(t=0\) 的求值，所以第 0 步 `optimizer.step()` 用的学习率是 0，权重实际不变（但梯度、动量缓冲照常计算）。
2. \(\lambda(W) = 1\)：第 100 步恰好达到峰值 \(\text{lr}_{\text{base}}\)（即 `--lr`）。
3. 此后单调下降，\(t \to T\) 时趋近 0。

时序上，每一步发生的事情是：`optimizer.step()` 用**当前**的 \(\lambda(t)\) 更新参数 → `lr_scheduler.step()` 把 \(t\) 推进到 \(t+1\) 并改写 `lr`。

#### 4.3.3 源码精读

[examples/toy_train.py:L340-L346](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L340-L346) —— 用 `len(train_loader)`（batch 总数）乘以 epoch 数作为总步数，热身固定 100 步，`num_cycles=0.5` 即标准半余弦。注意总步数必须在训练开始前告知调度器，所以改数据集大小或 batch_size 会直接改变退火曲线的形状。

```python
    epoch = 1
    lr_scheduler = get_cosine_schedule_with_warmup(
        optimizer=optimizer,
        num_warmup_steps=100,
        num_training_steps=len(train_loader) * epoch,
        num_cycles=0.5,
    )
```

[examples/toy_train.py:L354-L358](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L354-L358) —— 一个关键细节：日志里的 `LR` 是在 `lr_scheduler.step()` **之后**读取的，因此它显示的是「下一步」将使用的学习率，而不是刚刚用过的。第 0 步日志打印的 LR 是 \(\text{lr}_{\text{base}}/100\)，而这一步实际用的是 0。

```python
            optimizer.step()
            lr_scheduler.step()
            optimizer.zero_grad()
            logger.info(
                f"Epoch: {epoch} Step: {step} LR: {optimizer.param_groups[0]['lr']} Training loss: {loss.item()}"
            )
```

#### 4.3.4 代码实践

1. **实践目标**：不训练模型，单独验证调度器曲线的三个性质。
2. **操作步骤**：新建一个独立脚本（示例代码），只用一个假参数驱动调度器：

```python
# 示例代码：lr_schedule_probe.py（放在仓库外或临时目录运行均可）
import torch
from transformers import get_cosine_schedule_with_warmup

p = torch.nn.Parameter(torch.zeros(1))
opt = torch.optim.SGD([p], lr=1e-3)
sch = get_cosine_schedule_with_warmup(opt, num_warmup_steps=100,
                                      num_training_steps=1000, num_cycles=0.5)
lrs = []
for _ in range(1000):
    lrs.append(opt.param_groups[0]["lr"])   # 先记录本步要用的 lr
    opt.step()
    sch.step()
print(f"step0={lrs[0]}, step100={lrs[100]}, step999={lrs[999]:.2e}, peak={max(lrs)}")
```

3. **需要观察的现象**：打印出的四个数值。
4. **预期结果**：`step0=0.0`（第 0 步学习率为 0）、`step100=0.001`（恰在热身结束时到达峰值）、`peak=0.001`、`step999` 是约 1e-8 量级的极小值。具体输出待本地验证。

#### 4.3.5 小练习与答案

1. **问**：第 0 步 `optimizer.step()` 之后，权重变了吗？
   **答**：没有。该步学习率为 0：AdamW 的更新量与权重衰减项都乘以 lr；Muon 分支里 `p.data.mul_(1 - lr*wd)` 乘的是 1、`add_(u, alpha=-0)` 加的是 0。但梯度计算、AdamW 的动量估计、Muon 的 momentum_buffer 都照常更新。
2. **问**：日志第 0 行打印的 `LR: 1e-05`（假设 `--lr 1e-3`）说明第 0 步用了 1e-05 吗？
   **答**：不是。打印发生在 `lr_scheduler.step()` 之后，显示的是第 1 步将用的 \(\lambda(1)\cdot 10^{-3} = 10^{-5}\)；第 0 步实际用的是 0。
3. **问**：如果数据集变大导致 `len(train_loader)` 翻倍，同一 step 编号下的学习率会怎么变？
   **答**：热身段（前 100 步）不变；退火段分母 \(T-W\) 变大，同一 \(t\) 的 \(\lambda(t)\) 更大，即衰减得更慢——调度器会自动「拉长」退火曲线。

### 4.4 训练循环与日志：八个动作逐行走

#### 4.4.1 概念说明

这是整个脚本的心脏，也是所有 PyTorch 训练脚本的「标准步」：**前向传播**计算 loss，**反向传播**把梯度累加进 `p.grad`，**optimizer.step()** 沿梯度（的某种变换）更新参数，**zero_grad()** 清空梯度为下一个 batch 做准备。两个容易忽略的点：

- **梯度的累加语义**：`loss.backward()` 执行的是 `p.grad += 新梯度`。若不清零，上个 batch 的梯度会一直「留下来」，等价于用越积越大的混合梯度更新参数——这正是本讲实践要验证的。
- **labels 传入 input_ids 本身**：语言模型的任务是「用前 \(t\) 个 token 预测第 \(t+1\) 个」，HuggingFace 的 CausalLM 会在内部把 logits 与 labels 错开一位（`logits[:, :-1]` 对 `labels[:, 1:]`），所以无需手工 shift。

#### 4.4.2 核心流程

每个 batch 的 8 个动作（与源码一一对应）：

```text
① step, batch = enumerate(train_loader)   # 取出 [16, 512] 的 batch
② batch = batch.to(device)                # 搬到 GPU/CPU
③ input_ids = batch
④ outputs = model(input_ids, labels=...)  # 前向：logits + 内部错位计算的 CE loss
⑤ loss = outputs.loss
⑥ loss.backward()                         # 反向：梯度累加进每个 p.grad
⑦ optimizer.step()                        # 用当前 lr 更新参数
⑧ lr_scheduler.step()                     # 推进 t，改写 lr
⑨ optimizer.zero_grad()                   # 清空 p.grad
⑩ logger.info(...)                        # 记录 epoch/step/下一步 LR/当前 loss
```

初始若干步的 loss 应从 \(\ln(151936)\approx 11.93\) 附近开始下降（u1-l2 已建立的理论锚点）。

#### 4.4.3 源码精读

[examples/toy_train.py:L347-L353](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L347-L353) —— 双层循环的外层 `for epoch in range(epoch)` 用同名变量复用（`epoch=1`，只跑一遍，这是示例脚本的紧凑写法）；内层每个 batch 先搬设备再前向。`labels=input_ids` 就是「预测下一个 token」的自监督目标。

```python
    for epoch in range(epoch):
        for step, batch in enumerate(train_loader):
            batch = batch.to(device)
            input_ids = batch
            outputs = model(input_ids=input_ids, labels=input_ids)
            loss = outputs.loss
            loss.backward()
```

[examples/toy_train.py:L354-L359](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L354-L359) —— 更新-调度-清零三连，随后记日志。`loss.item()` 把 0 维张量转成 Python float，这会触发一次 GPU→CPU 同步，每步一次的开销在此可接受。`optimizer.zero_grad()` 新版 PyTorch 默认 `set_to_none=True`（把 `p.grad` 置为 `None` 而非清 0）；Muon 的 step 里对 `g is None` 有显式跳过（[L177-L179](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L177-L179)），两种清零方式在这里都安全。

```python
            optimizer.step()
            lr_scheduler.step()
            optimizer.zero_grad()
            logger.info(
                f"Epoch: {epoch} Step: {step} LR: {optimizer.param_groups[0]['lr']} Training loss: {loss.item()}"
            )
```

顺带一个观察：这个循环**没有**梯度裁剪、梯度累积、定期验证、checkpoint 保存和混合精度——这些都是生产级训练循环的常见部件，本示例一概从简。它们之中最先值得补上的就是梯度裁剪（`torch.nn.utils.clip_grad_norm_`），可作为扩展练习。

#### 4.4.4 代码实践（本讲主实践）

**实践 A：给训练循环装上「仪表盘」**

1. **实践目标**：每 10 步输出一次窗口平均 loss 与梯度范数，让训练可观测。
2. **操作步骤**：把 [L347-L359](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L347-L359) 的循环体改为如下（示例代码；注意梯度范数必须在 `zero_grad()` 之前、`backward()` 之后计算）：

```python
        losses = []
        for step, batch in enumerate(train_loader):
            batch = batch.to(device)
            input_ids = batch
            outputs = model(input_ids=input_ids, labels=input_ids)
            loss = outputs.loss
            loss.backward()
            # 梯度范数：所有参数梯度平方和开根号（须在 zero_grad 之前算）
            grad_norm = torch.sqrt(
                sum(p.grad.norm().pow(2) for p in model.parameters() if p.grad is not None)
            )
            optimizer.step()
            lr_scheduler.step()
            optimizer.zero_grad()
            losses.append(loss.item())
            if step % 10 == 9:
                logger.info(
                    f"win-avg loss: {sum(losses)/len(losses):.4f} | grad norm: {grad_norm.item():.2f}"
                )
                losses = []
```

3. **需要观察的现象**：窗口平均 loss 是否随 step 下降；梯度范数在热身期（前 100 步）随学习率上升有无明显波动。
4. **预期结果**：loss 从约 11.9 单调（带噪声）下降；梯度范数量级为个位数到几十，具体数值待本地验证。注意每步计算 `grad_norm` 会引入一次 GPU 同步，训练会略变慢——观测结束可以只在 `step % 10 == 9` 时计算。

**实践 B：注释掉 `zero_grad()`，验证梯度累加语义**

1. **实践目标**：亲眼确认「不清零梯度，训练会坏」。
2. **操作步骤**：在实践 A 的基础上注释掉 `optimizer.zero_grad()` 这一行，用 `--optimizer adamw` 跑约 200 步，记录 loss 曲线与梯度范数；再换 `--optimizer muon` 重复一次。
3. **需要观察的现象**：梯度范数从第 2 步起明显大于正常版本（每个 batch 的梯度在累加）；loss 下降显著变慢、震荡甚至发散。
4. **预期结果**：两种优化器都会异常，但表现不同——AdamW 的一阶/二阶动量对梯度幅值有一定的自归一化作用，而 Muon 虽然会把累加后的梯度正交化（更新谱范范数量级受 `lr` 约束），但方向被历史梯度持续污染。定性结论以你的实测为准（待本地验证）。

#### 4.4.5 小练习与答案

1. **问**：为什么每个 batch 结束都必须 `zero_grad()`？
   **答**：PyTorch 的 `backward()` 对 `p.grad` 做累加而非覆盖。不清零时，本次更新用的是「历史上所有 batch 梯度之和」，等效学习率越来越大、方向混乱，训练会退化（实践 B 验证）。反过来说，这一语义也被用来实现梯度累积——多个 micro-batch 的梯度累加后再 step，等效放大 batch size。
2. **问**：`labels=input_ids` 传的是同一个张量，为什么不需要手工把标签左移一位？
   **答**：HuggingFace 的 `Qwen2ForCausalLM` 在内部做 shift：用 `logits[:, :-1, :]` 与 `labels[:, 1:]` 计算交叉熵，即每个位置预测下一个 token。若手工再 shift 一次反而会错位。
3. **问**：把 `optimizer.step()` 与 `lr_scheduler.step()` 的顺序对调会有什么影响？
   **答**：每一步的学习率会比原顺序「提前一步」生效（第 0 步就会用上 \(\lambda(1)\) 的学习率），且日志里打印的 LR 变成「刚用过」的值。数值差别很小，但语义上正确顺序是先用当前 lr 更新、再推进调度器——PyTorch 文档也要求 `scheduler.step()` 在 `optimizer.step()` 之后调用。

## 5. 综合实践

把本讲知识串成一个「迷你训练观测与消融实验」，全部基于 `examples/toy_train.py` 改造（建议复制一份再改，不动原文件）：

1. **加仪表盘**：实现 4.4.4 实践 A——每 10 步记录窗口平均 loss、梯度范数，配合日志里已有的 step / 下一步 LR，形成四列时序数据。
2. **画学习率曲线**：从日志中提取 `LR` 列，绘制 lr–step 曲线，标出热身段（0-100 步线性上升）与余弦退火段，验证 4.3 的三个性质（第 0 步为 0、第 100 步达峰、末端趋近 0）。注意日志 LR 领先实际使用一步。
3. **做 zero_grad 消融**：按 4.4.4 实践 B 注释掉 `zero_grad()`，重跑相同步数，把正常/异常两条 loss 曲线画在同一张图上，写 3-5 句结论：梯度范数如何变化、loss 行为如何变化、AdamW 与 Muon 的异常表现有何差异。
4. **（可选扩展）补上梯度裁剪**：在 `backward()` 之后、`step()` 之前插入 `torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)`，对比有/无裁剪时梯度范数日志的差异，体会生产级训练循环为何几乎总带这一步。

完成后你将拥有一份可复用的实验记录方法，u3-l1 的「Muon vs AdamW 对比实验」将直接复用这套仪表盘。

## 6. 本讲小结

- 入口 `__main__` 按「解析参数 → 注册日志 → 装配模型/数据 → 装配优化器 → 选设备 → 建调度器 → 进循环」七步线性推进，其中 `--wd` 被解析但从未转发给 `get_optimizer`。
- cosine warmup 调度：前 100 步线性升温 \(\lambda(t)=t/W\)，之后按 \(\frac{1}{2}(1+\cos(\pi\frac{t-W}{T-W}))\) 退火；构造调度器时就会把 lr 置为 \(\lambda(0)=0\)，所以第 0 步的参数更新量实际为零。
- 日志中的 `LR` 在 `lr_scheduler.step()` 之后读取，显示的是**下一步**的学习率。
- 标准训练步五连：`forward → loss.backward() → optimizer.step() → lr_scheduler.step() → optimizer.zero_grad()`；梯度是累加语义，`zero_grad()` 不可省略。
- `labels=input_ids` 依赖 HuggingFace CausalLM 内部的错位 shift，实现「预测下一个 token」的自监督目标。
- 该循环省略了梯度裁剪、梯度累积、验证与 checkpoint 等生产级部件，是最精简的可运行形态。

## 7. 下一步学习建议

- 下一讲 **u1-l4（数据管线）**：深入 `MoonDataset`（[L16-L43](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L16-L43)），弄清 openwebtext-100k 如何被 Qwen2 分词器编码、缓存为 `.bin`、再按 512 切块——这解释了本讲中 `len(train_loader)` 与 batch 形状的来源。
- 进入 **u2 系列（Muon 优化器）**：本讲只把 `optimizer.step()` 当黑盒；u2-l1 从参数分组开始逐函数拆开它。
- 延伸阅读：PyTorch 官方文档中 `torch.optim` 的 param_groups 语义、`LambdaLR` 的实现，以及 `zero_grad(set_to_none=...)` 的性能说明，能加深对本讲各细节的理解。
