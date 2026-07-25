# 自动发现与模型注册

## 1. 本讲目标

在 u5-l1 中我们看到：写一个新模型，几乎全部工作量都集中在 `Model`（骨干）与 `DecoderLayer`（单层）两个类，外壳 `DecoderModelForCausalLM` 几乎零成本。但这里留下一个关键问题——

> 用户给 `LLM(model="./deepseek-v3")` 一个目录路径，TensorRT-LLM 是怎么知道该用哪一个 `ForCausalLM` 类来构造模型的？

本讲就回答这个问题。读完本讲，你应当能够：

1. 说清楚「一个 HF checkpoint 目录」是怎么变成「一个可前向的 Python 模型对象」的，重点是中间那一步「按 `architectures` 字段查表」。
2. 区分仓库里**两套同名但不同命**的注册表：顶层 `MODEL_MAP`（遗留引擎流，在 PyTorch 后端里是空的）与 `_torch` 后端的 `MODEL_CLASS_MAPPING`（PyTorch 后端真正在用的那一张表）。
3. 理解模型类如何**自注册**——只需一个 `@register_auto_model("XxxForCausalLM")` 装饰器，就能把自己写进注册表，且支持「一个类注册多个架构别名」与「完全不动 TensorRT-LLM 源码的 out-of-tree 注册」。

本讲是 u5-l1 的直接续篇：u5-l1 讲「模型长什么样」，本讲讲「模型怎么被找出来并实例化」。

## 2. 前置知识

- **HF `config.json` 里的 `architectures` 字段**。每个 HuggingFace checkpoint 根目录都有一个 `config.json`，其中 `architectures` 是一个字符串列表，例如 `"architectures": ["DeepseekV3ForCausalLM"]`。它声明「这个权重对应哪个模型类」。HF 的 `transformers.AutoConfig.from_pretrained()` 会把它读成 `hf_config.architectures`。本讲全靠这个字符串做「查表」的钥匙。
- **装饰器即注册**。Python 里 `@decorator(arg)` 在模块被 import 时就会执行。TensorRT-LLM 利用这一点：模型类被 import 的瞬间，装饰器就把「架构名 → 类」写进一张全局字典。所以「注册」不需要显式调用任何函数，import 即注册。
- **`ModelConfig` 包裹 `pretrained_config`**（u4-l3）。运行时的 `ModelConfig` 把 HF 的 `pretrained_config`（含 `architectures`）和并行拓扑、量化、注意力后端等部署期开关打包在一起。本讲的查表函数读的就是 `config.pretrained_config.architectures[0]`。
- **「Python 调度、C++ 加速」**（u2-l3）。模型类的解析与实例化完全是 Python 侧的编排逻辑，不涉及 C++ kernel。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `tensorrt_llm/models/automodel.py` | **顶层**的 `AutoConfig` / `AutoModelForCausalLM`，走遗留引擎流，依赖 `MODEL_MAP`（在 PyTorch 后端为空） |
| `tensorrt_llm/models/__init__.py` | 定义顶层 `MODEL_MAP = {}`，并明确注释「PyTorch 后端经由 `_torch.models` 解析」 |
| `tensorrt_llm/_torch/models/modeling_utils.py` | 定义 PyTorch 后端真正在用的 `MODEL_CLASS_MAPPING`，以及 `@register_auto_model`、`get_model_architecture` |
| `tensorrt_llm/_torch/models/modeling_auto.py` | PyTorch 后端的 `AutoModelForCausalLM`，核心方法 `_resolve_class` / `from_config` |
| `tensorrt_llm/_torch/models/__init__.py` | import 所有 `modeling_*.py`，**触发装饰器注册** |
| `tensorrt_llm/_torch/models/modeling_deepseekv3.py` | 自注册示例：一个类挂三个架构别名 |
| `tensorrt_llm/_torch/pyexecutor/model_loader.py` | 调用方：用 `_resolve_class` / `from_config` 把 checkpoint 变成模型对象 |
| `examples/llm-api/out_of_tree_example/modeling_opt.py` | out-of-tree 自注册示例 |

## 4. 核心概念与源码讲解

### 4.1 AutoConfig / AutoModel：从 HF architectures 到模型类

#### 4.1.1 概念说明

