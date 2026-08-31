# LibreVNA 是什么：项目定位与整体架构

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 LibreVNA 的三大组成部分——PC 端 GUI 应用、STM32 固件、FPGA bitstream——以及各自的职责。
2. 对照 RF 框图（RFBlockdiagram.svg），按顺序描述信号从激励源出发、经过功分器、端口开关、定向电桥、两级下变频混频器，最终到达 ADC 的完整路径。
3. 指出哪个频率段由 Si5351C 负责、哪个频率段由 MAX2871 负责，并解释为什么需要两颗频率源。
4. 理解本项目最核心的架构取舍："PCB 只是射频前端，其余处理全部在 PC 端完成"。

本讲不要求你拥有 LibreVNA 硬件，也不要求你能编译代码——所有实践都可以通过阅读文档和浏览仓库完成。

## 2. 前置知识

本讲会用到的概念都在这里用通俗语言解释一遍。已经熟悉的读者可以快速跳过。

### 2.1 什么是矢量网络分析仪（VNA）

VNA（Vector Network Analyzer，矢量网络分析仪）用来测量一个射频网络（也就是被测件 DUT，Device Under Test）在不同频率下对信号的影响。它测量的是 **S 参数（散射参数）**：

- \( S_{11} = \dfrac{b_1}{a_1} \)：端口 1 的反射系数——射进端口 1 的信号有多少被反射回来。
- \( S_{21} = \dfrac{b_2}{a_1} \)：端口 1 到端口 2 的传输系数——信号穿过被测件后剩多少。

其中 \( a_1 \) 是入射波，\( b_1 \)、\( b_2 \) 是反射波和出射波。"矢量"意味着同时测幅度和相位（是一个复数），而不只是功率。要得到比值 \( b/a \)，仪器必须**同时测量入射波和出射波**——这正是 LibreVNA 里"参考接收机 + 两个端口接收机"共三条接收链路的由来。

### 2.2 dBm、混频器与中频（IF）

- **dBm**：以 1 毫瓦为基准的功率单位。-10 dBm 约 0.1 mW，-42 dBm 约 63 nW。LibreVNA 的激励功率大约在 -42 到 -10 dBm 之间可调。
- **混频器（mixer）**：一个能把两个输入频率相加减的器件。输入射频 \( f_{RF} \) 和本振 \( f_{LO} \)，输出中频 \( f_{IF} = |f_{RF} - f_{LO}| \)。
- **中频（IF，Intermediate Frequency）**：把高频信号搬到低频再处理叫"下变频"。在固定低频上做滤波和放大比在几 GHz 上做容易且便宜得多。
- **ADC 采样**：LibreVNA 的 ADC 以 16 位分辨率、800 kHz 速率采样最终中频。由奈奎斯特定理，采样率必须高于信号最高频率的两倍：\( f_s > 2 f_{max} \)。最终中频只有 250 kHz，800 kHz 的采样率满足 \( 800\,\text{kHz} > 2 \times 250\,\text{kHz} \)，留有余量。

### 2.3 定向电桥 / 回波损耗桥（RLB）

测量反射系数需要把"入射波"和"反射波"分开。传统仪器用定向耦合器（directional coupler），但很难做到 100 kHz～6 GHz 这么宽的带宽。LibreVNA 改用**电阻式回波损耗桥（resistive return-loss bridge）**——由电阻构成的惠斯通电桥结构，带宽极宽、实现简单，代价是插入损耗较大。README 中明确说明了这一选择。

### 2.4 FPGA、MCU、bitstream

- **MCU（微控制器）**：LibreVNA 用的是 STM32G431，跑 FreeRTOS，负责 USB 通信和调度。
- **FPGA（现场可编程门阵列）**：LibreVNA 用的是 Xilinx Spartan 6 (XC6SLX9)。它是一块"可以用代码重新连线"的数字芯片，适合并行、精确时序的任务（如驱动 ADC、做 DSP）。
- **bitstream**：FPGA 的"程序"，即描述芯片内部连线的二进制文件。本项目中 FPGA 逻辑用 VHDL 语言编写。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/README.md) | 项目门面：安装方式、"How does it work" 架构总述（本讲最重要的文字材料） |
| [Documentation/DeveloperInfo/RFBlockdiagram.svg](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/RFBlockdiagram.svg) | 射频部分框图（图片，用浏览器/GitHub 直接查看） |
| [Documentation/DeveloperInfo/RFBlockdiagram.tex](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/RFBlockdiagram.tex) | 上面 SVG 的 LaTeX/TikZ 源码，框图里每个器件都在这里有对应代码，方便精确引用 |
| [Documentation/DeveloperInfo/DigitalBlockdiagram.svg](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/DigitalBlockdiagram.svg) | 数字部分框图（图片） |
| [Documentation/DeveloperInfo/DigitalBlockdiagram.tex](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/DigitalBlockdiagram.tex) | 数字框图的 TikZ 源码 |
| [Documentation/DeveloperInfo/PowerBlockdiagram.svg](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/PowerBlockdiagram.svg) | 电源部分框图（图片，本讲只做简要说明） |
| [Software/PC_Application/LibreVNA-GUI/main.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/main.cpp) | PC GUI 的入口文件（仓库地图模块中作为"GUI 组件"的锚点） |
| [FPGA/VNA/top.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd) | FPGA 工程顶层（仓库地图模块中作为"FPGA 组件"的锚点） |

