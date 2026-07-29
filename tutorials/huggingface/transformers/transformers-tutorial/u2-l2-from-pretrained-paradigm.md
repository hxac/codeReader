# from_pretrained / save_pretrained 统一范式

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「四大预训练对象」（config、model、tokenizer、processor）为什么共享同一套 `from_pretrained` / `save_pretrained` 接口，以及这套接口背后统一的「定位文件 → 下载/缓存 → 反序列化 → 构造对象」骨架。
- 读懂 [`PretrainedConfig`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/configuration_utils.py#L146) 的 `config.json` 加载/保存链路（`get_config_dict` → `cached_file` → `from_dict`，保存时只写「与默认值的差异」）。
- 读懂 [`PreTrainedModel.from_pretrained`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/modeling_utils.py#L3868) 是如何「先加载 config、再加载权重、再把权重灌进模型」的，并理解 `revision`、`dtype`、`token` 等关键参数的流动。
- 读懂 [`PreTrainedTokenizerBase.from_pretrained`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1498) 如何把一整套词表文件 + `tokenizer_config.json` 复原成一个 tokenizer 对象。
- 能够动手完成「加载模型 → 改 config → `save_pretrained` → 重新加载验证」这一最常见的工作流。

## 2. 前置知识

本讲是 u2-l1（Auto 类）的延续。在 u2-l1 里我们已经知道：Auto 类以 `config.json` 里的 `model_type` 为桥梁，查映射表选出**具体类**（例如 `LlamaForCausalLM`）。选出了类之后，真正的「把模型从硬盘/Hub 拿到内存」这一步，就交给本讲的主角——每个具体类都继承自同一组基类的 `from_pretrained`。

理解本讲需要先建立两个直觉：

1. **「预训练对象（pretrained object）」是一个统一抽象。** 在 transformers 里，一个 checkpoint 在磁盘上不是「一个大文件」，而是**一个目录**，里面散落着好几类文件：

   | 文件类型 | 典型文件名 | 由谁负责加载 | 对应对象 |
   |---|---|---|---|
   | 模型结构配置 | `config.json` | `PretrainedConfig` | config |
   | 模型权重 | `model.safetensors`（可能分片） | `PreTrainedModel` | model |
   | 分词器配置 | `tokenizer_config.json` | `PreTrainedTokenizerBase` | tokenizer |
   | 分词器词表 | `tokenizer.json` / `vocab.json` / `tokenizer.model` 等 | `PreTrainedTokenizerBase` | tokenizer |
   | 预处理器配置 | `preprocessor_config.json` | `ProcessorMixin` / `BaseImageProcessor` | processor |

   这些文件名常量都集中定义在 [`utils/__init__.py`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/__init__.py#L278) 里，例如 `CONFIG_NAME = "config.json"`、`SAFE_WEIGHTS_NAME = "model.safetensors"`。

2. **「同一套骨架，各自填空」。** 四类对象的加载流程高度同构：都是「把一个字符串（model id 或目录路径）解析成本地某个文件的路径 → 读文件 → 用读到的内容构造对象」。它们的差异只在于「读什么文件、怎么反序列化」。抓住这个同构性，本讲就不再是一堆零散的 API，而是「一个范式 × 四种实例」。

> 一个 checkpoint 目录长什么样？等你做完本讲的实践，用 `ls` 看一眼 `save_pretrained` 产生的目录，这张表就立刻具象化了。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|---|---|
| [`src/transformers/configuration_utils.py`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/configuration_utils.py) | 定义 `PretrainedConfig` 基类，含 `config.json` 的加载/保存全链路。 |
| [`src/transformers/modeling_utils.py`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/modeling_utils.py) | 定义 `PreTrainedModel` 基类，含权重加载的 `from_pretrained` 主流程与分片权重解析。 |
| [`src/transformers/tokenization_utils_base.py`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py) | 定义 `PreTrainedTokenizerBase` 基类，含分词器多文件加载与 `tokenizer_config.json` 还原。 |
| [`src/transformers/utils/hub.py`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/hub.py) | 提供 `cached_file`（统一文件下载/缓存）与 `PushToHubMixin`（统一上传）——本讲范式的「共享底盘」。 |

## 4. 核心概念与源码讲解

### 4.1 统一范式的全貌：四大对象与共享底盘 PushToHubMixin

#### 4.1.1 概念说明

在 u1-l1 里我们提过，transformers 把每个模型拆成 Configuration / Model / Preprocessing 三类对象，它们**共享 `from_pretrained` / `save_pretrained` 接口**。本节要回答：这种「共享」在源码层面是如何落地的？

答案是两个层次：

- **接口层面（鸭子类型）**：四类对象的基类都定义了同名的 `from_pretrained` / `save_pretrained`，签名形态一致（第一个位置参数都是 `pretrained_model_name_or_path`，都接受 `cache_dir`、`force_download`、`local_files_only`、`token`、`revision` 等「下载相关参数」）。用户因此可以无差别地写 `AutoXxx.from_pretrained(...)`。
- **实现层面（继承复用）**：四类对象都混入（mixin）了 [`PushToHubMixin`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/hub.py#L641)，它提供 `push_to_hub` 与上传辅助方法 `_upload_modified_files`；而「下载/定位文件」这一最公共的能力，则被抽取成一个**自由函数** [`cached_file`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/hub.py#L238)，由四类对象的加载逻辑各自调用。

换句话说，`PushToHubMixin` 是「写入（上传）」侧的统一底盘，`cached_file` 是「读取（下载/缓存）」侧的统一底盘。

#### 4.1.2 核心流程

四类对象的加载都遵循同一条骨架：

```
from_pretrained(pretrained_model_name_or_path, *, revision, token, ...)
        │
        ▼
① 判断输入类型：是 本地目录 / 本地单文件 / Hub model id？
        │
        ▼
② 对「需要加载的每个文件」，调用 cached_file(name, filename, revision=..., local_files_only=...)
   └─ 命中本地缓存就直接返回路径；否则从 Hub 下载并缓存
        │
        ▼
③ 反序列化文件内容（config 读 JSON、model 读 safetensors、tokenizer 读词表）
        │
        ▼
④ 用读到的内容构造对象：cls(...)
        │
        ▼
⑤ 返回对象（并记录 _commit_hash，供后续「版本一致性」校验）
```

而 `save_pretrained` 是其逆过程：把对象的可序列化部分写回 `save_directory`，文件名沿用上面那张表里的常量名，从而保证「存得回来」。

#### 4.1.3 源码精读

先看共享的「读取底盘」`cached_file` 的签名（它把 model id / 目录、文件名、版本、缓存等参数都收口在一处）：

[cached_file 的签名与文档](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/hub.py#L238-L242) —— 它「先在本地目录找文件，找不到就下载并缓存」，返回最终落地的本地文件路径。本讲后面 config、model、tokenizer 三条加载链都会回到这一个函数。

再看共享的「写入底盘」`PushToHubMixin`：

[PushToHubMixin 类定义](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/hub.py#L641-L644) —— 注意它是一个普通 mixin，`PretrainedConfig`、`PreTrainedModel`、`PreTrainedTokenizerBase` 都把它列在基类里，因此三者都获得了 `push_to_hub` 能力。

[`_upload_modified_files` 的提交信息推断](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/hub.py#L666-L678) —— 它根据类名里是否含 `Model`/`Config`/`Tokenizer` 自动生成默认 commit message（如 `Upload model`），这正是「四类对象共用一套上传逻辑」的体现。

最后看一眼这四个基类各自把 `PushToHubMixin` 放在第几位：

- [`class PretrainedConfig(PushToHubMixin, ...)`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/configuration_utils.py#L146)
- [`class PreTrainedModel(nn.Module, ..., PushToHubMixin, ...)`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/modeling_utils.py#L1181-L1183)
- [`class PreTrainedTokenizerBase(PushToHubMixin)`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L972)

#### 4.1.4 代码实践

**实践目标**：直观感受「同一套下载/版本底盘」——证明 config 与 model 在加载同一个 checkpoint 时命中**同一个 commit**。

**操作步骤**（示例代码，需联网）：

```python
# 示例代码
from transformers import AutoConfig, AutoModelForCausalLM

ckpt = "hf-internal-testing/tiny-random-LlamaForCausalLM"  # 体积很小的测试模型
cfg = AutoConfig.from_pretrained(ckpt)
mdl = AutoModelForCausalLM.from_pretrained(ckpt)

print("config commit:", cfg._commit_hash)
print("model config commit:", mdl.config._commit_hash)
```

**需要观察的现象**：两次打印的 `_commit_hash` 一致。这说明 config 和 model 走的是同一条「按 revision 解析文件 → 记录 commit hash」的链路。

**预期结果**：两个 hash 字符串相等（若该 checkpoint 没有 commit hash，则二者同为 `None`，仍一致）。

> 若本地无网络，把 `ckpt` 换成你已经缓存过的任意本地目录，或设 `HF_HUB_OFFLINE=1` 走纯缓存即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 transformers 把「下载/缓存」做成自由函数 `cached_file`，而不是放进某个基类的方法里？
**参考答案**：因为 config、model、tokenizer、processor 四类对象都要复用同一段「定位 + 下载 + 缓存」逻辑，而它们并不共享同一个基类（config 继承自 dataclass 风格的基类、model 继承自 `nn.Module`、tokenizer 继承自 `object` 体系）。做成自由函数可以避免「多重继承凑公共基类」的尴尬，任何加载链都能直接调用。

**练习 2**：`save_pretrained` 的 `push_to_hub=True` 选项背后，实际干活的是哪段共享代码？
**参考答案**：是 `PushToHubMixin._upload_modified_files`，它比较保存前后的文件时间戳，只上传被改动/新增的文件。

---

### 4.2 PretrainedConfig：config.json 的加载与保存

#### 4.2.1 概念说明

`PretrainedConfig` 是「描述模型结构的一组超参数」的容器——层数、隐藏维度、词表大小、激活函数、是否 tie embedding……全都存成它的属性。它序列化出来就是 checkpoint 目录里的 `config.json`。

理解 config 的加载/保存，关键有三点：

1. config **只描述结构、不包含权重**。官方文档明确提示：「加载 config 并据此初始化模型，**不会**加载权重，只影响模型结构」。
2. config 是一个 **dataclass**（注意类装饰器 `@dataclass` 与 `__post_init__`），它的属性既能在构造时传入，也能从 JSON 反序列化。
3. 保存时默认只写「与默认值的差异」（`use_diff=True`），所以你看到的 `config.json` 通常很短、可读性高。

#### 4.2.2 核心流程

`from_pretrained` 的内部其实是个「三层套娃」：

```
PretrainedConfig.from_pretrained(name_or_path, **kwargs)
        │  调用
        ▼
get_config_dict(name_or_path, **kwargs)          # 解析出「原始字典」config_dict
        │  调用
        ▼
_get_config_dict(...)                            # 真正定位 + 读 JSON
        │  内部调用
        ▼
cached_file(..., CONFIG_NAME="config.json")      # 下载/缓存 config.json
        │  读到字符串后
        ▼
_dict_from_json_file(...)  →  config_dict        # JSON 字符串 → Python dict
        │  回到 from_pretrained
        ▼
from_dict(config_dict, **kwargs)                 # 用 dict + 用户 kwargs 构造 config 对象
        │
        ▼
return config                                    # 最终对象
```

保存则是反过来的精简版：`save_pretrained` → `to_json_file(use_diff=True)` → `to_diff_dict()` → `to_dict()`，把对象写成 `config.json`。

#### 4.2.3 源码精读

**加载入口** [`PretrainedConfig.from_pretrained`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/configuration_utils.py#L615-L724)。它的关键只有三步：先把 `cache_dir/force_download/local_files_only/revision` 塞进 kwargs，再调 `get_config_dict` 拿到字典，最后用 `from_dict` 构造对象：

```python
# configuration_utils.py（节选关键行）
config_dict, kwargs = cls.get_config_dict(pretrained_model_name_or_path, **kwargs)
if cls.base_config_key and cls.base_config_key in config_dict:
    config_dict = config_dict[cls.base_config_key]
# ... 校验 model_type 是否匹配 ...
return cls.from_dict(config_dict, **kwargs)
```

注意最后那句 `cls.from_dict`：用户的 `**kwargs` 会在这里**覆盖**从文件读到的值（例如 `BertConfig.from_pretrained(name, output_attentions=True)`），这是「在加载时临时改超参」的官方入口。

**真正读文件的环节** [`_get_config_dict`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/configuration_utils.py#L759-L857)。它先判断是不是本地文件/目录，否则调 `cached_file` 拉取 `config.json`：

```python
# configuration_utils.py（节选）
resolved_config_file = cached_file(
    pretrained_model_name_or_path,
    configuration_file,            # 默认就是 "config.json"
    cache_dir=cache_dir, force_download=force_download,
    local_files_only=local_files_only, token=token,
    revision=revision, subfolder=subfolder, _commit_hash=commit_hash,
)
config_dict = cls._dict_from_json_file(resolved_config_file)
config_dict["_commit_hash"] = commit_hash   # 把版本指纹带回字典
```

这里有两个细节值得记住：
- `configuration_file` 默认是 `CONFIG_NAME`（即 `config.json`），但若是 GGUF 则换成 `gguf_file`（[`configuration_utils.py:795`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/configuration_utils.py#L795)）。
- 读到的 `config_dict` 会注入 `_commit_hash`，这就是 4.1.4 里我们看到 `cfg._commit_hash` 的来源。

**从字典构造对象** [`from_dict`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/configuration_utils.py#L860-L922)。它先用 `cls(**config_dict)` 走 dataclass 构造（触发 `__post_init__`），再把 kwargs 里「属于 config 属性」的键 `setattr` 覆盖上去：

```python
# configuration_utils.py（节选）
config = cls(**config_dict)
for key, value in kwargs.items():
    if hasattr(config, key):
        setattr(config, key, value)   # 用户传入的 kwargs 覆盖文件值
        to_remove.append(key)
```

**保存：只写「差异」** [`save_pretrained`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/configuration_utils.py#L553-L604)。它最终调用 `to_json_file(output_config_file, use_diff=True)`。`use_diff=True` 会走 [`to_diff_dict`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/configuration_utils.py#L1008-L1071)：拿「当前对象的 dict」和「一个全新默认实例的 dict」逐键比较，只保留不同的键，并强制保留 `model_type`、`transformers_version` 等。这就是 `config.json` 通常很短的原因。

#### 4.2.4 代码实践

**实践目标**：亲眼看到 `save_pretrained` 只保存「与默认值的差异」。

**操作步骤**（示例代码，本地可跑）：

```python
# 示例代码
from transformers import AutoConfig

cfg = AutoConfig.from_pretrained("hf-internal-testing/tiny-random-LlamaForCausalLM")
cfg.torchscript = True  # 故意改一个非默认属性
cfg.save_pretrained("/tmp/cfg_demo")
```

然后打开 `/tmp/cfg_demo/config.json`。

**需要观察的现象**：文件里通常只有十几行，且包含 `torchscript: true`，但**不会**出现「与 Llama 默认值相同的那些字段」（例如默认的 `hidden_act` 往往被省略）。

**预期结果**：`config.json` 内容远小于「完整 dict」，且能看到你修改过的 `torchscript`。待本地验证：不同模型省略的字段集合不同（取决于各自默认值）。

#### 4.2.5 小练习与答案

**练习 1**：调用 `BertConfig.from_pretrained("xxx", vocab_size=99999)` 会发生什么？文件里的 `vocab_size` 生效吗？
**参考答案**：会生效的是 `99999`。因为 `from_dict` 在用文件字典构造完对象后，会用 kwargs 里「属于 config 属性」的键覆盖——`vocab_size` 是 config 属性，所以 kwargs 覆盖文件值。

**练习 2**：为什么 `config.json` 里很少看到 `_name_or_path`？
**参考答案**：因为 `to_diff_dict` 在末尾显式 `del serializable_config_dict["_name_or_path"]`（[`configuration_utils.py:1057-1058`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/configuration_utils.py#L1057-L1058)），它被视为「加载环境信息」而非「结构信息」，不序列化。

**练习 3**：`get_config_dict` 里有一段「`configuration_files` 指向另一个文件」的逻辑，它的作用是什么？
**参考答案**：有些 checkpoint 根目录的索引文件会通过 `configuration_files` 字段指向真正要用的配置文件（例如按精度/变体选择），`get_config_dict` 会据此再做一次 `_get_config_dict`，加载那个被指向的文件（[`configuration_utils.py:751-755`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/configuration_utils.py#L751-L755)）。

---

### 4.3 PreTrainedModel.from_pretrained：配置与权重的协同加载

#### 4.3.1 概念说明

模型加载比 config 复杂，因为它要把「结构（config）」和「数值（权重）」两部分都拿到，并保证二者匹配。`PreTrainedModel.from_pretrained` 就是这个「总指挥」。

它的难点在于：模型权重通常很大，所以加载链要处理「分片（sharded）权重」「dtype 选择」「device 分配」「量化」等一系列工程问题。本讲聚焦**主流程骨架**，把这些高级选项的细节留到后续讲义（量化在 u10-l1、device_map/分布式在 u10-l3、dtype 细节在 u5-l2）。

#### 4.3.2 核心流程

把 `from_pretrained`（约 500 行）浓缩成 7 步：

```
PreTrainedModel.from_pretrained(name_or_path, config=None, dtype=None, **kwargs)
  ① 解析参数：dtype/torch_dtype(向后兼容)、device_map、quantization_config、
     trust_remote_code、subfolder、variant …… 并构造 download_kwargs
  ② 加载 config：若没传 config，则 config_class.from_pretrained(...)
     （复用 4.2 的链路！返回 config + 未用 kwargs）
  ③ 解析量化器：get_hf_quantizer(config, quantization_config, ...)
  ④ 解析权重文件：_get_resolved_checkpoint_files(...)
     └─ 优先 model.safetensors；分片则读 model.safetensors.index.json
  ⑤ 决定 dtype：_get_dtype(dtype, ...) —— 默认 "auto"，看 config 或首个权重
  ⑥ 在「初始化上下文」里构造空模型：with model_init_context: model = cls(config, ...)
  ⑦ 把权重灌进模型：_load_pretrained_model(model, state_dict, checkpoint_files, load_config)
     → model.eval() → （若 device_map 多设备）accelerate_dispatch(...) → return model
```

最关键的认知是 **② 复用了 config 的加载**：模型的 `from_pretrained` 自己不去解析 `config.json`，而是委托给 `cls.config_class.from_pretrained`（每个模型类都用 `config_class` 指明自己的配置类，例如 `LlamaModel.config_class is LlamaConfig`，见 [`modeling_utils.py:1204`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/modeling_utils.py#L1204)）。这就是「config 与权重被分别加载」在源码上的落点。

#### 4.3.3 源码精读

**参数解析与 download_kwargs 构造** [`from_pretrained 的开头`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/modeling_utils.py#L4111-L4161)。注意两件事：
- `torch_dtype` 被保留仅作向后兼容，最终统一到 `dtype`（`if dtype is None: dtype = "auto"`，[L4146-4147](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/modeling_utils.py#L4146-L4147)）。
- `revision`、`token`、`subfolder`、`local_files_only` 等被收进 `download_kwargs`，之后所有下载都复用它。

**② 加载 config（委托给 config_class）** [`modeling_utils.py:4197-4220`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/modeling_utils.py#L4197-L4220)：

```python
if not isinstance(config, PreTrainedConfig):
    config_path = config if config is not None else pretrained_model_name_or_path
    config_class = cls.config_class
    config, model_kwargs = config_class.from_pretrained(
        config_path, return_unused_kwargs=True, gguf_file=gguf_file,
        _from_auto=from_auto_class, _from_pipeline=from_pipeline,
        **download_kwargs, **kwargs,
    )
    commit_hash = model_kwargs.pop("_commit_hash", commit_hash)
```

注意它请求了 `return_unused_kwargs=True`：config 用不掉的 kwargs（比如 `attn_implementation`、`dtype` 之外的）会留在 `model_kwargs` 里，稍后原样传给模型构造函数。

**③ 量化器与 ④ 权重文件解析** [`get_hf_quantizer`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/modeling_utils.py#L4235-L4237) 与 [`_get_resolved_checkpoint_files`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/modeling_utils.py#L4258-L4268)。后者是「找权重文件」的核心，它有一套明确的优先级（[`_get_resolved_checkpoint_files` 本体](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/modeling_utils.py#L535-L628)）：

```python
# modeling_utils.py（节选优先级）
elif use_safetensors is not False and os.path.isfile(... SAFE_WEIGHTS_NAME ...):
    # 单文件 safetensors：model.safetensors
elif use_safetensors is not False and os.path.isfile(... SAFE_WEIGHTS_INDEX_NAME ...):
    # 分片 safetensors：model.safetensors.index.json，is_sharded=True
elif not use_safetensors and os.path.isfile(... WEIGHTS_NAME ...):
    # 旧式 pytorch_model.bin
```

也就是说：**默认优先 safetensors**（`use_safetensors is not False`），分片时通过 index.json 找到所有分片。

**⑤ dtype 决策** [`_get_dtype`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/modeling_utils.py#L4273-L4275)。`dtype="auto"` 时会依次尝试：config 里写明的 `dtype` → 否则看 checkpoint 里第一个浮点权重的真实 dtype。

**⑥ 构造空模型 + ⑦ 灌权重** [`modeling_utils.py:4314-4359`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/modeling_utils.py#L4314-L4359)：

```python
# modeling_utils.py（节选）
model_init_context = cls.get_init_context(dtype, is_quantized, _is_ds_init_called, allow_all_kernels)
with ContextManagers(model_init_context):
    model = cls(config, *model_args, **model_kwargs)   # ⑥ 在「按需初始化」上下文里建空模型
    if hf_quantizer is not None:
        hf_quantizer.preprocess_model(model=model, ...) # 用量化层替换普通层

# 打包所有加载选项
load_config = LoadStateDictConfig(pretrained_model_name_or_path=..., dtype=dtype, ...)
loading_info, disk_offload_index = cls._load_pretrained_model(model, state_dict, checkpoint_files, load_config)  # ⑦
model.eval()
```

真正「读权重 + 对齐键名 + 写入参数」的活儿在静态方法 [`_load_pretrained_model`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/modeling_utils.py#L4400-L4408) 里：它先取 `model.state_dict().keys()` 作为「期望键」，再逐个分片读盘、按 device_map 分发、报告 `missing_keys/unexpected_keys/mismatched_keys`。这些诊断信息就是你在加载时经常看到的那几行 warning 的来源。

**保存** [`save_pretrained`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/modeling_utils.py#L3288-L3352)：默认 `max_shard_size="50GB"`，会按大小自动分片，并写出 `model.safetensors.index.json` 索引（见 [`modeling_utils.py:3608`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/modeling_utils.py#L3608)）；同时调用 `config.save_pretrained` 把 config 也一并写进同一目录——这就是为什么「模型存盘 = 一个完整目录」。

#### 4.3.4 代码实践

**实践目标**：验证「② 委托 config 加载」和「⑤ dtype 决策」两件事。

**操作步骤**（示例代码）：

```python
# 示例代码
from transformers import AutoModelForCausalLM

ckpt = "hf-internal-testing/tiny-random-LlamaForCausalLM"

# (a) 不指定 dtype，默认 "auto"
m_auto = AutoModelForCausalLM.from_pretrained(ckpt)
print("auto dtype:", next(m_auto.parameters()).dtype)

# (b) 指定 bfloat16
m_bf16 = AutoModelForCausalLM.from_pretrained(ckpt, dtype="bfloat16")
print("bf16 dtype:", next(m_bf16.parameters()).dtype)

# (c) 验证 config 是被独立加载的（委托给 config_class）
print("config_class:", type(m_auto).config_class.__name__)
print("config.model_type:", m_auto.config.model_type)
```

**需要观察的现象**：
- (a)(b) 的 `dtype` 分别应为 `float32`（取决于 checkpoint 实际精度）与 `bfloat16`。
- (c) 打印出 `LlamaConfig` 和 `llama`，证明模型类确实通过 `config_class` 找到了对应配置类。

**预期结果**：dtype 受 `dtype=` 参数控制；`config_class` 与模型一一对应。待本地验证：(a) 的具体 dtype 取决于该测试 checkpoint 保存时的精度。

#### 4.3.5 小练习与答案

**练习 1**：为什么模型的 `from_pretrained` 要先 `model.eval()` 再返回？
**参考答案**：因为加载完的模型默认是拿来推理的，`eval()` 会关闭 Dropout 等训练期行为（[`modeling_utils.py:4360`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/modeling_utils.py#L4360) 文档里也写明默认 evaluation mode）。要训练需手动 `model.train()`。

**练习 2**：`from_pretrained` 既支持传 `config=...` 对象，也支持让它自动加载，这两种情况下 config 是怎么来的？
**参考答案**：若传入的 `config` 已是 `PreTrainedConfig` 实例，则直接 `copy.deepcopy` 复用（[L4217-4220](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/modeling_utils.py#L4217-L4220)）；否则把 `config`（或 `pretrained_model_name_or_path`）当路径，委托给 `config_class.from_pretrained` 重新加载（L4198-4213）。

**练习 3**：一个 100GB 的模型，`save_pretrained` 会存成几个文件？
**参考答案**：默认 `max_shard_size="50GB"`，所以会分成约 2 个 `model-0000X-of-0000N.safetensors` 分片，外加一个 `model.safetensors.index.json` 索引文件，以及 `config.json`。可通过 `max_shard_size` 调整分片大小。

---

### 4.4 PreTrainedTokenizerBase.from_pretrained：词表与配置的加载

#### 4.4.1 概念说明

分词器没有「一个大权重张量」，取而代之的是**一整套文件**：词表（`vocab.json`/`tokenizer.model`/`vocab.txt`…）、合并表（`merges.txt`）、统一格式（`tokenizer.json`）、配置（`tokenizer_config.json`）、聊天模板（`chat_template.jinja`）等。具体需要哪些文件，由每个 tokenizer 子类的 `vocab_files_names` 类属性声明。

因此 tokenizer 的加载特点在于：**多文件并行解析**，且最终用一个「合并后的 kwargs 字典」去 `cls(**kwargs)` 实例化。

#### 4.4.2 核心流程

```
PreTrainedTokenizerBase.from_pretrained(name_or_path, trust_remote_code=False, **kwargs)
  ① 组装 vocab_files 字典 = {**cls.vocab_files_names, **additional_files_names}
     └─ additional: tokenizer_config.json / tokenizer.json / chat_template...
  ② 对 vocab_files 里每个文件，调用 cached_file(...) 解析到本地路径
     └─ resolved_vocab_files = {file_id: 本地路径 or None}
  ③ 进入 _from_pretrained(resolved_vocab_files, ...):
     a. 读 tokenizer_config.json → init_kwargs（这是「还原构造参数」的关键）
     b. 读聊天模板文件（若有）→ 覆盖 init_kwargs["chat_template"]
     c. init_kwargs.update(kwargs)  ← 用户传入的 kwargs 再次覆盖
     d. tokenizer = cls(*init_inputs, **init_kwargs)   ← 真正构造
  ④ 返回 tokenizer
```

注意 `tokenizer_config.json` 的角色：它存的是「当初构造这个 tokenizer 时用的 kwargs」（如 `do_lower_case`、`model_max_length`、特殊 token），加载时被当作 `init_kwargs` 还原，从而「存什么、还原什么」。

#### 4.4.3 源码精读

**入口与签名** [`from_pretrained`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1498-L1510)。和 config/model 一样，第一参数是 `pretrained_model_name_or_path`，下载相关参数（`cache_dir`/`revision`/`local_files_only`/`token`）也一致；额外多了 `trust_remote_code`。

**① 组装 vocab_files** [`tokenization_utils_base.py:1631-1640`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1631-L1640)：

```python
# tokenization_utils_base.py（节选）
additional_files_names = {
    "added_tokens_file": ADDED_TOKENS_FILE,
    "special_tokens_map_file": SPECIAL_TOKENS_MAP_FILE,
    "tokenizer_config_file": TOKENIZER_CONFIG_FILE,   # tokenizer_config.json
    "tokenizer_file": FULL_TOKENIZER_FILE,            # tokenizer.json
    "chat_template_file": CHAT_TEMPLATE_FILE,
}
vocab_files = {**cls.vocab_files_names, **additional_files_names}
```

`cls.vocab_files_names` 是子类声明（例如 BERT 声明需要 `vocab.txt`，LLaMA 声明需要 `tokenizer.model`）；`additional_files_names` 是所有 tokenizer 通用的「配置类」文件。

**② 解析每个文件** [`tokenization_utils_base.py:1715-1728`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1715-L1728)：对每个 `file_id` 调用 `cached_file`（注意 `_raise_exceptions_for_missing_entries=False`——词表里某些文件可选，缺失不报错）。

**③a 读 tokenizer_config.json 还原 kwargs** [`_from_pretrained`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1777-L1787)：

```python
# tokenization_utils_base.py（节选）
tokenizer_config_file = resolved_vocab_files.pop("tokenizer_config_file", None)
if tokenizer_config_file is not None:
    with open(tokenizer_config_file, encoding="utf-8") as h:
        init_kwargs = json.load(h)
    init_kwargs.pop("tokenizer_class", None)
    saved_init_inputs = init_kwargs.pop("init_inputs", ())
```

**③b 聊天模板优先级** [`tokenization_utils_base.py:1794-1810`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1794-L1810)：独立的 `.jinja` 模板文件优先于 `tokenizer_config.json` 里内嵌的 `chat_template` 字段（多模板场景会合成 dict）。这是 u3-l4（聊天模板）要展开的内容，此处先记住「模板也是文件、也会被加载」。

**③d 真正构造** [`tokenization_utils_base.py:1939-1942`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1939-L1942)：

```python
# tokenization_utils_base.py（节选）
init_kwargs = cls.convert_to_native_format(**init_kwargs)   # 把 tokenizer.json 的内容预拆成 vocab/merges
tokenizer = cls(*init_inputs, **init_kwargs)
```

**保存** [`save_pretrained`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1986-L2059)：它把 `self.init_kwargs`（构造时记录的参数）连同当前属性序列化成 `tokenizer_config.json`，再调用子类实现的 `save_vocabulary`（[`tokenization_utils_base.py:2203`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2203) 抽象方法）写出词表文件。文档里有一句重要警告：**「运行时对 tokenizer 的修改（如改 `do_lower_case`）不会被保存」**——因为存的是 `init_kwargs`，不是当前对象状态。

#### 4.4.4 代码实践

**实践目标**：看清楚 tokenizer 的 `save_pretrained` 到底写了哪些文件。

**操作步骤**（示例代码）：

```python
# 示例代码
from transformers import AutoTokenizer
import os

tok = AutoTokenizer.from_pretrained("hf-internal-testing/tiny-random-LlamaForCausalLM")
tok.save_pretrained("/tmp/tok_demo")

print(sorted(os.listdir("/tmp/tok_demo")))
```

**需要观察的现象**：目录里至少有 `tokenizer_config.json`、`special_tokens_map.json`、`tokenizer.json`，可能还有 `tokenizer.model`（取决于子类）。

**预期结果**：出现一组「配置 + 词表」文件。把这些文件删到只剩 `tokenizer.json`，再次 `AutoTokenizer.from_pretrained("/tmp/tok_demo")` 往往仍能加载成功——因为 `tokenizer.json` 是自洽的统一格式。待本地验证：不同模型子类产生的文件集合不同。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `cached_file` 在解析 tokenizer 文件时传了 `_raise_exceptions_for_missing_entries=False`？
**参考答案**：因为 `vocab_files` 里既有「必需的词表文件」，也有「可选的配置文件」（如 `chat_template.jinja`、`added_tokens.json`）。可选文件缺失是正常情况，不应报错，所以用这个标志让 `cached_file` 在找不到时返回 `None` 而非抛异常。

**练习 2**：加载时调用 `AutoTokenizer.from_pretrained(ckpt, pad_token="<pad>")`，这个 `pad_token` 会生效吗？走的是哪段逻辑？
**参考答案**：会生效。`_from_pretrained` 在读完 `tokenizer_config.json` 得到 `init_kwargs` 后，执行了 `init_kwargs.update(kwargs)`（[L1818-1819](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1818-L1819)），用户 kwargs 覆盖文件值，随后才用合并后的 kwargs 构造对象。

**练习 3**：`save_pretrained` 的文档警告「运行时改 `do_lower_case` 不会被保存」，结合源码解释原因。
**参考答案**：`save_pretrained` 序列化的是 `self.init_kwargs`（构造时传入并记录的参数快照），而不是对象当前的动态属性。运行时 `setattr` 修改 `do_lower_case` 不会回写到 `init_kwargs`，因此不会被保存。

---

## 5. 综合实践

把本讲四个模块串起来，完成课程指定的主任务：**加载一个模型 → 修改 config 的某个属性 → `save_pretrained` 到本地目录 → 重新加载验证修改已持久化**。

**实践目标**：亲手走完「改 config → 存盘 → 读回」的闭环，并理解每一步在源码里对应哪段逻辑。

**操作步骤**（示例代码）：

```python
# 示例代码
import json, os
from transformers import AutoModelForCausalLM

ckpt = "hf-internal-testing/tiny-random-LlamaForCausalLM"
out_dir = "/tmp/round_trip_demo"

# ① 加载模型（4.3：内部会先委托加载 config，再加载权重）
model = AutoModelForCausalLM.from_pretrained(ckpt)

# ② 修改 config 的某个结构属性（4.2：这里改的是「结构描述」）
#    注意：改结构属性后，权重与结构可能不再匹配，仅用于演示持久化，不要拿去推理
model.config.torchscript = True
model.config.attention_bias = True          # 任意一个非默认字段即可
original_id2label = {0: "negative", 1: "positive"}
model.config.id2label = original_id2label   # 加一个分类相关字段

# ③ 存盘（4.3 save_pretrained：写权重分片 + 一并写 config.json）
model.save_pretrained(out_dir)

# ④ 看一眼 config.json（4.2 to_diff_dict：应只含差异，且能看到我们改的字段）
print("=== files ===")
print(sorted(os.listdir(out_dir)))
print("=== config.json (节选) ===")
with open(os.path.join(out_dir, "config.json")) as f:
    cfg = json.load(f)
print({k: cfg[k] for k in ("torchscript", "attention_bias", "id2label") if k in cfg})

# ⑤ 重新加载，验证修改已持久化
reloaded = AutoModelForCausalLM.from_pretrained(out_dir)
assert reloaded.config.torchscript is True
assert reloaded.config.attention_bias is True
assert reloaded.config.id2label == original_id2label
print("✅ 持久化验证通过")
```

**需要观察的现象**：
1. `out_dir` 是一个完整 checkpoint 目录，含 `config.json` 与至少一个 `*.safetensors`（可能还有 `model.safetensors.index.json`）。
2. `config.json` 里能看到 `torchscript`、`attention_bias`、`id2label`，但**省略了**所有「等于 Llama 默认值」的字段。
3. 第 ⑤ 步断言全部通过，说明修改确实落盘并被读回。

**预期结果**：所有断言通过。待本地验证：`attention_bias` 是否出现在 diff 里取决于该字段默认值是否被改变（若恰好等于默认值则不会出现——这也是 `to_diff_dict` 的行为，可作为额外观察点）。

**延伸思考（选做）**：把第 ② 步改成 `model.config.num_hidden_layers = 1` 然后 `save_pretrained` + 重新加载。你会发现重新加载的模型层数变了、但权重对不上（会触发 `missing/unexpected keys` 警告）。这正是「config 只描述结构、权重需要单独对齐」的直观证据，对应 4.3 里 `_load_pretrained_model` 的诊断输出。

## 6. 本讲小结

- 四大预训练对象（config / model / tokenizer / processor）共享 `from_pretrained` / `save_pretrained`，其「读取底盘」是自由函数 `cached_file`，「写入底盘」是 `PushToHubMixin`，签名形态统一（都接受 `revision`/`token`/`cache_dir`/`local_files_only` 等下载参数）。
- `PretrainedConfig` 的加载是「`get_config_dict` → `cached_file(config.json)` → `from_dict`」三段式；保存默认只写「与默认值的差异」（`to_diff_dict`），所以 `config.json` 短而可读。
- `PreTrainedModel.from_pretrained` 是「先委托 `config_class.from_pretrained` 加载 config，再解析权重文件（优先 safetensors/支持分片），决定 dtype，构造空模型，最后 `_load_pretrained_model` 灌权重」的总流程；`save_pretrained` 自动分片（默认 50GB）并连带写出 config。
- `PreTrainedTokenizerBase.from_pretrained` 的特点是「多文件并行解析」：组装 `vocab_files` → 逐个 `cached_file` → 在 `_from_pretrained` 里用 `tokenizer_config.json` 还原 `init_kwargs`、再叠加用户 kwargs → `cls(**init_kwargs)` 构造。
- 三条加载链都遵循「用户 kwargs 覆盖文件值」的优先级（config 的 `from_dict`、tokenizer 的 `init_kwargs.update(kwargs)`、model 的 `model_kwargs`），所以「加载时临时改参数」是统一的能力。
- 每个加载链都会记录并传播 `_commit_hash`，从而保证 config / model / tokenizer 来自同一个 checkpoint 版本。

## 7. 下一步学习建议

- **向下钻一层**：本讲多次提到 `cached_file` 与缓存目录。下一讲 **u2-l3（Hub 下载与本地缓存机制）** 会专门拆解 `cached_file` / `snapshot_download` 的下载与缓存命中逻辑、`HF_HOME`/离线模式等，建议紧接着读。
- **横向补齐预处理对象**：本讲聚焦 config/model/tokenizer 三件套，processor/image_processor 共享同一范式但细节不同，可在 **u4（图像/音视频处理器）** 里对照阅读。
- **纵向深入模型加载细节**：本讲有意略过了 `dtype` 精细策略、`device_map`、量化与 meta device 初始化，这些将在 **u5-l2（PreTrainedModel 模型基类）** 与 **u5-l3（权重初始化体系）** 展开，并可结合 **u10-l1（量化）**、**u10-l3（分布式）** 继续深入。
- **建议阅读的源码**：动手跟踪一次 `LlamaForCausalLM.from_pretrained` 的调用，对照本讲 4.3 的 7 步流程图，在源码里逐行找到对应位置——这是把「读讲义」变成「会读源码」的最短路径。
