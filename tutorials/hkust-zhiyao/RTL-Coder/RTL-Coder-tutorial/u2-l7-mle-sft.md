# mle.py 标准监督微调

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清「监督微调（SFT）」训练一个因果语言模型时，损失到底落在哪些 token 上。
- 解释 `IGNORE_INDEX = -100` 的作用，以及为什么「指令部分」必须被掩码掉。
- 读懂 `train/mle.py` 里从 JSON 数据到模型可吃的 `input_ids / labels / attention_mask` 的完整管线：`SupervisedDataset` → `preprocess` → `DataCollatorForSupervisedDataset`。
- 理解 `Response[-1] + eos_token` 这一行的两个关键设计意图。
- 独立写出一段脚本，打印一条训练样本的 `labels` 张量，并验证掩码边界落在指令与响应之间。
- 知道 `smart_tokenizer_and_embedding_resize` 在扩词表时如何避免「新 token 嵌入随机初始化」带来的训练初期不稳定。

本讲只讲标准 MLE 监督微调这一条最朴素、最省显存的训练线；评分训练（`mle_scoring.py`）与梯度切分（`mle_scoring_grad_split.py`）留到 u3 系列展开。

## 2. 前置知识

### 2.1 监督微调（Supervised Fine-Tuning, SFT）

把一个已经预训练好的因果语言模型（Causal LM，即「从左往右逐 token 预测下一个 token」的模型），用一批「输入—期望输出」配对样本继续训练，让它学会按特定格式作答。RTL-Coder 的目标就是让开源底座学会「给定一段自然语言电路需求，输出可仿真的 Verilog 代码」。

### 2.2 因果语言模型的逐 token 损失

对于一个 token 序列 \(x_1, x_2, \dots, x_N\)，模型在每个位置 \(i\) 根据前缀 \(x_{\le i}\) 预测下一个 token \(x_{i+1}\)。训练目标是最小化负对数似然（NLL），也就是交叉熵：

\[
\mathcal{L}_{\text{CE}} = -\frac{1}{N}\sum_{i=1}^{N} \log p_\theta(x_{i+1} \mid x_{\le i})
\]

关键问题：在 SFT 里，我们**只想让模型学「答案」那一段**，不想让它浪费梯度去学习「复述题目」。于是需要把「题目」对应位置的 token 从损失里剔除。

### 2.3 PyTorch 的 `ignore_index`

`torch.nn.CrossEntropyLoss` 默认 `ignore_index=-100`：任何标签等于 \(-100\) 的位置都会被跳过，既不计入分子，也不计入分母 \(N\)。这就是「标签掩码」的标准实现机制——把不该学的位置标签设成 \(-100\) 即可，无需改动模型或损失函数。

> 本讲建立在 u2-l6（三种训练方案总览、`ScoreDataset`、共享 `DataCollator` 命名）与 u1-l4（`{Instruction, Response}` 数据格式、`Response` 恒为列表）之上。本讲的 `DataCollatorForSupervisedDataset` 是 u2-l6 里那个能产出 `idxs/scores` 的评分版 `DataCollator` 的「简化前辈」，建议对照阅读。

## 3. 本讲源码地图

本讲几乎只围绕一个文件：

| 文件 | 作用 |
| --- | --- |
| [train/mle.py](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py) | 标准监督微调的完整脚本：参数解析、模型/词表加载、数据集、collator、训练入口 |

它内部的关键组件（按数据流动顺序）：

