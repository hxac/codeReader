# u2-l4 dsp.c：数字正交解调与 gamma 计算

## 1. 本讲目标

上一讲（u2-l3）我们看清了样本是如何到达 `dsp_process()` 门口的：音频 codec 以 48kHz 采样 5kHz 中频，DMA 双缓冲每 1ms 交出 96 个 16 位样本（48 个立体声帧）给 `i2s_end_callback`。本讲走进门内，读完之后你应该能：

1. 说出 `sincos_tbl[48][2]` 这张表的本质——它是一张按 5kHz 频偏定制的**数字正交本振表**，并能手推它的生成公式。
2. 理解 `dsp_process()` 用"乘法 + 累加"实现的**数字混频与多周期相干累积**，等效于一台软件锁相放大器（单 bin 的 48 点 DFT）。
3. 读懂 `calculate_gamma()` 的复数除法 `(samp ÷ ref)`，理解为什么"取比值"能消除信号源幅度漂移，从而得到归一化的反射/传输系数。
4. 在 PC 上把这三件套提取成一个独立的 C 程序，用合成信号验证输出。

## 2. 前置知识

### 2.1 锁相放大器：用"已知频率"从噪声里捞信号

锁相放大器（lock-in amplifier）是测量微弱周期信号的经典仪器。思路很简单：

- 待测信号是某个**已知频率** \( f_{IF} \) 的正弦波，埋在噪声里；
- 用同频率的本振（local oscillator，LO）正弦波与信号相乘再长时间平均。由三角函数的正交性，频率恰好等于 \( f_{IF} \) 的分量会被"相干"地累加（幅度随累加次数 N 线性增长），而其他频率的分量与噪声则增长得慢得多（非相干，只按 \( \sqrt{N} \) 量级增长）；
- 结果：信噪比随累加时间提升，等效于一个中心频率固定在 \( f_{IF} \)、带宽极窄的带通滤波器——但这个"滤波器"完全由乘加运算构成，不需要任何模拟器件。

NanoVNA 把整个锁相放大器搬进了 `dsp.c`：本振是一张预先算好的表（`sincos_tbl`），乘加就是对 48 个样本的循环。

### 2.2 正交解调（I/Q 解调）：只乘一次是不够的

单乘一个余弦本振只能得到幅度在某一个相位上的投影，会丢失相位信息。解决办法是**正交解调**：同时用一对相位差 90° 的本振——\( \cos\theta \) 与 \( \sin\theta \)——分别与信号相乘累加：

\[
X_c = \sum_i x_i \cos\theta_i, \qquad X_s = \sum_i x_i \sin\theta_i
\]

若输入为 \( x_i = A\cos(\theta_i + \varphi) \)，展开后利用 \( \sum\cos^2\theta = \sum\sin^2\theta = N/2 \)、\( \sum\sin\theta\cos\theta = 0 \)（整数周期时成立），可得：

\[
X_c = \frac{N}{2} A\cos\varphi, \qquad X_s = -\frac{N}{2} A\sin\varphi
\]

即 \( (X_c, -X_s) \) 完整保留了信号的幅度 \( A \) 和相位 \( \varphi \)。这就是"矢量测量"的数学基础：两路点积合起来是一个复数。

### 2.3 复数除法与共轭

复数除法 \( \dfrac{a+jb}{c+jd} = \dfrac{(a+jb)(c-jd)}{c^2+d^2} = \dfrac{ac+bd + j(bc-ad)}{c^2+d^2} \)。分子里乘的 \( c-jd \) 就是分母的**共轭**（conjugate，虚部取反）。`calculate_gamma()` 里那两行看似陌生的公式，展开后正是它。

### 2.4 定点数与防溢出

Cortex-M0 没有硬件除法器和浮点单元，DSP 内层循环用的是 16 位整型样本 × 16 位整型本振的**定点运算**。16 位有符号数范围是 ±32768，两个 16 位数相乘最大可达 \( 2^{30} \)，而 48 次累加会撑爆 32 位有符号数（上限 \( 2^{31}-1 \)），所以源码在乘积上先除以 16 再累加——这是本讲要精读的细节之一。

## 3. 本讲源码地图

| 文件 | 本讲涉及的内容 | 作用 |
| --- | --- | --- |
| [dsp.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c) | 全文件（仅 131 行） | 正交本振表、混频累积、复数除法，本讲主角 |
| [nanovna.h](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h) | L33-34、L106-123 | `FREQUENCY_OFFSET` 常量、`AUDIO_BUFFER_LEN`/`SAMPLE_LEN`/`STATE_LEN`、dsp 函数声明 |
| [main.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c) | L594-680、L703-725、L764-786、L856-897 | `rx_buffer`、`dsp_start/dsp_wait`、`i2s_end_callback`、`sample_func` 指针、`sweep()` 中对三件套的调用 |

