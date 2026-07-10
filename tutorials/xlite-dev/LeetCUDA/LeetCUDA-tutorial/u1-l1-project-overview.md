# LeetCUDA 是什么：项目定位与核心特性

## 1. 本讲目标

本讲是整本《LeetCUDA 学习手册》的第一讲，目标是让你在进入任何一行 CUDA 源码之前，先建立起一张「项目全景图」。读完本讲，你应当能够：

- 用一句话向别人说清楚 **LeetCUDA 是什么、为谁而做、解决了什么问题**。
- 说出项目包含的 **核心技术栈**（Tensor Cores / CUDA Cores、TF32 / FP16 / BF16 / FP8）和 **算子大类**（element-wise、reduce、softmax、norm、GEMV/GEMM、flash-attn 等）。
- 理解仓库的「学习骨架」`notes-v2.cu` 是如何用 **8 个 Phase** 把知识从易到难串起来的。
- 知道整本学习手册 **从入门到专家的整体路径**，从而知道每一讲大概在学什么。

本讲不要求你写过任何 CUDA 代码，所有概念都会先用通俗语言解释。

## 2. 前置知识

本讲几乎零门槛，但下面几个名词最好先有个「模糊印象」，不影响阅读：

- **GPU（显卡）**：和 CPU 不同，GPU 擅长同时执行成千上万个轻量计算任务，因此特别适合做深度学习里的大规模矩阵运算。
- **CUDA**：NVIDIA 提供的一套用 C/C++ 写 GPU 程序的工具链。写一个 `.cu` 文件，用 `nvcc` 编译，就能让显卡运行你写的「kernel（核函数）」。
- **kernel（核函数）**：在 GPU 上被成千上万个线程并发执行的函数，是 CUDA 编程的基本单位。
- **PyTorch**：深度学习最常用的 Python 框架。LeetCUDA 用 CUDA 写高性能算子，再用 PyTorch 的绑定机制暴露给 Python 调用，方便和已有的 Python 生态对接、做正确性验证和基准测试。
- **精度（dtype）**：FP32（单精度浮点）、FP16（半精度）、BF16（脑浮点）、FP8（8 位浮点）、TF32（Tensor Float 32）。数字越小、显存和带宽越省、但表示精度也越低，需要小心数值稳定性。

后面遇到不熟的术语，我们都会随讲随解释。

## 3. 本讲源码地图

本讲只涉及两个「文档型」文件，它们是认识整个项目的两把钥匙：

| 文件 | 作用 | 你要从中得到什么 |
|:---|:---|:---|
| [README.md](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/README.md) | 项目主 README，包含定位、特性、Quick Start、200+ kernel 列表、博客索引 | 项目定位、核心特性、算子分类、整体学习路线 |
| [kernels/interview/README.md](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/README.md) | `notes-v2.cu`（面试背题笔记）的说明文档 | 学习骨架的 8 个 Phase、编译运行命令、测试输出格式 |

后续讲义会深入 `kernels/` 下一个个具体的算子目录（如 `kernels/relu/`、`kernels/hgemm/`），但本讲先把这两个总览文件读透。

> 本讲只「读」文档、理解全局，不会修改任何源码。

## 4. 核心概念与源码讲解

### 4.1 项目定位与核心特性（读 README）

#### 4.1.1 概念说明

打开 LeetCUDA 的主页，你会看到这样一个标题：

> 📚 LeetCUDA: Modern CUDA Learn Notes with PyTorch for Beginners

注意最后两个词——**for Beginners（面向初学者）**。这是理解整个项目最关键的一句定位。

市面上很多 CUDA 学习资料要么太简单（只有 `a+b`），要么太难（一上来就是 CUTLASS 全家桶）。LeetCUDA 想解决的问题是：**给一个「会用 PyTorch 但没怎么写过 CUDA」的人，提供一条从 naive（朴素）实现一路优化到逼近官方库（cuBLAS）性能的、可对照源码的学习路径。** 它的名字 LeetCUDA 也暗含「像刷 LeetCode 题一样刷 CUDA kernel」的意思——每个算子都有从易到难的多个版本，可以一道道「刷」过去。

