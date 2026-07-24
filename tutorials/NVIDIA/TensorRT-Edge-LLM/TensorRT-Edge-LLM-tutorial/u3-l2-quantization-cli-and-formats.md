# 量化 CLI 与支持的格式

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `tensorrt-edgellm-quantize` 的三个子命令（`llm` / `draft` / `qwen3-omni`）各自解决什么问题、面向什么模型。
- 看懂 CLI 上每一类量化参数（`--quantization` / `--lm_head_quantization` / `--kv_cache_quantization` / `--visual_quantization` / `--audio_quantization` / `--cp_quantization`）的含义、可选项与默认值，并理解 backbone 与 lm_head 为什么可以分别量化。
- 区分「量化 CLI 能**生产**的格式」与「导出侧 `config.py` 能**消费**的格式常量」这两个集合，并说出它们的差集从哪里来。
- 理解 fp8 / nvfp4 / mxfp8 / int4_awq / int8_sq 在位宽、是否量化激活、分组大小与显存收益上的权衡。
- 独立组装一条「nvfp4 backbone + nvfp4 lm_head」的 LLM 量化命令，以及一条投机解码 draft 量化命令，并能解释每个参数。

## 2. 前置知识

本讲承接 **u3-l1 量化包设计与配方**，假定你已经了解：

- **训练后量化（PTQ）**：在不重新训练的前提下，跑若干条前向（校准）统计激活的 `amax`，按 `scale = amax / maxbound` 算出每个量化器（quantizer）的缩放系数，把 FP16 权重/激活压成低精度。底层引擎是 NVIDIA **ModelOpt**（`modelopt.torch.quantization`，简称 `mtq`）。
- **配方（recipe / quant_cfg）**：一张描述「哪些量化器开启、用什么位宽/分组/轴」的字典。ModelOpt 用通配符（如 `*weight_quantizer`）匹配模块，并按「后写覆盖先写（last-writer-wins）」合并。
- **统一检查点**：量化阶段**只产出**一个 HuggingFace 风格的检查点目录（safetensors + config + `hf_quant_config.json`），**不**产出 ONNX、**不**产出 engine。它和导出（u2 单元）完全解耦。
- **`QuantConfig` 与 `module_quant_type`**（来自 u2-l1 / `config.py`）：导出侧用一个强类型 `QuantConfig` 描述整模型的量化方案，`module_quant_type(module_name, model_config)` 是「某个 Linear 最终用什么精度」的唯一真相来源。

一句话定位：u3-l1 讲的是**量化包内部如何编排 load→校准→量化→写出**；本讲讲的是**用户面向的命令行怎么用、有哪些旋钮、旋钮如何映射到底层配方与格式常量**。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `tensorrt_edgellm/scripts/quantize.py` | 量化 CLI 入口；定义三个子命令与公共参数，分发到底层函数。本讲的绝对主角。 |
| `tensorrt_edgellm/config.py` | 导出侧的量化格式常量（`QUANT_*`）、`QuantConfig` 数据结构与 `module_quant_type`，定义「可消费的格式全集」。 |
| `tensorrt_edgellm/quantization/quantization_configs.py` | 把 CLI 的方法名翻译成 ModelOpt 配方（`build_quant_config` 及若干 `_*_CFG_MAP`），是「CLI 旋钮」与「底层配方」的桥梁。 |
| `tensorrt_edgellm/quantization/quantize.py` | `quantize_and_export` 编排函数（`llm` 子命令的真正实现，含 MTP draft 的内部量化）。 |
| `tensorrt_edgellm/quantization/models/eagle3_draft.py` / `dflash_draft.py` | `draft` 子命令的两个后端：EAGLE3 / DFlash draft 的独立量化实现。 |
| `docs/source/user_guide/features/quantization.md` | 官方用量文档，命令示例的权威来源。 |
| `pyproject.toml` | 把命令名 `tensorrt-edgellm-quantize` 登记到 `scripts.quantize:main`。 |

## 4. 核心概念与源码讲解

### 4.1 量化 CLI 总览：三个子命令

#### 4.1.1 概念说明

`tensorrt-edgellm-quantize` 是量化包对外的唯一命令。它不直接做量化，而是扮演**调度员**：用 `argparse` 解析命令行，然后按「要量化的是什么模型」分发到三个互斥的子命令：

