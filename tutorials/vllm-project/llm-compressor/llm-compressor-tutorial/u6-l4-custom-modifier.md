# 扩展点一：自定义 Modifier

## 1. 本讲目标

前面的讲义里，我们一直在「读」别人写好的压缩算法：GPTQ、AWQ、SmoothQuant、REAP……它们都是 `Modifier` 的子类。本讲换一个视角——**自己写一个 Modifier**。

llmcompressor 把「压缩算法」做成了可插拔的扩展点：任何压缩动作，只要按约定实现一个 `Modifier` 子类，就能和 `oneshot`、校准管线、recipe、保存流程无缝拼接，就像内置算法一样被使用。理解了这个扩展点，你就能把实验室里的新算法、业务里的自定义后处理（比如权重裁剪、结构化置零、自定义正则）直接挂进生产级压缩流水线。

学完本讲你应当掌握：

- 能写出**自定义 Modifier 的最小骨架**：知道必须实现哪个钩子、可选实现哪些钩子，以及 `on_initialize` / `on_calibration_start` / `on_sequential_epoch_end` / `on_calibration_end` 这条校准链分别在什么时候被触发。
- 能正确使用 **Pydantic 参数字段** 与 **`PrivateAttr` 运行时状态**，并理解 `model_config = ConfigDict(extra="forbid")` 带来的严格约束。
- 掌握两种「让自定义 Modifier 跑起来」的路径：**直接传 Python 对象**给 `oneshot`，或**注册到 `ModifierFactory` 后用 YAML recipe 的类名字符串**调用。
- 理解如何通过**混入 `QuantizationMixin`** 让一个自定义 modifier 直接获得量化能力（像 `GPTQModifier`、`QuantizationModifier` 那样）。

本讲只讲「如何造一个新的 Modifier 并让它被系统调用」这一件事，不再重复讲解具体的量化数学或某一种算法的内部细节。

## 2. 前置知识

本讲假设你已经读过：

- **u2-l3 Modifier 基类生命周期**：知道 `Modifier` 采用模板方法模式——`initialize` / `update_event` / `finalize` 是带状态校验的公开骨架方法，真正的逻辑下放到 `on_*` 钩子；知道带下划线后缀的 `initialized_` / `finalized_` / `started_` / `ended_` 是运行时状态标志（区别于被校验的配置字段 `start` / `end`）；知道 `update_event` 会把事件分流到**校准链**与**训练链**两条互斥的路径。本讲要「承接」这套机制去落地一个新子类。
- **u2-l4 ModifierFactory 自动发现与注册**：知道工厂维护三张注册表——`_main_registry`（正式算法、靠遍历子包自动发现）、`_experimental_registry`、`_registered_registry`（用户经 `register` 写入、优先级最高）；知道 recipe 解析时靠类名字符串经 `create` 实例化。本讲会真正用到其中的 `register` 与 `_registered_registry`。
- **u3-l2 QuantizationMixin 把方案挂到模块**（仅 4.4 节用到）：知道 `QuantizationMixin` 只负责挂卸量化装备（scheme、observer、校准 hook），本身不算 scale/zero-point。

几个需要先建立的直觉：

1. **Modifier 是 Pydantic 模型，不是普通类**。它的「参数」就是 Pydantic 字段，因此既能像普通对象一样 `MyModifier(sparsity=0.5)` 构造，也能从 YAML recipe 反序列化。这决定了写自定义 modifier 时参数要声明成字段、运行时缓存要用 `PrivateAttr`。
2. **你只写钩子，事件由管线触发**。Modifier 永远不会自己 `fire` 事件；`CALIBRATION_START` / `SEQUENTIAL_EPOCH_END` / `CALIBRATION_END` 都由校准管线在固定时机触发，你的 modifier 只需实现对应的 `on_*` 钩子去「响应」。
3. **「会算」和「能被调用」是两件事**。前者靠实现钩子，后者靠把类交给系统——要么作为 Python 对象直接传给 `oneshot(recipe=[...])`，要么 `ModifierFactory.register` 注册后用类名字符串写进 YAML。本讲两条路都会走通。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/llmcompressor/modifiers/interface.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/interface.py) | `ModifierInterface`：用抽象方法定义「所有 modifier 必须满足的契约」（`initialized` / `finalized` / `initialize` / `finalize` / `update_event`）。 |
| [src/llmcompressor/modifiers/modifier.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py) | `Modifier(ModifierInterface, HooksMixin)` 基类：模板方法骨架、`on_*` 钩子默认实现、`requires_calibration_data` 字段、`extra="forbid"` 严格配置。**自定义 modifier 继承它。** |
| [src/llmcompressor/modifiers/utils/hooks.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/utils/hooks.py) | `HooksMixin`：提供 `register_hook` / `remove_hooks` / `disable_hooks`，是「校准时挂 hook、结束后卸 hook」的基础设施，已被 `Modifier` 继承。 |
| [src/llmcompressor/modifiers/factory.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py) | `ModifierFactory`：`create`（按三级优先级实例化）、`register`（把自定义类写入 `_registered_registry`）、`refresh`（遍历子包自动发现）。 |
| [src/llmcompressor/recipe/recipe.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/recipe.py) | `Recipe.create_instance`：解析 YAML 时用类名字符串调 `ModifierFactory.create`（`allow_registered=True`），是 recipe 字符串化调用自定义 modifier 的入口。 |
| [src/llmcompressor/modifiers/pruning/reap/base.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/reap/base.py) | `REAPPruningModifier(Modifier)`：一个结构完整的真实自定义 modifier 范例（Pydantic 字段 + `PrivateAttr` + `model_validator` + 四个校准链钩子）。 |
| [src/llmcompressor/modifiers/quantization/quantization/mixin.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py) | `QuantizationMixin(HooksMixin)`：混入它即可获得量化装备管理能力（4.4 节）。 |
| [src/llmcompressor/modifiers/gptq/base.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py) | `GPTQModifier(Modifier, QuantizationMixin)`：双继承范例——既要生命周期、又要量化能力。 |
| [docs/developer-tutorials/add-modifier.md](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/developer-tutorials/add-modifier.md) | 官方「添加新 Modifier」开发教程，含一个完整的 `WeightClampModifier` 示例。 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 最小实现骨架：契约、钩子与模板方法**——回答「一个 modifier 至少要写什么、能写什么」（`interface.py` + `modifier.py`）。
- **4.2 参数与运行时状态：Pydantic 字段、`PrivateAttr` 与模块匹配**——回答「参数怎么声明、跨 batch 的缓存放哪、怎么找目标模块」（`modifier.py` + `reap/base.py`）。
- **4.3 注册与使用：`ModifierFactory.register` 与 recipe 字符串化**——回答「怎么让自定义 modifier 被 YAML recipe 找到」（`factory.py` + `recipe.py`）。
- **4.4 复用 `QuantizationMixin`：给自定义 modifier 装上量化能力**——回答「怎么让我的 modifier 像内置算法那样做取整」（`mixin.py` + `gptq/base.py`）。

