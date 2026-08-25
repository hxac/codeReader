# 第 1 讲：项目定位与全书学习地图

## 1. 本讲目标

读完本讲，你应该能够：

- 说出 `modern-gpu-programming-for-mlsys` 这个仓库**是什么**：一本以 NVIDIA Blackwell GPU 为对象、以 TIRx Python DSL 为工具的开源 GPU 内核编程教材（书站）。
- 说出它的**目标读者**与**教学主线**：理解硬件 → 学会编程 → 写出 SOTA（state-of-the-art，当前最优水平）内核。
- 复述全书 **Part I–IV 与附录**各自解决的问题，并能在仓库中找到每一章对应的源文件。
- 识别贯穿全书的**两条主线内核**（GEMM 与 Flash Attention）和**三个核心优化思想**（数据布局、异步数据搬运、异步协调）。
- 产出一分属于自己的**个人学习计划表**，作为后续所有讲义的导航。

本讲不要求你写过任何 GPU 代码，也不要求有 GPU 机器。

## 2. 前置知识

本讲是整套手册的第一讲，前置知识几乎为零。以下几个术语会用通俗语言解释，读完即可：

- **GPU 内核（kernel）**：一段运行在 GPU 上、被 CPU 端程序启动的函数。注意它和操作系统里的 "kernel"（内核）不是一回事——GPU 语境下 kernel 就是指 "设备端程序"。深度学习里绝大多数算子（矩阵乘法、attention 等）最终都要落到一个或多个 GPU 内核上执行。
- **GEMM**（GEneral Matrix-Matrix multiplication，通用矩阵乘法）：计算 \( D = A \times B^T \) 一类的稠密矩阵乘。它是深度学习负载的算力基本盘：全连接层、attention 里的投影、低精度 block-scaled GEMM 都以它为核心。
- **Attention / Flash Attention**：Transformer 的核心算子，数学上是 "softmax(QKᵀ)·V"。它比 GEMM 多了在线 softmax、掩码等步骤，是检验内核功底的更难考题。本仓库的 Part IV 会构建 Flash Attention 4（FA4）。
- **DSL**（Domain-Specific Language，领域特定语言）：为某一领域专门设计的编程语言。本书用的 TIRx 是一个 **Python DSL**——你写的是合法 Python，但会被解析成编译器 IR（中间表示）再生成 CUDA。
- **IR**（Intermediate Representation，中间表示）：编译器内部对程序的结构化表达。TIRx 的名字 "Tensor IR next" 就意味着它是 "面向张量计算的下一代 IR"，直接在 IR 层面写内核。
- **Blackwell / sm_100a**：NVIDIA 的 GPU 架构代号（B200 等芯片属于这一代），`sm_100a` 是它对应的编译目标。书中内核只保证在这代硬件上可运行。
- **Sphinx / MyST**：Python 社区常用的文档站点生成器。本书整个仓库就是一个 Sphinx 站点源码，正文是 Markdown（MyST 方言）与 reStructuredText 混合。

如果你对以上某些概念仍模糊，不必停下——本讲只要求你建立地图，细节会在后续对应单元展开。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用途 |
| --- | --- | --- |
| [README.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md) | 仓库门面：项目一句话定位、内容总览、本地构建方法、运行内核的环境要求 | 讲解项目定位与教学主线 |
| [index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/index.md) | 书站首页：写作动机、全书组织方式的正式描述，以及五个 Sphinx toctree（目录树） | 讲解四部分结构与附录的官方定义 |
| `chapter_*/index.md`（15 个章节文件） | 各章正文入口，文件名即章节名 | 本讲只取其标题验证结构，不深入内容 |
| `appendix/*.md`、`tirx_guide/**/*.rst` | 附录正文 | 同上，仅用于确认附录范围 |

> 说明：本仓库是"教材即代码"——正文本身就是仓库的主要内容，没有传统意义上的 `src/` 源码目录。因此本手册所说的"源码精读"，大部分时候是**精读书稿原文与其引用的内核代码块**。

## 4. 核心概念与源码讲解

### 4.1 模块一：README 项目定位——这本书教什么、用什么教

#### 4.1.1 概念说明

打开任何开源项目，第一个该读的文件都是 README。这个仓库的 README 用开篇两段话回答了三个问题：

1. **教什么**：现代 GPU 内核编程，遵循一条递进路线——理解 GPU 硬件 → 学会给它编程 → 写出最先进的内核。
2. **以什么为对象**：Blackwell 一代 GPU 被当作"真正的主角"——它的内存层级与 Tensor Memory、Tensor Core 与异步数据搬运引擎、warpgroup 与 cluster，都是本书要解剖的实体。
3. **用什么工具**：TIRx（Tensor IR next），一个在 IR 层面编写 GPU 内核的 Python DSL。

