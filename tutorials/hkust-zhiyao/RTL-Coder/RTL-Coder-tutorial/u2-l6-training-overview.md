# 训练方案总览与共享数据管线

## 1. 本讲目标

本讲是 RTL-Coder 训练部分的「总览篇」。读完本讲，你应当能够：

- 说清 RTL-Coder 提供的**三种训练方案**（MLE 直接训练、评分训练、评分训练+梯度切分）分别对应哪个脚本、解决什么问题、什么场景下选用。
- 理解评分训练独有的数据预处理类 `ScoreDataset`，特别是它为什么要在加载数据时做一道 `model_max_length * 0.5` 的**长度过滤**。
- 看懂两个评分脚本共享的 `DataCollatorForSupervisedDataset` 是如何把「一条指令 + 多个候选答案」**展开成多调 query+response 序列**并配上分数的。
- 把本讲当作后续 `u2-l7`（标准 SFT 精读）、`u3-l1`（评分训练损失）、`u3-l3`（梯度切分显存优化）的导航地图。

本讲只做「搭骨架」：损失函数的数学推导、梯度切分的显存细节都先当成黑盒，留给专家层讲义展开。

## 2. 前置知识

在进入训练之前，请确认你已经理解下面几个概念（它们都在前置讲义里讲过）：

