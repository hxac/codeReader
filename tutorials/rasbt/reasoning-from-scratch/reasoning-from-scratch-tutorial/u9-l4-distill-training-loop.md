# 蒸馏训练循环：单样本与批处理

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立讲出 `train_distillation` 训练循环的十个步骤（优化器 → 逐 epoch → 洗牌 → 逐样本 → 清梯度 → 算损失 → 反传 → 裁剪 → 步进 → 日志/检查点），并跑通一次小规模冒烟测试。
2. 读懂 `distill_batched.py` 的 `prepare_batch_tensors` 与 `compute_batch_loss`：如何用「左 padding + 注意力掩码 + `IGNORE_INDEX` 损失掩码」三件套，把长度不一的样本拼成一个合法的批。
3. 对比逐样本训练与批处理训练在**梯度语义、速度、显存**三个维度上的取舍。
4. 用 `plot_distill_metrics`（或 pandas + matplotlib）把训练 CSV 日志画成曲线，并解读作者发布的真实蒸馏日志。

本讲是知识蒸馏单元（第 8 章）的第 4 讲：u9-l2 造好了训练样本（`token_ids` + `prompt_len`），u9-l3 实现了单样本损失 `compute_example_loss`，本讲把这些零件装配成**可以反复运行的训练循环**，并给出它的批处理加速版。

## 2. 前置知识

本讲默认你已读过 u9-l2（数据管线）与 u9-l3（蒸馏损失）。在此基础上补充几个训练循环相关的概念：

- **SFT（Supervised Fine-Tuning，监督微调）**：给预训练模型看「输入 + 标准答案」对，用交叉熵把模型在答案上的预测概率推高。本项目的硬蒸馏就是 SFT——教师的回答文本被当作标准答案。
- **epoch（轮）**：把训练集完整过一遍叫一个 epoch。与第 6 章 GRPO 只把每条数据用一次不同，SFT 通常要让模型把同一批样本看多遍，所以训练循环外面多了一层 epoch 循环。
- **优化器四件套**：`optimizer.zero_grad()`（清掉上一步梯度）→ `loss.backward()`（反传计算梯度）→ （可选）梯度裁剪 → `optimizer.step()`（按梯度更新权重）。这是 PyTorch 训练的标准骨架。
- **梯度裁剪（gradient clipping）**：当梯度的 L2 范数超过阈值时整体缩回去，防止个别坏样本把权重推飞。`train_distillation` 里是可选项 `grad_clip_norm`。
- **checkpoint（检查点）**：`torch.save(model.state_dict(), path)` 把模型的全部权重张量存成一个 `.pth` 文件。它是纯权重（不含优化器状态），与第 7 章 GRPO checkpoint 同一格式。
- **验证损失（validation loss）**：在不参与训练的验证集上算平均损失。训练损失基于单个样本、噪声很大；验证损失平滑可比，是判断收敛与过拟合的主曲线。
- **padding（填充）与 IGNORE_INDEX**：一个批里各样本长度不同，必须补齐成同一长度才能堆成矩形张量；补进来的位置不能参与损失，PyTorch 交叉熵约定用标签值 `-100` 表示「忽略该位置」（即 `ignore_index`）。
- **左 padding vs 右 padding**：补在序列前面叫左 padding，补在后面叫右 padding。本讲会看到蒸馏批处理用的是**左 padding**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [reasoning_from_scratch/ch08.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py) | 库版实现：`train_distillation`（逐样本循环）、`save_checkpoint`、`append_csv_metrics`、`plot_distill_metrics`，以及上一讲的 `compute_example_loss` / `evaluate_examples` |
| [ch08/04_train_with_distillation/distill.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill.py) | 独立命令行脚本：argparse 全流程（加载数据 → 建样本 → 切分 → 训练），在库版循环上增加 ETA、tok/sec 与每 epoch 汇总行 |
| [ch08/04_train_with_distillation/distill_batched.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill_batched.py) | 批处理版脚本：`prepare_batch_tensors`（左 padding + 掩码）、`compute_batch_loss`（逐样本平均的批量损失）、`train_distillation_batched`（按长度排序分批） |
| [reasoning_from_scratch/qwen3_batched.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py) | 支持 padding 的批处理 Qwen3 实现（附录 E 的模型），`forward` 接受 `attn_mask`，`distill_batched.py` 直接用它训练 |
| [ch08/03_logs/deepseek-r1-2048_distill_metrics.csv](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/03_logs/deepseek-r1-2048_distill_metrics.csv) | 作者发布的真实训练日志（DeepSeek-R1 教师数据、max_seq_len=2048、3 个 epoch），供无 GPU 的读者画图分析 |
| [ch08/04_train_with_distillation/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/README.md) | 两个脚本的使用说明、示例命令与实验结果表 |
| [ch08/01_main-chapter-code/ch08_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/01_main-chapter-code/ch08_main.ipynb) | 第 8 章主 notebook：8.7 节逐步搭出 `train_distillation` 并跑 10 样例演示 |

## 4. 核心概念与源码讲解

### 4.1 train_distillation：逐样本 SFT 训练循环的十个步骤

#### 4.1.1 概念说明

`train_distillation` 是一个最朴素也最经典的 SFT 训练循环：**每个优化步只吃一个样本**。它解决的问题是：给定 u9-l2 产出的训练样本列表（每条只有 `token_ids` 和 `prompt_len` 两个键）和 u9-l3 的单样本损失，如何让 Qwen3 0.6B 的权重朝着「教师会怎么答」的方向更新，并且在训练过程中留下可复盘的证据（CSV 日志）和可恢复的产物（epoch checkpoint）。

与第 7 章的 `train_rlvr_grpo`（u7-l4）对比，骨架几乎相同，差异只有三处：