这个定位很重要：它声明本书**不是优化技巧清单**，而是把硬件当作学习对象、把 DSL 当作解剖工具的系统性教材。理解了这一点，你就能预期后续章节的写法——先讲硬件机制本身，再讲如何用它。

另外，README 还交代了这本书的双语与在线阅读入口（英文主站与中文镜像站），以及它由 GitHub Actions 自动部署。

#### 4.1.2 核心流程

README 传达的教学主线可以画成一条单向递进的路：

```text
理解硬件（Blackwell 的执行层级 / 存储空间 / 异步引擎）
        ↓
学会编程（用 TIRx 把硬件选择显式写成代码）
        ↓
写出 SOTA 内核（GEMM 九步进化到对齐 cuBLAS，再用同样技术造 Flash Attention 4）
```

对应到读者动作上，README 给出的使用流程是：

1. 在线阅读（两个 URL），或克隆仓库本地构建（Sphinx）。
2. 想运行书中内核：准备 Blackwell GPU + TIRx 编译器（`apache-tvm` wheel 里的 `tvm.tirx` 模块）+ CUDA 版 PyTorch，可选安装 `tirx-kernels` 参考内核库。
3. 想参与贡献：通过 GitHub 仓库提交修正与示例。

#### 4.1.3 源码精读

**（1）一句话定位与教学主线**。[README.md:L3-L7](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L3-L7) 开宗明义：这本书把内核编程当作一条 "理解硬件 → 编程 → 写 SOTA 内核" 的递进过程来教；主角是 Blackwell 级 GPU 本身（内存层级与 Tensor Memory、Tensor Core 与异步数据搬运引擎、warpgroup 与 cluster），载体是 TIRx——"一个在 IR 层面编写 GPU 内核的 Python DSL"。这两句是整个仓库的"宪法"，后面所有章节都在展开它。

**（2）在线阅读入口**。[README.md:L9-L11](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L9-L11) 给出英文主站与中文版地址。中文读者可以直接用中文镜像站对照本手册学习。

**（3）内容总览（What's inside）**。[README.md:L16-L30](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L16-L30) 用五个条目分别概括 Part I（理解 GPU：执行与内存模型、roofline 性能模型、数据布局、TMA/Tensor Memory/Tensor Core、异步协调、CLC 高级调度）、Part II（用 TIRx 编程：一个可运行的单 MMA GEMM 引出 scope/layout/dispatch，加上 `TileLayout` 布局模型）、Part III（GEMM：从分块到 SOTA——TMA 流水线、持久调度、warp 特化、2-CTA cluster）、Part IV（Flash Attention 4：两个 MMA 夹一个 softmax、在线 softmax 重缩放、causal 掩码、GQA）与附录（语言参考、可复现基准测试与剖析、编译器内部、异步内核调试）。这是本讲 4.2 节表格的依据。

**（4）本地构建**。[README.md:L32-L45](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L32-L45) 说明书是 Sphinx 站点（Markdown/MyST + reStructuredText），给出两条命令：`pip install -r requirements-docs.txt` 装依赖、`sphinx-build -b html . _build/html` 构建，再用 `python -m http.server -d _build/html 8000` 本地预览。远程机器上需要端口转发。详细操作留给下一讲（u1-l2）。

**（5）运行内核的硬件门槛**。[README.md:L51-L54](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L51-L54) 明确：书中内核目标平台是 Blackwell（`sm_100a`），运行需要 Blackwell GPU（如 B200）、TIRx 编译器和 CUDA 版 PyTorch。**没有这代 GPU 不影响读书**，只是无法执行内核——这正是本手册大量实践设计为"源码推演型"的原因。

**（6）TIRx 编译器的安装与验证**。[README.md:L56-L66](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L56-L66) 说明 TIRx 随 Apache TVM wheel 发布（`tvm.tirx` 模块），安装 `apache-tvm==0.26.0 cuda-bindings` 后用 `python -c "import tvm, tvm.tirx; print(tvm.__version__)"` 验证。

**（7）一个容易踩的坑**。[README.md:L83-L84](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L83-L84) 特意强调：TIRx 通过 **Python 源码检视（source inspection）**解析内核，所以示例必须写在文件或 notebook 单元格里，不能塞进 `python -c "..."`。这是初学者第一天就可能撞上的约束。

