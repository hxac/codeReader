# SmoothQuant 平滑激活

## 1. 本讲目标

本讲讲解 SmoothQuant 算法在 llm-compressor 中的实现。SmoothQuant 是一种**变换型（transform）**压缩算法：它不直接做量化取整，而是在量化之前，把激活里那些「难量化」的离群通道（outlier）等价地迁移到权重里，让后续的 W8A8 Int8 量化更容易、更准。

读完本讲，你应该能够：

1. 说清 SmoothQuant 为什么有效——它用校准阶段统计到的「激活逐通道动态范围」与「权重逐通道最大幅值」计算出一个平滑因子 \(s\)，并把激活除以 \(s\)、把对应权重乘以 \(s\)，使整体输出在数学上不变，却让激活更均匀。
2. 看懂 `SmoothQuantModifier` 的四个生命周期钩子如何串起「挂统计 hook → 校准累积 → 算缩放 → 重排权重 → 卸 hook」整条链路。
3. 理解 `modifiers/smoothquant/` 与 `modifiers/transform/smoothquant/` 两个包的真实分工：前者只是**已废弃的兼容垫片**，真正的代码早已搬迁到后者。
4. 搞懂 mapping（映射）体系：它如何按模型架构自动发现「该平滑谁、该平衡谁」，以及为什么 MoE 模型必须把**全部专家**都纳入平衡。

> 本讲承接 u3-l2（`QuantizationMixin` 把方案挂到模块）。注意：`SmoothQuantModifier` **只继承 `Modifier`，不混入 `QuantizationMixin`**，它本身不量化，因此它必须与一个真正的量化 modifier（如 `GPTQModifier`/`QuantizationModifier`）配合使用。

---

## 2. 前置知识

在进入源码前，先用三段话补齐本讲需要的基础直觉。

### 2.1 激活离群值是 Int8 量化的头号敌人

LLM 的激活张量里，少数通道的幅值会比其他通道大很多（outlier）。Int8 量化要把一个张量的取值范围线性压到 \([-128,127]\)，scale 由**最大幅值**决定。一个超大离群值会把 scale 撑得很大，导致其余「正常」通道被压成几乎相等的几个整数，精度惨跌。权重通常没有这种系统性离群值，所以**让激活更友好、把难度让给权重**是划算的——这就是 SmoothQuant 的核心动机。

### 2.2 一个「等价缩放」的不等式

对一个线性层 \(Y = XW\)（\(X\) 是激活、\(W\) 是权重），如果我们对每个输入通道 \(j\) 引入一个正的缩放因子 \(s_j\)，做如下变换：

\[
\tilde{X} = X \cdot \mathrm{diag}(s)^{-1}, \qquad \tilde{W} = \mathrm{diag}(s) \cdot W
\]

那么：

\[
Y = \tilde{X}\,\tilde{W} = \big(X\,\mathrm{diag}(s)^{-1}\big)\big(\mathrm{diag}(s)\,W\big) = XW
\]

输出**完全不变**，但激活被「压扁」（除以 \(s\)），权重被「拉伸」（乘以 \(s\)）。只要我们让 \(s_j\) 在离群通道上取大值，就能精准地削弱这些离群值。

### 2.3 SmoothQuant 与 AWQ 的家族相似性

本讲是 u4-l2（AWQ）的姐妹篇。两者都是**变换型 modifier**：自己不取整，只改写权重，把真正的量化交给后面的 `QuantizationModifier`/`GPTQModifier`。区别在于「改写的策略」：

- **AWQ**：搜索一个缩放向量，重点保护对激活幅值最敏感的权重通道。
- **SmoothQuant**：直接由激活离群值的大小推导缩放向量，目标是**压平激活**的动态范围。

两者都依赖 `SequentialPipeline`（u3-l5）逐子图校准，并通过同一个生命周期钩子 `on_sequential_epoch_end` 触发权重改写。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下。注意：表中第一组的两个文件已**废弃**，真正要读的是第二组。

