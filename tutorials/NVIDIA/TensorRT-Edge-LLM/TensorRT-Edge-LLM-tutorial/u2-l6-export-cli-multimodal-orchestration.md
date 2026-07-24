# 导出 CLI 与多模态组件编排

## 1. 本讲目标

上一篇（u2-l5）讲清楚了「如何把一个已经建好、灌好权重的 `nn.Module` 经 dynamo 导出成一张下游 C++ 构建器能消费的 ONNX 图」。但那只解决了**单个组件**的导出问题。

一个真实的多模态检查点往往不止一个组件：一个 Qwen3-VL 同时有「LLM 主干」和「视觉编码器」；一个 Qwen3-Omni 还多出「音频编码器」「Talker 解码器」「CodePredictor」「Code2Wav 声码器」。于是自然产生三个问题：

1. **导出谁**？—— 一个检查点到底要导出哪些组件？
2. **放哪儿**？—— 每个组件的 ONNX 写到输出目录的哪个子目录？
3. **带什么**？—— 每个 ONNX 旁边还要附带哪些「sidecar 文件」（config、tokenizer、chat template、权重）才能被 C++ 运行时正确加载？

本讲就是回答这三个问题的「编排层」。学完后你应该能够：

- 理解 `tensorrt-edgellm-export` 这条命令的入口、参数，以及它用一张「阶段表（stages）」驱动全部组件导出的设计。
- 记住 `_VLM_MODEL_TYPES` / `_AUDIO_MODEL_TYPES` / `_CODE2WAV_MODEL_TYPES` / `_ACTION_MODEL_TYPES` / `_LLM_COMPONENTS` 这些分类常量如何按 `model_type` 决定导出哪些组件。
- 掌握一次导出会产出哪些 ONNX 子图与 sidecar（`config.json` / `embedding.safetensors` / tokenizer 文件 / `processed_chat_template.json` / 外部权重文件）。
- 了解 `export_encoder.py` 如何用「家族注册表」把不同的视觉/音频编码器路由到对应的 `build_*` 函数。

## 2. 前置知识

本讲承接 u2-l5，**默认你已经知道**：

- `export_onnx()` 会用 `torch.onnx.export(dynamo=True)` 把一个 `CausalLM` 导出成 `model.onnx` + `model.onnx.data`，并做一串 TRT 兼容性后处理。
- ONNX 导出的真值最终交给 C++ 插件/算子（`trt::` / `trt_edgellm::` 自定义域）。
- 一个 `CausalLM` 的权重来自 u2-l2/u2-l4 描述的 `AutoModel.from_pretrained` + `load_weights` 链路。

你还需要理解三个通俗概念：

- **组件（component）**：导出流水线里一个相对独立的可导出单元，如 `thinker`（LLM 主干）、`visual`（视觉编码器）、`audio`、`talker`、`code_predictor`、`code2wav`、`action`。
- **model_type**：检查点 `config.json` 里的 `"model_type"` 字段，是整个编排的「主键」。导出器第一件事就是读它。
- **sidecar（伴生文件）**：ONNX 图之外、和它放在同一目录、被 C++ 运行时在加载引擎时一起读取的配套文件。典型的有 `config.json`、`embedding.safetensors`、`tokenizer.json`、`processed_chat_template.json`。

一句话总结本讲定位：**`scripts/export.py` 是「项目经理」，它不亲自做 dynamo 导出，而是根据 `model_type` 决定调哪些「工种」（各 `_export_*` 函数）、把活派到哪个子目录、并确保每个子目录凑齐 C++ 运行时要的全部 sidecar。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tensorrt_edgellm/scripts/export.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py) | 导出 CLI 的全部逻辑：参数解析、组件分类、阶段表编排、各 `_export_*` 工种、sidecar 写入。本讲的「主角」。 |
| [tensorrt_edgellm/onnx/export_encoder.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export_encoder.py) | 视觉/音频编码器的统一导出入口；用「家族注册表」把 `model_type` 路由到对应的 `build_*` 函数，复用与 LLM 同一套 dynamo 底座。 |
| [tensorrt_edgellm/external_weights.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/external_weights.py) | 把选定的 ONNX 权重初始化器「外置」成 safetensors sidecar 的机制（`--externalize-weights`），用于把超大权重从 ONNX 图里抽出来单独存放。 |
| [tensorrt_edgellm/onnx/export.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export.py) | 提供 `export_onnx()`，是所有 LLM 类组件（thinker/talker/code_predictor/draft）最终调用的导出底座，并负责写 LLM 的核心 sidecar。 |
| [tensorrt_edgellm/checkpoint/checkpoint_utils.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py) | `write_runtime_artifacts()` 在这里：负责写 LLM 目录下的 `config.json` / `embedding.safetensors` / tokenizer 拷贝 / chat template。 |
| [tensorrt_edgellm/chat_template.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/chat_template.py) | `process_chat_template()` 把 Jinja chat 模板「预编译」成 C++ 运行时能直接消费的 `processed_chat_template.json`。 |

---

## 4. 核心概念与源码讲解

### 4.1 export CLI：入口、参数与「阶段表」编排

#### 4.1.1 概念说明

`scripts/export.py` 的 `main()` 既是命令行入口（被 `pyproject.toml` 的 `[project.scripts]` 登记为 `tensorrt-edgellm-export`，见 u1-l4），也是整个导出过程的「总调度」。

它的核心设计是**「阶段表（stages）」**：把所有可能的导出工种列成一张三元组表 `(enabled, component_name, callable)`，其中：

- `enabled`：一个布尔表达式，综合「这个 `model_type` 是否支持该组件」「用户有没有用 `--skip-*` 跳过」「用户有没有用 `--components` 白名单限制」三件事。
- `component_name`：组件名，用于日志和最终汇总。
- `callable`：真正干活的 `_export_*` 函数，接收「算好的输出子目录」作为参数。

这种设计的好处是：**新增一个组件，只需要在阶段表里加一行 + 写一个 `_export_xxx`，不必改动调度主循环。** 主循环只有一句：

```python
for enabled, component, fn in stages:
    if enabled:
        fn(os.path.join(args.output_dir, _layout_for(model_type, component)))
```

#### 4.1.2 核心流程

`main()` 的执行顺序可以用下面的伪代码概括：

