# 数据集加载与注册

## 1. 本讲目标

本讲是「数据集处理」单元的第一篇，专注回答一个问题：

> 当你在命令行写下 `--dataset swift/self-cognition#500` 这样一串字符串时，ms-swift 内部究竟发生了什么，它最终是怎么变成一个可以喂给训练器的数据集对象的？

学完本讲你应该能够：

- 说清 `DATASET_MAPPING` 注册表的结构，以及「导入即注册」的两条路径。
- 跟踪 `DatasetLoader` 从本地文件或 ModelScope/HuggingFace Hub 加载数据的完整流程。
- 解读 `DatasetSyntax` 的数据集字符串语法（`::`、`:`、`/`、`#` 四个符号分别代表什么）。
- 自己写一段 Python 代码调用 `load_dataset` 加载内置数据集与本地 jsonl。

本讲只讲「数据怎么进来」，**不讲**数据进来之后如何被清洗、编码、打包——那是 u4-l2（预处理器）和 u4-l3（编码与 Packing）的主题。

## 2. 前置知识

阅读本讲前，你需要具备以下认知（在前面讲义中已建立）：

- **参数体系**：ms-swift 用 dataclass 组合出统一的 `Arguments` 对象，`--dataset` 属于 `DataArguments` 里的一个字段（见 u2-l1）。
- **统一扩展范式**：全项目遵循「基类（`base.py`）+ 注册表（`mapping.py` 的 `*_MAPPING`）+ CLI 参数开关」三件套（见 u1-l3）。本讲的 `DATASET_MAPPING` 正是这一范式的又一个实例。
- **对话格式**：训练样本最终要被 Template 编码成 token 序列（见 u3-l3）。本讲加载出来的数据，终点就是交给 Template。

还需要一点额外背景：ms-swift 的数据层底层依赖 HuggingFace 的 `datasets` 库（`Dataset`、`load_dataset`、`concatenate_datasets` 等）。本讲里出现的 `HfDataset` 就是 `datasets.Dataset`。ms-swift 并没有重造一个数据集容器，而是在 `datasets` 之上封装了「注册 + 加载 + 预处理」的胶水层。

一个容易混淆的点先点明：你会看到三个名字相近的东西——

| 名字 | 是什么 | 在哪里 |
| --- | --- | --- |
| `load_dataset`（ms-swift） | ms-swift 自己的顶层加载函数 | `swift/dataset/loader.py` |
| `hf_load_dataset` | HuggingFace `datasets.load_dataset`，被 ms-swift 内部复用 | `swift/dataset/loader.py` 顶部导入 |
| `hub.load_dataset` | ModelScope/HF Hub 客户端的下载方法 | `swift/hub` |

本讲的 `load_dataset` 若无特别说明，**均指 ms-swift 自己的那个**。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [swift/dataset/register.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/register.py) | 提供 `register_dataset` / `register_dataset_info`，把 `DatasetMeta` 写进 `DATASET_MAPPING`。 |
| [swift/dataset/dataset_meta.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/dataset_meta.py) | 定义 `DatasetMeta`、`SubsetDataset`、`BaseDatasetLoader`，以及全局空字典 `DATASET_MAPPING`。 |
| [swift/dataset/dataset_syntax.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/dataset_syntax.py) | 定义 `DatasetSyntax`，负责把命令行里的数据集字符串解析成结构化字段。 |
| [swift/dataset/loader.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/loader.py) | 定义 `DatasetLoader`（真正的加载器）与顶层 `load_dataset`（编排函数）。 |
| [swift/dataset/__init__.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/__init__.py) | 包入口，触发「导入即注册」。 |
| [swift/dataset/data/dataset_info.json](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/data/dataset_info.json) | 内置数据集的清单（一个 JSON 数组），每项描述一个数据集。 |
| [swift/dataset/dataset/llm.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/dataset/llm.py) | 用 Python 代码显式注册一部分需要特殊预处理的数据集（如 `self-cognition`）。 |

## 4. 核心概念与源码讲解

### 4.1 DATASET_MAPPING 注册表与注册机制

#### 4.1.1 概念说明

ms-swift 内置了上百个数据集（官方说 150+）。你不必每次都写一长串仓库 id（如 `AI-ModelScope/alpaca-gpt4-data-zh`），可以直接用一个**短名**或者**仓库 id** 来引用它们。要做到这一点，框架必须维护一张「数据集名 → 元信息」的表，这就是 `DATASET_MAPPING`。

这张表解决三个问题：

1. **别名**：同一个数据集，ModelScope 上的 id 和 HuggingFace 上的 id 往往不同（例如 `swift/self-cognition` 对应 HF 的 `modelscope/self-cognition`）。表里同时记录两个 id，框架按当前用的是哪个 Hub 来取。
2. **预处理器绑定**：每个数据集的原始格式千差万别（alpaca 三字段、messages 多轮、sharegpt……），表里记录了该数据集该用哪个 `preprocess_func` 把它规范化成统一格式。
3. **子集与切分**：一个仓库可能含多个子集（subset）和多个切分（split），表里记录了默认用哪些。

#### 4.1.2 核心流程

`DATASET_MAPPING` 本身只是一个普通的 Python 字典，定义在 `dataset_meta.py` 最末尾，初始为空：

```python
DATASET_MAPPING: Dict[Tuple[str, str, str], DatasetMeta] = {}
```

它被填满依靠两条「**导入即注册**」的路径（与 u3-l1 模型注册、u3-l3 模板注册是同一套范式）：

