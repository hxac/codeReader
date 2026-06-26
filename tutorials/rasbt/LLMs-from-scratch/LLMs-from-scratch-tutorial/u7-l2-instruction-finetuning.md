# 指令微调训练与响应生成

## 1. 本讲目标

在上一讲（[u7-l1](u7-l1-instruction-data-collate.md)）里，我们把原始的「指令—回答」数据整理成了模型能吃的 `(inputs, targets)` 批次：用 Alpaca 模板拼好文本、用 `custom_collate_fn` 做按批填充、并用 `ignore_index=-100` 屏蔽掉纯填充位置。**数据已经就绪，但模型还是那个「只会续写、不会听指令」的预训练 GPT。**

本讲就负责把这个差距补上：把 u7-l1 准备好的数据，喂进加载了 OpenAI 预训练权重的 GPT 模型里做训练，让它学会「听指令、写回答」，最后把测试集上的回答生成出来、存下来。

学完本讲，你应该能够：

- 说清楚「指令微调（instruction finetuning）」为什么是在**预训练权重**之上做的小步调整，以及它和第 5 章预训练、第 6 章分类微调的关系。
- 解释为什么这里能**原封不动复用**第 5 章的 `train_model_simple` 训练循环，以及指令微调「复用的是算法、换的是数据与掩码」这一核心思想。
- 读懂训练循环里的损失计算、优化器配置与每个 epoch 末的样本生成监控，并能解读训练/验证损失曲线。
- 独立在测试集上批量生成模型回答、切掉输入前缀、把回答写回 JSON，并用 `state_dict` 保存微调后的模型。

## 2. 前置知识

本讲承接多个已建立的认知，先用大白话把关键概念串一遍：

- **预训练 vs 微调**：预训练（第 5 章）让模型在海量无标注文本上学「续写下一个词」；微调是在已经学好的模型上，用**少量、有标注**的数据做小幅调整，让它适配某个具体任务。指令微调属于微调的一种——任务是「听懂人类指令并作答」。
- **加载 OpenAI GPT-2 权重**：我们在 [u5-l4](u5-l4-weight-loading.md) 已经掌握了 `download_and_load_gpt2` 下载、`load_weights_into_gpt` 把权重逐层翻译进自建 `GPTModel` 的整套流程。本讲直接拿来用，不再重复其内部细节。其中两项对齐 OpenAI 的关键配置 `context_length=1024`、`qkv_bias=True` 仍是硬性要求。
- **训练循环四件套**：`optimizer.zero_grad()` → 前向算 loss → `loss.backward()` 反传梯度 → `optimizer.step()` 更新权重。这套范式在 [u5-l2](u5-l2-training-loop.md) 已讲透，本讲的 `train_model_simple` 就是它的封装。
- **解码策略与 `generate`**：[u5-l3](u5-l3-decoding-strategies.md) 实现了支持温度（temperature）、top-k 采样、eos 提前停止的 `generate` 函数。本讲在生成回答时会再次用到它。
- **u7-l1 的掩码约定**：`custom_collate_fn` 把纯填充 token 在 `targets` 里替换成 `-100`，并**保留第一个** `<|endoftext|>`（50256）作为「回答结束」信号。PyTorch 的 `cross_entropy` 默认 `ignore_index=-100`，会自动跳过这些位置。本讲的损失计算正是建立在这个掩码之上的。

> 一句话：本讲不引入新的训练算法，而是把「第 5 章的训练循环 + u5-l4 的权重加载 + u7-l1 的指令数据」三件已有积木拼起来，完成一次端到端的指令微调。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `ch07/01_main-chapter-code/gpt_instruction_finetuning.py` | **本讲主文件**。自包含的指令微调脚本，把数据准备、模型加载、训练、生成、保存串成一个 `main()`。是 notebook 第 7.5–7.7 节的「精简汇总版」。 |
| `ch07/01_main-chapter-code/previous_chapters.py` | 前序章节代码汇总器。本讲复用其中的 `GPTModel`、`load_weights_into_gpt`、`train_model_simple`、`generate`、`calc_loss_loader`、`text_to_token_ids`、`token_ids_to_text` 等。 |
| `ch07/01_main-chapter-code/ch07.ipynb` | 第 7 章正文 notebook，含完整的运行输出（损失曲线、生成样本、训练时长表），是验证脚本行为的「真值参照」。 |
| `ch07/01_main-chapter-code/gpt_download.py` | 下载 OpenAI GPT-2 TensorFlow checkpoint 的工具（u5-l4 已讲，本讲间接通过 `download_and_load_gpt2` 调用）。 |