「自动发现」要解决的问题很朴素：用户只给一个目录，程序必须自己读出 `config.json`，取出 `architectures[0]` 这把钥匙，然后在一张「架构名 → 模型类」的表里查到对应的 Python 类，最后实例化它。

TensorRT-LLM 把这件事封装成一对和 HF 同名的工具类：`AutoConfig` 与 `AutoModelForCausalLM`。前者负责「读 HF 配置 → 翻译成 TRT-LLM 配置」，后者负责「读 HF 配置 → 实例化 TRT-LLM 模型」。注意：**仓库里有两个同名 `AutoModelForCausalLM`**（一个在顶层 `models/`，一个在 `_torch/models/`），它们查的不是同一张表——这是本讲最容易踩的坑，4.2 节会专门拆开。

#### 4.1.2 核心流程

顶层 `AutoModelForCausalLM.get_trtllm_model_class` 的解析过程可以概括为：

```
HF 目录
  └─ transformers.AutoConfig.from_pretrained(...)   读 config.json
        └─ hf_config.architectures[0]               取第一把钥匙（特殊情形：mamba 退回 'MambaForCausalLM'）
              └─ MODEL_MAP.get(hf_arch)             查顶层注册表
                    └─ 命中 → 返回 TRT-LLM 模型类
                       未命中 → raise NotImplementedError
```

三条要点：

1. 钥匙取 `architectures[0]`（列表第一项）；若配置里没有 `architectures` 但 `model_type` 含 `mamba`，则退回硬编码的 `'MambaForCausalLM'`。
2. 查的是**顶层 `MODEL_MAP`**。
3. 查不到直接抛 `NotImplementedError`，不会静默退回。

#### 4.1.3 源码精读

顶层 `AutoModelForCausalLM.get_trtllm_model_class` 完整实现了上面这个流程：

