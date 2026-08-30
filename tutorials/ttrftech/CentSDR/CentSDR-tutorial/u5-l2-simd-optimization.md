# 榨干 Cortex-M4：SIMD 指令与定点优化技巧

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐条说出 `__SMLAD` / `__SMLADX` / `__SMLSDX` / `__SMUAD` / `__SMUSDX` / `__PKHBT` / `__PKHTB` / `__SSAT` / `__QADD16` / `__QSUB16` 这些内建函数（intrinsic）各自完成什么数据搬运与运算。
2. 手工展开一段使用这些内建函数的真实固件代码（复数混频、FIR、鉴频），写出它等价的普通 C 表达式。
3. 解释 SIMD 版 `atan_2iq` 相对浮点 `atan2f` 的精度损失来自哪三层近似。
4. 掌握「乘加累加 → 缩位 → `__SSAT` 饱和」这条定点防溢出惯用链，并理解饱和必须放在截断/强转**之前**。
5. 在 PC 上用等价 C 宏替换全部 SIMD 内建函数，跑通提取出来的混频代码，并通过指令条数统计估算 Cortex-M4 上的理论加速比。

## 2. 前置知识

### 2.1 SIMD 与 Cortex-M4 的 DSP 扩展

SIMD（Single Instruction, Multiple Data，单指令多数据）指一条指令同时处理多份数据。Cortex-M4 的「DSP 扩展」是一组为 16 位定点信号处理定制的指令：它把一个 32 位寄存器看成**两个独立的 16 位半字**（halfword），一条指令即可完成两次 16 位乘法再加一次累加，并且只花 1 个时钟周期。

本讲全程使用如下记号：

- `x.L`（Low）：`x` 的低 16 位 `x[15:0]`，按有符号 int16 解释；
- `x.H`（High）：`x` 的高 16 位 `x[31:16]`，按有符号 int16 解释。

CentSDR 的音频流恰好是连续 `int16` 交织 IQ 对（u2-l3 讲过 I2S 每 5ms 送来 480 帧交织样本），小端机上一个 32 位字读出来就是打包好的 `(I, Q)` 对——SIMD 指令天生为这种「打包字」而生。

### 2.2 什么是 intrinsic（内建函数）

`__SMLAD(a, b, c)` 这种双下划线写法不是函数调用，而是编译器提供的**指令别名**：GCC 会把它原样翻译成一条 `smlad` 机器指令，零调用开销。用 C 的语法精确控制生成哪条汇编，这就是 intrinsic 的意义。

一个重要事实：**这套别名的标准定义并不在本仓库里**。`git ls-files` 显示仓库只跟踪 [CMSIS/DSP_Lib](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/CMSIS/DSP_Lib/Source/FilteringFunctions/arm_biquad_cascade_df1_q15.c) 下的 5 个算法 `.c` 文件（引入方式见 [Makefile:111-117](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L111-L117)），而 `__SMLAD` 等宏定义在 CMSIS 核心头文件（`core_cm4.h` / `cmsis_gcc.h`，随 ChibiOS 子模块分发，未纳入本仓库跟踪；当前环境子模块未检出，具体文件名待确认）。它们通常长这样（示意，非本仓库代码）：

```c
/* 示意代码：CMSIS 核心头文件中 intrinsic 的典型形态 */
__attribute__((always_inline)) __STATIC_INLINE int32_t
__SMLAD(uint32_t op1, uint32_t op2, int32_t op3)
{
  int32_t result;
  __ASM volatile ("smlad %0, %1, %2, %3" : "=r" (result) : "r" (op1), "r" (op2), "r" (op3));
  return(result);
}
```

CentSDR 甚至自己「补写」了几个 CMSIS 没提供的变体——见 4.1.3 的 `__SMULBB` 一族。这说明这套「双下划线指令名」只是薄封装，只要懂指令语义，人人可写。

### 2.3 为什么要省周期

DSP 回调运行在 I2S 中断上下文里，48kHz 采样时每 5ms 必须处理完 240 帧，192kHz 时窗口缩到 1.25ms（u2-l3）。复数混频这种最内层循环每秒要执行约 `2 × 48000` 次乘加对，是全固件执行次数最多的代码。在这里每省 3 条指令，就相当于每秒省近 30 万个周期——这就是本讲全部技巧的动机。

### 2.4 q15 定点回顾

u3-l1 已建立：q15 用 int16 表示 \([-1, 1)\)，两个 q15 相乘得 q30，须 `>>15` 重新归一化为 q15；求模、求和会越出 16 位范围，必须钳位（饱和）防止回绕。本讲反复用到这两条结论。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [dsp.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c) | 全部解调算法。SIMD 使用最密集的文件：NCO 三角函数表与查表内插（`cos_sin`）、Weaver/AM 的复数混频、`atan_2iq` 快速反正切、`fm_adj_filter` 三抽头 FIR、`stereo_matrix` 饱和加减、`_VSQRTF` 内联汇编 |
| [display.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c) | 频谱/波形绘制。作者在此自定义了 `__SMULBB/__SMULTT/__SMULTB/__SMULBT` 四条 16×16 乘法 intrinsic，并在 `window_*_15to31` 家族中用它们完成「加窗 + q15→q31 升位 + 交织重排」 |
| [nanosdr.h](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h) | `FS`、`PHASESTEP` 宏与音频缓冲声明，是理解混频参数的钥匙 |
| [CMSIS/DSP_Lib/Source/FilteringFunctions/arm_biquad_cascade_df1_q15.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/CMSIS/DSP_Lib/Source/FilteringFunctions/arm_biquad_cascade_df1_q15.c) | CMSIS-DSP 的 biquad 实现，内部使用与本讲完全相同的 intrinsic，用来对照「库内部的手法」 |

## 4. 核心概念与源码讲解

### 4.1 SIMD 内建函数速查：一表看懂数据通路

#### 4.1.1 概念说明

Cortex-M4 的 dual-16 位指令族可以按「配对方式」分三类记忆：

