# 项目定位与核心概念

## 1. 本讲目标

本讲是整本学习手册的第一篇，面向**完全没接触过 llm-compressor** 的读者。读完本讲，你应当能够：

- 说清楚 **llm-compressor 是什么**、它要解决什么问题，以及它和 **vLLM**、**compressed-tensors** 之间的关系。
- 识别项目支持的**精度方案**（W8A8 / FP8 / NVFP4 / W4A16 等）和**压缩算法**（GPTQ / AWQ / SmoothQuant / AutoRound / REAP 等）。
- 建立对 **recipe / modifier / oneshot** 这三个核心概念的初步印象，为后续逐层深入源码打好基础。

本讲**不要求你读懂源码细节**，重点是建立全局地图。后续每一讲都会从某个具体模块切入，逐步带你深入。

## 2. 前置知识

在开始之前，最好对以下几个概念有一点直观了解（不了解也没关系，本讲会顺带解释）：

- **大语言模型（LLM）**：以 Transformer 为基础架构的生成式语言模型，例如 Qwen、Llama、DeepSeek 等。它们参数量大，推理时对显存和带宽要求很高。
- **推理（Inference）**：模型训练好之后，接收输入文本、生成输出的过程。我们关心推理的**延迟（latency）**和**吞吐（throughput）**。
- **显存（VRAM）**：GPU 上的内存。模型权重和推理过程中的激活值、KV cache 都要占用显存。
- **量化（Quantization）**：把高精度数值（如 16 位浮点）用更少的位数（如 8 位、4 位）来表示，从而压缩模型体积、加速推理。
- **剪枝（Pruning）**：把模型中"不太重要"的权重或结构（如 MoE 中的某些专家）去掉，从而减少计算量。

如果你对上面任意一个名词完全陌生，没关系——本讲会在用到时用通俗的方式再解释一次。

## 3. 本讲源码地图

本讲主要阅读项目根目录下的文档，目的是建立全局认知。涉及的文件如下：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/README.md) | 项目首页。包含项目定位、支持精度/算法总览、安装方式、Quick Tour 示例。 |
| [docs/steps/why-llmcompressor.md](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/steps/why-llmcompressor.md) | 解释"为什么要用 llm-compressor"，给出量化收益、硬件成本、基本数学公式。 |
| [docs/steps/choosing-scheme.md](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/steps/choosing-scheme.md) | 精度方案选择指南。把每种方案映射到目标 GPU 架构与压缩格式。 |
| [src/llmcompressor/\_\_init\_\_.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/__init__.py) | 库的包入口。从这里可以看到对外暴露了哪些 API（如 `oneshot`）。 |

> 提示：本讲的"最小模块"是 `docs/steps`。这个目录下还有 `choosing-model.md`、`choosing-algo.md`、`choosing-dataset.md`、`compress.md`、`deploy.md` 等一份完整的"分步教程"，它们构成了项目官方推荐的学习路径。

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：

1. **4.1 项目定位与核心价值** —— llm-compressor 是什么、和谁配合。
2. **4.2 量化的基本数学直觉** —— 量化到底在做什么。
3. **4.3 支持的精度方案** —— W8A8 / FP8 / NVFP4 / W4A16 这些名字的含义。
4. **4.4 支持的压缩算法** —— GPTQ / AWQ / SmoothQuant / AutoRound / REAP 等做什么。
5. **4.5 三大核心概念：recipe / modifier / oneshot** —— 后续所有源码阅读都围绕这三个词展开。

---

### 4.1 项目定位与核心价值

#### 4.1.1 概念说明

llm-compressor 是一个**面向 vLLM 部署的大模型压缩库**。一句话概括它的职责：

> 给我一个 HuggingFace 格式的模型，再加上一份"压缩配方"，我帮你输出一个**体积更小、推理更快、并且能被 vLLM 直接加载**的量化/剪枝后的 checkpoint。

