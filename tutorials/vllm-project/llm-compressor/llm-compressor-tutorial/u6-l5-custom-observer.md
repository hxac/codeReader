# 扩展点二：自定义 Observer

## 1. 本讲目标

本讲是 llm-compressor 的第二个扩展点。在第三单元（u3-l3）里，我们已经知道 observer 是「把张量变成统计量、再变成 scale/zero_point」的小齿轮。本讲换个视角：**当你认为内置 observer 不够用、想自己写一个时，应当实现什么接口、遵守什么契约、又如何把它接到 `QuantizationModifier` 上去用。**

学完本讲你应该能够：

1. 说清 `Observer` 基类的「两阶段契约」——`forward()` 收集统计、`get_qparams()` 算量化参数，并知道只需实现一个抽象方法 `update_statistics_from_observed`。
2. 区分三种 MinMax observer（memoryless / static / moving-average）在「跨 batch 如何合并统计」与「DDP 如何同步」上的差异。
3. 理解 MSE / IMatrix observer 为何不用绝对 min/max，而是用网格搜索裁掉离群值，以及 IMatrix 为何额外收集 `E[x²]` 做通道重要性加权。
4. 掌握把自定义 observer 通过 `weight_observer` / `input_observer` / `observer` 等字段挂到 `QuantizationModifier` 上的完整链路，并最终能自己实现一个百分位（percentile）observer。

## 2. 前置知识

在动手之前，用三句话回顾 u3-l3 建立的认知，本讲直接承接：

- **observer 是统计收集器，不是量化器。** 它把权重或激活张量压缩成 `min_vals`/`max_vals`（或别的统计），真正换算成 `scale`/`zero_point` 是委托给 `compressed_tensors` 的 `calculate_qparams` 完成的。
- **取整三连 `observe → update_qparams → freeze`。** 校准 hook 在每个 batch 调 `observer(value)` 累积激活统计；epoch 末调 `update_qparams` 把统计换成 scale/zero_point 写回模块；`freeze` 删掉 observer、冻结参数。
- **observer 靠注册表按名字取用。** `@Observer.register("minmax")` 这样的装饰器把类登记进 `RegistryMixin` 维护的注册表，后续靠字符串名字实例化，与 modifier 的工厂机制（u2-l4）异曲同工。

如果上述任一点你觉得陌生，建议先回到 u3-l3 再读一遍校准部分。本讲默认你已经知道「observer 被挂在模块的 `{base_name}_observer` 属性上」这件事。

此外需要一点量化数学直觉（u1-l1 已讲过，这里复述要点）：量化用一个缩放因子 scale 和零点偏移 zero_point 把连续值映射成低位整数，

\[
\text{scale} = \frac{\max - \min}{q_{\max} - q_{\min}},\qquad
q = \mathrm{round}\!\left(\frac{x}{\text{scale}}\right) + \text{zero\_point}
\]

其中 \(q_{\max}, q_{\min}\) 由目标位宽决定（如 int8 对称量化时 \(q_{\max}=127\)）。**量化的误差全部来自 round，而 round 的误差大小由 scale 决定，scale 又由 observer 喂进来的 `min/max` 决定。** 这就是 observer 为何关键：选不同的统计策略，本质是在「裁剪离群值让多数值更准」和「保留极端值不溢出」之间做权衡。理解了这一点，下面的 memoryless / static / MSE / IMatrix 之间的差异就都是同一个权衡的不同答案。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/llmcompressor/observers/base.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/base.py) | `Observer` 抽象基类：定义两阶段契约、`get_qparams`、DDP 同步、fusion 协作；所有自定义 observer 的父类。 |
| [src/llmcompressor/observers/min_max.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/min_max.py) | 三种 MinMax observer：`memoryless_minmax` / `static_minmax` / `minmax`，是最简单的「min/max 统计」参考样板。 |
| [src/llmcompressor/observers/mse.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/mse.py) | MSE observer：用网格搜索找使量化误差最小的 min/max，是「不只取极端值」的代表。 |
| [src/llmcompressor/observers/imatrix.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/imatrix.py) | IMatrix observer：在 MSE 基础上用 `E[x²]` 加权，是「用自定义统计 + 自定义 hook」的高级范例。 |
| [src/llmcompressor/observers/helpers.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/helpers.py) | `flatten_for_calibration`（按策略把张量整形成统计形状）、`ACTIVATION_OBS`、`lerp`、`fuse_weight_observers`。 |
| [src/llmcompressor/modifiers/quantization/calibration.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py) | `initialize_observer` / `observe` / `update_qparams`：observer 如何被实例化挂载、被喂张量、被换算。 |
| [src/llmcompressor/modifiers/quantization/quantization/mixin.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py) | `QuantizationMixin`：`weight_observer` 等字段与 `_apply_observer_overrides` 把 observer 名字写进 scheme。 |
| [docs/developer-tutorials/add-observer.md](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/developer-tutorials/add-observer.md) | 官方「添加新 observer」开发者教程，含 percentile 完整示例。 |

## 4. 核心概念与源码讲解

### 4.1 Observer 基类契约：两阶段、一个抽象方法、注册表登记

#### 4.1.1 概念说明

自定义 observer 的全部心智负担其实很小：基类 `Observer` 把「张量怎么按策略切块」「统计怎么换成 scale」「怎么跨 DDP 卡同步」「怎么和融合层共享 global_scale」这些脏活全包了，**子类只需要回答一个问题：给定一个已经切好块的张量，你要累积什么统计？**

`Observer` 用「两阶段」来组织这一切：

