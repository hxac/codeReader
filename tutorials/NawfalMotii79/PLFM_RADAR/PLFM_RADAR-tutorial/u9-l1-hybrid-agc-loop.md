# Hybrid AGC：FPGA + STM32 + GUI 联动

## 1. 本讲目标

AERIS-10 雷达面对的目标回波强弱悬殊：近处的大目标可能让 ADC 饱和削顶，远处的小目标又可能弱得检测不到。本讲讲解系统如何用一套**混合自动增益控制（Hybrid Automatic Gain Control, Hybrid AGC）**，把增益自动压到「既不削顶、又不浪费动态范围」的甜区。

本讲学完后，读者应该能够：

- 说出 **FPGA 内环**（逐采样、数字增益）与 **STM32 外环**（逐帧、模拟 VGA 增益）的分工与各自的时间尺度。
- 解释 attack / decay / holdoff 三个参数如何实现「快降慢升」的稳定收敛。
- 复述 FPGA 把饱和标志经 `DIG_5` / `DIG_6` GPIO 送给 STM32 的外环机制。
- 指出 GUI 从 26 字节状态包的哪个字段读到 `current_gain`，并在 AGC Monitor 标签页上看到它。

## 2. 前置知识

在进入本讲前，读者应已建立以下认知（来自前置讲义）：

- **接收信号处理链**（u4-l1 ~ u4-l5）：DDC 把 400 MHz 实 ADC 信号变成 100 MHz 的 I/Q 基带，随后进入匹配滤波、距离抽取、MTI、Doppler FFT、CFAR。本讲的 AGC 模块插在 **DDC 输出与匹配滤波输入之间**，属于接收链最前端的「入口看门人」。
- **三层固件分工**（u2-l3）：FPGA 做确定性高速数字处理，STM32 管慢速外设与安全，GUI 做可视化。AGC 正是横跨这三层的典型跨层功能。
- **CFAR 检测**（u4-l5）：检测的前提是信号没有被削顶，否则 CFAR 的幅度统计会失真——这正是 AGC 要守护的东西。
- **ADAR1000 波束赋形与 Idq 校准**（u7-l3）：ADAR1000 既能设置每通道相位做波束扫描，其 RX VGA 也有可编程模拟增益，本讲 STM32 外环调的就是它。
- **主机命令协议**（u6-l2）：4 字节命令 `{opcode, addr, value}` 与 26 字节状态包是 GUI↔FPGA 的硬契约，AGC 的配置与回读都走这条路。

### 什么是 AGC，为什么要「混合」

自动增益控制（AGC）的核心思想是：**让接收通路的总增益跟随输入信号强弱反向变化**——信号太强就压低增益防止饱和，信号太弱就抬高增益把小信号拉出噪声底。

AERIS-10 把这件事拆成了**两个嵌套的环路**：

| 环路 | 执行体 | 调整对象 | 时间尺度 | 量程 |
|------|--------|----------|----------|------|
| 内环（inner） | FPGA `rx_gain_control` | 数字增益 `gain_shift`（移位） | 每帧（Doppler 帧完成时） | ±42 dB（±7 bit，每 bit 约 6 dB） |
| 外环（outer） | STM32 `ADAR1000_AGC` | ADAR1000 RX VGA 模拟增益 | 每帧（约 258 ms） | `agc_base_gain` 0~127 |

为什么需要两层？因为**数字增益只能左移（放大）或右移（衰减）已经量化后的数据**，它无法挽救已经被 ADC 削顶的样本；真正能「在 ADC 之前」改变信号大小的，是 ADAR1000 的模拟 VGA。所以内环负责快速、细粒度地把数字增益调到甜区，一旦连数字增益都压不住饱和（说明 ADC 入口就已经过载），就通过 GPIO「摇人」——通知 STM32 外环去把模拟 VGA 的增益降下来。两者嵌套，兼顾**速度**（内环快）与**动态范围**（外环覆盖 ADC 之前的模拟段）。

