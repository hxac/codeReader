# 检查点配置解析：ModelConfig 与 QuantConfig

## 1. 本讲目标

在 u1 系列，你已经把三段式流水线在命令行层面跑通了。你看到 `tensorrt-edgellm-export` 吃进去一个 HuggingFace 检查点目录，吐出来一份 ONNX 加上一堆 sidecar。但你可能一直有个疑问：

> 导出器拿到一个检查点目录后，**它怎么知道这个模型长什么样**？多少层、多大头、用哪种 RoPE、权重有没有被量化、是不是混合了 attention 和 mamba？

本讲就回答这个问题。我们要钻进「导出」阶段最开头的那一步——**配置解析**：把一个 HF 检查点目录里的 `config.json` / `hf_quant_config.json` / `*.safetensors` 索引，翻译成一个 Python 内存里的、扁平的、导出器和权重加载器都能直接消费的数据结构。

学完本讲你应该能够：

1. 说清楚 `load_checkpoint_config_dicts` 是如何把一个检查点目录读成 `(root_dict, llm_dict)` 两个字典的，尤其是多模态/嵌套配置是如何被「提升（promote）」的。
2. 理解 `ModelConfig` 这个扁平数据类里有哪些关键字段（架构、层类型 `layer_types`、RoPE、MoE、投机解码标志），以及 `ModelConfig.from_pretrained` 是如何把它们逐个填出来的。
3. 掌握 `QuantConfig` 与九个量化常量（`fp16` / `fp8` / `mxfp8` / `nvfp4` / `int4_awq` / `int4_awq_modelopt` / `int4_gptq` / `int8_sq` / `mixed_precision`），并知道 `_parse_quant` 是按什么顺序判定一个检查点用的是哪种量化。

本讲是 Python 导出前端（u2 单元）的第一篇，也是后续所有 Python 篇（模型分发、权重加载、ONNX 导出）的公共地基——因为后面每一篇都要先有一个解析好的 `ModelConfig` 才能干活。

---

## 2. 前置知识

本讲假设你已经具备来自 u1 系列的认知：

- **检查点目录**：一个 HF 风格的目录，至少包含 `config.json`（结构描述）和若干 `*.safetensors`（权重），通常还有分词器文件。`config.json` 是「模型的结构图纸」，本讲的主角就是它。
- **三段式流水线**：检查点 → Python 导出（ONNX）→ C++ 引擎构建 → C++ 运行时。本讲处于第二段的**最开头**。
- **量化的产物仍是检查点**：量化阶段不直接产出 ONNX，而是产出一个「已经被量化的 HF 风格检查点」，导出器再吃它。所以导出器必须能识别量化元数据。
- **量化 CLI 与包结构**：`tensorrt-edgellm` 是导出前端的 Python 包，`config.py` 是它的配置模块。

此外，先澄清几个本讲会反复出现的术语，对初学者可能陌生：

- **`model_type`**：HF `config.json` 里的一个字段，标注模型的架构族，例如 `"qwen3"`、`"llama"`、`"nemotron_h"`。它是后续模型分发（u2-l2）选择模型类的主键，但**本讲里它只是被原样读出来**，不做分发。
- **混合模型（hybrid）**：一个 Transformer 里**同时存在多种层类型**，比如有的层是普通 attention，有的层是 Mamba（状态空间模型），有的层是 GDN（线性注意力）。Nemotron-H、Qwen3.5 就是混合模型。本讲要讲清 `layer_types` 是如何把这种「逐层类型」解析出来的。
- **RoPE（旋转位置编码）**：给 attention 的 Q/K 注入位置信息的方式。不同模型族存 RoPE 超参的位置很不一样（有的在 `rope_theta`，有的在 `rope_scaling`，有的在 `rope_parameters`），本讲会讲清楚解析的兜底链。
- **`dataclass`**：Python 标准库里用来快速定义「全是字段的数据类」的装饰器。`ModelConfig` / `QuantConfig` 都是 dataclass。

---

## 3. 本讲源码地图

本讲只围绕两个文件展开：

| 文件 | 作用 | 本讲用它做什么 |
| --- | --- | --- |
| `tensorrt_edgellm/checkpoint/checkpoint_utils.py` | 检查点元数据的 I/O；负责把检查点目录读成原始 dict、生成运行时 sidecar | 讲 `load_checkpoint_config_dicts`：把 `config.json` 读成 `(root_dict, llm_dict)`，处理多模态嵌套提升与 RoPE 兼容 |
| `tensorrt_edgellm/config.py` | 配置解析的核心；定义所有 dataclass 与解析函数 | 讲 `ModelConfig` / `QuantConfig` 数据结构、`ModelConfig.from_pretrained` 工厂、`_parse_quant` / `_parse_layer_types` 等解析逻辑 |

> 一个重要的从属关系：`config.py` 在文件顶部就 `from .checkpoint.checkpoint_utils import load_checkpoint_config_dicts`（[config.py:60](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L60)），`ModelConfig.from_pretrained` 第一行就调用它。所以本讲的讲解顺序是：**先讲 `checkpoint_utils` 怎么把原始 dict 读出来，再讲 `config.py` 怎么把这些 dict 解析成强类型 dataclass**。

本讲三个最小模块对应这条执行链：

- **4.1 checkpoint_utils**：从磁盘读出 `(root_dict, llm_dict)`。
- **4.2 ModelConfig**：把 `llm_dict` 解析成架构/层类型/RoPE 字段。
- **4.3 QuantConfig**：从 `hf_quant_config.json` 或内嵌的 `quantization_config` 解析量化元数据。

---

## 4. 核心概念与源码讲解

### 4.1 checkpoint_utils：把检查点目录读成两个 dict

#### 4.1.1 概念说明

