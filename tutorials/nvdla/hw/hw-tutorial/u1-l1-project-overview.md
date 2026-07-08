# NVDLA 项目总览：它是什么、能做什么

> 本讲是「NVDLA 硬件学习手册」的第一篇。我们将从最基本的问题开始：NVDLA 到底是什么？这个仓库里又装了些什么？读完本讲，你不必懂得任何 Verilog 也能对项目建立起清晰的「地图感」，为后续逐模块深入打下基础。

---

## 1. 本讲目标

学完本讲后，你应该能够：

1. 用一句话向别人解释 NVDLA 是什么（以及它「不是」什么）。
2. 说出 `nvdlav1` 这个分支的固定算力规格：**2048 个 8-bit MAC**，或等价的 **1024 个 16-bit 定点/浮点 MAC**。
3. 理解 NVDLA 是一个可被集成进 SoC（System-on-Chip，片上系统）的「硬件 IP」，而不是一块独立芯片。
4. 看懂仓库的目录结构，能指出 RTL、C-model、testbench、综合脚本各自对应哪个目录。
5. 打开 README 与 LICENSE，找到这些结论的原文出处。

---

## 2. 前置知识

本讲默认你**没有任何硬件设计背景**。下面几个术语会反复出现，先建立直觉即可，不需要死记。

- **推理（Inference）**：训练好的神经网络「跑起来」做预测的阶段。本仓库只关心推理，不关心训练。
- **MAC（Multiply-Accumulate，乘加运算）**：神经网络中最核心的计算，即 \( a \times b + c \)。一个加速器的算力通常用「每秒多少 MAC」或「每周期多少 MAC」来衡量。
- **SoC（System-on-Chip，片上系统）**：把 CPU、GPU、加速器等多种功能集成在同一块芯片上的设计，例如手机的处理器。
- **IP（Intellectual Property，知识产权模块）**：芯片设计里可复用的模块。NVDLA 就是一个「推理加速器 IP」——别人设计好 SoC 时，可以把 NVDLA 当成一个零件嵌进去。
- **RTL（Register Transfer Level，寄存器传输级）**：用硬件描述语言（这里是 Verilog / SystemVerilog）写成的、描述电路行为的代码。本仓库的 `vmod/` 目录就是 RTL。
- **Testbench（测试平台）**：给 RTL 喂激励、检查输出的仿真环境，本仓库的 `verif/` 目录即是。
- **C-model（C 参考模型）**：用 C/C++ 写的、与 RTL 行为一致的高层模型，用来当「黄金标准」和 RTL 仿真结果对比。

> 一句话定位：**NVDLA 是 NVIDIA 开源的、用于 SoC 集成的深度学习「推理」加速器硬件 IP**。本讲后面所有内容都是对这句话的展开与证明。

---

## 3. 本讲源码地图

本讲涉及的关键文件都很「轻量」，主要是项目门面文件：

| 文件 | 作用 | 本讲用来回答的问题 |
| --- | --- | --- |
| [README.md](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/README.md) | 项目说明、目录结构、构建命令 | NVDLA 是什么？仓库里有什么？怎么编译？ |
| [LICENSE](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/LICENSE) | NVIDIA Open NVDLA License v1.0 | NVDLA 的法律定位（开源、免版税、可用于 DLA Product） |
| [VERSION](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/VERSION) | 版本标记文件 | 当前版本代号 |

此外，我们会顺带引用仓库**实际目录结构**来建立「资产地图」，但不会进入任何具体 RTL 文件——那是后续讲义的任务。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 README 概述**：NVDLA 到底是什么。
- **4.2 nvdlav1 分支说明**：本仓库的固定规格与维护定位。
- **4.3 硬件架构概览**：仓库资产与整体架构直觉。

### 4.1 README 概述：NVDLA 是什么

#### 4.1.1 概念说明

很多人第一次听到「NVDLA」会以为它是一块可以买到的显卡或芯片。**这是最常见的误解**。README 开篇就把定位讲清楚了：

> The NVIDIA Deep Learning Accelerator (NVDLA) is a free and open architecture that promotes a standard way to design deep learning inference accelerators. With its modular architecture, NVDLA is scalable, highly configurable, and designed to simplify integration and portability.