| 文件 | 角色 |
| --- | --- |
| `src/llmcompressor/modifiers/smoothquant/base.py` | **已废弃的兼容垫片**，只 `re-export` 真实实现并抛 `DeprecationWarning` |
| `src/llmcompressor/modifiers/smoothquant/utils.py` | 同上，已废弃垫片 |
| `src/llmcompressor/modifiers/transform/smoothquant/base.py` | **真正的核心实现**：`SmoothQuantModifier` 类、缩放计算、权重重排 |
| `src/llmcompressor/modifiers/transform/smoothquant/utils.py` | 架构→mapping 静态注册表 `MAPPINGS_REGISTRY`、`LayerMap`、错误装饰器 |
| `src/llmcompressor/modifiers/transform/smoothquant/dynamic_mappings.py` | 需要「读模型 config」才能构造的动态 mapping（如 Qwen3.5 混合注意力） |
| `examples/quantization_w8a8_int8/README.md` | 官方 W8A8 Int8 示例，展示 SmoothQuant + GPTQ 的标准 recipe |
| `tests/llmcompressor/modifiers/transform/smoothquant/test_base.py` | 单元测试，含「MoE 全专家平滑」「忽略语义」「端到端不变量」断言 |

---

## 4. 核心概念与源码讲解

### 4.1 模块定位与代码迁移：两个 smoothquant 包的分工

#### 4.1.1 概念说明

打开仓库你会在 `modifiers/` 下看到两个长得很像的包：

- `modifiers/smoothquant/`
- `modifiers/transform/smoothquant/`

这是 llm-compressor 一次**代码搬迁**的结果。SmoothQuant 本质上是一种「先变换、后量化」的算法，和 AWQ、SmoothQuant 这类逻辑被统一归入 `modifiers/transform/` 命名空间。为了不让老用户的 `from llmcompressor.modifiers.smoothquant.base import SmoothQuantModifier` 直接报错，旧路径被保留为**只抛弃用警告、再 re-export 新实现**的薄垫片。

> 这个学习目标（理解两者分工）的答案很干脆：**`smoothquant/base.py` 与 `smoothquant/utils.py` 已经没有真实逻辑**，全部真实代码都在 `transform/smoothquant/` 下。读源码、写新代码都应直接指向 `transform/smoothquant`。

#### 4.1.2 核心流程

垫片的工作流程极简：

1. 模块被 import 时，立刻 `warnings.warn(..., DeprecationWarning)`。
2. 用 `from ... import *` 把新实现的名字全部透传过来。
3. 因此 `SmoothQuantModifier` 这个名字在旧路径下仍可解析到同一个类对象。

#### 4.1.3 源码精读

垫片的全部内容就是一段警告加一行 re-export。以 `smoothquant/base.py` 为例：

