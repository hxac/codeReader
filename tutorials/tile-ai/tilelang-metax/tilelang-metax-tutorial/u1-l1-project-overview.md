# TileLang 与 tilelang-metax 项目概览

## 1. 本讲目标

本讲是整本学习手册的第一讲。读完本讲，你应该能够：

- 说清楚 **TileLang** 是什么、解决了什么问题，以及它与 [TVM](https://tvm.apache.org/) 的关系。
- 看懂官方给出的「三档编程接口（beginner / developer / expert）」和「六步下译流程」。
- 读懂 README 里那段 GEMM（矩阵乘）示例代码大致在做什么。
- 知道 **tilelang-metax** 这个分支相比上游 tilelang 多了什么——也就是面向 MetaX GPU 的 **MACA 后端**。
- 手动在仓库里定位出 MACA 相关的代码分布在哪些目录。

本讲不要求你会写 kernel，也不要求你懂编译原理，我们会从零开始解释。

## 2. 前置知识

在开始之前，先建立几个最朴素的直觉。后面的章节都会用到它们。

- **什么是 GPU kernel**：一段运行在 GPU 上的、用来做大量数值计算的小程序。比如「把两个大矩阵相乘」就是一个典型的 kernel。
- **什么是 DSL（Domain-Specific Language，领域特定语言）**：专门为某一类问题设计的「小语言」。TileLang 就是一种专门为「高性能 GPU/CPU kernel」设计的 DSL。它长得像 Python，但会被编译成 GPU 能跑的代码。
- **什么是 GEMM**：General Matrix Multiply，通用矩阵乘法，\(C = A \times B\)。它是深度学习里最核心、也最常被用来「压榨硬件性能」的算子。本讲和后续很多讲都会拿它当例子。
- **什么是后端（backend / target）**：同一段 TileLang 代码，最终要变成能在不同硬件上运行的代码——NVIDIA GPU 上叫 CUDA、AMD GPU 上叫 HIP、MetaX GPU 上叫 MACA。这里的「CUDA / HIP / MACA」就是不同的目标后端。
- **TVM**：Apache 的一个开源深度学习编译器框架。TileLang 不是从零造轮子，而是**构建在 TVM 之上**，复用了 TVM 的 IR（中间表示）、pass（编译优化遍）和后端基础设施。

> 一句话理解：TileLang 是一个「长得像 Python、底层靠 TVM」的 DSL，让你用较少的代码写出接近手写极限性能的 GPU kernel。

## 3. 本讲源码地图

本讲涉及的关键文件如下。这一讲偏「读与理解」，所以我们主要看文档和入口文件。

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/README.md) | 项目门面：定位、支持的设备、安装方式、GEMM 快速上手示例。 |
| [VERSION](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/VERSION) | 当前版本号。 |
| [docs/get_started/overview.md](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/overview.md) | 官方对「三档编程接口 + 六步编译流程」的文字说明。 |
| [tilelang/__init__.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/__init__.py) | Python 包入口，导出 `jit`、`compile`、各后端模块等。 |
| [tilelang/maca/](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca) | metax 分支新增的 MACA Python 模块目录。 |
| [src/maca/](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca) | metax 分支新增的 MACA C++ 编译/runtime 目录。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**项目定位**、**编译流程总览**、**metax 分支差异**。

### 4.1 项目定位

#### 4.1.1 概念说明

TileLang（仓库里常写作 **tile-lang**）是一种简洁的领域特定语言，目标是让开发者用很少的代码就能写出高性能的 GPU/CPU kernel，例如 GEMM、反量化 GEMM、FlashAttention、LinearAttention 等。

它的核心卖点在 README 第一段里就点明了：

