# 数据集准备

## 1. 本讲目标

在 u2-l2 里我们已经知道：一次 run 的 `data` 段有 `train_data_path / prompts_path / hidden_states_path` 三个字段，**三选一**。但「指向的那个 `.jsonl` 文件到底长什么样、怎么造出来」还没讲。本讲就来补上这一环。

读完本讲，你应该能够：

1. 说出 SpecForge 训练数据的**两种 schema**（对话格式 / 预格式化文本格式），并能手写一条合法的对话格式样例。
2. 用 `scripts/prepare_data.py` 的 19 个**预设**，把 Hugging Face 上的开源数据集一键转换成统一的 `id + conversations` JSONL 契约。
3. 解释**数据再生（regenerate）**为什么能提升草稿模型的接受率，并用 `scripts/regenerate_train_data.py` 把一份对话数据用目标模型重写一遍。

本讲是「数据与特征流水线」单元的第一篇，只讲**文本数据本身**的来源与格式；模板渲染、loss mask、离线特征捕获留到 u5-l2、u5-l3。

## 2. 前置知识

- **训练数据为什么重要**：在 u1-l3 我们学到，投机解码的加速比几乎完全取决于草稿模型的**接受率**，而接受率取决于草稿模型对目标模型输出分布的拟合程度。训练数据越贴近目标模型真正会生成的文本，草稿模型学到的分布就越准。这正是本讲所有设计的出发点。
- **`data` 段三选一**（来自 u2-l2）：`train_data_path`（原始在线数据，本讲的主角）、`prompts_path`（预分词的在线数据）、`hidden_states_path`（离线特征，u5-l3 讲）。本讲产出的文件，就是给 `train_data_path` 用的。
- **JSONL**：每行一个独立 JSON 对象、用换行分隔的文本格式。SpecForge 的训练数据全部以 JSONL 存储，方便流式读取与分布式切分。
- **ShareGPT 格式**：社区常见的多轮对话格式，消息用 `{"from": "human"/"gpt", "value": "..."}` 表示。SpecForge 内部不直接用它，而是统一归一化成下文的「对话格式」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [scripts/prepare_data.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/prepare_data.py) | 把 19 个开源数据集预设转换成统一的 `id + conversations` JSONL；本讲的主体。 |
| [scripts/regenerate_train_data.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/regenerate_train_data.py) | 调用 SGLang 的 OpenAI 兼容接口，用目标模型把对话里的 assistant 回复**重新生成**一遍。 |
| [scripts/conversation_validation.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/conversation_validation.py) | 被再生脚本复用的对话结构校验工具：检查 user/assistant 交替、内容非空、无 `<think>` 残留。 |
| [docs/basic_usage/data_preparation.md](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/data_preparation.md) | 官方数据准备文档，列出了预设表、两种格式说明、再生与离线特征的完整命令。 |

---

## 4. 核心概念与源码讲解

### 4.1 数据格式 schema

#### 4.1.1 概念说明

SpecForge 的训练入口只认**两种**数据 schema，不管数据是从哪个数据集来的、用什么脚本造的，最终都得落到这两种之一：

- **对话格式（Conversation Format）**：保留多轮对话的结构，每条消息标注 `role`（`user` / `assistant` / `system`）和 `content`。SpecForge 后续会用 `chat_template` 把它渲染成模型输入，并自动从 assistant 区间构建 loss mask（u5-l2 详讲）。
- **预格式化文本格式（Pre-formatted Text Format）**：你已经事先用某个 chat template 把对话渲染成了一段连续文本，直接整段塞进 `text` 字段。

为什么要有两种？因为生产环境里经常遇到这种情况：你手上的数据是**当初训练目标模型时用的那批已渲染 prompt**，再加上目标模型的**原始生成结果**。这时候你既没有原始的多轮结构、也不想再渲染一遍（怕和目标模型当年的模板对不上），就直接用预格式化文本格式喂进去。

#### 4.1.2 核心流程

两种格式的**唯一共同点**是：每行一个 JSON 对象，必须有一个 `id` 字段。

