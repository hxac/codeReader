# 代码结构与「核心 vs Ascend」分层原则

## 1. 本讲目标

上一讲（u1-l1）我们建立了 Triton-Ascend 的全局地图：它由三大组件构成——语言扩展、compiler、driver，主链路是 `TTIR → Linalg IR → AscendNPU IR → triton_xxx_kernel.o`。

但地图只告诉我们「有哪些零件」。本讲要回答一个更实际的问题：**这些零件的源码到底放在仓库的哪里？为什么放在那里？** 当你以后想改一行代码、加一个功能、或者读一段实现时，第一个动作就是定位文件——本讲就是那张「仓库导航图」。

学完本讲，你应当能够：

1. 区分 **Triton core** 与 **Triton-Ascend** 两部分代码，知道它们各自的「家园目录」。
2. 识别 `third_party/ascend/` 下 `language`、`backend`、`lib`、`include`、`costmodel` 等子目录的职责。
3. 理解并运用项目的核心放置原则：**「目标无关的改动留在 Triton core，目标亲和的改动放进 Triton-Ascend」**。
4. 看懂这条原则在「安装打包」阶段是如何被代码强制实现的（拷贝 + 挂载）。
5. 诚实看待分层：理解 core 里少数几个「扩展点钩子」是刻意的、最小化的例外。

---

## 2. 前置知识

本讲几乎不涉及算法，但要理解几个工程概念。我们用大白话逐一说明。

### 2.1 什么是「目标无关」与「目标亲和」

- **目标无关（target independent）**：这段代码跟「具体硬件」没有关系，换一块芯片它照样成立。比如 `@triton.jit` 装饰器的实现、IR 的内存分配逻辑、缓存目录管理。这些属于所有后端共享的「公共能力」。
- **目标亲和（target affinitive）**：这段代码是专门为某一类硬件写的，离开了它就没有意义。比如「如何把一个指针表达式重写成昇腾能张量化的形式」「如何用 CANN 的 `rtKernelLaunch` 启动一个内核」。这些是 Ascend 专属的。

> 一句话直觉：**如果你删掉这段代码，是「所有后端都坏了」还是「只有 Ascend 坏了」？** 前者说明它属于 core，后者说明它属于 Ascend。

### 2.2 Python 的包与模块

Python 用目录 + `__init__.py` 来组织代码包。`import triton.language.extra.cann` 这个写法，背后对应的是目录 `triton/language/extra/cann/`。本讲会看到，`cann` 这个目录在**源码仓库里**和**安装到 site-packages 之后**，物理位置是不一样的——这正是分层机制的关键。

### 2.3 打包与安装的直觉（setuptools）

`pip install -e .` 背后跑的是 `setup.py`（或 `pyproject.toml` 声明的构建后端）。`setup.py` 可以在安装时**把某些目录拷贝到另一个位置**，甚至**创建符号链接**。Triton-Ascend 正是用这个能力，把 `third_party/ascend/language/` 下的内容「挂载」到 `triton.language.extra` 下。你暂时只要知道「安装脚本可以在装的时候搬文件」就够了。

### 2.4 git submodule（第三方后端的栖身之处）

`third_party/` 是仓库里专门存放「可选后端」的地方。在本仓库里，Ascend 是默认构建的主后端之一，但代码仍按社区约定放在 `third_party/ascend/`，与 `nvidia`、`amd` 等其他后端并列。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下表。读者现在不必逐一打开，后面讲解到时再对照查阅。

| 文件 | 作用 | 本讲用来讲什么 |
| --- | --- | --- |
| `docs/en/architecture_design_and_core_features.md` | 架构与目录说明文档 | 「分层原则」与「目录职责表」的权威出处 |
| `pyproject.toml` | 构建配置 | 看构建依赖、构建后端，确认 core 与 ascend 的构建关系 |
| `setup.py` | 安装/打包入口 | 演示「拷贝 + 挂载」如何落地分层 |
| `python/triton/backends/__init__.py` | 后端发现机制 | 解释安装后 Ascend 后端如何被 Triton 自动找到 |
| `python/triton/compiler/code_generator.py` | core 的 IR 构造器 | 展示 core 里的「扩展点钩子」 |
| `python/triton/runtime/interpreter.py` | core 的解释器 | 展示「带保护的可选导入」钩子 |
| `third_party/ascend/backend/__init__.py` | Ascend 后端入口 | 展示「运行时 monkey patch」这一分层手段 |
| `third_party/ascend/language/cann/__init__.py` | Ascend 语言扩展入口 | 展示 `cann` 模块对外暴露了什么 |
| `third_party/ascend/backend/backend_register.py` | 运行时策略注册 | 展示 torch_npu/mindspore 两种运行时的策略分派 |

