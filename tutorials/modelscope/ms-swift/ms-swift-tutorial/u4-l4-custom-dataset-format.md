# 自定义数据集格式

## 1. 本讲目标

本讲聚焦「如何把自己的业务数据喂给 ms-swift」。读完本讲，你应当能够：

- 写出一份符合 ms-swift **标准格式**的数据集（`messages` 列表 + 可选字段），并理解 `AutoPreprocessor` 自动识别的四种原始格式。
- 区分接入自定义数据集的**三条路径**：`--dataset <路径>`、`--custom_dataset_info <json>`、`--external_plugins <py>`，并知道何时用哪一条。
- 理解 `register_dataset_info` 把一份 JSON 清单注册进 `DATASET_MAPPING` 的内部流程。
- 用 `loss` / `loss_scale` 字段在**多轮对话**里精确控制「哪一轮 assistant 的回答参与训练、参与多少」。

## 2. 前置知识

本讲是 u4 数据集单元的第 4 篇，承接前 3 讲的认知，这里只做术语回顾：

- **`DATASET_MAPPING`**：全局「数据集名 → 元信息」注册表，由「导入即注册」填充（详见 u4-l1）。
- **`DatasetMeta`**：描述一个数据集「从哪来、有哪些子集、用哪个预处理器」的元信息。
- **预处理器（Preprocessor）**：把千奇百怪的原始列清洗成**统一 `messages` 结构**的中间层，`MessagesPreprocessor`/`AlpacaPreprocessor`/`ResponsePreprocessor` 是三大内置实现，`AutoPreprocessor` 会按列名自动选一个（详见 u4-l2）。
- **`messages` 结构**：一个对话样本的规范形态，形如 `[{"role": "system/user/assistant", "content": "...", "loss": ..., "loss_scale": ...}, ...]`，是 Template 体系唯一认识的输入。
- **`-100` 与 loss**：训练时只有 assistant 回答段的 label 被保留，非回答段被填成 `-100` 不参与 loss（详见 u3-l3）。

如果你对「数据如何从文件进门」还陌生，建议先读 u4-l1；对「预处理器如何清洗」陌生，先读 u4-l2。本讲关注点在**格式规范**与**注册机制**，不重复加载链路细节。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [swift/dataset/register.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/register.py) | `register_dataset` / `register_dataset_info` / `_preprocess_d_info`：把数据集写进 `DATASET_MAPPING` 的核心逻辑。 |
| [swift/dataset/dataset_meta.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/dataset_meta.py) | `DatasetMeta` / `SubsetDataset` 数据类，定义注册所需的全部字段。 |
| [swift/dataset/preprocessor/core.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py) | `AutoPreprocessor`、`MessagesPreprocessor`、`_check_messages`（含 `loss`/`loss_scale` 字段保留逻辑）。 |
| [swift/dataset/data/dataset_info.json](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/data/dataset_info.json) | 内置数据集清单，是写自定义清单的最佳范本。 |
| [swift/arguments/base_args/data_args.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/data_args.py) | `custom_dataset_info` 参数与 `_init_custom_dataset_info`，把 `--custom_dataset_info` 接进注册流程。 |
| [swift/arguments/base_args/base_args.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py) | `external_plugins` 参数与 `_import_external_plugins`，用 `.py` 文件做手动注册。 |
| [docs/source_en/Customization/Custom-dataset.md](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/docs/source_en/Customization/Custom-dataset.md) | 官方自定义数据集文档，列出全部格式与字段语义。 |
| [examples/custom/dataset.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/custom/dataset.py) | 手动注册数据集的官方示例。 |

## 4. 核心概念与源码讲解

### 4.1 自定义数据集格式规范

#### 4.1.1 概念说明

ms-swift 的数据格式遵循一个**「漏斗」原则**：上游允许形态各异，下游只认一种。

