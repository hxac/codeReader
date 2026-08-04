# AWQ 缩放变换

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 **AWQ（Activation-aware Weight Quantization）** 与 GPTQ、RTN 这类「直接改权重」算法的根本区别：AWQ 自己**不做量化**，它只对权重做一次「缩放重排（smoothing）」，把量化这件事留给后面的 `QuantizationModifier`。
- 理解 **transform modifier（变换类 modifier）** 这个角色定位，以及它和 quantization modifier 在 recipe 里的**顺序依赖**与 Recipe 校验。
- 用数学语言解释「平滑缩放」为何能在**不改变模型输出**的前提下，让后续权重量化误差更小。
- 读懂 `AWQModifier` 的四个生命周期钩子（`on_initialize` / `on_calibration_start` / `on_sequential_epoch_end` / `on_calibration_end`）如何串成一条「挂 hook 收激活 → 网格搜索最优缩放 → 重排权重」的流水线。
- 掌握 `duo_scaling`、`n_grid`、`mappings` 等 AWQ 特有参数，以及架构映射表 `AWQ_MAPPING_REGISTRY` 如何把 smooth layer 和 balance layer 配对。

---

## 2. 前置知识

### 2.1 你需要先具备的认知

本讲承接 **u3-l2（QuantizationMixin 把方案挂到模块）**，默认你已经理解：

- **modifier 与生命周期钩子**：每个压缩动作是一个 `Modifier` 子类，由 `CompressionLifecycle` 在固定时机调用它的 `on_initialize` / `on_calibration_start` / `on_sequential_epoch_end` / `on_calibration_end` 等钩子（详见 u2-l3）。
- **QuantizationMixin 的角色**：它给模块挂上 `quantization_scheme`、挂上校准 hook，但**取整动作**（`observe` + `update_qparams`）由子类在 `on_sequential_epoch_end` 显式完成（详见 u3-l2、u3-l3）。
- **SequentialPipeline**：需要校准数据的算法默认走 sequential 管线，它把模型切成子图、逐子图两遍前向（详见 u3-l5）。

### 2.2 一个直觉：为什么要「先缩放，再量化」

权重量化（尤其是 W4A16 这种 4bit 权重量化）的误差并不是均匀分布在所有通道上的。研究发现：**只有大约 1% 的权重通道是「关键通道」**——它们的激活幅值特别大，一旦这些通道的权重量化误差偏大，整个模型的输出就会被严重拉偏。

AWQ 的核心思想不是「想办法精确量化所有通道」，而是**「先动一点手脚，让那 1% 的关键通道在量化时更受保护，代价由其余 99% 不重要的通道来承担」**。这个「动手脚」就是**缩放变换（smoothing）**：

- 把关键通道对应的**激活缩小**（除以一个大于 1 的因子 `s`）；
- 为了保持模型输出不变，同时把对应**权重列放大**同样的倍数 `s`。

这样一缩一放，模型的浮点输出**数学上完全不变**；但当后续对「放大后的权重」做 4bit 量化时，关键通道因为幅值变大，相对量化误差变小，于是整体量化精度提升。**AWQ 只负责这一步缩放重排，真正的取整量化交给紧跟其后的 `QuantizationModifier`。**

