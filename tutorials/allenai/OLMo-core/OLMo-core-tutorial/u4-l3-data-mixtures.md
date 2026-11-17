# 数据混合：source_mixture 与预置 mixes

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 OLMo 官方随库分发的 `.txt` 数据配方文件（`data/mixes/` 目录），理解 `label,path` 格式、`{TOKENIZER}` 占位符与「路径重复 = 上采样」的约定。
2. 掌握 `SourceMixtureConfig` / `SourceMixtureList` / `SourceMixtureDatasetConfig` 三件套：如何用显式的 `target_ratio` 精确控制每个源在混合中的占比，以及 `build()` 如何把它物化成「每个文件取多少 token」的清单。
3. 掌握 composable 世界中的比例混合：`MixingInstanceSource` 如何借助 `calculate_sample_sizes` 与 `SamplingInstanceSource` 在最少丢弃的前提下匹配目标比例。
4. 为自己的两个 `.npy` 数据源编写等价的 mixture 配置，并构建 `ComposableDataLoader` 验证各源的实际占比。

## 2. 前置知识

- **混合（mix / mixture）**：大模型预训练几乎从不只用一个语料库，而是把网页、代码、论文、数学、指令数据等按比例掺在一起。比例本身就是超参数——OLMo 2 的配方里 starcoder（代码）约占一半，wiki 只占百分之几但要重复采样。
- **上采样（upsampling）与重复（repetition）**：小而有价值的语料（如 Wikipedia）常被「重复多遍」使用，即采样量超过其固有 token 数。重复上限（`max_repetition_ratio` / `max_repetition_factor`）用来防止过拟合某些源。
- **`.npy` 预分词数据**：复习 u4-l1——OLMo-core 的数据底座是无文件头的裸无符号整型数组，token 数可以直接用「文件字节数 ÷ 每元素字节数」算出，**不需要读文件内容**。
- **`global_batch_size` 以 token 计**：复习 u4-l1/u4-l2。一个训练 step 消费 `global_batch_size` 个 token，即 `global_batch_size / sequence_length` 个定长实例。
- **composable 三层抽象**：复习 u4-l2——`TokenSource`（一维 token 流）→ `InstanceSource`（定长训练样本）→ `ComposableDataLoader`。本讲的混合发生在 `InstanceSource` 这一层。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/olmo_core/data/mixes/__init__.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/mixes/__init__.py) | `DataMix` 枚举与 `.txt` 配方文件的解析逻辑 |
| [src/olmo_core/data/mixes/dolma17.txt](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/mixes/dolma17.txt) | OLMo 1 时代的 Dolma 配方（路径重复上采样的活例子） |
| [src/olmo_core/data/source_mixture.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/source_mixture.py) | `SourceMixture` 家族：显式 target_ratio 的精确混合 |
| [src/olmo_core/data/composable/mixing_instance_source.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/mixing_instance_source.py) | composable 世界的实例级混合源 |
| [src/olmo_core/data/composable/sampling_instance_source.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/sampling_instance_source.py) | 实例级采样源（混合的执行者） |
| [src/olmo_core/data/composable/utils.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/utils.py) | `calculate_sample_sizes`：混合配额的核心算法 |
| [src/olmo_core/data/numpy_dataset.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/numpy_dataset.py) | `NumpyFSLDatasetConfig.from_src_mix()`：混合结果的下游消费入口 |
| [src/test/data/source_mixture_test.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/data/source_mixture_test.py) | SourceMixture 的行为断言（实践的依据） |

## 4. 核心概念与源码讲解

OLMo-core 里有**两条做数据混合的路线**，本讲按「先看官方配方文件 → 再看精确配额路线 → 最后看 composable 组合子路线」的顺序拆解。

### 4.1 预置配方：`.txt` mix 文件与 `DataMix`

#### 4.1.1 概念说明

`src/olmo_core/data/mixes/` 目录下有 16 个 `.txt` 文件，每个对应一份 OLMo 官方训练用配方：预训练（`OLMo-mix-0625/0925`、`OLMoE-mix-0824`、`dolma17`）、中期训练（`OLMo-midtraining-mix-*`）、长上下文扩展（`OLMo-longmino-mix-*`）以及验证集（`c4-ppl-validation` 等）。

这些文件采用一种刻意朴素的格式：每行 `label,path`，`#` 开头是注释，空行跳过。它解决的问题是**把「用了哪些语料、每个语料哪些分片」以纯文本形式随库版本化**——改配方就是改一行文本，diff 一目了然。

两个关键约定：

1. **`{TOKENIZER}` 占位符**：同一路径模板可服务不同分词器，路径中的占位符在运行时替换为分词器目录名。
2. **比例靠路径重复隐式表达**：`.txt` 里没有 ratio 字段，某源想上采样就把它的行多写几遍。

#### 4.1.2 核心流程

`DataMix.build(base_dir, tokenizer)` 的执行过程：

1. 调用方传入数据根目录 `base_dir`（如 `s3://ai2-llm` 或 Weka 挂载点）和分词器标识。
2. 按分词器与 mix 名称做**名称映射**（例如 OLMo 3 使用的 mix 统一映射到 `allenai/dolma3-tokenizer` 目录）。
3. 通过 `importlib_resources` 在**安装好的包内**定位 `data/mixes/<名字>.txt`。
4. 逐行解析：跳过空行与 `#` 注释；按第一个逗号切出 `label` 和 `path`；要求路径中必须含 `{TOKENIZER}`，否则报错；替换占位符后拼上 `base_dir` 前缀。
5. 返回 `(paths, labels)` 两个平行列表——同名的 label 把多个文件归到同一个源。

