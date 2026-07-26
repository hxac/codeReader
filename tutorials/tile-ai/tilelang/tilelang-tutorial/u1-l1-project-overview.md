# 讲义 u1-l1：项目总览与架构定位

## 1. 本讲目标

本讲是整本《tilelang 学习手册》的第一篇，目标是让你在**不写任何代码**的前提下，建立对 tilelang 的整体认知。读完本讲，你应该能够：

1. 用一句话说清楚 **tilelang 是什么**、它解决什么问题、和手写 CUDA / Triton 的取舍在哪里。
2. 画出 tilelang 从一段 Python 函数到**可执行 GPU/CPU kernel** 的端到端数据流（DSL → TIR → Pass → CodeGen → Adapter）。
3. 说出 tilelang 支持的**硬件后端**（CUDA / HIP / Metal / CPU / WebGPU / CuTeDSL）和它擅长的**典型算子**（GEMM / FlashAttention / MLA 等）。
4. 在仓库里定位三件事的位置：项目定位说明、编译入口、DSL 语言入口。

本讲只讲「全景」，不深入任何单一机制。后面每一讲都会从本讲建立的「地图」出发，钻进某一条具体的链路。

## 2. 前置知识

本讲面向零基础读者，但有几个名词先解释清楚会更好理解：

- **kernel（核函数）**：在 GPU 上并行执行的一段程序。比如「矩阵乘」就可以写成一个 kernel，让 GPU 上成百上千个线程同时算。
- **GEMM（GEneral Matrix Multiply）**：通用矩阵乘 \( C = A \times B \)，是深度学习里最核心、最吃性能的计算之一，几乎所有的性能 benchmark 都会拿它做基准。
- **DSL（Domain-Specific Language，领域专用语言）**：为某一类问题专门设计的小语言。tilelang 就是一个「为写高性能 kernel 而设计」的 Python 风格 DSL——你写的还是 Python，但这些 Python 语句会被「翻译」成 GPU 代码。
- **TVM**：一个开源的深度学习编译器框架（Apache 项目）。tilelang 不是从零造轮子，而是**构建在 TVM 之上**，复用 TVM 的中间表示（TIR）和后端能力。
- **TIR（Tensor IR）**：TVM 的「张量中间表示」，可以理解成一种介于 Python 和 CUDA 之间的、结构化的程序表示。tilelang 会把你写的 Python 翻译成 TIR，再做优化。
- **tile（分块）**：把一个大矩阵切成一小块一小块来处理，是 GPU 性能优化的核心思想。tilelang 的名字就来源于此。

如果你对 GPU 内存层级（global / shared / register）还完全没概念也不用担心，本讲只需要你知道「GPU 有不同层级的存储」即可，细节在后续讲义展开。

## 3. 本讲源码地图

本讲涉及的文件都是「项目门面」级别的文件，适合先通读：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/README.md) | 项目第一印象：定位、安装、Quick Start 示例、支持的硬件与算子。 |
| [docs/get_started/overview.md](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/overview.md) | 官方「简介」，重点讲三档编程接口与编译流程（Compilation Flow）。 |
| [docs/programming_guides/overview.md](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/overview.md) | 编程指南总览，给出建议阅读顺序。 |
| [docs/get_started/targets.md](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/targets.md) | 目标硬件（target）说明：cuda / hip / metal / llvm / webgpu / cutedsl。 |
| [pyproject.toml](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml) | Python 包定义：依赖、构建方式（scikit-build-core）、打包映射。 |
| [tilelang/__init__.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py) | Python 包入口：暴露 `jit / compile / language / Profiler / lower` 等公共 API。 |
| [tilelang/engine/lower.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py) | 编译器入口：`lower()` 把 DSL 函数经 Pass 流水线变成可编译 IR。 |
| [examples/quickstart.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/quickstart.py) | 官方 Quick Start：一个完整的 GEMM kernel，从定义到运行。 |

> 提示：本讲引用的链接都指向当前 HEAD `c6294f07`，点击即可在 GitHub 上看到对应行。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **tilelang 是什么**：定位、它解决的问题、与手写 CUDA / Triton 的取舍。
2. **端到端编译链路**：DSL → TIR → Pass → CodeGen → Adapter。
3. **支持的硬件与典型算子**：target 体系与代表性算子。

