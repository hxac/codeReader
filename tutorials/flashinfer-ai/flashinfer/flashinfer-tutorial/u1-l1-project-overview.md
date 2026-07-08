# FlashInfer 项目总览与定位

## 1. 本讲目标

本讲是整个 FlashInfer 学习手册的第一篇，面向从未接触过本项目的读者。读完本讲，你应当能够：

- 用一句话说清楚 FlashInfer **是什么**、**为谁服务**、**解决什么问题**。
- 说清 FlashInfer 的**三大核心能力**（注意力 / GEMM / MoE）以及它**多后端**（FlashAttention-2/3、cuDNN、CUTLASS、TensorRT-LLM）的设计特点。
- 读懂仓库自带的 **GPU 支持矩阵**，并能对照自己机器的 compute capability，判断哪些特性可用。
- 理解 FlashInfer 与「直接用 PyTorch 自带 attention」相比的价值所在。

本讲**不要求**你会写 CUDA，也不要求你理解任何具体算子的实现细节。我们只读三个项目级文件：`README.md`、`CLAUDE.md`、`pyproject.toml`，目的是先把「地图」建立起来。后续每一讲都会在这张地图上定位更细的模块。

## 2. 前置知识

在开始之前，请确认你大致了解下面几个概念。如果某个概念不熟，本节会用最朴素的语言补上。

### 2.1 LLM 推理与「服务（serving）」

大语言模型（LLM）训练好之后，把用户输入变成输出的过程叫**推理（inference）**。而把推理能力以 API 形式长期、高并发地提供给很多用户，就叫**推理服务（serving）**。FlashInfer 的定位副标题就是 "Kernel Library for LLM Serving"——它专门为「服务场景」优化 GPU 算子，而不是为训练场景。

服务场景有两个训练场景少见的痛点：

- **动态批处理（continuous batching / in-flight batching）**：不同用户的请求长度不同、到达时间不同，会被实时拼到同一个 batch 里。这要求算子能处理「变长、不规则」的输入。
- **KV-Cache 内存管理**：模型每生成一个 token，就要把它的 Key/Value 存起来给后续 token 复用。当 batch 里几十个请求各自的上下文长度参差不齐时，KV-Cache 的显存管理会变成性能瓶颈。

FlashInfer 的很多设计（Paged KV-Cache、ragged 张量、变长索引）都是为这两个痛点服务的。

### 2.2 GPU 与 compute capability

NVIDIA 每一代 GPU 架构都有一个**计算能力（compute capability，简称 CC）**版本号，例如 `8.0`、`9.0`、`10.0`。它的格式是「主版本.次版本」：

- **主版本**对应架构代际（8 = Ampere，9 = Hopper，10/11/12 = Blackwell 系列）。
- **次版本**区分同一代里的不同型号或特性集。

在 PyTorch 里可以这样查到本机 GPU 的 CC：

```python
import torch
print(torch.cuda.get_device_capability())  # 例如 (9, 0) 表示 SM 9.0
```

很多算子只在特定 CC 上可用（例如 Hopper 独占的 FlashAttention-3 需要 SM 9.0）。本讲末尾的实践就要用到这个查询。

### 2.3 「注意力（attention）」是什么

如果你只记一句话：注意力机制计算的是「当前 token 的 Query」与「历史所有 token 的 Key/Value」之间的加权求和。缩放点积注意力（Scaled Dot-Product Attention, SDPA）的公式是：

\[
\mathrm{Attention}(Q, K, V) = \mathrm{softmax}\!\left(\frac{Q K^{\mathsf{T}}}{\sqrt{d_k}}\right) V
\]

其中 \(d_k\) 是每个头的维度，分母上的 \(\sqrt{d_k}\) 是为了控制点积结果的数值范围。FlashInfer 的核心就是把这个公式（及其变体：decode、prefill、MLA、稀疏等）做成**高性能、可定制**的 GPU kernel。你暂时不需要记住公式，只要知道「注意力是 LLM 推理里最吃算力/带宽的算子之一」即可。

### 2.4 什么是「kernel」

在 GPU 编程语境里，**kernel** 指的是在 GPU 上并行执行的一段函数。我们把 Python 层调用最终翻译成「启动一个 GPU kernel」。所以「GPU kernel 库」可以粗略理解为「一堆在 GPU 上跑得飞快、被 Python 调用的底层函数集合」。