1. **双乘加**（`SMLAD` 族，D=Dual）：把两个操作数各自拆成两个半字，按某种配对做两次 16 位乘法再累加。`X` 后缀表示先把**第二操作数**的两个半字交换再配对。`S`（Subtract）表示两组乘积相减而不是相加。
2. **半字打包/拆取**（`PKHBT`/`PKHTB`）：从两个 32 位字里各取一半拼成新字，是「免移位组装打包字」的搬运工。
3. **饱和运算**（`SSAT`/`QADD16`/`QSUB16`）：定点世界的「安全阀」，见 4.4。

#### 4.1.2 核心流程

下表是本讲涉及的全部内建函数等价展开式（\(a\)、\(b\)、\(c\) 为操作数，`L`/`H` 意为低/高半字按 int16 解释）：

| 内建函数 | 等价展开 | 一句话用途 |
| --- | --- | --- |
| `__SMUAD(a,b)` | \(a.L{\times}b.L + a.H{\times}b.H\) | 直配点积 |
| `__SMUADX(a,b)` | \(a.L{\times}b.H + a.H{\times}b.L\) | 交换配点积 |
| `__SMUSDX(a,b)` | \(a.L{\times}b.H - a.H{\times}b.L\) | 交换配叉积（差） |
| `__SMLAD(a,b,c)` | \(c + a.L{\times}b.L + a.H{\times}b.H\) | 直配乘累加 |
| `__SMLADX(a,b,c)` | \(c + a.L{\times}b.H + a.H{\times}b.L\) | 交换配乘累加 |
| `__SMLSDX(a,b,c)` | \(c + a.L{\times}b.H - a.H{\times}b.L\) | 交换配乘减累加 |
| `__PKHBT(a,b,s)` | \((a \& \mathrm{0xFFFF}) \| ((b{\ll}s) \& \mathrm{0xFFFF0000})\) | 低半字取 a，高半字取 b 左移 |
| `__PKHTB(a,b,s)` | \((a \& \mathrm{0xFFFF}) \| (((\mathrm{int32})b){\gg}s \& \mathrm{0xFFFF0000})\) | 低半字取 a，高半字取 b 算术右移 |
| `__SSAT(x,n)` | 把 \(x\) 带符号饱和到 \(n\) 位 | 防溢出回绕 |
| `__QADD16(a,b)` | 两个半字各自饱和相加 | 双路饱和加 |
| `__QSUB16(a,b)` | 两个半字各自饱和相减 | 双路饱和减 |
| `__SMULBB(a,b)` | \(a.L{\times}b.L\) | 单个 16×16 乘 |
| `__SMULTB(a,b)` | \(a.H{\times}b.L\) | 命名中 B=低半字、T=高半字，首字母属第一操作数 |
| `__SMULBT(a,b)` | \(a.L{\times}b.H\) |  |
| `__SMULTT(a,b)` | \(a.H{\times}b.H\) |  |

另有 `__SIMD32(ptr)`：把 `int16_t*` 转成 `int32_t*`，一次读取一对样本，是「以 32 位视角看 16 位流」的类型戏法。

> 读表提示：这些展开式不是凭空给出的，下文每一条都会用 dsp.c / display.c 的真实代码反推验证。

#### 4.1.3 源码精读

CMSIS 没有提供「只做一次 16×16 乘法」的 `SMULB*` 变体，作者在 display.c 里自己补了四条，风格与官方完全一致——每条宏对应一条汇编指令：

[display.c:608-631](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L608-L631) —— 自定义 `__SMULBB/__SMULTT/__SMULTB/__SMULBT` 四条 intrinsic，每条都是一行内联汇编：

```c
__attribute__( ( always_inline ) ) __STATIC_INLINE uint32_t __SMULBB(uint32_t op1, uint32_t op2)
{
  uint32_t result;
  __ASM volatile ("smulbb %0, %1, %2" : "=r" (result) : "r" (op1), "r" (op2) );
  return(result);
}
```

[display.c:678-696](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L678-L696) —— `window_complex_interleaved_15to31` 用这四条宏完成「交织 IQ + 窗系数 → 平面 q31」的重排，命名与数据通路严格对应：

```c
uint32_t w = *__SIMD32(wf)++;        // 窗系数对 (w_n, w_{n+1})
uint32_t i1q1 = *__SIMD32(src)++;    // 交织对 (I1, Q1)
uint32_t i2q2 = *__SIMD32(src)++;    // 交织对 (I2, Q2)
*dest++ = __SMULBB(i1q1, w) << mag_shift;   // I1·w_n   → q31 实部
*dest++ = __SMULTB(i1q1, w) << mag_shift;   // Q1·w_n   → q31 虚部
*dest++ = __SMULBT(i2q2, w) << mag_shift;   // I2·w_{n+1}
*dest++ = __SMULTT(i2q2, w) << mag_shift;   // Q2·w_{n+1}
```

验证命名法：`__SMULTB(i1q1, w)` 的 `T` 指第一操作数 `i1q1` 的高半字 `Q1`，`B` 指第二操作数 `w` 的低半字 `w_n`，乘积正是 `Q1·w_n`。四条宏恰好覆盖两路样本 × I/Q 两个分量的全部组合，一次循环处理两个复数样本，输出顺序 `(re, im, re, im)` 正是 CFFT 需要的交织 q31 格式（u4-l1 讲过后续管线）。

#### 4.1.4 代码实践

1. **实践目标**：用普通 C 验证 `__PKHBT`/`__PKHTB` 的半字来源，建立对 4.1.2 表格的信任。
2. **操作步骤**：在 PC 上新建 `pack_test.c`，写入下面的示例代码并编译运行（`gcc -O2 pack_test.c -o pack_test && ./pack_test`）：

```c
/* 示例代码：验证半字打包方向 */
#include <stdio.h>
#include <stdint.h>

#define PKHBT(a, b, s) ((uint32_t)(((a) & 0xffff) | (((b) << (s)) & 0xffff0000u)))
#define PKHTB(a, b, s) ((uint32_t)(((a) & 0xffff) | ((((int32_t)(b)) >> (s)) & 0xffff0000u)))

int main(void)
{
    uint32_t x = 0x12345678, y = 0x89abcdef;
    printf("PKHBT(x,y,16) = %08x\n", PKHBT(x, y, 16));
    printf("PKHTB(x,y,16) = %08x\n", PKHTB(x, y, 16));
    return 0;
}
```