```
1. 解析命令行参数 (argparse)
2. 解析 model_dir（本地目录 or HF repo id → snapshot_download）
3. 读 config.json → 取出 model_type
4. 一系列互斥/依赖校验（--eagle-base / --mtp / --dflash-* 不能同时开等）
5. 定义两个「惰性加载器」：_get_weights()、_get_model_config()
   （只在真正需要权重/ModelConfig 的工种运行时才加载，省内存）
6. 构造 stages 阶段表（10 个候选工种）
7. 打印「计划导出哪些组件」的摘要日志
8. for 每个 enabled 的 stage: 调用对应 _export_*(out_dir)
9. 打印最终汇总（每个子目录的 model.onnx 大小 + sidecar 列表）
```

关键直觉有两点：

- **惰性加载（lazy load）**：权重和 `ModelConfig` 直到第一个需要它们的工种运行才被读入。对于 `--skip-llm` 只导视觉编码器的场景，可以完全不碰 LLM 主干权重。
- **`--components` 是白名单**：空字符串表示「不限制，导出检查点支持的全部组件」；非空则只导出列出的组件。这是「重跑单个阶段」（比如只刷新 CodePredictor）的官方手段。

#### 4.1.3 源码精读

入口先固定 umask、搭好 argparse，并登记全部参数（包括六个 `--skip-*`、`--components`、投机解码开关、`--externalize-weights`、`--tp-size` 等）：

[scripts/export.py:2373-2387](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L2373-L2387) —— `main()` 开头固定 `os.umask(0o022)`（保证导出产物在容器间可读），并构造 argparser。`--components` 的 help 文本明确列出全部合法值：`thinker, mtp_draft, talker, code_predictor, visual, audio, code2wav, action`。

参数解析后是一长串互斥校验，例如「`--eagle-base` 与 `--mtp` 不能同时开」「Gemma4 的 `--mtp` 必须配 `--mtp-draft-dir`」。这些校验把「不可能的组合」在昂贵的导出开始之前就挡掉：

[scripts/export.py:2589-2639](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L2589-L2639) —— 典型校验，如 `--eagle-base and --mtp cannot be enabled together`。Gemma4 MTP 还会在这里调用 `_validate_gemma4_mtp_pair()` 预校验 target/assistant 检查点的词表、hidden_size、KV 共享映射是否自洽。

接着是两个惰性加载闭包：

[scripts/export.py:2654-2675](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L2654-L2675) —— `_get_weights()` 与 `_get_model_config()` 都是「首次调用才加载、之后复用」的模式。`_get_weights` 调 `_load_all_weights()` 把目录里所有 `*.safetensors` 拼成一个扁平 dict。

然后是本讲的「心脏」——阶段表：

[scripts/export.py:2712-2769](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L2712-L2769) —— 完整的 `stages` 列表。每一项的第一字段是「是否启用」的布尔表达式，把「分类常量判断 + `--skip-*` + `--components` 白名单」三者用 `and` 串起来。注意 `audio` 阶段还额外检查 `_checkpoint_audio_config(config) is not None`，因为有些 `gemma4` 检查点虽然有 audio 家族但 `audio_config` 为 `null`（纯 dense 模型），此时要跳过。

主循环极简：

[scripts/export.py:2810-2814](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L2810-L2814) —— 遍历阶段表，对启用的项调用 `fn(out_dir)`，其中 `out_dir = output_dir + _layout_for(model_type, component)`。子目录路径完全由 `_layout_for` 决定（见 4.2）。

最后打印汇总，把每个子目录下的 `model.onnx` 大小和已知 sidecar 文件列出来：

[scripts/export.py:2831-2856](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L2831-L2856) —— `_SIDECARS` 元组列出全部已知 sidecar 文件名，主循环遍历每个组件子目录、检查这些文件是否存在并打印大小。这是「一次导出到底产出了什么」的可读清单。

#### 4.1.4 代码实践

> **实践目标**：不实际运行导出（那需要 GPU 和真实检查点），而是通过阅读源码，回答「`--components` 白名单如何改变阶段表的启用状态」。

**操作步骤**：

1. 打开 [scripts/export.py:2706-2707](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L2706-L2707)，读 `_allow()` 的定义：`return not requested_components or component in requested_components`。
2. 在阶段表 [scripts/export.py:2712-2769](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L2712-L2769) 里，观察每个阶段的启用条件末尾都有 `and _allow("xxx")`。
3. 想象运行 `tensorrt-edgellm-export /ckpt /out --components code_predictor`（假设是 Qwen3-Omni）。

**需要观察的现象 / 预期结果**：

- `requested_components = {"code_predictor"}`，非空。
- 只有 `code_predictor` 阶段的 `_allow("code_predictor")` 为 True，其余阶段 `_allow(...)` 全为 False。
- 因此即便 thinker/visual/audio 在分类上「支持」，也因 `_allow` 返回 False 被跳过——只导出 CodePredictor 一个组件。
- 这正是注释里说的「Useful for re-running a single stage, e.g. `--components code_predictor`」。

**待本地验证**：若有 Qwen3-Omni 检查点，可实际运行该命令并对比「不带 `--components`」与「带 `--components code_predictor`」时的汇总输出，验证只有 `code_predictor` 子目录被生成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_get_weights()` 要做成惰性加载，而不是 `main()` 开头就一次性读完所有权重？

**参考答案**：因为不同工种对权重的需求不同。`--skip-llm` 只导视觉/音频编码器时，LLM 主干权重完全用不到；`--dflash-draft` 只导 draft 时也不需要 base 权重。惰性加载避免「读了几十 GB 权重却没用上」的内存浪费，也缩短了不必要的启动时间。

**练习 2**：阶段表里 `audio` 阶段的启用条件是 `_has_audio(model_type) and not args.skip_audio and not _draft_only and _checkpoint_audio_config(config) is not None and _allow("audio")`。请解释 `_checkpoint_audio_config(config) is not None` 这一项为什么必要。

**参考答案**：`_has_audio` 只看 `model_type` 是否在 `_AUDIO_MODEL_TYPES` 里。但有些 `gemma4` 检查点虽然是 audio 家族，其 `config.json` 里 `audio_config` 显式为 `null`（纯 dense 文本模型，没有音频塔）。若不加这一项，导出器会试图导出一个不存在的音频编码器而失败。`_checkpoint_audio_config` 在 `audio_config` / `thinker_config.audio_config` / `sound_config` 三处都找不到时返回 `None`，作为「这个检查点真的有音频编码器吗」的最终事实判断。

---

### 4.2 组件分类：model_type 如何决定导出哪些组件

#### 4.2.1 概念说明

整个编排的「主键」是检查点 `config.json` 里的 `"model_type"`。`scripts/export.py` 用一组**分类常量（frozenset）**把 model_type 映射到「它带哪些组件」。

这里有一个关键的认知模型：一个多模态模型不是「一个大模型」，而是**一组协作的子模型**。例如 Qwen3-Omni 包含：

- `thinker`：理解文本+音频+图像的主 LLM；
- `talker`：生成语音 token 的解码器（本质是个小 LLM）；
- `code_predictor`：把 talker 输出转成离散 codec token 的小解码器；
- `audio`：音频编码器；
- `visual`：视觉编码器；
- `code2wav`：把 codec token 转成波形的声码器。

不同 model_type 带的子集不同。分类常量就是用来回答「这个 model_type 带不带某类组件」。

另一个关键概念是**输出布局（layout）**：每个组件写到输出根目录的哪个子目录。大多数组件用默认布局，少数 model_type（Qwen3-Omni、Qwen3-TTS）有定制布局，以便和已有的引擎构建脚本对齐。

#### 4.2.2 核心流程

分类与布局的决策可以用下面的伪代码表示：

```
model_type = config["model_type"]

