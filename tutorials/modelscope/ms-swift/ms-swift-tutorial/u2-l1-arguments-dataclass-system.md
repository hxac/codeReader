# Arguments 数据类体系

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `BaseArguments` 是如何通过**多继承**把 6 个参数基类「拼装」成一个统一参数对象的，以及为什么采用这种「组合」而非单一巨型类。
- 识别 `DataArguments` / `ModelArguments` / `TemplateArguments` 三个最核心的参数组各自负责什么字段、各自的 `__post_init__` 做了哪些默认值推断。
- 描述一条命令行参数从 `argv` 到 `args` 对象的完整生命周期：`parse_args` 解析 → `__post_init__` 推断/校验 → `save_args` 持久化 → `from_pretrained`/`load_args_from_ckpt` 回载，并理解「剩余参数校验」的作用。
- 学会用 Python 自省（`fields()`、`__mro__`）快速查清任意一个 `swift` 命令到底接受哪些参数。

本讲是进阶层「参数与配置体系」的第一讲，承接 [u1-l3](u1-l3-directory-and-architecture.md) 讲到的「`swift/` 一级目录即职责」与「基类 + 注册表 + 参数开关」三件套，专门拆解其中的**参数层**。

## 2. 前置知识

- **dataclass（数据类）**：Python 标准库 `dataclasses` 提供的装饰器。给一个类打上 `@dataclass`，它会根据类体里带类型注解的字段（如 `lr: float = 1e-4`）自动生成 `__init__`、`__repr__` 等方法。ms-swift 的所有参数类都是 dataclass。
- **`__post_init__`**：dataclass 自动生成的 `__init__` 在最后会调用 `self.__post_init__()`（如果你定义了的话）。ms-swift 用它来做「构造之后的默认值推断与合法性校验」，例如「用户没传 `torch_dtype` 就从 `config.json` 读」。
- **多继承与 MRO**：Python 一个类可以继承多个父类（如 `class C(A, B)`）。`__mro__`（Method Resolution Order，方法解析顺序）是这个继承链的线性化顺序，决定了查找方法/属性时从左到右的优先级。
- **`field(default_factory=list)`**：dataclass 里给「可变默认值」（如列表、字典）用的写法，避免所有实例共享同一个列表对象。
- **HfArgumentParser**：HuggingFace `transformers` 提供的解析器，能把 dataclass 的字段自动转成命令行参数（`--字段名 值`），ms-swift 直接复用它。

如果你对 transformers 的 `TrainingArguments` 有印象会更好——本讲会看到 `SftArguments` 直接继承了它。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [swift/arguments/base_args/base_args.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py) | 定义 `BaseArguments`：多继承 6 个参数基类的「拼装总成」，含 `__post_init__`、`from_pretrained`、`load_args_from_ckpt`、`save_args` 等生命周期方法 |
| [swift/arguments/base_args/data_args.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/data_args.py) | `DataArguments`：数据集相关字段（dataset/val_dataset/streaming/采样语法等） |
| [swift/arguments/base_args/model_args.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/model_args.py) | `ModelArguments`：模型加载相关字段（model/model_type/task_type/torch_dtype/rope_scaling 等） |
| [swift/arguments/base_args/template_args.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/template_args.py) | `TemplateArguments`：对话模板与编码相关字段（template/system/max_length/padding_free/loss_scale 等） |
| [swift/arguments/sft_args.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/sft_args.py) | `SftArguments`：在 `BaseArguments` 之上再叠加 SwanlabArguments / TunerArguments / Seq2SeqTrainingArguments |
| [swift/utils/utils.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/utils.py) | `parse_args`：基于 HfArgumentParser 的命令行解析，返回 `(args, remaining_argv)` |
| [swift/pipelines/base.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/base.py) | `SwiftPipeline._parse_args`：调用 `parse_args` 并对剩余未识别参数做校验 |

## 4. 核心概念与源码讲解

