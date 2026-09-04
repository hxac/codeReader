# Muon vs AdamW：设计你自己的对比实验

## 1. 本讲目标

前两个单元我们已经把 `examples/toy_train.py` 的每一处关键代码都读完了：数据管线、训练循环、Muon 的参数分组、Newton-Schulz 正交化、动量与权重衰减、更新 RMS 一致化、内嵌 AdamW 分支。本讲换一个角色——你不再只是读者，而是**实验设计者**。

学完本讲，你应该能够：

1. 说清楚为什么「各跑一次训练然后比 loss」不是公平对比，并列出一个优化器对比实验必须锁定的控制变量清单。
2. 设计并执行一次**学习率扫描**：对 AdamW 与 Muon 各自至少 3 组学习率，其余超参全部固定。
3. 从 `logs/` 目录的日志文件中解析出 loss 曲线，用移动平均去噪，绘制两种优化器的对比图。
4. 用「到达同一 loss 所需步数之比」把小实验现象与论文「Muon 约需 AdamW 52% 训练 FLOPs（约 2 倍计算效率）」的结论对应起来，并说清楚 toy 实验能验证什么、不能验证什么。

## 2. 前置知识

本讲默认你已完成 u1、u2 两个单元。用到的核心概念快速回顾：

- **toy_train.py 的整体结构**（u1-l3）：入口按「解析参数→注册日志→装配模型与数据→装配优化器→建调度器→进循环」推进；训练步为 forward→backward→`optimizer.step()`→`lr_scheduler.step()`→`optimizer.zero_grad()`。
- **更新 RMS 与形状自适应学习率**（u2-l4）：Muon 分支的实际更新量级是 \(0.2\eta\)（\(\eta\) 为学习率），由 `adjust_lr_for_muon` 乘上 \(0.2\sqrt{\max(A,B)}\) 实现形状归一；AdamW 分支的逐元素归一天然使更新量级约为 \(\eta\)。**这意味着两个优化器的 `--lr` 语义并不在同一把尺子上**——这是本讲要做学习率扫描的根本原因。
- **解耦权重衰减**（u2-l3）：Muon 分支以 \(p \cdot (1-\eta \cdot wd)\) 收缩参数，AdamW 分支同样先衰减后更新，两分支共用同一 `lr` 与 `wd`。
- **cosine warmup 调度**（u1-l3）：前 100 步线性热身，之后余弦退火；总步数由 `len(train_loader)` 决定。
- **每步消耗的 token 数是常数**（u1-l4）：`batch_size=16`、`max_length=512`，故每步恰好消耗 \(16 \times 512 = 8192\) 个 token。

本讲的新术语：

- **样本效率（sample efficiency）**：达到同一训练效果所需训练样本（token 数）越少，样本效率越高。README 对论文 Figure 1(a) 的说明原文是 "Muon is 2 times more sample efficient than Adam"。
- **计算效率（computational efficiency）**：达到同一效果所需训练 FLOPs 越少，计算效率越高。论文结论是 Muon 只需约 52% 的训练 FLOPs。
- **控制变量法**：一次实验只允许一个因素变化。对比优化器时，「优化器种类」是唯一变量，模型、数据、步数、调度、随机性都要被钉死或被平均掉。
- **学习率扫描（lr sweep）**：对每个优化器分别在若干个（通常按对数间隔的）学习率上各跑一次，取各自最优结果再横向比较——先给每个选手找它自己的最佳档位，再比成绩。

## 3. 本讲源码地图

本讲涉及的文件只有两个，但引用的角度与前面单元不同——我们关心的不是「它怎么实现」，而是「它为公平实验提供了什么抓手」。

| 文件 | 本讲关注点 | 关键位置 |
|---|---|---|
| `examples/toy_train.py` | 命令行参数如何成为实验变量；日志文件如何命名与写入；两种优化器如何从同一参数池构造；每步 token 数由哪些代码决定 | L316-359（入口与循环）、L287-313（get_optimizer）、L254 与 L36-43（每步 token 数）、L142-148（lr 语义差异的来源） |
| `README.md` | 论文核心结论的原始表述：2 倍计算效率、52% FLOPs、2 倍样本效率；官方推荐的对照训练命令 | L14-19（摘要）、L23-31（Key Ingredients）、L33-36（Figure 1 说明）、L130-137（训练命令） |

## 4. 核心概念与源码讲解

### 4.1 公平对比原则：让两个优化器站在同一起跑线

#### 4.1.1 概念说明

新手做优化器对比最常见的错误是：用默认超参各跑一次，看谁 loss 低就宣布谁赢。这个结论往往是错的，因为：

1. **优化器性能是超参的函数**。同一个优化器在合适与不合适的学习率下表现可以天差地别；如果默认学习率恰好适配 AdamW 而不适配 Muon，比较就失效了。正确做法是先分别扫描、再比较各自的最优（4.2 节）。
2. **两个优化器的 `lr` 语义不同**。u2-l4 已经推导过：Muon 分支经 `adjust_lr_for_muon` 缩放后，更新 RMS 统一为 \(0.2\eta\)，且单步更新的谱范数等于 \(\eta \cdot 0.2\sqrt{\max(A,B)}\)；而 AdamW 分支的更新量级约为 \eta。数值相同的 `--lr` 在两边产生的「每步改动幅度」不同，直接共用一个值并不天然公平。
3. **随机性会制造假差异**。模型初始化、数据 shuffle、dropout（本配置为 0）都引入随机性；两次「完全相同」的命令也会得到不同曲线。差异小于随机波动时，结论无效。