3. **需要观察的现象**：两个输出的高 16 位各来自哪个参数？低 16 位呢？
4. **预期结果**：`PKHBT` 的低半字来自第一个参数 `x` 的低半字 `0x78`，高半字来自 `y` 左移 16 位后的 `0x89ab`；`PKHTB` 的低半字同样来自 `x`，高半字来自 `y` 算术右移 16 位（符号扩展）。把两个结果与 4.1.2 的展开式逐位对照（精确十六进制值待本地验证——这正是本实践要你亲手确认的东西）。

#### 4.1.5 小练习与答案

**练习 1**：`__SMLAD(a, b, 0)` 与 `__SMLADX(a, b, 0)` 什么时候相等？
**答案**：当 `b.L == b.H`（第二操作数两个半字相同）或 `a.L == a.H` 时，直配与交换配的结果相同。本质是两种配对方式在操作数自身对称时退化。

**练习 2**：为什么 `__SMULBB` 一族返回 `uint32_t` 而乘积可能为负？
**答案**：intrinsic 只是汇编指令的别名，寄存器没有符号属性；乘积的 32 位结果按位原样返回，符号解释交给使用处的 C 表达式（赋给 `int32_t` 即得有符号值）。这也是表中很多「无符号」类型承载有符号半字的原因。

**练习 3**：`__SIMD32(src)++` 每次前进多少字节？
**答案**：4 字节。`__SIMD32` 把 `int16_t*` 转为 `int32_t*`，`++` 按 `int32` 步进，所以一次取走两个 `int16` 样本。

### 4.2 打包字与复数混频：两条指令完成一次复数乘

#### 4.2.1 概念说明

Weaver SSB/CW（u3-l2）和 AM（u3-l3）解调的第一步都是「NCO 复数混频」：把输入复样本 \(I + jQ\) 乘以本地振荡 \(e^{j\varphi}\)。复数乘按定义展开：

\[
(I + jQ)(\cos\varphi + j\sin\varphi) = \underbrace{(I\cos\varphi - Q\sin\varphi)}_{\text{实部}} + j\underbrace{(I\sin\varphi + Q\cos\varphi)}_{\text{虚部}}
\]

用普通 C 写需要 4 次 16 位乘、2 次加、若干次半字拆装与 `>>15` 归一化。SIMD 的洞察在于：**把 (I, Q) 和 (cos, sin) 各装进一个 32 位打包字，上面两组乘积正好是两种半字配对**——`__SMLSDX` 给实部、`__SMLAD` 给虚部，两条单周期指令搞定。

#### 4.2.2 核心流程

NCO 每个样本做三件事：

1. 相位累加器 `nco1_phase` 减去步进（负步进 = 负频率旋转，u3-l1/u3-l2 已讲符号如何选边带）；
2. `cos_sin(phase)` 查表内插出打包的 \((\cos\varphi \ll 16) | \sin\varphi\)；
3. 两条乘加指令 + `>>15` 归一化，得到新的 I、Q 写入平面缓冲。

#### 4.2.3 源码精读

[dsp.c:267-281](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L267-L281) —— `cos_sin()` 返回打包正交载波：

```c
static uint32_t
cos_sin(uint16_t phase)
{
    uint16_t mod = phase & 0xff;
    uint32_t r = __PKHBT(0x0100, mod, 16);      // r = (mod<<16)|0x0100
    uint16_t si = phase / 256;
    uint16_t ci = (si + 64) & 0xff;              // cos 索引 = sin 索引 + 90°
    uint32_t cd = *(uint32_t *)&cos_sin_table[ci];
    uint32_t sd = *(uint32_t *)&cos_sin_table[si];
    int32_t c = __SMUAD(r, cd);                  // = 256*表值 + mod*差分
    int32_t s = __SMUAD(r, sd);
    c /= 256;
    s /= 256;
    return __PKHBT(s, c, 16);                    // (cos<<16)|sin
}
```

先看数据布局：[dsp.c:8-265](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L8-L265) 的表项是 `{ int16 主值; int16 前向差分 }`，小端读成 32 位字后 `.L` = 主值 \(a\)、`.H` = 差分 \(d\)（可验证：`a[1]-a[0] = -32757-(-32767) = 10` 恰为第二列首项）。于是：

\[
\texttt{\_\_SMUAD}(r, cd) = r.L \times cd.L + r.H \times cd.H = 256\,a + \mathrm{mod}\cdot d
\]

除以 256 后得 \(a + \frac{\mathrm{mod}}{256}d\)——正是「表值 + 相位小数部分 × 斜率」的线性内插。**一条 `__SMUAD` 同时完成了取整配对乘和内插斜率乘**，这就是 u3-l1 所说「值＋差分斜率双列表」的指令级真相。返回值 `__PKHBT(s, c, 16)` 打包成 \((\cos\ll16) | \sin\)。

[dsp.c:358-364](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L358-L364) —— `demod_weaver` 的混频核心（AM 用的是同样的两条指令，见 [dsp.c:432-438](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L432-L438)）：

```c
int32_t *s = __SIMD32(src);                  // dsp.c:351，32 位视角读交织 IQ
for (i = 0; i < len/2; i++) {
    uint32_t cossin = cos_sin(nco1_phase);
    nco1_phase -= dc->phasestep1;
    uint32_t iq = *s++;                      // iq = (Q<<16)|I
    *bufi++ = __SMLSDX(iq, cossin, 0) >> 15; // I·cos − Q·sin  → 实部
    *bufq++ = __SMLAD(iq, cossin, 0) >> 15;  // I·sin + Q·cos  → 虚部
}
```

按 4.1.2 表格展开（`iq.L = I`、`iq.H = Q`；`cossin.L = sin`、`cossin.H = cos`）：

\[
\texttt{\_\_SMLAD}(iq, cossin, 0) = I\sin\varphi + Q\cos\varphi,\qquad
\texttt{\_\_SMLSDX}(iq, cossin, 0) = I\cos\varphi - Q\sin\varphi
\]