**判断这份脚本「复用了谁」的小技巧**（见 [u1-l3](u1-l3-repo-reading-map.md)）：`gpt_instruction_finetuning.py` 开头有 `from previous_chapters import ...`，属于「依赖模块型」汇总脚本——它必须和 `previous_chapters.py`、`gpt_download.py` 放在同一目录才能运行。

## 4. 核心概念与源码讲解

本讲按三个最小模块推进：**① 加载预训练权重 → ② 复用训练循环微调 → ③ 生成并保存回答**，正好对应脚本 `main()` 里三段被注释隔开的代码块。

### 4.1 加载预训练权重并微调为指令模型

#### 4.1.1 概念说明

指令微调的核心前提是：**我们不想、也不需要从零训练一个模型**。从零训练一个能听懂指令的 LLM 需要海量数据和巨大算力；而 OpenAI 已经公开了训练好的 GPT-2 权重。我们直接把这些权重加载进来，模型就「天生」具备了一定的语言能力，我们只需要用 1100 条指令数据做**小幅微调**，让它从「会写文章」变成「会答题」。

这里有一个关键选择：**用多大的模型？** 本讲用 `gpt2-medium (355M)`，而不用更小的 `gpt2-small (124M)`。notebook 给出的理由很直接：

> "instead of loading the smallest 124 million parameter model, we load the medium version with 355 million parameters since the 124 million model is too small for achieving qualitatively reasonable results via instruction finetuning"
> ——`ch07.ipynb` 7.5 节

也就是说，124M 太小，指令微调后也学不出像样的回答；355M 才能在普通笔记本上跑到「定性合理」的效果。这是「模型容量」与「教学可跑性」之间的权衡。

#### 4.1.2 核心流程

脚本里的「加载预训练模型」一段，数据流如下：

```
BASE_CONFIG（基础配置）
        │
        ├─ 读取 model_configs[CHOOSE_MODEL]   # 只覆盖 emb_dim / n_layers / n_heads
        ▼
download_and_load_gpt2(model_size="355M")      # 下载 TF checkpoint → 解析成 params 字典
        │
        ▼
model = GPTModel(BASE_CONFIG)                  # 用空权重搭出 355M 结构
        │
        ▼
load_weights_into_gpt(model, params)           # 把 OpenAI 权重逐层翻译进 model
        │
        ▼
model.eval(); model.to(device)                 # 关闭 dropout、搬到 GPU（若有）
```

注意配置的组织方式：`BASE_CONFIG` 放「所有规模共享」的四个参数（词表、上下文长度、dropout、qkv_bias），`model_configs` 字典只放「随规模变化」的三个参数（`emb_dim`/`n_layers`/`n_heads`）。`BASE_CONFIG.update(model_configs[CHOOSE_MODEL])` 把后者合并进前者，得到一份完整的 355M 配置。`drop_rate=0.0` 表示微调阶段关闭 dropout，与 u5-l4 加载权重做推理评估时的设置一致。

#### 4.1.3 源码精读

