# 数据集加载与标签/掩码构造

## 1. 本讲目标

本讲是「模型吃什么」输入链路的第二站（上一站 u2-l1 解决了「文本 ↔ token id」，本站解决「token id ↔ 训练张量」）。读完本讲你应该能够：

1. 说清 `PretrainDataset`、`SFTDataset`、`DPODataset`、`RLAIFDataset`、`AgentRLDataset` 这 5 个类的「数据形态」和「返回值」分别长什么样。
2. 理解为什么预训练对整段文本算 loss，而 SFT 只对 assistant 的回答段算 loss。
3. 手写/看懂 `SFTDataset.generate_labels`：它如何用 `bos_id` / `eos_id` 这两个小片段在 token 序列里「夹」出 assistant 段，并把其余位置置为 `-100`。
4. 区分三种强化学习相关的数据：DPO 的 chosen/rejected 偏好对、RLAIF 的「留空 answer」、Agent 的 `messages + tools + gt` 多轮轨迹。
5. 看懂 `pre_processing_chat` / `post_processing_chat` 这两个公共处理函数对对话数据做的轻量增强与思考标签处理。

---

## 2. 前置知识

在进入源码前，先用通俗语言把几个贯穿全讲的底层概念讲清楚。它们都来自上一讲（u2-l1）和更早的 u1，这里只做最小回顾。

- **token / input_ids**：文本被分词器切成一串整数 id（参见 u2-l1）。本讲里出现的一串 id 序列记作 \(x_0, x_1, \dots, x_{n-1}\)。
- **next-token prediction（下一个 token 预测）**：语言模型训练的根本目标——给定前面所有 token，预测下一个 token。也就是说，模型在位置 \(t\) 看到的是 \(x_{\le t}\)，要预测的是 \(x_{t+1}\)。
- **位移（shift）**：因为「看前一个、猜后一个」，所以训练时输入和目标要错开一位：输入取 \(x_0,\dots,x_{n-2}\)，目标取 \(x_1,\dots,x_{n-1}\)。代码里常见的写法就是 `x = input_ids[:-1]`、`y = input_ids[1:]`。
- **labels 与 `-100`**：PyTorch 的 `CrossEntropyLoss` 有一个 `ignore_index=-100` 参数——只要某个位置的标签等于 `-100`，它在 loss 里就被跳过，不产生梯度。所以「不想让模型学的位置」一律标成 `-100`，这就是**掩码（mask）**的核心手法。
- **padding**：训练需要把长短不一的样本拼成等长 batch，超出的部分用 `pad_token`（本项目里是 `<|endoftext|>`，见 u2-l1）补齐。补出来的 pad 位置当然不该算 loss，所以也被置成 `-100`。
- **chat_template**：上一讲讲过，它把 system/user/assistant/tool 四种角色的对话渲染成一段带 `<|im_start|>` / `<|im_end|>` 边界的纯文本。本讲要反复用到它渲染出来的**固定片段**。

> 一句话总结：**本讲所有花活，本质都是在回答一个问题——「这条 token 序列里，哪些位置该参与 loss、哪些该被 `-100` 屏蔽掉」**。预训练说「全都要」，SFT 说「只要 assistant 回答」，DPO 说「我要同时给你一个好回答和一个坏回答」。

---

## 3. 本讲源码地图

本讲只读一个核心文件，外加 README 里的数据格式说明：

| 文件 | 作用 |
| --- | --- |
| [dataset/lm_dataset.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py) | 全部 5 个 Dataset 类 + 2 个 chat 预处理函数，本讲的主战场 |
| [README.md](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md) | 各数据集的 `.jsonl` 原始格式说明（第 Ⅱ/Ⅲ/Ⅳ 节） |
| [model/tokenizer_config.json](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/tokenizer_config.json) | `chat_template`，决定 assistant 段渲染成什么样的固定片段（u2-l1 已讲） |

`lm_dataset.py` 内部的布局速览（行号便于跳转）：

| 区块 | 行号 | 内容 |
| --- | --- | --- |
| `pre_processing_chat` | 9–29 | 概率性给对话插一句 system prompt |
| `post_processing_chat` | 31–35 | 概率性移除空思考标签 |
| `PretrainDataset` | 37–55 | 纯文本 → 加 bos/eos → pad 置 -100 |
| `SFTDataset` | 58–119 | 多轮对话 → 渲染 → `generate_labels` 定位 assistant 段 |
| `DPODataset` | 122–192 | chosen/rejected → 各自 `generate_loss_mask` |
| `RLAIFDataset` | 195–224 | 留空 answer，交给 rollout 续写 |
| `AgentRLDataset` | 226–252 | 不分词，原样返回 messages + tools + gt |

---

## 4. 核心概念与源码讲解

