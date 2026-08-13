# 量化数据类型与 export_deploy 落盘

## 1. 本讲目标

本讲打开上一讲 [u7-l1](u7-l1-quant-modules.md) 留下的「黑盒」：`WeightQuantizer` / `ActivationQuantizer` 里的 `quant_obj` 到底是什么、内部怎么把一个浮点张量变成低比特数据，以及它的 `export_deploy` 又如何产出部署 payload。

学完本讲，你应当能够：

- 说清 `DTYPE_REGISTRY` 如何登记 `int`/`mxfp`/`hifp` 三类量化数据类型，以及三者共享的 `forward`/`fake_quant`/`export_deploy` 接口契约。
- 读懂 `int_impl` 里 `weight_quant`（per-channel 权重量化）与 `dynamic_per_token_quant`（per-token 激活量化）的实现，以及 4-bit 的打包（pack）逻辑。
- 看懂 `QuantDequantMx` 的 Microscaling「共享指数」量化，以及它 `deploy` 落盘时产出的 `e8m0` scale 与 `float8_e4m3fn` / 打包 `uint8` 权重。
- 把 [u4-l4](u4-l4-deploy-export.md) 里 deploy 阶段写进 safetensors 的 `.weight` / `.weight_scale` / `.weight_bias` 等 key，与本讲 payload 字典的命名一一对应起来。

## 2. 前置知识

本讲默认你已经读过 [u7-l1](u7-l1-quant-modules.md) 和 [u4-l4](u4-l4-deploy-export.md)。回顾两个关键结论：

1. **`quant_obj` 是数据类型对象**：`WeightQuantizer` / `ActivationQuantizer` 在构造时执行 `self.quant_obj = DTYPE_REGISTRY.get(args.quant_dtype)(bits=...)`（见 [quant_base.py:90](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L90) 与 [quant_base.py:129](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L129)）。u7-l1 把它当成一个「会 `forward` / `export_deploy` 的黑盒」，本讲就拆开这个黑盒。
2. **deploy 阶段消费 payload**：[u4-l4](u4-l4-deploy-export.md) 里 `export_block_deploy` 取 `payload["qweight"]` 写到 `.weight`，把其它字段写到 `.weight_xxx`。本讲回答：这个 payload 字典是**谁产生、字段叫什么、值是什么类型**。

再回顾三个基础概念（详见 [u2-l2](u2-l2-quant-dtypes-overview.md)）：

- **伪量化（fake_quant）**：量化再反量化，输出仍是浮点张量，用于训练/评测阶段近似量化误差。
- **真量化（real quant / deploy）**：输出真正的低比特整数或低比特浮点（如 `int8`、`float8_e4m3fn`），用于部署落盘。
- **per-channel / per-token / per-group**：量化粒度。per-channel 按权重输出通道一个 scale；per-token 按激活每行（每个 token）一个 scale；per-group 每 32 个元素共享一个 scale（MXFP 的做法）。

一个贯穿全讲的术语：**payload**——`export_deploy` 返回的字典，形如 `{"qweight": ..., "weight_scale": ..., "weight_bias": ...}`，是数据类型层交给 deploy 阶段写盘的「打包件」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [amct_pytorch/quantization/dtypes/__init__.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/__init__.py) | 定义 `DTYPE_REGISTRY` 与惰性注册函数 `register_dtype()`，是三类数据类型的登记处。 |
| [amct_pytorch/quantization/dtypes/int.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/int.py) | `QuantDequantInt`：整数（INT8/INT4）量化反量化类，含 `fake_quant` 与 `export_deploy`。 |
| [amct_pytorch/quantization/dtypes/int_impl.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/int_impl.py) | 整数族的无状态算子：`weight_quant`、`dynamic_per_token_quant`、`pack_4bit`、`scale_fp32_to_u64` 等。 |
| [amct_pytorch/quantization/dtypes/mxfp.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/mxfp.py) | `QuantDequantMx`：Microscaling（MXFP8/MXFP4）量化类，含共享指数 `quant` 与 `deploy` 落盘。 |
| [amct_pytorch/quantization/dtypes/mxfp_impl.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/mxfp_impl.py) | Microscaling 底层算子：`shared_exponents`、`quantize_elewise`、`weight_dequant`、`pack_uint4` 等。 |
| [amct_pytorch/quantization/dtypes/hifp.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/hifp.py) | `QuantDequantHifp`：HiFloat8 量化类，目前只支持伪量化，`export_deploy` 尚未实现。 |
| [amct_pytorch/quantization/modules/quant_base.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py) | `WeightQuantizer.export_deploy`：先跑算法再调 `quant_obj.export_deploy`，是 payload 的入口。 |
| [amct_pytorch/quantization/modules/quant_linear.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_linear.py) | `QuantLinear.export_deploy`：取出权重（含结构变换）后转发给 `WeightQuantizer.export_deploy`。 |
| [amct_pytorch/common/models/llm/common/deploy_export.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py) | `export_block_deploy` / `quant_payload`：把 payload 字典按命名规则展开成 safetensors 的 key。 |

