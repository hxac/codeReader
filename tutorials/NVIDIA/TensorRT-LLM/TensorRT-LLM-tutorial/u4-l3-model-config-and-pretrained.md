# ModelConfig 与 PretrainedConfig

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 **PyTorch 后端里 `ModelConfig` 与「PretrainedConfig」各自代表什么**，以及为什么运行时要在 HF 配置之外再包一层。
- 识别项目里**两个名字都叫 `PretrainedConfig` 的类**（一个是 HF `transformers` 的、一个是 `tensorrt_llm` 自带的旧版），并理解它们的边界。
- 用 `QuantConfig` / `LayerQuantConfig` / `QuantAlgo` 描述模型权重的量化方案（FP8、FP4/AWQ、MXFP4 等）。
- 看懂 `SpeculativeDecodingMode` 这种「标志位集合」是如何用 `IntFlag` 表达多种投机解码模式的。

本讲承接 [u4-l1](./u4-l1-llm-args-hierarchy.md)（`TorchLlmArgs` 运行时配置）与 [u4-l2](./u4-l2-model-defaults-llm-utils.md)（模型默认值合并）。`TorchLlmArgs` 描述的是「怎么跑」（并行、调度、KV cache 大小），而本讲的 `ModelConfig` 描述的是「跑的是什么模型、用什么精度跑」——这是加载期到前向期之间的桥梁（见 [u3-l3](./u3-l3-model-engine-forward.md) 的 `ModelLoader`）。

## 2. 前置知识

- **HuggingFace checkpoint 与 `config.json`**：每个 HF 模型仓库根目录都有一个 `config.json`，它记录了模型的「结构」——隐藏层大小、层数、注意力头数、词表大小、`architectures`（如 `LlamaForCausalLM`）等。加载权重前，`transformers` 库会先把它解析成一个 `transformers.PretrainedConfig` 对象。
- **量化（Quantization）**：把原本用 16 位浮点（BF16/FP16）存储的权重/激活，压成 8 位（FP8/INT8）或 4 位（FP4），用更少显存换更快推理。代价是少量精度损失。不同算法（FP8、AWQ、MXFP4 等）就是不同的「压缩配方」。
- **`IntFlag`（位标志枚举）**：Python 的 `enum.IntFlag` 允许把多个选项「按位或」组合，比如 `A | B`。它常用来表示「多个互不排斥的开关」，本讲的 `SpeculativeDecodingMode` 和底层的 `QuantMode` 都用到它。
- **Pydantic `StrictBaseModel`**：[u4-l1](./u4-l1-llm-args-hierarchy.md) 提过，`StrictBaseModel` 带 `extra="forbid"`，拼写错误的字段会直接报错。本讲的 `QuantConfig` / `LayerQuantConfig` 都继承自它。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tensorrt_llm/_torch/model_config.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/model_config.py) | 定义 PyTorch 后端的运行时配置 `ModelConfig`：包裹 HF 配置 + 量化 + 并行 + 注意力/MoE 后端等运行时开关，提供 `from_pretrained()` 从 checkpoint 加载。 |
| [tensorrt_llm/models/modeling_utils.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/models/modeling_utils.py) | 定义旧版（legacy 引擎流）的 `PretrainedConfig`，以及共享的 `QuantConfig` / `LayerQuantConfig` / `SpeculativeDecodingMode`。 |
| [tensorrt_llm/models/__init__.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/models/__init__.py) | `tensorrt_llm.models` 包的导出门面：re-export 上述类，并声明 `MODEL_MAP`（PyTorch 后端里刻意为空）。 |
| [tensorrt_llm/quantization/mode.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752e174940f164/tensorrt_llm/quantization/mode.py) | `QuantAlgo` 枚举（所有量化算法的字符串名）与 `QuantMode`（底层位标志）。 |

---

## 4. 核心概念与源码讲解

> 本讲按「自底向上」展开：先讲清楚被包裹的 `PretrainedConfig`（4.1）与它携带的量化描述 `QuantConfig`（4.2），再讲为什么运行时要在它们之外再包一层 `ModelConfig`（4.3），最后补充一个典型标志位集合 `SpeculativeDecodingMode`（4.4）。

### 4.1 PretrainedConfig：模型结构的权威描述

#### 4.1.1 概念说明

一个 LLM checkpoint 有两样东西：**权重张量**（`*.safetensors`）和**结构描述**（`config.json`）。`PretrainedConfig` 就是后者的 Python 表达——它回答「这个模型长什么样、用什么精度」：隐藏层多大、多少层、几个注意力头、激活函数是什么、词表多大、是否量化……

这里有一个**最容易踩坑的点（务必记住）**：项目里存在**两个名字都叫 `PretrainedConfig` 的类**，分属两个不同的流程：

