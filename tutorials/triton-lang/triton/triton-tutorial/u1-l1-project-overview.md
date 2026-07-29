# Triton 是什么：定位、技术栈与硬件支持

## 1. 本讲目标

本讲是整本《Triton 源码学习手册》的第一篇，面向**完全没接触过 Triton** 的读者。读完本讲，你应当能够：

1. 用一句话说清 Triton 是什么、它解决了什么问题，以及它为什么把自己定位在「CUDA」和「通用 DSL」之间。
2. 认出 Triton 的**三层技术栈**（Python 前端 / C++/MLIR 编译器 / 硬件后端），并理解一条 kernel 从 Python 代码走到 GPU 二进制的大致链路（AST → TTIR → TTGIR → LLIR → PTX/amdgcn → cubin/hsaco）。
3. 知道 Triton 支持哪些操作系统、哪些硬件、哪些 Python 版本，从而判断自己的机器能不能用、该装哪个版本。

本讲**不要求你读懂任何 C++ 或 MLIR 代码**，所有引用都停留在「看懂这段文字/这行 Python 在说什么」的层面。后续讲义才会深入到编译器内部。

---

## 2. 前置知识

在开始之前，只要理解下面几个朴素概念即可：

- **GPU（显卡）**：一种擅长大规模并行计算的硬件，深度学习训练/推理大量依赖它。
- **kernel（核函数）**：运行在 GPU 上的一段程序。你写一个函数，告诉 GPU「每个并行单元要做哪些计算」，然后一次性启动成千上万个并行单元去执行它。
- **编译器（compiler）**：把你写的高级代码翻译成硬件能执行的机器码的程序。比如把 C 翻译成 CPU 指令，或把 Triton 翻译成 GPU 指令。
- **深度学习算子（primitive / op）**：如矩阵乘法（matmul）、softmax、layer norm 等。这些算子的实现质量直接决定了模型跑得多快。
- **CUDA**：NVIDIA 提供的 GPU 编程方案，通常指用 C++ 写 GPU kernel。它是「最灵活但最难写」的极端。

> 如果你只熟悉 Python、从未写过 GPU 代码，没关系——Triton 的设计初衷之一就是让你尽量用接近 Python/NumPy 的方式来写 GPU kernel。本讲会反复对照「如果你用纯 CUDA 会怎样」，从而凸显 Triton 的价值。

---

## 3. 本讲源码地图

本讲只涉及 3 个文件，外加对仓库目录的一次性浏览，全部都很短：

| 文件 | 作用 | 本讲怎么用 |
|------|------|-----------|
| [README.md](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/README.md) | 项目首页：定位、安装、构建、调试、兼容性 | 用来理解项目「自我介绍」和硬件/平台支持 |
| [python/triton/\_\_init\_\_.py](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/__init__.py) | Triton 对外暴露 API 的总入口（`import triton` 拿到的东西） | 用来梳理「Triton 公开 API 清单」 |
| [docs/getting-started/installation.rst](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/docs/getting-started/installation.rst) | 安装文档 | 用来确认安装方式与 Python 版本范围 |

此外会顺带确认几个工程事实（版本号、Python 支持范围、LLVM 依赖），它们藏在：

- [setup.py](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/setup.py)（打包脚本，定义 Python 版本范围）
- [cmake/llvm-info.json](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/cmake/llvm-info.json)（锁定的 LLVM 版本）

---

## 4. 核心概念与源码讲解

本讲拆成 3 个最小模块：**项目定位**、**技术栈概览**、**硬件与平台支持**。

---

### 4.1 项目定位

#### 4.1.1 概念说明

我们先回答最根本的问题：**Triton 到底是什么？**

一句话：**Triton 是一门用来写「深度学习算子」的小型编程语言，外加一个把这门语言编译到 GPU 上的编译器。**

要理解它为什么存在，先看 GPU 算子开发的两种传统极端：

