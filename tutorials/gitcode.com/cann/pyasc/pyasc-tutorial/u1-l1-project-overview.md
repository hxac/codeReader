# pyasc 是什么：项目背景与总体架构

## 1. 本讲目标

本讲是整套 pyasc 学习手册的第一讲，不要求任何前置知识。读完本讲，你应该能够：

1. 用一两句话向别人解释清楚 pyasc 解决的问题：**用 Python 标准语法编写昇腾 AI 处理器上的自定义算子，编译后在 NPU 上执行**。
2. 记住 pyasc 的**五大核心模块**：编译和运行模块、Python 前端模块、AST 转 ASC-IR 模块（这三个合称前端模块）；ASC-IR 定义模块、Ascend C 代码生成模块（这两个合称后端模块）。
3. 说出一次算子执行经历的 **3 种中间产物**：AST（Python 抽象语法树）、ASC-IR（基于 MLIR 的中间表示）、Ascend C 代码。
4. 了解 pyasc 与 Ascend C API、CANN、毕昇编译器之间的关系，以及运行 pyasc 需要的软硬件环境。

本讲不深入任何模块的内部实现——那是后续每一讲的职责。本讲只做一件事：**在你脑子里建立一张正确的全局地图**。

## 2. 前置知识

本讲面向零基础读者，但有几个名词最好先混个眼熟：

- **算子（Operator）**：深度学习框架中的最小计算单元，比如矩阵乘、加法、激活函数。框架自带的算子不满足需求时，开发者就需要编写"自定义算子"。
- **昇腾 AI 处理器（NPU）**：华为推出的 AI 加速芯片，pyasc 生成的代码最终运行在它上面。文档中常见的 910B/910C、Atlas A2/A3 都是指具体的芯片/产品型号。
- **Ascend C**：昇腾算子开发的原生 C++ 类库，提供 `LocalTensor`、`TPipe`、`asc.data_copy` 背后对应的 C++ 接口。你可以把 Ascend C 理解为"昇腾算子的标准编程语言"，而 pyasc 是它的 Python 外衣。
- **CANN**（Compute Architecture for Neural Networks）：昇腾计算架构的软件栈总称，包含驱动、运行时、编译工具（毕昇编译器）等。pyasc 依赖 CANN 包提供的环境。
- **毕昇编译器**：CANN 中的编译器，负责把 C++/Ascend C 源码编译成 NPU 可执行的 Kernel 二进制。
- **MLIR**（Multi-Level Intermediate Representation）：LLVM 社区的多层中间表示框架。pyasc 后端的 ASC-IR 就是基于 MLIR 定义的 Dialect（方言）。本讲只需要知道"MLIR 是一种描述中间代码的框架"即可。
- **JIT**（Just-In-Time，即时编译）：程序运行时才触发编译，而不是提前编译好。pyasc 用 `@asc.jit` 装饰器拉起整个编译流程。

## 3. 本讲源码地图

本讲主要阅读文档类文件，辅以一个示例建立直观感受：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/README.md) | 项目门面：定位、目录结构、快速入门入口、软硬件配套要求 |
| [docs/architecture_introduction.md](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/architecture_introduction.md) | 官方架构文档：五大核心模块的划分、目录对照、各模块功能与约束 |
| [examples/01_add/add.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py) | 最简单的端到端示例：一个向量加法算子，本讲用来直观感受"pyasc 代码长什么样" |

## 4. 核心概念与源码讲解

### 4.1 README 概述：pyasc 是什么

#### 4.1.1 概念说明

一句话定义（来自 README）：

> pyasc 是一种用于编写高效自定义算子的编程语言，原生支持 Python 标准规范。基于 pyasc 编写的算子程序，通过编译器编译和运行时调度，运行在昇腾 AI 处理器上。

拆开来看，这句话包含三层信息：

1. **它是一门"编程语言"的形态，而不是一个普通 Python 库**。你用 Python 语法写算子，但这些代码不会被 CPython 直接执行，而是被 pyasc 的编译器"翻译"成 Ascend C 代码，再编译成 NPU 上的 Kernel。
2. **"原生支持 Python 标准规范"** 意味着你写的就是普通 Python 函数（有 `for`、有类型标注），但只有 pyasc 支持的子集能被翻译（语法支持边界会在第 4 单元详细讲）。
3. **pyasc 编程接口与 Ascend C 类库接口一一对应**。也就是说，`asc.add` 对应 Ascend C 的 `Add`，`asc.TPipe` 对应 Ascend C 的 `TPipe`——学会了 Ascend C 的概念，就等于学会了 pyasc 的接口地图。