公平对比的三个层次：

- **控制变量**：除优化器外一切固定——同一模型结构与初始化种子、同一数据集与窗口长度、同一 batch size、同一步数预算、同一调度形状。
- **各自最优**：每个优化器先在自己的超参网格里取最优，再比较最优对最优。
- **明确度量**：先定义好「赢」的标准（如固定步数后的窗口平均 loss、或到达某 loss 阈值的步数），再跑实验，避免事后挑指标。

另外要区分两种效率：**样本效率**（按 token 数/步数计）与**墙钟效率**（按秒计）。Muon 每步要多做 5 次 Newton-Schulz 迭代，单步墙钟时间略长；论文宣称的 2 倍效率指的是训练 FLOPs（≈ token 数），本讲的 toy 对比也应以步数为准。

#### 4.1.2 核心流程

一场公平对比的检查清单：

1. 固定 `--model qwen --dataset openwebtext-100k --hidden_size <同一值>`（须为 16 的倍数，头数固定 16）。
2. 固定随机种子（脚本本身没设，需要自己补，见 4.1.3）。
3. 固定窗口长度 `max_length=512` 与 `batch_size=16`——注意改窗口会同时改变每步计算量、每 epoch 步数和调度曲线，属于「一改三」，对比期间不许动。
4. 步数预算一致：要么都跑完整 epoch（步数天然相同），要么都截断到同一最大步数再做比较。
5. 唯一变量：`--optimizer` 与扫描中的 `--lr`。
6. 先定度量再看结果。

#### 4.1.3 源码精读

**两种优化器从同一个参数池构造。** [examples/toy_train.py:287-313](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L287-L313)：`get_optimizer` 里 `adamw` 分支把 `model.parameters()` 全部交给 `torch.optim.AdamW`（betas 取 `(0.9, 0.95)`）；`muon` 分支对同一个 `model.named_parameters()` 做互斥完备的两组划分后构造 `Muon`。两者操作完全相同的参数集合，这是「唯一变量是优化器」在代码层面的保证。

**`--wd` 实际上被双方固定为 0.1。** [examples/toy_train.py:332-334](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L332-L334)：调用 `get_optimizer(args.optimizer, model, lr=args.lr)` 时没有传 `wd`，于是函数签名 [L287](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L287) 的默认值 `wd=0.1` 生效——AdamW 分支的 `weight_decay` 与 Muon 分支的解耦衰减用的是同一个 0.1。u1-l2 把它当作脚本的坑来讲；在本讲的实验设计里，它反而「歪打正着」地保证了权重衰减公平。但你要知道：命令行改 `--wd` 不会生效，若想研究权重衰减本身，必须把这个参数真正转发进去。

**脚本没有设置任何随机种子。** 通读 [examples/toy_train.py:316-359](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L316-L359) 入口段，没有 `torch.manual_seed` 之类的调用；模型初始化与 DataLoader 的 `shuffle=True`（[L254](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L254)）都由全局随机状态驱动。也就是说，**两条「同命令」的曲线天然不可重复**。想做严格对照，需要在参数解析之后补种子（下面实践会给示例代码；即便补了，CUDA 上 cuDNN 的非确定性算子仍可能带来微小差异，只能弱化、不能根除）。

**调度形状对两种优化器一视同仁。** [examples/toy_train.py:341-346](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L341-L346)：`get_cosine_schedule_with_warmup` 通过改写 `param_groups[0]['lr']` 生效；AdamW 与 Muon 都只有一个 param group（Muon 把两组参数合并进唯一 group，靠 `state[p]["use_muon"]` 分流，见 u2-l1），所以两者经历完全相同的热身与退火曲线，公平。

**每步 token 数是常数，这是「步数≈样本量」的桥梁。** [examples/toy_train.py:36-43](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L36-L43) 把 token 长流按 `max_length` 切成定长样本，[L254](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L254) 以 `batch_size=16` 组批，因此每步消耗 \(16 \times 512 = 8192\) 个 token，与优化器无关。这个不变量在 4.3 节把「步数优势」翻译成「样本/FLOPs 优势」时会用到。

#### 4.1.4 代码实践

**实践目标**：把「随机性会制造假差异」变成亲眼所见，并为后续扫描固定好种子。

**操作步骤**（示例代码，需自行加入 `toy_train.py`，本讲义不改动仓库源码）：

1. 在 `args = parser.parse_args()`（[L326](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L326)）之后插入固定种子的代码：

   ```python
   # 示例代码：固定随机种子，使实验可复现
   import random
   import numpy as np

   seed = 42
   random.seed(seed)
   np.random.seed(seed)
   torch.manual_seed(seed)
   torch.cuda.manual_seed_all(seed)
   ```