> 名词速查：**attack**（攻击/快降）指检测到饱和时立即下调增益的步长；**decay**（恢复/慢升）指信号偏弱时缓慢上调增益的步长；**holdoff**（保持/冷却）指允许上调增益前必须连续观察多少帧无饱和。「快降慢升」是为了避免增益在临界点反复振荡。

## 3. 本讲源码地图

本讲横跨 FPGA、MCU、GUI 三层，关键文件如下：

| 文件 | 所属层 | 作用 |
|------|--------|------|
| [9_Firmware/9_2_FPGA/rx_gain_control.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/rx_gain_control.v) | FPGA | 内环 AGC 核心：数字移位增益 + 每帧自动调整 |
| [9_Firmware/9_2_FPGA/radar_system_top.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v) | FPGA | 顶层：AGC 配置寄存器、opcode 译码、GPIO（DIG_5/DIG_6）输出、状态回读 |
| [9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v) | FPGA | 把 AGC 四项指标打包进状态包 word 4 |
| [9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_AGC.cpp](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_AGC.cpp) | STM32 | 外环 AGC：读 DIG_5、调整 ADAR1000 RX VGA 基础增益 |
| [9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_AGC.h](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_AGC.h) | STM32 | 外环 AGC 架构注释与配置字段定义 |
| [9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp) | STM32 | 主循环：DIG_6 同步使能、读 DIG_5、调用外环 |
| [9_Firmware/9_3_GUI/radar_protocol.py](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py) | GUI | 解析状态包 word 4，得到 AGC 四项指标 |
| [9_Firmware/9_3_GUI/v7/dashboard.py](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py) | GUI | AGC Monitor 标签页 + FPGA Control 的 AGC 配置面板 |
| [9_Firmware/9_3_GUI/v7/agc_sim.py](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/agc_sim.py) | GUI | FPGA 内环的位精确离线仿真（Raw IQ 回放用） |

## 4. 核心概念与源码讲解

本讲的三个最小模块对应混合 AGC 的三段：**4.1 FPGA 内环**、**4.2 GPIO 外环**、**4.3 GUI 监控**。三者按数据/控制流向串成一条闭环。

### 4.1 FPGA 内环 AGC（rx_gain_control）

#### 4.1.1 概念说明

