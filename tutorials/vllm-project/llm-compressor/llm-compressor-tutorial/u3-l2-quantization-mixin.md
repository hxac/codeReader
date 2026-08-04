# QuantizationMixin 把方案挂到模块

## 1. 本讲目标

[u3-l1](u3-l1-quantization-modifier-and-scheme.md) 我们站在 `QuantizationModifier` 的「外面」看完了它的字段、scheme 解析和 RTN 的执行点。但我们一直把 `initialize_quantization` / `start_calibration` / `end_calibration` 当成三个黑盒——只知道它们「挂方案」「开量化」「冻结」。本讲要打开这三个黑盒，回答四个问题：

1. 一个 `QuantizationScheme`（方案）到底是**怎样**从 modifier 身上「搬」到一个具体的 `nn.Module` 身上的？模块被挂上方案后多了哪些属性？
2. 校准时那些收集统计的 observer、那些拦截前向的 calibration hook，是在哪一步、按什么规则挂上去的？校准结束又如何卸下？
3. `QuantizationMixin` 为什么**自己不算 scale / zero-point**，而要把这件事留给子类（如 `QuantizationModifier`、`GPTQModifier`）显式调用 `observe` + `update_qparams`？
4. `group_size`（分组量化的组大小）的整除性为什么要在**初始化阶段**就提前校验、甚至提前报错？

学完后你应当能够：画出「裸模块 → 挂方案 → 挂 observer+hook → 算 scale → 卸 hook+冻结」这条模块状态流转链，说清每一步由 mixin 的哪个方法驱动，并解释「挂方案」与「算 scale」为何被设计成两件分离的事。

本讲承接 [u3-l1](u3-l1-quantization-modifier-and-scheme.md)（量化字段、scheme 解析、RTN 执行点）和 [u2-l3](u2-l3-modifier-base-lifecycle.md)（`Modifier` 双生命周期钩子）。关于 observer 内部如何收集与融合统计，本讲只讲到理解挂载流程所需的程度，完整精读留给 [u3-l3](u3-l3-calibration-observers-hooks.md)。

## 2. 前置知识

先建立两个直觉，再读源码会很顺。

**直觉一：量化是给模块「打补丁」，不是替换模块。**

llm-compressor 不会把你的 `nn.Linear` 换成一个新的「量化 Linear」。它做的是：在**原有模块上挂一组属性**——一个描述方案的 `quantization_scheme`、一组收集统计的 `*_observer`、若干拦截前向的 hook、以及最终写回的 `weight_scale` / `weight_zero_point` 等参数。模块的类没变，只是身上多了「量化装备」。这些装备由上游库 `compressed_tensors` 在前向时识别并执行「模拟量化」。

回忆 [u3-l1](u3-l1-quantization-modifier-and-scheme.md) 的量化公式：

\[
q = \mathrm{round}\!\left(\frac{x}{s}\right) + z,\qquad
\hat{x} = s\cdot(q - z)
\]

这里 \(s\)（scale）和 \(z\)（zero-point）就是 mixin 要负责「安放」到模块上的量化参数。但**关键在于：mixin 只负责把「放参数的位置」准备好，并不负责把 \(s,z\) 的数值算出来**。

**直觉二：「挂方案」「算参数」「冻结」是三件故意分开的事。**

| 阶段 | 做什么 | 谁来做 | 量化是否生效 |
|------|--------|--------|--------------|
| 挂方案 | 给模块装上 `quantization_scheme`，关掉量化 | `initialize_quantization` | 关闭（前向仍是全精度） |
| 算参数 | 用 observer 收集统计、算出 scale/zp 写回模块 | **子类**显式调用 `observe`+`update_qparams` | 校准中开启 |
| 冻结 | 卸 hook、删 observer、固定 scale/zp | `end_calibration` | 开启（供推理） |