它并不负责"推理"本身，推理由 vLLM 完成；它的角色是**推理前的压缩预处理**。理解这个定位非常重要，因为它决定了整个项目的结构：输入是模型 + 配方，输出是符合 `compressed-tensors` 格式的可部署产物。

要理解它的价值，先看它要解决的痛点：

- 模型越来越大，**显存装不下**（一个 109B 参数的 BF16 模型大约要 220 GB，需要 3 张 GPU）。
- 全精度推理**延迟高、吞吐低**，生产环境成本高。
- 直接粗暴地降低精度，**精度损失又太大**，需要一个能"在体积/速度/精度三者间找平衡"的工具。

#### 4.1.2 核心流程

从用户视角看，llm-compressor 的端到端流程可以概括为：

```text
HuggingFace 模型  ──┐
                    ├──►  oneshot(model=..., recipe=...)  ──►  压缩后的模型
校准数据(可选) ─────┘                                            │
                                                                 ▼
                                                  model.save_pretrained(SAVE_DIR)
                                                                 │
                                                                 ▼
                                                   compressed-tensors 格式 checkpoint
                                                                 │
                                                                 ▼
                                                         vLLM 直接加载推理
```

注意几个关键点：

- **校准数据（calibration data）是可选的**。某些方案（如纯权重的 RTN、动态激活量化）不需要校准数据；而 GPTQ、AWQ 等算法需要少量校准样本来估计量化误差。
- **输出格式固定**：`compressed-tensors`，这是 llm-compressor 与 vLLM 之间的"契约"。
- **目标硬件决定可选方案**：不同 GPU 架构能加速的数值格式不同（详见 4.3）。

#### 4.1.3 源码精读

README 开头一句话直接定义了项目定位：

