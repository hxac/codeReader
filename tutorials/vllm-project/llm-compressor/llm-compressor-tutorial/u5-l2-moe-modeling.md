# MoE 建模与线性化

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 **MoE（Mixture-of-Experts，混合专家）模型在量化校准时遇到的两个特有难题**，以及 llm-compressor 分别用什么机制解决它们。
- 掌握 **专家线性化（linearization）**：为什么要把 3D 融合专家权重拆成 2D，`linearize_moe` 与 `load_quantizable_moe` 两条路径分别怎么工作。
- 掌握 **专家感知校准上下文** `moe_calibration_context` 与 `get_calibrate_all_experts_flag`：它们如何让「所有专家都看到所有校准 token」。
- 理解 **架构映射（conversion_mappings）**：`ARCH_TO_IMPORT_PATHS` / `ARCH_TO_2D_MAPPINGS` 如何为 Llama4、GraniteMoE、Qwen3-MoE 等架构提供高效加载映射。
- 能看懂 `oneshot` 在遇到未线性化 MoE 时打印的回退警告含义，并知道如何用 `load_context` / `load_quantizable_moe` 提前规避。

本讲承接 [u3-l5 SequentialPipeline 逐层校准深析](u3-l5-sequential-pipeline.md)：sequential 管线依赖 `torch.fx` 把模型切成子图逐层校准，而 MoE 的「融合 3D 权重」和「稀疏路由」会破坏这条链路——本讲讲的就是 llm-compressor 如何把 MoE 改造成 sequential 管线能正确处理的形式。

## 2. 前置知识

### 2.1 什么是 MoE

普通 Transformer 的每个 FFN/MLP 层对所有 token 用同一组权重。**MoE（混合专家）** 则在每一层准备多组权重（称为「专家 expert」），再用一个**路由器（router/gate）**决定每个 token 该交给哪几个专家。典型地每个 token 只激活 top-k 个专家（如 8 选 2）。

好处是「参数量大、计算量小」——总参数很多，但单次前向只动一小部分。代价是工程更复杂，校准也更难。

### 2.2 校准数据与「专家稀疏激活」问题

校准（calibration）是用一小批数据跑前向，收集激活统计（min/max、Hessian 等）来算量化参数（详见 [u3-l3 校准、Observers 与 Hooks](u3-l3-calibration-observers-hooks.md)）。对稠密模型，每个 Linear 都会收到全部 token。

但 MoE 的路由是**稀疏**的：每个 token 只去 top-k 个专家。校准数据集又相对小，于是会出现：

- 某些专家在整个校准集里**几乎不被激活**，收到的 token 极少甚至为 0；
- 统计样本不足 → 算出的 scale/zero-point 不稳，甚至出现 NaN。

这是本讲要解决的**第一个难题**。

### 2.3 「融合专家」与 3D 权重

为了推理速度，transformers v5 会把同一层的多个专家**融合（fuse）**成一个大张量。例如把 \(E\) 个专家的 `gate_proj` 堆成一个 3D 张量，形状大约是 \((E,\,\text{intermediate},\,\text{hidden})\)，再用 `group_mm`/`batch_mm` 一次性算完。

但 llm-compressor 的几乎所有量化算法（GPTQ、AWQ、SmoothQuant、RTN）都是围绕**2D Linear 权重** \((\text{out},\,\text{in})\) 设计的校准流程。3D 融合权重无法直接套用。

这是本讲要解决的**第二个难题**，对应的操作叫**线性化（linearization）**：把融合的 3D 专家拆回一串独立的 2D Linear。

> 关键直觉：**线性化不改数学结果**，只改权重的「存放形状」，目的是让量化算法能逐专家、逐 2D 矩阵地校准。

## 3. 本讲源码地图

本讲涉及的核心文件都在 `src/llmcompressor/modeling/moe/` 下：

| 文件 | 作用 |
| --- | --- |
| [context.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/moe/context.py) | 专家感知校准上下文：`moe_calibration_context` 与全局开关 `get_calibrate_all_experts_flag`（解决难题一）。 |
| [linearize.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/moe/linearize.py) | 线性化入口：`linearize_moe`（加载后转换）、`load_quantizable_moe`（加载时转换）、`get_non_linearized_moes`（探针）。 |
| [conversion_mappings.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/moe/conversion_mappings.py) | 架构映射表：`ARCH_TO_IMPORT_PATHS`（哪些架构有专家类）、`ARCH_TO_2D_MAPPINGS`（哪些架构能直接以 2D 加载）。 |
| [linear_experts.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/moe/linear_experts.py) | `LinearExperts2D`：线性化后的专家容器，`forward` 里用校准开关决定是否给每个专家喂全部 token。 |
| [helpers.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/moe/helpers.py) | `FusedExpertsProtocol`（识别专家模块的协议）、`MoEConfig`（从 config 抽取专家超参）。 |
| [granitemoe.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/moe/granitemoe.py) / [llama4.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/moe/llama4.py) | 针对不走标准 `use_experts_implementation` 装饰器的架构（GraniteMoE、Llama4）的自定义线性化定义。 |

