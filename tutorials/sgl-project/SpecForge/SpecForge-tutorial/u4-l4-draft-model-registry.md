# 草稿模型注册表 modeling draft registry

## 1. 本讲目标

本讲承接 u4-l2（算法注册表）与 u4-l3（算法 providers）。前面三讲解决的都是「**算法这条轴**」：`training.strategy` 怎么从字符串解析成 `AlgorithmRegistration`，以及算法的契约与可执行端口如何绑定。

但一个算法要真正跑起来，还差最后一块拼图——**草稿模型本身是哪个 Python 类、怎么从一份 `config.json` 把它实例化出来**。这就是本讲要讲的「**草稿架构这条轴**」。

读完本讲你应该能够：

1. 说出 `DRAFT_REGISTRY` 的数据结构，以及 `@register_draft` 装饰器在注册时强制的两个不变量。
2. 解释为什么「草稿架构」与「算法注册」是**两条相互独立的轴线**：一个架构能服务于多个算法，一个算法也能换不同架构。
3. 读懂 `AutoDraftModel` / `AutoDraftModelConfig` 如何只凭 `config.json` 里的 `architectures` 字段，一次性解析出「模型类 + 配置类」。
4. 说出 `Eagle3DraftModel` 这个抽象基类规定子类必须实现的接口，以及它提供的共享工具方法。

---

## 2. 前置知识

本讲假设你已经读过：

- **u4-l1 算法契约**：知道 `AlgorithmSpec` 是纯数据契约，`DraftRequirement` 里有一个 `compatible_architectures` 字段，它**只放架构名字符串、不放模型类**。这一点是本讲「两条轴线解耦」的关键伏笔。
- **u4-l2 算法注册表**：知道 `AlgorithmRegistry` 用 `resolve(name)` 按名字查表，返回不可变的 `AlgorithmRegistration`。
- **u4-l3 算法 providers**：知道算法的 `providers` 端口里有一个 `DraftConfigProvider`，其中的 `architecture` 字段是一个字符串（例如 `"LlamaForCausalLMEagle3"`），声明「我这个算法期望构建哪个架构」。

如果你对 HuggingFace 的 `AutoModelForCausalLM` / `AutoConfig` 这套「按 `config.json` 自动选类」的机制有基本印象，本讲的 `AutoDraftModel` 会非常容易理解——它就是 SpecForge 自家版的同款机制。

两个关键术语先澄清：

- **草稿方法 / 算法（algorithm）**：一条训练策略，对应 `training.strategy` 的取值，如 `eagle3`、`dflash`。这是「怎么训」的轴。
- **草稿架构（architecture）**：一个具体的模型类，如 `LlamaForCausalLMEagle3`、`DFlashDraftModel`。这是「用什么模型」的轴。

这两件事在 SpecForge 里被刻意拆开，本讲的核心就是讲清楚「拆开」之后，连接它们的胶水是什么。

---

## 3. 本讲源码地图

本讲涉及三个核心源码文件，全部位于 `specforge/modeling/` 下：

| 文件 | 作用 |
| --- | --- |
| [specforge/modeling/draft/registry.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/registry.py) | 草稿架构注册表。提供 `DRAFT_REGISTRY` 字典、`@register_draft` 装饰器、`resolve_draft` 查表函数。是新增架构的**唯一扩展点**。 |
| [specforge/modeling/auto.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/auto.py) | 自动加载器。`AutoDraftModel` 按 `config.architectures` 选模型类，`AutoDraftModelConfig` 按 `architectures` 选配置类。两者都只查 `DRAFT_REGISTRY`。 |
| [specforge/modeling/draft/base.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/base.py) | 草稿模型抽象基类 `Eagle3DraftModel`。规定 EAGLE3 家族架构必须实现的接口，并提供冻结/加载 embedding、加载 vocab mapping 等共享工具。 |

辅助理解（非本讲重点，但会引用作为佐证）：

- [specforge/training/assembly.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py) 里的 `_load_draft`，是「算法轴」与「架构轴」最终交汇、做 `isinstance` 校验的地方。
- [specforge/training/model_loading.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/model_loading.py) 里的 `resolve_draft_config`，校验 draft config 的 `architectures` 与算法期望一致。

当前已注册的全部草稿架构（即 `available_drafts()` 的返回，按字典序）：

| 架构名（类名） | `config_class` | 基类继承自 | 对应主力算法 |
| --- | --- | --- | --- |
| `LlamaForCausalLMEagle3` | `LlamaConfig` | `Eagle3DraftModel` | eagle3 |
| `PEagleDraftModel` | `LlamaConfig` | `Eagle3DraftModel` | peagle |
| `DFlashDraftModel` | `Qwen3Config` | `Qwen3PreTrainedModel` | dflash |
| `DominoDraftModel` | 继承自 DFlash | `DFlashDraftModel` | domino |
| `DSparkDraftModel` | 继承自 DFlash | `DFlashDraftModel` | dspark |

注意 `DominoDraftModel` 与 `DSparkDraftModel` 都直接继承自 `DFlashDraftModel`——这是后面讲「架构复用」的重要伏笔。

---

## 4. 核心概念与源码讲解

### 4.1 草稿架构注册表：register_draft / resolve_draft

#### 4.1.1 概念说明