为什么分开？因为「怎么算 scale」是**算法相关**的：RTN 用权重的 min/max、GPTQ 用 Hessian、AWQ 先做缩放变换。这些差异不该写进通用的 mixin。mixin 只提供「挂/卸装备」的通用脚手架，把「算 scale」这个差异化动作留给每个算法子类自己决定——这就是本讲反复强调的核心设计。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/llmcompressor/modifiers/quantization/quantization/mixin.py` | 本讲主角。`QuantizationMixin` 定义量化字段、scheme 解析，以及 `initialize_quantization` / `start_calibration` / `end_calibration` 三个搭脚手架的方法 |
| `src/llmcompressor/modifiers/quantization/group_size_validation.py` | group_size 整除性的提前校验，在「挂方案」后立即检查，避免校准跑完才发现保存会失败 |
| `src/llmcompressor/modifiers/quantization/quantization/base.py` | `QuantizationModifier`，展示 mixin 的三个方法在生命周期钩子里的**调用点**（本讲关注调用关系，字段细节见 u3-l1） |
| `src/llmcompressor/modifiers/quantization/calibration.py` | `initialize_observer` / `apply_calibration_status` / `freeze_module_quantization` / `reset_quantization_status` 等被 mixin 调用的底层函数 |
| `src/llmcompressor/modifiers/utils/hooks.py` | `HooksMixin`，提供 `register_hook` / `remove_hooks`，是校准 hook 挂载/卸载的基础设施 |

## 4. 核心概念与源码讲解

### 4.1 QuantizationMixin 的角色：搭脚手架，但不取整

#### 4.1.1 概念说明

`QuantizationMixin` 是一个**混入类（mixin）**——它自己不能独立实例化，而是被 `QuantizationModifier`、`GPTQModifier`、`AWQModifier` 等算法类继承，用来「混入」一套通用的量化能力。它的类声明很简洁：

[src/llmcompressor/modifiers/quantization/quantization/mixin.py:55-56](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L55-L56) — `QuantizationMixin(HooksMixin)`，继承 `HooksMixin` 获得 hook 管理能力（校准 hook 的挂/卸就靠它）。

它的 docstring 把三阶段生命周期和「不自己取整」的约定写得非常清楚，是理解整个类的纲领：

[src/llmcompressor/modifiers/quantization/quantization/mixin.py:56-79](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L56-L79) — 类 docstring。注意第 75-79 行那段 `NOTE`：**QuantizationMixin 不会自己更新 scale 和 zero-point**，因为不是所有继承它的 modifier 都想要这个行为；子类必须**显式**调用 `observe(modules, base_name="weight")` 再 `update_qparams(modules, base_name="weight")`。

这段 NOTE 是本讲最重要的一句话。它解释了为什么我们在 [u3-l1](u3-l1-quantization-modifier-and-scheme.md) 看到 RTN 的「取整」发生在 `QuantizationModifier.on_sequential_epoch_end` 里，而不是发生在 mixin 的某个方法里——因为 mixin 故意把这一步让出来了。

#### 4.1.2 核心流程

mixin 对外的三个方法，恰好对应模块状态机的三次「跳转」：

```
裸 nn.Module
   │  initialize_quantization(model)     ← on_initialize 调用
   ▼
挂了 quantization_scheme，但量化关闭
   │  start_calibration(model)           ← on_calibration_start 调用
   ▼
挂了 observer + calibration hook，状态=CALIBRATION
   │  （子类在此期间 observe + update_qparams 算出 scale/zp）
   ▼
   │  end_calibration(model)             ← on_calibration_end 调用
   ▼
卸了 hook + 删了 observer，状态=FROZEN，scale/zp 固定，量化开启
```

`QuantizationModifier` 把这三个方法分别转发到 `on_initialize` / `on_calibration_start` / `on_calibration_end` 三个钩子里（`on_sequential_epoch_end` 钩子则留给子类做 observe+update_qparams）。也就是说，**mixin 负责「头」和「尾」以及「开关」，子类负责「中间算参数」**。

#### 4.1.3 源码精读

来看 `QuantizationModifier` 是如何把 mixin 的三个方法嵌入钩子的。这三个钩子体都极薄，几乎只是一行转发：

[src/llmcompressor/modifiers/quantization/quantization/base.py:58-75](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/base.py#L58-L75) — `on_initialize`：先 `has_config` 防御（没配字段就报错），再调 `initialize_quantization` 挂方案。注意它**只调了 mixin 的方法，没有任何算 scale 的代码**。

[src/llmcompressor/modifiers/quantization/quantization/base.py:77-81](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/base.py#L77-L81) — `on_calibration_start`：一行转发 `start_calibration`。

[src/llmcompressor/modifiers/quantization/quantization/base.py:110-114](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/base.py#L110-L114) — `on_calibration_end`：一行转发 `end_calibration`。

真正「算参数」的 `on_sequential_epoch_end`（[base.py:83-108](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/base.py#L83-L108)）是 `QuantizationModifier` **自己**写的，里面显式调了 `observe` + `update_qparams`——这正是 docstring NOTE 要求子类做的事。对比之下你能清楚看到：mixin 不碰这一步。

#### 4.1.4 代码实践

1. **实践目标**：确认 mixin 的三个方法被调用时，子类的 `on_sequential_epoch_end` 才是算 scale 的地方。
2. **操作步骤**：对照阅读 `QuantizationModifier` 的四个钩子（[base.py:58-114](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/base.py#L58-L114)），用笔在每个钩子里标注它调的是「mixin 的脚手架方法」还是「显式 observe/update_qparams」。
3. **需要观察的现象**：`on_initialize` / `on_calibration_start` / `on_calibration_end` 三个钩子里**完全找不到** `observe` 或 `update_qparams` 的调用。
4. **预期结果**：理解「挂方案、开关量化」与「算 scale」在代码物理位置上是分离的——前者在 mixin，后者在子类的 `on_sequential_epoch_end`。

#### 4.1.5 小练习与答案

**练习 1**：如果我要写一个「只用 min/max 取整」的新量化 modifier，`QuantizationMixin` 能帮我省掉哪部分工作？

> **答案**：它能帮你省掉「挂方案、挂 observer、挂/卸 calibration hook、开关量化、冻结」这一整套与具体算法无关的脚手架。你只需继承 `QuantizationMixin`（连同 `Modifier`），实现 `on_sequential_epoch_end` 里调 `observe(weight)` + `update_qparams(weight)` 即可。

**练习 2**：为什么 mixin 的 docstring 强调「not desired for all Modifiers inheriting from it」？举一个不想要自动取整的例子。

> **答案**：像 `AWQModifier`（[u4-l2](u4-l2-awq-transform.md)）这种**变换类** modifier，它在量化前要先对权重做缩放重排，自己并不直接做最终的量化取整——最终取整由 recipe 里紧跟在它后面的 `QuantizationModifier` 完成。如果 mixin 自动取整，就会破坏这种「先变换、后量化」的组合。所以取整必须显式触发。

---

### 4.2 initialize_quantization：把 scheme 挂到模块

#### 4.2.1 概念说明

`initialize_quantization(model)` 是三阶段的第一步，在 `on_initialize` 钩子里被调用。它的职责很纯粹：**把 modifier 身上解析好的 `QuantizationConfig`，落到模型的每个目标模块上**，但**不启用量化**。

这里要分清两个「目标」概念（[u3-l1](u3-l1-quantization-modifier-and-scheme.md) 已铺垫）：`targets` 字段不是命中目标的唯一来源，真正决定「哪些模块被挂方案」的是 `resolved_targets` 属性。`initialize_quantization` 内部用的也是 `resolved_targets`。

#### 4.2.2 核心流程

`initialize_quantization` 做四件事，顺序很重要：

```
initialize_quantization(model):
  1. reset_quantization_status   # 清掉模块上残留的旧 quantization_status
  2. apply_quantization_config   # 把 resolved_config 写到 model，命中模块获得 quantization_scheme
  3. validate_group_size_divisibility  # 若非 bypass，校验 group_size 整除性（见 4.5）
  4. model.apply(disable_quantization) # 关闭量化：前向仍是全精度
