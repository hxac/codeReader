# 时域变换：FFT、Kaiser 窗与窗口损耗补偿

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 NanoVNA 为什么能从「频域扫频数据」算出「时域冲击/阶跃响应」，即频域测量与时域响应互为傅里叶变换对这一核心思想。
2. 讲解 `transform_domain()` 的完整流程：三种时域模式（带通 / 低通冲击 / 低通阶跃）各自的频谱构造方式、频域使用条件，以及 `wincorr` 因子如何补偿加窗与零填充带来的幅度损失。
3. 读懂 `fft.h` 中 Nayuki 的 radix-2 FFT：「交换实虚部下标实现反变换」这个巧妙技巧为什么成立。
4. 理解 Kaiser 窗的数学定义、`bessel0` 级数实现，以及 β=0/6/13 三档窗口在分辨率与旁瓣之间的取舍。
5. 在 PC 上用 numpy 完整复现 `transform_domain`，并验证源码中 2.01 / 2.92 两个「魔法常数」的来历。

## 2. 前置知识

本讲是全手册数学浓度最高的一讲，先把几个概念用通俗语言铺平。

**时域反射计（TDR）的直觉。** 传统 TDR 向电缆发一个快沿脉冲，观察反射波形随时间的分布，从而定位故障点：时间 × 波速 ÷ 2（往返）= 距离。NanoVNA 反其道而行——它在频域逐点测量反射系数 Γ(f)，而 Γ(f) 恰好就是被测系统冲击响应 h(t) 的傅里叶变换。因此对扫频数据做一次**反傅里叶变换（IFFT）**，就能得到等效的时域响应，这叫「频域 TDR」或 chirp-z 变换（网络分析仪的商用叫法）。好处是无需快沿脉冲这种苛刻的时域激励，只用本来就有的正弦扫频。

**频谱采样与时间轴的关系。** 若扫频步进为 \( \Delta f \)（相邻频点间隔），频域一共有效 \( N \) 个 bin，则反变换后：

- 时间分辨率 \( \Delta t = \dfrac{1}{N \cdot \Delta f} \)（总带宽越宽，脉冲越窄，定位越准）；
- 最大无模糊时延 \( T = \dfrac{1}{\Delta f} \)（步进越细，能看的时窗越长）。

**加窗为什么必要。** 直接对截断的频谱做 IFFT，时域上会出现 sinc 函数旁瓣（吉布斯振铃），一个小的真实反射峰会淹没在大反射的旁瓣里。在 IFFT 前给频谱乘一个两端渐变到零的窗函数（如 Kaiser 窗），能把旁瓣压低几十 dB，代价是主瓣变宽（距离分辨率变差）——这就是「最小 / 普通 / 最大」三档窗口菜单的含义。

**Kaiser 窗与贝塞尔函数。** Kaiser 窗定义为

\[ w(k) = \frac{I_0\!\left(\beta\sqrt{1 - r^2}\right)}{I_0(\beta)}, \qquad r = \frac{2k}{n-1} - 1 \]

其中 \( I_0 \) 是第一类零阶修正贝塞尔函数，β 是形状因子：β=0 退化为矩形窗（分辨率最高、旁瓣最差），β 越大旁瓣越低、主瓣越宽。固件用幂级数 \( I_0(x) = \sum_{m=0}^{\infty} \dfrac{(x^2/4)^m}{(m!)^2} \) 直接计算。

**零填充（zero-padding）与幅度损失。** FFT 要求点数为 2 的整数次幂，固件固定做 256 点 IFFT（`FFT_SIZE`），而测量数据只有 101 个点（低通模式构造共轭对称后也只有 202 个有效点），剩余 bin 全部填零。零填充本身不丢失信息（相当于时域 sinc 插值），但会让「窗函数增益」摊薄：窗的相干增益等于其平均值 \( \bar{w} \)，若不补偿，时域幅度会被低估 \( \frac{N_{fft}}{N_{win}\cdot \bar{w}} \) 倍。2020 年 10 月的提交 `4d64ef6`（Compensate IFFT window / zero-padding loss in TD）正是补上这一项，让时域读数与其他 VNA 对齐。

**共轭对称（Hermitian 对称）。** 实数信号的频谱满足 \( X(-f) = X^*(f) \)（实部偶对称、虚部奇对称）。低通模式手工把 101 个测量点「镜像共轭」填满 256 bin，强制 IFFT 结果为实数——这样才能得到物理上直观的实数冲击响应。

**承接前讲。** 本讲仍工作在 sweep 线程写好的 `measured[2][101][2]` 数组上（u2-l1、u2-l5）；`domain_mode` 存在 `current_props` 里、由 flash 掉电保存（u3-l4）；时域变换是对 `measured` 的**原地覆盖**，绘制仍走 plot.c 的既有轨迹流水线（u4 系列）。

## 3. 本讲源码地图

