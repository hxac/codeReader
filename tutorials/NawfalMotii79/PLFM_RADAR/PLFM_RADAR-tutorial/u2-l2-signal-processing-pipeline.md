# 雷达信号处理流水线

## 1. 本讲目标

上一讲（u2-l1）我们看清了 AERIS-10 雷达由哪些硬件子系统拼成，以及主板里每个器件的角色。本讲要回答一个更关键的问题：**一个电磁波从被发射出去，到最后变成屏幕上的一个目标点，中间到底经历了哪些处理步骤？这些步骤分别由哪段源码负责？**

读完本讲，你应当能够：

1. 顺着「DAC 生成 chirp → 混频上下变频 → ADAR1000 波束扫描 → ADC 采样 → FPGA 的 DDC/抽取/匹配滤波/Doppler/MTI/CFAR → USB 上传 → GUI 显示」这条主干，讲清每一站的目的。
2. 说出**脉冲压缩、MTI、Doppler、CFAR** 这四个核心 DSP 步骤在流水线里的先后顺序，以及为什么是这个顺序。
3. 理解发射链路（TX）与接收链路（RX）的**镜像对称**关系。
4. 把 README 里写的高层「Processing Pipeline」一句话步骤，**一一映射到具体的 FPGA 模块名和源码文件**。

本讲是「全景图」式的串讲，刻意不深挖每个算法的数学实现——那些会在第 4 单元（U4）逐模块精读。本讲的产物是一张清晰的「阶段—源码」对照表，它是后续所有 FPGA 讲义的导航地图。

## 2. 前置知识

本讲假设你已经读过 u2-l1（整体架构与功能框图）。下面三个概念是理解流水线的前提，我们用最朴素的话先建立直觉。

### 2.1 为什么要用 chirp（线性调频）？

雷达要看得远，就得发射能量足够大的脉冲；发射能量 = 功率 × 时长，所以「长脉冲」打得远。但雷达又要分辨得清（距离分辨率高），这要求脉冲「窄」——这两个要求天然矛盾。

**线性调频（LFM / chirp）** 是化解矛盾的巧思：发射一个频率随时间线性扫动的长脉冲（比如从低频扫到高频），它的带宽 \(B\) 很大；接收后用一个叫**匹配滤波（脉冲压缩）**的操作把它「压」回成一个窄峰。这样既享受了长脉冲的大能量，又获得了大带宽带来的高分辨率。距离分辨率只取决于带宽：

\[
\Delta R = \frac{c}{2B}
\]

其中 \(c\) 是光速。本项目的代号 **PLFM** = Pulse Linear Frequency Modulated，正是指「脉冲 + 线性调频」。

### 2.2 距离和速度，分别用什么测？

- **距离**：发射 chirp，接收回波，做脉冲压缩得到「距离像（range profile）」——峰值的位置就对应目标距离。
- **速度（Doppler）**：对同一个方向连续发射一串 chirp（一帧），每个距离门上把这一串 chirp 的回波幅度排成一个序列，再做一次 FFT（叫「慢时间 FFT」或 Doppler FFT），谱峰的位置就对应目标的径向速度。多普勒频率与速度的关系：

\[
f_d = \frac{2v}{\lambda}
\]

其中 \(\lambda\) 是波长（10.5 GHz 对应约 2.86 cm），\(v\) 是目标径向速度。

所以一次完整的测量，本质是生成一张「距离 × 速度」的二维图，叫 **Range-Doppler 图**。本讲的整条流水线，最终目标就是生产这张图，再在上面找目标。

### 2.3 为什么要 MTI 和 CFAR？

- 现实环境里有大量**静止杂波**（地面、建筑、山），它们在距离像上也是强峰，会把真目标淹没。**MTI（动目标显示）** 用一个极简的高通滤波器，把「静止」的分量滤掉，只留下「动」的。
- 找目标时不能只设一个固定门限（噪声会随环境变化）。**CFAR（恒虚警率检测）** 根据目标周围邻近单元的噪声/杂波水平，**自适应**地算出门限，让「误报概率」保持稳定。

记住这三个「为什么」，后面读到源码里的模块名时，你就知道每个模块在解决哪个问题。

