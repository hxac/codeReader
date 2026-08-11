# 项目总览与 FPGA 库定位

## 1. 本讲目标

本讲是整本学习手册的起点。读完本讲，你应该能够：

- 说出 `suisuisi/FPGA_Library` 这个仓库**是什么**、**收录了哪些 FPGA 设计**、**面向什么样的读者**。
- 说出仓库的三大顶层目录 `HDL` / `HLS` / `ThreePart` 各自承担的职责。
- 理解 FPGA、HDL（硬件描述语言）、IP（知识产权核）、HLS（高层综合）这几个基本概念之间的关系——这是后续所有讲义的共同语言。
- 学会用「读 README + 看目录结构 + 看 git 历史」三步法快速看懂一个陌生的多来源仓库。

本讲**不要求你写过任何 Verilog/VHDL 代码**，也不会让你立刻上板。我们的任务是先建立一张「地图」，知道每条路通向哪里，后续单元再逐条深入。

---

## 2. 前置知识

本讲面向零基础读者。下面几个名词会在文中反复出现，先用最朴素的语言解释一遍。

### 2.1 什么是 FPGA

**FPGA**（Field-Programmable Gate Array，现场可编程门阵列）是一种「出厂后还能被重新改写内部电路」的芯片。

- 你常用的 CPU 是「通用、顺序执行指令」的；它的指令集出厂时就固定了。
- FPGA 不同：你用一种特殊语言描述出一整套**数字电路**（逻辑门、触发器、连线），然后工具把这个描述「烧」进芯片，芯片内部就真的变出了你描述的那张电路。这个过程叫**综合（synthesis）**与**实现（implementation）**，最终产物是一个**比特流（bitstream）**文件，加载到芯片上即生效。

正因为电路是「空间上铺开」的，FPGA 天生适合**并行计算**，在图像处理、密码加速、视频显示、高速接口等领域很常见。

### 2.2 什么是 HDL

**HDL**（Hardware Description Language，硬件描述语言）就是用来描述上面那张电路的语言。最常用的两种是：

- **Verilog / SystemVerilog**（本仓库 AES 核心、projf 库主要用它）
- **VHDL**（本仓库 `color_space`、`axi_dynclk` 等用它）

关键直觉：HDL 描述的是「电路在每一拍的电平变化」，而不是「一行接一行执行的程序」。所以你会在 HDL 里频繁看到 `always`、时钟 `clk`、复位 `rst`、寄存器 `reg` / `logic` 这些概念。

### 2.3 什么是 IP 核

**IP 核**（Intellectual Property core，知识产权核）是一段「设计好、封装好、可以反复复用」的硬件模块。

打个比方：写软件时你会用现成的库函数；做 FPGA 时你会用现成的 IP 核，比如一个 UART 串口核、一个 AES 加密核。在 Xilinx Vivado 生态里，IP 核会被打包成一个带 `component.xml` 描述的目录，放进 **IP Catalog（IP 目录）**，之后在图形化的块设计（Block Design）里像搭积木一样拖出来用。本仓库的 `HDL/AesCryptoCore_1.0`、`HDL/DVI_TX` 就是这种被打包好的 IP。

### 2.4 什么是 HLS

**HLS**（High-Level Synthesis，高层综合）是一种更高的抽象层级：**用 C/C++ 写算法，由工具自动生成 HDL**。

- 传统 HDL：你手写每一拍电路，控制力强，但开发慢。
- HLS：你写 C 函数，加少量**编译指示（pragma）**告诉工具如何优化，工具帮你生成等价的 Verilog。

本仓库 `HLS/2D-median-filter-algorithm-HLS` 就是 HLS 设计的典型例子。

### 2.5 概念关系一览

下表把上面四个概念与软件世界里熟悉的东西做对照，方便记忆：

| FPGA 概念 | 直觉类比 | 本仓库中的例子 |
| --- | --- | --- |
| HDL（Verilog/VHDL） | 「汇编/C」——描述底层电路 | AES 核心的 `aes_top.v` |
| IP 核 | 「库/组件」——打包复用的模块 | `AesCryptoCore_1.0`、`DVI_TX` |
| HLS（C→电路） | 「高级语言」——自动生成电路 | 2D 中值滤波 |
| Bitstream | 「编译产物」——加载到芯片 | 综合/实现后的 `.bit` 文件 |

