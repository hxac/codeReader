# 讲义标题：项目总览与定位

> 本讲是 DeepGEMM 学习手册的第一篇。读完它，你会知道 DeepGEMM 是什么、为什么这样设计、它能在哪些硬件上跑哪些算子，以及它的版本是如何一步步演进到今天的。本讲不要求任何 GPU 编程基础，所有概念都从零讲起。

---

## 1. 本讲目标

学完本讲后，你应当能够：

1. 用一句话说清 DeepGEMM 的定位（一个面向 SM90/SM100 的统一高性能张量核 kernel 库）。
2. 列举它支持的核心算子类型（FP8/FP4/BF16 GEMM、分组 GEMM、Mega MoE、MQA 评分、HyperConnection 等）。
3. 说清它的目标硬件：NVIDIA SM90（Hopper，如 H800）与 SM100（Blackwell）。
4. 理解它的核心设计哲学：轻量、运行时 JIT 编译、借鉴 CUTLASS/CuTe 的概念但不重度依赖它们的模板。
5. 建立对 News 时间线与版本演进（Mega MoE、FP4 Indexer、PDL 等）的整体印象。

---

## 2. 前置知识

本讲面向零基础读者，但下面几个通用概念会帮你更好地理解：

- **GEMM（矩阵乘）**：深度学习里最常见的计算。DeepGEMM 采用约定 \( D = C + A \cdot B \)，其中 A 的形状是 \([M, K]\)、B 是 \([K, N]\)，结果 D 是 \([M, N]\)，C 是与 D 同形的偏置。
- **张量核（Tensor Core）**：NVIDIA GPU 里专门做矩阵乘加的硬件单元，吞吐远高于普通 CUDA Core。DeepGEMM 的几乎所有性能都来自它。
- **低精度浮点**：FP8（8 位浮点，如 E4M3）、FP4（4 位）、BF16。精度越低、位宽越小，同样带宽下能算得越多，是现代大模型推理/训练加速的关键。
- **JIT（Just-In-Time，即时编译）**：在「程序运行时」而不是「安装时」把代码编译成机器码。DeepGEMM 用它来按需生成 GPU kernel。
- **SM**：Streaming Multiprocessor，GPU 的计算基本块；一块 GPU 由很多个 SM 组成。SM90/SM100 指的是不同代 GPU 架构的「计算能力版本号」。

不需要现在完全掌握这些，后续讲义会逐个深入。这里只要建立一个直觉：DeepGEMM 是一套「把矩阵乘和相关算子在最新 GPU 张量核上跑到极致」的库。

---

## 3. 本讲源码地图

本讲主要阅读项目说明文档，并配合 Python 包入口做交叉验证。涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目的「门面」，集中说明了定位、设计理念、依赖、News 时间线与全部接口。本讲的主要依据。 |
| `deep_gemm/__init__.py` | Python 包入口。把底层 C++ 扩展 `_C` 里导出的函数重新暴露成 `deep_gemm.*`，用于交叉验证「到底支持哪些算子」。 |

> 说明：本讲是总览，因此以 README 为主、`__init__.py` 为辅。后续每一讲都会深入到 `csrc/`（宿主 C++）和 `deep_gemm/include/`（设备侧 CUDA 头文件）里的具体实现。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **项目定位与特性** —— DeepGEMM 是什么、为什么这样设计。
2. **硬件与算子矩阵** —— 跑在什么硬件上、支持哪些算子。
3. **版本演进时间线** —— 它是怎么一步步发展到今天的。

---

### 4.1 项目定位与特性

#### 4.1.1 概念说明

DeepGEMM 的官方定义只有一句话，但它信息量很大：

> DeepGEMM is a **unified, high-performance tensor core kernel library** that brings together the key computation primitives of modern large language models — GEMMs (FP8, FP4, BF16), fused MoE with overlapped communication (Mega MoE), MQA scoring for the lightning indexer, HyperConnection (HC), and more — into a **single, cohesive CUDA codebase**.

拆开来看，这句话告诉了我们三件事：

1. **「统一（unified）」**：它把很多本来散落在各处的算子（普通 GEMM、MoE、注意力评分等）整合进同一份 CUDA 代码库，共用同一套 JIT 基础设施和编程风格。
2. **「面向大模型的关键计算原语」**：它的算子都是为现代 LLM（大语言模型）量身打造的，比如 MoE（混合专家）、MQA（多头/多查询注意力）评分、HyperConnection 等。
3. **「高性能张量核 kernel 库」**：它的目标是在张量核上把性能榨干，README 直接声称性能「匹敌甚至超过专家级调优的库」。

