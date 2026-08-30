# 片上信号处理：加窗与 DFT

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `Hann.dat` / `Kaiser.dat` / `Flattop.dat` 三个窗系数文件是如何在**综合期**被 `window.vhd` 用 VHDL `textio` 读入、固化成 FPGA 位流里的 ROM 的。
2. 走读 `Windowing.vhd` 的十分状态机：一个 DSP 乘法器如何分时复用完成三路接收通道的「样本 × 窗系数」。
3. 走读 `DFT.vhd` 的五状态机：相位累加器如何用「每样本两次乘法 + 每 bin 一次加法」的递推结构，在 BRAM 里维护 96 个 bin × 4 个 48 位累加器。
4. 用固件侧公式亲手推导「800 kHz 采样、250 kHz 中频、1 kHz 分辨率」需要多少个采样点，并算出 `DFT_FIRST_BIN` / `DFT_FREQ_SPACING` 两个寄存器的具体值。
5. 解释为什么要把加窗和 DFT 做在 FPGA 片上，而不是把原始样本传回 PC 处理。
6. 读懂 `Test_DFT.vhd` 注入的激励，推导期望输出，并知道「无断言测试台」的验证边界在哪里。

## 2. 前置知识

### 2.1 为什么要加窗（频谱泄漏）

对一段有限长度的采样序列做 DFT，等价于先把无限长信号乘上一个矩形窗再变换。矩形窗的频谱是个 sinc 函数，主瓣宽、旁瓣高（第一旁瓣只衰减约 13 dB），于是某个频率的能量会「泄漏」到相邻 bin。改用边缘平滑过渡到零的窗（Hann、Kaiser、Flattop），旁瓣可以压到 -40 dB 甚至 -90 dB 以下，代价是主瓣变宽——**分辨率换动态范围**。

衡量主瓣变宽程度的标准参数是窗的**等效噪声带宽系数** \(k_{window}\)（单位：bin）。加窗后的实际分辨率：

\[ RBW = k_{window} \cdot \frac{f_{ADC}}{N} \]

其中 \(N\) 是每点采样的样本数，\(f_{ADC}\) 是 ADC 采样率。固件里那张系数表 `{0.89, 2.23, 1.44, 3.77}` 就是四种窗的 \(k_{window}\)。

### 2.2 DFT 与「逐样本累加」的直接实现

N 点 DFT 的定义：

\[ X_k = \sum_{n=0}^{N-1} x[n] \cdot e^{-j2\pi f_k n / f_s}, \qquad k = 0,1,\dots,BINS-1 \]

本设计的做法不是 FFT，而是最朴素的**直接 DFT**：每来一个样本 \(x[n]\)，就把它与每个 bin 的旋转因子相乘并累加到该 bin 的 accumulator 里，N 个样本累加完就得到全部 bin 的结果。它省去了 FFT 的位反转和蝶形调度，换来两个关键自由度：

- **bin 位置任意可编程**——频谱仪需要 bin 精确对齐扫频点间隔，而不是 FFT 的 \(f_s/N\) 整数倍网格；
- **每样本每 bin 只需一次乘加**，4 个 DSP Slice 分时复用即可跑满 96 bin。

### 2.3 定点数与相位累加器（NCO）

相位累加器是 DDS/数字下变频的标配：一个 32 位无符号计数器，每个时钟加一个步进 `phase_inc`，计满 \(2^{32}\) 自动回绕——正好对应相位转满一圈 \(2\pi\)。于是「每样本相位步进」与「归一化频率」一一对应：

\[ \frac{phase\_inc}{2^{32}} = \frac{f}{f_s} \;(\text{周期/样本}) \]

取相位高 12 位查正弦表即可得到定点 sin/cos。LibreVNA 里 VNA 通路的单 bin 解调（上一讲 `Sampling.vhd`）和本讲的 96 bin DFT 用的都是这套机制，只是相位步进的「每 bin 递增」技巧不同。

### 2.4 FPGA 资源词汇