### 4.1 输入链路全景：5 种 Dataset 与「labels / -100 / loss_mask」原理

#### 4.1.1 概念说明

MiniMind 的 5 个 Dataset 类对应项目 5 个不同的训练阶段，它们的**唯一共同职责**是：把磁盘上的 `.jsonl` 样本，变成训练循环 `for input_ids, labels in loader` 里那一对张量（或字典）。它们**不负责**：定义模型结构、计算 loss、更新参数——那些都在 `trainer/` 里。

这 5 个类的根本差异只有两点：

1. **读什么格式的 `.jsonl`**（纯文本？多轮对话？偏好对？带 gt 的工具轨迹？）
2. **返回什么、以及怎么标 mask**（整段算 loss？只算 assistant 段？返回 loss_mask 而不是 labels？完全不分词？）

#### 4.1.2 核心流程

一张表看懂全景（这是本讲的「地图」，后续每个模块都在填这张表的某一行）：

| 类 | 数据文件 | 原始字段 | `__getitem__` 返回 | mask 策略 | 训练阶段 |
| --- | --- | --- | --- | --- | --- |
| `PretrainDataset` | `pretrain_t2t*.jsonl` | `{"text": ...}` | `(input_ids, labels)` | 整段算 loss，pad 置 -100 | Pretrain |
| `SFTDataset` | `sft_t2t*.jsonl` | `{"conversations": [...]}` | `(input_ids, labels)` | 只算 assistant 段 | Full SFT |
| `DPODataset` | `dpo.jsonl` | `{"chosen":[...], "rejected":[...]}` | dict（含 chosen/rejected 的 x/y/mask） | 各算 assistant 段的 loss_mask | DPO |
| `RLAIFDataset` | `rlaif.jsonl` | `{"conversations":[...]}` | `{"prompt":..., "answer":""}` | 不分词、不标 mask（rollout 时算） | PPO/GRPO/CISPO |
| `AgentRLDataset` | `agent_rl*.jsonl` | `{"conversations":[...], "gt":...}` | `{"messages":..., "tools":..., "gt":...}` | 不分词、不标 mask | Agentic RL |

贯穿后三类有一个关键直觉：**越靠后的训练阶段，Dataset 越「懒」**。预训练/SFT 把 token 序列和掩码都给你算好；到了强化学习阶段，因为回答要由模型自己在线生成（rollout），Dataset 就只给「题目」（prompt 或 messages），把「答案」留空，交给训练脚本的 rollout 引擎去填。

#### 4.1.3 源码精读

这 5 个类都继承自 `torch.utils.data.Dataset`，统一用 HuggingFace `datasets.load_dataset('json', ...)` 把 `.jsonl` 懒加载进内存（`AgentRLDataset` 例外，它手动按行 `json.loads`）。文件顶部的导入与并行抑制设置：

[dataset/lm_dataset.py:1-7](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L1-L7) —— 引入 `Dataset` 基类、`load_dataset`，并设置 `TOKENIZERS_PARALLELISM=false` 避免分词器与 PyTorch DataLoader 的多进程并行冲突。

5 个类的原始数据格式由 README 明确规定，建议对照阅读：

- 预训练格式：[README.md:413-419](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L413-L419) —— 每行一个 `{"text": "..."}`。
- SFT 格式：[README.md:437-457](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L437-L457) —— `conversations` 列表，含普通对话与 Tool Call 两种样例。
- DPO 格式：[README.md:465-478](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L465-L478) —— `chosen` / `rejected` 两个对话列表。
- 其余 RL 数据「与 SFT 格式一致，但把最后一个 assistant 位置留空」：[README.md:480](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L480)。

#### 4.1.4 代码实践

**目标**：动手前，先用「眼睛」建立全景印象。

1. 打开 `dataset/lm_dataset.py`，用编辑器的折叠把 5 个 class 各自收起，只看每个 class 的 `__getitem__` 返回值类型。
2. 对照上面的全景表，确认你能在源码里指出每行「返回了什么」。

**需要观察的现象**：`PretrainDataset` 和 `SFTDataset` 返回的是元组 `(input_ids, labels)`；`DPODataset` 返回字典；`RLAIFDataset` / `AgentRLDataset` 返回的字典里**根本没有 input_ids**。这个差异正是「越往后越懒」的体现。

**预期结果**：你能不看表、只看源码说出 5 个类的返回类型。本实践无需运行，纯阅读型。

#### 4.1.5 小练习与答案

- **练习**：如果一个类返回的张量里完全没有 `input_ids`，它大概率服务于哪个训练阶段？
- **答案**：强化学习阶段（RLAIF / Agentic RL）。因为这些阶段的回答要由模型在线 rollout 生成，Dataset 只负责提供题目，所以不需要预先分词成 `input_ids`。

---

