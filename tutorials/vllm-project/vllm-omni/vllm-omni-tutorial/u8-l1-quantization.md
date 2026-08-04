# 量化体系：统一框架与各方法

## 1. 本讲目标

本讲是专家层 U8（量化、多硬件平台与性能剖析）的第一讲，专门讲清楚 vLLM-Omni 的**量化（quantization）体系**。学完后你应当能够：

1. 说清 `build_quant_config()` 这个**唯一入口**如何把「方法名 / 字典 / per-component 字典 / None」统一解析成一个 `QuantizationConfig`，以及它如何**委托（delegate）** vLLM 已有的 35+ 方法注册表，再叠加 omni 自己的扩散专属方法。
2. 区分**在线量化（online）**与**离线 / 预量化（offline / pre-quantized）**两种模式，并能对照 MXFP8 / Int8 等方法在 `get_quant_method()` 中按 `is_checkpoint_*_serialized` 标志与硬件平台分派不同线性算子。
3. 理解 `ComponentQuantizationConfig` 如何用**最长前缀匹配**为多阶段模型做 per-component（按组件）量化路由，以及它如何保护 vision/audio encoder 与 norm/modulation 层不被错误量化。

本讲是后续 U8 多硬件平台（u8-l2）与基准测试（u8-l3）的基础，也承接 u2-l2（配置体系）与 u5-l3（Diffusion Worker 与模型加载）。

## 2. 前置知识

- **量化（quantization）**：把高精度浮点权重（如 BF16）压缩成低精度（如 FP8 / INT8 / MXFP4），用更少显存、跑更快 GEMM，代价是少量精度损失。vLLM 已经有一套成熟的量化框架，本讲只讲 omni 如何在其上扩展，不会从零讲 FP8 编码原理。
- **权重（weight）与激活（activation）**：线性层 `y = xW + b` 中，`W` 是权重、`x` 是激活。W8A8 表示权重 8 位、激活 8 位；W4A16 表示权重 4 位、激活 16 位（权重专用量化）。
- **在线 vs 离线**：
  - 在线量化 = 加载 BF16 checkpoint 时，**运行时实时算**量化权重和 scale。
  - 离线量化 = checkpoint **已经存好**量化权重和 scale，加载时直接读。ModelOpt、AutoRound、msModelSlim 都属此类。
- **委托（delegation）**：omni 不重写 vLLM 的量化内核与注册表，而是「能复用就复用、只在缺口处补」。这个词会在本讲反复出现。
- **monkey-patch（猴子补丁）**：u2-l1 已讲过，指运行时替换某方法。本讲会看到量化与 patch 的交集（NVFP4 的 NaN 钳位）。
- **stage / 多阶段模型**：u2-l2、u3 已讲过，一个请求可能被拆成多个顺序 stage（如 Qwen3-Omni 的 Thinker→Talker→Code2wav）。per-component 量化就是为这种结构服务的。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [vllm_omni/quantization/__init__.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/__init__.py) | 量化子包入口，只暴露 `build_quant_config`、`ComponentQuantizationConfig`、`OmniINCConfig`、`SUPPORTED_QUANTIZATION_METHODS`、`register_quantization_override`；**刻意不导入重 config** 以避免拖入可选依赖。 |
| [vllm_omni/quantization/factory.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py) | **统一工厂**，是本讲的主角。`build_quant_config` 入口、`_OVERRIDES` 注册表、ModelOpt 探测、per-component 解析、别名归一、磁盘 reconcile 全在这里。 |
| [vllm_omni/quantization/component_config.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/component_config.py) | `ComponentQuantizationConfig` 及两个保护函数 `resolve_encoder_quant_config` / `safe_quant_config`。 |
| [vllm_omni/quantization/mxfp8_config.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/mxfp8_config.py) | `DiffusionMXFP8Config`，是「在线/离线 + 平台分派」最完整的范例，含 NPU 离线、NPU 在线、XPU 在线三条线性算子路径。 |
| [vllm_omni/quantization/int8_config.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/int8_config.py) | `DiffusionInt8Config`，跨 CUDA/NPU 的在线与序列化 Int8，结构与 MXFP8 同构。 |
| [vllm_omni/quantization/inc_config.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/inc_config.py) | `OmniINCConfig`，扩展 vLLM INCConfig，支持多阶段前缀重映射与 AutoRound MXFP8。 |
| [docs/user_guide/quantization/modelopt.md](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/user_guide/quantization/modelopt.md) | ModelOpt 预量化 checkpoint 的使用指南，是「综合实践」的依据。 |
| [examples/quantization/quantize_wan2_2_modelopt_fp8.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/quantization/quantize_wan2_2_modelopt_fp8.py) | 离线把 Wan2.2 量化成 ModelOpt FP8 checkpoint 的脚本，是综合实践的主角。 |

---

## 4. 核心概念与源码讲解

### 4.1 量化统一框架：build_quant_config 与委托机制

#### 4.1.1 概念说明

vLLM 本身已经有一套很强的量化框架：它维护一个 `QUANTIZATION_METHODS` 列表（fp8、int8、gguf、awq、gptq、bitsandbytes …… 共 35+ 方法），以及 `get_quantization_config(method)` 这个「方法名 → Config 类」的注册表。每个方法对应一个 `QuantizationConfig` 子类，子类再通过 `get_quant_method(layer, prefix)` 给具体线性层配上 `LinearMethod`（真正做量化矩阵乘的算子）。

vLLM-Omni 的设计选择是：**不重写这套体系，而是「委托 + 补丁」**。

- **委托**：用户写 `quantization="fp8"`，omni 原样转给 vLLM 注册表，复用 vLLM 的 FP8 内核。
- **补丁**：vLLM 没有针对**扩散 Transformer**的方法（如 NPU 上的 MXFP8/MXFP4），omni 在 `_OVERRIDES` 里新增。
- **统一入口**：无论用户传字符串、字典、per-component 字典、还是已有 Config 对象，都从一个函数 `build_quant_config(spec)` 进出。

这个入口的文档注释把设计意图说得很直白：