| 类 | 来源 | 用在哪 | 谁来填字段 |
|----|------|--------|-----------|
| `transformers.PretrainedConfig`（及其子类如 `LlamaConfig`） | HuggingFace `transformers` | **PyTorch 后端**：`ModelConfig.pretrained_config` 持有它 | HF 库从 `config.json` 自动解析 |
| `tensorrt_llm.models.modeling_utils.PretrainedConfig` | 本仓库自带 | **旧版 TensorRT 引擎流**（legacy） | 用户/脚本手动构造 |

在 PyTorch 后端里，模型实现（如 `modeling_llama.py`）从 `transformers` 直接导入 `PretrainedConfig`：

```python
# tensorrt_llm/_torch/models/modeling_llama.py:8-9 （示例代码：截取 import）
from transformers import (AutoProcessor, AutoTokenizer, Llama4Config,
                          Llama4VisionModel, LlamaConfig, PretrainedConfig)
```

也就是说，PyTorch 后端的「模型结构描述」直接复用 HF 生态，TRT-LLM 不重新发明轮子。本仓库自带的旧版 `PretrainedConfig`（下面 4.1.3 精读）是早期「引擎构建」流程遗留下来的自包含配置类，把结构和量化揉在一个对象里。

#### 4.1.2 核心流程

旧版 `PretrainedConfig` 对象的生命周期：

```text
config.json (或 dict)
   │  from_dict() / from_json_file()
   ▼
PretrainedConfig(architecture, dtype, hidden_size, num_hidden_layers, ...)
   │  ── 内部把 quantization 参数转成 QuantConfig
   │  ── 计算 head_size、rotary_embedding_dim 等派生量
   ▼
运行时读取：.quant_mode / .quant_algo / .kv_dtype / .mapping
```

注意它有「缺省自动补全」：例如 `num_key_value_heads` 不给就默认等于 `num_attention_heads`（MHA 退化为不分组），`intermediate_size` 不给就默认 `hidden_size * 4`，`head_size` 不给就用 `hidden_size // num_attention_heads` 算出来。

#### 4.1.3 源码精读

**① 旧版 `PretrainedConfig` 的构造签名**——注意它是**普通 Python 类**（不是 Pydantic、不是 dataclass），靠 `__init__` 手动赋值：