### 4.2 PretrainDataset：next-token prediction 与 padding→-100

#### 4.2.1 概念说明

预训练的目标最简单纯粹：**词语接龙**。给它任何一段自然语言文本，它都要学。所以 `PretrainDataset` 的 mask 策略是「整段都算 loss，只有补出来的 padding 不算」。

它对每条样本做三件事：① 在文本前后各包一个 bos/eos 边界 token；② 不够 `max_length` 的部分用 pad 补齐；③ 把 labels 复制成 input_ids 的副本，再把 pad 位置改写成 `-100`。

#### 4.2.2 核心流程

```
原始文本 text
   │  tokenizer(add_special_tokens=False, truncation 到 max_length-2)
   ▼
tokens（中间正文，已预留 2 个位置给 bos/eos）
   │  前后各加一个边界 token
   ▼
[bos] + tokens + [eos]
   │  尾部 pad 到 max_length
   ▼
input_ids  （等长序列）
   │  labels = input_ids.clone()
   │  labels[pad 位置] = -100
   ▼
返回 (input_ids, labels)
```

注意一个细节：分词时显式传 `add_special_tokens=False`，意思是「分词器别自作主张加 bos/eos」，因为这条样本的 bos/eos 由代码自己控制（用 `tokenizer.bos_token_id` / `eos_token_id`，本项目里就是 `<|im_start|>` / `<|im_end|>`，见 u2-l1）。

带掩码的交叉熵可以写成（设 \(m_t = 1\) 表示位置 \(t\) 参与 loss）：

\[
\mathcal{L} = -\frac{1}{\sum_t m_t} \sum_{t} m_t \, \log p_\theta(x_{t+1} \mid x_{\le t})
\]

对预训练，\(m_t = 1\) 当且仅当 \(x_t \ne \text{pad}\)。

#### 4.2.3 源码精读

[dataset/lm_dataset.py:37-55](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L37-L55) —— 整个 `PretrainDataset`。逐行说明：

- 第 49 行：分词正文，`max_length=self.max_length - 2` 给首尾的 bos/eos 预留位置，`truncation=True` 超长就截断。
- 第 50 行：`[bos] + tokens + [eos]`，手动加边界。bos 让模型知道「一段开始了」，eos 让模型学会「该停了」。
- 第 51 行：尾部补 pad 到 `max_length`，得到定长 `input_ids`。
- 第 53–54 行：`labels = input_ids.clone()`，再用布尔索引 `labels[input_ids == pad_token_id] = -100` 把 pad 位置屏蔽。

#### 4.2.4 代码实践

**目标**：验证「预训练 labels 里只有 pad 是 -100，其余原样保留」。

**操作步骤**（在项目根目录新建脚本运行，**示例代码**，非项目原有）：

```python
# practice_pretrain_labels.py
import torch
from transformers import AutoTokenizer
from dataset.lm_dataset import PretrainDataset

tok = AutoTokenizer.from_pretrained('./model', trust_remote_code=True)
# 造一个迷你 jsonl，避免下载 1.2GB 的 pretrain_t2t_mini.jsonl
import os, json
os.makedirs('./dataset', exist_ok=True)
p = './dataset/_mini_pretrain.jsonl'
with open(p, 'w', encoding='utf-8') as f:
    f.write(json.dumps({"text": "清晨的阳光透过窗帘洒进房间。"}, ensure_ascii=False) + '\n')

ds = PretrainDataset(p, tok, max_length=32)
input_ids, labels = ds[0]
print("bos_id=", tok.bos_token_id, "eos_id=", tok.eos_token_id, "pad_id=", tok.pad_token_id)
for i, (a, b) in enumerate(zip(input_ids.tolist(), labels.tolist())):
    print(f"{i:2d} id={a:6d} label={b:6d} {'<- PAD屏蔽' if b == -100 else ''}")
```

**需要观察的现象**：序列开头是 `bos_token_id`，结尾出现 `eos_token_id`，其后直到 32 长度的位置全是 `pad_token_id`，而这些 pad 位置在 labels 里全部变成 `-100`，其余位置 labels 与 input_ids 完全相等。

**预期结果**：参与 loss 的 token 数 = `len(正文) + 2`（bos+eos），其余为 -100。若运行环境无数据集/无 GPU，本脚本仍可纯 CPU 跑通（只需装好 transformers 与 tokenizer 文件），具体数值「待本地验证」。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `max_length - 2`？减的那个 2 对应什么？
- **答案**：对应首尾的 bos 和 eos 两个 token。先给它们留好位置，正文才不会被截断到刚好顶满 `max_length` 而放不下边界符。
- **练习 2**：如果把第 54 行的 `labels[input_ids == pad] = -100` 删掉会怎样？
- **答案**：pad 位置（`<|endoftext|>`）也会参与 loss，模型会被迫学习「在正常文本中间预测 pad」，这是错误的监督信号，会拖慢收敛、污染生成质量。

