# 指令数据集与自定义 collate 掩码

## 1. 本讲目标

第 5 章我们让 GPT 学会了「续写文本」，第 6 章又把它改造成了垃圾短信分类器。但真实的 ChatGPT 不仅能续写、能分类，还能**听懂指令**——你让它「把这句话改成被动语态」，它就照做。本讲要解决的，就是把一个只会续写的预训练 GPT，喂进**指令数据**、改造为「按指令回答」所需的**数据流水线**。

具体来说，学完本讲你应当能够：

- 理解为什么指令微调（instruction finetuning）需要一套特殊的**提示模板**（Alpaca 风格），并能读懂 `format_input` 如何把原始 JSON 拼成模型输入。
- 掌握 `InstructionDataset` 如何在初始化时**预先分词**整条「指令 + 回答」文本，并封装成 PyTorch `Dataset`。
- 彻底搞懂 `custom_collate_fn` 这一本讲核心：它如何把**变长**样本在批内**对齐填充**、如何构造「右移一位」的 `targets`、以及如何用 `ignore_index=-100` 做**损失掩码**——只让模型在「回答」部分上学习，把纯填充忽略掉。
- 会用 `functools.partial` 给 collate 函数**预绑定设备**，再挂到 `DataLoader` 上。

> 本讲只负责「把数据喂进去」，训练循环本身完全复用第 5 章的 `train_model_simple`，留到下一讲 u7-l2。

## 2. 前置知识

在进入源码前，先用大白话对齐几个概念：

- **预训练 vs. 指令微调**：预训练阶段的模型只会「预测下一个 token」，本质是个文本续写器；它并不懂「### Instruction」这种结构。指令微调（也叫 SFT，Supervised Finetuning）就是用大量「指令 → 正确回答」的成对样本，教模型按这种格式作答。
- **提示模板（prompt template）**：人和模型约定的一套「格式合同」。本仓库采用 Stanford **Alpaca** 风格：固定用 `### Instruction:`、`### Input:`、`### Response:` 三段标签把内容包起来，让模型学会识别「哪里是指令、哪里该开始作答」。
- **collate 函数**：`DataLoader` 从 `Dataset` 取出一批样本后，需要一个函数把它们「拼成一个 batch 张量」。默认 collate 只会简单堆叠，对**长度不一**的文本序列无能为力——所以本项目要写一个**自定义 collate**来做对齐与掩码。
- **`ignore_index=-100`**：PyTorch 的 `cross_entropy` 默认会把标签等于 `-100` 的位置**跳过**、不计入损失。这是把「不该学习」的位置屏蔽掉的标准手段。
- **`<|endoftext|>`（token ID = 50256）**：GPT-2 词表里的特殊「文本结束」符。本讲里它身兼两职：既是分隔/结束标志，也被借用当**填充符（padding token）**。

如果你对「下一个 token 预测 + 右移一位 target」这套语言模型训练目标还不熟，建议先复习 u5-l1（生成损失）与 u2-l3（滑动窗口）。

## 3. 本讲源码地图

本讲聚焦第 7 章正文的前半段，关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [`ch07/01_main-chapter-code/gpt_instruction_finetuning.py`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/01_main-chapter-code/gpt_instruction_finetuning.py) | 指令微调的**自包含脚本**，含本讲三个核心定义：`format_input`、`InstructionDataset`、`custom_collate_fn`，以及 `main()` 里装配 DataLoader 的完整代码。 |
| [`ch07/01_main-chapter-code/ch07.ipynb`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/01_main-chapter-code/ch07.ipynb) | 第 7 章正文 notebook。它把 `custom_collate_fn` **拆成三个递进草稿**（draft_1 → draft_2 → 最终版）逐步演示，是理解掩码演化过程的最佳材料。 |
| [`ch07/01_main-chapter-code/instruction-data.json`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/01_main-chapter-code/instruction-data.json) | 指令数据集，共 **1100 条**，每条是含 `instruction` / `input` / `output` 三字段的字典。本讲的实践任务就以它为输入。 |

## 4. 核心概念与源码讲解

### 4.1 Alpaca 风格指令模板与 `format_input`

