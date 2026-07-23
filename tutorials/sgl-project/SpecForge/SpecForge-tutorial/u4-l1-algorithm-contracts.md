# 算法契约 contracts

## 1. 本讲目标

学完本讲，你应当能够：

- 说清什么是「纯数据契约（pure contract）」，以及 SpecForge 为什么要把一个算法的静态约束写成**不含任何可执行对象**的纯值。
- 画出 `AlgorithmSpec` 的四件套结构（`name` / `draft` / `feature_contracts` / `capabilities`），并指出它和「可执行 providers」是同一个注册项的两半。
- 读懂 `FeatureContract` 如何用一个 `(mode, modality)` 二元组描述「某一类特征需求需要哪些 tensor」。
- 区分 `DraftRequirement`（草稿架构兼容性）与 `AlgorithmCapabilities`（算法能力）各自约束什么。
- 解释 `FeatureMode` 枚举与 `_assert_pure_value` 递归守卫的作用，理解为什么契约必须保持「纯」。

## 2. 前置知识

本讲是 [u1-l5 目录结构与源码地图](u1-l5-source-map.md) 的直接后续。在进入源码前，先用三句话回忆几条已建立的关键认知：

- **类型化配置**：SpecForge 用 Pydantic 描述 YAML，未知字段直接报错（见 [u2-l2 配置文件七段结构](u2-l2-config-sections.md)）。
- **算法注册表**：装配层用 `training.strategy` 这个字符串去查表，得到一个**不可变**的 `AlgorithmRegistration`，且「装配层只消费、不再查名」。
- **组合根**（[u3-l4 应用组合根 composition](u3-l4-composition-root.md)）：`resolve_run` 把字符串 `training.strategy` 翻译成 `AlgorithmRegistration`，而 `AlgorithmRegistration` 又由两半组成——**纯契约 `AlgorithmSpec`** 和**可执行 `providers`**。

本讲只讲其中一半：**纯契约 `AlgorithmSpec`**。可执行的 `providers`（怎么造草稿模型、怎么捕获特征）留到 [u4-l3 算法 providers 与扩展端口](u4-l3-algorithm-providers.md)；把它们绑在一起的注册表留到 [u4-l2 算法注册表 registry 与 builtin](u4-l2-algorithm-registry.md)。

如果你对投机解码、目标模型/草稿模型、EAGLE3 的「特征式草拟」还不够熟，建议先读 [u1-l3 投机解码原理](u1-l3-speculative-decoding.md) 和 [u1-l4 EAGLE3 特征式草稿原理](u1-l4-eagle3-concepts.md)。本讲会用到的术语：隐藏状态（hidden state）、特征（feature）、接受率、捕获层。

此外需要一点 Python 基础：`dataclass(frozen=True)`（冻结的数据类，实例不可变）、`frozenset`（不可变集合）、`Enum`（枚举）、`__post_init__`（dataclass 构造后自动调用的校验钩子）。不熟也没关系，下面会结合代码解释。

## 3. 本讲源码地图

本讲几乎只围绕**一个文件**展开：

| 文件 | 作用 |
| --- | --- |
| [specforge/algorithms/contracts.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py) | 唯一的契约定义文件，全部是纯数据类与校验函数，没有任何模型类、工厂或运行时对象。 |

为了把抽象契约落到具体算法上，我们会对照两个真实算法是怎么**填充**这些契约的：

| 文件 | 作用 |
| --- | --- |
| [specforge/algorithms/eagle3/providers.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/providers.py) | EAGLE3 算法，用 `algorithm_spec()` 函数构造并返回它的 `AlgorithmSpec`。 |
| [specforge/algorithms/dflash/providers.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/dflash/providers.py) | DFlash 算法，同样用 `algorithm_spec()` 返回自己的 `AlgorithmSpec`，可作为对照。 |

辅助阅读：

| 文件 | 作用 |
| --- | --- |
| tests/test_algorithms/test_contracts.py | 契约的单元测试，断言了「纯值」「多模态」「键查重」等行为，是理解预期行为的好入口。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **契约全景与 FeatureContract**：先建立「契约是什么」的整体观，再精读描述「一类特征需求」的 `FeatureContract`。
2. **DraftRequirement 与 AlgorithmCapabilities**：精读约束「草稿架构」和「算法能力」的两个配角数据类。
3. **FeatureMode 与纯值校验**：讲清枚举 `FeatureMode`，以及为什么契约必须保持「纯」、`_assert_pure_value` 如何递归地强制这一点。