调用方入口在 [oneshot.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py) 与 [utils/dev.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/utils/dev.py)。

## 4. 核心概念与源码讲解

### 4.1 MoE 校准的两个难题与解决思路

#### 4.1.1 概念说明

把第 2 节的两个难题 formally 写清楚：

**难题一：专家稀疏激活。** 设一层有 \(E\) 个专家、每 token 激活 top-k。校准集有 \(N\) 个 token。正常前向时，专家 \(e\) 只收到被路由到它的 token 子集 \(T_e \subseteq \{1..N\}\)。当 \(|T_e|\) 很小，激活统计（如 \(\max|x|\)）的估计方差很大，量化参数不可靠。

llm-compressor 的对策是**校准时让每个专家都看到全部 token**，但**只把路由命中那些 token 的输出计入最终结果**。即统计阶段「过广播」，输出阶段「按路由筛选」。这样每个专家都有充足的样本，而模型整体行为不变。

**难题二：融合 3D 权重。** 设专家权重被融合成 3D 张量 \(W \in \mathbb{R}^{E\times O\times I}\)。量化算法期望的是 \(E\) 个 2D 矩阵 \(W_e \in \mathbb{R}^{O\times I}\)。

llm-compressor 的对策是**线性化**：用一个 `LinearExperts2D` 容器（本质是 `nn.ModuleList`）装 \(E\) 个独立 2D Linear，替换掉融合模块。替换是**数值等价**的：`forward` 重新实现融合模块的计算（路由 + 逐专家 MLP + 加权求和），只是权重存成了 2D。

#### 4.1.2 核心流程

两条机制在 `oneshot` 里被串联起来：

```text
oneshot(model, recipe, ...)
  │
  ├─ apply_recipe_modifiers
  │    ├─ get_non_linearized_moes(model) 非空？
  │    │     ├─ 是 → 打回退警告，调 linearize_moe(model)   # 难题二：加载后转换
  │    │     └─ 否 → 已线性化，跳过
  │    │
  │    └─ with ExitStack() as stack:
  │           ├─ norm_calibration_context(model)           # norm 偏移（u6-l2）
  │           ├─ 若 moe_calibrate_all_experts=True:
  │           │     stack.enter_context(moe_calibration_context())  # 难题一：全专家校准
  │           ├─ session.initialize(...)                    # 初始化 modifier
  │           └─ pipeline(model, dataloader, ...)           # sequential 逐子图校准
```

关键点：

1. **线性化是「尽力而为」**：`oneshot` 会在校准开始前检查模型里有没有未线性化的 MoE，有就当场转换并**警告**——因为这种「加载后转换」比「加载时直接以 2D 读入」慢（见 4.3）。
2. **全专家校准是一个上下文开关**：`moe_calibration_context()` 把一个模块级全局标志翻成 `True`，`LinearExperts2D.forward` 据此改变行为。
3. **更推荐的做法是加载时就线性化**：用 `load_quantizable_moe`（或包了它的 `load_context`）包裹 `from_pretrained`，让权重直接以 2D 形态读入，避免「3D→2D」往返。

#### 4.1.3 源码精读

`oneshot` 里这两步的实际代码：

[oneshot.py:32-33](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L32-L33) 导入本讲的两个关键函数；

[oneshot.py:232-238](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L232-L238) 是「难题二」的回退处理——`get_non_linearized_moes` 返回非空就警告并调 `linearize_moe`：

```python
if len(get_non_linearized_moes(self.model)) > 0:
    logger.warning(
        "Detected an MoE model which has not been linearized. First load "
        "model `with llmcompressor.modeling.moe.linearize.load_quantizable_moe`"
        " before passing to `oneshot`. Falling back to post-load linearization."
    )
    linearize_moe(self.model)
```

[oneshot.py:242-245](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L242-L245) 是「难题一」的开关——根据数据集参数 `moe_calibrate_all_experts`（默认 `True`）决定是否进入全专家校准上下文：

```python
with ExitStack() as stack:
    stack.enter_context(norm_calibration_context(self.model))
    if self.dataset_args.moe_calibrate_all_experts:
        stack.enter_context(moe_calibration_context())
```