2. 先**不加种子**，用同一命令连续跑两次（建议用 `--hidden_size 256` 缩小模型加速，下同）：

   ```bash
   python3 examples/toy_train.py --model qwen --optimizer adamw --hidden_size 256 --lr 1e-3
   python3 examples/toy_train.py --model qwen --optimizer adamw --hidden_size 256 --lr 1e-3
   ```

   注意：日志文件是**追加写入**的（`logger.add` 默认 mode 为 append，见 [L327](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L327)），重跑前先 `rm logs/train_qwen_adamw_lr0.001.log`，否则两条曲线会混进同一个文件。
3. 再加上种子，同样跑两次。

**需要观察的现象**：不加种子时，两次运行同一步的 `Training loss` 数值不同；加种子后，前若干步的 loss 应当逐步对齐（CUDA 环境下可能仍有尾部微小偏差）。

**预期结果**：无种子两次运行的第 100 步窗口平均 loss 差异通常在小数点后一到两位（具体幅度待本地验证）；加种子后前几百步基本重合（待本地验证）。由此得出本讲第一条纪律：**不固定种子、不做重复，单次运行的细微差距不能作为结论**。

#### 4.1.5 小练习与答案

**练习 1**：为什么「AdamW 用 lr=1e-3、Muon 用 lr=1e-3，各跑一次」不是公平对比？
**答案**：一是因为两个优化器的 lr 语义不同——Muon 分支经 `adjust_lr_for_muon` 的 \(0.2\sqrt{\max(A,B)}\) 缩放后更新 RMS 为 \(0.2\eta\)，AdamW 分支更新量级约为 \(\eta\)——相同数值的 lr 对应不同的实际步长，谁占便宜取决于具体参数形状；二是因为单点超参可能恰好落在某一方的舒适区，必须先扫描让各自达到最优，再比较最优对最优。

**练习 2**：本配置下，若把 `max_length` 从 512 改成 256，会对对比实验造成哪些「连锁」影响？
**答案**：至少三个：(1) 每步 token 数从 8192 变为 4096，步数与样本量的换算关系改变；(2) `len(train_loader)` 约翻倍，cosine 调度的总步数与曲线形状随之改变；(3) 每步计算量变小、单步 loss 的噪声特性改变。一个改动影响三个因素，因此对比实验期间窗口长度必须固定。

**练习 3**：实验中你观察到 Muon 每步的墙钟时间比 AdamW 长一点，这能否推翻「Muon 更高效」？
**答案**：不能直接推翻。论文宣称的是计算（FLOPs/样本）效率，不是墙钟效率。Muon 每步额外的 Newton-Schulz 迭代（5 次矩阵乘）带来固定的每步开销；只要「到达同一 loss 的步数」优势足够大，扣除每步开销后仍占优。判断时要分别记录「步数-损失」与「时间-损失」两组曲线，区分样本效率与墙钟效率。

### 4.2 学习率扫描：给每个优化器找它的最优档位

#### 4.2.1 概念说明

学习率扫描（lr sweep）的做法：选定一组按对数间隔的学习率（如 \(\{3\times10^{-4}, 10^{-3}, 3\times10^{-3}\}\)，相邻约 3 倍），其余超参全部固定，每个学习率完整训练一次；给每次运行打一个标量分（本讲用「最后 K 步的窗口平均 loss」，K 取 50~100，能压掉单步噪声）；每个优化器取分数最低的那次运行作为它的「最佳档位」，最后比较两条最佳曲线。

扫描还能告诉你扫描本身的信息：

- **学习率过大**：loss 震荡、回升甚至发散为 NaN——扫描上界找到了。
- **学习率过小**：loss 下降缓慢、整体偏高——扫描下界找到了。
- **两者都不是**：最优可能在网格内部，也可能贴近某一侧，必要时在最优附近再加密一轮。

为什么网格要按对数间隔？因为学习率的合理范围常跨数量级，线性网格会把点浪费在不可能的区间。相邻倍数取 2~3 倍是常见折中：太密浪费算力，太疏容易错过最优。

#### 4.2.2 核心流程

```
for optimizer in [adamw, muon]:
    for lr in [3e-4, 1e-3, 3e-3]:
        清理同名旧日志
        运行 toy_train.py（其余参数固定）
        解析日志 → 最后 K 步平均 loss → 记录 (optimizer, lr, score, 曲线)
每个 optimizer 取 score 最低的运行 → 进入 4.3/4.4 的对比
```

汇总成一张表：

| optimizer | lr | 最后 K 步平均 loss | 是否发散 |
|---|---|---|---|
| adamw | 3e-4 | …（待本地验证） | 否 |
| adamw | 1e-3 | … | 否 |
| adamw | 3e-3 | … | 视环境而定 |
| muon | 3e-4 | … | 否 |
| muon | 1e-3 | … | 否 |
| muon | 3e-3 | … | 视环境而定 |

#### 4.2.3 源码精读