# 1. 组件有无：靠 frozenset 成员判断
has_visual = model_type in _VLM_MODEL_TYPES
has_audio  = model_type in _AUDIO_MODEL_TYPES and _checkpoint_audio_config(config) is not None
has_code2wav = model_type in _CODE2WAV_MODEL_TYPES
llm_components = _LLM_COMPONENTS.get(model_type, _DEFAULT_LLM_COMPONENTS)  # 默认 {"thinker"}

# 2. 输出子目录：先查 model_type 专属覆盖，否则用默认
sub_dir = _LAYOUT_OVERRIDES.get(model_type, {}).get(component, _DEFAULT_LAYOUT[component])
out_dir = os.path.join(output_root, sub_dir)
```

注意两个细节：

- **LLM 家族组件**用一张 dict `_LLM_COMPONENTS` 而不是 frozenset，因为它要回答的是「这个 model_type 的 LLM 部分包含哪些子解码器」。默认值 `{"thinker"}` 覆盖了绝大多数普通 LLM/VLM。
- **`qwen3_tts` 故意被排除在 `_AUDIO_MODEL_TYPES` 外**（源码注释明确写了 "Qwen3-TTS has NO audio encoder"）：它的 Talker/CodePredictor 都是 LLM 解码器，音频走 `speech_tokenizer/` 子目录里的 Code2Wav，没有独立音频编码器。

#### 4.2.3 源码精读

分类常量集中在文件开头：

[scripts/export.py:87-154](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L87-L154) —— 五组分类常量：

- `_VLM_MODEL_TYPES`（L99-L114）：带视觉编码器的，含 `qwen3_vl`、`qwen3_omni`、`qwen3_5`、`qwen2_5_vl`、`internvl`、`phi4mm`、`gemma4`、`alpamayo_r1`、Nemotron-Omni 等。
- `_AUDIO_MODEL_TYPES`（L116-L127）：带音频编码器的，**注释明确把 `qwen3_tts` 排除**。
- `_CODE2WAV_MODEL_TYPES`（L137-L141）：带 Code2Wav 声码器的，只有 `qwen3_omni`、`qwen3_omni_moe`、`qwen3_tts`。
- `_ACTION_MODEL_TYPES`（L143-L145）：带 action 专家（Alpamayo 机器人动作模型）。
- `_LLM_COMPONENTS`（L149-L153）：dict，列出哪些 model_type 的 LLM 部分含 `talker`/`code_predictor`；`_DEFAULT_LLM_COMPONENTS = {"thinker"}`（L154）是兜底。

把分类常量封装成布尔谓词，让阶段表更可读：

[scripts/export.py:157-197](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L157-L197) —— `_has_visual` / `_has_audio` / `_has_code2wav` / `_has_action` / `_has_llm_component` 等小函数，每个都是一行「`model_type in 某个 frozenset`」。`_has_llm_component` 用 `_LLM_COMPONENTS.get(model_type, _DEFAULT_LLM_COMPONENTS)` 做带默认值的查表。

布局表决定子目录名：

[scripts/export.py:201-235](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L201-L235) —— `_DEFAULT_LAYOUT`（L201-L211）给出每个组件的默认子路径（`thinker→llm`、`visual→visual`、`audio→audio`、`code2wav→code2wav`、`action→action`、`mtp_draft→mtp_draft`、`dflash_draft→dflash_draft` 等）；`_LAYOUT_OVERRIDES`（L214-L229）只列「需要特殊布局」的 model_type。`_layout_for`（L232-L235）先查 override、查不到用默认。

值得注意的是 Qwen3-Omni 的 override 把组件都塞进 `llm/thinker`、`llm/talker`、`llm/code_predictor`，并把 audio/code2wav 放到 `audio/` 下、visual 放到 `vision/`：

[scripts/export.py:214-229](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L214-L229) —— Qwen3-Omni 与 Qwen3-TTS 的定制布局。Qwen3-TTS 没有 thinker，所以把 `talker` 直接写到 `llm/`（注释解释：这样「期望单个 `llm/` 目录」的旧引擎构建脚本仍能工作）。

#### 4.2.4 代码实践

> **实践目标**：追踪一个 VLM（`qwen3_vl`）走 `export.py`，列出它会被导出哪些子图目录、以及每个目录会落哪些产物文件。

**操作步骤**：

1. 假设 `model_type = "qwen3_vl"`，`config.json` 里有 `vision_config`，无 `audio_config`，未传任何投机解码/`--skip-*`/`--components` 参数。
2. 用源码逐项判定阶段表 [scripts/export.py:2712-2769](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L2712-L2769) 中每个阶段的 `enabled`。
3. 对启用的阶段，用 `_layout_for("qwen3_vl", component)` 算子目录（`qwen3_vl` 不在 `_LAYOUT_OVERRIDES`，故全用默认）。

**需要观察的现象 / 预期结果**：

`qwen3_vl` 的判定结果：

| 阶段 | 判定 | 结果 |
| --- | --- | --- |
| thinker | `_has_llm_component("qwen3_vl","thinker")`：`qwen3_vl` 不在 `_LLM_COMPONENTS` → 用默认 `{"thinker"}` → True；未 skip → **启用** | ✅ |
| mtp_draft | `args.mtp` 默认 False → 不启用 | ❌ |
| dflash_draft | `args.dflash_draft` 默认 False → 不启用 | ❌ |
| talker / code_predictor | `qwen3_vl` 默认组件集只有 thinker → False | ❌ |
| visual | `qwen3_vl in _VLM_MODEL_TYPES` → True；未 skip → **启用** | ✅ |
| audio | `qwen3_vl` 不在 `_AUDIO_MODEL_TYPES` → False | ❌ |
| code2wav | `qwen3_vl` 不在 `_CODE2WAV_MODEL_TYPES` → False | ❌ |
| action | `qwen3_vl` 不在 `_ACTION_MODEL_TYPES` → False | ❌ |

因此 `qwen3_vl` 只导出 **两个组件**，落到两个子目录：

- `<out>/llm/` —— 由 `_export_llm` 产出（ thinker）。产物：`model.onnx` + `model.onnx.data` + `config.json` + `embedding.safetensors` + tokenizer 文件 + `processed_chat_template.json`（详见 4.4）。
- `<out>/visual/` —— 由 `_export_visual` 产出。产物：`model.onnx` + `config.json` + `preprocessor_config.json`（详见 4.3）。

此外，`_export_llm` 末尾会调用 `_patch_multimodal_token_ids` 把 `image_token_id` 等注入 `<out>/llm/config.json`（见 4.4），这样 C++ 运行时知道 prompt 里哪些占位 token 要被替换成视觉编码器 embedding。

**预期结果**：导出完成后，汇总输出里只有 `llm` 和 `visual` 两行，各带其 sidecar。

**待本地验证**：若有 Qwen3-VL 检查点和 GPU，运行 `tensorrt-edgellm-export /path/to/Qwen3-VL /out` 并核对最终汇总打印与上述两个子目录一致。

#### 4.2.5 小练习与答案

**练习 1**：Qwen3-TTS（`model_type = "qwen3_tts"`）会导出哪些组件？落到哪些子目录？

**参考答案**：`_LLM_COMPONENTS["qwen3_tts"] = {"talker", "code_predictor"}`，所以 thinker 阶段不启用（没有 thinker），但 talker 和 code_predictor 启用。`qwen3_tts` 在 `_CODE2WAV_MODEL_TYPES` 里，所以 code2wav 也启用。它**不在** `_VLM_MODEL_TYPES`、**不在** `_AUDIO_MODEL_TYPES`（故意排除），所以无 visual、无 audio。布局上 `qwen3_tts` 有 override：`talker → llm/`、其余用默认。因此产物子目录为：`<out>/llm/`（talker）、`<out>/code_predictor/`、`<out>/code2wav/`。

**练习 2**：如果想给一个新模型（比如某个新的 VLM）加上「带 action 专家」的支持，需要在 `export.py` 改哪些地方？

**参考答案**：把它加入 `_ACTION_MODEL_TYPES`（如有视觉还要加入 `_VLM_MODEL_TYPES`），阶段表里 `action` 阶段就会自动启用；若它需要非默认的输出布局，再在 `_LAYOUT_OVERRIDES` 加一条。`_export_action` 工种本身不需要改（除非新模型的 action 配置结构不同）。这正体现了「分类常量 + 阶段表」架构的可扩展性。

---

### 4.3 编码器注册与选择：export_encoder.py 的家族表

#### 4.3.1 概念说明

视觉/音频编码器的导出和 LLM 主干不同：LLM 走 `AutoModel.from_pretrained` + `export_onnx` 的通用管线（u2-l2/u2-l5），而视觉/音频编码器各有自己的 `build_*` 函数，从检查点权重「从零搭建」一个编码器 `nn.Module`。

`export_encoder.py` 把这两类编码器统一到一个模块里（它取代了旧的 `export_visual.py` / `export_audio.py`），核心机制是**家族注册表（family registry）**：三张 dict 把 `model_type → 家族名 → 模块路径 → build 函数名` 串起来。这样新增一个视觉编码器，只需要在三张表里各加一行，导出主流程不变。

它复用了与 LLM **同一套 dynamo 导出底座**和同一张 `custom_translation_table`，差异仅在于「模型怎么构建」和「I/O spec 从哪来」。编码器的 I/O spec 由模型类自己的 `get_onnx_export_args()` 提供，而不是 LLM 那种固定的 prefill/decode 双阶段输入。

#### 4.3.2 核心流程

视觉编码器导出的伪代码：

```
family = _VISUAL_REGISTRY[model_type]               # qwen3_vl → "qwen3_vl"
module = importlib.import_module(_VISUAL_FAMILY_MODULE[family])  # 动态导入
build_fn = getattr(module, _VISUAL_FAMILY_BUILD_FN[family])      # 取 build_qwen3_vl_visual
vcfg = _get_visual_config(model_type, config)        # 取出 vision 子配置
model = build_fn(vcfg, weights, model_config=..., dtype=...)    # 从零建模型
inputs, in_names, out_names, shapes = model.get_onnx_export_args(vcfg, device)  # I/O spec
_run_dynamo_export(model, inputs, output_path, ...)  # 复用 LLM 同款 dynamo 底座
# 量化权重后处理（NVFP4/MXFP8），与 LLM 路径一致
```

音频编码器走类似流程，但 build 函数的路由逻辑更复杂（Nemotron/Gemma4/Qwen 三类有不同的 `key_prefix` 和配置来源），其中 Gemma4-Unified 是「encoder-free」，完全绕过注册表自建模块。

#### 4.3.3 源码精读

视觉家族注册表三件套：

[onnx/export_encoder.py:81-100](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export_encoder.py#L81-L100) —— `_VISUAL_REGISTRY`：`model_type → 家族名`。例如 `qwen3_vl → "qwen3_vl"`，`internvl_chat → "internvl3"`，`phi4_multimodal → "phi4mm"`。注意 `qwen3_omni` / `qwen3_omni_moe` 都映射到 `"qwen3_omni"` 家族（因为 MoE 版的视觉编码器权重与 dense 版字节相同，只是 HF 命名不同，需要 key 翻译）。

[onnx/export_encoder.py:103-124](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export_encoder.py#L103-L124) —— `_VISUAL_FAMILY_MODULE`：`家族名 → 模块路径`，例如 `"qwen3_vl" → "tensorrt_edgellm.models.qwen3_vl.modeling_qwen3_vl_visual"`。

[onnx/export_encoder.py:127-138](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export_encoder.py#L127-L138) —— `_VISUAL_FAMILY_BUILD_FN`：`家族名 → build 函数名`，例如 `"qwen3_vl" → "build_qwen3_vl_visual"`。

视觉子配置提取：

[onnx/export_encoder.py:173-201](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export_encoder.py#L173-L201) —— `_get_visual_config()`：不同家族把视觉配置存在不同位置。Qwen 系（`qwen3_vl`/`qwen2_5_vl` 等）存在 `vision_config`（或 `thinker_config.vision_config`）；InternVL/Gemma4/Nemotron 需要完整 config；**Phi-4mm 的视觉配置是硬编码的**（不在 config.json 里，值要匹配 `vision_siglip_navit.py`）。

视觉编码器导出主函数：

[onnx/export_encoder.py:259-311](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export_encoder.py#L259-L311) —— `export_visual_onnx()`：校验 model_type 在注册表里 → 算家族 → 取 vcfg → 用 `importlib.import_module` 按 package 相对路径动态导入模块 → 取 build 函数 → `build_fn(vcfg, weights, model_config=..., dtype=...)` 建模型 → 从 `visual_model.get_onnx_export_args(vcfg, device)` 拿 I/O spec → 调 `_run_dynamo_export`。

[onnx/export_encoder.py:316-335](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export_encoder.py#L316-L335) —— 量化权重的 TRT 兼容后处理：NVFP4 改 dtype、MXFP8 剥离 onnxscript 内部属性、共享 DQL scale 去重。这些 pass 直接复用 `export.py` 里 LLM 路径的同名函数（`_fix_nvfp4_weight_dtype` 等），保证视觉编码器的量化图与 LLM 一致地「TRT 友好」。注意一个差异：视觉图里 RMSNorm 会合法地保留 FP32 常量，所以这里**跳过**了 LLM 路径用的「FP32→FP16 降级」。

共享的 dynamo 导出底座：

[onnx/export_encoder.py:209-251](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export_encoder.py#L209-L251) —— `_run_dynamo_export()`：与 u2-l5 讲的 LLM 导出同源——`torch.onnx.export(dynamo=True, opset_version=_OPSET_VERSION, custom_translation_table=...)`，同样在 `_permissive_inline_opset()` 上下文里解决 opset 冲突，同样 `external_data=True` 把大权重外置。差异只在「I/O 来自模型的 `get_onnx_export_args`」而非 LLM 的固定 prefill/decode 输入。

音频编码器路由更分散：

[onnx/export_encoder.py:343-432](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export_encoder.py#L343-L432) —— `export_audio_onnx()`：Gemma4-Unified 是「encoder-free」特例（L373-L389），自建模块、自给 I/O spec，完全绕过注册表；Nemotron-Omni、Gemma4、Qwen3-ASR/Omni 各自走不同的 `build_fn` 和 `key_prefix`（`_AUDIO_KEY_PREFIX` 决定权重在检查点里的前缀，如 `thinker.audio_tower.`）。

#### 4.3.4 代码实践

> **实践目标**：通过阅读注册表，说明「为什么 Qwen3-Omni 的 MoE 版视觉编码器能复用 dense 版的 C++ runner」。

**操作步骤**：

1. 读 [onnx/export_encoder.py:84-87](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export_encoder.py#L84-L87)：注释说明 `qwen3_omni` / `qwen3_omni_moe` 都分派到 `"qwen3_omni"` 家族，因为 MoE 版只是 HF 参数命名不同（`thinker.visual.merger.ln_q`、`mlp.0/2`、`merger_list`），计算图完全一致。
2. 再回到 `scripts/export.py` 的 `_export_visual`，看它如何为 `qwen3_omni_moe` 写 config.json：

[scripts/export.py:1145-1153](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L1145-L1153) —— `qwen3_omni_moe` 的 `vision_config.model_type` 被强制改写成 `qwen3_omni_vision_encoder`（dense 版注册的 C++ 标签），因为 HF 原始值 `qwen3_omni_moe_vision_encoder` 在 C++ `stringToModelType()` 里没注册。

**需要观察的现象 / 预期结果**：

- MoE 版视觉编码器导出时用 dense 版的 `build_qwen3_omni_visual`（内部做 key 翻译），产出的 ONNX 计算图与 dense 版一致。
- 写出的 `visual/config.json` 里 `model_type` 被规范化为 `qwen3_omni_vision_encoder`，这样 C++ visualBuilder 能识别。
- 结论：Python 侧的「家族复用 + key 翻译」与 sidecar 侧的「model_type 规范化」配合，让 MoE 版无需在 C++ 侧新增任何 runner 枚举。

**待本地验证**：若有 Qwen3-Omni 与 Qwen3-Omni-MoE 两个检查点，分别导出后对比两者 `visual/model.onnx` 的算子结构（应基本一致），并对比 `visual/config.json` 的 `model_type` 字段（应都被规范化为同一个值）。

#### 4.3.5 小练习与答案

**练习 1**：Phi-4mm 的视觉配置为什么不能用「从 config.json 读 vision_config」的方式获取？

**参考答案**：Phi-4mm 的视觉配置（SigLIP-NAViT 的 hidden_size=1152、image_size=448、27 层等）**不在 config.json 里**，而是硬编码在 `vision_siglip_navit.py::get_siglip_vision_model()`。所以 `_get_visual_config` 对 `phi4mm`/`phi4_multimodal` 返回一个硬编码 dict，只把 `proj_hidden_size` 从 config 里取（因为投影维度要匹配文本 hidden_size）。

**练习 2**：`_run_dynamo_export` 同时服务视觉和音频编码器，它们与 LLM 导出共享了哪些关键环节？

**参考答案**：共享四处：(1) `build_custom_translation_table()`——同一张「FX 自定义算子 → onnxscript 函数」翻译表；(2) `_permissive_inline_opset()` 上下文——解决 opset 18/21 冲突；(3) `torch.onnx.export(dynamo=True, opset_version=_OPSET_VERSION)` 底座；(4) `external_data=True` 把大权重外置成 `model.onnx.data`。差异仅在 I/O spec 来源：编码器来自模型类的 `get_onnx_export_args`，LLM 来自固定的 prefill/decode 双阶段输入。

---

### 4.4 sidecar 产物：ONNX 之外的运行时配套文件

#### 4.4.1 概念说明

光有一张 `model.onnx` 是不够的。C++ 运行时加载引擎时，还需要一堆「伴生文件（sidecar）」才能正确工作。本讲把它们分成三类：

1. **LLM 类组件的 sidecar**（由 `export_onnx` → `write_runtime_artifacts` 统一产出）：`config.json`（运行时配置）、`embedding.safetensors`（词嵌入表）、tokenizer 文件、`processed_chat_template.json`（预编译 chat 模板）。
2. **多模态占位 token ID**：`_patch_multimodal_token_ids` 把 `image_token_id` / `audio_token_id` 等注入 LLM 的 `config.json`，让运行时知道 prompt 里哪些占位 token 要被替换成编码器 embedding。
3. **外部权重 sidecar**（`external_weights.py`，由 `--externalize-weights` 触发）：把选定的超大权重从 ONNX 图里抽出来，单独写成 safetensors 文件，减小 ONNX 体积、便于按需加载。

编码器组件（visual/audio/code2wav）也有各自的 sidecar，主要是定制化的 `config.json`（见 4.3）和 `preprocessor_config.json`。

一个关键直觉：**sidecar 是 Python 导出端与 C++ 运行时之间的「契约文件」**。导出端负责把它们凑齐放进对应子目录，C++ 运行时按固定文件名去同目录找。文件名错了或缺了，运行时就会静默 fallback（比如 `audio_token_id` 为 0 时 thinker 会回答「我听不到音频」）或直接报错。

#### 4.4.2 核心流程

LLM sidecar 的产出链路：

```
_export_llm
  └─ export_onnx(model, output_path, ...)          # onnx/export.py
       ├─ _export_model(...)                        # dynamo 导出 model.onnx + .data
       ├─ write_runtime_artifacts(model, ...)       # checkpoint_utils.py
       │     ├─ build_runtime_llm_config_dict → config.json
       │     ├─ 写 embedding.safetensors（FP16 或 FP8）
       │     ├─ 拷贝 tokenizer 文件（RUNTIME_TOKENIZER_FILENAMES）
       │     ├─ 生成 tokenizer.json（若只有 vocab.json+merges.txt）
       │     ├─ 写 d2t.safetensors（EAGLE3 draft）
       │     └─ process_chat_template → processed_chat_template.json
       └─ patch_external_weight_manifest(...)       # 若有外部权重
  └─ _patch_multimodal_token_ids(...)               # 注入 image/audio token ID