### 4.1 契约全景与 FeatureContract

#### 4.1.1 概念说明

什么是「契约」？你可以把它理解成一张**只填静态信息、不带任何代码的登记表**。它声明：这个算法叫什么名字、能兼容哪些草稿架构、在什么模式下需要哪些 tensor、支持哪些注意力后端……但**它本身不会造模型、不会跑前向、不会捕获特征**。

为什么要这样设计？因为 SpecForge 把一个算法的生命周期劈成了两段：

- **解析期**：组合根 `resolve_run` 拿到配置里的 `training.strategy` 字符串，查注册表，读出这张「登记表」。这一段必须**轻量**——不能因为查个名字就把 `torch`、`sglang` 这些重型依赖全部 import 进来。
- **执行期**：真正要造草稿模型、捕获隐藏状态时，才去调用 `providers` 里那些会触发重型 import 的工厂函数。

`AlgorithmRegistration` 就是把这两段拼在一起的容器：一半是纯契约 `AlgorithmSpec`（本讲），一半是可执行 `providers`（u4-l3）。这种「声明与实现分离」的好处是：校验拓扑、拒绝不支持的组合，可以在**不实例化任何模型**的前提下完成。

`AlgorithmSpec` 这张总登记表由四个字段构成：

```
AlgorithmSpec
├── name              算法名（如 "eagle3"）
├── draft             DraftRequirement：草稿架构兼容性
├── feature_contracts 多个 FeatureContract：每个描述一类 (mode, modality) 特征需求
└── capabilities      AlgorithmCapabilities：算法能力（注意力后端、是否支持 vocab 映射等）
```

其中 `FeatureContract` 是「心脏」——它回答的核心问题是：**「要训练这个算法，我得给它的输入里塞哪些 tensor？」** 例如 EAGLE3 需要 `hidden_state`（目标模型的隐藏状态）和 `target`（教师标签），而 DFlash 只需要一份 `hidden_states`。这些差异全部声明在 `FeatureContract` 里。

#### 4.1.2 核心流程

一个 `FeatureContract` 用一个 **`(mode, modality)` 二元组**作为自己的身份键（`key`）：

- `mode`：特征怎么喂给算法，取值只有两种——`OFFLINE`（预先算好存盘）或 `STREAMING`（在线边算边喂）。
- `modality`：模态，目前主要是 `"text"`，未来可有 `"vision_language"` 等。

于是同一个算法可以为不同的 `(mode, modality)` 各写一张契约。例如 EAGLE3 同时声明了 `(OFFLINE, text)` 和 `(STREAMING, text)` 两张，表示它既能离线训练也能在线训练。

每张契约声明这些内容：

- `required_tensors`：**必需**的 tensor 名字集合（少一个就训练不起来）。
- `optional_tensors`：可有可无的 tensor（如 `position_ids`）。
- `allowed_target_representations` / `default_target_representation`：目标（教师）信号允许以哪些「表示」出现、默认用哪种。
- `schema_version`：契约格式版本，目前**强制为 1**。
- `storage`：仅 `OFFLINE` 模式需要，描述离线记录在磁盘上的原始形状（`OfflineStorageContract`）。

校验规则（在 `__post_init__` 里）可以总结成一张表：

| 规则 | 含义 |
| --- | --- |
| `mode` 必须可被 `FeatureMode` 解析 | 不认识的模式直接报错 |
| `required_tensors` 与 `optional_tensors` 必须**不相交** | 同一个 tensor 不能既必需又可选 |
| `default_target_representation` 必须属于 `allowed`，或两者都空 | 默认值不能凭空出现 |
| `OFFLINE` 模式**必须**带 `storage` | 离线训练要落地磁盘，得说清落成什么样 |
| `STREAMING` 模式**不能**带 `storage` | 在线特征不落盘，定义了就报错 |
| `schema_version` 只能是 1 | 留版本口子，但当前只认 1 |

#### 4.1.3 源码精读

先看 `FeatureContract` 的字段定义与它的 `key` 属性。[contracts.py:161-231](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L161-L231) 定义了这个数据类；其中 [contracts.py:229-231](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L229-L231) 把 `(mode, modality)` 暴露成身份键：

```python
@property
def key(self) -> tuple[FeatureMode, str]:
    return self.mode, self.modality
```