README 里有一句精炼的特性总结，几乎浓缩了项目全部卖点：

> 📚 **LeetCUDA**: It includes **Tensor/CUDA Cores, TF32/F16/BF16/F8**, 200+ CUDA Kernels with PyTorch, 100+ LLM/CUDA blogs, HGEMM which can achieve `98%~100%` TFLOPS of **cuBLAS**, and flash-attn using Tensor Cores with pure MMA PTX.

这段话拆开看就是 LeetCUDA 的五大支柱：

1. **双计算单元覆盖**：既教 **CUDA Cores**（通用标量/向量运算单元，跑 FP32/FP16 等），也教 **Tensor Cores**（专用矩阵乘加速单元，跑 TF32/FP16/FP8 等）。
2. **多精度覆盖**：TF32 / FP16 / BF16 / FP8，覆盖了当前大模型训练推理常用的全部精度。
3. **200+ 个 CUDA kernel**：从最简单的 `relu`、`elementwise_add`，到复杂的 GEMM、FlashAttention。
4. **HGEMM（半精度矩阵乘）逼近 cuBLAS**：在 L20、RTX 4090、RTX 3080 上，仓库自己手写的 HGEMM 能达到 cuBLAS 默认 Tensor Cores 算法 **98%~100%** 的性能——这是一个相当高的水平。
5. **FlashAttention（纯 MMA PTX 实现）**：用最底层的 PTX 汇编级指令手写了 FlashAttention-2，在小型 attention 上甚至能比官方 FA2/SDPA 更快。

#### 4.1.2 核心流程：项目如何把「学习」组织起来

理解项目不能只看它「有什么」，还要看它「怎么组织学习」。LeetCUDA 的组织方式可以归纳成一个三层结构：

```text
读 README（知道有什么）
        │
        ▼
按算子目录刷 kernel（每个目录一个独立主题）
   kernels/relu/   kernels/softmax/   kernels/hgemm/  ...
   每个目录都遵循统一约定：
      README.md  → 该算子的原理与优化记录
      *.cu       → CUDA 实现（含 pybind 绑定）
      *.py       → PyTorch 包装 + 正确性验证 + 基准测试
        │
        ▼
用 notes-v2.cu 串成面试骨架（8 个 Phase，从易到难）
```

也就是说，项目同时提供了「**按主题深入**（各算子目录）」和「**按面试/递进通读**（notes-v2.cu）」两条学习主线。本讲先建立这两条主线的概念，后续讲义会分别展开。

#### 4.1.3 源码精读

下面这几处是 README 中最值得逐字阅读的关键点，都附上永久链接方便你随时跳转对照。

**① 项目定位句**——一句话说清 LeetCUDA 是什么、含什么：