更关键的是它的**设计哲学**。README 里明确写了三条原则：

- **轻量**：只维护数量有限的几个核心 kernel 函数，不搞庞杂的模板体系。
- **运行时 JIT 编译**：所有 kernel 都在运行时按需编译，**安装时不需要任何 CUDA 编译**。
- **借鉴但不重度依赖 CUTLASS/CuTe**：吸收了 CUTLASS 和 CuTe 的概念，但刻意避免重度依赖它们的模板或代数抽象，使代码「干净、适合学习 GPU kernel 优化技术」。

> 术语解释：
> - **CUTLASS**：NVIDIA 官方的高性能矩阵运算 C++ 模板库，功能极全但模板极其复杂。
> - **CuTe**：CUTLASS 里的布局/张量代数子库。
> - DeepGEMM 「借鉴概念但不依赖模板」的意思是：它学到了 CUTLASS 里分块（tiling）、流水线（pipeline）等思想，但用自己的、更直白的 CUDA C++ 写法实现，避免读者被层层模板绕晕。

#### 4.1.2 核心流程

虽然本讲还不会深入实现，但你需要先建立一个「一次调用如何变成 GPU 计算」的整体直觉。DeepGEMM 的运行流程大致如下：

```text
用户 Python 代码                宿主侧（CPU / C++）                设备侧（GPU / CUDA）
─────────────────              ───────────────────               ─────────────────────
deep_gemm.fp8_gemm_nt(...)  →  校验形状/类型/布局
                              → 根据架构(SM90/SM100)派发
                              → JIT：按形状生成 .cu 源码
                              → 查缓存；未命中则编译成 cubin
                              → 加载 cubin、组装启动配置        → 张量核执行矩阵乘加
                              → cuLaunchKernel(...)             → 结果写回显存
                              ← 返回结果张量 D
```

要点有三：

1. **形状是运行时才知道的**，所以 kernel 不能在安装时全部预编译——这就是「为什么要 JIT」。
2. **编译结果会被缓存**（默认在 `$HOME/.deep_gemm`），第二次相同形状的调用几乎零编译开销。
3. **架构派发是核心开关**：库会根据当前 GPU 是 SM90 还是 SM100 走不同的实现路径（这一机制由 `get_arch_major()` 返回 9 或 10 驱动，会在后续讲义详细展开）。

这些细节分别对应后续「JIT 编译系统」「宿主侧内核启动链路」「启发式与配置选择」等单元，本讲只需建立框架印象。

#### 4.1.3 源码精读

下面三段 README 原文是理解定位的钥匙。逐段看：

**① 项目定义** —— 框定 DeepGEMM 是什么：

[README.md:3](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L3-L3) 这一行就是前面引用的那句「unified, high-performance tensor core kernel library」，它一口气列出了 GEMM(FP8/FP4/BF16)、Mega MoE、MQA scoring、HyperConnection 等全部算子家族。

**② 设计哲学** —— 为什么轻量、为什么借鉴但不依赖 CUTLASS：

[README.md:5](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L5-L5) 这一行说明它「leverages some concepts from CUTLASS and CuTe, but avoids heavy reliance on their templates or algebras」，并强调「designed for simplicity, with only a limited number of core kernel functions」，还点出它适合作为「学习 NVIDIA GPU kernel 优化技术的干净资源」。

**③ 性能定位** —— 轻量不代表慢：

[README.md:7](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L7-L7) 这一行声称「performance matches or exceeds expert-tuned libraries across various matrix shapes」。News 时间线里 2025.04.18 的「在 H800 上达到 1550 TFLOPS」就是这条声明的实证。

> 这三条连起来就是 DeepGEMM 的自我定位：**做一个小而精、覆盖 LLM 关键算子、安装即用、性能拉满的张量核库**。

#### 4.1.4 代码实践

这是一个「源码阅读型」实践，帮你在脑中固化定位。