**命令行就是实验面板。** [examples/toy_train.py:319-326](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L319-L326)：`--optimizer` 与 `--lr` 正是扫描要扫的两个维度；`--hidden_size` 默认 1024，实践建议缩到 256 以提速（须为 16 的倍数）。README 给出的官方对照命令（[README.md:130-137](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L130-L137)）以 `--hidden_size 896 --lr 1e-3` 成对运行两种优化器——这正是扫描网格中以 `1e-3` 为中心点向外扩展的起点：

```
# train qwen-like dense model with muon
python3 examples/toy_train.py --model qwen --optimizer muon --dataset openwebtext-100k --hidden_size 896 --lr 1e-3

# train qwen-like dense model with adamw
python3 examples/toy_train.py --model qwen --optimizer adamw --dataset openwebtext-100k --hidden_size 896 --lr 1e-3
```

**日志文件名自带实验标签。** [examples/toy_train.py:327](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L327)：

```python
logger.add(f"logs/train_{args.model}_{args.optimizer}_lr{args.lr}.log")
```

模型名、优化器名、学习率都被编进文件名（`1e-3` 会格式化成 `0.001`，得到 `logs/train_qwen_muon_lr0.001.log`）。扫描产生的六次运行天然落到六个不同文件，互不覆盖——脚本作者已经为扫描实验铺好了路。唯一要记住的坑：loguru 文件 sink 默认追加写入，**重跑同名实验前先删旧日志**。

**每次运行写进日志的只有一种行。** [examples/toy_train.py:357-359](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L357-L359)：

```python
logger.info(
    f"Epoch: {epoch} Step: {step} LR: {optimizer.param_groups[0]['lr']} Training loss: {loss.item()}"
)
```

分词进度条（tqdm）走 stderr，数据集/transformers 的告警走各自的 logger——都不进这个文件。因此日志文件里每行都是 `Epoch: 0 Step: 123 LR: 0.00095 Training loss: 6.78` 的干净格式，正则解析毫无歧义（4.4 节的解析脚本就依赖这一点）。另外注意 LR 打印的是**下一步**的学习率（u1-l3 的结论），画「学习率曲线」时要知道它超前一步。

**AdamW 分支的对照超参也是固定的。** [examples/toy_train.py:288-291](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L288-L291)：`betas=(0.9, 0.95)`、`weight_decay=0.1`，与 Muon 内嵌 AdamW 分支的默认 `adamw_betas=(0.9, 0.95)`、`wd=0.1`（[L114-116](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L114-L116)）对齐。对照双方的「非优化器超参」刻意一致，这是公平原则在代码里的又一处体现。

#### 4.2.4 代码实践

**实践目标**：完成 2×3 的学习率扫描，拿到六份日志与一张扫描汇总表。

**操作步骤**：

1. 确认数据缓存已就绪（首次运行会下载并分词 openwebtext-100k，生成 `openwebtext-100k.bin`；之后所有运行直接复用）。
2. 依次执行六次训练（示例命令以 `hidden_size 256` 提速；若算力充足可用 README 的 896）：

   ```bash
   for opt in adamw muon; do
     for lr in 3e-4 1e-3 3e-3; do
       rm -f "logs/train_qwen_${opt}_lr${lr}.log"
       python3 examples/toy_train.py --model qwen --optimizer $opt \
         --dataset openwebtext-100k --hidden_size 256 --lr $lr
     done
   done
   ```

   注意 shell 变量 `3e-4` 传入后 `args.lr` 是浮点数 `0.0003`，日志文件名会写作 `lr0.0003`——删除旧日志时的文件名要与实际生成的对上（保险起见可 `rm -f logs/train_qwen_*.log`）。
3. 每份日志取最后 100 行的 `Training loss` 求平均（可在 4.4 节脚本里一并完成），填进 4.2.2 的汇总表。

**需要观察的现象**：

- 每次运行开始的 loss 都在 \(\ln(151936) \approx 11.93\) 附近（随机初始化的理论值，u1-l2 已推导）；
- 前一百步热身期间 loss 下降缓慢，之后加速；
- 最高学习率的一组是否出现 loss 震荡、回升或 NaN。

**预期结果**：六份日志齐全，`logs/` 下有六个不同文件名的 `.log`；两种优化器各自存在一个「平均 loss 最低」的学习率档位；最高档是否发散取决于模型规模与环境（待本地验证）。若某优化器三档全部单调偏向一侧（例如 lr 越大越好），说明最优在网格外，应向该侧扩一格再扫。

#### 4.2.5 小练习与答案

**练习 1**：为什么用「最后 K 步的窗口平均 loss」做标量分，而不是用「历史最低 loss」？
**答案**：单步 loss 噪声很大，历史最低是对噪声的「择优采样」，天然偏低且不可复现；窗口平均平滑了随机波动，反映训练末期的稳定水平。历史最低还会奖励不稳定运行的偶然尖峰，系统性高估表现。

**练习 2**：扫描网格 \(\{3\times10^{-4}, 10^{-3}, 3\times10^{-3}\}\) 为什么不包含 `1e-2`？
**答案**：相邻倍数取 3 左右是对数扫描的常规密度；从默认 `1e-3` 向两侧各扩一格已覆盖一个数量级。是否需要 `1e-2` 由实验决定——若 `3\times10^{-3}` 恰是某优化器的最优且未发散，说明最优可能更靠上，应扩网格；若 `3\times10^{-3}` 已发散或劣化，上界已找到，无需再扩。

