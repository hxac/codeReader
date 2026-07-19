# 项目定位与 AERIS-10 雷达系统概览

> 本讲是 PLFM_RADAR 学习手册的第一篇。我们暂不读任何代码细节，而是先回答三个问题：
> 这个项目到底是什么？它背后用到了哪些雷达概念？它以什么许可证开源？
> 把这三件事弄清楚，后续读 FPGA、STM32、GUI 源码时你才不会迷路。

---

## 1. 本讲目标

学完本讲，你应当能够：

- 用一句话说清 **AERIS-10** 是什么，以及它的两个变体（AERIS-10N 与 AERIS-10E）在频率、距离、天线、发射功率上的差异。
- 理解 **PLFM/LFM 调制、脉冲压缩、相控阵电子扫描** 这几个核心雷达概念，并知道它们在本项目中分别由哪部分硬件负责。
- 说出本项目 **硬件用 CERN-OHL-P、软件用 MIT** 的双许可证模型，并能解释为什么硬件不能简单地沿用 MIT。
- 从 `README.md` 中独立提取出系统的主要子系统和关键规格。

---

## 2. 前置知识

本讲假设你具备：

- 基本的物理常识（光速、频率、波长）。
- 对「开源」这个词的直觉认识（源码/设计文件公开、可被他人使用与修改）。
- 会用文本编辑器或 `cat`/`less` 打开 Markdown 文件。

不需要你已经懂雷达、FPGA 或射频电路。本讲会从零解释必要的概念。下面这几个术语在文中会反复出现，先记个大概即可，后文会逐一展开：

| 术语 | 一句话解释 |
|------|------------|
| PLFM / LFM | 脉冲线性调频：一个脉冲内频率随时间线性扫过的信号，也叫 chirp（啁啾） |
| 相控阵 | 用多个天线单元 + 可编程相位差，实现「不动天线、电子转向」的天线阵列 |
| 脉冲压缩 | 用匹配滤波把长脉冲压成窄峰，兼顾探测距离与距离分辨率 |
| FPGA / STM32 | 分别做高速信号处理（FPGA）与系统管理（单片机）的两块芯片 |

---

## 3. 本讲源码地图

本讲不涉及复杂的代码逻辑，主要读懂以下四个「项目级」文件。它们是你认识 AERIS-10 的入口：

| 文件 | 作用 | 你要从中得到什么 |
|------|------|------------------|
| [README.md](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/README.md) | 项目主页，定位、规格、子系统、许可证、处理流水线全在这里 | 项目是什么、有哪些子系统、关键规格数字 |
| [1_Project_Description/Project_Description.docx](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/1_Project_Description/Project_Description.docx) | Word 格式的项目说明书（二进制文件） | 更详细的产品级说明，需用 Word/LibreOffice 打开阅读 |
| [Licence](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/Licence) | CERN-OHL-P（硬件许可证）的完整法律文本 | 硬件设计文件适用的开源许可证全文 |
| [docs/index.html](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/index.html) | GitHub Pages 文档站首页 | 项目当前的工程化状态（USB 方案、回归测试通过率等） |

> 说明：`Project_Description.docx` 是 Microsoft Word 2007+ 二进制文件，无法在终端直接 `cat` 阅读。本讲不臆测其内部文字；你需要本地用 Word 或 LibreOffice 打开它作为补充阅读。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**项目定位**、**雷达基础概念**、**许可证**。

### 4.1 项目定位：AERIS-10 到底是什么

#### 4.1.1 概念说明

仓库名叫 `PLFM_RADAR`，它是 **Pulse Linear Frequency Modulated Radar**（脉冲线性调频雷达）的缩写；而 **AERIS-10** 是这个产品/系统的代号。

一句话定位（来自 README 原文）：

> AERIS-10 is an open-source, low-cost 10.5 GHz phased array radar system featuring Pulse Linear Frequency Modulated (LFM) modulation.

