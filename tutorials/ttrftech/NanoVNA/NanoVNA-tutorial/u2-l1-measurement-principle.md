# u2-l1 VNA 测量原理与 sweep() 主循环

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 NanoVNA 「把射频测量搬到音频频段」的外差架构：为什么本振与激励要相差 5kHz（`FREQUENCY_OFFSET`），这 5kHz 如何让一台音频 codec 就能完成从 50kHz 到 2.7GHz 的矢量测量。
2. 读懂 `sweep()` 这个只有 40 行、却是整台仪器心脏的函数：它如何逐频点地「设频率 → 切通道 → 等采样 → 算复数」。
3. 理解 CH0（反射）与 CH1（传输）两路测量的物理含义，以及 `tlv320aic3204_select()` 是怎样用一个 I2C 命令在两路之间切换的。
4. 理解采样函数指针 `sample_func` 的设计意图：为什么热路径上用函数指针而不是 `if/else`。
5. 用 Python 从复数反射系数 Γ 计算驻波比 VSWR，并理解「开路 / 短路 / 匹配负载」这三种校准标准件在数学上分别对应 Γ = +1、-1、0——这是第 3 单元校准讲义的伏笔。

本讲不需要你有 NanoVNA 硬件。除一处标注「待本地验证」的可选真机任务外，所有实践都在 PC 上完成。

## 2. 前置知识

阅读本讲前，请先确认你理解了 u1-l1 ~ u1-l3 的三个结论：NanoVNA 是 STM32F072 + ChibiOS 的双线程固件；`nanovna.h` 是全项目唯一公共头文件，`current_props` 别名宏是读懂全局变量的钥匙；低优先级的 Thread1 负责测量与绘图，main 线程只跑 USB shell。在此基础上，本讲需要补充四个物理概念。

**反射系数 Γ（gamma）**。当一列电磁波沿特性阻抗为 \( Z_0 \)（NanoVNA 为 50Ω）的传输线传到负载 \( Z_L \) 时，一部分能量被反射回来。反射波与入射波的复数比值就是反射系数：

\[
\Gamma = \frac{Z_L - Z_0}{Z_L + Z_0}
\]

它是复数：模 \( |\Gamma| \in [0,1] \) 表示反射了多少能量，辐角表示反射时相位偏了多少。对无源器件，\( |\Gamma| \le 1 \)。三个特例请务必记住，后面反复用到：

| 负载 | \( Z_L \) | \( \Gamma \) | 物理含义 |
|---|---|---|---|
| 理想开路 | \( \infty \) | \( +1 \) | 全反射，反射波与入射波同相 |
| 理想短路 | \( 0 \) | \( -1 \) | 全反射，反射波反相 |
| 理想匹配负载 | \( 50\Omega \) | \( 0 \) | 无反射，能量全部被吸收 |

**驻波比 VSWR**。工程上常用一个 1 到 ∞ 之间的标量描述匹配好坏：

\[
\mathrm{VSWR} = \frac{1+|\Gamma|}{1-|\Gamma|}
\]

VSWR = 1 表示完美匹配，越大越差。它是第 4 单元里 SWR 轨迹格式的数学基础。

**S 参数**。把被测件（DUT）看成二端口网络，\( S_{11} \) 是端口 1 的反射系数（激励和测量都在端口 1），\( S_{21} \) 是端口 1 到端口 2 的正向传输系数（激励在端口 1、测量在端口 2）。NanoVNA 的 CH0 测 \( S_{11} \)，CH1 测 \( S_{21} \)。

**定向电桥与外差**。标量测量只要知道幅度，矢量测量必须同时知道幅度和相位。NanoVNA 的做法是：激励信号一路直接进参考通道，另一路经过定向电桥送到 DUT、再把反射回来的信号送入测量通道；两个通道各自与一个「本振」混频。如果本振频率是 \( f + 5\,\mathrm{kHz} \) 而激励是 \( f \)，混频输出的差频就恒等于 5kHz——**无论 \( f \) 是 50kHz 还是 2.7GHz**。这个恒定的 5kHz「中频」落在音频范围，于是可以用一颗便宜的音频 codec 采样，再用数字信号处理算出两通道的复数比值。这就是 u1-l1 里说的「外差到音频」架构，本讲 4.1 会用源码把它落实。

## 3. 本讲源码地图

| 文件 | 本讲涉及内容 | 作用 |
|---|---|---|
| [main.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c) | `sweep()`、`sample_func`、`dsp_start/dsp_wait`、`set_frequency` | 测量主循环与采样同步 |
| [nanovna.h](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h) | `FREQUENCY_OFFSET`、`measured`、`SWEEP_*`、采样函数原型 | 常量与接口契约 |
| [si5351.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c) | CLK0/CLK1/CLK2 分工注释、`ofreq = freq + offset` | 5kHz 频偏的硬件落点 |
| [tlv320aic3204.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/tlv320aic3204.c) | `tlv320aic3204_select()`、两份通道路由表 | CH0/CH1 切换 |
| [dsp.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c) | `calculate_gamma()`、`fetch_amplitude*()` | `sample_func` 指向的三个候选函数 |
| [README.md](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/README.md) | Reference 一节指向原理图与框图 | 硬件资料入口 |

