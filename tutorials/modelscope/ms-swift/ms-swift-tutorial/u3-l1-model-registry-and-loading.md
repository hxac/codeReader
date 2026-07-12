# 模型注册与加载机制

## 1. 本讲目标

在前面几讲里，我们已经知道 `swift sft --model xxx` 这条命令最终会进入训练管道。但训练管道要做的第一件事，就是「把 `--model` 指定的那个模型真正加载进来」。这一步看似简单——不就是个 `from_pretrained` 吗？其实不然：ms-swift 支持几百个模型，每个模型该用什么对话模板、哪些层该挂 LoRA、是否多模态、要不要走量化、是因果语言模型还是序列分类……这些信息必须有一个统一的地方登记和查询。

本讲学完后，你应当能够：

1. 说出 `ModelInfo` 与 `ModelMeta` 各自记录了什么，以及它们的区别。
2. 读懂 `MODEL_MAPPING` 注册表和 `register_model` 注册函数，并理解「一个 `model_type` 对应一份 `ModelMeta`」的设计。
3. 跟踪从 `model_id`（如 `Qwen/Qwen2.5-7B-Instruct`）到 `ModelMeta` 的匹配过程（`get_matched_model_meta` / `get_model_info_meta`）。
4. 读懂 `ModelLoader.load()` 与 `get_model_processor` 的加载主流程，理解 config / processor / model 三者是如何被依次构造出来的。
5. 解释 `ModelMeta.template` 字段是如何与模板系统（`get_template_meta`）联动的。

## 2. 前置知识

阅读本讲前，建议你先建立以下概念（来自 u1-l3「目录结构与模块化架构」）：

- **「基类 + 注册表 + CLI 参数开关」三件套**：ms-swift 几乎每个可扩展子系统都遵循这个范式——一个抽象基类、一个全局字典注册表（常命名为 `XXX_MAPPING`）、以及一个 CLI 参数用来选择。本讲的「模型子系统」就是最典型的例子。
- **懒加载 `_LazyModule`**：`swift/__init__.py` 通过懒加载组织对外 API，所以我们才能在顶层直接 `from swift.model import ModelMeta, register_model` 而不必关心内部文件路径。
- **transformers 的 `from_pretrained`**：本讲假设你知道 `AutoModelForCausalLM.from_pretrained(path)` 会按 `config.json` 找到对应模型类并加载权重。ms-swift 的 `ModelLoader` 正是对它的封装与增强。

另外需要区分两个容易混淆的词：

- **`model_id` / `model_path`**：模型在 ModelScope/HuggingFace 上的仓库 ID（如 `Qwen/Qwen2.5-7B-Instruct`）或本地目录。这是**用户视角**的名字，一个 `model_type` 下通常会登记几十个 `model_id`。
- **`model_type`**：ms-swift 内部的**唯一标识**（如 `qwen2_5`、`qwen3_vl`）。共享同一套「架构 + 模板 + loader」的模型归到同一个 `model_type`。注册表的键就是它。

## 3. 本讲源码地图

本讲聚焦在 `swift/model/` 这个一级模块，关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [swift/model/model_meta.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py) | 定义 `Model` / `ModelGroup` / `ModelMeta` / `ModelInfo` 四个数据类，以及全局注册表 `MODEL_MAPPING`、匹配函数 `get_matched_model_meta` 与综合解析函数 `get_model_info_meta`。 |
| [swift/model/register.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py) | 定义注册函数 `register_model`、加载器 `ModelLoader`（及其子类）、以及对外加载入口 `get_model_processor` / `get_processor`。 |
| [swift/model/models/qwen.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/models/qwen.py) | Qwen 系列模型的注册示例，是「如何调用 `register_model`」的最佳参考。 |
| [swift/template/register.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/register.py) | 模板系统的注册与匹配；其中 `get_template_meta` 消费了 `ModelMeta.template`，是模型↔模板的连接点。 |

> 提示：`swift/model/__init__.py` 通过 `from . import models` 触发所有 `models/*.py` 的执行——也就是说，注册动作发生在「import 时」，注册表在进程启动后就已经填满了。

## 4. 核心概念与源码精读

本讲拆成 4 个最小模块：

1. `ModelInfo` / `ModelMeta`：模型的元信息（数据结构基础）。
2. `MODEL_MAPPING` 注册表与 `register_model`：把模型登记进花名册。
3. 模型匹配：从 `model_id` 反查 `ModelMeta`（`get_matched_model_meta` / `get_model_info_meta`）。
4. `ModelLoader` 与 `get_model_processor`：把模型和 processor 真正载入内存。

### 4.1 ModelInfo / ModelMeta：模型的元信息

#### 4.1.1 概念说明