```

第 2 步是核心：`apply_quantization_config`（来自 `compressed_tensors`）会遍历模型，给每个匹配 `resolved_targets` 的模块挂上 `quantization_scheme` 属性。从这一刻起，模块「知道」自己要被量化成什么方案，但量化还没生效（被第 4 步关掉了）。

为什么第 4 步要关掉量化？因为挂上 scheme 后，`compressed_tensors` 的前向会自动「模拟量化执行」。但在校准真正开始前，我们不希望量化介入——否则会污染后续 observer 收集到的统计，或干扰上游模块的前向。所以先 disable，把「挂方案」和「启用量化」在时间上分开。

#### 4.2.3 源码精读

`initialize_quantization` 的实现只有十几行，但每行都对应上面流程的一步：

[src/llmcompressor/modifiers/quantization/quantization/mixin.py:221-238](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L221-L238) — `initialize_quantization` 全貌。第 229-230 行用 `match_named_modules(model, self.resolved_targets, self.ignore)` 找出命中模块并 reset；第 232 行 `apply_quantization_config` 挂方案；第 234-235 行条件性地校验整除性；第 238 行关闭量化。

`match_named_modules`（来自 `compressed_tensors.utils`）既按模块的全限定名（FQN）模式匹配，也按模块的**类名**匹配。这就是为什么 `targets=["Linear"]` 能命中所有 `nn.Linear`——靠的是类名 `Linear`。`ignore` 则把不该量化的模块（如 `lm_head`）排除在外。

`reset_quantization_status` 的实现证明「挂方案」前会先清理旧状态，是幂等的：

[src/llmcompressor/modifiers/quantization/calibration.py:234-238](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py#L234-L238) — `reset_quantization_status`：遍历模型所有模块，删掉残留的 `quantization_status` 属性。这保证重复 initialize 不会脏读旧状态。

`resolved_targets` 是命中目标的「真相之源」，它汇总了 config_groups 里的所有 targets：

[src/llmcompressor/modifiers/quantization/quantization/mixin.py:202-219](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L202-L219) — `resolved_targets` 属性：遍历 `resolved_config.config_groups` 收集所有 target，若设了 `kv_cache_scheme` 还补上 `KV_CACHE_TARGETS`（`q_proj`/`k_proj` 等）。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到「挂方案」让一个裸 `Linear` 凭空多出 `quantization_scheme` 属性。
2. **操作步骤**（**示例代码**，手动驱动 mixin 的第一个阶段）：
   ```python
   import torch
   from llmcompressor.modifiers.quantization import QuantizationModifier

   linear = torch.nn.Linear(32, 16)
   model = torch.nn.Sequential(linear)  # 包一层，便于按类名匹配
   mod = QuantizationModifier(targets="Linear", scheme="W4A16")

   print("挂方案前:", hasattr(linear, "quantization_scheme"))  # False
   mod.initialize_quantization(model)
   print("挂方案后:", hasattr(linear, "quantization_scheme"))  # True
   print(linear.quantization_scheme)          # W4A16 的完整方案
   print("此时已有 observer 吗:", hasattr(linear, "weight_observer"))  # False（observer 在下一步才挂）
   ```
3. **需要观察的现象**：挂方案前模块没有任何量化属性；挂方案后多了 `quantization_scheme`，但**还没有** `weight_observer`（observer 属于下一步 `start_calibration`）。
4. **预期结果**：理解「挂方案」只装 `quantization_scheme`，不装 observer、不算 scale。**待本地验证**：`quantization_scheme` 打印出的具体字段（weights/input_activations 等）以你环境的 `compressed_tensors` 版本为准。

#### 4.2.5 小练习与答案

**练习 1**：`initialize_quantization` 末尾为什么要 `model.apply(disable_quantization)`？

> **答案**：挂上 scheme 后，`compressed_tensors` 前向会自动模拟量化。但在校准开始前，量化不该介入（否则污染统计、干扰前向）。所以先 disable，等 `start_calibration` 再打开，从而把「挂方案」与「启用量化」在时间上分离。

**练习 2**：为什么用 `resolved_targets` 而不是直接用 `targets` 字段去匹配模块？

> **答案**：因为目标可能来自 `config_groups` 内部各 group 自己声明的 `targets`，甚至来自 `kv_cache_scheme` 触发的 `KV_CACHE_TARGETS`，而不仅是 modifier 顶层的 `targets` 字段。`resolved_targets`（[mixin.py:202-219](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L202-L219)）把这些来源汇总，才是真正的命中集合。

---

### 4.3 start_calibration：挂 observer 与校准 hook，打开量化

#### 4.3.1 概念说明

`start_calibration(model)` 在 `on_calibration_start` 钩子里被调用，是三阶段中「装备最多」的一步。它要给每个目标模块挂上**两类装备**：

- **observer**（观察者）：收集张量统计（如 min/max），是后续算 scale/zp 的原料。按观察对象分为 `weight_observer`（权重）、`input_observer`（输入激活）、`output_observer`（输出激活），以及对 KV cache 的 `q_observer`/`k_observer`/`v_observer`。
- **calibration hook**（校准钩子）：拦截模块的前向，把流过的激活喂给对应的 observer 累积统计。

挂完装备后，它把模块状态切到 `CALIBRATION`，让量化在校准期间生效。

一个关键判断：**不是每种激活都会挂 observer/hook**。只有「需要静态校准的激活」才挂——动态激活（`dynamic=True`）的 scale 在推理时实时算，不需要在校准里收集统计。这个判断贯穿 `_initialize_observers` 和 `_initialize_hooks` 两个辅助方法。

#### 4.3.2 核心流程

```
start_calibration(model):
  若命中了词嵌入(embedding) → untie_word_embeddings（解绑 input/output embedding）
  对每个命中模块 module:
     _initialize_observers(module)   # 按 scheme 决定挂哪些 observer
     _initialize_hooks(module)        # 按 scheme 决定挂哪些前向 hook
     apply_calibration_status(module) # 状态 → CALIBRATION
  fuse_weight_observers(model)        # 把融合组(Q/K/V、gate/up)的权重 observer 链接起来