1. **Observe 阶段**：`forward(value)` 被反复调用（权重调一次，激活每个 batch 调一次）。它先把张量按 `QuantizationArgs.strategy`（group/channel/token/...）整形成 `(num_observations, *qparam_shape, group_size)`，再交给子类的 `update_statistics_from_observed` 去填 `self.min_vals` / `self.max_vals`。
2. **Compute 阶段**：校准结束时调一次 `get_qparams()`，基类拿你的 `min_vals`/`max_vals` 调 `calculate_qparams` 换成 `{scale, zero_point, global_scale}`，**然后把统计删掉**，所以 `get_qparams` 只能调一次、且必须在 `forward` 之后。

这条「收集 → 计算 → 销毁」的单向流水线就是基类契约的核心。如果你的统计恰好就是 `min_vals`/`max_vals`（绝大多数情况），你**只需实现 `update_statistics_from_observed` 一个方法**；如果你用了别的统计（均值/方差/直方图），才需要再覆写 `get_qparams` 和 `has_statistics`。

`Observer` 同时继承 `InternalModule`（让它能作为子模块挂到 nn.Module 上）和 `RegistryMixin`（来自 `compressed_tensors`，提供 `@Observer.register("名字")` 装饰器与 `load_from_registry` 实例化）。这与第三单元的 `CalibrationPipeline` 注册机制完全同构。

#### 4.1.2 核心流程

自定义 observer 的生命周期（从写代码到被 oneshot 用上）：

```
① 你写一个子类 MyObserver(Observer)，用 @Observer.register("my_obs") 登记
② QuantizationModifier(weight_observer="my_obs") → _apply_observer_overrides
   把名字写进 scheme.weights.observer
③ start_calibration → initialize_observer 读 args.observer
   → Observer.load_from_registry("my_obs", base_name=, args=) 实例化
   → module.register_module("weight_observer", obs); obs.attach(module)
④ 校准：observe(module) → obs(weight) → forward → update_statistics_from_observed
⑤ epoch 末：update_qparams(module) → obs.get_qparams() → calculate_qparams
   → scale/zero_point 写回模块 → delete_statistics
⑥ end_calibration → freeze_module_quantization → obs.detach(module) 删掉 obs
```

其中第 ① 步是你的工作，② 以后的每一步基类和框架都已替你铺好。

#### 4.1.3 源码精读

先看基类定义与构造，理解它持有哪些状态：

