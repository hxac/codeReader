# 仓库结构与三段式流水线

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出仓库里每一个顶层目录（`tensorrt_edgellm/`、`cpp/`、`examples/`、`experimental/`、`kernelSrcs/`、`tests/`、`unittests/`、`3rdParty/` 等）各自负责什么。
- 用一句话概括 TensorRT Edge-LLM 的「三段式流水线」：从 HuggingFace 检查点，到 Python 导出的 ONNX，到 C++ 构建出的 TensorRT engine，再到 C++ 运行时的推理输出。
- 区分**量化、导出、构建、运行**四个阶段各自的输入和产物，并能说出每一步对应的文件类型（safetensors / ONNX / engine / 文本 token）。
- 看到「检查点 → ONNX → engine → 推理」这条链路时，知道每一段由仓库的哪一部分负责。

本讲不要求你动手编译或运行任何东西，重点是**建立一张全局地图**，让你在后续深入源码时不迷路。

## 2. 前置知识

本讲承接 [u1-l1 项目总览](u1-l1-project-overview.md) 建立的认知：TensorRT Edge-LLM 是 NVIDIA 面向边缘设备（Jetson、DRIVE、DGX Spark）的**纯 C++ LLM/VLM 推理运行时**，只做推理、不做训练。在进入源码之前，先理解三个基础概念：

1. **检查点（checkpoint）**：训练完成后保存的模型权重，通常是 HuggingFace 格式，权重文件后缀为 `.safetensors`，外加一份 `config.json` 描述模型结构。本项目的起点就是这种检查点。

2. **ONNX**：一种开放的神经网络交换格式（`.onnx` 文件）。它把「模型结构」用一张标准化的计算图表示，独立于任何训练框架。ONNX 在本流水线里扮演**中间产物**：Python 侧把检查点转成 ONNX，C++ 侧再把 ONNX 编译成最终引擎。

3. **TensorRT engine**：NVIDIA TensorRT 把 ONNX 图针对特定 GPU 架构（SM）做层层优化（算子融合、内核选择、精度调整）后产出的二进制文件（通常叫 `*.engine`）。它**不可跨 GPU 型号、不可跨 TensorRT 版本**移植，只能在「构建它的那台机器/那张卡」上运行。

> 一句话记住：**检查点是原料，ONNX 是设计图，engine 是按图造好的、只适配某张卡的发动机，运行时是发动它的人。**

如果你对 ONNX 或 TensorRT 完全陌生，不用紧张——本讲只需要你知道它们「是某种文件」即可，细节会在后续进阶讲义展开。

## 3. 本讲源码地图

本讲不进入复杂源码，只看三份「说明书性质」的文件，它们恰好从不同粒度描述了仓库结构：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/README.md) | 面向所有读者的项目门面：定位、特性、文档入口、用例。 |
| [AGENTS.md](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/AGENTS.md) | 面向开发者的「架构速查表」，用几行话点明了流水线、C++ 子包和 Python 包的职责，是本讲最重要的参考。 |
| [docs/source/overview.md](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/overview.md) | 官方总览文档，里面有一张**官方的三段式流水线 mermaid 图**和一张组件表。 |

掌握这三个文件，你就能不依赖记忆、随时核对仓库结构。

---

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：**4.1 仓库目录** 和 **4.2 流水线图**。前者回答「东西放在哪」，后者回答「东西怎么流动」。

### 4.1 仓库目录

#### 4.1.1 概念说明

一个同时包含 Python、C++、CUDA 的大型推理框架，代码量很大。TensorRT Edge-LLM 的组织原则是：**按流水线阶段分目录，而不是按编程语言杂乱堆放**。这样你只要知道「我现在处在流水线的哪一段」，就能直接定位到对应目录。

核心对应关系是：

