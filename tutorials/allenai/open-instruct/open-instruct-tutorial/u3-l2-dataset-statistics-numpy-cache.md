# u3-l2 数据统计、变换与 numpy 缓存

## 1. 本讲目标

学完本讲，你应当能够：

1. 用 `scripts/data/get_statistics.py` 与 `open_instruct/dataset_statistics.py` 统计并解读一份训练数据的 token 长度分布与可训练 token 比例。
2. 说清 `dataset_transformation.py` 的变换流水线：`TRANSFORM_FNS` 注册表如何把 messages 变成 `input_ids` / `labels` / `attention_mask`，`get_dataset_v1` 如何按函数名依次执行 map/filter。
3. 理解两级数据缓存（HF hub 仓库 / 本地目录）的键如何由 `compute_config_hash` 与 `DATASET_CACHE_VERSION` 决定，以及 numpy 预编码缓存（OLMo-core mmap 格式）的生成与复用条件。
4. 掌握本轮更新（PR #2300）：SFT/DPO 的 tulu 分词路径在渲染前用 `_normalize_tool_call_arguments` 把 JSON 字符串形式的 `tool_calls[].function.arguments` 解码成映射（`null` 读作 `{}`；解码结果不是对象、或无法回写为 UTF-8 JSON 的字符串原样保留），added-token 过滤同步读解码后的文本，并理解 `DATASET_CACHE_VERSION` 为何从 v9 升到 v10——缓存哈希感知不到代码行为变化，旧缓存会保留按字符串字面渲染的结果。

## 2. 前置知识

- **messages 统一数据格式**：上一讲（u3-l1）介绍了 `open_instruct` 的统一消息格式——每条样本的 `messages` 字段是 `{"role": ..., "content": ...}` 的列表。本讲处理的就是这份原始数据被「变成张量」之前的整条工程链路。
- **tokenizer 与 chat template**：chat template（Jinja2 模板）把 messages 渲染成模型实际看到的文本，tokenizer 再把文本切成 token id。不熟悉可先回看 u2-l3。
- **label 掩码**：训练时只对 assistant 回答计算损失，其余位置（prompt、系统消息等）的 label 被设为 `-100`（`MASKED_TOKEN_VALUE`），PyTorch 的交叉熵会忽略它们。
- **Arrow 列式存储**：HuggingFace `datasets` 底层用 Apache Arrow 存数据。Arrow 是强类型的：**一个 struct 字段的类型必须对所有行一致**。这正是本讲更新点（#2300）的起因——工具调用参数有的行是字符串、有的行是映射时，Arrow 存不下，只能统一存成 JSON 字符串。
- **缓存与哈希**：缓存用「配置的哈希值」当键。哈希只覆盖配置（数据集名、分词器、函数名、参数等），**不覆盖代码本身**——代码行为变了而配置没变时，必须手动升版本号让旧缓存失效。这是理解 v10 的关键。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `open_instruct/dataset_statistics.py` | 在已变换的数据集上用 Arrow 原生算子统计总 token 数与可训练 token 数，不写派生缓存 |
| `scripts/data/get_statistics.py` | 独立 CLI：对原始 jsonl / HF 数据集逐消息分角色统计长度分布（describe、阈值计数、最长样本） |
| `open_instruct/dataset_transformation.py` | 核心变换与缓存：chat template 注册表、tokenizer 装配、`TRANSFORM_FNS` 变换函数、`get_dataset_v1`、两级缓存、`DATASET_CACHE_VERSION` 与本轮新增的 `_normalize_tool_call_arguments` |
| `open_instruct/numpy_dataset_conversion.py` | 把变换后的混合数据集转成 OLMo-core 的 numpy mmap 布局（token_ids / labels_mask / 文档边界 / 统计报告） |
| `scripts/data/convert_sft_data_for_olmocore.py` | numpy 转换的命令行入口（内部调用 `convert_hf_to_numpy_sft`） |
| `open_instruct/test_dataset_statistics.py` | `count_tokens` 的单元测试（逻辑行计数、多批次、空值拒绝） |
| `open_instruct/test_dataset_transformation.py` | 变换与缓存的单元测试，含本轮新增的 `TestNormalizeToolCallArguments` |

## 4. 核心概念与源码讲解

### 4.1 token 统计：先看清数据长什么样

#### 4.1.1 概念说明

在启动一次昂贵的训练之前，你需要回答几个问题：对话平均多长？有多少样本超过 `max_seq_length`？assistant 回答占多少比例的 token？这些数字直接决定 `max_seq_length`、padding 策略与显存预算。

仓库里有**两层**统计工具，分工不同：

1. **变换前的统计**（`scripts/data/get_statistics.py`）：对原始 jsonl 或 HF 数据集逐消息分角色（user/assistant）数 token，产出 pandas `describe` 风格的分位数摘要。适合拿到一份新数据时做首检。
2. **变换后的统计**（`open_instruct/dataset_statistics.py` 的 `count_tokens`）：对已经 tokenized 的数据集统计总 token 数与可训练 token 数。它被缓存流程内部调用（见 4.3），用于在 `dataset_statistics.json` 里记录「这份缓存里到底有什么」。

