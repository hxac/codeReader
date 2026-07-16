# 数据集加载与混合（Dataset Mixture）

## 1. 本讲目标

本讲拆解 open-r1 的「数据进入训练器之前的最后一公里」——`get_dataset()`。学完后你应当能够：

- 说清 `get_dataset()` 对**单数据集**（`dataset_name`）和**混合数据集**（`dataset_mixture`）两条分支的处理差异。
- 掌握 `DatasetConfig` / `DatasetMixtureConfig` 两个数据类的字段含义，以及 `ScriptArguments.__post_init__` 做的三道校验。
- 会用 `weight`（按比例随机抽样）、`columns`（列裁剪）、`test_split_size`（切分验证集）这三个旋钮精确控制最终送进训练的样本数量与结构。
- 能对照 `tests/utils/test_data.py` 里的断言，手算出混合后的样本数。

本讲承接 [u1-l4 配置系统](u1-l4-config-system.md)：那里讲的是「YAML 三元组如何被解析」，这里讲的是其中 `ScriptArguments` 里 `dataset_mixture` 这个字段被解析成对象之后，`get_dataset()` 究竟拿它做了什么。它也为 [u2-l1 SFT 脚本主流程](u2-l1-sft-script-walkthrough.md) 里那个被当作黑盒的 `get_dataset(args)` 调用补上实现细节。

## 2. 前置知识

- **split（数据集分片）**：Hugging Face 数据集通常分为 `train` / `test` / `validation` 等多个分片。`datasets.load_dataset(id, config, split="train")` 可以只加载其中一片，甚至用切片语法 `split="train[:10]"` 只取前 10 条。
- **DatasetDict 与 Dataset**：`load_dataset` 默认返回 `DatasetDict`（一个像字典的容器，键是分片名，值是 `Dataset`）；指定 `split=` 时则返回单个 `Dataset`。
- **列（column）**：一个 `Dataset` 由若干列组成（类似表格的列）。要把两个数据集「上下拼接」（`concatenate_datasets`），它们的列名与列类型必须一致——这是本讲反复出现的硬约束。
- **权重抽样（weighted subsampling）**：这里的 `weight` 不是统计学里的加权平均权重，而是一个 0~1 之间的比例，表示「从这个数据集里随机抽取出该比例的样本」。`weight=0.25` 表示只用 1/4 的数据。
- **可复现性 / seed**：所有 shuffle 与抽样都用同一个 `seed`，保证每次运行得到完全相同的样本子集，这是训练可复现的前提。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/open_r1/utils/data.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/data.py) | 唯一的入口函数 `get_dataset()`，实现单数据集加载与混合数据集的「加载 → 裁列 → 抽样 → 拼接 → 洗牌 → 切分」全流程。 |
| [src/open_r1/configs.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py) | 定义 `DatasetConfig`、`DatasetMixtureConfig`，以及 `ScriptArguments` 中把 YAML 字典转成配置对象、并做校验的 `__post_init__`。 |
| [tests/utils/test_data.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/utils/test_data.py) | 用真实数据集 `trl-internal-testing/zen` 对每条分支写下的断言，是我们「手算样本数」的最佳参照。 |
| [README.md](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md) | 「Customising the dataset mixture」一节给出了官方的 `dataset_mixture` YAML 模板。 |

> 提示：`get_dataset` 在 [src/open_r1/utils/\_\_init\_\_.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/__init__.py) 中被导出，所以脚本里写 `from open_r1.utils import get_dataset` 即可。

## 4. 核心概念与源码讲解

### 4.1 配置数据类与三道校验

#### 4.1.1 概念说明

混合数据集的「配方」在 YAML 里是一段嵌套字典，但 Python 代码更希望操作的是有类型、有字段的对象。`configs.py` 用三个 dataclass 承担这件事：

- `DatasetConfig`：描述混合里**单个**成员数据集（叫哪个 id、哪个 config、哪个 split、保留哪些列、抽多少比例）。
- `DatasetMixtureConfig`：描述**整个**混合（成员列表 + 全局 seed + 是否切验证集）。
- `ScriptArguments`：脚本参数。它把 YAML 里的 `dataset_mixture` 字典在 `__post_init__` 里翻译成上面的 `DatasetMixtureConfig`，并顺手做几道合法性校验。

#### 4.1.2 核心流程