- 流水线的**第一段（Python 导出）** → `tensorrt_edgellm/` 目录（Python 包）。
- 流水线的**第二段（C++ 引擎构建器）** → `cpp/builder/`（C++ 编译器）。
- 流水线的**第三段（C++ 运行时）** → `cpp/runtime/`（C++ 推理引擎）。
- 把三段串起来给人看的**示例程序** → `examples/`（C++ 可执行程序）。
- 高性能算子的**原始 CUDA/CuTe-DSL 源码** → `kernelSrcs/`。

#### 4.1.2 核心流程

下面这张表是本讲最重要的产出——它把仓库顶层目录和流水线阶段一一对应。请对照仓库根目录阅读。

| 顶层目录 | 中文一句话说明 | 在流水线中的位置 |
|----------|----------------|------------------|
| `tensorrt_edgellm/` | Python 导出前端：读取 HF 检查点、可选量化、导出 ONNX | 第一段（导出，运行在 x86 开发机） |
| `cpp/` | C++ 运行时与构建器主体（内含 builder/runtime/plugins/kernels 等） | 第二段（构建）+ 第三段（运行，运行在边缘设备） |
| `examples/` | C++ 示例可执行程序，把「构建」「推理」做成命令行工具给人用 | 贯穿第二、三段的入口 |
| `experimental/` | 实验性高层 Python API（vLLM 风格）与 OpenAI 兼容服务端、pybind 绑定 | 包装第三段（运行时）的便捷接口 |
| `kernelSrcs/` | 高性能 CUDA / CuTe-DSL 算子的原始源码（FMHA、MoE、GEMM 等），按 SM 架构构建 | 支撑第二、三段的底层算子 |
| `tests/` | Python 集成测试 + YAML 测试列表，按 GPU/平台参数化 | 验证整条流水线 |
| `unittests/` | C++ GTest 单元测试（约 40 个文件，无需模型即可跑） | 验证 C++ 各组件 |
| `3rdParty/` | 第三方依赖：googletest、nlohmann/json、NVTX（git 子模块）+ miniaudio、stb | 构建依赖 |
| `docs/` | 文档源码（Sphinx/rst，构建出在线文档站） | 说明文档 |
| `scripts/` | 开发辅助脚本（如覆盖率统计 `run_coverage.sh`） | 开发工具 |
| `cmake/` | CMake 辅助模块（如 `CuteDslFMHA.cmake`） | 构建辅助 |
| 根目录配置文件 | `CMakeLists.txt`（C++ 构建）、`pyproject.toml`（Python 包与 CLI 命令）、`requirements*.txt`（依赖）、`.gitmodules`（子模块） | 构建与打包 |

其中 `cpp/` 是最大的目录，它内部又按职责拆成若干子包（这点非常关键，后续多讲都会用到）：

| `cpp/` 子包 | 职责 |
|-------------|------|
| `runtime/` | 推理运行时核心（统一入口 `LLMInferenceRuntime` + `handleRequest()`） |
| `builder/` | ONNX → TensorRT engine 的构建器 |
| `plugins/` | 自定义 TensorRT 插件（attention、MoE、mamba 等） |
| `kernels/` | CUDA 算子的 C++ 包装（FMHA/RoPE/MoE/Mamba/EAGLE） |
| `common/` | 公共抽象（张量 `tensor.h`、日志、工具） |
| `tokenizer/` | 分词器（BPE 编解码） |
| `sampler/` | GPU 采样（top-k/top-p/temperature） |
| `multimodal/` | 多模态 runner（视觉/音频编码器） |
| `profiling/` | 性能剖析（NVTX 等） |
| `action/` | VLA（视觉-语言-动作）模型支持 |

#### 4.1.3 源码精读

仓库结构最权威的一句话来自 AGENTS.md 的 Architecture 小节，它直接点明了流水线与 `cpp/` 的关系：

