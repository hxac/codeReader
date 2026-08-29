# si5351 信号源：频段划分与谐波模式

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 si5351 内部「晶振 → PLL → Multisynth 分频器 → R 分频器 → CLKx 输出」的信号链，并说出每一步的频率范围约束。
2. 说出 NanoVNA 把频率划分成 1~100MHz / 100~150MHz / 150~300MHz 三个频段的原因，以及三个频段各自「固定什么、调节什么」的策略差异。
3. 理解超过 300MHz 后的谐波模式：`mul`/`omul` 与 `harmonic_freq_threshold`（`FREQ_HARMONICS`）的关系，为什么 CLK1 取 3/5/7/9 次谐波、CLK0 取 5/7/9/11 次谐波。
4. 读懂 `si5351_set_frequency()`、`si5351_get_band()` 以及底层寄存器写入与 I2C 传输代码。

本讲承接 u2-l1：上一讲我们已经知道 si5351 的 CLK0（本振）与 CLK1（激励）恒差 5kHz（`FREQUENCY_OFFSET`），把射频外差到音频中频。本讲深入这颗芯片内部，回答一个问题——**任意一个频点 `freq` 进来，固件到底往 si5351 写了什么，才能让两个输出恰好差 5kHz？**

## 2. 前置知识

### 2.1 锁相环（PLL）频率合成

PLL（Phase-Locked Loop，锁相环）的核心思想是用一个振荡器（VCO，压控振荡器）产生高频信号，再把输出分频后与参考时钟（晶振）鉴相，用相位差反馈控制 VCO。稳定后：

\[ f_{VCO} = f_{XTAL} \times \left(a + \frac{b}{c}\right) \]

其中 \(a + b/c\) 叫反馈分频比，可以是整数（整数模式，噪声最低）也可以是小数（小数模式，灵活但引入杂散）。

### 2.2 Multisynth 输出分频器

si5351 的每路输出还有一个可编程分频器（Silicon Labs 称为 Multisynth），把几百 MHz~1GHz 的 VCO 信号分频成最终输出：

\[ f_{OUT} = \frac{f_{VCO}}{(a + b/c) \times rdiv} \]

- 分频比 \(a + b/c\) 的合法范围约为 8~1800（`div == 4` 时走特殊的 Divide-by-4 通道）；
- \(rdiv\) 是额外的二级整数分频（1/2/4/…/128），用于产生很低的输出频率。

### 2.3 连分数逼近

小数分频比里的分母 \(c\) 要写进 20 位寄存器，上限 \(2^{20}-1 = 1048575\)。当需要的分母超过上限时，就要找一个非常接近原分数、但分母更小的分数——经典算法是**连分数展开**（continued fraction），Python 标准库 `fractions.Fraction.limit_denominator()` 用的就是这套思路，si5351.c 的注释里也直接给出了 CPython 的源码链接。

### 2.4 I2C 总线

两线制（SCL 时钟 + SDA 数据）串行总线，一主多从。每个从机有 7 位地址，主机向从机寄存器写数据的典型格式是：`[从机地址] [寄存器号] [数据1] [数据2] …`。si5351 在 NanoVNA 上的 I2C 地址是 `0x60`，挂在 `I2CD1` 上。

### 2.5 谐波（谐波模式的前置概念）

纯正弦波只有单一频率，但让信号通过非线性电路（放大器驱动到饱和等）就会产生 2 倍、3 倍、5 倍…频率的分量，称为谐波。谐波幅度随次数升高而衰减——这个物理事实后面会解释 `gain_table` 为什么随谐波次数增大增益。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [si5351.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c) | si5351 驱动主体：PLL/Multisynth 计算、频段策略、谐波模式、寄存器写入 |
| [si5351.h](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.h) | si5351 寄存器号与寄存器位段的宏定义（数据手册的 C 语言翻译） |
| [main.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c) | 调用方：`set_frequency()` 包装函数、`sweep()` 主循环、`threshold`/`offset`/`power`/`freq` 等 shell 命令 |
| [nanovna.h](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h) | `FREQUENCY_OFFSET`（5kHz）常量与 `harmonic_freq_threshold` 配置字段 |

三个输出的分工（si5351.c 文件头注释明确写出）：