## 3. 本讲源码地图

本讲只看三个项目级文件，它们是理解整个仓库的「门面」：

| 文件 | 作用 | 本讲怎么用 |
|------|------|-----------|
| `README.md` | 面向用户的项目说明书：定位、特性清单、GPU 支持表、安装与基本用法 | 主要信息来源，几乎所有结论都来自这里 |
| `CLAUDE.md` | 面向开发者的工程向导：目录结构、JIT 架构、构建/调试命令 | 用来补充「为什么这么设计」的工程视角 |
| `pyproject.toml` | Python 包定义：包名、依赖、入口脚本、数据文件打包规则 | 用来确认包的真实身份与第三方依赖 |

> 提示：这三个文件都属于「项目级元数据」，不含具体算子实现。看懂它们之后，你就拥有了一张「按图索骥」的地图，后续每一讲都会深入到 `include/`、`csrc/`、`flashinfer/` 等具体代码目录。

## 4. 核心概念与源码讲解

本讲围绕三个最小模块展开：**项目说明**、**核心特性**、**GPU 支持矩阵**。

### 4.1 项目说明：FlashInfer 是什么

#### 4.1.1 概念说明

FlashInfer 是一个为 **LLM 推理服务**设计的 **GPU kernel 库与 kernel 生成器**。这句话有三个关键词：

1. **推理（而非训练）**：它优化的是「生成阶段」的算子，关心低延迟、动态批处理、KV-Cache 复用。
2. **kernel 库**：它直接提供可被 Python 调用的高性能 GPU 函数。
3. **kernel 生成器**：它不只是预编译好一堆 kernel，还能在**使用时**根据参数（数据类型、头维度等）现场生成并编译专用 kernel——这就是 FlashInfer 最有特色的 **JIT（Just-In-Time，即时编译）** 机制。

#### 4.1.2 核心流程

从「用户视角」看，使用 FlashInfer 的一次典型流程是：

1. 用 `pip install flashinfer-python` 安装（核心包，kernel 在首次使用时编译/下载）。
2. 在 Python 里 `import flashinfer`，调用某个 API（例如 `flashinfer.single_decode_with_kv_cache`）。
3. **首次调用**触发 JIT：根据参数生成专用 CUDA 代码 → 用 ninja 编译成 `.so` → 缓存到磁盘 → 加载执行。
4. **后续调用**直接复用缓存的 `.so`，几乎没有编译开销。

这个「首次编译、之后复用」的模式，让 FlashInfer 既能针对每一种参数组合做极致特化，又不需要用户手动管理编译——这是它相对传统预编译库的一大优势。我们在第 2 单元会专门拆解 JIT，这里先有个直觉即可。

#### 4.1.3 源码精读

README 开篇的一句话定义把 FlashInfer 的定位、能力和后端一次性讲清楚了：