#### 4.1.3 源码精读

`DataMix` 是一个 `StrEnum`，成员值就是 `.txt` 文件名（去掉 `.txt`），分为预训练、中期训练、长上下文、验证四组：

- [src/olmo_core/data/mixes/__init__.py:L33-L61](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/mixes/__init__.py#L33-L61) — 枚举了全部预置 mix，如 `OLMo_mix_0925 = "OLMo-mix-0925"`、`dolma17 = "dolma17"`。
- [src/olmo_core/data/mixes/__init__.py:L63-L80](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/mixes/__init__.py#L63-L80) — `_missing_` 钩子维护历史别名，如 `"dolma3-0625-6T-mix"` 会解析到 `OLMo-mix-0625`，保证旧配置不失效。

解析主体在 `build()`：

- [src/olmo_core/data/mixes/__init__.py:L82-L113](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/mixes/__init__.py#L82-L113) — 先做 tokenizer 名称映射：OLMo 3 系列的 mix 用 `dolma2` 分词器时统一改写为 `allenai/dolma3-tokenizer`（注释说明两者实为同一分词器、仅目录名不同）。
- [src/olmo_core/data/mixes/__init__.py:L114-L128](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/mixes/__init__.py#L114-L128) — 逐行解析的核心循环：`label, path = line.split(",")`；`{TOKENIZER}` 缺失直接 `ValueError`；最后拼接 `base_dir` 前缀。
- [src/olmo_core/data/mixes/__init__.py:L131-L143](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/mixes/__init__.py#L131-L143) — 用 `importlib_resources.files()` 定位包内资源，这样无论包是源码安装还是打包成 wheel，都能找到配方文件。

「路径重复 = 上采样」的活例子在 dolma17 配方里——wiki 的两个分片被原样写了两遍（第二遍是 L36-L37）：

- [src/olmo_core/data/mixes/dolma17.txt:L33-L37](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/mixes/dolma17.txt#L33-L37) — 注释写明 "repeated twice to up-sample"：`part-0` 与 `part-1` 各出现两次，等效于 wiki 被完整采样两遍。

最小的配方文件只有一行：

- [src/olmo_core/data/mixes/c4-ppl-validation.txt:L1](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/mixes/c4-ppl-validation.txt#L1) — 单源验证集配方：`c4_en-validation,eval-data/perplexity/v3_small_{TOKENIZER}/c4_en/val/part-0-00000.npy`。

`DataMix` 的消费入口是 `NumpyDatasetConfig` 的 `mix` / `mix_base_dir` 字段（与 `paths`、`source_mixture_config` 三选一，见 [src/olmo_core/data/numpy_dataset.py:L2868-L2879](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/numpy_dataset.py#L2868-L2879) 的 `validate()`）。

#### 4.1.4 代码实践

**实践目标**：亲手解析一个预置 `.txt` 配方，统计各 label 的文件数，体会「重复行 = 上采样」。

**操作步骤**（在仓库根目录，Python 交互式环境）：

```python
from collections import Counter
from olmo_core.data.mixes import DataMix

paths, labels = DataMix.dolma17.build(base_dir="/tmp/fake-base", tokenizer="allenai/dolma2-tokenizer")
print(len(paths), "个文件")                    # 注意：base_dir 只是前缀，本练习不读数据本身
counter = Counter(labels)
for label, n in sorted(counter.items(), key=lambda kv: -kv[1]):
    print(f"{label:30s} {n} 个分片")
print(paths[0])                                # 检查 {TOKENIZER} 已被替换、前缀已拼接
```

**需要观察的现象**：

1. `dolma17` 的 label 列表里有 `gutenberg-books`、`pes2o-stem`、`wiki`、`starcoder`、`c4` 等约十几个源。
2. `wiki` 的分片数是「物理分片数的两倍」（因为路径写了两遍）。
3. 打印出的路径形如 `/tmp/fake-base/preprocessed/.../allenai/dolma2-tokenizer/part-0-00000.npy`，不含 `{TOKENIZER}`。

**预期结果**：脚本能离线跑通（`build()` 只做字符串处理，不访问 `base_dir`），`Counter` 显示 starcoder/c4 这类大语料分片最多。**待本地验证**（具体数字取决于配方文件版本）。

#### 4.1.5 小练习与答案

**练习 1**：如果某行路径漏写了 `{TOKENIZER}`，会发生什么？

**答案**：`DataMix.build()` 在解析循环里检查 `"{TOKENIZER}" not in path`，不满足时抛出 `ValueError(f"line {line_num + 1} in data mix '{self}' is invalid")`（[mixes/__init__.py:L123-L124](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/mixes/__init__.py#L123-L124)）。这个约束保证每个分片路径都按分词器正确分目录。

**练习 2**：`.txt` 格式没有 ratio 字段，dolma17 是如何让 wiki 占比翻倍的？这种表达方式的局限是什么？

**答案**：把 wiki 的两个分片路径各写两遍（[dolma17.txt:L33-L37](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/mixes/dolma17.txt#L33-L37)），文件在数据集里出现两次即被采样两遍。局限：只能表达整数倍重复，无法精确到任意比例（如 37%），也无法限制「只用某个源的前 10%」——这正是下一模块 `SourceMixture` 要解决的问题。

### 4.2 SourceMixture：按 token 配额的精确混合

#### 4.2.1 概念说明

`source_mixture.py` 提供「**显式比例**」的混合：每个源声明 `target_ratio`（目标占比），总和必须为 1.0；再配合 `requested_tokens`（这次训练总共要多少 token），`build()` 会精确计算**每个文件的采样 token 数**，输出一份 `SourceMixtureDataset` 清单。

三个 Config 类的分工刻意做了分离：

- `SourceMixtureConfig`：一个源的名字、比例、路径、重复上限、用量上限。
- `SourceMixtureList`：一组源的容器，只负责「比例和为 1」的校验——docstring 明说这样可以把配方存成 YAML 而不必写死 `requested_tokens`、`global_batch_size` 这类**实验参数**。
- `SourceMixtureDatasetConfig`：拿着配方 + 实验参数，物化出清单。

这套机制是 OLMo 3 / OLMo-hybrid 中期训练脚本实际使用的入口（`SourceMixtureList.from_yaml(...)`，见 [src/scripts/train/OLMo3/OLMo3-7B-midtraining.py:L52-L58](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/train/OLMo3/OLMo3-7B-midtraining.py#L52-L58)）。

#### 4.2.2 核心流程

`SourceMixtureDatasetConfig.build(npdtype, sequence_length)` 的五步：

1. **数 token**：对每个源的每个文件，按「文件字节数 ÷ dtype 字节数」统计可用 token（不读内容，可用 glob 展开通配符）。
2. **算配额并校验**：对每个源
   - 需要量 \( n_{\text{need}} = \text{requested\_tokens} \times \text{target\_ratio} \)
   - 可提供上限 \( n_{\text{max}} = \text{population} \times \text{max\_source\_fraction} \times \text{max\_repetition\_ratio} \)
   - 若 \( n_{\text{max}} < n_{\text{need}} \) 抛 `OLMoConfigurationError`。
3. **按文件预分配**：`take_ratio = need / population`；若 > 1，把它拆成若干个 ≤ 1.0 的档（即「完整采几遍 + 最后一遍取一部分」），每个文件按档生成一条 `(path, tokens, max_tokens)` 记录。
4. **实例配给（Hamilton 最大余数法）**：训练实际需要 \( I = \lceil \text{requested\_tokens} / \text{global\_batch\_size} \rceil \times (\text{global\_batch\_size} / \text{sequence\_length}) \) 个实例；对每条文件记录取 `base = min(desired, max)` 个实例，不够的差额按「小数余数从大到小」逐个补给尚有余量的文件，且任何文件不超过其 `max_tokens` 允许的实例数。
5. **汇总输出**：把最终 token 数写回记录，渲染 Rich 表格，打印每个源「实际占比 vs 目标比例」的偏差，返回 `SourceMixtureDataset`。

Hamilton 法的直觉：把 \( I \) 个实例按各文件的「理想份额」分摊时会产生小数，先给每份取整（floor），再把剩下的名额按小数部分排序、从大到小每人发一个——这是「比例代表制议席分配」的经典算法，源码注释也直接链接了该方法。

#### 4.2.3 源码精读

单个源的配置与校验：

- [src/olmo_core/data/source_mixture.py:L29-L60](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/source_mixture.py#L29-L60) — `SourceMixtureConfig` 的五个字段：`source_name`、`target_ratio`（目标占比）、`paths`（支持 `*` 通配）、`max_repetition_ratio`（默认 1.0 = 不允许重复，>1 即允许上采样）、`max_source_fraction`（默认 1.0 = 允许用完全部数据）。
- [src/olmo_core/data/source_mixture.py:L64-L78](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/source_mixture.py#L64-L78) — `validate()`：`target_ratio` 必须在 (0, 1]、`max_source_fraction` 在 [0, 1]、`max_repetition_ratio` ≥ 1、`paths` 非空。
- [src/olmo_core/data/source_mixture.py:L80-L123](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/source_mixture.py#L80-L123) — `resolved_paths` 属性：含 `*` 的路径用 `deterministic_glob_directory` 展开并缓存；匹配不到时抛 `FileNotFoundError`，且当路径含 `0625` 时附加提示——OLMo-mix-0625 部分文件已不可得，续训需换 0925 配方或设 `ignore_fingerprint_mismatch=True`。这是「配方随库演化」的一个真实运维细节。

配方容器与总量参数：

- [src/olmo_core/data/source_mixture.py:L126-L148](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/source_mixture.py#L126-L148) — `SourceMixtureList.validate()`：所有 `target_ratio` 之和必须 `np.allclose(..., 1.0)`，否则报错（对比 4.3 中 composable 路线会**自动归一化**）。
- [src/olmo_core/data/source_mixture.py:L252-L294](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/source_mixture.py#L252-L294) — `SourceMixtureDatasetConfig` 字段：`source_list`、`requested_tokens`（期望总 token 数）、`global_batch_size`（用于换算实例数）、`processes`（并行数 token 的线程数）、`seed`、`render_tables`。docstring 概括了 build 的四步流程。

build 的关键代码段：

- [src/olmo_core/data/source_mixture.py:L301-L316](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/source_mixture.py#L301-L316) — build 入口：先 `validate()`，然后逐源并行统计可用 token。
- [src/olmo_core/data/source_mixture.py:L322-L344](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/source_mixture.py#L322-L344) — 配额计算与不足即报错：`needed = requested_tokens * target_ratio`，`max = population * max_source_fraction * max_repetition_ratio`。
- [src/olmo_core/data/source_mixture.py:L356-L361](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/source_mixture.py#L356-L361) — 实例总数换算：`training_steps = ceil(requested_tokens / global_batch_size)`，`requested_instances = training_steps * (global_batch_size // sequence_length)`；并断言 `global_batch_size` 是 `sequence_length` 的整数倍。
- [src/olmo_core/data/source_mixture.py:L367-L419](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/source_mixture.py#L367-L419) — Hamilton 最大余数法配给：每条路径先拿 `min(desired, max)` 个基础实例，缺口先均摊、再按余数从大到小补发，全程不突破 `max_tokens // sequence_length` 的容量。
- [src/olmo_core/data/source_mixture.py:L470-L509](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/source_mixture.py#L470-L509) — `get_paths_and_tokens_for_source()`：`take_ratio > 1` 时切档循环（`chunk = min(1.0, remaining)`），同一文件生成多条记录，每条 ≤ 一遍完整数据——这就是「重复 2.6 遍」的实现方式。
- [src/olmo_core/data/source_mixture.py:L553-L560](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/source_mixture.py#L553-L560) — token 计数的真身：`_bytes_to_tokens` 用 `get_file_size(path) // itemsize` 换算，印证了 u4-l1 讲的「无文件头裸数组」格式。

产出的清单与下游消费：

- [src/olmo_core/data/source_mixture.py:L202-L248](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/source_mixture.py#L202-L248) — `SourceMixtureDataset`：`to_index()` 返回 `{(path, 序号): token数}` 字典，`to_paths()` 返回稳定排序的路径列表；`filter_zero_token_paths` 可选地丢弃零 token 路径以保持编号紧凑（默认保留以稳定历史编号）。
- [src/olmo_core/data/numpy_dataset.py:L2857-L2866](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/numpy_dataset.py#L2857-L2866) — `NumpyFSLDatasetConfig.from_src_mix()`：把 `SourceMixtureDatasetConfig` 塞进数据集配置，同时清空 `paths` / `mix` / `mix_base_dir`（三选一约束）。
- [src/olmo_core/data/numpy_dataset.py:L2884-L2895](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/numpy_dataset.py#L2884-L2895) — `build()` 中的消费：调用 `source_mixture_config.build()` 得到清单，打开 `filter_zero_token_paths`，用 `to_paths()` + `to_index()` 构造 `NumpyFSLDatasetMixture`——清单里的 `path_offset_index` 告诉数据集「每个文件只读前 N 个 token」。

行为断言（实践的依据）：

- [src/test/data/source_mixture_test.py:L184-L220](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/data/source_mixture_test.py#L184-L220) — 源 1 只有 1M token 却要承担 50% × 5M 的配额，默认不允许重复时应抛 `OLMoConfigurationError`。
- [src/test/data/source_mixture_test.py:L223-L284](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/data/source_mixture_test.py#L223-L284) — 同样场景把 `max_repetition_ratio` 提到 3.0 即可满足，且断言实例总数恰等于 `requested_instances`。
- [src/test/data/fixtures.py:L39-L67](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/data/fixtures.py#L39-L67) — 两个源按 0.8/0.2 混合并经 `from_src_mix` 构建数据集的完整示例，可直接作为实践参考。

#### 4.2.4 代码实践

**实践目标**：用两个本地合成 `.npy` 验证 SourceMixture 的配额计算——目标占比 0.8/0.2，观察它如何切分 token。

**操作步骤**：

```python
# save as practice_srcmix.py，在仓库根目录运行: python practice_srcmix.py
import tempfile
from pathlib import Path
import numpy as np

from olmo_core.data.source_mixture import (
    SourceMixtureConfig, SourceMixtureDatasetConfig, SourceMixtureList,
)

tmp = Path(tempfile.mkdtemp())

def mk_npy(path: Path, n_tokens: int, fill: int) -> str:
    mm = np.memmap(path, mode="w+", dtype=np.uint32, shape=(n_tokens,))
    mm[:] = fill   # 用不同常量填充，便于之后区分来源
    mm.flush()
    return str(path)

p1 = mk_npy(tmp / "src_a.npy", 20_000, 1)   # 源 A：2 万 token
p2 = mk_npy(tmp / "src_b.npy", 10_000, 2)   # 源 B：1 万 token

seq_len, gbs = 64, 64 * 32
config = SourceMixtureDatasetConfig(
    source_list=SourceMixtureList([
        SourceMixtureConfig(source_name="A", target_ratio=0.8, paths=[p1]),
        SourceMixtureConfig(source_name="B", target_ratio=0.2, paths=[p2]),
    ]),
    requested_tokens=6_000,                 # 只想要 6000 token
    global_batch_size=gbs,
    quiet=True, render_tables=False,
)
mixture = config.build(npdtype=np.uint32, sequence_length=seq_len)

total = {s.name: sum(pt.tokens for pt in s.path_tokens) for s in mixture.sources}
grand = sum(total.values())
for name, tokens in total.items():
    print(f"{name}: {tokens} tokens, 实际占比 {tokens / grand:.3f}")
print("清单:", mixture.to_index())
```

**需要观察的现象**：A、B 两个源的实际占比约 0.8/0.2；`to_index()` 里每个 `(path, idx)` 的 token 数都是 `seq_len` 的整数倍；总实例数等于 `ceil(6000 / 2048) * 32`。

**预期结果**：占比打印接近 `0.8 / 0.2`（因实例对齐会有微小取整差）；总 token 略大于 `requested_tokens`。对照 [source_mixture_test.py:L66-L72](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/data/source_mixture_test.py#L66-L72) 的同款断言。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：源 A 有 100 万 token，`target_ratio=0.5`，`requested_tokens=500` 万。默认参数下 build 会怎样？两种修复方式是什么？

**答案**：默认 `max_repetition_ratio=1.0`、`max_source_fraction=1.0`，可提供上限 = 100 万 < 需要量 250 万，`build()` 在配额校验处抛 `OLMoConfigurationError`（[source_mixture.py:L330-L334](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/source_mixture.py#L330-L334)）。修复一：给源 A 设 `max_repetition_ratio=3.0` 允许重复采样（对应测试 `test_dataset_mixture_build_with_repetition`）；修复二：调低 A 的 `target_ratio` 或调低 `requested_tokens`。

**练习 2**：为什么 `SourceMixtureList` 只装配方、`requested_tokens` 等参数放在 `SourceMixtureDatasetConfig`？

**答案**：docstring 给出动机（[source_mixture.py:L131-L137](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/source_mixture.py#L131-L137)）：配方（哪些源、什么比例）是相对稳定的资产，可以单独存 YAML 复用；而 `requested_tokens` / `global_batch_size` / `processes` 是每次实验的运行参数。分离后同一份配方 YAML 可以服务不同规模的实验——OLMo3 midtraining 脚本正是 `SourceMixtureList.from_yaml()` 后再组装实验参数。

**练习 3**：`max_source_fraction=0.1` 与 `max_repetition_ratio=2.0` 同时设置时，源的可用上限是多少？

**答案**：\( \text{population} \times 0.1 \times 2.0 \)，见 [source_mixture.py:L325-L328](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/source_mixture.py#L325-L328)——两个系数相乘。语义是「最多用 10% 的数据，且这部分最多重复两遍」。对应测试 `test_dataset_mixture_build_insufficient_source_max_fraction`（10% 上限导致 25% 配额不可满足而报错）。

### 4.3 composable 世界的比例混合：`MixingInstanceSource` 与 `SamplingInstanceSource`

#### 4.3.1 概念说明

u4-l2 建立了 composable 的三层抽象，本模块讲其中最重要的组合子之一：**实例级按比例混合**。`MixingInstanceSource` 接收若干 `MixingInstanceSourceSpec`（源 + ratio + 重复上限 + 标签），内部做两件事：

1. 用共享函数 `calculate_sample_sizes` 算出每个源应贡献多少 token；
2. 把每个源包一层 `SamplingInstanceSource`（按配额采样子集），再拼接成 `ConcatenatedInstanceSource`。

与 4.2 的 `SourceMixture` 相比的关键差异：

| 维度 | SourceMixture（4.2） | MixingInstanceSource（4.3） |
| --- | --- | --- |
| 比例约束 | 必须严格和为 1.0，否则报错 | 不要求和为 1，自动归一化并给 `UserWarning` |
| 目标规模 | `requested_tokens` 必填（strict） | `num_tokens`/`num_instances` 可选；不填时目标 = 子源总量（非 strict） |
| 输出物 | 「每文件取多少 token」的清单 | 一个可直接迭代/喂给 DataLoader 的 `InstanceSource` |
| 层级组合 | 扁平 | 可任意嵌套（mix 套 mix，见门面 docstring 的 code/math 分组示例） |
| 生态 | `NumpyFSLDatasetConfig.from_src_mix` | `ComposableDataLoader` |

`SamplingInstanceSource` 单独使用时是「调容」工具：`.sample(max_tokens=...)` 运算符（u4-l2 已见）返回的就是它；它也是混合的执行者。

#### 4.3.2 核心流程

`MixingInstanceSource.__init__` 的流程：

1. 校验所有子源 `sequence_length` / `max_sequence_length` 一致；校验 `num_tokens` 与 `num_instances` 互斥（若都给，必须满足 `num_tokens == num_instances * sequence_length`）。
2. 调 `calculate_sample_sizes(sizes, ratios, max_repetition_factors, target_size=num_tokens)` 得到每个源的采样量。
3. 为每个子源构造 `SamplingInstanceSource(source, max_tokens=配额, seed=seed+i)`——**第 i 个源用 `seed+i`**，保证各源采样互不相关。
4. 拼接为 `ConcatenatedInstanceSource`，并打出形如 `❯ 80.000% source_a, 4,800 sampled instances (307K tokens) from ...` 的汇总日志。

`calculate_sample_sizes` 的算法（比例守恒的最少丢弃分配）：

1. 若 ratios 不和为 1，归一化并警告（strict 模式下若最终达不到目标则报错）。
2. 理想采样量 \( s_i^{\text{ideal}} = T \cdot r_i \)（\(T\) 为目标总量，未指定时取子源之和）。
3. 每个源需要的重复倍数 \( k_i = \max(s_i^{\text{ideal}} / s_i,\ 1) \)，实际使用的重复倍数取 \( \min(K_i,\ k_i) \)（\(K_i\) 为给定的上限）。
4. 可用量 \( s_i^{\text{use}} = s_i \cdot \min(K_i, k_i) \)。
5. **整体收缩因子** \( \alpha = \min\left(1,\ \min_i \frac{s_i^{\text{use}}}{s_i^{\text{ideal}}}\right) \)，最终 \( s_i = s_i^{\text{ideal}} \cdot \alpha \)。

第 5 步是灵魂：如果任何源即使重复到上限也填不满它的理想份额，就**按同一比例缩小所有源**，保证实际占比仍严格等于目标占比（函数末尾有 `np.allclose` 断言自检）。代价是混合总量小于目标——strict 模式下此时抛 `OLMoConfigurationError`，错误信息会精确指出瓶颈源、它需要的重复倍数和建议值。

`SamplingInstanceSource` 的执行流程：

1. `max_tokens` 换算为 `max_instances = max_tokens // sequence_length`（三选一约束在 config 的 `__post_init__` 校验）。
2. 多个子源时按 `len(source) / total` 比例分配配额，并向下对齐到 `chunk_size = max_sequence_length // sequence_length` 的整数倍（这是「改变 sequence_length 但保持数据顺序稳定」的机制，u4-l2 的 `build_global_indices` 注释有详细说明）。
3. 为每个源生成洗牌索引并**落盘**到 `work_dir`（文件名含双方 fingerprint，可跨进程复用）：`np.tile(全量索引, 重复次数)` 拼上前缀切片，实现「重复 k 遍 + 取剩余」。
4. 读取时 `__getitem__` 按偏移定位到子源，再查索引数组把「混合内下标」翻译回「源内真实下标」。

#### 4.3.3 源码精读

混合规格与配置：

- [src/olmo_core/data/composable/mixing_instance_source.py:L31-L51](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/mixing_instance_source.py#L31-L51) — `MixingInstanceSourceSpecConfig`：`source`（任意 `InstanceSourceConfig`）、`ratio`（>0）、`max_repetition_factor`（≥1）、`label`。
- [src/olmo_core/data/composable/mixing_instance_source.py:L55-L78](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/mixing_instance_source.py#L55-L78) — `MixingInstanceSourceConfig`：`source_specs` 列表 + 可选 `num_tokens` / `num_instances`；`build(work_dir)` 逐个 build 子配置再组装。
- [src/olmo_core/data/composable/mixing_instance_source.py:L82-L108](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/mixing_instance_source.py#L82-L108) — 运行时规格类 `MixingInstanceSourceSpec`，`ratio` 的 docstring 明确「跨所有 spec 不必和为 1，会自动归一化」。

混合主体：

- [src/olmo_core/data/composable/mixing_instance_source.py:L150-L183](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/mixing_instance_source.py#L150-L183) — 构造校验：至少一个 spec；所有子源序列长度一致；`num_tokens` / `num_instances` 互斥且可互相换算。
- [src/olmo_core/data/composable/mixing_instance_source.py:L187-L211](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/mixing_instance_source.py#L187-L211) — 核心装配：`calculate_sample_sizes(...)` 算配额 → 每个源包成 `SamplingInstanceSource(..., max_tokens=int(sample_size), seed=seed+i, ...)` → `ConcatenatedInstanceSource` 拼接。类 docstring（L122-L133）说明了「最少丢弃」目标与 strict 报错条件。
- [src/olmo_core/data/composable/mixing_instance_source.py:L213-L226](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/mixing_instance_source.py#L213-L226) — 构建完成后的汇总日志：逐行打印每个源的实际百分比、采样实例数 / 源实例数——**这就是验证占比的第一手工具**。

配额算法：

- [src/olmo_core/data/composable/utils.py:L147-L170](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/utils.py#L147-L170) — `target_size` 缺省时 `strict=False` 且目标取子源之和；ratios 归一化并发 `UserWarning`（逐源打印调整前后比例）。
- [src/olmo_core/data/composable/utils.py:L172-L192](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/utils.py#L172-L192) — 算法主体：`ideal_sample_sizes` → `repetition_factors_to_use = min(上限, 需要的倍数)` → `adjustment_factor = min(1.0, (sizes_to_use / ideal).min())` 整体收缩。注释完整解释了「为何需要丢弃/过采样」与收缩因子的推导。
- [src/olmo_core/data/composable/utils.py:L194-L225](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/utils.py#L194-L225) — 自检断言（实际占比 allclose 目标、不超可用量）与 strict 模式的报错：指出瓶颈源、需要的样本量、能提供的量、建议把重复倍数提到多少。

采样执行：

- [src/olmo_core/data/composable/sampling_instance_source.py:L20-L43](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/sampling_instance_source.py#L20-L43) — config 三选一约束：`max_tokens` / `max_instances` / `factor`（按子源总量的比例）恰好设一个；`factor` 路径在 `build()` 里换算成 `max_tokens`（L49-L51）。
- [src/olmo_core/data/composable/sampling_instance_source.py:L100-L131](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/sampling_instance_source.py#L100-L131) — 构造校验（二选一）与无 seed 警告：不设 seed 时直接取前 N 个实例，可能破坏子源内部分布，官方建议始终设 seed。
- [src/olmo_core/data/composable/sampling_instance_source.py:L133-L151](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/sampling_instance_source.py#L133-L151) — 多子源按长度比例分配：`sample_size = int(max_instances * (len(source) / total_instances))`，再对齐到 `chunk_size`。
- [src/olmo_core/data/composable/sampling_instance_source.py:L153-L176](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/sampling_instance_source.py#L153-L176) — 索引落盘：`build_global_indices` 洗牌 → `np.tile(全量, n_repetitions)` + 前缀 `sample_indices` → 写入 `work_dir / f"{fingerprint}-{source.fingerprint}-indices.npy"`；只在 `fs_local_rank == 0` 写，随后 `barrier()`——多进程安全。
- [src/olmo_core/data/composable/sampling_instance_source.py:L216-L234](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/sampling_instance_source.py#L216-L234) — 读取路由：按 `source_sample_sizes` 累计偏移定位子源，从索引文件读 1 个元素得到源内真实下标。

层级混合的官方示例（mix 套 mix，code 组内 0.5/0.5、math 组内 0.1/0.9，两组再各占 0.5）见 [src/olmo_core/data/composable/__init__.py:L188-L237](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/__init__.py#L188-L237)，随后的 `visualize()` 输出树（L239-L257）直观展示了「每个子源都被包了一层 SamplingInstanceSource」的结构。

#### 4.3.4 代码实践

**实践目标**：与 4.2.4 同一份数据（源 A 2 万 token、源 B 1 万 token，用常量 1/2 填充），在 composable 世界按 3:1 混合，先看源树，再用 token 值验证 DataLoader 产出的实际占比。

**操作步骤**：

```python
# save as practice_mix.py，运行: python practice_mix.py
import tempfile
from collections import Counter
from pathlib import Path
import numpy as np
import torch

from olmo_core.data import TokenizerConfig
from olmo_core.data.composable import (
    ComposableDataLoaderConfig, ConcatAndChunkInstanceSourceConfig,
    NumpyDocumentSourceConfig, MixingInstanceSource,
)

tmp = Path(tempfile.mkdtemp())
work_dir = tmp / "work"; work_dir.mkdir()

tokenizer = TokenizerConfig(vocab_size=32_000, eos_token_id=0, pad_token_id=-1)

def mk_npy(path: Path, n_tokens: int, fill: int) -> str:
    mm = np.memmap(path, mode="w+", dtype=np.uint32, shape=(n_tokens,))
    mm[:] = fill
    mm.flush()
    return str(path)

p1 = mk_npy(tmp / "src_a.npy", 20_000, 1)
p2 = mk_npy(tmp / "src_b.npy", 10_000, 2)
seq_len = 64

# 1) 两个 .npy 各成一个实例源（tokenizer 字段必填，声明分词时用的词表；
#    源文件不含文档边界信息，NumpyDocumentSource 把整个文件视作一个长文档）
def instance_source(paths, label):
    return ConcatAndChunkInstanceSourceConfig(
        sources=[NumpyDocumentSourceConfig(source_paths=paths, tokenizer=tokenizer)],
        sequence_length=seq_len, label=label,
    )

mix_config = MixingInstanceSource.Config(
    source_specs=[
        MixingInstanceSource.Spec.Config(
            source=instance_source([p1], "A"), ratio=3.0, label="A"),
        MixingInstanceSource.Spec.Config(
            source=instance_source([p2], "B"), ratio=1.0, label="B"),
    ],
    seed=42,
)
mix = mix_config.build(work_dir)
mix.visualize()

# 2) 构建前先验证配额（构建日志也会打印同样的百分比）
for s in mix.sampled_sources:
    print(f"{s.label}: {s.num_tokens} tokens, 占比 {s.num_tokens / mix.num_tokens:.3f}")

# 3) 喂给 ComposableDataLoader，用 token 值（1/2）统计真实占比
loader = ComposableDataLoaderConfig(
    tokenizer=tokenizer, global_batch_size=seq_len * 8, work_dir=str(work_dir), seed=42,
).build(mix)   # 未初始化分布式时按单进程（world size 1）处理

counts = Counter()
for i, batch in enumerate(loader):
    vals, nums = torch.unique(batch["input_ids"], return_counts=True)
    for v, n in zip(vals.tolist(), nums.tolist()):
        counts[int(v)] += n
    if i >= 3:
        break
total = sum(counts.values())
print({k: f"{v / total:.3f}" for k, v in sorted(counts.items())})
```

> 注：步骤 1 的写法对照官方示例 [composable/__init__.py:L127-L133](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/__init__.py#L127-L133)；`NumpyDocumentSourceConfig` 的 `tokenizer` 是必填字段（[numpy_document_source.py:L36](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/numpy_document_source.py#L36)）。

**需要观察的现象**：

1. `visualize()` 的源树：`MixingInstanceSource` 下挂两个 `SamplingInstanceSource`（各带 `[A]` / `[B]` 标签），再往下是各自的 `ConcatAndChunkInstanceSource`。
2. 步骤 2 打印 A ≈ 0.75、B ≈ 0.25（ratio 3:1 归一化的结果——注意我们故意没把 ratio 写成和为 1，构建时会有一条归一化 `UserWarning`）。
3. 步骤 3 统计的 token 值分布同样约 75% 是 1、25% 是 2（含 padding/取整误差）。

**预期结果**：源树、配额打印、batch 统计三者互相印证 3:1。若把某个源的 ratio 调大到其容量（含重复上限）填不满，观察整体收缩——所有源同比例缩水但占比不变。**待本地验证**（无 GPU 要求，纯 CPU 可跑）。

#### 4.3.5 小练习与答案

**练习 1**：`ratios=[0.3, 0.3]`（和为 0.6）传入 `MixingInstanceSource` 会发生什么？与 `SourceMixtureList` 的行为差异？

**答案**：composable 路线在 `calculate_sample_sizes` 里发现 `ratio_total` 不 allclose 1.0 时自动归一化为 [0.5, 0.5]，并发 `UserWarning` 逐源打印调整前后的比例（[utils.py:L152-L170](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/utils.py#L152-L170)）；`SourceMixtureList.validate()` 则直接抛 `OLMoConfigurationError`（[source_mixture.py:L145-L148](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/source_mixture.py#L145-L148)）。前者方便快速组合（3:1 直接写 3.0/1.0），后者适合严格审计的正式配方。

**练习 2**：源 A 100 token、源 B 100 token，目标 `num_tokens=1000`，ratio 各 0.5，A 的 `max_repetition_factor=8`、B 的为 1.0。最终各采样多少？

**答案**：理想各 500。A 需要重复 5 倍（500/100，未超上限 8），可用 800；B 最多 100。收缩因子 \( \alpha = \min(1, \min(800/500, 100/500)) = 0.2 \)，最终各 100 token——占比仍 0.5/0.5，总量只有 200。若 `num_tokens` 是显式指定的（strict），此时抛 `OLMoConfigurationError`，错误信息会指出 B 是瓶颈、建议把它的重复倍数提到 5.0（[utils.py:L205-L223](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/utils.py#L205-L223)）。

**练习 3**：`SamplingInstanceSource` 为什么建议设置 seed？落盘的索引文件里存的是什么？

**答案**：无 seed 时直接取每个源的前 N 个实例（并触发 warning，[sampling_instance_source.py:L126-L131](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/sampling_instance_source.py#L126-L131)），若子源内部有顺序性（如按时间排列）会破坏分布。索引文件存的是「洗牌后的源内实例下标序列」：`build_global_indices` 按 seed 洗牌得到全量下标，`np.tile` 复制 n_repetitions 遍再拼上剩余部分的前缀切片（L153-L174）——读取时把混合内的连续下标映射回源内真实位置，等价于「重复 k 遍且每遍顺序相同」。

## 5. 综合实践

把本讲两条路线串起来，完成一次「**为自己的语料写配方**」的完整闭环：

1. **造数据**：用 `np.memmap` 生成两个 `.npy`——`corpus_web.npy`（30_000 个 token，全部填 1）和 `corpus_code.npy`（10_000 个 token，全部填 2），模拟「网页多、代码少」的两源语料。
2. **读官方配方**：打开 [src/olmo_core/data/mixes/dolma17.txt](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/mixes/dolma17.txt)，找到 wiki 的重复行，理解它如何表达「2 倍上采样」；再运行 `DataMix.dolma17.build(...)` 对照 label 分组。
3. **路线一（精确配额）**：写 `SourceMixtureList`，令 web 占 0.6、code 占 0.4（注意 code 只有 1 万 token，若 `requested_tokens=40_000` 则需要给 code 设 `max_repetition_ratio≈2.0` 才可行）；`build()` 后用 `to_index()` 验证每个文件的 token 数都是 `sequence_length` 的倍数、各源占比接近目标。
4. **路线二（composable）**：把同样两个源按 ratio 0.6/0.4 组装 `MixingInstanceSource.Config`，`build()` 后 `visualize()`，再套 `ComposableDataLoaderConfig`（参考 4.3.4）迭代若干 batch，统计 token 值 1 与 2 的比例。
5. **对比结论**：两条路线得到的实际占比是否一致？哪条路线在 code 源容量不足时选择「整体收缩」、哪条直接报错？把观察写成三五行笔记。

这个任务覆盖了本讲全部要点：`.txt` 配方格式、显式比例与隐式重复、配额算法与收缩因子、DataLoader 层面的占比验证。

## 6. 本讲小结

- OLMo 官方配方以 `.txt` 随库分发：每行 `label,path`，`{TOKENIZER}` 运行时替换，比例靠路径重复隐式表达；`DataMix` 枚举 + 别名机制保证旧名可用。
- `SourceMixture` 三件套把「配方」（`SourceMixtureList`，比例必须和为 1）与「实验参数」（`requested_tokens`、`global_batch_size`）分离；`build()` 按文件大小数 token、算配额、用 Hamilton 最大余数法把实例分摊到文件，产出「每文件取多少 token」的清单，经 `NumpyFSLDatasetConfig.from_src_mix()` 消费。
- 上采样有两组旋钮：`max_repetition_ratio`（最多采几遍）与 `max_source_fraction`（最多用多少比例数据），可用上限是二者的乘积；容量不足时 SourceMixture 路线直接报错。
- composable 路线的 `MixingInstanceSource` 用 `calculate_sample_sizes` 做「比例守恒的最少丢弃」分配：理想份额 → 需要的重复倍数（受上限约束）→ 整体收缩因子；ratios 不要求和为 1（自动归一化）。
- 混合的执行者是 `SamplingInstanceSource`：配额按子源长度比例分配并对齐 chunk，洗牌索引（含 tile 重复）落盘共享，读取时按下标偏移路由回源内真实位置。
- 验证占比有三个层次的工具：构建汇总日志 / `visualize()` 源树、`sampled_sources` 的 `num_tokens` 配额、以及最彻底的——给每个源填充不同 token 值后在实际 batch 里数频次。

## 7. 下一步学习建议

- **单元五（优化器）**：数据就绪后进入训练侧——从 u5-l1「优化器配置体系」开始，看 `OptimConfig` 家族与参数组覆盖。
- **回读 u4-l2 的课程学习部分**：本讲的 `MixingInstanceSource` 可以和 `split()`、`sources_per_epoch` 组合出分阶段（curriculum）数据调度，值得用 `visualize()` 亲手搭一棵分层混合树（官方层级示例在 [composable/__init__.py:L188-L237](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/__init__.py#L188-L237)）。
- **阅读官方中期训练脚本**：[src/scripts/train/OLMo3/OLMo3-7B-midtraining.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/train/OLMo3/OLMo3-7B-midtraining.py) 展示了 `SourceMixtureList.from_yaml()` 在真实实验里如何与 `ExperimentConfig` 组装。
- **源码延伸**：对照 [src/test/data/source_mixture_test.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/data/source_mixture_test.py) 的 7 个测试逐个跑 `pytest -v src/test/data/source_mixture_test.py`，用断言反推行为边界。