### 4.1 BaseArguments 多继承组合

#### 4.1.1 概念说明

ms-swift 每一个 `swift <子命令>`（sft / infer / export / eval / deploy / app / rlhf）背后都有一个对应的「参数类」。这些命令的参数有大量重叠：它们都要指定**模型**、都要指定**数据**（哪怕只是验证集）、都要指定**对话模板**、都要指定**生成参数**。

如果为每个命令写一个独立的参数类，会产生巨量重复；如果写一个「无所不包」的超大参数类，又会把「只在训练时才用到的 `learning_rate`」和「只在推理时才用到的 `stream`」混在一起，难以维护。

ms-swift 的解法是 **mixin 组合**：把参数按职责切成 6 个独立的「参数基类」（mixin），再用**多继承**把它们拼成一个 `BaseArguments`：

- `GenerationArguments`：生成参数（temperature、top_k、top_p、max_new_tokens……）
- `QuantizeArguments`：量化参数（quant_method、quant_bits……）
- `DataArguments`：数据集参数
- `TemplateArguments`：模板与编码参数
- `ModelArguments`：模型加载参数
- `RayArguments`：Ray 分布式参数

`BaseArguments` 自己再补充少量「全局通用」字段（seed、tuner_type、adapters、packing、use_hf 等）。各个具体命令的参数类（如 `SftArguments`）则在 `BaseArguments` 之上继续叠加自己独有的参数组。

这是一种典型的「**用组合代替巨型继承树**」的设计：每个 mixin 自带「字段 + 处理逻辑」，拼装即可复用。

#### 4.1.2 核心流程

`BaseArguments` 的拼装与初始化流程：

```text
@dataclass
class BaseArguments(GenerationArguments, QuantizeArguments, DataArguments,
                    TemplateArguments, ModelArguments, RayArguments):
    # 1. 6 个父类的字段被「扁平」继承下来，成为 BaseArguments 的字段
    # 2. BaseArguments 自己再加全局字段（seed / tuner_type / adapters ...）
    # 3. @dataclass 自动生成 __init__，按 MRO 顺序收集所有字段
```

构造一个 `args` 对象时，dataclass 生成的 `__init__` 会：

1. 按 MRO（从最底层父类到 `BaseArguments`）收集全部字段，依次赋值。
2. 在最后调用 `self.__post_init__()`。

`__post_init__` 是这套体系的关键——它**不会自动链式调用各父类的 `__post_init__`**（dataclass 的 `__post_init__` 只有一个，不参与协作多继承），所以 `BaseArguments.__post_init__` 必须显式地逐个调用各父类的 `__post_init__`，让每个 mixin 都有机会做自己的默认值推断与校验。

#### 4.1.3 源码精读

`BaseArguments` 的类签名——注意它一次性继承了 6 个参数基类：

[swift/arguments/base_args/base_args.py:L46-L48](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L46-L48) —— `BaseArguments` 通过多继承把 6 个 mixin 的字段拼到一起。

`BaseArguments` 自己新增的「全局通用」字段：

[swift/arguments/base_args/base_args.py:L92-L121](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L92-L121) —— `tuner_backend` / `tuner_type` / `adapters` / `seed` / `packing` / `use_hf` / `ddp_timeout` 等不专属任何单一 mixin 的全局字段。

`__post_init__` 显式调用各父类后置初始化——这是 mixin 组合的「协作点」：

[swift/arguments/base_args/base_args.py:L172-L203](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L172-L203) —— 依次执行：打 peft 补丁、记录版本、初始化 hub、处理 adapters、初始化 checkpoint 目录、导入外部插件、解析 `model_kwargs`、计算分布式 rank，最后**显式调用** `ModelArguments.__post_init__(self)`、`QuantizeArguments.__post_init__(self)` 等五个父类的后置逻辑。

注意第 190–194 行的写法：