#### 4.1.2 核心流程

`get_statistics.py` 的流程：

1. 判断输入是 jsonl 文件还是 HF 数据集 id（`"json" in data_path` 或 `repo_exists`）。
2. 取第一条样本探测格式：有 `messages` 字段走 messages 路径，有 `prompt` 字段走 prompt-completion 路径。
3. messages 路径：先 `map` 删掉 system 消息，再逐条对话逐消息分角色编码计数。
4. 产出：轮数/用户长度/助手长度/总长度的 `describe()` 摘要、超过 512/768/…/4096 阈值的样本数、最长 100 条样本的 id。
5. 可选写入 `--save_path` 指定的 JSON 文件。

`count_tokens` 的流程：按 1024 行一批迭代 Arrow 格式的数据集，用 `pyarrow.compute` 的 `list_value_length` 求和得总 token 数，用 `list_flatten` + `not_equal(masked_token_value)` + `fill_null` 求和得可训练 token 数。全程不调用 `Dataset.map`，因此**不会生成派生缓存文件**。

可训练比例的口径是：

\[ \text{trainable\%} = \frac{\text{label} \ne -100 \text{ 的 token 数}}{\text{总 token 数}} \times 100\% \]

#### 4.1.3 源码精读

`count_tokens` 用 Arrow 原生算子代替 Python 循环，这是它能在大数据集上快的原因；测试特意断言它不触发 `Dataset.map`：

