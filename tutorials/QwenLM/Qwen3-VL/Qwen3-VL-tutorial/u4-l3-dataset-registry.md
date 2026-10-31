# 数据集注册表与采样率控制

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `qwenvl/data/__init__.py` 中数据集注册表的组织方式，以及 `annotation_path` 与 `data_path` 两个字段的分工。
2. 独立把一个自定义数据集（上一讲制作的 `my_dataset.json`）注册进训练框架。
3. 掌握 `%X` 采样率语法：它是如何被解析、何时被执行、有哪些边界坑。
4. 跟踪 `LazySupervisedDataset` 的 `__init__` 流程：从 `--dataset_use` 字符串到一份拼接完成的 `list_data_dict` 标注列表。

本讲是单元四「微调数据准备」的第三讲。上一讲我们搞清了「一条训练样本长什么样」，本讲回答「一堆训练样本如何被框架找到、按比例抽取、混在一起」。

## 2. 前置知识

- **注册表（registry）模式**：框架用一个字典把「数据集名字」映射到「配置信息」。用户在启动脚本里只写名字，框架凭字典查表拿到真实路径。这是许多训练框架（LLaVA 系、mPLUG 系）的通用做法，好处是启动脚本与磁盘路径解耦。
- **标注文件（annotation）与媒体数据（data）分离**：一条样本的 JSON 里只写图片/视频的相对路径，真实文件放在另一个目录。训练时用 `data_path + 相对路径` 拼出绝对路径。这样同一份标注可以配合不同的数据存放位置。
- **JSON 与 JSONL**：JSON 是一个完整数组（一次性 `json.load`）；JSONL 是「每行一个 JSON 对象」的流式格式（逐行 `json.loads`），适合超大标注文件。本框架两种都支持。
- **采样率（sampling rate）**：只想用某个数据集的一部分数据时，按百分比随机抽取，例如 `%50` 表示抽一半。常用于控制多个数据集的混合比例或做小规模冒烟实验。
- **lazy（惰性）加载**：`__init__` 阶段只读入标注列表，不做分词、不加载图片；真正的张量化推迟到训练时按样本索引 `__getitem__` 才发生。这样启动快、内存省。
- 承接上一讲（u4-l2）：你已经知道一条样本由 `image`/`video` 字段（单值或列表）与 `conversations`（human/gpt 交替）构成，`<image>`/`<video>` 占位符与媒体文件按出现顺序一一配对。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [qwen-vl-finetune/qwenvl/data/__init__.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/__init__.py) | 数据集注册表（全部仅 63 行） | 数据集字典定义、`data_dict` 查表、`parse_sampling_rate`、`data_list` |
| [qwen-vl-finetune/qwenvl/data/data_processor.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py) | 数据集类与样本张量化 | `LazySupervisedDataset.__init__` 的标注加载、采样执行、`data_path` 注入 |
| [qwen-vl-finetune/qwenvl/train/argument.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/argument.py) | 训练参数 dataclass | `DataArguments.dataset_use` 字段 |
| [qwen-vl-finetune/README.md](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md) | 官方文档 | 「Dataset config for training」一节的注册示例 |
| [qwen-vl-finetune/scripts/sft_qwen3_4b.sh](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_qwen3_4b.sh) | 官方启动脚本 | `datasets` 变量如何流入 `--dataset_use` |
| [qwen-vl-finetune/demo/single_images.json](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/demo/single_images.json) | 演示标注（3 条样本） | 实践环节的现成数据 |

> 说明：`data_processor.py` 其余部分（`_build_messages`、标签掩码、collator）分别属于 u4-l2 与 u5-l3/u5-l5 的内容，本讲只截取与「加载」相关的段落。

## 4. 核心概念与源码讲解

### 4.1 数据集字典：annotation_path 与 data_path 的分工

#### 4.1.1 概念说明

框架为每个数据集定义一个只有两个键的小字典：

- `annotation_path`：**标注文件的路径**，指向一个 `.json` 或 `.jsonl` 文件，里面是样本数组。
- `data_path`：**媒体文件的基准目录**。标注里写的图片/视频路径是相对路径时，就用它拼绝对路径；如果标注里已经是绝对路径，它可以留空字符串。

所有这些小字典再被收进总注册表 `data_dict`，键是数据集的「代号」。训练脚本里 `--dataset_use` 填的就是这些代号，而不是磁盘路径——这正是注册表模式的意义：**脚本里写名字，源码里存路径**。