---

### 4.3 SFTDataset 与 generate_labels：用 bos_id/eos_id 精确定位 assistant 段

> 这是本讲最重要、也是最容易看走眼的一个模块。SFT 的全部「对齐」功夫，都浓缩在 `generate_labels` 这一个函数里。

#### 4.3.1 概念说明

SFT（监督微调）的目标不是「学会说话」，而是「学会**好好回答**」。所以它的 mask 策略与预训练相反：**用户提问段不算 loss，只有 assistant 的回答段（含它要生成的 `<think>` 思考块和结尾的 `<|im_end|>`）才算 loss**。否则模型会学着「自己提问」，那不是我们想要的。

难点在于：渲染后的 token 序列里，user 段和 assistant 段是**交错拼接**在一起的（`...<|im_start|>user\n你好<|im_end|>\n<|im_start|>assistant\n你好！<|im_end|>\n...`），怎么自动找出「哪些 token 属于 assistant 回答」？

MiniMind 的解法非常巧妙：**利用 chat_template 渲染出来的固定片段做子串匹配**。

#### 4.3.2 核心流程

回顾 u2-l1 的 chat_template，assistant 段渲染出来形如：

```
<|im_start|>assistant\n<think>\n ... \n</think>\n\n 回答正文 <|im_end|>\n
└──── bos_id ────┘                                  └── eos_id ──┘
```

于是定义两个「锚点片段」：

- `bos_id = tokenizer('<|im_start|>assistant\n')` —— assistant 段的**起跳点**
- `eos_id  = tokenizer('<|im_end|>\n')` —— assistant 段的**落点**

`generate_labels` 在整条 `input_ids` 序列上滑动，一旦在某位置 `i` 匹配到 `bos_id`，就从 `i + len(bos_id)` 开始，向后找到第一个 `eos_id`，把 `[start, end + len(eos_id))` 这段标记为「参与 loss」，其余保持 `-100`。多轮对话里有多少个 assistant 段，就重复多少次。

```
labels 初始全 -100
for 每个匹配到的 bos_id:
    start = bos_id 之后第一个位置
    end   = 从 start 往后找到的第一个 eos_id 起点
    labels[start .. end+len(eos_id)) = input_ids[同区间]   # 含 eos 本身
返回 labels
```

#### 4.3.3 源码精读

先看锚点是如何在构造函数里定义的：

[dataset/lm_dataset.py:65-66](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L65-L66) —— `bos_id` / `eos_id` 就是把模板里的固定片段直接分词，得到一串 id（注意末尾的 `\n` 也包含在内，必须和模板渲染结果**逐 token 一致**，否则匹配失败）。

再看核心函数：

[dataset/lm_dataset.py:88-104](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L88-L104) —— `generate_labels`。关键点逐条：

- 第 89 行：`labels` 初始化为全 `-100`，默认「什么都不学」。
- 第 92 行：用切片比较 `input_ids[i:i+len(bos_id)] == self.bos_id` 做「子串匹配」，命中即找到一个 assistant 段起点。
- 第 99 行：标记区间是 `[start, end + len(eos_id))`——**注意把 eos 也算进了 loss**。这点至关重要：模型必须学会在回答末尾生成 `<|im_end|>`，否则推理时不知道何时停止。
- 第 99 行的 `min(..., self.max_length)`：防止 eos 越过截断点。

最后看 `__getitem__` 怎么把它们串起来：

[dataset/lm_dataset.py:106-119](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L106-L119) —— 流程是 `pre_processing_chat`（增强）→ `create_chat_prompt`（渲染）→ `post_processing_chat`（处理空思考）→ 分词截断 → pad → `generate_labels`。第 114–118 行还保留了一段被注释的调试打印，正是本讲「综合实践」要复刻的对照表。

> 延伸：`create_chat_prompt` 里 [dataset/lm_dataset.py:71-86](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L71-L86) 显式声明了 `features` 字段类型（含 `tool_calls`/`tools`），是为了让 `tool_calls` 这种「可能是 list 也可能是字符串」的字段被稳定加载，便于第 78–79 行 `json.loads` 反序列化；`add_generation_prompt=False` 表示「完整渲染含最终 assistant 回答」，这样 `generate_labels` 才有 assistant 段可定位。

#### 4.3.4 代码实践

**目标**：亲手加载一条 SFT 样本，打印 `X → Y → label` 的对齐表，确认**只有 assistant 回答段（含 `<think>` 和 `<|im_end|>`）的 label 不是 -100**。

**操作步骤**（项目根目录新建脚本运行，**示例代码**）：

