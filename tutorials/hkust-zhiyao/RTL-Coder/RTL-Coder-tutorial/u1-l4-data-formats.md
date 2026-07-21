# 数据格式详解（u1-l4）

> 阶段：入门层（beginner） · 依赖：u1-l2《仓库结构与依赖》
> 本讲只看「数据长什么样」，不进入生成脚本和训练算法内部。读懂本讲后，你拿到项目里任何一份 JSON 都能立刻判断它属于流水线的哪一环、该怎么读。

---

## 1. 本讲目标

学完本讲，你应当能够：

- 看懂 RTL-Coder 里三份关键 JSON 的字段结构与字段含义；
- 区分「普通指令格式」`{Instruction, Input, Response}` 与「评分训练格式」`{Instruction, Input, Response[], Score[]}`；
- 说出成品数据集 `Resyn27k.json` 相比生成样本**少了哪个字段**、以及 `Response` 为什么被存成列表；
- 写一小段 Python，逐行读取这三份 JSON 并打印字段名与 `Response` 列表长度，对比三种结构的差异。

本讲不要求你懂深度学习或 Verilog，只要会读 JSON 即可。

---

## 2. 前置知识

### 2.1 什么是 JSON 与「逐行 JSON（JSONL）」

JSON 是一种用花括号 `{}` 包起来的键值对文本格式，例如：

```json
{"名字": "半加器", "端口数": 3}
```

而 **JSONL（JSON Lines）** 是一种「**一行一个独立 JSON 对象**」的约定：文件里每一行都是一个完整的 JSON，行与行之间没有逗号，整体也不是一个 JSON 数组。这样做的好处是——**可以一行一行地流式读取**，不用把整个文件一次性加载进内存，对动辄几万条的训练数据非常友好。

> 🔑 关键事实：RTL-Coder 里的三份数据文件**全部采用逐行 JSON（JSONL）** 存储。你会在训练代码里反复看到 `[json.loads(l) for l in open(...)]` 这种「逐行解析」的写法，就是这个原因。

### 2.2 RTL-Coder 流水线里的三个数据位置

回顾 u1-l2 的目录导览，数据在项目里流动时会依次出现三种形态：

| 阶段 | 文件 | 角色 |
| --- | --- | --- |
| ① 数据生成（GPT 造数据） | `data_generation/data_sample.json` | GPT 产出的样本（指令 + 模块骨架 + 参考代码） |
| ② 成品训练集 | `dataset/Resyn27k.json` | 汇总清洗后、约 2.7 万条的**标准 SFT** 训练集 |
| ③ 评分训练数据 | `train/scoring_data_sample.json` | 同一条指令配**多个候选答案 + 质量分数**，给评分训练用 |

本讲就按 ① → ② → ③ 的顺序逐一拆解它们的字段。理解字段，是后续读懂生成脚本（u2 单元）和训练脚本（u2-l7、u3-l1）的前提。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `data_generation/data_sample.json` | GPT 数据生成流水线的产物样本 | `{Instruction, Input, Response}` 三字段，`Response` 为单元素列表 |
| `dataset/Resyn27k.json` | 标准监督微调（SFT）用的成品数据集 | 只有 `{Instruction, Response}`，**没有 Input**，`Response` 为列表 |
| `train/scoring_data_sample.json` | 评分训练（quality scoring）用的样本 | `{Instruction, Input, Response[], Score[]}`，多候选 + 分数 |
| `train/mle.py` | 标准 SFT 脚本（u2-l7 详讲） | 证明它如何消费 `Resyn27k.json`：取 `Response[-1]` |
| `train/mle_scoring.py` | 评分训练脚本（u3-l1 详讲） | 证明它如何消费多候选 + Score |

---

## 4. 核心概念与源码讲解

### 4.1 三份文件的共同骨架：逐行 JSON + 三个基本字段

#### 4.1.1 概念说明