`ScriptArguments.__post_init__` 的职责可以概括为「翻译 + 校验」：

1. **二选一校验**：`dataset_name` 和 `dataset_mixture` 必须至少给一个，否则报错。
2. **结构校验**：若提供了 `dataset_mixture`，它必须是个含 `datasets` 键的字典，且 `datasets` 必须是列表。
3. **翻译**：把列表里每个字典元素构造成 `DatasetConfig`，再包成一个 `DatasetMixtureConfig`，覆盖回 `self.dataset_mixture`。
4. **列一致性校验**：所有显式声明了 `columns` 的成员，其列名集合必须完全一致，否则报错（因为后续要 `concatenate`）。

#### 4.1.3 源码精读

两个配置数据类的字段非常精简：

[configs.py:L22-L39](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L22-L39) — 定义 `DatasetConfig`（单成员）与 `DatasetMixtureConfig`（整个混合）的全部字段。注意 `weight`、`columns`、`test_split_size` 都是 `Optional`，缺省时为 `None`。

`ScriptArguments` 把父类 `trl.ScriptArguments` 的 `dataset_name` 改成可选，并新增 `dataset_mixture`：

[configs.py:L69-L76](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L69-L76) — `dataset_name` 被改成 `Optional[str]`（默认 `None`），并新增 `dataset_mixture: Optional[dict]` 字段。这正是「二选一」能够成立的前提。

二选一校验在最前面：

[configs.py:L79-L80](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L79-L80) — 两个都为 `None` 时直接抛 `ValueError("Either dataset_name or dataset_mixture must be provided")`。

结构校验保证字典形态正确：

[configs.py:L82-L87](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L82-L87) — `dataset_mixture` 必须是字典且含 `datasets` 键。

随后把每个成员字典翻译成 `DatasetConfig`（注意几个默认值：`split` 默认 `"train"`，`weight` 默认 `1.0`）：

[configs.py:L89-L104](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L89-L104) — 遍历 `datasets` 列表，逐项构造 `DatasetConfig`。

再把它们包成 `DatasetMixtureConfig`（`seed` 默认 `0`，`test_split_size` 默认 `None`）：

[configs.py:L106-L110](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L106-L110) — 用解析出的字段构造 `DatasetMixtureConfig` 并覆盖 `self.dataset_mixture`。从此 `self.dataset_mixture` 不再是字典，而是带类型的对象。

最后是列一致性校验——这是为了让后面的 `concatenate_datasets` 不至于因为列对不上而崩：

[configs.py:L112-L120](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L112-L120) — 收集所有非空 `columns` 集合，只要彼此不完全相等就抛错。注意：**没有声明 `columns`（为 `None`）的成员不参与比较**，它们会保留原始全部列。

#### 4.1.4 代码实践

**实践目标**：亲手触发 `__post_init__` 的两条错误分支，确认校验确实生效。

操作步骤（Python 交互环境，需先 `pip install datasets`，并 `export PYTHONPATH=src` 让本地源码可见）：

1. 触发「二选一」错误：

   ```python
   from open_r1.configs import ScriptArguments
   ScriptArguments(dataset_name=None, dataset_mixture=None)  # 期望抛 ValueError
   ```

2. 触发「列不一致」错误：

   ```python
   from dataclasses import asdict
   from open_r1.configs import DatasetConfig, DatasetMixtureConfig, ScriptArguments
   mix = DatasetMixtureConfig(datasets=[
       DatasetConfig(id="a", columns=["prompt"]),
       DatasetConfig(id="b", columns=["chosen"]),
   ])
   ScriptArguments(dataset_mixture=asdict(mix))  # 期望抛 ValueError，提示 "Column names must be consistent"
   ```

需要观察的现象：两次构造都应在 `__post_init__` 阶段抛出 `ValueError`，错误信息分别包含 `Either ... must be provided` 与 `Column names must be consistent`。