```python
# practice_inspect_sft_labels.py
import json, os, torch
from transformers import AutoTokenizer
from dataset.lm_dataset import SFTDataset   # 复用真实代码路径

tokenizer = AutoTokenizer.from_pretrained('./model', trust_remote_code=True)

# 造一条多轮对话样本（无需下载 1.6GB 的 sft_t2t_mini.jsonl）
sample = {"conversations": [
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "你好！我是 minimind。"},
    {"role": "user", "content": "用一句话介绍自己"},
    {"role": "assistant", "content": "我是一个小巧但有用的语言模型。"},
]}
os.makedirs('./dataset', exist_ok=True)
mini_path = './dataset/_mini_sft_inspect.jsonl'
with open(mini_path, 'w', encoding='utf-8') as f:
    f.write(json.dumps(sample, ensure_ascii=False) + '\n')

ds = SFTDataset(mini_path, tokenizer, max_length=96)
input_ids, labels = ds[0]

# 复刻源码 114-118 行的调试段：位移对齐 X -> Y -> label
for i in range(len(input_ids) - 1):
    x = tokenizer.decode([input_ids[i].item()])
    y = tokenizer.decode([input_ids[i + 1].item()])
    lab = labels[i + 1].item()
    mark = '  <<<< 参与 loss' if lab != -100 else ''
    print(f"{i:3d}: X={x!r:22s} -> Y={y!r:22s} label={lab}{mark}")

print(f"\n序列总长 {len(input_ids)}，参与 loss 的 token 数 = {(labels != -100).sum().item()}")
```

**需要观察的现象**：

1. 当 `X` 处于 user 段（`你好`、`用一句话介绍自己`）时，对应的 `label` 全是 `-100`。
2. 当 `X` 跨入 `<|im_start|>assistant\n` 之后，`label` 开始等于真实 token（包括 `<think>` 块和最后的 `<|im_end|>`）。
3. 因为 `post_processing_chat` 有 80% 概率移除空 `<think>\n\n</think>\n\n`（见 4.4），每次运行看到的 think 段可能不同——可多跑几次对照。

**预期结果**：参与 loss 的 token 数等于「两个 assistant 回答段（含各自 `<think>` 与结尾 `<|im_end|>`）的 token 总数」。若你下载了真实 `sft_t2t_mini.jsonl`，把 `mini_path` 换成它即可验证真实分布；具体数值「待本地验证」。

#### 4.3.5 小练习与答案

- **练习 1**：为什么预训练整段算 loss，SFT 只算 assistant 段？
- **答案**：预训练是通用「词语接龙」，整段文本都是学习素材；SFT 是对齐任务，只想教「如何回答」，用户提问不该学，否则模型会学着生成提问。
- **练习 2**：如果把第 99 行的 `end + len(eos_id)` 改成 `end`（不含 eos），训练出的模型推理时会有什么毛病？
- **答案**：模型学不会在回答末尾生成 `<|im_end|>`，推理时会一直吐 token 直到撞上 `max_length`，无法自然停止。
- **练习 3**：`bos_id` 末尾那个 `\n` 能不能省？
- **答案**：不能。它必须和 chat_template 渲染结果逐 token 一致；少了 `\n`，匹配会错位，`start` 点就会偏移，loss 标错位置。

---

### 4.4 pre_processing_chat 与 post_processing_chat：数据增强与思考标签处理

#### 4.4.1 概念说明

这两个是模块级的小函数（不属于任何类），被 `SFTDataset`、`DPODataset`、`RLAIFDataset` 复用，用来在「渲染前/后」对对话做两件轻量但重要的随机化处理：

- `pre_processing_chat`（渲染**前**）：以一定概率给没有 system 角色的对话**随机插入一句 system prompt**，做数据增强，让模型见过多样的系统提示。
- `post_processing_chat`（渲染**后**）：以一定概率**移除空的思考标签** `<think>\n\n</think>\n\n`，让模型在训练时既见过「先思考再答」也见过「直接答」，从而学会自适应地决定要不要输出思考。

#### 4.4.2 核心流程

```
原始 conversations
   │  pre_processing_chat   (按 add_system_ratio 插 system)
   ▼
apply_chat_template       (渲染成带 <|im_start|> 的文本)
   │  post_processing_chat (按 empty_think_ratio 移除空 <think>)
   ▼
最终 prompt 文本
```

`pre_processing_chat` 有个特例：**含 `tools` 字段的 Tool Use 数据原样返回、不插 system**，因为这类数据的 system 槽位已经被工具说明占据，乱插会破坏工具协议。

`post_processing_chat` 处理的是 chat_template 给每个无 `reasoning_content` 的 assistant 段自动渲染出的空思考块 `<think>\n\n</think>\n\n`（见 u2-l1 的模板）。80% 概率删掉它，意味着训练样本里大多数 assistant 回答是「直接答」的，少数保留空思考块——这让模型同时掌握两种风格，对应 README 里「思考不再是独立阶段，靠模板+开关控制」的设计（参见 [README.md:820](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L820)）。