- **SFT（Supervised Fine-Tuning，监督微调）**：给模型看大量「指令 → 标准答案」的样本，让它学会照着答案续写。模型只对「答案部分」计算损失，「指令部分」被遮住不计入。遮住用的标记是 `IGNORE_INDEX = -100`（见 [train/mle.py:25](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py#L25)）。
- **三种数据格式**（详见 `u1-l4`）：
  - 生成/普通训练样本：`{Instruction, Input, Response[1]}`
  - 成品训练集 `Resyn27k.json`：`{Instruction, Response[1]}`（刻意丢掉 `Input`）
  - 评分训练样本：`{Instruction, Input, Response[N], Score[N]}`，多候选与多分数按位置一一对应。
- **DeepSpeed ZeRO-2**：一种把优化器状态切分到多卡（甚至卸载到 CPU）以省显存的分布式训练策略，三个方案都用同一份配置 `ds_stage_2.json`。
- **HuggingFace `Trainer`**：transformers 库提供的训练循环框架。三种方案都基于它，评分方案通过**子类化 `Trainer`** 来改写损失计算（必要时还改写训练步）。

一个直觉：**普通 SFT 只有一个「标准答案」可学；评分训练则给模型看好几个「质量高低不同」的候选答案，逼它不仅会写，还分得清好坏。** 这就是 RTL-Coder 能超过 GPT-3.5 的关键。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [README.md:233-309](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L233-L309) | 训练章节，给出三种方案的 `torchrun` 命令 | 三方案的启动参数差异 |
| [train/mle.py](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py) | MLE 标准监督微调 | 作为「对照基准」，理解最简数据管线 |
| [train/mle_scoring.py](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py) | 评分训练 | `ScoreDataset` 长度过滤、`DataCollator` 多候选拼接 |
| [train/mle_scoring_grad_split.py](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py) | 评分训练 + 梤度切分 | 与 `mle_scoring.py` 共享数据管线，只额外改写 `training_step` |
| [train/scoring_data_sample.json](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/scoring_data_sample.json) | 评分训练数据样例 | 观察多候选 + Score 字段的真实结构 |

> 提示：`mle_scoring_grad_split.py` 与 `mle_scoring.py` 的 `ScoreDataset`、`DataCollatorForSupervisedDataset`、`CompareTrainer.compute_loss` 几乎逐行相同，区别仅在于前者多覆写了一个 `training_step`。本讲解数据管线时以 `mle_scoring.py` 为准，结论对两者都成立。

---

## 4. 核心概念与源码讲解

### 4.1 三种训练方案：脚本、损失与适用场景

#### 4.1.1 概念说明

RTL-Coder 在 README 的训练章节开宗明义：

> We provide three options for instruction tuning: **MLE based direct train**, **Scoring train** and **Scoring train with gradients splitting**.
> （见 [README.md:236](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L236)）

三种方案是逐级增强的关系：

1. **MLE 直接训练（`mle.py`）**：最普通的标准 SFT。一条指令只有一个参考答案（取 `Response[-1]`），模型学习「照抄」参考代码。这是基线，显存最省、效果也最基础。
2. **评分训练（`mle_scoring.py`）**：本项目的核心贡献。一条指令配 **N 个质量不同的候选答案**，每个候选带一个 `Score`。模型除了仍在最好的候选上做 SFT（称为「域损失 domain loss」），还要学会**让高质量候选的似然高于低质量候选**（称为「比较损失 comparison loss」）。效果更好，但一条样本会被展开成 N 条序列，显存翻 N 倍。
3. **评分训练 + 梯度切分（`mle_scoring_grad_split.py`）**：学习目标和评分训练完全一样，但从**算法层**改写训练步，把一次要同时前向 N 个候选拆成「逐候选前向」，从而降低显存峰值。README 把它定位成评分训练的「兜底方案」。

一句话选型：

- 手上只有 `{Instruction, Response}` 数据，或显存紧张 → 用 `mle.py`。
- 有评分候选数据，且 GPU 放得下「一条样本的全部候选」→ 用 `mle_scoring.py`。
- 有评分候选数据，但 GPU 连 `per_device_train_batch_size=1` 都放不下 → 用 `mle_scoring_grad_split.py`。

#### 4.1.2 核心流程

三种方案的代码骨架高度一致，差别只在「损失怎么算」和「数据怎么喂」：

```
三种方案共享的主干（以 mle_scoring.py 的 train() 为例）：
  解析参数 (HfArgumentParser)
  → 加载模型 (AutoModelForCausalLM, fp16) + gradient_checkpointing_enable
  → 加载 tokenizer (padding_side="left")
  → make_supervised_data_module(...)   # 构建 Dataset + DataCollator
  → Trainer(model, data, args).train() # 跑训练循环
  → trainer.save_model(...)
```

差异点对照：

| 维度 | `mle.py` | `mle_scoring.py` | `mle_scoring_grad_split.py` |
| --- | --- | --- | --- |
| Trainer 类 | 基类 `Trainer` | 子类 `CompareTrainer` | 子类 `CompareTrainer` |
| 改写的方法 | 无 | `compute_loss` | `compute_loss` + `training_step` |
| Dataset | `SupervisedDataset` | `ScoreDataset` | `ScoreDataset` |
| 单条样本产生几条序列 | 1 条 | N 条（N = 候选数） | N 条 |
| 损失 | 标准 SFT 交叉熵 | 域损失 + 比较损失 | 同左（但分步反传） |

> 注：`CompareTrainer.compute_loss` 内部的损失数学（候选归一化、0.2/0.3 边距）是 `u3-l1`、`u3-l2` 的主题；`training_step` 的显存切分技巧是 `u3-l3` 的主题。本讲只确认「它们被覆写了」，不展开公式。

#### 4.1.3 源码精读

**三个 `torchrun` 启动命令**几乎相同，只在 `per_device_train_batch_size` 和 `gradient_accumulation_steps` 上有区别。MLE 方案的命令见 [README.md:240-260](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L240-L260)：

```bash
torchrun --nproc_per_node=4  mle.py \
    --per_device_train_batch_size 2 \
    --gradient_accumulation_steps 32 \
    --gradient_checkpointing True \
    --deepspeed ds_stage_2.json \
    --model_max_length 2048
    # ...其余参数省略
```

评分训练的命令见 [README.md:265-285](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L265-L285)，梯度切分版的命令见 [README.md:289-309](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L289-L309)，二者把 batch 改成 1、累积步数翻倍：

```bash
torchrun --nproc_per_node=4  mle_scoring.py \
    --per_device_train_batch_size 1 \
    --gradient_accumulation_steps 64 \
    # ...其余参数与 mle.py 一致
```

README 还点明了梯度切分版的适用场景（见 [README.md:287](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L287)）：

> If your gpu could't afford batch size 1 with these answer candidates, try the gradients splitting method.

**一个容易被忽略的关键观察**：评分方案把 `per_device_train_batch_size` 从 2 降到 1，同时把 `gradient_accumulation_steps` 从 32 翻到 64。这并非随意，而是为了**在缩小显存的同时保持有效 batch 不变**：

\[ \text{有效 batch} = \text{nproc\_per\_node} \times \text{per\_device\_train\_batch\_size} \times \text{gradient\_accumulation\_steps} \]

- MLE：\(4 \times 2 \times 32 = 256\)
- 评分 / 梯度切分：\(4 \times 1 \times 64 = 256\)

三者有效 batch 都是 256，优化轨迹基本对齐；评分方案之所以必须 batch=1，是因为 `DataCollator` 会把每条样本展开成 N 条序列（见 4.3），batch=2 就意味着单步要同时驻留 \(2N\) 条序列的激活值，极易 OOM。

#### 4.1.4 代码实践

**实践目标**：通读 README 训练章节，亲手整理一张「三方案启动参数对比表」，把命令行差异和背后的显存逻辑串起来。

**操作步骤**：

1. 打开 [README.md:233-309](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L233-L309)，分别找到三个 `torchrun` 代码块。
2. 对每个命令，记录：脚本名、`--nproc_per_node`、`--per_device_train_batch_size`、`--gradient_accumulation_steps`、`--model_max_length`、`--deepspeed`。
3. 用上面的有效 batch 公式计算三者有效 batch。
4. 按「单条样本展开几条序列」推断显存从低到高的排序。

**参考答案表**（以下数值直接来自 README，可直接核对）：

| 方案 | 脚本 | `--nproc_per_node` | `per_device_train_batch_size` | `gradient_accumulation_steps` | 有效 batch | 单步驻留序列数 | 显存需求 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| MLE 直接训练 | `mle.py` | 4 | 2 | 32 | 256 | \(\approx 2\) | 最低 |
| 评分训练 | `mle_scoring.py` | 4 | 1 | 64 | 256 | \(\approx 1\times N\) | 最高 |
| 评分训练+梯度切分 | `mle_scoring_grad_split.py` | 4 | 1 | 64 | 256 | \(\approx 1\)（逐候选） | 居中 |

> 注：「单步驻留序列数」一栏中 \(N\) 是每条样本的候选答案数，`scoring_data_sample.json` 里 \(N=4\)。显存需求排序为：MLE < 梯度切分 < 评分训练。

**需要观察的现象 / 预期结果**：你会确认三方案共用同一份 `ds_stage_2.json`、同一套超参（lr=1e-5、epochs=3、fp16、gradient_checkpointing），唯一刻意调整的就是 batch 与累积步数，且有效 batch 巧妙地对齐在 256。这一步无需运行训练，纯阅读即可完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么评分方案要把 `gradient_accumulation_steps` 从 32 调到 64，而不是保持 32？
**答案**：评分方案的 `per_device_train_batch_size` 从 2 降到了 1（因为每条样本会展开成 N 条序列，显存吃紧）。为了把有效 batch 保持在 256 不变，就必须把累积步数翻倍补偿：\(4 \times 1 \times 64 = 256\)。若仍用 32，有效 batch 会掉到 128，优化轨迹就和 MLE 方案不对齐了。

**练习 2**：如果你的显卡跑 `mle_scoring.py` 直接 OOM，README 建议怎么做？为什么这样能省显存？
**答案**：改用 `mle_scoring_grad_split.py`。它覆写了 `training_step`，把原本「N 个候选同时前向、激活值同时驻留」改成「逐候选前向、逐候选反传」，从而降低显存峰值。代价是训练步内部多了几次前向，速度会慢一些（细节见 `u3-l3`）。

---

### 4.2 ScoreDataset：候选长度过滤与「假 input_ids」约定

#### 4.2.1 概念说明

`ScoreDataset` 是两个评分脚本共用的数据集类（[train/mle_scoring.py:88-111](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L88-L111)）。它要解决两个问题：

1. **长度过滤**：评分训练的最终序列是 `query + response_i`，而且一条样本有 N 条这样的序列。如果某条指令本身就很长，加上多个候选就会超出 `model_max_length`，要么被截断、要么白白吃显存。所以加载时先扔掉「指令太长」的样本。
2. **延迟 tokenize**：与 `mle.py` 的 `SupervisedDataset`（在 `__init__` 里就把全部样本 tokenize 完）不同，`ScoreDataset` **不在构造时 tokenize 答案**，只把原始字典原样存起来，真正的 tokenize 推迟到 `DataCollator` 里按 batch 进行。原因正是 4.3 要讲的「多候选拼接」需要逐候选动态切块。

#### 4.2.2 核心流程

```
ScoreDataset.__init__:
  读取 JSONL → self.dic_temp（每行一个完整字典）
  取出每条的 Instruction 文本 → self.data_temp
  对每条 Instruction 单独 tokenize:
      若 token 数 < model_max_length * 0.5  → 保留该样本
      否则                                   → 丢弃
  打印 total / filtered 两个计数

ScoreDataset.__getitem__(i):
  返回 dict(input_ids = self.data[i])   # 注意：这里返回的是「原始字典」，不是 token id！
```

过滤阈值的含义：当 `--model_max_length 2048` 时，阈值是 \(2048 \times 0.5 = 1024\)，即**指令本身的 token 数必须小于 1024** 才被保留。这给后续拼接 `query + response` 预留了至少一半的长度预算。

#### 4.2.3 源码精读

长度过滤的核心就一行，见 [train/mle_scoring.py:99-106](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L99-L106)：

```python
self.data = []
index = 0
for data in self.data_temp:                 # data_temp = 每条的 Instruction 文本
    data_toked = _single_tokenize(data, tokenizer)
    if data_toked.shape[0] < model_max_length * 0.5:   # ← 长度过滤
        self.data.append(self.dic_temp[index])
    index += 1
print('filtered num of data: {}'.format(len(self.data)))
```

注意 `_single_tokenize` 只切了 `Instruction`（题目描述），没有切 `Response`（候选代码）。所以这个过滤是**按「指令长度」而非「答案长度」**做的粗筛，目的是保证「指令 + 任一候选」有较大概率落在 `model_max_length` 内、且答案不会被截到太短。

**一个必须知道的「hack」**——`__getitem__` 返回的字段名叫 `input_ids`，但值其实是**整条原始字典**（见 [train/mle_scoring.py:110-111](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L110-L111)）：

```python
def __getitem__(self, i):
    return dict(input_ids=self.data[i])   # self.data[i] 是 {Instruction, Input, Response, Score} 整个字典
```

为什么字段名这么误导？因为 HuggingFace 的默认 `Trainer` 会把 `__getitem__` 返回的字典键直接当张量名传给 collator。作者借用了 `input_ids` 这个名字让管线跑通，然后在 collator 里用 `ins = ins['input_ids']` 把字典「拆」回来（见 4.3 的 `# hack` 注释）。读代码时看到 `input_ids` 不要误以为是 token 张量。

另外，`make_supervised_data_module` 在评分脚本里**多了 `training_args` 参数**（对比 [train/mle.py:161](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py#L161) 与 [train/mle_scoring.py:170-174](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L170-L174)），正是因为过滤阈值要用 `training_args.model_max_length`。也正因如此，`ScoreDataset` 必须在拿到训练参数之后才能构建。

> 细节：`mle_scoring.py` 的 `TrainingArguments.model_max_length` 默认值是 **1024**（[train/mle_scoring.py:49-52](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L49-L52)），而 README 的命令行统一覆盖成 **2048**，所以实际阈值是 1024。

#### 4.2.4 代码实践

**实践目标**：用一个已有的 JSON 文件复现 `ScoreDataset` 的长度过滤行为，直观感受「total → filtered」会掉多少样本。

**操作步骤**：

1. 写一段最小脚本（**示例代码**，需本地安装 transformers 与一个 tokenizer）：

   ```python
   # 示例代码：复现 ScoreDataset 的长度过滤逻辑
   import json
   from transformers import AutoTokenizer

   tok = AutoTokenizer.from_pretrained("ishorn5/RTLCoder-v1.1", use_fast=False)
   model_max_length = 2048          # 对应 README 命令的 --model_max_length

   lines = open("train/scoring_data_sample.json").readlines()
   dic_temp = [json.loads(l) for l in lines]
   instructions = [d["Instruction"] for d in dic_temp]

   kept = []
   for d, text in zip(dic_temp, instructions):
       n = tok(text, truncation=True)["input_ids"]
       if len(n) < model_max_length * 0.5:
           kept.append(d)
   print("total:", len(dic_temp), "filtered:", len(kept))
   ```

2. 把脚本放在仓库根目录运行（需要能联网拉取 tokenizer，或换成本地模型路径）。

**需要观察的现象 / 预期结果**：由于 `scoring_data_sample.json` 的几条指令都不算特别长，预期 `filtered` 与 `total` 很接近（甚至相等）。这一步主要验证「过滤只看 Instruction 长度」这一行为。**待本地验证**：实际计数取决于 tokenizer 的切词结果。

> 若暂时没有 GPU 或无法拉 tokenizer，可降级为「源码阅读型实践」：打开 `scoring_data_sample.json`，肉眼比较 5 条样本的 `Instruction` 长短，预测哪条最可能被过滤掉，再去脚本里核对 `print` 输出。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `--model_max_length` 从 2048 调小到 1024，`ScoreDataset` 会保留更多还是更少样本？为什么？
**答案**：保留更少。过滤条件是 `Instruction tokens < model_max_length * 0.5`，阈值从 1024 降到 512，更多长指令会被判为「太长」而丢弃。

**练习 2**：`ScoreDataset.__getitem__` 返回的 `input_ids` 字段里到底装的是什么？为什么要这么命名？
**答案**：装的是整条原始字典 `{Instruction, Input, Response, Score}`，并不是 token id。这样命名是为了借用 HuggingFace `Trainer` 把 `__getitem__` 返回键当张量名传给 collator 的默认行为；真正的 tokenize 延后到 `DataCollator` 里做。collator 里有一句 `ins = ins['input_ids']  # hack` 把字典取回。

---

### 4.3 DataCollatorForSupervisedDataset：多候选 query+response 拼接

#### 4.3.1 概念说明

这是整条共享数据管线的「心脏」。评分训练的输入是一条指令 + N 个候选答案，但模型一次前向只能吃「一条 query+response 序列」。`DataCollatorForSupervisedDataset`（[train/mle_scoring.py:124-167](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L124-L167)）负责把一条样本**展开成 N 条序列**，并同时产出三样东西：

- `input_ids` / `labels` / `attention_mask`：展开后的所有序列（指令部分 label 置 `-100`，答案部分保留）。
- `idxs`：标记每条序列属于 batch 中第几个样本，供损失函数把 `(总序列数, L, V)` 的 logits **还原**回 `(batch, N, L, V)`。
- `scores`：每个候选的质量分，供比较损失使用。

对比 `mle.py` 的 collator（[train/mle.py:142-158](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py#L142-L158)）：那个版本里一个样本就是一条序列，collator 只做 padding。评分版则承担了「tokenize + 候选展开 + label 掩码」三件事。

#### 4.3.2 核心流程

设 batch 内有 \(B\) 个样本，第 \(j\) 个样本有 \(N_j\) 个候选。伪代码：

```
对 batch 中第 idx 个样本:
    query  = 样本["Instruction"]
    responses = 样本["Response"]    # 长度 N_idx 的列表
    scores    = 样本["Score"]       # 同样长度 N_idx
    query_ids = tokenize(query)
    query_target = [-100] * len(query_ids)         # 指令部分不计损失
    对每个 res in responses:
        res_ids = tokenize(res + eos_token, max_len = model_max_length - len(query_ids))
        input_ids.append( concat(query_ids, res_ids) )      # query + 该候选
        labels.append(   concat(query_target, res_ids) )    # query 被掩码，候选作为目标
    all_scores.append(scores)
    idxs.append( [idx] * N_idx )                 # 标记这 N_idx 条序列都属于样本 idx

最终:
    input_ids: pad 成 (sum_N, L)
    labels:    pad 成 (sum_N, L)，pad 位用 -100
    attention_mask = input_ids != pad_token_id
    idxs:   (sum_N, N_idx)   # 实际只用了第一列
    scores: (B, N)
```

关键结论：**batch 里的「样本数」和模型实际看到的「序列数」不再相等**。`per_device_train_batch_size=1` 意味着 1 个样本，但若它有 4 个候选，模型前向时拿到的是 4 条序列。这正是 4.1 中「评分方案 batch 必须降到 1」的根本原因。

#### 4.3.3 源码精读

候选展开的双层循环见 [train/mle_scoring.py:136-152](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L136-L152)：

```python
for idx, ins in enumerate(instances):
    ins = ins['input_ids']                 # hack：取回原始字典
    query = ins['Instruction']
    responses = ins['Response']            # 多候选列表
    scores = ins['Score']
    all_scores.append(scores)
    idxs.append([idx] * len(scores))       # 每个候选都贴上同一个样本下标

    query_input_ids = _single_tokenize(query, self.tokenizer, max_len=self.tokenizer.model_max_length)
    query_target = torch.LongTensor([IGNORE_INDEX] * (query_input_ids.shape[0]))
    for res in responses:
        res_input_ids = _single_tokenize(
            r + self.tokenizer.eos_token, self.tokenizer,
            max_len=self.tokenizer.model_max_length - query_input_ids.shape[0])  # 给 query 留出预算
        input_ids.append(torch.cat((query_input_ids, res_input_ids), dim=0))
        labels.append(torch.cat((query_target, res_input_ids), dim=0))
```

要点逐条说明：

- `res + eos_token`：每个候选末尾补结束符，让模型学会「写完就停」。
- `max_len = model_max_length - len(query_input_ids)`：候选的最大长度是**总预算减去指令已占用的部分**，保证 `query + 候选` 不超过 `model_max_length`。这也解释了 4.2 为什么要先过滤掉过长的指令。
- `labels` 里 `query_target` 全是 `-100`：指令部分不计入损失，只在候选答案上学习。
- `idxs.append([idx] * len(scores))`：同一条样本的 N 个候选都标成同一个 `idx`，让下游 `compute_loss` 能把展平的序列**按候选维度 reshape 回去**。

返回的字典见 [train/mle_scoring.py:161-167](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L161-L167)，多了 `idxs` 和 `scores` 两个字段（普通 SFT 的 collator 没有这两个）。

下游 `compute_loss` 如何「还原」候选维度（仅看思路，不展开损失公式）——见 [train/mle_scoring.py:225-231](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L225-L231)：

```python
batch_size, _ = torch.max(inputs['idxs'][:, 0], dim=0)   # 用 idxs 第一列还原样本数
batch_size = batch_size.cpu().detach().item() + 1
can_num = int(logits.size(0) / batch_size)               # 候选数 = 总序列数 / 样本数
logits = logits.view(batch_size, can_num, L, vocab)      # 还原成 (batch, cand, L, V)
```

也就是说，collator 负责「把 batch 展平成序列」，`compute_loss` 负责「把展平的序列 reshape 回 (batch, cand)」。两者是一对配套契约。

#### 4.3.4 代码实践

**实践目标**：拿 `scoring_data_sample.json` 里的一条真实样本，手算 collator 会产出哪些张量、形状是多少，把「多候选拼接」彻底想清楚。

**操作步骤**：

1. 打开 [train/scoring_data_sample.json](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/scoring_data_sample.json)，找到第 2 行（multiplier 乘法器那条）。它的字段是：
   - `Instruction`：一段较长的自然语言题目；
   - `Input`：模块签名骨架；
   - `Response`：**4 个**候选实现；
   - `Score`：`[0.476887871853547, 0.488870222595548, 0.4325437693099897, 1]`，最后一个 `1` 是参考答案。
2. 假设 `per_device_train_batch_size=1`，那么这个 batch 里只有这一条样本（\(B=1\)，\(N=4\)）。
3. 在纸上推演 collator 输出。

**需要观察的现象 / 预期结果**（手算即可，无需运行）：

| 张量 | 形状 | 内容说明 |
| --- | --- | --- |
| `input_ids` | `(4, L)` | 4 条 `query + response_i` 序列，按 batch 最长 pad |
| `labels` | `(4, L)` | 同上，但每条前半段（query）是 `-100`，pad 位也是 `-100` |
| `attention_mask` | `(4, L)` | `input_ids != pad_token_id` |
| `idxs` | `(4, 4)` | 4 行全是 `[0, 0, 0, 0]`（都属第 0 个样本） |
| `scores` | `(1, 4)` | `[[0.477, 0.489, 0.433, 1]]` |

对应到 `compute_loss`：`batch_size = max(idxs[:,0])+1 = 1`，`can_num = 4/1 = 4`，于是 `logits.view(1, 4, L, V)`。

**预期结论**：你会直观看到「1 个样本 → 4 条物理序列」，以及最后那个 `Score=1` 的候选正是 `Response[-1]`（参考答案），它在评分训练里同时承担「SFT 域损失的目标」和「比较损失的最高分锚点」。**待本地验证**：若想确认 pad 行为，可用上一节的示例脚本接一个真实 tokenizer 打印 `input_ids.shape`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `query_input_ids` 在每个候选循环里都被重复拼接，而不是只 tokenize 一次？
**答案**：因为每条候选序列都必须是**自包含的** `query + response_i`，模型对每条序列独立前向，需要各自带上完整的指令上下文。代价是同一段 query 被 tokenize 了 N 次（轻微浪费），换来的是序列间相互独立、可任意 pad 与 reshape 的简洁结构。

**练习 2**：若某条样本的 `Response` 有 4 个候选，`idxs.append([idx] * len(scores))` 产生的行是几行、每行几个元素？`compute_loss` 实际用到了 `idxs` 的哪一部分？
**答案**：产生 4 行、每行 4 个元素（全等于该样本的下标 `idx`），即形状 `(4, 4)`。`compute_loss` 只读第一列 `inputs['idxs'][:, 0]` 来还原 `batch_size`，其余列其实是冗余的。

**练习 3**：`labels` 里 query 部分被填成 `-100`（`IGNORE_INDEX`）的目的是什么？
**答案**：让交叉熵损失忽略指令部分，只在候选答案 token 上计算损失。这样模型学的是「给定指令，如何生成答案」，而不是去背诵指令本身。

---

## 5. 综合实践

**任务**：把本讲三块内容串起来，画出「评分训练一步数据流」的完整示意图，并标注每一步发生的代码位置。

要求在你的图里至少包含以下节点与箭头（可用任何画图工具或纯文本框图）：

1. **原始 JSONL**（`{Instruction, Input, Response[N], Score[N]}`）→ 进入 `ScoreDataset`。
2. **`ScoreDataset.__init__`** 的长度过滤（`Instruction tokens < model_max_length*0.5`），标注 [train/mle_scoring.py:103](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L103)。
3. **`__getitem__`** 返回「伪装成 `input_ids` 的原始字典」，标注 [train/mle_scoring.py:110-111](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L110-L111)。
4. **`DataCollator`** 把 1 条样本展开成 N 条 `query+response` 序列，并产出 `idxs` / `scores`，标注 [train/mle_scoring.py:148-152](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L148-L152)。
5. **模型前向** 吃 `(N, L)` 的 `input_ids`，吐 `(N, L, V)` 的 logits。
6. **`compute_loss`** 用 `idxs` 还原 `(1, N, L, V)`，分别算域损失与比较损失（黑盒），标注 [train/mle_scoring.py:225-231](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L225-L231)。

**进阶思考**（选做）：在图上用虚线标出 `mle_scoring_grad_split.py` 的 `training_step`（[train/mle_scoring_grad_split.py:285-344](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L285-L344)）会在第 4 步与第 6 步之间插入什么不同之处（提示：逐候选前向，而非一次性前向 N 条）。这一步是为 `u3-l3` 做铺垫。

**预期结果**：完成后你应得到一张能解释「为什么评分训练 `per_device_train_batch_size=1` 却仍很吃显存」「为什么需要长度过滤」「候选维度是怎么被展平又还原」的端到端数据流图。

---

## 6. 本讲小结

- RTL-Coder 提供三种逐级增强的训练方案：`mle.py`（标准 SFT）、`mle_scoring.py`（评分训练，域损失+比较损失）、`mle_scoring_grad_split.py`（评分训练+梯度切分，省显存兜底）。
- 三方案的 `torchrun` 命令共用同一套超参，只在 batch 与累积步数上不同；评分方案把 batch 降到 1、累积步数翻倍，是为了**对齐有效 batch=256 的同时压缩显存**。
- 评分方案用 `ScoreDataset` 取代普通 `SupervisedDataset`：加载时按 `Instruction tokens < model_max_length * 0.5` 做长度过滤，且 `__getitem__` 返回的 `input_ids` 其实是原始字典（一个有意的 hack）。
- 真正的「多候选拼接」发生在 `DataCollatorForSupervisedDataset`：一条样本被展开成 N 条 `query+response` 序列，query 部分用 `-100` 掩码，并附带 `idxs`（归属标记）与 `scores`（质量分）。
- collator 负责「把 batch 展平成 `(总序列数, L)`」，`CompareTrainer.compute_loss` 负责「用 `idxs` 把 logits reshape 回 `(batch, N, L, V)`」——两者是一对配套契约。
- 评分训练是后续 `u3-l1/u3-l2`（损失数学）与 `u3-l3`（梯度切分）的前置；本讲只搭骨架，把损失公式和显存技巧留作黑盒。

## 7. 下一步学习建议

- 想吃透**标准 SFT 的标签掩码与 padding 细节**（`IGNORE_INDEX`、`preprocess`、`SupervisedDataset` 取 `Response[-1]`）→ 继续读 `u2-l7`（mle.py 标准监督微调）。
- 想搞懂评分训练的**损失到底怎么算**（候选 token 级 NLL、softmax 归一化、0.2/0.3 边距掩码）→ 进入专家层 `u3-l1`（评分训练）与 `u3-l2`（比较损失数学）。
- 想理解**梯度切分为什么能省显存**（覆写 `training_step`、`represent_grad` 逐候选加权反传）→ 读 `u3-l3`。
- 想了解三方案共用的**分布式配置**（ZeRO-2、optimizer 卸载到 CPU、fp16 动态 loss scale）→ 读 `u3-l4`（DeepSpeed ZeRO-2）。