与 4.2.1 的复数乘展开逐项对照：**两条指令不多不少正好覆盖全部四个 16 位乘积**，各自输出复数乘的一路，`>>15` 把 q30 乘积归一化回 q15。对照手写 C 版本（拆 4 次半字 + 4 次乘 + 2 次加 + 移位，约 10 条指令），这里只要 2 条乘加 + 1 条移位。

[dsp.c:376-382](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L376-L382) —— Weaver 第三级「反向搬移」中 `__PKHBT` 反向组装输出：把平面 I/Q 重新拼回交织字，且用 `__PKHBT(r, r, 16)` 把同值复制到两个半字（双声道单音输出）：

```c
uint32_t iq = __PKHBT(*bufi++, *bufq++, 16);   // (Q<<16)|I：平面→交织
uint32_t r = __SMLAD(iq, cossin, 0) >> 15;
*d++ = __PKHBT(r, r, 16);                      // 低高半字同值 → 立体声双声道
```

AM 解调输出处 [dsp.c:462](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L462) 的 `__PKHBT(z, z, 16)` 与 `fm_demod` 的 [dsp.c:582](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L582) 是同一招。

#### 4.2.4 代码实践

1. **实践目标**：用具体数字验证 4.2.1 的等式，确认两条指令确实精确分解了复数乘。
2. **操作步骤**：在 PC 上写 20 行程序（示例代码）：取 \(\varphi = 30°\)（\(\cos\varphi \approx 0.866\)、\(\sin\varphi = 0.5\)，量化为 q15 后约 `28378` 与 `16384`），输入 \(I = 0.7\)（`22938`）、\(Q = 0.1\)（`3277`）。用 4.1.2 的宏展开式计算 `__SMLAD` / `__SMLSDX` 的 `>>15` 结果，再用 `double` 直接算 \((0.7 + j0.1) \times e^{j30°}\) 的实部虚部。
3. **需要观察的现象**：两组实部、虚部数值对比表。
4. **预期结果**：定点结果与浮点结果误差在几个 LSB 内（来源：q15 量化 + `>>15` 截断）。若实部虚部对调或差个负号，说明你把半字配对方向弄反了——回到 4.1.2 表格核对。具体误差值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `cossin` 打包成 \((\cos\ll16) | \sin\) 而不是 \((\sin\ll16) | \cos\)？
**答案**：这是作者的约定，配对由 `__SMLAD`/`__SMLSDX` 的固定配对方式迁就：`cossin.L = sin` 与 `iq.L = I` 直配出现在虚部 \(I\sin\)，`cossin.H = cos` 参与交换配。若交换 sin/cos 位置，两条指令的输出角色也要跟着对调。关键是**约定一旦固定，全链路一致即可**。

**练习 2**：`nco1_phase -= dc->phasestep1` 中 `phasestep1` 来自 [nanosdr.h:125-128](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L125-L128) 的 `PHASESTEP(freq) = 65536L*freq/FS`。SSB 时 `freq = SSB_FREQ_OFFSET = 1300`，步进是多少？
**答案**：\(65536 \times 1300 / 48000 = 1774.4\)，整数化为 `1774`。LSB 与 USB 配置只差正负号（[dsp.c:339-344](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L339-L344)），符号翻转旋转方向即翻转边带。

**练习 3**：如果把 4.2.3 混频循环里的 `__SMLAD` 与 `__SMLSDX` 互换位置，程序还能工作吗？
**答案**：输出会变成 \(I\sin - Q\cos\)（取负的虚部）进 I 通道、\(I\cos + Q\sin\)（实部）进 Q 通道——相当于复数乘结果整体旋转了 90° 相位。信号能量还在，但边带选择与后续滤波的对齐被破坏，SSB 会在「错误的那一边」。指令配对与信号定义必须严格对应。

### 4.3 查表内插与 FIR：乘加对的点积本质

#### 4.3.1 概念说明

`__SMUAD`/`__SMLAD`/`__SMLADX` 的本质都是「两个半字对的点积」。除了 4.2 的三角函数内插，这个模式还统治着一切短 FIR（有限冲激响应）滤波器：FIR 就是系数向量与滑动窗口样本向量的点积。当抽头数少（2~4 个）时，把相邻样本和系数各打包成一个 32 位字，几条乘加指令就是整个滤波器，连 CMSIS 的通用函数都不必请出来。

#### 4.3.2 核心流程

以 `fm_adj_filter` 的 I 路为例（3 抽头对称 FIR）：

1. 维护两个历史打包字 `x1`、`x2`（上一帧、上上帧的交织 IQ）；
2. 用 `__PKHBT` 把 `x2` 的低半字与 `x1` 的低半字拼成 `(I_{n-1}<<16)|I_{n-2}`，用 `__PKHBT(0, x0, 16)` 拼出 `(I_n<<16)|0`；
3. `__SMLAD` + `__SMLADX` 两对乘加覆盖三个抽头；
4. 缩位、饱和、写回（4.4 详述）。

#### 4.3.3 源码精读

[dsp.c:751-789](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L751-L789) —— `fm_adj_filter`，FM 立体声链路的 3 抽头频响校正 FIR（u3-l5 从功能角度讲过它）：

```c
uint32_t k12 = 0x5ae1eccd;                       // 打包的 q15 系数对
for (i = 0; i < len; i += 2) {
    int32_t x0 = *s;                             // 当前交织字 (I_n, Q_n)
    uint32_t i12 = __PKHBT(x2, x1, 16);          // .L=I_{n-2}, .H=I_{n-1}
    uint32_t i0_ = __PKHBT(zero, x0, 16);        // .L=0,     .H=I_n
    acc_i = __SMLAD(k12, i12, acc_i);            // kL·I_{n-2} + kH·I_{n-1}
    acc_i = __SMLADX(k12, i0_, acc_i);           // += kL·I_n + kH·0
    ...
}
```

先拆开系数字：`k12.H = 0x5ae1 = 23265`、`k12.L = 0xeccd = -4915`（按 int16 解释）。换成十进制系数：\(k_H = 23265/32768 \approx 0.710\)、\(k_L = -4915/32768 \approx -0.150\)。对照半带滤波器（half-band）的经典参数 \(1/\sqrt{2} \approx 0.707\) 与 \(-(1-1/\sqrt2)/2 \approx -0.146\)——这正是 u3-l5 说的「扳平 19~38kHz 频段」的近半带结构，两个系数**打包在一个立即数里**随取随用。

