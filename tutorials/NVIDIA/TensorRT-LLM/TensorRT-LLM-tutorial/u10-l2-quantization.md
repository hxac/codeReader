# 量化机制

## 1. 本讲目标

本讲深入 TensorRT-LLM 的「量化（Quantization）」子系统。读完本讲，你应当能够：

- 说清楚**为什么** LLM 推理需要量化，以及 FP8 / FP4 / INT8 AWQ 等几类常见配方的差别。
- 读懂 `QuantConfig` / `QuantAlgo` / `QuantMode` 这套「纯描述层」：它只描述「用什么量化方案」，本身不做任何计算。
- 追踪一份 HuggingFace checkpoint 里的 `hf_quant_config.json` 是如何被**归一化**成统一的 `QuantConfig` 的——尤其是 ModelOpt（0.x legacy 与 1.x flat 两种形状）与 llm-compressor（compressed-tensors）这两大「生产者」的不同输入路径。
- 知道量化描述最终在 `custom_ops` 里**落到了哪些 kernel**，并能从命名规律推断一个 op 服务于哪种量化方案。
- 为一个模型手工起草一份 FP8 量化配置，并解释每一层是如何被描述与排除的。

本讲依赖 u5-l1（模型架构范式 `DecoderModelForCausalLM`）与 u4-l3（`ModelConfig` 与 `PretrainedConfig` 的关系），并承接 u4-l1（`TorchLlmArgs` 配置体系）中提到的「部署期确定、checkpoint 不记录」的开关思想。

## 2. 前置知识

### 2.1 为什么要量化：显存墙与带宽墙

LLM 推理在 decode 阶段几乎总是**访存带宽受限（memory-bandwidth bound）**的。每生成一个 token，都要把全部权重和正在增长的 KV cache 从显存搬到 SM（GPU 流式多处理器）。权重越「宽」（BF16 每个元素占 16 bit），单位时间能搬的字节数就越吃紧，吞吐就越低。

量化的核心思想是：**用更窄的低精度类型表示权重 / 激活 / KV cache**，从而

- 少占显存（可塞下更大模型或更长上下文）；
- 少搬字节（访存受限时直接近乎线性提速）；
- 走更快的低精度 Tensor Core（如 FP8 / FP4 MMA）。

代价是精度损失。这个损失靠「缩放因子（scale）」来补偿。

### 2.2 缩放粒度：从粗到细

低精度类型的动态范围很窄（例如 FP8 e4m3 最大值约 448）。为了让高精度的真实数值「塞进」这个窄范围，需要计算一个 scale：

\[ x_{\text{quant}} = \mathrm{round}\!\left(\frac{x}{s}\right), \qquad s = \frac{\max(|x|)}{x_{\text{max, lowprec}}} \]

scale 在**多大一块数据上共享一个**，就是「缩放粒度」，粒度越细、精度越高但额外存储与计算越多：

| 粒度 | 共享范围 | 典型方案 |
|------|---------|---------|
| per-tensor | 整个张量一个 scale | `FP8`（per-tensor QDQ） |
| per-channel / per-token | 权重按列 / 激活按行各一个 | `FP8_PER_CHANNEL_PER_TOKEN`（rowwise） |
| per-block / per-group | 每 N 个元素一个 | `FP8_BLOCK_SCALES`（128×128）、AWQ（group_size=128）、NVFP4（group_size=16） |

TensorRT-LLM 用字符串枚举 `QuantAlgo` 把「位宽 × 粒度 × 方法」打包成易读的名字（如 `W4A16_AWQ` = 权重 4 位、激活 16 位、AWQ 方法）。

### 2.3 几个低精度数据类型速记

- **FP8 e4m3**：8 位浮点（4 位指数、3 位尾数），范围 ±448，TRT-LLM 里 FP8 的默认格式。
- **NVFP4 / e2m1**：4 位浮点，搭配一个 FP32 全局 scale + 每 16 个元素一个 FP8 分组 scale，精度显著高于朴素 INT4。
- **MXFP4 / MXFP8**：OCP MX 标准（microscaling），按 32 元素块共享一个 E8M0 scale。
- **INT4/INT8 + AWQ/GPTQ**：整数量化，配 AWQ（activation-aware）或 GPTQ（基于二阶信息的训练后量化）方法确定 scale 与 zero point。

如果你对这些名词还不熟，记住一句话即可：**本讲不教如何训练量化模型，只讲 TRT-LLM 如何「描述 + 加载 + 跑」一个已经量化好的 checkpoint。**

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tensorrt_llm/quantization/mode.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/quantization/mode.py) | `QuantAlgo` 字符串枚举（方案名）与 `QuantMode` 位标志（C++/kernel 消费的布尔集合），二者之间的翻译表 |
| [tensorrt_llm/models/modeling_utils.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/models/modeling_utils.py) | `QuantConfig`（全局描述）与 `LayerQuantConfig`（逐层混合精度描述）两个 Pydantic 类 |
| [tensorrt_llm/quantization/modelopt_config.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/quantization/modelopt_config.py) | ModelOpt 产出的两种 JSON 形状（legacy / flat）归一化为统一的 `quantization` 内层 dict |
| [tensorrt_llm/models/quant_config_utils.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/models/quant_config_utils.py) | 把 llm-compressor 的 compressed-tensors 配置翻译成 `QuantConfig` |
| [tensorrt_llm/_torch/model_config.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/model_config.py) | `ModelConfig` 在加载期消费上述归一化结果，按 `quant_method` 分流到不同生产者路径 |
| [tensorrt_llm/quantization/functional.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/quantization/functional.py) | 量化工具函数，如 INT4/INT8 权重的 MMA 布局预处理 |
| [tensorrt_llm/_torch/custom_ops/trtllm_gen_custom_ops.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/trtllm_gen_custom_ops.py) | 量化 MoE block-scale kernel（FP4/FP8/MXFP4 等融合 GEMM+路由+combine）的 Python 入口与 autotuner 包装 |
| [docs/source/features/quantization.md](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/features/quantization.md) | 官方特性文档：支持的配方矩阵、用法、ModelOpt 离线量化指引 |

一句话总览三层结构：

