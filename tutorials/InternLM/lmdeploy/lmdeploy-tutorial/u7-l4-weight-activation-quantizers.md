# 权重与激活量化器

## 1. 本讲目标

本讲是量化单元（U7）的「最底层」。前几讲（u7-l1 校准、u7-l2 AWQ、u7-l3 GPTQ/SmoothQuant）讲的都是「编排函数」——它们串起「加载模型→校准→平滑→量化→写盘」整条链。本讲下钻到这些编排函数脚下踩的那块砖：**真正的量化数学原语**。

读完本讲你应能：

1. 说清 `WeightQuantizer` 的三组旋钮（`bits` / `symmetry` / `granularity`）如何决定一段权重被怎样量化，并能解释它的 `calculate_qparams` + `quant` 两步法。
2. 看懂 `cal_qparams_*` 系列函数如何针对 per-tensor / per-channel / per-group 三种粒度、absmax / minmax 两种对称模式，算出 `scales` 与 `zero_points`，并理解 `quant_utils.py` 中 FP8 分块量化与之的分工。
3. 理解 `ActivationObserver` 如何用 forward hook 在「不改模型代码」的前提下逐通道累积 `max/min/mean/absmax/absmean`，以及 `GlobalAvailMixin` 如何让 hook 在全局注册表里找到正确的观察器。

本讲只引用三个核心文件加少量上下游文件，**不重复** u7-l1/u7-l2/u7-l3 已讲过的编排逻辑，只承接它们留下的「absmax 供 SmoothQuant、absmean 供 AWQ」「per-group 非对称量化」等结论，把结论对应的代码实现讲透。

## 2. 前置知识

### 2.1 量化的两个基本动作

量化（quantization）= 把高精度浮点数（如 FP16）压成低比特整数（如 INT4/INT8），用一个「缩放因子 scale（和一个可选的零点 zero_point）」记录映射关系。它由两步组成：

- **计算量化参数**：从一段数值的统计量（最大值、最小值、绝对最大值）推出 `scale`（与 `zero_point`）。
- **量化/反量化**：`quantized = round(w / scale) [+ zero_point]`；反量化时 `fake_w = quantized * scale [- zero_point * scale]`。

「伪量化（fake quant）」指量化后又立刻反量化回浮点——值还是浮点 dtype，但被限制在了一组离散网格点上。这是校准/搜索阶段用来「模拟整数运算误差」的标准手段。

### 2.2 对称 vs 非对称、粒度

- **对称（symmetric，absmax）**：以 0 为中心，\(\text{scale}=\max|w|/q_{\max}\)，\(q_{\max}=2^{b-1}-1\)（有符号），**没有 zero_point**。简单高效，但若权重整体偏向一边（如全是正数）会浪费一半量化范围。
- **非对称（asymmetric，minmax）**：用 \([w_{\min},w_{\max}]\) 整段范围，\(\text{scale}=(w_{\max}-w_{\min})/q_{\max}\)，\(q_{\max}=2^{b}-1\)（无符号），带 `zero_point` 记录零的位置。精度更高、参数更多。

**粒度（granularity）** 决定多少个数值共用一组 scale：

| 粒度 | 一组 scale 覆盖 | scale 形状（权重 (out_c, in_c)） |
|---|---|---|
| per_tensor | 整个张量 | 标量 `()` |
| per_channel | 每个输出通道 | `(out_c, 1)` |
| per_group | 每 `group_size` 个输入通道 | `(out_c, in_c//group_size, 1)` |

粒度越细，量化误差越小，但 scale 占用的额外存储越大。AWQ/GPTQ 这类 weight-only 4bit 量化普遍用 **per_group**（`group_size=128`），在精度和开销间折中。

### 2.3 与前几讲的衔接

- u7-l1 的 `CalibrationContext` 用 forward hook 截取每层 Linear 的输入/输出，累积 **per-channel absmax/absmean**——这「累积」的内部实现就是本讲的 `ActivationObserver`。
- u7-l2 的 AWQ 打包阶段做 **per-group 非对称量化**——其底层参数计算和「伪量化」语义来自本讲的 `WeightQuantizer` 与 `cal_qparams_per_group_minmax`；打包前的 `pseudo_quantize_tensor` 是同语义的独立实现。
- u7-l3 的 SmoothQuant 复用校准的 `absmax` 做 W8A8——激活侧统计同样出自 `ActivationObserver`。