整条数据通路一句话概括：**`i2s_end_callback`（中断，每 1ms）→ `dsp_process`（混频+块累加）→ `acc_*` 四个 float 全局量（跨块累加）→ `dsp_wait` 返回后 `sweep()` 线程调 `calculate_gamma`（复数除法）→ `measured[ch][i][2]`**。

## 4. 核心概念与源码讲解

### 4.1 sincos_tbl：为 5kHz 中频定制的正交本振表

#### 4.1.1 概念说明

[u2-l1](u2-l1-measurement-principle.md) 讲过 NanoVNA 的"外差到音频"架构：激励（CLK1）与本振（CLK0）恒差 5kHz（`FREQUENCY_OFFSET`），混频后的中频固定是 5kHz 的音频，由 48kHz 采样率采集。这带来一个极大的便利——**中频频率永远不变，本振波形也就永远不变**，于是本振可以在编译期算好，做成一张只读表放进 Flash，运行时一个字都不用算。

这张表就是 `sincos_tbl[48][2]`：48 行对应一个 DMA 半区里的 48 个立体声帧，每行两个 int16 分别是 \( \sin \) 与 \( \cos \) 在该样本处的取值（放大 32768 倍取整）。

#### 4.1.2 核心流程

表的生成公式（本讲用解析计算逐点核对过，与源码数值吻合到 ±1 个最低位）：

\[
\text{sincos\_tbl}[i] = \left\{\, 32768\sin\frac{2\pi \cdot 5 \cdot (i+0.5)}{48},\;\; 32768\cos\frac{2\pi \cdot 5 \cdot (i+0.5)}{48} \,\right\}, \quad i = 0,\dots,47
\]

三个数字各司其职：

| 数字 | 值 | 含义 |
| --- | --- | --- |
| 频偏 | 5kHz | 中频频率 = `FREQUENCY_OFFSET`，48kHz 采样下一个周期占 9.6 个样本 |
| 表长 | 48 | 一个 DMA 半区的帧数；48 = 9.6 × 5，**恰好是 5 个完整中频周期** |
| 偏移 0.5 | \( i+0.5 \) | 相位从"半个样本"处起算，源码未注释其动机（见练习 3） |

"48 点 = 5 个整周期"是整个设计能成立的关键：因为 48 采样点张好的相位窗口对 5kHz 中频是**闭合**的，所以每个 1ms 半区的第 0 个样本相位恒定，同一张表可以逐半区反复使用，块与块之间的相位天然对齐——这正是"相干累积"里"相干"二字的来源。头文件里那句注释也警告了这一点：改频偏就必须重造表。

#### 4.1.3 源码精读

常量定义（[nanovna.h:33-34](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L33-L34)）——注意注释明确说明表是为 5000Hz 频偏生成的，改频偏需重建表：

```c
// Frequency offset (sin_cos table in dsp.c generated for this offset, if change need create new table)
#define FREQUENCY_OFFSET         5000
```

缓冲区与表长常量（[nanovna.h:106-112](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L106-L112)）——`AUDIO_BUFFER_LEN=96` 是 int16 个数（头文件"5ms"注释与实际 1ms 不符，属历史遗留，u2-l3 已澄清），除以 2 得 48 帧，即 `SAMPLE_LEN`：

```c
// 5ms @ 48kHz
#define AUDIO_BUFFER_LEN 96
...
#define STATE_LEN 32
#define SAMPLE_LEN 48
```

> 关于 `STATE_LEN 32`：全仓库检索后它当前**没有任何引用**，属于历史遗留定义。`git log -S STATE_LEN` 可追溯到最早的 `10b2cb7 add dsp.c (hilbert, iir)` 时期（dsp.c 最初还有希尔伯特变换与 IIR 滤波的实验代码，后来被相关检测取代），具体原始用途**待确认**——但别把它和 `SAMPLE_LEN` 混为一谈，真正决定 DSP 行为的是 48。

本振表本体（[dsp.c:29-42](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L29-L42)）——`const` 修饰使它落在 Flash 的 `.rodata` 段，只花 192 字节 Flash、一个字节 RAM 都不占，这对只有 16K RAM 的 STM32F072 至关重要：