- **DSP Slice**：Spartan-6 里的硬核乘加单元，本设计通过 CoreGen 包装成 `DSP_SLICE`（.xco 核），实现 \(p = a \times b + c\)，`sel` 端口选择是否把上一次结果 \(p\) 回接到 \(c\) 形成「乘累加」。
- **BRAM**：块存储器。DFT 的 96 个 bin × 4 个 48 位累加器就存在一块 192 位宽的双端口 BRAM 里，读出旧值→DSP 加→写回同一地址，是经典的「读-改-写」累加模式。
- **textio**：VHDL 标准库的文件读写功能，`window.vhd` 用它在**综合/仿真 elaboration 时**把 `.dat` 文本读进 `constant`，从而变成 ROM 初值。

### 2.5 与前几讲的衔接

本讲站在数据流水线的「加窗 → 频域变换」两站（上一讲 u6-l3 讲了它们上游的 `MCP33131.vhd` 采样与 `Sampling.vhd` 单 bin 解调）。回忆 [top.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd) 里的分岔：三路 ADC 原始样本先一起过 `Windowing`，然后**双支并行**——VNA 路走 `Sampling`（单 bin、含参考通道），频谱仪路走 `DFT`（96 bin、只看端口 1/2）。`NSAMPLES` 等控制量由 `Sweep.vhd`（u6-l2）在扫描配置里下发。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [FPGA/VNA/window.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/window.vhd) | 窗系数 ROM：综合期读入三个 .dat 文件，按 `WINDOW_TYPE` 选出一个 16 位系数 |
| [FPGA/VNA/Hann.dat](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Hann.dat) / [Kaiser.dat](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Kaiser.dat) / [Flattop.dat](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Flattop.dat) | 各 128 行、每行 16 个二进制字符的窗系数表 |
| [FPGA/VNA/Windowing.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Windowing.vhd) | 加窗引擎：十分状态机，分时复用一个 DSP 给三路通道乘窗系数 |
| [FPGA/VNA/DFT.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/DFT.vhd) | 96 bin 直接 DFT 核：相位累加器 + BRAM 累加器 + 4 个 DSP |
| [FPGA/VNA/Test_DFT.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_DFT.vhd) | DFT 的仿真测试台（无断言，波形判读型） |
| [FPGA/VNA/Test_Windowing.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_Windowing.vhd) / [Test_Window.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_Window.vhd) | 加窗引擎与窗 ROM 的测试台 |
| [Software/VNA_embedded/Application/SpectrumAnalyzer.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp) | 固件侧：由 RBW 反推样本数、下发 DFT 频率参数、逐 bin 读结果 |
| [Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp) | `SetupDFT` / `ReadDFTResult`：频率↔相位增量的换算与 SPI 读出 |
| [Documentation/DeveloperInfo/FPGA_protocol.tex](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex) | MCU-FPGA 协议文档，含 DFT 寄存器公式与读出时序 |

## 4. 核心概念与源码讲解

### 4.1 窗系数与 Windowing 模块

#### 4.1.1 概念说明

`Windowing` 解决的问题是：在三路 ADC 样本进入频域变换之前，逐样本乘上窗系数。它必须跟上 ADC 的实时速率（默认 800 kHz，每个样本只有 128 个主时钟），所以不能为三路通道各配一个乘法器硬扛——设计者用一个 DSP Slice **分时复用**：端口 1、端口 2、参考三路排队过同一个乘法器，用状态机的「延迟存储」节拍把三个乘积按到达顺序分别锁存。

窗系数本身不是逻辑生成的，而是**预先离线算好、存成文本文件**，综合时读入变成 ROM。这带来一个跨层契约：换窗 = 换 .dat 文件（FPGA 侧）+ 改 `window_factors` 表（固件侧），两处必须同步。

#### 4.1.2 核心流程

`window.vhd`（ROM 侧）：