```

外部权重 sidecar 的产出链路（在 `_export_model` 内部，由 `externalize_model_weights` 完成）：

```
externalize_model_weights(onnx_path, model, kinds)
  ├─ resolve_externalize_weights(kinds)             # 规范化 "all" 等
  ├─ 按 kind 找出对应的 ONNX initializer
  │     （int4_ffn / int4_moe / nvfp4_moe / lm_head 各有专用查找函数）
  ├─ _write_external_weight_file → 写 safetensors
  ├─ _add_external_weight_inputs → 把这些张量改成 ONNX 图输入
  └─ _remove_externalized_initializers → 从 initializer 列表删掉
```

#### 4.4.3 源码精读

`export_onnx` 是所有 LLM 类组件的统一 sidecar 入口：

[onnx/export.py:71-127](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export.py#L71-L127) —— `export_onnx()` 的 docstring 明确写出它产出 `model.onnx`、`model.onnx.data`、运行时 config（`config_filename`）、`embedding.safetensors` 和 tokenizer 文件。它先做 `_export_model`（dynamo 导出 + 可选外部权重），再调 `write_runtime_artifacts` 写 sidecar，最后若有外部权重就 `patch_external_weight_manifest` 把外部权重清单写进 config.json。

`write_runtime_artifacts` 是 sidecar 的「总装车间」：

[checkpoint/checkpoint_utils.py:760-814](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py#L760-L814) —— 先用 `build_runtime_llm_config_dict(model)` 构造运行时 config；对 VLM 还会把原始 HF config 的 `vision_config` 保留进 config.json（C++ VLM runner 要从中读 `deepstack_visual_indexes` 等），并从 `config.json`/`generation_config.json` 传播 `eos_token_id`。

[checkpoint/checkpoint_utils.py:816-877](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py#L816-L877) —— 词嵌入 sidecar：**EAGLE3/MTP/Gemma4-MTP draft 模型跳过** `embedding.safetensors`（它们复用 base 模型的嵌入表）；其余模型从 `embed_tokens` 取权重，乘上 embedding scale，转成 FP16（或经 `--fp8-embedding` 量化成 FP8 E4M3 + per-block scale）写入 `embedding.safetensors`。Gemma4 PLE 还额外写 `ple_embedding.safetensors`。

tokenizer 文件拷贝与生成：

[checkpoint/checkpoint_utils.py:885-909](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py#L885-L909) —— 遍历 `RUNTIME_TOKENIZER_FILENAMES`（`tokenizer.json`、`tokenizer_config.json`、`tokenizer.model`、`special_tokens_map.json`、`processed_chat_template.json`）逐个拷贝；若 `tokenizer.json` 缺失但 `vocab.json`+`merges.txt` 存在（GPT-2 格式，Qwen3-ASR/TTS 用），用 `transformers.AutoTokenizer` 现场生成 `tokenizer.json`。

[checkpoint/checkpoint_utils.py:45-51](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py#L45-L51) —— `RUNTIME_TOKENIZER_FILENAMES` 常量定义。

chat 模板预编译：

[checkpoint/checkpoint_utils.py:921-925](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py#L921-L925) —— 若 `processed_chat_template.json` 不存在，调 `process_chat_template` 预编译；失败则写一个 fallback。

[chat_template.py:352-391](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/chat_template.py#L352-L391) —— `process_chat_template()`：优先用 model_type 对应的硬编码模板；否则用 `AutoProcessor`/`AutoTokenizer` 加载 Jinja chat 模板，格式化 system/user 占位消息，提取出「C++ 运行时只需做字符串拼接」就能用的前缀/后缀模式，写成 `processed_chat_template.json`。这是把「需要完整 Jinja 引擎」的模板预编译成「C++ 端轻量拼接」的形式。

多模态占位 token ID 注入：

[scripts/export.py:692-748](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L692-L748) —— `_patch_multimodal_token_ids()`：不同检查点家族把 `image_token_id`/`audio_token_id` 存在不同位置（Qwen3-Omni 在 `thinker_config`、Nemotron-Omni 在 root 且叫 `img_context_token_id`）。这个函数按家族分派 collector，必要时回退到「从 tokenizer 的 `<|image_pad|>` 特殊 token 反查 ID」，最后把收集到的 ID 注入已写好的 LLM `config.json`。注释点明：C++ 的 `llmEngineRunner.cpp` 读这些 ID 来定位「要被替换成编码器 embedding」的占位位置，ID 为 0 时运行时跳过替换，导致「我听不到音频」类的错误回答。

外部权重 sidecar 的种类定义：

[external_weights.py:24-35](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/external_weights.py#L24-L35) —— 四种外部权重种类：`int4_ffn`（dense FFN 的 Int4 GEMM 权重）、`int4_moe`（MoE 的 Int4 权重）、`nvfp4_moe`（NVFP4 MoE 权重）、`lm_head`（LM 头权重），外加 `all` 表示全部。

[external_weights.py:38-57](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/external_weights.py#L38-L57) —— `resolve_externalize_weights()`：规范化输入（支持单字符串或列表），把 `"all"` 展开成全部四种，并校验未知值。

[external_weights.py:398-490](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/external_weights.py#L398-L490) —— `externalize_model_weights()`：对每种 kind 用专用查找函数（如 `_find_int4_moe_weight_initializers`）在 ONNX 图里定位对应的 initializer → 写成独立 safetensors（如 `external_int4_moe_weights.safetensors`）→ 用 `_add_external_weight_inputs` 把这些张量暴露成 ONNX 图的固定形状输入 → 用 `_remove_externalized_initializers` 从 initializer 列表删除。manifest 最终由 `patch_external_weight_manifest` 写进 config.json 的 `external_weight_files` 字段。

特别注意：量化 LM 头不能外置。

[external_weights.py:74-105](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/external_weights.py#L74-L105) —— `reject_quantized_lm_head_externalization()`：`lm_head` 外置假设是一个 dense FP16 权重；若 LM 头被量化（FP8/NVFP4/INT4 AWQ），直接报错并提示用户去掉 `lm_head` 或重新量化。检测委托给 `config.module_quant_type`，与 `make_linear` 用的是同一套判定逻辑。

编码器组件的 sidecar 由各自的 `_export_*` 定制写入（如 `_export_visual` 写带 `model_type`/`vision_config`/token ID/`rope_scaling` 的 config.json，并拷贝 `preprocessor_config.json`），其逻辑在 4.3 与 `_export_visual` 函数体 [scripts/export.py:1075-1371](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L1075-L1371) 中，这里不重复。

#### 4.4.4 代码实践

> **实践目标**：在源码里追踪「一次 `qwen3_vl` 的 thinker 导出，`<out>/llm/` 目录下会落哪些 sidecar」，并把它们和 C++ 运行时的消费点对应起来。

**操作步骤**：

1. 从 `_export_llm` [scripts/export.py:891-902](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L891-L902) 看到 `export_onnx(model, output_path, model_dir=..., fp8_embedding=..., reduced_vocab_dir=..., externalize_weights=..., config_filename=...)`。
2. 进 `export_onnx` [onnx/export.py:114-126](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export.py#L114-L126)，确认它调 `write_runtime_artifacts`。
3. 进 `write_runtime_artifacts` [checkpoint/checkpoint_utils.py:760-925](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py#L760-L925)，逐项列出它写的文件。
4. 回到 `_export_llm` 末尾 [scripts/export.py:907-915](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L907-L915)，确认它额外调 `_patch_multimodal_token_ids`。

**需要观察的现象 / 预期结果**：`<out>/llm/` 目录的 sidecar 清单及用途：

| 文件 | 产出位置 | C++ 运行时用途 |
| --- | --- | --- |
| `model.onnx` + `model.onnx.data` | `_export_model` | 引擎构建的输入图 |
| `config.json` | `write_runtime_artifacts` | 架构/精度/边界配置；后被 `_patch_multimodal_token_ids` 追加 `image_token_id` 等 |
| `embedding.safetensors` | `write_runtime_artifacts`（FP16 或 `--fp8-embedding` 的 FP8） | 词嵌入查表 |
| `tokenizer.json` 等 | `write_runtime_artifacts` 拷贝/生成 | C++ tokenizer 分词 |
| `processed_chat_template.json` | `process_chat_template` | 多轮对话格式化 |
| `external_*_weights.safetensors`（仅 `--externalize-weights`） | `externalize_model_weights` | 大权重按需加载，清单在 config.json 的 `external_weight_files` |

**预期结果**：对一个未开 `--fp8-embedding`、未开 `--externalize-weights` 的 `qwen3_vl` thinker 导出，`<out>/llm/` 至少包含 `model.onnx`、`model.onnx.data`、`config.json`、`embedding.safetensors`、`tokenizer.json`、`tokenizer_config.json`、`special_tokens_map.json`、`processed_chat_template.json`，且 `config.json` 里有 `image_token_id` 字段。

**待本地验证**：实际导出后用 `ls <out>/llm/` 与 `python -c "import json;print(json.load(open('<out>/llm/config.json'))['image_token_id'])"` 核对。

#### 4.4.5 小练习与答案

**练习 1**：为什么 EAGLE3 draft 模型导出时不写 `embedding.safetensors`？

**参考答案**：draft 模型复用 base 模型的嵌入表（C++ 构建器对 draft 模型会跳过拷贝）。`write_runtime_artifacts` 检测到 `model.config.is_eagle3_draft`（或 `is_mtp_draft`/`is_gemma4_mtp_draft`）就跳过写 `embedding.safetensors`，避免冗余存储一份与 base 相同的大表。

**练习 2**：用户对 NVFP4 量化的模型加了 `--externalize-weights lm_head`，会发生什么？为什么？

**参考答案**：会直接报错。`reject_quantized_lm_head_externalization` 检测到 LM 头的 `module_quant_type` 不是 FP16（NVFP4 量化下是 nvfp4），就抛 `ValueError`，提示「去掉 `lm_head` 或不带 `--lm_head_quantization` 重新量化」。原因是 `lm_head` 外置路径假设单个 dense FP16 权重，量化权重无法这样简单外置。这个检查发生在昂贵的 dynamo 导出**之前**，能尽早失败。

**练习 3**：`processed_chat_template.json` 解决了什么问题？

**参考答案**：HF 的 chat 模板是 Jinja2 字符串，C++ 运行时没有 Jinja 引擎。`process_chat_template` 在 Python 端用 tokenizer 格式化占位的 system/user 消息，提取出「system 段前缀+后缀」「user 段前缀+后缀」等纯字符串模式，写成 JSON。C++ 运行时只需做字符串拼接就能格式化多轮对话，不必依赖 Jinja。

---

## 5. 综合实践

**任务**：为一个 Qwen3-Omni 检查点（`model_type = "qwen3_omni"`）规划完整的导出方案，并预测产物目录结构。

要求：

1. **判定组件**：用本讲的分类常量，列出 `qwen3_omni` 会启用哪些阶段（默认参数，不传 `--skip-*`/`--components`）。提示：查 `_LLM_COMPONENTS["qwen3_omni"]`、`_VLM_MODEL_TYPES`、`_AUDIO_MODEL_TYPES`、`_CODE2WAV_MODEL_TYPES`。
2. **算布局**：用 `_LAYOUT_OVERRIDES["qwen3_omni"]`（[scripts/export.py:215-222](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L215-L222)）算出每个组件的子目录。
3. **列 sidecar**：对每个子目录，列出会落的 sidecar 文件（区分 LLM 类组件经 `write_runtime_artifacts` 的统一 sidecar，与编码器/声码器组件经各自 `_export_*` 的定制 sidecar）。
4. **画目录树**：画出预期的 `<out>/` 目录树。
5. **设计重跑**：若你只改了 CodePredictor 想重导它一个，写出对应的命令（用 `--components`）。

**参考答案要点**：

- 启用阶段：thinker、talker、code_predictor（来自 `_LLM_COMPONENTS["qwen3_omni"] = {"thinker","talker","code_predictor"}`）、visual（在 `_VLM_MODEL_TYPES`）、audio（在 `_AUDIO_MODEL_TYPES` 且有 `thinker_config.audio_config`）、code2wav（在 `_CODE2WAV_MODEL_TYPES`）。共 6 个组件。
- 布局（Qwen3-Omni override）：thinker→`llm/thinker`、talker→`llm/talker`、code_predictor→`llm/code_predictor`、visual→`vision`、audio→`audio/audio_encoder`、code2wav→`audio/code2wav`。
- sidecar：`llm/thinker` 带 `embedding.safetensors` + tokenizer + chat template（且 config.json 被 `_patch_multimodal_token_ids` 注入 `image_token_id`/`audio_token_id`，再被 `_patch_tts_config` 注入 codec/TTS 字段）；`llm/talker` 带 `embedding.safetensors`/`hidden_projection.safetensors`/`text_projection.safetensors`（由 `_extract_omni_talker_sidecars` 产）；`llm/code_predictor` 带 `codec_embeddings.safetensors`/`lm_heads.safetensors`/`small_to_mtp_projection.safetensors`；`vision`/`audio/audio_encoder` 各带定制 `config.json` + `preprocessor_config.json`；`audio/code2wav` 带 `config.json`。
- 重跑命令：`tensorrt-edgellm-export /path/to/qwen3_omni /out --components code_predictor`。

**待本地验证**：上述目录树与 sidecar 清单需在有 Qwen3-Omni 检查点和 GPU 的环境实际导出后核对；若不可得，至少完成源码追踪并解释每一项的依据。

## 6. 本讲小结

- `scripts/export.py` 的 `main()` 是导出「项目经理」：用一张「阶段表（stages）」`(enabled, component, callable)` 驱动全部组件导出，主循环只有一句 `for ... if enabled: fn(out_dir)`，新增组件只需加一行。
- 组件分类完全由 `model_type` 主键驱动：`_VLM_MODEL_TYPES`/`_AUDIO_MODEL_TYPES`/`_CODE2WAV_MODEL_TYPES`/`_ACTION_MODEL_TYPES` 决定带不带某类编码器，`_LLM_COMPONENTS`（默认 `{"thinker"}`）决定 LLM 部分含哪些子解码器；`qwen3_tts` 故意被排除在 audio 家族外。
- 输出布局由 `_DEFAULT_LAYOUT` + `_LAYOUT_OVERRIDES` 决定；大多数模型用默认（`thinker→llm`、`visual→visual`…），Qwen3-Omni/Qwen3-TTS 有定制布局以兼容既有构建脚本。
- `export_encoder.py` 用三张家族注册表（`_VISUAL_REGISTRY`/`_VISUAL_FAMILY_MODULE`/`_VISUAL_FAMILY_BUILD_FN`）把 model_type 路由到 `build_*` 函数，视觉/音频编码器复用与 LLM 同一套 dynamo 底座和翻译表，差异仅在模型构建与 I/O spec 来源。
- sidecar 是 Python 导出端与 C++ 运行时的契约文件：LLM 类组件经 `write_runtime_artifacts` 统一产出 `config.json`/`embedding.safetensors`/tokenizer/`processed_chat_template.json`；多模态占位 token ID 经 `_patch_multimodal_token_ids` 注入 config.json；`--externalize-weights` 可把超大权重抽成独立 safetensors。
- 一次导出的产物清单可直接看 `main()` 末尾的汇总打印（`_SIDECARS` 元组 + 每组件 `model.onnx` 大小）。

## 7. 下一步学习建议

本讲讲完了「Python 导出端如何把一个多模态检查点编排成一组 ONNX + sidecar」。接下来：

- **进入 C++ 引擎构建器**（u4 单元，尤其 u4-l1 构建器八阶段、u4-l3 使用 builder CLI）：这些 ONNX 子图与 sidecar 正是 `llm_build` / `visual_build` 的输入，你会看到构建器如何读 config.json、按 prefill/decode 双 profile 编译 engine。
- **若关心多模态运行时**：直接跳到 u6-l1（多模态运行器与视觉编码器），看 C++ 端 `multimodalRunner` 如何消费这里导出的 visual ONNX 与 `image_token_id`。
- **若关心投机解码的导出侧**：本讲提到了 `--eagle-base`/`--mtp`/`--dflash-*` 阶段，完整变体解析与 draft 导出在 u7-l2（导出侧的投机解码）深入。
- **建议继续阅读的源码**：挑一个你感兴趣的 `_export_*`（如 `_export_talker` 或 `_export_code_predictor`）通读，体会「分类 → 布局 → 导出 → 写定制 sidecar」四步在一个具体组件上的完整落地。