[src/llmcompressor/observers/base.py:L29-L58](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/base.py#L29-L58) — `Observer(InternalModule, RegistryMixin)`，构造时保存 `base_name`（`weight`/`input`/`output`/`q`/`k`/`v`）和 `args: QuantizationArgs`（含量化位宽、对称性、strategy 等），并把额外的 `**observer_kwargs` 合并进 `args.observer_kwargs`（这是自定义超参的官方存放点）。每个 observer 还自带一个 `fusion_handler`（见 4.3）。

两个类属性是子类常要碰的：
- `_stats_attrs`（默认 `["min_vals", "max_vals"]`）声明「哪些属性算我的统计」，`has_statistics` / `delete_statistics` / `get_statistics` 都靠它判断；如果你的 observer 用了别的统计属性，要一并加进来（IMatrix 就追加了 `_imatrix_sum`/`_imatrix_count`）。
- `_act_sync_dict`（默认空）声明「DDP 下哪些统计属性要 all-reduce、用什么算子」，4.2 会详细对比。

抽象方法只有一个：

[src/llmcompressor/observers/base.py:L68-L76](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/base.py#L68-L76) — `update_statistics_from_observed(observed)` 是唯一必须实现的方法。注释明确告诉我们 `observed` 已被整形成 `(num_observations, *qparam_shape, group_size)`，子类**不需要自己处理切块**。

再看「Compute 阶段」的 `get_qparams`，它解释了为何 `update_qparams` 必须紧跟 `observe`：

[src/llmcompressor/observers/base.py:L98-L123](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/base.py#L98-L123) — 先 `assert self.has_statistics`（没调过 forward 会断言失败），再用 `self.min_vals`/`self.max_vals` 调 `calculate_qparams` 算出 scale/zero_point（TENSOR_GROUP 还算 global_scale），最后 **`self.delete_statistics()`** 把统计删掉。这就是「统计一次性消费」的来源：第二次调 `get_qparams` 会因统计已删而断言失败。

最后看「Observe 阶段」的 `forward`，它包含两个对子类行为影响很大的短路：

[src/llmcompressor/observers/base.py:L125-L145](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/base.py#L125-L145) — `forward` 先丢空张量（L134-135），再判断 `if self.is_weight_obs and self.has_statistics: return self`（L137-138）。这条短路意味着**权重 observer 一旦有了统计就不再重新观测**——因为权重在校准期不变，重复观测没有意义。所以对权重而言，无论你把 observer 声明成 memoryless 还是 static，`update_statistics_from_observed` 实际只被调用一次。激活则不同，每个 batch 都会调一次，于是 memoryless / static / moving-average 才有了发挥空间（见 4.2）。整形成统计形状的工作由 `flatten_for_calibration` 完成：

[src/llmcompressor/observers/helpers.py:L28-L60](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/helpers.py#L28-L60) — 按 `base_name`（weight/input/output/q/k/v）与 `args.strategy`（TENSOR/CHANNEL/GROUP/TOKEN/BLOCK/ATTN_HEAD）把张量整形成统一的三段式形状 `(num_observations, *qparam_shape, group_size)`。子类拿到的张量形状是有保证的，这也是 `_get_min_max` 敢直接用 `dim=(0, -1)` 做规约的原因。

#### 4.1.4 代码实践

实践目标：用最小代码验证「实现 `update_statistics_from_observed` 一个方法就能让 observer 跑通 observe → get_qparams 全流程」，不依赖完整 oneshot。

操作步骤（示例代码）：

```python
# 示例代码：手工驱动一个内置 MinMax observer，观察两阶段契约
import torch
from compressed_tensors.quantization import QuantizationArgs, QuantizationStrategy
from llmcompressor.observers import Observer

# 用注册表按名字取出内置的 memoryless MinMax observer
ObsCls = Observer.load_from_registry("memoryless_minmax")  # 注册表实例化

args = QuantizationArgs(
    num_bits=8, type="int", symmetric=True,
    strategy=QuantizationStrategy.CHANNEL, group_size=None,
)
obs = ObsCls(base_name="weight", args=args)

# ① Observe 阶段：模拟 observe(module) → obs(weight)
weight = torch.randn(4, 8)          # 一个 4x8 权重
obs(weight)                         # 等价于 forward，触发 update_statistics_from_observed

print("has_statistics:", obs.has_statistics)   # True
print("min_vals.shape:", obs.min_vals.shape)   # (4,) —— CHANNEL 策略下每行一个 min

# ② Compute 阶段：模拟 update_qparams(module) → obs.get_qparams()
qparams = obs.get_qparams()
print("scale.shape:", qparams["scale"].shape, "zero_point.shape:", qparams["zero_point"].shape)

# ③ 验证「统计一次性消费」：再调一次会断言失败
try:
    obs.get_qparams()
except AssertionError as e:
    print("第二次调用 get_qparams 失败（符合预期）：", e)
```

需要观察的现象 / 预期结果：

1. 第一次 `obs(weight)` 后 `has_statistics` 为 `True`，`min_vals` 形状为 `(4,)`（CHANNEL 策略按权重的行数 / 即 `num_rows` 给出每行一个统计）。
2. `get_qparams()` 返回 `scale` 与 `zero_point`，形状与 `min_vals` 一致。
3. 第二次调用 `get_qparams()` 抛出 `AssertionError`，证明统计已被删除。

> 说明：本实践不连真实模型，仅验证基类契约。`Observer.load_from_registry` 这个类方法由 `RegistryMixin` 提供。如果运行环境未安装 torch，则「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果你的自定义 observer 用 `_mean` / `_std` 而不是 `min_vals` / `max_vals` 作统计，除了实现 `update_statistics_from_observed` 还需要改什么？

**答案**：需要覆写 `has_statistics`（基类默认检查 `min_vals`/`max_vals` 是否存在，见 base.py L60-62），否则 `get_qparams` 的 `assert self.has_statistics` 会在统计已存在时仍判 False；还要覆写 `get_qparams`，把 `_mean ± k·_std` 换成 `min_vals/max_vals` 再调 `calculate_qparams`。可参考 `add-observer.md` 的 `NormalObserver` 示例。

**练习 2**：为什么权重 observer 即使声明为 `static_minmax`（跨 batch 累积），实际效果仍是 memoryless？

**答案**：因为 `Observer.forward` 在 L137-138 有 `if self.is_weight_obs and self.has_statistics: return self` 短路，权重 observer 只观测一次就不再累积；同时 `initialize_observer` 还会把权重 observer 强制降级为 memoryless（见 4.4.3）。两者一致地保证权重统计只算一遍、省内存。

---

### 4.2 三种 MinMax：跨 batch 如何合并、DDP 如何同步

#### 4.2.1 概念说明

`min_max.py` 里有三个 observer，它们都喂 `min_vals`/`max_vals`，**唯一区别是多次观测之间如何合并**。这是理解 `_act_sync_dict` 的最佳入口——同一个「取 min/max」的数学，因为合并方式不同，在多卡下要用不同的 reduce 算子。

- **`memoryless_minmax`**：每次观测都**覆盖**之前的统计，只保留最后一个 batch 的 min/max。最省内存、对权重最合适。
- **`static_minmax`**：跨 batch 累积**全局**的 running min/max（取更小/更大），覆盖整个校准集。
- **`minmax`**：用**滑动平均（EMA）**把新 batch 的 min/max 以很小的权重融合进来，对最近的统计更敏感。

这三种策略对应三种不同的「我想让校准集的激活分布如何影响 scale」的取舍。

#### 4.2.2 核心流程

设单个 batch 经过 `flatten_for_calibration` 后为 `observed`，`_get_min_max` 沿「观测维 + 分组维」`dim=(0, -1)` 规约出每组的 `(min, max)`。三种 observer 在拿到本 batch 的 `(min, max)` 后：

```
memoryless:   self.min_vals, self.max_vals = (min, max)        # 直接覆盖
static:       self.min_vals = min(self.min_vals, min)          # 取更小
              self.max_vals = max(self.max_vals, max)          # 取更大
              （首次观测直接赋值）
moving-avg:   new = lerp(self.min_vals, min, c)                # EMA 融合
              c = averaging_constant (默认 0.01)
```

`lerp(start, end, w) = start·(1-w) + end·w`，即 EMA 公式

\[
\text{min}_t = (1 - c)\cdot \text{min}_{t-1} + c\cdot \text{min}_{\text{batch}}
\]

DDP 下，每个 rank 各自累积自己的统计，再由 `sync_activation_stats` 按 `_act_sync_dict` 声明的算子做 all-reduce。三者的算子不同：memoryless 不同步（每个 rank 算自己那份就够，因为它本来就不跨 batch 累积，但注意这意味着各 rank 激活分布不同会让结果不同——实践中 memoryless 多用于权重，而权重各卡相同）、static 用 `MIN`/`MAX`（running min/max 的全局汇总是取极值）、moving-average 用 `AVG`（平均各卡的 EMA）。

#### 4.2.3 源码精读

三个类的注册与合并逻辑全在一个文件里：

[src/llmcompressor/observers/min_max.py:L10-L19](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/min_max.py#L10-L19) — `MemorylessMinMaxObserver` 用 `@Observer.register("memoryless_minmax")` 登记，`_act_sync_dict = {}`（空，不同步），`update_statistics_from_observed` 直接覆盖。这是写自定义 observer 时「最简单的可运行样板」。

[src/llmcompressor/observers/min_max.py:L22-L41](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/min_max.py#L22-L41) — `StaticMinMaxObserver` 继承 memoryless，把 `_act_sync_dict` 设为 `{"min_vals": MIN, "max_vals": MAX}`，并用 `if hasattr(self, "min_vals")` 判断「是否已有旧统计」来决定是累积（取极值）还是首次赋值。这个 `hasattr` 分支正是 4.1.4 练习里「跨 batch 累积」的标准写法。

[src/llmcompressor/observers/min_max.py:L44-L67](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/min_max.py#L44-L67) — `MinMaxObserver`（moving average）在 `__init__` 里从 `observer_kwargs` 读 `averaging_constant`（默认 0.01），`_act_sync_dict` 用 `AVG`，并用 `lerp` 做融合。注意 `c != 1.0` 才融合，`c == 1.0` 时退化为 memoryless（直接取新值）。

[src/llmcompressor/observers/min_max.py:L70-L74](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/min_max.py#L70-L74) — 公用的 `_get_min_max`：`dim=(0, -1)` 同时压掉观测维（batch×token）和分组维（group_size），剩下 `(num_groups,)` 的 per-group min/max。这与 `flatten_for_calibration` 保证的形状契约配套。

`lerp` 的实现在 helpers：

[src/llmcompressor/observers/helpers.py:L218-L220](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/helpers.py#L218-L220) — 手写线性插值，注释说明 `torch.lerp` 并非所有 dtype 都有实现，故自实现。

#### 4.2.4 代码实践

实践目标：用同一组「多个 batch」的模拟激活，对比三种 MinMax observer 累积出的 `min_vals` 差异，直观体会三种合并策略。

操作步骤（示例代码）：

```python
# 示例代码：对比 memoryless / static / minmax 三种合并
import torch
from compressed_tensors.quantization import QuantizationArgs, QuantizationStrategy
from llmcompressor.observers import Observer

args = QuantizationArgs(
    num_bits=8, type="int", symmetric=True,
    strategy=QuantizationStrategy.TENSOR, group_size=None,
)

def run(name):
    ObsCls = Observer.load_from_registry(name)
    obs = ObsCls(base_name="input", args=args)
    # 模拟 5 个 batch，激活范围逐 batch 缩小
    for b in range(5):
        x = torch.randn(8, 16) * (1.0 - 0.1 * b)   # 越往后 batch 的值越集中
        obs(x)
    return obs.get_qparams()["scale"].item()

for n in ["memoryless_minmax", "static_minmax", "minmax"]:
    print(f"{n:20s} scale = {run(n):.4f}")
```

需要观察的现象 / 预期结果：

1. `static_minmax` 的 scale 最大——它累积了所有 batch 的全局极值，范围被最早的 batch 拉宽。
2. `memoryless_minmax` 的 scale 反映最后一个 batch（范围最集中）。
3. `minmax`（moving average）介于两者之间，但更偏向旧统计（`c=0.01` 让新 batch 影响很小）。

> 这些是趋势性预期，具体数值「待本地验证」。注意权重 observer 会被 `forward` 短路成只观测一次，所以这里用 `base_name="input"` 才能体现多次合并的差异。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `StaticMinMaxObserver` 的 `_act_sync_dict` 用 `MIN`/`MAX` 而不是 `AVG`？

**答案**：static 累积的是「全局 running min/max」，多卡汇总时应取所有卡中的真正极值——min 用 MIN、max 用 MAX。用 AVG 会得到各卡极值的平均，既不是全局极值也不是单卡极值，语义错误。

**练习 2**：把 `averaging_constant` 设成 1.0，`MinMaxObserver` 的行为会变成什么？

**答案**：看 min_max.py L62-64，`c != 1.0` 才走 `lerp` 融合；`c == 1.0` 时跳过融合，直接 `self.min_vals = min_vals`，即退化为 memoryless（只保留最后一个 batch）。

---

### 4.3 MSE 与 IMatrix：用网格搜索裁掉离群值，再用重要性加权

#### 4.3.1 概念说明

MinMax 的最大问题是「少数极端离群值会把量化范围撑得很大，让绝大多数正常值的精度变差」。MSE observer 用一个更聪明的策略：**不直接取 min/max，而是搜索一个被「收缩」过的范围，让量化误差（而非范围覆盖率）最小。**

具体地，它把原始 `[min, max]` 乘一个收缩因子 \(p \in [1-\text{maxshrink},\ 1]\) 得到候选范围 \([p\cdot\text{min},\ p\cdot\text{max}]\)，对每个候选范围做一次伪量化（`fake_quantize`）算误差，挑误差最小的那个 \(p\)。收缩范围意味着主动「截掉」两端的离群值——它们会被 clamp，但换来主体值更细的量化粒度，整体误差往往更低。

IMatrix（importance matrix）更进一步：**不同输入通道对输出的重要性不一样**，搜索误差时不该一视同仁。它在校准期额外收集每个输入通道的 \(E[x^2]\)（激活平方的期望）作为重要性权重，把误差写成重要性加权和，让「重要的通道」在搜索时拥有更大话语权。这就是 `imatrix_mse` 比普通 `mse` 在 4bit 等低比特场景常更准的原因。

这两个 observer 还示范了自定义 observer 的两个进阶能力：**从 `observer_kwargs` 读超参**（MSE 的 `maxshrink`/`grid`/`norm`/`patience`）与**自己挂 PyTorch hook 收集额外统计**（IMatrix 的 `attach`/`detach`）。

#### 4.3.2 核心流程

MSE 网格搜索的数学：

\[
p_i = 1 - \frac{i}{\text{grid}},\quad i = 0, 1, \dots, \lfloor \text{maxshrink}\cdot \text{grid} \rfloor
\]

对每个 \(p_i\)：用 \([p_i\cdot\text{min},\ p_i\cdot\text{max}]\) 算 scale/zero_point，再 `fake_quantize(observed)` 得到 \(q_i\)，误差

\[
\text{err}_i = \sum_{\text{groups}}\bigl(|q_i - \text{observed}|^{\text{norm}}\cdot w\bigr)
\]

（普通 MSE 权重 \(w=1\)；IMatrix 权重 \(w_j = E[x_j^2]\) 归一化后广播）。取使 \(\text{err}_i\) 最小的 \((p_i\cdot\text{min},\ p_i\cdot\text{max})\) 作为最终 `min_vals`/`max_vals`。`patience` 控制连续多少步无改进就提前停止，`chunk_size` 是 `torch.compile` 编译路径的批次大小。

IMatrix 的重要性统计靠 `attach` 时挂的 forward-pre hook 累积：

\[
\text{\_imatrix\_sum}_j \mathrel{+}= \sum_t x_{tj}^2,\qquad
\text{\_imatrix\_count} \mathrel{+}= n_{\text{tokens}},\qquad
s_j = E[x_j^2] = \frac{\text{\_imatrix\_sum}_j}{\text{\_imatrix\_count}}
\]

这套「自己挂 hook 收集统计」的能力是 4.1 基类契约里 `attach`/`detach` 钩子的真正用武之地。

#### 4.3.3 源码精读

MSE observer 的注册与超参读取：

[src/llmcompressor/observers/mse.py:L12-L48](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/mse.py#L12-L48) — `MemorylessMSEObserver`（注册名 `memoryless_mse`）。`__init__` 从 `self.args.observer_kwargs.get(...)` 读 `maxshrink`(0.20)/`patience`(5)/`grid`(100.0)/`norm`(2.4)/`chunk_size`(5) 等超参，并预先构造一份 `strategy=TOKEN` 的 `_token_args`（搜索时按 token 维伪量化）。`update_statistics_from_observed` 把网格搜索委托给 `_grid_search_mse`。`_act_sync_dict={}`，故 memoryless 不同步。`mse.py` 还有一个 `MovingAverageMSEObserver`（注册名 `mse`，L51-98），在 memoryless 基础上加 `lerp` 平滑、`_act_sync_dict` 用 AVG，结构与 4.2 的 MinMax/MinMax 关系完全对应。

网格搜索本体在 `mse_quant.py`：

[src/llmcompressor/observers/mse_quant.py:L48-L85](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/mse_quant.py#L48-L85) — `_grid_search_mse` 先算 `min_val=torch.amin(observed, dim=(0,-1))`、`max_val=torch.amax(...)` 作为搜索起点，`total_steps = int(maxshrink * grid)` 决定收缩步数，再按 `active_session().state.enable_compile` 选择 eager 或 `torch.compile` 编译路径。imatrix.py 里有一份功能更全的 `_grid_search`（支持 `importance_weights`），可对照阅读：

[src/llmcompressor/observers/imatrix.py:L253-L319](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/imatrix.py#L253-L319) — IMatrix 的 `_grid_search`：循环 `i in range(shrink_steps+1)`，每个 `p` 算 `shrink_min/shrink_max`，调 `calculate_qparams` + `fake_quantize` 得 `q`，误差 `q.sub_(observed_f).abs_().pow_(norm)`，若 `importance_weights is not None` 再 `q.mul_(importance_weights)`，最后 `err = q.sum(dim=(0,-1))` 取最优。`improved = err < best_error` 更新 `best_min/best_max`，`no_improve >= patience` 提前 break。这就是「裁掉离群值」与「按通道重要性加权」的真正实现。

IMatrix 的 hook 生命周期示范了 `attach`/`detach`：

[src/llmcompressor/observers/imatrix.py:L24-L42](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/imatrix.py#L24-L42) — `IMatrixMSEObserver`（注册名 `imatrix_mse`）。关键两处：`_act_sync_dict` 同步的是 `_imatrix_sum`(SUM) 与 `_imatrix_count`(SUM) 而非 min/max；`_stats_attrs` 追加了 `_imatrix_sum`/`_imatrix_count`，让基类的 `has_statistics`/`delete_statistics` 也管它们。

[src/llmcompressor/observers/imatrix.py:L74-L121](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/imatrix.py#L74-L121) — `attach` 在模块上挂 `register_forward_pre_hook`，每次前向累积 `x.pow(2).sum(...)` 到 `_imatrix_sum`、token 数到 `_imatrix_count`；`detach` 卸 hook。注意它还检查 `HooksMixin._HOOKS_DISABLED`（u3-l3 讲过的全局开关），与 sequential 管线的 hook 临时禁用协作。这段是「自定义 observer 自己收额外统计」的完整范例。

#### 4.3.4 代码实践

实践目标：在同一组带离群值的模拟激活上，对比 `memoryless_minmax` 与 `memoryless_mse` 得到的 scale，体会 MSE 裁掉离群值的效果。

操作步骤（示例代码）：

```python
# 示例代码：对比 MinMax 与 MSE 在有离群值时的 scale
import torch
from compressed_tensors.quantization import QuantizationArgs, QuantizationStrategy
from llmcompressor.observers import Observer

args = QuantizationArgs(
    num_bits=8, type="int", symmetric=True,
    strategy=QuantizationStrategy.TENSOR, group_size=None,
)

# 构造 1000 个正常值 + 几个超大离群值
x = torch.randn(1000, 16)
x[:3] *= 20.0          # 制造离群值

def scale_of(name):
    ObsCls = Observer.load_from_registry(name)
    obs = ObsCls(base_name="input", args=args)
    obs(x)
    return obs.get_qparams()["scale"].item()

print("memoryless_minmax scale:", scale_of("memoryless_minmax"))
print("memoryless_mse     scale:", scale_of("memoryless_mse"))
```

需要观察的现象 / 预期结果：

1. `memoryless_minmax` 的 scale 被 3 个离群值撑大（接近 `20/max_normal` 量级）。
2. `memoryless_mse` 的 scale 明显更小——它搜索到一个收缩范围，把离群值 clamp 掉以换取主体值的更低误差。

> 趋势预期，具体数值取决于随机种子，「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`maxshrink=0` 时，MSE observer 退化成什么？

**答案**：`total_steps = int(maxshrink * grid) = 0`，循环只跑 `i=0` 一步，`p = 1 - 0/grid = 1.0`，即候选范围就是原始 `[min, max]`，等同于 `memoryless_minmax`。`maxshrink` 越大，搜索越激进地尝试收缩范围。

**练习 2**：IMatrix 为什么把 `_imatrix_sum`/`_imatrix_count` 加进 `_stats_attrs`？

**答案**：基类的 `has_statistics`、`delete_statistics`、`get_statistics` 都遍历 `_stats_attrs`。把它们加进去，才能保证：(a) 在校准还没累积到 importance 时 `has_statistics` 能正确反映状态；(b) `get_qparams` 删除统计时一并清掉这两个中间量；(c) 融合/同步逻辑能识别它们。

---

### 4.4 把自定义 observer 挂到 QuantizationModifier 上

#### 4.4.1 概念说明

写好并 `@Observer.register("my_obs")` 之后，怎么让它在一次真实量化里生效？答案不在 observer 侧，而在 `QuantizationMixin` 提供的四个字段：`weight_observer`、`input_observer`、`output_observer`，以及一个字典形式的总开关 `observer`。它们的作用都是**把你给的名字写进 `QuantizationArgs.observer` 字段**，后续 `initialize_observer` 会按这个名字从注册表实例化 observer。

换句话说，observer 的「名字」是连接你的自定义代码与整套量化引擎的唯一纽带：recipe 里写一个字符串，引擎运行时用 `load_from_registry(字符串)` 取出你的类。这与 modifier 用类名（u2-l4）、pipeline 用注册名（u3-l4）是同一套「字符串 → 类」的注册表思想。

#### 4.4.2 核心流程

名字从 recipe 流到模块属性的全链路：

```
QuantizationModifier(
    scheme={...},                         # 或 config_groups
    weight_observer="my_obs",             # ① 用户指定 observer 名字
)
  ↓ resolve_quantization_config + _apply_observer_overrides
scheme.weights.observer = "my_obs"        # ② 名字写进 QuantizationArgs
  ↓ apply_quantization_config(model, config)
module.quantization_scheme.weights.observer = "my_obs"   # ③ scheme 挂到每个目标模块
  ↓ start_calibration → initialize_observer(module, "weight")
ObsCls = Observer.load_from_registry(args.observer, ...)  # ④ 按名字取类
obs = ObsCls(base_name="weight", args=args)
module.register_module("weight_observer", obs); obs.attach(module)  # ⑤ 挂载
  ↓ observe → update_qparams → end_calibration(freeze)   # ⑥ 复用 4.1 的两阶段契约
```

注意第 ② 步的关键设计：用户给的是 modifier 级别的「友好字段」（`weight_observer`），但真正驱动引擎的是 `QuantizationArgs.observer`——`_apply_observer_overrides` 负责把前者翻译成后者。所以你也可以完全绕开 modifier 字段，直接在 `scheme` 的 `QuantizationArgs` 里写 `observer="my_obs"`，效果一样（见 4.4.4 的两种等价写法）。

#### 4.4.3 源码精读

四个 observer 字段的声明：

[src/llmcompressor/modifiers/quantization/quantization/mixin.py:L134-L139](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L134-L139) — `weight_observer` / `input_observer` / `output_observer`（单个字符串）与 `observer`（字典，键为 `weights`/`input`/`output`）。注释里列出了内置合法名字。

`observer` 字典格式有专门校验：

[src/llmcompressor/modifiers/quantization/quantization/mixin.py:L170-L190](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L170-L190) — `validate_observer` 限定字典键只能是 `weights`/`input`/`output`，且值必须是字符串。拼错键在这里就会被拦下。

字段 → `QuantizationArgs.observer` 的翻译逻辑：

[src/llmcompressor/modifiers/quantization/quantization/mixin.py:L386-L434](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L386-L434) — `_apply_observer_overrides`。三个要点：(a) 同时给「单个字段」和「字典」会抛 `ValueError`（L397-407），二者互斥；(b) 字典优先级高于单个字段（L414-418）；(c) 把名字通过 `model_dump()` → 改 `observer` → `QuantizationArgs.model_validate` 写回 scheme（L427-432）。这正是上面流程图第 ② 步的实现。

按名字实例化并挂载：

[src/llmcompressor/modifiers/quantization/calibration.py:L36-L83](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py#L36-L83) — `initialize_observer`：从 `module.quantization_scheme.{weights|input_activations|output_activations}` 读出 `args.observer`（L59-62），调 `Observer.load_from_registry(observer, base_name=, args=)` 实例化，再 `module.register_module(f"{base_name}_observer", observer)` 挂上去，并调 `observer.attach(module)`（L80-83）。注意 L64-78 的**权重强制降级**：训练已不再支持，权重 observer 若是 `static_minmax`/`minmax` 会被改成 `memoryless_minmax`，`mse` 被改成 `memoryless_mse`（并 `log_once` 告警），以省内存。这与 4.1.3 的 `forward` 短路一起，把权重 observer 锁定为「只算一遍」。

`observe` / `update_qparams` 如何驱动挂好的 observer：

[src/llmcompressor/modifiers/quantization/calibration.py:L86-L106](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py#L86-L106) — `observe` 取 `module.{base_name}_observer`，喂 `module.{base_name}`（对 `base_name="weight"` 即喂 `module.weight`），等价于 `observer(value)` → `forward`。

[src/llmcompressor/modifiers/quantization/calibration.py:L109-L162](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py#L109-L162) — `update_qparams` 取 observer，若 `has_statistics` 为 False 会告警（典型的「校准没喂到数据」症状），否则调 `observer.get_qparams()` 拿 `{scale, zero_point, global_scale}` 写回 `module.weight_scale` 等参数。动态量化的 scale/zero_point 不写（L155-156）。

最后 `freeze` 卸载 observer：

[src/llmcompressor/modifiers/quantization/calibration.py:L207-L231](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py#L207-L231) — `freeze_module_quantization` 遍历 `input/weight/output/q/k/v`，对每个 `{name}_observer` 调 `observer.detach(module)` 卸 hook，再 `delattr` 删掉 observer 属性，置状态为 FROZEN。

#### 4.4.4 代码实践

实践目标：实现一个真实的自定义 observer（基于百分位裁剪），登记注册，然后通过 `QuantizationModifier` 跑通一次真实量化，对比它与默认 MinMax 的 scale 差异。

操作步骤：

1. **实现并登记 percentile observer**（改编自 `docs/developer-tutorials/add-observer.md` 的官方示例）：

```python
# 示例代码：自定义百分位 observer（登记后即可被 recipe 用名字引用）
import torch
from llmcompressor.observers import Observer

@Observer.register("percentile")          # 关键：用名字登记进注册表
class PercentileObserver(Observer):
    """
    用配置的百分位范围（而非绝对 min/max）设置 min_vals/max_vals，
    裁掉极端离群值以缩小量化范围。通过 observer_kwargs 的 percentile(默认99.9)配置。
    """
    _act_sync_dict = {}                   # memoryless：不跨 batch 累积，无需 DDP 同步

    def update_statistics_from_observed(self, observed: torch.Tensor) -> None:
        percentile = self.args.observer_kwargs.get("percentile", 99.9)
        lower = 100.0 - percentile
        upper = percentile
        # observed 形状为 (num_observations, *qparam_shape, group_size)
        self.min_vals = torch.quantile(observed, lower / 100.0, dim=(0, -1))
        self.max_vals = torch.quantile(observed, upper / 100.0, dim=(0, -1))
```

2. **把它挂到 `QuantizationModifier` 上**（两种等价写法）：

```python
from compressed_tensors.quantization import QuantizationArgs
from llmcompressor.modifiers.quantization import QuantizationModifier

# 写法 A：用 QuantizationArgs.observer 直接指定
recipe_A = QuantizationModifier(
    targets="Linear",
    scheme={
        "weights": QuantizationArgs(
            num_bits=8, type="int", symmetric=True,
            strategy="channel",
            observer="percentile",                       # 自定义 observer 名字
            observer_kwargs={"percentile": 99.5},
        )
    },
    ignore=["lm_head"],
)

# 写法 B：用 modifier 级别的 weight_observer 字段（_apply_observer_overrides 翻译）
recipe_B = QuantizationModifier(
    targets="Linear",
    scheme="W8A8",                # 预设方案
    weight_observer="percentile", # 名字会被写进 scheme.weights.observer
    ignore=["lm_head"],
)
```

3. **跑一次真实量化并对比 scale**：

```python
from transformers import AutoModelForCausalLM
from llmcompressor import oneshot

model = AutoModelForCausalLM.from_pretrained("HF上的某个小模型")  # 待本地验证具体模型名

# 用默认 observer 量化
oneshot(model=model, recipe=QuantizationModifier(targets="Linear", scheme="W8A8",
                                                 ignore=["lm_head"]),
        output_dir="out_default")
# 用 percentile observer 量化
oneshot(model=model, recipe=recipe_A, output_dir="out_percentile")
```

4. 在 `out_default` 与 `out_percentile` 的 safetensors 里找到某个 `Linear` 的 `weight_scale`，比较数值。

需要观察的现象 / 预期结果：

1. percentile observer 得到的 `weight_scale` 普遍**更小**——因为它裁掉了权重分布尾部的离群值，量化范围被收缩。
2. `out_percentile/config.json` 的 `quantization_config` 里能看到 `observer: "percentile"`（如果该字段被序列化），证明名字确实流进了最终产物。
3. 写法 A 与写法 B 等价：只要 `percentile` observer 类在 `oneshot` 调用前已被 import（从而 `@Observer.register` 生效），两种写法都能跑通。

> 注意：`percentile` observer 必须在调用 `oneshot` **之前**被 import，注册表才会登记它（与 modifier 工厂的懒发现不同，observer 注册靠 import 副作用）。`torch.quantile` 在 `dim=(0,-1)` 上对大张量可能较慢，正式使用建议先在权重上验证。模型名与具体数值「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：如果你同时传了 `weight_observer="percentile"` 和 `observer={"weights": "mse"}`，会发生什么？

**答案**：`_apply_observer_overrides` 在 mixin.py L397-407 检测到「单个字段」与「字典」同时存在，直接抛 `ValueError("Cannot specify both ...")`。二者只能二选一；若都给了字典优先。

**练习 2**：为什么自定义 observer 类必须在 `oneshot` 之前被 import？

**答案**：`@Observer.register("percentile")` 是一个装饰器，它的登记副作用发生在 Python import 该类之时。`oneshot` → `initialize_observer` → `Observer.load_from_registry("percentile")` 只能取到「已经登记过」的名字；若类未被 import，注册表里没有 `"percentile"`，会找不到。这与 modifier 的 `ModifierFactory` 运行时遍历子包自动发现的机制不同（u2-l4），observer 靠的是显式 import 的副作用。

**练习 3**：如何在不改 recipe 的前提下，临时把某个 observer 换成自定义的？

**答案**：因为 observer 名字最终存在 `module.quantization_scheme.{weights}.observer`，你也可以直接构造 `QuantizationArgs(observer="percentile", ...)` 放进 `config_groups`，或用 `weight_observer` 字段覆盖——它们都走 `_apply_observer_overrides` 同一条翻译路径，无需改其它代码。

## 5. 综合实践

把本讲四个模块串起来，完成一个「从零写 observer 到端到端量化对比」的闭环任务：

**任务**：为权重量化实现一个 `MADObserver`（基于中位数绝对偏差的 robust observer），并验证它对带离群值的权重比 MinMax 更稳健。

要求：

1. **实现**：继承 `Observer`，用 `@Observer.register("mad")` 登记；`_act_sync_dict={}`；在 `update_statistics_from_observed` 里计算 `median` 与 `mad = median(|x - median|)`，把 `min_vals = median - k·mad`、`max_vals = median + k·mad`（`k` 从 `observer_kwargs` 读，默认 3.0）。这样能复用基类 `get_qparams`，无需自己写。
2. **单元验证**：用 4.1.4 的「手工驱动」方式，构造一个带离群值的权重，分别用 `memoryless_minmax` 与 `mad` 跑 `obs(weight)` + `get_qparams()`，打印两者的 `scale`，确认 `mad` 的 scale 不被离群值撑大。
3. **端到端验证**：用 4.4.4 的写法 B，把 `weight_observer="mad"` 挂到一个 `QuantizationModifier`，对一个极小模型跑 `oneshot`，确认它能正常完成量化并保存（看 `config.json` 是否生成）。
4. **反思**：思考 `MADObserver` 用于激活（`base_name="input"`）时，`forward` 不会被短路，每个 batch 都会重算 median/mad——这与 memoryless 行为一致。若你想要跨 batch 累积的 robust 统计，需要参考 4.2 的 `hasattr` 分支写累积逻辑，并相应设置 `_act_sync_dict`（median/mad 没有简单的 reduce 算子，这是 robust observer 在 DDP 下的固有难点）。

> 这是一个开放任务，无标准答案，重点是走通「实现 → 登记 → 挂载 → 验证」四步。若环境无 GPU/小模型，可只做第 1、2 步的源码级验证，第 3 步「待本地验证」。

## 6. 本讲小结

- **自定义 observer 的最小契约只有一个方法**：实现 `update_statistics_from_observed`，用 `@Observer.register("名字")` 登记；只要统计是 `min_vals`/`max_vals`，基类 `get_qparams` 会自动调 `calculate_qparams` 换算 scale/zero_point。
- **基类包办了脏活**：`forward` 按 strategy 切块、`get_qparams` 换算并删统计、`sync_activation_stats` 跨 DDP 同步、`FusionHandler` 协作算 global_scale；子类只决定「累积什么统计」。
- **三种 MinMax 的差异只在合并与同步**：memoryless 覆盖不同步、static 累积用 MIN/MAX、moving-average 用 `lerp`+AVG；权重 observer 还被 `forward` 短路 + `initialize_observer` 降级，恒为 memoryless。
- **MSE/IMatrix 是「不取极端值」的代表**：用网格搜索收缩范围裁掉离群值，IMatrix 额外用 `attach` 挂 hook 收集 \(E[x^2]\) 做通道重要性加权，示范了 `observer_kwargs` 超参与自定义 hook 两个进阶能力。
- **observer 名字是唯一纽带**：`weight_observer`/`input_observer`/`observer` 字段经 `_apply_observer_overrides` 写进 `QuantizationArgs.observer`，`initialize_observer` 再用 `load_from_registry` 按名字实例化挂载——所以 observer 类必须在 `oneshot` 前 import。
- **两阶段契约不可违反**：`observe → update_qparams → freeze` 单向流转，`get_qparams` 会删统计所以只能调一次；权重只观测一次、激活每 batch 观测一次。

## 7. 下一步学习建议

本讲补齐了「自定义 observer」这一扩展点，建议接下来：

1. **回到量化算法**：带着本讲的 observer 视角重读 u4-l1（GPTQ）与 u4-l4（AutoRound），你会更清楚它们的 Hessian/重建损失与 observer 的统计收集是同一类「为 scale 选择服务」的不同策略。
2. **自定义 Modifier（u6-l4）**：把本讲的 `QuantizationMixin` 双继承与这里的 observer 字段结合起来，尝试写一个「自带定制 observer 的自定义量化 modifier」，这是两个扩展点的交汇处。
3. **DDP 与大模型（u6-l1 / u6-l2）**：本讲反复提到 `_act_sync_dict` 与 `sync_activation_stats`，读这两讲能看清 observer 统计在多卡、逐层 offload 下的完整同步与内存行为。
4. **阅读源码**：重点对比 `min_max.py`（最简样板）、`mse.py`（自定义超参）、`imatrix.py`（自定义 hook + 自定义统计）三者的递进关系，这是实现任何新 observer 时最好的三档参考。
