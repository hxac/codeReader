# OpenOFDM 项目概览

## 1. 本讲目标

本讲是 OpenOFDM 学习手册的第一篇，面向**完全没接触过这个项目**的读者。读完本讲，你应该能够：

- 说清楚 **OpenOFDM 是什么**：它是一个用 Verilog 写的、可综合（能真正烧进 FPGA）的 802.11 OFDM 物理层（PHY）**解码器**。
- 理解 **PHY 解码器在 Wi-Fi 接收链路中的位置**：它接收的是数字基带 I/Q 样本，输出的是 802.11 数据包的字节。
- 说出 OpenOFDM 的**功能边界**：支持哪些标准、哪些速率、哪些带宽；哪些它**不做**。
- 看懂官方文档的**八步解码流水线**，并能把每一步对应到「从射频到字节」的整体流程上。
- 认识项目里**硬件解码器（FPGA）**与 **Python 参考解码器**两种实现，理解它们的关系。

本讲**不**深入任何一行 Verilog 细节，那是后续讲义的任务。本讲只建立「全局地图」。

---

## 2. 前置知识

在开始前，用最通俗的方式解释几个关键词。即使你以前没听过，也能跟着下面的描述理解。

### 2.1 什么是 Wi-Fi 的「物理层（PHY）」

当你用手机连 Wi-Fi 看视频时，数据在空气中是以**无线电波（射频信号）**的形式传输的。一块 Wi-Fi 芯片要完成两件事：

- **发送（TX）**：把要发的字节，调制成无线电波发出去。
- **接收（RX）**：把收到的无线电波，还原成原始字节。

**物理层（PHY, Physical Layer）**指的就是「把比特变成电波、把电波变回比特」这一层。本项目的 `dot11` 模块只做**接收方向**的 PHY：解码（decode）。

### 2.2 什么是 OFDM

OFDM（Orthogonal Frequency Division Multiplexing，正交频分复用）是 802.11a/g/n 使用的调制技术。它的核心思想是：把一整段带宽切成很多个互相**正交**（不互相干扰）的**子载波**，数据被分散到这些子载波上**并行**传输。

- 802.11a/g/n 在 20 MHz 带宽内使用 **64 个子载波**（FFT 点数 = 64）。
- 其中一部分子载波承载**数据**，一部分是**导频**（用于跟踪相位），剩下是空的保护子载波。

OFDM 的好处是抗多径衰落强、频谱利用率高；代价是接收端要做大量数字信号处理——这正是 OpenOFDM 要实现的事。

### 2.3 什么是 I/Q 样本

无线电信号经过天线接收、放大、下变频到基带后，会被模数转换器（ADC）采样成数字样本。基带信号通常用**两路**表示：

- **I（In-phase，同相分量）**
- **Q（Quadrature，正交分量）**

一对 I/Q 样本可以表示为一个复数 \( s = I + jQ \)。OpenOFDM 的输入就是一连串这样的复数样本。

> 如果你对复数或 FFT 不熟也不必担心，本讲只需要你知道「输入是一串复数样本，输出是字节」即可，细节会在后续讲义展开。

---

## 3. 本讲源码地图

本讲只读**文档类文件**（不是 Verilog 源码），目的是建立全局认识。涉及的文件如下：

| 文件 | 作用 |
|------|------|
| `Readme.rst` | 项目的「门面」说明：OpenOFDM 是什么、特性清单、输入输出、依赖工具、FAQ。 |
| `docs/source/index.rst` | Sphinx 文档首页：一句话定位 + 文档目录（toctree），是阅读官方文档的入口。 |
| `docs/source/overview.rst` | 总览文档：八步解码流水线、顶层模块 `dot11` 的端口表（Pinout）、项目目录结构、样本文件说明。 |

> 提示：这三个文件是后续所有讲义的「坐标原点」，建议你边读本讲边打开它们对照。

---

## 4. 核心概念与源码讲解

### 4.1 OpenOFDM 是什么：项目定位与功能边界

#### 4.1.1 概念说明

OpenOFDM 的官方一句话定位是：「**802.11 OFDM 物理层解码器的 Verilog 实现**」。

拆开理解三个关键词：

- **802.11**：就是 Wi-Fi 的国际标准（IEEE 802.11）。
- **OFDM 解码器**：只做「接收」方向，把射频采样还原成字节。
- **Verilog 实现**：用硬件描述语言写，最终能综合成真实的数字电路（FPGA/ASIC），而不是跑在 CPU 上的软件。