**练习 3**：某次运行日志里出现 `Training loss: nan`，这组数据应如何处理？
**答案**：记为发散，标量分记为正无穷（或从「最优」候选中剔除），但**要保留这个事实**——它标记了该优化器在此模型规模下的学习率上界，是扫描的副产品结论，不是需要藏起来的失败。

### 4.3 计算效率结论解读：从「步数优势」到「52% FLOPs」

#### 4.3.1 概念说明

先看论文结论的原始表述。[README.md:15](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L15)（摘要）："Scaling law experiments indicate that Muon achieves ∼ 2× computational efficiency compared to AdamW with compute optimal training."；[README.md:31](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L31)："Based on the scaling law results, Muon achieves comparable performance to AdamW trained counterparts while requiring only approximately 52% of the training FLOPs."；[README.md:35](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L35)（Figure 1 说明）："(a) Scaling law experiments comparing Muon and Adam. Muon is 2 times more sample efficient than Adam."

这三句话说的是同一件事的两种度量：

- **样本效率口径**：达到同一验证损失，Muon 所需 token 数约为 AdamW 的一半（2 倍样本效率）。
- **FLOPs 口径**：达到同一性能，Muon 所需训练 FLOPs 约为 AdamW 的 52%。

为什么「样本 ≈ FLOPs」？训练 FLOPs 近似为

\[
\text{FLOPs} \;\approx\; 6\,N\,D
\]

其中 \(N\) 是（非嵌入）参数量、\(D\) 是训练 token 数，系数 6 来自前向（≈\(2ND\)）加反向（≈\(4ND\)）。在**固定模型**（\(N\) 不变）的对比里，FLOPs 与 token 数成正比；而每步 token 数固定为 8192（4.1.3 已从源码确认），于是

\[
\text{FLOPs} \;\propto\; D \;=\; 8192 \times \text{步数}
\]

两个优化器每步的前向/反向计算完全相同，Muon 多出的只是 5 次 Newton-Schulz 迭代的开销（对 toy 模型不可忽略，对大模型占比很小）。因此在 toy 实验里，「到达同一 loss 的步数之比」就是「训练 FLOPs 之比」的一阶近似。

还要理解论文结论的来历，避免过度解读：52% 不是某两次训练的比值，而是**scaling law 拟合**的产物——论文在多个模型规模、多个 token 预算下训练，拟合出每个优化器的损失面 \(L(N, D)\)，再在「计算最优」（compute-optimal）意义上比较两条损失面：给定 FLOPs 预算 \(C\)，每种优化器都有自己的最优 \((N, D)\) 组合；Muon 的损失面整体更低，等损失等高线对应的 FLOPs 约为 AdamW 的 52%（Figure 1(a)，见 [README.md:33-36](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L33-L36)）。我们的 toy 实验只有单模型规模、单次运行，**只能做定性呼应，不能复现 52% 这个数字**。

#### 4.3.2 核心流程

把「更样本高效」翻译成 toy 实验可计算的量——**阈值到达步数法**：

1. 从 4.2 的扫描中取出两种优化器各自的最佳曲线（窗口平均平滑后）。
2. 选一个参考损失 \(L^*\)：要低于两条曲线早期平台、高于（或略高于）两条曲线的最终水平，保证两条曲线都能到达；敏感性检查时换 2~3 个 \(L^*\) 各算一次。
3. 分别找第一条满足 \(\text{smoothed loss} \le L^*\) 的步号 \(t_{\text{adamw}}\)、\(t_{\text{Muon}}\)。
4. 计算效率比：

\[
\text{效率比} \;=\; \frac{t_{\text{adamw}}}{t_{\text{Muon}}}
\]

比值 > 1 表示 Muon 用更少步数（≈更少 token/FLOPs）到达同一水平；比值约 2 即与论文「2 倍样本效率」同向。

5. 把步数换算成 token：\(D = 8192 \times t\)，写进报告，建立与论文口径的直接联系。

伪代码：

```
target = L*
t_adamw = min{ t : smooth(loss_adamw)[t] <= target }
t_muon  = min{ t : smooth(loss_muon)[t]  <= target }
ratio   = t_adamw / t_muon
```

#### 4.3.3 源码精读

**结论的出处。** 上面已引 [README.md:15](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L15)、[L31](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L31)、[L35](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L35)。注意 [L31](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L31) 明说结论基于 "scaling law results"——多规模拟合，不是单点实验。

**结论的代价与去处。** README 的 Key Ingredients（[README.md:23-31](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L23-L31)）指出两大支撑技术：权重衰减与逐参数更新尺度调整（update RMS 一致化）。它们在代码中的落点你已在 u2 系列精读过：解耦衰减 [examples/toy_train.py:199-200](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L199-L200)，形状缩放 [L142-148](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L142-L148)：

```python
adjusted_ratio = 0.2 * math.sqrt(max(A, B))
adjusted_lr = lr * adjusted_ratio
```

