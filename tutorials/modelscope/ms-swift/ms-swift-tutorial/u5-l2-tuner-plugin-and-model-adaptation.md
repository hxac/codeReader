# TunerPlugin 与模型适配

## 1. 本讲目标

本讲聚焦 ms-swift 训练链路中「**如何把一个加载好的基座模型，变成可以训练的微调模型**」这一关键环节。读者学完后应该能够：

- 理解 `Tuner` 基类用三个静态方法（`prepare_model`/`save_pretrained`/`from_pretrained`）定义的统一契约，以及它为什么是「训练—保存—加载」闭环的核心。
- 掌握 `tuners_map` 注册表的含义：哪些 tuner 走自定义 `Tuner` 子类、哪些走默认 peft 路径，以及两者如何分派。
- 读懂 `TunerMixin.prepare_model` 的编排逻辑：它如何先冻结、再按 `tuner_type` 选择挂载方式，并在「全量微调」与「adapter 微调」之间分流。
- 能够跟踪一次 `swift sft` 中 `prepare_model` 的完整调用链，解释可训练参数如何被挂载、其余参数如何被冻结、checkpoint 如何只保存 adapter。

本讲承接 [u3-l1 模型注册与加载机制](u3-l1-model-registry-and-loading.md)：那一讲解决了「模型 id 如何变成 `(model, processor)`」，本讲解决「这个 model 如何被改造成可训练的微调模型」。

## 2. 前置知识

在进入源码前，先用通俗语言澄清三个概念。

**微调方式是正交维度。** 回顾 [u1-l1](u1-l1-project-overview.md) 的结论：ms-swift 的训练沿三个正交维度自由组合——训练任务（pt/sft/rlhf）、微调方式（`--tuner_type`：full/lora/qlora/...）、推理或并行后端。本讲只关心第二个维度：`tuner_type`。`full` 表示全量微调（训练所有参数），`lora`/`llamapro`/`vera`/`ia3` 等表示「挂载一个轻量增量模块，只训练它」，这类方法统称为 **adapter（适配器）微调**。

**什么是 adapter？** 以 LoRA 为例：它不改动原模型权重，而是在某些 `Linear` 层旁路并联一个低秩矩阵 \(BA\)（其中 \(A\in\mathbb{R}^{r\times d}\)、\(B\in\mathbb{R}^{d\times r}\)，秩 \(r\) 远小于维度 \(d\)）。前向变成：

\[
y = Wx + BAx
\]

训练时冻结 \(W\)、只更新 \(A,B\)。由于 \(r\ll d\)，可训练参数量从几十亿降到几千万，显存与存储都大幅下降。保存时只需存 \(A,B\)（称为 adapter 权重），推理时再把 \(BA\) 合并回 \(W\) 或动态挂载。

**为什么需要一层 `Tuner` 抽象？** 对上层（pipeline 与 trainer）而言，「把模型改造成可训练」需要三个动作，且每种微调方式实现不同：

| 动作 | 说明 | 因微调方式而异 |
| --- | --- | --- |
| `prepare_model` | 在基座模型上挂载可训练模块、冻结其余参数 | LoRA 挂在 Linear、LLaMAPro 插入新 block、全量则全部解冻 |
| `save_pretrained` | 把可训练参数落盘 | adapter 只存增量；`lora_llm` 还要额外存 ViT 权重 |
| `from_pretrained` | 从 checkpoint 重新挂载增量 | adapter 读 `adapter_config.json`；`lora_llm` 还要读 `vit.safetensors` |

上层调用方不应关心这些差异，因此 ms-swift 把这三个动作抽象成 `Tuner` 基类的三个静态方法，所有具体微调方式都遵循同一契约。这就是本讲的核心——`tuner_plugin` 模块。