要把一个模型加载好，框架需要两类信息，分别由两个 dataclass 承载：

- **`ModelMeta`**：**「静态、可注册」** 的元信息。也就是开发者写代码时就确定好的、和具体某次下载无关的属性——这个 `model_type` 用哪个对话模板、是不是多模态、需要的 transformers 版本约束、有哪些对应的模型仓库 ID 等。它登记在 `MODEL_MAPPING` 里，跨进程共享。

- **`ModelInfo`**：**「动态、按需生成」** 的信息。也就是这次加载、这个具体下载目录里读出来的事实——权重在哪个本地目录、`config.json` 里写的精度是什么、最大上下文长度、量化方式与量化位数等。它由 `config.json` 等运行时文件解析得到。

一句话区分：**`ModelMeta` 描述「这一类模型」，`ModelInfo` 描述「这一次加载的这一个目录」**。

辅助这两个主类的还有两个小 dataclass：

- **`Model`**：登记一个具体的仓库来源，含 ModelScope ID（`ms_model_id`）、HuggingFace ID（`hf_model_id`）、本地路径（`model_path`）以及各自的 `revision`。
- **`ModelGroup`**：把若干 `Model` 打包成一组，并可附带组级别的覆盖属性（`template` / `ignore_patterns` / `requires` / `tags`），组级别属性优先级高于 `ModelMeta` 顶层。

#### 4.1.2 核心流程

`ModelMeta` 的字段可以分成四组来看：

| 分组 | 字段 | 含义 |
| --- | --- | --- |
| 身份 | `model_type` | 注册表键，唯一标识 |
| 来源 | `model_groups` | 多个 `ModelGroup`，列出对应的仓库 ID |
| 联动 | `template` / `model_arch` / `architectures` | 对话模板、架构描述、HF `architectures` 名 |
| 加载 | `loader` / `torch_dtype` / `additional_saved_files` | 用哪个加载器、默认精度、额外需保存的文件 |
| 能力标记 | `is_multimodal` / `is_reward` / `task_type` | 多模态/奖励模型/任务类型 |
| 环境 | `ignore_patterns` / `requires` / `tags` | 下载忽略、依赖约束、标签 |

`ModelMeta.__post_init__` 会做几件重要的推断与校验：

1. 若 `loader` 为 `None`，默认填 `ModelLoader`。
2. 把 `model_groups` 归一成列表。
3. 汇总 `candidate_templates`（自身 `template` + 各组的 `template`，去重保序），用于模板自动匹配。
4. 若 `model_type` 出现在 `MLLMModelType` / `RMModelType` 枚举里，自动置 `is_multimodal` / `is_reward` 为真。

#### 4.1.3 源码精读

`ModelMeta` 的定义与 `__post_init__`：

[swift/model/model_meta.py:56-95](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L56-L95) —— 定义 `ModelMeta`，并在 `__post_init__` 中完成默认 loader、`candidate_templates` 推导与多模态/奖励标记。

其中 `candidate_templates` 的推导逻辑很关键：

```python
self.candidate_templates = list(
    dict.fromkeys(t for t in [self.template] + [mg.template for mg in self.model_groups] if t is not None))
```

`dict.fromkeys(...)` 是一个保序去重的惯用法——先把自身模板放最前，再追加各组模板，去掉重复和 `None`。这个列表稍后会被模板系统的 `get_template_meta` 用作「自动匹配模板」的候选。

`ModelInfo` 的定义，注意它记录的是「这次加载」的事实：

[swift/model/model_meta.py:125-143](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L125-L143) —— 定义 `ModelInfo`，并在 `__post_init__` 里由 `model_dir` 推导出 `model_name`。

辅助 dataclass `Model` / `ModelGroup`：

[swift/model/model_meta.py:20-43](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L20-L43) —— `Model` 登记仓库来源，`ModelGroup` 打包多个 `Model` 并允许组级别覆盖（如 `tags=['financial']`）。

#### 4.1.4 代码实践

**实践目标**：亲手构造一个 `ModelMeta`，观察 `__post_init__` 的自动推断。

**操作步骤**：

```python
# 示例代码
from swift.model import Model, ModelGroup, ModelMeta

meta = ModelMeta(
    model_type='my_demo',
    model_groups=[
        ModelGroup([
            Model('MyOrg/Demo-1B', 'MyOrg/Demo-1B'),
            Model('MyOrg/Demo-7B'),
        ]),
        ModelGroup([Model('MyOrg/Demo-VL')], template='demo_vl'),
    ],
    template='demo',
)
print('loader      :', meta.loader.__name__)        # 期望: ModelLoader（被自动填充）
print('is_multimodal:', meta.is_multimodal)          # 期望: False（my_demo 不在 MLLMModelType）
print('candidates  :', meta.candidate_templates)     # 期望: ['demo', 'demo_vl']
```