**（8）自动部署**。[README.md:L86-L89](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L86-L89) 说明每次推送到 `main` 都会由 GitHub Actions 自动构建发布到官网站点——这也是为什么正文源文件在仓库根目录而不是 `docs/` 下。

#### 4.1.4 代码实践

**实践 A：验证你与这个仓库的"接口"是否通畅**

1. **实践目标**：不写任何代码，确认你能读 README、能打开在线书站、能克隆仓库，为整个学习手册建立物理基础。
2. **操作步骤**：
   1. 在浏览器打开 README 中给出的英文主站 `https://mlc.ai/modern-gpu-programming-for-mlsys/`，浏览首页。
   2. 打开中文版 `https://mlc.ai/modern-gpu-programming-for-mlsys/zh/`，对照看两版目录是否一致。
   3. 在终端执行 `git clone https://github.com/mlc-ai/modern-gpu-programming-for-mlsys.git`（或在本机已有的仓库副本中操作）。
   4. 克隆后进入目录，运行 `ls`，对照第 3 节的源码地图确认 `README.md`、`index.md`、15 个 `chapter_*` 目录、`appendix/`、`tirx_guide/`、`zh/` 都在。
3. **需要观察的现象**：书站能正常打开并显示 "Modern GPU Programming For MLSys" 标题；克隆得到的目录结构与本讲第 3 节的地图吻合。
4. **预期结果**：两项都通过，说明后续所有"读源码"实践都有了落脚点。（书站可达性与克隆速度依赖网络环境，若失败请先解决网络问题再继续。）

**实践 B：用 README 的原话回答三个问题**

1. **实践目标**：训练"从 README 提取项目定位"的能力，这是你未来接触任何新开源项目的通用第一步。
2. **操作步骤**：打开 [README.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md)，**只读前 30 行**，然后合上文件，用自己的话（每题一句）回答：① 这本书的教学路线是什么？② 它把什么当作"真正的主角"？③ TIRx 是什么？
3. **需要观察的现象**：你能否不看原文复述出 "understand the GPU hardware → learn to program it → write state-of-the-art kernels" 这条线，以及 "TIRx = 在 IR 层面写 GPU 内核的 Python DSL" 这个定义。
4. **预期结果**：三题都能答出大意即通过；若答不出，重读 4.1.3 的第（1）条引用。

#### 4.1.5 小练习与答案

**练习 1**：为什么这个仓库说 Blackwell GPU 是 "the real subject"（真正的主角），而不是 TIRx？

<details>
<summary>参考答案</summary>

因为本书的教学目标是**理解硬件**：内存层级、Tensor Memory、Tensor Core、异步数据搬运引擎、warpgroup、cluster 这些硬件实体才是要掌握的知识本体；TIRx 只是"载体/解剖工具"（vehicle），它的价值恰恰在于把这些硬件层面的选择**显式地**留在代码里，方便读者推理控制流、访存与同步。工具会换，硬件原理长青。
</details>

**练习 2**：判断题——没有 Blackwell GPU，这套学习手册就完全无法学习。说法是否正确？

<details>
<summary>参考答案</summary>

不正确。README 明确运行内核需要 Blackwell（`sm_100a`），但**阅读、构建书站、推演布局与流水线、跑图表生成脚本**都不需要 GPU。本手册为此设计了大量"源码推演型实践"作为替代路径。
</details>

**练习 3**：TIRx 内核为什么不能写在 `python -c "..."` 里？

<details>
<summary>参考答案</summary>

因为 TIRx 通过 **Python 源码检视**（source inspection）来解析内核：它需要读到内核函数真实的源代码文本（文件或 notebook 单元格），而 `python -c` 传入的字符串无法被源码检视机制可靠获取。这是 README:L83-L84 明确提示的约束。
</details>

---

### 4.2 模块二：四部分结构——从硬件直觉到完整内核

#### 4.2.1 概念说明

`index.md` 是 Sphinx 站点的首页，也是全书结构唯一权威的定义处。它做了两件事：

1. **讲动机**：为什么机器学习系统工程师需要学内核编程——端到端性能越来越取决于少数关键 GPU 内核（attention、LLM prefill/decode、低精度 block-scaled GEMM、融合 MoE 层等）；而新一代 GPU 架构引入了更丰富的内存空间、新的数据搬运机制和越来越专用的执行单元，单靠优化技巧清单用不好它们，必须同时具备"硬件如何执行程序"的清晰认识和"朴素内核如何进化成高性能实现"的实践经验。
2. **给结构**：正文按 Part I → II → III → IV 递进组织，外加四个附录。每个 Part 对应 `index.md` 中的一个 toctree（Sphinx 的目录树指令），toctree 里列出的文件就是该部分的真实章节。