#### 4.4.3 源码精读

[dataset/lm_dataset.py:9-29](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L9-L29) —— `pre_processing_chat`：

- 第 11 行：含 tools 的样本直接 return，不动。
- 第 13–24 行：内置 10 条中英文 system prompt 池子。
- 第 26–28 行：若首条不是 system，且 `random.random() < 0.2`，就在最前面插一条随机 system。

[dataset/lm_dataset.py:31-35](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L31-L35) —— `post_processing_chat`：

- 第 33 行：`random.random() > 0.2` 即以 80% 概率进入移除分支（`empty_think_ratio=0.2` 是「保留」的概率）。
- 第 34 行：字符串 `replace` 把 `<think>\n\n</think>\n\n` 删掉。

调用点可对照 [dataset/lm_dataset.py:108-110](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L108-L110)（SFT）和 [dataset/lm_dataset.py:142-147](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L142-L147)（DPO chosen/rejected 各调一次）。

#### 4.4.4 代码实践

**目标**：观察 `post_processing_chat` 的随机性如何影响同一条样本的渲染结果。

**操作步骤**（**示例代码**）：

```python
# practice_post_chat.py
import random
from transformers import AutoTokenizer
from dataset.lm_dataset import pre_processing_chat, post_processing_chat

tok = AutoTokenizer.from_pretrained('./model', trust_remote_code=True)
convs = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好！"}]

for seed in range(6):
    random.seed(seed)
    c = pre_processing_chat(convs)
    prompt = tok.apply_chat_template(c, tokenize=False, add_generation_prompt=False)
    prompt = post_processing_chat(prompt)
    has_empty_think = '<think>\n\n</think>\n\n' in prompt
    print(f"seed={seed}  插入system={c[0]['role']=='system'}  保留空think={has_empty_think}")
```

**需要观察的现象**：不同 seed 下，「是否插入 system」「是否保留空 think」会变化；保留空 think 的比例约为 20%。

**预期结果**：6 次里大约 1 次 `保留空think=True`，0–2 次插入 system（受 `add_system_ratio=0.2` 控制）。具体组合「待本地验证」。

#### 4.4.5 小练习与答案

- **练习**：为什么含 `tools` 的样本不插入随机 system？
- **答案**：Tool Use 数据的 system 角色承载着工具函数签名说明（`# Tools ...`），是工具调用协议的一部分；若被随机 system 覆盖或前置，会破坏 `<tool_call>`/`<tool_response>` 的模板契约，导致模型学不到正确的工具调用格式。

---

### 4.5 DPODataset、RLAIFDataset、AgentRLDataset：偏好对、留空回答与多轮轨迹

> 这三个类服务于强化学习相关阶段。它们共享「越往后越懒」的直觉：DPO 还肯帮你分词并标 mask；RLAIF 只给题目不给答案；Agent 干脆连分词都留给训练脚本。

#### 4.5.1 概念说明

- **DPODataset（直接偏好优化）**：每条样本是一对完整对话——一个好回答（`chosen`）和一个差回答（`rejected`），共享相同的 user 提问。DPO 要让模型给 `chosen` 的概率高于 `rejected`。所以它对 chosen/rejected **分别**渲染、分词、标 loss_mask，然后做位移（`[:-1]` / `[1:]`）返回。
- **RLAIFDataset（强化学习采样数据）**：样本还是多轮对话，但**故意丢掉最后一轮 assistant 回答**（`conversations[:-1]`），用 `add_generation_prompt=True` 渲染成「问到一半」的 prompt，`answer` 字段留空字符串。回答由 rollout 引擎在训练时实时生成、打分。
- **AgentRLDataset（多轮工具 RL）**：最「懒」。直接把 `.jsonl` 按行 `json.loads`，**完全不分词**，原样返回「去掉最后 assistant 轮的 messages + tools + gt」。`gt`（ground truth）是最终答案校验目标，用于 RLVR 式的可验证奖励。

#### 4.5.2 核心流程

三者对照：

```
DPO:   chosen / rejected  → 各自 apply_chat_template → 分词+pad → generate_loss_mask → 位移 → dict
RLAIF: conversations[:-1] → apply_chat_template(add_generation_prompt=True, open_thinking=随机) → {"prompt":..., "answer":""}
Agent: conversations      → 去掉最后 assistant 轮 → {"messages":..., "tools":..., "gt":...}   (不分词!)
```

DPO 的 `generate_loss_mask` 与 SFT 的 `generate_labels` 几乎是同一套「bos_id/eos_id 子串匹配」逻辑，区别只是它产出 `0/1` 掩码列表，而 `generate_labels` 产出「拷贝 token 或 -100」的 labels。

