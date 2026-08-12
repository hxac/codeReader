# asc-tools 项目定位与工具全景

## 1. 本讲目标

本讲是整个 asc-tools 学习手册的第一篇。读完本讲，你应当能够：

1. 说清楚 **asc-tools 是什么**、它服务于谁、解决什么问题。
2. 区分 **cpu debug、npu check、msobjdump、show_kernel_debug_data、optype_collector** 这五个工具各自的功能边界与使用场景。
3. 在脑海中建立一条 **「算子源码 → CPU 调测 → 离线分析」** 的工具链认知，知道每个工具在算子开发流程中的哪个环节出场。

本讲不要求你已经写过任何 Ascend C 代码，也不要求你手头有 NPU 设备。我们会从最基础的概念讲起。

---

## 2. 前置知识

在进入源码之前，先用大白话把几个反复出现的术语解释清楚：

- **CANN**：全称 *Compute Architecture for Neural Networks*，是华为昇腾（Ascend）的异构计算架构软件栈。可以把 CANN 理解成「让程序能在昇腾 NPU 上跑起来的整套软件」。
- **NPU**：神经网络处理单元（Neural Processing Unit），即昇腾 AI 处理器，负责执行神经网络相关的高密度计算。
- **算子（Operator / Op）**：神经网络里的一个计算单元，比如矩阵乘、加法、激活函数。一个完整的 AI 模型由成百上千个算子组成。
- **Ascend C**：昇腾推出的算子开发编程语言，开发者用它编写运行在 NPU 上的算子 Kernel 代码。
- **Kernel（核函数）**：算子里真正在硬件核上执行的那段代码，是开发者写、硬件跑的核心部分。
- **CPU 域 / NPU 域**：「域」指运行环境。CPU 域指在普通电脑 CPU 上运行；NPU 域指在真实昇腾 NPU 上运行。一个算子最终要部署到 NPU，但在开发阶段，能先在 CPU 上跑通会极大提升调试效率。

理解了这些，你就能看懂下面这句话的含义：**asc-tools 是 CANN 为 Ascend C 配套提供的一套调试工具，让开发者写完算子 Kernel 后，能更快地定位实现中的问题。**

> 提示：如果你手头没有 NPU 设备，完全不影响本讲的学习。本讲只读文档和源码，不依赖硬件。

---

## 3. 本讲源码地图

本讲是「全景篇」，不会深入任何工具的内部实现，只读取项目最顶层的说明文档与目录结构。涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目中文说明，包含概述、目录结构、文档索引。本讲的主要信息来源。 |
| `README_en.md` | 项目英文说明，内容与中文版对应，方便对照术语。 |
| `docs/00_quick_start.md` | 快速入门文档，描述环境准备、编译、安装、验证全流程，是「工具链整体定位」的依据。 |

> 说明：永久链接基准地址为
> `https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/`
> 本讲所有源码引用都基于该 HEAD 提交。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**项目概述**、**五大工具功能划分**、**工具链整体定位**。

### 4.1 项目概述

#### 4.1.1 概念说明

asc-tools 的全称是 **Ascend C Tools**，它是 CANN 基于 Ascend C 编程语言推出的**配套调试工具**。关键词是「配套」——它不是用来写算子的，而是**算子写完之后，用来帮助调试、验证、分析的**。

可以打个比方：Ascend C 是「画图纸的笔」，算子源码是「画好的图纸」，而 asc-tools 是「放大镜 + 测量仪 + 验收清单」，帮你在图纸正式交付给工厂（NPU）之前，先把错误找出来。

#### 4.1.2 核心流程

从开发者视角，使用 asc-tools 的典型流程是：

1. **写算子**：用 Ascend C 编写算子 Kernel 源码。
2. **CPU 上调测**：借助 asc-tools 的 cpu debug 能力，先在 CPU 上把算子跑通、做功能与精度验证。
3. **离线分析**：算子编译产物（ELF 文件、调试 bin 文件）用 msobjdump、show_kernel_debug_data 等工具做离线解析。
4. **上线交付**：确认算子实现没有问题后，再部署到真实 NPU。

这套流程的核心价值是：**把大量在 NPU 上才能发现的问题，前移到 CPU 上和离线阶段解决**，从而大幅缩短算子开发周期。

#### 4.1.3 源码精读

README 的概述部分直接点明了项目定位：

> Ascend C Tools 是 CANN 基于 Ascend C 编程语言推出的配套调试工具。借助 Ascend C Tools，开发者可以进行 CPU 域孪生调试、解析算子调测信息以及文件信息，从而快速定位算子实现中可能存在的问题。