[ tensorrt_llm/models/modeling_utils.py:373-400](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/models/modeling_utils.py#L373-L400) 定义了 `PretrainedConfig.__init__`，必填 `architecture / dtype / hidden_size / num_hidden_layers / num_attention_heads`，其余（词表大小、激活函数、norm_epsilon、位置编码类型、KV 头数等）都有默认值。

其中有一段关键的「量化参数 → `QuantConfig`」归一化逻辑：如果传入的 `quantization` 是 `dict`，就转成 `QuantConfig(**dict)`：

[ tensorrt_llm/models/modeling_utils.py:436-441](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/models/modeling_utils.py#L436-L441) —— 把 `quantization` 统一成 `QuantConfig` 或 `LayerQuantConfig`，存到 `self.quantization`。

**② 派生属性 `quant_mode` / `quant_algo`**：旧版 `PretrainedConfig` 把量化读取能力委托给内部的 `QuantConfig`：

[ tensorrt_llm/models/modeling_utils.py:549-555](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/models/modeling_utils.py#L549-L555) —— `quant_mode` 与 `quant_algo` 这两个 property 直接转发到 `self.quantization`。

**③ 序列化 `to_dict` / 反序列化 `from_dict`**：

[ tensorrt_llm/models/modeling_utils.py:490-506](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/models/modeling_utils.py#L490-L506) —— `from_dict` 通过 `MODEL_MAP[config['architecture']]` 找到模型类、取其 `config_class` 再实例化；`to_dict` 则把 `__dict__` 深拷贝并把 `mapping`/`quantization` 序列化成 dict。

> 注意上面 `from_dict` 用到的 `MODEL_MAP`，在 PyTorch 后端里是空的（见 4.1.3 末尾），所以这条旧路径在 PyTorch 流程里走不通——这正好印证了「两个 PretrainedConfig 互不通用」。

**④ 两个 `MODEL_MAP` 的关系**：本仓库的 `models/__init__.py` 里：

[ tensorrt_llm/models/__init__.py:19-21](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/models/__init__.py#L19-L21) —— 注释明确写道「Architecture registry is intentionally empty: the PyTorch backend resolves model classes via `tensorrt_llm._torch.models`」。也就是说 PyTorch 后端的模型注册走的是另一套表（见 [u5-l2](./u5-l2-model-registry-and-auto-discovery.md) 的 `MODEL_CLASS_MAPPING`），与旧版 `MODEL_MAP` 分道扬镳。

#### 4.1.4 代码实践

**实践目标**：亲手验证「PyTorch 后端里 `PretrainedConfig` 来自 HF」，并对比旧版 `PretrainedConfig` 的字段。

**操作步骤**：

1. 在已安装 `tensorrt_llm` 与 `transformers` 的环境里运行：

   ```python
   # 示例代码
   from transformers import PretrainedConfig as HFPretrainedConfig
   from tensorrt_llm.models import PretrainedConfig as LegacyPretrainedConfig

   print("HF PretrainedConfig 模块:", HFPretrainedConfig.__module__)
   print("Legacy PretrainedConfig 模块:", LegacyPretrainedConfig.__module__)

   # 构造一个最小旧版 PretrainedConfig，看它的字段
   cfg = LegacyPretrainedConfig(
       architecture="LlamaForCausalLM", dtype="bfloat16",
       hidden_size=64, num_hidden_layers=2, num_attention_heads=4)
   print("num_key_value_heads(默认):", cfg.num_key_value_heads)   # 预期 4
   print("intermediate_size(默认):", cfg.intermediate_size)       # 预期 256 = 64*4
   print("head_size(默认):", cfg.head_size)                       # 预期 16 = 64//4
   print("quant_algo:", cfg.quant_algo)                           # 预期 None
   ```

2. 在 `tensorrt_llm/models/__init__.py` 中确认 `MODEL_MAP` 为空字典。

**需要观察的现象**：两个 `PretrainedConfig` 来自不同模块（`transformers` vs `tensorrt_llm.models.modeling_utils`）；旧版构造时未传的 `num_key_value_heads`/`intermediate_size`/`head_size` 都被自动补上了默认值。

**预期结果**：`num_key_value_heads == 4`、`intermediate_size == 256`、`head_size == 16`、`quant_algo is None`。

**无法运行时**：若 `import tensorrt_llm` 因 CUDA/C++ 依赖失败，本实践可降级为「源码阅读型实践」——直接阅读 4.1.3 引用的源码段，在纸上填出各默认值即可，运行结果标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 PyTorch 后端的 `ModelConfig` 要包的是 HF 的 `transformers.PretrainedConfig`，而不是本仓库旧版的 `modeling_utils.PretrainedConfig`？

> **参考答案**：因为 PyTorch 后端的模型实现（`_torch/models/modeling_*.py`）直接复用 HF 的权重格式与配置类（如 `LlamaConfig`），HF 生态已经能从 `config.json` 解析出完整的结构描述；自带的旧版 `PretrainedConfig` 是为早期「先建引擎再推理」流程设计的自包含类，`MODEL_MAP` 在 PyTorch 后端为空，两套体系不通用。复用 HF 配置能避免重复造轮子，也便于直接加载 HF 权重。

**练习 2**：旧版 `PretrainedConfig.__init__` 中，`intermediate_size` 不传时的默认值是多少？依据在哪一行？

> **参考答案**：默认 `hidden_size * 4`（见 [tensorrt_llm/models/modeling_utils.py:424-426](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/models/modeling_utils.py#L424-L426)）。

---

### 4.2 QuantConfig / LayerQuantConfig：量化的描述能力

#### 4.2.1 概念说明

`QuantConfig` 是「量化配方的描述」，它**只描述、不执行**——告诉系统「这个模型用 FP8 权重 + FP8 KV cache」，至于具体怎么反量化、调用哪个 kernel，是前向时才决定的。它有两条主线：

- **全局 `QuantConfig`**：整模型统一一种量化方案。核心字段 `quant_algo`（权重量化算法）+ `kv_cache_quant_algo`（KV cache 量化算法）+ `group_size`（分组大小）等。
- **逐层 `LayerQuantConfig`**：用于「混合精度」——不同层用不同算法（比如 MLP 用 NVFP4、注意力用 FP8）。它把每个模块名映射到一个 `QuantConfig`。

`QuantConfig` 是新旧两套流程**共享**的（4.1 里两个 `PretrainedConfig` 都用它），所以它定义在 `modeling_utils.py` 里并被 `ModelConfig` 复用。

#### 4.2.2 核心流程

```text
checkpoint 里的量化信息（hf_quant_config.json / config.json / dtypes.json）
   │  ModelConfig.from_pretrained() / load_hf_quant_config() 解析
   ▼
QuantConfig(quant_algo=..., kv_cache_quant_algo=..., group_size=..., exclude_modules=...)
   │  .quant_mode  ← cached_property，由 QuantAlgo 推导出底层 QuantMode 位标志
   ▼
运行时判断：is_module_excluded_from_quantization(name) 决定某模块是否跳过量化
```

`QuantAlgo` 是所有量化算法的「字符串枚举」（`StrEnum`），常见的有：`FP8`、`FP8_BLOCK_SCALES`、`NVFP4`、`W4A16_AWQ`、`W4A8_AWQ`、`W8A8_SQ_PER_CHANNEL`（SmoothQuant）、`MXFP8`、`MIXED_PRECISION` 等（完整列表见 4.2.3）。命名规律是 `W{权重大小}A{激活大小}_{算法}`，例如 `W4A16_AWQ` = 4 位权重、16 位激活、AWQ 算法。

`quant_mode` 是把 `quant_algo` 这种「人类可读字符串」翻译成底层 C++ 认识的位标志（`QuantMode`，`IntFlag`）。

#### 4.2.3 源码精读

**① `QuantConfig` 的核心字段**：

[ tensorrt_llm/models/modeling_utils.py:126-168](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/models/modeling_utils.py#L126-L168) —— 继承 `StrictBaseModel`（Pydantic），关键字段包括 `quant_algo`、`kv_cache_quant_algo`、`group_size`（默认 128）、`smoothquant_val`（SmoothQuant 的 α，默认 0.5）、`has_zero_point`/`pre_quant_scale`（AWQ 专用）、`exclude_modules`（跳过量化的模块名模式）。

**② `quant_mode` 派生属性**：

[ tensorrt_llm/models/modeling_utils.py:170-185](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/models/modeling_utils.py#L170-L185) —— 用 `@cached_property` 缓存，通过 `QuantMode.from_quant_algo(quant_algo, kv_cache_quant_algo)` 把算法字符串翻译成底层位标志。缓存意味着同一对象只算一次。

**③ `is_module_excluded_from_quantization`**：决定某个模块是否被排除在量化之外，支持 glob 和 `re:` 正则两种模式：

[ tensorrt_llm/models/modeling_utils.py:245-282](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/models/modeling_utils.py#L245-L282) —— 沿模块名「逐级上溯」匹配祖先，因此列出父模块（如 `model.layers.1`）会连带排除其所有子模块。

**④ `LayerQuantConfig`（混合精度逐层）**：

[ tensorrt_llm/models/modeling_utils.py:300-332](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/models/modeling_utils.py#L300-L332) —— `quantized_layers: Dict[str, QuantConfig]` 把每个模块名映射到一个 `QuantConfig`，并在 `model_post_init` 里为每层预计算 `QuantMode` 缓存到 `_auto_quant_mode`。

**⑤ `QuantAlgo` 枚举全貌**：

[ tensorrt_llm/quantization/mode.py:23-51](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/quantization/mode.py#L23-L51) —— 列出全部量化算法字符串常量（`W8A16`、`W4A16`、`W4A16_AWQ`、`W4A8_AWQ`、`W8A16_GPTQ`、`W4A16_GPTQ`、`W8A8_SQ_PER_CHANNEL`、各 SmoothQuant 插件变体、`FP8`、`FP8_PER_CHANNEL_PER_TOKEN`、`FP8_BLOCK_SCALES`、`INT8`、`MIXED_PRECISION`、`NVFP4`、`MXFP8`、`MXFP4` 系列、`NO_QUANT` 等）。注意 `QuantConfig` 描述的就是这些算法名。

#### 4.2.4 代码实践

**实践目标**：构造几个 `QuantConfig`，直观感受它「能描述哪些量化算法」，并查看 `quant_mode` 的推导。

**操作步骤**：

```python
# 示例代码
from tensorrt_llm.models import QuantConfig, LayerQuantConfig, QuantAlgo

# 1) 描述一个 FP8 权重 + FP8 KV cache 的全局量化
qc = QuantConfig(quant_algo=QuantAlgo.FP8, kv_cache_quant_algo=QuantAlgo.FP8)
print("FP8 quant_mode:", qc.quant_mode)
print("需要 modelopt 量化?", qc._requires_modelopt_quantization)

# 2) AWQ：4 位权重 + 16 位激活
awq = QuantConfig(quant_algo=QuantAlgo.W4A16_AWQ, group_size=128,
                  has_zero_point=True, pre_quant_scale=True)
print("AWQ group_size:", awq.group_size)

# 3) 混合精度逐层
layer = LayerQuantConfig(
    quant_algo=QuantAlgo.MIXED_PRECISION, kv_cache_quant_algo=QuantAlgo.FP8,
    quantized_layers={"model.layers.0.mlp": QuantConfig(quant_algo=QuantAlgo.NVFP4)})
print("mlp 层 algo:", layer.quantized_layers["model.layers.0.mlp"].quant_algo)
```

**需要观察的现象**：同一份 `QuantConfig` 仅靠改 `quant_algo` 就能切换成完全不同的量化方案；`LayerQuantConfig` 能让不同模块各用各的算法。

**预期结果**：`FP8 quant_mode` 打印出一个非零的 `QuantMode` 位标志；`_requires_modelopt_quantization` 对 `FP8` 返回 `True`（见 [tensorrt_llm/models/modeling_utils.py:198-209](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/models/modeling_utils.py#L198-L209)）。

**无法运行时**：标注「待本地验证」，改为阅读 4.2.3 的 `QuantAlgo` 枚举源码，在纸上列举至少 6 种算法及其含义。

#### 4.2.5 小练习与答案

**练习 1**：`QuantConfig` 与 `LayerQuantConfig` 各自适合什么场景？

> **参考答案**：`QuantConfig` 描述整模型统一一种量化方案（全局 FP8 / AWQ 等）；`LayerQuantConfig` 用于混合精度（`quant_algo=MIXED_PRECISION`），把每个模块名映射到不同的 `QuantConfig`，使不同层可以各用各的算法。

**练习 2**：`quant_algo`（字符串枚举）与 `quant_mode`（位标志）为什么要分两层？

> **参考答案**：`QuantAlgo` 是面向人类/配置文件的可读字符串（`FP8`、`NVFP4`…），便于序列化和填写；`QuantMode` 是面向 C++/kernel 的紧凑位标志（`IntFlag`），便于在算子内部用「按位与」快速判断「权重是否 4 位」「是否有 FP8 KV cache」等。`QuantConfig.quant_mode` 这个 cached_property 就是把前者翻译成后者的桥梁。

---

### 4.3 ModelConfig：运行时的包裹层

#### 4.3.1 概念说明

回到本讲核心问题：**为什么运行时要在 HF 配置之外再包一层 `ModelConfig`？**

HF 的 `transformers.PretrainedConfig` 描述的是「checkpoint 自带的、与硬件无关的模型结构」，它**不知道**：

- 这台机器有几张卡、要不要张量并行（TP）/流水线并行（PP）——这是 `Mapping`。
- 想用哪个注意力后端（TRTLLM / FlashInfer）、哪个 MoE 后端（CUTLASS / TRTLLM / TRITON）——这是 `attn_backend` / `moe_backend`。
- 权重要不要量化、怎么量化——这是 `QuantConfig`。
- 单步最多算多少 token、要不要开 CUDA Graph——这是 `max_num_tokens` / `use_cuda_graph`。

这些「运行时才确定、且 checkpoint 本身不会记录」的开关，需要一个专门的容器——这就是 [tensorrt_llm/_torch/model_config.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/model_config.py) 里的 `ModelConfig`。它的设计哲学是：**`pretrained_config`（来自 checkpoint，不可变）+ 一堆运行时开关（来自 `llm_args`，加载完即冻结）**。

`ModelConfig` 是一个 `@dataclass`，并且用 `Generic[TConfig]` 做泛型——`TConfig` 绑定到 `transformers.PretrainedConfig`，所以 `ModelConfig.pretrained_config` 持有的就是真实模型类对应的那个 HF 配置子类（如 `LlamaConfig`）。它**创建后即冻结**（`_frozen=True`），保证前向期间配置不会被意外改动。

#### 4.3.2 核心流程

`ModelConfig` 的创建由 `from_pretrained()` 完成，它是 `ModelLoader` 的核心一步（见 [u3-l3](./u3-l3-model-engine-forward.md)）：

```text
checkpoint_dir (HF 模型路径)
   │  ① load_pretrained_config()  → HF PretrainedConfig（结构）
   │  ② 解析量化文件：hf_quant_config.json / config.json / dtypes.json
   │     → QuantConfig (+ 可选 LayerQuantConfig)
   │  ③ resolve_moe_backend()：根据架构 + 显卡 SM 版本把 "AUTO" 解析成具体后端
   │  ④ 传入运行时开关（mapping/attn_backend/max_num_tokens 等 kwargs）
   ▼
ModelConfig(pretrained_config=..., quant_config=..., **kwargs)
   │  __post_init__：补 is_encoder_decoder / is_generation / moe_max_num_tokens
   │  _frozen = True   ← 冻结，之后只读
   ▼
DecoderModelForCausalLM.__init__(config=ModelConfig) 持有它，前向时反复读取
```

冻结后，只有少数字段（`extra_attrs`、`pretrained_config`、`quant_config`）允许修改（见 4.3.3），其余字段改写会抛 `AttributeError`。

#### 4.3.3 源码精读

**① `ModelConfig` 是带泛型的 dataclass**：

[ tensorrt_llm/_torch/model_config.py:131-137](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/model_config.py#L131-L137) —— `@dataclass(kw_only=True)` + `Generic[TConfig]`，首字段 `pretrained_config: Optional[TConfig]` 就是 HF 配置；接着 `mapping`、`quant_config`。

`TConfig` 的定义在文件顶部，绑定到 HF 的配置基类：

[ tensorrt_llm/_torch/model_config.py:59](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/model_config.py#L59) —— `TConfig = TypeVar("TConfig", bound=transformers.PretrainedConfig)`。这一行是「`ModelConfig` 包的是 HF 配置」的铁证。

**② 运行时开关字段**——这些都是 HF checkpoint 里没有的：

[ tensorrt_llm/_torch/model_config.py:156-163](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/model_config.py#L156-L163) —— `max_num_tokens`（单步算力预算，默认 8192）、`max_seq_len`、`moe_max_num_tokens`、`attn_backend`（默认 `'TRTLLM'`）、`moe_backend`（默认 `'CUTLASS'`）等。

**③ 冻结机制 `_frozen` + `__setattr__`**：

[ tensorrt_llm/_torch/model_config.py:213-228](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/model_config.py#L213-L228) —— 一旦 `_frozen=True`，除白名单（`_frozen` / `extra_attrs` / `pretrained_config` / `quant_config`）外的字段都禁止赋值，强制抛错。注释明确说这是「为了防止无意间改动属性」。

**④ `from_pretrained()` 是创建入口**：

[ tensorrt_llm/_torch/model_config.py:821-822](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/model_config.py#L821-L822) —— classmethod `from_pretrained(cls, checkpoint_dir, trust_remote_code=False, **kwargs)`。

它先用 `load_pretrained_config` 拿到 HF 配置：

[ tensorrt_llm/_torch/model_config.py:893-897](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/model_config.py#L893-L897) —— 从 `checkpoint_dir` 加载 HF `pretrained_config`，包在文件锁 `config_file_lock` 里防止多进程并发下载竞争。

随后解析量化（依次尝试 `hf_quant_config.json` → `config.json` 内联 `quantization_config` → `dtypes.json`），解析 MoE 后端，最后实例化并冻结：

[ tensorrt_llm/_torch/model_config.py:1197-1202](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/model_config.py#L1197-L1202) —— `model_config = cls(pretrained_config=..., quant_config=..., quant_config_dict=..., **kwargs)` 然后 `model_config._frozen = True`。

**⑤ 桥接 C++：`get_bindings_model_config()`**：

[ tensorrt_llm/_torch/model_config.py:1204-1257](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/model_config.py#L1204-L1257) —— 把 Python `ModelConfig` 翻译成 C++ `bindings.ModelConfig`（用于 KV cache 容量计算等 C++ 侧逻辑），会根据 `mapping` 的 TP/CP 对 `num_heads`/`hidden_size` 做 `ceil_div` 切分——这正是 [u2-l3](./u2-l3-cpp-core-and-nanobind.md) 讲的「Python 调度、C++ 加速」又一实例。

**⑥ 模型层如何消费它**：

[ tensorrt_llm/_torch/models/modeling_utils.py:381-384](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_utils.py#L381-L384) —— `DecoderModelForCausalLM.__init__(self, model, *, config: ModelConfig[TConfig], ...)` 直接把 `config` 存为 `self.model_config`，之后所有层都从 `config.pretrained_config` 读结构、从 `config.quant_config` 读量化。

#### 4.3.4 代码实践

**实践目标**：对比 `ModelConfig` 与旧版/ HF 的 `PretrainedConfig` 字段差异，亲手回答「为什么运行时需要再包一层」。

**操作步骤**：

```python
# 示例代码：字段对比（无需 GPU，纯类型内省）
import dataclasses
from tensorrt_llm._torch.model_config import ModelConfig

# ModelConfig 是 dataclass，可直接列出字段
mc_fields = {f.name for f in dataclasses.fields(ModelConfig)}
print("ModelConfig 字段数:", len(mc_fields))

# 关键运行时开关（HF 配置里没有的）
runtime_only = {
    'mapping', 'quant_config', 'quant_config_dict', 'attn_backend',
    'moe_backend', 'max_num_tokens', 'moe_max_num_tokens',
    'use_cuda_graph', 'allreduce_strategy', 'lora_config',
}
print("运行时开关字段是否都在:", runtime_only <= mc_fields)
```

然后做**源码阅读型对照**：打开 [tensorrt_llm/_torch/model_config.py:131-211](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/model_config.py#L131-L211)（`ModelConfig` 全部字段）与 [tensorrt_llm/models/modeling_utils.py:373-400](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/models/modeling_utils.py#L373-L400)（旧版 `PretrainedConfig` 字段），在纸上填一张两列对照表：

| 维度 | HF/Legacy PretrainedConfig | ModelConfig |
|------|---------------------------|-------------|
| 并行拓扑 | 无（旧版有 `mapping` 但 HF 无） | `mapping` |
| 注意力/MoE 后端 | 无 | `attn_backend` / `moe_backend` |
| 量化 | 旧版有 `quantization`；HF 无 | `quant_config` + `quant_config_dict` |
| 单步算力预算 | 无 | `max_num_tokens` |
| CUDA Graph | 无 | `use_cuda_graph` |
| 是否可变 | HF 可变；旧版可变 | **创建后冻结** |

**需要观察的现象**：`ModelConfig` 的字段远比 HF 配置多，且多出来的是「与硬件/部署策略相关」的开关；HF `config.json` 里根本不会出现 `attn_backend`、`mapping` 这类字段。

**预期结果**：`运行时开关字段是否都在: True`，且对照表里右列（ModelConfig）覆盖了大量左列没有的部署相关字段——这正是「需要再包一层」的根本原因。

**无法运行时**：标注「待本地验证」，直接用源码对照表完成练习。

#### 4.3.5 小练习与答案

**练习 1**：用一句话说明 `ModelConfig` 存在的必要性。

> **参考答案**：HF 配置只描述 checkpoint 自带、与硬件无关的模型结构；而 TP/PP 并行、注意力/MoE 后端选择、量化方案、CUDA Graph 等都是「运行时与部署相关、checkpoint 不记录」的开关，需要一个专门容器——`ModelConfig`——把它们和 HF 配置打包在一起，并在创建后冻结，供前向反复只读访问。

**练习 2**：`ModelConfig` 的 `_frozen` 机制允许哪几个字段在冻结后仍可修改？为什么允许这几个？

> **参考答案**：允许 `_frozen`、`extra_attrs`、`pretrained_config`、`quant_config`（见 [tensorrt_llm/_torch/model_config.py:213-228](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/model_config.py#L213-L228)）。`extra_attrs` 给 `torch.compile` 等流程挂额外属性；`pretrained_config` 允许多模态模型在加载后期补字段；`quant_config` 允许 VLM 给视觉/文本部分设不同量化。其余字段冻结是为了防止无意改动影响前向一致性。

**练习 3**：`from_pretrained()` 解析量化时，按什么顺序尝试哪些文件？

> **参考答案**：依次尝试 `hf_quant_config.json`（modelopt 格式）、`config.json` 内联的 `quantization_config`、`dtypes.json`（见 [tensorrt_llm/_torch/model_config.py:1127-1176](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/model_config.py#L1127-L1176)）。

---

### 4.4 SpeculativeDecodingMode：投机解码的标志位

#### 4.4.1 概念说明

`SpeculativeDecodingMode` 是一个 `IntFlag` 位标志枚举，用「位组合」表达多种投机解码（speculative decoding）模式。它告诉我们「当前推理要不要、用哪种方式投机解码」——例如 EAGLE、Medusa、N-gram、MTP、Lookahead 等。它是旧版 `PretrainedConfig` 体系下定义的，但概念上和 `ModelConfig.spec_config` 一起服务于投机解码（投机解码细节见 [u10-l3](./u10-l3-speculative-decoding.md)）。

#### 4.4.2 核心流程

```text
用户参数 / 命令行 args.speculative_decoding_mode (字符串)
   │  SpeculativeDecodingMode.from_arguments(args)
   ▼
SpeculativeDecodingMode.EAGLE / NGRAM / ...  (位标志)
   │  可按位组合：mode & SpeculativeDecodingMode.AUTO 等
   ▼
运行时据此选择 drafter 与 target 的协作方式
```

因为是 `IntFlag`，多个标志可以共存（例如某些模式同时带 `SAVE_HIDDEN_STATES`）。

#### 4.4.3 源码精读

[ tensorrt_llm/models/modeling_utils.py:87-98](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/models/modeling_utils.py#L87-L98) —— `SpeculativeDecodingMode(IntFlag)` 定义了 `NONE / DRAFT_TOKENS_EXTERNAL / MEDUSA / LOOKAHEAD_DECODING / EXPLICIT_DRAFT_TOKENS / EAGLE / NGRAM / USER_PROVIDED / SAVE_HIDDEN_STATES / AUTO`。注意顶部注释提醒：它必须与 C++ 头文件 `cpp/tensorrt_llm/runtime/speculativeDecodingMode.h` 保持同步——这是「Python 调度、C++ 加速」两侧标志位必须对齐的典型约束。

[ tensorrt_llm/models/modeling_utils.py:100-123](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/models/modeling_utils.py#L100-L123) —— `from_arguments` 把命令行字符串映射成枚举值，`None` 映射成 `NONE`。

#### 4.4.4 代码实践

**实践目标**：理解 `IntFlag` 的「按位组合」语义。

**操作步骤**：

```python
# 示例代码
from tensorrt_llm.models import SpeculativeDecodingMode as SDM

print("EAGLE:", SDM.EAGLE)
print("EAGLE | SAVE_HIDDEN_STATES:", SDM.EAGLE | SDM.SAVE_HIDDEN_STATES)
print("是否含 SAVE_HIDDEN_STATES:",
      bool((SDM.EAGLE | SDM.SAVE_HIDDEN_STATES) & SDM.SAVE_HIDDEN_STATES))
```

**需要观察的现象**：两个标志「按位或」后能合并；用「按位与」可检测是否包含某标志。

**预期结果**：`EAGLE | SAVE_HIDDEN_STATES` 打印出组合值，按位与检测返回 `True`。

**无法运行时**：标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `SpeculativeDecodingMode` 要用 `IntFlag` 而不是普通 `Enum`？

> **参考答案**：因为某些标志需要**同时成立**（如某模式既开 EAGLE 又要 `SAVE_HIDDEN_STATES`），`IntFlag` 允许按位或组合、按位与检测；普通 `Enum` 一次只能取一个值，无法表达组合。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「配置考古」任务：

1. **找一个真实 checkpoint**（如 HF 上的一个小模型，或仓库 `examples/` 里提到的模型）。
2. **加载并剖析**：用 `ModelConfig.from_pretrained(checkpoint_dir)`（或阅读其源码路径）回答：
   - 它的 HF `pretrained_config.architectures[0]` 是什么？属于哪种 `PretrainedConfig` 子类？
   - 它的 `quant_config.quant_algo` 是什么？是全局量化还是逐层（`quant_config_dict` 非空）？
   - 默认 `attn_backend` / `moe_backend` 是什么？若是 MoE 模型，`resolve_moe_backend` 在你的显卡（SM 版本）上会解析成什么？
3. **画一张「三层包裹」图**：最内层 HF `PretrainedConfig`（结构）→ 中层 `QuantConfig`（量化）→ 外层 `ModelConfig`（运行时开关 + 冻结），标注每层「谁负责填写、何时冻结」。
4. **写一段说明**：如果想让这个模型改用 FlashInfer 注意力后端、并启用 FP8 KV cache，应该改 `ModelConfig` 的哪些字段？为什么这些字段不会出现在 HF 的 `config.json` 里？

> 提示：第 4 点的答案应落在「`attn_backend='FLASHINFER'`、`quant_config.kv_cache_quant_algo=QuantAlgo.FP8`」，并解释这是部署期决策、与 checkpoint 解耦。

## 6. 本讲小结

- 项目里有**两个 `PretrainedConfig`**：HF `transformers.PretrainedConfig`（PyTorch 后端用，由 HF 从 `config.json` 解析）与本仓库旧版 `modeling_utils.PretrainedConfig`（legacy 引擎流，自包含、`MODEL_MAP` 在 PyTorch 后端为空）。两者不通用。
- **`ModelConfig`（`_torch/model_config.py`）是运行时包裹层**：`Generic[TConfig]`（`TConfig` 绑定 HF 配置），把 HF 配置 + `Mapping` 并行 + 量化 + 注意力/MoE 后端 + CUDA Graph 等运行时开关打包，`from_pretrained()` 创建后**立即冻结**。
- **`QuantConfig` / `LayerQuantConfig` 是量化的纯描述**：前者描述全局统一方案（`quant_algo` + `kv_cache_quant_algo` + `group_size` 等），后者用 `quantized_layers: Dict[str, QuantConfig]` 描述逐层混合精度；`quant_mode` 把可读算法翻译成底层 C++ 位标志 `QuantMode`。新旧两套流程共享它们。
- **为什么再包一层**：HF 配置只描述「与硬件无关的模型结构」，而 TP/PP、后端选择、量化、CUDA Graph 都是「运行时与部署相关、checkpoint 不记录」的开关，需要专门容器，且创建后冻结以防前向期被改动。
- **`SpeculativeDecodingMode` 是 `IntFlag` 位标志**，表达 EAGLE / Medusa / N-gram / MTP / Lookahead 等投机解码模式，可按位组合，且必须与 C++ 头文件保持同步。

## 7. 下一步学习建议

- **进入模型层**：[u5-l1 模型架构范式](./u5-l1-model-architecture-pattern.md) 会打开 `DecoderModelForCausalLM`，看它如何把本讲的 `ModelConfig` 喂给逐层前向，`config.pretrained_config` 与 `config.quant_config` 在每一层里如何被读取。
- **量化深入**：[u10-l2 量化机制](./u10-l2-quantization.md) 会讲 `QuantConfig` 描述的算法在前向时如何落到 `custom_ops` 与 `modelopt_config`。
- **投机解码**：[u10-l3 投机解码](./u10-l3-speculative-decoding.md) 会把 `SpeculativeDecodingMode` 与 `ModelConfig.spec_config` 的具体 drafter（eagle3 / mtp / ngram）连起来。
- **建议阅读源码**：通读 `ModelConfig.from_pretrained`（[tensorrt_llm/_torch/model_config.py:821](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/model_config.py#L821)）和 `QuantConfig.is_module_excluded_from_quantization`（[tensorrt_llm/models/modeling_utils.py:245](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/models/modeling_utils.py#L245)），这是后续量化与权重加载的高频调用点。