```

`_initialize_observers` 和 `_initialize_hooks` 用的是**同一套判断条件**（哪些激活需要校准），只是一个挂 observer、一个挂 hook。两者都先算三个布尔值：

- `input`：输入激活存在且**非动态**（`dynamic in (False, LOCAL)`）→ 需要
- `weight`：有权重量化（`weights is not None`）→ 需要
- `output`：输出激活存在且**非动态** → 需要

还有一个特殊分支：如果模块是**注意力缓存模块**（`is_cached_attention_module`，用于 KV cache 量化），输入侧不挂普通的 input observer/hook，而是挂 `q`/`k`/`v` 三件套。

#### 4.3.3 源码精读

`start_calibration` 的主体：

[src/llmcompressor/modifiers/quantization/quantization/mixin.py:240-257](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L240-L257) — 第 248-249 行：若命中的目标包含词嵌入，先 `untie_word_embeddings` 解绑（因为 input/output embedding 共享权重时，分别量化会冲突）。第 251-254 行对每个命中模块依次挂 observer、挂 hook、切到 CALIBRATION 状态。第 257 行 `fuse_weight_observers` 把融合组链接起来。

`_initialize_observers` 展示了「按 scheme 决定挂哪些 observer」的完整判断：

[src/llmcompressor/modifiers/quantization/quantization/mixin.py:436-466](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L436-L466) — 先读 `module.quantization_scheme`，算出 `input`/`weight`/`output` 三个布尔值；按需调 `initialize_observer(module, base_name=...)`。注意第 461-462 行注释点明：**weight observer 是供子类（或 `observe`/`update_qparams`）使用的**——再次印证 mixin 只准备装备、不算参数。

`_initialize_hooks` 用同一套布尔判断决定挂哪些 hook，两者结构几乎对称：

[src/llmcompressor/modifiers/quantization/quantization/mixin.py:468-498](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L468-L498) — 输入侧挂 `calibrate_input_hook`（`forward_pre` 类型，拦截输入），输出侧挂 `calibrate_output_hook`（`forward` 类型，拦截输出）。注意力模块则挂 `calibrate_query_hook`/`calibrate_key_hook`/`calibrate_value_hook`。这里调的是 `self.register_hook`（来自 `HooksMixin`），而不是原生的 `module.register_*_hook`，原因见下。

`HooksMixin.register_hook` 的特别之处：它把 hook 包了一层，使其能被 `disable_hooks()` 上下文管理器**临时禁用**：

[src/llmcompressor/modifiers/utils/hooks.py:69-106](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/utils/hooks.py#L69-L106) — `register_hook`：用 `wrapped_hook` 包裹真实 hook，当 `HooksMixin._HOOKS_DISABLED=True` 且该 hook 不在 `_HOOKS_KEEP_ENABLED` 时直接跳过。这个机制让管线在「捕获量化后的激活」时能临时关掉校准 hook，避免二次统计。

`initialize_observer` 的底层实现揭示了一个省内存的细节：**权重 observer 会被强制降级成 memoryless 版本**：

[src/llmcompressor/modifiers/quantization/calibration.py:36-82](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py#L36-L82) — `initialize_observer`：当 `base_name=="weight"` 时，把 `minmax`/`static_minmax` 改写成 `memoryless_minmax`、把 `mse` 改写成 `memoryless_mse`（第 64-78 行），注释说「training is no longer supported: always use memoryless for weights」。memoryless 意味着 observer 只记当前这一次的统计、不累积历史，从而省掉保存全部校准样本统计的内存。

`apply_calibration_status` 把模块切到校准态：

[src/llmcompressor/modifiers/quantization/calibration.py:199-204](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py#L199-L204) — `apply_calibration_status`：把 `module.quantization_status` 设为 `QuantizationStatus.CALIBRATION`。

#### 4.3.4 代码实践

1. **实践目标**：看到 `start_calibration` 让模块挂上 observer，并切到 CALIBRATION 状态。
2. **操作步骤**（**示例代码**，承接 4.2.4 的 `mod`/`model`/`linear`）：
   ```python
   print("start 前 weight_observer:", hasattr(linear, "weight_observer"))  # False
   print("start 前 input_observer :", hasattr(linear, "input_observer"))   # False（W4A16 不量化激活）
   mod.start_calibration(model)
   print("start 后 weight_observer:", hasattr(linear, "weight_observer"))  # True
   print("start 后 input_observer :", hasattr(linear, "input_observer"))   # 仍 False：W4A16 激活保持 FP16，不挂
   print("quantization_status:", getattr(linear, "quantization_status", None))
   ```
3. **需要观察的现象**：`weight_observer` 出现；但 `input_observer` **不会**出现——因为 W4A16 不量化激活，`_initialize_observers` 里 `input` 布尔值为 False。
4. **预期结果**：直观体会「按 scheme 决定挂哪些 observer」。若想看到 `input_observer`，把 scheme 换成带静态激活的方案（如 `W8A8`）。**待本地验证**：`quantization_status` 的具体枚举值以实际打印为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 W4A16 方案下，`start_calibration` 不会给模块挂 `input_observer` 和 input 的 calibration hook？

> **答案**：W4A16 只量化权重、激活保持 FP16 不量化，所以 scheme 里没有 `input_activations`（或其 dynamic=True）。`_initialize_observers`/`_initialize_hooks` 算出的 `input` 布尔值为 False，于是跳过 input observer 和 input hook。只有带静态激活的方案（如 W8A8 Int8）才会挂。

**练习 2**：`start_calibration` 为什么要把 hook 注册到 `self.register_hook`（HooksMixin）而不是直接 `module.register_forward_hook`？

> **答案**：因为校准 hook 需要能在「捕获量化后激活」时被临时禁用（避免把同一段激活统计两次）。`HooksMixin.register_hook`（[hooks.py:69-106](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/utils/hooks.py#L69-L106)）把 hook 包了一层，配合 `disable_hooks()` 上下文管理器统一开关。原生 hook 没有这个能力。

**练习 3**：`fuse_weight_observers(model)` 在 `start_calibration` 末尾做什么？

> **答案**：它把融合组（如注意力的 Q/K/V、MLP 的 gate/up）的权重 observer 链接起来，以便在 TENSOR_GROUP 策略下共同计算一个共享的 `global_scale`。详见 [u3-l3](u3-l3-calibration-observers-hooks.md)。

---

### 4.4 end_calibration：卸 hook、删 observer、冻结

#### 4.4.1 概念说明

`end_calibration(model)` 在 `on_calibration_end` 钩子里被调用，是三阶段的收尾。此时校准已经结束、scale/zero-point 已经被子类算好并写回模块。`end_calibration` 要做的是**清理校准装备**（hook 和 observer 都是一次性的，校准完就没用了）并把模块**冻结**到一个稳定的量化态，供后续推理或保存。

关键区别：`start_calibration` 是「挂装备 + 打开量化」，`end_calibration` 是「卸装备 + 保持量化开启」。注意是「保持开启」而不是「关闭」——因为校准结束后，模块要带着算好的 scale/zp 持续以量化模式前向（用于验证、保存、最终被 vLLM 加载）。

#### 4.4.2 核心流程

```
end_calibration(model):
  1. remove_hooks(_calibration_hooks)     # 卸下所有校准 hook
  2. 对每个命中模块: freeze_module_quantization  # 删 observer，状态 → FROZEN
  3. model.apply(enable_quantization)      # 保持量化开启