- **CLK0**：本振，输出 `frequency + offset`（带 5kHz 频偏）；
- **CLK1**：激励，输出 `frequency`；
- **CLK2**：固定 8MHz，作为音频 codec tlv320aic3204 的 MCLK（见 [tlv320aic3204.c:L33](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/tlv320aic3204.c#L33) 中 codec 时钟源配置为 "PLL Clock High, MCLK, PLL"）。

## 4. 核心概念与源码讲解

### 4.1 si5351 芯片架构：两级合成与 P1/P2/P3 寄存器

#### 4.1.1 概念说明

si5351 是一颗「任意频率时钟发生器」：输入一颗 26MHz 晶振，内部有 **两个独立的 PLL（PLLA/PLLB）**，每个 PLL 后面挂 **多路 Multisynth 输出分频器**（MS0/MS1/MS2 分别对应 CLK0/CLK1/CLK2）。

NanoVNA 需要两路「恒差 5kHz」的输出，所以让 CLK0 走 PLLA、CLK1 走 PLLB（在 100MHz 以上频段），两路各自独立调 PLL，却仍能精确保持 5kHz 差值——这是理解后文一切策略的基础。

芯片硬件约束（来自数据手册，代码注释里也有体现）：

- PLL（VCO）频率范围约 400~1600MHz，推荐 600~900MHz；
- Multisynth 常规分频比 8~1800，输出上限约 100~128MHz；`div == 4` 时走 Divide-by-4 专用通道，输出可达 200~300MHz；
- 小数分母 P3 是 20 位寄存器。

#### 4.1.2 核心流程

一个输出频率的合成路径：

```
26MHz 晶振 ──► PLLA/PLLB（反馈分频 a+b/c，VCO = 26M×(a+b/c)）
                      │
                      ▼
              Multisynth 分频（div+num/denom，再经 rdiv）
                      │
                      ▼
              CLK0 / CLK1 / CLK2 引脚输出
```

固件的两种「搭配手法」（对应三个频段的策略）：

1. **固定 PLL、调分频器**（`si5351_set_frequency_fixedpll`）：VCO 钉死在一个整数倍频率（26M×32=832MHz），只改输出分频比。优点：PLL 始终整数模式、杂散小、换频点时 PLL 不动、切换快。
2. **固定分频器、调 PLL**（`si5351_setupPLL_freq`）：输出分频比钉死（6 或 4），改 PLL 频率凑出目标。优点：分频器可为整数模式，输出频率高时这是唯一可行结构。

#### 4.1.3 源码精读

**PLL 配置**。[si5351.c:L177-L214](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L177-L214) 的 `si5351_setupPLL()` 把分频比 \(a + b/c\) 编码成三个寄存器值（公式在代码注释 L182-L190 中给出）：

\[ P1 = 128a + \left\lfloor \frac{128b}{c} \right\rfloor - 512,\quad P2 = (128b) \bmod c,\quad P3 = c \]

对应源码关键片段（整数模式时 `P1 = mult - 512`，小数模式再叠加 `num/denom` 项）：

```c
mult <<= 7;                  // 即 mult * 128
num <<= 7;                   // 即 num * 128
uint32_t P1 = mult - 512;    // Integer mode
uint32_t P2 = 0;
uint32_t P3 = 1;
if (num) {                   // Fractional mode
  P1 += num / denom;
  P2 = num % denom;
  P3 = denom;
}
```

随后把 P1/P2/P3 按数据手册的位域拆进 9 字节寄存器序列，一次 `si5351_bulk_write(reg, 9)` 写入（L203-L213）。

**Multisynth 配置**。[si5351.c:L217-L278](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L217-L278) 的 `si5351_setupMultisynth()` 用完全相同的 P1/P2/P3 公式，但多两个分支：

- [L238-L239](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L238-L239)：`if (div == 4) rdiv |= SI5351_DIVBY4;` —— 分频比为 4 时走 Divide-by-4 专用模式，此时 P1/P2/P3 全部为 0（150~300MHz 高频输出全靠这条路）；
- [L263-L266](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L263-L266)：小数部分 `num == 0` 时给 CLKX_CONTROL 寄存器置 `SI5351_CLK_INTEGER_MODE` 位，告知芯片分频器工作在整数模式（更低的抖动）。

**连分数逼近**。[si5351.c:L281-L302](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L281-L302) 的 `approximate_fraction()`：当分母超过 `MAX_DENOMINATOR`（\(2^{20}-1\)）时，用辗转相除（欧几里得展开即连分数）逐步构造分子/分母，直到分母将超限为止，返回「分母不超过上限意义下最接近」的分数。注释 L284 直接指向 CPython `fractions.py` 的同类实现。

**两个「手法」的封装**：

- [si5351.c:L305-L313](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L305-L313) `si5351_set_frequency_fixedpll()`：给定固定 `pllfreq` 与目标 `freq`，算 `div = pllfreq/freq`、`num = pllfreq%freq`（注意：这里直接拿 `freq` 当分母，所以经常超过 20 位上限，必须做逼近），写入 Multisynth。
- [si5351.c:L316-L325](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L316-L325) `si5351_setupPLL_freq()`：反方向——分频比固定为 `div`，反解 PLL 所需频率 \(f_{PLL} = freq \times div\)，再以 `XTALFREQ * mul` 为分母把 PLL 反馈分频比写成 \(freq \times div / (26M \times mul)\)。

#### 4.1.4 代码实践

**实践：用 Python 复现 P1/P2/P3 编码，算出 10MHz 频点的真实寄存器值。**

1. 实践目标：亲手验证「832MHz 固定 PLL ÷ 83.2 = 10MHz」是如何变成寄存器数值的。
2. 操作步骤：在 PC 上新建 `pll_calc.py`（示例代码，非项目源码）：

```python
# 示例代码：复现 si5351_setupPLL / si5351_setupMultisynth 的 P1/P2/P3 计算
def p_values(a, b, c):
    # P1 = 128a + int(128b/c) - 512, P2 = (128b) % c, P3 = c
    if b == 0:
        return 128*a - 512, 0, 1
    return 128*a - 512 + (128*b)//c, (128*b) % c, c

XTAL, PLL_N = 26_000_000, 32
pll = XTAL * PLL_N            # 832 MHz，band 1 的固定 PLL
freq = 10_000_000
div, num = pll // freq, pll % freq     # 83, 2_000_000 → 83 + 2/10 = 83.2
print("div+num/denom =", div, num, "/", freq)
print("P1,P2,P3 =", p_values(div, num, freq))
```

3. 需要观察的现象：`div=83, num=2000000, denom=10000000`，即分频比 83.2；分母 10,000,000 已超过 20 位上限 1,048,575，正好是 `approximate_fraction()` 的用武之地。
4. 预期结果：`P1,P2,P3 = (10137, 3, 5)`。手工验证：83.2 = 83 + 1/5（连分数约分 2/10 → 1/5），代入公式 \(P1 = 128\times83 + \lfloor 128/5 \rfloor - 512 = 10624 + 25 - 512 = 10137\)，\(P2 = 128 \bmod 5 = 3\)，\(P3 = 5\)。可在本机运行核对（数学推导确定，具体数值可本地验证）。
5. 进阶：把 `freq` 换成 10,000,001Hz，此时分频比是无理逼近不精确的，观察逼近后 P3 的大小，并思考由此引入的频率误差量级。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `si5351_setupPLL()` 里要先 `mult <<= 7; num <<= 7;`？
**答案**：左移 7 即乘 128，把公式里的 \(128a\) 与 \(128b\) 一次算好；之后整数模式的 `P1 = mult - 512` 与小数模式的 `P1 += num/denom` 都建立在「已乘 128」的量纲上，避免重复书写乘法。

**练习 2**：`si5351_setupMultisynth()` 中 `div == 4` 时为什么完全不算 P1/P2/P3？
**答案**：Divide-by-4 是 Multisynth 的专用硬件模式，由寄存器位 `SI5351_DIVBY4`（`3<<2`，见 [si5351.h:L52](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.h#L52)）直接选择，不需要小数分频参数；该模式输出频率可以到 200~300MHz，这是普通 8~1800 分频通道做不到的。

**练习 3**：band 1 若不用 `rdiv`，直接让 832MHz PLL 分频出 50kHz，分频比是多少？合法吗？
**答案**：\(832\,000\,000 / 50\,000 = 16640\)，远超 Multisynth 上限 1800，不合法。所以固件先把目标频率乘 64（移位 6 位）到 3.2MHz，分频比 260 合法，再在输出端用 R 分频器 /64 还原（见 4.3.3 节）。

### 4.2 si5351_get_band：三个频段的策略差异

#### 4.2.1 概念说明

`si5351_get_band()` 按频率把输出分成三个「频段」（band）。频段不是随便划的，每一段对应一种「PLL 与分频器谁固定、谁可调」的搭配：

- **Band 1（1~100MHz）**：固定 PLL = 晶振×32 = 832MHz（整数模式），用**小数 Multisynth 分频**凑任意频点。换频点时 PLL 完全不动，只有分频器变，所以扫频时相位噪声表现好、切换最快。
- **Band 2（100~150MHz）**：Multisynth 分频比**固定为 6**，用小数 PLL（600~900MHz）凑频点。
- **Band 3（150~300MHz）**：Multisynth 走 **Divide-by-4**，分频比**固定为 4**，小数 PLL 范围 600~1200MHz。

为什么 100MHz 是 band 1/2 的分界？因为 832MHz PLL 分频出 >100MHz 需要 <8.32 的分频比，触碰 Multisynth 下限 8；而 Divide-by4/6 的固定分频恰好把 PLL 推回 600MHz 以上的推荐区间。

#### 4.2.2 核心流程

```text
输入 freq ──► si5351_get_band(freq)
                ├── freq < 100MHz  ──► band 1（固定 PLL 832M，调 MS 小数分频）
                ├── freq < 150MHz  ──► band 2（固定 MS 分频 6，调 PLL）
                └── 其他           ──► band 3（固定 MS 分频 4 = DIVBY4，调 PLL）
```

频段与谐波模式的完整覆盖表（摘自源码注释，TH = `harmonic_freq_threshold` 默认 300MHz）：

| 输出范围 | 模式 | CLK1 谐波 : CLK0 谐波 | CLK1 基频 f | CLK0 基频 of | band |
|---|---|---|---|---|---|
| 50kHz ~ 100MHz | 直通 ×1 | x1 : x1 | 50k~100M | 50k~100M | 1 |
| 100 ~ 150MHz | 直通 ×1 | x1 : x1 | 100~150M | 100~150M | 2 |
| 150 ~ 300MHz | 直通 ×1 | x1 : x1 | 150~300M | 150~300M | 3 |
| 300 ~ 450MHz | 谐波 | x3 : x5 | 100~150M | 60~90M | 2 |
| 450 ~ 900MHz | 谐波 | x3 : x5 | 150~300M | 90~180M | 3 |
| 900 ~ 1500MHz | 谐波 | x5 : x7 | 180~300M | 128~215M | 3 |
| 1500 ~ 2100MHz | 谐波 | x7 : x9 | 214~300M | 166~234M | 3 |
| 2100 ~ 2700MHz | 谐波 | x9 : x11 | 233~300M | 190~246M | 3 |

（对应 [si5351.c:L359-L369](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L359-L369) 的 ASCII 注释表；f = freq/mul 是 CLK1 基频，of = ofreq/omul 是 CLK0 基频。）

#### 4.2.3 源码精读

**频段判断函数**。[si5351.c:L371-L377](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L371-L377)：

```c
static inline uint8_t
si5351_get_band(uint32_t freq)
{
  if (freq < 100000000U) return 1;
  if (freq < 150000000U) return 2;
  return 3;
}
```

只有三个比较，极其简单。注意它接收的 `freq` 是**已经除以谐波次数 mul 之后的基频**（见 4.3.3 节的调用点 `band = si5351_get_band(freq / mul)`），所以「400MHz 的测量频率」实际按 133MHz 判断成 band 2。

**band 1 分支**。[si5351.c:L425-L443](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L425-L443)：仅在频段刚切换（`current_band != 1`）时才配置 PLLA 为固定 832MHz 并把 CLK2 设成 8MHz；平时只更新 CLK0/CLK1 两个通道的 Multisynth 分频器。CLK0 的等效 PLL 是 `omul × 832MHz`（谐波模式下等效到基频的 omul 倍），CLK1 是 `mul × 832MHz`：

```c
if (current_band != 1) {
  si5351_setupPLL(SI5351_REG_PLL_A, PLL_N, 0, 1);        // PLLA = 26M*32 = 832M
  si5351_set_frequency_fixedpll(2, XTALFREQ*PLL_N, CLK2_FREQUENCY, ...); // CLK2 = 8M
  delay = DELAY_BANDCHANGE_1;
} else {
  delay = DELAY_BAND_1;
}
si5351_set_frequency_fixedpll(0, (uint64_t)omul * XTALFREQ * PLL_N, ofreq, rdiv, ...); // CLK0
si5351_set_frequency_fixedpll(1, (uint64_t)mul  * XTALFREQ * PLL_N, freq,  rdiv, ...); // CLK1
```

**band 2/3 分支**。[si5351.c:L444-L466](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L444-L466)：结构对偶——固定的是 Multisynth 分频器（band 2 用 6、band 3 用 4），每个频点更新两个 PLL。CLK0 用 PLLA、CLK1 用 PLLB：

```c
fdiv = (band == 2) ? 6 : 4;
if (current_band != band) {          // 频段切换时才写固定分频器
  si5351_setupMultisynth(0, fdiv, 0, 1, SI5351_R_DIV_1, drive_strength | SI5351_CLK_PLL_SELECT_A);
  si5351_setupMultisynth(1, fdiv, 0, 1, SI5351_R_DIV_1, drive_strength | SI5351_CLK_PLL_SELECT_B);
  delay = DELAY_BANDCHANGE_2;
}
si5351_setupPLL_freq(SI5351_REG_PLL_A, ofreq, fdiv, omul); // PLLA = (ofreq/omul)*fdiv
si5351_setupPLL_freq(SI5351_REG_PLL_B, freq,  fdiv, mul);  // PLLB = (freq/mul)*fdiv
```

值得注意的细节是 CLK2 的处理（[L462-L465](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L462-L465)）：此时 CLK2 改从 PLLB 取源，传入的目标频率是 `CLK2_FREQUENCY * mul`——因为 PLLB 实际是基频的 fdiv 倍（`(freq/mul)×fdiv`），除以 `8MHz×mul` 恰好得到与 CLK1 一致缩放的分频比，输出仍是 8MHz。这种「按同一比例缩放分子分母」的技巧让固定 8MHz 输出在谐波模式下无需额外 PLL。

#### 4.2.4 代码实践

**实践：手工填写频段策略表，验证三个代表频点。**

1. 实践目标：用纸笔（或 Python）验证三个频段的「PLL 频率」与「分频比」都落在合法区间。
2. 操作步骤：
   - 对 10MHz：band 1，PLL=832MHz，Multisynth 分频比 = 832/10 = 83.2（8~1800 内 ✓）；
   - 对 120MHz：band 2，MS 固定 6，PLLB = 120×6 = 720MHz（600~900 推荐区间内 ✓）；
   - 对 200MHz：band 3，MS 固定 4（DIVBY4），PLLB = 200×4 = 800MHz（✓）。
3. 需要观察的现象：三行的 PLL 值是否都落在 600~900MHz、分频比是否都在合法范围。
4. 预期结果：如上所示，三个频点全部满足约束；band 1 的 PLL 永远是 832MHz，band 2/3 的 PLL 随频点滑动。
5. 「待本地验证」项：若想实测，可在真机上用 `scan` 命令分别扫 10M/120M/200M 附近观察锁定情况（无硬件可跳过，纯计算已可验证）。

#### 4.2.5 小练习与答案

**练习 1**：band 1 的策略为什么对扫频最友好？
**答案**：扫频时每个频点只写 CLK0/CLK1 的 Multisynth 寄存器，PLLA/PLLB 完全不动。PLL 重锁需要微秒级稳定时间，而分频器切换几乎瞬时，所以 band 1 内部扫频最快、相位连续性最好。

**练习 2**：band 2 用固定分频 6 而不是 5 或 7，可能的原因是什么？
**答案**：分频 6 把 100~150MHz 映射到 PLL 600~900MHz，恰好是 si5351 数据手册推荐的 VCO 工作区间，兼顾低端（100M×6=600M）与高端（150M×6=900M）都不过界；同时 6 是偶整数分频，避免了非 DIVBY4 特殊模式下「分频比 <8」的非法区。

**练习 3**：`si5351_get_band()` 为什么是 `static inline` 而且只有一个调用点？
**答案**：它只被 `si5351_set_frequency()` 调用一次，`inline` 让编译器把三次比较直接内联进主流程，省去函数调用开销——在每频点都要执行的路径上，这类微优化在 Cortex-M0 上有意义。

### 4.3 si5351_set_frequency：谐波模式、mul/omul 与低频 rdiv

#### 4.3.1 概念说明

si5351 的单端输出最高约 300MHz，但 NanoVNA 声称可以测到 2.7GHz——靠的就是**谐波模式**：

- CLK1 输出基频 \(freq/mul\)，激励通路中的非线性环节产生它的 \(mul\) 次谐波，谐波频率恰为 \(freq\)，作为真正的测试信号；
- CLK0 输出基频 \((freq+5000)/omul\) 的 \(omul\) 次谐波作为本振，与测试信号混频后仍得 5kHz 中频——**外差架构在谐波模式下依然成立**。

`mul` 与 `omul` 取 3/5、5/7、7/9、9/11（CLK0 恒比 CLK1 高两档）而不是相同值，是为了让两路输出基频错开、都落在 100~300MHz 这一输出性能最好的区间（对照 4.2.2 表格的 f 与 of 两行）。

判断阈值 `config.harmonic_freq_threshold` 默认 300MHz（[main.c:L797](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L797)），在 main.c 里被宏封装成：

- [main.c:L82](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L82)：`#define FREQ_HARMONICS (config.harmonic_freq_threshold)`
- [main.c:L83](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L83)：`#define IS_HARMONIC_MODE(f) ((f) > FREQ_HARMONICS)`

最大测量频率按源码注释（[si5351.c:L379-L380](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L379-L380)「Maximum supported frequency = FREQ_HARMONICS * 9U」）为阈值 × 9，默认即 300M×9 = 2.7GHz——其物理本质是最高谐波档 mul=9 下基频不能超过 300MHz（\(freq/9 \le 300\,\text{MHz}\)），两个说法在默认阈值下恰好相等。

#### 4.3.2 核心流程

`si5351_set_frequency(freq, drive_strength)` 的决策序列：

```text
freq（测量频率）
  │
  ├─ freq == current_freq ? ──是──► 直接返回（缓存命中）
  ├─ current_freq > freq ?  ──是──► current_band = 0（扫频回扫，强制重配频段）
  │
  ├─ freq >= TH*7 ?  → mul=9,  omul=11   （2100~2700MHz）
  ├─ freq >= TH*5 ?  → mul=7,  omul=9    （1500~2100MHz）
  ├─ freq >= TH*3 ?  → mul=5,  omul=7    （900~1500MHz）
  ├─ freq >= TH   ?  → mul=3,  omul=5    （300~900MHz）
  ├─ freq <= 500k ?  → rdiv=64，freq/ofreq 左移 6 位
  ├─ freq <= 4M   ?  → rdiv=8， freq/ofreq 左移 3 位
  │                   （以上两条互斥，低频扩展；其余 rdiv=1）
  │
  ├─ band = si5351_get_band(freq / mul)   ← 注意用基频判段
  ├─ band==1：固定 PLLA=832M，写 CLK0/CLK1 分频器（CLK0 用 omul 等效 PLL）
  ├─ band==2/3：固定 MS 分频 6/4，写 PLLA/PLLB
  │
  └─ 频段变化 ? → 延时 5000µs 后复位两个 PLL（si5351_reset_pll）
  返回 delay（告诉调用方要丢弃几个 DSP 缓冲周期）
```

#### 4.3.3 源码精读

**入口与缓存**。[si5351.c:L386-L401](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L386-L401)：

```c
if (freq == current_freq)
  return delay;                       // 同频直接返回 DELAY_NORMAL
else if (current_freq > freq)
  current_band = 0;                   // 回扫时作废频段缓存
current_freq = freq;
uint32_t ofreq = freq + current_offset;   // CLK0 目标 = freq + 5kHz（默认）
```

`current_offset` 初值为 `FREQUENCY_OFFSET`（5000，[nanovna.h:L34](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L34)）。回扫重置 `current_band = 0` 的注释（L393）解释得很清楚：扫 150~600MHz 时终点回起点，600MHz 是 band 2/3，若不重置，回到 150MHz 时会误认为还在旧频段。

**谐波与低频的六级选择**。[si5351.c:L403-L423](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L403-L423)：

```c
if (freq >= config.harmonic_freq_threshold * 7U) { mul = 9;  omul = 11; }
else if (freq >= config.harmonic_freq_threshold * 5U) { mul = 7;  omul = 9; }
else if (freq >= config.harmonic_freq_threshold * 3U) { mul = 5;  omul = 7; }
else if (freq >= config.harmonic_freq_threshold)      { mul = 3;  omul = 5; }
else if (freq <= 500000U) { rdiv = SI5351_R_DIV_64;  freq <<= 6;  ofreq <<= 6; }
else if (freq <= 4000000U){ rdiv = SI5351_R_DIV_8;   freq <<= 3;  ofreq <<= 3; }
```

三个要点：

1. 谐波分支与低频分支**互斥**（`else if` 链）：谐波模式只发生在 ≥300MHz，低频扩展只发生在 ≤4MHz，中间 4M~300M 全是直通。
2. 低频移位时 `freq` 与 `ofreq` **同时**左移，两路输出再经相同的 R 分频器，最终输出差值仍是精确的 5000Hz——5kHz 中频架构在低频段不被破坏。
3. `band = si5351_get_band(freq / mul)`（[L424](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L424)）：判段用基频（移位后的 `freq` 除以谐波次数），不是测量频率。

**频段切换时的 PLL 复位**。[si5351.c:L468-L471](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L468-L471)：

```c
if (current_band != band) {
  si5351_reset_pll(SI5351_PLL_RESET_A|SI5351_PLL_RESET_B);
  current_band = band;
}
```

`si5351_reset_pll()`（[L152-L159](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L152-L159)）先睡 `DELAY_RESET_PLL`（5000µs，L51-L52 的注释解释：band 1 上少于 900µs 不稳定，4000~5000µs 则换频时无幅度毛刺）再写寄存器 177 的自清零复位位。

**返回值 delay 的用途**。`sweep()` 中（[main.c:L865-L867](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L865-L867)）：

```c
delay = set_frequency(frequencies[i]);   // 700
tlv320aic3204_select(0);
dsp_start(delay + ((i == 0) ? 1 : 0));   // 丢弃 delay 个音频缓冲后再采样
```

频率刚写入时 PLL 还在稳定、电路还在过渡，此期间采到的数据是「脏」的。`set_frequency()` 返回需要丢弃的缓冲周期数（普通 2、band 1 内 3、频段切换 3，见 [L43-L50](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L43-L50) 的 `DELAY_*` 常量），`dsp_start()` 据此跳过——这正是 u2-l1 讲过的「先丢再测」策略在驱动层的落点。

**上层包装 set_frequency()**。[main.c:L359-L368](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L359-L368)：在调用 si5351 之前先 `adjust_gain()`，且当 `drive_strength == DRIVE_STRENGTH_AUTO`（-1，[main.c:L81](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L81)）时按是否进入谐波模式自动选 8mA/2mA 驱动强度（谐波信号弱，需要更强的基波驱动）。`adjust_gain()`（[main.c:L347-L357](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L347-L357)）用 `newfreq / FREQ_HARMONICS` 即**谐波次数**去索引 [gain_table](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L333-L343)（0/40/50/75/85/95…），谐波越高 codec 增益越大——这从侧面印证了「谐波次数越高信号越弱」的物理事实。

#### 4.3.4 代码实践

**实践：跟踪 3 个频率的 mul/omul 选择，并玩转 shell 命令。**

1. 实践目标：把六级 `if-else` 链在脑中跑通，并了解可通过 shell 现场改变哪些参数。
2. 操作步骤：
   - 源码阅读（无硬件也可完成）：分别代入 freq = 350MHz、1.2GHz、2.5GHz，写出每档的 `mul`/`omul`、基频 `freq/mul`、band；
   - 有真机时：`freq 350000000`（[main.c:L494-L506](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L494-L506)）设置单频点；`power -1` 恢复自动驱动强度（[main.c:L508-L516](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L508-L516)）；`threshold` 查询/修改谐波阈值（[main.c:L545-L555](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L545-L555)）；`offset 5000` 重设频偏（[main.c:L485-L492](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L485-L492)）。
3. 需要观察的现象（源码阅读部分）：350M → mul=3/omul=5，基频 116.7MHz，band 2；1.2G → mul=5/omul=7，基频 240MHz，band 3；2.5G ≥ TH×7=2100M → mul=9/omul=11，基频约 277.8MHz，band 3（注意不能落在 mul=7 档：2500/7≈357MHz 超出 300MHz 的基频上限）。
4. 预期结果：350M→(3,5,band2)；1.2G→(5,7,band3)；2.5G→(9,11,band3)。用 `threshold` 把阈值改成 200000000 后再推演 350MHz：仍不满足 ≥3×TH（600MHz），mul 保持 3 不变；但 700MHz 会从 mul=3 变为 mul=5（700 ≥ 3×200M）。再试 `threshold 100000000`：2.6GHz 仍落在 mul=9 档（≥ 7×100M）且基频 288.9MHz 合法——判断链的最高档就是 mul=9，硬上限由「基频 ≤300MHz」即 2.7GHz 决定，与 TH 无关；源码注释「Maximum supported frequency = FREQ_HARMONICS * 9U」在默认 TH=300M 下与它恰好相等。
5. 「待本地验证」项：shell 命令效果需真机验证；推理部分可离线完成。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `omul` 总比 `mul` 大 2？
**答案**：CLK0 输出 `(freq+5000)/omul` 的 omul 次谐波。若 omul == mul，两路基频几乎相同（只差 5kHz/mul），都挤在同一个狭窄频点附近；取 omul = mul+2 让 CLK0 基频比 CLK1 低一档（对照表中 of < f），两路都落在 60~300MHz 的良性能区间，同时保持各自谐波频率只差 5kHz。

**练习 2**：`si5351_set_frequency()` 为什么在 `current_freq > freq` 时把 `current_band` 清零，而不是 `!=` 时？
**答案**：正常单向扫频时频率递增，band 只会向前跳变，靠 `current_band != band` 就能检测切换；但扫完回扫（从高频回起点）时频率递减，可能出现「新 band 与缓存相同但 PLL 参数族已不同」的组合（注释举的例子：150-600MHz 扫描，600MHz 处 band 2/3，回头 150MHz 仍会命中 band 2 判断），故在检测到频率回退时干脆作废缓存、强制完整重配。

**练习 3**：`si5351_set_frequency_offset()`（[si5351.c:L59-L63](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L59-L63)）为什么要把 `current_freq` 清零？
**答案**：offset 改变后，即使下一个测量频率与之前相同，CLK0 的目标频率也变了。清零 `current_freq` 让下次调用不再命中 `freq == current_freq` 的缓存分支，强制重新计算并写入所有寄存器。

### 4.4 寄存器写入与 I2C 传输

#### 4.4.1 概念说明

前面所有「计算」最终都要变成寄存器写操作。这一层回答三个问题：

1. **怎么写**：si5351 挂在 I2CD1 上，地址 0x60；每次传输格式为 `[寄存器号, 数据…]`。
2. **初始化写了什么**：`si5351_configs[]` 是一张「长度前缀」压缩表，上电时按序回放。
3. **怎么省**：I2C 一次传输几十微秒，扫频热路径上能不写就不写——`current_freq` 缓存、`current_band` 缓存、CLKX_CONTROL 寄存器缓存（`USE_CLK_CONTROL_CACHE`）三重手段。

#### 4.4.2 核心流程

```text
si5351_write(reg, dat) ──组 2 字节 buf──►
si5351_bulk_write(buf, len) ──► i2cAcquireBus(I2CD1)
                                   i2cMasterTransmitTimeout(&I2CD1, 0x60, buf, len, NULL, 0, 1000ms)
                                   i2cReleaseBus(I2CD1)
```

初始化时 `si5351_init()` 扫描 `si5351_configs[]`：每项先读一个长度字节 `len`，随后 `len` 个字节作为一次批量写发出，直到遇到 0 哨兵。

#### 4.4.3 源码精读

**I2C 传输**。[si5351.c:L65-L71](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L65-L71)：

```c
static void
si5351_bulk_write(const uint8_t *buf, int len)
{
  i2cAcquireBus(&I2CD1);
  (void)i2cMasterTransmitTimeout(&I2CD1, SI5351_I2C_ADDR, buf, len, NULL, 0, 1000);
  i2cReleaseBus(&I2CD1);
}
```

`i2cAcquireBus/i2cReleaseBus` 是 ChibiOS 的总线互斥封装（虽然 NanoVNA 当前只有 shell 线程会并发碰 I2C，养成习惯仍是对的）；超时 1000ms 防止硬件异常卡死调度。`SI5351_I2C_ADDR` 为 `0x60`（[L36-L37](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L36-L37) 注释说明：Si5351A 10-pin MSOP 封装只有这一个地址）。单寄存器写入是它的两字节特例（[L95-L100](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L95-L100)）。

**初始化寄存器表**。[si5351.c:L103-L120](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L103-L120) 与 [L122-L131](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L122-L131)：

```c
const uint8_t si5351_configs[] = {
  2, SI5351_REG_3_OUTPUT_ENABLE_CONTROL, 0xff,      // 先全部关输出
  4, SI5351_REG_16_CLK0_CONTROL, SI5351_CLK_POWERDOWN, SI5351_CLK_POWERDOWN, SI5351_CLK_POWERDOWN,
  2, SI5351_REG_183_CRYSTAL_LOAD, SI5351_CRYSTAL_LOAD_8PF,  // 晶振负载电容 8pF
  2, SI5351_REG_3_OUTPUT_ENABLE_CONTROL, ~(SI5351_CLK0_EN|SI5351_CLK1_EN|SI5351_CLK2_EN),
  0 // sentinel
};
```

紧凑之处在于「数据即代码」：没有结构体、没有 switch，一个 `while (*p)` 循环（L125-L130）就能回放任意长的寄存器序列，表里嵌了一段 `#if 0` 的注释代码，保留了早期「初始化时直接配 PLL/CLK2」方案的寄存器级快照，是极好的学习材料（例如其中 `26MHz * 32 = 832MHz, 32/2-2=14` 和 `832MHz / 104 = 8MHz, 104/2-2=50` 两条注释，直接演示了 P1 公式里 `-512 = 128×(a/2) - 512` 的整数模式取 a=32、104 的用法）。此函数在 `main()` 初始化时被调用（[main.c:L2378](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2378)，u1-l3 已梳理过）。

**寄存器缓存**。[si5351.c:L268-L277](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L268-L277)：`si5351_setupMultisynth()` 每次都会算出 CLKX_CONTROL 的目标值 `dat`，但只有与缓存 `clk_cache[channel]` 不同才真正发起 I2C 写。扫频时绝大多数频点 drive_strength 等控制位不变，这一行判断就省掉一次 I2C 事务。文件头 [L25-L26](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L25-L26) 的注释概括了意图："little speedup exchange"。

**寄存器名与位段的定义**全部在 [si5351.h](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.h)：例如 CLKX_CONTROL（寄存器 16~18）的位段 `SI5351_CLK_POWERDOWN`(bit7)、`SI5351_CLK_INTEGER_MODE`(bit6)、`SI5351_CLK_PLL_SELECT_B`(bit5)、`SI5351_CLK_INPUT_MULTISYNTH_N`(bit3:2)、驱动强度 2/4/6/8mA(bit1:0)，见 [si5351.h:L27-L44](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.h#L27-L44)。读 si5351.c 时手边开一份 [Si5351 数据手册](https://www.silabs.com/documents/public/data-sheets/Si5351-B.pdf) 对照，会发现这个头文件就是数据手册寄存器表的直译。

**输出开关**。[si5351.c:L161-L174](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L161-L174)：`si5351_disable_output()` 先关输出使能再让三路 CLKX 进 POWERDOWN，并把 `current_band` 清零（下次开输出必然全量重配）；`si5351_enable_output()` 重新使能三路并把 `current_freq`、`current_band` 都清零——与 4.3.5 练习 3 同一个模式：**任何影响输出的外部变化都要作废缓存**。

#### 4.4.4 代码实践

**实践：手工解码 `si5351_configs[]` 初始化表。**

1. 实践目标：学会读「长度前缀」压缩表，并把每个数字翻译成寄存器操作。
2. 操作步骤：对照 si5351.h 的宏展开下表每一项（示例答案，第一项已填）：

   | 原始数据 | 展开含义 |
   |---|---|
   | `2, 3, 0xff` | 写寄存器 3（OUTPUT_ENABLE_CONTROL）= 0xFF，即 CLK0~CLK7 全部禁止输出 |
   | `4, 16, 0x80, 0x80, 0x80` | （待填写：写寄存器 16/17/18 各 = ?，含义是？） |
   | `2, 183, 0x80` | （待填写：0x80 对应 `SI5351_CRYSTAL_LOAD_8PF`，即 bit7:6 = 10b） |
   | `2, 3, ~0x07` | （待填写：使能哪三路输出？） |

3. 需要观察的现象：展开后每一行都能对应到数据手册的一个寄存器操作。
4. 预期结果：第二项是连续写寄存器 16、17、18（CLK0/1/2_CONTROL）各 0x80（POWERDOWN）；第四项 `~0x07` = 0xF8，即只清 CLK0/1/2 的使能位 → 三路输出使能。整张表的顺序是「先全关 → 各路掉电 → 配晶振负载 → 再开三路」，典型的「安全上电」次序。
5. 「待本地验证」项：可用逻辑分析仪/示波器抓 I2C 波形核对（可选）；纸面展开即可完成本实践。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `si5351_configs[]` 用「长度前缀 + 哨兵」而不是结构体数组？
**答案**：省内存。Cortex-M0 上 RAM 只有 16KB，结构体数组每项要存指针和对齐填充；一张 `const uint8_t[]` 放 Flash，回放循环只需 4 行代码，且任意长度的批量写（1 字节到 9 字节）都能统一表达。

**练习 2**：`si5351_reset_pll()` 为什么要 `chThdSleepMicroseconds(5000)` 再写复位寄存器，而不是写完再睡？
**答案**：注释（L156-L157）说明复位必须发生在 PLL 新参数生效之后——先把 PLL 寄存器写进去、给芯片留出锁存时间，再触发复位让 PLL 按新分频比重新锁定；顺序反了 PLL 可能带着旧配置复位，输出错误频率。另外 5000µs 这个值是实测折中：小于 900µs 在 band 1 上不稳定，4000~5000µs 时换频看不到幅度毛刺（L51-L52）。

**练习 3**：`si5351_bulk_write()` 忽略了 `i2cMasterTransmitTimeout` 的返回值（`(void)` 强转），这样安全吗？
**答案**：属于「容错但不报告」的取舍。I2C 传输失败（例如芯片未上电）时固件不会重试也不报警，测量结果会异常但系统不崩溃；对仪器固件而言这通常可接受，更严谨的做法是计数错误并通过 shell 暴露出来（可作为二次开发的小改进点）。

## 5. 综合实践

**把 `si5351_get_band()` 与 `si5351_set_frequency()` 的 mul/omul/rdiv 选择逻辑完整翻译成 Python 函数 `band_plan(freq)`，用 8 个测试频点核对源码注释表。**

这是本讲的收尾实践，完成后你将拥有一个可以在 PC 上随便做实验的「软件 si5351 频率规划器」。

1. 实践目标：用代码证明你理解了六级 `if-else` 链 + 频段判断的全部逻辑。
2. 操作步骤：新建 `band_plan.py`（示例代码，非项目源码）：

```python
# 示例代码：si5351.c L403-L424 的 Python 直译
TH = 300_000_000          # config.harmonic_freq_threshold 默认值 (main.c L797)
FREQ_OFFSET = 5_000       # FREQUENCY_OFFSET (nanovna.h L34)

def band_plan(freq):
    ofreq = freq + FREQ_OFFSET
    mul = omul = 1
    rdiv = 1
    if   freq >= TH * 7: mul, omul = 9, 11
    elif freq >= TH * 5: mul, omul = 7, 9
    elif freq >= TH * 3: mul, omul = 5, 7
    elif freq >= TH:     mul, omul = 3, 5
    elif freq <= 500_000:  rdiv = 64; freq <<= 6; ofreq <<= 6
    elif freq <= 4_000_000: rdiv = 8; freq <<= 3; ofreq <<= 3
    # si5351.c L371-L377
    band = 1 if freq // mul < 100_000_000 else (2 if freq // mul < 150_000_000 else 3)
    return band, mul, omul, rdiv

for f in [50_000, 1_000_000, 10_000_000, 120_000_000, 200_000_000,
          400_000_000, 1_000_000_000, 2_600_000_000]:
    band, mul, omul, rdiv = band_plan(f)
    print(f"{f:>12,} Hz -> band={band} mul={mul} omul={omul} rdiv={rdiv}")
```

3. 需要观察的现象：每个频点的 `(band, mul, omul, rdiv)`，以及 `freq/mul`（CLK1 基频）落在哪个区间。
4. 预期结果（可与源码注释表 [si5351.c:L359-L369](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L359-L369) 逐行核对）：

   | freq | band | mul | omul | rdiv | 基频 freq/mul | 注释表位置 |
   |---|---|---|---|---|---|---|
   | 50kHz | 1 | 1 | 1 | 64 | 3.2MHz（×64 后） | 50kHz~100MHz band 1 ✓ |
   | 1MHz | 1 | 1 | 1 | 8 | 8MHz（×8 后） | 同上 ✓ |
   | 10MHz | 1 | 1 | 1 | 1 | 10MHz | 同上 ✓ |
   | 120MHz | 2 | 1 | 1 | 1 | 120MHz | 100~150 band 2 ✓ |
   | 200MHz | 3 | 1 | 1 | 1 | 200MHz | 150~300 band 3 ✓ |
   | 400MHz | 2 | 3 | 5 | 1 | 133.3MHz | 300~450，x3:x5，f=100~150 → band 2 ✓ |
   | 1GHz | 3 | 5 | 7 | 1 | 200MHz | 900~1500，x5:x7，f≈214~300 → band 3 ✓ |
   | 2.6GHz | 3 | 9 | 11 | 1 | 288.9MHz | 2100~2700，x9:x11，f≈233~300 → band 3 ✓ |

   8 行全部与注释表吻合（该表为纯逻辑推导，建议本地运行 `python3 band_plan.py` 核对）。
5. 扩展实验：把 `TH` 改成 200_000_000 重跑——400MHz 不满足 ≥3×TH（600MHz），仍是 `(band 2, mul 3, omul 5)`；但 700MHz 从 mul=3 变成 mul=5（700 ≥ 600M），基频从 233MHz 降到 140MHz、band 从 3 变 2。再试 `TH = 100_000_000`：2.6GHz 仍落 mul=9 档（≥ 7×100M）、基频 288.9MHz 合法——最高档固定是 mul=9，硬上限 2.7GHz 由基频 ≤300MHz 决定，与 TH 无关。`threshold` 命令的实际意义是移动各谐波档的**切换点**（对改装过激励通路的设备可调优谐波模式起点），而不是改变绝对量程上限。

## 6. 本讲小结

- si5351 的信号链是「26MHz 晶振 → PLL（小数反馈分频）→ Multisynth（8~1800 小数分频，或 Divide-by-4）→ R 分频（1~128）→ CLKx」，三路输出共享两个 PLL，频率规划就是为每一路分配「固定谁、调节谁」。
- 三个频段三种策略：band 1（<100MHz）固定 PLL=832MHz 调分频器（扫频快、PLL 不动）；band 2（100~150MHz）固定分频 6 调 PLL；band 3（150~300MHz）固定分频 4（DIVBY4）调 PLL。
- 谐波模式把量程从 300MHz 扩到 2.7GHz：CLK1 取基频的 mul（3/5/7/9）次谐波作激励、CLK0 取 omul（5/7/9/11）次谐波作本振，阈值 `harmonic_freq_threshold`（`FREQ_HARMONICS`）默认 300MHz，最大频率 = 阈值×9。
- 低频（≤500kHz 用 /64、≤4MHz 用 /8）靠 R 分频器扩展：先把目标频率左移再在输出端分频还原，`freq` 与 `ofreq` 同步移位保证 5kHz 差频不变。
- 频段判断用基频 `freq/mul` 而非测量频率；频段切换要延时 5000µs 后复位 PLL；返回的 `delay` 告诉 `sweep()` 要丢弃几个音频缓冲——「先丢再测」在驱动层的实现。
- I2C 层三板斧省带宽：`current_freq` 缓存跳过同频写、`current_band` 缓存跳过频段重配、`clk_cache` 跳过未变化的 CLKX_CONTROL 写；`si5351_configs[]` 用长度前缀压缩表 + 0 哨兵完成上电初始化。

## 7. 下一步学习建议

沿测量主链路继续走，下一讲（u2-l3）将进入信号链的下一站：**tlv320aic3204 编解码器与 I2S DMA 采集**——CLK2 输出的 8MHz MCLK 如何被 codec 倍频成 48kHz 采样时钟、两路中频信号如何经 I2S 双缓冲 DMA 进入 `rx_buffer`、以及 `i2s_end_callback` 如何在缓冲就绪时唤醒 DSP。建议预习时先读 [tlv320aic3204.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/tlv320aic3204.c) 的寄存器初始化序列，你会发现它的「寄存器号+长度+数据」表与本讲 `si5351_configs[]` 是同一种手法。

若想更深入本讲主题，可以：阅读 [Si5351-B 数据手册](https://www.silabs.com/documents/public/data-sheets/Si5351-B.pdf) 的寄存器映射章节，对照 si5351.h 逐个确认位段；以及用 git 历史看看 `si5351_set_frequency()` 的演化（`git log --follow -p si5351.c`），观察频段策略是如何从单频段逐步长出谐波模式的。