**配置与模型选择** —— [ch07/01_main-chapter-code/gpt_instruction_finetuning.py:242-266](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/01_main-chapter-code/gpt_instruction_finetuning.py#L242-L266)

这段先定义基础配置 `BASE_CONFIG`（含对齐 OpenAI 的 `context_length=1024`、`qkv_bias=True`），再用 `model_configs` 字典列出四种 GPT-2 规格供选择，`CHOOSE_MODEL` 选定 `gpt2-medium (355M)` 后 `update` 合并配置。`model_size = CHOOSE_MODEL.split(" ")[-1].lstrip("(").rstrip(")")` 这句从字符串 `"gpt2-medium (355M)"` 里抠出 `"355M"`，作为下载目录和权重的 key。

**加载权重并切到评估模式** —— [ch07/01_main-chapter-code/gpt_instruction_finetuning.py:261-266](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/01_main-chapter-code/gpt_instruction_finetuning.py#L261-L266)

```python
settings, params = download_and_load_gpt2(model_size=model_size, models_dir="gpt2")
model = GPTModel(BASE_CONFIG)
load_weights_into_gpt(model, params)
model.eval()
model.to(device)
```

`download_and_load_gpt2` 返回 `(settings, params)`：`settings` 是超参（`hparams.json`），`params` 是嵌套字典形式的权重。`load_weights_into_gpt` 充当「翻译官」，处理 OpenAI 的 `Conv1D`/命名差异/`c_attn` 合并等（详见 [u5-l4](u5-l4-weight-loading.md)）。它的最后一步是权重共享（weight tying）：`gpt.out_head.weight = assign(gpt.out_head.weight, params["wte"])`，让输出头复用 token 嵌入矩阵——这正是 [u4-l3](u4-l3-gpt-model-assembly.md) 提到的 124M 口径的由来。加载完调用 `model.eval()` 关闭 dropout。

**微调前的「基线」表现**：notebook 在训练前先用验证集第一条指令试生成（`ch07.ipynb` 7.5 节），指令是：

> Convert the active sentence to passive: 'The chef cooks the meal every day.'

未微调模型的回答是：

> The chef cooks the meal every day.
> ### Instruction:
> Convert the active sentence to passive: 'The chef cooks the

它只是把输入句子复读了一遍、又续写出了一个新的 `### Instruction:` 段——**完全没听懂「改成被动语态」这个指令**。这就是微调要解决的问题。同时，脚本在训练前打印初始损失（见 [ch07/01_main-chapter-code/gpt_instruction_finetuning.py:274-280](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/01_main-chapter-code/gpt_instruction_finetuning.py#L274-L280)），notebook 实测约 **训练损失 3.83 / 验证损失 3.76**——说明模型对 Alpaca 指令格式还很「陌生」。

#### 4.1.4 代码实践

**实践目标**：亲手加载预训练权重，确认模型「能跑但不会听指令」，建立微调前的基线。

**操作步骤**：

1. 确认 `ch07/01_main-chapter-code/` 目录下有 `gpt_download.py`、`previous_chapters.py`（首次运行会自动下载约 1.4 GB 的 355M 权重到 `gpt2/355M/`）。
2. 打开 `ch07.ipynb`，从开头运行到 7.5 节「Loading a pretrained LLM」的加载单元格，以及紧随其后的「微调前生成」单元格（用 `format_input(val_data[0])` 做输入、`max_new_tokens=35` 生成）。
   - 若想跑脚本而非 notebook，可执行 `python gpt_instruction_finetuning.py`，它会把加载、训练、生成一气呵成（见 4.3 节）。
3. 把生成的回答与正确答案 `entry['output']` 对比。

**需要观察的现象**：模型输出里出现 `### Response:` 之后只是复读指令或续写出新的 `### Instruction:`，没有真正作答。

**预期结果**：与 notebook 7.5 节一致——回答里复现输入句子而非其被动语态版本，证明模型此时「会续写但不会听指令」。

**待本地验证**：未训练权重加载是确定性的，但首次下载耗时取决于网络；若你的机器没有 GPU，脚本会自动退回 CPU（`device = torch.device("cuda" if torch.cuda.is_available() else "cpu")`）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `CHOOSE_MODEL` 改成 `"gpt2-small (124M)"`，`BASE_CONFIG` 最终会变成什么？为什么作者不推荐这样做？

> **参考答案**：`BASE_CONFIG` 会变成 `{vocab_size:50257, context_length:1024, drop_rate:0.0, qkv_bias:True, emb_dim:768, n_layers:12, n_heads:12}`。不推荐是因为 124M 容量太小，指令微调后达不到「定性合理」的回答质量（notebook 7.5 节明确说明）。

**练习 2**：`model_size = CHOOSE_MODEL.split(" ")[-1].lstrip("(").rstrip(")")` 在 `CHOOSE_MODEL = "gpt2-medium (355M)"` 时得到什么？为什么要做 `lstrip/rstrip`？

> **参考答案**：得到 `"355M"`。`split(" ")[-1]` 取到 `"(355M)"`，`lstrip("(")` 去掉左括号、`rstrip(")")` 去掉右括号，剩下的 `"355M"` 正好是 `download_and_load_gpt2` 和下载目录所用的 key。

---

### 4.2 复用 train_model_simple 完成训练

#### 4.2.1 概念说明

本模块最关键的认知是：**指令微调在「训练算法」层面与第 5 章预训练完全相同**——都是「下一个 token 预测」的交叉熵损失，配上 AdamW 优化器和 `zero_grad→forward→backward→step` 四件套。所以我们能**原封不动地复用**第 5 章写好的 `train_model_simple`，一行都不用改。

那指令微调和预训练到底差在哪？**差在「喂什么数据、掩码什么位置」**：

| 维度 | 第 5 章预训练 | 第 6 章分类微调 | 第 7 章指令微调（本讲） |
| --- | --- | --- | --- |
| 训练样本 | 滑动窗口切出的连续文本 | 整条短信（带 padding） | 「指令+回答」拼接文本 |
| 损失作用位置 | 所有 token | **只取最后一个 token** | 所有未被 `-100` 屏蔽的 token |
| 损失函数 | 交叉熵（`flatten(0,1)`） | 交叉熵（`[:,-1,:]`） | 交叉熵（`flatten(0,1)`，复用预训练版） |
| 任务 | 学语言 | 二分类 | 学「在指令后写出回答」 |

注意中间这一列：第 6 章分类把 `calc_loss_batch` 改成只取最后一个 token 的 logits；而本讲**用的是预训练的原始 `flatten` 版本**，因为指令微调要学的是「整段回答」的语言建模，而不只是某一个分类位。u7-l1 里那个 `-100` 掩码，正是在这里发挥作用：它把纯填充剔除出损失，只让模型在「回答正文 + 第一个结束符」这些位置上学习。

#### 4.2.2 核心流程

带掩码的下一个 token 交叉熵损失可以写成（\(\mathcal{M}\) 是未被屏蔽的目标位置集合，即回答 token 与首个 `<|endoftext|>`）：

\[
\mathcal{L} = -\frac{1}{|\mathcal{M}|}\sum_{(n,t)\,\in\,\mathcal{M}} \log p_{\theta}\!\left(y^{(n)}_t \,\big|\, x^{(n)}_{\le t}\right)
\]

PyTorch 的 `cross_entropy` 默认 `ignore_index=-100`，会自动把目标为 `-100` 的位置既不计入分子也不计入分母，于是 \(|\mathcal{M}|\) 恰好等于「有效目标数」，填充位置被天然排除。

`train_model_simple` 的循环结构（外层 epoch、内层 batch）：

```
for epoch in range(num_epochs):
    model.train()
    for input_batch, target_batch in train_loader:
        optimizer.zero_grad()              # 清空上一步累积的梯度
        loss = calc_loss_batch(...)         # 前向 + 带掩码交叉熵（含梯度）
        loss.backward()                     # 反传
        optimizer.step()                    # 更新权重
        tokens_seen += input_batch.numel()
        if global_step % eval_freq == 0:    # 每 eval_freq 步评估一次
            evaluate_model(...)             # model.eval() + no_grad 算 train/val loss
    generate_and_print_sample(...)          # 每个 epoch 末打印一条样本
```

这条「数值监控线」（`evaluate_model` 给损失趋势）和「样本监控线」（`generate_and_print_sample` 给定性回答质量）并行埋在循环里，是判断训练是否健康的一对互补信号。

#### 4.2.3 源码精读

**复用的训练循环本体** —— [ch07/01_main-chapter-code/previous_chapters.py:293-326](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/01_main-chapter-code/previous_chapters.py#L293-L326)

这是从第 5 章搬来的 `train_model_simple`。注意它内部调用的是 `calc_loss_batch`（4.2.1 表格里的「预训练版」），即对**所有 token**位置算损失，依赖 u7-l1 的 `-100` 掩码来剔除填充——这就是「复用算法、换数据与掩码」的落地。它返回 `(train_losses, val_losses, track_tokens_seen)` 三条记录，供后续画曲线。

**损失计算（被复用的预训练版）** —— [ch07/01_main-chapter-code/previous_chapters.py:430-434](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/01_main-chapter-code/previous_chapters.py#L430-L434)

```python
def calc_loss_batch(input_batch, target_batch, model, device):
    input_batch, target_batch = input_batch.to(device), target_batch.to(device)
    logits = model(input_batch)
    loss = torch.nn.functional.cross_entropy(logits.flatten(0, 1), target_batch.flatten())
    return loss
```

`logits.flatten(0, 1)` 把 `(batch, num_tokens, vocab)` 压成 `(batch*num_tokens, vocab)`，`target_batch.flatten()` 压成对应的一维目标。`cross_entropy` 默认 `ignore_index=-100`，自动跳过 u7-l1 填进去的 `-100` 位置。对比第 6 章分类版把这里改成 `logits[:, -1, :]`，就能看出指令微调与分类微调在损失作用位置上的本质差异。

**优化器与训练调用** —— [ch07/01_main-chapter-code/gpt_instruction_finetuning.py:282-292](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/01_main-chapter-code/gpt_instruction_finetuning.py#L282-L292)

```python
optimizer = torch.optim.AdamW(model.parameters(), lr=0.00005, weight_decay=0.1)
num_epochs = 2
torch.manual_seed(123)
train_losses, val_losses, tokens_seen = train_model_simple(
    model, train_loader, val_loader, optimizer, device,
    num_epochs=num_epochs, eval_freq=5, eval_iter=5,
    start_context=format_input(val_data[0]), tokenizer=tokenizer
)
```

几个关键超参值得一记：

- `lr=0.00005`（5e-5）：**比第 5 章预训练的 4e-4 小近一个数量级**。微调是对已学好模型的「小步调整」，学习率太大会破坏预训练学到的知识（ catastrophic forgetting，灾难性遗忘）。
- `weight_decay=0.1`：AdamW 的解耦权重衰减，起正则化作用，与预训练一致。
- `num_epochs=2`：只训 2 轮。指令数据集小（训练集 935 条），多训容易过拟合。
- `start_context=format_input(val_data[0])`：每个 epoch 末用这条验证指令生成样本，方便肉眼看到「模型逐渐学会听指令」。

**训练效果（来自 notebook 实测）**：第 1 个 epoch 后，模型对 `start_context` 的回答从乱续写变成了 **"The meal is prepared every day by the chef."**；第 2 个 epoch 后变成 **"The meal is cooked everyday by the chef."**——已经能正确地把主动句改成被动句了。损失方面，训练损失从约 2.6 一路降到 ~0.3，验证损失降到 ~0.65 后趋于停滞，**约 1 个 epoch 后出现轻微过拟合迹象**（与第 5 章在《The Verdict》上过拟合的现象同理，根因都是数据少）。

**训练时长参考**（notebook 7.6 节给出的实测表，2 个 epoch）：CPU（M3 MacBook Air）约 15.78 分钟、GPU（M3）约 10.77 分钟、L4 约 1.83 分钟、A100 约 0.86 分钟。可见 355M 在 CPU 上也能跑通，只是慢。

#### 4.2.4 代码实践

**实践目标**：理解训练循环的复用关系，并跑通一次（最小成本）指令微调。

**操作步骤**：

1. **快速冒烟测试（推荐先做）**：脚本提供 `--test_mode` 标志，会用一个极小模型（`emb_dim=12, n_layers=1, n_heads=2, context_length=120`）、各划分只取 10 条样本、在 CPU 上跑，几分钟内验证整条流水线是否通畅：
   ```bash
   cd ch07/01_main-chapter-code
   python gpt_instruction_finetuning.py --test_mode
   ```
   注意：test_mode 下不下载真实权重，损失不会真正下降，仅用于验证代码可跑通。
2. **正式微调**：去掉 `--test_mode`，在 GPU 机器上运行 `python gpt_instruction_finetuning.py`（或运行 notebook 7.6 节单元格），2 个 epoch。
3. 观察终端打印：每 5 步打印一次 `Train loss / Val loss`，每个 epoch 末打印一条生成样本。

**需要观察的现象**：

- 训练损失在前几十步快速下降（从 ~2.6 降到 < 1），验证损失随之下降但在 ~0.65 附近趋于平台。
- epoch 末的样本里出现符合指令的回答（如把主动句改成被动句）。

**预期结果**：训练损失降至 ~0.3、验证损失降至 ~0.65；样本回答正确遵循指令。

**待本地验证**：具体损失数值和耗时取决于你的设备与随机种子；CPU 上完整跑约 15 分钟。若验证损失明显高于上述值，检查是否误用了 124M 模型或数据划分有误。

#### 4.2.5 小练习与答案

**练习 1**：本讲的 `calc_loss_batch` 和第 6 章分类微调的 `calc_loss_batch` 在取 logits 时有什么区别？为什么指令微调用前者而不是后者？

> **参考答案**：分类版取 `logits[:, -1, :]`（只对最后一个 token 算损失，做二分类）；指令微调复用预训练版的 `logits.flatten(0, 1)`（对所有 token 算损失）。因为指令微调要学的是「整段回答」的语言建模，需要回答里每个 token 都提供梯度信号；纯填充位置则靠 u7-l1 的 `-100` 掩码被 `cross_entropy` 自动跳过。

**练习 2**：为什么本讲的学习率（5e-5）比第 5 章预训练（4e-4）小得多？

> **参考答案**：微调是在已经训练好的模型上做小幅调整，学习率太大会用少量指令数据「冲掉」预训练学到的通用语言知识（灾难性遗忘）。小学习率让模型在保留语言能力的前提下，温和地学会「听指令」这一新行为。

**练习 3**：`train_model_simple` 里 `optimizer.zero_grad()` 能不能省掉？为什么？

> **参考答案**：不能。PyTorch 的梯度默认会**累加**而非覆盖，若不清零，当前 batch 的梯度会叠加上一个 batch 的梯度，导致更新方向错误。`zero_grad()` 是四件套里不可或缺的一步（详见 [u5-l2](u5-l2-training-loop.md)）。

---

### 4.3 响应生成与保存

#### 4.3.1 概念说明

训练完成后，模型已经「会听指令」，但它的能力需要被量化、被复用。本模块做两件事：**① 在测试集上批量生成回答，存成 JSON 供后续评估；② 用 `state_dict` 把微调后的模型权重存盘，方便日后加载复用。**

这里有个**训练/推理对齐**的关键细节值得强调：

- **训练时**，u7-l1 的 `InstructionDataset` 把「指令 + `### Response:\n` + 回答 + `<|endoftext|>`」整段喂给模型，模型在「回答正文 + 结束符」这些位置上学习。
- **推理/生成时**，我们**只输入指令部分**（`format_input` 的输出，不含 `### Response:`），让模型自己续写出 `### Response:` 和后面的回答，直到遇到 `<|endoftext|>`（`eos_id=50256`）停止。

也就是说，模型在训练时「见过」`### Response:` 这个标记，学会在它之后写答案；推理时我们用同样的指令格式起头，模型就会主动续写出 `### Response:` 和答案。这种「训练格式 = 推理格式」的对齐，是指令微调能奏效的根本原因。

另一个细节：`generate` 函数（[u5-l3](u5-l3-decoding-strategies.md)）返回的是「输入 + 输出」拼接在一起的完整文本。要拿到纯回答，需要切掉输入前缀、再去掉 `### Response:` 标记。

#### 4.3.2 核心流程

```
for entry in test_data:                         # 遍历 110 条测试指令
    input_text = format_input(entry)            # 只格式化指令部分（不含 Response）
    token_ids = generate(                        # u5-l3 的 generate
        model, text_to_token_ids(input_text),
        max_new_tokens=256, context_size=1024,
        eos_id=50256)                            # 遇到结束符提前停
    generated_text = token_ids_to_text(token_ids)
    response = generated_text[len(input_text):]  # 切掉输入前缀
                 .replace("### Response:", "")   # 去掉响应标记
                 .strip()                        # 去首尾空白
    entry["model_response"] = response           # 写回字典

json.dump(test_data, ...)                        # 存成 JSON
torch.save(model.state_dict(), "...-sft.pth")    # 存模型权重
```

#### 4.3.3 源码精读

**批量生成回答** —— [ch07/01_main-chapter-code/gpt_instruction_finetuning.py:305-320](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/01_main-chapter-code/gpt_instruction_finetuning.py#L305-L320)

```python
for i, entry in tqdm(enumerate(test_data), total=len(test_data)):
    input_text = format_input(entry)
    token_ids = generate(
        model=model,
        idx=text_to_token_ids(input_text, tokenizer).to(device),
        max_new_tokens=256,
        context_size=BASE_CONFIG["context_length"],
        eos_id=50256
    )
    generated_text = token_ids_to_text(token_ids, tokenizer)
    response_text = generated_text[len(input_text):].replace("### Response:", "").strip()
    test_data[i]["model_response"] = response_text
```

注意几个点：

- `generate` 的 `temperature` 用默认值 `0.0`，即**贪心解码**（见 [ch07/01_main-chapter-code/previous_chapters.py:250-290](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/01_main-chapter-code/previous_chapters.py#L250-L290) 中 `temperature > 0.0` 才走 multinomial 分支，否则 `argmax`），保证回答可复现。
- `eos_id=50256`：一旦模型生成 `<|endoftext|>` 就 `break` 提前结束，避免无意义的后续续写。
- `generated_text[len(input_text):]`：因为 `generate` 返回「输入+输出」拼接，`len(input_text)` 正好把输入那段切掉。
- `.replace("### Response:", "").strip()`：模型续写时会在答案前自带一个 `### Response:`（它训练时见过这个标记），去掉它和首尾空白，得到干净回答。

**保存回答 JSON 与模型权重** —— [ch07/01_main-chapter-code/gpt_instruction_finetuning.py:322-329](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/01_main-chapter-code/gpt_instruction_finetuning.py#L322-L329)

```python
test_data_path = "instruction-data-with-response-standalone.json"
with open(test_data_path, "w") as file:
    json.dump(test_data, file, indent=4)
file_name = f"{re.sub(r'[ ()]', '', CHOOSE_MODEL) }-sft-standalone.pth"
torch.save(model.state_dict(), file_name)
```

- 回答写回每条 `test_data[i]["model_response"]`，整体存成 JSON（`indent=4` 美化），文件名带 `-standalone` 后缀以区别于 notebook 版（notebook 版叫 `instruction-data-with-response.json`，无后缀）。这份 JSON 正是 [u11-l3](u11-l3-model-evaluation.md) 用 Ollama 打分评估的输入。
- `re.sub(r'[ ()]', '', CHOOSE_MODEL)` 把 `"gpt2-medium (355M)"` 里的空格和括号删掉，得到 `gpt2-medium355M`，拼出模型文件名 `gpt2-medium355M-sft-standalone.pth`。`torch.save(model.state_dict(), ...)` 存的是权重字典（checkpoint），日后用 `model.load_state_dict(torch.load(...))` 即可还原（详见 [u5-l4](u5-l4-weight-loading.md) 的 state_dict 机制）。

**生成质量示例**（notebook 7.7 节，前 3 条测试指令）：

| 指令 | 正确答案 | 模型回答 | 评价 |
| --- | --- | --- | --- |
| 用明喻改写 "The car is very fast." | as fast as lightning | as fast as a bullet | ✅ 合理（明喻正确） |
| 哪种云和雷暴有关？ | cumulonimbus（积雨云） | a cumulus cloud（积云） | ⚠️ 接近但有误 |
| 《傲慢与偏见》的作者？ | Jane Austen | Jane Austen | ✅ 正确 |

可见微调确实让模型「学会听指令」，但还不够完美（如把「积雨云」答成「积云」）。这种「不像分类准确率那样有标准答案」的开放式评估，正是下一阶段 [u11-l3](u11-l3-model-evaluation.md) 要用 LLM-as-a-judge 解决的问题——本讲只负责**生成并保存**回答，把评估留给后续。

#### 4.3.4 代码实践

**实践目标**：用微调后的模型在若干测试指令上生成回答，并人工对比「微调前 vs 微调后」的回答质量。

**操作步骤**：

1. 完成 4.2 节的微调（或加载一个已有的 `gpt2-medium355M-sft.pth`）。
2. 运行 notebook 7.7 节前半段那几个单元格（对 `test_data[:3]` 逐条 `generate` 并打印），脚本则会自动对全部 110 条生成。
3. 为做「微调前后对比」，可在加载预训练权重后（4.1 节）、训练前，也对同样的 3 条指令跑一次 `generate`，把回答记录下来。

**需要观察的现象**：

- 微调前：回答多是复读指令或续写出新的 `### Instruction:`，不切题。
- 微调后：回答切题、语法正确，如把 "very fast" 改成明喻、回答出作者名。

**预期结果**：与上表一致——大部分回答合理，少部分接近但有细微错误（如 cumulus vs cumulonimbus）。

**待本地验证**：贪心解码（temperature=0）下结果可复现，但若你改用了非零温度，回答会有随机性。

#### 4.3.5 小练习与答案

**练习 1**：为什么推理时只输入 `format_input(entry)`（指令部分），而不像训练时那样把 `### Response:\n{回答}` 也拼上？

> **参考答案**：推理时我们没有「正确回答」（那是要模型生成的），所以只能给指令起头；模型在训练时学会了「见到指令格式后，续写 `### Response:` 再写答案」，于是会主动补全响应标记和答案，并在生成 `<|endoftext|>` 后停止。这正是「训练格式 = 推理格式」对齐的体现。

**练习 2**：`response_text = generated_text[len(input_text):].replace("### Response:", "").strip()` 中，为什么需要 `generated_text[len(input_text):]` 这一步？

> **参考答案**：因为 `generate` 返回的是「输入 token + 新生成 token」拼接后的完整序列，`token_ids_to_text` 解码出的 `generated_text` 前半段就是输入指令本身。`len(input_text)` 正好把这段输入前缀切掉，只保留模型新生成的部分。

**练习 3**：保存模型用 `torch.save(model.state_dict(), ...)` 而不是 `torch.save(model, ...)`，有什么好处？

> **参考答案**：`state_dict` 只存权重张量字典（不存模型类定义和图结构），文件更小、更安全、跨版本更稳健。加载时需先 `model = GPTModel(BASE_CONFIG)` 重建结构，再 `load_state_dict(torch.load(...))` 填权重——这要求加载端的 `BASE_CONFIG` 与保存时一致（详见 [u5-l4](u5-l4-weight-loading.md)）。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次端到端的「加载 → 微调 → 生成 → 对比 → 保存」：

1. **加载基线**：运行 `ch07/01_main-chapter-code/gpt_instruction_finetuning.py` 的加载部分（或 notebook 7.5 节），加载 `gpt2-medium (355M)` 权重。挑 3 条测试指令（如 notebook 用的「明喻改写」「雷暴云」「《傲慢与偏见》作者」），用 `generate(..., temperature=0, eos_id=50256)` 生成，记录**微调前**回答。
2. **微调 2 个 epoch**：用 `train_model_simple`（`lr=5e-5`、`weight_decay=0.1`、`num_epochs=2`）训练。记录训练/验证损失曲线，留意验证损失约 1 个 epoch 后是否停滞（过拟合信号）。
3. **生成微调后回答**：对同样的 3 条指令再次生成，对比「微调前 vs 微调后」。
4. **保存**：把全部测试集回答写回 `test_data[i]["model_response"]` 存成 JSON，并用 `torch.save(model.state_dict(), "gpt2-medium355M-sft.pth")` 存模型。

**验收标准**：

- 微调后回答在内容上切题、语法正确（明喻改写、作者问答正确；被动句改写正确）。
- 能解释为什么训练用 `flatten` 版损失、推理只输入指令、`generate` 要切前缀。
- 产出两个文件：`instruction-data-with-response.json`（供 [u11-l3](u11-l3-model-evaluation.md) 评估）和 `gpt2-medium355M-sft.pth`（可被 `load-finetuned-model.ipynb` 重新加载）。

> 提示：若没有 GPU，可先用 `python gpt_instruction_finetuning.py --test_mode` 跑通整条流水线（极小模型、各 10 条样本、CPU），再在有 GPU 的机器上做正式微调。

## 6. 本讲小结

- **指令微调 = 预训练权重 + 指令数据 + 复用的训练循环**。本讲不引入新算法，而是把 u5-l4 的权重加载、u5-l2 的 `train_model_simple`、u7-l1 的指令数据三块积木拼起来。
- **选 `gpt2-medium (355M)`** 而非 124M，因为后者容量太小、微调后达不到定性合理的回答质量；`context_length=1024`、`qkv_bias=True` 是对齐 OpenAI 权重的硬性配置。
- **`train_model_simple` 原样复用**：指令微调与预训练在算法上同构（都是带掩码的下一个 token 交叉熵），区别只在「喂什么数据、掩码什么位置」——u7-l1 的 `-100` 掩码让 `cross_entropy` 自动剔除纯填充。
- **学习率要小（5e-5）**：微调是对已学好模型的温和调整，避免灾难性遗忘；只训 2 个 epoch，约 1 个 epoch 后出现轻微过拟合。
- **训练/推理格式对齐**：训练时喂「指令+Response+回答+结束符」，推理时只喂指令，模型会主动续写出 `### Response:` 和答案，遇 `<|endoftext|>` 停止；`generate` 返回的是拼接文本，需切掉输入前缀、去掉 Response 标记。
- **产出两个文件**：`instruction-data-with-response.json`（回答，供后续评估）和 `gpt2-medium355M-sft.pth`（权重，用 `state_dict` 保存，可重新加载复用）。

## 7. 下一步学习建议

- **评估回答质量**：本讲只「生成并保存」了回答，如何给它打分？请进入 [u11-l3 模型评估：用 Ollama / OpenAI 评分](u11-l3-model-evaluation.md)，用 Llama 3 作为「裁判模型」对本讲产出的 `instruction-data-with-response.json` 批量打分，做评分相关性分析。
- **偏好对齐**：指令微调之后，还可以用 DPO（直接偏好优化）让模型更贴合人类偏好，见 [u11-l2 DPO 偏好对齐](u11-l2-dpo-alignment.md)。
- **加载复用微调模型**：阅读同目录的 `load-finetuned-model.ipynb`，学习如何在新会话里用 `load_state_dict` 还原本讲保存的 `gpt2-medium355M-sft.pth`。
- **延伸阅读**：对照 `ch07.ipynb` 的 7.5–7.7 节与 `gpt_instruction_finetuning.py` 的 `main()`，体会 notebook 的「逐行演进」与脚本的「精简汇总」之间的对应关系（呼应 [u1-l3](u1-l3-repo-reading-map.md) 的汇总脚本约定）。