```c
const int16_t sincos_tbl[48][2] = {
  { 10533,  31029 }, { 27246,  18205 }, { 32698,  -2143 }, { 24636, -21605 },
  ...
};
```

随手验证第 0 行：\( 32768\sin(2\pi \cdot 2.5/48) = 32768\sin 18.75° \approx 10533 \)，\( 32768\cos 18.75° \approx 31029 \)——与源码精确一致。

#### 4.1.4 代码实践：用 Python 重建这张表

1. **实践目标**：证明上文的生成公式正确，建立"表 = 数字本振"的手感。
2. **操作步骤**：运行下面 8 行脚本（示例代码，与仓库无关）：

```python
import math
N, CYC = 48, 5            # 表长 48 点，覆盖 5 个中频周期
for i in range(8):        # 只打印前 8 行，与 dsp.c 前 8 行对照
    th = 2 * math.pi * CYC * (i + 0.5) / N
    print("{ %6d, %6d }," % (round(32768 * math.sin(th)), round(32768 * math.cos(th))))
```

3. **需要观察的现象**：打印出的 8 行数值与 [dsp.c:30-32](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L30-L32) 逐个对照。
4. **预期结果**：每个分量误差不超过 ±1（四舍五入的最低位差异）。若把 `CYC` 改成 4（对应频偏 4kHz），表中数值将完全不同——这就是"改频偏必须重造表"的直接体现。本实践在 PC 上即可完成，无需硬件；具体输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么表恰好是 48 行，而不是 `AUDIO_BUFFER_LEN` 的 96 行？

**答案**：96 是 int16 **样本**个数，而数据是左右声道交织的立体声帧——96 个 int16 = 48 帧。`dsp_process` 里 `len = length / 2` 做的正是这个换算，循环 48 次，每次消费一个 32 位字（一帧），表因此是 48 行。

**练习 2**：若想把中频改到 4kHz（`FREQUENCY_OFFSET 4000`），这张表还能用吗？需要怎样改？

**答案**：不能直接用。4kHz 在 48kHz 采样下每周期 12 个样本，48 点恰好是 4 个整周期（仍然闭合、相位对齐），所以只需按 \( 2\pi \cdot 4 \cdot (i+0.5)/48 \) 重新生成一张 48 行的表并同步修改 si5351 两路输出的频偏配置即可；表长本身不用变。反之，凡是 48 点不张成整数个周期的频偏（例如 5.5kHz）都会破坏块间相位对齐，相干累积失效。

**练习 3**：公式里的 \( (i+0.5) \) 半样本偏移是什么作用？

**答案**：源码没有注释说明。可以客观验证的是：偏移 0.5 后 \( \sin \) 部分关于块中心呈反对称、\( \cos \) 部分呈正对称，等效于以块中心为参考的对称采样；一个合理猜测是补偿半样本级的群延迟或使相邻频谱响应对称，但确切动机**待确认**——源码考古时保持这种"已知/推测"边界的标注，比编造一个理由更可靠。

### 4.2 dsp_process：数字混频与相干累积

#### 4.2.1 概念说明

`dsp_process()` 是锁相放大器的"乘法 + 积分"核心。它每次被 `i2s_end_callback` 调用（中断上下文，每 1ms 一次），把一个半区的 48 个立体声帧分别投影到 \( \sin \)、\( \cos \) 两个本振上，得到 4 个部分和：

- `ref_s`、`ref_c`：参考通道（左声道，固定接电桥参考信号）的 \( X_s \)、\( X_c \)
- `samp_s`、`samp_c`：测量通道（右声道，CH0 反射 / CH1 传输）的 \( X_s \)、\( X_c \)

4 个部分和再累加到 4 个 `float` 全局量 `acc_ref_s/acc_ref_c/acc_samp_s/acc_samp_c` 上。**跨多个 1ms 块的继续累加就是"多周期相干累积"**：由 u2-l3 已知的 `bandwidth_accumerate_count[] = {1, 3, 10, 33, 100}`，带宽越窄档位累积的块数越多，等效噪声带宽约为 \( \frac{1}{N \times 1\text{ms}} \)（N 为累积块数），这就是"以时间换精度"在 DSP 侧的落点。

#### 4.2.2 核心流程

一次测量的完整时序（承接 u2-l3 的"先丢再测"协议，这里从 DSP 视角看）：

