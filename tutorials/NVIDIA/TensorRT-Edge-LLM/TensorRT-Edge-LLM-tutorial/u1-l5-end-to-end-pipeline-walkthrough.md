# 端到端流水线实战

## 1. 本讲目标

在前几讲里，你已经从「概念」上知道了 TensorRT Edge-LLM 是一条三段式流水线：

> HuggingFace 检查点 →（Python 导出）→ ONNX →（C++ 引擎构建）→ TensorRT engine →（C++ 运行时）→ 推理文本

但「知道流水线长什么样」和「亲手把它跑通」是两回事。本讲的目标是让你**真正把这三个阶段串起来**，对每个阶段的输入、输出和关键参数建立直观认识。学完后你应该能够：

1. 用 `tensorrt-edgellm-export` 命令把一个 LLM 检查点导出成 ONNX，并说清楚它产出了哪些文件。
2. 用 `examples/llm/llm_build` 把 ONNX 编译成 TensorRT 引擎，并解释 `--maxBatchSize` / `--maxInputLen` / `--maxKVCacheCapacity` 这三个参数的含义。
3. 用 `examples/llm/llm_inference` 加载引擎、跑一次推理、读懂输出 JSON，并对照源码理解从「读输入文件」到「写输出文件」发生了什么。

本讲是 beginner 层的收尾：它不深入任何阶段的内部机制（那是进阶层的任务），而是让你在真实源码的指引下走完一遍端到端流程。

---

## 2. 前置知识

在开始前，请确认你已经具备下面这些认知（来自前置讲义）：

- **三段式流水线与四类产物**：流水线产出文件类型依次是 `.safetensors`（检查点）、`.onnx`（导出产物）、`.engine`（引擎）、最终是 token 文本。
- **量化是可选的前置步骤**：量化的产物仍是 HF 风格的检查点，不是 ONNX；导出器可以直接吃「已经量化好的检查点」。
- **跨机器限制**：导出在 x86 开发机上进行；构建（build）和运行（inference）通常要在目标边缘设备上完成，因为 `.engine` 绑定特定 GPU 型号与 TensorRT 版本，不能跨设备迁移。
- **构建系统**：要跑 `llm_build` / `llm_inference` 这两个 C++ 示例，你先得按 u1-l3 的方式用 CMake 编译出可执行文件（产物默认在 `build/examples/llm/` 下）。
- **CLI 入口**：`tensorrt-edgellm-export` 是 pyproject.toml 里登记的命令，背后指向 `scripts/export.py` 的 `main`。

如果你对上面任何一条感到陌生，建议先回到对应讲义复习。

此外，本讲会用到两个初学者需要先了解的术语：

- **检查点（checkpoint）**：一个 HuggingFace 风格的目录，里面至少有 `config.json`（模型结构描述）和若干 `*.safetensors`（权重），通常还有分词器文件（`tokenizer.json` 等）。
- **ONNX**：一种跨框架的计算图中间格式。这里它是「Python 世界」和「C++ 世界」之间的契约——导出器写出 ONNX，构建器读入 ONNX。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲用它做什么 |
| --- | --- | --- |
| `docs/source/user_guide/getting_started/quick-start-guide.md` | 官方快速上手文档，给出 export→build→inference 三步的真实命令 | 作为命令组装的权威参照 |
| `tensorrt_edgellm/scripts/export.py` | `tensorrt-edgellm-export` 命令的实现：解析参数、按模型类型分发组件、调用导出 | 理解「导出」阶段在源码里到底做了什么 |
| `tensorrt_edgellm/onnx/export.py` | 真正写 ONNX 文件的函数 `export_onnx` | 理解导出的产物构成 |
| `examples/llm/llm_build.cpp` | 「构建」阶段的 C++ 示例入口 | 理解 `llm_build` 的参数与主流程 |
| `cpp/builder/llmBuilder.h` | `LLMBuilder` 与 `LLMBuilderConfig` 的声明 | 理解构建参数的数据结构 |
| `examples/llm/llm_inference.cpp` | 「推理」阶段的 C++ 示例入口 | 理解 `llm_inference` 从读文件到写文件的完整流程 |
| `cpp/runtime/llmInferenceRuntime.h` | 统一运行时 `LLMInferenceRuntime` 的声明 | 理解推理示例如何构造运行时、调用 `handleRequest` |

> 提示：本讲只读这些文件的「入口层」，不展开它们的内部机制。例如 `LLMBuilder::build()` 内部的「八阶段构建流程」会在 u4-l1 专门讲解；`LLMInferenceRuntime` 内部的引擎执行与解码策略会在 u5 系列讲解。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，分别对应流水线的三个阶段：**export CLI**、**llm_build**、**llm_inference**。

### 4.1 export CLI：把检查点导出成 ONNX

#### 4.1.1 概念说明