## 4. 核心概念与源码讲解

### 4.1 DTYPE_REGISTRY 与三类量化数据类型的统一契约

#### 4.1.1 概念说明

AMCT 支持三族低比特数据类型：整数 `int`、Microscaling 浮点 `mxfp`、华为自研 `hifp`（HiFloat8）。它们形态各异——整数是均匀格点、MXFP 是共享指数浮点、HiFloat8 是带 Dot 位的锥形浮点——但 AMCT 让它们实现**同一个接口**，这样上层 `WeightQuantizer` / `ActivationQuantizer` 不用关心你选了哪种 `--quant_dtype`，统一用 `quant_obj(x)` 做伪量化、用 `quant_obj.export_deploy(x)` 做落盘。

实现这个「多态」靠的是注册表：`DTYPE_REGISTRY` 把名字（`"int"`/`"mxfp"`/`"hifp"`）映射到具体类，上层用 `DTYPE_REGISTRY.get(args.quant_dtype)(bits=...)` 拿到一个实例。这套机制与 [u3-l3](u3-l3-registry-architecture.md) 讲的四大注册表同构，`DTYPE_REGISTRY` 正是其中之一。

#### 4.1.2 核心流程

三类数据类型都遵循同一个三方法契约：

```text
forward(x, v)          # 训练/评测入口：observe 态或 16-bit 时透传，否则 fake_quant
  └── fake_quant(x, v) # 伪量化：输出仍是浮点张量（quant → dequant 回到原 dtype）

export_deploy(x, v)    # 部署入口：输出真低比特 payload 字典 {qweight, weight_scale, ...}
```

三个关键开关 / 行为：

- `is_observe`：为 `True` 时 `forward` 直接返回 `x`（校准态透明），与 [u6-l1](u6-l1-algo-base-observe.md) 讲的 observe 通路一致。
- `bits == 16`：`forward` 透传——16 表示「不量化」（见 [u3-l4](u3-l4-bit-policy-config.md) 的位宽约定）。
- `is_act`：仅 `QuantDequantInt` 用到，区分走激活通路还是权重通路。

#### 4.1.3 源码精读

注册表本体与惰性注册函数：