| 文件 | 关键内容 | 本讲角色 |
| --- | --- | --- |
| `main.c` | `transform_domain()`（L194-277）、`bessel0()`（L169-184）、`kaiser_window()`（L186-192）、`cmd_transform`（L1841-1882）、Thread1 中的触发点（L131-135） | 主角：时域变换全部逻辑 |
| `fft.h` | Nayuki radix-2 `fft256()`（L41-82）与正反变换包装（L84-90） | 数学引擎 |
| `nanovna.h` | `domain_mode` 位域常量（L68-80）、`FFT_SIZE`/`POINTS_COUNT`（L40、L80）、`spi_buffer`（L308、L327）、别名宏（L406） | 接口契约 |
| `plot.c` | `time_of_index`/`distance_of_index`（L784-794）、时域坐标轴显示（L1600-1605、L1629-1642） | 时轴的物理解释 |
| `ui.c` | 时域菜单项（L932-934） | 入口之一（可选了解） |

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：①时域模式与频域条件；②`transform_domain` 主流程（含 wincorr 补偿）；③`fft.h` 反变换；④Kaiser 窗与 `bessel0`。

### 4.1 时域模式与频域条件：domain_mode 位域

#### 4.1.1 概念说明

`domain_mode` 是一个 8 位标志字节（存在 `current_props._domain_mode`，随校准槽一起掉电保存），它同时回答三个问题：现在显示**频域还是时域**、时域用**哪种响应模式**、加**哪档窗**。头文件注释 `0bxxxxxffm` 点明了布局：bit0 是域选择，bit1-2 是时域函数，bit3-4 是窗口档位。三个字段的取值都通过掩码读写、互不干扰，这是嵌入式代码里常见的「打包配置字节」手法。

三种时域模式的物理含义与**频域使用条件**（这是本模块的重点，也是初学者最容易踩的坑）：

- **带通（BANDPASS）**：把 101 个复数 Γ 原样当作一段频谱做 IFFT。它不要求扫频从直流附近开始（所以叫带通——测量可以落在任意频段内），代价是结果只含「时延差」信息，且是复数。
- **低通冲击（LOWPASS IMPULSE）**：把频谱构造为从直流到 stop 的完整低通响应（直流 bin 由第一个测量点充当，高频段镜像共轭配对），得到实数冲击响应。**要求扫频起点尽量低（接近直流）且等步进**，否则直流外推不成立。
- **低通阶跃（LOWPASS STEP）**：在低通冲击响应上做逐点累加，得到阶跃响应——即传统 TDR 屏幕上那种「台阶」波形，最适合直接读电缆故障的反射极性和位置。

#### 4.1.2 核心流程

```
用户选择（shell 命令 transform 或屏幕 DISPLAY 菜单）
        │
        ▼
set_domain_mode / set_timedomain_func / set_timedomain_window
   （各自只改 domain_mode 的对应位段）
        │
        ▼
sweep 线程完成一次完整扫频（completed 置位，u2-l5）
        │
        ▼
(domain_mode & DOMAIN_MODE) == DOMAIN_TIME ？──否──► 直接按频域画轨迹
        │是
        ▼
transform_domain()  原地改写 measured[] ──► plot_into_index 照常画图
（x 轴语义由 plot.c 换算成时间/距离）
```

#### 4.1.3 源码精读

位域定义（注意每个字段先有「掩码」再有各取值）：

- [nanovna.h:L68-L80](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L68-L80)：定义 `DOMAIN_MODE`（bit0，`DOMAIN_FREQ`=0 / `DOMAIN_TIME`=1）、`TD_FUNC`（bit1-2，BANDPASS / LOWPASS_IMPULSE / LOWPASS_STEP）、`TD_WINDOW`（bit3-4，NORMAL / MINIMUM / MAXIMUM）以及 `FFT_SIZE 256`。一个字节打包了「域 + 模式 + 窗」三件事。
- [nanovna.h:L379](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L379) 与 [nanovna.h:L406](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L406)：`_domain_mode` 是 `properties_t` 的成员，别名宏 `domain_mode` 让 main.c / plot.c 像用全局变量一样用它——这就是 u1-l1 讲过的「current_props 别名宏」钥匙。

三个 setter 用「清位段再置位」的模板改写对应字段：

- [main.c:L1819-L1839](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1819-L1839)：`set_domain_mode` 切换频/时域（顺带请求重绘频率轴、把拨轮切回 marker 模式），`set_timedomain_func` 与 `set_timedomain_window` 分别改 `TD_FUNC`、`TD_WINDOW` 位段。

shell 侧的入口是一个多关键字命令：

- [main.c:L1841-L1882](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1841-L1882)：`cmd_transform` 用 `get_str_index` 在 `"on|off|impulse|step|bandpass|minimum|normal|maximum"` 里查关键字，命中即调对应 setter。命令表注册在 [main.c:L2195](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2195)（`{"transform", cmd_transform, 0}`，无 `CMD_WAIT_MUTEX`，因为只写一个字节，不必移交 sweep 线程）。屏幕菜单入口对应 [ui.c:L932-L934](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L932-L934)。

触发点在 sweep 线程主循环里（u2-l5 讲过的四级标志消费循环）：

- [main.c:L131-L135](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L131-L135)：只有 `SWEEP_ENABLE` 且 `completed`（一轮扫频完整结束）时才做时域变换，然后照常 `plot_into_index`。也就是说**每一轮扫频都重新变换一次**，画图代码完全不感知时域/频域的区别。

时域下 x 轴的物理换算在 plot.c：