理解这个结构的关键是看到它的**递进逻辑**：Part I 建立硬件直觉（不写代码也能懂），Part II 给出编程模型（开始写代码），Part III 用 GEMM 把技术练到 SOTA（九步进化），Part IV 用 Flash Attention 4 综合检验（更复杂的真实算子）。附录则是查阅型内容。

#### 4.2.2 核心流程

读者沿全书结构前进时，知识依赖关系如下：

```text
Part I  理解硬件（9 章）
   执行模型 → 性能模型 → 数据布局 → Tensor Core 布局三代演进
   → TMA → tcgen05 → TMEM → mbarrier → CLC
        ↓  提供"硬件上有什么、为什么快"的直觉
Part II TIRx 编程模型（2 章）
   TIRx 入门（scope/layout/dispatch）→ TileLayout API
        ↓  提供"怎么把硬件选择写成代码"的语言
Part III GEMM：从分块到 SOTA（3 章）
   基础分块 → TMA 流水线 → warp 特化与 cluster
        ↓  提供一条完整可复现的优化路径（九步）
Part IV Flash Attention 4（1 章）
   用 Part III 的全部技术构建一个真实算子
        ↓
附录（4 个）：语言参考 / 基准测试与剖析 / 编译器内部 / 异步内核调试
```

本学习手册的单元划分（见手册大纲）正是沿这条线做的，只是把每章再拆成若干篇讲义。

#### 4.2.3 源码精读