翻译过来就是：AERIS-10 是一个**开源、低成本、工作在 10.5 GHz 的相控阵雷达**，采用脉冲线性调频（LFM）调制。

它有两个变体（variant），面向不同探测距离：

- **AERIS-10N（Nexus）**：3 km 量程，8×16 贴片天线阵，每通道约 1 W 发射功率。
- **AERIS-10E（Extended）**：20 km 量程，32×16 介质填充缝隙波导天线阵，每通道 10 W（GaN 功放）。

目标用户是研究者、无人机开发者、以及严肃的 SDR（软件无线电）爱好者。

#### 4.1.2 核心流程

从「拿到一份开源设计」到「雷达真正工作」，链路大致是：

```
开源仓库 (README + 设计文件 + 固件 + GUI)
        │
        ├── 硬件：4_Schematics and Boards Layout 下的 Gerber/BOM → 制板 → 采购元器件 → 焊接装配
        ├── 固件：9_Firmware 下的 FPGA bitstream + STM32 程序 → 烧录
        └── 软件：9_Firmware/9_3_GUI 下的 Python GUI → 运行
        │
        ▼
   雷达上电采集 → GUI 显示目标
```

也就是说，这个项目把**硬件设计、固件、上位机软件**三套东西一起开源了。本讲只关注「它是什么」，后续讲义会分别深入这三套。

#### 4.1.3 源码精读

README 标题就点明了项目性质——开源、脉冲线性调频、相控阵雷达：