没有这两处改造，原版 Muon 在本讲的对比里未必占优——论文宣称的效率优势是「Moonlight 改造版 Muon」的优势。做实验解读时，这一点必须写进报告。

**每步开销差异的来源。** Muon 分支对每个二维参数执行 `zeropower_via_newtonschulz5(g, steps=ns_steps)`（[examples/toy_train.py:194](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L194)），每次调用做 5 轮矩阵迭代（[L67-72](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L67-L72)），并经 `@torch.compile` 加速（[L48](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L48)）。这是「Muon 每步墙钟更长」的代码证据，也是 4.1.5 练习 3 的注脚。

**效率优势的最终去处。** [README.md:47-65](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L47-L65) 的对照表显示：同样 5.7T tokens、同构的 16B MoE，Muon 训练的 Moonlight 在 MMLU 上 70.0 对 DSV2-Lite（AdamW）的 58.3——这是「样本效率→同预算更强模型」的端到端体现。

#### 4.3.4 代码实践

**实践目标**：用阈值到达步数法定量计算两条最佳曲线的效率比，并检验结论对 \(L^*\) 选取的稳健性。

**操作步骤**（示例代码，接 4.2 的解析结果；完整解析函数见 4.4.4）：

```python
# 示例代码：阈值到达步数法
def first_step_below(steps, smoothed, target):
    for s, v in zip(steps, smoothed):
        if v <= target:
            return s
    return None  # 未到达阈值

# steps_adamw/losses_adamw 与 steps_muon/losses_muon 来自 4.2 各自的最佳运行
smooth_a = moving_average(losses_adamw, k=100)
smooth_m = moving_average(losses_muon, k=100)

for target in [6.0, 5.5, 5.0]:          # 换 2~3 个阈值做敏感性检查
    ta = first_step_below(steps_adamw, smooth_a, target)
    tm = first_step_below(steps_muon,  smooth_m, target)
    if ta and tm:
        print(f"L*={target}: adamw {ta} 步, muon {tm} 步, "
              f"效率比={ta/tm:.2f}, token 比={(ta*8192)/(tm*8192):.2f}")
    else:
        print(f"L*={target}: 有曲线未到达该阈值")
```

**需要观察的现象**：效率比是否在所有（或多数）阈值下都大于 1；换平滑窗口（k=50 与 k=100）后结论是否翻转。

**预期结果**：待本地验证。若在 toy 规模下 Muon 的最佳曲线整体位于 AdamW 下方且更早到达各阈值（效率比 > 1 且对不同 \(L^*\) 稳定），则定性支持「Muon 更样本高效」；若两曲线交错或比值随 \(L^*\) 大幅摆动，正确结论是「toy 规模下差异不显著/不稳健」——这同样是有价值的实验结果，与论文结论并不矛盾（论文结论产生于远大于 toy 的规模与多规模拟合）。

#### 4.3.5 小练习与答案

**练习 1**：论文说「约 52% 的训练 FLOPs」，为什么等价于「约 2 倍计算效率」？toy 实验里对应的量是什么？
**答案**：效率与开销互为倒数——AdamW 需要 \(C\) FLOPs 达到的损失，Muon 约 \(0.52C\) 就能达到，故 Muon 的计算效率约为 \(1/0.52 \approx 1.92 \approx 2\) 倍。toy 实验固定模型规模，每步 FLOPs 近似相同且每步 token 数固定为 8192，因此该比值近似等于「AdamW 到达阈值步数 / Muon 到达阈值步数」。

**练习 2**：为什么不能用 toy 实验的效率比去「验证 52%」这个具体数字？
**答案**：三个原因。(1) 规模不同：论文结论来自多模型规模的 scaling law 拟合与计算最优外推，toy 是单一小规模；(2) 预算不同：论文比较的是 compute-optimal 配置（每个优化器各自最优的 \(N/D\) 组合），toy 固定了一个模型；(3) 噪声：toy 单次运行、短训练、无验证集，比值随阈值与平滑参数摆动。toy 能做的只是检验结论的**方向性**。

**练习 3**：如果把 `max_length` 从 512 改成 1024，每步 token 数变为 16384，效率比的计算要相应改什么？
**答案**：效率比本身（步数之比）不变，但「步数→token 数」的换算系数从 8192 改为 \(16 \times 1024 = 16384\)；同时每 epoch 步数减半、调度曲线改变、单步计算量翻倍——又一次说明对比实验期间不该动窗口长度。

### 4.4 结果可视化：把六条日志变成一张可比的图

#### 4.4.1 概念说明

原始的逐步 loss 曲线噪声很大（每个 batch 的 loss 取决于该批文本的难度），直接叠图画出来是「毛毛」，肉眼难辨优劣。可视化的三步处理：

1. **解析**：从每份 `.log` 提取 `(step, loss)` 序列。日志行格式唯一（4.2.3 已确认），一行正则即可。
2. **平滑**：滑动窗口平均，窗口 k 取 50~100。平滑是对「局部趋势」的估计，窗口越大越平滑、时滞也越大；对比不同曲线时必须用同一窗口。
3. **编码**：一张图里用**颜色区分优化器、线型区分学习率**（或反之），保证只扫一个视觉维度；比较最优对最优时加粗最佳曲线；画阈值 \(L^*\) 的水平虚线，让「到达步数」在图上可见。

