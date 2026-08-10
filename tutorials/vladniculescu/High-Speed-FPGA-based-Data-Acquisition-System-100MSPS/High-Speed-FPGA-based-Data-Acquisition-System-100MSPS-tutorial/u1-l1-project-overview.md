# 项目总览：它是什么

## 1. 本讲目标

本讲是整本学习手册的第一篇。读完本讲，你应该能够：

- 用一句话说清楚这个项目「是什么」——它的定位、它的三大功能。
- 说清楚每一项功能分别依赖哪一部分硬件（模拟前端板 / ADC / FPGA / 上位机 GUI）。
- 认出目标开发板 Nexys 4 DDR 和它使用的 Vivado 工具链版本。
- 了解作者团队与它背后的 Xilinx 竞赛背景（XIL-23991）。

本讲只讲「这是什么」，不讲任何具体代码实现。代码细节从下一讲（仓库结构）开始，再后面才会进入 Verilog/VHDL 源码。

## 2. 前置知识

本讲面向零基础读者，但有几个名词先解释清楚会更好理解：

- **DAQ（Data Acquisition，数据采集）**：把现实世界的模拟信号（电压、光、皮肤电阻等）转成数字数据，再交给计算机处理。一个 DAQ 系统通常包含：传感器 → 模拟调理电路 → ADC → 数字处理器 → 上位机。
- **FPGA（Field-Programmable Gate Array，现场可编程门阵列）**：一种可以在出厂后由用户重新「画电路」的芯片。和写软件不同，给 FPGA 写代码（Verilog/VHDL）本质上是在描述硬件电路。
- **ADC（Analog-to-Digital Converter，模数转换器）**：把连续的模拟电压采样成一串数字。本项目用的 ADC 型号是 AD9215，最高采样率可达 100 MSPS（每秒一亿次采样）。
- **MSPS（Mega-Samples Per Second，百万次采样每秒）**：采样率单位。100 MSPS = 100,000,000 次/秒。
- **FFT（Fast Fourier Transform，快速傅里叶变换）**：把时域的波形（横轴是时间）转成频域的频谱（横轴是频率），从而看出信号里包含哪些频率成分。
- **Nexys 4 DDR**：Digilent 公司出品的一款 FPGA 学习开发板，板载一颗 Xilinx Artix-7 FPGA，本项目就跑在这块板上。
- **Vivado**：Xilinx 官方的 FPGA 开发软件，用来综合、实现并生成烧到板子里的「比特流（bitstream）」。
- **LabVIEW**：NI（National Instruments）的图形化编程环境，本项目用它写了运行在 PC 上的上位机界面（GUI）。

如果上面某些词暂时记不住没关系，后面用到时会反复出现。

## 3. 本讲源码地图

本讲只依赖一个文件，它是整个项目唯一的「说明书」：

| 文件 | 作用 |
| --- | --- |
| [readme.md](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/readme.md) | 项目说明：团队信息、开发板、工具链版本、项目简介、目录结构说明、复现三步法、演示视频链接。 |

> 说明：本仓库的真正「源码」是 Verilog / VHDL 文件，但它们从下一讲才开始介绍。本讲的全部信息都来自上面这份 README。

## 4. 核心概念与源码讲解

本讲把 README 拆成两个最小模块来读：先看「项目定位与三大功能」，再看「开发板、工具链与团队背景」。

### 4.1 项目定位与三大功能

#### 4.1.1 概念说明

这个项目的全名是 **High speed DAQ system with EDA and EKG extension**（带 EDA 与 EKG 扩展的高速数据采集系统）。其中：

- **EDA**（Electrodermal Activity，皮电活动）≈ 本项目里的 **GSR（Galvanic Skin Response，皮电响应）**，用于测量皮肤电阻变化，常用于情绪/应激监测。
- **EKG**（心电图）在本项目里以「心率测量」的形式实现，采用的是 **plethysmograph（体积描记法）**——一种通过检测指尖血流变化来推算心率的方法。

README 把它概括为一句话：这是一个用 FPGA 实现的高速数据采集系统，集成了 **三个功能**：

1. **示波器**：带 FFT（频谱分析）能力，最高采样率 100 MSPS。
2. **心率测量模块**：基于体积描记法。
3. **皮电响应（GSR）模块**。

把这三件事放在一起，是一个典型的「一个硬件平台跑多种测量」的设计。FPGA 提供高速、确定时序的数据处理能力，三种测量共用同一套 ADC 采样 + FPGA 处理 + 上位机显示的骨架，只是模拟前端（传感器和调理电路）不同。

