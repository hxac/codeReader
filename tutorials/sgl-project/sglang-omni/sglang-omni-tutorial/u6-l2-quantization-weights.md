# 量化、权值加载与校验

> 适用读者：已学完 u4-l3（ModelRunner 与 AR 前向路径），知道一次前向最终落到 `model_runner.model` 上的一堆 `torch.Tensor`。
> 本讲回答三个问题：这些张量在磁盘上是**压缩过的（量化）**，框架如何识别并预处理？当磁盘上的参数名跟运行时模块树**对不上**时怎么办？在线 RL 改完权重后，如何**严格证明**权值确实变了、且变得正确？

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清 **SGLang 与 SGLang-Omni 在量化上的分工边界**——谁负责检测、谁负责构造量化层、谁只补一段「omni 专属的兼容代码」。
2. 读懂 [`sglang_omni/quantization.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/quantization.py) 里的两条关键路径：FP8 块量化的 **scale 预处理** 与 AutoRound 的 **阶段局部配置归一化**。
3. 理解**何时需要自定义权值加载器**，并以 Higgs TTS 的 [`DiscreteWeightMapper`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/higgs_tts/weight_loader.py) 为模板说清它的形态与职责。
4. 读懂 [`StrictWeightChecker`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/weight_checker.py) 的 SHA256 摘要由哪些字段构成、`snapshot/compare/checksum` 三件事分别解决什么问题，并能解释**为什么全模型校验会阻塞推理**。

---

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**量化（Quantization）是什么。** 神经网络的权重默认是 `float32`/`bfloat16`。量化把它们压成更窄的整数或低比特浮点（如 INT4、FP8），用更少显存装下更大模型、换更快的访存。代价是：权重不再是「能直接乘」的张量，必须带上**缩放因子（scale）**才能还原。不同量化方案存的 scale 形态不同（逐张量、逐通道、逐块），名字也不同（`weight_scale`、`weight_scale_inv`、`weight_block_size`……）。本讲不教你如何量化，只讲框架**如何把别人量化好的 checkpoint 正确喂进运行时**。

**权值加载（Weight Loading）是什么。** 一个 checkpoint 就是一堆 `(参数名, 张量)` 的流（通常是 `.safetensors`）。加载要做两件事：把名字**对到**运行时模型模块树的对应位置（`model.layers.0.self_attn.q_proj.weight` ←→ checkpoint 里的某个 key）；必要时对张量做**形态变换**（如把分开存的 `q/k/v` 拼成 `qkv_proj`）。当 checkpoint 的命名约定跟运行时模块树一致时，SGLang 上游已经能自动加载；不一致时，就需要模型自己写一个「名字重映射器」。

**权值校验（Weight Checking）是什么。** 在线 RL（Reinforcement Learning）会在训练侧算出新权重，再通过 admin 控制平面热更新到推理 worker（见 u6-l4）。问题是：你怎么知道权重「真的更新了」「更新对了」？`StrictWeightChecker` 的做法是对模型里**每一个张量**算一个 SHA256 摘要，更新前后一比，就知道谁变了、变成什么。这是「严格」的——不是抽样，是全量逐张量。

> 贯穿全讲的一个核心边界判断：**SGLang 拥有量化的端到端实现**（解析 `quantization_config`、构造量化层、跑 post-load 钩子），SGLang-Omni 只在 SGLang **推断不出来**的地方补一段兼容代码。这句话来自 [`quantization.py` 的模块文档](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/quantization.py#L1-L9)，请记住它，后面三条主线都是它的展开。

---

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用到 |
| --- | --- | --- |
| [`sglang_omni/quantization.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/quantization.py) | **量化兼容胶水层**。解析量化配置、选择量化方法名、提供 FP8 scale 预处理器与 AutoRound 配置归一化。 | 模块 4.1 主角 |
| [`sglang_omni/models/higgs_tts/weight_loader.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/higgs_tts/weight_loader.py) | **自定义权值名重映射器**示例。把 Higgs checkpoint 的前缀命名翻译成 sglang 的参数树。 | 模块 4.2 主角 |
| [`sglang_omni/model_runner/weight_checker.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/weight_checker.py) | **严格 SHA256 校验器**。对每个张量算摘要、聚合出 per-rank 校验和、支持快照与比对。 | 模块 4.3 主角 |
| [`sglang_omni/model_runner/model_worker.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/model_worker.py) | 集成点：调用量化适配器、暴露 `weights_checker` 与各 `update_weights_*` 方法。 | 串联三模块 |
| [`sglang_omni/models/qwen3_omni/components/talker.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/components/talker.py) | 集成点：在 `load_weights` 里**选择启用** FP8 scale 预处理。 | 模块 4.1 例子 |
| [`sglang_omni/models/higgs_tts/model.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/higgs_tts/model.py) | 集成点：在 `load_weights` 里实例化 `DiscreteWeightMapper` 并分拣权重。 | 模块 4.2 例子 |
| [`sglang_omni/proto/admin.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/admin.py) | admin 动作常量（`update_weights_from_disk` / `weights_checker` 等）。 | 模块 4.3 上下文 |
| [`docs/developer_reference/rl_admin_control.md`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/rl_admin_control.md) | 权重热更新与校验的设计文档（含「Weight Checker」小节）。 | 模块 4.3 参考 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，正好对应规格里的四项（量化、weight_loader、weight_checker、SHA256 校验，后两项同属一个模块）。

---

### 4.1 量化策略选择与配置归一化

#### 4.1.1 概念说明

回顾 §2 的边界判断：**SGLang 拥有量化的端到端实现**。这意味着检测「这个 checkpoint 是不是量化的、用了什么方案」、构造对应的量化层（如 `FP8Linear`）、跑加载后的反量化钩子——全是 SGLang 上游的活。