> 小提示：如果上面某个概念现在还模糊，没关系。本讲只要求你**建立印象**，后面每一单元都会在真实代码里反复巩固。

---

## 3. 本讲源码地图

本讲主要阅读**说明性文档（README）和目录结构**，这是理解一个多来源仓库最快的方式。下表列出本讲涉及的关键文件：

| 路径 | 作用 | 在本讲中的角色 |
| --- | --- | --- |
| `README.md` | 仓库根说明，一句话定位整个仓库 | 理解仓库总体定位 |
| `LICENSE` | 根目录许可证（MIT） | 理解自研部分的授权 |
| `HLS/2D-median-filter-algorithm-HLS/README.md` | 中值滤波 HLS 项目说明 | 代表 `HLS` 目录的范式 |
| `ThreePart/projf-explore/README.md` | Project F 教程库总览 | 代表 `ThreePart` 中最有教学价值的一个合集 |
| `ThreePart/ISOIEC18033-3StandardBlock/README.md` | 标准分组密码 HDL 合集说明 | 代表 `ThreePart` 中的学术密码资源 |
| `ThreePart/hardwarebee/README.md`、`ThreePart/digilent_ip/README.md` | 另两个第三方合集说明 | 补全 `ThreePart` 的版图 |

> 注意：`HDL` 目录下的自研 IP（如 AES）没有一个统一的根 README，它们的「说明」散落在各自的工程目录里（例如 `HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/readme.md` 讲的是工程如何版本化）。这正是本仓库「文档不统一」的真实特点，我们在 4.2 节会专门讲怎么应对。

---

## 4. 核心概念与源码讲解

本讲把仓库拆成四个最小模块来讲：根 README 与整体定位 → `HDL` 自研目录 → `HLS` 高层综合目录 → `ThreePart` 第三方合集目录。

### 4.1 仓库根 README 与整体定位

#### 4.1.1 概念说明

开源 FPGA 项目的根 README，通常是读者接触项目的第一扇窗。它应该回答三个问题：**这是什么？给谁用？怎么开始？**

但现实中，很多「聚合型」仓库（即把多个来源、多个作者的设计集中到一起的仓库）的根 README 写得非常简短，因为真正的说明都在各子项目里。`suisuisi/FPGA_Library` 就属于这类聚合仓库——它的根 README 只有一句话。所以读这种仓库时，**不能只看根 README，必须结合目录结构和 git 历史一起判断**。

#### 4.1.2 核心流程

理解一个聚合仓库的三步法：

1. **读根 README 与 LICENSE**：拿到一句话定位和整体授权。
2. **看顶层目录结构**：判断仓库由哪几大块组成、各块的语言/范式是什么。
3. **看 git 提交历史**：从「每次提交加了什么」还原仓库的演进顺序，理解它是怎么一点点搭起来的。

#### 4.1.3 源码精读

先看根 README 的全部内容，它只有两行：