#### 4.1.2 核心流程

虽然本讲不深入代码，但可以用一个高层的数据流图先建立直觉。整个系统的信号走向大致如下：

```
传感器(探针/光电/电极)
      │  模拟信号
      ▼
模拟前端板 (Oscilloscope / Heartbeat / 等 PCB)   ← 调理、放大
      │  模拟电压
      ▼
ADC (AD9215, 最多 100 MSPS, 10 位)              ← 模拟转数字
      │  10 位并行数字
      ▼
FPGA (Nexys 4 DDR 上的 Artix-7)                 ← 采集 + FFT + 幅度计算
      │  串口数据帧
      ▼
USB↔串口桥 (MCP2200 板)                          ← 转成 USB
      │
      ▼
PC 上的 LabVIEW GUI                              ← 显示波形 / 频谱 / 数值
```

要点：

- 三大功能里，**示波器**是「主菜」，它的完整数字处理链（采样→存储→FFT→幅度→开方→上传）都在 FPGA 里实现，后面整本手册大部分篇幅都在讲它。
- **心率**和 **GSR** 的「特色电路」主要在模拟前端板（PCB）上，而不是在 FPGA 代码里。它们复用示波器的 ADC + FPGA + GUI 通路，只是传感方式和最终显示不同。这一点很关键，相关模拟电路细节属于 PCB 设计范畴，本手册（以 HDL 源码为主）会如实标注「待确认」。

#### 4.1.3 源码精读

README 里对项目的官方一句话描述在第 18–20 行：

