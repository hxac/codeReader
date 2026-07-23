# 数据集加载（DatasetConfig 与 Loaders）

## 1. 本讲目标

本讲解决一个核心问题：**genai-bench 在压测前，是怎么把「真实业务语料」读进来、变成一条条请求的？**

学完后你应该能够：

- 看懂 `DatasetConfig` / `DatasetSourceConfig` 这两个 Pydantic 模型的字段含义，知道它们如何描述「一个数据集」。
- 理解 genai-bench 用「两条正交的轴」来组织数据加载：一条轴决定**数据从哪里来**（本地文件 / HuggingFace / 自定义），另一条轴决定**数据被塑形成什么形状**（文本 / 图像）。
- 掌握 `DataLoaderFactory.load_data_for_task` 按任务的**输入模态**分发到不同 loader 的逻辑。
- 了解 `TextDatasetLoader` 与 `ImageDatasetLoader` 如何把原始数据加工成采样器（Sampler）能直接消费的 `List[str]` 或图像结构。
- 自己写脚本，用 `from_cli_args` 构造配置并加载内置的 `sonnet.txt` 数据集。

## 2. 前置知识

在进入本讲前，请确认你已经了解（参见 u2-l1、u2-l2）：

- **任务字符串 `<input>-to-<output>`**：例如 `text-to-text`、`image-text-to-text`。任务的**输入模态**（`-to-` 左边）决定使用哪种采样器和哪种数据 loader；**输出模态**决定生成什么类型的请求（聊天、嵌入、画图等）。
- **场景（Scenario）**：用 `N(...)`、`D(...)` 等微型语言描述每个请求的输入/输出 token 规模。其中有一个特殊的场景字符串叫 `dataset`，它表示「不按分布合成 prompt，而是直接从真实数据集里取原文」。
- **Pydantic 数据契约**：项目用 Pydantic 模型作为模块间统一的数据格式（参见 u1-l5）。本讲的 `DatasetConfig` 就是这种契约的又一实例。

一个直觉性的问题先放在这里：**为什么需要一整套数据加载子系统？** 因为很多基准测试不能用「合成随机文本」，而要用真实语料（客服问答、多模态图文、企业知识库等）才有代表性。genai-bench 因此把「读数据」这件事做成了一个可配置、可扩展、与采样器解耦的独立子系统。

> 小提示：本讲只关心「数据怎么进来」，不关心「数据进来后采样器怎么用」。后者属于 u2-l4（Sampler）。

## 3. 本讲源码地图

本讲涉及的关键源码文件集中在 `genai_bench/data/` 目录下：

| 文件 | 作用 |
| --- | --- |
| `genai_bench/data/config.py` | 定义 `DatasetConfig` 与 `DatasetSourceConfig` 两个 Pydantic 模型，是描述「一个数据集」的数据契约；提供 `from_file` / `from_cli_args` 两种构造入口。 |
| `genai_bench/data/sources.py` | 定义**数据源**抽象 `DatasetSource` 及三种实现（文件 / HuggingFace / 自定义），并由 `DatasetSourceFactory` 按类型创建。解决「数据从哪里来」。 |
| `genai_bench/data/loaders/base.py` | 定义**加载器**抽象基类 `DatasetLoader` 与 `DatasetFormat` 枚举。加载器在内部组合一个数据源，并按模态加工数据。 |
| `genai_bench/data/loaders/factory.py` | 定义 `DataLoaderFactory`，按任务的输入模态把请求分发给文本或图像加载器。 |
| `genai_bench/data/loaders/text.py` | `TextDatasetLoader`：把数据加工成 `List[str]`（一组 prompt）。 |
| `genai_bench/data/loaders/image.py` | `ImageDatasetLoader`：把数据加工成图像 + prompt 结构（目前只支持 HuggingFace 源）。 |
| `genai_bench/data/sonnet.txt` | 内置默认数据集：莎士比亚十四行诗，每行一条 prompt，共 84 条非空行。 |

一句话概括整体关系：

