# 校准、Observers 与 Hooks

## 1. 本讲目标

上一讲（u3-l2）我们读完了 `QuantizationMixin` 这个「脚手架」，得出了一个关键结论：**mixin 只负责挂卸装备（scheme / observer / hook），它自己不算 scale 和 zero-point**；真正把权重「取整」（round-to-nearest）的动作，是由子类在 `on_sequential_epoch_end` 钩子里显式调用 `observe(...)` + `update_qparams(...)` 完成的。

本讲就钻进这两个函数背后，回答三个问题：

1. `observe` 到底把张量变成了什么？`update_qparams` 又是怎么把统计变成 `scale` / `zero_point` 写回模块的？
2. `MinMaxObserver` / `MSEObserver` / `IMatrixMSEObserver` 这些 observer 在「收集统计」这件事上到底差在哪里？
3. 校准 hook 如何在前向传播中把激活「喂」给 observer？多卡（DDP）下统计如何同步？`NVFP4` 这种 `TENSOR_GROUP` 方案又如何让 Q/K/V、gate/up 共享同一个 `global_scale`？

学完后你应当能：看懂一条校准数据的张量从进入模块到最终变成模块上 `weight_scale` 属性的完整链路；能够根据场景选择合适的 observer；并理解 `ACTIVATION_OBS` / `WEIGHT_OBS` 这两套命名约定。

## 2. 前置知识

- **量化数学直觉**（来自 u1-l1）：量化用一个缩放因子 \(s\)（scale）和零点偏移 \(z\)（zero-point）把高精度浮点值映射为低位整数。对称量化的核心公式为：

  \[ x_q = \mathrm{round}(x / s) + z,\qquad s = \frac{\max(|x_{\min}|, |x_{\max}|)}{q_{\max}} \]

  其中 \(q_{\max}\) 由目标位宽决定（如 int8 为 127）。误差全部来自 `round` 这一步。所以「选 scale」本质上是在「少数大值造成的截断」和「多数小值造成的舍入」之间做权衡——这正是 MSE observer 的出发点。

- **量化策略（QuantizationStrategy）**：决定 scale 的粒度。`TENSOR`（整层一个 scale）、`CHANNEL`（每个输出通道一个）、`GROUP`（每 group_size 列一个）、`TENSOR_GROUP`（一组张量共享一个 `global_scale`，如 NVFP4）、`TOKEN`（逐 token 动态，用于激活动态量化）、`BLOCK`（二维块）。

- **compressed-tensors 的角色**：llmcompressor 的 observer 把张量统计算出来后，真正「统计 → scale/zero-point」的数学换算委托给 `compressed_tensors.quantization.utils.calculate_qparams`，模块上的量化状态机（`QuantizationStatus`：INITIALIZED → CALIBRATION → FROZEN）也来自 compressed-tensors。本讲的 observer 是「统计收集器」，`calculate_qparams` 才是「换算器」。

- **base_name 命名约定**：每个被量化的模块上，量化相关属性都按 `base_name` 前缀组织。`weight` 表示权重，`input`/`output` 表示输入/输出激活，`q`/`k`/`v` 表示 attention 的 query/key/value（用于 KV cache 量化）。于是有 `weight_observer`、`weight_scale`、`input_observer`、`output_scale` 等一组同构属性。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/llmcompressor/observers/base.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/base.py) | `Observer` 抽象基类：定义「收集统计 → 换算 qparams → 同步/删除统计」的统一契约，所有 observer 都继承它。 |
| [src/llmcompressor/observers/min_max.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/min_max.py) | MinMax 家族三个 observer：`memoryless` / `static` / 移动平均（moving average）。 |
| [src/llmcompressor/observers/mse.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/mse.py) | MSE observer：用网格搜索寻找使量化误差最小的裁剪范围。 |
| [src/llmcompressor/observers/imatrix.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/imatrix.py) | `IMatrixMSEObserver`：用校准激活的 \(E[x^2]\) 作为重要性权重，做加权 MSE 搜索。 |
| [src/llmcompressor/observers/fusion.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/fusion.py) | `FusionHandler`：管理「融合组」内多个 observer 共享 `global_scale` 的簿记。 |
| [src/llmcompressor/observers/helpers.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/helpers.py) | `flatten_for_calibration`（按策略重塑张量）、`ACTIVATION_OBS` 常量、`fuse_weight_observers`（扫描融合层）、`lerp`。 |
| [src/llmcompressor/modifiers/quantization/calibration.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py) | 取整三连：`observe` / `update_qparams` / `freeze_module_quantization`，以及一组校准 hook 函数。 |
| [src/llmcompressor/modifiers/utils/hooks.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/utils/hooks.py) | `HooksMixin`：hook 的注册 / 临时禁用 / 卸载基础设施。 |

## 4. 核心概念与源码讲解

### 4.1 Observer 抽象基类：统计收集器的统一契约

#### 4.1.1 概念说明

`Observer` 是所有 observer 的抽象基类。它的定位非常克制：**只负责「把看到的张量变成统计量」，不负责把统计量换算成 scale/zero-point**。换算交给 compressed-tensors 的 `calculate_qparams`。

这样设计的好处是：统计策略（怎么算 min/max、要不要做 MSE 搜索）和换算数学（min/max → scale 公式、对称/非对称、有符号/无符号）彻底解耦。你换一个 observer，只是换了「统计收集方式」，换算逻辑一行都不用动。

