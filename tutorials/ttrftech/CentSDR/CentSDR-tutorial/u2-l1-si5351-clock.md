# 本振之源：SI5351 时钟发生器驱动

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 SI5351 内部「晶振 → PLL → Multisynth → R 分频器 → CLKx 引脚」的信号链，以及每一级在 [si5351.h](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.h#L1-L56) 中对应哪些寄存器。
2. 解释 PLL 反馈分频与输出分频的数学公式，并亲手从 `div/num/denom` 算出 `P1/P2/P3` 三个寄存器值（这是本讲的核心）。
3. 对比 `fixedpll`（PLL 固定、输出级分数分频）与 `fixeddiv`（输出级整数分频、PLL 浮动）两种合成策略，说明它们各自适用的频段以及为什么频段边界是 100MHz 和 150MHz。
4. 说明 `set_tune()` 中「4 倍频」与 `mode_freq_offset` 补偿的用意。
5. 理解为什么 SI5351 的第一次初始化发生在 RTOS 内核启动之前，用的是 [si5351_low.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351_low.c#L74-L107) 里的裸机 I2C。

## 2. 前置知识

### 2.1 什么是本振（LO）

超外差 / 零中差接收机都离不开**本机振荡器**（Local Oscillator，LO）。混频器把天线收到的射频信号与本振信号相乘，把感兴趣的频段搬到低频（中频或基带）。本讲的主角 SI5351 就是 CentSDR 的本振发生器：它是一颗由 I2C 配置的「任意频率时钟发生器」，输出频率可以低至几 kHz、高至 200MHz 以上。

在 u1-l1 已经建立的整体链路中，SI5351 输出的时钟送给**正交检波器**，产生 I/Q 两路基带信号。代码层面能确认的事实是：`set_tune()` 写给 SI5351 的频率是目标频率的 4 倍（见 4.5 节）；仓库不含硬件设计资料，外部如何用这 4 倍频得到 0°/90° 正交本振，具体电路**待确认**（可在 doc/centsdr-blockdiagram.png 框图中查看）。

### 2.2 PLL 与小数分频

锁相环（PLL）能把一个固定频率的晶振「倍频」成一个可变的更高频率。SI5351 内部有两颗 PLL（PLLA/PLLB），每颗都是一个反馈分频比为

\[ f_{PLL} = f_{XTAL} \times \left(a + \frac{b}{c}\right) \]

的小数锁相环：\(a\) 是整数倍数，\(b/c\) 是小数部分。当 \(b = 0\) 时是整数模式，相位抖动最小；\(b \neq 0\) 时小数部分靠「脉冲吞除」技巧平均出来，会引入小数杂散。

SI5351 的晶振在 CentSDR 上是 **26MHz**（[si5351.c:190](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L190-L192) 的 `XTALFREQ 26000000L`；注意 [si5351.h:54](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.h#L54) 里那个 25MHz 的宏并没有被用到）。

### 2.3 Multisynth 与 R 分频

PLL 之后还有第二级分频器，叫 **Multisynth**（MS0~MS2，分别对应输出引脚 CLK0~CLK2），同样支持 \(a + b/c\) 的小数分频。它的输出再经过一个可选的 **R 分频器**（1/2/4/.../128，纯二的幂）进一步降低频率。所以最终输出为

\[ f_{OUT} = \frac{f_{PLL}}{\left(a + \frac{b}{c}\right)_{MS}} \times \frac{1}{R} \]

两级分频组合起来，就构成了「固定 PLL、浮动 MS」（fixedpll）和「浮动 PLL、固定 MS」（fixeddiv）两种策略——这是 4.3 节的主线。

### 2.4 I2C 寄存器写模型

SI5351 的全部控制都通过 I2C 写寄存器完成，格式是「寄存器地址 + 数据字节」。CentSDR 上它挂在 I2CD1，7 位地址 `0x60`。运行期用 ChibiOS 的 I2C 驱动（带总线互斥），开机首次初始化则用轮询式裸机 I2C（4.4 节）。

## 3. 本讲源码地图

| 文件 | 角色 | 关键内容 |
| --- | --- | --- |
| [si5351.h](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.h#L1-L70) | 寄存器/位定义与函数原型 | PLL 选择、R 分频宏、寄存器地址、CLK 控制位 |
| [si5351.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L1-L295) | 运行期驱动（基于 ChibiOS I2C） | `setupPLL`/`setupMultisynth` 的 P1/P2/P3 编码、`fixedpll`/`fixeddiv`、`si5351_set_frequency` 频段调度 |
| [si5351_low.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351_low.c#L1-L107) | 先于内核的裸机初始化 | 手写 RCC/GPIO/I2C 寄存器操作、`(len, reg, data...)` 哨兵配置表 |
| [main.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L165-L201) | 调用方 | `mod_table`（含各模式 `freq_offset`）、`set_tune` 的 4 倍频 |
| [NANOSDR_STM32_F303/board.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/NANOSDR_STM32_F303/board.c#L65-L75) | 板级入口 | `__early_init()` 在内核启动前调用 `si5351_setup()` |
| [nanosdr.h](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L126-L128) | 全局头 | `AM_FREQ_OFFSET 10000`、`PHASESTEP` 宏 |

## 4. 核心概念与源码讲解

### 4.1 认识 SI5351：信号链架构与寄存器地图

#### 4.1.1 概念说明

把 SI5351 想成一条三级流水线：

```
26MHz 晶振 ──→ PLL A / PLL B（小数倍频，600~900MHz）
                    │
                    ├──→ Multisynth 0 ──→ R 分频 ──→ CLK0
                    ├──→ Multisynth 1 ──→ R 分频 ──→ CLK1   ← CentSDR 运行期本振输出
                    └──→ Multisynth 2 ──→ R 分频 ──→ CLK2   ← 裸机初始化默认输出
```

两条硬性约束决定了后面所有的频段划分：

- **PLL 输出必须在 600~900MHz 之间**（ datasheet 规定，太小锁不住、太大杂散大）。
- **Multisynth 分频比必须在 8~1800 之间**，且 ÷4、÷6 是特殊模式（[si5351.c:106](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L106) 的注释也写明了 `div // 4,6,8, 8+ ~ 900`）。

#### 4.1.2 核心流程

运行期驱动与芯片的每一次交互都是「算出 P1/P2/P3 → 打包成 9 字节 → 一次 I2C 批量写」。读写入口只有两个薄封装：

```c
si5351_write(reg, dat)        // 写 1 个寄存器：[reg, dat]
si5351_bulk_write(buf, len)   // 连续写 len 字节：[首寄存器地址, 数据...]
```

#### 4.1.3 源码精读

头文件前半部分定义了三级流水线的「旋钮」：

- [si5351.h:L1-L14](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.h#L1-L14) — 这段定义 PLL A/B 选择符、R 分频档位宏（`SI5351_R_DIV_1` 到 `SI5351_R_DIV_128`，编码为 3 位档号左移 4 位）以及 `SI5351_DIVBY4`（÷4 特殊模式标志）。注意 R 分频宏的值 `(n<<4)`，它们会直接拼进 MS 寄存器的 reg[3] 字节。
- [si5351.h:L17-L25](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.h#L17-L25) — 这段是寄存器地址表：输出使能（3）、三个 CLK 控制（16~18）、两个 PLL 参数组（26 起、34 起）、三个 Multisynth 参数组（42/50/58 起）。每组参数占 **8 个连续寄存器**，所以一次 bulk 写 9 字节（地址 + 8 数据）就能配完一级分频器。
- [si5351.h:L27-L42](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.h#L27-L42) — 这段是 CLKx 控制寄存器的位定义：电源关断（bit7）、整数模式标志（bit6）、选 PLL B（bit5）、时钟源（bit3:2，`SI5351_CLK_INPUT_MULTISYNTH_N` 表示取本通道 Multisynth）、驱动强度（bit1:0，2/4/6/8mA）。
- [si5351.c:L6-L23](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L6-L23) — 这两个函数是全部 I2C 访问的唯一出口：先 `i2cAcquireBus` 拿总线互斥，`i2cMasterTransmitTimeout` 发送，再释放。因为 shell 线程、UI 线程都可能触发改频率，互斥是必须的。

#### 4.1.4 代码实践

1. **实践目标**：确认「一次 bulk 写 = 一级分频器的全部参数」。
2. **操作步骤**：数一数 [si5351.c:L90-L100](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L90-L100) 中 `setupPLL` 打包的 `reg[9]`：1 个地址 + 8 个数据字节；再对照 [si5351.h:L21-L25](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.h#L21-L25) 的地址表，验证 PLL A 组从 26 开始、MS1 组从 50 开始。
3. **需要观察的现象**：每组恰好 8 个数据寄存器，与 datasheet 中 P1(18bit)+P2(20bit)+P3(20bit) 的位宽合计（58 bit，加上保留位凑成 8 字节）吻合。
4. **预期结果**：能徒手画出「寄存器 26~33 = PLL A 参数、42~49 = MS0、50~57 = MS1」的地址地图。无需硬件。

#### 4.1.5 小练习与答案

**练习 1**：`SI5351_R_DIV_64` 的宏值是多少？它最终被拼进哪个字节？
答案：`(6<<4) = 0x60`。它出现在 `setupMultisynth` 的 reg[3]（[si5351.c:L161](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L161)），与 P1 的高 2 位和 DIVBY4 标志共用一个字节。

**练习 2**：为什么 `si5351_write`/`si5351_bulk_write` 要 `i2cAcquireBus`/`i2cReleaseBus`，而 si5351_low.c 里的裸机发送不需要？
答案：运行期有多个线程（shell 的 tune 命令、UI 旋钮）可能并发访问 I2CD1，ChibiOS 的总线锁防止两笔传输交织；开机裸机阶段是单线程顺序执行，不存在并发，直接轮询寄存器即可。

### 4.2 P1/P2/P3：把分频比编码进寄存器

#### 4.2.1 概念说明

SI5351 不接受「给我分频 29 又 21/71」这样的人话，它只认三个整数寄存器 P1（18 位）、P2（20 位）、P3（20 位）。对分频比 \(a + \frac{b}{c}\)，编码公式为（对 PLL 反馈级和输出 Multisynth 级**完全相同**）：

\[
P_1 = 128a + \left\lfloor \frac{128b}{c} \right\rfloor - 512
\]

\[
P_2 = 128b - c \cdot \left\lfloor \frac{128b}{c} \right\rfloor,\qquad P_3 = c
\]

直觉解释：芯片内部把小数部分放大 \(2^7 = 128\) 倍存储——\(P_3 = c\) 是分母，\(P_2\) 是 \(128b\) 对 \(c\) 取模的余数，两者之比 \(P_2/P_3 = (128b \bmod c)/c\) 精确还原小数部分；\(P_1\) 则把整数部分同样乘 128 并减去一个固定的 512 偏置（datasheet 规定，与内部计数器预装值有关）。整数模式（\(b=0\)）退化为 \(P_1 = 128a - 512,\ P_2 = 0,\ P_3 = 1\)。

#### 4.2.2 核心流程

```
输入: a(div/mult), b(num), c(denom)
若 a==4 的输出级: 置 DIVBY4 标志, P1=P2=0, P3=1
否则若 b==0:     整数模式 P1=128a-512, P2=0, P3=1
否则:            分数模式, 按上面公式
打包 9 字节: [基址, P3>>8, P3&0xff, P1[17:16]|div4|rdiv, P1[15:8], P1[7:0],
              P3[19:16] 拼 P2[19:16], P2[15:8], P2[7:0]]
再写 1 字节 CLKx 控制寄存器（PLL 选择、整数模式标志、驱动强度）
```

#### 4.2.3 源码精读

- [si5351.c:L47-L101](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L47-L101) — `si5351_setupPLL`：这段代码先在注释里给出 datasheet 的三个公式（[L61-L69](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L61-L69)），然后分整数/分数两个分支算 P1/P2/P3。注意 [L83](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L83) 用 `(128 * num) / denom` 这种**纯整数运算**实现 `floor`，避免在固件里引入浮点（被注释掉的浮点版本就留在上一行作对照）。[L92-L99](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L92-L99) 把三个数按位拆进 9 字节缓冲，其中 reg[6] 一个字节里同时装了 P3 的高 4 位和 P2 的高 4 位。
- [si5351.c:L103-L176](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L103-L176) — `si5351_setupMultisynth`：与 PLL 级公式相同，但多了两个输出级特有的东西。其一是 [L140-L143](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L140-L143) 的 `div == 4` 特判——÷4 是唯一支持 >150MHz 输出的高速模式，参数清零、只置 `SI5351_DIVBY4` 标志；其二是 [L169-L175](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L169-L175) 顺手写 CLKx 控制寄存器：驱动强度 | 时钟源选本通道 MS |（选 PLL B 时加 bit5）|（num==0 时加整数模式标志）。
- [si5351.c:L41-L45](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L41-L45) — `si5351_reset_pll`：写寄存器 177 让 PLL 重新锁相。改 PLL 参数后必须复位一次，否则芯片可能工作在旧的锁定状态。

#### 4.2.4 代码实践

1. **实践目标**：验证「裸机初始化表里的神秘字节」就是 P1/P2/P3 编码。
2. **操作步骤**：手算 PLL A 整数 32 倍频（832MHz）的 P1：\(P_1 = 128 \times 32 - 512 = 3584 = \) `0x0E00`。拆字节得 reg[3]=0x00、reg[4]=0x0E、reg[5]=0x00。再对照 [si5351_low.c:L80](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351_low.c#L80) 那行 `9, SI5351_REG_26_PLL_A, /*P3*/0, 1, /*P1*/0, 14, 0, ...` 中的 `0, 14, 0`。
3. **需要观察的现象**：0x0E00 的高字节 0x0E = 十进制 14，与配置表中的 `14` 完全一致；同理 MS2 整数 104 分频的 \(P_1 = 128\times104-512 = 12800 = \) `0x3200`，高字节 `0x32` = 50，对应表中的 `50`。
4. **预期结果**：确认 si5351_low.c 配置表与 si5351.c 的编码公式是同一套数学，只是前者把结果预先手算成了字面量（注释 `32/2-2=14`、`104/2-2=50` 是对高字节值的粗略记法）。无需硬件，纸上即可完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `P1` 公式要减 512？
答案：这是 datasheet 规定的编码偏置（与芯片内部计数器的预装值有关）。固件侧只需照做；从代码看 [si5351.c:L75](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L75) 整数分支与 [L83](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L83) 分数分支都统一减 512。

**练习 2**：分数模式下 \(P_2/P_3\) 还原的是 (128b mod c)/c 而不是 b/c，为什么最终分频比仍精确等于 \(a + b/c\)？
答案：芯片内部以 1/128 步长做脉冲吞除，\(P_1\) 携带 \(\lfloor 128b/c \rfloor\) 个整步，\(P_2/P_3\) 携带剩余的小数步，两者之和恰为 \(128 \times b/c\)，平均效果精确等于 \(b/c\)（长期平均意义上，瞬时相位有小数纹波）。

**练习 3**：`setupMultisynth` 的 reg[3] 字节里最多可能同时有哪几类信息？
答案：P1 的 bit17:16、DIVBY4 标志（bit3:2）、R 分频档位（bit7:4 的低 3 位有效），见 [si5351.c:L161](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L161)。

### 4.3 两种合成策略与频段切换：fixedpll、fixeddiv 与 si5351_set_frequency

#### 4.3.1 概念说明

要让 CLK 输出频率 \(f\)，可以自由选择「哪一级动、哪一级固定」：

- **fixedpll（PLL 固定 832MHz，输出 MS 分数分频）**：PLL 永远锁在 \(26\text{MHz} \times 32 = 832\text{MHz}\) 的**整数模式**，零小数抖动；改频率只动输出 Multisynth，PLL 不需要重锁。代价是输出级只能做分数分频。适合 832MHz 以下（分频比 ≥ 8.32 ≥ 8）的频率，也就是 band 0（≤100MHz）——覆盖 CentSDR 全部预置信道（最高 26.8MHz），**实际整机永远走这条路**。
- **fixeddiv（输出 MS 整数分频，PLL 浮动）**：输出级用整数 ÷6 或 ÷4（÷4 即 DIVBY4 高速模式），小数部分全部塞给 PLL。PLL 必须 \(= f \times \text{div}\) 且落在 600~900MHz：
  - ÷6 → \(f \in (100, 150)\text{MHz}\) 时 PLL ∈ (600, 900)MHz ✓（band 1）
  - ÷4 → \(f \in [150, 225)\text{MHz}\) 时 PLL ∈ [600, 900)MHz ✓（band 2）

  100MHz 和 150MHz 两个频段边界，正是被「PLL 必须在 600~900MHz」这一条硬件约束逼出来的。

#### 4.3.2 核心流程

`si5351_set_frequency(freq)`（freq 已是 4 倍中心频率）的调度逻辑：

```
1. 定频段: freq ≤ 100MHz → band 0；< 150MHz → band 1；否则 band 2
2. 定 R 分频: freq ≤ 500kHz → ÷64；≤ 4MHz → ÷8；否则 ÷1
3. band 0: 先把 freq 预乘 R 倍率, 再 fixedpll(CLK1, PLL_A, 832MHz, freq')
           → MS 分数分频, R 在片内再除回来
   band 1: fixeddiv(CLK1, PLL_B, freq, div=6)   // 若从 band 2 切来, 先多写一次 PLL
   band 2: fixeddiv(CLK1, PLL_B, freq, div=4)   // DIVBY4 模式
4. 若 band 发生变化 → si5351_reset_pll() 让 PLL 重锁
```

R 分频的用意：Multisynth 直接输出低于几 MHz 的频率时，分频比会逼近 1800 上限、分数粒度也变差；先把目标频率乘 8 或 64、让 MS 工作在 ≥4MHz 的「舒适区」（分频比大约 8~208），再让片内 R 分频器除回来，最终频率不变。

`fixedpll` 内部的整数化技巧（[si5351.c:L194-L210](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L194-L210)）：

```
div   = 832MHz / freq                    // 整数部分
num   = 832MHz - freq * div              // 余数
denom = freq
k = gcd(num, denom); num /= k; denom /= k // 约分, 让 num/denom 最简
while (denom >= 2^20) { num>>=1; denom>>=1 }  // P2/P3 只有 20 位, 超限就牺牲精度
```

由于 num/denom 是精确有理数，合成出的输出频率**理论上分毫不差**（只要约分后的 denom 没超 20 位；右移循环是精度兜底，会引入微小偏差）。

#### 4.3.3 源码精读

- [si5351.c:L178-L188](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L178-L188) — 辗转相除法求最大公约数，服务于上面的约分。
- [si5351.c:L190-L192](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L190-L192) — 定义晶振 26MHz、PLL 固定倍数 32、PLL 频率 832MHz。这一行是 fixedpll 策略的「锚点」。
- [si5351.c:L194-L210](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L194-L210) — `si5351_set_frequency_fixedpll` 全文：按上述算法算出 div/num/denom 后只调 `setupMultisynth`，**完全不碰 PLL 寄存器**——这就是「PLL 固定」的含义，也是 band 0 内连续改频率不需要 PLL 复位的原因。
- [si5351.c:L212-L229](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L212-L229) — `si5351_set_frequency_fixeddiv` 反其道而行：`pllfreq = freq * div`，把 PLL 配成 \(26\text{MHz} \times (multi + num/denom)\)（分数在 PLL 侧），输出 MS 用 `num=0` 的**整数分频**（[L228](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L228) 传 0 和 1）。输出级整数分频杂散更小，这是高频段把分数挪去 PLL 侧的动机。
- [si5351.c:L235-L292](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L235-L292) — `si5351_set_frequency` 频段调度器。[L240-L251](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L240-L251) 定 band 与 rdiv；[L258-L284](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L258-L284) 按策略分发，其中 band 1 分支里那句「从 band 2 切来时先写一次 PLL、再写一次」([L271-L277](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L271-L277)) 是针对 DIVBY4→÷6 切换的稳定性 workaround，代码注释原话是 `Set PLL twice on changing from band 2`；[L286-L291](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L286-L291) 只在跨频段时复位 PLL，并更新 `current_band` 静态变量避免同段内重复复位。
- [si5351.c:L231-L233](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L231-L233) — 这两行把驱动强度统一定义为 `SI5351_CLK_DRIVE_STRENGTH_8MA` 并用静态变量 `current_band` 记住当前频段——高频时钟要走 PCB 走线，强驱动保证边沿质量；`current_band` 则用于判断是否跨频段、避免同段内重复复位 PLL。

#### 4.3.4 代码实践

1. **实践目标**：验证 fixeddiv 的参数计算，并理解频段边界的来历。
2. **操作步骤**：纸上为 `si5351_set_frequency(120000000)`（band 1）推算 `si5351_set_frequency_fixeddiv(1, PLL_B, 120000000, 6, 8mA)` 的参数：`pllfreq = 120e6 × 6 = 720e6`；`multi = 720e6 / 26e6 = 27`；`num = 720e6 − 27×26e6 = 18000000`；`denom = 26000000`；`k = gcd(18000000, 26000000) = 2000000` → `num=9, denom=13`。
3. **需要观察的现象**：验证 \(26\text{MHz} \times (27 + 9/13) = 702\text{MHz} + 18\text{MHz} = 720\text{MHz}\) 精确成立，且 720MHz 落在 600~900MHz 窗口内。
4. **预期结果**：参数为 `multi=27, num=9, denom=13`，输出 MS 为整数 ÷6，最终 \(720\text{MHz}/6 = 120\text{MHz}\)。无需硬件；若要机上验证，CentSDR 的接收范围实际到不了 120MHz，此题仅作纸面推演（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 band 0 内连续改变接收频率时不触发 `si5351_reset_pll`？
答案：fixedpll 只改写输出 Multisynth 的 P1/P2/P3，PLL 始终锁在 832MHz 整数模式，锁定状态不受影响；[L286](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L286) 的 `current_band != band` 判断保证只有跨频段（PLL 参数真的变了）才复位。

**练习 2**：`fixedpll` 里 `while (denom >= (1<<20))` 右移循环什么时候会真正执行？损失了什么？
答案：当约分后的 denom 仍超过 20 位（>1048576）时执行。P2/P3 寄存器只有 20 位放不下，只能同时右移分子分母、丢弃最低位精度，输出频率会出现微小偏差。对 CentSDR 的 HF 频段（几十 MHz 以下），约分后 denom 通常远小于该限，循环一般不执行。

**练习 3**：为什么 `fixedpll` 的 `denom` 初值直接取 `freq` 而不是随便一个数？
答案：余数 num 与 freq 的比值本就是精确的有理数 \((832\text{MHz} \bmod freq)/freq\)，取 denom=freq 再用 gcd 约分，得到的是**精确最简**分数；随便选一个 denom（比如固定 1000）就会引入量化误差，输出频率不再严格等于目标值。

### 4.4 先于操作系统：si5351_low.c 裸机 I2C 初始化

#### 4.4.1 概念说明

u1-l3 讲过：`main()` 之前，板级钩子 `__early_init()` 已经把 SI5351 预配好并切换了系统时钟。那时 ChibiOS 内核还没启动，当然也没有 I2C 驱动可用——所以 [si5351_low.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351_low.c#L1-L107) 用「直接写 STM32 寄存器 + 轮询标志位」的方式实现了一个最小 I2C 发送器。为什么这么急？因为系统时钟切换依赖 SI5351 的输出（它同时是 MCU 时钟源链路的一环），必须**先有时钟、后有系统**。

#### 4.4.2 核心流程

```
__early_init()                     (board.c, 内核启动前)
 ├─ si5351_setup()                  (si5351_low.c)
 │   ├─ rcc_gpio_init()   复位并使能 I2C1/PWR/GPIOB 时钟, PB8/PB9 配复用开漏
 │   ├─ i2c_init(I2C1)    100kHz@8MHz(HSI), 7 位地址, 模拟滤波
 │   └─ si5351_init_bulk() 按 si5351_configs 表逐条发送
 └─ stm32_clock_init()    之后才切主时钟
```

配置表 `si5351_configs` 采用 `(len, reg, data...)` 三元组序列、`0` 结尾哨兵的编码，逐条完成：关全部输出 → 三路 CLK 掉电 → 晶振负载电容 8pF → **PLL A = 26MHz×32 = 832MHz 整数模式** → 复位 PLL → **MS2/CLK2 = 832/8 = 8MHz 整数输出** → CLK2 控制字（2mA|MS 源|整数模式）→ 重新打开输出。开机默认本振就是这 8MHz。

#### 4.4.3 源码精读

- [si5351_low.c:L6-L25](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351_low.c#L6-L25) — `rcc_gpio_init`：先整块复位 AHB/APB1/APB2 外设（写全 1 再清零），再使能 PWR、I2C1、GPIOB 时钟；`RCC->CFGR3 |= RCC_CFGR3_I2C1SW_HSI` 把 I2C1 的时钟源选为 **HSI 内部 8MHz**——这一步是关键，后面 `stm32_clock_init()` 改主频也不影响 I2C 时序；最后 PB8/PB9 配成复用 4、开漏输出。
- [si5351_low.c:L27-L56](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351_low.c#L27-L56) — `i2c_init`：写死 `TIMINGR = 0x10420F13`（注释注明 100kHz@8MHz，对应 HSI），7 位地址模式、开模拟滤波、最后置 PE 位使能。没有中断、没有线程，一切靠轮询。
- [si5351_low.c:L58-L71](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351_low.c#L58-L71) — `i2cSendByte`：一次 CR2 写入同时给出从机地址、字节数、START 和 AUTOEND（发完自动产生 STOP），然后 `while (!(i2c->ISR & I2C_ISR_TXIS))` 死等发送缓冲空、逐字节写入 TXDR。
- [si5351_low.c:L74-L88](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351_low.c#L74-L88) — `si5351_configs` 哨兵表本体。每个条目第一个字节是本条 I2C 报文长度，随后是寄存器地址和数据。注释 `26MHz * 32 = 832MHz : 32/2-2=14` 与 `832MHz/8MHz=104,104/2-2=50` 记录的正是 4.2.4 节验证过的 P1 高字节值。
- [si5351_low.c:L90-L99](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351_low.c#L90-L99) — `si5351_init_bulk`：`while (*p)` 读长度、发送、指针前移——直到哨兵 0 终止，一个循环把整张表灌进芯片。
- [NANOSDR_STM32_F303/board.c:L65-L75](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/NANOSDR_STM32_F303/board.c#L65-L75) — `__early_init` 先 `si5351_setup()` 再 `stm32_clock_init()`，顺序不可颠倒：I2C 时序按 HSI 8MHz 算好，而时钟切换后系统进入最终主频，本振此时已在输出。

值得注意的一处不对称：裸机初始化配置的是 **CLK2/MS2**（8MHz 默认），而运行期 `si5351_set_frequency` 固定用 **channel 1（CLK1/MS1）**（[si5351.c:L266](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L266)、[L273](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L273)、[L282](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L282) 都传 1）。两路输出各通向哪里、开机阶段哪一路在驱动正交检波器，仓库不含硬件资料，**待确认**。

#### 4.4.4 代码实践

1. **实践目标**：读懂哨兵表的编码并逐条翻译。
2. **操作步骤**：对照 [si5351.h:L17-L52](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.h#L17-L52) 的寄存器/位定义，把 `si5351_configs` 的每一条目翻译成一句话，例如第 2 条 =「写寄存器 16~18（CLK0~2 控制）为 POWERDOWN」，第 3 条 =「写寄存器 183 晶振负载 8pF」。
3. **需要观察的现象**：所有条目长度加起来恰为一次状态机式的开机序列：先关后配再开。
4. **预期结果**：得到一张 8 行左右的「开机动作清单」，并能指出哪一条对应 832MHz、哪一条对应 8MHz。无需硬件。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `i2c_init` 要把 I2C1 时钟源选成 HSI？
答案：HSI 是内部 8MHz 振荡器，不受 `stm32_clock_init()` 切换主频影响；TIMINGR 按 8MHz 算好的时序在时钟切换前后都成立，保证裸机阶段配置完、内核接管后 ChibiOS I2C 驱动重新初始化前也能正常通信。

**练习 2**：哨兵表结尾的 `0` 起什么作用？如果某条报文长度恰好为 0 会出现什么问题？
答案：`si5351_init_bulk` 用 `while (*p)` 判断表尾，0 即终止符。I2C 报文至少含寄存器地址 1 字节，长度不可能为 0，所以该编码是安全的——这是一种用「不可能的值」做哨兵的常见固件手法（u1-l4 讲过的 shell 命令表 NULL 哨兵同款思路）。

**练习 3**：`i2cSendByte` 里没有任何超时保护，裸机阶段这样写安全吗？
答案：在这个受控场景（单主、仅一个从机、上电即用）基本安全；若总线被拉死会卡在 `while` 死循环。作为对比，运行期 [si5351.c:L12](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L12) 用的是带 1000ms 超时的 `i2cMasterTransmitTimeout`。

### 4.5 从 tune 命令到本振频率：4 倍频与 mode_freq_offset

#### 4.5.1 概念说明

shell 的 `tune 7100000` 到 SI5351 寄存器之间隔着两层「频率翻译」，都发生在 [main.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L196-L201)：

1. **减去 `mode_freq_offset`**：AM 与 CW 模式下本振并不落在信号频率上，而是低 10kHz（`AM_FREQ_OFFSET`，[nanosdr.h:L126](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L126)），让载波落在 10kHz 中频；DSP 侧再用 `mode_freqoffset_phasestep = PHASESTEP(mode_freq_offset)`（[main.c:L188-L189](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L188-L189)）把它搬回零中频（详见 u3-l3 的 `am_demod`，那里用 `nco1_phase -= mode_freqoffset_phasestep` 做数字混频）。LSB/USB/FM 则偏移为 0，本振直接对准信号。
2. **乘以 4**：写给 SI5351 的是中心频率的 4 倍。这与 u1-l1 框图中「四倍频正交本振」的硬件方案对应：外部检波电路利用 4 倍频产生多路相位（具体电路仓库未含，**待确认**）。

#### 4.5.2 核心流程

```
shell: tune 7100000            (假设当前 LSB)
  └─ set_tune(7100000)
       ├─ center_frequency = 7100000 - mode_freq_offset(=0) = 7100000
       └─ si5351_set_frequency(7100000 * 4 = 28400000)
            └─ band 0, rdiv=÷1
                 └─ fixedpll: div=29, num=21, denom=71 → P1=3237, P2=61, P3=71
                      （832MHz / (29+21/71) = 28.4MHz 精确成立）
```

换模式时 `set_modulation` 从 `mod_table` 取出该模式的 `freq_offset` 与解调函数、采样率一并切换——所以「本振偏移多少」是随调制模式联动的。

#### 4.5.3 源码精读

- [main.c:L165-L177](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L165-L177) — `mod_table`：每行是「解调函数、频率偏移、采样率、名字」。可见只有 `cw` 和 `am` 用 `AM_FREQ_OFFSET`（10kHz），`lsb/usb/fm/fms` 均为 0。
- [main.c:L179-L194](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L179-L194) — `set_modulation`：切换采样率与 `signal_process` 函数指针（u2-l3 的主题），同时更新 `mode_freq_offset` 并预计算 NCO 相位步长。注意改模式**不会**自动重调本振——偏移变了但 SI5351 还是旧频率，要等下一次 `set_tune`/选信道才对齐。
- [main.c:L196-L201](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L196-L201) — `set_tune` 全文仅两行：减偏移、乘 4、下发。`center_frequency` 存为全局变量供 DSP 与显示使用。
- [main.c:L72-L81](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L72-L81) — `cmd_freq`：**绕过** `set_tune` 直接调 `si5351_set_frequency(freq)`，即 `freq` 命令设置的是 SI5351 引脚上的原始输出频率（不乘 4、不减偏移），是调试本振的直通道。
- [main.c:L83-L96](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L83-L96) — `cmd_tune`：走 `set_tune` 并同步 `uistat.freq`、切 UI 到 FREQ 档、刷屏——用户语义的「接收频率」命令。

#### 4.5.4 代码实践

1. **实践目标**：验证 `freq` 与 `tune` 两个命令的语义差（4 倍关系）。
2. **操作步骤**：有硬件时，先 `mode lsb`，再 `freq 28400000`，听 7.1MHz 附近信号；换 `tune 7100000`，比较两者是否指向同一接收频率。无硬件时读 [main.c:L72-L96](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L72-L96) 两个函数，写出两条命令各自到达 `si5351_set_frequency` 的实参表达式。
3. **需要观察的现象**：`freq 28400000` 与 `tune 7100000`（LSB）应等效；`tune` 还会更新 `uistat.freq` 与屏幕，`freq` 不会。
4. **预期结果**：两命令听感一致（**待本地验证**）；代码层面可确认实参分别为 `freq` 原值与 `(hz - mode_freq_offset) × 4`。

#### 4.5.5 小练习与答案

**练习 1**：`tune 567000`（AM 模式，广播中波）时写给 SI5351 的频率是多少？
答案：`(567000 - 10000) × 4 = 2228000` Hz。进入 `si5351_set_frequency` 后因 ≤4MHz 会再乘 R=8 做固定 PLL 分数分频，约分后 num=378、denom=557（可用第 5 节脚本复算）。

**练习 2**：为什么 SSB 的 `freq_offset` 是 0，而 AM/CW 是 10kHz？
答案：SSB 信号没有离散载波，Weaver 解调直接在零中频附近处理边带（u3-l2）；AM 的包络检波和 CW 的音调都希望载波/拍频落在可听范围内且避开直流，于是本振偏 10kHz、DSP 再搬回（u3-l3），这样载波能量不与 1/f 噪声和直流偏置重叠。

**练习 3**：`set_modulation` 改了 `mode_freq_offset` 却没有重调本振，这算 bug 吗？
答案：算「暂态不一致」而非致命 bug：从 LSB(偏移0) 切到 AM(偏移10kHz) 后，本振仍指旧中心频率，收到的载波会偏离 10kHz IF，直到下一次 tune/选信道。读者可自行评估是否值得在 `set_modulation` 末尾追加一次 `set_tune(uistat.freq)`（这正是 u5-l4 二次开发的练手点）。

## 5. 综合实践：把 fixedpll 算法搬到 PC 上

**任务**：写一个 PC 端小程序（下面以 Python 为例，语言不限），完整复现 `si5351_set_frequency_fixedpll` 的整数算法与 `si5351_setupMultisynth` 的 P1/P2/P3 编码，输入目标频率打印 `div/num/denom` 与 `P1/P2/P3`，并与固件在 7.1MHz（LSB）时的真实调用参数逐项对照。

**步骤**：

1. 回顾两段源码：[si5351.c:L194-L210](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L194-L210)（fixedpll 算法）与 [si5351.c:L144-L154](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L144-L154)（分数模式编码）。
2. 运行下面的脚本（**示例代码**，非项目源码）：

```python
# si5351_calc.py —— 复现 CentSDR fixedpll 频率合成算法（示例代码）
from math import gcd

XTALFREQ = 26_000_000
PLL_N = 32
PLLFREQ = XTALFREQ * PLL_N          # 832 MHz, 对应 si5351.c L190-L192

def fixedpll(pllfreq, freq):        # 对应 si5351_set_frequency_fixedpll
    div = pllfreq // freq
    num = pllfreq - freq * div
    denom = freq
    k = gcd(num, denom)
    num //= k; denom //= k
    while denom >= (1 << 20):
        num >>= 1; denom >>= 1
    return div, num, denom

def ms_regs(div, num, denom):       # 对应 si5351_setupMultisynth 分数分支
    P1 = 128 * div + (128 * num) // denom - 512
    P2 = 128 * num - denom * ((128 * num) // denom)
    P3 = denom
    return P1, P2, P3

if __name__ == "__main__":
    freq = 28_400_000               # = set_tune(7100000, LSB): (7100000-0)*4
    div, num, denom = fixedpll(PLLFREQ, freq)
    P1, P2, P3 = ms_regs(div, num, denom)
    print(f"div={div} num={num} denom={denom}")
    print(f"P1={P1}(0x{P1:04X}) P2={P2} P3={P3}")
    print(f"回算: {PLLFREQ/(div+num/denom):.1f} Hz (期望 {freq})")
```

3. 与固件真实参数对照：LSB 模式 `tune 7100000` → `set_tune` 算出 28,400,000Hz → band 0、R=÷1 → fixedpll。

**预期输出与对照表**（这就是「与固件实际调用参数对照验证」的答案）：

| 量 | 脚本结果 | 固件路径来源 |
| --- | --- | --- |
| div | 29 | `832000000 / 28400000 = 29`（[si5351.c:L198](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L198)） |
| num / denom | 21 / 71 | `gcd(8400000, 28400000)=400000` 约分（[L199-L204](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L199-L204)） |
| P1 / P2 / P3 | 3237(0x0CA5) / 61 / 71 | 分数模式公式（[L151-L153](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L151-L153)） |
| MS1 写入字节 | `[50, 0x00, 0x47, 0x00, 0x0C, 0xA5, 0x00, 0x00, 0x3D]` | 9 字节打包（[L157-L167](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L157-L167)），0x47=71、0x0C/0xA5=3237、0x3D=61 |
| CLK1 控制字 | 0x0F | 8MA(3) \| MULTISYNTH_N(0x0C)，PLL_A 不加 bit5、分数模式不加 bit6（[L170-L175](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L170-L175)） |
| 回算频率 | 832000000 × 71 / 2080 = 28,400,000 Hz 精确 | 外部 ÷4 后即 7.1MHz 正交本振 |

4. 再用 `freq = 2_228_000`（对应默认信道 567kHz AM：`(567000-10000)*4`）跑一遍，验证程序自动处理 rdiv 预乘前的原始值——注意脚本只复现 fixedpll 本体，`freq ≤ 4MHz` 时固件会先乘 8 再进 fixedpll（[si5351.c:L260-L264](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.c#L260-L264)），请手工先乘再输入，预期 `div=46, num=378, denom=557`。
5. 若有硬件：连接 USB，用 `python/centsdr.py -s` 或 shell 直接 `tune 7100000`，观察屏幕频率与收听情况，确认 7.1MHz 附近确实有业余 SSB 信号（**待本地验证**）。

**观察要点**：`回算` 一行应打印精确的 28400000.0（无舍入），体会 4.3 节所说「有理数合成理论上零误差」；把输入改成质数频率（如 7100003Hz），观察 denom 变大但仍在 20 位以内。

## 6. 本讲小结

- SI5351 是三级流水线：26MHz 晶振 → PLL A/B（600~900MHz 小数倍频）→ Multisynth 0~2（8~1800 小数分频）→ R 分频 → CLKx；每组分频参数占 8 个寄存器，一次 9 字节 I2C 批量写完成配置。
- 分频比 \(a+b/c\) 通过 \(P_1 = 128a + \lfloor 128b/c \rfloor - 512\)、\(P_2 = 128b \bmod c\)、\(P_3 = c\) 编码进硬件；整数模式退化为 \(P_1=128a-512, P_2=0, P_3=1\)。
- `fixedpll`（PLL 恒 832MHz=26MHz×32 整数、MS 分数分频）覆盖 ≤100MHz，改频率不动 PLL、无需重锁，CentSDR 全部预置信道都走这条路；`fixeddiv`（MS 整数 ÷6/÷4、PLL 浮动）覆盖 100~225MHz，频段边界 100/150MHz 由「PLL 必须落在 600~900MHz」决定。
- 低频输出靠 R 分频：先把目标乘 8/64 抬高 MS 工作频率，再由片内 R 分频除回，保证 MS 分频比在 8~1800 舒适区。
- 首次初始化在内核启动前由 `__early_init()` 用 si5351_low.c 的裸机轮询 I2C 完成：PLL A=832MHz、CLK2=8MHz 默认输出；I2C1 时钟源选 HSI 以免受主时钟切换影响。
- `set_tune()` 做两层翻译：减 `mode_freq_offset`（AM/CW 为 10kHz，SSB/FM 为 0）得到中心频率，再乘 4 写给 SI5351，配合外部四倍频正交检波；`freq` 命令则是绕过翻译的本振直调后门。

## 7. 下一步学习建议

本振荡出了稳定的本振，下一讲 **u2-l2（TLV320AIC3204 音频编解码器）** 讲正交检波之后的另一端：I/Q 基带如何被声卡芯片放大、采样，其中同样会出现「哨兵结尾的 I2C 寄存器配置表」（与本讲 4.4 节呼应）。如果你想先看本振频率最终如何被使用，可以跳读 **u3-l1（NCO 与定点基础）** 中 `PHASESTEP` 宏与 `mode_freqoffset_phasestep` 的用法；对「为什么 R 分频能改善低频输出」感兴趣的读者，可顺手翻阅 SI5351 datasheet 中关于 Multisynth 分辨率与小数杂散的章节（外部资料，仓库不含）。