> **描述层（QuantConfig / QuantAlgo / QuantMode）→ 加载归一化层（modelopt_config / quant_config_utils / model_config）→ kernel 层（custom_ops）**。描述层只回答「是什么」，加载层回答「checkpoint 里写的是什么、怎么读进来」，kernel 层回答「真正怎么算」。

## 4. 核心概念与源码讲解

### 4.1 QuantConfig：量化的纯描述层

#### 4.1.1 概念说明

`QuantConfig` 是一个**可序列化的「量化方案说明书」**，挂在 `PretrainedConfig` 上随 checkpoint 一起保存。它的职责是回答：

- 权重和激活用哪种量化算法？（`quant_algo`）
- KV cache 是否量化、用哪种？（`kv_cache_quant_algo`）
- 分组大小？（`group_size`）
- 哪些模块**不**量化？（`exclude_modules`）

关键点：`QuantConfig` **本身不含任何 tensor，也不做任何量化运算**。它只是一份「合同」，告诉运行时「这些权重是 FP8 的、请用 FP8 kernel 来算」。真正执行量化/反量化的，是 4.3 节的 custom ops。

这套描述分两个层次：

- `QuantConfig`：**全局**默认方案，绝大多数层共用。
- `LayerQuantConfig`：**逐层**混合精度（`MIXED_PRECISION`），允许不同层用不同方案，内部用 `quantized_layers: Dict[str, QuantConfig]` 给出每层覆盖。

#### 4.1.2 核心流程

描述层内部有一次重要的「枚举 → 位标志」翻译：

```text
QuantAlgo (字符串枚举, 人读)            ──┐
   例: "FP8_BLOCK_SCALES"                 │  QuantMode.from_quant_algo()
                                          └──►  QuantMode (IntFlag 位标志, 机器读)
                                                例: FP8_1x128_128 | PER_GROUP
```

- `QuantAlgo` 是给人和配置文件用的字符串（如 `"FP8"`、`"NVFP4"`、`"W4A16_AWQ"`）。
- `QuantMode` 是一组布尔位的按位或（如「权重是否量化」「激活是否量化」「是否 per-group」「是否 FP8 KV cache」）。C++ 运行时和 kernel 用 `if (mode.has_fp8_kv_cache())` 这种布尔查询来分支。

`QuantConfig.quant_mode` 这个 `cached_property` 就负责做这次翻译：

```python
# modeling_utils.py（节选）
@cached_property
def quant_mode(self) -> QuantModeWrapper:
    quant_mode_list = [
        QuantMode.from_quant_algo(self.quant_algo, self.kv_cache_quant_algo)
    ]
    return QuantModeWrapper(quant_mode_list)
```

它把 `(quant_algo, kv_cache_quant_algo)` 这一对枚举翻译成位标志，并缓存起来供下游反复查询。

#### 4.1.3 源码精读