---

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块，从「原则」到「实现」，最后到「现实的例外」。

### 4.1 两条代码线与「分层原则」

#### 4.1.1 概念说明

整个仓库本质上只有「两条代码线」：

1. **Triton core（核心线）**：来自社区标准 Triton 的、与硬件无关的通用实现。它负责把 Python kernel 翻译成与硬件无关的中间表示 TTIR，并提供 JIT、缓存、解释器等公共运行时。
2. **Triton-Ascend（亲和线）**：所有为华为昇腾 NPU（CANN 软件栈）专门写的代码，它接在 core 后面，把 TTIR 一路变换成能在 NPU 上跑的二进制。

项目对这两条线的划分立了一条**硬规矩**，原文如下：

> - **If the modification is target independent**, it should be retained in the **Triton core** part (such as general modifications to the language and runtime).
> - **If the modification is target affinitive**, it should be placed in the **Triton-Ascend** part.

我们把这条规矩叫做「**先 core、后 ascend**」：写新代码时，先问自己「这是不是与硬件无关的通用能力？」如果是，就放进 core；只有在确实离不开昇腾硬件时，才放进 ascend。

#### 4.1.2 核心流程

「分层原则」如何影响日常开发？可以总结成一条简单的决策流：

```
我要写/改一段代码
        │
        ▼
它依赖昇腾硬件/CANN/BiSheng 吗？
        │
   ┌────┴────┐
   否        是
   │         │
   ▼         ▼
 放进 core   放进 third_party/ascend
(python/    (language/backend/
 include/    include/lib/
 lib/)       costmodel/...)
```

这条流不只是「美学」，它有现实意义：core 的改动理论上对所有后端（NVIDIA、AMD、Ascend）都生效；ascend 的改动只影响 Ascend。混放会导致「我以为只改了 Ascend，结果把别人的后端也改了」这类事故。

#### 4.1.3 源码精读

分层原则的权威出处是架构文档的「Code Structure Principles」与「Directory Structure」两节。