`Observer` 同时继承了两个身份（见 [base.py:L29](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/base.py#L29)）：
- `InternalModule`（来自 compressed-tensors）：让它本身是一个 `nn.Module`，可以被 `register_module` 挂到目标模块上成为子模块；
- `RegistryMixin`：提供 `@Observer.register("minmax")` 装饰器注册 + `Observer.load_from_registry(name, ...)` 按名字加载的工厂机制，和上一单元的 `ModifierFactory` 思路一致。

#### 4.1.2 核心流程

一个 observer 的生命周期可以画成下面这条流水线：

```
observer(value)                     # 入口：喂入一个张量
   │  即 base.py 里的 forward()
   ▼
flatten_for_calibration(value)      # 按策略把张量重塑为 (num_obs, *qparam_shape, group_size)
   ▼
update_statistics_from_observed()   # 抽象方法：子类实现，写入 self.min_vals / self.max_vals
   ▼
fusion_handler.get_fused_statistics()  # 若处于融合组，顺便触发同伴 observer
   ─────── (统计收集阶段结束，可被反复调用累积) ───────
   ▼
observer.get_qparams()              # 换算阶段
   │  → get_global_scale()          #   仅 TENSOR_GROUP 算 global_scale
   │  → calculate_qparams(min_vals, max_vals, args, global_scale)   # compressed-tensors 换算
   │  → delete_statistics()         #   统计被「消费」后删除
   ▼
返回 {"scale", "zero_point", "global_scale"}
```

两个关键属性：

- `_stats_attrs`（[base.py:L43](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/base.py#L43)）：声明这个 observer 的统计字段名，默认 `["min_vals", "max_vals"]`。`has_statistics` 属性就是检查这些字段是否都存在（[base.py:L60-L62](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/base.py#L60-L62)）。IMatrix observer 会扩展它（见 4.2.3）。
- `_act_sync_dict`（[base.py:L40](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/base.py#L40)）：DDP 同步表，声明哪些统计字段、用哪种 reduce 操作跨卡合并（见 4.5.2）。

#### 4.1.3 源码精读

先看统计收集的入口 `forward`（[base.py:L125-L145](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/base.py#L125-L145)）。注意调用 observer 时写的是 `observer(value)`，这是因为 `nn.Module.__call__` 会转发到 `forward`：

```python
@torch.no_grad
def forward(self, observed):
    if observed.numel() == 0:
        return self
    if self.is_weight_obs and self.has_statistics:
        return self                       # 权重只观测一次：已有统计就跳过
    observed = flatten_for_calibration(observed, self.base_name, self.args)
    self.update_statistics_from_observed(observed)   # 子类实现的统计逻辑
    self.fusion_handler.get_fused_statistics()        # 融合组协同
    return self
```

这里有个重要语义：**权重 observer 只观测一次**（`is_weight_obs and self.has_statistics` 直接 return）。因为权重是常量，反复观测没有意义；而激活每次前向都不同，需要跨 batch 累积。

再看换算阶段 `get_qparams`（[base.py:L98-L123](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/base.py#L98-L123)）：

```python
@torch.no_grad
def get_qparams(self) -> QParamsDict:
    assert self.has_statistics, "No statistics available. Call observer(value) first."
    global_scale = self.get_global_scale()
    scale, zero_point = calculate_qparams(
        min_vals=self.min_vals, max_vals=self.max_vals,
        quantization_args=self.args, global_scale=global_scale,
    )
    self.delete_statistics()              # 统计被消费，立即删除
    return {"scale": scale, "zero_point": zero_point, "global_scale": global_scale}
```

两个要点：
1. **换算委托**：`calculate_qparams` 来自 `compressed_tensors.quantization.utils`，它接收 min/max 统计和 `QuantizationArgs`（含位数、对称性、策略），输出 scale/zero-point。observer 自己不做这套数学。
2. **统计是一次性的**：`get_qparams` 末尾调用 `delete_statistics()` 删掉 `min_vals`/`max_vals`。这就是为什么 `update_qparams` 必须紧跟在 `observe` 之后——一旦换算完，统计就没了，再调 `get_qparams` 会触发开头的 `assert self.has_statistics` 报错。

#### 4.1.4 代码实践

**目标**：体会 observer 是「统计收集器」这一身份，以及「统计一次性消费」的语义。

**步骤**：

1. 阅读 `tests/llmcompressor/modifiers/calibration/test_observers.py` 的 `test_observer_min_max_vals`，它示范了如何用 `Observer.load_from_registry` 构造 observer、逐次喂值、读取 `min_vals`/`max_vals`（[test_observers.py:L106-L123](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/modifiers/calibration/test_observers.py#L106-L123)）。
2. 在 Python 里运行下面这段（示例代码，非项目原有）：

```python
import torch
from compressed_tensors.quantization import QuantizationArgs
from llmcompressor.observers import Observer

obs = Observer.load_from_registry(
    "static_minmax", base_name="input", args=QuantizationArgs(strategy="tensor")
)
obs(torch.tensor([[0.0, 0.0], [-3.0, 1.0]]))   # 第一次观测
print("第一次 min/max:", obs.min_vals.item(), obs.max_vals.item())
obs(torch.tensor([[-1.0, 3.0]]))               # 第二次观测，应累积
print("第二次 min/max:", obs.min_vals.item(), obs.max_vals.item())
qp = obs.get_qparams()
print("scale / zp:", qp["scale"], qp["zero_point"])
# 此时统计已被删除，下面这行会触发 assert
try:
    obs.get_qparams()
except AssertionError as e:
    print("预期内的报错:", e)
```

**需要观察的现象**：第二次的 `min_vals` 应为 `-3.0`（取两次的最小）、`max_vals` 应为 `3.0`（取两次的最大），印证 `static_minmax` 是跨观测的 running min/max；第二次 `get_qparams` 会因统计已删除而 assert 失败。

**预期结果**：理解 observer 「观测可累积、换算即销毁」的内存模型。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `forward` 里对权重 observer 加了 `if self.is_weight_obs and self.has_statistics: return self` 的守卫，而对激活 observer 不加？

**答案**：权重是常量，多次观测得到的统计完全相同，重复累积只是浪费算力与内存（尤其大模型权重很大）；激活每次前向都不同，必须跨 batch 累积才能反映真实分布，所以不能跳过。

**练习 2**：如果一个模块只调用了 `update_qparams` 却没先调 `observe`，会发生什么？

**答案**：`update_qparams` 内部会检查 `observer.has_statistics`，若无统计会打印一条 warning（提示「校准设置可能有问题，比如没开 `moe_calibrate_all_experts` 或选错了管线」）并直接 return，不会写 scale（见 [calibration.py:L139-L145](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py#L139-L145)）。这正是排查「量化没生效」时的第一线索。

---

### 4.2 统计策略：MinMax 家族与 MSE / IMatrix

本节回答核心问题：**不同 observer 在 `update_statistics_from_observed` 这一步到底干了什么不同的事？** 所有 observer 最终都产出 `min_vals` / `max_vals`，差异全在「这两个值怎么算」。

#### 4.2.1 概念说明：MinMax 家族

MinMax 家族有三个成员，区别在于「如何把多次观测合并」：

| observer | 注册名 | 多次观测的合并方式 | 典型用途 |
| --- | --- | --- | --- |
| `MemorylessMinMaxObserver` | `memoryless_minmax` | **只保留最后一次**观测的 min/max | 权重（只观测一次，等价于单次 min/max） |
| `StaticMinMaxObserver` | `static_minmax` | 跨所有观测取 **running min/max** | 静态激活量化（跨校准 batch 累积极值） |
| `MinMaxObserver` | `minmax` | **移动平均（EMA）**，受 `averaging_constant` 控制 | 训练式压缩中的激活（PTQ 中较少用） |

#### 4.2.2 核心流程

三者共享一个底层函数 `_get_min_max`（[min_max.py:L70-L74](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/min_max.py#L70-L74)），它对重塑后的张量在 `dim=(0, -1)` 上做 reduce：

```python
def _get_min_max(observed):
    min_vals = torch.amin(observed, dim=(0, -1))   # 同时压掉观测维和 group 维
    max_vals = torch.amax(observed, dim=(0, -1))
    return min_vals, max_vals
```

回顾 4.1.2，`flatten_for_calibration` 把张量变成 `(num_observations, *qparam_shape, group_size)`。在 `dim=(0, -1)` 上 reduce，意味着「把所有观测、每个 group 内的所有元素」一起取极值，留下中间的 `qparam_shape`——这正是 scale 的形状（CHANNEL 策略下是 `[out_features]`，GROUP 策略下是 `[num_groups]`，TENSOR 策略下是标量）。所以 **observer 输出的 min/max 形状 = 该策略下 scale 的形状**，这是 flatten 与 reduce 配合的关键。

三者的差异在合并：

- `memoryless`：直接覆盖（[min_max.py:L18-L19](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/min_max.py#L18-L19)）。
- `static`：与已有统计取 `torch.min` / `torch.max`（[min_max.py:L33-L41](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/min_max.py#L33-L41)）。
- `minmax`（移动平均）：用 `lerp(old, new, avg_constant)` 做线性插值（[min_max.py:L59-L67](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/min_max.py#L59-L67)），`avg_constant` 默认 `0.01`（来自 `observer_kwargs`）。

`lerp` 的定义在 [helpers.py:L218-L220](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/helpers.py#L218-L220)：

\[ \mathrm{lerp}(a, b, w) = a(1-w) + b\cdot w \]

即新统计只占 `w` 的比重，老统计占 `1-w`。

#### 4.2.3 概念说明：MSE observer——寻找最优裁剪范围

MinMax 的痛点：**离群值（outlier）会把整个范围撑得很大**，导致绝大多数正常值被挤在很少的几个量化级别里，舍入误差巨大。MSE 的思路是：**主动裁掉一部分离群值，缩小 [min, max] 范围**，让多数值量化得更准，用「少数大值的截断误差」换「多数小值的舍入误差」，总误差更小。

具体做法是网格搜索：取一个收缩因子 \(p \in (0, 1]\)，用 \([p\cdot x_{\min},\; p\cdot x_{\max}]\) 作为候选范围，fake-quant 后算误差，挑使误差最小的 \(p\)。误差度量：

\[ \mathrm{err}(p) = \sum \left| Q_p(x) - x \right|^{\text{norm}} \]

其中 \(Q_p(x)\) 是用候选范围做的伪量化，`norm` 默认 `2.4`（接近但非严格 MSE，对大误差更敏感）。`maxshrink` 控制最多收缩多少（搜索步数 = `int(maxshrink * grid)`），`grid` 是分辨率，`patience` 是连续多少步无改进就早停。

#### 4.2.4 源码精读

MSE observer 的统计更新委托给 `mse_quant._grid_search_mse`（见 [mse.py:L38-L48](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/mse.py#L38-L48)）。核心循环在 [mse_quant.py:L106-L130](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/mse_quant.py#L106-L130)：

```python
for i in range(total_steps):
    p = 1 - i / grid                       # 收缩因子从 1 递减
    shrinked_min_val = min_val * p
    shrinked_max_val = max_val * p
    err = _calculate_error(observed, args, token_args,
                           shrinked_min_val, shrinked_max_val, norm)
    improved = err < best_error
    if torch.any(improved):
        best_error[improved] = err[improved]              # 逐通道更新最优
        best_min_val[improved] = shrinked_min_val[improved]
        best_max_val[improved] = shrinked_max_val[improved]
        no_improve_count = 0
    else:
        no_improve_count += 1
        if no_improve_count >= patience:                  # 早停
            break
```

注意 `improved` 是逐通道（逐 scale）判断的，所以不同通道可以选不同的收缩比。`_calculate_error`（[mse_quant.py:L244-L273](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/mse_quant.py#L244-L273)）就是「算 qparams → fake_quantize → 算误差」三步。当 `active_session().state.enable_compile` 为真时，会走 `torch.compile` 的分块版本 `_grid_search_compiled` 以加速（[mse_quant.py:L56-L71](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/mse_quant.py#L56-L71)）。

`MovingAverageMSEObserver`（注册名 `mse`）在此基础上再加一层 EMA 平滑（[mse.py:L93-L98](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/mse.py#L93-L98)），`MemorylessMSEObserver`（注册名 `memoryless_mse`）则不加。

#### 4.2.5 概念说明：IMatrix——重要性加权的 MSE

`IMatrixMSEObserver`（注册名 `imatrix_mse`）是 MSE 的进阶版：**不是所有通道同样重要**。它在校准前用一个 forward-pre hook 累积每个输入通道的 \(E[x^2]\)（激活的二阶矩），把它作为重要性权重 \(w_c\)，误差度量变成加权形式：

\[ \mathrm{err}(p) = \sum_c w_c \left| Q_p(x_c) - x_c \right|^{\text{norm}} \]

承载「重要信号」的通道被赋予更高权重，搜索会优先照顾它们。重要性归一化为均值 1（[imatrix.py:L146](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/imatrix.py#L146)）。注意它**只对权重 observer 有意义**（权重按输入通道分组），用于非权重或缺少重要性数据时会回退到普通 MSE 并打 warning（`strict=False` 时）。

这也是为什么 u3-l1 提到：scheme 里用了 `imatrix_mse` 会自动让 `requires_calibration_data = True`——因为它需要校准数据来算 \(E[x^2]\)（见 [mixin.py:L307-L309](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L307-L309)）。

#### 4.2.6 源码精读：IMatrix 的 hook 与统计扩展

IMatrix 在 `attach` 时注册 forward-pre hook 累积 \(E[x^2]\)（[imatrix.py:L74-L115](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/imatrix.py#L74-L115)）：

```python
def attach(self, module):
    ...
    self._imatrix_sum = torch.zeros(in_features, dtype=IMATRIX_PRECISION, device=device)
    self._imatrix_count = torch.tensor(0, dtype=torch.int64, device=device)
    def _hook(mod, args):
        ...
        token_sum = x_f.pow(2).sum(dim=list(range(x_f.dim() - 1)))  # 每个输入通道的 x² 之和
        self._imatrix_sum.add_(token_sum)
        self._imatrix_count += n_tokens
    self._imatrix_hook = module.register_forward_pre_hook(_hook)
```

它把 `_stats_attrs` 扩展为 `["min_vals", "max_vals", "_imatrix_sum", "_imatrix_count"]`（[imatrix.py:L42](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/imatrix.py#L42)），所以基类的 `has_statistics` / `delete_statistics` 自动覆盖这些字段——这就是 `_stats_attrs` 设计的妙处：子类只要声明字段名，生命周期管理就白捡。

#### 4.2.7 权重 observer 为何被强制降级为 memoryless

回到 `initialize_observer`（[calibration.py:L36-L83](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py#L36-L83)）。它有一段「强制降级」逻辑（[calibration.py:L64-L78](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py#L64-L78)）：

```python
# training is no longer supported: always use memoryless for weights
if base_name == "weight" and args.observer in ("static_minmax", "minmax"):
    observer = "memoryless_minmax"
    logger.warning("Overriding weight observer for lower memory usage ...")
if base_name == "weight" and args.observer in ("mse",):
    observer = "memoryless_mse"
    logger.warning("Overriding weight observer for lower memory usage ...")
```

原因有二：① 权重只观测一次（4.1.3），累积/平均语义本就用不上；② `static`/`minmax`/`mse` 这些「带状态」的 observer 为了跨 batch 累积会保留中间统计，对一次性观测的权重是纯内存浪费。注意 `imatrix_mse` **不在降级名单**里——它本身就需要跨 batch 累积 \(E[x^2]\)，所以对权重也保留。

#### 4.2.8 小练习与答案

**练习 1**：`static_minmax` 和 `minmax` 都支持跨多次观测合并，为什么权重 observer 仍要降级成 `memoryless_minmax` 而不是用 `static_minmax`？

**答案**：权重是常量，只会被 `observe` 一次，根本没有「多次观测合并」的需求；`static`/`minmax` 内部为合并预留的状态字段对单次观测毫无价值，只会多占内存。`memoryless` 每次观测直接覆盖，最省。

**练习 2**：MSE observer 的 `maxshrink` 调大（比如从 0.2 到 0.8）会发生什么？

**答案**：`maxshrink` 决定搜索步数 `int(maxshrink * grid)`，调大意味着在更大的收缩范围内搜索（允许更激进地裁掉离群值）。可能找到更小的总误差，但搜索耗时也更长，且过度裁剪可能伤害被裁掉通道的精度——这是一个误差/耗时/精度的权衡。

---

### 4.3 取整三连：observe → update_qparams → freeze

本节是全讲的「主菜」：把 4.1、4.2 的零件串成 u3-l2 留下的那个问题——子类显式调用的 `observe` + `update_qparams` 到底做了什么，以及 `freeze` 如何收尾。

#### 4.3.1 概念说明

`calibration.py` 是一组**模块级**的纯函数（不挂在任何类上），它们操作单个 `nn.Module`，读写模块上以 `base_name` 为前缀的属性。取整三连的职责划分：

| 函数 | 职责 | 改变的模块属性 |
| --- | --- | --- |
| `observe(module, base_name)` | 触发 observer 收集统计 | observer 内部的 `min_vals`/`max_vals` |
| `update_qparams(module, base_name)` | 把统计换算成 scale/zp 并写回 | `module.{base_name}_scale`、`{base_name}_zero_point` |
| `freeze_module_quantization(module)` | 删除 observer、状态置 FROZEN | 删掉 `{base_name}_observer`，设 `quantization_status` |

#### 4.3.2 核心流程

`observe`（[calibration.py:L86-L106](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py#L86-L106)）极其薄：

```python
def observe(module, base_name):
    if isinstance(module, Iterable):           # 支持传一组模块
        for m in module:
            observe(m, base_name)
        return
    observer = getattr(module, f"{base_name}_observer", None)
    if observer is None:
        return
    observer(getattr(module, base_name))       # 即 observer(weight) 或 observer(input)
```

它做两件事：按命名约定拿到 `{base_name}_observer`，然后把它「喂」给 observer，被观测的值就是 `getattr(module, base_name)`——对权重是 `module.weight`，对激活是 hook 传入的激活张量（见 4.4）。

`update_qparams`（[calibration.py:L109-L162](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py#L109-L162)）核心是换算 + 写回：

```python
qparams = observer.get_qparams()               # 4.1 讲过的换算（含删除统计）
for param_name, param_val in qparams.items():
    if param_val is None:
        continue
    if is_dynamic and param_name in ("scale", "zero_point"):
        continue                               # 动态量化：scale/zp 推理时才算，这里跳过
    full_param_name = f"{base_name}_{param_name}"   # 如 weight_scale / weight_global_scale
    if hasattr(module, full_param_name):
        if not only_update_onload:
            update_offload_parameter(module, full_param_name, param_val)  # 同步 offload 副本
        else:
            getattr(module, full_param_name).data = param_val             # 仅更新 onloaded
```

两个要点：
1. **动态量化的特殊处理**：对于动态激活量化（`dynamic=True`），scale/zero-point 是推理时按当前激活现算的，校准阶段不写死，只可能写 `global_scale`。
2. **`update_offload_parameter`**：因为大模型权重可能在 CPU/disk offload（见 u6-l2），写 scale 时要同时更新 offload 副本和 onloaded 副本，否则两者会不一致。`only_update_onload` 是 DDP 场景下只让一个 rank 写 offload 的开关。

`freeze_module_quantization`（[calibration.py:L207-L231](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py#L207-L231)）收尾：

```python
def freeze_module_quantization(module):
    scheme = getattr(module, "quantization_scheme", None)
    if not scheme:
        return
    if module.quantization_status == QuantizationStatus.FROZEN:
        return                                  # 幂等：已冻结直接返回
    for name in ("input", "weight", "output", "q", "k", "v"):   # 删掉所有 observer
        obs_name = f"{name}_observer"
        if hasattr(module, obs_name):
            getattr(module, obs_name).detach(module)
            delattr(module, obs_name)
    module.quantization_status = QuantizationStatus.FROZEN
```

冻结后 observer 被删除（释放内存），模块状态从 CALIBRATION 切到 FROZEN，量化保持开启——此后前向走的就是真正的量化算子，不再是「收集统计」模式。

#### 4.3.3 源码精读：on_sequential_epoch_end 的编排

把三连放回真实调用方——`QuantizationModifier.on_sequential_epoch_end`（[base.py:L83-L108](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/base.py#L83-L108)），这是理解「激活与权重为何分开处理」的关键：

```python
def on_sequential_epoch_end(self, state, event, modules, **kwargs):
    modules = [m for m in modules if is_module_quantized(m)]
    self.sync_obs_act_stats(modules)            # ① DDP：同步激活统计
    update_qparams(modules, ACTIVATION_OBS)     # ② 激活：只 update（统计已由 hook 累积完）

    if not is_distributed():
        observe(modules, "weight")              # ③ 权重：observe + update 一次性完成
        update_qparams(modules, "weight")
        return
    # 分布式：按 rank 分配模块、各自 observe+update、再广播 qparams
    ...
```

注意两条截然不同的路径：
- **激活**（`input`/`output`/`q`/`k`/`v`）：统计是在整个 epoch 里由校准 hook 跨 batch 累积好的（4.4），到 epoch 末只需 `update_qparams` 换算写回，**不需要再 observe**。
- **权重**：`weight` observer 没有挂 hook（权重不需要在前向中被动观测），所以必须在这里**主动 `observe` 一次**（触发对 `module.weight` 的统计），紧接着 `update_qparams` 换算。这就是 u3-l2 反复强调的「子类必须显式调用 observe + update_qparams」的具体落点。

`ACTIVATION_OBS` 是一个常量元组 `("input", "output", "q", "k", "v")`（[helpers.py:L25](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/helpers.py#L25)）。注意 `update_qparams(modules, ACTIVATION_OBS)` 的第二个参数是元组，函数内部会用 `itertools.product` 把「每个模块 × 每个 base_name」两两组合逐一处理（[calibration.py:L129-L134](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py#L129-L134)）。这就是 `ACTIVATION_OBS` / `WEIGHT_OBS`（即 `"weight"`）两套命名的由来：前者一组（多 base_name），后者单个。

> 权重对应的 DDP 分支（`greedy_bin_packing` + `broadcast_qparams_and_cleanup`）留待 u6-l1 精读，本讲只需知道非分布式下是朴素的 observe + update。

#### 4.3.4 代码实践

**目标**：亲手完成一次「挂 scheme → 挂 observer → observe → update_qparams → freeze」全流程，看清模块属性的变化。依据是 `test_observers.py::test_observers_update`（[test_observers.py:L30-L58](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/modifiers/calibration/test_observers.py#L30-L58)）。

**步骤**（示例代码）：

```python
import torch
from compressed_tensors.quantization import (
    QuantizationArgs, QuantizationScheme, initialize_module_for_quantization,
)
from llmcompressor.modifiers.quantization.calibration import (
    initialize_observer, observe, update_qparams, freeze_module_quantization,
)

module = torch.nn.Linear(64, 128)
scheme = QuantizationScheme(
    targets=["Linear"],
    weights=QuantizationArgs(num_bits=8, symmetric=True, strategy="channel"),
    input_activations=QuantizationArgs(num_bits=8, symmetric=True, strategy="tensor"),
)
initialize_module_for_quantization(module, scheme)   # 挂 scheme、创建 scale/zp 占位
initialize_observer(module, "weight")                # 挂 weight_observer（被降级为 memoryless_minmax）
initialize_observer(module, "input")                 # 挂 input_observer

print("observe 前有 weight_scale 吗:", hasattr(module, "weight_scale"))   # True，但还是占位值
observe(module, "weight")                            # 收集权重统计
update_qparams(module, "weight")                     # 换算并写回 weight_scale / weight_zero_point
print("update 后 weight_scale 形状:", module.weight_scale.shape)          # [128]（CHANNEL）
print("update 后 weight_observer 还在吗:", hasattr(module, "weight_observer"))  # True（还没 freeze）

freeze_module_quantization(module)                   # 删 observer、置 FROZEN
print("freeze 后 weight_observer 还在吗:", hasattr(module, "weight_observer"))  # False
print("状态:", module.quantization_status)           # QuantizationStatus.FROZEN
```

**需要观察的现象**：`initialize_module_for_quantization` 后模块就已有 `weight_scale` 占位（全 1 之类），但只有 `observe`+`update_qparams` 后才写入真实统计换算出的值；`freeze` 之后 observer 消失、状态变 FROZEN，但 `weight_scale` 保留。

**预期结果**：`weight_scale.shape == torch.Size([128])`（CHANNEL 策略，每个输出通道一个 scale）；freeze 后 observer 被删除。若你的环境缺少 `compressed_tensors`，请先 `pip install llmcompressor`（它会带入该依赖）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `update_qparams(modules, ACTIVATION_OBS)` 之前没有对应的 `observe(modules, ACTIVATION_OBS)`？

**答案**：激活的统计不是在这一刻收集的，而是整个 epoch 里由 `calibrate_input_hook` / `calibrate_output_hook` 在每次前向时喂给 observer 累积的（见 4.4）。到 epoch 末，激活 observer 早已 `has_statistics == True`，所以只需换算写回，不用再 observe。

**练习 2**：`freeze_module_quantization` 删除 observer 后，模块还能正常量化前向吗？

**答案**：能。freeze 删的是 observer（统计收集器）并把状态置为 FROZEN，但 `weight_scale` / `weight_zero_point` 等量化参数保留，量化保持开启（`end_calibration` 里会调 `enable_quantization`）。FROZEN 状态下前向走的就是用已算好的 scale 做真正量化的算子，不再收集统计。

---

### 4.4 校准 Hooks：把前向激活喂给 observer

本节回答：**激活的统计是怎么「自动」累积起来的？** 答案是 forward hook。

#### 4.4.1 概念说明

权重是模块的属性（`module.weight`），可以主动 `observe`；但激活是前向传播时才产生的瞬时张量，怎么把它交给 observer？答案是挂 hook：

- **forward-pre hook**（`calibrate_input_hook`）：在模块前向**之前**拦截输入，喂给 `input_observer`；
- **forward hook**（`calibrate_output_hook`）：在模块前向**之后**拦截输出，喂给 `output_observer`，并顺带对输出做一次伪量化（让下游看到「量化后的」激活，误差才能逐层传播）。

回顾 u3-l2，`QuantizationMixin._initialize_hooks` 就是把这两个 hook 挂到每个目标模块上（[mixin.py:L468-L498](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L468-L498)）。本讲看 hook 内部。

#### 4.4.2 核心流程

```
校准前向：  input ──► [calibrate_input_hook] ──► module.forward ──► [calibrate_output_hook] ──► output
                         │                                          │
                         ▼                                          ▼
                  input_observer(输入)                      output_observer(输出)
                                                           + forward_quantize(输出)  # 伪量化
```

每个 hook 只做两件事：取张量、调 observer。observer 的 `forward` 会累积统计（4.1）。

#### 4.4.3 源码精读

输入 hook（[calibration.py:L165-L170](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py#L165-L170)）：

```python
def calibrate_input_hook(module, args):
    args = args[0] if isinstance(args, tuple) else args
    module.input_observer(args)
```

输出 hook（[calibration.py:L173-L184](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py#L173-L184)）多一步伪量化：

```python
def calibrate_output_hook(module, _args, output):
    module.output_observer(output)            # ① 收集输出统计
    output = forward_quantize(                # ② 对输出伪量化，让下游看到量化后激活
        module=module, value=output, base_name="output",
        args=module.quantization_scheme.output_activations,
    )
    return output
```

第②步是 sequential 校准能传播量化误差的关键：如果只收集统计而不伪量化，下游模块看到的就是全精度激活，校准出来的 scale 会过于乐观。`forward_quantize` 来自 `compressed_tensors.quantization.lifecycle.forward`，用当前（还在更新的）scale 对输出做量化。

对 attention 的 KV cache 量化，还有 `calibrate_query_hook` / `calibrate_key_hook` / `calibrate_value_hook`（[calibration.py:L187-L196](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/../../modifiers/quantization/calibration.py#L187-L196)），分别喂 `q_observer` / `k_observer` / `v_observer`，由 compressed-tensors 的 `register_query_hook` / `register_key_hook` / `register_value_hook` 注册（见 hooks.py 的 `_get_register_function`，[hooks.py:L123-L133](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/utils/hooks.py#L123-L133)）。这些 hook 的内容同样很薄，例如 `calibrate_query_hook` 只有 `module.q_observer(query_states)` 一行（[calibration.py:L187-L196](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py#L187-L196)）。

#### 4.4.4 概念说明：HooksMixin 的「全局禁用」开关

sequential pipeline 在逐子图校准时，会先跑一遍前向触发 hook 收集统计、再跑一遍前向捕获「量化后」的中间激活（详见 u3-l5）。第二遍前向不希望再触发 hook 重复累积统计。`HooksMixin.disable_hooks` 这个上下文管理器解决了这个问题——它用**类变量**做全局开关，一次禁用所有 modifier 的所有 hook：

#### 4.4.5 源码精读

[hooks.py:L52-L67](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/utils/hooks.py#L52-L67)：

```python
@classmethod
@contextlib.contextmanager
def disable_hooks(cls, keep=frozenset()):
    try:
        cls._HOOKS_DISABLED = True            # 类级开关：所有实例共享
        cls._HOOKS_KEEP_ENABLED |= keep       # 可保留个别 hook（如仅禁用除某几个外的全部）
        yield
    finally:
        cls._HOOKS_DISABLED = False
        cls._HOOKS_KEEP_ENABLED -= keep
```

`register_hook` 注册时会把 hook 包一层 `wrapped_hook`，进入时先检查开关（[hooks.py:L89-L99](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/utils/hooks.py#L89-L99)）：

```python
def wrapped_hook(*args, **kwargs):
    if HooksMixin._HOOKS_DISABLED and handle not in HooksMixin._HOOKS_KEEP_ENABLED:
        return                                # 被禁用且不在保留名单：直接跳过
    return hook(*args, **kwargs)
```

用「类变量」而非「实例变量」是有意为之：禁用是跨所有 modifier 的全局语义， sequential pipeline 只需 `with HooksMixin.disable_hooks():` 就能临时关掉全校准 hook，退出后自动恢复。注意 IMatrix 的内部 hook 也遵循同一开关（[imatrix.py:L92-L97](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/imatrix.py#L92-L97)），保证一致性。

#### 4.4.6 小练习与答案

**练习**：如果删掉 `calibrate_output_hook` 里的 `forward_quantize` 那一步（只保留 `output_observer(output)`），sequential 量化结果会偏向乐观还是悲观？

**答案**：偏向乐观（误差偏小）。没有伪量化，下游模块的输入激活校准用的是全精度值，离群值/分布都与真实量化后不同，导致下游 scale 算得过宽，最终真实推理时误差被低估。`forward_quantize` 的作用就是让校准阶段就「体验」到上游量化带来的激活畸变，使误差逐层传播、被后续 scale 补偿。

---

### 4.5 多 rank / 融合：FusionHandler 与 DDP 同步

本节讲两个进阶机制：`TENSOR_GROUP`（NVFP4）下融合层共享 `global_scale`，以及 DDP 下激活统计的跨卡同步。二者都属于「把多个 observer 的统计合到一起」的场景。

#### 4.5.1 概念说明：融合层共享 global_scale

NVFP4 用 `TENSOR_GROUP` 策略：一组张量共享一个 `global_scale`，每个张量再有自己的局部 scale（`scale = global_scale * local_factor` 形式）。vLLM 的 NVFP4 kernel 要求**融合层**（`q_proj`/`k_proj`/`v_proj` 融合成的 `qkv_proj`，或 `gate_proj`/`up_proj` 融合成的 `gate_up_proj`）共享同一个 `global_scale`——因为推理时它们被拼成一个矩阵一次算完。

但校准时它们是分开的四个模块、四个 weight observer。如何让它们算出同一个 `global_scale`？答案：把这四个 observer「融合」成一组，算 `global_scale` 时取全组的整体 absmax。

#### 4.5.2 核心流程

```
fuse_weight_observers(model)            # 扫描模型，发现融合层组合并 observer
   │  遍历 FUSED_LAYER_NAMES: ("q_proj","k_proj","v_proj"), ("gate_proj","up_proj"), ...
   │  对每个同时含这些子模块的父模块：
   ▼
FusionHandler.fuse([(obs1,mod1), (obs2,mod2), ...])   # 用 weakref 把它们的 handler 互连
   ─────── 校准进行中，各 observer 各自收集 min/max ───────
   ▼
某 observer.get_qparams() → get_global_scale()
   │  → fusion_handler.get_fused_statistics()   # 收集全组（含自己）的 min/max
   │  → 对全组取 global_absmax                   # max over {max, -min} across group
   │  → generate_gparam(-absmax, absmax)         # compressed-tensors 换算 global_scale
   ▼
所有 fused observer 得到同一个 global_scale
```

#### 4.5.3 源码精读

融合层的定义在 [helpers.py:L162-L171](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/helpers.py#L162-L171)，涵盖 attention 的 qkv、MLP 的 gate/up、DeepSeek 的 MLA、MoE 的 w1/w3 等命名。扫描与合并逻辑 `fuse_weight_observers`（[helpers.py:L174-L215](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/helpers.py#L174-L215)）有两个断言：要么整组都有 weight observer、要么整组都是 `TENSOR_GROUP`，否则报错（防止半融合的不一致）。它在 `QuantizationMixin.start_calibration` 末尾被调用（[mixin.py:L257](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L257)）。

`global_scale` 的换算在 `Observer.get_global_scale`（[base.py:L78-L96](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/base.py#L78-L96)）：

```python
def get_global_scale(self):
    if self.args.strategy != QuantizationStrategy.TENSOR_GROUP:
        return None                           # 非 TENSOR_GROUP：无 global_scale
    all_stats = self.fusion_handler.get_fused_statistics()
    global_absmax = all_stats[0]["max_vals"].max()
    for stats in all_stats:
        global_absmax = torch.max(global_absmax, -stats["min_vals"].min())  # 取 -min
        global_absmax = torch.max(global_absmax, stats["max_vals"].max())   # 取 max
    return generate_gparam(-global_absmax.reshape(1), global_absmax.reshape(1))
```

它对全组的 min/max 取「全局绝对值最大」，这正是 global_scale 应代表的——覆盖整组张量的最大动态范围。

`FusionHandler` 的精妙处在 `get_fused_statistics`（[fusion.py:L60-L79](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/fusion.py#L60-L79)）：如果组内某个同伴还没被观测过（权重 observer 可能还没到 `observe` 时机），它会通过 `fuse` 时存的 weakref 找到那个模块，**主动帮它观测一次权重**（`obs(mod.weight)`）。这样无论哪个 observer 先调 `get_qparams`，都能拿到完整的全组统计。

#### 4.5.4 概念说明：协作式删除

`get_qparams` 会 `delete_statistics()`。但在融合组里，统计是全组共享的——如果 A observer 换算完就删了自己的 min/max，B observer 再换算时 `get_fused_statistics` 就拿不到 A 的统计了。`maybe_delete_statistics`（[fusion.py:L81-L101](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/fusion.py#L81-L101)）用「计数到齐才删」解决：每个 handler 换算完标记 `_deletion_called=True`，只有当**全组都标记完**，才一起删——这就是「cooperative deletion」。

#### 4.5.5 概念说明：DDP 激活统计同步

多卡量化时，每张卡看到不同的校准数据（data parallel），所以**激活统计各不相同**，必须 all-reduce 合并；而权重各卡相同，无需同步。`sync_activation_stats`（[mixin.py:L272-L294](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L272-L294)）在 `on_sequential_epoch_end` 开头被调用，遍历每个模块的 `ACTIVATION_OBS + ("weight",)` 各 observer，调 `observer.sync_activation_stats()`。

#### 4.5.6 源码精读

`sync_activation_stats`（[base.py:L147-L162](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/base.py#L147-L162)）按 `_act_sync_dict` 声明的 reduce 操作异步 all-reduce：

```python
def sync_activation_stats(self):
    comms = []
    for attr_name, reduce_op in self._act_sync_dict.items():
        val = getattr(self, attr_name, None)
        if val is not None:
            comms.append(dist.all_reduce(as_broadcastable(val), op=reduce_op, async_op=True))
    return comms
```

各 observer 用 `_act_sync_dict` 声明自己的同步语义（回顾 4.2）：
- `static_minmax`：`min_vals → MIN`、`max_vals → MAX`（跨卡取极值，[min_max.py:L28-L31](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/min_max.py#L28-L31)）；
- `minmax`（移动平均）：`AVG`；
- `imatrix_mse`：`_imatrix_sum → SUM`、`_imatrix_count → SUM`（先求和再相除得全局 \(E[x^2]\)，[imatrix.py:L37-L40](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/imatrix.py#L37-L40)）；
- `memoryless_*`：空字典（权重用，不同步）。

reduce 操作的选择有讲究：min/max 统计跨卡应取极值（MIN/MAX），求和量应取 SUM，平均量取 AVG。`async_op=True` 让通信异步进行，最后由 `wait_for_comms` 统一等待（[mixin.py:L294](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L294)），通信与计算可重叠。

#### 4.5.7 小练习与答案

**练习 1**：为什么 `sync_activation_stats` 不同步权重 observer 的统计？

**答案**：DDP 下各卡的权重是相同的（权重是模型参数，被同步），所以权重统计跨卡必然一致，同步是冗余的；只有激活因每卡喂入不同校准数据而不同，需要 all-reduce。`memoryless_*` 的 `_act_sync_dict` 为空也正因权重 observer 多用于权重。

**练习 2**：`FusionHandler` 为什么用 `weakref` 引用模块，而不是强引用？

**答案**：避免引用环导致模块无法被垃圾回收。observer 是模块的子模块（强引用模块），若 handler 再强引用模块，就形成「模块 → observer → handler → 模块」的环；weakref 打破这个环，模块生命周期不受 observer 影响。`get_fused_statistics` 里 `assert mod is not None` 正是在 weakref 失效（模块已被回收）时报错（`_msg`，[fusion.py:L14](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/fusion.py#L14)）。

---

## 5. 综合实践

把本讲的知识串起来，完成一个「对比 MinMax 与 MSE observer」的小实验，直观感受两种统计策略对量化参数和量化误差的影响。本实践参考 `test_observer_min_max_vals` 与 `test_observers_update` 的写法，无需 GPU、无需完整模型，只需 torch + compressed_tensors（随 llmcompressor 安装）。

**实践目标**：构造一组带离群值的「伪激活」，分别用 `static_minmax` 和 `memoryless_mse` 算出 scale/zero-point，量化同一组数据，比较量化误差，体会 MSE「裁剪离群值」的效果。

**操作步骤**（示例代码）：

```python
import torch
from compressed_tensors.quantization import QuantizationArgs
from compressed_tensors.quantization.lifecycle import fake_quantize
from llmcompressor.observers import Observer

torch.manual_seed(0)
# 构造带离群值的激活：99% 在 [-1,1]，1% 是大离群值
base = torch.randn(1024, 64) * 0.3
outliers = (torch.rand(1024, 64) > 0.99) * 8.0 * (torch.rand(1024, 64) * 2 - 1)
x = base + outliers                      # 含离群值的张量

args = QuantizationArgs(num_bits=8, symmetric=True, strategy="tensor")
results = {}
for name in ("static_minmax", "memoryless_mse"):
    obs = Observer.load_from_registry(name, base_name="input", args=args)
    obs(x)                               # observe：收集统计
    qp = obs.get_qparams()               # update_qparams 的换算部分
    scale, zp = qp["scale"], qp["zero_point"]
    # 用算出的 scale/zp 做伪量化，算 MSE
    xq = fake_quantize(x, scale.unsqueeze(0), zp.unsqueeze(0), args)
    mse = ((xq - x) ** 2).mean().item()
    results[name] = (scale.item(), zp.item(), mse, float(x.abs().max()))
    print(f"{name:18s} scale={scale.item():.4f} zp={zp.item()} mse={mse:.6f}")

print("\n离群值最大幅值:", float(x.abs().max()))
print("结论：MSE 的 scale 通常更小（裁掉离群值），整体 MSE 更低；")
print("     MinMax 的 scale 被离群值撑大，正常值量化精度更差。")
```

**需要观察的现象**：
1. `static_minmax` 的 scale 应接近 `x.abs().max() / 127`（被离群值撑大）。
2. `memoryless_mse` 的 scale 通常更小（主动裁剪），整体 MSE 更低——但被裁掉的离群值本身误差更大。
3. 可试着把 `outliers` 的系数从 8 调到 0（去掉离群值），两种 observer 的 scale 与 MSE 应趋于接近，印证「MSE 的优势来自处理离群值」。

**预期结果**：含离群值时，`memoryless_mse` 的整体 MSE 显著低于 `static_minmax`；无离群值时两者接近。若数值不一致，检查 `QuantizationArgs` 是否设了 `symmetric=True`。具体数值「待本地验证」（依赖 torch 版本与随机种子）。

**延伸（源码阅读型）**：阅读 `tests/llmcompressor/observers/test_fused_observers.py`，理解融合 observer 的测试如何构造 Q/K/V 三个模块、调用 `fuse_weight_observers`、断言三者 `global_scale` 一致——这是把 4.5 的融合机制落到测试层面的范本。

## 6. 本讲小结

- **Observer 是统计收集器，不做换算数学**：它把张量变成 `min_vals`/`max_vals`，`scale`/`zero-point` 的换算委托给 `compressed_tensors.calculate_qparams`；统计在 `get_qparams` 后被一次性删除，故 `update_qparams` 必须紧跟 `observe`。
- **三种 MinMax 策略差异在「多次观测如何合并」**：`memoryless`（只留最后一次，用于权重）、`static`（running min/max，用于静态激活）、`minmax`（EMA）；权重 observer 在 `initialize_observer` 里被强制降级为 `memoryless` 省内存。
- **MSE / IMatrix 用网格搜索寻找最优裁剪范围**：通过收缩 [min,max] 裁掉离群值，最小化量化误差；IMatrix 再用校准激活的 \(E[x^2]\) 做通道重要性加权。
- **取整三连 `observe` → `update_qparams` → `freeze`** 是模块级纯函数：激活统计由 hook 跨 batch 累积、epoch 末只 update；权重无 hook、需在 `on_sequential_epoch_end` 主动 observe+update；freeze 删 observer 并置 FROZEN。
- **校准 hook 把前向激活喂给 observer**：`calibrate_output_hook` 还会对输出伪量化，让量化误差逐层传播；`HooksMixin.disable_hooks` 用类变量做全局开关，供 sequential pipeline 临时关 hook。
- **融合与 DDP 同步**：`TENSOR_GROUP`（NVFP4）下 `FusionHandler` 让 Q/K/V、gate/up 共享 `global_scale`，用协作式删除保证全组统计到齐才清；DDP 下 `sync_activation_stats` 按 `_act_sync_dict` 声明的 reduce 操作 all-reduce 激活统计，权重因各卡相同而不同步。

## 7. 下一步学习建议

- **下一讲 u3-l4（CalibrationPipeline 注册与选择）**：本讲讲的是「单个模块怎么校准」，下一讲上升到「整模型怎么组织校准」——`CalibrationPipeline.from_modifiers` 如何根据 `requires_calibration_data` 在 sequential / datafree 间推断管线。本讲的 hook、observer 是管线的「弹药」，下一讲是「发射器」。
- **深入 sequential 管线（u3-l5）**：本讲多次提到「两遍前向」「disable_hooks」，其完整含义要到 `SequentialPipeline` 才讲清——它如何切子图、如何在子图边界同步激活统计（正好用到本讲的 `sync_obs_act_stats`）。
- **DDP 与大模型（u6-l1、u6-l2）**：本讲的 `broadcast_qparams_and_cleanup`、`update_offload_parameter`、`only_update_onload` 等 DDP/offload 细节，留到第六单元结合 `greedy_bin_packing` 与磁盘 offloading 系统讲解。
- **自定义 Observer（u6-l5）**：若你想实现自己的统计策略（比如百分位 observer），本讲的 `Observer` 基类、`_stats_attrs`、`_act_sync_dict`、`@Observer.register` 就是你要继承和实现的全部接口。