```text
sweep() 线程                              I2S 中断（每 1ms）
────────────                              ──────────────────────
set_frequency(f)  → 返回 delay
tlv320aic3204_select(0/1)  切换被测通道
dsp_start(delay):
    wait_count = delay            ──→     wait_count>1 ? 只减一（丢弃暂态缓冲）
    accumerate_count = N(带宽档)
    reset_dsp_accumerator() 清零 4 个累积器
dsp_wait(): __WFI 睡眠          ──→     wait_count==1 且 accumerate_count>0 ?
                                          dsp_process(p, 96):
                                            混频+累加得 4 个部分和
                                            累入 acc_* 四个 float
                                          accumerate_count--
                                          （减到 0 时唤醒 sweep）
(*sample_func)(measured[ch][i])
  = calculate_gamma(gamma)       ←────   acc_* 已经就绪
```

`dsp_process` 内层循环的数学，写成公式即（\( \gg 4 \) 表示除以 16）：

\[
\text{acc} \mathrel{+}= \sum_{i=0}^{47} \frac{x_i \cdot \text{sincos\_tbl}[i]}{16}
\]

对 5kHz 分量相干累加（线性于块数），对直流、噪声与谐波镜像分量非相干累加（远慢于线性）——一个乘加循环同时完成了混频、滤波和积分。

#### 4.2.3 源码精读

四个跨块累积器（[dsp.c:44-47](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L44-L47)）——float 类型让多次累加不受 32 位整数范围限制，也直接对接后面 `calculate_gamma` 的浮点除法：

```c
float acc_samp_s;
float acc_samp_c;
float acc_ref_s;
float acc_ref_c;
```

函数主体（[dsp.c:49-86](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L49-L86)）。逐行看关键点：

```c
void
dsp_process(int16_t *capture, size_t length)
{
  uint32_t *p = (uint32_t*)capture;   // 一个 32 位字 = 一帧立体声
  uint32_t len = length / 2;          // 96 个 int16 → 48 帧
  ...
```

- 用 `uint32_t*` 一次读入一帧：低 16 位是左声道 `ref`、高 16 位是 `smp`，与 u2-l3 讲过的"左=参考、右=被测"的接线对应（[dsp.c:60-63](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L60-L63)）：

```c
  for (i = 0; i < len; i++) {
    uint32_t sr = *p++;
    int16_t ref = sr & 0xffff;
    int16_t smp = (sr>>16) & 0xffff;
```

- 混频与累加的核心 6 行（[dsp.c:68-73](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L68-L73)）：4 个 int32 累加器分别积累两路信号与两个正交本振的点积；`/16` 先把乘积缩小 4 个 bit，保证 48 次累加不越出 int32：

```c
    int32_t s = sincos_tbl[i][0];
    int32_t c = sincos_tbl[i][1];
    samp_s += smp * s / 16;
    samp_c += smp * c / 16;
    ref_s += ref * s / 16;
    ref_c += ref * c / 16;
```

- 被 `#if 0` 封存的 SIMD 版本（[dsp.c:74-80](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L74-L80)）：`__SMLABB/__SMLABT` 等是 Cortex-M3/M4 的单周期乘加指令，**M0 没有**，作者保留代码但关闭编译，留作迁移到更强内核时的加速开关——读源码时见到 `#if 0` 块不要跳过，它常常记录着硬件约束：

```c
#if 0
    uint32_t sc = *(uint32_t)&sincos_tbl[i];
    samp_s = __SMLABB(sr, sc, samp_s);
    ...
#endif
```

- 块结果转入 float 累积器（[dsp.c:82-85](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L82-L85)），随后（[dsp.c:124-131](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L124-L131)）`reset_dsp_accumerator()` 在每次 `dsp_start` 时把 4 个累积器清零，保证每个频点从零开始累积：

```c
  acc_samp_s += samp_s;
  acc_samp_c += samp_c;
  acc_ref_s += ref_s;
  acc_ref_c += ref_c;
```

调用侧的同步机制（[main.c:614-627](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L614-L627)）——`dsp_start` 登记丢弃数与累积块数并清零累积器，`dsp_wait` 用 `__WFI` 休眠等中断把 `accumerate_count` 扣完（两个计数器是 `volatile uint8_t`，理由见 u2-l3）：

```c
static inline void dsp_start(int count)
{
  wait_count = count;
  accumerate_count = bandwidth_accumerate_count[bandwidth];
  reset_dsp_accumerator();
}

static inline void dsp_wait(void)
{
  while (accumerate_count > 0)
    __WFI();
}
```

中断回调里的调用点（[main.c:651-661](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L651-L661)）——只在"丢完暂态、正在累积"的窗口内才喂数据给 DSP，每喂一块 `accumerate_count` 减一：