展开 I 路累加：

\[
\texttt{acc\_i} = k_L\,(I_{n} + I_{n-2}) + k_H\,I_{n-1}
\]

一条 `__PKHBT` 加一对 `__SMLAD`/`__SMLADX` 就是一个完整对称 FIR——没有循环、没有系数数组索引。Q 路在 [dsp.c:772-775](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L772-L775) 用 `__PKHTB` 的算术右移特性做同样骨架，其半字配对细节留作练习 3。

[dsp.c:528-566](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L528-L566) —— `atan_2iq` 开头两条指令是点积/叉积的又一登场（u3-l4 已讲鉴频原理，这里只看数据通路）：

```c
int32_t re = __SMUAD(iq1, iq0);   // 相邻两样本的点积（幅度信息）
int32_t im = __SMUSDX(iq1, iq0);  // 相邻两样本的叉积（相位差信息）
```

两条指令把「前后样本共轭相乘取辐角」所需的实部虚部一次算清，后续 [dsp.c:535-550](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L535-L550) 的象限折叠只在这两个标量上操作。

> 读码注记：按 4.1.2 的展开式，`__SMUSDX(iq1, iq0)` 等于 \(I_1 Q_0 - Q_1 I_0\)，与源码注释写的 \(I_0 Q_1 - I_1 Q_0\) 互为相反数。对 FM 鉴频这只翻转输出音频的整体极性（相当于边带对调），不影响频率信息本身——符号约定以指令语义为准，注释可作参考。

#### 4.3.4 代码实践

1. **实践目标**：亲手拆解一个「藏在立即数里的滤波器」。
2. **操作步骤**：用 Python 或 C 打印 `0x5ae1eccd` 的两个半字（按 int16），算出 \(k_L\)、\(k_H\)；再代入 4.3.3 的展开式，手算 `I_{n-2}=1000, I_{n-1}=2000, I_n=3000` 时的 `acc_i`（`>>14` 之前）。
3. **需要观察的现象**：\(k_L \approx -0.150\)、\(k_H \approx 0.710\) 与半带滤波器理论值的接近程度；手算 `acc_i` 与公式的一致性。
4. **预期结果**：`acc_i = -4915*(1000+3000) + 23265*2000 = -19660000 + 46530000 = 26870000`。注释行 [dsp.c:761](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L761) 还留有另一组系数 `0x51ecea3d`，可一并拆开对比两版设计的差异。

#### 4.3.5 小练习与答案

**练习 1**：`cos_sin` 里为什么用 `c /= 256` 而不是 `c >>= 8`？
**答案**：此处 `c` 为正数，两者等价；`/256` 可读性更强，编译器会优化成移位。若值可能为负，`/256`（向零取整）与 `>>8`（向下取整）在负数时不同，需要留意。

**练习 2**：把 4.3.3 的三抽头 FIR 改成五抽头对称 FIR，最少需要几条乘加指令（系数两两打包）？
**答案**：三对系数打包成 3 个 `k` 字，样本拼 3 个窗口字，用 `__SMLAD`/`__SMLADX` 交替累加约 6 条乘加即可覆盖 5 个抽头（对称性使独立系数只有 3 个）。

**练习 3**：展开 [dsp.c:772-775](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L772-L775) 的 Q 路：写出 `q12 = __PKHTB(x1, x2, 16)` 与 `q0_ = __PKHTB(x0, zero, 16)` 的 `.L/.H` 展开式，再与 I 路的 `i12/i0_` 对比。
**答案**：按宏定义，`q12` 的低半字取自第一个参数 `x1`（即 `I_{n-1}`），高半字来自 `x2` 算术右移 16 位（即 `Q_{n-2}`）；`q0_` 的低半字是 `I_n`、高半字为 0。于是 Q 路累加混入了 `I_{n-1}`、`I_n` 等 I 路半字，与 I 路的纯 I 半字 FIR 并不镜像。这个练习的价值在于体会：宏把数据通路藏起来之后，「看起来对称」的代码实际搬运的数据可能出乎意料——把它提取到 PC 上实测（见综合实践）才是指令级读码的最终裁判。

### 4.4 饱和与钳位：`__SSAT`、`__QADD16` 的惯用法

#### 4.4.1 概念说明

定点运算溢出不是「变大」而是**回绕**：`32767 + 1` 在 int16 里变成 `-32768`，波形瞬间从正峰跳到负峰，扬声器里是一声爆响。所有定点代码必须在「乘加之后、截断之前」插入饱和（saturation）：把超出 \([-2^{15}, 2^{15})\) 的值压回边界。`__SSAT(x, n)` 把任意 int32 饱和到 n 位；`__QADD16/__QSUB16` 则让两个半字**各自**饱和地加减，相当于一次算两路带保护的运算。

#### 4.4.2 核心流程

CentSDR 的标准防溢出链是三步：

```text
乘加累加（32 位 acc） → 缩位（acc >> k，把增益折算掉） → __SSAT(acc, 16)（压回 int16 范围）
```

顺序不可颠倒：先缩位再饱和，饱和才有机会在 16 位边界之前拦住越界值。

#### 4.4.3 源码精读

[dsp.c:776-779](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L776-L779) —— `fm_adj_filter` 输出处的教科书式惯用法：

```c
acc_i = __SSAT(acc_i >>14, 16);
acc_q = __SSAT(acc_q >>14, 16);
*s++ = __PKHBT(acc_i, acc_q, 16);
```

累加器最大约 \(3 \times 32767 \times 23265 \approx 2.3 \times 10^9\)，已贴近 int32 上限；`>>14` 折算回 q15 量程，`__SSAT(...,16)` 保证进入 `__PKHBT` 的正是合法半字。一行之内完成「缩位 + 饱和」。

[dsp.c:565](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L565) —— `atan_2iq` 的返回值同样先缩放（尺度换算成 1024/rad，见 u3-l4）再饱和：

```c
return __SSAT(ang/32, 16);
```

[dsp.c:685-697](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L685-L697) —— `stereo_matrix` 用 `__QADD16/__QSUB16` 一步完成「和差矩阵 + 饱和保护」（[dsp.c:699-711](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L699-L711) 的 `stereo_matrix2` 同款）：

