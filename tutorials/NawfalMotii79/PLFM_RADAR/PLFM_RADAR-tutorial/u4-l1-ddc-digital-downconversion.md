# 数字下变频 DDC：ADC→NCO→CIC→FIR

> 本讲属于 **U4 FPGA 接收信号处理链** 的第一讲。它承接 [u3-l1 FPGA 顶层模块 radar_system_top 全景] 中建立的「接收机 `rx_inst` 内部串接 DDC→匹配滤波→MTI→Doppler」的整体认知，带你钻进接收链最前端、也是最「硬核」的一段：**数字下变频（Digital Down-Conversion, DDC）**。

---

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 **数字下变频** 要解决的问题：把 400 MHz 采样的实信号，变成 100 MHz 采样的复（I/Q）基带信号。
- 顺着源码画出一条完整的数据通路：`ADC 接口 → 符号化 → NCO 正交本振 → DSP48E1 混频 → CIC 抽取 → 跨时钟域 → FIR 低通 → baseband_i/q`，并标注每一段工作在哪个时钟域。
- 解释 **NCO 相位增量（FTW）** 的计算公式，验证仓库里 `0x4CCCCCCD` 这个「魔数」从何而来。
- 解释 **CIC 抽取滤波器** 的「积分→抽取→梳状」结构与直流增益，并算出为什么 5 级、4 倍抽取对应 `>>> 10` 的缩放。
- 说清楚一个关键工程教训：**为什么原始 ADC 数据不能用同频域 Gray 码 CDC 跨域**，而必须先做 CIC 抽取再跨域。

---

## 2. 前置知识

本讲会用到几个 DSP 与数字设计的基础概念，这里用最朴素的方式先过一遍。

### 2.1 实信号、中频（IF）与基带（baseband）

- ADC 采到的是 **实信号**（real signal），每个采样就是一个数。
- AERIS-10 的射频前端把 10.5 GHz 的雷达回波 **下变频到中频（IF）= 120 MHz** 再交给 ADC，所以 ADC 实际采的是「载在 120 MHz 上的基带信息」。
- 我们真正关心的 **基带（baseband）**，是把这个 120 MHz 的「载波」剥离掉之后、剩余的低频包络。基带信号最自然的表示是 **复信号** \(s(t)=I(t)+jQ(t)\)，即同相（I）与正交（Q）两路。

> 直觉：把 120 MHz 的载波「搬」到 0 Hz 的过程，就叫 **下变频**。用模拟电路（混频器）做一遍，用数字电路再做一遍——后者就是「**数字**下变频 DDC」。

### 2.2 正交混频：为什么是 I、Q 两路

要把实信号 \(x[n]\) 搬到基带，把它分别乘上本振的余弦和正弦：

\[
I[n] = x[n]\cdot \cos(2\pi f_{IF} n/f_s), \qquad Q[n] = x[n]\cdot \sin(2\pi f_{IF} n/f_s)
\]

这一对 I/Q 就是复基带信号的实部与虚部。**只用一路（只乘 cos）会丢失「正负频率」的区分能力**，后续 Doppler 处理就无法判断目标靠近还是远离——所以雷达必须做正交混频，拿到 I 和 Q。

### 2.3 采样率、抽取与时钟域

- 仓库里 ADC 是 **400 MSPS**（每秒 4 亿个样本），系统主处理时钟是 **100 MHz**。
- 把数据率从 400 MSPS 降到 100 MSPS 的过程叫 **抽取（decimation）**：既降速，也滤掉高频镜像。
- 400 MHz 和 100 MHz 是 **两个不同的时钟域**，数据要从前者搬到后者，就必须处理「跨时钟域（CDC）」问题。这正是 u3-l2 讲过的 CDC 基础在本讲的真实落地场景。

### 2.4 关键参数一览

| 名称 | 值 | 含义 |
|---|---|---|
| `IF_FREQ` | 120 MHz | 中频，NCO 要搬移的目标频率 |
| `FS` | 400 MHz | ADC 采样率 |
| `ADC_WIDTH` | 8 bit | ADC 原始位宽 |
| `NCO_WIDTH` | 16 bit | NCO 输出 sin/cos 位宽 |
| `MIXER_WIDTH` | 18 bit | 混频器输入位宽（ADC 符号扩展后） |
| `OUTPUT_WIDTH` | 18 bit | 最终 I/Q 输出位宽 |

