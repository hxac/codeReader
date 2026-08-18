# u2-l1 数据下载与训练/评测切分

## 1. 本讲目标

学完本讲，你应该能够：

- 独立运行 `scripts/data/download_and_split.py`，产出一份训练 JSONL 和一份评测 JSONL。
- 说清 `--sample-size`、`--test-size`、`--seed`、`--skip-existing` 这四个参数各自控制什么。
- 准确描述两种输出文件的字段约定：训练侧的 `id + conversations`，评测侧的 `turns`，并理解为什么评测侧只保留 user 提问。

本讲是第 2 单元「数据流水线」的第一步——在动手生成 target cache（那个默认约 38 TB 的大文件）之前，先把最轻、最容易跑通的一步吃透。

## 2. 前置知识

承接 u1-l2 建立的认知：DeepSpec 的数据准备、训练、评估三阶段通过**磁盘文件**而非函数调用交接。本讲处理的是第一份交接产物。

本讲需要的基础概念：

- **JSONL**：JSON Lines，一行一个独立的 JSON 对象，用 `\n` 分隔。相比整份大 JSON，它的好处是可以流式逐行读写、可以 `wc -l` 数条数、可以 `head` 抽查，非常适合百万级语料。
- **ShareGPT 对话格式**：Hugging Face 上常见的一种多轮对话表示。每条样本有一个 `conversations` 列表，每条消息形如 `{"from": "human", "value": "..."}`，`from` 表示说话方，`value` 是文本。`mlabonne/open-perfectblend` 用的就是这种格式。
- **role 风格**：DeepSpec 内部统一用 `user` / `assistant` 两种角色（与聊天模板的惯例一致），所以需要把 ShareGPT 的 `human` / `gpt` 等映射过来。
- **留出集（held-out set）**：从训练语料中随机切出一小部分，永远不参与训练，专门用于评测。这里切出的不是完整对话，而是**只保留 user 提问**——因为评测时 assistant 回复要由目标模型现场生成（这正是 u2-l3 要做的事）。
- **Hugging Face `datasets` 库**：`load_dataset` 负责从 HF Hub 下载数据并加载为 `Dataset` 对象；`train_test_split` 负责随机切分。依赖已在 [requirements.txt:16-18](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/requirements.txt#L16-L18) 中声明（`datasets==4.8.5`），安装 `python -m pip install -r requirements.txt` 即可覆盖。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [scripts/data/download_and_split.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/download_and_split.py) | 本讲主角：下载数据、清洗对话、切分、写出两份 JSONL，全流程不到 200 行 |
| [scripts/data/README.md](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md) | 数据准备三步的官方文档，第 1 步就是本讲脚本的标准调用方式 |
| [scripts/data/prepare_data.sh](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_data.sh) | 三步流水线的包装脚本，可以看到本讲脚本在整条链路中的位置 |
| [eval_datasets/gsm8k.jsonl](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval_datasets/gsm8k.jsonl) | 仓库自带的评测数据样例，用来对照确认 `turns` 格式 |
| [eval_datasets/convert_eval_datasets_to_jsonl.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval_datasets/convert_eval_datasets_to_jsonl.py) | 生成官方评测集的工具，其文档字符串交代了 `perfectblend.jsonl` 的来历 |

## 4. 核心概念与源码讲解

### 4.1 数据集下载与切分：从 HF Hub 到标准对话格式

#### 4.1.1 概念说明

这一步解决的问题是：**训练草稿模型需要多轮对话语料，而公开语料的格式五花八门**。DeepSpec 选择了 `mlabonne/open-perfectblend` 作为默认语料（一个约百万量级的混合指令数据集），但下游的对话模板解析器（u2-l2 的 `parser.py`）只认 `role`/`content` 风格。因此第一步脚本要完成三件事：

1. 从 HF Hub 下载指定数据集的指定 split；
2. 把 ShareGPT 风格的 `from`/`value` 消息翻译成 `role`/`content`，并顺手过滤掉未知角色；
3. 给每一行打上全局递增的 `id`，方便后续步骤追踪和对齐。

#### 4.1.2 核心流程

```text
load_dataset(name, split)          # 下载/加载
        │
        ▼  (可选) --sample-size
dataset.select(range(N))           # 只取前 N 行
        │
        ▼
dataset.map(add_index)             # 打上源行号 id
        │
        ▼
normalize_conversations           # from/value → role/content
        │
        ▼
validate_conversations            # 质量闸门：非空、首条必须是 user
```

#### 4.1.3 源码精读

脚本顶部的常量交代了所有默认值。[scripts/data/download_and_split.py:8-18](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/download_and_split.py#L8-L18) 定义了默认数据集名、默认输出路径和角色映射表：

- 默认训练输出是 `cache/dataset/perfectblend_train.jsonl`，而官方文档的命令显式传了 `train_datasets/perfectblend_train.jsonl`——也就是说**不传参时文件落在 cache 目录，README 的用法才与后续步骤对齐**，这是初学者容易踩的第一个坑。
- `ROLE_MAPPING` 把 `human` 映射为 `user`，把 `gpt`/`chatgpt`/`bing`/`bard` 都映射为 `assistant`。不在这张表里的角色（例如 `system`）会被直接丢弃。

[scripts/data/download_and_split.py:107-114](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/download_and_split.py#L107-L114) 是下载逻辑：`from datasets import load_dataset` 被刻意放进函数体内（延迟导入，让 `--help` 不必先装好 datasets），随后若指定了 `--sample-size` 且小于数据集长度，就用 `select(range(...))` 截取前 N 行，最后用 `map(add_index, with_indices=True)` 给每行写入 `id` 列。注意 **`id` 是在切分之前打的**，所以切分后 train/test 里的 `id` 仍指向源数据集中的原始行号。

[scripts/data/download_and_split.py:117-128](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/download_and_split.py#L117-L128) 做格式翻译：逐条消息查 `ROLE_MAPPING`，查不到就 `continue` 跳过，查到的转成 `{"role": ..., "content": ...}`，返回只含 `id` 和 `conversations` 两个字段的精简字典。

[scripts/data/download_and_split.py:131-141](https://github.com/deepseek-ai-DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/download_and_split.py#L131-L141) 是质量闸门：清洗后的对话非空、第一条必须是 `user`、所有角色只能是 `user`/`assistant`、`content` 必须是非空字符串。任何一条不满足就抛 `ValueError` 并带上行号，**整个脚本立即失败**——这是刻意的设计：脏数据宁可早点炸掉，也不要带进后面 38 TB 的缓存里。

#### 4.1.4 代码实践

**实践目标**：跑通下载与清洗，观察输出。

操作步骤（命令来自官方文档 [scripts/data/README.md:31-38](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md#L31-L38)，为省流量加了 `--sample-size`，并把输出名改成 `mini` 以免和正式产物混淆）：

```bash
# 1. 安装依赖（datasets 已包含在内）
python -m pip install -r requirements.txt

# 2. 小规模跑通：只处理前 200 行源数据
python scripts/data/download_and_split.py \
    --test-size 0.05 \
    --sample-size 200 \
    --train-output-path train_datasets/mini_train.jsonl \
    --test-output-dir eval_datasets \
    --test-output-name mini_eval.jsonl
```

需要观察的现象：

1. 终端最后打印两行 `wrote train split: X rows -> ...` 和 `wrote eval split: Y rows -> ...`；
2. 紧接着**原样再执行一次同一条命令**，应当报错退出：`Output JSONL already exists: ...`（原因见 4.3.3 的 `check_output_paths`）；
3. 第三次执行时在命令末尾追加 `--skip-existing`，应当打印两行 `skip existing output: ...` 后安静退出。

预期结果：`train_datasets/mini_train.jsonl` 与 `eval_datasets/mini_eval.jsonl` 两个文件生成，且 X + Y = 200。注意 `--sample-size` 只减少**处理**的行数，`load_dataset` 仍会下载整个数据集，流量敏感的环境请知悉（待本地验证具体下载体积）。

如果网络不可用，可以用离线替代实践：检查仓库自带的 [eval_datasets/gsm8k.jsonl:1](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval_datasets/gsm8k.jsonl#L1) 每行结构，并用纯 Python 复演 `normalize_conversations`：自造一条含 `"from": "system"` 的 ShareGPT 消息列表，验证它被丢弃、其余消息被翻译。

#### 4.1.5 小练习与答案

**练习 1**：一条源数据的 `conversations` 以 `{"from": "system", ...}` 开头，随后是 `human`/`gpt` 交替。这条数据能通过本脚本吗？

答案：能。`normalize_conversations` 会把 `system` 消息直接丢弃（不在 `ROLE_MAPPING` 中），丢弃后第一条剩余消息是 `user`，能通过 `validate_conversations` 的首条检查。

**练习 2**：如果把一条 `gpt` 开头（没有 preceding human）的数据喂进来，会发生什么？

答案：`validate_conversations` 检测到 `conversations[0]["role"] != "user"`，抛出 `ValueError("row N does not start with a user message.")`，脚本整体终止。因为写文件用的是覆盖模式（`open("w")`），此前已写入的行会留在磁盘上但文件不完整——这正是下一节输出路径保护存在的原因之一。

**练习 3**：为什么 `id` 要在 `train_test_split` 之前打入数据集，而不是切分之后再打？

答案：在切分前打 `id`，train 与 eval 两份产物中同一条对话保留的是**同一个源行号**，可双向追溯；若切分后再打，两份文件的编号各自独立，无法对回答案来源。

### 4.2 train/test 划分：train_test_split 与可复现种子

#### 4.2.1 概念说明

切分解决的问题是：**评测必须用训练时没见过的提问**，否则测出来的接受率衡量的是记忆而不是泛化。脚本把这一步完全委托给 `datasets` 库的 `train_test_split`：它默认先打乱（shuffle）再按比例切，固定 `seed` 即可复现。

#### 4.2.2 核心流程

[scripts/data/download_and_split.py:177-194](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/download_and_split.py#L177-L194) 的 `main()` 给出了完整编排：

```text
parse_args → validate_args → check_output_paths（已存在且 --skip-existing 则直接返回）
    → load_source_dataset（4.1）
    → dataset.train_test_split(test_size, seed)
    → write_train_jsonl(split["train"])   →  训练 JSONL
    → write_eval_jsonl(split["test"])     →  评测 JSONL
```

设处理后的样本总数为 \( N \)，留出比例为 \( s \)（默认 0.05），则：

\[ n_{\text{test}} \approx N \cdot s, \qquad n_{\text{train}} = N - n_{\text{test}} \]

具体取整规则由 `datasets` 库内部决定，最可靠的确认方式就是跑一次小规模切分数一数（见下方实践）。默认参数下 \( s = 0.05 \) 恰好是 SpecForge 的默认值，参数说明里写明了这一点（[scripts/data/download_and_split.py:40-45](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/download_and_split.py#L40-L45)）。

参数校验在 [scripts/data/download_and_split.py:77-81](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/download_and_split.py#L77-L81)：`--sample-size` 必须为正，`--test-size` 必须严格落在开区间 \( (0, 1) \)——既不允许 0（评测集为空没有意义），也不允许 1（训练集为空）。`--seed` 默认 42（[scripts/data/download_and_split.py:46-51](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/download_and_split.py#L46-L51)）。

#### 4.2.3 源码精读

真正执行切分的只有一行：[scripts/data/download_and_split.py:187](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/download_and_split.py#L187) 调用 `dataset.train_test_split(test_size=args.test_size, seed=args.seed)`，返回一个字典风格的 `DatasetDict`，随后 `split_dataset["train"]` 与 `split_dataset["test"]` 分别流向两个写文件函数。整条链路在官方包装脚本 [scripts/data/prepare_data.sh:30-36](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_data.sh#L30-L36) 中被固定为 `test_size=0.05`（变量定义见 [scripts/data/prepare_data.sh:7-10](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_data.sh#L7-L10)），与文档 [scripts/data/README.md:27-45](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md#L27-L45) 的推荐命令一致。

值得注意的是 `main()` 开头的返回路径：[scripts/data/download_and_split.py:180-184](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/download_and_split.py#L180-L184) 把 `check_output_paths` 抛出的 `FileExistsError` 转换成干净的 `SystemExit`（只打印消息，不带 traceback），让重复执行时的报错对使用者友好。

#### 4.2.4 代码实践

**实践目标**：亲手验证切分比例与可复现性。

操作步骤（接着 4.1.4 的产物）：

```bash
wc -l train_datasets/mini_train.jsonl eval_datasets/mini_eval.jsonl
```

再用一段独立脚本验证同样输入切两次结果一致（示例代码，非项目原有代码）：

```python
# 示例代码：验证 seed 固定时切分可复现
from datasets import Dataset
ds = Dataset.from_dict({"conversations": [[{"from": "human", "value": f"q{i}"}] for i in range(200)]})
a = ds.train_test_split(test_size=0.05, seed=42)
b = ds.train_test_split(test_size=0.05, seed=42)
assert [row["conversations"][0]["value"] for row in a["test"]] == \
       [row["conversations"][0]["value"] for row in b["test"]]
print("reproducible, test size =", len(a["test"]))
```

需要观察的现象：两个 `wc -l` 数字之和等于处理的源行数（200）；示例脚本的断言通过。

预期结果：`mini_eval.jsonl` 约 10 行（200 × 0.05），`mini_train.jsonl` 约 190 行；同 seed 两次切分完全一致。具体取整方式以本地运行结果为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么留出的是 user 提问而不是完整对话？

答案：投机解码评测要度量「草稿模型能否追上目标模型」的接受率，assistant 回复必须由目标模型在评测时现场生成（否则无从验证）；而提问只是触发生成的 prompt，所以评测侧只需要 `turns`。完整对话留给训练侧使用。

**练习 2**：把 `--test-size` 调成 0.5 会发生什么？

答案：通过校验（0.5 在开区间内），一半数据被留出、训练数据减半——这正是讲义开头建议的「把 test-size 调大减少训练数据量」的省资源做法；评测文件则会变成约 100 行。

**练习 3**：换一个 `--seed` 会导致什么变化？

答案：打乱顺序不同，落入 train 与 eval 的样本集合不同，两份 JSONL 的内容随之改变；但对同一 seed，任何人任何机器上重跑都得到相同划分（可复现性）。

### 4.3 JSONL 输出格式：conversations 与 turns 两种约定

#### 4.3.1 概念说明

同一次切分写出**两种 schema 不同的文件**，因为两个下游消费者的需求不同：

- 训练侧（`generate_train_data.py`、`parser.py`）需要完整的多轮对话来渲染聊天模板，因此保留 `id + conversations`；
- 评测侧（`eval.py`）只需要 user 提问列表，因此精简为 `{"turns": [...]}`。

`turns` 是一个字符串列表，多轮对话时每个 user 提问占一项——这与仓库自带的 gsm8k 等评测集完全同构，所以本脚本的产出可以直接混进 `eval_datasets/` 参与统一评测。

#### 4.3.2 核心流程

```text
split["train"] ── write_train_jsonl ──▶ 每行 {"id": int, "conversations": [{"role","content"}, ...]}
split["test"]  ── write_eval_jsonl  ──▶ 每行 {"turns": [str, ...]}
```

#### 4.3.3 源码精读

[scripts/data/download_and_split.py:152-161](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/download_and_split.py#L152-L161) 的 `write_train_jsonl`：建父目录 → 以覆盖模式打开 → 逐行「翻译 + 校验 + `json.dumps`（`ensure_ascii=False` 保留中文原文）」，返回写入条数。注意 `enumerate(dataset, start=1)` 的 `row_number` 是**切分后子集内**的序号，只用于报错信息，与 `row["id"]` 的源行号是两回事。

[scripts/data/download_and_split.py:164-174](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/download_and_split.py#L164-L174) 的 `write_eval_jsonl` 结构完全相同，差别只在序列化对象：先经 [user_turns（scripts/data/download_and_split.py:144-149）](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/download_and_split.py#L144-L149) 过滤出全部 `role == "user"` 的 `content`，再写成 `{"turns": turns}`——`id` 被有意丢弃。

输出路径保护由 [scripts/data/download_and_split.py:88-99](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/download_and_split.py#L88-L99) 的 `check_output_paths` 实现，规则很讲究：

- 两个输出文件都不存在 → 正常执行；
- **两个都存在**且带了 `--skip-existing` → 打印后跳过（幂等，方便 `prepare_data.sh` 反复重跑）；
- 只有**其中一个**存在，或没带 `--skip-existing` → 抛 `FileExistsError`。

由于写文件是整体覆盖而非追加，「半成品文件被静默重写」或「完整产物被无意覆盖」都由这道闸门挡住。

格式佐证：[eval_datasets/gsm8k.jsonl:1](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval_datasets/gsm8k.jsonl#L1) 的每一行都是 `{"turns": ["……题干……\nPlease reason step by step, ..."]}`，与本脚本的评测输出同构。另一个容易困惑的点在 [eval_datasets/convert_eval_datasets_to_jsonl.py:26-29](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval_datasets/convert_eval_datasets_to_jsonl.py#L26-L29) 的文档字符串：它提到「checked-in perfectblend.jsonl 由再生成后的测试缓存构建」。但按当前 HEAD 检查，`eval_datasets/` 中**并没有** `perfectblend.jsonl`——它是你运行本脚本后才会生成的文件，仓库里自带的其他评测集（gsm8k、math500 等）才是 checked-in 的。

#### 4.3.4 代码实践

**实践目标**：机器验证两种输出文件的字段约定（对应讲义规格中的实践任务）。

操作步骤（示例代码，非项目原有代码）：

```python
# check_format.py：校验 4.1.4 产出的两份 JSONL
import json

def check(path, required_keys):
    n = 0
    with open(path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            row = json.loads(line)
            missing = required_keys - row.keys()
            assert not missing, f"{path}:{line_no} 缺少字段 {missing}"
            n += 1
    return n

train_n = check("train_datasets/mini_train.jsonl", {"id", "conversations"})
eval_n = check("eval_datasets/mini_eval.jsonl", {"turns"})
print(f"train rows = {train_n}, eval rows = {eval_n}, total = {train_n + eval_n}")
```

需要观察的现象：断言全部通过；打印的 train 行数与脚本退出时打印的 `wrote train split: X rows` 一致；`total` 恰好等于处理的源行数。

预期结果：`mini_eval.jsonl` 每行有且仅有 `turns` 字段且为非空字符串列表；`mini_train.jsonl` 每行含 `id`（整数）与 `conversations`（列表，首项 `role == "user"`）。以上均为「待本地验证」——请以你机器上的实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `--skip-existing` 要求**两个**文件都存在才跳过，而不是「存在一个就跳过那个」？

答案：因为写文件用覆盖模式且脚本不支持断点续写。若只存在一个文件（上次运行中途失败），「存在即跳过」会让另一半永远不被重写，产出一新一旧的假完整产物；现状是显式报错，逼使用者清理后重跑。

**练习 2**：默认输出路径 `DEFAULT_TRAIN_OUTPUT_PATH` 与 README 命令里的路径不一致，以哪个为准？

答案：以 README / `prepare_data.sh` 的显式参数为准（`train_datasets/perfectblend_train.jsonl`），因为第 2 步 `generate_train_data.py` 的 `--input-file-path` 引用的就是这个路径；脚本内的默认值 `cache/dataset/...` 只是不传参时的兜底位置。

**练习 3**：`json.dumps(converted, ensure_ascii=False)` 里去掉 `ensure_ascii=False` 会怎样？

答案：所有非 ASCII 字符（中文、全角标点等）会被转义成 `\uXXXX`，文件体积膨胀且人眼不可读；功能上仍能被 `json.loads` 正确还原，但抽查数据时体验极差。

## 5. 综合实践

为你自己的运行制作一份「数据切分卡片」，把本讲三个模块串起来：

1. 用 `--sample-size 200 --test-size 0.05` 跑一次切分，记录：处理的源行数 N、`wrote train split` 的 X、`wrote eval split` 的 Y，验证 \( X + Y = N \)；
2. 用 4.3.4 的校验脚本机器验证两种 schema，并抽查 3 条训练样本，确认 `conversations` 的 role 严格以 `user` 开头且与 `assistant` 交替（不交替是否一定报错？结合 `validate_conversations` 说明你的结论）；
3. 画一张字段映射图：源 ShareGPT 行 `{conversations: [{from, value}]}` → 训练行 `{id, conversations: [{role, content}]}` → 评测行 `{turns: [str]}`，在箭头上标注每一步丢掉了什么（未知角色、assistant 内容、id）；
4. 回答收尾问题：为什么评测文件里连 `id` 都不留？（提示：想想评测脚本如何统计指标、是否需要回指训练数据。）

产出物：一张笔记页 + 一张映射图。它将在 u2-l3（重生成答案）和 u6-l1（评估框架）被再次引用。

## 6. 本讲小结

- `download_and_split.py` 是三阶段流水线的第一环：下载 `mlabonne/open-perfectblend` → 角色映射清洗 → `train_test_split` 切分 → 写出训练/评测两份 JSONL。
- 训练侧行格式为 `{"id": 源行号, "conversations": [{"role", "content"}]}`；评测侧行格式为 `{"turns": [user 提问字符串列表]}`，与仓库自带 `eval_datasets/*.jsonl` 同构。
- `id` 在切分前打入，train/eval 共享同一套源行号；`validate_conversations` 是宁可失败也不放脏数据的质量闸门。
- `--skip-existing` 只在**两个**输出文件都存在时跳过；只存在一个时抛 `FileExistsError`，防止半成品被误当完整产物。
- 默认 `--train-output-path` 落在 `cache/dataset/`，与后续步骤对齐需显式传 `train_datasets/`，这是新手最易踩的路径坑。
- 切分由 `datasets.train_test_split(test_size, seed)` 完成，默认打乱、seed 固定即可复现。

## 7. 下一步学习建议

训练 JSONL 中的 `conversations` 只是「逻辑对话」，下一讲（u2-l2 对话模板与 loss_mask）将追踪 `deepspec/data/parser.py` 如何用目标模型的聊天模板把它渲染成 token 序列，并用正则匹配 assistant 片段构造 token 级 `loss_mask`——那是理解「草稿模型到底在学什么」的关键一步。建议先浏览 `deepspec/data/` 目录结构热身。