| 方式 | 代表 | 优点 | 痛点 |
|------|------|------|------|
| **手写 CUDA C++** | cuBLAS、cuDNN 的内部实现 | 性能极致、控制力最强 | 开发慢、门槛高、只认 NVIDIA、要手写 tiling/共享内存/warp 同步等海量底层细节 |
| **通用 DSL / 模板算子库** | TVM、各类「自动生成 matmul」的库 | 生产率高、几行配置就能用 | 灵活性差，只能套预设模板，很难表达像 FlashAttention 这种非标准计算 |

Triton 想要的就是这两者中间的「甜区」：

> **像写 CUDA 一样灵活、像写 Python/NumPy 一样省事。**

具体地，在 Triton 里你写 kernel 时操作的不是单个线程（thread），而是**一个数据块（block / tile）**——你用类似 NumPy 的语法描述「对这一块数据做加减乘除、矩阵乘、规约」，而**线程分派、共享内存管理、数据在 warp 之间的搬运**这些麻烦事，统统交给 Triton 编译器自动处理。这让你既能写出任意形状的计算（灵活），又不必操心几百行 CUDA 样板代码（高效）。

这种「块级别编程 + 编译器自动调度」的思路来自一篇 2019 年的学术论文，Triton 的工程实现就是它的延续。

#### 4.1.2 核心流程

从使用者视角看，一条 Triton kernel 的生命周期是这样的（本讲只给直觉，细节留到后面几讲）：

```
你写的 Python kernel 函数（带 @triton.jit）
        │  第一次调用时触发 JIT（just-in-time）编译
        ▼
   编译器逐阶段翻译 ──► 最终得到 GPU 可执行二进制（cubin / hsaco）
        │  把二进制缓存起来，下次同样参数直接复用
        ▼
   在 GPU 上启动（launch），成千上万个 program 并行执行
```

这里有两个关键词先记住即可：

- **JIT（即时编译）**：kernel 不是预先编译好的，而是**第一次被调用、且参数类型已知时**才编译。这样编译器能看到具体形状/类型，做针对性优化。
- **program（程序实例）**：Triton 用 `program_id` / `grid` 的概念来表达并行度。粗略地说，一个 program 负责处理数据的一个分块，你启动一个 grid，里面就有很多个 program 同时跑。（第一篇先留个印象，u1-l4 会动手写。）

#### 4.1.3 源码精读

项目的「自我介绍」就在 README 第一段，这是理解 Triton 定位最权威的一句话：