```
DatasetConfig（契约）
        │
        ▼
DataLoaderFactory.load_data_for_task(task, config)   ── 按输入模态分发
        │
   ┌────┴────────────────────┐
   ▼                         ▼
TextDatasetLoader        ImageDatasetLoader        （按模态塑形 / Loader 轴）
   │  内部组合                 │  内部组合
   ▼                         ▼
DatasetSourceFactory.create(config.source)
   │
   ├─ FileDatasetSource        ── 本地 txt/csv/json
   ├─ HuggingFaceDatasetSource ── HuggingFace Hub
   └─ CustomDatasetSource      ── 自定义 loader 类     （数据从哪来 / Source 轴）
```

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：4.1 数据契约 `DatasetConfig`、4.2 数据源 `DatasetSource`、4.3 加载工厂 `DataLoaderFactory`、4.4 loader 实现。

### 4.1 DatasetConfig 模型：描述「一个数据集」

#### 4.1.1 概念说明

要让压测用上真实语料，首先得有一种**统一的方式来描述「我要哪个数据集」**。`DatasetConfig` 就是这个描述符。它不负责真正去读文件，它只承载「配置信息」——读文件这件事交给后面的数据源（4.2）。

`DatasetConfig` 内部嵌套一个 `DatasetSourceConfig`，两者分工是：

- `DatasetSourceConfig`：描述**来源**——是本地文件、HuggingFace 还是自定义 loader？路径在哪？要传什么参数？
- `DatasetConfig`：在来源之上，再补充**与模态相关的列名**——例如 CSV 里哪一列是 prompt、哪一列是图像，以及一个可选的 `prompt_lambda`（用 lambda 表达式动态拼装 prompt）。

#### 4.1.2 核心流程

构造一个 `DatasetConfig` 有两条入口，对应 CLI 的两种用法：

1. **配置文件入口** `from_file(config_path)`：读一个 JSON 文件，直接反序列化成 `DatasetConfig`。对应 CLI 的 `--dataset-config xxx.json`。
2. **命令行参数入口** `from_cli_args(dataset_path, prompt_column, image_column, ...)`：把零散的 CLI 参数拼装成配置。对应 CLI 的 `--dataset-path`、`--dataset-prompt-column` 等。

`from_cli_args` 有一段**智能推断**逻辑，值得记住：

- 如果 `dataset_path` 为空 → 默认用内置 `sonnet.txt`，来源类型 = `file`，格式 = `txt`。
- 如果 `dataset_path` 指向一个**本地真实存在**的路径 → 来源 = `file`，格式按后缀判定（`.csv`/`.txt`/`.json`，其余报错）。
- 如果 `dataset_path` 指向的路径**本地不存在** → 推断它是 HuggingFace 数据集 ID，来源 = `huggingface`。

来源类型 `type` 还会被一个 `field_validator` 校验，只允许 `file` / `huggingface` / `custom` 三种，其余直接报错。

#### 4.1.3 源码精读

`DatasetSourceConfig` 定义了来源相关的全部字段，并用 `validate_type` 限定合法类型：