```

三步分别对应「卸 hook」「删 observer+冻结」「确保量化开着」。observer 在这里被删除——因为统计已经转化成了 scale/zp 写在模块上，observer 这一「收集原料的工具」可以丢弃了。

#### 4.4.3 源码精读

`end_calibration` 的实现和 `start_calibration` 严格对称：

[src/llmcompressor/modifiers/quantization/quantization/mixin.py:259-270](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L259-L270) — 第 266 行 `remove_hooks` 卸下校准 hook；第 267-268 行对命中模块逐个 `freeze_module_quantization`；第 270 行 `enable_quantization` 保持量化开启。

`freeze_module_quantization` 是「删 observer + 设 FROZEN」的实现，并且是幂等的：

[src/llmcompressor/modifiers/quantization/calibration.py:207-231](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py#L207-L231) — 遍历 `input`/`weight`/`output`/`q`/`k`/`v` 六种 observer，逐个 `detach` 并 `delattr` 删除（第 224-229 行），然后把 `quantization_status` 设为 `FROZEN`。若已经是 FROZEN 则直接返回（第 220-222 行），保证重复调用安全。

注意 `remove_hooks` 收到的是 mixin 私有属性 `_calibration_hooks`（[mixin.py:141](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L141)），它在 `start_calibration` 里通过 `_initialize_hooks` 的返回值累积（[mixin.py:253](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L253)）。这是一组成对使用：start 时挂入集合，end 时统一卸下。

#### 4.4.4 代码实践

1. **实践目标**：看到 `end_calibration` 把 observer 删掉、hook 卸掉，但量化保持开启。
2. **操作步骤**（**示例代码**，承接 4.3.4；在 end 之前先手动做一次 weight 的 observe+update_qparams，模拟子类的取整）：
   ```python
   from llmcompressor.modifiers.quantization.calibration import observe, update_qparams
   observe(linear, "weight")           # 模拟子类：收集权重统计
   update_qparams(linear, "weight")    # 算出 scale/zp 写回模块
   print("end 前 weight_scale:", hasattr(linear, "weight_scale"))   # True（已写回）
   print("end 前 observer :", hasattr(linear, "weight_observer"))  # True
   mod.end_calibration(model)
   print("end 后 observer :", hasattr(linear, "weight_observer"))  # False（被 freeze 删除）
   print("end 后 weight_scale:", hasattr(linear, "weight_scale"))   # True（保留）
   print("quantization_status:", getattr(linear, "quantization_status", None))  # FROZEN
   ```
3. **需要观察的现象**：observer 在 `end_calibration` 后消失，但 `weight_scale` 保留，状态变为 FROZEN。
4. **预期结果**：理解「observer 是一次性的统计工具，校准完即删；scale/zp 是成果，永久留在模块上」。**待本地验证**：`weight_scale` 的确切属性名与张量形状依 scheme 而定。

#### 4.4.5 小练习与答案

**练习 1**：`end_calibration` 里是 `enable_quantization` 而不是 `disable_quantization`，为什么？

> **答案**：校准结束后，模块要带着算好的 scale/zp **持续以量化模式前向**——无论是后续做量化误差验证、保存 checkpoint，还是最终被 vLLM 加载，都需要量化生效。所以是「保持开启」。对比 `initialize_quantization` 末尾的 `disable_quantization`，二者方向相反，正好对应「校准前关闭、校准后开启」。

**练习 2**：`freeze_module_quantization` 为什么要删 observer 而不是保留？

> **答案**：observer 只是「收集统计的临时工具」，校准结束时它累积的统计已经被 `update_qparams` 转化成 scale/zero-point 写在模块上了。保留 observer 既浪费内存，又可能在校准后误触发统计。所以删除（[calibration.py:224-229](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py#L224-L229)），并把状态锁定为 FROZEN。

---

### 4.5 group_size 整除性：为何要在初始化时提前报错

#### 4.5.1 概念说明

`group_size`（组大小）是「按组量化」（strategy=GROUP 或 TENSOR_GROUP）的一个关键参数：它把权重的一定列数划为一组，每组共享一个 scale。比如 `group_size=128` 表示每 128 列共用一个 scale。

这里有个**硬性数学约束**：对于 GROUP / TENSOR_GROUP 策略，权重的列数必须能被 `group_size` 整除。因为分组是按列均分进行的，列数不能整除组大小，就无法均分成完整的组。如果不整除，`compressed_tensors` 在**保存或前向**时会抛 `ValueError`。

问题在于：这个错误默认发生在很靠后的阶段（保存时），而校准（尤其 GPTQ）可能跑了很久。为了不浪费用户的时间，llm-compressor 选择在 `initialize_quantization`（挂方案之后、校准之前）就**提前检查并报错**，把出问题的层名一次性列出来，让用户加到 `ignore` 里。

#### 4.5.2 核心流程

整除性的数学条件（对应 `compressed_tensors` 前向里触发 `ValueError` 的同一条件）：

\[
\text{columns} \geq \text{group\_size} \quad\text{且}\quad \text{columns} \bmod \text{group\_size} \neq 0 \;\Rightarrow\; \text{不整除，需报错}
\`

校验策略分三类（`group_size_validation.py` 顶部 docstring 是单一真相源）：

| 策略 | 是否校验 | 原因 |
|------|----------|------|
| GROUP / TENSOR_GROUP | **校验，不整除则报错** | 运行/保存 kernel 要求 `columns % group_size == 0`，当前无非整除支持 |
| BLOCK | 不校验 | block kernel 支持 `strategy_cdiv(strict=False)`，允许非整除 |
| CHANNEL / TENSOR / TOKEN / ATTN_HEAD | 不校验 | 本身没有 group_size 整除要求 |

#### 4.5.3 源码精读

`initialize_quantization` 第 234-235 行就是校验的调用点：

[src/llmcompressor/modifiers/quantization/quantization/mixin.py:234-235](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L234-L235) — 只有 `bypass_divisibility_checks=False`（默认）时才校验。`bypass_divisibility_checks` 字段（[mixin.py:139](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L139)）让运行时支持非整除维度（如某些 vLLM 配置）的用户可以跳过。

判定单层是否不整除的核心函数：

[src/llmcompressor/modifiers/quantization/group_size_validation.py:38-55](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/group_size_validation.py#L38-L55) — `_layer_indivisible`：先看 strategy 是否为 GROUP/TENSOR_GROUP（第 43-45 行），不是就直接返回 None；再读 `module.weight.shape[-1]`（列数）和 `group_size`，套用上面的整除条件。返回 `(columns, group_size)` 或 None。

收集所有不整除的层：

[src/llmcompressor/modifiers/quantization/group_size_validation.py:58-92](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/group_size_validation.py#L58-L92) — `get_layers_indivisible_by_group_size`：遍历命中模块，对每个有 `weights` scheme 的调 `_layer_indivisible`，返回 `(fqn, columns, group_size)` 列表。注意它用 `disable_onloading()` 上下文（第 80 行），避免逐层 onloading 干扰读取权重形状。

报错信息把出问题的层名和数值都列出来，并给出修复建议：

[src/llmcompressor/modifiers/quantization/group_size_validation.py:95-123](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/group_size_validation.py#L95-L123) — `validate_group_size_divisibility`：若有不整除层，抛 `ValueError`，信息里逐行列出 `fqn (columns=..., group_size=...)`，并提示「加到 `ignore` 或设 `bypass_divisibility_checks=True`」。`bypass=True` 时直接跳过（第 108-109 行）。

#### 4.5.4 代码实践

1. **实践目标**：亲手触发一次整除性报错，看清报错信息长什么样。
2. **操作步骤**（**示例代码**）：
   ```python
   import torch
   from llmcompressor.modifiers.quantization import QuantizationModifier

   # 一个列数=32 的 Linear，group_size=128 > 32，但因 columns < group_size 不会触发
   # 改成列数=96、group_size=128：96 < 128，仍不触发（条件要求 columns >= group_size）
   # 要触发，需要 columns >= group_size 且不整除，例如 columns=160, group_size=128
   linear = torch.nn.Linear(160, 4)  # in_features=160 列
   model = torch.nn.Sequential(linear)
   mod = QuantizationModifier(
       targets="Linear",
       config_groups={"group_0": dict(num_bits=4, type="int", strategy="group",
                                      group_size=128, targets=["Linear"])},
   )
   mod.initialize_quantization(model)  # 期望在此抛 ValueError
   ```
3. **需要观察的现象**：`initialize_quantization` 抛出 `ValueError`，信息列出 `0 (columns=160, group_size=128)`，并提示加到 `ignore`。
4. **预期结果**：理解整除性校验是「早期失败」设计——在校准前就把不可保存的配置拦下。**待本地验证**：`config_groups` 手写字典的字段名（strategy/group_size 等）以你环境的 `compressed_tensors` 版本为准；若构造不出非整除场景，可调大 group_size 或调整列数。

#### 4.5.5 小练习与答案

**练习 1**：为什么 BLOCK 策略不参与整除性校验？

> **答案**：block kernel 支持 `strategy_cdiv(strict=False)`，允许非整除维度（用 cdiv 向上取整补齐），所以没有 `columns % group_size == 0` 的硬约束。只有 GROUP/TENSOR_GROUP 的 kernel 要求严格整除（见 `group_size_validation.py` 顶部 docstring 的 Policy 说明）。

**练习 2**：如果我的运行时（如 vLLM）确实支持非整除维度，怎么跳过这个报错？

> **答案**：在 `QuantizationModifier` 上设 `bypass_divisibility_checks=True`（[mixin.py:139](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L139)），`validate_group_size_divisibility` 在 `bypass=True` 时直接返回（[group_size_validation.py:108-109](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/group_size_validation.py#L108-L109)）。

**练习 3**：为什么校验放在 `initialize_quantization`（挂方案后）而不是更早？

> **答案**：因为校验需要读「每个命中模块的真实权重列数」和「挂在模块上的 scheme 的 group_size」。这两样东西只有在 `apply_quantization_config` 把 scheme 挂到模块之后才齐备。所以校验必须排在挂方案之后、校准之前——这个位置既拿得到数据，又能在昂贵的校准跑起来之前失败。

---

## 5. 综合实践：手动驱动 QuantizationMixin 的三阶段

本任务把本讲五个模块串起来：在一个 `Linear` 模块上**手动**调用 mixin 的 `initialize_quantization` → `start_calibration` → `end_calibration`，配合子类职责的 `observe` + `update_qparams`，完整跑一遍模块状态流转，并打印每个阶段前后的属性变化。

### 5.1 实践目标

亲手验证：
1. 「挂方案」只给模块装上 `quantization_scheme`，不装 observer、不算 scale。
2. 「开始校准」按 scheme 装上 observer 与 hook，切到 CALIBRATION。
3. 「算 scale」是**子类**（这里由我们手动代行）通过 `observe` + `update_qparams` 完成的，mixin 不参与。
4. 「结束校准」卸 hook、删 observer、冻结，但 scale/zp 保留、量化开启。
5. group_size 整除性在挂方案后立即被校验。

### 5.2 操作步骤

准备一个脚本（**示例代码**，非项目原有）：

```python
# 示例代码：drive_mixin.py
import torch
from llmcompressor.modifiers.quantization import QuantizationModifier
from llmcompressor.modifiers.quantization.calibration import observe, update_qparams