这些常量定义在 [ddc_400m.v:31-40](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/ddc_400m.v#L31-L40)。

---

## 3. 本讲源码地图

本讲围绕接收链最前端的 5 个文件展开，它们构成一条「接力赛」式的数据通路：

| 文件 | 模块名 | 角色 | 工作时钟域 |
|---|---|---|---|
| `ad9484_interface_400m.v` | `ad9484_interface_400m` | AD9484 ADC 物理接口，LVDS→CMOS，DDR 捕获拼出 400 MSPS 流 | 400 MHz |
| `nco_400m_enhanced.v` | `nco_400m_enhanced` | 数控振荡器，产生正交本振 sin/cos | 400 MHz |
| `cic_decimator_4x_enhanced.v` | `cic_decimator_4x_enhanced` | 级联积分梳状滤波器，4 倍抽取 | 400 MHz（输出有效降到 100 MSPS） |
| `fir_lowpass.v` | `fir_lowpass_parallel_enhanced` | 32 抽头低通 FIR，滤除抽取镜像 + 补偿 CIC 衰落 | 100 MHz |
| `cdc_modules.v` | `cdc_adc_to_processing` | 多比特 Gray 码 CDC，把 CIC 输出从 400 MHz 域搬到 100 MHz 域 | 400→100 MHz |

把这一切串起来、并完成「符号化、混频、溢出监测、输出寄存」的总控模块是 `ddc_400m.v`（**注意：文件名叫 `ddc_400m.v`，但里面的模块名是 `ddc_400m_enhanced`**，例化时用的是后者，见 [radar_receiver_final.v:214](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v#L214)）。

整条通路的鸟瞰图（请你先记住这张图，后续每一节都是在拆解它）：

```
        400 MHz 时钟域                                          100 MHz 时钟域
 ┌─────────────────────────────────────────────────────┐   ┌──────────────────┐
 │ ADC → 符号化 → NCO(sin,cos) → DSP48E1 混频 → mixed  │   │                  │
 │  8bit   18bit     16bit        ×      34bit         │   │   FIR 低通       │
 │                                            │         │   │   32 抽头        │
 │                                            ▼         │   │   baseband_i/q   │
 │                                   CIC 抽取 4:1       │   │      ▲           │
 │                                   cic_i/q_out ───────┼───┼──► CDC_FIR ──────┤
 │                                   (有效每 4 拍 1 次)  │   │   (Gray 码跨域)  │
 └─────────────────────────────────────────────────────┘   └──────────────────┘
```

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：**ADC 接口 → NCO 正交混频 → CIC 抽取 → FIR 补偿滤波（含真实 CDC）**。

---

### 4.1 ADC 接口：把差分 LVDS 变成 400 MSPS 数据流

#### 4.1.1 概念说明

AD9484 是一片 8 位、最高 1 GSPS 的 ADC。仓库用它工作在 **400 MSPS、LVDS、DDR（双沿）** 模式：

- **LVDS**：数据和时钟都以差分对（P/N）传输，抗干扰强。
- **DDR（Double Data Rate）**：数据在时钟的上升沿和下降沿 **各传一个** 样本。于是 400 MHz 的 DCO 时钟，每个周期送出 2 个样本，拼起来就是 400 MSPS 的数据率（注意：这里「400 MHz 时钟 + DDR」是 AD9484 数据手册规定的 400 MSPS 工作方式，而不是「200 MHz 双沿凑 400」）。

这一级模块 `ad9484_interface_400m` 的职责只有三件事：①把差分信号变成单端；②用专用时钟原语把数据稳稳采进 FPGA；③把上升沿/下降沿两路样本交织成一条连续的 400 MSPS 流，同时给出一个供下游使用的、缓冲好的 400 MHz 时钟 `adc_dco_bufg`。

#### 4.1.2 核心流程

```
adc_d_p/n[8] ──IBUFDS──► adc_data[8]（单端）
adc_dco_p/n  ──IBUFDS──► adc_dco
                            │
                ┌───────────┴────────────┐
                ▼                        ▼
            BUFIO（近零延迟）       adc_clk_mmcm（抖动清洗）
            只驱动 IDDR             走 BUFG 驱动逻辑
                │                        │
                ▼                        ▼
   IDDR：上升沿→rise，下降沿→fall    adc_dco_buffered
                │                  （= adc_dco_bufg，400 MHz）
                ▼
   在 BUFG 域用 dco_phase 翻转，交替选 rise / fall
                ▼
        adc_data_400m + adc_data_valid_400m
```

两个时钟缓冲原语各司其职，这是 Xilinx 源同步接口的标准打法：

- **BUFIO**：插入延迟几乎为零，但 **只能驱动 IOB（输入输出块）里的 IDDR**，不能进通用逻辑。它保证数据经过 IBUFDS 的延时与时钟经过 BUFIO 的延时匹配，消除保持时间违例。
- **adc_clk_mmcm（MMCME2_ADV）**：锁相环把 DCO 「清洗」一遍再走 BUFG 全局网络，把输入抖动从约 50 ps 降到 20–30 ps，改善 400 MHz CIC 路径的时序裕量，然后用它驱动后续所有 fabric 逻辑。

#### 4.1.3 源码精读

端口声明：差分数据 + 差分 DCO + 100 MHz 系统时钟（仅控制用），输出 400 MHz 域的 8 位数据和有效信号，以及缓冲后的 400 MHz 时钟——[ad9484_interface_400m.v:1-16](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/ad9484_interface_400m.v#L1-L16)。

8 路差分数据用 `generate` 循环逐位做 `IBUFDS`，差分终端电阻 `DIFF_TERM` 与电平标准 `IOSTANDARD` 故意不在 RTL 里写死，而是交给 XDC 约束，这样同一份代码可以适配不同 bank 电压的 FPGA（50T 是 3.3V→LVDS_33，200T 是 2.5V→LVDS_25）——[ad9484_interface_400m.v:28-39](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/ad9484_interface_400m.v#L28-L39)。

时钟缓冲：BUFIO 只喂 IDDR，MMCM 清洗后走 BUFG——[ad9484_interface_400m.v:64-80](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/ad9484_interface_400m.v#L64-L80)。

DDR 捕获：每条数据线一个 `IDDR`，`SAME_EDGE_PIPELINED` 模式让上升/下降沿数据在同一拍稳定输出——[ad9484_interface_400m.v:87-104](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/ad9484_interface_400m.v#L87-L104)。

最后一步——把 rise/fall 交织成 400 MSPS 流。注意 `dco_phase` 每拍翻转，用它当多路选择开关，交替输出下降沿数据与上升沿数据：

```verilog
// ad9484_interface_400m.v:152-163（节选）
dco_phase <= ~dco_phase;
if (dco_phase)
    adc_data_400m_reg <= adc_data_fall_bufg;   // 补齐偶数样本
else
    adc_data_400m_reg <= adc_data_rise_bufg;   // 奇数样本
adc_data_valid_400m_reg <= 1'b1;               // ADC 跑起来后始终有效
```

复位用经典「异步复位、同步释放」，并且用 `mmcm_locked` 把释放门控住——400 MHz 域要等到 MMCM 锁定、时钟稳定才退出复位——[ad9484_interface_400m.v:134-144](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/ad9484_interface_400m.v#L134-L144)。

#### 4.1.4 代码实践

1. **实践目标**：理解 DDR 如何让 400 MHz 时钟产出 400 MSPS 数据。
2. **操作步骤**：打开 [ad9484_interface_400m.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/ad9484_interface_400m.v)，在 `iddr_gen` 与「交织」两段代码旁各写一句中文注释，说明 `Q1/Q2` 分别对应哪个沿、`dco_phase` 在选什么。
3. **观察现象**：你会发现 BUFIO 与 BUFG 虽源自同一个 DCO，但用途完全不同——一个「准」、一个「稳」。
4. **预期结果**：能口述出「ADC 一个 DCO 周期内，IDDR 在上升沿采到样本 A、下降沿采到样本 B，再用 `dco_phase` 把 A、B 交替送上 `adc_data_400m`」。
5. 本地无需运行硬件，纯源码阅读型实践。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DIFF_TERM` 和 `IOSTANDARD` 要放在 XDC 里、而不是写进 `IBUFDS` 的参数？

> **答**：因为同一份 RTL 要烧进两种 bank 电压不同的 FPGA（50T 用 LVDS_33、200T 用 LVDS_25）。放进 XDC 后，换板只需换约束文件，不用改 RTL。

**练习 2**：如果删掉 MMCM、让 DCO 直接走 BUFG，系统「功能上」还能跑通，但什么指标会变差？

> **答**：时钟抖动（jitter）会从 ~20–30 ps 退回 ~50 ps，400 MHz CIC 关键路径的建立时间裕量（WNS）变差，采样点不确定度增大，直接降低 ADC 的有效位数（ENOB）和信噪比。

---

### 4.2 NCO 正交混频：把 120 MHz 搬到 0 Hz

#### 4.2.1 概念说明

**NCO（Numerically Controlled Oscillator，数控振荡器）** 是 DDC 的「数字本振」。它按一个叫 **频率调谐字（Frequency Tuning Word, FTW）** 的步长，每拍累加相位，再查正弦表输出 sin/cos。

混频器（mixer）则把 ADC 实信号分别乘上 cos（得 I 路）和 sin（得 Q 路），完成 §2.2 讲的正交下变频。在 FPGA 里，这个「乘法」用硬核乘法器 **DSP48E1** 来做——它自带流水寄存器（AREG/BREG/MREG/PREG），是 400 MHz 能跑通的关键。

> 注意：本模块在数据通路上属于「NCO + 混频」合起来的一级。NCO 产生本振，DSP48E1 完成乘法，两者都在 400 MHz 域。

#### 4.2.2 核心流程

**NCO 的相位累加** 是一根从 0 满量程 \(2^N\) 溢出再回卷的「锯齿」：

\[
\phi[n] = (\phi[n-1] + \text{FTW}) \bmod 2^N
\]

每个周期相位前进 FTW，溢出频率就是输出频率。于是输出频率为：

\[
f_{out} = \frac{\text{FTW}}{2^N} \cdot f_s
\]

反解出我们要的 FTW（令 \(f_{out}=f_{IF}=120\text{ MHz}\)、\(f_s=400\text{ MHz}\)、\(N=32\)）：

\[
\text{FTW} = \frac{f_{IF}}{f_s}\cdot 2^{32} = \frac{120}{400}\cdot 2^{32} = 0.3 \times 4294967296 \approx 1\,288\,490\,189 = \texttt{0x4CCCCCCD}
\]

这正是源码里的「魔数」。混频流程：

```
phase_inc_dithered(FTW + 抖动) ──► NCO ──► sin_out, cos_out（16 bit）
                                              │
adc_data ──► 符号化(18 bit) ──────────────────┤
                                              ▼
                          DSP48E1_I: adc × cos  ──► mixed_i（34 bit）
                          DSP48E1_Q: adc × sin  ──► mixed_q（34 bit）
```

为了让 sin/cos 更「干净」（减少量化产生的杂散 spur），仓库在 FTW 上叠加了一个由 LFSR 产生的 8 位 **相位抖动（phase dither）**——这是 DDS 设计里抑制杂散的常见技巧。

#### 4.2.3 源码精读

FTW 的定义与抖动叠加，注意 `PHASE_INC_120MHZ` 就是我们上面算出来的 `0x4CCCCCCD`——[ddc_400m.v:188-197](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/ddc_400m.v#L188-L197)：

```verilog
localparam PHASE_INC_120MHZ = 32'h4CCCCCCD;
// 加抖动以降低杂散（寄存输出，满足 400 MHz 时序）
phase_inc_dithered <= PHASE_INC_120MHZ + {24'b0, phase_dither_bits};
```

NCO 例化，把抖动后的 FTW 喂进去，拿到 16 位 sin/cos——[ddc_400m.v:202-211](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/ddc_400m.v#L202-L211)。

ADC 是无符号 8 位（0~255），混频前要变成有符号。下面这行把「无符号 8 位」转成「有符号 18 位」（减去中点 128，左对齐到 18 位）——[ddc_400m.v:228-229](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/ddc_400m.v#L228-L229)：

```verilog
assign adc_signed_w = {1'b0, adc_data, {(MIXER_WIDTH-ADC_WIDTH-1){1'b0}}}
                    - {1'b0, {ADC_WIDTH{1'b1}}, {(MIXER_WIDTH-ADC_WIDTH-1){1'b0}}} / 2;
```

混频用 DSP48E1 直接例化（I 路），关键属性 `AREG/BREG/MREG/PREG` 全开，`OPMODE=0000101` 表示「只乘不加」——[ddc_400m.v:324-410](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/ddc_400m.v#L324-L410)。Q 路结构完全对称，只是 B 端口从 cos 换成 sin——[ddc_400m.v:412-489](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/ddc_400m.v#L412-L489)。

> 提示：源码里 `ifdef SIMULATION` 分支提供 Icarus 仿真的行为级等价模型（因为 Icarus 不认识 DSP48E1 原语）；`else` 分支才是 Vivado 综合用的真 DSP48E1。读源码时两段对照看，能同时理解「综合长什么样」和「仿真跑什么」。

再看 NCO 内部。相位累加用一片 DSP48E1 跑「P = P + C」累加模式（`OPMODE=0101100`），P 寄存器 **本身就是** 相位累加器，省掉了一长串 CARRY4 进位链——[nco_400m_enhanced.v:158-243](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/nco_400m_enhanced.v#L158-L243)。

正弦查表只存 0–90° 的 1/4 波形（64 项），靠象限逻辑把全周期拼出来；只用相位高 8 位寻址——[nco_400m_enhanced.v:62-103](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/nco_400m_enhanced.v#L62-L103)。这是 DDS 节省存储的经典手法：存 1/4 波，靠 `sin(π−θ)=sin θ` 与符号翻转覆盖 360°。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证 FTW「魔数」，建立对 NCO 频率公式的信心。
2. **操作步骤**：用计算器或一段 Python 算 \(\lfloor 0.3 \times 2^{32} \rceil\)，看是否等于 `1288490189`，再转成 16 进制看是否等于 `0x4CCCCCCD`。
3. **观察现象**：四舍五入后的整数与源码常量逐位吻合。
4. **预期结果**：能写出「若想把中频从 120 MHz 改成 100 MHz，FTW 应改成 \(\lfloor 0.25 \times 2^{32} \rceil = \texttt{0x40000000}\)」。
5. 示例代码（非项目原有代码）：

```python
# 示例代码：验证 NCO 频率调谐字
N, fs, fif = 32, 400e6, 120e6
ftw = round(fif / fs * (2**N))
assert ftw == 0x4CCCCCCD, hex(ftw)
print(hex(ftw), '->', hex(round(0.25 * 2**32)))   # 改 100MHz 时该用多少
```

#### 4.2.5 小练习与答案

**练习 1**：为什么混频要分别乘 cos（I）和 sin（Q），而不是只乘一个 cos？

> **答**：只乘 cos 会把正频率和负频率「折叠」到一起，丢失目标运动方向（多普勒正负）的信息。I/Q 一对构成复基带，能区分靠近/远离，后续 Doppler FFT 才有意义。

**练习 2**：`adc_signed_w` 那一行为什么要「减去一半」？

> **答**：ADC 输出是无符号 8 位（0~255，中点 128）。有符号运算前必须把直流中点减掉，否则「零输入」会被当成 +128 的直流偏置，混频后会多出一个本振泄漏的 spur。

**练习 3**：NCO 为什么只存 1/4 波形（64 项）的正弦表？

> **答**：利用正弦的对称性（第一、二象限对称；半波反对称），64 项 + 象限符号逻辑就能合成完整 360°，存储量只有完整表的 1/4。表又足够小（1024 bit），用分布式 RAM（LUTRAM）即可，连 BRAM 都不必占。

---

### 4.3 CIC 抽取：400 MSPS → 100 MSPS 的速率变换

#### 4.3.1 概念说明

混频后信号已经在基带，但仍是 400 MSPS，数据率太高。**CIC（Cascaded Integrator-Comb，级联积分梳状）滤波器** 是一种 **不用乘法器** 的抽取滤波器，特别适合做高速第一级降速。

CIC 由三段组成：

1. **积分器（Integrator）**：\(H_I(z)=1/(1-z^{-1})\)，就是一个累加器，工作在 **高采样率**。
2. **抽取（↓D）**：每 D 个样本丢掉 D−1 个。
3. **梳状器（Comb）**：\(H_C(z)=1-z^{-M}\)，工作在 **降速后** 的低采样率。

把梳状放到抽取之后、积分放到抽取之前，是 CIC 的精髓——积分在高频跑、梳状在低频跑，**两者中间正好借抽取把速率降下来**，且全程只需要加减法，零乘法。

CIC 的代价是 **通带不平坦**（sinc 形的「衰落/droop」），这正是下一节 FIR 要补偿的对象。

#### 4.3.2 核心流程

仓库的 CIC 是 `STAGES=5`（5 级积分 + 5 级梳状）、`DECIMATION=4`、`M=1`：

```
data_in ─► [I0]→[I1]→[I2]→[I3]→[I4] ──(每 4 个取 1)──► [C0]→[C1]→[C2]→[C3]→[C4] ─► >>>10 ─► data_out
          (5 级积分，400 MHz)                          (5 级梳状，100 MSPS 有效)
```

5 级积分用 **DSP48E1 的 PCOUT→PCIN 专用级联路径** 串起来，走的是硅片上垂直相邻 DSP 之间的专用布线，零 fabric 延迟，这是 400 MHz 能闭合的关键。

CIC 的直流增益由级数 N、抽取因子 D、梳状延迟 M 决定：

\[
G_{DC} = (D \cdot M)^{N} = (4 \cdot 1)^{5} = 4^{5} = 1024 = 2^{10}
\]

所以输出要 **右移 10 位**（`>>> 10`）把增益归一化。

#### 4.3.3 源码精读

模块端口与参数：5 级、4 倍抽取，输入输出都是 18 位有符号——[cic_decimator_4x_enhanced.v:1-23](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cic_decimator_4x_enhanced.v#L1-L23)。

积分器第 0 级用 DSP48E1 跑 `P = P + C`（`OPMODE=0101100`），`CREG=1` 把输入寄存在 DSP 内部以消除 fabric→DSP 的建立时间违例——[cic_decimator_4x_enhanced.v:108-184](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cic_decimator_4x_enhanced.v#L108-L184)。

第 1~4 级积分器用 **`PCOUT→PCIN` 级联**（`OPMODE=0010010`，`P = P + PCIN`），逐级串联——[cic_decimator_4x_enhanced.v:186-482](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cic_decimator_4x_enhanced.v#L186-L482)。

抽取控制：计数器数到 `DECIMATION-1` 就锁存一级积分器输出、产生一次有效——[cic_decimator_4x_enhanced.v:740-749](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cic_decimator_4x_enhanced.v#L740-L749)：

```verilog
if (decimation_counter == DECIMATION - 1) begin
    decimation_counter <= 0;
    data_valid_delayed <= 1;                      // 每 4 拍产生一次有效
    integrator_sampled <= p_out_4[COMB_WIDTH-1:0]; // 锁存最后一级积分结果
end else decimation_counter <= decimation_counter + 1;
```

增益归一化与饱和：梳状结果右移 10 位，再判饱和——[cic_decimator_4x_enhanced.v:850-857](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cic_decimator_4x_enhanced.v#L850-L857)：

```verilog
temp_scaled_output <= comb[STAGES-1] >>> 10;   // 除以 4^5 = 1024，归一化直流增益
```

DDC 里例化两个 CIC（I、Q 各一），输入取混频乘积 `mixed_i[33:16]` 的高 18 位——[ddc_400m.v:566-584](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/ddc_400m.v#L566-L584)。

#### 4.3.4 代码实践

1. **实践目标**：理解「抽取因子 4」如何把 400 MHz 降到 100 MSPS。
2. **操作步骤**：在 [ddc_400m.v:566-584](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/ddc_400m.v#L566-L584) 找到 CIC 例化，记下 `DECIMATION`；再到 [cic_decimator_4x_enhanced.v:740-749](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cic_decimator_4x_enhanced.v#L740-L749) 看计数器如何数到 3 才输出一次有效。
3. **观察现象**：`data_valid_delayed` 每 4 个 400 MHz 时钟拍只拉高 1 次，即输出有效率为 400/4 = 100 MSPS。
4. **预期结果**：能口算「5 级 CIC、4 倍抽取 → 直流增益 4⁵=1024 → 需 `>>>10` 归一」，并解释为什么这一步输出 **仍处于 400 MHz 时钟域**、只是有效信号变稀疏（这是下一节 CDC 的伏笔）。
5. 待本地验证：可在仿真里数 `cic_valid` 脉冲间隔，确认恰为 4 个 `clk_400m` 周期。

#### 4.3.5 小练习与答案

**练习 1**：CIC 全程没有一个乘法器，凭什么能当滤波器用？

> **答**：积分器（累加）+ 梳状（差分）的组合，其等效冲激响应是宽度为 D·M 的矩形滑动平均的 N 次卷积，频响是 sinc^N 低通。它只用加减法就能滤除抽取后的高频镜像，所以适合做高速第一级。

**练习 2**：为什么要做 `>>> 10`？不做的后果是什么？

> **答**：5 级 4 倍抽取的直流增益是 \(4^5=1024=2^{10}\)。不归一化会让输出幅度放大 1024 倍，迅速溢出 18 位范围，后级全部饱和。右移 10 位正好抵消这个增益。

**练习 3**：积分器工作在 400 MHz、梳状工作在 100 MSPS，为什么可以「省」成只有加减？

> **答**：因为抽取放在积分与梳状中间：积分在高频对每个样本累加（高频做廉价加法），抽取后梳状只需对降速后的稀疏样本做差分。若把梳状也放高频就会白算被丢弃的样本，CIC 的效率优势正来自「让昂贵的工作发生在低速」。

---

### 4.4 FIR 补偿滤波与真实时钟域跨越

#### 4.4.1 概念说明

CIC 把数据率降到了 100 MSPS，但留下两个问题：①通带内有 sinc 形衰落（droop），信号高频分量被压低；②抽取后仍残留镜像，需要再滤。这两件事交给一片 **32 抽头低通 FIR** 来做——它既能滤镜像，其系数形状也可设计成对 CIC 衰落的「补偿」。

更关键的是：FIR 工作在 **100 MHz 时钟域**，而 CIC 输出还在 **400 MHz 时钟域**（只是有效信号变稀疏）。这中间必须做一次 **真正的跨时钟域（CDC）**。仓库用 `cdc_adc_to_processing`（Gray 码 + toggle）完成这次跨越。

这里要重点消化一个 **反直觉的工程教训**：

> 为什么不直接对原始 ADC 数据做 Gray 码 CDC？因为 **Gray 码只保证「相邻两个值只差 1 LSB（只翻转 1 位）」时安全**。ADC 样本逐拍剧烈变化（很多位同时翻转），Gray 编码毫无保护作用；而且跨越的是 400→100 MHz 的 **速率差**（源每拍都产、目的每 4 拍才采一次），同频 Gray 握手根本搬不动。**正确做法是先用 CIC 把速率降到与目的域匹配的 100 MSPS，再做 Gray CDC。**

这段教训不是我们臆想的，而是仓库里一段真实的「踩坑修复」注释——见下方源码精读。

#### 4.4.2 核心流程

```
cic_i_out（400 MHz 域，有效 100 MSPS）
      │
      ▼
cdc_adc_to_processing（CDC_FIR_i）
   源域：Gray 编码 + toggle 翻转
   目的域：3 级同步 → Gray 解码 → 检测 toggle 变化 → 产生 dst_valid
      │
      ▼  fir_d_in_i（100 MHz 域）
fir_lowpass_parallel_enhanced（32 抽头流水线 FIR，100 MHz）
      │
      ▼  fir_i_out
baseband_i_reg（100 MHz 输出寄存）──► baseband_i
```

Gray 码 + toggle CDC 的安全性来自两点：①数据 **先寄存一拍再做 Gray 编码**，避免组合逻辑直接喂给第一级同步器（这正是 u3-l2 讲的 CDC-10 规则）；②用 **toggle 计数器** 通知对岸「有新数据」，对岸检测到 toggle 变化才置 `dst_valid`。

#### 4.4.3 源码精读

先看那段「踩坑修复」注释——它直接解释了为什么不能对 ADC 做 Gray CDC，以及真正的跨越发生在哪里——[radar_receiver_final.v:200-205](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v#L200-L205)：

```verilog
// NOTE: The cdc_adc_to_processing instance that was here used src_clk=dst_clk=clk_400m
// (same clock domain — no crossing). Gray-code CDC on same-clock with fast-changing
// ADC data corrupts samples because Gray coding only guarantees safe transfer of
// values that change by 1 LSB at a time. The real 400MHz→100MHz CDC crossing is
// handled inside ddc_400m_enhanced via CIC decimation + CDC_FIR instances.
```

这段注释说的就是：曾经有人在这里放过一个 `cdc_adc_to_processing`，但源/目都是 `clk_400m`（同域，没意义），而且 ADC 数据变化剧烈，Gray 码会 **破坏样本**。于是它被删掉，ADC 直接送进 DDC；真正的跨越改由 DDC 内部的 CIC 抽取 + `CDC_FIR` 完成。

DDC 内部，I/Q 两路各挂一个 `cdc_adc_to_processing`，把 CIC 输出从 `clk_400m` 搬到 `clk_100m`——[ddc_400m.v:596-622](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/ddc_400m.v#L596-L622)：

```verilog
cdc_adc_to_processing #( .WIDTH(18), .STAGES(3) ) CDC_FIR_i(
    .src_clk(clk_400m), .dst_clk(clk_100m),
    .src_data(cic_i_out), .src_valid(cic_valid_i),
    .dst_data(fir_d_in_i), .dst_valid(fir_in_valid_i)
);
```

Gray 编解码与「先寄存再编码」的源域逻辑——[cdc_modules.v:27-72](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cdc_modules.v#L27-L72)。注意 `src_data_gray` 是 **寄存输出**（`src_valid` 触发时才编码翻转），不是组合直连：

```verilog
end else if (src_valid) begin
    src_data_reg  <= src_data;
    src_data_gray <= binary_to_gray(src_data);   // 先寄存一拍再 Gray，满足 CDC-10
    src_toggle    <= src_toggle + 1;              // 通知对岸有新数据
end
```

目的域 3 级同步链 + toggle 变化检测产生 `dst_valid`——[cdc_modules.v:78-128](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cdc_modules.v#L78-L128)。

FIR 是 32 抽头、对称系数、5 级流水二叉加法树，专为 100 MHz 时序闭合设计——[fir_lowpass.v:3-17](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/fir_lowpass.v#L3-L17)（模块端口与参数 `TAPS=32`），对称低通系数见 [fir_lowpass.v:78-88](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/fir_lowpass.v#L78-L88)。FIR 例化与最终输出寄存（100 MHz 域）见 [ddc_400m.v:629-669](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/ddc_400m.v#L629-L669)。

#### 4.4.4 代码实践

1. **实践目标**：彻底搞懂「为什么先 CIC 再 CDC，而不能直接对 ADC CDC」。
2. **操作步骤**：
   - 打开 [radar_receiver_final.v:200-205](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v#L200-L205)，把注释里给出的 **两个** 理由各抄一句中文。
   - 打开 [ddc_400m.v:596-622](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/ddc_400m.v#L596-L622)，确认 `src_clk`/`dst_clk` 分别接 `clk_400m`/`clk_100m`（这次是 **真跨域**，不是被删掉的同域实例）。
3. **观察现象**：CIC 之后每 4 个 400 MHz 拍才产生 1 个有效样本（100 MSPS），与目的域 100 MHz 的采样能力 **恰好匹配**，所以 Gray toggle 握手才来得及完成。
4. **预期结果**：能用自己的话回答——「Gray 码 CDC 要求相邻样本只差 1 LSB，且源/目速率要匹配；原始 400 MSPS ADC 数据两个条件都不满足，所以必须先 CIC 降速再 CDC。」
5. 待本地验证：可在仿真里把 `src_valid` 频率设成高于 `dst_clk`，观察 `dst_valid` 是否丢包。

#### 4.4.5 小练习与答案

**练习 1**：被删掉的那个 `cdc_adc_to_processing` 实例错在哪两处？

> **答**：①源时钟 = 目的时钟 = `clk_400m`，根本没有跨域，CDC 多此一举；②ADC 数据变化远超 1 LSB，Gray 编码只对「相邻差 1」安全，对剧烈变化的样本会破坏数据。

**练习 2**：为什么放在 CIC 之后的 Gray CDC 就安全了？

> **答**：两个条件都满足了——CIC 已把有效数据率降到 100 MSPS，与目的 100 MHz 域速率匹配，toggle 握手来得及；且 CIC 输出是滤波后的平滑值，相邻样本变化小，Gray 编码的安全性前提成立。

**练习 3**：FIR 放在 CIC 之后，除了滤镜像还能干什么？

> **答**：CIC 通带有 sinc^5 衰落（droop），通带高频被压低。FIR 的系数可设计成对这段衰落做「补偿」（提升高频），把通带拉平，同时进一步抑制残留镜像。所以这级 FIR 常被称为「补偿 FIR（CFIR）」。

---

## 5. 综合实践

**任务**：把本讲四节串起来，画出「一个 ADC 样本的一生」，并标注每一段的时钟域。

请按下列步骤完成（纯源码阅读 + 推算，不需硬件）：

1. **定位入口**：从 [radar_receiver_final.v:188-198](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v#L188-L198) 的 `ad9484_interface_400m` 例化开始，确认 8 位 `adc_data_cmos` 与 `clk_400m` 都来自这里。
2. **进入 DDC**：顺着 [radar_receiver_final.v:214-226](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v#L214-L226) 进入 `ddc_400m_enhanced`，在一张纸上画出下面的表格并填空。

| 阶段 | 处理 | 源码位置（行） | 工作时钟域 | 数据率 |
|---|---|---|---|---|
| ① 采入 | ADC 物理接口 | ad9484_interface_400m.v:152-163 | 400 MHz | 400 MSPS |
| ② 符号化 | 8→18 位有符号 | ddc_400m.v:228-229 | 400 MHz | 400 MSPS |
| ③ 本振 | NCO 产 sin/cos | nco_400m_enhanced.v:158-243 | 400 MHz | 400 MSPS |
| ④ 混频 | DSP48E1 乘 cos/sin | ddc_400m.v:324-489 | 400 MHz | 400 MSPS |
| ⑤ 抽取 | CIC 5 级 ↓4 | cic_decimator_4x_enhanced.v:740-749 | 400 MHz（有效变稀疏） | 100 MSPS |
| ⑥ 跨域 | Gray 码 CDC | ddc_400m.v:596-622 | 400→100 MHz | 100 MSPS |
| ⑦ 低通 | 32 抽头 FIR | fir_lowpass.v:142-292 | 100 MHz | 100 MSPS |
| ⑧ 输出 | baseband_i/q 寄存 | ddc_400m.v:657-669 | 100 MHz | 100 MSPS |

3. **回答关键问题**（口述或写成一段话）：
   - 400 MHz 是在哪一步、靠什么机制降到 100 MSPS 的？
   - 这一步降速之后，数据为什么「还在 400 MHz 时钟域」？真正切到 100 MHz 域发生在哪一步？
   - 为什么不能把第 ⑥ 步提前到第 ① 步之后？
4. **预期答案要点**：
   - 降速发生在第 ⑤ 步 CIC：`DECIMATION=4` 让 `data_out_valid` 每 4 个 400 MHz 拍只拉高 1 次 → 100 MSPS。
   - CIC 仍跑在 `clk_400m` 上，只是有效信号变稀疏；真正切到 100 MHz 域是第 ⑥ 步的 `CDC_FIR`（`clk_400m`→`clk_100m`）。
   - 不能提前到 ① 之后，因为原始 400 MSPS ADC 数据变化剧烈、速率又远高于 100 MHz，Gray 码 CDC 既不安全也搬不动；必须先 CIC 降速匹配后才能安全跨域。
5. **待本地验证（可选进阶）**：若环境装了 iverilog，可到 `9_Firmware/9_2_FPGA/tb/` 下找一个 DDC 相关 testbench，用 `./run_regression.sh` 跑一遍，在波形上数 `cic_valid` 与 `baseband_valid_i` 的脉冲间隔，验证抽取比 4 与最终 100 MSPS 有效率。

---

## 6. 本讲小结

- **DDC 的使命**：把 400 MSPS 的实 ADC 信号变成 100 MSPS 的复（I/Q）基带信号，剥掉 120 MHz 中频载波。
- **四段通路**：ADC 接口（DDR 拼出 400 MSPS）→ NCO 正交本振 + DSP48E1 混频（实→复、IF→基带）→ CIC 抽取（400→100 MSPS）→ FIR 低通/补偿（滤镜像、平通带）。
- **NCO 魔数**：`FTW = f_IF/f_s · 2^32 = 0.3·2^32 ≈ 0x4CCCCCCD`，叠加 LFSR 抖动抑制杂散。
- **CIC 增益**：5 级、4 倍抽取 → 直流增益 \(4^5=1024=2^{10}\)，故输出 `>>> 10` 归一化；全程只用 DSP48E1 的加减与级联，零乘法。
- **真实 CDC 在 CIC 之后**：FIR 在 100 MHz 域、CIC 在 400 MHz 域，中间用 Gray 码 toggle CDC 搬运。
- **关键教训**：原始 ADC 数据 **不能** 直接做同频 Gray CDC——Gray 只对「相邻差 1 LSB」安全，且 400 MSPS 与 100 MHz 速率不匹配；必须先 CIC 降速再跨域。这条教训有仓库里一段真实的「踩坑修复」注释为证。

---

## 7. 下一步学习建议

本讲把接收链最前端（DDC）讲透了，输出 `baseband_i/q` 之后，数据会进入脉冲压缩。建议按以下顺序继续：

1. **下一讲 [u4-l2 匹配滤波与脉冲压缩]**：DDC 的 I/Q 基带如何被 `matched_filter_multi_segment` 做频域匹配滤波，压成距离像（range profile）。重点关注 `latency_buffer` 如何对齐参考 chirp 与回波。
2. **回头补 [u3-l2 时钟域、复位同步与 CDC 基础]**：如果本讲的 Gray 码 CDC、`ASYNC_REG`、复位同步让你想深入，u3-l2 有更系统的 CDC 分类（单比特 / 多比特 / 握手）与 CDC-10 规则。
3. **延伸阅读源码**：
   - 想懂 400 MHz 时序闭合的「黑魔法」→ 重读 `cic_decimator_4x_enhanced.v` 里 DSP48E1 的 `PCOUT→PCIN` 级联与 `CREG/AREG/BREG/PREG` 流水。
   - 想懂 FIR 系数怎么设计成补偿滤波器 → 对比 `fir_lowpass.v` 的系数与理想 sinc^5 逆响应。
4. **动手方向**：仿一遍 DDC，在波形上确认 `cic_valid` 每 4 拍一次、`baseband_valid` 在 100 MHz 域连续，亲手验证「抽取 4 + 跨域」的速率变换。