- **下游（标准格式）**：每个样本必须有一个 `messages` 字段，它是一个「对话段」列表，每段至少含 `role` 和 `content`。可选地携带 `rejected_response`（DPO 等）、`label`（KTO/分类）、`images/videos/audios`（多模态）、`tools`（Agent）、`objects`（grounding）、`channel`（channel loss）等附加列。
- **上游（四种原始格式）**：你自己手上很可能不是标准 `messages`，而是 alpaca、sharegpt、query-response 等历史格式。ms-swift 用 `AutoPreprocessor` 自动把它们统统清洗成标准 `messages`，因此你**不必手工转换**，直接 `--dataset <文件>` 即可。

这种设计的好处是：只要你的数据能落进这四种之一，就能零代码接入；只有在列名/结构更特殊时，才需要进阶到注册机制（4.2）。

#### 4.1.2 核心流程

`AutoPreprocessor` 的选型规则非常朴素——**按列名嗅探**：

1. 若含 `conversation`/`conversations`/`messages` 列 → `MessagesPreprocessor`（处理 messages 与 sharegpt 两种）。
2. 否则若同时含 `instruction` 与 `input` 列 → `AlpacaPreprocessor`。
3. 否则 → `ResponsePreprocessor`（query/response/history 老格式）。

选定后，预处理器会做「列名归一 → 角色名归一 → 拼成 messages → 清掉无关列」四步。最终所有样本的 `messages` 形态一致，再交给 u4-l3 的 `EncodePreprocessor` 编码成 token。

四种格式速查（节选自官方文档）：

| 格式 | 关键列 | 清洗后 |
| --- | --- | --- |
| **messages（标准）** | `messages: [{role, content}]` | 原样保留 |
| **sharegpt** | `conversation: [{human, assistant}]` + `system` | 角色名 `human→user`、`assistant` 不变 |
| **query-response** | `query`/`response`/`history`/`system` | `history` + 当前轮拼成 messages |
| **alpaca** | `instruction`/`input`/`output` | `instruction\ninput` 合并成 query |

#### 4.1.3 源码精读

先看 `AutoPreprocessor` 的选型逻辑——它在 `swift/dataset/preprocessor/core.py` 中只看 `dataset.features` 里有没有特定列名：

[swift/dataset/preprocessor/core.py:552-559](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L552-L559) — `AutoPreprocessor._get_preprocessor` 按列名嗅探选出三大预处理器之一：含 `conversation/conversations/messages` 走 `MessagesPreprocessor`，含 `instruction+input` 走 `AlpacaPreprocessor`，其余走 `ResponsePreprocessor`。

再看「下游允许携带哪些列」的白名单——`standard_keys`：

[swift/dataset/preprocessor/core.py:28-40](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L28-L40) — `RowPreprocessor.standard_keys` 列出所有「合法标准列」，加载收尾时 `remove_useless_columns` 只保留这些列，其余被丢弃；注意 `label`、`channel`、`rejected_response`、`chat_template_kwargs` 都在白名单里。

最关键的是 `_check_messages`——它规定了一条 message 内**只允许 4 个键**：

[swift/dataset/preprocessor/core.py:66-82](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L66-L82) — `_check_messages` 逐条 message 把 `{'role','content','loss','loss_scale'}` 之外的键全部 `pop` 掉，并断言 `role` 必须是 `system/user/tool_call/tool_response/tool/assistant` 之一、`content` 不能为 `None`。这就是「标准 messages」的硬约束，也是 `loss`/`loss_scale` 字段能合法存活的位置。

> 小贴士：`tool_response` 与 `tool` 是同义词（见 core.py 第 81 行注释），写哪个都行。

#### 4.1.4 代码实践

**目标**：亲手验证「四种原始格式 → 同一 messages 形态」的归一过程，不跑训练，只看清洗结果。

**步骤**：

1. 准备两份各 2 行的 jsonl，分别用 alpaca 和 sharegpt 格式：

   `alpaca.jsonl`：
   ```jsonl
   {"instruction": "把下面的句子翻译成英文", "input": "今天天气真好", "output": "The weather is nice today."}
   {"instruction": "1+1等于几？", "input": "", "output": "等于 2。"}
   ```

   `sharegpt.jsonl`：
   ```jsonl
   {"system": "你是有用的助手", "conversation": [{"human": "你好", "assistant": "你好！有什么可以帮你？"}]}
   ```