注意「解码器」这个词：OpenOFDM **只接收、不发送**。

#### 4.1.2 核心流程

OpenOFDM 作为一个硬件 IP，在整个接收链路里只占一段：

```
天线 → 射频前端(放大/下变频) → ADC → 数字下变频(DDC)
   → 【基带 I/Q 样本】
   → 【 OpenOFDM: dot11 解码器 】   ← 本项目在这里
   → 【解码出的 802.11 字节】
   → MAC 层 / 上层协议
```

也就是说，别人（比如 USRP 的射频前端）负责把无线电波变成 I/Q 样本喂给它，它负责吐出数据字节。它**不**碰天线、不**碰**ADC 的模拟部分。

#### 4.1.3 源码精读

OpenOFDM 的项目开头第一句就给定位，随后列出 5 条核心特性：

> 这是项目定位与特性清单，描述了 OpenOFDM 是什么、能做什么：
> [Readme.rst:4-13](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/Readme.rst#L4-L13)

其中最关键的几条特性翻译如下：

- **Fully synthesizable**（可完全综合），并在 Ettus Research 的 **USRP N210** 平台上实测过——说明它不是玩具仿真，而是真能上硬件。
- **Full support for legacy 802.11a/g**：完整支持传统 802.11a/g 的所有速率。
- **Support 802.11n for MCS 0 - 7 @ 20 MHz bandwidth**：支持 802.11n 的 MCS 0–7，但**仅限 20 MHz 带宽**。
- **Cross validation with included Python decoder**：内置一个 Python 解码器，用来交叉验证 Verilog 实现是否正确。
- **Modular design**：模块化设计，便于修改和扩展。

文档首页也给出了几乎一致的功能边界表述：

> 文档首页对功能边界的总结（支持 a/g 全速率 + 802.11n 20MHz MCS0-7）：
> [docs/source/index.rst:9-14](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/index.rst#L9-L14)

#### 4.1.4 代码实践

1. **实践目标**：把 OpenOFDM 的「功能边界」整理成一张清晰的表格。
2. **操作步骤**：
   - 打开 `Readme.rst` 第 4–13 行和 `docs/source/index.rst` 第 9–14 行。
   - 用下表格式，自己填写「支持 / 不支持 / 待确认」：

     | 维度 | 支持 | 说明 |
     |------|------|------|
     | 802.11a/g | ? | 哪些速率 |
     | 802.11n | ? | MCS 范围、带宽 |
     | 40MHz 带宽 | ? | 是否支持 |
     | 发送 (TX) | ? | 是否实现 |
     | 目标平台 | ? | 哪块 FPGA |

3. **需要观察的现象**：你会发现「支持」和「不支持」在文档里写得非常明确，没有歧义。
4. **预期结果**：802.11a/g 全速率支持；802.11n 仅 MCS 0–7 且仅 20MHz；不支持 40MHz；不实现发送；目标平台是 USRP N210 里的 Spartan 3A-DSP 3400。

#### 4.1.5 小练习与答案

**练习 1**：OpenOFDM 是「收发机」还是「纯接收解码器」？为什么？

> **答案**：是**纯接收解码器**。它只把 I/Q 样本解码成字节，不实现发送链路。

**练习 2**：有人想用 OpenOFDM 解码 802.11n MCS 15（最高速率）的 40MHz 信号，能成功吗？

> **答案**：不能。OpenOFDM 只支持 802.11n 的 MCS 0–7 且仅 20MHz 带宽，既不支持 MCS 8–15，也不支持 40MHz。

---

### 4.2 八步解码流水线

#### 4.2.1 概念说明

OFDM 接收端要把「一串复数 I/Q 样本」变回「原始比特」，中间要经过一连串信号处理步骤。OpenOFDM 把这件事拆成**固定顺序的 8 个阶段**，这就是贯穿整个项目的「主链路」。后续几乎每一篇讲义都对应其中的一两步。

#### 4.2.2 核心流程

官方文档 `overview.rst` 在开头就列出了这 8 步，顺序如下（注意是**有序列表**，顺序不能乱）：

```
1. Packet detection              包检测：判断「有没有包到来」
2. Center frequency offset        中心频偏校正：补偿收发双方晶振不一致带来的频偏
   correction
3. FFT                           快速傅里叶变换：把时域样本变到频域子载波
4. Channel gain estimation        信道增益估计：用训练序列估每个子载波的衰落
5. Demodulation                   解调：把星座点还原成比特
6. Deinterleaving                 解交织：还原发射端打乱的比特顺序
7. Convolutional decoding         卷积解码（Viterbi）：纠正传输中的比特错误
8. Descrambling                   解扰：还原发射端加扰的数据
```

直觉上可以这样理解整条链路的职责分工：

- **第 1 步**：先确认「信号来了」，别对着噪声瞎处理。
- **第 2、4 步**：修正「信号在传输中变形了」（频率偏了、幅度被衰落了）。
- **第 3 步**：把信号从「时域」搬到「频域」，因为 OFDM 的数据是按子载波（频域）组织的。
- **第 5、6 步**：把频域的星座点还原成「比特流」，并按规则重排。
- **第 7 步**：用纠错码恢复出真正想发的比特。
- **第 8 步**：去掉发射端为了「让 0/1 分布均匀」而加的扰码。

#### 4.2.3 源码精读

> 官方文档对八步流水线的原始表述（注：RST 里用 `#.` 表示自动编号的有序列表，所以这里显示为 1/#/# …）：
> [docs/source/overview.rst:4-15](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/overview.rst#L4-L15)

这段后面紧跟一句说明：本文档会逐步走完这条流水线，解释每一步在 OpenOFDM 中如何实现。这正是本项目学习手册第 2、3 单元要做的事。

> 文档承诺「逐步讲解每一步」：
> [docs/source/overview.rst:17-18](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/overview.rst#L17-L18)

#### 4.2.4 代码实践

1. **实践目标**：把八步流水线记牢，并建立「输入 → 输出」的直觉。
2. **操作步骤**：
   - 不看上面的中文，只打开 `docs/source/overview.rst` 第 4–15 行。
   - 在纸上把 8 个英文步骤名抄一遍，并在每个后面用自己的话写一句「这一步在干什么、为什么需要」。
3. **需要观察的现象**：你会注意到前 4 步是「把信号弄干净、搬到频域」，后 4 步是「把星座点变回比特」。
4. **预期结果**：能复述出八步顺序，并能解释「为什么 FFT（第 3 步）必须在频偏校正（第 2 步）之后」——因为频偏会破坏子载波正交性、造成子载波间干扰（ICI），不先校正就没法正确做 FFT。

#### 4.2.5 小练习与答案

**练习 1**：如果跳过第 2 步（频偏校正）直接做 FFT，会发生什么？

> **答案**：收发晶振不一致导致的中心频偏会破坏子载波之间的正交性，引起**子载波间干扰（ICI）**，FFT 后每个子载波都会串进相邻子载波的能量，导致后续解调失败。

**练习 2**：第 7 步「卷积解码」和第 6 步「解交织」为什么不能调换顺序？

> **答案**：因为发射端是先做卷积编码、再做交织。接收端必须按相反顺序：先解交织（把比特还原到卷积编码后的位置），再做 Viterbi 卷积解码，才能正确纠错。通信收发两端的处理顺序是**严格镜像**的。

---

### 4.3 顶层模块 dot11 的输入输出

#### 4.3.1 概念说明

OpenOFDM 的所有信号处理都封装在一个**顶层模块** `dot11` 里（对应文件 `verilog/dot11.v`）。从外部看，你不需要知道它内部多复杂，只要知道它有哪些「管脚」（port）：喂什么进来、吐什么出去。本讲只看「黑盒」接口，不看内部。

#### 4.3.2 核心流程

`dot11` 模块的数据契约非常简洁，`Readme.rst` 用一句话概括：

- **输入**：32 位的 I/Q 样本（高 16 位是 I，低 16 位是 Q）。
- **输出**：解码后的 802.11 数据包字节。
- **采样率 20 MSPS**，**时钟 100 MHz**，所以「每 5 个时钟周期接收一对 I/Q 样本」。

这里有一个关键的时间关系：

\[
\frac{\text{时钟频率}}{\text{采样频率}} = \frac{100\,\text{MHz}}{20\,\text{MSPS}} = 5
\]

也就是说，样本是每 5 个时钟周期来一个（5:1 关系）。这个约定贯穿整个项目，是理解测试台和握手时序的基础（后续讲义会反复用到）。

#### 4.3.3 源码精读

> `Readme.rst` 的「Input and Output」小节，是接口契约的权威描述：
> [Readme.rst:24-30](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/Readme.rst#L24-L30)

`overview.rst` 提供了更完整的端口表（Dot11 Module Pinout），把端口分成几类：

> 顶层模块说明与端口总表的引出：
> [docs/source/overview.rst:20-33](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/overview.rst#L20-L33)

> 完整端口表（Dot11 Module Pinout），按控制 / 配置 / I/Q 输入 / 包信息输出 / 字节输出 / FCS 校验分组：
> [docs/source/overview.rst:34-71](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/overview.rst#L34-L71)

为了方便记忆，把端口表归类成 6 组：

| 组别 | 端口 | 方向 | 含义 |
|------|------|------|------|
| 控制 | `clock`, `enable`, `reset` | 输入 | 时钟、模块使能、复位 |
| 配置 | `set_stb`, `set_addr`(8), `set_data`(32) | 输入 | 设置寄存器总线（地址 + 数据 + 选通） |
| I/Q 输入 | `sample_in`(32), `sample_in_stb` | 输入 | 32 位 I/Q 样本 + 样本有效标志 |
| 包信息 | `pkt_begin`, `pkt_ht`, `pkt_rate`(8), `pkt_len`(16) | 输出 | 包开始、是否 HT、速率/MCS、包长 |
| 字节输出 | `byte_out_stb`, `byte_out`(8) | 输出 | 字节有效标志 + 8 位字节值 |
| 校验 | `fcs_out_stb`, `fcs_ok` | 输出 | FCS 校验输出 + 是否正确 |

> 小贴士：注意几个「strobe（选通）」信号——`sample_in_stb`、`byte_out_stb`、`fcs_out_stb`、`set_stb`。OpenOFDM 大量使用「数据 + strobe」的握手风格来传递数据，strobe 为高时表示当前周期的数据有效。这是后续讲义反复出现的模式。

#### 4.3.4 代码实践

1. **实践目标**：亲手验证「100MHz / 20MSPS = 每 5 拍一个样本」这个核心时序约定。
2. **操作步骤**：
   - 打开 `Readme.rst` 第 24–30 行，确认采样率与时钟的数值。
   - 用上面的公式手算一遍比值。
   - 打开 `docs/source/overview.rst` 第 34–71 行的端口表，找出 `sample_in_stb` 这一行，确认它是「输入」方向。
3. **需要观察的现象**：你会确认输入样本的节拍由外部（如测试台或 USRP 接收链）按 5:1 提供。
4. **预期结果**：得出 100MHz ÷ 20MSPS = 5，即每个 I/Q 样本间隔 5 个时钟周期。这一结论是 `dot11_tb.v` 里实现「每 5 拍喂一个样本」的依据（下一讲会看到 `clk_count==4` 的实现）。
5. **运行结果**：待本地验证（本讲只做纸面推导，不运行仿真）。

#### 4.3.5 小练习与答案

**练习 1**：`pkt_rate` 这个 8 位输出，在 legacy 和 HT 两种包里含义一样吗？

> **答案**：不一样。对 **HT（802.11n）**，低 7 位是 MCS 索引；对 **legacy（802.11a/g）**，低 4 位是 SIGNAL 字段里的 rate 比特。同一个端口、两种解读。

**练习 2**：`fcs_ok` 为高代表什么？为什么 OpenOFDM 要输出它？

> **答案**：`fcs_ok` 为高表示这一包的 **FCS（帧校验序列，CRC-32）通过**，即整包字节完整无误。输出它让上层（MAC）能直接丢弃损坏的包，而不必自己再做一遍校验。

---

### 4.4 文档导航与项目目录结构

#### 4.4.1 概念说明

OpenOFDM 是个「自包含」的项目——除了一个用于仿真的 Icarus Verilog 工具链外，仓库里就带齐了所有需要的东西（包括 Xilinx 库的仿真模型、样本数据、Python 参考解码器）。理解目录怎么组织，能帮你在后续讲义里快速定位「某个东西在哪个文件」。

同时，官方用 Sphinx 生成文档，`index.rst` 就是文档目录树（toctree）的根，是阅读官方文档的导航入口。

#### 4.4.2 核心流程

项目根目录的主要职责划分：

```
openofdm/
├── Readme.rst          项目门面说明（本讲重点）
├── requirements.txt    Python 依赖
├── LICENSE.txt         Apache 2.0 许可证
├── docs/               Sphinx 文档源（本讲重点）
│   └── source/         index.rst 在这里，以及各模块 .rst
├── verilog/            Verilog 实现（后续讲义重点）
│   ├── Xilinx/         Xilinx 专用库
│   ├── coregen/        Xilinx ISE 生成的 IP 核 + 各种查找表
│   └── usrp2/          USRP 平台专用模块（setting_reg、ram_2port 等）
├── scripts/            Python 脚本（生成 LUT、转样本、交叉验证、参考解码器）
└── testing_inputs/     样本文件（conducted 传导采集 / radiated 空口采集）
```

特别强调一个区分：仓库里有**两套解码器实现**——

- **硬件解码器（FPGA）**：`verilog/` 下的 Verilog，最终综合成电路，是项目的核心产出。
- **Python 参考解码器**：`scripts/decode.py`，跑在 CPU 上的浮点实现，**不是产品**，而是用来**交叉验证** Verilog 实现是否正确的「标尺」。

#### 4.4.3 源码精读

> `index.rst` 的文档目录树（toctree），列出了官方文档的全部章节，是后续阅读的路线图：
> [docs/source/index.rst:16-29](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/index.rst#L16-L29)

从 toctree 能看出官方文档的讲解顺序：overview（总览）→ detection（检测）→ freq_offset（频偏）→ sync_long（长同步）→ eq（均衡）→ decode（解码）→ sig（信号字段）→ setting（配置寄存器）→ verilog → usrp。这个顺序正好对应「八步流水线 + 控制平面 + 平台集成」，也基本就是本学习手册的展开顺序。

> `overview.rst` 的「Project Structure」小节，解释了 verilog / scripts / testing_inputs 各目录的职责，并说明了 Xilinx 与 USRP 依赖：
> [docs/source/overview.rst:74-99](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/overview.rst#L74-L99)

> `overview.rst` 对 `testing_inputs` 样本集的说明（覆盖所有 legacy 和 HT 速率）：
> [docs/source/overview.rst:108-110](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/overview.rst#L108-L110)

#### 4.4.4 代码实践

1. **实践目标**：动手浏览仓库，建立「目录 → 内容 → 角色」的脑图。
2. **操作步骤**：
   - 在项目根目录列出各子目录（命令行 `ls` 或文件管理器均可）。
   - 制作一张三列表格：**目录路径 | 里面有什么 | 在项目中的角色**。
   - 重点标注哪些目录是**仿真必需**（如 `verilog/` 的 RTL、`scripts/`、`testing_inputs/`），哪些是**综合 / 上板专用**（如 `verilog/coregen`、`verilog/usrp2`、`verilog/Xilinx`）。
3. **需要观察的现象**：你会看到 `verilog/` 下有大量 `.v` 文件，名字正好对应八步流水线（`power_trigger`、`sync_short`、`sync_long`、`equalizer`、`demodulate`、`deinterleave`、`viterbi`、`descramble` 等）。
4. **预期结果**：得到一张完整的目录职责表，并能指出 `scripts/decode.py` 是 Python 参考解码器、`scripts/test.py` 是交叉验证驱动脚本。

#### 4.4.5 小练习与答案

**练习 1**：`scripts/decode.py` 和 `verilog/dot11.v` 都是「802.11 解码器」，为什么要写两遍？

> **答案**：`verilog/dot11.v` 是要综合进 FPGA 的**产品实现**（定点、硬件）；`scripts/decode.py` 是跑在 CPU 上的**浮点参考实现**。Python 版本用来产生「每一步的期望输出」，和 Verilog 仿真结果逐阶段比对，从而**交叉验证**硬件实现是否正确。

**练习 2**：没有 Xilinx 商业工具链的人，能仿真 OpenOFDM 吗？

> **答案**：能。`overview.rst` 明确说项目是自包含的，可以用开源的 **Icarus Verilog**（`iverilog` + `vvp`）仿真，因为 `coregen` 目录里带了 IP 核的行为级 `.v` 仿真模型。综合上板才需要 Xilinx ISE。

---

## 5. 综合实践

本讲的综合实践任务，是把前面 4 个模块的知识串成一张全局地图。这是后续所有讲义的「导航图」，请认真完成。

**任务**：阅读 `Readme.rst` 与 `docs/source/overview.rst`，完成下面三件事：

1. **画出「射频信号 → 字节输出」的整体数据流框图**。
   - 在图里标出 OpenOFDM 的输入（I/Q 样本）和输出（字节 + FCS）。
   - 在图里把**八步解码流水线**按顺序画成一个从左到右的流水线，每一步标注：步骤名 + 一句话职责。
   - 在图里标出 OpenOFDM 在整条接收链路中的位置（ADC 之后、MAC 之前）。

2. **标注 OpenOFDM 支持 / 不支持的速率**。
   - 在框图旁边列一个清单：支持 802.11a/g 全速率、802.11n MCS0–7 @ 20MHz；不支持 40MHz、不支持发送。

3. **提交一段 200 字以内的项目定位说明**。
   - 用自己的话写，要求包含：OpenOFDM 是什么（Verilog 实现的 802.11 OFDM 接收 PHY 解码器）、输入输出是什么、支持边界、为什么有 Python 参考解码器。

**参考答案（项目定位说明示例，约 180 字）**：

> OpenOFDM 是一个用 Verilog 编写、可综合的 802.11 OFDM 物理层**接收解码器**。它接收 20 MSPS 的 32 位基带 I/Q 样本（高 16 位 I、低 16 位 Q），输出解码后的 802.11 数据字节及 FCS 校验结果。它支持 802.11a/g 全部速率和 802.11n 的 MCS 0–7（仅 20MHz 带宽），不实现发送、不支持 40MHz。解码经过包检测、频偏校正、FFT、信道估计、解调、解交织、卷积解码、解扰共八步。项目还内置一个 Python 浮点参考解码器，用于与 Verilog 实现做逐阶段交叉验证。目标平台是 USRP N210 的 Spartan 3A-DSP FPGA。

> 完成后建议把这张框图保存好——后续讲义会不断往这张图里补充「每一步对应哪个 `.v` 文件、哪个 strobe 信号」。

---

## 6. 本讲小结

- OpenOFDM 是一个**可综合的 802.11 OFDM 接收 PHY 解码器**的 Verilog 实现，只做接收、不做发送。
- 它的输入是 **32 位 I/Q 样本**，输出是**字节 + FCS 校验**；采样率 20 MSPS、时钟 100 MHz，样本每 **5 个时钟周期来一个**。
- 功能边界明确：支持 **802.11a/g 全速率**、**802.11n MCS 0–7 @ 20MHz**；不支持 40MHz 和发送方向。
- 解码走一条固定的**八步流水线**：包检测 → 频偏校正 → FFT → 信道估计 → 解调 → 解交织 → 卷积解码 → 解扰。
- 顶层模块 `dot11` 用「数据 + strobe」的握手风格对外通信，端口可分为控制、配置、I/Q 输入、包信息、字节输出、FCS 校验六组。
- 项目里有两套解码器：**Verilog 硬件实现**（产品）和 **Python 参考实现**（交叉验证的标尺）。

---

## 7. 下一步学习建议

本讲建立了全局地图，但还没有真正「跑起来」项目，也没看任何 Verilog 源码。建议按下面顺序继续：

1. **下一讲 `u1-l2`（开发环境搭建与仿真运行）**：亲手安装 Icarus Verilog，用 `verilog/Makefile` 跑通 `dot11_tb` 测试台，看到真实的字节输出和波形。这是把「纸面认识」变成「动手能力」的第一步。
2. **随后 `u1-l4`（顶层模块 dot11 的接口）**：打开 `verilog/dot11.v`，把本讲第 4.3 节的端口表和真实代码逐一对照。
3. **`u1-l5`（解码流水线总览）**：在 `dot11.v` 里定位八步流水线每一步对应的子模块实例，往本讲画的框图里填上真实文件名。
4. **如果你急于验证理解**：可以先跳到 `docs/source/overview.rst` 的「Sample File」小节（第 112–131 行），看看官方如何用一段 iPython 代码加载 24Mbps 样本，这能帮你理解后续交叉验证的样本从哪来。

继续阅读的源码优先级：先 `Readme.rst` 全文（已读）→ `docs/source/overview.rst` 全文（已读）→ `verilog/dot11.v` 的端口声明部分（下一讲开始深入）。