> 提示：三个框图同时提供 `.svg`（看图用）、`.pdf`（打印用）和 `.tex`（源码）三种格式，都在 `Documentation/DeveloperInfo/` 目录下。本讲的精确引用以 `.tex` 源码的行号为准。

## 4. 核心概念与源码讲解

### 4.1 RFBlockdiagram 框图解读

#### 4.1.1 概念说明

这个模块解决的问题是：**一个 100 kHz～6 GHz 的测量信号在电路板上到底走了哪条路？**

100 kHz 到 6 GHz 跨越了 4.6 个数量级，没有任何一颗频率源、滤波器或耦合器能在整个范围内都表现良好。因此 LibreVNA 的射频部分可以概括为三段设计：

1. **双激励源分工**：低于 25 MHz 用 Si5351C（本来就要用它产生所有时钟，顺便兼任低频源）；25 MHz 以上用 MAX2871（专业宽带 PLL+VCO 芯片）。
2. **一分为三的信号分配**：放大后的激励信号经功分器分成两路——较强的一路经端口选择开关送往 Port 1 或 Port 2，较弱的一路直接送入参考接收机（作为比值 \( b/a \) 的分母）。
3. **三条完全独立、结构相同的接收链**：Port 1 接收机、Port 2 接收机、参考接收机，每条都经过"电桥/耦合 → 一级混频（到 60 MHz 中频）→ 二级混频（到 250 kHz 中频）→ 滤波放大 → ADC"。

框图中出现的关键器件一览：

| 器件 | 型号 | 在框图中的角色 |
| --- | --- | --- |
| 时钟源/低频源 | Si5351C | 全板时钟分配器 + 25 MHz 以下激励源 + 第二本振（2.LO） |
| 高频源 | MAX2871 | 25 MHz 以上激励源 |
| 第一本振 | MAX2871（另一颗） | 一级下变频用本振（1.LO） |
| 数字衰减器 | RFSA3714 | 调节激励功率（约 -42 ～ -10 dBm） |
| 放大器 | TRF37A73 | 激励信号放大 |
| 射频开关 | QPC6324 ×3、RFSW6024 ×2 | 端口选择、频段/滤波器选择 |
| 回波损耗桥 | 电阻电桥（RLB） | 分离入射波与反射波（替代定向耦合器） |
| 一级混频器 | ADL5801 | \( f_{RF} \to 60\,\text{MHz} \) |
| 二级混频器 | LT5560 | \( 60\,\text{MHz} \to 250\,\text{kHz} \) |
| 中频放大器 | THS4521 | 驱动 ADC 的差分放大 |
| ADC | MCP33131D-10 ×3 | 16 位 @ 800 kHz 采样最终中频 |

#### 4.1.2 核心流程

以"测量 Port 1 的 \( S_{11} \)"为例，信号逐级流动如下（频率值为框图和 README 标注的标称值）：

```text
[激励源选择]
  f < 25MHz   : Si5351C ──> 40MHz 低通 ──> 频段选择开关(QPC6324)
  f >= 25MHz  : MAX2871 ──> 低通滤波器组(900/1800/3500MHz) ──> 频段选择开关
                    │
                    ▼
[电平调整]     RFSA3714 数字衰减器 ──> TRF37A73 放大器
                    │
                    ▼ (功分器 splitter，-1dB 主路 / -25dB 耦合路)
        ┌───────────┴────────────┐
        ▼ 主路(强)                ▼ 耦合路(弱)
  端口选择开关(QPC6324)        参考接收机
  + 每端口两枚开关串联
        │
        ▼
  Port 1 ──> 被测件(DUT)
        │ 反射波
        ▼
  回波损耗桥(RLB) 取出反射信号
        │
        ▼
[接收链 ×3，结构完全相同]
  一级混频 ADL5801  + 1.LO(MAX2871)  ==> 1.IF = 60MHz   ──> 70MHz 低通
  二级混频 LT5560   + 2.LO(Si5351C)  ==> 2.IF = 250kHz ──> 300kHz 低通
  THS4521 差分放大 ──> MCP33131 ADC(16bit @ 800kHz)
        │
        ▼
  FPGA 采样与数字处理（下一模块）
```

两级下变频的频率关系：

\[ f_{IF1} = \left| f_{RF} - f_{LO1} \right| = 60\,\text{MHz}, \qquad f_{IF2} = \left| f_{IF1} - f_{LO2} \right| = 250\,\text{kHz} \]

为什么要两级而不是一级？直觉上有两点：一是把几 GHz 直接变到 250 kHz 需要本振在极宽范围内连续可调且滤波器难以抑制镜像频率；二是 60 MHz 这个固定一级中频让镜像始终落在离有用信号很远的地方，可以用固定的 70 MHz 低通轻松滤除。二级本振（2.LO）由 Si5351C 提供，具体频率值由固件根据配置计算，本讲不做展开（详细推导待后续固件相关讲义）。