一句话：**前几讲是「指挥官」，本讲是「弹药库」**。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
|---|---|---|
| `lmdeploy/lite/quantization/weight/quantizer.py` | `WeightQuantizer` 类：统一的权重量化接口 | 核心：weight quantizer |
| `lmdeploy/lite/utils/cal_qparams.py` | `cal_qparams_*` 六个参数计算函数 + `QParams` 容器 | 核心：参数计算工具（被 `WeightQuantizer` 派发调用） |
| `lmdeploy/lite/quantization/weight/quant_utils.py` | `quant_blocked_fp8` 等 FP8 分块量化工具 | 核心：quant_utils（FP8 量化路径） |
| `lmdeploy/lite/quantization/activation/observer.py` | `ActivationObserver` / `KVCacheObserver` 激活统计 | 核心：activation observer |
| `lmdeploy/lite/utils/global_avail.py` | `GlobalAvailMixin` 全局注册表 | 关键依赖：让 observer 在 hook 里被「按名找到」 |
| `lmdeploy/lite/quantization/calibration.py` | `CalibrationContext` | 上游：observer 的实际使用者 |
| `lmdeploy/lite/quantization/awq.py` | AWQ 编排（`quant_weights`/`pseudo_quantize_tensor`） | 上游：WeightQuantizer 的实际使用者 |
| `lmdeploy/lite/quantization/modules/linear.py` | `WeightOnlyQLinear` | 下游：把量化结果打包进 int32 |

> 注意一个容易混淆的点：任务里说的「quant_utils 的参数计算工具」其实跨了**两个文件**——`WeightQuantizer` 真正调用的 `cal_qparams_*` 住在 `lmdeploy/lite/utils/cal_qparams.py`；而同名的 `lmdeploy/lite/quantization/weight/quant_utils.py` 装的是另一条 **FP8 分块量化**路径的工具。本讲 4.2 会把两者一并讲清。

## 4. 核心概念与源码讲解

### 4.1 权重量化器 WeightQuantizer

#### 4.1.1 概念说明

`WeightQuantizer` 是 lmdeploy lite 里**面向整段权重张量**的统一量化入口。它不关心模型结构、不做平滑、不碰激活——只回答一个问题：给我一个 `(out_features, in_features)` 的权重张量，按指定 `bits/symmetry/granularity` 把它伪量化成什么样。

它的设计是典型的「**配置对象 + 派发表 + 两步法**」：

- **配置对象**：`__init__` 把三组旋钮存成实例属性，构造期就断言非法组合（bits 只能 4 或 8、粒度只能是那三种、per_group 必须给正的 group_size）。
- **派发表**：类属性 `CAL_FUNC_MAP` 是一张 `{粒度: {对称模式: 计算函数}}` 的二级字典，把「选哪个参数计算函数」从 if/else 退化成两次查表。
- **两步法**：对外暴露 `calculate_qparams(weight)`（算 scale/zero_point）和 `quant(weight, qparams, real)`（做伪量化/真量化）两个方法，干净分离「定标」与「量化」。

#### 4.1.2 核心流程

```
WeightQuantizer(bits, symmetry, granularity, group_size)
   │  构造期断言合法性；由 symmetry 派生 observer='absmax' or 'minmax'
   ▼
calculate_qparams(weight)
   │  cal_func = CAL_FUNC_MAP[granularity][observer]   # 两次查表
   │  per_group ⇒ cal_func(weight, bits, group_size)
   │  其他      ⇒ cal_func(weight, bits)
   ▼  返回 QParams(scales, zero_points)
quant(weight, qparams=None, real=False)
   │  若 qparams 为 None，先调 calculate_qparams 补算
   │  按 scales 维度对齐 reshape weight
   │  对称:   real_q = round(w / scale);            fake = real_q * scale
   │  非对称: real_q = precise_round((w-w_min)/scale); fake = (real_q - zp) * scale
   │  还原形状；real=True ⇒ int32 量化整数；real=False ⇒ 原 dtype 伪量化
```

对称与非对称两种定标公式（与 2.2 节对应）：

\[ \text{对称:}\quad \text{scale}=\frac{\max|w|}{2^{b-1}-1} \]

\[ \text{非对称:}\quad \text{scale}=\frac{w_{\max}-w_{\min}}{2^{b}-1},\quad z=\text{round}(-w_{\min}/\text{scale}) \]

#### 4.1.3 源码精读

先看类的整体结构与文档（注意文档里的 Example 就是「算参数→伪量化」两步法的缩影）：