### 4.1 最小实现骨架：契约、钩子与模板方法

#### 4.1.1 概念说明

写一个自定义 modifier，本质是「继承 `Modifier` 并填钩子」。但要填得对，得先分清三层东西：

- **契约（`ModifierInterface`）**：用抽象方法规定「一个 modifier 对外必须长什么样」——必须能被 `initialize` / `update_event` / `finalize`，必须能报告 `initialized` / `finalized`。这是系统调用你的入口约定。
- **骨架（`Modifier`）**：把契约落成**模板方法**。`initialize` / `update_event` / `finalize` 这些公开方法是带状态校验的「外壳」，真正干活的是它们内部调用的 `on_*` 钩子。你**重写钩子，不重写外壳**。
- **钩子（`on_*`）**：你的算法逻辑住在这里。其中 `on_initialize` 是**唯一必须实现**的，其余钩子默认是空操作（no-op），按需重写。

承接 u2-l3 的结论：钩子分两条互斥的链——**校准链**（服务 PTQ）和**训练链**（服务带 step 范围的微调式压缩）。本讲的实践任务走校准链。

#### 4.1.2 核心流程

一个自定义 modifier 在 oneshot 里的生命轨迹如下（承接 u2-l2/u2-l3，这里只看「你的钩子在何时被调」）：

```
oneshot(recipe=[MyModifier(...)])
   │
   ├─ session.initialize  →  对每个 modifier 调 initialize()
   │                            └─ 外壳校验状态后调用  on_initialize()   ← 你必须实现
   │
   ├─ 选管线 + 跑校准（管线触发事件，modifier 只响应）
   │     ├─ CALIBRATION_START      → on_calibration_start()    ← 常用于挂 hook / 重置统计
   │     ├─ (前向若干 batch)        → 你的 forward hook 累积统计
   │     ├─ SEQUENTIAL_EPOCH_END   → on_sequential_epoch_end() ← 真正改权重的高发位置
   │     └─ CALIBRATION_END        → on_calibration_end()      ← 常用于卸 hook / 收尾
   │
   └─ session.finalize  →  对每个 modifier 调 finalize()
                                └─ on_finalize()              ← 可选，清理 / 改 config
```

关键判断：你的算法「需不需要校准数据」决定 `requires_calibration_data`，后者又决定 oneshot 给你派哪种管线（u3-l4：需数据→`sequential`，否则→`datafree`）。好消息是——**三种管线（sequential / basic / datafree）都会触发 `CALIBRATION_START` → `SEQUENTIAL_EPOCH_END` → `CALIBRATION_END` 这三个回调**，所以你在 `on_calibration_end` 里写的逻辑，即便没有校准数据（走 datafree）也会被执行。

#### 4.1.3 源码精读

**契约层**——`ModifierInterface` 用 `@abstractmethod` 规定五个必须存在的成员：

