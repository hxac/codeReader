# 采样链路：Sampling.vhd 与 ADC 接口

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐状态解释 [MCP33131.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/MCP33131.vhd) 如何驱动外部 16 位 SAR ADC 完成一次「启动转换 → 等待 → 串行移出 16 位」的完整事务，并算出每个阶段消耗的时钟周期数。
2. 解释 [Sampling.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sampling.vhd) 如何产生 ADC 触发脉冲、如何把三路接收通道 × I/Q 共 6 路乘累加分时复用到同一个 DSP 乘法器上，最终汇聚成 6 个 48 位结果。
3. 读懂仓库自带的两个采样测试台（Test_Sampling.vhd、Test_MCP33131.vhd），并能动手修改参数、预测波形变化。
4. 把「16 bit @ 800 kHz 中频采样」放回整机数据链路：GUI 里的 IF 带宽设置如何一路变成 FPGA 里的样本数，ADC 采样率的上限为什么是 914.3 kHz。

本讲承接 u6-l1（top.vhd 顶层与数据流水线）与 u6-l2（Sweep 扫描引擎），继续沿「MCU 预编程 + FPGA 自主扫描」框架深入数据通路的第一站：模拟信号如何变成数字样本，样本如何变成 I/Q。

## 2. 前置知识

### 2.1 SAR ADC 与它的串行接口

MCP33131 是 Microchip 的 16 位 SAR（逐次逼近型）模数转换器。它不是像内存那样随时可读，而是按「事务」工作：

1. 主机（这里是 FPGA）拉高 **CONVSTART**，ADC 开始对输入电压采样并转换；
2. 转换需要时间（本设计给它 77 个 FPGA 时钟）；
3. 转换完成后，主机用 **SCLK** 时钟逐位读出结果，数据在 **SDO** 线上一位一位地移出（MSB 在前）；
4. 16 位读完后，本次事务结束，等下一次 CONVSTART。

也就是说，FPGA 必须自己当「SPI 主机」，严格按芯片的时序要求产生脉冲——这正是 MCP33131.vhd 存在的意义。具体的时序参数上限请对照 MCP33131 数据手册；本讲解读的是 RTL 代码实际做了什么。

### 2.2 采样率、奈奎斯特与中频采样

回顾 u1-l1 的射频链路：信号经过两级下变频，最终停在 250 kHz 的第二中频（IF2）上。三颗 ADC 分别采样端口 1、端口 2 和参考通道的 IF2 信号。要无失真地数字化一个 250 kHz 的正弦，采样率必须大于两倍最高频率：\( f_s > 2 \times 250\,\text{kHz} = 500\,\text{kHz} \)。LibreVNA 默认取 \( f_s = 800\,\text{kHz} \)，留出 300 kHz 余量。**这是整机唯一的模数转换环节**——RF 到 IF 的搬移全部在模拟域完成，ADC 只面对 250 kHz 的低频中频。

### 2.3 正交解调与 I/Q

对实数采样序列 \( x[n] \) 做「单 bin DFT」，就是把样本分别与同频的余弦、正弦相乘后累加：

\[
I = \sum_{n=0}^{N-1} x[n]\cos(2\pi f_{IF2} n/f_s),\qquad
Q = \sum_{n=0}^{N-1} x[n]\sin(2\pi f_{IF2} n/f_s)
\]

得到的复数 \( I + jQ \) 就是中频信号复包络的一个采样点（幅度与相位）。采样链路的终点，就是把 3 个通道各变成一对 \( I/Q \)。与 ADC 同频的余弦/正弦由 FPGA 内的 **NCO**（数控振荡器，这里是一个 12 位相位累加器 + SinCos 查找表）产生：相位每样本前进 \( \Delta\varphi = 4096 \cdot f_{IF2}/f_s \) 个「刻度」（一圈 4096 刻度对应 \( 2\pi \)）。

### 2.4 testbench 最小概念

testbench（测试台）是一个「不综合、只仿真」的顶层 VHDL 文件：它例化被测模块，自己产生时钟与激励，用 `wait for` / `wait until` 控制时间推进，人通过波形窗口观察输出。u6-l6 会系统总结，本讲只需要用到「时钟进程 + 激励进程」这两件套。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
| --- | --- | --- |
| [FPGA/VNA/MCP33131.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/MCP33131.vhd) | 单颗 ADC 的接口状态机 | 精读：五状态事务 + 移位接收 + MIN/MAX |
| [FPGA/VNA/Sampling.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sampling.vhd) | 采样调度器 + 数字正交解调 | 精读：预分频触发 + 6 路乘累加流水 |
| [FPGA/VNA/Test_Sampling.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_Sampling.vhd) | Sampling 的测试台 | 精读 + 动手改参数 |
| [FPGA/VNA/Test_MCP33131.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_MCP33131.vhd) | MCP33131 的测试台 | 对照激励理解时序 |
| [FPGA/VNA/top.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd) | 顶层互连 | 节选：三颗 ADC 与 Windowing/Sampling 的接线 |
| [Documentation/DeveloperInfo/FPGA_protocol.tex](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex) | MCU↔FPGA 寄存器协议 | 查证：Prescaler/PhaseInc/SPP 寄存器语义 |
| [Software/VNA_embedded/Application/Hardware.hpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.hpp) | 固件侧参数定义 | 查证：800 kHz / 250 kHz / 样本数上下限从哪来 |