## 3. 本讲源码地图

本讲聚焦三条主干文件，它们正好对应「流水线的三个层级」：

| 文件 | 作用 | 在流水线中的角色 |
|------|------|------------------|
| `README.md` | 项目说明 | 用自然语言定义了高层「Processing Pipeline」的 6 个步骤，是我们要「落地」的规格书 |
| `9_Firmware/9_2_FPGA/radar_system_top.v` | FPGA 顶层模块 | 把发射机、接收机、CFAR、自测试、USB 接口「接线」连成整机的总开关 |
| `9_Firmware/9_2_FPGA/radar_receiver_final.v` | FPGA 接收处理链 | 流水线的「数字心脏」：ADC→DDC→匹配滤波→抽取→MTI→Doppler 全在这里串起来 |

此外，流水线表格里会引用到若干**子模块文件**（如 `radar_transmitter.v`、`doppler_processor.v`、`cfar_ca.v` 等），它们都真实存在于 `9_Firmware/9_2_FPGA/` 目录下，本讲只点名、不深读。

## 4. 核心概念与源码讲解

### 4.1 发射链路：从数字 chirp 到电磁波

#### 4.1.1 概念说明

发射链路（TX）要解决的问题：**把一段预先算好的 chirp 数字波形，变成 10.5 GHz 的射频电磁波投向天空，并按需控制波束指向。**

它分三段：

1. **波形生成**：FPGA 从内存里取出 chirp 样本，送给 DAC，DAC 把数字样本变成模拟中频电压。
2. **上变频**：模拟中频信号经混频器（LTC5552）与本振（ADF4382）混频，搬到 10.5 GHz 射频。
3. **波束赋形**：4 片 ADAR1000 相移器给 16 个通道分别加上递进的相位差，让合成波束指向想要的俯仰/方位角；前端芯片 ADTR1107 做功率放大。

发射与接收共用同一副天线和同一条射频通路，靠 **RF 开关**分时切换——这一点决定了 TX/RX 链路是「镜像对称」的（见 4.3）。

#### 4.1.2 核心流程

FPGA 侧发射的数字部分，可以用一句话伪代码概括：

```
STM32 发来 new_chirp 脉冲
  → plfm_chirp_controller_enhanced 进入 chirp 状态机，
     依次输出 长chirp → 监听 → 保护 → 短chirp → 监听
  → dac_interface_enhanced 把样本送到 dac_data[7:0] 引脚
  → 同时拉起 tx_mixer_en、adar_tx_load、fpga_rf_switch 等控制信号
```

这里出现一个关键概念：**chirp 周期里「发」和「收」是分时的**。发完一个 chirp 后有一段「监听（listen）」时间用来收回波，再有「保护（guard）」间隔，然后发下一段（短 chirp）。这种「长 chirp + 短 chirp」交替叫做 **staggered PRI**（交错脉冲重复周期），后面 Doppler 处理的双 16 点 FFT 就是为它量身设计的。

#### 4.1.3 源码精读

顶层把发射机作为一个整体例化，它对外只暴露 DAC 引脚、混频器使能、ADAR1000 控制线和 STM32 控制线：

> [9_Firmware/9_2_FPGA/radar_system_top.v:435-435](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L435) —— 顶层例化 `radar_transmitter tx_inst`，把 `dac_data`、`tx_mixer_en`、`adar_tx_load_*`、`stm32_new_chirp` 等接线连好。整个块到 L496。

打开 `radar_transmitter.v`，能看到发射机内部由三个边沿检测器加两个核心子模块组成：