- [AGENTS.md:L48](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/AGENTS.md#L48) — 这一行用一句话定义了整条流水线：`HuggingFace Model → Python Export (quantize + ONNX) → C++ Engine Builder (TRT engine) → C++ Runtime (inference)`。这是本讲所有内容的主轴。

- [AGENTS.md:L50-L52](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/AGENTS.md#L50-L52) — 这几行说明 `cpp/` 用一个统一的 `LLMInferenceRuntime` 类承担所有推理（通过 `handleRequest()`），并按可插拔的 `DecodingStrategy` 层切换普通解码与投机解码；同时列出了 C++ 子包 `common/ kernels/ plugins/ builder/ tokenizer/ multimodal/ profiling/ sampler/`。

- [AGENTS.md:L54-L63](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/AGENTS.md#L54-L63) — 这几行说明 `tensorrt_edgellm/` 是「基于检查点的导出前端」：它**从零用 ONNX 内置算子 + 自定义算子重新实现模型结构**，直接读取稳定的 HF 权重，而不是去 trace 不稳定的 HF FX 计算图。这是它区别于很多其他导出工具的核心设计动机，并列举了 `model.py`、`config.py`、`checkpoint/loader.py`、`onnx/export.py` 等关键文件。

- [overview.md:L50](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/overview.md#L50) — 「Key Components」开头一行用一句话标注了各组件的代码位置：量化在 `tensorrt_edgellm/quantization/`、导出在 `tensorrt_edgellm/`、Python API/服务端在 `experimental/server/`、运行时在 `cpp/`、示例在 `examples/`。这张「位置清单」和本节的目录表完全一致。

理解的关键点：**为什么 `tensorrt_edgellm/` 要「从零重新实现模型」而不是 trace HF 图？** 因为 HF 的 FX 计算图会随 PyTorch / transformers 版本剧烈变化，导出不稳定；而检查点里的权重张量名是稳定的。所以本项目选择「直接读权重 + 自己用算子搭结构」，换来一条**可重复、可维护**的导出路径。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**（无需运行）。

**实践目标**：不靠记忆，自己从仓库文件中验证目录职责。

**操作步骤**：

1. 在仓库根目录执行 `ls`，把所有顶层目录与根目录配置文件（`CMakeLists.txt`、`pyproject.toml`、`requirements.txt`、`.gitmodules`）列出来。
2. 打开 `AGENTS.md` 的 Architecture 小节（约第 44–63 行），核对「Key Files」表里的路径是否真实存在。
3. 打开 `tensorrt_edgellm/scripts/`，确认它确实包含 `export.py`、`quantize.py`、`insert_lora.py`、`merge_lora.py`、`process_lora_weights.py`、`reduce_vocab.py`——这六个文件就是六个 CLI 命令的入口（详见下一讲 u1-l4）。
4. 打开 `examples/llm/`，确认存在 `llm_build.cpp`（构建器示例）和 `llm_inference.cpp`（运行时示例）——它们就是流水线第二、三段最直观的「可执行入口」。

**需要观察的现象**：`examples/llm/` 里的 `llm_build.cpp` 对应「ONNX → engine」，`llm_inference.cpp` 对应「engine → 推理输出」——两个文件名直接对应流水线的两段。

**预期结果**：你能不查文档，指认出「导出用哪个目录、构建用哪个示例、运行用哪个示例」。

> 待本地验证：如果你手头能 `ls`，请亲自执行；若无法访问仓库，则按上表逐项确认即可。

#### 4.1.5 小练习与答案

**练习 1**：如果你要修改一个模型的**导出逻辑**（比如让它在导出 ONNX 时多输出一个张量），你会去哪个目录改代码？

> **参考答案**：`tensorrt_edgellm/`（Python 导出前端）。具体到模型结构实现可能在 `tensorrt_edgellm/models/<架构>/`，导出编排则在 `tensorrt_edgellm/scripts/export.py` 与 `tensorrt_edgellm/onnx/export.py`。

**练习 2**：为什么 `3rdParty/` 里的 googletest、nlohmann/json、NVTX 是 git 子模块（见 `.gitmodules`），而不是直接拷进仓库？

> **参考答案**：它们是独立的第三方项目，用子模块可以保持上游可更新、避免把大量无关代码塞进主仓库历史，也方便锁定到特定版本。这三个子模块分别服务：googletest（C++ 单测框架）、nlohmann/json（C++ JSON 解析）、NVTX（性能剖析标注）。

**练习 3**：`unittests/`（C++ 单测）和 `tests/`（Python 集成测试）的运行代价有什么本质区别？

> **参考答案**：`unittests/` 是纯 C++ GTest，不依赖模型文件、不一定要 GPU，跑得快；`tests/` 是 YAML 驱动的端到端集成测试，需要真实 GPU、真实模型检查点，且要跑完整「export → build → inference」，代价大得多。

---

### 4.2 流水线图

#### 4.2.1 概念说明

「流水线」描述的是**数据如何变换形态、一步步从原料变成最终输出**。TensorRT Edge-LLM 的流水线有四个关键产物，每一种都是不同的文件类型：

1. **检查点**（`.safetensors` + `config.json`）—— 起点，原料。
2. **ONNX**（`.onnx` 计算图）—— 第一段产物，中间设计图。
3. **TensorRT engine**（`.engine` 二进制）—— 第二段产物，针对特定卡优化好的发动机。
4. **推理输出**（生成的 token / 文本）—— 第三段产物，最终结果。

理解流水线的核心是：**每一段都只接受上一段的产物，并产出下一段需要的输入**，各段之间通过文件解耦。这意味着你可以单独重跑某一段而不必从头开始（比如改了量化参数要重跑导出，但改了运行时只需要重新构建而不必重新导出 ONNX——前提是 ONNX 没变）。

#### 4.2.2 核心流程

官方在 overview.md 里把流水线画成一张 mermaid 图。用文字描述它的主轴如下（与官方图一一对应）：

```text
HuggingFace 检查点              ← 原料：*.safetensors + config.json
        │
        ▼
(1) Python 导出                  ← 在 x86 开发机上跑，代码在 tensorrt_edgellm/
   可选：先量化 (quantization/)   ← 产出：仍是 HF 风格检查点，但权重已被量化
   导出 ONNX                      ← 产物：*.onnx 计算图 + tokenizer/config 等 sidecar
        │
        ▼
(2) C++ 引擎构建器                ← 在边缘设备上跑，代码在 cpp/builder/，入口示例 examples/llm/llm_build.cpp
   把 ONNX 编译成 engine          ← 产物：*.engine（绑定特定 GPU 型号 + 特定 TensorRT 版本）
        │
        ▼
(3) C++ 运行时                    ← 在边缘设备上跑，代码在 cpp/runtime/，入口示例 examples/llm/llm_inference.cpp
   加载 engine 做推理             ← 产物：生成的 token 序列 / 文本
        │
        ▼
   应用（汽车 / 机器人 / 工业 IoT）
```

四个阶段的产物与归属一览：

| 阶段 | 运行位置 | 负责代码 | 产物文件类型 |
|------|----------|----------|--------------|
| 量化（可选） | x86 开发机 | `tensorrt_edgellm/quantization/` | 仍是 `.safetensors` 检查点（权重被量化） |
| 导出 | x86 开发机 | `tensorrt_edgellm/`（scripts + onnx + models） | `.onnx` 计算图 + sidecar（tokenizer、config、chat template） |
| 构建 | 边缘设备 | `cpp/builder/`（示例 `examples/llm/llm_build.cpp`） | `.engine` 二进制 |
| 运行 | 边缘设备 | `cpp/runtime/`（示例 `examples/llm/llm_inference.cpp`） | 生成的 token / 文本 |

> 注意「量化」与「导出」的细微区别：量化的产物**仍然是 HuggingFace 风格的检查点**（只是权重变成了 fp8/nvfp4/int4 等低精度表示），然后再交给导出阶段转 ONNX。所以量化是导出的**前置可选步骤**，二者都属于流水线第一段（Python 侧）。这是初学者最容易混淆的一点。

#### 4.2.3 源码精读

- [overview.md:L52-L86](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/overview.md#L52-L86) — 这是官方的三段式流水线 mermaid 图（第 52 行写明 "uses a three-stage pipeline"，第 54–86 行是 `graph LR` 图）。图里清晰地标出了 `HuggingFace Models → Checkpoint-Based Model Exporter → ONNX → Engine Builder → TensorRT Engines → C++ Runtime → Examples → Applications` 这条主轴。**这是本讲最值得对着原图看一遍的参考。**

- [overview.md:L88-L95](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/overview.md#L88-L95) — 紧跟在图后的「组件表」，逐项说明每个组件（Quantization Package、Checkpoint Exporter、Experimental Python API、Engine Builder、C++ Runtime、Examples）的作用，并给出对应的详细文档链接。

- [AGENTS.md:L48](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/AGENTS.md#L48) — 同一条流水线在 AGENTS.md 里的一句话版定义，便于记忆。

- [README.md:L24](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/README.md#L24) — README 的 Overview 段落，说明 Python 脚本把 HF 检查点转成 ONNX，「Engine build and end-to-end inference runs entirely on Edge platforms」。这句话点明了一个关键设计取舍：**导出在 x86 上做，构建与推理必须回到边缘设备上做**（因为 engine 绑定具体卡）。

#### 4.2.4 代码实践

这是一个**阅读 + 画图型实践**（无需运行）。

**实践目标**：把官方的 mermaid 流程图内化成自己能默写的文字流程。

**操作步骤**：

1. 打开 [overview.md 的 mermaid 图](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/overview.md#L54-L86)，数一数图里一共有几个节点（提示：8 个）。
2. 在纸上（或文本里）把这张图重新画一遍，但**额外标注每两个节点之间的产物文件类型**（如「PYTHON_EXPORT → ONNX_MODEL 之间产出 `*.onnx`」）。
3. 思考并标注：哪些段在 x86 开发机上运行？哪些段在边缘设备上运行？（答案见上一节表格）

**需要观察的现象**：你会发现 mermaid 图里的「PYTHON_EXPORT」「ENGINE_BUILDER」「CPP_RUNTIME」三个绿色节点，正好对应仓库里的 `tensorrt_edgellm/`、`cpp/builder/`、`cpp/runtime/` 三个位置——目录划分与流水线阶段严格对应。

**预期结果**：你能不看任何资料，画出「检查点 → ONNX → engine → 推理输出」四步图，并为每一步标出产物文件类型与运行位置。

> 待本地验证：若你能在 GitHub 上打开 overview.md，请对照原图确认你的手绘图节点数一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么「构建」和「运行」必须在边缘设备上做，而不能在 x86 开发机上做？

> **参考答案**：因为 TensorRT engine 是**针对特定 GPU 型号（SM 架构）和特定 TensorRT 版本**优化的二进制，不跨平台移植。边缘设备（Jetson/DRIVE/Spark）的 GPU 架构与 x86 开发机上的独立显卡不同，所以在 x86 上构建出的 engine 拿到边缘设备上跑不了。导出（ONNX）则在 x86 上做，因为 ONNX 是平台无关的中间格式。

**练习 2**：假设你已经导出了 ONNX，但还没有构建 engine。此时如果只修改了 C++ 运行时（`cpp/runtime/`）的解码逻辑，你需要重新执行流水线的哪几段？

> **参考答案**：只需要重新「构建」和「运行」两段。ONNX 没变就不必重新导出；但运行时代码变了，必须重新编译 `cpp/` 并重新构建（或直接复用已有 engine，只要运行时接口没变）。「运行」段的产物是文本，每次推理都会产生。

**练习 3**：量化的产物和导出的产物，文件类型分别是什么？

> **参考答案**：量化的产物**仍然是 `.safetensors` 检查点**（只是权重被量化成低精度），它还是 HF 风格的检查点；导出的产物是 **`.onnx` 计算图**（外加 tokenizer、config、chat template 等 sidecar）。两者都属于流水线第一段（Python 侧）。

---

## 5. 综合实践

把本讲两个最小模块串起来，完成下面这个任务（这正是本讲规格里要求的实践）：

**任务**：为仓库**每一个顶层目录**写一句中文说明，并画出一张「检查点 → ONNX → engine → 推理」的流程图，**标注每一步产出的文件类型**。

**建议产出格式**：

```markdown
### 目录速查
- tensorrt_edgellm/：……（一句中文）
- cpp/：……
- examples/：……
- experimental/：……
- kernelSrcs/：……
- tests/ / unittests/：……
- 3rdParty/：……
（其余顶层目录各自一句）

### 流水线图
检查点(*.safetensors) --导出(tensorrt_edgellm/)--> ONNX(*.onnx)
ONNX(*.onnx) --构建(cpp/builder/, examples/llm/llm_build)--> engine(*.engine)
engine(*.engine) --运行(cpp/runtime/, examples/llm/llm_inference)--> 文本输出
```

**验证方法**：

1. 把你写的目录说明和本讲 4.1.2 节的表格对照，检查是否遗漏重要目录。
2. 把你画的流程图和 [overview.md 的官方 mermaid 图](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/overview.md#L54-L86) 对照，检查节点和产物是否齐全。
3. 自查三个判断题：
   - 量化产物是不是 ONNX？（应为「不是，仍是 safetensors 检查点」）
   - engine 能不能从 x86 拷到 Jetson 直接用？（应为「不能」）
   - 导出和构建，哪个在 x86、哪个在边缘？（应为「导出在 x86，构建在边缘」）

如果你能独立完成这张图并答对三道自查题，本讲就达标了。

## 6. 本讲小结

- 仓库**按流水线阶段分目录**：Python 导出在 `tensorrt_edgellm/`，C++ 构建在 `cpp/builder/`，C++ 运行在 `cpp/runtime/`，示例入口在 `examples/`，原始 CUDA 算子在 `kernelSrcs/`。
- 三段式流水线的主轴是：**HuggingFace 检查点 → Python 导出(ONNX) → C++ 引擎构建(engine) → C++ 运行时(推理)**。
- 四个关键产物文件类型依次是：`.safetensors`（检查点）→ `.onnx`（计算图）→ `.engine`（二进制）→ 文本 token（输出）。
- 量化是导出的**前置可选步骤**，它的产物仍是 HF 风格检查点（不是 ONNX）。
- 导出在 x86 开发机上做，**构建和运行必须在边缘设备上做**，因为 engine 绑定特定 GPU 型号与 TensorRT 版本。
- 官方权威描述集中在 `AGENTS.md` 的 Architecture 小节与 `docs/source/overview.md` 的流水线图——遇到不确定时回查这两处即可。

## 7. 下一步学习建议

建立全局地图后，下一讲建议学习：

- **[u1-l3 构建系统与依赖](u1-l3-build-system-and-dependencies.md)**：弄清 `CMakeLists.txt`、`pyproject.toml`、git 子模块、`LD_LIBRARY_PATH` 这些「如何把代码变成可运行程序」的机制，为真正动手做准备。
- **[u1-l4 CLI 入口与包导出](u1-l4-cli-entry-points.md)**：搞懂六个 `tensorrt-edgellm-*` 命令分别对应哪个脚本入口，以及 Python 包对外暴露了哪些 API。
- **[u1-l5 端到端流水线实战](u1-l5-end-to-end-pipeline-walkthrough.md)**：用一个最小模型把 export → build → inference 真正跑通，把本讲的「文件类型流转」变成可见的命令输出。

如果想提前加深对某一目录的理解，可以直接打开 `AGENTS.md` 的「Key Files」表与「Architecture」小节反复对照——它是最浓缩的导航地图。后续进阶单元（u2 Python 导出前端、u4 C++ 构建器、u5 C++ 运行时核心）会逐一深入这些目录。
