# 全参数监督微调（Full SFT）与多轮对话

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚 **预训练（Pretrain）** 和 **监督微调（SFT）** 在「训练目标」上的本质区别——为什么预训练出来的模型「会说话但不会聊天」。
2. 读懂 `dataset/lm_dataset.py` 中的 `SFTDataset`，理解它是如何用两个锚点（`bos_id` / `eos_id`）在一条长 token 流里「夹」出 assistant 回答段，并用 `-100` 掩码让模型**只对回答算 loss**。
3. 读懂 `create_chat_prompt` 如何把多轮对话（含 system / tool_calls / tool 返回）渲染成统一的 chat_template 文本，以及 `pre/post_processing_chat` 如何概率性地混入 system prompt 与「空思考」标签。
4. 读懂 `trainer/train_full_sft.py` 的 `train_epoch` 与 `__main__`，理解它如何从 `pretrain` 权重接力，用更小的学习率完成对话对齐，最终产出 `full_sft_{dim}.pth`。

## 2. 前置知识

在进入本讲前，你需要先建立以下认知（它们来自前置讲义，本讲不再重复细节）：

- **位移交叉熵与 `-100` 掩码**（u3-l5、u2-l2）：模型的损失是 `logits[:-1]` 预测 `labels[1:]`，`ignore_index=-100` 的位置不参与 loss。这是「只学回答不学提问」的实现根基。
- **chat_template 与特殊标记**（u2-l1）：`<|im_start|>` / `<|im_end|>` 是结构边界符；assistant 段在模板里固定渲染成 `<|im_start|>assistant\n...<|im_end|>\n`；`<think>` / `<tool_call>` 是 `special=False` 的标记，需要模型学会生成、参与 loss。
- **预训练的产物**（u5-l1）：`pretrain_{dim}.pth` 只学了「词语接龙」，输入是 `bos_token + prompt` 的纯文本续写，它**不认识**多轮对话模板。
- **训练循环模板**（u4-l3、u5-l1）：`init_distributed_mode` 自动判别单卡/多卡、`autocast` + `GradScaler` 混合精度、梯度累积、更新顺序 `unscale_ → clip → step → update → zero_grad`。本讲的 `train_epoch` 与预训练几乎逐行一致，差异只在「数据」和「超参」上。
- **init_model 的权重命名**（u4-l1）：`init_model` 按 `{from_weight}_{hidden_size}{_moe?}.pth` 加载权重，`strict=False` 容错。

> 一句话铺垫：**SFT 不是让模型学新知识为主，而是让模型学会「按对话模板回答」这件事**。MiniMind 主线的 14GB SFT 数据体量较大，所以也带有一定「持续预训练 / mid training」的味道，但它的训练机制仍是 SFT。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| `trainer/train_full_sft.py` | Full SFT 训练脚本（173 行） | `train_epoch` 训练循环、`__main__` 9 步装配、与预训练的超参差异 |
| `dataset/lm_dataset.py` | 全部 Dataset 类 | `SFTDataset`（含 `create_chat_prompt` / `generate_labels`）、`pre/post_processing_chat` |
| `model/tokenizer_config.json` | 分词器配置 | `chat_template` 字段（L333），决定多轮对话如何渲染成 token |
| `eval_llm.py` | CLI 推理入口（94 行） | 用 `--weight full_sft` 对话，对比 pretrain 与 full_sft 的回答质量 |

## 4. 核心概念与源码讲解

本讲按「数据流」顺序拆成三个最小模块：

- **4.1 `SFTDataset`**：把一条多轮对话样本变成 `(input_ids, labels)`，核心是 `generate_labels` 的 `-100` 掩码。
- **4.2 `create_chat_prompt`**：把 `conversations` 渲染成 chat_template 文本（含工具调用、思考标签）。
- **4.3 `train_epoch` 与 `__main__`**：SFT 的训练循环与主流程，从 `pretrain` 权重接力。

---

### 4.1 SFTDataset：只对 assistant 回答段算 loss

#### 4.1.1 概念说明