> [README.md:23-28](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/README.md#L23-L28) —— 把 `llmcompressor` 定义为"为 vLLM 部署优化模型"的库，并列出四大能力：丰富的量化算法、与 HuggingFace 无缝集成、输出 `compressed-tensors` 格式、支持 DDP 和磁盘 offloading。

`why-llmcompressor.md` 用一张表把项目收益讲得很清楚：

> [docs/steps/why-llmcompressor.md:9-16](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/steps/why-llmcompressor.md#L9-L16) —— 列出六大收益：降低硬件成本（显存减少 50–75%）、提升推理速度、保持精度、支持广泛模型（含多模态与 MoE）、产物生产可用（与 vLLM 直接对接）、算法灵活可选。

关于"为什么压缩能省钱"，文档给了一个直观的例子：

> [docs/steps/why-llmcompressor.md:24-30](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/steps/why-llmcompressor.md#L24-L30) —— 以一个 109B BF16 模型（约 220 GB，3 张 GPU）为例：量化到 INT8/FP8 约需 109 GB（2 张 GPU），量化到 INT4/FP4 约需 55 GB（1 张 GPU）。

最后，关于"产物格式"：

> [docs/steps/why-llmcompressor.md:60-62](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/steps/why-llmcompressor.md#L60-L62) —— 明确说明 llm-compressor 使用 `compressed-tensors` 格式保存模型，该格式同时兼容 vLLM 和 HuggingFace。

#### 4.1.4 代码实践

**实践目标**：用自己的话复述 llm-compressor 的定位，避免照抄原文。

**操作步骤**：

1. 阅读 [README.md:23-28](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/README.md#L23-L28) 和 [docs/steps/why-llmcompressor.md:9-16](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/steps/why-llmcompressor.md#L9-L16)。
2. 在笔记里用一句话回答三个问题：
   - llm-compressor 的**输入**是什么？
   - 它的**输出**是什么、保存成什么格式？
   - 它**不做**哪件事（提示：和推理有关）？

**需要观察的现象**：你会发现自己能脱稿回答，说明定位已经清楚。

**预期结果**：参考答案——输入是"HuggingFace 模型 + 压缩配方（可选校准数据）"；输出是"`compressed-tensors` 格式的压缩 checkpoint"；它本身**不做推理**，推理交给 vLLM。

#### 4.1.5 小练习与答案

**练习 1**：llm-compressor 和 vLLM 的分工是什么？

> **答案**：llm-compressor 负责"压缩"（推理前的离线预处理），vLLM 负责"推理"（在线加载压缩后的模型并提供服务）。两者通过 `compressed-tensors` 格式衔接。

**练习 2**：把一个 109B BF16 模型量化到 FP4，大约能省多少显存？

> **答案**：约从 220 GB 降到 55 GB（约为原来的 1/4），可从 3 张 GPU 减到 1 张 GPU。

---

### 4.2 量化的基本数学直觉

#### 4.2.1 概念说明

在讨论具体方案之前，先建立"量化到底在做什么"的直觉。量化本质上是**用一个更小的数值范围去近似表示原来的高精度数值**。具体做法是：算出一个**缩放因子（scale）**和一个**零点偏移（zero-point）**，把原始值映射到低位整数。

#### 4.2.2 核心流程

`why-llmcompressor.md` 直接给出了量化公式：

\[ q = \mathrm{round}\!\left( \frac{x}{s} \right) + z \]

其中：

- \(x\) 是原始的高精度数值（如 FP16 权重）。
- \(s\) 是缩放因子（scale），决定"一个低位单位代表多少原始数值"。
- \(z\) 是零点偏移（zero point），用来对齐 0 的位置（对称量化时 \(z=0\)）。
- \(q\) 是量化后的低位整数（如 INT8）。

反量化（推理时还原）就是反过来：

\[ \hat{x} = s \cdot (q - z) \]

量化的**误差**来自 `round`（四舍五入）这一步。一个好的量化算法/方案，目标就是让 \(s\) 选得尽量合理，使得整体误差小、同时对硬件友好。

关于收益，文档这样描述：

> 量化能让模型内存占用降低 50–75%，并利用专门的 tensor core 加速计算。

#### 4.2.3 源码精读

> [docs/steps/why-llmcompressor.md:45-57](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/steps/why-llmcompressor.md#L45-L57) —— 解释量化通过计算 scale 和 zero-point 把高精度值映射到更小范围，并直接给出 `quantized_value = round(original_value / scale) + zero_point` 公式。

> [docs/steps/why-llmcompressor.md:39-41](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/steps/why-llmcompressor.md#L39-L41) —— 引用研究结论：恰当的量化对精度影响很小（如 DeepSeek-R1 全精度与量化版本精度差异小于 1%）。

#### 4.2.4 代码实践

**实践目标**：用一个最小 NumPy 片段亲手"量化"一组数，感受 scale 和误差。

**操作步骤**：

1. 在本地 Python 环境运行下面的**示例代码**（注意：这不是项目源码，仅为帮助理解公式的演示）：

   ```python
   import numpy as np

   x = np.array([0.12, -0.34, 0.88, -1.20, 0.05], dtype=np.float32)

   # 对称量化到 INT8：scale = max(|x|) / 127
   scale = np.max(np.abs(x)) / 127.0
   q = np.round(x / scale)            # 量化
   x_hat = q * scale                  # 反量化还原

   print("原始  :", x)
   print("量化后 :", q)
   print("还原后 :", x_hat)
   print("误差   :", np.abs(x - x_hat))
   ```

**需要观察的现象**：还原值 `x_hat` 与原始值 `x` 很接近但不完全相等；误差来自于 `round`。

**预期结果**：误差的量级与 `scale` 相当（每个数误差不超过 \(s/2\)）。**待本地验证**：实际打印的数值取决于你的 NumPy 版本，但定性结论应一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么对称量化可以令 \(z=0\)？

> **答案**：对称量化假设数值范围关于 0 对称（如 \([-127, 127]\)），0 本身就映射到 0，因此不需要零点偏移。

**练习 2**：如果 scale 选得过大，量化误差会变大还是变小？

> **答案**：变大。scale 越大，一个低位单位"代表"的原始值越大，`round` 引入的还原误差也越大。但 scale 过小又会超出低位能表示的范围（溢出）。所以选 scale 是量化算法的核心权衡。

---

### 4.3 支持的精度方案

#### 4.3.1 概念说明

"精度方案（scheme）"定义了**用什么数值格式、几位、作用于权重还是激活**。理解这些命名规则很关键，因为它们贯穿整个项目：

- 命名形如 **WxAy**：`W` 后的数字是**权重位数**，`A` 后的数字是**激活位数**。
  - 例如 **W8A8** 表示权重和激活都是 8 位；**W4A16** 表示权重 4 位、激活仍是 16 位（只压权重）。
- **INT / FP**：`INT` 是整数格式（如 INT8），`FP` 是浮点格式（如 FP8）。
- **Microscale（NVFP4 / MXFP4 / MXFP8）**：带"微缩放（microscaling）"的低位格式，按小组（group）再带一层缩放，适合最新的 Blackwell GPU。

#### 4.3.2 核心流程

选择方案的总体思路是：**先看硬件，再看需求**。`choosing-scheme.md` 给出了一条主线：

```text
选择模型 ──► 选择精度方案（看 GPU 架构） ──► 选择压缩算法
```

硬件之所以重要，是因为**只有 GPU 支持的数值格式才能被硬件加速**。例如 NVFP4 需要 Blackwell（SM100），FP8 在 Hopper（SM90）上能跑出最高吞吐，而老一些的 Turing/Ampere 上常用 INT8 或 W4A16。

#### 4.3.3 源码精读

README 的"Supported Precisions and Types"按用途分类列出了所有支持的精度：

> [README.md:77-81](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/README.md#L77-L81) —— 列出激活量化（W8A8 int8/fp8、W4AFP8、NVFP4/MXFP4/MXFP8）、混合精度（W4A16、W8A16、各种 MX/NV FPx+A16）、注意力与 KV cache 量化（FP8、NVFP4）、低位任意位（WNA4/WNA8/WNA16）。

`choosing-scheme.md` 用一张表把方案、精度、作用对象、所需 GPU 和最低算力对应起来：

> [docs/steps/choosing-scheme.md:11-20](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/steps/choosing-scheme.md#L11-L20) —— 方案对照表，例如 W4A16/W8A16（Turing, 算力≥7.5）、W8A8-INT8（Turing, ≥7.5）、W8A8-FP8（Lovelace, ≥8.9）、NVFP4/MXFP4（Blackwell, ≥10.0）等。

针对不同代际 NVIDIA GPU 的推荐方案：

> [docs/steps/choosing-scheme.md:29-47](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/steps/choosing-scheme.md#L29-L47) —— Blackwell 推荐 NVFP4/MXFP4；Hopper 推荐 W8A8-FP8；Ampere 推荐 W4A16；Turing 推荐 W8A8-INT8。

最后，每种方案对应一个 `compressed-tensors` 里的 compressor（决定权重/scale/zero-point 如何落盘）：

> [docs/steps/choosing-scheme.md:77-92](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/steps/choosing-scheme.md#L77-L92) —— 方案到 compressor 的映射表，如 W8A8-int 对应 `int_quantized`、W8A8-float 对应 `float_quantized`、W4A16-int 对应 `pack_quantized` 等。

> ⚠️ 注意：[docs/steps/choosing-scheme.md:93-94](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/steps/choosing-scheme.md#L93-L94) 明确说明：由于缺乏硬件支持和用户需求，**稀疏压缩（含 2:4 稀疏）已不再支持**。

#### 4.3.4 代码实践

**实践目标**：根据你（假设）的硬件，选定一个合适的方案。

**操作步骤**：

1. 打开 [docs/steps/choosing-scheme.md:11-20](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/steps/choosing-scheme.md#L11-L20) 的对照表。
2. 假设你手上有一张 **Ampere（算力 8.0）** 的 GPU，查表确定一个推荐方案和一个备选方案。
3. 再假设你有一张 **Hopper（算力 9.0）**，重复一遍。

**需要观察的现象**：你会体会到"硬件决定可选方案集"这件事。

**预期结果**：Ampere → 推荐 W4A16，备选 W8A8-INT8；Hopper → 推荐 W8A8-FP8，备选 W4AFP8。

#### 4.3.5 小练习与答案

**练习 1**：W4A16 中的 W 和 A 分别指什么？它压缩了权重还是激活？

> **答案**：W 指权重（4 位），A 指激活（16 位）。它**只压缩权重**，激活保持 16 位精度，因此也叫"weight-only quantization"。

**练习 2**：NVFP4 至少需要哪种 GPU 架构？

> **答案**：NVIDIA Blackwell（SM100，算力 10.0）。

---

### 4.4 支持的压缩算法

#### 4.4.1 概念说明

如果说"精度方案"决定了**量化到几位、什么格式**，那么"压缩算法"决定了**怎么把权重变成那几位、并尽量保住精度**。同一个方案（如 W4A16）可以用不同算法（GPTQ / AWQ / AutoRound）来实现，精度恢复程度和压缩耗时各不相同。

llm-compressor 把这些算法都封装成统一的概念——**modifier（修改器）**，后续会详细讲。这里只需先建立总览。

#### 4.4.2 核心流程

常见算法的定位可以这样理解：

| 算法 | 类型 | 一句话直觉 |
|------|------|-----------|
| **Simple PTQ / RTN** | 量化 | 最朴素：直接四舍五入到目标精度，速度快、精度一般。 |
| **GPTQ** | 量化 | 用校准数据估计一个 Hessian，按列依次量化并补偿误差，精度好。 |
| **AWQ** | 变换 + 量化 | 先对"重要通道"做缩放重排，再量化，对 W4A16 友好。 |
| **SmoothQuant** | 变换 + 量化 | 把激活里的离群值"平滑"到权重上，让 W8A8 更友好。 |
| **AutoRound** | 量化 | 用少量步数的轻量优化搜索更好的量化权重，支持多种低位方案。 |
| **Rotation-based（SpinQuant / QuIP）** | 变换 + 量化 | 用旋转矩阵预处理权重/激活，降低量化难度。 |
| **REAP** | 剪枝 | 针对 MoE，按"显著性"剪掉不太重要的专家，减少显存。 |

其中 AWQ、SmoothQuant、Rotation-based 属于**变换（transform）类**：它们在真正的量化之前先对权重做一次等价变换，让后续量化更友好。这种"先变换再量化"的思路会在后面的算法单元详细拆解。

#### 4.4.3 源码精读

README 的"Supported Algorithms"列出了项目当前支持的算法：

> [README.md:83-90](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/README.md#L83-L90) —— 列出 Simple PTQ、GPTQ、AWQ、SmoothQuant、AutoRound、Rotation-based（SpinQuant/QuIP）、REAP 专家剪枝。

关于 FP4 方案可以用哪些算法：

> [docs/steps/choosing-scheme.md:59-65](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/steps/choosing-scheme.md#L59-L65) —— 说明 FP4（NVFP4/MXFP4）用 RTN 即可快速得到结果，但用 GPTQ 或 AWQ 有望获得更好的精度恢复。

REAP 是较新的剪枝能力，README 专门介绍了它：

> [README.md:58-61](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/README.md#L58-L61) —— 介绍 REAP 通过校准前向计算出的 saliency 指标，结构性移除 MoE 中相关性低的专家，实现用户指定程度的专家稀疏，并指出其实现可作为其它专家剪枝算法的模板。

#### 4.4.4 代码实践

**实践目标**：把"算法"和"精度方案"两个维度区分清楚。

**操作步骤**：

1. 阅读 [README.md:83-90](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/README.md#L83-L90) 和 [docs/steps/choosing-scheme.md:59-65](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/steps/choosing-scheme.md#L59-L65)。
2. 画一张 2×2 的小表：行是"W4A16"和"W8A8"，列是"RTN"和"GPTQ"，每个格子里写一句"这种组合大概会怎样"（精度/速度的直觉即可）。

**需要观察的现象**：你会意识到**方案和算法是两个正交的选择**——同一种算法可以用于多种方案，同一种方案也可以由多种算法实现。

**预期结果**：例如（W4A16 + RTN）最快但精度一般，（W4A16 + GPTQ）精度更好但更慢，（W8A8 + RTN）很快且对 Hopper 友好等。

#### 4.4.5 小练习与答案

**练习 1**：AWQ 和 GPTQ 都是量化算法，它们的主要区别是什么？

> **答案**：AWQ 是"变换类"——先对重要通道做缩放重排再量化；GPTQ 是"误差补偿类"——用校准数据估计 Hessian，按列量化并补偿已引入的误差。两者都能用于 W4A16 等方案，但思路不同。

**练习 2**：REAP 针对哪类模型？它压缩的是权重位宽还是模型结构？

> **答案**：REAP 针对 MoE（混合专家）模型，它压缩的是**模型结构**（剪掉部分专家），而不是降低权重位宽。

---

### 4.5 三大核心概念：recipe / modifier / oneshot

#### 4.5.1 概念说明

这是本讲最重要的一节，因为**后续所有源码阅读都围绕这三个词展开**。请务必建立如下对应关系：

- **modifier（修改器）**：一个具体压缩动作的封装。例如 `QuantizationModifier` 负责"量化"，`GPTQModifier` 负责"GPTQ 量化"，`REAP` 负责专家剪枝。一个 modifier 通常对应一种算法。
- **recipe（配方）**：一个或多个 modifier 的有序集合，描述"对模型依次做哪些压缩动作"。recipe 可以是单个 modifier，也可以是一个 modifier 列表，还可以写成 YAML 文件。
- **oneshot（一次性压缩）**：llm-compressor 的**主入口函数**。你把模型和 recipe 交给它，它负责跑完整个压缩流程并把结果写回模型。所谓"one shot"是指"一次前向校准就完成压缩"（区别于需要训练的"训练感知量化"）。

打个比方：modifier 是"一道工序"，recipe 是"工艺单"（按顺序列出多道工序），oneshot 是"按下启动按钮的工人"。

#### 4.5.2 核心流程

一次典型的压缩调用流程：

```text
1. 加载模型与 tokenizer
2. 构造 recipe = [若干 modifier，按顺序]
       └─ 例：QuantizationModifier(targets="Linear", scheme="FP8_BLOCK", ignore=[...])
3. 调用 oneshot(model=model, recipe=recipe)
       └─ 内部：解析参数 → 构建 modifier → 跑校准管线 → 修改模型权重
4. model.save_pretrained(SAVE_DIR)
       └─ 以 compressed-tensors 格式落盘
5. (可选) 用 vLLM 加载 SAVE_DIR 直接推理
```

注意 recipe 里 modifier 的**顺序有意义**：像 AWQ、SmoothQuant 这种"变换类"必须放在 `QuantizationModifier` 之前（先变换、再量化）。这种顺序约束在源码层有专门校验，会在后续 Recipe 讲义里展开。

#### 4.5.3 源码精读

先看库入口暴露了哪些 API——`oneshot` 就是从这里被导出的：

> [src/llmcompressor/\_\_init\_\_.py:23-29](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/__init__.py#L23-L29) —— 从 `llmcompressor.entrypoints` 导入并对外暴露 `Oneshot`、`oneshot`、`model_free_ptq`；同时还暴露了 `active_session`、`create_session`、`reset_session`、`callbacks` 等会话相关函数（这些会在第二单元的 Session 讲义中讲解）。

README 的 Quick Tour 是理解三者关系最直观的例子：

> [README.md:177-184](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/README.md#L177-L184) —— 构造一个 `QuantizationModifier(targets="Linear", scheme="FP8_BLOCK", ignore=["lm_head", "re:.*mlp.gate$"])`，然后调用 `oneshot(model=model, recipe=recipe)` 完成量化。这里**单个 modifier 直接作为 recipe 传入**。

> 说明：`targets="Linear"` 表示对所有 `nn.Linear` 层量化；`scheme="FP8_BLOCK"` 表示用 FP8、按 block 分组带缩放；`ignore=[...]` 表示跳过 `lm_head` 和名字匹配 `.*mlp.gate$` 的层（正则前缀 `re:`）。这些字段的精确含义会在第三单元的 QuantizationModifier 讲义里逐个讲解，现在只需知道"modifier 通过这些参数描述压缩动作"即可。

> [README.md:196-199](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/README.md#L196-L199) —— 压缩完成后，`model.save_pretrained(SAVE_DIR)` 即以 `compressed-tensors` 格式保存到磁盘，随后可被 vLLM 直接加载。

> [README.md:214-218](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/README.md#L214-L218) —— 用 vLLM 加载保存后的 checkpoint：`LLM("Qwen/Qwen3-30B-A3B-FP8-BLOCK")`，然后 `model.generate(...)` 即可推理。

#### 4.5.4 代码实践

**实践目标**：亲手运行一次最小压缩，直观看到 recipe / modifier / oneshot 三者的协作。

**操作步骤**：

1. 确认已安装：`pip install llmcompressor`（详见 [README.md:99-103](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/README.md#L99-L103)）。
2. 准备一个**极小的本地或 HF 小模型**（例如几十层、几百万参数的 tiny 模型，避免显存压力）。这是**示例代码**，模型名请按你本地实际情况替换：

   ```python
   from transformers import AutoModelForCausalLM, AutoTokenizer
   from llmcompressor import oneshot
   from llmcompressor.modifiers.quantization import QuantizationModifier

   MODEL_ID = "<你本地的一个极小模型 id>"  # 请替换

   model = AutoModelForCausalLM.from_pretrained(MODEL_ID)
   tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)

   # 一个 modifier 即可作为 recipe
   recipe = QuantizationModifier(targets="Linear", scheme="FP8")

   oneshot(model=model, recipe=recipe)

   SAVE_DIR = "tiny-fp8"
   model.save_pretrained(SAVE_DIR)
   tokenizer.save_pretrained(SAVE_DIR)
   ```

3. 观察 `oneshot(...)` 执行时控制台打印的日志，留意是否出现 calibration / pipeline / modifier 相关字样。

**需要观察的现象**：`oneshot` 跑完后，`SAVE_DIR` 下会生成 `config.json` 和 `*.safetensors`；打开 `config.json`，应能看到一个 `quantization_config` 字段。

**预期结果**：`config.json` 的 `quantization_config` 里记录了量化方案与目标层，证明模型已被压缩并按 `compressed-tensors` 格式保存。

> ⚠️ 如果没有合适的小模型或 GPU，**待本地验证**：你也可以只阅读 [README.md:160-200](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/README.md#L160-L200) 的 Quick Tour 源码，逐行注释每一步，作为"源码阅读型实践"。

#### 4.5.5 小练习与答案

**练习 1**：recipe 和 modifier 是什么关系？

> **答案**：modifier 是单个压缩动作（如量化、剪枝）的封装；recipe 是若干 modifier 的有序集合，描述一次完整的压缩流程。单个 modifier 也可以直接当作 recipe 传入 `oneshot`。

**练习 2**：为什么 `oneshot` 叫"one shot"？

> **答案**：因为它只需一次（少量样本的）前向校准就能完成压缩，不需要像训练感知量化那样做完整的多轮训练，因此叫"一次性（one-shot）"压缩。

**练习 3**：在 Quick Tour 的例子中，`ignore=["lm_head", "re:.*mlp.gate$"]` 起什么作用？

> **答案**：它告诉 `QuantizationModifier` 跳过 `lm_head` 层，以及名字匹配正则 `.*mlp.gate$` 的层（MoE 里的路由 gate），不对它们量化——这些层对精度敏感，通常保持原精度。

---

## 5. 综合实践

把本讲知识串起来，完成下面这个**数据流梳理**任务（这是本讲的主实践任务）：

1. **阅读** [README.md](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/README.md) 的 Quick Tour 部分和 [docs/steps/why-llmcompressor.md](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/steps/why-llmcompressor.md)。
2. **画一张数据流图**，要求至少包含以下节点和它们的方向关系：
   - 输入：HuggingFace 模型、recipe（含 modifier）、可选校准数据。
   - 处理：`oneshot(...)` 内部做了什么（用一句话概括，例如"按 recipe 依次执行 modifier 完成校准与压缩"）。
   - 输出：`compressed-tensors` 格式的 checkpoint。
   - 下游：vLLM 加载并推理。
3. **在你的图上标注**：
   - 哪一步用到"校准数据"，哪些方案可以不要校准数据？
   - 哪一步决定"量化到几位"（提示：scheme），哪一步决定"怎么量化"（提示：算法/modifier）？
4. **列出一种你最感兴趣的精度方案**，写出它的名字、适用 GPU 架构、是压权重还是权重+激活，以及你打算搭配哪种算法。

**预期产物**：一张数据流图 + 一段对所选方案的说明。这是你后续深入源码时的"导航图"，请保存好。

> ⚠️ 如果你无法确定某个细节（例如某方案是否需要校准数据），请查阅 [docs/steps/choosing-scheme.md](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/steps/choosing-scheme.md) 后再下结论，不要凭猜测。

## 6. 本讲小结

- **llm-compressor 是面向 vLLM 部署的大模型压缩库**：输入 HuggingFace 模型 + recipe，输出 `compressed-tensors` 格式的可部署 checkpoint；它本身不做推理。
- **量化的数学本质**是用 scale 和 zero-point 把高精度值映射到低位，误差主要来自 `round`；合理选 scale 是各算法的核心。
- **精度方案（scheme）** 决定"量化到几位、什么格式"，如 W8A8 / FP8 / NVFP4 / W4A16；**方案选择由 GPU 架构决定**。
- **压缩算法** 决定"怎么量化/压缩"，如 Simple PTQ / GPTQ / AWQ / SmoothQuant / AutoRound / Rotation-based / REAP；其中 AWQ、SmoothQuant 等是"先变换再量化"。
- **三大核心概念**：modifier = 单个压缩动作；recipe = modifier 的有序集合；oneshot = 主入口函数，一次校准完成压缩。
- **产物通过 `compressed-tensors` 格式与 vLLM 衔接**，稀疏压缩（2:4）已不再支持。

## 7. 下一步学习建议

本讲建立了全局地图，接下来建议按以下顺序深入：

1. **先把环境跑通**：进入下一讲《安装与第一次量化实践》，亲手完成一次 FP8 量化并保存。
2. **再建立目录感**：阅读《目录结构与库入口》，了解 `src/llmcompressor` 下各子包的职责，方便后续定位源码。
3. **进入引擎层**：第二单元会从 `oneshot` 入口的"三阶段生命周期"切入，逐步讲解 Session / Lifecycle / Modifier 基类 / ModifierFactory / Recipe / 参数系统——这些是理解任何压缩流程的骨架。
4. **建议同步阅读**官方分步教程 `docs/steps/` 下的其余文件（`choosing-model.md`、`choosing-algo.md`、`choosing-dataset.md`、`compress.md`、`deploy.md`），与本手册互为补充。

下一篇：**《u1-l2 安装与第一次量化实践》**。