```c
  if (wait_count > 1) {
    --wait_count;                      // 丢弃：换频/换道后的暂态
  } else if (wait_count > 0) {
    if (accumerate_count > 0) {
      dsp_process(p, n);
      accumerate_count--;
    }
```

#### 4.2.4 代码实践：观察"相干累积"的降噪效果

1. **实践目标**：直观看到累积块数 N 增加 → 输出抖动下降。
2. **操作步骤**（需真机，属可选实践；无硬件读者做第 5 节综合实践即可）：
   - 用 USB 串口终端连接 NanoVNA（u1-l3 已建立 shell 环境）；
   - 依次执行 `bandwidth 1000`、`bandwidth 100`、`bandwidth 10`（命令实现在 [main.c:1965-1980](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1965-L1980)，把选中的档位序号写入 `bandwidth`）；
   - 在各档位下执行 `scan 290000000 300000000 101` 后用 `data 0` 连续读取几次 CH0 数据。
3. **需要观察的现象**：不同带宽档下重复读数的散布差异，以及扫描一圈耗时的明显变化。
4. **预期结果**：`10Hz` 档（N=100，约 100ms/通道/点）读数应明显比 `1kHz` 档（N=1）稳定，但扫描更慢——与 `bandwidth_accumerate_count[]` 表（[main.c:604-610](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L604-L610)）一一对应。具体数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`smp * s / 16` 若改成 `smp * s`（不除 16），会发生什么？

**答案**：16 位样本 × 16 位本振最大 \( \pm 32768 \times 32768 \approx \pm 2^{30} \)，在 int32 内单次乘法尚可，但 48 次累加最坏可达 \( 48 \times 2^{30} \)，远超 \( 2^{31}-1 \)，有符号整数溢出（C 语言中是未定义行为，实际通常回绕成错误符号）。除以 16 把每项压小 4 个 bit，给累加留出了裕量；代价是每项最多 1 个单位的截断误差，相对每项 \( \sim 10^6 \) 的量级（幅度数千、本振数万）约百万分之一，可以忽略。

**练习 2**：为什么内层累加用 int32，最后才转成 float 存进 `acc_*`？

**答案**：内层是定点整数运算，M0 上就是几条乘加指令，快且确定性好（中断回调里必须执行时间可预测，u2-l3 讲过回调只做有界计算）；int32 块结果一次转入 float，既解除"多块累加"的整数范围限制（N 最大 100 块），又直接为 `calculate_gamma` 的浮点除法备好数据。另可注意 float 只有 24 位尾数，本例量级约 \( 10^8 \)，舍入误差相对约 \( 10^{-7} \)，无碍。

**练习 3**：`dsp_process` 运行在中断上下文。`acc_*` 四个变量既被中断写、又被 `sweep` 线程经 `calculate_gamma` 读，为什么没有加锁也没出问题？

**答案**：时序上互斥——`sweep` 线程只有从 `dsp_wait()` 返回（`accumerate_count == 0`，之后中断里 `if (accumerate_count > 0)` 不再成立）才会读 `acc_*`；下次 `dsp_start()` 先 `reset_dsp_accumerator()` 再放行中断写入。读写被 `accumerate_count` 这个 volatile 计数器隔离在不同的时间窗，这是无锁的单生产者/单消费者约定，靠协议而非互斥量保证正确。

### 4.3 calculate_gamma：复数除法与归一化

#### 4.3.1 概念说明

累积结束后，`acc_samp_*` 与 `acc_ref_*` 各代表一个复数（幅度 + 相位）。但它们还携带了激励信号的绝对幅度——si5351 输出幅度、电桥损耗、codec 增益都会影响它，而且这些量随频率和时间漂移。直接用 `samp` 的绝对值当测量结果会把这些漂移全部误报成 DUT 的特性。

解决办法是**取比值**：右声道（被测）除以左声道（参考）。两者经过几乎相同的前端路径，公共的幅度/相位漂移在除法中相消，剩下的就是 DUT 独有的复数传递特性——反射系数 \( \Gamma \)（S11）或传输系数（S21）。这正是 u2-l1 所说"比值测量"的软件落点，也解释了为什么 codec 的左声道要固定接参考信号：它是复数除法永远的分母。

#### 4.3.2 核心流程

把 \( X_s \)（sin 相关）记作实部、\( X_c \)（cos 相关）记作虚部，即令复数

\[
z_{\text{samp}} = ss + j\,sc, \qquad z_{\text{ref}} = rs + j\,rc
\]