尽管三份文件服务于不同阶段，它们共享同一套「**指令式（instruction-style）**」数据骨架。这是一种从 Self-Instruct 系列工作继承下来的范式：用一段自然语言「指令（Instruction）」描述任务，再给出「回答（Response）」。RTL-Coder 在此基础上为 Verilog 任务增加了一个可选字段「输入（Input）」——用来放模块的端口签名骨架。

三个基本字段：

- **Instruction**（指令）：一段自然语言，描述「请设计一个什么样的 Verilog 模块」。通常以 `Please act as a professional Verilog designer...` 开头。
- **Input**（输入，可选）：一段 Verilog 模块签名骨架，例如 `module foo (...); ... endmodule`，给模型一个填空的起点。
- **Response**（回答）：参考答案——一段完整的 Verilog 代码。**在本项目里统一存成「列表」**，即使只有一个答案也用单元素列表 `[ "...code..." ]` 包裹。

> 🔑 为什么 `Response` 要存成列表？因为同一条指令在评分训练阶段会有**多个候选答案**需要并列存放。为了让生成、训练、评分三套代码共用同一套读写逻辑，项目干脆把 `Response` 统一存成列表。这一点是理解三种格式差异的钥匙。

#### 4.1.2 核心流程

三种格式可以被一张「字段加法表」概括：

```
生成样本    = Instruction + Input      + Response[1]
成品训练集  = Instruction              + Response[1]        ← 注意：去掉了 Input
评分训练集  = Instruction + Input      + Response[N] + Score[N]
```

也即：从「生成样本」到「成品训练集」，项目**丢掉了 Input 字段**；从「成品训练集」到「评分训练集」，项目**重新加回 Input，并把 Response 从 1 个候选扩展到 N 个候选，再补一列 Score**。

#### 4.1.3 源码精读

三种格式都采用逐行 JSON。以成品训练集为例，标准 SFT 脚本 `mle.py` 的读取方式就是「逐行 `json.loads`」：