[DTYPE_REGISTRY 定义与 register_dtype](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/__init__.py#L22-L37)——`register_dtype()` 靠 import 副作用触发三个类上的 `@DTYPE_REGISTRY.register(...)` 装饰器完成登记，`_REGISTERED` 做幂等保护，由 workflow 的 `setup()` 调用（见 [llm_deploy.py:71](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L71)）。

三个类用完全相同的姿势登记：

- [QuantDequantInt 注册](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/int.py#L29-L30)：`name="int"`
- [QuantDequantMx 注册](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/mxfp.py#L31-L32)：`name="mxfp"`
- [QuantDequantHifp 注册](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/hifp.py#L27-L28)：`name="hifp"`

三者的 `forward` 几乎一模一样，以 int 为例：

[QuantDequantInt.forward](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/int.py#L45-L48)：observe 或 16-bit 透传，否则 `fake_quant`。`mxfp`（[mxfp.py:65-68](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/mxfp.py#L65-L68)）与 `hifp`（[hifp.py:43-51](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/hifp.py#L43-L51)）结构相同。这就是「统一契约」的体现。

> 一个值得注意的差异：`hifp` 的 `export_deploy` 与 `deploy` 都直接 `raise NotImplementedError`（[hifp.py:53-59](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/hifp.py#L53-L59)）。也就是说 HiFloat8 目前只能伪量化（训练/评测），还不能烘焙成部署 payload。这也是 `test_hifp_deploy_and_export_deploy_are_unsupported` 断言的内容。

#### 4.1.4 代码实践

**实践目标**：验证三类数据类型都登记在 `DTYPE_REGISTRY` 下，且都遵守「observe 态原样返回」的契约。

**操作步骤**（源码阅读型，结合 [test_dtypes.py:81-97](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/tests/unit_test/quantization/test_dtypes.py#L81-L97)）：

1. 阅读上面的注册链接，确认 `DTYPE_REGISTRY.get("int") / get("mxfp") / get("hifp")` 分别返回三个类。
2. 阅读测试 `test_dtype_observe_returns_input_object_unchanged`：它对三个类各构造一个 `bits=8` 对象，置 `is_observe = True`，断言 `out is x`（不仅是相等，而是同一个对象）。

**需要观察的现象**：当 `is_observe=True` 时，三类 `forward` 都直接 `return x`，输入对象原封不动地返回——这是校准阶段激活能「透明穿过」的根源。

**预期结果**：`DTYPE_REGISTRY.get("hif")` 会抛 `KeyError`（注册名是 `hifp` 不是 `hif`，见 [test_dtypes.py:372-375](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/test_dtypes.py#L372-L375)）。

#### 4.1.5 小练习与答案

**练习 1**：为什么三类数据类型的 `forward` 都要在开头判断 `self.is_observe`，而不是由上层 `WeightQuantizer` 统一判断？

**参考答案**：因为 `quant_obj` 是被 `WeightQuantizer.fake_quant` 直接调用的最内层（[quant_base.py:165-168](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L165-L168)）。校准态需要保证量化「完全不发生」，所以在最贴近量化的位置兜一道，避免上层某条路径漏判导致校准数据被污染。

**练习 2**：如果新增一种数据类型（比如 `bf8`），需要改动哪些地方？

**参考答案**：写一个 `QuantDequantBf8` 类实现 `forward`/`fake_quant`/`export_deploy`，用 `@DTYPE_REGISTRY.register(name="bf8", ...)` 装饰，再在 `register_dtype()` 里加一行 `from .bf8 import QuantDequantBf8  # noqa: F401`。上层 `WeightQuantizer` / deploy 流程不用改——这就是注册表多态的好处。

---

### 4.2 QuantDequantInt 与 int_impl：per-channel 权重 / per-token 激活

#### 4.2.1 概念说明

`QuantDequantInt` 是整数族（INT8/INT4）的数据类型类。它的核心思想是最朴素的「对称均匀量化」：找一个 scale，把浮点值除以 scale、四舍五入到整数、再截断（clip）到 \([-qmax, qmax]\)；反量化时乘回 scale。

权重和激活走**两种粒度**：

- **权重用 per-channel**：每个输出通道（行）一个 scale。因为权重是静态的、离线量化，通道间幅值差异大，逐通道缩放更精确。
- **激活用 per-token**：每行（每个 token）一个 scale。因为激活动态变化、按 token 算 scale 才能跟得上。

`int_impl` 是一组**无状态函数**（不持有任何参数），承担真正的运算；`QuantDequantInt` 只是把这些函数按 `is_act` 分发一下。这种「类做分发、函数做实现」的拆分让底层算子可独立测试。

#### 4.2.2 核心流程

权重量化的数学（per-channel，对每一行 \(i\)）：

\[
qmax = 2^{bits-1}-1,\qquad scale_i = \frac{\max_j |x_{ij}|}{qmax}
\]

\[
q_{ij} = \mathrm{clip}\left(\mathrm{round\_ste}\left(\frac{x_{ij}}{scale_i} + v_{ij}\right),\ -qmax,\ qmax\right)
\]

其中 \(v\) 是**舍入偏移**（rounding offset），用于 AutoRound 一类算法学习更优的舍入方向（详见 [u6-l4](u6-l4-learnable-algos.md)）。`round_ste` 是直通估计器（STE）：前向取 round，反向把梯度直通过去，保证量化这一步可导。

伪量化反量化（`real_quant=False`）：

\[
\hat{x}_{ij} = q_{ij} \cdot scale_i
\]

真量化（`real_quant=True`）则把 \(q\) 转成 `int8` 返回，并把 `scale`、`bias` 一起交给调用方——这就是 deploy payload 的来源（4.4 节详述）。

激活量化（per-token）几乎一样，只是 `scale` 沿最后一维（每个 token）算，且不使用偏移 \(v\)。

```text
QuantDequantInt.fake_quant(x, v)
 ├── is_act=True  → dynamic_per_token_quant(x, bits)        # per-token，无 v
 └── is_act=False → weight_quant(x, bits, v=v)              # per-channel，带 v
```

#### 4.2.3 源码精读

[QuantDequantInt.fake_quant](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/int.py#L38-L43) 就是上面流程图里那个二分发的实现，注意激活分支不传 `v`，权重分支传 `v`。

权重量化的核心实现：

[weight_quant](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/int_impl.py#L72-L93)——关键几句：

```python
abs_max = torch.abs(tensor).max(dim=1, keepdim=True)[0]   # 每行一个 max → per-channel
scale = (abs_max / qmax).clamp_min(1e-9)
quantized = round_ste(tensor / scale + v)                  # STE + 舍入偏移
quantized = torch.clamp(quantized, -qmax, qmax)
if real_quant:
    ...   # 见下文 4.4 节
else:
    return quantized * scale                                # 伪量化：乘回 scale
```

激活量化：

[dynamic_per_token_quant](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/int_impl.py#L26-L35)——结构与 `weight_quant` 几乎一致，差别是 `abs_max` 沿 `dim=-1`（每个 token）算，并 `.clamp(min=1e-5)` 防 0；没有 `v`，也没有 `real_quant` 分支（激活只在运行时动态量化、不落盘）。

STE 的实现：

[round_ste](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/int_impl.py#L22-L23)——`(torch.round(x) - x).detach() + x`。前向等于 `round(x)`，反向对 `x` 求导为 1（因为 `.detach()` 切断了 round 部分的梯度）。这就是 [test_int_quant_passes_gradient_through_ste](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/test_dtypes.py#L200-L208) 断言梯度为 1 的来源。

#### 4.2.4 代码实践

**实践目标**：亲手验证舍入偏移 `v` 如何改变量化结果。

**操作步骤**（结合 [test_int_export_deploy_uses_rounding_offset_when_provided](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/test_dtypes.py#L181-L190)）：

1. 取一行权重 `x = [[0.0, 0.49, 1.0]]`，`bits=8`，则该行 `abs_max=1.0`，`qmax=127`，`scale = 1/127`。
2. 不加偏移：`round_ste(0.49 / (1/127)) = round(62.23) = 62`。
3. 加偏移 `v = [[0.0, 0.6, 0.0]]`：`round_ste(0.49/(1/127) + 0.6) = round(62.83) = 63`。

**需要观察的现象**：`v` 把 `0.49` 这个值的舍入结果从 62 推到了 63——它微调了「四舍五入」的判决边界。AutoRound 一类算法正是通过学习每个元素的 `v`，让量化误差更小。

**预期结果**：与测试断言一致，`without_v[0,1] == 62`、`with_v[0,1] == 63`。可以本地运行 `pytest tests/unit_test/quantization/test_dtypes.py::test_int_export_deploy_uses_rounding_offset_when_provided -q` 验证；若环境跑不起来则「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么权重量化用 per-channel（`dim=1`），而激活用 per-token（`dim=-1`）？

**参考答案**：权重是静态的、形状为 `[out_features, in_features]`，每行（输出通道）幅值差异大，按行算 scale 能为每个通道量身定制，精度更高；激活动态变化、按 token（每个序列位置）算 scale 才能在运行时实时跟上分布变化，且 per-token 是 W8A8 / W4A8 全量化时硬件 GEMM kernel 约定的粒度。

**练习 2**：`round_ste` 为什么不能直接写成 `torch.round(x)`？

**参考答案**：`torch.round` 不可导（梯度为 0），直接用会导致反向传播时量化这一步阻断梯度，算法参数学不动。STE 的写法让前向仍是 round、反向却放行恒定梯度 1，使整条量化链路可微，这是 PTQ 可学习算法（LWC/LAC/AutoRound）能训练的前提。

---

### 4.3 QuantDequantMx：Microscaling 共享指数与 deploy 落盘

#### 4.3.1 概念说明

`QuantDequantMx` 是 Microscaling 浮点族（MXFP8/MXFP4）。与整数族「逐通道一个 scale」不同，MXFP 的做法是：沿最后一维每 **32 个元素**划成一块，块内所有元素**共享一个指数**（shared exponent，存储为 8-bit `e8m0`），每个元素自己只保留一个小尺寸浮点尾数（MXFP8 用 `e4m3`，MXFP4 用 `e2m1`）。这样用 1 字节指数摊到 32 个元素上，省存储又比纯整数精度高，还有专门硬件加速（详见 [u2-l2](u2-l2-quant-dtypes-overview.md)）。

它的 `forward`/`fake_quant` 与 int 族同形，但实现完全不同：要走「分块 → 求共享指数 → 缩放 → 逐元素量化到浮点格式」四步。

#### 4.3.2 核心流程

```text
quant(x, qdim=-1, v)
 ├── x.unflatten(qdim, (-1, 32))        # 把最后一维拆成 (..., n_blocks, 32)
 ├── e8m0 = shared_exponents(x, emax)   # 每块一个共享指数，形状 (..., 1)
 ├── x = x / 2**e8m0                    # 用共享指数把块内值缩放到目标格式量级
 └── ex_mx = quantize_elewise(x, ...)   # 逐元素量化到 e4m3 / e2m1 网格

fake_quant：dx = (2**e8m0) * ex_mx      # 反量化（乘回共享指数），flatten 还原维度
```

参数 `emax` / `max_norm` / `shift_val` / `min_exp` 由 `_get_format_params` 按位宽给出（8-bit 用 e4m3 的一组、4-bit 用 e2m1 的一组）：

[_get_format_params](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/mxfp.py#L81-L99)——8-bit: `emax=8, max_norm=448.0`（e4m3 最大正规数 448）；4-bit: `emax=2, max_norm=6.0`（e2m1 最大正规数 6）。

共享指数的选取（直觉）：取块内绝对值最大者，圆整到最近的 2 的幂，再减去 `emax` 给尾数格式留出「头部空间」，最后截断到 \([-127, ...]\) 以塞进 8-bit。

#### 4.3.3 源码精读

[QuantDequantMx.quant](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/mxfp.py#L70-L79)——四步主流程的入口，注意 `block_size = 32` 写死在 `__init__`（[mxfp.py:39](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/mxfp.py#L39)），所以 [test_mxfp_block_size_must_divide_last_dim](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/test_dtypes.py#L135-L139) 要求最后一维必须被 32 整除，否则 `unflatten` 报 `RuntimeError`。

[shared_exponents](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/mxfp_impl.py#L187-L193)——取块内 `max(|x|)`，经 `round_to_decimal` 圆整到最近的「2 的幂量级」，减 `emax`，截断。

[quantize_elewise](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/mxfp_impl.py#L201-L208)——逐元素把值映射到目标浮点格式的格点，同样用 `round_ste` 保证可微。

**deploy 落盘**是本族最值得看的地方：

[QuantDequantMx.deploy](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/mxfp.py#L41-L50)——把 `quant` 的两个产物（共享指数 `e8m0`、尾数 `ex_mx`）转成可落盘的低比特类型：

```python
e8m0 = (e8m0 + 127).to(torch.uint8).squeeze(-1).cpu()   # 偏移到无符号 8-bit
ex_mx = ex_mx.flatten(qdim - 1, qdim).cpu()
if self.bits == 8:
    ex_mx = ex_mx.to(torch.float8_e4m3fn)               # MXFP8：原生 FP8 类型
else:
    ex_mx = f32_to_f4_unpacked(ex_mx.float().cpu())     # MXFP4：先转 f4
    ex_mx = pack_uint4(ex_mx)                            # 再两个 f4 打包进一个字节
return ex_mx, e8m0
```

[QuantDequantMx.export_deploy](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/mxfp.py#L52-L57)——把 `deploy` 的返回值包成统一 payload：`{"qweight": ex_mx, "weight_scale": e8m0}`。

> 注意 MXFP 的 `weight_scale` 是 **uint8**（共享指数），而 INT 的 `weight_scale` 是 **float32**（per-channel scale）。同样是 `weight_scale` 这个 key，落盘的语义和类型随数据类型不同——这是 deploy 阶段 `is_mx` 总开关要特别区分的根本原因（见 [u4-l4](u4-l4-deploy-export.md) 与 4.4 节）。

#### 4.3.4 代码实践

**实践目标**：观察 MXFP8 与 MXFP4 deploy 产物的 dtype 差异。

**操作步骤**（结合 [TestMxfpDeploy](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/test_dtypes.py#L303-L323)）：

1. 构造 `x = torch.randn(4, 64)`，分别用 `QuantDequantMx(bits=8)` 和 `QuantDequantMx(bits=4)` 调 `deploy(x)`。
2. 对 8-bit：断言 `ex_mx.dtype == torch.float8_e4m3fn`，`e8m0.dtype == torch.uint8`。
3. 对 4-bit：断言 `ex_mx.dtype == torch.uint8`（两个 fp4 打包成一个字节），`e8m0.dtype == torch.uint8`。

**需要观察的现象**：MXFP8 的权重是 1 字节/元素的浮点；MXFP4 的权重是 0.5 字节/元素（打包），最后一维长度缩为一半。

**预期结果**：与测试断言一致。`pytest tests/unit_test/quantization/test_dtypes.py::TestMxfpDeploy -q` 可本地验证；环境不具备则「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：MXFP 的 `weight_scale` 为什么是 uint8，而 INT 的 `weight_scale` 是 float32？

**参考答案**：MXFP 用的是「共享指数」表示——每 32 个元素共享一个 8-bit 无符号指数 `e8m0`，它本身就是整数（存储时还加了 127 偏移落到 uint8），所以 scale 是 uint8；INT 用的是「逐通道浮点缩放因子」，是一个实数，所以是 float32。两者同名但语义完全不同。

**练习 2**：为什么 MXFP4 要 `pack_uint4`，而 MXFP8 不用打包？

**参考答案**：MXFP8 的尾数是 e4m3（8-bit），正好占一个字节，可直接用原生 `float8_e4m3fn` 类型存储；MXFP4 的尾数只有 4-bit，两个尾数拼起来才占一个字节，所以要用 `pack_uint4` 把两个 fp4 打包进一个 uint8，落盘后最后一维长度减半。

---

### 4.4 export_deploy 落盘链路：从 payload 到 safetensors key

#### 4.4.1 概念说明

前面三节都在讲「一个浮点张量怎么变成低比特 payload 字典」。本节回答最后一个问题：这个 payload 字典**怎么变成 safetensors 文件里的 `.weight` / `.weight_scale` / `.weight_bias` 等 key**。

整条链路是（block 粒度 deploy）：

```text
QuantLinear.export_deploy()                 # 取权重（含 structure_transform）
 └── WeightQuantizer.export_deploy(weight)  # 先跑算法（algo_forward），再交给 quant_obj
      └── quant_obj.export_deploy(weight)   # 数据类型层产出 payload 字典
           {"qweight": ..., "weight_scale": ..., "weight_bias": ...}
                │
                ▼
export_block_deploy(pipeline, layer_idx, ..)# 遍历每层每个 QuantLinear
 for weight_key, module in iter_deploy_bindings:
    payload = module.export_deploy()
    deploy_tensors[weight_key] = payload["qweight"]              # ...down_proj.weight
    for extra_name, extra_tensor in payload.items():
        extra_key = weight_key.replace(".weight", f".{extra_name}")  # ...down_proj.weight_scale
        deploy_tensors[extra_key] = extra_tensor
                │
                ▼
save_file(deploy_tensors, "layer_XXX.safetensors")   # 写盘（见 u4-l4）
```

核心是**命名映射规则**：payload 里的 `qweight` 直接顶替原始 `.weight` key；其它字段（`weight_scale`、`weight_bias`）则把 `.weight` 替换成 `.weight_<name>`；值为 `None` 的字段（如 INT8 没有.bias）被跳过。

#### 4.4.2 核心流程

payload 的字段构成取决于数据类型：

| 数据类型 | `qweight` | `weight_scale` | `weight_bias` |
| --- | --- | --- | --- |
| INT8 | `int8` | `float32` (per-channel) | `None`（跳过） |
| INT4 | `int8`（打包） | `uint64`（特殊编码） | `float32`（W4A8 MoEGMM 辅助） |
| MXFP8 | `float8_e4m3fn` | `uint8` (e8m0) | 无此字段 |
| MXFP4 | `uint8`（两个 fp4 打包） | `uint8` (e8m0) | 无此字段 |
| HiFloat8 | — | — | — (`export_deploy` 未实现) |

落到 safetensors 的 key（以 `...down_proj.weight` 为例）：

```text
INT8 : ...down_proj.weight (int8), ...down_proj.weight_scale (float32)
INT4 : ...down_proj.weight (int8 packed), ...down_proj.weight_scale (uint64), ...down_proj.weight_bias (float32)
MXFP8: ...down_proj.weight (float8_e4m3fn), ...down_proj.weight_scale (uint8)
```

#### 4.4.3 源码精读

**链路第一跳：QuantLinear → WeightQuantizer**

[QuantLinear.export_deploy](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_linear.py#L68-L73)——取出 `self.linear.weight`，若有结构变换（FlatQuant 一类，见 [u6-l4](u6-l4-learnable-algos.md)）则先用 `structure_transform(..., inv_t=True)` 把权重变换到位，再转发给 `weight_quantizer.export_deploy(weight)`。

**链路第二跳：WeightQuantizer → quant_obj**

[WeightQuantizer.export_deploy](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L149-L163)——分两种情况：

- 若有带 `quantize()` 钩子的权重算法（AutoRound），调它的 `export_deploy(x, self.quant_obj)`（钩子接管量化）。
- 否则直接调 `self.quant_obj.export_deploy(x)`（本讲重点）。注意会先 `getattr(..., "export_deploy", None)` 探测，不存在就抛 `NotImplementedError`——这正是 HiFloat8 会在此处报错的根源。

**链路第三跳：payload → safetensors key**

[export_block_deploy](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L136-L153)——命名映射的核心循环：

```python
for weight_key, module in pipeline.iter_deploy_bindings(layer_idx, block):
    payload = module.export_deploy()
    deploy_tensors[weight_key] = payload["qweight"]          # qweight → .weight
    tensor_routes[weight_key] = weight_key
    for extra_name, extra_tensor in payload.items():
        if extra_name == "qweight" or extra_tensor is None:  # 跳过 qweight 与 None
            continue
        extra_key = weight_key.replace(".weight", f".{extra_name}")  # .weight → .weight_scale
        deploy_tensors[extra_key] = extra_tensor
        tensor_routes[extra_key] = weight_key
```

`tensor_routes` 记录「每个 extra key 来自哪个原始 weight」（用于 `_collect_replaced_original_weights` 判断哪些原始权重已被替换、不应再写盘，详见 [u4-l4](u4-l4-deploy-export.md)）。

**INT 的真量化细节**：payload 里的 int8/uint64/bias 从哪来？答案在 `weight_quant(real_quant=True)`：

[weight_quant 的 real_quant 分支](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/int_impl.py#L82-L93)——

- `bits==8`：返回 `(int8 量化值, float32 scale, None)`。
- `bits==4`：走 W4A8 MoEGMM 专用路径——把量化值打包（[pack_4bit](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/int_impl.py#L53-L69)，两个 int4 打包进一字节），scale 转 uint64（[scale_fp32_to_u64](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/int_impl.py#L38-L50)），并算一个辅助 bias（[int4_assistance_bias](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/int_impl.py#L96-L104)）。

而 [QuantDequantInt.export_deploy](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/int.py#L50-L56) 把这三元组包成 `{"qweight", "weight_scale", "weight_bias"}`，且统一 `.detach().cpu()`——payload 落盘前必须搬到 CPU。

**tensor 粒度的等价路径**：当 `--granularity tensor` 时（不跑 PTQ，做纯格式转换），deploy 不走 `QuantLinear`，而是直接对原始权重调 `quant_payload`：

[quant_payload](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L187-L196)——与 `export_block_deploy` 用**完全相同的命名规则**，只是直接 `quant_cls(bits=bit).export_deploy(weight)`，不经过 WeightQuantizer 的算法环节。

#### 4.4.4 代码实践

**实践目标**：追踪一次 INT8 权重量化的完整落盘，讲清 payload 字段到 safetensors key 的命名映射。

**操作步骤**（源码阅读型，按链路自底向上读）：

1. **数据类型层**：读 [QuantDequantInt.export_deploy](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/int.py#L50-L56)。对一个 `bits=8` 的权重，它调 `weight_quant(x, 8, real_quant=True)`，返回三元组 `(qx:int8, scale:float32, None)`，包成 `{"qweight": int8, "weight_scale": float32, "weight_bias": None}`。
2. **量化器层**：读 [WeightQuantizer.export_deploy](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L149-L163)。无 `quantize()` 钩子算法时，它把这个字典原样向上返回。
3. **deploy 层**：读 [export_block_deploy](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L143-L152)。设 `weight_key = "model.layers.0.mlp.down_proj.weight"`：
   - `payload["qweight"]` → `deploy_tensors["model.layers.0.mlp.down_proj.weight"] = int8 张量`
   - `weight_scale` → key 变成 `"model.layers.0.mlp.down_proj.weight_scale"` = float32 scale
   - `weight_bias` 值为 `None` → **跳过**，不写盘

**需要观察的现象 / 命名映射规则总结**：

| payload 字段 | safetensors key | 取值（INT8） |
| --- | --- | --- |
| `qweight` | `…down_proj.weight` | int8 量化权重（顶替原浮点 weight） |
| `weight_scale` | `…down_proj.weight_scale` | float32 per-channel scale |
| `weight_bias`（None） | （不写） | INT8 无 bias，跳过 |

规则一句话：**`qweight` 直接覆盖原 `.weight`；其余字段把 `.weight` 改写成 `.weight_<字段名>`；`None` 字段丢弃。**

**预期结果**：在 `output_dir` 下会生成 `layer_0000.safetensors` 等分片，里面有 `…down_proj.weight`（int8）与 `…down_proj.weight_scale`（float32）两个 key，且原始浮点 `…down_proj.weight` 不会出现在任何输出分片里（被 `tensor_routes` 标记为已替换）。

> 想看真实断言，可读 [test_int_export_deploy_uses_rounding_offset_when_provided](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/test_dtypes.py#L181-L190) 与 [test_weight_quant_4bit_real_quant](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/test_dtypes.py#L245-L250)：后者断言 4-bit real_quant 产出的 `qw.dtype==int8`、`bias.shape==(8,)`，即 bias 确实进入 payload。

#### 4.4.5 小练习与答案

**练习 1**：对一个 MXFP8 的 `down_proj`，deploy 后 safetensors 里会有哪两个 key？类型分别是什么？

**参考答案**：`…down_proj.weight`（`float8_e4m3fn`，对应 payload 的 `qweight`）和 `…down_proj.weight_scale`（`uint8`，对应 payload 的 `weight_scale`，即共享指数 e8m0）。没有 `weight_bias`，因为 `QuantDequantMx.export_deploy` 的字典里只有两个键。

**练习 2**：为什么 `export_block_deploy` 要把 `payload["weight_bias"] is None` 的字段跳过？

**参考答案**：不同数据类型/位宽产出的 payload 字段数不同——INT8 没有 bias（`weight_quant` 返回 `None`），INT4 才有辅助 bias。如果不去掉 `None`，`save_file` 会试图写一个空张量导致出错，也会在 index 里留下无意义的 key。所以「`None` 即不存在」是 payload 字典的约定，跳过它才能让同一套命名循环兼容 int/mxfp 两种字典。

**练习 3**：HiFloat8 模型如果想跑 deploy 命令，会在哪一步、哪个函数报错？

**参考答案**：在 `export_block_deploy` 遍历到某个 `QuantLinear`、调 `module.export_deploy()` → `WeightQuantizer.export_deploy` → `getattr(self.quant_obj, "export_deploy", None)` 时，`QuantDequantHifp.export_deploy` 存在但内部 `raise NotImplementedError`（[hifp.py:53-56](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/hifp.py#L53-L56)），于是抛出 `NotImplementedError: HiFloat-8 export_deploy is not supported yet.`。

## 5. 综合实践

**任务**：为一份 INT4 MoE 权重画出从「浮点权重张量」到「safetensors key」的完整落盘数据流，并预测每个产物张量的 dtype 与 shape 变化。

设某个专家的 `down_proj` 原始浮点权重 shape 为 `[inter_size, hidden_size]`（`[K, N]`，记 `K` 沿 dim=0）。请按以下步骤完成：

1. **查 `weight_quant` 的 4-bit real_quant 分支**（[int_impl.py:82-89](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/int_impl.py#L82-L89)），写出它返回的三元组各自的 dtype：
   - `qx`：经 `pack_4bit` 后仍是 `int8`，但因转置 + 两两打包，shape 的某一维会减半。
   - `scale`：经 `scale_fp32_to_u64` 变成 `uint64`，且最后一维翻倍（每个 float32 scale 占两个 uint32 拼成 uint64）。
   - `bias`：`int4_assistance_bias` 沿 `dim=1` 求和，shape 为 `(K,)`，dtype float32。
2. **套命名规则**（[deploy_export.py:147-152](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L147-L152)），列出该专家在 safetensors 里会出现的三个 key 与对应 dtype。
3. **解释 `int4_assistance_bias` 的作用**（提示：注释写明是 "for W4A8 MoEGEMM"，看 [int4_assistance_bias](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/int_impl.py#L96-L104) 里 `(expanded_scale * weight * 8).sum(dim=1)` 在补偿什么）。

**参考要点**：

- key 列表：`…down_proj.weight`（int8 打包）、`…down_proj.weight_scale`（uint64）、`…down_proj.weight_bias`（float32）。
- `bias` 把每个输出通道的 `scale × 量化值` 预先聚合成一个标量偏置，供 W4A8 MoEGMM kernel 在反量化计算时一次性补偿 int4 打包引入的偏移，省去运行时逐元素乘 scale 的开销。
- dtype 预测可对照 [test_pack_4bit_shape](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/test_dtypes.py#L228-L232) 与 [test_int4_assistance_bias_shape](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/test_dtypes.py#L252-L256) 验证；具体 shape 数值受 `pack_4bit` 转置影响，若不便手算可标注「待本地验证」。

## 6. 本讲小结

- `DTYPE_REGISTRY` 登记了 `int`/`mxfp`/`hifp` 三类数据类型，三者共享 `forward`（observe/16-bit 透传，否则 fake_quant）与 `export_deploy`（产出真低比特 payload）的统一契约，靠注册表多态让上层不关心具体类型。
- `QuantDequantInt` 把工作分给 `int_impl`：权重走 per-channel `weight_quant`（带舍入偏移 `v`、用 STE 可微），激活走 per-token `dynamic_per_token_quant`；伪量化乘回 scale，真量化返回 int8。
- `QuantDequantMx` 用 Microscaling：每 32 元素共享一个 uint8 指数 `e8m0`，尾数 MXFP8 用 `float8_e4m3fn`、MXFP4 用打包 uint8；`weight_scale` 在这里是 uint8 而非 float32。
- HiFloat8 目前只能伪量化，`export_deploy` / `deploy` 均 `raise NotImplementedError`，不能烘焙落盘。
- export_deploy 落盘链路为 `QuantLinear.export_deploy` → `WeightQuantizer.export_deploy` → `quant_obj.export_deploy` → payload 字典；deploy 阶段的命名规则是：`qweight` 覆盖原 `.weight`，其余字段把 `.weight` 改写为 `.weight_<字段名>`，`None` 字段跳过。
- INT8 payload = `{qweight:int8, weight_scale:float32, weight_bias:None}`；INT4 额外有 `weight_bias`（W4A8 MoEGMM 辅助）且 scale 编码为 uint64；MXFP payload = `{qweight, weight_scale}`——同名 key 在不同数据类型下类型与语义不同，这是 deploy 阶段 `is_mx` 总开关区分的根本原因。

## 7. 下一步学习建议

- **回到 deploy 全景**：本讲填上了 [u4-l4](u4-l4-deploy-export.md) 里 payload 的来源，建议重读 `generate_quant_config`，体会 payload 的实际 dtype 与 `quantization_config` 里 `qtype`（`is_mx`→`float`/`int`）、`strategy`（`group`/`channel`/`token`）、`weight_block_size`（`[1, 32]`）是如何对齐的。
- **算法如何影响 payload**：`WeightQuantizer.export_deploy` 对带 `quantize()` 钩子的算法（AutoRound）会走单独分支，建议结合 [u6-l4](u6-l4-learnable-algos.md) 读 AutoRound 的 `export_deploy`，看它如何把学习到的舍入偏移烘焙进权重。
- **HiFloat8 的另一条路**：HiFloat8 的 deploy 由独立的 NPU 算子 `hifloat8_cast` 承担（见 [u8-l2](u8-l2-hifloat8-cast-op.md)），而非走本讲的 `export_deploy`。若你对 HiFloat8 落盘感兴趣，下一步应读 amct_ops 的算子层。
- **测试先行**：本讲大量引用 `tests/unit_test/quantization/test_dtypes.py`，建议本地跑一遍 `pytest tests/unit_test/quantization/test_dtypes.py -q`（CPU 可跑，标 `npu` 的用例会自动跳过），用断言巩固对每种 dtype 行为的记忆。