校验逻辑集中在 [contracts.py:174-227](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L174-L227) 的 `__post_init__`，其中两条关键互斥校验是：

- [contracts.py:203-208](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L203-L208)：`required_tensors` 与 `optional_tensors` 取交集，非空就报错。
- [contracts.py:220-223](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L220-L223)：`OFFLINE` 必须有 `storage`、`STREAMING` 不能有 `storage`。

再看顶层容器 `AlgorithmSpec`。[contracts.py:265-309](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L265-L309) 定义了它并在 `__post_init__` 里做整体校验，其中 [contracts.py:297-307](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L297-L307) 检查所有 `feature_contracts` 的 `(mode, modality)` 键**不能重复**——即同一个算法对同一个 `(mode, modality)` 只能有一张契约。

`AlgorithmSpec` 还提供两个查询方法，是装配期最常用的接口：

- [contracts.py:325-333](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L325-L333) `supports(mode, modality)`：返回布尔，问「这个算法支不支持某组合」。
- [contracts.py:335-354](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L335-L354) `feature_contract(mode, modality)`：取出对应契约，找不到就抛 `KeyError` 并列出全部支持的组合——这是个非常友好的错误信息，fail-fast 但不让人猜。

最后注意 [contracts.py:309](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L309)：`AlgorithmSpec` 构造的最后一步是 `_assert_pure_value(self, path="AlgorithmSpec")`，递归检查整棵树里有没有混进可执行对象。这条「纯值不变量」是本讲第 3 个模块的主题，先记住它的位置。

#### 4.1.4 代码实践

**实践目标**：亲手构造一个最小的 `FeatureContract` 和 `AlgorithmSpec`，并用查询方法验证它们的行为。

**操作步骤**：在仓库根目录启动 Python（假定你已按 [u1-l2](u1-l2-install-and-env.md) 装好环境），逐段粘贴：

```python
from specforge.algorithms.contracts import (
    AlgorithmSpec, FeatureContract, FeatureMode, OfflineStorageContract,
    DraftRequirement, AlgorithmCapabilities,
)

# 1) 造一张离线文本契约
offline = FeatureContract(
    mode=FeatureMode.OFFLINE,
    modality="text",
    required_tensors={"input_ids", "hidden_state", "loss_mask"},
    allowed_target_representations={"hidden_state"},
    default_target_representation="hidden_state",
    storage=OfflineStorageContract(
        format="my_v1",
        required_tensors={"input_ids", "hidden_state", "loss_mask"},
        normalizer="my_normalizer",
    ),
)

# 2) 装进一个 AlgorithmSpec
spec = AlgorithmSpec(
    name="mydraft",
    draft=DraftRequirement(
        compatible_architectures={"MyDraft"},
        default_architecture="MyDraft",
    ),
    feature_contracts=(offline,),
    capabilities=AlgorithmCapabilities(attention_backends={"sdpa"}),
)

# 3) 用查询方法问它
print(spec.supports("offline", "text"))          # 预期 True
print(spec.feature_contract("offline", "text").required_tensors)
print(spec.supports("streaming", "text"))        # 预期 False（只声明了 offline）
```

**需要观察的现象**：

- 前两行打印应分别给出 `True` 和那组 tensor 名字。
- 第三行应为 `False`，因为我们只声明了 offline 契约。
- 若把第 1 步的 `mode` 改成 `FeatureMode.STREAMING` 但**保留** `storage=...`，构造时应直接抛 `ValueError: streaming feature contracts cannot define storage`（见 [contracts.py:222-223](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L222-L223)）。

**预期结果**：能成功构造并通过查询；故意写错组合时拿到带「supported: [...]」提示的 `KeyError`。**待本地验证**：上述打印的精确字符串以你本机实际运行为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `FeatureContract` 要把 `required_tensors` 和 `optional_tensors` 设计成两个不相交的集合，而不是合并成一个带「必需/可选」标记的字典？

> **参考答案**：分成两个 `frozenset` 后，校验只需一次集合交集运算（[contracts.py:203](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L203)）；而且装配期可以直接用 `required - already_provided` 算出「还缺哪些 tensor」，用集合运算比遍历字典更直观。强制不相交还避免了一个 tensor「既必需又可选」这种自相矛盾的声明。

**练习 2**：如果一个算法声明了两张 `(OFFLINE, text)` 契约（内容不同），会发生什么？