框图与原理图在 [README.md:94-99](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/README.md#L94-L99) 的 Reference 一节可以找到：`doc/nanovna-blockdiagram.png`（框图）和 `doc/nanovna-sch.pdf`（原理图）。建议把框图和本讲 4.1 的信号链对照着看。

## 4. 核心概念与源码讲解

### 4.1 电桥与 5kHz 频偏：把射频测量搬进音频频段

#### 4.1.1 概念说明

一台「真正的」射频 VNA 需要在每个频点做复数（幅相）测量，传统方案是可调本振 + 鉴相器，硬件昂贵。NanoVNA 的天才之处在于一个观察：**测量两路信号的复数比值，不需要在射频频率上采样**。

只要让本振和激励永远相差一个固定的 5kHz，混频器输出的差频就恒为 5kHz。5kHz 是音频，于是：

- 采样器可以是一颗几十元的立体声音频 codec（tlv320aic3204），而不是射频 ADC；
- 采样率固定为 48kHz 量级，与被测射频频率完全解耦；
- 「本振」不需要是独立的可调振荡器，而是同一颗 si5351 时钟发生器的另一个输出——两路信号同源，相位噪声大量抵消；
- 正交解调可以用一张固定的 48 行正弦余弦表（`dsp.c` 的 `sincos_tbl`）在软件里完成，对所有频点通用。

代价是：混频之后的 DSP 必须「认得」这个 5kHz，表也必须按 5kHz 生成。所以这个 5kHz 在固件里是一等公民常量，改名 `FREQUENCY_OFFSET`，并且注释明确警告：改它就要重新生成 `dsp.c` 里的表。

#### 4.1.2 核心流程

一次测量的信号链（自上而下）：

```text
si5351 CLK1 = f          ──► 激励 ──► 定向电桥 ──► DUT (CH0 口)
                                          │
                                          └─► 反射信号 ──► 混频器 B ──┐
si5351 CLK0 = f + 5kHz   ──► 本振 ──┬──► 混频器 A ──► 参考中频 5kHz    │
                                      └──► 混频器 B ──► 测量中频 5kHz ◄─┘
                                              │
codec 立体声 ADC 48kHz 采样（左/右各一路 5kHz 音频）
                                              │
dsp_process(): 用 sincos_tbl 做数字正交解调，累加出 4 个数
  acc_ref_s / acc_ref_c   （参考通道的 sin / cos 分量）
  acc_samp_s / acc_samp_c （测量通道的 sin / cos 分量）
                                              │
calculate_gamma(): 复数除法 Γ = 测量 ÷ 参考
```

要点是：**混频把「射频幅度与相位信息」原封不动地搬运到了 5kHz 载波上**。两路中频的幅度比就是 \( |\Gamma| \)，相位差就是 \( \angle\Gamma \)。而复数除法天然同时表达这两件事：

\[
\Gamma = \frac{S_\mathrm{samp}}{S_\mathrm{ref}} = \frac{|S_\mathrm{samp}|}{|S_\mathrm{ref}|} \cdot e^{j(\varphi_\mathrm{samp} - \varphi_\mathrm{ref})}
\]

用参考通道做分母还有一层意义：激励信号的绝对幅度波动（si5351 输出电平漂移、线缆损耗）会同时出现在分子分母上，相除后自动抵消。这是一种「比值测量」思想，也是后面校准讲义里误差模型的起点。

#### 4.1.3 源码精读

5kHz 频偏定义在公共头文件里，紧挨着它的注释说明了它与 `dsp.c` 正弦表的共生关系：

[nanovna.h:29-38](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L29-L38) 定义了四个测量相关的核心常量：最低/最高扫描频率（50kHz ~ 2.7GHz）、**`FREQUENCY_OFFSET 5000`（频偏 5kHz，注释明确写着「`dsp.c` 里的 sin_cos 表是按这个偏移生成的，改它就必须重新生成新表」**，所以不要随手改这个数字）、光速（时域变换换算距离用）和 π。

这个频偏在硬件侧的落点在 si5351 驱动里。`si5351.c` 顶部的注释直接给出了三个时钟输出的分工：

[si5351.c:379-385](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L379-L385) 这段注释是理解整机架构的钥匙：**CLK0 输出 `frequency + offset`（本振）、CLK1 输出 `frequency`（激励）、CLK2 固定输出 8MHz**。也就是说「本振」和「激励」来自同一颗芯片的两个输出，5kHz 差值在寄存器写入时由软件保证。

在 `si5351_set_frequency()` 里能看到 `ofreq`（offset frequency）就是简单加法：

[si5351.c:396](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L396) `uint32_t ofreq = freq + current_offset;`——本振频率 = 激励频率 + 5kHz。`current_offset` 是 [si5351.c:41](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L41) 的静态变量，初值取自 `FREQUENCY_OFFSET`；[si5351.c:59-63](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L59-L63) 的 `si5351_set_frequency_offset()` 允许通过 shell 命令 `offset` 在运行时修改它（同时把 `current_freq` 清零强制重设频率），这是调试电桥时用的后门。

频偏的另一端——「认得 5kHz」的数字本振表——在 dsp.c 开头：

[dsp.c:29-42](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L29-L42) `sincos_tbl[48][2]` 是 48 行、每行一对 `(sin, cos)` 的 Q15 定点表。48 行恰好对应一个音频缓冲里每路的样本数（`SAMPLE_LEN`，见 [nanovna.h:111-112](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L111-L112)）。表的每一行就是 5kHz 中频在对应采样时刻的单位复数本振，乘上去再求和，就是离散傅里叶变换在 5kHz 这一个频点上的取值。表的生成方式与相位约定属于 u2-l4 的内容，本讲只需要知道「它按 `FREQUENCY_OFFSET` 生成，两者必须配套」。

最后，DSP 累加出的四个数如何变成 Γ：

[dsp.c:88-108](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L88-L108) `calculate_gamma()` 执行复数除法 `samp ÷ ref`。代码约定「实部 = cos 分量、虚部 = sin 分量」，即复数 \( X = X_c + jX_s \)，于是 \( \Gamma = (S_c + jS_s)/(R_c + jR_s) \) 展开成两行：`gamma[0] = (sc*rc + ss*rs)/rr` 是实部，`gamma[1] = (ss*rc - sc*rs)/rr` 是虚部，其中 `rr = rs² + rc²` 是参考的模平方（除以模平方 = 乘以共轭再归一化，一次除法都不用开方）。输出写入调用方给定的 `float gamma[2]`——在 `sweep()` 里，这个指针就是 `measured[0][i]` 或 `measured[1][i]`。

#### 4.1.4 代码实践

**实践目标**：亲手算一遍 Γ → VSWR 的换算，并用三个理想标准件验证 Γ 的三个特例，为第 3 单元的 SOL 校准建立直觉。

**操作步骤**（本实践的指定任务）：

1. 新建 `vswr_practice.py`（示例代码，放在仓库外的任意目录，不要写进固件源码树）：

```python
# 示例代码：本讲指定的实践任务
import cmath

def vswr_from_gamma(g):
    """由复数反射系数计算驻波比 VSWR=(1+|Γ|)/(1-|Γ|)"""
    mag = abs(g)
    if mag >= 1.0:          # |Γ|>=1 时 VSWR 无定义（无源器件不会出现）
        return float('inf')
    return (1 + mag) / (1 - mag)

def gamma_of_load(z_load, z0=50.0):
    """由负载阻抗计算反射系数 Γ=(Z-Z0)/(Z+Z0)"""
    return (z_load - z0) / (z_load + z0)

# 三种理想校准标准件
standards = {
    'OPEN  (理想开路)': complex(1e12, 0),   # Z -> ∞
    'SHORT (理想短路)': complex(0.0,   0),   # Z = 0
    'LOAD  (50Ω 匹配)': complex(50.0,  0),   # Z = Z0
}
for name, z in standards.items():
    g = gamma_of_load(z)
    print(f'{name}:  Γ = {g.real:+.3f}{g.imag:+.3f}j   '
          f'|Γ| = {abs(g):.3f}   VSWR = {vswr_from_gamma(g):.1f}')

# 一个不完美负载：75Ω 纯电阻
g = gamma_of_load(75.0)
print(f'75Ω 负载:  Γ = {g.real:+.3f}{g.imag:+.3f}j   '
      f'|Γ| = {abs(g):.3f}   VSWR = {vswr_from_gamma(g):.3f}')
```

2. 运行 `python3 vswr_practice.py`。

**需要观察的现象**：

- OPEN 给出 \( \Gamma \approx +1 \)、VSWR → ∞；
- SHORT 给出 \( \Gamma = -1 \)（实部恰为 -1）；
- LOAD 给出 \( \Gamma = 0 \)、VSWR = 1.0；
- 75Ω 给出 \( \Gamma = 0.2 \)、VSWR = 1.5。

**预期结果**：四个输出与上表一致（OPEN 的实部是一个非常接近 1 的浮点数，如 0.999...）。这三个 Γ 特例正是 SOL（Short-Open-Load）校准的标准件：它们是仅有的三种「Γ 精确已知」的负载，第 3 单元将用它们反解仪器自身的误差项。

**验证结论**：本实践在 PC 上即可完整运行，无需真机。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `FREQUENCY_OFFSET` 改成 6000 而不重新生成 `sincos_tbl`，会发生什么？

**答案**：混频输出的实际中频变成 6kHz，而数字本振表仍按 5kHz 生成。混频后解调出的不再是直流/固定相位分量，而是 1kHz 的残余差拍，累加结果会大幅衰减且相位随时间旋转，测得的 Γ 幅值偏低、读数漂移。[nanovna.h:33-34](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L33-L34) 的注释就是为了防止这种「改一头不改另一头」的事故。

**练习 2**：为什么用参考通道做分母，而不是直接用测量通道的绝对幅度？

**答案**：因为激励信号的幅度波动（si5351 输出电平随频率/频段变化、电缆损耗、混频器增益漂移）会同时出现在分子和分母上，复数除法后自动抵消，剩下的才是 DUT 的反射特性。这本质上是比值测量。而剩余的、不能被比值抵消的系统误差（直接性、源匹配、跟踪误差），正是后面校准模型要解决的（见 [nanovna.h:62-66](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L62-L66) 的五个 `ETERM_*` 注释）。

**练习 3**：`calculate_gamma()` 里为什么用 `rr = rs*rs + rc*rc`（模平方）而不是先开方再除？

**答案**：Cortex-M0 没有硬件除法/开方指令，开方是昂贵操作。复数除法 \( (a+bj)/(c+dj) \) 通过乘以共轭 \( (c-dj) \) 化为「乘法 + 除以 \( c^2+d^2 \)」，全程只需实数乘加，最后除以模平方一次即可。这是嵌入式 DSP 的常见手法。

---

### 4.2 `sweep()` 扫频循环：整台仪器的心脏

#### 4.2.1 概念说明

`sweep()` 是固件里唯一真正「测量」的函数，只有约 40 行，却把上一节的所有环节串成了一条流水线。它回答的问题是：**给定一张频点表 `frequencies[]`，如何在每个频点上得到 CH0 与 CH1 两路复数测量值，存进 `measured[]` 数组**。

理解它需要先明确三个数据结构（都在 `nanovna.h`）：

- `frequencies[]`（`current_props._frequencies`，别名宏见 [nanovna.h:399](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L399)）——长度为 `POINTS_COUNT`(101) 的频点表，由 `set_frequencies()` 预先算好（u3-l1 详述）。表的结尾用 0 填充，所以循环里可以用 `frequencies[i] == 0` 判断提前结束。
- `measured[2][POINTS_COUNT][2]`（[nanovna.h:40-41](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L40-L41)）——测量结果数组：第一维是通道（0 = 反射 CH0，1 = 传输 CH1），第二维是频点索引，第三维是复数的实部/虚部。**这个数组就是整台仪器的「测量本体」**，绘图、marker、shell 的 `data` 命令、Python 上位机，全都从它取数。
- `sweep_mode`（[nanovna.h:98-100](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L98-L100)）——线程间的控制标志：`SWEEP_ENABLE`(0x01) 表示连续扫描，`SWEEP_ONCE`(0x02) 表示只扫一次。

#### 4.2.2 核心流程

`sweep()` 对每个频点执行如下 9 步（括号内是源码注释里的耗时参考值，单位未注明）：

```text
对 i = 0 .. sweep_points-1:
  0. frequencies[i]==0 ?  → 是则 break（频点表用 0 结尾）
  1. set_frequency(frequencies[i])        (700)  设 si5351，返回需要的稳定延时
  2. tlv320aic3204_select(0)               (60)  切到 CH0 反射通道
  3. dsp_start(delay + (i==0 ? 1 : 0))   (1900)  武装采样：丢弃 delay 个缓冲再累计
  4. dsp_wait()                                  睡眠等待累计完成
  5. (*sample_func)(measured[0][i])        (60)  算 Γ，写入 CH0 结果
  6. tlv320aic3204_select(1)               (60)  切到 CH1 传输通道
  7. dsp_start(DELAY_CHANNEL_CHANGE)     (1700)  通道切换后固定丢 2 个缓冲
  8. dsp_wait()
  9. (*sample_func)(measured[1][i])        (60)  算 S21，写入 CH1 结果
  +. 若开启校准: apply_error_term_at(i)     (170)  逐点误差修正
  +. 若设了电延迟: apply_edelay_at(i)
  +. 若 UI 有操作请求且 break_on_operation: return false（让出 CPU 去响应）
```

几个容易被忽略但很关键的细节：

**「先丢再测」的延时设计**。`set_frequency()` 返回的 `delay` 不是随便等的：si5351 改频率后输出需要时间稳定，codec 的模拟前端路由切换也需要时间。`dsp_start(count)` 把 `wait_count` 设为 `count`，I2S 回调每来一个缓冲就减一，减到 1 才开始真正送入 `dsp_process()`。**这些缓冲不是被浪费了，而是被主动丢弃的脏数据**。`i == 0` 时多丢一个（`+1`），注释说是为了「对齐时序」——第一点还要等 LED 关断后的电源稳定（见 [main.c:860-862](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L860-L862) 的注释）。

**测量与 UI 的让步**。`break_on_operation` 参数决定了 `sweep()` 的两种用法：Thread1 用 `sweep(true)`——一旦触摸/拨轮置位了 `operation_requested`，立即 `return false` 放弃本次未完成的扫描去响应用户；而 shell 命令 `scan` 用 `sweep(false)`——命令式的一次性测量，必须完整跑完 101 点才返回（见 [main.c:926-927](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L926-L927) 先 `pause_sweep()` 再 `sweep(false)`）。返回值 `completed` 决定 Thread1 是否触发绘图。

**等待是睡眠不是空转**。`dsp_wait()` 里是 `while (accumerate_count > 0) __WFI();`——WFI（Wait For Interrupt）让 CPU 停到下一个中断来，I2S DMA 的缓冲完成中断会唤醒它。这是电池设备上典型的「事件驱动」写法。

#### 4.2.3 源码精读

完整函数（建议对照阅读原文件）：

[main.c:856-897](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L856-L897) 这是 `sweep()` 全文。开头 `palClearPad(GPIOC, GPIOC_LED)` 点亮 LED 表示「正在扫描」（扫描开始亮、[main.c:895](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L895) 结束时熄灭——所以真机上 LED 闪烁频率就等于扫描速率）；循环体严格按上面 9 步执行；源码里两处 `//====` 注释之间的空档是作者预留的「等待期间可以做的事」——目前为空，但这个结构说明作者有意识地把等待时间当作可复用资源（u2-l5 的实践会利用它做耗时统计）。

采样同步的三件套在循环体上游：

[main.c:614-627](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L614-L627) `dsp_start()` 做三件事：设 `wait_count`（要丢弃的缓冲数）、按当前 `bandwidth` 档位设 `accumerate_count`（要累计的缓冲数）、调用 `reset_dsp_accumerator()` 清零 dsp.c 的四个累加器。`dsp_wait()` 则是带 `__WFI()` 的忙等。配套的计数表在 [main.c:604-610](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L604-L610)：`bandwidth_accumerate_count[] = {1, 3, 10, 33, 100}`，对应带宽档 `{1000, 300, 100, 30, 10}` Hz——**带宽越窄，累计的缓冲越多，耗时越长，信噪比越高**。这是一个用时间换精度的旋钮（shell 命令 `bandwidth`，见 [main.c:1965-1980](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1965-L1980)）。

真正消费这两个计数的是 I2S DMA 回调：

[main.c:641-670](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L641-L670) `i2s_end_callback()` 由 DMA 半满/全满中断调用：`wait_count > 1` 时只递减（丢缓冲）；递减到最后一档且 `accumerate_count > 0` 时才把这段数据送进 `dsp_process()` 并递减累计计数。**`sweep()` 线程设初值、中断上下文递减、`dsp_wait()` 监视归零**——三个执行流靠两个 `volatile uint8_t` 协作，没有一个互斥锁。这是本固件并发设计（u1-l3 讲过的「无锁交接」）在数据采集侧的具体体现。

频率设置与增益联动：

[main.c:359-368](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L359-L368) `set_frequency()` 先调 `adjust_gain()` 再调 `si5351_set_frequency()`，把两段需要的延时加起来返回给 `sweep()` 用于 `dsp_start()`。[main.c:333-357](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L333-L357) 的 `gain_table[]` 按谐波次数给出 codec 增益：基波 0dB，谐波次数越高增益越大（2400MHz 以上 95dB）——因为谐波信号的能量弱得多，需要提前放大。跨越谐波阈值（默认 300MHz，`config.harmonic_freq_threshold`）时增益要变，所以返回 `DELAY_GAIN_CHANGE` 让 `sweep()` 多丢几个缓冲。

#### 4.2.4 代码实践

**实践目标**：用源码注释里的耗时参考值建立「一次扫描要多久」的定量模型，并搞清楚 `sweep(true)` 与 `sweep(false)` 的行为差异。

**操作步骤**：

1. 精读 [main.c:857-897](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L857-L897)，把 4.2.2 的 9 步与代码逐行对上号。
2. 写一个 host 端脚本 `sweep_timing.py`（示例代码）：

```python
# 示例代码：按 sweep() 源码注释里的耗时参考值建模
STEPS = [                # 每个频点的注释数字（单位源码未注明，仅相对参考）
    ('set_frequency',            700),
    ('select CH0',                60),
    ('dsp_start/wait CH0',      1900),
    ('sample_func CH0',           60),
    ('select CH1',                60),
    ('dsp_start/wait CH1',      1700),
    ('sample_func CH1',           60),
    ('cal + edelay',             170),
]
per_point = sum(v for _, v in STEPS)
POINTS = 101
print(f'每频点合计: {per_point}')
print(f'101 点合计: {per_point * POINTS}')
print(f'循环头注释值 5300 与每点合计的差: {5300 - per_point}（循环开销？）')
# 再叠加带宽档位的影响：bandwidth_accumerate_count[] = {1,3,10,33,100}
for bw, acc in zip([1000, 300, 100, 30, 10], [1, 3, 10, 33, 100]):
    print(f'bandwidth={bw:4d}Hz -> 每点累计 {acc} 个缓冲，扫描时长约放大 {acc} 倍（近似）')
```

3. 运行 `python3 sweep_timing.py`，记录输出。

**需要观察的现象**：每频点的注释数字加起来约 4710，与循环头注释的 5300 相差约 590；带宽从 1000Hz 降到 10Hz 时，累计次数从 1 变成 100。

**预期结果**：注释数字是作者手工标注的相对耗时参考（源码未注明单位，推测为微秒或系统节拍——**待确认**），不必执着于绝对值；重点是从模型里看出两个结论：(a) 每个频点的时间大头在两次 `dsp_start` 的等待（约 3600/4710 ≈ 76%），也就是「等数据采够」；(b) 带宽每降一档，扫描时间成倍增加。

**真机任务（可选，待本地验证）**：有硬件的读者可以在 USB 串口里执行 `scan 1000000 900000000 101` 并用秒表计时，再执行 `bandwidth 10` 后重复一次，对比两次耗时是否与 `accumerate_count` 的比值（1 → 100）大致相符。

#### 4.2.5 小练习与答案

**练习 1**：`sweep()` 为什么在 `operation_requested && break_on_operation` 时返回 `false` 而不是继续扫完？

**答案**：为了让 UI 保持响应。触摸和拨轮事件由中断/主线程置位 `operation_requested`（[nanovna.h:432-436](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L432-L436)），如果一次扫描要几百毫秒到几秒，扫完才处理会让操作感觉「卡死」。放弃未完成的扫描立即返回，代价只是这一帧数据不完整（`completed == false`，Thread1 因此不会触发 `plot_into_index` 绘图，见 [main.c:129-147](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L129-L147)），下一轮循环会重扫。

**练习 2**：`scan` 命令为什么先 `pause_sweep()` 再 `sweep(false)`？

**答案**：`scan` 在命令表里带着 `CMD_WAIT_MUTEX` 标志（[main.c:2177](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2177)），所以它并不是在 shell 线程里立即执行——shell 线程只把它登记成函数指针 `shell_function` 然后休眠（[main.c:2299-2304](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2299-L2304)），等 Thread1 在「上一轮循环收尾、下一轮扫描尚未开始」的空档里替它执行（[main.c:120-126](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L120-L126)）。也就是说 `cmd_scan` 与连续扫描本来就在同一个线程里串行执行，`pause_sweep()` 的作用不是防并发，而是让这台仪器在一次性测量完成后**停在暂停态**：否则 Thread1 回到循环顶部看到 `SWEEP_ENABLE` 仍置位，会立刻又开始连续扫描，把 PC 端还没读完的 `measured[]` 覆盖掉。至于 `sweep(false)`（不响应 UI 打断）则是保证这 101 点完整测完；测完还能用第 4 个参数 outmask 直接在命令内部回传数据，省去再发一条 `data` 命令的往返（[main.c:929-939](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L929-L939)）。

**练习 3**：为什么 `measured` 的第三维是长度 2 的 float 数组，而不是 C99 的 `float complex`？

**答案**：Cortex-M0 上 `float complex` 的运算会引入 `__mulsc3/__divsc3` 之类的库调用，且与 `float[2]` 的内存布局互操作更麻烦；更重要的是 `measured` 要被 `plot.c`（按 `[i][0]`/`[i][1]` 取实虚部）、shell 的 `data` 命令（用 `float (*array)[2]` 接住后逐点打印两个 float，见 [main.c:682-698](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L682-L698)，它还能用同一个指针无差别地读 `measured[sel]` 或 `cal_data[sel-2]`）和 flash 持久化（`_cal_data[5][POINTS_COUNT][2]` 同构）共用，朴素数组让这些边界都变成零成本内存拷贝。

---

### 4.3 `tlv320aic3204_select()`：用一个 I2C 命令切换被测通道

#### 4.3.1 概念说明

NanoVNA 只有一颗 codec，却要测两路信号（参考+测量是立体声两通道同时采，但 CH0 反射和 CH1 传输是**先后**测的）。解决方式非常朴素：**被测通道的模拟输入路由是可编程的，测 CH0 之前把「反射混频输出」接到 codec，测 CH1 之前换成「传输混频输出」**。

tlv320aic3204 是一颗可编程的音频编解码器，内部有多个输入引脚（IN1、IN3 等），可以通过 I2C 写寄存器把它们路由到 ADC。NanoVNA 用两份静态寄存器表描述两种路由，切换只是一次 3 对寄存器的 I2C 写入。这解释了 `sweep()` 里一个重要的时间成本来源：**切换通道后必须丢掉 2 个音频缓冲（`DELAY_CHANNEL_CHANGE`）再开始累计**，因为模拟路由刚切换过来的头几毫秒数据不可信。

#### 4.3.2 核心流程

```text
tlv320aic3204_select(0)                     tlv320aic3204_select(1)
        │                                           │
写 conf_data_ch3_select                     写 conf_data_ch1_select
  page 1                                     page 1
  IN3R -> RIGHT_P                            IN1R -> RIGHT_P
  IN3L -> RIGHT_N                            IN1L -> RIGHT_N
        │                                           │
codec 右声道 ADC 现在                         codec 右声道 ADC 现在
采样的是 CH0 反射混频输出                     采样的是 CH1 传输混频输出
```

注意两点：

- 两次切换**只改右声道（RIGHT）的路由**；左声道接的是参考信号，永远不动——这就是「参考连续可得、被测分时复用」的设计。
- 切换是纯寄存器操作，codec 不需要重新上电或重新初始化，所以代价小到只要几十微秒（源码注释 `// 60`），真正贵的是后面的稳定等待。

#### 4.3.3 源码精读

[tlv320aic3204.c:131-134](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/tlv320aic3204.c#L131-L134) `tlv320aic3204_select(int channel)` 整个函数只有一行：按 `channel` 是否为 0 选择两份静态表之一，交给 `tlv320aic3204_config()` 按「寄存器地址, 数据」成对地写 I2C。**用查表代替一串 if 判断寄存器值**，是嵌入式驱动里非常值得学习的写法。

两份路由表：

[tlv320aic3204.c:82-87](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/tlv320aic3204.c#L82-L87) `conf_data_ch3_select`：切到 page 1 后把 **IN3R/IN3L** 路由到右声道 ADC 的正/负输入端（10kΩ 阻抗）——这一路接的是反射（CH0）混频输出。

[tlv320aic3204.c:89-94](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/tlv320aic3204.c#L89-L94) `conf_data_ch1_select`：同样在 page 1，把 **IN1R/IN1L** 路由到右声道——这一路接的是传输（CH1）混频输出。两份表唯一的差别就是输入引脚编号。

寄存器写入的底层实现：

[tlv320aic3204.c:115-122](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/tlv320aic3204.c#L115-L122) `tlv320aic3204_config()` 先 `i2cAcquireBus()` 拿总线，然后按 `(reg, value)` 成对地调 `tlv320aic3204_bulk_write()`，最后释放总线。表以 `len` 计数、每次 `data += 2` 前进——所以表长度必须传 `sizeof(conf_data_ch3_select)/2`（对数不是字节数），`tlv320aic3204_select()` 里传的正是这个。

通道常量与延时的定义点：

[main.c:854](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L854) `#define DELAY_CHANNEL_CHANGE 2`——通道切换后丢弃的缓冲数。`sweep()` 里 `tlv320aic3204_select(0)` 与 `select(1)` 的调用点分别是 [main.c:866](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L866) 与 [main.c:875](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L875)，行尾注释直接写明 `CH0:REFLECT` 与 `CH1:TRANSMISSION`。此外 shell 还提供了手动切换命令：[main.c:1961-1962](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1961-L1962) 的 `port {0:TX 1:RX}` 命令直接调 `tlv320aic3204_select(port)`，调试硬件时用它把某一路固定接进来再看原始数据。

#### 4.3.4 代码实践

**实践目标**：通过 shell 命令与 `stat` 命令观察通道切换的真实效果，把「寄存器路由」和「数据变化」联系起来。（真机任务，待本地验证；无硬件读者完成第 1-2 步的源码阅读即可。）

**操作步骤**：

1. 阅读 [tlv320aic3204.c:82-94](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/tlv320aic3204.c#L82-L94)，确认两份表只差 IN1/IN3 两个引脚号；再读 [tlv320aic3204.c:131-134](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/tlv320aic3204.c#L131-L134) 确认 `channel ? ch1 : ch3` 的映射关系：**0 → IN3 → CH0 反射，1 → IN1 → CH1 传输**。
2. 画出时序图：以 `sweep()` 的一个频点为横轴，标出 `select(0) → dsp_start → dsp_wait → select(1) → dsp_start → dsp_wait` 五个事件，并标出每次 `dsp_start` 丢弃的缓冲数（第一次为 `delay`，第二次固定为 2）。
3. （真机）在 USB 串口执行：

```text
pause
port 0
stat
port 1
stat
resume
```

**需要观察的现象**：`stat` 命令（[main.c:1982-2011](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1982-L2011)）会打印左右声道的平均值与 RMS。`port 0` 与 `port 1` 之后右声道的 RMS 应当明显不同——因为右声道现在采的是不同的物理信号（反射混频输出 vs 传输混频输出），而左声道（参考）的读数应当基本不变。

**预期结果**：待本地验证。可以预判的定性结论是：CH0 口空载（开路）时反射接近全反射、右声道 RMS 较大；CH1 口没接东西时传输为零、RMS 接近噪底。左声道两次读数接近。

#### 4.3.5 小练习与答案

**练习 1**：既然左右声道是同时采样的，为什么不让左声道采参考、右声道同时采被测，一次就得到 Γ，而要先后切两次？

**答案**：事实上正是这样——左声道固定采参考，右声道采被测，一次 `dsp_wait()` 同时得到两路的 sin/cos 累加，`calculate_gamma()` 用它们做除法。**切换的不是「参考 vs 被测」，而是「被测的是反射还是传输」**：NanoVNA 的两个测量口（CH0 反射口、CH1 传输口）各有一个混频器，但只有一颗 codec 的一个被测输入，所以反射和传输必须分时复用，一个频点上先后各测一次。

**练习 2**：如果把 `DELAY_CHANNEL_CHANGE` 从 2 改成 0，会发生什么？

**答案**：通道切换后的头 1~2 个音频缓冲里，模拟路由尚未稳定（且可能还包含切换前一路的残余电荷），这些脏数据会被当成 CH1 的有效样本送进 `dsp_process()` 累加，导致传输系数的幅相出现系统性偏差、扫频曲线上 CH1 出现无规律的跳变。2 是经验下限，[si5351.c:43-44](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L43-L44) 里也有同风格的注释解释「频率变化的生效要等到下一次 dsp 测量，所以至少要跳过一个缓冲」。

**练习 3**：`tlv320aic3204_config()` 为什么要 `i2cAcquireBus()` / `i2cReleaseBus()`？

**答案**：I2C 总线上还挂着 si5351（同一组 `I2CD1`）。ChibiOS 的 `i2cAcquireBus()` 是总线互斥加锁，保证「写 3 对寄存器」这一整个事务不被另一个线程插入的 si5351 写入打断，否则两颗芯片的寄存器流会交错、各自收到错位的字节。

---

### 4.4 `sample_func`：采样函数指针——热路径上的可切换策略

#### 4.4.1 概念说明

`sweep()` 把「如何从 DSP 累加器得到一个测量值」抽象成了一个函数指针：

```c
static void (*sample_func)(float *gamma) = calculate_gamma;
```

所有候选函数都遵守同一个签名「把结果写进 `float gamma[2]`」。固件提供了三个实现：

| 函数 | 写入 `gamma[]` 的内容 | 用途 |
|---|---|---|
| `calculate_gamma` | \( \Gamma = \mathrm{samp} / \mathrm{ref} \)（复数比值） | **默认**，正常测量 |
| `fetch_amplitude` | 采样通道累加值 \( \times 10^{-9} \) | 调试：看被测通道原始幅度 |
| `fetch_amplitude_ref` | 参考通道累加值 \( \times 10^{-9} \) | 调试：看参考通道原始幅度 |

为什么用函数指针而不是在循环里写 `if (mode == 0) ... else ...`？两个原因：

1. **热路径开销**：`sweep()` 每个频点要调它两次、一次扫描 202 次、连续扫描下每秒上千次。间接调用是一次取址 + 跳转，而分支判断在流水线上的代价并不更小——但更重要的是编译器可以对「指向固定地址的函数指针」做更好的寄存器分配，代码也更短（这是 Cortex-M0 上 16KB RAM / 128KB Flash 的现实约束）。
2. **关注点分离**：`sweep()` 只关心「测到数、写进 `measured`」，至于这个数的物理含义（比值还是原始幅度）由外部决定。这让诊断模式（`sample ampl`）不用改动测量主循环一行代码。

这是「策略模式」在 C 里的最朴素形态，也是阅读这份固件时会反复见到的手法（`ui.c` 的菜单回调、shell 的命令表都是同一家族）。

#### 4.4.2 核心流程

```text
启动时:  sample_func = calculate_gamma        (静态初始化，默认正常测量)
                │
shell:  sample ampl   ──► cmd_sample() 把指针改成 fetch_amplitude
shell:  sample ref    ──► 改成 fetch_amplitude_ref
shell:  sample gamma  ──► 改回 calculate_gamma
                │
sweep() 每个频点:  (*sample_func)(measured[0][i]);  (*sample_func)(measured[1][i]);
                │
输出路径完全相同: 都写进 measured[]，都被绘图 / data 命令 / Python 端读取
（只是数值的物理含义变了）
```

#### 4.4.3 源码精读

指针定义与切换命令：

[main.c:764](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L764) `static void (*sample_func)(float *gamma) = calculate_gamma;`——定义即默认值，正常开机即为比值模式，不需要任何初始化代码。

[main.c:766-786](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L766-L786) `cmd_sample` 是 shell 命令 `sample {gamma|ampl|ref}` 的实现：用 `get_str_index()` 在字符串 `"gamma|ampl|ref"` 里查参数下标，然后把 `sample_func` 指向对应的函数。三个 `case` 各只有一行 `sample_func = xxx; return;`——**切换策略的成本就是一次赋值**。

三个候选实现（都在 dsp.c）：

[dsp.c:88-108](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L88-L108) `calculate_gamma()`：复数除法，4.1.3 已精读。注意 `#if 1/#elif 0/#else` 的条件编译块——被注释掉的两个分支分别直接输出采样通道或参考通道的原始累加，作者调试时靠改编译开关切换，后来才演化成运行时的 `sample_func` 机制（这是从这段 `#if` 残迹能读出的演化史）。

[dsp.c:110-122](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L110-L122) `fetch_amplitude()` 与 `fetch_amplitude_ref()`：分别把 `acc_samp_*` 或 `acc_ref_*` 乘以 \( 10^{-9} \ 后写进 `gamma[0..1]`。乘 \( 10^{-9} \) 是把 int32 累加量纲缩到 float 能舒适表示的范围（定点累加的满量程约 \( 2^{31} \)，除以 10⁹ 后约 2.1，正好落在 1 附近便于观察）。

调用点：

[main.c:873](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L873) 与 [main.c:882](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L882) `(*sample_func)(measured[0][i]);` 与 `(*sample_func)(measured[1][i]);`——注意传入的是 `measured[ch][i]`，类型 `float[2]` 自动退化为 `float*`，函数内部往 `[0]`/`[1]` 写实部与虚部。行尾注释分别写着 `calculate reflection coefficient` 与 `calculate transmission coefficient`。

函数原型的集中声明：

[nanovna.h:119-123](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L119-L123) dsp.c 的四个公开函数（`dsp_process`、`reset_dsp_accumerator`、`calculate_gamma`、`fetch_amplitude*`）在这里声明，`main.c` 靠这份声明把它们接进 `sample_func`——再次印证 u1-l1 的结论：`nanovna.h` 是全项目的接口契约。

#### 4.4.4 代码实践

**实践目标**：在 PC 上验证 `calculate_gamma()` 那两行复数除法公式的正确性——这是本讲唯一一处「纯数学」的源码，值得手工推一遍。

**操作步骤**：

1. 先手工展开：设复数约定为「实部 = cos 分量，虚部 = sin 分量」，即 \( S = S_c + jS_s \)、\( R = R_c + jR_s \)。证明：

\[
\frac{S}{R} = \frac{(S_c + jS_s)(R_c - jR_s)}{R_c^2 + R_s^2}
= \frac{S_c R_c + S_s R_s}{rr} + j\,\frac{S_s R_c - S_c R_s}{rr},
\quad rr = R_c^2 + R_s^2
\]

对照 [dsp.c:99-100](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L99-L100)，两行代码正是这个展开的逐项翻译。

2. 写 `verify_gamma.py`（示例代码）验证：

```python
# 示例代码：验证 calculate_gamma() 的复数除法展开
import cmath, random

def calculate_gamma(ss, sc, rs, rc):
    """逐行翻译 dsp.c:88-108（ss=sc 累加的 sin 分量, sc=cos 分量, ...）"""
    rr = rs * rs + rc * rc
    g0 = (sc * rc + ss * rs) / rr   # 对应 gamma[0]
    g1 = (ss * rc - sc * rs) / rr   # 对应 gamma[1]
    return g0, g1

random.seed(1)
for _ in range(5):
    # 模拟 dsp_process 累加出的四个量
    ss, sc = random.uniform(-1e4, 1e4), random.uniform(-1e4, 1e4)
    rs, rc = random.uniform(-1e4, 1e4), random.uniform(-1e4, 1e4)
    g0, g1 = calculate_gamma(ss, sc, rs, rc)
    ref = complex(sc, ss) / complex(rc, rs)   # 复数约定: 实部=cos 分量, 虚部=sin 分量
    assert abs(g0 - ref.real) < 1e-9 and abs(g1 - ref.imag) < 1e-9
    print(f'C 公式 ({g0:+.6f},{g1:+.6f})  复数除法 ({ref.real:+.6f},{ref.imag:+.6f})  一致')
print('全部一致：calculate_gamma() == (S_c + j*S_s) / (R_c + j*R_s)')
```

3. 运行 `python3 verify_gamma.py`。

**需要观察的现象**：5 组随机输入下，C 公式逐行翻译的结果与 Python 复数除法完全一致（误差在浮点舍入量级）。

**预期结果**：最后打印「全部一致」。特别地，当 `samp = 0.5 × ref`（同相）时输出应为 `(0.5, 0)`；`samp = 0.5j × ref` 时输出应为 `(0, 0.5)`——你可以额外加两行构造这两种情况验证，这正是 u2-l4 实践要用的判据。

**真机任务（可选，待本地验证）**：在串口执行 `sample ampl` 后 `scan 10000000 10000000 1`，再用 `data 0` 读出的数值将是原始幅度而不是 Γ；执行 `sample gamma` 恢复。

#### 4.4.5 小练习与答案

**练习 1**：`sample ampl` 模式下，`measured[]` 里存的还是反射系数吗？屏幕上的 Smith 圆图还有意义吗？

**答案**：不是。此时 `measured[ch][i][0..1]` 存的是采样通道 DSP 累加值乘 \( 10^{-9} \) 的原始幅度（实虚两路），不再是归一化的比值。Smith 圆图的坐标变换以 \( |\Gamma| \le 1 \) 为前提，原始幅度可以远大于 1，所以圆图上的点会跑到圆外、没有物理意义——这个模式只用于排查信号链（比如确认中频幅度是否合理），不用于读数。

**练习 2**：为什么 `fetch_amplitude` 要乘 `1e-9`？

**答案**：`dsp_process()` 的累加是 int32 定点（每样本乘以 Q15 本振再除以 16 后累加，见 [dsp.c:60-73](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L60-L73)），满量程接近 \( 2^{31} \approx 2.1\times10^9 \)。乘 \( 10^{-9} \) 把它缩到 1 上下的量级：一来 float（24bit 尾数）表示这个范围内的数没有精度损失，二来 `plot.c` 的坐标换算与 `data` 命令的输出都以 1 为参考量级更方便观察。

**练习 3**：如果要新增一个「只输出参考与采样幅度之比（标量）」的采样模式，需要改哪些地方？

**答案**：三处。(1) 在 `dsp.c` 新增一个同签名函数，比如 `fetch_amplitude_ratio(float *gamma)`，用 `sqrtf` 分别求两路模再相除，实部存比值、虚部清零；(2) 在 [nanovna.h:119-123](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L119-L123) 加原型；(3) 在 [main.c:766-786](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L766-L786) 的 `cmd_sample` 里给 `"gamma|ampl|ref"` 字符串追加一个名字并加一个 `case`。`sweep()` 一行都不用动——这正是函数指针抽象的收益。

---

## 5. 综合实践

**任务：写一个「软件 NanoVNA」——模拟一次 101 点扫描，把 Γ、|Γ|(dB)、VSWR 与最小 VSWR 搜索全部串起来。**

本任务把本讲的四个模块合成一条链：频点表（4.2 的 `frequencies[]`）→ 逐点算 Γ（4.1/4.4 的复数比值）→ 换算工程量（4.1 的 VSWR）→ 极值搜索（预告第 4 单元的 marker_search）。

1. **构造被测件**：用一个串联 RLC 回路当 DUT，\( Z_L(j\omega) = R + j(\omega L - \frac{1}{\omega C}) \)。取 R = 5Ω、L = 100nH、C ≈ 100pF（谐振频率约 50MHz，可在 30~70MHz 内扫描）。用 4.1.4 的 `gamma_of_load()` 逐点算 Γ。
2. **生成频点表**：仿照 `sweep()` 的循环结构，`frequencies[i]` 从 30MHz 均匀取到 70MHz 共 101 点（精确的整数误差扩散算法留给 u3-l1，这里用 numpy 即可）。
3. **逐点「测量」**：对每个频点调 `gamma_of_load(z_of_f(f))`，把结果存进一个 `measured[101][2]` 形状的数组——模拟 `sweep()` 往 `measured[0][i]` 写数的过程。
4. **换算与输出**：对每个点计算 \( |\Gamma| \)、\( 20\log_{10}|\Gamma| \)（dB）与 VSWR，用 matplotlib 画两条曲线（dB 曲线 + VSWR 曲线，双 y 轴），并在图上标注 VSWR 最小的频点。
5. **marker 预告**：用 `min(range(101), key=lambda i: vswr[i])` 找最小 VSWR 的索引——这就是第 4 单元 `marker_search()` 在 SWR 轨迹上要做的事的等价实现。

**预期结果**：dB 曲线在谐振频率处出现深陷波（\( |\Gamma| \) 最小、接近 -20dB 量级），VSWR 曲线在同一点降到接近 1；偏离谐振后 \( |\Gamma| \to 1 \)、VSWR 迅速升高。谐振点由 \( f_0 = \frac{1}{2\pi\sqrt{LC}} \) 可独立算出，应与搜索到的频点吻合。本实践全程在 PC 上完成。

**思考题（选做）**：把 R 改成 50Ω 再跑一次，曲线变成什么样？为什么？（答案：R = Z₀ 时回路在谐振点完美匹配，Γ 恒为 0，陷波消失，VSWR 恒为 1。）

## 6. 本讲小结

- **5kHz 频偏是整台仪器的架构支点**：CLK0 = f+5kHz、CLK1 = f（[si5351.c:379-385](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L379-L385)），把任意射频频点的测量都搬到固定音频中频上，使音频 codec + 软件 DSP 就能完成矢量测量；`FREQUENCY_OFFSET` 与 `sincos_tbl` 必须配套。
- **测量本体是 `measured[2][101][2]`**：通道 × 频点 × 实虚部；一切下游（绘图、marker、shell、Python）都从它取数。
- **`sweep()` = 逐频点的 9 步流水线**：设频率 → 切 CH0 → 丢缓冲并累计 → 算 Γ → 切 CH1 → 再测 → 误差修正；`break_on_operation` 让它能随时放弃扫描去响应 UI。
- **「先丢再测」是精度的保障**：`dsp_start(count)` 丢弃频率/增益/通道切换后的脏缓冲，`bandwidth_accumerate_count[] = {1,3,10,33,100}` 用时间换带宽。
- **通道切换是一次查表 I2C 写**：`tlv320aic3204_select()` 只改 codec 右声道的输入路由（IN3 = 反射 CH0，IN1 = 传输 CH1），参考通道固定在左声道、从不动。
- **`sample_func` 是热路径上的策略指针**：默认 `calculate_gamma`（复数除法 samp÷ref），可用 shell 命令切换到原始幅度诊断模式，`sweep()` 主循环对此无感知。

## 7. 下一步学习建议

本讲之后，你已经知道「一次测量在宏观上怎么跑」，但三个黑盒还没打开：

1. **u2-l2（si5351 信号源）**：`set_frequency()` 里那 700 个时间单位到底在等什么？三个频段、谐波模式、`mul/omul/rdiv` 的分频策略，是下一讲的主角。
2. **u2-l3（codec 与 I2S DMA）**：`AUDIO_BUFFER_LEN=96` 与 48kHz 采样率的关系、双缓冲 `rx_buffer` 的回调时机、增益表与谐波次数的联动。
3. **u2-l4（dsp.c 正交解调）**：`sincos_tbl` 这张 48 行的表是如何按 5kHz 生成的，多周期相干累积为什么等效于窄带带通滤波——本讲 4.4.4 的验证脚本已经为它铺好了路。

建议按 u2-l2 → u2-l3 → u2-l4 的顺序（数据流的物理顺序）继续，之后 u2-l5 会把 `sweep()` 放回 Thread1 的完整循环里，讲清楚它与 UI、shell 的并发协作。若你想先睹校准，可直接跳到 u3-l2，但需要先接受「Ed/Es/Er/Et/Ex 五个误差项」的模型设定。
