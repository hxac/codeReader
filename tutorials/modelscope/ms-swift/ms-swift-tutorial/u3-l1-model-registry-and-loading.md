# 模型注册与加载机制

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `MODEL_MAPPING` 注册表是什么、由谁填充、用什么键查找。
- 看懂 `ModelMeta` / `ModelInfo` 这两份「模型档案」分别记录了什么，以及它们在加载链路里各自的角色。
- 跟踪一次 `get_model_processor(...)` 调用，描述它如何从「一个模型 id 字符串」一路走到「`(model, processor)`」。
- 理解 `ModelMeta.template` 字段如何把「模型加载」和「对话模板」这两个子系统粘合在一起。

本讲是 u3「模型与模板」单元的第一篇，只聚焦**模型这一侧**；模板本身的 `encode`/对话格式化会在 [u3-l3](u3-l3-template-and-chat-format.md) 详讲。

## 2. 前置知识

阅读本讲前，你需要建立以下几个直觉（若不熟悉可先看前置讲义）：

- **ms-swift 的统一扩展范式**（来自 [u1-l3](u1-l3-directory-and-architecture.md)）：全项目几乎每个子系统都遵循「`base.py` 基类 + `mapping.py` 的 `*_map`/`*_MAPPING` 注册表 + CLI 参数开关」三件套。模型子系统正是这套范式的典型代表。
- **transformers 的 `AutoModelForCausalLM.from_pretrained`**：ms-swift 的加载最终会落到 transformers 的 Auto 类上，它做的事是在此之上**做匹配、做兜底、做 patch**，而不是另起炉灶。
- **「模型 id」与「模型类型」的区别**：`Qwen/Qwen2.5-7B-Instruct` 是模型 id（仓库地址），而 `qwen2` / `qwen3` 这种是 ms-swift 内部的「模型类型（model_type）」。一个 model_type 背后挂着一族结构相同的模型，共享同一套 `template`、`loader`、`model_arch`。本讲的核心就是把「id → type」这条映射讲清楚。

如果你还不清楚 `swift` 命令最终如何进入模型加载，可回头看 [u1-l4](u1-l4-cli-entry-and-dispatch.md) 的 CLI 分发链路。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [swift/model/model_meta.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py) | 定义 `ModelMeta`/`ModelInfo`/`ModelGroup`/`Model` 数据类，持有全局注册表 `MODEL_MAPPING`，并实现「id → meta」的匹配逻辑 `get_matched_model_meta` 与「id → info+meta」的 `get_model_info_meta`。 |
| [swift/model/register.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py) | 定义 `ModelLoader`（真正的加载器）、顶层入口 `get_model_processor` / `get_processor`，以及注册函数 `register_model`。 |
| [swift/model/models/qwen.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/models/qwen.py) | 一个**具体的注册样例**：演示如何用 `register_model(ModelMeta(...))` 把 Qwen 系列登记进 `MODEL_MAPPING`，并自定义 `QwenLoader`。 |
| [swift/model/constant.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/constant.py) | 定义 `LLMModelType`/`MLLMModelType`/`RMModelType`/`ModelType` 等命名空间，提供所有合法的 model_type 字符串常量。 |
| [swift/template/register.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/register.py) | 模板子系统的注册表。其中的 `get_template_meta` 会读取 `model_meta.template`，是「模型↔模板」联动的落点。 |

> 说明：`swift/model/__init__.py` 通过 `from . import models` 触发各 `models/*.py` 模块在导入时执行 `register_model(...)`，从而填充 `MODEL_MAPPING`。这就是「导入即注册」。

## 4. 核心概念与源码讲解

### 4.1 MODEL_MAPPING 注册表与 register_model 注册机制

#### 4.1.1 概念说明

ms-swift 要支持几百个模型，但不可能为每个模型 id 写一套加载逻辑。它的做法是**把「结构相同的一族模型」抽象成一个 model_type**，再用一张全局字典把每个 model_type 映射到它的「档案」`ModelMeta`。这张字典就是 `MODEL_MAPPING`：