---

### 4.1 tilelang 是什么：定位与取舍

#### 4.1.1 概念说明

一句话定位（来自 README 第一段介绍）：

> Tile Language（tile-lang）是一个简洁的领域专用语言，用来**简化高性能 GPU/CPU kernel 的开发**（例如 GEMM、Dequant GEMM、FlashAttention、LinearAttention）。它采用 Pythonic 语法，底层是基于 [TVM](https://tvm.apache.org/) 的编译器基础设施，让开发者既能保持生产力，又不牺牲达到 SOTA（state-of-the-art）性能所需的底层优化。

换句话说，tilelang 想同时拿到两个极端的好处：

- **写起来像 Python**：你不用一行行写 CUDA C++、不用手动管 warp 与寄存器分配的每一个细节。
- **跑起来接近手写算子库**：通过分块（tile）、内存层级显式分配、软件流水线、swizzle 等手段，达到和 CUTLASS / 手写汇编接近的性能。

它和两个常见对手的取舍可以这样概括：

| 方案 | 写法 | 性能可控度 | 典型代表 |
| --- | --- | --- | --- |
| 手写 CUDA / CUTLASS | C++ 模板，极其底层 | 极高，但开发成本极高 | CUTLASS、FlashAttention (C++) |
| Triton | Python DSL，偏「线程级」抽象 | 较高，但部分硬件特性（如 TMA/WGMMA）暴露较晚 | OpenAI Triton |
| **tilelang** | **Python DSL，偏「分块（tile）级」抽象**，底层基于 TVM | **高，且显式暴露 shared/fragment 内存层级与流水线** | 本项目 |

「tile 级抽象」是理解 tilelang 的关键：你不写「每个线程做什么」，而是写「一个线程块（block）里这一小块数据怎么搬、怎么算」，再由编译器把它落到具体硬件指令上。

#### 4.1.2 核心流程

从用户视角，使用 tilelang 的最小流程是：

```text
1. 用 @tilelang.jit 装饰一个「返回 tensor」的 Python 函数（函数体里写 tile 级计算）
2. 调用 kernel.compile(...) 或直接 kernel(a, b) 触发编译
3. tilelang 内部：解析函数体 → 生成 TIR → 跑优化 Pass → 生成设备代码 → 包装成可调用对象
4. 拿到一个可以像普通函数一样调用的 kernel，传入 torch tensor 即可运行
```

最关键的「直觉」是：**你写的 Python 函数其实是一个「描述计算结构的程序」，而不是真的在 CPU 上执行计算**。tilelang 解释这段描述，把它翻译成 GPU 代码。

#### 4.1.3 源码精读

**项目定位（README 首段）**：[README.md:12](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/README.md#L12) 这一句就是 tilelang 的官方定义，强调了「Pythonic 语法 + 基于 TVM + 高性能」三个关键词。

**包描述（pyproject.toml）**：[pyproject.toml:2-3](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml#L2-L3) 写着 `name = "tilelang"` 与 `description = "A tile level programming language to generate high performance code."`（一个 tile 级编程语言，用于生成高性能代码）。再看关键字段 [pyproject.toml:9](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml#L9)：

```toml
keywords = ["BLAS", "CUDA", "HIP", "Code Generation", "TVM"]
```

这五个词点明了它的定位：面向 BLAS 类计算、生成 CUDA/HIP 代码、本质是代码生成、构建在 TVM 上。

**Quick Start 直觉（README 示例）**：[README.md:140-193](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/README.md#L140-L193) 给出的 `matmul_relu` 是认识 tilelang 写法的最好入口。先不用看懂每一行，只看结构：

- `@tilelang.jit` 装饰一个普通 Python 函数；
- 函数里用 `T.Tensor` 标注输入形状与 dtype，用 `T.empty` 分配输出；
- `with T.Kernel(...) as (bx, by):` 声明一个 kernel 的执行上下文（网格/线程）；
- `T.alloc_shared` / `T.alloc_fragment` 在 GPU 不同层级显式开缓冲；
- `T.copy` 搬数据，`T.gemm` 做分块矩阵乘，`T.Pipelined` 套软件流水线；
- 最后 `return C` 返回输出 tensor。

这就是「tile 级抽象」的样子：你描述的是**一块数据（tile）的搬运与计算**，而不是每个线程的指令。

#### 4.1.4 代码实践

**实践目标**：建立「一段 Python = 一个 kernel 描述」的直觉。

**操作步骤**（纯阅读，无需 GPU）：

1. 打开 [README.md:140-193](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/README.md#L140-L193) 的 `matmul_relu` 示例。
2. 对照 [examples/quickstart.py:8-48](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/quickstart.py#L8-L48) 的另一个 GEMM 版本，找出两者都出现的 6 个关键构造：`@tilelang.jit`、`T.const`、`T.Tensor`、`T.Kernel`、`T.alloc_shared/alloc_fragment`、`T.copy/T.gemm`。
3. 在纸上把示例代码分成三段画出来：**①声明形状与缓冲 → ②在 `T.Kernel` 里做搬数+计算+写回 → ③返回输出**。

**需要观察的现象**：你会发现整个函数体里**没有任何 `threadIdx`、没有任何 CUDA 关键字**，但它描述的恰恰是一个 GPU kernel 的结构。

**预期结果**：你得到一张「函数体三段式」的草图。如果暂时没有 GPU，本步只需阅读，**结果待本地验证**（运行步骤在 u1-l4 讲义）。

#### 4.1.5 小练习与答案

**练习 1**：tilelang 是「从零自研的编译器」吗？依据是什么？

> **答案**：不是。README 与 pyproject 都说明它**构建在 TVM 之上**，复用 TVM 的 TIR 与后端；tilelang 自身提供的是「tile 级 DSL + 面向 kernel 的 Pass 与代码生成」。

**练习 2**：用一句话写出 tilelang 相比「手写 CUDA」和「Triton」的取舍。

> **答案**：tilelang 用 Pythonic 的 tile 级抽象换取接近手写算子库的性能——比手写 CUDA 开发效率高得多，同时比 Triton 更显式地暴露 shared/fragment 内存层级与流水线/ swizzle，便于压榨 TMA/WGMMA 这类硬件特性。

---

### 4.2 端到端编译链路：DSL → TIR → Pass → CodeGen → Adapter

#### 4.2.1 概念说明

tilelang 的本质是一个**编译器**。你写的 Python 只是「源码」，真正跑在 GPU 上的是编译器生成出来的设备代码。理解这条链路，是理解整个项目的钥匙。

官方文档 [docs/get_started/overview.md](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/overview.md) 把这条链路描述为 6 个阶段（见该文件「Compilation Flow」一节），我们可以把它浓缩成 tilelang 实际的实现：

```text
Python DSL 函数（@tilelang.jit / T.prim_func）
        │   ① 解析函数体（eager builder / AST）
        ▼
TIR PrimFunc（TVM 中间表示）
        │   ② 语义检查（PreLowerSemanticCheck）
        ▼
Pass 流水线（layout_inference / inject_pipeline / lower_tile_op / …）
        │   ③ 一系列优化与 lowering Pass
        ▼
拆分 host / device IR
        │   ④ 设备代码生成（device codegen）
        ▼
设备源码（CUDA / HIP / Metal / C / WebGPU）
        │   ⑤ 编译成二进制（nvcc / hipcc / …）+ host 封装
        ▼
Kernel Adapter（tvm_ffi / cython / nvrtc / torch / cutedsl）
        │   ⑥ 包装成 Python 可调用对象
        ▼
可调用 kernel（传入 torch tensor 即可运行）
```

每一个箭头后面都对应后续一篇讲义，本讲只需要你记住这条「主干」。

#### 4.2.2 核心流程

把上面链路对应到 tilelang 的实际函数调用上，主干是这样的（伪代码，仅示意顺序）：

```text
@tilelang.jit 装饰的函数
   └─> func() 执行时被记录成 TIR PrimFunc
         └─> tilelang.engine.lower(func, target)          # 编译入口
               ├─ lower_to_host_device_ir(...)
               │    ├─ determine_target(target)           # 解析目标硬件
               │    ├─ PreLowerSemanticCheck(mod)         # 编译前合法性检查
               │    ├─ resolve_pipeline(target)           # 构造 Pass 流水线
               │    └─ pipeline.lower(mod, target)        # 跑 Pass，得到优化后的 IR
               ├─ device_codegen(device_mod, target)      # 生成设备源码（如 CUDA）
               └─ （可选）host_codegen + libgen            # 生成主机封装与库
         └─> Kernel Adapter 包装产物
               └─> 返回 JITKernel（可调用）
```

关键认知：**「编译」是显式的一步**。你可以只编译不运行（拿源码看），也可以编译后立刻运行。`lower()` 就是这条主干上的核心枢纽。

#### 4.2.3 源码精读

**官方编译流程描述**：[docs/get_started/overview.md:32-50](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/overview.md#L32-L50) 列出了从 Tile Program → IRModule → 源码生成 → 硬件可执行文件的完整 6 步，是理解链路最权威的文字说明。同文件 [docs/get_started/overview.md:15-31](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/overview.md#L15-L31) 还说明了 tilelang 提供「Beginner / Developer / Expert」三档接口，且允许在同一 kernel 里混用——这解释了为什么 tilelang 既能写得简单又能压榨到底层。

**编译器入口 `lower()`**：[tilelang/engine/lower.py:297-342](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L297-L342) 是整条链路的 Python 主函数，它的函数签名就揭示了核心参数：

```python
def lower(
    func_or_mod: tirx.PrimFunc | tvm.IRModule,
    target: str | Target = "auto",
    ...
) -> CompiledArtifact:
```

它的函数体（[lower.py:312-319](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L312-L319)）先把任务交给 `lower_to_host_device_ir` 做检查与 Pass，再做 `device_codegen`，正好对应链路里的 ②③④ 步。

**Pass 流水线是怎么挂上去的**：[tilelang/engine/lower.py:259-294](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L259-L294) 的 `lower_to_host_device_ir` 是「内部主干」，重点看这三行（[lower.py:286-292](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L286-L292)）：

```python
# 编译前先做与后端无关的语义检查
PreLowerSemanticCheck(mod)

# 根据 target 构造一条 Pass 流水线，然后跑它
pipeline = resolve_pipeline(target)
mod = pipeline.lower(mod, target)

# 把优化后的 IR 拆成 host 和 device 两份
host_mod = tirx.transform.Filter(_is_host_call)(mod)
device_mod = tirx.transform.Filter(_is_device_call)(mod)
```

这几行就是「②检查 → ③Pass → ④拆分」三步的真实代码落点。本讲你只要知道「有这么几步」即可，Pass 具体做什么在 u6 单元展开。

**包入口如何把这些能力暴露给用户**：[tilelang/__init__.py:192-220](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py#L192-L220) 集中导出了公共 API，其中几行直接对应链路的不同环节：

```python
from .jit import jit, JITKernel, compile, par_compile   # 用户写 kernel / 编译的入口
from .profiler import Profiler                           # 测延迟
from . import language                                   # DSL 语言（T.* 命名空间）
from .engine import lower, ...                           # 编译器入口
from .autotuner import autotune                          # 自动调优
```

注意 [tilelang/__init__.py:159-160](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py#L159-L160) 的 `if not env.is_light_import():` 守卫——这意味着 `import tilelang` 有「轻量导入」模式，部分重依赖（TVM、native 库）只在非 light 模式下加载。这是 tilelang 为了让 CLI/文档工具能快速启动而做的一个工程优化，后续 u1-l3 会再提。

**DSL 语言入口**：[tilelang/language/__init__.py:1-14](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/__init__.py#L1-L14) 是 `import tilelang.language as T` 时拿到的「门面」。它的实现非常短：

```python
"""Default TileLang language facade.
``tilelang.language`` re-exports the CUDA dialect so that ``import
tilelang.language as T`` yields the common surface plus CUDA extensions.
"""
from tilelang.cuda.language import *        # 默认导出 CUDA 方言
__tilelang_dialect__ = "cuda"
```

这说明：`T.*` 默认就是「CUDA 方言 + 公共部分」，其它后端（HIP/Metal/CPU/WebGPU）通过 `tilelang.<backend>.language` 显式引入。这是 tilelang 多后端设计的核心约定，记住它，后面看代码就不会乱。

#### 4.2.4 代码实践

**实践目标**：把「链路」从抽象概念变成可定位的源码坐标。

**操作步骤**（源码阅读型实践，无需 GPU）：

1. 打开编译器入口 [tilelang/engine/lower.py:297](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L297)。
2. 顺着 `lower()` 的函数体往下读，依次在源码里定位这三个调用：`lower_to_host_device_ir`（[L312](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L312)）、`device_codegen`（[L319](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L319)）、可选的 `host_codegen`（[L323](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L323)）。
3. 再跳到 [lower.py:286-292](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L286-L292)，确认「检查 → Pass → 拆分」三步真实存在。
4. 在 [examples/quickstart.py:59-80](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/quickstart.py#L59-L80) 里，找到用户侧触发这条链路的两个动作：`matmul.compile(...)`（触发编译）和 `matmul_relu_kernel.get_kernel_source()`（取出刚生成的设备源码）。

**需要观察的现象**：用户侧的 `compile()` 和编译器侧的 `lower()` 是「同一件事的两端」——用户只管描述，`lower()` 负责把描述变成源码。

**预期结果**：你能在自己的笔记里画出一张图，标出 `compile → lower → lower_to_host_device_ir → (检查/Pass/拆分) → device_codegen → kernel_source` 这条路径。本实践无需运行，结论可直接得出。

#### 4.2.5 小练习与答案

**练习 1**：tilelang 的编译入口是哪个函数？它属于哪个模块？

> **答案**：`tilelang.engine.lower.lower()`（[lower.py:297](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L297)），并通过 `tilelang/__init__.py` 以 `tilelang.lower` 暴露。

**练习 2**：把 `PreLowerSemanticCheck`、`resolve_pipeline`、`device_codegen` 三者按执行先后排序。

> **答案**：`PreLowerSemanticCheck`（语义检查，最先）→ `resolve_pipeline(...).lower(...)`（跑 Pass 流水线）→ `device_codegen`（设备代码生成，最后）。

**练习 3**：为什么 `import tilelang.language as T` 默认就带有 CUDA 扩展？

> **答案**：因为 [tilelang/language/__init__.py:11](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/__init__.py#L11) 直接 `from tilelang.cuda.language import *`，即「默认方言 = cuda」；其它后端要显式用 `tilelang.<backend>.language`。

---

### 4.3 支持的硬件与典型算子

#### 4.3.1 概念说明

一个「生成 kernel 的编译器」必须知道**为哪种硬件生成代码**。在 tilelang / TVM 体系里，这个「目标硬件」用一个叫 **target** 的概念来描述。常见 target 包括：

| target | 含义 |
| --- | --- |
| `auto` | 自动按 CUDA → HIP → Metal 顺序探测当前机器可用后端 |
| `cuda` | NVIDIA GPU（可用 `{"kind":"cuda","arch":"sm_90"}` 指定架构） |
| `cutedsl` | NVIDIA CUTLASS/CuTe DSL 后端（较新） |
| `hip` | AMD GPU（ROCm） |
| `metal` | Apple Silicon GPU（M 系列芯片） |
| `llvm` | CPU 执行 |
| `webgpu` | 浏览器 / WebGPU 运行时 |
| `c` | 生成纯 C 源码（便于检视或自定义工具链） |

在算法层面，tilelang 擅长的是**可分块、计算密集、对内存层级敏感**的算子。README「OP Implementation Examples」列出的典型代表是认识 tilelang 适用范围的最佳参考。

一个判断 GEMM 规模与计算量的直觉公式：对 \( M \times K \) 乘 \( K \times N \) 的矩阵乘，浮点乘加运算量约为

\[
\text{FLOPs} \approx 2 \cdot M \cdot N \cdot K
\]

系数 2 来源于「一次乘 + 一次加」算两次浮点运算。tilelang 要做的，就是让这 \( 2MNK \) 次运算尽可能跑在硬件峰值上——而实现手段就是分块、显式内存层级与流水线。

#### 4.3.2 核心流程

tilelang 处理 target 的流程很简洁：

```text
用户传入 target（"auto" / "cuda" / {"kind":"cuda","arch":"sm_90"} / TVM Target 对象）
        │
        ▼
tilelang.backend.target.determine_target(target)   # 归一化成 TVM Target
        │
        ▼
target 决定：用哪条 Pass 流水线 + 用哪个代码生成后端
        │
        ▼
device_codegen 按 target 生成对应语言的源码（CUDA / HIP / Metal / C / WGSL）
```

注意 `auto` 是「运行时探测」：它会在编译时按 CUDA → HIP → Metal 顺序找第一个可用的，方便同一份代码跨机器跑。

#### 4.3.3 源码精读

**官方 target 一览表**：[docs/get_started/targets.md:13-22](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/targets.md#L13-L22) 给出全部 target 种类及说明，这是认识「tilelang 支持哪些硬件」最权威的表格。其中 `auto` 的探测顺序在该表与 [targets.md:15](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/targets.md#L15) 注释里都有说明：Detects CUDA → HIP → Metal。

**target 的多种写法**：[docs/get_started/targets.md:40-45](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/targets.md#L40-L45) 展示了 tilelang 统一接受的四种 target 输入形式（字符串 / 裸 kind / 配置字典 / TVM Target 对象），说明 tilelang 在 target 这一层做了很友好的归一化。

**实测验证过的设备**：[README.md:39-40](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/README.md#L39-L40) 明确列出了项目测试验证过的硬件清单——NVIDIA 侧 H100（支持 Auto TMA/WGMMA）、A100、V100、RTX 4090/3090/A6000；AMD 侧 MI250（Auto MatrixCore）、MI300X（Async Copy）。读这一段能让你知道「tilelang 的性能数字是在什么硬件上得到的」。

**典型算子清单**：[README.md:42-52](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/README.md#L42-L52) 列出了官方示例覆盖的算子，是判断「tilelang 适合做什么」的最好参考：

- Matrix Multiplication（普通 GEMM）
- Dequantization GEMM（反量化矩阵乘，常用于大模型权重）
- Flash Attention（注意力机制）
- Flash Linear Attention（线性注意力，如 RetNet/Mamba）
- Flash MLA Decoding（DeepSeek 的多头潜注意力解码）
- Native Sparse Attention（稀疏注意力）

这说明 tilelang 的「主战场」是**大模型训练 / 推理里最吃性能的那几个算子**。

**determine_target 在编译入口被调用**：回到链路代码 [tilelang/engine/lower.py:274-275](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L274-L275)：

```python
if isinstance(target, str):
    target = determine_target(target)
```

这一行就是把用户友好的 target 写法（字符串/字典）转成 TVM 内部 Target 的真实落点，连接了「4.2 链路」和「4.3 硬件」两个模块。

#### 4.3.4 代码实践

**实践目标**：把「支持哪些硬件 / 擅长哪些算子」从文字变成可在仓库里核对的清单。

**操作步骤**（阅读 + 对照型实践）：

1. 打开 [docs/get_started/targets.md:13-22](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/targets.md#L13-L22)，把 8 种 target 抄进你的笔记，并标注哪些是 GPU（cuda/hip/metal/webgpu/cutedsl）、哪些不是（llvm/c）。
2. 对照 [README.md:39-40](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/README.md#L39-L40)，把「target」和「具体型号」对应起来（例如 `cuda` + `sm_90a` ↔ H100）。
3. 浏览 `examples/` 目录（[README.md:42-52](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/README.md#L42-L52) 给了链接），挑一个你最关心的算子（如 `flash_attention` 或 `deepseek_mla`），记下它的目录路径，后续讲义会用到。

**需要观察的现象**：target 与具体 GPU 型号是「多对一」关系——同一个 `cuda` target 通过 `arch` 可以指向 A100、H100 等不同代际的卡。

**预期结果**：得到一张三列表格：**target kind ｜ 对应硬件家族 ｜ 代表算子示例**。本实践无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`target="auto"` 在一台同时装了 NVIDIA 和 AMD 卡的机器上，会优先选哪个？

> **答案**：优先选 CUDA（探测顺序是 CUDA → HIP → Metal，见 [targets.md:15](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/targets.md#L15)）。

**练习 2**：想指定「编译到 H100」，target 字典应该怎么写？

> **答案**：`{"kind": "cuda", "arch": "sm_90a"}`（H100 对应 `sm_90a`，见 [targets.md:144](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/targets.md#L144)）。

**练习 3**：tilelang 的「主战场」算子有什么共同特征？

> **答案**：它们都是**可分块（tile）、计算密集、对 GPU 内存层级（shared/register）极敏感**的算子（GEMM / Attention / MLA / 稀疏注意力等），正是 tile 级抽象最能发挥优势的场景。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「全景理解」小任务：

**任务**：为 tilelang 画一张**一页纸全景图**，要求在同一张图上同时体现以下三层信息：

1. **定位层**：在图的最上方，用一句话写出 tilelang 是什么（参考 4.1）。
2. **链路层**：在图中间，画出从 `@tilelang.jit` 函数到可调用 kernel 的完整流程，并在每个关键节点旁标注对应的源码文件与行号（参考 4.2，至少标出 `lower`、`lower_to_host_device_ir`、`device_codegen` 三处）。
3. **硬件/算子层**：在图底部，画出「target → 硬件 → 典型算子」的对应关系，至少覆盖 cuda/hip/metal/llvm 四种 target 与 GEMM/FlashAttention/MLA 三类算子（参考 4.3）。

**验收标准**：

- 图上每一个源码引用都能在仓库里点开（链接指向 HEAD `c6294f07`）。
- 能用这张图向一个没接触过 tilelang 的同事解释清楚「我写的一段 Python 是怎么变成 GPU 代码的」。
- 能在图上指出「哪几步是 tilelang 自己实现的、哪几步复用了 TVM」。

> 提示：如果你想在 GPU 上真正跑一次 kernel 来印证这张图，请先完成 u1-l2（安装）和 u1-l4（Quickstart 实跑），本讲不要求运行。

## 6. 本讲小结

- **tilelang 是构建在 TVM 之上的 tile 级 DSL**，用 Pythonic 语法描述高性能 GPU/CPU kernel，兼顾开发效率与极致性能（[README.md:12](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/README.md#L12)）。
- **它的本质是一个编译器**，主干链路是：DSL → TIR → 语义检查 → Pass 流水线 → host/device 拆分 → 设备代码生成 → Kernel Adapter → 可调用 kernel。
- **编译入口是 `tilelang.engine.lower()`**（[lower.py:297](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L297)），内部由 `lower_to_host_device_ir` 完成检查与 Pass、由 `device_codegen` 生成设备源码。
- **`import tilelang.language as T` 默认是 CUDA 方言**（[language/__init__.py:11](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/__init__.py#L11)），其它后端通过 `tilelang.<backend>.language` 显式引入。
- **支持的硬件覆盖 NVIDIA / AMD / Apple / CPU / WebGPU**，常用 target 有 `auto/cuda/hip/metal/llvm/webgpu/cutedsl/c`（[targets.md:13-22](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/targets.md#L13-L22)）。
- **主战场算子**是 GEMM、Dequant GEMM、FlashAttention、LinearAttention、MLA、稀疏注意力等大模型核心算子（[README.md:42-52](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/README.md#L42-L52)）。

## 7. 下一步学习建议

本讲建立的是「全景地图」，接下来按手册顺序推进：

1. **u1-l2 安装、构建与运行环境**：把 tilelang 装起来，跑通 `import tilelang`，理解 native 库加载。
2. **u1-l3 仓库目录结构与包入口**：把本讲里提到的 `tilelang/`、`src/`、`3rdparty/` 目录逐个摸清，建立「Python 侧与 C++ 侧一一对应」的认知。
3. **u1-l4 第一个 Kernel：Quickstart GEMM 实跑**：真正运行一个 GEMM，对照本讲的链路图，观察 `compile()` 和 `get_kernel_source()` 的输出，把「图」变成「可运行的现实」。

如果你急于看「DSL 怎么写」，也可以在完成 u1-l2 后直接跳到 u2（DSL 语言基础），再回头补 u1-l3/u1-l4。

> 建议延伸阅读（不在本讲范围，但有助于加深理解）：[docs/get_started/overview.md](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/overview.md) 的「Compilation Flow」一节，以及 [TVM 项目](https://tvm.apache.org/) 关于 TIR 的入门材料——理解 TIR 能让你后续读 tilelang 的 Pass 与代码生成时事半功倍。
