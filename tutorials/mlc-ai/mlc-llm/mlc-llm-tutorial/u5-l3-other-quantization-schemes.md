# 其他量化方案对比

## 1. 本讲目标

上一讲（u5-l2）我们把 `group-quant`（分组量化）讲透了：对称、按组、int4 打包进 uint32。但 `QUANTIZATION` 注册表里还有一大半成员并不属于 group-quant 家族——它们用不同的数据类型、不同的粒度、不同的加载路径。本讲把这些「其他方案」放在一起横向对比，让你建立一张完整的量化选型地图。

学完本讲你应该能够：

- 说清 **AWQ** 的存储格式（`qweight` / `qzeros` / `scales` 三件套）、它为何是**非对称**量化、以及为什么它的 `quantize_model` 不填 `QuantizeMapping`（对应「加载预量化权重」路径）。
- 区分 **FP8** 的两种粒度：`per-tensor`（整张量一个 scale）与 `block-scale`（按 2D 块各一个 scale），并理解 FP8 作为**浮点**格式与 int 量化的本质差别。
- 理解 **per-tensor FP8 为何需要校准（calibrate）**：把 `calibration_mode` 的 `inference` / `max` 两条分支与 `cli/calibrate.py` 串起来。
- 知道 **`no_quant`**（只转 dtype、不压缩）与 **`ft-quant`**（FasterTransformer/CUTLASS int4、NVIDIA 专用、带 group-quant 回退）各自的定位与适用场景。
- 能独立产出一张「方案名 / kind / 输出张量 / 是否需校准 / 典型用途」的对比表。

## 2. 前置知识

本讲是 U5 量化单元的收口，假定你已掌握 u5-l1、u5-l2 建立的几条主线。这里只补三个本讲会反复用到、但前面没展开的直觉。

**直觉一：整数（int）量化 vs 浮点（FP8）量化的本质差别。** group-quant / AWQ / ft 把权重压成**整数**（int4/int8），存储时按位打包进 uint32/int8，反量化时乘 scale 还原成浮点。而 FP8（`float8_e4m3fn` / `float8_e5m2`）本身就是一种** 8 位浮点格式**——它有指数位和尾数位，只是位宽只有 8。这意味着 FP8 不需要「按位打包」：一个 fp8 元素就占 1 字节，可以直接存。两种格式各有取舍：

| 维度 | int 量化（int4/int8） | FP8 量化 |
|---|---|---|
| 数据类型 | 整数，需 scale 还原 | 浮点，自带动态范围 |
| 压缩比 | int4 约 4× | 固定 2×（相对 fp16） |
| 表示方式 | 均匀刻度（等间距） | 非均匀刻度（指数分布，越靠近 0 刻度越密） |
| 硬件 | 通用（反量化融进 matmul） | 需 fp8 硬件（Hopper/Ada/MI300 等）才划算 |

FP8 的两种子格式：`e4m3`（4 指数 + 3 尾数）尾数精度更高、范围较小（最大值 **448**）；`e5m2`（5 指数 + 2 尾数）范围更大（最大值 **57344**）但精度较低。这两个常数会反复出现在源码里。

**直觉二：「对称」vs「非对称」。** 对称量化假设数值关于 0 对称（典型如权重），用一个 `scale` 把 `[-max, +max]` 映射到整数范围，**没有零点（zero-point）**——group-quant 就是这种。非对称量化允许数值分布偏离 0（典型如激活值全正），除了 `scale` 还要存一个**零点 `zero`**，反量化公式是 `(q - zero) * scale`——AWQ 就是这种。

**直觉三：「校准（calibration）」到底在校什么。** 权重是静态的，量化权重时直接看它的 max 就能定 scale。但**激活值（每层 matmul 的输入）是动态的**，推理时才知道。如果激活里有少数极大的「离群值」（LLM 的典型现象），用动态 max 定 scale 会被离群值拉爆、小数值精度全失。校准就是：**先用一批代表性数据跑一遍，统计每层激活的典型 max，把这个 scale 固定下来**，之后推理用固定 scale（不再动态算）。并不是所有量化都需要校准——只有「激活也量化」的方案（FP8 静态激活）才需要。

> 前置依赖：u5-l1（注册表 / 统一接口 / `kind` 桥接）、u5-l2（group-quant 的 visitor 改图、`QuantizeMapping`、`num_elem_per_storage` 概念）、u4-l2（`QuantizeMapping` 方向、加载路径 A 即时量化 vs 路径 B 预量化权重）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [python/mlc_llm/quantization/awq_quantization.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/awq_quantization.py) | AWQ 量化：`AWQQuantize` 配置、`_Mutator` 改图、`AWQQuantizeLinear`（`qweight`/`qzeros`/`scales`）、非对称 `_dequantize`。 |
| [python/mlc_llm/quantization/per_tensor_quantization.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/per_tensor_quantization.py) | FP8 per-tensor 量化：`PerTensorQuantize`、`quantize_float8`（TRT-LLM scale 公式）、`calibration_mode`（inference/max）、CUTLASS `fp8_gemm` 派发。 |
| [python/mlc_llm/quantization/block_scale_quantization.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/block_scale_quantization.py) | FP8 block-scale 量化：`BlockScaleQuantize`、2D 块粒度 `weight_scale_inv`、DeepSeek MLA 特殊处理、静态激活变体、`cutlass`/`triton` groupwise GEMM。 |
| [python/mlc_llm/quantization/ft_quantization.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/ft_quantization.py) | FasterTransformer 量化：`FTQuantize`（int8 存储）、CUTLASS `ft_preprocess_weight`、`GroupQuantize` 回退机制、`faster_transformer_dequantize_gemm`。 |
| [python/mlc_llm/quantization/no_quantization.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/no_quantization.py) | `NoQuantize`：最简配置，仅 `name`/`kind`/`model_dtype`，不做任何量化。 |
| [python/mlc_llm/quantization/quantization.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/quantization.py) | `QUANTIZATION` 注册表：本讲所有方案名（`q4f16_autoawq` / `e4m3_e4m3_f16` / `fp8_e4m3fn_bf16_block_scale` / `q4f16_ft` / `q0f16` 等）的实例来源。 |
| [python/mlc_llm/quantization/model_quantization.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/model_quantization.py) | `make_quantization_functions` 工厂：用 `supports_awq` / `supports_per_tensor` / `supports_block_scale` 等开关决定一个模型支持哪些 kind。 |
| [python/mlc_llm/interface/calibrate.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/calibrate.py) | 校准接口：`CalibrationObserver`（全局回调累积激活 max）+ `calibrate()`（驱动 `AsyncMLCEngine` 跑 ShareGPT 样本）。 |
| [python/mlc_llm/cli/calibrate.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/calibrate.py) | `mlc_llm calibrate` 命令行入口：解析 `--dataset` / `--num-calibration-samples` 等参数。 |