```python
ModelArguments.__post_init__(self)
QuantizeArguments.__post_init__(self)
TemplateArguments.__post_init__(self)
DataArguments.__post_init__(self)
RayArguments.__post_init__(self)
```

为什么不写 `super().__post_init__()`？因为这里要**确定性地控制每个 mixin 都执行一次**，而不是依赖 MRO 的隐式协作。这种「显式调用具名父类方法」是 mixin 模式里很常见的做法。

#### 4.1.4 代码实践

**实践目标**：用 Python 自省验证 `BaseArguments` 确实把 6 个 mixin 的字段「扁平」继承下来了。

**操作步骤**（需要在已安装 ms-swift 的环境中执行）：

```python
# 示例代码
from dataclasses import fields
from swift.arguments import BaseArguments

# 1. 看 BaseArguments 的继承链（MRO）
for cls in BaseArguments.__mro__:
    print(cls.__module__ + '.' + cls.__name__)
```

**需要观察的现象**：`__mro__` 列表里应出现 `GenerationArguments`、`QuantizeArguments`、`DataArguments`、`TemplateArguments`、`ModelArguments`、`RayArguments` 这 6 个类，顺序与类签名里的继承顺序一致。

**预期结果**：`fields(BaseArguments)` 返回的字段数远大于 `BaseArguments` 类体里写的那二十来个字段——多出来的全部来自 6 个父类。字段总数与「各父类字段数之和 + BaseArguments 自身字段数」基本吻合（少数覆盖字段会去重）。

> 待本地验证：精确字段总数取决于各 mixin 当前版本，请以本地 `len(fields(BaseArguments))` 实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：如果删掉 `BaseArguments.__post_init__` 里那一行 `ModelArguments.__post_init__(self)`，会发生什么？

**参考答案**：`ModelArguments.__post_init__` 里负责校验 `--model` 必填、推断 `torch_dtype`、加载 `model_info`/`model_meta`、设置 `model_suffix` 等。删掉它后，`args.model` 为 `None` 时不会立即报错，但后续依赖 `model_info`、`torch_dtype` 的逻辑会拿到未初始化的属性而崩溃。这说明显式后置调用链是 mixin 体系正确工作的前提。

**练习 2**：`BaseArguments` 为什么把 `RayArguments` 也作为父类，而不是只在需要 Ray 的命令里引入？

**参考答案**：因为训练、推理、导出等多种命令都可能跑在 Ray 上（跨机训练、异步 RL），把 Ray 参数下沉到 `BaseArguments` 可让所有命令统一接受 `--use_ray` 等参数，避免重复声明；即便不启用 Ray，这些字段也只是闲置，不影响其它逻辑。

---

### 4.2 DataArguments / ModelArguments / TemplateArguments 三大参数组

#### 4.2.1 概念说明

在 6 个 mixin 里，有 3 个几乎被所有命令共用、是理解整个参数体系的「三大件」：

| 参数组 | 职责一句话 | 典型字段 |
| --- | --- | --- |
| `DataArguments` | 「拿什么数据」 | `dataset`、`val_dataset`、`split_dataset_ratio`、`streaming`、`dataset_num_proc` |
| `ModelArguments` | 「用什么模型」 | `model`、`model_type`、`task_type`、`torch_dtype`、`attn_impl`、`rope_scaling` |
| `TemplateArguments` | 「数据如何变成 token」 | `template`、`system`、`max_length`、`truncation_strategy`、`padding_free`、`loss_scale` |

这三者的关系正好对应一次训练/推理的数据流：**模型**加载基座 → **数据**指定训练样本 → **模板**把样本格式化成 `input_ids`。

每个 mixin 都遵循同一套写法：

1. 在类体里声明字段（带类型注解 + 默认值 + `help` 元数据）。
2. 定义一个 `__post_init__`，做「用户没传就推断/校验」的收尾工作。
3. 提供一个 `get_xxx_kwargs()` 方法，把自己负责的字段打包成字典，供下游（loader / template / model loader）直接解包使用。