> [9_Firmware/9_2_FPGA/radar_transmitter.v:207-207](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_transmitter.v#L207) —— `plfm_chirp_controller_enhanced plfm_chirp_inst`：chirp 状态机，决定何时发长/短 chirp、何时监听。

> [9_Firmware/9_2_FPGA/radar_transmitter.v:240-240](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_transmitter.v#L240) —— `dac_interface_enhanced dac_interface_inst`：把 chirp 样本按 120 MHz DAC 时序送到 `dac_data` 引脚。

注意一个时钟域细节：发射侧的 chirp 计数器跑在 **120 MHz DAC 时钟域**，而系统主处理跑在 **100 MHz**。顶层用专门的「toggle CDC」把 `new_chirp_frame` 脉冲从 120 M 安全搬到 100 M 域，这在 u3-l2 会详讲，这里只需知道「跨时钟域不是直接连线」。

#### 4.1.4 代码实践

**实践目标**：确认发射链「STM32 触发 → chirp 状态机 → DAC」这条调用链真的存在。

**操作步骤**：

1. 打开 `9_Firmware/9_2_FPGA/radar_transmitter.v`，找到 `plfm_chirp_inst`（L207 附近）的例化块。
2. 看 `plfm_chirp_controller_enhanced` 的输入里有没有 `stm32_new_chirp`（或经边沿检测后的等价信号），输出里有没有驱动 DAC 接口的 valid/data 信号。
3. 再看 `dac_interface_inst`（L240 附近），确认它的输出连到了顶层的 `dac_data`/`dac_clk` 物理引脚。

**需要观察的现象**：你能画出 `stm32_new_chirp → edge_detector → plfm_chirp_controller_enhanced → dac_interface_enhanced → dac_data[7:0]` 这条单向数据/控制流，中间没有「断头路」（悬空信号）。

**预期结果**：发射链在 FPGA 内是一条清晰的「触发→状态机→DAC 引脚」通路，所有射频相关的外部器件（混频器、相移器、功放）都由 STM32 在上电时配置好，FPGA 只负责产生波形和时序控制脉冲。

> 说明：本实践为「源码阅读型实践」，无需硬件即可完成；若要运行时验证，需要 50T 开发板与 DAC 子板，属「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：发射链里，是谁在产生 chirp 的数字样本——FPGA 现算，还是从内存读？
**答案**：从内存读。目录里的 `long_chirp_seg*_i.mem`、`short_chirp_i.mem` 等就是预存的 chirp 波形，FPGA 的 chirp 控制器按地址把它们读出来送给 DAC。

**练习 2**：为什么 `new_chirp_frame` 从 120 MHz 域搬到 100 MHz 域时，不能用一根普通连线，而要用 toggle CDC？
**答案**：`new_chirp_frame` 是 1 个时钟周期的窄脉冲。120 MHz 域里的单周期脉冲，100 MHz 采样时很可能正好采不到（亚稳态/漏采）。toggle CDC 把脉冲翻成电平翻转、同步后再用边沿检测还原成脉冲，保证不丢。

---

### 4.2 接收 DSP 链路：从回波采样到 Range-Doppler 图

#### 4.2.1 概念说明

接收链路（RX）是流水线的重头戏。它要解决的问题：**把天线收到的微弱射频回波，一步步处理成一张干净的「距离 × 速度」二维图，供检测使用。**

注意一个关键的「角色分工」：

- **射频前段**（ADC 之前：低噪放、混频器下变频到中频、ADAR1000 接收相移）属于模拟/微波范畴，由 STM32 配置，本讲不展开。
- **数字处理段**（ADC 之后）全部在 FPGA 的 `radar_receiver_final.v` 里完成，是本节主角。

AD9484 是一片 400 MHz、8 位的高速 ADC，它把中频信号采成一串数字。之后的全部处理——下变频、匹配滤波、抽取、MTI、Doppler——都是在这串数字上做数学运算。

#### 4.2.2 核心流程

接收数字段是一条严格的「串行流水线」，每一步的输出是下一步的输入。按 `radar_receiver_final.v` 里的例化顺序，整条链是这样的（★ 标记的是 4.3 节要重点讲的四个算法）：

```
AD9484 采到 400MHz 实数样本
  → ad9484_interface_400m        (LVDS 接口，拿到 8bit CMOS 数据)
  → ddc_400m_enhanced            (数字下变频：实信号→I/Q 基带，400M→100M)
  → ddc_input_interface          (整理 I/Q 格式)
  → rx_gain_control              (数字增益 + AGC)
  → matched_filter_multi_segment ★ (脉冲压缩：得到距离像)
  → range_bin_decimator          (1024 距离门 → 64 距离门，峰值抽取)
  → mti_canceller              ★ (二脉冲对消：滤静止杂波)
  → doppler_processor_optimized ★ (慢时间 FFT：得到速度谱)
  ---- 回到顶层 radar_system_top.v ----
  → DC notch                     (挖掉零多普勒附近的杂波峰)
  → cfar_ca                    ★ (恒虚警率检测：输出「有没有目标」)
  → usb_data_interface*          (打成包，经 USB 送给上位机)
```

一个极其重要的顺序事实：**MTI 在 Doppler FFT 之前**（这叫 pre-Doppler MTI）。这是因为 MTI 是在「每个距离门上，沿 chirp 序列」做差分，必须在慢时间 FFT 之前完成。4.3 节会解释为什么。

#### 4.2.3 源码精读

`radar_receiver_final.v` 用一连串例化把上面的流水线接死。下面按顺序给出每个关键例化点的永久链接。

**第 0 步 · 模式控制器**（决定何时收、收哪个波束）：

> [9_Firmware/9_2_FPGA/radar_receiver_final.v:148-172](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v#L148-L172) —— `radar_mode_controller rmc`，根据 `host_mode`（默认自动扫描）驱动 chirp/elevation/azimuth 时序，并接收主机可配置的 chirp 时长参数。

**第 1 步 · ADC 接口**（400 MHz LVDS → 8bit CMOS）：

> [9_Firmware/9_2_FPGA/radar_receiver_final.v:188-198](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v#L188-L198) —— `ad9484_interface_400m adc`，从 LVDS 差分对拿到 8 位 ADC 数据和 400 MHz 数据时钟。

**第 2 步 · 数字下变频 DDC**（实→复，400M→100M）：

> [9_Firmware/9_2_FPGA/radar_receiver_final.v:214-226](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v#L214-L226) —— `ddc_400m_enhanced ddc`，输入 400 MHz 的 ADC 数据，输出 100 MHz 的基带 I/Q（`baseband_i/q`）。

这里源码里有一段非常关键的注释（L200-L205），点出一个常见的 CDC 误区：ADC 数据**不能**用「同频 Gray 码 CDC」来跨时钟域，因为 Gray 码只对「每次只变 1 LSB」的值才安全，而 ADC 样本变化剧烈。真正的 400M→100M 跨域是靠 DDC 内部的 **CIC 抽取** 完成的——抽取本身就是降采样，顺带把数据搬到了 100 MHz 域。

**第 3 步 · 参考 chirp 加载与时延对齐**：

> [9_Firmware/9_2_FPGA/radar_receiver_final.v:301-311](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v#L301-L311) —— `latency_buffer ref_latency_buffer`，参数 `LATENCY=3187`，把参考 chirp 延迟固定周期，使其与匹配滤波器的数据流对齐。

**第 4 步 · 匹配滤波（脉冲压缩）★**：

> [9_Firmware/9_2_FPGA/radar_receiver_final.v:330-352](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v#L330-L352) —— `matched_filter_multi_segment mf_dual`，输入 DDC 后的 I/Q 和（经时延对齐的）参考 chirp，输出脉冲压缩后的距离像 `range_profile_i/q`。

**第 5 步 · 距离抽取**（1024→64）：

> [9_Firmware/9_2_FPGA/radar_receiver_final.v:356-373](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v#L356-L373) —— `range_bin_decimator range_decim`，参数 `INPUT_BINS=1024, OUTPUT_BINS=64, DECIMATION_FACTOR=16`，模式 `2'b01` 为峰值检测，把 1024 个距离门压成 64 个，降低后续 Doppler 的计算量。

**第 6 步 · MTI 杂波对消 ★**：

> [9_Firmware/9_2_FPGA/radar_receiver_final.v:379-395](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v#L379-L395) —— `mti_canceller mti_inst`，注释直接写出传递函数 `H(z) = 1 - z^{-1}`，即「当前 chirp 减上一 chirp」；`host_mti_enable=0` 时透传。

**第 7 步 · Doppler 处理 ★**：

> [9_Firmware/9_2_FPGA/radar_receiver_final.v:433-455](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v#L433-L455) —— `doppler_processor_optimized doppler_proc`，参数 `DOPPLER_FFT_SIZE=16, RANGE_BINS=64, CHIRPS_PER_FRAME=32, CHIRPS_PER_SUBFRAME=16`。`frame_complete` 信号在这里产生，用来通知下游「一帧的 Range-Doppler 图已就绪」。

Doppler 之后，数据回到顶层 `radar_system_top.v`，先做 DC notch，再进 CFAR：

> [9_Firmware/9_2_FPGA/radar_system_top.v:620-651](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L620-L651) —— `cfar_ca cfar_inst`，输入是 DC-notch 后的 Doppler 数据和帧完成脉冲，输出 `detect_flag`（有没有目标）等检测信号。

最后由 USB 接口打包上传（FT601 或 FT2232H 二选一）：

> [9_Firmware/9_2_FPGA/radar_system_top.v:718-718](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L718) —— `generate` 块开头，用 `USB_MODE` 参数在 `usb_data_interface`（FT601，32 位）和 `usb_data_interface_ft2232h`（FT2232H，8 位）之间编译期二选一，整块到 L863。

#### 4.2.4 代码实践

**实践目标**：亲自核对流水线里 11 个子模块的**例化顺序**和它们各自的**工作时钟域**。

**操作步骤**：

1. 打开 `9_Firmware/9_2_FPGA/radar_receiver_final.v`。
2. 从 L148 到 L455，把每个例化实例名（`rmc`、`adc`、`ddc`、…、`doppler_proc`）按出现顺序抄下来。
3. 对每个实例，看它接的 `.clk(...)` 是什么：接 `clk`（=100 MHz）还是 `clk_400m`（=400 MHz，仅 ADC/DDC 内部用）。

**需要观察的现象**：你应该能看到一条「400 MHz 端（ADC + DDC 内部）→ 100 MHz 端（增益、匹配滤波、抽取、MTI、Doppler）」的清晰分界，分界点就是 DDC 的降采样输出。

**预期结果**：得到一张「实例名 / 模块名 / 时钟域」三列小表，且 400 MHz 端只有 `ad9484_interface_400m` 与 `ddc_400m_enhanced` 的内部，其余全部在 100 MHz。这印证了「DDC 是时钟域的分水岭」。

> 说明：本实践为源码阅读型，无需运行；时钟域的划分可在 u3-l2 的 CDC 讲义中进一步验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么距离抽取（range_bin_decimator）放在匹配滤波之后、Doppler 之前，而不是最前面？
**答案**：抽取前必须先有「距离像」，而距离像是匹配滤波（脉冲压缩）的产物；在压缩前抽取会破坏信号。抽取后再做 Doppler，是因为 Doppler 是逐距离门算的，距离门数从 1024 降到 64 能把 Doppler 的计算量降到 1/16。

**练习 2**：`host_mti_enable=0` 时，MTI 模块会让数据「消失」吗？
**答案**：不会。源码注释明确写了 `transparent pass-through`（透传），MTI 关闭时当前数据原样送往 Doppler，只是不滤杂波。

---

### 4.3 处理阶段：README 步骤到 FPGA 模块的精确映射

#### 4.3.1 概念说明

本节解决学习目标里的第三条：**把 README 写的高层流水线，精确映射到 FPGA 模块名**。这是从「读文档」走向「读源码」的桥梁。

README 在「Processing Pipeline」一节用 6 条自然语言描述了整个流程；但这 6 条是给人看的概括，每一条背后都对应着一个或多个具体的 `.v` 文件。本节给出完整的对照关系，并解释四个核心 DSP 步骤（脉冲压缩、MTI、Doppler、CFAR）的**先后顺序与目的**。

#### 4.3.2 核心流程

先看四个核心 DSP 算法的「为什么是这个顺序」。它们沿「快时间（一个 chirp 内的采样）→ 慢时间（chirp 之间）→ 检测」的自然维度递进：

1. **脉冲压缩（匹配滤波）** —— 沿**快时间**做。把长 chirp 回波与参考 chirp 相关，得到距离像。这一步确定「距离」维。之所以最先做，是因为后面的 MTI/Doppler 都要按「距离门」组织数据，必须先有距离像。

2. **MTI（二脉冲对消）** —— 沿**慢时间**做，但在 Doppler FFT **之前**。它在每个距离门上做 \(y[n]=x[n]-x[n-1]\)（\(n\) 是 chirp 编号），传递函数：

\[
H(z) = 1 - z^{-1}, \qquad |H(e^{j\omega})| = 2|\sin(\omega/2)|
\]

它在 \(\omega=0\)（零多普勒，即静止目标）处有零点，正好滤掉地杂波。放在 FFT 前做（pre-Doppler）实现极简，只需缓存上一 chirp 的距离像。

3. **Doppler FFT** —— 沿**慢时间**做 FFT。把每个距离门上 32 个 chirp（分成两个 16 点子帧，对应长/短 chirp 的 staggered PRI）变换到速度谱，得到 Range-Doppler 二维图。

4. **DC notch + CFAR** —— 在 Range-Doppler 图上做。DC notch 进一步挖掉零多普勒附近的剩余杂波峰；CFAR 用邻近单元自适应估出门限，标出「哪些单元格是目标」。

记忆口诀：**先压（脉冲压缩）→ 再滤（MTI）→ 变谱（Doppler）→ 后判（CFAR）**。

> 关于 TX/RX 的对称性：发射链是「数字 chirp → DAC → 上变频 → 相移 → 功放 → 天线」，接收链是反过来的「天线 → 低噪放 → 相移 → 下变频 → ADC → 数字处理」。中间靠 RF 开关分时切换，共享同一组 ADAR1000 相移器（TX/RX 各一套配置）。理解这种镜像关系，能帮你把 u5（发射）和 u4（接收）两单元的知识互相印证。

#### 4.3.3 源码精读

先看 README 对流水线的权威定义：

> [README.md:97-118](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/README.md#L97-L118) —— README 的「Processing Pipeline」整节，6 步高层描述。

> [README.md:58-68](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/README.md#L58-L68) —— README 对 XC7A50T FPGA 职责的逐条列举（ADC 读取、AGC、I/Q 下变频、抽取滤波、FFT、脉冲压缩、Doppler/MTI/CFAR、USB），这是 FPGA 子任务的「清单」。

把上面两段 README 与 4.2 节的源码精读合并，就得到下面这张**「阶段—源码」映射表**（这正是本讲的核心交付物）：

| # | 流水线阶段 | README 对应描述 | 主要源码文件 | 解决的问题 |
|---|-----------|----------------|--------------|-----------|
| 1 | 波形生成（DAC chirp） | Waveform Generation | `radar_transmitter.v`、`plfm_chirp_controller.v`、`dac_interface_single.v` | 把预存 chirp 样本按时序送 DAC |
| 2 | 上/下变频 | Up/Down Conversion | （模拟：LTC5552 混频器，STM32 配置） | 中频 ↔ 10.5 GHz 射频搬移 |
| 3 | 波束扫描 | Beam Steering | （ADAR1000 相移器，STM32 经 SPI 配置） | 16 通道电子波束指向 ±45° |
| 4 | ADC 采样 | Raw ADC data capture | `radar_receiver_final.v` → `ad9484_interface_400m`（文件 `ad9484_interface_400m.v`） | 400 MHz 把中频回波采成 8 bit 数字 |
| 5 | 数字下变频 | I/Q baseband down-conversion | `ddc_400m.v`（模块 `ddc_400m_enhanced`） | 实信号 → I/Q 基带，400M → 100M |
| 6 | 抽取与滤波 | Decimation & filtering (CIC/FIR) | `cic_decimator_4x_enhanced.v`、`fir_lowpass.v`（在 DDC 内部） | 降采样 + 滤除带外分量 |
| 7 | 脉冲压缩 ★ | Pulse compression | `matched_filter_multi_segment.v`、`chirp_memory_loader_param.v`、`latency_buffer.v` | 长 chirp 压成窄峰，得距离像 |
| 8 | 距离抽取 | （隐含于 Doppler 准备） | `range_bin_decimator.v` | 1024 → 64 距离门，降算量 |
| 9 | MTI 杂波对消 ★ | MTI | `mti_canceller.v` | \(H(z)=1-z^{-1}\) 滤静止杂波 |
| 10 | Doppler FFT ★ | Doppler FFT processing | `doppler_processor.v`（模块 `doppler_processor_optimized`）、`xfft_16.v` | 慢时间 FFT 得速度谱 |
| 11 | DC notch | （CFAR 前处理） | `radar_system_top.v`（L578-L601 逻辑） | 挖掉零多普勒附近杂波 |
| 12 | CFAR 检测 ★ | CFAR detection | `cfar_ca.v`（顶层 L620 例化） | 自适应门限，标出目标 |
| 13 | USB 上传 | （数据回主机） | `usb_data_interface.v` / `usb_data_interface_ft2232h.v` | 打包经 FT601/FT2232H 送给上位机 |
| 14 | GUI 显示 | Visualization (Python GUI) | `9_Firmware/9_3_GUI/v7/dashboard.py` | Range-Doppler 图/目标点可视化 |

> 说明：第 2、3 步是模拟/微波与 STM32 配置范畴，FPGA 不直接处理，故只标注负责器件；其余步骤均有对应 FPGA 源码文件。★ 为四个核心 DSP 阶段。

#### 4.3.4 代码实践

**实践目标**：自己动手把这张映射表「验证一遍」，并补上时钟域信息。

**操作步骤**：

1. 复制上面这张表到自己的笔记里。
2. 对表中每一个标注了源码文件的阶段，用编辑器打开对应文件，确认 (a) 文件确实存在；(b) 模块名与表中一致（注意 `ddc_400m.v` 里模块叫 `ddc_400m_enhanced`、`doppler_processor.v` 里模块叫 `doppler_processor_optimized` 这类「文件名 ≠ 模块名」的情况）。
3. 给表再加一列「时钟域」，标注该阶段跑在 400 MHz、100 MHz 还是 DAC 的 120 MHz。
4. 标出 ★ 的四行，确认它们的顺序是：脉冲压缩(7) → MTI(9) → Doppler(10) → CFAR(12)，与 4.3.2 的口诀一致。

**需要观察的现象**：每一步都能在源码里「指」到一个具体的模块或代码块；没有哪一步是 README 凭空多写或漏写的。

**预期结果**：你得到一张三列变四列（增加时钟域）的完整流水线表，且能对着它向别人讲清「从电磁波到目标点」的每一站。

> 说明：本实践纯源码阅读，无需硬件或仿真；是后续 U4 各模块精读讲义的索引底稿。

#### 4.3.5 小练习与答案

**练习 1**：如果有人说「Doppler 应该在 MTI 之前做」，他错在哪里？
**答案**：MTI 是沿慢时间（chirp 间）的差分滤波 \(1-z^{-1}\)，必须在慢时间 FFT 之前作用于时域样本；若先做 Doppler FFT 再 MTI，等价于在频域挖零多普勒，实现复杂且失去了「二脉冲对消」的简单性。本项目采用 pre-Doppler MTI，故 MTI 在前。

**练习 2**：README 第 58–68 行列举了 FPGA 的多项职责，其中「Decimation & filtering」对应流水线里的哪个阶段？为什么它和 DDC 难以分开？
**答案**：对应阶段 6（抽取与滤波），由 CIC + FIR 完成。因为抽取（降采样）本身就是把数据从 400 MHz 搬到 100 MHz 的手段，所以「下变频」和「抽取滤波」在 DDC 模块内是一体的，README 把它们分列只是逻辑上区分。

**练习 3**：DC notch 和 MTI 都在去杂波，它们重复了吗？
**答案**：不重复。MTI 在「慢时间时域」去除静止目标（脉冲压缩后的距离像上做差分）；DC notch 在「Doppler 频域」把零多普勒附近的谱线置零，处理的是经过 FFT 之后残留的直流/近直流分量。两者作用维度和位置不同，是互补的两道清理。

---

## 5. 综合实践

**任务：绘制一张「AERIS-10 端到端信号处理流水线」全景图，并标注所有跨层协作点。**

把本讲三节的内容串起来，完成下面四件事：

1. **画主干**：在一张图（手绘或软件均可）上，从左到右画出 14 个阶段（DAC → 混频 → 波束 → ADC → DDC → 抽取滤波 → 脉冲压缩 → 距离抽取 → MTI → Doppler → DC notch → CFAR → USB → GUI），用箭头连起来。

2. **标归属层**：用三种颜色区分每个阶段属于哪一层——FPGA（`radar_system_top.v` / `radar_receiver_final.v`）、STM32、还是 GUI。你会发现绝大多数 DSP 阶段都是 FPGA 色，这正是「FPGA 做高速信号处理」的直观体现。

3. **标时钟域**：在 FPGA 段内，用虚线把「400 MHz 区（ADC + DDC 内部）」和「100 MHz 区（增益→匹配滤波→抽取→MTI→Doppler→CFAR）」分开，并标出 DAC 的 120 MHz 区。这正是 u3-l2（时钟域与 CDC）要深入的内容。

4. **标跨层接口**：在图上圈出三个跨层协作点并写一句话：
   - FPGA→STM32：RF 开关 / 混频器使能 / ADAR1000 load 等控制线（发射时序由 STM32 触发）；
   - FPGA→USB→GUI：数据/状态包（详见 u6、u8）；
   - STM32→ADAR1000/频综/电源：上电时配置微波前端（详见 u7）。

**验收标准**：你能指着图上任意一个阶段，说出 (a) 它解决什么问题，(b) 对应的源码文件，(c) 它的上下游分别是谁。做到这一点，本讲的目标就全部达成了。

> 说明：本实践为设计/文档型，无需运行代码；产出的全景图建议保存，它会在 U3–U8 的每一篇讲义里被反复引用。

## 6. 本讲小结

- AERIS-10 的信号处理是一条严格的单向流水线：发射（DAC→上变频→波束）与接收（ADC→DDC→…→CFAR→USB→GUI）镜像对称，靠 RF 开关分时共用射频通路。
- 接收数字段全部在 `radar_receiver_final.v` 内串接，顺序是：ADC 接口 → DDC → 增益 → **匹配滤波（脉冲压缩）** → 距离抽取 → **MTI** → **Doppler FFT** →（回顶层）DC notch → **CFAR** → USB。
- 四个核心 DSP 步骤的顺序与目的：先「压」（脉冲压缩得距离像）→ 再「滤」（MTI 去静止杂波）→ 「变谱」（Doppler 得速度谱）→ 「判」（CFAR 自适应门限检测）。
- 关键顺序事实：MTI 在 Doppler FFT **之前**（pre-Doppler），因为它在慢时间时域做 \(H(z)=1-z^{-1}\) 差分。
- DDC 是时钟域的分水岭：400 MHz 端只有 ADC 接口与 DDC 内部，其余处理都在 100 MHz；400M→100M 的跨域由 DDC 内部的 CIC 抽取完成，不能用同频 Gray CDC。
- README 的 6 步高层流水线可一一映射到具体 FPGA 源码文件；本讲交付的「阶段—源码」对照表是后续 U4 各模块精读的导航地图。

## 7. 下一步学习建议

本讲是「全景串讲」，刻意没有深入任何单个模块的内部实现。建议按数据流方向继续：

1. **紧接着读 u2-l3（三层固件分工）**：把本讲里反复出现的 FPGA / STM32 / GUI 三层职责边界讲清楚，巩固「哪个阶段归谁」。
2. **进入 U3（FPGA 顶层与时钟域）**：本讲提到的 400M / 100M / 120M 三个时钟域、toggle CDC、复位同步，会在 u3-l1、u3-l2 详细展开。
3. **逐站精读 U4（FPGA 接收信号处理链）**：本讲表格里 ★ 的四个阶段，U4 各有一篇专题讲义（DDC、匹配滤波、MTI+抽取、Doppler、CFAR），可按 4.3.3 的映射表对照阅读。
4. **想知道数据怎么变成屏幕上的目标点**：直接跳到 u8-l2（数据采集线程与帧组装），看 GUI 这一侧如何接收 USB 字节流并还原成 Range-Doppler 图。

> 阅读源码时，建议始终把本讲的「阶段—源码」对照表放在手边：每打开一个新模块文件，先在表里定位它属于哪一站、上下游是谁，再钻进细节，就不容易在大型 Verilog 工程里迷路。