- `llm`：量化一个普通 LLM（含 VLM 的文本塔、视觉塔、音频塔、CodePredictor 等组件）。最常用。
- `draft`：量化一个**独立的**投机解码草稿模型（EAGLE3 或 DFlash）。这两种 draft 有自己单独的检查点目录。
- `qwen3-omni`：Qwen3-Omni-MoE 的专用路径，把 Thinker→Talker 链路在一次校准里联合 NVFP4 量化，普通 `llm` 表达不了这种依赖。

为什么需要这么多子命令？因为不同模型的**校准前向方式不同**：纯文本 LLM 喂 `input_ids`；VLM 视觉塔要喂「图片+问题」；ASR 要喂「音频+转写」；Omni 要把 Thinker 的隐藏状态喂给 Talker；draft 要把 base 模型的隐藏状态喂给 draft。CLI 用子命令把「选哪条校准链路」这件事显式化。

#### 4.1.2 核心流程

```text
tensorrt-edgellm-quantize <subcommand> [args]
        │
        ▼  argparse 解析 subcommand（required=True）
   ┌────┴────────────┬─────────────────┐
   ▼                 ▼                 ▼
  llm              draft            qwen3-omni
   │                 │                 │
   │ --model_dir     │ --base_model_dir  │ --model_dir
   │                 │ --draft_model_dir │ （隐式 NVFP4）
   ▼                 ▼                 ▼
quantize_and_      分两种：         quantize_qwen3_omni
export             EAGLE3 / DFlash
                   （按 dflash_config 自动判别）
```

三个子命令最终都落到底层函数，产出同一个东西：一个带量化元数据的统一 HF 检查点。

#### 4.1.3 源码精读

CLI 入口 `main()` 先建一个带 `required=True` 的子命令解析器，再逐个注册三个子命令。注意每个子命令只追加自己**独有**的位置/必选参数，公共旋钮由 `_add_common_args` 统一注入：