内环是整个 AGC 的「前线哨兵」，住在 FPGA 接收链里。它的位置在 [radar_receiver_final.v:245](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v#L245) 处被例化为 `gain_ctrl`，输入接 DDC 输出的 I/Q，输出送给匹配滤波器：

```
DDC(adc_i_scaled) ──► rx_gain_control ──► 匹配滤波(脉冲压缩)
```

它做两件事：

1. **数字移位增益**：对每个 16 位有符号 I/Q 样本做左移（放大）或右移（衰减），溢出时饱和到 ±32767。
2. **每帧自动调整**：在 Doppler 帧完成脉冲（`frame_boundary`，来自 `doppler_frame_done`）到来时，根据这一帧的峰值与饱和次数，决定下一帧的增益往哪个方向调。

增益用一种紧凑的 4 位编码 `gain_shift[3:0]` 表示：最高位 `[3]` 是方向（0=放大左移，1=衰减右移），低 3 位 `[2:0]` 是移位量（0~7）。所以内环的有符号增益范围是 −7（最大衰减）到 +7（最大放大），换算成功率约 \(\pm 42\ \mathrm{dB}\)（每移一位约 6 dB）。

它支持两种模式：

- **手动模式**（`agc_enable=0`，默认）：直接用主机下发的 `host_gain_shift`，行为与没有 AGC 时完全一致，保证向后兼容。
- **自动模式**（`agc_enable=1`）：由内部状态机每帧自动改写增益，`host_gain_shift` 仅作为使能切换瞬间的初始值。

#### 4.1.2 核心流程

内环的 AGC 调整只在 `frame_boundary`（帧边界脉冲）这一拍发生，逻辑是一个三选一的状态更新：

```text
每个有效样本 valid_in:
    计算移位后的 sat_i/sat_q（饱和到 ±32767）
    若发生溢出 → frame_sat_count++
    更新 frame_peak = max(frame_peak, |I|, |Q|)   // 增益前的输入峰值

帧边界 frame_boundary 到来:
    快照本帧 frame_sat_count / frame_peak → 对外输出
    清零 frame_sat_count / frame_peak（为下一帧重新统计）
    若 agc_enable:
        若 本帧有饱和 (sat_count > 0):
            agc_gain -= agc_attack          // 快降
            holdoff_counter = agc_holdoff   // 重置冷却
        否则若 峰值偏低 (peak < agc_target):
            若 holdoff_counter == 0:
                agc_gain += agc_decay       // 慢升
            否则:
                holdoff_counter--           // 还在冷却，先等
        否则:
            保持当前增益
            holdoff_counter = agc_holdoff   // 信号正好，重置冷却
```

「快降慢升 + 冷却」是这套逻辑的灵魂：

- **快降**：一旦检测到饱和（削顶），立刻按 `agc_attack`（默认 1）扣增益，**同帧立即生效**，因为饱和数据已经无法用于检测。
- **慢升**：只有当峰值**低于目标**（默认 200）才考虑升增益；而且即使满足条件，也要等 `agc_holdoff`（默认 4）帧的冷却期结束才真正升一档 `agc_decay`（默认 1）。这避免了「刚降下去又立刻升回来」的振荡。
- **保持**：当峰值正好落在目标附近且无饱和时，增益不动，同时把冷却计数器重置——意味着下次信号变弱时又要重新等满 4 帧。

峰值目标是一个 8 位数（`agc_target`，默认 200）。注意 `peak_magnitude` 对外输出的是 15 位绝对值的**高 8 位**（`frame_peak[14:7]`），所以 `agc_target` 也是这个尺度上的数。

#### 4.1.3 源码精读

模块端口声明揭示了它与外界的全部契约。注意 AGC 的五项主机配置（`agc_enable/target/attack/decay/holdoff`）与帧边界脉冲 `frame_boundary`：[rx_gain_control.v:35-69](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/rx_gain_control.v#L35-L69)。这段定义了「主机怎么配、下游怎么读」的全部接口。

数字移位 + 饱和的组合逻辑用 24 位中间量检测溢出，再钳位到 16 位有符号范围：[rx_gain_control.v:119-131](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/rx_gain_control.v#L119-L131)。这里 `>>>` 是算术右移（保符号位），`<<<` 是逻辑左移，饱和时按符号位决定钳到 +32767 还是 −32768。

AGC 状态机的核心——帧边界处的三选一调整：[rx_gain_control.v:244-266](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/rx_gain_control.v#L244-L266)。关键点：饱和检测条件是 `wire_frame_sat_incr || frame_sat_count > 0`（含「valid_in 与 frame_boundary 同拍」的边界修正），饱和即 `agc_gain <= clamp_gain(agc_gain - agc_attack)` 并重置 holdoff；峰值偏低分支里只有 `holdoff_counter==0` 才升增益，否则只递减计数器。

使能切换时的初始化：当 `agc_enable` 出现 0→1 跳变，把主机 `gain_shift` 编码转成有符号增益作为起点：[rx_gain_control.v:270-273](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/rx_gain_control.v#L270-L273)。

对外输出 `current_gain`——自动模式下来自内部 `agc_gain` 的编码，手动模式下直接回显 `gain_shift`：[rx_gain_control.v:276-279](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/rx_gain_control.v#L276-L279)。这个 `current_gain` 就是 GUI 要读的那个字段。

> 顶层把主机配置接进接收机，再把 AGC 状态接出来：AGC 配置寄存器与状态线在 [radar_system_top.v:541-565](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L541-L565) 处穿过 `rx_inst` 端口。配置寄存器本身（复位默认值 enable=0/target=200/attack=1/decay=1/holdoff=4）定义在 [radar_system_top.v:273-278](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L273-L278) 与复位块 [radar_system_top.v:937-942](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L937-L942)。

#### 4.1.4 代码实践

**目标**：在源码层面走通一次「饱和→快降」的调整过程，验证 attack/decay/holdoff 的默认值。

**操作步骤**：

1. 打开 `rx_gain_control.v`，定位到 [L244-266](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/rx_gain_control.v#L244-L266) 的帧边界逻辑。
2. 假设某一帧有 5 个样本溢出（`frame_sat_count=5`），`agc_attack=1`、`agc_gain=+3`。手算下一帧的 `agc_gain` 与 `holdoff_counter`。
3. 再假设接下来连续 6 帧都无饱和、且峰值都低于 `agc_target=200`、`agc_decay=1`、`agc_holdoff=4`。手算这 6 帧后 `agc_gain` 的最终值。

**需要观察的现象 / 预期结果**：

- 第 1 步：饱和帧后 `agc_gain = clamp(3 - 1) = +2`，`holdoff_counter = 4`。
- 第 2 步：随后每帧峰值偏低，但要先耗尽 holdoff。第 1 帧计数器 4→3（不升），第 2 帧 3→2，第 3 帧 2→1，第 4 帧 1→0，第 5 帧计数器已为 0 故升一档 `agc_gain=+3` 并重置计数器=4，第 6 帧计数器 4→3。最终 `agc_gain = +3`。

这就是「快降慢升」的直观体现：降一档只要 1 帧，升回一档要等 5 帧（4 帧冷却 + 1 帧生效）。

> 待本地验证：以上为根据源码逻辑的手算结果。若想跑通真实波形，可结合 4.3 节的 `agc_sim.py` 用一段构造的 IQ 数据复现。

#### 4.1.5 小练习与答案

**练习 1**：为什么峰值 `peak_magnitude` 取的是 15 位绝对值的**高 8 位**，而不是直接用全精度？

**参考答案**：状态包字段只有 8 位，而样本绝对值可达 15 位（0~32767）。取高 8 位相当于右移 7 位（除以 128），既压缩了位宽，又让 `agc_target`（默认 200）与显示量纲对齐——200 对应原始绝对值约 25600，恰好落在 16 位有符号（满量程 32767）的「接近但不削顶」区间，符合 AGC 想维持的甜区。

**练习 2**：`agc_enable` 从 0 切到 1 的那一拍，`agc_gain` 的初值从哪里来？为什么这样设计？

**参考答案**：从主机 `gain_shift` 经 `encoding_to_signed` 转换而来（[L270-273](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/rx_gain_control.v#L270-L273)）。这样设计让用户在手动模式下先凭经验调好一个起点，再打开 AGC，自动环路就从该起点开始微调，而不是从 0 突变，避免增益跳变造成瞬态异常。

---

### 4.2 GPIO 外环（FPGA → STM32）

#### 4.2.1 概念说明

内环调的是**数字增益**，只能改变 ADC 量化之后的数值。如果信号在进入 ADC **之前**就已经过载（ADAR1000 模拟前端增益太高），那么无论数字增益怎么压，样本都已经被削顶了。这时需要外环出手：降低 ADAR1000 RX VGA 的**模拟增益**，从源头把信号拉回线性区。

外环由 STM32 上的 [ADAR1000_AGC.cpp](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_AGC.cpp) 实现，每雷达帧（约 258 ms）在主循环里跑一次。它和内环之间不用 USB（太慢、要主机参与），而是用两根**直连 GPIO**：

- `DIG_5`（FPGA H11 → STM32 PD13）：**饱和标志**。当本帧 `rx_agc_saturation_count != 0` 时拉高。
- `DIG_6`（FPGA G12 → STM32 PD14）：**AGC 使能镜像**。直接回显 `host_agc_enable`，作为「唯一真相源」让 STM32 外环知道内环是否在自动模式。

这样，内环每帧把「我饱和了吗」「我开着吗」两个事实通过 GPIO 电平告诉 STM32，STM32 据此决定是否调模拟增益，形成 **FPGA 内环 ⊂ STM32 外环** 的嵌套闭环。

#### 4.2.2 核心流程

外环的调整逻辑与内环高度同构，也是「快降慢升」，但作用对象是 ADAR1000 的模拟 VGA：

```text
每雷达帧 (main loop, ~258ms):
    读 DIG_6 (PD14) → 经 2 帧去抖确认 → outerAgc.enabled
    若 enabled:
        sat = 读 DIG_5 (PD13)
        outerAgc.update(sat):
            若 sat:
                saturation_event_count++
                holdoff_counter = 0
                agc_base_gain -= gain_step_down   // 默认降 4，钳到 min_gain
            否则:
                holdoff_counter++
                若 holdoff_counter >= holdoff_frames (默认 4):
                    holdoff_counter = 0
                    agc_base_gain += gain_step_up  // 默认升 1，钳到 max_gain
        outerAgc.applyGain(mgr):
            对 4 片 × 4 通道 = 16 路 RX VGA:
                gain = clamp(agc_base_gain + cal_offset[ch], min_gain, max_gain)
                mgr.adarSetRxVgaGain(dev, ch+1, gain)
```

几个关键细节：

- **默认步长「降 4 升 1」**（构造函数 [ADAR1000_AGC.cpp:14-27](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_AGC.cpp#L14-L27)）：饱和时一次降 4 个码，恢复时一次只升 1 个码，比内环更激进地压、更保守地升，因为模拟增益变化影响整条链路，振荡代价更大。
- **per-channel 校准 `cal_offset`**：16 路通道并非完全一致，`effectiveGain = agc_base_gain + cal_offset[ch]` 允许对每个通道单独微调，校正通道间增益失衡（默认全 0）。
- **2 帧去抖**：STM32 读 `DIG_6` 时要求连续两帧读到相同值才更新 `enabled`，防止单次毛刺引发误切换（[main.cpp:2182-2199](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L2182-L2199)）。

#### 4.2.3 源码精读

顶层声明两根 GPIO 输出并注释其物理引脚映射（DIG_5→PD13，DIG_6→PD14）：[radar_system_top.v:130-133](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L130-L133)。

GPIO 的实际赋值——`DIG_5` 在「本帧饱和计数非零」时拉高，`DIG_6` 直接镜像使能寄存器：[radar_system_top.v:1043-1044](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L1043-L1044)。这两行就是内外环的物理接线。

外环的 attack/recovery 逻辑：[ADAR1000_AGC.cpp:34-71](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_AGC.cpp#L34-L71)。饱和时 `agc_base_gain -= gain_step_down`（默认 4）并清零冷却计数；非饱和时累计冷却，满 `holdoff_frames`（默认 4）才 `+= gain_step_up`（默认 1）。

把增益真正写到 16 路 RX VGA：[ADAR1000_AGC.cpp:79-88](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_AGC.cpp#L79-L88)。注意通道索引用 1-based（匹配 `setBeamAngle` 的约定）。

主循环里串起整套外环：先 `runRadarPulseSequence()`，再用 DIG_6 同步使能（带 2 帧去抖），最后读 DIG_5、调用 `update` + `applyGain`：[main.cpp:2180-2205](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L2180-L2205)。架构注释（内环数字 ±42 dB / 外环模拟 VGA）在 [ADAR1000_AGC.h:7-18](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_AGC.h#L7-L18)。

#### 4.2.4 代码实践

**目标**：理清「FPGA 通知 STM32 饱和」的完整物理路径，并明确外环与内环调整对象的差异。

**操作步骤**：

1. 在 [radar_system_top.v:1043](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L1043) 确认 `gpio_dig5` 的条件表达式。
2. 在 [main.cpp:2201-2204](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L2201-L2204) 找到 STM32 读取该引脚的 `HAL_GPIO_ReadPin` 调用。
3. 列出内环与外环各自调整的「对象」与「量纲」。

**预期结果**：

| 维度 | 内环 | 外环 |
|------|------|------|
| 触发源 | 帧内峰值/饱和统计（FPGA 自算） | `DIG_5` GPIO 电平（FPGA 送来） |
| 调整对象 | 数字移位 `gain_shift`（±7 bit） | ADAR1000 RX VGA `agc_base_gain`（0~127 码） |
| 作用位置 | ADC 之后、匹配滤波之前 | ADC 之前、模拟前端 |
| 速率 | 每帧 | 每帧（约 258 ms） |

> 待本地验证：若要在真实板子上观察，可用逻辑分析仪同时探测 PD13（DIG_5）与 PD14（DIG_6），向接收端注入强信号，应能看到 PD13 出现脉冲、随后 ADAR1000 寄存器值下降。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `DIG_6`（使能镜像）要走 GPIO，而不是让 STM32 自己保存一份 AGC 是否开启的标志？

**参考答案**：因为 AGC 的「唯一真相源」是 FPGA 的 `host_agc_enable` 寄存器——它由主机经 USB opcode 0x28 设置（[radar_system_top.v:988](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L988)）。STM32 不参与 USB 命令解析，若自己另存一份就会与 FPGA 失步。通过 `DIG_6` 镜像，STM32 每帧直接读到 FPGA 当前的真实使能状态，保证内外环严格同步启停。

**练习 2**：外环默认「降 4 升 1」，内环默认「降 1 升 1」。为什么外环的降要更激进？

**参考答案**：模拟 VGA 增益一旦过高导致 ADC 削顶，数据已不可用且影响整条链路；外环更激进地降（一次 4 码）能更快脱离饱和区，减少丢失的帧数。而升的时候保守（一次 1 码）是为了避免增益过冲再次饱和。内环作用在数字域、范围窄（±7 bit），所以步长更小、升降对称即可。

---

### 4.3 GUI 监控（状态回读 + AGC Monitor + 离线仿真）

#### 4.3.1 概念说明

GUI 在这套混合 AGC 里扮演两个角色：

1. **控制者**：通过 USB 命令（opcode 0x28~0x2C）下发五项 AGC 参数，并用快捷按钮一键开关 AGC。
2. **观察者**：每收到一个 26 字节状态包，从中解出 `current_gain / peak_magnitude / saturation_count / enable` 四项指标，在 FPGA Control 标签页显示数值、在 AGC Monitor 标签页画三条实时曲线。

此外，`v7/agc_sim.py` 提供了一个**位精确（bit-accurate）的 FPGA 内环离线仿真**，用于 Raw IQ 回放模式与离线分析——它逐帧复刻 `rx_gain_control.v` 的增益编码、钳位与 attack/decay/holdoff 逻辑，使得「不接硬件也能验证 AGC 行为」成为可能。

#### 4.3.2 核心流程

AGC 指标在状态包里的布局集中在 **word 4**（第 4 个 32 位字，状态包字节 17~20）：

```text
status_words[4] = { agc_current_gain[31:28],     // 4 bit
                    agc_peak_magnitude[27:20],    // 8 bit
                    agc_saturation_count[19:12],  // 8 bit
                    agc_enable[11],               // 1 bit
                    9'd0[10:2],                   // reserved
                    range_mode[1:0] }
```

GUI 解析时按右移 + 掩码逐字段切出：

```python
sr.agc_enable          = (words[4] >> 11) & 0x01
sr.agc_saturation_count= (words[4] >> 12) & 0xFF
sr.agc_peak_magnitude  = (words[4] >> 20) & 0xFF
sr.agc_current_gain    = (words[4] >> 28) & 0x0F
```

`agc_current_gain` 即「GUI 要读的增益字段」，占 word 4 的最高 4 位。GUI 随后把 `st.agc_current_gain` 推进一个长度 256 的环形缓冲，在 AGC Monitor 的第一条子图（Gain Code，y 轴 0~15）上画出随帧演化的曲线，同时在峰值子图上叠一条 y=200 的目标参考线、在饱和子图上用红色填充标出饱和事件。

#### 4.3.3 源码精读

FPGA 侧把四项指标拼进 word 4：[usb_data_interface_ft2232h.v:382-387](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L382-L387)（FT601 模块 `usb_data_interface.v` 布局相同）。注释明确标注了每个字段占据的比特位。

Python 侧的解析：[radar_protocol.py:250-256](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L250-L256)，字段定义在 [radar_protocol.py:146-149](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L146-L149)。FPGA 打包与 Python 拆包两侧的位域必须逐位一致，否则 GUI 读到的增益会错位——这是排查「AGC 数值乱跳」的第一现场。

GUI 把读到的增益写进状态标签：[dashboard.py:1776-1784](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py#L1776-L1784)（`Gain: {st.agc_current_gain}`）。三条曲线的环形缓冲与节流重绘在 [dashboard.py:1789-1858](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py#L1789-L1858)，AGC Monitor 标签页的三个子图（Gain/Peak/Sat）构建在 [dashboard.py:884-993](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py#L884-L993)。

FPGA Control 标签页里的 AGC 配置面板（opcode 0x28~0x2C 的输入行 + Enable/Disable 按钮）：[dashboard.py:779-801](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py#L779-L801)。点击 Enable AGC 即发送 `0x28` 命令 value=1。

离线位精确仿真 `process_agc_frame`，其 AGC 更新分支与 RTL 三选一一一对应：[agc_sim.py:198-212](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/agc_sim.py#L198-L212)，增益编码助手 `signed_to_encoding` / `encoding_to_signed` / `clamp_gain` 在 [agc_sim.py:40-60](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/agc_sim.py#L40-L60)。

#### 4.3.4 代码实践

**目标**：用 `agc_sim.py` 在无硬件环境下复现一次内环 AGC 收敛，并定位 GUI 读增益的精确字段。

**操作步骤**：

1. 用 Python 构造一段「前几帧强到削顶、之后变弱」的 int16 IQ 数据。
2. 调用 `process_agc_frame` 逐帧处理，打印每帧返回的 `gain_signed` 与 `saturation_count`。
3. 观察 attack 是否在前几帧把增益压下去、decay 是否在后面缓慢回升。
4. 在 [radar_protocol.py:256](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L256) 确认 `current_gain` 取自 word 4 的最高 4 位。

**示例代码**（非项目原有代码，仅为说明用法）：

```python
import numpy as np
from v7.agc_sim import AGCConfig, AGCState, process_agc_frame

cfg = AGCConfig(enabled=True, target=200, attack=1, decay=1, holdoff=4)
state = AGCState()
# 前 3 帧强信号（会饱和），之后弱信号
for i in range(8):
    amp = 30000 if i < 3 else 80
    frame_i = (amp * np.ones((16, 64))).astype(np.int16)
    frame_q = np.zeros((16, 64), dtype=np.int16)
    r = process_agc_frame(frame_i, frame_q, cfg, state)
    print(f"frame {i}: gain={r.gain_signed:+d} sat={r.saturation_count}")
```

**预期结果**：前 3 帧因 `saturation_count > 0` 触发 attack，`gain_signed` 逐帧 −1（0→−1→−2→−3）；之后弱信号分支经 holdoff 冷却后逐步回升。同时在 [radar_protocol.py:256](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L256) 看到 GUI 读的 `current_gain` 来自 `(words[4] >> 28) & 0x0F`。

> 待本地验证：依赖 `numpy`；可在安装了 `requirements_v7.txt` 的 venv 里运行。

#### 4.3.5 小练习与答案

**练习 1**：AGC Monitor 的 Gain 子图 y 轴范围是 −0.5~15.5，而 `current_gain` 是 4 位编码。把增益编码 0x09 解释成有符号值是多少？它代表放大还是衰减？

**参考答案**：0x09 = 二进制 `1001`，最高位 1 表示衰减方向，低 3 位 `001` 表示移 1 位。经 `encoding_to_signed` 得 −1，即衰减 1 bit（约 −6 dB）。y 轴画的是原始 4 位编码值 0~15，便于直接看寄存器原值。

**练习 2**：为什么 `agc_sim.py` 要逐位复刻 RTL，而不是写一个「差不多」的浮点版本？

**参考答案**：因为它用于 Raw IQ 回放——回放要重现硬件真实行为用于调参与验证。若仿真用浮点近似，会出现移位饱和、钳位边界与硬件不一致，导致「回放调好的参数上板却不对」。位精确才能保证离线分析与在线硬件的可比性（参见文件头注释「bit-accurate ... identical to the FPGA RTL」）。

---

## 5. 综合实践

**任务**：追踪一次「饱和事件」如何触发三层联动，把整条混合 AGC 闭环走通。

请按以下顺序阅读源码并回答，画出一张包含 FPGA / STM32 / GUI 三方的时序图：

1. **内环降增益**：假设某一帧 DDC 输出有若干样本经移位后超过 ±32767。在 [rx_gain_control.v:244-266](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/rx_gain_control.v#L244-L266) 说明内环如何在下一个 `frame_boundary` 把 `agc_gain` 减 `agc_attack`，并把 `saturation_count` 锁存到对外输出。

2. **GPIO 通知**：在 [radar_system_top.v:1043](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L1043) 说明 `saturation_count != 0` 如何把 `DIG_5`（PD13）拉高，把「我饱和了」这个事实送到 STM32 引脚。

3. **外环进一步调整**：在 [main.cpp:2200-2204](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L2200-L2204) 与 [ADAR1000_AGC.cpp:41-53](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_AGC.cpp#L41-L53) 说明 STM32 读到 PD13=1 后，如何把 `agc_base_gain` 减 `gain_step_down`（默认 4），并经 `applyGain` 写到 16 路 ADAR1000 RX VGA——这是内环数字增益做不到的「ADC 之前」的模拟调整。

4. **GUI 观察**：在 [radar_protocol.py:254-256](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L254-L256) 指出 GUI 从状态包 **word 4** 的哪些位读到 `saturation_count`（`[19:12]`）与 `current_gain`（`[31:28]`），并在 [dashboard.py:1776-1784](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py#L1776-L1784) 看到 GUI 把增益写进 AGC Monitor 曲线。

**交付物**：一张三层时序图 + 一段文字，说明「数字降增益（内环）→ GPIO 告警 → 模拟降增益（外环）→ GUI 显示」的完整链路与每一层调整对象的区别（数字 `gain_shift` vs 模拟 VGA 码 vs 显示字段）。

> 待本地验证：时序图可手工绘制；若要实测，需在真实雷达板上用强信号源触发，并配合逻辑分析仪抓 PD13/PD14 与 GUI 曲线对照。

## 6. 本讲小结

- 混合 AGC 是**两个嵌套环路**：FPGA 内环做数字移位增益（±42 dB），STM32 外环做 ADAR1000 RX VGA 模拟增益，前者快而细，后者覆盖 ADC 之前的模拟段。
- 内环在 Doppler 帧边界按 **attack（快降）/ decay（慢升）/ holdoff（冷却）** 三选一调整，默认「降 1 升 1、冷却 4 帧」，目标是把峰值维持在 `agc_target=200`。
- 外环通过 **`DIG_5`（PD13，饱和标志）/ `DIG_6`（PD14，使能镜像）** 两根 GPIO 与内环通信，默认「降 4 升 1」，并用 `cal_offset` 做 16 通道逐路均衡。
- GUI 通过状态包 **word 4** 读四项指标：`current_gain[31:28]`、`peak_magnitude[27:20]`、`saturation_count[19:12]`、`enable[11]`，并在 AGC Monitor 画三条实时曲线、在 FPGA Control 下发 opcode 0x28~0x2C。
- `agc_sim.py` 提供 FPGA 内环的**位精确离线仿真**，使无硬件调参与在线行为一致。
- 「快降慢升 + 冷却」与 2 帧去抖贯穿三层，是防止增益在临界点振荡的关键工程取舍。

## 7. 下一步学习建议

- **u10-l1 FPGA 板级自测试**：自测试会检查接收链各子系统，可结合本讲理解「饱和计数」如何在 bring-up 阶段作为诊断信号。
- **u11-l3 跨层契约测试**：本讲的 word 4 位域是 Python↔Verilog 必须逐位一致的硬契约，正是跨层契约测试的重点对象，建议接着读 `contract_parser.py` 如何自动校验这些位域。
- **继续阅读源码**：想深入可看 `usb_data_interface.v`（FT601 版）确认其 word 4 布局与 FT2232H 版一致；以及 `ADAR1000_Manager.cpp` 的 `adarSetRxVgaGain` 实现，理解外环 `applyGain` 最终落到哪条 SPI 写入。
