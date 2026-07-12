# 模型架构 ModelArch 与 ModelKeys

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `ModelArch`、`ModelKeys`、`MultiModelKeys` 三者的关系，以及它们为什么存在。
- 解释 `MODEL_ARCH_MAPPING` 注册表与 `get_model_arch` 查找机制，理解 `ModelMeta.model_arch` 从「字符串」被替换成「对象」的关键一步。
- 看懂一个具体模型的 `ModelKeys` 指向了哪些子模块（`q_proj`/`mlp`/`lm_head`/`language_model` 等）。
- 准确回答一个容易答错的问题：`--target_modules all-linear` 在文本模型和多模态模型上分别命中了哪些模块，`ModelKeys` 在其中起了什么作用。
- 知道哪些 tuner（llamapro / adapter / ia3）真正直接消费 `ModelKeys` 的线性层字段。

## 2. 前置知识

本讲承接 [u3-l1 模型注册与加载机制](u3-l1-model-registry-and-loading.md)，那里我们建立了两条结论：

1. ms-swift 用全局字典 `MODEL_MAPPING: Dict[str, ModelMeta]` 把「结构相同的一族模型」抽象成一个 `model_type`。
2. `ModelMeta` 是一份**静态档案**，加载后会被挂回 `model.model_meta`，供模板系统、tuner 等后续环节读取。

本讲聚焦 `ModelMeta` 里一个看起来不起眼、实则牵动「该冻哪些参数」「该给哪些层挂 LoRA」的字段：`model_arch`。

在进入源码前，先建立两个直觉：

- **不同模型族给同一类子模块起的名字千差万别。**同样是注意力里的「输出投影」，LLaMA 叫 `self_attn.o_proj`，Qwen（旧版）叫 `attn.c_proj`，ChatGLM 叫 `self_attention.dense`，Baichuan 干脆把 QKV 打包成一个 `W_pack`。如果要写一个「给所有 MLP 加 Adapter」或「插入新 Transformer block」的通用算法，就必须有一张表，告诉算法「这个模型族的 MLP 在哪」「它的 module list 在哪」——这正是 `ModelKeys` 要做的事。
- **多模态模型是「若干个子模型的拼装」。**一个 Qwen2-VL 至少包含：语言模型（LLM）、视觉塔（vision tower）、对齐器（aligner/merger，把视觉特征投影到语言空间）。训练时往往希望「只训 LLM、冻结 ViT、冻结 aligner」，因此需要一张表标明这三部分的边界——这正是 `MultiModelKeys` 要做的事。

理解了这两点，本讲所有源码都是在回答「如何用数据结构描述模型结构」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [swift/model/model_arch.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_arch.py) | 定义 `ModelArch`（架构名常量）、`ModelKeys`/`MultiModelKeys` 数据类、`MODEL_ARCH_MAPPING` 注册表与 `register_model_arch`/`get_model_arch`。本讲的主战场。 |
| [swift/model/register.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py) | `register_model` 在注册模型时把 `ModelMeta.model_arch` 从「字符串」解析成「`ModelKeys` 对象」。 |
| [swift/model/model_meta.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py) | `ModelMeta` 数据类，声明 `model_arch` 字段。 |
| [swift/model/models/qwen.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/models/qwen.py) | Qwen 系列模型的注册示例，展示 `model_arch=` 的真实用法。 |
| [swift/utils/transformers_utils.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/transformers_utils.py) | `find_all_linears` / `get_multimodal_target_regex` / `find_layers` —— 把 `ModelKeys`/`MultiModelKeys` 翻译成 LoRA 的 `target_modules`。 |
| [swift/pipelines/train/tuner.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/tuner.py) | `get_target_modules` 把 `all-linear` 展开成真实模块名；`prepare_adapter` 给 llamapro/adapter/reft 等 tuner 传 `model_arch`。 |
| [swift/arguments/tuner_args.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/tuner_args.py) | `freeze_llm`/`freeze_vit`/`freeze_aligner` 参数与全参数训练时的冻结逻辑。 |
| [swift/tuners/llamapro.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/llamapro.py) | LLaMAPro tuner，`ModelKeys` 线性层字段（`module_list`/`o_proj`/`down_proj`/`attention`）的直接消费者。 |

## 4. 核心概念与源码讲解

### 4.1 ModelArch 与 ModelKeys：用数据结构描述模型结构

#### 4.1.1 概念说明

ms-swift 需要一个「模型族名」常量来标识「这一族模型的结构相同、模块命名相同」。这个常量就是 `ModelArch`——它其实只是两个装满字符串常量的类拼起来的命名空间。

