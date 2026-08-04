# 注意力后端：role 感知选择

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 diffusion 注意力（attention）的「后端（backend）」是什么，以及为什么同一个模型里不同的注意力站点（self / cross）会想要用不同的后端。
- 跟踪一个 `Attention` 层在构造期如何凭 `role` 字符串解析出最终使用的后端实现类。
- 复述 `get_attn_backend_for_role` 的四级解析优先级：per_role 精确匹配 → role_category 回退 → global default → 平台默认。
- 理解 `AttentionBackend` / `AttentionImpl` 这两个抽象基类的契约，以及 `AttentionSpec` 如何把后端专有参数序列化成 `backend_kwargs`。
- 读懂 `FLASH_ATTN` / `TORCH_SDPA` / `SAGE_ATTN` / `TRTLLM_ATTN` / `CUDNN_ATTN` 等后端的名字从何而来、各自在什么硬件上可用。
- 手写一份「self 用 FLASH_ATTN、cross 用 TORCH_SDPA」的 `diffusion_attention_config`，并验证它确实被按你期望的顺序解析命中。

本讲是 U7（Diffusion 加速）的第一篇，只讲「后端选择」这一件事；注意力层的序列并行（Ring / Ulysses）叠加在后端之上，留给下一讲 u7-l2。

## 2. 前置知识

本讲假设你已经读过 u5-l3（Diffusion Worker 与模型加载），知道 `pipeline.forward` 内部会反复调用注意力层。下面补几个本讲要用到的基础概念。

**注意力（attention）做什么。** 给定查询 \(Q\)、键 \(K\)、值 \(V\)，注意力计算

\[
\mathrm{Attention}(Q,K,V) = \mathrm{softmax}\!\left(\frac{QK^{\top}}{\sqrt{d_k}}\right)V
\]

是扩散 Transformer（DiT）里最核心、也最耗时的算子。数学上它很简单，但工程上「具体用哪个 GPU kernel 来算它」差别巨大——不同 kernel 在显存占用、是否支持注意力掩码（mask）、是否支持 FP8、是否支持变长序列上差异很大。

**后端（backend）。** 在 vLLM-Omni 里，「后端」就是「一个具体的注意力 kernel 实现」，例如 `FLASH_ATTN`（FlashAttention）、`TORCH_SDPA`（PyTorch 内置 `scaled_dot_product_attention`）、`TRTLLM_ATTN`（FlashInfer 的 trtllm-gen FMHA）。后端是一对类：`AttentionBackend`（描述「我是谁、支持哪些 head_size」）+ `AttentionImpl`（实际算前向）。

**self-attention 与 cross-attention。** DiT 里常见的两类注意力站点：

- **self（自注意力）**：\(Q/K/V\) 都来自同一个隐藏态，通常作用在很长的 latent 序列上（比如视频的时空 token），是性能瓶颈，需要最强的 kernel。
- **cross（交叉注意力）**：\(Q\) 来自 latent，但 \(K/V\) 来自文本编码器输出。文本序列通常短得多，用重型 kernel 反而没收益（甚至有质量代价）。

正因为 self 和 cross 的「形状与诉求」不同，vLLM-Omni 允许你**按角色（role）给它们配不同的后端**，这就是本讲主题「role 感知选择」。

**平台（platform）。** 后端能不能用，取决于硬件与已安装的库（`flash-attn`、`flashinfer`、`sageattention` 等）。vLLM-Omni 把「在这个硬件上应该默认用哪个后端」这件事委托给当前平台对象 `current_omni_platform`（见 u8-l2 平台抽象）。本讲里平台只扮演「最后兜底的默认选择」一个角色。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `vllm_omni/diffusion/attention/layer.py` | `Attention(nn.Module)` 层：构造期凭 `role` 解析后端并实例化，前向期调度到后端实现。 |
| `vllm_omni/diffusion/attention/selector.py` | `get_attn_backend_for_role`：四级优先级的解析入口，以及按 `(backend_name, head_size)` 缓存解析结果。 |
| `vllm_omni/diffusion/attention/backends/abstract.py` | `AttentionBackend` / `AttentionImpl` / `AttentionMetadata` 三个抽象基类，定义后端契约。 |
| `vllm_omni/diffusion/data.py` | `AttentionConfig` / `AttentionSpec` 数据类：用户配置的载体，含 `resolve_with_source` 三级查表与 `backend_kwargs` 序列化。 |
| `vllm_omni/diffusion/attention/backends/registry.py` | `DiffusionAttentionBackendEnum`：把后端名字（`FLASH_ATTN` 等）映射到实现类的全限定路径。 |
| `vllm_omni/platforms/cuda/platform.py` | `CudaOmniPlatform.get_diffusion_attn_backend_cls`：CUDA 平台上「校验用户选择 + 选平台默认」的具体实现。 |
| `docs/design/module/dit_module.md` | 设计文档 5.1 节，给出 role 感知选择的总览与四级优先级。 |
| `docs/user_guide/diffusion/attention_backends.md` | 面向用户的配置手册，列出全部后端、平台默认与 CLI 写法。 |

模型侧的「role 声明」样例见 `vllm_omni/diffusion/models/`，本讲会引用 Wan2.2、SDXL、LTX-2 三处真实声明。

## 4. 核心概念与源码讲解

### 4.1 Attention 层的 role 声明与后端解析