[README.md:L18-L18](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/README.md#L18) —— FlashInfer 的官方一句话定义：**为推理提供 SOTA 性能的库与 kernel 生成器，提供 attention/GEMM/MoE 的统一 API，并支持 FlashAttention-2/3、cuDNN、CUTLASS、TensorRT-LLM 多种后端**。

`pyproject.toml` 则从「包」的角度确认了身份与定位：

[pyproject.toml:L15-L18](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/pyproject.toml#L15-L18) —— 包名是 `flashinfer-python`，描述明确写着 "Kernel Library for LLM Serving"，要求 Python ≥ 3.10。

FlashInfer 最具特色、也是贯穿后续所有讲义的工程决策，是「默认 JIT 编译」。CLAUDE.md 的 Project Overview 一节对此有一句精炼的说明：

[CLAUDE.md:L5-L7](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/CLAUDE.md#L5-L7) —— 说明 FlashInfer 默认使用 JIT 编译，好处是**修改 kernel 源码后无需重装包即可生效**，这对开发极其友好。

此外，`pyproject.toml` 还透露了两个有用事实：包把若干第三方库（CUTLASS、spdlog、cccl）作为数据目录打进包里，这是 JIT 编译时需要的「就地」依赖：

[pyproject.toml:L64-L68](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/pyproject.toml#L64-L68) —— `flashinfer.data` 指向仓库根目录，并把 `3rdparty/cutlass`、`3rdparty/spdlog`、`3rdparty/cccl` 打包进去，供 JIT 时引用。

#### 4.1.4 代码实践

**实践目标**：从两个不同来源（README 与 pyproject.toml）确认 FlashInfer 的定位，体会「用户向文档」与「包元数据」的相互印证。

**操作步骤**：

1. 打开仓库根目录的 `README.md`，找到第 18 行的一句话定义，记下它提到的「三大能力」和「四种后端」。
2. 打开 `pyproject.toml`，找到第 15–18 行的 `[project]` 段，记下包名和 `description`。
3. 比对两处对「定位」的描述是否一致。

**需要观察的现象**：README 偏「能力与后端」，pyproject.toml 偏「身份与依赖」，但二者都把 FlashInfer 定位为「LLM 推理服务用的 kernel 库」。

**预期结果**：你会得到一份「FlashInfer 一句话定位卡」，例如：*FlashInfer 是一个为 LLM 推理服务设计、默认 JIT 编译、支持 attention/GEMM/MoE 三大能力与多后端的 GPU kernel 库。*

**待本地验证**：以上为阅读型任务，无运行结果。

#### 4.1.5 小练习与答案

**练习 1**：FlashInfer 优化的是「训练」还是「推理」阶段的算子？为什么这很重要？

> **参考答案**：是**推理（inference）**阶段。推理服务要面对动态批处理、变长请求、KV-Cache 复用等训练场景少见的痛点，所以 FlashInfer 的很多设计（Paged KV-Cache、变长索引、低延迟）都是为服务场景量身定制的。

**练习 2**：FlashInfer「默认 JIT 编译」给开发者带来的直接好处是什么？

> **参考答案**：修改 kernel 源码（如 `include/` 下的 `.cuh`）后，**无需重新安装包**，下次调用会自动检测变化并重新编译。这让开发循环非常短——「改一行代码 → 直接跑测试」即可。

---

### 4.2 核心特性：三大能力与多后端

#### 4.2.1 概念说明

FlashInfer 的能力可以归为三大类，外加一组「其他算子」和「通信算子」：

- **Attention（注意力）**：decode / prefill / append 全阶段，Paged 与 Ragged KV-Cache，MLA（DeepSeek 风格）、Cascade（共享前缀）、稀疏注意力、POD（prefill+decode 融合）。
- **GEMM（矩阵乘）与线性运算**：BF16、FP8（per-tensor / groupwise）、FP4（NVFP4 / MXFP4，面向 Blackwell）、Grouped GEMM（服务 LoRA 与多专家路由）。
- **MoE（混合专家）**：融合 MoE kernel、多种路由方法（DeepSeek-V3 / Llama-4 / 标准 top-k）、量化 MoE（FP8/FP4）。

更重要的是，FlashInfer 对同一个问题往往提供**多个后端实现**，并能根据硬件和数据类型**自动选择最优后端**。这四种后端是：FlashAttention-2/3、cuDNN、CUTLASS、TensorRT-LLM。这就是 README 反复强调的「Multiple Backends」特性。

#### 4.2.2 核心流程

「多后端」的工作方式可以用下面的伪流程表示：

```text
用户调用某个 API（如 attention plan/run）
        │
        ▼
后端选择器：根据 (GPU 架构, 数据类型, 头维度, 工作负载) 综合判断
        │
        ├── SM 9.0 (Hopper) + fp16/bf16  →  FlashAttention-3 后端
        ├── 通用支持                       →  FlashAttention-2 后端
        ├── cuDNN 支持的形状/dtype         →  cuDNN 后端
        └── 特定量化/结构（FP4/MoE）        →  CUTLASS / TensorRT-LLM 后端
        │
        ▼
对应的 JIT 生成 + 编译 + 执行
```

初学者只需要记住一个直觉：**「选最合适的实现」这件事，FlashInfer 替你做了**。具体的选择逻辑（`determine_attention_backend`、`@backend_requirement` 装饰器）我们留到后续 attention 与 GEMM 单元细讲。

#### 4.2.3 源码精读

README 的 "Why FlashInfer?" 一节，用五条 bullet 浓缩了项目的卖点：

[README.md:L20-L26](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/README.md#L20-L26) —— 五大卖点：SOTA 性能、多后端自动选择、SM75 起的架构支持、FP8/FP4 低精度、CUDA Graph / torch.compile 兼容（面向低延迟服务）。

随后 README 把核心能力分成几大块列出。注意力部分（也是 FlashInfer 最核心、最复杂的子系统）：

[README.md:L30-L36](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/README.md#L30-L36) —— Attention 能力清单：Paged/Ragged KV-Cache、decode/prefill/append 三阶段、MLA、Cascade、稀疏、POD。

GEMM 与线性运算部分：

[README.md:L38-L43](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/README.md#L38-L43) —— GEMM 能力清单：BF16（SM10.0+）、FP8（per-tensor/groupwise）、FP4（NVFP4/MXFP4，Blackwell）、Grouped GEMM（LoRA/多专家）。

MoE 部分：

[README.md:L44-L48](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/README.md#L44-L48) —— MoE 能力清单：融合 MoE kernel、多种路由（DeepSeek-V3/Llama-4/top-k）、量化 MoE（FP8/FP4）。

> 备注：BF16 GEMM 在 README 中标注为 "for SM10.0+ GPUs"，而 FP8 GEMM 之类则覆盖更广的架构。这说明**不同能力在不同架构上的可用范围不同**——这正是下一节「GPU 支持矩阵」要解决的前提问题。

#### 4.2.4 代码实践

**实践目标**：把 README 的能力清单整理成一张「能力 × 后端」速查表，建立对项目范围的总体印象。

**操作步骤**：

1. 在 `README.md` 第 18 行找齐「四种后端」的名称，作为表格列。
2. 在第 30–48 行找齐「三大能力」，作为表格行。
3. 结合你目前的了解，标注每种能力最可能用到的后端（不确定就写「待确认」，后续单元会落实）。

**预期结果**（示意，部分「待确认」留给后续讲义）：

| 能力 | 可能后端 |
|------|---------|
| Attention（decode/prefill/MLA/...） | FlashAttention-2/3、cuDNN |
| GEMM（BF16/FP8/FP4） | cuBLAS、CUTLASS、cuDNN、TensorRT-LLM |
| MoE（融合/量化） | CUTLASS、TensorRT-LLM |

**待本地验证**：这是阅读整理型任务，具体后端选择细节会在后续 attention/GEMM/MoE 单元中结合源码落实。

#### 4.2.5 小练习与答案

**练习 1**：FlashInfer 的「多后端」是指什么？为什么 LLM 推理服务特别需要它？

> **参考答案**：指对同一类算子（如 attention）提供多种实现（FlashAttention-2/3、cuDNN、CUTLASS、TensorRT-LLM），并自动根据硬件和数据类型选最优者。LLM 推理服务的硬件（从 T4 到 B200）和负载（prefill/decode/混合）差异巨大，没有单一后端在所有情况下都最优，因此「按需选后端」能持续拿到 SOTA 性能。

**练习 2**：举出一个「某个能力只在部分 GPU 上可用」的例子（来自本节源码）。

> **参考答案**：README 第 39 行明确写道 BF16 GEMM 是 "for SM10.0+ GPUs"，即主要面向 Blackwell 及更新架构；而更早的架构上 BF16 GEMM 的可用性更受限。这说明「能力 × 架构」并非全覆盖。

---

### 4.3 GPU 支持矩阵

#### 4.3.1 概念说明

FlashInfer 跨越多代 NVIDIA 架构：从最老的 Turing（SM 7.5，如 T4）一直到最新的 Blackwell（SM 12.x，如 RTX 50 系列）。理解支持矩阵有两层意义：

1. **确认能否用**：先确认你的 GPU CC 在支持范围内，否则安装/运行会报错。
2. **确认能用哪些特性**：即使 CC 在范围内，**也并非所有功能都支持**——例如 FlashAttention-3 需要 Hopper（SM 9.0），FP4 GEMM 主要面向 Blackwell。

#### 4.3.2 核心流程

判断「我的机器能用什么」的标准三步：

```text
1. 查本机 compute capability（major.minor）
        │
        ▼
2. 对照支持矩阵，确认是否在范围内
        │
        ▼
3. 结合「能力 × 架构」约束，判断具体特性是否可用
   （例如 FA3 需要 SM9.0、BF16 GEMM 主要 SM10.0+、FP4 GEMM 面向 Blackwell）
```

compute capability 的版本号遵循「主版本 = 架构代际」的约定，可以用一个简单关系表达架构与 CC 的映射：

\[
\text{架构代际} = \lfloor \text{CC} \rfloor
\quad(\text{例如 CC } 9.0 \Rightarrow \text{Hopper},\ \text{CC } 10.0 \Rightarrow \text{Blackwell})
\]

#### 4.3.3 源码精读

README 的 "GPU Support" 一节给出了一张官方支持表：

[README.md:L63-L73](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/README.md#L63-L73) —— GPU 支持矩阵：Turing(7.5)、Ampere(8.0/8.6)、Ada(8.9)、Hopper(9.0)、Blackwell(10.0/10.3/11.0/12.0/12.1)，并附带了代表性 GPU 型号。

表后紧跟一句关键提醒——**支持矩阵只保证「架构可用」，不保证「每个特性都可用」**：

[README.md:L75-L75](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/README.md#L75) —— "Not all features are supported across all compute capabilities."（并非所有特性都在所有 CC 上可用）。

从开发者视角，CLAUDE.md 用一行精确列出了具体支持的 SM 列表（与 README 表格互补，更细分了次版本）：

[CLAUDE.md:L599-L599](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/CLAUDE.md#L599-L599) —— FlashInfer 支持的 SM 列表：SM75、SM80、SM86、SM89、SM90、SM100、SM103、SM110、SM120、SM121。

> 注意两处表述的差别：README 用「架构名 + 主次版本」给用户看，CLAUDE.md 用「SMxx」给开发者看，二者都指向同一组 GPU。把这两份信息放在一起，你既知道「我的 H100 属于哪个档」，也知道「在 JIT 编译配置里它对应哪个 SM 编号」。

#### 4.3.4 代码实践

**实践目标**：在本机查出 GPU 的 compute capability，对照支持矩阵判断「我属于哪一档、哪些高级特性大概率可用」。

**操作步骤**：

1. 确认有 CUDA 可用的 PyTorch，运行：

   ```python
   import torch
   print(torch.cuda.is_available())                 # 应为 True
   print(torch.cuda.get_device_name(0))              # GPU 名称
   print(torch.cuda.get_device_capability(0))        # 例如 (9, 0)
   ```

2. 把得到的 `(major, minor)`（如 `(9, 0)` → SM 9.0）对照 README 第 63–73 行的表格，确定架构名（如 Hopper）和代表性型号。
3. 再结合本讲 4.2 节里「能力 × 架构」的约束，列出你这台机器上**大概率可用**与**大概率不可用**的高级特性各一两个（例如 FA3 是否可用、FP4 GEMM 是否面向你这代卡）。

**需要观察的现象**：`(major, minor)` 元组与 README 表格里的 `SM x.x` 一一对应；不同机器会得到不同结果。

**预期结果**（举例）：若你得到 `(9, 0)`，则判定为 Hopper，处于支持范围内，FlashAttention-3（需 SM9.0）大概率可用；而 BF16 GEMM（README 标注 SM10.0+）和 FP4 GEMM（Blackwell）大概率不可用或受限。

**待本地验证**：不同机器结果不同，请以你本机实际输出为准。命令本身是标准 PyTorch API，可放心运行。

#### 4.3.5 小练习与答案

**练习 1**：如果你的 GPU 是 A100（SM 8.0），它属于哪一代架构？FlashAttention-3 在它上面可用吗？

> **参考答案**：A100 是 Ampere 架构（SM 8.0）。FlashAttention-3 需要 Hopper（SM 9.0），所以在 A100 上**不可用**，FlashInfer 会退而选择 FlashAttention-2 或其他支持的 backend。

**练习 2**：README 的支持矩阵里，Blackwell 这一架构出现了哪几个 CC？为什么「同属 Blackwell」却有好几个不同编号？

> **参考答案**：出现 SM 10.0、10.3、11.0、12.0、12.1。同属 Blackwell 但定位不同（数据中心卡 B200/B300、嵌入式 Jetson Thor、消费级 RTX 50/DGX Spark），它们在特性集、显存层次上存在差异，因此用不同的次版本号区分。

**练习 3**：把 `torch.cuda.get_device_capability()` 返回的 `(8, 9)` 翻译成「SM 编号」和「架构名」。

> **参考答案**：`(8, 9)` → SM 8.9 → Ada Lovelace 架构（代表性 GPU：L4、L40、RTX 40 系列）。

## 5. 综合实践

把本讲的三块知识串成一个完整的小任务，作为「项目总览」的收尾。

**任务背景**：假设你刚入职一家做 LLM 推理服务的公司，技术负责人让你「先评估一下 FlashInfer 是否适合我们的硬件」。请完成下面四步，并把结论写成一段话。

**步骤**：

1. **定位复述**：用你自己的话写一句 FlashInfer 是什么（参考 4.1 节，结合 README 第 18 行与 pyproject.toml 第 15–18 行）。
2. **能力盘点**：列出 FlashInfer 的三大核心能力，并各举一个 README 中提到的具体子特性（参考 4.2 节，第 30–48 行）。
3. **硬件自检**：运行 `torch.cuda.get_device_capability()`，对照 README 第 63–73 行的支持矩阵，写明你的 GPU 属于哪一档。
4. **价值论述**：写一段话（100–200 字），论述「相比直接用 PyTorch 自带的 scaled dot-product attention，引入 FlashInfer 能带来什么价值」。请至少涉及以下几点：多后端自动选择、Paged/Ragged KV-Cache 对动态批处理的意义、低精度（FP8/FP4）与低延迟（CUDA Graph 兼容）。

**验收标准**：

- 定位复述准确（提到「推理服务 / kernel 库 / JIT」中的至少两点）。
- 三大能力各举出一个真实子特性（不得编造）。
- 硬件自检给出真实的 `(major, minor)` 与对应架构名。
- 价值论述覆盖多后端、KV-Cache 管理、低精度/低延迟中的至少两点。

**待本地验证**：第 3 步的输出取决于你的机器，请如实记录；其余为阅读与写作任务。

> 这段「价值论述」就是你本讲的最终交付物。它回答了最根本的问题：**为什么不用 PyTorch 自带的 attention，而要用 FlashInfer？** 当你能流畅地讲清楚这一点，本讲的目标就达成了。

## 6. 本讲小结

- FlashInfer 是面向 **LLM 推理服务**的 **GPU kernel 库与 kernel 生成器**，最鲜明的工程特征是**默认 JIT 编译**。
- 它的三大核心能力是 **Attention / GEMM / MoE**，外加采样、归一化、RoPE、激活、通信等「其他算子」。
- 它对同一类算子提供 **多后端**（FlashAttention-2/3、cuDNN、CUTLASS、TensorRT-LLM），并能**自动选择最优后端**。
- 支持的 GPU 跨度从 **Turing(SM7.5) 到 Blackwell(SM12.x)**，但**并非每个特性都在每代卡上可用**（如 FA3 需 SM9.0、BF16/FP4 GEMM 主要面向 Blackwell）。
- 可以用 `torch.cuda.get_device_capability()` 查本机 CC，再对照 README 支持矩阵判断可用特性。
- 相比直接用 PyTorch attention，FlashInfer 的价值在于：**专为服务场景优化（动态批处理 + KV-Cache 管理）、多后端自动选优、低精度与 CUDA Graph/torch.compile 兼容**。

## 7. 下一步学习建议

本讲只读了项目级元数据，还没有进入任何代码目录。建议按下面的顺序继续：

1. **先跑通一个 kernel**：阅读 `README.md` 的 "Basic Usage"（第 122–135 行）与 "Installation"（第 87–113 行），在本机安装并用 `flashinfer.single_decode_with_kv_cache` 跑出第一个结果。对应本手册的 **u1-l2（安装与首次运行）** 与 **u1-l5（第一个注意力算子实践）**。
2. **建立目录地图**：读 `CLAUDE.md` 的 "Directory Structure" 一节，弄清 `include/`、`csrc/`、`flashinfer/`、`tests/` 四大顶层目录的职责，以及「框架无关 kernel 与 TVM-FFI 绑定分离」的关键规则。对应 **u1-l3（仓库目录结构与代码分层）**。
3. **理解 JIT**：在动手改任何 kernel 之前，务必先理解第 2 单元「JIT 编译系统」——它是理解后续所有算子如何「从 Python 调用到 GPU 执行」的钥匙。
4. **进阶方向**：对算子实现感兴趣，可从 Attention 单元（第 3 单元）入手；对工程化感兴趣，可直接跳到第 9–10 单元（扩展 FlashInfer / 工程化与运维）。

> 一句话定位后续路线：**先把环境跑起来（u1-l2）→ 看懂目录分层（u1-l3）→ 吃透 JIT（第 2 单元）→ 再按兴趣深入某个算子家族。**