那 [`quantization.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/quantization.py) 这个 omni 自己的模块到底补了什么？模块文档说得很直白：它只提供 **SGLang 自己推断不出来的、Qwen3-Omni 专属的兼容性**，目前落地为两件事：

1. **阶段局部（stage-local）的 AutoRound 配置归一化**：AutoRound 是一种 INT4 量化，它的配置里用正则名匹配「要量化哪些 block」。但 Qwen3-Omni 是多阶段模型，同一份 checkpoint 在不同阶段被加载时，模块名前缀（`thinker.` / `talker.`）会变。SGLang 看不到「我现在在加载哪个阶段」，所以这段前缀剥离只能 omni 来做。
2. **给自定义权值加载器用的 FP8 scale 预处理**：原生块量化 FP8 checkpoint 把缩放因子存成 `weight_scale_inv`（即 scale 的倒数），而 SGLang 运行时期望的是 scale 本身。omni 提供一个「逐张量取倒数」的预处理器，由**模型自己的 `load_weights` 决定是否启用**。

一句话：**检测与执行归 SGLang，命名/约定的微调归 omni**。

> 术语提示：**`quantization_config`** 是 HuggingFace config.json 里的一个字段，描述「这个 checkpoint 是怎么量化的」，典型含 `quant_method`（如 `"fp8"`、`"auto-round"`）、`weight_block_size`、`extra_config` 等。本模块的输入输出几乎都围绕这一个 dict。

#### 4.1.2 核心流程

量化兼容代码在模型加载前被触发，整体分两条互不相干的支线：

```text
                     model_config (含 hf_config)
                              │
            ┌─────────────────┴──────────────────┐
            ▼                                     ▼
   【支线 A：配置归一化】                  【支线 B：scale 预处理】
   _apply_omni_quantization_adapters        get_weight_preprocessor
   (model_worker 启动时调用一次)            (模型的 load_weights 里逐张量调用)
            │                                     │
   resolve_quant_config ──► quant_dict            │
            │                                     │
   needs_quant_config_normalization?              │
      (method == "auto-round")                    │
            │ yes                                 │
   normalize_quant_config                         │
   (剥掉 thinker./talker. 前缀)                   │
                                                 resolve_quant_config ──► quant_dict
                                                 is_fp8_block_quant?
                                                    (method=="fp8" 且有 weight_block_size)
                                                    │ yes 且 fp8_scale_inverted=True
                                                 convert_fp8_weight_scale_inv
                                                 (对 weight_scale_inv 张量取倒数)
```

两条支线都从同一个入口函数 `resolve_quant_config(config)` 拿到「量化配置 dict」开始。下面分别精读。

#### 4.1.3 源码精读

**(1) 找到量化配置：`resolve_quant_config`**

量化配置可能藏在根 config，也可能藏在多阶段模型的子 config（`text_config` / `thinker_config` / `talker_config`）里。该函数用带 `visited` 去重的深度搜索把它挖出来，见 [sglang_omni/quantization.py:68-88](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/quantization.py#L68-L88)：

```python
_QUANT_METADATA_KEYS: tuple[str, ...] = ("quantization_config", "compression_config")
_NESTED_QUANT_CONFIG_ATTRS: tuple[str, ...] = (
    "text_config", "thinker_config", "talker_config",
)

def resolve_quant_config(config: Any) -> dict[str, Any] | None:
    visited: set[int] = set()
    def _search(node: Any) -> dict[str, Any] | None:
        if node is None or id(node) in visited:
            return None
        visited.add(id(node))
        for key in _QUANT_METADATA_KEYS:        # 先在本节点找
            raw_config = _read_metadata(node, key)
            if raw_config is not None:
                return _to_mutable_dict(raw_config, key)
        for attr in _NESTED_QUANT_CONFIG_ATTRS:  # 再下沉到子 config
            found = _search(_read_metadata(node, attr))
            if found is not None:
                return found
        return None
    return _search(config)
```

`_to_mutable_dict`（[L45-L58](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/quantization.py#L45-L58)）把可能是 `dict`、也可能是带 `to_dict()` 的对象、也可能是普通对象的配置统一转成可变 dict——因为后面归一化要**就地改写**它。

**(2) 标准化方法名：`quant_method_name`**

拿到 dict 后，先取出并标准化方法名，把 `"fp8"` / `"auto-round"` / `"AutoRound"` 等大小写、下划线差异归一，见 [sglang_omni/quantization.py:91-98](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/quantization.py#L91-L98)：`str(method).lower().replace("_", "-")`。后续所有判断都基于这个标准化后的名字。

**(3) 支线 B：FP8 scale 预处理器**

先看判定函数 [`is_fp8_block_quant`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/quantization.py#L101-L107)：方法名是 `fp8` **且**配置里带 `weight_block_size`，才算「原生块量化 FP8」。

真正的预处理逻辑在 [`convert_fp8_weight_scale_inv`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/quantization.py#L110-L129)：

```python
def convert_fp8_weight_scale_inv(target_name, loaded_weight):
    if not target_name.endswith("weight_scale_inv"):
        return loaded_weight          # 非 scale 张量原样返回
    import torch
    if not torch.is_floating_point(loaded_weight):
        raise TypeError(...)
    if loaded_weight.numel() == 0:        raise ValueError(...)
    if not bool(torch.isfinite(loaded_weight).all().item()):
        raise ValueError(...)             # 非有限值（inf/nan）直接报错
    if bool(torch.any(loaded_weight == 0).item()):
        raise ValueError(...)             # 含 0 会触发除零，先拦下
    return torch.reciprocal(loaded_weight)
```

它做了三件事：①只对名字以 `weight_scale_inv` 结尾的张量动手；②做三道**健康检查**（浮点、非空、有限、非零），任何一道不过就抛异常——因为一个坏 scale 会让后续反量化整个崩；③对合法 scale 取倒数。

为什么是取倒数？checkpoint 存的是 `weight_scale_inv`（scale 的倒数），SGLang 运行时要的是 scale 本身：

\[
s \;=\; \frac{1}{\text{weight\_scale\_inv}}
\]

「非零检查」正是为了保证 `reciprocal` 不会除零。**注意**：这里只做命名约定的转换，**不碰真正的反量化**——把 FP8 权重乘回 float 是 SGLang 量化层在 forward 时干的事，依旧守着「SGLang 拥有执行」的边界。

谁决定启用它？看 [`get_weight_preprocessor`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/quantization.py#L138-L148)：

```python
def get_weight_preprocessor(config=None, *, fp8_scale_inverted=False):
    quant_dict = resolve_quant_config(config)
    if fp8_scale_inverted and is_fp8_block_quant(quant_dict):
        return convert_fp8_weight_scale_inv
    return _identity_preprocessor          # 默认：原样返回
```

关键设计：是否取倒数由调用方传的 `fp8_scale_inverted=True` **显式 opt-in**，而不是自动判断。因为「这个 checkpoint 的 scale 是不是倒着存的」只有**模型作者**知道。Qwen3-Omni Talker 就是这么启用的，见 [sglang_omni/models/qwen3_omni/components/talker.py:1771-1773](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/components/talker.py#L1771-L1773)：

```python
preprocess_weight = get_weight_preprocessor(
    self.root_config, fp8_scale_inverted=True
)
```

随后在加载循环里，每装载一个参数都先过一遍 `preprocess_weight(mapped, loaded_weight)`（见 [talker.py:1789](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/components/talker.py#L1789)、[L1802](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/components/talker.py#L1802)、[L1818](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/components/talker.py#L1818)）。对绝大多数权重它是恒等函数，只在遇到 `weight_scale_inv` 时才真正取倒数——开销几乎为零。

**(4) 支线 A：AutoRound 配置归一化**

判定函数 [`needs_quant_config_normalization`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/quantization.py#L151-L154) 只认 `auto-round` 一种方法。入口 [`normalize_quant_config`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/quantization.py#L264-L285) 做三步：从 `hf_config` 里定位「可写回的」量化配置（`_load_writable_quant_config`）；按当前阶段架构查出前缀（`_resolve_stage_prefix`）；最后剥前缀。

前缀表在 [sglang_omni/quantization.py:28-32](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/quantization.py#L28-L32)：

```python
_STAGE_PREFIX_BY_ARCH: dict[str, str] = {
    "Qwen3OmniThinkerForCausalLM": "thinker.",
    "Qwen3ASRForConditionalGeneration": "thinker.",
    "Qwen3OmniTalker": "talker.",
}
```

它对量化配置里的两类名字做前缀剥离：`block_name_to_quantize`（要量化的 block 名，见 [`_normalize_block_name_to_quantize`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/quantization.py#L194-L220)）与 `extra_config` 的正则键（见 [`_normalize_extra_config_keys`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/quantization.py#L171-L191)）。例如 checkpoint 写的是 `thinker.model.layers.0...`，但加载 thinker 阶段时运行时模块名是 `model.layers.0...`（前缀已被剥掉），这里就把配置里的正则也同步剥掉，保证 SGLang 按名字匹配时能对上。注意 [`_strip_stage_prefix`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/quantization.py#L157-L168) 对 `.*thinker.` 这种带前导通配的正则会**保留通配、只剥前缀**，避免把语义改坏。

触发时机：模型 worker 初始化时，[`_apply_omni_quantization_adapters`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/model_worker.py#L562-L575) 在 **SGLang 构造它自己的 config 之前**调用一次归一化：

```python
def _apply_omni_quantization_adapters(model_config: ModelConfig) -> None:
    quant_dict = resolve_quant_config(getattr(model_config, "hf_config", None))
    if quant_dict is None:
        return
    if needs_quant_config_normalization(quant_dict):
        normalize_quant_config(model_config)
```

注释点明：SGLang 拥有检测、解析、构造、post-load 钩子；omni 唯一要做的就是 AutoRound 的阶段局部命名归一化。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `get_weight_preprocessor` 在不同配置下的分支选择，并观察 FP8 scale 预处理器的输入输出。无需 GPU，纯 CPU 可跑。

**操作步骤**（在装好本项目的环境里执行 `python`）：

```python
# 示例代码：复现量化预处理器的分支逻辑（无需 GPU/权重）
import torch
from types import SimpleNamespace
from sglang_omni.quantization import (
    get_weight_preprocessor, resolve_quant_config,
    quant_method_name, is_fp8_block_quant,
)

def mk_cfg(method=None, block_size=None):
    q = {}
    if method is not None:
        q["quant_method"] = method
    if block_size is not None:
        q["weight_block_size"] = block_size
    return SimpleNamespace(quantization_config=q or None)

# 1) 无量化 → 恒等预处理器
print(get_weight_preprocessor(mk_cfg()))                      # <function _identity_preprocessor>

# 2) fp8 但非块量化 → 仍恒等（is_fp8_block_quant 为 False）
print(get_weight_preprocessor(mk_cfg("fp8")))                 # <function _identity_preprocessor>

# 3) fp8 块量化 + 显式 opt-in → 返回 convert_fp8_weight_scale_inv
print(get_weight_preprocessor(mk_cfg("fp8", [128, 128]),
                              fp8_scale_inverted=True))       # <function convert_fp8_weight_scale_inv>

# 4) 直接观察取倒数行为
pre = get_weight_preprocessor(mk_cfg("fp8", [128, 128]), fp8_scale_inverted=True)
scale_inv = torch.tensor([2.0, 4.0, 8.0])
print(pre("layers.0.weight_scale_inv", scale_inv))            # tensor([0.5000, 0.2500, 0.1250])
print(pre("layers.0.weight", scale_inv))                      # 普通权重原样返回：tensor([2., 4., 8.])
```

**需要观察的现象**：

- 前三种情况返回的函数对象不同（恒等 vs 取倒数），印证「只有 fp8 块量化 + 显式 opt-in 才启用」。
- 第 4 步：同一个张量，名字带 `weight_scale_inv` 时被取倒数，普通权重名时原样返回。
- 把 `scale_inv` 里某个值改成 `0.0` 再跑第 4 步，应抛 `ValueError: Invalid zero FP8 scale tensor...`——印证健康检查生效。

**预期结果**：分支选择与上面注释一致；取倒数结果为 `[0.5, 0.25, 0.125]`。

> 如果环境里 `torch` 不可用或包未安装，本实践可作为「源码阅读型实践」：直接对照 [quant_method_name](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/quantization.py#L91-L98)、[is_fp8_block_quant](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/quantization.py#L101-L107)、[get_weight_preprocessor](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/quantization.py#L138-L148) 三个函数手推分支结论，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `get_weight_preprocessor` 不自动判断「要不要取倒数」，而要让模型传 `fp8_scale_inverted=True`？

**参考答案**：因为「checkpoint 的 scale 是否以倒数形式存储」是**模型/checkpoint 专属的约定**，光看 `quantization_config` 无法区分（同样是 `fp8` + 块量化，不同来源的 checkpoint 可能用不同约定）。把决策权交给最了解 checkpoint 的模型作者（如 talker 的 `load_weights`），比让框架瞎猜更安全，也守住了「SGLang 拥有执行、omni 只补兼容」的边界。

**练习 2**：`is_fp8_block_quant` 为什么要同时检查 `weight_block_size`，而不是只看 `quant_method == "fp8"`？

**参考答案**：`"fp8"` 方法既可能是逐张量/逐通道的 FP8，也可能是**块量化** FP8。只有带 `weight_block_size` 才是后者，而 omni 的 scale 倒数约定（`weight_scale_inv`）只针对**块量化** FP8 checkpoint。不加这个条件会把不该取倒数的 FP8 权重也取倒数，导致数值错误。

**练习 3**：`normalize_quant_config` 在什么架构下会真正改写配置？`Qwen3OmniThinkerForCausalLM` 还是其它？

**参考答案**：只有当 `hf_config.architectures[0]` 命中 [`_STAGE_PREFIX_BY_ARCH`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/quantization.py#L28-L32)（`Qwen3OmniThinkerForCausalLM` / `Qwen3ASRForConditionalGeneration` / `Qwen3OmniTalker`）时，`_resolve_stage_prefix` 才返回非空前缀，归一化才可能改写。但还要叠加 `needs_quant_config_normalization` 为真（方法须是 `auto-round`），二者同时成立才会剥前缀。

---

### 4.2 自定义权值加载器与权值名重映射

#### 4.2.1 概念说明

当一个 checkpoint 的参数命名跟运行时模块树**天然一致**时（如标准 Qwen3），SGLang 上游的 `load_weights` 就能自动装载，模型作者什么都不用写。但当命名**不一致**——典型是第三方/自研模型把权重按自己的前缀组织（如 `tied.embedding.text_embedding.`、`body.layers.`）——就需要一个**权值名重映射器（weight-name remapper）**：在把 `(name, tensor)` 交给 SGLang 的加载器之前，先把 `name` 翻译成运行时认识的模块名。

Higgs TTS 就是这样一个例子。它的 checkpoint 用 `tied.embedding.modality_embeddings.0.embedding.` 这类前缀，而 sglang 期望的是 `multimodal_embedding.modality_embedding_0.` 这套参数树。仓库给出的模板是 [`sglang_omni/models/higgs_tts/weight_loader.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/higgs_tts/weight_loader.py) 里的 [`DiscreteWeightMapper`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/higgs_tts/weight_loader.py#L15-L51)。

> 设计要点：这个 mapper 被刻意设计成**与目标前缀解耦**——它接受一个 `text_prefix_map` 参数，由不同模型 wrapper 注入各自的参数树布局（见[模块文档 L6-L8](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/higgs_tts/weight_loader.py#L1-L8)）。也就是说，重映射逻辑是**可复用的模板**，不是写死的查表。

#### 4.2.2 核心流程

权值加载在模型的 `load_weights(weights)` 里发生，`weights` 是 `(name, tensor)` 的迭代器。以 Higgs 为例的整体流程：

```text
for (name, tensor) in checkpoint 流:
        │
        ▼
   DiscreteWeightMapper.map(name)        # 重命名（或返回 None 丢弃）
        │
        ├──► mapped 以 "backbone." 开头?   →  剥掉 backbone. 前缀，塞进 backbone_weights
        │                                    （交给 Qwen3ForCausalLM.load_weights，它自己做 qkv 拼接）
        ├──► mapped 属于本模型自有模块?    →  塞进 self_weights（多模态 embedding/head）
        └──► 返回 None?                    →  跳过（如冻结的 audio tokenizer backbone）

最后：self.backbone.load_weights(iter(backbone_weights))   # 标准 LLM 加载
      遍历 self_weights，按名字装到自己的 param 上
```

核心思想是**分拣（split）**：把一条权重流按重映射后的归属，分流给「标准文本骨干」（复用 SGLang 加载器）和「本模型自有模块」（自己装），各走各的最优加载路径。

#### 4.2.3 源码精读

**(1) mapper 的数据结构与映射规则**

[`DiscreteWeightMapper`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/higgs_tts/weight_loader.py#L15-L51) 是一个冻结 dataclass，字段与映射方法如下：

```python
@dataclass(frozen=True)
class DiscreteWeightMapper:
    text_prefix_map: dict[str, str]                       # 由调用方注入的目标前缀表
    embedding_dest: str = "multimodal_embedding.modality_embedding_0."
    head_dest: str = "modality_head."
    tie_modality: bool = True                             # 必须与 ckpt 的 tie_word_embeddings 一致

    def _instance_prefix_map(self) -> dict[str, str]:
        mapping = {"tied.embedding.modality_embeddings.0.embedding.": self.embedding_dest}
        if not self.tie_modality:
            mapping["tied.head.modality_heads.0."] = self.head_dest   # 不 tie 时才要 head
        return mapping

    def map(self, name: str) -> str | None:
        for higgs_prefix, dest_prefix in self._instance_prefix_map().items():
            if name.startswith(higgs_prefix):
                return dest_prefix + name[len(higgs_prefix):]
        # 冻结的 audio tokenizer backbone —— 不在服务化图里，丢弃
        if name.startswith("tied.embedding.modality_embeddings.0.model."):
            return None
        for higgs_prefix, dest_prefix in self.text_prefix_map.items():
            if name.startswith(higgs_prefix):
                return dest_prefix + name[len(higgs_prefix):]
        return name                                              # 都不命中：原样返回
```

注意三个细节：①`map` 返回 `str | None`，`None` 表示**丢弃**该权重（用于冻结、不参与推理的骨干）；②前缀匹配的**顺序很关键**——`tied.embedding.modality_embeddings.0.model.`（要丢弃的 audio tokenizer）必须放在通用 embedding 映射**之后**单独判断，否则会被前面的 `0.embedding.` 误命中（实际上它因多了 `.model.` 后缀不会撞，但作者仍显式列出以表意）；③`tie_modality` 必须与 checkpoint 的 `tie_word_embeddings` 一致——tie 时 modality_head 共享 embedding 权重，checkpoint 里的 head 副本应被丢弃（不进 `mapping`），见[类文档 L18-L22](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/higgs_tts/weight_loader.py#L15-L22)。

**(2) 在 `load_weights` 里调用 mapper**

Higgs 模型在 [sglang_omni/models/higgs_tts/model.py:542-575](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/higgs_tts/model.py#L542-L575) 落地上面那条分拣流程：

```python
def load_weights(self, weights):
    mapper = DiscreteWeightMapper(
        text_prefix_map=_BACKBONE_PREFIX_MAP,
        tie_modality=self._tie_modality,
    )
    backbone_weights, self_weights, loaded = [], [], set()
    own_names = self._own_param_names()
    for name, tensor in weights:
        mapped = mapper.map(name)
        if mapped is None:
            continue                                   # 丢弃冻结骨干
        if mapped.startswith("backbone."):
            backbone_weights.append((mapped[len("backbone."):], tensor))
        elif mapped in own_names:
            self_weights.append((mapped, tensor))
    self.backbone.load_weights(iter(backbone_weights))  # 交给标准 LLM 加载器
    ...
```

这里能直接看到 §4.2.2 的分拣逻辑：mapped 名以 `backbone.` 开头 → 剥前缀进骨干流（骨干的 `load_weights` 会自己做 qkv/gate_up 拼接与 lm_head tying，见[方法文档 L546-L548](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/higgs_tts/model.py#L542-L549)）；mapped 名落在 `own_names` → 进自有模块流；`None` → 跳过。

**(3) 与 §4.1 的关系**

注意 `load_weights` 里的「名字重映射」和 §4.1 的「scale 预处理」是**正交的两件事**：前者改 `name`（张量该放哪），后者改 `tensor`（张量的数值形态）。一个 checkpoint 完全可能同时需要两者——Higgs 这里主要做名字重映射；Qwen3-Omni talker 走的是标准命名（不需要重映射），但需要 FP8 scale 预处理。理解这层正交关系，就不至于把两段代码搞混。

#### 4.2.4 代码实践

**实践目标**：用真实 mapper 跑通几个权值名，理解 `map` 的命中与丢弃逻辑。无需 GPU/权重。

**操作步骤**：

```python
# 示例代码：直接调用 DiscreteWeightMapper.map
from sglang_omni.models.higgs_tts.weight_loader import DiscreteWeightMapper

mapper = DiscreteWeightMapper(
    text_prefix_map={"tied.embedding.text_embedding.": "backbone.model.embed_tokens."},
    tie_modality=True,
)

cases = [
    "tied.embedding.modality_embeddings.0.embedding.weight",   # 多模态 embedding
    "tied.embedding.modality_embeddings.0.model.layer.0",      # 冻结的 audio tokenizer
    "tied.embedding.text_embedding.weight",                    # 文本骨干 embedding
    "tied.embedding.text_embedding.layers.0.self_attn.q_proj", # 文本骨干某层
    "some.unknown.prefix.weight",                              # 未命中
]
for n in cases:
    print(f"{n!r:60} -> {mapper.map(n)!r}")
```

**需要观察的现象**：

- 第 1 条被映成 `multimodal_embedding.modality_embedding_0.weight`（默认 `embedding_dest`）。
- 第 2 条返回 `None`（被判定为冻结骨干，丢弃）。
- 第 3、4 条前缀 `tied.embedding.text_embedding.` 被替换成 `backbone.model.embed_tokens.`，后续片段保留。
- 第 5 条原样返回（未命中任何规则）。
- 把 `tie_modality` 改成 `False` 再跑，观察 `_instance_prefix_map` 多出一条 head 映射。

**预期结果**：输出与上述一一对应。若未安装包，可对照 [`map`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/higgs_tts/weight_loader.py#L37-L51) 手推，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`DiscreteWeightMapper` 为什么把 `text_prefix_map` 设计成构造参数，而不是硬编码在类里？

**参考答案**：为了让重映射逻辑**可复用**。不同模型 wrapper 的参数树布局不同（骨干可能挂在 `backbone.` 也可能挂在别的前缀下），把目标前缀表作为参数注入，同一个 mapper 就能服务于多种布局，符合模块文档所述「mapping function is parameterised by the destination prefix」。

**练习 2**：`map` 返回 `None` 与返回原 `name` 有什么区别？分别意味着什么？

**参考答案**：返回 `None` 表示**主动丢弃**该权重（如冻结、不参与推理的 audio tokenizer backbone），加载循环里 `continue` 跳过；返回原 `name` 表示「未命中任何重映射规则」，权重会按原名继续走后续装载逻辑（可能装上，也可能因找不到对应 param 而被忽略）。两者语义相反：一个是「故意不要」，一个是「没规则、按默认处理」。

**练习 3**：`tie_modality=True` 时，checkpoint 里 `tied.head.modality_heads.0.*` 这些权重会怎样？

**参考答案**：因为 tie 时 modality_head 共享 embedding 权重、不需要单独的 head 权重，`_instance_prefix_map` 不会把 `tied.head.modality_heads.0.` 加入映射表（见 [L33-L34](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/higgs_tts/weight_loader.py#L29-L35) 的 `if not self.tie_modality`）。于是这些权重既不命中 modality 映射、也不命中 text 映射，原样返回后通常找不到对应 param 而被忽略——等价于「丢弃 checkpoint 里多余的 head 副本」。

---

### 4.3 weight_checker 的 SHA256 严格校验

#### 4.3.1 概念说明

在线 RL 场景下，训练侧会不断产出新权重，通过 admin 控制平面热更新到推理 worker（见 u6-l4 与 [`docs/developer_reference/rl_admin_control.md`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/rl_admin_control.md)）。一个自然的疑问是：**「更新」真的发生了吗？哪些张量变了？有没有意外多出/少了张量？**

回答这个问题最可靠的方式是**对模型的每一个张量算密码学摘要**，更新前后对比。这就是 [`StrictWeightChecker`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/weight_checker.py) 的职责。它的关键词是 **strict（严格）**：

- **不抽样**：遍历模型全部 `named_parameters` 与 `named_buffers`，一个不漏。
- **SHA256**：用密码学哈希，碰撞概率可忽略，足以作为「权值身份」的指纹。
- **多字段**：摘要不只看字节，还把名字、dtype、shape 都纳入——任何一项变了都算「变了」。

它对外暴露四个动作（见 [run 方法](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/weight_checker.py#L38-L50)）：

| 动作 | 作用 |
| --- | --- |
| `snapshot` | 拍下当前全模型摘要，存为基线（用于事后 compare） |
| `reset_tensors` | 同 snapshot，语义上表示「以此为新基线」 |
| `compare` | 与上次 snapshot 比对，报告 missing / unexpected / changed |
| `checksum` | 一次性算出当前摘要与 per-rank 校验和（不存基线） |

> 重要前提：**全模型 SHA256 校验会阻塞推理**。因为它要遍历并把每个张量拷到 CPU 取原始字节来哈希，期间该 worker 无法服务请求。设计文档 [`rl_admin_control.md` 的 Weight Checker 小节](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/rl_admin_control.md#L88-L94) 明确写了这一点，源码里也用 `logger.warning` 反复提醒（见下文）。

#### 4.3.2 核心流程

校验器的典型使用节奏（在线 RL 验证权重更新）：

```text
1) POST /weights_checker  action=snapshot      # 更新前拍基线，得到一组摘要 + per_gpu_checksum
2) POST /update_weights_from_disk  ...          # 训练侧触发权重热更新（见 u6-l4）
3) POST /weights_checker  action=compare        # 更新后再算一次，与基线比对
        │
        ├──► missing    : 基线有、现在没有的张量（结构变了 / 权重被删）
        ├──► unexpected : 现在有、基线没有的张量（新结构 / 意外注入）
        ├──► changed    : 名字相同但 sha256/shape/dtype 任一不同的张量
        └──► matched    : 三者皆空才为 True（完全一致）
```

若只想留一个「当前指纹」做记录（不比对），用 `checksum`：它返回每个张量的摘要、所有张量名→摘要的映射，以及一个聚合出的 `per_gpu_checksum`。

聚合校验和的算法很关键——它**按张量名排序后再哈希**（见 [`_aggregate_checksum`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/weight_checker.py#L190-L195)），这样即使遍历顺序不同（不同 rank、不同 PyTorch 版本），只要「名字集合 + 各自摘要」相同，per-rank 校验和就一致，便于跨 rank 比对。

#### 4.3.3 源码精读

**(1) 每个张量的摘要由哪些字段构成**

这是本模块最核心的问题，答案在 [`_digest_tensor`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/weight_checker.py#L150-L161)：

```python
def _digest_tensor(name: str, tensor: Any) -> TensorDigest:
    detached = tensor.detach() if hasattr(tensor, "detach") else tensor
    contiguous = detached.contiguous() if hasattr(detached, "contiguous") else detached
    cpu = contiguous.cpu() if hasattr(contiguous, "cpu") else contiguous
    shape = tuple(int(x) for x in getattr(cpu, "shape", ()))
    dtype = str(getattr(cpu, "dtype", type(cpu).__name__))
    h = hashlib.sha256()
    h.update(name.encode())        # 字段 1：参数名
    h.update(dtype.encode())       # 字段 2：dtype 字符串
    h.update(str(shape).encode())  # 字段 3：shape 元组的字符串
    h.update(_tensor_bytes(cpu))   # 字段 4：张量原始字节
    return TensorDigest(name=name, shape=shape, dtype=dtype, sha256=h.hexdigest())
```

四个字段参与 SHA256：**name + dtype + shape + 原始字节**。也就是说，即便两个张量字节完全相同，只要名字/dtype/shape 任一不同，摘要就不同。预处理顺序也讲究：`detach`（脱离计算图）→ `contiguous`（保证内存连续）→ `cpu`（搬到 CPU 才能安全取字节）。

`TensorDigest`（[L15-L28](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/weight_checker.py#L15-L28)）把这四样存成一个 dataclass，`to_dict()` 用于序列化进响应。

**取字节要兼容低精度 dtype**：[`_tensor_bytes`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/weight_checker.py#L164-L187) 先试 `tensor.numpy().tobytes()`；失败（如 `bfloat16`/`float8_e4m3fn` 在某些 torch 版本上 `.numpy()` 不支持）就退回 `tensor.view(torch.uint8)` 再取字节；再不行试 `tobytes()`。这条回退路径有[单测覆盖](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/model_runner/test_weight_checker.py#L53-L71)（bfloat16、float8），保证量化权重也能被正确摘要。

**(2) 遍历模型 + 阻塞推理的根因**

[`_digest_model`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/weight_checker.py#L87-L106) 是阻塞的源头：

```python
def _digest_model(self) -> dict[str, TensorDigest]:
    model = getattr(self._model_runner, "model", None)
    if model is None:
        raise RuntimeError("model_runner has no model for weights_checker")
    logger.warning(
        "weights_checker: starting full-model SHA256 digest; "
        "inference is blocked until this completes. "
        "Elapsed time will be reported in the response."
    )
    t0 = time.time()
    digests: dict[str, TensorDigest] = {}
    for name, tensor in self._iter_named_tensors(model):
        digests[name] = _digest_tensor(name, tensor)
    logger.warning("weights_checker: digest complete; %d tensors in %.1fs",
                   len(digests), time.time() - t0)
    return digests
```

「阻塞推理」的本质：①这是**同步**遍历，调用它的线程（admin/scheduler 线程）要等所有张量算完才返回；②每个张量都要 `.cpu()` 拷贝 + 取字节 + SHA256，对大模型（几千个张量、几十 GB）耗时可达**秒级甚至更久**；③在这期间该 worker 无法处理推理请求。所以源码用 `logger.warning` 明确告警，并在响应里回传 `elapsed_s` 让调用方知道到底阻塞了多久。

[`_iter_named_tensors`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/weight_checker.py#L108-L127) 用 `id()` 去重，避免 tied/共享参数被重复摘要（同一个张量对象只算一次），同时覆盖 `named_parameters` 与 `named_buffers`（量化里的 scale、running stats 常作为 buffer 存在）。

**(3) 比对逻辑**

[`compare`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/weight_checker.py#L63-L85) 用集合运算把差异分三类：

```python
current = self._digest_model()
missing    = sorted(set(self._snapshot) - set(current))   # 基线有、现在没有
unexpected = sorted(set(current) - set(self._snapshot))    # 现在有、基线没有
changed = [name for name in sorted(set(self._snapshot) & set(current))
           if self._snapshot[name].sha256 != current[name].sha256
           or self._snapshot[name].shape  != current[name].shape
           or self._snapshot[name].dtype  != current[name].dtype]
summary["matched"] = not missing and not unexpected and not changed
```

注意 `changed` 同时比 `sha256`、`shape`、`dtype` 三项——其实只要进了 `_digest_tensor`，sha256 已经把这三者编码进去了，这里再显式比一次是**防御性写法**，确保即便摘要实现变了也能正确报「变」。

**(4) 如何被外部触发**

HTTP 侧由 `/weights_checker` 路由暴露，GET/POST 皆可，见 [sglang_omni/serve/openai_api.py:532-542](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L532-L542)；动作名来自 [`proto/admin.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/admin.py#L9-L17) 的 `ADMIN_WEIGHTS_CHECKER`。请求经 Client → Coordinator 控制平面下发给 stage，最终在 [`ModelWorker.weights_checker`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/model_worker.py#L373-L380) 里懒加载一个 `StrictWeightChecker` 实例并 `run(action)`：

```python
def weights_checker(self, action: str) -> dict[str, Any]:
    checker = getattr(self, "_strict_weight_checker", None)
    if checker is None:
        from sglang_omni.model_runner.weight_checker import StrictWeightChecker
        checker = StrictWeightChecker(self.model_runner)
        self._strict_weight_checker = checker       # 实例缓存，保证 snapshot 基线常驻
    return checker.run(action)
```

实例被缓存在 `self._strict_weight_checker` 上——这点很重要：`compare` 依赖之前 `snapshot` 存的基线，必须用同一个 checker 实例。

#### 4.3.4 代码实践

**实践目标**（本讲规定任务）：阅读 `weight_checker` 的 checksum 实现，说明它对每个 tensor 计算哪些字段参与摘要，以及全模型校验为何会阻塞推理。

**操作步骤**：

1. 打开 [sglang_omni/model_runner/weight_checker.py:150-161](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/weight_checker.py#L150-L161)，确认 `_digest_tensor` 把 `name`、`dtype`、`shape`、`_tensor_bytes(cpu)` 四段依次喂进同一个 `hashlib.sha256()`。
2. 打开 [sglang_omni/model_runner/weight_checker.py:87-106](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/weight_checker.py#L87-L106)，找到 `_digest_model` 里的 `logger.warning("...inference is blocked until this completes...")` 与 `time.time()` 计时。
3. 跑下面这段**无需 GPU、无需真模型**的最小复现（与仓库[单测](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/model_runner/test_weight_checker.py#L14-L33)同构），亲手触发 snapshot→改权重→compare：

```python
# 示例代码：用最小模型复现 snapshot/compare/checksum（纯 CPU）
import torch
from types import SimpleNamespace
from sglang_omni.model_runner.weight_checker import StrictWeightChecker

model = torch.nn.Linear(2, 2, bias=False)         # 只有 1 个张量：weight
checker = StrictWeightChecker(SimpleNamespace(model=model))

snap = checker.run("snapshot")                    # 拍基线
print("snapshot per_gpu_checksum:", snap["per_gpu_checksum"])
print("snapshot tensor_count:", snap["tensor_count"], "names:", list(snap["checksums"]))

compare_before = checker.run("compare")
print("compare before change -> matched:", compare_before["matched"])

with torch.no_grad():
    model.weight[0, 0] += 1.0                      # 改一个元素

compare_after = checker.run("compare")
print("compare after change  -> matched:", compare_after["matched"],
      "changed:", compare_after["changed"])

chk = checker.run("checksum")
print("checksum per_gpu_checksum:", chk["per_gpu_checksum"],
      "elapsed_s:", round(chk["elapsed_s"], 4))
```

**需要观察的现象**：

- `snapshot` 的 `tensor_count` 为 1，`checksums` 只含 `"weight"`。
- 改权重**之前**的 `compare`，`matched` 为 `True`，三列表全空。
- 改权重**之后**的 `compare`，`matched` 为 `False`，`changed == ["weight"]`。
- `checksum` 的 `per_gpu_checksum` 与 `snapshot` 的一致（因为 snapshot 内部也算了一次摘要）。

**预期结果**：与上述完全一致——这正是[单测 `test_strict_weight_checker_snapshot_compare_and_checksum`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/model_runner/test_weight_checker.py#L14-L33) 断言的行为。

**关于「为何阻塞推理」的书面回答**（结合源码写一句话）：`_digest_model` 对模型里每个张量依次做 `detach → contiguous → cpu → 取原始字节 → SHA256`，这是一段**同步**的 CPU 密集计算，调用线程要等全部张量算完才返回；大模型张量多、总字节大，耗时可达秒级，期间该 worker 无法服务推理请求，故源码用 `logger.warning` 明确标注「inference is blocked until this completes」并在响应回传 `elapsed_s`。

> 若环境无 `torch`/未装包，可改为「源码阅读型实践」：对照 `_digest_tensor` 与 `_digest_model` 口述上述字段与阻塞原因，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果只把张量的**字节**纳入 SHA256，不加 name/dtype/shape，会有什么隐患？

**参考答案**：两个不同的参数（如一个 weight 和一个 bias）若恰好字节相同，摘要会撞车，`compare` 就分不清「谁变了」。把 name/dtype/shape 一起纳入，等于给字节加上身份标签，只要身份或内容任一变化摘要就不同，校验才严格可靠。

**练习 2**：`per_gpu_checksum` 为什么要在聚合前对张量名**排序**？

**参考答案**：`named_parameters`/`named_buffers` 的遍历顺序可能因 PyTorch 版本、rank、模型结构微调而不同；但「名字集合 + 各自摘要」是稳定的。先按名字排序再哈希（见 [`_aggregate_checksum`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/weight_checker.py#L190-L195)），就能消除遍历顺序的影响，让不同 rank/环境下的 per-rank 校验和可比对。

**练习 3**：为什么 `compare` 必须用「上一次 snapshot 的同一个 checker 实例」，而不能每次新建？

**参考答案**：基线摘要存在实例属性 `self._snapshot` 上（见 [`snapshot`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/weight_checker.py#L52-L54)）。新建实例的 `_snapshot` 为 `None`，`compare` 会直接抛 `RuntimeError("weights_checker compare requires snapshot first")`（见 [L64-L65](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/weight_checker.py#L63-L85)）。[`ModelWorker.weights_checker`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/model_worker.py#L373-L380) 正是把实例缓存在 `self._strict_weight_checker` 上来保证这一点。

---

## 5. 综合实践

**任务**：把本讲三块知识串起来，设计一次「在线 RL 权重热更新 + 严格校验」的完整验证流程，并解释每一步背后的量化/加载/校验机制。

**背景**：假设你用 FP8 块量化的 Qwen3-Omni Talker 提供服务，训练侧产出了新一版权重，要通过 admin 控制平面热更新，并**严格证明**更新确实生效、且只改了该改的张量。

**操作步骤**（需要可运行的服务与 GPU，部分步骤**待本地验证**）：

1. **启动服务**：用 `sgl-omni serve` 拉起一个 Talker（或完整 Qwen3-Omni）服务，记录其 `--host`/`--port` 与 `--admin-api-key`（admin 端点鉴权，见 [u6-l4](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/rl_admin_control.md)）。理解此时 FP8 权重的 `weight_scale_inv` 已在 `load_weights` 里被 [`convert_fp8_weight_scale_inv`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/quantization.py#L110-L129) 取过倒数（因 talker 传了 `fp8_scale_inverted=True`）。

2. **拍基线**：

   ```bash
   curl -s -X POST http://localhost:PORT/weights_checker \
     -H "Authorization: Bearer $ADMIN_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"action":"snapshot"}' | jq '{tensor_count, per_gpu_checksum}'
   ```

   记下 `per_gpu_checksum`。此时 checker 实例被 [`ModelWorker.weights_checker`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/model_worker.py#L373-L380) 缓存，`_snapshot` 已就位。

3. **热更新权重**（从磁盘，路径指向新版 checkpoint）：

   ```bash
   curl -s -X POST http://localhost:PORT/update_weights_from_disk \
     -H "Authorization: Bearer $ADMIN_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"model_path":"/data/qwen3-omni-talker-v2","load_format":"safetensors","weight_version":"v2"}' \
     | jq .
   ```

   说明这步触发的链路（承接 u6-l4）：`update_weights_from_disk`（[model_worker.py:261-290](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/model_worker.py#L261-L290)）会暂停调度 → 调底层 runner 重新加载 → 成功后把可见的 `model_path`/`load_format`/`weight_version` 一并刷新。若新版仍是 FP8 块量化，加载时同样会走 scale 取倒数。

4. **比对**：

   ```bash
   curl -s -X POST http://localhost:PORT/weights_checker \
     -H "Authorization: Bearer $ADMIN_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"action":"compare"}' | jq '{matched, changed, missing, unexpected, tensor_count}'
   ```

**需要观察的现象与判断**：

- 若更新真的改了权重：`matched=false`，`changed` 列出被改的张量名（如 `weight`、各层 `qkv_proj` 等），`missing`/`unexpected` 为空（结构没变，只是数值变了）。
- 若 `missing`/`unexpected` 非空：说明新 checkpoint 的参数树与旧的不同（结构变了或名字重映射不对），需排查（如是否漏配了自定义 `weight_loader`）。
- 若 `matched=true` 且你确信应更新：说明更新**没生效**（可能路径写错、load_format 不对、或更新被拒绝）。
- 响应里的 `elapsed_s` 即「阻塞推理」的时长——验证了 §4.3 所述：校验期间该 worker 不服务请求。

**预期结果**：一次成功的权重更新应表现为 `matched=false`、`changed` 非空、`missing`/`unexpected` 为空，且 `changed` 张量集合与训练侧预期修改的范围一致。若你所在环境无法启动真实服务，可退化为「源码阅读型实践」：口述上述 4 步对应的源码入口（`openai_api.py` 路由 → `ModelWorker` 方法 → `StrictWeightChecker`/底层 runner），并说明每步的量化/加载/校验含义，标注「待本地验证」。

---

## 6. 本讲小结

- **分工边界**：SGLang 拥有量化的端到端实现（检测、构造量化层、post-load 钩子）；[`quantization.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/quantization.py) 只补两段 omni 专属兼容——FP8 块量化的 `weight_scale_inv` 取倒数、AutoRound 的阶段局部配置前缀归一化。
- **FP8 scale 预处理是 opt-in**：是否取倒数由模型的 `load_weights` 传 `fp8_scale_inverted=True` 决定（如 [talker.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/components/talker.py#L1771-L1773)），框架不自动猜；`convert_fp8_weight_scale_inv` 还做非空/有限/非零三道健康检查。
- **自定义 weight_loader 解决「名字对不上」**：当 checkpoint 命名与运行时模块树不一致时，写一个 `map(name) -> str | None` 的重映射器（模板见 [`DiscreteWeightMapper`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/higgs_tts/weight_loader.py#L15-L51)），在 `load_weights` 里分拣给骨干流与自有模块流；它与 scale 预处理正交（一个改名、一个改值）。
- **SHA256 摘要的四字段**：[`_digest_tensor`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/weight_checker.py#L150-L161) 把 **name + dtype + shape + 原始字节** 一起喂进 SHA256，且兼容 bfloat16/float8 等低精度 dtype 的取字节回退。
- **为何阻塞推理**：`_digest_model` 是同步的 CPU 密集遍历（每张量 `detach→contiguous→cpu→取字节→SHA256`），大模型耗时可达秒级，期间 worker 无法服务请求，故源码用 `logger.warning` 告警并回传 `elapsed_s`。
- **四动作语义**：`snapshot`/`reset_tensors` 拍基线、`compare` 报 missing/unexpected/changed、`checksum` 一次性出摘要与按名排序聚合的 `per_gpu_checksum`；`compare` 必须复用同一 checker 实例（基线常驻其 `_snapshot`）。

---

## 7. 下一步学习建议

- **深入 RL 热更新全流程**：本讲的 `weight_checker` 是「校验」半场，「更新」半场（暂停→abort/retract→`update_weights_from_disk/distributed`→刷缓存→恢复、TP rank 聚合、router 广播）请接着学 u6-l4《RL 权重热更新与 Admin 控制》，并对照 [`docs/developer_reference/rl_admin_control.md`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/rl_admin_control.md)。
- **理解量化层在 forward 里如何用 scale**：本讲只到「scale 被正确喂进运行时」，真正的 FP8/INT4 反量化发生在 SGLang 的量化层 forward 中，建议去上游 SGLang 的 `sglang.srt.layers.quantization` 读 `FP8` 等实现，体会「SGLang 拥有执行」的另一面。
- **上手一个自定义 weight_loader**：若你要接入一个命名非标准的 checkpoint，参照 [`higgs_tts/model.py` 的 `load_weights`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/higgs_tts/model.py#L542-L575) 与 u7-l5《综合实战：新增一个模型家族》，写一个最小 `map` 并用单测验证名字分拣正确。
- **跑通校验单测**：执行 [`tests/unit_test/model_runner/test_weight_checker.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/model_runner/test_weight_checker.py) 与 [`tests/unit_test/quantization/`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/quantization) 下的 `test_fp8.py`/`test_autoround.py`/`test_weight_preprocess.py`，用断言固化对分支选择与摘要字段的理解。