1. **实践目标**：用一段话（3–5 句）讲清 DeepGEMM 与 cuBLASLt、CUTLASS 三者的差异。
2. **操作步骤**：
   - 打开 [README.md:3-7](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L3-L7)，重点读「unified …」「leverages some concepts …」「performance matches …」三句。
   - 思考三个对比维度：**① 是否需要安装时编译**（DeepGEMM 不需要，靠运行时 JIT）；**② 代码复杂度/可学习性**（DeepGEMM 刻意轻量、少模板）；**③ 算子范围**（cuBLASLt 偏通用 BLAS，CUTLASS 极全但重，DeepGEMM 聚焦 LLM 关键算子）。
3. **需要观察的现象**：你会注意到 README 反复强调「lightweight」「simplicity」「no CUDA compilation during installation」——这正是它区别于 CUTLASS 的卖点。
4. **预期结果**：你能写出类似这样的总结：「cuBLASLt 是 NVIDIA 官方的通用 GEMM 库，预编译、黑盒；CUTLASS 是功能极全但模板极重的源码库；DeepGEMM 则是一个轻量、安装免编译、运行时 JIT、专门为 LLM 关键算子（FP8/FP4 GEMM、MoE、MQA 等）优化的张量核库，代码干净、适合学习。」
5. 本实践为阅读型，**待本地验证**的部分：如果你手头有相应 GPU，可后续在「环境搭建」一讲中真正跑起来验证「安装免编译」的体验。

#### 4.1.5 小练习与答案

**练习 1**：DeepGEMM 说自己「avoids heavy reliance on CUTLASS templates」。这和「借鉴 CUTLASS 概念」矛盾吗？

> **参考答案**：不矛盾。「借鉴概念」指吸收了分块、流水线、TMA 描述符等**思想**；「不重度依赖模板」指**实现层面**不照搬 CUTLASS/CuTe 那套层层嵌套的 C++ 模板与代数抽象，而是用更直白的 CUDA C++ 重写。目的是让代码更易读、更易学。

**练习 2**：为什么 DeepGEMM 选择「运行时 JIT 编译」而不是「把所有形状的 kernel 在安装时全编译好」？

> **参考答案**：因为 GEMM 的形状（M/N/K）、数据类型、布局、分块配置组合起来是天文数字，无法在安装时穷举预编译。运行时 JIT 只为「真正被调用到的形状」按需生成 kernel，并用缓存避免重复编译，兼顾了灵活性与启动开销。

---

### 4.2 硬件与算子矩阵

#### 4.2.1 概念说明

DeepGEMM 不是「能在任何 GPU 上跑」的通用库，它有明确的硬件与算子边界。

**目标硬件**：仅支持 NVIDIA 两代架构：

- **SM90**：Hopper 架构，代表产品 H100/H800。
- **SM100**：Blackwell 架构，新一代旗舰。

这两代架构的能力不同，所以 DeepGEMM 对它们的支持范围也不同（后面会讲具体差异）。最低 CUDA 版本要求：SM90 需 CUDA 12.3+（强烈推荐 12.9+ 以获得最佳性能），SM100 需 CUDA 12.9+。

**算子家族**：从 Python 包入口 `deep_gemm/__init__.py` 的导出清单可以一目了然地看到 DeepGEMM 支持的全部算子。它们大致归为五类：

1. **稠密 GEMM（dense GEMM）**：FP8、FP8×FP4、BF16、以及 TF32/CUbase 的 cuBLASLt 对照。包括普通 `fp8_gemm_*`、`bf16_gemm_*`、`cublaslt_gemm_*` 等。
2. **分组 GEMM（grouped GEMM / MoE）**：M 轴分组（contiguous 连续布局 / masked 掩码布局）与 K 轴分组（用于权重梯度 wgrad）。
3. **Mega MoE**：把 EP dispatch + 两层 GEMM + SwiGLU + EP combine 融合成单个超大 kernel，并用对称内存重叠 NVLink 通信与计算。
4. **MQA 评分内核（lightning indexer）**：为 DeepSeek v3.2 闪电索引器计算 token-to-token 的加权 ReLU MQA logits，分 non-paged（预填充）与 paged（解码）两版。
5. **HyperConnection（HC）与 Einsum**：`tf32_hc_prenorm_gemm` 预归一化 GEMM，以及硬编码的 `einsum` / `fp8_einsum` 爱因斯坦求和。

#### 4.2.2 核心流程

理解算子矩阵的关键，是抓住「**架构差异**」这条主线。DeepGEMM 在两个层面因架构而异：