RLAIF 的 `thinking_ratio=0.5` 控制 `open_thinking` 开关：一半样本开思考、一半不开，让 policy 在两种模式下都被训练到（呼应 u1/u2-l1 的「思考靠开关控制」）。

#### 4.5.3 源码精读

**DPO** —— [dataset/lm_dataset.py:135-174](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L135-L174) 为 `__getitem__`：

- 第 137–138 行：取出 chosen / rejected 两个对话列表。
- 第 139–153 行：分别渲染 + `post_processing_chat` + 分词（`padding='max_length'` 直接定长）。
- 第 160–165 行：位移成 `x = ids[:-1]`、`y = ids[1:]`、`mask = loss_mask[1:]`，这正是 next-token 训练的标准错位。
- 配套的 `generate_loss_mask`：[dataset/lm_dataset.py:176-192](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L176-L192)，与 `generate_labels` 同构。

**RLAIF** —— [dataset/lm_dataset.py:208-216](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L208-L216) 为 `create_chat_prompt`：

- 第 209 行：先调 `pre_processing_chat` 增强。
- 第 210 行：`use_thinking = random.random() < 0.5` 随机决定开不开思考。
- 第 212 行：**关键**——传 `conversations[:-1]`（丢最后一轮），且 `add_generation_prompt=True`，渲染出以 `<|im_start|>assistant\n` 结尾、等待续写的 prompt。
- 第 217–224 行 `__getitem__`：返回 `{"prompt":..., "answer":""}`，answer 恒为空。

**Agent** —— [dataset/lm_dataset.py:226-252](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L226-L252)：

- 第 231–234 行：手动 `open` + `json.loads` 逐行读，不走 `load_dataset`。
- 第 239–247 行 `parse_conversations`：抽出 tools，并 `messages[:-1]` 去掉最后 assistant 轮。
- 第 249–252 行 `__getitem__`：返回 `{"messages", "tools", "gt"}`，**全程不碰 tokenizer**。

#### 4.5.4 代码实践

**目标**：对比 RLAIF 与 Agent 的返回，直观感受「留空 answer / 不分词」。

**操作步骤**（**示例代码**）：

```python
# practice_rl_agent.py
import json, os
from transformers import AutoTokenizer
from dataset.lm_dataset import RLAIFDataset, AgentRLDataset

tok = AutoTokenizer.from_pretrained('./model', trust_remote_code=True)
os.makedirs('./dataset', exist_ok=True)

# RLAIF 样本（最后留一个 assistant 轮，会被 [: -1] 丢掉）
rlaif = {"conversations": [
    {"role": "user", "content": "写一句关于春天的诗"},
    {"role": "assistant", "content": "（占位，会被丢弃）"},
]}
p1 = './dataset/_mini_rlaif.jsonl'
with open(p1, 'w', encoding='utf-8') as f:
    f.write(json.dumps(rlaif, ensure_ascii=False) + '\n')
ds_r = RLAIFDataset(p1, tok)
print("RLAIF 返回键:", list(ds_r[0].keys()))
print("RLAIF prompt 末尾是否以 assistant\\n 结尾:", ds_r[0]['prompt'].rstrip().endswith('assistant'))
print("RLAIF answer:", repr(ds_r[0]['answer']))

# Agent 样本（带 gt）
agent = {"conversations": [
    {"role": "user", "content": "计算 12*7"},
    {"role": "assistant", "content": "（占位）"},
], "gt": "84"}
p2 = './dataset/_mini_agent.jsonl'
with open(p2, 'w', encoding='utf-8') as f:
    f.write(json.dumps(agent, ensure_ascii=False) + '\n')
ds_a = AgentRLDataset(p2, tok)
print("Agent 返回:", {k: type(v).__name__ for k, v in ds_a[0].items()})
print("Agent gt:", ds_a[0]['gt'], "  messages 末尾角色:", ds_a[0]['messages'][-1]['role'])
```

**需要观察的现象**：

1. RLAIF 返回 `{'prompt', 'answer'}`，`answer` 是空字符串；prompt 文本以 `<|im_start|>assistant\n`（+ 可能的 `<think>` 起始）结尾，留好了续写位置。
2. Agent 返回 `{'messages', 'tools', 'gt'}`，`gt='84'`；`messages` 末尾角色是 `user`（最后的占位 assistant 已被去掉）。

**预期结果**：确认 RLAIF「留空 answer」、Agent「不分词只给题目+gt」。具体 prompt 文本受 `thinking_ratio` 随机影响，「待本地验证」。

#### 4.5.5 小练习与答案