```c
uint32_t x1 = *s1;                    // 单个 int16 样本（符号扩展进 32 位容器）
uint32_t x2 = *s2;
uint32_t l = __QADD16(x1, x2);        // 低半字 = sat16(x1+x2)
uint32_t r = __QSUB16(x1, x2);        // 低半字 = sat16(x1-x2)
*s1++ = l;                            // int16 存储只取低半字，高位弃置
*s2++ = r;
```

玄机在于：单样本放入 32 位容器后只有低半字有效，`__QADD16` 对低半字做饱和加的同时，高半字算出的垃圾（例如 `-1 + -1`）会在 `int16` 存储时被丢弃。**一条指令借来半字饱和器，专门保护低半字**。

反面教材是 [dsp.c:456-461](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L456-L461) AM 包络检波的手写钳位：

```c
z = (int16_t)_VSQRTF((float)(x*x+y*y));   // 先强转 int16！
if (z > 32767) z = 32767;                 // 此时 z 已不可能 > 32767
if (z < -32768) z = -32768;               // 钳位空转
```

u3-l3 已指出：`(int16_t)` 强转发生在钳位之前，越界值早已回绕，两个 `if` 永远不触发。若按本讲惯用法改写成 `z = __SSAT((int32_t)_VSQRTF(...), 16)`，一行即正确。对比之下更能体会「饱和放在截断之前」这条纪律。

#### 4.4.4 代码实践

1. **实践目标**：直观感受回绕与饱和的差异。
2. **操作步骤**：在 PC 上（示例代码）分别实现 `naive(x, y) = (int16_t)(x + y)` 与 `sat(x, y)`（用 4.1.2 的 `__SSAT` 展开式），扫 `x = y` 从 16000 到 33000，打印两版结果。
3. **需要观察的现象**：`naive` 版在 `x+y` 越过 32767 的瞬间从大正数跳到大负数；`sat` 版停在 32767。
4. **预期结果**：回绕点恰在 `x + y = 32768`（因 x=y，即 `x = 16384`）；`sat` 版全程单调不减。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `fm_adj_filter` 是 `>>14` 而混频循环是 `>>15`？
**答案**：`>>15` 对应 q15×q15=q30 的纯归一化；`fm_adj_filter` 的系数模长与半带结构增益合计约需补偿一位，`>>14` 在归一化的同时把量程折算回 q15 满幅附近。缩位数与系数设计必须联调——改系数时这个移位数要一起审。

**练习 2**：`__QADD16` 的高半字「垃圾」为什么可以不管？
**答案**：见 4.4.3——结果经 `int16` 指针存储，只有低半字被写入内存；高半字的饱和运算结果随 32 位临时值一起丢弃。

**练习 3**：把 [dsp.c:460-461](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L460-L461) 的两行 `if` 钳位改正确，最少改几处？
**答案**：一处——把钳位移到强转之前：先 `int32_t z = _VSQRTF(...)`、钳位后再 `*d++ = __PKHBT(z, z, 16)`，或直接 `int32_t z = __SSAT((int32_t)_VSQRTF(...), 16)`。

### 4.5 精度损失与分工：SIMD vs 浮点、手写 vs CMSIS-DSP

#### 4.5.1 概念说明

SIMD 定点快是有代价的：精度。以 `atan_2iq` 对比标准库 `atan2f` 为例，误差来自三层叠加：

1. **输入量化**：I/Q 样本本身是 q15，相对满幅度已有约 \(2^{-15}\)（约 −90 dB）的量化底噪，且 ADC 之前的链路噪声往往更大；
2. **定点除法截断**：[dsp.c:551-561](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L551-L561) 的 `d /= re >> 16` 把分母截到高 16 位、商为整数截断，16.16 格式的比值又只取 8 位做表索引；
3. **查表内插**：[dsp.c:493-523](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L493-L523) 的 `arctantbl` 只有 256 项线性内插，角度分辨率受表距限制。

对 FM 鉴频这种最终产物是「人耳听音频」的应用，三层误差换几十倍速度完全划算；但若做测量仪器，就得换成浮点或 CORDIC 迭代。

#### 4.5.2 核心流程

固件的运算实现分两条路线，判据是「通用性」：

```text
热路径、格式固定（q15 打包流）  →  手写 SIMD 循环（dsp.c / display.c）
通用算法、参数运行时可变        →  调 CMSIS-DSP（biquad 级联、radix-4 CFFT）
```

两条路线的底层是**同一批指令**——CMSIS-DSP 内部就是用同样的 intrinsic 写的。

#### 4.5.3 源码精读