[readme.md:18-20](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/readme.md#L18-L20)

> 这段就是上面 4.1.1 里转述的那句话：项目把示波器（FFT + 100 MSPS）、心率（体积描记法）、GSR 三个功能集成进一个系统。

README 还给出了「复现项目」的三步说明（第 28–34 行），这三步正好对应上面数据流图里硬件链的三个环节：

[readme.md:28-34](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/readme.md#L28-L34)

- **Step 1**：把 bitstream（比特流，即 `FPGA bit file/TOP.bit`）烧进 Nexys 4 板子 → 对应 FPGA 这一环。
- **Step 2**：复现 PCB 板并连到 Nexys 板 → 对应模拟前端板 + ADC 这一环。
- **Step 3**：打开 GUI、把板子连到 PC → 对应上位机显示这一环。

最后，README 第 36–37 行留了一个 YouTube 演示视频链接，可以直观看到系统跑起来的样子：

[readme.md:36-37](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/readme.md#L36-L37)

#### 4.1.4 代码实践

> **实践类型：源码阅读 + 归纳**（本讲还没有可运行的代码，因此做阅读归纳型实践）

1. **实践目标**：亲手从 README 里提炼出一段不超过 150 字的项目摘要，并搞清楚三大功能各自靠什么硬件实现。
2. **操作步骤**：
   - 重新通读一遍 [readme.md](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/readme.md) 的第 17–34 行。
   - 用自己的话写一段不超过 150 字的摘要，要求包含：① 是什么硬件平台；② 集成了哪三个功能；③ 数据大致怎么流向 PC。
   - 填下面这张「功能 → 依赖硬件」的对照表。
3. **需要观察的现象**：你会注意到示波器这一列「FPGA」相关的处理最重（FFT 等），而心率 / GSR 的特色主要落在「模拟前端板」上。
4. **预期结果**：对照表大致如下（请先自己填，再对答案）。

   | 功能 | 模拟前端板 | ADC | FPGA 处理 | PC GUI |
   | --- | --- | --- | --- | --- |
   | 示波器（含 FFT，100 MSPS） | Oscilloscope_board | AD9215 | 采样+FFT+幅度+开方 | LabVIEW 画波形/频谱 |
   | 心率测量（体积描记法） | Heartbeat_measurement_board | AD9215 | 采集+上传 | LabVIEW 显示心率 |
   | 皮电响应 GSR | （模拟前端板，具体板待确认） | AD9215 | 采集+上传 | LabVIEW 显示数值 |

   > 注：GSR 对应的专用 PCB 在本仓库中需要进一步确认，上表「模拟前端板」一列对 GSR 标注为「待确认」。三种功能复用同一套 ADC + FPGA + GUI 通道，这一点是确定的。

5. 本实践无需运行任何命令，属于纯阅读与归纳。摘要与表格的「准确版」就是 4.1.3、4.1.2 的内容。

#### 4.1.5 小练习与答案

**练习 1**：项目名里的 "EDA" 和 "EKG" 分别对应 README 简介里的哪两个功能？

> **答案**：EDA 对应 GSR（皮电响应）模块；EKG 在本项目里以「心率测量」的形式实现，采用的是体积描记法（plethysmograph），而不是传统的心电图电极接法。

**练习 2**：README 给出的复现「三步」分别对应数据流里的哪三个环节？

> **答案**：Step 1 烧 bitstream → FPGA 环节；Step 2 复现并连接 PCB → 模拟前端板 + ADC 环节；Step 3 打开 GUI 连 PC → 上位机显示环节。

**练习 3**：为什么说心率 / GSR 的「特色」主要不在 FPGA 代码里？

> **答案**：因为三种测量共用同一套 ADC → FPGA → GUI 的数字通路，心率和 GSR 区别于示波器的地方主要在于传感器和模拟调理电路（在 PCB 上）。FPGA 代码里更多是实现通用的「高速采集 + FFT + 上传」骨架。

### 4.2 开发板、工具链与团队背景

#### 4.2.1 概念说明

光知道「做什么」还不够，还得知道「在什么上做、谁做的」。这一节读 README 头部的元信息（team number、开发板、Vivado 版本、团队成员），这些信息决定了你能否复现这个工程：

- **开发板**决定了 FPGA 的型号、外设（晶振频率、可用 IO、板载外设）。
- **Vivado 版本**非常重要——Xilinx 工程（尤其是含 IP 核的工程）在不同 Vivado 版本之间不一定兼容。本项目同时给出了「推荐版本」和「实际使用版本」，下面会特别说明。
- **团队与竞赛编号**帮助你理解项目来源和背景。

#### 4.2.2 核心流程

复现一个 FPGA 工程的环境准备流程通常是：

```
确认开发板型号 (Nexys 4 DDR)
      │
      ▼
确认 Vivado 版本 (本项目: 实际用 2016.1)
      │
      ▼
打开/重建工程 (Vivado Project.rar)
      │
      ▼
重新生成 IP 核 (FFT/乘法/加法/CORDIC/PLL)
      │
      ▼
综合→实现→生成 bitstream → 烧板
```

这里要特别小心的一点：仓库里的 `Vivado Project.rar` 是一份完整的 Vivado 工程，里面包含 Xilinx IP 核。换一个 Vivado 版本打开时，IP 核很可能需要「upgrade」或重新生成，否则综合会报错。这就是为什么 README 要写清楚版本。

#### 4.2.3 源码精读

README 第 1–8 行是项目的「身份证」和指导老师信息：

[readme.md:1-8](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/readme.md#L1-L8)

> 关键信息：竞赛/团队编号 **XIL-23991**；项目日期 2016-06-29；来自两所罗马尼亚高校（布加勒斯特理工大学 与 雅西「格奥尔基·阿萨基」技术大学）；指导老师 Vlad-Mihai Placinta。

第 9–13 行是两位参赛学生：

[readme.md:9-13](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/readme.md#L9-L13)

> 两位参赛者：Vlad Niculescu 与 Ovidiu Emanuel Hutanu（仓库作者 vladniculescu 即前者）。这也是为什么仓库里能看到 `aux_vlad.vhd` 这样带名字的文件。

最关键的两行是开发板与工具链版本（第 15–16 行）：

[readme.md:15-16](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/readme.md#L15-L16)

> 解读：
> - **Board used: Nexys 4 DDR** —— 目标板是 Nexys 4 DDR，板载 Xilinx Artix-7 FPGA。本手册后面所有时钟（100 MHz 晶振）、UART 波特率、RAM 容量等讨论都基于这块板。
> - **Vivado Version (preferably 2015.4): 2016.1** —— 这行写法值得注意：括号里 `preferably 2015.4` 是「建议」版本，而冒号后的 `2016.1` 才是**作者实际使用的版本**。也就是说，这份工程是在 Vivado **2016.1** 下开发和提交的。如果你要重新打开 `Vivado Project.rar`，用 2016.1 最稳妥；用别的版本则要做好升级 IP 核的心理准备。

至于 XIL-23991 这个编号：这是 **Xilinx（现 AMD）举办的 FPGA 设计竞赛**给参赛队伍分配的团队编号。理解这一点，就理解了项目的「出身」——它是一份竞赛参赛作品，因此在工程整洁度、文档完整性上带有学生作品的特点（例如目录名带空格、混用 Verilog 与 VHDL 等），后面阅读源码时要有这个心理预期。

#### 4.2.4 代码实践

> **实践类型：环境信息核查（源码阅读型）**

1. **实践目标**：确认你能否在本机复现这个工程，搞清楚需要哪些软硬件条件。
2. **操作步骤**：
   - 打开 [readme.md:15-16](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/readme.md#L15-L16)，记下开发板和 Vivado 版本。
   - 检查你本机：① 有没有 Nexys 4 DDR 实体板（没有也能继续学源码，只是不能上板验证）；② 有没有装 Vivado，版本是多少。
   - 在仓库根目录用文件浏览器查看 `Vivado Project.rar` 是否存在（它在 `git ls-files` 的输出里能看到）。
3. **需要观察的现象**：你会注意到 README 同时写了 2015.4（preferably）和 2016.1（实际）两个版本号。
4. **预期结果**：得出结论——本工程实际开发环境是 **Vivado 2016.1 + Nexys 4 DDR**。如果你手上没有这块板或这个版本，本手册的学习方式应以「读源码 + 理解原理」为主，对应「待本地验证」的环节会在后续讲义里明确标注。
5. 无法确定能否在你的具体 Vivado 版本下直接打开工程，相关结论标注为：换版本时 IP 核可能需要重新生成，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：README 里 Vivado 版本那一行写了两个数字 `2015.4` 和 `2016.1`，哪个是作者实际使用的版本？

> **答案**：`2016.1`。括号里的 `preferably 2015.4` 是建议版本，冒号后的 `2016.1` 才是实际开发版本。

**练习 2**：XIL-23991 是什么？

> **答案**：是 Xilinx FPGA 设计竞赛分配给这支参赛队伍的团队编号。它说明本项目是一份竞赛参赛作品。

**练习 3**：为什么 FPGA 工程对 Vivado 版本这么敏感？

> **答案**：因为工程里包含 Xilinx IP 核（FFT、乘法器、加法器、CORDIC 开方、PLL 等）。这些 IP 核由 Vivado 版本相关的工具生成，跨版本打开时往往需要 upgrade 或重新生成，否则综合会失败。

## 5. 综合实践

把本讲学到的所有信息整合成一张「项目名片」：

> 请用一份不超过 10 行的文档介绍这个项目，至少包含以下要素：
> 1. 项目全名（中英文）与一句话定位。
> 2. 目标开发板 + 实际使用的 Vivado 版本。
> 3. 三大功能，以及各自依赖的硬件链（模拟前端板 / ADC / FPGA / PC GUI）。
> 4. 团队编号 XIL-23991 与两位作者。
> 5. 复现三步法。
>
> 完成后，对照本讲 4.1 与 4.2 节自查：你是否漏掉了「心率/GSR 主要在模拟前端、而非 HDL」这个关键点？这个判断会直接影响你后续读源码时把注意力放在哪里（应放在示波器的 DSP 链路上）。

这个综合实践没有标准答案需要运行，它的目的是让你在进入下一讲（仓库结构与具体源码）之前，先在大脑里建立一个稳定的「项目全景图」。

## 6. 本讲小结

- 这是一个用 **Nexys 4 DDR（Xilinx Artix-7）** 实现的高速数据采集系统，实际开发环境是 **Vivado 2016.1**。
- 它集成三大功能：**示波器（含 FFT，最高 100 MSPS）**、**心率测量（体积描记法）**、**皮电响应 GSR**。
- 三大功能共用 ADC（AD9215）→ FPGA → 上位机 的骨架；示波器的数字处理最重，心率/GSR 的特色主要在模拟前端 PCB。
- 复现分三步：烧 bitstream（`FPGA bit file/TOP.bit`）→ 连 PCB 板 → 打开 LabVIEW GUI。
- 项目来自 Xilinx 竞赛，团队编号 **XIL-23991**，作者是 Vlad Niculescu 与 Ovidiu Emanuel Hutanu。
- 本讲信息全部来自仓库根目录的 [readme.md](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/readme.md)。

## 7. 下一步学习建议

现在你已经知道「这是什么」，下一步应该看「它由哪些文件组成、怎么组织」。建议继续学习：

- **u1-l2 仓库结构与复现方式**：列出 Verilog / VHDL / 比特流 / LabVIEW / PCB 各目录的内容，识别可读源码与二进制产物，并讲解混用 Verilog 与 VHDL 的工程组织方式。
- **u1-l3 系统总体架构与数据流**：基于顶层 `TOP.v` 画出 ADC→RAM→FFT→幅度→开方→UART 的完整数据流框图——这是后续所有源码讲义的「地图」。

读这两讲时，建议把本讲的「项目名片」放在手边对照，每当看到具体模块，就问一句：它在数据流图的哪一环？