仓库内置了 5 个示例条目（`cambrian_737k`、`cambrian_737k_pack`、`mp_doc`、`clevr_mc`、`videochatgpt`），值都是 `PATH_TO_XXX` 占位符，需要用户替换成真实路径才能用。

#### 4.1.2 核心流程

```text
用户视角：
  data/__init__.py
  ├─ CAMBRIAN_737K      = {annotation_path: 标注文件, data_path: 媒体目录或 ""}
  ├─ CAMBRIAN_737K_PACK = {...}   # packed 数据集也是同样的两键结构
  ├─ MP_DOC / CLEVR_MC / VIDEOCHATGPT = {...}
  └─ data_dict = { "cambrian_737k": CAMBRIAN_737K, ... }   # 代号 → 配置 的总表
```

分工的判断口诀：

| 情况 | data_path 该填什么 |
| --- | --- |
| 标注里是相对路径（如 `demo/images/10095.png`） | 相对路径的基准目录（如 `/xxx/qwen-vl-finetune`） |
| 标注里是绝对路径或 `file://` 路径 | 留空 `""` |

注意一个细节：`data_path` 为空时，相对路径会以**运行训练时的工作目录**为基准解析（后文 4.3 会看到 `Path("")` 的行为），所以最稳妥的做法是要么给足 `data_path`，要么标注里写绝对路径。

#### 4.1.3 源码精读

数据集字典的定义，每个数据集就是 `annotation_path` + `data_path` 两键：

