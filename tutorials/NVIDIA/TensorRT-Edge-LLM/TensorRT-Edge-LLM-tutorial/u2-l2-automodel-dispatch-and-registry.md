# AutoModel 分发与模型注册表

## 1. 本讲目标

本讲承接 [u2-l1](u2-l1-checkpoint-config-parsing.md)：你已经知道导出器如何把一个 HuggingFace 检查点目录解析成一份强类型的 `ModelConfig`。但「有了配置」并不等于「有了模型」——还差最后一步：**根据这份配置，决定用哪一段 Python 代码把模型搭出来，并把检查点权重灌进去**。

这一步的入口就是 `AutoModel.from_pretrained`。学完本讲，你应当能够：

1. 说出 `_MODEL_REGISTRY` 注册表是什么、`register_model` 如何填充它，以及为什么大多数架构不需要注册也能被处理。
2. 画出 `AutoModel.from_pretrained` 从「检查点目录」到「带权重的 `nn.Module`」的完整执行顺序，并能指出「选模型类」发生在哪一步。
3. 解释 `_resolve_model_variant` 如何在普通 LLM 与四种投机解码变体（eagle / mtp / dflash / gemma4-mtp）之间裁决，以及为什么 EAGLE3 draft 是「自动检测」而其它变体是「开关驱动」。
4. 理解权重 key remapping（`_eagle3_key_remap` / `_mtp_key_remap` / `_dflash_key_remap`）在加载阶段的作用。

## 2. 前置知识

- **检查点（checkpoint）**：一个本地目录，里面有 `config.json`、分词器文件和若干 `.safetensors` 权重分片。上一讲我们只读了它的「配置」。
- **`model_type`**：写在 `config.json` 里的字符串字段（如 `qwen3_moe`、`nemotron_h`、`gemma4`），是模型族的「身份证」。它由解析阶段原样读出，本身不携带任何代码。
- **`nn.Module`**：PyTorch 的模型基类。本项目中所有模型（无论 LLM、draft 还是视觉编码器）都是 `nn.Module` 子类。
- **投机解码（speculative decoding）**：用一个小的「draft（草稿）」模型先猜几个 token，再用大的「base（目标）」模型批量验证，命中就一次接受多个 token，从而加速生成。本项目支持四种草稿方案：EAGLE3、Qwen3.5 MTP、DFlash、Gemma4 MTP。这一讲不深入它们的算法（那是 u7 单元的事），只关心**导出阶段如何把同一份检查点导成不同角色（base 还是 draft）**。
- **工厂模式（factory）**：调用方只说「给我这个检查点的模型」，不关心内部用哪个类。`AutoModel` 就是一个工厂，和 HuggingFace 的 `AutoModelForCausalLM` 思路一致。

一个直觉类比：`ModelConfig` 是「这栋楼的设计图纸」，`AutoModel.from_pretrained` 是「按图纸选一支施工队，把楼盖起来，再把家具（权重）搬进去」。注册表是「图纸类型 → 施工队」的花名册。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tensorrt_edgellm/model.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py) | `AutoModel` 工厂、`register_model` 注册函数、`_resolve_model_variant` 变体裁决、三套 key remap 函数。本讲的主战场。 |
| [tensorrt_edgellm/__init__.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/__init__.py) | 包入口。导入各模型类，并在此处集中调用 `register_model` 填充注册表。 |
| [tensorrt_edgellm/config.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py) | `ModelConfig`。提供 `is_eagle3_draft`、`root_model_type` 等属性，是变体自动检测的依据。 |
| [tensorrt_edgellm/models/default/modeling_default.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py) | 默认 `CausalLM`。注册表里查不到某个 `model_type` 时就回退到它。 |

## 4. 核心概念与源码讲解

### 4.1 注册表机制：`_MODEL_REGISTRY` 与 `register_model`

#### 4.1.1 概念说明

导出器拿到一个检查点后，第一道分叉问题是：**用哪段 Python 代码搭这个模型？** 项目维护着一份全局字典 `_MODEL_REGISTRY`，把 `model_type` 字符串映射到对应的 `nn.Module` 子类。这就是「注册表（registry）模式」：注册发生在导入时（进程启动），查询发生在 `from_pretrained` 调用时。

注册表带来的核心好处是**解耦**：

- 写新模型的人只需在某处 `register_model("我的模型", MyModel, scale_fn)`，不必改动 `AutoModel` 的逻辑。
- 没有注册的 `model_type` 会自动回退到默认 `CausalLM`（见 4.2），所以「主流标准架构」零配置即可工作。

本项目其实维护着**两张**并行的注册表：

1. `_MODEL_REGISTRY`：`model_type → 模型类`。
2. `_ATTENTION_SCALE_DEFAULT_REGISTRY`：`model_type → 注意力缩放函数`（输入 `head_dim`，返回缩放系数）。