[mle.py:L120-L127](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py#L120-L127) —— 标准微调如何消费 JSONL 数据。

这段代码做了三件事：

1. `list_data_dict = [json.loads(l) for l in open(data_path, "r")]`：逐行把每一行解析成一个 Python 字典；
2. `sources = [example['Instruction'] + '\n' ...]`：取每条的 `Instruction` 作为模型输入；
3. `targets = [f"{example['Response'][-1]}{tokenizer.eos_token}" ...]`：取 `Response[-1]`（列表的最后一个元素）作为训练目标，并在末尾补上结束符 `eos_token`。

> 注意 `Response[-1]` 这个写法——它正是「`Response` 是列表」的直接证据。在 `Resyn27k.json` 里每条 `Response` 只有一个元素，所以 `[-1]` 取到的就是那唯一的参考答案。

#### 4.1.4 代码实践

**目标**：验证三份文件确实都是「逐行 JSON」，且能用同一套代码读取。

**步骤**：在仓库根目录新建 `inspect_lines.py`（这是你自己的临时脚本，不要放进 `RTL-Coder-tutorial/` 之外的项目目录以外；放在仓库根目录运行完即可删除）：

```python
# 示例代码：逐行统计每份 JSON 的行数与首行字段
import json

files = [
    "data_generation/data_sample.json",
    "dataset/Resyn27k.json",
    "train/scoring_data_sample.json",
]
for fn in files:
    with open(fn, "r") as f:
        lines = f.readlines()
    first = json.loads(lines[0])
    print(f"{fn}: 共 {len(lines)} 行, 首行字段 = {list(first.keys())}")
```

**预期结果**（基于实际阅读源文件）：

- 三份文件都能被逐行 `json.loads` 成功解析（即它们都是 JSONL）；
- 首行字段分别是：
  - `data_generation/data_sample.json` → `['Instruction', 'Input', 'Response']`
  - `dataset/Resyn27k.json` → `['Instruction', 'Response']`
  - `train/scoring_data_sample.json` → `['Instruction', 'Input', 'Response', 'Score']`

**需要观察的现象**：如果哪份文件首行字段与上面不符，说明它不是 JSONL 或结构有变，应停下来核对。运行环境若未装 Python，可改用 `head -c 200 <文件>` 肉眼查看每行开头。

#### 4.1.5 小练习与答案

**练习 1**：为什么项目不用「一个大 JSON 数组」`[{...}, {...}]` 来存所有数据，而要用逐行 JSON？
**参考答案**：因为逐行 JSON 可以流式、按行读取，不必把整个数据集一次性载入内存；也方便在数据生成阶段断点续写（生成一条就追加一行）。代码里 `[json.loads(l) for l in open(...)]` 正是利用了这一点。

**练习 2**：`mle.py` 里 `example['Response'][-1]` 中的 `-1` 起什么作用？
**参考答案**：取 `Response` 列表的最后一个元素。因为 `Response` 被统一存成列表，即使只有一个答案也要用 `[-1]`（或 `[0]`）从列表里取出字符串本身。

---

### 4.2 普通指令格式：`{Instruction, Input, Response}`（生成样本）

#### 4.2.1 概念说明

`data_generation/data_sample.json` 是 GPT 数据生成流水线（u2 单元详讲）产出的样本文件。它最完整地保留了「指令 + 模块骨架 + 参考代码」三元组，是理解另外两种格式的基准。

它的三个字段含义：

- **Instruction**：给 GPT 的完整指令文本，例如「请作为一个专业的 Verilog 设计师，设计一个信道均衡模块……输入是……输出是……功能是……」。
- **Input**：模块的端口签名骨架，例如：
  ```verilog
  module ChannelEqualization (
    input [n-1:0] transmitted_signal,
    input [n-1:0] channel_response,
    output [n-1:0] equalized_signal
  );
  parameter n = 8;
  // Define your inputs and outputs here
  endmodule
  ```
  注意结尾是空的 `endmodule`——这是一个等待被填空的「壳」。
- **Response**：单元素列表 `[ "...完整 Verilog 实现..." ]`，里面是 GPT 生成的、把上述壳填满的完整代码。

#### 4.2.2 核心流程

生成一条样本的过程可以抽象为：

```
关键词/模板  ──GPT-3.5──▶  Instruction（自然语言指令）
 Instruction ──GPT-3.5──▶  Input（模块骨架） + Response（参考实现）
        ──写盘──▶  {"Instruction":..., "Input":..., "Response":[...]}  一行
```

也就是说，GPT 在这里被用了**两次**：先变出指令，再变出对应的骨架与参考代码。这与 u1-l1 强调的「GPT 仅用于造数据、不参与最终模型」一致。

#### 4.2.3 源码精读

看 `data_generation/data_sample.json` 的第 1 行（信道均衡块）：

[data_generation/data_sample.json:L1](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/data_sample.json#L1) —— 一条生成样本，字段为 `{Instruction, Input, Response}`，`Response` 是单元素列表。

这条记录里：

- `Instruction`：一大段自然语言，描述信道均衡模块的功能需求；
- `Input`：`module ChannelEqualization(...); ... endmodule` 的空壳骨架；
- `Response`：`["module ChannelEqualization(...); ...wire...assign equalized_signal = ...; endmodule"]`——把骨架补全后的完整实现，被包在一个**长度为 1 的列表**里。

> 你可以观察到：`Response[0]` 的开头几乎照搬了 `Input` 的模块签名，然后补上了 `wire`、`assign` 等实现细节，并在结尾补上 `endmodule`。这正是「填空」的痕迹。

#### 4.2.4 代码实践

**目标**：确认生成样本里 `Response` 列表长度为 1，并体会 Input 与 Response 的「壳 vs 填充」关系。

**步骤**：

```python
# 示例代码
import json

with open("data_generation/data_sample.json") as f:
    sample = json.loads(f.readline())

print("字段:", list(sample.keys()))
print("Response 列表长度:", len(sample["Response"]))
print("--- Input（骨架）前 120 字 ---")
print(sample["Input"][:120])
print("--- Response[0]（实现）前 120 字 ---")
print(sample["Response"][0][:120])
```

**预期结果**：

- `字段` 为 `['Instruction', 'Input', 'Response']`；
- `Response 列表长度` 为 `1`；
- `Input` 与 `Response[0]` 开头都是 `module ChannelEqualization (...)`，但 `Response[0]` 后面有真正的实现语句。

**需要观察的现象**：`Input` 到 `endmodule` 之间基本是空的或只有注释，而 `Response[0]` 之间是满满当当的逻辑。

#### 4.2.5 小练习与答案

**练习 1**：`Input` 字段里为什么经常出现 `// Define your inputs and outputs here` 这种注释？
**参考答案**：这是给模型（或 GPT）的一个填空提示，标明「请在这里补充实现」。它本质上是 prompt 工程的一部分，引导生成代码出现在正确的位置。

**练习 2**：如果把 `Response` 从列表改成普通字符串，下游代码哪里会出错？
**参考答案**：`mle.py` 里的 `example['Response'][-1]` 会失效——对字符串做 `[-1]` 只会取到最后一个**字符**，而不是整段代码。所以保持列表结构是必要的。

---

### 4.3 成品训练集 `Resyn27k.json`：`Response` 为列表、且**没有** `Input`

#### 4.3.1 概念说明

`dataset/Resyn27k.json` 是清洗汇总后的成品训练集，约 2.7 万条，供标准监督微调（SFT，`mle.py`）使用。它和生成样本最大的区别有两个：

1. **没有 `Input` 字段**——每条记录只剩 `Instruction` 和 `Response`；
2. `Response` 依然是**列表**，但每条只有一个元素。

换句话说，成品数据集把「模块骨架」这个中间信息丢掉了，只保留「自然语言指令 → 完整代码」这一对。这样训练时模型直接从指令生成完整代码，不依赖骨架。

#### 4.3.2 核心流程

从生成样本到成品训练集的「瘦身」过程：

```
{"Instruction":..., "Input":..., "Response":[code]}   ← 生成样本（data_sample.json）
                         │  清洗 / 去重 / 汇总（约 2.7 万条）
                         ▼
{"Instruction":..., "Response":[code]}                ← 成品训练集（Resyn27k.json）
```

> 🔑 注意：丢掉 `Input` 是一个**有意的设计选择**。它让评测和推理阶段的输入更简单——只喂 `Instruction` 即可（见 u2-l8 的基准脚本），不必再准备骨架。

#### 4.3.3 源码精读

看 `Resyn27k.json` 第 1 行（一个简易计算器模块）：

[dataset/Resyn27k.json:L1](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/dataset/Resyn27k.json#L1) —— 成品训练集的一条记录，字段只有 `{Instruction, Response}`，**无 Input**。

这条记录里：

- `Instruction`：`"\nYou are tasked with designing a module for a simple calculator...add/sub/mul/div...If b is zero, the div output should be zero..."`；
- `Response`：`["module calculator(...); ... add<=a+b; ... endmodule"]`——一个单元素列表，里面是完整的计算器实现。

注意它**没有** `Input` 字段。再对照 `mle.py` 的消费方式：

[mle.py:L124-L127](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py#L124-L127) —— 标准微调只读取 `Instruction` 与 `Response[-1]`，从不访问 `Input`。

这段代码只用 `example['Instruction']` 和 `example['Response'][-1]`，完全不需要 `Input`——这与 `Resyn27k.json` 没有 `Input` 字段是互相吻合的。

#### 4.3.4 代码实践

**目标**：用代码验证 `Resyn27k.json` 的首行**确实没有 `Input` 字段**，且 `Response` 是单元素列表。

**步骤**：

```python
# 示例代码
import json

with open("dataset/Resyn27k.json") as f:
    rec = json.loads(f.readline())

print("字段:", list(rec.keys()))
print("是否有 Input 字段:", "Input" in rec)
print("Response 长度:", len(rec["Response"]))
print("Instruction 前 100 字:", rec["Instruction"][:100].replace("\n", " "))
```

**预期结果**：

- `字段` 为 `['Instruction', 'Response']`；
- `是否有 Input 字段` 为 `False`；
- `Response 长度` 为 `1`。

**需要观察的现象**：当你尝试 `rec["Input"]` 时会抛出 `KeyError`，这正是「该字段不存在」的铁证。

#### 4.3.5 小练习与答案

**练习 1**：既然 `Response` 里只有一个元素，为什么 `mle.py` 还要写成 `Response[-1]` 而不是 `Response[0]`？
**参考答案**：`[-1]` 与 `[0]` 在单元素列表上结果相同。使用 `[-1]` 可能是为了与评分训练数据里「最后一个候选是参考答案」的约定保持一致（见 4.4），让两套数据/代码在语义上对齐：**列表最后一个元素 = 最佳参考答案**。

**练习 2**：如果在 `Resyn27k.json` 上误用 `example['Input']`，会发生什么？
**参考答案**：会抛出 `KeyError: 'Input'`，因为这个字段在成品训练集里已被丢弃。这也提醒我们：**不同阶段的 JSON 字段并不完全相同，不能假设一份模板通吃所有文件**。

---

### 4.4 评分训练格式：多候选 `Response[]` + `Score[]`

#### 4.4.1 概念说明

`train/scoring_data_sample.json` 服务于 RTL-Coder 的核心创新——**基于代码质量评分的训练**（u3-l1 详讲）。它的关键特征是：**同一条指令配多个候选答案，并给每个候选打一个质量分**。

字段含义：

- **Instruction**：自然语言指令（与前面一致）；
- **Input**：模块骨架（在这里**又回来了**）；
- **Response**：一个**多元素列表**，每个元素都是一段完整 Verilog 实现，对应同一条指令的不同候选答案（样本里通常是 4 个）；
- **Score**：一个与 `Response` **等长**的浮点数列表，`Score[i]` 就是 `Response[i]` 的质量分，取值在 \([0, 1]\) 区间，**1.0 表示满分（通常是参考答案）**。

> 🔑 评分对齐关系：\[ \text{Score}[i] \;\leftrightarrow\; \text{Response}[i] \] 两个列表按位置一一对应。这是评分训练能把「代码内容」与「质量信号」绑在一起学习的前提。

#### 4.4.2 核心流程

评分训练数据的构造与消费流程：

```
一条 Instruction（+Input）
        │  让模型/采样得到 N 个候选代码 Response[0..N-1]
        │  用功能仿真/基准打分得到 Score[0..N-1]
        ▼
{"Instruction":..., "Input":..., "Response":[c0,c1,...,cN-1], "Score":[s0,s1,...,sN-1]}
        │  mle_scoring.py 的 ScoreDataset 读入
        ▼  对每个候选都算损失，并按 Score 做比较/加权
```

观察样本里的 `Score` 列表可以发现一个规律：**最后一个候选的分数往往是 1.0**，即参考答案排在最后且满分；其余候选分数较低，代表模型采样得到的「不够好」的实现。这正是评分训练要利用的信号——让模型学会「偏爱高分答案」。

#### 4.4.3 源码精读

看 `scoring_data_sample.json` 第 1 行：

[train/scoring_data_sample.json:L1](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/scoring_data_sample.json#L1) —— 评分训练样本，字段为 `{Instruction, Input, Response, Score}`，`Response` 与 `Score` 都是 4 元素列表。

这条记录里（信道均衡块）：

- `Response`：4 段不同的 Verilog 实现组成的列表；
- `Score`：`[0.4037882049074473, 0.35548841893252764, 0.0022547914317925587, 1]`——4 个分数，**最后一个恰为 1**，对应参考答案。

再看评分训练脚本如何消费这种结构：

[mle_scoring.py:L139-L152](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L139-L152) —— `DataCollator` 读取多候选 + 分数，对每个候选都构造一条「query + 候选」序列。

这段代码的关键三行：

```python
query = ins['Instruction']      # 第 139 行
responses = ins['Response']     # 第 140 行：拿到候选列表
scores = ins['Score']           # 第 141 行：拿到分数列表（与候选等长）
...
for res in responses:           # 第 148 行：遍历每一个候选
    ...
```

可以看到：

- 它读取了 `Instruction`、`Response`、`Score` 三个字段；
- 用 `for res in responses` **遍历所有候选**（不像 `mle.py` 只取 `[-1]`），把每个候选都拼成一条训练序列；
- `scores` 被收集进 `all_scores`，供后续比较损失使用（u3-l1、u3-l2 详讲）。

这就解释了为什么评分训练数据必须是「多候选 + 分数」的结构——训练算法需要**同一指令下多个候选之间的相对质量关系**。

#### 4.4.4 代码实践

**目标**：用代码验证 `Response` 与 `Score` 等长，并观察「最后一个候选分数 = 1」的规律。

**步骤**：

```python
# 示例代码
import json

with open("train/scoring_data_sample.json") as f:
    lines = f.readlines()

for i in range(min(3, len(lines))):
    rec = json.loads(lines[i])
    print(f"--- 第 {i+1} 条 ---")
    print("字段:", list(rec.keys()))
    print("Response 候选数:", len(rec["Response"]),
          "| Score 个数:", len(rec["Score"]),
          "| 等长:", len(rec["Response"]) == len(rec["Score"]))
    print("Score:", rec["Score"])
    print("最后一个候选分数:", rec["Score"][-1])
```

**预期结果**（基于实际阅读样本前几条）：

- 每条字段为 `['Instruction', 'Input', 'Response', 'Score']`；
- `Response` 与 `Score` **等长**（样本里通常都是 4）；
- `Score` 是 \([0,1]\) 区间的浮点数列表，且**最后一个元素为 `1`**（参考答案）。

**需要观察的现象**：把多条记录的 `Score` 打印出来对比，你会看到非参考候选的分数明显低于 1（有的甚至接近 0，如第 1 条里的 `0.0022`），这说明它们是「质量较差」的负样本。

> 待本地验证：如果你计算的环境里 `Score` 不是 4 个、或最后一个不为 1，请以你本地文件的实际内容为准——样本数据可能随版本更新而调整。

#### 4.4.5 小练习与答案

**练习 1**：如果某条记录里 `Response` 有 4 个候选、但 `Score` 只有 3 个分数，下游训练会发生什么？
**参考答案**：`Response[i]` 与 `Score[i]` 的位置对应关系会错乱甚至越界。评分训练依赖两者等长且按位置对齐，长度不一致会导致索引错误或学到错误的质量排序。这正是为什么必须用 `len(Response) == len(Score)` 做校验。

**练习 2**：为什么评分训练要把「参考答案」也放进候选列表（而不是只放负样本）？
**参考答案**：因为评分训练的目标不只是「远离负样本」，还要「学到正样本的语言模型概率」。参考答案（Score=1）提供了正向目标，配合低分候选形成**对比（contrastive）** 信号，让模型同时知道「什么好」和「什么不好」。

---

## 5. 综合实践

把三种格式串起来，完成一个小任务：**写一个「数据格式体检脚本」**，自动判断任意一份 JSONL 属于哪种格式。

**任务要求**：

1. 逐行读取目标文件的前若干行；
2. 根据字段集合分类：
   - 只有 `{Instruction, Response}` → 判为「成品训练集（Resyn27k 风格）」；
   - 有 `{Instruction, Input, Response}` 但无 `Score` → 判为「生成样本（data_sample 风格）」；
   - 有 `{Instruction, Input, Response, Score}` → 判为「评分训练数据」；
3. 对评分训练数据，额外校验每条 `len(Response) == len(Score)`，并统计 `Score[-1] == 1` 的比例。

**参考实现骨架**（示例代码，请自行补全并运行）：

```python
# 示例代码：数据格式体检
import json

def classify(fn, n=5):
    with open(fn) as f:
        rows = [json.loads(line) for _, line in zip(range(n), f)]
    keys = set(rows[0].keys())
    if keys == {"Instruction", "Response"}:
        kind = "成品训练集（无 Input）"
    elif keys == {"Instruction", "Input", "Response"}:
        kind = "生成样本（含 Input，无 Score）"
    elif keys == {"Instruction", "Input", "Response", "Score"}:
        kind = "评分训练数据（多候选 + Score）"
    else:
        kind = f"未知格式：{keys}"
    print(f"{fn} -> {kind}")

    if "Score" in keys:
        ok = all(len(r["Response"]) == len(r["Score"]) for r in rows)
        last_is_one = sum(r["Score"][-1] == 1 for r in rows) / len(rows)
        print(f"    Response/Score 等长: {ok}; 末位 Score==1 占比: {last_is_one:.0%}")

for fn in ["data_generation/data_sample.json",
           "dataset/Resyn27k.json",
           "train/scoring_data_sample.json"]:
    classify(fn)
```

**预期输出**：三份文件依次被判定为「生成样本」「成品训练集」「评分训练数据」，其中评分训练数据那行会额外打印等长校验通过、末位 `Score==1` 占比约为 100%。

**验收标准**：脚本能正确分类三份文件；若你把 `Resyn27k.json` 的路径换成生成样本，分类结果应当相应变化。

---

## 6. 本讲小结

- 三份数据文件**全部采用逐行 JSON（JSONL）** 存储，因此训练脚本里普遍出现 `[json.loads(l) for l in open(...)]` 的写法。
- `Response` 在本项目里**统一存成列表**，即便只有一个答案也用单元素列表包裹——这是为了让生成、训练、评分共用同一套读写逻辑。
- `data_generation/data_sample.json` 是**最完整的三元组** `{Instruction, Input, Response}`，是 GPT 造数据的产物。
- 成品训练集 `dataset/Resyn27k.json` **丢掉了 `Input` 字段**，只剩 `{Instruction, Response}`；标准 SFT（`mle.py`）用 `Response[-1]` 取训练目标，从不访问 `Input`。
- 评分训练数据 `train/scoring_data_sample.json` 是 `{Instruction, Input, Response[], Score[]}`，多候选与多分数**按位置一一对应**，末位候选通常为参考答案（`Score=1`）。
- 两种训练脚本对数据的消费方式截然不同：`mle.py` 只取一个参考答案，`mle_scoring.py` 遍历所有候选并读取 `Score`——这正是两种数据格式存在的根本原因。

---

## 7. 下一步学习建议

- 想知道这些 JSON 是**怎么被生成出来**的，请进入 u2 单元，尤其是：
  - **u2-l1《数据集生成流程总览》**：三阶段生成流程全貌；
  - **u2-l4《指令生成主循环 instruction_gen.py》**：逐行落盘 JSON 的主循环。
- 想知道**标准 SFT 如何把 `{Instruction, Response}` 变成训练张量**（标签掩码、`IGNORE_INDEX`），请读 **u2-l7《mle.py 标准监督微调》**。
- 想深入**评分训练如何利用多候选 + Score**（比较损失、候选归一化），请读 **u3-l1《mle_scoring.py 质量评分训练》** 与 **u3-l2《比较损失的数学原理》**。
- 建议动手：在本讲综合实践脚本的基础上，加上「统计每条 `Instruction` 的平均 token 长度」的功能，提前体会 `model_max_length` 过滤（`ScoreDataset` 里 `model_max_length*0.5` 的阈值）的动机。