```
综合期（elaboration）：
  打开 Hann.dat / Kaiser.dat / Flattop.dat
  逐行 read 进 128 × 16bit 的 constant 数组     ← 成为位流中的 ROM 初值
运行期（每个时钟）：
  INDEX(7bit) + WINDOW_TYPE(2bit) → 打一拍 → VALUE(16bit)
    "00" → 常数 0x1000（矩形窗）
    "01" → Kaiser.dat[INDEX]
    "10" → Hann.dat[INDEX]
    "11" → Flattop.dat[INDEX]
```

`Windowing.vhd`（引擎侧），每个 ADC 样本走一轮：

```
CalcWindowInc   根据 NSAMPLES 选好「每样本 index 步进量」
WaitingForADC   等 ADC_READY 脉冲
CalcPort1       mult_b ← PORT1_RAW，mult_enable=1     ┐
CalcPort2       mult_b ← PORT2_RAW                     ├ 三次乘法背靠背进流水线
CalcRef         mult_b ← REF_RAW                      ┘
MultDelay1/2    等待 DSP 流水线延迟
StorePort1      取 mult_p(30..13) → PORT1_WINDOWED     ┐
StorePort2      取 mult_p(30..13) → PORT2_WINDOWED     ├ 三个乘积依次冒出流水线
StoreRef        取 mult_p(30..13) → REF_WINDOWED，     ┘
                WINDOWING_DONE=1，窗索引前进一步
```

窗索引的推进是个小小的「Bresenham 式」分数步进：128 项的窗要均匀铺满整个测量点 \(16 \times NSAMPLES\) 个样本，即每样本前进 \(8/NSAMPLES\) 项。整数化实现是让 `window_sample_cnt` 以 `cnt_inc` 为步长累加，每超过 `NSAMPLES` 就回绕一次、`window_index` 加一，`case` 语句按 NSAMPLES 档位挑选 2 的幂次步进（NSAMPLES=1 每样本 +8；2~3 每 2 样本 +4；4~7 加 2；≥8 加 1）。可以验证四个档位的平均速率都是 \(8/NSAMPLES\) 项/样本，于是一个测量点恰好走完整个 128 项窗。

#### 4.1.3 源码精读