则 `calculate_gamma` 计算的就是标准复数除法 \( \gamma = z_{\text{samp}} / z_{\text{ref}} \)，分子乘分母共轭展开：

\[
\gamma = \frac{(ss + j\,sc)(rs - j\,rc)}{rs^2 + rc^2}
       = \frac{(sc\!\cdot\!rc + ss\!\cdot\!rs) \;+\; j\,(ss\!\cdot\!rc - sc\!\cdot\!rs)}{rr}, \qquad rr = rs^2+rc^2
\]

对照源码的两行赋值，逐项完全一致。两点值得玩味：

- **约定旋转不影响比值**。教科书 DFT 惯例是把 cos 相关当实部（\( z_{\text{dft}} = X_c + jX_s \) 的共轭方向），固件把 sin 相关当实部——两种约定之间差一个整体的固定旋转（约 90° 的相位因子）。但分子分母同乘同一个因子在除法中相消，所以 \( \Gamma \) 的值不受约定选择影响；固定的旋转只是把 Smith 圆图整体转了个角度，与硬件混频极性共同决定"开路在右、短路在左"的最终朝向（见练习 2）。
- **无开方**。`gamma` 的幅度信息隐含在实虚部中，后面 plot.c 需要模值时再算；这里省掉 `sqrtf`（注释掉的 `//rr = sqrtf(rr) * 1e8;` 恰是历史痕迹）是为了中断后路径上的速度。

#### 4.3.3 源码精读

`calculate_gamma` 全文（[dsp.c:88-108](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L88-L108)）——先算分母模方 `rr`，再做共轭乘法展开；`#elif` 分支是调试期直接输出分子/分母原始值的开关：

```c
void
calculate_gamma(float gamma[2])
{
#if 1
  // calculate reflection coeff. by samp divide by ref
  float rs = acc_ref_s;
  float rc = acc_ref_c;
  float rr = rs * rs + rc * rc;
  float ss = acc_samp_s;
  float sc = acc_samp_c;
  gamma[0] =  (sc * rc + ss * rs) / rr;
  gamma[1] =  (ss * rc - sc * rs) / rr;
#elif 0
  ...
```

两个"只看分子"的调试函数（[dsp.c:110-122](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L110-L122)）——不做除法、只把累积器缩小 \( 10^9 \) 倍输出，用于检查两路信号的原始强度（例如排查电桥接线、codec 增益）：

```c
void
fetch_amplitude(float gamma[2])
{
  gamma[0] =  acc_samp_s * 1e-9;
  gamma[1] =  acc_samp_c * 1e-9;
}
```

三者经函数指针接入系统（[main.c:764-786](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L764-L786)）——`sample_func` 默认指向 `calculate_gamma`，shell 命令 `sample {gamma|ampl|ref}` 可在线切换，无需改代码重编译：

```c
static void (*sample_func)(float *gamma) = calculate_gamma;

VNA_SHELL_FUNCTION(cmd_sample)
{
  ...
  switch (get_str_index(argv[0], cmd_sample_list)) {
    case 0: sample_func = calculate_gamma;    return;
    case 1: sample_func = fetch_amplitude;    return;
    case 2: sample_func = fetch_amplitude_ref; return;
```

sweep() 中的最终调用（[main.c:856-882](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L856-L882)）——`dsp_wait()` 返回即累积就绪，CH0 一测完立刻切 CH1 重复"丢-积-除"，两个复数分别落入 `measured[0][i]`（S11）与 `measured[1][i]`（S21）：

```c
    dsp_start(delay + ((i == 0) ? 1 : 0));
    dsp_wait();
    // calculate reflection coefficient
    (*sample_func)(measured[0][i]);

    tlv320aic3204_select(1);
    dsp_start(DELAY_CHANNEL_CHANGE);
    dsp_wait();
    // calculate transmission coefficient
    (*sample_func)(measured[1][i]);
```

`measured` 的定义见 [nanovna.h:40-41](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L40-L41)：`float measured[2][POINTS_COUNT][2]`，通道 × 频点 × (实部, 虚部)，正是 u1-l1 介绍过的全局数据枢纽。

#### 4.3.4 代码实践：验证"比值归一化"的价值