关键信息有三层：

1. **它是「架构 / 设计」，不是「实物芯片」**。你拿到的是 RTL 源码（Verilog），而不是一块能插上主板的板卡。
2. **它面向「推理（inference）」**，不负责训练。
3. **它是「模块化、可扩展、可集成」的**——目标是让别人更容易把它嵌进自己的 SoC 设计里。

LICENSE 用更精确的法律语言再次确认了这一点：NVDLA 是「a hardware design that accelerates inferencing in System-on-a-Chip designs」（一个加速 SoC 中推理的硬件设计）。

#### 4.1.2 核心流程

从「拿到 NVDLA 源码」到「变成芯片里的一部分」，使用流程大致是：

```text
NVDLA 开源 RTL 源码
      │
      ▼
 配置特性（spec/） → 生成/编译 RTL（tools/）
      │
      ▼
 仿真验证（verif/ + cmod/ 黄金比对）
      │
      ▼
 逻辑综合（syn/，映射到某家工艺库）
      │
      ▼
 作为 IP 嵌入自家 SoC → 流片成 DLA Product
```

注意「DLA Product」是 LICENSE 里的术语，指「按 NVDLA 规格设计制造的半导体芯片产品」。也就是说，NVDLA 本身不是芯片，**你（或芯片厂商）把它做成芯片后，那个芯片才叫 DLA Product**。

#### 4.1.3 源码精读

先看 README 对 NVDLA 的定义（这是全文最重要的一段）：