## 4. 核心概念与源码讲解

### 4.1 AWQ 格式：非对称、预量化、转置存储

#### 4.1.1 概念说明

AWQ（Activation-aware Weight Quantization）是一种 int4 量化方案，和 group-quant 一样把权重压成 4 bit，但有三点本质不同：

1. **非对称量化**：除了 scale，还多存一个**零点（zero-point）**，反量化公式是 `(q - zero) * scale`。这是为了适配权重分布不完全关于 0 对称的情况。
2. **粒度是「per-channel-out × per-group-in」**：scale 和 zero 的形状是 `(in_features // group_size, out_features)`——对每个输出通道、每组输入维度各有一对 scale/zero，比 group-quant 的纯 1D 分组更细。
3. **加载的是预量化权重**：AWQ 的权重通常由 HuggingFace 上的 `autoawq` 工具预先量化好，MLC 直接读取（u4-l2 说的「路径 B」），而不是从 fp16 现场量化。

注册表里它叫 `q4f16_autoawq`，`kind="awq"`，`group_size=128`。

> 为什么要「AWQ」这个名字？原始 AWQ 论文的核心观察是：不同通道对量化的敏感度不同，应当用「激活的分布」来挑选哪些通道保留高精度（activation-aware）。MLC 这里的 `AWQQuantize` 主要实现的是 AWQ 的**存储与反量化格式**（与 autoawq 工具产物兼容），权重敏感度的预处理由 autoawq 在生成 checkpoint 时完成。

#### 4.1.2 核心流程

AWQ 的三层结构（与 group-quant 对照看）：

1. **配置**（`AWQQuantize.__post_init__`）：校验三个 dtype（量化 INT、存储 UINT、计算 FLOAT），算 `num_elem_per_storage`、`num_storage_per_group`、`max_int_value`。和 group-quant 用同一套公式，但 **AWQ 的 `group_size=128`**（远大于 group-quant 的 32）。
2. **改图**（`quantize_model`）：用 `_Mutator` 把符合条件的 `nn.Linear` 替换成 `AWQQuantizeLinear`。**关键差别：这里的 visitor 根本不碰 `quant_map`**——它不登记任何 `param_map`/`map_func`。因为 AWQ 权重是预量化的，值的来源是 HF checkpoint 而非现场压缩，名字对齐由 `ExternMapping`（u4-l1/u4-l2）单独完成。
3. **反量化**（`_dequantize`）：运行期把 `(qweight, qzeros, scales)` 解包还原成浮点权重，融进 matmul。非对称公式：

\[
w_{i,j} = \bigl(q_{i,j} - z_{\,i,\,\lfloor j/g \rfloor}\bigr)\cdot s_{\,i,\,\lfloor j/g \rfloor}
\]

其中 \(g\) 是 `group_size`，\(i\) 是输出通道，\(j\) 是输入维度。

存储形状（对一个 `nn.Linear(in_features, out_features)`，int4、`num_elem_per_storage=8`）：

| 参数 | 形状 | dtype |
|---|---|---|
| `qweight` | `(in_features, out_features // 8)` | uint32 |
| `qzeros` | `(in_features // group_size, out_features // 8)` | uint32 |
| `scales` | `(in_features // group_size, out_features)` | float16 |

注意 **`in_features` 在前**——这是 FasterTransformer/AutoAWQ 的转置布局约定（与 group-quant 的 NK/KN 不同）。反量化时还要做一次 `ft_reorder=True` 的**位重排**（FasterTransformer 对 int4 的特殊位序要求）。

#### 4.1.3 源码精读