配套一张汇总表（4.2.2 的表填上数字），图给趋势、表给精确值。

#### 4.4.2 核心流程

```
logs/*.log
   │  正则解析（每行提取 Step 与 Training loss）
   ▼
{run_name: (steps, losses)}
   │  移动平均（k=100）
   ▼
matplotlib 一张图：
   - 全部运行：细线，颜色=优化器，线型=lr
   - 各优化器最佳：加粗
   - 阈值 L* 水平虚线
   ▼
汇总表 + 阈值到达步数（衔接 4.3）
```

#### 4.4.3 源码精读

可视化的全部数据来源就是这一行日志。[examples/toy_train.py:357-359](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L357-L359)：每步写入 `Epoch: ... Step: ... LR: ... Training loss: ...`，由 [L327](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L327) 的 sink 落盘到带优化器与学习率标签的文件。两个值得注意的细节：

- **LR 字段顺手可得**：想核对 cosine 调度形状（热身 100 步、余弦退火，由 [L341-346](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L341-L346) 决定），直接解析日志里的 LR 列画出来即可，记得它超前一步（u1-l3）。
- **发散也能被解析**：loss 为 `nan`/`inf` 时 `float()` 可直接转换（`float('nan')`），matplotlib 默认不画 NaN 点，曲线会在发散处断开——这本身就是最直观的发散标记。

#### 4.4.4 代码实践

**实践目标**：写出「解析 + 平滑 + 绘图 + 汇总」脚本，产出本讲的核心对比图。

**操作步骤**（示例代码，保存为仓库外的独立脚本，如 `plot_sweep.py`）：

```python
# 示例代码：解析扫描日志并绘制对比图
import re
from pathlib import Path

import matplotlib.pyplot as plt

LINE = re.compile(
    r"Step: (\d+) LR: ([\d.eE+-]+) Training loss: ([\d.eE+-]+|nan|inf)"
)

def parse_log(path):
    steps, losses = [], []
    for line in Path(path).read_text().splitlines():
        m = LINE.search(line)
        if m:
            steps.append(int(m.group(1)))
            losses.append(float(m.group(3)))
    return steps, losses

def moving_average(xs, k=100):
    out, acc = [], 0.0
    for i, v in enumerate(xs):
        acc += v
        if i >= k:
            acc -= xs[i - k]
        out.append(acc / min(i + 1, k))
    return out

def final_score(losses, k=100):
    tail = [x for x in losses[-k:] if x == x]      # 剔除 NaN
    return sum(tail) / len(tail) if tail else float("inf")

runs = {}                                           # name -> (steps, losses)
for f in sorted(Path("logs").glob("train_qwen_*.log")):
    runs[f.stem] = parse_log(f)

fig, ax = plt.subplots(figsize=(8, 5))
styles = {"adamw": ("tab:blue", "-"),  "muon": ("tab:red", "-")}
lrdash = {"lr0.0003": (0, (1, 1)), "lr0.001": (0, (5, 3)), "lr0.003": (0, (3, 1, 1, 1))}
best = {}
for name, (steps, losses) in runs.items():
    opt = "adamw" if "adamw" in name else "muon"
    lr_tag = name.split("_lr")[-1]
    color, _ = styles[opt]
    ax.plot(steps, moving_average(losses), color=color,
            linestyle=lrdash.get(lr_tag, "-"), alpha=0.6, label=name)
    score = final_score(losses)
    if score < best.get(opt, (float("inf"), None))[0]:
        best[opt] = (score, name)
for opt, (score, name) in best.items():            # 加粗各自最佳
    steps, losses = runs[name]
    color, _ = styles[opt]
    ax.plot(steps, moving_average(losses), color=color, lw=2.5,
            label=f"best {opt} ({name}, {score:.3f})")
ax.set_xlabel("step"); ax.set_ylabel("training loss (moving avg, k=100)")
ax.set_yscale("log")   # loss 从 ~11.9 降到来量级，对数轴更易读
ax.legend(fontsize=8); ax.set_title("Muon vs AdamW: lr sweep")
fig.savefig("sweep.png", dpi=150, bbox_inches="tight")
print("best:", best)
```

运行 `python3 plot_sweep.py`。

**需要观察的现象**：

- 六条曲线是否都从 \(\approx 11.93\) 出发；
- 同一优化器的三条曲线是否呈「太小-合适-太大（或发散）」的排序；
- 两种优化器的加粗最佳曲线是否分出高下、是否交叉。

**预期结果**：生成 `sweep.png`，图上同色三条虚/点线属于同一优化器的三档学习率，两条加粗实线是各自最佳；控制台打印两个最佳运行名与末段平均 loss（具体数值待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么六条曲线必须用同一个平滑窗口 k？k 取 10 与取 500 各有什么问题？
**答案**：窗口不同的滑动平均不可比——大窗口时滞大、更像全局趋势，小窗口保留更多噪声。k=10 基本没去噪，曲线仍是毛刺，视觉比较失真；k=500 则过度平滑，会把快速下降段的差异抹掉、让「到达阈值步数」产生系统性偏移，且前 500 步都被「半窗口」污染。50~100 是 toy 曲线的常用折中。