1. **实践目标**：体会除以参考通道如何抵消公共漂移（本实践是纯推导 + 小程序，PC 可做）。
2. **操作步骤**：在第 5 节综合实践的 host 端程序里，把第二次运行时的 `ref` 与 `smp` **同时**乘以 1.37、再叠加 -0.2 弧度的公共相移（模拟激励源漂移），重跑一次。
3. **需要观察的现象**：两次运行打印出的 gamma 是否变化。
4. **预期结果**：gamma 不变（精确到浮点舍入）。分子分母携带同一因子，在复数除法中严格相消——这就是 VNA 用比值法抗漂移的定量演示。若改用 `fetch_amplitude` 的口径（只看分子），输出会变化约 1.37 倍与 0.2 弧度。具体数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `rr = rs*rs + rc*rc` 之后不开平方再除（即除以 \( |z_{\text{ref}}| \) 而不是 \( |z_{\text{ref}}|^2 \)）？

**答案**：如果先开方再除两次，要做两次除法且每次都是 M0 上昂贵的软除法 + `sqrtf`。把模方直接当分母、让分子少除一次 \( |z_{\text{ref}}| \)，结果完全等价（\( \frac{z_1 \bar z_2}{|z_2|^2} \) 本来就是复数除法的标准形式），却省掉了开方。源码中被注释的 `//rr = sqrtf(rr) * 1e8;` 正是这种"想开方、又删掉"的痕迹。

**练习 2**：若把复数约定改成教科书式的 \( w = X_c + jX_s \)（cos 相关为实部、sin 相关为虚部），按同样的共轭乘法公式计算 \( w_{\text{samp}}/w_{\text{ref}} \)，结果与固件输出有什么关系？

**答案**：记 \( a = X_s,\, b = X_c \)。固件约定 \( z = a + jb \)，而 \( w = b + ja = \overline{(b - ja)} \)，即 \( w = \overline{-j\,z} \)（把 \( z \) 旋转 −90° 再取共轭）。于是 \( \frac{w_s}{w_r} = \overline{-j z_s}/\overline{-j z_r} = \overline{z_s}/\overline{z_r} = \overline{(z_s/z_r)} \)——**得到固件结果的共轭**（虚部反号，Smith 圆图上下镜像）。这解释了为什么复数约定的选择必须与硬件混频极性配套：约定错了，开路/短路在 Smith 圆图上的位置就镜像翻转。

**练习 3**：`fetch_amplitude` 里的 `1e-9` 起什么作用？

**答案**：纯粹是数值缩放便于阅读。累积器量级可达 \( 10^8 \)（幅度数千 × 本振数万 / 16 × 48 点 × 100 块），乘 \( 10^{-9} \) 后落到个位数量级，`shell_printf("%f")` 打印时不至于一长串有效数字。它不影响任何后续计算——这个口径的数据只用于人工检查信号强度。

## 5. 综合实践：把三件套提取成 host 端 C 程序

**任务**：从 `dsp.c` 中原样提取 `sincos_tbl`、`dsp_process`、`calculate_gamma`（连同 4 个累积器），写成一个在 PC 上编译运行的标准 C 程序 `dsp_sim.c`（示例代码，仓库中不存在），用合成信号验证正交解调与复数除法的正确性。

**操作步骤**：

1. 新建 `dsp_sim.c`（放在仓库外任意目录，不要写进源码树），内容分四段：
   - **第一段：本振表**——从 [dsp.c:29-42](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L29-L42) 原样复制全部 48 行（一行都不能少）；
   - **第二段：DSP 三件套**——把 [dsp.c:44-47](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L44-L47) 的 4 个累积器、[dsp.c:49-86](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L49-L86) 的 `dsp_process`、[dsp.c:88-108](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L88-L108) 的 `calculate_gamma`、[dsp.c:124-131](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L124-L131) 的 `reset_dsp_accumerator` 原样复制（删除 `#ifdef ENABLED_DUMP`、`#if 0` 与 `#include <arm_math.h>` 等嵌入式相关行，`VNA_SHELL` 无关）；
   - **第三段：构造信号**——48 点合成帧，左右声道交织进一个 `int16_t buf[96]`：

     ```c
     /* 示例代码：构造 ref = A*cos(2*pi*5*(i+0.5)/48)，samp 与 ref 成指定关系 */
     int16_t buf[96];
     double A = 3000.0;
     for (int i = 0; i < 48; i++) {
         double th = 2 * M_PI * 5 * (i + 0.5) / 48;
         double ref  = A * cos(th);
         double smp  = 0.5 * ref;            /* 情形一：samp = 0.5*ref（同相）   */
         /* double smp = 0.5 * A * sin(th);  情形二：samp = 0.5j*ref（正交 90°） */
         buf[2*i]     = (int16_t)lround(ref);   /* 低 16 位 = 左声道 = ref  */
         buf[2*i + 1] = (int16_t)lround(smp);   /* 高 16 位 = 右声道 = smp  */
     }
     ```

     注意"正交"情形的写法：时域实信号上"乘虚数单位 j"对应希尔伯特变换，把余弦变正弦，所以 `samp = 0.5*A*sin(th)` 就是 `0.5j * ref`；
   - **第四段：跑起来**——`reset_dsp_accumerator(); dsp_process(buf, 96); calculate_gamma(gamma); printf("gamma = %.6f %+.6f j\n", gamma[0], gamma[1]);`。