- [open_instruct/dataset_statistics.py:L7-L24](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/dataset_statistics.py#L7-L24)——`count_tokens` 主体：`select_columns` 后切到 arrow 格式按批迭代；`list_value_length` 求每行长度再求和；`list_flatten` 把嵌套 label 摊平后与掩码值比较，`fill_null(..., True)` 保持与旧 Python 实现一致（null 标量按可训练计）。
- [open_instruct/test_dataset_statistics.py:L10-L33](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/test_dataset_statistics.py#L10-L33)——三个测试：逻辑行计数（含乱序 `select` 与空行）、多批次与空数据集、null 序列直接抛 `ValueError`。第一个测试用 `mock.patch.object(Dataset, "map", ...)` 证明没有派生数据集被创建。

原始数据侧的统计脚本：

- [scripts/data/get_statistics.py:L29-L96](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/data/get_statistics.py#L29-L96)——`get_statistics_for_messages_data`：删 system 消息（L41-45）、逐消息按角色编码计数（L51-64）、`np.argsort` 取最长 100 条（L66-70）、阈值计数（L78-83）。
- [scripts/data/get_statistics.py:L141-L183](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/data/get_statistics.py#L141-L183)——`__main__` 分发：按 `"json" in data_path` / `repo_exists` 区分本地文件与数据集 id，再按首样本字段选择 messages 或 prompt-completion 统计函数。

注意一个细节：`get_statistics.py` 统计的是**裸 content 的 token 数**，不含 chat template 加上的特殊标记与轮次分隔符，所以它的数字会略小于变换后的实际序列长度；选 `max_seq_length` 时要留出余量。

#### 4.1.4 代码实践

1. **实践目标**：对一份小 SFT 数据得到长度分布摘要，并解读它。
2. **操作步骤**：
   - 准备一个 jsonl 文件（每行 `{"id": ..., "messages": [...]}`，几十条即可，可从 `allenai/tulu-3-sft-mixture` 抽样导出）；
   - 运行：
     ```bash
     uv run python scripts/data/get_statistics.py \
         --data_path /path/to/small_sft.jsonl \
         --tokenizer philschmid/meta-llama-3-tokenizer \
         --save_path /tmp/small_sft_stats.json
     ```
3. **需要观察的现象**：终端打印的 JSON 里 `total_lengths_summary` 的 `mean` / `50%` / `max`，以及 `num_instances_with_total_length_gt_2048` 等阈值计数。
4. **预期结果**：得到一份分位数摘要；对照 `max` 与你打算用的 `max_seq_length`，估算会被截断的样本比例。具体数字取决于你的数据，**待本地验证**。
5. 若不想下载数据，可直接跑单元测试验证统计逻辑：`uv run pytest open_instruct/test_dataset_statistics.py`（不需要 GPU）。

#### 4.1.5 小练习与答案

**练习 1**：`count_tokens` 为什么要用 `fill_null(..., True)` 把 null 标量 label 当作「可训练」？

**答案**：注释里写明这是为了与最初的 Python 实现（`label != mask` 的逐元素比较，null 参与 Python 比较时结果为真）保持计数口径一致；同时总 token 数与可训练数都用 `or 0` 兜底空数据集。见 [open_instruct/dataset_statistics.py:L18-L23](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/dataset_statistics.py#L18-L23)。

**练习 2**：`get_statistics.py` 的 messages 路径为什么先删除 system 消息再统计？

**答案**：该脚本的设计口径是度量「用户问题 + 助手回答」的长度分布（`user_prompt_lengths` 与 `assistant_response_lengths`），system 提示不属于任何一类，删除后 `instance_lengths` 也只累计这两类；见 [scripts/data/get_statistics.py:L41-L64](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/data/get_statistics.py#L41-L64)。

### 4.2 数据集变换流水线：从 messages 到张量

#### 4.2.1 概念说明

`dataset_transformation.py` 是数据进入训练前的中枢：它把 u3-l1 讲的 messages 数据，按「变换函数列表」依次处理成 tokenized 数据集。它的模块 docstring 说明了为什么要把变换做成显式、可缓存的流水线——大型训练中每个节点各自 tokenizing 会浪费巨量算力（原文举例：405B SFT 上 32 节点 × 5 分钟 ≈ 21 小时 H100 时间被浪费），见 [open_instruct/dataset_transformation.py:L29-L44](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/dataset_transformation.py#L29-L44)。

两个核心抽象：

- **`TokenizerConfig`**：描述分词器怎么加载（模型名、revision、chat template 名、`get_tokenizer_fn` 版本等）。
- **`DatasetConfig`**：描述一份原始数据怎么来（本地 jsonl/parquet 或 HF 数据集、split、revision）与怎么采样（`update_range` 支持按比例或按条数，甚至上采样重复）。

#### 4.2.2 核心流程

`get_dataset_v1` 对单个 `DatasetConfig` 的执行流程：

1. 校验 `transform_fn` 与 `transform_fn_args` 等长。
2. 解析 `tc.tokenizer`（加载、设 chat template、必要时提升 reserved-slot token）。
3. `dataset.map` 给每行打上 `dataset_source` 列（记录来源，混洗后仍可追溯）。
4. 若存在 `env_config` 列，先做规范化 map。
5. 依序执行每个变换函数：从 `TRANSFORM_FNS` 查到 `(函数, "map"|"filter")`；map 用 `dataset.map`（并按 `target_columns` 删掉多余列），filter 用 `dataset.filter` 并记录删掉了多少行。
6. 每一步都计算一个**包含 `DATASET_CACHE_VERSION` 的自定义指纹**传给 `new_fingerprint`，让 HuggingFace 内部的 `.map()` 缓存随版本号失效。

`TRANSFORM_FNS` 注册表把字符串名字映射到函数与类型（map/filter），命令行只需传函数名列表，例如 SFT 默认链 `sft_tulu_tokenize_and_truncate_v1` → `sft_tulu_filter_v1`，以及可选的前置过滤 `sft_added_token_strings_filter_v2`。

#### 4.2.3 源码精读

- [open_instruct/dataset_transformation.py:L2394-L2409](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/dataset_transformation.py#L2394-L2409)——`TRANSFORM_FNS` 注册表：SFT 的三个 tokenize/enter/filter、added-token 过滤、偏好数据的 tokenize/filter、RLVR 的 tokenize/长度过滤。注意 `preference_tulu_tokenize_and_truncate_v1` 这个名字实际绑定到 `_v1_2` 实现（只训练最后一轮的版本）。
- [open_instruct/dataset_transformation.py:L2553-L2644](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/dataset_transformation.py#L2553-L2644)——`get_dataset_v1` 主体：打来源列（L2571-2575）、env_config 规范化（L2578-2587）、逐函数执行与过滤计数日志（L2591-2637）。
- [open_instruct/dataset_transformation.py:L2597-L2604](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/dataset_transformation.py#L2597-L2604)——指纹计算：`DATASET_CACHE_VERSION + fn_name + dataset._fingerprint + fn_args + tokenizer 配置 + chat template 哈希` 的 SHA256 前 16 位。版本号出现在指纹里，是代码行为变化时让 HF map 缓存失效的开关。
- [open_instruct/dataset_transformation.py:L2610-L2614](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/dataset_transformation.py#L2610-L2614)——tools 列的去留规则：SFT 分词函数消费 tools 列后不保留，其他变换保留，避免静默丢弃工具 schema。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：把命令行参数映射到 `TRANSFORM_FNS` 里的具体函数，理解一条 SFT 数据的变换链。
2. **操作步骤**：阅读 `TRANSFORM_FNS` 注册表与 `finetune.py` 默认的 `--dataset_transform_fn`（默认为 `sft_tulu_tokenize_and_truncate_v1` + `sft_tulu_filter_v1`），为每个函数写一句「输入什么、输出什么、删什么列」。
3. **需要观察的现象**：tokenize 是 map（新增 `input_ids`/`labels`/`attention_mask` 三列并删掉 `messages`），filter 只保留「至少有一个非 -100 label」的行。
4. **预期结果**：得到一张两行的变换链表格。可用 `uv run pytest open_instruct/test_dataset_transformation.py -k "sft"` 对照测试断言验证你的理解（测试会用仓库自带的迷你 tokenizer 与样本 jsonl，无需 GPU）。
5. 完整变换行为（含掩码推导细节）在 u2-l3 已讲，本讲不重复。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `get_dataset_v1` 要给数据集加 `dataset_source` 列，而返回前又有 `remove_dataset_source_field` 把它删掉？

**答案**：混合多份数据并打乱后，需要来源信息做按数据集的统计（见 4.3 的 `per_dataset_counts`）；统计完成后删除是为了不让这列元数据进入训练缓存。见 [open_instruct/dataset_transformation.py:L1091-L1099](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/dataset_transformation.py#L1091-L1099)。

**练习 2**：过滤函数（如 `sft_tulu_filter_v1`）执行的日志会告诉你什么？为什么有用？

**答案**：`get_dataset_v1` 在 filter 后记录「删掉 N 行 / 共 M 行（百分比）」（L2633-2637）。如果某个过滤把数据几乎删空，这里是最早能发现的地方——最终 `len(dataset) == 0` 时会抛「No examples left after transformation」，但日志能告诉你是哪一个函数删的。

### 4.3 缓存体系：配置哈希、两级缓存与 numpy 预编码

#### 4.3.1 概念说明

变换很贵（下载、渲染模板、分词），所以要缓存。仓库有两层缓存：

1. **tokenized 数据集缓存**：把变换后的混合数据集整个存起来。两种模式——`hf` 模式推到 HuggingFace 私有仓库 `<entity>/dataset-mix-cached`，以配置哈希为 revision；`local` 模式存到本地目录 `local_dataset_cache/<config_hash>/`。键都是 `compute_config_hash(dcs, tc)`。
2. **numpy 预编码缓存**：`numpy_dataset_conversion.py` 把 tokenized 数据集进一步写成 OLMo-core 直接 mmap 读取的 numpy 布局。OLMo-core 训练（`olmo_core_finetune.py`）不用 HuggingFace DataLoader，而是直接读这批 `.npy` 文件，多机训练时每个节点零分词开销。

**复用条件**：缓存的键是配置哈希，哈希的输入包括 `DATASET_CACHE_VERSION`、每个 `DatasetConfig` 的可序列化字段、`TokenizerConfig` 与 chat template 哈希。任何一项变化（包括手动升版本号）都会得到新哈希、完全复用不了旧缓存——这是有意的失效机制。

#### 4.3.2 核心流程

**缓存命中路径**（`LocalDatasetTransformationCache.load_or_transform_dataset`）：

1. 拼 `local_dataset_cache/<config_hash>` 路径，存在则 `Dataset.load_from_disk` 直读，并读取同目录的 `dataset_statistics.json`。
2. 不存在则逐 `DatasetConfig` 调 `get_dataset_v1` 变换，收集逐数据集统计（此时调 `count_tokens`），拼接、`save_to_disk`，同时写 `config.json`（完整配置快照）与 `dataset_statistics.json`。

**numpy 转换路径**（`convert_hf_to_numpy_sft`）：

1. 先保存 tokenizer 快照到输出目录（`tokenizer/`，含 chat template）。
2. 调 `get_cached_dataset_tulu_with_statistics` 拿 tokenized 混合数据集（复用第 1 层缓存），按 `shuffle_seed` 打乱。
3. 按词表大小选 token dtype（uint8/16/32/64）。
4. 逐批迭代：把每行的 `input_ids` 追加写进临时 tokens 文件、`labels != -100` 转成 uint8 掩码写进 labels 文件、`(start, end)` 边界写进 boundaries 文件（断点续跑的 commit log）。
5. 把临时文件切成 ≤1 GiB 的 `token_ids_part_XXXX.npy` 与 `labels_mask_part_XXXX.npy`，为每个 part 写 csv.gz 的文档边界元数据。
6. `_aggregate_stats` 从磁盘上的边界与掩码重算逐数据集统计，`write_dataset_statistics` 产出 `dataset_statistics.{json,txt}` 报告。

产出目录布局（模块 docstring 明确列出）：`token_ids_part_XXXX.npy`、`labels_mask_part_XXXX.npy`、`token_ids_part_XXXX.csv.gz`（文档边界）、`tokenizer/`、`dataset_statistics.{json,txt}`。

#### 4.3.3 源码精读

- [open_instruct/dataset_transformation.py:L2682-L2706](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/dataset_transformation.py#L2682-L2706)——`compute_config_hash`：组合 `cache_version`（即 `DATASET_CACHE_VERSION`）、数据集配置列表、tokenizer 配置、chat template 哈希，取 SHA256 前 10 位。docstring 点明版本号的作用就是变换逻辑显著变化时让旧缓存失效。
- [open_instruct/dataset_transformation.py:L2802-L2921](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/dataset_transformation.py#L2802-L2921)——`LocalDatasetTransformationCache`：命中即 `load_from_disk`（L2834-2850）；未命中则变换、统计（L2859-2891，其中 L2882-2888 调 `count_tokens`）、落盘三件套（L2908-2915）。
- [open_instruct/dataset_transformation.py:L3012-L3052](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/dataset_transformation.py#L3012-L3052)——`get_cached_dataset_tulu_with_statistics`：未传哈希则先 `load_dataset_configs` 再计算；按 `dataset_cache_mode` 选 local 或 HF 缓存实现。训练入口（`finetune.py`、`olmo_core_finetune.py`）最终都走到这里。
- [open_instruct/numpy_dataset_conversion.py:L1-L9](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/numpy_dataset_conversion.py#L1-L9)——输出布局的权威说明。
- [open_instruct/numpy_dataset_conversion.py:L38-L42](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/numpy_dataset_conversion.py#L38-L42)——`_select_token_dtype`：按词表大小选最窄的无符号整型，直接决定缓存文件体积。
- [open_instruct/numpy_dataset_conversion.py:L257-L310](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/numpy_dataset_conversion.py#L257-L310)——`convert_hf_to_numpy_sft` 开头：校验并保存 tokenizer（L292-293），随后 L298-310 调 `get_cached_dataset_tulu_with_statistics` 复用第一层缓存。
- [open_instruct/numpy_dataset_conversion.py:L379-L409](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/numpy_dataset_conversion.py#L379-L409)——token 收集主循环：`labels != -100` 转 uint8 掩码（L395）、`(start, end)` 边界写入（L399-401）、每批 `fflush + fsync`（L409），掉电后可按边界文件截断恢复。
- [open_instruct/numpy_dataset_conversion.py:L145-L183](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/numpy_dataset_conversion.py#L145-L183)——`_recover_partial_state`：把 boundaries 文件当 commit log，尾部不完整的写会被截掉，实现 `--resume` 断点续跑。
- [open_instruct/numpy_dataset_conversion.py:L498-L576](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/numpy_dataset_conversion.py#L498-L576)——`write_dataset_statistics`：合并变换前统计与磁盘重算的逐数据集统计，输出 JSON 与人类可读 txt 报告。

命令行入口的真实用法示例见 [scripts/data/convert_sft_data_for_olmocore.py:L22-L33](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/data/convert_sft_data_for_olmocore.py#L22-L33)。

#### 4.3.4 代码实践

1. **实践目标**：亲手生成一份 numpy 预编码缓存目录，核对产物布局，并对比「直接变换」与「缓存命中」的加载耗时。
2. **操作步骤**：
   ```bash
   uv run --script scripts/data/convert_sft_data_for_olmocore.py \
       --tokenizer_name_or_path allenai/OLMo-2-1124-7B \
       --dataset_mixer_list <你的小数据集路径.jsonl> 1.0 \
       --output_dir ./data/numpy-cache-demo \
       --chat_template_name olmo \
       --max_seq_length 4096
   ```
   （该脚本的依赖声明在文件头的 script 元数据里，`uv run --script` 会自建环境。）
3. **需要观察的现象**：
   - `./data/numpy-cache-demo/` 下出现 `token_ids_part_0000.npy`、`labels_mask_part_0000.npy`、`token_ids_part_0000.csv.gz`、`tokenizer/`、`dataset_statistics.json` 与 `.txt`；
   - `dataset_statistics.txt` 里每个数据集的 instances / tokens / trainable tokens / 平均长度；
   - 第二次运行时日志出现 `✅ Found cached dataset at ...local_dataset_cache/<hash>`（第一层缓存命中，跳过变换）。
4. **预期结果**：第二次运行的准备阶段耗时显著低于第一次（第一次含下载 + 渲染 + 分词）。具体倍数依赖数据规模与网络，**待本地验证**。
5. 若没有合适数据，可跑 `uv run pytest open_instruct/test_numpy_dataset_conversion.py` 观察转换逻辑的断言（小规模内存数据集，无 GPU 要求）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 numpy 转换要先把 token 写进临时 `.partial.bin` 文件，最后再切成 part 文件，而不是直接写 part 文件？

**答案**：切分点取决于实际写入的 token 总量与（`--row_aligned_parts` 时的）行边界；先收集再切分才能保证每个 part ≤1 GiB 且（行对齐模式下）没有一行跨两个文件。同时 boundaries 临时文件兼任 commit log，支撑 `--resume`。见 [open_instruct/numpy_dataset_conversion.py:L68-L106](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/numpy_dataset_conversion.py#L68-L106) 与 [L145-L183](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/numpy_dataset_conversion.py#L145-L183)。

**练习 2**：你只改了 `dataset_transformation.py` 里某个变换函数的内部实现，没有改任何命令行参数，缓存会失效吗？

**答案**：不会自动失效——`compute_config_hash` 与 map 指纹的输入都是配置而非代码。正因如此，仓库约定在变换逻辑显著变化时手动递增 `DATASET_CACHE_VERSION`（当前 v10），让所有旧缓存作废。这是 4.4 的主题。

**练习 3**：`_select_token_dtype` 对一个词表 100k 的 tokenizer 会选什么 dtype？

**答案**：`uint16`（最大 65535 ≥ 99999 > 255）。词表 ≤256 选 uint8，≤65536 选 uint16，以此类推；这决定 token 文件是 1 字节还是 2 字节/token。

### 4.4 工具调用参数解码与缓存版本 v10（本轮更新 #2300）

#### 4.4.1 概念说明

这是本轮代码变化（PR #2300，提交 2880a0939）落在本讲的核心更新。

**问题**：Arrow 无法存储「字段类型跨行变化」的 struct。工具调用数据里 `tool_calls[].function.arguments` 有的是 JSON 对象、有的是字符串，于是为 open-instruct SFT 制备的数据集（如 `allenai/simfc-thinking-qwen35`、`allenai/nemotron-sft-agentic-v2-tool-calling-oi`）统一把它**存成 JSON 字符串**。但 Olmo 3.5 的 chat template 用 `| items` 迭代 arguments，需要的是**映射**，收到字符串直接报错。

**解法**：`_normalize_tool_call_arguments` 在渲染前把 JSON 字符串解码成映射。关键设计是**宽容而非严格**：早期版本曾对无法解码的字符串直接抛错，但这会中断那些「忽略工具调用」或「原样打印字符串」的模板本来能正常渲染的行（SFT 和 DPO 都受影响）。最终版把解码不了的字符串原样留给模板处理。

**缓存为何升 v10**：解码发生在变换函数内部，配置（数据集、tokenizer、函数名、参数）完全没变，`compute_config_hash` 算出的哈希也不变——旧缓存里存的是按字符串字面渲染的结果，会被错误复用。把 `DATASET_CACHE_VERSION` 从 v9 升到 v10，所有哈希整体改变，强制重建。

#### 4.4.2 核心流程

`_normalize_tool_call_arguments(messages)` 对每条消息的判定表：

| 输入情形 | 处理 | 结果 |
| --- | --- | --- |
| 无 `tool_calls`（或非 dict 消息） | 原样引用 | 不变 |
| `arguments` 已是映射 | 原样引用 | 不变 |
| `arguments` 是字符串，`json.loads` 成功且为 dict，且能以 UTF-8 回写 | 替换为解码后的映射（浅拷贝该 call） | 变为映射 |
| `arguments` 是字符串 `"null"` | 解码为 `None`，按 `{}` 处理 | 变为空映射 |
| `arguments` 是字符串但解码失败（非法 JSON、超出 Python 整数位数上限、嵌套超过递归深度） | 原样保留字符串 | 不变 |
| `arguments` 解码成 dict 但含孤立代理字符（如 `"\ud800"`）、无法编码为 UTF-8 | 原样保留字符串 | 不变 |

三个工程细节：

1. **不改动数据集行本身**：发生变化的地方做浅拷贝（`{**call, "function": {...}}`），原始 row 不被 mutate。
2. **added-token 过滤同步读取解码后的文本**：`_non_content_texts` 先过 `_normalize_tool_call_arguments` 再 `json.dumps(tool_calls)`。修复前的漏洞是：一个 JSON 转义拼进字符串里的 added token（例如被转义后不再匹配原始字面量）能骗过读原始字符串的过滤器，随后渲染时却变成 token 本身。
3. **渲染差异的告诫**（docstring 明确写出）：把字符串参数原样打印的模板（Qwen 的 `is string` 分支）现在会渲染自己对解码映射的 `tojson`，空格与转义可能与数据集里的字符串不同；无条件 `tojson` 的模板则从「JSON 编码的字符串」变成渲染对象本身。这意味着**同样的数据在 v9 与 v10 缓存下的渲染文本可能不同**——这正是必须整体换缓存的原因。

**消费者边界**：

- 走 `_tokenize_row_or_mask_out` 的路径（`sft_tulu_tokenize_and_truncate_v1`、`sft_tulu_tokenize_without_truncation_v1`、`last_turn_tulu_tokenize_and_truncate_v1`，以及复用它们的偏好路径 `preference_tulu_tokenize_and_truncate_v1`/`_v1_2`）→ **解码**。即 SFT 与 DPO 的 tulu 分词器都受益。
- 旧路径 `sft_tokenize_v1`、`preference_tokenize_v1`、`rlvr_tokenize_*` → **按存储原样渲染**，行为不变。

#### 4.4.3 源码精读

- [open_instruct/dataset_transformation.py:L1106-L1113](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/dataset_transformation.py#L1106-L1113)——`DATASET_CACHE_VERSION = "v10"` 及逐版本注释：v7 tools 列入模板、v8 保留 GPT-2 预分词器、v9 显式 generation 区间推 label、**v10 渲染前解码 JSON 字符串工具调用参数**。
- [open_instruct/dataset_transformation.py:L1116-L1122](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/dataset_transformation.py#L1116-L1122)——`_serializes_as_utf8`：解码结果要能 `json.dumps(..., ensure_ascii=False)` 后编码为 UTF-8 才可用；孤立代理字符与超深嵌套在此被拒。
- [open_instruct/dataset_transformation.py:L1125-L1171](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/dataset_transformation.py#L1125-L1171)——`_normalize_tool_call_arguments` 主体：docstring 讲清 Arrow 约束、涉及的数据集、宽容策略与渲染差异告诫；L1157-1169 是判定核心——`json.loads` 捕获 `ValueError` 与 `RecursionError`，`None` 归一为 `{}`，仅当「解码为 dict 且可 UTF-8 回写」才替换。
- [open_instruct/dataset_transformation.py:L1899-L1903](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/dataset_transformation.py#L1899-L1903)——`_tokenize_row_or_mask_out` 中的调用点：`messages = _normalize_tool_call_arguments(messages)` 在进入 `_tokenize_tulu_sft_with_assistant_labels` 渲染之前。这就是「SFT 与 DPO 的 tulu 分词器」统一收口的位置。
- [open_instruct/dataset_transformation.py:L2073-L2089](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/dataset_transformation.py#L2073-L2089)——`_non_content_texts`：added-token 过滤的取文本逻辑，第一行就是 `_normalize_tool_call_arguments(row[sft_messages_key] or [])`，随后对 `tool_calls` 与 tools schema 各做一次 `json.dumps` 供字面量扫描。
- [open_instruct/dataset_transformation.py:L1317-L1329](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/dataset_transformation.py#L1317-L1329)——旧路径对照：`sft_tokenize_v1` 直接 `apply_chat_template(row[sft_messages_key])`，不经过解码，参数按存储原样渲染。
- [open_instruct/test_dataset_transformation.py:L396-L443](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/test_dataset_transformation.py#L396-L443)——`TestNormalizeToolCallArguments` 两个用例：① JSON 字符串变映射、`"{}"` 变空映射、已是映射的不动、原始行不被改动；② `"null"` 变 `{}`，而 `"{not json"`、`"[1, 2]"`、空串、5000 位整数、10 万层嵌套、孤立代理字符全部原样保留（`assertIs(out[0], row[0])`）。

#### 4.4.4 代码实践

1. **实践目标**：验证「模板拿到的 arguments 已是映射、无法解码的字符串未被改写」，并解释缓存版本号为何要升级。
2. **操作步骤**（纯 Python，示例代码，可在 `uv run python` 交互环境中执行）：

   ```python
   # 示例代码：直接调用 _normalize_tool_call_arguments 观察行为
   from open_instruct.dataset_transformation import _normalize_tool_call_arguments

   messages = [
       {
           "role": "assistant",
           "content": "",
           "tool_calls": [
               # ① JSON 字符串 → 应变为映射
               {"id": "c0", "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city": "Paris", "days": 5}'}},
               # ② null 字符串 → 应变为 {}
               {"id": "c1", "type": "function",
                "function": {"name": "ping", "arguments": "null"}},
               # ③ 无法解码 → 应原样保留
               {"id": "c2", "type": "function",
                "function": {"name": "bad", "arguments": "{not json"}},
           ],
       }
   ]
   out = _normalize_tool_call_arguments(messages)
   for call in out[0]["tool_calls"]:
       print(call["function"]["name"], "->", repr(call["function"]["arguments"]))
   print("原始行未被改动:", messages[0]["tool_calls"][0]["function"]["arguments"])
   ```

3. **需要观察的现象**：前两个 call 的 `arguments` 分别变为 `{'city': 'Paris', 'days': 5}` 与 `{}`；第三个仍是字符串 `'{not json'`；原始 `messages` 里第一个 call 的 arguments 仍是字符串。
4. **预期结果**：与上表一致；也可以直接跑仓库测试对照：`uv run pytest "open_instruct/test_dataset_transformation.py::TestNormalizeToolCallArguments" -v`（无需 GPU；测试还需要下载仓库自带的迷你 tokenizer 与样本文件，**待本地验证**）。
5. **思考题（对应缓存升级）**：假设某模板会把 arguments 原样 `tojson` 打印。同一份数据在 v9 缓存（字符串字面）与 v10 重新变换（解码后重新序列化）下渲染文本是否相同？——可能不同（空格、键序、转义），且 v9 缓存因哈希未变会被继续复用，所以必须靠版本号强制失效。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `"null"` 被解码为 `{}` 而不是保留为 `None` 或报错？

**答案**：工具调用 schema 约定 `arguments` 是「调用参数的对象」；`null` 表示没有参数，语义上就是空对象。模板的 `| items` 需要可迭代映射，`{}` 是既不丢信息又能渲染的选择。见 [open_instruct/dataset_transformation.py:L1163-L1164](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/dataset_transformation.py#L1163-L1164)。

**练习 2**：解码成功但含孤立代理字符 `"\ud800"` 的对象为什么被拒绝？

**答案**：模板渲染与 tokenizer 编码都要求合法的 UTF-8 文本；`ensure_ascii=False` 的 `json.dumps` 结果无法 `.encode("utf-8")`（抛 `UnicodeEncodeError`），在 `_serializes_as_utf8` 里被判定不可用，于是回退为保留原字符串，让按原样渲染它的模板继续工作。见 [open_instruct/dataset_transformation.py:L1116-L1122](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/dataset_transformation.py#L1116-L1122)。

**练习 3**：DPO 训练会从这个变化中受益吗？

**答案**：会。注册名 `preference_tulu_tokenize_and_truncate_v1` 绑定的实现内部调用 `sft_tulu_tokenize_and_truncate_v1`（对 chosen 与 rejected 各一次），后者经 `_sft_tulu_tokenize` → `_tokenize_row_or_mask_out`，在渲染前统一解码。见 [open_instruct/dataset_transformation.py:L2131-L2164](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/dataset_transformation.py#L2131-L2164)。

## 5. 综合实践

把本讲四个模块串成一条链：**统计 → 变换 → 缓存 → 验证解码**。

1. 准备一份小型 SFT jsonl（10–100 行），其中**手工混入至少一条带 `tool_calls` 的 assistant 消息**（arguments 分别为 JSON 字符串与 `null`）。
2. 用 `scripts/data/get_statistics.py` 统计长度分布，记录 `total_lengths_summary` 的均值与最大值，据此选定一个 `max_seq_length`（module 4.1）。
3. 用 `scripts/data/convert_sft_data_for_olmocore.py` 生成 numpy 缓存目录，检查 `dataset_statistics.txt` 的逐数据集统计与 `tokenizer/` 快照（module 4.2 + 4.3）。
4. 删掉 `local_dataset_cache/` 后重新运行同一命令，用 `time` 对比两次的准备阶段耗时，确认第二次命中第一层缓存（module 4.3）。
5. 用 4.4.4 的示例代码验证解码行为；再回答：如果你此时把 `open_instruct/dataset_transformation.py` 里的 `DATASET_CACHE_VERSION` 改回 `"v9"`，会发生什么？（提示：`compute_config_hash` 恢复旧值，`local_dataset_cache/` 里 v9 时代的旧目录（如果存在）会被重新命中，里面的渲染结果是解码前的。）
6. 全程无需 GPU；步骤 3 的 tokenizer 需要联网下载，其余步骤**待本地验证**。

## 6. 本讲小结

- 统计分两层：`get_statistics.py` 看原始数据的分角色长度分布；`count_tokens` 用 Arrow 原生算子在变换后的数据集上数总 token 与可训练 token，不产生派生缓存。
- 变换是「函数名列表」驱动的流水线：`TRANSFORM_FNS` 注册 map/filter，`get_dataset_v1` 依序执行并记录过滤计数，每步的 map 指纹里编入了 `DATASET_CACHE_VERSION`。
- 缓存的键是 `compute_config_hash`（版本号 + 数据集配置 + tokenizer 配置 + 模板哈希），有两种落盘模式（HF hub / 本地目录）；numpy 预编码再把它变成 OLMo-core 可 mmap 的 `token_ids` / `labels_mask` / 边界文件三件套，支持断点续跑。
- #2300 之后，SFT 与 DPO 的 tulu 分词路径在渲染前把 JSON 字符串形式的工具调用参数解码成映射（`null` → `{}`，解码失败或非 UTF-8 对象原样保留），added-token 过滤同步读解码后文本；旧的 `sft_tokenize_v1` / `preference_tokenize_v1` / `rlvr_tokenize_*` 路径行为不变。
- 缓存哈希看不到代码，所以行为变化靠 `DATASET_CACHE_VERSION` v9 → v10 手动失效——旧缓存里存的是字符串字面渲染的结果，必须作废重建。

## 7. 下一步学习建议

- 下一讲 u3-l3（数据制备脚本实战）会走进 `scripts/data/` 下的 gsm8k、gpqa 等脚本，看你刚才验证过的 messages + `tool_calls` + `ground_truth` 数据是怎么从公开数据集造出来的。
- 想看 numpy 缓存在训练侧如何被消费，读 u7-l1（`olmo_core_finetune.py`：numpy 预编码缓存是 OLMo-core SFT 的前置要求，缺缓存会直接报错）。
- 变换函数内部的掩码推导细节（offset mapping、generation 区间）属于 u2-l3 的内容；本讲的 `_tokenize_row_or_mask_out` 只是它的入口包装加上本轮新增的解码步骤。
- 顺带一提：`preference_tulu_tokenize_and_truncate_v1` 注册名绑定 `_v1_2` 实现这个「名字与函数不完全对应」的现象，是仓库为保持命令行兼容的常见做法，阅读 `TRANSFORM_FNS` 时留意。