在动 `ModelConfig` 之前，导出器得先有「原料」。原料就是检查点目录里那几个 JSON 文件描述出来的字典。`checkpoint_utils` 模块的职责非常聚焦：**只做检查点元数据的 I/O，不碰权重本身**（文件顶部明确写了「Weights are loaded only via `loader.load_weights`」，见 [checkpoint_utils.py:15-19](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py#L15-L19)）。

为什么需要单独这一层？因为 HuggingFace 的 `config.json` 格式在不同模型族之间**非常不一致**：

- 纯 LLM（如 Llama、Qwen3）的架构字段（`hidden_size`、`num_attention_heads`）直接放在 `config.json` 顶层。
- 多模态模型（如 Qwen2.5-VL、Qwen3-Omni）的 LLM 架构字段藏在嵌套子对象里（`text_config`、`language_config`、`thinker_config.text_config`、`talker_config`）。
- `transformers` v5 把 RoPE 配置从 `rope_scaling` 挪到了 `rope_parameters`，还可能只存在于某个嵌套子配置里。

如果每个下游模块都要自己处理这些乱七八糟的情况，代码会重复且易错。所以 `checkpoint_utils` 把这些「脏活」集中到一个函数里，给下游返回**两个已经规整好的字典**：`root_dict`（原始顶层配置，保留多模态的视觉/音频等子树）和 `llm_dict`（专门给 LLM 架构解析用的、已经被「提升」过的字典）。

#### 4.1.2 核心流程

`load_checkpoint_config_dicts(model_dir)` 的执行流程如下：

```text
传入 model_dir（本地目录 或 HF model id）
        │
        ▼
1. 直接读 model_dir/config.json 到 raw（原始 JSON 字典）
   └─ 若 model_dir 不是本地目录，尝试从 HF Hub 下载 config.json
        │
        ▼
2. 尝试 AutoConfig.from_pretrained(model_dir, trust_remote_code=True)
   ├─ 成功 → root = config.to_dict()
   └─ 失败（未注册的 model_type）→ root = raw，记一条 warning
        │
        ▼
3. 提升 LLM 子配置：llm = _promote_llm_subconfig(config, root)
   ├─ root 自带 num_attention_heads → llm = root（纯 LLM）
   ├─ 否则在 text_config/llm_config/language_config 里找
   ├─ Qwen3-Omni：在 thinker_config.text_config 里找
   ├─ Qwen3-TTS：用 talker_config
   └─ Alpamayo-R1：单独走 _promote_alpamayo_llm_config（从 vlm_name_or_path 拉取）
        │
        ▼
4. 补回提升时丢失的字段：把 raw 里有、但 llm 里没有的字段补进 llm
        │
        ▼
5. RoPE 兼容：若 llm 没有 rope_scaling，尝试从 rope_parameters
   或嵌套 text_config 等位置找回，并 normalize 成运行时期待的形状
        │
        ▼
6. 返回 (root, llm)
```

返回的 `llm` 才是后续 `ModelConfig` 解析真正用的那个字典；`root` 则保留下来给多模态的视觉/音频导出使用。

#### 4.1.3 源码精读

函数签名与文档在 [checkpoint_utils.py:206-217](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py#L206-L217)，它说明：先用 `AutoConfig.from_pretrained`（能处理已注册的 HF 模型类型），失败再回退到直接读 `config.json`（用于自定义/未注册类型如 `qwen3_asr`、`qwen3_tts`）；多模态模型的文本配置由 `_promote_llm_subconfig` 提升出来。

回退逻辑在 [checkpoint_utils.py:236-248](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py#L236-L248)：`AutoConfig` 抛 `ValueError`/`OSError` 时，记一条 warning，把 `root` 置为原始 `raw`，`config` 也指向 `root`，然后继续——这样未注册的模型类型也能被解析。

核心的「提升」逻辑在 `_promote_llm_subconfig`，见 [checkpoint_utils.py:127-158](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py#L127-L158)：

```python
def _promote_llm_subconfig(config, root):
    # 1) 纯 LLM：root 自己就有 num_attention_heads，直接用 root
    if root.get("num_attention_heads") is not None:
        return root
    # 2) 常规多模态：在 llm_config / text_config / language_config 里找
    for name in ("llm_config", "text_config", "language_config"):
        ...
        if (sub_dict.get("hidden_size") is not None
                and sub_dict.get("num_attention_heads") is not None):
            return sub_dict
    # 3) Qwen3-ASR / Qwen3-Omni：LLM 在 thinker_config.text_config
    thinker = root.get("thinker_config")
    ...
    # 4) Qwen3-TTS：LLM(talker) 在 talker_config
    talker = root.get("talker_config")
    ...
    return root
```

判定一个子配置「是不是 LLM 配置」的标准很朴素但有效：**同时有 `hidden_size` 和 `num_attention_heads`**。这两个字段是所有 LLM 架构的必备项。

提升之后的「补回字段」和「RoPE 兼容」两步在 [checkpoint_utils.py:261-287](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py#L261-L287)。补回字段只会填入 `llm` 里**缺失**的键，绝不覆盖已有值；RoPE 兼容则是为了 C++ 运行时的 `collectRopeConfig()` 能正确识别 mRoPE——只要 `rope_scaling` 缺失，就尝试从 `rope_parameters` 或嵌套的 `text_config` 里找回，最后用 `normalize_rope_scaling_for_runtime` 规整。

> 小贴士：还有个便捷包装 `load_config_dict(model_dir)`（[checkpoint_utils.py:291-293](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py#L291-L293)），它只返回提升后的 `llm_dict`，适合只关心 LLM 架构、不需要 `root` 的调用方。本讲的代码实践里两个都会用到。

#### 4.1.4 代码实践

**实践目标**：亲手把一个真实 HF 检查点目录读成 `(root, llm)` 两个字典，验证 `_promote_llm_subconfig` 的效果。

**操作步骤**：

1. 准备一个本地检查点目录（任何已下载的 LLM，如 `Qwen/Qwen3-8B` 的本地副本），或直接用 HF model id（首次会联网下载）。
2. 在仓库根目录，确保已按 u1-l3 安装好 `tensorrt_edgellm`（至少装了基础依赖，里面有 `transformers`）。
3. 写一段最小脚本（**示例代码**，非项目原有）：

```python
# probe_config.py —— 示例代码
from tensorrt_edgellm.checkpoint.checkpoint_utils import (
    load_checkpoint_config_dicts, load_config_dict,
)

model_dir = "Qwen/Qwen3-8B"   # 换成你的本地路径或 HF id

root, llm = load_checkpoint_config_dicts(model_dir)
print("root model_type      :", root.get("model_type"))
print("root 有没有 num_attention_heads:", "num_attention_heads" in root)
print("llm  hidden_size     :", llm["hidden_size"])
print("llm  num_attn_heads  :", llm["num_attention_heads"])
print("llm  rope_theta      :", llm.get("rope_theta"))
print("llm  rope_scaling    :", llm.get("rope_scaling"))

# 便捷包装：只拿 llm_dict
only_llm = load_config_dict(model_dir)
print("only_llm 和 llm 是同一个 dict 吗:", only_llm is llm or only_llm == llm)
```

4. 运行：`python probe_config.py`。

**需要观察的现象**：

- 对一个**纯 LLM**（如 Qwen3-8B），`root` 里就有 `num_attention_heads`，所以 `_promote_llm_subconfig` 直接返回 `root`，`root` 和 `llm` 几乎是同一份内容。
- 若你换成多模态（如 `Qwen/Qwen2.5-VL-7B-Instruct`），会看到 `root` 里没有顶层 `num_attention_heads`，但 `llm` 里有——这正是「提升」把 `text_config` 提上来的效果。

**预期结果**：脚本不报错，并打印出合理的架构字段数值。

**待本地验证**：如果你本地没有 GPU/没装 `transformers`，或网络拉不到模型，这一步可能失败。此时可退化为「源码阅读型实践」——直接打开任一 HF 模型的 `config.json`，对照 `_promote_llm_subconfig` 的四个分支，人工判断它会走哪一支。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `load_checkpoint_config_dicts` 要先尝试 `AutoConfig`，失败了才回退到原始 `config.json`？

**参考答案**：`AutoConfig` 能识别 HF 已注册的模型类型，会做归一化（比如 `attribute_map` 把 `num_experts` 映射成 `num_local_experts`）、能把嵌套子配置解析成对象；但它对未注册/自定义类型（`qwen3_asr`、`qwen3_tts`）会抛异常。回退到原始 JSON 保证这些自定义类型也能被解析，代价是丢失归一化（后续解析函数要自己处理别名）。

**练习 2**：`_promote_llm_subconfig` 用「同时有 `hidden_size` 和 `num_attention_heads`」作为「这是 LLM 配置」的判据。如果某个视觉编码器的 `config` 里恰好也有这两个字段，会发生什么？

**参考答案**：遍历顺序是 `llm_config` → `text_config` → `language_config` → `thinker_config.text_config` → `talker_config`，先命中的子配置会被返回。视觉编码器的配置通常不在这些命名空间下（它叫 `vision_config`），所以一般不会误命中；但这种「按字段存在性」而非「按类型」的判据确实较脆弱，是它需要 `_promote_*` 系列特例（如 Alpamayo）不断打补丁的原因。

---

### 4.2 ModelConfig：架构字段、层类型与 RoPE

#### 4.2.1 概念说明

有了 `llm_dict` 之后，`config.py` 把它解析成一个强类型的扁平数据类 `ModelConfig`。「扁平」是关键词——`ModelConfig` 的设计哲学是：**把 HF 那套层层嵌套、字段散落各处的配置，摊平成一组一目了然的字段**，让下游的模块构建器（`make_linear`、attention 组装、权重加载）不用再到处翻字典。

`ModelConfig` 在 [config.py:469-663](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L469-L663) 定义，字段非常多，但可以归成几组：

| 字段组 | 代表字段 | 含义 |
| --- | --- | --- |
| 架构主键 | `model_type`、`hidden_size`、`num_hidden_layers`、`num_attention_heads`、`num_key_value_heads`、`intermediate_size`、`head_dim`、`vocab_size` | 模型的「骨架尺寸」 |
| 归一化与激活 | `rms_norm_eps`、`hidden_activation` | RMSNorm 的 epsilon、激活函数名 |
| RoPE | `rope_theta`、`rope_scaling`、`partial_rotary_factor`、`max_position_embeddings`、`original_max_position_embeddings`、`sliding_rope_config`、`full_rope_config` | 位置编码相关 |
| 模型族特性开关 | `has_qk_norm`、`has_value_norm`、`attention_bias`、`attention_scaling`、`embedding_scale`、`final_logit_softcapping`、`tie_word_embeddings` | 不同模型族的细节差异 |
| 逐层层类型 | `layer_types`、`attention_layer_types` | **混合模型**的核心：每个 decoder 层是什么类型 |
| 混合/Mamba/GDN | `mamba_cfg`、`gdn_cfg` | Mamba 与 GDN 层的超参 |
| MoE | `num_experts`、`n_routed_experts`、`num_experts_per_tok`、`moe_intermediate_size`、`decoder_sparse_step` | 稀疏专家 |
| 投机解码 | `mtp_*`、`eagle_base`、`dflash_*`、`gemma4_mtp_*`、`draft_vocab_size` | EAGLE3/MTP/DFlash/Gemma4-MTP 标志 |
| 量化 | `quant: QuantConfig` | 见 4.3 |
| 张量并行 | `mapping: Mapping` | TP/PP/EP 放置信息 |

为什么有这么多字段？因为 EdgeLLM 要在一个统一运行时里支持非常多的模型族（Qwen3、Llama、Gemma4、Nemotron-H、Phi、Qwen3.5、各种投机解码 draft……），每个族都有自己的小脾气。把这些差异全部摊到一个扁平结构里，换取的是「下游只认 `ModelConfig`，不必关心是哪个模型族」的统一性。

#### 4.2.2 核心流程

`ModelConfig.from_pretrained(model_dir, default_attention_scale)` 是唯一的入口，流程如下（对应 [config.py:811-1022](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L811-L1022)）：

```text
1. root, llm_dict = load_checkpoint_config_dicts(model_dir)   # 4.1
        │
        ▼
2. 读架构主键：model_type / hidden_size / num_attention_heads / head_dim ...
   └─ head_dim 缺失时用 hidden_size // num_attention_heads 兜底
        │
        ▼
3. quant = _parse_quant(model_dir, llm_dict)                  # 4.3
        │
        ▼
4. layer_types = _parse_layer_types(llm_dict)                 # 逐层类型
   attention_layer_types = _parse_attention_layer_types(...)  # Gemma4 sliding/full
   mamba_cfg = _parse_mamba_cfg(llm_dict, layer_types, model_dir)
   gdn_cfg   = _parse_gdn_cfg(llm_dict, layer_types)
        │
        ▼
5. 特性开关：has_qk_norm（扫权重 key 自动探测）、has_value_norm、
   attention_scaling、embedding_scale、rope_theta、rope_scaling、
   partial_rotary_factor、dual_rope_configs ...
        │
        ▼
6. 解析 MoE 字段、MTP/EAGLE/DFlash 字段、Gemma4 字段 ...
        │
        ▼
7. return cls(...)  —— 把上面所有局部变量一次性灌进 ModelConfig
```

注意第 2 步对 `head_dim` 的兜底：`head_dim = llm_dict.get("head_dim", hidden_size // num_attention_heads)`（[config.py:837](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L837)）。很多老模型（Llama）不写 `head_dim`，要靠 `hidden_size / num_heads` 推出来。

#### 4.2.3 源码精读

**(a) 逐层层类型 `layer_types`**

这是混合模型的关键。常量在 [config.py:82-87](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L82-L87)：`attention` / `mamba` / `mlp` / `gdn` / `moe`。解析函数 `_parse_layer_types` 在 [config.py:1271-1311](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L1271-L1311)，它按下面优先级工作：

1. 优先读 `layers_block_type` 或 `layer_types`（Nemotron-H 用前者，Qwen3.5 用后者）。
2. 把原始字符串归一化：`linear_attention` → `gdn`；含 `mamba` → `mamba`；`moe` → `moe`；含 `mlp` → `mlp`；`sliding_attention`/`full_attention`（Gemma4）保留原串；其余归为 `attention`。
3. 如果都没有，看 `hybrid_override_pattern`（Nemotron-H 的模式串，`M`/`-`/`*`/`E` 分别代表 mamba/mlp/attention/moe）。
4. 再没有，就 `[attention] * num_hidden_layers`（默认全是 attention 层）。

也就是说：**对纯 attention 模型，`layer_types` 是一个全 `attention` 的列表**；对混合模型，它是一个逐层标注的序列。下游属性如 `is_hybrid`、`num_mamba_layers`、`num_gdn_layers`（[config.py:729-760](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L729-L760)）都是基于这个列表数出来的。

**(b) Mamba / GDN 子配置**

只要 `layer_types` 里出现了 `mamba`，就解析一个 `MambaConfig`（[config.py:1314-1359](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L1314-L1359)）；出现 `gdn` 就解析 `GdnConfig`（[config.py:1400-1411](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L1400-L1411)）。

`MambaConfig` 有一处特别值得讲的工程细节——`conv_dim` 的循环依赖问题。`conv_dim` 依赖 `n_groups`，`n_groups` 又依赖 `conv_dim`。当 config.json 没显式写 `conv_dim`（如 NemotronH-4B-BF16）时，解析器会**直接去 safetensors 索引里读 `conv1d.weight` 的形状**来打破这个环（[config.py:1336-1345](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L1336-L1345) 与 [`_detect_mamba_conv_dim`:1362-1397](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L1362-L1397)）：`conv1d` 权重形状是 `[conv_dim, 1, conv_kernel]`，取 `shape[0]` 即可，**完全不加载张量数据**。

> 这是一个很好的设计范例：配置解析阶段就允许「读一点点权重元数据（形状）」来解决纯 JSON 表达不了的依赖，但又克制到只读 shape 不读 data。

**(c) RoPE 的多级兜底**

不同模型族存 `rope_theta` 的位置五花八门，`_get_rope_theta`（[config.py:102-122](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L102-L122)）用一条兜底链处理：

1. 顶层 `rope_theta`；
2. `rope_scaling.rope_theta` / `rope_parameters.rope_theta`；
3. `rope_scaling.full_attention.rope_theta` / `sliding_attention.rope_theta`（Gemma4 双 RoPE）；
4. 都没有就返回默认 `10000.0`。

类似地，`partial_rotary_factor`（部分旋转，phi3/phi4 用 0.75）也走「顶层 → `rope_parameters` → 嵌套 attention 块」的兜底（[config.py:1414-1433](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L1414-L1433)）。

**(d) `has_qk_norm` 的自动探测**

很多模型族（Qwen3、Gemma）在 Q/K 投影后接了一个 per-head RMSNorm。但不同模型标记它的方式不一样。EdgeLLM 的做法很优雅——**不靠 `model_type` 字符串判断，而是直接扫检查点的权重 key**：只要存在 `.q_norm.weight`，就认定 `has_qk_norm=True`（[config.py:1436-1443](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L1436-L1443)）。这种「以权重事实为准」的策略比「按模型名 if-else」更鲁棒，新模型只要权重命名规范就能被自动识别。

文件顶部的模块文档对此有明确声明：「`has_qk_norm` 通过扫描检查点 key 索引里的 `q_norm` 权重名自动探测；不使用任何 model_type 字符串比较」（[config.py:24-27](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L24-L27)）。

**(e) 派生属性与张量并行**

`ModelConfig` 还提供一组只读属性（[config.py:691-775](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L691-L775)），如 `is_hybrid`（有 mamba/gdn 即为混合）、`is_eagle3_draft`、`is_mtp_draft`、`compute_dtype`。张量并行靠 `for_rank(rank, world)`（[config.py:777-805](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L777-L805)）：它深拷贝一份 config，把 `num_attention_heads` / `num_key_value_heads` / `intermediate_size` 整除 `world`，并更新 `mapping`，从而每个 rank 拿到的是「自己的那份形状」。

#### 4.2.4 代码实践

**实践目标**：用 `ModelConfig.from_pretrained` 解析一个真实检查点，打印它的架构字段、`layer_types` 和量化类型。

**操作步骤**：

1. 接 4.1.4 的环境。注意 `from_pretrained` 的签名（[config.py:811-814](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L811-L814)）要求第二个参数 `default_attention_scale: Callable[[int], float]`——一个接收 `head_dim`、返回默认 attention scale 的函数。标准 Transformer 用 \(1/\sqrt{d}\)。
2. 写脚本（**示例代码**）：

```python
# probe_modelconfig.py —— 示例代码
import math
from tensorrt_edgellm.config import ModelConfig

model_dir = "Qwen/Qwen3-8B"   # 换成你的检查点

cfg = ModelConfig.from_pretrained(
    model_dir,
    default_attention_scale=lambda head_dim: 1.0 / math.sqrt(head_dim),
)

print("model_type        :", cfg.model_type)
print("hidden_size       :", cfg.hidden_size)
print("num_hidden_layers :", cfg.num_hidden_layers)
print("num_attention_heads:", cfg.num_attention_heads)
print("num_key_value_heads:", cfg.num_key_value_heads)
print("head_dim          :", cfg.head_dim)
print("vocab_size        :", cfg.vocab_size)
print("layer_types       :", cfg.layer_types[:6], "... 共", len(cfg.layer_types), "层")
print("is_hybrid         :", cfg.is_hybrid)
print("has_qk_norm       :", cfg.has_qk_norm)
print("rope_theta        :", cfg.rope_theta)
print("rope_scaling      :", cfg.rope_scaling)
print("--- 量化 ---")
print("quant.quant_type  :", cfg.quant.quant_type)
print("quant.is_quantized:", cfg.quant.is_quantized)
print("quant.group_size  :", cfg.quant.group_size)
```

3. 运行：`python probe_modelconfig.py`。

**需要观察的现象**：

- 对一个纯 Qwen3 fp16 模型：`layer_types` 是 `['attention', 'attention', ...]`（长度等于 `num_hidden_layers`）；`is_hybrid=False`；`has_qk_norm=True`（Qwen3 有 QK-norm，会被自动扫到）；`quant.quant_type == "fp16"`、`is_quantized=False`。
- 若换成量化过的检查点（如 NVFP4 版本），`quant.quant_type` 会变成 `"nvfp4"`，`is_quantized=True`。
- 若换成 Nemotron-H 或 Qwen3.5，`layer_types` 里会出现 `mamba` 或 `gdn`，`is_hybrid=True`，且 `mamba_cfg`/`gdn_cfg` 不再是 `None`。

**预期结果**：打印出的字段与该模型 HF 主页宣称的架构一致。

**待本地验证**：若本地无法拉取模型，退化为阅读型实践——打开 Qwen3-8B 的 `config.json`，对照 `_parse_layer_types` 的分支，确认它的 `layer_types` 会走「都没有 → 全 attention」的兜底分支。

#### 4.2.5 小练习与答案

**练习 1**：`_parse_layer_types` 在没有任何层类型信息时，返回 `[LAYER_ATTN] * num_hidden_layers`。为什么不返回空列表？

**参考答案**：因为绝大多数模型是纯 attention 模型，且下游（KV cache 分配、模块构建、C++ 运行时的逐层路由表）都依赖 `layer_types` 非空。空列表会让 `num_attn_layers` 等属性全为 0，导致 KV cache 不分配、构建崩溃。返回「全 attention」是最安全的合理默认。

**练习 2**：`has_qk_norm` 用扫权重 key 的方式探测，而不是看 `model_type`。举一个这种设计带来好处的新模型场景。

**参考答案**：假设社区新出一个 `model_type="my_new_llm"` 的模型，它和 Qwen3 一样在 Q/K 后接了 RMSNorm、权重命名为 `*.q_norm.weight`。如果按 `model_type` 判断，代码里没有 `"my_new_llm"` 分支就会漏掉这个 norm，导致数值错误；而按权重 key 探测，新模型无需改任何代码就能被正确识别为 `has_qk_norm=True`。代价是：如果某个模型的 QK-norm 权重命名不符合 `.q_norm.weight` 约定，就会漏检——这是「按事实探测」的固有风险。

---

### 4.3 QuantConfig：量化元数据

#### 4.3.1 概念说明

`ModelConfig.quant` 字段是一个 `QuantConfig`。它的职责是回答：「这个检查点的权重是什么量化格式？哪些模块被排除了？KV 缓存有没有量化？」这些信息决定了导出时每个 Linear 层该用哪个类（`FP16Linear` / `FP8Linear` / `NVFP4Linear` / `AWQLinear` ...）、权重重排（repacking）该怎么排、以及运行时的 KV 缓存用不用 FP8。

量化类型常量定义在 [config.py:66-77](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L66-L77)，共九个：

| 常量 | 字符串 | 含义 |
| --- | --- | --- |
| `QUANT_FP16` | `"fp16"` | 普通 bf16/fp16 权重，无量化（**默认值**） |
| `QUANT_FP8` | `"fp8"` | FP8 E4M3，per-tensor 静态量化 |
| `QUANT_MXFP8` | `"mxfp8"` | MXFP8，per-block（block_size=32） |
| `QUANT_NVFP4` | `"nvfp4"` | NVFP4，per-group，FP8 组缩放 |
| `QUANT_INT4_AWQ` | `"int4_awq"` | AWQ INT4，列打包 int32 检查点 |
| `QUANT_INT4_AWQ_MODELOPT` | `"int4_awq_modelopt"` | ModelOpt 预打包 uint8 `[out//2, in]` 检查点 |
| `QUANT_INT4_GPTQ` | `"int4_gptq"` | GPTQ INT4 分组量化 |
| `QUANT_INT8_SQ` | `"int8_sq"` | SmoothQuant INT8，W8A8 per-channel |
| `QUANT_MIXED` | `"mixed_precision"` | 逐层混合量化（`hf_quant_config` 专用标记） |

> 一个重要区分：`int4_awq`（列打包 int32）和 `int4_awq_modelopt`（预打包 uint8）虽然都叫 AWQ，但**检查点的张量布局完全不同**，对应不同的重排路径。这也是为什么要把它们拆成两个常量。模块顶部的文档对每种格式的存储布局都有简短说明（[config.py:29-38](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L29-L38)）。

#### 4.3.2 核心流程

量化解析的入口是 `_parse_quant(model_dir, config)`（[config.py:1663-1765](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L1663-L1765)）。它按下面的优先级判定：

```text
1. 有没有 sidecar 文件 hf_quant_config.json？
   ├─ 有 → 读它的 quantization.quant_algo，进入 ModelOpt/统一检查点分支：
   │       · W4A16_AWQ          → int4_awq_modelopt
   │       · MIXED_PRECISION    → 调 _parse_mixed_precision，拆出 dominant + layer_overrides
   │       · FP8_PB/MXFP8       → mxfp8
   │       · FP8                → fp8
   │       · NVFP4/FP4          → nvfp4
   │       · W8A8/INT8          → int8_sq
   │       · 其余               → fp16
   │       期间用 _effective_excluded_modules 修正排除列表（对照真实权重）
   │
   └─ 无 → 看内嵌的 config.json["quantization_config"]
           · 有 quant_algo 键   → 同上的 algo 映射
           · quant_method=="awq" → int4_awq（列打包）
           · quant_method=="gptq"→ int4_gptq（并探测 zero_point_offset）
           · 都没有             → 返回默认 QuantConfig()（fp16，未量化）
```

关键点：**判定优先级是「外部 sidecar 文件 > 内嵌块」**。ModelOpt/本工具量化产出的检查点带 `hf_quant_config.json`；社区 HF 上的 AWQ/GPTQ 模型则把量化信息内嵌在 `config.json` 的 `quantization_config` 里。

#### 4.3.3 源码精读

**(a) `QuantConfig` 数据结构**

定义在 [config.py:351-390](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L351-L390)，关键字段：

- `quant_type: str = QUANT_FP16`：主量化类型。
- `group_size: int = 1`：分组大小。注释写得很清楚——`1` 表示 per-tensor/per-channel，`16` 是 NVFP4，`128` 是 AWQ。
- `gptq_zero_point_offset: int = 1`：GPTQ 零点偏移（不同 GPTQ 检查点对 qzeros 的存法不一致，这个偏移用来对齐）。
- `kv_cache_quant: Optional[str]`：KV 缓存量化，`"fp8"` 或 `None`。
- `excluded: List[str]`：被排除出量化的模块名（通常是 `["lm_head"]`）。
- `layer_overrides: dict`：逐层量化覆盖（仅 `MIXED_PRECISION` 用），把模块名映射到量化类型字符串。
- `is_mixed_precision: bool`：是否混合精度。

它还提供两个派生属性：`is_quantized`（`quant_type != fp16`，[config.py:374-376](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L374-L376)）和 `uses_nvfp4_weights`（主类型或任一覆盖是 nvfp4，[config.py:378-383](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L378-L383)）。

**(b) 算法字符串到类型的映射**

`_algo_to_quant_type`（[config.py:1819-1836](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L1819-L1836)）把 `hf_quant_config` 里的 `quant_algo` 字符串（如 `"W4A16_AWQ"`、`"FP8"`、`"NVFP4"`）翻译成上述常量。注意它**先判 `MXFP8`/`FP8_PB` 再判通用 `FP8`**，否则 per-block 的 MXFP8 会被通用 FP8 吞掉。

**(c) 混合精度解析**

`MIXED_PRECISION` 是最复杂的情况：不同层用不同量化。`_parse_mixed_precision`（[config.py:1839-1880](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L1839-L1880)）的做法是：

1. 统计 `quantized_layers` 里每种 algo 出现的次数，**取出现最多的那个作为 dominant（主导）类型**，作为 `quant_type`。
2. 把**每一个**量化模块名都记进 `layer_overrides`，映射到它的量化类型。
3. 还会把融合投影名展开：`self_attn.qkv_proj` → `q_proj`/`k_proj`/`v_proj`，`mlp.gate_up_proj` → `gate_proj`/`up_proj`，因为 `make_linear` 是按拆分后的名字查的。

这样 `quant_type` 给出「总体是啥」，`layer_overrides` 给出「逐层例外」。下游 `module_quant_type` 负责把它们合并成「某个具体模块最终用什么」。

**(d) `module_quant_type`：单模块的最终裁决**

这是下游最常调用的函数（[config.py:393-419](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L393-L419)）。给定一个 `module_name`（如 `"lm_head"`、`"layers.0.mlp.gate_proj"`），它按下面顺序返回该模块的最终量化类型：

```text
1. module_name 在 excluded 列表里         → fp16
2. 是 tied lm_head 且 backbone 未量化      → fp16（权重会从 embed_tokens 克隆）
3. 有 layer_overrides：
   · 命中 module_name                     → 用覆盖值
   · 没命中 + is_mixed_precision          → fp16（未列即未量化）
   · 没命中 + 非混合                       → 用主 quant_type
4. 否则                                    → 主 quant_type
```

它是「这个 Linear 最终用什么精度」的**唯一真相来源**——`make_linear` 用它选 Linear 类，ONNX 导出器用它校验「lm_head 外置只在 fp16 head 时允许」。

**(e) 对照真实权重修正排除列表**

量化解析里反复出现 `_effective_excluded_modules` 和 `_detect_modelopt_unquantized_linears`，它们都在**用检查点的真实权重事实去修正 `exclude_modules` 列表**：

- `_effective_excluded_modules`（[config.py:1582-1598](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L1582-L1598)）：丢掉「列表说排除、但检查点里其实有量化张量」的假阳性。
- `_detect_modelopt_unquantized_linears`（[config.py:1601-1660](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L1601-L1660)）：补上「列表没说排除、但 ModelOpt 实际跳过了」的假阴性（典型是视觉塔/音频编码器被整段跳过 PTQ）。

这两个一正一反，保证 `excluded` 与检查点的真实量化状态一致，避免「主类型是 NVFP4，但视觉塔其实是 fp16」时把 fp16 权重错误地塞进 `NVFP4Linear` 导致形状不匹配。

#### 4.3.4 代码实践

**实践目标**：观察同一个模型在「未量化」与「量化」两种检查点下，`QuantConfig` 的差异。

**操作步骤**：

1. 接前面的环境。
2. 写脚本（**示例代码**），对比一个 fp16 检查点和一个量化检查点（如 `Qwen/Qwen3-8B` 与它的 NVFP4/FP8 量化版，若有）：

```python
# probe_quant.py —— 示例代码
import math
from tensorrt_edgellm.config import ModelConfig

def show(model_dir, tag):
    cfg = ModelConfig.from_pretrained(
        model_dir, default_attention_scale=lambda d: 1.0 / math.sqrt(d))
    q = cfg.quant
    print(f"=== {tag} ===")
    print("  quant_type      :", q.quant_type)
    print("  is_quantized    :", q.is_quantized)
    print("  group_size      :", q.group_size)
    print("  kv_cache_quant  :", q.kv_cache_quant)
    print("  excluded        :", q.excluded[:5], "..." if len(q.excluded) > 5 else "")
    print("  is_mixed_precision:", q.is_mixed_precision)
    print("  layer_overrides 项数:", len(q.layer_overrides))

show("Qwen/Qwen3-8B", "fp16 原版")          # 预期: fp16, 未量化
# 若你本地有量化版，换路径再调用一次，例如：
# show("/path/to/Qwen3-8B-NVFP4", "NVFP4 量化版")
```

3. 运行并对比输出。

**需要观察的现象**：

- fp16 原版：`quant_type="fp16"`，`is_quantized=False`，`excluded=[]`，`kv_cache_quant=None`。
- NVFP4 量化版（若有）：`quant_type="nvfp4"`，`is_quantized=True`，`group_size` 多为 `16`，`kv_cache_quant` 可能是 `"fp8"`（取决于量化时是否开了 FP8 KV），`excluded` 里通常能看到 `"lm_head"`（lm_head 常被排除量化）。

**预期结果**：量化版的字段能正确反映该检查点的量化配方。

**待本地验证**：若手头没有量化检查点，可改为阅读型实践——打开 `tests/` 下任意量化集成测试（参考 u9-l6 的测试列表），找到它使用的量化检查点路径与期望的 `quant_type` 断言，对照理解。

#### 4.3.5 小练习与答案

**练习 1**：`_parse_quant` 为什么把「有 `hf_quant_config.json`」的判定放在「内嵌 `quantization_config`」之前？

**参考答案**：`hf_quant_config.json` 是本工具/ModelOpt 量化产出的「权威 sidecar」，信息最完整（含 `exclude_modules`、`kv_cache_quant_algo`、`quantized_layers` 等）；而内嵌的 `quantization_config` 多是社区 AWQ/GPTQ 模型的格式，信息较少。先判 sidecar 能优先走信息更全、布局更规范的分支；同时避免某些模型同时存在两份量化描述时的歧义。

**练习 2**：对一个 `MIXED_PRECISION` 检查点，backbone 大部分层是 NVFP4、但 `lm_head` 是 FP8。`_parse_mixed_precision` 会怎么设 `quant_type` 和 `layer_overrides`？`module_quant_type("lm_head")` 又会返回什么？

**参考答案**：`_parse_mixed_precision` 统计各 algo 出现次数取最多的为 dominant，所以 `quant_type="nvfp4"`；同时把包括 `lm_head` 在内的每个量化模块写进 `layer_overrides`（`"lm_head" -> "fp8"`），并设 `is_mixed_precision=True`。`module_quant_type("lm_head")` 先看 `excluded`（不在）、再看 `layer_overrides`（命中 `"lm_head"`），返回 `"fp8"`。这就是「逐层例外」如何在最终裁决时生效的。

---

## 5. 综合实践

把三个模块串起来，完成一个小任务：**写一个「检查点体检脚本」**，输入任意 HF 检查点目录，输出一份该模型的「配置体检报告」。

报告至少要包含：

1. 模型族（`model_type`）与架构尺寸（`hidden_size` / `num_hidden_layers` / `num_attention_heads` / `head_dim` / `vocab_size`）。
2. 是否混合模型；若是，统计各类层（attention / mamba / gdn / mlp / moe）各有多少层（用 `cfg.num_attn_layers` 等属性）。
3. 量化体检：`quant_type`、是否量化、`group_size`、KV 缓存是否 FP8、`excluded` 列表、是否混合精度。
4. RoPE 体检：`rope_theta`、`partial_rotary_factor`、是否有 `rope_scaling`。
5. 特性开关：`has_qk_norm`、`tie_word_embeddings`、`attention_bias`。
6. 角色判定：这个检查点是普通 LLM、还是某种 draft（EAGLE3/MTP/DFlash/Gemma4-MTP）？用 `cfg.is_eagle3_draft` / `is_mtp_draft` / `is_dflash_draft` / `is_gemma4_mtp_draft` 判断。

实现要点：

- 第一步一定先 `root, llm = load_checkpoint_config_dicts(model_dir)`，观察「提升」前后 `llm` 是否和 `root` 不同（判断是不是多模态）。
- 再 `cfg = ModelConfig.from_pretrained(model_dir, default_attention_scale=lambda d: 1.0/math.sqrt(d))`。
- 把 `cfg` 的字段格式化成报告。注意 `layer_types` 可能很长，只打印前若干项 + 计数。
- 用三个不同类型的检查点跑一遍：纯 LLM（Qwen3/Llama）、混合模型（Nemotron-H/Qwen3.5）、量化检查点，对比报告差异。

> 这个综合实践直接对应导出器在 `export_onnx` 开头做的事：拿到检查点 → 解析 config → 据此决定怎么搭模型。你写的就是一个「精简版的导出前置步骤」。如果你能让脚本对上述三类模型都不崩溃并输出合理报告，说明你已经真正掌握了本讲。

**待本地验证**：若本地没有这么多模型，至少保证纯 LLM 这一支能跑通；其余两支可以用「人工对照源码 + 手算」的方式验证（打开对应模型的 `config.json`，逐字段预测 `cfg` 的值，再与脚本输出比对）。

---

## 6. 本讲小结

- 配置解析分两层：`checkpoint_utils.load_checkpoint_config_dicts` 负责把检查点目录读成规整的 `(root_dict, llm_dict)`（处理多模态嵌套提升、RoPE 兼容、未注册类型回退）；`config.py` 再把 `llm_dict` 解析成强类型 `ModelConfig`。
- `ModelConfig` 是一个**扁平**的数据类，把 HF 散落各处的字段摊平成一组统一字段，覆盖架构、RoPE、层类型、MoE、投机解码、量化、张量并行，是后续所有 Python 篇的公共地基。
- 混合模型靠 `layer_types`（逐层标注 `attention`/`mamba`/`gdn`/`mlp`/`moe`）表达；Mamba 的 `conv_dim` 在 config 缺失时会**直接读 safetensors 的 conv1d 权重形状**来打破与 `n_groups` 的循环依赖。
- `QuantConfig` 用九个常量描述量化格式，`_parse_quant` 按「外部 `hf_quant_config.json` 优先于内嵌 `quantization_config`」的顺序判定，并用真实权重事实（`_effective_excluded_modules` / `_detect_modelopt_unquantized_linears`）修正排除列表。
- `module_quant_type` 是「某个 Linear 最终用什么精度」的**唯一真相来源**，合并 `excluded` / `layer_overrides` / 主类型三方信息。
- `has_qk_norm` 等「以权重事实为准」的探测策略，让新模型无需改代码即可被正确识别——这是整个 config 解析模块反复出现的设计哲学。

---

## 7. 下一步学习建议

本讲只解决了「**把检查点解析成一个 `ModelConfig`**」。拿到 `ModelConfig` 之后，接下来会发生什么？建议按下面的顺序继续：

1. **u2-l2 AutoModel 分发与模型注册表**：`AutoModel.from_pretrained` 如何用 `ModelConfig.model_type` 在注册表里选模型类，以及如何裁决 base/draft 变体（EAGLE/MTP/DFlash/Gemma4-MTP）。本讲的 `is_eagle3_draft` 等标志会在那一讲被真正「用起来」。
2. **u2-l4 检查点加载与权重重排**：`loader.load_weights` 如何依据 `QuantConfig` 决定每个 Linear 该加载成什么格式、`repacking.py` 如何把不同量化布局重排成运行时格式——那是本讲 `quant_type` 字段的直接消费方。
3. **u3-l1 量化包设计与配方**：如果你想反过来理解「`hf_quant_config.json` 是怎么被产出来的」，去读量化包。量化产出检查点，本讲解析检查点，两者是一对。
4. **提前关注**：`build_runtime_llm_config_dict`（在 `checkpoint_utils.py`）会把 `ModelConfig` 再「反向序列化」成一份 C++ 运行时能读的 `config.json`（带 `layer_types`、`kv_layer_configs`、`kv_cache_dtype` 等）。这条「正向解析 → 反向序列化」的链路在 u5 系列（C++ 运行时）会被 C++ 端读回来，届时你会更理解本讲这些字段为何如此设计。