[README.md:1-2](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/README.md#L1-L2) —— 第 1 行是标题 `# Xilinx_Library`，第 2 行写「Vivado 诸多 IP，包括图像处理等」。

这告诉我们两个事实：

- **定位**：这是一个收录「Vivado IP」的合集，并且明确提到包含图像处理类设计。
- **历史名称**：标题写的是 `Xilinx_Library`。仓库现在的名字是 `suisuisi/FPGA_Library`，但 README 还保留着旧名 `Xilinx_Library`——这说明仓库经历过改名，阅读时要以**实际目录结构**为准，不要被标题误导。

再看根 LICENSE：

[LICENSE:1](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/LICENSE#L1-L1) 是 MIT 许可证，[LICENSE:3](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/LICENSE#L3-L3) 写明 `Copyright (c) 2023 suisuisi`。

⚠️ 重要提醒：根目录的 MIT 只覆盖**仓库作者自研/整理的部分**。仓库里大量第三方子项目（projf、Digilent、东北大学密码核心等）各自带有**自己的许可证**（例如 HLS 子项目是 Apache 2.0、projf 是 MIT）。复用代码前，必须到对应子项目目录里看它自己的 LICENSE。

最后看 git 历史，它还原了仓库的搭建顺序（从旧到新）：

```text
25c6d90 add projf-explore            # 最早加入 Project F 教程库
fec43dd AesCryptoCore                # 加入 AES 加密核心
bc39ddd ISOIEC18033-3StandardBlock   # 加入标准分组密码合集
1e33525 Create README.md             # 最后补了一个根 README
```

可以看到，仓库是「先有内容、后补说明」一步步搭起来的——这也是为什么根 README 如此简短。

#### 4.1.4 代码实践

**实践目标**：亲手用三步法看懂这个仓库的「门面」。

**操作步骤**：

1. 在仓库根目录运行 `cat README.md`，确认你看到的就是上面那两行。
2. 运行 `git log --oneline -10`，观察提交历史。
3. 运行 `git log --oneline --name-status` 的最近几条，看看每次提交具体增删了哪些目录。

**需要观察的现象**：根 README 极短；提交历史里能清楚看到 `projf-explore`、`AesCryptoCore`、`ISOIEC18033-3StandardBlock` 等子项目是分批加入的。

**预期结果**：你能用一句话说出「这个仓库是一个聚合了多个 FPGA 设计的合集，内容由多次提交累积而成」。

#### 4.1.5 小练习与答案

**练习 1**：根 README 标题是 `Xilinx_Library`，但仓库实际名叫 `FPGA_Library`。这说明了什么？阅读时应以哪个为准？
> **答案**：说明仓库经历过改名，README 没有同步更新。阅读和引用时以**实际目录结构与仓库实际名称**为准，不要被旧标题误导。

**练习 2**：根 LICENSE 是 MIT，这是否意味着仓库里所有代码都可以按 MIT 自由使用？
> **答案**：不是。MIT 只覆盖作者自研/整理的部分；第三方子项目（projf、Digilent、东北大学密码核心、HLS 滤波等）各自带自己的许可证，复用前需逐一确认。

---

### 4.2 HDL 目录：自研硬件描述语言 IP

#### 4.2.1 概念说明

`HDL` 目录是仓库里**自研（或仓库作者整理的）硬件描述语言设计**的大本营。这里的设计都是直接用 Verilog/VHDL 写的电路，其中很多被打包成了 **Vivado IP**（带 `component.xml`，可以拖进块设计复用）。

理解这一节的关键是：**目录里的每个子文件夹，基本就是一个独立的 IP 或一个小工程**，彼此之间大多可以单独学习。

#### 4.2.2 核心流程

一个典型的自研 HDL IP 是怎么从「源码」变成「可用 IP」的：

1. 用 Verilog/VHDL 写电路功能（如 `aes_top.v`）。
2. 写一个顶层包装，把功能模块与 AXI 接口连起来（如 `AesCryptoCore_v1_0.v`）。
3. 用 `component.xml` 声明 IP 的版本、文件、总线接口，打包进 IP Catalog。
4. 在块设计里例化，连接处理器（如 Zynq 的 ARM 核），综合、实现、生成 bitstream。

> 这条流水线在 Unit 3「AXI-Lite IP 封装与软硬件协同」会完整拆解，本讲只要知道「HDL 目录里的东西最终会变成可复用的 IP」即可。

#### 4.2.3 源码精读

用 `git ls-files HDL/` 可以确认，`HDL` 目录下共有 5 个子项目，覆盖了「密码 + 完整视频通路」两大主题：

| 子项目 | 主要语言 | 作用 |
| --- | --- | --- |
| `AesCryptoCore_1.0` | Verilog | AES 加密核心，并封装成 AXI IP（仓库里**最完整、最适合精读**的自研设计） |
| `DVI_TX` | Verilog | DVI 发送器 IP（`dvi_encoder.v`、`serializer_10_to_1.v`，TMDS 接口） |
| `axi_dynclk_v1_0` | VHDL + Verilog | AXI 动态时钟发生器（基于 MMCM 的 DRP 配置，给显示提供像素时钟） |
| `color_space` | 多为 VHDL | 颜色空间转换（RGB↔HSV/YCbCr/CMYK/RYB 等多种） |
| `ov5640_cap_data` | Verilog | OV5640 摄像头图像采集 IP |

把它们连起来看，你会发现一条清晰的**视频链路**：`ov5640_cap_data`（摄像头采集）→ `color_space`（颜色空间转换）→ `DVI_TX`（DVI 输出），中间用 `axi_dynclk` 提供动态时钟。再加上独立的 `AesCryptoCore` 密码核心，这就解释了根 README 里「Vivado 诸多 IP，包括图像处理等」这句话。

其中 `AesCryptoCore_1.0` 还附带了一份讲「工程如何版本化」的说明：

[HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/readme.md:1-3](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/readme.md#L1-L3) —— 说明该目录是一套「Vivado 工程模板」，遵循 Xilinx 官方文档 UG892 推荐的最小化版本控制做法。

> 这份 readme 在 Unit 1 第 3 讲（Vivado 工程模板与工作流）会精读。本讲你只需知道：AES 工程有一套规范化的「保存/重建」流程，不是把整个 Vivado 工程一股脑塞进 git。

#### 4.2.4 代码实践

**实践目标**：用命令行快速摸清 `HDL` 目录里到底有什么。

**操作步骤**：

1. 进入仓库根目录，运行 `ls HDL/`，列出 5 个子项目。
2. 运行 `git ls-files HDL/ | sed 's#/.*##' | sort -u`，看看每个子项目在 git 里实际跟踪了哪些顶层条目。
3. 挑 `AesCryptoCore_1.0`，运行 `git ls-files HDL/AesCryptoCore_1.0 | head -30`，感受它的目录划分（`src` 源码、`utils` 工具模块、`gf_s_box` 复合域 S-Box、`tb` 测试、`VE_sv` SystemVerilog 验证环境）。

**需要观察的现象**：`HDL` 下确实有 5 个子项目；AES 核心的目录层次清晰，既有源码也有测试和验证环境。

**预期结果**：你能在不打开任何文件的情况下，说出「`HDL` 目录 = AES 密码核心 + 一条视频链路（采集/颜色/输出/时钟）」。

#### 4.2.5 小练习与答案

**练习 1**：`HDL` 目录里有哪几个子项目属于「视频通路」？请按数据流方向排序。
> **答案**：采集 `ov5640_cap_data` → 颜色转换 `color_space` → 输出 `DVI_TX`；时钟由 `axi_dynclk_v1_0` 提供。（`AesCryptoCore` 属于密码主题，不在视频通路内。）

**练习 2**：为什么说 `AesCryptoCore_1.0` 是 `HDL` 目录里最适合精读的设计？
> **答案**：因为它目录最完整——包含 `src`（算法源码）、`utils`（工具模块）、`gf_s_box`（复合域 S-Box）、`tb`（单元测试）、`VE_sv`（SystemVerilog 验证环境）、还有打包好的 AXI IP 和驱动，从算法到工程到验证一条龙，教学价值最高。

---

### 4.3 HLS 目录：高层综合

#### 4.3.1 概念说明

`HLS` 目录展示的是与 `HDL` **完全不同的设计范式**：不手写电路，而是用 C/C++ 写算法，交给工具（Xilinx Vivado HLS）自动生成 Verilog。

这条路线的好处是：算法工程师可以用熟悉的 C 语言快速把图像处理、信号处理算法搬上 FPGA；代价是生成出的 Verilog 通常又长又机器化（本讲后面会看到一个真实例子）。

#### 4.3.2 核心流程

一个标准 HLS 工作流分三步：

1. **C 仿真（C Simulation）**：像普通 C 程序一样编译运行，验证算法功能正确。
2. **综合（Synthesis）**：把 C 函数综合成 RTL（Verilog），查看**时延（latency）**和**资源占用（BRAM/DSP/LUT/FF）**报告。
3. **协同仿真（Co-Simulation）**：用原 C 写测试激励，驱动生成的 RTL 仿真，确认综合后的电路行为和 C 版本一致。

#### 4.3.3 源码精读

`HLS` 目录下有两个项目，分别处于工作流的不同阶段：

- `2D-median-filter-algorithm-HLS`：保留了完整的 C 源码（`MedianFilter.c`/`.h`）、C 仿真测试（`main_test.c`）、测试数据（`clean.csv`/`noisy.csv`）和 Vivado HLS 工程文件（`vivado_hls.app`）。这是一个**处于设计阶段**的 HLS 项目。
- `edge_canny_detector`：目录里是 `IP/` 和 `sorce/`（原文如此，应为 source），`IP/hdl/verilog/` 下已经是**综合后生成的大量 Verilog 文件**（如 `edge_canny_detector_fifo_w16_d2_S.v`、`..._mac_muladd_*.v` 等）。这是一个**已经综合成 IP** 的 Canny 边缘检测设计。

来看中值滤波项目的 README，它把 HLS 的目标和成果讲得很清楚：

[HLS/2D-median-filter-algorithm-HLS/README.md:1-3](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HLS/2D-median-filter-algorithm-HLS/README.md#L1-L3) —— 第 1 行注明了来源（`From -https://github.com/13hanu/...`，即这是个第三方引入的项目），第 3 行说明目标：用 HLS 实现二维中值滤波，**在 3 毫秒内完成去噪，且占用少于 25% 的 PL 资源**。

成果在结尾的 Results 一节：

[HLS/2D-median-filter-algorithm-HLS/README.md:48](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HLS/2D-median-filter-algorithm-HLS/README.md#L48-L48) —— 实际达到「12 毫秒内去噪、PL 资源占用约 13%」。

> 注意：3 ms 是目标，12 ms 是实际测得结果——这正是 HLS 工程师日常做的事：写 C → 看报告 → 加 pragma 优化 → 在时延和资源之间权衡。Unit 4 会专门讲这套优化思路。

#### 4.3.4 代码实践

**实践目标**：对比「设计阶段的 HLS」与「已综合成 IP 的 HLS」在目录长相上的巨大差异。

**操作步骤**：

1. 运行 `ls HLS/2D-median-filter-algorithm-HLS/`，你会看到简洁的 `.c`/`.h`/`.csv`/`.app` 文件。
2. 运行 `ls HLS/edge_canny_detector/IP/hdl/verilog/ | head -20`，你会看到几十个机器生成的 Verilog 文件，名字又长又规律（`fifo`、`mux`、`mul`、`mac_muladd` 等都是 HLS 生成的典型算子）。

**需要观察的现象**：前者像一份干净的 C 工程；后者像一份「爆炸」开的 RTL，文件数量极多。

**预期结果**：你直观体会到「同样的算法，手写 HLS C 只有几百行，综合出的 Verilog 却有几十个文件」——这就是 HLS 抽象层带来的「源码紧凑、产物庞大」特点。

#### 4.3.5 小练习与答案

**练习 1**：`HLS` 目录下哪个项目适合学「怎么用 C 写 HLS 算法」？哪个适合看「HLS 综合出来的 RTL 长什么样」？
> **答案**：学写算法看 `2D-median-filter-algorithm-HLS`（有干净 C 源码）；看综合产物看 `edge_canny_detector`（有完整生成 Verilog）。

**练习 2**：中值滤波项目 README 里，3 ms 和 13% 这两个数字分别代表什么？
> **答案**：3 ms 是最初设定的时延目标（实际做到约 12 ms）；13% 是最终 PL（可编程逻辑）资源占用率（目标是低于 25%，达到了）。

---

### 4.4 ThreePart 目录：第三方合集

#### 4.4.1 概念说明

`ThreePart`（三方）目录收录的是**来自外部作者/机构**的 FPGA 设计。它们不是仓库作者写的，而是为了方便学习集中放在一起。

阅读第三方合集时，要养成两个习惯：

1. **先看每个子项目自己的 README 和 LICENSE**，搞清楚来源和授权。
2. **注意分发形式**：有的是完整的源码目录，有的只是 `.zip` 压缩包，有的甚至只是单个 `.vhd`/`.vhd.txt` 片段——需要解压或补全才能用。

#### 4.4.2 核心流程

使用 `ThreePart` 里某个子项目的通用流程：

1. 读它的 README，确认用途、来源、许可证。
2. 如果是 zip，先解压查看内部结构。
3. 判断它是「可直接综合的源码」还是「需要放进 IP Catalog 的 IP」或「仅作参考的片段」。
4. 按需放入自己的工程（直接加源码，或作为 IP 仓库引用）。

#### 4.4.3 源码精读

`ThreePart` 下有 4 个子项目，性质各不相同：

**① `projf-explore` —— Project F FPGA 教程库（最有教学价值）**

这是来自 projectf.io 的开源 FPGA 教程合集，是整个 `ThreePart` 目录里最系统、最适合学习的部分。

[ThreePart/projf-explore/README.md:1](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/README.md#L1-L1) 开宗明义：`# Project F - FPGA Development`，是一个提供「开源设计供学习与二次开发」的小乐园。

它包含几大板块：一个可复用的 **Verilog 库（`lib/`）**、**FPGA Graphics（图形系列）**、**Maths（数学系列）**、**Hello（入门三部曲）**、**Demos（特效演示）**。其中 `lib/` 库又细分为 `clock`/`display`/`essential`/`graphics`/`maths`/`memory`/`uart` 七个分区——本手册的 Unit 5–7 几乎全部围绕这座「迷你 FPGA 大学」展开。

[ThreePart/projf-explore/README.md:11-15](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/README.md#L11-L15) 介绍了 Verilog Library 的定位：从帧缓冲、视频输出，到除法、开方、ROM/RAM，再到画圆，MIT 许证、可自由复用。

它还明确支持两种 FPGA 架构，体现「厂商中立」思想：

[ThreePart/projf-explore/README.md:66-79](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/README.md#L66-L79) —— 支持 Xilinx 7 系列（XC7：`BUFG`/`MMCME2_BASE`/`OBUFDS`/`OSERDES2`）和 Lattice iCE40（`SB_IO`/`SB_PLL40_PAD`/`SB_SPRAM256KA`）。

**② `ISOIEC18033-3StandardBlock` —— 标准分组密码 HDL 合集**

来自日本东北大学 Aoki 研究室的学术密码核心。

[ThreePart/ISOIEC18033-3StandardBlock/README.md:3](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/ISOIEC18033-3StandardBlock/README.md#L3-L3) ——「ISO/IEC 18033-3 标准分组密码 HDL 代码」；[第 7 行](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/ISOIEC18033-3StandardBlock/README.md#L7-L7)给出学术来源链接。用 `ls` 可见其下有 `AES`、`Camellia`、`DES`、`SEED`、`MISTY1`、`CAST128`、`RSA` 等算法目录，以及 `JWIS2007.zip`、`glitchy-clock_generator.zip` 两个压缩包。可与自研的 `HDL/AesCryptoCore` 做设计风格对比。

**③ `hardwarebee` —— 杂项开源 IP 集合**

[ThreePart/hardwarebee/README.md:1-4](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/hardwarebee/README.md#L1-L4) —— 只有一个网盘链接和一篇「IP 介绍」公众号文章作为索引。目录里既有 `.zip` 工程（如 `AES128.zip`、`cic_core.zip`、`dds_synthesizer.zip`、`Floating-Point-Multiplier-32-bit.zip`、`jpeg_latest.zip`），也有裸的 VHDL 片段（如 `spi_slave.vhd`、`fm.vhd`、`seven_segment.vhd.txt`、`square_root.vhd.txt`）。**形式最杂、文档最薄**，适合按需挑选，不适合系统学习。

**④ `digilent_ip` —— Digilent Vivado IP 库**

[ThreePart/digilent_ip/README.md:1-6](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/digilent_ip/README.md#L1-L6) —— 「Digilent Vivado library」，提供与 Xilinx Vivado IP Catalog 兼容的免费 IP 核和接口定义。它最典型的用途是驱动 Digilent 开发板的 **Pmod 扩展外设**（OLED 屏、按键键盘、导航传感器等）。

#### 4.4.4 代码实践

**实践目标**：给 `ThreePart` 的 4 个子项目做一次「来源/形式/可信度」评估。

**操作步骤**：

1. 逐一打开 4 个子项目的 README，记录它们的来源链接和许可证（projf 的 LICENSE 在仓库根附近，digilent/hardwarebee 通常只有简介）。
2. 运行 `ls ThreePart/ISOIEC18033-3StandardBlock/`，确认里面的算法目录和 zip 包。
3. 运行 `ls ThreePart/hardwarebee/`，数一数有几个 `.zip`、几个裸 VHDL 片段。

**需要观察的现象**：projf 文档最完善；ISO 合集以算法目录为主；hardwarebee 形式最杂；digilent 是规范的 IP 库。

**预期结果**：你能列出一张表，对每个子项目写出「来源 / 分发形式（源码 or zip or 片段）/ 是否适合系统学习」。

#### 4.4.5 小练习与答案

**练习 1**：如果你想系统学「Verilog 基础库模块（时钟、显示、内存、UART）」，`ThreePart` 里哪个子项目最合适？为什么？
> **答案**：`projf-explore`。它的 `lib/` 库分门别类、文档完善、MIT 许证，且明确支持 XC7 与 iCE40 两种架构，最适合系统学习。

**练习 2**：`hardwarebee` 目录里既有 `.zip` 又有 `.vhd.txt`，这说明什么？使用时应注意什么？
> **答案**：说明来源杂、分发形式不统一——zip 是完整工程需解压，`.vhd.txt` 往往只是贴出来的代码片段（用 `.txt` 后缀避免被工具直接当成源文件）。使用前必须先解压/补全，并核实来源与许可证，不能直接当生产级 IP 用。

---

## 5. 综合实践

把本讲学到的「三步法 + 三大目录」串起来，完成下面的任务。这是本讲的核心实践，请动手做并写下你的结论。

**任务**：浏览仓库根目录与三个顶层目录（`HDL` / `HLS` / `ThreePart`），完成两件事：

1. **用一句话分别概括**每个顶层目录的作用（例如：`HDL` = 自研 Verilog/VHDL 设计与 Vivado IP，含 AES 核心与一条视频链路）。
2. **选出你认为最适合作为入门的子项目，并写出理由。**（提示：可从「文档完善度、目录是否清晰、是否需要硬件、许可证」等角度比较。一般推荐 `ThreePart/projf-explore/hello` 或 `HLS/2D-median-filter-algorithm-HLS`，因为前者不依赖 IP 集成、后者能先跑 C 仿真。）

**建议产出**：一张表，列为「顶层目录 / 一句话作用 / 代表子项目 / 入门友好度（高/中/低）」，并在表下用 3–5 句话说明你的入门推荐与理由。

> 自检：如果你的「一句话作用」里能自然出现本讲讲过的关键词（自研 IP、HLS 高层综合、第三方合集、AXI、视频链路、Project F 库），说明你已经掌握了本讲的地图。

---

## 6. 本讲小结

- `suisuisi/FPGA_Library`（旧名 `Xilinx_Library`）是一个**聚合型 FPGA 设计合集**，根 README 极简，需结合目录结构与 git 历史来理解。
- 仓库由三大顶层目录构成：`HDL`（自研/整理的硬件描述语言 IP）、`HLS`（高层综合设计）、`ThreePart`（第三方合集）。
- `HDL` 收录 AES 密码核心以及一条「摄像头采集 → 颜色转换 → DVI 输出 + 动态时钟」的视频链路，其中 `AesCryptoCore_1.0` 最完整、最适合精读。
- `HLS` 展示了与 HDL 不同的范式：用 C 写算法、工具生成 Verilog；`2D-median-filter-algorithm-HLS` 是干净的设计阶段项目，`edge_canny_detector` 是已综合成 IP 的产物。
- `ThreePart` 收录 4 个来源各异的第三方合集：教学价值最高的是 `projf-explore`，另有学术密码合集、杂项 hardwarebee IP、Digilent Pmod IP 库。
- FPGA / HDL / IP / HLS 四个概念的关系：HDL 是描述电路的语言，IP 是打包复用的硬件模块，HLS 是用 C 自动生成电路的更高抽象，最终都产出加载到 FPGA 的 bitstream。
- 许可证需分清：根目录 MIT 只覆盖自研部分，各第三方子项目自带各自的许可证。

---

## 7. 下一步学习建议

本讲只画了「地图」，还没有进入任何一段真实代码。建议按下面的顺序继续：

1. **下一步必读**：Unit 1 第 2 讲《目录结构与模块地图》——会更细致地逐层拆解目录，帮你精确定位 AES、HLS 滤波、projf 库等关键源码所在路径。
2. **想了解 Vivado 工程怎么版本化**：Unit 1 第 3 讲《Vivado 工程模板与版本化工作流》，精读 `create_project.tcl` 与 cleanup 脚本。
3. **想直接啃硬核源码**：跳到 Unit 2，从 `HDL/AesCryptoCore_1.0` 的 `aes_top.v` 开始系统学 AES 数据通路——这是全手册的主线。
4. **配套阅读**：可浏览 [projf-explore 的总 README](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/README.md) 和 [projectf.io 网站](https://projectf.io)，建立对 Project F 教程体系的整体印象，为 Unit 5–7 做准备。