先看 ROM 的「文件进位流」机制——[window.vhd:42-54](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/window.vhd#L42-L54) 定义了 `window_data` 数组类型和 `impure function InitWindowDataFromFile`：它用 `textio` 的 `readline`/`read` 逐行读入文件。注意 `for i in window_data'range` 是从 127 降到 0，所以**文件第 1 行落在 rom(127)、第 128 行落在 rom(0)**——对对称窗无所谓，但自己生成系数文件时要记得这个行序。

[window.vhd:56-58](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/window.vhd#L56-L58) 把三个文件初始化成三个 `constant`。`constant` 在综合时被求值并折叠进网表，这就是「系数文件 → 位流内 ROM」的全部魔法；副作用是**综合/仿真时工作目录里必须能找到这三个 .dat 文件**（ISE 工程目录即 `FPGA/VNA/`，仿真时需复制到仿真器工作目录）。

[window.vhd:64-75](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/window.vhd#L64-L75) 是打了一拍的查表进程，`WINDOW_TYPE` 编码在此定死：`"00"` 直接给常数 `"0001000000000000"`（= 0x1000 = 4096，矩形窗），`"01"`/`"10"`/`"11"` 分别查 Kaiser/Hann/Flattop。这个编码与固件 [SpectrumAnalyzer.cpp:250](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L250) 的 `window_factors[4] = {0.89f, 2.23f, 1.44f, 3.77f}` 下标一一对应（矩形、Kaiser、Hann、Flattop），也与协议文档 [FPGA_protocol.tex:368-373](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L368-L373) 的 `Window[1:0]` 寄存器描述一致——**三处同源，这是全项目反复出现的跨层契约模式**。

三个系数文件的实际数值（可用 `sed -n '1p;64p;65p;128p' Hann.dat` 之类核对）：

| 文件 | 边缘值 | 中心峰值 | 形状说明 |
| --- | --- | --- | --- |
| Hann.dat | 1 | 0x1FFE = 8190 | 对称 S 曲线，经典 Hann 形状 |
| Kaiser.dat | 6 | 0x27FD = 10237 | 对称、边缘略高于 Hann |
| Flattop.dat | 0xFFF8（按带符号数读为 −8） | 0x4A2F = 18991 | 中心峰值最高，近边缘存在约 −1340 的**负瓣**（5 项 Flattop 特征），因此该文件必须按补码理解 |

Flattop 的负瓣解释了 [Windowing.vhd:100-103](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Windowing.vhd#L100-L103) 里那两行符号扩展：`mult_a(17:16) <= mult_a(15) & mult_a(15)` 把窗系数当**带符号** 16 位数扩到 18 位——如果没有这一步，Flattop 边缘的 0xFFF8 会被当成 +65528，把样本放大到溢出。Hann/Kaiser 全为正值，不受影响。各窗峰值标度不同（4096/8190/10237/18991），乘法后又统一右移 13 位（见下），绝对增益差异属于固定增益，最终被设备级幅度校准与 SA 归一化吸收。

再看引擎。实体端口 [Windowing.vhd:32-45](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Windowing.vhd#L32-L45)：三路 16 位原始样本进、三路 **18 位**加窗样本出；`NSAMPLES` 是 13 位输入——它同时决定窗的拉伸长度，并被复位逻辑锁存到 `window_sample_compare`（[Windowing.vhd:115](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Windowing.vhd#L115)）。

顶层里它挂在 ADC 之后（[top.vhd:647-660](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L647-L660)）：`RESET => sampling_start`（每个测量点重启一次窗索引）、`ADC_READY => adc_port1_ready`（三片 ADC 同步，就绪以端口 1 为准，见上一讲）、输出同时喂给 `Sampling`（VNA 路，[top.vhd:662-685](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L662-L685)）和 `SA_DFT`（频谱仪路，[top.vhd:825-838](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L825-L838)）——**加窗是两路共用的公共前置级**，`WINDOWING_DONE` 就是下游的 `NEW_SAMPLE`。

[Windowing.vhd:118-136](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Windowing.vhd#L118-L136) 是步进量选择，四个档位如上所述。[Windowing.vhd:145-159](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Windowing.vhd#L145-L159) 三个 Calc 状态背靠背地把三路样本依次装进 `mult_b`，`mult_a` 保持窗系数不动——DSP 流水线里同时有最多三个乘法在飞。[Windowing.vhd:170-186](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Windowing.vhd#L170-L186) 三个 Store 状态按流水线延迟节奏取结果：`PORT1_WINDOWED <= mult_p(30 downto 13)`，即取 48 位乘积的 30..13 位——等价于**乘积右移 13 位再截 18 位**。矩形窗时 4096×样本>>13 = 样本/2，正好把 16 位输入折进 18 位输出而不溢出。

[Windowing.vhd:187-193](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Windowing.vhd#L187-L193) 是分数步进的回绕逻辑，`window_index` 是 7 位信号，加到 128 自然回绕，无缝衔接下一个测量点。

#### 4.1.4 代码实践

**实践目标**：验证「系数文件 → ROM」通路，并亲手核对窗索引的分数步进。

**操作步骤**（纯源码阅读 + 离线计算，无需硬件）：

1. 打开 [FPGA/VNA/Hann.dat](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Hann.dat)，用文本编辑器或 `sed -n '1p;32p;64p;65p;96p;128p' Hann.dat` 抽查对称性：镜像对称轴应在第 64/65 行之间，两侧行的数值应两两相等。
2. 用下面的示例脚本复算一个教科书 Hann 并与文件逐行对比（峰值标度取 8190）：

   ```python
   # 示例代码：复算 128 点 Hann，与仓库 Hann.dat 对比
   import math
   N, peak = 128, 8190.0
   repo = [int(l.strip(), 2) for l in open("FPGA/VNA/Hann.dat")]
   mine = [round(0.5 * (1 - math.cos(2 * math.pi * m / (N - 1))) * peak)
           for m in range(N)]
   for m in (1, 2, 4, 8, 16, 32, 64):
       print(m, repo[m], mine[m])
   ```

3. 手工推演 [Test_Windowing.vhd:116-130](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_Windowing.vhd#L116-L130) 的激励：`NSAMPLES = "0000000010001"` = 17，即一个测量点 17×16 = 272 个样本（测试台循环恰好跑 272 次）；`WINDOW_TYPE = "10"` 选 Hann；三路输入是三个不同幅度的直流（128/256/512）；`ADC_READY` 每 111 个时钟脉冲一次。NSAMPLES=17 落在 `others` 档位（cnt_inc=8），据此列出前 10 个样本各自的 `window_sample_cnt` 与 `window_index` 值。

**需要观察的现象 / 预期结果**：

- 第 2 步的对比会发现：中段（m ≥ 8）复算值与文件吻合在百分之几以内，但边缘前几点偏差明显（如 m=1 文件为 1 而公式约 5）。结论：**仓库系数不是教科书公式的精确采样**，生成脚本含特定处理（具体生成式待确认）；以文件为准。这本身就是重要一课——不要轻信「应该是」，要读数据。
- 第 3 步的推演应得到：`window_sample_cnt` 从 0 起按档位步进累加、达到比较值就减去比较值回绕并令 `window_index` 前进一步；一个测量点累计正好走完 128 项。若在 ISE 里跑 `Test_Windowing.vhd`（测试台选了 Hann、三路恒定直流输入），波形上应看到三路输出同相、幅度包络按 Hann 起伏——**待本地验证**（需要 ISE 14.7 仿真环境，且 .dat 文件须在仿真工作目录）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `WINDOW_TYPE="00"`（矩形窗）不查 ROM，而是给常数 0x1000？

**答案**：矩形窗所有系数相同（都是 1），无需存储；给常数省掉一次查表。取 0x1000=4096 而非更大值，配合输出右移 13 位（4096>>13 = 0.5），保证 16 位满量程样本乘窗后仍落在 18 位输出范围内不溢出。

**练习 2**：若把 `Windowing.vhd` 的 `RESET` 从 `sampling_start` 改成全局复位 `int_reset`，会有什么后果？

**答案**：`RESET` 还负责在每个测量点开始时把 `window_index` 清零并重新锁存 `NSAMPLES`（[Windowing.vhd:108-115](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Windowing.vhd#L108-L115)）。改成全局复位后，每个测量点开始时窗索引不会归零，窗的相位与样本序列错位，且若本点 NSAMPLES 与上一点不同，`window_sample_compare` 也不会更新——加窗结果系统性错误。

**练习 3**：`NSAMPLES=1`（16 样本）档位里 `window_index_inc=8`，为什么 16 个样本正好走完 128 项窗？

**答案**：每样本 index +8，16 样本共 16×8 = 128，恰好覆盖整个 128 项 ROM（7 位 index 自然回绕）。这也是各档位的统一不变量：\(16 \times NSAMPLES \times \frac{8}{NSAMPLES} = 128\)。

### 4.2 DFT 核

#### 4.2.1 概念说明

`DFT.vhd` 是频谱仪模式的「片上频谱引擎」。它对端口 1/2 两路（**不含参考通道**——频谱仪测绝对电平，不需要比值）已经加窗的样本流做 96 bin 的直接 DFT，bin 的起始频率与间隔由 MCU 经两个 SPI 寄存器编程。

数学核心是一个巧妙的**相位分解**。bin \(k\) 在样本 \(n\) 处的相位：

\[ \varphi_{n,k} = \frac{2\pi}{2^{32}}\Big( n \cdot B_1 \cdot 2^{16} + k \cdot n \cdot D \cdot 2^{8} \Big) \]

其中 \(B_1\) 是 `BIN1_PHASEINC`，\(D\) 是 `DIFFBIN_PHASEINC`。把它整理成「随 \(n\) 线性」的形式就能看出每个 bin 分析的归一化频率：

\[ f_k = \underbrace{\frac{B_1}{2^{16}}}_{\text{bin 0 频率}} + k \cdot \underbrace{\frac{D}{2^{24}}}_{\text{bin 间隔}} \;(\text{周期/样本}) \]

这正是协议文档 [FPGA_protocol.tex:514-535](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L514-L535) 给出的两条公式：

\[ f_{firstBin} = \frac{SR_{ADC} \cdot DFT\_FIRST\_BIN}{2^{16}}, \qquad \Delta f = \frac{SR_{ADC} \cdot DFT\_FREQ\_SPACING}{2^{24}} \]

硬件实现的省钱之处在于：固定样本 \(n\) 扫 bin 时，\(\varphi_{n,k}\) 对 \(k\) 是等差数列——所以**每样本只需算两次乘法**（\(n \times B_1\) 和 \(n \times D\)，用来生成相位初值和相位步进），之后每前进一个 bin 只需**一次加法**（`phase += phase_inc`）。乘法用在刀刃上：每样本每 bin 的 4 次「样本 × sin/cos」乘累加。

**为什么要在片上做，而不是把原始样本传回 PC？** 算一笔账（默认 \(f_{ADC}\) = 800 kHz，见 [Hardware.hpp:37](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.hpp#L37)）：

- 要 1 kHz 的 Kaiser 窗分辨率，每点需要 1792 个样本（下节推导），三通道 16 bit 即约 10.7 KB/点。一次 501 点扫描就是 5 MB 上行——USB 批量传输和协议打包根本撑不起扫描节奏。
- 做到硬件极限 RBW ≈ 14 Hz（[Hardware.hpp:86](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.hpp#L86)，MaxSamples = 130944）时，每点每通道约 785 KB 原始样本——**只有片上预处理这条路存在**。
- 片上处理后，一个 DFT 块的 96 个 bin 对应 96 个扫频点，每点上行仅 24 字节，压缩比约 450:1。协议文档也点明这个模块的定位：它用于加速频谱仪测量、与其他计算并行运行（[FPGA_protocol.tex:512](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L512)）。

#### 4.2.2 核心流程

五个状态（[DFT.vhd:110](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/DFT.vhd#L110)），每个加窗样本循环一遍：

```
WaitingForSample  等 NEW_SAMPLE（= WINDOWING_DONE）
                  用两个乘法器算 n×B1 和 n×D          ┐ 相位种子，只在每样本
WaitMult          等若干拍乘法完成                     │ 开始时算一次
                  phase ← (n×B1 mod 2^16) << 16        │
                  phase_inc ← (n×D mod 2^24) << 8      ┘
WaitSinCos        等正弦表查表稳定，
                  锁存 port1/port2 样本，
                  按 sample_cnt 是否为 0 选择「首样本覆盖/后续累加」
BUSY              96 个 bin 的主循环，每时钟一个 bin：
                    phase += phase_inc                ← 递推：每 bin 一次加法
                    4 个 DSP 同时做 port1×sin, port1×cos, port2×sin, port2×cos
                    c 端接 BRAM 读出的旧累加值 → p = a×b + c
                    写地址滞后 3（流水线对齐），读地址超前 2
                  bin 计数到 BINS+2 后：
                    sample_cnt < samples_to_take ? 回 WaitingForSample : 进 Ready
Ready             RESULT_READY=1，等 MCU 发 NEXT_OUTPUT 逐 bin 取数；
                  取完（read_address 到 BINS-1）自动回 WaitingForSample 开始下一组
```

数据面：96 个 bin × 4 个 48 位累加器打包成 96 个 192 位字存在 `result_bram`。读改写环路在 [DFT.vhd:207-212](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/DFT.vhd#L207-L212)：四个 DSP 的 `c` 输入分别接 BRAM 输出字的四段，四个乘加结果重新拼接写回——**BRAM 当累加器用，DSP 只出乘加**。

时序上最紧的一点：全速 800 kHz 采样时每个样本只有 128 个主时钟（102.4 MHz / 800 kHz），而 DFT 单样本一遍约需百余拍（BUSY 里每时钟一个 bin，96 bin 加流水线余量）——**刚好塞进预算**。这是「每 bin 一个时钟」设计的直接动机；ADC 预分频加大（降采样率）时余量更宽。