- [interface.py:9-62](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/interface.py#L9-L62) 定义了 `initialized` / `finalized` 两个抽象 property，以及 `initialize` / `finalize` / `update_event` 三个抽象方法。注意：这里**没有**任何 `on_*` 钩子——钩子是 `Modifier` 基类的实现细节，不属于对外契约。

**骨架层**——`Modifier` 是一个 Pydantic 模型，三处对自定义 modifier 最关键：

- [modifier.py:42-55](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py#L42-L55)：`model_config = ConfigDict(extra="forbid")`（**禁止传入未声明的字段**，传错参数会在构造时报错，而非静默忽略）；`requires_calibration_data: bool = False`（控制管线选择，默认不需要数据）；`initialized_` 等 4 个运行时标志。
- [modifier.py:71-95](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py#L71-L95)：`initialize` 外壳——先校验「不能重复初始化、不能在 finalize 后再初始化」，再调 `on_initialize`，返回值赋给 `initialized_`。**你的 `on_initialize` 必须返回 `True` 表示成功**。
- [modifier.py:114-175](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py#L114-L175)：`update_event` 的分流——L134 先无条件跑 catch-all 的 `on_event`；L138-150 是**校准链**（`CALIBRATION_START` → `on_calibration_start`、`SEQUENTIAL_EPOCH_END` → `on_sequential_epoch_end`、`CALIBRATION_END` → `on_calibration_end`，每条都 `return` 互斥）；L154-175 是训练链。

**钩子层**——只有 `on_initialize` 是 `@abstractmethod`：

- [modifier.py:200-211](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py#L200-L211)：`on_initialize` 抽象方法——**子类不实现它，类都无法实例化**。这是「最小实现」的硬下限。
- [modifier.py:213-306](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py#L213-L306)：其余钩子的默认实现——`on_finalize` 默认返回 `True`，`on_event` / `on_start` / `on_update` / `on_end` / `on_calibration_start` / `on_sequential_epoch_end` / `on_calibration_end` 默认都是 `pass`。这就是「只重写你需要的、不必调 `super()`」的依据。

把以上读成一个「最小骨架」就是：

```python
# 示例代码：最小自定义 modifier
from llmcompressor.modifiers import Modifier
from llmcompressor.core import State, Event

class MyModifier(Modifier):
    # 1) 参数声明为 Pydantic 字段（extra="forbid"，未声明的字段会被拒绝）
    my_param: float = 1.0
    # 2) 不需要校准数据时保持默认 False，会走 DataFreePipeline
    requires_calibration_data: bool = False

    # 3) 唯一必须实现的钩子
    def on_initialize(self, state: State, **kwargs) -> bool:
        # state.model 就是被压缩的 torch.nn.Module
        return True

    # 4) 其余钩子按需重写，默认是空操作
    def on_calibration_end(self, state: State, event: Event, **kwargs):
        ...
```

#### 4.1.4 代码实践

**实践目标**：写一个「会说话」的最小 modifier，验证它在 oneshot 流程里确实被调用了。

**操作步骤**：

1. 把下面的脚本存为 `hello_modifier.py`（示例代码）：

```python
# 示例代码
from loguru import logger
from llmcompressor.modifiers import Modifier
from llmcompressor.core import State, Event

class HelloModifier(Modifier):
    """什么都不改，只在每个校准链钩子里打印一行，用来确认钩子被触发。"""
    tag: str = "hello"

    def on_initialize(self, state: State, **kwargs) -> bool:
        logger.info(f"[{self.tag}] on_initialize 被调用")
        return True

    def on_calibration_start(self, state: State, event: Event, **kwargs):
        logger.info(f"[{self.tag}] on_calibration_start 被调用")

    def on_sequential_epoch_end(self, state: State, event: Event, modules=None, **kwargs):
        logger.info(f"[{self.tag}] on_sequential_epoch_end 被调用")

    def on_calibration_end(self, state: State, event: Event, **kwargs):
        logger.info(f"[{self.tag}] on_calibration_end 被调用")

    def on_finalize(self, state: State, **kwargs) -> bool:
        logger.info(f"[{self.tag}] on_finalize 被调用")
        return True
```

2. 用一个极小模型 + 无校准数据跑一次（不传 `dataset`，触发 `DataFreePipeline`）：

```python
# 示例代码
from transformers import AutoModelForCausalLM
from llmcompressor import oneshot

model = AutoModelForCausalLM.from_pretrained("Xenova/Qwen2.5-0.5B")  # 任选一个小模型
oneshot(model=model, recipe=[HelloModifier(tag="demo")])
```

**需要观察的现象**：日志里按顺序出现 `on_initialize` → `on_calibration_start` → `on_sequential_epoch_end` → `on_calibration_end` → `on_finalize` 五行，且都带 `[demo]` 标签。

**预期结果**：因为 `requires_calibration_data=False` 且未传 dataset，oneshot 推断出 `datafree` 管线，而 datafree 管线会按序触发这三个校准回调（u3-l6），故五个钩子全部被调用一次。模型权重不会被改动（本 modifier 没有动权重）。

> 若本地无对应小模型或无网络，上述命令的精确日志「待本地验证」；但调用顺序由管线实现保证，可对照 [src/llmcompressor/pipelines/data_free/pipeline.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/data_free/pipeline.py) 的 `__call__` 确认。

#### 4.1.5 小练习与答案

**练习 1**：如果把上面 `HelloModifier` 的 `on_initialize` 删掉，会发生什么？

**参考答案**：`on_initialize` 在基类里是 `@abstractmethod`（[modifier.py:200-211](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py#L200-L211)），不实现它，`HelloModifier` 仍是一个抽象类，无法实例化，会在 `HelloModifier(tag="demo")` 处抛 `TypeError: Can't instantiate abstract class ...`。

**练习 2**：想让这个 modifier 走 `SequentialPipeline`（逐层校准），需要改哪个字段、还需要补什么？

**参考答案**：把 `requires_calibration_data` 设为 `True`（默认 `False`）。但 sequential 管线需要校准数据与逐层输入，因此还需在 `oneshot` 调用时传 `dataset` / `num_calibration_samples`，否则管线无数据可喂。是否真的「需要」数据取决于你的算法——单纯打印不需要。

---

### 4.2 参数与运行时状态：Pydantic 字段、`PrivateAttr` 与模块匹配

#### 4.2.1 概念说明

写「能用的」自定义 modifier，绕不开三件事：

1. **参数即字段**：`Modifier` 是 Pydantic 模型，所有对外参数（如 `sparsity`、`targets`、`ignore`）都要声明成类级字段。这样它才能既被 Python 构造、又能从 YAML 反序列化。而 `extra="forbid"` 意味着**写错字段名会直接报错**——这是免费校验，也是常见踩坑点。
2. **运行时状态用 `PrivateAttr`**：跨 batch 累积的统计（Hessian、saliency tracker、已处理模块集合）不属于「配置参数」，不应出现在序列化结果里，也不应被 `extra="forbid"` 当成未知字段拒绝。这类状态用 pydantic 的 `PrivateAttr` 声明。
3. **目标模块用 `match_named_modules`**：和内置算法保持一致地按类型名/路径模式筛选模块。它来自外部库 `compressed_tensors`，返回 `(名字, 模块)` 迭代器。

#### 4.2.2 核心流程

一个典型的「带统计累积」的 modifier，其字段与状态的组织方式：

```
声明阶段（类定义时）
  ├─ 配置字段：sparsity / targets / ignore / scheme ...  → Pydantic 字段，可序列化
  └─ 运行时状态：_tracker / _hooks / _clamped ...        → PrivateAttr，不序列化

on_initialize（校准前，一次性）
  └─ 读取配置字段 → 校验（如 sparsity 取值范围、目标模块是否存在）
                   → 可用 model_validator(mode="after") 把校验前置到构造时

on_calibration_start（每个 epoch 开始）
  └─ 给 PrivateAttr 状态分配容器，挂 forward hook 开始累积

on_sequential_epoch_end（逐层或整模型结束）
  └─ 读累积的状态 → 算结果 → 改权重

on_calibration_end / on_finalize
  └─ 清理 PrivateAttr 状态、卸 hook
```

关键约定：**校验尽量早**。能在构造时校验（`model_validator`）就不要拖到 `on_initialize`，能在 `on_initialize` 校验（数据相关、需要先看模型）就不要拖到校准跑一半——否则浪费算力。

#### 4.2.3 源码精读

真实范例 `REAPPruningModifier` 把这三件事都做齐了：

- [reap/base.py:59-72](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/reap/base.py#L59-L72)：配置字段 `sparsity`（必填，无默认）、`ignore`（`Field(default_factory=list)`）；运行时状态 `_moe_attrs` / `_saliency_trackers` / `_n_experts_to_drop` 等全部用 `PrivateAttr(default=...)` 声明——注意带下划线前缀，正好与「配置字段不带下划线、运行时标志带下划线」的命名约定一致。
- [reap/base.py:74-78](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/reap/base.py#L74-L78)：`@model_validator(mode="after")` 在对象构造完成后立即校验 `sparsity ∈ (0,1)`——错误用法在 `REAPPruningModifier(sparsity=1.5)` 这一步就报错，而不是等到量化流程里。
- [reap/base.py:80-154](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/reap/base.py#L80-L154)：`on_initialize` 里做「需要先看模型才能做」的校验（如剪枝后剩余专家是否够 router 的 top_k，不够就 `raise ValueError`），并填充 `_moe_attrs`、计算 `_n_experts_to_drop`。

目标模块筛选方面，官方教程里的 `WeightClampModifier` 是最干净的范例，用 `match_named_modules` 处理 `targets` / `ignore`：

```python
# 示例代码（摘自 docs/developer-tutorials/add-modifier.md 的 WeightClampModifier）
from compressed_tensors.utils import match_named_modules

class WeightClampModifier(Modifier):
    max_weight_magnitude: float = 1.0
    targets: list[str] = Field(default_factory=lambda: ["Linear"])
    ignore: list[str] = Field(default_factory=list)

    def on_initialize(self, state: State, **kwargs) -> bool:
        # 校验：至少匹配到一个目标模块
        matched = list(match_named_modules(state.model, self.targets, self.ignore))
        if not matched:
            raise ValueError(f"No modules matched targets={self.targets} ignore={self.ignore}")
        return True

    def on_calibration_end(self, state: State, event: Event, **kwargs):
        for name, module in match_named_modules(state.model, self.targets, self.ignore):
            with torch.no_grad():
                module.weight.clamp_(-self.max_weight_magnitude, self.max_weight_magnitude)
```

`match_named_modules` 的真实导入位置见 [src/llmcompressor/modifiers/quantization/quantization/mixin.py:27](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L27)（`from compressed_tensors.utils import match_named_modules`），内置量化/平滑算法都用同一个函数，自定义 modifier 跟着用即可保证筛选语义一致。

#### 4.2.4 代码实践

**实践目标**：体会 `extra="forbid"` 与 `PrivateAttr` 的区别——拼错字段会立刻报错，而运行时状态不会出现在序列化里。

**操作步骤**（示例代码，可不依赖 GPU）：

```python
from pydantic import Field, PrivateAttr
from llmcompressor.modifiers import Modifier

class DemoModifier(Modifier):
    factor: float = 0.5                       # 配置字段：会进序列化
    targets: list[str] = Field(default_factory=lambda: ["Linear"])
    _cache: dict = PrivateAttr(default_factory=dict)  # 运行时状态：不进序列化

    def on_initialize(self, state, **kwargs) -> bool:
        return True

# (a) 正常构造
m = DemoModifier(factor=0.1)
print(m.factor, m._cache)          # 0.1 {}

# (b) 故意拼错字段名
try:
    DemoModifier(fctor=0.1)        # 拼错
except Exception as e:
    print("被 extra='forbid' 拦下：", type(e).__name__)
```

**需要观察的现象**： 第 (a) 步正常打印；第 (b) 步抛出 pydantic 的校验异常（`ValidationError`），提示 `fctor` 是未知字段。

**预期结果**：`extra="forbid"`（[modifier.py:42](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/modifier.py#L42)）让拼错参数在构造阶段就被拦截。若想进一步确认 `_cache` 不被序列化，可对比 `m.model_dump()` 的输出里只有 `factor` / `targets`、没有 `_cache`。精确异常文本「待本地验证」（随 pydantic 版本略有差异）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 REAP 把 `_saliency_trackers` 声明成 `PrivateAttr` 而不是普通类属性 `saliency_trackers: dict = {}`？

**参考答案**：两个原因。其一，`PrivateAttr` 声明的状态不会进入 pydantic 序列化（`model_dump` / YAML），避免把跨 batch 的临时统计写进 recipe；其二，可变默认值（如空 dict）必须用 `default_factory` / `PrivateAttr(default_factory=...)` 生成，直接写 `dict = {}` 会被所有实例共享，是经典 Python 陷阱。REAP 用的是 `PrivateAttr(default_factory=dict)`（[reap/base.py:65-67](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/reap/base.py#L65-L67)）。

**练习 2**：`match_named_modules(model, ["Linear"], ["lm_head"])` 返回的元素是什么形状？

**参考答案**：返回的是 `(模块全限定名, nn.Module)` 二元组的迭代器，所以在 `for name, module in match_named_modules(...)` 里解包。`targets=["Linear"]` 按类型名匹配所有 Linear，`ignore=["lm_head"]` 按名字模式排除 lm_head。

---

### 4.3 注册与使用：`ModifierFactory.register` 与 recipe 字符串化

#### 4.3.1 概念说明

4.1/4.2 解决了「会算」，本模块解决「能被调用」。把一个自定义 modifier 接进系统有两条路：

- **路径 A：直接传 Python 对象**。`oneshot(model=..., recipe=[MyModifier(...)])`。`Recipe.create_instance` 接受 Modifier 实例列表，对象原样进入生命周期。**无需注册**，最简单，适合脚本内一次性使用。
- **路径 B：注册到工厂后用 YAML 字符串**。先 `ModifierFactory.register("MyModifier", MyModifier)`，再在 YAML recipe 里写类名字符串。适合把 modifier 配置存盘、版本化、与他人共享。

承接 u2-l4：工厂有三张注册表，`register` 写入的是 `_registered_registry`，它的**优先级最高**（高于自动发现的 `_main_registry`）。而 recipe 解析时调 `create` 会带上 `allow_registered=True`——这意味着**你注册的类，确实能被 YAML recipe 找到**。

#### 4.3.2 核心流程

```
路径 A（Python 对象）：
  MyModifier(...) ──直接──▶ oneshot(recipe=[实例])
                              └─ Recipe.create_instance 发现是 Modifier 实例 → 原样收进 recipe.modifiers

路径 B（YAML 字符串）：
  ① ModifierFactory.register("MyModifier", MyModifier)
        └─ 写入 _registered_registry["MyModifier"] = MyModifier

  ② 写 YAML：
     my_stage:
       my_modifiers:
         MyModifier: {factor: 0.1}

  ③ oneshot(recipe="...yaml...")
        └─ Recipe.create_instance → from_dict → 对每个类名调
              ModifierFactory.create("MyModifier", allow_registered=True, allow_experimental=True, **参数)
                  └─ 命中 _registered_registry → 实例化
```

#### 4.3.3 源码精读

**注册入口**——`ModifierFactory.register`：

- [factory.py:171-189](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L171-L189)：先 `issubclass(modifier_class, Modifier)` 校验必须是 Modifier 子类，否则 `raise ValueError`；通过后写入 `_registered_registry[type_] = modifier_class`。**注册名 `type_` 就是 YAML 里要写的类名字符串**，可以和类名不同，但建议保持一致以免混淆。

**实例化入口**——`create` 的三级查找：

- [factory.py:130-169](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L130-L169)：查找顺序是 `_registered_registry`（需 `allow_registered`，[L152-157](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L152-L157)）→ `_experimental_registry`（需 `allow_experimental`，[L159-164](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L159-L164)）→ `_main_registry`（[L166-167](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L166-L167)）。找不到才 `raise ValueError`。注意 `_registered_registry` 排第一，所以注册名若与内置算法重名，会**覆盖**内置的查找结果。

**recipe 的调用点**——证实 `allow_registered=True`：

- [recipe.py:191-197](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/recipe.py#L191-L197)：`Recipe.create_instance` 解析 YAML 时，对每个类名调 `ModifierFactory.create(mod_type, group=group, allow_registered=True, allow_experimental=True, **mod_args)`。这两个 `True` 是「自定义注册的 modifier 能被 YAML 找到」的根本原因。

> 补充：`register` 写的是 `_registered_registry`，而 `refresh()`（[factory.py:23-35](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L23-L35)）只重建 `_main_registry` 与 `_experimental_registry`，**不会清空 `_registered_registry`**——但 `refresh` 的 docstring 提醒「会清掉之前注册的 modifier」。实践中：注册调用应放在 recipe 解析之前（oneshot 内部解析 recipe 时会触发首次 `refresh`），稳妥做法是在脚本开头、调用 oneshot 之前完成 `register`。

#### 4.3.4 代码实践

**实践目标**：把 4.2 的 modifier 注册到工厂，验证 YAML recipe 能用类名字符串把它实例化。

**操作步骤**（示例代码）：

```python
# 示例代码
from llmcompressor.modifiers import Modifier
from llmcompressor.modifiers.factory import ModifierFactory

class LinearHalfZeroModifier(Modifier):
    """演示：校准结束后把每个 Linear 权重的前一半行置零。不需要校准数据。"""
    targets: list[str] = __import__("pydantic").Field(default_factory=lambda: ["Linear"])
    ignore: list[str] = __import__("pydantic").Field(default_factory=lambda: ["lm_head"])

    def on_initialize(self, state, **kwargs) -> bool:
        return True

    def on_calibration_end(self, state, event, **kwargs):
        import torch
        from compressed_tensors.utils import match_named_modules
        for name, module in match_named_modules(state.model, self.targets, self.ignore):
            with torch.no_grad():
                module.weight[: module.weight.shape[0] // 2].zero_()

# 关键一步：注册到工厂（名字即 YAML 里要用的字符串）
ModifierFactory.register("LinearHalfZeroModifier", LinearHalfZeroModifier)
print("已注册:", "LinearHalfZeroModifier" in ModifierFactory._registered_registry)
```

然后**用 YAML recipe 调用**（路径 B）：

```python
# 示例代码
from transformers import AutoModelForCausalLM
from llmcompressor import oneshot

model = AutoModelForCausalLM.from_pretrained("Xenova/Qwen2.5-0.5B")
recipe_yaml = """
linear_half_zero_stage:
  linear_half_zero_modifiers:
    LinearHalfZeroModifier:
      targets: [Linear]
      ignore: [lm_head]
"""
oneshot(model=model, recipe=recipe_yaml)
```

> 上面用 `__import__("pydantic").Field` 仅为演示自包含；正式代码应写 `from pydantic import Field`。

**需要观察的现象**：

1. 注册后打印出 `已注册: True`。
2. 跑完 oneshot 后，模型里每个 Linear（除 `lm_head`）的权重前一半行应全为 0。

**预期结果**：`Recipe.create_instance` 用类名 `LinearHalfZeroModifier` 命中 `_registered_registry` 并实例化；由于 `requires_calibration_data=False` 且未传 dataset，走 `DataFreePipeline`，`on_calibration_end` 被触发一次，权重被置零。可用下面这段在 oneshot 前后对比零值比例：

```python
# 示例代码：校验
def zero_ratio(model):
    import torch
    for name, m in model.named_modules():
        if type(m).__name__ == "Linear" and "lm_head" not in name:
            print(name, (m.weight == 0).float().mean().item())
```

精确数值「待本地验证」（取决于模型实际 Linear 形状）。

#### 4.3.5 小练习与答案

**练习 1**：如果注册时用的名字和类名不一致，比如 `register("Foo", LinearHalfZeroModifier)`，YAML 里该怎么写？会有什么副作用？

**参考答案**：YAML 里就得写 `Foo:` 而不是 `LinearHalfZeroModifier:`，因为查找键是注册名 `type_`（[factory.py:189](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/factory.py#L189)）。副作用是可读性变差、容易和自动发现的内置算法（按真实类名登记在 `_main_registry`）混淆，所以官方教程与 REAP 都让两者保持一致（类名 `REAPPruningModifier` 即注册名）。

**练习 2**：不调用 `register`，能不能直接 `oneshot(recipe=[LinearHalfZeroModifier()])` 用上它？

**参考答案**：能。这是「路径 A」——`Recipe.create_instance` 接受 Modifier 实例列表，对象直接进入 recipe，**完全不需要经过工厂**。`register` 只在「想用 YAML 类名字符串调用」时才需要。

---

### 4.4 复用 `QuantizationMixin`：给自定义 modifier 装上量化能力

#### 4.4.1 概念说明

前面的自定义 modifier 都是「直接改权重」（置零、裁剪、剪枝）。如果你的新算法本质是**量化**（要把权重/激活映射成低位整数，算 scale/zero_point 并写回模块），从零写挂 scheme、挂 observer、管校准 hook 会非常繁琐。

好在 llmcompressor 把这套「量化装备管理」抽成了 `QuantizationMixin`（详见 u3-l2）。只要让你的 modifier **同时继承 `Modifier` 与 `QuantizationMixin`**，就免费拿到：

- `initialize_quantization`：把 `QuantizationScheme`（由 `scheme` / `config_groups` 描述）挂到目标模块、并先关闭量化。
- `start_calibration`：按 scheme 选择性挂 observer、挂校准 hook、切到 CALIBRATION 状态。
- `end_calibration`：卸 hook、冻结量化参数、删 observer、置 FROZEN。

你要做的只是：在合适的钩子里**调用 `observe()` + `update_qparams()`** 完成取整（取整三连详见 u3-l3）。内置的 `QuantizationModifier`（RTN）和 `GPTQModifier` 都是这个套路。

#### 4.4.2 核心流程

```
class MyQuantModifier(Modifier, QuantizationMixin):   # 双继承
      │
      ├─ 继承自 Modifier        → 生命周期钩子
      └─ 继承自 QuantizationMixin → initialize_quantization / start_calibration / end_calibration

  钩子编排（以 RTN 式取整为例）：
    on_initialize          → （可选）解析 targets / scheme
    on_calibration_start   → 调 start_calibration()：挂 observer + 校准 hook
    on_sequential_epoch_end→ 对每个目标权重调 observe(w) + update_qparams(w)：真正取整
    on_calibration_end     → 调 end_calibration()：卸 hook、冻结
```

一个直觉：**mixin 搭台（挂装备），你的子类唱戏（决定何时取整、取整前要不要先做变换）**。RTN 子类直接取整；GPTQ 子类在取整前先用 Hessian 校准权重；AWQ 子类在取整前先做缩放变换——它们都复用同一套 mixin 装备。

#### 4.4.3 源码精读

**双继承的真实写法**——两个内置量化算法的类声明完全同构：

- [gptq/base.py:47](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L47)：`class GPTQModifier(Modifier, QuantizationMixin):`——同时要生命周期与量化能力。
- [quantization/quantization/base.py:24](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/base.py#L24)：`class QuantizationModifier(Modifier, QuantizationMixin):`——最朴素的 RTN 量化也用同一套双继承。

**mixin 本身的位置与基类**：

- [quantization/quantization/mixin.py:55](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L55)：`class QuantizationMixin(HooksMixin):`——它自己也继承 `HooksMixin`，所以「双继承 `Modifier, QuantizationMixin`」不会重复拿到 hooks 能力（`Modifier` 已含 `HooksMixin`，Python MRO 会归并）。

> 取整三连 `observe → update_qparams → freeze` 的具体语义在 u3-l3 讲过：observer 只收集 `min_vals/max_vals` 统计，换算 scale/zero_point 委托给 `compressed_tensors.calculate_qparams`，且统计在 `get_qparams` 后被删除，所以 `update_qparams` 必须紧跟 `observe`。本模块不重复展开。

#### 4.4.4 代码实践

**实践目标**（源码阅读型）：通过对比三个真实 modifier 的类声明，理解「什么时候该混入 `QuantizationMixin`」。

**操作步骤**：

1. 打开下面三处类声明，记录各自的基类：
   - `QuantizationModifier`：[quantization/quantization/base.py:24](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/base.py#L24)
   - `GPTQModifier`：[gptq/base.py:47](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L47)
   - `REAPPruningModifier`：[reap/base.py:30](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/reap/base.py#L30)
2. 在 `GPTQModifier` 的 `on_sequential_epoch_end` 里找到它调用取整/校准的位置，确认它复用了 mixin 的能力。

**需要观察的现象**：前两者基类含 `QuantizationMixin`，REAP 不含。

**预期结果**：

| Modifier | 基类 | 为什么 |
|----------|------|--------|
| `QuantizationModifier` | `Modifier, QuantizationMixin` | 本身就是量化（RTN），需要挂 scheme/observer |
| `GPTQModifier` | `Modifier, QuantizationMixin` | 量化算法，先用 Hessian 校准权重再取整，仍需整套量化装备 |
| `REAPPruningModifier` | `Modifier` | 只做结构化剪枝（删专家），不做量化取整，不需要量化装备 |

**判断准则**：你的算法**要不要把权重/激活映射成低位整数并计算 scale/zero_point**？要 → 混入 `QuantizationMixin`；只是改权重数值或结构（置零、裁剪、剪枝）→ 只继承 `Modifier` 即可。

> 若你想写一个真正「会取整」的自定义量化 modifier，最稳的起点不是从空类写起，而是**复制 `QuantizationModifier`（RTN）的实现**，在它的 `on_sequential_epoch_end` 取整之前插入你自己的预处理（例如自定义的权重变换），其余装备管理全部交给 mixin。完整可运行的量化示例依赖具体 scheme 与硬件，「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`QuantizationModifier` 和 `GPTQModifier` 都混入了 `QuantizationMixin`，但它们的 `on_sequential_epoch_end` 干的事不一样。各自在「取整」这件事上的区别是什么？

**参考答案**：`QuantizationModifier` 直接对权重做 RTN——在 `on_sequential_epoch_end` 里 `observe(weight)` + `update_qparams(weight)` 就近取整（u3-l1）。`GPTQModifier` 在取整之前，先用校准激活累积的 Hessian 做逐块最优补偿（阻尼 + Cholesky 求逆 + 误差反传到剩余列），再取整（u4-l1）。两者复用同一套 mixin 装备，差异只在「取整前是否用 Hessian 校准权重」。

**练习 2**：为什么 `QuantizationMixin` 自己也继承 `HooksMixin`，而 `Modifier` 已经继承了 `HooksMixin`，双继承时不会冲突？

**参考答案**：Python 的方法解析顺序（MRO）会归并重复的基类，`HooksMixin` 只在继承链里出现一次，`register_hook` / `remove_hooks` 的实现也只有一份。因此 `class MyMod(Modifier, QuantizationMixin)` 拿到的是同一套 hooks 方法，不会重复或冲突。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个**完整、可注册、可被 YAML 调用、且会改权重**的自定义 modifier。

**任务**：实现 `LinearHalfZeroModifier`——在校准结束后，把每个 `Linear`（`lm_head` 除外）权重的前一半行置零，作为「自定义后处理」的演示。要求：

1. **正确建模**：用 Pydantic 字段声明 `targets` / `ignore`；`requires_calibration_data` 保持 `False`（无需校准数据）。
2. **校验前置**：在 `on_initialize` 里用 `match_named_modules` 校验至少命中一个目标模块，否则 `raise ValueError`。
3. **两种调用方式都跑通**：
   - 路径 A：`oneshot(model=..., recipe=[LinearHalfZeroModifier()])`；
   - 路径 B：`ModifierFactory.register(...)` 后，用 YAML recipe 的类名字符串调用。
4. **验证生效**：对比 oneshot 前后 Linear 权重的零值比例，确认前一半行确被置零。
5. **（进阶，可选）**把 `on_calibration_end` 改成 `on_sequential_epoch_end`，传 `dataset` 与 `num_calibration_samples` 让它走 `SequentialPipeline`，观察钩子被触发的次数变化（sequential 下每个子图触发一次，datafree/basic 下触发一次）。

**参考实现框架**（示例代码）：

```python
import torch
from pydantic import Field, PrivateAttr
from compressed_tensors.utils import match_named_modules
from llmcompressor.modifiers import Modifier
from llmcompressor.modifiers.factory import ModifierFactory
from llmcompressor.core import State, Event

class LinearHalfZeroModifier(Modifier):
    """把每个 Linear 权重前一半行置零的演示 modifier。"""
    targets: list[str] = Field(default_factory=lambda: ["Linear"])
    ignore: list[str] = Field(default_factory=lambda: ["lm_head"])
    _done: set = PrivateAttr(default_factory=set)

    def on_initialize(self, state: State, **kwargs) -> bool:
        if not list(match_named_modules(state.model, self.targets, self.ignore)):
            raise ValueError("没有匹配到任何 Linear 目标模块")
        return True

    def on_calibration_end(self, state: State, event: Event, **kwargs):
        for name, module in match_named_modules(state.model, self.targets, self.ignore):
            if name in self._done:
                continue
            with torch.no_grad():
                module.weight[: module.weight.shape[0] // 2].zero_()
            self._done.add(name)

    def on_finalize(self, state: State, **kwargs) -> bool:
        self._done.clear()
        return True

# 注册（路径 B 需要）
ModifierFactory.register("LinearHalfZeroModifier", LinearHalfZeroModifier)
```

跑通后，你就拥有了把任意自定义压缩/后处理逻辑挂进 llmcompressor 生产流水线的全部知识：**继承 `Modifier` → 声明字段与钩子 → 选择「对象直传」或「注册 + YAML」→ 在校准链钩子里改权重**。

## 6. 本讲小结

- **最小骨架**：自定义 modifier = 继承 `Modifier` + 实现 `on_initialize`（唯一必须）+ 按需重写校准链钩子；钩子由管线触发、modifier 永不自己 fire 事件。
- **参数与状态**：配置参数声明为 Pydantic 字段（`extra="forbid"` 拒绝拼错），跨 batch 的运行时状态用 `PrivateAttr`；校验尽量前置（`model_validator` 或 `on_initialize`）。
- **模块匹配**：用 `compressed_tensors.utils.match_named_modules` 按 `targets` / `ignore` 筛选模块，与内置算法保持一致语义。
- **两种调用路径**：直接传 Python 对象给 `oneshot(recipe=[...])` 无需注册；`ModifierFactory.register(name, cls)` 写入 `_registered_registry` 后，YAML recipe 用类名字符串即可调用（`create` 带 `allow_registered=True`）。
- **量化能力复用**：需要做取整的算法，双继承 `(Modifier, QuantizationMixin)` 即可拿到 scheme/observer/hook 的全套装备管理，自己只需在钩子里调取整三连。
- **真实模板**：`REAPPruningModifier`（纯 `Modifier`、带统计累积与结构化剪枝）与 `QuantizationModifier`/`GPTQModifier`（双继承量化）是写新 modifier 时最好的参考样板。

## 7. 下一步学习建议

- **阅读官方开发教程全文**：[docs/developer-tutorials/add-modifier.md](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/developer-tutorials/add-modifier.md) 里的 `WeightClampModifier` 是一个干净完整的范例，建议逐行读懂后改写一个自己的版本。
- **进阶到自定义 Observer**：下一讲 **u6-l5 扩展点二：自定义 Observer** 会讲如何自定义张量统计收集器，与本讲的「自定义量化 modifier」天然配套——你的自定义量化算法可以用自定义 observer 来算 scale/zero_point。
- **深入真实算法实现**：想写「带校准」的自定义 modifier，精读 [reap/base.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/reap/base.py)（挂 hook 累积统计 → `on_sequential_epoch_end` 剪枝）与 [gptq/base.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py)（双继承 + 四钩子编排），它们覆盖了「挂 hook、跨 batch 累积、逐层收尾」的全部典型模式。
- **为它补测试**：参考 [tests/llmcompressor/modifiers/pruning/reap/test_base.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/modifiers/pruning/reap/test_base.py) 的写法，给你的自定义 modifier 写最小单元测试（这一步会在 **u6-l6 测试与贡献流程** 中系统讲解）。