**练习 2**：横轴用 step 还是用 token 数（\(8192\times\)step）？两者何时等价？
**答案**：本讲固定 `max_length=512`、`batch_size=16`，两者只差一个常数因子，图的形状完全一样；用 token 数作横轴的好处是单位直接对齐论文口径（样本效率），写报告时建议换算。但一旦对比中改了窗口或 batch，token 横轴才是唯一公平的横轴——这也是 4.1 强调不许动这两个参数的又一原因。

**练习 3**：日志中 `Step` 在每个 epoch 都从 0 开始（[L348](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L348) 的 `enumerate(train_loader)`），本配置下会有问题吗？
**答案**：本配置 `epoch = 1`（[L340](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L340)），只跑一个 epoch，Step 单调无歧义。若把 `epoch` 改成大于 1 再解析日志，需要把 `Epoch` 一并解析并把步号换算成 `epoch * len(train_loader) + step`，否则多轮曲线会叠在同一个横轴区间上。

## 5. 综合实践

把四个模块串成一份完整的**迷你验证报告**（这也是本讲的交付物）：

1. **实验设计**：写下控制变量清单（模型 `qwen`、`hidden_size`、数据集、窗口 512、batch 16、调度 warmup 100 + cosine、种子），声明唯一变量为优化器与学习率；从 [README.md:130-137](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L130-L137) 的官方命令出发构造 2×3 网格。
2. **执行扫描**：完成 4.2.4 的六次运行，保留全部日志（含发散的）。
3. **可视化**：用 4.4.4 脚本产出 `sweep.png` 与末段平均 loss 汇总表。
4. **效率度量**：用 4.3.4 的阈值到达步数法，对 2~3 个阈值 \(L^*\) 计算效率比及其换算 token 数，做一次敏感性检查（换 \(L^*\)、换平滑窗口 k）。
5. **撰写报告**（建议 400 字以内），必须包含：
   - 各优化器最佳学习率与末段 loss；
   - 效率比及其稳健性（是否所有阈值下同号）；
   - 一句诚实的限定语，例如：「在 hidden_size=256、单 epoch 的 toy 规模下，观察到/未观察到 Muon 的样本效率优势；该结果仅对论文 2× 结论做定性呼应，52% 的数字来自多规模 scaling law 拟合，不可由本实验复现」；
   - 对照论文两大技术（权重衰减、update RMS 一致化，[README.md:27](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L27)）说明你测的是「Moonlight 改造版 Muon」。

## 6. 本讲小结

- 公平对比三要素：控制变量（模型/数据/步数/调度/种子）、各自最优（先扫描后比较）、先定度量后跑实验；`get_optimizer` 从同一参数池构造两种优化器、双方 wd 同为 0.1、调度同形状，是代码层面的公平保证，但**脚本未设随机种子**，需自行补上。
- 学习率扫描是对数网格上的「先找各自最佳档位」：日志文件名自带优化器与 lr 标签（[L327](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L327)），每行日志格式唯一（[L357-359](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L357-L359)），解析即可成曲线；注意日志是追加写入，重跑先清理。
- 论文「约 52% 训练 FLOPs / 2 倍样本效率」来自多模型规模的 scaling law 拟合；toy 实验因每步 token 固定为 8192 且每步前向/反向相同，可用「到达同一 loss 的步数之比」做一阶近似，但只能验证方向性、不能复现数字。
- 可视化流程是「正则解析→同一窗口的滑动平均→颜色编码优化器/线型编码 lr→加粗最佳→阈值虚线」；对数纵轴适合从 11.93 起步的 loss 曲线，NaN 断线即是发散标记。
- 实验报告的价值一半在结论、一半在限定语：写清规模、预算、种子与度量定义，才能让「toy 定性呼应」与「论文定量结论」各安其位。

## 7. 下一步学习建议

- **下一讲 u3-l2（定制你的玩具模型：Qwen2Config 配置详解）**：本讲的扫描固定了一个模型规模；下一讲学会缩放 `hidden_size`/层数/`intermediate_size` 后，你可以把本讲的对比从「单点」扩展成「2~3 个规模的小型 scaling 曲线」，向论文 Figure 1(a) 的方法再靠近一步。
- **u3-l4（走向分布式：ZeRO-1 式 Muon 与 Megatron 集成）**：了解论文如何把这里的单卡 Muon 扩展到内存最优的分布式实现。
- 延伸阅读：Moonlight.pdf 的 scaling law 实验章节（对照 [README.md:33-36](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L33-L36) 的 Figure 1），以及 Muon 原始仓库 [KellerJordan/Muon](https://github.com/KellerJordan/Muon)（`toy_train.py` 的来源之一，见 [L46-47](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L46-L47) 的出处注释）中的 speedrun 记录——它们是「小规模、强对比」实验设计的另一个范例。