1. `SupervisedDataset`（[L114-L139](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py#L114-L139)）：读 JSON，拼出 `source`（指令）/`target`（参考代码）。
2. `preprocess`（[L99-L111](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py#L99-L111)）：tokenize + 标签掩码。
3. `_tokenize_fn`（[L75-L96](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py#L75-L96)）：底层分词工具，顺便数出每条样本「非 pad token 数」。
4. `DataCollatorForSupervisedDataset`（[L142-L158](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py#L142-L158)）：把一个 batch 的不等长样本 padding 成矩形张量。
5. `smart_tokenizer_and_embedding_resize`（[L52-L72](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py#L52-L72)）：补 pad token 后扩词表、并合理初始化新嵌入。
6. `train()`（[L172-L217](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py#L172-L217)）：把以上组件粘起来跑训练的入口。

> ⚠️ **源码阅读提示（影响「直接运行」）**：`mle.py` 顶部用 `from transformers import (...)` 只导入了具体名字，但函数体/类定义里却多处用 `transformers.X` 前缀引用（如 [L43](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py#L43) 的基类 `transformers.TrainingArguments`、[L146](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py#L146) 的注解 `transformers.PreTrainedTokenizer`）。按 Python 语义，`from X import Y` **不会**把名字 `X` 绑定进当前模块命名空间，因此直接 `import` 该文件会在类定义处抛 `NameError`。若要在本地把 `preprocess` 等函数 import 出来复用，最简单的做法是在文件顶部补一行 `import transformers`。本讲的代码实践因此采用「把所需函数复制到独立脚本」的自包含方式，绕开这个问题。

## 4. 核心概念与源码讲解

### 4.1 SupervisedDataset：从 JSON 到 source / target 训练对

#### 4.1.1 概念说明

数据集类要回答一个问题：**每一条原始 JSON 样本，如何变成模型能学习的一对 `(输入, 目标)`？**

在 SFT 里，这一对通常是：

- **source（输入 / 指令）**：题目本身，模型需要「读到」但不需「学着复述」。
- **target（目标 / 答案）**：期望模型生成的参考 Verilog 代码，这是损失真正作用的地方。

`SupervisedDataset` 的职责就是把 `data_path` 指向的 JSONL 文件读进来，逐条构造出这两段字符串，再交给 `preprocess` 去做掩码。

#### 4.1.2 核心流程

```text
读 JSONL（逐行 json.loads）
  ──► list_data_dict = [{"Instruction":..., "Response":[...]}, ...]
对每条样本：
  source = Instruction + "\n"          # 指令文本，末尾补一个换行作分隔
  target = Response[-1] + eos_token    # 取最后一个候选（参考答案），末尾补结束符
把 (sources, targets) 喂给 preprocess，得到 input_ids 与 labels
```

两个细节会在 4.1.3 展开：为什么是 `Response[-1]`、为什么要补 `eos_token`。

#### 4.1.3 源码精读

数据加载与 source/target 构造（[train/mle.py:L117-L130](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py#L117-L130)）：

```python
list_data_dict = [json.loads(l) for l in open(data_path, "r")]
sources = [
   example['Instruction'] + '\n'
    for example in list_data_dict
]
targets = [f"{example['Response'][-1]}{tokenizer.eos_token}" for example in list_data_dict]
data_dict = preprocess(sources, targets, tokenizer)
```

要点：

- **逐行读 JSON**：和 u1-l4/u2-l4 一致，数据是 JSONL 格式，一行一个对象，流式低内存。
- **`Response[-1]`**：承接 u1-l4 的结论——`Response` 在 RTL-Coder 里**恒为列表**。标准 SFT 数据集 `Resyn27k.json` 每条只有一个参考答案（单元素列表），`[-1]` 取「最后一个」即该参考答案。写成 `[-1]` 而非 `[0]` 是为了与评分训练数据格式对齐：在评分数据里 `Response` 有多个候选、末位通常是参考答案（`Score=1`），同样用 `[-1]` 取参考。这是两套数据格式能复用同一段取值逻辑的关键。
- **`tokenizer.eos_token`**：在参考代码末尾显式拼接结束符。这一步至关重要——它教会模型「代码写完就停止生成」。这正是 u1-l5 提到的「Mistral 版会自动停止」的训练侧根源：模型在 SFT 阶段见多了 `...endmodule</s>`，推理时才会在 `endmodule` 后输出 `eos` 而停下；Deepseek 版之所以「不会自动停止」，正是这类停止信号学习不充分，才需要在后处理里用 `endmodulemodule` 关键字兜底。
- **`Instruction + '\n'`**：source 直接用指令原文加一个换行，不在脚本里再套「Please act as a professional Verilog designer」模板——任何需要的前缀都应当已经写进数据集的 `Instruction` 字段里（对照 `scoring_data_sample.json` 的 Instruction 自带专业设计师前缀）。

`__getitem__` 只返回原始的 `input_ids / labels` 两个 Python list，**故意不做 padding**——padding 是 batch 时才发生的事，交给 collator（[train/mle.py:L138-L139](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py#L138-L139)）：

```python
def __getitem__(self, i) -> Dict[str, torch.Tensor]:
    return dict(input_ids=self.input_ids[i], labels=self.labels[i])
```

#### 4.1.4 代码实践

**目标**：验证 source/target 的构造逻辑，理解 `Response[-1]` 与 `eos_token` 的拼接。

**操作步骤**：

1. 在仓库根目录新建 `practice_sft_source_target.py`，读取 `dataset/Resyn27k.json` 的第一行。
2. 手动复刻 `sources/targets` 的构造，打印两段字符串的首尾各 40 个字符。

```python
# practice_sft_source_target.py（示例代码）
import json
from transformers import AutoTokenizer

line = open("dataset/Resyn27k.json").readline()
ex = json.loads(line)

tok = AutoTokenizer.from_pretrained("ishorn5/RTLCoder-v1.1", use_fast=False)

source = ex["Instruction"] + "\n"
target = f"{ex['Response'][-1]}{tok.eos_token}"

print("source 头部:", repr(source[:40]))
print("target 尾部:", repr(target[-40:]))
print("Response 是列表，长度 =", len(ex["Response"]), "取 [-1]"))
```

**需要观察的现象 / 预期结果**：`source` 以指令文本开头、以 `\n` 结尾；`target` 以 `endmodule` 紧接 `</s>`（eos）结尾。`len(ex["Response"])` 对 `Resyn27k.json` 应为 1。

> 实际字符与是否需要联网下载 tokenizer 属「待本地验证」。

#### 4.1.5 小练习与答案

1. **问**：如果把 `Response[-1]` 改成 `Response[0]`，对 `Resyn27k.json` 的训练结果有没有影响？
   **答**：没有实质影响，因为 `Resyn27k.json` 的 `Response` 是单元素列表，`[-1]` 与 `[0]` 取到同一条。但若换用评分数据（多候选），`[0]` 会取到「第一个候选」而非「参考答案」，语义就错了。所以用 `[-1]` 是跨数据集兼容的更稳健写法。
2. **问**：为什么 `source` 末尾要加 `\n`？
   **答**：作为指令文本与代码之间的视觉/词法分隔，避免指令最后一个词与代码第一个 token 在分词时粘连；同时也让模型学到「看到一个换行后就要开始写代码」的位置感。

---

### 4.2 preprocess：标签掩码的精髓（IGNORE_INDEX = -100）

#### 4.2.1 概念说明

这是整篇 `mle.py` 最核心、也最值得精读的函数。它解决：**给定 source（指令）和 target（答案）两段文本，如何生成一份 `labels`，使得只有「答案」部分的 token 参与损失计算？**

答案是经典的三步走（这套写法源自 Alpaca / LLaMA 系列微调脚本，被广泛复用）：

1. 把 `source + target` 拼成完整序列，分词得到 `input_ids`。
2. 把 `labels` 初始化为 `input_ids` 的深拷贝。
3. 把 `labels` 中**属于 source 的前缀段**全部改写为 `IGNORE_INDEX = -100`。

这样，CrossEntropyLoss 会自动跳过前缀（指令），只在后缀（答案 + eos）上累计损失。

#### 4.2.2 核心流程

```text
examples  = source + target          # 拼完整序列
examples_tokenized  = tokenize(examples)   # 得完整 input_ids
sources_tokenized   = tokenize(sources)    # 单独分词 source，数出 source_len
labels = deepcopy(input_ids)
for 每条样本:
    labels[:source_len] = -100        # 把指令段掩掉
return input_ids, labels
```

对应的损失（被掩码的位置由指示函数 \(\mathbb{1}[\cdot]\) 剔除）：

\[
\mathcal{L} = -\frac{1}{\sum_i \mathbb{1}[y_i \neq -100]} \sum_{i=1}^{N} \mathbb{1}[y_i \neq -100] \cdot \log p_\theta(y_i \mid x_{<i})
\]

#### 4.2.3 源码精读

常量定义（[train/mle.py:L25](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py#L25)）：

```python
IGNORE_INDEX = -100
```

`preprocess` 主体（[train/mle.py:L99-L111](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py#L99-L111)）：

```python
examples = [s + t for s, t in zip(sources, targets)]
examples_tokenized, sources_tokenized = [_tokenize_fn(strings, tokenizer) for strings in (examples, sources)]
input_ids = examples_tokenized["input_ids"]
labels = copy.deepcopy(input_ids)
for label, source_len in zip(labels, sources_tokenized["input_ids_lens"]):
    label[:source_len] = IGNORE_INDEX
return dict(input_ids=input_ids, labels=labels)
```

`source_len` 的来源（[train/mle.py:L88-L90](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py#L88-L90)）：

```python
input_ids_lens = [
    tokenized.input_ids.ne(tokenizer.pad_token_id).sum().item() for tokenized in tokenized_list
]
```

要点：

- **掩码边界 = 单独分词 source 得到的非 pad token 数**。`ne(pad_token_id).sum()` 统计真实 token 数（排除可能的 pad）。
- **`copy.deepcopy`**：必须用深拷贝。若直接 `labels = input_ids`，后续对 `labels` 的就地赋值 `label[:source_len] = -100` 会污染 `input_ids` 本身——那样模型连输入都看不到正确 token 了。这是新手极易踩的坑。
- **一个分词边界的微妙近似**：`tokenize(source + target)` 的长度**不一定**等于 `tokenize(source)` 的长度 + `tokenize(target)` 的长度，因为 BPE/WordPiece 可能在拼接处发生 token 合并。本实现用「单独分词 source 的长度」作为掩码边界，在边界附近可能多掩或少掩一两个 token。这是 Alpaca 范式的已知近似，实践中影响很小，但要知道它不是「数学上精确」的切分。

#### 4.2.4 代码实践（本讲主实践）

**目标**：打印一条样本的 `labels` 张量，肉眼验证「指令段被 `-100` 掩码、答案段保留真实 token id」。

**操作步骤**：把 `preprocess` 与 `_tokenize_fn` 复制到一个自包含脚本里，喂一条构造好的半加器样本，打印掩码前后的解码文本。

```python
# practice_mle_masking.py（示例代码，自包含，绕开前文提到的 import 问题）
import copy
from transformers import AutoTokenizer

IGNORE_INDEX = -100

def _tokenize_fn(strings, tokenizer):
    tokenized_list = [
        tokenizer(text, return_tensors="pt", padding="longest",
                  max_length=tokenizer.model_max_length, truncation=True)
        for text in strings
    ]
    input_ids = [t.input_ids[0] for t in tokenized_list]
    input_ids_lens = [t.input_ids.ne(tokenizer.pad_token_id).sum().item() for t in tokenized_list]
    return dict(input_ids=input_ids, input_ids_lens=input_ids_lens)

def preprocess(sources, targets, tokenizer):
    examples = [s + t for s, t in zip(sources, targets)]
    ex_tok, src_tok = _tokenize_fn(examples, tokenizer), _tokenize_fn(sources, tokenizer)
    input_ids = ex_tok["input_ids"]
    labels = copy.deepcopy(input_ids)
    for label, src_len in zip(labels, src_tok["input_ids_lens"]):
        label[:src_len] = IGNORE_INDEX
    return dict(input_ids=input_ids, labels=labels)

if __name__ == "__main__":
    # 若无网络下载大模型词表，可换 "gpt2" 等任意本地 causal LM tokenizer 验证掩码逻辑
    tok = AutoTokenizer.from_pretrained("gpt2")
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    source = "Design a half adder. module half_adder(input a, input b, output sum, output cout);\n"
    target = "assign sum = a ^ b; assign cout = a & b; endmodule" + tok.eos_token

    data = preprocess([source], [target], tok)
    ids, labels = data["input_ids"][0], data["labels"][0]
    n_masked = int((labels == IGNORE_INDEX).sum())

    print("序列总长:", len(ids))
    print("被掩码 token 数（指令段）:", n_masked)
    print("保留 token 数（答案段）:", len(ids) - n_masked)
    print("掩码段解码:", repr(tok.decode(ids[:n_masked])))
    print("答案段解码:", repr(tok.decode(ids[n_masked:])))
```

**需要观察的现象 / 预期结果**：

- `被掩码 token 数` 等于 `source` 单独分词的 token 数。
- 掩码段解码得到的是指令文本（含 `module half_adder(...)` 骨架）；答案段解码得到的是 `assign ... endmodule` + eos。
- `labels[n_masked-1] == -100` 且 `labels[n_masked] != -100`（边界处由真实 token id 接管）。

> 具体数值与解码文本因 tokenizer 而异，属「待本地验证」。

#### 4.2.5 小练习与答案

1. **问**：把 `labels = copy.deepcopy(input_ids)` 改成 `labels = input_ids`，会发生什么？
   **答**：`label[:source_len] = IGNORE_INDEX` 会就地修改 `labels[i]`，而它和 `input_ids[i]` 是同一个对象，于是 `input_ids` 的指令段也被改成 `-100`。模型前向时会把 `-100` 当成输入 token id 送进 embedding 查表（查到第 -100 行，行为未定义/报错），训练彻底错乱。所以深拷贝不可省。
2. **问**：如果完全不掩码（`labels` 全等于 `input_ids`），模型还能学会写 Verilog 吗？
   **答**：能学，但学习信号被稀释——模型要同时学「复述长指令」和「写代码」，梯度有一部分浪费在与目标无关的指令 token 上，收敛更慢、效果更差。掩码的本质是「把有限的梯度预算集中到答案段」。
3. **问**：为什么掩码值偏偏是 `-100`？
   **答**：因为 `torch.nn.CrossEntropyLoss(ignore_index=-100)` 默认就是 `-100`，`DataCollator` 里用 `-100` 给 `labels` 做 padding 也是同一个理由。三者共用 `-100` 是整个链路一致跳过无效位置的约定。

---

### 4.3 DataCollatorForSupervisedDataset：padding 与 attention_mask

#### 4.3.1 概念说明

`__getitem__` 返回的是**不等长**的样本（每条指令+代码长度不同）。而一个 batch 的张量必须是矩形（每个序列等长）才能做矩阵运算。`DataCollator` 的职责就是：在每个 step 把 `Trainer` 抽取的一组样本 **padding 到本 batch 内最长那条**，并构造对应的 `attention_mask`。

#### 4.3.2 核心流程

```text
收集一个 batch 的 (input_ids, labels)
input_ids ← pad_sequence(..., padding_value=pad_token_id)   # 右侧补 pad
labels    ← pad_sequence(..., padding_value=IGNORE_INDEX)   # 右侧补 -100
attention_mask = input_ids.ne(pad_token_id)                 # 真实 token=1，pad=0
return {input_ids, labels, attention_mask}
```

注意两个 padding value 不同：`input_ids` 补 `pad_token_id`（一个合法但无意义的 token id），`labels` 补 `-100`（让损失继续跳过 pad 位置）。

#### 4.3.3 源码精读

`DataCollatorForSupervisedDataset`（[train/mle.py:L142-L158](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py#L142-L158)）：

```python
def __call__(self, instances: Sequence[Dict]) -> Dict[str, torch.Tensor]:
    input_ids, labels = tuple(
        [instance[key] for instance in instances] for key in ("input_ids", "labels")
    )
    input_ids = torch.nn.utils.rnn.pad_sequence(
        input_ids, batch_first=True, padding_value=self.tokenizer.pad_token_id
    )
    labels = torch.nn.utils.rnn.pad_sequence(labels, batch_first=True, padding_value=IGNORE_INDEX)
    return dict(
        input_ids=input_ids,
        labels=labels,
        attention_mask=input_ids.ne(self.tokenizer.pad_token_id),
    )
```

要点：

- **`pad_sequence(batch_first=True)`**：把一组一维 list/tensor 拼成 `(batch, max_len)` 的二维张量，短的在右侧补 `padding_value`。
- **两套 padding value**：`input_ids` 用 `pad_token_id`、`labels` 用 `IGNORE_INDEX`。这样 pad 位置既不会干扰注意力（被 `attention_mask` 屏蔽），也不会进损失（被 `ignore_index` 跳过）。
- **`attention_mask = input_ids.ne(pad_token_id)`**：逐元素判断「不等于 pad」→ 真实 token 为 `True`、pad 为 `False`。注意这里隐含一个假设：真实 token 的 id **绝不等于** `pad_token_id`。对绝大多数 tokenizer 成立，但严格来说，若某条样本里碰巧出现了与 pad id 相同的真实 token（极罕见），mask 会误判。这是用「值比较」构造 mask 的固有近似。

`make_supervised_data_module` 把 dataset 与 collator 打包（[train/mle.py:L161-L165](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py#L161-L165)），注意 `eval_dataset=None`——本脚本**不做训练中评估**，与 README 命令里的 `--evaluation_strategy "no"` 一致。

#### 4.3.4 代码实践

**目标**：用一个 batch 内两条不等长样本，验证 padding 方向、padding 值与 attention_mask 的正确性。

**操作步骤**：在 4.2.4 脚本基础上，复刻 collator 逻辑，喂一长一短两条样本。

```python
# practice_mle_collator.py（示例代码）
import torch
from transformers import AutoTokenizer

IGNORE_INDEX = -100
tok = AutoTokenizer.from_pretrained("gpt2")
if tok.pad_token is None:
    tok.pad_token = tok.eos_token

# 构造两条不等长的 labels（已含 -100 掩码，简化演示）
labels = [
    torch.tensor([-100, -100, 7, 8, 9, tok.eos_token_id]),   # 长 6
    torch.tensor([-100, -100, -100, 42]),                    # 长 4
]
input_ids = [l.clone() for l in labels]  # 演示用，真实情况二者不同

padded_ids = torch.nn.utils.rnn.pad_sequence(input_ids, batch_first=True, padding_value=tok.pad_token_id)
padded_lab = torch.nn.utils.rnn.pad_sequence(labels, batch_first=True, padding_value=IGNORE_INDEX)
attn = padded_ids.ne(tok.pad_token_id)

print("input_ids shape:", tuple(padded_ids.shape))
print("labels:\n", padded_lab)
print("attention_mask:\n", attn.int())
```

**需要观察的现象 / 预期结果**：`padded_ids` 形状为 `(2, 6)`（补到最长那条）；第二条样本右侧补了 2 个 `pad_token_id`；`labels` 右侧补的是 `-100`；`attention_mask` 第二行末两位为 `0`、其余为 `1`。

> 形状 `(2, 6)` 是确定的；具体 id 数值取决于 eos/pad id，属「待本地验证」。

#### 4.3.5 小练习与答案

1. **问**：为什么 `labels` 的 padding 值用 `-100` 而不是 `0`？
   **答**：`-100` 是 CrossEntropyLoss 的 `ignore_index`，pad 位置会被自动跳过、不计入损失。若用 `0`，pad 位置会被当成「真实标签 = token id 0」参与损失，等于强迫模型在所有 pad 位置预测 token 0，严重破坏训练。
2. **问**：`attention_mask` 的作用是什么？不传给模型会怎样？
   **答**：它告诉模型哪些位置是真实 token、哪些是 pad，使 pad 不参与注意力计算。不传的话，模型会把 pad token 也当成有效上下文 attend，引入大量噪声，效果下降。HuggingFace 的 causal LM 在前向时会用 `attention_mask` 对 pad 位置做掩码。

---

### 4.4 smart_tokenizer_and_embedding_resize：词表扩容与嵌入初始化

#### 4.4.1 概念说明

许多底座 tokenizer 没有 pad token（例如 LLaMA 系列原生缺 pad）。`train()` 在加载后检查 `tokenizer.pad_token is None`，若缺失就调用 `smart_tokenizer_and_embedding_resize` 往词表里加一个 pad token，并相应**扩大模型的 embedding 矩阵**。

朴素的扩法是「加了 token、resize embedding，让新行随机初始化」。问题在于：随机初始化的新嵌入在训练初期会产生不可忽略的损失，扰乱已经训练好的嵌入空间。`smart_` 版本的做法是把新 token 的嵌入初始化为**已有嵌入的均值**，让新 token 从一个「居中、温和」的起点开始学。

#### 4.4.2 核心流程

```text
num_new = tokenizer.add_special_tokens({pad_token: "[PAD]"})   # 加入新 token，返回新增个数
model.resize_token_embeddings(len(tokenizer))                  # 扩 embedding 矩阵到新词表大小
if num_new > 0:
    avg_in  = 旧 input embedding  的均值（按行）
    avg_out = 旧 output embedding 的均值
    新 input embedding 行  ← avg_in
    新 output embedding 行 ← avg_out
```

#### 4.4.3 源码精读

函数主体（[train/mle.py:L52-L72](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py#L52-L72)）：

```python
num_new_tokens = tokenizer.add_special_tokens(special_tokens_dict)
model.resize_token_embeddings(len(tokenizer))

if num_new_tokens > 0:
    input_embeddings = model.get_input_embeddings().weight.data
    output_embeddings = model.get_output_embeddings().weight.data

    input_embeddings_avg = input_embeddings[:-num_new_tokens].mean(dim=0, keepdim=True)
    output_embeddings_avg = output_embeddings[:-num_new_tokens].mean(dim=0, keepdim=True)

    input_embeddings[-num_new_tokens:] = input_embeddings_avg
    output_embeddings[-num_new_tokens:] = output_embeddings_avg
```

要点：

- **`[:-num_new_tokens]`**：取「旧词表」那部分行求均值——`resize_token_embeddings` 默认把新增行随机初始化，这里手动覆盖。
- **input 与 output 两套嵌入都处理**：causal LM 的输入 embedding（查表）和输出 `lm_head`（投影到词表 logits）是两套参数（即使某些模型二者 tied，`get_input_embeddings` / `get_output_embeddings` 返回的视图仍各自可寻址），都要初始化。
- **调用点**：在 `train()` 里仅当 pad 缺失时调用（[train/mle.py:L194-L199](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py#L194-L199)）；此外对 `llama` 底座还会补 `eos/bos/unk`（[L200-L207](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py#L200-L207)）。

#### 4.4.4 代码实践

**目标**：验证「新增 embedding 行 == 旧行均值」。

**操作步骤**：加载一个无 pad 的小 tokenizer + 模型，调用 resize 前后对比。

```python
# practice_smart_resize.py（示例代码）
import torch
from transformers import AutoTokenizer, AutoModelForCausalLM

tok = AutoTokenizer.from_pretrained("gpt2")          # gpt2 无 pad_token
mdl = AutoModelForCausalLM.from_pretrained("gpt2")
old_num = len(tok)

before_avg = mdl.get_input_embeddings().weight.data.mean(dim=0)  # resize 前全表均值（参考）

num_new = tok.add_special_tokens({"pad_token": "[PAD]"})
mdl.resize_token_embeddings(len(tok))

if num_new > 0:
    old_rows = mdl.get_input_embeddings().weight.data[:-num_new]
    avg = old_rows.mean(dim=0, keepdim=True)
    new_row = mdl.get_input_embeddings().weight.data[-1:]
    print("新增行与旧表均值差的范数:", (new_row - avg).norm().item())   # 手动赋值前应为随机；手动赋值后为 0
```

**需要观察的现象 / 预期结果**：若手动执行了 `smart_tokenizer_and_embedding_resize` 中的赋值步骤，则「新增行与旧表均值差的范数」应为 `0`（完全相等）；若只 `resize` 不赋值，则该范数明显大于 0（随机初始化）。该脚本演示对比逻辑，复现 `smart_` 的赋值需自行补上 4.4.3 中的赋值两行。

> 数值属「待本地验证」。

#### 4.4.5 小练习与答案

1. **问**：为什么用「均值」而不是「零向量」初始化新嵌入？
   **答**：零向量会让新 token 在训练第一步产生极大的 logits 偏置（偏离正常 token 的分布），损失尖刺明显；均值则让新 token 落在已有嵌入空间的「中心」，初始损失平稳，训练更稳。
2. **问**：`num_new_tokens == 0` 时函数会发生什么？
   **答**：`add_special_tokens` 返回 0 表示要加的 token 词表里已存在，`resize_token_embeddings` 即使调用也不改变大小，`if num_new_tokens > 0` 跳过均值初始化——逻辑上正确地什么都不做。

---

## 5. 综合实践

**任务**：端到端跑通「数据 → 掩码 → 组 batch」这条不含反向传播的管线，并断言掩码与 attention 的一致性。

**操作步骤**：

1. 在 `dataset/Resyn27k.json` 里取前 3 条样本。
2. 用本讲 4.1 的逻辑构造 `sources/targets`（`Instruction + '\n'`、`Response[-1] + eos`）。
3. 用 4.2 的 `preprocess` 得到 3 条 `input_ids/labels`。
4. 用 4.3 的 `DataCollatorForSupervisedDataset` 把它们组成一个 batch。
5. 打印 `input_ids / labels / attention_mask` 三个张量的形状。
6. 写两条断言验证一致性：
   - `attention_mask.sum() == (labels != -100).sum() + 各样本答案段中被 pad 掉之前的真实 token 数之外…`——更稳妥的断言是：**在 `attention_mask == 0` 的位置，`labels` 必然等于 `-100`**。
   - 即 `(labels[attention_mask == 0] == -100).all()` 应为 `True`。

**预期结果**：三个张量形状一致，均为 `(3, max_len_in_batch)`；断言通过，说明「pad 位置一定不出现在损失里」这一不变量成立。

> 这条不变量是 SFT 数据管线正确性的「金标准」——若不成立，说明 padding value 与 ignore_index 配置不一致，训练必有 silent bug。

## 6. 本讲小结

- 标准监督微调的核心是**把损失集中在答案段**：用 `IGNORE_INDEX = -100` 掩码指令部分，依赖 PyTorch `CrossEntropyLoss(ignore_index=-100)` 自动跳过。
- `SupervisedDataset` 用 `Response[-1]`（兼容单/多候选格式）取参考答案，并补 `tokenizer.eos_token` 教会模型「写完即停」——这是 Mistral 版推理能自动停止的训练侧根源。
- `preprocess` 是「拼序列 → 深拷贝 labels → 掩码前缀」三步走，掩码边界取自「单独分词 source 的长度」；`deepcopy` 不可省，否则会污染 `input_ids`。
- `DataCollatorForSupervisedDataset` 把不等长样本 padding 到 batch 内最长，`input_ids` 补 `pad_token_id`、`labels` 补 `-100`，并用 `input_ids.ne(pad_token_id)` 构造 `attention_mask`，三者共用 `-100`/pad 约定保证 pad 不进损失、不进注意力。
- `smart_tokenizer_and_embedding_resize` 在补 pad token 后扩词表，并把新嵌入初始化为旧嵌入均值，避免训练初期的损失尖刺。
- 整条数据管线正确性的「金标准」不变量：`labels[attention_mask == 0] == -100` 恒成立。

## 7. 下一步学习建议

- 本讲的 `DataCollatorForSupervisedDataset` 只产出 `{input_ids, labels, attention_mask}`；进到 **u3-l1（mle_scoring.py 质量评分训练）** 后，你会看到它的「升级版」还要额外产出 `idxs` 与 `scores`，并让 `CompareTrainer.compute_loss` 把 logits reshape 回 `(batch, N候选, L, V)`——建议直接对照两者的 `__call__` 差异。
- 关于 `IGNORE_INDEX` 掩码如何配合「域损失 + 比较损失」，见 **u3-l2（比较损失的数学原理）**；那里会把本讲的逐 token NLL 扩展成「多候选归一化」的形式。
- 若想理解为何评分训练对显存压力大、需要 u3-l3 的梯度切分，可先回忆本讲 `train()` 里的 `model.gradient_checkpointing_enable()`（[L184](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py#L184)）与 fp16 已经是两种省显存手段——评分训练在此基础上还要多算 N 个候选的前向，才需要更激进的优化。
- 想直接跑训练，按 README 的 MLE 命令（[README.md:L238-L260](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L238-L260)）用 `torchrun --nproc_per_node=4 mle.py ...` 启动；DeepSpeed ZeRO-2 配置的细节留到 u3-l4。
