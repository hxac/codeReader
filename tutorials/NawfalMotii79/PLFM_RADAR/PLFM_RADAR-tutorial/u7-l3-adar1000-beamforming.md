# ADAR1000 波束赋形与 Idq 校准

## 1. 本讲目标

本讲聚焦 AERIS-10 雷达的「模拟前端控制」三件套：用 ADAR1000 相移器做电子波束扫描、用 ADAR1000_AGC 做接收增益自动控制、用 DAC5578 + ADS7830 + INA241A3 把 16 路功放（PA）的静态工作电流 Idq 闭环校准到目标值。

学完后你应当能够：

- 说清 ADAR1000 这颗 4 通道波束赋形芯片如何靠「逐通道相位 + VGA 增益」实现电扫描，以及 4 片级联如何凑成 16 通道。
- 读懂 `calculatePhaseSettings` 里的相控阵相位公式，并把角度换算成芯片要的 128 级相位码。
- 解释 ADAR1000_AGC 外环的 attack/decay/holdoff 逻辑，以及它为何要和 FPGA 内环组成「混合 AGC」。
- 画出「DAC5578 设 Vg → PA 漏极电流 → 5mΩ 采样电阻 → INA241A3 → ADS7830」这条电流检测链路，并用源码里的公式把 ADC 原始值换算成安培。
- 复述开机 Idq 闭环校准循环的退出条件，以及它曾因为循环条件写反（Bug #12）导致永远校不准的教训。

## 2. 前置知识

本讲默认你已读过 [u7-l1 STM32 main 与外设初始化](u7-l1-stm32-main-and-peripherals.md)，知道 STM32 是全板「系统管理者」、掌握 I2C/SPI 外设句柄（`hi2c*`/`hspi*`）的概念。在此基础上，再补充三个本讲会用到的概念：

- **相控阵与电子扫描**：传统雷达靠电机转动天线来改变指向；相控阵靠给每个天线单元馈入「递进的相位差」，让合成波前在空间里偏转，从而无需机械转动就能扫描。关键公式是相邻单元的相位增量 \(\Delta\varphi=\frac{2\pi}{\lambda}d\sin\theta\)（u1-l1 已建立）。
- **矢量调制器（Vector Modulator, VM）**：ADAR1000 不是直接调「相位」，而是给每个通道一对正交分量 I 与 Q。给定一个相位角，对应的 (I, Q) 就唯一确定一个幅度为 1、角度为该相位的复数。所以设置一个通道的相位需要写**两个**寄存器：`PHS_I` 和 `PHS_Q`。
- **PA 静态工作点（Quiescent Current, Idq）**：GaN/GaAs 功放管要在栅极加一个负压 Vg，让它在「无信号时」也流过一个小电流 Idq（本系统目标 1.680 A，是 16 路合在一起的每通道值）。Idq 偏低→线性差、失真大；Idq 偏高→发热大、易烧。每颗管的阈值有离散性，所以必须**逐通道闭环**把 Idq 调到目标。

> 关于器件关系的一句话地图：ADAR1000 是「相位 + 增益 + 偏置 DAC」的控制芯片；ADTR1107 是挂在它后面的「PA + LNA + T/R 开关」前端芯片。本讲的 Idq 闭环走的是**另一条更精密的外部链路**（DAC5578/ADS7830），与 ADAR1000 内部的偏置寄存器是两套机制（详见 4.3.1 的说明）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [`ADAR1000_Manager.cpp`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_Manager.cpp) | 管理 4 片 ADAR1000：上电、收发模式切换、波束角度→相位码计算、SPI 寄存器读写。本讲「相移器管理」主角。 |
| [`ADAR1000_Manager.h`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_Manager.h) | 寄存器地址表、偏置常量、VM 查找表声明。 |
| [`ADAR1000_AGC.cpp`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_AGC.cpp) / [`.h`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_AGC.h) | STM32 侧的外环 AGC：每帧根据 FPGA 送来的饱和标志调整 16 路 RX VGA 增益。本讲「PA 增益控制」主角。 |
| [`DAC5578.H`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/DAC5578.H) / [`DA5578.c`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/DA5578.c) | DAC5578 8 通道 8 位 I2C DAC 驱动：写 Vg、LDAC 同步更新、CLR 紧急清零。（注意实现文件名是 `DA5578.c`，少一个 C。） |
| [`ADS7830.H`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADS7830.H) / [`ADS7830.c`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADS7830.c) | ADS7830 8 通道 8 位 I2C ADC 驱动：单端采样，读回经 INA241A3 放大的 Idq 电压。 |
| [`main.cpp`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp) | 把上面三件套串起来：初始化 4 片 DAC/ADC、读 Idq、跑闭环校准循环、周期性重读、健康检查、紧急停机。 |

---

## 4. 核心概念与源码讲解

### 4.1 相移器管理：ADAR1000 如何实现电子波束扫描

#### 4.1.1 概念说明

ADAR1000 是 Analog Devices 的 4 通道 X/Ku 波段模拟波束赋形芯片：每个通道一路矢量调制器（设相位）+ 一路 VGA（设增益），外加片内偏置 DAC 驱动前端的 ADTR1107。AERIS-10 用 **4 片 ADAR1000 级联**，4×4 = **16 通道**，对应 16 路天线单元/功放。