[genai_bench/data/config.py:10-50](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/data/config.py#L10-L50) —— 定义 `type/path/file_format/huggingface_kwargs/loader_class/loader_kwargs` 六个字段，分别服务于三种来源；`validate_type` 把 `type` 限定在 `{file, huggingface, custom}`。

> 注意：这些字段大量是 `Optional`，因为不同来源只用到其中一部分（文件用 `path`+`file_format`，HuggingFace 用 `path`+`huggingface_kwargs`，自定义用 `loader_class`+`loader_kwargs`）。这是一种「联合配置」式的建模。

`DatasetConfig` 在来源之上补充模态相关字段：

[genai_bench/data/config.py:53-71](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/data/config.py#L53-L71) —— 嵌套 `source`，并新增 `prompt_column`、`image_column`、`prompt_lambda`、`unsafe_allow_large_images`。

`from_cli_args` 的智能推断逻辑是本模块的重点：

[genai_bench/data/config.py:80-127](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/data/config.py#L80-L127) —— 空路径默认 `sonnet.txt`（第 89–93 行）；本地存在按后缀定格式（94–106 行）；本地不存在则当 HuggingFace ID（107–110 行）。

> 内置默认数据集 `sonnet.txt` 的路径由 `Path(__file__).parent / "sonnet.txt"` 得到，即与 `config.py` 同目录，位于 `genai_bench/data/sonnet.txt`。

#### 4.1.4 代码实践

**实践目标**：用 `from_cli_args` 构造一个指向内置 `sonnet.txt` 的 `DatasetConfig`，打印其字段，验证默认推断行为。

**操作步骤**（示例代码，保存为 `inspect_config.py` 后用 `python inspect_config.py` 运行）：

```python
# 示例代码
from genai_bench.data.config import DatasetConfig

# 不传任何路径 → 默认使用内置 sonnet.txt
cfg = DatasetConfig.from_cli_args()

print("source.type       =", cfg.source.type)
print("source.path       =", cfg.source.path)
print("source.file_format=", cfg.source.file_format)
print("prompt_column     =", cfg.prompt_column)

# 用 Pydantic 序列化看全貌
print(cfg.model_dump_json(indent=2))
```

**需要观察的现象**：`source.type` 应为 `file`，`file_format` 应为 `txt`，`source.path` 应指向仓库内 `genai_bench/data/sonnet.txt` 的绝对路径。

**预期结果**（待本地验证路径前缀）：

```
source.type       = file
source.file_format= txt
source.path       = .../genai_bench/data/sonnet.txt
prompt_column     = None
```

> 小提示：`import genai_bench...` 会触发包入口的 `gevent.monkey.patch_all()`（见 u1-l1）。对这种纯数据脚本没有副作用，但要知道有这一步。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `dataset_path` 设为一个本地不存在的字符串 `"myorg/my-dataset"`，`from_cli_args` 会把它判定成什么来源？

**参考答案**：本地不存在 → 走 else 分支，判定为 `huggingface`，`file_format=None`。这正是「按路径是否存在来区分本地文件 vs HuggingFace ID」的推断逻辑。

**练习 2**：`DatasetSourceConfig` 为什么把文件、HuggingFace、自定义三套字段都塞进同一个模型，而不是拆成三个子类？

**参考答案**：为了让 `DatasetConfig` 能用**一份 JSON / 一组 CLI 参数**统一表达三种来源，配置文件可以「按需填字段」。代价是字段多为 `Optional`、合法性靠运行时校验；好处是序列化与 CLI 传递极其简单，新增来源类型只需加字段+在 `validate_type` 里放行。

---

### 4.2 数据源 DatasetSource：数据从哪里来

#### 4.2.1 概念说明

`DatasetConfig` 只描述「要什么数据」，**真正去读**的是数据源 `DatasetSource`。这是数据加载子系统的第一条轴——**来源轴**。

genai-bench 内置三种数据源，恰好对应 `DatasetSourceConfig.type` 的三种取值：

- `FileDatasetSource`：读本地文件，支持 `txt`（逐行读成字符串列表）、`csv`（用 pandas 读成「列名→列值」的字典）、`json`（必须是列表）。
- `HuggingFaceDatasetSource`：调用 `datasets.load_dataset`，支持透传任意 `load_dataset` 参数；加载前会先用 `dataset_info` 校验数据集是否存在（对 gated repo 需要设 `HF_TOKEN`）。
- `CustomDatasetSource`：按「点分导入路径」动态 import 一个用户自定义类，实例化后调用其 `load()` 方法。这是留给二次开发的扩展口。

三者的共同抽象是基类 `DatasetSource`，它只要求实现一个 `load()` 方法，返回「原始数据」（格式不限，交给 loader 再加工）。

#### 4.2.2 核心流程

数据源的创建由 `DatasetSourceFactory.create(config)` 完成，它用一个**注册表字典** `_sources` 把 `type` 映射到具体类：

```
DatasetSourceFactory.create(config)
   ├─ config.type == "file"        → FileDatasetSource(config)
   ├─ config.type == "huggingface" → HuggingFaceDatasetSource(config)
   └─ config.type == "custom"      → CustomDatasetSource(config)
```

之后调用 `source.load()` 拿到原始数据。以 `txt` 为例，每条**非空行**就是一条 prompt，于是 prompt 数量等于非空行数：

\[
\ |\text{prompts}|\ =\ |\{\,\text{line}\in\text{file}\ \mid\ \text{line.strip()}\neq \text{''}\,\}|
\]

`csv` 稍特别：它不是返回字符串列表，而是返回 `df.to_dict(orient="list")`，即 `{列名: [列值...]}` 的字典——这样后续 loader 才能用 `prompt_column` 去挑列。

`CustomDatasetSource` 用 `importlib.import_module` 按点分路径导入类，例如配置 `loader_class = "mypkg.mymod.MyLoader"`，它会拆成模块 `mypkg.mymod` 和类名 `MyLoader`，再调用 `MyLoader(**loader_kwargs).load()`。

#### 4.2.3 源码精读

`DatasetSource` 抽象基类与三种实现都集中在 `sources.py`：

[genai_bench/data/sources.py:20-33](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/data/sources.py#L20-L33) —— `DatasetSource` 基类只持有 `config` 并要求子类实现 `load()`。

`FileDatasetSource.load()` 按格式分发到三个私有方法：

[genai_bench/data/sources.py:36-58](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/data/sources.py#L36-L58) —— 校验路径存在，按 `file_format` 走 txt/csv/json 三条分支。`_load_text_file` 逐行读取并 `strip`、过滤空行（[第 59–65 行](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/data/sources.py#L59-L65)），`_load_csv_file` 用 pandas 读成 dict（[第 67–77 行](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/data/sources.py#L67-L77)）。

`HuggingFaceDatasetSource.load()` 透传 `huggingface_kwargs` 给 `load_dataset`，并做存在性校验：

[genai_bench/data/sources.py:94-123](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/data/sources.py#L94-L123) —— 先判断路径是否为本地目录（是则直接 `load_dataset`，跳过联网校验）；否则用 `dataset_info` 校验远程数据集存在性，找不到就提示设置 `HF_TOKEN`。

`CustomDatasetSource.load()` 用 `importlib` 动态加载自定义类：

[genai_bench/data/sources.py:126-156](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/data/sources.py#L126-L156) —— `loader_class.rsplit(".", 1)` 拆出模块路径与类名，`import_module` + `getattr` 取到类，实例化后要求该对象必须有 `load()` 方法。

最后是创建它们的工厂：

[genai_bench/data/sources.py:159-200](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/data/sources.py#L159-L200) —— `DatasetSourceFactory` 用注册表 `_sources` 做 `type→类` 映射；还提供 `register_source` 让外部注册新来源类型，是扩展点。

#### 4.2.4 代码实践

**实践目标**：绕过 loader，直接用 `FileDatasetSource` 读取内置 `sonnet.txt`，观察「来源层」最原始的产出。

**操作步骤**（示例代码）：

```python
# 示例代码
from pathlib import Path
from genai_bench.data.config import DatasetSourceConfig
from genai_bench.data.sources import FileDatasetSource, DatasetSourceFactory

sonnet = str(Path("genai_bench/data/sonnet.txt"))

src_cfg = DatasetSourceConfig(type="file", path=sonnet, file_format="txt")
# 两种等价写法：直接 new，或走工厂
source = DatasetSourceFactory.create(src_cfg)

data = source.load()
print("条数:", len(data))
print("前 3 条:", data[:3])
```

**需要观察的现象**：`data` 是一个 `List[str]`，长度应为 84；前几条是莎士比亚十四行诗的句子，例如 `"Shall I compare thee to a summer's day?"`。

**预期结果**：`条数: 84`，且每条都已去除首尾空白、空行被过滤。

> 说明：这一步只走到「来源层」，还没有经过任何 loader 的塑形。对 txt 来说恰好已经是 `List[str]`；但对 csv，这里拿到的是字典，必须靠 loader 才能挑出 prompt 列。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `FileDatasetSource` 读 CSV 时返回的是字典（`df.to_dict(orient="list")`），而不是像 txt 那样直接返回字符串列表？

**参考答案**：因为 CSV 有多列，loader 还不知道用户要用哪一列当 prompt（由 `prompt_column` 指定）。返回「列名→列值」的字典，把「选哪一列」的决定权留给后续的 `TextDatasetLoader`，从而支持任意列名。

**练习 2**：如果你想接入公司内部的一个数据 API，应该用哪种数据源？需要实现什么？

**参考答案**：用 `custom` 类型。写一个带 `load()` 方法的类（`load()` 返回原始数据），把它的点分导入路径填进 `DatasetSourceConfig.loader_class`，构造参数填进 `loader_kwargs`。`CustomDatasetSource` 会自动 `importlib` 导入并调用它。

---

### 4.3 加载工厂 DataLoaderFactory：按输入模态分发

#### 4.3.1 概念说明

数据加载子系统的第二条轴是**模态轴（loader 轴）**：同样一批数据，文本任务和图像任务需要的「形状」完全不同。`DataLoaderFactory` 就是把任务路由到正确 loader 的入口。

它的核心方法 `load_data_for_task(task, dataset_config)` 做的事情非常简洁：**把任务字符串按 `-to-` 拆开，看输入模态是什么**——

- 输入是 `text` → 用 `TextDatasetLoader`，产出 `List[str]`。
- 输入含 `image`（注意是子串匹配，所以 `image-text-to-text` 也命中）→ 用 `ImageDatasetLoader`，产出图像结构。
- 其他（如 `video`）→ 抛 `ValueError: Unsupported input modality`。

> 关键认知：**分发依据是输入模态，不是输出模态**。这一点和 u2-l1 里「输入模态决定采样器/loader，输出模态决定请求类型」的结论完全一致。

#### 4.3.2 核心流程

```
load_data_for_task("text-to-text", config)
   ├─ task.split("-to-") → ("text", "text")
   ├─ input == "text" → _load_text_data(config, output)
   │      └─ TextDatasetLoader(config).load_request() → List[str]
   └─ "image" in input → _load_image_data(config)
          └─ ImageDatasetLoader(config).load_request() → 图像结构
```

分发后真正干活的，是各个 loader 的 `load_request()`（见 4.4）。`_load_text_data` 还会把 `output_modality` 透传给文本 loader——不过当前 `TextDatasetLoader` 并未使用它，属于为未来留的参数。

#### 4.3.3 源码精读

整个工厂只在一个文件里，逻辑很短：

[genai_bench/data/loaders/factory.py:16-36](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/data/loaders/factory.py#L16-L36) —— `load_data_for_task`：`task.split("-to-")` 拆模态，按输入模态 `text` / 含 `image` / 其他三分支分发。

两个内部静态方法分别实例化对应 loader 并调用 `load_request()`：

[genai_bench/data/loaders/factory.py:38-58](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/data/loaders/factory.py#L38-L58) —— `_load_text_data` 用 `cast(List[str], data)` 标注文本 loader 必返回字符串列表；`_load_image_data` 返回 loader 的原始产出（`List[Tuple[str, Any]]`）。

CLI 里正是这样串起来的（读结果→建采样器）：

[genai_bench/cli/cli.py:298-323](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L298-L323) —— 先用 `from_file` 或 `from_cli_args` 建 `dataset_config_obj`，再 `DataLoaderFactory.load_data_for_task(task, ...)` 得到 `data`，最后把 **`data` 和 `dataset_config_obj` 一起**传给 `Sampler.create`（文本采样吃 `data`，图像/raw 采样还会再用到 `dataset_config`）。

> 这也解释了为什么要同时保留「预加载的 data」和「原始 dataset_config」两个对象：文本路径用预加载列表更快，而图像/`dataset` 模式需要按行取原始记录，得依赖 config。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：完成本讲规定的实践——用 `from_cli_args` 构造指向内置 `sonnet.txt` 的 `DatasetConfig`，再用 `DataLoaderFactory.load_data_for_task` 加载 `text-to-text` 数据并打印前几条。

**操作步骤**（示例代码）：

```python
# 示例代码
from genai_bench.data.config import DatasetConfig
from genai_bench.data.loaders.factory import DataLoaderFactory

# 1) 构造配置：不传路径 → 默认内置 sonnet.txt
cfg = DatasetConfig.from_cli_args()

# 2) 按任务加载：输入模态 text → 走 TextDatasetLoader
data = DataLoaderFactory.load_data_for_task("text-to-text", cfg)

# 3) 打印前几条
print("类型:", type(data).__name__, "| 总条数:", len(data))
for i, prompt in enumerate(data[:5]):
    print(f"[{i}] {prompt}")
```

**需要观察的现象**：`data` 是 `list`；总条数应为 84；前 5 条是十四行诗的句子。

**预期结果**（待本地验证确切文本）：

```
类型: list | 总条数: 84
[0] Shall I compare thee to a summer's day?
[1] Thou art more lovely and more temperate:
...
```

**延伸观察（可选）**：把任务换成不支持的输入模态，例如 `load_data_for_task("video-to-text", cfg)`，应抛出 `ValueError: Unsupported input modality: video`（与单测 [test_factory.py:52-61](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/data/loaders/test_factory.py#L52-L61) 一致）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `load_data_for_task` 用 `"image" in input_modality`（子串匹配）而不是 `input_modality == "image"`？

**参考答案**：因为多模态任务的输入模态可能是复合的，如 `image-text-to-text`（图文转文本）。用子串匹配能让 `image`、`image-text` 都正确路由到 `ImageDatasetLoader`，而不必为每种组合写一个分支。

**练习 2**：`_load_text_data` 把 `output_modality` 传给了文本 loader，但 `TextDatasetLoader` 当前并未用到它。这说明什么？

**参考答案**：说明这是一个**面向未来/预留**的参数——设计上允许文本 loader 未来按输出模态（如 `text` vs `embeddings`）做不同处理，但当前实现尚未区分。读源码时要能识别这种「已接线、未启用」的设计意图，避免误以为它影响了当前行为。

---

### 4.4 Loader 实现：TextDatasetLoader 与 ImageDatasetLoader

#### 4.4.1 概念说明

`DataLoaderFactory` 只负责「选哪个 loader」，真正「加工数据」的是两个 loader 子类。它们都继承自抽象基类 `DatasetLoader`，核心约定是：

- 在 `__init__` 里**组合一个数据源**（`DatasetSourceFactory.create(config.source)`）——这就是「来源轴」与「模态轴」的交汇点。
- 声明自己支持的格式集合 `supported_formats` 和媒体类型 `media_type`。
- 实现 `_process_loaded_data(data)`，把数据源返回的原始数据加工成目标形状。
- 对外只暴露 `load_request()`：先（按需）关闭 Pillow 大图保护，再 `source.load()`，再 `_process_loaded_data`。

`base.py` 还定义了 `DatasetFormat` 枚举（`txt/csv/json/huggingface`），用于在加载前校验「这个 loader 是否支持当前来源格式」。

#### 4.4.2 核心流程

`DatasetLoader.load_request()` 的统一流程：

```
load_request()
  ├─ _disable_pillow_decompresion_check()   # 仅 unsafe_allow_large_images=True 时生效
  ├─ data = self.dataset_source.load()      # 来源轴：拿到原始数据
  └─ return self._process_loaded_data(data) # 模态轴：加工成 List[str] / 图像结构
```

两个子类的差异主要在 `_process_loaded_data`：

- **`TextDatasetLoader`**：支持 txt/csv/json/huggingface 全部四种格式。若数据已是 `list`（txt/json 的产出）直接返回；若是字典（csv 的产出）或 HuggingFace 数据集，则用 `prompt_column` 取列并强转成 `List[str]`；找不到列时抛出**带可用列名提示**的错误。
- **`ImageDatasetLoader`**：**只支持 HuggingFace**。它把 HuggingFace 的 `Dataset` / `DatasetDict` 归一化（优先选 `train` split），并**拒绝流式数据集**（`streaming=True`），要求提供具体 split。

> 设计对比：文本 loader 「宽进」（四种来源都吃），图像 loader 「严进」（只认 HuggingFace）。原因是从 HuggingFace 行里取图像字段（PIL Image）最规范，而本地图像目录的加载语义复杂，项目暂未支持。

#### 4.4.3 源码精读

`DatasetFormat` 枚举与 `DatasetLoader` 基类（含组合数据源、格式校验、`load_request` 模板）：

[genai_bench/data/loaders/base.py:14-20](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/data/loaders/base.py#L14-L20) —— `DatasetFormat` 四种取值。

[genai_bench/data/loaders/base.py:23-58](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/data/loaders/base.py#L23-L58) —— `__init__` 里先 `_validate_source_format`（按 `supported_formats` 校验来源格式），再 `DatasetSourceFactory.create(...)` 组合数据源。这是「loader 内部持有一个 source」的关键。

[genai_bench/data/loaders/base.py:67-80](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/data/loaders/base.py#L67-L80) —— `load_request` 模板方法：先关大图保护、再 `source.load()`、再交给子类 `_process_loaded_data`；`_process_loaded_data` 是 `@abstractmethod`。

`TextDatasetLoader` 的加工逻辑：

[genai_bench/data/loaders/text.py:16-51](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/data/loaders/text.py#L16-L51) —— `supported_formats` 含全部四种；`_process_loaded_data` 对 `list` 原样返回，对字典/HF 数据按 `prompt_column` 取列并转 `List[str]`，列缺失时给出可用列名提示。

`ImageDatasetLoader` 的归一化逻辑：

[genai_bench/data/loaders/image.py:19-56](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/data/loaders/image.py#L19-L56) —— 只支持 `HUGGINGFACE_HUB`；`Dataset` 直接返回，`DatasetDict` 优先选 `train` split，流式数据集直接报错要求关闭 `streaming`。

#### 4.4.4 代码实践

**实践目标**：体会 `TextDatasetLoader` 对 csv 的「按列挑选」加工，并用项目单测验证行为。

**操作步骤**（阅读型 + 动手型结合）：

1. 先阅读单测 [test_text.py:68-83](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/data/loaders/test_text.py#L68-L83)：它 mock 出一个返回 `{"prompt": [...], "other_col": [...]}` 字典的数据源，断言 loader 只取出 `prompt` 列、返回 `["val1", "val3"]`。
2. 再动手验证「列名不存在」的报错路径（示例代码）：

```python
# 示例代码
from unittest.mock import MagicMock, patch
from genai_bench.data.config import DatasetConfig, DatasetSourceConfig
from genai_bench.data.loaders.text import TextDatasetLoader

cfg = DatasetConfig(
    source=DatasetSourceConfig(type="file", path="x.csv", file_format="csv"),
    prompt_column="invalid_column",
)
fake_source = MagicMock()
fake_source.load.return_value = {"text": ["a", "b"], "label": ["A", "B"]}

with patch("genai_bench.data.sources.DatasetSourceFactory.create", return_value=fake_source):
    try:
        TextDatasetLoader(cfg).load_request()
    except ValueError as e:
        print("捕获到错误:", e)
```

**需要观察的现象**：因为 `prompt_column="invalid_column"` 在 `{"text","label"}` 中不存在，loader 应抛出 `ValueError`，且消息里**列出可用列名**。

**预期结果**（待本地验证）：

```
捕获到错误: Column 'invalid_column' not found in CSV file. Available columns: ['text', 'label']
```

> 这与单测 [test_text.py:99-114](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/data/loaders/test_text.py#L99-L114) 的断言完全对应——这种「带可用列名提示」的错误信息是项目刻意设计的可用性细节。

#### 4.4.5 小练习与答案

**练习 1**：`ImageDatasetLoader` 为什么在遇到流式数据集（`IterableDataset`）时直接报错，而不是兼容处理？

**参考答案**：因为图像采样需要按索引/长度随机访问具体行（取图像字段、控制采样数量），而流式数据集只能顺序迭代、不支持 `len()` 和随机 `__getitem__`。所以项目要求 `streaming=False` 并提供具体 split，把「可随机访问」作为前提。

**练习 2**：`TextDatasetLoader` 支持 `csv`，`ImageDatasetLoader` 不支持 `csv`。如果你有一个「本地 csv 里用 URL 列表示图像」的数据集，当前能否直接用图像任务加载？

**参考答案**：不能直接用。`ImageDatasetLoader.supported_formats` 只含 `HUGGINGFACE_HUB`，`_validate_source_format` 会对 `file`+`csv` 报「不被 image loader 支持」。要实现这种需求，需要新增 loader 或数据源（扩展点见 u8-l3），或先把数据转成 HuggingFace 数据集。

---

## 5. 综合实践

把本讲的「契约 + 来源 + 工厂 + loader」四件事串起来，完成下面这个小任务：

**任务**：自造一个本地 CSV 数据集，分别用「命令行参数」和「JSON 配置文件」两种方式加载它，并验证两者结果一致。

**步骤**：

1. 准备数据。建一个 `prompts.csv`（示例代码生成）：

   ```python
   # 示例代码
   from pathlib import Path
   Path("prompts.csv").write_text("prompt,category\n你好, greet\n讲个笑话, fun\n解释 RAG, tech\n")
   ```

2. 方式 A——命令行参数风格：用 `from_cli_args(dataset_path="prompts.csv", prompt_column="prompt")` 构造配置，再用 `DataLoaderFactory.load_data_for_task("text-to-text", cfg)` 加载并打印。

3. 方式 B——配置文件风格：把下面的 JSON 存成 `my_config.json`（与 `examples/dataset_configs/local_csv.json` 同构），用 `DatasetConfig.from_file("my_config.json")` 构造，再走同一个 `load_data_for_task` 加载。

   ```json
   {
     "source": {"type": "file", "path": "prompts.csv", "file_format": "csv"},
     "prompt_column": "prompt"
   }
   ```

4. 对比两种方式得到的 `data`：应当都是 `["你好", "讲个笑话", "解释 RAG"]`。

**需要观察的现象 / 预期结果**（待本地验证）：

- 两种入口产出的 `data` 内容与顺序一致，都是 3 条 prompt。
- 你能解释：CSV 经 `FileDatasetSource` 变成字典 → `TextDatasetLoader._process_loaded_data` 用 `prompt_column="prompt"` 取列 → 得到 `List[str]`。这就是「来源轴产出字典、模态轴挑列塑形」的完整链路。

**反思题**：如果把 `prompt_column` 改成不存在的列名，两种方式是否都会报同样的错？为什么？（提示：列挑选发生在 loader 层，与配置入口无关。）

## 6. 本讲小结

- genai-bench 把数据加载做成一个独立子系统，由**两条正交的轴**组织：**来源轴**（数据从哪来：文件/HuggingFace/自定义）与**模态轴**（数据塑成什么形状：文本/图像）。
- `DatasetConfig` + `DatasetSourceConfig` 是描述数据集的 Pydantic 契约；`from_file` 走 JSON 配置文件，`from_cli_args` 走零散 CLI 参数，并能智能推断来源类型（本地存在=文件，否则=HuggingFace，空则默认内置 `sonnet.txt`）。
- `DatasetSource` 三实现 + `DatasetSourceFactory` 负责「真正去读」，其中 `txt` 逐行成 `List[str]`、`csv` 读成字典、`huggingface` 调 `load_dataset`、`custom` 按 importlib 动态加载。
- `DataLoaderFactory.load_data_for_task` 按**输入模态**分发：`text` → `TextDatasetLoader` 出 `List[str]`，含 `image` → `ImageDatasetLoader`，其余报错。
- `DatasetLoader` 基类在 `__init__` 里**组合一个数据源**，从而把两条轴连起来；`load_request()` = 关大图保护 → `source.load()` → `_process_loaded_data`。
- `TextDatasetLoader`「宽进」（四种格式都吃、按 `prompt_column` 挑列、列缺失给提示），`ImageDatasetLoader`「严进」（只认 HuggingFace、拒绝流式）。

## 7. 下一步学习建议

本讲把数据「读进来、变成 `List[str]` 或图像结构」就结束了，但这些数据如何变成发往后端的 `UserRequest`，还差一环。建议：

- **下一讲 u2-l4（Sampler 与请求构造）**：看 `Sampler` 如何接收本讲产出的 `data` 与 `dataset_config`，结合场景（含 `dataset` 模式）生成一条条 `UserChatRequest`。重点理解「为什么 sampler 同时要 `data` 和 `dataset_config` 两个参数」——答案就藏在本讲 4.3 提到的两条路径里。
- **扩展阅读**：浏览 `examples/dataset_configs/*.json`，对照本讲字段说明，看真实的多模态/高级 HuggingFace 配置长什么样。
- **后续 u8-l3（扩展指南）**：当你需要新增一种数据来源或一种 loader 时，`DatasetSourceFactory.register_source` 与 `DatasetLoader` 子类化就是切入点。