| 维度 | GRPO 循环（第 6~7 章） | 蒸馏循环（本章） |
| --- | --- | --- |
| 损失来源 | 每步现场采样 G 个 rollout，再由奖励算优势 | 直接用现成的 `compute_example_loss`（教师文本的交叉熵） |
| 数据使用 | 每条 prompt 基本只用一次 | 同一批样本反复看 `epochs` 遍（外层多一个 epoch 循环） |
| 步数口径 | 一个 step ≈ 一条 prompt | 一个 step = 一个样本；`total_steps = epochs × len(train_examples)` |

#### 4.1.2 核心流程

源码注释把循环明确编号为十个步骤（其中第 5 步的注释写作 "Stage 5"，是源码里的小笔误，不影响逻辑）：

```text
输入: model(已加载权重), train_examples, val_examples, device
      epochs, lr, grad_clip_norm, seed, log_every, checkpoint_dir, csv_log_path

Step 1  创建 AdamW 优化器(lr)；model.train()
        total_steps = epochs × len(train_examples)；global_step = 0
        csv_log_path 未指定时用时间戳生成文件名
for epoch in 1..epochs:                          # Step 2  逐 epoch
    Step 3  复制一份训练列表并用独立随机源 rng(seed) 洗牌
    for example in epoch_examples:               # Step 4  逐样本
        global_step += 1
        Step 5  optimizer.zero_grad()
        Step 6  loss = compute_example_loss(model, example, device)
        Step 7  loss.backward()
                (可选) 按 grad_clip_norm 裁剪梯度
        Step 8  optimizer.step()
        Step 9  若 log_every 非零且 global_step 整除 log_every:
                在验证集上算 val_loss → 打印 → 追加一行 CSV
    Step 10 save_checkpoint 存本 epoch 权重（文件名含全局步数）
返回 model
```

几个值得注意的设计：

- **洗牌用独立随机源** `random.Random(seed)`（[ch08.py:L230](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L230)），不污染全局随机状态，且同种子下打乱顺序可复现。
- **CSV 日志是追加式（append）的**：文件不存在先写表头，之后每次 `log_every` 步追加一行。好处是训练中途崩溃也不丢历史行。
- **checkpoint 按 epoch 存**，文件名把全局步数格式化成五位数字（`step00005`），方便事后按训练进度挑选 checkpoint 做评测。

#### 4.1.3 源码精读

先看循环主体的关键片段。优化器初始化与训练模式设置（Step 1）：

- [reasoning_from_scratch/ch08.py:L224-L235](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L224-L235) — 创建 `AdamW(model.parameters(), lr=lr)`，调用 `model.train()`，计算 `total_steps`，初始化独立随机源 `rng`，并在调用方没给 `csv_log_path` 时按时间戳生成默认文件名。

内层「清梯度 → 算损失 → 反传 → 裁剪 → 步进」五连（Step 5~8）：

- [reasoning_from_scratch/ch08.py:L244-L264](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L244-L264) — 每个样本先 `zero_grad()`，用 u9-l3 精读过的 `compute_example_loss` 得到答案区交叉熵，`backward()` 反传，`grad_clip_norm` 非 `None` 时用 `clip_grad_norm_` 全局裁剪，最后 `optimizer.step()` 更新权重。注意 `zero_grad` 放在循环**开头**而不是 `step()` 之后，这是 PyTorch 的最佳实践写法。

周期性验证与日志（Step 9）：

- [reasoning_from_scratch/ch08.py:L266-L286](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L266-L286) — `global_step % log_every == 0` 时调用 `evaluate_examples`（u9-l3 讲过的 no_grad 验证函数）算 `val_loss`，打印一行进度，并把 `epoch, total_steps, train_loss, val_loss` 四个字段追加进 CSV。注意这里打印的 `train_loss` 是**当前单个样本**的损失，噪声很大。

epoch 检查点（Step 10）与两个配套小函数：

- [reasoning_from_scratch/ch08.py:L288-L295](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L288-L295) — 每个 epoch 结束调用 `save_checkpoint`，后缀 `epoch{n}`。
- [reasoning_from_scratch/ch08.py:L299-L309](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L299-L309) — `save_checkpoint`：`mkdir(parents=True, exist_ok=True)` 建目录，拼出文件名 `qwen3-0.6B-distill-step{step:05d}{-suffix}.pth`，`torch.save(model.state_dict(), ...)` 存纯权重。与第 7 章 GRPO checkpoint 完全同一格式，所以第 3 章评测脚本可以无缝加载它。
- [reasoning_from_scratch/ch08.py:L312-L328](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L312-L328) — `append_csv_metrics`：文件不存在先写表头 `epoch,total_steps,train_loss,val_loss`，然后以追加模式写入一行，损失保留六位小数。

`distill.py` 的脚本版循环是同一骨架的工程增强版，差异集中在三处：