- **练习 1**：DPO 的 `generate_loss_mask` 和 SFT 的 `generate_labels` 有什么同、什么异？
- **答案**：同——都用 `bos_id`/`eos_id` 子串匹配定位 assistant 段。异——前者输出 `0/1` 掩码列表（配合外部已位移的 x/y 用），后者直接输出「拷贝 token / -100」的 labels。
- **练习 2**：RLAIFDataset 为什么把 `answer` 留成空字符串？
- **答案**：RLAIF 的回答要由 policy 模型在训练时在线 rollout 生成，再由奖励模型/函数打分；数据集只负责给「题目」（prompt），所以 answer 留空。
- **练习 3**：AgentRLDataset 为什么连分词都不做？
- **答案**：Agentic RL 是多轮 Tool-Use rollout：每一步生成都要执行工具、把 `<tool_response>` 拼回上下文再续写，分词时机和上下文都是动态的，必须由训练脚本（`train_agent.py` 的 rollout 引擎）在现场处理，无法在 Dataset 阶段静态分词。

---

## 5. 综合实践

把本讲所有知识点串起来：自己造一条**带 Tool Call 的多轮对话**样本，分别用 `SFTDataset` 跑通，并解释 `generate_labels` 是如何同时定位「普通回答段」和「工具调用回答段」的。

**任务**：

1. 构造一条形如 README [SFT 格式样例](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L448-L456) 的样本（system 带 tools、user 提问、assistant 发 `tool_calls`、tool 返回结果、assistant 再总结），写成 `_mini_tool_sft.jsonl`。
2. 用 `SFTDataset` 加载，打印 `input_ids` 与 `labels` 的对齐表（复用 4.3.4 的打印循环）。
3. 回答两个问题：
   - `tool` 角色的 `<tool_response>` 段，label 是 -100 还是真实 token？为什么？
   - 两个 assistant 段（一个发 tool_call、一个给最终答案）是否都被标进了 loss？

**提示**：

- `tool` 角色在 chat_template 里被渲染进 `<|im_start|>user ... <tool_response> ... <|im_end|>`（见 u2-l1 模板），它**不含** `<|im_start|>assistant\n` 这个 `bos_id` 锚点，所以它的 label 应为 -100——工具结果不是模型要「生成」的，而是环境给的。
- 两个 assistant 段都各自以 `<|im_start|>assistant\n` 开头、`<|im_end|>\n` 结尾，所以都会被 `generate_labels` 命中并标进 loss。

**预期结果**：对齐表里能看到「user 提问→-100」「tool_response→-100」「assistant 工具调用段→真实 token」「assistant 总结段→真实 token」的清晰分区。这恰好印证了 SFT 的对齐哲学：**只学「模型该说的」，不学「环境和用户给的」**。具体渲染「待本地验证」。

---

## 6. 本讲小结

- MiniMind 的 5 个 Dataset 类职责单一：把 `.jsonl` 变成训练循环要的张量/字典；越靠后的训练阶段越「懒」。
- `PretrainDataset` 对整段文本算 loss，只在 pad 位置置 `-100`；目标是通用词语接龙。
- `SFTDataset.generate_labels` 用 `bos_id='<|im_start|>assistant\n'` 和 `eos_id='<|im_end|>\n'` 两个固定片段做子串匹配，精确夹出 assistant 回答段（含 `<think>` 与结尾 eos），其余置 -100。
- `pre_processing_chat` 概率插 system、`post_processing_chat` 概率删空思考块，两者共同实现「思考能力靠模板+数据随机化控制、而非独立训练阶段」。
- `DPODataset` 对 chosen/rejected 分别标 loss_mask 并位移；`RLAIFDataset` 丢掉最后一轮、留空 answer 交给 rollout；`AgentRLDataset` 完全不分词，只返回 messages+tools+gt。
- 贯穿全讲的底层手法只有一个：用 `-100`（或 0/1 mask）回答「这条序列里哪些位置参与 loss」。

---

## 7. 下一步学习建议

本讲把「输入张量怎么来」讲完了，接下来：

- 想看这些 `(input_ids, labels)` 进入模型后如何变成 loss，请进入 **u3 单元（模型结构）**，尤其是 u3-l5（CausalLM 前向与交叉熵损失）——那里会解释 `labels` 的位移交叉熵和 `ignore_index=-100` 在模型侧如何实现。
- 想看预训练/SFT 怎么真正跑起来，跳到 **u5-l1（Pretrain）** 和 **u5-l2（Full SFT）**，它们直接消费本讲的 `PretrainDataset` / `SFTDataset`。
- 对强化学习数据如何被 rollout 消费感兴趣，可在学完 u3 后直接读 **u7-l2（Rollout 引擎）**，看 RLAIF/Agent 的 prompt 如何被生成、打分、回填成训练样本。
- 建议先做一遍「综合实践」，确认你真的能读懂 `generate_labels` 的对齐表，再往后读模型源码会顺畅很多。