```python
MODEL_MAPPING: Dict[str, ModelMeta] = {}
```

它定义在 [swift/model/model_meta.py:122](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L122)，键是 model_type（如 `'qwen3'`），值是 `ModelMeta` 对象。这张表在包导入时被各个 `models/*.py` 文件填充。

为什么用「模块导入时副作用」来填充？因为 ms-swift 用了 `_LazyModule` 懒加载（见 [u1-l3](u1-l3-directory-and-architecture.md)），只有真正用到某类模型时，对应的注册代码才会执行，避免一次性把所有重型依赖都拉起来。

#### 4.1.2 核心流程

注册一条模型的流程非常简单：

1. 在 `swift/model/models/xxx.py` 里构造一个 `ModelMeta(...)`。
2. 调用 `register_model(model_meta)`。
3. `register_model` 校验 model_type 不重复，解析 `model_arch`，写入 `MODEL_MAPPING`。

伪代码：

```text
register_model(meta):
    if meta.model_type in MODEL_MAPPING and not exist_ok:
        raise ValueError(重复注册)
    if meta.model_arch:                       # 字符串 -> ModelArch 对象
        meta.model_arch = get_model_arch(meta.model_arch)
    MODEL_MAPPING[meta.model_type] = meta
```

#### 4.1.3 源码精读

注册函数本体在 [swift/model/register.py:31-42](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L31-L42)：

```python
def register_model(model_meta: ModelMeta, *, exist_ok: bool = False) -> None:
    from .model_arch import get_model_arch
    model_type = model_meta.model_type
    if not exist_ok and model_type in MODEL_MAPPING:
        raise ValueError(f'The `{model_type}` has already been registered in the MODEL_MAPPING.')
    if model_meta.model_arch:
        model_meta.model_arch = get_model_arch(model_meta.model_arch)
    MODEL_MAPPING[model_type] = model_meta
```

关键点：① 默认不允许重复注册（`exist_ok=False`），用来在开发期尽早发现「两个 model_type 撞车」的 bug；② `model_arch` 在注册时被「字符串 → `ModelArch` 对象」物化，这样后续 LoRA 选 `target_modules` 时就不必再解析（详见 [u3-l2](u3-l2-model-arch-and-keys.md)）。