> [README.md:25](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/README.md#L25)（项目定义）——「Triton 是一门语言和编译器，用来编写高效的、自定义的深度学习算子；目标是提供一种开源环境，以**比 CUDA 更高的生产率**、又**比其它已有 DSL 更高的灵活性**来写快速的代码。」

紧接着它指明了理论出处：

[README.md:27](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/README.md#L27) 说明本项目的根基来自 MAPL 2019 论文《Triton: An Intermediate Language and Compiler for Tiled Neural Network Computations》。如果你对学术背景感兴趣可以读，本手册后续讲义会直接讲实现，不依赖你读论文。

至于「我现在用的是哪个版本的 Triton」，看包入口的第一行：

[python/triton/\_\_init\_\_.py:2](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/__init__.py#L2) 定义了 `__version__ = '3.8.0'`——也就是说本手册对应的源码版本是 Triton 3.8.0。你随时可以 `import triton; print(triton.__version__)` 来核对自己环境里的版本。

#### 4.1.4 代码实践

**实践目标**：亲手验证上面的「项目定义」与版本信息，而不是只听我转述。

**操作步骤**：

1. 打开仓库里的 [README.md](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/README.md)，找到第 25 行那句项目定位（注意它同时提到了 CUDA 和 DSL，这正是 Triton 的「夹缝定位」）。
2. 打开 [python/triton/\_\_init\_\_.py](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/__init__.py) 第 2 行，确认版本号。
3. （可选）如果你的环境里已 `pip install triton`，在 Python 里执行：

   ```python
   import triton
   print(triton.__version__)
   ```

**需要观察的现象**：打印出的版本应与源码 `__version__` 一致（或你安装的发行版号）。

**预期结果**：能复述「Triton = 写深度学习算子的语言 + 编译器，定位在 CUDA 和通用 DSL 之间」，并报出版本号。若没有 GPU 环境，无法 `import triton` 也没关系——本步骤只读源码即可完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么说 Triton「比 CUDA 生产率高」？请举一个 CUDA 中让人头疼、而 Triton 试图自动处理的环节。
> **参考答案**：在 CUDA 里，程序员通常要手动管理共享内存（shared memory）的分配、加载与同步，手动把数据切成 tile 并安排 warp 间的分工。Triton 让你在「块」级别编程，这些底层调度由编译器自动完成，因此代码更短、更接近数学表达，生产率更高。

**练习 2**：Triton 说的 DSL（领域专用语言）通常指哪类工具？它相比这类工具的「更高灵活性」体现在哪？
> **参考答案**：DSL 在这里指 TVM、各种自动算子生成库等「用模板/调度规则生成算子」的方案。它们的灵活性受限于预设模板，难以表达非标准计算（如 FlashAttention 的在线 softmax）。Triton 是一门可写任意逻辑的小语言，因此能表达这类复杂、非模板化的计算。

---

### 4.2 技术栈概览

#### 4.2.1 概念说明

要理解 Triton 的代码结构，先抓住它的**三层技术栈**。这三层分别由不同的语言写成、解决不同的问题，对应仓库里三组不同的目录：

| 层 | 主要语言 | 仓库位置 | 解决什么问题 |
|----|---------|---------|-------------|
| ① Python 前端 | Python | `python/triton/` | 用户写 kernel 的入口、`@triton.jit`/`autotune` 等装饰器、JIT 调度、参数特化、解释器（无 GPU 调试） |
| ② C++/MLIR 编译器 | C++ + TableGen | `lib/`、`include/` | 定义中间表示方言（TTIR/TTGIR）、各种优化 pass、把 IR lowering 到 LLVM IR |
| ③ 硬件后端 | Python + C + C++ | `third_party/nvidia`、`third_party/amd` 等 | 针对具体硬件的编译阶段、驱动、二进制生成与加载 |

> **术语：MLIR**。MLIR（Multi-Level Intermediate Representation）是 LLVM 项目下的一个「构造编译器的基础设施」。它允许你定义自己的「方言（dialect）」——也就是一组自定义的中间表示操作。Triton 就是基于 MLIR 定义了两个核心方言：
> - **TTIR（Triton IR）**：接近你写的 Python 逻辑的「逻辑层」IR，还不关心具体硬件。
> - **TTGIR（TritonGPU IR）**：引入了「数据如何分布到 GPU 线程上」的「物理层」IR。
>
> 你现在只需记住这两个名字，u7 会专门精读它们的 C++ 定义。

把这三层串起来，一条 kernel 的**编译产物链路**（compilation pipeline）大致是：

```
Python kernel 源码
   │  ① 前端：解析 Python AST
   ▼
TTIR   （逻辑 IR，TTIR 方言）
   │  ② 编译器：分配 GPU 布局、跑各种优化 pass
   ▼
TTGIR  （GPU 物理 IR，TTGIR 方言）
   │  ② 编译器：lowering 到 LLVM IR
   ▼
LLIR   （LLVM IR）
   │  ③ 后端：转成目标硬件的汇编
   ▼
PTX (NVIDIA)  或  amdgcn (AMD)      ← 这是「接近汇编」的文本
   │  ③ 后端：汇编成机器码
   ▼
cubin (NVIDIA) 或 hsaco (AMD)       ← 这是最终扔给 GPU 执行的二进制
```

记住这条链路：**AST → TTIR → TTGIR → LLIR → PTX/amdgcn → cubin/hsaco**。它是整本手册的「主线剧情」，后面每一篇讲义都在讲这条链路上的某一段。

> **术语小贴士**：
> - **PTX**（Parallel Thread eXecution）：NVIDIA 定义的一种「虚拟 GPU 指令」文本格式，介于高级和机器码之间。
> - **cubin**：NVIDIA GPU 的最终二进制（CUDA binary）。
> - **hsaco**：AMD GPU 使用的二进制格式（HSA code object）。

#### 4.2.2 核心流程

上面那条链路里有几个关键「关卡」，对应后续讲义：

1. **前端生成（AST → TTIR）**：Triton 遍历你 kernel 函数的 Python 抽象语法树（AST），把每条 Python 语句翻译成 TTIR 操作。（详见 u6）
2. **逻辑转物理（TTIR → TTGIR）**：编译器为每个张量分配「布局」（数据怎么分布到线程/ warp），并做一堆优化。（详见 u7、u10）
3. **lowering（TTGIR → LLIR）**：把带布局的 IR 翻译成 LLVM IR，期间分配共享内存、把 `dot` 映射成硬件矩阵乘指令。（详见 u10-l3）
4. **后端收尾（LLIR → PTX/amdgcn → cubin/hsaco）**：由具体硬件后端完成，NVIDIA 用 `ptxas` 汇编成 cubin，AMD 走 hsaco。（详见 u8）

注意，这条链路的「分段」是由各硬件后端**自己注册**的——也就是说 NVIDIA 和 AMD 的具体阶段名称可能不同，但总体顺序一致。这正是「前端/编译器/后端」三层分工的价值：前端和编译器内核通用，后端各管各的硬件。

#### 4.2.3 源码精读

**（a）三层目录长什么样。** 用只读命令浏览仓库顶层，就能看到这三层的物理边界：

- `python/` —— ① Python 前端（`python/triton/` 下有 `runtime`、`compiler`、`language`、`backends` 等子包）。
- `lib/`、`include/` —— ② C++/MLIR 编译器。其中 `lib/Dialect/` 下能看到 TTIR、TTGIR 等方言的目录：

  ```
  lib/Dialect/
  ├── Triton/          ← TTIR 方言
  ├── TritonGPU/       ← TTGIR 方言
  ├── Gluon/           ← 实验性的 Gluon 方言（见 u12）
  ├── TritonNvidiaGPU/ ← NVIDIA 专用方言
  └── TritonInstrument/← 插桩方言
  ```

  而 `lib/Conversion/` 下则是「把一种 IR 翻译成另一种」的转换 pass（如 `TritonToTritonGPU/`、`TritonGPUToLLVM/`）。

- `third_party/` —— ③ 硬件后端，按厂商分目录：

  ```
  third_party/
  ├── nvidia/   ← NVIDIA 后端（cubin）
  ├── amd/      ← AMD 后端（hsaco）
  ├── proton/   ← Triton 专用 profiler（见 u11）
  └── f2reduce/ ← 辅助库
  ```

**（b）前端对外暴露了什么（API 总入口）。** 当你 `import triton` 时，Python 实际加载的就是这个文件，它决定了「Triton 的公开 API 长什么样」：

[python/triton/\_\_init\_\_.py:8-28](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/__init__.py#L8-L28) 把运行时、编译、语言、工具等模块的内容 re-export 到顶层，关键的几行是：

```python
from .runtime import (autotune, Config, heuristics, JITFunction, ...)
from .runtime.jit import constexpr_function, jit          # ← @triton.jit 的来源
from .runtime._async_compile import AsyncCompileMode, FutureKernel
from .compiler import compile, CompilationError           # ← triton.compile 的来源
from .errors import TritonError
from .runtime._allocation import set_allocator
from . import language   # ← 这就是 kernel 里写的 tl（triton.language）
from . import testing
from . import tools
```

也就是说：你用的 `triton.jit`、`triton.autotune`、`triton.compile`、`triton.language`（习惯简写成 `tl`），全部是这里「搬」到顶层的。其中 `triton.language` 才是 kernel 内部用的算子库（`tl.load`、`tl.store`、`tl.dot` 等），它由 [python/triton/language/\_\_init\_\_.py:6-119](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/language/__init__.py#L6-L119) 进一步从 `standard.py`（高层算子如 `softmax`、`sum`、`sort`）和 `core.py`（底层算子如 `dot`、`load`、`store`、`program_id`、各 `dtype`）汇总而来。

**（c）编译器层依赖一个特定版本的 LLVM。** ② 层的 C++ 代码不能在任意 LLVM 上编译，因为 LLVM 的 API 经常变。构建时会按一个固定哈希去下载预编译好的 LLVM：

[cmake/llvm-info.json:3](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/cmake/llvm-info.json#L3) 里写着 `"llvm_hash": "850a2b1b975c061ae0fc982ba68064d305485cb2"`——这正是本版 Triton 绑定的 LLVM 版本。如果以后你想「用自己编译的 LLVM」，就得 checkout 到这个 hash。这一点 README「Building with a custom LLVM」一节也有强调（[README.md:83-89](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/README.md#L83-L89)）。

#### 4.2.4 代码实践

**实践目标**：把仓库的三类代码「肉眼分类」，建立「看到某个目录就知道它属于哪一层」的直觉。

**操作步骤**：

1. 列出仓库顶层目录（`ls` 或在代码托管网站上浏览）。
2. 把每个顶层目录归到下表三类之一（或标注「其它/文档/测试」）。

   | 目录 | 归类（① 前端 / ② 编译器 / ③ 后端 / 其它） |
   |------|------------------------------------------|
   | `python/` | ① 前端（同时含编译编排 `compiler/`、后端接口 `backends/`） |
   | `lib/`、`include/` | ② C++/MLIR 编译器 |
   | `third_party/nvidia/`、`third_party/amd/` | ③ 硬件后端 |
   | `bin/`、`test/`、`docs/`、`examples/` | 其它（工具/测试/文档/示例） |

3. 打开 `python/triton/__init__.py`，对照 4.2.3 节，确认 `jit`、`autotune`、`compile`、`language` 确实都在前 N 行被 import。

**需要观察的现象**：你会注意到 `python/triton/` 内部其实也细分了 `compiler/` 和 `backends/`——也就是说「前端目录」里也藏着编译编排和后端发现的 Python 代码。这提示我们：三层是**逻辑职责**的划分，不完全等同于目录的物理分割。

**预期结果**：能指着 `lib/Dialect/TritonGPU` 说「这是编译器层」，指着 `third_party/amd` 说「这是后端层」，指着 `python/triton/runtime/jit.py`（下一讲会用到）说「这是前端层」。若不确定某个目录归属，标注「待确认」即可。

#### 4.2.5 小练习与答案

**练习 1**：TTIR 和 TTGIR 的本质区别是什么？
> **参考答案**：TTIR 是「逻辑层」IR，描述计算本身，不关心数据在 GPU 上的分布；TTGIR 是「物理层」IR，额外编码了「每个张量的数据如何映射到线程 / warp / CTA」的布局（layout）。从 TTIR 到 TTGIR 的转换，就是 Triton 决定并行/数据分布的过程。

**练习 2**：为什么 NVIDIA 和 AMD 后端最终产物的扩展名不同？请把两者配对。
> **参考答案**：因为两家 GPU 的二进制格式不同。NVIDIA 用 `cubin`（CUDA binary），AMD 用 `hsaco`（HSA code object）；中间还分别经过 `PTX`（NVIDIA）和 `amdgcn`（AMD）的文本汇编阶段。

**练习 3**：`import triton` 之后，`triton.jit` 和 `triton.language` 分别从哪个文件「搬」来的？
> **参考答案**：`triton.jit` 来自 `python/triton/runtime/jit.py`（在 `__init__.py` 第 20 行导入）；`triton.language` 是 `python/triton/language/` 子包（第 26 行导入），其内容由 `language/__init__.py` 从 `standard.py` 和 `core.py` 汇总。

---

### 4.3 硬件与平台支持

#### 4.3.1 概念说明

知道 Triton「是什么」「怎么分层」之后，下一个现实问题是：**我的机器能用吗？** 这取决于三件事——操作系统、GPU 型号、Python 版本。

先解释两个硬件术语：

- **Compute Capability（计算能力版本，简称 CC）**：NVIDIA 给每代 GPU 的一个版本号，如 8.0、9.0。它反映了架构代际：
  - 8.x ≈ Ampere（安培）架构，如 A100、RTX 30 系。
  - 9.x ≈ Hopper（霍普）架构，如 H100。
  - 10.x ≈ Blackwell（布莱克威尔）架构。
  CC 越高，支持的新指令越多。Triton 对 NVIDIA 的最低要求是 **CC 8.0+**，也就是说 Maxwell/Pascal/Volta/Turing 这些老架构不在支持范围内。
- **ROCm**：AMD 的 GPU 软件栈（类比 NVIDIA 的 CUDA + 驱动）。Triton 对 AMD 的要求是 **ROCm 6.2+**。

#### 4.3.2 核心流程

Triton 在运行时会根据本机检测到的 GPU 选择一个**后端**（NVIDIA 或 AMD），后端决定了上面 4.2 那条编译链路最后几段（LLIR → 目标汇编 → 二进制）的具体实现。本讲只需要知道：

```
启动 Triton ──► 探测本机 GPU ──► 选择对应后端（nvidia / amd）
                                      │
                                      ▼
               用该后端注册的编译阶段，把 IR 变成 cubin 或 hsaco
```

「后端是如何被发现和选择的」是 u8 的主题，这里不展开。CPU 后端目前仍在开发中（官方标注 "Under development"）。

#### 4.3.3 源码精读

**（a）官方兼容性声明。** README 末尾有专门的「Compatibility」一节：

[README.md:292-300](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/README.md#L292-L300) 明确列出：

- **支持的操作系统**：只有 **Linux**。
- **支持的硬件**：
  - NVIDIA GPU（Compute Capability 8.0+）
  - AMD GPU（ROCm 6.2+）
  - CPU：开发中（Under development）

这意味着 macOS / Windows 不是「官方支持的开发/运行平台」（即便有 dev container 帮助，那也是 Linux 容器）。

**（b）支持的 Python 版本。** 这在两处可以互相印证：

[docs/getting-started/installation.rst:17](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/docs/getting-started/installation.rst#L17) 写「Binary wheels are available for CPython 3.10-3.14」；构建脚本里把这件事定死：

[setup.py:578-581](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/setup.py#L578-L581) 用 `MIN_PYTHON = (3, 10)`、`MAX_PYTHON = (3, 14)` 拼出 `python_requires`。所以本手册涉及的版本支持范围是 **Python 3.10、3.11、3.12、3.13、3.14**。低于 3.10 装不上。

**（c）多平台的预编译依赖。** 既然只支持 Linux，那 `cmake/llvm-info.json` 里为什么还有 `macos-*`、`windows-*` 的条目？看一眼就明白：

[cmake/llvm-info.json](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/cmake/llvm-info.json) 为多种 OS/架构都准备了预编译 LLVM 的 `sha256sum`（用于构建时校验下载）。这更多是「构建工具链层面的覆盖」，并不改变「Triton 运行时只官方支持 Linux」这一事实。不要被这里的 `windows-x64` 条目误导——它只表示下载清单里有，不等于 Triton 在 Windows 上被官方支持。

> 顺带一提，[setup.py:595](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/setup.py#L595) 还写着包的官方描述："A language and compiler for custom Deep Learning operations"，并标注 [license 为 MIT](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/setup.py#L597)——它是开源的，你可以自由阅读和修改源码。

#### 4.3.4 代码实践

**实践目标**：判断「我手上这台机器」是否满足 Triton 的运行前提。

**操作步骤**：

1. 确认操作系统：本机是否为 Linux？（macOS/Windows 不在官方支持范围。）
2. 确认 Python 版本是否落在 3.10–3.14：

   ```bash
   python --version
   ```

3. （若有 GPU）确认 GPU 型号与驱动：
   - NVIDIA：执行 `nvidia-smi`，确认显卡存在；查阅其 Compute Capability 是否 ≥ 8.0。
   - AMD：确认 ROCm 版本是否 ≥ 6.2。
4. （无 GPU）也没关系——后续讲义会介绍 `TRITON_INTERPRET=1` 解释器模式（[README.md:188-189](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/README.md#L188-L189)），可以纯 Python 模拟 kernel，无需 GPU。

**需要观察的现象**：把 OS、Python 版本、（可能的）GPU 型号与 CC 四项记下来。

**预期结果**：能给出一句结论，例如「Linux + Python 3.12 + NVIDIA A100(CC 8.0)，满足要求」或「无 GPU，将使用解释器模式学习」。若你的 CC 低于 8.0，记录为「本机硬件不满足官方要求，需用解释器模式或更换设备」。如无法本地确认 GPU 的 CC，明确写「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：一台装着 NVIDIA RTX 2080 Ti（Turing，CC 7.5）的 Linux 机器，能官方支持地跑 Triton 吗？
> **参考答案**：不能。Triton 对 NVIDIA 的最低要求是 Compute Capability 8.0+，而 RTX 2080 Ti 是 CC 7.5（Turing 架构），低于门槛。不过在没有 GPU 的情况下仍可用 `TRITON_INTERPRET=1` 解释器模式学习 kernel 语义。

**练习 2**：`cmake/llvm-info.json` 里出现了 `windows-x64`，这是否意味着 Triton 官方支持 Windows？
> **参考答案**：不是。该条目只是预编译 LLVM 下载校验清单的一部分，用于构建工具链层面的覆盖。Triton 的运行时/开发官方仅支持 Linux（见 README 的 Compatibility 一节），不要把下载清单与运行支持范围混为一谈。

**练习 3**：本手册对应的 Triton 版本支持 Python 3.9 吗？
> **参考答案**：不支持。[setup.py:578-581](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/setup.py#L578-L581) 把 `MIN_PYTHON` 设为 `(3, 10)`，所以最低需要 Python 3.10，3.9 无法安装。

---

## 5. 综合实践

现在把三个模块串起来，完成本讲的主任务：**整理一份《Triton 公开 API 清单》**。这也是本讲规格里指定的代码实践任务。

**实践目标**：通过阅读 README 与 `__init__.py`，亲手梳理出 `import triton` 后「最常用」的公开 API，并为每个 API 写一句话，说明它在「编译 / 运行」流程中扮演的角色。这能帮你把 4.1（定位）、4.2（链路）、4.3（平台）三部分的知识一次性用上。

**操作步骤**：

1. 打开 [python/triton/\_\_init\_\_.py](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/__init__.py)，重点看第 8–28 行的 import 和第 33–60 行的 `__all__`（`__all__` 列出了「官方认为是公开 API」的名字）。
2. 对照下表，把每个 API 归到编译链路的相应位置（可参考 4.2 的链路图）。建议你**自己先填一遍「角色」一栏，再和我给出的参考答案对照**。

   | API | 所在模块 | 一句话角色（编译/运行流程中的位置） |
   |-----|---------|-------------------------------------|
   | `triton.jit` | `runtime.jit` | 把普通 Python 函数包装成「可 JIT 编译、可启动」的 kernel 对象（链路最前端的入口） |
   | `triton.language`（`tl`） | `language` | kernel **内部**使用的算子库（`tl.load`/`tl.store`/`tl.dot` 等），这些算子会被翻译成 TTIR 操作 |
   | `triton.autotune` | `runtime.autotuner` | 在编译前自动尝试多组配置（如 `num_warps`、`BLOCK` 大小），选出最快的，再编译 |
   | `triton.Config` | `runtime.autotuner` | 描述 autotune 的一组候选配置（配置空间的一项） |
   | `triton.heuristics` | `runtime.autotuner` | 依据运行时参数用规则**推导**出某个 constexpr 配置（区别于 autotune 的「试出来」） |
   | `triton.compile` | `compiler` | 主动触发一次编译（AST/IR → 二进制），是「编译编排」的对外入口 |
   | `triton.set_allocator` | `runtime._allocation` | 把 Triton 的临时显存分配委托给宿主框架（如 PyTorch），属于运行期资源管理 |
   | `triton.tools` | `tools` | 命令行/AOT 工具集（如 `compile.py`、`disasm.py`），用于离线编译与反汇编 |

3. 把这份清单和 4.2.2 节的链路对照：你会发现 `jit` / `language` / `autotune` / `heuristics` 都集中在**链路最前端**（决定「编译什么、用什么参数编译」），而 `compile` 则是真正**驱动整条链路**的引擎。

**需要观察的现象**：清单中**没有**任何一个「执行 kernel」的独立顶层函数——启动 kernel 是通过 `kernel[grid](...)` 这样的语法糖完成的（这是 u3 的内容）。本步只需注意到这一点即可。

**预期结果**：产出一张类似上表的清单，且每个 API 都能对应到「它在 AST → cubin 这条链路上的哪个环节」。若你对某个 API 的角色拿不准，先写「待确认」，后续讲义（u3 讲 jit、u4 讲 autotune、u5 讲 compile）会逐一补全。**本实践为「源码阅读型实践」，无需 GPU、无需运行，读完源码即可完成。**

---

## 6. 本讲小结

- **Triton 是什么**：一门写深度学习算子的小语言 + 把它编译到 GPU 的编译器，定位在「CUDA（极致但难写）」和「通用 DSL（省事但不灵活）」之间，靠「块级编程 + 编译器自动调度」兼得灵活与高效。
- **三层技术栈**：① Python 前端（`python/`）→ ② C++/MLIR 编译器（`lib/`、`include/`）→ ③ 硬件后端（`third_party/nvidia`、`third_party/amd`）。
- **编译主线**：AST → TTIR → TTGIR → LLIR → PTX/amdgcn → cubin/hsaco；这是贯穿全手册的「主线剧情」。
- **公开 API**：`import triton` 后，`jit`、`language`（即 `tl`）、`autotune`/`Config`、`heuristics`、`compile` 等都集中在编译链路的**最前端**。
- **平台/硬件**：仅官方支持 **Linux**；NVIDIA 需 CC 8.0+，AMD 需 ROCm 6.2+，CPU 仍在开发中。
- **Python 版本**：3.10–3.14（本手册对应 Triton 3.8.0），低于 3.10 装不上；无 GPU 时可用 `TRITON_INTERPRET=1` 解释器模式学习。

---

## 7. 下一步学习建议

本讲只建立了「全局心智地图」，还没有真正动手。建议接下来按顺序：

1. **u1-l2《从源码构建、安装与运行测试》**：把项目装起来、能跑测试，是后续所有动手实践的前提。如果你已经有可用的 `pip install triton` 环境，可以快速浏览，重点看 `setup.py`、`Makefile` 与 `cmake/llvm-info.json` 的关系。
2. **u1-l3《仓库与目录结构地图》**：用一张图把本讲提到的三层目录固化下来，方便之后定位源码。
3. **u1-l4《编写并运行第一个 Triton kernel》**：亲手写一个 vector-add，把本讲的「链路图」变成可运行的程序，并第一次看到 `.asm` 里各级 IR 的真面目。

> 建议继续阅读的真实源码：先把 `python/triton/__init__.py` 通读一遍（只有 80 多行），再扫一眼 `python/triton/language/__init__.py` 的 import 列表——这两份 import 清单，就是「Triton 给用户的所有承诺」。