#### 4.1.1 概念说明

vLLM-Omni 的 `Attention` 层不直接绑定某个具体 kernel。模型作者在 `__init__` 里创建 `Attention(...)` 时，用一个自由字符串 `role`（如 `"self"`、`"cross"`、`"ltx2.connector"`）给这个注意力站点「起名字」。这个字符串本身不参与任何计算，它的唯一作用是：让用户在配置里能精确地把某个后端「钉」到这一类站点上，而不必改模型代码。

关键直觉是：**后端选择发生在构造期，不是前向期**。每个 `Attention` 层在 `__init__` 里就把要用哪个后端实现类定死了，并实例化成 `self.attention`；前向时只是调用它。所以「换后端」=重新构造模型，而不是运行时切换。这把昂贵的 kernel 探测与平台校验成本分摊到一次性初始化里。

为什么不同站点要不同后端？举个 Wan2.2 的真实理由：它的 cross-attention 作用在很短的文本编码器序列上，做 FP8 量化既无性能收益又会掉点，所以模型作者直接在声明处用 `disable_kv_quant=True` 把这一层排除在 KV 量化之外；同理，用户也可能想让 self 用最快的 `FLASH_ATTN`、cross 退到最稳的 `TORCH_SDPA`。

#### 4.1.2 核心流程

`Attention` 构造期解析后端的过程：

1. 读取进程级全局配置 `get_current_diffusion_config_or_none()`，取出其中的 `diffusion_attention_config`（一个 `AttentionConfig`）。
2. 从模型元数据判断当前 pipeline 是否「无掩码（mask-free）」，得到 `allow_trtllm_default` 开关——它只影响第 4 级平台默认是否敢于自动选 `TRTLLM_ATTN`。
3. 调 `get_attn_backend_for_role(role, head_size, attention_config, role_category, allow_trtllm_default)`，拿到 `(后端类, spec)`。
4. 若 `spec` 非空，调用 `spec.backend_kwargs()` 把后端专有参数（如 skip_softmax）序列化成字典。
5. 用 `后端类.get_impl_cls()` 得到 `AttentionImpl` 子类，连同 `backend_kwargs` 一起实例化为 `self.attention`。
6. 额外无条件实例化一个 `SDPABackend` 实现作为 `self.sdpa_fallback`，供 `float32` 输入时降级（见 4.3）。

#### 4.1.3 源码精读

先看构造函数签名，注意 `role`、`role_category` 两个参数：