## 4. 核心概念与源码讲解

### 4.1 ADC 接口时序（MCP33131.vhd）

#### 4.1.1 概念说明

MCP33131.vhd 解决的问题是：**用 FPGA 的 102.4 MHz 主时钟（`clk_pll`），按芯片要求的节奏，把一次模数转换完整地「演」出来**。它对外提供非常干净的握手：`START` 进来一个脉冲，`READY` 回一个脉冲，`DATA` 上就是 16 位结果。上层完全不需要关心 CONVSTART/SCLK/SDO 的细节。

两个 generic 决定了时序的「快慢」：

- `CLK_DIV`：SCLK 相对主时钟的分频参数（top.vhd 中取 2）；
- `CONVCYCLES`：给 ADC 的转换时间，单位是主时钟周期（top.vhd 中取 77）。

此外它还免费附赠一个功能：持续跟踪本通道采样值的运行最小值/最大值（`MIN`/`MAX` 输出），供 MCU 读取 ADC 输入电平。

#### 4.1.2 核心流程

模块内部是一个五状态状态机：

```text
Idle ──START=1──► Conversion ──计满77──► WAIT_tEN ──► Transmission ──16位移完──► Done ──► Idle
                   CONVSTART=1            (1拍)        SCLK突发,SDO移位           ready_int=1
                                                                           (再延迟3拍 → READY脉冲)
```

各阶段耗时（CLK = 102.4 MHz，即一个周期 9.765625 ns）：

| 阶段 | 周期数 | 说明 |
| --- | --- | --- |
| Conversion | 77（≈752 ns） | CONVSTART 保持高电平，等待 SAR 转换 |
| WAIT_tEN | 1 | 过渡态，名字来自数据手册的 tEN 类参数 |
| Transmission | 32 | 16 位 × 每位 2 个时钟（CLK_DIV=2 时 SCLK 每个主时钟翻转一次） |
| Done + READY 延迟 | 1 + 3 | 结果锁存、READY 脉冲 |
| **合计** | **≈111（≈1.08 µs）** | **一次完整事务** |

由此可以算出两个关键数字：

- SCLK 频率 = 102.4 MHz ÷ 2 = **51.2 MHz**（CLK_DIV=2 时每主时钟翻转一次，SCLK 周期占 2 个主时钟）；
- 事务时长 ≈ 111 CLK → 理论最高采样率 ≈ 922 kHz。这与协议文档规定的「预分频最小值 112（对应 914.3 kHz）」落在同一点上——**ADC 接口事务长度就是整机采样率的天花板**。

#### 4.1.3 源码精读