**需要观察的现象**：

1. 我们没有传 `loader`，但 `meta.loader` 已经被填成了 `ModelLoader`。
2. `candidate_templates` 把组级别的 `'demo_vl'` 也收了进来，且 `'demo'` 在前、去重保序。

**预期结果**：打印出 `loader: ModelLoader`、`is_multimodal: False`、`candidates: ['demo', 'demo_vl']`。若 `model_type` 改成某个多模态枚举值（例如 `MLLMModelType.qwen_vl`），则 `is_multimodal` 会变成 `True`。**待本地验证**（取决于 `MLLMModelType` 的成员名）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ModelMeta` 要把 `template` 放在两个地方——顶层一个、`ModelGroup` 里又一个？

**答案**：顶层的 `template` 是这个 `model_type` 的「默认模板」；而某些同一 `model_type` 下的特殊仓库可能需要换模板，这时在对应 `ModelGroup.template` 覆盖即可，组级别优先级更高（见 4.3 的 `get_matched_model_meta`）。这避免了为换一个模板就新注册一个 `model_type`。

**练习 2**：`ModelInfo` 的字段里，哪些是从「这次下载的目录」读出来的，哪些是框架推断/注入的？

**答案**：`model_dir`、`torch_dtype`、`max_model_len`、`quant_method`、`quant_bits`、`rope_scaling`、`is_moe_model`、`config` 等来自 `config.json` 解析；而 `task_type`、`num_labels`、`problem_type`、`is_multimodal`（部分）是 `get_model_info_meta` 根据用户参数和 `ModelMeta` 标记推断后注入的；`model_name` 则由 `model_dir` 在 `__post_init__` 推导。

---

### 4.2 MODEL_MAPPING 注册表与 register_model

#### 4.2.1 概念说明

有了 `ModelMeta` 这个数据结构，下一步就是「把这些元信息集中登记起来，供全框架查询」。这个集中登记处就是全局字典 `MODEL_MAPPING`：

```python
MODEL_MAPPING: Dict[str, ModelMeta] = {}
```

它的键是 `model_type`，值是对应的 `ModelMeta`。整个 ms-swift 支持的模型清单，本质就是这个字典的内容。

登记动作由 `register_model` 完成。它被写在 `swift/model/models/*.py` 里——每 import 一个文件，就往 `MODEL_MAPPING` 里塞若干条。`swift/model/models/__init__.py` 会把所有这些文件 import 一遍，而 `swift/model/__init__.py` 又 `from . import models`，所以只要顶层 `import swift`（或任意触发模型模块加载的导入），注册表就满了。

#### 4.2.2 核心流程

`register_model` 的执行流程非常轻量：

1. 取出 `model_meta.model_type` 作为键。
2. 若该键已存在且未指定 `exist_ok=True`，抛 `ValueError`（防止重复注册覆盖）。
3. 若声明了 `model_arch`，调用 `get_model_arch` 把字符串名解析成 `ModelArch` 对象（用于 LoRA `target_modules` 选择，见下一讲 u3-l2）。
4. `MODEL_MAPPING[model_type] = model_meta`。

注意第 3 步：`model_arch` 在注册时就被「提前解析」成对象挂回 `ModelMeta` 上，这样后续每次加载都不必重复解析。

#### 4.2.3 源码精读

`register_model` 的实现：

[swift/model/register.py:31-42](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L31-L42) —— 注册函数：校验重名、解析 `model_arch`、写入 `MODEL_MAPPING`。

`MODEL_MAPPING` 的声明位置在 `model_meta.py`：

[swift/model/model_meta.py:122](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L122) —— 全局注册表，`Dict[str, ModelMeta]`。

一个真实的注册示例（Qwen 第一代），可以看到所有关键字段一起出现：

[swift/model/models/qwen.py:76-114](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/models/qwen.py#L76-L114) —— 用 `register_model(ModelMeta(...))` 登记了 `LLMModelType.qwen`：多个 `ModelGroup`、自定义 `QwenLoader`、`architectures=['QWenLMHeadModel']`、`template=TemplateType.qwen`、`model_arch=ModelArch.qwen`。

这段代码值得注意的几点：

- 第一个 `ModelGroup` 收纳了 chat/base/int4/int8 等几十个仓库 ID——它们共享同一个 `model_type='qwen'`。
- 第二个 `ModelGroup`（通义金融）带了 `tags=['financial']`，是一个打了标签的子分组。
- `loader=QwenLoader` 是 `ModelLoader` 的子类，说明同一类模型可以复用并扩展加载逻辑。

#### 4.2.4 代码实践

**实践目标**：把注册表「打开」看看里面到底有什么。

**操作步骤**：

```python
# 示例代码
import swift  # 触发 models/*.py 的注册
from swift.model import MODEL_MAPPING

keys = list(MODEL_MAPPING.keys())
print('已注册 model_type 数量:', len(keys))
print('qwen 相关:', [k for k in keys if 'qwen' in k.lower()])

meta = MODEL_MAPPING['qwen2_5']          # 待确认：键名以本地实际为准
print('template    :', meta.template)
print('arch        :', meta.model_arch)
print('groups 数量 :', len(meta.model_groups))
```

**需要观察的现象**：注册表里应该有几百个 `model_type`；Qwen 系列（`qwen` / `qwen2_5` / `qwen3` / `qwen3_vl` 等）各占一条，每条都有非空的 `template`。

**预期结果**：`len(keys)` 为数百量级；筛选出的 Qwen 相关键包含多个版本。具体键名（如是否叫 `qwen2_5`）**待本地验证**——可先 `print(keys)` 全量浏览再替换。

#### 4.2.5 小练习与答案

**练习 1**：如果我两次调用 `register_model` 注册同一个 `model_type`，会发生什么？怎么才能允许覆盖？

**答案**：第二次会抛 `ValueError(f'The {model_type} has already been registered ...')`。传 `register_model(meta, exist_ok=True)` 可允许覆盖（常用于自定义时替换默认元信息）。

**练习 2**：`register_model` 为什么要调用 `get_model_arch(model_meta.model_arch)`？

**答案**：把字符串形式的架构名（如 `'qwen'`）解析成 `ModelArch` 对象。解析后的对象记录了各层模块名（`ModelKeys`），供后续 LoRA 选择 `target_modules`、Megatron 权重转换等使用。提前解析避免每次加载重复解析。

---

### 4.3 模型匹配：从 model_id 到 ModelMeta

#### 4.3.1 概念说明

用户给的是 `--model Qwen/Qwen2.5-7B-Instruct`，框架拿到的是字符串 `model_id`。但训练管道需要的却是 `ModelMeta`（知道模板、架构）和 `ModelInfo`（知道目录、精度）。把字符串「翻译」成这两份元信息，就是匹配逻辑的职责。它由两个函数承担：

- **`get_matched_model_meta(model_id_or_path)`**：只看名字，纯靠字符串匹配在 `MODEL_MAPPING` 里反查 `ModelMeta`。不下载、不读 config。
- **`get_model_info_meta(model_id_or_path, ...)`**：在反查之外，还会**下载模型**、读取 `config.json` 生成 `ModelInfo`，并把 `ModelMeta` 与 `ModelInfo` 协调一致（比如自动推断 `model_type`、`task_type`、`torch_dtype`）。这是真正进入加载前的「总装配」函数。

#### 4.3.2 核心流程

**字符串匹配 `get_matched_model_meta` 的规则**：

1. 先用 `get_model_name` 把 `model_id` 规范成 `model_name`（取最后一段、转小写、兼容 HF 缓存目录与 ModelScope `___` 转义）。
2. 遍历 `MODEL_MAPPING` 里每个 `ModelMeta`，对它的每个 `ModelGroup` 调 `get_matched_model_group`：只要组里任一 `Model` 的 `ms_model_id` / `hf_model_id` / `model_path` 末段（小写）等于 `model_name`，就算命中该组。
3. 命中后，把该 `ModelGroup` 的非空属性**覆盖**到一份 `deepcopy(ModelMeta)` 上（组级别优先），返回。

注意第 2 步用的是「末段小写匹配」。所以 `Qwen/Qwen2.5-7B-Instruct` 会被简化成 `qwen2.5-7b-instruct` 去比对。

**总装配 `get_model_info_meta` 的流程**：

1. `get_matched_model_meta` 拿到候选 `model_meta`。
2. `safe_snapshot_download` 按需下载模型到本地 `model_dir`（带 `ignore_patterns`）。
3. `_get_model_info` 读 `config.json`，解析出 `torch_dtype` / `max_model_len` / `quant_info` / `is_moe_model` / `is_multimodal` 等。
4. 若 `model_type` 仍未知，依次尝试：从 `args.json` 读 → 从 `config.architectures` 反查（`get_matched_model_types`）→ 报错让用户指定。
5. 协调 `torch_dtype`（优先用户参数 > `ModelMeta.torch_dtype` > config 默认）。
6. 推断 `task_type`：默认 `causal_lm`；有 `num_labels` 则 `seq_cls`；奖励模型强制 `num_labels=1`；`ModelMeta.task_type` 可覆盖。
7. `check_requires` 检查依赖（量化还需额外装 `bitsandbytes` / `autoawq` 等）。
8. 返回 `(model_info, model_meta)`。

一个重要的兜底：如果模型完全没被注册（如某个新上的纯文本模型），框架不会直接报错，而是临时造一个 `ModelMeta(None, [], ModelLoader, template='dummy', model_arch=None)`，走「裸 transformers」路径；但若是多模态模型未注册，则直接报错要求显式指定 `model_type`。

#### 4.3.3 源码精读

字符串匹配的核心：

[swift/model/model_meta.py:97-104](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L97-L104) —— `get_matched_model_group`：对三种 id 字段做「末段小写」比对。

[swift/model/model_meta.py:162-171](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L162-L171) —— `get_matched_model_meta`：遍历注册表，命中后用 `asdict(model_group)` 把组级别属性覆盖到 `deepcopy` 的 meta 上。

`get_model_name` 的规范化处理（兼容多种路径写法）：

[swift/model/model_meta.py:146-159](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L146-L159) —— 取末段、兼容 HF `models--org--name` 缓存路径、Windows 反斜杠、ModelScope `___` 转义。

总装配函数 `get_model_info_meta`（重点看下载、解析、协调三段）：

[swift/model/model_meta.py:247-325](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L247-L325) —— 下载 → `_get_model_info` 读 config → `model_type` 回退链（args.json / architectures）→ `torch_dtype` / `task_type` 协调 → 依赖检查 → 返回 `(model_info, model_meta)`。

其中未注册纯文本模型的兜底（造 `dummy` meta）：

[swift/model/model_meta.py:280-287](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L280-L287) —— 找不到匹配且非多模态时，临时创建 `template='dummy'` 的 `ModelMeta`，让训练仍能以裸 transformers 方式跑起来。

`model_type` 的 architectures 反查（`get_matched_model_types`）：

[swift/model/model_meta.py:174-193](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L174-L193) —— 由 `MODEL_MAPPING` 反向构建 `architectures → model_type` 映射，用于在用户没给 `model_type` 时自动推断。

#### 4.3.4 代码实践

**实践目标**：用不同形式的 `model_id` 喂给匹配函数，观察匹配结果，理解「末段小写」规则。

**操作步骤**：

```python
# 示例代码
from swift.model import get_matched_model_meta

for mid in ['Qwen/Qwen2.5-7B-Instruct', 'qwen2.5-7b-instruct', 'AI-ModelScope/xxx-Qwen2.5-7B-Instruct']:
    meta = get_matched_model_meta(mid)
    print(f'{mid:45s} -> template={meta.template if meta else None}, '
          f'model_type={meta.model_type if meta else None}')
```

**需要观察的现象**：

1. 前两种写法（带 org 前缀 / 裸末段）都应命中同一个 `ModelMeta`，得到相同的 `template`。
2. 第三种末段名相同、但加了别的 org 前缀的，由于匹配只看末段，仍会命中同一个 meta——这正是「末段小写」匹配的副作用（也是为何不同组织的同名模型需要小心）。

**预期结果**：三条都能匹配到 Qwen2.5 对应的 meta（前提是末段名确为 `Qwen2.5-7B-Instruct`）。若末段名在注册表里不存在，`get_matched_model_meta` 返回 `None`。**待本地验证**：不同 Qwen 版本的注册末段名以本地 `MODEL_MAPPING` 为准。

> 注意：`get_matched_model_meta` 是纯内存操作，不联网，所以这个实践很轻量、可以放心跑。

#### 4.3.5 小练习与答案

**练习 1**：`get_matched_model_meta` 命中后为什么要把 `ModelGroup` 的属性「覆盖」回 `ModelMeta`，而不是直接返回 `ModelMeta`？

**答案**：因为 `ModelGroup` 可以携带比顶层 `ModelMeta` 更细粒度的覆盖（如某组的 `template`、`tags`、`ignore_patterns`）。覆盖后返回的是「针对这个具体仓库 ID 的、最终的」`ModelMeta`，让下游加载直接用即可，无需再关心组级别差异。

**练习 2**：为什么多模态模型未注册时会直接报错，而纯文本模型未注册却允许走 `dummy` 兜底？

**答案**：纯文本因果语言模型的加载流程相对统一，裸 transformers 的 `AutoModelForCausalLM` 通常能直接处理，所以兜底风险可控；而多模态模型涉及图像/音频 processor、占位符、视觉塔等大量定制逻辑，没有正确的 `model_type` 与模板几乎必然出错，因此宁可显式报错让用户指定 `model_type`。

---

### 4.4 ModelLoader 与 get_model_processor

#### 4.4.1 概念说明

元信息就绪后，就轮到真正的加载。ms-swift 把加载逻辑抽象成一个加载器基类 `BaseModelLoader`，它定义了一个极简契约：

```python
class BaseModelLoader(ABC):
    def __init__(self, model_info, model_meta, *args, **kwargs): ...
    def load(self) -> Tuple[Optional[PreTrainedModel], Processor]: ...
```

即「构造时接收 `model_info` 与 `model_meta`，调用 `load()` 返回 `(model, processor)`」。默认实现是 `ModelLoader`；特殊模型可以子类化它，例如 `SentenceTransformersLoader`（句向量）、`RewardModelLoader`（奖励模型）、`QwenLoader`（Qwen 特有修复）。

对外暴露的两个高层入口是：

- **`get_model_processor(model_id_or_path, ...)`**：一站式加载，返回 `(model, processor)`。内部完成「匹配元信息 → 下载 → 构造 loader → loader.load()」全流程。
- **`get_processor(model_id_or_path, ...)`**：只加载 processor（`load_model=False` 的便捷封装），适合「只想要分词器/多模态预处理器、不想拉权重」的场景（比如 4.3 之后的模板实践）。

> `processor` 在这里是一个泛称：对纯文本模型它是 `tokenizer`；对多模态模型它是 `AutoProcessor`（内含 tokenizer + 图像/音频处理器）。`ModelLoader` 会按目录里有没有 `preprocessor_config.json` / `processor_config.json` 来自动选择用 `AutoProcessor` 还是 `AutoTokenizer`。

#### 4.4.2 核心流程

`get_model_processor` 的总体编排：

1. `load_model=True` 时先 `patch_mp_ddp()`（多卡相关补丁）。
2. 调 `get_model_info_meta(...)` 得到 `(model_info, model_meta)`（即 4.3 的总装配）。
3. 决定 `device_map`（默认 `get_default_device_map()`），连同 `quantization_config` / `max_memory` 装进 `model_kwargs`。
4. 实例化加载器：`loader = model_meta.loader(model_info, model_meta, load_model=..., **加载选项)`。
5. `return loader.load()`。

注意第 4 步用的是 **`model_meta.loader`**——也就是说，`ModelMeta` 里登记的 loader 类决定了具体加载行为。这就是「同一 `model_type` 复用 loader、特殊模型换 loader」的机制。

`ModelLoader.load()` 内部是一个有序的三段式（外加前后处理）：

```
load():
  ├─ get_config(model_dir)          # 读 config.json
  ├─ _postprocess_config(config)     # 注入 dtype / rope_scaling / max_model_len / attn_impl / num_labels
  ├─ _get_model_processor(...)       # 并行拿 processor 和 model
  │    ├─ get_processor(model_dir)   # AutoProcessor / AutoTokenizer
  │    └─ get_model(model_dir, ...)  # AutoModelForCausalLM 等 + 各种 patch 上下文
  ├─ _postprocess_processor(processor)  # 修 pad/eos、挂 model_info/model_meta
  ├─ _postprocess_model(model)           # 挂 model_info/model_meta、初始化 generation_config
  └─ _add_new_special_tokens(...)        # 按需扩词表
```

`get_model` 里有一段重要的「按 `task_type` 选 AutoModel 类」逻辑：`seq_cls` / `reranker` 走 `AutoModelForSequenceClassification`（还会 patch tie_word_embeddings 等），否则走 `AutoModelForCausalLM`；并且会用 `patch_automodel` 等上下文管理器在加载时给模型打补丁（比如挂上 `model_info` / `model_meta` 属性，这正是后续 `model.model_meta.is_multimodal` 之类调用的来源）。

#### 4.4.3 源码精读

加载器抽象基类：

[swift/model/model_meta.py:45-53](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L45-L53) —— `BaseModelLoader`：定义 `__init__(model_info, model_meta, ...)` 与 `load()` 契约。

默认加载器 `ModelLoader` 的加载主入口 `load()`：

[swift/model/register.py:470-482](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L470-L482) —— `ModelLoader.load()`：config → postprocess → 拿 model/processor → 后处理 → 扩词表。

按目录内容自动选择 processor 类：

[swift/model/register.py:259-268](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L259-L268) —— `get_processor`：目录有 `preprocessor_config.json`/`processor_config.json` 用 `AutoProcessor`，否则用 `AutoTokenizer`。

按 `task_type` 选择模型类并打 patch：

[swift/model/register.py:270-331](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L270-L331) —— `get_model`：`seq_cls`/`reranker` 用 `AutoModelForSequenceClassification`，否则 `AutoModelForCausalLM`；用 `patch_automodel` 等上下文挂载 `model_info` / `model_meta` 等属性。

把 `model_info` / `model_meta` 挂回 model（这就是 `model.model_meta` 的来源）：

[swift/model/register.py:342-356](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L342-L356) —— `_postprocess_model`：`model.model_info = ...; model.model_meta = ...; model.model_dir = ...`，并初始化 `generation_config`。

对外的一站式入口与文档（注意 docstring 里给出的用法示例）：

[swift/model/register.py:516-630](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L516-L630) —— `get_model_processor`：装配 `model_info`/`model_meta` → 组装 `model_kwargs` → `model_meta.loader(...).load()`。docstring 示例正是 `get_model_processor('Qwen/Qwen2.5-7B-Instruct')`。

只取 processor 的便捷封装：

[swift/model/register.py:633-664](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L633-L664) —— `get_processor`：内部就是 `get_model_processor(..., load_model=False)[1]`，避免拉权重。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：用 `get_model_processor` 加载一个 Qwen 模型的 processor（不拉权重，省时省显存），打印 `model_info` 与 `model_meta`，并验证 `model_meta.template` 能被模板系统识别。

**操作步骤**（推荐轻量版：只取 processor）：

```python
# 示例代码
from swift.model import get_processor

processor = get_processor('Qwen/Qwen2.5-7B-Instruct')   # load_model=False，不下载权重
model_info = processor.model_info
model_meta = processor.model_meta

print('=== ModelInfo ===')
print('model_type   :', model_info.model_type)
print('model_dir    :', model_info.model_dir)
print('torch_dtype  :', model_info.torch_dtype)
print('max_model_len:', model_info.max_model_len)
print('quant_method :', model_info.quant_method)
print('is_multimodal:', model_info.is_multimodal)

print('=== ModelMeta ===')
print('model_type   :', model_meta.model_type)
print('template     :', model_meta.template)
print('candidates   :', model_meta.candidate_templates)
print('model_arch   :', model_meta.model_arch)
print('is_multimodal:', model_meta.is_multimodal)
```

接着验证 `template` 与模板系统的联动：

```python
# 示例代码：用 ModelMeta.template 喂给模板系统
from swift.template import get_template_meta
tmeta = get_template_meta(model_info, model_meta)   # 不传 template_type，自动用 model_meta.template
print('resolved template_type:', tmeta.template_type)
```

**需要观察的现象**：

1. `processor.model_info` / `processor.model_meta` 已经被 `ModelLoader._postprocess_processor` 挂上了——这正是 4.4.3 里 `tokenizer.model_info = self.model_info` 的效果。
2. `model_meta.template` 是一个具体模板名（如 `qwen2_5`），且出现在 `candidate_templates` 里。
3. `get_template_meta(model_info, model_meta)` 在不显式传 `template_type` 时，会取 `model_meta.template` 作为默认，从而把「模型」和「模板」自动绑定起来——这就是「训练即所见，推理即所得」的元信息基础。

**预期结果**：`model_info.model_type` 与 `model_meta.model_type` 一致；`model_meta.template` 非空；`get_template_meta` 返回的 `template_type` 等于 `model_meta.template`。**待本地验证**：具体 `model_type` / `template` 名以本地注册表与下载到的模型为准；首次运行会联网下载 tokenizer/processor 文件。

> 想加载完整模型权重的读者可改用 `model, processor = get_model_processor('Qwen/Qwen2.5-7B-Instruct', torch_dtype=torch.float16)`，但这需要 GPU 与下载几 GB 权重，建议本地具备条件时再尝试。

#### 4.4.5 小练习与答案

**练习 1**：`model.model_meta.is_multimodal` 这个属性是从哪里来的？为什么 model 对象上会有「元信息」属性？

**答案**：来自 `ModelLoader._postprocess_model` 里的 `model.model_meta = self.model_meta`（以及 `_postprocess_processor` 里的 `tokenizer.model_meta = ...`）。加载器在加载完成后，把本次加载所依据的 `model_info` / `model_meta` 主动挂到 model 和 processor 上，方便下游管道（如训练器、推理引擎）随时取用，而不必再各自查注册表。

**练习 2**：`get_processor` 和 `get_model_processor(load_model=False)` 是什么关系？为什么不直接写两个独立实现？

**答案**：`get_processor` 就是 `get_model_processor(..., load_model=False)[1]` 的薄封装（见源码末尾）。复用同一套匹配/下载/构造逻辑，只是跳过了 `get_model` 这一步，避免代码重复，也保证了「只取 processor」与「全量加载」得到的 `model_meta` / `model_info` 完全一致。

**练习 3**：如果我希望某个新模型加载时多跑一段自定义修复代码，应该改哪里？

**答案**：写一个 `ModelLoader` 的子类，重写 `get_model`（或 `get_processor`）加入修复逻辑，然后在注册该模型时把 `ModelMeta.loader` 指向你的子类（参考 `QwenAudioLoader` / `RewardModelLoader`）。这样修复只对该 `model_type` 生效，不影响其他模型。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个小任务：**为「一个本地未注册的纯文本模型」走一遍从识别到加载的全过程，并观察框架的兜底行为。**

任务步骤：

1. 准备一个本地 transformers 模型目录（可以是从 HF/ModelScope 任意下的小模型，比如 `Qwen/Qwen3-0.6B` 之类），记为 `LOCAL_DIR`。
2. 编写脚本，依次调用：
   - `get_matched_model_meta(LOCAL_DIR)` —— 看名字能否命中注册表；
   - `get_model_info_meta(LOCAL_DIR)` —— 看总装配后 `model_type` 是被推断出来还是走了 `dummy` 兜底，`task_type` 落到哪个值；
   - `get_processor(LOCAL_DIR)` —— 拿到 processor，打印 `processor.model_info` 与 `processor.model_meta`；
   - `get_template_meta(processor.model_info, processor.model_meta)` —— 看 `template_type` 是否被解析（`dummy` 时会怎样？）。
3. 回答三个问题：
   - 这个模型的 `model_type` 是怎么被确定的（注册表命中 / architectures 反查 / args.json / dummy）？
   - `model_meta.template` 在「已注册」和「未注册」两种情况下分别是什么？对后续模板加载意味着什么？
   - `model_info` 里哪些字段确实来自 `config.json`，哪些来自框架推断？

4. 进阶（可选）：仿照 `tests/llm/test_custom.py`，用 `register_model(ModelMeta(...))` 给这个模型登记一个自定义 `model_type`，指定一个已有模板（如 `qwen`），然后重新跑第 2 步，对比 `get_template_meta` 的输出变化——体会「注册让模型自动获得正确模板」的效果。

> 这个任务不需要 GPU，但会联网下载 tokenizer/processor 与可能的 config 文件；若网络受限，可改用 `LOCAL_DIR` 指向已下载好的目录。具体输出**待本地验证**。

## 6. 本讲小结

- ms-swift 用两个 dataclass 切分模型信息：`ModelMeta` 是「可注册、静态」的类级别元信息；`ModelInfo` 是「按需生成、动态」的本次加载事实。
- 全局字典 `MODEL_MAPPING` 是所有支持的模型的「花名册」，键是 `model_type`；`register_model` 在 `models/*.py` import 时填充它，并会把 `model_arch` 提前解析成对象。
- `get_matched_model_meta` 靠「末段小写」字符串匹配把 `model_id` 反查到 `ModelMeta`；`get_model_info_meta` 在此基础上下载、读 config、协调 `model_type`/`torch_dtype`/`task_type`，是加载前的总装配函数，未注册纯文本模型会走 `dummy` 兜底。
- 加载由 `BaseModelLoader` 抽象、`ModelLoader` 默认实现：`load()` 依次构造 config → processor → model，并把 `model_info`/`model_meta` 挂回对象；特殊模型通过子类化 loader 定制（如 `QwenAudioLoader`、`RewardModelLoader`）。
- 对外入口 `get_model_processor` 一站式返回 `(model, processor)`，`get_processor` 是其「不拉权重」的便捷封装；选用哪个 loader 由 `ModelMeta.loader` 决定。
- `ModelMeta.template`（及 `candidate_templates`）是模型↔模板的连接点：`get_template_meta` 在不显式指定模板时取它作默认，从而让「加载一个模型」自动带上「正确的对话格式」。

## 7. 下一步学习建议

本讲解决了「模型如何被识别与加载」。接下来建议：

1. **u3-l2 模型架构 ModelArch 与 ModelKeys**：本讲多次提到 `model_arch` 在注册时被解析成对象，但没展开它内部结构。下一讲会讲 `ModelKeys` 如何定位 linear/embedding/norm 层，以及这如何决定 LoRA `target_modules=all-linear` 的实际命中范围——与本讲的 `register_model` 直接相关。
2. **u3-l3 Template 体系与对话格式**：本讲只展示了 `ModelMeta.template` → `get_template_meta` 的连接点，下一讲深入 `Template.encode` 如何把 messages 变成 token 序列、`labels` 哪些部分被置 `-100`。
3. **想动手扩展的读者**：直接阅读 `swift/model/models/qwen.py` 与 `tests/llm/test_custom.py`，尝试用 `register_model` + `register_template` 注册一个全新模型，这是 u10-l3「自定义模型、模板与 Agent 注册」的预习。