[smoothquant/base.py:9-19](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/smoothquant/base.py#L9-L19) —— 抛出 `DeprecationWarning` 后，把 `transform.smoothquant.base` 的所有公开符号原样导出。`smoothquant/utils.py` 与包级 `smoothquant/__init__.py` 的写法完全相同（见 [smoothquant/utils.py:9-19](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/smoothquant/utils.py#L9-L19) 与 [smoothquant/__init__.py:11-23](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/smoothquant/__init__.py#L11-L23)）。

而真实实现的包入口则干净直接：[transform/smoothquant/__init__.py:1-4](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/smoothquant/__init__.py#L1-L4) 只有一行 `from .base import *`。

> 重要提醒：还记得 u2-l4 讲过 `ModifierFactory` 靠「类名以 `Modifier` 结尾」自动发现算法类。由于垫片用 `import *` 透传了 `SmoothQuantModifier`，**务必保证 deprecated 包在工厂遍历时被提前排除**，否则旧路径的弃用警告会污染整个发现过程（这正对应 u2-l4 提到的 deprecated 前缀过滤机制）。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证「旧路径只是垫片、新旧路径指向同一个类」。
2. **操作步骤**：在装好 `llmcompressor` 的环境里运行：

   ```python
   # 示例代码
   import warnings
   from llmcompressor.modifiers.smoothquant.base import SmoothQuantModifier as Old
   from llmcompressor.modifiers.transform.smoothquant.base import (
       SmoothQuantModifier as New,
   )
   print(Old is New)  # 预期 True：两个名字是同一个类对象
   ```
3. **需要观察的现象**：第一行 import 会打印一条 `DeprecationWarning`，告诉你改用 `transform.smoothquant`；`Old is New` 输出 `True`。
4. **预期结果**：确认旧路径除警告外无任何额外行为，后续阅读与编写都应直接用 `llmcompressor.modifiers.transform.smoothquant`。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 llm-compressor 要保留旧路径而不是直接删除？
  - **答案**：为了向后兼容。社区里大量脚本和文档仍写着 `from llmcompressor.modifiers.smoothquant import SmoothQuantModifier`，直接删除会让这些脚本在升级后 `ImportError`；保留垫片 + 警告，能平滑地把用户引导到新路径。

---

### 4.2 平滑的数学原理：把激活离群值迁移到权重

#### 4.2.1 概念说明

`SmoothQuantModifier` 是一个**纯变换 modifier**：它的类声明是 `class SmoothQuantModifier(Modifier)`（[transform/smoothquant/base.py:63](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/smoothquant/base.py#L63)），**不混入 `QuantizationMixin`**。这意味着它不会做任何 round-to-nearest，只负责按 §2.2 的等价缩放重排权重。它的全部数学集中在 `_calculate_smoothing_scales` 这一个方法里。

平滑因子 \(s_j\) 由两部分拼成：激活的「难度」（离群程度）和权重的「难度」（幅值）。SmoothQuant 原论文给出的经典公式是：

\[
s_j = \frac{\max(|X_j|)^{\alpha}}{\max(|W_j|)^{1-\alpha}}
\]

其中 \(j\) 是输入通道下标，\(\alpha\)（代码里叫 `smoothing_strength`）是「让难度往激活还是往权重倾斜」的旋钮，默认 0.5。在 llm-compressor 的实现里，激活项用的是**逐通道动态范围**（校准期 max − min）而非严格 abs-max，这是一个值得留意的实现细节。

#### 4.2.2 核心流程

计算一个 mapping 的平滑因子，分四步：

1. **取激活难度**：用校准阶段累积到的逐通道 `max_channel_vals - min_channel_vals` 作为 `activation_scales`。
2. **取权重难度**：遍历所有 balance 层，取每层权重的逐列绝对值最大值，再在所有 balance 层之间取最大，并乘以 `2.0`，得到 `weight_scales`。
3. **按公式相除**：`s = activation_scales^α / weight_scales^(1-α)`。
4. **兜底**：用 `MINIMUM_SMOOTHING_SCALE = 1e-5` 截断下限，防止除零；对权重全零的通道回退为原始 `activation_scales`。

> 此外，`algorithm` 字段还支持一个变体 `log_equalization`（来自另一篇论文），它**完全不看权重**，只用激活项 \(s_j = \max(|X_j|)/\log_2(2+\max(|X_j|))\)。本讲聚焦默认的 `smoothquant` 算法。

#### 4.2.3 源码精读

核心公式在 `_calculate_smoothing_scales` 里，短短十几行就讲完了 SmoothQuant 的灵魂：

[transform/smoothquant/base.py:383-415](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/smoothquant/base.py#L383-L415) —— 关键三句（精简后）：

```python
# 激活难度：取每层 balance 权重的逐列绝对值最大，再跨层取最大并乘 2.0
weight_scales = 2.0 * torch.cat(weight_scales, dim=0).max(dim=0)[0]
# SmoothQuant: s_j = max(|X_j|)^alpha / max(|W_j|)^(1-alpha)
scales = activation_scales.pow(self.smoothing_strength) / \
         weight_scales.pow(1 - self.smoothing_strength)
return torch.where(weight_scales > 0.0, scales, activation_scales)
```

其中 `activation_scales` 是在调用方 `_apply_smoothing` 里现算的（[transform/smoothquant/base.py:345-348](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/smoothquant/base.py#L345-L348)）：

```python
activation_scales = (
    self.scales_[mapping.smooth_name].max_channel_vals
    - self.scales_[mapping.smooth_name].min_channel_vals
)
```

而 `smoothing_strength` 与 `algorithm` 是 modifier 的配置字段（[transform/smoothquant/base.py:107-112](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/smoothquant/base.py#L107-L112)），默认 `smoothing_strength=0.5`、`algorithm="smoothquant"`。

#### 4.2.4 代码实践

1. **实践目标**：脱离大模型，单独观察 `smoothing_strength`（α）如何改变缩放因子在「激活 vs 权重」间的分配。
2. **操作步骤**：

   ```python
   # 示例代码：手工构造激活与权重难度，复现公式
   import torch
   activation = torch.tensor([0.1, 8.0, 0.2, 5.0])   # 第 2、4 通道是离群值
   weight = torch.tensor([[0.3, 0.4, 0.2, 0.5]])     # shape [1, channels]
   weight_scales = 2.0 * weight.abs().max(dim=0)[0]
   for alpha in (0.0, 0.5, 0.8, 1.0):
       s = activation.pow(alpha) / weight_scales.pow(1 - alpha)
       print(f"alpha={alpha}: scales={s.tolist()}")
   ```
3. **需要观察的现象**：`alpha` 越大，离群通道的 \(s_j\) 越大；`alpha=1.0` 时完全只看激活，`alpha=0.0` 时完全只看权重。
4. **预期结果**：直观体会「`smoothing_strength` 越大 → 激活被压得越狠、权重被拉得越多」。W8A8 场景下官方示例用 `0.8`（见 §5），正是因为把更多难度推向了能承受的权重侧。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `weight_scales` 在跨层取最大后还要 `* 2.0`？
  - **答案**：这是一个工程化的安全系数，避免权重侧难度被估得过小、从而让 \(s_j\) 过大、把权重拉伸到反而难量化的程度（属于实现经验项；理解公式主干时把它当作一个常数缩放即可）。
- **练习 2**：若某通道权重恒为零，代码会得到什么 \(s_j\)？
  - **答案**：因为 `weight_scales=0` 会触发 `torch.where` 的回退分支，该通道的 \(s_j\) 直接取 `activation_scales`，即不做任何权衡、把全部难度留在激活侧（也是合理的：零权重列乘任何数都不变）。

---

### 4.3 mapping 体系：架构自动发现与 MoE 全专家平衡

#### 4.3.1 概念说明

SmoothQuant 要改写权重，就得先知道**「平滑谁、平衡谁」**——即一个 LayerNorm 的输出要被压扁，那么吃这个输出的所有 `q_proj/k_proj/v_proj` 都得被同步放大来补偿。这套「平滑层 ↔ 平衡层」的配对就叫一个 **mapping**。

代码用两个 dataclass 承载它（[transform/smoothquant/base.py:32-60](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/smoothquant/base.py#L32-L60)）：

- `SmoothQuantScale`：存每个 smooth 层在校准期看到的逐通道 `min/max`。
- `SmoothQuantMapping`：存一对配对——`smooth_name`/`smooth_layer`（要被压扁的归一化层）与 `balance_layers`（要被放大的后续权重列表）。

mapping 有两种来源：用户手写传入 `mappings=` 参数，或由模型架构自动推断。

#### 4.3.2 核心流程

mapping 解析分三层，优先级从高到低：

1. **用户显式指定**：`on_initialize` 里若 `self.mappings is not None` 则直接用（[transform/smoothquant/base.py:175-183](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/smoothquant/base.py#L175-L183)）。
2. **动态推断**：`get_layer_mappings_from_model` 先查 `SMOOTHQUANT_DYNAMIC_MAPPING_REGISTRY`（需要读模型 config 才能构造的映射，如 Qwen3.5 混合注意力只对 full-attention 层平滑），再查静态注册表，最后回退默认（[dynamic_mappings.py:22-44](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/smoothquant/dynamic_mappings.py#L22-L44)）。
3. **静态注册表**：`MAPPINGS_REGISTRY` 按模型类名（如 `LlamaForCausalLM`、`CohereForCausalLM`、`MixtralForCausalLM`）查预设的 `LayerMap` 列表（[utils.py:114-146](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/smoothquant/utils.py#L114-L146)）。

随后 `_resolve_mappings` 把这些「正则字符串」解析成真正的模块对象。它有一个对 MoE 至关重要的细节：**在父作用域内匹配全部 balance 层**，因此一个 `re:.*experts.*w1` 会把所有专家的 `w1` 全部纳入 `balance_layers`（[transform/smoothquant/base.py:185-249](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/smoothquant/base.py#L185-L249)）。

#### 4.3.3 源码精读

默认 mapping 反映了 SmoothQuant 论文的建议——平滑 self-attention 与 MLP 的输入：

[utils.py:16-25](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/smoothquant/utils.py#L16-L25) —— `DEFAULT_SMOOTHQUANT_MAPPINGS` 把 `input_layernorm`↔`q/k/v_proj`、`post_attention_layernorm`↔`gate/up_proj` 配成两对。每个 `LayerMap` 的格式是 `(balance_layers 列表, smooth_layers)`。

mapping 格式语义在官方 README 里有图示化讲解（[transform/smoothquant/README.md:34-44](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/smoothquant/README.md#L34-L44)）：`[[吃平滑激活的层], 要平滑其输出的层]`，且必须匹配到**叶子模块**（如 `gate_proj` 而非整个 `mlp`）。

MoE 全专家平衡由 `_resolve_mappings` 保证——它用 `match_modules_set` 在父作用域展开所有匹配模块，并强制「每个 mapping 只能匹配一个 smooth 层」（[transform/smoothquant/base.py:205-224](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/smoothquant/base.py#L205-L224)）。对应的测试 `test_moe_all_experts_smoothed` 明确断言：8 个专家必须**全部**出现在 `balance_layers` 里（[test_base.py:52-117](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/modifiers/transform/smoothquant/test_base.py#L52-L117)）。

`ignore` 的语义也容易踩坑：**只有当一个 mapping 的所有层都被忽略时才跳过它**（[transform/smoothquant/base.py:226-243](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/smoothquant/base.py#L226-L243)），由 `test_ignore_behavior` 验证（[test_base.py:180-231](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/modifiers/transform/smoothquant/test_base.py#L180-L231)）。

> 顺带一提：MoE 模型通常**不平滑 MLP 的 post_attention_layernorm**，因为「一个缩放向量罩住所有专家」会造成专家间失衡、反而掉精度。`QWEN_MOE_SMOOTHQUANT_MAPPINGS` 与 `MIXTRAL_SMOOTHQUANT_MAPPINGS` 都只配了 attention 那一对（[utils.py:26-31](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/smoothquant/utils.py#L26-L31) 与 [utils.py:74-79](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/smoothquant/utils.py#L74-L79)）。

#### 4.3.4 代码实践

1. **实践目标**：用你熟悉的模型架构，查清 SmoothQuant 会自动选哪套 mapping。
2. **操作步骤**：

   ```python
   # 示例代码：查看某架构的预设 mapping
   from llmcompressor.modifiers.transform.smoothquant.utils import (
       get_layer_mappings_from_architecture,
   )
   for arch in ["LlamaForCausalLM", "MixtralForCausalLM", "CohereForCausalLM"]:
       maps = get_layer_mappings_from_architecture(arch)
       print(arch)
       for m in maps:
           print("  balance:", m.balance_layers, " smooth:", m.smooth_layers)
   ```
3. **需要观察的现象**：Llama 用默认两对；Mixtral/Qwen-MoE 只有 attention 一对；Cohere 把 attn 与 mlp 的 balance 合并到了同一个 `input_layernorm`。
4. **预期结果**：理解不同架构的 mapping 差异，并能解释「为什么 MoE 少了 MLP 那一对」。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 mapping 必须匹配叶子模块（`q_proj`），而不是上一层（`self_attn`）？
  - **答案**：平滑要在计算图的叶子节点施加，才能精确地「除掉激活、乘进权重」并保持等价；匹配到 `self_attn` 这种复合模块无法定位到具体的 `weight` 张量去做补偿。
- **练习 2**：`ignore=["lm_head"]` 会跳过哪些 mapping？
  - **答案**：只跳过 smooth/balance 层**全部**落在 ignore 命中的那个 mapping；只要 mapping 里还有任意一层没被忽略，它仍会被平滑（参见 `any_not_ignored` 判断）。

---

### 4.4 生命周期编排：统计收集、缩放计算与权重重排

#### 4.4.1 概念说明

`SmoothQuantModifier` 是 `Modifier` 基类（u2-l3）双生命周期里**校准链**的典型住户。它实现了四个钩子，把整条 SmoothQuant 流程串起来：

| 钩子 | 时机（由 SequentialPipeline 触发） | 做什么 |
| --- | --- | --- |
| `on_initialize` | 会话初始化 | 校验「只能 one-shot」、推断并解析 mapping |
| `on_calibration_start` | 校准开始 | 给每个 smooth 层挂 forward hook，准备收集统计 |
| `on_sequential_epoch_end` | 每个子图校准完 | 用累积的统计算缩放、改写权重 |
| `on_calibration_end` | 校准结束 | 卸下 hook |

由于 `requires_calibration_data = True`（[transform/smoothquant/base.py:105](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/smoothquant/base.py#L105)），oneshot 会自动为它选 `SequentialPipeline`（回顾 u3-l4 的 `_infer_pipeline`）。

#### 4.4.2 核心流程

校准期间，每个 smooth 层的输出会流过 forward hook，hook 维护该层**逐通道的全局 min/max**（跨 batch 取极值）。子图校准结束后：

1. `activation_scales = max - min`（逐通道动态范围）。
2. `_calculate_smoothing_scales` 算出 \(s\)（§4.2）。
3. 对每个 balance 层：`weight ← weight * s`（放大输入通道列）。
4. 对 smooth 层：`weight ← weight / s`（压扁），若有 `bias` 也 `bias ← bias / s`。
5. 删掉该层统计，进入下一个 mapping。

分布式（DDP）下，因为各 rank 看到的是校准数据的不同切片，统计前要先 `all_reduce(MIN/MAX)` 汇总成全局值，保证每张卡算出**相同**的 \(s\)。

#### 4.4.3 源码精读

四个钩子的骨架极薄，只是把工作分派给内部方法（[transform/smoothquant/base.py:119-156](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/smoothquant/base.py#L119-L156)）：

```python
def on_initialize(self, state, **kwargs) -> bool:
    ...
    self.mappings = self._infer_mappings_from_model(state.model)
    self.resolved_mappings_ = self._resolve_mappings(state.model)
    self.scales_ = {}
    return True

def on_calibration_start(self, state, event, **kwargs):
    self._setup_scale_hooks()

def on_sequential_epoch_end(self, state, event, **kwargs):
    self._apply_smoothing(state.model)

def on_calibration_end(self, state, event, **kwargs):
    self.remove_hooks()
```

统计收集在 `_setup_scale_hooks` 里完成，hook 把每次前向的输出按通道取 min/max 并与历史做极值合并（[transform/smoothquant/base.py:251-285](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/smoothquant/base.py#L251-L285)）：

```python
latest_mins = torch.min(out, dim=0)[0]
latest_maxes = torch.max(out, dim=0)[0]
# 与历史 min/max 取极值合并
```

> 注意这里用的是 `HooksMixin` 提供的 `register_hook`（u2-l3），与 QuantizationMixin 的校准 hook 是同一套基础设施，所以 `remove_hooks` 能在 `on_calibration_end` 干净卸载。

权重改写在 `_apply_smoothing` 里，正是 §2.2 那个不变量的落地（[transform/smoothquant/base.py:321-381](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/smoothquant/base.py#L321-L381)）：

```python
def smooth(module):
    if module in balance_layers:                 # W <- W * s
        update_offload_parameter(module, "weight", module.weight * scales.view(1, -1))
    elif module == smooth_layer:                 # gamma <- gamma / s
        update_offload_parameter(module, "weight", module.weight / scales)
```

其中 `update_offload_parameter` 来自 `compressed_tensors.offload`，保证即便权重被 offload 到 CPU 也能正确写回——这是 llm-compressor 支持超大模型（u6-l2）的基础设施之一。

分布式汇总在 `_reduce_activation_scales`（[transform/smoothquant/base.py:287-319](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/smoothquant/base.py#L287-L319)），非分布式时直接 `return`，是 no-op。

最后 `on_finalize` 做收尾校验：若 `scales_` 还有残留，说明有模块没被压缩，直接抛错（[transform/smoothquant/base.py:158-173](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/smoothquant/base.py#L158-L173)）。

#### 4.4.4 代码实践

1. **实践目标**：验证 SmoothQuant 的「不变量」——平滑后模型输出应与平滑前**近似相等**（这正是 e2e 测试的核心断言）。
2. **操作步骤**：阅读并复现 `test_smoothquant_e2e` 的思路（[test_base.py:234-282](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/modifiers/transform/smoothquant/test_base.py#L234-L282)）。它对一个微型模型 `nm-testing/tinysmokellama-3.2`：

   ```python
   # 示例代码（摘自测试思路）
   before_smooth = model(**sample_input).logits          # 平滑前 logits
   oneshot(model=model, dataset="open_platypus",
           recipe=SmoothQuantModifier(smoothing_strength=0.5),
           num_calibration_samples=4, max_seq_length=128)
   after_smooth = model(**sample_input).logits           # 平滑后 logits
   torch.testing.assert_close(before_smooth, after_smooth, atol=0.1, rtol=0.1)
   ```

3. **需要观察的现象**：`input_layernorm`、`q_proj.weight` 等被命中的权重**确实发生了变化**；但模型 logits 几乎不变（`atol=0.1, rtol=0.1`）。
4. **预期结果**：亲眼确认「激活被压扁、权重被拉伸，输出却不变」的等价缩放成立。若本地无 GPU，可在 CPU 上跑该微型模型；完整大模型量化则待本地验证。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 hook 收集的是**逐通道 min/max**而不是均值或方差？
  - **答案**：SmoothQuant 关心的是激活的「极端值」（离群程度），min/max 的差（动态范围）正好刻画了量化 scale 会被撑大的程度；均值/方差无法捕捉离群值。
- **练习 2**：`on_sequential_epoch_end` 收到的 `modules` 参数（u2-l3 透传）对 SmoothQuant 有什么用？
  - **答案**：SmoothQuant 当前是按 `resolved_mappings_` 全量改写，并不显式依赖该参数；但 SequentialPipeline 的逐子图两遍前向机制保证了「统计在本子图真实激活上收集、改写后误差能传到下一层」，这是它依赖 sequential 管线的根本原因。

---

## 5. 综合实践

**任务**：用 SmoothQuant + W8A8 Int8 量化，对比「直接 W8A8 Int8 量化」的**激活量化误差**，体会平滑带来的收益。

官方 W8A8 Int8 示例（[examples/quantization_w8a8_int8/README.md:79-110](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/examples/quantization_w8a8_int8/README.md#L79-L110)）给出的标准 recipe 是：

```python
from llmcompressor import oneshot
from llmcompressor.modifiers.gptq import GPTQModifier
from llmcompressor.modifiers.transform.smoothquant import SmoothQuantModifier

# 配方 B：先平滑，再 W8A8 量化
recipe_with_smooth = [
    SmoothQuantModifier(smoothing_strength=0.8),
    GPTQModifier(targets="Linear", scheme="W8A8", ignore=["lm_head"]),
]
```

请按以下步骤完成对比：

1. **准备两组 recipe**：
   - 配方 A（直接量化）：`[GPTQModifier(targets="Linear", scheme="W8A8", ignore=["lm_head"])]`
   - 配方 B（先平滑）：上面那段，多一个 `SmoothQuantModifier(smoothing_strength=0.8)`。
2. **跑两次 oneshot**：对同一个模型（小模型可用 `nm-testing/tinysmokellama-3.2`，正式实验用 Llama-3-8B 类模型），分别用 A、B 量化并保存到不同目录。建议复用示例的数据准备（`ultrachat`、512 样本、seq=2048）。
3. **度量激活量化误差**：对同一批校准样本，分别记录量化前后某一层（如第一个 decoder 层的 `q_proj` 输入激活）的余弦相似度或相对 MSE。预期配方 B 的激活误差显著更小。
4. **（可选）度量端到端精度**：用 `lm_eval` 在 GSM-8K 上评估两个 checkpoint，预期配方 B 的 `exact_match` 更高（官方示例约 `0.75`）。
5. **观察要点**：
   - 日志里应能看到 `Smoothing with ...input_layernorm` 等行（来自 `_apply_smoothing` 的 `logger.info`）。
   - `config.json` 的 `quantization_config` 两份都应是 W8A8，但配方 B 的模型权重在量化前已被平滑改写过。

> 说明：完整大模型量化与 `lm_eval` 评估需要 GPU 与较长时间，相关数值**待本地验证**；小模型可在 CPU 上跑通流程并观察激活误差趋势。若想先不动模型，可只跑 §4.4.4 的「不变量」实验作为最小验证。

---

## 6. 本讲小结

- SmoothQuant 是**变换型 modifier**（只继承 `Modifier`，不混入 `QuantizationMixin`），自己不取整，只把激活离群值等价迁移到权重，必须后接量化 modifier。
- 核心数学：\(s_j = \mathrm{activation}^{\alpha} / \mathrm{weight}^{1-\alpha}\)，其中激活项用逐通道动态范围（max−min）、权重项跨所有 balance 层取最大并乘 2.0；`smoothing_strength`（α）决定难度在激活与权重间的分配。
- 权重重排严格满足不变量 \(Y = (X\,\mathrm{diag}(s)^{-1})(\mathrm{diag}(s)\,W)\)：smooth 层除以 \(s\)、balance 层乘以 \(s\)，输出不变。
- mapping 体系按「用户指定 → 动态推断 → 静态注册表 → 默认」四级解析；MoE 模型会把**全部专家**纳入 balance，且通常不平滑 MLP 的归一化层以避免专家失衡。
- 生命周期由四个校准链钩子串起：`on_initialize` 解析 mapping、`on_calibration_start` 挂统计 hook、`on_sequential_epoch_end` 算缩放并改写权重、`on_calibration_end` 卸 hook；DDP 下先 all-reduce 统计。
- `modifiers/smoothquant/` 是**已废弃的兼容垫片**，真实实现全部在 `modifiers/transform/smoothquant/`；读源码与写新代码都应直接用后者。

---

## 7. 下一步学习建议

- **算法对比**：回到 u4-l2（AWQ）对照阅读，体会两种变换型 modifier「搜索式（AWQ 网格搜索）」与「解析式（SmoothQuant 由统计直接算）」的差异。
- **管线依赖**：结合 u3-l5（SequentialPipeline）理解为什么 `requires_calibration_data=True` 的 SmoothQuant 会被分配到逐子图两遍前向的 sequential 管线。
- **分布式与超大模型**：SmoothQuant 的 `_reduce_activation_scales` 是 DDP 通信的典型样例，可顺延到 u6-l1（DDP 分布式量化）与 u6-l2（大模型内存策略）继续读。
- **扩展实践**：若要自定义一种新的「变换」算法，可参考本讲的四钩子骨架，并到 u6-l4（自定义 Modifier）学习如何用 `ModifierFactory.register` 注册到 recipe。