```text
调用一个算子(如 fp8_gemm_nt)
   │
   ├─ 布局支持差异：SM90 只支持 NT；SM100 支持 NT/TN/NN/TT 全部
   │
   └─ 缩放因子(SF)格式差异：
        ├─ SM90：FP32 缩放因子
        └─ SM100：打包 UE8M0（4 个 UE8M0 打进一个 torch.int）
```

也就是说，同一个 Python 函数名，在 SM90 和 SM100 上走的底层实现、对输入张量的格式要求是不同的。这套「按 `get_arch_major()` 返回 9 或 10 来派发」的机制贯穿全库，是后续「C++ 绑定与 API 派发层」一讲的核心。

#### 4.2.3 源码精读

**① 硬件与依赖要求** —— 明确的硬件边界：

[README.md:27-38](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L27-L38) 这段 Requirements 列出了：SM90/SM100 GPU、Python 3.8+、C++20 编译器、CUDA 12.3+（SM90）/12.9+（SM100）、PyTorch 2.1+、CUTLASS 4.0+、`{fmt}` 库。其中 CUTLASS 与 `{fmt}` 可以通过 git submodule 拉取。

**② 布局命名约定与架构差异** —— NT 是主布局：

[README.md:65](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L65-L65) 这一行说明：DeepGEMM 用 `D = C + A @ B` 约定，输入布局是 NT（non-transposed A, transposed B）；SM90 只支持 NT，SM100 支持全部 NT/TN/NN/TT。例如 `fp8_gemm_nt` 实际计算的是 \( D = C + A \cdot B^{\top} \)。

**③ 缩放因子格式差异** —— SM90 vs SM100：

[README.md:69-70](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L69-L70) 这两行说明：SM90 要求 FP32 格式的逐块缩放因子；SM100 要求打包的 UE8M0 格式（把 4 个 UE8M0 打进一个 `torch.int`）。这是低精度 GEMM 区分两代架构的关键点。

**④ Python 入口交叉验证** —— 真实导出的算子清单：

[deep_gemm/__init__.py:35-73](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L35-L73) 这是 `from ._C import (...)` 的主块，按注释分组导出了 FP8/FP4 GEMM、分组 GEMM、BF16 GEMM、Einsum、Attention(MQA)、HyperConnection、Layout 工具等。其中：

- [deep_gemm/__init__.py:37-42](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L37-L42)：FP8×FP4 GEMM 及其 M 轴分组版本。
- [deep_gemm/__init__.py:62-68](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L62-L68)：Attention 内核，即 MQA 评分家族（含 legacy 别名）。
- [deep_gemm/__init__.py:69-72](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L69-L72)：HyperConnection（`tf32_hc_prenorm_gemm`）与 Layout 工具。

[deep_gemm/__init__.py:83-90](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L83-L90) 单独从 `deep_gemm.mega` 导出 Mega MoE 家族（`SymmBuffer`、`fp8_fp4_mega_moe`、`bf16_mega_moe` 等）。

> 把 README 的文字描述和 `__init__.py` 的真实导出对照着看，你能确认「五类算子家族」不是宣传话术，而是实打实写在代码里的接口。

#### 4.2.4 代码实践

1. **实践目标**：列出 DeepGEMM 当前支持的「5 类核心 kernel 家族」，并各配一个典型应用场景。
2. **操作步骤**：
   - 打开 [deep_gemm/__init__.py:35-90](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L35-L90)。
   - 按注释分组（FP8/FP4 GEMM、Grouped、Attention、HyperConnection/ Einsum、Mega）归类，并回看 README 对应章节（如 [README.md:74-88](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L74-L88) 讲分组 GEMM、[README.md:90-112](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L90-L112) 讲 MQA、[README.md:114-140](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L114-L140) 讲 Mega MoE）。
3. **需要观察的现象**：你会发现 `__init__.py` 的导出顺序与 README 的 Interfaces 章节顺序高度一致，说明文档与代码是对齐的。
4. **预期结果**：得到如下表格（场景为常见用法，便于记忆）：

   | 家族 | 代表接口 | 典型场景 |
   | --- | --- | --- |
   | 稠密 GEMM | `fp8_gemm_nt`、`bf16_gemm_*` | 普通 Linear 层前向（如 FFN、投影） |
   | 分组 GEMM(MoE) | `m_grouped_fp8_gemm_nt_contiguous` / `_masked`、`k_grouped_*` | MoE 专家层前向/权重梯度 |
   | Mega MoE | `fp8_fp4_mega_moe` | MoE 整层融合（dispatch+两层 GEMM+SwiGLU+combine） |
   | MQA 评分 | `fp8_fp4_mqa_logits`、`fp8_fp4_paged_mqa_logits` | DeepSeek v3.2 闪电索引器 |
   | HyperConnection/ Einsum | `tf32_hc_prenorm_gemm`、`einsum`、`fp8_einsum` | HC 预归一化、特定 Einstein 求和 |