[python/mlc_llm/quantization/awq_quantization.py:34-51](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/awq_quantization.py#L34-L51) —— `AWQQuantize` 字段：`group_size`、`quantize_dtype`、`storage_dtype`、`model_dtype` 四个用户配置，加三个派生量（默认 0）和一个量化函数缓存。

[python/mlc_llm/quantization/awq_quantization.py:53-68](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/awq_quantization.py#L53-L68) —— `__post_init__`：与 group-quant 同构的派生计算，但注意 `group_size` 默认 128（见 [quantization.py:135-142](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/quantization.py#L135-L142) 的 `q4f16_autoawq`）。

[python/mlc_llm/quantization/awq_quantization.py:96-131](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/awq_quantization.py#L96-L131) —— **本模块最关键的一段**：`_Mutator.visit_module` 只做 `isinstance(node, nn.Linear) and not is_final_fc(...) and not is_moe_gate(...)` 判断后返回 `AWQQuantizeLinear.from_linear(node, self.config)`。**它从头到尾没有一行写 `self.quant_map[...]`**。对比 group-quant 的 visitor（会登记 `param_map`/`map_func`），这就是 AWQ「只改图、不压数」的直接证据——它走 u4-l2 的路径 B。

[python/mlc_llm/quantization/awq_quantization.py:191-210](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/awq_quantization.py#L191-L210) —— `AWQQuantizeLinear.__init__` 定义三个参数 `qweight` / `qzeros` / `scales` 的形状。注意三者都以 `in_features` 为第一维（转置布局）。

[python/mlc_llm/quantization/awq_quantization.py:133-172](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/awq_quantization.py#L133-L172) —— `_dequantize`：先用 `convert_uint_to_float(..., ft_reorder=True)` 把 `qweight`/`qzeros` 拆包成浮点，再 `topi.transpose`，最后 `te.compute` 实现 `(q - z) * scale`。第 168 行的 `j // self.group_size` 就是上面公式里的 \(\lfloor j/g \rfloor\)。

[python/mlc_llm/quantization/awq_quantization.py:19-31](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/awq_quantization.py#L19-L31) —— `_calculate_zeros_width`：根据 `group_size` 给 `qzeros` 的宽度做对齐填充（128→×1、64→×2、32→×4），是兼容 AutoAWQ 落盘布局的细节。

#### 4.1.4 代码实践

**实践目标**：亲手验证 AWQ 与 group-quant 在「改图时是否登记 `QuantizeMapping`」上的差别，并手算 AWQ 的存储形状。

**操作步骤**（源码阅读型）：

1. 打开 [awq_quantization.py:96-126](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/awq_quantization.py#L96-L126)，确认 visitor 里**没有**任何 `self.quant_map.param_map[...] =` 或 `self.quant_map.map_func[...] =`。
2. 对比 group-quant 的同名 visitor（[group_quantization.py 的 _Mutator](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/group_quantization.py#L97-L148)，回顾 u5-l2），那里每个被替换的层都会登记 `param_map` 和 `map_func`。
3. 假设一个 `nn.Linear(4096, 4096)`，`group_size=128`、int4（`num_elem_per_storage=8`），手算 `qweight` / `qzeros` / `scales` 三者的形状与字节数。

**需要观察的现象 / 预期结果**：

- 第 1 步应确认 AWQ visitor 对 `quant_map` 零写入。
- 第 3 步预期：
  - `qweight = (4096, 4096/8) = (4096, 512)`，uint32 → `4096×512×4 = 8,388,608` 字节 ≈ 8 MiB。
  - `qzeros = (4096/128, 4096/8) = (32, 512)`，uint32 → 65,536 字节。
  - `scales = (32, 4096)`，float16 → 262,144 字节。
  - 合计约 8.7 MiB；原始 fp16 是 `4096×4096×2 = 33.5 MiB`，压缩比约 3.85×。

> 待本地验证：若你装好了 `mlc_llm`，可以用 `mlc_llm.model.llama.LlamaForCausalLM` 建图后调 `QUANTIZATION["q4f16_autoawq"].quantize_model(model, QuantizeMapping({},{}), "")`，打印替换后某层的 `.qweight.shape` 验证上面的手算。

#### 4.1.5 小练习与答案

**练习 1**：AWQ 的反量化公式多了一个 `qzeros`（零点），而 group-quant 没有。请用一句话解释为什么 AWQ 需要它、group-quant 不需要。

> **答案**：group-quant 是**对称**量化（数值关于 0 对称，零点固定为 0，无需存储）；AWQ 是**非对称**量化（数值分布可能整体偏移），必须额外存一个零点 `zero` 来记录「整数 0 对应的真实值」，否则还原出的权重会带一个整体偏置。

**练习 2**：AWQ 的 `quantize_model` 不写 `quant_map`，那运行时 AWQ 层的 `qweight`/`qzeros`/`scales` 的具体数值从哪里来？

> **答案**：从 HuggingFace 上**已经用 autoawq 预量化好的 checkpoint** 直接加载，名字对齐由 `ExternMapping`（u4-l1/u4-l2 的「路径 B」）完成；`quantize_model` 只负责把模型图的参数名和形状改成 AWQ 布局，并不产生数值。

---

### 4.2 FP8：per-tensor 与 block-scale 两种粒度

#### 4.2.1 概念说明

FP8 量化把权重（和激活）压成 8 位浮点。和 int 量化最大的不同是：**FP8 元素本身是浮点，一个元素就占 1 字节，不需要按位打包**（除非 storage_dtype 选了 uint32 才打包）。因此当 `storage_dtype == weight_dtype`（都是 fp8）时，`num_elem_per_storage = 1`。

MLC 提供两种 FP8 粒度，对应两个 `kind`：

- **`per-tensor-quant`**：整张权重张量**共用一个标量 scale**（`q_scale` 形状 `(1,)`），粒度最粗、开销最小。代表名 `e4m3_e4m3_f16`、`e5m2_e5m2_f16`。激活可选「静态 scale」（需校准）或「动态 scale」。
- **`block-scale-quant`**：把权重切成 2D 块（如 128×128），**每块一个 scale**（`weight_scale_inv`），粒度细、精度高，专为 Hopper 的 FP8 groupwise GEMM 设计。代表名 `fp8_e4m3fn_bf16_block_scale`。常用于 DeepSeek-V3 这类对精度极敏感的大模型。

两者都支持「激活也量化」（W8A8），而激活量化是否需要校准是关键分水岭。

> 命名约定：FP8 方案名直接写明激活/权重的 dtype 与计算精度，如 `e4m3_e4m3_f16` = 激活 e4m3 + 权重 e4m3 + 计算 fp16。这与 int 方案 `q4f16_1`（权重 4bit + 计算 fp16）的命名风格不同。

#### 4.2.2 核心流程

**per-tensor 路径**（`PerTensorQuantize`）：

1. 配置含 `activation_dtype`、`weight_dtype`、`storage_dtype`、`use_scale`、`calibration_mode`（`"inference"` / `"max"`）。
2. `quantize_model`：替换 `nn.Linear`/`nn.Embedding`/`MixtralExperts` 为 `PerTensorQuantize*`，并**登记 `quant_map`**（`param_map` 指向 `[q_weight, q_scale]`、`map_func` 指向 `quantize_weight`）——这走 u4-l2 的**路径 A（即时量化）**，与 AWQ 相反。
3. `quantize_weight`：编译（并按 shape/dtype/device 缓存）一个量化函数，调 `quantize_float8`。scale 用 TRT-LLM 公式：

\[
s = \max\!\left(\frac{\max|w|}{m},\ \frac{1}{m \cdot 512}\right), \qquad \hat{w}=\mathrm{cast}_{fp8}(w/s)
\]

其中 \(m = \max\_int\_value\)（对 e4m3 是 448）。那个 \(\frac{1}{m\cdot 512}\) 下限是为了防止 scale 过小导致溢出。
4. `forward` 按 `calibration_mode` 分两支（详见 4.2.3）。

**block-scale 路径**（`BlockScaleQuantize`）：

1. `weight_block_size` 不在配置里写死，而是**从模型自身读取**（`model.weight_block_size`，DeepSeek 这类模型会自带），所以 `quantize_model` 第一步就是 `weight_block_size = model.weight_block_size`。
2. 替换 `nn.Linear` → `BlockScaleQuantizeLinear`，它存 `weight`（fp8，`(out, in)`）+ `weight_scale_inv`（float32，形状 `(ceil(out/bs0), ceil(in/bs1))`）。注意 **fp8 不打包**，`weight` 就是原形状的 fp8 张量。
3. `forward`：单 token（GEMV）走 `dequantize_float8_groupwise_scaled_gemv`；多 token 先对激活做 `rowwise_group_quant_fp8`（行内按组分组量化），再调 `cutlass.fp8_groupwise_scaled_gemm` 或 `triton.fp8_groupwise_scaled_gemm`。
4. 静态激活变体 `BlockScaleQuantizeLinearStaticActivation` 多存一个 `activation_scale`（预计算，需校准），对应 `fp8_e4m3fn_bf16_block_scale_static_activation`。

#### 4.2.3 源码精读

**per-tensor：**

[python/mlc_llm/quantization/per_tensor_quantization.py:31-52](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/per_tensor_quantization.py#L31-L52) —— `PerTensorQuantize` 字段。注意 `calibration_mode: Literal["inference","max"]`（第 51 行）和 `use_scale`（第 46 行）这两个开关决定了是否走校准。

[python/mlc_llm/quantization/per_tensor_quantization.py:54-60](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/per_tensor_quantization.py#L54-L60) —— `__post_init__`：`max_int_value = int(tirx.max_value(self.weight_dtype).value)`，对 e4m3 这个值就是 448。

[python/mlc_llm/quantization/per_tensor_quantization.py:117-163](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/per_tensor_quantization.py#L117-L163) —— `_Mutator.visit_module`：与 AWQ 形成鲜明对照——这里**每个被替换的层都写了 `self.quant_map.param_map[weight_name] = param_names` 和 `self.quant_map.map_func[weight_name] = self.config.quantize_weight`**（第 131-132 行等）。第 147-162 行还处理 `q_calibration_scale` 占位参数。

[python/mlc_llm/quantization/per_tensor_quantization.py:220-244](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/per_tensor_quantization.py#L220-L244) —— `_compute_scale`：上面那条 TRT-LLM 公式的实现。第 233 行 `min_scaling_factor = tirx.const(1.0 / (self.max_int_value * 512.0), ...)`。

[python/mlc_llm/quantization/per_tensor_quantization.py:417-439](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/per_tensor_quantization.py#L417-L439) —— `forward` 里 `calibration_mode` 的两个分支：
- `"inference"`（第 418-422 行）：用**预存**的 `q_calibration_scale` 除激活后转 fp8，即「静态激活 scale」。
- `"max"`（第 423-437 行）：现场用 `quantize_float8` 算激活 scale，跨卡 `nn.ccl_allreduce(..., "max")`，再通过 `nn.extern("mlc_llm.calibration_observer", ...)` 把每层激活 scale 喂给校准回调——**这就是校准时收集数据的入口**。

[python/mlc_llm/quantization/per_tensor_quantization.py:441-464](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/per_tensor_quantization.py#L441-L464) —— 当 `weight_dtype==storage_dtype` 且 inference 模式时，优先派发到 CUTLASS `fp8_gemm`（第 458 行），把反量化与 matmul 融进单个 fp8 GEMM kernel——这是 FP8 在 Hopper/Ada 上高吞吐的来源。

[python/mlc_llm/quantization/quantization.py:150-187](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/quantization.py#L150-L187) —— 三个 per-tensor 实例：`e5m2_e5m2_f16`（`use_scale=False`）、`e4m3_e4m3_f16`（`calibration_mode="inference"`）、`e4m3_e4m3_f16_max_calibrate`（`calibration_mode="max"`）。后两者成对出现——一个用于校准阶段收集 scale，一个用于推理阶段消费 scale。

**block-scale：**

[python/mlc_llm/quantization/block_scale_quantization.py:23-32](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/block_scale_quantization.py#L23-L32) —— `BlockScaleQuantize` 字段：`weight_block_size` 默认 `None`（运行时从模型读）、`use_activation_scale` 控制是否静态激活。

[python/mlc_llm/quantization/block_scale_quantization.py:71-72](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/block_scale_quantization.py#L71-L72) —— `weight_block_size = model.weight_block_size`：块大小来自模型自身声明。

[python/mlc_llm/quantization/block_scale_quantization.py:98-154](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/block_scale_quantization.py#L98-L154) —— 对 DeepSeek MLA 层（带 `w_uk`/`w_uv`）的特殊处理：把这两个权重也转成 fp8 + 块 scale，并校验 `qk_nope_head_dim`/`v_head_dim` 必须是块大小的整数倍（否则报错，第 105-112 行）。这正说明 block-scale 主要服务 DeepSeek 这类架构。

[python/mlc_llm/quantization/block_scale_quantization.py:198-205](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/block_scale_quantization.py#L198-L205) —— `BlockScaleQuantizeLinear.__init__`：`weight` 直接是 fp8 的 `(out_features, in_features)`（**不打包**），`weight_scale_inv` 是 `(ceil(out/bs0), ceil(in/bs1))` 的 float32——这就是「2D 块粒度」。

[python/mlc_llm/quantization/block_scale_quantization.py:662-664](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/block_scale_quantization.py#L662-L664) —— `rowwise_group_quant_fp8` 里 `fp8_max = 448.0 if dtype=="float8_e4m3fn" else 57344.0`：激活按行分组量化的上界，与 per-tensor 的 `max_int_value=448` 同源。

[python/mlc_llm/quantization/block_scale_quantization.py:289-318](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/block_scale_quantization.py#L289-L318) —— `forward`：优先 CUTLASS `fp8_groupwise_scaled_gemm`，否则回退到 Triton `fp8_groupwise_scaled_gemm`。这是「硬件有 fp8 groupwise GEMM 才划算」的直接体现。

#### 4.2.4 代码实践

**实践目标**：搞清 per-tensor FP8 为何可能需要校准，并把 `calibration_mode` 两个分支与 `cli/calibrate.py` 串成一条链。

**操作步骤**（跟踪型）：

1. 读 [per_tensor_quantization.py:417-439](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/per_tensor_quantization.py#L417-L439)，分别用一句话写出 `inference` 与 `max` 两个分支对激活 scale 的处理。
2. 读 [interface/calibrate.py:32-50](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/calibrate.py#L32-L50)，找到名为 `mlc_llm.calibration_observer` 的全局回调，看它如何用 `np.maximum` 累积每次推理的激活 max。
3. 读 [interface/calibrate.py:131-172](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/calibrate.py#L131-L172)，确认 `calibrate()` 用 `AsyncMLCEngine(..., mode="server")` 跑 ShareGPT 样本，结束时 `save_params(output)` 落盘 scale。
4. 读 [cli/calibrate.py:39-51](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/calibrate.py#L39-L51)，看 `--dataset`（ShareGPT）和 `--num-calibration-samples`（默认 16）两个必填参数。

**需要观察的现象 / 预期结果**：

- `max` 分支在每次 forward 时都把「当前 batch 的激活 max」通过 `mlc_llm.calibration_observer` extern 喂回 Python；`CalibrationObserver.callback` 用 `np.maximum` 在多个样本上取**逐元素 max**，最终得到一个稳定的逐层激活 scale。
- `inference` 分支则用这个**已固化**的 scale（`q_calibration_scale`）除激活，不再动态算——这就是「校准后推理」。
- 因此 per-tensor FP8 的典型两步流程：先用 `q4..._max_calibrate`（即 `calibration_mode="max"`）跑 `mlc_llm calibrate` 收集 scale → 再用 `e4m3_e4m3_f16`（`inference`）带这些 scale 编译/推理。

**为什么需要校准（一句话结论）**：FP8 e4m3 的动态范围只有 ±448，而 LLM 激活有少量极大离群值；若每次推理都用当前 batch 的动态 max 定 scale，离群值会把 scale 撑大、绝大多数正常值严重欠精度。校准用一批代表性数据预先把每层激活的典型 max 固定下来，既压住离群值又保住正常值精度。

> 待本地验证：若有 NVIDIA Hopper/Ada GPU，可对一个小模型分别用动态 scale 与校准后静态 scale 跑评估，对比 perplexity/准确率差异。

#### 4.2.5 小练习与答案

**练习 1**：`e4m3_e4m3_f16` 配置下 `num_elem_per_storage` 等于多少？为什么和 group-quant 的 8 不一样？

> **答案**：等于 **1**。因为 `storage_dtype == weight_dtype == float8_e4m3fn`，8/8=1，一个 fp8 元素正好占 1 字节存储单元，无需按位打包。group-quant 的 8 来自 uint32 存储 / int4 量化 = 32/4。

**练习 2**：block-scale 的 `weight_scale_inv` 是 2D 的，per-tensor 的 `q_scale` 是 `(1,)` 标量。请说明这种粒度差别对精度的影响。

> **答案**：per-tensor 用一个 scale 迁就整张量的最大离群值，远离离群值的区域动态范围利用不足、精度较差；block-scale 给每个 2D 块（如 128×128）单独的 scale，每块都能用满 fp8 范围，精度显著更高（代价是多了 scale 存储与 groupwise GEMM 的实现复杂度）。

**练习 3**：为什么 `block_scale_quantization.py` 里要专门为 DeepSeek 的 `w_uk`/`w_uv` 写特殊分支，而 per-tensor 不需要？

> **答案**：block-scale 要求权重维度是块大小的整数倍（第 105-112 行的校验），而 DeepSeek 的 MLA 结构里 `w_uk`/`w_uv` 的形状特殊（`kv_lora_rank`、`qk_nope_head_dim`、`v_head_dim`），需要单独 reshape 并校验对齐；per-tensor 只有一个标量 scale，对形状没有对齐约束，故无需特判。

---

### 4.3 ft（FasterTransformer）与 no_quant：两个极端

#### 4.3.1 概念说明

这两个方案分别代表「为极致吞吐而高度专用」和「完全不量化」两个极端。

**`no_quant`（`NoQuantize`）**：最简单的量化方案——**根本不量化**，只把模型转成目标 dtype（fp16/bf16/fp32）。注册表里对应 `q0f16`、`q0bf16`、`q0f32`，`kind="no-quant"`，名字里的 `q0` 表示「0 bit 量化」。它的 `NoQuantize` 类只有三个字段，连 `quantize_model`/`quantize_weight` 方法都没有。用途：精度基线、调试、模型很小不必压缩、或目标硬件没有量化 kernel。

**`ft-quant`（`FTQuantize`，FasterTransformer）**：NVIDIA 专用的高吞吐 int4/int8 方案，对应 `q4f16_ft`，`kind="ft-quant"`。三个关键特征：

1. **存储用 int8**（不是 uint32！），int4 时两个元素 pack 进一个 int8（`num_elem_per_storage=2`）。
2. **强依赖 CUTLASS**：`quantize_weight` 要调 `cutlass.ft_preprocess_weight` 做权重布局重排（FasterTransformer 的 int4 GEMM 对布局有特殊要求），运行期 `forward` 调融合的 `faster_transformer_dequantize_gemm`。
3. **混合量化（带 group-quant 回退）**：当某层不满足 CUTLASS 的形状约束（如 `out_features % 8 != 0`、是 `lm_head`、`out_dtype==float32`），就**自动回退到 group-quant**。所以 `ft-quant` 实际上是「能用 FT 就用 FT、不能用就用 group-quant」的混合体。

#### 4.3.2 核心流程

**no_quant 流程**（极简）：

1. `NoQuantize.__post_init__` 只 assert `kind == "no-quant"`，仅此而已。
2. 工厂 `_no_quant`（[model_quantization.py:40-43](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/model_quantization.py#L40-L43)）：建图 → `model.to(model_dtype)` → 返回**空的** `QuantizeMapping({}, {})`。
3. 因为 `QuantizeMapping` 空，所以没有任何参数被改名/被压缩，权重原样以 fp16/bf16/fp32 落盘。

**ft-quant 流程**（混合）：

1. `__post_init__`：校验 `quantize_dtype ∈ {int4,int8}`、`storage_dtype` 是 INT（int8）、`model_dtype==float16`、`group_size ∈ {None,64,128}`；算 `num_elem_per_storage`（int4→2、int8→1）。
2. `quantize_model` 的 visitor 遍历 `nn.Linear`/`nn.Embedding`，但**对每个 Linear 先判断是否满足 CUTLASS 约束**：
   - 若 `is_final_fc`、`out_dtype==float32`、int4 时 `out_features % 8 != 0`、int8 时 `out_features % 4 != 0` → **回退**：用 `fallback_group_quantize()` 造一个 `GroupQuantize`，替换成 `GroupQuantizeLinear`，`map_func` 指向 group-quant 的 `quantize_weight`。
   - 否则 → 用 `FTQuantizeLinear`，`map_func` 指向 FT 的 `quantize_weight`。
   - `nn.Embedding` **一律回退**到 `GroupQuantizeEmbedding`。
3. `quantize_weight`：必须是 CUDA 设备且 TVM 启用了 CUTLASS（`relax.ext.cutlass`），否则直接报错；用 TE 做分组量化后调 `cutlass.ft_preprocess_weight` 重排布局。
4. `forward`：调 `faster_transformer_dequantize_gemm`——一个融合了「反量化 + GEMM」的 CUTLASS kernel。

#### 4.3.3 源码精读

**no_quant：**

[python/mlc_llm/quantization/no_quantization.py:7-16](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/no_quantization.py#L7-L16) —— 整个 `NoQuantize` 类只有 `name`/`kind`/`model_dtype` 三个字段 + 一个 assert `kind=="no-quant"` 的 `__post_init__`。没有 `quantize_model`、没有 `quantize_weight`。

[python/mlc_llm/quantization/model_quantization.py:40-43](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/model_quantization.py#L40-L43) —— 工厂 `_no_quant`：建图、`to(dtype)`、返回空 `QuantizeMapping`。这就是「不量化」的全部实现。

[python/mlc_llm/quantization/quantization.py:32-46](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/quantization.py#L32-L46) —— `q0f16`/`q0bf16`/`q0f32` 三个实例，唯一差别是 `model_dtype`。

**ft-quant：**

[python/mlc_llm/quantization/ft_quantization.py:29-39](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/ft_quantization.py#L29-L39) —— `FTQuantize` 字段：`storage_dtype` 固定 int8、`group_size ∈ {None,64,128}`。

[python/mlc_llm/quantization/ft_quantization.py:42-59](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/ft_quantization.py#L42-L59) —— `fallback_group_quantize()`：硬编码回退用的 `GroupQuantize(group_size=32, ..., linear_weight_layout="NK")`。

[python/mlc_llm/quantization/ft_quantization.py:125-163](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/ft_quantization.py#L125-L163) —— visitor 的回退判定（第 131-150 行）：四个回退条件（`is_final_fc`、`out_dtype==float32`、形状不整除 8/4），命中则换 `GroupQuantizeLinear` 并改 `map_func`；第 154-162 行 `nn.Embedding` 一律回退 `GroupQuantizeEmbedding`。

[python/mlc_llm/quantization/ft_quantization.py:170-189](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/ft_quantization.py#L170-L189) —— `quantize_weight` 的前置断言：`assert tvm.get_global_func("relax.ext.cutlass", True)`，未启用 CUTLASS 直接报错，并提示去 TVM 的 `config.cmake` 开 `USE_CUTLASS`。第 193 行还限定 `device_type == "cuda"`。

[python/mlc_llm/quantization/ft_quantization.py:199-217](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/ft_quantization.py#L199-L217) —— 用 `BlockBuilder` 构建：先 `bb.emit_te(self._quantize, ...)` 做分组量化，再 `relax.call_pure_packed("cutlass.ft_preprocess_weight", ...)` 做布局重排（第 206-214 行），最后输出 `(预处理后的 q_weight, scale)`。

[python/mlc_llm/quantization/ft_quantization.py:376-392](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/ft_quantization.py#L376-L392) —— `FTQuantizeLinear.forward`：直接调 `faster_transformer_dequantize_gemm(x, q_weight, q_scale, bias, group_size=...)`，把反量化与 GEMM 融进单个 CUTLASS kernel。

#### 4.3.4 代码实践

**实践目标**：验证 `no_quant` 的「零量化」与 `ft-quant` 的「混合回退」两件事。

**操作步骤**（源码阅读 + 修改观察型）：

1. 读 [model_quantization.py:40-43](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/model_quantization.py#L40-L43)，确认 `_no_quant` 返回的 `QuantizeMapping` 是空字典——意味着 `convert_weight` 阶段不会触发任何 `quantize_weight`，权重按原始 dtype 落盘。
2. 读 [ft_quantization.py:125-163](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/ft_quantization.py#L125-L163)，列一张「什么条件 → 回退到 group-quant」的清单。
3. **修改观察**：假想一个 `nn.Linear(in=4096, out=4097)`（注意 `out_features=4097` 不是 8 的倍数），判断它在 `q4f16_ft` 下会走 FT 还是回退。

**需要观察的现象 / 预期结果**：

- 第 2 步清单：`is_final_fc(name)`（输出投影） / `out_dtype=="float32"` / int4 且 `out_features % 8 != 0` / int8 且 `out_features % 4 != 0` / 是 `nn.Embedding`。
- 第 3 步：`out=4097 % 8 = 1 ≠ 0`，**回退**到 `GroupQuantizeLinear`（即便整体选了 `q4f16_ft`，这一层仍是 group-quant）。这正是 `ft-quant` 是「混合体」的体现。

> 待本地验证：在 CUDA + 编译了 CUTLASS 的环境下，对比同一个模型用 `q4f16_ft` 与 `q4f16_1`（group-quant）的 decode 吞吐，FT 通常更快；但在非 CUDA 设备上 `mlc_llm convert_weight --quantization q4f16_ft` 会在 `quantize_weight` 的断言处直接失败。

#### 4.3.5 小练习与答案

**练习 1**：`NoQuantize` 没有 `quantize_model`/`quantize_weight` 方法，这违反 u5-l1 说的「`Quantization` 统一接口」吗？

> **答案**：不违反。u5-l1 指出 `Quantization` 是**鸭子类型**（注释里的「required to have」是约定，不是强制继承）。`no-quant` 的工厂 `_no_quant` 在外层（`model_quantization.py`）用「建图 + `to(dtype)` + 空 mapping」绕开了对这两个方法的调用，所以 `NoQuantize` 本身不需要实现它们。

**练习 2**：`ft-quant` 为什么要把 `nn.Embedding` 一律回退到 group-quant，而不是用 FT 的 int4？

> **答案**：FasterTransformer/CUTLASS 的 int4 GEMM kernel 是为 `nn.Linear` 这种 dense matmul 设计的，`nn.Embedding` 本质是按索引查表（gather）而非 matmul，没有对应的 FT kernel；而 group-quant 有专门的 `GroupQuantizeEmbedding`（反量化后 `take`），所以一律回退。

**练习 3**：用户在 AMD GPU（ROCm）上选 `q4f16_ft` 会发生什么？

> **答案**：`quantize_weight` 会因 `device_type != "cuda"` 抛 `NotImplementedError`（[ft_quantization.py:244-245](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/ft_quantization.py#L244-L245)）；即便在 CPU 上预处理也会因找不到 `relax.ext.cutlass` 而报错。所以 `ft-quant` 是 NVIDIA + CUTLASS 专属方案。

---

## 5. 综合实践

把本讲五个方案（AWQ / per-tensor FP8 / block-scale FP8 / ft / no_quant）和上一讲的 group-quant 放在一起，完成下面两件事。

### 任务一：制作量化方案对比表

请按下面的列，自己先填一遍，再对照参考答案：

| 方案名 | kind | 权重 dtype | 存储格式（每层输出张量） | 是否需校准 | 典型用途 |
|---|---|---|---|---|---|
| `q4f16_1` | group-quant | int4 | `q_weight`(uint32) + `q_scale`(fp16) | 否 | 通用默认，跨平台 |
| `q4f16_autoawq` | awq | int4 | `qweight`(uint32) + `qzeros`(uint32) + `scales`(fp16) | 否 | 加载 HF 上 autoawq 预量化权重 |
| `e4m3_e4m3_f16` | per-tensor-quant | float8_e4m3fn | `q_weight`(fp8) + `q_scale`(f32) [+ `q_calibration_scale`(f32)] | 是（若用静态激活） | Hopper/Ada 上 W8A8 推理 |
| `e4m3_e4m3_f16_max_calibrate` | per-tensor-quant | float8_e4m3fn | 同上 + 校准分支 | ——（它就是校准阶段） | 收集激活 scale |
| `fp8_e4m3fn_bf16_block_scale` | block-scale-quant | float8_e4m3fn | `weight`(fp8) + `weight_scale_inv`(f32) | 否（动态激活） | DeepSeek 等，Hopper FP8 groupwise GEMM |
| `fp8_e4m3fn_bf16_block_scale_static_activation` | block-scale-quant | float8_e4m3fn | 同上 + `activation_scale`(f32) | 是 | 同上但静态激活、更稳 |
| `q4f16_ft` | ft-quant | int4（存 int8） | `q_weight`(int8) + `q_scale`(fp16)，不满足约束处回退为 group-quant | 否 | NVIDIA + CUTLASS 极致吞吐 |
| `q0f16` / `q0bf16` / `q0f32` | no-quant | 原 dtype | 原样（fp16/bf16/fp32） | 否 | 基线/调试/小模型 |

**操作要点**：

1. 先盖住答案，从 [quantization.py:31-201](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/quantization.py#L31-L201) 的注册表逐条填「方案名 / kind」。
2. 「存储格式」一列回到各方案的 `QuantizeLinear.__init__` 核对参数形状与 dtype。
3. 「是否需校准」只对「激活也量化」的 FP8 方案标「是」，且只有静态激活变体才真正需要——动态激活方案（`use_activation_scale=False`、`calibration_mode="max"` 仅用于收集）推理时不强制。

### 任务二：解释 per-tensor FP8 为何可能需要 calibrate

写一段 3–5 句话，覆盖以下要点（参考 4.2.4）：

1. FP8 e4m3 动态范围只有 ±448，LLM 激活有少量极大离群值。
2. 若用动态 scale，离群值撑大 scale → 正常值欠精度。
3. 校准用代表性数据（ShareGPT）预先固定每层激活 max → 逐层 `q_calibration_scale`。
4. 流程：`mlc_llm calibrate --model ... --dataset ShareGPT ... -o scales`（[cli/calibrate.py:10-80](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/calibrate.py#L10-L80)）驱动 `AsyncMLCEngine` 在 `calibration_mode="max"` 下跑，`PerTensorQuantizeLinear.forward` 经 `mlc_llm.calibration_observer` extern 把激活 max 回传给 `CalibrationObserver.callback`（[interface/calibrate.py:32-50](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/calibrate.py#L32-L50)），用 `np.maximum` 逐元素累积，最后 `save_params` 落盘。
5. 之后用 `e4m3_e4m3_f16`（`inference`）带这些 scale 推理（[per_tensor_quantization.py:417-422](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/per_tensor_quantization.py#L417-L422)）。

> 待本地验证：在支持的 GPU 上对比「动态激活 scale」与「校准后静态 scale」的 perplexity，静态通常更优。

## 6. 本讲小结

- AWQ 是**非对称** int4 量化（多一个零点 `qzeros`），粒度为 per-channel-out × per-group-in，**加载预量化权重**（路径 B），所以它的 `quantize_model` 不写 `QuantizeMapping`。
- FP8 是**浮点**格式（e4m3 范围 ±448、e5m2 范围 ±57344），一个元素占 1 字节、无需按位打包；`per-tensor` 用一个标量 scale，`block-scale` 用 2D 块级 scale（精度更高、主要服务 DeepSeek/Hopper）。
- per-tensor FP8 **可能需要校准**：因为激活离群值会撑爆动态 scale；`calibration_mode="max"` 收集、`"inference"` 消费，桥梁是 `mlc_llm.calibration_observer` 与 `cli/calibrate.py`。
- `ft-quant` 是 **NVIDIA + CUTLASS 专用**的 int4 方案（存 int8、用 `ft_preprocess_weight` 重排布局），且是**混合体**：不满足形状约束的层自动回退 group-quant。
- `no_quant`（`q0f16`/`q0bf16`/`q0f32`）只转 dtype 不压缩，工厂返回空 `QuantizeMapping`，用作基线/调试/小模型。
- 整套体系靠 `kind` 桥接「名字查配置」与「工厂查函数」，靠 `supports_*` 开关决定每个模型支持哪些 kind（[model_quantization.py:125-136](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/model_quantization.py#L125-L136)）。

## 7. 下一步学习建议

U5 量化单元到此结束。你现在已经掌握 MLC 全部六大 kind（`no-quant` / `group-quant` / `awq` / `ft-quant` / `per-tensor-quant` / `block-scale-quant`）的存储格式、改图机制与适用场景。建议：

1. **横向收口**：回头读一遍 [quantization.py 的完整注册表](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/quantization/quantization.py)，确认你能对每一个名字说出它的 kind、dtype 与典型用途——这是量化选型的基础功。
2. **向下游衔接 U6（对话模板与协议）**：量化发生在编译期，而 `mlc-chat-config.json` 里的 `quantization` 字段会把这个选择一路带到运行期。下一讲（u6-l1）开始讲对话模板，届时你会看到量化方案如何被记录进配置契约。
3. **想深入 FP8/校准**：可继续读 [interface/calibrate.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/calibrate.py) 与 `python/mlc_llm/op/cutlass.py`、`python/mlc_llm/op/triton.py` 里 fp8 groupwise GEMM 的派发，理解「硬件 kernel 可用性」如何反向决定量化方案能否落地。
4. **想验证理解**：本讲的对比表请亲手填一遍；如果本地有 GPU，尝试用 `mlc_llm calibrate` 对一个小模型跑一次校准，观察输出的 scale 文件。
