# 新增一个草稿架构

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「草稿架构（draft architecture）」与「训练算法（algorithm）」是 SpecForge 里两条正交的轴线，新增架构不等于新增算法。
- 用 `@register_draft` 装饰器把一个新草稿模型类注册进 `DRAFT_REGISTRY`，做到「新增一个文件 + 一个装饰器」，**无需修改 `modeling/auto.py`**。
- 理解 `config_class` 这条「两用桥梁」约束：一个注册条目要同时提供模型类与配置类。
- 读懂 `AutoDraftModel` / `AutoDraftModelConfig` 如何仅凭草稿 `config.json` 里的 `architectures[0]` 查表解析，以及这套机制为何天然对新增架构开放。
- 把一条完整链路串起来：新类被装饰注册 → 在 `__init__.py` 导入以保证注册时机 → `config.json` 的 `architectures` 字段选中它 → 装配阶段与算法做 `isinstance` 收敛校验。

本讲是「扩展与二次开发」单元的第一讲，定位是**动手扩展**：不再只是读懂既有流程，而是亲手加一个可被框架识别的草稿架构骨架。

## 2. 前置知识

本讲默认你已经掌握前置讲义里建立的两条认知，下面只做最简回顾，不展开：

- **u4-l4 草稿模型注册表**：`specforge/modeling/` 下有一条与「算法注册轴」正交的「草稿架构轴」。算法（eagle3/dflash/…）是「怎么训」的契约，草稿架构（`LlamaForCausalLMEagle3`/`DFlashDraftModel`/…）是「草稿模型长什么样」的类。两条轴最终在装配阶段交汇。
- **u6-l2 训练策略**：算法差异收敛进 `DraftTrainStrategy` 插件，`TrainerCore.train_step` 只调 `strategy.forward_loss`，本身无分支。这说明「架构」与「策略/算法」是分开扩展的。
- **u1-l5 / u4-l2**：SpecForge 没有方法专属的训练入口，统一走 `specforge train --config`，靠 `training.strategy` 选算法。新增架构不触碰这条主轴。

如果你对 PyTorch 的 `nn.Module`、HuggingFace 的 `PretrainedConfig` / `PreTrainedModel`、以及「装饰器」这个 Python 概念完全陌生，建议先补这一层基础再读本讲。

一个关键直觉先建立起来：**SpecForge 的扩展点设计目标是「加东西不改老代码」**。新增一个草稿架构，理想情况下你只新建一个 `.py` 文件、写一个类、打一个装饰器、在 `__init__.py` 里加一行 import——`modeling/auto.py` 这种「总线」文件一行都不用动。本讲就是在源码里验证这个承诺是怎么被代码结构保证的。

## 3. 本讲源码地图

本讲涉及的文件全部在 `specforge/modeling/` 与一处装配落点：

| 文件 | 作用 | 本讲视角 |
| --- | --- | --- |
| `specforge/modeling/draft/registry.py` | `DRAFT_REGISTRY` 字典 + `@register_draft` 装饰器 + `resolve_draft`/`available_drafts` | **核心扩展点**，本讲的主角 |
| `specforge/modeling/auto.py` | `AutoDraftModel` / `AutoDraftModelConfig`，凭 `architectures[0]` 查表解析 | 总线，**只读不改** |
| `specforge/modeling/draft/base.py` | `Eagle3DraftModel` 抽象基类，定下 4 个抽象方法接口 | 新架构通常要继承的基类之一 |
| `specforge/modeling/draft/__init__.py` | 导入所有草稿架构模块，触发注册 | **注册时机**的关键 |
| `specforge/training/assembly.py` | `_load_draft`：装配阶段对草稿架构做 `isinstance` 校验 | 两轴交汇的收敛点 |
| `specforge/training/model_loading.py` | `resolve_draft_config`：校验 config 的 `architectures` 与算法要求一致 | 配置解析侧的查表 |
| `docs/advanced_features/customization.md` | 官方「自定义」文档，给出新增架构的配方 | 本讲的权威依据与验证对象 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：注册扩展点、`config_class` 约束、`auto` 总线解析、注册生命周期与两轴交汇。

### 4.1 register_draft 扩展点

#### 4.1.1 概念说明

「草稿架构」就是草稿模型的**类**——它定义了草稿模型的网络结构（几层、注意力怎么算、logits 怎么出）。SpecForge 把它设计成一条独立于「训练算法」的扩展轴：