绝大多数模型族用 `1/sqrt(head_dim)` 这个标准缩放（`standard_attention_scale`）；Gemma4 系列很特殊，它的注意力不做缩放（系数恒为 1.0，见 `_identity_attention_scale`），所以为它单独注册了恒等缩放函数。

#### 4.1.2 核心流程

注册表的生命周期可以拆成两段：

```text
【导入时：填充注册表】
import tensorrt_edgellm
  └─> 执行 __init__.py 中的顶层语句
        └─> 多次调用 register_model("xxx", XxxCausalLM, scale_fn)
              ├─> _MODEL_REGISTRY["xxx"] = XxxCausalLM
              └─> _ATTENTION_SCALE_DEFAULT_REGISTRY["xxx"] = scale_fn

【调用时：查询注册表】
AutoModel.from_pretrained(dir)
  └─> model_class = _MODEL_REGISTRY.get(config.model_type, CausalLM)
                                          └─ 查不到就用默认
```

注意两个细节：

- 注册表是**模块级全局变量**，进程内共享，且只随导入顺序写入。这意味着调用方必须在导入了 `tensorrt_edgellm`（触发了 `__init__.py`）之后才能查到表里的项。
- `register_attention_scale_default` 是一个「只填缩放表、不填模型类表」的版本，专门留给「有特殊工厂分发、但不直接走 registry 查类」的模型（例如 `gemma4_assistant`，它由变体解析阶段硬编码处理，见 4.3）。

#### 4.1.3 源码精读

注册表本身只是两个模块级字典，定义在 [tensorrt_edgellm/model.py:46-48](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L46-L48)：

```python
AttentionScaleDefault = Callable[[int], float]
_MODEL_REGISTRY: Dict[str, Type[nn.Module]] = {}
_ATTENTION_SCALE_DEFAULT_REGISTRY: Dict[str, AttentionScaleDefault] = {}
```

填表的函数 `register_model` 非常薄，就是往两张表各写一条，见 [tensorrt_edgellm/model.py:82-98](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L82-L98)：

```python
def register_model(model_type, model_class, default_attention_scale):
    _MODEL_REGISTRY[model_type] = model_class
    _ATTENTION_SCALE_DEFAULT_REGISTRY[model_type] = default_attention_scale
```

它的 docstring 还明确约定了一条**契约**：被注册的 `model_class` 必须能以单个 `ModelConfig` 作为构造参数（即 `model_class(config)`）。这条契约是后续 `model = model_class(config)` 能统一调用的前提。