- [plot.c:L784-L794](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L784-L794)：`time_of_index(idx) = idx / (Δf · FFT_SIZE)`，正是 \( \Delta t = 1/(N\Delta f) \)；`distance_of_index` 再乘光速（[nanovna.h:L36](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L36) 定义 `SPEED_OF_LIGHT`）、除以 2（往返双程）、乘 `velocity_factor`（默认 0.7，见 [main.c:L832](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L832) 的 RG-316 电缆速度因子）。
- [plot.c:L1629-L1642](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1629-L1642)：`draw_frequencies` 在时域下把坐标轴从 `START/STOP Hz` 换成 `START 0s / STOP xx s (xx m)`；marker 读数同理（[plot.c:L1600-L1605](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1600-L1605)）。

#### 4.1.4 代码实践

**实践目标**：把 `domain_mode` 的位域布局在 PC 上玩熟，能从任意字节值反解出「域/模式/窗」。

**操作步骤**（本实践的代码是「示例代码」，不是项目原有代码）：

1. 新建 `domain_decode.py`，按 nanovna.h 的位定义实现解码：

```python
# 示例代码：对应 nanovna.h L68-L80 的位域
def decode(dm: int) -> str:
    m  = dm & 0b1            # DOMAIN_MODE
    ff = (dm >> 1) & 0b11    # TD_FUNC
    ww = (dm >> 3) & 0b11    # TD_WINDOW
    domain = "TIME" if m else "FREQ"
    func   = ["BANDPASS", "LOWPASS_IMPULSE", "LOWPASS_STEP", "?"][ff]
    window = ["NORMAL", "MINIMUM", "MAXIMUM", "?"][ww]
    return f"0b{dm:08b}: {domain} / {func} / {window}"

for v in (0x00, 0x01, 0x03, 0x05, 0x09, 0x13, 0x1B):
    print(decode(v))
```

2. 逐个值与 `cmd_transform` 的关键字对照：`on` 只改 bit0，`impulse` 只改 bit1-2，`minimum` 只改 bit3-4。

**需要观察的现象**：同一个字节被三组掩码独立读写；`on`、`bandpass`、`maximum` 三个命令叠加后的值等于各字段取值的按位或。

**预期结果**：例如 `0x09` 应解码为 `TIME / BANDPASS / MINIMUM`（bit0=1 时域，bit1-2=00 带通，bit3-4=01 最小窗）。有真机的读者可接 USB 终端依次敲 `transform on`、`transform step`、`transform maximum`，再用 `dump` 类命令读回配置对照（具体可回显的命令以 `help` 列表为准，待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么低通模式要求扫频起点接近直流，而带通模式没有这个要求？

**答案**：低通模式要把频谱构造成「直流 + 正频率 + 镜像负频率」的完整低通响应，第一个测量点被当作直流 bin 使用；若起点远离直流，直流到起点之间的频谱缺失，冲击响应会被「错认」时延基准。带通模式原样使用测量频段、不做直流假设，只反映带内时延差，因此不受起点限制。

**练习 2**：`time_of_index` 里为什么除以 `FFT_SIZE`（256）而不是 `POINTS_COUNT`（101）？

**答案**：反变换的记录长度是 256 个 bin（零填充后），时间分辨率由**总 bin 数 × 步进**决定，即 \( \Delta t = 1/(256 \cdot \Delta f) \)。频点数 101 只决定其中非零 bin 的个数，不决定 IFFT 的时间轴刻度。

**练习 3**：时域模式下 `measured[]` 被原地覆盖，下一轮扫频会怎样？

**答案**：sweep 每轮都重新测量并覆盖 `measured[]`（u2-l1 的 sweep 主循环），随后 Thread1 在 `completed` 后再次执行 `transform_domain()`（main.c:131-135）。变换结果只活在 RAM，不写 flash，随时切回 `transform off` 即恢复频域显示。

### 4.2 transform_domain 主流程与 wincorr 幅度补偿

#### 4.2.1 概念说明

`transform_domain()` 是本讲的枢纽：输入是频域 `measured[2][101][2]`，输出是时域响应（原地写回同一数组）。它每轮做四件事——选窗与补偿因子、加窗并零填充到 256 点、（低通模式）构造共轭对称频谱、IFFT 后归一化（阶跃模式再累加）。

`wincorr` 是 2020 年 10 月提交 `4d64ef6` 引入的幅度补偿因子。它要回答的问题是：**加窗 + 零填充让时域幅度整体缩小了多少？** 补偿掉的损耗分两部分：

1. **零填充损耗**：窗作用于 \( N_{win} \) 个样本（低通 202、带通 101），而 IFFT 归一化除以 \( N_{fft}=256 \)，等效增益为 \( N_{win}/N_{fft} \)，补偿系数 \( N_{fft}/N_{win} \)；
2. **窗函数损耗**：窗的相干增益是其平均值 \( \bar{w} \)，Kaiser 窗两端压零使 \( \bar{w}<1 \)，补偿系数 \( 1/\bar{w} \)。

合并：\( \text{wincorr} = \dfrac{N_{fft}}{N_{win} \cdot \bar{w}} \)。以带通 + 普通窗为例：\( \frac{256}{101 \times 0.4975} \approx 5.09 \)，即 \( 20\log_{10} 5.09 \approx 14.2\,\mathrm{dB} \)——与提交说明里的数字完全一致。

#### 4.2.2 核心流程