[vllm_omni/quantization/__init__.py:L3-L12](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/__init__.py#L3-L12) —— 注释明确写「Delegates to vLLM's quantization registry (35+ methods, all platforms). Adds per-component quantization for multi-stage models.」

另一个工程要点：`__init__.py` **刻意不导入重 config**（如 `mxfp8_config`、`int8_config`），避免一 `import vllm_omni.quantization` 就拖入 `pynvml`、`torch_npu` 等可选依赖。重 config 在需要时才 lazy import（见 [vllm_omni/quantization/__init__.py:L18-L21](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/__init__.py#L18-L21)）。这与 u1-l3 讲过的「懒加载避免轻量子进程崩溃」是一脉相承的。

#### 4.1.2 核心流程

`build_quant_config` 的解析流程可以画成下面这张决策树（伪代码）：

```
build_quant_config(spec):
    if spec is None:                      return None        # 不量化
    if spec is QuantizationConfig:        return spec        # 已经是 Config，透传
    if spec is str:
        if spec == "none":                return None
        return _build_single(spec)                            # 方法名 → 单一 Config
    if spec is dict:
        if _is_per_component_dict(spec):  return _build_component_config(spec)   # 形如 {"transformer":{...},"vae":None}
        if _detect_modelopt_method(spec): return _build_modelopt_from_config(...) # config.json 带 ModelOpt 元数据
        method = spec.pop("method"/"quant_method")
        return _build_from_method_and_config(method, spec)

_build_single(method, **kw):
    method = normalize(method)            # 小写化 + 连字符转下划线
    if method in _OVERRIDES:              return _OVERRIDES[method](**kw)   # omni 扩展方法优先
    if method not in QUANTIZATION_METHODS: raise ValueError(未知方法)
    return get_quantization_config(method)(**kw)                # 否则委托 vLLM 注册表
```

四条关键规则：

1. **`_OVERRIDES` 优先于 vLLM 注册表**：同一个方法名（如 `int8`、`mxfp8`），omni 的扩散专用实现会**遮蔽** vLLM 的通用实现。
2. **per-component 字典靠形态识别**：`_is_per_component_dict` 用「无 `method` 键 + 至少一个值是 `None` 或带 `method` 的 dict」来判定。
3. **ModelOpt 靠元数据探测**：从 checkpoint 的 `config.json` 字段（`quant_algo` / `producer.name`）反推具体是哪种 ModelOpt 变体。
4. **别名归一**：`auto-round`、`auto_round`、`inc` 都映射到同一个 builder。

#### 4.1.3 源码精读

**① `_OVERRIDES` —— omni 扩展方法的注册表**

[factory.py:L142-L151](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py#L142-L151) 是 omni 自己新增的方法名 → builder 映射：

```python
_OVERRIDES: dict[str, Callable[..., QuantizationConfig]] = {
    "int8": _build_int8,
    "bitsandbytes": _build_bitsandbytes,
    "mxfp8": _build_mxfp8,
    "mxfp4": _build_mxfp4,
    "mxfp4_dualscale": _build_mxfp4_dualscale,
    "inc": _build_inc,
    "auto-round": _build_inc,   # 别名
    "auto_round": _build_inc,   # 别名
}
```

注意三个细节：(a) `auto-round` 与 `auto_round` 都是 `inc` 的别名（INC 与 AutoRound 在 vLLM 里共用 INCConfig）；(b) 每个 builder 都是 **lazy import** 的薄封装（例如 `_build_mxfp8` 在函数体内才 `from .mxfp8_config import DiffusionMXFP8Config`，见 [factory.py:L98-L102](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py#L98-L102)），避免可选依赖在 import 期报错；(c) `_build_inc` 还做了参数归一——把 checkpoint 里常见的 `bits` 键映射成 INCConfig 的 `weight_bits`（[factory.py:L128-L139](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py#L128-L139)）。

**② `SUPPORTED_QUANTIZATION_METHODS` —— 全量方法列表**

[factory.py:L154-L158](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py#L154-L158) 把 vLLM 的 `QUANTIZATION_METHODS` 与 omni 的 `_OVERRIDES` 合并去重：

```python
def _compute_supported_quantization_methods() -> list[str]:
    return list(dict.fromkeys(QUANTIZATION_METHODS + list(_OVERRIDES.keys())))

SUPPORTED_QUANTIZATION_METHODS: list[str] = _compute_supported_quantization_methods()
```

`dict.fromkeys` 保持插入顺序去重——vLLM 方法在前，omni 扩展在后。这个列表既是「支持什么」的对外声明，也是报错信息里列给用户的候选清单。

**③ `_build_single` —— 单一 Config 的真正构造**

[factory.py:L287-L308](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py#L287-L308) 体现了「omni 优先、否则委托 vLLM」的解析顺序：

```python
def _build_single(method: str, **kwargs: Any) -> QuantizationConfig:
    method = _normalize_method_name(method)          # lower + '-' -> '_'
    if method in _OVERRIDES:                         # ① omni 扩展方法
        return _OVERRIDES[method](**kwargs)
    if method not in QUANTIZATION_METHODS:           # ② 不认识就报错
        raise ValueError(f"Unknown quantization method: {method!r}. ...")
    config_cls = get_quantization_config(method)     # ③ 委托 vLLM 注册表
    try:
        return config_cls(**kwargs)
    except TypeError:
        ...  # 报错时打印期望签名，方便调试
```

`fp8` 这种通用方法走 ③（委托 vLLM）；`mxfp8` 这种扩散/NPU 专用方法走 ①（omni 自己）。

**④ `build_quant_config` —— 总入口的分发逻辑**

[factory.py:L358-L402](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py#L358-L402) 按 `spec` 的类型分流。其中 dict 分支最值得读（[factory.py:L381-L400](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py#L381-L400)）：先判 per-component，再判 ModelOpt，最后才取 `method` 键构造普通 Config。**顺序很重要**——ModelOpt 的 checkpoint dict 里也可能没有顶层 `method` 键（它靠 `producer`/`quant_algo` 表达），所以必须先探测。

**⑤ humming 桩 —— 一处值得学的健壮性技巧**

[factory.py:L28-L69](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py#L28-L69) 在导入期就向 `sys.modules` 注册了一组空的 `humming.*` 子模块。原因是：vLLM 的 `get_quantization_config()` 内部会无条件 `from .humming import HummingConfig`，若用户没装 `humming` wheel 就会崩。omni 用桩模块兜住这步，让量化注册表在缺依赖时也能正常工作。这是「补丁型扩展」里典型的**逃逸舱**思维（与 u2-l1 讲的健壮性模式同源）。

#### 4.1.4 代码实践

**实践目标**：亲手调用 `build_quant_config`，验证不同形态的输入如何被解析成不同的 `QuantizationConfig`，并观察委托与遮蔽。

**操作步骤**（源码阅读 + 最小调用，待本地验证）：

1. 阅读 [factory.py:L142-L151](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py#L142-L151) 与 [factory.py:L287-L308](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py#L287-L308)，确认 `mxfp8` 走 `_OVERRIDES`、`fp8` 走 vLLM 注册表。
2. 写一段最小脚本（示例代码，非项目原有）：

```python
# 示例代码
from vllm_omni.quantization import build_quant_config, SUPPORTED_QUANTIZATION_METHODS

print("mxfp8" in SUPPORTED_QUANTIZATION_METHODS)          # True
print(build_quant_config(None))                            # None
print(build_quant_config("fp8").get_name())                # fp8 (委托 vLLM)
print(build_quant_config("mxfp8").get_name())             # mxfp8 (omni 扩展，需 NPU/XPU)
print(build_quant_config({"method": "fp8",
                          "ignored_layers": ["img_mlp"]}).get_name())  # fp8
```

3. 故意传一个不存在的方法名，观察报错信息是否列出 `SUPPORTED_QUANTIZATION_METHODS`。

**需要观察的现象**：

- `fp8` 的 `.get_name()` 返回 `"fp8"`，证明它走的是 vLLM 的 FP8 Config；
- `mxfp8` 的构造会触发 lazy import `DiffusionMXFP8Config`，在非 NPU/XPU 平台上**构造 Config 本身不报错**（报错发生在后续 `get_quant_method` 选算子时）；
- 不存在的方法名会抛 `ValueError`，信息里附带全部支持方法。

**预期结果 / 待本地验证**：第 2 步中 `build_quant_config("mxfp8")` 在普通 CUDA 机器上能成功返回一个 `DiffusionMXFP8Config` 对象（因为构造只存标志位），但若进一步调用它的 `get_quant_method(...)`，会落到 [mxfp8_config.py:L144-L147](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/mxfp8_config.py#L144-L147) 抛 `NotImplementedError`。这一「构造期宽容、算子期严格」的行为待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：用户传 `quantization="auto-round"`。它最终被哪个 builder 处理？为什么不直接出现在 vLLM 注册表里？

**答案**：`auto-round` 经 `_normalize_method_name` 归一为 `auto_round`，命中 `_OVERRIDES["auto-round"]` 与 `_OVERRIDES["auto_round"]`，两者都指向 `_build_inc`（[factory.py:L148-L150](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py#L148-L150)）。它在 omni 侧被遮蔽，是为了在构造前做 `bits→weight_bits` 的键名归一（[factory.py:L132-L134](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py#L132-L134)）和参数过滤，再交给 vLLM 的 `INCConfig`。

**练习 2**：为什么 `__init__.py` 不直接 `from .mxfp8_config import DiffusionMXFP8Config`？

**答案**：因为 `mxfp8_config` 顶部会 `from vllm_omni.platforms import current_omni_platform` 并在 NPU 下 `import torch_npu`（见 [mxfp8_config.py:L58](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/mxfp8_config.py#L58) 及 int8 的 [int8_config.py:L42-L45](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/int8_config.py#L42-L45)）。顶层导入会把 `torch_npu` 这类重平台依赖拖进所有 `import vllm_omni.quantization` 的进程，破坏懒加载。所以只在 builder 内部 lazy import。

---

### 4.2 量化方法与平台适用性：在线 / 离线与扩散专属方法

#### 4.2.1 概念说明

有了统一入口，下一个问题是：**一个方法名背后，到底跑哪种量化算子？** 这由每个 `QuantizationConfig` 子类的 `get_quant_method(layer, prefix)` 决定。本模块聚焦三个判据：**在线 vs 离线**、**硬件平台**、**被跳过的层**。

先建立一张全局表（综合自 [docs/user_guide/quantization/overview.md:L10-L28](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/user_guide/quantization/overview.md#L10-L28)）：

| 模式 | 含义 | 代表方法 |
|------|------|----------|
| 在线量化 | 加载 BF16 checkpoint，**运行时实时**算量化权重 + scale | FP8 W8A8、Int8 W8A8、MXFP8 W8A8、MXFP4 W4A4 |
| 预量化 checkpoint | checkpoint **已存好**量化权重 + scale，加载即用 | ModelOpt、GGUF、AutoRound、msModelSlim、serialized Int8、offline MXFP8、offline MXFP4 DualScale |

区分这两者的开关是一组命名一致的布尔字段 `is_checkpoint_*_serialized`，例如 MXFP8 的 `is_checkpoint_mxfp8_serialized`、Int8 的 `is_checkpoint_int8_serialized`。`True` = 离线（checkpoint 已序列化好），`False` = 在线。

平台适用性（同样综合自 overview.md）：

| 方法 | CUDA | ROCm | XPU | NPU |
|------|:----:|:----:|:---:|:---:|
| FP8 W8A8（在线） | ✅ | ⭕ | ⭕ | ❌ |
| Int8 W8A8 | ✅ | ⭕ | ⭕ | ✅ |
| ModelOpt（预量化） | ✅ | ⭕ | ⭕ | ❌ |
| MXFP8 W8A8 | ⭕ | ⭕ | 在线✅ | ✅ |
| MXFP4 W4A4 | ⭕ | ⭕ | ⭕ | ✅ |
| msModelSlim（预量化） | ❌ | ❌ | ❌ | ✅ |

可以读出一个规律：**FP8/ModelOpt 是 NVIDIA 系，MXFP8/MXFP4/msModelSlim 是 Ascend NPU 系**。omni 的扩散专用方法（MXFP8/MXFP4）几乎都是为 NPU 补的缺口——vLLM 上游没有这些。

#### 4.2.2 核心流程

每个 Config 子类的 `get_quant_method` 内部都遵循同一个三段式判定：

```
get_quant_method(layer, prefix):
    if not isinstance(layer, LinearBase):    return None      # 非线性层不量化
    if is_layer_skipped(prefix, ignored_layers):              # 命中黑名单
        return UnquantizedLinearMethod()                       #   保持 BF16
    # 按 [是否序列化] × [硬件平台] 选 LinearMethod
    if is_checkpoint_*_serialized:  <离线算子>
    else:                           <在线算子>
```

- **`ignored_layers`**：精度敏感层的黑名单。扩散 DiT 里常见的是 `img_mlp`、`condition_embedder`、`norm_out`、`proj_out`、`scale_shift_table` 等（详见综合实践的 Wan2.2 例子）。命中黑名单的层返回 `UnquantizedLinearMethod`，即不量化。
- **在线算子的关键技巧**：权重先用 `_LazyWeightMixin` 在 meta device 上占位，等真实 BF16 权重加载完，再在 `process_weights_after_loading` 里一次性转成 FP8/MXFP8。
- **离线算子**：`create_weights` 直接为已量化的权重 + scale 注册参数位。

#### 4.2.3 源码精读

**① Int8 —— 在线/离线 + 平台分派的最小范例**

[DiffusionInt8Config.__init__](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/int8_config.py#L85-L98) 存了三个标志：`is_checkpoint_int8_serialized`、`activation_scheme`（只支持 `"dynamic"`）、`ignored_layers`。

真正的分派在 [int8_config.py:L136-L159](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/int8_config.py#L136-L159)：

```python
def get_quant_method(self, layer, prefix):
    if isinstance(layer, LinearBase):
        if is_layer_skipped(prefix, ignored_layers=self.ignored_layers, ...):
            return UnquantizedLinearMethod()
        if not self.is_checkpoint_int8_serialized:          # 在线
            if current_omni_platform.is_cuda():
                return Int8OnlineLinearMethod(self)
            elif current_omni_platform.is_npu():
                return NPUInt8OnlineLinearMethod(self)
            else:
                raise NotImplementedError(...)
        else:                                                # 离线
            if current_omni_platform.is_cuda():
                return Int8LinearMethod(self)
            elif current_omni_platform.is_npu():
                ...  # NPU 离线算子
```

这就是「在线/离线 × 平台」的十字交叉：`is_checkpoint_int8_serialized` 选行、`current_omni_platform.is_cuda()/is_npu()` 选列。`current_omni_platform` 正是 u8-l2 要讲的平台抽象单例，这里先把它当作「当前硬件是什么」的全局询问即可。

**② MXFP8 —— 在线/离线 + 三平台的完整范例**

MXFP8 比 Int8 多一个 XPU 路径，是本模块最完整的例子。先看 [DiffusionMXFP8Config.get_quant_method](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/mxfp8_config.py#L120-L148)：

```python
def get_quant_method(self, layer, prefix):
    if isinstance(layer, LinearBase):
        if is_layer_skipped(...):
            return UnquantizedLinearMethod()
        if current_omni_platform.is_npu():
            if self.is_checkpoint_mxfp8_serialized:
                return NPUMxfp8LinearMethod(self)         # NPU 离线
            return NPUMxfp8OnlineLinearMethod(self)        # NPU 在线
        if current_omni_platform.is_xpu():
            if self.is_checkpoint_mxfp8_serialized:
                raise NotImplementedError("XPU 不支持原生 MXFP8 离线，请用 AutoRound MXFP8 ...")
            return VllmMxfp8OnlineLinearMethod()           # XPU 仅在线
        raise NotImplementedError("W8A8 MXFP8 当前只支持 NPU 和 XPU")
    return None
```

这段同时体现了三个判据：黑名单跳过、序列化标志分流、平台分派。

**MX（microscaling）格式**需要单独解释：MXFP8 不是「整张权重一个 scale」，而是**每 32 个 K 维元素共享一个 8 位指数 scale（`float8_e8m0fnu`）**，见 [mxfp8_config.py:L75-L78](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/mxfp8_config.py#L75-L78)。一个权重块 \(W \in \mathbb{R}^{N\times K}\) 被沿 K 维切成 \(K/32\) 组，第 \(g\) 组的实际值为

\[
\hat{W}_{n,k}=W_{n,k}\cdot 2^{s_{n,g(k)}},\quad g(k)=\lfloor k/32\rfloor,\quad s\in \text{e8m0}
\]

因为 scale 粒度细，MXFP8 通常比逐张量 FP8 精度更高，代价是要存一张 scale 张量（形状 `(N, K/32)`，存为 `uint8`，见离线路径 [mxfp8_config.py:L343-L352](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/mxfp8_config.py#L343-L352)）。

**③ 在线算子如何「先占位、后量化」—— `_LazyWeightMixin`**

[NPUMxfp8OnlineLinearMethod](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/mxfp8_config.py#L421-L459) 继承 `_LazyWeightMixin` + 离线方法 `NPUMxfp8LinearMethod`，它的 MRO（方法解析顺序）注释把分工讲得很清楚（[mxfp8_config.py:L424-L430](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/mxfp8_config.py#L424-L430)）：

- `create_weights` 来自 `_LazyWeightMixin`：在 meta device 上注册占位权重，并打补丁 weight_loader，统计已加载 numel；
- `process_weights_after_loading` 来自自己：当全部权重到齐，调用 `torch_npu.npu_dynamic_mx_quant` 把 BF16 → FP8 + MX scale（[mxfp8_config.py:L451-L455](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/mxfp8_config.py#L451-L455)），并规整成与离线路径**相同的**规范布局；
- `apply` / 量化算子来自 `NPUMxfp8LinearMethod` / `MXFPLinearMethodBase`，在线离线**共享**。

这就是「在线 vs 离线」在代码层面的真正区别：**离线方法的 `create_weights` 直接注册已量化的 `weight + weight_scale`；在线方法靠 `_LazyWeightMixin` 拖到加载完成，再用 `process_weights_after_loading` 现场量化，之后两者合流到同一套 `apply`。** 共享骨架 [MXFPLinearMethodBase](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/mxfp8_config.py#L228-L289) 定义了 `apply = reshape → _apply_inner → unreshape` 的模板，子类只填 `_quantize_activation` / `_quant_matmul` 两个钩子。

**④ ModelOpt —— 预量化的代表，以及它与 patch 的交集**

ModelOpt 走的是**另一条路**：它不在 `_OVERRIDES` 里，而是 vLLM 注册表本来就有的方法（`modelopt` / `modelopt_fp4` / `modelopt_mixed`）。omni 的增值是「**探测具体是哪种 ModelOpt 变体**」。

探测逻辑 [_detect_modelopt_method](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py#L231-L262)：先看 `method`/`quant_method` 是否属于 ModelOpt 家族，或 `producer.name == "modelopt"`；再用 `quant_algo` 精确分派——`FP8`/`FP8_PER_CHANNEL_PER_TOKEN` → `modelopt`，`NVFP4` → `modelopt_fp4`，`MIXED_PRECISION` → `modelopt_mixed`。然后 [_build_modelopt_from_config](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py#L265-L269) 调 `get_quantization_config(method)` 拿到 vLLM 的 Config 类并 `.from_config()`。

一个重要的使用纪律（见 [modelopt.md:L17-L20](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/user_guide/quantization/modelopt.md#L17-L20)）：**ModelOpt 预量化 checkpoint 不要再传 `--quantization fp8`**，因为 checkpoint 的 `config.json` 自带 ModelOpt 元数据，会走自动探测。这一点在综合实践里会再次出现。

ModelOpt 与 patch 的交集：NVFP4 的权重 scale 在 ModelOpt 0.44 偶尔会写出字面 NaN 字节，会让输出塌缩成 `!!!!`。omni 用 u2-l1 讲过的 patch 机制给 `ModelOptNvFp4LinearMethod.process_weights_after_loading` 装了一个「加载时钳 NaN」的防御覆盖，可用 `VLLM_OMNI_SKIP_NVFP4_NAN_CLAMP=1` 关闭（见 [modelopt.md:L115-L126](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/user_guide/quantization/modelopt.md#L115-L126)）。这是「修改型扩展」服务于量化的典型例子。

**⑤ quack —— Blackwell 上的 FP8 加速补丁**

在数据中心 Blackwell（`sm_100/101/103`）上，vLLM 默认的 FlashInfer FP8 kernel 把 bias 拆成单独 kernel 启动，对视频 DiT 的小 GEMM 是显著开销。omni 的 [quack_fp8.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/quack_fp8.py) 把 \(\alpha(A@B)+b\) 融进单个 CuteDSL GEMM。它靠 [_is_quack_capable()](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/quack_fp8.py#L18-L40) 检测 `compute_capability[0]==10`（只有这些卡有 `tcgen05` 张量核），装了 `quack-kernels` 就自动启用，可用 `VLLM_OMNI_USE_QUACK_FP8` 强制开关。这是「同是 FP8，不同硬件走不同 kernel」的又一例。

#### 4.2.4 代码实践

**实践目标**：用现成的对比工具，量化感受「在线 FP8 量化」对生成质量的影响，并定位 `get_quant_method` 的分派代码。

**操作步骤**：

1. 阅读 [int8_config.py:L136-L159](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/int8_config.py#L136-L159) 与 [mxfp8_config.py:L120-L148](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/mxfp8_config.py#L120-L148)，在两张分派表里各标出「在线/离线 × 平台」的四个分支。
2. （可选运行）使用 overview.md 提供的轨迹相似度工具做一次在线 FP8 对比（命令见 [overview.md:L159-L172](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/user_guide/quantization/overview.md#L159-L172)）：

```bash
python -m vllm_omni.quantization.tools.compare_diffusion_trajectory_similarity \
  --task t2i --model Qwen/Qwen-Image \
  --candidate-quantization fp8 --ignored-layers img_mlp \
  --prompt "a cup of coffee on the table" \
  --height 512 --width 512 --num-inference-steps 20 --seed 142 \
  --output-json /tmp/qwen_fp8/result.json --enforce-eager
```

**需要观察的现象**：

- 源码层面：`is_checkpoint_int8_serialized=False` 且 `is_cuda()` 时返回 `Int8OnlineLinearMethod`，这是「在线」分支；
- 运行层面：对比工具会输出 `psnr_db`、`cosine_similarity` 等指标，量化模型相对 BF16 参考应有较高相似度（参考 [overview.md:L214-L221](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/user_guide/quantization/overview.md#L214-L221) 的启发式阈值）。

**预期结果 / 待本地验证**：在没有 GPU 的环境下第 2 步无法运行，请明确标注「待本地验证」；源码阅读部分（第 1 步）可独立完成。

#### 4.2.5 小练习与答案

**练习 1**：同一个 `DiffusionMXFP8Config` 对象，为什么在线和离线两种模式可以共享同一个 `apply()`？

**答案**：因为 `NPUMxfp8OnlineLinearMethod` 的 `process_weights_after_loading` 把在线量化的结果**规整成与离线路径完全相同的规范布局**（权重 `(K,N)` FP8、scale `(K_groups/2,N,2)` e8m0，见 [mxfp8_config.py:L301-L305](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/mxfp8_config.py#L301-L305) 的注释）。布局一致，所以 `_quantize_activation`/`_quant_matmul`/`apply` 可以从离线基类直接继承。

**练习 2**：用户给一个 ModelOpt FP8 checkpoint 又传了 `--quantization fp8`，会发生什么？

**答案**：在扩散 stage，`OmniDiffusionConfig._propagate_quantization_from_tf_config` 会发现 checkpoint 的 `config.json` 标了 `is_checkpoint_fp8_serialized`，且当前 `quantization_config` 是「通用 fp8」，于是用 checkpoint 自带的序列化配置**覆盖**它（见 [config/omni_config.py:L619-L627](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/omni_config.py#L619-L627)）。所以多传 `--quantization fp8` 通常无害但不必要；官方文档建议直接省略。

---

### 4.3 多阶段模型的 per-component 量化：ComponentQuantizationConfig

#### 4.3.1 概念说明

前面两模块都假设「整个模型用同一种量化」。但多阶段模型（u2-l2、u3 讲的 Qwen3-Omni、多阶段 diffusion）里，**不同组件对量化的耐受度完全不同**：

- **Thinker 语言模型 / DiT transformer**：参数量大，是量化的主要收益目标。
- **vision/audio encoder**：预量化 checkpoint（如 ModelOpt）根本**没有给它们准备 scale 张量**，权重仍是 BF16，若强行套 FP8 kernel 会读不到 scale 而崩溃或得到乱码。
- **VAE / tokenizer / scheduler**：通常保持原样。
- **norm/modulation 层**（LayerNorm、RMSNorm、AdaLayerNorm、`img_mod`、`txt_mod`）：产出精度敏感的 shift/scale/gate 值，套 FP8 会污染整层。

`ComponentQuantizationConfig` 就是为这种「按组件分别配置」而生的路由器：它本身不做量化，只负责**按层名前缀把请求转发给不同的子 Config（或 None）**。

#### 4.3.2 核心流程

per-component 量化的数据流分两段：

**第一段：构造（在 factory 里识别形态）**

```
spec = {"transformer": {"method": "fp8"}, "vae": None, "default": None}
            ↓ _is_per_component_dict 判定为 per-component（无顶层 method 键，且有 None 值）
            ↓ _build_component_config 遍历每个 prefix：
                 "transformer" -> _build_single("fp8")  -> Fp8Config
                 "vae"         -> None
                 "default"     -> None（特殊键，作为兜底）
            ↓
ComponentQuantizationConfig({"transformer": Fp8Config}, default=None)
```

**第二段：分发（模型加载时，每个线性层都来问一次）**

```
对每个线性层 layer，模型加载器调用 quant_config.get_quant_method(layer, prefix):
    config = self.resolve(prefix)        # 最长前缀匹配
        "transformer.blocks.0.attn.to_q"  -> 命中 "transformer" -> Fp8Config
        "vae.encoder.conv_in"             -> 不命中任何 -> default -> None
    if config is None: return None       # 不量化
    else: return config.get_quant_method(layer, prefix)   # 委托子 Config
```

两个细节：(a) 匹配是**最长前缀**，所以 `"transformer"` 不会误吞 `"transformer_2"` 之外的更具体规则——靠按长度降序排序实现；(b) `default` 是约定俗成的兜底键。

#### 4.3.3 源码精读

**① 形态识别：什么算 per-component 字典**

[_is_per_component_dict](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py#L311-L324)（[factory.py:L311-L324](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py#L311-L324)）用三条规则判定：

```python
def _is_per_component_dict(spec: dict[str, Any]) -> bool:
    if "method" in spec or "quant_method" in spec:   # 有 method 键 → 普通单方法 dict
        return False
    if not all(isinstance(v, (dict, str, type(None))) for v in spec.values()):
        return False
    # 至少一个值是 None，或是带 method 的 dict，才认定（避免误判 {"activation_scheme":"static"}）
    return any(v is None or (isinstance(v, dict) and ("method" in v or "quant_method" in v))
               for v in spec.values())
```

最后一条很关键：它防止把 `{"activation_scheme": "static"}` 这种**全字符串值的扁平配置**误判成 per-component（key 是组件名）。只有当至少一个值是 `None` 或「带 method 的 dict」时，才认为 key 是组件前缀。

**② 构造：`_build_component_config`**

[_build_component_config](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py#L327-L355)（[factory.py:L327-L355](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py#L327-L355)）遍历每个 prefix，按值类型分派：`None`→不量化、`str`→`_build_single`、`dict`→取出 `method` 再构造。`prefix == "default"` 被单独存进 `default_config`，其余进 `component_configs` 字典，最后包成 `ComponentQuantizationConfig`。

**③ 路由：最长前缀匹配**

[ComponentQuantizationConfig.__init__](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/component_config.py#L78-L86) 预先把前缀**按长度降序**排好（[component_config.py:L86](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/component_config.py#L86)），[resolve](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/component_config.py#L88-L98) 取第一个 `prefix.startswith(comp_prefix)` 命中：

```python
def resolve(self, prefix: str) -> QuantizationConfig | None:
    for comp_prefix in self._sorted_prefixes:   # 长的在前
        if prefix.startswith(comp_prefix):
            return self._components[comp_prefix]
    return self._default
```

[get_quant_method](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/component_config.py#L103-L107) 就是 `resolve` + 委托子 Config，本身不碰任何量化算子。注释还提醒：vLLM 的 WeightsMapper 可能重映射前缀，若映射后前缀对不上会落到 default（[component_config.py:L90-L94](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/component_config.py#L90-L94)）。

**④ 两个保护函数：encoder 与 norm/modulation**

除了显式的 per-component 配置，omni 还有两条**自动**保护规则：

[resolve_encoder_quant_config](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/component_config.py#L32-L48)（[component_config.py:L32-L48](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/component_config.py#L32-L48)）：对**预量化方法**（`PRE_QUANTIZED_METHODS = {modelopt, modelopt_fp4, modelopt_mxfp8, modelopt_mixed}`，见 [component_config.py:L29](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/component_config.py#L29)）的 vision/audio encoder，强制返回 `None`——因为它们的权重是 BF16、checkpoint 里没有 scale。

[safe_quant_config](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/component_config.py#L51-L72)（[component_config.py:L51-L72](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/component_config.py#L51-L72)）：norm/modulation 层只接受预量化方法（如 INC/AutoRound 的 W4A16，需要 config 来正确加载打包权重），其余一律返回 `None`，防止 FP8 污染 shift/scale/gate。

这两个函数互为反面：前者从 encoder 里**剥掉**预量化配置，后者在 norm 层里**只留**预量化配置。

**⑤ 磁盘 reconcile：加载时再校准一次**

[resolve_quant_config_from_disk](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py#L418-L487)（[factory.py:L418-L487](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py#L418-L487)）在加载单个 transformer block（每个 block 有自己的 `config.json`，如级联模型 `transformer` + `transformer_2`）时，把「当前激活的 quant_config」与「磁盘 config.json 声明」对账：方法不一致直接 `raise ValueError`（防静默权重损坏）；磁盘标了序列化但当前是在线模式则按磁盘重建；`ignored_layers` 不同也重建。这是 per-component 思想在「加载期」的延伸。

#### 4.3.4 代码实践

**实践目标**：构造一个 per-component 配置，手工模拟 `resolve` 的最长前缀匹配，验证不同层命中不同子配置。

**操作步骤**（源码阅读型实践）：

1. 阅读构造链 [_build_component_config](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py#L327-L355) 与路由 [resolve](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/component_config.py#L88-L98)。
2. 给定配置与一组层名，**在纸上**（或最小脚本，示例代码）推演 `resolve` 结果：

```python
# 示例代码
spec = {
    "transformer": {"method": "fp8"},
    "transformer_2": None,     # 第二个 transformer 保持 BF16
    "vae": None,
    "default": None,
}
# 假设已通过 build_quant_config(spec) 得到 cqc
for prefix in ["transformer.blocks.0.attn.to_q",
               "transformer_2.blocks.0.attn.to_q",
               "vae.encoder.conv_in",
               "text_encoder.embeddings"]:
    print(prefix, "->", cqc.resolve(prefix).get_name() if cqc.resolve(prefix) else None)
```

**需要观察的现象**：

- `transformer.blocks.0.attn.to_q` 命中 `transformer`（不是 `transformer_2`，因为按长度降序、且 startswith 精确）；
- `transformer_2.blocks.0...` 命中 `transformer_2` → None；
- `vae.encoder.conv_in` 命中 `vae` → None；
- `text_encoder.embeddings` 不命中任何前缀 → default → None。

**预期结果**：四条层名分别得到 `fp8 / None / None / None`。注意一个陷阱：因为 `transformer_2` 的前缀 `transformer_` 不等于 `transformer`，`"transformer_2...".startswith("transformer")` 其实为 **True**！所以「按长度降序」是必须的——必须让更长的 `transformer_2` 先匹配，否则会被 `transformer` 抢走。这一点是本实践的观察重点。

#### 4.3.5 小练习与答案

**练习 1**：`resolve_encoder_quant_config` 为什么对 `ComponentQuantizationConfig` 直接放行（原样返回）？

**答案**：因为 per-component 配置已经由用户**显式**决定了哪些组件量化、哪些不量化（用户大概率已把 encoder 设成 None）。再叠加自动剥离规则会与用户意图冲突。所以该函数只对「单方法预量化配置」（如一把全开的 ModelOpt）做强制剥离，对 per-component 与 None 都原样返回（[component_config.py:L42-L48](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/component_config.py#L42-L48)）。

**练习 2**：`ComponentQuantizationConfig.get_config_filenames()` 返回空列表、`from_config()` 直接 `raise NotImplementedError`，为什么？

**答案**：因为它是一个**路由器**而非具体量化方法，自身不对应任何 checkpoint 权重文件，也没有可从单一 dict 反序列化的格式。它的正确构造路径只能是 `build_quant_config`（[component_config.py:L120-L125](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/component_config.py#L120-L125)），所以 `from_config` 显式禁用以防误用。

---

## 5. 综合实践：一次 ModelOpt FP8 量化导出的输入输出，以及它在 factory 中的注册

本任务把三个模块串起来：用 [examples/quantization/quantize_wan2_2_modelopt_fp8.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/quantization/quantize_wan2_2_modelopt_fp8.py) 把 Wan2.2 从 BF16 量化成 ModelOpt FP8 checkpoint，再回答「这个方法在 factory 中如何被注册」。

### 5.1 实践目标

- 看清一次离线 FP8 量化的**输入**（BF16 diffusers checkpoint + 校准 prompt）与**输出**（带 ModelOpt 元数据的 diffusers 目录）。
- 把「离线产出 checkpoint」与「在线/加载期消费 checkpoint」连成一条完整链路。
- 准确回答 ModelOpt 在 factory 里**不是 `_OVERRIDES`**，而是靠**元数据探测 + 委托 vLLM 注册表**注册的。

### 5.2 操作步骤

**第一步：读脚本的量化计划。** [main()](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/quantization/quantize_wan2_2_modelopt_fp8.py#L376-L467) 的输入输出可以这样概括：

| 项 | 内容 |
|----|------|
| **输入** | BF16 diffusers checkpoint（默认 `Wan-AI/Wan2.2-TI2V-5B-Diffusers`）+ 8 条默认校准视频 prompt（见 [L41-L50](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/quantization/quantize_wan2_2_modelopt_fp8.py#L41-L50)）+ 分辨率/帧数/步数等校准超参 |
| **量化对象** | DiT transformer（`pipe.transformer`，A14B 还可能有 `transformer_2`，见 [_list_transformers](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/quantization/quantize_wan2_2_modelopt_fp8.py#L328-L339)） |
| **量化算法** | `mtq.FP8_DEFAULT_CFG`（[L406](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/quantization/quantize_wan2_2_modelopt_fp8.py#L406)），靠 `forward_loop` 跑校准视频收集 amax 统计 |
| **保持全精度的层** | `condition_embedder`、`patch_embedding`、`norm_out`、`scale_shift_table`、`proj_out`、`timestep_proj_prepare` 等（见 [_filter_func_wan22](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/quantization/quantize_wan2_2_modelopt_fp8.py#L152-L159)）——这正对应 4.2 讲的精度敏感层黑名单，与 #2728/#2795 的模式一致 |
| **输出** | 一个 diffusers 风格目录：源目录逐字拷贝（去掉 `transformer` 子目录），再保存量化后的 transformer；并**改写每个 `transformer/config.json`**，注入 ModelOpt FP8 元数据 |

**第二步：看输出的元数据是怎么写进去的。** [_wan22_quant_config_block](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/quantization/quantize_wan2_2_modelopt_fp8.py#L275-L301)（[L275-L301](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/quantization/quantize_wan2_2_modelopt_fp8.py#L275-L301)）写入的关键字段是：

```python
"producer": {"name": "modelopt"},
"quant_algo": "FP8",
"quant_method": "modelopt",
```

由 [_patch_quant_config](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/quantization/quantize_wan2_2_modelopt_fp8.py#L304-L325)（[L304-L325](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/quantization/quantize_wan2_2_modelopt_fp8.py#L304-L325)）注入到 `transformer/config.json` 与 `transformer_2/config.json`。脚本末尾还打印一句重要提示（[L464-L467](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/quantization/quantize_wan2_2_modelopt_fp8.py#L464-L467)）：`--quantization fp8` 会在运行时**自动升级**成 ModelOpt FP8，正是因为 config.json 带了这些元数据。

**第三步：回答「在 factory 中如何注册」。** 这是关键。打开 [factory.py:L142-L151](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py#L142-L151) 的 `_OVERRIDES`，你会发现**里面根本没有 `modelopt` / `modelopt_fp4` / `modelopt_mixed`**。ModelOpt 的注册路径是：

1. **方法名本身来自 vLLM 注册表**：`modelopt`、`modelopt_fp4`、`modelopt_mixed` 是 vLLM `QUANTIZATION_METHODS` 的成员，由 `get_quantization_config(method)` 提供 Config 类。
2. **omni 负责探测具体变体**：[_detect_modelopt_method](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py#L231-L262) 读 checkpoint 的 `quant_algo`/`producer`/`method`，把 `quant_algo="FP8"` 翻译成 `method="modelopt"`。
3. **再委托 vLLM 构造**：[_build_modelopt_from_config](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py#L265-L269) 调 `get_quantization_config("modelopt").from_config(...)`。

换言之：**ModelOpt 是「委托」的纯粹案例——方法注册在 vLLM，omni 只加了「探测 + NaN 钳位 patch + checkpoint 适配器」三件增值物。** 这与 `mxfp8`（注册在 omni `_OVERRIDES`）形成鲜明对比，正好印证 4.1 讲的「委托 + 补丁」二分法。

**第四步（可选运行）：实际跑一次量化并验证。**

```bash
python examples/quantization/quantize_wan2_2_modelopt_fp8.py \
    --model Wan-AI/Wan2.2-TI2V-5B-Diffusers \
    --output ./wan22-ti2v-modelopt-fp8 --overwrite
```

### 5.3 需要观察的现象

- 运行结束（脚本 [_summarize_export](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/quantization/quantize_wan2_2_modelopt_fp8.py#L220-L237)）应打印 `quant_method: modelopt`、`quant_algo: FP8`、`producer: modelopt ...`；
- 输出目录的 `transformer/config.json` 里 `quantization_config` 块含有上述三字段；
- `_force_export_quantized_weights`（[L240-L272](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/quantization/quantize_wan2_2_modelopt_fp8.py#L240-L272)）报告「N 个权重已转 FP8」，若为 0 说明校准没覆盖到任何层（会直接 `SystemExit`）。

### 5.4 预期结果

把整条链路画成一张图：

```
[BF16 diffusers checkpoint]
   │  quantize_wan2_2_modelopt_fp8.py (mtq.FP8_DEFAULT_CFG + 校准)
   ▼
[带 modelopt 元数据的 FP8 checkpoint]   ← quant_algo=FP8, producer=modelopt
   │  vllm serve / Omni(model=...) 加载
   ▼
factory: _detect_modelopt_method → "modelopt"
   │  _build_modelopt_from_config → vLLM get_quantization_config("modelopt").from_config()
   ▼
ModelOpt FP8 Config → get_quant_method → ModelOpt FP8 LinearMethod
   （加载期叠加 NVFP4 NaN 钳位 patch，仅 NVFP4 变体）
```

无 GPU 环境下第四步标注「待本地验证」；前三步（源码阅读 + 元数据分析）可独立完成并得出 5.2 第三步的结论。

---

## 6. 本讲小结

- **统一入口 + 委托**：`build_quant_config` 是唯一入口，按 `None / QuantizationConfig / str / dict / per-component dict` 五种形态分流；`_build_single` 的解析顺序是 **omni `_OVERRIDES` 优先，否则委托 vLLM `QUANTIZATION_METHODS` 注册表**。
- **支持列表**：`SUPPORTED_QUANTIZATION_METHODS = QUANTIZATION_METHODS + _OVERRIDES` 去重合并，omni 扩展的方法有 `int8/bitsandbytes/mxfp8/mxfp4/mxfp4_dualscale/inc/auto-round` 等。
- **在线 vs 离线**：由 `is_checkpoint_*_serialized` 标志区分；在线方法靠 `_LazyWeightMixin` 先占位、加载完成后在 `process_weights_after_loading` 现场量化，离线方法直接注册已量化权重 + scale，两者最终合流到同一套 `apply`。
- **平台分派**：`get_quant_method` 用 `current_omni_platform.is_cuda()/is_npu()/is_xpu()` 在「在线/离线 × 平台」的十字交叉里选 LinearMethod；FP8/ModelOpt 属 NVIDIA 系，MXFP8/MXFP4/msModelSlim 属 NPU 系。
- **ModelOpt 是纯委托**：方法注册在 vLLM，omni 只加「探测变体（`_detect_modelopt_method`）+ NVFP4 NaN 钳位 patch + checkpoint 适配器」三件增值物；预量化 checkpoint 不应再传 `--quantization fp8`。
- **per-component 路由**：`ComponentQuantizationConfig` 用最长前缀匹配把不同层前缀转发给不同子 Config（或 None），辅以 `resolve_encoder_quant_config`（保护 encoder）与 `safe_quant_config`（保护 norm/modulation）两条自动规则。

## 7. 下一步学习建议

- **u8-l2（平台抽象）**：本讲反复用到的 `current_omni_platform.is_cuda()/is_npu()/is_xpu()` 就是下一讲的主角，读完你会更清楚「平台如何决定默认量化方法与默认注意力后端」。
- **u8-l3（基准测试与性能剖析）**：量化的最终价值是省显存、提吞吐，下一讲会讲 `bench_attention_backends`、`quantization_quality` 等评测脚本，以及 `compare_diffusion_trajectory_similarity`（本讲已露面）的完整用法。
- **继续阅读源码**：想深入 MXFP4 双尺度（dual-scale）可读 [vllm_omni/quantization/mxfp4_config.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/mxfp4_config.py)；想看 ModelOpt checkpoint 的逐权重适配可读 [vllm_omni/diffusion/model_loader/checkpoint_adapters/modelopt.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/model_loader/checkpoint_adapters/modelopt.py)（`ModelOptFp8CheckpointAdapter`）。
- **回看关联讲义**：u2-l1 的 patch 机制（NVFP4 NaN 钳位）、u2-l2 的配置体系（`OmniDiffusionConfig.quantization_config` 的传播）、u5-l3 的 Diffusion Worker（量化算子最终在 worker 进程的 `execute_model` 里被调用）共同构成量化的完整上下文，建议交叉对照。