[swift/model/model_arch.py:10-105](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_arch.py#L10-L105) 定义了三件事：

- `LLMModelArch`：纯文本模型族名，如 `llama`、`qwen`、`chatglm`、`internlm2`、`deepseek_v2`。
- `MLLMModelArch`：多模态模型族名，如 `qwen2_vl`、`qwen3_vl`、`internvl`、`llava_hf`、`minicpmv`。
- `ModelArch(LLMModelArch, MLLMModelArch)`：多重继承，把两族常量合并成一个统一命名空间，方便外部用 `ModelArch.llama`、`ModelArch.qwen2_vl` 取值。

注意文件里的注释点出了一条重要经验：

```python
class ModelArch(LLMModelArch, MLLMModelArch):
    # Multimodal models typically require specifying model_arch,
    # while text-only models usually do not need to specify model_arch.
    pass
```

也就是说：**多模态模型几乎必须显式指定 `model_arch`**（因为要分层冻结/分层加 LoRA），**纯文本模型多数情况下可以不指定**（因为线性层可以通过扫描模型结构自动发现）。

`ModelArch` 只是「名字表」。真正描述「这个族里 q_proj 在哪、mlp 在哪」的是 `ModelKeys` 数据类：

[swift/model/model_arch.py:108-133](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_arch.py#L108-L133)

```python
@dataclass
class ModelKeys:
    """Used to support training of tuners such as llama-pro"""
    arch_name: str = None

    embedding: str = None
    module_list: str = None
    lm_head: str = None

    q_proj: str = None
    k_proj: str = None
    v_proj: str = None
    o_proj: str = None
    attention: str = None

    mlp: str = None
    down_proj: str = None

    qkv_proj: str = None
    qk_proj: str = None
    qa_proj: str = None
    ...
```

读这段要抓住三个要点：

1. **每个字段都是「点分层级路径」字符串**，例如 `'model.layers.{}.self_attn.q_proj'`。
2. **`{}` 是层号占位符**，代表「第 0/1/2... 层」。使用方会把 `{}` 替换成具体层号（如 `.format(3)`），或替换成 `0` 去 `get_submodule` 探测。
3. **同时存在「拆分」与「打包」两种注意力写法**：`q_proj/k_proj/v_proj` 用于 QKV 分开存的模型（LLaMA 系），`qkv_proj` 用于把 QKV 打包进一个 `Linear` 的模型（Qwen 旧版 `c_attn`、InternLM2 `wqkv`、Baichuan `W_pack`）。`ModelKeys` 用「哪几个字段非空」来隐式记录这个结构差异。

以 LLaMA 族（Qwen2/Qwen3/Llama 等都复用它）为例，注册内容如下：

[swift/model/model_arch.py:167-180](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_arch.py#L167-L180)

```python
register_model_arch(
    ModelKeys(
        LLMModelArch.llama,
        module_list='model.layers',
        mlp='model.layers.{}.mlp',
        down_proj='model.layers.{}.mlp.down_proj',
        attention='model.layers.{}.self_attn',
        o_proj='model.layers.{}.self_attn.o_proj',
        q_proj='model.layers.{}.self_attn.q_proj',
        k_proj='model.layers.{}.self_attn.k_proj',
        v_proj='model.layers.{}.self_attn.v_proj',
        embedding='model.embed_tokens',
        lm_head='lm_head',
    ))
```

这一份 `ModelKeys` 就是「LLaMA 系模型的解剖图」：模块列表在 `model.layers`，每层注意力在 `self_attn`，MLP 输出投影在 `mlp.down_proj`，词表嵌入在 `model.embed_tokens`，输出头叫 `lm_head`。

> **一个容易被忽略的事实**：Qwen2、Qwen2.5、Qwen3 这些「qwen」名字的模型，结构上其实和 LLaMA 一样（都是 `model.layers.N.self_attn.q_proj` 这种命名），所以它们在注册时填的是 `model_arch=ModelArch.llama`，而不是 `ModelArch.qwen`。见 [swift/model/models/qwen.py:488-490](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/models/qwen.py#L488-L490) 与 [swift/model/models/qwen.py:568-570](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/models/qwen.py#L568-L570)。而 `ModelArch.qwen` 对应的是**老版 Qwen**（`QWenLMHeadModel`，命名是 `transformer.h.N.attn.c_attn`），见 [swift/model/model_arch.py:276-287](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_arch.py#L276-L287)。这告诉我们：**`model_arch` 反映的是「模块命名结构」，而不是「模型品牌」**。

#### 4.1.2 核心流程

把一个 `ModelKeys` 字段翻译成「真实可访问的子模块」的过程，可以用下面这段伪代码描述：

```text
# 字段值，例如 'model.layers.{}.self_attn.q_proj'
key_str = model_keys.q_proj

# (1) 拿到某一层：把 {} 替换成层号
layer_path = key_str.format(layer_idx)        # 'model.layers.3.self_attn.q_proj'
submodule  = model.get_submodule(layer_path)  # 真实的 nn.Linear

# (2) 或者「定位模块名」而非具体层（用于扫描所有层）：
#     把 {} 替换成 0 探测、或用正则 '\d+' 抹掉层号做模式匹配。
probe_path = key_str.replace('{}', '0')
```

所以 `ModelKeys` 的本质是：**一份「字段名 → 层号模板字符串」的映射表**，让算法能用统一的字段名（如 `down_proj`）去访问任何模型族的对应模块，而不必为每个模型族写 if-else。

#### 4.1.3 源码精读：谁在直接消费 ModelKeys 的线性层字段？

这是本讲最容易产生误解的地方，必须用源码澄清。直接消费 `q_proj`/`mlp`/`o_proj`/`down_proj`/`module_list`/`attention` 这些字段的，是少数「需要精确知道某类子模块位置」的 tuner，典型代表是 LLaMAPro：

[swift/tuners/llamapro.py:228-231](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/llamapro.py#L228-L231) 用 `module_list` 找到所有 Transformer 层：

```python
@staticmethod
def _find_module_list(config, module: nn.Module) -> nn.ModuleList:
    model_key_mapping = LLaMAPro.get_model_key_mapping(config.model_type, config)
    return module.get_submodule(model_key_mapping.module_list)
```

[swift/tuners/llamapro.py:204-219](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/llamapro.py#L204-L219) 用 `o_proj`/`down_proj` 把新插入 block 的这两个投影权重清零（保证新 block 初始为恒等映射）：

```python
o_proj = model_key_mapping.o_proj.split('{}.')[1]
down_proj = model_key_mapping.down_proj.split('{}.')[1]
for idx, module in enumerate(module_list):
    if idx not in new_module_idx:
        continue
    _o_proj: nn.Linear = module.get_submodule(o_proj)
    _down_proj: nn.Linear = module.get_submodule(down_proj)
    _o_proj.weight.data = torch.zeros_like(_o_proj.weight.data)
    _down_proj.weight.data = torch.zeros_like(_down_proj.weight.data)
```

同理，`adapter` 与 `ia3` tuner 用 `mlp` 字段定位前馈网络：

[swift/pipelines/train/tuner.py:250-261](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/tuner.py#L250-L261)

```python
elif args.tuner_type == 'adapter':
    model_arch = model.model_meta.model_arch
    mlp_key = model_arch.mlp
    mlp_key = mlp_key.split('.{}.')[1]
    adapter_config = AdapterConfig(dim=model.config.hidden_size, target_modules=[mlp_key], ...)
```

[swift/tuner_plugin/ia3.py:19-21](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuner_plugin/ia3.py#L19-L21)

```python
model_arch: ModelKeys = model.model_meta.model_arch
ia3_config = IA3Config(
    target_modules=find_all_linears(model), feedforward_modules='.*' + model_arch.mlp.split('{}.')[1] + '.*')
```

注意 `.split('.{}.')[1]` 这个惯用法：它从 `'model.layers.{}.mlp'` 里切出叶子模块名 `'mlp'`，正是「层号占位符 `{}`」的设计意图——既能在带 `{}` 时定位某层，也能在切掉 `{}` 段后拿到「跨层通用的叶子名」。

> 关键结论（先记下，4.4 会展开）：**`all-linear` 并不直接读 `q_proj`/`mlp` 这些字段去拼目标模块**；它主要靠扫描模型实例。`ModelKeys` 的线性层字段是给 llamapro/adapter/ia3 这类「需要精确解剖结构」的 tuner 用的。

#### 4.1.4 代码实践：打印一个模型的 ModelKeys 解剖图

1. **实践目标**：不下载权重，仅通过 `MODEL_MAPPING` 与 `MODEL_ARCH_MAPPING` 查看某个 `model_type` 的「结构解剖表」。
2. **操作步骤**：在一个装好 ms-swift 的环境里执行下面的脚本（示例代码，可直接保存为 `inspect_arch.py` 运行）。

```python
# 示例代码
from swift.model import MODEL_MAPPING
from swift.model.model_arch import ModelKeys

def show(model_type: str):
    meta = MODEL_MAPPING[model_type]
    arch = meta.model_arch  # 注册时已被替换成 ModelKeys/MultiModelKeys 对象
    print(f'== {model_type} ==')
    print(f'arch_name      : {arch.arch_name}')
    print(f'is_multimodal  : {meta.is_multimodal}')
    for f in ['module_list', 'mlp', 'down_proj', 'o_proj', 'q_proj', 'embedding', 'lm_head']:
        print(f'  {f:12s}: {getattr(arch, f)}')
    # 若是多模态，还有分层字段
    for f in ['language_model', 'aligner', 'vision_tower', 'generator']:
        v = getattr(arch, f, None)
        if v:
            print(f'  {f:12s}: {v}')

# 文本模型：Qwen3 复用 llama 架构
show('qwen3')
# 多模态模型
show('qwen2_vl')
```

3. **需要观察的现象**：
   - `qwen3` 的 `arch_name` 应为 `llama`，`module_list='model.layers'`，`lm_head='lm_head'`，且 `language_model` 等字段为空（因为它是 `ModelKeys` 而非 `MultiModelKeys`）。
   - `qwen2_vl` 的 `arch_name` 应为 `qwen2_vl`，`module_list` 等线性字段为 `None`，但 `language_model`、`vision_tower`、`aligner` 是列表。
4. **预期结果**：你会直观看到「文本模型的解剖表偏线性层定位、多模态模型的解剖表偏分层边界」。
5. ⚠️ 具体字段值（尤其 `qwen2_vl` 在不同 `transformers` 版本下 `language_model` 的写法）依赖运行环境的 `transformers` 版本，**待本地验证**。可对照 [swift/model/model_arch.py:575-590](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_arch.py#L575-L590) 里的 `transformers_ge_4_52` 分支理解差异。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ModelKeys` 要同时提供 `q_proj/k_proj/v_proj` 和 `qkv_proj` 两套字段，而不是统一用一套？

> **参考答案**：因为不同模型族的注意力实现不同。LLaMA/Qwen2 把 Q/K/V 存成三个独立 `Linear`，所以填 `q_proj/k_proj/v_proj`；老版 Qwen/InternLM2/Baichuan 把 QKV 打包进单个 `Linear`（如 `c_attn`/`wqkv`/`W_pack`），所以填 `qkv_proj`。算法通过「哪个字段非空」就能判断该模型是拆分式还是打包式，从而选择正确的挂载/清零策略。

**练习 2**：Qwen3 的 `model_arch` 为什么是 `ModelArch.llama` 而不是 `ModelArch.qwen`？

> **参考答案**：`model_arch` 描述的是「模块命名结构」。Qwen3 的权重命名（`model.layers.N.self_attn.q_proj` 等）与 LLaMA 完全一致，而老版 `ModelArch.qwen` 对应的是 `transformer.h.N.attn.c_attn` 这种命名。复用 `llama` 让 Qwen3 直接享有 llama 系的所有解剖信息与 tuner 支持。

---

### 4.2 MODEL_ARCH_MAPPING 匹配：注册表与那个关键的「字符串→对象」替换

#### 4.2.1 概念说明

有了 `ModelKeys` 数据类，还需要一个全局注册表把「架构名 → ModelKeys 实例」存起来，这就是 `MODEL_ARCH_MAPPING`。它和 u3-l1 里的 `MODEL_MAPPING` 是同一套「导入即注册 + 字典查表」范式（参见 [u1-l3 模块化架构](u1-l3-directory-and-architecture.md) 讲过的「base.py + mapping.py + 注册表」三件套）。

注册与查找的代码非常薄：

[swift/model/model_arch.py:152-164](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_arch.py#L152-L164)

```python
MODEL_ARCH_MAPPING = {}

def register_model_arch(model_arch: ModelKeys, *, exist_ok: bool = False) -> None:
    arch_name = model_arch.arch_name
    if not exist_ok and arch_name in MODEL_ARCH_MAPPING:
        raise ValueError(f'The `{arch_name}` has already been registered in the MODEL_ARCH_MAPPING.')
    MODEL_ARCH_MAPPING[arch_name] = model_arch
```

[swift/model/model_arch.py:843-844](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_arch.py#L843-L844)

```python
def get_model_arch(arch_name: Optional[str]) -> Optional[MultiModelKeys]:
    return MODEL_ARCH_MAPPING.get(arch_name)
```

注意 `get_model_arch` 的返回类型注解写的是 `Optional[MultiModelKeys]`——因为 `MultiModelKeys` 继承自 `ModelKeys`，凡是注册进表里的对象，统一按「最多四字段分层」的 `MultiModelKeys` 视角看待是安全的。

#### 4.2.2 核心流程：注册模型时发生的那一步替换

这里有一个**极易被忽略、但本讲最关键的细节**。在 [swift/model/model_meta.py:65](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_meta.py#L65)，`ModelMeta` 声明 `model_arch` 字段的类型是字符串（实际写成 `Optional[str]`）：

```python
model_arch: Optional[str] = None
```

而各 `models/*.py` 在注册模型时，`model_arch=` 也确实传的是字符串常量（如 `ModelArch.llama`，本质是 `'llama'`）。

但是后续所有消费方（`find_all_linears`、`get_multimodal_target_regex`、llamapro、ia3 …）访问的都是 `model.model_meta.model_arch.lm_head`、`.language_model` 这样的**对象属性**。字符串哪来的属性？

答案在 `register_model` 里——注册模型时做了一次「就地替换」：

[swift/model/register.py:31-42](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/register.py#L31-L42)

```python
def register_model(model_meta: ModelMeta, *, exist_ok: bool = False) -> None:
    from .model_arch import get_model_arch
    model_type = model_meta.model_type
    if not exist_ok and model_type in MODEL_MAPPING:
        raise ValueError(f'The `{model_type}` has already been registered in the MODEL_MAPPING.')
    if model_meta.model_arch:
        model_meta.model_arch = get_model_arch(model_meta.model_arch)   # ← 字符串变对象
    MODEL_MAPPING[model_type] = model_meta
```

读这段要抓住三点：

1. `model_meta.model_arch` 进入函数时是字符串（如 `'llama'`），出函数时被替换成 `MODEL_ARCH_MAPPING['llama']` 指向的那个 `ModelKeys` 实例。
2. 替换是**就地修改** `model_meta`，所以存进 `MODEL_MAPPING` 的那份档案，其 `model_arch` 已经是对象。
3. 若 `model_arch` 为空（很多纯文本模型不指定），则保持 `None`，消费方需自行兜底。

这是个非常典型的「**声明时是惰性引用、注册时解析成真实对象**」的设计：声明侧只记一个名字（轻量、可读、不引入对 `model_arch.py` 的循环依赖），注册侧统一查表替换。理解了这一步，前面「字符串却有 `.lm_head` 属性」的疑惑就迎刃而解。

完整的「架构匹配」链路因此是：

```text
models/qwen.py 注册: ModelMeta(..., model_arch=ModelArch.llama)   # 'llama' 字符串
        │
        ▼ register_model()
ModelMeta.model_arch = MODEL_ARCH_MAPPING['llama']                 # ModelKeys 对象
        │
        ▼ get_model_processor() 加载后挂回
model.model_meta.model_arch                                        # 同一个 ModelKeys 对象
        │
        ▼ 消费方读取
find_all_linears / get_multimodal_target_regex / llamapro / ia3 ...
```

#### 4.2.3 源码精读：自定义模型的注册示例

官方示例 `examples/custom/my_qwen2_5_omni/my_register.py` 完整展示了「先注册架构、再注册模型」的两步流程，是把本讲知识用于二次开发的最小范本：

[examples/custom/my_qwen2_5_omni/my_register.py:7-20](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/custom/my_qwen2_5_omni/my_register.py#L7-L20)

```python
from swift.model import (Model, ModelGroup, ModelLoader, ModelMeta, MultiModelKeys, get_model_processor,
                         register_model, register_model_arch)
...
register_model_arch(
    MultiModelKeys(
        'my_qwen2_5_omni',
        # `freeze_llm`, `freeze_vit`, `freeze_aligner` behavior is determined by the values below.
        ...
```

[examples/custom/my_qwen2_5_omni/my_register.py:73-88](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/custom/my_qwen2_5_omni/my_register.py#L73-L88)

```python
register_model(
    ModelMeta(
        'my_qwen2_5_omni',
        ...
        template='my_qwen2_5_omni',
        is_multimodal=True,  # Whether it's a multimodal model
        model_arch='my_qwen2_5_omni',  # Usually set only for multimodal models
        ...
```

注释里写得很直白：`freeze_llm`/`freeze_vit`/`freeze_aligner` 的行为就由你在这里填的 `MultiModelKeys` 决定。

#### 4.2.4 代码实践：验证「字符串→对象」替换确实发生了

1. **实践目标**：用代码证实 `MODEL_MAPPING[type].model_arch` 是对象而非字符串。
2. **操作步骤**：运行下面的示例代码。

```python
# 示例代码
from swift.model import MODEL_MAPPING

meta = MODEL_MAPPING['qwen2_vl']
arch = meta.model_arch
print('type(arch)        :', type(arch).__name__)      # 期望 MultiModelKeys
print('arch.arch_name    :', arch.arch_name)           # 期望 'qwen2_vl'
print('arch.language_model:', arch.language_model)     # 期望是 list
print('arch.vision_tower  :', arch.vision_tower)
print('arch.aligner       :', arch.aligner)
```

3. **需要观察的现象**：`type(arch)` 是 `MultiModelKeys` 而非 `str`，证明 `register_model` 里的替换已生效。
4. **预期结果**：字段值与 [swift/model/model_arch.py:575-598](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_arch.py#L575-L598) 中 `qwen2_vl` / `qwen3_vl` 的注册一致；具体前缀（是否带 `model.`）取决于 `transformers` 版本分支，**待本地验证**。

#### 4.2.5 小练习与答案

**练习**：如果 `MODEL_MAPPING['xxx'].model_arch` 为 `None`（文本模型未指定），后续 `find_all_linears` 还能正常工作吗？

> **参考答案**：能。`find_all_linears`（见 4.4.3）内部对 `model_arch` 做了兜底：若 `model_arch` 为空或 `model_arch.lm_head` 为空，则用默认值 `'lm_head'` 作为排除名。所以未指定 `model_arch` 的文本模型仍可正常扫描出线性层，只是 `lm_head` 的排除名按默认 `lm_head` 处理。这正是「纯文本模型通常不必指定 `model_arch`」的底气所在。

---

### 4.3 MultiModelKeys：多模态模型的分层解剖

#### 4.3.1 概念说明

多模态模型不是一个单一 Transformer，而是「视觉/音频编码器 + 对齐器 + 语言模型」的拼装体。训练时我们常需要分别控制每一部分：只想训 LLM、冻结视觉塔、冻结对齐器，等等。`MultiModelKeys` 就是为这种「分层控制」设计的：

[swift/model/model_arch.py:135-150](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_arch.py#L135-L150)

```python
@dataclass
class MultiModelKeys(ModelKeys):
    """Used to support freeze_vit/freeze_aligner/freeze_llm"""
    language_model: Union[str, List[str]] = field(default_factory=list)
    aligner: Union[str, List[str]] = field(default_factory=list)
    vision_tower: Union[str, List[str]] = field(default_factory=list)
    generator: Union[str, List[str]] = field(default_factory=list)

    def __post_init__(self):
        for key in ['language_model', 'aligner', 'vision_tower', 'generator']:
            v = getattr(self, key)
            if isinstance(v, str):
                setattr(self, key, [v])
            if v is None:
                setattr(self, key, [])
```

读这段要理解三件事：

1. **它继承自 `ModelKeys`**。所以多模态模型「也可以」同时填线性层字段（`module_list`/`mlp`/`o_proj` …），从而支持 llamapro 这类 tuner；但大多数多模态模型不需要插入新 block，只填分层字段即可。这也解释了为什么 `get_model_arch` 的返回类型注解统一写成 `MultiModelKeys`——它是 `ModelKeys` 的超集。
2. **四个分层字段**：
   - `language_model`：语言模型主体（含可能的 `lm_head`）。
   - `vision_tower`：视觉/音频编码器（注释里强调「vit」也包含 `audio_tower`，所以命名上叫 vision 实则泛指感知前端）。
   - `aligner`：对齐器/投影器（把感知特征投影到语言空间，如 `merger`/`multi_modal_projector`）。
   - `generator`：生成器（如 Omni 模型的 `talker`/`token2wav` 这类语音合成、TTS 部分）。
3. **`__post_init__` 做归一化**：把 `str` 统一包成 `[str]`，把 `None` 统一改成 `[]`。这样下游消费方可以无差别地「遍历列表」，不必同时处理三种类型。这是 dataclass 里非常实用的小技巧——**入口归一化，内部代码就不用到处写 `isinstance` 判断**。

#### 4.3.2 核心流程：分层如何映射成冻结/挂载行为

以 Qwen2-VL 为例看一份真实的多模态注册（`transformers>=4.52` 分支）：

[swift/model/model_arch.py:575-582](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_arch.py#L575-L582)

```python
register_model_arch(
    MultiModelKeys(
        MLLMModelArch.qwen2_vl,
        language_model=['model.language_model', 'lm_head'],
        aligner='model.visual.merger',
        vision_tower='model.visual',
    ))
```

这份表说：Qwen2-VL 的 LLM 部分是 `model.language_model` 加上 `lm_head`；视觉塔是 `model.visual`；对齐器是 `model.visual.merger`（视觉塔内部的一个子模块，负责把多尺度特征合并）。

这套分层表有两个独立的消费路径，分别对应「全参数训练」和「LoRA 训练」：

**路径 A：全参数训练（`tuner_type=full`）→ 冻结参数**

[swift/arguments/tuner_args.py:204-220](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/tuner_args.py#L204-L220)

```python
def _init_multimodal_full(self):
    model_arch = self.model_meta.model_arch
    if not self.model_meta.is_multimodal or not model_arch or self.tuner_type != 'full':
        return
    if self.freeze_llm:
        self.freeze_parameters += model_arch.language_model
    if self.freeze_vit:
        self.freeze_parameters += model_arch.vision_tower
    if self.freeze_aligner:
        self.freeze_parameters += model_arch.aligner
    else:
        self.trainable_parameters += model_arch.aligner
    self.freeze_parameters += model_arch.generator
    ...
```

读法：全参数训练时，三个 freeze 开关直接把对应的分层路径追加进 `freeze_parameters` 列表（前缀匹配冻结，见 u5 训练器单元）。注意 `generator` 默认无条件冻结——因为像 TTS 这类生成器通常不需要训练。

**路径 B：LoRA 训练（`target_modules=['all-linear']`）→ 只给「未冻结」的部分挂 LoRA**，这条逻辑在 4.4 节专门展开。

两条路径共用同一份 `MultiModelKeys` 表，区别只在于「冻结」还是「挂 LoRA」——这就是分层抽象的威力。

#### 4.3.3 源码精读：复杂多模态的分层示例

有些模型的分层更复杂，能很好地体现 `MultiModelKeys` 的表达力。

Omni 模型（Qwen2.5-Omni）同时有视觉和音频塔、还有独立的生成器：

[swift/model/model_arch.py:600-607](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_arch.py#L600-L607)

```python
register_model_arch(
    MultiModelKeys(
        MLLMModelArch.qwen2_5_omni,
        language_model=['thinker.model', 'thinker.lm_head'],
        vision_tower=['thinker.audio_tower', 'thinker.visual'],
        aligner=['thinker.audio_tower.proj', 'thinker.visual.merger'],
        generator=['talker', 'token2wav'],
        ))
```

注意这里 `vision_tower` 同时列了 `audio_tower` 和 `visual`——印证了「vit 泛指感知前端」。若你想「只对视觉塔加 LoRA、不动音频塔」，就需要改这张表（官方文档在 [docs/source/Megatron-SWIFT/Command-line-parameters.md](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/docs/source/Megatron-SWIFT/Command-line-parameters.md) 的 `freeze_vit` 说明里也提到了这一点）。

某些模型会把对齐器写成多个（因为有多模态注入路径），例如 phi4-multimodal 同时有图像和音频投影：

[swift/model/model_arch.py:527-538](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_arch.py#L527-L538)

```python
register_model_arch(
    MultiModelKeys(
        MLLMModelArch.phi4_multimodal,
        language_model='model.layers',
        aligner=[
            'model.embed_tokens_extend.image_embed.img_projection',
            'model.embed_tokens_extend.audio_embed.audio_projection'
        ],
        vision_tower=[
            'model.embed_tokens_extend.image_embed.img_processor', 'model.embed_tokens_extend.audio_embed.encoder'
        ],
    ))
```

正因为 `__post_init__` 把列表归一化了，下游的 `for module in modules:` 才能统一遍历，无需关心某个字段原本是 str 还是 list。

#### 4.3.4 代码实践：把分层表翻译成「会训哪些部分」

1. **实践目标**：给定一个多模态 `model_type` 与一组 freeze 开关，预测 LoRA 训练会触及哪些子模型。
2. **操作步骤**：运行下面的示例代码，模拟 `get_multimodal_target_regex` 的「选模块」逻辑。

```python
# 示例代码
from swift.model import MODEL_MAPPING

def which_parts(model_type, freeze_llm, freeze_vit, freeze_aligner):
    arch = MODEL_MAPPING[model_type].model_arch
    chosen = []
    if not freeze_llm:
        chosen += [('llm', m) for m in arch.language_model]
    if not freeze_vit:
        chosen += [('vit', m) for m in arch.vision_tower]
    if not freeze_aligner:
        chosen += [('aligner', m) for m in arch.aligner]
    return chosen

# 默认配置：freeze_llm=False, freeze_vit=True, freeze_aligner=True
print('默认 (训LLM, 冻ViT, 冻aligner):')
for tag, m in which_parts('qwen2_vl', freeze_llm=False, freeze_vit=True, freeze_aligner=True):
    print(f'  会加LoRA: [{tag}] {m}')
```

3. **需要观察的现象**：默认配置下只有 `language_model` 里的路径会进入「加 LoRA」候选，`vision_tower`/`aligner` 被排除。
4. **预期结果**：输出应只包含 `model.language_model` 与 `lm_head` 对应的 tag=`llm` 行。这与 [swift/utils/transformers_utils.py:217-225](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/transformers_utils.py#L217-L225) 的实际选模块逻辑一致（具体路径前缀**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：`MultiModelKeys.__post_init__` 把 `None` 改成 `[]`、把 `str` 改成 `[str]`。如果去掉这段归一化，下游代码会在哪里出错？

> **参考答案**：下游（如 `_init_multimodal_full` 的 `+= model_arch.vision_tower`、`get_multimodal_target_regex` 的 `for module in modules`）都假设这些字段是 list。若不归一化，当某个模型只填了 `vision_tower='visual'`（str）时，`+=` 会把字符串按字符拆开、`for module in modules` 会遍历到单个字符，导致冻结/挂载完全错乱；若填 `None` 则 `+= None` 直接抛 TypeError。

**练习 2**：为什么 `generator` 字段在全参数训练里被「无条件冻结」（`self.freeze_parameters += model_arch.generator`），而不像 llm/vit/aligner 那样给开关？

> **参考答案**：`generator` 通常指 Omni/TTS 模型里的语音合成等「非语言建模」组件（如 `token2wav`/`talker`），它们与文本任务目标不一致，常规 SFT/LoRA 一般不应训练它们，所以默认无条件冻结。需要训练它们的场景非常少，故没有暴露独立开关，保持参数面简洁。

---

### 4.4 综合落地：ModelKeys 如何影响 `target_modules=all-linear` 的命中范围

> 本节回应本讲的核心实践任务，把前面三个模块串起来。它澄清一个高频误解：**很多人以为 `all-linear` 是去读 `ModelKeys.q_proj` 之类的字段来拼目标模块，其实不是。**

#### 4.4.1 概念说明

`--target_modules all-linear` 是 LoRA 的默认值（[swift/arguments/tuner_args.py:126](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/tuner_args.py#L126)）。它的「展开」入口在 `get_target_modules`：

[swift/pipelines/train/tuner.py:91-110](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/tuner.py#L91-L110)

```python
def get_target_modules(args, model) -> Union[str, List[str]]:
    """Replace all-linear to actual modules"""
    if isinstance(args.target_modules, str):
        return args.target_modules
    target_modules = args.target_modules.copy()
    if 'all-linear' in target_modules:
        if model.model_meta.is_multimodal:
            return get_multimodal_target_regex(
                model,
                freeze_llm=args.freeze_llm,
                freeze_vit=args.freeze_vit,
                freeze_aligner=args.freeze_aligner,
                include_embedding='all-embedding' in target_modules)
        else:
            target_modules.remove('all-linear')
            target_modules += find_all_linears(model)
    if 'all-embedding' in target_modules:
        target_modules.remove('all-embedding')
        target_modules += find_embedding(model)
    return target_modules
```

读法：`all-linear` 的展开**按「是否多模态」分两条完全不同的路径**。

#### 4.4.2 核心流程：文本模型 vs 多模态模型

**路径 1：纯文本模型 → `find_all_linears`（扫描模型实例）**

[swift/utils/transformers_utils.py:178-205](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/transformers_utils.py#L178-L205)

```python
def find_all_linears(model, model_arch=None, extra_layers=None, sub_module=None):
    if model_arch is None:
        model_arch = model.model_meta.model_arch
    # lm_head
    if model_arch and model_arch.lm_head:
        output = model_arch.lm_head
        idx = output.rfind('.')
        lm_head_name = output[idx + 1:]
    else:
        lm_head_name = 'lm_head'
    ignore_layers = [lm_head_name, 'score', 'v_head', 'classifier'] + ['lora_A', 'lora_B', 'base_layer']
    ...
    def _cond(name, module):
        module_name = module.__class__.__name__.lower()
        if (... or 'linear' in module_name ...) and all(layer not in name for layer in ignore_layers):
            return True
        return False
    return find_layers(model, _cond, sub_module=sub_module)
```

关键结论：对文本模型，`find_all_linears` 是**遍历真实的 `nn.Module` 树**，挑出所有类名含 `linear` 的子模块，并排除 `lm_head`/`score`/`v_head`/`classifier`（分类/奖励模型头）以及已存在的 lora 层。它**不读** `q_proj`/`k_proj`/`mlp` 这些字段去拼名字。

那它读 `ModelKeys` 的哪个字段？只有一个：**`lm_head`**——用来知道「这个模型的输出头叫什么名字，要排除掉」。例如：

- LLaMA 系：`lm_head='lm_head'` → 排除名为 `lm_head` 的层。
- InternLM2：`lm_head='output'` → 排除名为 `output` 的层（见 [swift/model/model_arch.py:191](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_arch.py#L191)）。
- ChatGLM：`lm_head='transformer.output_layer'` → 排除 `output_layer`。

所以对纯文本模型，「`ModelKeys` 影响 `all-linear` 命中范围」的真正含义是：**`ModelKeys.lm_head` 决定了哪个输出头被排除**；至于哪些线性层被命中，取决于扫描模型实例，而非 `ModelKeys` 的线性层字段。

**路径 2：多模态模型 → `get_multimodal_target_regex`（用 MultiModelKeys 分层 + 扫描）**

[swift/utils/transformers_utils.py:208-255](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/transformers_utils.py#L208-L255)

```python
def get_multimodal_target_regex(model, *, freeze_llm=False, freeze_vit=True, freeze_aligner=True,
                                include_embedding=False, exclude_router=False) -> str:
    model_arch = model.model_meta.model_arch
    modules = []
    if not freeze_llm:
        modules += model_arch.language_model
    if not freeze_vit:
        modules += model_arch.vision_tower
    if not freeze_aligner:
        modules += model_arch.aligner
    assert len(modules) > 0, f'modules: {modules}'
    ...
    for module in modules:
        sub_module = deep_getattr(model, module)
        ...
        target_modules = find_all_linears(sub_module, model_arch, extra_layers)
        ...
        res.append(rf'{rejected_pattern}{re.escape(module)}(?=\.){target_pattern}')
    return rf'^({"|".join(res)})$'
```

读法（这是多模态 LoRA 的核心逻辑）：

1. 先按 freeze 开关，从 `MultiModelKeys` 的 `language_model`/`vision_tower`/`aligner` 里**选出「要训练」的子模块路径**（默认 `freeze_llm=False` ⇒ 只选 `language_model`）。
2. 对每个选中的子模块，**递归地在该子模块内部**调用 `find_all_linears`（扫描它的线性层）。
3. 把结果拼成一条正则表达式返回（注意返回类型是 `str`，是一条正则，所以 `get_target_modules` 里 `isinstance(args.target_modules, str)` 分支会直接走 `target_regex` 路径，把它整体当作正则用）。

因此对多模态模型，`MultiModelKeys` 的分层字段**直接决定了 LoRA 会挂到哪个子模型上**。以 Qwen2-VL 默认配置（`freeze_llm=False, freeze_vit=True, freeze_aligner=True`）为例：只有 `language_model`（`model.language_model`、`lm_head`）进入候选 → LoRA 只挂在 LLM 的线性层上，ViT 与 aligner 完全不挂。这就是「多模态 LoRA 默认只训 LLM」的底层原因。

#### 4.4.3 一张表总结 ModelKeys 的真实消费者

| 字段 | 主要消费者 | 作用 |
| --- | --- | --- |
| `module_list` | LLaMAPro | 找到 Transformer 层列表，用于插入新 block |
| `o_proj` / `down_proj` | LLaMAPro | 把新插入 block 的这两个投影清零，保证恒等初始化 |
| `attention` | LLaMAPro | 给新 block 的注意力层补 `layer_idx` 等属性 |
| `mlp` | `adapter` tuner、`ia3` tuner | 定位前馈网络，决定 Adapter/IA3 挂载点与 `feedforward_modules` |
| `lm_head` | `find_all_linears`（文本路径） | 决定 `all-linear` 要排除哪个输出头 |
| `language_model` / `vision_tower` / `aligner` / `generator` | `get_multimodal_target_regex`（多模态 LoRA 路径）、`_init_multimodal_full`（全参数冻结）、`get_lm_head_model` | 多模态分层控制：决定冻哪部分、给哪部分挂 LoRA |
| `embedding` | （保留字段，供 `all-embedding` 等扩展使用） | 定位词表嵌入 |

记住这张表，就能准确判断「改 `ModelKeys` 的哪个字段会影响什么行为」。

#### 4.4.4 代码实践：对比文本与多模态在 `all-linear` 下的命中范围

1. **实践目标**：亲手验证「文本模型的命中范围由扫描决定、多模态由分层决定」，并理解 `lm_head` 字段的排除作用。
2. **操作步骤**：
   - 步骤 a：阅读 [swift/utils/transformers_utils.py:133-164](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/transformers_utils.py#L133-L164) 的 `find_layers`，理解它如何把「`name` 里的数字 `\d+` 归一成 `{}`」来推导跨层通用的叶子模块名。
   - 步骤 b：准备一个可下载的小文本模型（如 `Qwen/Qwen3-0.6B`）与一个小多模态模型（如 `Qwen/Qwen2-VL-2B-Instruct`），用下面的示例代码加载后查看命中范围（**需要联网下载权重，待本地验证**）。

```python
# 示例代码（需要能下载权重）
from swift.model import get_model_processor
from swift.utils import find_all_linears, get_multimodal_target_regex

# 文本模型：直接扫描，看命中了哪些叶子模块名
args_text = dict(model='Qwen/Qwen3-0.6B', model_type='qwen3', full_precision=False, torch_dtype='float16')
model_text, _ = get_model_processor(**{k: v for k, v in args_text.items() if k != 'full_precision'})
print('文本 all-linear 命中:', sorted(set(find_all_linears(model_text))))
print('其 lm_head 字段:', model_text.model_meta.model_arch.lm_head)

# 多模态模型：默认只挂 LLM
args_mm = dict(model='Qwen/Qwen2-VL-2B-Instruct', model_type='qwen2_vl', torch_dtype='float16')
model_mm, _ = get_model_processor(**{k: v for k, v in args_mm.items()})
print('多模态默认 target_regex:')
print(get_multimodal_target_regex(model_mm, freeze_llm=False, freeze_vit=True, freeze_aligner=True))
```

3. **需要观察的现象**：
   - 文本模型的命中列表应包含 `q_proj/k_proj/v_proj/o_proj/gate_proj/up_proj/down_proj` 这类叶子名（不带层号），且**不含** `lm_head`。
   - 多模态默认正则应只覆盖 `language_model` 路径（如 `model.language_model`、`lm_head` 命名的子树），不覆盖 `model.visual`。
4. **预期结果**：与上面「路径 1 / 路径 2」的分析一致。若把 `freeze_vit=False` 再跑一次，正则应额外包含 `model.visual` 相关分支。
5. ⚠️ `get_model_processor` 的确切参数签名与可下载的最小模型 id 可能随版本变化；若无法运行，可退化为「源码阅读型实践」：直接对照 [swift/utils/transformers_utils.py:178-205](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/transformers_utils.py#L178-L205) 与 [swift/utils/transformers_utils.py:208-255](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/transformers_utils.py#L208-L255) 手动推演。

#### 4.4.5 小练习与答案

**练习 1**：某纯文本模型的 `ModelKeys` 把 `lm_head` 字段填成了 `'output'`（如 InternLM2）。这会如何改变 `all-linear` 的命中范围？

> **参考答案**：`find_all_linears` 会把 `ignore_layers` 里的 `lm_head` 替换成 `output`，于是扫描时排除的是名为 `output` 的层（InternLM2 的真实输出头），而不是去排除一个根本不存在的 `lm_head`。若不填对，输出头会被误当作普通线性层挂上 LoRA，导致训练异常。这正是 `lm_head` 字段对 `all-linear` 的真实影响。

**练习 2**：在多模态 LoRA 训练里，默认 `freeze_vit=True`、`freeze_aligner=True`。如果你希望「给视觉塔也加 LoRA」，应该改命令行参数还是改 `ModelKeys`？

> **参考答案**：改命令行参数 `--freeze_vit false` 即可。`get_multimodal_target_regex` 会把 `vision_tower` 路径加入候选，并在视觉塔内部扫描线性层挂 LoRA。`ModelKeys` 本身不需要改——它只是「边界表」，加不挂 LoRA 是由 freeze 开关在运行时决定的。只有当你想「只给视觉塔的某一部分加 LoRA」（例如只 `visual` 不动 `audio_tower`）这种表内细分时，才需要改 `MultiModelKeys` 的字段。

## 5. 综合实践

把本讲全部知识串成一个端到端的小任务：**为一个假想的多模态模型补全架构注册，并预测三种训练配置下的参数行为。**

背景：假设有一个新模型族 `my_model`，结构为：语言模型在顶层 `model.lang`，输出头 `lm_head`，视觉塔 `model.vision`，对齐器 `model.projector`。

1. **任务 1（写注册表）**：参考 [swift/model/model_arch.py:575-582](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/model_arch.py#L575-L582)，为 `my_model` 写一份 `MultiModelKeys` 注册。
2. **任务 2（预测全参数训练行为）**：对照 [swift/arguments/tuner_args.py:204-220](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/tuner_args.py#L204-L220)，写出在 `freeze_llm=False, freeze_vit=True, freeze_aligner=True` 时，`freeze_parameters` 与 `trainable_parameters` 分别会是哪些路径。
3. **任务 3（预测 LoRA 行为）**：对照 [swift/utils/transformers_utils.py:208-255](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/transformers_utils.py#L208-L255)，写出此时 `get_multimodal_target_regex` 返回的正则会覆盖哪些子树、不覆盖哪些。
4. **任务 4（验证）**：把任务 1 的注册代码放进一个临时脚本（`import` 后），用 4.2.4 与 4.3.4 的小脚本打印其 `model_arch` 与分层字段，确认与你手写的一致；再用 4.3.4 的 `which_parts` 函数核对任务 2/3 的预测。

> 这个任务覆盖了本讲全部三个最小模块：`ModelKeys`/`MultiModelKeys` 数据结构（任务 1）、`MODEL_ARCH_MAPPING` 注册与查找（任务 1+4）、分层控制对冻结与 `all-linear` 命中范围的影响（任务 2+3）。

## 6. 本讲小结

- `ModelArch` 只是「模型族名常量表」；真正描述模型结构的是 `ModelKeys`——一份「字段名 → 带 `{}` 层号占位符的点分路径」的解剖表。
- `MultiModelKeys` 继承 `ModelKeys`，新增 `language_model`/`vision_tower`/`aligner`/`generator` 四个分层字段，`__post_init__` 把 `str`/`None` 归一化成 `list`，供下游无差别遍历。
- 注册侧有一个关键替换：`register_model` 把 `ModelMeta.model_arch` 从字符串（`arch_name`）通过 `get_model_arch` 解析成真实的 `ModelKeys` 对象，这才让后续代码能 `.lm_head`、`.language_model` 地访问。
- 直接消费 `ModelKeys` 线性层字段（`module_list`/`o_proj`/`down_proj`/`mlp` 等）的是 llamapro、adapter、ia3 这类需要精确解剖结构的 tuner。
- `--target_modules all-linear` 分两条路：文本模型走 `find_all_linears` 扫描模型实例，只读 `ModelKeys.lm_head` 决定排除哪个输出头；多模态模型走 `get_multimodal_target_regex`，由 `MultiModelKeys` 的分层字段配合 freeze 开关决定 LoRA 挂在哪个子模型上。
- 因此「`ModelKeys` 如何影响 LoRA 命中范围」要分场景回答：文本模型只影响 `lm_head` 的排除；多模态模型则直接决定「训 LLM / 训 ViT / 训 aligner」的边界。

## 7. 下一步学习建议

- 本讲把「模型结构如何被描述」讲透了，但训练时样本如何变成 token、对话格式如何拼接，是另一套体系。建议下一讲进入 **[u3-l3 Template 体系与对话格式](u3-l3-template-and-chat-format.md)**，看 `ModelMeta.template`（在 u3-l1 里提到过它和 `model_arch` 同为 ModelMeta 字段）是如何被模板系统读取并驱动 `encode` 的。
- 如果你对「 tuner 如何挂载可训练参数」更感兴趣，可以先跳到 u5 单元，尤其是 **u5-l2 TunerPlugin 与模型适配** 和 **u5-l3 LoRA 与轻量微调方法**，那里会把本讲提到的 llamapro/adapter/ia3 与 `target_modules` 的展开再放到训练主流程里完整走一遍。
- 想做二次开发的读者，可以把 [examples/custom/my_qwen2_5_omni/my_register.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/custom/my_qwen2_5_omni/my_register.py) 当作起点，亲手注册一个自定义多模态模型并验证分层冻结行为。