#### 4.2.2 核心流程

以 `DataArguments` 为例的构造后处理：

```text
__post_init__:
  ├── columns = json_parse_to_dict(self.columns)   # 把 JSON 字符串列映射转成 dict
  ├── 若指定了 val_dataset 或 streaming，则把 split_dataset_ratio 强制置 0
  ├── _init_custom_dataset_info()                  # 注册自定义数据集
  └── _init_val_dataset_exists()                   # 标记是否存在验证集
```

下游用法（在 `BaseArguments.load_dataset` 里）：

```text
kwargs = self.get_dataset_kwargs()   # 把 DataArguments 的字段打包成 dict
load_dataset(self.dataset, **kwargs)  # 解包传给数据加载器
```

`ModelArguments` 同理：`get_model_kwargs()` 打包、`get_model_processor` 解包；`TemplateArguments` 的字段则通过 `get_template_kwargs()` 打包给 `get_template`。这种「**字段组 + get_kwargs 打包**」的一致模式，让新增一个参数组变得非常机械化。

#### 4.2.3 源码精读

`DataArguments` 的数据集语法字段——注意 `dataset` 支持 `dataset_id:subset#count` 这种「ID:子集#采样数」语法：

[swift/arguments/base_args/data_args.py:L75-L100](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/data_args.py#L75-L100) —— `dataset`/`val_dataset`/`cached_dataset`/`split_dataset_ratio`/`streaming`/`columns`/`model_name` 等数据集字段；其中 `#count` 即 u1-l5 提到的「采样条数」语法来源。

`DataArguments.__post_init__` 的推断与校验：

[swift/arguments/base_args/data_args.py:L109-L121](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/data_args.py#L109-L121) —— 当显式指定了 `val_dataset` 或开启 `streaming` 时，自动把 `split_dataset_ratio` 置 0 并打印日志，避免「既切分又指定验证集」的矛盾。

`DataArguments.get_dataset_kwargs`——把本组字段打包成 dict 供下游解包：

[swift/arguments/base_args/data_args.py:L127-L145](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/data_args.py#L127-L145) —— 这就是「字段组 + get_kwargs 打包」模式的典型实现。

`ModelArguments` 的模型字段：

[swift/arguments/base_args/model_args.py:L68-L93](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/model_args.py#L68-L93) —— `model`/`model_type`/`task_type`/`torch_dtype`/`attn_impl`/`rope_scaling`/`max_model_len` 等模型加载字段。

`ModelArguments.__post_init__`——校验 `--model` 必填，并触发 dtype / model_info 推断：

[swift/arguments/base_args/model_args.py:L219-L226](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/model_args.py#L219-L226) —— 第 220–221 行：`model` 为 `None` 时直接 `raise ValueError`，这就是为什么 `swift sft` 不传 `--model` 会立刻报错的原因。

`TemplateArguments` 的模板字段（仅看类签名与首段字段）：

[swift/arguments/base_args/template_args.py:L12-L13](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/template_args.py#L12-L13) —— `TemplateArguments` 定义处；其字段（`template`/`system`/`max_length`/`truncation_strategy`/`padding_free`/`loss_scale` 等）在本文件后续声明，本讲的 u3-l3 会深入模板本身。

#### 4.2.4 代码实践

**实践目标**：确认三大参数组的字段确实都「贴」在 `BaseArguments` 实例上，且能被各自 `get_xxx_kwargs()` 取出。

**操作步骤**：

```python
# 示例代码
from swift.arguments import BaseArguments
import inspect

# 检查 DataArguments/ModelArguments/TemplateArguments 的字段是否都出现在 BaseArguments 上
for group_cls_name, method in [('DataArguments', 'get_dataset_kwargs'),
                               ('ModelArguments', 'get_model_kwargs')]:
    print(group_cls_name, '->', method in dir(BaseArguments))
```

**需要观察的现象**：`get_dataset_kwargs`、`get_model_kwargs` 都能在 `BaseArguments` 上找到（因为它们定义在父类，子类继承）。

**预期结果**：两个方法名都打印 `True`。这印证了「字段组 + get_kwargs」是从父类继承下来的可调用接口，下游 pipeline 不需要关心字段具体来自哪个 mixin。

> 待本地验证：完整构造一个 `BaseArguments` 实例需要传 `--model` 等必填项，建议改用 `swift sft --help` 在命令行直接观察三大参数组的字段被自动转成 `--xxx` 选项。

#### 4.2.5 小练习与答案

**练习 1**：`swift sft --dataset a.jsonl --val_dataset b.jsonl --split_dataset_ratio 0.05` 这条命令里，最终 `split_dataset_ratio` 会是多少？为什么？

**参考答案**：会被强制置为 `0.`。因为同时指定了 `val_dataset`，`DataArguments.__post_init__`（data_args.py 第 111–117 行）检测到 `len(self.val_dataset) > 0`，就把 `split_dataset_ratio` 重置为 0 并打印日志——既然已经显式给了验证集，就不再从训练集里切分。

**练习 2**：为什么 `model_type` 的 `help` 元数据里要写 `model_type choices: {list(MODEL_MAPPING.keys())}`？

**参考答案**：这是给 `--help` 输出和命令行补全用的——把当前注册的所有 model_type 列出来，方便用户查阅；同时也起到文档作用。注意它只是 `help` 文本，**不做强制枚举校验**（字段类型是 `Optional[str]` 而非 `Literal[...]`），因为模型注册表是动态可扩展的。

---

### 4.3 parse_args、剩余参数校验与 from_pretrained 回载

#### 4.3.1 概念说明

参数对象不是凭空出现的。一个 `args` 要经历完整的生命周期：

```text
命令行 argv
   │  parse_args（HfArgumentParser）
   ▼
args 对象（dataclass __init__）
   │  __post_init__（推断 + 校验）
   ▼
运行训练/推理
   │  save_args  →  落盘 args.json
   ▼
下次推理/导出：from_pretrained / load_args_from_ckpt  →  从 args.json 回载
```

这里有三个关键机制：

1. **`parse_args`**：把命令行字符串解析成 dataclass 实例，并返回「剩余未识别参数」`remaining_argv`。
2. **剩余参数校验**：如果 `remaining_argv` 非空，默认直接报错——这是 ms-swift 防止「参数名拼错却静默忽略」的保护机制（可用 `--ignore_args_error true` 放开，主要给 notebook 用）。
3. **`from_pretrained` / `load_args_from_ckpt`**：推理和导出时，从训练时落盘的 `args.json` 自动回载模型、模板、系统提示等，实现 u1-l5 讲到的「训练即所见，推理即所得」。

#### 4.3.2 核心流程

`parse_args` 的处理：

```text
parse_args(class_type, argv):
  1. 用 _patch_get_type_hints() 临时修补类型提示
     （处理 Union[str, dict, None] 的字段顺序问题）
  2. parser = HfArgumentParser([class_type])
  3. args, remaining_args = parser.parse_args_into_dataclasses(argv, return_remaining_strings=True)
  4. return args, remaining_args
```

剩余参数校验（在 `SwiftPipeline._parse_args`）：

```text
_parse_args(args):
  args, remaining_argv = parse_args(self.args_class, args)
  if len(remaining_argv) > 0:
      if getattr(args, 'ignore_args_error', False):
          logger.warning(...)        # 只警告
      else:
          raise ValueError(...)      # 直接报错
  return args
```

`from_pretrained` 的回载（推理/导出路径）：

```text
from_pretrained(checkpoint_dir):          # classmethod
  1. self = super().__new__(cls)          # 绕过 __init__，不要求传任何必填字段
  2. self.load_data_args = True
  3. self.load_args_from_ckpt()           # 读 args.json
  4. 遍历 BaseArguments 所有字段，缺失的补 None
  → 返回一个「基本只填了回载字段」的轻量 args
```

`load_args_from_ckpt` 的「选择性回载」策略是核心：并非把 `args.json` 全部覆盖到当前 args，而是分两类 key：

- **`force_load_keys`**：无条件覆盖（如 `tuner_type`、`task_type`、bnb 量化参数）——这些必须与训练时一致。
- **`load_keys`**：仅当**当前值为 `None` 或空列表**时才回填（如 `model`、`template`、`system`、`quant_method`）——保留用户在命令行显式覆盖的能力。
- **数据相关 key**：仅当 `load_data_args=True` 时才回载（推理默认不读训练数据配置）。

这种「强制 + 按需 + 条件」三档回载，既保证推理行为与训练一致，又允许局部覆盖。

#### 4.3.3 源码精读

`parse_args` 实现——基于 HfArgumentParser，返回剩余参数：

[swift/utils/utils.py:L174-L183](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/utils.py#L174-L183) —— 注意 `return_remaining_strings=True`，这正是「剩余参数校验」的数据来源；另外第 177–179 行还支持从环境变量 `RAY_SWIFT_ARGS` 读取 argv（Ray 跨进程传参用）。

剩余参数校验：

[swift/pipelines/base.py:L31-L41](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/base.py#L31-L41) —— `remaining_argv` 非空时，默认 `raise ValueError`；只有 `ignore_args_error=True` 才降级为 warning。这就是为什么你拼错一个参数名（如 `--lora_rnk`）会立刻被拦截。

`from_pretrained`——绕过 `__init__`、从 checkpoint 回载：

[swift/arguments/base_args/base_args.py:L224-L234](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L224-L234) —— `super().__new__(cls)` 跳过 dataclass 的 `__init__`（因此不要求 `--model` 等必填项），再补齐缺失字段为 `None`。

`load_args_from_ckpt`——`force_load_keys` 与 `load_keys` 的选择性回载：

[swift/arguments/base_args/base_args.py:L246-L301](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L246-L301) —— 第 251–259 行是 `force_load_keys`（无条件覆盖），第 261–288 行是 `load_keys`（按需回填），第 289 行用 `fields(DataArguments)` 单独取出数据字段，配合 `self.load_data_args` 决定是否回载数据配置。

触发回载的入口 `_init_ckpt_dir`——决定从哪个目录读 `args.json`：

[swift/arguments/base_args/base_args.py:L236-L244](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L236-L244) —— `ckpt_dir = get_ckpt_dir(model, adapters)`，当 `load_args=True` 时调用 `load_args_from_ckpt()`。

落盘的对应方法 `save_args`：

[swift/arguments/base_args/base_args.py:L303-L313](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L303-L313) —— 训练时由 master 进程把 `self.__dict__` 序列化成 `args.json`，供日后 `from_pretrained` 回载。

#### 4.3.4 代码实践

**实践目标**：亲手验证「剩余参数校验」——故意拼错一个参数名，观察 ms-swift 如何拦截。

**操作步骤**：

```bash
# 1. 正常命令（仅打印帮助，不需要 GPU）
swift sft --help | head -n 20

# 2. 故意拼错参数名：把 --lora_rank 写成 --lora_rnk
swift sft --model Qwen/Qwen3-4B --dataset swift/self-cognition --lora_rnk 8 2>&1 | tail -n 20

# 3. 用 --ignore_args_error true 放开校验，再观察行为差异
swift sft --model Qwen/Qwen3-4B --dataset swift/self-cognition \
  --lora_rnk 8 --ignore_args_error true 2>&1 | tail -n 20
```

**需要观察的现象**：

- 步骤 2 应当直接抛出 `ValueError`，错误信息里包含 `remaining_argv: ['--lora_rnk', '8']`（或类似），训练不会启动。
- 步骤 3 应当只打印一条 `WARNING ... remaining_argv`，然后继续往下走（可能因其它原因失败，但**不会**因为 `--lora_rnk` 而停）。

**预期结果**：默认严格模式下拼错参数立刻被拦下；`--ignore_args_error true` 时降级为 warning。这正是剩余参数校验保护你的方式。

> 待本地验证：步骤 2/3 的确切报错文本随版本略有差异，以本地输出为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `from_pretrained` 要用 `super().__new__(cls)` 而不是正常构造 `cls(...)`？

**参考答案**：正常构造会触发 dataclass 的 `__init__`，它要求所有必填字段（如 `ModelArguments` 要求 `--model`）都被传入，并执行一整套 `__post_init__` 推断逻辑。而 `from_pretrained` 的目的恰恰是「从 checkpoint 重建一个最小 args，字段由 args.json 填充」，因此用 `__new__` 绕过 `__init__`，再手动补齐缺失字段为 `None`，避免触发那些依赖完整输入的校验。

**练习 2**：`load_args_from_ckpt` 里，`template` 属于 `load_keys` 而非 `force_load_keys`。这意味着什么？

**参考答案**：意味着用户可以在推理时用 `--template xxx` **覆盖**训练时的模板，只要命令行显式传了就不会被 args.json 覆盖（因为 `load_keys` 只在「当前值为 None」时回填）。而 `tuner_type` 在 `force_load_keys` 里，所以推理时无法用 LoRA adapter 加载一个训练时是 `full` 的 checkpoint——这是合理约束，保证推理行为与训练一致。

**练习 3**：`parse_args` 为什么要在解析前用 `_patch_get_type_hints()` 临时改类型提示？

**参考答案**：某些字段的类型注解是 `Optional[Union[dict, str]]`（如 `columns`、`model_kwargs`），HfArgumentParser 在推断「这个字段对应什么命令行类型」时会用 `get_type_hints`。ms-swift 需要把 `Union[str, dict, None]` 与 `Union[dict, str, None]` 统一成同一种顺序，否则解析器可能把这类字段误判成不支持的类型而报错。`_patch_get_type_hints` 就是做这个归一化（utils.py 第 158–171 行）。

## 5. 综合实践

把本讲三大模块串起来，完成一个「**用自省读懂 SftArguments**」的小任务。

**背景**：`SftArguments` 是 `swift sft` 真正使用的参数类，它在 `BaseArguments` 之上又叠加了更多参数组。本任务要求你用代码列出 `SftArguments` 相比 `BaseArguments` **额外引入了哪些字段组**，并据此回答「为什么用 dataclass 组合而非单一巨型类」。

**操作步骤**：

1. 读 `SftArguments` 的类签名，记录它额外继承了哪些类：

   [swift/arguments/sft_args.py:L123-L124](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/sft_args.py#L123-L124) —— 应能看到 `SwanlabArguments, TunerArguments, BaseArguments, Seq2SeqTrainingArguments`。

2. 用自省代码计算「SftArguments 比 BaseArguments 多出的字段」：

   ```python
   # 示例代码
   from dataclasses import fields
   from swift.arguments import SftArguments, BaseArguments

   base_names = {f.name for f in fields(BaseArguments)}
   sft_names = {f.name for f in fields(SftArguments)}
   extra = sft_names - base_names
   print('SftArguments 额外字段数:', len(extra))
   print(sorted(extra)[:30])  # 只看前 30 个，避免输出过长
   ```

3. 把多出来的字段「归类」到来源类：
   - 来自 `Seq2SeqTrainingArguments`（transformers 训练参数）：如 `per_device_train_batch_size`、`num_train_epochs`、`learning_rate`、`save_steps`、`warmup_ratio`、`weight_decay`、`gradient_accumulation_steps`、`logging_steps`、`lr_scheduler_type`、`report_to`、`gradient_checkpointing`、`save_strategy` 等。
   - 来自 `TunerArguments`：如 `freeze_parameters`、`freeze_parameters_ratio`、`trainable_parameters` 等。
   - 来自 `SwanlabArguments`：`swanlab_*` 系列。
   - `SftArguments` 自身：`add_version`、`create_checkpoint_symlink`、`output_dir`、`deepspeed_autotp_size`、`zero_hpz_partition_size`、`fsdp` 等。

4. 据此撰写一段说明：**为什么用 dataclass 组合而非单一巨型类？**

**预期结论**（供你对照）：

- **复用**：`DataArguments`/`ModelArguments`/`TemplateArguments` 被 Sft/Infer/Export/Eval/Deploy 等多个命令共享，写在 `BaseArguments` 里一次，处处可用。
- **按需加载**：transformers 的 `Seq2SeqTrainingArguments` 是个上百字段的「巨无霸」，只在训练（sft/rlhf/pretrain）时需要，推理/导出不需要——通过组合，让训练命令额外继承它，而推理命令不继承，避免无关字段污染。
- **职责隔离**：每个 mixin 自带「字段 + `__post_init__` + `get_xxx_kwargs`」，改数据逻辑不会碰到模型逻辑，单一巨型类则会让任何一个改动都牵动全局。
- **可扩展**：新增一种命令（如未来加 `swift xxx`），只需声明 `class XxxArguments(某几个 mixin, BaseArguments)`，不必复制粘贴字段。

## 6. 本讲小结

- `BaseArguments` 通过**多继承**把 `GenerationArguments` / `QuantizeArguments` / `DataArguments` / `TemplateArguments` / `ModelArguments` / `RayArguments` 6 个 mixin「扁平」拼成一个统一参数对象，自己再补充少量全局字段。
- dataclass 的 `__post_init__` **不会自动链式调用**各父类，因此 `BaseArguments.__post_init__` 必须**显式**调用每个父类的 `__post_init__`——这是 mixin 组合正确工作的协作点。
- 三大参数组 `DataArguments` / `ModelArguments` / `TemplateArguments` 对应「拿什么数据 / 用什么模型 / 数据如何变 token」，每个都遵循「字段组 + `__post_init__` 推断 + `get_xxx_kwargs()` 打包」的一致模式。
- `SftArguments` 在 `BaseArguments` 之上再叠加 `SwanlabArguments` + `TunerArguments` + transformers 的 `Seq2SeqTrainingArguments`，体现「按需组合」。
- `parse_args` 用 HfArgumentParser 解析命令行并返回 `remaining_argv`；`SwiftPipeline._parse_args` 对非空剩余参数默认 `raise ValueError`（`--ignore_args_error true` 可放开），防止参数名拼错被静默忽略。
- 参数生命周期闭环：`parse_args` 构造 → `__post_init__` 推断 → `save_args` 落盘 `args.json` → 推理/导出时 `from_pretrained` / `load_args_from_ckpt` 按「强制/按需/条件」三档选择性回载。

## 7. 下一步学习建议

- **下一篇 [u2-l2](u2-l2-config-and-arg-parsing.md)** 讲 YAML/JSON 配置文件与 ENV 注入：本讲的 `parse_args` 只处理 `argv`，而 u2-l2 会讲清楚 `swift sft config.yaml` 时配置文件是如何被展开成 `argv` 再喂给这里的 `parse_args` 的，两者正好衔接。
- **横向延伸到模型与模板**：本讲的 `ModelArguments` / `TemplateArguments` 只是「参数声明」，真正的模型加载逻辑在 `swift/model/`（对应 [u3-l1](u3-l1-model-registry-and-loading.md)），模板的 `encode` 流程在 `swift/template/`（对应 [u3-l3](u3-l3-template-and-chat-format.md)）。建议读完 u2 后顺着 `get_model_kwargs` → `get_model_processor` 这条线进入 u3。
- **想立刻验证**：可以先在本机跑 `swift sft --help`，对照本讲列出的字段组，把帮助文档里的参数「对号入座」到 6 个 mixin 上，这是巩固本讲最快的办法。