2. 写一段最小脚本（示例代码，非项目原文件），分别用对应预处理器跑一遍并打印 `messages`：
   ```python
   # 示例代码
   from datasets import load_dataset
   from swift.dataset import AlpacaPreprocessor, MessagesPreprocessor

   for path, pp in [('alpaca.jsonl', AlpacaPreprocessor()), ('sharegpt.jsonl', MessagesPreprocessor())]:
       ds = load_dataset('json', data_files=path, split='train')
       ds = pp(ds, strict=False)
       print(path, '->', ds[0]['messages'])
   ```

3. **需要观察的现象**：alpaca 的 `instruction`+`input` 被合并成单条 `user` 消息、`output` 变成 `assistant`；sharegpt 的 `human`→`user`、`assistant` 不变、`system` 被提到列表头部。

4. **预期结果**：两份异构数据清洗后都得到 `[{'role':..., 'content':...}, ...]` 同构结构，可被同一 template 消费。若报 `KeyError` 或字段丢失，多半是列名没命中嗅探规则——这正是 4.2 注册机制要解决的场景。

> 说明：本实践未在编写讲义时实际执行，输出形态为「待本地验证」；可参照官方文档 `Custom-dataset.md` 第 16–42 行给出的四种格式样例对照。

#### 4.1.5 小练习与答案

**练习 1**：如果你的 jsonl 只有一列 `text`（纯预训练语料），`AutoPreprocessor` 会选哪个预处理器？会出错吗？

> **答案**：会落到 `ResponsePreprocessor`（因为既无 `messages` 列也无 `instruction+input`）。而 `ResponsePreprocessor` 的 `response_keys` 包含 `'text'`/`'completion'`/`'content'`（见 core.py 第 378–379 行），所以 `text` 会被当作 `response`，进而被组装成单轮 `{'role':'assistant','content':<text>}`——这正是预训练数据期望的形态，不会出错。

**练习 2**：一条 message 里如果多塞了一个 `"score": 0.9` 字段，训练时会发生什么？

> **答案**：`_check_messages` 会把它 `pop` 掉（不在 `{role,content,loss,loss_scale}` 白名单内），不会报错也不会保留。若确实需要按样本传额外信息（如 GRPO 的 `solution`），需用 GRPO 的透传机制而非塞进 message。

---

### 4.2 register_dataset_info 注册自定义数据集

#### 4.2.1 概念说明

当 `AutoPreprocessor` 嗅探不出合适的列名，或你想给数据集起一个**短别名**（如 `swift/stsb`）方便复用时，就需要「注册」。ms-swift 提供三条由易到难的路径：

1. **`--dataset <路径>`（推荐）**：零注册，本地文件直接喂，靠 `AutoPreprocessor` 自动清洗。适合绝大多数新手。
2. **`--custom_dataset_info <json>`**：把数据集写进一份 JSON 清单（格式同内置 `dataset_info.json`），框架启动时自动注册。适合 pip 安装用户、想复用别名与列映射的场景。
3. **`--external_plugins <py>`**：写 Python 文件，用 `register_dataset(DatasetMeta(...))` 手动注册，可挂**自定义 Preprocessor**，灵活度最高。适合列结构完全非标的开发者。

路径 1、2 在底层都复用了路径 3——区别只是「清单驱动」还是「代码驱动」。本模块聚焦**路径 2** 的核心函数 `register_dataset_info`。

#### 4.2.2 核心流程

`register_dataset_info` 把一份 JSON 清单（一个 list）整体灌进 `DATASET_MAPPING`，流程是：

```
读 JSON 文件/字符串
   └─ 对每条 d_info：
        _preprocess_d_info(d_info)        # ① 摘出 columns、装配 preprocessor、解析 dataset_path、递归处理 subsets
        DatasetMeta(**d_info)             # ② 构造元信息对象
        register_dataset(dataset_meta)    # ③ 写入 DATASET_MAPPING（默认禁止重名）
```

其中 `_preprocess_d_info` 有三个关键动作值得记：