预训练阶段（u5-l1）的 `PretrainDataset` 把整段纯文本都拿来算 loss——因为目标是「词语接龙」，每个位置都是学习目标。但 SFT 的目标变了：**模型应当学习「在给定提问时如何作答」，而不是学习「如何复述用户的提问」**。

于是 SFT 引入了「回答掩码」：

- 用户提问、system 指令、padding 这些位置 → 标 `-100`（不参与 loss）。
- assistant 的回答（含 `<think>` 思考块、正文、`<tool_call>` 工具调用、结尾 `<|im_end|>`）→ 标真实 token id（参与 loss）。

`SFTDataset` 就是干这件事的：它先用 chat_template 把整轮对话渲染成一条 token 流，再用两个「锚点」在流里定位每个 assistant 段，把段内的 token「点亮」成学习目标。

#### 4.1.2 核心流程

`SFTDataset.__getitem__` 的数据流（伪代码）：

```
sample = {'conversations': [system?, user, assistant, user, assistant, ...]}
   │
   1. pre_processing_chat   ── 概率插入 system prompt（工具数据除外）
   2. create_chat_prompt    ── apply_chat_template 渲染成文本（含 tool_calls/think）
   3. post_processing_chat  ── 概率移除空 <think></think> 块（80% 移除）
   4. tokenizer(prompt)     ── 文本 → input_ids，截断到 max_length
   5. 补 pad 到 max_length
   6. generate_labels       ── 用 bos_id/eos_id 锚点夹出 assistant 段，段内置真值、其余 -100
   ▼
返回 (input_ids, labels)，两个等长的 LongTensor
```

`generate_labels` 的锚点匹配逻辑（关键）：

```
锚点 bos_id = "<|im_start|>assistant\n" 的 token 序列
锚点 eos_id = "<|im_end|>\n"            的 token 序列

遍历 input_ids：
  若发现 bos_id 子串 → 记 start = 当前位置 + len(bos_id)
                      向后找 eos_id 子串 → 记 end
                      把 labels[start .. end+len(eos_id)) 置为真值
                      跳过这段继续
  否则 → 前进一位
其余位置保持 -100
```

注意配合 forward 的位移（`logits[:-1]` 预测 `labels[1:]`，见 [model/model_minimind.py:251-252](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L251-L252)）：`labels[start]` 是 assistant 段的第一个 token（即 `<think>` 或正文首字），用来监督它的输入正是 `start-1` 位置——也就是 `<|im_start|>assistant\n` 末尾的 `\n`。这保证了模型「看到 assistant 开头标记后，就开始预测回答」。

#### 4.1.3 源码精读

**① 锚点定义**：在构造时把两个边界符分词成 token 序列，作为后续子串匹配的「图样」。

[dataset/lm_dataset.py:65-66](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L65-L66) — 用 `bos_token + 'assistant\n'` 与 `eos_token + '\n'` 分别分词，得到 `bos_id` / `eos_id` 两个 token 列表。