```text
import swift.dataset
      │
      ├── swift/dataset/__init__.py 执行：
      │       ├── from . import dataset          ── 触发路径 B
      │       └── register_dataset_info()        ── 触发路径 A
      │
      ├── 路径 A：读 data/dataset_info.json（一个大 JSON 数组）
      │       └── 对每一项调用 _register_d_info → DatasetMeta → register_dataset
      │
      └── 路径 B：dataset/__init__.py → from . import llm, mllm
              └── llm.py / mllm.py 顶层执行大量 register_dataset(DatasetMeta(...))
```

两条路径最终都汇到同一个函数 `register_dataset`，它把 `DatasetMeta` 写进字典。注意 `DATASET_MAPPING` 的 key 有**两种形态**：

- 当 `DatasetMeta.dataset_name` **有值**时，key 就是这个字符串（如 `'self-cognition'`）。
- 当 `dataset_name` 为 `None`时，key 是一个三元组 `(ms_dataset_id, hf_dataset_id, dataset_path)`。

这一点很关键，后面 `DatasetSyntax` 反查时需要特别处理。

#### 4.1.3 源码精读

先看注册的「终点」`register_dataset`：它负责查重并写入字典。

[swift/dataset/register.py:26-40](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/register.py#L26-L40) 中文说明：根据是否有 `dataset_name` 决定 key 形态，默认禁止重名（防呆），最后写入 `DATASET_MAPPING`。

```python
def register_dataset(dataset_meta: DatasetMeta, *, exist_ok: bool = False) -> None:
    if dataset_meta.dataset_name:
        dataset_name = dataset_meta.dataset_name          # 字符串 key
    else:
        dataset_name = dataset_meta.ms_dataset_id, dataset_meta.hf_dataset_id, dataset_meta.dataset_path  # 三元组 key
    if not exist_ok and dataset_name in DATASET_MAPPING:
        raise ValueError(f'The `{dataset_name}` has already been registered in the DATASET_MAPPING.')
    DATASET_MAPPING[dataset_name] = dataset_meta
```

再看 `DatasetMeta` 本身——它就是一个装满元信息的 dataclass：

[swift/dataset/dataset_meta.py:173-199](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/dataset_meta.py#L173-L199) 中文说明：`DatasetMeta` 描述一个数据集「从哪来、怎么切、怎么预处理」；`__post_init__` 里若未指定 `loader`，则默认用 `DatasetLoader`，并把字符串形式的 subset 包成 `SubsetDataset` 对象。

```python
@dataclass
class DatasetMeta:
    ms_dataset_id: Optional[str] = None
    hf_dataset_id: Optional[str] = None
    dataset_path: Optional[str] = None   # 也可以是本地目录
    dataset_name: Optional[str] = None
    ms_revision: Optional[str] = None
    hf_revision: Optional[str] = None
    subsets: List[Union[SubsetDataset, str]] = field(default_factory=lambda: ['default'])
    split: List[str] = field(default_factory=lambda: ['train'])
    preprocess_func: PreprocessFunc = field(default_factory=lambda: AutoPreprocessor())
    loader: Optional[BaseDatasetLoader] = None
    ...
```

路径 A（JSON 清单）由 `register_dataset_info` 驱动。当不传参时，它默认读包内自带的 `data/dataset_info.json`：

[swift/dataset/register.py:84-115](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/register.py#L84-L115) 中文说明：支持文件路径 / JSON 字符串 / 默认清单三种输入；逐项交给 `_register_d_info` 转成 `DatasetMeta` 并注册。

```python
def register_dataset_info(dataset_info: Union[str, List[str], None] = None) -> List[DatasetMeta]:
    if dataset_info is None:
        dataset_info = os.path.join(os.path.dirname(__file__), 'data', 'dataset_info.json')
    ...
    for d_info in dataset_info:
        res.append(_register_d_info(d_info, base_dir=base_dir))
```

JSON 数组里每一项通常很简短，比如（取自 `data/dataset_info.json` 开头）：

[swift/dataset/data/dataset_info.json:7-11](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/data/dataset_info.json#L7-L11) 中文说明：一个多语言数据集条目，只给了 `ms_dataset_id`、`subsets` 列表和 `tags`，其余字段（preprocess_func 等）由 `_preprocess_d_info` 自动补默认值。

```json
{
    "ms_dataset_id": "damo/nlp_polylm_multialpaca_sft",
    "subsets": ["ar", "de", "es", "fr", "id", "ja", "ko", "pt", "ru", "th", "vi"],
    "tags": ["chat", "general", "multilingual"]
}
```

注意这个条目**没有** `dataset_name`，所以它的 key 是三元组。JSON 清单里大量数据集都是这种形态。`_preprocess_d_info` 会做一件重要的事：根据是否有 `messages` 字段，决定挂 `MessagesPreprocessor` 还是 `AutoPreprocessor`：

[swift/dataset/register.py:43-69](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/register.py#L43-L69) 中文说明：把字典形式的配置翻译成 `DatasetMeta` 能接受的字段，重点是把 `messages`/`columns` 这些子配置实例化成预处理器对象。

```python
if 'messages' in d_info:
    d_info['preprocess_func'] = MessagesPreprocessor(**d_info.pop('messages'), columns=columns)
else:
    d_info['preprocess_func'] = AutoPreprocessor(columns=columns)
```

路径 B（Python 代码注册）用于需要**自定义预处理类**的数据集。最典型的就是 `self-cognition`（自我认知数据集），它需要一个能注入「模型名/作者名」的 `SelfCognitionPreprocessor`：

[swift/dataset/dataset/llm.py:912-926](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/dataset/llm.py#L912-L926) 中文说明：用 Python 代码注册 `self-cognition`，显式给了 `dataset_name='self-cognition'`（所以它的 key 是字符串），并为不同子集挂不同的预处理器（默认、qwen3 专用、empty_think）。

```python
register_dataset(
    DatasetMeta(
        ms_dataset_id='swift/self-cognition',
        hf_dataset_id='modelscope/self-cognition',
        subsets=[
            SubsetDataset(preprocess_func=SelfCognitionPreprocessor()),
            SubsetDataset('qwen3', preprocess_func=SelfCognitionPreprocessor(...)),
            SubsetDataset('empty_think', preprocess_func=SelfCognitionPreprocessor(...)),
        ],
        dataset_name='self-cognition',
        tags=['chat', 'self-cognition', '🔥']))
```

最后，这一切的「自动启动开关」在包入口 `__init__.py` 的最后一行——这就是为什么你只要 `import swift`，注册表就已经填满了：

[swift/dataset/__init__.py:18](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/__init__.py#L18) 中文说明：导入数据集包时无条件执行一次 `register_dataset_info()`，把内置清单全部注册进 `DATASET_MAPPING`。

```python
register_dataset_info()
```

而上面 `self-cognition` 所在的 `llm.py`，是靠 `__init__.py` 里的 `from . import dataset`（再触发 `dataset/__init__.py` 的 `from . import llm, mllm`）被导入的——模块被导入时，顶层那些 `register_dataset(...)` 语句就会执行。这就是「导入即注册」。

#### 4.1.4 代码实践

**实践目标**：直观看到 `DATASET_MAPPING` 被填满，并区分两种 key 形态。

**操作步骤**：

1. 在项目根目录启动 Python（确保已按 u1-l2 安装 ms-swift）。
2. 执行下面这段「示例代码」：

```python
# 示例代码：探查 DATASET_MAPPING
from swift.dataset.register import DATASET_MAPPING

print('已注册数据集总数:', len(DATASET_MAPPING))

# 统计 key 形态
str_keys = [k for k in DATASET_MAPPING if isinstance(k, str)]
tuple_keys = [k for k in DATASET_MAPPING if isinstance(k, tuple)]
print('字符串 key（带 dataset_name）数量:', len(str_keys), '例如:', str_keys[:3])
print('三元组 key 数量:', len(tuple_keys), '例如:', tuple_keys[:1])

# 取 self-cognition 的元信息
meta = DATASET_MAPPING['self-cognition']
print('self-cognition 的 ms/hf id:', meta.ms_dataset_id, '/', meta.hf_dataset_id)
print('它的子集:', [(s.name, s.subset) for s in meta.subsets])
```

**需要观察的现象**：

- 总数应当是上百条。
- 字符串 key 数量很少（只有那些显式写了 `dataset_name` 的，如 `self-cognition`），三元组 key 占绝大多数。
- `self-cognition` 有 3 个子集（`default`/`qwen3`/`empty_think`）。

**预期结果**：`len(DATASET_MAPPING)` 远大于 100；`self-cognition` 能被字符串 key 直接取到。具体数值**待本地验证**（取决于本机已注册内容）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `data/dataset_info.json` 里的数据集大多用三元组作 key，而 `self-cognition` 用字符串 key？

> **答案**：JSON 清单里的数据集没有 `dataset_name` 字段，`register_dataset` 就回退到 `(ms_dataset_id, hf_dataset_id, dataset_path)` 三元组作 key；`self-cognition` 在 `llm.py` 里显式设置了 `dataset_name='self-cognition'`，所以用字符串 key。后者方便用短名直接引用（`DATASET_MAPPING['self-cognition']`）。

**练习 2**：如果你想让一个数据集同时出现在 ModelScope 和 HuggingFace 上都能加载，需要在 `DatasetMeta` 里填哪两个字段？

> **答案**：`ms_dataset_id` 和 `hf_dataset_id`。加载时框架会根据当前使用的是哪个 Hub（由环境变量 `USE_HF` 或语法前缀 `hf::`/`ms::` 决定）选择对应的 id。

---

### 4.2 DatasetLoader 加载流程

#### 4.2.1 概念说明

注册表只告诉我们「这个数据集是什么、在哪、怎么预处理」，真正把数据**读进内存**的活儿由 `DatasetLoader` 干。它继承自抽象基类 `BaseDatasetLoader`，后者定义了加载器的契约（`load` 方法）和一些通用工具（拼接、采样、切分）。

`DatasetLoader` 要处理两种来源：

- **本地文件**（`path` 类型）：用户直接给一个 `.jsonl`/`.json`/`.csv`/`.txt` 路径。
- **仓库**（`repo` 类型）：给一个 ModelScope/HF 仓库 id，或一个本地目录，框架去下载/读取，可能含多个 subset 与 split。

#### 4.2.2 核心流程

`DatasetLoader.load` 是一个分派器，按 `dataset_type` 走两条分支：

```text
DatasetLoader.load(dataset_syntax, dataset_meta)
        │
        ├── dataset_type == 'path'
        │       └── _load_dataset_path
        │             ├── 按扩展名选 file_type（jsonl→json, txt→text）
        │             ├── hf_load_dataset(file_type, data_files=路径)
        │             ├── 可选: 重命名列（columns 映射）
        │             ├── dataset_meta.preprocess_func(dataset)  ── 规范化
        │             └── 删除无用列
        │
        └── dataset_type == 'repo'
                ├── _select_subsets  ── 决定加载哪些 subset
                └── 对每个 subset:
                      └── _load_repo_dataset
                            ├── 判定本地目录 / Hub 下载
                            ├── hub.load_dataset(id, subset, split) ── 带重试
                            ├── 可选: 重命名列
                            ├── subset.preprocess_func(dataset)
                            └── 删除无用列
                最后 concat_datasets 拼接所有 subset/split
```

而 `DatasetLoader.load` 本身又是被顶层函数 `load_dataset` 编排的。完整的端到端流程是：

```text
load_dataset(['swift/self-cognition#500'], ...)        ← 顶层入口
   │
   ├── 对每个数据集字符串:
   │     ├── DatasetSyntax.parse(str)                   ← 解析语法（4.3 节）
   │     ├── 匹配 DatasetMeta（先查 DATASET_MAPPING）
   │     ├── 实例化 dataset_meta.loader(...)
   │     ├── loader.load(dataset_syntax, dataset_meta)  ← 本节的分派器
   │     └── loader.post_process(...)                   ← 按 #500 采样、按比例切训练/验证
   │
   ├── （多数据集时）concat 或 interleave 拼接
   └── 可选 shuffle
   → 返回 (train_dataset, val_dataset)
```

#### 4.2.3 源码精读

先看分派器 `DatasetLoader.load`，它只有十几行，逻辑非常清晰：

[swift/dataset/loader.py:162-187](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/loader.py#L162-L187) 中文说明：按 `dataset_type` 分两支——本地路径走 `_load_dataset_path`；仓库走「选子集 + 逐子集加载 + 拼接」。注意 repo 分支里 `revision` 会根据 `use_hf` 选 `hf_revision` 或 `ms_revision`。

```python
def load(self, dataset_syntax, dataset_meta, *, use_hf=None):
    if dataset_syntax.dataset_type == 'path':
        dataset = self._load_dataset_path(dataset_syntax.dataset, dataset_meta=dataset_meta)
    else:
        subsets = self._select_subsets(dataset_syntax.subsets, dataset_meta)
        revision = dataset_meta.hf_revision if use_hf else dataset_meta.ms_revision
        datasets = []
        for subset in subsets:
            dataset = self._load_repo_dataset(dataset_syntax.dataset, subset, use_hf=use_hf, revision=revision)
            datasets.append(dataset)
        dataset = self.concat_datasets(datasets)
    return dataset
```

本地文件分支 `_load_dataset_path`：核心是复用 HuggingFace 的 `load_dataset`，根据扩展名映射文件类型（`.jsonl` 当作 `json`、`.txt` 当作 `text`）：

[swift/dataset/loader.py:46-69](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/loader.py#L46-L69) 中文说明：用 `hf_load_dataset` 读取本地文件，随后做「列重命名 → 预处理 → 删无用列」三步流水线；`safe_ddp_context` 保证多卡训练时只有一个进程真正读盘，其余进程复用缓存。

```python
ext = os.path.splitext(dataset_path)[1].lstrip('.')
file_type = {'jsonl': 'json', 'txt': 'text'}.get(ext) or ext
...
with safe_ddp_context(None, True):
    kwargs['cache_dir'] = os.path.join(get_cache_dir(), 'datasets')
    dataset = hf_load_dataset(file_type, data_files=dataset_path, **kwargs)
if self.columns:
    dataset = RowPreprocessor.safe_rename_columns(dataset, self.columns)
dataset = dataset_meta.preprocess_func(dataset, ...)
if self.remove_unused_columns:
    dataset = RowPreprocessor.remove_useless_columns(dataset)
```

仓库分支 `_load_repo_dataset` 稍复杂，因为它要区分「本地目录」与「远程 Hub 仓库」，并支持重试与流式：

[swift/dataset/loader.py:71-140](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/loader.py#L71-L140) 中文说明：本地目录直接读（还会临时改名 `dataset_infos.json` 避免冲突），远程仓库用 `hub.load_dataset` 下载并带最多 3 次重试；对每个 split 分别加载后拼接。`retry` 在本地为 1、远程为 3。

```python
if os.path.isdir(dataset_id):
    retry = 1
    ...
else:
    retry = 3
    load_context = partial(safe_ddp_context, hash_id=dataset_id, use_barrier=True)
...
hub = get_hub(use_hf)
for split in subset.split:
    ...
    while True:
        try:
            dataset = hub.load_dataset(dataset_id, subset.subset, split, ...)
        except Exception as e:
            if i == retry:
                raise
            i += 1
        else:
            break
```

子集选择 `_select_subsets` 有一套优先级规则：不指定子集时，若只有一个子集就用它，有多个且有 `default` 就用 `default`，否则报错让你显式指定；指定 `all` 时会跳过被标记为「弱子集」（`is_weak_subset`）的项：

[swift/dataset/loader.py:142-160](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/loader.py#L142-L160) 中文说明：把用户给的子集名解析成 `SubsetDataset` 对象，未在 `dataset_meta.subsets` 中登记的名字会被包成一个「裸」SubsetDataset（用用户给的名字直接当 hub 上的 subset 名），最后 `set_default` 用 meta 的字段补全缺失属性。

```python
if not subsets:
    if len(subset_names) <= 1:
        subsets = subset_names
    elif 'default' in subset_names:
        subsets = ['default']
    else:
        raise ValueError(f'Please provide subsets. available subsets: {subset_names}')
elif len(subsets) == 1 and subsets[0] == 'all' and 'all' not in subset_names:
    subsets = [n for n in subset_names if not subset_mapping[n].is_weak_subset]
```

顶层 `load_dataset` 把上面这些串起来。它的循环体是整条链路的浓缩：

[swift/dataset/loader.py:326-350](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/loader.py#L326-L350) 中文说明：对每个数据集字符串，先 `DatasetSyntax.parse`，再决定 meta 来源——若短名在 `DATASET_MAPPING` 里就直接用（并把 `dataset` 字段替换成真实的仓库 id 或本地路径），否则用 `dataset_syntax.get_dataset_meta` 现场构造一个 meta；然后实例化 loader、调用 `loader.load`、`post_process` 采样切分。

```python
for dataset in datasets:
    dataset_syntax = DatasetSyntax.parse(dataset)
    use_hf = dataset_syntax.use_hf or use_hf_default
    if dataset_syntax.dataset in DATASET_MAPPING:
        dataset_meta = DATASET_MAPPING[dataset_syntax.dataset]
        if dataset_syntax.use_hf is None and dataset_meta.dataset_path is not None:
            dataset_syntax.dataset = dataset_meta.dataset_path
            dataset_syntax.dataset_type = 'path'
        else:
            dataset_syntax.dataset = dataset_meta.hf_dataset_id if use_hf else dataset_meta.ms_dataset_id
    else:
        dataset_meta = dataset_syntax.get_dataset_meta(use_hf)
    loader = dataset_meta.loader(num_proc=num_proc, ...)
    train_dataset = loader.load(dataset_syntax, dataset_meta, use_hf=use_hf)
```

注意上面 `dataset_syntax.dataset in DATASET_MAPPING` 这个判断——因为多数数据集的 key 是三元组而非字符串，**只有那些带 `dataset_name` 的短名（如 `self-cognition`）才能命中这一支**。其余数据集走 `get_dataset_meta` 的反查机制（见 4.3 节）。

最后看 `post_process`，它解释了 `#500` 的真正含义——**采样条数**，而不是训练步数：

[swift/dataset/dataset_meta.py:119-170](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/dataset_meta.py#L119-L170) 中文说明：根据 `dataset_sample` 与 `split_dataset_ratio` 做采样与训练/验证集切分；非流式下用 `train_test_split` 切分后再 `sample_dataset` 采样到目标条数。

```python
if dataset_sample is None:
    dataset_sample = len(train_dataset)
if split_dataset_ratio == 0:
    train_dataset = sample_dataset(train_dataset, dataset_sample, shuffle, random_state)
    val_dataset = None
...
```

#### 4.2.4 代码实践

**实践目标**：用 `load_dataset` 加载一个**本地 jsonl**，观察它如何被读成 `datasets.Dataset`，并验证 `#N` 的采样语义。

**操作步骤**：

1. 在项目根目录建一个临时文件 `/tmp/demo.jsonl`，写入两行（alpaca 格式）：

```bash
# 示例命令：准备一份本地数据
cat > /tmp/demo.jsonl <<'EOF'
{"instruction":"你好","output":"你好，我是助手。"}
{"instruction":"1+1=?","output":"1+1=2。"}
EOF
```

2. 执行下面这段「示例代码」：

```python
# 示例代码：加载本地 jsonl
from swift.dataset import load_dataset

# 不带 #N：加载全部
train_ds, val_ds = load_dataset('/tmp/demo.jsonl', split_dataset_ratio=0.)
print('全部条数:', len(train_ds))
print('第一行:', train_ds[0])

# 带 #1：只采样 1 条（注意路径后缀 #1）
train_ds2, _ = load_dataset('/tmp/demo.jsonl#1', split_dataset_ratio=0.)
print('采样后条数:', len(train_ds2))
```

**需要观察的现象**：

- 第一次 `len(train_ds)` 应为 2（文件里有两条）。
- 第二次 `len(train_ds2)` 应为 1，证明 `#1` 是「采样 1 条」而非「训练 1 步」。
- `train_ds[0]` 里能看到原始的 `instruction`/`output` 字段（此时还未被预处理器规范化成 `messages`，规范化是 u4-l2 的主题；本地的裸 jsonl 默认走 `AutoPreprocessor`）。

**预期结果**：条数分别为 2 和 1。若本机 `datasets` 版本对 `data_files` 行为有差异，以实际输出为准（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：`_load_dataset_path` 为什么要把 `.jsonl` 映射成 `json`、`.txt` 映射成 `text`？

> **答案**：HuggingFace `load_dataset` 的第一个参数是「builder 类型」而非扩展名。`.jsonl` 本质是行分隔的 JSON，用 `json` builder 可正确解析；纯文本要用 `text` builder 一行一条。这个映射是 ms-swift 对 `hf_load_dataset` 的用户体验包装。

**练习 2**：多卡训练时，8 张卡会不会同时去下载/读取同一个数据集导致冲突？

> **答案**：不会。加载过程被 `safe_ddp_context` 包裹，它会用锁/屏障协调，保证只有一个进程真正执行下载或读盘，其余进程复用结果，避免重复 IO 和缓存冲突。

---

### 4.3 DatasetSyntax 数据集语法

#### 4.3.1 概念说明

ms-swift 允许你用一串紧凑的字符串表达「加载哪个数据集、用哪个子集、采样多少、从哪个 Hub」四件事。例如：

```text
hf::AI-ModelScope/alpaca-gpt4-data-zh:default/subset#500
```

这一串里出现了四个符号：`::`、`:`、`/`、`#`。`DatasetSyntax` 就是把这串文本解析成结构化字段（`dataset`、`subsets`、`dataset_sample`、`use_hf`）的解析器。它同时还能反向工作——`get_raw` 把结构化字段重新序列化成字符串。

理解这套语法是读懂 `--dataset` 参数的关键。

#### 4.3.2 核心流程

`DatasetSyntax.parse` 用「从左到右逐层剥离」的方式切分字符串。每一步都先判断 `os.path.exists`，若当前字符串恰好是一个已存在的本地路径，就不再继续切分（避免把路径里合法的 `:` `#` 字符误当作语法分隔符）：

```text
输入: [hf/ms::]dataset_id[:subset1/subset2/...][#sample]

第 1 步  按 '::' 切（仅当不是本地路径）
         → 得到 use_hf（'hf'→True, 'ms'→False, 无前缀→None）
                 与剩余 dataset

第 2 步  按右侧第一个 '#' 切（rsplit，仅当不是本地路径）
         → 得到 other 与 dataset_sample（'#500' → 500）

第 3 步  按左侧第一个 ':' 切（仅当不是本地路径）
         → 得到 dataset 与 subsets 字符串

第 4 步  subsets 用 '/' 切成列表；dataset_sample 转成 int
         → 得到 DatasetSyntax(dataset, subsets, dataset_sample, use_hf)

__post_init__: 若 dataset 是文件 → dataset_type='path'；否则 → 'repo'
```

解析完成后，`get_dataset_meta` 负责把它对应到一个 `DatasetMeta`。由于 `DATASET_MAPPING` 的 key 形态不统一（字符串或三元组），这里另建了一张「二级索引」`(type, id) → meta` 来反查。

#### 4.3.3 源码精读

先看 `DatasetSyntax` 数据类本身和它的 `__post_init__`：

[swift/dataset/dataset_syntax.py:13-29](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/dataset_syntax.py#L13-L29) 中文说明：四个字段 + 一个 `dataset_type` 推断（文件→path，否则→repo）；`get_raw` 是 `parse` 的逆运算，把字段拼回字符串。

```python
@dataclass
class DatasetSyntax:
    dataset: str
    subsets: List[str] = field(default_factory=list)
    dataset_sample: Optional[int] = None
    use_hf: Optional[bool] = None

    def __post_init__(self):
        if os.path.isfile(self.dataset):
            self.dataset_type = 'path'
        else:
            self.dataset_type = 'repo'
```

解析的核心是 `parse`，它反复借助辅助方法 `_safe_split`：

[swift/dataset/dataset_syntax.py:55-79](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/dataset_syntax.py#L55-L79) 中文说明：依次按 `::`、`#`（rsplit）、`:`（split）三段切分；每次切分前都用 `os.path.exists` 短路，保护本地路径不被误切；最后把 subsets 按 `/` 拆成列表、sample 转成整数。

```python
@classmethod
def parse(cls, dataset: str) -> 'DatasetSyntax':
    # hf/ms::dataset_id or dataset_path:subset1/subset2/subset3#dataset_sample
    if os.path.exists(dataset):
        use_hf = None
    else:
        use_hf, dataset = cls._safe_split(dataset, '::', False)
        if isinstance(use_hf, str):
            use_hf = use_hf.lower()
        use_hf = {'hf': True, 'ms': False}.get(use_hf)
    if os.path.exists(dataset):
        other, dataset_sample = dataset, None
    else:
        other, dataset_sample = cls._safe_split(dataset, '#', True, 'right')
    if os.path.exists(other):
        dataset, subsets = other, None
    else:
        dataset, subsets = cls._safe_split(other, ':', True)

    if subsets is not None:
        subsets = [subset.strip() for subset in subsets.split('/')]
    if dataset_sample is not None:
        dataset_sample = int(dataset_sample)
    return cls(dataset.strip(), subsets or [], dataset_sample, use_hf)
```

辅助方法 `_safe_split` 的关键参数是 `use_0`（切出来只有一段时算前段还是后段）与 `split_mode`（`split` 从左、`rsplit` 从右）：

[swift/dataset/dataset_syntax.py:31-53](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/dataset_syntax.py#L31-L53) 中文说明：一个容忍「分隔符不存在」的安全切分函数；找不到分隔符时按 `use_0` 决定把整串归到前段还是后段。

```python
@staticmethod
def _safe_split(s, sep, use_0, split_mode='left'):
    if s is None or len(s) == 0:
        return None, None
    if split_mode == 'left':
        part = s.split(sep, 1)
    else:
        part = s.rsplit(sep, 1)
    if len(part) == 1:
        if use_0:
            part = part[0], None
        else:
            part = None, part[0]
    else:
        assert len(part) == 2
    return part
```

解析出字段后，`get_dataset_meta` 负责找到对应的 `DatasetMeta`。由于 key 形态不统一，它构建了一张二级索引：

[swift/dataset/dataset_syntax.py:81-105](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/dataset_syntax.py#L81-L105) 中文说明：先用 `(type, id)` 精确反查；查不到再用 `_get_matched_dataset_meta` 按「仓库名后缀」模糊匹配；都失败就返回一个空的默认 `DatasetMeta()`（让加载器用默认 AutoPreprocessor 现场处理）。

```python
def get_dataset_meta(self, use_hf: bool):
    dataset_meta_mapping = self._get_dataset_meta_mapping()
    dataset_type = self.dataset_type
    if dataset_type == 'path':
        dataset_meta = dataset_meta_mapping.get((dataset_type, self.dataset))
    else:
        dataset_type = 'repo' if os.path.isdir(self.dataset) else {True: 'hf', False: 'ms'}[use_hf]
        dataset_meta = dataset_meta_mapping.get((dataset_type, self.dataset))
    return dataset_meta or self._get_matched_dataset_meta(dataset_meta_mapping) or DatasetMeta()
```

二级索引 `_get_dataset_meta_mapping` 的构建——它把每个 `DatasetMeta` 按「path / ms / hf」三种来源分别登记，这就绕开了 `DATASET_MAPPING` key 形态不统一的问题：

[swift/dataset/dataset_syntax.py:91-105](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/dataset_syntax.py#L91-L105) 中文说明：遍历 `DATASET_MAPPING` 所有 value，按 `dataset_path`/`ms_dataset_id`/`hf_dataset_id` 三个维度建立 `(type, id) → meta` 索引，并用模块级变量 `_dataset_meta_mapping` 缓存，避免重复构建。

```python
@staticmethod
def _get_dataset_meta_mapping():
    global _dataset_meta_mapping
    if _dataset_meta_mapping is not None:
        return _dataset_meta_mapping
    _dataset_meta_mapping = {}
    for dataset_meta in DATASET_MAPPING.values():
        if dataset_meta.dataset_path is not None:
            dataset_type = 'repo' if os.path.isdir(dataset_meta.dataset_path) else 'path'
            _dataset_meta_mapping[(dataset_type, dataset_meta.dataset_path)] = dataset_meta
        if dataset_meta.ms_dataset_id is not None:
            _dataset_meta_mapping[('ms', dataset_meta.ms_dataset_id)] = dataset_meta
        if dataset_meta.hf_dataset_id is not None:
            _dataset_meta_mapping[('hf', dataset_meta.hf_dataset_id)] = dataset_meta
    return _dataset_meta_mapping
```

把 4.1 节的 `register_dataset` 与这里的 `_get_dataset_meta_mapping` 连起来看，就能理解整个反查设计：

- `register_dataset` 用「短名或三元组」做 `DATASET_MAPPING` 的 key（面向**人类**，方便用短名引用）。
- `_get_dataset_meta_mapping` 用「(来源, 真实 id)」做二级索引（面向**机器**，方便用解析出的仓库 id 反查）。

两张表指向同一批 `DatasetMeta` 对象，各司其职。

#### 4.3.4 代码实践

**实践目标**：用 `DatasetSyntax.parse` 验证你对四个符号的理解。这个实践**不需要下载数据**，纯字符串解析，可直接运行。

**操作步骤**：执行下面这段「示例代码」：

```python
# 示例代码：解析各种数据集字符串
from swift.dataset.dataset_syntax import DatasetSyntax

cases = [
    'swift/self-cognition#500',                       # 短名 + 采样
    'AI-ModelScope/alpaca-gpt4-data-zh:default#500',  # 仓库id + 子集 + 采样
    'hf::open-r1/DAPO-Math-17k-Processed',            # 强制 HF
    'ms::damo/nlp_polylm_multialpaca_sft:ar/de',      # 强制 MS + 多子集
    '/tmp/demo.jsonl',                                # 本地路径（若存在）
]
for c in cases:
    s = DatasetSyntax.parse(c)
    print(f'{c!r:55} -> dataset={s.dataset!r}, subsets={s.subsets}, '
          f'sample={s.dataset_sample}, use_hf={s.use_hf}, type={s.dataset_type}')
```

**需要观察的现象与预期结果**（基于源码逻辑推断，请运行验证）：

| 输入 | dataset | subsets | sample | use_hf | type |
| --- | --- | --- | --- | --- | --- |
| `swift/self-cognition#500` | `swift/self-cognition` | `[]` | 500 | None | repo |
| `AI-ModelScope/alpaca-gpt4-data-zh:default#500` | `AI-ModelScope/alpaca-gpt4-data-zh` | `['default']` | 500 | None | repo |
| `hf::open-r1/DAPO-Math-17k-Processed` | `open-r1/DAPO-Math-17k-Processed` | `[]` | None | True | repo |
| `ms::damo/nlp_polylm_multialpaca_sft:ar/de` | `damo/nlp_polylm_multialpaca_sft` | `['ar','de']` | None | False | repo |
| `/tmp/demo.jsonl`（存在时） | `/tmp/demo.jsonl` | `[]` | None | None | path |

注意最后一行：只要 `/tmp/demo.jsonl` 存在，`parse` 在每一步都会因 `os.path.exists` 短路，从而**保留**路径不被任何符号切分——这就是本地路径安全机制。如果该文件不存在，则会被当作普通 repo 字符串解析。

**预期结果**：表格中的字段值与程序输出一致。`use_hf` 解析失败（无 `::` 前缀）时为 `None`，由上层根据环境变量 `USE_HF` 兜底决定。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `parse` 在每一步切分前都要判断一次 `os.path.exists`？

> **答案**：为了兼容含特殊字符的本地路径。Windows 路径可能含 `:`（如 `C:\...`），某些文件名也可能含 `#`。若不判断就直接按符号切分，会把合法路径切碎。先判断「这是不是一个已存在的文件/目录」，是则整体保留，否则才当作语法字符串切分。

**练习 2**：`get_dataset_meta` 反查失败时返回什么？这意味着什么？

> **答案**：返回一个空的 `DatasetMeta()`（字段全是默认值：默认 `AutoPreprocessor`、`subsets=['default']`、`split=['train']`）。这意味着用户给了一个「未被注册」的数据集 id，框架不会报错，而是用默认配置现场加载并交给 `AutoPreprocessor` 自动推断格式——这就是 ms-swift 能直接加载任意仓库 id 或本地文件的容错设计。

**练习 3**：`dataset_syntax.dataset in DATASET_MAPPING` 这个判断（见 4.2.3）能命中哪些数据集？

> **答案**：只能命中 key 为**字符串**的那些数据集，即带 `dataset_name` 的（如 `self-cognition`）。因为 `dataset_syntax.dataset` 是字符串，而 `DATASET_MAPPING` 多数 key 是三元组，字符串 `in` 字典不会匹配三元组 key。其余数据集靠 `get_dataset_meta` 的二级索引反查。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个端到端的「源码阅读 + 运行」任务：

**任务**：解释命令 `--dataset swift/self-cognition#500` 在数据加载阶段的完整生命周期，并用代码验证关键环节。

**步骤**：

1. **解析阶段**（对应 4.3）：写出 `DatasetSyntax.parse('swift/self-cognition#500')` 的预期结果，然后运行 4.3.4 的示例代码验证。

2. **匹配阶段**（对应 4.1 + 4.3）：说明为什么 `swift/self-cognition` 能被 `dataset_syntax.dataset in DATASET_MAPPING` 直接命中（提示：因为它在 `llm.py` 里注册时带了 `dataset_name='self-cognition'`，但注意 parse 出来的 `dataset` 字段是 `swift/self-cognition`——这里需要你思考「短名」与「ms_dataset_id」的关系；可对照 `load_dataset` 中 `DATASET_MAPPING.get('self-cognition')` 的用法，见 [loader.py:313](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/loader.py#L313)）。

3. **加载阶段**（对应 4.2）：对照 `_load_repo_dataset`，说明框架会用 `hub.load_dataset('swift/self-cognition', 'default', 'train', ...)` 去下载；并指出 `self-cognition` 有 3 个 subset 但默认只加载第一个（`default`），若想加载全部要写 `swift/self-cognition:all`。

4. **采样阶段**（对应 4.2）：对照 `post_process`，说明 `#500` 会让 `dataset_sample=500`，最终从下载的数据里采样 500 条。

5. **验证**：在能联网的环境下运行（**待本地验证**）：

   ```python
   # 示例代码：端到端加载（需联网下载 self-cognition）
   from swift.dataset import load_dataset
   train_ds, val_ds = load_dataset('swift/self-cognition#500',
                                   model_name=('小黄鸭', 'Duck'), model_author=('魔搭', 'ModelScope'))
   print('采样后条数:', len(train_ds))
   print('一条样本:', train_ds[0])
   ```

   预期 `len(train_ds)` 为 500，且样本里能看到注入的模型名/作者名（这正是 `init_self_cognition_preprocessor` 的作用，见 [loader.py:190-214](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/loader.py#L190-L214)）。

> 提示：第 2 步是个小「陷阱」——`parse` 出来的 `dataset` 是 `swift/self-cognition`（ms_dataset_id），并非 `self-cognition`（dataset_name），所以严格来说它不会命中 `in DATASET_MAPPING`，而是走 `get_dataset_meta` 的 `('ms', 'swift/self-cognition')` 二级索引。请用代码（打印 `_get_dataset_meta_mapping()` 中相关条目）确认你的判断。

## 6. 本讲小结

- ms-swift 的数据层是一层「注册 + 加载 + 预处理」的胶水，底层容器复用 HuggingFace `datasets`。
- `DATASET_MAPPING` 是全局注册表，靠「导入即注册」填满：`__init__.py` 末尾的 `register_dataset_info()` 读 JSON 清单，`from . import dataset` 触发 `llm.py`/`mllm.py` 里的代码注册；key 有字符串（带 `dataset_name`）与三元组两种形态。
- `DatasetLoader.load` 是分派器：本地文件走 `_load_dataset_path`（复用 `hf_load_dataset` + 扩展名映射），仓库走「选子集 + 逐子集下载 + 拼接」。
- 顶层 `load_dataset` 编排整条链路：`DatasetSyntax.parse` → 匹配/构造 `DatasetMeta` → `loader.load` → `post_process` 采样切分 → 拼接/交错。
- `DatasetSyntax` 用 `::` / `:` / `/` / `#` 四个符号一串表达「Hub / 子集 / 多子集 / 采样数」；`#500` 是采样 500 条，不是训练 500 步。
- 由于 key 形态不统一，`_get_dataset_meta_mapping` 另建 `(来源, id)` 二级索引做反查，未注册的 id 会兜底成空 `DatasetMeta` + `AutoPreprocessor`。

## 7. 下一步学习建议

本讲只讲到「数据进了门」，原始字段还保持着各自的格式（alpaca 三字段、messages 多轮、纯文本……）。下一步建议：

1. **u4-l2 数据预处理器**：阅读 `swift/dataset/preprocessor/core.py`，看 `RowPreprocessor` / `AlpacaPreprocessor` / `MessagesPreprocessor` / `AutoPreprocessor` 如何把五花八门的原始格式统一成 `messages` 结构——这正是本讲反复出现的 `preprocess_func` 的真身。
2. **u4-l3 编码与 Packing**：看统一的 `messages` 如何被 Template 编码成 `input_ids`/`labels`，以及 `PackingDataset` 如何拼接短样本提升训练效率。
3. **u4-l4 自定义数据集格式**：学习如何用 `register_dataset_info` 把自己的业务数据集注册成内置数据集，实现「短名引用」。

如果想横向对照，可以重读 u3-l1（模型注册）与 u3-l3（模板注册）——它们与本讲的 `DATASET_MAPPING` 是同一套「基类 + 注册表 + 导入即注册」范式，学会一个即会全部。