- [ch08/04_train_with_distillation/distill.py:L163-L187](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill.py#L163-L187) — 入口先做参数校验：`log_every` 不能为负、`grad_clip_norm` 给了就必须大于 0，非法值就地 `ValueError`（与 u1-l3 下载入口「本地校验早失败」同一风格）。
- [ch08/04_train_with_distillation/distill.py:L209-L246](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill.py#L209-L246) — 日志行增加两项：`tok/sec`（用本步被监督的 token 数 `len(token_ids) - prompt_len` 除以本步耗时）和复用第 3 章 `eta_progress_message` 算出的 ETA。
- [ch08/04_train_with_distillation/distill.py:L248-L267](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill.py#L248-L267) — 每个 epoch 结束**再追加一行汇总**：`train_loss` 用整个 epoch 的平均（比单样本值平滑得多），并再次算 `val_loss`、存 checkpoint。库版没有这行汇总。

另外注意脚本版的日志与检查点路径是**脚本相对路径**而非当前目录：

- [ch08/04_train_with_distillation/distill.py:L22-L24](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill.py#L22-L24) — `CSV_LOG_PATH = <脚本目录>/logs/distill_metrics.csv`，`CHECKPOINT_DIR = <脚本目录>/checkpoints/distill`，多次实验的产物不会散落在任意的当前目录里。

#### 4.1.4 代码实践：5+2 样本冒烟测试

notebook 的 8.7 节用 10 个样本演示过这个循环（[ch08_main.ipynb 的 8.7 节](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/01_main-chapter-code/ch08_main.ipynb)），作者记录的输出是两行日志加两个 checkpoint。我们照它的思路缩小规模。

1. **实践目标**：在不下载数据全集的情况下跑通 `train_distillation`，确认 CSV 日志与 epoch checkpoint 真的落地。
2. **操作步骤**（以下为示例代码，在本仓库安装好的环境中运行；样本数据用 [ch08/02_generate_distillation_data/math_train_sample.json](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/math_train_sample.json)（仓库自带的小样例文件）：

```python
# 示例代码：冒烟测试 train_distillation
import json, torch
from reasoning_from_scratch.ch02 import get_device
from reasoning_from_scratch.ch03 import load_model_and_tokenizer
from reasoning_from_scratch.ch08 import (
    build_examples, train_distillation, load_reasoning_tokenizer,
)

device = get_device()
model, _ = load_model_and_tokenizer(which_model="base", device=device, use_compile=False)
tokenizer = load_reasoning_tokenizer()

with open("ch08/02_generate_distillation_data/math_train_sample.json") as f:
    data = json.load(f)

examples, skipped = build_examples(data, tokenizer)
examples = examples[:7]                      # 7 条就够了
train_examples, val_examples = examples[:5], examples[5:]

torch.manual_seed(0)
train_distillation(
    model,
    train_examples=train_examples,
    val_examples=val_examples,
    device=device,
    epochs=1,
    lr=5e-6,
    grad_clip_norm=1.0,
    seed=123,
    log_every=2,                             # 每 2 步验证一次
    checkpoint_dir="checkpoints_smoke",
    csv_log_path="smoke_metrics.csv",
)
```

3. **需要观察的现象**：终端在第 2、4 步各打印一行 `[Epoch 1/1 Step x/5] train_loss=... val_loss=...`；第 5 步结束后打印 `Saved checkpoint to ...`。
4. **预期结果**：生成 `checkpoints_smoke/qwen3-0.6B-distill-step00005-epoch1.pth`；`smoke_metrics.csv` 含表头加两行数据（步数 2 和 4）。具体 loss 数值与运行设备有关，待本地验证。
5. 把 `log_every` 改成 `0` 再跑一次，验证 Step 9 被整体跳过（无打印、CSV 不生成），但 epoch checkpoint 依然保存——这是 `if log_every and ...` 短路写法的效果。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `optimizer.zero_grad()` 放在每个样本循环的开头，而不是放在 `optimizer.step()` 之后？

**答案**：两者效果等价——只要保证「上一步的梯度在新的 `backward()` 之前被清零」即可。放开头是 PyTorch 社区惯例：即使某一步异常跳过了 `backward()`，下一次循环开头仍会把陈旧梯度清掉，逻辑上更稳；本仓库第 6 章 GRPO 循环也是同样写法。

**练习 2**：库版 `train_distillation` 的 CSV 里 `train_loss` 列与 `distill.py` 脚本版在 epoch 末尾追加的那一行 `train_loss`，含义有何不同？

**答案**：库版的每行都是**触发日志时当前那一个样本**的损失，噪声大；脚本版 epoch 汇总行的 `train_loss = epoch_train_loss / len(epoch_examples)`，是该 epoch 全部样本损失的**平均**，更平滑、更适合看趋势（见 [distill.py:L248-L260](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill.py#L248-L260)）。

**练习 3**：若把 `epochs=2`、训练样本 5 条，`total_steps` 与 checkpoint 文件名中的步数分别是多少？

**答案**：`total_steps = 2 × 5 = 10`；epoch 1 的 checkpoint 是 `step00005-epoch1`，epoch 2 是 `step00010-epoch2`——步数是**全局累计**的，不随 epoch 归零。

### 4.2 prepare_batch_tensors 与 compute_batch_loss：padding、注意力掩码与损失掩码的组合

#### 4.2.1 概念说明

逐样本训练每步只喂 batch=1 的序列，GPU 大部分算力浪费在「等待」，且每步都要付一次 Python 调度与内核启动开销。批处理把 B 个样本拼成一个矩形张量一次前向，是标准的提速手段——但代价是**样本长度不一**：张量必须是矩形，怎么办？

答案是 padding（补齐）+ 两层掩码，这正是 `distill_batched.py` 的 `prepare_batch_tensors` / `compute_batch_loss` 要解决的问题。它必须同时满足三个约束：

1. **补齐的位置不能干扰注意力**：真实 token 不许「看到」填充符 → 需要**注意力掩码** `attn_mask`（True=真实位置）。
2. **补齐位置不能进损失**：填充位置与 prompt 位置的预测都不该被监督 → 需要**损失掩码**（标签填 `IGNORE_INDEX = -100`）。
3. **位置编码要自洽**：RoPE 只关心相对距离，所以选择**左 padding**——真实 token 靠右对齐，序列之间真实 token 的相对距离保持连续，且每行最后一个真实 token 恰好都在最右列（这对生成任务尤其重要，详见 u10-l2）。

三者的分工一句话：**pad_id 负责把张量填成矩形，attn_mask 负责让模型看不见填充符，IGNORE_INDEX 负责让损失看不见不该监督的位置。**

#### 4.2.2 核心流程

`prepare_batch_tensors` 把一个 batch 的样本列表变成四个张量：

```text
输入: batch_examples（各含 token_ids, prompt_len），pad_id，device
1. max_input_len = max(len(token_ids) - 1)        # 输入序列是 token_ids[:-1]
2. input_ids = 全 pad_id 的 (B, max_input_len) 矩形
   attn_mask = 全 False 的 (B, max_input_len)
   labels    = 全 IGNORE_INDEX 的 (B, max_input_len)
3. 逐行 i：
   input_seq = token_ids[:-1]；target_seq = token_ids[1:]
   offset = max_input_len - len(input_seq)         # 该行左边的 pad 数
   input_ids[i, offset:] = input_seq               # 真实输入靠右填
   attn_mask[i, offset:] = True
   labels[i, offset:]   = target_seq               # 先整段填目标
   answer_start = max(prompt_len - 1, 0)
   labels[i, offset : offset+answer_start] = IGNORE_INDEX   # 再把 prompt 段掩掉
4. 顺带累计 supervised_token_count（供 tok/sec 统计）
输出: input_ids, attn_mask, labels, supervised_token_count
```

`compute_batch_loss` 在其上算**宏平均**批量损失（先样本内平均、再批内平均）：

\[
\mathcal{L}_{\text{batch}} = \frac{1}{B}\sum_{i=1}^{B}\frac{1}{|S_i|}\sum_{t \in S_i} \text{CE}_{i,t}, \qquad S_i = \{t : \text{labels}_{i,t} \ne -100\}
\]

其中 \(\text{CE}_{i,t}\) 是第 \(i\) 个样本第 \(t\) 个位置对真实目标 token 的交叉熵。这个「样本内先平均」的口径与单样本版 `compute_example_loss` 完全一致——因此批处理不是逐样本的近似，而是**语义等价的向量化实现**：长样本不会因为 token 多就在梯度里嗓门更大。

#### 4.2.3 源码精读

`IGNORE_INDEX` 常量与批切分器：

- [ch08/04_train_with_distillation/distill_batched.py:L28](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill_batched.py#L28) — `IGNORE_INDEX = -100`，即 PyTorch `cross_entropy` 的默认 `ignore_index`，此处显式声明以便复用。
- [ch08/04_train_with_distillation/distill_batched.py:L164-L166](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill_batched.py#L164-L166) — `iter_batches` 是一个生成器，按 `batch_size` 切片产出子列表；最后一个批可能不足额，代码各处都用 `len(batch_examples)` 而非 `batch_size` 来计数。

三张张量的初始化：

- [ch08/04_train_with_distillation/distill_batched.py:L169-L192](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill_batched.py#L169-L192) — 先检查 `pad_id` 非 None（没有 pad token 就无法批处理，就地报错），算 `max_input_len`，然后 `torch.full` 一次性建好三个矩形：`input_ids` 全填 `pad_id`、`attn_mask` 全 False、`labels` 全 `IGNORE_INDEX`。「先全部填默认值、再逐行覆写真实段」是构造 padding 张量的惯用法。

逐行左对齐填充与 prompt 段掩码：

- [ch08/04_train_with_distillation/distill_batched.py:L194-L216](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill_batched.py#L194-L216) — 对每行：错位构造 `input_seq = token_ids[:-1]`、`target_seq = token_ids[1:]`（u9-l3 的同款约定）；`offset = max_input_len - seq_len` 是该行左边的 pad 列数；三张张量都只写 `[offset:]` 右半段。随后 `answer_start = max(prompt_len - 1, 0)`，把 `labels[i, offset:offset+answer_start]` 重置为 `IGNORE_INDEX`——即**桥接位置之前的 prompt 区不监督**。最终该行被监督的标签恰好是全部答案 token（含 eos）各一次，与单样本版切片完全等价。`supervised_token_count` 累加 `len(token_ids) - prompt_len` 供吞吐统计。

批量损失的计算：

- [ch08/04_train_with_distillation/distill_batched.py:L221-L242](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill_batched.py#L221-L242) — `compute_batch_loss` 先调 `prepare_batch_tensors`，再 `model(input_ids, attn_mask=attn_mask)` 一次前向拿到 `(B, T, V)` 的 logits；`logits.transpose(1, 2).float()` 变成 `(B, V, T)` 并升 fp32（交叉熵对数值精度敏感）；`cross_entropy(..., ignore_index=IGNORE_INDEX, reduction="none")` 返回**逐 token** 损失（被忽略位置为 0）；接着 `active_mask = labels.ne(IGNORE_INDEX)` 重新数出每行活跃 token 数，先按行求和除以行数（`.clamp(min=1)` 防零除），再对批求均值——正是上面的宏平均公式。

注意力掩码在模型侧的消费（为什么 `attn_mask` 传进去就够）：

- [reasoning_from_scratch/qwen3_batched.py:L95-L107](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py#L95-L107) — 批处理模型先把因果掩码扩成 4D，再检测到存在 pad 时，把 `attn_mask==0` 的**键位置**并入掩码（`mask = causal4d | kpm`），保证 softmax 分母里根本没有填充符。
- [reasoning_from_scratch/qwen3_batched.py:L111-L114](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py#L111-L114) — 同时把 pad 的**查询行**的 embedding 乘零置空，这些行的输出是无意义的，但它们的标签本来就是 `IGNORE_INDEX`，不会进损失。位置编码方面（[qwen3_batched.py:L109](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py#L109)），每行真实 token 占据连续位置，RoPE 只依赖相对距离，故左 padding 不改变行内相对几何（该模型的完整生成语义留到 u10-l2）。

验证侧的批处理版：

- [ch08/04_train_with_distillation/distill_batched.py:L245-L267](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill_batched.py#L245-L267) — `evaluate_examples_batched` 逐批调用 `compute_batch_loss`，按**实际批大小**加权求宏平均（`total_loss += batch_loss.item() * batch_size_actual`），最后除以样本总数，与单样本版 `evaluate_examples` 的口径一致，同样有 `no_grad` 装饰器与 train/eval 模式恢复。

#### 4.2.4 代码实践：手工画出 batch_size=3 的 padding 布局

1. **实践目标**：不依赖模型与 GPU，彻底吃透 `prepare_batch_tensors` 的输出布局。
2. **操作步骤**：先手算，再用最小脚本验证（示例代码，pad_id 任取一个如 `151643`）：

```python
# 示例代码：验证 prepare_batch_tensors 的输出布局
import sys
sys.path.append("ch08/04_train_with_distillation")  # 独立脚本不在包内，需手动加路径
from distill_batched import prepare_batch_tensors

batch = [
    {"token_ids": list(range(100, 110)), "prompt_len": 6},  # 总长 10, prompt 6
    {"token_ids": list(range(200, 207)), "prompt_len": 4},  # 总长 7,  prompt 4
    {"token_ids": list(range(300, 305)), "prompt_len": 3},  # 总长 5,  prompt 3
]
input_ids, attn_mask, labels, supervised = prepare_batch_tensors(batch, pad_id=151643, device="cpu")
print(input_ids.shape, supervised)
print(attn_mask.int())
print(labels)
```

3. **手算对照**（max_input_len = 9；□=pad，I=IGNORE_INDEX 即 -100，P=prompt 段目标，A=答案段目标）：

```text
attn_mask (True=真实token)：          labels（P 段被掩成 I，A 段保留）：
行0: 1 1 1 1 1 1 1 1 1   (offset=0)   行0: I I I I I A A A A   (answer_start=5)
行1: 0 0 0 1 1 1 1 1 1   (offset=3)   行1: □ □ □ I I I A A A   (answer_start=3)
行2: 0 0 0 0 0 1 1 1 1   (offset=5)   行2: □ □ □ □ □ I I A A   (answer_start=2)
      ←— pad 区 —→←真实区→           supervised_token_count = 4+3+2 = 9
```

   每行 `A` 的个数 = `len(token_ids) - prompt_len`（含 eos），行 0/1/2 分别是 4、3、2，合计 9。
4. **预期结果**：`input_ids.shape == torch.Size([3, 9])`；`attn_mask` 每行 True 的个数 = 9/6/4；`labels` 中等于 `-100` 的位置 = 全部 pad 列 + 各行 prompt 段；`supervised == 9`。待本地验证。
5. **延伸观察**：三行长度差距越大，pad 列（全 0 区）越多、白算的算力越多——这正是 4.3 节「按长度排序」动机的直观来源。

#### 4.2.5 小练习与答案

**练习 1**：如果改成**右** padding（真实 token 靠左、pad 在尾部），`prepare_batch_tensors` 的哪些部分需要改？会引入什么新问题？

**答案**：填充位置从 `offset:` 变成 `:seq_len`，`attn_mask`/`labels` 相应改写。但因果掩码下右 padding 的真实 token 会把尾部 pad 当作「更晚的 token」的键——训练时这些位置被掩掉问题不大，可每行**最后一个真实 token**不再对齐在同一列，生成任务（逐 token 解码、取末位 logits）就需要额外的长度簿记。左 padding 让所有序列右对齐，是训练与生成共用同一实现的更省心选择。

**练习 2**：为什么 `compute_batch_loss` 不直接对全部活跃 token 求一个总平均（微平均），而要先按样本平均再按批平均（宏平均）？

**答案**：宏平均让每个样本对损失的贡献权重相同，与逐样本训练「一步一个样本、样本无权重差异」的语义一致，两种实现的损失数值可直接对比；微平均则会让 token 多的长答案主导梯度（一个 2000 token 的样本影响是 200 token 样本的 10 倍），隐式改变了样本权重。两种口径都有人用，但要清楚自己选了哪种。

**练习 3**：`compute_batch_loss` 里 `cross_entropy` 已经传了 `ignore_index=IGNORE_INDEX`，为什么后面还要 `active_mask = labels.ne(IGNORE_INDEX)` 再算一遍？

**答案**：`reduction="none"` 只把被忽略位置的损失置 0，并不帮我们求平均；要实现「按行平均」，必须自己知道每行有多少个活跃位置（`active_mask.sum(dim=1)`），所以需要重新构造这个掩码来同时完成筛选（乘法清零）与计数（按行求和）。

### 4.3 train_distillation_batched 与两个脚本的取舍：速度、显存与梯度语义

#### 4.3.1 概念说明

`distill.py` 与 `distill_batched.py` 是同一任务的两种工程实现：前者逐样本更新（batch=1），后者把 B 个样本拼批更新。理解取舍前先明确一个容易忽略的事实——**批处理改变的不仅是速度，还有优化轨迹**：

- 逐样本版每个样本触发一次 `optimizer.step()`，5 个样本 = 5 次更新；
- 批处理版一个批只触发一次 `optimizer.step()`，梯度是批内 B 个样本宏平均损失的梯度，5 个样本（batch_size=5）= 1 次更新。

也就是说 batch_size 同时是**吞吐旋钮**和**优化超参数**（等效地，逐样本版相当于 batch_size=1 且步数更多的 SGD 路径）。此外批处理引入 padding 计算浪费与更大的激活显存，是一组「速度 ↔ 显存 ↔ 优化语义」的三方权衡。

#### 4.3.2 核心流程

`train_distillation_batched` 的循环结构与 4.1 完全同构，改动点只有四处：

```text
1. steps_per_epoch = ceil(len(train) / batch_size)     # 步数口径从样本数变批数
2. 每 epoch：先 rng.shuffle 再按长度稳定排序           # 相邻样本长度接近，减少 pad
3. 内层循环对象从 example 变成 batch（iter_batches 切片）
4. 损失与验证分别换成 compute_batch_loss / evaluate_examples_batched
   （两者都多一个 pad_id 参数）
```

#### 4.3.3 源码精读

步数口径与参数校验：

- [ch08/04_train_with_distillation/distill_batched.py:L286-L301](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill_batched.py#L286-L301) — 开头校验 `batch_size > 0`、`log_every >= 0` 等；关键是 `steps_per_epoch = (len(train_examples) + batch_size - 1) // batch_size`，即向上取整除法，`total_steps = epochs * steps_per_epoch`。

洗牌 + 按长度排序（关键的吞吐技巧）：

- [ch08/04_train_with_distillation/distill_batched.py:L303-L308](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill_batched.py#L303-L308) — 每个 epoch 先 `rng.shuffle` 再 `sort(key=len(token_ids))`。Python 排序是**稳定**的：同长度样本保持洗牌后的随机相对顺序，跨 epoch 仍各不相同；而排序让相邻样本长度接近，批内 `max_input_len` 由组内最长者决定，pad 浪费被压到最小。若想保留完全随机顺序，可用 `--no_sorting` 关闭。

批循环与损失：

- [ch08/04_train_with_distillation/distill_batched.py:L313-L332](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill_batched.py#L313-L332) — 与逐样本版五连完全相同的 `zero_grad → compute_batch_loss → backward → (clip) → step`，只是对象换成批；epoch 损失按实际批大小加权累计（`epoch_train_loss += loss.item() * batch_size_actual`），保证 epoch 汇总行仍是样本宏平均。

模型加载路径（注意它换了模型类）：

- [ch08/04_train_with_distillation/distill_batched.py:L19-L23](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill_batched.py#L19-L23) — 从 `reasoning_from_scratch.qwen3_batched` 导入模型与加载函数：批处理需要能接受 `attn_mask` 的实现。
- [ch08/04_train_with_distillation/distill_batched.py:L518-L531](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill_batched.py#L518-L531) — 无论从 checkpoint 还是基座初始化，都显式传 `float32_upcast=False`。批处理模型默认把注意力分数路径升 fp32（数值更稳但更慢更耗显存，见 [qwen3_batched.py:L31-L45](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py#L31-L45) 的文档说明），训练时作者选择关掉它换速度与显存。

`distill.py` 的 argparse 全流程（两个脚本共享同一套参数面）：

- [ch08/04_train_with_distillation/distill.py:L307-L391](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill.py#L307-L391) — 参数一览：`--data_path`（输入 JSON）、`--dataset_size`（前缀截断，0=全部）、`--validation_size`（验证集**绝对条数**，默认 25）、`--epochs`、`--lr`、`--max_seq_len`（超长过滤阈值）、`--checkpoint_path`（从旧 checkpoint 续训，只恢复权重不恢复优化器状态）、`--grad_clip_norm`、`--seed`、`--use_think_tokens`（改用 reasoning 分词器并给目标答案包 `<think>` 标签，与库版 `format_distilled_answer` 的行为开关联动）。
- [ch08/04_train_with_distillation/distill.py:L286-L304](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill.py#L286-L304) — `split_data`：洗牌后按绝对条数切出验证集，与 notebook 里 `val = filtered[:25]` 的做法一致但封装成了带校验的函数。
- [ch08/04_train_with_distillation/distill.py:L394-L435](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill.py#L394-L435) — `main` 的数据准备段：加载 JSON →（可选）截断 →（可选）加载续训权重 → `build_examples` 编码 → `filter_examples_by_max_len` 过滤 → `split_data` 切分，全程打印各阶段数量（原始行数 / 跳过行数 / 过滤行数 / 最终 train/val 条数）。
- [ch08/04_train_with_distillation/README.md:L149-L184](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/README.md#L149-L184) — 作者的真实实验结论表：DeepSeek-R1 教师数据 1/2/3 个 epoch 的 MATH-500 准确率为 30.6%/32.4%/33.6%，最终验证损失 0.5436/0.5349/0.5343；Qwen3-235B 教师数据为 45.0%/43.8%/44.2%。整个训练「H100 上约 30 分钟、DGX Spark 上约 3 小时、峰值约 15 GB 内存」。可以看到：蒸馏把 0.6B 基座的 15.2% 一路推到 33.6%（R1 教师）甚至 45.0%（Qwen3-235B 教师），且验证损失在第 2 个 epoch 后基本走平——继续加 epoch 收益很小。

#### 4.3.4 代码实践：阅读 + 对比（源码阅读型）

1. **实践目标**：把两个脚本的差异整理成一张可查阅的对照表，并能解释每条差异的动机。
2. **操作步骤**：并排打开 `distill.py` 与 `distill_batched.py`，沿「导入 → 模型加载 → build_examples → 训练函数 → parse_args → main」的顺序逐段对照，填写下表（示例答案已给出，请自行到源码里核对出处行号）：

| 维度 | distill.py（逐样本） | distill_batched.py（批处理） |
| --- | --- | --- |
| 模型类 | `ch03.load_model_and_tokenizer`（标准 qwen3） | `qwen3_batched.Qwen3Model`（支持 attn_mask），且 `float32_upcast=False` |
| 每步处理 | 1 个样本，1 次 `optimizer.step()` | B 个样本，1 次 `optimizer.step()` |
| 损失函数 | `compute_example_loss`（切片法，u9-l3） | `compute_batch_loss`（IGNORE_INDEX 掩码法，语义等价） |
| 步数口径 | 样本数 | 批数（向上取整） |
| 特有参数 | — | `--batch_size`、`--no_sorting` |
| 长度排序 | 无 | 每 epoch 洗牌后按长度稳定排序（减少 pad） |
| 吞吐统计 | tok/sec（单样本 token 数/耗时） | tok/sec（`supervised_token_count`/耗时） |

3. **需要观察的现象**：核对表中每一格都能在源码里找到对应行；特别确认两个脚本的 `compute_example_loss`（逐样本版）逐字相同——批处理脚本保留它是为了兼容无 pad 场景的可读参照。
4. **预期结果**：完成对照表后，回答一个问题：同一份数据、同 `lr` 下，把 `batch_size` 从 1 调到 8，单位 epoch 的 `optimizer.step()` 次数变为几分之一？（答案：约 1/8，因此若想保持更新次数不变需要相应调整 `epochs` 或 `lr`——这正是「batch_size 也是优化超参数」的含义。）

#### 4.3.5 小练习与答案

**练习 1**：`sort_by_length` 为什么要在 `shuffle` **之后**做？直接按长度排序一次、每个 epoch 复用不行吗？

**答案**：排序是稳定的，先洗牌能随机化同长度样本的相对顺序，使每个 epoch 的批内组合仍随种子变化；若全程只按长度排一次序，每个 epoch 的批组成完全相同，样本配对的系统性偏差会被反复强化。另外 shuffle+sort 让「长度相近」与「顺序随机」两个目标同时成立。

**练习 2**：README 提到批处理版「uses much more GPU memory (depending on the batch size)」。显存开销主要涨在哪几项？

**答案**：激活值随批内 token 总数（`batch_size × max_input_len`，含 pad 列）线性增长，前向中最大的张量之一是 logits（`B × T × 151936` 个元素，本模型词表极大），反向还需保存中间激活供求导；而逐样本版同一时刻只有一条序列的激活。权重与优化器状态两边相同。

**练习 3**：`--checkpoint_path` 续训时「Optimizer and step state are not restored」（参数帮助原文）。这意味着什么？

**答案**：它只 `load_state_dict` 恢复**模型权重**（见 [distill.py:L420-L422](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill.py#L420-L422)），AdamW 的一阶/二阶动量与全局步数都从零开始。续训的学习率预热行为与真正中断续跑并不完全等价，定长训练分段跑时要意识到这一点。

### 4.4 plot_distill_metrics：训练曲线可视化与真实日志解读

#### 4.4.1 概念说明

训练循环留下的 CSV 是「黑匣子」，`plot_distill_metrics` 把它变成一张诊断图：横轴全局步数，纵轴损失，两条曲线分别是 `train_loss`（半透明，因为噪声大）与 `val_loss`（实线），并在主轴下方追加一条**第二横轴标注 epoch 边界**。学会看这张图，就能回答 SFT 最常见的三个问题：收敛了吗？过拟合了吗？该停在第几个 epoch？

#### 4.4.2 核心流程

```text
1. csv.DictReader 逐行读 CSV → 收集 total_steps / train_losses / val_losses
2. epoch_bounds 字典：epoch → [首步, 末步]（setdefault 技巧，首次遇到记首步，之后不断刷新末步）
3. 画两条曲线（train 用 alpha=0.3 弱化）
4. secondary_xaxis("bottom") 再加一条 epoch 轴：
   刻度位置 = 每个 epoch 首末步的中点，刻度标签 = epoch 号
```

#### 4.4.3 源码精读

- [reasoning_from_scratch/ch08.py:L331-L341](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L331-L341) — 读 CSV 并同步构建 `epoch_bounds`：`setdefault(epoch, [step, step])[1] = step` 一行完成「没有就建、有了就更新末步」。
- [reasoning_from_scratch/ch08.py:L343-L349](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L343-L349) — 画图主体：`train_loss` 用 `alpha=0.3` 半透明，视觉上暗示「噪声大、仅供参考」；`val_loss` 全亮，是主曲线。
- [reasoning_from_scratch/ch08.py:L350-L359](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L350-L359) — 第二横轴：刻度放在每个 epoch 步数区间的中点，标签是 epoch 号，轴线下移 45 pt 避免与主轴重叠。
- [ch08/03_logs/deepseek-r1-2048_distill_metrics.csv:L1-L5](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/03_logs/deepseek-r1-2048_distill_metrics.csv#L1-L5) — 真实日志开头：表头 `epoch,total_steps,train_loss,val_loss`，第一行数据是 epoch 1、step 50、train 0.765625、val 0.698918。整个文件 404 行（1 表头 + 403 数据行），对应 3 个 epoch、每 50 步一条的 step 日志再加每 epoch 一条汇总行。

notebook 8.8 节对这张图的解读（作者的定性结论，记录在 [ch08_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/01_main-chapter-code/ch08_main.ipynb)）：训练损失基于单样本所以很抖；验证损失**先陡降、随后进入平台期**——这与 README 实验表中 val loss 从 0.5436（epoch 1）到 0.5343（epoch 3）只微降、MATH-500 准确率增益递减完全吻合。工程结论：epoch 2 之后基本可以停了。

#### 4.4.4 代码实践：画作者的真实训练曲线（无需 GPU）

1. **实践目标**：不训练，直接消费作者发布的 CSV，复现诊断图并读出结论。
2. **操作步骤**（示例代码，仓库内文件路径直接可读）：

```python
# 示例代码：两种画法任选
# ① 库函数（需要 matplotlib，会弹图/内联显示）
from reasoning_from_scratch.ch08 import plot_distill_metrics
plot_distill_metrics("ch08/03_logs/deepseek-r1-2048_distill_metrics.csv")

# ② pandas 复刻（无交互环境时保存 PNG）
import pandas as pd, matplotlib.pyplot as plt
df = pd.read_csv("ch08/03_logs/deepseek-r1-2048_distill_metrics.csv")
fig, ax = plt.subplots(figsize=(7, 4))
ax.plot(df.total_steps, df.train_loss, alpha=0.3, label="train_loss")
ax.plot(df.total_steps, df.val_loss, label="val_loss")
ax.set_xlabel("Total Steps"); ax.set_ylabel("Loss"); ax.legend()
fig.savefig("distill_curve.png", dpi=150, bbox_inches="tight")
print(df.groupby("epoch").val_loss.min())   # 各 epoch 的最佳验证损失
```

3. **需要观察的现象**：train_loss 曲线剧烈抖动（单样本噪声）；val_loss 前 1/3 段快速下降，之后趋平。
4. **预期结果**：`groupby("epoch")` 的 val_loss 最小值随 epoch 递增只微降（README 记录的最终 val_loss 为 0.5436 → 0.5349 → 0.5343）；对照 README 实验表第 3~5 行的准确率 30.6% → 32.4% → 33.6%，写出你自己的「早停建议」。具体像素图待本地验证。
5. **附加观察**：数一数 CSV 中同一 `epoch` 值的最大 `total_steps`，估算该次训练的实际训练集大小（每 epoch 步数 = 样本数；README 的 checkpoint 文件名 `step06682-epoch1` 印证了这一口径）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `train_loss` 用 `alpha=0.3` 画而 `val_loss` 用默认不透明？

**答案**：逐样本训练损失是单点估计，方差大、锯齿重，半透明呈现避免它抢走视觉焦点；验证损失是多样本平均（25 条），平滑可比，才是判断收敛与过拟合的主信号。

**练习 2**：如果 val_loss 持续下降而 train_loss 已经几乎不动，这通常说明什么？反之 val_loss 开始回升呢？

**答案**：前者常见于欠拟合未结束或数据充足时的正常收敛段；后者是过拟合的经典信号——模型开始记忆训练样本 specifics，泛化变差，应当参考 epoch 边界轴在回升前的 checkpoint 停止（本数据集三个 epoch 内未明显出现过拟合拐点）。

**练习 3**：`epoch_bounds` 为什么用 `setdefault(epoch, [step, step])[1] = step` 而不是先判断键是否存在？

**答案**：`setdefault` 在键不存在时插入默认值并返回它，存在时直接返回现有列表——一行同时完成「初始化首步」与「刷新末步」，比 if/else 分支更紧凑；这是处理「首见即最小、再见只更新最大」这类聚合的惯用技巧。

## 5. 综合实践：单样本 vs 批处理的对照小实验

把本讲三个模块串成一个端到端任务（在能加载 0.6B 模型的环境里做；纯 CPU 也可，只是慢）：

1. **准备**：用 `load_distill_data`（或仓库自带的 `ch08/02_generate_distillation_data/math_train_sample.json`）+ `build_examples` 产出约 10~20 个样本，`filter_examples_by_max_len(max_len=512)` 压短，切 8 条训练、2 条验证。
2. **跑逐样本版**：`train_distillation(..., epochs=1, lr=5e-6, log_every=2, grad_clip_norm=1.0, csv_log_path="single.csv")`，用 `time` 模块记录总耗时。
3. **跑批处理版**：把 `distill_batched.py` 中的 `prepare_batch_tensors`、`compute_batch_loss`、`train_distillation_batched` 导入（或直接以命令行方式运行该脚本，参数参考 [README 的示例命令](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/README.md#L55-L98)：`--dataset_size 10 --validation_size 2 --epochs 1 --batch_size 4 --log_every 1`），`batch_size=4`，同样记录耗时与 `logs/distill_batched_metrics.csv`。
4. **对照分析**：
   - 把两份 CSV 用 `plot_distill_metrics` 或 pandas 各画一条 val_loss 曲线；
   - 记录两版的总耗时与（GPU 下的）峰值显存（批处理脚本 main 末尾自带 `torch.cuda.max_memory_allocated()` 打印，见 [distill_batched.py:L601-L603](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/distill_batched.py#L601-L603)）；
   - 回答：样本这么少时哪版更快？为什么小规模下批处理的优势可能显现不出来（内核启动开销占比低、pad 浪费占比高、GPU 未喂饱）？
5. **预期结论方向**：小样本下两版耗时接近甚至逐样本更快；差异要到大训练集、大 batch、GPU 喂饱后才拉开——README 记录的 15 GB 显存 / 30 分钟（H100）才是它的设计工况。数值待本地验证。

## 6. 本讲小结

- `train_distillation` 的十步骨架：建优化器 → 逐 epoch → 洗牌 → 逐样本 → `zero_grad` → `compute_example_loss` → `backward` → （可选裁剪）→ `step` → 周期验证 + CSV 日志 + epoch checkpoint；与第 7 章 GRPO 循环同构，差别只在损失来源与 epoch 复用数据。
- CSV 日志（追加式四列）+ `.pth` checkpoint（纯 `state_dict`，文件名内嵌全局步数）构成训练黑匣子；脚本版 `distill.py` 在此之上增加参数校验、tok/sec、ETA 与每 epoch 汇总行。
- 批处理的三件套：**pad_id 填矩形、attn_mask 屏蔽填充键、`IGNORE_INDEX=-100` 屏蔽非监督标签**；左 padding 让真实 token 右对齐、相对位置连续，桥接位置 `prompt_len-1` 之前的 prompt 段一律不监督——与单样本切片法语义等价。
- `compute_batch_loss` 是宏平均（先样本内平均、再批内平均），长答案不会主导梯度；`batch_size` 既是吞吐旋钮也是优化超参数（改变每 epoch 的更新次数）。
- 批处理的配套技巧：每 epoch 洗牌后按长度稳定排序以减少 pad 浪费（`--no_sorting` 可关）；用 `qwen3_batched` 模型并以 `float32_upcast=False` 换显存与速度。
- `plot_distill_metrics` 用半透明 train 曲线 + 实线 val 曲线 + epoch 第二横轴呈现训练动态；作者真实日志显示 val_loss 一轮内陡降后趋平，与「epoch 2 后收益递减」的实验结论互相印证。

## 7. 下一步学习建议

- **下一讲 u9-l5（与 Hugging Face 生态互通）**：本讲产出的蒸馏 checkpoint 将通过 `download_qwen3_distill_checkpoints` 下载、并经 wrapper / export 两条路线接入 `transformers`，是蒸馏单元的收尾。
- **想吃透批处理模型的生成语义**（left padding 下的位置 id、按行收缩 KV cache 的 `shrink_kv_cache_inplace`），直接预习 u10-l2（`qwen3_batched` 与批量推理），本讲的 `attn_mask` 消费是它的训练侧入口。
- **对照第 6~7 章训练循环**重读一遍 `train_rlvr_grpo`（u7-l4）：同一套「日志 + checkpoint + 验证」工程模式在 SFT 与 RL 两个场景的复用与变形，是本仓库训练代码最有迁移价值的设计。
- 延伸阅读：仓库 [ch08/04_train_with_distillation/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/04_train_with_distillation/README.md) 的「Evaluating a distilled checkpoint」一节，看如何把本讲的 epoch checkpoint 交给第 3 章评测脚本量化效果。