```
wincorr 初值 1.0
 ├─ 按 TD_WINDOW 选 β 与窗损耗常数：
 │    MINIMUM: β=0   wincorr = 256/202          （矩形窗，只剩零填充损耗）
 │    NORMAL : β=6   wincorr = 256/202 × 2.01
 │    MAXIMUM: β=13  wincorr = 256/202 × 2.92
 └─ 按 TD_FUNC 调整：
      BANDPASS:        窗只有 101 点 → wincorr ×= 2
      LOWPASS_STEP:    wincorr = 1.0（阶跃响应用累加，不做此补偿）后落入下分支
      LOWPASS_IMPULSE: is_lowpass=1, 窗 202 点、offset=101

对每个通道 ch ∈ {0,1}：
  ① measured[ch] → tmp（借用 spi_buffer）
  ② i=0..100: tmp[i] ×= kaiser(i+offset, window_size, β) × wincorr
  ③ i=101..255: 清零（零填充）
  ④ 若低通：i=1..100: tmp[256−i] = conj(tmp[i])   ← 共轭对称
  ⑤ fft256_inverse(tmp)
  ⑥ 取前 101 点 ÷ FFT_SIZE 写回 measured[ch]（低通时虚部清零）
  ⑦ 若阶跃：实部做前缀累加 Σ_{k≤i} h[k]
```

#### 4.2.3 源码精读

- [main.c:L194-L199](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L194-L199)：函数开头把 `spi_buffer`（本来的 LCD 截图缓冲，[nanovna.h:L327](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L327) 定义为 `uint16_t[2048]`，4096 字节 = 512 个复数 float）强转成 `float*` 当 FFT 工作区——256 个复数只需 1024 字节，绰绰有余。这是 16KB RAM 约束下的典型复用手法；安全性依赖一个事实：另一个使用者 `cmd_capture` 带 `CMD_WAIT_MUTEX`（见 [main.c:L2190](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2190)），与 `transform_domain` 同在 sweep 线程串行执行，不会撞车（u2-l5 的跨线程命令机制）。
- [main.c:L201-L221](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L201-L221)：第一段 switch 按 `TD_WINDOW` 定 β 与 wincorr。注释直接写明常量来历：`1/mean(kaiser(202,6)) = 2.01`、`1/mean(kaiser(202,13)) = 2.92`——即上一节推导的 \( 1/\bar{w} \)（按 202 点窗预先算好，运行时只乘常数，省掉 Cortex-M0 上逐点求和的软浮点开销）。
- [main.c:L223-L241](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L223-L241)：第二段 switch 按 `TD_FUNC` 调整：带通窗长减半（101 点），损耗翻倍，`wincorr *= 2.0f`；低通阶跃显式置 `wincorr = 1.0` 后 **fall-through** 到低通冲击分支（共用 `is_lowpass / offset=101 / window_size=202` 三个设置）——注意 C 语言的 fall-through 在这里是被刻意利用的，注释也标明了。
- [main.c:L243-L249](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L243-L249) 加窗主循环：`kaiser_window(i + offset, window_size, beta)`——低通模式下 `offset=101`，即让 101 个测量点落在 202 点窗的**后半段**，靠近「直流」一端（k 小）几乎不衰减、靠近 stop 一端（k 大）平滑压零；带通模式 `offset=0`，两端都压零。
- [main.c:L250-L259](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L250-L259)：先把 101..255 bin 清零（零填充），低通模式再把 `tmp[256−i]` 填成 `tmp[i]` 的共轭（实部照抄、虚部取负）。bin 0 是直流、自共轭，不参与镜像；bin 101..155 保持为零，于是正负频率恰好配满 256 bin。
- [main.c:L261-L270](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L261-L270)：调用 `fft256_inverse`（下一模块精读），随后**只取前 101 点**除以 `FFT_SIZE` 写回 `measured[ch]`——时域只显示正时间轴；低通模式额外把虚部清零（共轭对称保证理论上本就是实数，清零是消除数值残差）。注意归一化除以 256 而不是 202，与 `fft256` 「反变换不带 1/N 因子」的约定配套。
- [main.c:L271-L275](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L271-L275)：阶跃模式对实部做前缀和 \( s[i] = \sum_{k \le i} h[k] \)，把冲击响应积分成阶跃响应——这正是离散序列「累加 = 积分」的直接体现。

#### 4.2.4 代码实践

**实践目标**：用 numpy 逐行复现 `transform_domain`，验证 2.01 / 2.92 两个常数与 14.2 dB 损耗，并亲手生成低通阶跃（TDR）距离域响应。

**操作步骤**（示例代码，在 PC 上运行，`pip install numpy matplotlib` 后即可）：