- [README.md:6-9](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/README.md#L6-L9)：把 NVDLA 定义为「free and open architecture（自由且开放的架构）」，强调 scalable（可扩展）、highly configurable（高度可配置）、simplify integration and portability（简化集成与移植）。**中文说明：这一段定性了 NVDLA 是「开放架构 / 可集成 IP」，而不是成品硬件。**

再看 LICENSE 中对 NVDLA 与 DLA Product 的定义：

- [LICENSE:3-4](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/LICENSE#L3-L4)：`"NVDLA" means NVIDIA's Deep Learning Accelerator, a hardware design that accelerates inferencing in System-on-a-Chip designs.` **中文说明：NVDLA = 加速 SoC 推理的硬件设计。**
- [LICENSE:40-43](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/LICENSE#L40-L43)：`"DLA Product" shall mean a semiconductor chip product ...` **中文说明：DLA Product = 据此制造出来的半导体芯片产品。** 两者区分开，你就不会把「设计」和「芯片」搞混。

#### 4.1.4 代码实践

> **实践目标**：亲手从源码里「抠」出 NVDLA 的官方定位，而不是听我转述。

操作步骤：

1. 用编辑器或 `Read` 打开仓库根目录的 `README.md`。
2. 定位到第 6–9 行。
3. 同样打开 `LICENSE`，定位到第 3–4 行和第 40–43 行。

需要观察的现象：README 和 LICENSE 用了不同语气（前者是市场/工程描述，后者是法律定义），但**结论一致**——NVDLA 是可被集成进 SoC 的推理加速器硬件设计。

预期结果：你能用自己的话写出一句不超过 30 字的定义，例如「NVDLA 是 NVIDIA 开源的、用于 SoC 的深度学习推理加速器 IP」。

> 说明：本实践为「源码阅读型」，不涉及编译运行。

#### 4.1.5 小练习与答案

**练习 1**：有人说「我买了一块 NVDLA 显卡」，这句话哪里不对？

> **参考答案**：NVDLA 不是成品硬件，而是一份开源的硬件设计（RTL）。你能买到的、集成了 NVDLA 的实体芯片，按 LICENSE 的术语叫 **DLA Product**，那是芯片厂商基于 NVDLA 设计制造出来的产品，不是 NVDLA 本身。

**练习 2**：NVDLA 主要做训练还是推理？从哪里可以看出来？

> **参考答案**：只做**推理（inference）**。README 第 6 行写的是「deep learning inference accelerators」，LICENSE 第 3 行写的是「accelerates inferencing」，两处都明确是 inference。

---

### 4.2 nvdlav1 分支说明：规格与维护定位

#### 4.2.1 概念说明

GitHub 上的 `nvdla/hw` 有多个分支，其中最常被提及的是 `master` 和 `nvdlav1`。**本仓库（也就是我们正在学的）处于 `nvdlav1` 分支**。它和 `master` 的本质区别在于「**是否可配置**」：

- `master` 分支：可配置版本，你可以调整 MAC 数量、数据宽度等参数，裁剪出不同规模的 NVDLA。
- `nvdlav1` 分支：**不可配置的「全精度（full-precision）」固定版本**，规格锁死。

为什么要单独学 `nvdlav1`？因为它是**稳定维护版（stable sustaining release）**：会持续接收 bug 修复，规格不会变来变去，最适合作为「教科书」来阅读和理解。

#### 4.2.2 核心流程

`nvdlav1` 的固定算力规格是理解整个项目规模的钥匙：

| 精度模式 | MAC 数量 | 含义 |
| --- | --- | --- |
| int8（8 位定点） | **2048** | 每周期可做 2048 次 8-bit 乘加 |
| int16 / fp16（16 位定点或浮点） | **1024** | 每周期可做 1024 次 16-bit 乘加 |

为什么 2048 与 1024 是「2:1」关系？因为一条 16-bit 的乘法数据通路，可以拆成两条 8-bit 通路复用。用公式表达这种配对关系：

\[ 2048 \;(\text{int8 MAC}) = 1024 \times 2 \;(\text{每条 16-bit 通路做 2 个 int8 MAC}) \]

峰值算力则取决于核心时钟频率 \( f \)（单位 Hz）：

\[ \text{峰值 int8 算力} = 2048 \times f \quad [\text{MAC/秒}] \]

例如核心跑在 \( f = 1\,\text{GHz} \) 时：

\[ 2048 \times 10^{9} = 2.048 \times 10^{12} \;\text{MAC/秒} = 2.048\;\text{TMAC/s} \]

这个量级就是后续阅读卷积核心（CMAC 阵列）源码时，你会反复感受到的「为什么阵列这么大」的根本原因。

#### 4.2.3 源码精读

`nvdlav1` 分支的定位全部写在 README 的「About this release」一节：

- [README.md:15-19](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/README.md#L15-L19)：明确这是 `nvdlav1` 分支，包含「non-configurable full-precision version（不可配置的全精度版本）」，并固定为 **2048 个 8-bit MAC 或 1024 个 16-bit 定点/浮点 MAC**。**中文说明：这一段是整个分支算力规格的唯一权威出处。**
- [README.md:20-22](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/README.md#L20-L22)：说明本分支是 stable sustaining release（稳定维护版），会修 bug 但不会加新 RTL 特性；并且会与 `master` 分叉，只做 cherry-pick，不做整体合并。**中文说明：解释了「为什么 nvdlav1 适合作为学习对象」——稳定、不漂移。**

版本标记则记录在 VERSION 文件：

- [VERSION:1](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/VERSION#L1)：内容为 `NVDLA_OS_INITIAL`。**中文说明：这是 NVDLA 开源版本的初始代号，文件本身没有语义化的版本号（如 1.2.3），而是一个固定的发布标记。**

> 备注：README 第 1 行标题写的是「NVDLA Open Source Hardware, version 1.0」——「1.0」指开源发布的大版本，与 `nvdlav1` 这个分支名呼应。我们在引用永久链接时使用当前 HEAD（`8e06b1b9...`）作为稳定锚点。

#### 4.2.4 代码实践

> **实践目标**：把抽象的「2048 / 1024 MAC」换算成你能感受的算力数字。

操作步骤：

1. 打开 [README.md:15-22](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/README.md#L15-L22)，确认规格原文。
2. 假设核心时钟频率 \( f = 500\,\text{MHz} \)（一个常见的嵌入式频率），用公式 \( 2048 \times f \) 算出 int8 峰值算力。
3. 再用 \( 1024 \times f \) 算出 int16/fp16 峰值算力。

需要观察的现象：同一频率下，int8 算力恰好是 int16 的两倍——印证了 4.2.2 节里「16-bit 通路可拆成两条 8-bit 通路」的配对关系。

预期结果：

- \( f = 500\,\text{MHz} \)：int8 = \( 2048 \times 5\times10^{8} = 1.024\,\text{TMAC/s} \)；int16 = \( 0.512\,\text{TMAC/s} \)。
- 你应该得出结论：**频率不变时，降精度能换来 2 倍吞吐**，这也是 NVDLA 同时支持多精度的工程动机。

> 说明：本实践为「规格推导型」，不需要运行仿真。

#### 4.2.5 小练习与答案

**练习 1**：`nvdlav1` 分支和 `master` 分支最大的区别是什么？

> **参考答案**：`nvdlav1` 是**不可配置**的固定全精度版本（2048 个 8-bit / 1024 个 16-bit MAC），而 `master` 是可配置版本，可以裁剪规模。此外 `nvdlav1` 定位为稳定维护版，只接收 bug 修复、不加新特性。

**练习 2**：某 SoC 把 NVDLA 核心时钟设为 1 GHz，跑 int8 推理，理论峰值算力是多少？

> **参考答案**：\( 2048 \times 10^{9} = 2.048\,\text{TMAC/s} \)（约每秒 2.048 万亿次乘加）。

**练习 3**：如果你想要「更高的数值精度」而不是「更高的吞吐」，应该选 int8 还是 int16/fp16？算力会受什么影响？

> **参考答案**：选 int16/fp16 精度更高，但每周期 MAC 数减半（从 2048 降到 1024），相同频率下吞吐约为 int8 的一半。这是精度与吞吐之间的权衡。

---

### 4.3 硬件架构概览：仓库资产与整体架构

#### 4.3.1 概念说明

理解了「是什么」之后，我们来看「仓库里有什么」。README 把仓库概括为「RTL、C-model 和 testbench 代码」三类核心资产，外加综合脚本和工具。把这些资产和目录对应起来，是后续所有讲义的导航基础。

先看仓库的实际顶层目录（已核对，与 README 描述一致，并补充了 README 未列入的 `cmod/`）：

| 顶层目录 | 资产类别 | 作用 |
| --- | --- | --- |
| `vmod/` | **RTL** | Verilog 实现，包括 `vmod/nvdla`（NVDLA 各引擎）、`vmod/vlibs`（库单元）、`vmod/rams`（RAM 行为模型） |
| `cmod/` | **C-model** | 与 RTL 对应的 C++/SystemC 黄金参考模型（README 文字提及，目录实际存在） |
| `verif/` | **Testbench** | trace-player 测试平台 + 样例 trace |
| `syn/` | 综合脚本 | Synopsys DC 综合示例脚本与 SDC 约束 |
| `spec/` | 配置 | RTL 特性选项配置（`defs`、`manual`） |
| `tools/` | 工具 | tmake、defgen、eperl 等构建与生成工具 |
| `perf/` | 性能 | `NVDLA_OpenSource_Performance.xlsx` 性能估算表 |

其中 `vmod/nvdla/` 下进一步分了 17 个子模块，它们正是 NVDLA 的「功能器官」。本讲只要求你建立大致印象（后续每篇讲义会逐一深入）：

```text
vmod/nvdla/
├── top/         顶层与分区（NV_nvdla.v, partition_a/c/m/o/p）
├── apb2csb/     APB→CSB 配置总线桥
├── csb_master/  中央配置路由器
├── glb/         全局配置与中断聚合
├── cdma/ csc/ cmac/ cacc/ cbuf/   ← 卷积计算主流水线
├── nocif/       存储接口（MCIF/CVIF）
├── bdma/        桥 DMA（存储间搬运）
├── sdp/ pdp/ cdp/ rubik/          ← 后处理流水线
├── car/         时钟、复位、门控
└── retiming/    流水/重定时寄存器
```

> 一句话架构直觉：**外部 CPU 通过配置总线（CSB）写寄存器来「编程」NVDLA；数据从外部存储（AXI）进来，依次流过「卷积核心 → 后处理」，结果再写回存储；期间通过中断（GLB 聚合）通知 CPU 完成。**

#### 4.3.2 核心流程

把 NVDLA 当作「黑盒子」时，它的对内对外关系可以抽象为：

```text
                 ┌──────────────────────── NVDLA ────────────────────────┐
  CPU ──CSB/APB──▶│ 配置空间(csb2csb→csb_master→各引擎寄存器)              │
                 │                                                       │
  外部存储◀─AXI─▶│ 存储接口(MCIF/CVIF) ──▶ 卷积核心 ──▶ 后处理 ──▶ 写回   │
                 │       (CDMA→CBUF→CSC→CMAC→CACC)  (SDP/PDP/CDP/Rubik) │
                 │                                                       │
   CPU◀─中断────│ 全局中断聚合(GLB)                                      │
                 └───────────────────────────────────────────────────────┘
```

把这张「黑盒图」和 4.3.1 的目录表对照看，你会发现**几乎每个顶层目录都对应黑盒里的一个职责**：`vmod/nvdla/*` 是盒子的内部实现，`verif/` 是用来测试盒子的台子，`cmod/` 是盒子的「标准答案」，`syn/` 把盒子综合成真实电路，`tools/` 和 `spec/` 帮你生成和配置盒子。

#### 4.3.3 源码精读

README 的「Directory Structure」一节是这份资产地图的权威出处：

- [README.md:35-36](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/README.md#L35-L36)：说明本仓库包含 RTL、C-model、testbench 三类代码。**中文说明：点明仓库的三大核心资产。**
- [README.md:38-41](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/README.md#L38-L41)：`vmod/` 下再分 `vmod/nvdla`（Verilog 实现）、`vmod/vlibs`（库与单元模型）、`vmod/rams`（RAM 行为模型）。**中文说明：RTL 内部的三层细分。**
- [README.md:42-47](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/README.md#L42-L47)：`syn/`（综合脚本）、`perf/`（性能估算表）、`verif/`（trace-player 测试平台，含 `verif/traces/` 样例）、`tools`（构建/仿真/综合工具）、`spec`（RTL 配置选项）。**中文说明：其余资产目录的官方职责说明。**
- [README.md:49-55](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/README.md#L49-L55)：给出最简构建命令 `bin/tmake`（实际位于 `tools/bin/tmake`，由顶层 `Makefile` 驱动）。**中文说明：编译设计的入口。**

> 小贴士：README 的目录列表里**没有单列 `cmod/`**，但第 35 行文字明确写了「RTL, C-model, and testbench code」。目录树里确实存在 `cmod/`，它就是 C-model 的家。这种「文字提到、列表未列」的小出入，读源码时要注意。

#### 4.3.4 代码实践

> **实践目标**：用 `ls` 亲自核对仓库目录，验证 README 的描述与磁盘实际一致。

操作步骤：

1. 在仓库根目录执行 `ls -1`，查看顶层目录。
2. 进入 `vmod/nvdla`，执行 `ls -1`，数一数有多少个子模块目录。
3. 对照本讲 4.3.1 的目录表，给每个顶层目录标注资产类别。

需要观察的现象：顶层会出现 `vmod / cmod / verif / syn / spec / tools / perf` 等目录，与你刚学的资产表一一对应；`vmod/nvdla` 下能看到 `top / cdma / csc / cmac / cacc / sdp / pdp ...` 等子模块。

预期结果：你能填出下面这张「资产对应表」（答案见 4.3.1）：

| 资产类别 | 对应目录 |
| --- | --- |
| RTL | `vmod/` |
| C-model | `cmod/` |
| Testbench | `verif/` |
| 综合脚本 | `syn/` |

> 说明：本实践为「目录核对型」，只读不写，安全可重复执行。

#### 4.3.5 小练习与答案

**练习 1**：如果你想看 NVDLA 的「Verilog 源码」，应该进哪个目录？它内部又分哪三个子目录？

> **参考答案**：进 `vmod/`。内部三个子目录是 `vmod/nvdla`（NVDLA 各引擎的 Verilog 实现）、`vmod/vlibs`（库与单元模型）、`vmod/rams`（RAM 行为模型）。

**练习 2**：C-model 和 RTL 是什么关系？为什么要同时存在？

> **参考答案**：C-model 是与 RTL 行为一致的 C++/SystemC 高层参考模型，充当「黄金标准」。仿真时用它产生期望输出，再和 RTL 的实际输出逐比特比对，从而验证 RTL 实现是否正确。两者并行存在是为了交叉验证。

**练习 3**：README 的目录列表里没有 `cmod/`，但仓库里却有这个目录。这说明什么？

> **参考答案**：说明文档（README）与实际仓库状态可能存在细微出入。读源码时要以**实际文件系统**为准，文档只作参考；遇到不一致要主动核对，而不是盲信文档。

---

## 5. 综合实践

> **贯穿任务**：写一份「NVDLA 一页纸简介」，把本讲三个模块串起来。

请完成以下三件事，产出一份不超过半页的中文笔记：

1. **三个核心卖点**：阅读 [README.md:6-9](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/README.md#L6-L9)，用自己的话提炼 NVDLA 的三个核心卖点（提示：可从「开源免费」「模块化可集成」「面向推理」等角度入手，但要用自己的措辞，不能照抄原文）。
2. **算力规格换算**：依据 [README.md:15-22](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/README.md#L15-L22)，写出 `nvdlav1` 的固定 MAC 规格，并假设核心频率 1 GHz，给出 int8 与 int16 的峰值算力。
3. **资产目录清单**：对照 [README.md:35-47](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/README.md#L35-L47)，列出「RTL / C-model / Testbench / 综合脚本」四类资产各自对应的目录，并各用一句话说明该目录的作用。

**预期产出示例（节选）**：

```text
NVDLA 一页纸简介
================
定位：NVIDIA 开源的、面向 SoC 集成的深度学习推理加速器 IP（不是成品芯片）。
卖点：① 开放、免版税（见 LICENSE）；② 模块化、可扩展、易集成；③ 专为推理优化。
规格：nvdlav1 = 2048 个 int8 MAC / 1024 个 int16 或 fp16 MAC。
算力：@1GHz → int8 2.048 TMAC/s，int16 1.024 TMAC/s。
资产：RTL→vmod/，C-model→cmod/，Testbench→verif/，综合脚本→syn/。
```

> 说明：本任务全部基于阅读与推导，不涉及编译或仿真运行。完成后，你应当能脱稿向同事介绍 NVDLA 是什么。

---

## 6. 本讲小结

- **NVDLA 是硬件 IP，不是芯片**：它是 NVIDIA 开源的、用于 SoC 集成的深度学习**推理**加速器设计；据此造出来的实体芯片叫 **DLA Product**。
- **本仓库在 `nvdlav1` 分支**：不可配置的「全精度」固定版本，规格锁死为 **2048 个 8-bit MAC** 或 **1024 个 16-bit 定点/浮点 MAC**，定位为稳定维护版（修 bug、不加新特性）。
- **算力可换算**：峰值算力 = MAC 数 × 核心频率；int8 吞吐是 int16 的两倍，体现了精度与吞吐的权衡。
- **四类核心资产**：RTL（`vmod/`）、C-model（`cmod/`）、Testbench（`verif/`）、综合脚本（`syn/`），外加 `spec/`（配置）、`tools/`（构建工具）、`perf/`（性能表）。
- **`vmod/nvdla/` 内有 17 个子模块**，分别承担配置、卷积核心、存储接口、后处理、时钟复位等职责——这就是后续讲义的路线图。
- **读源码要以实际文件系统为准**：文档（如 README 未列 `cmod/`）可能与实际有细微出入，遇到不一致要主动核对。

---

## 7. 下一步学习建议

本讲建立了宏观认知，接下来建议按以下顺序推进（对应学习手册单元 1）：

1. **u1-l2 仓库目录结构详解**：深入到每个 `vmod/nvdla` 子模块，给它们标上中文名，弄清谁属于卷积核心、谁属于后处理。
2. **u1-l3 构建系统与工具链**：搞懂 `tmake`、`build.config`、`defgen`、`eperl` 如何协同把 RTL 编译出来——这是跑通一切的前提。
3. **u1-l4 运行第一次仿真**：用 `verif/sim` 跑一个 sanity trace，亲眼看到 NVDLA「动起来」。
4. **u1-l5 顶层 RTL NV_nvdla.v**：第一次真正进入 Verilog，看顶层端口和分区结构。

> 想提前感受架构全貌的话，可以先扫一眼 [README.md:38-47](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/README.md#L38-L47) 的目录说明，以及 [http://nvdla.org/hwarch.html](http://nvdla.org/hwarch.html) 的硬件架构页（README 第 28 行给出）。具体 RTL 文件的精读，留给 u1-l5 开始。