参见 [README.md:L3-L5](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/README.md#L3-L5)，这段代码对应的是 `## 🚀概述` 标题及其下方的项目定位说明。

英文版对「孪生调试」用了更直白的表述：*CPU-domain twin debugging*（CPU 域孪生调试），即「在 CPU 上造一个 NPU 的孪生体来跑算子」，参见 [README_en.md:L3-L5](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/README_en.md#L3-L5)。

> 注意「孪生调试」这个概念会在后续讲义反复出现，先记住它的直觉含义：**用 CPU 模拟 NPU 的行为来做调试**。

#### 4.1.4 代码实践

**实践目标**：通过阅读官方一句话定义，建立对项目价值的准确表述。

**操作步骤**：

1. 打开项目根目录的 `README.md`。
2. 定位到 `## 🚀概述` 一节。
3. 用你自己的话，写一句不超过 30 字的中文，概括 asc-tools 的价值。

**需要观察的现象**：你会注意到概述里出现的动词——「CPU 域孪生调试」「解析算子调测信息」「解析文件信息」。这三个动词恰好对应了工具的三大类能力。

**预期结果**：你应能写出类似「asc-tools 让 Ascend C 算子在部署到 NPU 前，先在 CPU 上调测、并离线解析产物」这样的句子。

#### 4.1.5 小练习与答案

**练习 1**：asc-tools 和 Ascend C 是什么关系？

> **参考答案**：Ascend C 是编写算子的**编程语言**，asc-tools 是为该语言**配套的调试工具集**。前者负责「写」，后者负责「查」。

**练习 2**：「孪生调试」中「孪生」二字大致指什么？

> **参考答案**：指在 CPU 上构造一个与 NPU 行为对应的「孪生体」来运行和调试算子，从而无需真实 NPU 即可发现算子实现问题。

---

### 4.2 五大工具功能划分

#### 4.2.1 概念说明

asc-tools 包含五个工具。其中 README 概述部分明确列出了前四个（cpu debug、npu check、msobjdump、show_kernel_debug_data），它们是**调试与分析类工具**；第五个 `optype_collector` 位于 `utils/` 目录下、有独立的文档 `docs/05_optype_collector.md`，是**算子信息采集与冲突检测工具**。把这五个合起来，才是完整的工具全景。

下面逐一说明。先看一张总览表：

| 工具 | 一句话用途 | 运行域 | 主要产物/输入 |
| --- | --- | --- | --- |
| **cpu debug** | 让 Ascend C 源码用 GCC 编译在 CPU 上跑，做功能/精度验证与 gdb 调试 | CPU 域 | CPU 域可执行程序 |
| **npu check** | 在 CPU 域执行时检查算子内存/同步等实现逻辑是否合法 | CPU 域（依附于 cpu debug） | `*_npuchk.log` |
| **msobjdump** | 解析算子编译产出的 ELF 文件，提取 meta 信息 | 离线（命令行） | 算子 ELF 文件 |
| **show_kernel_debug_data** | 离线解析 `DumpTensor/printf` 落盘的调试 bin 文件 | 离线（命令行） | `.bin` 调试文件 |
| **optype_collector** | 采集 OpType 信息并检测自定义算子与内置算子的重名冲突 | 离线（命令行） | CANN OPP 包目录 |

#### 4.2.2 核心流程

五个工具并不孤立，它们的协作关系可以这样理解：

1. **cpu debug** 是基础——它让算子能在 CPU 上跑起来。
2. **npu check** 在 cpu debug 执行算子的同时，**同步**进行内存与同步检查，是 cpu debug 之上的「增强检查层」。
3. 算子除了 CPU 调测，还会被编译成 ELF，**msobjdump** 负责解析这种产物。
4. 算子在执行时可以通过 `DumpTensor/printf` 把中间数据落盘，**show_kernel_debug_data** 负责把这些 bin 文件解析成可读信息。
5. 当算子要作为「自定义算子包」安装交付时，**optype_collector** 帮你提前发现命名冲突。

#### 4.2.3 源码精读

**① cpu debug**

README 的概述里，对 cpu debug 的定义是：

> cpu debug 工具本质上是提供了 CPU 调试库文件，使得 Ascend C 源码可以通过通用 GCC 编译器编译得到在 CPU 上运行、调测的算子二进制文件。

参见 [README.md:L7-L9](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/README.md#L7-L9)。更细的使用方式（添加 `cpu_debug_launch.h` 头文件、`CMAKE_ASC_RUN_MODE=cpu` 编译）记录在 [docs/01_cpu_debug.md:L14-L35](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/01_cpu_debug.md#L14-L35)。这一段代码说明了 cpu debug 的两步用法：引入头文件、用 cmake 指定 CPU 域编译后直接运行可执行程序。

**② npu check**

README 对它的定义是：

> npu check 工具，用于检查 Kernel 源码实现逻辑，功能包含：内存检查、多线程检查、内存生命周期管理、内存地址依赖管理、同步事件管理等。

参见 [README.md:L11-L13](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/README.md#L11-L13)。它的一个关键特性在 [docs/02_npu_check.md:L3-L5](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/02_npu_check.md#L3-L5) 中说明：**只有当 debug 阶段正常退出（即没有触发 ASSERT 校验失败），npu check 才会输出完整的校验日志与分析**。这说明 npu check 依赖于 cpu debug 提供的执行环境，是「叠在 debug 之上」的。

**③ msobjdump**

README 对它的定义是：

> msobjdump 针对 Kernel 直调算子开发与工程化算子开发编译生成的算子 ELF 文件提供解析和解压功能，并将结果信息以可读形式呈现。

参见 [README.md:L15-L17](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/README.md#L15-L17)。它的三种命令模式（`--dump-elf` / `--extract-elf` / `--list-elf`）定义在 [docs/03_msobjdump.md:L8-L26](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/03_msobjdump.md#L8-L26)。

**④ show_kernel_debug_data**

README 对它的定义是：

> show_kernel_debug_data 工具用于离线解析通过 `AscendC::DumpTensor`/`AscendC::print` 接口保存的 Kernel 侧算子调试信息。

参见 [README.md:L19-L21](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/README.md#L19-L21)。它的命令行用法 `show_kernel_debug_data <bin_file_path> [<output_path>]` 记录在 [docs/04_show_kernel_debug_data.md:L11-L18](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/04_show_kernel_debug_data.md#L11-L18)。

**⑤ optype_collector（第五个工具）**

这个工具没有出现在 README 的概述段落里，但它是 asc-tools 正式出包的一部分，位于 `utils/optype_collector/`。它的定位见 [docs/05_optype_collector.md:L3-L5](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/05_optype_collector.md#L3-L5)：

> `optype_collector` 用于采集 CANN OPP 包中指定 SoC 的 OpType 信息，并检测自定义算子包和内置算子包、自定义算子包之间的 OpType 重名问题，帮助开发者在自定义算子安装或交付前提前发现命名冲突。

它的信息来源（`built-in` 内置算子、`vendors` 自定义算子、`ASCEND_CUSTOM_OPP_PATH` 环境变量）参见 [docs/05_optype_collector.md:L7-L12](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/05_optype_collector.md#L7-L12)。

#### 4.2.4 代码实践

**实践目标**：用一句话分别概括五个工具的用途，加深记忆。

**操作步骤**：

1. 打开 [README.md:L7-L21](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/README.md#L7-L21)，读前四个工具的描述。
2. 打开 [docs/05_optype_collector.md:L3-L5](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/05_optype_collector.md#L3-L5)，读第五个工具的描述。
3. 在自己的笔记里，为每个工具写一句「不超过 15 字」的概括。

**需要观察的现象**：你会注意到前四个工具的描述里高频出现「调试 / 解析 / 检查」等词，而 optype_collector 强调的是「采集 / 冲突检测」——这正区分了它们的角色。

**预期结果**：可以写出类似下表的概括（参考）：

| 工具 | 一句话概括 |
| --- | --- |
| cpu debug | 在 CPU 上跑算子做功能精度验证 |
| npu check | 检查算子内存与同步逻辑是否合法 |
| msobjdump | 解析算子 ELF 文件信息 |
| show_kernel_debug_data | 离线解析调试 bin 文件 |
| optype_collector | 采集 OpType 并检测重名冲突 |

> 注：本实践为源码阅读型实践，无需运行任何命令；若想尝试运行命令，需要先按下一模块完成环境与编译安装。

#### 4.2.5 小练习与答案

**练习 1**：npu check 和 cpu debug 是什么关系？能不能脱离 cpu debug 单独运行 npu check？

> **参考答案**：npu check 依附于 cpu debug。根据 [docs/02_npu_check.md:L11-L13](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/02_npu_check.md#L11-L13)，算子是通过 cpu_debug 在 CPU 域执行时，npu check 才同步进行检查的，因此不能脱离 cpu debug 单独运行。

**练习 2**：如果你想看一个算子编译出的 ELF 里有哪些 meta 字段，应该用哪个工具？

> **参考答案**：用 **msobjdump**，配合 `--dump-elf` 或 `--list-elf` 命令。

**练习 3**：optype_collector 主要解决什么问题？

> **参考答案**：检测自定义算子包与内置算子包、或多个自定义算子包之间的 OpType 重名冲突，避免安装交付时才发现命名问题。

---

### 4.3 工具链整体定位

#### 4.3.1 概念说明

单看每个工具容易只见树木不见森林。本模块把视角拉高，从**算子的完整生命周期**来看 asc-tools 在哪个环节出场。

关键认知是：asc-tools 不是一个「从头到尾一条龙」的算子开发框架，而是**横跨「开发 → 调测 → 交付」三个阶段的诊断工具集**。它的产物形态有三种：

1. **CPU 域可执行程序**（cpu debug 的产物）——可被 gdb 调试。
2. **校验日志**（npu check 的产物）——文本形式的错误报告。
3. **离线解析结果**（msobjdump / show_kernel_debug_data / optype_collector 的产物）——把二进制/目录信息转成可读输出。

#### 4.3.2 核心流程

下面这张文字流程图描述了「算子源码 → 各工具产物」的链路：

```
            Ascend C 算子源码 (.asc / .cpp)
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
   【CPU 域编译】   【NPU 域编译】   【自定义算子打包】
   (cpu debug)                        │
        │              │              ▼
   ┌────┴────┐         ▼        optype_collector
   ▼         ▼     算子 ELF    (采集 OpType / 检测冲突)
 gdb 调试   npu check   │
 (单步/      (内存/同步   ├─ msobjdump
  断点)       检查 →       │   (解析 ELF / meta)
            *_npuchk.log) │
                          └─ show_kernel_debug_data
                              (解析 DumpTensor/printf
                               落盘的 .bin 文件)
```

简而言之：

- **左侧「CPU 域」分支**：cpu debug + npu check，负责「在 CPU 上把算子跑对」。
- **中间「NPU 域」分支**：算子编译成 ELF 后，由 msobjdump 解析、show_kernel_debug_data 解析调试 bin。
- **右侧「交付」分支**：optype_collector 在算子包安装前做命名冲突体检。

#### 4.3.3 源码精读

**① 目录结构与工具定位**

README 的目录结构一节，把仓库划分为几个清晰的职责区，参见 [README.md:L24-L45](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/README.md#L24-L45)。其中与本讲工具直接对应的目录是：

- `cpudebug/` —— cpu debug 工具的实现源代码（C++ 核心）。
- `npuchk/` —— npu check 检查工具。
- `utils/msobjdump/` —— msobjdump 实现源代码。
- `utils/show_kernel_debug_data/` —— show_kernel_debug_data 实现源代码。
- `utils/optype_collector/` —— optype_collector 实现源代码（位于 `utils/` 下，README 目录树中归类于 utils，与实际磁盘结构一致）。
- `examples/` —— 各工具的样例工程。
- `docs/` —— 各工具使用说明。

> 这也解释了一个细节：为什么 optype_collector 没出现在 README 概述里，却仍是工具之一——它和 msobjdump、show_kernel_debug_data 一样，都位于 `utils/` 目录下，作为独立 Python 工具随包发布。

**② 整体使用链路**

快速入门文档把「环境准备 → 编译 → 安装 → 验证」串成了一条完整链路，参见 [docs/00_quick_start.md:L3-L10](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/00_quick_start.md#L3-L10)（环境准备）以及编译安装 [docs/00_quick_start.md:L349-L372](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/00_quick_start.md#L349-L372)。核心命令是：

```bash
bash build.sh --pkg          # 编译并打包
cd build_out
./cann-asc-tools_<version>_linux-<arch>.run --full --pylocal   # 安装
```

安装完成后，这些工具会被装到 CANN 包的路径下，用户无需进入工具目录，可直接用命令名调用（如 `msobjdump`、`show_kernel_debug_data`、`optype_collector`）。

**③ 文档索引**

README 还提供了一张文档导航表，参见 [README.md:L49-L53](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/README.md#L49-L53)，分别指向快速入门、各工具使用说明、以及 Ascend C 算子编程指南。这是后续深入学习每个工具时的入口。

#### 4.3.4 代码实践

**实践目标**：把本讲的工具认知固化为一张「算子源码 → 各工具产物」关系图。

**操作步骤**：

1. 对照本模块 4.3.2 的文字流程图，结合 [README.md:L24-L45](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/README.md#L24-L45) 的目录结构，自己用纸笔或绘图工具重画一张图。
2. 图中至少包含：算子源码起点、cpu debug、npu check、msobjdump、show_kernel_debug_data、optype_collector 六个节点。
3. 用箭头标出每个工具的**输入**与**产物**。

**需要观察的现象**：画图时你会反复确认「谁依赖谁的产物」——比如 npu check 依赖 cpu debug 的执行环境，而 msobjdump 依赖编译器产出的 ELF。这个确认过程就是建立工具链认知的过程。

**预期结果**：得到一张能清楚回答「算子写完后，每个工具在哪个环节、吃什么、吐什么」的关系图。

> 注：本实践不依赖运行环境；如果你已按 4.3.3 完成编译安装，可在样例目录实际跑一遍 add 样例（详见后续 u1-l4 讲义）来印证这张图。

#### 4.3.5 小练习与答案

**练习 1**：五个工具中，哪几个是「在 CPU 域执行算子时」起作用的？哪几个是「离线」对产物起作用的？

> **参考答案**：cpu debug、npu check 在 CPU 域执行算子时起作用；msobjdump、show_kernel_debug_data、optype_collector 是离线对产物（ELF / bin / OPP 包目录）起作用。

**练习 2**：为什么 README 概述只列了四个工具，但我们说有五个？

> **参考答案**：README 的概述段落重点介绍了四个「调试与分析」类工具，而 optype_collector 虽然在概述里未单独列出，但它位于 `utils/optype_collector/`，有独立文档 `docs/05_optype_collector.md`，并随包发布、可独立命令调用，因此它是工具集里的第五个工具。

---

## 5. 综合实践

把本讲的三个模块串起来，完成一个小任务：

**任务**：假设你是一名新加入团队的 Ascend C 算子开发者，团队让你写一份「工具链速查卡」给后来的新人。

要求你的速查卡里包含：

1. 一句话说明 asc-tools 是什么（取自 4.1）。
2. 一张五工具对照表，包含「用途 / 运行域 / 输入或产物」三列（取自 4.2）。
3. 一张「算子源码 → 各工具产物」关系图（取自 4.3）。
4. 在关系图上，标注出五个工具对应的源码目录（`cpudebug/`、`npuchk/`、`utils/msobjdump/`、`utils/show_kernel_debug_data/`、`utils/optype_collector/`）。

**预期结果**：完成后，你应当能用 3 分钟向一个完全没接触过 asc-tools 的人讲清楚这套工具集的全貌。

> 待本地验证：本综合实践为文档阅读与归纳型任务，无需运行命令；若要让速查卡更扎实，可在完成 u1-l4 后补充真实编译运行的截图。

---

## 6. 本讲小结

- **asc-tools** 是 CANN 为 Ascend C 配套提供的调试工具集，目标是「把 NPU 上才能发现的问题，前移到 CPU 域和离线阶段解决」。
- 工具集包含 **五个工具**：cpu debug、npu check、msobjdump、show_kernel_debug_data、optype_collector。
- **cpu debug** 让算子在 CPU 上跑起来；**npu check** 依附于它做内存与同步检查。
- **msobjdump** 解析算子 ELF；**show_kernel_debug_data** 解析调试 bin；两者都是离线工具。
- **optype_collector** 在算子包交付前检测 OpType 重名冲突，是常被忽略但很重要的第五个工具。
- 整体链路可以概括为：**算子源码 → CPU 调测（cpu debug + npu check）→ 离线分析（msobjdump / show_kernel_debug_data）→ 交付体检（optype_collector）**。

---

## 7. 下一步学习建议

本讲建立了「全景认知」，接下来建议按以下顺序深入：

1. **u1-l2 目录结构与源码组织**：进入仓库内部，看每个工具的实现放在哪里、根 CMake 如何组织它们。
2. **u1-l3 开发环境搭建与依赖管理**：了解编译运行 asc-tools 需要哪些依赖，为真正动手做准备。
3. **u1-l4 一键编译与运行第一个样例**：用 `build.sh` 编译出 run 包，并跑通第一个 cpudebug 的 add 样例，亲眼印证本讲的关系图。

如果想直接看某个工具的用法，也可跳到 `docs/` 下对应文档；但建议先走完 u1 单元，建立起完整的工具链视角，再深入单个工具的源码。