- **摘 `columns`**：用 `pop` 取出列映射，单独传给预处理器，不污染 `DatasetMeta` 字段。
- **选预处理器**：若该条 d_info 带 `messages` 键（注意：这里是**预处理器配置**，不是数据！），则用 `MessagesPreprocessor(**messages配置, columns=columns)`，否则用 `AutoPreprocessor(columns=columns)`。
- **解析 `dataset_path`**：若是相对路径，按清单文件所在目录 `base_dir` 拼成绝对路径并展开 `~`。
- **递归 `subsets`**：清单里的 `subsets` 若是 dict（带 `columns` 等），递归套同样处理，构造 `SubsetDataset`。

#### 4.2.3 源码精读

入口 `register_dataset_info` 支持「文件路径 / JSON 字符串 / None（用内置清单）」三种入参，自动判定后逐条注册：

[swift/dataset/register.py:84-115](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/register.py#L84-L115) — 当 `dataset_info is None` 时回退到内置 `swift/dataset/data/dataset_info.json`（第 92–93 行）；是文件则读盘并记下 `base_dir`（用于后续相对路径解析），是字符串则 `json.loads` 当作内联 JSON；最后循环调用 `_register_d_info`。

核心清洗 `_preprocess_d_info`——注意 `messages` 键的「双重身份」：

[swift/dataset/register.py:43-69](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/register.py#L43-L69) — 第 50–53 行：清单里若出现 `messages` 键，其值被当作 `MessagesPreprocessor` 的构造参数（如 `role_key`/`content_key`/`user_role`），而非数据；否则用 `AutoPreprocessor`。第 55–61 行把相对 `dataset_path` 解析成绝对路径。第 63–68 行把 dict 形态的 subset 递归处理成 `SubsetDataset`。

写入注册表 `register_dataset`——默认防重名：

[swift/dataset/register.py:26-40](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/register.py#L26-L40) — `dataset_name` 取自 `dataset_meta.dataset_name`，否则用 `(ms_dataset_id, hf_dataset_id, dataset_path)` 三元组（这正是 u4-l1 提到的 key 形态）；未设 `exist_ok` 且重名时抛 `ValueError`，防止覆盖。

而这一切在「导入 swift」时就已经自动发生一次——内置清单在包入口被注册：

[swift/dataset/__init__.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/__init__.py) — 末尾的 `register_dataset_info()`（无参）读取内置 `dataset_info.json`，把 150+ 内置数据集一次性灌进 `DATASET_MAPPING`，配合 `_LazyModule` 懒加载实现「导入即注册」。

CLI 侧的接线在 `DataArguments`：

[swift/arguments/base_args/data_args.py:100](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/data_args.py#L100) — `custom_dataset_info: List[str]` 字段，接收一个或多个 `.json` 路径。

[swift/arguments/base_args/data_args.py:102-107](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/data_args.py#L102-L107) — `_init_custom_dataset_info` 遍历每个路径调用 `register_dataset_info(path)`，把外部清单注册进来；该方法在 `__post_init__` 链路中被调用（见第 118 行）。

路径 3（手动注册）的接线在 `BaseArguments`：

[swift/arguments/base_args/base_args.py:95](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L95) — `external_plugins: List[str]` 字段，接收 `.py` 文件路径。

[swift/arguments/base_args/base_args.py:142-155](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L142-L155) — `_import_external_plugins` 用 `import_external_file` 把每个 `.py` 导入执行，文件里的 `register_dataset(...)` 由此生效；第 148–149 行兼容旧参数 `custom_register_path`。

官方手动注册示例（自定义预处理器）：

[examples/custom/dataset.py:20-25](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/custom/dataset.py#L20-L25) — 用 `DatasetMeta(ms_dataset_id='swift/stsb', preprocess_func=CustomPreprocessor())` 注册一个带自定义 Prompt 模板的相似度数据集；`CustomPreprocessor` 继承 `ResponsePreprocessor`，把 `text1/text2/label` 三列拼成 `query/response`（见同文件第 7–17 行）。

#### 4.2.4 代码实践

**目标**：用 `--custom_dataset_info` 把本地数据集注册成带别名的「内置数据集」，再用别名训练。

**步骤**：

1. 准备数据 `my_data.jsonl`（messages 标准格式）：
   ```jsonl
   {"messages":[{"role":"user","content":"你好"},{"role":"assistant","content":"你好，我是助手。"}]}
   ```

2. 写清单 `my_info.json`（与内置 `dataset_info.json` 同构）：
   ```json
   [{"dataset_name": "mychat", "dataset_path": "/abs/path/to/my_data.jsonl"}]
   ```
   注意 `dataset_name` 给出短别名 `mychat`；`dataset_path` 推荐写绝对路径。

3. 训练命令（示例命令，待本地验证）：
   ```bash
   CUDA_VISIBLE_DEVICES=0 swift sft \
       --custom_dataset_info my_info.json \
       --model Qwen/Qwen3-4B --tuner_type lora \
       --dataset mychat --max_length 1024 --max_steps 5
   ```

4. **需要观察的现象**：日志出现 `Successfully registered ...`；后续可用 `--dataset mychat` 反复引用而无需再写长路径。
5. **预期结果**：训练正常启动并读到 `my_data.jsonl` 的样本。若报 `The ... has already been registered`，说明别名冲突，换一个 `dataset_name` 或在代码里用 `register_dataset(meta, exist_ok=True)`。

> 说明：本实践未在编写讲义时执行，命令效果为「待本地验证」。可对照 `examples/custom/sft.sh`（用 `--external_plugins examples/custom/dataset.py --dataset swift/stsb`）理解路径 3 的用法。

#### 4.2.5 小练习与答案

**练习 1**：清单里某条 d_info 同时写了 `"messages": {...}` 和数据列，会发生什么？

> **答案**：这里的 `messages` 不是数据，而是 `MessagesPreprocessor` 的构造参数（register.py 第 50–51 行），用于覆盖默认的 `role_key`/`content_key`/角色名映射。真正作为「列名」的 `messages` 由 `AutoPreprocessor`/`MessagesPreprocessor` 在加载数据时嗅探。二者同名但语义不同，是初学者最易混淆的点。

**练习 2**：为什么 `register_dataset` 默认禁止重名？想覆盖旧注册该怎么办？

> **答案**：防止同名数据集被意外覆盖造成「训练用的数据和我以为的不一样」。需要覆盖时传 `exist_ok=True`（register.py 第 26、37 行），它会在重名时静默更新而非报错。

**练习 3**：`--custom_dataset_info` 与 `--external_plugins` 的根本区别是什么？

> **答案**：前者是**数据驱动**（JSON 清单只能配置列映射与 `MessagesPreprocessor` 参数，预处理器能力受限）；后者是**代码驱动**（Python 文件可挂任意自定义 `Preprocessor` 子类，能处理完全非标的列结构与 Prompt 模板，如 `examples/custom/dataset.py`）。前者简单、后者灵活。

---

### 4.3 loss 字段与多轮对话数据组织

#### 4.3.1 概念说明

多轮对话数据天然是一长串 `system → user → assistant → user → assistant → ...`。但训练时我们**只想在 assistant 的回答上算 loss**，user 的话不该让模型学着去「复述」。ms-swift 用两个**样本级字段**把这种控制权交还给数据本身：

- **`loss`（布尔）**：写在某条 assistant message 上。`true` 表示该回答参与 loss，`false` 表示不参与。默认 `None`（按命令行 `--loss_scale` 策略走）。**仅对 `role=assistant` 生效**，优先级高于 `--loss_scale` 的基础策略（`default`/`last_round`/`all`）。
- **`loss_scale`（浮点，ms-swift ≥ 4.2.0）**：给某条 assistant message 一个**权重**，控制它对 loss 的贡献比例。优先级高于 `--loss_scale` 的其它策略组件（如 `ignore_empty_think`/`hermes`）。若出现大于 1 的值，需额外传 `--is_binary_loss_scale false`。

这两个字段让你能「逐轮、逐段」控制训练目标——比如让模型学多轮推理的「中间思考」权重低于「最终答案」，或故意让某一轮不学（数据有噪音）。

#### 4.3.2 核心流程

一条带 `loss`/`loss_scale` 的多轮样本，从数据到 token 的旅程是：

1. **数据落盘**：jsonl 里每条 assistant message 可选带 `"loss": false` 或 `"loss_scale": 2.0`。
2. **预处理器放行**：`_check_messages` 只保留 `{role, content, loss, loss_scale}` 四键，所以这俩字段能穿过清洗存活下来（见 4.1.3）。
3. **Template encode**：Template 把对话切成上下文段，按 `ContextType` 给每段一个基础 `loss_scale`（RESPONSE/SUFFIX 为 1，OTHER 为 0，详见 u3-l3）；遇到 message 上的 `loss`/`loss_scale` 字段时，**字段优先级盖过基础策略**。
4. **算 loss**：被盖成 0 的段其 label 等效于 `-100`，不参与交叉熵；`loss_scale` 作为逐 token 权重乘进 loss。

字段与命令行策略的优先级关系（综合官方文档第 64–65 行）：

```
message.loss          >  --loss_scale 基础策略(default/last_round/all)
message.loss_scale    >  --loss_scale 其它策略组件(ignore_empty_think/hermes/...)
```

注意：`loss` 只压制「基础策略」，不影响 `ignore_empty_think` 等组件；`loss_scale` 则压制「其它策略组件」。两者作用层次不同，可叠加。

如果用权重，逐 token 的损失可简化理解为：

\[
\mathcal{L} = \frac{\sum_{t} s_t \cdot \mathbb{1}[y_t \neq -100] \cdot \mathrm{CE}_t}{\sum_{t} s_t \cdot \mathbb{1}[y_t \neq -100]}
\]

其中 \(s_t\) 是 token \(t\) 所在回答段的 `loss_scale`（由字段或策略决定），\(\mathbb{1}[y_t \neq -100]\) 排除非回答段。

#### 4.3.3 源码精读

字段能存活的根本原因——`_check_messages` 的白名单只放行这四个键：

[swift/dataset/preprocessor/core.py:73-76](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L73-L76) — `keys = set(message.keys()) - {'role','content','loss','loss_scale'}`，多余键被逐个 `pop`；`loss` 与 `loss_scale` 得以保留并随 messages 流向 Template。

Arrow 落盘时的特征声明也专门为这俩字段留了位（兼容 datasets < 4.0 分支）：

[swift/dataset/preprocessor/core.py:273-278](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L273-L278) — `messages_feature_with_loss` 比 `messages_feature` 多出 `loss: bool` 与 `loss_scale: float64` 两个子列，确保带权重的 messages 能正确序列化到 Arrow。

多轮组织本身由 `MessagesPreprocessor` 保证角色交替与 system 提前：

[swift/dataset/preprocessor/core.py:481-492](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/preprocessor/core.py#L481-L492) — `sharegpt_to_messages` 把 `{human, assistant}` 对拆成交替的 user/assistant 段，并把 system 插到列表头部；`to_std_messages`（第 494–508 行）则做角色名归一（`human→user`、`gpt→assistant`、`function_call→tool_call` 等）。多轮的「轮次」就是这些交替段的个数。

官方文档对 `loss`/`loss_scale` 语义与优先级的权威说明：

[docs/source_en/Customization/Custom-dataset.md:64-70](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/docs/source_en/Customization/Custom-dataset.md#L64-L70) — 明确：`loss` 仅对 assistant 生效且优先级高于基础策略；`loss_scale`（≥4.2.0）优先级高于其它策略组件，出现 >1 的值需 `--is_binary_loss_scale false`。

文档还给出一个可直接运行的调试片段，把字段效果「显形」：

[docs/source_en/Customization/Custom-dataset.md:74-92](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/docs/source_en/Customization/Custom-dataset.md#L74-L92) — 用 `get_template(get_processor('Qwen/Qwen3-8B'), loss_scale='default+ignore_empty_think', is_binary_loss_scale=False)` 编码带 `loss`/`loss_scale` 的样本，打印 `labels` 与 `loss_scale` 张量，即可看到被关掉的段在 `labels` 里被替换、权重段在 `loss_scale` 里被放大。

> 细节（文档第 94–97 行）：若在连续多条 `tool_call` 上都设了 `loss`，**只有第一条 `tool_call` 的配置生效**，这是 tool_call 合并语义导致的。

#### 4.3.4 代码实践

**目标**：用官方调试片段亲眼看到 `loss`/`loss_scale` 如何改变 labels 与 loss_scale 张量。

**步骤**：

1. 把文档第 74–92 行的脚本原样存为 `debug_loss.py`（示例代码，来自官方文档）：
   ```python
   from swift import get_processor, get_template
   data = {"messages": [
       {"role": "user", "content": "hello!"},
       {"role": "assistant", "content": "<think>\n...\n</think>\n", "loss_scale": 1.},
       {"role": "assistant", "content": "hi!", "loss_scale": 2.},
       {"role": "user", "content": "1+1=?"},
       {"role": "assistant", "content": "<think>\n...\n</think>\n1+1=3", "loss": False},
   ]}
   template = get_template(get_processor('Qwen/Qwen3-8B'),
                           loss_scale='default+ignore_empty_think', is_binary_loss_scale=False)
   template.set_mode('train')
   inputs = template.encode(data)
   print(template.safe_decode(inputs['labels']))
   print(inputs['loss_scale'])
   ```

2. 运行 `python debug_loss.py`（需先安装好 ms-swift 并能拉到 Qwen3-8B 的 processor）。

3. **需要观察的现象**：
   - `labels` 里 `"loss": False` 的最后一段回答被替换成占位（等效 `-100`，不参与 loss）。
   - `loss_scale` 张量里 `loss_scale=2.0` 的那段权重明显高于 `1.0` 的段。
   - 即便 `loss_scale='default+ignore_empty_think'`，`ignore_empty_think` 仍对空 `<think>` 生效，说明 `loss` 字段只压制 `default` 不压制组件。

4. **预期结果**：直观看到「字段优先级 > 命令行策略」。若 `loss_scale` 出现 >1 报错，确认是否漏了 `is_binary_loss_scale=False`。
5. 无法本地拉模型时，标注「待本地验证」，可改为只读源码：在 `swift/template/base.py` 的 `_swift_encode` 处下断点，观察 `ContextType` 与逐段 `loss_scale` 的赋值。

#### 4.3.5 小练习与答案

**练习 1**：我想让模型只学多轮对话的「最后一轮回答」，前面的回答都不学。有几种写法？

> **答案**：至少两种。①命令行 `--loss_scale last_round`（不用改数据）；②数据里给前面所有 assistant message 加 `"loss": false`，只留最后一轮不设或设 `true`。后者更细粒度，可逐轮控制。

**练习 2**：给某段设 `"loss_scale": 3.0` 但忘了开 `--is_binary_loss_scale false`，会发生什么？

> **答案**：默认 `is_binary_loss_scale=True` 会把 loss_scale 当作 0/1 二值掩码处理，>1 的值会被截断/归一，权重意图失效。必须显式 `--is_binary_loss_scale false` 才能让 3.0 真正作为权重（文档第 65 行）。

**练习 3**：为什么 `loss` 字段对 `role=user` 的消息无效？

> **答案**：user 段本就不该参与 loss，Template 在 encode 时已按 `ContextType` 把 user/system 段的基础 `loss_scale` 设为 0（详见 u3-l3）。`loss` 字段的设计语义是「在原本会算 loss 的 assistant 段里做开关」，所以只对 assistant 有意义（文档第 64 行明确「only takes effect for parts where role is assistant」）。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「业务数据 → 注册 → 带权多轮训练」的完整闭环。

**任务**：假设你有一份客服多轮对话日志，希望微调一个模型，且要求「最终解决客户的回答权重最高、中间澄清问句不学」。

**步骤**：

1. **整理数据**（4.1 格式规范）。把日志写成 `service.jsonl`，用标准 messages 格式，并在关键轮次打字段：
   ```jsonl
   {"messages":[
     {"role":"system","content":"你是耐心客服"},
     {"role":"user","content":"我订单没到"},
     {"role":"assistant","content":"请问您的订单号是？","loss":false},
     {"role":"user","content":"12345"},
     {"role":"assistant","content":"已为您加急，预计明天送达。","loss_scale":2.0}
   ]}
   ```
   （澄清问句 `loss:false` 不学；最终解决回答 `loss_scale:2.0` 加权。）

2. **注册成内置数据集**（4.2）。写 `service_info.json`：
   ```json
   [{"dataset_name":"myservice","dataset_path":"/abs/path/to/service.jsonl"}]
   ```

3. **训练**（4.3）。由于出现 `loss_scale=2.0>1`，必须关掉二值化：
   ```bash
   CUDA_VISIBLE_DEVICES=0 swift sft \
       --custom_dataset_info service_info.json \
       --model Qwen/Qwen3-4B --tuner_type lora \
       --dataset myservice --max_length 2048 --max_steps 20 \
       --loss_scale default --is_binary_loss_scale false
   ```

4. **验证字段生效**：训练前先用 4.3.4 的调试脚本对 `service.jsonl` 第一条 encode，确认 `loss:false` 段在 labels 中消失、`loss_scale:2.0` 段权重翻倍。

5. **反思**：如果把 `--loss_scale default` 换成 `last_round`，`loss:false` 还生效吗？（答：生效，`loss` 字段优先级高于基础策略，`last_round` 属基础策略。）

> 说明：本综合实践需本地 GPU 与可拉的 processor，命令效果为「待本地验证」。重点是理解三模块如何协作：格式规范保证数据被正确解析，注册机制提供别名与复用，loss 字段提供逐轮训练控制。

## 6. 本讲小结

- ms-swift 的**标准格式**是 `messages` 列表，可选携带 `label`/`rejected_response`/`images`/`tools`/`objects`/`channel` 等附加列；`AutoPreprocessor` 按列名自动把 messages/sharegpt/query-response/alpaca 四种原始格式清洗成标准形态。
- `_check_messages` 规定一条 message **只允许 `{role, content, loss, loss_scale}` 四键**，其余被丢弃——这是 `loss`/`loss_scale` 字段能合法存活的位置。
- 接入自定义数据集有**三条路径**：`--dataset <路径>`（零注册）、`--custom_dataset_info <json>`（清单注册）、`--external_plugins <py>`（代码注册，可挂自定义预处理器）。
- `register_dataset_info` 读 JSON 清单，经 `_preprocess_d_info`（摘 columns、选预处理器、解析路径、递归 subsets）→ `DatasetMeta` → `register_dataset` 三步写入 `DATASET_MAPPING`；内置清单在包导入时自动注册一次。
- 多轮对话靠 `MessagesPreprocessor` 保证角色交替与 system 提前；`loss`（布尔，仅 assistant）与 `loss_scale`（浮点权重）提供**逐轮、逐段**训练控制，字段优先级高于命令行 `--loss_scale` 策略。
- 用 `loss_scale>1` 必须配 `--is_binary_loss_scale false`；`loss` 只压制基础策略、不压制 `ignore_empty_think` 等组件。

## 7. 下一步学习建议

- 想看「清洗后的 messages 如何编码成 token、如何 packing」→ 读 **u4-l3 编码与 Packing 机制**，本讲的 `loss` 字段会在那里变成逐 token 的 `loss_scale` 张量。
- 想看「`--loss_scale` 策略（default/last_round/all/hermes/ignore_empty_think）底层如何实现」→ 读 **u10-l1 自定义 Loss 与 Loss Scale**，本讲的字段优先级在那里有完整源码对应。
- 想用 DPO/GRPO 等对齐方法 → 关注 `rejected_response`/`label` 字段在 **u7-l1 RLHF 训练流程** 与 **u7-l2 GRPO 算法核心** 中的消费方式（GRPO 会透传 `solution` 等额外字段给 ORM）。
- 想注册完全非标数据 → 精读 [examples/custom/dataset.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/custom/dataset.py) 与 `swift/dataset/preprocessor/core.py` 的 `RowPreprocessor` 基类，自己继承一个预处理器。