5. 本实践为阅读型；若想真正调用某一接口，需先完成「环境搭建」一讲。具体数值结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：SM90 上调用 `fp8_gemm_nn`（NN 布局）会发生什么？为什么？

> **参考答案**：SM90 原生只支持 NT 布局。`fp8_gemm_nn/tn/tt` 在 SM90 上是通过把输入做转置后复用 NT 实现来完成的（这一转置派生关系在 `csrc/apis/gemm.hpp` 里，会在「GEMM 命名约定与 NT 布局」一讲详解）。也就是说接口能用，但底层仍走 NT 路径；只有 SM100 才真正原生支持全部四种布局。

**练习 2**：FP8 GEMM 里，SM90 和 SM100 对「缩放因子（scaling factor）」的格式要求有什么不同？

> **参考答案**：SM90 要求 FP32 格式的逐块缩放因子；SM100 要求把缩放因子打包成 UE8M0 格式（4 个 UE8M0 压进一个 `torch.int`），并且 LHS 缩放因子还需要 TMA 对齐且转置的布局。这是两代架构 SF 处理路径的核心差异。

**练习 3**：`cublaslt_gemm_nt` 也出现在 DeepGEMM 的导出里，它的作用是什么？

> **参考答案**：它是 NVIDIA cuBLASLt 库的封装，主要作为**性能对照基线**。DeepGEMM 在测试和基准里用它来对比自己的 FP8/BF16 kernel 到底快了多少。

---

### 4.3 版本演进时间线

#### 4.3.1 概念说明

DeepGEMM 是一个高速演进的项目（从 2025 年 2 月首次提交到当前已是 `2.6.1` 版）。看懂它的 News 时间线，能帮你理解「为什么代码里会有 SM90 和 SM100 两套实现」「为什么 JIT 模块被反复重构」「Mega MoE / FP4 是什么时候加进来的」。

README 顶部的 News 章节[README.md:11-23](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L11-L23) 记录了几个关键里程碑。理解这条时间线的意义在于：**很多看似复杂的代码（双架构派发、JIT 缓存、Mega MoE 融合内核）都是为了支撑这些演进而逐步引入的**。

#### 4.3.2 核心流程

把 News 时间线整理成一张表（日期与内容均来自 README 的 News 章节及 git 提交记录）：

| 时间 | 里程碑 | 对代码/能力的影响 |
| --- | --- | --- |
| 2025.02 | 项目首次提交（git Initial commit） | 最初版本，仅面向 SM90（Hopper） |
| 2025.04.18 | H800 上达到 **1550 TFLOPS** | 性能持续打磨的阶段性成果 |
| 2025.05.07 | 引入 **NVRTC**，编译提速最高 10×（`DG_JIT_USE_NVRTC=1`） | JIT 有了 NVCC 与 NVRTC 两条后端 |
| 2025.05.14 | 支持稠密与 MoE 的**权重梯度（wgrad）**内核 | 引入 K 轴分组 GEMM |
| 2025.07.20 | 同时支持 **SM90/SM100**，JIT CPP 模块大重构（低 CPU 开销） | 出现全库的架构派发机制；早期 NVRTC/SASS 后处理一度禁用 |
| 2025.09.28 | 支持 **MQA 评分内核**（lightning indexer，DeepSeek v3.2） | 引入 attention 家族 |
| 2026.04.16 | **Mega MoE、FP8×FP4 GEMM、FP4 Indexer、PDL**、更快的 JIT | 引入 mega 融合内核、FP4 路径、PDL 启动特性 |

> 术语解释：
> - **NVRTC**：NVIDIA 的运行时编译器，在进程内编译 CUDA，比调用外部 nvcc 更快。DeepGEMM 用 `DG_JIT_USE_NVRTC=1` 切换。
> - **PDL（Programmatic Dependent Launch）**：一种让相邻 kernel 提前重叠启动的硬件特性，可由 `set_pdl` 控制。
> - **FP4 Indexer**：用 FP4 低精度做索引器评分，进一步降带宽、提吞吐。