> **参考答案**：`AlgorithmSpec.__post_init__` 在 [contracts.py:297-307](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L297-L307) 检测到重复的 `(mode, modality)` 键，抛 `ValueError`。注意：**同一个 mode 可以配多个不同 modality**（例如 `(STREAMING, text)` 和 `(STREAMING, vision_language)`），这是被允许的——见测试 [test_contracts.py:82-89](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_algorithms/test_contracts.py#L82-L89)。

### 4.2 DraftRequirement 与 AlgorithmCapabilities

#### 4.2.1 概念说明

`AlgorithmSpec` 的另外两个字段是两个「配角」数据类，它们各自约束一个正交的方面：

- **`DraftRequirement`**：约束**草稿架构**。它声明「这个算法只能搭配哪些草稿模型架构」。例如 EAGLE3 只认 `LlamaForCausalLMEagle3`，DFlash 只认 `DFlashDraftModel`。
- **`AlgorithmCapabilities`**：约束**算法自身的能力**。它声明「这个算法支持哪些注意力后端、是否需要固定 batch、能不能用 compact teacher、能不能做 vocab mapping」。

这里有一条非常关键的认知（也是 [u4-l4 草稿模型注册表](u4-l4-draft-model-registry.md) 的伏笔）：**草稿架构是一条独立于算法的轴线**。算法（EAGLE3）和草稿架构（`LlamaForCausalLMEagle3`）是两个维度的东西。`DraftRequirement` 只是算法这一侧对「我能兼容哪些草稿架构」的**静态声明**——它只放名字字符串，**不放模型类**。真正的草稿模型类注册在另一套注册表（`modeling/draft/registry.py`）里。这就是本讲练习题要回答的「为什么这些契约不含模型类」的一部分答案。

#### 4.2.2 核心流程

`DraftRequirement` 的字段与校验：

- `compatible_architectures`：兼容架构名集合（不能空）。
- `default_architecture`：默认架构，**必须**出现在 `compatible_architectures` 里。
- `supported_overrides`：允许用户在配置里覆盖的草稿参数名（可空）。
- `fixed_override_values`：被算法**钉死**的参数值（如 EAGLE3 钉死 `num_hidden_layers=1`），每项是 `(名字, 整数)` 对。

校验要点（[contracts.py:93-129](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L93-L129)）：

| 规则 | 含义 |
| --- | --- |
| `default_architecture` 必须 ∈ `compatible_architectures` | 默认值不能凭空 |
| `fixed_override_values` 的名字必须 ⊆ `supported_overrides` | 钉死的参数必须先声明支持 |
| `fixed_override_values` 不能有重名 | 同一参数不能钉两个值 |
| `fixed_override_values` 的值必须是**整数**且不能是 `bool` | 类型收紧（Python 里 `bool` 是 `int` 子类，要单独排除） |

`AlgorithmCapabilities` 的字段（[contracts.py:234-262](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L234-L262)）：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `attention_backends` | `FrozenSet[str]` | 支持的注意力后端（如 `sdpa`/`fa`/`usp`） |
| `required_batch_size` | `int \| None` | 若非 None，batch 必须正好等于它 |
| `supports_compact_teacher` | `bool` | 是否支持分块教师投影 |
| `supports_vocab_mapping` | `bool` | 是否支持词表映射（小词表草稿） |
| `allows_aux_layer_override` | `bool` | 是否允许覆盖辅助层 |

#### 4.2.3 源码精读

`DraftRequirement` 定义在 [contracts.py:78-129](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L78-L129)。注意它的 docstring 明说：草稿配置加载、目标派生、校验、模型构造「**都不属于这里**，它们属于草稿模型注册表和算法自有的 providers」。这里只放「稳定标识符」和「声明式覆盖名」。

其中两个值得一看的校验：[contracts.py:114-119](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L114-L119) 检查 `fixed_override_values` 的名字必须都已被 `supported_overrides` 声明；[contracts.py:120-126](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L120-L126) 用 `isinstance(value, bool)` 显式排除布尔——因为 Python 中 `True` 会被当成 `1`，不排除就会让 `num_hidden_layers=True` 漏网。

`AlgorithmCapabilities` 定义在 [contracts.py:234-262](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L234-L262)，它的校验同样把 `bool` 单独拎出来强校验（[contracts.py:255-261](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L255-L261)），并对 `required_batch_size` 要求「正整数或 None」。

现在对照两个真实算法。**EAGLE3** 的 `algorithm_spec()` 在 [eagle3/providers.py:117-166](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/providers.py#L117-L166)，它的 `DraftRequirement` 钉死了草稿层数：

```python
draft=DraftRequirement(
    compatible_architectures={DRAFT_ARCHITECTURE},          # "LlamaForCausalLMEagle3"
    default_architecture=DRAFT_ARCHITECTURE,
    supported_overrides={"attention_layout", "num_hidden_layers"},
    fixed_override_values=(("num_hidden_layers", 1),),      # EAGLE3 草稿恒为 1 层
),
```

它的能力声明（[eagle3/providers.py:160-165](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/providers.py#L160-L165)）开启了全部高阶能力：

```python
capabilities=AlgorithmCapabilities(
    attention_backends={"sdpa", "flex_attention", "fa", "usp"},
    supports_compact_teacher=True,
    supports_vocab_mapping=True,
    allows_aux_layer_override=True,
),
```

**DFlash** 的 `algorithm_spec()` 在 [dflash/providers.py:133-162](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/dflash/providers.py#L133-L162)，能力声明明显更朴素：

```python
capabilities=AlgorithmCapabilities(
    attention_backends={"eager", "sdpa", "flex_attention"},
    # 其余字段全部走默认 False / None
),
```

两组对比已经能看出：EAGLE3 支持 `fa`（FlashAttention）和 `usp`（USP 并行，见 [u8-l2 并行拓扑](u8-l2-parallel-topologies.md)）以及 vocab mapping、compact teacher，而 DFlash 连这些都默认关闭。

#### 4.2.4 代码实践

**实践目标**：用源码事实，填出 EAGLE3 与 DFlash 在契约层面的差异表（这也是本讲综合实践的一部分，这里先做「能力」一栏）。

**操作步骤**：

1. 打开 [eagle3/providers.py:160-165](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/providers.py#L160-L165) 与 [dflash/providers.py:159-161](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/dflash/providers.py#L159-L161)。
2. 填下表（答案见「预期结果」）：

| 字段 | EAGLE3 | DFlash |
| --- | --- | --- |
| `attention_backends` | ? | ? |
| `supports_compact_teacher` | ? | ? |
| `supports_vocab_mapping` | ? | ? |
| `allows_aux_layer_override` | ? | ? |

**需要观察的现象**：两个算法在能力开关上的「全开 vs 全关」对比。

**预期结果**：

| 字段 | EAGLE3 | DFlash |
| --- | --- | --- |
| `attention_backends` | `sdpa, flex_attention, fa, usp` | `eager, sdpa, flex_attention` |
| `supports_compact_teacher` | `True` | `False`（默认） |
| `supports_vocab_mapping` | `True` | `False`（默认） |
| `allows_aux_layer_override` | `True` | `False`（默认） |

#### 4.2.5 小练习与答案

**练习 1**：EAGLE3 的 `fixed_override_values=(("num_hidden_layers", 1),)` 想表达什么？如果把 `("num_hidden_layers", True)` 写进去会怎样？

> **参考答案**：它表达「EAGLE3 的草稿模型**恒为 1 层**，用户不能在配置里改」。`True` 会被 [contracts.py:120-126](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L120-L126) 拒绝并抛 `TypeError`，因为该处显式排除了 `bool`——避免 `True` 被当成整数 `1` 静默通过。

**练习 2**：`AlgorithmCapabilities` 里的 `supports_vocab_mapping` 和 `DraftRequirement` 里的 `compatible_architectures`，一个约束算法、一个约束草稿架构。请用一句话区分两者的责任边界。

> **参考答案**：`supports_vocab_mapping` 回答「**这个算法**能不能把大词表映射成小词表来训练」（算法能力）；`compatible_architectures` 回答「**这个算法**能搭配哪几种草稿模型架构」（算法对草稿的兼容性声明）。前者是算法自身的开关，后者是算法与草稿架构这两条轴线之间的「接线表」。

### 4.3 FeatureMode 与纯值校验

#### 4.3.1 概念说明

`FeatureMode` 是一个极简枚举，只有两个值（[contracts.py:71-75](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L71-L75)）：

- `OFFLINE = "offline"`：特征预先算好、落盘，训练时从磁盘读。
- `STREAMING = "streaming"`：特征在线边算边喂（见 [u7 DataFlow 运行时](u7-l1-runtime-architecture.md)）。

它继承自 `str, Enum`，所以既可以当枚举用、又可以当字符串比较，`FeatureContract.mode` 接受字符串或枚举都会被规整成枚举（[contracts.py:175-178](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L175-L178)）。

本模块的真正主角是**「纯值不变量」**。回到模块文件最顶部的 docstring（[contracts.py:1-6](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L1-L6)），它一句话定调：

> This module intentionally contains no factories, model classes, or runtime objects.

翻译过来就是：本模块**故意不包含**任何工厂、模型类或运行时对象。`_assert_pure_value` 就是这条铁律的**强制执行者**——它在 `AlgorithmSpec` 构造的最后一步被调用（[contracts.py:309](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L309)），递归地遍历整棵契约树，一旦遇到函数、类、lambda 或不透明的对象就抛 `TypeError`。

为什么要这么严格？原因有三：

1. **解析期要轻量**：组合根解析时只读契约，绝不能因此触发 `import torch` 之类重型依赖。纯值保证「读契约」零副作用。
2. **要可哈希、可序列化**：契约经常被放进集合去重、跨进程/跨节点传输。纯值（字符串、数字、frozenset）天然可哈希；函数对象不行。
3. **声明与实现解耦**：可执行的工厂属于 `providers`（u4-l3），不属于契约。把两者物理隔离，能防止「在声明层偷偷塞实现」导致的耦合。

#### 4.3.2 核心流程

`_assert_pure_value`（[contracts.py:42-68](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L42-L68)）是一个**递归守卫**，逻辑可以画成这样：

```
对传入的 value：
├─ 是 type 或 callable?           → 报错（混入了可执行对象）
├─ 是 None / str / int / float / bool / Enum?  → 通过（叶子纯值）
├─ 是 tuple / list / set / frozenset?  → 对每个元素递归
├─ 是 dict?                       → 对每个 key 和 value 递归
├─ 是 dataclass 实例?             → 对每个字段递归
└─ 都不是?                        → 报错（遇到不透明对象）
```

关键在第一条：`isinstance(value, type) or callable(value)` 会拦住**一切**函数、lambda、类、甚至带 `__call__` 的实例。这就是「为什么契约不能含模型类」的**机制层面**答案——你根本塞不进去。

配合它的还有 [contracts.py:18-39](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L18-L39) 的 `_normalized_names`，它把名字集合规整成 `frozenset` 并校验：不能传单个字符串（否则会被当成「字符的集合」静默错误）、名字不能为空、不能带首尾空白。这是「纯值」之外的第二道卫生检查。

#### 4.3.3 源码精读

`FeatureMode` 枚举见 [contracts.py:71-75](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L71-L75)：

```python
class FeatureMode(str, Enum):
    OFFLINE = "offline"
    STREAMING = "streaming"
```

`_assert_pure_value` 见 [contracts.py:42-68](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L42-L68)，核心两行：

```python
if isinstance(value, type) or callable(value):
    raise TypeError(f"{path} must be a pure value, got executable {value!r}")
```

注意它对 `dict` 的 key 也做校验（[contracts.py:53-57](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L53-L57)），对 dataclass 用标准库 `fields()` 反射每个字段（[contracts.py:58-64](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L58-L64)）——所以 `AlgorithmSpec` 内嵌的 `DraftRequirement`、`FeatureContract`、`AlgorithmCapabilities` 都会被自动递归到。

这条不变量在测试里也被显式断言：[test_contracts.py:62-80](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_algorithms/test_contracts.py#L62-L80) 用一个与 `_assert_pure_value` 同构的 `_assert_contract_is_pure` 再次遍历整棵树，确保没有可执行对象「漏」进契约。

最后看 `_normalized_names` 的字符串陷阱防护 [contracts.py:24-25](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L24-L25)：如果你误把 `required_tensors="hidden_state"`（单个字符串）传进去，它会报错——因为 `frozenset("hidden_state")` 会被拆成一个个字符的集合，这是常见的 Python 陷阱，这里主动拦下。

#### 4.3.4 代码实践

**实践目标**：亲手触发 `_assert_pure_value`，观察它如何拒绝可执行对象。

**操作步骤**：在 Python 里尝试把一个**函数**塞进 `feature_contracts`（这违反「契约只含纯值」）：

```python
from specforge.algorithms.contracts import (
    AlgorithmSpec, FeatureContract, FeatureMode, OfflineStorageContract,
    DraftRequirement, AlgorithmCapabilities,
)

def sneaky():
    pass

try:
    AlgorithmSpec(
        name="bad",
        draft=DraftRequirement(
            compatible_architectures={"X"}, default_architecture="X",
        ),
        feature_contracts=(
            FeatureContract(
                mode=FeatureMode.OFFLINE,
                modality="text",
                required_tensors={"input_ids"},
                storage=OfflineStorageContract(
                    format="v1", required_tensors={"input_ids"}, normalizer="n",
                ),
            ),
        ),
        capabilities=AlgorithmCapabilities(attention_backends={"sdpa"}),
    )
    # 上面是合法的；现在故意把一个函数塞进 capabilities 的集合里
    AlgorithmCapabilities(attention_backends={sneaky})
except TypeError as e:
    print("被拒绝:", e)
```

**需要观察的现象**：

- 第一段合法构造应正常返回 `AlgorithmSpec`（注意：构造它本身不会触发任何重型 import）。
- 第二段把函数 `sneaky` 放进 `attention_backends`，应在构造 `AlgorithmCapabilities` 后、或被 `AlgorithmSpec` 递归校验时抛 `TypeError`，信息形如 `... must be a pure value, got executable <function sneaky ...>`。

**预期结果**：可执行对象被拒绝，错误信息会指出具体路径。**待本地验证**：确切触发点（是在 `_normalized_names` 还是 `_assert_pure_value`）与文案以本机为准；重点是**会被拦下**这一行为。

#### 4.3.5 小练习与答案

**练习 1**：有人想「优化」，把草稿模型的构造函数 `build_eagle3_draft` 直接存进 `AlgorithmSpec` 的某个字段，省得再查 providers。这能成功吗？为什么不行？

> **参考答案**：构造时会失败。`build_eagle3_draft` 是函数（callable），`_assert_pure_value` 在 [contracts.py:45-46](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L45-L46) 会拦下任何 callable 并抛 `TypeError`。即使绕过这一步也不该这么做：可执行对象属于 `providers` 半边（u4-l3），把实现塞进声明层会破坏「解析期轻量、声明与实现解耦」的设计。

**练习 2**：`FeatureMode` 继承自 `(str, Enum)`，这意味着 `FeatureMode.OFFLINE == "offline"` 为 `True`。这对调用方有什么好处？

> **参考答案**：调用方既可以用字符串 `"offline"` 传参（如 `spec.supports("offline", "text")`），也可以用枚举 `FeatureMode.OFFLINE`，两者等价。`FeatureContract.__post_init__` 用 `FeatureMode(self.mode)` 把字符串规整成枚举（[contracts.py:175-178](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L175-L178)），既容错又统一。

## 5. 综合实践

**实践目标**：把三个模块串起来，完成规格要求的对比任务——说明 EAGLE3 与 DFlash 在 `required_tensors`、`attention_backends`、`supports_vocab_mapping` 上的差异，并解释为何这些契约不含模型类。

**操作步骤**：

1. 打开 [eagle3/providers.py:117-166](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/providers.py#L117-L166) 与 [dflash/providers.py:133-162](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/dflash/providers.py#L133-L162)。
2. 分别定位两者的 `required_tensors`（EAGLE3 在 [eagle3/providers.py:118-124](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/providers.py#L118-L124)，DFlash 在 [dflash/providers.py:134](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/dflash/providers.py#L134)）与 `capabilities`。
3. 填写下面这张「契约差异表」。

**预期结果（差异表）**：

| 契约字段 | EAGLE3 | DFlash | 差异说明 |
| --- | --- | --- | --- |
| `required_tensors`（offline/streaming 共用） | `input_ids, attention_mask, loss_mask, hidden_state, target` | `input_ids, loss_mask, hidden_states` | EAGLE3 是**特征式**：需要目标模型的 `hidden_state` 当输入、还要 `target` 当教师标签；DFlash 只需一份 `hidden_states`（注意复数拼写不同），不需要显式 `target`。 |
| `attention_backends` | `sdpa, flex_attention, fa, usp` | `eager, sdpa, flex_attention` | EAGLE3 支持 FlashAttention(`fa`) 与 USP 并行(`usp`)；DFlash 额外支持 `eager`，但不支持 `fa`/`usp`。 |
| `supports_vocab_mapping` | `True` | `False`（默认） | EAGLE3 支持把大词表映射成小词表草稿；DFlash 不支持。 |

**为何契约不含模型类？** 请从两个层面回答（这是本实践的核心问）：

1. **设计意图层面**：模块 docstring（[contracts.py:1-6](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L1-L6)）明确声明本模块「故意不含工厂、模型类、运行时对象」。算法的静态约束（叫什么、需要什么 tensor、支持什么后端）和它的可执行实现（怎么造模型、怎么捕获特征）是两件事，被分别放在 `AlgorithmSpec`（本讲）和 `providers`（u4-l3）。草稿架构本身又是一条独立轴线，模型类注册在另一套注册表（`modeling/draft/registry.py`，见 u4-l4），`DraftRequirement` 只放架构的**名字字符串**。
2. **机制强制层面**：就算你想偷偷塞进去也塞不进。`_assert_pure_value`（[contracts.py:42-68](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L42-L68)）在 `AlgorithmSpec` 构造末尾递归扫描，任何 `type` 或 `callable` 都会触发 `TypeError`。这保证了「解析期只读契约」始终零重型 import、可哈希、可跨进程传输。

**需要观察的现象**：能在源码中**逐字**找到上表每个值的出处行号，并能解释 EAGLE3 多出来的 `hidden_state` / `target` 与 [u1-l4 EAGLE3 特征式草稿原理](u1-l4-eagle3-concepts.md) 的对应关系（特征式草拟需要目标隐藏状态作输入）。

**待本地验证**：若想用程序自动核对，可在 Python 里 `from specforge.algorithms.eagle3.providers import algorithm_spec as eagle; print(eagle().capabilities)` 打印实际对象（DFlash 同理），对照上表。精确的 `repr` 文案以本机为准。

## 6. 本讲小结

- **契约 = 纯数据登记表**：`AlgorithmSpec` 只声明算法的静态约束（名字、草稿架构兼容性、特征需求、能力），不含任何工厂、模型类或运行时对象（[contracts.py:1-6](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L1-L6)）。
- **四件套结构**：`AlgorithmSpec = name + draft + feature_contracts + capabilities`，它是 `AlgorithmRegistration` 的「纯契约」半边，另半边是可执行 `providers`。
- **FeatureContract 按 `(mode, modality)` 分键**：每张契约声明一类特征需求需要哪些 tensor，`OFFLINE` 必须带 `storage`、`STREAMING` 不能带；`required` 与 `optional` 必须不相交。
- **两条正交约束**：`DraftRequirement` 约束「能搭配哪些草稿架构」（只放名字），`AlgorithmCapabilities` 约束「算法自身能力」（注意力后端、vocab mapping、compact teacher 等）。
- **FeatureMode 极简**：只有 `OFFLINE` / `STREAMING` 两个值，继承 `(str, Enum)` 兼容字符串传参。
- **纯值不变量由 `_assert_pure_value` 强制**：递归扫描契约树，拒绝一切 callable/type，保证解析期轻量、可哈希、声明与实现解耦。

## 7. 下一步学习建议

本讲只讲了 `AlgorithmRegistration` 的「纯契约」半边。顺着依赖往下读：

1. **[u4-l2 算法注册表 registry 与 builtin](u4-l2-algorithm-registry.md)**：看 `AlgorithmRegistry` 如何把本讲的 `AlgorithmSpec` 和可执行 `providers` 绑成 `AlgorithmRegistration`，以及 `builtin_algorithm_registry()` 注册了哪些算法名。这是承接本讲的最自然下一篇。
2. **[u4-l3 算法 providers 与扩展端口](u4-l3-algorithm-providers.md)**：看「另一半」——`build_draft`、`resolve_capture_layers`、collator 等可执行端口，理解声明与实现是怎么接线的。
3. **[u4-l4 草稿模型注册表 modeling draft registry](u4-l4-draft-model-registry.md)**：看草稿架构这条独立轴线，理解为什么 `DraftRequirement` 只放架构名字、真正的模型类在另一套注册表里。
4. 想看契约如何被**消费**，可跳到 [u3-l4 应用组合根 composition](u3-l4-composition-root.md) 的 `validate_resolved_run`，那里会用本讲的 `FeatureContract` / `AlgorithmCapabilities` 做拓扑校验。