预期结果：与 [test_data.py:L106-L125](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/utils/test_data.py#L106-L125) 两个用例 `test_mixture_with_mismatched_columns`、`test_no_dataset_name_or_mixture` 的断言一致。

#### 4.1.5 小练习与答案

**练习 1**：YAML 里某个成员没写 `weight`，加载时它的 `weight` 是多少？会被抽样吗？
**答案**：在 [configs.py:L100](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L100) 中 `weight=dataset_config.get("weight", 1.0)`，默认 `1.0`。但真正决定是否抽样的是 `data.py` 里 `if dataset_config.weight is not None`——只要 `weight` 非 `None`（哪怕是 1.0）就会进入 `shuffle().select(range(...))` 分支，只是抽样比例为 100%、不改变数量。

**练习 2**：为什么列一致性校验只比较「声明了 `columns` 的成员」，而不是所有成员？
**答案**：因为 `columns=None` 的成员会在 `data.py` 中保留原始全部列，其列集合在配置阶段不可知；强行比较没有意义。真正的列对齐最终由 `concatenate_datasets` 在运行时兜底——若类型对不上仍会报错。

### 4.2 单数据集加载分支（dataset_name）

#### 4.2.1 概念说明

当你只指定 `dataset_name`（不指定 `dataset_mixture`），`get_dataset()` 走的是最简单的分支：直接把参数透传给 `datasets.load_dataset()`，原样返回。这是 [u2-l1](u2-l1-sft-script-walkthrough.md) 里 `config_distill.yaml` 使用 `dataset_name: open-r1/Mixture-of-Thoughts` 的路径——数据集的混合已经在数据生产阶段完成，这里只管加载。

#### 4.2.2 核心流程

```
get_dataset(args)
  └─ args.dataset_name 非空 且 dataset_mixture 为空
       └─ return datasets.load_dataset(args.dataset_name, args.dataset_config)
```

要点：这一分支**不传 `split`**（加载所有分片）、**不裁列**、**不抽样**、**不切验证集**。它只是 `load_dataset` 的薄封装。

#### 4.2.3 源码精读

[data.py:L21-L23](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/data.py#L21-L23) — 单数据集分支：记一条日志，然后把 `dataset_name` 与 `dataset_config` 交给 `load_dataset` 后直接返回。注意条件是 `dataset_name and not dataset_mixture`，二者同时给出时优先走混合分支（见 4.3）。

#### 4.2.4 代码实践

**实践目标**：确认单数据集分支返回的是「完整 `DatasetDict`，包含数据集自带的所有分片与所有列」。

操作步骤（对照测试 `test_dataset_and_config_name`）：

```python
from open_r1.configs import ScriptArguments
from open_r1.utils import get_dataset
args = ScriptArguments(dataset_name="trl-internal-testing/zen", dataset_config="conversational_preference")
ds = get_dataset(args)
print(type(ds), list(ds.keys()))
```

需要观察的现象：返回值是 `DatasetDict`，包含 `zen` 数据集自带的分片（如 `train` / `test`），且列没有被裁剪。

预期结果：与 [test_data.py:L30-L35](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/utils/test_data.py#L30-L35) 一致——`len(ds["train"])` 与直接 `load_dataset` 得到的 `train` 长度相等。若本地无法联网下载，此步「待本地验证」。

#### 4.2.5 小练习与答案

**练习**：若同时设置了 `dataset_name` 和 `dataset_mixture`，会走哪条分支？为什么？
**答案**：走**混合分支**。因为判定条件是 `if args.dataset_name and not args.dataset_mixture:`（[data.py:L21](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/data.py#L21)），`dataset_mixture` 非空时该条件为假，落到 `elif args.dataset_mixture:`。`dataset_name` 在这种情况下被忽略。

### 4.3 混合数据集：加载、裁列、按权重抽样、拼接

#### 4.3.1 概念说明

混合分支是本讲的核心。它把多个来源的数据「调和」成一个训练集：每个来源可以单独指定用哪个 split、保留哪些列、只用多少比例，然后把它们上下拼接（`concatenate`）成一张大表，再整体洗牌。这正是 open-r1 在做大规模 SFT 时「把不同领域、不同来源的推理数据按设计比例混合」的基础设施。

#### 4.3.2 核心流程

对 `dataset_mixture.datasets` 里的**每个**成员 `dc`：

```
ds = load_dataset(dc.id, dc.config, split=dc.split)   # 只加载指定分片
if dc.columns is not None:
    ds = ds.select_columns(dc.columns)                # 列裁剪
if dc.weight is not None:
    ds = ds.shuffle(seed).select(range(int(len(ds) * dc.weight)))  # 按比例随机抽样
datasets_list.append(ds)

combined = concatenate_datasets(datasets_list)        # 上下拼接（要求列一致）
combined = combined.shuffle(seed)                     # 整体再洗牌一次
```

抽样这一步的样本数是确定的（与随机种子无关的是「数量」，「具体抽到哪些」才依赖 seed）：

\[ N_{\text{combined}} = \sum_{i} \lfloor N_i \cdot w_i \rfloor \]

其中 \(\lfloor\cdot\rfloor\) 由 `int()` 实现（向零截断）。例如 100 条数据、`weight=0.25`，则保留 `int(100*0.25)=25` 条。

#### 4.3.3 源码精读

进入混合分支，先取出全局 `seed`，准备空列表：

[data.py:L24-L27](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/data.py#L24-L27) — 进入混合分支，记录成员数量，取出 `seed`，准备 `datasets_list`。

逐个加载成员（注意这里**传了 `split`**，与单数据集分支不同）：

[data.py:L29-L35](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/data.py#L29-L35) — 用 `dc.id / dc.config / dc.split` 调用 `load_dataset`，所以混合分支每个成员加载的是**单个分片**。

可选的列裁剪：

[data.py:L36-L37](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/data.py#L36-L37) — 当 `columns` 非 `None` 时调用 `select_columns` 丢掉不需要的列。这一步配合 4.1 的列一致性校验，保证各成员最终列集合相同，才能拼接。

关键的权重抽样：

[data.py:L38-L42](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/data.py#L38-L42) — 当 `weight` 非 `None` 时，先 `shuffle(seed)` 再 `select(range(int(len(ds)*weight)))`。两个细节：(1) 用的是**同一个** `seed`（即整个混合的 seed），所以不同成员用相同种子各自洗牌后再取前 k 条；(2) 数量由 `int(len(ds)*weight)` 截断决定，与 [test_data.py:L52-L67](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/utils/test_data.py#L52-L67) 中 `len//4 + len//2` 的断言一致。

拼接 + 整体洗牌：

[data.py:L44-L49](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/data.py#L44-L49) — 用 `concatenate_datasets` 把所有成员上下拼成一张大表，再用同一个 `seed` 整体 `shuffle` 一次，打乱不同来源的先后顺序，并记录最终样本数。

#### 4.3.4 代码实践

**实践目标**：构造一个混合了 `trl-internal-testing/zen` 的 `train` 与 `test` 两个分片的配方，设置不同 `weight`，验证拼接后样本数等于各成员截断后数量之和。

操作步骤（对照 `test_weighted_mixture`）：

```python
from open_r1.configs import ScriptArguments
from open_r1.utils import get_dataset
from datasets import load_dataset

mix = {
    "datasets": [
        {"id": "trl-internal-testing/zen", "config": "conversational_preference",
         "split": "train", "weight": 0.25},
        {"id": "trl-internal-testing/zen", "config": "conversational_preference",
         "split": "test", "weight": 0.5},
    ]
}
args = ScriptArguments(dataset_mixture=mix)

# 手算期望值
ref = load_dataset("trl-internal-testing/zen", "conversational_preference")
expected = len(ref["train"]) // 4 + len(ref["test"]) // 2

ds = get_dataset(args)
print("期望:", expected, "实际:", len(ds["train"]))
assert len(ds["train"]) == expected
```

需要观察的现象：`get_dataset` 返回的 `DatasetDict` 只有 `train` 一个键（因为没设 `test_split_size`，见 4.4），其长度精确等于 `train//4 + test//2`。

预期结果：断言通过，与 [test_data.py:L52-L67](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/utils/test_data.py#L52-L67) 完全一致。若不设 `weight`（为 `None`），则等价于 `test_unweighted_mixture`，样本数为 `len(train)+len(test)`（[test_data.py:L37-L50](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/utils/test_data.py#L37-L50)）。无法联网下载时「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：把上面例子里两个成员的 `weight` 都设为 `None`，最终样本数是多少？
**答案**：两个成员都不抽样，各保留全量，拼接后等于 `len(ref["train"]) + len(ref["test"])`。这正是 `test_unweighted_mixture` 验证的。

**练习 2**：如果两个成员列不同（一个只有 `prompt`，一个只有 `chosen`），会在哪一步报错？是 `get_dataset` 还是更早？
**答案**：更早——在 `ScriptArguments.__post_init__` 的列一致性校验（[configs.py:L112-L120](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L112-L120)）就抛错，根本进不了 `get_dataset`。对应 `test_mixture_with_mismatched_columns`。

### 4.4 train_test_split 切分与返回结构

#### 4.4.1 概念说明

拼接并洗牌后，`get_dataset()` 还提供最后一个旋钮：`test_split_size`。给了它，函数会把混合后的大表再切出一份验证集，返回同时含 `train` 和 `test` 的 `DatasetDict`；不给，则把整张大表都放进 `train`。这一步常用于在混合数据上做轻量评估。

#### 4.4.2 核心流程

```
combined = concatenate + shuffle  (4.3 的产物)
if test_split_size is not None:
    return combined.train_test_split(test_size=test_split_size, seed=seed)  # 含 train/test
else:
    return DatasetDict({"train": combined})  # 只有 train
```

`train_test_split` 是 `datasets` 对 sklearn 同名函数的封装：当 `test_size` 是 0~1 的浮点时，`test` 的大小按比例取（sklearn 用「向上取整」确定 test 条数，`train = 总数 - test`）。

#### 4.4.3 源码精读

切分分支：

[data.py:L51-L60](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/data.py#L51-L60) — 当 `test_split_size` 非 `None` 时调用 `train_test_split(test_size, seed)`，返回含 `train`/`test` 两个键的 `DatasetDict`；否则把整张大表包成 `DatasetDict({"train": combined})` 返回。注意切分用的也是同一个 `seed`。

两条兜底错误：

[data.py:L61-L65](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/data.py#L61-L65) — 若 `datasets_list` 为空（理论上不会发生，因为配置层已校验），或在既无 `dataset_name` 又无 `dataset_mixture` 时（同样已被 `__post_init__` 拦截），抛 `ValueError`。这是防御性编程的「双保险」。

#### 4.4.4 代码实践

**实践目标**：用 `test_split_size` 切出验证集，并验证 `train + test` 的总数等于混合后的总数（即切分只是 partition，不丢样本）。

操作步骤（对照 `test_mixture_and_test_split`）：

```python
from open_r1.configs import ScriptArguments
from open_r1.utils import get_dataset

mix = {
    "datasets": [
        {"id": "trl-internal-testing/zen", "config": "conversational_preference",
         "split": "train[:10]"},  # 只取前 10 条，方便手算
    ],
    "test_split_size": 0.2,
}
args = ScriptArguments(dataset_mixture=mix)
ds = get_dataset(args)
print(len(ds["train"]), len(ds["test"]))
assert len(ds["train"]) + len(ds["test"]) == 10   # 切分不丢样本
```

需要观察的现象：返回的 `DatasetDict` 同时含 `train` 与 `test` 两个键；总数仍是 10（切分前后样本守恒）。在 `test_size=0.2`、总数 10 时，sklearn 把 test 条数向上取整为 `ceil(0.2*10)=2`，train 为 8。

预期结果：`len(train)==8`、`len(test)==2`，与 [test_data.py:L69-L83](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/utils/test_data.py#L69-L83) 完全一致。不同 `datasets`/sklearn 版本下 test 条数的取整方式若不同，则以本地实际输出为准（「待本地验证」取整细节）。

#### 4.4.5 小练习与答案

**练习 1**：混合 13 条样本、`test_split_size=0.1`，`test` 会有几条？
**答案**：sklearn 对浮点 `test_size` 取 `ceil(0.1*13)=ceil(1.3)=2` 条，`train` 为 11 条。注意是向上取整，而非 `int()` 截断——这与 4.3 里 `weight` 抽样用的 `int()` 截断是两套规则，别混淆。

**练习 2**：单数据集分支（`dataset_name`）能不能用 `test_split_size` 切验证集？
**答案**：不能。`test_split_size` 是 `DatasetMixtureConfig` 的字段，只在混合分支（[data.py:L51-L54](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/data.py#L51-L54)）被读取。单数据集分支直接返回 `load_dataset` 的原始结果，验证集需由数据集本身提供（或训练配置里另行处理）。

## 5. 综合实践

把三个旋钮（`weight` / `columns` / `test_split_size`）串起来，完成一次「端到端」的数据混合。

1. 在仓库根目录新建 `mixture_demo.yaml`（**示例代码**，参照 README 的官方模板）：

   ```yaml
   dataset_mixture:
     datasets:
       - id: trl-internal-testing/zen
         config: conversational_preference
         split: train
         columns: ["prompt", "chosen"]
         weight: 0.4
       - id: trl-internal-testing/zen
         config: conversational_preference
         split: test
         columns: ["prompt", "chosen"]
         weight: 0.6
     seed: 42
     test_split_size: 0.1
   ```

2. 编写运行脚本 `practice_mixture.py`（**示例代码**）读取该 YAML，复用项目真实的 `ScriptArguments` 与 `get_dataset`：

   ```python
   import yaml
   from datasets import load_dataset
   from open_r1.configs import ScriptArguments
   from open_r1.utils import get_dataset

   with open("mixture_demo.yaml") as f:
       cfg = yaml.safe_load(f)

   # __post_init__ 会把字典转成 DatasetMixtureConfig 并做校验
   args = ScriptArguments(dataset_mixture=cfg["dataset_mixture"])

   # 手算：切分前混合总数 = int(N_train*0.4) + int(N_test*0.6)
   ref = load_dataset("trl-internal-testing/zen", "conversational_preference")
   expected_total = int(len(ref["train"]) * 0.4) + int(len(ref["test"]) * 0.6)

   ds = get_dataset(args)
   actual_total = len(ds["train"]) + len(ds["test"])  # 切分只是 partition
   print(f"期望混合总数={expected_total}, 实际(train+test)={actual_total}")
   assert actual_total == expected_total, "样本数与权重公式不符！"
   assert ds["train"].column_names == ["prompt", "chosen"], "列裁剪未生效！"
   print("通过：权重抽样 + 列裁剪 + 切分 均符合预期。")
   ```

3. 运行 `PYTHONPATH=src python practice_mixture.py`。

需要观察与解释的现象：

- **样本守恒**：`train+test` 等于按权重截断后的混合总数，证明 `train_test_split` 只是切分、不丢样本。
- **列裁剪生效**：最终 `column_names` 只剩 `prompt` / `chosen`，证明 `select_columns` 起作用（也佐证了两个成员列一致才能拼接）。
- **可复现**：连续运行两次结果完全一致，因为所有 shuffle/split 共用 `seed: 42`。

预期结果：两个 `assert` 均通过。若本地无网络无法下载 `zen`，可把 `id` 换成任意本地可见的小数据集重做；具体条数「待本地验证」。

## 6. 本讲小结

- `get_dataset()` 有两条互斥分支：`dataset_name`（薄封装 `load_dataset`，不裁列/不抽样/不切分）与 `dataset_mixture`（加载 → `select_columns` → 按 `weight` 抽样 → `concatenate` → 洗牌 → 可选切分）。
- YAML 里的 `dataset_mixture` 字典在 `ScriptArguments.__post_init__` 里被翻译成 `DatasetMixtureConfig`，并经过「二选一」「结构合法」「列一致」三道校验。
- `weight` 抽样的数量由 `int(len(ds)*weight)` **截断**决定；抽样「抽到哪些」依赖统一的 `seed`，保证可复现。
- `concatenate_datasets` 要求列一致——这正是配置阶段做列一致性校验的原因。
- `test_split_size` 只在混合分支生效，用 sklearn 的 `train_test_split`（test 条数**向上取整**），切分前后样本守恒。
- 单数据集分支与混合分支在「是否传 split、是否裁列/抽样/切分」上存在显著不对称，是阅读与排错时最容易踩坑的地方。

## 7. 下一步学习建议

- 数据准备好后，接下来要进入训练器。建议阅读 [u2-l1 SFT 训练脚本主流程](u2-l1-sft-script-walkthrough.md)，看 `get_dataset(args)` 的返回值如何被喂给 `SFTTrainer`。
- 若想了解模型与分词器如何加载（`get_model` / `get_tokenizer`，与 `get_dataset` 同在 `utils/__init__.py` 导出），继续看 u2-l3。
- 想看更多混合数据集的边界用例，直接精读 [tests/utils/test_data.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/utils/test_data.py)，七个用例覆盖了本讲所有分支。
- 进阶可关注 [scripts/decontaminate.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/scripts/decontaminate.py)：混合外部数据时，常用它做 n-gram 去污染，避免训练集泄漏进评估基准。