为什么要做 pyasc？直觉上的答案：C++ 写算子门槛高、迭代慢；Python 写算子更贴近算法研究者的习惯，配合 JIT 可以"改一行、跑一次"。pyasc 把性能关键的部分交给编译器，把表达留给 Python。

#### 4.1.2 核心流程

先给一个最粗粒度的全局流程，帮助理解 README 中"编译器编译 + 运行时调度"两个词：

```text
开发者编写 Python 算子（@asc.jit 修饰的函数）
        │
        ▼  触发 JIT 编译
编译器：Python 源码 → AST → ASC-IR → Ascend C 代码 → 毕昇编译 → Kernel 二进制
        │
        ▼  运行时调度
运行时：加载 Kernel 二进制，下发到 NPU 的多个核上执行
```

其中"编译器"和"运行时"就是五大核心模块中"编译和运行模块"的两大职责，第 4.2 节会展开。

#### 4.1.3 源码精读

**（1）项目定位原文**。README 概述段明确了 pyasc 的定位、接口对应关系和支持的处理器型号：

[README.md:L7-L10](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/README.md#L7-L10)

这一段有三个关键句：①"原生支持 python 标准规范"；②"编程接口与 Ascend C 类库接口一一对应"；③"支持的 AI 处理器包括 Ascend 910C、Ascend 910B"。第三点直接决定了你手上硬件能不能跑 pyasc。

**（2）仓库目录结构**。README 给出了顶层目录的职责注释：

[README.md:L12-L35](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/README.md#L12-L35)

先记住四个最重要的顶层目录，后面反复用到：

- `python/` —— Python 前端代码，其中 `python/asc` 是用户可见的 Python 包；
- `include/` 与 `lib/` —— 后端的头文件/td 定义与 C++ 实现（MLIR 方言、Pass、代码生成）；
- `examples/` —— 8 个端到端算子样例（`01_add` 到 `08_rmsnorm`），是学习接口最直观的素材；
- `bin/` —— `ascir-opt` 等开发者工具源码。

**（3）一个真实的 pyasc 算子长什么样**。看 `examples/01_add/add.py` 中被 `@asc.jit` 修饰的核函数：

[examples/01_add/add.py:L28-L29](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L28-L29)

`@asc.jit` 是整个 pyasc 的入口装饰器：它把一个普通 Python 函数标记为"Device 侧函数"，被调用时不再走 CPython 解释执行，而是触发第 4.1.2 节那条编译-执行流水线。函数体内的 `asc.get_block_idx()`、`asc.GlobalTensor()`、`asc.data_copy(...)` 都是 Python 前端模块提供的接口（对应 Ascend C 同名能力）。

再看 Host 侧如何"发射"这个核函数：

[examples/01_add/add.py:L72-L79](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L72-L79)

第 78 行的 `vadd_kernel[USE_CORE_NUM, rt.current_stream()](x, y, z, block_length)` 是 pyasc 特有的调用语法：**中括号传运行时配置（用几个核、放在哪条流），小括号传算子参数**。这是"编译参数走装饰器、运行时配置走中括号"设计的直观体现。

#### 4.1.4 代码实践

**实践目标**：不运行任何代码，仅通过"读"建立对 pyasc 代码形态的手感，并验证"接口与 Ascend C 一一对应"这句话。

**操作步骤**：

1. 打开 [examples/01_add/add.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py)，通读一遍（总共 109 行，其中一半是注释和 main 函数）。
2. 列出核函数 `vadd_kernel` 中出现的所有 `asc.` 前缀调用，做成一张表。
3. 打开 [docs/python-api/index.md 所在目录](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/python-api)（API 列表文档），在你列出的每个 API 后面补充它的文档位置。

**需要观察的现象**：你会发现 `asc.add`、`asc.data_copy`、`asc.set_flag`/`asc.wait_flag` 这些名字与 Ascend C 官方文档中的 API 名几乎逐字对应（大小写风格不同：pyasc 用 snake_case，Ascend C 用 PascalCase）。

**预期结果**：得到一张 5~8 行的"示例 API 对照表"，例如：

| pyasc 接口 | 出现位置 | 直觉含义 |
| --- | --- | --- |
| `asc.get_block_idx()` | add.py:31 | 当前是第几个核（多核切分用） |
| `asc.GlobalTensor()` | add.py:32 | 全局内存（GM）上的张量 |
| `asc.data_copy(dst, src, len)` | add.py:53 | 在 GM 与本地内存间搬运数据 |
| `asc.add(z, x, y, len)` | add.py:60 | 向量逐元素加法 |

本实践为纯阅读型，无需运行环境，结果"待本地验证"仅指表格内容由你亲手核对。

#### 4.1.5 小练习与答案

**练习 1**：pyasc 是"Python 库"还是"编程语言"？用 README 原文支持你的判断。

<details>
<summary>参考答案</summary>

更接近"编程语言 + 编译器 + 运行时"的组合。README 概述明确说 pyasc 是"用于编写高效自定义算子的编程语言，原生支持 python 标准规范"，其代码经"编译器编译和运行时调度"运行在昇腾处理器上——普通 Python 库的代码由解释器直接执行，而 pyasc 代码会被翻译成 Ascend C 再编译成 NPU Kernel。当然，它的载体是一个 pip 安装的 Python 包（`import asc`），所以形式上两者兼具。
</details>

**练习 2**：`vadd_kernel[8, stream](x, y, z, n)` 中，中括号和小括号分别传什么？

<details>
<summary>参考答案</summary>

中括号 `[8, stream]` 传运行时配置：核数（core_num）和执行流（stream）；小括号传 Kernel 函数的输入输出参数。依据是架构文档中"开发者定义 Kernel 函数时通过修饰器 @asc.jit 的小括号 () 传入编译参数，执行 Kernel 函数时通过中括号 [] 传入运行时配置（核数和 Stream）"。
</details>

**练习 3**：如果你的机器是 Ascend 910A，能直接运行 pyasc 吗？

<details>
<summary>参考答案</summary>

按 README 概述，pyasc 当前支持的 AI 处理器为 Ascend 910C 和 Ascend 910B（配套文档中的 Atlas A2/A3 产品）。910A 不在支持列表中，不建议直接使用；但 pyasc 提供 Model（仿真器）后端，`add.py` 支持 `-r Model` 模式，可以在无 NPU 的机器上做编译链路验证（详见第 4 单元及 u1-l4）。
</details>

### 4.2 五大核心模块：从 Python 源码到 NPU 执行的流水线

#### 4.2.1 概念说明

架构文档把 pyasc 划分为五个核心模块：

| 模块 | 归属 | 一句话职责 | 代码所在目录 |
| --- | --- | --- | --- |
| 编译和运行模块 | 前端 | JIT 拉起、调度编译、下发执行 | `python/asc/runtime` |
| Python 前端模块 | 前端 | 提供与 Ascend C 一一对应的 Python 编程接口 | `python/asc/language` |
| AST 转 ASC-IR 模块 | 前端 | 遍历 Python 语法树，生成 MLIR 形式的 ASC-IR | `python/asc/codegen` |
| ASC-IR 定义模块 | 后端 | 基于 MLIR Dialect 定义 Type/Attribute/Interfaces/Operation | `include/ascir` + `lib/Dialect` |
| Ascend C 代码生成模块 | 后端 | 把 ASC-IR 翻译成 Ascend C 代码 | `include/ascir/Target` + `lib/Target` |

三个模块为什么这样分？直觉理解：

- **Python 前端模块**解决"用户怎么写"——`asc.float32`、`asc.TPipe`、`asc.add` 这些名字都从这里来，它尽量与 Ascend C 保持一致，降低迁移成本。
- **AST 转 ASC-IR 模块**解决"怎么读懂 Python"——Python 函数先被解析成 AST，再由遍历器逐节点"抄写"成 IR。这是任何 Python-to-X 编译器都必经的一步。
- **ASC-IR 定义模块**解决"中间语言长什么样"——为什么不直接从 AST 生成 C 代码？因为中间需要一层结构化表示来做优化（Pass）和统一发射。MLIR 的 Dialect 机制正好提供了这套基础设施。
- **Ascend C 代码生成模块**解决"怎么落地成 C 代码"——每个 IR 操作映射为一条 Ascend C 调用，拼出最终 `.cpp` 文件。
- **编译和运行模块**是总指挥：按次序调用上面各模块，再用毕昇编译器生成二进制，最后把 Kernel 下发到 NPU。

#### 4.2.2 核心流程

把五个模块串成一条数据流（这也是本讲综合实践要你亲手画出的图）：

```text
【Python 前端模块】 用户在此编写
   Python 算子源码（@asc.jit 函数，import asc 使用其接口）
        │  装饰/调用时由 inspect + ast 模块抓取源码并解析
        ▼
   中间产物 ①：AST（Python 抽象语法树）
        │
        │ 【AST 转 ASC-IR 模块】 function_visitor.py 遍历 AST 节点
        │ 【ASC-IR 定义模块】    提供 asc.xxx Operation/Type 的定义（MLIR Dialect）
        ▼
   中间产物 ②：ASC-IR（MLIR 文本可打印为 .mlir）
        │  运行若干优化/变换 Pass
        │ 【Ascend C 代码生成模块】 IR 操作逐条映射为 Ascend C 语句
        ▼
   中间产物 ③：Ascend C 代码（.cpp 文本）
        │  【编译和运行模块】组装编译命令，调用毕昇编译器
        ▼
   Kernel 二进制（.o）
        │  【编译和运行模块】运行时通过 acl runtime 加载、下发
        ▼
   昇腾 NPU 多核执行，结果拷回 Host
```

两个值得注意的设计点：

1. **三种中间产物均可导出查看**。架构文档说明：设置环境变量 `PYASC_DUMP_PATH` 后，编译过程生成的 ASC-IR 与 Ascend C 代码文件会保存到该路径。这是后续所有讲义调试手段的基础。
2. **编译与运行分离**。编译产物（Kernel 二进制）可被 JIT 缓存复用；运行时只需解析中括号里的配置（核数、流）和参数即可执行。架构文档还提到缓存目录可通过 `PYASC_HOME`、`PYASC_CACHE_DIR` 定制，`always_compile=True` 可强制重编。

#### 4.2.3 源码精读

**（1）五大模块的官方划分**。架构文档"核心模块说明"一节给出了权威定义：

[docs/architecture_introduction.md:L8-L10](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/architecture_introduction.md#L8-L10)

原文明确："编译和运行模块、Python 前端模块、AST 转 ASC-IR 模块，可以统称为前端模块；ASC-IR 定义模块、Ascend C 代码生成模块，可以统称为后端模块"。前端是 Python 世界，后端是 C++/MLIR 世界，两者通过 pybind11 桥接。

**（2）前端目录结构**。前端三个模块在 `python/asc` 下的落位：

[docs/architecture_introduction.md:L12-L37](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/architecture_introduction.md#L12-L37)

按注释对应：`runtime/` = 编译和运行模块；`language/`（下分 `adv`/`basic`/`core`/`fwk`）= Python 前端模块；`codegen/` = AST 转 ASC-IR 模块；`lib/` 是对 C++ 侧库的 Python 封装（`lib/host` 对应 Ascend C Host 侧接口、`lib/runtime` 对应 acl runtime 接口）。

**（3）后端目录结构**。后端两个模块在 `include/ascir` 与 `lib` 下的落位：

[docs/architecture_introduction.md:L39-L76](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/architecture_introduction.md#L39-L76)

`include/ascir/Dialect/Asc` 及其 `lib/Dialect/Asc` 对应 ASC-IR 定义模块；`include/ascir/Target/Asc` 与 `lib/Target/AscendC` 对应 Ascend C 代码生成模块。注意 lib 目录与 include 目录一一镜像，这是典型的"声明与实现分离"C++ 项目布局。

**（4）编译和运行模块的职责细则**。这是五个模块中的"总指挥"，文档描述最详细：

[docs/architecture_introduction.md:L79-L90](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/architecture_introduction.md#L79-L90)

关键信息点：

- L82：JIT 机制由 `@asc.jit` 装饰器拉起；
- L83：**核函数（Kernel）** 是"由 Host 侧发起调用、且被 @asc.jit 修饰的函数"，其余被修饰的称为 **Device 侧执行函数**——这对概念贯穿全手册；
- L84：Device 侧执行函数传给 jit 的编译参数无效，仅 Kernel 函数生效；
- L85：`PYASC_DUMP_PATH` 导出中间产物；JIT 缓存的失效因素包括编译选项、Kernel 参数、全局变量、Kernel 函数代码；
- L87：运行模块负责加载二进制、执行，并自动完成 Host/Device 数据拷贝，还支持 msprof 采集性能数据。

**（5）编译参数与运行时配置清单**。编译和运行模块暴露给用户的全部"旋钮"：

[docs/architecture_introduction.md:L92-L110](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/architecture_introduction.md#L92-L110)

编译参数共 6 个（`kernel_type`/`opt_level`/`auto_sync`/`auto_sync_log`/`matmul_cube_only`/`always_compile`），运行时配置 2 个（`core_num` 必选、`stream` 可选）。初学阶段只需要记住 `kernel_type` 和 `always_compile`。

**（6）AST 转 ASC-IR 模块与 ASC-IR 定义模块的关键描述**：

[docs/architecture_introduction.md:L185-L196](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/architecture_introduction.md#L185-L196)

这一段说明了模块输入（Python 源码及 AST、参数、变量、配置）、核心机制（按 AST 节点类型分派处理）、以及两个重要工程手段：用 **pybind11** 把 C++ 侧 IR 创建接口暴露给 Python，用 **TableGen** 自动生成这些绑定（否则上千个 API 的绑定手写不现实）。L196 直接点名模块入口文件为 `python/asc/codegen/function_visitor.py`——第 4 单元将逐行精读它。

[docs/architecture_introduction.md:L217-L245](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/architecture_introduction.md#L217-L245)

ASC-IR 定义模块的设计目标（L221-L223）：①1:1 映射 Ascend C API；②可直接由 Python 代码通过 JIT 编译生成 Ascend C 代码。ASC Dialect 由 **Type、Attribute、Interfaces、Operation** 四部分构成（L225-L233）。Operation 命名规则（L245）为 `Dialect_类名_成员函数`，例如后面会遇到的 `asc.Add`。

**（7）Ascend C 代码生成模块**：

[docs/architecture_introduction.md:L247-L254](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/architecture_introduction.md#L247-L254)

职责单一而明确：解析 ASC-IR 中的各种结构，映射为等价的 Ascend C 语法结构（基础 API、高阶 API）。

#### 4.2.4 代码实践

**实践目标**：用源码目录"验证"五大模块的存在——架构文档不是空谈，每个模块都有真实代码落位。

**操作步骤**：

1. 在仓库根目录执行 `ls python/asc`，确认 `runtime`、`language`、`codegen`、`lib` 四个子目录存在，分别对应表中哪个模块。
2. 执行 `ls python/asc/language`，确认 `adv`、`basic`、`core`、`fwk` 四个子目录（Python 前端模块的四类接口）。
3. 执行 `ls include/ascir/Dialect/Asc include/ascir/Target`，确认后端两模块的目录存在（`Dialect/Asc` 下有 `IR`、`Transforms`；`Target/Asc` 下有 `Adv`、`Basic`、`Core`、`External`、`Fwk`）。
4. 打开 [python/asc/codegen/function_visitor.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py) 只看前 50 行，确认它 `import ast`（Python 标准库的 AST 模块）——这正是"AST 转 ASC-IR"名字的由来。

**需要观察的现象**：目录结构与架构文档 L12-L76 的两棵目录树逐层吻合；`function_visitor.py` 中能看到 `ast.NodeVisitor` 或类似 AST 相关引用。

**预期结果**：得到一张"模块 → 目录 → 我亲眼确认"的核对清单。若某目录与文档不符（例如文档滞后于代码），以实际目录为准并记录差异。本实践仅需文件浏览权限，无需构建环境，可在任意克隆出的仓库上完成。

#### 4.2.5 小练习与答案

**练习 1**：把五大模块按"前端/后端"分组，并各写一句职责。

<details>
<summary>参考答案</summary>

前端模块：①Python 前端模块——提供与 Ascend C 一一对应的 Python 接口（`python/asc/language`）；②AST 转 ASC-IR 模块——遍历 Python AST 生成 ASC-IR（`python/asc/codegen`）；③编译和运行模块——JIT 拉起、调度编译、下发执行（`python/asc/runtime`）。后端模块：④ASC-IR 定义模块——基于 MLIR Dialect 定义 Type/Attribute/Interfaces/Operation（`include/ascir/Dialect` + `lib/Dialect`）；⑤Ascend C 代码生成模块——把 ASC-IR 翻译成 Ascend C 代码（`include/ascir/Target` + `lib/Target`）。
</details>

**练习 2**：为什么 pyasc 不直接把 Python AST 翻译成 Ascend C，而要经过 ASC-IR 这一层？

<details>
<summary>参考答案</summary>

（开放题，结合文档合理作答即可。）ASC-IR 是一层结构化的中间表示，带来三个好处：①可以在 IR 上做统一优化与变换（如同步自动插入、内存分配提升，见第 6 单元 Pass）；②ASC-IR 1:1 映射 Ascend C API，使"Python 接口 → IR → C 代码"两端职责解耦，新增 API 只需定义一次 Operation 即可同时获得 IR 构建和代码发射能力；③借助 MLIR/TableGen 基础设施自动生成大量样板代码（pybind 绑定、发射声明）。没有中间层的话，每条 Python 语法都要直接处理 C 代码生成细节，无法做全局优化。
</details>

**练习 3**：`PYASC_DUMP_PATH` 能导出哪些文件？它们分别对应流水线中的哪个中间产物？

<details>
<summary>参考答案</summary>

按架构文档"编译和运行模块"一节（L85），设置后可在编译完成后到该路径查看生成的 ASC-IR 文件（如 `codegen.mlir`、`ascir.mlir`，对应中间产物 ②）和 Ascend C 代码文件（`ascendc.cpp`，对应中间产物 ③）。AST（中间产物 ①）是 Python 标准库解析的内存对象，通常不单独落盘，但它是 codegen 模块的输入。
</details>

### 4.3 软硬件配套要求：跑通 pyasc 需要什么环境

#### 4.3.1 概念说明

pyasc 是"上层语言 + 底层硬件"的组合，环境要求自然分三层：

1. **硬件层**：一块受支持的昇腾产品（Atlas A2/A3 训练/推理产品，对应 910B/910C 系列芯片）；没有 NPU 时可用 Model（仿真器）模式走通编译与模拟执行链路。
2. **系统层**：`aarch64` 或 `x86_64` CPU 架构 + 受支持的 Linux 发行版；Python 3.9~3.12。
3. **软件栈层**：正确版本的 CANN 社区版包。**pyasc 版本与 CANN 版本有严格对应关系**——这是新手最常见的坑：CANN 太新或太旧都可能导致编译失败。

三者关系可理解为：pyasc（语言）→ CANN/毕昇编译器（工具链）→ 驱动 → 昇腾 NPU（硬件）。每一层都向下依赖。

#### 4.3.2 核心流程

环境准备的决策流程：

```text
手头有 Atlas A2/A3 硬件？
├── 有 → 安装匹配版本的 CANN 社区包 → 按硬件选 CPU 架构对应包 → NPU 模式运行
└── 没有 → 两种选择：
      ├── CANN Docker 镜像 / CPU 服务器 + Model 仿真模式（可完整验证编译链路）
      └── 仅阅读源码 + 本教程的源码阅读型实践
```

选 CANN 版本时，先确定你安装的 pyasc 版本，再反查 README 支持表（见 4.3.3）。

#### 4.3.3 源码精读

**（1）软硬件环境依赖清单**：

[README.md:L57-L66](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/README.md#L57-L66)

原文列出：昇腾产品（Atlas A2/A3 训练推理产品）、CPU 架构（`aarch64`/`x86_64`）、Linux 系统、Python 3.9-3.12。注意 Python 上限是 3.12——用 3.13 的机器装 pyasc 可能直接失败。

**（2）pyasc 版本与 CANN 版本对应表**：

[README.md:L67-L96](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/README.md#L67-L96)

以表格末行为例：pyasc v1.0.0 需要社区版 CANN 8.5.0.alpha001 或 8.5.0.alpha002；v1.1.0/v1.1.1 则要求 8.5.0.alpha001 **及以上**。两代 pyasc 都支持 Atlas A2 与 A3 产品。

**（3）快速入门与学习文档入口**。README 汇总了全部官方文档的入口，本手册后续各讲会按需引用其中几篇：

[README.md:L38-L55](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/README.md#L38-L55)

对学习路线最有用的四篇：`quick_start.md`（构建安装，u1-l2 展开）、`examples/README.md`（样例执行，u1-l4 展开）、`architecture_introduction.md`（本讲主角）、`developer_guide.md`（二次开发，u7-l6 展开）。

#### 4.3.4 代码实践

**实践目标**：为你的机器制定一份"环境可行性判断书"，并锁定 CANN 版本号。

**操作步骤**：

1. 在终端执行 `uname -m` 与 `python3 --version`，记录 CPU 架构与 Python 版本。
2. 对照 README L57-L66 的清单，逐项勾选你的机器是否满足（架构、系统、Python 3.9-3.12）。
3. 执行 `ls /usr/local/Ascend 2>/dev/null || echo "no CANN"`（CANN 默认安装位置）判断是否已装 CANN；若已安装，用 `cat /usr/local/ascend/latest/version.cfg 2>/dev/null || cat /usr/local/Ascend/latest/*/version.info 2>/dev/null` 查看版本（不同 CANN 版本文件名不同，找不到时以 `ls /usr/local/Ascend` 的目录名为线索）。
4. 按你的 pyasc 目标版本，从 README L67-L96 的表格中查出所需 CANN 版本，写入判断书。
5. 若无 NPU，明确写下"将采用 Model 模式"并注明该模式同样需要 CANN 环境（详见 u1-l2 的三种环境准备方式）。

**需要观察的现象**：每一步命令的真实输出；特别留意 Python 版本是否越界、CANN 是否存在及版本号。

**预期结果**：一份类似下面的判断书——

| 检查项 | 要求 | 本机结果 | 结论 |
| --- | --- | --- | --- |
| CPU 架构 | aarch64 / x86_64 | x86_64 | ✅ |
| Python | 3.9-3.12 | 3.10.12 | ✅ |
| CANN | 8.5.0.alpha001+（以 v1.1.x 为例） | 未安装 | ⚠️ 需按 u1-l2 安装 |
| NPU | Atlas A2/A3 | 无 | 用 Model 模式 |

命令输出依赖本机环境，表中"本机结果"一列需你本地实际执行后填写。

#### 4.3.5 小练习与答案

**练习 1**：一台 x86_64 + Python 3.13 + 已装 CANN 8.5.0.alpha002 的机器，能顺利使用 pyasc 吗？

<details>
<summary>参考答案</summary>

大概率不能。x86_64 架构满足要求，CANN 版本也满足 v1.0.0/v1.1.x 的要求，但 Python 3.13 超出 README 明示的 3.9-3.12 范围，属于不受支持的运行环境。应降级到 3.12 及以下（可用 conda/pyenv 建独立环境）。
</details>

**练习 2**：没有昇腾硬件还能体验 pyasc 吗？依据是什么？

<details>
<summary>参考答案</summary>

可以。依据有二：①`examples/01_add/add.py` 的 `-r` 参数支持 `Model` 与 `NPU` 两种后端（add.py:L94-L101），Model 即仿真器模式，且代码在 Model 模式下会把张量放在 `cpu` 设备（add.py:L84）；②编译链路（AST→ASC-IR→Ascend C 代码）本身与硬件无关，配合 `PYASC_DUMP_PATH` 可以在无 NPU 环境完整观察三种中间产物。具体安装方式见 u1-l2。
</details>

## 5. 综合实践

**任务：绘制"从 Python 算子源码到 NPU 执行"的数据流图**（本讲规格指定的核心实践，纯文档阅读型，无需运行环境）。

**步骤**：

1. 重读 [README.md:L7-L10](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/README.md#L7-L10) 与 [docs/architecture_introduction.md:L8-L10](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/architecture_introduction.md#L8-L10)，确认五模块名称与前后端归属。
2. 以 [examples/01_add/add.py:L28-L78](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L28-L78) 为具体素材：假设 `vadd_launch` 里的 `vadd_kernel[8, stream](x, y, z, block_length)` 被执行，追踪这次调用触发的完整流程。
3. 画图（纸笔或 Mermaid 均可），要求图上必须出现：
   - **5 个核心模块**作为加工节点：Python 前端模块（用户在其上编写）、AST 转 ASC-IR 模块、ASC-IR 定义模块（为前者提供 IR 定义）、Ascend C 代码生成模块、编译和运行模块（串联全部并负责执行）；
   - **3 种中间产物**作为数据边上的标注：AST、ASC-IR、Ascend C 代码；
   - 两个"端点"：Python 算子源码（输入）、NPU 上执行（输出）；
   - 毕昇编译器与 Kernel 二进制、aclrt 运行时这两个辅助角色。
4. 在图旁为每条边标注依据（架构文档行号），确保每个箭头都能在文档中找到出处。
5. 自查：随便指着图上一个模块，能否说出它的代码目录与一句话职责？说不出来就回到 4.2.3 重新核对。

**预期结果示例**（文字版数据流，可直接对照你的图）：

```text
Python 算子源码（add.py 中的 vadd_kernel）
  → [Python 前端模块：提供 asc.* 接口语义]
  → AST（inspect 抓源码，ast 解析）
  → [AST 转 ASC-IR 模块 function_visitor.py + ASC-IR 定义模块 提供的 asc.* Operation]
  → ASC-IR（.mlir，PYASC_DUMP_PATH 可导出）
  → [Ascend C 代码生成模块：IR 逐操作映射]
  → Ascend C 代码（ascendc.cpp，PYASC_DUMP_PATH 可导出）
  → [编译和运行模块：组装命令 → 毕昇编译器]
  → Kernel 二进制（.o）
  → [编译和运行模块：aclrt 加载下发，Host/Device 自动拷贝]
  → NPU 多核执行
```

**待本地验证**：若你已完成 u1-l2 的环境搭建，可加做一步——设置 `PYASC_DUMP_PATH` 运行 `01_add`，确认路径下真的出现 `.mlir` 与 `ascendc.cpp` 文件，与图中"中间产物 ②③"对应。

## 6. 本讲小结

- **pyasc 是一门用 Python 标准语法编写昇腾自定义算子的编程语言**：代码不由 CPython 执行，而是经编译器翻译成 Ascend C、再由毕昇编译器编成 NPU Kernel；其接口与 Ascend C 类库一一对应，当前支持 Ascend 910B/910C。
- **五大核心模块分前后端**：前端 = 编译和运行模块（`python/asc/runtime`）+ Python 前端模块（`python/asc/language`）+ AST 转 ASC-IR 模块（`python/asc/codegen`）；后端 = ASC-IR 定义模块（`include/ascir/Dialect` + `lib/Dialect`）+ Ascend C 代码生成模块（`include/ascir/Target` + `lib/Target`）。
- **一条数据流三种中间产物**：Python 源码 → AST → ASC-IR（MLIR Dialect）→ Ascend C 代码 → Kernel 二进制 → NPU 执行；`PYASC_DUMP_PATH` 可导出中间文件供学习调试。
- **调用语法分两处**：`@asc.jit(...)` 小括号传编译参数（如 `kernel_type`、`always_compile`），`kernel[core_num, stream](...)` 中括号传运行时配置；Device 侧执行函数的 jit 编译参数无效。
- **环境三层约束**：硬件（Atlas A2/A3）、系统（aarch64/x86_64 Linux、Python 3.9-3.12）、工具链（CANN 版本须与 pyasc 版本匹配，见 README 对应表）；无 NPU 可先用 Model 仿真模式。
- **后端的两大工程支柱**是 pybind11（把 C++ IR 接口暴露给 Python）与 TableGen（自动生成绑定与发射声明），它们是理解后端代码量的钥匙。

## 7. 下一步学习建议

下一讲 **u1-l2《环境搭建与源码构建：setup.py 驱动的 CMake 构建》** 将把本讲的"软件栈层"落到实处：安装 CANN 包与 LLVM 预编译包，解析 `setup.py` 如何拉起 CMake+Ninja 构建 `libpyasc` 扩展模块，并完成 `pip install -e .` 验证。

在此之前，建议你做两件轻量准备：

1. 通读 [docs/quick_start.md](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/quick_start.md)，对三种环境准备方式（云环境、Docker 镜像、手动安装 CANN）有印象。
2. 浏览 [examples/README.md](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/README.md)，了解 8 个样例各自演示什么——u1-l4 将运行其中的 `01_add`。

若你想提前建立对编译流水线的代码级直觉，可以粗读 [python/asc/runtime/jit.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py) 中的 `_run` 方法——它就是本讲数据流图在代码中的"总指挥"实现，u1-l5 会逐行走读。