[README.md:12](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/README.md#L12)——这段说明 TileLang 是「构建在 TVM 之上的 DSL」，让你既不牺牲底层优化，又能保持高生产率。

换句话说，TileLang 想同时拿到两样东西：

1. **生产率**：写起来像 Python，不用手写 CUDA 汇编或 CUTLASS 模板。
2. **性能**：底层有编译器做分块（tiling）、内存层级放置、软件流水线（software pipeline）、张量核（tensor core）指令发射等优化，性能接近手写。

要理解它「为谁设计」，官方给出了**三档编程接口**，这是理解整个项目心智模型的关键：

| 级别 | 面向人群 | 特点 |
| --- | --- | --- |
| Beginner（硬件无关） | 不想关心硬件细节的人 | 只写计算逻辑（注：尚未完全实现）。 |
| Developer（硬件感知 + Tile Library） | 懂 GPU 内存层级的开发者 | 用现成的「Tile 库」原语，不必管线程细节。 |
| Expert（硬件感知 + 线程原语） | 极致性能追求者 | 直接操控线程原语，精细控制布局与同步。 |

这三档不是互斥的——**同一个 kernel 里可以混用**，你在哪一层抽象写得顺手，就在哪一层写。

#### 4.1.2 核心流程

从「使用者视角」看，用 TileLang 写一个 kernel 的流程非常短：

```text
1. 用 @tilelang.jit 装饰一个普通 Python 函数（函数体里写计算）。
2. 调用 matmul.compile(...) 或直接 matmul(a, b) 触发编译。
3. 拿到 kernel 对象，可以：
   - kernel(a, b)              直接运行
   - kernel.get_kernel_source()看生成的底层源码（如 CUDA）
   - kernel.get_profiler()     测延迟
```

这里的关键概念是：**TileLang 是一个编译器**。你写的 Python 函数其实是一份「程序规格说明」，真正运行的是编译器从它生成的 GPU 代码。

#### 4.1.3 源码精读

先看官方对三档接口的文字定义，位置在 overview 文档：

[docs/get_started/overview.md:17-30](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/overview.md#L17-L30)——这三段分别解释 Beginner / Developer / Expert 三档接口的定位，与本讲 4.1.1 的表格一一对应。

再看 README 里那段经典 GEMM 示例的「入口部分」，理解 TileLang 代码长什么样。下面只截取关键几行（完整示例在 README 中）：

[README.md:116-119](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/README.md#L116-L119)——说明 target（目标后端）可以写成 `"auto"`、裸字符串（如 `"cuda"`）或配置字典（如 `{"kind": "cuda", "arch": "sm_90"}`）。这是 TileLang 区分不同硬件的方式，metax 分支正是靠它把 target 指向 `"maca"`。

[README.md:137-140](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/README.md#L137-L140)——`with T.Kernel(...)` 定义 kernel 的启动上下文（线程块网格、线程数），随后 `T.alloc_shared` / `T.alloc_fragment` 在 GPU 的共享内存 / 寄存器上分配 tile 缓冲。

[README.md:148-158](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/README.md#L148-L158)——`T.Pipelined(..., num_stages=3)` 做软件流水线，循环体内 `T.copy` 把数据搬进共享内存，`T.gemm` 在 tile 上做矩阵乘。这些就是「Tile Library」原语，对应 Developer 档接口。

> 现在不必完全看懂每一行，只需记住：这段 Python 代码描述了「分块搬运 + 分块计算 + 流水线」的结构，剩下的脏活累活（指令选择、布局推断、生成 CUDA/HIP/MACA 源码）由编译器完成。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是让你建立起「TileLang 是编译器、写的是规格、跑的是生成代码」的直觉。

1. **实践目标**：理解 README 的 GEMM 示例里「哪些是你写的、哪些是编译器帮你做的」。
2. **操作步骤**：
   - 打开 [README.md 的 GEMM 示例](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/README.md#L114-L171)。
   - 用不同颜色/记号把代码分成三类：
     - **A 类：计算逻辑**（如 `T.gemm`、`T.copy`、relu 的 `T.max(..., 0)`）。
     - **B 类：硬件资源声明**（如 `T.Kernel`、`alloc_shared`、`alloc_fragment`）。
     - **C 类：编译后端与运行**（如 `@tilelang.jit`、`.compile(...)`、`get_kernel_source()`）。
3. **需要观察的现象**：你会发现真正「算东西」的代码很少，大部分是「声明在哪算、怎么搬数据」。
4. **预期结果**：得出「TileLang 代码 ≈ 计算逻辑 + 内存层级描述，剩下的交给编译器」这一结论。

> 注：本机若无 GPU，不必实际运行，只做标注阅读即可；实际运行在后续讲义（U1-L4）专门讲。

#### 4.1.5 小练习与答案

**练习 1**：TileLang 是「解释执行」还是「编译执行」？依据是什么？

> **参考答案**：编译执行。因为代码里有 `.compile(...)`、`get_kernel_source()` 这些步骤，说明 Python 函数先被编译成底层源码（如 CUDA），再作为可执行 kernel 运行。

**练习 2**：三档编程接口（Beginner / Developer / Expert）的区别，用一句话各自概括。

> **参考答案**：Beginner 完全不关心硬件（只写计算）；Developer 用现成 Tile 原语（关心内存层级但不碰线程）；Expert 直接操控线程原语做精细调优。

---

### 4.2 编译流程总览

#### 4.2.1 概念说明

理解了 TileLang 是编译器之后，下一步要搞清楚：**它从你写的 Python 代码，到 GPU 上真正运行的二进制，中间经过了哪些阶段**。官方 overview 文档把这叫「Compilation Flow」，一共六步。

这个流程之所以重要，是因为后续几乎每一讲都在讲这六步里的某一环：lowering（下译）、layout inference（布局推断）、codegen（代码生成）等。本讲只要求你建立整体印象。

#### 4.2.2 核心流程

六步下译流程如下（文字版流程图）：

```text
① Tile Program                          你写的高层计算描述
        │
        ▼
② Tile Program + Tile Library           developer 档：展开成库调用
        │
        ▼
③ Tile Program + Thread Primitives      expert 档：插入线程原语
        │
        ▼
④ IRModule                              下译成中间表示（捕获硬件细节）
        │
        ▼
⑤ Source Code Generation                生成 C/CUDA/HIP/MACA/... 源码
        │
        ▼
⑥ Hardware-Specific Executable/Runtime  编译成可在设备上运行的可执行/运行时
```

要点：

- 步骤 ①②③ 是「输入端的三种写法」，可以混用；它们最终都汇聚到 ④ IRModule。
- ④→⑤ 是把 IR 翻译成某种硬件的源代码（后端相关）。
- ⑤→⑥ 是把源码再编译成真正的二进制，交给 runtime 运行。

如果用数学化的视角概括，可以认为 TileLang 编译器实现的是一个映射：

\[
\text{TileLang Program} \;\xrightarrow{\;\text{lowering + codegen}\;}\; \text{Executable on Target Hardware}
\]

而这个映射的关键，就是中间的 IRModule 与若干编译 pass。

#### 4.2.3 源码精读

官方对这六步的文字说明在：

[docs/get_started/overview.md:33-50](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/overview.md#L33-L50)——逐条列出 Tile Program → IRModule → 源码生成 → 硬件可执行这六步。其中第 ⑤ 步特别提到「tuned for the desired backends or GPU architectures」，这正是 metax 分支加入 MACA 后端的着力点。

至于「内存层级」这个贯穿全流程的概念，overview 文档用 `T.alloc_shared` / `T.alloc_fragment` / `T.copy` 做了解释：

[docs/get_started/overview.md:80-84](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/overview.md#L80-L84)——说明 `alloc_shared` 对应共享内存、`alloc_fragment` 对应寄存器（fragment），数据搬运用 `T.copy`，初始化用 `T.clear` / `T.fill`。这些在 ⑤ 代码生成阶段会被翻译成具体硬件指令。

再看 Python 侧的「下译入口」与后端注册位置：

[tilelang/__init__.py:207](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/__init__.py#L207)——这里导出了 `lower`（下译主入口）以及 `register_cuda_postproc` / `register_hip_postproc` / `register_c_postproc` 三个「后处理回调注册函数」。它们对应流程 ④→⑤ 之间、针对不同后端做的收尾处理（maca 也有自己的一套，见 4.3）。

#### 4.2.4 代码实践

1. **实践目标**：把抽象的六步流程，对应到具体仓库目录上。
2. **操作步骤**：
   - 对照上面的 ① → ⑥ 流程图，在仓库里为每一步找一个「大致对应的代码位置」：
     - ① Tile Program → `examples/` 下的示例（如 `examples/quickstart.py`）。
     - ④ IRModule / lowering → `tilelang/engine/lower.py`（后续 U4-L1 详讲）。
     - ⑤ Source Code Generation → `src/cuda/`、`src/rocm/`、`src/maca/` 下的 codegen。
     - ⑥ Runtime → `src/maca/runtime/`、TVM 的 runtime。
3. **需要观察的现象**：你会发现「代码生成」这一步是**按后端分目录**的，这正是多后端架构的体现。
4. **预期结果**：能够说出「五步源码生成」在仓库里大致由哪些目录负责。

> 本步骤只需阅读目录名与文件名，不需要打开每个文件细看。

#### 4.2.5 小练习与答案

**练习 1**：六步流程里，哪一步把「与具体硬件相关」的细节引入进来？

> **参考答案**：④ IRModule 阶段开始捕获硬件细节，并在 ⑤ Source Code Generation 阶段生成针对特定后端（CUDA/HIP/MACA…）的源码。

**练习 2**：为什么说步骤 ①②③ 可以混用？

> **参考答案**：因为它们只是「输入端的三种抽象层级」，最终都会被下译成同一个 IRModule，所以同一个 kernel 里可以一部分用 Tile 库原语（developer 档），另一部分用线程原语（expert 档）。

---

### 4.3 metax 分支差异

#### 4.3.1 概念说明

**tilelang-metax 是 tilelang 的一个分支**，它的核心使命是：让 TileLang 能在 **MetaX（摩卡·象芯）GPU** 上跑起来。

MetaX GPU 使用的软件栈叫 **MACA**（你可以把它类比成 NVIDIA 的 CUDA）。所以本分支相对上游 tilelang，新增的主要就是一整套 **MACA 后端**：包括 MACA target 注册、MACA codegen、MACA runtime、以及 MACA 的张量核（mfma）指令发射。

一个有力的旁证：README 的「Tested Devices」里，**MetaX 被列在第一位**：

[README.md:39-40](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/README.md#L39-L40)——明确写出支持 MetaX C500，并列出 NVIDIA、AMD 的多款 GPU。这告诉我们 metax 分支是把 MetaX 当作一等公民来支持的。

而安装文档里也专门有一份 MACA 指南：

[docs/get_started/Installation_maca.md:91-108](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/Installation_maca.md#L91-L108)——展示用 `USE_MACA=ON cmake -B build` 来启用 MACA 后端构建。这个开关是 metax 分支的标志性配置。

#### 4.3.2 核心流程

MACA 后端在两个层面接入 TileLang：

```text
Python 层（tilelang/maca/）            C++ 层（src/maca/）
─────────────────────────             ─────────────────────────
target.py      : maca target 检测       runtime/maca_target_kind.cc : 注册 maca target（warp_size=64 等）
codegen.py     : 注册 maca codegen     codegen/codegen_maca.cc      : MACA 源码生成器
intrinsics/    : mfma 指令发射          op/gemm.cc                   : MACA gemm 指令选择
pipeline.py    : MACA 编译流水线        transform/lower_maca_intrin  : MACA intrinsic 下译
execution_backend.py : 运行后端        runtime/maca_device_api.cc   : MACA device API
```

两者通过 TVM 的 FFI（foreign function interface）连起来。从「六步流程」的角度看，MACA 后端主要插手的是第 ⑤ 步（Source Code Generation）和第 ⑥ 步（Runtime）。

#### 4.3.3 源码精读

先看 Python 包入口里，MACA 是如何与其他后端并列导出的：

[tilelang/__init__.py:211-215](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/__init__.py#L211-L215)——这里把 `cpu`、`cuda`、`rocm`、`metal`、`maca` 五个后端模块并列 `import`。`maca` 就是 metax 分支新增的那一行，它与 cuda/rocm 平级。

再看 MACA 的 Python 子模块都包含什么：

[tilelang/maca/__init__.py:1-6](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/__init__.py#L1-L6)——导出 `intrinsics`、`op`、`pipeline`、`target`、`execution_backend`、`transform` 六个子模块，覆盖了「target 检测 + 指令发射 + 编译流水线 + 运行后端」的全链路。

C++ 侧，MACA 后端集中在 `src/maca/` 目录，结构如下（来自仓库实际目录）：

| 路径 | 职责 |
| --- | --- |
| `src/maca/runtime/maca_target_kind.cc` | 注册 `maca` target kind（含 `warp_size=64` 等属性）。 |
| `src/maca/runtime/maca_device_api.cc` | MACA device API（`mc*` 系列接口）。 |
| `src/maca/codegen/codegen_maca.cc` | MACA 源码生成器 `CodeGenTileLangMACA`。 |
| `src/maca/codegen/intrin_rule_maca.cc` | MACA 内置函数（fastmath / warp shuffle）降低规则。 |
| `src/maca/op/gemm.cc` | MACA gemm 的指令选择（如 16x16x16fp16 等 mfma）。 |
| `src/maca/transform/lower_maca_intrin.cc` | MACA intrinsic 下译 pass。 |

> 一个关键差异点：MACA target 的 **warp_size 是 64**，而 NVIDIA CUDA 是 32。这会影响线程划分和指令布局，是后续 U7（MACA 后端深入）的核心知识点。本讲只需记住「MACA 与 CUDA 不完全一样」。

#### 4.3.4 代码实践

1. **实践目标**：在仓库里亲手「数」出 MACA 后端涉及的所有目录。
2. **操作步骤**：
   - 用 `ls` 或文件浏览器查看这两个目录：
     - Python 侧：`tilelang/maca/`（以及它的子目录 `intrinsics/`、`op/`、`transform/`）。
     - C++ 侧：`src/maca/`（以及子目录 `runtime/`、`codegen/`、`op/`、`transform/`）。
   - 对比 `tilelang/cuda/`、`tilelang/rocm/` 是否有同样结构的对应模块。
3. **需要观察的现象**：MACA 模块的「目录骨架」和 cuda/rocm 高度相似——这说明 metax 分支是「照着已有后端的样子，新加了一个后端」。
4. **预期结果**：列出至少 6 个 MACA 相关文件/目录，并说出它们分别对应六步流程的哪一步。

> 待本地验证：若你在本机已 clone 仓库，可执行 `ls tilelang/maca src/maca` 对照；若无仓库也无需运行，按本讲给出的清单理解即可。

#### 4.3.5 小练习与答案

**练习 1**：metax 分支相比上游 tilelang，最核心的新增是什么？

> **参考答案**：一整套面向 MetaX GPU 的 MACA 后端，包括 target 注册、codegen、runtime、mfma 指令发射等，使 TileLang 能在 MetaX GPU 上编译和运行。

**练习 2**：从 `tilelang/__init__.py` 看，MACA 与哪些后端是平级的？

> **参考答案**：与 `cpu`、`cuda`、`rocm`、`metal` 平级，说明 MACA 是 TileLang 多后端架构中的一个一等后端。

**练习 3**：MACA 后端的 warp_size 与 CUDA 有何不同？（提示性记忆，后续详讲）

> **参考答案**：MACA 的 warp_size 是 64，CUDA 是 32；这会影响线程划分和指令布局，是 MACA 后端的关键差异点之一。

---

## 5. 综合实践

现在把三个模块串起来，完成本讲的主线任务。

> **任务**：阅读 README 中的 GEMM 示例，画出从「Tile Program」到「硬件可执行文件」的下译流程图，并指出 metax 分支新增的 MACA 相关内容出现在哪些目录。

**步骤**：

1. **画流程图**：把 4.2.2 的六步流程图抄下来或重画一遍，并在每一步旁边标注：
   - 这一步「谁负责」（用户 / 编译器 / runtime）。
   - 这一步在仓库里的「大致代码位置」（参考 4.2.4 的对应关系）。
2. **标注 GEMM 示例归属**：在流程图最左端（① Tile Program）旁边，写上 README GEMM 示例里哪些行属于「计算逻辑 / 内存层级声明 / 启动上下文」（参考 4.1.4 的分类练习）。
3. **圈出 MACA**：在流程图的第 ⑤ 步（Source Code Generation）和第 ⑥ 步（Runtime）处，标出 metax 分支新增的 MACA 目录：
   - ⑤ 处标：`src/maca/codegen/`、`tilelang/maca/codegen.py`、`tilelang/maca/intrinsics/`。
   - ⑥ 处标：`src/maca/runtime/`、`tilelang/maca/execution_backend.py`。
4. **写一句总结**：用一句话回答「tilelang-metax 相比 tilelang 多了什么，多出来的东西插在编译流程的哪一步」。

**预期产出**：一张带标注的下译流程图（手绘或电子版皆可），加一句总结。完成后，你就建立起了贯穿全本手册的「全局地图」。

## 6. 本讲小结

- TileLang 是构建在 TVM 之上的 DSL，目标是「像 Python 一样写、接近手写性能地跑」高性能 GPU/CPU kernel。
- 它提供 Beginner / Developer / Expert **三档编程接口**，且可在同一 kernel 内混用。
- TileLang 是**编译器**：你写的是 Tile Program，经过六步下译流程，最终生成硬件相关的源码与可执行文件。
- 六步流程的关键节点是 IRModule（中间表示）与 Source Code Generation（按后端分目录的代码生成）。
- **tilelang-metax** 分支的核心增量是**面向 MetaX GPU 的 MACA 后端**，使 MetaX 成为与 cuda/rocm/metal 平级的一等后端。
- MACA 后端代码分布在 `tilelang/maca/`（Python）与 `src/maca/`（C++）两处，主要插手编译流程的代码生成与 runtime 两步。

## 7. 下一步学习建议

本讲建立了全局地图，接下来建议：

- 想动手跑代码：进入 **U1-L4（第一个 kernel：跑通 GEMM）**，亲手把 GEMM 示例跑起来。
- 想搭环境（含 MACA）：进入 **U1-L2（环境搭建与编译安装）**，学习 `USE_MACA=ON` 构建。
- 想摸清代码组织：进入 **U1-L3（仓库目录结构与代码组织）**，系统了解 `tilelang/` 与 `src/` 的布局。
- 后续进阶会依次进入「编译流水线（U4）」「代码生成与后端（U5）」以及本分支的核心「MACA 后端（U7）」。

建议下一步先读 **U1-L3**，把目录结构理清，再回头跑 U1-L4 的示例，学习曲线会更顺。