```python
# 示例代码：transform_domain 的 PC 复现
import numpy as np
import matplotlib.pyplot as plt

POINTS_COUNT, FFT_SIZE = 101, 256

def kaiser_window(k, n, beta):          # 对应 main.c L186-192
    if beta == 0.0: return 1.0
    r = (2 * k) / (n - 1) - 1
    return np.i0(beta * np.sqrt(1 - r * r)) / np.i0(beta)

def transform_domain(gamma, func, beta):
    wincorr = {0.0: FFT_SIZE / (2*POINTS_COUNT),
               6.0: FFT_SIZE / (2*POINTS_COUNT) * 2.01,
               13.0: FFT_SIZE / (2*POINTS_COUNT) * 2.92}[beta]
    is_lowpass = func != "bandpass"
    offset, window_size = (POINTS_COUNT, POINTS_COUNT*2) if is_lowpass else (0, POINTS_COUNT)
    if func == "bandpass": wincorr *= 2.0      # 窗长减半，损耗翻倍
    if func == "step":     wincorr = 1.0       # 阶跃模式不补偿

    tmp = np.zeros(FFT_SIZE, complex)
    tmp[:POINTS_COUNT] = gamma
    for i in range(POINTS_COUNT):              # 加窗 + 补偿
        tmp[i] *= kaiser_window(i + offset, window_size, beta) * wincorr
    if is_lowpass:                             # 共轭对称
        tmp[FFT_SIZE - np.arange(1, POINTS_COUNT)] = np.conj(tmp[1:POINTS_COUNT])

    h = np.fft.ifft(np.concatenate([tmp[:FFT_SIZE//2],
                                    np.conj(tmp[FFT_SIZE//2:0:-1])]))
    h = h[:POINTS_COUNT] * FFT_SIZE            # numpy 的 ifft 自带 1/N，先抵消再模拟 /N
    if is_lowpass:
        h = h.real
        if func == "step":
            h = np.cumsum(h.real)              # 阶跃 = 冲击的前缀和
    return h

# ① 验证窗损耗常数
for beta in (0.0, 6.0, 13.0):
    w = np.array([kaiser_window(k, 202, beta) for k in range(202)])
    print(f"beta={beta:4.0f}  1/mean(kaiser(202,beta)) = {1/w.mean():.3f}")
print("bandpass+normal 损耗 = %.1f dB" %
      (20*np.log10(FFT_SIZE/202*2.01*2)))

# ② 模拟一根 3 米处有开路故障的电缆（阶梯阻抗 → Γ 为复数振荡）
n, df = np.arange(POINTS_COUNT), 30e6/100     # 0~30MHz, Δf=300kHz
fault_delay = 2*3/0.7/299792458               # 往返 6m、速度因子 0.7
gamma = 0.8*np.exp(-2j*np.pi*df*n*fault_delay)

# ③ 低通阶跃模式 + 三档窗对比
for beta, name in ((0.0,"minimum"), (6.0,"normal"), (13.0,"maximum")):
    s = transform_domain(gamma, "step", beta)
    t = n / (df * FFT_SIZE)
    plt.plot(t*299792458*0.7/2, s, label=name)   # 时轴换算回米
plt.xlabel("distance [m]"); plt.legend(); plt.show()
```

（第 ① 步不依赖 matplotlib，可以独立先跑；第 ③ 步需要 matplotlib。）

**需要观察的现象**：① 中打印的 `1/mean` 值；③ 中故障台阶出现在 3 m 附近，且 β 越大台阶前沿越缓但振铃越小。

**预期结果**：① 打印约 `1.000 / 2.01 / 2.92`；② 损耗 `≈ 14.2 dB`（与提交 4d64ef6 说明一致）；③ 三档窗的台阶位置相同、陡峭程度与振铃呈明显取舍。numpy 的 `ifft` 与 C 代码的位序/归一化差异需要像上面那样先乘 FFT_SIZE 对齐，若你的对齐方式不同，幅度可能差一个常数因子，属正常，重点看波形形状（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么低通模式加窗时用 `offset = POINTS_COUNT`，让数据落在 202 点窗的后半段，而不是从头开始？

**答案**：低通频谱的能量集中在直流附近（bin 0 及低频端），把测量点映射到窗的后半段（k=101..201）意味着低频端 r≈0、窗值≈1（不衰减最重要的直流/低频分量），只有高频端被平滑压零；若从前半段开始，直流 bin 会被窗的零点直接掐掉，冲击/阶跃响应严重失真。

**练习 2**：提交 4d64ef6 之前（旧代码只乘 `w` 不乘 `wincorr`），带通 + 普通窗的时域幅度会低多少？

**答案**：低 \( 20\log_{10}(5.09) \approx 14.2 \) dB。旧代码中 `beta` 的 switch 与模式 switch 是分开的、没有 wincorr；新代码把两个 switch 前移合并，并在乘窗时一并乘上补偿因子（可用 `git show 4d64ef6 -- main.c` 对照）。

**练习 3**：`wincorr` 在低通阶跃模式下为什么置 1？

**答案**：提交说明明确本次只补偿带通与低通冲击两种模式。阶跃响应由冲击响应逐点累加得到，其关注重点是台阶位置与反射极性、最终收敛值，作者选择不做该补偿（代码注释：`no IFFT losses need to be considered to calculate the step response`）。这也提醒我们：补偿是标定约定问题，要与「其他 VNA 对齐」这一目标绑定理解。

### 4.3 fft.h：Nayuki radix-2 反变换

#### 4.3.1 概念说明

`fft.h` 取自 Project Nayuki 的「Free FFT in multiple languages」（MIT 许可，文件头注明），被原样内联进 main.c（[main.c:L26](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L26) `#include "fft.h"`）。它是教科书级的 Cooley-Tukey 按时间抽取 radix-2 实现，定点不友好、但结构极简，非常适合学习。其最有意思的设计是**用「交换实虚部下标」实现反变换**：数学上 \( \mathrm{IDFT} \) 与 \( \mathrm{DFT} \) 只差一个共轭与 1/N 因子（\( x = \frac{1}{N}\overline{\mathrm{DFT}(\overline{X})} \)），而「对调实虚部」恰好等价于乘 ±j 再取共轭的复合操作，于是在蝶形循环里把 `real`/`imag` 两个下标变量对调，同一个正变换循环就变成了反变换——零额外内存、零取反循环。