[qwen-vl-finetune/qwenvl/data/__init__.py:L4-L12](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/__init__.py#L4-L12)

上面这段代码定义了 `CAMBRIAN_737K` 与 `CAMBRIAN_737K_PACK` 两个字典：前者 `data_path` 为空（标注内自带可解析路径），后者是打包（packed）数据的变体。占位符 `PATH_TO_...` 表示官方留给你替换的位置。

其余三个内置数据集条目，同样两键结构：

[qwen-vl-finetune/qwenvl/data/__init__.py:L14-L27](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/__init__.py#L14-L27)

总注册表 `data_dict`，把小写下划线代号映射到上面那些字典——训练脚本里的 `--dataset_use` 填的就是这些键：

[qwen-vl-finetune/qwenvl/data/__init__.py:L29-L35](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/__init__.py#L29-L35)

注册入口在参数侧只有一处：`DataArguments.dataset_use`，默认空字符串，类型是被逗号分隔的多个数据集代号：

[qwen-vl-finetune/qwenvl/train/argument.py:L14-L16](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/argument.py#L14-L16)

官方 README 对注册方式的正式说明（与本节内容一致，可作为权威参照）：

[qwen-vl-finetune/README.md:L161-L177](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L161-L177)

#### 4.1.4 代码实践

> 以下操作都在你自己的训练副本上完成（本讲义不改动仓库源码）。

1. **实践目标**：把上一讲制作的 `my_dataset.json` 注册成一个名为 `my_dataset` 的数据集；若你还没做上一讲的实践，可直接用仓库自带的 `demo/single_images.json`（含 3 条样本）代替。
2. **操作步骤**：
   - 编辑 `qwen-vl-finetune/qwenvl/data/__init__.py`，在 `data_dict` 之前添加：
     ```python
     MY_DATASET = {
         "annotation_path": "/绝对路径/my_dataset.json",   # 或 /绝对路径/qwen-vl-finetune/demo/single_images.json
         "data_path": "/绝对路径/你的图片目录",             # demo 场景填 /绝对路径/qwen-vl-finetune
     }
     ```
   - 在 `data_dict` 字典里追加一行：`"my_dataset": MY_DATASET,`。
   - 在 `qwen-vl-finetune` 目录下运行：`python -m qwenvl.data`（该文件自带 `__main__` 演示，见 [qwen-vl-finetune/qwenvl/data/__init__.py:L59-L63](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/__init__.py#L59-L63)），确认能正常打印 `cambrian_737k` 的配置而不报错，说明文件语法正确。
3. **需要观察的现象**：`python -m qwenvl.data` 输出形如 `{'annotation_path': 'PATH_TO_CAMBRIAN_737K_ANNOTATION', 'data_path': '', 'sampling_rate': 1.0}` 的字典。
4. **预期结果**：注册后 `data_dict` 里多出 `my_dataset` 键；占位符条目因路径不存在，本步只验证语法，不真正读文件。

#### 4.1.5 小练习与答案

**练习 1**：标注里写的是 `demo/images/10095.png` 这样的相对路径，`data_path` 应该填什么？
**答案**：填相对路径的基准目录，即 `qwen-vl-finetune` 仓库根目录的绝对路径（`demo/images/10095.png` 是相对它写的）。后文 4.3 会看到源码用 `Path(data_path) / 相对路径` 拼接。

**练习 2**：如果我把 `annotation_path` 填成了一个不存在 `.json`/`.jsonl` 后缀结尾的路径，框架会怎么处理？
**答案**：框架按 `annotation_path.split(".")[-1]` 判断格式，**只有恰好是 `jsonl` 才走逐行读取**，其余一律走 `json.load`（见 4.3 源码）。所以无后缀或 `.json` 后缀都按完整 JSON 解析；路径不存在则抛文件 IO 错误。

**练习 3**：为什么 `data_dict` 里 packed 数据集（`cambrian_737k_pack`）与普通数据集的结构完全一样？
**答案**：因为「打包」只改变**标注文件内部的结构**（从「样本的列表」变成「桶的列表、桶内再嵌样本」，见 u4-l2），对注册层来说它仍然只是「一个标注文件 + 一个媒体目录」，两键结构足够。区别在加载循环里对嵌套列表的展开（见 4.4）。

### 4.2 data_list 解析与 %X 采样率语法

#### 4.2.1 概念说明

`data_list` 是注册表的查询函数：输入一组「可能带 `%X` 后缀」的数据集名字，输出一组**可直接使用的配置字典**，每个字典额外多一个 `sampling_rate` 键（浮点数 0~1）。它做三件事：

1. 用正则从名字末尾剥出 `%X`，换算成 `X / 100`；
2. 去掉后缀得到纯代号，去 `data_dict` 查表；
3. 查到的配置**浅拷贝**一份，挂上 `sampling_rate` 后收集返回；查不到就抛 `ValueError: do not find ...`。

采样发生在**解析之后、训练之前**的加载阶段（见 4.3），`data_list` 本身不读任何文件。

#### 4.2.2 核心流程

```text
输入: ["my_dataset%50", "clevr_mc"]
  对每个名字 name:
    rate = parse_sampling_rate(name)        # 正则 r"%(\d+)$" 命中末尾 %50 → 0.5；未命中 → 1.0
    name  = 去掉末尾 %50                     # "my_dataset"
    config = data_dict[name].copy()          # 浅拷贝，不污染注册表
    config["sampling_rate"] = rate
输出: [ {annotation_path, data_path, sampling_rate: 0.5},
       {annotation_path, data_path, sampling_rate: 1.0} ]
```

采到的样本条数公式（在 4.3 的加载阶段计算）：

\[ n_{sampled} = \operatorname{int}\left( N \times rate \right) \]

注意 `int()` 是**向下取整**：3 条样本 × 50% = 1.5 → 取 1 条。

必须掌握的四个边界情况：

| 写法 | 解析结果 | 后果 |
| --- | --- | --- |
| `my_dataset%50` | rate=0.5 | 正常用一半 |
| `my_dataset%150` | rate=1.5 | 解析不报错；加载时 `random.sample(annotations, k>N)` 抛 `ValueError` |
| `my_dataset%0` | rate=0.0 | 该数据集贡献 0 条样本（`random.sample(..., 0)` 返回空列表） |
| `my_dataset%50v2` | 正则要求 `%数字` 在**末尾**，不命中 | 整串 `my_dataset%50v2` 当作代号查表 → `do not find` |

另有一个易踩的坑：`dataset_use` 用逗号分隔多个名字时**不要加空格**。`"a,b"` 没问题，`"a, b"` 会把 `" b"`（带前导空格）当代号去查表而失败。此外 `random.sample` 没有固定随机种子，**同一命令两次运行抽到的子集不同**，做对比实验时需自行在训练脚本里 `random.seed(...)`（待本地验证：`train_qwen.py` 未额外设种子，跨 run 不可复现）。

#### 4.2.3 源码精读

`parse_sampling_rate`：正则 `%(\d+)$` 锚定字符串末尾，命中则返回 `整数/100.0`，否则默认全量 1.0：

[qwen-vl-finetune/qwenvl/data/__init__.py:L38-L42](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/__init__.py#L38-L42)

`data_list` 主函数：先算采样率、再用 `re.sub` 剥掉后缀得到纯代号、查表浅拷贝并挂 `sampling_rate`；查不到抛出带名字的 `ValueError`：

[qwen-vl-finetune/qwenvl/data/__init__.py:L45-L56](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/__init__.py#L45-L56)

这段代码里有两个值得注意的细节：`config = data_dict[dataset_name].copy()` 是**浅拷贝**，`sampling_rate` 只写进副本，注册表保持干净（多次调用 `data_list` 不会累积污染）；而 `re.search` 只匹配一次，所以 `%50%30` 这类写法只有末尾的 `%30` 生效（前缀变成非法代号，实际会查表失败）。

官方 README 对采样率语法的说明，与本节一致：

[qwen-vl-finetune/README.md:L179-L184](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L179-L184)

#### 4.2.4 代码实践

1. **实践目标**：验证 `%X` 解析行为与各种边界写法。
2. **操作步骤**：在 `qwen-vl-finetune` 目录下新建 `test_data_list.py`（示例代码，不是仓库文件）：
   ```python
   from qwenvl.data import data_list, parse_sampling_rate

   print(parse_sampling_rate("my_dataset%50"))   # 期望 0.5
   print(parse_sampling_rate("my_dataset"))      # 期望 1.0
   for cfg in data_list(["my_dataset%50", "clevr_mc"]):
       print(cfg)                                 # 观察每个配置里的 sampling_rate 键
   try:
       data_list(["my_dataset%50v2"])
   except ValueError as e:
       print("caught:", e)                        # 期望 do not find my_dataset%50v2
   ```
   运行 `python test_data_list.py`。
3. **需要观察的现象**：前两行打印 `0.5` 与 `1.0`；`data_list` 返回的两个字典分别带 `sampling_rate: 0.5` 与 `1.0`；最后一行捕获到 `do not find my_dataset%50v2`。
4. **预期结果**：与上表边界情况完全一致。若报 `ModuleNotFoundError`，请确认在 `qwen-vl-finetune` 目录下运行（该包无 pip 安装项，按 u4-l1 的「源码即工程」方式从源码目录启动）。

#### 4.2.5 小练习与答案

**练习 1**：`dataset_use="my_dataset%33"`，标注共 100 条，最终训练用多少条？
**答案**：`int(100 × 0.33) = 33` 条（向下取整）。

**练习 2**：我想让同一个数据集被「重复叠加」两次（等效放大它在混合中的权重），`%200` 可以吗？
**答案**：不可以。`%200` 会让加载阶段 `random.sample(annotations, 2N)` 因 k 大于总体而抛 `ValueError`。正确做法是在 `dataset_use` 里把代号写两次：`"my_dataset,my_dataset"`，`data_list` 会输出两个配置，加载后各贡献一份全量数据。

**练习 3**：为什么 `data_list` 要对查到的配置做 `.copy()` 而不是直接用？
**答案**：`config["sampling_rate"] = sampling_rate` 是往字典里写键。若不拷贝，会把这个临时键写进 `data_dict` 里的原始注册条目，污染注册表；浅拷贝保证注册表条目永远只有 `annotation_path` 与 `data_path` 两个键。

### 4.3 LazySupervisedDataset 加载：从 dataset_use 到 list_data_dict

#### 4.3.1 概念说明

`LazySupervisedDataset` 是训练时真正的数据集类（继承 `torch.utils.data.Dataset`）。它的 `__init__` 完成「从字符串到样本清单」的全部工作：

- 把 `data_args.dataset_use` 按逗号拆开，交给 4.2 的 `data_list` 得到配置列表；
- 逐个配置读标注文件（JSONL 逐行、JSON 整体）；
- 对 `sampling_rate < 1.0` 的数据集执行 `random.sample` 随机抽取；
- 给**每条标注**注入 `data_path` 键（后面 `_build_messages` 用它解析相对媒体路径）；
- 把所有数据集的标注按注册顺序**顺序拼接**成 `self.list_data_dict`。

「Lazy」体现在：到此为止**不做任何分词与图片读取**，只持有轻量的标注字典列表；`__len__` 就是 `len(list_data_dict)`，真正的张量化发生在训练循环按索引调用 `__getitem__` 时（u5-l3 精读）。

#### 4.3.2 核心流程

```text
__init__(processor, data_args)
  ├─ dataset_use.split(",")            # "a%50,b" → ["a%50", "b"]
  ├─ data_list(名字列表)               # 查表 + 解析采样率（4.2）
  ├─ for data in 配置列表:
  │    ├─ 后缀 == "jsonl" ? read_jsonl : json.load      # 按扩展名二分
  │    ├─ sampling_rate < 1.0 ?
  │    │     random.sample(annotations, int(N × rate))  # 在这里才真正采样
  │    └─ for ann in annotations:
  │         ann["data_path"] = data["data_path"]         # 逐条注入基准路径
  │         （ann 是 list 时对每个子项注入 → packed 样本，见 4.4）
  │    list_data_dict += annotations                     # 顺序拼接
  ├─ 打印 "Total training samples: N"
  ├─ update_processor_pixels(...)       # 把像素预算参数写进 processor（u5-l3 展开）
  └─ data_packing ? item_fn=_get_packed_item : item_fn=_get_item   # 决定取样本方式
```

`data_path` 注入是被下一讲反复引用的关键一步：标注文件里的相对路径之所以能被解析，就是因为加载时每条样本都随身携带了基准目录。`_build_messages` 中用 `Path(source.get("data_path", ""))` 取出它、再与相对路径拼接（参见 [qwen-vl-finetune/qwenvl/data/data_processor.py:L140-L156](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L140-L156) 中的 `_make_abs_paths` 调用）。`data_path` 为空时 `Path("")` 退化为相对当前工作目录解析——这就是 4.1 说「留空需谨慎」的原因。

#### 4.3.3 源码精读

入口三行：拆分 `dataset_use`、调 `data_list`、打印将要加载的配置：

[qwen-vl-finetune/qwenvl/data/data_processor.py:L247-L252](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L247-L252)

JSONL 读取辅助函数，逐行 `json.loads`：

[qwen-vl-finetune/qwenvl/data/data_processor.py:L35-L37](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L35-L37)

加载主循环——本讲的心脏。按扩展名选读取方式；`sampling_rate < 1.0` 时用 `random.sample` 按比例抽取；随后无论是否采样，都给每条标注注入 `data_path`，其中 `isinstance(ann, list)` 分支专门处理 packed 样本（桶内逐个子样本注入）；最后 `+=` 顺序拼接：

[qwen-vl-finetune/qwenvl/data/data_processor.py:L269-L291](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L269-L291)

采样条数公式 `int(len(annotations) * sampling_rate)` 就在 L280，向下取整；L282 的打印让你能在训练日志里直接看到「每个数据集实际采了多少条」。

拼接完成后的总量打印与 lazy 声明——注意「Skip in lazy mode」明确表示此处不做格式化：

[qwen-vl-finetune/qwenvl/data/data_processor.py:L293-L298](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L293-L298)

`__len__` 直接返回标注条数——采样已经发生，这里看到的就是「采样后」的规模：

[qwen-vl-finetune/qwenvl/data/data_processor.py:L309-L310](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L309-L310)

官方脚本中的用法参照：`datasets` 变量（逗号分隔代号）经 `--dataset_use` 传给训练入口：

[qwen-vl-finetune/scripts/sft_qwen3_4b.sh:L22-L33](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_qwen3_4b.sh#L22-L33)

其中 `datasets=public_dataset1,public_dataset2` 是占位示例，你需要替换成自己在 `data_dict` 里注册过的代号（可带 `%X` 后缀）。

#### 4.3.4 代码实践

1. **实践目标**：完整走一遍「注册 → 解析 → 加载」，亲眼看到采样与总量打印。
2. **操作步骤**：
   - 前置：完成 4.1 的注册（`my_dataset`，指向 demo 或你的数据；demo 场景 `data_path` 填 `qwen-vl-finetune` 的绝对路径）。
   - 新建 `test_lazy_dataset.py`（示例代码）。注意 `model_type` 在真实训练里由 `train_qwen.py` 依据模型名判定后注入（u5-l1 精读），这里手动指定 `qwen3vl`：
     ```python
     from types import SimpleNamespace
     from transformers import AutoProcessor
     from qwenvl.data.data_processor import LazySupervisedDataset

     processor = AutoProcessor.from_pretrained("Qwen/Qwen3-VL-2B-Instruct")  # 只下载处理器配置，体量很小
     data_args = SimpleNamespace(
         dataset_use="my_dataset%50",   # demo 标注 3 条 → 期望采到 1 条
         model_type="qwen3vl",
         data_packing=False,
         min_pixels=256*32*32, max_pixels=1280*32*32,
         video_min_pixels=256*28*28, video_max_pixels=1024*28*28,
         video_min_frames=4, video_max_frames=8, video_fps=2,
     )
     ds = LazySupervisedDataset(processor=processor, data_args=data_args)
     print("len(ds) =", len(ds))
     print("第一条样本自带的 data_path:", ds.list_data_dict[0]["data_path"])
     ```
   - 在 `qwen-vl-finetune` 目录下运行 `python test_lazy_dataset.py`。
3. **需要观察的现象**：日志依次出现 `Loading datasets: [...]`（含 `sampling_rate: 0.5`）、`sampling 1 examples from dataset ...`、`Total training samples: 1`；结尾打印 `len(ds) = 1` 与你填写的 `data_path`。
4. **预期结果**：demo 标注 3 条 × 50% 向下取整 = 1 条。把 `dataset_use` 改成 `"my_dataset"`（不带后缀）再跑，应看到 `dataset name: ...` 分支与 `Total training samples: 3`。整个过程不加载任何图片权重（lazy）。首次运行需联网拉取处理器配置；若网络受限，可先在能联网的机器上 `save_pretrained` 后改用本地目录加载（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`dataset_use="a%50,b"`，a 有 10 条、b 有 7 条，`Total training samples` 是多少？
**答案**：`int(10×0.5) + 7 = 5 + 7 = 12` 条。采样按数据集独立执行，未标注 `%X` 的默认全量。

**练习 2**：为什么说采样「发生在加载阶段而非解析阶段」？从源码找依据。
**答案**：`data_list`/`parse_sampling_rate` 只产出 `sampling_rate` 数值（[qwen-vl-finetune/qwenvl/data/__init__.py:L38-L56](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/__init__.py#L38-L56)）；真正调用 `random.sample` 的是 `LazySupervisedDataset.__init__` 读入标注之后（[qwen-vl-finetune/qwenvl/data/data_processor.py:L277-L282](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L277-L282)）。所以 `%150` 在解析时不报错，直到加载才抛 `ValueError`。

**练习 3**：`__init__` 里为什么顺带调用 `update_processor_pixels`？
**答案**：把命令行的像素预算（`min_pixels`/`max_pixels`/视频参数）写进 processor，保证后续 `__getitem__` 张量化时图像按训练预算缩放。这属于 processor 侧逻辑，u5-l3 会展开（参见 [qwen-vl-finetune/qwenvl/data/data_processor.py:L44-L60](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L44-L60)）。

### 4.4 多数据集混合、packed 嵌套注入与长度属性

#### 4.4.1 概念说明

真实 SFT 往往混合多个数据集。本框架的混合规则非常朴素：

- **顺序拼接**：按 `dataset_use` 里代号的书写顺序，逐个数据集把标注 `+=` 到总列表。谁写在前面，谁的样本就排在列表前部。
- **逐数据集采样**：`%X` 只作用于它修饰的那一个代号，互不影响。
- **packed 样本兼容**：若某个数据集的标注是「桶的列表、桶内又是样本的列表」（u4-l2 讲过的两层结构），加载循环会把 `data_path` 注入到**每个内层子样本**上，保证后续逐样本处理时基准路径不丢失。

另外，`__init__` 会依据 `data_args.data_packing` 决定取样本的函数 `item_fn`：开 packing 时用 `_get_packed_item`（整桶取出），否则用 `_get_item`（单样本取出）。这与 u4-l4 的 `pack_data.py` 产出物配套。

#### 4.4.2 核心流程

```text
dataset_use = "a%50,b_pack"
list_data_dict = [a 的 5 条] + [b_pack 的若干「桶」(每桶是 list)]
                        ↑ 顶层元素类型不统一？没关系：
                          data_packing=True 时 _get_packed_item 把「桶」整体取出再逐样本处理
                          data_packing=False 时 _get_item 期望顶层元素是普通样本 dict
```

长度属性（供训练侧做长度分组/装箱参考）：

- `lengths`：粗略估计 = 对话文本按空格切词数 + 每个含图样本固定加 128（视觉 token 的近似占位）。
- `pre_calculated_length`：若样本带 `num_tokens` 字段（由 `pack_data.py` 用 processor 真实预估后写入），优先使用精确值——这正是 u4-l4「pre-calculate 长度」的消费端。

#### 4.4.3 源码精读

加载循环中的 packed 分支：`isinstance(ann, list)` 判定该条标注是「桶」，对桶内每个子样本注入 `data_path`；普通样本则直接注入：

[qwen-vl-finetune/qwenvl/data/data_processor.py:L285-L291](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L285-L291)

`item_fn` 的选择：`data_packing` 开关决定后续 `__getitem__` 走「整桶」还是「单样本」路径：

[qwen-vl-finetune/qwenvl/data/data_processor.py:L304-L307](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L304-L307)

粗略长度属性 `lengths`（注意 `img_tokens = 128` 是占位近似，不是真实视觉 token 数）：

[qwen-vl-finetune/qwenvl/data/data_processor.py:L312-L321](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L312-L321)

精确长度属性 `pre_calculated_length`，读取 pack 阶段写入的 `num_tokens` 字段：

[qwen-vl-finetune/qwenvl/data/data_processor.py:L336-L340](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L336-L340)

注册表里 packed 与普通数据集并存的真实示例（`cambrian_737k_pack` 的注册与普通条目无任何差别）：

[qwen-vl-finetune/qwenvl/data/__init__.py:L9-L12](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/__init__.py#L9-L12)

README 的注意事项：多数据集时采样率逐数据集独立生效：

[qwen-vl-finetune/README.md:L206-L210](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L206-L210)

#### 4.4.4 代码实践

1. **实践目标**：验证多数据集顺序拼接与 packed 嵌套注入。
2. **操作步骤**：
   - 再注册一个 `my_dataset_pack`，`annotation_path` 指向一个手工构造的两层列表 JSON（示例代码生成的文件）：
     ```python
     import json
     bucket = [
         {"image": ["demo/images/10095.png", "demo/images/10149.png"],
          "conversations": [
              {"from": "human", "value": "<image>"},
              {"from": "human", "value": "<image>"},
              {"from": "gpt", "value": "ok"}]},
     ]
     json.dump([bucket], open("my_dataset_pack.json", "w"), ensure_ascii=False, indent=2)
     ```
     （仅为验证加载层， conversations 格式未必能通过后续张量化校验，本实践只看 `data_path` 注入。）
   - 在 4.3 脚本基础上把 `dataset_use` 改为 `"my_dataset,my_dataset_pack"`、`data_packing=True`，重新运行并打印：
     ```python
     for i, ann in enumerate(ds.list_data_dict[:4]):
         kind = "桶" if isinstance(ann, list) else "普通样本"
         paths = [a["data_path"] for a in ann] if isinstance(ann, list) else [ann["data_path"]]
         print(i, kind, paths)
     ```
3. **需要观察的现象**：列表前部是来自 `my_dataset` 的普通样本 dict，随后出现来自 `my_dataset_pack` 的「桶」（list），桶内**每个**子样本都带上了相同的 `data_path`。
4. **预期结果**：普通样本与桶按注册顺序分区排列；桶内所有子样本的 `data_path` 均被注入。若你想进一步看 `lengths` 属性，对含 `num_tokens` 字段的标注调用 `ds.pre_calculated_length`（需要先跑 u4-l4 的 pack 工具；待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：两个数据集 A（100 条）、B（20 条），想让它们在训练中以约 1:1 的量级混合，怎么写 `dataset_use`？
**答案**：`"a%20,b"`——A 抽 `int(100×0.2)=20` 条，B 全量 20 条，两者各占一半。混合靠「调小大数据集的采样率」实现，而不是给小数据集 `%X>100`（那会崩溃，见 4.2 练习 2）。

**练习 2**：`data_packing=True` 却喂了一个普通（非 packed）标注文件，会发生什么？
**答案**：加载层不会报错——`isinstance(ann, list)` 为假，普通 dict 也被注入 `data_path` 后拼接。取样阶段的 `_get_packed_item` 其实写了兜底分支：顶层元素是 dict 时本意是包一层列表后转交 `_get_item`（见 [qwen-vl-finetune/qwenvl/data/data_processor.py:L442-L448](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L442-L448)），但该分支第 L445 行把 `sources` 误写成了未定义的 `source`（源码笔误，行尾还带着 `# FIXME`），实际触发时会抛 `NameError`。结论：**packing 开关必须与标注结构匹配**，不要指望这个兜底分支救你（该行为待本地验证）。

**练习 3**：`lengths` 属性里 `img_tokens = 128` 意味着什么？
**答案**：它是一个**固定占位数**而非真实视觉 token 数——真实值取决于图片分辨率（u2-l2 的像素预算）。`lengths` 只用于粗粒度长度分组；需要精确长度时应走 pack_data.py 预计算 `num_tokens` 再由 `pre_calculated_length` 读取。

## 5. 综合实践

**任务：搭建你自己的「三数据集混合训练清单」。**

1. 准备三份标注：
   - `my_dataset.json`（u4-l2 制作的 5 条，或 demo 的 3 条）；
   - `my_dataset_pack.json`（4.4 实践构造的两层列表）；
   - 仓库自带 `demo/video.json`（3 条视频样本，`data_path` 同样填 `qwen-vl-finetune` 绝对路径）。
2. 在 `data/__init__.py` 中把三者分别注册为 `my_dataset`、`my_dataset_pack`、`demo_video`。
3. 写一个探针脚本（示例代码）依次验证：
   - `data_list(["my_dataset%60", "demo_video"])` 输出的两个配置各自携带正确的 `sampling_rate`；
   - `LazySupervisedDataset`（`dataset_use="my_dataset%60,demo_video"`，`data_packing=False`）打印的 `Total training samples` 等于 `int(5×0.6)+3`（用你自己的标注条数代入；demo 数据则验证 3 条全量时为 3）；
   - 连续运行两次脚本，对比两次实际采到的样本——体会 `random.sample` 无种子导致的不可复现性；
   - 给脚本开头加 `import random; random.seed(42)` 后再跑两次，确认结果稳定。
4. 把观察到的数字与现象写成一张表（数据集、标注条数、采样率、期望条数、实际条数），存入你的学习笔记。

预期：全部数字符合公式 \( n = \operatorname{int}(N \times rate) \)；加种子前后可复现性发生质变。这一步做完，你就完整掌握了从「一堆 JSON 文件」到「训练可用的样本清单」的全链路。

## 6. 本讲小结

- 注册表是一个 63 行的纯 Python 字典文件：每个数据集只有 `annotation_path`（标注文件）与 `data_path`（媒体基准目录）两个键，代号到配置的映射集中在 `data_dict`。
- `%X` 采样率语法由 `parse_sampling_rate` 用正则 `%(\d+)$` 从名字末尾剥出，换算为 `X/100`；解析只算数值，真正的 `random.sample` 抽取发生在 `LazySupervisedDataset.__init__` 读入标注之后，条数为 `int(N×rate)` 向下取整。
- `data_list` 对查表结果做浅拷贝再挂 `sampling_rate`，注册表不会被污染；查不到代号抛 `ValueError: do not find ...`。
- 加载循环按扩展名区分 JSONL/JSON，逐条注入 `data_path`（packed 桶则逐子样本注入），再按 `dataset_use` 书写顺序拼接成 `list_data_dict`；「lazy」意味着到此不做任何分词与图片读取。
- 边界三坑：逗号后不要带空格、`%X>100` 会崩、`random.sample` 无默认种子导致跨 run 不可复现。
- `data_packing` 开关决定 `item_fn` 走整桶还是单样本路径，必须与标注结构（是否两层列表）匹配，这是与 u4-l4 pack 工具的衔接点。

## 7. 下一步学习建议

- **u4-l4（数据打包 pack_data.py 与长度分桶）**：本讲多次出现的 `num_tokens` 字段与 packed 两层标注正是由它产出，学完你就能亲手把 `my_dataset.json` 打包并回接到本讲的加载链路。
- **u5-l3（LazySupervisedDataset 与标签掩码构造）**：本讲到 `list_data_dict` 为止；样本 dict 如何被 `_build_messages` 展开成 messages、如何生成 labels，属于下一单元的样本张量化内容。
- 延伸阅读：官方 README 的「Dataset config for training」一节（[qwen-vl-finetune/README.md:L157-L215](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L157-L215)）与 `tools/check_image.py`——在注册大量开源数据前先跑完整性校验，能省去大量「坏样本被静默跳过」的排查时间。