最终 ADC 以 800 kHz 采样 250 kHz 中频，采样点随后在 FPGA 中做加窗和 DFT，提取出复数幅度/相位——这是第 6 单元（FPGA）的内容，这里只需知道 ADC 是模拟世界的终点。

#### 4.1.3 源码精读

**(1) 项目自我定位**。[README.md:L67-L68](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/README.md#L67-L68) 中"How does it work?"一节开宗明义：

> The PCB is really only the RF frontend with some processing power. Everything else is handled in the PC application once the data is transferred via USB.

这句话是理解整个仓库的钥匙：电路板只负责产生信号和采集数据，校准、误差修正、绘图、数学运算全部发生在 PC 端。它还告诉你——没有硬件也可以运行 GUI 并导入示例测量。

**(2) 频率范围**。[README.md:L7](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/README.md#L7)：`**100kHz to 6GHz VNA**`——这就是仪器的完整规格范围。

**(3) 双激励源**。[README.md:L72-L73](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/README.md#L72-L73) 说明 Si5351C 是主时钟源、兼任 25 MHz 以下激励源（参考时钟为 26 MHz 晶振或外部 10 MHz 信号），MAX2871 负责 25 MHz 以上。对应到框图源码：

- [RFBlockdiagram.tex:L216](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/RFBlockdiagram.tex#L216) 定义 Si5351 节点，标签写明它身兼三职：`LF Source / CLK Distributor / 2.LO`。
- [RFBlockdiagram.tex:L217-L221](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/RFBlockdiagram.tex#L217-L221) 画出 10 MHz 外参考输入/输出和 26 MHz 晶振。
- [RFBlockdiagram.tex:L226-L227](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/RFBlockdiagram.tex#L226-L227) 定义 MAX2871 为 `HF Source`，并且它的参考时钟（100 MHz）也来自 Si5351C——**全板所有频率最终锁定到同一个基准**，这是相位测量的前提。
- [RFBlockdiagram.tex:L235-L238](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/RFBlockdiagram.tex#L235-L238)：MAX2871 输出后接一组可切换的低通滤波器（900/1800/3500 MHz），用两枚 RFSW6024 开关选择，用于抑制谐波。
- [RFBlockdiagram.tex:L243](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/RFBlockdiagram.tex#L243)：Si5351C 的低频输出经 40 MHz 低通进入频段选择开关，标注范围 `1-25 MHz`。

**(4) 功率控制与信号分配**。[README.md:L74-L75](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/README.md#L74-L75)：RFSA3714 数字衰减器把功率调到约 -42 ～ -10 dBm；TRF37A73 放大后信号被功分，弱的一路进参考接收机。对应 [RFBlockdiagram.tex:L246](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/RFBlockdiagram.tex#L246)：`to[vpiattenuator,label={RFSA3714}] ... to[amp, label={TRF37A73}] ... node[splitter]`；功分器样式在 [RFBlockdiagram.tex:L163-L164](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/RFBlockdiagram.tex#L163-L164) 中标注了两路输出分别为 -1 dB 和 -25 dB。

**(5) 端口路由与隔离**。[README.md:L76](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/README.md#L76)：较强的一路可路由到任一端口，每条路径用**两枚 RF 开关串联**以提高端口间隔离度。对应 [RFBlockdiagram.tex:L248-L253](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/RFBlockdiagram.tex#L248-L253)（`portselect`、`port1switch`、`port2switch`，注释 `3x QPC6324`）。

**(6) 电桥与独立接收链**。[README.md:L77-L78](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/README.md#L77-L78)：用电阻式回波损耗桥替代定向耦合器（宽带更容易实现）；两个端口拥有**完全分离的接收路径**——BOM 成本更高，但能同时测两个参数（S11 和 S21，或 S22 和 S12），也避免了接收路径合并可能带来的隔离问题。电桥的电阻桥结构定义在 [RFBlockdiagram.tex:L184-L196](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/RFBlockdiagram.tex#L184-L196)（`RLB` 样式，三个 50Ω 电阻 + 接地电阻构成电桥）。

**(7) 两级中频与 ADC**。[README.md:L79-L80](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/README.md#L79-L80)：每个接收机含两个下变频混频器，第一中频 60 MHz、第二中频 250 kHz；ADC 以 16 位 @ 800 kHz 采样最终中频。以 Port 1 接收链为例，源码依次是：

- 一级混频 ADL5801 + 70 MHz 低通：[RFBlockdiagram.tex:L258-L264](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/RFBlockdiagram.tex#L258-L264)
- 二级混频 LT5560 + 300 kHz 低通：[RFBlockdiagram.tex:L266-L272](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/RFBlockdiagram.tex#L266-L272)
- THS4521 放大与 MCP33131D-10 ADC：[RFBlockdiagram.tex:L274-L282](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/RFBlockdiagram.tex#L274-L282)

参考接收机链路在 [RFBlockdiagram.tex:L285-L309](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/RFBlockdiagram.tex#L285-L309)（其中 L309 把功分器的 -25 dB 耦合端接入参考混频器），Port 2 链路在 [RFBlockdiagram.tex:L312-L338](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/RFBlockdiagram.tex#L312-L338)。三条链结构完全相同。

**(8) 本振分配**。第一本振是另一颗 MAX2871（[RFBlockdiagram.tex:L341](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/RFBlockdiagram.tex#L341)），它的输出一分为三同时喂给三个一级混频器（[RFBlockdiagram.tex:L343-L345](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/RFBlockdiagram.tex#L343-L345)）；第二本振信号全部来自 Si5351C（[RFBlockdiagram.tex:L347-L350](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/RFBlockdiagram.tex#L347-L350)）。三条链共用同源本振，保证 \( b/a \) 比值的相位一致。

#### 4.1.4 代码实践

**实践目标**：不看任何硬件，仅凭 GUI/文档/框图，写出信号从激励源到 ADC 的完整路径，并明确两个频率源的分工。

**操作步骤**：

1. 在浏览器打开 [RFBlockdiagram.svg](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/RFBlockdiagram.svg)（GitHub 页面可直接渲染；下载后用任意浏览器打开亦可）。
2. 对照上面 4.1.2 的流程图，在 SVG 上依次找到：Si5351C → 40MHz 低通 → 频段选择开关 → RFSA3714 → TRF37A73 → 功分器 →（一路向下）端口开关 → RLB → ADL5801 → LT5560 → THS4521 → ADC。
3. （可选，推荐）按 [README.md:L21-L40](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/README.md#L21-L40) 的 Ubuntu 说明（或 Windows/macOS 对应小节）从 Release 下载并启动 GUI，确认它不连接硬件也能启动。README 第 68 行明确说明：没有 PCB 时可以导入 [Documentation/Measurements](https://github.com/jankae/LibreVNA/tree/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/Measurements) 下的示例测量来体验功能。
4. 用自己的话写约 200 字的信号路径总结，其中必须包含两个频率源的分工（参考答案见下方练习 1）。

**需要观察的现象**（若执行了第 3 步）：

- GUI 正常启动，设备列表为空/显示未连接，各窗口仍可操作。
- 导入示例测量后能看到曲线和 Smith 图。

**预期结果**：一段能按顺序复述"源 → 衰减 → 放大 → 功分 → 端口 → 电桥 → 60MHz → 250kHz → ADC"的文字。GUI 运行行为为「待本地验证」（本讲义写作环境未实际运行 GUI）。

#### 4.1.5 小练习与答案

**练习 1**：哪个频率段由 Si5351C 负责？哪个由 MAX2871 负责？为什么需要两颗？

**参考答案**：Si5351C 负责 25 MHz 以下（框图标注 1–25 MHz，上限受其 40 MHz 低通约束）；MAX2871 负责 25 MHz 以上直到 6 GHz。Si5351C 本来就作为全板时钟分配器存在（还给 MAX2871 提供 100 MHz 参考、给二级混频提供 2.LO），让它兼任低频源可以省一颗器件；而 MAX2871 是宽带 PLL+VCO，无法输出低至 100 kHz 的信号，低频段必须由 Si5351C 顶上。

**练习 2**：把 3 GHz 的激励信号下变频到 60 MHz 第一中频，第一本振应设为多少？

**参考答案**：由 \( f_{IF1} = |f_{RF} - f_{LO1}| \)，\( f_{LO1} = f_{RF} \pm 60\,\text{MHz} \)，即 3.06 GHz 或 2.94 GHz 均可（取高或取低取决于镜像频率规划，由固件决定）。

**练习 3**：为什么参考接收机不经过电桥，而两个端口接收机要经过？

**参考答案**：\( S_{11} = b_1/a_1 \) 中的 \( a_1 \) 是流入端口 1 的入射波，参考接收机直接从功分器耦合端取一小段激励信号即可得到它。而 \( b_1 \)（反射波）和 \( b_2 \)（出射波）在端口处与入射波混在一起，必须用电桥把两个方向的波分离开才能单独测量。

### 4.2 数字部分与电源部分框图

#### 4.2.1 概念说明

射频前端解决了"产生和搬移信号"的问题，但采样之后的实时数字处理和对外通信由谁做？LibreVNA 的答案是经典的 **FPGA + MCU 分工**：

- **FPGA（Spartan 6 XC6SLX9）**：紧贴 ADC，做硬实时的事——采样三个 ADC、与射频芯片逐寄存器交互、扫描调度、片上 DFT。README 特别强调：因为 FPGA 直接和射频块通信，测量频率的切换几乎是瞬时的，只受 PLL 稳定时间限制。
- **MCU（STM32G431）**：跑 FreeRTOS 的"大管家"——在 FPGA 里设置扫描、提取并预处理测量结果、经 USB 传给 PC、同时经 I2C 配置 Si5351C。
- **PC GUI**：一切非实时处理——校准、误差修正、绘图、数学运算、用户交互。

电源部分（PowerBlockdiagram.svg）则回答"这么多射频器件怎么供电"：整机由 USB 供电（或外部 5V DC），几乎每个射频块都有自己的**本地稳压器**，防止噪声和信号通过电源线耦合到整块 PCB。

#### 4.2.2 核心流程

数字部分有两条方向相反的流：

**数据流（设备 → PC）**：

```text
3x ADC (16bit@800kHz)
   └─> FPGA：采样、加窗、DFT 等实时处理
         └─> MCU：提取/预处理测量结果
               └─> USB 2.0 FS ──> PC GUI：校准/绘图/数学
```

**控制流（PC → 设备）**：

```text
PC GUI ──USB──> MCU(STM32G431，FreeRTOS)
                 ├─ SPI ────────────> FPGA（配置扫描、收数据）
                 ├─ Control / IRQ ──> FPGA（启动/完成握手）
                 ├─ I2C ────────────> Si5351C（时钟树配置）
                 └─ SPI ────────────> Flash（读写 FPGA bitstream）
FPGA ──> 射频衰减器与开关 / MAX2871(HF Source) / MAX2871(1.LO)
```

一个值得记住的设计细节：**FPGA 的 bitstream 存在 Flash 里，而 MCU 能直接访问这个 Flash**。因此给 FPGA 更新逻辑完全不需要 JTAG 编程器——MCU 从 USB 收到新 bitstream，写进 Flash，再通过框图上标注的 `Configuration` 线重新配置 FPGA。整机的固件（MCU 固件 + FPGA bitstream）都只靠一根 USB 线就能升级。

#### 4.2.3 源码精读

**(1) README 的数字部分总述**。[README.md:L82-L87](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/README.md#L82-L87) 依次说明：FPGA 是中心元件、负责与射频块的全部通信和 ADC 采样（频率切换近乎瞬时）；MCU 负责设置扫描、提取预处理测量并经 USB 上传；Flash 存放 bitstream，无需 JTAG 等 FPGA 专用工具，一切经 USB 更新。

**(2) 框图中的 MCU 与 USB**。[DigitalBlockdiagram.tex:L172-L175](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/DigitalBlockdiagram.tex#L172-L175) 画出 STM32G431 与 USB-C 接口，连接标注为 `USB 2.0 FS`（Full Speed，12 Mbps）和 `USB PD`（供电协商）。

**(3) FPGA 与 MCU 的四条连接**。[DigitalBlockdiagram.tex:L185-L189](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/DigitalBlockdiagram.tex#L185-L189)：Spartan 6 XC6SLX9 与 MCU 之间有四类信号——双向 `SPI`（传数据/命令）、`Control`（MCU→FPGA 控制）、`IRQ`（FPGA→MCU 中断，例如"一个采样块完成了"）、`Configuration`（MCU 用 Flash 里的 bitstream 配置 FPGA）。这四条连接就是第 4、5 单元要精读的两套协议（`USB_protocol` 与 `FPGA_protocol`）的物理载体。

**(4) 时钟分配**。[DigitalBlockdiagram.tex:L194-L196](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/DigitalBlockdiagram.tex#L194-L196)：Si5351C 在数字框图中以 `CLK Distributor` 身份出现，MCU 经 I2C 配置它，它向 FPGA 提供 16 MHz 时钟。

**(5) 三个 ADC 与射频控制**。[DigitalBlockdiagram.tex:L198-L211](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/DigitalBlockdiagram.tex#L198-L211) 定义了 Port1/Port2/Ref 三个 MCP33131D-10 ADC，输出全部进入 FPGA；[DigitalBlockdiagram.tex:L213-L220](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/DigitalBlockdiagram.tex#L213-L220) 显示 FPGA 直接控制"射频衰减器与开关"、HF Source MAX2871 和 1.LO MAX2871——这与 4.1 中"FPGA 处理所有与射频块的通信"完全对应。

**(6) 电源部分**。[README.md:L89-L93](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/README.md#L89-L93)：一切由 USB 供电（或外部 5V DC）；几乎每个射频块都有本地稳压器，防止噪声/信号经电源线耦合扩散到整块 PCB。详细拓扑见 [PowerBlockdiagram.svg](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/PowerBlockdiagram.svg)。

**(7) 框图与代码的互相印证**（先尝一小口源码）：数字框图里的"三个 ADC 进 FPGA"可以直接在 FPGA 顶层代码中找到实例——[FPGA/VNA/top.vhd:L597](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L597)、[top.vhd:L613](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L613)、[top.vhd:L629](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L629) 分别实例化了 `Port1ADC`、`Port2ADC`、`RefADC` 三个 MCP33131 接口。**文档说一件事，代码能找到对应实体**——这是本手册反复使用的验证方法。

#### 4.2.4 代码实践

**实践目标**：验证"数字框图中的每个连接在代码里都有落点"，建立"框图 ↔ 仓库"的映射直觉。

**操作步骤**：

1. 打开 [DigitalBlockdiagram.svg](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/DigitalBlockdiagram.svg)，记下 FPGA 与 MCU 之间的四类信号（SPI / Control / IRQ / Configuration）。
2. 在仓库中搜索这些连接的固件侧落点，例如在本地克隆中执行（示例命令，只读）：

   ```console
   grep -rn "FPGA_" Software/VNA_embedded/Application/FPGA.cpp | head -20
   ```

   或者直接在 GitHub 仓库的搜索框中搜索 `FPGA_`。
3. 再验证三个 ADC 的 FPGA 实例：在本地执行 `grep -n "MCP33131" FPGA/VNA/top.vhd`，或直接打开上面 4.2.3 第 (7) 条给出的三个永久链接。
4. 把"框图器件 → 代码位置"整理成一张三列表格（器件 | 所在子系统 | 代码/文件位置）。

**需要观察的现象**：

- `top.vhd` 中恰好有三个 `MCP33131` 实例，实例名分别是 `Port1ADC`、`Port2ADC`、`RefADC`。
- 固件侧存在一个 `FPGA.cpp`/`FPGA.hpp`（位于 `Software/VNA_embedded/Application/`），其中能找到与 SPI 传输、中断处理相关的函数名。

**预期结果**：一张至少包含 5 行的映射表。步骤 2/3 的 grep 结果为「待本地验证」（本讲义写作环境未运行该命令，但 4.2.3 第 (7) 条的三个 ADC 行号是直接读源码确认过的）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 ADC 接口和射频开关控制放在 FPGA 而不是 MCU 里？

**参考答案**：三个 ADC 各以 800 kHz 持续输出样本，且采样窗口必须与扫描点、本振稳定时间精确对齐——这类"多路并行 + 精确时序"的任务正是 FPGA 的强项。MCU 跑 FreeRTOS 任务存在调度抖动，逐寄存器地驱动 ADC 时序和几十个射频开关/PLL 会既吃力又难以保证确定性。所以 MCU 只做"设置扫描、收结果、传 USB"这类吞吐与逻辑为主的工作。

**练习 2**：框图上 MCU 与 FPGA 之间的 `IRQ` 线是干什么的？方向是哪边到哪边？

**参考答案**：方向是 FPGA → MCU（[DigitalBlockdiagram.tex:L188](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/DigitalBlockdiagram.tex#L188) 的箭头为 `latex-`，指向 MCU）。FPGA 在完成一个采样/处理块后用它打断 MCU，通知 MCU 经 SPI 取走结果，避免 MCU 忙等。

**练习 3**：为什么每个射频块都要有自己的本地稳压器？

**参考答案**：射频电路（尤其 PLL、放大器）会在电源线上注入纹波和射频泄漏；如果所有块共用一条电源轨，一个块的噪声会串进其他块，表现为本振相位噪声恶化、串扰、测量轨迹出现毛刺。本地稳压器（配合去耦）把每个块的污染限制在本地，是射频 PCB 的常规做法（见 [README.md:L93](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/README.md#L93)）。

### 4.3 仓库三大组件地图

#### 4.3.1 概念说明

前两个模块讲的是"电路板上有什么"，这个模块回答"代码放在哪"。LibreVNA 仓库实际上是**三个被一起版本管理的独立工程**，分别产出三个交付物：

| 组件 | 目录 | 语言/工具链 | 产物 | 职责 |
| --- | --- | --- | --- | --- |
| PC GUI | `Software/PC_Application/LibreVNA-GUI/` | C++ / Qt6 (qmake) | 桌面应用 | 校准、绘图、数学运算、用户交互、SCPI 远程控制 |
| MCU 固件 | `Software/VNA_embedded/` | C / STM32CubeIDE (FreeRTOS) | 固件镜像 | USB 通信、扫描调度、时钟配置、预处理 |
| FPGA 逻辑 | `FPGA/` | VHDL / Xilinx ISE | bitstream | ADC 采样、射频芯片时序、扫描引擎、片上 DFT |

三者通过两级协议衔接：GUI ↔ 固件走 USB 二进制协议 + SCPI 文本协议（`Documentation/DeveloperInfo/USB_protocol_v12.tex`、`Device_protocol_v13.tex`），固件 ↔ FPGA 走 SPI 寄存器协议（`FPGA_protocol.tex`）。这些协议是第 4、6 单元的主角，本讲只需知道它们的存在和位置。

#### 4.3.2 核心流程

拿到仓库后按"由外向内"的顺序导航：

```text
仓库根
├── README.md                  ← 安装与架构总述（本讲主材料）
├── AssembleFirmware.py        ← 把 MCU 固件 + FPGA bitstream 组装成单一可烧写文件
├── Software/
│   ├── PC_Application/
│   │   ├── LibreVNA-GUI/      ← 组件1：Qt GUI 源码（Calibration/ Device/ Traces/ VNA/ SpectrumAnalyzer/ Generator/ ...）
│   │   ├── LibreVNA-Test/     ← GUI 的单元测试工程
│   │   └── 51-vna.rules       ← Linux udev 规则（USB 权限）
│   ├── VNA_embedded/          ← 组件2：STM32 固件
│   │   ├── Application/       ←   项目自有业务代码（App/ VNA/ SpectrumAnalyzer/ Communication/ Drivers/ ...）
│   │   ├── Src/ Middlewares/  ←   STM32CubeMX 生成的框架代码与 FreeRTOS
│   │   └── VNA_embedded.ioc   ←   CubeMX 工程描述文件
│   └── Integrationtests/      ← Python 硬件在环测试
├── FPGA/
│   ├── VNA/                   ← 组件3a：VNA 测量逻辑（top.vhd、Sweep.vhd、DFT.vhd、Sampling.vhd + Test_*.vhd 仿真）
│   └── Generator/             ← 组件3b：信号发生器逻辑
├── Documentation/
│   ├── DeveloperInfo/         ← 三份协议文档 + 三份框图 + 构建/烧写说明
│   ├── UserManual/            ← 用户手册与 SCPI 编程指南
│   └── Measurements/          ← 示例测量数据（无硬件体验 GUI 用）
└── Hardware/                  ← PCB 设计文件（Eagle/KiCad）
```

记忆要点：**想知道某个功能在哪，先问它属于哪一层**——用户看到的功能（校准、Smith 图）几乎都在 GUI；射频时序都在 FPGA；夹在中间的翻译和调度在固件。

#### 4.3.3 源码精读

**(1) GUI 的入口**。[Software/PC_Application/LibreVNA-GUI/main.cpp:L14-L30](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/main.cpp#L14-L30) 是整个 PC 应用的 `main()`：创建 `QApplication`，设置组织名/应用名，然后构造 `AppWindow`。第 2 单元会从这里的启动链路一路讲到模式系统。

```cpp
// 示例引用：GUI 入口（节选自 main.cpp）
int main(int argc, char *argv[]) {
    qSetMessagePattern("%{time process}: [%{type}] %{message}");
    app = new QApplication(argc, argv);
    ...
    window = new AppWindow;
```

**(2) 固件的入口**。[Software/VNA_embedded/Src/main.c:L1-L12](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Src/main.c#L1-L12) 的文件头带着 STMicroelectronics 版权声明和 `@attention` 注释——这是 STM32CubeMX 自动生成代码的标志。这个组件的代码分两类：`Src/`、`Middlewares/` 里主要是生成代码，**项目自有逻辑集中在 [Software/VNA_embedded/Application](https://github.com/jankae/LibreVNA/tree/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application) 目录**（App.cpp、VNA.cpp、SpectrumAnalyzer.cpp、Hardware.cpp、Communication/ 等）。第 5 单元详解。

**(3) FPGA 的顶层**。[FPGA/VNA/top.vhd:L32](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L32) 声明 `entity top`——整个 FPGA 设计的最外层，4.2.3 里看到的三个 ADC 实例就挂在这里。同目录下的 `Test_*.vhd`（Test_DFT.vhd、Test_Sampling.vhd、Test_SPICommands.vhd 等）是不需要任何硬件就能跑的仿真测试台，第 6 单元会充分利用它们。

**(4) 组装脚本**。[AssembleFirmware.py](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/AssembleFirmware.py)（71 行）把编译好的 MCU 固件和 FPGA bitstream 拼成一个文件，配合 4.2 里"MCU 能直接写 Flash 里的 bitstream"的机制，实现纯 USB 升级。构建细节在第 1 单元第 4 讲（u1-l4）展开。

**(5) 目录级导航入口**（均为提交 c4276df 下的固定链接）：

- GUI 子目录：[Software/PC_Application/LibreVNA-GUI](https://github.com/jankae/LibreVNA/tree/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI)（内含 Calibration/、Device/、Traces/、VNA/、SpectrumAnalyzer/、Generator/、Tools/、CustomWidgets/ 等）
- 固件业务代码：[Software/VNA_embedded/Application](https://github.com/jankae/LibreVNA/tree/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application)
- FPGA 工程：[FPGA/VNA](https://github.com/jankae/LibreVNA/tree/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA)
- 开发者文档：[Documentation/DeveloperInfo](https://github.com/jankae/LibreVNA/tree/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo)（三份协议 + 三份框图 + BuildAndFlash.md）

#### 4.3.4 代码实践

**实践目标**：不看任何工具，先凭直觉定位三个功能，再用目录浏览验证——训练"功能 → 层 → 目录"的映射反射。

**操作步骤**：

1. 先在纸上（或心里）回答以下三个问题，**不要**打开文件浏览器：
   - a. "GUI 中频谱分析仪模式的实现代码"在哪个目录？
   - b. "固件中 Si5351C 时钟芯片的驱动"在哪个目录？
   - c. "FPGA 中做离散傅里叶变换（DFT）的模块"在哪个文件？
2. 然后逐一验证：
   - a. 浏览 [Software/PC_Application/LibreVNA-GUI](https://github.com/jankae/LibreVNA/tree/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI)，应看到 `SpectrumAnalyzer/` 子目录。
   - b. 浏览 [Software/VNA_embedded/Application/Drivers](https://github.com/jankae/LibreVNA/tree/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers)，应看到 `Si5351C.cpp`。
   - c. 浏览 [FPGA/VNA](https://github.com/jankae/LibreVNA/tree/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA)，应看到 `DFT.vhd`（以及对应的 `Test_DFT.vhd`）。
3. 统计自己的命中率，并把答错的项记下来——错的模式（比如"把固件功能猜到了 GUI"）通常就是你对该层职责的误解所在。

**需要观察的现象**：三个路径全部存在；每个目录里还能顺带看到相邻功能的文件（例如 `Drivers/` 里同时有 `max2871.cpp`，对应 4.1 里的两颗 MAX2871）。

**预期结果**：三题全部命中。若某题找不到，回到 4.3.2 的目录树重看该层职责。

#### 4.3.5 小练习与答案

**练习 1**：我想给 GUI 的 Smith 图加一个新功能，应该去哪个目录？想改激励源的切换时序呢？

**参考答案**：Smith 图属于绘图层，在 `Software/PC_Application/LibreVNA-GUI/Traces/`（tracesmithchart.cpp）。激励源切换时序属于硬实时，在 `FPGA/VNA/`（顶层 top.vhd 与扫描控制 Sweep.vhd），同时可能牵动固件侧 `Software/VNA_embedded/Application/Drivers/` 中的芯片驱动。

**练习 2**：`Documentation/Measurements/` 下的示例测量对无硬件学习者有什么价值？

**参考答案**：GUI 的全部数据处理（Trace、数学运算、校准显示、导入导出）都只依赖数据而不依赖硬件，导入示例测量可以在没有设备的情况下体验并调试 GUI 的几乎所有非采集功能——这正是"PCB 只是射频前端"架构给学习者的红利（依据 [README.md:L65](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/README.md#L65) 与 L68）。

**练习 3**：为什么 MCU 固件工程里要区分"CubeMX 生成代码"和 `Application/` 自有代码？

**参考答案**：CubeMX 按引脚配置/外设配置自动生成外设初始化与中断框架（`Src/`、`Middlewares/`），这些文件在重新生成时可能被覆盖；把项目逻辑隔离在 `Application/` 里既避免被覆盖，也让阅读者能直接跳到业务代码。识别这两类代码是阅读任何 CubeMX 工程的第一课。

## 5. 综合实践

**任务：制作一张属于你自己的"LibreVNA 全链路地图"。**

把本讲三个模块的产出合并成一张图/一页笔记，要求包含：

1. **信号链**（来自 4.1）：从 Si5351C/MAX2871 出发，沿功分器→端口开关→电桥→两级混频→ADC 画出主链路，标注每个节点的芯片型号和关键频率（1–25 MHz / ≥25 MHz、60 MHz、250 kHz、800 kHz 采样）。
2. **数字三级流水**（来自 4.2）：在同一张图下方接上 FPGA → MCU → USB → PC GUI 的数据流和反向控制流，标出 FPGA-MCU 之间的四类信号（SPI/Control/IRQ/Configuration）。
3. **代码落点**（来自 4.3）：给图上每个方框标注它对应的仓库目录（如"电桥与接收链 → 硬件；混频器配置 → VNA_embedded/Application/Drivers；DFT → FPGA/VNA/DFT.vhd；绘图 → LibreVNA-GUI/Traces"）。
4. 在图上用一句话回答：为什么说"PCB 只是射频前端"？

**验收标准**：拿这张图给一个没读过本讲的同事看，对方能在 5 分钟内明白信号怎么流、代码放在哪。完成后保留它——第 4、5、6 单元会分别放大这张图的 USB 段、固件段和 FPGA 段。

## 6. 本讲小结

- LibreVNA 是 100 kHz–6 GHz 的开源矢量网络分析仪，由三个一起版本管理的工程组成：Qt GUI（PC 端全部处理）、STM32G431 固件（调度与 USB）、Spartan6 FPGA（采样与射频时序）。
- 射频链路：Si5351C 负责 25 MHz 以下激励并兼任全板时钟和 2.LO；MAX2871 负责 25 MHz 以上激励，另一颗 MAX2871 作 1.LO；激励经 RFSA3714 衰减、TRF37A73 放大、功分后送往端口与参考接收机。
- 三条完全独立的接收链（Port1/Port2/Ref）结构相同：电桥取信号 → ADL5801 混到 60 MHz 一中频 → LT5560 混到 250 kHz 二中频 → THS4521 放大 → MCP33131 以 16 位 @ 800 kHz 采样。
- 数字部分以 FPGA 为中心与射频块通信，MCU 夹在 FPGA 与 USB 之间做设置、预处理与上传；FPGA bitstream 存在 Flash 中、由 MCU 经 USB 更新，因此整机升级不需要 JTAG。
- 电源全部来自 USB（或外部 5V），几乎每个射频块配本地稳压器以阻断电源耦合。
- 核心架构取舍："PCB 只是射频前端"——校准、误差修正、绘图、数学全在 PC 端，因此无硬件也能用示例测量体验 GUI 的大部分功能。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：《仓库地图：目录结构与三大组件》将把本讲 4.3 的目录树展开到文件级，带你逐个子目录浏览并学会快速定位功能。
- 之后建议按顺序学习 u1-l3（编译运行 GUI）和 u1-l4（固件与 FPGA 工具链），完成入门层的闭环。
- 想提前感受代码的话，推荐现在就浏览两个入口文件：[main.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/main.cpp)（GUI）与 [top.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd)（FPGA），不求读懂，只求混个眼熟。
- 协议文档（`Documentation/DeveloperInfo/` 下的 USB_protocol_v12.tex、Device_protocol_v13.tex、FPGA_protocol.tex）本讲只点了个名，第 4、6 单元会逐段精读，现在不必啃。