`moe_calibrate_all_experts` 这个参数定义在 [dataset_arguments.py:194-205](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/dataset_arguments.py#L194-L205)，默认 `True`，文档原话是「all experts will see all tokens during calibration」。

#### 4.1.4 代码实践

**目标**：直观感受「回退警告」是在什么条件下触发的。

**操作步骤**（阅读型实践，无需 GPU）：

1. 打开 [oneshot.py:232-238](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L232-L238)，确认判断条件是「`get_non_linearized_moes(model)` 返回列表长度 > 0」。
2. 在你自己的一次 `oneshot` 调用中，如果看到日志里出现 `Falling back to post-load linearization.`，说明你的模型是**未提前线性化**进来的，`oneshot` 帮你做了较慢的「加载后转换」。
3. 对照阅读 [dev.py:35-52](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/utils/dev.py#L35-L52) 的 `load_context`——它同时进入 `load_offloaded_model` 与 `load_quantizable_moe` 两个上下文，是官方推荐的 MoE 加载方式。

**需要观察的现象**：用 `load_context()` 包裹 `from_pretrained` 后再 `oneshot`，日志里**不应再出现**回退警告。

**预期结果**：`get_non_linearized_moes` 在提前线性化的模型上返回空列表，警告分支被跳过。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `moe_calibrate_all_experts` 设成 `False`，校准会发生什么变化？

**答案**：`oneshot` 不会进入 `moe_calibration_context()`，`get_calibrate_all_experts_flag()` 保持 `False`，专家 `forward` 走「只喂被路由命中的 token」分支。结果是校准更快，但激活样本少的专家可能量化质量变差，甚至数值不稳。

**练习 2**：为什么 `oneshot` 检测到未线性化 MoE 时只警告而不报错？

**答案**：因为「加载后转换」虽然慢但能保证几乎所有 MoE 定义都被正确处理（见 4.2）。报错会阻断可用流程，而警告既提醒用户可以优化（用 `load_quantizable_moe`），又保证功能不中断。

---

### 4.2 专家线性化：把 3D 融合权重拆成 2D

#### 4.2.1 概念说明

线性化的目标是用一个数值等价的 `LinearExperts2D`（一个装着 \(E\) 个 2D Linear 的 `nn.ModuleList`）替换掉原始的融合专家模块。替换后：

- 量化算法能像对待普通 Linear 一样，给每个专家的 `gate_proj`/`up_proj`/`down_proj` 挂 observer、算 scale；
- sequential 管线的 `torch.fx` 能把每个专家当成普通子模块追踪、切子图；
- DDP 分布式量化时，专家可以被当作独立模块分配到各 rank。

llm-compressor 提供两条线性化路径，按「效率」从高到低：

1. **加载时转换（`load_quantizable_moe`）**：包裹 `from_pretrained`，让权重**直接以 2D 读入**，或对已存为 2D 的 checkpoint 跳过融合。最快。
2. **加载后转换（`linearize_moe`）**：模型已经按 3D 加载完了，再遍历模块、逐个替换。慢（可能经历 2D→3D→2D 往返），但兜底可用。

#### 4.2.2 核心流程

**`linearize_moe`（加载后转换）** 的流程：

```text
linearize_moe(model):
  non_linearized = get_non_linearized_moes(model)   # 找出所有融合专家模块
  if 空: return model                                # 已全部线性化，直接返回
  打印「加载后转换可能低效」的警告
  for (name, module) in non_linearized:
      cls  = LinearExperts2D.get_linear_experts_cls(type(module))  # 取/造对应的 2D 类
      new  = cls.from_experts_module(module, config)               # 从 3D 拷贝权重到 E 个 2D Linear
      model.set_submodule(name, new)                              # 原地替换
```

**`get_non_linearized_moes`（探针）** 用两条判据识别「专家模块」：

- 实现了 `FusedExpertsProtocol`（结构化协议，有 `down_proj` 且有 `up_proj`/`gate_up_proj`）；
- 或者在 `LinearExperts2D._registry` 里登记过（自定义定义，如 GraniteMoE）。

注意：已经被替换成 `LinearExperts2D` 的模块**不会**再被识别为「未线性化」，所以这个探针可以重复调用、幂等。

**`LinearExperts2D.get_linear_experts_cls`（取/造 2D 类）** 的三级查找：

1. 先查 `_registry`（针对非标准架构的显式自定义类，如 `GraniteMoeLinearExperts`）；
2. 否则解析原始专家类上的 `@use_experts_implementation(...)` 装饰器参数（`is_concatenated`/`is_transposed`/`has_bias`/`has_gate`），**动态创建**一个 `LinearExperts2D` 子类并登记；
3. 若连装饰器都没有 → 抛错（说明该架构需要手写自定义定义）。

#### 4.2.3 源码精读

`get_non_linearized_moes` 的识别逻辑——同时支持协议判据与注册表判据：

[linearize.py:128-144](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/moe/linearize.py#L128-L144)

```python
return [
    (name, module)
    for name, module in model.named_modules()
    if isinstance(module, FusedExpertsProtocol)
    or LinearExperts2D.get_registration(module.__class__) is not None
]
```

> `FusedExpertsProtocol` 是一个 `TorchModuleProtocol`，它的 `__validate__` 通过结构化属性（有 `down_proj` 且有 `up_proj`/`gate_up_proj`，且类带 `use_experts_implementation` 装饰器）做**鸭子类型**识别，见 [helpers.py:15-43](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/moe/helpers.py#L15-L43)。

`linearize_moe` 主体——先探针、再逐模块替换，并打印低效警告（提示去注册加载映射）：

[linearize.py:96-125](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/moe/linearize.py#L96-L125)

```python
non_linearized_moes = get_non_linearized_moes(model)
if len(non_linearized_moes) <= 0:
    return model
logger.warning("MoE is being linearized after loading ...")  # 提示去 add-moe-support 文档
for name, module in tqdm.tqdm(non_linearized_moes, desc="Linearizing experts"):
    config = getattr(module, "config", model.config)
    linear_experts_cls = LinearExperts2D.get_linear_experts_cls(module.__class__)
    linear_moe = linear_experts_cls.from_experts_module(module, config)
    model.set_submodule(name, linear_moe)
```

`LinearExperts2D.from_experts_module`——用 `skip_weights_initialize` 跳过随机初始化、再逐专家 `copy_from_experts_module` 把 3D 权重拷成 2D，最后把原模块的 offload 配置复制过来：

[linear_experts.py:190-207](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/moe/linear_experts.py#L190-L207)

每个 `ExpertMLP`（单个专家）由三个 2D Linear 组成，`copy_from_experts_module` 处理「是否转置 / 是否拼接 gate_up」等差异，见 [linear_experts.py:53-67](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/moe/linear_experts.py#L53-L67)。

#### 4.2.4 代码实践

**目标**：用 `get_non_linearized_moes` 验证「线性化前有未线性化模块、线性化后清零」。

**操作步骤**（基于 `tests/llmcompressor/modeling/test_linearize.py` 的 mock 模型思路，**示例代码**，待本地验证）：

```python
# 示例代码：构造一个只含一个融合专家模块的极简模型
import torch
from transformers.models.granitemoe.configuration_granitemoe import GraniteMoeConfig
from transformers.models.granitemoe.modeling_granitemoe import GraniteMoeParallelExperts
from llmcompressor.modeling.moe.linearize import get_non_linearized_moes, linearize_moe

config = GraniteMoeConfig(hidden_size=512, intermediate_size=1024)  # 内含 num_local_experts
experts = GraniteMoeParallelExperts(config.num_local_experts, config.hidden_size, config.intermediate_size)

class DummyModel(torch.nn.Module):
    def __init__(self, module, config):
        super().__init__()
        self.module = module
        self.config = config
    def forward(self, *a, **k):
        return self.module(*a, **k)

model = DummyModel(experts, config)

print("线性化前:", len(get_non_linearized_moes(model)))   # 预期 > 0
linearize_moe(model)
print("线性化后:", len(get_non_linearized_moes(model)))   # 预期 0
print("替换后类型:", type(model.module).__name__)          # 预期含 LinearExperts2D
```

**需要观察的现象**：

1. 线性化前 `get_non_linearized_moes` 返回长度 ≥ 1（识别到 `GraniteMoeParallelExperts`）；
2. `linearize_moe` 打印一条「MoE is being linearized after loading」警告 + 一个进度条；
3. 线性化后探针返回 0，`model.module` 变成 `GraniteMoeLinearExperts`（`LinearExperts2D` 子类）。

**预期结果**：与 `test_linearize.py::test_linearize_moe_granite` 的断言一致——`mock_model.module is not experts`（模块已被替换），且替换前后 `forward` 输出的 MSE 很小。

> 若本地无 GPU/无网下载，可直接 `pytest tests/llmcompressor/modeling/test_linearize.py -k granite -s` 观察同样的现象。

#### 4.2.5 小练习与答案

**练习 1**：`linearize_moe` 为什么在「列表为空」时直接 `return`，而不是报错说「没有 MoE 可线性化」？

**答案**：因为它要被无条件调用（如 `oneshot` 的回退分支）。对稠密模型或已线性化的 MoE 模型，探针返回空是正常情况，直接返回保证幂等与通用性。

**练习 2**：`from_experts_module` 为什么要包在 `skip_weights_initialize()` 里？

**答案**：创建 \(E\) 个 2D Linear 时默认会做随机初始化，但这些权重马上要被 `copy_` 覆盖。`skip_weights_initialize` 跳过无用的随机初始化，对大模型能显著省时间与显存（与逐层加载机制相关，详见 u6-l2）。

---

### 4.3 架构映射与高效加载：conversion_mappings

#### 4.3.1 概念说明

`linearize_moe` 是「加载后转换」，慢在可能经历 **2D → 3D → 2D** 往返：transformers 加载时先把 checkpoint 里的 2D 权重融合成 3D，`linearize_moe` 再拆回 2D。

`load_quantizable_moe` 的优化点是**让权重直接以 2D 读入**，跳过融合。它能这样做的前提是：该架构在 `conversion_mappings.py` 里登记了「2D 加载映射」。于是加载分成两条路径：

1. **有映射（直接 2D 加载）**：注册 patch（用 `LinearExperts2D` 替换专家类）+ 注册 checkpoint 转换映射，让 `from_pretrained` 直接读成 2D；加载后再挂上「保存映射」以便量化完写回。
2. **无映射（回退到加载后转换）**：正常 `from_pretrained`（读成 3D），再调 `linearize_moe`。

#### 4.3.2 核心流程

`load_quantizable_moe` 是一个上下文管理器，它**临时替换** `AutoModelForCausalLM.from_pretrained`：

```text
with load_quantizable_moe():              # 装上 patched from_pretrained
    model = AutoModelForCausalLM.from_pretrained(model_id)
        │
        ├─ AutoConfig.from_pretrained → model_type
        ├─ has_linearize_load_mappings(model_type)?
        │    ├─ 否（无映射）→ original_from_pretrained(...) 读成 3D → linearize_moe(model) 回退
        │    └─ 是（有映射）→
        │          ├─ get_linearize_load_mappings(model_type) → (experts_cls, load_map, save_map)
        │          ├─ LinearExperts2D.get_linear_experts_cls(experts_cls) → 2D 类
        │          ├─ register_patch_mapping({原专家类名: 2D类})     # from_pretrained 用 2D 类实例化
        │          ├─ register_checkpoint_conversion_mapping(load_map) # 直接按 2D 读权重
        │          ├─ model = original_from_pretrained(...)           # 这次直接得到 2D 模型
        │          ├─ clear_patch_mapping()
        │          └─ set_save_conversion_mapping(model, save_map)    # 为后续 save 预置逆映射
        └─ 退出上下文，恢复原 from_pretrained
```

判断「有没有映射」由 `has_linearize_load_mappings(model_type)` 决定，它要求**两个条件同时满足**：

1. `model_type in ARCH_TO_IMPORT_PATHS`——该架构的专家类能被 import（即 llm-compressor 知道它的专家在哪）；
2. `remapped_type in ARCH_TO_2D_MAPPINGS`——该架构有显式的 2D 重命名规则（能跳过融合直接读 2D）。

条件 1 对几乎所有 transformers 支持的 MoE 架构都成立（`ARCH_TO_IMPORT_PATHS` 是一张大表）；条件 2 目前只对少数架构成立（`ARCH_TO_2D_MAPPINGS` 较小）。因此**大多数架构仍走「无映射回退」路径**，这也是回退警告常见的原因。

#### 4.3.3 源码精读

`ARCH_TO_IMPORT_PATHS` 是「架构 →（config 类, 专家类）」的大表，覆盖 afmoe、cohere2_moe、deepseek_v3/v4、gemma4、glm4_moe、gpt_oss、granitemoe、llama4、mixtral、olmoe、phimoe、qwen2_moe、qwen3_moe 等几十种架构：

[conversion_mappings.py:22-162](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/moe/conversion_mappings.py#L22-L162)

注意有些架构的专家类是**列表**（如 `deepseek_v3`、`granitemoe`、`glm4_moe`），用 `import_or_none` 逐个尝试，兼容不同 transformers 版本里专家类的不同命名。

`ARCH_TO_2D_MAPPINGS` 是「架构 → (要移除的 3D 目标, 2D 重命名规则)」的小表，目前含 `deepseek_v4`、`qwen2_moe`、`hy_v3`。以 `qwen2_moe` 为例，它声明把 `mlp.experts.gate_up_proj` / `mlp.experts.down_proj` 这两个 3D 目标「移除」，并用一组 `WeightRenaming` 把逐专家的 2D 权重名直接映射到位：

[conversion_mappings.py:164-216](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/moe/conversion_mappings.py#L164-L216)

`has_linearize_load_mappings` 的双条件判断（注意第二个条件用的是经 `_MODEL_TO_CONVERSION_PATTERN` 重映射后的类型）：

[conversion_mappings.py:219-221](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/moe/conversion_mappings.py#L219-L221)

```python
def has_linearize_load_mappings(model_type: str) -> bool:
    remapped_type = _MODEL_TO_CONVERSION_PATTERN.get(model_type, model_type)
    return model_type in ARCH_TO_IMPORT_PATHS and remapped_type in ARCH_TO_2D_MAPPINGS
```

`get_linearize_load_mappings` 组装出 `(experts_cls, load_mappings, save_mappings)`——**正向（load）追加 2D 重命名、反向（save）只保留不涉及 3D 目标的转换**，从而读时按 2D、存时也按 2D：

[conversion_mappings.py:224-258](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/moe/conversion_mappings.py#L224-L258)

`load_quantizable_moe` 的 patched `from_pretrained`——上面流程图的代码实现，分「无映射回退」与「有映射直接 2D」两支：

[linearize.py:52-82](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/moe/linearize.py#L52-L82)

其中「无映射回退」分支就是简单调原始加载再 `linearize_moe`：

```python
if not has_linearize_load_mappings(model_type):
    model = original_from_pretrained(*args, **kwargs)
    linearize_moe(model)
    return model
```

`load_quantizable_moe` 本身用 `patch_attr` 临时替换类方法，退出时若 `from_pretrained` 从没被调过则警告（提示你传错了 model_cls）：

[linearize.py:29-48](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/moe/linearize.py#L29-L48) 与 [linearize.py:84-93](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/moe/linearize.py#L84-L93)

#### 4.3.4 代码实践

**目标**：搞清楚一个给定架构会走「直接 2D 加载」还是「加载后回退」。

**操作步骤**（阅读型实践）：

1. 在 [conversion_mappings.py:22-162](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/moe/conversion_mappings.py#L22-L162) 的 `ARCH_TO_IMPORT_PATHS` 里查你关心的 `model_type`（例如 `granitemoe`、`llama4`、`qwen2_moe`）。
2. 再去 [conversion_mappings.py:164-216](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/moe/conversion_mappings.py#L164-L216) 的 `ARCH_TO_2D_MAPPINGS` 里查同一个 key。
3. 用 `has_linearize_load_mappings` 的双条件推断结果：
   - `qwen2_moe`：两张表都有 → **直接 2D 加载**；
   - `granitemoe` / `llama4`：只在 `ARCH_TO_IMPORT_PATHS` → **加载后回退**（`linearize_moe`），但因它们有自定义定义（`granitemoe.py`/`llama4.py`），回退仍能正确工作。

**需要观察的现象**：把你的推断与「`oneshot` 是否打印回退警告」对照——只有走回退路径的架构才会触发那条警告。

**预期结果**：`qwen2_moe` 类架构用 `load_quantizable_moe` 时不警告（直接 2D）；`granitemoe`/`llama4` 会警告但功能正常。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ARCH_TO_2D_MAPPINGS` 故意只列少数架构，而不是把所有架构都加上以获得最快加载？

**答案**：写 2D 重命名映射需要精确知道 checkpoint 里逐专家权重的原始命名，且要随 transformers 版本维护、加测试（文档要求新映射补 `test_linearize.py`）。而「加载后回退」对几乎所有走标准 `use_experts_implementation` 的架构都能正确工作。因此只对高价值架构维护 2D 映射，其余兜底回退，是正确性与维护成本的权衡。

**练习 2**：`set_save_conversion_mapping` 把 `save_mappings` 挂到 `model._weight_conversions` 上，作用是什么？

**答案**：量化完成后 `save_pretrained` 时，transformers 会用这些映射的**逆**把 2D 权重写回 checkpoint（见 [conversion_mappings.py:261-272](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/moe/conversion_mappings.py#L261-L272)）。这保证读时按 2D 校准、存时也按 2D（或按架构需要融合），产物能被 vLLM 正确加载。

---

### 4.4 专家感知校准上下文：moe_calibration_context

#### 4.4.1 概念说明

`moe_calibration_context` 解决的是第 4.1 节的**难题一**（专家稀疏激活）。它的实现极其精简：一个模块级全局布尔变量 `_CALIBRATE_ALL_EXPERTS`，加一个上下文管理器在进入时把它置 `True`、退出时恢复。

真正「让所有专家看到所有 token」的逻辑不在 `context.py` 里，而在 **`LinearExperts2D.forward`**（以及各自定义定义的 `forward`）里——它们读取 `get_calibrate_all_experts_flag()`，据此在两条前向路径间切换：

- **标志为 `False`（正常前向/推理）**：每个专家只处理被路由到它的 token 子集 \(x[T_e]\)。
- **标志为 `True`（校准）**：每个专家处理**全部** token \(x\)，但只取路由命中位置 \(T_e\) 的输出计入加权求和。

数学上，对专家 \(e\) 的输出 \(y_e = f_e(x)\)：

- 正常：\(y_e\) 只在 \(T_e\) 上有意义，贡献为 \(\sum_{i\in T_e} g_i \cdot f_e(x_i)\)；
- 校准：\(f_e\) 对**所有** \(i\) 计算（让 observer 收到全部 token 的统计），但最终仍只把 \(i\in T_e\) 的 \(g_i f_e(x_i)\) 累加，所以**模型输出不变**，只是统计更充分。

这种「广播计算 + 路由筛选」是 llm-compressor（及其他量化框架）对 MoE 校准的标准做法。

#### 4.4.2 核心流程

```text
oneshot 里 stack.enter_context(moe_calibration_context())
    │  _CALIBRATE_ALL_EXPERTS := True（保存旧值）
    │
    ▼  校准期间 sequential 管线逐子图前向
LinearExperts2D.forward(hidden_states, top_k_index, top_k_weights):
    for e in range(num_experts):
        算 expert_mask[e] → 命中的 (top_k_pos, token_indices)
        if get_calibrate_all_experts_flag():       # 校准：喂全部 token
            out = expert(hidden_states)[token_indices]
        else:                                       # 正常：只喂命中 token
            out = expert(hidden_states[token_indices])
        weighted = out * top_k_weights[token_indices, top_k_pos]
        final.index_add_(0, token_indices, weighted)   # 只把命中位置写回
    │
    ▼  退出上下文
    _CALIBRATE_ALL_EXPERTS := restore_value（恢复旧值，通常是 False）
```

两条路径的**唯一区别**是 `expert(...)` 收到的输入大小：校准时是全部 token（统计充分），正常时是命中子集（省算力）。最终 `index_add_` 都只写路由命中的位置，保证输出等价。

#### 4.4.3 源码精读

`context.py` 全文非常短——一个全局标志 + getter + 上下文管理器：

[context.py:1-18](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/moe/context.py#L1-L18)

```python
_CALIBRATE_ALL_EXPERTS = False

def get_calibrate_all_experts_flag() -> bool:
    return _CALIBRATE_ALL_EXPERTS

@contextlib.contextmanager
def moe_calibration_context():
    global _CALIBRATE_ALL_EXPERTS
    restore_value, _CALIBRATE_ALL_EXPERTS = _CALIBRATE_ALL_EXPERTS, True
    try:
        yield
    finally:
        _CALIBRATE_ALL_EXPERTS = restore_value
```

> 注意是「保存旧值再恢复」而非「无条件置回 False」——这样嵌套使用也不会破坏外层状态。这也是为什么它配合 `ExitStack` 使用是安全的（异常也会在 `finally` 里恢复）。

`LinearExperts2D.forward` 里读取标志、在两条路径间切换——注意 `index_add_` 始终只写 `token_indices`，这是「输出等价」的关键：

[linear_experts.py:236-269](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/moe/linear_experts.py#L236-L269)

```python
expert = self[expert_index]
if get_calibrate_all_experts_flag():
    expert_output = expert(hidden_states)[token_indices]          # 喂全部 token
else:
    expert_output = expert(hidden_states[token_indices])          # 只喂命中 token
expert_weights = top_k_weights[token_indices, top_k_pos, None]
weighted_output = expert_output * expert_weights
final_hidden_states.index_add_(0, token_indices, weighted_output) # 只写命中位置
```

自定义定义（不走标准装饰器的架构）也遵循同一约定。以 GraniteMoE 为例，它的 `forward` 同样用 `get_calibrate_all_experts_flag()` 切换：

[granitemoe.py:66-86](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/moe/granitemoe.py#L66-L86)

```python
for i in range(self.num_experts):
    if get_calibrate_all_experts_flag():
        expert_out = self[i](inputs).split(expert_size, dim=0)[i]   # 全部 token 后再切
    else:
        expert_out = self[i](inputs.split(expert_size, dim=0)[i])   # 先切再算
    output_list.append(expert_out)
```

这也是 `docs/developer-tutorials/add-moe-support.md` 对「新增自定义 MoE 定义」的硬性要求——`forward` 必须用 `get_calibrate_all_experts_flag()` 区分两条路径。

#### 4.4.4 代码实践

**目标**：用前向 hook 证明「校准上下文里每个专家确实收到了全部 token，且输出形状不变」。

**操作步骤**（**示例代码**，改编自 `tests/llmcompressor/modeling/test_linear_experts.py`，可在纯 CPU 跑，待本地验证）：

```python
# 示例代码
import torch
from transformers.models.qwen3_moe.configuration_qwen3_moe import Qwen3MoeConfig
from transformers.models.qwen3_moe.modeling_qwen3_moe import Qwen3MoeExperts
from llmcompressor.modeling.moe.context import moe_calibration_context
from llmcompressor.modeling.moe.linear_experts import LinearExperts2D

config = Qwen3MoeConfig(hidden_size=16, intermediate_size=32,
                        num_experts=4, num_experts_per_tok=2)
cls = LinearExperts2D.get_linear_experts_cls(Qwen3MoeExperts)
experts = cls(config)

# 记录每个专家收到的 token 数
seen = {}
def make_hook(i):
    def hook(_, inp, __):
        seen[i] = inp[0].size(0)
    return hook
for i in range(experts.num_experts):
    experts[i].register_forward_hook(make_hook(i))

num_tokens = 16
x = torch.randn(num_tokens, config.hidden_size)
top_k_index = torch.tensor([[0],[1],[2],[3]])   # 每个专家只被 1 个 token 路由到
top_k_weights = torch.randn(num_tokens, 1)

seen.clear(); experts(x, top_k_index, top_k_weights)
print("无校准上下文，各专家收到 token 数:", list(seen.values()))   # 预期 [1,1,1,1]

seen.clear()
with moe_calibration_context():
    out = experts(x, top_k_index, top_k_weights)
print("校准上下文内，各专家收到 token 数:", list(seen.values()))    # 预期 [16,16,16,16]
print("输出形状:", tuple(out.shape))                              # 预期 (16, 16) == 输入形状
```

**需要观察的现象**：

1. 无上下文时每个专家收到 1 个 token（路由稀疏）；
2. 进入 `moe_calibration_context()` 后每个专家收到全部 16 个 token；
3. 两种模式下输出形状都与输入相同、且不含 NaN（输出在路由命中位置等价）。

**预期结果**：与 `test_linear_experts.py::test_linear_experts_2d_with_hooks` 的断言完全一致——无上下文 `input_size == 1`，有上下文 `input_size == num_tokens`。

#### 4.4.5 小练习与答案

**练习 1**：`moe_calibration_context` 用「保存旧值、退出恢复」而不是「退出置 False」，好处是什么？

**答案**：支持嵌套与异常安全。若外层已经把标志设成某个值，内层 `with` 退出后能恢复到外层的值而非粗暴地置 `False`；`finally` 块则保证即使校准中抛异常也能恢复，避免标志卡在 `True` 污染后续推理。

**练习 2**：校准时让所有专家处理全部 token，模型整体输出为什么仍然不变？

**答案**：因为 `forward` 最后用 `index_add_(0, token_indices, ...)` 只把**路由命中位置** `token_indices` 的加权输出写回 `final_hidden_states`。虽然每个专家对全部 token 都算了，但非命中位置的输出被丢弃，最终累加结果与「只算命中 token」一致——多算的那部分只为喂给 observer 做统计。

---

## 5. 综合实践

把本讲三条线索串起来：**线性化（4.2）让专家变成可校准的 2D 模块，全专家校准上下文（4.4）让每个专家都拿到充分统计，架构映射（4.3）决定加载走快路径还是回退路径**。

**任务**：写一个脚本，对比「直接 `from_pretrained`（不预线性化）」与「`load_quantizable_moe` 预线性化」两种方式加载同一个 MoE 模型，验证两者最终都达到「无可线性化模块」状态，并体会 `oneshot` 回退警告的触发条件。

**操作步骤**（**示例代码**，待本地验证）：

```python
# 示例代码
from transformers import AutoModelForCausalLM
from llmcompressor.modeling.moe.linearize import (
    load_quantizable_moe, get_non_linearized_moes,
)

model_id = "<一个 MoE 模型 id，如 small-moe>"

# 方式 A：不预线性化（模拟用户直接把模型丢给 oneshot）
model_a = AutoModelForCausalLM.from_pretrained(model_id)
print("A 加载后未线性化 MoE 数:", len(get_non_linearized_moes(model_a)))
# ↑ 预期 > 0；若把这个 model_a 传给 oneshot，就会触发回退警告

# 方式 B：用 load_quantizable_moe 预线性化（推荐）
with load_quantizable_moe():
    model_b = AutoModelForCausalLM.from_pretrained(model_id)
print("B 加载后未线性化 MoE 数:", len(get_non_linearized_moes(model_b)))
# ↑ 预期 0（load_quantizable_moe 内部已转换完毕）
```

**延伸思考（可写成你的观察笔记）**：

1. 方式 A 的 `model_a` 如果直接传给 `oneshot`，日志会打印哪条警告？为什么？（对应 [oneshot.py:232-238](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L232-L238)）
2. 方式 B 内部，你加载的 `model_type` 走的是「直接 2D 加载」还是「加载后回退」？（用 4.3.4 的方法查 `has_linearize_load_mappings`）
3. 若把方式 B 得到的 `model_b` 传给 `oneshot`，并把 `moe_calibrate_all_experts=False`，校准行为会如何变化？（对应 4.4）

**预期结果**：方式 B 得到的模型 `get_non_linearized_moes` 为空，`oneshot` 不再打印回退警告；方式 A 则相反。两者最终量化产物在数值上应接近，但方式 B 加载更快、更省内存。

> 若无合适 MoE 模型或无 GPU，可降级为阅读 `tests/llmcompressor/modeling/test_linearize.py` 与 `test_linear_experts.py`，按上面的断言逐条理解现象。

## 6. 本讲小结

- MoE 校准有**两个特有难题**：专家稀疏激活（统计样本不足）与融合 3D 权重（量化算法只认 2D），llm-compressor 分别用「全专家校准上下文」与「线性化」解决。
- **线性化**用 `LinearExperts2D`（装 \(E\) 个 2D Linear 的 `ModuleList`）数值等价地替换融合专家模块；`linearize_moe` 是「加载后转换」（兜底、较慢），`load_quantizable_moe` 是「加载时转换」（推荐、最快）。
- **`get_non_linearized_moes`** 用「`FusedExpertsProtocol` 协议判据 + `LinearExperts2D._registry` 注册表判据」识别专家模块，且对已线性化模块幂等返回空。
- **`load_quantizable_moe`** 按 `has_linearize_load_mappings` 分两路：有 2D 映射则直接按 2D 读入（注册 patch + checkpoint 映射），无映射则回退到 `linearize_moe`。
- **`conversion_mappings`** 维护两张表：`ARCH_TO_IMPORT_PATHS`（架构→专家类，覆盖广）决定「认不认识」，`ARCH_TO_2D_MAPPINGS`（架构→2D 重命名，较窄）决定「能不能直接 2D 加载」。
- **`moe_calibration_context`** 只是一个全局开关；真正改变行为的是 `LinearExperts2D.forward` 读 `get_calibrate_all_experts_flag()`，在校准时给每个专家喂全部 token、但只把路由命中位置的输出写回，从而「统计充分、输出等价」。

## 7. 下一步学习建议

- **接 u4 算法层**：线性化把专家变成普通 2D Linear 后，GPTQ/AWQ/SmoothQuant 就能逐专家校准。建议读 [u4-l1 GPTQ 算法实现](u4-l1-gptq-algorithm.md)，关注它的 Hessian 累积如何作用在线性化后的专家权重上。
- **接 u6 规模化层**：`load_quantizable_moe` 通常和逐层加载/磁盘 offloading 一起用（`load_context` 同时进入两者）。读 [u6-l2 大模型内存策略](u6-l2-big-model-memory.md) 理解 `load_offloaded_model` 与线性化如何协同压低峰值显存。
- **扩展阅读**：若你要为新架构加 MoE 支持，直接读 [docs/developer-tutorials/add-moe-support.md](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/developer-tutorials/add-moe-support.md) 与 `granitemoe.py`/`llama4.py` 这两个自定义定义范例，并按文档要求为新映射补 `test_linearize.py` 用例。
- **回顾依赖**：若对「线性化后的专家如何被切子图、逐层校准」还有疑问，回到 [u3-l5 SequentialPipeline](u3-l5-sequential-pipeline.md) 复习 `trace_subgraphs` 与两遍前向。