#### 4.3.3 源码精读

News 章节本身就在 README 里，直接对应代码演进：

[README.md:11-13](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L11-L13) 这一条记录了 2026.04.16 引入 Mega MoE、FP8×FP4 GEMM、FP4 Indexer、PDL——这是当前代码里 `deep_gemm/mega/`、`sm100_fp8_fp4_*` 系列文件与 `set_pdl` 接口的由来。

[README.md:16-20](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L16-L20) 这一条记录了 2025.07.20 的 SM90/SM100 双架构支持与 JIT CPP 模块大重构——这是为什么全库处处可见 `get_arch_major()` 派发、以及 `csrc/jit/` 这套基础设施存在的根本原因。

版本号本身可以在 Python 包入口确认：

[deep_gemm/__init__.py:127](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L127-L127) 这一行 `__version__ = '2.6.1'`，对应上述演进累积到当前 HEAD 的版本。

#### 4.3.4 代码实践

1. **实践目标**：把 News 时间线里的每个里程碑，映射到代码里真实存在的文件/接口，建立「文档→代码」的对应感。
2. **操作步骤**：
   - 读 [README.md:11-23](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L11-L23) 的每一条 News。
   - 对每条，在仓库里找到它引入的产物。例如：2026.04 Mega MoE → `deep_gemm/mega/` 与 `tests/test_mega_moe.py`；MQA 评分 → `csrc/apis/attention.hpp`；SM90/SM100 双架构 → 设备侧 `deep_gemm/include/deep_gemm/impls/` 下同时存在 `sm90_*` 与 `sm100_*` 两套 `.cuh` 文件。
3. **需要观察的现象**：你会看到 `impls/` 目录里几乎所有算子都有 `sm90_*` 和 `sm100_*` 两个版本（如 `sm90_fp8_gemm_1d1d.cuh` 与 `sm100_fp8_fp4_gemm_1d1d.cuh`），这正是「双架构支持」在文件层面的直接体现。
4. **预期结果**：你能口述「这条 News 引入了哪个目录/接口」，从而把抽象的版本号落到具体源码上。
5. 本实践为阅读型，目录结构**已可确认**（见下方目录），无需运行即可完成。

   ```text
   deep_gemm/include/deep_gemm/impls/
   ├── sm90_fp8_gemm_1d1d.cuh      # SM90 FP8 GEMM
   ├── sm100_fp8_fp4_gemm_1d1d.cuh # SM100 FP8×FP4 GEMM
   ├── sm100_fp8_fp4_mega_moe.cuh  # SM100 Mega MoE 融合内核
   └── ... (每个算子基本都有 sm90 / sm100 两版)
   ```

#### 4.3.5 小练习与答案

**练习 1**：为什么 `deep_gemm/include/deep_gemm/impls/` 下会同时有 `sm90_*` 和 `sm100_*` 两套 `.cuh` 文件？

> **参考答案**：因为 2025.07.20 起 DeepGEMM 同时支持 SM90 与 SM100 两代架构，而两代的张量核指令（WGMMA vs UMMA）、TMA 能力、SF 格式都不同，无法用一套代码通吃，所以为每个算子分别实现了 `sm90_*` 和 `sm100_*` 两个版本，由运行时按 `get_arch_major()` 派发。

**练习 2**：Mega MoE 是哪个版本引入的？它融合了哪几个阶段？

> **参考答案**：2026.04.16 引入。它把 EP dispatch、Linear1(FP8×FP4)、SwiGLU、Linear2(FP8×FP4)、EP combine 融合成单个 mega-kernel，并用对称内存把 NVLink 通信与张量核计算重叠起来。

**练习 3**：`DG_JIT_USE_NVRTC=1` 这个开关背后的演进故事是什么？

> **参考答案**：2025.05.07 引入 NVRTC 后端，编译提速最高 10×；但早期在某些 case 会有性能损失。后来（2025.07.20 重构期）NVRTC 一度被禁用以稳定性能，再之后重新支持。这说明「编译速度」与「kernel 运行性能」之间存在权衡，是 DeepGEMM 反复打磨的重点。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个综合任务（这是本讲的主实践）：

**任务**：阅读 README 全文后，产出一份「DeepGEMM 速览卡片」，包含两部分：