[automodel.py:54-79](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/models/automodel.py#L54-L79) —— 读 HF 配置、取 `architectures[0]`、在 `MODEL_MAP` 里查类，查不到抛错。

关键的两行是「取钥匙」和「查表」：

```python
hf_arch = hf_config.architectures[0]            # 取钥匙
trtllm_model_cls = MODEL_MAP.get(hf_arch, None) # 查顶层注册表
```

[automodel.py:64-65](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/models/automodel.py#L64-L65) 先用 HF 的 `AutoConfig` 把目录变成配置对象；[automodel.py:73](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/models/automodel.py#L73) 用 `architectures[0]` 在 `MODEL_MAP` 查 TRT-LLM 模型类。

`AutoConfig.from_hugging_face` 走的是同一条钥匙链，但它查到类之后还会进一步要 `trtllm_model_cls.config_class.from_hugging_face(...)` 来产出配置对象，见 [automodel.py:30-48](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/models/automodel.py#L30-L48)。

需要特别提醒：这个顶层 `AutoModelForCausalLM` 被 `tensorrt_llm/__init__.py` 直接 re-export：

[__init__.py:124](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/__init__.py#L124) —— `from .models.automodel import AutoConfig, AutoModelForCausalLM`。

也就是说，`import tensorrt_llm` 后你拿到的 `tensorrt_llm.AutoModelForCausalLM` 是**顶层那个、查 `MODEL_MAP` 的版本**，而不是 PyTorch 后端在用的那个。这正是 4.2 节要澄清的歧义。

#### 4.1.4 代码实践

**实践目标**：亲手验证「取钥匙 + 查表」这两步，并观察顶层注册表在 PyTorch 后端下的行为。

**操作步骤**：

1. 任选一个本地 HF checkpoint 目录（或用 transformers 在线拉一个小模型），确认其 `config.json` 里有 `architectures` 字段。
2. 写一段脚本，模拟 `get_trtllm_model_class` 的前两步：

```python
# 示例代码（非项目原有代码）
import transformers
from tensorrt_llm.models import MODEL_MAP

hf_config = transformers.AutoConfig.from_pretrained(
    "<你的模型目录>", trust_remote_code=False)
hf_arch = hf_config.architectures[0]
print("钥匙 architectures[0] =", hf_arch)
print("顶层 MODEL_MAP 内容 =", MODEL_MAP)
print("查表结果 =", MODEL_MAP.get(hf_arch))
```

**需要观察的现象**：

- `hf_arch` 应当是类似 `LlamaForCausalLM`、`DeepseekV3ForCausalLM` 这样的字符串。
- `MODEL_MAP` 应当是**空字典 `{}`**（原因见 4.2 节）。
- 因此 `MODEL_MAP.get(hf_arch)` 为 `None`——这正是顶层 `AutoModelForCausalLM` 在 PyTorch 后端下「不可直接用于实例化模型」的直接证据。

**预期结果**：钥匙取得到，但顶层表是空的，查表返回 `None`。这把你推向 4.3 节的 `_torch` 后端注册表。如果手头没有可用模型，可标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `get_trtllm_model_class` 用 `architectures[0]` 而不是 `architectures` 整个列表？如果某个 checkpoint 的 `architectures` 是 `["A", "B"]`，会发生什么？

**参考答案**：注册表的键是单个字符串，所以只取第一个架构名作为钥匙。若列表是 `["A", "B"]`，只会用 `"A"` 查表，`"B"` 被忽略；若想被识别，`"A"` 必须出现在注册表里（或模型类用多个 `@register_auto_model` 把 `"A"` 注册进去）。

**练习 2**：`AutoConfig.from_hugging_face` 查到 `trtllm_model_cls` 后，还额外要求这个类具备哪两个属性/方法？

**参考答案**：要求 `trtllm_model_cls.config_class` 存在，且该 `config_class` 具备 `from_hugging_face` 方法（见 [automodel.py:36-45](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/models/automodel.py#L36-L45)）。缺任一个都会抛 `NotImplementedError`。

---

### 4.2 两套注册表：MODEL_MAP 与 MODEL_CLASS_MAPPING

#### 4.2.1 概念说明

TensorRT-LLM 历史上有两条执行路径：旧的引擎构建流（`trtllm-build` → engine）与新的 PyTorch 原生后端。两条路径各自需要一张「架构名 → 模型类」的表，于是仓库里出现了**两个同名的 `AutoModelForCausalLM`**，分别查两张不同的表：

| 注册表 | 定义位置 | 配套的 AutoModelForCausalLM | 在 PyTorch 后端的状态 |
|--------|----------|------------------------------|------------------------|
| `MODEL_MAP` | `tensorrt_llm/models/__init__.py` | `tensorrt_llm/models/automodel.py` | **空 `{}`，已退役** |
| `MODEL_CLASS_MAPPING` | `tensorrt_llm/_torch/models/modeling_utils.py` | `tensorrt_llm/_torch/models/modeling_auto.py` | 真正在用，由装饰器填充 |

记住一句话：**顶层 `MODEL_MAP` 是遗留产物，PyTorch 后端真正查的是 `_torch` 里的 `MODEL_CLASS_MAPPING`。** 顶层那张表被刻意留空，并在源码里写了一段明确注释说明这件事。

#### 4.2.2 核心流程

PyTorch 后端的解析路径与 4.1 节的顶层路径**形似而神不同**：

```
HF 目录
  └─ checkpoint_loader.load_config(...)            读 config.json → ModelConfig（包裹 HF pretrained_config）
        └─ AutoModelForCausalLM._resolve_class(config)
              ├─ 取 config.pretrained_config.architectures[0]
              ├─ 特殊情形重写（vision encoder / EAGLE3 / MTP）
              └─ MODEL_CLASS_MAPPING.get(model_arch)   ← 查的是这张表
                    └─ from_config(config) → cls(config) 实例化
```

它与顶层路径的两点关键差别：

1. **钥匙来源不同**：顶层直接用 `transformers.AutoConfig`；PyTorch 后端用 `ModelConfig.pretrained_config.architectures[0]`（`ModelConfig` 还额外携带并行、量化等运行时开关，见 u4-l3）。
2. **查的表不同**：顶层查空的 `MODEL_MAP`；PyTorch 后端查被装饰器填满的 `MODEL_CLASS_MAPPING`。

#### 4.2.3 源码精读

先看顶层 `MODEL_MAP` 为什么是空的——源码注释讲得一清二楚：

[__init__.py:19-21](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/models/__init__.py#L19-L21) —— 注释「Architecture registry is intentionally empty: the PyTorch backend resolves model classes via tensorrt_llm._torch.models.」紧接着 `MODEL_MAP = {}`。

```python
# Architecture registry is intentionally empty: the PyTorch backend resolves
# model classes via tensorrt_llm._torch.models.
MODEL_MAP = {}
```

再看 PyTorch 后端那张真正在用的表，以及它的查询入口 `get_model_architecture`：

[modeling_utils.py:814-815](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_utils.py#L814-L815) 定义 `MODEL_CLASS_MAPPING = {}`（同处还定义了视觉编码器、权重加载器等若干并列注册表）。

[modeling_utils.py:924-941](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_utils.py#L924-L941) 是 `get_model_architecture`，它取 `architectures[0]` 在 `MODEL_CLASS_MAPPING` 查类，并对未识别的 Gemma4 架构给出「请升级 transformers」的友好提示：

```python
def get_model_architecture(model_config):
    cls = None
    if model_config.architectures is not None and len(model_config.architectures) > 0:
        cls = MODEL_CLASS_MAPPING.get(model_config.architectures[0])
    ...
    if cls is None:
        arch = model_config.architectures[0]
        if arch in _GEMMA4_ARCHITECTURES:
            raise RuntimeError("Gemma4 model support requires transformers>=5.5.0 ...")
        raise RuntimeError(f"Unknown model architecture: {arch}")
    return cls, model_config.architectures[0]
```

另一条更常用的查询入口是 `_torch` 后端 `AutoModelForCausalLM._resolve_class`，它比 `get_model_architecture` 多做了几步「架构名重写」（见 4.3 节），最终同样落到 `MODEL_CLASS_MAPPING.get(model_arch)`：

[modeling_auto.py:43](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_auto.py#L43) —— `return MODEL_CLASS_MAPPING.get(model_arch)`。

调用方在 `model_loader.py`：先 `_resolve_class` 拿到类（顺带取模型默认值），再 `from_config` 实例化：

[model_loader.py:393](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/model_loader.py#L393) 用 `_resolve_class` 解析类并取 `get_model_defaults`；[model_loader.py:501](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/model_loader.py#L501) 与 [model_loader.py:508](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/model_loader.py#L508) 分别在 `MetaInitMode` 与回退路径里调用 `from_config` 构造模型。

#### 4.2.4 代码实践

**实践目标**：用源码阅读的方式确认「两套表、两个同名类」这一事实，不再混淆。

**操作步骤**：

1. 在 `tensorrt_llm/models/__init__.py` 确认 `MODEL_MAP = {}`（4.2.3 已给链接）。
2. 在 `tensorrt_llm/_torch/models/modeling_utils.py` 确认 `MODEL_CLASS_MAPPING` 的定义，并统计它被多少个 `modeling_*.py` 用 `@register_auto_model` 填充（可用下文命令）。
3. 对比两个 `AutoModelForCausalLM` 的 import 路径：
   - 顶层：`from tensorrt_llm.models.automodel import AutoModelForCausalLM`（查 `MODEL_MAP`）
   - 后端：`from tensorrt_llm._torch.models import AutoModelForCausalLM`（查 `MODEL_CLASS_MAPPING`）

**需要观察的现象**：`modeling_utils.py` 里 `MODEL_CLASS_MAPPING` 本身只是一个空字典字面量，它的内容完全由散布在几十个 `modeling_*.py` 里的 `@register_auto_model` 装饰器在 import 时填充。可用如下命令统计注册点数量：

```bash
# 统计 PyTorch 后端里有多少处 @register_auto_model
grep -rn "@register_auto_model" tensorrt_llm/_torch/models/ | wc -l
```

**预期结果**：得到一个远大于 0 的数字（数十处），印证「PyTorch 后端表是被装饰器集体填满的」，而顶层 `MODEL_MAP` 是空。若环境无法运行 grep，可标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果一个新手写了 `from tensorrt_llm import AutoModelForCausalLM` 然后调用它的 `get_trtllm_model_class`，会发生什么？为什么？

**参考答案**：他拿到的是顶层那个查 `MODEL_MAP` 的版本，而 `MODEL_MAP` 在 PyTorch 后端是空的，所以任何架构都会查到 `None` 并抛 `NotImplementedError`。正确做法是走 `_torch` 后端的 `AutoModelForCausalLM._resolve_class` / `from_config`（这也是 `model_loader.py` 实际调用的路径）。

**练习 2**：`get_model_architecture` 和 `_resolve_class` 都查 `MODEL_CLASS_MAPPING`，它们的主要差别是什么？

**参考答案**：`get_model_architecture` 是「纯查表 + 友好报错」；`_resolve_class` 在查表之前会先做架构名重写（多模态视觉编码器、EAGLE3 草稿、MTP 草稿等特殊情形），并且返回 `Optional[Type]`（查不到返回 `None` 而非直接抛错），便于调用方在 `None` 时跳过模型默认值合并等后续步骤。

---

### 4.3 MODEL_CLASS_MAPPING 与 @register_auto_model 自注册

#### 4.3.1 概念说明

`MODEL_CLASS_MAPPING` 是 PyTorch 后端的核心注册表。它的填充方式非常优雅——**没有任何中央化清单**，而是靠每个模型文件里的一个装饰器在 import 时「自注册」。

这个设计带来三个直接好处：

1. **加新模型零侵入**：在新文件里写一个装饰器就完成注册，不必维护一张统一的大表。
2. **一个类可注册多个架构别名**：同一份实现可以服务多个名字相近的 checkpoint（例如 DeepSeek-V3 与 V3.2 共用一套实现）。
3. **支持 out-of-tree 注册**：用户在自己机器上写一个 `modeling_xxx.py` 并 `import` 它，就能让 `LLM(...)` 识别自己的私有模型，完全不用改 TensorRT-LLM 源码。

#### 4.3.2 核心流程

注册与解析合起来构成一个闭环：

```
【注册阶段——import 时发生】
  _torch/models/__init__.py
    └─ from .modeling_xxx import XxxForCausalLM     # import 触发装饰器
          └─ @register_auto_model("XxxForCausalLM")
                └─ MODEL_CLASS_MAPPING["XxxForCausalLM"] = XxxForCausalLM

【解析阶段——加载模型时发生】
  model_loader.load(checkpoint_dir)
    └─ _resolve_class(config)
          ├─ 特殊重写 architectures[0]
          ├─ MODEL_CLASS_MAPPING.get(arch)          ← 命中上面注册的类
          └─ from_config(config) → cls(config)      ← 实例化
```

这里有几个**架构名重写**的特殊情形，它们让同一份 checkpoint 在不同部署目标下被解析成不同的执行类：

- **多模态视觉编码器**：`config.mm_encoder_only` 为真时，改查 `MODEL_CLASS_VISION_ENCODER_MAPPING`。
- **EAGLE3 草稿检测**：若 `pretrained_config` 带 `draft_vocab_size`，把架构名里的 `Eagle3` 去掉再前缀 `EAGLE3`，定位到 TRT-LLM 自己的草稿模型类。
- **MTP 草稿**：当架构是 `DeepseekV3ForCausalLM` / `Glm4MoeForCausalLM` / `ExaoneMoEForCausalLM` 且 `spec_config.max_draft_len == 0` 时，改写成 `MTPDraftModelForCausalLM`。

这三条重写解释了一句反直觉的话：**最终实例化的类，未必等于 `architectures[0]` 直接查到的那一个**。

#### 4.3.3 源码精读

`@register_auto_model` 是一个标准的「带参装饰器工厂」，它把「架构名 → 类」写进全局字典：

[modeling_utils.py:822-828](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_utils.py#L822-L828) —— 装饰器定义：

```python
def register_auto_model(name: str):
    def decorator(cls):
        MODEL_CLASS_MAPPING[name] = cls
        return cls
    return decorator
```

它返回原类不变（`return cls`），所以装饰器唯一的副作用就是「往表里写一条」。正因为无副作用地返回原类，装饰器可以**叠加**——一个类挂多个 `@register_auto_model` 就注册了多个别名。DeepSeek-V3 是最典型的例子，同一个类注册了三个架构名：

[modeling_deepseekv3.py:1890-1893](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_deepseekv3.py#L1890-L1893)：

```python
@register_auto_model("GlmMoeDsaForCausalLM")
@register_auto_model("DeepseekV32ForCausalLM")
@register_auto_model("DeepseekV3ForCausalLM")
class DeepseekV3ForCausalLM(SpecDecOneEngineForCausalLM[DeepseekV3Model, PretrainedConfig]):
```

装饰器自下而上执行，最终 `MODEL_CLASS_MAPPING` 里会同时出现这三个键，全部指向同一个 `DeepseekV3ForCausalLM` 类——这就是「GLM-5 家族（`GlmMoeDsaForCausalLM`）与 DeepSeek-V3/V3.2 共用一套实现」的来源。

注册发生在何时？答案是「`_torch/models/__init__.py` 被 import 时」。该文件用一长串 `from .modeling_xxx import ...` 把所有模型文件拉进来，每条 import 都触发对应文件里的装饰器：

[__init__.py:9-17](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/__init__.py#L9-L17)（节选）：

```python
from .modeling_deepseekv3 import DeepseekV3ForCausalLM
from .modeling_deepseekv4 import DeepseekV4ForCausalLM
...
```

这就形成了一条因果链：**只要 `tensorrt_llm._torch.models` 被 import 过一次，`MODEL_CLASS_MAPPING` 就被填满**。out-of-tree 模型之所以「`import modeling_mymodel` 就能用」，靠的也是同一条机制——用户的 import 同样会触发他自己文件里的装饰器。

解析侧的入口 `_resolve_class` 把前面说的特殊重写都集中在一处：

[modeling_auto.py:12-43](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_auto.py#L12-L43)：

```python
@staticmethod
def _resolve_class(config: ModelConfig) -> Optional[Type]:
    pretrained_config = config.pretrained_config
    if not pretrained_config.architectures:
        return None
    model_arch = pretrained_config.architectures[0]

    if config.mm_encoder_only:                       # 多模态视觉编码器
        ...
    if hasattr(pretrained_config, "draft_vocab_size"):  # EAGLE3 草稿
        model_arch = "EAGLE3" + model_arch.replace("Eagle3", "")
    if model_arch in ("DeepseekV3ForCausalLM", "Glm4MoeForCausalLM",
                      "ExaoneMoEForCausalLM") and config.spec_config is not None \
            and config.spec_config.max_draft_len == 0:  # MTP 草稿
        model_arch = "MTPDraftModelForCausalLM"

    return MODEL_CLASS_MAPPING.get(model_arch)
```

最后是实例化入口 `from_config`，它在拿到类之后做两件小事：若类是 `DecoderModelForCausalLM` 的子类，就置 `skip_create_weights_in_init=True`（配合 meta 设备省显存，见 u5-l1），再用 `model_extra_attrs` 上下文把额外属性挂上去，然后 `cls(config)`：

[modeling_auto.py:59-72](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_auto.py#L59-L72)。

#### 4.3.4 代码实践

**实践目标**：以 DeepSeek-V3 为例，完整追踪「HF `architectures` 字段 → 最终模型类」的解析路径，并说明两套注册表分别在哪一步起作用（这是本讲的核心练习）。

**操作步骤**：

1. **取钥匙**。找一个 DeepSeek-V3 的 `config.json`（可在 [HuggingFace](https://huggingface.co/) 上查看 deepseek-ai/DeepSeek-V3 仓库的 `config.json`），确认其包含 `"architectures": ["DeepseekV3ForCausalLM"]`。
2. **走顶层路径（演示它在此失效）**。对照 [automodel.py:64-79](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/models/automodel.py#L64-L79)：它会用 `DeepseekV3ForCausalLM` 去查顶层 `MODEL_MAP`，而那张表是空的（[__init__.py:19-21](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/models/__init__.py#L19-L21)），于是返回 `None`。结论：**顶层 `MODEL_MAP` 在 PyTorch 后端不起作用**。
3. **走 PyTorch 后端路径（真正生效）**。对照 [modeling_auto.py:12-43](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_auto.py#L12-L43)：
   - 钥匙仍是 `DeepseekV3ForCausalLM`（`pretrained_config.architectures[0]`）。
   - 本例不开启投机解码，故 MTP 重写不触发，`model_arch` 保持 `DeepseekV3ForCausalLM`。
   - `MODEL_CLASS_MAPPING.get("DeepseekV3ForCausalLM")` 命中，返回 [modeling_deepseekv3.py:1893](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_deepseekv3.py#L1893) 的 `DeepseekV3ForCausalLM` 类——这个键正是同一个文件里 [modeling_deepseekv3.py:1892](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_deepseekv3.py#L1892) 的 `@register_auto_model("DeepseekV3ForCausalLM")` 在 import 时写进去的。
4. **验证多别名**。由于该类还挂了 `DeepseekV32ForCausalLM` 与 `GlmMoeDsaForCausalLM` 两个别名（[modeling_deepseekv3.py:1890-1891](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_deepseekv3.py#L1890-L1891)），可推论：DeepSeek-V3.2 与 GLM-5 家族（同样声明 `GlmMoeDsaForCausalLM`）的 checkpoint 也会被解析到这同一个类。

**需要观察的现象**：

- 顶层 `MODEL_MAP` 全程不参与 DeepSeek-V3 的解析。
- PyTorch 后端的 `MODEL_CLASS_MAPPING` 在 `_resolve_class` 这一步起决定作用。
- 最终类由 `@register_auto_model` 装饰器在 import 时埋下的键决定。

**预期结果**：写出一张时序/路径图，标注「钥匙 = `DeepseekV3ForCausalLM`」「顶层表空→不生效」「后端表命中→`DeepseekV3ForCausalLM` 类（同一文件装饰器写入）」三个关键事实。

**延伸（可选）**：阅读 out-of-tree 示例 [modeling_opt.py:228](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/llm-api/out_of_tree_example/modeling_opt.py#L228) 的 `@register_auto_model("OPTForCausalLM")`，确认它用的装饰器与 in-tree 模型完全相同——这正是「out-of-tree 不需要任何特殊机制，import 即注册」的由来。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `@register_auto_model` 必须返回原类 `cls`？如果它返回 `None` 会出什么问题？

**参考答案**：因为装饰器要把被装饰的类正常绑定到模块命名空间（`class XxxForCausalLM` 这个名字要指向类本身）。若返回 `None`，`XxxForCausalLM` 就变成 `None`，后续 `from .modeling_xxx import XxxForCausalLM` 会 import 到 `None`，构造与继承全部失败。返回原类才能让「注册」成为一个纯粹的副作用，不影响正常的类定义语义。

**练习 2**：DeepSeek-V3 的类同时注册了 `DeepseekV3ForCausalLM`、`DeepseekV32ForCausalLM`、`GlmMoeDsaForCausalLM` 三个别名。请说明这种「一个类多别名」的设计相比「每个架构各写一个类」有什么好处，又可能带来什么风险。

**参考答案**：好处是复用——三套权重大同小异，共用一份实现避免代码重复，且新版本（V3.2）或同族模型（GLM-5）能零成本接入。风险是「实现差异」会被掩盖：如果某个别名对应的 checkpoint 其实有细微结构差别，共用实现就需要在类内部用 `pretrained_config.architectures` 等字段做分支判断（本讲 4.1.3 提到的 `get_preferred_transceiver_runtime` 正是按 checkpoint 区分 GLM 与 DeepSeek 的例子），分支多了会降低可读性、增加维护成本。

**练习 3**：`_resolve_class` 在哪些情况下会返回 `None`？返回 `None` 后 `model_loader` 还能继续工作吗？

**参考答案**：当 `pretrained_config.architectures` 为空，或架构名重写后在 `MODEL_CLASS_MAPPING` 里查不到时，返回 `None`。`model_loader` 对此有预案：`model_cls` 为 `None` 时只是跳过 `get_model_defaults` 等模型相关步骤（见 [model_loader.py:395-398](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/model_loader.py#L395-L398)），但真正调 `from_config` 时，[modeling_auto.py:60-63](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_auto.py#L60-L63) 会把 `None` 转成显式的 `ValueError`，所以最终仍会报错，只是报错时机稍晚。

## 5. 综合实践

把本讲的三条主线串起来，做一个「模型注册体检」小任务。

**任务**：选择仓库里任意一个已支持的模型（推荐 DeepSeek-V3 或 Llama），完成下面四件事，并整理成一份一页笔记。

1. **取钥匙**：写出该模型 HF `config.json` 里 `architectures[0]` 的值。
2. **顶层表体检**：打开 [tensorrt_llm/models/__init__.py:19-21](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/models/__init__.py#L19-L21)，确认 `MODEL_MAP` 为空；据此判断「顶层 `AutoModelForCausalLM` 能否单独完成该模型的实例化」，并说明理由。
3. **后端表体检**：在 `tensorrt_llm/_torch/models/` 下找到该模型的 `modeling_*.py`，定位其 `@register_auto_model(...)` 装饰器，确认它注册的架构名与第 1 步的钥匙一致（或属于同一类的别名）。再确认该文件被 [tensorrt_llm/_torch/models/__init__.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/__init__.py#L9-L66) import，从而保证注册一定发生过。
4. **画一张解析路径图**：从「`LLM(model=<目录>)`」出发，画出经过 `model_loader.load_config` → `_resolve_class`（标注是否触发架构名重写）→ `MODEL_CLASS_MAPPING` 命中 → `from_config` → `cls(config)` 的完整路径，并在图上用不同颜色区分「顶层 `MODEL_MAP`（不参与）」与「后端 `MODEL_CLASS_MAPPING`（真正生效）」。

**验收标准**：笔记里能清楚回答「两套注册表分别在哪一步起作用」——顶层那套在 PyTorch 后端不起作用（表为空），后端那套在 `_resolve_class` 这一步决定最终类。如果你还想动手验证 out-of-tree，可以参考 `examples/llm-api/out_of_tree_example/`：把它复制到一个独立目录，`import modeling_opt` 后用 `tensorrt_llm._torch.models.modeling_utils.MODEL_CLASS_MAPPING` 查看 `OPTForCausalLM` 是否已被注入。

## 6. 本讲小结

- **自动发现的本质是「用 `architectures[0]` 当钥匙查表」**：HF `config.json` 的 `architectures` 字段是唯一的钥匙来源，查表得到 TRT-LLM 的模型类。
- **仓库有两套同名注册表**：顶层 `MODEL_MAP`（`tensorrt_llm/models/__init__.py`，在 PyTorch 后端刻意留空）与 PyTorch 后端的 `MODEL_CLASS_MAPPING`（`tensorrt_llm/_torch/models/modeling_utils.py`，真正在用）。
- **`import tensorrt_llm` 拿到的 `AutoModelForCausalLM` 是顶层那个**（查空的 `MODEL_MAP`）；PyTorch 后端实际用的是 `_torch/models/modeling_auto.py` 里那个（查 `MODEL_CLASS_MAPPING`），二者不可混用。
- **自注册靠 `@register_auto_model` 装饰器**：import 时执行，把「架构名 → 类」写进 `MODEL_CLASS_MAPPING`；装饰器可叠加，实现「一个类、多个架构别名」（如 DeepSeek-V3 同时服务 V3 / V3.2 / GLM-5）。
- **解析路径不是纯查表**：`_resolve_class` 在查表前会按多模态视觉编码器、EAGLE3 草稿、MTP 草稿等情形**重写架构名**，所以最终实例化的类未必等于 `architectures[0]` 直接查到的那一个。
- **out-of-tree 注册零特殊机制**：用户在自己脚本里 `import modeling_mymodel` 即可触发同一个装饰器，把自己的私有模型写进 `MODEL_CLASS_MAPPING`，无需改动 TensorRT-LLM 源码。

## 7. 下一步学习建议

- **学下一讲 u5-l3「添加一个新模型」**：本讲只讲了「已有模型怎么被找出来」，u5-l3 会把视角反过来——「我要新增一个模型，从配置、定义、权重加载到注册的完整 onboarding 流程」。你会亲手写一个 `@register_auto_model`，把本讲的自注册机制用起来。
- **回看 u4-l2「模型默认值与 llm_utils」**：本讲提到 `_resolve_class` 解析出类之后，`model_loader` 会调 `model_cls.get_model_defaults(llm_args)` 取模型默认值；这套默认值如何与用户参数深度合并，正是 u4-l2 的主题，两讲合起来就是「解析类 → 取默认值 → 合并 → 实例化」的完整加载前半段。
- **延伸阅读**：`tensorrt_llm/_torch/models/modeling_utils.py` 里 `MODEL_CLASS_MAPPING` 旁边还定义了 `MODEL_CLASS_VISION_ENCODER_MAPPING`、`MODEL_CLASS_MAPPER_MAPPING` 等若干并列注册表，以及配套的 `register_vision_encoder` / `register_mapper` 等装饰器（[modeling_utils.py:814-903](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_utils.py#L814-L903)）。理解了本讲的 `register_auto_model`，这些姊妹机制可以举一反三。