**（1）动机：关键内核决定端到端性能**。[index.md:L3-L6](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/index.md#L3-L6) 指出：随着模型变大、部署环境变复杂，端到端性能越来越取决于少数关键 GPU 内核——attention、LLM prefill 与 decode、低精度 block-scaled GEMM、融合 MoE 层等大型融合内核直接影响训练与推理速度。这解释了"为什么值得学"。

**（2）动机：技巧清单不够用**。[index.md:L8-L12](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/index.md#L8-L12) 说明：让这些内核变快需要的不是一串优化技巧，新架构带来的新内存空间、新数据搬运机制、新专用执行单元，要求同时理解"硬件如何执行程序"与"朴素内核如何进化为高性能实现"。本书两者都教。

**（3）主线与三个核心思想**。[index.md:L14-L19](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/index.md#L14-L19) 是全书中信息密度最高的几行：全书从硬件到编程模型再到完整内核；主要目标是 NVIDIA Blackwell；贯穿的运行示例是 **GEMM 和 FlashAttention**；沿途建立 GPU 优化的三个关键思想——**数据布局（data layout）、异步数据搬运（asynchronous data movement）、异步协调（asynchronous coordination）**。本讲 4.3 节会展开这一条。

**（4）出身与工具立场**。[index.md:L21-L25](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/index.md#L21-L25) 交代本书源自 CMU 的 Machine Learning Systems 课程系列；选用 TIRx 是为了让思想能在真实内核中被研究、运行和验证——它保持硬件层面的选择**显式**，使读者能一边对着可运行代码一边推理控制流、访存与同步。

**（5）五个 Part 的官方定义**。[index.md:L30-L43](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/index.md#L30-L43) 逐条定义：Part I 建立"其余全书依赖的硬件直觉"；Part II 介绍 TIRx 关键要素，是全书代码示例的基础；Part III 是优化分块 GEMM 的完整指南（TMA 流水线、持久调度、warp 特化、2-CTA cluster）；Part IV 用 Part III 技术构建完整 attention 内核；附录含语言参考、可复现基准测试与剖析工作流、编译器内部、异步内核调试指南。

**（6）五个 toctree 与章节文件的对应**。[index.md:L45-L93](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/index.md#L45-L93) 的五个 `{toctree}` 块把 Part 与文件一一对上。结合各章文件首行标题（已逐一核实），得到下表：

| Part | 章节文件 | 章章标题（原文） | 中文释义 |
| --- | --- | --- | --- |
| I 理解 GPU | `chapter_background/index.md` | GPU Execution Model | GPU 执行模型（线程层级、存储空间） |
| | `chapter_performance/index.md` | What Makes a Kernel Fast | 什么让内核变快（roofline 等） |
| | `chapter_data_layout/index.md` | Data Layout and Its Notation | 数据布局及其记号 |
| | `chapter_layout_generations/index.md` | The Evolution of Tensor Core Data Layouts | Tensor Core 数据布局三代演进 |
| | `chapter_tma/index.md` | Async Data Movement: TMA | 异步数据搬运：TMA |
| | `chapter_tensor_cores/index.md` | Blackwell Tensor Core: `tcgen05.mma` | Blackwell Tensor Core |
| | `chapter_tmem/index.md` | Tensor Memory (TMEM) | Tensor Memory |
| | `chapter_async_barriers/index.md` | Async Coordination: mbarrier | 异步协调：mbarrier |
| | `chapter_clc/index.md` | Advanced Scheduling: Cluster Launch Control | 高级调度：CLC |
| II TIRx | `chapter_intro_tirx/index.md` | Introduction to TIRx | TIRx 入门 |
| | `chapter_tirx_layout_api/index.md` | TIRx Layout API | TIRx 布局 API |
| III GEMM | `chapter_gemm_basics/index.md` | Building a Tiled GEMM | 构建分块 GEMM（Step 1–3） |
| | `chapter_gemm_async/index.md` | Pipelining GEMM with TMA | 用 TMA 给 GEMM 建流水线（Step 4–6） |
| | `chapter_gemm_advanced/index.md` | Scaling GEMM with Warp Specialization and Clusters | 用 warp 特化与 cluster 扩展 GEMM（Step 7–9） |
| IV FA4 | `chapter_flash_attention/index.md` | Flash Attention 4 | Flash Attention 4 |
| 附录 | `tirx_guide/language_reference/index.rst` | TIRx Language Reference | TIRx 语言参考 |
| | `appendix/benchmarking_gpu_kernels.md` | Measuring and Analyzing GPU Kernel Performance | GPU 内核测量与分析 |
| | `tirx_guide/arch/index.rst` | TIRx Compiler Architecture | TIRx 编译器内部 |
| | `appendix/debugging_warp_specialized.md` | Debugging Warp-Specialized Kernels | 调试 warp 特化内核 |

（附录还有一个 `appendix/index.md` 作为分组入口。）

#### 4.2.4 代码实践

**实践：亲手数一遍 toctree，建立"结构即文件"的手感**

1. **实践目标**：验证全书结构不是背出来的，而是可以从 `index.md` 里**推导**出来的。
2. **操作步骤**：
   1. 打开本地仓库的 `index.md`，定位到第一个 ```` ```{toctree} ```` 块（约 L45 起）。
   2. 数一数 Part I 的 toctree 列出了几个文件（应为 9 个，且都是 `chapter_*/index` 形式）。
   3. 对后续四个 toctree 重复计数：Part II（2 个）、Part III（3 个）、Part IV（1 个）、附录（5 个，含 `appendix/index`）。
   4. 用 `ls chapter_* -d` 列出仓库里实际的章目录，与 toctree 对账：不应有"目录存在却没进目录树"的遗漏。
3. **需要观察的现象**：toctree 里的每个条目都能在仓库中找到同名文件/目录；Part III 的 toctree 使用 `:maxdepth: 2`（比其他 Part 深 1 层，因为它每章内部还有小节层级）。
4. **预期结果**：计数结果为 9 / 2 / 3 / 1 / 5，且文件对账无缺漏。若与你数出的不一致，请重数并核对 `:caption:` 标签——每个 toctree 的 caption 正是 Part 名。

#### 4.2.5 小练习与答案

**练习 1**：如果只想"看懂别人写的内核"，能不能跳过 Part I 直接读 Part III？

<details>
<summary>参考答案</summary>

不建议。`index.md` 对 Part I 的定义是"建立其余全书所依赖的硬件直觉"（the hardware intuition that the rest of the book relies on）。Part III 的每一步优化（TMA、流水线、warp 特化、cluster）都直接调用 Part I 引入的机制；跳过 Part I 意味着读到 Step 4 之后会同时面对 TMA、mbarrier、TMEM 等多个未建立的概念。本手册的依赖关系（u11+ 依赖 u2/u6/u8 等单元）也体现了这一点。
</details>

**练习 2**：Part II 为什么只讲一个"单 MMA 的 GEMM"？

<details>
<summary>参考答案</summary>

README 对 Part II 的概括是"通过一个可运行的单 MMA GEMM 介绍 TIRx——scope、layout、dispatch，以及编译如何工作"。用一个最小可运行的完整内核做载体，能同时展示编程模型的全部三要素与"源码 → 编译 → 验证"的完整回路，而又不被优化细节干扰。优化的展开是 Part III 的任务。
</details>

**练习 3**：附录的四个部分分别对应什么使用场景？

<details>
<summary>参考答案</summary>

语言参考——写内核时查语法与原语；基准测试与剖析（`appendix/benchmarking_gpu_kernels.md`）——测内核性能与找瓶颈时用；编译器内部（`tirx_guide/arch/`）——想理解 TIRx 如何把 tile 操作降级成线程级 CUDA 时用；调试指南（`appendix/debugging_warp_specialized.md`）——内核死锁/出错/偏慢时按流程排查。附录是工具书，按需查阅而非顺序通读。
</details>

---

### 4.3 模块三：GEMM 与 Flash Attention 主线——两个内核串起全部技术

#### 4.3.1 概念说明

`index.md` 说得很清楚：全书的目标硬件是 NVIDIA Blackwell，**运行示例（running examples）是 GEMM 和 FlashAttention**。这两个内核不是随手选的：

- **GEMM 是"教学阶梯"**。它的计算结构规整（\( D = A \times B^T \)，A 是 M×K、B 是 N×K、D 是 M×N），性能瓶颈清晰（算力受限），因此适合作为逐步叠加优化技术的平台。Part III 把它从朴素实现一路推进九步，最终在 B200 上对齐 cuBLAS（NVIDIA 官方高性能矩阵库，业界事实基准）。
- **Flash Attention 是"毕业考题"**。它在两个 MMA 之间夹了一个在线 softmax，还要处理 causal 掩码与 GQA（grouped-query attention），数据流比 GEMM 复杂得多。Part IV 用 Part III 练出来的全部技术（TMA、流水线、warp 特化、TMEM 复用）把它完整造一遍。

支撑这两条主线的是 `index.md` 点出的**三个核心优化思想**：

1. **数据布局（data layout）**：数据在 SMEM/TMEM/寄存器里怎么摆，直接决定访存冲突与搬运效率（Part I 的布局章节、Part II 的 `TileLayout`）。
2. **异步数据搬运（asynchronous data movement）**：用 TMA 等引擎让数据搬运与计算重叠（Part I 的 TMA 章、Part III Step 4–5）。
3. **异步协调（asynchronous coordination）**：用 mbarrier 等机制协调多个异步参与者（Part I 的 mbarrier 章、Part III Step 7 的多角色流水线）。

GEMM 的九步进化恰好是这三个思想逐个落地、再组合的过程。

#### 4.3.2 核心流程

GEMM 主线的进化路线（Part III 的三个章节 × 每章三步）：

```text
Step 1  单 tile 同步内核          ┐
Step 2  K 循环累加                 ├ chapter_gemm_basics：先把"算对"做出来
Step 3  空间分块 / 多 CTA          ┘
Step 4  TMA 异步加载               ┐
Step 5  双缓冲软件流水线            ├ chapter_gemm_async：让搬运与计算重叠（异步搬运+协调）
Step 6  持久内核与 tile scheduler   ┘
Step 7  warp 角色划分              ┐
Step 8  双 CTA cluster             ├ chapter_gemm_advanced：让多个引擎/CTA 协作（协调+布局）
Step 9  多消费者                   ┘
终点：B200 上 M=N=K=4096，70 ms → 0.094 ms，对齐 cuBLAS
```

Flash Attention 主线（Part IV）在同样的机制之上追加：

```text
QKᵀ MMA → 在线 softmax（exp2/多项式/条件 rescale） → PV MMA
        + causal 掩码块分类 + GQA 行打包 + TMEM 布局复用 + 多角色屏障协议
```

从性能视角看，GEMM 的衡量标准是 TFLOPS：

\[ \text{TFLOPS} = \frac{2 \cdot M \cdot N \cdot K}{t \cdot 10^{12}} \]

其中 \( t \) 是内核耗时（秒），因子 2 来自乘加各计一次浮点操作。以书中终态为例：\( M=N=K=4096 \)、\( t = 0.094\,\text{ms} \)，代入可得约 \( 1.46 \times 10^{3} \) TFLOPS 量级——这正是 B200 这代硬件 fp16 稠密算力的量级（具体数值待本地验证，计算方式将在单元三与 u15-l4 展开）。

#### 4.3.3 源码精读

**（1）两条主线与三个思想的出处**。[index.md:L14-L19](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/index.md#L14-L19) 原文写明：主要目标是 NVIDIA Blackwell，运行示例是 GEMM 和 FlashAttention，沿途建立 GPU 优化的关键思想——data layout、asynchronous data movement、asynchronous coordination。**这是本手册反复回引的总纲**。

**（2）Part III 的技术清单**。[index.md:L38-L39](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/index.md#L38-L39) 概括 Part III 为"优化分块 GEMM 的完整指南，经由 TMA 流水线、持久调度、warp 特化与 2-CTA cluster 逐级构建"。这四项技术正是 Step 4–9 的内容。

**（3）Part IV 的构成**。[index.md:L40-L41](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/index.md#L40-L41) 概括 Part IV 为"用 Part III 技术构建的完整 attention 内核：两个 MMA 夹 softmax、在线 softmax 重缩放、causal 掩码、GQA"。这四个短语就是 FA4 章的骨架。

**（4）GEMM 主线的性能终点**。[chapter_gemm_advanced/index.md:L864](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L864) 交代测量条件：NVIDIA B200、`M=N=K=4096`、fp16 输入、锁定时钟、每版本 1000 次计时迭代，并要求新测量遵循基准附录的完整协议；[chapter_gemm_advanced/index.md:L877](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L877) 的性能表把 cuBLAS 参考列为 0.094 ms（约 744× 于朴素基线）；[chapter_gemm_advanced/index.md:L902](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L902) 总结：TMA + 软件流水线 + 持久调度 + warp 特化 + cluster 级复用共同把耗时从 **70 ms 降到 0.094 ms**，在同一测试条件下对齐 cuBLAS——并强调这是多项优化**跨数据搬运、执行重叠与片上复用的协同**，而非任何单一机制的功劳。这句话本身就呼应了三个核心思想。

**（5）为什么是这两个内核**。[index.md:L3-L6](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/index.md#L3-L6) 列举的真实负载里，attention 与低精度 block-scaled GEMM 都榜上有名——主线内核正是从工业界真实热点里选出来的，学完即可对接真实场景。

#### 4.3.4 代码实践

**实践：用 grep 在仓库中"看见"三个核心思想**

1. **实践目标**：把 `index.md` 里抽象的三个思想，落到仓库中真实存在的章节与代码块上，证明它们不是口号。
2. **操作步骤**：
   1. 在仓库根目录执行 `grep -ril "swizzle" --include="*.md" chapter_data_layout chapter_tma chapter_tirx_layout_api | head`（swizzle 是数据布局思想的代表技术），记下命中的章节数。
   2. 执行 `grep -ril "TMA" --include="*.md" chapter_gemm_async | head`（异步搬运在 GEMM 章的落地）。
   3. 执行 `grep -c "mbarrier" chapter_gemm_advanced/index.md`（异步协调在 Step 7–9 中的出现密度）。
   4. 把三个结果填进一张三列表格：思想 / 代表机制 / 命中章节。
3. **需要观察的现象**：三个思想各自在 Part I（讲原理）、Part II/III（写代码）都有大量命中；`chapter_gemm_advanced` 中 mbarrier 出现次数显著多于 `chapter_gemm_basics`（后者接近于零——前几步还用不到异步协调）。
4. **预期结果**：得到一张能直观展示"思想 → 章节"分布的表格。命中的具体数字依赖仓库当前版本，**待本地验证**；但"basics 少、advanced 多"的对比趋势应当成立，若不成立请回来重读 4.3.2 的九步路线找原因。

#### 4.3.5 小练习与答案

**练习 1**：GEMM 与 Flash Attention 两条主线各自承担什么教学职能？

<details>
<summary>参考答案</summary>

GEMM 是**教学阶梯**：结构规整、瓶颈清晰，用来逐级叠加并验证优化技术（Part III 九步，从 70 ms 到对齐 cuBLAS）。Flash Attention 是**毕业考题**：在两个 MMA 之间加入在线 softmax、causal 掩码、GQA 等真实复杂度，检验同样的技术（TMA、流水线、warp 特化、TMEM 复用）能否迁移到更难的数据流（Part IV）。
</details>

**练习 2**："70 ms → 0.094 ms 对齐 cuBLAS"主要归功于哪一个机制？

<details>
<summary>参考答案</summary>

这是陷阱题——`chapter_gemm_advanced/index.md:L902` 明确说结果来自"跨数据搬运、执行重叠与片上复用协同组织的多项优化"，而非任何单一机制。TMA、软件流水线、持久调度、warp 特化、cluster 级复用缺一不可，这也正是三个核心思想（布局/异步搬运/异步协调）需要组合使用的证据。
</details>

**练习 3**：用书中的测量条件估算终态内核的 TFLOPS（只列算式）。

<details>
<summary>参考答案</summary>

按 4.3.2 的公式：\( \text{TFLOPS} = \frac{2 \times 4096^3}{0.094 \times 10^{-3} \times 10^{12}} = \frac{2 \times 4096^3}{9.4 \times 10^{7}} \approx 1459 \) TFLOPS。注意这只是按表中数据的一次估算，实际复测必须遵循 `appendix/benchmarking_gpu_kernels.md` 的协议（锁定时钟、预热、多迭代等），**具体数值待本地验证**。
</details>

## 5. 综合实践

**任务：制作你的《个人学习计划表》**（本讲规格中指定的正式实践任务）

这个任务把本讲三个模块的输出合并成一份可以贯穿整个手册使用的文档。建议存为 `modern-gpu-programming-for-mlsys-tutorial/my-learning-plan.md`（属于讲义目录内的个人笔记，不违反"不改源码"的约束）。

**步骤**：

1. **通读目录**：打开在线书站（或本地 `index.md`），按 Part I → II → III → IV → 附录的顺序通读全部章节标题。
2. **建章节清单**：参照 4.2.3 的表格，为每一章写一行：`章节文件 | 原标题 | 一句话摘要（自己写，不要抄本手册）`。摘要的来源可以是该章文件开头的 Overview 告示块（每个 `chapter_*/index.md` 开头都有 `:::{admonition} Overview`），也可参考本手册对应讲义的"主题"字段。
3. **自评前置知识**：对照下表逐项打勾（✅ 已具备 / 🟡 模糊 / ❌ 缺失）：

   | 前置项 | 对应章节 | 我的水平 |
   | --- | --- | --- |
   | 线程/锁/并发的直觉 | Part I 执行模型、mbarrier | |
   | 矩阵乘法与分块 | Part III 全部 | |
   | C 家族语言（指针、位运算） | TIRx 各章 | |
   | Python 装饰器/源码检视 | Part II | |
   | CUDA 或 GPU 编程经验 | 全书（非必需） | |
   | softmax / attention 公式 | Part IV | |

4. **标注重点与顺序**：给每一章标 `优先级（高/中/低）` 和 `计划周次`；❌ 缺失的项安排在前置章节里补。
5. **回填验证**：把第 2 步的摘要与本手册对应讲义的 topic 字段对照，偏差大的章节说明你理解有误或值得重点学。

**验收标准**：表格覆盖全部 15 个章节文件 + 4 个附录条目；每个前置项都有明确的水平标注；至少为前 4 周排出具体章节。

## 6. 本讲小结

- 这个仓库是**开源教材**（Sphinx 书站），主线是"理解 Blackwell 硬件 → 用 TIRx 编程 → 写出 SOTA 内核"；硬件是主角，TIRx（在 IR 层面写内核的 Python DSL）是载体。
- 全书四部分递进：Part I 建立硬件直觉（9 章），Part II 教 TIRx 编程模型（scope/layout/dispatch + TileLayout），Part III 用 GEMM 九步练到对齐 cuBLAS，Part IV 用 Flash Attention 4 综合检验；附录四件套按需查阅。
- 贯穿全书的**两条主线内核**是 GEMM 与 Flash Attention，分别承担"教学阶梯"与"毕业考题"的职能。
- 贯穿全书的**三个核心优化思想**是数据布局、异步数据搬运、异步协调，最终在 GEMM 终态中协同把 70 ms 压到 0.094 ms。
- 运行书中内核需要 Blackwell GPU（`sm_100a`），但阅读、推演与构建书站不需要；TIRx 靠 Python 源码检视解析内核，示例必须写在文件或 notebook 单元格里。
- 本讲产出：一张 Part–章节–摘要对照表 + 一份个人学习计划表，它们是后续所有讲义的导航。

## 7. 下一步学习建议

- **下一讲（u1-l2）**《仓库结构与本地构建》：学习如何用 Sphinx 在本地把这本书构建出来，并搞清 `zh/` 中文镜像、`_extra/demo` 交互演示与 `img/scripts` 图表脚本的组织方式——这会让你获得一个可搜索、可标注的本地版全书。
- **再下一讲（u1-l3）**《运行环境与内核运行方式》：安装并验证 `apache-tvm==0.26.0` 与 `cuda-bindings`，确认自己的硬件是否满足 `sm_100a`；有 Blackwell GPU 的读者将在此讲跑通第一个参考内核。
- **提前浏览**：如果急于看内核长什么样，可以直接翻 [chapter_intro_tirx/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md) 的第一个代码块——看不懂没关系，Part II 会逐行讲解，本手册单元九（u9）也会带读。