> 术语提示：注意区分两个「注册表」。本讲的 `tuners_map`（`swift/tuner_plugin/mapping.py`，仅 3 项）是「**需要自定义保存/加载逻辑的 Tuner 类**」的注册表；而 `swift/tuners/mapping.py` 里的 `SWIFT_MAPPING` 是「swift 原生 adapter 实现（LoRA/LLaMAPro/Adapter 等的算法类）」的注册表。两者职责不同，下文会详细说明它们的协作关系。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `swift/tuner_plugin/base.py` | 定义 `Tuner` 基类（三个静态方法契约）与 `PeftTuner`（基于 peft 的默认 save/load 实现） |
| `swift/tuner_plugin/mapping.py` | `tuners_map` 注册表，收录需要自定义逻辑的 3 个 Tuner |
| `swift/tuner_plugin/dummy.py` | `DummyTuner`：占位 Tuner，什么都不挂载 |
| `swift/tuner_plugin/ia3.py` | `IA3Tuner`：IA³ 微调示例，展示如何继承 `PeftTuner` |
| `swift/tuner_plugin/lora_llm.py` | `LoRALLMTuner`：多模态「LLM 走 LoRA、ViT/Aligner 全量」的混合策略，演示为何要自定义 save/load |
| `swift/pipelines/train/tuner.py` | 编排核心：`TunerMixin.prepare_model`、`prepare_adapter`、`get_target_modules` 等 |
| `swift/pipelines/train/sft.py` | `SwiftSft` 管道，在 `run()` 中调用 `self.prepare_model(...)` |
| `swift/trainers/mixin.py` | `_save_model`：训练器保存 checkpoint 时按 `tuners_map` 分派到 `Tuner.save_pretrained` |
| `swift/tuners/base.py` | `Swift.prepare_model`：统一封装 peft 与 swift 原生 adapter 的模型包装 |
| `swift/utils/transformers_utils.py` | `freeze_parameters`/`activate_parameters`/`find_all_linears` 等冻结与扫描工具 |
| `swift/arguments/base_args/base_args.py` | `is_adapter` 属性、`tuner_type` 字段、`get_supported_tuners` |

## 4. 核心概念与源码讲解

### 4.1 Tuner 基类与三个静态方法契约

#### 4.1.1 概念说明

`Tuner` 是所有「微调方式适配器」的抽象基类。它不持有任何状态、不实现具体算法，只**用三个静态方法声明契约**：给定一个基座模型，告诉框架「怎么挂训练参数、怎么存、怎么读」。这种「以静态方法契约定义能力、由子类填实现」的设计，让上层可以用统一代码路径处理 LoRA、IA³、全量等完全不同的微调方式。

为什么用静态方法而不是实例方法？因为微调方式的「配置」已经全部存在于 `args`（一个 `SftArguments` 数据类）里，`Tuner` 本身不需要保存额外状态——它只是一个无状态的函数集合，把「模型 + 参数」映射成「可训练模型」。

#### 4.1.2 核心流程

`Tuner` 的三个方法构成一个闭环：

```
prepare_model(args, model)  ──►  可训练模型（训练时调用）
          │
          ▼
save_pretrained(model, dir) ──►  checkpoint 目录（trainer 落盘）
          │
          ▼
from_pretrained(model, dir) ──►  重新挂载的可训练模型（断点续训 / 推理）
```

- `prepare_model`：训练**开始前**调用一次，负责把 adapter 挂到基座上、冻结不该训练的参数。
- `save_pretrained`：训练过程中由 trainer 周期性调用，负责把**可训练参数**（而非整个模型）落盘。
- `from_pretrained`：断点续训或推理时调用，负责把基座模型与落盘的 adapter 重新拼装起来。

#### 4.1.3 源码精读

`Tuner` 基类的完整定义，三个方法体都只是 `raise NotImplementedError`，即「必须由子类实现」：