一个真实的注册样例见 [swift/model/models/qwen.py:76-114](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/models/qwen.py#L76-L114)，它把 Qwen 系列挂在 `LLMModelType.qwen` 上，指定了专用 `QwenLoader`、架构名 `QWenLMHeadModel`、模板 `TemplateType.qwen`、`model_arch=ModelArch.qwen`。

这些 model_type 字符串常量集中定义在 [swift/model/constant.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/constant.py)，并用多继承聚合成一个总表：

```python
class ModelType(LLMModelType, MLLMModelType, BertModelType, RMModelType):
    ...
```

见 [swift/model/constant.py:269](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/constant.py#L269)。`ModelType` 把纯文本（LLM）、多模态（MLLM）、BERT、奖励模型（RM）四类命名空间拼在一起，`get_model_list()` 等工具函数就是遍历它的成员来枚举所有已知模型。

#### 4.1.4 代码实践

**实践目标**：亲手「看见」`MODEL_MAPPING` 这张表被填充的过程。

**操作步骤**：

1. 在装好 ms-swift 的环境里执行一段最小 Python 脚本：

   ```python
   import swift.model  # 触发 models/*.py 的注册副作用
   from swift.model import MODEL_MAPPING
   print(len(MODEL_MAPPING))            # 已注册的 model_type 数量
   print('qwen3' in MODEL_MAPPING)      # True
   print(MODEL_MAPPING['qwen3'].template)
   ```

2. 在第 1 行 `import swift.model` 前后分别打印 `len(MODEL_MAPPING)`，观察导入前后表大小的变化。

**需要观察的现象**：不 import 时 `MODEL_MAPPING` 为空；import 后被几百条记录填满；`qwen3` 的 `template` 字段是一个非空字符串（如 `'qwen3'`）。

**预期结果**：你会直观看到「导入即注册」——这正是 `swift/model/__init__.py` 里 `from . import models` 这一行的意义。

> 本实践依赖本地已安装 ms-swift；若仅做源码阅读，可在 [swift/model/models/qwen.py:76](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/models/qwen.py#L76) 处对照阅读 `register_model` 调用。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果不执行任何 `import`，`MODEL_MAPPING` 里会有记录吗？为什么？

> **答案**：不会有。`MODEL_MAPPING` 初始化为空字典（[model_meta.py:122](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L122)），记录是各 `models/*.py` 在被 import 时通过 `register_model` 填进去的；没人 import 就没人填。

**练习 2**：`register_model` 默认拒绝重复注册同一个 model_type，这个设计是为了防止什么？

> **答案**：防止两个不同模型族意外占用同一个 model_type 字符串，导致后注册的覆盖前者、加载行为不可预期。它是开发期的「防呆」校验，需要覆盖时显式传 `exist_ok=True`。

---

### 4.2 ModelMeta / ModelInfo 元信息与模型匹配

#### 4.2.1 概念说明

光有 model_type 还不够，加载一个模型需要两类信息，它们被拆成两个数据类：

- **`ModelMeta`**：**静态档案**，写在源码里、注册进 `MODEL_MAPPING`。描述「这一族模型天生是什么样的」——用什么 loader、用什么 template、属于什么架构、是不是多模态/奖励模型。
- **`ModelInfo`**：**运行时档案**，加载时才生成。描述「这一次加载的具体实例」——从哪个 `model_dir` 读、用什么 `torch_dtype`、量化方式是什么、最大长度多少。

为什么要分两层？因为静态信息可以复用（所有 `qwen3` 模型都用 `qwen3` 模板），而运行时信息每次都变（你这次加载 bf16、下次加载 fp8 量化版）。把它们分开，`MODEL_MAPPING` 只存可复用的 `ModelMeta`，加载时再生成一次性的 `ModelInfo`。

#### 4.2.2 核心流程

「模型 id → meta + info」的匹配是本节重点，由 `get_model_info_meta` 编排：

```text
get_model_info_meta(model_id_or_path):
    model_meta = get_matched_model_meta(id)      # 用 id 的末段去 MODEL_MAPPING 里找
    model_dir  = safe_snapshot_download(id)      # 下载/解析到本地目录
    model_info = _get_model_info(model_dir, type)# 读 config.json 推断 dtype/quant/长度等
    # 兜底：若没匹配到 meta，非多模态则造一个 dummy ModelMeta
    推断 task_type / num_labels
    return model_info, model_meta
```

其中「怎么从一个 id 找到 model_type」有三道防线，优先级从高到低：

1. 调用方显式传 `model_type`。
2. 本地 `model_dir/args.json` 里记录的 `model_type`（ms-swift 训练时会落盘 args.json，所以重新加载微调产物时能自动认出）。
3. 读 `config.json` 的 `architectures` 字段，反查 `_get_arch_mapping()` 得到候选 model_type 列表；若唯一就用，若多个就报错要求手动指定。

#### 4.2.3 源码精读

`ModelMeta` 定义在 [swift/model/model_meta.py:56-95](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L56-L95)，关键字段：

```python
@dataclass
class ModelMeta:
    model_type: Optional[str]
    model_groups: List[ModelGroup]
    loader: Optional[Type[BaseModelLoader]] = None
    template: Optional[str] = None
    model_arch: Optional[str] = None
    architectures: List[str] = field(default_factory=list)
    additional_saved_files: List[str] = field(default_factory=list)
    torch_dtype: Optional[torch.dtype] = None
    is_multimodal: bool = False
    is_reward: bool = False
    task_type: Optional[str] = None
    ...
```

它的 `__post_init__`（[model_meta.py:82-95](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L82-L95)）做了三件重要的事：① `loader` 默认补成 `ModelLoader`；② 收集 `candidate_templates`（自身 template 加上各 `ModelGroup` 的 template）；③ 根据 model_type 是否出现在 `MLLMModelType`/`RMModelType` 命名空间里，自动设置 `is_multimodal`/`is_reward`。

`ModelGroup`（[model_meta.py:30-42](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L30-L42)）把一组同族模型 id 聚到一起，并可附带「组级覆盖」（如不同的 `template`、`tags`）。`Model`（[model_meta.py:20-27](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L20-L27)）则同时记录 `ms_model_id`（魔搭）和 `hf_model_id`（HuggingFace），让一份注册同时服务两个 hub。

「id 末段匹配」的核心是 `get_matched_model_meta`（[model_meta.py:162-171](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L162-L171)）：它取 id 的最后一段（如 `Qwen/Qwen2.5-7B-Instruct` → `qwen2.5-7b-instruct`，转小写），去每个 `ModelMeta` 的所有 model id 里找匹配，命中后返回一份**深拷贝**并把组级覆盖叠上去。深拷贝很关键——避免不同调用方改动同一份共享档案。

运行时信息由 `_get_model_info`（[model_meta.py:204-244](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L204-L244)）从 `config.json` 提取，产出 `ModelInfo`（[model_meta.py:125-143](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L125-L143)）。注意它的 model_type 推断走的就是前面说的「显式 > args.json > architectures」三道防线（见 [model_meta.py:218-231](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L218-L231)）。

兜底逻辑在 `get_model_info_meta`（[model_meta.py:280-287](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L280-L287)）：如果匹配不到 meta，多模态模型直接报错（多模态必须显式支持），纯文本模型则临时造一个 `template='dummy'` 的 `ModelMeta`，让加载继续——这是 ms-swift「对未知纯文本模型尽量兜底」的设计取向。

#### 4.2.4 代码实践

**实践目标**：观察同一个 id 在「匹配到」和「匹配不到」两种情况下的不同行为。

**操作步骤**：

1. 阅读并对比 [get_matched_model_meta](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L162-L171) 与 [get_matched_model_types](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L187-L193) 两个匹配函数，前者用「id 末段」，后者用「architectures」。
2. 写一段伪代码（不必运行）预测：传入 `model_id_or_path='Qwen/Qwen2.5-7B-Instruct'` 时，会走哪条匹配路径？传入一个完全自定义的本地路径 `/data/my-llama`（未注册）又会怎样？

**需要观察的现象**（源码阅读型）：

- `Qwen/Qwen2.5-7B-Instruct`：末段 `qwen2.5-7b-instruct` 能在某个 `ModelGroup` 里命中 → 返回真实 `ModelMeta`。
- `/data/my-llama`：末段匹配不到 → 转而读 `config.json` 的 `architectures`（如 `LlamaForCausalLM`）反查；若仍无法唯一确定，最终走 dummy 兜底。

**预期结果**：能口述出「id 末段 → architectures → dummy」三级回退的判定顺序。

> 若想实际运行，可在本地装好 ms-swift 后调用 `from swift.model import get_matched_model_meta; print(get_matched_model_meta('Qwen/Qwen2.5-7B-Instruct'))` 观察返回。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`ModelMeta` 和 `ModelInfo` 谁是静态的、谁是运行时的？为什么 `MODEL_MAPPING` 只存前者？

> **答案**：`ModelMeta` 是静态档案（写死在源码、可复用），`ModelInfo` 是运行时档案（每次加载依 dtype/量化/路径而变）。`MODEL_MAPPING` 作为全局注册表只应保存可复用的静态信息，运行时差异由 `ModelInfo` 在加载时单独承载，避免污染共享档案。

**练习 2**：一个未注册的本地纯文本模型，为什么 ms-swift 还能加载它？

> **答案**：因为 [model_meta.py:286](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L286) 会为匹配不到 meta 的纯文本模型临时创建一个 `template='dummy'` 的 `ModelMeta` 兜底；多模态模型因为模板/预处理复杂，必须显式支持，所以不兜底直接报错。

---

### 4.3 ModelLoader 的加载流程

#### 4.3.1 概念说明

`ModelMeta.loader` 指向一个加载器类，默认是 `ModelLoader`。它是一个有状态的「加载机器」：构造时接收 `model_info`/`model_meta` 和一堆加载选项，调用 `.load()` 后吐出 `(model, processor)`。基类契约定义在 `BaseModelLoader`（[model_meta.py:45-53](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L45-L53)），只要求实现 `__init__` 和 `load` 两个抽象方法。

为什么要把「加载」做成一个类而不是一个函数？因为加载过程步骤多（拿 config、拿 processor、拿 model、各种 patch、补 generation_config……），用类可以把这些步骤拆成可复写的方法（`get_config`/`get_processor`/`get_model`），子类只需覆盖其中一步就能定制某一族模型——这正是 [qwen.py:44](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/models/qwen.py#L44) 里 `QwenLoader` 只覆盖 `get_model`/`_update_attn_impl`/`get_processor` 三个方法就能定制 Qwen 加载的原因。

#### 4.3.2 核心流程

`ModelLoader.load()` 的主干（[register.py:470-482](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L470-L482)）是一个清晰的线性流水线：

```text
load():
    with 几个 patch 上下文:
        config   = get_config(model_dir)        # 读 config.json
        config   = _postprocess_config(config)  # 补 dtype/rope/max_len/attn_impl
        model, processor = _get_model_processor(model_dir, config)
            processor = get_processor(...)       # AutoTokenizer / AutoProcessor
            if load_model:
                model     = get_model(...)       # AutoModelForCausalLM.from_pretrained
        _postprocess_processor(processor)        # 补 pad/eos token，挂 model_info
        if model:
            _postprocess_model(model)            # 挂 model_info/meta，init generation_config
    _add_new_special_tokens(model, processor, config)
    return model, processor
```

要点：`load_model=False` 时只返回 processor、不加载权重——这正是 `get_processor` 顶层函数的实现原理（见 4.4）。

#### 4.3.3 源码精读

构造函数 [ModelLoader.__init__](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L163-L214) 把所有加载选项（`attn_impl`/`experts_impl`/`rope_scaling`/`max_model_len`/`auto_model_cls`…）存为成员，并根据 `quant_method` 决定 `torch_dtype`（fp8 量化时强制 `'auto'`）。它还要处理 transformers 4.x 与 5.x 的参数名差异（`torch_dtype` vs `dtype`）。

三步「拿东西」的方法：

- [get_config](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L247-L249)：`AutoConfig.from_pretrained(model_dir, trust_remote_code=True)`。
- [get_processor](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L259-L268)：优先用 `AutoProcessor`（当目录里有 `preprocessor_config.json`/`processor_config.json`，即多模态场景），否则退回 `AutoTokenizer`。
- [get_model](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L270-L331)：根据 `task_type` 选 Auto 类。`seq_cls`/`reranker` 走 `AutoModelForSequenceClassification`，其余默认 `AutoModelForCausalLM`，并在加载前后套上各种 `patch_automodel*` 上下文来修正 transformers 的行为。

加载后的收尾 `_postprocess_model`（[register.py:342-356](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L342-L356)）有一段非常关键：

```python
model.model_info = self.model_info
model.model_meta = self.model_meta
model.model_dir = model_dir
```

它把两份档案**挂回到 model 对象本身**上。这意味着加载完成后，你在任何地方拿到这个 model，都能直接 `model.model_meta.template` 读出它的模板名——下游的模板系统正是依赖这一点（见 4.4.3）。

此外，项目还内置了两个特殊 loader 子类：`SentenceTransformersLoader`（[register.py:485](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L485)）面向句向量 embedding，`RewardModelLoader`（[register.py:508](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L508)）面向奖励模型，它们都只覆盖 `get_model` 一步，体现了「基类管流水线、子类管定制点」的分工。

#### 4.3.4 代码实践

**实践目标**：理解 `QwenLoader` 是如何通过「最小覆盖」定制 Qwen 加载的。

**操作步骤**：

1. 打开 [swift/model/models/qwen.py:44-73](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/models/qwen.py#L44-L73)，阅读 `QwenLoader`。
2. 对照基类 [ModelLoader](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L160)，列出 `QwenLoader` 只覆盖了哪几个方法、各自定制了什么。
3. 回答：为什么 `QwenLoader` 不需要重新实现 `load()`？

**需要观察的现象**：`QwenLoader` 仅覆盖 `get_model`（修正老版 Qwen 的 dtype 标志位与 mp+ddp 的 mask bug）、`_update_attn_impl`（设 `use_flash_attn`）、`get_processor`（补 `eos_token_id=eod_id`），其余全部继承。

**预期结果**：你会得出结论——因为流水线骨架在基类 `load()` 里已经固定，子类只需覆盖「这一个模型族与众不同的那一步」，这就是把加载器做成可继承类的核心收益。

#### 4.3.5 小练习与答案

**练习 1**：`ModelLoader.load()` 在 `load_model=False` 时还会去读权重吗？

> **答案**：不会。`_get_model_processor`（[register.py:463-468](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L463-L468)）里只有 `if self.load_model:` 才调用 `get_model`；否则 model 为 None，只返回 processor。

**练习 2**：加载完成后，`model.model_meta` 是从哪里来的？为什么要把 meta 挂到 model 上？

> **答案**：来自 `_postprocess_model`（[register.py:351-352](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L351-L352)）的显式赋值。挂到 model 上是为了让下游（如模板系统、推理引擎）拿到 model 就能直接读到它的 model_type/template 等元信息，而不必再把 meta 单独传来传去。

---

### 4.4 get_model_processor 顶层入口与模板联动

#### 4.4.1 概念说明

`ModelLoader` 是内部机器，对外暴露的「一键加载」入口是 `get_model_processor`。用户/上层 pipeline 只需提供一个模型 id（或本地路径），它负责把 4.2 的「匹配」和 4.3 的「加载」串起来，返回 `(model, processor)`。还有一个更轻的 `get_processor`，只返回 processor 不加载权重。

本节还要回答规格里的核心问题：**`ModelMeta.template` 字段是如何与模板系统联动的？** 答案是——加载阶段并不调用模板，而是把 `model_meta` 挂到 model/processor 上；之后模板系统在需要时通过 `get_template_meta` 读取 `model_meta.template`，从而自动选出正确的对话模板。这是一种**延迟耦合**：模型加载与模板选择分开进行，靠 `model_meta` 这份共享档案衔接。

#### 4.4.2 核心流程

```text
get_model_processor(model_id_or_path, ...):
    if load_model: patch_mp_ddp()                       # 多卡相关 patch
    model_info, model_meta = get_model_info_meta(id)     # 4.2 的匹配
    device_map = device_map or get_default_device_map()
    loader = model_meta.loader(model_info, model_meta, ...)  # 4.3 的机器
    return loader.load()                                 # -> (model, processor)

# 之后，模板系统侧：
get_template_meta(model_info, model_meta, template_type=None):
    template_type = template_type or model_meta.template # ← 联动点！
    return TEMPLATE_MAPPING[template_type]
```

#### 4.4.3 源码精读

顶层入口 [get_model_processor](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L516-L630) 的核心几行：

```python
model_info, model_meta = get_model_info_meta(model_id_or_path, ...)
...
loader = model_meta.loader(
    model_info, model_meta, load_model=load_model, ...,
    model_kwargs=model_kwargs, **kwargs)
return loader.load()
```

注意 `model_meta.loader`——它就是 `ModelMeta.loader` 字段（默认 `ModelLoader`，可被 `QwenLoader` 等子类替换）。这一行实现了「按模型族选用不同加载器」的多态分发。函数签名上还暴露了 `attn_impl`、`experts_impl`、`rope_scaling`、`max_model_len`、`task_type`、`quantization_config` 等丰富选项，覆盖了日常训练/推理所需的全部加载控制。

轻量入口 [get_processor](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L633-L664) 就是对 `get_model_processor` 的薄封装，固定 `load_model=False` 并返回元组第二项：

```python
return get_model_processor(..., load_model=False, **kwargs)[1]
```

模板联动的关键一行在 [swift/template/register.py:36](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/register.py#L36)：

```python
template_type = template_type or model_meta.template
```

它位于 `get_template_meta`（[template/register.py:31-52](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/register.py#L31-L52)）里，逻辑是：如果调用方没显式传 `template_type`，就读 `model_meta.template`；若该字段也是 None，再用 `candidate_templates` 兜底；最终用选定的 `template_type` 去 `TEMPLATE_MAPPING` 取出 `TemplateMeta`。

至此整条联动链路清晰了：

\[
\texttt{register\_model}(\text{template}=T)
\;\longrightarrow\;
\texttt{MODEL\_MAPPING}[\text{type}].\text{template}=T
\;\longrightarrow\;
\texttt{model.model\_meta.template}=T
\;\longrightarrow\;
\texttt{get\_template\_meta}\,\Rightarrow\,\texttt{TEMPLATE\_MAPPING}[T]
\]

也就是：注册时写下的 `template` 字段，经由 `model_meta` 这份档案，最终决定了训练/推理时用哪套对话格式——这就是「模型加载」与「模板系统」的衔接点。

#### 4.4.4 代码实践

**实践目标**：用 `get_model_processor` 加载一个 Qwen 模型，打印两份档案，并验证 `model_meta.template` 与模板系统的联动。

**操作步骤**：

1. 确保已按 [u1-l2](u1-l2-installation-and-dependencies.md) 装好 ms-swift，并在能联网的环境执行：

   ```python
   from swift.model import get_model_processor

   # load_model=False 只拿 processor，不下载/加载权重，适合快速验证
   _, processor = get_model_processor('Qwen/Qwen2.5-7B-Instruct', load_model=False)
   print('--- model_info ---')
   print(processor.model_info)
   print('--- model_meta ---')
   print(processor.model_meta)
   print('--- template field ---')
   print(processor.model_meta.template)
   ```

2. 接着验证联动——用同一个 `model_meta` 让模板系统自动选出模板：

   ```python
   from swift.template import get_template_meta
   tmeta = get_template_meta(processor.model_info, processor.model_meta)
   print('matched template_type:', tmeta.template_type)
   ```

**需要观察的现象**：

- `processor.model_info` 包含 `model_dir`/`torch_dtype`/`max_model_len` 等运行时字段。
- `processor.model_meta.template` 是一个非空字符串（Qwen 对应的模板名）。
- `get_template_meta` 在不传 `template_type` 的情况下，返回的 `template_type` 恰好等于上一步的 `model_meta.template`——证明联动成立。

**预期结果**：三处的 template 值一致，说明「注册时写死的 template → model_meta → 模板系统」这条链路是通的。

> 本实践需要联网下载 config/processor 文件（`load_model=False` 时不会下载权重，体积很小）。若本地无法联网，可改为纯源码阅读：在 [get_template_meta](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/register.py#L31-L52) 第 36 行确认 `template_type = template_type or model_meta.template`，并在 [qwen.py:113](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/models/qwen.py#L113) 确认 Qwen 注册时确实写了 `template=TemplateType.qwen`。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`get_model_processor` 是如何决定用哪个 loader 类的？

> **答案**：通过 `model_meta.loader`（[register.py:617](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L617)）。匹配得到的 `ModelMeta` 自带 loader 字段（注册时指定，如 `QwenLoader`；未指定则在 `ModelMeta.__post_init__` 里默认成 `ModelLoader`），顶层入口直接 `model_meta.loader(...)` 实例化它。

**练习 2**：如果用户既不传 `--template`，模型注册时也没写 `template` 字段，会发生什么？

> **答案**：`get_template_meta` 会落到 `candidate_templates` 兜底（[template/register.py:38-48](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/register.py#L38-L48)）：候选为空或多个时报错并提示用 `--template` 手动指定，恰好一个时自动采用。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成一次「手动重现加载链路」的源码追踪任务：

**任务**：给定模型 id `Qwen/Qwen2.5-7B-Instruct`，画出从「id 字符串」到「`(model, processor)` + 选定模板」的完整调用图，并标注每一步落在哪个文件的哪一行。

**建议步骤**：

1. 从顶层入口 [get_model_processor](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L516-L630) 出发。
2. 进入 [get_model_info_meta](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L247-L325)，分别画出「匹配 meta」和「生成 info」两条支线，标出三道 model_type 推断防线。
3. 回到顶层，进入 `model_meta.loader(...)` 实例化与 [ModelLoader.load](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L470-L482)，画出 `get_config → get_processor → get_model → _postprocess_*` 流水线，并标出「把 model_meta 挂回 model」的那一行。
4. 最后接上模板侧的 [get_template_meta](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/register.py#L31-L52)，标出 `template_type = template_type or model_meta.template` 这个联动点。
5. 在图上用不同颜色区分「静态档案 ModelMeta」「运行时档案 ModelInfo」「注册表 MODEL_MAPPING/TEMPLATE_MAPPING」三类对象的生命周期。

**验收标准**：你能指着图上的每个节点说出对应的 `文件:行号`，并能解释「为什么 `model.model_meta` 这个属性是模型子系统和模板子系统之间的桥梁」。

> 提示：这张图里没有任何数学公式，但它的拓扑结构本身就是一种「数据流」——对象沿管线流动，每一步要么读注册表、要么生成运行时信息、要么挂载属性。把它画清楚，本讲就真正消化了。

## 6. 本讲小结

- ms-swift 用一张全局字典 `MODEL_MAPPING`（[model_meta.py:122](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L122)）把 model_type 映射到静态档案 `ModelMeta`，靠各 `models/*.py` 在导入时调用 `register_model` 填充。
- 元信息分两层：`ModelMeta` 是可复用的静态档案（loader/template/model_arch…），`ModelInfo` 是一次性的运行时档案（model_dir/dtype/quant…），两者共同喂给加载器。
- 「id → model_type」走「显式传入 > args.json > architectures 反查」三道防线，匹配不到时纯文本模型走 dummy 兜底、多模态模型直接报错。
- 加载由 `ModelLoader.load()` 编排成线性流水线（config → processor → model → 收尾），子类（如 `QwenLoader`）只覆盖少数方法即可定制；加载后 `model_meta` 会被挂回 model/processor。
- 顶层入口 `get_model_processor` 串起「匹配 + 加载」，`get_processor` 是其 `load_model=False` 的薄封装。
- `ModelMeta.template` 经 `model.model_meta` 流转，被模板系统的 `get_template_meta` 读取，实现「模型加载」与「对话模板」的延迟耦合。

## 7. 下一步学习建议

- 想深入「`model_arch` 如何决定 LoRA 的 `target_modules`」：继续本单元的 [u3-l2 模型架构 ModelArch 与 ModelKeys](u3-l2-model-arch-and-keys.md)。
- 想了解模板那侧的 `encode`/对话格式化：跳到 [u3-l3 Template 体系与对话格式](u3-l3-template-and-chat-format.md)，本讲 4.4 已为它铺好了「model_meta.template 联动」的前置。
- 想看模型加载在训练主流程里处于什么位置：预习 [u5-l4 SFT 训练主流程 SwiftSft](u5-l4-sft-main-pipeline.md)，注意 `get_model_processor` 在准备模型阶段被调用。
- 想自己注册一个新模型：直接读 [swift/model/models/qwen.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/models/qwen.py) 作为模板，参考 [u10-l3 自定义模型、模板与 Agent 注册](u10-l3-custom-model-template-agent.md)。