1. **定位对比**：用一段话（3–5 句）总结 DeepGEMM 与 **cuBLASLt**、**CUTLASS** 的差异（建议从：是否安装时编译、代码复杂度/可学习性、算子范围三个维度切入）。
2. **算子清单**：列出 DeepGEMM 当前支持的 **5 类核心 kernel 家族**，每类给出一个代表接口名和典型应用场景。

**操作步骤**：

1. 通读 [README.md:3-7](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L3-L7)（定位与哲学）与 [README.md:61-158](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L61-L158)（Interfaces 与 Utilities）。
2. 用 [deep_gemm/__init__.py:35-90](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L35-L90) 的真实导出清单交叉核对你的算子清单，确保每个家族都有真实接口支撑。
3. 写下你的速览卡片。

**预期结果**（参考）：

- **定位**：cuBLASLt 是预编译、黑盒的通用 BLAS；CUTLASS 是功能极全但模板极重的源码库；DeepGEMM 则是轻量、安装免编译、运行时 JIT、专为 LLM 关键算子优化的张量核库，代码干净、适合学习。
- **五大家族**：① 稠密 GEMM（`fp8_gemm_nt`，普通 Linear）；② 分组 GEMM/MoE（`m_grouped_fp8_gemm_nt_contiguous`，专家层）；③ Mega MoE（`fp8_fp4_mega_moe`，整层融合）；④ MQA 评分（`fp8_fp4_mqa_logits`，闪电索引器）；⑤ HyperConnection/Einsum（`tf32_hc_prenorm_gemm`、`einsum`，HC 与特定求和）。

> 本实践为阅读与归纳型，无需运行 GPU 即可完成；真正「跑起来」留到下一讲「环境要求与构建安装」。

---

## 6. 本讲小结

- DeepGEMM 是一个**面向 SM90/SM100 的统一高性能张量核 kernel 库**，覆盖 FP8/FP4/BF16 GEMM、分组 GEMM(MoE)、Mega MoE、MQA 评分、HyperConnection 等 LLM 关键算子。
- 它的**设计哲学**是：轻量（核心 kernel 数量有限）、运行时 JIT（安装免 CUDA 编译）、借鉴 CUTLASS/CuTe 概念但不重度依赖其模板，刻意保持代码「干净、可学习」。
- **硬件边界**明确：仅支持 SM90（Hopper，CUDA 12.3+）与 SM100（Blackwell，CUDA 12.9+），全库围绕 `get_arch_major()`（9 或 10）做架构派发。
- **架构差异**体现在两处：SM90 只支持 NT 布局而 SM100 支持全部四种；SM90 用 FP32 缩放因子，SM100 用打包 UE8M0。
- **版本演进**：从 2025.02 的 SM90-only，到 2025.07 双架构+JIT 重构、2025.09 MQA 评分，再到 2026.04 的 Mega MoE / FP4 / PDL，当前版本 `2.6.1`。
- 阅读 DeepGEMM 的正确姿势是「**文档（README）与代码（`__init__.py` 导出）对照看**」，二者高度对齐。

---

## 7. 下一步学习建议

本讲只是建立了整体印象，还没有真正接触代码细节。建议按以下顺序继续：

1. **下一讲 u1-l2《环境要求与构建安装》**：动手把 DeepGEMM 在本机或容器里构建起来，亲手验证「安装免编译、运行时 JIT」的体验。
2. **u1-l3《目录结构与分层架构》**：建立 Python → C++ 绑定 → JIT → 设备 kernel 的分层心智模型，为后续逐层下钻做准备。
3. **u1-l4《Python 接口全貌与第一次调用》**：完成你的第一次真实 `fp8_gemm_nt` 调用并校验数值正确性。
4. **想先尝鲜源码的读者**：可以提前扫一眼 `csrc/python_api.cpp`（pybind11 入口）和 `deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh`（一个完整设备 kernel 的样貌），但这些会在进阶层（Unit 3–6）系统讲解，现在看不懂很正常。

> 提醒：DeepGEMM 横跨 Python / C++ / CUDA PTX 三层，复杂度高。本手册采用「自顶向下逐层下钻」的策略——先在入门层把环境、目录、接口跑通，再在进阶层拆解 JIT 与宿主链路，最后在专家层进入设备内核与 Mega MoE。不要急于一开始就钻进 PTX 细节。