[quantizer.py:20-49](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/weight/quantizer.py#L20-L49) — `WeightQuantizer` 类定义与文档，Example 演示了 `calculate_qparams` + `fake_quant` 的标准用法。

派发表是本类最精巧的地方，它把 3×2=6 种组合变成一张纯数据字典，新增粒度或对称模式只需加一行：

[quantizer.py:51-64](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/weight/quantizer.py#L51-L64) — `CAL_FUNC_MAP`：`{per_group/per_channel/per_tensor: {absmax/minmax: 函数}}`，六个函数都来自 `lmdeploy.lite.utils`（见 4.2.3）。

构造期做两件事：断言合法性、由 `symmetry` 派生内部用的 `observer` 字符串（`absmax` 或 `minmax`）——后者正是 `CAL_FUNC_MAP` 第二级查表的 key：

[quantizer.py:66-86](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/weight/quantizer.py#L66-L86) — `__init__`：断言 `bits in [4,8]`、粒度三选一、per_group 需 `group_size>0`；第 86 行 `self.observer = 'absmax' if symmetry else 'minmax'`。

`calculate_qparams` 极薄——两次查表拿到函数，per_group 多传一个 `group_size` 实参：

[quantizer.py:88-103](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/weight/quantizer.py#L88-L103) — `calculate_qparams`：查表派发，返回 `QParams`。

`quant` 是真正做量化的地方，重点有三：

[quantizer.py:105-155](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/weight/quantizer.py#L105-L155) — `quant`：伪量化/真量化主逻辑。

需要拆开看三段：

1. **形状对齐**（L129-L135）：per_group 的 scale 是三维 `[out_c, in_c//group_size, 1]`，所以把 weight 也 `reshape(out_c, n_groups, -1)`，让每个 group 对齐到一组 scale。注释里写「per tensor scales shape: [1]」其实与实测不符——per_tensor 返回的是 0 维标量 `()`（见 4.2.4 实践），算源码注释的一处小瑕疵。

2. **对称分支**（L137-L140）：`real_qweight = (float_w / scales).round()`，`fake = real_qweight * scales`，无 zero_point。注意这里**没有 clamp**（不饱和），溢出值不会被截断。

3. **非对称分支**（L142-L146）：重新算了一遍 `float_w.min(-1, keepdim=True)` 作为偏移，再 `precise_round((w - w_min)/scale)`，最后 `(real_q - zero_points) * scale`。这里的 `precise_round` 是「向远离零方向舍入」（见 4.2.3），比 `torch.round` 的「四舍六入五成双」更适合量化。

最后 `real=True` 返回 `int32`（供打包），`real=False` 返回原 dtype 的伪量化张量（供误差评估）。

> **一个关键事实**：在 AWQ 实际打包链路里，`WeightQuantizer` 经常只被当作「配置袋」用。看 [awq.py:334-339](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L334-L339)：真正的 per-group 非对称量化由独立的 `pseudo_quantize_tensor` 完成，`WeightQuantizer` 只是随后被传给 `WeightOnlyQLinear.from_linear`，用于读取它的 `.bits/.group_size/.symmetry` 属性。只有在 `from_linear` 收到 `qparams=None` 时（见 [modules/linear.py:108-110](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/modules/linear.py#L108-L110)），才会真正调用 `quantizer.calculate_qparams` + `quantizer.quant(real=True)`。所以 `WeightQuantizer` 身兼两职：既能独立量化，也能当配置载体。

#### 4.1.4 代码实践

**目标**：用一个 CPU 上跑得动的小脚本，亲手驱动 `WeightQuantizer` 的两步法，并对比对称/非对称的量化误差。

**操作步骤**（示例代码，可直接保存为 `try_weight_quantizer.py` 运行）：

```python
# 示例代码：手动驱动 WeightQuantizer
import torch
from lmdeploy.lite.quantization import WeightQuantizer

torch.manual_seed(0)
w = torch.randn(128, 256)          # 模拟一个 Linear 的权重 (out, in)

for symmetry in [True, False]:
    q = WeightQuantizer(bits=4, symmetry=symmetry,
                        granularity='per_group', group_size=64)
    qparams = q.calculate_qparams(w)              # 步骤一：定标
    fake = q.quant(w, qparams, real=False)         # 步骤二：伪量化
    real = q.quant(w, qparams, real=True)          # 真量化 → int32
    mse = ((fake - w) ** 2).mean()
    print(f"symmetry={symmetry}: scales.shape={tuple(qparams.scales.shape)}, "
          f"zero_points={'None' if qparams.zero_points is None else qparams.zero_points.shape}, "
          f"dtype(real)={real.dtype}, MSE={mse.item():.6f}")
```

**需要观察的现象**：
- `scales.shape` 应为 `(128, 4, 1)`（128 输出通道，256/64=4 个 group，末维 1 用于广播）。
- 对称模式 `zero_points` 为 `None`，非对称模式为 `(128, 4, 1)`。
- `real=True` 的结果是 `torch.int32`。
- 非对称的 MSE 通常**略低于**对称（因为权重分布未必关于 0 对称）。

**预期结果**：4bit per-group 下，MSE 量级在 \(10^{-4}\sim10^{-3}\) 左右；非对称 MSE ≤ 对称 MSE。具体数值「待本地验证」（依赖随机种子与硬件浮点）。

#### 4.1.5 小练习与答案

**练习 1**：把上面脚本的 `granularity` 改成 `'per_tensor'`，`symmetry` 保持 `True`，`scales.shape` 会变成什么？为什么？

**答案**：变成 0 维标量 `()`（不是 `[1]`）。因为 per_tensor 让整个张量共用一组 scale，`cal_qparams_per_tensor_absmax` 用 `.abs().max()` 取全局最大值，结果是零维张量。

**练习 2**：`quant(..., real=True)` 返回 int32，但量化比特是 4——多出来的位数用来干什么？

**答案**：int32 只是「容器」，把多个 4bit 量化整数打包存进一个 32bit 字里（每个 int32 装 8 个 int4）。`real=True` 的语义是「给出量化后的整数值本身」，由下游 `WeightOnlyQLinear` 负责把它打包/重排（参见 u7-l2 的打包阶段）。

---

### 4.2 量化参数计算工具：cal_qparams 与 quant_utils

#### 4.2.1 概念说明

`WeightQuantizer` 只是个壳，真正算 scale/zero_point 的数学在 `cal_qparams.py` 里——六个函数，覆盖 {per_tensor, per_channel, per_group} × {absmax, minmax}。它们都是纯函数：输入权重张量 + bits（+ group_size），输出 `QParams`（一个 `NamedTuple(scales, zero_points)`）。

另一条路是 `quant_utils.py` 里的 **FP8 分块量化**（`quant_blocked_fp8`）。它与 `cal_qparams_*` 不是同一套：后者服务 INT4/INT8 的 weight-only 量化（AWQ/GPTQ/SmoothQuant），前者服务 **blocked FP8**（即 u5-l2 讲过的 `BlockedF8Linear` 离线侧）。两条路对应两种不同的权重数值格式，互不共用代码。

#### 4.2.2 核心流程

`cal_qparams_*` 的统一套路（以 per_group_minmax 为例）：

```
输入 w (out_c, in_c), bits, group_size
  ├─ 校验 in_c 能被 group_size 整除
  ├─ reshape 成 (out_c, in_c//group_size, group_size)
  ├─ 沿最后一维求 w_min, w_max（per-group 统计量）
  ├─ scale = (w_max - w_min) / (2**bits - 1)
  └─ zero_point = precise_round(-w_min / scale)
输出 QParams(scales=[out_c, n_groups, 1], zero_points=[out_c, n_groups, 1])
```

absmax 变体只把「求 min/max」换成「求绝对最大」，\(q_{\max}=2^{b-1}-1\)，且 `zero_points=None`。

FP8 分块量化的套路不同，它先把权重按 `block_size=128` 对齐、做「反像素重排」（unflatten），再**逐块**算 scale 并转成 fp8 dtype：

```
quant_blocked_fp8(w, fp8_dtype, block_size=128, scale_fmt=None)
  ├─ 对齐 K,N 到 128 倍数（不足补零）
  ├─ unflatten 成 (..., K//128, 128, N//128, 128)
  ├─ 对每个 128×128 块：scale = amax / fp8_max   （默认）
  │                         或 2^ceil(log2(amax/fp8_max))  （ue8m0，2 的幂次 scale）
  ├─ w_q = (w / scale).to(fp8_dtype)
  └─ 裁回原始 K,N
输出 (w_q: fp8, scaling: 每块一个 scale)
```

#### 4.2.3 源码精读

先看 `QParams` 容器与 `precise_round`——后者是量化专用的「向远离零方向舍入」：

[cal_qparams.py:7-16](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/utils/cal_qparams.py#L7-L16) — `QParams` 是 `NamedTuple(scales, zero_points)`；`precise_round(x) = x.sign() * (|x|+0.5).floor()`，对正负数都「五入」，与 `torch.round`（四舍六入五成双）不同。

以 per_group_absmax 看 per-group 定标的最小实现（对称，无 zero_point）：

[cal_qparams.py:57-75](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/utils/cal_qparams.py#L57-L75) — `cal_qparams_per_group_absmax`：`reshape(outc, -1, group_size)` → `.abs().max(dim=-1)` → `scales = absmax / (2**(n_bits-1) - 1)`，其中 `q_max = 2**(n_bits-1) - 1` 是有符号量化的范围上限。

对应的非对称版本多了 `zero_point` 计算：

[cal_qparams.py:78-101](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/utils/cal_qparams.py#L78-L101) — `cal_qparams_per_group_minmax`：`scale = (w_max - w_min) / (2**n_bits - 1)`，`zero_points = precise_round(-w_min / scales)`。对比 absmax 版的 \(q_{\max}=2^{b-1}-1\)，非对称用的是 \(2^b-1\)（无符号整范围）。

per_tensor 版的区别在于统计量是全局标量（`.max()` 不带 dim），并对 scale 做了 `clamp_(min=1e-5)` 防止除零：

[cal_qparams.py:104-136](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/utils/cal_qparams.py#L104-L136) — per_tensor 的 minmax/absmax：`scales` 为 0 维标量，minmax 版 `clamp_(min=1e-5)` 保护退化情况。

再看 FP8 那条路。`_aligned_size` 是向上取整到 block 的整数倍，几个 `fast_*` 函数用浮点数位操作实现「快速 log2/ pow2」，专供 `ue8m0` 这种「scale 必须是 2 的幂」的格式：

[quant_utils.py:7-27](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/weight/quant_utils.py#L7-L27) — `_aligned_size`（对齐）、`fast_log2_ceil_torch`/`fast_pow2_torch`/`fast_round_scale_torch`（位运算加速的 2 的幂次 scale 计算）。

FP8 定标策略在 `_get_quant_scaling` 里二选一：默认 `scale = amax / fp8_max`；`ue8m0` 则把 scale 取整成最近的 2 的幂（硬件友好的指数缩放）：

[quant_utils.py:30-44](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/weight/quant_utils.py#L30-L44) — `_get_quant_scaling`：`amax = weight.abs().amax(dim, keepdim=True).clamp_min(1e-6)`，按 `scale_fmt` 选线性 scale 或 2 的幂次 scale。

主函数 `quant_blocked_fp8` 把上面的积木串起来：对齐补零 → unflatten 分块 → 逐块定标 → 转 fp8 → 裁回原尺寸：

[quant_utils.py:47-82](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/weight/quant_utils.py#L47-L82) — `quant_blocked_fp8`：返回 `(quantized_weight(fp8), scaling)`，scaling 形状按块数压扁。其运行时消费侧是 PyTorch 引擎里的 `BlockedF8Linear`/`FusedMoEBlockedF8`（见 u5-l2/u5-l3）。

#### 4.2.4 代码实践

**目标**：用仓库自带的单元测试，验证六种 `cal_qparams_*` 的 scale/zero_point 形状，从而把「粒度 × 对称」的二维表彻底记住。

**操作步骤**：

```bash
# 仓库已内置形状断言测试，直接跑
pytest tests/test_lmdeploy/test_lite/test_quantization/test_utils/test_cal_qparams.py -v
```

读 [test_cal_qparams.py:15-49](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/tests/test_lmdeploy/test_lite/test_quantization/test_utils/test_cal_qparams.py#L15-L49) 里的断言，把结论填进下表：

| 函数 | bits | group_size | scales.shape | zero_points |
|---|---|---|---|---|
| per_channel_absmax | 8 | — | `(64,1)` | None |
| per_channel_minmax | 8 | — | `(64,1)` | `(64,1)` |
| per_group_absmax | 8 | 16 | `(64,4,1)` | None |
| per_group_minmax | 8 | 16 | `(64,4,1)` | `(64,4,1)` |
| per_tensor_absmax | 8 | — | `()` | None |
| per_tensor_minmax | 8 | — | `()` | `()` |

**需要观察的现象**：六个断言全部通过；尤其注意 **per_tensor 的 scales.shape 是 `()` 而非 `[1]`**——这正好印证了 4.1.3 提到的 `quant.py` 注释瑕疵。

**预期结果**：测试通过（`1 passed`）。若环境无 torch 则「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 absmax（对称）的 `zero_points` 恒为 `None`，而 minmax（非对称）需要算 zero_point？

**答案**：对称量化以 0 为中心，浮点 0 直接映射到整数 0，无需额外偏移；非对称量化把 \([w_{\min},w_{\max}]\) 映射到 \([0, 2^b-1]\)，浮点 0 一般不对应整数 0，必须用 `zero_point` 记录 0 的整数位置，反量化时才能还原。

**练习 2**：`quant_blocked_fp8` 为什么要先把 K、N 对齐到 128 的倍数？

**答案**：分块量化的基本约束是每维都能被 `block_size` 整除，否则无法 `unflatten` 成 `(K//128, 128, N//128, 128)`。对齐补零保证可整除，量化完再 `[..., :K, :N]` 裁回原始尺寸，补零区不参与最终权重。

---

### 4.3 激活统计观察器 ActivationObserver

#### 4.3.1 概念说明

`ActivationObserver` 是「**被动统计器**」：你把它挂到某个 Linear 的输入或输出上，它在前向传播流经时**逐通道**记录这段激活的 `max/min/mean/absmax/absmean`，并维护「已观测 batch 数」。它本身不修改张量、不改变计算图，纯靠 `@torch.no_grad()` 旁路统计。

它的设计要点有三个：

1. **逐通道（per-channel）累积**：统计量是与激活最后一维同长的向量，不是标量。这一点至关重要——AWQ 算平滑因子 \(s_j\) 需要的是每个输入通道 \(j\) 的 \(|x_j|\) 统计，正是这里的 `absmax`/`absmean`。
2. **运行最大/最小 + 运行均值**：`max/min/absmax` 用逐元素 `maximum/minimum` 取「历次之最」；`mean/absmean` 用「按 batch 数加权」做运行平均。
3. **`GlobalAvailMixin` 全局注册**：observer 把自己按 `(group, key)` 存进类级字典，forward hook 凭模块名反查到对应 observer——这样 hook 闭包就不必捕获 observer 引用，注册表是观察器与 hook 之间的「电话簿」。

`observer.py` 里还有一个 `KVCacheObserver`，做的是同一件事，但面向 **4D 的 KV cache 张量**（自动识别 `(bs,seqlen,heads,dims)` vs `(bs,heads,seqlen,dims)` 两种布局），只记 `max/min/absmax`，服务于 KV cache 量化（即 u2-l3 的 `QuantPolicy`）。它和 `ActivationObserver` 一起被 export 在 `lmdeploy.lite.quantization.__all__` 里。

#### 4.3.2 核心流程

`ActivationObserver` 在校准链路里的完整生命周期：

```
CalibrationContext 初始化阶段
  ├─ 对每个目标 Linear：
  │    obs = ActivationObserver(weight.size(-1))   # 输入 observer，dim=in_features
  │    obs.global_available(name, group=inp_obs_group)   # 注册进全局表
  ├─ 注册 forward_pre_hook：每次前向前，obs.observe(inp[0])
  │
校准前向（喂校准数据）阶段
  └─ 每个 batch 流经时，observe(x) 更新 max/min/mean/absmax/absmean
     └─ 类方法 disable()/enable() 可在 search_scale 时临时关掉，避免重复统计
  │
收集阶段
  ├─ ActivationObserver.find_group(inp_obs_group) 拿回全部 observer
  └─ 读 obs.absmax_val / obs.absmean_val → 装进 inputs_stats 字典 → 导出 pth
```

AWQ 的平滑因子用到的是这里的 `absmax`（默认 search_scale=False 路径）或 `absmean + ratio`（search_scale=True 路径，`ratio` 由 `save_ratio` 逐层记录），完美对应 u7-l2 的结论。

#### 4.3.3 源码精读

`ActivationObserver` 的状态字段——五条 per-channel 统计量加一个 batch 计数器，全部预分配为 `float16`：

[observer.py:53-76](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/activation/observer.py#L53-L76) — `ActivationObserver.__init__`：`max_val/min_val/absmax_val/absmean_val/mean_val` 全部 `torch.full((dim,), ...)`，`num_batches_tracked=0`，另有 `value/ratio/num_ratio_tracked` 给 search_scale 用。

`disable/enable` 是类方法，翻一个类级布尔 `observed`——这是 AWQ 网格搜索 `ratio` 时的优化：搜索过程会多次重放前向，已统计好的激活无需重算：

[observer.py:78-86](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/activation/observer.py#L78-L86) — `disable/enable`：`cls.observed = True/False`。

`observe` 是统计心脏，把 4 个关键动作挤在一个函数里：

[observer.py:88-124](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/activation/observer.py#L88-L124) — `observe`：① 断言无 NaN、若 `observed` 则直接返回；② `flatten(0,1)` 把前两维合并成 batch，对最后一维（通道）求 `max/min/mean/absmax/absmean`；③ `max/min/absmax` 用 `torch.maximum/minimum` 取历次之最；④ `mean/absmean` 用「旧值×已计 batch + 新值」除以「batch+1」做运行平均；⑤ `num_batches_tracked += 1`。

> 注意 L101-L102 的早退：若 `flatten` 后某维为 0（空 batch），直接返回，避免对空张量求 max 报错。

`save_ratio` 是给 AWQ search_scale 用的副统计量——逐层记录搜索出的 `ratio` 并做运行平均：

[observer.py:126-131](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/activation/observer.py#L126-L131) — `save_ratio`：`ratio` 的运行平均，分母是 `num_ratio_tracked`。

`KVCacheObserver` 的差别只在布局自动识别——它会按 `(num_head, head_dim)` 试着匹配第 2/3 维，决定要不要 `transpose(1,2)`：

[observer.py:8-50](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/activation/observer.py#L8-L50) — `KVCacheObserver`：4D 张量布局自动识别 + 仅记 `max/min/absmax`，服务于 KV cache int8 量化。

再看上游怎么用。`CalibrationContext` 初始化时按 Linear 的 `weight.size(-1)`（输入）和 `weight.size(0)`（输出）建 observer，并 `global_available` 注册：

[calibration.py:102-112](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/calibration.py#L102-L112) — `_init_input_observers`/`_init_output_observers`：分别用 `weight.size(-1)` / `weight.size(0)` 构造 observer，注册到 `inp_obs_group` / `out_obs_group`。

hook 函数本身只做「按名查 observer → 调 observe」——这正是 `GlobalAvailMixin` 的用武之地：

[calibration.py:114-129](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/calibration.py#L114-L129) — `_insert_input_observers`：`register_forward_pre_hook`，hook 里 `ActivationObserver.find(m_name, group=inp_obs_group).observe(inp[0])`。

最后收集阶段，遍历 group 把每个 observer 的 `absmax_val/absmean_val` 拽出来装进字典——这就是导出到 `inputs_stats.pth` 的数据来源：

[calibration.py:178-191](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/calibration.py#L178-L191) — `collect_inputs_stats`：读 `obs.max_val/min_val/mean_val/absmax_val/absmean_val`，组装成 5 键字典返回。

#### 4.3.4 代码实践

**目标**：脱离大模型，用一段随机激活亲手驱动 `ActivationObserver`，看清「逐通道运行统计」是如何随 batch 累积的，并体会 `GlobalAvailMixin` 的「注册—查找」机制。

**操作步骤**（示例代码）：

```python
# 示例代码：手动驱动 ActivationObserver
import torch
from lmdeploy.lite.quantization.activation import ActivationObserver

dim = 64
obs = ActivationObserver(dim)
obs.global_available('fc1', group='inputs')   # 注册进全局表

# 模拟 3 个 batch 的前向激活（形状 (tokens, dim)）
for b in range(3):
    x = torch.randn(32, dim) * (b + 1)         # 逐 batch 尺度递增
    obs.observe(x)

# 从全局表把它找回来（模拟 hook 里的反查）
found = ActivationObserver.find('fc1', group='inputs')
print("num_batches_tracked =", found.num_batches_tracked)
print("absmax_val[:5] =", found.absmax_val[:5].tolist())
print("absmean_val[:5] =", found.absmean_val[:5].tolist())
print("max_val >= min_val ?", bool((found.max_val >= found.min_val).all()))
```

**需要观察的现象**：
- `num_batches_tracked == 3`。
- `absmax_val` 反映第 3 个 batch（尺度 ×3）带来的最大幅值，应明显大于第 1 个 batch。
- `max_val` 全程 ≥ `min_val`（逐元素比较的合理性自检）。
- `find('fc1', group='inputs')` 拿回的就是 `obs` 本身——验证了 `GlobalAvailMixin` 的注册—查找闭环。

**预期结果**：上述断言全部成立。具体数值「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`observe` 里 `max_val` 用 `torch.maximum` 更新，而 `mean_val` 用「加权除法」更新——为什么不用同一种方式？

**答案**：`max` 是「可分解」的累积量（历次最大 = max(当前最大, 新值)），直接逐元素取大即可；而 `mean` 不是可分解的——全局均值必须知道「总样本数」与「累计和」，故用 `旧均值×已计batch + 新均值)/(batch+1)` 做运行平均（这里隐含假设每 batch 样本数相同）。

**练习 2**：AWQ 算平滑因子时，默认路径用 `absmax`，开启 `search_scale` 才用 `absmean + ratio`。在 `ActivationObserver` 里，这两个量分别对应哪个字段、由哪段代码维护？

**答案**：`absmax` 对应 `absmax_val`（`observe` 里 `cur_absmax = cur_val.abs().max(0)` + `torch.maximum` 更新）；`absmean` 对应 `absmean_val`（`cur_absmean = cur_val.abs().mean(0)` + 运行平均更新），`ratio` 由 `save_ratio` 单独维护。这正是 u7-l1「absmax 供 SmoothQuant、absmean 供 AWQ」结论的代码落点。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来，写一个 30 行的脚本，复现「激活统计 → 据此选择量化策略 → 量化权重 → 评估误差」的迷你闭环，从而直观感受这些原语如何被上游编排函数（AWQ/SmoothQuant）组合使用。

**要求**：

1. 构造一个 `Linear(in=256, out=128)` 的随机权重 `W`，并造一段含「离群通道」的随机激活 `x`（例如把第 10 个通道的幅值放大 10 倍——模拟 AWQ 关心的 outlier）。
2. 用 `ActivationObserver(256)` 统计 `x` 的 `absmax_val`，打印离群通道（第 10 通道）是否显著大于其它通道。
3. 对 `W` 分别用 `WeightQuantizer(4, symmetry=False, 'per_group', 64)` 和 `(4, symmetry=True, 'per_group', 64)` 做伪量化，计算两种的 MSE，验证 4.1.4 的结论。
4. 进一步思考（不必编码）：若按 u7-l2 的 AWQ 做法，用第 2 步的 `absmax` 算平滑因子 \(s_j=|x_j|^\alpha/|W_j|^{1-\alpha}\) 先平滑 `W` 再量化，MSE 会如何变化？为什么？（这正是「激活感知」相对「裸量化」的收益所在。）

**检查点**：
- 第 2 步应观察到第 10 通道 `absmax` 远大于均值。
- 第 3 步非对称 MSE ≤ 对称 MSE。
- 第 4 步：平滑后量化误差应进一步降低（离群通道的量化负担被分摊到 `W` 的对应行）。此为分析题，结论「待本地验证」可结合 u7-l2 的恒等变换 \(y=Wx=(W\cdot\text{diag}(s))(\text{diag}(s)^{-1}x)\) 说明。

> 这个综合实践不需要 GPU、不需要真实模型，只需 `torch` + `lmdeploy.lite` 的 import，能在任何装好 lmdeploy（哪怕 `DISABLE_TURBOMIND=1`）的环境里跑通。

## 6. 本讲小结

- `WeightQuantizer` 是「**配置对象 + `CAL_FUNC_MAP` 派发表 + `calculate_qparams`/`quant` 两步法**」的统一权重量化入口；`bits/symmetry/granularity` 三组旋钮决定量化行为，构造期即断言非法组合。
- 真正的定标数学在 `cal_qparams.py` 的六个纯函数里（{per_tensor/per_channel/per_group} × {absmax/minmax}）；对称用 \(q_{\max}=2^{b-1}-1\)、无 zero_point，非对称用 \(2^b-1\)、带 zero_point；`precise_round` 是「向远离零方向舍入」。
- `quant_utils.py` 是**另一条 FP8 分块量化**路径（`quant_blocked_fp8`，block_size=128，可选 `ue8m0` 的 2 的幂次 scale），服务 `BlockedF8Linear`，与 INT4/INT8 的 `cal_qparams_*` 不共用代码。
- `ActivationObserver` 是「被动逐通道统计器」，靠 `forward hook` 在不改模型代码的前提下累积 `max/min/mean/absmax/absmean`；`max` 类用运行最值、`mean` 类用运行平均；`disable/enable` 供 search_scale 时跳过重复统计。
- `GlobalAvailMixin` 是 observer 与 hook 之间的「全局电话簿」，让 hook 凭模块名反查 observer；`KVCacheObserver` 是其 4D KV cache 版本，服务 KV cache 量化。
- 上下游关系：`CalibrationContext` 用 observer 产出 `inputs_stats.pth`（u7-l1），`WeightQuantizer`/`pseudo_quantize_tensor` 服务 AWQ 打包（u7-l2）与 SmoothQuant（u7-l3）——本讲是整条量化链的**最底层弹药库**。

## 7. 下一步学习建议

- **横向收口量化单元**：回到 u7-l2/u7-l3，重读 `smooth_fc_fcs`/`pseudo_quantize_tensor`/`WeightOnlyQLinear.from_linear`，此时你应能逐行解释每一步用到了本讲的哪个原语。
- **纵向跨到运行时**：本讲的 FP8 工具产出的权重，最终被 PyTorch 引擎的 `lmdeploy/pytorch/nn/linear/blocked_fp8.py` 与 `nn/moe/blocked_fp8.py` 消费（见 u5-l2/u5-l3）。建议接着读 u5-l2 的 `BlockedF8Linear`，看「离线量化产出的 scale」如何被「在线反量化 kernel」用上，闭合量化从离线到在线的完整环。
- **若关注 KV cache 量化**：可结合 u2-l3 的 `QuantPolicy`，去 `lmdeploy/pytorch/kernels/cuda/fill_kv_cache.py` 看 `KVCacheObserver` 的统计量如何落到 KV cache int8 写入 kernel 上。