**代码原则原文**（[docs/en/architecture_design_and_core_features.md:35-38](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/architecture_design_and_core_features.md#L35-L38)）——这一段把「target independent → core / target affinitive → ascend」白纸黑字写成了项目约束。

紧接着的目录职责表（[docs/en/architecture_design_and_core_features.md:42-53](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/architecture_design_and_core_features.md#L42-L53)）把每个目录归到了某一层，下面是简化后的对照：

| 目录 | 所属层 | 文档给的一句话职责 |
| --- | --- | --- |
| `python/` | Triton core | 标准 Triton 的通用 Python：`triton.language`、JIT、runtime、cache、工具入口 |
| `include/`、`lib/` | Triton core | 通用的 C++/MLIR 基础设施、方言、pass、转换 |
| `third_party/ascend/` | Triton-Ascend | Ascend 后端根目录，包含语言扩展、编译后端、运行时驱动、MLIR pass、示例与测试 |
| `third_party/ascend/language/` | 语言扩展 | 安装时被链接到 `triton.language.extra`，使 kernel 可用 `triton.language.extra.cann` |
| `third_party/ascend/backend/compiler.py` | compiler | Ascend 编译后端主入口 |
| `third_party/ascend/backend/driver.py` | driver | Ascend 运行时驱动 |
| `third_party/ascend/include/`、`third_party/ascend/lib/` | compiler | Ascend 专属 MLIR 方言与 pass（`TritonToLinalg` 等） |

这张表是本讲的「地图主图」，后面几个模块都是对它的展开。

#### 4.1.4 代码实践

**实践目标**：用「删掉它会坏谁」的标准，亲手验证一次分层判断。

**操作步骤**：

1. 打开 [docs/en/architecture_design_and_core_features.md:42-53](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/architecture_design_and_core_features.md#L42-L53) 的目录表。
2. 任选表中的 3 个目录，分别为它们设想「如果删除这个目录」的后果。
3. 把后果写成「所有后端都坏 / 只有 Ascend 坏」二选一。

**需要观察的现象 / 预期结果**：

- `python/` 删除 → JIT、缓存、TTIR 生成都没了 → **所有后端都坏** → 它是 core。✅
- `third_party/ascend/backend/compiler.py` 删除 → Ascend 没法把 TTIR 变成 `.o`，但 NVIDIA/AMD 后端不受影响 → **只有 Ascend 坏** → 它是 ascend。✅
- `third_party/ascend/lib/TritonToLinalg` 删除 → Ascend 缺一个 lowering pass，别的后端照常 → **只有 Ascend 坏** → 它是 ascend。✅

> 待本地验证：以上判断基于源码职责，不需要运行设备即可完成；结论与文档归类一致。

#### 4.1.5 小练习与答案

**练习 1**：假设你想给 `@triton.jit` 增加一个「记录每次编译耗时的通用日志」，应该放在 core 还是 ascend？为什么？

> **参考答案**：放在 **core**。因为「记录编译耗时」与具体硬件无关，对所有后端都有用，属于 target independent。

**练习 2**：如果你想新增「让 Ascend 把某类访存用 UB（Unified Buffer）专门优化」的代码，应该放哪里？

> **参考答案**：放在 **ascend**（很可能是 `third_party/ascend/lib/` 下新增一个 pass）。它强依赖昇腾的 UB 硬件单元，属于 target affinitive。

---

### 4.2 Triton core 的两个家园：`python/` 与 `include/`、`lib/`

#### 4.2.1 概念说明

Triton core 的代码按「语言」分在两个家园：

- **`python/`**：所有 Python 实现。kernel 作者直接打交道的 `@triton.jit`、`tl.load` 等都在这里。
- **`include/` 与 `lib/`**：所有 C++/MLIR 实现。负责 IR 方言定义、分析、转换（Conversion）等底层基础设施。

为什么要分 Python 和 C++？因为 Triton 是「Python 前端 + C++/MLIR 后端」的混合架构：Python 负责易用，C++ 负责高性能的 IR 处理。两者通过 pybind11 桥接（`triton._C.libtriton`）。

#### 4.2.2 核心流程

core 内部的代码流可以粗略画成：

```
python/triton/runtime/jit.py        ← @triton.jit 触发编译
        │
        ▼
python/triton/compiler/             ← 生成 TTIR（硬件无关）
        │
        ▼ (交给某个后端，例如 ascend)
include/triton/ + lib/              ← 通用 C++/MLIR 基础设施（方言、分析、转换）
```

注意：core 的 C++ 基础设施里**没有** Ascend 专属 pass——`lib/Conversion/` 里是 `TritonGPUToLLVM`、`TritonToTritonGPU` 这类**面向 GPU** 的通用转换，与 Ascend 无关。

#### 4.2.3 源码精读

**Python 家园**（`python/triton/`）的顶层结构如下，全部属于 core：

| 路径 | 职责 |
| --- | --- |
| `python/triton/runtime/jit.py` | `@triton.jit` 装饰器与 `JITFunction`（编译触发点） |
| `python/triton/runtime/autotuner.py` | 通用的 `@triton.autotune` 机制 |
| `python/triton/runtime/cache.py`、`code_cache.py` | 编译缓存 |
| `python/triton/runtime/interpreter.py` | 解释器模式（精度基准） |
| `python/triton/language/core.py` | `tl.*` 语言 API 的核心实现 |
| `python/triton/compiler/` | TTIR 生成与编译驱动 |
| `python/triton/backends/` | 后端**发现**机制（不包含具体后端实现） |

**C++ 家园**（`include/triton/` 与 `lib/`）的顶层结构：

| 路径 | 职责 |
| --- | --- |
| `include/triton/Dialect/`、`lib/Dialect/` | 通用方言：`Triton`、`TritonGPU`、`TritonInstrument`、`Gluon` 等 |
| `lib/Conversion/TritonGPUToLLVM/` | GPU→LLVM 的通用转换 |
| `lib/Conversion/TritonToTritonGPU/` | Triton→TritonGPU 的通用转换 |
| `lib/Analysis/`、`lib/Tools/`、`lib/Target/` | 通用分析、工具、目标后端基础设施 |

关键点：`lib/Conversion/` 里出现的是 `TritonGPUToLLVM`、`TritonInstrumentToLLVM`、`TritonToTritonGPU`——它们是社区 Triton 原有的、面向 GPU 的转换。**Ascend 的转换（`TritonToLinalg` 等）不在这里**，而在 `third_party/ascend/lib/`。这正是分层原则在 C++ 层的体现。

#### 4.2.4 代码实践

**实践目标**：在 core 的 Python 家园里走一遍「JIT 入口」的定位，确认它确实与硬件无关。

**操作步骤**：

1. 打开 `python/triton/runtime/jit.py`，找到 `@triton.jit` 对应的 `JITFunction` 类。
2. 搜索它内部是否出现 `ascend`、`npu`、`cann` 等字样。
3. 对比：再打开 `third_party/ascend/backend/compiler.py`，看它是否大量出现这些字样。

**需要观察的现象 / 预期结果**：

- `jit.py` 主要处理「函数签名、缓存键、何时触发编译」等通用逻辑，几乎不关心具体硬件——它是 core。
- `compiler.py` 则满是 `Ascend`、`NPU`、`CANN`、`BiSheng` 相关代码——它是 ascend。
- 这种对比会让你对「分层」产生直观感受。

> 待本地验证：具体关键字出现频次可在本地用搜索工具统计确认。

#### 4.2.5 小练习与答案

**练习 1**：`lib/Conversion/TritonGPUToLLVM/` 属于 core 还是 ascend？为什么它在 core 里？

> **参考答案**：属于 **core**。它是社区 Triton 原有的、面向 GPU 的通用转换，与昇腾无关，所以放在 core 的 `lib/Conversion/`。

**练习 2**：用户写的 `import triton; import triton.language as tl` 中，`triton` 这个包的根目录对应仓库里的哪个文件夹？

> **参考答案**：对应 `python/`。安装后 `triton` 包的内容来自 `python/triton/`。

---

### 4.3 Triton-Ascend 的大本营：`third_party/ascend/`

#### 4.3.1 概念说明

`third_party/ascend/` 是 Ascend 后端的「大本营」。上一讲提到的三大组件（语言扩展、compiler、driver）以及它们的 C++ pass、调优、示例、测试，**全部**住在这一棵子树里。这样设计的好处是：如果某天不想支持 Ascend，理论上只要不构建这棵子树即可，core 完全不受影响。

#### 4.3.2 核心流程

这棵子树内部又按职责分成若干目录，对应上一讲三大组件 + 周边设施：

```
third_party/ascend/
├── language/          ← 语言扩展（cann）
│   └── cann/
│       ├── libdevice.py        ← 数学函数封装
│       └── extension/          ← custom_op / compile_hint / 同步原语等
├── backend/           ← compiler + driver
│   ├── compiler.py             ← AscendBackend（编译阶段注册）
│   ├── driver.py               ← NPUDriver / NPULauncher（运行时）
│   ├── npu_utils.cpp           ← 硬件探测
│   ├── backend_register.py     ← torch_npu/mindspore 策略分派
│   └── runtime/                ← autotuner / costmodel / ubtuner 等
├── include/ 与 lib/   ← Ascend 专属 MLIR 方言与 pass
│   ├── TritonToLinalg/         ← ttir→linalg
│   ├── TritonToStructured/     ← 指针/掩码张量化
│   ├── DynamicCVPipeline/      ← Cube-Vector 流水线
│   ├── AutoBlockify/           ← 并行块映射
│   └── ...（共 14 个 pass 目录）
├── costmodel/         ← 编译期代价模型（含硬件 schema JSON）
├── AscendNPU-IR/      ← Ascend NPU IR 与 BiSheng 链路集成
├── tutorials/         ← 示例（vector-add / softmax / matmul ...）
└── unittest/          ← Python 单测与 MLIR conversion 测试
```

注意 `third_party/ascend/include/` 与顶层 `include/` 同名但**完全不同**：前者是 Ascend 专属 pass 头文件，后者是 core 通用基础设施。这是分层最容易让新手混淆的点，务必记住「带 `third_party/ascend/` 前缀的才是亲和代码」。

#### 4.3.3 源码精读

我们看两个最能体现「Ascend 子树自成一体」的入口文件。

**语言扩展入口**（[third_party/ascend/language/cann/__init__.py:21-53](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/language/cann/__init__.py#L21-L53)）——这个文件对外暴露 `libdevice`（数学函数）和 `extension`（自定义算子等），并把部分接口映射到 Ascend 的专属实现（例如 `libdevice.tanh`、`extension.flip`）。这就是 kernel 里 `triton.language.extra.cann` 能用的东西的来源。

```python
from . import libdevice
from . import extension
# ...把若干数学函数指向 Ascend 专属实现或通用 math
libdevice.exp = math.exp
math.tanh = libdevice.tanh
__all__ = ["libdevice", "extension"]
```

**后端策略注册**（[third_party/ascend/backend/backend_register.py:25-55](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/backend_register.py#L25-L55)）——它用 `BackendStrategyRegistry` 把「同一个能力」按运行时（`torch_npu` 或 `mindspore`）分别注册，例如「如何取当前流」：

```python
@backend_strategy_registry.register("torch_npu", "get_current_stream")
def get_current_stream(device): ...

@backend_strategy_registry.register("mindspore", "get_current_stream")
def get_current_stream(device): ...
```

这说明：连「Ascend 内部」还要再按上层框架（torch_npu / mindspore）分策略，目标亲和代码被组织得非常细。

#### 4.3.4 代码实践

**实践目标**：亲手列出 Ascend 子树下的全部 pass 目录，建立「亲和 C++ 代码量」的直觉。

**操作步骤**：

1. 列出 `third_party/ascend/lib/` 下的所有子目录。
2. 与顶层 `lib/Conversion/` 的子目录对比。

**需要观察的现象 / 预期结果**：

- `third_party/ascend/lib/` 下有约 14 个 pass 目录（`TritonToLinalg`、`TritonToStructured`、`TritonToUnstructure`、`DynamicCVPipeline`、`AutoBlockify`、`DiscreteMaskAccessConversion`、`TritonToHIVM`、`TritonToHFusion`、`TritonToAnnotation`、`TritonToLLVM`、`TritonToGraph`、`TritonControlFlowOpt`、`Dialect`、`Utils`）。
- 顶层 `lib/Conversion/` 只有面向 GPU 的 3 个。
- 结论：Ascend 的 lowering 工作量集中在 `third_party/ascend/lib/`，core 的 C++ 部分与之泾渭分明。

> 待本地验证：pass 目录数量可本地 `ls` 确认。

#### 4.3.5 小练习与答案

**练习 1**：`third_party/ascend/lib/TritonToLinalg/` 和顶层 `lib/Conversion/TritonToTritonGPU/` 都是「转换」，它们为什么不在同一个目录？

> **参考答案**：前者是 Ascend 专属的 ttir→linalg 转换（目标亲和），后者是社区原有的 Triton→TritonGPU 转换（目标无关）。按分层原则，亲和的放进 `third_party/ascend/lib/`，通用的留在 core 的 `lib/Conversion/`。

**练习 2**：`third_party/ascend/include/` 和顶层 `include/` 同名，初学者怎么快速判断一个 `#include` 引到的是哪个？

> **参考答案**：看构建目标与 CMake 配置。Ascend 专属 pass 的头文件通过 `third_party/ascend/CMakeLists.txt` 组织进 Ascend 后端目标；通用头文件来自顶层 `include/`。源码层面最简单的判别是看路径前缀是否带 `third_party/ascend/`。

---

### 4.4 分层如何落地：安装期的「拷贝 + 挂载」机制

#### 4.4.1 概念说明

到目前为止，分层还只是「源码放在不同目录」。但有一个问题没解决：用户写 kernel 时用的是 `triton.language.extra.cann`，而源码里 `cann` 明明在 `third_party/ascend/language/cann/`。这中间是怎么对上的？

答案是：**`setup.py` 在安装时，把后端目录「搬」到了 core 的命名空间下**。具体有两种手段——正式安装时**拷贝**，开发模式（`pip install -e .`）下**创建符号链接**。这套机制让「源码物理位置」和「import 路径」解耦，是分层原则能真正工作的关键。

#### 4.4.2 核心流程

安装时的搬运流程：

```
源码仓库                                   安装后（site-packages / 可编辑目录）
third_party/ascend/backend/      ──拷贝──▶  triton/backends/ascend/      (成为 triton.backends.ascend)
third_party/ascend/language/cann ──挂载──▶  triton/language/extra/cann/  (成为 triton.language.extra.cann)
                         │
                         ▼
python/triton/backends/__init__.py 在运行时扫描，发现 ascend 后端并加载它的 AscendBackend
```

搬运完之后，Triton 启动时会自动「发现」这些后端，于是 Ascend 就被接进了编译流程。

#### 4.4.3 源码精读

**第一步：声明要构建哪些后端**（[setup.py:764](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/setup.py#L764)）——这一行说明 ascend 与 nvidia、amd 并列为「in-tree 后端」：

```python
backends = [*BackendInstaller.copy(["ascend", "nvidia", "amd"]), *BackendInstaller.copy_externals()]
```

**第二步：定位后端的 `backend/`、`language/` 目录**（[setup.py:96-101](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/setup.py#L96-L101)）——注意它约定每个后端必须有 `backend/compiler.py` 和 `backend/driver.py`，并且可选地有 `language/` 目录：

```python
backend_path = os.path.join(backend_src_dir, "backend")
...
language_dir = os.path.join(backend_src_dir, "language")
```

**第三步：把 `language/` 挂载到 `triton.language.extra`**（[setup.py:823-830](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/setup.py#L823-L830)）——这是「挂载」的核心，它在开发模式下创建符号链接，把 `cann` 链到 core 的 `language/extra/` 下：

```python
extra_dir = os.path.abspath(os.path.join(..., "python", "triton", "language", "extra"))
for x in os.listdir(backend.language_dir):
    src_dir = os.path.join(backend.language_dir, x)
    install_dir = os.path.join(extra_dir, x)
```

安装打包阶段也有等价的「拷贝」声明（[setup.py:777-781](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/setup.py#L777-L781)），把后端的 `language` 内容安装为 `triton.language.extra.<名字>`。

**第四步：运行时自动发现后端**（[python/triton/backends/__init__.py:38-63](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/backends/__init__.py#L38-L63)）——它通过 Python entry points（组名 `triton.backends`）或 in-tree 目录扫描，找到每个后端的 `compiler.py` 和 `driver.py`，并从中提取 `BaseBackend` 的具体子类（即 `AscendBackend`）：

```python
for ep in entry_points().select(group="triton.backends"):
    compiler = importlib.import_module(f"{ep.value}.compiler")
    driver = importlib.import_module(f"{ep.value}.driver")
    backends[ep.name] = Backend(_find_concrete_subclasses(compiler, BaseBackend), ...)
```

这套「拷贝/挂载 + 自动发现」组合，正是「core 提供插槽、ascend 提供插件」的工程实现。

#### 4.4.4 代码实践

**实践目标**：亲眼看到「安装让 ascend 进入 core 命名空间」这件事。

**操作步骤**：

1. 若已 `pip install -e .`，在仓库根目录查看是否生成了符号链接：`ls -la python/triton/language/extra/`，看是否有指向 `third_party/ascend/language/cann` 的 `cann` 链接。
2. 在仓库根目录查看 `python/triton/backends/` 下是否出现了 `ascend/`（或对应的链接）。
3. 若未安装，也可直接读 [setup.py:816-834](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/setup.py#L816-L834)，理解 `add_link_to_backends` 会在 `extra_dir` 下为每个后端的 `language` 子目录建链接。

**需要观察的现象 / 预期结果**：

- 开发模式下，`python/triton/language/extra/cann` 应是指向 `third_party/ascend/language/cann` 的符号链接；于是 `import triton.language.extra.cann` 在源码层面等价于读到了 ascend 的语言扩展。
- `python/triton/backends/ascend/` 同理指向 `third_party/ascend/backend`。

> 待本地验证：是否已生成链接取决于本地是否执行过安装；未安装时仅能通过阅读 `setup.py` 推断。这是「源码阅读型实践」，不强制运行设备。

#### 4.4.5 小练习与答案

**练习 1**：用户在 kernel 里写 `import triton.language.extra.cann as cann`，请追溯这个 `cann` 模块在源码仓库里的真实出处。

> **参考答案**：真实出处是 `third_party/ascend/language/cann/`。安装时由 `setup.py` 的挂载逻辑（`add_link_to_backends`）把它链接（或拷贝）到 `triton.language.extra.cann`，所以 import 路径与源码物理位置不同。

**练习 2**：为什么 Triton 要用「entry points / 自动发现」而不是在 core 里硬编码 `import ascend`？

> **参考答案**：为了保持 core 与具体后端解耦。硬编码会让 core 强依赖 ascend，违背分层；用自动发现，core 只定义 `BaseBackend` 抽象，任何后端只要放到 `triton.backends` 命名空间并实现该抽象，就能被接入。

---

### 4.5 分层的现实：core 里的「扩展点钩子」

#### 4.5.1 概念说明

前面讲的都是「干净分层」的理想画面。但作为诚实的源码读者，必须指出一个现实：**分层不是 100% 纯净的**。

为了让 Ascend 能深度介入编译流程（例如注入自己的 IR builder、提供解释器），core 里保留了**少数几个刻意的「扩展点钩子」**。这些钩子本身写得很「中性」（带保护、可降级），但它们确实让 core「知道」了 ascend 的存在。理解这一点，你才不会在 core 里看到 `ascend` 字样时感到困惑，也才能区分「这是破坏分层的污染」还是「这是设计好的插槽」。

#### 4.5.2 核心流程

Ascend 介入 core 行为，有三种典型手段，按「耦合程度」从低到高：

```
1. 带保护的可选导入（最松）   —— core 用 try/except 尝试加载 ascend，失败则降级
2. core 暴露中性扩展点        —— core 留一个「builder 插槽」，ascend 往里塞方法
3. 运行时 monkey patch（最紧）—— ascend 后端启动时替换 core 某些方法的行为
```

这三种都遵循同一个底线：**ascend 专属的代码体仍然住在 `third_party/ascend/`**，core 只是留了「接线端子」。

#### 4.5.3 源码精读

**手段一：带保护的可选导入**。core 的解释器尝试加载 ascend 解释器，失败就降级（[python/triton/runtime/interpreter.py:30-42](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/interpreter.py#L30-L42)）——注意这个钩子本身是中性的，它不假设 ascend 一定存在：

```python
def _try_import_ascend():
    global _has_ascend_support, AscendInterpreterBuilder
    try:
        from . import ascend_interpreter
        AscendInterpreterBuilder = ascend_interpreter.AscendInterpreterBuilder
        _has_ascend_support = True
    except ImportError:
        _has_ascend_support = False
        AscendInterpreterBuilder = None
```

**手段二：core 暴露中性扩展点**。core 的 IR 构造器在初始化时创建一个 `ascend_builder`，并通过 `setup_unified_builder` 把它的方法合并进主 builder（[python/triton/compiler/code_generator.py:331-333](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/code_generator.py#L331-L333)）——`ascend_builder` 来自 `libtriton.ascend` 这个 C++ 扩展（其源码在 `third_party/ascend/`），而 `setup_unified_builder` 是一个通用的「多 builder 合并」抽象：

```python
self.ascend_builder = ascend_ir.ascendnpu_ir_builder(context, getattr(options, "arch", ""))
self.ascend_builder.set_loc(file_name, begin_line, 0)
setup_unified_builder(self.builder, self.ascend_builder)
```

> 注：这里 core 直接 `import` 了 ascend 的 C++ 扩展（见 [code_generator.py:23](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/code_generator.py#L23)），是一个相对紧的耦合点。它是分层原则下「为性能/耦合而做的最小妥协」，并非随意混放。

**手段三：运行时 monkey patch**。Ascend 后端在导入时通过 `_apply_ascend_patch` 修改 core 的 `CodeGenerator.__init__`、`compiler.parse`、`TritonSemantic.dot` 等行为（[third_party/ascend/backend/__init__.py:27-51](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/__init__.py#L27-L51)）——关键在于：**patch 的代码体写在 ascend 子树里，而不是改 core 源码**：

```python
def _apply_ascend_patch():
    from triton.compiler.code_generator import CodeGenerator
    if not getattr(CodeGenerator, "_ascend_patch_applied", False):
        _original_cg_init = CodeGenerator.__init__
        def _patched_cg_init(self, *args, **kwargs):
            """Monkey Patch for Ascend: 注入 hacc.target 属性"""
            _original_cg_init(self, *args, **kwargs)
            ...
        CodeGenerator.__init__ = _patched_cg_init
```

这就是「分层原则」在现实里最精妙的体现：ascend 改了 core 的行为，但改动**逻辑**仍归属 ascend，core 的源码文件本身没有被动过一刀。

#### 4.5.4 代码实践

**实践目标**：学会区分「core 里的 ascend 字样」是钩子还是污染。

**操作步骤**：

1. 在 `python/triton/` 下搜索 `ascend` 关键字。
2. 对每一处命中，判断它属于三类中的哪一类：
   - **中性钩子**（带 try/except、可降级，如 `_try_import_ascend`）；
   - **扩展点插槽**（core 留的接口，如 `ascend_builder`）；
   - **疑似越界**（亲和逻辑直接写在了 core 里，没有保护也没有抽象）。

**需要观察的现象 / 预期结果**：

- `interpreter.py` 的 `_try_import_ascend` 属于**中性钩子**——它假设 ascend 可能不存在。
- `code_generator.py` 的 `ascend_builder` 属于**扩展点插槽**——它把具体实现委托给 ascend 的 C++ 扩展。
- 你大概率会发现命中很少，且大多是这两类——这说明 core 基本守住了分层边界。
- `python/triton/runtime/ascend_interpreter.py` 是一个值得注意的**例外**：它是 ascend 专属的解释器代码，却物理上放在 core 的 `runtime/` 目录里（与 `interpreter.py` 强耦合）。这是一个为「紧耦合」而做的现实取舍，阅读时应把它视为「core 里被特许的 ascend 飞地」。

> 待本地验证：搜索命中的具体行数与分类请在本地确认；本实践为「源码阅读型」，不依赖设备。

#### 4.5.5 小练习与答案

**练习 1**：core 的 `interpreter.py` 用 `_try_import_ascend()` 而不是直接 `import ascend_interpreter`，这样做的好处是什么？

> **参考答案**：用 try/except 包裹后，即使没有 ascend 支持，core 解释器也能正常运行（只是 `_has_ascend_support=False`）。这让 core 保持「ascend 可有可无」的松耦合，是分层原则的体现。

**练习 2**：ascend 用 monkey patch 改了 `CodeGenerator.__init__`，这算不算「破坏了分层」？

> **参考答案**：不算「破坏」，而是「设计好的扩展手段」。因为改动的**代码体**（`_apply_ascend_patch`）写在 `third_party/ascend/backend/__init__.py` 里，core 的源码文件并未被修改。它利用了 Python 运行时的动态性，在不 fork core 的前提下注入 ascend 行为，恰恰是分层原则在工程上的落地方式之一。

---

## 5. 综合实践

把本讲所有知识串起来，完成下面这个「仓库考古」小任务。

**任务**：在仓库中找出 **3 处 Ascend 亲和代码** 与 **1 处目标无关代码**，分别为每一处写一段「它为什么放在当前位置」的说明，要求：

1. 每一处都给出 `文件路径:行号` 与永久链接；
2. 用「删掉它会坏谁」的标准论证它属于 core 还是 ascend；
3. 对其中至少 1 处，说明它是通过本讲的哪种机制（拷贝/挂载/自动发现/扩展点钩子/monkey patch）与 core 发生联系的。

**参考做法（示例，非唯一答案）**：

- **亲和①**：`third_party/ascend/backend/compiler.py`（`AscendBackend`）。删掉它，Ascend 无法注册编译阶段，但 NVIDIA/AMD 不受影响 → ascend。通过「自动发现」机制（`triton.backends`）被 core 加载。
- **亲和②**：`third_party/ascend/language/cann/libdevice.py`。删掉它，kernel 里 `cann.libdevice` 的数学函数失效，仅影响 Ascend → ascend。通过「挂载」机制变成 `triton.language.extra.cann`。
- **亲和③**：`third_party/ascend/lib/TritonToLinalg/`（一个 lowering pass）。删掉它，Ascend 缺这个转换，别的后端照常 → ascend。它是 `third_party/ascend/lib/` 下的纯亲和 C++ 代码。
- **无关④**：`python/triton/runtime/jit.py`（`@triton.jit`）。删掉它，所有后端的编译入口都没了 → core。它是典型的 target independent 能力。

> 完成后，建议把结论整理成你自己的「仓库导航表」——后续阅读源码时它会持续派上用场。

---

## 6. 本讲小结

- 仓库只有两条代码线：**Triton core**（目标无关）与 **Triton-Ascend**（目标亲和），判定标准是「删掉它会坏所有后端，还是只坏 Ascend」。
- core 的家园是 `python/`（Python）与顶层 `include/`、`lib/`（C++/MLIR）；ascend 的家园是 `third_party/ascend/`。
- `third_party/ascend/` 内部按 `language`（语言扩展）、`backend`（compiler+driver）、`include/lib`（MLIR pass）、`costmodel`、`tutorials/unittest` 分工，共约 14 个 pass 目录。
- 分层通过安装期的 **拷贝 / 挂载**（`setup.py`）把 ascend 的 `language` 挂到 `triton.language.extra`、把 `backend` 放进 `triton.backends`，再由 core 的 **自动发现** 机制加载。
- 分层不是绝对纯净：core 里保留了少数中性「扩展点钩子」（如 `_try_import_ascend`、`ascend_builder`），ascend 还用 **运行时 monkey patch** 改 core 行为——但改动代码体始终留在 ascend 子树。
- 记住一个易错点：`third_party/ascend/include` 与顶层 `include` 同名但完全不同，带 ascend 前缀的才是亲和代码。

---

## 7. 下一步学习建议

本讲让你掌握了「代码在哪、为什么在那」。接下来：

- **想真正跑起来**：进入 u1-l3《环境准备、安装与构建》，亲手完成一次 `pip install -e .`，验证本讲的「挂载」是否真的生成了符号链接。
- **想理解编译链路**：本讲提到的 `third_party/ascend/backend/compiler.py`（`AscendBackend`）是编译后端主入口，将在 u3-l2《AscendBackend：阶段注册与 NPUOptions》精读。
- **想理解运行时**：`third_party/ascend/backend/driver.py` 将在 u5-l1《NPUDriver 与 NPUUtils》展开。
- **想看语言扩展细节**：`cann/libdevice.py` 与 `cann/extension/` 将在 u7 单元（Ascend 语言扩展）系统讲解。

建议在进入下一讲前，先完成第 5 节的综合实践，建立你自己的「仓库导航表」。