> 关键术语：
> - **smooth layer（平滑层）**：产生激活的层，通常是 `LayerNorm` / `RMSNorm`，权重是 1D 向量。
> - **balance layer（平衡层）**：紧接在 smooth layer 后、需要「反向缩放」以抵消平滑影响的层，通常是 `Linear`（如 `q_proj`、`gate_proj`），权重是 2D 矩阵。
> - **transform modifier（变换类 modifier）**：只对权重做变换、不自己量化的 modifier，AWQ、SmoothQuant、SpinQuant 都属此类。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/llmcompressor/modifiers/transform/awq/base.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/base.py#L1-L1045) | `AWQModifier` 主类：生命周期钩子、激活缓存 hook、网格搜索 `_compute_best_scale`、权重重排 `_apply_smoothing`。本讲核心。 |
| [src/llmcompressor/modifiers/transform/awq/mappings.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/mappings.py#L1-L333) | 架构映射表：`AWQMapping` 数据类、各模型族（Llama/Qwen/Phi/Gemma/Cohere…）的 smooth↔balance 配对规则、`AWQ_MAPPING_REGISTRY`。 |
| [src/llmcompressor/modifiers/transform/awq/dynamic_mappings.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/dynamic_mappings.py#L1-L281) | `get_layer_mappings_from_model`：根据模型类名查表/动态生成 mappings，`on_initialize` 在用户没给 mappings 时调用它。 |
| [src/llmcompressor/modifiers/awq/\_\_init\_\_.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/awq/__init__.py#L1-L84) | 旧路径的**兼容垫片**：把旧的「单 modifier」API 自动拆成 `[AWQModifier, QuantizationModifier]`，直观印证了「AWQ 必须后接量化」这一约定。 |
| [src/llmcompressor/recipe/recipe.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/recipe.py#L206-L230) | `validate_model_after`：Recipe 的 pydantic 校验，强制要求 AWQModifier 之后必须存在 `QuantizationMixin` 子类。 |
| [examples/awq/llama_example.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/examples/awq/llama_example.py#L1-L86) | 官方 AWQ 示例：`[AWQModifier(duo_scaling="both"), QuantizationModifier(scheme="W4A16_ASYM")]` 的标准用法。 |

---

## 4. 核心概念与源码讲解

### 4.1 AWQ 是「变换型」modifier，自己不量化

#### 4.1.1 概念说明

回到本系列反复出现的「modifier 分类」视角：

- **QuantizationModifier / GPTQModifier**：自己**改权重数值**（取整到低位整数）。
- **AWQModifier**：只对权重做**等价缩放重排**，**不产生任何量化参数**，量化前后的权重都还是浮点。

`AWQModifier` 的类声明只继承 `Modifier`，**不继承 `QuantizationMixin`**：

```python
class AWQModifier(Modifier):
```

[src/llmcompressor/modifiers/transform/awq/base.py:55-55](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/base.py#L55-L55) —— 这里 `AWQModifier(Modifier)` 说明它只具备生命周期骨架，没有量化字段与量化能力。

它的文档字符串把这一点讲得很直白：

> AWQModifier is a transform-based modifier, in that **it does not perform quantization or compression on its own**. It just scales activation channels according to a quantization scheme. It must be applied in conjunction with a modifier that inherits from `QuantizationMixin` in order to create a compressed checkpoint.

[src/llmcompressor/modifiers/transform/awq/base.py:72-75](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/base.py#L72-L75)

这意味着 AWQ 单独跑完，模型权重还是 FP16/BF16，磁盘上的 `config.json` 里也**不会出现 `quantization_config`**——它只是把权重「挪了挪位置」。要得到真正可部署的 4bit checkpoint，recipe 里必须再跟一个 `QuantizationModifier`，由后者用 RTN 把（已平滑的）权重取整到 int4。

#### 4.1.2 核心流程：AWQ 与 QuantizationModifier 的协作时序

在 `oneshot` 里，recipe 的执行顺序就是 modifier 的书写顺序。AWQ 的标准 recipe 永远长这样（先 AWQ，后量化）：

```python
recipe = [
    AWQModifier(duo_scaling="both"),
    QuantizationModifier(ignore=["lm_head"], scheme="W4A16_ASYM", targets=["Linear"]),
]
```

[examples/awq/llama_example.py:52-59](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/examples/awq/llama_example.py#L52-L59)

两个 modifier 通过**生命周期阶段**交错协作，关键在于「initialize 阶段全部跑完，才开始校准」：

```text
initialize 阶段（按 recipe 顺序逐个调用 on_initialize）：
  1. AWQModifier.on_initialize        → 推断 mappings、设默认 offload_device
  2. QuantizationModifier.on_initialize → （经 QuantizationMixin）给 Linear 挂 quantization_scheme
                                          ★ 此时模块上才有 quantization_scheme 可读

calibration 阶段（SequentialPipeline 逐子图两遍前向）：
  3. AWQModifier.on_calibration_start → 解析 mappings、校验 duo_scaling、挂激活缓存 hook
  4. 每个子图末尾：
     AWQModifier.on_sequential_epoch_end → _apply_smoothing：网格搜索最优 s，把权重重排（W←W*s, γ←γ/s）
     QuantizationModifier.on_sequential_epoch_end → observe(weight)+update_qparams：对重排后的权重做 RTN 取整
  5. AWQModifier.on_calibration_end    → 卸 hook
```

注意第 3 步：AWQ 的 `on_calibration_start` 会去读模块上的 `quantization_scheme`（用来校验 `duo_scaling` 是否与量化策略兼容）。这能成立，正是因为第 2 步的 `QuantizationModifier.on_initialize` **已经在 initialize 阶段把 scheme 挂上去了**。所以「AWQ 必须排在 QuantizationModifier 前面」不仅是为了「先平滑再取整」的语义，还为了**初始化顺序**——但 AWQ 读 scheme 发生在校准期，那时所有 modifier 都已初始化完毕，顺序上没有冲突。

#### 4.1.3 源码精读：Recipe 强制「AWQ 必须后接量化」

「AWQ 之后必须存在量化 modifier」这条约束不是口头约定，而是 Recipe 在 pydantic 校验阶段**强制执行**的：

```python
@model_validator(mode="after")
def validate_model_after(model: "Recipe") -> "Recipe":
    ...
    for modifier_idx, modifier in enumerate(model.modifiers):
        if isinstance(modifier, (AWQModifier)) and not any(
            isinstance(mod, QuantizationMixin)
            for mod in model.modifiers[(modifier_idx + 1) :]
        ):
            raise ValueError(
                f"Recipe includes AWQModifier with no subsequent quantization "
                f"modifer: {model.modifiers}. AWQ must be run with "
            )
    return model
```

[src/llmcompressor/recipe/recipe.py:206-230](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/recipe.py#L206-L230)

逻辑是：遍历 recipe，遇到 `AWQModifier`，就检查它**之后**（`model.modifiers[(modifier_idx + 1):]`）是否存在至少一个 `QuantizationMixin` 子类；没有就抛 `ValueError`。这就是为什么「把 QuantizationModifier 删掉」会直接报错（见 4.1.4）。

> 一个来自源码的细节：上面那段错误信息字符串以 `"AWQ must be run with "` 结尾、显得没写完——这是仓库当前 HEAD 的真实状态（非笔误），你按报错信息排查时留意即可。

旧路径的兼容垫片 `modifiers/awq/__init__.py` 也从反面印证了这条约定：它把老的「单 modifier」用法自动拆成两个，确保拆分后**一定带一个 QuantizationModifier**：

```python
def AWQModifier(**kwargs):
    ...
    return [AWQTransformModifier(**awq_kwargs), QuantizationModifier(**quant_kwargs)]
```

[src/llmcompressor/modifiers/awq/\_\_init\_\_.py:58-83](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/awq/__init__.py#L58-L83) —— 把 `scheme/targets/ignore` 等「量化参数」剥离给 `QuantizationModifier`，把 `duo_scaling` 等「AWQ 参数」留给 `AWQTransformModifier`，正好对应新 API 里两个 modifier 的分工。

#### 4.1.4 代码实践：观察 Recipe 校验报错

1. **实践目标**：亲手触发 Recipe 校验，确认「AWQ 缺少量化后继」会被拦截。
2. **操作步骤**：在一个能 import `llmcompressor` 的 Python 环境里运行下面这段「示例代码」（不需要 GPU、不需要模型）：

   ```python
   # 示例代码：只用于触发 Recipe 校验，不加载任何模型
   from llmcompressor.recipe.recipe import Recipe
   from llmcompressor.modifiers.transform.awq import AWQModifier

   # 错误用法：只有 AWQ，后面没有量化 modifier
   Recipe.create_instance(recipe=[AWQModifier(duo_scaling="both")])
   ```
3. **需要观察的现象**：调用立即抛出 `ValueError`，信息包含 `"AWQModifier with no subsequent quantization modifer"`。
4. **预期结果**：把 `QuantizationModifier(scheme="W4A16_ASYM", targets=["Linear"])` 加到列表末尾后，`create_instance` 正常返回一个 `Recipe` 对象，不再报错。
5. 若你的环境里该调用行为与本说明不一致，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 AWQModifier 不直接继承 `QuantizationMixin`，而是单独做变换？
**参考答案**：因为 AWQ 的产物仍是浮点权重（只是被等价缩放过），它不产生 scale/zero-point、不写 `quantization_config`。量化是另一个独立动作（RTN），由 `QuantizationModifier` 完成。把它们拆成两个 modifier，可以让 AWQ 的缩放与「用哪种方案量化（W4A16 / FP8…）」解耦——同一套平滑可以服务不同的量化方案。

**练习 2**：如果把 recipe 写成 `[QuantizationModifier(...), AWQModifier(...)]`（顺序反过来），会发生什么？
**参考答案**：Recipe 的 `validate_model_after` 只检查「AWQ **之后**有没有 QuantizationMixin」，反过来写能通过校验；但语义上是错的——先取整再平滑，平滑作用于已量化的权重，失去了「先平滑降低量化误差」的意义，且 AWQ 的 `on_calibration_start` 读到的 `quantization_scheme` 来自已被自己改过的状态，结果不可预期。正确顺序永远是 AWQ 在前。

---

### 4.2 平滑缩放的数学与权重重排

#### 4.2.1 概念说明

本模块回答：「除以 s、乘以 s」到底改了哪些张量，为什么模型输出不变？我们以最常见的「RMSNorm（smooth layer）→ Linear（balance layer）」结构为例。

设 smooth layer（RMSNorm，权重 `γ` 为长度 `d` 的向量）输出激活 `h`（形状 `[…, d]`），balance layer（Linear，权重 `W` 形状 `[out, d]`）输出 `y = h @ Wᵀ`。AWQ 选一个长度为 `d` 的逐通道缩放向量 `s`，做两件事：

1. smooth layer 权重 `γ ← γ / s`（逐通道除），从而激活 `h ← h / s`；
2. balance layer 权重 `W ← W · s`（沿输入维 `d` 逐列乘，即 `W[i,j] ← W[i,j] · s[j]`）。

#### 4.2.2 核心流程：等价变换的数学证明

用分量形式验证输出不变。记缩放后量为带撇变量：

\[
y'[i] = \sum_{j} h'[...,j]\cdot (W')^{T}[j,i] = \sum_{j} \frac{h[...,j]}{s_j}\cdot \bigl(W[i,j]\cdot s_j\bigr) = \sum_{j} h[...,j]\cdot W[i,j] = y[i]
\]

即 \(\forall i:\ y'[i]=y[i]\)。所以**缩放前后模型浮点输出逐元素相等**（在数值精度范围内）。

那为什么这对量化有利？因为真正被量化的是**放大后的权重** \(\tilde{W}=W\cdot s\)。把 \(s_j\) 选得「在激活 \(h_j\) 大的通道上更大」，关键通道的权重幅值被抬高，在 4bit 量化里获得更多有效分辨率；而 \(h_j\) 小的通道权重被压低，那里的相对量化误差虽大，却因激活小而对输出影响微弱。AWQ 用一个**网格搜索**直接最小化「量化后输出与原始输出的 MSE」来挑 `s`，不必人工猜（详见 4.4）。

#### 4.2.3 源码精读：`_smooth` 内联函数的权重改写

权重重排发生 `_apply_smoothing` 内部的 `_smooth` 闭包里。它对 balance layer 与 smooth layer 分别处理：

```python
@torch.no_grad()
def _smooth(module, orig_layer_weights):
    scales = best_scales.to(module.weight.device)
    if module in balance_layers:
        # balance layer：W ← W · s（沿输入维逐列乘）
        update_offload_parameter(
            module, "weight",
            orig_layer_weights[module].to(module.weight.device) * scales.view(1, -1),
        )
    elif module == smooth_layer:
        if module.weight.ndim == 1:
            # smooth layer（1D，如 RMSNorm 的 γ）：γ ← γ / s
            update_offload_parameter(module, "weight", module.weight.div_(scales))
        ...
        if hasattr(module, "bias") and module.bias is not None:
            update_offload_parameter(module, "bias", module.bias.div_(scales))
```

[src/llmcompressor/modifiers/transform/awq/base.py:544-578](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/base.py#L544-L578)

要点：

- `scales.view(1, -1)` 把长度 `d` 的 `s` 摊成 `[1, d]`，与权重 `[out, d]` 广播——正是「沿输入维逐列乘」\(W\cdot s\)。
- smooth layer 的 1D 权重做 `div_(scales)`，对应 \(\gamma/s\)；bias 也同步除以 `s`（保持仿射等价）。
- `update_offload_parameter` 而非直接 `module.weight = ...`，是为了兼容大模型的磁盘 offload（权重可能不在 GPU 上，详见 u6-l2）。

`_apply_smoothing` 在 `_smooth` 之前，先用 `best_scales = self._compute_best_scale(...)` 算出最优缩放，然后用循环把每个 balance layer 和 smooth layer 都喂给 `_smooth`：

[src/llmcompressor/modifiers/transform/awq/base.py:580-582](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/base.py#L580-L582)

> 边界情况：当 smooth layer 是 2D（如把 q/k/v 融合成 `qkv_proj`，再去平滑 `o_proj`），`out_features` 与 `in_features` 维度对不上，代码退化为只缩放最后一组输出特征（对应 `v_proj` 那一段），见 [base.py:564-572](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/base.py#L564-L572)，并附了 AutoAWQ 的参考链接。

#### 4.2.4 代码实践：用最小张量验证「等价变换」

1. **实践目标**：用随机张量手算一遍 \(W\cdot s\) 与 \(\gamma/s\)，确认 `y` 不变。
2. **操作步骤**（示例代码，纯 torch，无需项目依赖）：

   ```python
   # 示例代码
   import torch
   d, out = 8, 4
   x = torch.randn(1, d)                 # 假装是 RMSNorm 之前的输入（已归一化）
   gamma = torch.ones(d)                 # smooth layer 权重 γ
   W = torch.randn(out, d)               # balance layer 权重 W

   h = x * gamma                         # smooth layer 输出（简化：跳过归一化）
   y = h @ W.T                           # 原始输出

   s = torch.tensor([0.5, 2.0, 1.0, 3.0, 0.25, 1.5, 1.0, 2.5])
   gamma2 = gamma / s                    # γ ← γ / s
   W2 = W * s.view(1, -1)                # W ← W · s
   h2 = x * gamma2
   y2 = h2 @ W2.T

   print(torch.allclose(y, y2, atol=1e-6))   # 期望 True
   ```
3. **需要观察的现象**：`y` 与 `y2` 几乎完全相等（浮点误差内）。
4. **预期结果**：打印 `True`，验证「缩放前后输出不变」。
5. 本实践在任意带 torch 的环境都能复现；若不一致请「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 smooth layer 的 bias 也要除以 `s`，而不是乘？
**参考答案**：smooth layer 的输出是 `h = γ ⊙ x̂ + bias`（仿射）。为了让 `h ← h/s` 成立，必须同时把 `γ` 和 `bias` 都除以 `s`：`(γ/s)⊙x̂ + bias/s = (γ⊙x̂ + bias)/s = h/s`。只除 `γ` 不除 bias 会破坏等价性。

**练习 2**：缩放向量 `s` 是「逐通道（per-channel）」的，这要求 balance layer 的权重量化采用什么策略？
**参考答案**：要求权重量化是**逐通道/逐组（CHANNEL / GROUP）**而非逐张量（TENSOR）。因为 `s` 沿输入维逐列缩放，若整张量共用一个 scale，缩放带来的幅值差异会被「整张量归一」抹平、失去保护意义。代码里 `duo_scaling` 校验正是禁止与 TENSOR 策略搭配（见 4.4.3）。

---

### 4.3 架构映射 mappings：把 smooth 与 balance 配对

#### 4.3.1 概念说明

AWQ 要对「smooth layer 及其后继 balance layer」成对操作。但每个模型架构的层名都不一样（Llama 叫 `q_proj`，Phi 融合成 `qkv_proj`，Bloom 叫 `query_key_value`…）。`mappings` 模块就是一张「**架构 → 配对规则**」的查找表，让 AWQ 知道「该平滑哪个 norm、对应去平衡哪些 Linear」。

核心数据结构是 `AWQMapping`：

```python
@dataclass
class AWQMapping:
    smooth_layer: str                      # 要平滑的激活层（norm）的 regex 或名字
    balance_layers: list[str]              # 要反向缩放的权重层列表
    activation_hook_target: str | None = None  # 可选：在哪个子模块挂激活缓存 hook
```

[src/llmcompressor/modifiers/transform/awq/mappings.py:12-36](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/mappings.py#L12-L36)

一组映射描述：**「`smooth_layer` 的输出激活 = `balance_layers` 的输入激活」**。因此平滑 smooth layer 的激活，必须同时缩放所有共享这同一份激活的 balance layer，才能整体抵消。

#### 4.3.2 核心流程：默认映射与架构注册表

以最通用的 Llama/Qwen 结构为例，`default_mappings` 列出四组配对：

```python
default_mappings = [
    AWQMapping("re:.*input_layernorm$",       ["re:.*q_proj$", "re:.*k_proj$", "re:.*v_proj$"]),
    AWQMapping("re:.*v_proj$",                ["re:.*o_proj$"]),
    AWQMapping("re:.*post_attention_layernorm$", ["re:.*gate_proj$", "re:.*up_proj$"]),
    AWQMapping("re:.*up_proj$",               ["re:.*down_proj$"]),
]
```

[src/llmcompressor/modifiers/transform/awq/mappings.py:39-53](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/mappings.py#L39-L53)

读法：

- `input_layernorm` 的输出同时喂给 `q_proj/k_proj/v_proj` 三个 Linear → 这三个是同一组 balance layer，必须**一起**缩放。
- `post_attention_layernorm` 的输出喂给 `gate_proj/up_proj`（FFN 的两个并行分支）→ 一起缩放。
- `v_proj → o_proj`、`up_proj → down_proj`：把上一层的**激活输出**当作下一层 Linear 的「smooth layer」（注意：这里 smooth 是 Linear 而非 norm，是 AWQ 对注意力/FFN 内部衔接的额外平滑）。

不同架构的差异都体现在这张表里，例如：

- **Phi** 把 q/k/v 融合成 `qkv_proj`，所以 balance 只有一个：`AWQMapping("re:.*input_layernorm$", ["re:.*qkv_proj$"])`（[mappings.py:99-113](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/mappings.py#L99-L113)）。
- **Gemma** 在 MLP 前多了一个 `pre_feedforward_layernorm`，映射改用它而非 `post_attention_layernorm`（[mappings.py:118-132](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/mappings.py#L118-L132)）。
- **Cohere** 的注意力和 MLP **并行**（都直接吃 `input_layernorm`），所以第一条映射的 balance 列表把 attn 的 q/k/v 和 mlp 的 gate/up 全合在一起（[mappings.py:139-155](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/mappings.py#L139-L155)）。

这些规则统一登记在 `AWQ_MAPPING_REGISTRY`，键是 HF 模型的类名：

```python
AWQ_MAPPING_REGISTRY: dict[str, list[AWQMapping]] = {
    "LlamaForCausalLM": default_mappings,
    "MistralForCausalLM": default_mappings,
    "Qwen2ForCausalLM": default_mappings,
    ...
    "Phi3ForCausalLM": _phi_mappings,
    "Gemma3ForCausalLM": _gemma_mappings,
    "CohereForCausalLM": _cohere_mappings,
    ...
}
```

[src/llmcompressor/modifiers/transform/awq/mappings.py:272-304](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/mappings.py#L272-L304)

`on_initialize` 在用户没传 `mappings` 时，调用 `get_layer_mappings_from_model(model)` 自动查表：

```python
def on_initialize(self, state, **kwargs) -> bool:
    if self.mappings is None:
        logger.info("No AWQModifier.mappings provided, inferring from model...")
        self.mappings = get_layer_mappings_from_model(state.model)
    ...
```

[src/llmcompressor/modifiers/transform/awq/base.py:177-179](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/base.py#L177-L179)

`get_layer_mappings_from_model` 的查找顺序是「动态注册表 → 静态注册表 → default_mappings」，找不到架构就用通用 `default_mappings` 兜底：

[src/llmcompressor/modifiers/transform/awq/dynamic_mappings.py:29-52](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/dynamic_mappings.py#L29-L52)

> 「动态注册表」`AWQ_DYNAMIC_MAPPING_REGISTRY` 服务于**混合注意力**模型（如 Qwen3Next/Qwen3.5：部分层用全注意力、部分用线性注意力），mappings 需要按层索引动态生成，不能静态写死（[dynamic_mappings.py:55-146](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/dynamic_mappings.py#L55-L146)）。

#### 4.3.3 源码精读：regex 解析成 `ResolvedMapping`

mapping 里写的是 regex（`"re:.*q_proj$"`），运行时要解析成真正指向 `torch.nn.Module` 的 `ResolvedMapping`。这件事在 `on_calibration_start` 调用的 `_set_resolved_mappings` 里完成：

```python
for mapping in self.mappings:
    for smooth_layers, *nested_balance_layers in match_modules_set(
        model, (mapping.smooth_layer, *mapping.balance_layers)
    ):
        ...
        balance_layers = tree_leaves(nested_balance_layers)
        ...
        ancestor_name, ancestor = get_lowest_common_ancestor_with_avoid(
            balance_names, model, torch.nn.ModuleList
        )
        ...
        resolved_mappings.append(ResolvedMapping(...))
```

[src/llmcompressor/modifiers/transform/awq/base.py:297-379](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/base.py#L297-L379)

要点：

- `match_modules_set`（来自 compressed_tensors）按「模块名最长公共前缀」把每一层的 smooth 与 balance 配成一组（`layer.0.input_layernorm` 配 `layer.0.q_proj/k_proj/v_proj`，`layer.1` 同理……），regex 在此被实例化。
- `get_lowest_common_ancestor_with_avoid` 找到这些 balance layer 的**最近公共父模块**（`parent`），后续整个子图的前向都从 parent 跑起；它特意**避开 `ModuleList`**，因为 MoE 里 expert 的 forward 不会被 ModuleList 直接调用（[base.py:1003-1025](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/base.py#L1003-L1025)）。
- 解析阶段还会过滤「形状不兼容」的配对（如 `v_proj→o_proj` 维度对不上的情况），并记录跳过数（[base.py:971-1000](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/base.py#L971-L1000) 与 [base.py:380-384](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/base.py#L380-L384)）。

解析结果存入私有字段 `self._resolved_mappings`（类型 `list[ResolvedMapping]`，[mappings.py:307-332](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/mappings.py#L307-L332)）。

#### 4.3.4 代码实践：查看一个模型的映射

1. **实践目标**：确认给定架构会被查到哪套 mappings，理解「类名 → 映射表」的查找。
2. **操作步骤**（示例代码，只需 transformers + 一个小模型）：

   ```python
   # 示例代码：用一个极小模型查映射，不跑量化
   from transformers import AutoModelForCausalLM
   from llmcompressor.modifiers.transform.awq.dynamic_mappings import (
       get_layer_mappings_from_model, AWQ_MAPPING_REGISTRY,
   )

   model = AutoModelForCausalLM.from_pretrained("HuggingFaceTB/SmolLM2-135M")
   print("类名:", model.__class__.__name__)
   print("命中表?", model.__class__.__name__ in AWQ_MAPPING_REGISTRY)
   for m in get_layer_mappings_from_model(model):
       print(m.smooth_layer, "->", m.balance_layers)
   ```
3. **需要观察的现象**：若类名在注册表里（如 `LlamaForCausalLM`），直接返回对应映射；否则打印「Using default mappings」并返回 `default_mappings`。
4. **预期结果**：看到形如 `.*input_layernorm$ -> ['.*q_proj$', '.*k_proj$', '.*v_proj$']` 的若干行。
5. 若该小模型类名不在注册表中，你会观察到 default 兜底——这属于预期行为；具体输出「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `input_layernorm` 的 balance 是 `[q_proj, k_proj, v_proj]` 三个，而不是只取一个？
**参考答案**：因为这三个 Linear **共享同一份激活**（`input_layernorm` 的输出）。AWQ 平滑的是这份激活，激活被 `÷s` 后，三个 Linear 的输入同时变了，必须把三个的权重都 `×s` 才能整体抵消；只补偿其中一个会让另外两个的输出失真。

**练习 2**：`activation_hook_target` 字段解决什么问题？
**参考答案**：默认 AWQ 把「缓存激活」的 hook 挂在 `balance_layers[0]`。但对**并行 transformer 块**（如 Cohere、Gemma 3，注意力和 MLP 并行），`balance_layers[0]` 可能只是某一个 expert，hook 到它只能采集到该 expert 的输入，而非流入整个分支的完整激活。`activation_hook_target`（如 `"mlp"`）指定挂到更高层的父模块，从而采集到正确的激活（见 [mappings.py:255-270](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/mappings.py#L255-L270) 的示例）。

---

### 4.4 校准执行：激活统计与网格搜索

#### 4.4.1 概念说明

知道了「缩放哪个 norm、平衡哪些 Linear」，还要回答：**缩放向量 `s` 具体取多少？** AWQ 不解析求解，而是用**网格搜索（grid search）**：对每个 smooth layer，枚举一组候选 `s`，每个候选都「真做一次量化」并测量输出误差，挑误差最小的。

`AWQModifier` 把这条「收集激活 → 网格搜索 → 重排」的流水线分散在三个钩子里（生命周期见 [base.py:91-112](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/base.py#L91-L112)）：

| 钩子 | 干什么 |
| --- | --- |
| `on_calibration_start` | 解析 mappings、校验 `duo_scaling`、挂激活缓存 hook |
| `on_sequential_epoch_end` | `_apply_smoothing`：网格搜索最优 `s`，重排权重 |
| `on_calibration_end` | 断言所有激活都被消费、卸 hook |

注意 `requires_calibration_data = True`，所以 AWQ 一定走 SequentialPipeline（详见 u3-l4）：

[src/llmcompressor/modifiers/transform/awq/base.py:142-142](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/base.py#L142-L142)

#### 4.4.2 核心流程：两个 hook 收集两类统计

`_setup_activation_cache_hooks` 注册两种 forward hook：

1. **parent 前向 hook（`cache_parent_kwargs_hook`）**：挂在 balance layer 的公共父模块上，缓存每次前向的**完整输入参数**。这些输入稍后在网格搜索里反复重放，用来计算「量化后的输出」。

   ```python
   def cache_parent_kwargs_hook(module, args, kwargs):
       values = inspect.signature(module.forward).bind(*args, **kwargs)
       ...
       self._parent_args_cache[module].append(values.arguments)
   ```

   [src/llmcompressor/modifiers/transform/awq/base.py:395-407](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/base.py#L395-L407)

2. **smooth 激活 hook（`cache_smooth_activations_hook`）**：挂在第一个 balance layer（或 `activation_hook_target`）上，跨 batch 累积激活的**绝对值之和与计数**，用来求通道均值 `x_mean = Σ|x| / count`（duo_scaling 与否都要用）。

   ```python
   new_sum = masked_activations.float().sum(dim=0).cpu()
   new_count = torch.tensor(masked_activations.size(0)).cpu()
   ...
   self._smooth_activation_stats[smooth_name][0] += new_sum   # 激活绝对值累加和
   self._smooth_activation_stats[smooth_name][1] += new_count # token 计数
   ```

   [src/llmcompressor/modifiers/transform/awq/base.py:436-444](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/base.py#L436-L444)

   `_parent_args_cache` 用 `IntermediatesCache`（u3-l5 讲过的同一套缓存）实现 CPU/GPU 间的 offload，`offload_device` 控制是否落盘省显存（MoE 模型默认 offload 到 CPU，见 [base.py:182-194](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/base.py#L182-L194)）。

#### 4.4.3 源码精读：网格搜索 `_compute_best_scale`

网格搜索的目标函数（论文公式）写得很清楚——最小化「量化缩放后的输出」与「原始 FP16 输出」的 MSE：

\[
L(s)=\bigl\|\;Q(W\cdot s)\,(s^{-1}\odot X)\;-\;W\cdot X\;\bigr\|
\]

其中 \(Q\) 是权重伪量化，\(X\) 是校准输入，\(s\) 是逐通道缩放。[src/llmcompressor/modifiers/transform/awq/base.py:610-623](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/base.py#L610-L623)

候选 `s` 的生成取决于 `duo_scaling`：

```python
if use_duo_scaling:
    scales = (x_mean.pow(ratio) / (w_mean.pow(1 - ratio) + 1e-4)).clamp(min=1e-4)
else:
    scales = x_mean.pow(ratio).clamp(min=1e-4).view(-1)
scales = scales / (scales.max() * scales.min()).sqrt()   # 归一化到几何均值≈1
```

[src/llmcompressor/modifiers/transform/awq/base.py:670-674](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/base.py#L670-L674)

- **不用 duo（仅激活）**：\(s = x_{mean}^{ratio}\)，`ratio ∈ [0,1]`。`ratio=0` 退化为 `s=1`（恒等），`ratio=1` 即纯按激活幅值缩放。
- **用 duo（激活+权重）**：\(s = x_{mean}^{ratio}/(w_{mean}^{1-ratio}+\epsilon)\)。其中 `w_mean` 由 `_compute_layer_means` 按**量化策略**（CHANNEL/GROUP/BLOCK）算出权重的逐通道归一化均值（[base.py:894-960](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/base.py#L894-L960)）。引入 `w_mean` 让权重本身较大的通道也得到保护。

每个候选 `s` 都会**真做一次量化**并算误差——这一步复用了 u3-l3 讲的校准三件套：

```python
# (W * s)
balance_layer.weight.data.copy_(orig * _scalesview)
balance_layer.weight_observer.delete_statistics(check_fused=False)
# 计算量化参数
observe(balance_layers_to_patch, "weight")
update_qparams(balance_layers_to_patch, "weight", only_update_onload=True)
# Q(W * s)
balance_layer.weight.data = forward_quantize(...) / _scalesview
# W_q * X，算 MSE
int_w_outputs = self._run_samples(mapping.parent)
loss = self._compute_loss(fp16_outputs, int_w_outputs)
```

[src/llmcompressor/modifiers/transform/awq/base.py:680-712](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/base.py#L680-L712)

注意 `Q(W·s)/s` 这一步：先量化放大后的权重，再除回 `s`，对应公式里的 \(Q(W\cdot s)\,(s^{-1}\odot X)\)。最终返回使 `loss` 最小的 `best_scales`（[base.py:720-758](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/base.py#L720-L758)）。

`duo_scaling` 三种取值的网格点由 `_get_grid_search_params` 生成：

```python
match self.duo_scaling:
    case "both":   # 一半关 duo、一半开 duo
    case False:    # 全程关 duo
    case True:     # 显式纳入 (0.0, False) 恒等点，其余开 duo
```

[src/llmcompressor/modifiers/transform/awq/base.py:798-832](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/base.py#L798-L832)

`on_calibration_start` 还会校验 `duo_scaling` 不能搭配 **TENSOR（逐张量）** 权重策略（因为逐张量量化无法体现逐通道缩放的意义）：

```python
if module.quantization_scheme.weights.strategy == QuantizationStrategy.TENSOR:
    raise ValueError("duo_scaling is only supported with per-channel quantization ...")
```

[src/llmcompressor/modifiers/transform/awq/base.py:225-239](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/base.py#L225-L239)

#### 4.4.4 代码实践：观察 duo_scaling 对网格点的影响

1. **实践目标**：直观看到 `duo_scaling` 三种取值如何改变网格搜索的候选点数与构成。
2. **操作步骤**（示例代码，无需模型/GPU）：

   ```python
   # 示例代码：直接调内部网格生成器
   from llmcompressor.modifiers.transform.awq.base import AWQModifier

   m = AWQModifier(duo_scaling="both", n_grid=20)
   params = m._get_grid_search_params()
   print("both -> 候选点数:", len(params), " 含 duo 的:", sum(p[1] for p in params))

   m2 = AWQModifier(duo_scaling=False, n_grid=20)
   print("False -> 候选点数:", len(m2._get_grid_search_params()))
   ```
3. **需要观察的现象**：`duo_scaling="both"` 时候选点「一半 duo=False、一半 duo=True」；`False` 时全部 `duo_scaling=False`。
4. **预期结果**：`both` 的总点数与 `n_grid` 一致（按 `n_grid/2` 折半再各生成两组），`False` 的点数等于 `n_grid`。
5. `_get_grid_search_params` 返回纯 list，无副作用，本地可直接复现；若结果不符请「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`duo_scaling="both"` 相比 `duo_scaling=True` 有什么额外好处？
**参考答案**：`"both"` 把网格一分为二，一半在「仅激活」模式下搜、一半在「激活+权重」模式下搜，等于同时覆盖了两种缩放策略，给搜索更大的多样性，更可能找到更优 `s`；而 `True` 几乎全在 duo 模式下搜（仅保留一个 `(0.0, False)` 恒等点）。代价是 `"both"` 单个 ratio 只搜一半点数。

**练习 2**：为什么网格搜索里要先把 observer 换成 `memoryless_minmax`？
**参考答案**：网格搜索每个候选 `s` 都要「干净地」量化一次权重并算量化参数。`memoryless_minmax` 每次只看当前这批（无记忆），保证每个候选的量化参数只由当前的 `W·s` 决定，不被上一个候选的统计污染；同时也省内存（u3-l3 讲过 memoryless 不保留历史统计）。见 [base.py:649-661](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/transform/awq/base.py#L649-L661)。

---

## 5. 综合实践

把本讲的「变换思想 + mappings + 网格搜索 + Recipe 校验」串起来，完成下面这个端到端任务。这是本讲规格指定的代码实践任务。

### 实践目标

跑通一次真正的 AWQ + W4A16 量化，并亲手触发「缺量化 modifier」的 Recipe 校验错误，从而把「AWQ 是变换、必须后接量化」这条核心约束钉死在记忆里。

### 操作步骤

1. **准备环境**：`pip install llmcompressor`（需要 GPU；首次运行会下载模型与校准数据）。
2. **跑标准 AWQ 示例**：直接运行官方示例（它就是本讲的「标准答案」用法）。

   ```bash
   python examples/awq/llama_example.py
   ```

   它的 recipe 正是 `[AWQModifier(duo_scaling="both"), QuantizationModifier(scheme="W4A16_ASYM", targets=["Linear"], ignore=["lm_head"])]`（[examples/awq/llama_example.py:52-59](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/examples/awq/llama_example.py#L52-L59)），跑完会在 `Meta-Llama-3-8B-Instruct-awq-asym/` 下生成含 `quantization_config` 的 checkpoint。

3. **复刻一个最小版**（可选，便于换小模型）：把示例改写成下面这样，套一个更小的模型以降低门槛：

   ```python
   # 示例代码：最小 AWQ + W4A16（需要 GPU + 校准数据）
   from transformers import AutoModelForCausalLM
   from llmcompressor import oneshot
   from llmcompressor.modifiers.quantization import QuantizationModifier
   from llmcompressor.modifiers.transform.awq import AWQModifier

   model = AutoModelForCausalLM.from_pretrained("HuggingFaceTB/SmolLM2-135M")

   recipe = [
       AWQModifier(duo_scaling="both"),
       QuantizationModifier(scheme="W4A16_ASYM", targets=["Linear"], ignore=["lm_head"]),
   ]

   oneshot(
       model=model,
       dataset="HuggingFaceH4/ultrachat_200k",
       num_calibration_samples=32,   # 先用很少样本快速跑通
       max_seq_length=512,
       recipe=recipe,
   )
   model.save_pretrained("smollm2-awq-w4a16", save_compressed=True)
   ```

4. **触发 Recipe 校验错误**：把上一步 recipe 里的 `QuantizationModifier(...)` **整段删掉**，只留 `AWQModifier(duo_scaling="both")`，再次运行。

### 需要观察的现象

- 步骤 2/3：日志里依次出现 `No AWQModifier.mappings provided, inferring from model...`、`Grid search for ...` 进度条、`Smoothing` 进度条，最后保存的目录里 `config.json` 含 `"quantization_config"`（说明 QuantizationModifier 完成了取整）。
- 步骤 4：**在量化还没开始前**就抛出 `ValueError: Recipe includes AWQModifier with no subsequent quantization modifer...`，程序立即终止，不会进入校准。

### 预期结果

- 正常 recipe：得到一个 W4A16 的 compressed-tensors checkpoint，体积约为 FP16 的 1/4 ~ 1/3。
- 缺量化的 recipe：被 [recipe.py:206-230](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/recipe.py#L206-L230) 的 `validate_model_after` 拦下。

> 若你没有 GPU，可只做步骤 4（4.1.4 已给出不需要模型的触发方式）和第 4.2/4.4 的纯 torch 小实践；端到端量化的具体耗时与产物体积「待本地验证」。

---

## 6. 本讲小结

- **AWQ 是变换型 modifier，不是量化 modifier**：`AWQModifier(Modifier)` 不继承 `QuantizationMixin`，它只对权重做等价缩放重排，产物仍是浮点；真正的取整由紧跟其后的 `QuantizationModifier` 完成。
- **缩放数学上等价**：`γ ← γ/s`、`W ← W·s` 使 Linear 输出逐元素不变，却把关键（激活大）通道的权重幅值抬高，降低其在 4bit 量化里的相对误差。
- **架构靠 mappings 配对**：`AWQ_MAPPING_REGISTRY` 用模型类名查到 smooth↔balance 的 regex 规则，`on_initialize` 缺省时自动推断，覆盖 Llama/Qwen/Phi/Gemma/Cohere/MoE 等几十种架构。
- **缩放向量靠网格搜索确定**：`_compute_best_scale` 枚举候选 `s`，每个都「真做一次量化」（复用 `observe`/`update_qparams`/`forward_quantize`），挑输出 MSE 最小的；`duo_scaling` 控制是否纳入权重幅值 `w_mean`。
- **Recipe 强制顺序依赖**：`validate_model_after` 要求 AWQModifier 之后必须存在 `QuantizationMixin` 子类，否则在校验阶段即报错——这是「AWQ 必须后接量化」的硬约束。
- **生命周期分工**：`on_initialize` 推断 mappings、`on_calibration_start` 解析+挂 hook、`on_sequential_epoch_end` 搜索+重排、`on_calibration_end` 卸 hook，依赖 SequentialPipeline 的逐子图校准节奏。

---

## 7. 下一步学习建议

- **对比另一种变换 modifier**：下一讲 **u4-l3（SmoothQuant）** 同属 transform 类，但它平滑的是「激活离群值」以服务 W8A8（权重+激活都量化），可与本讲对照体会「同为缩放、目标不同」。
- **深入网格搜索的量化基础**：`_compute_best_scale` 里的 `observe`/`update_qparams`/`forward_quantize` 来自 **u3-l3（校准、Observers 与 Hooks）**，若对取整细节存疑建议回看。
- **规模化 AWQ**：MoE 模型的 AWQ 涉及专家线性化与 `offload_device`，可结合 **u5-l2（MoE 建模与线性化）** 与 **u6-l2（大模型内存策略）** 阅读。
- **自定义变换**：理解了 AWQ 的「只改权重不量化」模式后，可参考 **u6-l4（自定义 Modifier）** 写自己的 transform modifier，复用 `QuantizationMixin` 完成端到端压缩。