- 同一个架构可以服务于多个算法（例如 `DFlashDraftModel` 同时是 dflash 与 domino 的骨架——见 `DominoDraftModel(DFlashDraftModel)`）。
- 同一个算法也可以换不同架构（例如 eagle3 算法可以配不同家族的目标模型对应架构）。

正因为它是一条独立轴，SpecForge 给它配了一个**极小但强约束**的扩展点：一个模块级字典 `DRAFT_REGISTRY`，外加一个类装饰器 `@register_draft`。你新增架构的「动作」就是往这个字典里加一条。装饰器替你做了两件事——生成字典 key、做注册期校验——让你不必手写字典赋值。

这个文件的开头注释把设计意图说得很直白：新增一个架构是「一个新文件加 `@register_draft`」，而不是「去改 `modeling/auto.py`」。

[specforge/modeling/draft/registry.py:9-17](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/registry.py#L9-L17) —— 这段注释是整个扩展点契约的「一句话总纲」：架构与算法是两条轴，注册靠 `@register_draft`，选型靠 `architectures[0]`。

#### 4.1.2 核心流程

注册与解析的核心流程可以用伪代码描述：

```
# 注册（在你的新文件里，import 时发生）
@register_draft                       # 或 @register_draft(name="别名")
class MyDraft(Eagle3DraftModel):
    config_class = MyDraftConfig      # 必填，否则装饰器抛 TypeError
    ...

# 装饰器内部做的事
def register_draft(cls, *, name=None):
    key = name or cls.__name__        # ① key 默认 = 类名
    if cls.config_class is None:       # ② 校验 config_class 必填
        raise TypeError(...)
    if key 已被别的类占用:               # ③ 校验架构名唯一
        raise ValueError(...)
    DRAFT_REGISTRY[key] = cls          # ④ 写入全局字典
    return cls

# 解析（在 auto.py / model_loading.py 里）
model_cls = DRAFT_REGISTRY[config.architectures[0]]   # 凭名字查表
```

四个要点：

1. **key 默认就是类名**——这正是草稿 `config.json` 里 `architectures` 字段会填的值，所以「不传 name」是最常见用法。
2. **`config_class` 是硬约束**——缺失直接 `TypeError`，不让你注册一个「半成品」（详见 4.2）。
3. **架构名必须唯一**——同名重复注册会 `ValueError`，防止两个模块互相覆盖。
4. **解析是纯查表**——`resolve_draft(name)` 读字典，查不到抛 `KeyError` 并列出所有可用架构名，方便排错。

#### 4.1.3 源码精读

先看注册表本体——就是一个普通的模块级字典：

[specforge/modeling/draft/registry.py:23-23](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/registry.py#L23-L23) —— `DRAFT_REGISTRY: Dict[str, type] = {}` 是整条架构轴的唯一事实来源，所有架构类都在这里登记。

再看装饰器本体。它支持两种写法——裸用 `@register_draft` 与带参 `@register_draft(name=...)`，靠 `cls is not None` 判断来同时兼容（见末行）：

[specforge/modeling/draft/registry.py:26-50](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/registry.py#L26-L50) —— 这是注册扩展点的全部实现。注意它刻意保持「无外部依赖」：不 import torch、不依赖 config，使注册可以在任何环境（包括无 GPU 的 `--plan`）下安全完成。

其中两条校验最关键：

- [specforge/modeling/draft/registry.py:37-40](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/registry.py#L37-L40) —— `config_class` 缺失即 `TypeError`，把「忘了声明配置类」这个常见错误拦在 import 期。
- [specforge/modeling/draft/registry.py:41-46](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/registry.py#L41-L46) —— 同名重复注册（且不是同一个类）即 `ValueError`，避免静默覆盖。

解析与列举函数：

[specforge/modeling/draft/registry.py:53-63](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/registry.py#L53-L63) —— `resolve_draft` 查不到时用 `available_drafts()` 列出全部合法架构名，`available_drafts()` 返回**排序后**的列表（确定性输出，便于比对）。

对照真实注册样例：项目里所有内置架构都用了裸装饰器，key 取类名：

[specforge/modeling/draft/dflash.py:268-270](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/dflash.py#L268-L270) —— `DFlashDraftModel` 用 `@register_draft`，key 就是类名字符串 `"DFlashDraftModel"`，并显式声明 `config_class = Qwen3Config`。

[specforge/modeling/draft/peagle.py:174-175](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/peagle.py#L174-L175) —— `PEagleDraftModel` 同样裸装饰器，key = `"PEagleDraftModel"`。

#### 4.1.4 代码实践

**实践目标**：亲手验证注册表里到底登记了哪些架构，并确认「key = 类名」这一约定。

**操作步骤**：

1. 在仓库根目录用只读搜索列出所有 `@register_draft` 落点：

   ```bash
   grep -rn "@register_draft" specforge/modeling/draft/
   ```

2. 写一段「示例代码」（**不是项目原有代码**，仅用于观察注册表行为），保存为 `/tmp/probe_registry.py`：

   ```python
   # 示例代码：观察 DRAFT_REGISTRY 内容
   import specforge.modeling.draft  # 触发 __init__.py，完成全部注册
   from specforge.modeling.draft.registry import DRAFT_REGISTRY, available_drafts

   print("已注册架构（排序）:", available_drafts())
   for key, cls in DRAFT_REGISTRY.items():
       print(f"  key={key!r}  ->  类名={cls.__name__}  config_class={cls.config_class}")
   ```

3. 运行 `python /tmp/probe_registry.py`（无需 GPU，注册不依赖 torch 训练栈）。

**需要观察的现象**：

- 打印出的 `key` 与对应 `cls.__name__` **完全相同**（因为都用裸装饰器，key 取类名）。
- 每个 `cls` 都有一个非空的 `config_class`。

**预期结果**：你会看到 `DominoDraftModel`、`DFlashDraftModel`、`DSparkDraftModel`、`LlamaForCausalLMEagle3`、`PEagleDraftModel` 等条目，且每条都带 `config_class`。如果你看到某条 `config_class` 为 `None`，那说明注册期校验没有生效——但实际上装饰器会在那一步就抛 `TypeError`，所以这种情况不会出现。**待本地验证**：具体条目集合以你本地 `available_drafts()` 输出为准。

#### 4.1.5 小练习与答案

**练习 1**：如果两个不同模块都用 `@register_draft` 注册了名为 `MyDraft` 的类，会发生什么？

**参考答案**：装饰器里的唯一性校验会发现 `key` 已被**另一个不同的类**占用，抛 `ValueError: draft architecture 'MyDraft' already registered to ...`。注意它用 `existing is not cls` 判断——如果重复注册的是**同一个类**（例如同一模块被 import 两次），不会报错，幂等。

**练习 2**：`@register_draft` 和 `@register_draft(name="Foo")` 的 key 分别来自哪里？

**参考答案**：前者 `key = cls.__name__`（类名），后者 `key = "Foo"`。绝大多数情况用前者，因为草稿 `config.json` 的 `architectures` 字段习惯直接填类名。

---

### 4.2 config_class 约束

#### 4.2.1 概念说明

`config_class` 是 `@register_draft` 强制要求每个架构类必须声明的类属性，指向「构造这个架构所用的 HuggingFace `PretrainedConfig` 子类」。它是一条**两用桥梁**：

- 正向：从一个架构名字符串，能取到**模型类**（`DRAFT_REGISTRY[name]`）。
- 反向：从同一个架构名字符串，还能取到**配置类**（`DRAFT_REGISTRY[name].config_class`）。

这一桥接让 SpecForge 只靠「一个注册条目」就能同时解决「模型怎么实例化」和「config.json 怎么反序列化」两件事，而不必维护两张会漂移的表。这也是为什么装饰器把「`config_class` 为空」当成 `TypeError` 级别的硬错误——少它一半功能就断了。

一个重要细节：`config_class` 检查用的是 `getattr(cls, "config_class", None)`，意味着**子类可以继承父类的 `config_class`**。例如 `DominoDraftModel(DFlashDraftModel)` 自己没写 `config_class`，但因为父类 `DFlashDraftModel` 声明了 `config_class = Qwen3Config`，子类注册时仍然通过校验。

#### 4.2.2 核心流程

`config_class` 在两个解析点被消费，理解这两点就理解了它的全部意义：

```
解析点 A（auto.py，AutoDraftModelConfig.from_file）：
  读 config.json → architectures[0] = "MyDraft"
  → config_cls = DRAFT_REGISTRY["MyDraft"].config_class   # 取配置类
  → return config_cls.from_dict(config)                    # 反序列化成 config 对象

解析点 B（model_loading.py 的 payload 解析）：
  同样从 architectures[0] 取出注册类
  → return DRAFT_REGISTRY[architecture].config_class.from_dict(payload)

实例化模型时（auto.py，AutoDraftModel.from_config）：
  → 取模型类 DRAFT_REGISTRY["MyDraft"]
  → model = model_cls(config, ...)                         # 用配置对象实例化模型
```

即：**配置类负责「从 JSON 读进来」，模型类负责「用 config 建网络」**，两者都挂在同一个注册条目上。

#### 4.2.3 源码精读

注册期的强制校验已在 4.1.3 看到（[registry.py:37-40](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/registry.py#L37-L40)）。下面看消费侧。

解析点 A —— `AutoDraftModelConfig.from_file` 末尾取 `config_class` 并反序列化：

[specforge/modeling/auto.py:105-116](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/auto.py#L105-L116) —— 这里 `DRAFT_REGISTRY[architecture].config_class` 就是「两用桥梁」的反向用法：从一个架构名取到配置类，再 `from_dict` 把 JSON 变成 config 对象。顺带它还做了两件善后：缺 `draft_vocab_size` 时回填 `vocab_size`、强制 `tie_word_embeddings=False`（见同文件 [auto.py:90-92](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/auto.py#L90-L92)）。

解析点 B —— `model_loading.py` 里另一处 payload 解析同样依赖 `config_class`：

[specforge/training/model_loading.py:108-119](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/model_loading.py#L108-L119) —— 同样的模式：先确认 `architecture` 已注册，再用 `DRAFT_REGISTRY[architecture].config_class.from_dict(payload)` 反序列化。两处解析点共享同一座桥，保证行为一致。

继承的例子 —— `DFlashDraftModel` 显式声明、`DominoDraftModel` 靠继承：

[specforge/modeling/draft/dflash.py:269-270](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/dflash.py#L269-L270) —— 父类显式 `config_class = Qwen3Config`。

[specforge/modeling/draft/domino.py:18-19](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/domino.py#L18-L19) —— 子类 `DominoDraftModel(DFlashDraftModel)` 不写 `config_class`，靠 `getattr` 继承到 `Qwen3Config`，注册照样通过。这说明新增一个「父架构的变体」时，连 `config_class` 都可以省。

#### 4.2.4 代码实践

**实践目标**：确认「`config_class` 同时服务配置反序列化与模型实例化」这条两用桥梁。

**操作步骤**：

1. 阅读 [auto.py:105-116](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/auto.py#L105-L116) 与 [auto.py:23-41](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/auto.py#L23-L41)（`AutoDraftModel.from_config`）。
2. 在一张纸上画两栏：左栏「取配置类」，右栏「取模型类」，分别填入它们各自访问 `DRAFT_REGISTRY` 的表达式。

**需要观察的现象**：

- 取配置类：`DRAFT_REGISTRY[architecture].config_class`（属性访问，得到「类」）。
- 取模型类：`DRAFT_REGISTRY[architecture]`（直接得到「类」）。
- 两者**源自同一个注册条目**，没有任何第二张映射表。

**预期结果**：你会确认一个注册条目同时回答了「config.json 怎么读」和「模型怎么建」两个问题，这正是装饰器强制 `config_class` 非空的原因。

#### 4.2.5 小练习与答案

**练习 1**：为什么装饰器用 `getattr(cls, "config_class", None) is None` 而不是 `not hasattr(cls, "config_class")` 来判断？

**参考答案**：因为要**允许继承**。`hasattr` 在父类声明、子类继承时也会返回 `True`，两者效果类似；但用 `getattr(..., None) is None` 能同时覆盖「没声明」和「显式声明成 `None`」两种情况，语义更紧。关键是配合 Python 的属性继承机制，让子类（如 `DominoDraftModel`）自动获得父类的 `config_class` 而无需重复声明。

**练习 2**：如果你注册了一个架构却忘了写 `config_class`，错误会在哪个阶段、以什么类型抛出？

**参考答案**：在**模块 import 期**（装饰器执行时）就抛 `TypeError: @register_draft: <类名> must declare config_class`。它不会拖到训练运行时才暴露——这是 fail-fast 的体现。

---

### 4.3 auto 解析

#### 4.3.1 概念说明

`auto.py` 里的 `AutoDraftModel` 与 `AutoDraftModelConfig` 是架构轴的「总线」。它们的工作只有一件：**读 `config.architectures[0]`，去 `DRAFT_REGISTRY` 查表，拿到对应的类**。它本身**不硬编码任何具体架构名**——这就是为什么新增架构「不需要改 `auto.py`」：总线是数据驱动的，字典里有什么它就能解析什么。

这与 HuggingFace 的 `AutoModelForCausalLM` 思路一致（`AutoDraftModel` 正是继承自它），区别在于 SpecForge 用自己的 `DRAFT_REGISTRY` 替代了 HF 那张庞大的全局映射表，只保留草稿模型这一类。

#### 4.3.2 核心流程

`AutoDraftModel` 的两个入口与 `AutoDraftModelConfig` 的一个入口，共同覆盖「从 JSON 到模型」的完整路径：

```
# 路径 1：只有 config 对象，要建一个空权重的模型
AutoDraftModel.from_config(config, torch_dtype=...)
  → _model_cls_from_config(config)   # 读 architectures[0] 查表
  → model_cls(config)                # 实例化
  → 可选 .to(dtype)

# 路径 2：有权重目录，要从盘加载
AutoDraftModel.from_pretrained(path)
  → AutoConfig.from_pretrained(path) 取 config（若未传）
  → _model_cls_from_config(config)   # 同样查表
  → model_cls.from_pretrained(path, config=config)

# 路径 3：只有 config.json 文件，要得到 config 对象（给路径 1 用）
AutoDraftModelConfig.from_file(config_path)
  → json.load + 校验 architectures 唯一且已注册
  → config_class.from_dict(config)
```

三个入口都汇到同一个查表函数 `_model_cls_from_config`，它是真正的「总线闸口」。

#### 4.3.3 源码精读

总线闸口——只认 `architectures[0]`，且严格要求「恰好一个、且已注册」：

[specforge/modeling/auto.py:13-21](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/auto.py#L13-L21) —— 注意三点：(1) `archs` 为空或多于 1 个都报错；(2) 必须在 `DRAFT_REGISTRY` 里；(3) 报错信息带 `available_drafts()`，方便定位。这就是「架构靠名字解析、总线条目无需手改」的落点。

`from_config`——拿到类后实例化，可选转 dtype：

[specforge/modeling/auto.py:23-41](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/auto.py#L23-L41) —— 模型类的真正构造发生在 `_model_cls(config, **config_kwargs)`，`auto.py` 自始至终不知道也不需要知道具体是哪个类。

`from_pretrained`——从盘加载权重，顺带过滤一条无害的 embedding 警告：

[specforge/modeling/auto.py:43-71](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/auto.py#L43-L71) —— 它临时替换 `modeling_utils.logger.warning`，吞掉 `embed_tokens.weight ... initialized` 这条警告（因为草稿模型的 embedding 常常刻意从目标模型加载、允许「未初始化」提示），用 `try/finally` 保证警告过滤器一定复原。

`AutoDraftModelConfig.from_file`——从 JSON 到 config 对象，并做 `tie_word_embeddings` 与 `draft_vocab_size` 的归一化：

[specforge/modeling/auto.py:74-116](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/auto.py#L74-L116) —— 这是「写一份草稿 config.json」时最需要对照的函数：它告诉你 JSON 必须有 `architectures`（唯一一条）、可以省 `draft_vocab_size`（会回填 `vocab_size`）、`tie_word_embeddings` 会被强制改 `False`（并打印提示）。

#### 4.3.4 代码实践

**实践目标**：写出一份能被 `AutoDraftModelConfig.from_file` 接受的最小草稿 `config.json`，并解释它如何被选中。

**操作步骤**：

1. 对照 [customization.md:117-124](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/advanced_features/customization.md#L117-L124) 里给出的最小 config 样例。
2. 写一份「示例」JSON（**仅为说明字段，非项目既有文件**），例如 `/tmp/my_draft_config.json`：

   ```json
   {
     "architectures": ["MyEagle3Draft"],
     "model_type": "my-draft",
     "vocab_size": 128256,
     "draft_vocab_size": 32000,
     "hidden_size": 4096,
     "num_hidden_layers": 1
   }
   ```

3. 阅读并在脑中走一遍 [auto.py:74-116](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/auto.py#L74-L116) 的逻辑，标注：哪一行读 `architectures`、哪一行查 `DRAFT_REGISTRY`、哪一行回填 `draft_vocab_size`。

**需要观察的现象**：

- 如果 `"MyEagle3Draft"` 尚未注册（即没写 4.4 的骨架并 import），`from_file` 会在 [auto.py:105-109](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/auto.py#L105-L109) 抛 `Architecture MyEagle3Draft not registered; available: [...]`。
- 如果 `architectures` 写成两个元素，会在 [auto.py:100-101](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/auto.py#L100-L101) 抛 `Only one architecture is supported`。

**预期结果**：你应当能说清「`architectures[0]` 这个字符串就是架构的身份证，它决定了整条解析链走哪个类」。完整跑通需要先完成 4.4 的骨架注册，故此处「待本地验证」到本讲综合实践后即闭环。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `from_file` 要强制 `tie_word_embeddings=False`？

**参考答案**：草稿模型的输出头（lm_head/embedding）在 SpecForge 里有专门的处理（如冻结 embedding、vocab mapping、compact teacher 等），权重绑定（tying）会干扰这些机制。强制关闭 tying 让草稿的 embedding 与 lm_head 权重解耦，行为可控。

**练习 2**：`AutoDraftModel.from_config` 与 `from_pretrained` 的关键区别是什么？

**参考答案**：`from_config` 只用 config 对象**新建**一个随机权重的模型（再可选转 dtype）；`from_pretrained` 会先 `AutoConfig.from_pretrained` 取 config，再调对应类的 `from_pretrained` 从盘加载真实权重。两者查表的逻辑（`_model_cls_from_config`）完全相同，区别只在「建空模型」还是「加载权重」。

---

### 4.4 注册生命周期与两轴交汇

#### 4.4.1 概念说明

前三个模块讲了「怎么注册、怎么解析」。本模块补上两个工程上必须踩准的点：

1. **注册时机**：装饰器是在模块**被 import 时**才执行的。也就是说，光写一个新文件并 `@register_draft` 还不够，必须让这个文件**被 import**，注册才会发生。SpecForge 的做法是让 `specforge/modeling/draft/__init__.py` 导入每个架构模块——所以你新增架构时，要在这个 `__init__.py` 里加一行 import。
2. **两轴交汇**：架构轴与算法轴是分开注册的，但它们最终必须在装配阶段**对得上**。装配函数 `_load_draft` 会用算法要求的架构名查表得到 `expected_type`，再 `isinstance` 校验实际建出的草稿模型确实是这个类——这是「解耦但安全」的最后一道闸。

理解这两点，你才能写出「真正能被框架用上」的架构，而不是只在孤立文件里自娱自乐。

#### 4.4.2 核心流程

完整的「新增架构 → 被选中 → 被校验」生命周期：

```
① 写新文件 specforge/modeling/draft/my_draft.py
   @register_draft
   class MyEagle3Draft(Eagle3DraftModel):
       config_class = MyDraftConfig
       ...（实现 4 个抽象方法）

② 在 specforge/modeling/draft/__init__.py 加一行：
   from .my_draft import MyEagle3Draft    # 关键：触发注册

③ 任何代码 import specforge.modeling.draft（或其子模块）时，
   __init__.py 执行 → 各架构模块被 import → 装饰器运行 → DRAFT_REGISTRY 填充完成

④ 训练时配置 model.draft_model_config 指向一份 config.json，
   其 "architectures": ["MyEagle3Draft"]

⑤ 装配阶段 _load_draft：
   - resolve_draft_config 校验 config.architectures == 算法要求的架构名
   - provider.build_draft 经 AutoDraftModel.from_config 建出 draft_model
   - expected_type = resolve_draft(算法要求的架构名)
   - isinstance(draft_model, expected_type)  ← 两轴在此交汇
```

第 ⑤ 步是理解整个设计哲学的关键：架构轴（`DRAFT_REGISTRY`）和算法轴（`AlgorithmRegistration`）各管各的，只在装配点用一次 `isinstance` 确认「算法要的架构」与「config 实际建出的架构」一致。

#### 4.4.3 源码精读

注册时机——`__init__.py` 的导入就是注册触发器：

[specforge/modeling/draft/__init__.py:1-12](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/__init__.py#L1-L12) —— 这里 `from .domino import DominoDraftModel` 等每一行，除了把名字导出，更重要的作用是**触发对应模块执行**，从而让模块顶部的 `@register_draft` 装饰器跑起来。官方 customization 文档明确要求：「Import the module from `__init__.py` so registration runs before config resolution」（见 [customization.md:114-115](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/advanced_features/customization.md#L114-L115)）。

两轴交汇——装配阶段的对齐校验：

[specforge/training/assembly.py:111-127](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L111-L127) —— `_load_draft` 是两轴的交汇点。注意它做了三件事：(1) 用算法的 `provider` 建 draft 模型；(2) `expected_type = resolve_draft(architecture)` 从架构轴取算法要求的类；(3) `isinstance(draft_model, expected_type)` 确认实际建出的模型就是该类，否则抛 `ValueError` 提示 strategy 与架构不匹配。

其中 [assembly.py:120-126](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L120-L126) —— 这几行就是「算法 ↔ 架构」两轴的**唯一耦合点**：一根 `architecture` 名字符串 + 一次 `isinstance`。除此之外，两条轴互不感知。

配置解析侧的同样约束——`resolve_draft_config` 要求 config 里的架构名与算法要求严格一致：

[specforge/training/model_loading.py:273-295](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/model_loading.py#L273-L295) —— 它读出 `expected = provider.architecture`（算法要求的架构名），再要求 `draft_config.architectures == [expected]`，不一致即报错。这是在装配建模型**之前**就拦下「config 里写错架构名」的情况。

最后，官方文档强调一条**边界**——架构不是算法：

[docs/advanced_features/customization.md:130-136](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/advanced_features/customization.md#L130-L136) —— 新增架构不定义新损失；真正的新算法还需要 `AlgorithmSpec` + `AlgorithmProviders` + `AlgorithmRegistration`（那是 u10-l2 的主题）。本讲只覆盖「架构」这一条轴。

#### 4.4.4 代码实践

本模块的实践与第 5 节综合实践合并——见下方「综合实践」，在那里你将亲手写出完整的骨架、加 import、并解释它如何被 `config.json` 选中。

#### 4.4.5 小练习与答案

**练习 1**：你在新文件里写了 `@register_draft` 的类，却忘了在 `__init__.py` 里 import 它，会怎样？

**参考答案**：这个模块从未被加载，装饰器从未执行，`DRAFT_REGISTRY` 里没有它。当你用指向它的 `config.json` 训练时，`AutoDraftModelConfig.from_file` 会在 [auto.py:105-109](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/auto.py#L105-L109) 抛 `Architecture <名字> not registered; available: [...]`。修复方式就是在 `__init__.py` 加一行 import。

**练习 2**：算法要求架构 A，但 config.json 里 `architectures` 写成了架构 B（B 也已注册），会在哪里、以什么方式失败？

**参考答案**：会在装配早期的 `resolve_draft_config` 处失败（[model_loading.py:290-295](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/model_loading.py#L290-L295)），提示架构名不匹配；即便绕过这一步，`_load_draft` 的 `isinstance`（[assembly.py:120-126](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L120-L126)）也会兜底失败。即两道闸口都保证「算法与架构必须对齐」。

## 5. 综合实践

**实践目标**：编写一个最小草稿架构类骨架，完成「新文件 + 装饰器 + `config_class` + `__init__` 导入」全流程，并说明它如何被一份 `config.json` 的 `architectures` 字段选中。这是本讲规格要求的代码实践任务。

**操作步骤**：

> 说明：本实践为「源码阅读 + 骨架编写」型，不要求真的跑训练（那需要目标模型与数据集）。骨架代码标注为**示例代码**，仅供学习，不是项目既有文件。**不要把它写入仓库源码**——本讲 worker 只允许写讲义目录。

1. **新建架构文件**（示例代码，路径仅作示意）：`specforge/modeling/draft/my_draft.py`

   ```python
   # 示例代码：最小草稿架构骨架
   from transformers import PretrainedConfig

   from specforge.modeling.draft.base import Eagle3DraftModel
   from specforge.modeling.draft.registry import register_draft


   class MyDraftConfig(PretrainedConfig):
       model_type = "my-draft"


   @register_draft                       # key 默认 = "MyEagle3Draft"
   class MyEagle3Draft(Eagle3DraftModel):
       config_class = MyDraftConfig      # 必填：两用桥梁

       def __init__(self, config, **kwargs):
           super().__init__(config)
           # ... 在此搭建网络层，并实现下面 4 个抽象方法 ...

       # Eagle3DraftModel 要求的 4 个抽象方法（见 base.py）：
       def embed_input_ids(self, input_ids): ...
       def project_hidden_states(self, hidden_states): ...
       def compute_logits(self, hidden_states): ...
       def backbone(self, input_embeds, hidden_states, cache_hidden,
                    attention_mask, position_ids, past_key_values=None,
                    use_cache=True): ...
   ```

   这份骨架直接对应官方文档的配方，见 [customization.md:94-112](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/advanced_features/customization.md#L94-L112)。`Eagle3DraftModel` 要求子类实现的 4 个抽象方法定义在 [base.py:44-59](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/base.py#L44-L59) 与 [base.py:96-109](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/base.py#L96-L109)。

2. **在 `__init__.py` 加一行 import**（示例代码，仅示意）：

   ```python
   # 加到 specforge/modeling/draft/__init__.py
   from .my_draft import MyEagle3Draft
   ```

   这一步是让注册真正发生的关键（见 4.4.3 对 [__init__.py:1-12](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/__init__.py#L1-L12) 的讲解）。

3. **写一份 `config.json`**（示例代码，仅示意）：

   ```json
   {
     "architectures": ["MyEagle3Draft"],
     "model_type": "my-draft",
     "vocab_size": 128256,
     "draft_vocab_size": 32000
   }
   ```

   注意 `"architectures"` 里的字符串必须与类名 `MyEagle3Draft` **完全一致**——这正是 `@register_draft` 默认用类名做 key 的原因。

4. **用 YAML 指向它**（示例，仅示意字段）：

   ```yaml
   model:
     draft_model_config: /path/to/my_draft_config.json
   ```

**需要观察的现象**（沿解析链逐段推理）：

- 模块被 import → `@register_draft` 执行 → `DRAFT_REGISTRY["MyEagle3Draft"] = MyEagle3Draft`，且校验 `config_class=MyDraftConfig` 非空、架构名唯一。
- 训练装配读取 `config.json` → `AutoDraftModelConfig.from_file` 读到 `architectures=["MyEagle3Draft"]` → 查表命中 → 取 `MyDraftConfig` 反序列化成 config 对象。
- `AutoDraftModel.from_config` → `_model_cls_from_config` 查表得到 `MyEagle3Draft` → 实例化模型。
- 装配 `_load_draft` → 若该算法要求架构名正是 `"MyEagle3Draft"`，`isinstance` 通过；否则 fail-fast。

**预期结果**：你应当能用一句话回答「它如何被选中」——

> `config.json` 的 `architectures[0]` 字符串 `"MyEagle3Draft"` 作为 key，在 `DRAFT_REGISTRY` 查表得到类 `MyEagle3Draft`，配置类则通过同一个注册条目的 `config_class` 属性取到 `MyDraftConfig`；整个过程由 `AutoDraftModel` / `AutoDraftModelConfig` 数据驱动完成，**没有在 `auto.py` 里写任何架构名**。

**若无法本地验证**：因骨架未真正实现网络层、也未必有匹配算法，跑通完整训练「待本地验证」。但「注册表命中 + 解析链路」可以用 4.1.4 的探针脚本独立验证——只要 `available_drafts()` 里出现 `"MyEagle3Draft"`，就证明注册与选中机制成立。

## 6. 本讲小结

- **架构与算法是两条正交的轴**：草稿架构（模型类）走 `DRAFT_REGISTRY`，训练算法走 `AlgorithmRegistration`，同一架构可服务多算法、同一算法可换架构。
- **新增架构 = 新文件 + `@register_draft`**：注册表本体是模块级字典 `DRAFT_REGISTRY`，装饰器替你生成 key（默认类名）、做 `config_class` 必填与架构名唯一两道校验，**无需改 `auto.py`**。
- **`config_class` 是两用桥梁**：一个注册条目同时提供模型类（`DRAFT_REGISTRY[name]`）与配置类（`DRAFT_REGISTRY[name].config_class`），分别服务「建模型」和「读 config.json」，且可被子类继承。
- **`auto.py` 总线是数据驱动的**：`AutoDraftModel` / `AutoDraftModelConfig` 只认 `config.architectures[0]` 查表，不硬编码架构名，故对新架构天然开放。
- **注册时机靠 import**：装饰器在模块被 import 时执行，新增架构必须在 `specforge/modeling/draft/__init__.py` 加一行 import，注册才会在配置解析前完成。
- **两轴在装配点交汇**：`_load_draft` 用算法要求的架构名 `resolve_draft` 取 `expected_type`，再 `isinstance` 校验实际模型，以一根字符串 + 一次类型检查实现「解耦但安全」。

## 7. 下一步学习建议

- **新增一个完整训练算法**：本讲只覆盖「架构」轴。若你要做的不是换网络结构、而是换训练损失/流程，请进 **u10-l2 新增一个训练算法**，那里讲 `AlgorithmSpec` 契约 + `AlgorithmProviders` 端口 + `DraftTrainStrategy` 实现 + 注册到 builtin 的三件套。
- **深入装配与加载细节**：想看清「`expected_type` 之外装配还做了什么」，复习 **u6-l1 训练装配**；想看清权重热启动与训练恢复如何与架构加载配合，看 `specforge/training/model_loading.py` 的 `resolve_draft_config` 与 `_warm_start`。
- **阅读真实架构实现**：建议从较小的 [domino.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/domino.py)（在 `DFlashDraftModel` 上加 GRU 修正）和 [peagle.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/peagle.py)（多层 EAGLE 变体）读起，对照本讲的骨架，理解 4 个抽象方法的真实落点。
- **质量门禁**：新增架构后应补齐测试，参考 **u10-l3 测试与质量门禁**，了解 `tests/` 分层与 e2e gate 如何校验新代码。