[layer.py:41-64](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/layer.py#L41-L64) —— `Attention.__init__` 接收 `role="self"`、`role_category=None`、`qkv_layout` 等参数。`role` 默认 `"self"`，`role_category` 默认 `None`（表示不做类别回退）。

接着是解析后端的核心几行：

[layer.py:77-98](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/layer.py#L77-L98) —— 从全局 config 取 `attention_config`；用 `get_diffusion_model_metadata(model_class_name).attention_mask_free` 算出 `allow_trtllm_default`；调 `get_attn_backend_for_role(...)` 得到 `(attn_backend_cls, spec)`；若 `spec` 非空则 `backend_kwargs = spec.backend_kwargs()`，并记录 `self.backend_pref = spec.backend`。

拿到后端类后实例化：

[layer.py:99-119](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/layer.py#L99-L119) —— `self.attn_impl_cls = attn_backend_cls.get_impl_cls()`，再用它实例化 `self.attention`（传入 `backend_kwargs`），并额外建一个 `self.sdpa_fallback`。

再看模型侧是怎么声明 role 的。三处真实例子：

- Wan2.2 的 cross-attention：[wan2_2_transformer.py:574-588](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/wan2_2/wan2_2_transformer.py#L574-L588) —— `role="cross"`，并配 `skip_sequence_parallel=True`（K/V 跨 rank 复制，不参与序列并行）、`disable_kv_quant=True`（短文本序列，量化无收益）。
- LTX-2 的 connector 注意力：[ltx2_components.py:238-251](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/ltx2/ltx2_components.py#L238-L251) —— `role="ltx2.connector"`，并配 `role_category="self"`。这就是「模型专有 role + 通用类别回退」的典型用法：用户既可以用 `per_role["ltx2.connector"]` 精确命中，也可以什么都不配让它回退到 `per_role["self"]`。
- SDXL UNet 的 cross-attention：[sdxl_unet.py:180-185](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/sdxl/sdxl_unet.py#L180-L185) —— `role="cross"`。

#### 4.1.4 代码实践

实践目标：理解「role 是自由字符串、在构造期被消费」。

操作步骤：

1. 打开 `vllm_omni/diffusion/models/wan2_2/wan2_2_transformer.py`，定位上面那处 `role="cross"`。
2. 在同文件搜索 `role="self"`（self-attention 块），对比两处声明。
3. 回答：如果把 cross 块的 `role` 改成一个任意字符串如 `"my_cross"`，模型能正常前向吗？为什么？

需要观察的现象：`role` 字符串不出现在 `forward` 的任何计算里，它只在 `__init__` 里被传给 `get_attn_backend_for_role`。

预期结果：改 role 字符串不影响数值结果（只要后端仍能解析）；它只会改变「这一层在配置里被哪个 key 命中」。若改成无任何配置覆盖且平台默认不变，行为完全一致。

> 说明：本步骤是源码阅读型实践，不要求运行 GPU。

#### 4.1.5 小练习与答案

**练习 1**：LTX-2 用了 `role="ltx2.connector"` 配 `role_category="self"`。如果用户既没配 `per_role["ltx2.connector"]` 也没配 `per_role["self"]`，这一层会用什么后端？

答案：走 `AttentionConfig.resolve_with_source` 的三级都落空后，进入第 4 级「平台默认」，由 `current_omni_platform.get_diffusion_attn_backend_cls(None, head_size)` 选（CUDA 上典型是 `FLASH_ATTN` 或 Blackwell 上的 `CUDNN_ATTN`）。

**练习 2**：为什么 `allow_trtllm_default` 要从「模型是否 mask-free」推出，而不是写死 True？

答案：`TRTLLM_ATTN` 无法接受注意力掩码（见 4.4 与用户手册）。若模型前向会带 mask，自动选它会报错；所以只有被元数据标注为 mask-free 的 pipeline（如 Wan 家族）才允许平台默认自动选它。

### 4.2 四级优先级解析：get_attn_backend_for_role 与 AttentionConfig

#### 4.2.1 概念说明

后端选择的「大脑」是 `get_attn_backend_for_role`，它实现了一条固定的四级优先级链。配置的载体是 `AttentionConfig`，它只有两块内容：一个全局 `default`（`AttentionSpec` 或 `None`），外加一张 `per_role` 映射（role 字符串 → `AttentionSpec`）。

四级优先级（设计文档 dit_module.md 5.1 节与源码 docstring 完全一致）：

1. `per_role[role]` —— 与该层 `role` 字符串精确匹配。
2. `per_role[role_category]` —— 精确匹配落空时，用该层声明的 `role_category` 做类别回退。
3. `default` —— 全局默认。
4. **平台默认** —— 前三级都为空时，交给当前平台按硬件选。

注意第 1～3 级都发生在 `AttentionConfig.resolve_with_source` 内部，返回 `(spec, source)`；第 4 级发生在 `get_attn_backend_for_role` 里——当 `resolve_with_source` 返回 `spec=None` 时，转而调 `_cached_get_backend_cls(None, head_size, allow_trtllm_default)`。换句话说，**配置层负责 1～3 级，selector 层负责衔接第 4 级**。

#### 4.2.2 核心流程

`get_attn_backend_for_role` 的决策伪代码：

```
def get_attn_backend_for_role(role, head_size, attention_config, role_category, allow_trtllm_default):
    if attention_config is not None:
        spec, source = attention_config.resolve_with_source(role, role_category)
        if spec is not None:                      # 命中第 1/2/3 级
            backend_cls = cached_load(spec.backend, head_size)
            log(backend_cls.name, source)
            return backend_cls, spec
    # 第 4 级：平台默认
    backend_cls = cached_load(selected=None, head_size, allow_trtllm_default)
    log(backend_cls.name, "platform default")
    return backend_cls, None      # spec=None 表示「用的是平台默认，没有用户 spec」
```

而 `AttentionConfig.resolve_with_source` 内部三级：

```
def resolve_with_source(self, role="self", role_category=None):
    if role in self.per_role:          # 第 1 级
        return self.per_role[role], f"per_role[{role}]"
    if role_category and role_category in self.per_role:   # 第 2 级
        return self.per_role[role_category], f"per_role[{role_category}] (category fallback)"
    if self.default is not None:       # 第 3 级
        return self.default, "default"
    return None, None                  # 交给第 4 级平台默认
```

一个重要细节：`per_role` 里的 role 字符串在构造期被「点号拍平」。比如用户写 `per_role={"mymodel": {"audio_to_video": {"backend": "SAGE_ATTN"}}}`，会被 `_flatten_per_role_entry` 规范化成 `per_role["mymodel.audio_to_video"]`，于是模型里声明 `role="mymodel.audio_to_video"` 就能命中。这让用户可以用嵌套字典表达分层命名空间。

#### 4.2.3 源码精读

`get_attn_backend_for_role` 的完整定义与 docstring（注意 docstring 里写明的四级优先级）：

[selector.py:97-153](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/selector.py#L97-L153) —— 若 `attention_config` 非空则先 `resolve_with_source`；命中时经 `_cached_get_backend_cls(spec.backend, head_size)` 加载并返回 `(cls, spec)`；否则走 `_cached_get_backend_cls(None, head_size, allow_trtllm_default)` 返回 `(cls, None)`。

`AttentionConfig.resolve_with_source` 的三级查表：

[data.py:1600-1615](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/data.py#L1600-L1615) —— 先 `per_role.get(role)`，再 `per_role.get(role_category)`，再 `self.default`，逐级返回带 `source` 字符串的元组。

`per_role` 的点号拍平逻辑：

[data.py:1571-1598](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/data.py#L1571-L1598) —— `_flatten_per_role_entry` 递归把嵌套字典的键路径用 `"."` 拼成一个 role 字符串；若某层既含 `backend` 等 spec 键又含子 role 键则报错（不能混用）。

「backend="auto" 表示放弃配置」的约定：

[data.py:1552-1557](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/data.py#L1552-L1557) —— `_coerce_spec_or_none` 在 backend 小写为 `"auto"` 时返回 `None`，等价于「不指定，留给下一级」。这是让 `--diffusion-attention-backend auto` 显式表达「用平台默认」的出口。

设计文档对该机制的权威描述（与源码一致）：

[dit_module.md:514-545](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/module/dit_module.md#L514-L545) —— 5.1 节「Backend Selection Mechanism」给出四级优先级与「`AttentionSpec.backend_kwargs()` 是后端专有参数的唯一序列化出口」的说明。

#### 4.2.4 代码实践

实践目标：**在没有 GPU 的纯 Python 环境里**，亲手构造一份 `AttentionConfig`，验证四级优先级中前三级的确切命中路径。

操作步骤（可直接 `python` 运行）：

```python
# 示例代码：不依赖 GPU，只验证配置解析
from vllm_omni.diffusion.data import AttentionConfig, AttentionSpec

cfg = AttentionConfig(
    default=AttentionSpec(backend="FLASH_ATTN"),     # 第 3 级
    per_role={
        "cross": AttentionSpec(backend="TORCH_SDPA"),  # 第 1/2 级
    },
)

# 模拟一个 self-attention 层：role="self", role_category=None
spec, source = cfg.resolve_with_source(role="self", role_category=None)
print("self  ->", spec.backend, "|", source)
# 预期：self  -> FLASH_ATTN | attention_config.default

# 模拟一个 cross-attention 层：role="cross"
spec, source = cfg.resolve_with_source(role="cross", role_category=None)
print("cross ->", spec.backend, "|", source)
# 预期：cross -> TORCH_SDPA | attention_config.per_role['cross']

# 模拟一个模型专有站点：role="mymodel.audio_to_video", role_category="cross"
spec, source = cfg.resolve_with_source(role="mymodel.audio_to_video", role_category="cross")
print("a2v   ->", spec.backend, "|", source)
# 预期：a2v   -> TORCH_SDPA | attention_config.per_role['cross'] (role_category fallback)
```

需要观察的现象：第三个调用没有精确匹配 `mymodel.audio_to_video`，于是命中第 2 级 `role_category="cross"`，`source` 字符串里会带 `(role_category fallback)`。

预期结果：三行输出分别落在 default、per_role 精确、per_role 类别回退三条路径上，与上面注释一致。若三行都正确，说明你已经掌握了前三级。

> 说明：本步骤不调用任何后端实现类（不触发平台校验），所以无需 GPU、无需安装 `flash-attn`。第 4 级平台默认因为要读 GPU compute capability，留到 4.4 节与综合实践中讨论。

#### 4.2.5 小练习与答案

**练习 1**：把上面示例的 `default` 改成 `AttentionSpec(backend="auto")`，`per_role` 保持 `{"cross": TORCH_SDPA}`。问 `role="self"` 会解析出什么？

答案：`_coerce_spec_or_none` 把 `"auto"` 规整成 `None`，于是 `AttentionConfig.default` 实际为 `None`。`resolve_with_source("self")` 第 1/2/3 级都落空，返回 `(None, None)`，进入第 4 级平台默认。

**练习 2**：用户在 CLI 写 `--diffusion-attention-backend FLASH_ATTN`，同时又写 `--diffusion-attention-config.default.backend TORCH_SDPA`，会发生什么？

答案：报错。`--diffusion-attention-backend` 是 `default.backend` 的简写，二者互斥；`parse_attention_config` 在 [data.py:1641-1648](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/data.py#L1641-L1648) 检测到 `default` 已存在时直接 raise。

### 4.3 AttentionBackend / AttentionImpl 接口与 AttentionSpec 序列化

#### 4.3.1 概念说明

每个后端都是一对类：

- **`AttentionBackend`**（静态描述）：回答「我叫什么名字（`get_name`）」「我的实现类是谁（`get_impl_cls`）」「我支持哪些 head_size（`get_supported_head_sizes`）」「我能不能吃注意力掩码（`supports_attention_mask`）」「我能不能处理分段（piecewise）注意力（`supports_piecewise_spans`）」。
- **`AttentionImpl`**（实际计算）：持有构造期参数（`num_heads`、`softmax_scale`、`causal`、`backend_kwargs`…），并在 `forward(query, key, value, attn_metadata)` 里真正算注意力。

`AttentionImpl.forward` 不直接写平台分支，而是先按 `current_omni_platform` 分派到 `forward_cuda` / `forward_hip` / `forward_npu` / `forward_xpu` / `forward_musa` 之一。这让同一个后端实现可以跨硬件复用（如 SDPA 在 CUDA/HIP/XPU 上都可用）。

**`AttentionSpec` 是配置里的「后端 + 后端专有参数」容器。** 它不止有 `backend` 名字，还可能带 `skip_softmax`（仅 `TRTLLM_ATTN`）和 `quant`（仅 `TRTLLM_ATTN` / `FLASHINFER_ATTN`）。`AttentionSpec.backend_kwargs()` 把这些结构化字段序列化成一个普通字典，交给 `AttentionImpl.__init__` 的 `backend_kwargs` 形参。这样后端实现只需要在 `__init__` 里读自己关心的键，配置层不必知道每个后端的具体字段。

`AttentionSpec` 在 `__post_init__` 里做了一次重要的跨字段校验：`skip_softmax` 只允许配 `TRTLLM_ATTN`，`quant` 只允许配 `TRTLLM_ATTN` 或 `FLASHINFER_ATTN`，否则构造期就 raise——把「配错后端」的错提早到配置解析阶段，而不是延迟到首次前向。

#### 4.3.2 核心流程

后端实例化与调用的流程：

1. `attn_backend_cls.get_impl_cls()` → 得到 `AttentionImpl` 子类。
2. 用 `(num_heads, head_size, softmax_scale, causal, num_kv_heads, qkv_layout, prefix, backend_kwargs)` 实例化它。
3. 前向时，`Attention.forward` → `_forward_impl` → `_run_local_attention` → `self.attention.forward(q,k,v,attn_metadata)`。
4. `AttentionImpl.forward` 按 `current_omni_platform` 分派到 `forward_cuda` 等具体方法。
5. `Attention` 还内建一个 `sdpa_fallback`：若输入 `dtype == torch.float32`，只有 SDPA 能算，于是降级到它并打一条 `warning_once`。

分段注意力（piecewise）的安全网：当 `attn_metadata.full_attn_spans` 非 None（混合因果/全注意力）且没有 4D `attn_mask` 时，`Attention` 会要求后端 `supports_piecewise_spans=True`，否则 raise，提示改用 Flash 家族后端或提供 4D mask。

#### 4.3.3 源码精读

`AttentionBackend` 抽象基类（注意两个类级属性 `accept_output_buffer` / `supports_piecewise_spans` 与一组 `@staticmethod @abstractmethod`）：

[abstract.py:13-52](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/backends/abstract.py#L13-L52) —— 定义 `get_name` / `get_impl_cls` / `get_metadata_cls` / `get_builder_cls` / `get_supported_head_sizes` 抽象方法，以及 `supports_head_size` 默认实现（支持列表为空表示「全部支持」）。

`AttentionImpl` 与平台分派：

[abstract.py:96-145](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/backends/abstract.py#L96-L145) —— `forward` 先按 `current_omni_platform.is_rocm()/is_cuda()/is_npu()/...` 分派；`forward_hip` / `forward_musa` 默认复用 `forward_cuda`。`supports_kv_cache_dtype` 用 `_supported_kv_cache_dtypes`（平台 → dtype 集合）判断本实现是否支持某 KV 量化 dtype。

`AttentionSpec.backend_kwargs()` 的序列化：

[data.py:1491-1514](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/data.py#L1491-L1514) —— 把 `skip_softmax` 与 `quant` 翻译成扁平字典键（如 `skip_softmax_threshold`、`target_sparsity`、`quant` 子字典），空则返回 `None`。

`AttentionSpec.__post_init__` 的跨字段校验：

[data.py:1467-1481](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/data.py#L1467-L1481) —— `skip_softmax` 配非 `TRTLLM_ATTN`、`quant` 配非 `TRTLLM_ATTN`/`FLASHINFER_ATTN` 时直接 raise。

`Attention` 层前向里的 float32 降级与分段校验：

[layer.py:292-303](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/layer.py#L292-L303) —— float32 输入走 `self.sdpa_fallback`；

[layer.py:305-317](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/layer.py#L305-L317) —— `_assert_piecewise_compatible` 在分段场景要求 `supports_piecewise_spans`。

两个真实后端，对照「名字从哪来」：

- SDPA：[sdpa.py:55-72](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/backends/sdpa.py#L55-L72) —— `get_name()` 返回 `"SDPA"`，`get_supported_head_sizes()` 返回 `[x for x in range(1024)]`（事实上的「全支持」），`accept_output_buffer=True`、`supports_attention_mask=True`；其 `SDPAImpl.__init__` 显式忽略 `backend_kwargs`（见 [sdpa.py:89-90](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/backends/sdpa.py#L89-L90)）。
- FlashAttention：[flash_attn.py:22-40](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/backends/flash_attn.py#L22-L40) —— `get_name()` 返回 `"FLASH_ATTN"`，`get_supported_head_sizes()` 返回 `[64, 96, 128, 192, 256]`，`supports_piecewise_spans=True`。

> 术语提醒：`get_name()` 返回的字符串（如 `"FLASH_ATTN"`）是运行时日志与配置里看到的名字；而注册表 `DiffusionAttentionBackendEnum` 的成员名（`FLASH_ATTN`）是配置层用来寻址的 key。二者刻意保持一致，但分属两个层。

#### 4.3.4 代码实践

实践目标：验证「后端专有参数校验」与「序列化」。

操作步骤（纯 Python，无需 GPU）：

```python
# 示例代码
from vllm_omni.diffusion.data import AttentionSpec, SkipSoftmaxSpec

# 1) 正确：skip_softmax 只能配 TRTLLM_ATTN
ok = AttentionSpec(backend="TRTLLM_ATTN", skip_softmax=SkipSoftmaxSpec(target_sparsity=0.65))
print("backend_kwargs:", ok.backend_kwargs())

# 2) 错误：把 skip_softmax 配给 FLASH_ATTN —— 期望构造期就 raise
try:
    bad = AttentionSpec(backend="FLASH_ATTN", skip_softmax=SkipSoftmaxSpec(threshold=0.5))
except ValueError as e:
    print("caught:", str(e)[:60], "...")
```

需要观察的现象：第 1 步的 `backend_kwargs()` 输出形如 `{'target_sparsity': 0.65}`（`disabled_until_timestep` 为 0 时被省略）；第 2 步在 `__init__` 阶段就抛 `ValueError`，不会等到前向。

预期结果：第 1 步打印出序列化字典，第 2 步捕获到 `skip_softmax is only supported by the TRTLLM_ATTN backend`。

#### 4.3.5 小练习与答案

**练习 1**：`SDPABackend.get_supported_head_sizes()` 返回一个 0..1023 的大列表，`FlashAttentionBackend` 只返回 `[64,96,128,192,256]`。这对 `supports_head_size` 的含义是什么？

答案：`supports_head_size` 在支持列表为空时返回「全部支持」；SDPA 返回非空大列表近似表示「几乎所有 head_size 都行」，FA 则只在那 5 个值上返回 True。若模型用了 FA 不支持的 head_size，配置层选 FA 会在平台校验/实例化时被发现。

**练习 2**：为什么 `AttentionImpl.forward` 要先分派到 `forward_cuda`/`forward_npu` 等，而不是直接写一份？

答案：同一后端可能跨硬件复用（如 SDPA 在 CUDA/NPU/XPU 上行为略有不同，SDPA 在 NPU 上需要 `[B,1,Q,K]` 的 full_qk 掩码布局，CUDA 上用 `[B,1,1,K]` 广播）。分派让一份实现按平台走不同分支，见 [sdpa.py:164-171](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/backends/sdpa.py#L164-L171)。

### 4.4 平台映射与后端可用性：DiffusionAttentionBackendEnum 与平台默认

#### 4.4.1 概念说明

selector 只知道「后端名字字符串」（来自 `AttentionSpec.backend`），它需要把这个名字变成「实现类的全限定路径」。这件事分两步：

1. **名字 → 路径**：`DiffusionAttentionBackendEnum` 是一张「后端名 → 默认类路径」的枚举表（如 `FLASH_ATTN → "...flash_attn.FlashAttentionBackend"`）。它还支持运行时用 `register_diffusion_backend` 覆盖某个后端的实现，供平台/插件替换内核。
2. **路径 → 类**：`_load_backend_cls` 用 `importlib.import_module` 懒加载，避免在 import 期拖入 `flash-attn`/`flashinfer` 等重依赖。

而「这个后端在当前硬件上能不能用 / 默认该选谁」是**平台**的职责。selector 把这两件事都委托给 `current_omni_platform.get_diffusion_attn_backend_cls(selected_backend, head_size, allow_trtllm_default)`：传具体名字就校验并返回该名字对应的路径；传 `None` 就让平台按硬件挑默认。解析结果按 `(backend_name, head_size, allow_trtllm_default)` 用 `@cache` 缓存，保证平台校验（读 compute capability、探包是否存在）每个组合只跑一次。

CUDA 平台的默认选择体现了「逐级降级」思想（见用户手册 Platform Defaults 一节）：在 Blackwell 数据中心卡上且 mask-free + head_dim=128 + 有 flashinfer 时优先 `TRTLLM_ATTN`；否则 Blackwell + cuDNN≥9.5 选 `CUDNN_ATTN`；再否则 `FLASHINFER_ATTN` → `FLASH_ATTN` → `TORCH_SDPA`。Hopper/Ada/Ampere 上则是 `FLASH_ATTN` 优先、`TORCH_SDPA` 兜底。

#### 4.4.2 核心流程

从 selector 到平台的两级委托：

```
get_attn_backend_for_role(...)
  └─ _cached_get_backend_cls(backend_name, head_size, allow_trtllm_default)   # @cache
       └─ current_omni_platform.get_diffusion_attn_backend_cls(selected_backend, head_size, ...)
            ├─ selected_backend 非 None：校验（compute capability / 包是否安装）→ enum[name].get_path()
            └─ selected_backend == None：按硬件逐级降级选默认 → enum[choice].get_path()
       └─ _load_backend_cls(path)   # importlib 懒加载
```

用户三选一的配置入口（优先级从高到低，见用户手册 Configuration 一节）：

1. `--diffusion-attention-config`（结构化，含 per_role）——最高优先级。
2. `--diffusion-attention-backend` 或环境变量 `DIFFUSION_ATTENTION_BACKEND`——`default.backend` 的全局简写。
3. 平台默认——什么都不配时。

`build_attention_config` 是这三入口的「唯一归一化点」，在 `OmniDiffusionConfig.__post_init__` 里恰好被调用一次：它把 dict/`AttentionConfig` 都规整成 `AttentionConfig`，并在 `default` 仍为空时读 `DIFFUSION_ATTENTION_BACKEND` 环境变量补一个 `default`。

#### 4.4.3 源码精读

selector 里把名字委托给平台、并懒加载 + 缓存的函数：

[selector.py:50-69](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/selector.py#L50-L69) —— `_cached_get_backend_cls` 带 `@cache`，调 `current_omni_platform.get_diffusion_attn_backend_cls(...)` 拿路径，再 `_load_backend_cls` 导入。注释说明缓存是为了避免重复打平台校验日志。

注册表枚举与路径解析（含运行时覆盖机制）：

[registry.py:38-67](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/backends/registry.py#L38-L67) —— 列出全部 9 个后端名 → 默认类路径；

[registry.py:69-99](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/backends/registry.py#L69-L99) —— `get_path()` 优先用 `_DIFFUSION_ATTN_OVERRIDES` 覆盖表，否则用枚举默认值；`get_class()` 用 `resolve_obj_by_qualname` 解析。

平台的抽象方法签名：

[interface.py:108-128](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/interface.py#L108-L128) —— `OmniPlatform.get_diffusion_attn_backend_cls(selected_backend, head_size, allow_trtllm_default)` 抽象方法，docstring 说明返回「全限定类路径」。

CUDA 平台「用户显式选择时的校验」分支：

[platform.py:135-202](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/cuda/platform.py#L135-L202) —— `selected_backend is not None` 时：`FLASH_ATTN` 在算力<8.0 或包缺失时回退到 `TORCH_SDPA`；`SAGE_ATTN_3` 要求 Blackwell 且装了 `sageattn3`，否则回退 SDPA；`TRTLLM_ATTN` 要求 sm_100/sm_103 且有 flashinfer，否则 **raise**（不静默降级）。

CUDA 平台「平台默认」的逐级降级：

[platform.py:204-247](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/cuda/platform.py#L204-L247) —— `trtllm_attn_default_ok`（数据中心 Blackwell + head_size==128 + 有 trtllm-gen 内核）→ `CUDNN_ATTN`（Blackwell + cuDNN≥9.5）→ `FLASHINFER_ATTN` → `FLASH_ATTN` → `TORCH_SDPA`。

配置三入口的归一化点：

[data.py:1652-1680](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/data.py#L1652-L1680) —— `build_attention_config`：先 `parse_attention_config` 规整类型，若 `default` 已存在直接返回，否则读 `DIFFUSION_ATTENTION_BACKEND` 环境变量补 `default`（`auto` 视为不指定）。

用户手册对「后端名、平台默认、CLI 写法」的权威清单：

[attention_backends.md:13-34](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/user_guide/diffusion/attention_backends.md#L13-L34) —— `docs/user_guide/diffusion/attention_backends.md` 的 Backend Options 表与 Configuration 三入口（结构化 config / `--diffusion-attention-backend` / 平台默认）。

[attention_backends.md:100-127](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/user_guide/diffusion/attention_backends.md#L100-L127) —— Platform Defaults 段，给出 Blackwell 与 Hopper/Ada/Ampere 的逐级降级顺序。

#### 4.4.4 代码实践

实践目标：在源码层面回答「平台默认会选谁」，并对照日志确认。

操作步骤：

1. 假设你的 CUDA 卡是 Hopper（sm_90），且装了 `flash-attn`。打开 [platform.py:242-247](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/cuda/platform.py#L242-L247)，确认 `is_blackwell=False`，于是跳过 TRTLLM/CUDNN/FLASHINFER 三条 Blackwell 专属分支，落到 `flash_attn_supported` → 返回 `FLASH_ATTN`。
2. 启动任一 diffusion 服务（如 u1-l5 的 `vllm serve ... --omni`），在启动日志里搜下面三种行之一：
   - `Using diffusion attention backend 'XXX'`（用户显式选）
   - `Defaulting to diffusion attention backend XXX ...`（平台自动选）
   - `Defaulting to diffusion attention backend SDPA`（什么都不可用）
3. 改用环境变量 `DIFFUSION_ATTENTION_BACKEND=TORCH_SDPA` 重启，观察日志变成 `Using diffusion attention backend 'SDPA'`。

需要观察的现象：什么都不配时日志是 `Defaulting to ...`；显式配后变成 `Using ...`。这与 selector 里 `_log_backend_resolution` 的 `source` 参数（`"platform default"` vs `"attention_config"`）一致。

预期结果：日志行与你在 platform.py 里读到的降级链一致。若实际硬件/包不匹配（例如以为有 `flash-attn` 其实没装），日志会落到 `TORCH_SDPA` 并伴随 `warning`。

> 待本地验证：日志的精确措辞取决于实际硬件与已安装包，请在目标机器上确认。

#### 4.4.5 小练习与答案

**练习 1**：为什么显式选 `TRTLLM_ATTN` 在不满足条件时是 **raise**，而显式选 `FLASH_ATTN` 不满足条件时是**回退到 SDPA**？

答案：见 [platform.py:188-202](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/cuda/platform.py#L188-L202)。`TRTLLM_ATTN` 若静默降级，用户会以为开了 Skip-Softmax 加速其实没开，难以察觉；所以宁可在启动就报错。`FLASH_ATTN` 回退到 SDPA 是常见且无害的降级，给 warning 即可。

**练习 2**：`_cached_get_backend_cls` 的缓存 key 是 `(backend_name, head_size, allow_trtllm_default)`，没有包含 `role`。为什么不需要 role？

答案：后端类的选择只依赖「要哪个后端名 + 它支持这个 head_size 吗 + 允不允许 trtllm 默认」，与 role 无关。role 只决定**查到哪个 spec**（在 `resolve_with_source` 里完成），一旦确定了 `spec.backend`，加载哪个类就与 role 无关了。

## 5. 综合实践

把本讲四个模块串起来，完成一次完整的「按角色配后端」的配置设计与验证。

**任务背景**：某 DiT 同时含 self-attention（`role="self"`，长视频序列，head_dim=128）和 cross-attention（`role="cross"`，文本序列）。性能上希望 self 跑最快的 `FLASH_ATTN`，但 cross 因为序列短且要做数值对齐，改用最稳的 `TORCH_SDPA`。另外模型里还有一个模型专有站点 `role="mymodel.audio_to_video"`、`role_category="cross"`，希望它跟 cross 一样走 SDPA。

**步骤 1：用程序化 API 构造配置并验证前三级命中**（无需 GPU）：

```python
# 示例代码
from vllm_omni.diffusion.data import AttentionConfig, AttentionSpec

cfg = AttentionConfig(
    default=AttentionSpec(backend="FLASH_ATTN"),   # 第 3 级：self 兜底
    per_role={
        "cross": AttentionSpec(backend="TORCH_SDPA"),  # 第 1 级：cross 精确命中
        # "mymodel.audio_to_video" 不写 —— 让它第 2 级经 role_category="cross" 回退到 SDPA
    },
)
for role, cat in [("self", None), ("cross", None), ("mymodel.audio_to_video", "cross")]:
    spec, source = cfg.resolve_with_source(role=role, role_category=cat)
    print(f"{role:28s} -> {spec.backend:10s} | {source}")
```

预期输出：

```
self                         -> FLASH_ATTN | attention_config.default
cross                        -> TORCH_SDPA | attention_config.per_role['cross']
mymodel.audio_to_video       -> TORCH_SDPA | attention_config.per_role['cross'] (role_category fallback)
```

**步骤 2：写出等价的 CLI 配置**（来自用户手册 [attention_backends.md:61-70](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/user_guide/diffusion/attention_backends.md#L61-L70)）：

```bash
vllm-omni serve <model> \
    --diffusion-attention-config.default.backend FLASH_ATTN \
    --diffusion-attention-config.per_role.cross.backend TORCH_SDPA
```

或一份 JSON：

```bash
vllm-omni serve <model> \
    --diffusion-attention-config '{"default":{"backend":"FLASH_ATTN"},"per_role":{"cross":{"backend":"TORCH_SDPA"}}}'
```

**步骤 3：解释解析顺序如何命中**。请用本讲学到的四级优先级，逐站点回答：

- `self`：`per_role["self"]` 无 → `role_category` 为 None 跳过第 2 级 → 命中第 3 级 `default=FLASH_ATTN`。
- `cross`：第 1 级 `per_role["cross"]` 直接命中 `TORCH_SDPA`。
- `mymodel.audio_to_video`：第 1 级无精确匹配 → 第 2 级用 `role_category="cross"` 命中 `per_role["cross"]` → `TORCH_SDPA`。

**步骤 4（可选，需 GPU）**：启动服务，在日志里确认每个 role 实际解析到的后端。日志由 selector 的 `_log_backend_resolution` 产生，形如 `Resolved diffusion attention backend 'TORCH_SDPA' for role='cross' via attention_config.per_role['cross']`。

> 待本地验证：步骤 4 的日志行需要在真实 diffusion 服务启动时观察。

完成本实践，你就把「role 声明 → 四级解析 → 后端契约 → 平台映射」整条链路走通了。

## 6. 本讲小结

- diffusion 注意力的**后端**是「具体 kernel 实现」，成对出现：`AttentionBackend`（静态描述）+ `AttentionImpl`（实际前向）。
- 模型用自由字符串 **`role`**（self/cross/模型专有）给注意力站点命名；`role_category` 提供类别回退。命名只影响配置命中，不影响数值。
- **后端选择发生在构造期**：`Attention.__init__` 调 `get_attn_backend_for_role` 定死后端实现类并实例化，前向只调用。
- 解析是固定**四级优先级**：`per_role[role]` → `per_role[role_category]` → `default` → 平台默认。前三级在 `AttentionConfig.resolve_with_source`，第四级在 selector 调平台。
- `AttentionSpec` 用 `backend_kwargs()` 把后端专有参数（`skip_softmax`/`quant`）序列化给 `AttentionImpl`，并在构造期强制校验「专有参数只配给支持它的后端」。
- selector 只懂「后端名 → 类路径」（`DiffusionAttentionBackendEnum` + 懒加载 + `@cache`）；「这个硬件能用谁/默认选谁」全权委托给 `current_omni_platform.get_diffusion_attn_backend_cls`，CUDA 上是逐级降级（TRTLLM→CUDNN→FLASHINFER→FLASH_ATTN→SDPA）。

## 7. 下一步学习建议

- **u7-l2 并行注意力**：本讲只讲了「单卡后端选择」。Ring / Ulysses 序列并行是**叠加在后端之上**的通信策略，`Attention._forward_impl` 里的 `pre_attention / post_attention` 就是它们插入的地方。读 [layer.py:263-290](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/layer.py#L263-L290) 衔接。
- **u8-l2 平台抽象**：本讲反复出现的 `current_omni_platform` 来自 `vllm_omni/platforms`，下一阶段应系统学习各平台类如何提供 worker 类、注意力后端与默认 stage 配置。
- **u9-l1 添加新 Diffusion 模型**：如果你要为自己接的模型声明 role，参考 `docs/contributing/model/adding_diffusion_model.md` 的「Declaring attention roles」一节与本讲引用的三处真实声明。
- **进阶阅读**：`docs/user_guide/diffusion/attention_backends.md`（全部后端与 Skip-Softmax/SAGE 量化）、`docs/design/feature/skip_softmax.md`（TRTLLM_ATTN 的稀疏注意力算法）。