linear = torch.nn.Linear(128, 16)
model = torch.nn.Sequential(linear)
mod = QuantizationModifier(targets="Linear", scheme="W4A16")

def snap(tag):
    print(f"[{tag}] scheme={hasattr(linear,'quantization_scheme')} "
          f"w_obs={hasattr(linear,'weight_observer')} "
          f"w_scale={hasattr(linear,'weight_scale')} "
          f"status={getattr(linear,'quantization_status',None)}")

snap("0 裸模块")
mod.initialize_quantization(model);   snap("1 挂方案后")
mod.start_calibration(model);         snap("2 开始校准后")
observe(linear, "weight")             # 子类职责：收集权重统计
update_qparams(linear, "weight")      # 子类职责：算 scale/zp 写回
snap("3 observe+update_qparams 后")
mod.end_calibration(model);           snap("4 结束校准后")
```

### 5.3 需要观察的现象与预期结果

对照下表理解每个阶段模块身上的属性变化（预期，基于源码逻辑）：

| 阶段 | `quantization_scheme` | `weight_observer` | `weight_scale` | `quantization_status` |
|------|:--:|:--:|:--:|:--:|
| 0 裸模块 | ✗ | ✗ | ✗ | 无 |
| 1 挂方案后 | ✓ | ✗ | ✗ | 初始化/禁用 |
| 2 开始校准后 | ✓ | ✓ | ✗ | CALIBRATION |
| 3 取整后 | ✓ | ✓ | ✓ | CALIBRATION |
| 4 结束校准后 | ✓ | ✗（已删） | ✓ | FROZEN |

**待本地验证**：`quantization_status` 的确切枚举名、`weight_scale` 是否在第 3 步就出现，以你环境的 `compressed_tensors` 版本和 W4A16 预设细节为准。若第 1 步就因 group_size 整除性报错，把 `in_features` 调成 128 的整数倍（本例已是 128）。

### 5.4 进阶观察

- 把 scheme 换成带静态激活的方案（如 `W8A8`），重跑脚本，观察第 2 步会**额外**出现 `input_observer`，印证 4.3「按 scheme 决定挂哪些 observer」。
- 在第 2 步后用 `with __import__('llmcompressor.modifiers.utils.hooks', fromlist=['HooksMixin']).HooksMixin.disable_hooks():` 包一次前向，观察校准 hook 被临时禁用（不会累积统计），印证 `HooksMixin.register_hook` 的可禁用设计。
- 把 `linear` 的 `in_features` 改成非 `group_size` 整数倍（如 160，group_size=128），观察 `initialize_quantization` 立即抛整除性 `ValueError`，印证 4.5 的「早期失败」。

## 6. 本讲小结

- `QuantizationMixin(HooksMixin)` 是混入类，给 `QuantizationModifier`/`GPTQModifier`/`AWQModifier` 等提供通用的量化脚手架：字段定义、scheme 解析、以及 `initialize_quantization`/`start_calibration`/`end_calibration` 三个方法。
- **核心设计**：mixin 只负责「挂方案、挂/卸 observer 与 hook、开关量化、冻结」，**不自己更新 scale/zero-point**；取整由子类在 `on_sequential_epoch_end` 显式调用 `observe` + `update_qparams` 完成。这让 mixin 能同时服务于「直接取整」(RTN) 和「先变换后量化」(AWQ) 等不同算法。
- `initialize_quantization` 四步：reset 旧状态 → `apply_quantization_config` 挂 `quantization_scheme` → 校验 group_size 整除性 → `disable_quantization`（挂方案但关闭量化）。命中目标用 `resolved_targets` 而非 `targets`。
- `start_calibration` 按方案**选择性**挂 observer 与 calibration hook（动态激活不挂），权重 observer 强制降级为 memoryless 省内存，hook 经 `HooksMixin.register_hook` 注册以便临时禁用，最后切到 CALIBRATION。
- `end_calibration` 与 start 严格对称：卸 hook、`freeze_module_quantization` 删 observer 并置 FROZEN、`enable_quantization` 保持量化开启；observer 是一次性工具被删除，scale/zp 是成果被保留。
- group_size 整除性（GROUP/TENSOR_GROUP）在 `initialize_quantization` 阶段提前校验并报错，避免校准跑完才发现保存失败；可用 `bypass_divisibility_checks=True` 跳过。

## 7. 下一步学习建议

- 阅读 [u3-l3](u3-l3-calibration-observers-hooks.md) 深入 observer 内部：`observe`/`update_qparams` 背后 MinMax/MSE/IMatrix observer 如何收集统计、`fuse_weight_observers` 如何融合 Q/K/V 共享 global_scale。
- 想看「需要数据」的对照，进 [u4-l1](u4-l1-gptq-algorithm.md)（GPTQ），观察它如何在 mixin 挂好的脚手架上，用 Hessian 替代纯 min/max 来驱动 `observe` + `update_qparams`。
- 想理解变换类 modifier 如何与量化 modifier 组合，阅读 [u4-l2](u4-l2-awq-transform.md)（AWQ），体会「mixin 不自动取整」这一设计为何对 AWQ 至关重要。
- 想看这三个方法在真实管线里的触发时机，阅读 [u3-l5](u3-l5-sequential-pipeline.md)（SequentialPipeline），它会在子图边界触发 `CALIBRATION_START`/`SEQUENTIAL_EPOCH_END`/`CALIBRATION_END`。