「导出」阶段要做的事，用一句话概括：**读一个 HuggingFace 检查点，写出一份 C++ 世界能直接消费的 ONNX 图加上若干 sidecar（附带文件）**。

为什么需要这么一个阶段？因为：

- C++ 运行时不依赖 PyTorch/Transformers，它不会去「重新搭一个 HF 模型」。它要的是一张**冻结好的计算图**（ONNX）。
- 这张图里用到的算子并不全是标准 ONNX 算子——EdgeLLM 有自己的自定义算子域（attention、MoE、mamba 等，后续讲义会讲）。导出器负责把这些自定义算子正确地写进图里，并让构建器在编译时把它们 lowering 成 TensorRT 插件。
- 除了图本身，运行时还需要一些「图之外的常量/元数据」，比如 embedding 表（单独存成 `embedding.safetensors`）、分词器文件、`config.json`。这些 sidecar 也由导出器一并产出。

对**纯 LLM**（本讲重点）来说，一次导出的产物是一个 `llm/` 子目录，里面有：

- `model.onnx`（+ `model.onnx.data` 外部权重）：计算图。
- `config.json`：运行时配置（由模型 config 派生）。
- `embedding.safetensors`：词表 embedding，单独存出。
- 分词器文件（`tokenizer.json` 等）：从检查点复制过来。