#### 4.1.1 概念说明

原始数据集 `instruction-data.json` 每条长这样：

```json
{
  "instruction": "Identify the correct spelling of the following word.",
  "input": "Ocassion",
  "output": "The correct spelling is 'Occasion.'"
}
```

注意 `input` 字段**可能为空**（比如纯问答类指令没有额外输入）。如果直接把这些原始文本丢给模型，它根本分不清「哪句是指令、哪句是要我回答的内容」。所以需要用一个**固定的模板**把它们重新排版。

本仓库采用 **Alpaca** 风格模板，这是指令微调最早期的经典格式。固定结构是：

```
Below is an instruction that describes a task. Write a response that appropriately completes the request.

### Instruction:
{指令内容}

### Input:
{可选的额外输入}        ← 若 input 为空则整段省略

### Response:
{期望的回答}            ← 这部分是训练时模型要学着生成的
```

#### 4.1.2 核心流程

`format_input` 负责拼出**前两段（指令 + 可选输入）**，也就是喂给模型的输入部分；`### Response:` 及其后的回答在别处拼接。流程是：

1. 拼一段固定开场白 + `### Instruction:\n` + 指令内容。
2. 若 `entry["input"]` 非空，追加 `\n\n### Input:\n` + 输入内容；为空则什么都不加。
3. 两段字符串相加返回。

#### 4.1.3 源码精读

`format_input` 定义在自包含脚本里，逻辑极简但有两个细节值得注意：开场白用多个相邻 f-string 自动拼接（Python 隐式字符串字面量拼接）；`input_text` 用三元表达式根据是否为空决定是否追加。