真正「填表」的语句集中在 `__init__.py` 的导入区，见 [tensorrt_edgellm/__init__.py:60-91](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/__init__.py#L60-L91)。这里把每个模型类 `import` 进来，再逐一登记：

```python
register_model("gemma4", Gemma4ForCausalLM, _identity_attention_scale)
register_model("nemotron_h", NemotronHCausalLM, standard_attention_scale)
register_model("qwen3_5_text", Qwen3_5CausalLM, standard_attention_scale)
register_model("qwen3_moe", Qwen3MoeCausalLM, standard_attention_scale)
register_model("qwen3_omni", Qwen3OmniLanguageModel, standard_attention_scale)
# ... 以及其它若干条
register_attention_scale_default("gemma4_assistant", _identity_attention_scale)
```

两条要点：

- 上一讲提到的「同一模型族在不同检查点里 `model_type` 写法不同」的问题（如 `gemma4` / `gemma4_text` / `gemma4_unified`），在这里通过**给多个别名登记同一个类**来解决。
- `gemma4_assistant` 用的是 `register_attention_scale_default`（只登缩放、不登类），因为它的类由 4.3 的变体逻辑硬编码选择，不经过 registry 查类。

最后看一眼 `__all__` 把哪些名字暴露为公共 API，见 [tensorrt_edgellm/__init__.py:93-103](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/__init__.py#L93-L103)。注意 `register_model` 本身也在 `__all__` 里——这是给「想接入自定义模型」的二次开发者准备的扩展点。

#### 4.1.4 代码实践

**目标**：亲自看清注册表「导入时填、调用时查」这一生命周期。

**操作步骤**：

1. 打开 [tensorrt_edgellm/__init__.py:60-91](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/__init__.py#L60-L91)，把每一条 `register_model(...)` 的「`model_type` → 类名」抄成一张表。
2. 打开一个 Python REPL（无需 GPU），执行：

   ```python
   import tensorrt_edgellm           # 触发 __init__.py，填表
   from tensorrt_edgellm.model import _MODEL_REGISTRY
   for k, v in sorted(_MODEL_REGISTRY.items()):
       print(f"{k:30s} -> {v.__name__}")
   ```

**需要观察的现象**：REPL 打印出的表，与你手工从 `__init__.py` 抄出来的表应当**完全一致**——这证明注册表内容完全由 `__init__.py` 的导入顺序决定。

**预期结果**：你会看到约十几条映射，例如 `gemma4 → Gemma4ForCausalLM`、`nemotron_h → NemotronHCausalLM`、`qwen3_moe → Qwen3MoeCausalLM`，以及 `qwen3_omni` 等多模态/对话模型族的若干条。

**说明**：如果你没有安装本包而只想读源码，跳过执行，直接核对第 1 步抄出的表即可（属于源码阅读型实践）。运行结果取决于你本机安装的版本，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `qwen3_5_moe` 和 `qwen3_5_moe_text` 两个 `model_type` 都映射到 `Qwen3_5MoeCausalLM`？

**参考答案**：因为不同来源的检查点（HF 原始 vs 导出后的产物）里 `model_type` 字段写法不同，但它们是同一个模型族、用同一段代码。对多个别名登记同一个类，就能让两种写法都命中正确的实现。

**练习 2**：`gemma4_assistant` 为什么用 `register_attention_scale_default` 而不是 `register_model`？

**参考答案**：`gemma4_assistant` 是 Gemma4 MTP 的 draft 角色，它的模型类由变体解析阶段（4.3）硬编码选择（`Gemma4AssistantForCausalLM`），不走 `_MODEL_REGISTRY` 查类；但它仍需要登记自己的注意力缩放函数，所以只填缩放表。

---

### 4.2 `AutoModel.from_pretrained` 工厂分发

#### 4.2.1 概念说明

`AutoModel` 是一个仿 HuggingFace 风格的「自动分发工厂」。它本身不持有任何模型实现，唯一的方法 `from_pretrained(model_dir, ...)` 负责把「一个检查点目录」变成「一个加载好权重的 `nn.Module`」。

它是**所有上层调用方**（CLI、Python API、VLM 文本子模型）的共同入口。无论你要导出普通 LLM、EAGLE3 draft、还是 Qwen3.5 MTP base，最后都会汇聚到这一个方法。区别只在于传给它的**开关参数**不同。

理解 `from_pretrained` 的关键是抓住它的「选模型类」有**两层**：

- **第一层：变体（variant）**。先决定这是「普通 LLM」还是某一种投机解码角色（eagle3_draft / mtp_draft / dflash_draft / gemma4_mtp_draft / mtp_base / …）。这一层是硬编码的 `if/elif`，由 `_resolve_model_variant`（见 4.3）裁决。
- **第二层：注册表查询**。只有当变体是普通 LLM（`llm`）或部分 base 变体时，才用 `_MODEL_REGISTRY.get(model_type, CausalLM)` 选类。查不到就回退到默认 `CausalLM`。

这种「变体优先、注册表兜底」的顺序意味着：**投机解码的 draft 模型永远不走注册表**，它们是变体逻辑里写死的专用类。

#### 4.2.2 核心流程

`from_pretrained` 的主干可以概括成八个阶段：

```text
1. 读配置        load_model_config(model_dir) -> ModelConfig
2. 置变体标志    把 eagle_base / mtp_base / dflash_base / gemma4_mtp_base
                 等开关「写进 config」(config.eagle_base = True ...)
3. 张量并行切片  若 tp_size>1, config = config.for_rank(tp_rank, tp_size)
4. 裁决变体      variant = _resolve_model_variant(config, ...)
5. 选模型类      按 variant 选 model_class(变体硬编码 或 注册表查询 或 默认CausalLM)
6. 实例化+搬设备 model = model_class(config); model.to(device)
7. 加载权重      load_weights(model, model_dir, key_remap=..., pre_repack_hook=...)
8. 后处理        如 Qwen3.5 的 GDN 投影融合 fuse_gdn_input_projections(model)
```

其中**第 4、5 步**是「分发」的核心，第 1 步是上一讲的成果，第 7 步的细节属于 [u2-l4](u2-l4-checkpoint-loading-and-repacking.md)。

第 2 步有个容易忽略的设计：开关参数（`eagle_base`、`mtp_base` 等）并非只传给 `_resolve_model_variant`，而是**先合并进 `config`**（见 4.2.3）。这样下游的 `model_class(config)`、ONNX 导出、运行时配置都能从 `config` 里读到这些角色信息，`from_pretrained` 不必把一堆布尔到处传。

#### 4.2.3 源码精读

`from_pretrained` 的签名很长，但绝大多数是「角色开关」。见 [tensorrt_edgellm/model.py:124-144](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L124-L144)（节选关键参数）：

```python
@classmethod
def from_pretrained(cls, model_dir, device="cpu", key_remap=None,
                    eagle_base=False, mtp_base=False, mtp_draft=False,
                    dflash_base=False, dflash_tree_base=False,
                    dflash_draft=False, dflash_draft_dir=None,
                    gemma4_mtp_base=False, gemma4_mtp_draft=False,
                    gemma4_kv_sharing_map=None, gemma4_target_kv_cache_quant=None,
                    num_decoder_layers=None, ...):
```

**第 1 步**读配置走的是 `load_model_config`，它和上一讲的 `ModelConfig.from_pretrained` 的区别在于：它会先查 `_ATTENTION_SCALE_DEFAULT_REGISTRY` 来确定这个模型族的缩放函数，再传给 `from_pretrained`。见 [tensorrt_edgellm/model.py:108-118](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L108-L118)：

```python
def load_model_config(model_dir):
    root, llm_dict = load_checkpoint_config_dicts(model_dir)
    default_attention_scale = standard_attention_scale
    for config in (root, llm_dict):
        model_type = config.get("model_type")
        if model_type in _ATTENTION_SCALE_DEFAULT_REGISTRY:
            default_attention_scale = _ATTENTION_SCALE_DEFAULT_REGISTRY[model_type]
            break
    return ModelConfig.from_pretrained(model_dir, default_attention_scale)
```

**第 2 步**把开关合并进 `config`，见 [tensorrt_edgellm/model.py:205-236](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L205-L236)（节选）：

```python
config = load_model_config(model_dir)
if eagle_base:
    config.eagle_base = True
if mtp_base or config.mtp_base:
    config.mtp_base = True
if gemma4_mtp_base:
    config.gemma4_mtp_base = True
if dflash_base:
    config.dflash_base = True
if dflash_tree_base:
    config.dflash_base = True
    config.dflash_tree_base = True
...
if tp_size > 1:
    config = config.for_rank(tp_rank, tp_size)
```

**第 4、5 步**裁决变体并选类，是整个方法的分发心脏，见 [tensorrt_edgellm/model.py:238-322](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L238-L322)。其结构是一连串 `elif`：

```python
variant = _resolve_model_variant(config, eagle_base=..., mtp_base=..., ...)
if variant == "eagle3_draft":
    model_class = Eagle3DraftModel
    if key_remap is None: key_remap = _eagle3_key_remap
elif variant == "mtp_draft":
    ...
    model_class = Qwen3_5MtpDraftModel
elif variant == "dflash_draft":
    ...
    model_class = DFlashDraftModel
elif variant == "gemma4_mtp_draft":
    ...
    model_class = Gemma4AssistantForCausalLM
else:
    # 兜底分支：普通 LLM 或 base 变体走注册表查询 + CausalLM 回退
    if variant == "gemma4_mtp_base":
        config.gemma4_mtp_base = True
        model_class = Gemma4ForCausalLM
    else:
        model_class = _MODEL_REGISTRY.get(config.model_type, CausalLM)
```

最后那一行 `_MODEL_REGISTRY.get(config.model_type, CausalLM)` 就是**注册表查询 + 默认回退**。注意 `CausalLM` 是在方法内部 `from .models.default.modeling_default import CausalLM` 延迟导入的（[tensorrt_edgellm/model.py:203](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L203)），避免模块加载时的循环依赖。

默认 `CausalLM` 的构造函数严格符合注册表契约——只吃一个 `ModelConfig`，见 [tensorrt_edgellm/models/default/modeling_default.py:526](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L526)：

```python
def __init__(self, config: ModelConfig) -> None:
```

它之所以能当「万能回退」，是因为它**不按 `model_type` 写死层结构，而是依据 `config.layer_types` 逐层动态搭建**——这正是上一讲强调「`layer_types` 是混合模型的逐层标注」的价值所在：只要一种架构的层类型是 attention/mamba/gdn/moe/mlp 中的组合，默认 `CausalLM` 就能搭出来，无需注册。

#### 4.2.4 代码实践

**目标**：跟踪真实调用方，理解「同一个 `from_pretrained` 入口、不同开关」的用法。

**操作步骤**：

1. 打开 [tensorrt_edgellm/scripts/export.py:866-880](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L866-L880)，这是普通 LLM / EAGLE base / DFlash base / MTP base 共用的导出调用点。观察它如何把 `eagle_base`、`mtp_base`、`dflash_base`、`dflash_tree_base` 这些布尔一次性传进去。
2. 再对比两个 draft 专用调用点：
   - Gemma4 MTP draft：[tensorrt_edgellm/scripts/export.py:974-981](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L974-L981)，只开 `gemma4_mtp_draft=True` 并附带 KV 共享映射。
   - DFlash draft：[tensorrt_edgellm/scripts/export.py:1010-1013](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L1010-L1013)，开 `dflash_draft=True` 并传 `dflash_draft_dir`。

**需要观察的现象**：所有这些调用都落在同一个 `AutoModel.from_pretrained`，没有任何 if/else 选择「用哪个加载函数」——角色完全由布尔开关编码。

**预期结果**：你能用一句话概括「CLI 决定开关 → 开关进 `config` → `config` 决定变体 → 变体决定模型类」这条链路。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `from_pretrained` 要先把 `eagle_base` 等布尔写进 `config`，而不是只在 `_resolve_model_variant` 调用里直接传参？

**参考答案**：因为模型类实例化后，ONNX 导出和运行时配置也需要知道当前角色（例如 EAGLE3 base 要多输出一份 `hidden_states`、要加 tree-attention 输入）。把角色固化进 `config` 后，下游只需读 `config.eagle_base`，`from_pretrained` 不必再把这些布尔层层传递。

**练习 2**：一个全新、未注册的 decoder-only 模型，为什么往往不需要改任何代码就能被 `from_pretrained` 处理？

**参考答案**：因为兜底分支 `_MODEL_REGISTRY.get(model_type, CausalLM)` 会回退到默认 `CausalLM`，而默认 `CausalLM` 依据 `config.layer_types` 逐层搭建。只要新架构的层类型属于已支持的集合（attention/mamba/gdn/moe/mlp），就能被默认实现覆盖。只有当层结构或算子有特殊性（如 Gemma4 的恒等注意力缩放）时，才需要专门注册一个类。

---

### 4.3 变体解析：`_resolve_model_variant` 与 key remapping

#### 4.3.1 概念说明

「变体（variant）」是本讲最核心的概念。同一个检查点，可以被导成**不同角色**：

- `llm`：普通自回归 LLM（既可作 base，也可是无投机解码的独立模型）。
- `eagle3_draft` / `mtp_draft` / `dflash_draft` / `gemma4_mtp_draft`：四种投机解码的**草稿**模型。
- `eagle_base` / `mtp_base` / `dflash_base` / `gemma4_mtp_base`：对应的**目标**模型（带额外输入/输出以配合草稿验证）。

`_resolve_model_variant` 的职责就是：**综合「调用方传入的开关」和「检查点自身的元数据」，决定本次到底导成哪一个变体。** 它返回一个字符串，`from_pretrained` 再据此选模型类。

这里有一个**极其重要的不对称设计**：

- 大多数变体是**开关驱动**的——调用方显式传 `mtp_draft=True` 才会导成 draft。
- 但 **EAGLE3 draft 是自动检测**的：只要检查点的 `config.json` 里有 `draft_vocab_size` 字段，就被判定为 EAGLE3 draft，即使调用方没传任何 eagle 开关。

这是因为 EAGLE3 的 draft 检查点自带一个特殊的「词表裁剪」标记（`draft_vocab_size`，draft 模型用一个小词表），这个事实写在检查点里、无法从外部开关推断。自动检测能让用户「拿来一个 EAGLE3 draft 检查点直接导出」，而不必记住要加什么参数。

另一个需要理解的概念是 **key remapping（权重键重映射）**。draft 检查点在训练/保存时的权重张量命名，往往和导出模型期望的 `nn.Module` 参数名对不上。例如 EAGLE3 训练产物里有个 `midlayer.*` 前缀，但模型类期望 `layers.0.*`；还有 `t2d`（target-to-draft）投影是训练用的、推理不需要，应当跳过。`key_remap(key)` 这个函数就是用来在加载权重时把检查点的 key 翻译成模型期望的 key（返回 `None` 表示丢弃该 key）。

#### 4.3.2 核心流程

`_resolve_model_variant` 的判定逻辑是「**先互斥校验，后按优先级返回**」：

```text
【第一阶段：互斥校验】
eagle_base 与 mtp_base 不能同时为真      -> raise
eagle_base 与 mtp_draft 不能同时为真     -> raise
mtp_base 与 mtp_draft 不能同时为真        -> raise
dflash_base 与 dflash_draft 不能同时为真  -> raise
dflash 与 eagle/mtp 互斥                  -> raise
gemma4_mtp 与其它一切投机变体互斥          -> raise
gemma4_mtp_base 与 gemma4_mtp_draft 互斥  -> raise

【第二阶段：按优先级返回唯一变体字符串】
if config.is_eagle3_draft:        # 自动检测：检查点带 draft_vocab_size
    return "eagle3_draft"
if gemma4_mtp_draft:              # 开关驱动
    return "gemma4_mtp_draft"
if dflash_draft:
    return "dflash_draft"
if dflash_base:
    return "dflash_base"
if mtp_draft:
    return "mtp_draft"
if mtp_base:
    return "mtp_base"
if gemma4_mtp_base:
    return "gemma4_mtp_base"
if eagle_base:
    return "eagle_base"
return "llm"                       # 都没有 -> 普通模型
```

注意优先级顺序不是随意的：**EAGLE3 draft 的自动检测被放在最前**。这意味着即使调用方误传了别的开关，只要检查点确实是 EAGLE3 draft（带 `draft_vocab_size`），也会被优先识别——前提是它没和 `mtp_base`/`mtp_draft` 冲突（那种情况会在第一阶段就 raise）。

选完变体后，`from_pretrained` 会**在加载权重前**挂上对应的 `key_remap`：

| 变体 | key_remap 函数 | 主要变换 |
|------|----------------|----------|
| eagle3_draft | `_eagle3_key_remap` | 丢 `t2d`（保留 `d2t`）、丢 `target_model.*`、`midlayer.*` → `layers.0.*`、展平 `qkv_proj.*`、修 `pre_quant_scale` 命名 |
| mtp_draft | `_mtp_key_remap` | 去掉 `mtp.` 前缀、保留 `lm_head.weight`、按需把 `embed_tokens` 当作 tied `lm_head` |
| dflash_draft | `_dflash_key_remap` | 丢弃 `rotary_emb` 相关 key（位置编码不是可学权重） |

#### 4.3.3 源码精读

`_resolve_model_variant` 全文见 [tensorrt_edgellm/model.py:475-533](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L475-L533)。互斥校验段（节选）：

```python
if eagle_base and mtp_base:
    raise ValueError("eagle_base and mtp_base cannot both be enabled.")
if dflash_base and dflash_draft:
    raise ValueError("dflash_base and dflash_draft cannot both be enabled.")
if gemma4_mtp_base and (eagle_base or mtp_base or mtp_draft or dflash_base or dflash_draft):
    raise ValueError("gemma4_mtp_base cannot be combined with other speculative variants.")
...
```

返回段的关键——EAGLE3 自动检测在最前，见 [tensorrt_edgellm/model.py:513-533](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L513-L533)：

```python
if config.is_eagle3_draft:
    if mtp_base or mtp_draft:
        raise ValueError("EAGLE3 draft checkpoints cannot be loaded as Qwen3.5 MTP variants.")
    return "eagle3_draft"
if gemma4_mtp_draft:
    return "gemma4_mtp_draft"
if dflash_draft:
    return "dflash_draft"
...
if eagle_base:
    return "eagle_base"
return "llm"
```

`is_eagle3_draft` 是 `ModelConfig` 上的只读属性，判定依据就是「检查点是否带 `draft_vocab_size`」，见 [tensorrt_edgellm/config.py:699-701](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L699-L701)：

```python
@property
def is_eagle3_draft(self) -> bool:
    return self.draft_vocab_size is not None
```

而 `draft_vocab_size` 是解析阶段直接从检查点读出来的，见 [tensorrt_edgellm/config.py:874](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L874)（`draft_vocab_size = llm_dict.get("draft_vocab_size", None)`）。这就是「EAGLE3 自动检测」的完整链条：检查点字段 → `ModelConfig` 属性 → `_resolve_model_variant` 优先分支。

Gemma4 MTP draft 则走另一条「自动 + 开关混合」的路径：它的检查点根 `model_type` 必须是 `gemma4_assistant`，见 [tensorrt_edgellm/config.py:830-833](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L830-L833)，而 `from_pretrained` 在选中 `gemma4_mtp_draft` 变体后还会**强制校验**这一点，见 [tensorrt_edgellm/model.py:288-302](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L288-L302)：

```python
elif variant == "gemma4_mtp_draft":
    if config.root_model_type != "gemma4_assistant":
        raise ValueError("Gemma4 MTP draft requires a gemma4_assistant checkpoint.")
    ...
    config.shares_target_kv = True
    config.has_own_kv_cache = False
    config.kv_sharing_map = list(gemma4_kv_sharing_map or [])
    model_class = Gemma4AssistantForCausalLM
```

`shares_target_kv` / `has_own_kv_cache = False` 反映了 Gemma4 MTP draft 与目标模型**共享 KV 缓存**的特殊设计（详见 u7-l1）。

最后看 key remapping。`_eagle3_key_remap` 见 [tensorrt_edgellm/model.py:536-558](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L536-L558)：

```python
def _eagle3_key_remap(key):
    if "t2d" in key and "d2t" not in key:
        return None          # 跳过 target→draft 投影（推理不需要）
    if key.startswith("target_model."):
        return None          # 跳过多目标训练产物
    key = key.replace("midlayer.", "layers.0.")
    key = key.replace("qkv_proj.q_proj", "q_proj")
    ...
    return key
```

`_mtp_key_remap` 见 [tensorrt_edgellm/model.py:763-777](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L763-L777)，核心是去掉 `mtp.` 前缀，并在 `tie_word_embeddings` 时把 embedding 表当作 `lm_head`：

```python
def _mtp_key_remap(key, *, tie_word_embeddings):
    if key.startswith("mtp."):
        return key[len("mtp."):]
    if key == "lm_head.weight":
        return key
    if tie_word_embeddings and key in ("model.embed_tokens.weight", ...):
        return "lm_head.weight"
    return None
```

`_dflash_key_remap` 见 [tensorrt_edgellm/model.py:756-760](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L756-L760)，只丢弃 `rotary_emb`（位置编码是常量、非可学权重）：

```python
def _dflash_key_remap(key):
    if "rotary_emb" in key:
        return None
    return key
```

这些 `key_remap` 在哪里被使用？就在 `from_pretrained` 选完类之后、调用 `load_weights` 时作为参数传入，见 [tensorrt_edgellm/model.py:394-400](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L394-L400)：

```python
load_weights(model, model_dir, device=device,
             key_remap=key_remap, key_prefix=key_prefix,
             pre_repack_hook=pre_repack_hook, mapping=config.mapping)
```

权重的实际加载机制（`key_remap` 如何逐 key 应用、`pre_repack_hook` 如何处理量化权重）是 [u2-l4](u2-l4-checkpoint-loading-and-repacking.md) 的主题，本讲只需记住：**key remap 是「检查点命名 → 模型参数命名」的翻译层，且发生在变体裁决之后**。

#### 4.3.4 代码实践

**目标**：亲手走一遍「同一检查点、不同开关 → 不同变体 → 不同返回值」的判定。

**操作步骤**（纯源码阅读 + 心智推演，无需 GPU）：

1. 打开 [tensorrt_edgellm/model.py:475-533](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L475-L533)，准备一张「输入 → 输出」推演表。
2. 对下面四种虚拟场景，分别推断 `_resolve_model_variant` 返回什么、`from_pretrained` 会选哪个 `model_class`：
   - **场景 A**：普通 Qwen3 检查点，所有开关为 `False`，`config` 无 `draft_vocab_size`。
   - **场景 B**：一个带 `draft_vocab_size` 的 EAGLE3 draft 检查点，调用方什么也没传。
   - **场景 C**：Qwen3.5 base 检查点，调用方传 `mtp_draft=True`。
   - **场景 D**：调用方同时传 `eagle_base=True` 和 `mtp_base=True`。

**需要观察的现象 / 预期结果**：

| 场景 | `_resolve_model_variant` 返回 | 选中的 model_class | 是否抛异常 |
|------|-------------------------------|--------------------|-----------|
| A | `"llm"` | `_MODEL_REGISTRY.get(model_type, CausalLM)` → 默认 `CausalLM` | 否 |
| B | `"eagle3_draft"`（自动检测优先） | `Eagle3DraftModel`，并挂 `_eagle3_key_remap` | 否 |
| C | `"mtp_draft"` | `Qwen3_5MtpDraftModel`（需 `model_type` 属于支持的集合），并挂 `_mtp_key_remap` | 否（若 `model_type` 不支持则 `NotImplementedError`） |
| D | — | — | 是，第一阶段互斥校验 `raise ValueError` |

3. 核对场景 C 的支持集合：`_is_qwen3_5_mtp_draft_supported` 只认 `qwen3_5_text`，见 [tensorrt_edgellm/model.py:60-70](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L60-L70)。

**说明**：本实践为源码阅读型，结论由静态阅读得出；如要运行验证，需准备对应检查点并安装本包，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 EAGLE3 draft 用「自动检测」，而 MTP draft 用「开关驱动」？

**参考答案**：EAGLE3 draft 检查点自身带 `draft_vocab_size` 这个明确标记，能被解析阶段读到，所以可以零参数自动识别，对用户最友好。MTP draft 是从一个**普通 base 检查点**「派生」出来的（用 `make_mtp_draft_config` 改造 base 配置），base 检查点本身没有「我是 draft」的标记，必须由调用方显式声明 `mtp_draft=True` 才知道要派生。

**练习 2**：`_mtp_key_remap` 里对 `tie_word_embeddings` 的分支是做什么用的？

**参考答案**：当源检查点词表权重与输出头「绑定」（tie）时，检查点里可能只存了 `embed_tokens.weight` 而没有独立的 `lm_head.weight`。此时把 `embed_tokens.weight` 重映射成 `lm_head.weight`，让 draft 模型的输出头能从 embedding 表加载。若没绑定（`tie_word_embeddings=False`）则不做此回退，避免错误地用 embedding 当输出头。

**练习 3**：`_resolve_model_variant` 把「EAGLE3 自动检测」放在返回链最前面，有什么副作用风险？项目如何防护？

**参考答案**：风险是：如果用户对一个 EAGLE3 draft 检查点误传了 `mtp_base`/`mtp_draft`，自动检测会让它先被判成 `eagle3_draft`，语义混乱。项目在 EAGLE3 分支里加了显式校验——若同时有 `mtp_base or mtp_draft` 就 `raise ValueError`（见 [model.py:513-517](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L513-L517)），把冲突挡在最早。

---

## 5. 综合实践

**任务**：完整复现「注册表是怎么被填的」，并设计接入一个自定义模型的最小步骤。

### 步骤一：列出当前所有 `model_type → 模型类` 映射

1. 读 [tensorrt_edgellm/__init__.py:60-91](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/__init__.py#L60-L91)，把每条 `register_model(...)` / `register_attention_scale_default(...)` 抄成一张三列表：`model_type | 模型类 | 注意力缩放函数`。
2. （可选，需本机安装）用 REPL 验证：

   ```python
   import tensorrt_edgellm
   from tensorrt_edgellm.model import _MODEL_REGISTRY, _ATTENTION_SCALE_DEFAULT_REGISTRY
   assert set(_MODEL_REGISTRY) <= set(_ATTENTION_SCALE_DEFAULT_REGISTRY)
   ```

   你会发现缩放表的键集合 ⊇ 模型类表的键集合——因为 `gemma4_assistant` 只进了缩放表。**待本地验证**。

### 步骤二：为一个新的 `model_type` 接入自定义类（伪代码）

假设你要接入一个 `model_type == "myllm"` 的新架构，且它需要一个自定义模型类 `MyLLMCausalLM`。基于本讲所学，最小步骤是：

```text
1. 写模型类 MyLLMCausalLM(nn.Module)
   - 构造函数签名必须是 def __init__(self, config: ModelConfig)
   - 内部依据 config.layer_types 逐层搭建（参考默认 CausalLM）
   - 权重参数名要与 HF 检查点的张量名兼容，否则要配 key_remap

2. 决定注意力缩放
   - 若用标准 1/sqrt(head_dim)：用现成的 standard_attention_scale
   - 若有特殊缩放（如恒等）：写一个 def my_scale(head_dim): return 1.0

3. 在某处导入时注册（最自然的位置是 __init__.py，或你自己的入口模块）
   from .models.myllm import MyLLMCausalLM
   register_model("myllm", MyLLMCausalLM, standard_attention_scale)

4. 确保该 register_model 所在模块被 import 到
   （否则注册表里没有这一条，from_pretrained 会回退到默认 CausalLM）

5. 验证三步走（参考 u1-l5）
   - AutoModel.from_pretrained("path/to/myllm") 能加载、参数数(param_count)合理
   - export_onnx 导出成功
   - 后续 build → inference 跑通
```

**需要观察的现象**：第 4 步最关键——如果你忘了让注册语句被执行，`_MODEL_REGISTRY.get("myllm", CausalLM)` 会**静默回退**到默认 `CausalLM` 而不报错。这既是注册表的优点（兼容性好），也是它的坑（接错了不容易察觉）。验证方法是在 `from_pretrained` 选类处临时加一行 `print(model_class)`，或检查 `model.__class__.__name__` 是否是你期望的类名。

**预期结果**：你能清晰说出「写类 → 选缩放函数 → 注册 → 确保导入 → 三步验证」这五步，并理解第 4 步「确保被导入」是新手最容易漏的一环。

## 6. 本讲小结

- `AutoModel.from_pretrained` 是整个 Python 导出前端的统一加载入口：读配置 → 置变体标志 → 张量并行切片 → 裁决变体 → 选模型类 → 实例化 → 加载权重 → 后处理。
- 注册表是两个模块级字典 `_MODEL_REGISTRY`（`model_type → 类`）与 `_ATTENTION_SCALE_DEFAULT_REGISTRY`（`model_type → 缩放函数`），由 `__init__.py` 在导入时通过 `register_model` 填充。
- 选模型类分两层：**变体优先**（draft 模型硬编码），**注册表兜底**（普通 LLM 查表，查不到回退默认 `CausalLM`）。默认 `CausalLM` 因依据 `layer_types` 逐层搭建，能覆盖大多数标准架构。
- `_resolve_model_variant` 先做互斥校验、再按优先级返回变体字符串。EAGLE3 draft 是**唯一自动检测**的变体（依据检查点的 `draft_vocab_size`），其余都是开关驱动。
- key remapping（`_eagle3_key_remap` / `_mtp_key_remap` / `_dflash_key_remap`）在变体裁决之后、权重加载时生效，负责把检查点张量名翻译成模型参数名。
- 所有上层调用（CLI、Python API、VLM 文本子模型、各 draft 导出）最终都汇聚到同一个 `from_pretrained`，角色完全由布尔开关编码。

## 7. 下一步学习建议

本讲讲清了「选哪个类、搭哪个角色」，但模型类内部到底怎么从 `ModelConfig` 一层层搭出来、又如何用 EdgeLLM 自定义算子构建，还没展开。建议下一讲进入：

- **[u2-l3 默认解码器模型实现](u2-l3-default-decoder-implementation.md)**：深入默认 `CausalLM` 的内部，看 `embedding → attention → mlp → lm_head` 如何用 `ops.py` 的自定义算子组装，以及 `linear.py` 里 FP16 线性层与量化线性层的区别。
- 配合阅读 **[u2-l4 检查点加载与权重重排](u2-l4-checkpoint-loading-and-repacking.md)**，搞清楚本讲末尾那个 `load_weights(..., key_remap=...)` 内部到底怎么逐 key 落位、怎么处理量化权重的 repack。

如果想提前看「变体」在导出 CLI 层面如何被组合调度，可先扫一眼 `scripts/export.py` 的各 `_export_*` 函数（[tensorrt_edgellm/scripts/export.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py)），那是 [u2-l6](u2-l6-export-cli-multimodal-orchestration.md) 的主题。