回顾 u4-l2：算法注册表 `AlgorithmRegistry` 解决的是「`training.strategy` 字符串 → `AlgorithmRegistration`」的映射。那里登记的是**训练策略**。

本讲的 `DRAFT_REGISTRY` 解决的是另一个完全正交的映射：「**架构名字符串 → 模型类**」。它登记的是**模型实现**。

整个 `registry.py` 只有一个模块级字典和三个函数，刻意做得极简：

```python
DRAFT_REGISTRY: Dict[str, type] = {}
```

[specforge/modeling/draft/registry.py:23](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/registry.py#L23) —— 这一行就是全部注册表的存储。键是架构名（默认就是类名），值是模型类本身。

**为什么要和算法注册表分开？** 因为它们是两条独立的轴线：

- 你可以**不改任何架构类**，只换一个 `training.strategy`（算法），就能让同一个 `LlamaForCausalLMEagle3` 架构在不同算法下训练。
- 你也可以**不改任何算法**，只新增一个架构类文件 + `@register_draft`，就能让现有算法去驱动一个新的模型实现（只要算法在契约里声明它兼容这个架构名）。

这两条轴线的交汇点不在注册表里，而在装配阶段（见 4.1.3 末尾）。

#### 4.1.2 核心流程

注册（`@register_draft` 装饰器执行时）：

```
类定义完成
  │
  ▼
@register_draft 触发 _register(cls)
  │
  ├─ 1. 确定 key：name= 显式指定，否则用 cls.__name__
  ├─ 2. 校验 config_class：若 cls.config_class 为 None → 抛 TypeError（强制声明）
  ├─ 3. 去重校验：若 key 已存在且不是同一个类 → 抛 ValueError
  └─ 4. 写入 DRAFT_REGISTRY[key] = cls
  │
  ▼
返回 cls（原样返回，装饰器不改类）
```

解析（`resolve_draft(name)`）：

```
resolve_draft(name)
  │
  ├─ 命中 DRAFT_REGISTRY[name] → 返回该类
  └─ 未命中 KeyError → 重新抛出，并在消息里列出 available_drafts() 全部合法名
```

两个注册期不变量（fail-fast）：

1. **必须声明 `config_class`**：架构类必须带一个类属性 `config_class`，指向它所基于的 HuggingFace `PretrainedConfig` 子类。没有就拒绝注册。
2. **名字唯一**：同一个架构名不能注册到两个不同的类。

#### 4.1.3 源码精读

先看装饰器主体。[specforge/modeling/draft/registry.py:26-50](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/registry.py#L26-L50)：

```python
def register_draft(cls: Optional[type] = None, *, name: Optional[str] = None):
    def _register(cls: type) -> type:
        key = name or cls.__name__                       # 默认用类名当键
        if getattr(cls, "config_class", None) is None:   # 强制 config_class
            raise TypeError(
                f"@register_draft: {cls.__name__} must declare config_class"
            )
        existing = DRAFT_REGISTRY.get(key)
        if existing is not None and existing is not cls:  # 名字唯一
            raise ValueError(
                f"draft architecture {key!r} already registered to "
                f"{existing.__name__}"
            )
        DRAFT_REGISTRY[key] = cls
        return cls

    return _register(cls) if cls is not None else _register
```

三个要点：

- **`key = name or cls.__name__`**：键默认是类名。这正是 draft config JSON 的 `architectures` 字段里填的东西——类的名字就是协议。这也是为什么 `available_drafts()` 列出的都是类名。
- **`getattr(cls, "config_class", None) is None` → TypeError**：这是本讲要重点回答的「config_class 的强制作用」。`config_class` 是一条类属性，让注册表的一个条目同时能解析出**模型类**和**配置类**（见 4.2）。没有它，`AutoDraftModelConfig.from_file` 就不知道该用哪个 `PretrainedConfig` 子类去构建配置对象。
- **最后一行 `return _register(cls) if cls is not None else _register`**：让 `@register_draft`（无括号）和 `@register_draft(name="X")`（带括号）两种写法都能用。

再看真实使用例子。[specforge/modeling/draft/llama3_eagle.py:1628-1631](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/llama3_eagle.py#L1628-L1631) 注册 EAGLE3 主力架构：

```python
@register_draft
class LlamaForCausalLMEagle3(Eagle3DraftModel):
    config_class = LlamaConfig
```

以及 [specforge/modeling/draft/dflash.py:268-271](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/dflash.py#L268-L271) 注册 DFlash 架构：

```python
@register_draft
class DFlashDraftModel(Qwen3PreTrainedModel):
    config_class = Qwen3Config
```

注意 DFlash 没有继承 `Eagle3DraftModel`，而是直接继承 `Qwen3PreTrainedModel`——这说明**注册表对基类没有任何要求**，它只关心「你声明了 `config_class`、且名字不重复」。架构之间可以共享基类，也可以完全异构。

再看 `DominoDraftModel`，[specforge/modeling/draft/domino.py:18-19](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/domino.py#L18-L19)：

```python
@register_draft
class DominoDraftModel(DFlashDraftModel):
    """DFlash backbone with Domino's GRU logits correction."""
```

它继承自 `DFlashDraftModel`，自动继承了 `config_class = Qwen3Config`，但以**自己的类名 `DominoDraftModel`** 注册成独立条目。这就是「架构复用」的活样本：Domino 复用了 DFlash 的主干，只加了一层 GRU 修正头，却是一个独立注册、独立命名的架构。

最后看解析函数。[specforge/modeling/draft/registry.py:53-63](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/registry.py#L53-L63)：

```python
def resolve_draft(name: str) -> type:
    try:
        return DRAFT_REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown draft architecture {name!r}; available: {available_drafts()}"
        ) from None

def available_drafts() -> List[str]:
    return sorted(DRAFT_REGISTRY)
```

未命中时用 `from None` 抹掉原始 traceback，并附带 `available_drafts()`——和 u4-l2 的 `AlgorithmRegistry.resolve` 一样，是「报错即给出全部合法选项」的友好 fail-fast 风格。

**两条轴线的交汇点（重要佐证）**。注册表本身完全不认识算法，但装配阶段会把两条轴拉到一起做最终核对。看 [specforge/training/assembly.py:111-127](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L111-L127) 的 `_load_draft`：

```python
def _load_draft(cfg: Config, algorithm: AlgorithmRegistration):
    provider = algorithm.providers.model
    draft_config = resolve_draft_config(cfg, provider=provider.draft_config)
    draft_model = provider.build_draft(cfg, draft_config)          # 算法侧构建
    architecture = provider.draft_config.architecture              # 算法声明的期望架构名
    expected_type = resolve_draft(architecture)                    # 架构轴查表
    if not isinstance(draft_model, expected_type):                 # 两轴交汇校验
        raise ValueError(
            f"training.strategy={algorithm.name!r} requires {architecture}, but "
            f"the resolved draft config builds {type(draft_model).__name__}"
        )
    return draft_config, draft_model
```

这一段是理解「两条轴线」的最佳切口：

- `algorithm` 来自算法轴（由 `training.strategy` 解析）。
- `resolve_draft(architecture)` 来自架构轴（由 `DRAFT_REGISTRY` 查表）。
- `provider.draft_config.architecture` 是算法**声明**它期望的架构名（例如 eagle3 算法里写死 `DRAFT_ARCHITECTURE = "LlamaForCausalLMEagle3"`，见 [specforge/algorithms/eagle3/providers.py:39](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/providers.py#L39)）。
- 最后的 `isinstance` 是一道运行时保险：确保「算法实际构建出来的模型」确实属于「算法声称要的架构类」。

正因为两条轴解耦，所以这套 `isinstance` 校验才有意义——如果架构是硬编码进算法的，就不需要校验了。

#### 4.1.4 代码实践

**实践目标**：亲手验证「架构轴与算法轴相互独立」，并解释 `config_class` 的强制作用。

**操作步骤（源码阅读型）**：

1. 打开 `specforge/algorithms/` 下任意一个算法的 `providers.py`，找到形如 `DRAFT_ARCHITECTURE = "..."` 的常量（例如 dflash 是 `"DFlashDraftModel"`，domino 是 `"DominoDraftModel"`）。注意：**算法侧只持有一个字符串常量**，没有 `import` 任何模型类。
2. 对照 `specforge/modeling/draft/registry.py`，确认这个字符串正是 `DRAFT_REGISTRY` 的某个键。
3. 思考：如果 domino 算法的 `DRAFT_ARCHITECTURE` 改成 `"DFlashDraftModel"`（即让 domino 算法直接驱动 DFlash 架构，丢掉 GRU 头），注册表层面会不会报错？

**需要观察的现象**：

- 算法与架构之间**没有任何直接的 Python import 依赖**，唯一的耦合是一根「名字字符串」。
- 注册表对「这个架构服务哪个算法」一无所知——它只是个 `名字 → 类` 的字典。

**预期结论**：

- **同一个架构能服务多个算法**：因为架构类本身不含任何算法知识。理论上只要多个算法在 `DraftRequirement.compatible_architectures` 里都把 `DFlashDraftModel` 列为兼容，它们就都能用 `resolve_draft("DFlashDraftModel")` 拿到同一个类。当前 domino / dspark 都复用了 DFlash 主干就是这个思路的体现。
- **同一个算法能换不同架构**：因为算法对架构的依赖只是一根字符串 `provider.draft_config.architecture`。若把算法的兼容集合与期望架构名换成另一个已注册架构，且 draft config 的 `architectures` 字段也跟着改，`resolve_draft` 就会解析出不同的类——算法的 providers 代码一行都不用动。
- **`config_class` 的强制作用**：它让 `@register_draft` 的一个条目能够同时回答两个问题——「该用哪个模型类」和「该用哪个配置类」。没有它，`AutoDraftModelConfig.from_file` 就无法从一个架构名推导出配置对象（见 4.2.3）。所以注册时若 `config_class` 缺失，`@register_draft` 直接抛 `TypeError`，把错误挡在「import 阶段」而不是推到「运行到一半」。

> 注：以上步骤均为源码阅读与推演，不需要运行任何命令、不占 GPU。

#### 4.1.5 小练习与答案

**练习 1**：如果有人写了一个新架构类却忘了加 `config_class`，会怎样？在什么时候报错？

**参考答案**：在该类被定义、`@register_draft` 装饰器执行的瞬间（即模块 import 时），`_register` 内部的 `getattr(cls, "config_class", None) is None` 判定为真，立即抛 `TypeError: @register_draft: <类名> must declare config_class`。错误发生在 import 阶段，远早于训练启动。

**练习 2**：`resolve_draft("SomeFakeArch")` 抛出的异常里会包含什么信息？为什么这样设计？

**参考答案**：会抛 `KeyError`，消息形如 `unknown draft architecture 'SomeFakeArch'; available: [...]`，其中 `[...]` 是 `available_drafts()` 返回的、按字典序排列的全部已注册架构名。这样设计是为了让调用者一眼看到全部合法选项，避免「报错了却不知道该填什么」（与 u4-l2 的 `AlgorithmRegistry.resolve` 同风格）。

**练习 3**：`DominoDraftModel` 注册时并没有显式写 `config_class = Qwen3Config`，为什么没有触发 TypeError？

**参考答案**：因为它继承自 `DFlashDraftModel`，而后者已经声明了 `config_class = Qwen3Config`。`getattr(cls, "config_class", None)` 能从父类继承到这个属性，不为 `None`，所以通过校验。这也是「架构复用」能省事的原因之一。

---

### 4.2 自动加载器：AutoDraftModel / AutoDraftModelConfig

#### 4.2.1 概念说明

HuggingFace 的 `AutoModelForCausalLM` 能凭一份 `config.json` 自动选出对应的模型类来实例化。SpecForge 的 `AutoDraftModel` 是这套机制的「草稿模型专用版」：它不从 HF 的全局注册表选类，而是**只从 `DRAFT_REGISTRY` 选类**。

`auto.py` 里有两个并列的自动类：

- **`AutoDraftModel`**：给一个 config（或一个模型路径），按 `config.architectures[0]` 从 `DRAFT_REGISTRY` 选出模型类并实例化。
- **`AutoDraftModelConfig`**：给一个 `config.json` 路径，按 `architectures[0]` 选出**配置类**（`config_class`），把 JSON 字典反序列化成一个 `PretrainedConfig` 对象。

这一对类的存在意义，正是 4.1 强调的「一个注册条目同时产出模型类与配置类」——而那条桥梁，就是注册时强制的 `config_class`。

#### 4.2.2 核心流程

`AutoDraftModel.from_config(config)`：

```
传入一个 PretrainedConfig 对象
  │
  ├─ _model_cls_from_config(config)
  │     ├─ 取 config.architectures
  │     ├─ 必须长度恰为 1，且该名字在 DRAFT_REGISTRY 中
  │     └─ 返回 DRAFT_REGISTRY[archs[0]]   # 选出模型类
  ├─ model = _model_cls(config, **kwargs)   # 实例化
  └─ 若指定 torch_dtype → model.to(dtype=...)
```

`AutoDraftModelConfig.from_file(path)`：

```
传入 config.json 路径
  │
  ├─ json.load 读取字典
  ├─ 强制 tie_word_embeddings=False（草稿模型不绑权重）
  ├─ 校验 architectures 存在、且恰为 1 个
  ├─ 校验该架构名已注册
  ├─ config_cls = DRAFT_REGISTRY[architecture].config_class   # 取配置类（关键！）
  ├─ 若无 draft_vocab_size → 用 vocab_size 兜底
  └─ return config_cls.from_dict(config)   # 用对应配置类反序列化
```

两者的共同点：**全部决策只依赖 `config.architectures[0]` 这一个字段 + `DRAFT_REGISTRY`**。配置文件里写什么架构名，就构建什么类。

#### 4.2.3 源码精读

先看 `AutoDraftModel` 选类的核心。[specforge/modeling/auto.py:13-21](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/auto.py#L13-L21)：

```python
@classmethod
def _model_cls_from_config(cls, config: PretrainedConfig):
    archs = getattr(config, "architectures", None) or []
    if len(archs) != 1 or archs[0] not in DRAFT_REGISTRY:
        raise ValueError(
            "draft config must name exactly one registered architecture; "
            f"got {archs!r}, available: {available_drafts()}"
        )
    return DRAFT_REGISTRY[archs[0]]
```

两个硬约束：

1. **`len(archs) != 1`**：草稿 config 必须且只能声明**恰好一个**架构。HF 的 AutoModel 允许多个架构然后逐个试，SpecForge 不允许——草稿模型没有「多个候选类逐个回退」的必要，精确匹配更安全。
2. **`archs[0] not in DRAFT_REGISTRY`**：名字必须在草稿注册表里。注意它查的是 `DRAFT_REGISTRY`，**不是** HF 的全局 `MODEL_MAPPING`，所以即使名字撞了 HF 某个原生架构，只要没在草稿表注册就报错。

再看 `from_config`。[specforge/modeling/auto.py:23-41](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/auto.py#L23-L41)：选出类之后，直接 `_model_cls(config, **config_kwargs)` 实例化，可选地 `to(dtype=...)`。逻辑很直白：选类 → 实例化 → 可选转 dtype。

接着看 `AutoDraftModelConfig.from_file`，这是 `config_class` 桥梁真正发挥作用的地方。[specforge/modeling/auto.py:74-116](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/auto.py#L74-L116)，关键段：

```python
architecture = architectures[0]
if architecture not in DRAFT_REGISTRY:
    raise ValueError(...)
config_cls = DRAFT_REGISTRY[architecture].config_class      # ← 取配置类

if "draft_vocab_size" not in config or config["draft_vocab_size"] is None:
    config["draft_vocab_size"] = config.get("vocab_size", None)

return config_cls.from_dict(config)
```

注意第 `config_cls = DRAFT_REGISTRY[architecture].config_class` 这一行——它把一个**架构名**翻译成一个**配置类**。这正是 `@register_draft` 强制要求 `config_class` 的回报：注册表里存的不光是模型类，还顺带能取到「该架构对应的 `PretrainedConfig` 子类」，于是同一份 JSON 字典能用正确的配置类 `from_dict` 出来。

`from_file` 还有两个值得注意的小逻辑：

- [specforge/modeling/auto.py:90-92](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/auto.py#L90-L92)：草稿模型强制 `tie_word_embeddings=False`（打印一行提示）。草稿模型走的是独立 lm_head，不与 embedding 绑定权重。
- [specforge/modeling/auto.py:113-114](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/auto.py#L113-L114)：若 JSON 里没写 `draft_vocab_size`，就用 `vocab_size` 兜底——这是「草稿词表可以小于目标词表」的 vocab mapping 机制（见 u6-l1）的默认行为。

最后看 `from_pretrained` 里一个对开发体验很重要的细节。[specforge/modeling/auto.py:43-71](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/auto.py#L43-L71)：

```python
original_warn = modeling_utils.logger.warning
def filtered_warning(msg):
    if "embed_tokens.weight" in str(msg) and "initialized" in str(msg):
        return
    original_warn(msg)
modeling_utils.logger.warning = filtered_warning
try:
    ...  # 调用 model_cls.from_pretrained(...)
finally:
    modeling_utils.logger.warning = original_warn
```

这段在加载权重期间**临时屏蔽** HF 关于 `embed_tokens.weight` 被随机初始化的告警。原因是草稿模型的 embedding 通常**故意**从目标模型单独加载（见 base.py 的 `load_embedding`）或保持冻结，HF 把它当成「缺失权重」来警告会形成噪声。用 `try/finally` 保证告警过滤器一定被还原，避免污染全局 logger 状态。

#### 4.2.4 代码实践

**实践目标**：跟踪一份 `config.json` 的 `architectures` 字段如何同时决定「模型类」和「配置类」。

**操作步骤（源码阅读 + 文本推演型）**：

1. 想象一份最小草稿 config（来自官方 customization 文档）：

   ```json
   {
     "architectures": ["LlamaForCausalLMEagle3"],
     "model_type": "eagle3",
     "vocab_size": 128256,
     "draft_vocab_size": 32000
   }
   ```

2. 在 `auto.py` 里分别跟踪两条解析路径：
   - 走 `AutoDraftModelConfig.from_file(path)`：`architecture` 取到 `"LlamaForCausalLMEagle3"` → `config_cls = DRAFT_REGISTRY["LlamaForCausalLMEagle3"].config_class` → 应得到 `LlamaConfig` → `LlamaConfig.from_dict(config)`。
   - 走 `AutoDraftModel.from_config(cfg)`：`_model_cls_from_config` 返回 `LlamaForCausalLMEagle3` → `LlamaForCausalLMEagle3(cfg)`。

3. 把上面两步的结果填进一张表：**一个 `architectures` 字段 → 同时解析出模型类 `LlamaForCausalLMEagle3` 与配置类 `LlamaConfig`**。

**需要观察的现象**：

- 两条路径**共享同一个 `DRAFT_REGISTRY` 查表动作**，只是模型类直接用作值，配置类是值的 `.config_class` 属性。
- 若把 `architectures` 改成 `["SomeMissing"]`，两条路径都会在「名字不在注册表」这一步 fail-fast。

**预期结果**：你会清楚地看到，`config_class` 是「让一个注册条目既能产模型类、又能产配置类」的唯一粘合剂——这正是 4.1.4 里 `config_class` 强制作用的落点。

> 注：本实践为源码跟踪与文本推演，未实际运行加载（实际加载需要真实权重文件与 GPU）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_model_cls_from_config` 要求 `len(archs) == 1`，而不是像 HF 那样允许多个架构逐个尝试？

**参考答案**：草稿模型与目标模型是强绑定的（特征 schema、捕获层、vocab mapping 都对齐到具体架构），不存在「不确定是哪个类、逐个试」的合理场景。要求恰好一个架构名能让「配置写错」这类错误立刻暴露，而不是被静默回退掩盖。

**练习 2**：`AutoDraftModelConfig.from_file` 把 `tie_word_embeddings` 强制改成 `False`，这对训练意味着什么？

**参考答案**：草稿模型的 embedding 与 lm_head 不共享权重。embedding 通常从目标模型加载并冻结（见 base.py 的 `freeze_embedding` / `load_embedding`），而 lm_head 是要训练的，二者必须独立，所以不能 tie。

**练习 3**：如果未来某个新架构的 `config_class` 不小心指向了一个**不存在的**配置类，错误会在哪一步暴露？

**参考答案**：注册阶段不会发现（`@register_draft` 只检查 `config_class is not None`，不检查它是否真的是 `PretrainedConfig` 子类）。错误要等到 `AutoDraftModelConfig.from_file` 真正调用 `config_cls.from_dict(config)` 时才会以 `AttributeError` / `TypeError` 的形式暴露。这也是为什么新架构作者必须认真选 `config_class`。

---

### 4.3 草稿模型基类接口：Eagle3DraftModel

#### 4.3.1 概念说明

`Eagle3DraftModel` 是 EAGLE3 特征式草稿家族（含 EAGLE3 与 P-EAGLE）的抽象基类。它的作用有两面：

1. **规定接口**：用 `@abstractmethod` 声明子类必须实现的几个方法。任何想当 EAGLE3 架构的新类，都得补全这些方法，否则无法实例化。
2. **共享工具**：把所有 EAGLE3 草稿都要用到的通用能力（构造因果掩码、冻结 embedding、从目标模型加载 embedding、加载 vocab mapping）实现一次，子类直接继承。

注意：注册表**不要求**架构类必须继承 `Eagle3DraftModel`（DFlash 家族就继承了别的基类，见 4.1.3）。`Eagle3DraftModel` 只是「EAGLE3 特征式草稿」这一脉的约定基类，不是注册表层面的强制。

#### 4.3.2 核心流程

EAGLE3 草稿前向的概念流程（对应 u1-l4 的特征式草拟）：

```
input_ids, 多层 hidden_states, cache_hidden, attention_mask, position_ids
  │
  ├─ embed_input_ids(input_ids)               # 抽象：token → embedding
  ├─ project_hidden_states(hidden_states)     # 抽象：3 层隐藏状态拼接后投影回 d
  ├─ 拼接 embedding + 投影后的 hidden → 输入表示
  ├─ backbone(...)                            # 抽象：草稿主干（常仅 1 层）前向
  └─ compute_logits(hidden_states)            # 抽象：得到供目标模型验证的 logits
```

子类必须实现的四个抽象方法：`embed_input_ids`、`project_hidden_states`、`compute_logits`、`backbone`。基类提供的共享能力：`prepare_decoder_attention_mask`、`freeze_embedding`、`load_embedding`、`load_vocab_mapping`、`all_tied_weights_keys`。

#### 4.3.3 源码精读

先看类声明与四个抽象方法。[specforge/modeling/draft/base.py:38-60](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/base.py#L38-L60)：

```python
class Eagle3DraftModel(PreTrainedModel, ABC):
    """The base class for the Eagle3 draft model implementation. The child class
    needs to implement the abstract methods to support training with TTT."""

    @abstractmethod
    def embed_input_ids(self, input_ids: torch.Tensor) -> torch.Tensor: ...

    @abstractmethod
    def project_hidden_states(self, hidden_states: torch.Tensor) -> torch.Tensor: ...

    @abstractmethod
    def compute_logits(self, hidden_states: torch.Tensor) -> torch.Tensor: ...
```

`project_hidden_states` 的 docstring 直接点明了 u1-l4 讲的「多层隐藏状态拼接」：它把从高/中/低三个深度层抽出的隐藏状态拼接后投影回目标隐藏维度。这正是 EAGLE3 富输入的来源。

第四个抽象方法是 `backbone`，[specforge/modeling/draft/base.py:96-109](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/base.py#L96-L109)：

```python
@abstractmethod
def backbone(
    self,
    input_embeds: torch.Tensor,
    hidden_states: torch.Tensor,
    cache_hidden: torch.Tensor,
    attention_mask: torch.Tensor,
    position_ids: torch.Tensor,
    past_key_values: Optional[Cache] = None,
    use_cache: bool = True,
) -> torch.Tensor: ...
```

`cache_hidden` 这个参数对应 u1-l4 讲的「训练时测试（TTT）」——草稿在训练循环里连走多步，每一步要把自己上一步产出的隐藏状态缓存起来当下一步输入，从而贴近推理、缓解误差累积。

接着看共享的掩码构造，[specforge/modeling/draft/base.py:62-94](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/base.py#L62-L94)（`prepare_decoder_attention_mask`）：它先造一个因果掩码（`_make_causal_mask`），再叠加 padding 掩码（`_expand_mask`），合并成最终 `[bsz, 1, tgt, src]` 的注意力掩码。这是所有 EAGLE3 草稿共用的掩码逻辑，子类不必重写。

再看训练侧最关键的两个工具。冻结 embedding，[specforge/modeling/draft/base.py:128-132](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/base.py#L128-L132)：

```python
def freeze_embedding(self) -> None:
    self.embed_tokens.weight.requires_grad = False
```

草稿的 embedding 直接复用目标模型的 embedding（EAGLE3 假设草稿与目标同词表、同 embedding 空间），训练时**不更新**它，只训练投影层与主干。对应的加载逻辑 `load_embedding`，[specforge/modeling/draft/base.py:134-191](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/base.py#L134-L191)，能从本地目录（`*.index.json` / `model.safetensors` / `pytorch_model.bin` 三种布局逐个探测）或 HuggingFace 仓库（`snapshot_download` 后递归）把目标模型的 `embed_tokens.weight` 拷进草稿。

最后是 vocab mapping 加载，[specforge/modeling/draft/base.py:193-206](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/base.py#L193-L206)：

```python
def load_vocab_mapping(self, file_path: str) -> None:
    assert hasattr(self, "t2d") and hasattr(self, "d2t"), "t2d and d2t buffers ..."
    vocab_mapping = torch.load(file_path)
    self.t2d.copy_(vocab_mapping["t2d"])
    self.d2t.copy_(vocab_mapping["d2t"])
    self.vocab_mapping_loaded = True
```

当草稿词表小于目标词表时（`draft_vocab_size < vocab_size`），需要一对映射缓冲区 `t2d`（target→draft）和 `d2t`（draft→target）在两套词表之间翻译。这个方法把磁盘上的映射文件灌进这两个缓冲区。注意它**断言**子类必须先定义 `t2d`/`d2t` 这两个 buffer——这是「基类提供加载工具，子类负责声明缓冲区」的分工。

> 旁注：`all_tied_weights_keys` 这个 property（[specforge/modeling/draft/base.py:111-126](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/base.py#L111-L126)）是一个兼容不同 Transformers 版本对 tied weights 处理差异的桥接，属于工程适配细节，初学可略读。

#### 4.3.4 代码实践

**实践目标**：明确「新增一个 EAGLE3 家族架构时，子类必须自己实现什么、能白嫖基类什么」。

**操作步骤（源码阅读型）**：

1. 在 `base.py` 中列出所有带 `@abstractmethod` 装饰的方法名。
2. 打开 [specforge/modeling/draft/llama3_eagle.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/llama3_eagle.py)，确认 `LlamaForCausalLMEagle3` 是否实现了这四个方法（grep `def embed_input_ids` / `def project_hidden_states` / `def compute_logits` / `def backbone`）。
3. 再确认它是否**重写**了 `freeze_embedding` / `load_embedding` / `load_vocab_mapping`（预期：没有重写，直接继承）。

**需要观察的现象**：

- 抽象方法在子类里都有具体实现；共享工具方法在子类里基本不重写。
- 这正好体现了基类「定接口 + 供工具」的双重职责。

**预期结果**：你会得出一张清晰的「必须实现 vs 直接继承」对照表：

| 类别 | 方法 | 子类是否必须自己写 |
| --- | --- | --- |
| 抽象接口 | `embed_input_ids` / `project_hidden_states` / `compute_logits` / `backbone` | 必须实现 |
| 共享工具 | `prepare_decoder_attention_mask` / `freeze_embedding` / `load_embedding` / `load_vocab_mapping` | 直接继承（除非有特殊需求） |

> 注：本实践为源码阅读与归纳，不需要运行。

#### 4.3.5 小练习与答案

**练习 1**：如果一个新架构类继承了 `Eagle3DraftModel` 却没实现 `backbone`，会发生什么？

**参考答案**：因为 `backbone` 是 `@abstractmethod`，这个新类仍然是抽象类，**无法被实例化**。当 `AutoDraftModel.from_config` 调用 `_model_cls(config)` 试图构造它时，Python 会抛 `TypeError: Can't instantiate abstract class ... without implementation for abstract method 'backbone'`。这把「接口没补全」的错误挡在了模型构建时刻。

**练习 2**：`load_vocab_mapping` 为什么用 `assert` 检查 `t2d`/`d2t` 两个属性？

**参考答案**：基类提供「加载」逻辑，但 `t2d`/`d2t` 这两个映射缓冲区是子类在 `__init__` 里用 `register_buffer` 声明的（因为不同架构的 buffer 形状/精度可能不同）。基类无法预知它们的存在，所以用 assert 做「调用前自检」：如果子类忘了声明这两个 buffer，加载会在第一时间失败并给出明确提示，而不是在训练中才暴露形状不匹配。

**练习 3**：DFlash 家族（`DFlashDraftModel`）没有继承 `Eagle3DraftModel`，它还能被注册表正确解析吗？

**参考答案**：能。注册表 `DRAFT_REGISTRY` 只关心「类声明了 `config_class` 且名字唯一」，对基类没有任何要求。`resolve_draft("DFlashDraftModel")` 照样返回 `DFlashDraftModel` 类，`AutoDraftModel.from_config` 照样能实例化它。`Eagle3DraftModel` 只是 EAGLE3 特征式一脉的约定基类，不是注册机制的一部分。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一个「**新增一个草稿架构的最小骨架 + 走通解析链**」的任务（纯源码阅读 + 编写骨架，不运行训练）。

**任务背景**：假设你要为 EAGLE3 算法实验一个新的草稿架构 `MyEagle3Draft`，希望它能被 `AutoDraftModel` / `AutoDraftModelConfig` 正确解析。

**步骤 1：写架构骨架**。参考官方 customization 文档与 4.3 的接口表，写出最小骨架（示例代码，非项目原有文件）：

```python
# 示例代码：一个最小草稿架构骨架
from transformers import LlamaConfig, PretrainedConfig

from specforge.modeling.draft.base import Eagle3DraftModel
from specforge.modeling.draft.registry import register_draft


class MyDraftConfig(LlamaConfig):
    model_type = "my-eagle3"


@register_draft
class MyEagle3Draft(Eagle3DraftModel):
    config_class = MyDraftConfig  # ← 4.1 讲的强制项

    def __init__(self, config, **kwargs):
        super().__init__(config)
        # ... 这里要补全 embed_input_ids / project_hidden_states /
        #     compute_logits / backbone 四个抽象方法，否则无法实例化

    def embed_input_ids(self, input_ids): ...
    def project_hidden_states(self, hidden_states): ...
    def compute_logits(self, hidden_states): ...
    def backbone(self, input_embeds, hidden_states, cache_hidden,
                 attention_mask, position_ids, past_key_values=None,
                 use_cache=True): ...
```

**步骤 2：注册生效**。按 customization 文档的要求，在 `specforge/modeling/draft/__init__.py` 里 `from .my_eagle3 import MyEagle3Draft`，让模块被 import、装饰器执行、条目进入 `DRAFT_REGISTRY`。

**步骤 3：写最小 config.json**：

```json
{
  "architectures": ["MyEagle3Draft"],
  "model_type": "my-eagle3",
  "vocab_size": 128256,
  "draft_vocab_size": 32000
}
```

**步骤 4：人工走查解析链**（不运行）。回答以下问题，验证你对本讲的掌握：

1. 装饰器执行时：`key` 取什么值？（答：`"MyEagle3Draft"`，即类名）`config_class` 校验是否通过？（答：通过，因为 `MyDraftConfig` 不为 `None`）。
2. `AutoDraftModelConfig.from_file` 读到这份 JSON 时：`architecture` 是什么？`config_cls` 取到哪个类？（答：`"MyEagle3Draft"`；`config_cls = DRAFT_REGISTRY["MyEagle3Draft"].config_class = MyDraftConfig`）。
3. `AutoDraftModel.from_config` 时：`_model_cls_from_config` 返回哪个类？若四个抽象方法没补全会怎样？（答：返回 `MyEagle3Draft`；若没补全，实例化时抛 `TypeError: Can't instantiate abstract class`）。
4. 若把这个架构接到 `eagle3` 算法上训练，[specforge/training/assembly.py:120-126](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L120-L126) 的 `isinstance(draft_model, expected_type)` 会怎么判？（答：`expected_type = resolve_draft("LlamaForCausalLMEagle3")`，而 `draft_model` 是 `MyEagle3Draft`，二者不匹配，会抛 `ValueError: training.strategy='eagle3' requires LlamaForCausalLMEagle3, but the resolved draft config builds MyEagle3Draft`。这正说明：要让 eagle3 算法接受新架构，还得在算法侧把 `DRAFT_ARCHITECTURE` 与 `compatible_architectures` 一并改成新架构——即「换架构」需要同时动算法的兼容声明，这是两条轴线的交汇约束。）

> 注：步骤 4 的第 4 点尤其值得回味——它把本讲「两条轴线解耦」与「它们在装配处校验」两件事一次性讲透。骨架代码仅用于阅读理解，未在本项目内实际运行。

---

## 6. 本讲小结

- SpecForge 把「**算法**」（`training.strategy`）和「**草稿架构**」（模型类）设计成两条相互独立的轴线：前者由 `AlgorithmRegistry`（u4-l2）管理，后者由本讲的 `DRAFT_REGISTRY` 管理。
- `DRAFT_REGISTRY` 是一个极简的 `Dict[str, type]`；`@register_draft` 装饰器在注册期强制两个不变量——**必须声明 `config_class`**、**架构名唯一**，缺一即 fail-fast。
- `config_class` 是「让一个注册条目同时产出模型类与配置类」的桥梁：`AutoDraftModel` 用架构名取模型类，`AutoDraftModelConfig` 用 `.config_class` 取配置类，二者都只依赖 `config.architectures[0]` 这一个字段。
- `resolve_draft` 与 `available_drafts()` 延续「报错即给全部合法选项」的友好 fail-fast 风格；架构类对基类没有强制要求（DFlash 家族不继承 `Eagle3DraftModel` 也能注册）。
- `Eagle3DraftModel` 是 EAGLE3 特征式一脉的抽象基类：用四个 `@abstractmethod`（`embed_input_ids`/`project_hidden_states`/`compute_logits`/`backbone`）定接口，用 `freeze_embedding`/`load_embedding`/`load_vocab_mapping` 等供共享工具。
- 两条轴线最终在装配阶段 `_load_draft` 的 `isinstance(draft_model, expected_type)` 处交汇校验——这是「解耦但安全」的设计落点。

---

## 7. 下一步学习建议

本讲把「草稿架构这条轴」讲完了。接下来：

- **向下深入装配**：读 [specforge/training/assembly.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py) 的 `_load_draft` 与 `build_training_run`（u6-l1），看草稿模型如何与目标模型、tokenizer、优化器一起被组装成 `TrainingRun`。
- **向横向扩展**：读 [specforge/training/model_loading.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/model_loading.py) 的 `resolve_draft_config`，看 draft config 的 `architectures` 字段如何与算法期望的 `provider.architecture` 对齐校验。
- **动手扩展**：若你想真正新增一个草稿架构，下一阶段可直接做 u10-l1（新增一个草稿架构），把本讲综合实践的骨架补成可运行实现。
- **补全两轴全景**：结合 u4-l1 的 `DraftRequirement.compatible_architectures`、u4-l3 的 `DraftConfigProvider.architecture`、本讲的 `DRAFT_REGISTRY`，画出「算法声明兼容架构 → 配置命名架构 → 注册表解析架构 → 装配处 isinstance 校验」的完整四步图，你就彻底掌握了 SpecForge 算法与架构的接线方式。