[gpt_instruction_finetuning.py:113-122 — `format_input` 把原始字段拼成 Alpaca 风格输入文本](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/01_main-chapter-code/gpt_instruction_finetuning.py#L113-L122)

```python
def format_input(entry):
    instruction_text = (
        f"Below is an instruction that describes a task. "
        f"Write a response that appropriately completes the request."
        f"\n\n### Instruction:\n{entry['instruction']}"
    )

    input_text = f"\n\n### Input:\n{entry['input']}" if entry["input"] else ""

    return instruction_text + input_text
```

注意：`format_input` 只产出**到 `### Input:` 为止**的文本，**不含 `### Response:`**。回答部分在哪里拼？在 `InstructionDataset.__init__` 里手工拼上 `f"\n\n### Response:\n{entry['output']}"`（见下一模块）。训练时这套「指令在前、回答在后」的连续文本会被当作普通序列做下一个 token 预测，配合掩码只让回答段产生损失。

#### 4.1.4 代码实践

**目标**：直观看到模板效果，并确认空 `input` 会被正确省略。

**步骤**：在 `ch07/01_main-chapter-code/` 目录下运行：

```python
import json
from gpt_instruction_finetuning import format_input

with open("instruction-data.json", "r") as f:
    data = json.load(f)

# data[50] 有 input 字段；data[999] 的 input 为空
print("--- 含 input 的样本 ---")
print(format_input(data[50]))
print("\n--- 无 input 的样本 ---")
print(format_input(data[999]))
```

**观察现象 / 预期结果**：含 `input` 的样本会打印出完整的三段式（含 `### Input:`）；无 `input` 的样本在 `### Instruction:` 后**直接结束**，不出现空的 `### Input:`。这与 notebook 中 `data[50]` / `data[999]` 的演示输出一致。

#### 4.1.5 小练习与答案

**练习 1**：如果把开场白那句 `Below is an instruction...` 删掉，只保留 `### Instruction:`，模型还能训练吗？为什么要保留它？

> **参考答案**：技术上能训练，但效果会变差。开场白是一句「任务说明」，它在数据集的**每一条**里都重复出现，等于给模型一个稳定的上下文锚点，让它更容易学到「接下来的 `### Instruction:` 后面是要执行的任务」。这种固定的结构化前缀能降低学习难度、稳定格式。

**练习 2**：`input_text` 那行用了 `if entry["input"] else ""`。如果某条数据没有 `input` 这个键（而不是值为空），会发生什么？该怎么改？

> **参考答案**：会抛 `KeyError`。当前实现假设每条都含 `input` 键（本数据集确实如此）。更健壮的写法是 `entry.get("input", "")`，用 `dict.get` 提供默认值，既兼容「键不存在」也兼容「值为空」。

---

### 4.2 `InstructionDataset`：预分词与 Dataset 封装

#### 4.2.1 概念说明

有了模板，下一步是把文本变成模型能吃的 **token ID**（回顾 u2：文本 → 分词 → token ID → 嵌入）。PyTorch 的 `DataLoader` 要求被加载的对象是一个 `Dataset`（实现 `__getitem__` 与 `__len__`）。

这里有个**性能取舍**：如果在每次取样本时才临时分词，训练每个 epoch 都要重复分词，开销大。所以 `InstructionDataset` 借鉴第 6 章 `SpamDataset` 的做法，**在 `__init__` 时一次性把全部文本预先分词好**，存进列表；之后 `__getitem__` 只是 O(1) 地查表返回。

#### 4.2.2 核心流程

1. `__init__(data, tokenizer)`：遍历每条 `entry`，用 `format_input` 拼出「指令+输入」文本，再手工拼上 `\n\n### Response:\n{output}` 得到**完整序列**。
2. 用 `tokenizer.encode(full_text)` 把整条序列编码成 token ID 列表，存入 `self.encoded_texts`。
3. `__getitem__(index)` 直接返回已编码的 token ID 列表；`__len__` 返回样本数。

注意：`InstructionDataset` 返回的是**不等长的 token ID 列表**（Python list），对齐与批化的活儿全部推迟到 `custom_collate_fn`。这是「Dataset 只管取一条、collate 负责拼一批」的清晰分工。

#### 4.2.3 源码精读

[gpt_instruction_finetuning.py:35-53 — `InstructionDataset` 在初始化时预分词整条「指令+回答」](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/01_main-chapter-code/gpt_instruction_finetuning.py#L35-L53)

```python
class InstructionDataset(Dataset):
    def __init__(self, data, tokenizer):
        self.data = data
        self.encoded_texts = []
        for entry in data:
            instruction_plus_input = format_input(entry)
            response_text = f"\n\n### Response:\n{entry['output']}"
            full_text = instruction_plus_input + response_text
            self.encoded_texts.append(tokenizer.encode(full_text))

    def __getitem__(self, index):
        return self.encoded_texts[index]

    def __len__(self):
        return len(self.data)
```

关键点：`response_text` 直接用字符串拼接把 `### Response:` 与 `output` 连上，所以最终 `full_text` 是「开场白 + 指令 + 输入 + `### Response:` + 回答」一整条连续文本。**分词是对整条文本做的**，这样模板里的标签（如 `### Response:`）会被切分成固定 token，模型能稳定识别这些结构标记。

#### 4.2.4 代码实践

**目标**：验证预分词生效，并观察不同样本的**长度不一**。

**步骤**：

```python
import json, tiktoken
from gpt_instruction_finetuning import InstructionDataset

with open("instruction-data.json", "r") as f:
    data = json.load(f)

tokenizer = tiktoken.get_encoding("gpt2")
dataset = InstructionDataset(data[:5], tokenizer)   # 取前 5 条

for i in range(len(dataset)):
    ids = dataset[i]
    print(f"样本 {i}: token 数 = {len(ids)}")
```

**观察现象 / 预期结果**：每条 token 数各不相同（这正是后续要靠 collate 对齐的原因）。`__getitem__` 返回的是 Python `list[int]`，还没批化、也还没对齐。

#### 4.2.5 小练习与答案

**练习 1**：`InstructionDataset.__getitem__` 返回的是单个样本的 list，而不是已对齐的 batch。为什么把「对齐」这一步放在 collate 而不是 `__getitem__` 里？

> **参考答案**：因为对齐长度是**批内**属性——一个 batch 要补齐到「该 batch 内最长那条」的长度，不同 batch 可以有不同的对齐长度，从而节省填充与计算。`__getitem__` 在被 collate 之前取样本时，还不知道同批其它样本有多长，没法对齐。所以只能把对齐留到「能看到整个 batch」的 collate 函数里。

**练习 2**：如果把预分词删掉、改成 `__getitem__` 里临时 `tokenizer.encode(...)`，功能上还能跑通吗？代价是什么？

> **参考答案**：功能上能跑通，但每个 epoch、每次取样本都要重新分词，重复计算浪费大量时间；预分词把这部分开销前置到一次性的 `__init__`，后续训练循环里 `__getitem__` 退化为纯查表，更快。

---

### 4.3 `custom_collate_fn`：批内填充、对齐与损失掩码（核心）

这是本讲的重头戏。`custom_collate_fn` 一次性解决三件事：**变长对齐**、**构造 input/target 对**、**损失掩码**。建议配合 notebook 第 7.3 节看它的三个草稿演化，本讲直接精读最终版。

#### 4.3.1 概念说明

**① 变长对齐（padding）**：一个 batch 里的样本长短不一，而张量必须是规整矩形。本项目借用 `<|endoftext|>`（ID = 50256）当填充符，把每条补到**该 batch 内最长那条**的长度。注意是「批内对齐」而非「全数据集对齐」——后者会浪费，前者让每个 batch 都尽可能短。

**② input/target 右移一位**：语言模型的训练目标是「给前面所有 token，预测下一个 token」。所以 target 就是 input 整体右移一位：`targets[i]` 应等于 `inputs[i+1]`。这和 u2-l3 滑动窗口、u5-l1 生成损失是同一套逻辑。

**③ 损失掩码（`ignore_index=-100`）**：填充符补出来的位置，模型**根本不该去学**——否则它会把「如何输出一长串 `<|endoftext|>`」也当成训练目标，污染学习信号。PyTorch 的 `cross_entropy` 默认 `ignore_index=-100`：凡是 target 等于 `-100` 的位置，**自动从损失里剔除**。

这里有个**精妙的细节**：`<|endoftext|>` 既是填充符又是「回答结束」标志。代码特意**保留第一个** 50256 作为有效目标（教模型「回答完了要停下」），只把**之后纯用于对齐的填充**替换成 `-100`。

#### 4.3.2 核心流程

notebook 用一个 toy batch `([0,1,2,3,4], [5,6], [7,8,9])` 演示了三版草稿的进化：

- **draft_1**：只做对齐 + 填充，返回 `inputs`（还不含 target）。
- **draft_2**：再加上「右移一位」的 `targets`。
- **最终版 `custom_collate_fn`**：在 draft_2 基础上，把 targets 中「除第一个外的填充位」换成 `-100`，并支持可选的 `allowed_max_length` 截断。

最终版对每条样本的处理步骤：

1. 算 `batch_max_length = max(len(item)+1 for item in batch)`，那个 `+1` 是为「右移 target」预留一个槽位。
2. 复制样本，**先在末尾追加一个 50256**（作为「回答结束」标志，会进入 target）。
3. 用 50256 把它补到 `batch_max_length` 长度，得到 `padded`。
4. `inputs = padded[:-1]`（去掉最后一个）、`targets = padded[1:]`（整体右移一位）——两者等长。
5. **掩码**：找出 targets 中所有等于 50256 的位置；若多于 1 个，**保留第一个**、其余替换为 `-100`。
6. 可选：按 `allowed_max_length` 截断。

最终把整个 batch 堆叠成 `(batch_size, seq_len)` 张量并搬到 `device`。

#### 4.3.3 源码精读

[gpt_instruction_finetuning.py:56-96 — `custom_collate_fn`：对齐 + 右移 target + 损失掩码](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/01_main-chapter-code/gpt_instruction_finetuning.py#L56-L96)

```python
def custom_collate_fn(
    batch, pad_token_id=50256, ignore_index=-100,
    allowed_max_length=None, device="cpu"
):
    batch_max_length = max(len(item)+1 for item in batch)   # +1 为右移预留
    inputs_lst, targets_lst = [], []

    for item in batch:
        new_item = item.copy()
        new_item += [pad_token_id]                          # 追加一个结束/分隔符
        padded = new_item + [pad_token_id] * (batch_max_length - len(new_item))
        inputs  = torch.tensor(padded[:-1])                 # 去掉最后一个
        targets = torch.tensor(padded[1:])                  # 右移一位

        # 保留第一个填充位（结束标志），其余替换为 ignore_index
        mask = targets == pad_token_id
        indices = torch.nonzero(mask).squeeze()
        if indices.numel() > 1:
            targets[indices[1:]] = ignore_index

        if allowed_max_length is not None:                  # 可选截断
            inputs  = inputs[:allowed_max_length]
            targets = targets[:allowed_max_length]

        inputs_lst.append(inputs)
        targets_lst.append(targets)

    inputs_tensor  = torch.stack(inputs_lst).to(device)
    targets_tensor = torch.stack(targets_lst).to(device)
    return inputs_tensor, targets_tensor
```

用 toy batch `[0,1,2,3,4] / [5,6] / [7,8,9]` 跑最终版（notebook 第 7.3 节的实际输出），结果一目了然：

```
inputs:
 tensor([[    0,     1,     2,     3,     4],
         [    5,     6, 50256, 50256, 50256],
         [    7,     8,     9, 50256, 50256]])
targets:
 tensor([[    1,     2,     3,     4, 50256],
         [    6, 50256,  -100,  -100,  -100],
         [    8,     9, 50256,  -100,  -100]])
```

看 targets 第二行 `[6, 50256, -100, -100, -100]`：第一个 50256 被**保留**（告诉模型「这里该结束」），其后的纯填充全部变 `-100`（被损失忽略）。最长那条第一行 `[1,2,3,4,50256]` 只有一个 50256，`indices.numel()==1` 不满足 `>1`，所以原样保留。

**关于 `ignore_index` 的数学含义**。普通交叉熵对一个序列里所有 \(N\) 个位置求平均：

\[
\mathcal{L} = -\frac{1}{N}\sum_{n=1}^{N}\log p^{(n)}_{y^{(n)}}
\]

而带 `ignore_index=-100` 时，被标记为 `-100` 的位置既不进入分子、也不计入分母：

\[
\mathcal{L} = -\frac{1}{\bigl|\{n : y^{(n)}\neq -100\}\bigr|}\sum_{n:\, y^{(n)}\neq -100}\log p^{(n)}_{y^{(n)}}
\]

notebook 用一个 2 分类的小例子直接验证：3 个样本里把第 3 个标签设成 `-100` 后，损失与「只用前 2 个样本」完全相等（`loss_1 == loss_3` 为 `True`），证明 `-100` 位置确实被无视。

> **一个容易被误解的点（务必注意）**：本讲实现的 `custom_collate_fn` **只掩蔽了填充 token**，**并没有**把「指令/prompt 那段」对应的 target 也设成 `-100`。也就是说，模型其实也在学习「预测指令文本本身」。notebook 第 7.3 节明确说明：把指令部分也一并掩掉是更常见的做法，但被留作读者练习。因此实践时你会观察到：targets 里只有末尾的填充是 `-100`，而指令段仍是真实 token——这是本实现的**有意简化**，并非 bug。

#### 4.3.4 代码实践

**目标**：亲手运行 `custom_collate_fn`，验证「填充被 `-100` 掩蔽、首个 50256 被保留、指令段未被掩蔽」这一真实行为。

**步骤**：在 `ch07/01_main-chapter-code/` 目录下运行（需要已 `pip install` torch 与 tiktoken）：

```python
import json, tiktoken, torch
from gpt_instruction_finetuning import InstructionDataset, custom_collate_fn

# 1. 加载数据、构造一个微型 Dataset
with open("instruction-data.json", "r") as f:
    data = json.load(f)
tokenizer = tiktoken.get_encoding("gpt2")
dataset = InstructionDataset(data[:3], tokenizer)

# 2. 取出 3 条已分词样本，组成一个 batch（模拟 DataLoader 取批）
batch = [dataset[i] for i in range(len(dataset))]

# 3. 运行 collate
inputs, targets = custom_collate_fn(batch, device="cpu")
print("inputs shape :", inputs.shape)
print("targets shape:", targets.shape)

# 4. 把 50256 / -100 还原成可读标记，直观看到掩码位置
def tag(ids):
    return ["<PAD>" if t == 50256 else ("<IGNORE>" if t == -100 else t)
            for t in ids.tolist()]

print("\n第 0 条 inputs :", tag(inputs[0]))
print("第 0 条 targets:", tag(targets[0]))

# 5. 统计 targets 里 -100 的数量，确认掩蔽确实发生
n_ignore = int((targets == -100).sum())
n_keep   = int((targets == 50256).sum())
print(f"\n整个 batch: -100 个数 = {n_ignore}，保留的 50256 个数 = {n_keep}")
```

**观察现象 / 预期结果**：

1. `inputs` 与 `targets` 形状相同，都是 `(3, seq_len)`，其中 `seq_len` 等于本批最长样本长度。
2. 第 0 条 `targets` 末尾应出现形如 `[..., 50256, <IGNORE>, <IGNORE>, ...]` 的模式——**第一个** 50256 保留，其后纯填充显示为 `<IGNORE>`。
3. `inputs` 里填充位置仍是 `50256`（输入侧不掩蔽，模型能看到填充；只有 target 侧掩蔽）。
4. 指令段对应的 target（`<IGNORE>` 之前的部分）**仍是真实 token**，没有被 `-100` 掩蔽——印证上一节「指令段未被掩蔽」的说明。

> 若本地未装依赖无法运行，本步标注为「待本地验证」，但你完全可以参照上面 toy batch 的输出表格推演结果。

#### 4.3.5 小练习与答案

**练习 1**：`batch_max_length = max(len(item)+1 for item in batch)` 里的 `+1` 去掉会怎样？用最长样本 `[0,1,2,3,4]` 推演。

> **参考答案**：去掉 `+1` 后，最长那条 `new_item = [0,1,2,3,4,50256]` 长度已等于 `batch_max_length=5`……实际上会出现长度对不齐 / 最长样本被截掉的问题。保留 `+1` 是为了给「追加的那个 50256」留位置，使 `targets = padded[1:]` 末尾正好是那个结束符 50256，让模型学到「回答结束后要输出 `<|endoftext|>`」。简言之，`+1` 保证了结束标志能进入 target。

**练习 2**：为什么掩码只把「第一个之后的填充」换成 `-100`，而不是把**所有** 50256 都换成 `-100`？

> **参考答案**：第一个 50256 承担「回答结束信号」的职责——我们希望模型学会在回答完毕后主动产出 `<|endoftext|>` 来收尾（推理时也能据此 `eos_id=50256` 提前停止）。若全部掩成 `-100`，模型就失去了「学习何时停止」的机会。只有纯用于补齐长度的多余填充才该被忽略。

**练习 3**（进阶）：参考 notebook 的提示，如何改造 `custom_collate_fn`，把**指令/prompt 段**也掩蔽掉？

> **参考答案**：在拼 `full_text` 时记录下「`### Response:` 之前」那段（即 `format_input(entry)` 的分词结果）的长度 `prompt_len`；在 collate 里把 `targets` 中索引 `< prompt_len` 的位置也设成 `-100`。这样模型只在「回答」部分上计算损失。注意要把 `prompt_len` 一并存进 `InstructionDataset`（比如和 `encoded_texts` 并列一个列表），collate 才能拿到它。

---

### 4.4 装配 DataLoader：`partial` 注入设备与 `allowed_max_length`

#### 4.4.1 概念说明

`custom_collate_fn` 有个 `device` 参数（默认 `"cpu"`），它会把整理好的 batch **直接搬到目标设备**（GPU）。这样做有个好处：collate 是在 DataLoader 的后台进程里跑的，**在 collate 阶段就完成 `.to(device)`** 可以让数据搬运与 GPU 计算重叠，比「在主训练循环里再搬」更高效。

但 `DataLoader` 调用 collate 时只会传「batch」这一个位置参数，无法直接把 `device` 之类传进去。解决办法是用标准库 `functools.partial`：**预绑定**部分参数，生成一个「参数更少」的新函数。

#### 4.4.2 核心流程

1. 用 `torch.device(...)` 选设备。
2. `partial(custom_collate_fn, device=device, allowed_max_length=1024)` 生成 `customized_collate_fn`——它只剩 `batch` 一个参数待填。
3. 把它作为 `collate_fn=` 传给 `DataLoader`。

`allowed_max_length=1024` 对齐 GPT-2 的 `context_length`，防止超长样本越界。

#### 4.4.3 源码精读

[gpt_instruction_finetuning.py:193 — 用 `partial` 给 collate 预绑定 device 与最大长度](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/01_main-chapter-code/gpt_instruction_finetuning.py#L193)

```python
customized_collate_fn = partial(custom_collate_fn, device=device, allowed_max_length=1024)
```

[gpt_instruction_finetuning.py:200-208 — 把自定义 collate 挂到训练 DataLoader](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/01_main-chapter-code/gpt_instruction_finetuning.py#L200-L208)

```python
train_dataset = InstructionDataset(train_data, tokenizer)
train_loader = DataLoader(
    train_dataset,
    batch_size=batch_size,
    collate_fn=customized_collate_fn,
    shuffle=True,
    drop_last=True,
    num_workers=num_workers
)
```

数据划分在 `main()` 顶部完成：85% 训练 / 10% 测试 / 剩余 5% 验证，对应 1100 条里的 935 / 110 / 55。

[gpt_instruction_finetuning.py:170-175 — 三划分：85% 训练 / 10% 测试 / 5% 验证](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch07/01_main-chapter-code/gpt_instruction_finetuning.py#L170-L175)

```python
train_portion = int(len(data) * 0.85)  # 85% for training
test_portion = int(len(data) * 0.1)    # 10% for testing
train_data = data[:train_portion]
test_data = data[train_portion:train_portion + test_portion]
val_data = data[train_portion + test_portion:]
```

> 提示：`drop_last=True`（训练集）会丢掉最后不足一个 batch 的样本，保证每个 batch 都是完整的 `batch_size`；验证/测试集用 `drop_last=False` 以便评估覆盖全部样本。

#### 4.4.4 代码实践

**目标**：搭一条最小 DataLoader，复现 notebook 中「各 batch 长度不同、但 batch_size 恒为 8」的现象。

**步骤**：

```python
import json, tiktoken, torch
from functools import partial
from torch.utils.data import DataLoader
from gpt_instruction_finetuning import InstructionDataset, custom_collate_fn

with open("instruction-data.json", "r") as f:
    data = json.load(f)
tokenizer = tiktoken.get_encoding("gpt2")

torch.manual_seed(123)
dataset = InstructionDataset(data[:40], tokenizer)          # 取 40 条
collate = partial(custom_collate_fn, device="cpu", allowed_max_length=1024)
loader  = DataLoader(dataset, batch_size=8, collate_fn=collate,
                     shuffle=True, drop_last=True)

for i, (inputs, targets) in enumerate(loader):
    print(f"batch {i}: inputs {tuple(inputs.shape)}")
    if i == 2:
        break
```

**观察现象 / 预期结果**：每个 batch 第一维都是 `8`（batch_size），但第二维（序列长度）**各不相同**——这正是「批内对齐」的体现：每个 batch 只补到自身最长那条的长度。

#### 4.4.5 小练习与答案

**练习 1**：为什么用 `partial` 而不是直接写一个 `lambda batch: custom_collate_fn(batch, device=..., allowed_max_length=...)`？

> **参考答案**：两者功能等价，`partial` 是标准做法、更清晰、且对 `pickle` 友好（`DataLoader` 在 `num_workers>0` 时会把 collate 函数序列化传给子进程，`partial` 可被 pickle，而某些 `lambda` 在多进程下会报错）。本项目 `num_workers=0` 时区别不大，但用 `partial` 是更稳妥的工程习惯。

**练习 2**：如果把 `allowed_max_length` 设成一个很小的值（比如 30），会发生什么？

> **参考答案**：collate 里的 `inputs = inputs[:allowed_max_length]` 会把每条样本**截断**到前 30 个 token，于是所有 batch 的序列长度上限变为 30。这能防止超长样本超过模型 `context_length`，但也会**截掉回答的后半段**，导致训练信号不完整——所以 `allowed_max_length` 应与模型上下文长度（GPT-2 为 1024）匹配，不能随意调小。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，写一个独立的探查脚本，完整走一遍「JSON 原始数据 → 模板化 → 预分词 → 批内对齐 → 损失掩码 → DataLoader」全链路，并人工核对掩码是否符合预期。

**要求**：

1. 加载 `instruction-data.json`，取前 8 条作为 `train_data`。
2. 用 `format_input` 打印第 0 条的完整「输入 + 回答」文本（含 `### Response:`），确认模板正确。
3. 用 `InstructionDataset` 预分词，再用 `partial + custom_collate_fn` 组成一个 `batch_size=8` 的 `DataLoader`。
4. 取出唯一一个 batch，把 `targets[0]` 与 `inputs[0]` 解码成「可读 + `<PAD>`/`<IGNORE>` 标记」的形式并打印。
5. **核对三件事**并写下你的观察：
   - `targets` 末尾是否出现 `[..., 50256, -100, -100, ...]`（首个结束符保留、其余填充掩蔽）？
   - `inputs` 的填充位是否仍是 `50256`（输入侧不掩蔽）？
   - 指令/prompt 段的 target 是否**未被** `-100` 掩蔽（印证本实现的有意简化）？
6. （选做）参照 4.3.5 练习 3，给 `InstructionDataset` 增加一个 `prompt_lengths` 列表，改造 collate 把 prompt 段也掩成 `-100`，重新运行第 4 步，对比前后 targets 的差异。

**预期结果**：第 1～4 步能顺利产出对齐的 `(8, seq_len)` 张量；第 5 步的三项核对全部为「是」。第 6 步（选做）会看到指令段 target 从「真实 token」变为 `-100`，这正是工业界更常见的做法，也让你真正理解 `ignore_index` 的价值。

> 若本地环境无 GPU/缺依赖，第 1～5 步可纯 CPU 跑通；只要装好 `torch` 与 `tiktoken` 即可，无需下载预训练权重（本讲完全不碰模型）。

## 6. 本讲小结

- 指令微调（SFT）用「指令→回答」成对样本教模型按固定格式作答；本仓库用 **Alpaca 风格模板**，由 `format_input` 把原始三字段拼成 `### Instruction:` / `### Input:` 前缀。
- `InstructionDataset` 在 `__init__` 时**预分词**整条「指令+输入+`### Response:`+回答」文本，返回不等长的 token ID 列表，把对齐工作推迟到 collate。
- `custom_collate_fn` 是本讲核心：它做**批内对齐**（补 50256）、构造**右移一位的 target**、并用 `ignore_index=-100` 做**损失掩码**——保留首个 `<|endoftext|>` 作为结束信号，把纯填充从损失中剔除。
- `ignore_index=-100` 依赖 PyTorch `cross_entropy` 默认行为：标签为 `-100` 的位置**既不计入分子也不计入分母**，从而被完全忽略。
- 本实现**只掩蔽填充、不掩蔽指令段**（指令段掩蔽被留作读者练习），这是源码的真实行为，实践时务必据此核对。
- 用 `functools.partial` 给 collate **预绑定 `device` 与 `allowed_max_length`**，再挂到 `DataLoader`，能在后台进程阶段就完成数据搬运，提升训练效率。

## 7. 下一步学习建议

本讲完成了第 7 章的**数据准备**：你已经能把指令数据喂成「带掩码的 input/target batch」。接下来：

- **u7-l2 指令微调训练与响应生成**：把本讲产出的 `train_loader` 接上第 5 章的 `train_model_simple`（直接复用，因为损失计算天然支持 `-100` 掩码），在 gpt2-medium 预训练权重上微调 2 个 epoch，并在测试集上生成、保存响应。
- **若想深入掩码**：动手完成本讲 4.3.5 练习 3 与综合实践第 6 步，实现「prompt 段掩蔽」，体会工业级 SFT 的完整做法。
- **延伸阅读**：notebook 第 7.3 节的三个 collate 草稿值得逐行对比；之后可继续看 `ch07/04_preference-tuning-with-dpo/`（偏好对齐，对应 u11-l2）了解指令微调之后更进一步的人类偏好优化。