**(1) `QuantAlgo` 字符串枚举**——人类可读的方案全集。[mode.py:L23-L51](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/quantization/mode.py#L23-L51) 定义了所有合法方案名。命名规律是 `W<权重点数>A<激活点数>[_METHOD]`，少数例外是纯浮点方案：

```python
class QuantAlgo(StrEnum, metaclass=BaseEnumMeta):
    W8A16 = auto()        # 权重 8 位 / 激活 16 位（weight-only INT8）
    W4A16 = auto()        # weight-only INT4
    W4A16_AWQ = auto()    # INT4 + AWQ 方法
    W4A16_GPTQ = auto()   # INT4 + GPTQ 方法
    W8A8_SQ_PER_CHANNEL = auto()   # smooth-quant
    FP8 = auto()                       # 全 FP8 per-tensor
    FP8_PER_CHANNEL_PER_TOKEN = auto() # FP8 rowwise
    FP8_BLOCK_SCALES = auto()          # FP8 128x128 block（DeepSeek 系）
    NVFP4 = auto()                     # 4-bit 浮点
    MXFP8 = auto()                     # microscaling FP8
    W4A8_MXFP4_FP8 = auto()            # MXFP4 权重 + FP8 激活
    NO_QUANT = auto()
    # ... 共约 30 种
```

旁注：紧接着的 [mode.py:L54-L65](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/quantization/mode.py#L54-L65) 给出几个有用的派生集合，例如：

```python
KV_CACHE_QUANT_ALGO_LIST = [QuantAlgo.FP8, QuantAlgo.INT8, QuantAlgo.NVFP4]
```

它界定了 **KV cache 量化** 只允许这三种——这正是 [docs/source/features/quantization.md](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/features/quantization.md) 文档里 `KvCacheConfig(dtype='fp8')` / `'nvfp4'` 两个开关的理论根据。

**(2) `QuantMode` 位标志**——机器消费的布尔集合。[mode.py:L68-L108](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/quantization/mode.py#L68-L108) 定义了一组按位标志。**注意第 69 行的告警注释**：

```python
class QuantMode(IntFlag):
    # [WARNING] KEEP BELOW DEFINITION IN SYNC WITH cpp/tensorrt_llm/common/quantization.h
    INT4_WEIGHTS = auto()
    INT8_WEIGHTS = auto()
    ACTIVATIONS = auto()
    PER_CHANNEL = auto()
    PER_TOKEN = auto()
    PER_GROUP = auto()
    INT8_KV_CACHE = auto()
    FP8_KV_CACHE = auto()
    FP8_QDQ = auto()
    # ...
```

这条注释与 u4-l3 提到的「Python 枚举须与 C++ 头文件同步」的纪律一致：`QuantMode` 是给 C++/kernel 看的，每一位的数值必须和 `cpp/tensorrt_llm/common/quantization.h` 完全对齐，改一个就要两边一起改。位标志的查询方法都是布尔判断，例如 [mode.py:L169-L185](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/quantization/mode.py#L169-L185)：

```python
def has_fp8_kv_cache(self):     return self._any(self.FP8_KV_CACHE)
def has_fp8_block_scales(self): return self._any(self.FP8_1x128_128)
def has_nvfp4(self):            return self._any(self.NVFP4)
```

**(3) 翻译表 `QuantMode.from_quant_algo`**。[mode.py:L379-L456](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/quantization/mode.py#L379-L456) 是一张「字符串方案 → 位标志」的大查表，节选几条关键映射：

```python
if quant_algo == QuantAlgo.FP8:                       # per-tensor QDQ
    quant_mode = QuantMode.from_description(use_fp8_qdq=True)
elif quant_algo == QuantAlgo.FP8_PER_CHANNEL_PER_TOKEN:  # rowwise
    quant_mode = QuantMode.from_description(use_fp8_rowwise=True)
elif quant_algo == QuantAlgo.FP8_BLOCK_SCALES:            # 128x128
    quant_mode = QuantMode.from_description(use_fp8_block_scales=True)
elif quant_algo == QuantAlgo.NVFP4:
    quant_mode = QuantMode.from_description(use_nvfp4=True)
# ...
if kv_cache_quant_algo == QuantAlgo.FP8:
    quant_mode = quant_mode.set_fp8_kv_cache()
```

可以看到「KV cache 量化」是**正交叠加**的：它和权重/激活方案相互独立，最后用 `set_fp8_kv_cache()` 在位标志上额外或上一个 `FP8_KV_CACHE` 位。

**(4) `QuantConfig` 类本体**。[modeling_utils.py:L126-L168](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/models/modeling_utils.py#L126-L168) 定义了它的字段，全部是可序列化的标量：

```python
class QuantConfig(StrictBaseModel):
    quant_algo: Optional[QuantAlgo] = ...          # 量化算法
    kv_cache_quant_algo: Optional[QuantAlgo] = ... # KV cache 量化算法
    group_size: Optional[int] = 128                # 分组大小
    smoothquant_val: float = 0.5                   # smooth-quant 的 alpha
    clamp_val: Optional[List[float]] = ...         # FP8 rowwise 的钳位值
    has_zero_point: bool = False                   # 是否带 zero point（AWQ）
    pre_quant_scale: bool = False                  # 是否带 pre-quant scale（AWQ）
    exclude_modules: Optional[List[str]] = ...     # 不量化的模块名模式
    mamba_ssm_cache_dtype: Optional[str] = ...     # Mamba SSM 缓存类型
```

注意 `quant_algo` 字段被标记了 `json_schema_extra={"telemetry": True}`（[modeling_utils.py:L129-L132](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/models/modeling_utils.py#L129-L132)），这意味着它是遥测可见字段——与 AGENTS.md 里「新字段需 telemetry/privacy CODEOWNER 审批」的纪律挂钩。

**(5) 排除模块的匹配逻辑**。[modeling_utils.py:L245-L282](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/models/modeling_utils.py#L245-L282) 的 `is_module_excluded_from_quantization` 解释了 `exclude_modules` 如何工作：它沿模块名逐级上溯（`a.b.c → a.b → a`），对每个候选名用 `fnmatch` 通配或 `re:` 前缀的正则匹配。因此写一个父模块名会**隐式**排除它下面所有子模块，这点对排查「为什么某层没被量化」很关键。

**(6) 逐层混合精度 `LayerQuantConfig`**。[modeling_utils.py:L300-L327](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/models/modeling_utils.py#L300-L327) 定义了它：顶层 `quant_algo=MIXED_PRECISION`，真正的逐层方案藏在 `quantized_layers: Dict[str, QuantConfig]` 里。它在 `model_post_init` 里预算每层的 `QuantMode` 缓存起来（`auto_quant_mode`），避免前向时反复翻译。

#### 4.1.4 代码实践

**实践目标**：亲手构造并检视一个 `QuantConfig`，确认它「只是描述、不做计算」。

**操作步骤**（示例代码，非项目原有）：

```python
# 示例代码
from tensorrt_llm.models.modeling_utils import QuantConfig
from tensorrt_llm.quantization.mode import QuantAlgo, QuantMode

# 1) 构造一份 FP8 per-tensor + FP8 KV cache 的描述
qc = QuantConfig(quant_algo=QuantAlgo.FP8,
                 kv_cache_quant_algo=QuantAlgo.FP8,
                 exclude_modules=["lm_head"])

# 2) 看它如何被翻译成位标志
mode = QuantMode.from_quant_algo(qc.quant_algo, qc.kv_cache_quant_algo)
print("fp8_qdq       ?", mode.has_fp8_qdq())        # True
print("fp8_kv_cache  ?", mode.has_fp8_kv_cache())   # True
print("per_group     ?", mode.has_per_group_scaling())  # False

# 3) 验证「排除」匹配
print(qc.is_module_excluded_from_quantization("lm_head"))  # True
print(qc.is_module_excluded_from_quantization("model.layers.0.mlp"))  # False
```

**需要观察的现象**：`QuantConfig` 对象里没有任何 tensor 或 CUDA 调用，纯粹是字段与查询；`quant_mode` 的布尔查询结果会随 `quant_algo` 的取值改变。

**预期结果**：上述断言全部成立。如果你把 `quant_algo` 改成 `QuantAlgo.FP8_BLOCK_SCALES`，`has_per_group_scaling()` 不一定为 True（block scales 用的是专门的 `FP8_1x128_128` 位，走 `has_fp8_block_scales()`），这正是「位标志比字符串更精细」的体现。

> 待本地验证：本实践需要安装好 tensorrt_llm（可 `import`）。若环境无 GPU，纯描述层的构造与查询仍可在 CPU 上运行；但下游 kernel 落点（4.3 节）需 GPU。

#### 4.1.5 小练习与答案

**练习 1**：`QuantAlgo.FP8` 与 `QuantAlgo.FP8_BLOCK_SCALES` 翻译出的 `QuantMode` 有何不同？

**答案**：前者只置 `FP8_QDQ` 位（per-tensor，整张量一个 scale）；后者置 `FP8_1x128_128` 位（128×128 block，每块一组 scale）。kernel 会据此选完全不同的 GEMM 实现。

**练习 2**：为什么 `QuantMode` 要和 C++ 头文件 `quantization.h` 保持同步，而 `QuantAlgo` 不用？

**答案**：`QuantMode` 是 `IntFlag`，每一位的具体数值会被序列化并跨 Python/C++ 边界传递给 kernel；数值一旦错位，C++ 会误判方案。`QuantAlgo` 是字符串枚举，只做名字匹配，不依赖具体数值，故无需数值同步（但仍需保证名字集合一致）。

**练习 3**：`exclude_modules=["model.layers.0"]` 会不会排除 `model.layers.0.self_attn.q_proj`？

**答案**：会。`is_module_excluded_from_quantization` 会把模块名按 `.` 逐级上溯，`model.layers.0.self_attn.q_proj` 上溯到 `model.layers.0` 时命中，故整个子树都被排除。

---

### 4.2 配置归一化：从多种 checkpoint 格式到统一 QuantConfig

#### 4.2.1 概念说明

`QuantConfig` 描述的是「理想形状」，但现实里 checkpoint 的量化配置 JSON 长什么样，取决于**谁生产的**。TensorRT-LLM 至少要兼容三类生产者：

1. **NVIDIA ModelOpt**（自家工具）：会产出 `hf_quant_config.json`，且历史上换过格式——
   - **legacy（0.x）**：`{producer, quantization: {quant_algo, kv_cache_quant_algo, ...}}`，方案信息嵌在 `quantization` 内层 dict。
   - **flat（1.x，compressed-tensors 风格）**：`{producer, quant_method="modelopt", quant_algo, kv_cache_scheme, ignore, config_groups, ...}`，方案字段摊平在顶层。
2. **llm-compressor**（社区工具）：产出 `quant_method="compressed-tensors"` 的 `config_groups` 结构。
3. **裸 HuggingFace 风格**：直接写 `quant_method="fp8"` / `"mxfp4"` / `"nvfp4"` / `"mxfp8"` 等朴素字段。

如果让下游 kernel 直接面对这么多形状，会到处是 `if/else`。因此 TRT-LLM 在加载期做一次**归一化**：无论哪种生产者，都先翻译成一个 `QuantConfig` 对象，后续只认这一个统一抽象。这是典型的「**适配器模式**」：外部格式多变，内部接口单一。

#### 4.2.2 核心流程

加载期的归一化入口在 `ModelConfig`，按 `quant_method` 分流：

```text
            checkpoint 的量化配置 JSON
                      │
          ┌───────────┼────────────────────────┐
          ▼           ▼                        ▼
   is_modelopt?   quant_method?          compressed-tensors?
          │           │                        │
  read_modelopt_   原生 fp8/mxfp4/      update_quant_config_
  quant_config()   nvfp4/mxfp8 分支     from_compressed_tensors()
          │           │                        │
          └───────────┴────────────────────────┘
                      ▼
              统一的 QuantConfig（+ 可选 LayerQuantConfig）
```

ModelOpt 路径内部还有一次「legacy ↔ flat」的形状折叠：`read_modelopt_quant_config` 把两种形状都压成 legacy 的 `quantization` 内层 dict，因为所有下游调用点都只消费这个形状（见模块文档字符串 [modelopt_config.py:L3-L16](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/quantization/modelopt_config.py#L3-L16)）。

#### 4.2.3 源码精读

**(1) ModelOpt 形状探测**。[modelopt_config.py:L23-L33](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/quantization/modelopt_config.py#L23-L33) 的 `is_modelopt_quant_config` 用两种信号识别 ModelOpt 产物：

```python
def is_modelopt_quant_config(raw):
    if str(raw.get("quant_method", "")).lower().startswith("modelopt"):
        return True
    return (raw.get("producer") or {}).get("name") == "modelopt"
```

**(2) KV cache scheme 的两种编码**。ModelOpt 1.x 对 `kv_cache_scheme` 有两种写法（[modelopt_config.py:L42-L68](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/quantization/modelopt_config.py#L42-L68)）：FP8 写成 dict `{"type":"float","num_bits":8}`，而 NVFP4/INT8 写成裸字符串。`_kv_cache_scheme_to_algo` 把它们都翻译回 legacy 的算法名（`"FP8"`/`"NVFP4"`/`"INT8"`），并**对无法识别的形状打 warning**，避免静默丢掉 KV cache 量化设置：

```python
_KV_SCHEME_DICT_MAP = {("float", 8): "FP8", ("float", 4): "NVFP4", ("int", 8): "INT8"}
_KV_SCHEME_STRING_ALGOS = {"FP8", "NVFP4", "INT8"}

def _kv_cache_scheme_to_algo(scheme):
    if scheme is None: return None
    if isinstance(scheme, str):
        algo = scheme.upper()
        if algo in _KV_SCHEME_STRING_ALGOS: return algo
    elif isinstance(scheme, dict):
        mapped = _KV_SCHEME_DICT_MAP.get((scheme.get("type"), scheme.get("num_bits")))
        if mapped is not None: return mapped
    logger.warning(f"Unrecognized 'kv_cache_scheme' {scheme!r}; KV-cache quant disabled.")
    return None
```

**(3) 主归一化函数**。[modelopt_config.py:L71-L103](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/quantization/modelopt_config.py#L71-L103) 的 `read_modelopt_quant_config`：legacy 形状直接取 `raw["quantization"]`；flat 形状则**改名**（`ignore → exclude_modules`、`kv_cache_scheme → kv_cache_quant_algo`）并丢弃 1.x 专属元数据（`producer`/`quant_method`/`config_groups`）。末尾还有一条别名规整：

```python
# Canonicalize the fp8_pb_wo legacy alias.
if result.get("quant_algo") == "fp8_pb_wo":
    result["quant_algo"] = "FP8_BLOCK_SCALES"
```

**(4) inline 与文件配置一致性检查**。[modelopt_config.py:L106-L140](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/quantization/modelopt_config.py#L106-L140) 的 `warn_if_inline_diverges` 体现了「**文件优先**」原则：当 `config.json` 内联的 `quantization_config` 与独立的 `hf_quant_config.json` 不一致时，以文件为准、内联只打 warning。这能帮你定位「为什么我改了 inline 却没生效」——因为文件才是权威。

**(5) compressed-tensors 翻译**。[quant_config_utils.py:L22-L108](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/models/quant_config_utils.py#L22-L108) 的 `update_quant_config_from_compressed_tensors` 把 llm-compressor 的 `config_groups` 结构就地翻译成 `QuantConfig` 字段，核心是三段按「权重位宽 × 策略」的匹配：

```python
# 权重 8 位：channel 策略 → rowwise FP8；block 策略 → FP8_BLOCK_SCALES(必须 group=128)
if weights_quant_config["num_bits"] == 8:
    if weights_quant_strategy == "channel":
        quant_config.quant_algo = QuantAlgo.FP8_PER_CHANNEL_PER_TOKEN
    elif weights_quant_strategy == "block":
        quant_config.quant_algo = QuantAlgo.FP8_BLOCK_SCALES
        if group_size != 128: raise ValueError(...)   # 只支持 128
# 权重 4 位 float + tensor_group → NVFP4（必须 group=16）
elif (weights_quant_config["num_bits"] == 4
      and weights_quant_config.get("type") == "float"
      and weights_quant_strategy == "tensor_group"):
    quant_config.quant_algo = QuantAlgo.NVFP4
```

可以看到，归一化层还承担了**合法性校验**：超出支持的组合直接抛 `ValueError`，而不是让错误的配置流到 kernel 才崩。

**(6) ModelConfig 的分流入口**。[model_config.py:L373-L380](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/model_config.py#L373-L380) 的 `load_modelopt_quant_config` 读文件、归一化、再交 `_build_modelopt_quant_config` 构造 `QuantConfig`。而 [model_config.py:L484-L573](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/model_config.py#L484-L573) 的 `load_hf_quant_config` 则是按 `quant_method` 逐一分支的总入口——modelopt 优先走 modelopt 路径，其余 `fp8`/`mxfp4`/`nvfp4`/`mxfp8`/`compressed-tensors` 各自填 `quant_algo` 与默认 `exclude_modules`。

一个值得注意的细节在 [model_config.py:L498-L519](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/model_config.py#L498-L519)：FP8 block scaling 会**强制排除** `*kv_b_proj*` 等模块，注释解释了原因——128×128 block 边界不一定对齐 per-head 维度（如 GLM-5 的 `qk_nope_head_dim=192`），scale 张量无法干净地按 head reshape。这是「描述层选择受 kernel 布局约束」的一个鲜活例子。

#### 4.2.4 代码实践

**实践目标**：用归一化函数把两种 ModelOpt 形状的 JSON 都翻译成同一个 dict，验证它们殊途同归。

**操作步骤**（示例代码）：

```python
# 示例代码
from tensorrt_llm.quantization.modelopt_config import (
    read_modelopt_quant_config, is_modelopt_quant_config)

legacy = {"producer": {"name": "modelopt", "version": "0.20"},
          "quantization": {"quant_algo": "FP8", "kv_cache_quant_algo": "FP8"}}
flat   = {"producer": {"name": "modelopt", "version": "1.0"},
          "quant_method": "modelopt",
          "quant_algo": "FP8",
          "kv_cache_scheme": {"type": "float", "num_bits": 8},
          "ignore": ["lm_head"]}

assert is_modelopt_quant_config(legacy) and is_modelopt_quant_config(flat)
n_legacy = read_modelopt_quant_config(legacy)
n_flat   = read_modelopt_quant_config(flat)
print(n_legacy)  # {'quant_algo': 'FP8', 'kv_cache_quant_algo': 'FP8'}
print(n_flat)    # {'quant_algo': 'FP8', 'kv_cache_quant_algo': 'FP8', 'exclude_modules': ['lm_head']}
```

**需要观察的现象**：两种形状归一化后，`quant_algo` 与 `kv_cache_quant_algo` 字段名与取值一致；flat 的 `ignore` 被改名成 `exclude_modules`，dict 形式的 `kv_cache_scheme` 被翻译成字符串 `"FP8"`。

**预期结果**：两份归一化结果的 `quant_algo`/`kv_cache_quant_algo` 相同；flat 多出 `exclude_modules`。还可以参照 `tests/unittest/llmapi/test_llm_quant.py` 的 `test_read_modelopt_quant_config_invalid_raises` 断言：传入非 modelopt 配置会抛 `ValueError`。

> 待本地验证：归一化是纯 Python 字典操作，无需 GPU。

#### 4.2.5 小练习与答案

**练习 1**：如果一个 flat ModelOpt 配置里 `kv_cache_scheme` 写成 `"int4"`（不被支持），会发生什么？

**答案**：`_kv_cache_scheme_to_algo` 既不在字符串集合里、也不在 dict 映射里，于是打一条 warning 并返回 `None`，即 KV cache 量化被静默关闭（但会在日志里留下痕迹）。这正是函数注释强调的「让被丢弃的设置浮到日志里」。

**练习 2**：为什么 `read_modelopt_quant_config` 要把 `fp8_pb_wo` 改名成 `FP8_BLOCK_SCALES`？

**答案**：`fp8_pb_wo` 是 ModelOpt 历史遗留别名（block-scales weight-only），归一化时统一成当前标准名 `FP8_BLOCK_SCALES`，避免下游再为旧名写特判分支。

**练习 3**：compressed-tensors 路径里，如果权重 `num_bits=8`、`strategy="block"`、`group_size=64`，会怎样？

**答案**：会抛 `ValueError`。TRT-LLM 的 `FP8_BLOCK_SCALES` 只支持 `group_size=128`，归一化层在 [quant_config_utils.py:L57-L59](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/models/quant_config_utils.py#L57-L59) 处显式拒绝其他值，确保只有 kernel 能跑的组合能通过。

---

### 4.3 量化算子：描述如何落到 kernel

#### 4.3.1 概念说明

描述层说「要 FP8」，kernel 层回答「FP8 的矩阵乘具体怎么做」。在 PyTorch 后端，这些高性能 kernel 以 **`torch.library.custom_op`** 的形式注册，统一挂在 `trtllm::` 命名空间下，分布在 `tensorrt_llm/_torch/custom_ops/` 多个文件里：

- `torch_custom_ops.py`：量化 **GEMM**（通用矩阵乘），如 `fp8_rowwise_gemm`、`nvfp4_gemm_cutlass`、`weight_only_quant_gemm`、`fp8_block_scaling_gemm`。
- `trtllm_gen_custom_ops.py`：量化 **MoE 融合 kernel**（路由 + 双 GEMM + combine 一把梭），如 `fp8_block_scale_moe_runner`、`fp4_block_scale_moe_runner`。
- `cute_dsl_custom_ops.py`：Blackwell 上的 CuTe DSL 实现，如 `cute_dsl_nvfp4_gemm_blackwell`、`cute_dsl_fp8_gemm_blackwell`。
- `cpp_custom_ops.py` / `flashinfer_custom_ops.py`：含部分量化相关融合算子（如 `flashinfer_fused_add_rmsnorm_quant`）。

此外，`quantization/functional.py` 提供的不是 kernel，而是**权重布局预处理工具**——把 checkpoint 里按常规布局存的 INT4/INT8 权重，重排成 Tensor Core MMA 要求的交错布局。它通常在加载期（而非前向期）一次性完成。

#### 4.3.2 核心流程

一个量化 kernel 在前向期的典型数据流：

```text
输入激活 x  ──┐
              ├──► 量化 GEMM op  ──►  输出（BF16）
权重 W_quant ──┤      (内部：scale 对齐 → 低精度 MMA → 反量化累加)
权重 scale ────┘
```

对 MoE 来说， op 把「路由 topk → dispatch → 两个融合 GEMM（gate_up + down）→ combine 加权」整条链路融合进单个 kernel，以减少中间张量的显存来回。这类融合 kernel 的特点是：输入参数列表很长（权重、scale、alpha/beta/clamp、路由参数……），并且都带一个 **autotuner**，在首次调用时为当前 SM 代际与张量形状挑最佳 tactic（策略）。

命名规律（重要，实践任务会用）：

```text
trtllm::<精度配方>_<算子>[_<后端/代际>]

精度配方 = { fp8, fp4, nvfp4, mxfp4, mxfp8, w4a8_mxfp4_fp8, e4m3, mxe2m1, bf16, weight_only_quant, ... }
算子     = { gemm, bmm, moe_runner, fused_add_rmsnorm_quant, ... }
后端/代际 = { cutlass, cublaslt, trtllmgen, blackwell, ... }（可选）
```

例如 `trtllm::fp8_block_scaling_gemm`（FP8 块缩放 GEMM）、`trtllm::nvfp4_gemm_cutlass`（NVFP4 GEMM，CUTLASS 实现）、`trtllm::fp4_block_scale_moe_runner`（FP4 块缩放 MoE 融合 kernel）。看到名字就能反推它服务哪种 `QuantAlgo`。

#### 4.3.3 源码精读

**(1) 一个量化 MoE op 的注册与签名**。[trtllm_gen_custom_ops.py:L1015-L1039](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/trtllm_gen_custom_ops.py#L1015-L1039) 注册了 `trtllm::fp8_block_scale_moe_runner`，它的参数列表就是「量化 MoE 全家桶」：

```python
@torch.library.custom_op("trtllm::fp8_block_scale_moe_runner", mutates_args=())
def fp8_block_scale_moe_runner(routing_logits, routing_bias,
        hidden_states, hidden_states_scale,           # 激活 + 激活 scale（FP8）
        gemm1_weights, gemm1_weights_scale,           # 第一个 GEMM 权重 + scale
        gemm2_weights, gemm2_weights_scale,           # 第二个 GEMM 权重 + scale
        num_experts, top_k, n_group, topk_group,
        intermediate_size, local_expert_offset, local_num_experts,
        routed_scaling_factor, routing_method_type,
        topk_weights=None, topk_ids=None, act_type=0,
        gemm1_clamp_limit=None, output=None,
        tune_max_num_tokens=8192, use_dp=False) -> torch.Tensor:
```

可以看到：权重与激活都是**成对的 `(tensor, scale)`**——这就是「块缩放」在接口上的体现：低精度张量必须配它的 scale 才能被正确解释。`mutates_args=()` 声明它不修改输入（纯函数语义），便于被 `torch.compile` 与 CUDA Graph 安全捕获（详见 u10-l4）。

**(2) Runner + Autotuner 模式**。[trtllm_gen_custom_ops.py:L885-L905](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/trtllm_gen_custom_ops.py#L885-L905) 的 `FP8BlockScaleMoERunner.forward` 把 Python 张量转交给 C++ 真正的 `torch.classes.trtllm.FP8BlockScaleMoERunner`：

```python
def forward(self, inputs, tactic=[-1, -1], output=None):
    args = FP8BlockScaleMoEInputs(*inputs)
    kernel_runner = self.get_runner()      # 懒加载 C++ runner 单例
    return kernel_runner.run_moe(
        args.routing_logits, args.routing_bias, args.hidden_states,
        args.hidden_states_scale, args.gemm1_weights, args.gemm1_weights_scale,
        args.gemm2_weights, args.gemm2_weights_scale, ...,
        tactic, args.topk_weights, args.topk_ids,
        self.gemm1_clamp_limit_value, output)
```

这呼应 u2-l3 的「Python 调度、C++ 加速」：Python 侧负责组参数、挑 tactic，真正算的是 C++ 类。autotuner 在 op 入口（[trtllm_gen_custom_ops.py:L1091-L1096](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/trtllm_gen_custom_ops.py#L1091-L1096)）用一组 dummy 输入为当前形状选最佳 tactic 并缓存。

**(3) 同文件的量化 MoE 家族**。除 FP8 外，本文件还注册了多种精度配方的 MoE 融合 kernel，命名严格遵循上面的规律：

| op 名 | 精度配方 | 行号 |
|--------|---------|------|
| `trtllm::fp4_block_scale_moe_runner` | NVFP4 权重 + 块 scale | [L622](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/trtllm_gen_custom_ops.py#L622) |
| `trtllm::fp8_block_scale_moe_runner` | FP8 块 scale | [L1015](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/trtllm_gen_custom_ops.py#L1015) |
| `trtllm::mxe4m3_mxe2m1_block_scale_moe_runner` | MXFP4(E2m1) 权重 + MXFP8(E4m3) 激活 | [L1372](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/trtllm_gen_custom_ops.py#L1372) |
| `trtllm::e4m3_mxe2m1_block_scale_moe_runner` | MXFP4 权重 + FP8(E4m3) 激活 | [L1693](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/trtllm_gen_custom_ops.py#L1693) |
| `trtllm::bf16_mxe2m1_block_scale_moe_runner` | MXFP4 权重 + BF16 激活 | [L2016](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/trtllm_gen_custom_ops.py#L2016) |
| `trtllm::fp8_fp4_block_scale_moe_runner` | FP8 激活 + FP4 权重 混合 | [L2325](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/trtllm_gen_custom_ops.py#L2325) |

对比 `QuantAlgo` 枚举就能发现一一对应关系：例如 `W4A8_MXFP4_FP8`（MXFP4 权重 + FP8 激活）对应 `e4m3_mxe2m1_block_scale_moe_runner`。**描述层的方案名 ↔ kernel 层的 op 名**，这就是「描述如何落到 kernel」的真相。

**(4) 量化 GEMM op 家族（torch_custom_ops.py）**。除了 MoE，普通线性层走更轻量的 GEMM op，命名同样有规律（此处只列注册行，详见 [tensorrt_llm/_torch/custom_ops/torch_custom_ops.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/torch_custom_ops.py)）：

| op 名 | 服务对象 |
|--------|---------|
| `trtllm::fp8_rowwise_gemm` ([L513](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/torch_custom_ops.py#L513)) | `FP8_PER_CHANNEL_PER_TOKEN` |
| `trtllm::fp8_block_scaling_gemm` ([L2006](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/torch_custom_ops.py#L2006)) | `FP8_BLOCK_SCALES` |
| `trtllm::nvfp4_gemm_cutlass` / `nvfp4_gemm_cublaslt` ([L927](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/torch_custom_ops.py#L927)/[L867](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/torch_custom_ops.py#L867)) | `NVFP4`（两种后端实现可选） |
| `trtllm::w4a8_mxfp4_fp8_gemm` ([L1528](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/torch_custom_ops.py#L1528)) | `W4A8_MXFP4_FP8` |
| `trtllm::weight_only_quant_gemm` ([L1621](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/torch_custom_ops.py#L1621)) | weight-only INT4/INT8（AWQ/GPTQ） |
| `trtllm::finegrained_mixed_dtype_gemm` ([L1720](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/torch_custom_ops.py#L1720)) | `MIXED_PRECISION` 逐层 |
| `trtllm::quantize_e4m3_per_tensor` ([L2635](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/torch_custom_ops.py#L2635)) | 在线把激活量化成 FP8 |

**(5) 权重布局预处理工具**。[tensorrt_llm/quantization/functional.py:L21-L36](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/quantization/functional.py#L21-L36) 的 `preprocess_weights_for_mixed_gemm` 是 INT4/INT8（AWQ/GPTQ）权重的「进场重排」：它根据 SM 代际（Ampere/Hopper/Blackwell）和激活/权重点数，选不同的交错置换表（`permutation_map`，[functional.py:L38-L108](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/quantization/functional.py#L38-L108)），把 sub-byte 权重重排成 Tensor Core MMA 期望的内存布局：

```python
def preprocess_weights_for_mixed_gemm(tensor, quant_mode, act_dtype, sm_=-1, ...):
    sm_ = sm_ if sm_ > 0 else get_sm_version()
    # 3-D inputs (MoE) on Hopper+ 或 SM120/121 复用 SM80 interleaved 布局
    if (len(tensor.shape) == 3 and sm_ >= 90) or sm_ >= 120:
        sm_ = 80
    ...
    # 选 {16_8, 16_4, 8_4} 之一作为 sub-byte 行置换表
```

这解释了一个常见困惑：为什么 AWQ 模型加载时要做一次权重 permute——因为 checkpoint 存的是「逻辑布局」，而 kernel 要的是「MMA 物理布局」，二者只在特定 SM 代际上等价。它是**加载期一次性**的预处理，不会进前向热路径。

#### 4.3.4 代码实践

**实践目标**：盘点量化相关 custom ops 的命名规律，并建立「`QuantAlgo` → op 名」的映射直觉。

**操作步骤**：

1. 在仓库内用 Grep 列出全部量化 op 注册：
   ```bash
   grep -rn 'torch.library.custom_op("trtllm::' tensorrt_llm/_torch/custom_ops/ \
     | grep -E 'fp8|fp4|nvfp4|mxfp|quant|gemm|moe_runner'
   ```
2. 按本节给出的命名公式 `trtllm::<精度配方>_<算子>[_<后端>]` 给每个结果标注三段。
3. 对照 [mode.py:L23-L51](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/quantization/mode.py#L23-L51) 的 `QuantAlgo` 枚举，把每个 op 配到一个方案名上。

**需要观察的现象**：同一个方案可能有多个 op（如 `NVFP4` 同时有 `nvfp4_gemm_cutlass` 与 `nvfp4_gemm_cublaslt` 两种后端实现，以及 MoE 用的 `fp4_block_scale_moe_runner`）；后缀 `blackwell` 的 op 仅在 SM100+ 可用（受 `IS_CUTLASS_DSL_AVAILABLE` 守卫，见 [custom_ops/__init__.py:L50-L71](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/__init__.py#L50-L71)）。

**预期结果**：你能口述「FP8 per-tensor → 走通用路径、FP8 block scales → `fp8_block_scaling_gemm` / MoE 走 `fp8_block_scale_moe_runner`、NVFP4 → `nvfp4_gemm_*` 或 `fp4_block_scale_moe_runner`」。

> 待本地验证：Grep 不需要 GPU；真正调用这些 op 需要 Blackwell/Hopper 及对应 wheel。

#### 4.3.5 小练习与答案

**练习 1**：`trtllm::nvfp4_gemm_cutlass` 与 `trtllm::nvfp4_gemm_cublaslt` 为什么并存？

**答案**：同一个 `NVFP4` 方案有多种后端实现可选（CUTLASS、cuBLASLt），运行时按硬件、形状、autotuner 结果择优。命名后缀正是用来区分「同方案的不同实现」。

**练习 2**：为什么量化 MoE op 的输入里 `(weights, weights_scale)` 总是成对出现，而 BF16 GEMM 不需要 scale？

**答案**：低精度张量（FP8/FP4）的动态范围窄，必须配 scale 才能还原真实数值，scale 本身也是 kernel 的输入张量。BF16 范围足够，直接存真实值，无需 scale。

**练习 3**：`preprocess_weights_for_mixed_gemm` 在什么时候被调用，为什么不在每次前向都跑？

**答案**：它在**模型加载期**被调用一次，把 checkpoint 的逻辑布局重排成 Tensor Core MMA 的物理布局并固化进显存。前向期反复跑只会增加无谓开销，所以布局预处理是「一次付出、长期受益」。

---

## 5. 综合实践

把三个最小模块串起来，完成一个「**为模型设计 FP8 量化配置并追踪它如何落地**」的小任务。

**任务背景**：假设你有一个 BF16 的 decoder-only 模型 `MyModel`（沿用 u5-l3 的假想模型），你想给它配一份 FP8 per-tensor 量化 + FP8 KV cache 的部署配置，并理解每个字段会如何影响后续加载与 kernel 选择。

**步骤 1 — 设计配置（描述层）**。用 `QuantConfig` 表达：

```python
# 示例代码
from tensorrt_llm.models.modeling_utils import QuantConfig
from tensorrt_llm.quantization.mode import QuantAlgo

quant_config = QuantConfig(
    quant_algo=QuantAlgo.FP8,                  # 权重+激活均 FP8 per-tensor
    kv_cache_quant_algo=QuantAlgo.FP8,         # KV cache 也走 FP8
    exclude_modules=["lm_head"],               # 最后一层投影不量化，保精度
)
```

**步骤 2 — 解释每一层如何被描述**。写下：
- 所有 `decoder_layer` 的 `*q_proj/*k_proj/*v_proj/*o_proj/*gate_proj/*up_proj/*down_proj` 共用上面的全局 `quant_algo=FP8`（因为 `LayerQuantConfig` 为空，没有逐层覆盖）。
- `lm_head` 因 `exclude_modules=["lm_head"]` 被跳过（参考 [modeling_utils.py:L245-L282](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/models/modeling_utils.py#L245-L282) 的逐级上溯匹配）。
- 如果你想让某些敏感层保留 BF16，应改用 `MIXED_PRECISION` + `LayerQuantConfig.quantized_layers`，给敏感层单独写 `QuantConfig(quant_algo=None)`。

**步骤 3 — 追踪落地路径**。沿加载链路写一段说明：
1. 若你用 ModelOpt 离线量化得到 checkpoint，其 `hf_quant_config.json` 会被 [model_config.py:L373-L380](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/model_config.py#L373-L380) 读入，经 `read_modelopt_quant_config` 归一化、`_build_modelopt_quant_config` 构造出与你手写等价的 `QuantConfig`。
2. 前向期，普通线性层命中 `trtllm::` 命名空间下的 FP8 GEMM op；若有 MoE 层，则命中 `trtllm::fp8_block_scale_moe_runner`（若你改用 block scales）。
3. KV cache 部分由 `KvCacheConfig(dtype='fp8')` 在运行时开启，对应 `QuantMode.has_fp8_kv_cache()` 为真，KV cache manager 会按 FP8 分配块（承接 u7-l1）。

**步骤 4 — 自检**。回答两个问题：
- 这份配置里 `group_size` 还重要吗？（答：对 per-tensor FP8 不重要，它只在 per-group 方案如 `FP8_BLOCK_SCALES`/AWQ/NVFP4 下生效。）
- 如果改主意要 NVFP4，需要改哪三处？（答：`quant_algo=NVFP4`、`group_size=16`、并确认目标硬件是 Blackwell/Hopper 且支持 NVFP4 GEMM op。）

> 待本地验证：本综合实践以源码阅读与配置起草为主，不需要真正跑量化；若要端到端验证，需按 [docs/source/features/quantization.md](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/features/quantization.md) 的 ModelOpt 流程先离线量化出 checkpoint。

## 6. 本讲小结

- 量化是「用更窄的低精度类型表示权重/激活/KV cache」以翻越显存墙与带宽墙，核心补偿手段是**缩放因子**，缩放粒度（per-tensor / per-channel / per-block）决定精度与开销的折中。
- TensorRT-LLM 的量化分三层：**描述层（`QuantConfig`/`QuantAlgo`/`QuantMode`）→ 加载归一化层（`modelopt_config`/`quant_config_utils`/`model_config`）→ kernel 层（`custom_ops`）**。
- `QuantConfig` 是**纯描述**、不含计算；`QuantAlgo`（字符串）经 `QuantMode.from_quant_algo` 翻译成 `QuantMode`（位标志，与 C++ 头文件须同步）供 kernel 分支查询。
- 加载层用适配器模式把多种 checkpoint 格式（ModelOpt legacy/flat、compressed-tensors、原生 fp8/mxfp4/nvfp4/mxfp8）归一化成统一 `QuantConfig`，并在归一化时做合法性校验（如 `FP8_BLOCK_SCALES` 只认 `group_size=128`、NVFP4 只认 16）。
- kernel 层的量化 op 统一挂在 `trtllm::` 命名空间下，命名遵循 `trtllm::<精度配方>_<算子>[_<后端>]`，与 `QuantAlgo` 方案名基本一一对应；权重布局预处理（`preprocess_weights_for_mixed_gemm`）在加载期一次性完成。
- KV cache 量化（`FP8`/`INT8`/`NVFP4` 三选一）是**正交叠加**在权重/激活方案之上的，由 `kv_cache_quant_algo` 单独描述、由 `set_*_kv_cache()` 在位标志上叠加。

## 7. 下一步学习建议

- **u10-l1（MoE 架构与后端）**：本讲的 MoE 量化 kernel（`fp8_block_scale_moe_runner` 等）正是 MoE 后端选择的一部分，那里会讲清「量化方案 × SM 代际」如何决定 MoE 后端，以及 autotuner 在 MoE 里的完整角色。
- **u10-l3（投机解码）**与 **u10-l4（CUDA Graph / torch.compile）**：量化 op 都用 `@torch.library.custom_op` + `register_fake` 注册，正是为了能被 `torch.compile` 与 CUDA Graph 安全捕获——本讲埋下的「`mutates_args=()` 纯函数语义」伏笔会在那里收回。
- **u12-l2（自定义算子与内核）**：想新增一个量化 kernel 时，应先读 `docs/source/torch/adding_custom_kernels.md`，按 cpp/torch/triton/cute-dsl 四类来源择优，并遵守本讲的命名与注册约定。
- **继续阅读源码**：建议顺读 [tensorrt_llm/_torch/custom_ops/torch_custom_ops.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/torch_custom_ops.py) 里某个具体 GEMM op（如 `fp8_block_scaling_gemm`）的完整实现，看它如何从 `QuantMode` 分支选 kernel；以及 [tests/unittest/models/test_quant_config_utils.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tests/unittest/models/test_quant_config_utils.py) 里对 compressed-tensors 归一化的断言，加深对合法/非法组合的直觉。