注意 `bos_token` 是 `<|im_start|>`、`eos_token` 是 `<|im_end|>`（见 [model/tokenizer_config.json:317-319](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/tokenizer_config.json#L317-L319)）。之所以锚点是 `<|im_start|>assistant\n` 而不仅仅是 `<|im_start|>`，是因为 `<|im_start|>` 后面可能跟 `system` / `user` / `assistant` 三种角色，必须连角色名一起匹配，才能精确锁定 assistant 段的起点。

**② Features 显式 schema**：

[dataset/lm_dataset.py:63-64](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L63-L64) — 用 `datasets.Features` 显式声明每条消息含 `role/content/reasoning_content/tools/tool_calls` 五个字段。这是因为同一份 `sft_t2t` 数据里既有普通对话（只有 role/content），也有工具调用样本（带 tool_calls）和思考样本（带 reasoning_content），显式 schema 避免 `load_dataset` 因字段缺失或类型不一致报错。

**③ `generate_labels` 掩码构造**：

[dataset/lm_dataset.py:88-104](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L88-L104) — 先把 labels 全初始化为 `-100`，再用 `while` 循环做子串匹配：命中 `bos_id` 就向右找最近的 `eos_id`，把 `[start, end+len(eos_id))` 区间回填真值。这段区间**包含 `<|im_end|>` 及其后的 `\n`**，意味着模型必须学会「生成到合适位置就主动吐 `<|im_end|>` 结束」——这是生成阶段能够自然停止的关键。

**④ `__getitem__` 总装**：

[dataset/lm_dataset.py:106-119](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L106-L119) — 串起 `pre_processing_chat → create_chat_prompt → post_processing_chat → 分词 → pad → generate_labels`。注意 L111 调用 `tokenizer(prompt)` 时**没有**传 `add_special_tokens=False`，但 `tokenizer_config.json` 里 `add_bos_token=false` / `add_eos_token=false`（[L2-L3](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/tokenizer_config.json#L2-L3)），所以不会额外插入 bos/eos；模板里已有的 `<|im_start|>` / `<|im_end|>` 作为字面量被分词器识别成对应特殊 token id。

L114-117 留有一段被注释的调试打印：把每个位置的 `X → Y → label` 逐 token 对齐打印，这正是本讲「代码实践」要复刻的东西。

#### 4.1.4 代码实践（源码阅读型，无需 GPU/权重）

**目标**：用一条手工构造的多轮对话，肉眼验证「只有 assistant 段参与 loss」。

**操作步骤**（在项目根目录执行）：

```python
# 文件名：inspect_sft_labels.py（示例代码，非项目原有文件）
import sys, torch
from transformers import AutoTokenizer
sys.path.append('.')
from dataset.lm_dataset import SFTDataset

tok = AutoTokenizer.from_pretrained('./model')

# 手工造一个 2 轮对话，不依赖真实数据文件
class FakeDS(SFTDataset):
    def __init__(self, tok):
        self.tokenizer = tok
        self.max_length = 128
        self.bos_id = tok(f'{tok.bos_token}assistant\n', add_special_tokens=False).input_ids
        self.eos_id = tok(f'{tok.eos_token}\n', add_special_tokens=False).input_ids

ds = FakeDS(tok)
conversations = [
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "你好！我是 minimind。"},
    {"role": "user", "content": "再见"},
    {"role": "assistant", "content": "再见！"},
]
prompt = ds.create_chat_prompt(conversations)
input_ids = tok(prompt).input_ids[:ds.max_length]
input_ids += [tok.pad_token_id] * (ds.max_length - len(input_ids))
labels = ds.generate_labels(input_ids)

for i, (x, y) in enumerate(zip(input_ids[:-1], labels[1:])):
    if y != -100:
        print(f"{i:3d}: X={tok.decode([x])!r:20s} -> Y={tok.decode([input_ids[i+1]])!r:20s}")
```

**需要观察的现象**：

1. 打印出的 `X → Y` 行，`X` 应当落在 `<|im_start|>assistant\n` 之后（即每个 assistant 段内部）。
2. 用户提问段（`<|im_start|>user\n...`）、system 段、padding 都不会出现——它们被 `-100` 屏蔽。
3. 每个 assistant 段的最后一行 `Y` 应当是 `<|im_end|>`，说明模型在学「主动结束」。

**预期结果**：每个 assistant 回答（含 `<think>\n...\n</think>\n\n` 包裹与结尾 `<|im_end|>`）都被点亮，其余位置被跳过。若你看到 user 段也被打印，说明锚点匹配有误，需重新检查。

> 说明：本实践只读源码、不训练，可在纯 CPU 环境运行；`./model` 目录下需有分词器文件（仓库自带）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `bos_id` 改成只用 `<|im_start|>`（去掉 `assistant\n`），`generate_labels` 会出什么问题？

**答案**：`<|im_start|>` 同时出现在 `<|im_start|>system`、`<|im_start|>user`、`<|im_start|>assistant` 三种段开头，匹配会误命中 system/user 段，把提问和系统指令也点亮进 loss，违背「只学回答」的初衷。所以锚点必须连角色名一起匹配。

**练习 2**：`generate_labels` 把 `<|im_end|>\n`（eos_id）也纳入了 labels 区间，为什么？

**答案**：让模型学会在回答结束时主动生成 `<|im_end|>`（及紧随的换行）。推理时 `generate` 检测到 `<|im_end|>` 就会停止解码；若不在训练中监督这个 token，模型可能永远不主动收尾，出现无限生成。

**练习 3**：一个 batch 内不同样本长度不同，SFTDataset 如何对齐？

**答案**：L111-112 把每条样本截断到 `max_length` 后，用 `pad_token_id` 补齐到等长；pad 位置不在任何 assistant 段内，`generate_labels` 自然保持 `-100`，不参与 loss（也不影响 attention，因为训练时整段参与、padding 在末尾）。

---

### 4.2 create_chat_prompt：渲染多轮对话与工具调用模板

#### 4.2.1 概念说明

`create_chat_prompt` 是 `SFTDataset` 的一个方法（[dataset/lm_dataset.py:71-86](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L71-L86)），它做两件事：

1. **数据清洗**：把数据里以「字符串」形式存的 `tools` / `tool_calls` 反序列化回 Python 对象（jsonl 里它们常被存成 JSON 字符串）。
2. **模板渲染**：调用 `tokenizer.apply_chat_template(...)` 把 `messages` 列表渲染成一段完整文本。

为什么需要它？因为 `sft_t2t` 数据是「混编」的：同一份文件里既有普通问答，也有带 `tools` 的工具调用样本、带 `reasoning_content` 的思考样本。`chat_template`（一段 Jinja2，见 [model/tokenizer_config.json:333](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/tokenizer_config.json#L333)）能统一处理这三种情况——这就是 README 反复强调的「统一模板、减少预处理分叉」。

> 关键区别（对照 u1-l3）：训练时用 `add_generation_prompt=False`，因为 assistant 的回答**已经在 messages 里**，要整段渲染出来当监督信号；推理时用 `add_generation_prompt=True`，只渲染到 `<|im_start|>assistant\n`，把后续留给模型生成。

#### 4.2.2 核心流程

`create_chat_prompt` 的处理步骤：

```
for message in conversations:
    若 message.role == 'system' 且带 tools → 把 tools 字符串 json.loads 成列表
    若 message.tool_calls 是字符串            → json.loads 成列表
    收集进 messages
return apply_chat_template(messages, tokenize=False,
                           add_generation_prompt=False, tools=tools)
```

模板对四种角色的渲染规则（摘自 chat_template 逻辑）：

| 角色 | 渲染结果（简版） |
|------|-----------------|
| system（无 tools） | `<\|im_start\|>system\n{content}<\|im_end\|>\n` |
| system（带 tools） | `<\|im_start\|>system\n{content}\n\n# Tools\n...\n<tool_call>...</tool_call><\|im_end\|>\n` |
| user | `<\|im_start\|>user\n{content}<\|im_end\|>\n` |
| assistant | `<\|im_start\|>assistant\n<think>\n{reasoning}\n</think>\n\n{content}\n<tool_call>...</tool_call><\|im_end\|>\n` |
| tool（工具返回） | 并入下一个 `<\|im_start\|>user\n<tool_response>\n{content}\n</tool_response><\|im_end\|>\n` |

注意 assistant 段**永远**被 `<think>\n...\n</think>\n\n` 包裹：若样本没有 `reasoning_content`，模板里 reasoning 为空，得到 `<think>\n\n</think>\n\n`（即「空思考块」）。`post_processing_chat` 就是为处理这种情况而生的。

#### 4.2.3 源码精读

**① tools / tool_calls 反序列化**：

[dataset/lm_dataset.py:74-80](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L74-L80) — `system.tools` 和 `assistant.tool_calls` 在 jsonl 里可能是字符串（如 `"[{\"name\":...}]"`）也可能是对象，这里用 `isinstance(..., str)` 判别后 `json.loads`，兼容两种存法。

**② apply_chat_template 调用**：

[dataset/lm_dataset.py:81-86](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L81-L86) — `tokenize=False` 返回字符串而非 id（后续统一在 `__getitem__` 里分词）；`add_generation_prompt=False` 渲染完整对话；`tools=tools` 把工具定义传给模板，触发 system 段的工具说明渲染分支。

**③ pre/post_processing_chat 的随机化混入**：

[dataset/lm_dataset.py:9-35](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L9-L35) — 这是 SFT 数据「多样性」的关键：
- `pre_processing_chat`（L9-29）：以 `add_system_ratio=0.2` 的概率给没有 system 的对话随机插入一条 system prompt（10 条候选里抽，中英各半）；**但带 tools 的工具数据原样返回不做处理**（L11），避免破坏工具声明。
- `post_processing_chat`（L31-35）：模板会给没有 reasoning 的 assistant 段插入空 `<think>\n\n</think>\n\n`；这里以 `1-0.2=0.8` 的概率把空块**移除**，剩下 0.2 概率保留。

这两步随机化的设计意图：让同一模型在推理时既能应对「带思考」也能应对「不带思考」的输入——这正是 u1-l1 提到的「思考能力由模板+数据随机化控制，而非独立训练阶段」的落地。

#### 4.2.4 代码实践

**目标**：肉眼看到一条带工具调用的多轮对话被渲染成什么样。

**操作步骤**（接 4.1.4 的脚本环境）：

```python
conv_with_tool = [
    {"role": "system", "content": "你可以调用翻译工具。",
     "tools": '[{"type":"function","function":{"name":"translate_text",'
               '"parameters":{"type":"object","properties":{"text":{"type":"string"}}}}}]'},
    {"role": "user", "content": "把'你好世界'翻译成english"},
    {"role": "assistant", "content": "",
     "tool_calls": '[{"name":"translate_text","arguments":{"text":"你好世界"}}]'},
    {"role": "tool", "content": '{"translated_text":"Hello World"}'},
    {"role": "assistant", "content": "Hello World"},
]
print(ds.create_chat_prompt(conv_with_tool))
```

**需要观察的现象**：

1. system 段应包含 `# Tools` 与 `<tools>...</tools>` 说明，末尾有 `<tool_call>` 调用格式示范。
2. 第一个 assistant 段应为空 content + 一个 `<tool_call>{"name":"translate_text",...}</tool_call>`。
3. tool 角色被渲染进 `<|im_start|>user\n<tool_response>\n...\n</tool_response><|im_end|>\n`。
4. 最后一个 assistant 段被 `<think>\n\n</think>\n\n` 包裹（空思考），正文是 `Hello World`。

**预期结果**：渲染出的文本与 README 第 Ⅲ 节 SFT 数据示例（[README.md:448-455](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L448-L455)）结构一致。可在此打印基础上，再调用 `ds.generate_labels` 验证只有两个 assistant 段（tool_call 段 + 最终回答段）被点亮。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `pre_processing_chat` 对「带 tools 的数据」直接 return 不做处理？

**答案**：工具调用样本的 system 段已经携带了 `# Tools` 声明，它不是普通的「你是 xx 助手」身份描述。若再随机叠加一条通用 system prompt，会破坏模板里 tools 渲染分支的结构（system 段会被重复或错位），导致 `generate_labels` 锚点匹配失败。所以工具数据必须原样保留。

**练习 2**：`post_processing_chat` 把空 `<think>` 块以 80% 概率移除，剩下 20% 保留，目的是什么？

**答案**：让模型在训练时同时见过「有思考块」和「无思考块」两种 assistant 输出，避免模型对模板里固定出现的空 `<think>\n\n</think>\n\n` 产生硬依赖。这样推理时无论 `open_thinking` 开或关（u2-l1），模型都能正确处理，思考行为可由开关动态控制而非写死在权重里。

---

### 4.3 train_epoch 与 __main__：从 pretrain 接力的训练主流程

#### 4.3.1 概念说明

`train_full_sft.py` 的训练循环和预训练（u5-l1）**几乎逐行相同**——同样的余弦学习率、同样的混合精度、同样的梯度累积、同样的更新顺序。差异只有三处：

1. **数据**：`PretrainDataset` → `SFTDataset`（带回答掩码）。
2. **起点**：默认 `--from_weight pretrain`，从预训练权重接力，而不是从随机初始化开始。
3. **超参**：`--learning_rate 1e-5`（预训练通常是更大的 lr）、`--epochs 2`、`--max_seq_len 768`。

这三处差异恰恰体现了 SFT 的工程哲学：**用更小的学习率、在已经具备语言能力的预训练权重上做「轻量调整」，把模型「对齐」到对话格式**。学习率过大会把预训练辛苦学到的语言知识「灾难性遗忘」掉。

输出权重命名为 `full_sft_{hidden_size}{_moe?}.pth`（如 `full_sft_768.pth`），其中 `full` 表示**全参数**微调（区别于后续 u6 的 LoRA 部分参数微调）。

#### 4.3.2 核心流程

`__main__` 是清晰的 9 步装配（与预训练脚本同构）：

```
1. init_distributed_mode + setup_seed     ── 分布式与随机种子
2. 建 save_dir、MiniMindConfig、查 ckp    ── 配置与断点检查
3. 设混合精度 autocast_ctx                ── fp16/bf16
4. 配 wandb（swanlab）                     ── 训练记录
5. init_model(from_weight) + SFTDataset + AdamW ── 模型/数据/优化器
6. 从 ckp 恢复 epoch/step                  ── 断点续训
7. torch.compile + DDP 包装                ── 加速与分布式
8. for epoch: SkipBatchSampler + train_epoch ── 训练循环
9. dist.barrier + destroy_process_group    ── 清理
```

`train_epoch` 单个 step 的处理：

```
取 (input_ids, labels) → 算 lr 并写回 optimizer
前向：res = model(input_ids, labels=labels)
      loss = res.loss + res.aux_loss        ── 主损失 + MoE 负载均衡损失
      loss = loss / accumulation_steps
反向：scaler.scale(loss).backward()
每 N 步：unscale_ → clip_grad_norm_ → step → update → zero_grad
日志：打印 loss / logits_loss / aux_loss / lr
存档：每 save_interval 存 full_sft_{dim}.pth + _resume.pth
```

#### 4.3.3 源码精读

**① train_epoch 的核心 step（与预训练同构）**：

[trainer/train_full_sft.py:35-49](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_full_sft.py#L35-L49) — 前向得 `res.loss + res.aux_loss`（Dense 模型 `aux_loss` 恒为 0），除以累积步数后反向，每 `accumulation_steps` 步执行 `unscale_ → clip → step → update → zero_grad`。这段与 `train_pretrain.py` 的 `train_epoch` 完全一致，是全项目的训练循环模板（u4-l3、u5-l1）。

**② 关键超参差异**：

[trainer/train_full_sft.py:90](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_full_sft.py#L90) — `--learning_rate` 默认 `1e-5`，比预训练小一个量级以上，避免破坏预训练知识。

[trainer/train_full_sft.py:102-103](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_full_sft.py#L102-L103) — `--data_path` 默认 `../dataset/sft_t2t_mini.jsonl`；`--from_weight` 默认 `pretrain`，即从 `pretrain_{dim}.pth` 接力。

[trainer/train_full_sft.py:100](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_full_sft.py#L100) — `--max_seq_len` 默认 768，匹配 README 推荐的 `sft_t2t_mini` 序列长度（[README.md:514](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L514)）。

**③ 数据与模型装配**：

[trainer/train_full_sft.py:135-139](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_full_sft.py#L135-L139) — `init_model` 加载 `pretrain` 权重并返回模型与分词器；`SFTDataset` 接上 `sft_t2t_mini.jsonl`；优化器是普通 `AdamW`（全参数都进优化器，区别于 LoRA 的「只把含 lora 的参数进优化器」）。

**④ 权重保存命名**：

[trainer/train_full_sft.py:61-72](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_full_sft.py#L61-L72) — 每 `save_interval` 步存两份：干净的推理权重 `{save_weight}_{hidden_size}{_moe?}.pth`（默认 `full_sft_768.pth`，fp16）+ 续训检查点（`lm_checkpoint`，u4-l2）。`raw_model` 的剥壳（`.module` / `._orig_mod`）是为了兼容 DDP 与 `torch.compile` 包装。

**⑤ 日志解读**：

[trainer/train_full_sft.py:53-58](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_full_sft.py#L53-L58) — 打印 `loss = logits_loss + aux_loss`。Dense 模型 `aux_loss` 恒 0，所以 `logits_loss` 就是纯交叉熵，是 SFT 阶段要盯的主指标。SFT 的 loss 通常远低于预训练（因为只对 assistant 段算，且模型已具备语言能力），下降曲线见 README 的 `sft_loss.jpg`（[README.md:717-718](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L717-L718)）。

#### 4.3.4 代码实践

**目标**：理解 `train_full_sft.py` 的启动方式与日志含义（无需真的跑完训练）。

**操作步骤**：

1. 阅读 [trainer/train_full_sft.py:84-108](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_full_sft.py#L84-L108) 的全部 CLI 参数，对比预训练脚本 `train_pretrain.py` 的参数，列出三处以上差异（提示：`learning_rate`、`from_weight`、`data_path`、`save_weight`）。
2. 阅读 [trainer/train_full_sft.py:158-168](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_full_sft.py#L158-L168) 的 epoch 循环，理解 `SkipBatchSampler` + `len(loader)+skip` 如何在断点续训时保持学习率曲线连续（详见 u4-l1、u4-l2）。

**需要观察的现象**：在源码中确认，`train_epoch` 的前向调用 `model(input_ids, labels=labels)` 返回的 `res.loss` 已经是「只对 assistant 段算的位移交叉熵」（因为 `-100` 掩码在 `SFTDataset` 端就注入了），训练脚本本身**不再做任何掩码**——掩码逻辑全部封装在数据侧。

**预期结果**：能口头复述「SFT 与预训练共用同一套 train_epoch 模板，差异全在数据集类与超参」。完整训练实践见第 5 节。

#### 4.3.5 小练习与答案

**练习 1**：为什么 SFT 的学习率（`1e-5`）要比预训练小得多？

**答案**：SFT 是在已经具备语言能力的预训练权重上做「微调对齐」，目的是调整输出分布使之符合对话模板，而非从头学习语言。学习率过大会把预训练学到的通用知识冲刷掉（灾难性遗忘），所以用小学习率做轻量调整。

**练习 2**：`full_sft` 里的 `full` 是相对什么而言？

**答案**：相对 LoRA（u6）的「部分参数微调」。`full_sft` 更新模型**全部参数**（`AdamW(model.parameters())` 把所有参数送进优化器），而 LoRA 只训练注入的低秩增量。所以命名上用 `full` 强调全参微调。

**练习 3**：训练脚本里看不到任何 `-100` 或 `loss_mask` 的处理代码，那「只学回答」是在哪里实现的？

**答案**：在数据侧的 `SFTDataset.generate_labels` 里。`labels` 张量在被喂给 `model(input_ids, labels=labels)` 之前，就已经把非 assistant 段置成 `-100`；`model_minimind.py` 的 `F.cross_entropy(..., ignore_index=-100)`（[model_minimind.py:252](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L252)）自动忽略这些位置。训练脚本因此可以和预训练脚本共用同一套模板，无需特判。

---

## 5. 综合实践

**任务**：跑通「pretrain → full_sft → 对话评测」的最小闭环，对比预训练权重与 SFT 权重的回答差异。

**前置准备**（本仓库不含数据与权重，需自行下载，见 u1-l2、README 第 Ⅱ/Ⅲ 节）：

- `./dataset/pretrain_t2t_mini.jsonl` 与 `./dataset/sft_t2t_mini.jsonl`（从 ModelScope 下载，放入 `./dataset/`）。
- 已按 u5-l1 跑出预训练权重 `./out/pretrain_768.pth`（若没有，可先跑 `train_pretrain.py`）。
- 单卡 GPU，已确认 `torch.cuda.is_available()` 为 True。

**操作步骤**：

1. **启动 SFT 训练**（须在 `trainer/` 目录下执行）：

   ```bash
   cd trainer
   # 方式1：单进程
   python train_full_sft.py
   # 方式2：torchrun（与单进程等价，nproc_per_node=1）
   torchrun --nproc_per_node 1 train_full_sft.py
   ```

   关键观察：日志里 `loss` / `logits_loss` 应从某个较低值（因为只对 assistant 段算）开始继续下降；确认 `from_weight` 默认为 `pretrain`、`init_model` 加载的是 `pretrain_768.pth`。

2. **得到权重**：训练结束（或每隔 `save_interval` 步）会在 `../out/` 产出 `full_sft_768.pth`。

3. **对话对比**（回到项目根目录）：

   ```bash
   # 先用预训练权重：纯文本续写模式
   python eval_llm.py --weight pretrain
   # 再用 SFT 权重：多轮对话模板模式
   python eval_llm.py --weight full_sft
   ```

   在 `eval_llm.py` 里（[eval_llm.py:73-76](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L73-L76)），`pretrain` 走 `bos_token + prompt` 纯续写分支，其余权重走 `apply_chat_template(..., add_generation_prompt=True)` 对话分支——这正好对应本讲的两种数据渲染方式。

**需要观察的现象与预期结果**：

- `pretrain` 权重：对 `解释什么是机器学习` 这类提问，它会**继续往下写**类似文本，但不会以「助手回答」的口吻组织，常常自言自语、不收尾或跑题（因为它只学了词语接龙，没见过对话模板）。
- `full_sft` 权重：应当以「助手」身份给出较连贯的回答，并在合适位置主动以 `<|im_end|>` 收尾，回答风格接近 README 示例（[README.md:723-735](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L723-L735)）。

> 若无 GPU 或数据，可退化为「源码阅读型实践」：完成 4.1.4 与 4.2.4 即可掌握本讲核心机制。完整训练在单卡 3090 上约 1.1 小时、约 1.43 元（[README.md:621-625](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L621-L625)）。具体耗时与成本「待本地验证」。

## 6. 本讲小结

- **SFT 的本质是「对齐」**：在预训练权重上用小学习率（`1e-5`）微调，让模型从「会说话」变成「会按对话模板回答」，而非从头学语言。
- **回答掩码在数据侧实现**：`SFTDataset.generate_labels` 用 `bos_id`（`<|im_start|>assistant\n`）与 `eos_id`（`<|im_end|>\n`）两个锚点夹出每个 assistant 段，段内置真值、其余置 `-100`，使模型只对回答算 loss。
- **掩码对齐位移交叉熵**：配合 forward 的 `logits[:-1]` 预测 `labels[1:]`，模型学会「看到 `<|im_start|>assistant\n` 后开始预测回答，并在结束时主动吐 `<|im_end|>`」。
- **create_chat_prompt 统一渲染混编数据**：一份 `sft_t2t` 里普通对话、工具调用、思考样本混在一起，靠 `chat_template` + `apply_chat_template(add_generation_prompt=False)` 统一处理。
- **思考能力由随机化注入**：`pre_processing_chat` 概率插 system prompt、`post_processing_chat` 概率移除空 `<think>` 块，使同一权重能动态适配 `open_thinking` 开关。
- **训练脚本与预训练同构**：`train_epoch` 逐行复用预训练模板，差异只在 `SFTDataset`、`from_weight=pretrain`、`lr=1e-5` 三处；输出 `full_sft_{dim}.pth`，`full` 强调全参数微调。

## 7. 下一步学习建议

- **横向对比**：回到 `train_pretrain.py`（u5-l1）与 `train_full_sft.py`，用 `diff` 工具逐行比对，你会直观看到「SFT 和预训练共用同一套训练底座」的事实，差异点就是本讲讲的那些。
- **进入参数高效微调**：下一讲 u6-l1（LoRA 原理与从 0 实现）将讲解如何只训练极少量低秩增量参数完成垂域适配，对比 `full_sft` 的全参数更新，理解「全参微调 vs LoRA」的工程取舍。
- **深入白盒蒸馏**：u6-l3（白盒知识蒸馏）会把 `full_sft` 权重当作**教师模型**，用一个更小的学生模型拟合它的输出分布——届时你会再次用到本讲的 `SFTDataset` 与交叉熵知识。
- **扩展阅读**：阅读 `dataset/lm_dataset.py` 中的 `DPODataset`（u7-l1 会用到），它的 `generate_loss_mask` 与本讲的 `generate_labels` 几乎同源，是 SFT 掩码思想在偏好优化里的延续。