#### 4.3.2 核心流程

```
fft256(array, dir):
  ① 位反转重排：i 与 reverse_bits(i, 8) 交换（把自然序变按位反转序）
  ② 逐级蝶形：size = 2,4,8,...,256（共 8 级）
       每级内 tablestep = 256/size，旋转因子 θ = 2π·k/256
       蝶形：t = a[l] · e^{−jθ}（dir=0 正变换）
             a[l] = a[j] − t;  a[j] = a[j] + t
  ③ dir=1 时 real/imag 下标对调 ⇒ 上式旋转因子等效为 e^{+jθ}
     ⇒ 整体结果 = N × IDFT（不含 1/N）
调用方约定：fft256_inverse 之后自行除以 256 归一化
```

复杂度 \( O(N\log N) \)：256 点 = 8 级 × 每级 128 个蝶形 = 1024 次复数乘法，比直接 DFT 的 65536 次少两个数量级。

#### 4.3.3 源码精读

- [fft.h:L29-L35](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/fft.h#L29-L35)：`reverse_bits` 把 x 的低 n 位按位反转——位反转重排是按时间抽取 FFT 的标配预处理，使蝶形可以全部原位进行。
- [fft.h:L41-L46](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/fft.h#L41-L46)：`fft256` 把点数 256、级数 8 写死；`real = dir & 1; imag = ~real & 1` 两行就是「下标对调」技巧的实现——`dir=1` 时实部存在 `array[i][1]`、虚部存在 `array[i][0]`，从根上把正变换循环复用成了反变换。
- [fft.h:L50-L60](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/fft.h#L50-L60)：位反转重排循环，只在 `j > i` 时交换以防换回去。
- [fft.h:L63-L78](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/fft.h#L63-L78)：蝶形主体。旋转因子没有查表，而是每次实时调 `cos/sin`（[fft.h:L71-L72](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/fft.h#L71-L72)）——在 Cortex-M0 上这是软浮点库函数、相当慢，但作者优先了代码体积与简洁；每级结束用 `if (size == n) break` 防 `size *= 2` 在 uint8_t 上溢出（L79-80），这是 8 位类型循环里的经典细节。
- [fft.h:L84-L90](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/fft.h#L84-L90)：`fft256_forward/inverse` 内联包装，目前固件只调用 inverse。

#### 4.3.4 代码实践

**实践目标**：把 `fft.h` 原封不动搬到 PC 上编译，验证它确实等于「N × numpy IFFT」。

**操作步骤**（示例代码；`fft.h` 是项目原有文件，其余为示例）：

1. 复制一份 `fft.h` 到临时目录，编写 `fft_host.c`：

```c
/* 示例代码：fft.h 依赖 VNA_PI 与 float[][2] 布局，先补定义再包含 */
#include <stdio.h>
#define VNA_PI 3.14159265358979323846
#include "fft.h"

int main(void) {                       /* 验证：冲激频谱 → 直流信号 */
    static float a[256][2];
    a[0][0] = 1.0f;                    /* 只有 bin0 = 1，IDFT 应得全 1/N */
    fft256_inverse(a);
    for (int i = 0; i < 4; i++)
        printf("%d: %f %f\n", i, a[i][0], a[i][1]);
    /* 期望 0.003906 (=1/256)，虚部 0 —— 证明 fft256_inverse = N×IDFT */
    return 0;
}
```

2. `gcc -O2 fft_host.c -lm -o fft_host && ./fft_host`。
3. 第二个实验：把 4.2.4 的 numpy 复现中的 `np.fft.ifft(...)*FFT_SIZE` 换成对 `fft256_inverse` 结果的逐点对比（可用 Python `ctypes` 封装，或改写为纯 C 打印），统计最大偏差。

**需要观察的现象**：bin0=1 的频谱经 `fft256_inverse` 后每个点是 1.0 还是 1/256；与 numpy 逐点对比的最大绝对误差量级（float 单精度约 1e-6 级）。

**预期结果**：打印 `0.003906 0.000000` 一类数值（即 1/256），证实「不含 1/N 的反变换」约定——这正是 `transform_domain` 里 L264 要除以 `FFT_SIZE` 的原因。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `fft256_inverse` 之后必须除以 256？这个约定给谁省了事？

**答案**：Nayuki 实现（下标对调技巧）算出的是未归一化的 N×IDFT。把 1/N 留给调用方，正反变换共用同一个未缩放核心，代码最短；`transform_domain` 在写回 `measured` 时统一除以 `FFT_SIZE`（main.c:264）。

**练习 2**：旋转因子每次现算 `cos/sin` 而不查表，得失是什么？

**答案**：得——省掉一张 128 项 float 表（512 字节 RAM 或 512 字节 Flash）与建表代码，结构更简单；失——每个蝶形两次超越函数调用，在无 FPU 的 M0 上是软浮点库调用，速度明显慢。对每轮扫频只做 2 次（两通道）256 点 IFFT 的 NanoVNA 来说，速度够用，体积优先。

**练习 3**：`fft256` 为什么能保证「原位」计算而不需要额外缓冲？

**答案**：先做位反转重排，使每个蝶形的两个输入在上一级结束后已经就位（下标 j 与 j+halfsize），输出写回同一位置；radix-2 按时间抽取算法的递推结构保证了这一性质。

### 4.4 kaiser_window 与 bessel0：窗函数

#### 4.4.1 概念说明

Kaiser 窗是「可调窗」：一个 β 参数在「矩形窗（高分辨率、差旁瓣）」与「重衰减窗（低分辨率、好旁瓣）」之间连续滑动。它由零阶修正贝塞尔函数 \( I_0 \) 构造，接近同主瓣宽度下的最优（最小旁瓣能量）窗，所以从 MATLAB 到 scipy 都拿它当默认可调窗。固件三档菜单直接映射 β：minimum→0（矩形）、normal→6、maximum→13。β=6 时第一旁瓣约 −44 dB，β=13 时约 −108 dB——对「在强反射旁瓣里找弱反射」的 TDR 用途，差别是实质性的。

`bessel0` 用幂级数迭代计算 \( I_0 \)：相邻项比值 \( \frac{t_m}{t_{m-1}} = \frac{x^2}{4m^2} \) 极其简单，逐项累加直到新增项小于累计值的 \( 10^{-4} \) 倍（相对误差控制），既不需要 math.h 的贝塞尔函数（嵌入式库根本没有），也没有指数溢出风险。

#### 4.4.2 核心流程

```
kaiser_window(k, n, β):
  β == 0 ? 直接返回 1.0（矩形窗，跳过所有计算——扫频时省 CPU）
  r = 2k/(n−1) − 1        # k 扫过 0..n−1，r 从 −1 到 +1
  return bessel0(β·√(1−r²)) / bessel0(β)
  # 两端 r=±1 → 比值 → 0；中心 r=0 → I0(β)/I0(β) = 1

bessel0(x):
  ret=0, term=1, m=0
  循环: ret += term; m++; term ×= x²/(4m²)
  直到 term < 1e-4·ret      # 级数收敛快（x 最大 13 时约迭代 20 次）
```

#### 4.4.3 源码精读

- [main.c:L169-L184](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L169-L184)：`bessel0` 幂级数。注意判据 `term > eps * ret` 是**相对**判据（与已累计值比较），不是绝对误差——对 x=13 这种大参数也能自动多迭代几项；`term *= (x*x)/(4*m*m)` 一行同时完成了 \( (m!)^2 \) 分母的递推，避免了阶乘溢出。
- [main.c:L186-L192](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L186-L192)：`kaiser_window` 与教科书公式逐项对应；`beta == 0.0` 的早退让矩形窗档位完全绕开贝塞尔计算（每轮扫频 2 通道 × 101 点次调用）。
- 常数 2.01 / 2.92 的出处：它们分别是 `1/mean(kaiser(202,6))` 与 `1/mean(kaiser(202,13))`，以注释形式固化在 [main.c:L213-L219](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L213-L219)。Kaiser 窗的平均值有个良好性质：对足够长的窗，mean 几乎不随窗长变化（101 与 202 点算出来几乎相同），所以带通模式直接 `×2` 复用同一对常数即可。

#### 4.4.4 代码实践

**实践目标**：亲手算出 2.01 / 2.92，并观察三档窗的时域形状差异。

**操作步骤**（示例代码）：

```python
import numpy as np
from math import sqrt

def bessel0(x, eps=1e-4):               # 逐行翻译 main.c L169-184
    ret, term, m = 0.0, 1.0, 0
    while term > eps * ret:
        ret += term; m += 1
        term *= (x*x) / (4*m*m)
    return ret

def kaiser(k, n, beta):                 # 逐行翻译 main.c L186-192
    if beta == 0.0: return 1.0
    r = (2*k)/(n-1) - 1
    return bessel0(beta*sqrt(1-r*r)) / bessel0(beta)

for beta in (0.0, 6.0, 13.0):
    w = np.array([kaiser(k, 202, beta) for k in range(202)])
    w101 = np.array([kaiser(k, 101, beta) for k in range(101)])
    print(f"β={beta:4.0f} mean(202)={w.mean():.4f} 1/mean={1/w.mean():.3f}"
          f" | mean(101)={w101.mean():.4f}")
```

**需要观察的现象**：① 自写 `bessel0` 与 `scipy.special.i0` 的偏差（可加一行对比，预期相对误差 ≤ 1e-4 量级，与 eps 一致）；② `mean(kaiser(101,β))` 与 `mean(kaiser(202,β))` 是否几乎相等；③ 把三条窗曲线画出来，β=13 的窗「有效宽度」明显变窄。

**预期结果**：`1/mean` 分别约 `1.000 / 2.01 / 2.92`；101 点与 202 点的 mean 在小数点后三位内一致——这解释了固件敢于用同一常数配两种窗长的原因。待本地验证（精确到小数点后几位的偏差取决于浮点顺序）。

#### 4.4.5 小练习与答案

**练习 1**：三档窗口分别适合什么场景？

**答案**：minimum（矩形）——距离分辨率优先、被测件反射强且单一（如天线谐振点定位）；normal（β=6）——通用折中，约 −44 dB 旁瓣；maximum（β=13）——要在强反射旁边分辨弱反射（如电缆中段的小故障），愿意用分辨率换 −100 dB 级旁瓣抑制。

**练习 2**：`bessel0` 的收敛判据为什么用相对误差而不是绝对误差？

**答案**：\( I_0(13) \approx 1.9\times10^4 \)（对 β=13 的分母项），若用绝对阈值 1e-4 会过早截断大参数情形、丢掉有效数字；相对判据 `term < eps·ret` 使任意 x 下的截断误差都约为结果 的 1e-4 倍，数值稳定性更好。

**练习 3**：如果把 `kaiser_window` 的 `beta == 0.0` 早退删掉，功能还正确吗？

**答案**：数学上正确——β=0 时 \( I_0(0)=1 \)，比值恒为 1，恰是矩形窗。早退是性能优化：跳过两次 `bessel0` 调用与一次 `sqrt`。但注意 `while (term > eps*ret)` 在 ret=0 起步时若 eps*ret=0 会死循环？不会——首循环 ret=1>0 后才可能退出，但依赖初值的写法值得在阅读时多看一眼（实际首项 term=1 > 0·eps=0 成立，会正常进入循环，逻辑无误）。

## 5. 综合实践

**任务：在 PC 上做一台「软件 TDR」，定位一段模拟电缆中的故障，并量化窗档位与扫频带宽的取舍。**

要求把本讲四个模块串起来（全程 numpy + matplotlib，无需真机）：

1. **建模**：一段 2 米好电缆（`velocity_factor=0.7`）末端接一个 5 米分支处的并联电容（模拟接头进水），再延长 1 米开路。用传输线公式或简化模型生成 101 点复数 Γ(f)（提示：总反射 = 各反射点贡献按 \( e^{-2j\pi f \tau_i} \) 相干叠加，\( \tau_i \) 为各点往返时延——这正好复用 u2-l1 里 Γ 的复数本质）。
2. **复现变换**：用 4.2.4 的 `transform_domain()`，以低通阶跃模式（扫频设 0~30 MHz，`Δf=300 kHz`）得到距离域响应，画出 0~15 m 曲线，标注台阶位置，验证与建模距离一致（`distance_of_index` 公式：`idx·c·0.7/(Δf·256·2)`）。
3. **量化取舍**：
   - 三档窗口（β=0/6/13）下，测量「5 m 处小台阶」与「最强旁瓣」的幅度比，填一张 3 行对比表；
   - 把扫频上限改成 300 MHz（`Δf=3 MHz`），观察分辨率提升与最大无模糊距离（\( 1/\Delta f \) 对应的时窗）缩短的矛盾。
4. **（可选，真机）**：有 NanoVNA 的读者可 `transform on` + `transform step`，对一条已知长度的同轴电缆实测，用 marker 读故障距离，与尺子量的结果对比（记得速度因子不是 0.7 时先在 Device 菜单改 `velocity factor`）。

预期成果：一张距离域曲线图 + 一张「窗口 × 信噪」对比表 + 一段 200 字左右的结论（什么带宽、什么窗、什么模式下你的 TDR 分辨率/量程最优）。

## 6. 本讲小结

- NanoVNA 的时域功能 = 对 101 点频域 Γ 做 256 点 IFFT：频域扫频数据本身就是系统冲击响应的频谱采样，反变换即得时域响应（频域 TDR）。
- 三种模式对应两种频谱构造：带通原样用复数频谱（不要求从直流开始）；低通冲击/阶跃用共轭对称（Hermitian）填满 256 bin 得到实数响应，阶跃模式再对冲击响应做前缀和。
- `domain_mode` 一个字节打包「域/模式/窗」三个字段（nanovna.h:68-80），经 `transform` 命令或屏幕菜单修改，随 `properties_t` 掉电保存；变换在 sweep 线程每轮扫频完成后原地覆盖 `measured[]`。
- Kaiser 窗由 `bessel0` 幂级数支撑，β=0/6/13 对应矩形/普通/最大三档，在分辨率与旁瓣之间取舍；低通模式把测量点放在 202 点窗的后半段以保护直流分量。
- 提交 4d64ef6 的 `wincorr = N_fft/(N_win·mean(w))` 补偿零填充与加窗的幅度损失（带通+普通窗约 14.2 dB），使时域读数与其他 VNA 对齐；阶跃模式刻意不补偿。
- `fft.h` 是 Nayuki 的 radix-2 原位 FFT，用「交换实虚部下标」把正变换循环复用成反变换，结果为 N×IDFT，调用方除以 256 归一化。

## 7. 下一步学习建议

本讲之后，数据链路（采集→解调→校准→变换）已经完整闭环。建议下一步：

1. **进入显示层**：`plot_into_index` 如何把 `measured[]`（现在是时域数据）映射为屏幕坐标——阅读 u4-l2（轨迹系统），留意时域下 x 轴语义切换（plot.c 的 `time_of_index`）但绘图代码零改动这一设计优点。
2. **看数据的另一条出口**：u5-l2（Python 上位机）——你可以用 `scan`/`data` 命令把 101 点复数 Γ 拉到 PC，再用本讲的 numpy 代码做离线时域变换，与固件结果互相对拍。
3. **资源视角回看本讲**：u5-l3 会讨论 `spi_buffer` 复用、`fft.h` 现算旋转因子等「体积 vs 速度」的取舍，本讲的 4.2.3/4.3.5 正是那讲的伏笔。
4. 若想深入数学：阅读 Nayuki 原项目文档（fft.h 文件头链接）了解 radix-2 之外的选择，以及 chirp-z 变换（非 2 幂点数、非均匀网格的频域→时域方法，商用 VNA 的底层算法）。