[arm_biquad_cascade_df1_q15.c:133-141](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/CMSIS/DSP_Lib/Source/FilteringFunctions/arm_biquad_cascade_df1_q15.c#L133-L141) —— CMSIS 的 biquad 内核与 `demod_weaver` 手法同源（`__SIMD32` 批量读、`__SMUAD/__SMLALD` 一次处理两个样本）：

```c
in = *__SIMD32(pIn)++;
out = __SMUAD(b0, in);
acc = __SMLALD(b1, state_in, out);
```

[arm_biquad_cascade_df1_q15.c:156](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/CMSIS/DSP_Lib/Source/FilteringFunctions/arm_biquad_cascade_df1_q15.c#L156) 与 [arm_biquad_cascade_df1_q15.c:169-175](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/CMSIS/DSP_Lib/Source/FilteringFunctions/arm_biquad_cascade_df1_q15.c#L169-L175) —— 同样以 `__SSAT` 收尾、用 `__PKHBT` 滚动状态，与本讲 4.3/4.4 的惯用法一字不差。

为什么 Weaver 解调（[dsp.c:368-369](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L368-L369)）宁可调库也不手写 biquad？因为级联 biquad 要处理任意阶数、postShift、64 位累加保护这些通用细节（u3-l2 讲过系数约定），复用库最稳。而 NCO 混频只有两条指令的机会成本，手写反而更短。

浮点侧的一个小样本是 [dsp.c:411-416](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L411-L416) 的 `_VSQRTF`：

```c
__attribute__( ( always_inline ) ) __STATIC_INLINE
float _VSQRTF(float op1) {
  float result;
  __ASM volatile ("vsqrt.f32 %0,%1" : "=w"(result) : "w"(op1) );
  return(result);
}
```

Cortex-M4F 的 FPU 有硬件 `vsqrt.f32` 指令，作者用内联汇编显式发出它，避免走编译器可能选择的软件库路径（u3-l3 从开销角度讲过）。这提示固件里「浮点不缺席，但只出现在标量、非热循环的位置」。

此外 [dsp.c:880-888](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L880-L888) 还留有一段 `#if 0` 注释掉的 `ldr/str` 后索引（post-index）裸汇编搬运循环——同一仓库里能看到从「纯汇编风格」到「intrinsic 风格」的演化痕迹，后者可读性与可移植性都更好。

#### 4.5.4 代码实践

1. **实践目标**：把 `atan_2iq` 的三层误差逐一拆开，找出主导项。
2. **操作步骤**：把 `atan_2iq` 与 `arctantbl` 提取到 PC（用综合实践的 shim 宏），另写参考版 `atan2f`。做三组对照实验：①原样对比最大角度误差（换算成度）；②把 `d /= re >> 16` 改为 `d = (int32_t)(((double)im * 65536.0) / re)`（消除除法截断）再看误差；③再把 256 项表换成 4096 项（可用 `atan(x/256.0)*25736` 现场生成）看误差。
3. **需要观察的现象**：每放宽一层近似，最大误差下降多少。
4. **预期结果**：误差量级在零点几度级别；三项近似对总误差的贡献排序待本地验证——这正是实验要回答的问题（u3-l4 的实践从鉴频输出角度做过整链对比，本实践定位误差的分层归属）。

#### 4.5.5 小练习与答案

**练习 1**：`atan_2iq` 输出尺度为什么是 1024/rad？
**答案**：表以 `Q15_PI_4 = 25736 = (π/4)×32768` 标定（[dsp.c:525](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L525)），即 1 rad 对应 \(25736 \times 4/\pi = 32768\) 个表单位；返回式 `ang/32` 把它压进 int16 的同时保留 10 位分辨率，\(32768/32 = 1024\)，故输出尺度为 1024/rad。

**练习 2**：`_VSQRTF` 用 `volatile` 修饰汇编，意味着什么？
**答案**：禁止编译器对该 asm 块做重排或消除——即使它认为结果未使用。对这种依赖硬件语义的内联汇编，`volatile` 是保底手段。

**练习 3**：既然 CMSIS-DSP 也用 SIMD，为什么不把 NCO 混频也改成调库（如 `arm_cmplx_mult_cmplx_q15`）？
**答案**：通用库要处理任意长度、任意缩放策略，接口开销（参数检查、缩放约定、输出格式）对两指令的热循环不划算；且 NCO 的载波来自自家查表，打包格式私有。热路径手写、通用算法调库，是本讲反复出现的分工判据。

## 5. 综合实践：PC 端 SIMD 模拟器与加速比估算

### 5.1 任务

在 PC 上完成三件事：①用纯 C 宏替换全部 SIMD intrinsic，跑通提取出来的 `cos_sin` + 复数混频循环，验证结果正确；②用 `-O2` 编译 100 万样本混频的基准，对比「宏展开版」与「直连版」的耗时；③统计两版循环体的指令条数，推算 Cortex-M4 上的理论加速比。

### 5.2 第一步：编写 shim 头文件（示例代码）

新建 `simd_shim.h`，内容是 4.1.2 表格的直接翻译（GCC/Clang 可编译）：

```c
/* 示例代码：simd_shim.h —— 在 PC 上模拟 Cortex-M4 DSP intrinsic */
#include <stdint.h>

static inline int16_t lo16(uint32_t x) { return (int16_t)(x & 0xffff); }
static inline int16_t hi16(uint32_t x) { return (int16_t)(x >> 16); }

static inline int32_t ssat(int32_t x, int n) {
    int32_t m = (1 << (n - 1)) - 1, i = -(1 << (n - 1));
    return x > m ? m : (x < i ? i : x);
}
static inline uint32_t sat16u(int32_t x) { return (uint32_t)(ssat(x, 16) & 0xffff); }

#define __SMUAD(a, b)      (hi16(a)*hi16(b) + lo16(a)*lo16(b))
#define __SMUADX(a, b)     (hi16(a)*lo16(b) + lo16(a)*hi16(b))
#define __SMUSDX(a, b)     (lo16(a)*hi16(b) - hi16(a)*lo16(b))
#define __SMLAD(a, b, c)   ((c) + hi16(a)*hi16(b) + lo16(a)*lo16(b))
#define __SMLADX(a, b, c)  ((c) + hi16(a)*lo16(b) + lo16(a)*hi16(b))
#define __SMLSDX(a, b, c)  ((c) + lo16(a)*hi16(b) - hi16(a)*lo16(b))
#define __PKHBT(a, b, s)   ((uint32_t)(((a) & 0xffff) | (((b) << (s)) & 0xffff0000u)))
#define __PKHTB(a, b, s)   ((uint32_t)(((a) & 0xffff) | ((((int32_t)(b)) >> (s)) & 0xffff0000u)))
#define __SSAT(x, n)       ssat((x), (n))
#define __QADD16(a, b)     (sat16u(lo16(a) + lo16(b)) | (sat16u(hi16(a) + hi16(b)) << 16))
#define __QSUB16(a, b)     (sat16u(lo16(a) - lo16(b)) | (sat16u(hi16(a) - hi16(b)) << 16))
#define __SIMD32(p)        ((int32_t *)(void *)(p))
```

### 5.3 第二步：提取固件代码（示例代码）

从 [dsp.c:8-265](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L8-L265) 原样复制 `cos_sin_table`，再抄入 `cos_sin()`（[dsp.c:267-281](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L267-L281)，include 本头文件后无需改动）和混频循环骨架：

```c
/* 示例代码：mix_bench.c 的核心，来自 dsp.c:358-364 的骨架 */
#include "simd_shim.h"

extern const int16_t cos_sin_table[256][2];   /* 从 dsp.c 复制 */

static uint32_t cos_sin(uint16_t phase)       /* dsp.c:267-281 原样 */
{
    uint16_t mod = phase & 0xff;
    uint32_t r = __PKHBT(0x0100, mod, 16);
    uint16_t si = phase / 256;
    uint16_t ci = (si + 64) & 0xff;
    uint32_t cd = *(uint32_t *)&cos_sin_table[ci];
    uint32_t sd = *(uint32_t *)&cos_sin_table[si];
    int32_t c = __SMUAD(r, cd);
    int32_t s = __SMUAD(r, sd);
    c /= 256;
    s /= 256;
    return __PKHBT(s, c, 16);
}

static uint16_t nco_phase = 0;

static void mix(const int16_t *src, int16_t *dsti, int16_t *dstq, int n, int16_t step)
{
    const int32_t *s = __SIMD32(src);
    for (int k = 0; k < n/2; k++) {
        uint32_t cossin = cos_sin(nco_phase);
        nco_phase -= step;
        uint32_t iq = *s++;
        dsti[k] = __SMLSDX(iq, cossin, 0) >> 15;
        dstq[k] = __SMLAD(iq, cossin, 0) >> 15;
    }
}
```

### 5.4 第三步：正确性验证

生成一段频率 \(f = 3000\,\mathrm{Hz}\) 的复指数测试信号（`I[n] = 32767·cos(2πf·n/Fs)`、`Q[n] = 32767·sin(2πf·n/Fs)`，`Fs = 48000`），以 `step = PHASESTEP(3000) = 65536*3000/48000 = 4096` 调用 `mix`（注意固件里是 `nco_phase -= step`，旋转方向随之确定），把输出与 `double` 复数乘对照。

**预期结果**：NCO 频率与信号频率相等且方向一致时，输出应集中在直流附近（近似恒定复数）；每个样本误差在几十个 LSB 内（查表内插 + q15 量化的合计，u3-l1 实测过约 −78 dB 量级）。具体数值待本地验证。

### 5.5 第四步：基准与指令条数

1. 编译 `gcc -O2 mix_bench.c -o mix_bench`，循环调用 `mix` 处理 100 万个打包样本，用 `clock_gettime(CLOCK_MONOTONIC)` 计时，同时累计一个校验和防止编译器删除循环。
2. 制作「直连版」：另写一个 `mix_direct()`，把 `__SMLAD/__SMLSDX` 宏体替换为直接以 32 位运算拼出的等价骨架（仅供计时对照，允许不 bit-exact），两版跑同样负载对比耗时。
3. 统计指令条数：`objdump -d mix_bench` 找到 `mix` 的循环体（向后跳转目标之间的指令序列），数宏展开版每对样本的指令数；SIMD 版则按「`__SMLAD`+`__SMLSDX` = 2 条、`__PKHBT` = 1 条、`>>15` 归一化与装配约 3~4 条」直接计。

### 5.6 需要观察的现象与预期结论

- **PC 耗时**：两版差异可能很小，甚至宏展开版因编译器自动向量化（SSE/AVX 把多个 16 位通道并行）反而更快——这本身就是个教训：**x86 的测量不能外推到 M4**，M4 没有向量寄存器，只有本讲的 dual-16 指令。
- **指令条数**：宏展开版每对样本约 12~18 条（拆半字 ×4、乘 ×4、加 ×2、移位归一化 ×2，外加装载存储与循环控制）；SIMD 版约 6~8 条。
- **理论加速比**：按 Cortex-M4 单发射、乘加指令均 1 周期计，混频核心 \((12{\sim}18)/(6{\sim}8) \approx 2{\sim}3\) 倍；只看乘加对本身（约 10 条 vs 2 条）可达 \(4{\sim}5\) 倍。192kHz 立体声模式下这就是「能实时」与「不能实时」的差距（u5-l1 的负载测量可以佐证）。

以上耗时与条数均为**估算，待本地验证**；请把你数出的指令条数回填到本节，形成你自己的测量记录。

## 6. 本讲小结

- Cortex-M4 DSP 扩展把 32 位寄存器当两个 int16 半字用，`__SMLAD` 族一条指令、一个周期完成「两次 16 位乘 + 累加」；`X` 后缀 = 先交换第二操作数的半字，`S` = 乘积相减。
- `cos_sin` 用「值＋前向差分」双列表，一条 `__SMUAD` 同时算出表值配对乘与内插斜率乘；`demod_weaver` 的复数混频恰好被 `__SMLSDX`（实部）+ `__SMLAD`（虚部）两条指令精确覆盖。
- 打包字是 SIMD 的燃料：`__PKHBT/__PKHTB` 免移位组装/拆取半字，`__SIMD32` 让 16 位流按 32 位读写；`display.c` 的加窗升位与 `fm_adj_filter` 的三抽头 FIR（系数就藏在 `0x5ae1eccd` 一个立即数里，近似半带结构）都是这套搬运术的实例。
- 防溢出惯用链是「乘加累加 → 缩位 → `__SSAT`/`__QADD16` 饱和」，饱和必须放在强转/截断之前——AM 检波里那对空转的 `if` 钳位是反面注脚。
- `atan_2iq` 相对 `atan2f` 的精度损失由三层叠加：q15 输入量化、16.16 定点除法截断、256 项查表线性内插；对人耳应用绰绰有余。
- 分工判据：热路径、格式私有（q15 打包流）就手写 intrinsic；通用算法、参数可变就调 CMSIS-DSP——而库内部用的正是同一批指令（`arm_biquad_cascade_df1_q15.c` 可证）。

## 7. 下一步学习建议

- 下一讲 u5-l3「内存的版图：链接脚本与启动文件」把视角从指令移到内存：这些热代码放在 Flash、RAM 还是 CCM，同样决定实时性。
- 建议回头精读 [display.c:635-722](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L635-L722) 的 `window_*` 家族四姐妹，画出各自的半字搬运图，检验你已能独立读懂任意 intrinsic 代码。
- 进阶方向：用 `arm-none-eabi-objdump -d build/ch.elf` 反汇编 `demod_weaver`，亲眼确认 `smlad`/`smlsdx`/`pkhbt` 真的各只生成一条指令；再对照 ARMv7-M 手册的指令周期表核算本讲的加速比估算。