> 这条结论来自 [tensorrt_edgellm/onnx/export.py:71-103](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export.py#L71-L103) 里 `export_onnx` 的文档注释——它明确写了会输出 `model.onnx`、`model.onnx.data`、运行时 config、`embedding.safetensors` 以及分词器文件。

多模态模型（VLM/音频）还会额外导出 `visual/`、`audio/`、`code2wav/` 等子目录，但本讲用最小 LLM 跑通即可，不涉及它们。

#### 4.1.2 核心流程

`tensorrt-edgellm-export` 的 `main()` 大致按下面顺序工作：

```text
解析命令行参数 (argparse)
        │
        ▼
解析 model 参数 → 得到本地目录或 HF 仓库 ID
解析 config.json → 得到 model_type（如 "qwen3"）
        │
        ▼
根据 model_type + 命令行开关，构建「阶段表 stages」
每个 stage = (是否启用, 组件名, 导出函数)
        │
        ▼
依次执行启用的 stage：
  对 LLM：调用 _export_llm → AutoModel.from_pretrained 加载 → export_onnx 写图
        │
        ▼
打印导出总结（每个组件的 model.onnx 路径与大小、各 sidecar）
```

关键点：导出器**不是无脑把整张 HF 模型 trace 一遍**，而是根据 `model_type` 决定要导出哪些组件（LLM 骨干 / 视觉编码器 / 音频编码器 / draft 模型 / Talker 等）。对纯 LLM，默认就只导出 `thinker`（即 LLM 骨干）。

#### 4.1.3 源码精读

**① 命令行入口与两个位置参数**

`main()` 用 `argparse` 定义了命令。最关键的是两个位置参数 `model`（检查点目录或 HF ID）和 `output_dir`（输出根目录）：

[tensorrt_edgellm/scripts/export.py:2388-2396](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L2388-L2396) —— 定义 `model`（检查点目录或 HF 模型 ID）与 `output_dir`（输出根目录，会自动创建 `llm/`、`visual/`、`audio/` 子目录）。

这意味着最简单的调用形式就是：

```bash
tensorrt-edgellm-export <检查点或HF_ID> <输出目录>
```

`model` 参数会被 `_resolve_model_dir` 处理：如果是本地目录就直接用，否则用 `huggingface_hub.snapshot_download` 下载。

**② 按模型类型分发组件**

`main()` 读取 `config.json` 拿到 `model_type` 后，构造了一张「阶段表」`stages`。每个元素是 `(是否启用, 组件名, 导出函数)`：

[tensorrt_edgellm/scripts/export.py:2712-2769](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L2712-L2769) —— 根据 `model_type` 和命令行开关，列出所有可能运行的导出阶段（thinker/mtp_draft/talker/code_predictor/visual/audio/code2wav/action），每个阶段绑定一个 `_export_*` 函数。

对纯 LLM（如 Qwen3），只有 `thinker` 这一行的第一列为真，其余组件因为 `model_type` 不在对应集合里（如 `_VLM_MODEL_TYPES`）而不会被启用。随后用一个简单循环执行启用的阶段，并把输出目录交给 `_layout_for(model_type, component)` 决定：

[tensorrt_edgellm/scripts/export.py:2810-2814](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L2810-L2814) —— 遍历阶段表，对启用的阶段调用其导出函数，输出子目录由 `_layout_for` 计算（对纯 LLM，`thinker` 映射到 `llm`）。

**③ LLM 骨干的真正导出**

`_export_llm` 是 LLM 组件的导出函数。它的核心是两步：用 `AutoModel.from_pretrained` 把权重加载成内存中的模型，再调用 `export_onnx` 写出 ONNX：

[tensorrt_edgellm/scripts/export.py:866-899](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L866-L899) —— `_export_llm` 内部先用 `AutoModel.from_pretrained(...)` 加载检查点权重，再用 `export_onnx(model, output_path, ...)` 把模型写成 ONNX。`output_path` 默认是 `llm/model.onnx`。

注意这里 `AutoModel.from_pretrained` 是 EdgeLLM 自己的分发器（不是 HF 的 `AutoModel`）：它根据 `model_type` 选择已注册的模型类，或回退到默认解码器。这正是 u2-l2 要讲的内容，本讲只需知道「它能从检查点构造出模型对象」即可。

**④ export_onnx 的产物**

真正写文件的是 `export_onnx`，它的文档注释直接告诉我们产物有哪些：

[tensorrt_edgellm/onnx/export.py:16-45](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export.py#L16-L45) —— 注释说明这是一张「同时覆盖 prefill 和 decode」的图，并列出输入输出张量布局（`inputs_embeds`、`past_key_values_*`、`logits`、`present_key_values_*` 等）。

这段注释很重要，它揭示了 EdgeLLM 的一个关键设计：**一张图同时跑 prefill（首 token）和 decode（后续 token）**，靠 `past_len` 是否为 0 来区分；attention 等算子把状态（KV cache）作为图的输入输出暴露出来。这也是为什么构建阶段需要两套优化 profile（prefill / decode），下文 4.2 会再提到。

#### 4.1.4 代码实践

**实践目标**：为一个最小 LLM（Qwen3-0.6B）组装完整的导出命令，并解释每个参数；如果有 NVIDIA GPU 的 x86 机器则实际执行。

**操作步骤**：

参照官方快速上手文档的「Manual Export and C++ Runtime Path」一节：

1. 设置工作区与 Python 路径：

```bash
export WORKSPACE_DIR=$HOME/tensorrt-edgellm-workspace
export MODEL_NAME=Qwen3-0.6B
mkdir -p $WORKSPACE_DIR
# 让 Python 能找到本仓库的 tensorrt_edgellm 包
export PYTHONPATH=/path/to/TensorRT-Edge-LLM:$PYTHONPATH
```

2. 执行导出（`Qwen/Qwen3-0.6B` 是 HF 仓库 ID，`_resolve_model_dir` 会自动下载）：

```bash
tensorrt-edgellm-export Qwen/Qwen3-0.6B $MODEL_NAME/onnx
```

这条命令对应 [docs/source/user_guide/getting_started/quick-start-guide.md:142-144](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/user_guide/getting_started/quick-start-guide.md#L142-L144)。

**参数解释**：

| 部分 | 含义 |
| --- | --- |
| `tensorrt-edgellm-export` | pyproject.toml 登记的命令，指向 `scripts/export.py:main` |
| `Qwen/Qwen3-0.6B` | 位置参数 `model`：HF 仓库 ID（也会被当作本地目录尝试） |
| `$MODEL_NAME/onnx` | 位置参数 `output_dir`：导出根目录，LLM 会落在 `Qwen3-0.6B/onnx/llm/` |

**需要观察的现象 / 预期结果**：

- 控制台先打印一段「组件启用表」，例如 `Model type : qwen3`，以及 `thinker : yes`，其它组件 `no`。
- 最终打印「Export complete」总结，列出 `Qwen3-0.6B/onnx/llm/model.onnx` 的大小，以及 `embedding.safetensors` 等 sidecar。
- 产物目录结构大致为：

```text
Qwen3-0.6B/onnx/llm/
├── model.onnx          # 计算图
├── model.onnx.data     # 外部权重数据
├── config.json         # 运行时配置
├── embedding.safetensors
└── tokenizer.json ...  # 复制自检查点
```

> 实际执行需要一台装有 NVIDIA GPU 且已安装 TensorRT、CUDA、PyTorch 的 x86 机器。如果你当前环境没有这些依赖，**这一步的运行结果待本地验证**；但命令本身和参数含义你可以照上面的解释先掌握。

#### 4.1.5 小练习与答案

**练习 1**：为什么导出器不直接把整个 HuggingFace 模型 `torch.trace` 一遍，而是要按 `model_type` 分发组件、用自定义算子搭图？

**参考答案**：因为 C++ 运行时不依赖 PyTorch/Transformers，它需要一张冻结的计算图，且图里包含 EdgeLLM 自定义的高性能算子（attention/MoE/mamba 等），这些算子在构建阶段会被 lower 成 TensorRT 插件。直接 trace HF 模型得到的是标准 PyTorch 算子图，无法被运行时高效消费，也无法表达 KV cache 这类需要作为图 I/O 暴露的状态。

**练习 2**：导出纯 LLM（如 Qwen3）时，`visual` / `audio` 组件会运行吗？为什么？

**参考答案**：不会。阶段表里每个组件的第一列（是否启用）取决于 `model_type` 是否在对应集合里。`qwen3` 不在 `_VLM_MODEL_TYPES` / `_AUDIO_MODEL_TYPES` 中，所以这两个 stage 第一列为假，循环里被跳过。只有 `thinker`（LLM 骨干）会运行。

**练习 3**：导出产出的 `model.onnx` 为什么说「一张图同时覆盖 prefill 和 decode」？

**参考答案**：见 `export_onnx` 的注释——同一张图用 `past_len=0` 表示 prefill、`past_len>0` 表示 decode，并通过把 KV cache 等状态作为图的输入输出来在两阶段间传递。这样运行时只需一个引擎、在 prefill/decode profile 间切换即可（详见 4.2 与 u5）。

---

### 4.2 llm_build：把 ONNX 编译成 TensorRT 引擎

#### 4.2.1 概念说明

「构建」阶段把上一阶段的 ONNX **编译**成一个针对当前 GPU 优化过的 TensorRT 引擎（`.engine` 文件）。这一步的产物**不能跨 GPU 型号、不能跨 TensorRT 版本迁移**——这就是为什么它通常要在目标边缘设备上运行。

构建需要几个关键约束参数，它们决定了引擎能处理的输入范围：

- `--maxBatchSize`：引擎能处理的最大批大小。
- `--maxInputLen`：单次输入的最大 token 数（prefill 长度上界）。
- `--maxKVCacheCapacity`：KV 缓存容量（序列长度上界），它决定了一次请求「输入 + 生成」总共能用多长的上下文。

这三个参数之所以重要，是因为 TensorRT 引擎在编译期就要为这些范围挑选/生成最优 kernel；超出范围的输入在运行时会被拒绝。

> 在讲机制之前先建立直觉：`maxKVCacheCapacity` 可以理解为「引擎最多能记住多少个历史 token 的注意力状态」。如果它设成 4096，那么「输入长度 + 生成长度」的总和受这个上限约束。

`llm_build` 本身只是一个**薄薄的命令行外壳**：它解析参数、校验输入目录、把参数填进 `LLMBuilderConfig`，然后委托给 `LLMBuilder::build()` 干真正的活。构建器内部的「八阶段流程」（插件加载→配置解析→网络创建→模型类型检测→ONNX 解析→优化 profile→引擎编译→文件管理）会在 u4-l1 专门讲解，本讲只关注「怎么用」。

#### 4.2.2 核心流程

`llm_build` 的 `main()` 流程很简单：

```text
解析命令行参数 (getopt_long) → LLMBuildArgs
        │
        ▼
校验：onnxDir 下必须存在 config.json
        │
        ▼
把 args 填进 builder::LLMBuilderConfig
        │
        ▼
构造 builder::LLMBuilder(onnxDir, engineDir, config)
        │
        ▼
调用 llmBuilder.build() —— 真正的八阶段编译在这里
        │
        ▼
成功则在 engineDir 下产出 engine 文件 + 拷贝过来的 config/tokenizer/embedding 等
```

#### 4.2.3 源码精读

**① 参数结构**

`llm_build.cpp` 用一个 `LLMBuildArgs` 结构集中存放所有参数，默认值在声明处给出：

[examples/llm/llm_build.cpp:49-64](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_build.cpp#L49-L64) —— `LLMBuildArgs` 定义了 `onnxDir`、`engineDir`、`maxInputLen`（默认 1024）、`maxKVCacheCapacity`（默认 4096）、`maxBatchSize`（默认 4）、`maxLoraRank`（默认 0 = 不带 LoRA）、投机解码开关等字段。

注意几个默认值：`maxBatchSize=4`、`maxInputLen=1024`、`maxKVCacheCapacity=4096`、`maxLoraRank=0`。这些默认值也是 `LLMBuilderConfig` 的默认值，二者保持一致。

**② 主流程**

`main()` 在解析完参数后，先做了一项重要校验——ONNX 目录里必须有 `config.json`：

[examples/llm/llm_build.cpp:217-225](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_build.cpp#L217-L225) —— 校验 `onnxDir + "/config.json"` 存在；若不存在直接报错退出。这说明构建器依赖导出阶段写出的 `config.json` 来读取模型维度（hidden size、KV head 数等）。

随后把参数填进 `LLMBuilderConfig`，构造 `LLMBuilder` 并调用 `build()`：

[examples/llm/llm_build.cpp:228-245](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_build.cpp#L228-L245) —— 把 `args` 逐字段填入 `builder::LLMBuilderConfig`，构造 `builder::LLMBuilder(onnxDir, engineDir, config)`，然后调用 `llmBuilder.build()`；返回 false 则报错退出。

**③ LLMBuilderConfig 与 LLMBuilder**

`LLMBuilderConfig` 的字段与 `LLMBuildArgs` 一一对应，并且能序列化成 JSON（写进引擎目录的 `config.json` 里的 `builder_config`），让运行时知道这个引擎是在什么约束下编译的：

[cpp/builder/llmBuilder.h:37-48](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.h#L37-L48) —— `LLMBuilderConfig` 的字段：`maxInputLen`、`specDraft`、`specBase`、`maxBatchSize`、`maxLoraRank`、`maxKVCacheCapacity`、`maxVerifyTreeSize`、`maxDraftTreeSize` 等。

`LLMBuilder` 类只暴露一个 `build()` 方法，内部完成全部八阶段：

[cpp/builder/llmBuilder.h:150-171](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.h#L150-L171) —— `LLMBuilder` 类声明：构造函数接收 `onnxDir`/`engineDir`/`config`；`build()` 完成加载解析 ONNX、设置优化 profile、编译引擎、拷贝必要文件到 engineDir 的全过程。

> 私有方法里能看到 `setupVanillaProfiles`、`setupSpecDecodeProfiles`、`setupKVCacheProfiles` 等，它们就是「双阶段优化 profile」的来源——对应 4.1.3 提到的「一张图覆盖 prefill+decode」，构建器需要分别为 prefill（context）和 generation（decode）各设一套 profile。这部分细节留给 u4。

#### 4.2.4 代码实践

**实践目标**：为 4.1 导出的 Qwen3-0.6B ONNX 组装一条构建命令，解释参数；如果在目标设备上已编译出 `llm_build`，则实际执行。

**操作步骤**（参照 quick-start-guide 的 Build TensorRT Engine 一节）：

```bash
# 在目标设备（或开发机）上，已 cd 到仓库根目录
./build/examples/llm/llm_build \
    --onnxDir $WORKSPACE_DIR/$MODEL_NAME/onnx/llm \
    --engineDir $WORKSPACE_DIR/$MODEL_NAME/engines \
    --maxBatchSize 1 \
    --maxInputLen 1024 \
    --maxKVCacheCapacity 4096
```

这条命令对应 [docs/source/user_guide/getting_started/quick-start-guide.md:205-211](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/user_guide/getting_started/quick-start-guide.md#L205-L211)。

**参数解释**：

| 参数 | 含义 | 本例取值理由 |
| --- | --- | --- |
| `--onnxDir` | 上一阶段产出的 `llm/` 目录（含 `model.onnx` 与 `config.json`） | 指向 4.1 的产物 |
| `--engineDir` | 引擎输出目录 | 自定义，下一阶段 `llm_inference` 要用它 |
| `--maxBatchSize 1` | 最大批大小 | 最小化构建，单条请求即可 |
| `--maxInputLen 1024` | 最大输入 token 数 | 默认值，覆盖常见 prompt |
| `--maxKVCacheCapacity 4096` | KV 缓存容量（序列长度上界） | 「输入 + 生成」总和不能超过它 |

**需要观察的现象 / 预期结果**：

- 文档提示构建耗时约 2–5 分钟。
- 构建完成后，`engines/` 目录里会出现引擎文件，以及构建器拷贝过来的 `config.json`、分词器文件、`embedding.safetensors` 等（运行时需要它们）。
- 若 `--onnxDir` 下缺 `config.json`，会命中上面源码 ① 的校验并报错。

> ⚠️ engine 不可移植：在 A 机器构建的 engine 拿到 GPU 型号或 TensorRT 版本不同的 B 机器上无法加载。实际执行需要目标设备上有匹配的 GPU 与 TensorRT；**运行结果待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：把 `--maxKVCacheCapacity` 设得很大（比如 32768）会有什么影响？

**参考答案**：一方面允许更长的「输入+生成」上下文；另一方面，TensorRT 会在编译期为这么大的 KV cache 范围挑选/生成 kernel，构建时间变长，且运行时显存占用更高。它是一个「能力 vs. 资源」的权衡，应按实际场景取值。

**练习 2**：`llm_build.cpp` 为什么要先校验 `onnxDir/config.json` 存在？

**参考答案**：因为构建器内部（`LLMBuilder::parseConfig`）要从这份 `config.json` 读取模型维度（hidden size、KV head 数、层数等），才能正确设置优化 profile 的动态形状。这份 `config.json` 是导出阶段写出的，是「导出器→构建器」契约的一部分。缺它则构建无法进行。

**练习 3**：`llm_build` 与 `LLMBuilderConfig` 的默认 `maxBatchSize` 是多少？如果想让引擎支持批处理推理，该改哪个参数？

**参考答案**：默认都是 4（见 `LLMBuildArgs` 与 `LLMBuilderConfig` 的字段默认值）。要支持更大批处理，调大 `--maxBatchSize`；注意它会与 `--maxInputLen`、`--maxKVCacheCapacity` 一起决定编译期的形状范围和显存预算。

---

### 4.3 llm_inference：加载引擎跑推理

#### 4.3.1 概念说明

「推理」阶段加载上一阶段编译好的引擎，吃一个**请求 JSON**，吐一个**响应 JSON**。`llm_inference` 同样是一个命令行外壳，但它比 `llm_build` 复杂一些，因为它要串起：加载插件库 → 解析请求文件 → 构造统一运行时 → 捕获 CUDA graph → 逐请求调用 `handleRequest` → 收集响应并写成 JSON。

输入 JSON 的格式接近 OpenAI Chat Completions：顶层有 `batch_size`、采样参数（`temperature`/`top_p`/`top_k`）、`max_generate_length`，以及一个 `requests` 数组，每个请求含若干 `messages`（`role` + `content`）。输出 JSON 则在 `responses` 数组里给出每个请求的 `output_text`、`finish_reason`、原始 `messages` 等。

运行时的核心是 `LLMInferenceRuntime` 这个**统一入口**：它既能当纯 vanilla（普通自回归）运行时用，也能在传入「草稿配置」后变成投机解码运行时（base + draft 双引擎）。本讲用最简单的 vanilla 路径，投机解码留到 u7。

> 重要设计：运行时默认用 **non-blocking CUDA stream**（`cudaStreamNonBlocking`），并尝试**捕获 CUDA graph** 加速解码循环。这两点对边缘低延迟很关键。

#### 4.3.2 核心流程

`llm_inference` 的 `main()` 流程（仅看 vanilla 主路径，省略 TTS/投机解码分支）：

```text
解析命令行参数 → LLMInferenceArgs（校验 inputFile/engineDir/outputFile 必填）
        │
        ▼
loadEdgellmPluginLib()           # 动态加载自定义插件共享库
        │
        ▼
parseInputFile()                  # 把 input.json 解析成 loraWeightsMap + batchedRequests
        │
        ▼
创建 non-blocking CUDA stream
        │
        ▼
根据是否 --specDecode，构造 LLMInferenceRuntime（vanilla 或 spec 两套构造函数）
        │
        ▼
runtime->captureDecodingCUDAGraph(stream)   # 尝试捕获解码 CUDA graph
        │
        ▼
（可选）warmup 预热
        │
        ▼
for 每个请求：
    runtime->handleRequest(request, response, stream, ...)   # 真正的推理
    把 response 汇总进 outputData["responses"]
        │
        ▼
把 outputData 写入 outputFile（pretty JSON）
```

#### 4.3.3 源码精读

**① 参数与必填校验**

`LLMInferenceArgs` 集中了所有参数，其中 `inputFile`、`engineDir`、`outputFile` 三个是必填，`parseLLMInferenceArgs` 里做了校验：

[examples/llm/llm_inference.cpp:371-393](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_inference.cpp#L371-L393) —— 校验 `inputFile`、`engineDir`、`outputFile` 均非空，否则报错返回 false（main 里会打印用法并退出）。

注意：采样参数（`temperature`/`top_p`/`top_k`）**不通过命令行传**，而是写在输入 JSON 里；命令行只能覆盖 `--batchSize`、`--maxGenerateLength`、`--numLogprobs` 三项。这是初学者容易踩的点。

**② 解析请求文件**

输入文件由共享解析器 `exampleUtils::parseRequestFile` 处理，返回一个「LoRA 权重映射」和「分批后的请求列表」：

[examples/llm/llm_inference.cpp:467-512](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_inference.cpp#L467-L512) —— `parseInputFile` 是 `exampleUtils::parseRequestFile` 的薄封装，把输入 JSON 解析成 `loraWeightsMap` 和 `batchedRequests`（TTS 相关字段在这里顺带读取）。

**③ 构造运行时（vanilla vs spec）**

`main()` 根据 `--specDecode` 是否开启，选择不同的 `LLMInferenceRuntime` 构造函数。vanilla 路径用的是「四参数」构造函数：

[examples/llm/llm_inference.cpp:560-600](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_inference.cpp#L560-L600) —— 创建 non-blocking CUDA stream；若开启投机解码则用「五参数」构造函数（多一个 `draftingConfig`），否则用 vanilla「四参数」构造函数；随后调用 `runtime->captureDecodingCUDAGraph(stream)` 尝试捕获解码 CUDA graph，失败只警告不中止。

这两个构造函数在运行时头文件里有声明，并且注释明确：**不带 draft 配置构造时，运行时是纯 vanilla、零 draft 模型内存开销**：

[cpp/runtime/llmInferenceRuntime.h:73-86](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.h#L73-L86) —— `LLMInferenceRuntime` 的两个构造函数：带 `SpecDecodeDraftingConfig` 的是投机解码构造，不带的是 vanilla-only 构造（注释强调「zero draft-model memory overhead」）。

**④ 真正的推理调用**

请求循环里，vanilla 主路径的推理就是一行 `handleRequest`：

[examples/llm/llm_inference.cpp:773-783](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_inference.cpp#L773-L783) —— 非 TTS 流式分支里，调用 `runtime->handleRequest(request, response, stream, args.enableAudioOutput)`，返回布尔表示成功/失败。

`handleRequest` 的签名与契约在头文件里有完整说明：吃请求、出响应、返回成功与否：

[cpp/runtime/llmInferenceRuntime.h:104-113](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.h#L104-L113) —— `handleRequest` 声明：输入 `LLMGenerationRequest`（含 prompt 与参数），输出 `LLMGenerationResponse`（生成 token 与文本），返回 bool；第四个参数 `outputThinkerEmbeddings` 默认 false（Omni 音频路径才会设 true）。

**⑤ 写输出 JSON**

循环结束后，把汇总的 `outputData` 以 pretty JSON 写入 `outputFile`：

[examples/llm/llm_inference.cpp:1164-1178](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_inference.cpp#L1164-L1178) —— 打开 `outputFile`，用 `outputData.dump(4)`（4 空格缩进）写出全部响应；失败则报错退出。每个 response 含 `output_text`、`request_idx`、`batch_idx`、`finish_reason`、原始 `messages` 等。

#### 4.3.4 代码实践

**实践目标**：为 4.2 构建出的引擎组装「输入 JSON + 推理命令」，解释字段；若在目标设备上已编译出 `llm_inference` 则实际执行。

**操作步骤**（参照 quick-start-guide 的 Run Inference 一节）：

1. 创建输入文件 `input.json`：

```bash
cat > $WORKSPACE_DIR/input.json << 'EOF'
{
    "batch_size": 1,
    "temperature": 1.0,
    "top_p": 1.0,
    "top_k": 50,
    "max_generate_length": 128,
    "requests": [
        {
            "messages": [
                { "role": "user", "content": "What is the capital of United States?" }
            ]
        }
    ]
}
EOF
```

这条 JSON 对应 [docs/source/user_guide/getting_started/quick-start-guide.md:220-238](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/user_guide/getting_started/quick-start-guide.md#L220-L238)。

> 提示：仓库 `tests/test_cases/` 下也有现成的示例输入（如 `llm_basic.json`），可以直接拿来用。

2. 执行推理：

```bash
./build/examples/llm/llm_inference \
    --engineDir $WORKSPACE_DIR/$MODEL_NAME/engines \
    --inputFile $WORKSPACE_DIR/input.json \
    --outputFile $WORKSPACE_DIR/output.json
```

这条命令对应 [docs/source/user_guide/getting_started/quick-start-guide.md:248-252](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/user_guide/getting_started/quick-start-guide.md#L248-L252)。

**字段/参数解释**：

| 字段/参数 | 含义 |
| --- | --- |
| `batch_size` | 一次推理的批大小，受构建期 `--maxBatchSize` 约束 |
| `temperature` / `top_p` / `top_k` | 采样参数，**写在 JSON 里而非命令行** |
| `max_generate_length` | 最多生成多少 token，受 `--maxKVCacheCapacity` 约束 |
| `messages` | 多轮对话消息，格式接近 OpenAI |
| `--engineDir` | 4.2 产出的引擎目录 |
| `--inputFile` / `--outputFile` | 输入请求 JSON / 输出响应 JSON |

**需要观察的现象 / 预期结果**：

- 控制台打印 `Successfully parsed N batches of requests`，随后逐请求打印进度。
- `output.json` 里 `responses[0].output_text` 应给出类似 "The capital of the United States is Washington, D.C." 的回答，`finish_reason` 为 `stop`。完整结构见 [docs/source/user_guide/getting_started/quick-start-guide.md:263-287](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/user_guide/getting_started/quick-start-guide.md#L263-L287)。
- 若缺 `--inputFile` / `--engineDir` / `--outputFile`，会命中源码 ① 的校验并报错退出。

> 实际执行同样需要目标设备与匹配的 GPU/TensorRT；**运行结果待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `temperature` / `top_p` / `top_k` 要写在输入 JSON 里，而 `--batchSize` 能用命令行覆盖？

**参考答案**：见参数注释——只有 `batchSize`、`maxGenerateLength`、`numLogprobs` 三项支持命令行覆盖；采样参数（temperature/top_p/top_k）必须写在输入 JSON 里。设计上，输入 JSON 让「一个场景自描述」，便于复现和批量测试；命令行覆盖只保留少数最常调整的运行参数。

**练习 2**：`llm_inference` 构造运行时时，为什么用 `cudaStreamNonBlocking`？

**参考答案**：non-blocking stream 允许主机和设备、或多个流之间并行，避免推理过程中阻塞主机；这对边缘低延迟场景（以及后续流式输出、多组件流水线）很重要。源码里用 `cudaStreamCreateWithFlags(&stream, cudaStreamNonBlocking)` 创建。

**练习 3**：vanilla 推理路径里，`handleRequest` 返回 false 意味着什么？程序会怎样？

**参考答案**：返回 false 表示该请求处理失败。程序不会立刻退出，而是把 `hasFailedRequest` 置真、计数 `failedCount`，并在响应里写错误信息；所有请求处理完后，`main` 最终返回 `EXIT_FAILURE`（见文件末尾 `return hasFailedRequest ? EXIT_FAILURE : EXIT_SUCCESS`）。也就是说它「尽力处理所有请求，但只要有失败就以失败码退出」。

---

## 5. 综合实践

把三个阶段串起来，完成一次最小化的端到端跑通。建议你准备一份「实验记录表」，每个阶段记下：**命令、关键参数、产出目录、产出文件、耗时/状态**。

**任务**：用 Qwen3-0.6B 走完 export → build → inference，回答一个问题。

1. **导出**（x86 开发机）：执行 4.1.4 的 `tensorrt-edgellm-export` 命令，确认 `onnx/llm/model.onnx` 生成，记录其大小和生成的 sidecar 文件清单。
2. **构建**（目标设备或开发机）：执行 4.2.4 的 `llm_build` 命令。尝试把 `--maxKVCacheCapacity` 从 4096 改成 2048，**预测**它会如何影响后续能生成的最大 token 数，然后验证你的预测。
3. **推理**（同构建设备）：执行 4.3.4 的 `llm_inference` 命令，查看 `output.json`。
4. **横向对比**：把同一个 `input.json` 里的 `temperature` 改成 0.0（近似 greedy）和 1.0 各跑一次，观察 `output_text` 的稳定性差异。
5. **走读源码**：对照 4.3.3 的四个代码点，在 `llm_inference.cpp` 里从 `main` 走到 `handleRequest`，用自己的话写一段「从输入 JSON 到输出 JSON 经过了哪些关键步骤」。

**验收标准**：

- 能画出一张包含三个阶段、每阶段输入输出文件类型（`.safetensors`→`.onnx`→`.engine`→文本）的流程图。
- 能解释「为什么 engine 不能从开发机直接拷到任意边缘设备运行」。
- 能指出采样参数应写在 JSON 而非命令行。

> 若当前环境无 GPU，第 1–4 步的执行结果标注「待本地验证」，但第 5 步（源码走读）与流程图绘制可以在任何环境完成。

---

## 6. 本讲小结

- TensorRT Edge-LLM 的流水线在命令行层面就是三步：`tensorrt-edgellm-export`（导出 ONNX）→ `llm_build`（编译引擎）→ `llm_inference`（跑推理）。
- **导出**阶段按 `model_type` 分发组件，纯 LLM 只导出 `thinker`；`_export_llm` 用 `AutoModel.from_pretrained` 加载权重、用 `export_onnx` 写出一张「同时覆盖 prefill 和 decode」的图，外加 `embedding.safetensors`、`config.json`、分词器等 sidecar。
- **构建**阶段是 `llm_build`（参数外壳）→ `LLMBuilderConfig` → `LLMBuilder::build()`；三个关键约束参数 `--maxBatchSize` / `--maxInputLen` / `--maxKVCacheCapacity` 决定引擎的输入范围与显存预算，且 engine 不可跨 GPU/版本移植。
- **推理**阶段：`llm_inference` 加载插件库 → 解析输入 JSON → 用 non-blocking stream 构造 `LLMInferenceRuntime`（vanilla 四参数 / spec 五参数）→ 捕获 CUDA graph → 逐请求 `handleRequest` → 写输出 JSON。
- 采样参数写在输入 JSON 里，命令行只能覆盖 `batchSize` / `maxGenerateLength` / `numLogprobs`。
- 输入/输出 JSON 格式接近 OpenAI Chat Completions，便于从其它生态迁移。

---

## 7. 下一步学习建议

本讲让你「跑通」了流水线，但每个阶段的内部都还藏着大量机制。建议按数据流方向继续深入：

1. **Python 导出前端（u2 系列）**：想搞懂 `AutoModel.from_pretrained` 如何按 `model_type` 分发、默认解码器模型如何用自定义算子从零搭图、权重如何加载与重排——从 u2-l1（config 解析）开始。
2. **C++ 引擎构建器（u4 系列）**：想搞懂 `LLMBuilder::build()` 的「八阶段流程」、为什么需要 prefill/decode 双优化 profile——从 u4-l1 开始。
3. **C++ 运行时核心（u5 系列）**：想搞懂 `handleRequest` 内部如何切换 prefill/decode profile、KV cache 如何管理、采样如何做——从 u5-l1（`LLMInferenceRuntime` 与 `handleRequest`）开始。
4. **量化（u3 系列）**：如果你在 4.1 想用一个已量化的检查点（NVFP4/AWQ）而非 FP16，先读 u3 了解量化是导出的前置可选步骤。

一个推荐的「首跑」里程碑：先确保你能在本机/目标设备上完整复现本讲的 Qwen3-0.6B 三步流程，再带着「这张 ONNX 图里到底有哪些自定义算子」「引擎编译时为 prefill 和 decode 各做了什么优化」这两个问题进入 u2 与 u4。