[swift/tuner_plugin/base.py:L10-L58](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuner_plugin/base.py#L10-L58) — 定义 `Tuner` 抽象基类与三个静态方法契约 `prepare_model`/`save_pretrained`/`from_pretrained`，方法体仅声明类型与文档、用 `raise NotImplementedError` 强制子类实现。

注意三个方法的签名设计要点：

- `prepare_model(args, model)`：第一个参数是**训练参数对象**而非散落的配置，因为挂载方式依赖大量参数（`lora_rank`、`target_modules`、`tuner_backend` 等），直接传整个 `args` 最简洁。
- `save_pretrained(model, save_directory, state_dict=None, ...)`：`state_dict` 可选，DeepSpeed ZeRO 训练时由框架传入「只含可训练参数」的字典（因为 ZeRO-3 下完整 state_dict 需要跨卡汇聚）。
- `from_pretrained(model, model_id, ...)`：`model_id` 是 checkpoint 目录路径或模型库 id。

`PeftTuner` 是关键的中间层——它继承 `Tuner`，但**只覆写 `save_pretrained` 和 `from_pretrained`**，因为绝大多数 adapter 都基于 peft 库，保存与加载逻辑通用，不必每个子类重写：

[swift/tuner_plugin/base.py:L61-L80](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuner_plugin/base.py#L61-L80) — `PeftTuner` 给出基于 peft 的默认实现：保存时若模型是 `PeftModel` 则默认只存 `default` adapter，加载时直接委托 `PeftModel.from_pretrained`。

`PeftTuner.save_pretrained` 的细节值得注意：当 `model` 是 `PeftModel` 且调用方没指定 `selected_adapters` 时，默认只保存名为 `default` 的 adapter（peft 支持多 adapter 共存，ms-swift 训练场景只用一个）。这样调用方无需关心 peft 的多 adapter 细节。

#### 4.1.4 代码实践

**实践目标**：通过阅读 `Tuner` 基类，理解「契约即接口」的设计。

**操作步骤**：

1. 打开 [swift/tuner_plugin/base.py:L10-L58](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuner_plugin/base.py#L10-L58)，对照三个方法的 docstring。
2. 思考：如果你要实现一种新的微调方式（比如某种自定义旁路模块），你需要实现哪几个方法？哪些可以复用 `PeftTuner` 的默认实现？
3. 用 Python 在本地加载该模块（需先 `pip install -e .` 安装 ms-swift），观察类型关系：

```python
# 示例代码：仅用于观察类继承关系，不真正训练
from swift.tuner_plugin import Tuner, PeftTuner, tuners_map
print('Tuner 的三个契约方法:', [m for m in ('prepare_model', 'save_pretrained', 'from_pretrained')])
print('PeftTuner 是 Tuner 子类:', issubclass(PeftTuner, Tuner))
print('tuners_map 当前内容:', tuners_map)
```

**需要观察的现象**：`PeftTuner` 是 `Tuner` 的子类；`tuners_map` 只包含 3 个键。

**预期结果**：输出显示 `PeftTuner` 继承自 `Tuner`，且 `tuners_map` 含 `ia3`/`lora_llm`/`dummy` 三项。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Tuner` 用静态方法而不是实例方法？如果改成实例方法会带来什么问题？

> **参考答案**：微调方式的所有配置都已在 `args`（数据类）中，`Tuner` 本身无需持有状态；用静态方法可以把它当作无状态的「函数集合」直接按 `tuners_map[tuner_type]` 索引调用，无需先实例化。若改成实例方法，则需要先 `tuner = SomeTuner()` 再调用，多一步无意义的构造，且没有需要在 `__init__` 中保存的字段。

**练习 2**：`PeftTuner` 为什么只覆写 `save_pretrained`/`from_pretrained` 而不覆写 `prepare_model`？

> **参考答案**：因为不同 peft adapter（LoRA/IA³/Vera...）的**挂载方式各不相同**（不同的 Config、不同的 target_modules 解析），`prepare_model` 无法给出通用默认实现，必须由各子类按自身算法覆写；而 peft 模型的**保存与加载逻辑是统一的**（都是 `model.save_pretrained` / `PeftModel.from_pretrained`），所以 `PeftTuner` 在这两者上提供默认实现，子类直接继承即可。

### 4.2 tuners_map 注册表与 Tuner 子类

#### 4.2.1 概念说明

`tuners_map` 是一个简单的字典，把 `tuner_type` 字符串映射到具体的 `Tuner` 子类。但**它不是全部微调方式的注册表**——它只收录「**需要自定义保存/加载逻辑**」的那一小撮 tuner。

ms-swift 实际上采用了**双轨制**来分派微调方式：

1. **tuners_map 轨道**：`ia3`、`lora_llm`、`dummy` 这 3 个。它们有专门的 `Tuner` 子类，`prepare_model`/`save_pretrained`/`from_pretrained` 全部自定义。
2. **prepare_adapter 轨道**：`lora`/`longlora`/`adalora`/`llamapro`/`adapter`/`vera`/`boft`/`fourierft`/`reft`/`bone` 等。它们**不在** `tuners_map`，而是由 `prepare_adapter` 这个大函数分发，最终统一走 `Swift.prepare_model`，序列化复用 peft 默认逻辑。

`get_supported_tuners()` 把两条轨道合并，得到用户可选的全部 `tuner_type`：

[swift/arguments/base_args/base_args.py:L28-L30](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L28-L30) — `get_supported_tuners` 返回「硬编码的 11 种 tuner」与 `tuners_map.keys()` 的并集，即用户可选的全部微调方式（含 `full` 全量微调）。

#### 4.2.2 核心流程

`tuners_map` 的内容与各子类职责：

```
tuners_map = {
  'ia3'      : IA3Tuner      # 示例：演示如何继承 PeftTuner
  'lora_llm' : LoRALLMTuner  # 多模态混合：LLM 用 LoRA、ViT/Aligner 全量
  'dummy'    : DummyTuner    # 占位：不做任何挂载
}
```

[swift/tuner_plugin/mapping.py:L6-L10](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuner_plugin/mapping.py#L6-L10) — `tuners_map` 显式字典，把 3 个 `tuner_type` 映射到对应 `Tuner` 子类。

三个子类各有代表性：

- `DummyTuner`（最简）：

[swift/tuner_plugin/dummy.py:L11-L15](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuner_plugin/dummy.py#L11-L15) — `DummyTuner.prepare_model` 直接返回原模型、不挂任何 adapter，其余 save/load 继承 `PeftTuner`，是「什么都不做」的占位实现。

- `IA3Tuner`（教学示例）：

[swift/tuner_plugin/ia3.py:L15-L22](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuner_plugin/ia3.py#L15-L22) — `IA3Tuner.prepare_model` 用 `find_all_linears` 扫出所有线性层作为 target、依据 `model_arch.mlp` 标记 feedforward 模块，构造 `IA3Config` 后 `get_peft_model` 包装模型。它只覆写 `prepare_model`，保存/加载完全复用 `PeftTuner` 默认实现。

这里体现了 [u3-l2](u3-l2-model-arch-and-keys.md) 讲过的 `ModelKeys`：`model_arch.mlp.split('{}.')[1]` 从形如 `model.layers.{}.mlp` 的路径中取出 `mlp`，用于区分注意力线性层与 FFN 线性层（IA³ 只对 FFN 做乘性缩放）。

- `LoRALLMTuner`（最复杂，演示为何需要自定义 Tuner）：

[swift/tuner_plugin/lora_llm.py:L25-L78](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuner_plugin/lora_llm.py#L25-L78) — `LoRALLMTuner` 实现「LLM 走 LoRA、ViT/Aligner 全量训练」的混合策略：`prepare_model` 用 `get_multimodal_target_regex` 只对 LLM 部分挂 LoRA，再把 vision_tower 与 aligner 解冻；`save_pretrained` 除保存 LoRA adapter 外，**额外**把 ViT/Aligner 权重写入 `vit.safetensors`；`from_pretrained` 读回 LoRA 后再单独加载 `vit.safetensors`。

这个子类是理解「为什么要有 `tuners_map`」的最佳例子：因为它需要保存**两类**权重（peft 的 adapter + 额外的 ViT 全量权重），peft 默认的 `save_pretrained` 不够用，所以必须自定义并注册进 `tuners_map`。注意它**直接继承 `Tuner`** 而非 `PeftTuner`，因为它的 save/load 逻辑与 peft 默认差异太大。

#### 4.2.3 源码精读（关键细节）

`LoRALLMTuner.save_pretrained` 的两段式保存是本模块最值得读的代码：

[swift/tuner_plugin/lora_llm.py:L48-L66](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuner_plugin/lora_llm.py#L48-L66) — 先收集所有 `requires_grad` 的参数（含 LoRA 与 ViT/Aligner 全量）调用 `model.save_pretrained` 存 adapter，再用 `is_vit_aligner_param` 过滤出 ViT/Aligner 参数单独写入 `vit.safetensors`。

它还处理了 DeepSpeed ZeRO-3 的特殊情况：在 ZeRO-3 下参数被分片，不能直接 `load_state_dict`，需用 `deepspeed.zero.GatheredParameters` 先汇聚（见 `from_pretrained` 的 L32-40 分支）。

#### 4.2.4 代码实践

**实践目标**：验证 `tuners_map` 与 `get_supported_tuners` 的双轨关系。

**操作步骤**：

1. 阅读 [swift/arguments/base_args/base_args.py:L28-L30](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L28-L30)。
2. 在本地运行以下示例代码，对比两条轨道：

```python
# 示例代码：观察双轨制
from swift.tuner_plugin import tuners_map
from swift.arguments.base_args.base_args import get_supported_tuners

custom_track = set(tuners_map.keys())               # 自定义 Tuner 轨道
all_supported = get_supported_tuners()              # 全部可选 tuner_type
prepare_adapter_track = all_supported - custom_track - {'full'}  # 走 prepare_adapter 的
print('自定义 Tuner 轨道:', custom_track)
print('prepare_adapter 轨道:', prepare_adapter_track)
print('非 adapter (full):', {'full'})
```

**需要观察的现象**：`lora`、`llamapro`、`vera` 等常见 tuner 出现在 `prepare_adapter 轨道`，而**不**在 `tuners_map`。

**预期结果**：`prepare_adapter 轨道` 含 `lora/longlora/adalora/llamapro/adapter/vera/boft/fourierft/reft/bone`；`tuners_map` 仅 `ia3/lora_llm/dummy`。**待本地验证**：不同版本 `get_supported_tuners` 的硬编码集合可能微调。

#### 4.2.5 小练习与答案

**练习 1**：如果一个用户想新增一种「需要同时保存 adapter 权重和某个外部优化器状态」的微调方式，应该走哪条轨道？为什么？

> **参考答案**：应走 `tuners_map` 轨道——新建 `Tuner` 子类，自定义 `save_pretrained`/`from_pretrained` 处理额外的优化器状态文件，并把它注册进 `tuners_map`。因为 `prepare_adapter` 轨道复用 peft 默认序列化，无法保存自定义文件。

**练习 2**：`LoRALLMTuner` 为什么直接继承 `Tuner` 而不是 `PeftTuner`？

> **参考答案**：因为它需要保存 `vit.safetensors` 这个 peft 默认逻辑之外的文件，且 ZeRO-3 下加载方式也不同，`PeftTuner` 的默认 `save_pretrained`/`from_pretrained` 不适用，所以直接继承 `Tuner` 全部重写。

### 4.3 prepare_model 编排：挂载、冻结与全量分流

#### 4.3.1 概念说明

前两节讲的是「`Tuner` 契约」和「`tuners_map` 注册」，本节讲真正调度它们的指挥者：`TunerMixin.prepare_model`。它位于 `swift/pipelines/train/tuner.py`，是一个 classmethod，被 `SwiftSft`（及 `SwiftRLHF` 等）通过 mixin 继承获得。

它的职责可以概括为一句话：**根据 `args.tuner_type`，把基座模型改造成符合预期的可训练模型**。这里要处理四种情况：

1. **adapter 微调 + 新建**（最常见）：冻结全部 → 按 tuner_type 挂载增量模块。
2. **adapter 微调 + 断点续训/加载已有 adapter**：用 `from_pretrained` 重新挂载。
3. **全量微调（full）**：全部解冻 → 按 ratio/regex 进一步冻结/激活。
4. **特殊优化器（galore）与 ZeRO-3 兼容**的收尾补丁。

#### 4.3.2 核心流程

`TunerMixin.prepare_model` 的决策树（伪代码）：

```
prepare_model(args, model):
    if args.use_liger_kernel 且 transformers 版本不支持内置 liger:
        apply_liger(args.model_type)               # 内核优化，与 tuner 正交

    if args.is_adapter:                            # tuner_type != 'full'
        # 第 1 步：冻结（unsloth 与已注册 tuner 除外）
        if tuner_backend != 'unsloth' and tuner_type not in tuners_map:
            model.requires_grad_(False)            # 全部冻结

        # 第 2 步：分派挂载
        if resume_from_checkpoint or args.adapters:
            tuner = tuners_map.get(tuner_type) or Swift
            model = tuner.from_pretrained(...)     # 续训：读回 adapter
        else:
            if tuner_type in tuners_map:
                model = tuners_map[tuner_type].prepare_model(args, model)
            else:
                model = prepare_adapter(...)       # lora/llamapro/vera... 的大分派

        # fp16 梯度 bug 修复
        把 fp16 可训练参数转 fp32

    elif tuner_type == 'full':                     # 全量微调
        model.requires_grad_(True)
        freeze_parameters(...)                      # 按 ratio/列表/正则冻结
        activate_parameters(...)                    # 激活指定参数

    if args.use_galore:                             # 优化器专属补丁
        ...
    if is_deepspeed_zero3_enabled():
        _patch_modules_to_save_zero3()             # ZeRO-3 兼容补丁
    return model
```

关键判断是 `args.is_adapter`：

[swift/arguments/base_args/base_args.py:L212-L214](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L212-L214) — `is_adapter` 属性：只要 `tuner_type != 'full'` 就认为是 adapter 微调（即使是 `dummy` 这种不挂载任何模块的也算），从而走 adapter 分支而非全量分支。

#### 4.3.3 源码精读

完整编排逻辑：

[swift/pipelines/train/tuner.py:L336-L389](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuner.py#L336-L389) — `TunerMixin.prepare_model`：按 `is_adapter` 在「冻结+挂载 adapter」与「全量解冻+选择性冻结」两条主线间分派，并处理 liger、unsloth、fp16 修复、galore、ZeRO-3 等边界情况。

逐段拆解最核心的 adapter 分支（L346-371）：

1. **冻结**（L347-351）：除 unsloth（它自己管理梯度）和 `tuners_map` 中的 tuner（它们在 `prepare_model` 内部自行冻结/解冻，如 `LoRALLMTuner` 要解冻 ViT）外，先把所有参数 `requires_grad_(False)`。这是 adapter 微调省显存的根基——先全冻，再只放开增量模块。
2. **续训判定**（L352-358）：若 `resume_from_checkpoint` 或 `--adapters` 指定了已有 adapter，则优先用 `from_pretrained` 加载；`tuner` 取 `tuners_map[tuner_type]`，取不到则用 `Swift`（默认 peft 加载）。
3. **新建分派**（L359-365）：这是双轨制的交汇点——`tuner_type in tuners_map` 走自定义 `Tuner.prepare_model`，否则走 `prepare_adapter`（lora/llamapro 等的大函数）。

`prepare_adapter` 是个超长函数（L146-318），按 `tuner_type` 用 if/elif 分发到不同 Config：

[swift/pipelines/train/tuner.py:L146-L318](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuner.py#L146-L318) — `prepare_adapter`：根据 `tuner_type`（lora/adalora/llamapro/adapter/vera/boft/fourierft/reft/bone）构造对应 Config，统一调用 `Swift.prepare_model(model, config)` 完成挂载；其中 LoRA 还按 `tuner_backend`（peft/unsloth/swift-lora）再细分。

以 LoRA 为例（L164-224）：先组装 `lorakwargs`（rank/alpha/target_modules/modules_to_save 等），再按 `use_swift_lora`、`tuner_backend == 'peft'`、`tuner_backend == 'unsloth'` 三种后端分别构造 config 并调用 `Swift.prepare_model` 或 unsloth 的 `get_peft_model`。

`Swift.prepare_model` 是两条轨道的共同出口——它判断 config 类型，swift 原生 config 包装成 `SwiftModel`，peft config 走 `get_peft_model`：

[swift/tuners/base.py:L702-L720](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/base.py#L702-L720) — `Swift.prepare_model`：若 config 是 `SwiftConfig`/dict 则构造 `SwiftModel`（swift 原生 adapter 容器），否则（peft config）调用 `get_peft_model`，统一了两种 adapter 的入口。

**target_modules 的展开**也是 `prepare_model` 体系的重要一环。`--target_modules all-linear` 不是一个真实模块名，需要被展开成模型里所有 Linear 层名：

[swift/pipelines/train/tuner.py:L91-L110](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuner.py#L91-L110) — `get_target_modules`：把 `all-linear`/`all-embedding` 占位符展开为真实模块名；多模态模型走 `get_multimodal_target_regex`（受 freeze_llm/freeze_vit/freeze_aligner 控制），纯文本模型走 `find_all_linears` 扫描实例。

这里复用了 [u3-l2](u3-l2-model-arch-and-keys.md) 的结论：纯文本模型的 `all-linear` 不读 `ModelKeys`，而是用 `find_all_linears` 扫描实例、用 `lm_head` 排除输出头；多模态则由分层字段配合 freeze 开关决定。

**全量微调分支的冻结/激活**用到的工具函数：

[swift/utils/transformers_utils.py:L70-L98](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/transformers_utils.py#L70-L98) — `freeze_parameters` 提供三种冻结方式：按比例（`freeze_parameters_ratio`，从前到后冻结指定比例参数）、按模块名前缀（`freeze_parameters` 列表）、按正则（`freeze_parameters_regex`）。

[swift/utils/transformers_utils.py:L101-L130](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/transformers_utils.py#L101-L130) — `activate_parameters` 与之对偶，用 `trainable_parameters`（前缀匹配）和 `trainable_parameters_regex`（正则匹配）把指定参数重新置为可训练，实现「全量微调中只放开部分层」的需求。

#### 4.3.4 代码实践（本讲主线实践）

**实践目标**：跟踪一次 `swift sft --tuner_type lora` 中 `prepare_model` 的完整调用链，亲眼看到「冻结→挂载→只存 adapter」三件事。

**操作步骤**：

1. **入口定位**。`SwiftSft.run()` 是训练主入口，在其中调用 `self.prepare_model`（`SwiftSft` 通过 mixin 继承自 `TunerMixin`）：

   [swift/pipelines/train/sft.py:L22-L22](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L22-L22) — `SwiftSft(SwiftPipeline, TunerMixin)` 通过多继承获得 `prepare_model` 方法。

   [swift/pipelines/train/sft.py:L170-L171](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L170-L171) — `self.model = self.prepare_model(self.args, self.model, ...)` 是改造模型的那一行，传入 template 与 train_dataset 是因为 LoRA-GA 等特殊 tuner 初始化时需要数据。

2. **运行一次最小 LoRA 训练**（单卡，参考 `examples/train/lora_sft.sh`）：

   ```bash
   CUDA_VISIBLE_DEVICES=0 swift sft \
       --model Qwen/Qwen2.5-7B-Instruct \
       --tuner_type lora \
       --dataset 'AI-ModelScope/alpaca-gpt4-data-en#200' \
       --lora_rank 8 --target_modules all-linear \
       --max_length 1024 --max_steps 5 \
       --output_dir output/lora-demo
   ```

   若显存不足，可换更小模型（如 `Qwen/Qwen3-0.6B`）并降低 `--max_length`。**待本地验证**：具体能否跑通取决于本地 GPU 与网络。

3. **观察训练日志**，重点看两段输出：
   - `lora_config: ...`：由 [swift/pipelines/train/tuner.py:L146-L203](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuner.py#L146-L203) 中的 `logger.info(f'lora_config: {lora_config}')` 打印，确认 config 被正确构造。
   - `model_parameter_info: ...`：打印可训练参数占比。LoRA 下应显示 trainable params 远小于 total params（通常 <1%）。

4. **验证只保存 adapter**。训练结束后查看 `output/lora-demo/checkpoint-*` 目录：

   ```bash
   ls output/lora-demo/checkpoint-*/
   ```

   **预期结果**：目录中应出现 `adapter_config.json` 与 `adapter_model.safetensors`（LoRA 增量权重），而**不**出现完整的 `model.safetensors`（基座权重不重复落盘）。这是因为保存时走的是 [swift/trainers/mixin.py:L371-L373](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/mixin.py#L371-L373) 的默认 peft 分支（lora 不在 `tuners_map`，落到 L374-386 的 `self.model.save_pretrained`，peft 自动只存 adapter）。

5. **用源码验证保存分派逻辑**。打开 [swift/trainers/mixin.py:L322-L373](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/mixin.py#L322-L373)，对照 `_save_model` 的 if/elif 链：
   - L371 `elif self.args.tuner_type in tuners_map`：仅 `ia3`/`lora_llm`/`dummy` 走自定义 `Tuner.save_pretrained`。
   - L374 `else`：其余（含 lora）走 `self.model.save_pretrained`（peft 默认，只存 adapter）。

**需要观察的现象**：训练日志出现 `lora_config` 与可训练参数统计；checkpoint 目录只有 adapter 文件，没有完整模型权重。

**预期结果**：可训练参数占比极小（如 0.x%），checkpoint 含 `adapter_config.json`+`adapter_model.safetensors`，体积远小于完整模型。这一现象直接印证了「prepare_model 挂载了轻量增量、save_pretrained 只存增量」两条结论。

#### 4.3.5 小练习与答案

**练习 1**：在 `TunerMixin.prepare_model` 中，为什么对 `tuners_map` 中的 tuner **不执行** `model.requires_grad_(False)`，而对 `prepare_adapter` 轨道的 tuner 却要先全部冻结？

> **参考答案**：`prepare_adapter` 轨道（lora 等）挂载的增量模块在调用 `Swift.prepare_model` 时会被自动标记为可训练，所以需要先全冻，挂载后只有增量可训练；而 `tuners_map` 中的 tuner（如 `LoRALLMTuner`）需要在 `prepare_model` 内部自行决定哪些部分可训练（它要解冻 ViT/Aligner），若框架先全冻再让它解冻会丢失「按需解冻」的灵活性，因此交给子类全权管理梯度状态。

**练习 2**：`--tuner_type full` 时，`is_adapter` 为何值？模型走哪条分支？如何只训练最后几层？

> **参考答案**：`is_adapter` 为 `False`（`tuner_type == 'full'`）。模型走 L372-378 的全量分支：先 `requires_grad_(True)` 全部解冻，再用 `freeze_parameters` 冻结、`activate_parameters` 激活。要只训练最后几层，可用 `--trainable_parameters_regex` 匹配最后几层的命名（如 `.*layers\.(2[8-9]|3[0-1])\..*`），或用 `--freeze_parameters_ratio` 冻结靠前的大部分参数。

**练习 3**：断点续训时，`prepare_model` 如何避免重复挂载一个新的随机 LoRA？

> **参考答案**：当 `args.resume_from_checkpoint` 非空时，L352-358 的分支优先执行：它不调用 `prepare_model`/`prepare_adapter` 新建 adapter，而是调用 `tuner.from_pretrained(model, resume_from_checkpoint, is_trainable=True)`，把已有 checkpoint 里的 LoRA 权重读回并挂到基座上，从而恢复到中断前的可训练状态。

## 5. 综合实践

设计一个把本讲三个最小模块串起来的小任务：**对比 lora 与 lora_llm 两种 adapter 的挂载与保存差异**。

1. **阅读阶段**：对照 [swift/tuner_plugin/lora_llm.py:L68-L78](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuner_plugin/lora_llm.py#L68-L78)（`LoRALLMTuner.prepare_model`）与 [swift/pipelines/train/tuner.py:L164-L203](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuner.py#L164-L203)（普通 lora），写一段说明：普通 lora 只在 LLM 的 Linear 上挂低秩矩阵、其余全冻；而 lora_llm 在多模态模型上对 LLM 挂 LoRA，**同时**把 ViT/Aligner 解冻做全量训练。

2. **追踪保存差异**：阅读 [swift/trainers/mixin.py:L371-L386](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/mixin.py#L371-L386)，说明：
   - `lora` 不在 `tuners_map`，走 L374 的 `else` → `self.model.save_pretrained`，只产 `adapter_model.safetensors`。
   - `lora_llm` 在 `tuners_map`，走 L371-373 的 `LoRALLMTuner.save_pretrained`，除 adapter 外**额外**产 `vit.safetensors`。

3. **验证（可选，需多模态模型与足够显存）**：若有条件，用 `--tuner_type lora_llm` 跑一个多模态模型（如 Qwen2.5-VL）的微调，检查 checkpoint 目录是否同时包含 `adapter_model.safetensors` 与 `vit.safetensors`。**待本地验证**。

通过这个任务，你能直观看到 `tuners_map` 存在的根本理由：**当一种微调方式的序列化需求超出 peft 默认能力时，就必须自定义 `Tuner` 子类并注册进来**。

## 6. 本讲小结

- `Tuner` 基类用三个静态方法 `prepare_model`/`save_pretrained`/`from_pretrained` 定义了「挂载—保存—加载」的统一契约，让上层 pipeline 与 trainer 用同一套代码处理所有微调方式。
- `PeftTuner` 是基于 peft 的中间层，提供通用的 `save_pretrained`/`from_pretrained` 默认实现，子类（如 `IA3Tuner`）只需覆写 `prepare_model`。
- `tuners_map` 只收录需要自定义序列化逻辑的 3 个 tuner（`ia3`/`lora_llm`/`dummy`）；其余（lora/llamapro/vera 等）走 `prepare_adapter`→`Swift.prepare_model`，复用 peft 默认序列化——这是 ms-swift 的「双轨制」分派。
- `TunerMixin.prepare_model` 是真正的指挥者：按 `is_adapter` 在「先冻结再挂载增量」与「全量解冻再选择性冻结」两条主线间分流，并处理 unsloth、fp16、galore、ZeRO-3 等边界情况。
- adapter 微调的关键是「先 `requires_grad_(False)` 全冻，再只放开增量」，`--target_modules all-linear` 会通过 `find_all_linears`（纯文本）或 `get_multimodal_target_regex`（多模态）展开为真实模块名。
- 保存时 `trainer._save_model` 据 `tuner_type in tuners_map` 分派：在表中走自定义 `Tuner.save_pretrained`，不在表中走 peft 默认（只存 adapter），这解释了 LoRA checkpoint 为何只有 `adapter_model.safetensors`。

## 7. 下一步学习建议

- **下一讲 [u5-l3 LoRA 与轻量微调方法](u5-l3-lora-and-lightweight-tuners.md)**：深入 `prepare_adapter` 调用的各 Config，精读 `swift/tuners/lora.py` 中 LoRA 的算法实现与 `swift_to_peft_format` 转换，理解 `lora_rank`/`lora_alpha`/`target_modules` 的数学含义。
- **继续阅读源码**：`swift/tuners/base.py` 的 `SwiftModel` 类（adapter 容器，多 adapter 共存机制）、`swift/tuners/llamapro.py`（插入新 block 的结构改造，与 LoRA 的旁路并联截然不同）。
- **横向对比**：结合 [u5-l1](u5-l1-trainer-factory-and-trainers.md) 的 trainer 体系与 [u5-l4](u5-l4-sft-main-pipeline.md) 的 SFT 主流程，理解 `prepare_model` 在整个训练链路中的时序位置——它发生在模型加载（u3-l1）之后、数据集编码（u4-l3）与 trainer 构造之前。