```text
原始数据（各种来源）
      │
      ├── 对话格式 ──► {"id": ..., "conversations": [{"role","content"}, ...]}
      │                    │
      │                    └── 后续：chat_template 渲染 + 自动 loss mask（u5-l2）
      │
      └── 预格式化 ──► {"id": ..., "text": "<|im_start|>...<|im_end|>\n..."}
                           │
                           └── 后续：仍需 chat_template「定位 assistant 区间」来切 loss mask
```

关键点（也是 u5-l2 要承接的）：**预格式化并不等于「不需要 chat_template」**。恰恰相反，SpecForge 仍然要求你提供 `data.chat_template`，只不过它的用途从「渲染文本」变成了「识别 assistant 片段、切 loss mask」。这一点官方文档反复强调，初学者很容易踩坑。

#### 4.1.3 源码精读

两种格式的权威定义在官方文档里。先看**对话格式**：

[docs/basic_usage/data_preparation.md:L215-L229](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/data_preparation.md#L215-L229) — 文档明确对话格式的 schema：顶层是 `id` + `conversations`，`conversations` 是消息列表，每条消息有 `role`（取 `user` / `assistant`）和 `content`。这就是 `prepare_data.py` 所有预设统一输出的契约。

再看**预格式化文本格式**及其与 `chat_template` 的关系：

[docs/basic_usage/data_preparation.md:L231-L247](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/data_preparation.md#L231-L247) — 文档给出 `text` 字段的样例（一段已经渲染好的、带 `<|im_start|>` 等特殊 token 的文本），并强调：用预格式化数据时必须设 `data.is_preformatted: true`，**且 `data.chat_template` 仍然必填、必须与当初渲染这段文本所用的模板一致**，SpecForge 靠它来识别 assistant 区间、构建 loss mask。

对应到运行配置，这两个字段就在 `DataConfig` 里：

[specforge/config/schema.py:L127-L138](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L127-L138) — `train_data_path` 指向本讲产出的 JSONL；`is_preformatted` 默认 `False`，预格式化数据要手动置 `True`。这与 u2-l2 讲的「三选一」校验（`_exactly_one_source`）共同决定了数据如何被读取。

#### 4.1.4 代码实践

**实践目标**：手写两条最小的合法训练样本，分别覆盖两种 schema。

**操作步骤**：

1. 新建文件 `my_dataset.jsonl`。
2. 第 1 行写一条**对话格式**样本（单轮）：

```json
{"id": "demo-001", "conversations": [{"role": "user", "content": "用一句话解释投机解码。"}, {"role": "assistant", "content": "用小模型先猜几个 token，再让大模型一次性验证。"}]}
```

3. 第 2 行写一条**预格式化文本格式**样本：

```json
{"id": "demo-002", "text": "<|im_start|>user\n你好<|im_end|>\n<|im_start|>assistant\n你好！有什么可以帮你的？<|im_end|>\n"}
```

**需要观察的现象**：两条样本都满足「顶层有 `id` 字段」这一共同契约；第 1 条额外有 `conversations`，第 2 条额外有 `text`。

**预期结果**：文件能被 `jq` 逐行解析（`cat my_dataset.jsonl | jq .` 不报错）。注意这两种格式**不要混在同一个文件里**——一个文件只该用一种 schema，否则下游解析会出错。

> 待本地验证：把第 2 条样本对应的 YAML 里 `data.is_preformatted` 设成 `true` 并配上匹配的 `chat_template` 后，能否正常进入训练（本讲不实际启动训练，留到 u6）。

#### 4.1.5 小练习与答案

**练习 1**：如果你把 `is_preformatted` 设成了 `true` 但忘了填 `chat_template`，会发生什么？

**参考答案**：SpecForge 仍然需要 `chat_template` 来在已渲染的文本里**定位 assistant 区间、切出 loss mask**。没有它，训练就不知道该监督哪些 token，因此配置校验或装配阶段会报错。预格式化省的是「渲染」，不是「模板」。

**练习 2**：对话格式的一条样本里，`conversations` 列表是否必须以 `user` 开头？

**参考答案**：可以以一条 `system` 消息开头，但第一条非 system 消息必须是 `user`，且之后 `user` / `assistant` 严格交替。这一点由 `conversation_validation.validate_conversation` 强制（见 4.3.3）。

---

### 4.2 prepare_data 预设

#### 4.2.1 概念说明

`scripts/prepare_data.py` 是一个「数据集格式归一化器」：你给它一个**预设名**（比如 `ultrachat`），它就去 Hugging Face 把对应数据集拉下来，逐行翻译成统一的 `id + conversations` JSONL 写到磁盘。它的核心价值是：**屏蔽各数据集五花八门的原始字段**（UltraChat 用 `messages`、ShareGPT 用 `conversations`+`from`/`value`、GSM8K 用 `question`/`answer`……），让你下游的训练流程永远只面对同一种 schema。

当前共 **19 个文本预设**，外加 2 个**明确不支持**的 VLM（视觉语言模型）数据集。

#### 4.2.2 核心流程

`prepare_data.py` 的主流程是一条干净的「加载 → 可选切分 → 写盘」流水线：

```text
parse_args(argv)
   │  校验：--data-path 只能配 sharegpt；--sample-size 必须 > 0
   ▼
load_dataset_preset(name, data_path, opc_subset)
   │  返回 (dataset, processor) 二元组
   │  processor 是「把一行原始数据翻译成 id+conversations」的函数
   ▼
（可选）dataset.select(range(sample_size))   # 截断行数
（可选）dataset.train_test_split(test_size=0.05, seed=42)  # 确定性 95/5 切分
   ▼
process_and_save_dataset(...)
   │  若 <name>_train.jsonl 已存在 → 直接跳过（幂等）
   │  否则逐行 processor(item) → json.dumps → 写一行
   ▼
默认输出 cache/dataset/<name>_train.jsonl
```

两个关键设计：

- **重依赖懒加载**：HuggingFace `datasets` 库只在 `_load_hf_dataset` 内部才 `import`（[scripts/prepare_data.py:L115-L118](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/prepare_data.py#L115-L118)）。这让脚本主体、行转换函数及其测试可以在没装 `datasets` 的轻量环境里也能 import，符合 SpecForge 「解析/校验阶段尽量轻」的一贯风格（与 u4-l2 的注册表懒加载同理）。
- **processor 解耦**：每个数据集有自己的 `process_<name>_row` 函数，签名统一为 `(row, dataset_name) -> (dict|None, skipped_count)`。返回 `None` 表示丢弃该行，`skipped_count` 累计被跳过的消息数。

#### 4.2.3 源码精读

先看全部 19 个预设的权威清单与 2 个不支持的 VLM 名字：

[scripts/prepare_data.py:L18-L39](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/prepare_data.py#L18-L39) — `SUPPORTED_DATASETS` 元组列出 19 个预设；`UNSUPPORTED_VLM_DATASETS` 明确 `sharegpt4v`、`allava4v` 不支持，文档也说明 VLM 数据准备与训练整体不支持。

这 19 个预设可归为两族（见 [docs/basic_usage/data_preparation.md:L12-L16](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/data_preparation.md#L12-L16)）：

| 族 | 预设 |
| --- | --- |
| 通用对话 | `ultrachat`、`sharegpt`、`eaglechat`、`perfectblend` 系列（含 4 个已再生的 llama 变体）、`magpie-qwen2.5-pro-1m-v0.1`、`nebius-llama31-8b-infinity-instruct` |
| 推理 / 数学 / 代码 | `opc`、`gsm8k`、`hendrycks_math`、`math_qa`、`codealpaca-20k`、`opencodeinstruct`、`magicoder-evol-instruct`、`sciq`、`camel` |

命令行参数与默认输出目录：

[scripts/prepare_data.py:L60-L97](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/prepare_data.py#L60-L97) — `build_parser` 定义 `--dataset`（必填、choices 锁定 19 个）、`--output-path`（默认 `cache/dataset`）、`--data-path`（自定义 ShareGPT 文件）、`--sample-size`（截断行数）、`--split-eval`（切 5% 验证集）、`--opc-subset`（opc 的子集）。

[scripts/prepare_data.py:L40-L41](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/prepare_data.py#L40-L41) — `DEFAULT_OUTPUT_DIRECTORY` 指向仓库根的 `cache/dataset`；`SUPPORTED_DATA_PATH_SUFFIXES` 限制自定义文件只能是 `.json` / `.jsonl`。

接下来看「行翻译」的几个典型实现。最基础的是 `_conversation_row`，单轮对话的几个数学/代码预设都复用它：

[scripts/prepare_data.py:L131-L142](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/prepare_data.py#L131-L142) — `_conversation_row` 接 `(row_id, user_content, assistant_content)`，输出标准的 `{"id", "conversations": [user, assistant]}` 两轮结构。这是统一契约的「原子构造器」。

[scripts/prepare_data.py:L127-L128](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/prepare_data.py#L127-L128) — `_stable_id` 用 `md5` 把内容哈希成稳定 id，给那些原始数据没有 `id` 字段的预设（如 gsm8k、opc）用。稳定意味着同一行内容每次跑出的 id 一致，便于断点续训与去重。

再看两个有多轮 / 多字段差异的预设：

[scripts/prepare_data.py:L145-L159](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/prepare_data.py#L145-L159) — `process_ultrachat_row` 直接复用原始的 `messages`（UltraChat 本来就是 `{role, content}` 格式），只过滤掉非 `user`/`assistant` 角色，`id` 取 `prompt_id`，是多轮对话的典型。

[scripts/prepare_data.py:L162-L178](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/prepare_data.py#L162-L178) — `process_sharegpt_row` 处理 ShareGPT 格式：用 `ROLE_MAPPING`（`human→user`、`gpt/chatgpt/bing/bard→assistant`）翻译 `from` 字段，遇到未知 `from` 则计入 `skipped_count` 并跳过该条消息（而不是整行丢弃）。

`ROLE_MAPPING` 的定义：

[scripts/prepare_data.py:L48-L54](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/prepare_data.py#L48-L54) — 把 ShareGPT 各种历史角色名统一映射到 SpecForge 内部只认的 `user` / `assistant`。

加载与路由的核心是 `load_dataset_preset`，它是一长串 `if name == ...`：

[scripts/prepare_data.py:L354-L390](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/prepare_data.py#L354-L390) — 函数头 + 前几个分支。注意几个模式：`eaglechat` 和 perfectblend 的再生变体用 `_identity_row`（数据本身就是对话格式、无需翻译，原样透传）；`perfectblend` / `magpie` 这类没 `id` 字段的用 `_indexed` 或 `rename_column` 先补 id 再复用 `process_sharegpt_row`。未命中任何分支则在末尾抛 `ValueError`（[L488-L490](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/prepare_data.py#L488-L490)）。

最后看写盘与主流程：

[scripts/prepare_data.py:L519-L554](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/prepare_data.py#L519-L554) — `process_and_save_dataset` 是**幂等**的：若 `<name>_train.jsonl` 已存在则打印跳过、直接返回（L529-L531）；否则逐行 `processor` → `json.dumps(ensure_ascii=False)` → 写一行；`--split-eval` 时额外写一份 `<name>_test.jsonl`。

[scripts/prepare_data.py:L574-L598](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/prepare_data.py#L574-L598) — `main`：加载 → `select` 截断 → `train_test_split(test_size=0.05, seed=42)` 确定性切分 → 保存。`seed=42` 保证同一份数据两次切出的 train/eval 完全一致。

#### 4.2.4 代码实践

**实践目标**：用 `prepare_data.py` 生成一份 ultrachat 训练 jsonl，并验证它符合对话格式契约。

**操作步骤**：

1. 先装好 data 扩展（regenerate 也用得上，u1-l2 已介绍 extras 概念）：

```bash
pip install -e '.[data]'
```

2. 在**仓库根目录**执行（默认输出到 `cache/dataset`）：

```bash
python scripts/prepare_data.py --dataset ultrachat --split-eval
```

3. 想先小规模试跑，加 `--sample-size` 截断：

```bash
python scripts/prepare_data.py --dataset ultrachat --sample-size 200 --output-path ./cache/dataset
```

**需要观察的现象**：
- 终端打印 `Saved ultrachat training data to .../ultrachat_train.jsonl.`。
- `cache/dataset/` 下出现 `ultrachat_train.jsonl`，且因加了 `--split-eval` 还会出现 `ultrachat_test.jsonl`。
- **再次运行同一条命令**：终端打印 `Dataset already exists at ...; skipping conversion.`，不会重复转换（验证幂等性）。

**预期结果**：取 `ultrachat_train.jsonl` 第一行用 `jq` 查看，应看到顶层有 `id`，`conversations` 是一个 `role` 在 `user`/`assistant` 间交替、每条都有非空 `content` 的列表——完全符合 4.1 的对话格式 schema。

> 待本地验证：`ultrachat` 实际拉取需要联网访问 Hugging Face；若网络受限，可改用本地 ShareGPT 文件 + `--dataset sharegpt --data-path ./raw.jsonl` 路线（见下文练习 2）。

#### 4.2.5 小练习与答案

**练习 1**：`--data-path` 为什么只能配合 `--dataset sharegpt` 使用？

**参考答案**：见 [scripts/prepare_data.py:L104-L108](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/prepare_data.py#L104-L108) 的校验。`--data-path` 用于读取**本地** ShareGPT 格式文件，而其他预设都是从 Hugging Face 拉取的固定数据集，不接受本地覆盖；`parse_args` 直接对非 sharegpt 的组合 `parser.error`。

**练习 2**：你手上有一份本地 ShareGPT 格式的 `raw.jsonl`，每行是 `{"id": ..., "conversations": [{"from":"human","value":...}, {"from":"gpt","value":...}]}`，怎么转成 SpecForge 的对话格式？

**参考答案**：

```bash
python scripts/prepare_data.py \
    --dataset sharegpt \
    --data-path ./raw.jsonl \
    --output-path ./cache/dataset
```

`process_sharegpt_row` 会用 `ROLE_MAPPING` 把 `human` 翻译成 `user`、`gpt` 翻译成 `assistant`，输出统一的 `id + conversations`（`role`/`content`）契约。

---

### 4.3 regenerate 流程

#### 4.3.1 概念说明

「数据再生」回答一个很直接的问题：**既然草稿模型是要去模仿目标模型，那为什么不直接拿目标模型生成的回复当训练数据？**

开源对话数据集（如 ShareGPT）里的 assistant 回复，是**别的模型**（甚至是人）写的，风格、用词、长度分布都和你的目标模型不一样。用这种数据训草稿模型，相当于让它学一个「和目标模型不同的分布」，接受率自然上不去。

再生的做法是：保留数据集里的 **user 提问**，把 **assistant 回复**全部丢给目标模型重新生成一遍。这样训练数据的分布就和目标模型的输出分布对齐了，接受率随之提升。

不过这里有个重要的工程取舍，官方文档（[docs/basic_usage/data_preparation.md:L55](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/data_preparation.md#L55)）讲得很直白：根据 EAGLE1 论文，EAGLE 方法对数据质量**不太敏感**，用原始数据效果也不错；**只有追求生产环境的最佳性能时**，才值得花算力做再生。所以再生是「锦上添花」，不是「必需」。

#### 4.3.2 核心流程

`regenerate_train_data.py` 的本质是一个**多线程的 OpenAI 兼容客户端**，它把目标模型所在的 SGLang 服务器当成 OpenAI API 来调用：

```text
启动阶段
  ├─ 校验 temperature ∈ [0,1]、max_tokens > 0
  ├─ （可选 --resume）从已有输出文件统计已处理行数，跳过前 N 行
  └─ 用一句 "Hello" 探活每个 --server-address，剔除不可用的服务器
逐行处理（线程池并发）
  ├─ validate_regen_input：校验 conversations 结构合法（user/assistant 交替）
  │     不合法 → 写入 *_skipped.jsonl
  ├─ 轮询（round-robin）选一个服务器，提交 call_sglang 任务
  └─ call_sglang 内部：
        · 跳过「以 assistant 开头」的数据
        · 遍历每条消息：system 原样保留、assistant 跳过、user 触发一次重新生成
        · 把目标模型的新回复接回去，继续多轮
        · 根据 --reasoning 决定是否保存 reasoning_content
收尾
  ├─ 成功 → *_regen.jsonl（带统计：最短/最长/平均上下文长度）
  ├─ 失败 → *_error.jsonl
  └─ 跳过 → *_skipped.jsonl
```

并发模型值得注意：线程数 = `--concurrency × 服务器数量`（[scripts/regenerate_train_data.py:L433-L435](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/regenerate_train_data.py#L433-L435)），且用 round-robin 在多个服务器间均匀分发请求（[L464-L465](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/regenerate_train_data.py#L464-L465)）。这意味着**加 GPU（起更多 SGLang server）就能线性加速再生**，这是文档反复推荐的扩容姿势。

#### 4.3.3 源码精读

脚本开头的 docstring 给出了最典型的两步用法：

[scripts/regenerate_train_data.py:L1-L31](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/regenerate_train_data.py#L1-L31) — 先用 `python -m sglang.launch_server` 起目标模型服务（可配 `--reasoning-parser qwen3` 等推理解析器），再用本脚本多地址并发再生。注意命令里 `--server-address` 接了 8 个地址、`--reasoning save`，这是生产扩容 + 推理模型的典型组合。

OpenAI 客户端也是懒加载，并提供清晰的安装提示：

[scripts/regenerate_train_data.py:L42-L48](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/regenerate_train_data.py#L42-L48) — `from openai import OpenAI` 放在 try/except 里，没装时把异常存起来，等真正要用时（`call_sglang` 里，[L254-L258](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/regenerate_train_data.py#L254-L258)）才抛出带 `pip install 'specforge[data]'` 提示的错误。和 `prepare_data.py` 的懒加载是同一套思路。

`--reasoning` 三种模式是理解推理模型再生的关键：

[scripts/regenerate_train_data.py:L89-L97](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/regenerate_train_data.py#L89-L97) — `none`（标准模型）、`save`（保存 `reasoning_content`，用于推理模型）、`disable`（通过 `extra_body` 关闭思考、不保存推理内容）。这三种模式在 `build_query_kwargs` 里被翻译成不同的请求体：

[scripts/regenerate_train_data.py:L236-L241](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/regenerate_train_data.py#L236-L241) — `disable` 时往请求塞 `chat_template_kwargs={"enable_thinking": False}`；`save` 时塞 `enable_thinking: True`。这正是文档里「给推理模型加 `--reasoning save`；想关思考加 `--reasoning disable`」对应的代码落点。

`call_sglang` 是再生的核心循环，它逐条消息处理一个对话：

[scripts/regenerate_train_data.py:L264-L278](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/regenerate_train_data.py#L264-L278) — 先拒绝「以 assistant 开头」的数据；然后遍历消息：`system` 原样保留、`assistant` 直接 `continue`（**这就是「丢弃原回复」的地方**）、`user` 则把它加入上下文并触发一次 `client.chat.completions.create` 重新生成。

[scripts/regenerate_train_data.py:L300-L328](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/regenerate_train_data.py#L300-L328) — `--reasoning save` 时，从响应里取 `reasoning_content`（先试属性，再回退到 `model_extra`），校验非空且无 `<think>` 残留后，挂到 assistant 消息上一起保存。

`<think>` 残留的检测由共享的 `conversation_validation.py` 完成：

[scripts/conversation_validation.py:L6-L8](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/conversation_validation.py#L6-L8) — `has_think_marker` 检查文本里是否混入了 `<think>` / `</think>` 标记。再生 `disable` 模式下若回复里还带思考标记，会被判为 `skipped`，避免污染训练数据。

[scripts/conversation_validation.py:L21-L55](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/conversation_validation.py#L21-L55) — `validate_conversation` 强制 `user`/`assistant` 严格交替（用 `expected_role` 翻转）、允许开头若干 `system`、内容必须是非空字符串。`error_style="regeneration"` 时给出面向再生场景的报错文案，被 `validate_regen_input` 调用（[regenerate_train_data.py:L56-L64](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/regenerate_train_data.py#L56-L64)）。

最后看 `--resume` 与探活两段工程化设计：

[scripts/regenerate_train_data.py:L365-L383](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/regenerate_train_data.py#L365-L383) — resume 模式下，统计输出文件已有的 `success + error + skipped` 行数，跳过输入文件前同样多的行，并以 `append` 模式打开输出文件（[L410](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/regenerate_train_data.py#L410)）。这让动辄几小时的大规模再生任务可以断点续跑。

[scripts/regenerate_train_data.py:L386-L403](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/regenerate_train_data.py#L386-L403) — 处理前先用一句 `"Hello, how are you?"` 对每个 server 探活，剔除不可用地址；若全部不可用则直接 `raise ValueError`，避免空跑。

#### 4.3.4 代码实践

**实践目标**：把 4.2 生成的（或任意一份）对话数据，用一个目标模型再生一遍，观察输出文件结构。

**操作步骤**：

1. 起一个目标模型的 SGLang 服务（示例用 Llama-3.1-8B-Instruct）：

```bash
python3 -m sglang.launch_server \
    --model-path meta-llama/Llama-3.1-8B-Instruct \
    --cuda-graph-max-bs 128 \
    --dtype bfloat16 \
    --mem-fraction-static 0.8 \
    --port 30000
```

2. 等 `curl --fail http://127.0.0.1:30000/health` 成功后，执行再生：

```bash
python scripts/regenerate_train_data.py \
    --model meta-llama/Llama-3.1-8B-Instruct \
    --concurrency 128 \
    --max-tokens 98304 \
    --server-address localhost:30000 \
    --temperature 0.8 \
    --input-file-path ./cache/dataset/ultrachat_train.jsonl \
    --output-file-path ./cache/dataset/ultrachat_train_regen.jsonl
```

**需要观察的现象**：
- 终端先打印 `Configuration:` 摘要，再打印 `Using 1 server addresses: ...`（探活通过）。
- 进度条 `Processing` 推进；结束时打印 `Context length statistics`（最短 / 最长 / 平均上下文长度）。
- `cache/dataset/` 下出现三个文件：`ultrachat_train_regen.jsonl`（成功）、`ultrachat_train_regen_error.jsonl`（失败）、`ultrachat_train_regen_skipped.jsonl`（结构非法 / reasoning 不符）。

**预期结果**：打开 `*_regen.jsonl`，对比原始 `ultrachat_train.jsonl`：`user` 内容**不变**，`assistant` 内容已被目标模型重新生成（风格、措辞会明显不同）。这就是「数据对齐到目标分布」的直观证据。

> 待本地验证：实际接受率提升要等 u6 训练完成后、用 u9-l4 的 benchmark 度量才能看到。本讲只验证「再生产物结构正确」。

#### 4.3.5 小练习与答案

**练习 1**：为什么线程池大小是 `concurrency × 服务器数量`，而不是固定 `concurrency`？

**参考答案**：见 [scripts/regenerate_train_data.py:L433-L435](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/regenerate_train_data.py#L433-L435)。`--concurrency` 定义的是**单个服务器**的并发请求数；当你加更多 SGLang server（更多 GPU）时，总并发应等比例扩大才能打满新算力。这样「加 GPU 即加速」几乎是线性的。

**练习 2**：再生时 `--reasoning save` 和 `--reasoning disable` 分别适合什么场景？

**参考答案**：`save` 适合**推理模型**（如带 thinking 的 Qwen3.6），它会把模型的 `reasoning_content` 一并存进数据，供后续训练推理类草稿；`disable` 适合「用推理模型但想关掉思考」的场景，它通过 `chat_template_kwargs.enable_thinking=False` 让模型直接给最终答案、不存推理内容，且会拒绝带 `<think>` 残留的回复（见 [L236-L241](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/regenerate_train_data.py#L236-L241) 与 [conversation_validation.py:L6-L8](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/conversation_validation.py#L6-L8)）。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「从开源数据集到对齐目标分布的训练数据」的完整准备：

1. **生成基础数据**：用 `prepare_data.py` 把 `sharegpt` 预设转成对话格式 JSONL（加 `--split-eval`）：

   ```bash
   python scripts/prepare_data.py --dataset sharegpt --split-eval
   ```

2. **（可选）再生对齐**：起一个目标模型 SGLang 服务，把上一步的 `sharegpt_train.jsonl` 用目标模型重写一遍，得到 `sharegpt_train_regen.jsonl`。

3. **手写一份对照样例**：在不运行脚本的前提下，根据 4.1 的 schema，手写一条与上述输出**同 schema** 的单轮对话样本（含 `id` 与 `conversations` 两个字段），追加到 `sharegpt_train.jsonl` 末尾，用 `jq` 验证它能被正确解析。

4. **衔接运行配置**：写一份最小 YAML 的 `data` 段，把 `train_data_path` 指向你的产物文件，并说明：若你手写的是预格式化文本格式，还需要补哪两个字段（答案：`is_preformatted: true` 和匹配的 `chat_template`）。

**验收标准**：能说清「原始数据集 → 统一 JSONL 契约 →（可选）再生对齐 → `data.train_data_path`」这条链路上每一步发生了什么、为什么这么做。

## 6. 本讲小结

- SpecForge 训练数据只有**两种 schema**：对话格式（`id` + `conversations`，消息带 `role`/`content`）和预格式化文本格式（`id` + `text`）；两者都必须有 `id`。
- **预格式化 ≠ 免模板**：`is_preformatted: true` 时 `chat_template` 仍必填，用途从「渲染」变成「定位 assistant 区间、切 loss mask」。
- `scripts/prepare_data.py` 用 **19 个预设**把各类开源数据集归一化成统一的 `id + conversations` JSONL，默认写 `cache/dataset/<name>_train.jsonl`，写盘幂等、`--split-eval` 用 `seed=42` 做确定性 95/5 切分。
- 行翻译靠每个数据集自己的 `process_<name>_row` 函数，重依赖（`datasets`、`openai`）一律懒加载，保证脚本主体在轻量环境也可用。
- **数据再生**保留 user、用目标模型重写 assistant，使训练数据对齐目标分布以提升接受率；它是「锦上添花」而非必需，靠多 SGLang server 并发线性加速，支持 `--resume` 断点续跑与 `--reasoning none/save/disable` 三种推理模式。
- 再生产物分三类文件（`_regen` / `_error` / `_skipped`），结构校验由共享的 `conversation_validation.py` 完成，强制 user/assistant 交替、内容非空、无 `<think>` 残留。

## 7. 下一步学习建议

本讲产出的是「原始文本数据」。接下来：

- **u5-l2 模板与预处理**：本讲反复提到的 `chat_template` 渲染、loss mask 构建、`is_preformatted` 的具体处理，都在 `specforge/data/` 包里，下一讲深入 `template.py` / `preprocessing.py` / `parse.py`。
- **u5-l3 离线特征生成**：如果你走 offline 路线，本讲的 JSONL 还要经 `scripts/prepare_hidden_states.py` 转成目标隐藏状态特征，那里会用到本讲数据的对话格式。
- 想立刻跑一次完整训练，可回到 **u2-l1**，把示例 YAML 的 `data.train_data_path` 指向本讲生成的文件直接启动。