**实体与握手接口**。[FPGA/VNA/MCP33131.vhd:32-46](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/MCP33131.vhd#L32-L46) 声明了两个 generic（CLK_DIV、CONVCYCLES）和全部端口：`START`/`READY` 是对上层的握手，`CONVSTART`/`SCLK`/`SDO` 是对芯片的物理连线，`DATA`/`MIN`/`MAX` 是数据输出。注意 `SCLK` 是 `inout`——它同时在模块内部被 `sclk_phase` 驱动（见 [FPGA/VNA/MCP33131.vhd:64](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/MCP33131.vhd#L64)），这样测试台可以直接把它当信号观测。

**移位接收：以 SCLK 的下降沿为时钟**。[FPGA/VNA/MCP33131.vhd:66-71](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/MCP33131.vhd#L66-L71)

```vhdl
process(SCLK, START)
begin
    if(falling_edge(SCLK)) then
        adc_data <= adc_data(14 downto 0) & SDO;
    end if;
end process;
```

这是一个独立于主时钟的小时钟域：每遇到 SCLK 下降沿，就把 SDO 上的一位从低位压进 16 位移位寄存器。先收到的是 MSB（16 次左移后正好落在 bit15）。用 SCLK 本身而非 CLK 来采样 SDO，天然满足芯片对 SDO 建立/保持时间的要求。细节：敏感表里的 `START` 在进程体内并没有被使用，属于无害的历史残留。

**状态机主体**。[FPGA/VNA/MCP33131.vhd:114-155](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/MCP33131.vhd#L114-L155)。三个关键片段：

- [Idle→Conversion（L115-L121）](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/MCP33131.vhd#L115-L121)：捕获 `START`，拉高 `CONVSTART`，开始计转换周期；
- [Conversion（L122-L130）](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/MCP33131.vhd#L122-L130)：计满 `CONVCYCLES-1` 后撤下 `CONVSTART`，进入 WAIT_tEN；
- [Transmission（L134-L151）](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/MCP33131.vhd#L134-L151)：用 `div_cnt` 做 SCLK 分频——CLK_DIV=2 时 `(CLK_DIV/2)-1 = 0`，判断条件永假，于是每个主时钟都翻转一次 `sclk_phase`（这就是 SCLK = CLK/2 的由来）；每次 1→0 的翻转（下降沿）让 `bit_cnt` 加一，计满 16 进入 Done。

**READY 的三拍延迟**。[FPGA/VNA/MCP33131.vhd:101-112](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/MCP33131.vhd#L101-L112)：Done 态置位 `ready_int`，随后 `ready_delay` 从 3 递减到 1 时才输出单拍 `READY`，并把 `adc_data` 锁进 `data_int`。这样 `DATA` 输出与 `READY` 脉冲对齐，上层「看到 READY 再取数」即可。

**MIN/MAX 跟踪**。[FPGA/VNA/MCP33131.vhd:89-99](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/MCP33131.vhd#L89-L99)：每拍把 `data_int` 与当前最值比较更新；`RESET_MINMAX` 一来就恢复成 +32767/−32768。三颗 ADC 的六 个 16 位最值在顶层拼成 96 位的 `adc_minmax`（见 4.2.3），MCU 经 SPI 读回后写进设备状态上报（固件侧 [Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp:418-432](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L418-L432) 的 `GetADCLimits()`，使用处如 [Software/VNA_embedded/Application/Hardware.cpp:349-352](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L349-L352)），用来监视各接收通道的输入电平是否接近满量程。

#### 4.1.4 代码实践：读出「每个采样周期的控制脉冲顺序」

1. **实践目标**：不看波形也能口头复述一次采样事务中 FPGA 各输出引脚的动作顺序与宽度。
2. **操作步骤**：
   - 打开 [FPGA/VNA/Test_MCP33131.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_MCP33131.vhd)。先看 [L75](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_MCP33131.vhd#L75) 的时钟周期 `CLK_period : time := 9.765 ns`——对应 102.4 MHz，与真实主时钟一致；
   - 再看 [L114-L119](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_MCP33131.vhd#L114-L119) 的激励：每 `CLK_period*111` 拉高 START 一拍。111 正好是 4.1.2 算出的一次事务长度——测试台在用背靠背的 START 把 ADC 逼到极限速率（≈922 kHz）；
   - 若有仿真环境（该测试台是纯 RTL、无 Xilinx IP 依赖，ISE 内置仿真器或 ghdl 等通用仿真器均可），把 Test_MCP33131 设为仿真顶层运行若干微秒，观察 CONVSTART/SCLK/SDO/READY 波形；没有环境就纯走读代码完成下面的表格。
3. **需要观察的现象**（按时间顺序）：

   | 顺序 | 信号 | 动作 | 宽度 |
   | --- | --- | --- | --- |
   | 1 | START | 测试台输入，1 拍脉冲 | 1 CLK |
   | 2 | CONVSTART | 拉高 | 77 CLK ≈ 752 ns |
   | 3 | CONVSTART | 拉低 | — |
   | 4 | （等待） | WAIT_tEN | 1 CLK |
   | 5 | SCLK | 连续 16 个周期，每位 2 CLK | 32 CLK，频率 51.2 MHz |
   | 6 | SDO | 每个 SCLK 下降沿更新一位，MSB 在前 | 与 SCLK 对齐 |
   | 7 | READY | 单拍脉冲 | 1 CLK（结束后约 3 CLK） |

4. **预期结果**：相邻两次 CONVSTART 上升沿间距 111 CLK ≈ 1.084 µs；SCLK 只在 Transmission 阶段出现，其余时间静止为 0。若你能对照 MCP33131 数据手册核对「CONVSTART 最小脉宽、SCLK 最高频率、tEN」三项指标均被满足，就把手册结论补充到表旁（手册数值请以你查到的版本为准；本讲只保证 RTL 行为与上述计算一致）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 top.vhd 里的 `CONVCYCLES` 从 77 改成 100，系统的最高采样率会变成多少？
**答案**：事务长度变为 100+1+32+1 = 134 CLK ≈ 1.309 µs，背靠背速率约 102.4 MHz/134 ≈ 764 kHz。同时协议文档规定的「最小预分频」也必须相应加大（≥134），否则下一次 START 到来时上一次事务还没做完，样本会被跳过——这正是文档警告的行为。

**练习 2**：为什么 SCLK 的接收进程（L66-71）用 `falling_edge(SCLK)` 而不是在 CLK 进程里判断 `sclk_phase` 的下降沿？
**答案**：SDO 是 ADC 输出的异步于 FPGA 主时钟的外部信号，它相对 SCLK 边沿的时序关系由芯片保证。直接用 SCLK 当采样时钟，移位动作与数据源同步；若改在 CLK 域判断，还要额外处理 SDO 相对 CLK 的建立/保持裕量，反而不可靠。

**练习 3**：`DATA` 端口输出的为什么是 `data_int`（L63），而不是移位寄存器 `adc_data`？
**答案**：`data_int` 在 READY 脉冲出现时锁存 `adc_data`（L110），保证 DATA 与 READY 对齐——上层看到 READY 时数据已经稳定。同时 MIN/MAX 比较也用 `data_int`，保证每个样本只参与一次比较。

### 4.2 样本缓冲与汇聚（Sampling.vhd）

#### 4.2.1 概念说明

Sampling.vhd 是采样链路的「总调度」。它要同时干三件事：

1. **定时触发**：按 `ADC_PRESCALER` 设定的节拍发出 `ADC_START` 脉冲，驱动三颗 ADC 同步采样；
2. **汇聚解调**：每收到一个新样本（`NEW_SAMPLE`），把三路 18 位加窗样本分别乘以 NCO 的余弦/正弦并累加——共 3 通道 × 2 (I/Q) = **6 路乘累加**，但只例化了 **1 个 DSP 乘法器**，靠状态机分时复用；
3. **计数收尾**：收满 `SAMPLES × 16` 个样本后，把 6 个 48 位累加结果搬到输出端口，`ACTIVE` 拉低宣告本点测量结束。

为什么输出是 48 位？以标称满幅估算：16 位样本 × 16 位正余弦，每个乘积约 \( 2^{30} \)，累加 \( N \le 130944 \approx 2^{17} \) 个，总和约 \( 2^{47} \)——48 位有符号数恰好装得下。可以把 `MaxSamples = 130944`（略小于 \( 2^{17} \)）与 48 位输出理解为同一份设计余量的两端。

#### 4.2.2 核心流程

整个模块是一个 13 状态的流水线（[FPGA/VNA/Sampling.vhd:107](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sampling.vhd#L107)）：

```text
START=1
   │  samples_to_take ← SAMPLES×16, phase←0
   ▼
┌──────────────────── 每 1 个样本循环一次 ────────────────────┐
│ Sampling: 等NEW_SAMPLE → 装载 P1×cos                          │
│ P1Q:      装载 P1×sin        ┐ 6个状态依次「装载」6次乘法    │
│ P2I/P2Q:  装载 P2×cos/sin     │                              │
│ RI/RQ:    装载 R×cos/sin     ┘ 第6拍起 mult_p 开始出结果     │
│ SaveP1Q..SaveRQ: 依次回收6个结果到累加器                      │
│   └─ sel←1(改为累加), phase += PHASEINC                       │
│      sample_cnt < samples_to_take ? ──是──► 回到 Sampling     │
│                                    └─否──► Ready             │
└──────────────────────────────────────────────────────────────┘
Ready: 6路48位结果搬到输出, DONE/PRE_DONE置位, ACTIVE←0, 回Idle
```

三个数字化的关键机制：

**（a）采样节拍发生器**。`ADC_START` 每 `ADC_PRESCALER` 个主时钟产生一个 1 拍脉冲（见 4.2.3），所以采样率是：

\[
f_s = \frac{102.4\,\text{MHz}}{\text{Presc}},\qquad \text{默认 Presc}=128 \Rightarrow f_s = 800\,\text{kHz}
\]

**（b）NCO 相位步进**。12 位相位累加器一圈 4096 格，每样本前进：

\[
\Delta\varphi = \frac{4096 \cdot f_{IF2}}{f_s} = \frac{4096 \times 250000}{800000} = 1280
\]

注意一个漂亮的巧合：\( \Delta\varphi = 4096 f_{IF2} \cdot \text{Presc} / 102.4\,\text{MHz} \)，当 \( f_{IF2} = 250\,\text{kHz} \) 时恒等于 \( 10 \times \text{Presc} \)——协议文档直接把这条捷径写了出来。

**（c）样本数与 IF 带宽的换算**。寄存器值 `SAMPLES` 以 16 个样本为单位（代码里 `SAMPLES & "0000"` 即乘 16），每点实际采 \( N = 16 \times \text{SAMPLES} \) 个样本。单 bin 积分的等效带宽约为 \( f_s/N \)，所以：

\[
\text{IFBW}_{max} = \frac{800\,\text{kHz}}{16} = 50\,\text{kHz},\qquad
\text{IFBW}_{min} = \frac{800\,\text{kHz}}{130944} \approx 6.1\,\text{Hz}
\]

这两个值正是固件里 `limits_minIFBW`/`limits_maxIFBW` 的算法（[Software/VNA_embedded/Application/Hardware.hpp:81-82](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.hpp#L81-L82)）。GUI 里选的 IF 带宽，最终就是这里的 N。

#### 4.2.3 源码精读

**在顶层的位置**。[FPGA/VNA/top.vhd:662-685](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L662-L685) 例化 Sampler：输入三路 **加窗后** 的 18 位数据（`port1/2_windowed`、`ref_windowed`），输出 `ADC_START` 回馈到三颗 ADC 的公共触发线，六路 48 位结果拼进 304 位的 `sampling_result`，`ACTIVE` 作为 `sampling_busy` 交给 Sweep 模块。注意两点：`DONE`/`PRE_DONE` 都接了 `open`（[L674-L675](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L674-L675)，未使用）；generic `CLK_CYCLES_PRE_DONE` 在 Sampling 的结构体里根本没被引用——这两个都是历史残留，完成检测实际走的是 `ACTIVE` 信号的下降沿（见本节末尾）。

**三颗 ADC 的同步**。[FPGA/VNA/top.vhd:597-644](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L597-L644)：三个 MCP33131 实例（Port1ADC/Port2ADC/RefADC）共用同一条 `adc_trigger_sample` 触发线、同一组 generic（CLK_DIV=2、CONVCYCLES=77），保证三路**同时**采样——相位测量的一致性全靠这一点。只有 Port1 的 READY 被使用，另两个接 `open`，注释写明 "synchronous ADCs, ready indicated by port 1 ADC"（[L620](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L620)、[L636](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L636)）。三路的 MIN/MAX 拼成 96 位 `adc_minmax`（[L606-L607](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L606-L607) 等三处），由 SPIConfig 用读事务 `"111"` 读出（[FPGA/VNA/SPIConfig.vhd:233-235](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L233-L235)）。

**采样节拍发生器**。[FPGA/VNA/Sampling.vhd:152-165](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sampling.vhd#L152-L165)

```vhdl
if state /= Idle then
    if clk_cnt = unsigned(ADC_PRESCALER) - 1 then
        if sample_cnt < samples_to_take then
            ADC_START <= '1';
        end if;
        clk_cnt <= 0;
    else
        clk_cnt <= clk_cnt + 1;
        ADC_START <= '0';
    end if;
else
    ADC_START <= '0';
end if;
```

这段在状态机之外并行运行：非 Idle 态时，`clk_cnt` 数到 `Presc-1` 就发一个 1 拍 `ADC_START` 并清零。内层的 `sample_cnt < samples_to_take` 保证**只发需要数量的脉冲**——收满后即使节拍还在走也不会再触发。

**接收一个样本并启动 6 次乘法**。[FPGA/VNA/Sampling.vhd:181-193](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sampling.vhd#L181-L193)：Sampling 态等到 `NEW_SAMPLE=1`，`sample_cnt` 加一，随即给 DSP 装载第一组操作数（PORT1×cosine，累加初值取 `p1_I`），进入 P1Q。此后 [P1Q→RQ 共 6 个状态（L194-L240）](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sampling.vhd#L194-L240) 每拍装载一组：P1×sin、P2×cos、P2×sin、R×cos、R×sin。

**结果的流水线回收**。DSP 有节拍延迟，第一个乘积在 RQ 态才出现在 `mult_p` 上——[L238-L240](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sampling.vhd#L238-L240) 处注释 "first result is available" 并把 `mult_p` 存入 `p1_I`；随后 [SaveP1Q..SaveRQ 六个状态（L241-L274）](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sampling.vhd#L241-L274) 依次回收其余 5 个结果。这是典型的软件流水：装载与回收错开 6 拍，单个乘法器跑出 6 路并行的效果。

**累加开关与相位推进**。[FPGA/VNA/Sampling.vhd:269-282](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sampling.vhd#L269-L282)

```vhdl
when SaveRQ =>
    ...
    r_Q <= mult_p;
    -- from now on accumulate results
    mult_accumulate <= "1";
    phase <= std_logic_vector(unsigned(phase) + unsigned(PHASEINC));
    if sample_cnt < samples_to_take then
        state <= Sampling;
    else
        state <= Ready;
    end if;
```

第一个样本期间 `mult_accumulate="0"`（DSP 直通 \( p = a \cdot b \)，初始化 6 个累加器），从第二个样本起置 "1"（\( p = a \cdot b + c \)，`c` 装载的就是当前累加值）。同时 NCO 相位步进一次，为下一个样本准备好正余弦。之后判断样本是否收满，决定回 Sampling 还是进 Ready。

**符号位扩展**。[FPGA/VNA/Sampling.vhd:134-135](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sampling.vhd#L134-L135) 把 16 位的正余弦符号扩展成 18 位送入 DSP 的 b 端口——`mult_b(17 downto) <= mult_b(15) & mult_b(15)` 这种「同信号不同位段由两个源驱动」的写法在 VHDL 中合法且常用。

**收尾与输出搬运**。[FPGA/VNA/Sampling.vhd:283-294](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sampling.vhd#L283-L294)：Ready 态把 6 个累加器搬到输出端口、`DONE`/`PRE_DONE` 同时置位、`ACTIVE` 保持为 1，随后回 Idle（Idle 态把 `ACTIVE` 清零，见 [L172](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sampling.vhd#L172)）。

**完成事件如何通知 MCU**。由于 DONE 接了 `open`，真正的完成检测是 Sweep 模块盯住 `SAMPLING_BUSY`：[FPGA/VNA/Sweep.vhd:229-235](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L229-L235) 在 Exciting 态等待 `SAMPLING_BUSY='0'`，随后在 SamplingDone 态 [L245-L246](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L245-L246) 脉冲 `NEW_DATA`，经顶层信号 `sampling_done`（[FPGA/VNA/top.vhd:723](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L723)）送进 SPIConfig 的 `NEW_SAMPLING_DATA`（[top.vhd:775](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L775)），锁存结果并对 MCU 拉中断——这就接回了 u5-l4 讲过的「FPGA 中断取数」。

**两处代码考古**。① [L76-L83](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sampling.vhd#L76-L83) 声明了 `window` 组件却从未例化——加窗功能如今在 ADC 与 Sampling 之间独立的 Windowing 模块里完成（u6-l4 的主题），声明是搬家前的遗迹；② [L110-L113](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sampling.vhd#L110-L113) 的断言（phaseinc 必须精确对应 IF 频率）在仿真中恒失败而被注释掉——固件用 `static_assert` 在编译期接管了同样的检查（[Software/VNA_embedded/Application/Hardware.hpp:51-54](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.hpp#L51-L54)，如 `DefaultDFTphaseInc * DefaultADCSamplerate == 4096 * DefaultIF2`）。

#### 4.2.4 代码实践：把参数闭环走一遍（源码阅读型）

1. **实践目标**：验证「GUI 的 IF 带宽 → FPGA 样本数」这条换算链在代码里的每一步。
2. **操作步骤**：
   - 假设用户在 GUI 选 IFBW = 1 kHz。固件需要的样本数 \( N = f_s/\text{IFBW} = 800000/1000 = 800 \)，换成 16 样本单位得 `SAMPLES` 寄存器值 = 50；
   - 打开协议文档 [Documentation/DeveloperInfo/FPGA_protocol.tex:322-333](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L322-L333)，确认 Samples Per Point 寄存器（0x02）确实「以 16 样本为增量」；
   - 再对照 [FPGA/VNA/Sampling.vhd:179](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sampling.vhd#L179) 的 `samples_to_take <= to_integer(unsigned(SAMPLES & "0000"))`，确认 FPGA 侧把寄存器值乘回 16；
   - 最后算测量时长：800 样本 × 1.25 µs/样本 = 1 ms（每点、每 stage）。
3. **需要观察的现象**：四个环节（固件计算 / 协议文档 / FPGA 代码 / 手算）给出的 N 完全一致。
4. **预期结果**：换算链闭合，无「待确认」项。再用同样方法算 IFBW = 10 Hz 时的 N（答案：80000，对应 `SAMPLES`=5000，仍小于 MaxSamples=130944，合法）。

#### 4.2.5 小练习与答案

**练习 1**：频谱仪模式固件写入 `ADCPrescaler=112`、`PhaseIncrement=1120`（[Software/VNA_embedded/Application/SpectrumAnalyzer.cpp:74-75](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L74-L75)）。此时的采样率和 IF2 是多少？
**答案**：\( f_s = 102.4\,\text{MHz}/112 \approx 914.3\,\text{kHz} \)（协议允许的最高档）；\( f_{IF2} = 1120 \times f_s/4096 = 10 \times 112 \times f_s /4096 \)。代入得 \( f_{IF2} = 1120 \times 914285.7/4096 = 250\,\text{kHz} \)——正好还是 250 kHz，验证了 \( \text{PhaseInc} = 10 \times \text{Presc} \) 的关系。

**练习 2**：为什么 6 路乘累加只用了 1 个 DSP_SLICE，而不用 6 个？
**答案**：每样本的乘累加窗口是 6 个主时钟（状态机 6 个状态），而样本间隔是 128 个主时钟（800 kHz 时）——DSP 的占用率不到 5%。分时复用节省了 5 个 DSP 单元；代价是状态机复杂度。这是「时间换面积」的典型权衡，前提是采样率远低于时钟频率。

**练习 3**：如果把 `ADC_PRESCALER` 设成 100，会发生什么？
**答案**：采样节拍周期 100 CLK 短于 MCP33131 一次事务的 ≈111 CLK。`ADC_START` 到来时接口状态机还没回到 Idle，START 脉冲被丢掉，该样本缺失——协议文档对 Presc<112 的警告正是这个现象。数据不会错位（状态机仍按收到的样本数工作），但有效采样率下降、NCO 相位与实际时间轴脱节，解调结果会出错。

### 4.3 采样测试台（Test_Sampling.vhd 与 Test_MCP33131.vhd）

#### 4.3.1 概念说明

仓库为采样链路提供了两个 testbench。它们的价值不只在「跑通」，更在于**测试台本身就是一份可执行的时序规格**：Test_MCP33131.vhd 用 111 周期的 START 间隔「陈述」了 ADC 事务长度；Test_Sampling.vhd 用「ADC_START 后延迟 110 个时钟再给 NEW_SAMPLE」的激励「陈述」了 ADC（加窗）的往返延迟。

同时它们也是很好的代码考古现场：Test_Sampling.vhd 里存在一处会让编译直接报错的字面量位宽 bug，以及一个与真实主时钟不一致的时钟周期。发现并解释这两处「为什么现在还能躺在仓库里」，正是源码阅读能力的体现（testbench 不参与综合，不影响交付的 bitstream，所以最容易被岁月遗忘）。

#### 4.3.2 核心流程

两个测试台结构相同，都是三段式：

```text
① 实体为空（无端口）── testbench 是仿真顶层，不需要引脚
② 例化被测单元（UUT）＋ 声明与 UUT 端口一一对应的内部信号
③ 两个并发进程：
   时钟进程： forever { CLK=0; wait T/2; CLK=1; wait T/2; }
   激励进程： 上电复位 → 设置参数 → 发 START → 循环产生激励 → wait;（挂起）
```

Test_Sampling 的激励循环（[FPGA/VNA/Test_Sampling.vhd:149-156](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_Sampling.vhd#L149-L156)）用 `while True loop` 无限循环，替代了真实环境里 MCP33131+Windowing 的行为：每次等到 `ADC_START='1'` 就撤掉 START，等 110 个时钟，再给一拍 `NEW_SAMPLE`。测试者观察若干个采样周期后手动停止仿真（循环后的 `wait;` 永远到不了）。

#### 4.3.3 源码精读

**时钟周期**。[FPGA/VNA/Test_Sampling.vhd:92](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_Sampling.vhd#L92)：`CLK_period : time := 6.25 ns` → 160 MHz。而 Test_MCP33131 用 [9.765 ns](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_MCP33131.vhd#L75)（≈102.4 MHz，与真实 `clk_pll` 一致）。两个测试台的时钟对不上——推断 Test_Sampling 写作时主时钟还是 160 MHz，后来顶层改用 102.4 MHz 时没有同步更新它。对本测试台验证「相对时序」（脉冲个数、状态推进）无碍，但换算绝对时间时必须用它自己的 6.25 ns。

**参数与激励**。[FPGA/VNA/Test_Sampling.vhd:142-148](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_Sampling.vhd#L142-L148)

```vhdl
ADC_PRESCALER <= "011110000";      -- ⚠ 9 位字面量，端口却是 8 位
PHASEINC <= "010001100000";        -- = 1120 = 10×112，与 Presc=112 自洽
PORT1 <= "000001111111111111";     -- 0x0FFFF = 65535（直流电平）
PORT2 <= "000011111111111111";     -- 0x1FFFF = 131071
REF   <= "000111111111111111";     -- 0x3FFFF = 262143（接近正满幅）
SAMPLES <= "0000000000001";        -- 1 → 实采 16 个样本
START <= '1';
```

第一行是本讲的「彩蛋」：字面量 `"011110000"` 有 9 个字符，而端口声明是 `std_logic_vector(7 downto 0)`（[L47](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_Sampling.vhd#L47)）。VHDL 字符串字面量的长度必须与目标信号位数一致，这个文件按原样送入仿真器会在 elaborate 阶段报长度不匹配错误。旁证它本意是 `"01110000"`（=112）：因为下一行 `PHASEINC = 1120 = 10 × 112`，恰好满足文档规定的 250 kHz 关系式。

三路输入是三个不同高度的**直流**电平——用直流可以最容易地预测输出（见 4.3.5 练习 2）。`SAMPLES=1` 即一次测量 16 个样本，配合 `CLK_CYCLES_PRE_DONE => 0`（[L98](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_Sampling.vhd#L98)）与真实顶层取值相同。

**环境提示**：Test_Sampling 例化的 Sampling 内部引用了两个 Xilinx IP 核（SinCos 查找表与 DSP_SLICE，[FPGA/VNA/Sampling.vhd:115-132](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sampling.vhd#L115-L132)），完整仿真需要 ISE 工程及其仿真库（在 ISE 中把 Test_Sampling.vhd 设为仿真顶层即可）；而 Test_MCP33131 只含纯 RTL，任何标准 VHDL 仿真器都能直接跑。

#### 4.3.4 代码实践：修一个 bug、改一个参数、预测波形

1. **实践目标**：亲手让 Test_Sampling 跑起来，并通过「改参数前先写下预期」的方式验证你对采样链路的理解。
2. **操作步骤**：
   - **修复位宽 bug**：把 [L142](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_Sampling.vhd#L142) 改为 `ADC_PRESCALER <= "01110000";`（=112）。在 ISE 中把 Test_Sampling.vhd 加入工程的 Simulation Sources 并设为顶层，运行行为仿真；
   - **修改样本数**：把 `SAMPLES` 从 `"0000000000001"` 改为 `"0000000000010"`（=2，实采 32 样本），重新仿真；
   - （可选进阶）把 `PHASEINC` 改为全 0 再跑一次。
3. **需要观察的现象**：
   - 第一轮：`ADC_START` 恰好出现 **16 个** 1 拍脉冲，间隔 = 112 × 6.25 ns = 700 ns；每个脉冲后 110 个时钟出现一拍 `NEW_SAMPLE`；第 16 个样本处理完（SaveRQ 之后）状态进入 Ready，`ACTIVE` 拉低、`DONE`/`PRE_DONE` 置位，六路输出更新后模块回 Idle；
   - 第二轮：`ADC_START` 变为 **32 个**脉冲，`DONE` 相对 START 的延迟增加约 16 × 700 ns = 11.2 µs；由于输入是直流且多累加了一倍样本，六路输出幅值近似翻倍（严格值取决于 NCO 相位序列，见练习 2）；
   - 进阶层：`PHASEINC=0` 时 NCO 相位恒为 0 → cosine 恒为满幅、sine 恒为 0 → 三路 **_Q 输出为 0**，三路 **_I 输出 = 样本数 × 直流电平 × cos 满幅标度**，例如 PORT1_I ≈ 16 × 65535 × (cos 满幅/2¹⁵)。SinCos 核的满幅标度（32767 还是 32768）需要从仿真波形确认——**待本地验证**。
4. **预期结果**：脉冲计数、DONE 延迟、Q 路为零三点均应与预测一致；幅值关系在确认 SinCos 标度后闭合。若没有仿真环境，本实践退化为纯走读：把上面三条预测写成清单，对照 4.1/4.2 的状态机逐条给出「代码依据」（哪一行决定了这个行为），同样有效。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Test_Sampling 用 `wait until ADC_START = '1'` 而不是固定延时来对齐激励？
**答案**：`ADC_START` 的节拍由 `ADC_PRESCALER` 与状态机共同决定，属被测模块的输出。用 `wait until` 跟随它，测试台就模拟了「MCP33131 收到触发、延时 110 CLK 后数据就绪」的因果链，参数怎么改激励都自动对齐；用固定延时则每次改参数都要手工重算。

**练习 2**：输入为直流 \( A \)、相位步进 \( \Delta\varphi \) 时，16 样本后的 \( I \) 是多少？
**答案**：\( I = A\sum_{n=0}^{15}\cos(n\Delta\varphi) \)，\( Q = A\sum_{n=0}^{15}\sin(n\Delta\varphi) \)（首次样本 NCO 相位为 0）。当 \( \Delta\varphi = 1120 \times 2\pi/4096 \approx 0.2734 \times 2\pi \) 时这不是简单倍数关系——这就是 4.3.4 中「近似翻倍」要加注严格值的原因，也正是把 `PHASEINC` 设 0（\( \cos = 1, \sin = 0 \)）能把手算变平凡的原因。

**练习 3**：测试台的 110 CLK 延迟模拟的是谁的时间？
**答案**：MCP33131 一次事务（≈111 CLK，4.1.2）加上 Windowing 模块的处理延迟。真实顶层里 `NEW_SAMPLE` 来自 Windowing 的 `WINDOWING_DONE`（[FPGA/VNA/top.vhd:658](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L658)、[L673](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L673)），测试台没有例化 Windowing，就用这个延时近似「触发→数据就绪」的往返。

## 5. 综合实践

**任务：画出一「点」测量的完整时序与参数闭环图。**

以 VNA 模式、默认参数（Presc=128、PhaseInc=1280、IFBW=1 kHz → SAMPLES=50）为对象，完成三份产出：

1. **一张时序图**（手绘即可）：时间轴上画出 `START`（来自 Sweep 的 `START_SAMPLING`）、`ADC_START`（每 1.25 µs 一拍，共 800 拍）、`CONVSTART/SCLK`（放大窗口画一次事务内部）、`NEW_SAMPLE`、六个乘累加状态（Sampling→SaveRQ 循环 800 圈）、`ACTIVE` 拉低、Sweep 的 `NEW_DATA` 脉冲、SPIConfig 置中断。标注每个信号的产生代码位置（文件:行号）。
2. **一张参数换算表**：GUI IFBW → 固件样本数计算 → `SAMPLES` 寄存器值 → `samples_to_take` → 测量时长 → IFBW 上报值（`limits_min/maxIFBW`），每一行给出两侧的代码/文档依据，验证首尾闭合。
3. **一组极限检查**：分别取 IFBW = 50 kHz、6 Hz、60 kHz，判断哪个非法、为什么（对照 [Hardware.hpp:41-42](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.hpp#L41-L42) 的 MinSamples/MaxSamples 与 48 位累加器余量）。

有仿真条件的话，用 4.3.4 的修改版测试台为时序图提供波形佐证；没有则全程源码走读（本综合实践不依赖硬件与仿真器）。

## 6. 本讲小结

- MCP33131.vhd 用五状态机把一次 ADC 事务（77 CLK 转换 + 32 CLK 串行移出 + 收尾 ≈ 111 CLK ≈ 1.08 µs）封装成 START/READY 握手；SCLK = CLK/2 = 51.2 MHz，数据在 SCLK 下降沿按 MSB 在前移入。
- 事务长度决定了协议规定的采样率上限：Presc 最小 112 → 914.3 kHz；默认 Presc=128 → 800 kHz，对 250 kHz 的 IF2 满足奈奎斯特并留 300 kHz 余量。
- Sampling.vhd 身兼三职：按 Presc 发 ADC 触发脉冲、用单个 DSP 乘法器分时完成 3 通道 × I/Q 共 6 路乘累加（软件流水，装载与回收错开 6 拍）、收满 `SAMPLES×16` 个样本后输出 6 个 48 位 I/Q。
- NCO 相位步进 \( \Delta\varphi = 4096 f_{IF2}/f_s \)（默认 1280，且当 IF2=250 kHz 时恒等于 10×Presc）；每点样本数 N=16×SAMPLES，IFBW ≈ f_s/N，从 50 kHz 到约 6 Hz——GUI 的 IF 带宽设置最终就是这里的 N。
- 完成事件不走 DONE（顶层接 `open`），而是 Sweep 监视 `ACTIVE` 下降沿后发 `NEW_DATA` 给 SPIConfig 锁存结果并中断 MCU；`CLK_CYCLES_PRE_DONE`、未例化的 `window` 组件声明、Sweep 未用的 `SAMPLING_DONE` 输入都是历史残留。
- 测试台是可执行的时序规格，也是考古现场：Test_Sampling 的 160 MHz 时钟与 9 位 `ADC_PRESCALER` 字面量 bug 提醒我们——不参与综合的代码最容易与设计脱节。

## 7. 下一步学习建议

下一讲（u6-l4「片上信号处理：加窗与 DFT」）向左走一格，进入本讲输入的来源：Windowing.vhd 如何用 ROM 系数（Hann/Flattop/Kaiser）把 16 位原始样本变成本讲看到的 18 位加窗样本，DFT.vhd 又如何用类似的结构并行计算频谱仪需要的 96 个 bin。建议带着两个问题去读：① 加窗为什么放在采样与解调之间、系数如何预生成；② DFT.vhd 的多 bin 递推与本讲的单 bin NCO 在数学上是同一件事的两种展开。之后 u6-l5 会从 MCU 视角回到 SPIConfig，把本讲的 48 位结果如何被读走讲完整。