[scripts/quantize.py:L119-L132](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/quantize.py#L119-L132) 创建三个子解析器，`llm` 只额外要求 `--model_dir`，`draft` 额外要求 `--base_model_dir` 与 `--draft_model_dir`，`qwen3-omni` 自定义一套参数（见 4.1）。

[scripts/quantize.py:L169-L236](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/quantize.py#L169-L236) 是分发主体。`llm` 调 `quantize_and_export`；`draft` 先判别再二选一；`qwen3-omni` 调 `quantize_qwen3_omni`。注释也点明了为什么 Qwen3-Omni 需要专门路径：它要在单次 `mtq.quantize()` 的前向循环里把 Thinker→隐藏/文本投影→Talker 串起来，标准 `llm` 路径表达不了这条依赖。

文件顶部模块文档串给出三段最简用法，可以直接当备忘：

[scripts/quantize.py:L15-L37](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/quantize.py#L15-L37) 三类模型的最小命令模板。

命令名到入口的映射登记在 `pyproject.toml` 的 `[project.scripts]`，这和 u1-l4 讲过的机制一致：

[pyproject.toml:L54-L56](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/pyproject.toml#L54-L56) `tensorrt-edgellm-quantize = "tensorrt_edgellm.scripts.quantize:main"`。

#### 4.1.4 代码实践

**实践目标**：在不真正下载模型的前提下，用 `--help` 探明 CLI 的全部子命令与参数，建立第一手印象。

**操作步骤**（依据 `docs/source/user_guide/features/quantization.md` 的 Setup 小节）：

```bash
export EDGE_LLM_PATH=/path/to/TensorRT-Edge-LLM
cd $EDGE_LLM_PATH
export PYTHONPATH=$EDGE_LLM_PATH:$PYTHONPATH
tensorrt-edgellm-quantize --help           # 看到三个子命令
tensorrt-edgellm-quantize llm --help       # 看 llm 的参数（含公共参数）
tensorrt-edgellm-quantize draft --help     # 看 draft 的参数
tensorrt-edgellm-quantize qwen3-omni --help
```

**需要观察的现象**：
- `--help` 列出的子命令恰好是 `llm` / `draft` / `qwen3-omni`，对应 [scripts/quantize.py:L124-L165](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/quantize.py#L124-L165)。
- `llm --help` 与 `draft --help` 的公共部分完全一致（都来自 `_add_common_args`），但 `llm` 多一个 `--model_dir`，`draft` 多 `--base_model_dir` / `--draft_model_dir`。
- 不带子命令直接运行会报错（`required=True`）。

**预期结果**：三份帮助文本各列出一组参数，无 GPU 也能跑（`--help` 不触发模型加载）。

#### 4.1.5 小练习与答案

**Q1**：为什么 `qwen3-omni` 不复用 `llm` 子命令？
**A**：Omni-MoE 需要在一次校准前向里串起 Thinker（多模态）→隐藏/文本投影→Talker，且 NVFP4 是当前唯一验证过的配方；标准 `llm` 路径无法表达这种组件间依赖，故专门开一条命令。见 [scripts/quantize.py:L134-L140](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/quantize.py#L134-L140) 注释。

**Q2**：`draft` 子命令为什么需要 `--base_model_dir`？
**A**：draft 自身不存完整的 base 权重，校准时需要用 base 跑前向产出「目标隐藏状态」喂给 draft；某些 draft（如 DFlash）还在自身没有 `lm_head` 时把 base 当作 lm_head 兜底。见 [docs/.../quantization.md:L119-L125](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/user_guide/features/quantization.md#L119-L125)。

### 4.2 公共参数与按组件拆分的量化选项

#### 4.2.1 概念说明

`_add_common_args` 是理解整张旋钮表的关键。它的设计思想是**按组件（component）分别量化**：一个多模态模型里有文本 backbone、lm_head、KV cache、视觉塔、音频塔、CodePredictor 等不同部件，它们对精度的敏感度不同，因此 CLI 给每个部件一个独立的 `--*_quantization` 旋钮。

这带来一个重要的能力：**backbone 和 lm_head 可以用不同精度**（混合精度）。例如 backbone 用 NVFP4 压显存，而 lm_head 因为直接影响下一个 token 的 logits，可以单独保留更稳的 fp8。

不是所有部件都开放所有格式——开放范围由「该部件每种格式是否经过端到端精度验证」决定，体现为 `argparse` 的 `choices`。

#### 4.2.2 核心流程

公共参数的开放范围（来自 `_add_common_args` 的 `choices`）：

| 旋钮 | 含义 | 可选值（CLI 暴露） | 默认 |
|---|---|---|---|
| `--quantization` | backbone（主干 Linear）量化方法 | `fp8` `int4_awq` `nvfp4` `mxfp8` `int8_sq` | None（不量化） |
| `--lm_head_quantization` | lm_head 量化方法 | `fp8` `int4_awq` `nvfp4` `mxfp8` | None |
| `--kv_cache_quantization` | KV cache 量化方法 | `fp8` | None |
| `--visual_quantization` | 视觉塔量化方法 | `fp8` | None（保持 fp16） |
| `--audio_quantization` | 音频塔量化方法 | `fp8` | None |
| `--cp_quantization` | Qwen3-Omni CodePredictor 方法 | `fp8` | None |
| `--dtype` | 加载 dtype | `fp16` | fp16 |
| `--text_dataset` / `--image_dataset` / `--audio_dataset` | 校准数据集名 | `cnn_dailymail`/`wikitext`、`mmmu`、`librispeech` | 见默认 |
| `--num_samples` | 校准样本数 | 整数 | 512 |

注意几个约束关系（都写在代码注释里）：

- **lm_head 比 backbone 少一个 `int8_sq`**：SmoothQuant 是 W8A8（权重+激活都量化），lm_head 的输入 logits 分布不适合，故不开放。
- **KV/视觉/音频/CP 只开 `fp8`**：更低位的配方尚未在该部件上完成端到端精度验收（视觉/音频分别要 VLM eval / WER 验证），所以「推迟暴露」。
- **CP 的 `down_proj` 强制不量化**：CodePredictor 的 `down_proj` 涉及 FP32 MLP 的 WAR（见 `modeling_code_predictor.py`），量化会破坏数值范围。
- 校准数据集「**按名选取**」：未知名字会直接失败并指向自定义指南，不会静默回退。

#### 4.2.3 源码精读

公共参数的集中定义：

[scripts/quantize.py:L48-L116](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/quantize.py#L48-L116) `_add_common_args`，逐个 `add_argument` 的 `choices=` 即上表「可选值」的权威来源。例如 `--quantization` 的 choices 是 `["fp8", "int4_awq", "nvfp4", "mxfp8", "int8_sq"]`（[L50-L54](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/quantize.py#L50-L54)），而 `--lm_head_quantization` 是 `["fp8", "int4_awq", "nvfp4", "mxfp8"]`（[L55-L58](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/quantize.py#L55-L58)）。

这些方法名在 `build_quant_config` 里被翻译成 ModelOpt 配方。两张核心映射表决定了「能组合出什么」：

[quantization_configs.py:L303-L316](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/quantization_configs.py#L303-L316) `_BACKBONE_CFG_MAP`（5 种）与 `_LM_HEAD_CFG_MAP`（4 种）。正是这两张表，使得「任意 backbone × 任意 lm_head」都能合法组合（如 nvfp4 backbone + fp8 lm_head）。

`build_quant_config` 的合并顺序也值得记住：先深拷贝 backbone 配方 → 删补 lm_head → 叠 KV → 默认禁用所有非 LLM 组 → 再按用户请求逐个重新启用 visual/audio/cp。这是 u3-l1 讲过的「深拷贝基底→删补→禁用→叠加覆盖」有序合并：

[quantization_configs.py:L435-L550](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/quantization_configs.py#L435-L550) `build_quant_config` 全文。

#### 4.2.4 代码实践

**实践目标**：组装一条「nvfp4 backbone + nvfp4 lm_head + fp8 KV cache」的命令，并追踪它会被翻译成哪张配方。

**操作步骤**（命令模板直接取自官方文档的「Enable FP8 KV Cache」小节）：

```bash
tensorrt-edgellm-quantize llm \
  --model_dir Qwen/Qwen3-8B \
  --output_dir /tmp/qwen3_nvfp4_fp8kv \
  --quantization nvfp4 \
  --lm_head_quantization nvfp4 \
  --kv_cache_quantization fp8
```

**需要观察的现象 / 预期结果**（**待本地验证**，需要 CUDA 与联网下载模型）：
- 程序先 `--model_dir` 加载模型，打印 `Text calibration dataset: cnn_dailymail`，进入 `Calibrating` 进度条（默认 512 条文本）。
- 量化完成后打印 `mtq.print_quant_summary` 的量化摘要，可见 backbone 的 weight/input quantizer 与 lm_head 的 quantizer 都已启用，KV BMM quantizer 也启用。
- 输出目录 `/tmp/qwen3_nvfp4_fp8kv` 下会出现 `model.safetensors`、`config.json`、`hf_quant_config.json`、tokenizer 文件等，**没有** ONNX。

**纯阅读型追踪**（无 GPU 也可做）：在 `build_quant_config("nvfp4", "nvfp4", "fp8")` 这条调用里，依次命中 `_BACKBONE_CFG_MAP["nvfp4"]` → `NVFP4_LM_HEAD` → `mtq.FP8_KV_CFG + FP8_ATTN`，最终 `quant_cfg` 同时覆盖了主干 NVFP4、lm_head NVFP4 与 KV/注意力 BMM 的 FP8。

#### 4.2.5 小练习与答案

**Q1**：为什么 `--visual_quantization` 目前只暴露 `fp8`？
**A**：视觉塔的更低精配方（NVFP4/MXFP4/INT4）尚未完成 VLM 端到端精度验收，故在 CLI 层面「推迟暴露」，避免用户拿到一个未验证的产物。见 [scripts/quantize.py:L59-L67](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/quantize.py#L59-L67) 注释。

**Q2**：若只给 `--visual_quantization fp8` 而不给 `--quantization`，视觉塔会被量化吗？
**A**：会。backbone 不量化时 `build_quant_config` 仍会按 `_VISUAL_CFG_MAP["fp8"]` 单独为视觉塔叠加 FP8 配方（前提是用了带 `--image_dataset` 的多模态校准，否则视觉塔 scale 未初始化）。主干则保持 fp16。

### 4.3 量化格式常量：可生产 vs 可消费

#### 4.3.1 概念说明

这是本讲最容易被忽略、却最容易踩坑的一点：**CLI 能生产的格式** ≠ **导出侧能消费的格式**。两者是不同模块维护的两张表：

- **CLI 能生产（`scripts/quantize.py` 的 `choices`）**：`fp8` `int4_awq` `nvfp4` `mxfp8` `int8_sq`（5 种 backbone 方法，加 lm_head 的 4 种）。
- **导出侧能消费（`config.py` 的 `QUANT_*` 常量）**：共 9 个常量，多出 `int4_awq_modelopt`、`int4_gptq`、`mixed_precision`。

差集从哪来？来自**外部预量化检查点**：GPTQ 检查点由社区工具产出，本包**只读取、不生产**（README 明确「GPTQ checkpoints are loaded as pre-quantized checkpoints; this package does not create GPTQ models」）；`int4_awq_modelopt` 是 ModelOpt 预打包的 uint8 布局；`mixed_precision` 来自逐层混合的 `hf_quant_config`。所以导出器必须认识它们，但量化 CLI 不会生成它们。

#### 4.3.2 核心流程

`config.py` 顶部的九个常量是整条导出链的「量化格式词典」：

| 常量 | 字符串值 | 含义 | 位宽/分组 | 谁产出 |
|---|---|---|---|---|
| `QUANT_FP16` | `fp16` | 不量化（fp16/bf16） | — | 原始检查点 |
| `QUANT_FP8` | `fp8` | FP8 E4M3 per-tensor 静态量化 | 8 bit | CLI |
| `QUANT_MXFP8` | `mxfp8` | MXFP8 per-block FP8 | 8 bit，block=32 | CLI |
| `QUANT_NVFP4` | `nvfp4` | NVFP4 per-group + FP8 组缩放 | 4 bit，group=16 | CLI |
| `QUANT_INT4_AWQ` | `int4_awq` | AWQ INT4 分组（列打包 int32） | 4 bit，group=128（W4A16） | CLI / 外部 |
| `QUANT_INT4_AWQ_MODELOPT` | `int4_awq_modelopt` | ModelOpt 预打包 uint8 `[out//2, in]` | 4 bit | ModelOpt |
| `QUANT_INT4_GPTQ` | `int4_gptq` | GPTQ INT4 分组 | 4 bit | 外部工具 |
| `QUANT_INT8_SQ` | `int8_sq` | INT8 SmoothQuant W8A8 per-channel | 8 bit（权重+激活） | CLI |
| `QUANT_MIXED` | `mixed_precision` | 逐层混合（主导 algo + layer_overrides） | 混合 | `hf_quant_config` |

各格式的**精度/显存权衡**直觉（结合位宽与是否量化激活）：

- **fp8**：权重与激活都 8-bit，相比 fp16 约省一半权重显存，精度损失小，是较新平台（Thor/Spark/Blackwell）的主力。
- **nvfp4**：权重 4-bit（NVFP4 + FP8 组 scale），相比 fp16 约省到 1/4 权重显存，最适合显存极紧的边缘设备，需要 Blackwell 级 FP4 支持。
- **mxfp8**：8-bit 但带 per-block scale（block=32），精度介于 fp8 与更低位之间。
- **int4_awq**：**仅权重量化**（W4A16，激活仍是 fp16），权重 4-bit（group=128），省显存的同时激活路径保持高精度。
- **int8_sq**：权重与激活都 8-bit（SmoothQuant 平滑激活异常值），适合较老 GPU（Ampere/Orin），但 lm_head 不开放。

> 量化核心公式（承接 u3-l1，校准算 scale）：行内记作 \( \text{scale} = \text{amax} / \text{maxbound} \)，反量化时 \( x \approx \text{scale}\cdot q \)。分组越小（如 NVFP4 的 16），scale 越贴合局部数值范围，精度越高但元数据开销越大。

#### 4.3.3 源码精读

九个常量的定义与逐行注释就在 `config.py` 顶部，注释里直接写明了每种格式的位宽/打包方式：

[config.py:L66-L77](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L66-L77) `QUANT_*` 常量；模块文档 [config.py:L29-L38](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L29-L38) 也列了「Supported quantization formats」。

`QuantConfig` 数据结构把上述常量组织成一份方案：主导类型 `quant_type`、`group_size`、`kv_cache_quant`、`excluded`、`layer_overrides`、`is_mixed_precision`：

[config.py:L351-L390](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L351-L390) `QuantConfig`。

`module_quant_type` 决定「某个模块最终用什么 Linear 类」，是 backbone 与 lm_head 能分别量化的落地点——它优先看 `excluded`，再看 `layer_overrides` 里是否有该模块的逐层覆盖：

[config.py:L393-L419](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L393-L419) `module_quant_type`。

字符串到常量的翻译规则（`MIXED_PRECISION`、`FP8_PB`→MXFP8 等判别）：

[config.py:L1819-L1836](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L1819-L1836) `_algo_to_quant_type`。

「可消费全集」的权威清单也写在 AGENTS.md 里：`fp16, fp8, nvfp4, int4_awq, int4_awq_modelopt, int4_gptq, int8_sq, mixed_precision`，注意它比 CLI 多出 `int4_awq_modelopt` / `int4_gptq` / `mixed_precision`。

#### 4.3.4 代码实践

**实践目标**：手工对齐「CLI choices」与「`QUANT_*` 常量」两张表，找出差集并解释。

**操作步骤**：
1. 打开 [scripts/quantize.py:L50-L58](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/quantize.py#L50-L58)，抄下 `--quantization` 与 `--lm_head_quantization` 的 choices。
2. 打开 [config.py:L66-L77](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L66-L77)，抄下 9 个常量。
3. 求差集，对每个「只在导出侧」的格式，去 `docs/.../quantization.md` 的 Notes 找原因。

**预期结果（差集表）**：

| 仅导出侧有的格式 | 来源 |
|---|---|
| `int4_gptq` | 社区 GPTQ 检查点，本包只读不写 |
| `int4_awq_modelopt` | ModelOpt 预打包 uint8 布局 |
| `mixed_precision` | 逐层混合的 `hf_quant_config` |

参考 [docs/.../quantization.md:L150-L154](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/user_guide/features/quantization.md#L150-L154) 的 Notes。

#### 4.3.5 小练习与答案

**Q1**：用户跑 `--quantization nvfp4 --lm_head_quantization fp8`，导出侧的 `QuantConfig.quant_type` 与 `layer_overrides` 大致是什么？
**A**：主导类型 `quant_type = QUANT_NVFP4`（backbone 占多数），`layer_overrides` 里 `lm_head` 映射到 `QUANT_FP8`。`module_quant_type("lm_head", cfg)` 会优先返回 `layer_overrides["lm_head"] = fp8`，于是 lm_head 走 FP8Linear 而非 NVFP4Linear。

**Q2**：为什么 `int4_gptq` 不在 CLI choices 里，却必须在导出侧支持？
**A**：很多社区模型直接以 GPTQ 格式发布检查点；本包不重新生产 GPTQ，但 `tensorrt_edgellm` 导出时必须能加载并 repack 它，否则这些模型无法走 Edge-LLM 流水线。

### 4.4 投机解码 draft 量化：draft 子命令与 MTP 的区别

#### 4.4.1 概念说明

投机解码（speculative decoding）用一个小 draft 模型提议若干 token，再用大 base 模型批量验证，从而减少大模型的串行前向次数。draft 模型同样需要量化。这里有一个**极易混淆**的分叉：

- **EAGLE3 / DFlash**：draft 是**独立的检查点目录**（有自己的权重），用 **`draft` 子命令**量化，需要同时给 `--base_model_dir`（校准用）和 `--draft_model_dir`。
- **MTP（Multi-Token Prediction，Qwen3.5）**：draft **不是独立检查点**，而是从 base 检查点里派生（base config 里带 `mtp_num_hidden_layers`），所以**不走 `draft` 子命令**，而是在 **`llm` 子命令内部自动完成**——检测到 `mtp_num_hidden_layers` 时，先于 base 量化 MTP draft，再把它和 base 一起写进统一检查点。

这个区别决定了你「组装 draft 量化命令」时该用哪条路径。

#### 4.4.2 核心流程

```text
要量化投机解码的 draft？
        │
   draft 有独立检查点目录？
        ├─ 是 → tensorrt-edgellm-quantize draft
        │         └─ 读 draft 的 config.json::dflash_config？
        │             ├─ 有 → DFlash 流程（默认 nvfp4，禁用 KV 量化）
        │             └─ 无 → EAGLE3 流程（默认 fp8）
        │
        └─ 否（Qwen3.5 MTP，draft 派生自 base）
              → tensorrt-edgellm-quantize llm
                 （检测到 mtp_num_hidden_layers 时，内部先量化 MTP draft）
```

两种 draft 后端的关键差异：

| 维度 | EAGLE3 | DFlash |
|---|---|---|
| 默认 `--quantization` | `fp8` | `nvfp4` |
| 校准输入 | base 的 3 层隐藏状态拼接 | base 多层隐藏状态 + mask 提议序列 |
| `lm_head` 来源 | draft 自带 | 缺失时从 base 兜底 |
| 特殊约束 | — | target-hidden 投影 `fc` 强制不量化；KV 量化未验证会报错 |

#### 4.4.3 源码精读

`draft` 子命令的分发与自动判别：

[scripts/quantize.py:L187-L218](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/quantize.py#L187-L218) 先用 `_is_dflash_draft(args.draft_model_dir)` 判别，再分别调 `quantize_and_export_dflash_draft`（注意 DFlash 默认 `nvfp4`）或 `quantize_and_export_draft`（EAGLE3 默认 `fp8`）。

判别函数：读 draft 的 `config.json`，看是否有 `dflash_config` 字段：

[scripts/quantize.py:L239-L250](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/quantize.py#L239-L250) `_is_dflash_draft` 与 `_validate_dflash_quant_args`（DFlash 不允许 `--kv_cache_quantization`）。

两个后端的函数签名（注意默认值不同，且都不接受 visual/audio/cp）：

[eagle3_draft.py:L197-L209](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/models/eagle3_draft.py#L197-L209) `quantize_and_export_draft`（`quantization="fp8"`）。

[dflash_draft.py:L387-L399](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/models/dflash_draft.py#L387-L399) `quantize_and_export_dflash_draft`（`quantization="nvfp4"`）；DFlash 的 `fc` 投影强制 FP32 路径见 [dflash_draft.py:L547-L559](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/models/dflash_draft.py#L547-L559)。

对照之下，MTP draft **不**走这两个函数，而是藏在 `llm` 子命令的 `quantize_and_export` 里——检测到 `mtp_num_hidden_layers > 0` 时，先调 `quantize_mtp_from_base` 把 draft 量化好，再量化 base：

[quantize.py（量化包）:L654-L683](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/quantize.py#L654-L683) MTP draft 在 base 之前量化。

官方文档对 EAGLE3 / DFlash 两条 draft 命令的范例：

[docs/.../quantization.md:L99-L118](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/user_guide/features/quantization.md#L99-L118) EAGLE3（fp8）与 DFlash（nvfp4 + lm_head）范例。

#### 4.4.4 代码实践

**实践目标**：组装一条 EAGLE3 draft 量化命令并逐参数解释。

**操作步骤**（模板取自官方文档）：

```bash
tensorrt-edgellm-quantize draft \
  --base_model_dir /path/to/base_model \
  --draft_model_dir /path/to/eagle3_draft \
  --output_dir /tmp/eagle3_draft_fp8 \
  --quantization fp8
```

**参数解释**：
- `draft`：子命令，表示量化一个独立 draft 检查点（此例会被判别为 EAGLE3，因为 draft 的 `config.json` 没有 `dflash_config`）。
- `--base_model_dir`：base 模型目录。**仅用于校准**：跑 base 前向产出隐藏状态喂给 draft，并不被量化、也不被写出。
- `--draft_model_dir`：要量化的 draft 检查点目录；它的 `config.json::dflash_config` 有无决定走 EAGLE3 还是 DFlash。
- `--output_dir`：量化后统一 draft 检查点的写出目录（含 `model.safetensors` + `config.json` + `hf_quant_config.json`）。
- `--quantization fp8`：draft backbone 用 fp8。省略时 EAGLE3 默认就是 `fp8`，DFlash 默认 `nvfp4`。
- 校准数据集默认 `cnn_dailymail`，`--num_samples` 默认 512（由公共参数注入）。

**预期结果**（**待本地验证**）：输出目录里 draft 的 Linear 权重被替换为 FP8 打包格式，`hf_quant_config.json` 记录 `quant_algo=FP8`；base 不出现在输出里。

#### 4.4.5 小练习与答案

**Q1**：用户想量化一个 Qwen3.5 MTP 的 draft，该用哪个子命令？
**A**：用 `llm` 子命令（`tensorrt-edgellm-quantize llm --model_dir <base> --quantization nvfp4 ...`）。MTP draft 派生自 base 检查点，没有独立目录，会在 `quantize_and_export` 内部被检测到 `mtp_num_hidden_layers` 后自动先于 base 量化。**不**能用 `draft` 子命令。

**Q2**：为什么 DFlash 的 `fc` 投影不能量化？
**A**：`fc` 把 base 多层隐藏状态投影成 target delta，对精度极敏感，运行时需要 FP32 累加路径；量化会破坏数值范围，因此量化阶段显式禁用其 quantizer，并在 `hf_quant_config.json` 的 `exclude_modules` 里写入 `fc`。见 [dflash_draft.py:L547-L559](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/models/dflash_draft.py#L547-L559) 与 [docs/.../quantization.md:L119-L125](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/user_guide/features/quantization.md#L119-L125)。

## 5. 综合实践

把本讲两条主线（LLM 全 NVFP4 量化 + 投机解码 draft 量化）串起来。**目标**：为「base + EAGLE3 draft」的投机解码系统准备一整套量化检查点，准备交给后续 `tensorrt-edgellm-export`。

**任务一：base 模型 nvfp4 backbone + nvfp4 lm_head 量化**

```bash
# 1. 量化 base（nvfp4 backbone + nvfp4 lm_head）
tensorrt-edgellm-quantize llm \
  --model_dir /path/to/base_model \
  --output_dir /tmp/base_nvfp4 \
  --quantization nvfp4 \
  --lm_head_quantization nvfp4
```

逐参数说明：`llm` 选 LLM 量化链路；`--quantization nvfp4` 把主干 Linear 压成 4-bit（group=16，FP8 组 scale），最大化省显存；`--lm_head_quantization nvfp4` 让 lm_head 也走 NVFP4（二者可以不同，这里为极致省显存取一致）；`--output_dir` 写出统一检查点；校准默认 `cnn_dailymail` 512 条。

**任务二：EAGLE3 draft 的 fp8 量化**

```bash
# 2. 量化 EAGLE3 draft（fp8），base 仅用于校准
tensorrt-edgellm-quantize draft \
  --base_model_dir /path/to/base_model \
  --draft_model_dir /path/to/eagle3_draft \
  --output_dir /tmp/eagle3_draft_fp8 \
  --quantization fp8
```

逐参数说明：`draft` 选独立 draft 量化链路，自动判别为 EAGLE3；`--base_model_dir` 提供 base 前向的隐藏状态作校准输入（base 不被量化/写出）；`--draft_model_dir` 是真正要量化的 draft；`--quantization fp8` 给 draft 用 fp8（draft 体量小、追求接受率，常用 fp8 而非更激进的 nvfp4）；写出统一 draft 检查点。

**串联验证（阅读型，无 GPU 也可做）**：在 `scripts/quantize.py` 里分别找到任务一命中 [L169-L186](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/quantize.py#L169-L186)（`quantize_and_export`）与任务二命中 [L204-L218](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/quantize.py#L204-L218)（`quantize_and_export_draft`）的代码路径，确认两条命令最终落到不同后端函数，但产物都是「带 `hf_quant_config.json` 的统一检查点」，可直接被 `tensorrt-edgellm-export` 消费。

> 若手头没有 GPU/模型，至少把两条命令组装完整、逐参数解释清楚，并标注「待本地验证」。

## 6. 本讲小结

- `tensorrt-edgellm-quantize` 有三个子命令：`llm`（普通 LLM/多模态各组件）、`draft`（独立 EAGLE3/DFlash draft）、`qwen3-omni`（Omni-MoE 联合 NVFP4）。
- 公共参数按**组件**拆分（backbone / lm_head / kv / visual / audio / cp），各自只暴露**经验证**的格式；backbone 与 lm_head 可分别量化实现混合精度。
- **CLI 能生产的格式**（`fp8`/`int4_awq`/`nvfp4`/`mxfp8`/`int8_sq`）是**导出侧 `QUANT_*` 常量全集的子集**；多出的 `int4_gptq`/`int4_awq_modelopt`/`mixed_precision` 来自外部预量化检查点，本包只读不写。
- 格式权衡的核心维度：位宽、是否量化激活（如 int4_awq 是 W4A16 仅权重，int8_sq 是 W8A8）、分组大小（nvfp4=16、awq=128、mxfp8 block=32）。
- 投机解码 draft 量化有两条路径：EAGLE3/DFlash 走 `draft` 子命令（按 `dflash_config` 自动判别，默认分别 fp8/nvfp4）；Qwen3.5 MTP 走 `llm` 子命令（内部检测 `mtp_num_hidden_layers` 自动先量化 draft）。
- 方法名经 `build_quant_config` 翻译成 ModelOpt 配方，经 `_BACKBONE_CFG_MAP`/`_LM_HEAD_CFG_MAP` 决定可组合范围；导出侧再用 `module_quant_type` 把方案落到每个 Linear 类。

## 7. 下一步学习建议

- 想知道量化后的权重**张量布局**与 sidecar 细节，进入 **u3-l3 量化权重格式与 sidecar**，看 AWQ 列打包、NVFP4 分组、FP8 embedding sidecar 如何在 `repacking.py` 里被重排为运行时格式。
- 想把量化检查点真正变成 ONNX，回到 **u2-l5 ONNX 导出**，看 `QuantConfig` 如何驱动 `make_linear` 选 Linear 类、以及量化算子如何进入自定义 ONNX 域。
- 想理解 draft 量化产物在导出侧如何被识别为 EAGLE3/MTP/DFlash 变体，看 **u2-l2 AutoModel 分发与模型注册表** 的 `_resolve_model_variant` 与权重 key remap。
- 若你要为 QuantConfig 新增一种格式，应同时改 `config.py` 常量与 `_algo_to_quant_type`、并在 `build_quant_config` 的映射表与 CLI `choices` 里同步登记——这是本讲两张表的联动点。