2. 编译运行：`gcc -O2 -o dsp_sim dsp_sim.c -lm && ./dsp_sim`（两种情形分别编译或用参数切换）。

**需要观察的现象与预期结果**：

| 情形 | samp 信号 | 预期 gamma（解析推导值） |
| --- | --- | --- |
| 一（同相） | `0.5 * ref` | \( (0.5,\; 0) \) —— 幅度减半、相位差为零 |
| 二（正交） | `0.5 * A*sin(th)`（即 0.5j·ref） | \( (0,\; 0.5) \) —— 幅度减半、相位差 90° |

推导要点（情形一）：\( rc = \sum \frac{ref \cdot c}{16} = \frac{A}{16}\cdot\frac{32768}{1}\cdot 24 \)，\( rs \approx 0 \)；\( sc = 0.5\,rc \)，\( ss \approx 0 \)，代入 `calculate_gamma` 得 \( \gamma_0 = \frac{0.5 rc^2}{rc^2} = 0.5 \)、\( \gamma_1 \approx 0 \)。情形二对偶：\( ss = 0.5\,rc \) 落入虚部公式。`/16` 截断误差每项不超过 1，相对量级约 \( 10^{-7} \)，因此打印值应精确到小数点后 4~6 位（`gamma[1]` 可能显示 ±0.000001 的残余）。以上为推导预期，具体输出待本地验证。

**加分项**（对应 4.3.4 的归一化验证）：把 `ref` 与 `smp` 同乘 1.37 并同时叠加 −0.2 弧度公共相移，重跑后 gamma 应保持不变。

## 6. 本讲小结

- `sincos_tbl[48][2]` 是按 \( 2\pi\cdot5\cdot(i+0.5)/48 \) 生成的**数字正交本振表**：48 点恰好张成 5 个 5kHz 中频周期，因此每个 DMA 半区相位对齐、一张 const 表（Flash 中 192 字节）可以永久复用；改频偏必须重造表。
- `dsp_process` 用"乘本振 + 累加"完成数字混频，等效于 48 点 DFT 的单个频点（bin 5），也是一台软件锁相放大器；`/16` 为 48 点 int32 累加防溢出，`#if 0` 的 SMLABB 块记录了 Cortex-M0 缺少 SIMD 指令的硬件约束。
- 跨块累加到 4 个 `float` 累积器实现**多周期相干累积**，块数由 `bandwidth_accumerate_count[]` 决定（1/3/10/33/100），带宽以时间换精度；读写窗口靠 `accumerate_count` 这个 volatile 计数器无锁隔离。
- `calculate_gamma` 做复数除法 \( z_{\text{samp}}/z_{\text{ref}} \)（分子乘分母共轭、分母用模方省掉开方），参考通道作分母抵消激励漂移，输出归一化的 S11/S21 到 `measured[2][101][2]`；`sample_func` 函数指针可在 gamma/ampl/ref 三种口径间在线切换。
- 复数约定的选择（sin 相关为实部）与硬件混频极性配套——换成教科书约定会得到共轭结果，比值本身不受整体旋转影响。

## 7. 下一步学习建议

至此，"设频率 → 采样本 → 正交解调 → 复数比值"的单点测量链路已经完整。下一讲 **u2-l5 扫频线程：Thread1 主循环与并发协作** 会把这条链路放回 `Thread1` 的完整循环中，考察 `sweep()` 如何被 UI 操作打断、测量与绘图如何流水线交错。继续深入前的两个源码预习建议：

1. 通读 [main.c:856-897](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L856-L897) 的 `sweep()`，标出所有 `dsp_start/dsp_wait` 调用点，思考每个调用点为什么需要不同的丢弃计数。
2. 想预习校准数学的读者可以提前浏览 [main.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c) 中 `cal_collect`/`cal_done`（u3-l2 的主角）——本讲的 \( \Gamma \) 正是那里误差模型的输入量。