[README.md:16](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/README.md#L16)（README 开头那段特性总结，包含 Tensor/CUDA Cores、多精度、200+ kernel、HGEMM、flash-attn 五大卖点）。

**② Quick Start 编译运行**——这是后续任何讲义动手实践的起点：

[README.md:41-47](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/README.md#L41-L47)（`git submodule update` + 三条 `nvcc` 命令，分别对应 Ada / Ada+CuTe / Hopper 三种编译目标，并给出运行二进制的提示）。先不用看懂参数，下一讲（u1-l2）会专门讲。

**③ 算子学习的标准 workflow**——README 用一句话说明了每个主题的统一学习流程：

> The **workflow** for each topic will be as follows: custom **CUDA kernel** implementation -> PyTorch **Python bindings** -> Run tests.

[README.md:243-247](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/README.md#L243-L247)（说明 200+ kernel 从 Easy 到 Hard++ 的难度分级，以及每个主题统一的「kernel → 绑定 → 测试」三步 workflow）。

**④ 算子大类划分**——README 把 200+ kernel 分成了清晰的难度梯度：

[README.md:257](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/README.md#L257)（一句话概括 Easy/Medium 主题包含哪些算子，Hard 及以上主题包含哪些算子）。

为了便于记忆，把 README 提到的大类归纳成下表（后续各单元讲义会逐个深入）：

| 大类 | 代表算子 | 学习重点 |
|:---|:---|:---|
| Element-wise | relu、elementwise_add、sigmoid、gelu | 线程层次、合并访问、向量化 |
| Reduce | block_all_reduce、dot_product | warp/block 归约原语 |
| Norm | layer_norm、rms_norm | 归约原语的复用、精度 |
| Softmax | naive / safe / online softmax | 数值稳定性、online 算法 |
| LLM 小算子 | rope、embedding | 位置编码、查表访存模式 |
| GEMV | sgemv、hgemv | memory-bound、warp-per-row |
| GEMM | sgemm、hgemm（WMMA/MMA/CuTe/WGMMA） | 分块、流水线、Tensor Core、swizzle |
| Attention | flash-attn（MMA/CuTe） | online softmax + 分块 + 寄存器复用 |

**⑤ 性能卖点**——HGEMM 逼近 cuBLAS 的特性清单：

[README.md:118-126](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/README.md#L118-L126)（HGEMM 特性表，列出了 Tensor Cores、Pack LDST、Multi Stages、Block/Warp/SMEM Swizzle 等一整套优化技术，这些正是专家层讲义会逐项拆解的内容）。

#### 4.1.4 代码实践

> 这是本讲的第一个实践，属于「**阅读 + 总结型**」实践，无需 GPU 也能完成。

**实践目标**：用一段中文说清 LeetCUDA 解决什么问题、包含哪几大类算子。

**操作步骤**：

1. 打开 [README.md](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/README.md)，重点阅读第 16 行（定位句）、第 108–162 行（HGEMM / FlashAttention 基准）、第 243–257 行（算子分级与 workflow）。
2. 浏览 `200+ CUDA Kernels` 的几张表格，感受从 Easy（⭐️）到 Hard++（⭐️⭐️⭐️⭐️⭐️）的难度跨度。
3. 用中文写一段 150–300 字的总结，回答两个问题：
   - LeetCUDA **为谁解决什么问题**？
   - 它包含 **哪几大类算子**（至少列出 5 类）？

**需要观察的现象**：你会发现 README 几乎每个表格都遵循「**Elem DType / Acc DType / Level**」三列，这其实是 CUDA 算子描述的标准范式——**输入元素精度 / 累加精度 / 难度**。记住这个范式，后续看任何算子都能快速定位。

**预期结果**：你能写出类似下面这样的总结（示例答案，请用自己的话改写）：

> 示例总结：LeetCUDA 面向「会用 PyTorch 但想学手写 CUDA」的初学者，提供 200+ 个从 naive 到高度优化的 kernel，并配套 PyTorch 绑定与正确性/性能验证脚本。它的特色是同时覆盖 CUDA Cores 与 Tensor Cores、支持 TF32/FP16/BF16/FP8 多精度，且手写的 HGEMM 与 FlashAttention 能逼近甚至超过官方 cuBLAS / FA2。算子大类包括 element-wise、reduce、softmax、layer/rms-norm、RoPE/embedding、GEMV、GEMM、FlashAttention 等。

**⚠️ 说明**：如果你本地暂时没有 NVIDIA GPU，无法实际编译运行，这个总结型实践完全可以离线完成，不需要「假装运行过命令」。

#### 4.1.5 小练习与答案

**练习 1**：README 里反复出现「HGEMM 能达到 cuBLAS 98%~100% TFLOPS」这句话。请问这里的「HGEMM」中的 **H** 代表什么？为什么它的性能对标对象是 **cuBLAS**？

> **参考答案**：H 代表 **Half**，即 FP16 半精度。HGEMM = Half-precision GEMM（半精度矩阵乘）。cuBLAS 是 NVIDIA 官方的高性能 BLAS 库，通常被当作「几乎无法超越的性能天花板」，因此把手写 HGEMM 拿来和 cuBLAS 比，是衡量优化水平的最高标准。

**练习 2**：README 算子表格里，每个 kernel 都标注了「Elem DType」和「Acc DType」两列。请用一个具体例子说明这两者为什么可能不同（比如 `rms_norm_f16_f32`）。

> **参考答案**：以 `rms_norm_f16_f32` 为例，输入/输出元素（Elem）用 FP16 以省显存和带宽，但归约过程中累加方差/均值时（Acc）用 FP32，因为大量 FP16 相加会丢失精度（数值不稳定）。所以「**输入输出低精度、累加高精度**」是高频组合，Acc DType 这列就是用来标注这个差异的。

---

### 4.2 notes-v2.cu 学习骨架与 8 个 Phase（读 interview 说明）

#### 4.2.1 概念说明

`kernels/interview/notes-v2.cu` 是整个仓库的「**学习骨架**」——它把面试高频的 CUDA kernel 汇总进一个文件，按难度分成多个 Phase，每个 Phase 都附带详细的中文注释（说明 WHY 和 HOW），并配一个测试用例做正确性验证。

它的姊妹文档 [kernels/interview/README.md](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/README.md) 一开头就把它的定位讲清楚了：

> notes-v2.cu — CUDA Kernel 面试背题笔记，共 8 个 Phase、26 个 kernel、25 个 test case。

[kernels/interview/README.md:1-3](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/README.md#L1-L3)（说明 `notes-v2.cu` 是面试高频 CUDA kernel 的完整实现，共 8 个 Phase、26 个 kernel、25 个 test case）。

理解这个文件，你就理解了 LeetCUDA 想让你「按什么顺序学 CUDA」。它最大的价值不在于「写得多花哨」，而在于把零散的优化技术排成了一条 **递进的学习链**：

```text
naive 实现  →  向量化(vectorize)  →  共享内存分块(tile)
   →  Tensor Core(MMA/WGMMA)  →  多级流水线(multistage)
   →  swizzle 消除 bank conflict  →  warp specialization
```

每一步都只在前一步基础上加一个优化点，非常便于「一道题一道题刷」。

#### 4.2.2 核心流程：8 个 Phase 分别讲什么

`notes-v2.cu` 文件头有一段对全部 Phase 的总览。需要说明一点：README 与 interview 文档把它描述为「**8 个 Phase**」（指 8 个含 kernel 的阶段 Phase 1–Phase 8），而文件里还有 Phase 0，它**纯由注释组成**（面试框架速查，不含 kernel），可以理解为「第 0 步：先记住这些基础知识」。

8 个含 kernel 的 Phase 与主题对应如下（取自文件头注释）：

| Phase | 主题 | 关键算子 | 后续讲义 |
|:---|:---|:---|:---|
| **Phase 1** | 基础原语 | Warp Reduce / Block Reduce / Dot Product | U4 |
| **Phase 2** | Elementwise | ReLU / Elementwise Add / Histogram（含 float4 向量化 + atomic） | U2、U7 |
| **Phase 3** | Softmax 三级递进 | naive → safe → online，外加 RMS/Layer Norm | U5、U6 |
| **Phase 4** | RoPE | 旋转位置编码（Llama 风格 theta=10000） | U6 |
| **Phase 5** | Mat Transpose | 基础版 + BCF merge_write 最佳版（Bank Conflict 专题） | U7 |
| **Phase 6** | GEMV | SGEMV K32 / K128 / K16（warp-per-row） | U8 |
| **Phase 7** ★ | GEMM | SGEMM → HGEMM → MMA m16n8k16(TN) → WGMMA m64n128k16 | U9–U13 |
| **Phase 8** | FlashAttention | split_q（FA-2，含 online softmax + P@V 寄存器复用） | U14 |

可以看到，Phase 7（GEMM）是重头戏，它本身又细分成 SGEMM、HGEMM MMA、CuTe、WGMMA 多个子阶段，对应了专家层的多篇讲义。

而 Phase 0（虽然不在「8 个 Phase」计数内）极其重要——它是一份「**面试框架速查表**」，用纯注释总结了 GPU 架构、内存层次、Roofline 模型、常见优化清单。它等于「学后面所有 Phase 前必须先背下来的地基知识」。

#### 4.2.3 源码精读

下面这几处是 `notes-v2.cu` 最值得精读的「地图型」代码点。

**① 全部 Phase 的总览注释**：

[kernels/interview/notes-v2.cu:11-20](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L11-L20)（文件头列出 Phase 0–Phase 8 各自的主题，是整份笔记的目录）。

注意第 8 行点明了贯穿全篇的优化递进思路：

> 优化技术的递进式讲解（naive → tiling → vectorize → tensor core → ws）

[kernels/interview/notes-v2.cu:6-9](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L6-L9)（说明这份笔记整理自 LeetCUDA，每类 kernel 附带 WHY+HOW 注释，并约定 BLAS 的 N/T 布局语义）。

**② Phase 0：GPU 架构与内存层次速查**——这是「地基」，给出了判断算子瓶颈所需的全部数量级：

[kernels/interview/notes-v2.cu:35-39](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L35-L39)（Memory Hierarchy 带宽数量级：HBM3 ~3.35 TB/s、L2 ~12 TB/s、L1/SMEM ~19 TB/s、Register ~100+ TB/s，数字以 H100 为参考）。

这段注释揭示了一个关键事实：**离计算单元越近的存储越快**（寄存器 > SMEM > L2 > HBM）。后面几乎所有的优化（向量化、分块、流水线、寄存器复用）本质都是「**把数据尽量留在更快的那一层**」。

**③ Phase 0：常见优化手段清单**——把全书要学的优化技术先列了个目录：

[kernels/interview/notes-v2.cu:51-89](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L51-L89)（列出 9 类常见优化：合并访问、Tiling、向量化、Thread Tile、Bank Conflict 规避、流水线/双缓冲、Tensor Core、Warp Specialization、TMA）。

**④ Phase 0：Roofline 模型示例**——教你判断一个算子到底是「算力受限」还是「访存受限」：

[kernels/interview/notes-v2.cu:90-105](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L90-L105)（用算术强度 AI = FLOPs / Bytes 来分类：GEMM 是 compute-bound，GEMV/Softmax 是 memory-bound）。

其核心思想是一个比值——**算术强度（Arithmetic Intensity, AI）**：

\[ \text{AI} = \frac{\text{计算量 FLOPs}}{\text{访存量 Bytes}} \]

- AI 很大（如 GEMM 约 685 FLOPS/Byte）→ 受限于算力 → **compute-bound**。
- AI 很小（如 GEMV 约 0.5 FLOPS/Byte、Softmax 约 0.625）→ 受限于显存带宽 → **memory-bound**。

这一条判断会贯穿整本手册：**不同瓶颈的算子要用完全不同的优化方向**（compute-bound 要榨干 Tensor Core，memory-bound 要减少访存、提高带宽利用率）。

**⑤ 测试入口与输出格式**——所有 Phase 的正确性验证都汇总在 `main` 里：

[kernels/interview/notes-v2.cu:4896-4931](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L4896-L4931)（`main` 函数，打印 `verification harness` 表头，逐个 kernel 输出 `Max Err` 与 `PASS/FAIL`，最后打印 `All tests done`）。

每个 kernel 都有一个正确性阈值（如 ReLU 用 `1e-4`、MatTranspose 用 `1e-6`、FlashAttn 用 `1e-1`），运行后只要 `Max Err` 小于阈值就打印 `PASS`。这套「**与参考实现比对最大误差**」的验证范式，是后续每个 `.py` 脚本的标准写法。

#### 4.2.4 代码实践

> 本实践仍属「**源码阅读型**」，目的是让你亲手把 8 个 Phase 的对应关系理清楚。即使无 GPU 也能完成。

**实践目标**：指出 `notes-v2.cu` 的 8 个 Phase 分别对应哪些主题，并理解 Phase 0 的 Roofline 速查。

**操作步骤**：

1. 打开 [kernels/interview/notes-v2.cu:11-20](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L11-L20)，把 Phase 0–8 的主题抄一遍到自己的笔记里。
2. 打开 [kernels/interview/README.md:20-51](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/README.md#L20-L51)，对照「测试输出」表格，确认每个 Phase 对应哪些 kernel 名字（如 Phase 2 对应 `ReLU` / `ElemwiseAdd` / `Histogram`，Phase 7 对应 `SGEMM` / `HGEMM MMA` / `HGEMM WGMMA` 等）。
3. 读 [notes-v2.cu:90-105](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L90-L105)，把 GEMM、GEMV、Softmax 三者的算术强度（AI）和瓶颈类型填进自己的表格。
4. 用中文写出 Phase 0–8 与主题的对应关系（即本讲的 `practice_task`）。

**需要观察的现象**：注意 interview README 的「测试输出」表里，`Max Err` 一列从 `0.000000e+00`（完全精确，如 ReLU）一直到 `1.6e-4`（FlashAttention，有较大浮点误差）。**精度要求是因算子而异的**——整数/比较类算子可以零误差，而含大量浮点累加的 attention 误差会大得多但仍算 `PASS`。

**预期结果**：你能给出类似 4.2.2 节那张「Phase → 主题」对照表，并说明 GEMM 是 compute-bound、GEMV 与 Softmax 是 memory-bound。

**⚠️ 说明**：如果你想在本地真正跑一遍这张表（可选），需要 NVIDIA GPU + CUDA 工具链。编译命令见 [README.md:41-47](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/README.md#L41-L47)，但**本讲不要求你跑通**，下一讲 u1-l2 会专门讲编译运行。当前没有 GPU 就标注「待本地验证」即可。

#### 4.2.5 小练习与答案

**练习 1**：README 说 notes-v2.cu 有「8 个 Phase」，但文件头注释里却列出了 Phase 0 到 Phase 8。这两者矛盾吗？请解释。

> **参考答案**：不矛盾。文件里确实有 Phase 0，但 Phase 0 是**纯注释的「面试框架速查」**（GPU 架构、内存层次、Roofline、优化清单），不含可执行 kernel。所以「8 个 Phase」指的是 **Phase 1–Phase 8 这 8 个含 kernel 的阶段**，Phase 0 是额外的「第 0 步地基知识」，不参与计数。读源码时要分清「注释型的 Phase 0」和「含 kernel 的 Phase 1–8」。

**练习 2**：用 Phase 0 给出的 Roofline 思路判断：对一个矩阵向量乘（GEMV），下面哪种优化方向最可能有效？为什么？
- (a) 用 Tensor Core 把算力拉满
- (b) 用向量化访存 / 减少不必要的 HBM 读取来提高带宽利用率

> **参考答案**：选 **(b)**。根据 [notes-v2.cu:100-103](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L100-L103)，GEMV 的算术强度 AI ≈ 0.5 FLOPS/Byte，严重 **memory-bound**——瓶颈在显存带宽而非算力。这时把算力拉满（a）没有意义，因为数据根本喂不满计算单元；正确的方向是减少访存量、提升带宽利用率（向量化合并访问、减少重复读）。这正是 Phase 6 SGEMV 采用 warp-per-row 策略的原因（后续 U8 会讲）。

**练习 3**：`verification harness` 输出里，ReLU 的 `Max Err` 是 `0.000000e+00`，而 FlashAttention 是 `1.6e-4` 左右。两者都显示 `PASS`，这说明 LeetCUDA 对「正确」的定义是什么？

> **参考答案**：LeetCUDA 不要求「与参考实现逐比特相同」，而是要求**误差小于一个算子相关的阈值**（见 [notes-v2.cu:4896-4931](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L4896-L4931) 中各 kernel 不同的阈值）。比较/整数类算子可以做到零误差，而含大量浮点累加的算子（尤其低精度 FP16 + attention）必然有舍入误差，只要在容忍范围内就算正确。这也是后续讲义里「`Max Err` vs PyTorch 参考实现」这套验证范式的含义。

## 5. 综合实践

**综合任务**：把本讲两个模块串起来，制作一张「**LeetCUDA 学习地图**」。

要求你产出一份自己的 markdown 笔记（可以放在本地，不放进仓库），包含三部分：

1. **项目一句话定位** + 五大支柱（用你自己的话，不要复制本讲原文）。
2. **算子大类速查表**：从 README 的 200+ kernel 列表里，挑出至少 8 个算子，填入「算子名 / 所属大类 / 难度星级 / 后续讲义」四列。
3. **8 个 Phase 学习链**：画出 Phase 1 → Phase 8 的递进关系（可用文字箭头），并在每个 Phase 旁标注「它解决的核心问题」和「它属于 compute-bound 还是 memory-bound」。

**验证标准**：

- 完成后，你应当能不看任何资料，回答出：「LeetCUDA 是什么？它怎么组织学习？我接下来要按什么顺序学？」
- 如果某一项你填不出来（比如某算子属于哪种瓶颈），说明该回去重读 4.1.3 或 4.2.3 的对应链接——这张地图就是你的「**接下来学习的目录**」。

> 提示：这张地图其实就是本手册 U1–U16 的缩影。U1（本讲）是定位，U2–U3 是基础与绑定，U4–U9 是核心算子与基础优化（对应 Phase 1–6、部分 7），U10–U14 是专家级的 Tensor Core / CuTe / WGMMA / FlashAttention（对应 Phase 7–8），U15–U16 是 Triton 与性能分析扩展。

## 6. 本讲小结

- **LeetCUDA** 是一套面向初学者的「CUDA 学习笔记 + PyTorch 绑定」，目标是带你从 naive 实现一路优化到逼近 cuBLAS / FA2 的性能。
- 项目五大支柱：**Tensor/CUDA Cores 双覆盖**、**TF32/FP16/BF16/FP8 多精度**、**200+ kernel**、**HGEMM 逼近 cuBLAS（98%~100%）**、**纯 MMA PTX 实现的 FlashAttention**。
- 学习有两条主线：**按算子目录深入**（`kernels/<算子>/`，统一 README+.cu+.py 三件套）和 **按面试骨架通读**（`notes-v2.cu` 的 8 个 Phase）。
- `notes-v2.cu` 的 Phase 0 是纯注释的「**面试框架速查**」（GPU 架构、内存层次、优化清单、Roofline），Phase 1–8 是含 kernel 的 8 个递进阶段。
- 全书优化的本质是「**把数据尽量留在更快的存储层**」（寄存器 > SMEM > L2 > HBM），并通过 **算术强度 AI** 判断算子是 compute-bound 还是 memory-bound，从而决定优化方向。
- 整本手册 U1–U16 大致对应 Phase 0–8 的递进：入门打基础 → 核心算子 → 专家级 Tensor Core/CuTe/WGMMA/FlashAttention → Triton 与性能分析扩展。

## 7. 下一步学习建议

本讲只建立了全局认识，还没有真正动手。建议按以下顺序继续：

1. **下一讲 u1-l2《编译运行第一个 CUDA kernel》**：照着 README Quick Start，亲手用 `nvcc` 编译并运行 `notes-v2.cu`，看懂 `verification harness` 的输出，理解 `sm_89` / `sm_90a` 等架构选项和 `-DNOTES_V2_ENABLE_CUTE` 等条件编译宏。
2. **u1-l3《目录结构与 kernel 模块约定》**：以 `kernels/relu/` 为样板，搞懂每个算子目录的 `.cu`（CUDA+pybind）与 `.py`（PyTorch 包装+验证+基准）分工，建立「读任意一个算子目录」的能力。
3. 之后进入 **U2《CUDA 编程模型与内存基础》**，从 `relu`、`elementwise_add` 这种最简单的 kernel 入手，正式开始读 `.cu` 源码、写自己的 kernel。

> 在进入 u1-l2 前，建议你先完成本讲第 5 节的「学习地图」综合实践——有了地图，后面每一讲你都能清楚地知道「我现在站在哪、下一步要去哪」。