- [README.md:7](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/README.md#L7) —— 标题 `AERIS-10: Open Source Pulse Linear Frequency Modulated Phased Array Radar`。

README 开头对项目的一句话定位与目标用户：

- [README.md:17](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/README.md#L17) —— 「open-source, low-cost 10.5 GHz phased array radar…Available in two versions (3km and 20km range)」，说明频率、双版本与受众。

两个版本的关键差异写在「Dual Version Availability」里：

- [README.md:28-30](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/README.md#L28-L30) —— AERIS-10N（3km，8×16 patch）与 AERIS-10E（20km，32×16 dielectric-filled slotted waveguide）。

> 注意：README 在规格表（见 4.2.3）里把 Extended 版记作 `AERIS-10X`，而在功能描述里记作 `AERIS-10E`。两个写法指同一个 Extended 版本，阅读时不要被绕晕。

#### 4.1.4 代码实践

**实践目标**：亲手从 README 中提取两个版本的差异，而不是凭记忆。

**操作步骤**：

1. 打开仓库根目录的 `README.md`。
2. 找到 `## 📊 Technical Specifications` 这一节（约第 122 行起）。
3. 对照其中的表格，把下面四个维度填进自己的笔记：

| 维度 | AERIS-10N（Nexus） | AERIS-10E / 10X（Extended） |
|------|--------------------|-----------------------------|
| 频率 | ？ | ？ |
| 最大距离 | ？ | ？ |
| 天线 | ？ | ？ |
| 发射功率 | ？ | ？ |

**需要观察的现象**：两个版本在「频率、波束转向、机械扫描、处理芯片」这几行是相同的——它们共享同一套 FPGA+STM32 处理平台，差异主要在**天线与发射功率**上。

**预期结果**：你会发现 Nexus 与 Extended 的真正区别是「轻量贴片阵列 + 1W 功放」对「大型缝隙波导阵列 + 10W GaN 功放」。这也解释了为什么 Extended 版能多出一个数量级的探测距离（3 km → 20 km）。

#### 4.1.5 小练习与答案

**练习 1**：项目代号 AERIS-10 和仓库名 PLFM_RADAR 各代表什么含义？

> **答案**：AERIS-10 是产品/系统代号；PLFM_RADAR 是 Pulse Linear Frequency Modulated Radar（脉冲线性调频雷达）的缩写，描述了它采用的调制方式。

**练习 2**：为什么 README 把 Extended 版有时写成 `AERIS-10E`、有时写成 `AERIS-10X`？

> **答案**：同一个 Extended 版本在不同章节用了不同后缀。功能描述里用 `AERIS-10E`，技术规格表里用 `AERIS-10X`，指代的是同一硬件变体。

---

### 4.2 雷达基础概念：PLFM 调制与相控阵

#### 4.2.1 概念说明

AERIS-10 的「PLFM + 相控阵」这两个词，浓缩了它最核心的两项技术。我们分别讲。

**(1) PLFM / LFM（脉冲线性调频，chirp）**

普通脉冲雷达靠发一个很短的高功率脉冲来测距：脉冲越短，距离分辨率越高。但短脉冲意味着峰值功率要很高，硬件吃不消。**线性调频（LFM/Chirp）** 的思路是：把脉冲拉长，但让脉冲内的频率随时间**线性**扫过一个带宽 \(B\)。这样既保留了平均能量（看得远），又能在接收端用「匹配滤波」把它压回一个窄峰（看得清）。

这个「拉长再压窄」的过程叫**脉冲压缩（Pulse Compression）**。

距离分辨率由信号带宽决定：

\[
\Delta R = \frac{c}{2B}
\]

其中 \(c\) 是光速，\(B\) 是 chirp 扫过的带宽。带宽越大，分辨越细。

**(2) 相控阵（Phased Array）与电子波束扫描**

传统雷达靠**机械转动**天线来改变照射方向。相控阵则用一排天线单元，每个单元前接一个**可编程相位器（phase shifter）**。给相邻单元设置一个递进的相位差，合成波束就会偏向某个方向——**无需任何机械运动**就能扫描，这叫电子波束转向（electronic beam steering）。

波束指向与相邻单元相位差 \( \Delta\varphi \) 的关系（单元间距 \(d\)，波长 \( \lambda \)）：

\[
d\sin\theta = \frac{\Delta\varphi}{2\pi}\lambda
\]

AERIS-10 在俯仰与方位两个方向都能电子扫描 ±45°，另外还能用步进电机做 360° 机械扫描。

**(3) 接收端的后续处理（先混个眼熟）**

回波信号回来后，FPGA 还会做：**Doppler 处理**（测速度）、**MTI**（动目标显示，滤掉静止杂波）、**CFAR**（恒虚警率检测，决定「这点是不是目标」）。本讲只需知道有这一串环节，具体源码在 U4 单元细讲。

#### 4.2.2 核心流程

把上面的概念串成一条完整的信号流水线（README 把它称为 Processing Pipeline）：

```
1. 波形生成      DAC 生成 LFM chirp
2. 上/下变频      LTC5552 混频器做频率搬移
3. 波束扫描       ADAR1000 相位器控制 16 个单元的相位 → 电子转向
4. 信号处理(FPGA) ADC 采样 → I/Q 下变频 → 抽取/滤波 → 脉冲压缩 → Doppler → MTI/CFAR
5. 系统管理(STM32) 电源时序、外设配置、GPS/IMU、步进电机
6. 可视化(GUI)    实时目标绘图、地图集成、雷达控制
```

关键直觉：**发射链**（DAC→混频→波束）和**接收链**（ADC→DDC→脉冲压缩→Doppler→CFAR）是镜像关系。ADAR1000 相位器既管发射波束也管接收波束。

#### 4.2.3 源码精读

README 的 Processing Pipeline 一节把整条链路列得很清楚：

- [README.md:97-118](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/README.md#L97-L118) —— 从 Waveform Generation 一路到 Visualization，对应上文流水线的 6 个阶段。

主板（Main Board）上承载了几乎所有关键有源器件，README 把它们逐个列出：

- [README.md:53-84](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/README.md#L53-L84) —— DAC（生成 chirp）、2× LTC5552 混频器、4× ADAR1000 四通道相位器、16× ADTR1107 前端芯片、XC7A50T FPGA、STM32F746xx 单片机及其挂载的外设。

两个版本的硬指标对照表：

- [README.md:122-132](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/README.md#L122-L132) —— Technical Specifications 表，频率 10.5 GHz、最大距离 3km/20km、±45° 电子转向、360° 机械扫描、发射功率 ~1W×16 与 10W×16（GaN）。

文档站首页还透露了项目当前的工程化状态（USB 方案选择、回归通过率），有助于你判断项目成熟度：

- [docs/index.html:35-48](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/index.html#L35-L48) —— 例如「50T 生产板用 FT2232H（USB 2.0）」「MCU 15/15、FPGA 18/18 回归通过」。

#### 4.2.4 代码实践

**实践目标**：用两个简单计算，把「10.5 GHz」「1W vs 10W」这些规格数字变成直觉。

**操作步骤**：

1. 算波长。光速 \(c \approx 3\times10^8\) m/s，频率 \(f = 10.5\) GHz。代入 \( \lambda = c/f \)。
2. 算功率比。Nexus 每通道 ~1 W，Extended 每通道 10 W（GaN 功放 QPA2962）。

**需要观察的现象**：

- 波长应当落在 2.8 cm 左右——这正是为什么天线单元可以做得比较小、一块板上能塞下 8×16 甚至 32×16 个单元。
- Extended 版单通道功率是 Nexus 的 10 倍，加上更大的天线阵列，两者共同贡献了 3 km → 20 km 的距离提升。

**预期结果**：

\[
\lambda = \frac{3\times10^8}{10.5\times10^9} \approx 0.0286\,\text{m} \approx 2.86\,\text{cm}
\]

功率比 \( 10/1 = 10\times \)（每通道）。这个练习纯算术，本地用计算器即可验证，不需要运行任何代码。

#### 4.2.5 小练习与答案

**练习 1**：为什么 PLFM 雷达要「把脉冲拉长再压窄」，而不是直接发一个极短的脉冲？

> **答案**：短脉冲虽然距离分辨率好，但要求极高的峰值功率，硬件难以承受。LFM chirp 把能量铺在较长的时间里（平均功率可控），接收端再用匹配滤波（脉冲压缩）压回窄峰，从而兼顾「看得远」和「看得清」。

**练习 2**：相控阵是如何做到「不动天线就能改变照射方向」的？

> **答案**：每个天线单元前都有可编程相位器（AERIS-10 用 ADAR1000）。给相邻单元设置递进的相位差，合成波束就会按 \( d\sin\theta = (\Delta\varphi/2\pi)\lambda \) 偏向相应方向，从而实现电子波束转向。

**练习 3**：在 README 的处理流水线里，Doppler、MTI、CFAR 分别解决什么问题？

> **答案**：Doppler 处理测目标速度；MTI 滤除静止杂波（如地面）；CFAR 用恒虚警率准则判定某个采样点是否真的是目标。

---

### 4.3 许可证：硬件 CERN-OHL-P 与软件 MIT

#### 4.3.1 概念说明

AERIS-10 的开源方式有一个**容易踩坑**的设计：它对硬件和软件使用**两套不同的许可证**。

- **硬件设计文件**（原理图、PCB、Gerber、BOM、机械图纸）→ **CERN-OHL-P**（CERN 开放硬件许可证 v2，宽松版）。
- **软件与固件**（FPGA 的 Verilog/VHDL、STM32 固件、Python GUI）→ **MIT 许可证**。

为什么硬件不直接用 MIT？README 给出了明确理由：MIT 缺少物理硬件需要的法律保护。CERN-OHL-P 专门为硬件设计，特点包括：

- 明确定义「Hardware / Documentation / Product」等概念；
- 为贡献者和用户提供**明确的专利保护**；
- 提供**更强的责任限制**（对高功率射频很重要——雷达涉及 10 W 量级的 GaN 功放，安全责任不可忽视）；
- 与 CERN、OSHWA 等专业开源硬件标准对齐。

项目最初全部使用 MIT，后来在社区（特别感谢 gmaynez）建议下，把硬件部分切换到了 CERN-OHL-P。

#### 4.3.2 核心流程

判断「某个文件适用哪个许可证」的决策树：

```
这个文件是什么？
        │
        ├── 是硬件设计? (原理图 .sch / PCB .brd / Gerber / BOM / 机械图纸)
        │       └─► CERN-OHL-P  (见 Licence 文件全文)
        │
        └── 是软件/固件? (FPGA Verilog/VHDL / STM32 C/C++ / Python)
                └─► MIT
```

两个许可证都允许你使用、修改、甚至商业销售衍生品，但 CERN-OHL-P 要求：保留版权声明、修改后的设计仍以相同许可证发布、并以 Source 格式提供你的修改。

#### 4.3.3 源码精读

README 顶部用 badge 一眼标明双许可证：

- [README.md:4-5](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/README.md#L4-L5) —— `License: MIT` 与 `Hardware: CERN-OHL-P` 两个徽章。

README 的 License 小节详细说明了哪些产物适用哪个许可证，以及切换原因：

- [README.md:166-203](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/README.md#L166-L203) —— 硬件用 CERN-OHL-P 的四点理由，以及「Why This Change?」解释从 MIT 迁移到 CERN-OHL-P 的来龙去脉。

`Licence` 文件是 CERN-OHL-P 的完整法律文本，开头 preamble 就点明了它的定位——「为硬件设计而生的宽松许可证」：

- [Licence:1-15](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/Licence#L1-L15) —— CERN Open Hardware Licence Version 2 - Permissive 的前言，说明它聚焦于「设计本身」与署名/再分发义务。

#### 4.3.4 代码实践

**实践目标**：把「双许可证」从抽象概念变成可操作的判断能力。

**操作步骤**：

1. 打开 `Licence` 文件，确认它的第一行是 `CERN Open Hardware Licence Version 2 - Permissive`。
2. 打开 README 的 License 小节（约 166 行起），阅读它列出的「硬件设计文件包括哪些」与「软件包括哪些」。
3. 对仓库里下面这几类文件，判断它们各自适用哪个许可证，填入下表：

| 文件类别 | 仓库中的例子 | 适用许可证 |
|----------|--------------|------------|
| PCB 生产文件 | `4_Schematics and Boards Layout/4_7_Production Files/` 下的 Gerber/BOM | ？ |
| FPGA Verilog | `9_Firmware/9_2_FPGA/*.v` | ？ |
| STM32 固件 | `9_Firmware/9_1_Microcontroller/` 下的 C/C++ | ？ |
| Python GUI | `9_Firmware/9_3_GUI/*.py` | ？ |

**需要观察的现象**：你会清楚看到「硬件类产物 = CERN-OHL-P」「代码类产物 = MIT」的整齐划分。

**预期结果**：前两类（PCB、机械图）属于硬件设计 → CERN-OHL-P；后三类（Verilog、C/C++、Python）属于软件/固件 → MIT。

> 说明：判断结果可完全由 README 的 License 小节推导得出，不需要运行命令。如果你将来要给本项目提 PR，请按这个划分选择对应许可证的义务。

#### 4.3.5 小练习与答案

**练习 1**：为什么 AERIS-10 的硬件部分要从 MIT 切换到 CERN-OHL-P？

> **答案**：因为 MIT 缺少物理硬件所需的法律保护。CERN-OHL-P 专为硬件设计，明确区分 Hardware/Documentation/Product，提供专利保护与更强的责任限制（对高功率射频尤其重要），并与专业开源硬件标准对齐。

**练习 2**：你把主板原理图改了一处布局并公开发布，需要遵守什么义务？

> **答案**：硬件设计受 CERN-OHL-P 约束，你需要：保留原始版权声明、以相同许可证（CERN-OHL-P）发布修改后的设计，并以 Source 格式公开你的修改。

**练习 3**：FPGA 的 Verilog 代码适用哪个许可证？为什么它和 PCB 不一样？

> **答案**：适用 MIT。README 把软件与固件（包括 FPGA 的 Verilog/VHDL、STM32 固件、Python GUI）整体归为软件类，用 MIT 以获得最大灵活性；而 PCB 等硬件设计文件用 CERN-OHL-P 以获得硬件所需的额外法律保护。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「项目速写」小任务。它不需要你运行任何代码，只需要阅读与归纳。

**任务背景**：假设你要向一位没接触过雷达的同事用 5 分钟介绍 AERIS-10，并回答他对许可证的疑问。

**操作步骤**：

1. **定位**：用一句话告诉对方 AERIS-10 是什么（频率、调制方式、阵列类型、两个版本）。
2. **对比**：写一段话（约 100 字）说明 AERIS-10N 与 AERIS-10E 在「频率、最大距离、天线、发射功率」四个维度上的差异，并解释为什么 Extended 版能看得更远（提示：天线面积 + 功放功率）。
3. **直觉**：算出 10.5 GHz 对应的波长（约 2.86 cm），用它解释为什么板子上能放下 8×16 / 32×16 个天线单元。
4. **许可证**：对方问「我能不能拿这套设计做成产品卖？」——结合 CERN-OHL-P（硬件）与 MIT（软件）回答他可以，但需要履行哪些义务（保留版权、相同许可证发布硬件修改、以 Source 格式公开）。

**预期产出**：一份约 300 字的中文「项目速写」，涵盖定位、两版差异、波长直觉、许可证义务四个要点。写完后回头对照本讲 4.1–4.3，检查是否有遗漏或错误。

> 如果你想验证自己的理解是否准确，可以把这份速写的关键数字（10.5 GHz、3km/20km、1W×16/10W×16、±45°）与 README 的 Technical Specifications 表（[README.md:122-132](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/README.md#L122-L132)）逐一核对。

---

## 6. 本讲小结

- AERIS-10 是一个开源、低成本、10.5 GHz 的相控阵雷达，采用脉冲线性调频（PLFM/LFM）调制；仓库名 `PLFM_RADAR` 即由此而来。
- 它有两个变体：Nexus（3km，8×16 贴片阵，~1W×16）与 Extended（20km，32×16 缝隙波导阵，10W×16 GaN 功放），共享同一套 FPGA+STM32 处理平台。
- 信号流水线是「DAC 生成 chirp → 混频 → ADAR1000 电子波束扫描 → ADC → FPGA 处理（脉冲压缩/Doppler/MTI/CFAR）→ GUI 显示」，发射链与接收链镜像对称。
- 项目采用双许可证：硬件设计文件用 CERN-OHL-P（为硬件提供专利保护与责任限制），软件与固件用 MIT（最大灵活性）。
- 距离分辨率由带宽决定 \( \Delta R = c/(2B) \)；波束指向由相邻单元相位差决定 \( d\sin\theta = (\Delta\varphi/2\pi)\lambda \)。10.5 GHz 对应波长约 2.86 cm。
- README 的 Technical Specifications 表和 docs 站点是后续随时核对硬指标的最佳入口。

---

## 7. 下一步学习建议

本讲只建立了「项目是什么」的整体认知。建议接下来按这个顺序继续：

1. **先看仓库结构**：进入下一讲 **u1-l2（仓库目录结构与文件放置策略）**，搞清各编号目录（`1_`…`9_`）的职责与「哪些产物该入库、哪些该 gitignore」，这决定了你以后找文件的速度。
2. **再看入口文件**：u1-l3 会指出 FPGA / STM32 / GUI 三层的入口文件，是从「认识项目」过渡到「读源码」的桥梁。
3. **建立端到端地图**：U2 单元（系统架构与端到端数据流）会把本讲的流水线展开成具体模块，之后你就可以按信号流向逐层深入 FPGA 接收链（U4）等核心模块。
4. **补充阅读**：如果想看更详细的产品级说明，用 Word/LibreOffice 打开 `1_Project_Description/Project_Description.docx`；想看当前工程状态可浏览 [docs/index.html](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/index.html)。