`ADAR1000Manager` 这个类就是「4 片 ADAR1000 的总管」：它持有 4 个 `ADAR1000Device` 对象（[`ADAR1000_Manager.cpp`:L89-L93](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_Manager.cpp#L89-L93)），用 SPI1 上的 4 根独立 CS（片选）逐片寻址，把「波束角度」翻译成「每片每通道的相位码 + 增益码」写下去。

它要解决的问题有三个：

1. **怎么把角度变成相位码？**——靠相控阵公式 + 一张 128 状态的矢量调制器查找表。
2. **怎么把相位码写进正确的通道？**——靠 SPI 3 字节指令帧 + 寄存器地址表。
3. **收发怎么切换？**——TX/RX 用同一套相位定律，但要先切换偏置与 ADTR1107 的 T/R 开关。

#### 4.1.2 核心流程

设波束角度 `setBeamAngle(θ, direction)` 的执行流程：

```text
setBeamAngle(θ, TX/RX)
  │
  ├─ calculatePhaseSettings(θ, phase[4])   // 算出本片 4 通道的相位码
  │     element_spacing = λ/2
  │     Δφ = 2π·(λ/2)·sin(θ)/λ = π·sin(θ)
  │     phase[i] = quantize128( i·Δφ )     // i=0..3
  │
  ├─ setAllDevicesTXMode() 或 RXMode()     // 先把 ADTR1107 切到对应方向
  │
  └─ for dev in 0..3:                      // 4 片
        for ch in 1..4:                    // 每片 4 通道（1-based）
           adarSetTx/RxPhase(dev, ch, phase[ch-1])   // 写 PHS_I + PHS_Q
           adarSetTx/RxVgaGain(dev, ch, default)     // 写增益
```

关键点：相位写入不是直接写一个「相角」寄存器，而是查表得到 `(I, Q)` 两个字节，分别写到 `CHx_RX_PHS_I` 与 `CHx_RX_PHS_Q`，最后向 `REG_LOAD_WORKING(0x028)` 写 1 把影子寄存器搬进工作寄存器（ADAR1000 的双缓冲机制，保证 4 通道同时生效）。

相邻单元的相位增量满足：

\[
\Delta\varphi = \frac{2\pi}{\lambda}\,d\,\sin\theta
\]

本系统取单元间距 \(d=\lambda/2\)（10.5 GHz 对应 \(\lambda\approx 2.857\,\text{cm}\)，故 \(d\approx 1.43\,\text{cm}\)），于是 \(\Delta\varphi=\pi\sin\theta\)。第 \(i\) 个单元（相对第一个）的相位为 \(i\cdot\Delta\varphi\)，再量化到 128 级网格（每级 \(360/128=2.8125^\circ\)）。

#### 4.1.3 源码精读

**(a) 角度→相位码：相控阵数学**

[`ADAR1000_Manager.cpp`:L740-L755](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_Manager.cpp#L740-L755) 把上面的公式原样实现：先算每单元相位增量 `phase_shift`，再给 4 个通道乘以 0/1/2/3，最后 `(element_phase / 2π) * 128` 量化成 `uint8_t`。注意源码用 `while` 把相位归一到 \([0,2\pi)\)，等价于对 128 取模。

```cpp
const float freq = 10.5e9;
const float wavelength = c / freq;            // ≈ 2.857e-2 m
const float element_spacing = wavelength / 2; // d = λ/2
float phase_shift = (2*M_PI*element_spacing*sin(angle_rad))/wavelength; // = π·sinθ
for (int i = 0; i < 4; ++i) {
    float element_phase = i * phase_shift;
    ...
    phase_settings[i] = (element_phase / (2*M_PI)) * 128;  // 量化到 128 级
}
```

**(b) 矢量调制器查找表 VM_I / VM_Q**

128 个相位角对应的 (I,Q) 字节是芯片固有的特性数据，来自 ADAR1000 数据手册 Rev.B Table 13-16。源码把这两张表硬编码进来，并在注释里说明了字节格式（[`ADAR1000_Manager.cpp`:L23-L42](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_Manager.cpp#L23-L42)）：`bit[5]`=极性（正/负象限）、`bits[4:0]`=幅值。表本身是 [`ADAR1000_Manager.cpp`:L43-L79](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_Manager.cpp#L43-L79)。注释（L81-L87）特别提醒：**不存在独立的 VM_GAIN 表**——幅值就编码在 I/Q 字节的低 5 位里；要改通道增益得用另一个寄存器 `CHx_RX_GAIN`。

**(c) 把相位写进通道：adarSetRxPhase**

[`ADAR1000_Manager.cpp`:L870-L880](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_Manager.cpp#L870-L880)：查表 → 算出该通道的 `PHS_I`/`PHS_Q` 寄存器地址（基地址 + `(channel&0x03)*2`，因为 I/Q 各占一址交替排列）→ 两次 `adarWrite` → 触发 `REG_LOAD_WORKING`。

```cpp
uint8_t i_val = VM_I[phase % 128];
uint8_t q_val = VM_Q[phase % 128];
uint32_t mem_addr_i = REG_CH1_RX_PHS_I + (channel & 0x03) * 2; // 0x014,0x016,0x018,0x01A
uint32_t mem_addr_q = REG_CH1_RX_PHS_Q + (channel & 0x03) * 2; // 0x015,0x017,0x019,0x01B
adarWrite(deviceIndex, mem_addr_i, i_val, broadcast);
adarWrite(deviceIndex, mem_addr_q, q_val, broadcast);
adarWrite(deviceIndex, REG_LOAD_WORKING, 0x1, broadcast);      // 影子→工作寄存器
```

寄存器地址全部定义在 [`ADAR1000_Manager.h`:L180-L225](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_Manager.h#L180-L225)：RX 增益 `0x010-0x013`、RX 相位 `0x014-0x01B`、TX 增益 `0x01C-0x01F`、TX 相位 `0x020-0x027`。

**(d) SPI 指令帧：adarWrite**

[`ADAR1000_Manager.cpp`:L797-L813](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_Manager.cpp#L797-L813)：ADAR1000 的 SPI 写是 3 字节——`[ (dev_addr<<5) | (mem_addr>>8) , mem_addr_low , data ]`。`dev_addr` 是每片芯片的 2 位硬件地址（用于同总线多片寻址），通过拉低对应 CS 选片（`CHIP_SELECTS[4]`，[`L13-L21`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_Manager.cpp#L13-L21)）。`broadcast=1` 时首字节改写成 `0x08`，可同时写挂同一 SPI 的所有芯片。

**(e) 整片波束设置：setBeamAngle**

[`ADAR1000_Manager.cpp`:L242-L268](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_Manager.cpp#L242-L268)：调用 `calculatePhaseSettings` 得到 4 通道相位，再 `for dev × for ch` 把同一套 4 相位定律写到 4 片芯片，并配默认 VGA 增益（`kDefaultRxVgaGain=30`、`kDefaultTxVgaGain=0x7F`，见 [`ADAR1000_Manager.h`:L127-L136](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_Manager.h#L127-L136)）。

> 需要全 16 通道各自不同相位时（例如做完整的 16 单元阵列综合），改用 [`setCustomBeamPattern16`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_Manager.cpp#L673-L685)（[`L673-L685`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_Manager.cpp#L673-L685)），它接收 16 字节相位数组，按 `phase_pattern[dev*4+ch]` 分发——这与天线阵列的具体排布有关，确切几何关系需结合原理图与 [`docs/AERIS_Antenna_Report.pdf`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/AERIS_Antenna_Report.pdf) 确认。

#### 4.1.4 代码实践

**目标**：不依赖硬件，手算一遍「角度→相位码」的换算，验证你对公式和查找表的理解。

**步骤**：

1. 取波束角 \(\theta=30^\circ\)，按 \(\Delta\varphi=\pi\sin\theta\) 算 4 个通道的相位码：`phase[i] = (i * 64 * sin(30°)) mod 128`（因为 `(i·π·sinθ)/(2π)·128 = i·64·sinθ`）。
2. 把算出的 4 个码（应是 `0, 32, 64, 96`）分别查 [`VM_I/VM_Q 表`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_Manager.cpp#L43-L79)，读出对应字节。
3. 写一张小表：通道号 | 相位码 | VM_I 字节 | VM_Q 字节。

**需要观察的现象**：相位码 0 对应 `VM_Q[0]=0x20`（纯 Q 正向、I 为 0），相位码 32（即 90°）对应 `VM_I[32]=0x21`（纯 I 正向、Q 为 0）——这印证了「相位角由 (I,Q) 矢量方向决定」。

**预期结果**：\(\sin 30^\circ=0.5\)，故 `phase = [0, 32, 64, 96]`。查表得 `VM_I=[0x3F,0x21,0x1F,0x01]`、`VM_Q=[0x20,0x3D,0x20,0x3D]`（以源码表为准）。若你的结果一致，说明你已掌握「角度→相位码→(I,Q) 字节」的完整链路。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `adarSetRxPhase` 要写 `PHS_I` 和 `PHS_Q` 两个寄存器，而不是一个「相位」寄存器？

**答案**：ADAR1000 用矢量调制器实现移相，相位是由正交分量 (I, Q) 的比值与符号共同决定的复数辐角；芯片没有单一的相角寄存器，必须分别给出 I、Q 两路的幅值（含极性位），所以是两个寄存器。

**练习 2**：`setBeamAngle` 把同一套 4 通道相位定律写到 4 片芯片。如果你想给 16 个通道各自不同的相位，应该调用哪个函数？

**答案**：调用 `setCustomBeamPattern16(phase_pattern[16], direction)`，它按 `phase_pattern[dev*4+ch]` 把 16 个相位分发到 4 片 × 4 通道。

**练习 3**：`adarWrite` 里 `instruction[0] = (dev_addr & 0x03) << 5`，为什么是 `<<5` 且掩码 `0x03`？

**答案**：ADAR1000 的 SPI 指令字节里 `bit[7]` 是读/写位、`bit[6]` 是广播位、`bits[4:5]`（即 `bit[5:4]`，2 位）放芯片地址，所以把 2 位 `dev_addr`（`&0x03`）左移到 `bit[5:4]`（`<<5` 实际让 2 位地址落在 bit6:5，与 `(mem_addr>>8)` 的低位拼接；确切位域以数据手册为准）。其作用是让挂在同一 SPI 总线、共享 CS 之外用地址区分的多片芯片各自只响应属于自己的命令。

---

### 4.2 PA 增益控制：ADAR1000_AGC 外环

#### 4.2.1 概念说明

`ADAR1000_AGC` 是 STM32 侧的**外环自动增益控制**。它调整的不是 PA 功率，而是 ADAR1000 每个接收通道的 **VGA（可变增益放大器）增益**，目标是让回波幅度始终落在 ADC 的最佳范围里——既不饱和（饱和会削顶丢信息），也不太小（太小会被噪声淹没）。

它是「混合 AGC」的外半圈（详见 [u9-l1](u9-l1-hybrid-agc-loop.md)，本讲只看 STM32 这侧）：

- **内环（FPGA，逐采样）**：`rx_gain_control` 在数字域根据峰值/饱和快速调，范围 ±42 dB，反应最快但动态范围有限。
- **外环（本模块，逐帧）**：每帧（约 258 ms）读一次 FPGA 通过 GPIO `DIG_5`（`PD13`）送来的「本帧是否饱和」标志，若饱和就降 ADAR1000 的模拟增益，连续多帧不饱和才缓慢升回来。模拟增益动态范围大，是内环的「粗调」。

`cal_offset[16]` 数组给每个通道一个独立的有符号偏置，用来校正 16 路之间的增益不一致（通道均衡）。

#### 4.2.2 核心流程

`ADAR1000_AGC::update(fpga_saturation)` 每帧调用一次，是一个经典的 attack/decay（快降慢升）控制环：

```text
update(saturated):
  if not enabled: 直接返回（手动模式，不自动调）
  if saturated:                         // ATTACK（快降）
      saturation_event_count++
      holdoff_counter = 0               // 复位恢复计时
      agc_base_gain -= gain_step_down   // 默认 -4，立即降
      clamp to min_gain
  else:                                 // DECAY（慢升）
      holdoff_counter++
      if holdoff_counter >= holdoff_frames:   // 默认连续 4 帧不饱和
          holdoff_counter = 0
          agc_base_gain += gain_step_up  // 默认 +1，缓慢升
          clamp to max_gain

applyGain(mgr):                          // 把决定好的增益写进 16 路 VGA
  for dev in 0..3:
     for ch in 0..3:
        g = clamp(agc_base_gain + cal_offset[dev*4+ch], min, max)
        mgr.adarSetRxVgaGain(dev, ch+1, g)   // 注意通道是 1-based
```

每通道的有效增益公式：

\[
\text{VGA}[dev][ch] = \mathrm{clamp}\big(\text{agc\_base\_gain} + \text{cal\_offset}[dev\cdot4+ch],\ \text{min\_gain},\ \text{max\_gain}\big)
\]

「快降慢升」的用意：饱和是有害的（信号已失真），必须立刻处理；而噪声底下回波变弱是常态，贸然升增益会引入自激，所以必须连续若干帧都确认「确实不饱和」才小幅升一档。

#### 4.2.3 源码精读

**(a) 安全默认值（构造函数）**

[`ADAR1000_AGC.cpp`:L14-L27](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_AGC.cpp#L14-L27)：`agc_base_gain=30`（与 `kDefaultRxVgaGain` 对齐）、`gain_step_down=4`、`gain_step_up=1`、`min_gain=0`、`max_gain=127`、`holdoff_frames=4`、`enabled=false`。`enabled` 默认关——开机不自动调，需显式打开才进入闭环。

**(b) attack/decay 主逻辑**

[`ADAR1000_AGC.cpp`:L34-L71](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_AGC.cpp#L34-L71) 是上一节伪代码的精确实现。饱和分支里 `if (agc_base_gain >= gain_step_down + min_gain)` 防止下溢；恢复分支里 `if (agc_base_gain + gain_step_up <= max_gain)` 防止上溢，两处都做了钳位。

**(c) 每通道增益计算**

[`ADAR1000_AGC.cpp`:L103-L116](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_AGC.cpp#L103-L116)：`effectiveGain` 用 `int16_t` 接收 `base + offset` 以避免 `uint8_t` 下溢，再分别与 `min_gain`/`max_gain` 比较钳位；越界通道号回退到 `min_gain`（安全兜底）。

**(d) 写入 16 路 VGA**

[`ADAR1000_AGC.cpp`:L79-L88](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_AGC.cpp#L79-L88)：`AGC_NUM_DEVICES=4`、`AGC_NUM_CHANNELS=4`（见 [`ADAR1000_AGC.h`:L30-L34](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_AGC.h#L30-L34)），双重循环写 16 路；通道参数传 `ch+1`（Manager 约定 1-based，与 `setBeamAngle` 一致）。架构概述见 [`ADAR1000_AGC.h`:L7-L18](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_AGC.h#L7-L18)。

#### 4.2.4 代码实践

**目标**：用纸笔推演 AGC 在一段饱和序列下的 `agc_base_gain` 变化，确认你理解 attack/decay 节奏。

**步骤**：假设初始 `agc_base_gain=30`，连续输入如下 6 帧的 `fpga_saturation` 标志：`[true, false, false, false, false, false]`（`holdoff_frames=4`）。逐帧记录 `agc_base_gain`、`holdoff_counter`。

**需要观察的现象**：第 1 帧饱和→立即降 4；之后连续 5 帧不饱和，但只有每攒满 4 帧才升 1。

**预期结果**：

| 帧 | saturated | agc_base_gain | holdoff_counter |
|---|---|---|---|
| 起始 | — | 30 | 0 |
| 1 | true | 26 | 0 |
| 2 | false | 26 | 1 |
| 3 | false | 26 | 2 |
| 4 | false | 26 | 3 |
| 5 | false | 27 | 0（升 1 后清零） |
| 6 | false | 27 | 1 |

结论：降一档只需 1 帧，升一档需要 4 帧——这正是「快降慢升」。完整行为以 [`ADAR1000_AGC.cpp`:L34-L71](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_AGC.cpp#L34-L71) 源码为准（本表为推演，待本地用单元测试验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `enabled` 默认是 `false`？

**答案**：开机阶段系统尚未稳定（波形、电平、温度都在收敛），此时若让 AGC 自动调增益容易误判。默认关闭、由上层在确认链路正常后显式打开，是安全做法。

**练习 2**：`cal_offset` 数组解决什么问题？如果所有通道理想一致，它应取什么值？

**答案**：解决 16 路天线/通道之间的增益不一致（通道均衡）。理想一致时全 0，`effectiveGain` 就等于 `agc_base_gain`。

**练习 3**：把 `gain_step_down` 调成 1、`gain_step_up` 调成 4，系统行为会变成什么样？为什么不推荐？

**答案**：会变成「慢降快升」——饱和时降得很慢（信号持续削顶），不饱和时升得很快（容易自激/过载）。这与雷达 AGC「宁可保守也不要饱和」的设计目标相反，所以不推荐。

---

### 4.3 Idq 闭环校准：DAC5578 设 Vg、ADS7830 读 Idq

#### 4.3.1 概念说明

功放（PA）的栅极负压 Vg 决定了它的静态工作电流 Idq。因为每颗 PA 管的阈值电压有离散性，同样的 Vg 在不同通道上得到的 Idq 不一样；而 Idq 直接影响线性度、效率与寿命。所以 AERIS-10 对 16 路 PA **逐通道闭环**：用一个 DAC 设 Vg，用一个 ADC 测 Idq，循环微调 Vg 直到 Idq 落在目标值（1.680 A，容差 ±0.2 A）。

硬件链路（16 路 PA = 2 片 DAC + 2 片 ADC，每片 8 通道）：

```text
          ┌─ DAC5578 #1 (I2C1, 0x48, 板级标注 U7)  ─ Vg[0..7]  ─┐
STM32 ─I2C┤                                                     ├─→ 16 路 PA 栅极
          └─ DAC5578 #2 (I2C1, 0x49, 板级标注 U69) ─ Vg[8..15] ─┘

16 路 PA 漏极电流 Idq
   │  每路串一个 5 mΩ 采样电阻
   ▼
INA241A3 电流检测放大器（增益 G=50）   ← 把 5mΩ 上的微小压降放大 50 倍
   │
   ▼
ADS7830 #1 (I2C2, 0x48, 板级标注 U88) ─ 读 Idq[0..7]
ADS7830 #2 (I2C2, 0x4A, 板级标注 U89) ─ 读 Idq[8..15]
```

> 注：U7/U69/U88/U89 是板级原理图的设计ator，本讲据任务规格引用；确切位号与供电请以 [`RADAR_Main_Board.sch`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_6_Schematics/MainBoard/RADAR_Main_Board.sch) 为准。
>
> 另一个易混点：`ADAR1000_Manager` 里也有 `REG_PA_CH*_BIAS_ON` 这组偏置寄存器（4.1 里 `setPABias`/`setADTR1107Mode` 会写），那是经 ADAR1000 片内偏置 DAC 去驱动 ADTR1107 的路径；而本节的 DAC5578/ADS7830 是**独立的外部高精度闭环链路**，用于把 16 路 GaN PA 的 Idq 校准到 1.680 A。两套机制并存，确切由哪一套驱动最终 PA 偏置需结合原理图确认（待确认）。

#### 4.3.2 核心流程

开机时 `main.cpp` 的 `if(PowerAmplifier){...}` 块（[`main.cpp`:L1840-L1972](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1840-L1972)）按下面顺序工作：

```text
1. 初始化 2 片 DAC5578（地址 0x48/0x49，8 位，配 LDAC=0xFF、CLR 清零码=ZERO）
2. 给 16 路 DAC 写初值 DAC_val=126（对应一个较安全的 Vg 起点）
3. 脉冲 LDAC，让 16 路同时更新；使能 RFPA VDD=22V
4. 初始化 2 片 ADS7830（地址 0x48/0x4A，单端、内部参考开 + ADC 开）
5. 首次读取 16 路 Idq（仅观察初值）
6. 对每片 DAC、每个通道跑闭环：
      DAC_val = 126
      do:
         safety_counter++  （上限 50，防死循环）
         DAC_val -= 4                        // Vg 朝「更大 Idq」方向步进
         DAC5578_WriteAndUpdateChannelValue(dac, ch, DAC_val)
         raw = ADS7830_Measure_SingleEnded(adc, ch)
         Idq = (3.3/255)*raw / (50*0.005)     // 换算成安培
      while ( DAC_val > 38  &&  |Idq - 1.680| > 0.2 )   // 远离目标就继续，进入容差就停
```

电流换算公式（Vg 越负 Idq 越小；代码里 `DAC_val` 减小对应 Vg 变化使 Idq 增大，逐步逼近目标）：

\[
I_{dq} = \frac{V_{adc}}{G\cdot R_{shunt}} = \frac{(3.3/255)\cdot \text{raw}}{50 \times 0.005}
\]

其中分母 \(G\cdot R_{shunt}=50\times0.005=0.25\ \Omega\)，于是 \(I_{dq}=\text{raw}\times\frac{3.3}{255\times0.25}\approx\text{raw}\times0.0518\ \text{A}\)。例如目标 1.680 A 对应 raw ≈ 32–33（与测试用例一致）。

> **重要历史教训（Bug #12，已修）**：这个 do-while 的循环条件曾经写反成 `abs(Idq-1.680) < 0.2`——那样只有「已经接近目标」时才继续循环、一旦「远离目标」反而立刻退出，结果永远校不准。修复后改为 `> 0.2`：远离目标时继续调，进入容差带才停。配套的 `tests/test_bug12_pa_cal_loop_inverted.c` 用一组已知 Idq 值锁死了这个语义。
>
> **Bug #13（已修）**：DAC2 的循环里曾误用 `adc1_readings`（读了 ADC1 的值去校 DAC2 的通道），修复成 `adc2_readings`。

#### 4.3.3 源码精读

**(a) 全局句柄与缓冲**

[`main.cpp`:L201-L214](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L201-L214)：`hdac1/hdac2`（Vg 用）、`hadc1/hadc2`（Idq 用）、`adc1_readings[8]/adc2_readings[8]`（原始字节）、`Idq_reading[16]`（换算后的安培）、`DAC_val=126`（初始码值）。

**(b) DAC5578 初始化 + LDAC/CLR 配置**

[`main.cpp`:L1845-L1871](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1845-L1871)：两片 DAC 分别在 I2C1 的 0x48/0x49，8 位分辨率；`SetClearCode(ZERO)` 让 CLR 引脚被拉低时所有输出归 0 V（紧急停机用）；`SetupLDAC(0xFF)` 让 8 个通道都响应硬件 LDAC，从而可以「同时」更新（避免 16 路依次刷新时出现瞬态不一致）。

**(c) ADS7830 初始化 + 首次读 Idq**

[`main.cpp`:L1900-L1930](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1900-L1930)：两片 ADC 在 I2C2 的 0x48/0x4A，单端模式、内部参考与 ADC 均开启（`ADS7830_PDIRON_ADON`）。首次把 16 路 raw 读进 `adc*_readings` 并用公式换算成 `Idq_reading[]`。公式注释明确写出 `G_INA241A3=50; Rshunt=5mOhms`。

**(d) 闭环校准循环（核心）**

DAC1 段在 [`main.cpp`:L1932-L1950](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1932-L1950)，DAC2 段在 [`main.cpp`:L1952-L1970](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1952-L1970)，结构完全对称，是本节主角。摘 DAC1 段：

```cpp
for (uint8_t channel = 0; channel < 8; channel++){
    uint8_t safety_counter = 0;
    DAC_val = 126;                       // 每通道都从 126 重新开始
    do {
        if (safety_counter++ > 50) break;        // 防死循环
        DAC_val = DAC_val - 4;                    // 步进 4
        DAC5578_WriteAndUpdateChannelValue(&hdac1, channel, DAC_val);
        adc1_readings[channel] = ADS7830_Measure_SingleEnded(&hadc1, channel);
        Idq_reading[channel] = (3.3/255) * adc1_readings[channel] / (50 * 0.005);
    } while (DAC_val > 38 && abs(Idq_reading[channel] - 1.680) > 0.2);  // Bug #12 修复
}
```

四个细节值得注意：①每通道独立、`DAC_val` 每通道复位到 126；②`safety_counter` 上限 50 防止硬件无响应时死循环；③下界 `DAC_val > 38` 是硬保护，防止 Vg 被推到危险区；④退出条件是「进入 ±0.2 A 容差带」或「触底」。

**(e) DAC5578 写一条命令**

[`DA5578.c`:L140-L151](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/DA5578.c#L140-L151) 的 `DAC5578_WriteAndUpdateChannelValue` 校验通道号 ≤7、把 value 掩到 8 位，再调 `DAC5578_CommandWrite` 发 3 字节：`[ 命令字节 , MSB(=0) , LSB(数据) ]`，命令字节 = `DAC5578_CMD_WRITE_UPDATE | (channel&0x7)`（`0x2<<4`，见 [`DAC5578.H`:L16-L26](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/DAC5578.H#L16-L26)）。

**(f) ADS7830 单端采样**

[`ADS7830.c`:L75-L138](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADS7830.c#L75-L138) 的 `ADS7830_Measure_SingleEnded`：拼一个命令字节（`SD模式 | 通道选择 | 掉电模式`，定义见 [`ADS7830.H`:L21-L48](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADS7830.H#L21-L48)）→ `HAL_I2C_Master_Transmit` 写命令 → 等 `conversion_delay`(=1ms) → `HAL_I2C_Master_Receive` 读回 1 字节结果。失败返回 `0xFF`。

**(g) 周期性重读 + 健康检查 + 紧急停机**

- 周期重读：`Idq_reading[]` 不是只在开机填一次。每 5 秒（与温度巡检同一个 5s 节拍，[`main.cpp`:L2102-L2151](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L2102-L2151)）重读 16 路、刷新数组——这是 Gap-3 Fix 4，避免健康检查用陈旧值。
- 健康检查：[`main.cpp`:L734-L748](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L734-L748) 对每路判 `>2.5A` 报过流、`<0.1A` 报偏置故障。
- 紧急停机：[`main.cpp`:L820-L826](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L820-L826) 的 `Emergency_Stop()` 调 `DAC5578_ActivateClearPin` 把两片 DAC 的 CLR 拉低→因清零码是 ZERO，16 路 Vg 瞬时归 0，PA 立刻关断（[`DA5578.c`:L330-L337](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/DA5578.c#L330-L337)）。

#### 4.3.4 代码实践

**目标**：把 Idq 换算公式和闭环退出条件跑过一遍，并用项目自带的单元测试验证语义。

**步骤**：

1. **手算换算**：用公式 \(I_{dq}=\text{raw}\times 0.0518\) 算 raw=0、33、48、255 对应的电流，并判断分别落在「偏置故障 <0.1A / 正常 / 临界过流 / 过流」哪一档。
2. **跑单元测试**：进入 `9_Firmware/9_1_Microcontroller/tests/` 目录，运行：
   ```bash
   make test_bug12_pa_cal_loop_inverted
   make test_gap3_idq_periodic_reread
   ```
   （或直接 `make test` 跑全套；这两个测试不需要真硬件，用 shim/mock 在 PC 上编译运行，详见 [u1-l4](u1-l4-toolchain-and-running.md) 与 `tests/Makefile`。）

**需要观察的现象**：

- `test_bug12` 会打印 7 条用例，覆盖「远离目标→继续 / 进入容差→退出 / 触底→退出 / 临界值」等情况。
- `test_gap3_idq_periodic_reread` 会打印 raw=33 对应 Idq≈1.709A、raw=255 对应≈13.2A 等，并断言过流/故障阈值判定正确。

**预期结果**：两个测试最后都应打印 `=== ... ALL TESTS PASSED ===`。其中 raw=33 → ≈1.709A（落在校准目标 1.680A 的 ±0.1A 内），raw=255 → ≈13.2A（远超 2.5A 过流阈值）。能否在你本机顺利编译运行取决于工具链（`cc`/`c++` 与 `-Ishims` 路径），若 Makefile 报缺失依赖请按 [u1-l4](u1-l4-toolchain-and-running.md) 安装；测试本身的断言语义以源码 [`test_bug12_pa_cal_loop_inverted.c`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/test_bug12_pa_cal_loop_inverted.c) 与 [`test_gap3_idq_periodic_reread.c`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/test_gap3_idq_periodic_reread.c) 为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么闭环用 `do-while` 而不是 `while`？如果用 `while`，第一次进入循环体前 `Idq_reading[channel]` 的值是什么？

**答案**：`do-while` 保证至少执行一次，从而在判断条件前先把 `DAC_val` 减 4、写出新 Vg 并读回真实 Idq。若改用 `while`，第一次判断用的是「上一步（首次读取或上一个通道）」留下的 `Idq_reading[channel]` 旧值，可能因陈旧值而提前退出，校不准。

**练习 2**：`safety_counter` 上限 50、`DAC_val` 步进 4、起点 126、下界 38。最坏情况下一个通道会迭代多少次？为什么 50 是安全的？

**答案**：从 126 每次减 4 到 38，需要 (126-38)/4 = 22 次就会触底退出。50 远大于 22，留了双倍余量；它的真正意义是硬件无响应（ADC 一直读不到合理值、Idq 永远不进容差带）时防止死循环——即便下界保护失效，50 次后也会强制 break。

**练习 3**：健康检查里过流阈值是 2.5A，但校准目标是 1.680A。为什么过流阈值要比目标高这么多？

**答案**：发射期间 PA 流过的是叠加了射频信号的动态电流，会显著高于静态 Idq；阈值必须留出足够余量，让「正常发射」不被误判为故障，同时仍能在真正异常（如管子击穿、偏置失控）时及时报过流并触发紧急停机。

---

## 5. 综合实践

把本讲三块内容串成一个「板日调试」小任务：假设你在实验室给一块新主板做 PA 上电，需要把「设波束 → 校 Idq → 监控健康」走通。请按顺序完成：

1. **画链路图**：在纸上画出从 STM32 到 16 路 PA 的完整模拟前端，标注：
   - SPI1 上的 4 片 ADAR1000（CS、dev_addr）、每片 4 通道相位定律由 `calculatePhaseSettings` 给出；
   - I2C1 上的 2 片 DAC5578（0x48/0x49）设 Vg，I2C2 上的 2 片 ADS7830（0x48/0x4A）读 Idq，中间是 5mΩ + INA241A3(G=50)。
2. **读日志定位阶段**：打开串口（`huart3`）的 `DIAG("PA", ...)` 日志，按顺序找到这些关键行，并把它们对应到源码行号：
   - `Initializing DAC1/DAC2`（[`main.cpp`:L1844-L1861](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1844-L1861)）
   - `Reading initial Idq from ADC1/ADC2`（[`L1917-L1930`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1917-L1930)）
   - `Starting Idq calibration loop ... (target=1.680A)` 与每通道的 `calibrated` 行（[`L1932-L1970`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1932-L1970)）
   - `Periodic IDQ re-read`（[`L2142-L2151`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L2142-L2151)）
3. **判读校准结果**：若某通道日志显示 `safety limit reached (50 iterations)`，结合 4.3.3 的循环逻辑说明可能的原因（候选：该通道 PA 损坏/偏置供电缺失/ADC 读不到值/采样电阻开路），并给出用万用表与 `verifyDeviceCommunication` 进一步定位的步骤。
4. **设一个 RX 波束角**：调用 `setBeamAngle(30.0f, BeamDirection::RX)`，用 4.1.4 的方法算出写给每片 ADAR1000 的 4 个相位码（应为 `[0,32,64,96]`），说明它们如何经 `adarSetRxPhase` 变成 `PHS_I/PHS_Q` 字节。
5. **验证安全网**：解释一旦 `checkSystemHealth` 检测到某路 `Idq>2.5A`，`Emergency_Stop` 如何经 CLR 引脚在毫秒级把 16 路 Vg 归零（4.3.3(g)）。

> 本任务为「源码阅读 + 日志判读 + 手算」型实践，不需要真硬件即可完成第 1/2/4/5 步；第 3 步的故障定位结论待本地用真实板日数据验证。

## 6. 本讲小结

- **相移器管理**：`ADAR1000Manager` 管 4 片 ADAR1000（16 通道）。`calculatePhaseSettings` 用相控阵公式 \(\Delta\varphi=\frac{2\pi}{\lambda}d\sin\theta\)（\(d=\lambda/2\)）算出 4 通道相位码并量化到 128 级（2.8125°/级）；`adarSetRx/TxPhase` 查 VM_I/VM_Q 表把相位写成 (PHS_I, PHS_Q) 两字节并触发 `REG_LOAD_WORKING` 双缓冲搬移；SPI 用 4 根 CS + 2 位 dev_addr 寻址。
- **PA 增益控制**：`ADAR1000_AGC` 是逐帧外环，读 FPGA 的 DIG_5 饱和标志，按「快降 4、慢升 1、holdoff 4 帧」调 16 路 RX VGA 增益，并用 `cal_offset[16]` 做通道均衡；它是混合 AGC（FPGA 内环 + STM32 外环）的外半圈。
- **Idq 闭环校准**：2 片 DAC5578（0x48/0x49）设 16 路 Vg，2 片 ADS7830（0x48/0x4A）经 5mΩ 采样电阻 + INA241A3(G=50) 读回 Idq，换算 \(I_{dq}=(3.3/255)\cdot\text{raw}/0.25\)；开机 do-while 循环把每通道 Idq 闭环到 1.680A±0.2A，带 50 次/触底双重防死循环保护。
- **安全网**：每 5 秒周期性重读 Idq（Gap-3 Fix 4）；健康检查判 >2.5A 过流、<0.1A 偏置故障；紧急停机用 DAC5578 的 CLR 引脚把 Vg 瞬时归零。
- **历史教训**：Bug #12（循环条件写反，`<` vs `>`）与 Bug #13（DAC2 误用 adc1_readings）都被专门的单元测试锁死，是「为什么这类闭环必须有测试」的活教材。

## 7. 下一步学习建议

- 想看「混合 AGC」的另一半（FPGA 内环 `rx_gain_control` 如何逐采样调数字增益、如何经 DIG_5/DIG_6 把饱和标志送给 STM32、GUI 如何展示），直接进入 [u9-l1 Hybrid AGC：FPGA + STM32 + GUI 联动](u9-l1-hybrid-agc-loop.md)。
- 想了解 ADAR1000/ADTR1107 的**收发模式切换与偏置时序**（`switchToTXMode`/`setADTR1107Mode` 里 LNA/PA 供电与 T/R 开关的先后），重读 [`ADAR1000_Manager.cpp` 的模式切换段](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ADAR1000_Manager.cpp#L155-L205)（L155-L205、L568-L651）并结合 [u5-l1 PLFM Chirp 生成与发射机](u5-l1-plfm-chirp-and-transmitter.md)。
- 想验证本讲涉及的寄存器位域与命令字节是否在三套代码里一致，进入 [u11-l3 跨层契约测试](u11-l3-cross-layer-contract-tests.md) 与 [u11-l2 STM32 单元测试与 bug 回归](u11-l2-mcu-unit-tests.md)，那里会专门讲 `test_bug12`/`test_gap3_*` 这类测试如何用 shim/mock 在 PC 上跑嵌入式代码。
