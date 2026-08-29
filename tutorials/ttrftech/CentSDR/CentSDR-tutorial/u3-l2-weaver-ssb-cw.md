# 单边带与电报：Weaver 法 SSB/CW 解调

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 Weaver 法 SSB 解调的「三级流水」——NCO 混频 → IIR 低通 → 二次混频取实部——每一步对信号频谱做了什么。
2. 解释 `usb_demod_conf` 与 `lsb_demod_conf` 为什么只差一个符号，就能选择上边带或下边带。
3. 读懂 CMSIS `arm_biquad_cascade_df1_q15` 的系数排列 `{b0, 0, b1, b2, a1, a2}`（a 已取反）、postShift 的作用，以及为什么 notebook 要乘 16384 而不是 32768。
4. 用 `python/SSB-Filter-Design.ipynb` 的流程重新设计滤波器，把浮点 biquad 系数换算成 q15 整数填回 `bq_coeffs`。
5. 说明 CW 模式如何复用同一个 `demod_weaver()`：10kHz 中频 + 150Hz 窄带滤波 + 可调 800Hz 侧音。

## 2. 前置知识

### 2.1 SSB 与 CW：业余电台的两种主力模式

- **AM（调幅）**：载波 + 两个边带，占带宽约 2×最高音频频率。
- **SSB（单边带，Single Sideband）**：把载波和一个边带抑制掉，只发另一个边带。话音 300~2700Hz 只占约 2.4kHz 带宽，同功率下比 AM 传得远得多。业余短波通联几乎全用 SSB：40m 波段习惯用 LSB（下边带），20m 以上习惯用 USB（上边带）。
- **CW（连续波电报）**：只发射等幅载波，用通断（keying）表示摩尔斯电码，占用带宽只有几百 Hz，是抗噪能力最强的模式。

SSB 的载波被抑制了，收到的只是「边带」——一个以调谐频率为中心、单侧延伸出去的频谱。CW 信号则近似一个单音。

### 2.2 为什么解调 SSB 需要「搬家」

把本振精确调到被抑制的载波频率上，正交检波后 SSB 信号变成基带复数信号：USB 的能量集中在 0~+2.6kHz，LSB 集中在 0~-2.6kHz。问题来了：

- 你想要 USB，但 -2.6k~0 的 LSB 干扰（别的电台！）就贴在旁边，两者只在**符号方向**上不同；
- 若直接取实部，正负频率混在一起，边带无法分开。

经典解法有二。**滤波法**：用一个极陡的带通滤波器只留一个边带——在音频上做 300~2700Hz 的滤波器无法抑制 0~300Hz 内贴着的对面边带残留，做陡滤波器很难。**Weaver 法**（本讲的主角）：先把频谱搬移，让想要的边带**对称地跨在 0Hz 上**，这时一个截止频率等于带宽一半的低通就能完美分离，最后再搬回音频。低通滤波器比陡峭的边带滤波器好做得多，这正是 Weaver 法的价值。

### 2.3 本讲要用的前置知识（来自前面各讲）

- **q15 定点与 NCO**（u3-l1）：int16 表示 [-1,1)，乘法后 `>>15` 归一化；`PHASESTEP(freq)` 宏把频率换算成 16 位相位累加器的整数步进；`cos_sin()` 用 256 项查值+差分表插值出正交载波，返回打包字 `(cos<<16)|sin`。
- **回调环境**（u2-l3）：`demod_weaver` 运行在 I2S 半满/全满中断回调里，每 5ms 处理 480 个 int16（240 对交织 IQ）；输入 `src` = `rx_buffer`，输出 `dst` = `tx_buffer`。
- **前端频谱方向**（u2-l2）：正交检波的 IQ 极性、以及 codec 片内 mini-DSP 的频谱倒置补偿（`config.freq_inverse = -1`，[main.c:161](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L161)）决定了「正频率」对应射频的哪一侧。代码里 usb/lsb 的符号选择是和这个前端极性配套调好的。
- **椭圆滤波器（elliptic filter）**：在通带和阻带都允许波纹的 IIR 滤波器，同阶数下过渡带最陡，适合本应用「贴身抑制邻道」的需求。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [dsp.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c) | 解调算法核心：`demod_weaver()`、usb/lsb/cw 三种配置、三组 biquad 系数表 |
| [nanosdr.h](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h) | `FS`、`SSB_FREQ_OFFSET`、`PHASESTEP` 宏与 `mode_freqoffset_phasestep` 等全局声明 |
| [main.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c) | `mod_table` 模式表、`set_modulation()`/`set_tune()` 接线、`cwtone` 命令 |
| [python/SSB-Filter-Design.ipynb](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/SSB-Filter-Design.ipynb) | SSB 1300Hz 椭圆低通的完整设计过程：zpk 分解→增益分配→q15 换算 |
| [python/CW-Filter-Design.ipynb](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/CW-Filter-Design.ipynb) | CW 150Hz 滤波器同款流程，末尾多了自动格式化输出的 `bq()` 函数 |
| [CMSIS/DSP_Lib/Source/FilteringFunctions/arm_biquad_cascade_df1_q15.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/CMSIS/DSP_Lib/Source/FilteringFunctions/arm_biquad_cascade_df1_q15.c) | CMSIS-DSP 的 q15 biquad 级联实现，64 位累加器 + SIMD 双样本处理 |

> 提示：链接到 `.ipynb` 的行号指向的是 notebook 的 JSON 源文本行，在 GitHub 页面上选择查看原始文件即可对应。

## 4. 核心概念与源码讲解

### 4.1 Weaver 法原理：搬两次家，中间过窄门

#### 4.1.1 概念说明

Weaver 法（也叫「第二代 SSB 解调」）的核心想法：**不要直接滤边带，先把边带搬到 0Hz 两侧变成「低通问题」**。设话音频带为 0~W（W = 1300Hz，即 2.6kHz 带宽的一半），三步如下：

1. **第一次混频（搬到中心）**：复数基带信号乘以 \( e^{-j2\pi f_c n/f_s} \)（\( f_c = W = 1300\,\text{Hz} \)），把感兴趣边带的中心搬到 0Hz——想要的边带对称落在 ±W 内，对面边带则被推远。
2. **低通滤波（过窄门）**：截止 W 的低通只留 0Hz 两侧 ±W 的内容——这一步等价于边带滤波器，但「对称低通」比「陡峭单边带滤波」容易实现得多。
3. **第二次混频取实部（搬回音频）**：再乘 \( e^{+j2\pi f_c n/f_s} \) 搬回音频位置，取实部得到 0~2W 的话音实信号，复制到左右声道输出。

#### 4.1.2 核心流程

```
rx_buffer (交织 IQ, q15)
   │
   ▼ ① NCO1 复数混频（±1300Hz，方向由配置符号决定）
buffer[0]=I', buffer[1]=Q'   —— 想要的边带对称跨在 0Hz
   │
   ▼ ② arm_biquad_cascade_df1_q15 × 2（I、Q 各一路，3 级级联椭圆低通）
buffer2[0]=I'', buffer2[1]=Q''  —— 邻道干扰被压掉 60dB
   │
   ▼ ③ NCO2 混频 + 取实部（反向搬回 1300Hz）
tx_buffer (左右声道同值, 交织)  —— 0~2600Hz 音频
```

USB 与 LSB 的全部差别在①③的**搬移方向**：方向翻了，选中的边带就翻到对面。

数学上，第一步对每个样本执行（\( \varphi_n \) 为 NCO1 相位累加器）：

\[
I'_n + jQ'_n = (I_n + jQ_n)\cdot(\sin\varphi_n + j\cos\varphi_n) = j\,(I_n + jQ_n)\,e^{-j\varphi_n}
\]

乘 \( e^{-j\varphi_n} \) 是频谱旋转（方向由 \( \varphi_n \) 的增减决定），多出来的常数因子 \( j \) 只是固定 90° 相转，不影响幅度谱。第三步（\( \theta_n \) 为 NCO2 相位累加器）：

\[
r_n = I''_n\cos\theta_n + Q''_n\sin\theta_n = \operatorname{Re}\{(I''_n + jQ''_n)\,e^{-j\theta_n}\}
\]

正是「混频 + 取实部」一步到位。

#### 4.1.3 源码精读

配置结构体把「两台 NCO 的步进 + 两路滤波器实例」打包在一起：

[dsp.c:332-344](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L332-L344) —— 定义 `weaver_demod_conf_t`（两个 int16 相位步进 + 两个滤波器实例指针），以及 usb/lsb 两份配置。**两份配置唯一的差别就是正负号**：

```c
const weaver_demod_conf_t usb_demod_conf = {
  SSB_NCO_PHASESTEP, SSB_NCO_PHASESTEP, &bq_i, &bq_q
};
const weaver_demod_conf_t lsb_demod_conf = {
  -SSB_NCO_PHASESTEP, -SSB_NCO_PHASESTEP, &bq_i, &bq_q
};
```

`SSB_NCO_PHASESTEP` 展开为 `PHASESTEP(SSB_FREQ_OFFSET)`（[dsp.c:286](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L286)），而 `PHASESTEP(freq)` 与 `SSB_FREQ_OFFSET` 定义在 [nanosdr.h:125-128](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L125-L128)：

```c
#define FS 48000
#define AM_FREQ_OFFSET 10000
#define SSB_FREQ_OFFSET 1300
#define PHASESTEP(freq) (65536L * freq / FS)
```

手算一下：`PHASESTEP(1300)` = 65536×1300/48000 = **1774**（整数截断，实际移频 1774×48000/65536 ≈ 1299.5Hz，量化误差约 0.5Hz——这就是 u3-l1 讲过的约 0.73Hz 步进分辨率）。

相位累加器是两个全局 uint16_t（[dsp.c:284-285](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L284-L285)）——必须是全局变量，才能在 5ms 一次的回调之间保持相位连续，否则每次块边界都会产生相位跳变咔哒声。

符号如何决定方向？注意 `demod_weaver` 里两个累加器一减一加：NCO1 是 `nco1_phase -= dc->phasestep1`，NCO2 是 `nco2_phase += dc->phasestep2`。以 USB 为例（phasestep 为 +1774）：NCO1 相位递减、NCO2 相位递增——两次旋转方向**必然相反**，正好实现「搬到 0Hz → 滤波 → 搬回音频」。把两处步进同时取负（LSB 配置），两次旋转同时反向，选中的就换成了对面的边带。至于「正号对应射频上方还是下方」，还与前端 IQ 极性和 u2-l2 讲过的 codec 频谱倒置（`freq_inverse = -1`）有关，固件中的符号是按整机实测校准的。

#### 4.1.4 代码实践

**实践目标**：不跑代码，纯手算，把「频谱搬家」变成肌肉记忆。

1. 代入 `PHASESTEP` 宏计算三个值：`PHASESTEP(1300)`、`PHASESTEP(10000)`、`PHASESTEP(800)`，并算出各自的量化后实际频率。
2. 画四条频谱轴（时间序列：混频前 → ①后 → ②后 → ③后），假设调谐 7.100MHz LSB、邻道 7.1005MHz 有个 USB 电台（音频内容 0~3kHz），标出两个信号在每一步的位置与带宽。
3. 回答：若把 `lsb_demod_conf` 的两个步进都改成 `+SSB_NCO_PHASESTEP`（即与 usb 相同），收 7.100MHz 时听到的边带会怎样变化？

**操作步骤**：纸笔完成；第 3 问可对照 [main.c:145-147](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L145-L147) 的默认信道表（7.100MHz 配 MOD_LSB、14.100MHz 配 MOD_USB）理解业余习惯。

**预期结果**：`PHASESTEP(1300)=1774`（1299.5Hz）、`PHASESTEP(10000)=13653`（9999.0Hz）、`PHASESTEP(800)=1092`（799.7Hz）；第 3 问——两配置完全相同则 LSB 档位解出的其实是 USB 边带（再叠加前端倒置极性，实际听到的是对面边带），无法正常收听本边带话音。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Weaver 法需要的低通截止是「带宽的一半」（1300Hz 对应 2.6kHz 带宽），而不是整个带宽 2.6Hz kHz？
**答案**：第一次混频把边带中心搬到 0Hz，能量对称分布在 0Hz 两侧各 1300Hz，所以只需 ±1300Hz（复数信号意义下的双边）低通；2.6kHz 是最终实音频的宽度。

**练习 2**：`nco1_phase`/`nco2_phase` 为什么不能是 `demod_weaver` 的局部变量？
**答案**：回调每 5ms 处理一块，若相位每块从 0 重新开始，NCO 在块边界跳变，输出出现周期性毛刺；全局变量让相位跨越数据块连续累积（CW 侧音、FM 立体声导频同理，见 u3-l5）。

**练习 3**：常数因子 \( j \)（90° 相转）混在第一级混频里，为什么可以不管？
**答案**：它对幅度谱、功率谱无影响，只给所有频率分量加固定 90° 相移；人耳与后续包络/功率检测对此不敏感，最终音频的相位本来就随发射机任意。

### 4.2 `demod_weaver()` 三级流水逐行精读

#### 4.2.1 概念说明

`demod_weaver` 是 USB/LSB/CW 三个模式共用的唯一实现，长度不到 40 行。它同时展示了三个惯用法：

- **配置驱动**：差异（NCO 步进符号、滤波器组）全部外置到 `weaver_demod_conf_t`，函数体零分支；
- **分离/交织缓冲**：交织 IQ 进来，先拆到 `buffer[0]`（I）/`buffer[1]`（Q）平面格式处理，最后再装回交织格式（u2-l3 讲过的固定套路）；
- **显示搭便车**：四级 `disp_fetch_samples` 钩子把流水线每一级的信号顺路送给频谱显示，零拷贝、不打断实时流程。

#### 4.2.2 核心流程

```
for 每个样本对 (len/2 = 240 次):
    cossin = cos_sin(nco1_phase); nco1_phase -= phasestep1
    I' = SMLSDX(iq, cossin) >> 15   // I·sin − Q·cos
    Q' = SMLAD (iq, cossin) >> 15   // I·cos + Q·sin
arm_biquad_cascade_df1_q15(bq_i, buffer[0] → buffer2[0])
arm_biquad_cascade_df1_q15(bq_q, buffer[1] → buffer2[1])
for 每个样本对:
    cossin = cos_sin(nco2_phase); nco2_phase += phasestep2
    r = SMLAD(iq'', cossin) >> 15   // I''·cos + Q''·sin = 实部
    *d++ = PKHBT(r, r)              // 同值写左右声道
```

#### 4.2.3 源码精读

完整函数在 [dsp.c:346-385](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L346-L385)。分三段看：

**第一级混频**（[dsp.c:357-364](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L357-L364)）——`__SIMD32(src)` 把交织缓冲当 32 位数组读，一次取出一对 IQ；两条 SIMD 乘加算出复数乘的两个分量，`>>15` 完成 q15 归一化（对应 4.1.2 的公式）：

```c
for (i = 0; i < len/2; i++) {
    uint32_t cossin = cos_sin(nco1_phase);
    nco1_phase -= dc->phasestep1;
    uint32_t iq = *s++;
    *bufi++ = __SMLSDX(iq, cossin, 0) >> (15-0);  // I·sin − Q·cos
    *bufq++ = __SMLAD(iq, cossin, 0) >> (15-0);   // I·cos + Q·sin
}
```

`__SMLAD(a,b,0)` = a.h×b.h + a.l×b.l；`__SMLSDX(a,b,0)` = a.h×b.l − a.l×b.h（交换相减）。cossin 打包为 `(cos<<16)|sin`，于是两条指令恰好给出复数乘的两个分量——这就是 u3-l1 讲过的「免拆包 SIMD 复混频」。

**低通滤波**（[dsp.c:368-369](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L368-L369)）——I、Q 两路各跑一个 3 级 biquad 级联，输入 `buffer`、输出 `buffer2`：

```c
arm_biquad_cascade_df1_q15(dc->bq_i, buffer[0], buffer2[0], len/2);
arm_biquad_cascade_df1_q15(dc->bq_q, buffer[1], buffer2[1], len/2);
```

**第二级混频取实部**（[dsp.c:373-382](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L373-L382)）——`__PKHBT` 把 I''、Q'' 重新打包成一个 32 位字，一条 `__SMLAD` 得到混频结果的实部；`__PKHBT(r, r, 16)` 把同一个样本写进左右两个声道：

```c
for (i = 0; i < len/2; i++) {
    uint32_t cossin = cos_sin(nco2_phase);
    nco2_phase += dc->phasestep2;
    uint32_t iq = __PKHBT(*bufi++, *bufq++, 16);
    uint32_t r = __SMLAD(iq, cossin, 0) >> (15-0);
    *d++ = __PKHBT(r, r, 16);        // 单声道 → 立体声复制
}
```

**显示钩子**：函数里穿插的 4 次 `disp_fetch_samples(B_CAPTURE / B_IF1 / B_IF2 / B_PLAYBACK, ...)`（[dsp.c:355](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L355)、[365](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L365)、[371](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L371)、[384](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L384)）把流水线入口、一级混频后、滤波后、输出的样本交给显示线程，当且仅当 `uistat.spdispmode` 匹配时才真正拷贝（[display.c:726-729](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L726-L729)），对应 `buffer_t` 枚举（[nanosdr.h:100](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L100)）和 `buffers_table`（[main.c:102-107](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L102-L107)）。细节留到 u4-l1。

三个薄包装完成模式注册（[dsp.c:387-397](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L387-L397)）——`lsb_demod`/`usb_demod` 各自把对应配置传给 `demod_weaver`，签名满足 `signal_process_func_t`（[nanosdr.h:112](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L112)），从而能被 `mod_table` 以函数指针热切换（u2-l3）。

#### 4.2.4 代码实践

**实践目标**：从「数据」角度走一遍三级流水——比较滤波前（B_IF1）与滤波后（B_IF2）的信号差别。

**操作步骤**（无硬件做步骤 1-3 源码阅读；有硬件继续 4-6）：

1. 读 [dsp.c:346-385](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L346-L385)，给每一行标注它属于三级中的哪一级、读写的是哪个缓冲。
2. 数一数：`len` 是多少个 int16？为什么两个循环都用 `len/2`？（答案：480；240 对 IQ。）
3. 追查 `buffer`/`buffer2` 的容量（[dsp.c:4-5](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L4-L5)、[nanosdr.h:93-98](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L93-L98)），确认 240 < 480 留有余量。
4. 有硬件时：用 `python/centsdr.py` 连接设备（u1-l4 的方法），`mode lsb` 后依次 `data 2`（取 `buffer[0]`，即一级混频后的 I 路）与 `data 3`（取 `buffer2[0]`，滤波后），抓取十六进制转储（[main.c:315-349](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L315-L349) 的 `cmd_data`；centsdr.py 的 `fetch_array` 封装在 [python/centsdr.py:77-86](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L77-L86)）。
5. 对两组数据分别做 FFT，比较幅度谱。
6. 注意：`cmd_data` 抓取瞬间数据仍在被中断回调更新，两次抓取并非同一段信号，比较应看统计特征（带宽、邻道抑制度）而非逐样本。

**需要观察的现象**：`data 3` 的频谱带宽明显收窄到约 ±1.3kHz、带外噪声台被压低；`data 2` 仍保留宽带内容。

**预期结果**：滤波前后频谱对比能看到 60dB 量级的阻带衰减。**待本地验证**（依赖硬件与天线环境）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 I、Q 两路要各自跑一个独立的 biquad 实例（`bq_i`、`bq_q`），而不是共用？
**答案**：两实例共享同一张系数表但**状态（历史样本）必须独立**——I 路的 x[n-1]、y[n-1] 与 Q 路互不相干；若共用状态，两路信号会在滤波器记忆里混叠。注意状态数组却又是共享的下一层事实：见 4.3.4 练习 3。

**练习 2**：第二级输出为什么写 `__PKHBT(r, r, 16)`（同值复制到两声道），而 FM 立体声模式却要写不同的左右值？
**答案**：SSB/CW 是单声道内容，复制即可；FM 立体声经和差矩阵分离出真正的 L/R（u3-l5）。

**练习 3**：如果把 `>> (15-0)` 改成 `>> (15-1)`（多右移一位），整个链路会发生什么？
**答案**：每级混频增益减半，两级共 -12dB，音量明显变小但不会失真；反之少移一位则可能让 I'、Q' 超出 int16 丢高位（q15 溢出），产生严重失真——定点代码里移位量就是「增益预算」。

### 4.3 CMSIS `arm_biquad_cascade_df1_q15`：级联 biquad 的执行者

#### 4.3.1 概念说明

**biquad**（bi-quad，双二阶）是数字滤波器的最小 IIR 积木——一个二阶节，传递函数：

\[
H(z) = \frac{b_0 + b_1 z^{-1} + b_2 z^{-2}}{1 + a_1 z^{-1} + a_2 z^{-2}}
\]

对应的差分方程：

\[
y[n] = b_0 x[n] + b_1 x[n-1] + b_2 x[n-2] - a_1 y[n-1] - a_2 y[n-2]
\]

高阶滤波器（本例 6 阶椭圆）分解成 3 个 biquad **级联**，每级保存 4 个状态（x[n-1]、x[n-2]、y[n-1]、y[n-2]）。**为什么分解**：直接实现 6 阶传递函数，系数对量化极度敏感（极点挤在单位圆附近时，int16 量化足以把滤波器推到不稳定）；拆成二阶节后每对共轭极点独立处理，量化鲁棒得多。

CMSIS-DSP 的 q15 版本用 **64 位累加器**保存中间结果（2.30 格式乘积累加成 34.30），彻底杜绝内部溢出，最后经 `postShift` 移位并饱和回 q15——用「宽位累加 + 尾部饱和」换全程无溢出。

#### 4.3.2 核心流程

```
对每个 stage (共 numStages=3):
    载入 6 系数 {b0,0,b1,b2,a1,a2}（SIMD 一次读 2 个）
    载入状态 {x[n-1],x[n-2]} 和 {y[n-1],y[n-2]}
    对每对样本（循环展开、双发射 SIMD）:
        acc = b0*x[n] + b1*x[n-1] + b2*x[n-2] + a1*y[n-1] + a2*y[n-2]
        acc += 1<<lShift          // 舍入偏置（本项目加的 FIX）
        out = saturate16(acc >> (15 - postShift))
        更新状态
```

注意 CMSIS 约定：**系数表里的 a1、a2 已经取过负号**，差分方程里做加法（见下面源码注释 `acc += a1*y[n-1] + a2*y[n-2]`）。这是从 scipy 系数迁移到 CMSIS 时最容易踩的坑——notebook 专门提醒了这一点。

#### 4.3.3 源码精读

实例初始化在 [dsp.c:289-309](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L289-L309)。SSB 系数表（当前生效的 60dB 版本，[dsp.c:300-306](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L300-L306)）：

```c
// 6th order elliptic lowpass filter fc=1300Hz, 60dB
q15_t bq_coeffs[] = {
          157, 0,   -238,   157, 31237, -14936,
         3643, 0,  -6974,  3643, 31656, -15580,
         8272, 0, -16096,  8272, 32074, -16158
};
arm_biquad_casd_df1_inst_q15 bq_i = { 3, bq_i_state, bq_coeffs, 1};
```

三行 = 三个 biquad 级；每行 6 个数 = `{b0, 0, b1, b2, a1, a2}`。结构体四个字段依次是：级数 3、状态数组、系数表、**postShift = 1**。状态数组 `bq_i_state[4 * 3]`（[dsp.c:290](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L290)）——每级 4 个状态 × 3 级。上方 `#if 0` 块（[dsp.c:292-298](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L292-L298)）是被淘汰的 40dB 旧设计，保留作对照。

CMSIS 实现在 [arm_biquad_cascade_df1_q15.c:75-79](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/CMSIS/DSP_Lib/Source/FilteringFunctions/arm_biquad_cascade_df1_q15.c#L75-L79)（函数签名），其文档注释 [L62-69](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/CMSIS/DSP_Lib/Source/FilteringFunctions/arm_biquad_cascade_df1_q15.c#L62-L69) 说明了 64 位累加与 postShift 机制。核心循环（[L102-144](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/CMSIS/DSP_Lib/Source/FilteringFunctions/arm_biquad_cascade_df1_q15.c#L102-L144)）：

[L104-117](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/CMSIS/DSP_Lib/Source/FilteringFunctions/arm_biquad_cascade_df1_q15.c#L104-L117) —— 用 `__SIMD32` 一次读两个系数、两个状态，为双样本展开做准备。

[L119-141](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/CMSIS/DSP_Lib/Source/FilteringFunctions/arm_biquad_cascade_df1_q15.c#L119-L141) —— 双样本 SIMD 计算。注释（L120-124）写明差分方程 `acc = b0*x[n] + b1*x[n-1] + b2*x[n-2] + a1*y[n-1] + a2*y[n-2]`，对应指令序列：

```c
out = __SMUAD(b0, in);                       // b0*x[n] + 0*0
acc = __SMLALD(b1, state_in, out);           // += b1*x[n-1] + b2*x[n-2]
acc = __SMLALD(a1, state_out, acc);          // += a1*y[n-1] + a2*y[n-2]
acc += 1 << lShift;                          // FIX: 补偿移位造成的直流偏置
```

其中 [L143-144](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/CMSIS/DSP_Lib/Source/FilteringFunctions/arm_biquad_cascade_df1_q15.c#L143-L144) 那行 `acc += 1 << lShift` 注释标明是 **FIX**——为算术右移补一个舍入偏置、避免截断引入直流漂移，这是 CentSDR 作者在本地副本上加的小补丁，回答了「为什么 SSB 解调后直流分量很小」的一个细节。

`__SMLALD` 是 32×32 中「双 16 位乘、64 位加」的 SIMD 指令（u5-l2 会系统梳理）；`lShift = 15 − postShift`（[L96](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/CMSIS/DSP_Lib/Source/FilteringFunctions/arm_biquad_cascade_df1_q15.c#L96)）：postShift=1 时累加结果少右移一位 = 增益 ×2，恰好补回系数预缩放的 1/2。

#### 4.3.4 代码实践

**实践目标**：在 PC 上用 Python 逐样本复现 CMSIS 的差分方程，跑 `dsp.c` 里的**真实 q15 系数**，验证 60dB 阻带没有被 int16 量化破坏。

**操作步骤**：

1. 把 [dsp.c:301-305](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L301-L305) 的三行系数抄进 Python。
2. 写下面这个 20 行左右的自包含脚本（**示例代码**，非项目原有文件）：

```python
import numpy as np
from scipy.signal import freqz

coeffs = np.array([
    [157, 0, -238, 157, 31237, -14936],
    [3643, 0, -6974, 3643, 31656, -15580],
    [8272, 0, -16096, 8272, 32074, -16158]], dtype=np.int64)

def biquad_q15(x, c, post_shift=1):
    b0, _, b1, b2, a1, a2 = c          # a 已取反（CMSIS 约定）
    x1 = x2 = y1 = y2 = 0
    out = np.empty_like(x)
    for n, xn in enumerate(x):
        acc = b0*xn + b1*x1 + b2*x2 + a1*y1 + a2*y2  # 64 位整数域
        y = np.clip(acc >> (15 - post_shift), -32768, 32767)  # 饱和
        x2, x1 = x1, xn; y2, y1 = y1, y; out[n] = y
    return out

# 冲激响应 → 频率响应（级联三级，输入幅度用 4096 防饱和）
N = 4096
imp = np.zeros(N, dtype=np.int64); imp[0] = 4096
h = imp
for c in coeffs:
    h = biquad_q15(h, c)
H = np.fft.rfft(h.astype(float))
```

3. 画 \( 20\log_{10}|H| \) 对频率（0~3kHz）的曲线；再用 2.6kHz 以上的「邻道单音」当输入跑时域，测稳态输出幅度。

**需要观察的现象**：0~1300Hz 通带平坦（约 1dB 波纹）；1300Hz 后快速跌落，2kHz 以后低于 -60dB；时域实验中带外单音输出接近满量程的 1/1000 以下。

**预期结果**：阻带衰减在 -60dB 附近（与 [SSB-Filter-Design.ipynb L157](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/SSB-Filter-Design.ipynb#L157) 的浮点设计 `ellip(6, 1, 60, ...)` 一致），说明 16 位量化在本组系数下损失可接受。**待本地验证**（具体 dB 数值以运行结果为准）。

#### 4.3.5 小练习与答案

**练习 1**：notebook 为什么把系数乘 16384（2^14）而不是标准的 32768（2^15）？固件里哪一项设置与之配套？
**答案**：反馈系数 a1 ≈ -1.93 超出 q15 的 [-1,1) 表示范围，全体系数右移 1 位（乘 2^14）才能放进 int16；配套的是实例初始化的 `postShift = 1`（[dsp.c:308](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L308)），输出端少移一位补回 2 倍增益。

**练习 2**：6 阶滤波器为什么分成 3 个 biquad 而不是 1 个 6 阶直接型？
**答案**：见 4.3.1——极点成对分组后系数敏感度大幅下降，int16 量化下仍稳定；直接型高阶的反馈系数大数值相消，量化误差会显著改变特性甚至不稳定。

**练习 3**：观察 [dsp.c:308-309](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L308-L309)、[319-320](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L319-L320)、[329-330](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L329-L330)：SSB、AM、CW 的滤波器实例共享同一个 `bq_i_state`/`bq_q_state` 状态数组。切换模式时会发生什么？有什么影响？
**答案**：三个实例（系数不同）共用同一块状态内存，切模式瞬间新滤波器从旧滤波器的历史样本「冷启动」——输出会有一个短暂的过渡（毫秒级），IIR 稳定后自行消失；代价是省 RAM，收益大于偶尔的切换瞬态。

### 4.4 CW 模式：同一函数的第三种配置

#### 4.4.1 概念说明

CW（电报）只需要听一个几百 Hz 带宽里的单音，用 Weaver 框架实现它只差**换两个步进、换一组系数**——但这两个步进不再是编译期常量，而是运行期变量：

- **第一级 NCO = 10kHz**：CW 的 `mod_table` 条目带 `freq_offset = AM_FREQ_OFFSET`（10000Hz），`set_tune` 让本振偏离信号 10kHz，CW 信号落在 10kHz 中频上；第一级混频把它搬回 0Hz。
- **低通 = 150Hz**：±150Hz 即 300Hz 接收带宽，比 SSB 窄近 9 倍，邻台挤压能力极强。
- **第二级 NCO = 侧音频率（默认 800Hz）**：把 0Hz 的 CW 单音搬移到人耳敏感的音频，操作者听到的「哔」声高低可由 `cwtone` 命令实时调整。

#### 4.4.2 核心流程

```
shell: mode cw → set_modulation(MOD_CW)
    ├─ mod_table[MOD_CW] = { cw_demod, 10000Hz, fs=48, "cw" }
    ├─ mode_freqoffset_phasestep = PHASESTEP(10000) = 13653
    ├─ cw_tone_phasestep = PHASESTEP(uistat.cw_tone_freq=800) = 1092
    └─ signal_process = cw_demod          ← 函数指针热切换
每次回调: cw_demod
    └─ 栈上组装 dc = { 13653, 1092, &bq_cw_i, &bq_cw_q }
        └─ demod_weaver(src, dst, len, &dc)
shell: cwtone 600 → uistat.cw_tone_freq=600 → update_cwtone()
    └─ cw_tone_phasestep = PHASESTEP(600)  ← 下一回调立即生效
```

#### 4.4.3 源码精读

[dsp.c:399-407](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L399-L407) —— `cw_demod` 每次**在栈上**组装配置，两个步进直接引用全局变量（而不是像 usb/lsb 那样的 const 编译期常量），因此 `cwtone` 命令改完全局变量后下一次回调立即生效，无需任何重启：

```c
void
cw_demod(int16_t *src, int16_t *dst, size_t len)
{
  weaver_demod_conf_t dc = {
    mode_freqoffset_phasestep, cw_tone_phasestep, &bq_cw_i, &bq_cw_q
  };
  demod_weaver(src, dst, len, &dc);
}
```

150Hz 滤波器系数与实例在 [dsp.c:322-330](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L322-L330)，由 [CW-Filter-Design.ipynb](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/CW-Filter-Design.ipynb) 设计（见 4.5）。

接线在 main.c：[main.c:165-177](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L165-L177) 的 `mod_table` 中 CW 条目 `{ cw_demod, AM_FREQ_OFFSET, 48, "cw" }`；[main.c:179-194](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L179-L194) 的 `set_modulation` 据此设置采样率、替换 `signal_process` 函数指针、换算两个相位步进：

```c
mode_freq_offset = mod_table[mod].freq_offset;
mode_freqoffset_phasestep = PHASESTEP(mode_freq_offset);
cw_tone_phasestep = PHASESTEP(uistat.cw_tone_freq);
```

[main.c:196-201](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L196-L201) 的 `set_tune` 把 10kHz 偏移算进本振频率（`center_frequency = hz - mode_freq_offset`，再 ×4 下发给 SI5351，见 u2-l1）——所以 CW 时 LCD 显示的频率是信号频率，而本振实际低 10kHz。

侧音运行时调整：[main.c:681-698](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L681-L698) 的 `cmd_cwtone` 写 `uistat.cw_tone_freq` 后调用 [main.c:228-232](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L228-L232) 的 `update_cwtone` 重算步进。默认值 800Hz 来自 [main.c:116](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L116) 与 [main.c:137](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L137)。

#### 4.4.4 代码实践

**实践目标**：验证「侧音频率 = 第二级 NCO」这一机制。

**操作步骤**（有硬件时）：

1. 按u1-l4 的方法连接 shell，`mode cw` 切到电报模式。
2. `cwtone`（无参数）读回当前值，应为 800。
3. `cwtone 600`、`cwtone 1000` 分别设置，戴上耳机听 CW 背景噪声的音调变化；或用 `data 1` 抓 `tx_buffer` 做 FFT，找主能量峰。
4. 无硬件时做源码推演：沿 `cwtone 600` → `uistat.cw_tone_freq = 600` → `update_cwtone()` → `cw_tone_phasestep = PHASESTEP(600) = 819` → 下一次回调 `cw_demod` 组装的 `dc.phasestep2` 这一链条，在纸上写出每个变量的值。

**需要观察的现象**：侧音音调随 `cwtone` 设置立即变化（无需重新调谐）；FFT 主峰从 800Hz 移到设置值附近。

**预期结果**：峰位误差在 ±1Hz 内（16 位相位步进量化分辨率 48000/65536 ≈ 0.73Hz）。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：CW 的 10kHz 偏移和 SSB 的 1300Hz 偏移，本质区别是什么？
**答案**：SSB 的 1300Hz 是 Weaver 搬移的「带宽一半」，服务于边带选择；CW 的 10kHz 是**中频**——把窄带信号先搬到远离直流的中频（避开直流偏置和 1/f 噪声区），解调时再搬回来。第二级的 800Hz 才对应 Weaver 的「搬回音频」。

**练习 2**：`cw_demod` 为什么每次回调都在栈上重新组装 `dc`，而不是像 usb/lsb 那样定义一份 const 全局配置？
**答案**：CW 的两个步进是运行期可变的（侧音可调、且随 `mode_freq_offset` 由 `set_modulation` 计算），组装时读全局变量才能拿到最新值；usb/lsb 的 ±1300Hz 是纯常量，放 const 表还能进 Flash 省 RAM。

**练习 3**：把 CW 采样率换成 192kHz 会遇到什么问题？
**答案**：`PHASESTEP` 宏用写死的 `FS=48000`（[nanosdr.h:125-128](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L125-L128)），192kHz 下步进算错 4 倍，NCO 频率全错；且 150Hz 滤波器系数是按 48kHz 设计的，采样率变了整个频率响应被拉伸——这就是 `mod_table` 给 CW 固定 `fs=48`、由 `set_fs` 保证采样率配套的原因（u2-l3 讲过切换握手）。

### 4.5 从 Notebook 到固件：椭圆滤波器设计与 q15 系数换算

#### 4.5.1 概念说明

`dsp.c` 里的三组系数不是手写的，而是由 `python/` 下两个 Jupyter notebook 用 scipy 设计后「翻译」过去的。这条「notebook 设计 → 定点换算 → 粘贴进固件」的流水线是 CentSDR 算法迭代的核心工作流，共五步：

1. **规格与设计**：`signal.ellip(6, 1, 60, 1300/24000, 'low')`——6 阶椭圆、通带波纹 1dB、阻带衰减 60dB、截止 1300Hz（归一化到奈奎斯特 = 1300/24000）。
2. **zpk 分解**：以零极点增益形式重新设计（`output='zpk'`），把 3 对共轭零点、3 对共轭极点各配成对，`zpk2tf` 还原出 3 个二阶节。
3. **增益分配**：总增益 k 按 `k**0.68 / k**0.22 / k**0.1` 拆到三级，让最先处理的级增益最低（先衰减），防止中间结果溢出 int16。
4. **定点换算**：`np.rint(b*16384)`——乘 2^14 并取整（原因见 4.3 练习 1）。
5. **格式化粘贴**：按 CMSIS 的 `{b0, 0, b1, b2, -a1, -a2}` 排列（a 取反、第二个恒 0），粘进 `dsp.c`。

SSB notebook 的演进还记录了一个工程判断：初版只有 40dB 阻带（notebook [L40](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/SSB-Filter-Design.ipynb#L40) 写明「感到 40dB 阻带不足而重新设计」），重设计到 60dB——`dsp.c` 里 `#if 0` 的旧系数表正是那次升级的化石。规格文字里写的目标甚至是 80dB（[L57](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/SSB-Filter-Design.ipynb#L57)），最终代码采用 60dB 版本：**指标、实现、注释三者要对着看，以代码为准**。

#### 4.5.2 核心流程

```
ellip(6, 1, 60, fc/24000, 'low', output='zpk')   → z[], p[], k
        │  按 z[n::3], p[m::3] 取共轭对
        ▼
zpk2tf(z[n::3], p[m::3], k**e_n)                 → b, a（浮点，共 3 组）
        │  np.rint(x * 16384)
        ▼
q15 整数（int16 范围内）
        │  排列 {b0, 0, b1, b2, -a1, -a2}，每级一行
        ▼
dsp.c 的 bq_coeffs[] / bq_coeffs_150hz[]
```

#### 4.5.3 源码精读

**SSB 设计**（[python/SSB-Filter-Design.ipynb](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/SSB-Filter-Design.ipynb)，行号指 JSON 源文本）：

- [L157](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/SSB-Filter-Design.ipynb#L157)：`signal.ellip(6, 1, 60, 1300.0/24000, 'low')` 最终采用的 60dB 设计。
- [L229](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/SSB-Filter-Design.ipynb#L229)：同规格的 zpk 形式，得到零极点数组。
- [L297-306](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/SSB-Filter-Design.ipynb#L297-L306)：`plot_biquad(n, m, kk)` 用 `z[n::3]`、`p[m::3]` 取共轭对组成二阶节，增益指数 0.68/0.22/0.1（和为 1，大头给最先处理、衰减最陡的级）。
- [L317](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/SSB-Filter-Design.ipynb#L317)：增益分配的动机说明——为不发生溢出，按「先衰减」排序各级。
- [L364](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/SSB-Filter-Design.ipynb#L364)：换算说明——系数有超过 1 的，右移 1 位乘 16384。
- [L383-385](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/SSB-Filter-Design.ipynb#L383-L385)：`np.rint(b*16384), np.rint(a*16384)`；其输出（[L376-378](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/SSB-Filter-Design.ipynb#L376-L378)）正是 `dsp.c` 系数表的来源，例如第一级 `[157, -238, 157] [16384, -31237, 14936]` → 固件里的 `157, 0, -238, 157, 31237, -14936`（a 取反）。

**CW 设计**（[python/CW-Filter-Design.ipynb](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/CW-Filter-Design.ipynb)）流程相同：

- [L142](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/CW-Filter-Design.ipynb#L142)：`ellip(6, 1, 60, 150.0/24000, 'low')`。
- [L407-410](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/CW-Filter-Design.ipynb#L407-L410)：CW notebook 比 SSB 多了一个自动格式化函数，直接打印可粘贴的 C 代码，把「取整、排列、a 取反」固化成代码：

```python
def bq(b, a):
    b, a = np.rint(b*16384), np.rint(a*16384)
    print "\t" + "".join(["%d, "%v for v in [b[0], 0, b[1], b[2], -a[1], -a[2]]])
```

其输出即 [dsp.c:323-327](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L323-L327) 的 `bq_coeffs_150hz[]`，逐字对应。

> 注意：两个 notebook 都是 **Python 2** 语法（`print x`、`"".join(...)` 无括号打印）且是旧版 ipynb 格式。在现代 Python 3 + Jupyter 环境打开时，需把 print 语句改成函数调用，或直接把单元格代码复制到一个新 notebook/脚本里运行。

#### 4.5.4 代码实践（本讲主实践）

**实践目标**：完整走一遍「改指标 → 重设计 → 定点换算 → 验证」的算法迭代闭环，把 SSB 滤波器截止从 1300Hz 改为 1800Hz（话音带宽 2.6k → 3.6kHz）。

**操作步骤**：

1. 复制 `python/SSB-Filter-Design.ipynb` 为自己的实验副本（避免改动仓库文件），在 Python 3 环境（scipy/numpy/matplotlib）中逐格执行，先原样复现 1300Hz 的结果。
2. 把两处设计频率 `1300.0/24000` 改为 `1800.0/24000`（zpk 版与 ba 版都要改）。
3. 检查增益分配：`k**0.68 / k**0.22 / k**0.1` 的拆分是针对 1300Hz 手调的，1800Hz 时 k 值不同——观察 `plot_biquad` 三条曲线的峰值，确保没有任何一级的通带增益明显大于 1（否则量化后中间级可能饱和），必要时微调三个指数（保持和为 1）。
4. 换算与自检（**示例代码**）：

```python
import numpy as np
from scipy.signal import ellip, zpk2tf, freqz

z, p, k = ellip(6, 1, 60, 1800.0/24000, 'low', output='zpk')
b0, a0 = zpk2tf(z[0::3], p[0::3], k**0.68)
b1, a1 = zpk2tf(z[1::3], p[1::3], k**0.22)
b2, a2 = zpk2tf(z[2::3], p[2::3], k**0.10)

for b, a in [(b0,a0), (b1,a1), (b2,a2)]:
    bq, aq = np.rint(b*16384), np.rint(a*16384)
    assert np.all(np.abs(bq) <= 32767) and np.all(np.abs(aq) <= 32767), "超出 int16！"
    print("\t%d, 0, %d, %d, %d, %d," % (bq[0], bq[1], bq[2], -aq[1], -aq[2]))
```

5. 用 4.3.4 的整数域 biquad 脚本（或直接对量化系数级联做 `freqz`）验证新系数：通带 0~1800Hz 波纹 ≤1dB，阻带 ≤ -60dB。
6. （可选，有硬件且明确在实验分支上）把打印出的三行粘贴替换 [dsp.c:301-304](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L301-L304) 的 `bq_coeffs`，**同时**把 [nanosdr.h:127](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L127) 的 `SSB_FREQ_OFFSET` 从 1300 改为 1800（NCO 搬移量必须等于滤波器截止，两者是同一个「带宽一半」），重新编译烧录。

**需要观察的现象**：新设计在 1800Hz 后滚降，2.5kHz 以上阻带低于 -60dB；三行 q15 系数全部通过 int16 断言；漏改 `SSB_FREQ_OFFSET` 时（只换滤波器）SSB 话音会缺失 1300~1800Hz 频段——滤波器放行了，但 NCO 没把那部分搬进通带。

**预期结果**：阻带指标与 `ellip` 的第三参数 60 一致；改 `SSB_FREQ_OFFSET` 后整机 SSB 带宽展宽到约 3.6kHz。**待本地验证**（频响曲线数值以本机运行结果为准）。

#### 4.5.5 小练习与答案

**练习 1**：CMSIS 系数行的第 2 个数恒为 0，它对应差分方程的哪一项？为什么是 0？
**答案**：对应 `b0 * x[n]` 展开里的 SIMD 配对占位——CMSIS 用 `__SIMD32` 一次读 (b0, 0) 和 (b1, b2)，这个 0 让 `__SMUAD(b0, in)` 只取当前样本与 b0 相乘（另一个半字乘 0），是数据排布约定的产物。

**练习 2**：阻带衰减从 40dB 提到 60dB，为什么不能「免费」获得？代价体现在哪里？
**答案**：同阶数下要更陡的过渡带就得允许更大通带波纹或更高阶；本设计保持 6 阶、1dB 波纹，靠椭圆特性逼近 60dB，代价是系数更极端（第一级 b0 从 515 缩到 157，增益更早压低）、对量化更敏感，这也是 notebook 要仔细做增益分配的原因。

**练习 3**：如果想要 500Hz 带宽的「窄带 SSB」（如竞赛用），沿本讲流程要改哪几处？
**答案**：notebook 中截止改 250Hz 重新设计（可参考 CW notebook，它就是 150Hz 的同款流程）；固件中替换一组新系数表、新增/复用 biquad 实例（注意 [dsp.c:312-320](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L312-L320) AM 滤波器 `bq_coeffs_am` 就是这么来的：截止 6×1300=7800Hz 服务 10kHz 中频的 AM）；`SSB_FREQ_OFFSET` 改 250。u5-l4 会把「新增一种解调模式」作为完整的二次开发实战。

## 5. 综合实践

**任务：给 CentSDR 设计并验证一个「3.6kHz 宽带 SSB」补丁包。**

综合运用本讲全部知识，产出三样东西：

1. **设计文档**（一份 notebook 或脚本）：按 4.5.4 流程产出 1800Hz/60dB 椭圆滤波器的三行 q15 系数，附频率响应曲线与增益分配说明（哪些级先衰减、为什么）。
2. **验证报告**：用 4.3.4 的整数域 biquad 模拟器跑量化后系数，给出实测通带波纹与阻带衰减，与浮点设计对比，回答「int16 量化损失了多少 dB」。
3. **固件改动清单**：列出把新滤波器接入固件需要动的每一处——`dsp.c` 系数表、`nanosdr.h` 的 `SSB_FREQ_OFFSET`、（若做新模式）`mod_table` 与 `set_modulation` 的接线，参照 4.4.3 的 CW 接线图说明每处改动的理由。

有硬件者可进一步烧录实测：切换 `spdispmode` 到 IF 档观察滤波前后频谱（4.2.4 的方法），收一个已知边带信号对比改前改后的话音带宽差异。完成后，你就独立走过了一次 CentSDR 算法迭代的全流程——这也是 u5-l5「从 Notebook 到固件工作流」的预演。

## 6. 本讲小结

- **Weaver 法三级流水**：NCO 混频把边带搬到 0Hz 对称位置 → ±1300Hz（SSB）或 ±150Hz（CW）椭圆低通滤除邻道 → 反向 NCO 混频取实部搬回音频；`demod_weaver`（[dsp.c:346-385](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L346-L385)）是三个模式共用的唯一实现。
- **边带选择只差一个符号**：`usb_demod_conf` 与 `lsb_demod_conf` 的两台 NCO 步进互为相反数，配合「NCO1 相减、NCO2 相加」的固定结构，翻转两次搬移方向即翻转边带。
- **CMSIS biquad 约定**：系数排列 `{b0, 0, b1, b2, a1, a2}` 且 a 已取反；反馈系数超 1 所以全体乘 16384，配套 `postShift=1`；64 位累加器保证内部无溢出。
- **CW 是第三种配置**：10kHz 中频步进 + 运行时可调的侧音步进（默认 800Hz，`cwtone` 命令热更新）+ 150Hz 窄带系数，在栈上组装 `dc` 复用同一函数。
- **notebook 是算法源头**：`ellip` 设计 → zpk 拆二阶节 → `k**0.68/0.22/0.1` 增益分配防溢出 → `rint(x*16384)` 定点化 → 按 CMSIS 格式粘贴；40dB→60dB 的演进记录在 notebook 与 `#if 0` 化石里。
- **两个必须联动的常量**：滤波器截止（系数表）与 `SSB_FREQ_OFFSET`（NCO 步进）描述同一个「带宽一半」，改带宽必须同时改两处。

## 7. 下一步学习建议

- **下一讲 u3-l3（AM 解调）**：`am_demod` 的前半段与本讲的混频+滤波结构几乎相同（10kHz 中频 + 7800Hz 低通，[dsp.c:312-320](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L312-L320)），后半段换成 `_VSQRTF` 硬件开方包络检波——学完本讲再读它会非常轻松，重点体会「同一 Weaver 骨架 + 不同检波尾段」的复用模式。
- **再往后 u3-l4（FM 鉴频）**：离开 Weaver 框架，看相位差分鉴频如何用上一讲的 `atan_2iq` 实现。
- **源码精读建议**：把 [dsp.c:284-407](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L284-L407) 从头到尾抄写一遍注释——这段 120 行代码浓缩了定点 NCO、SIMD 复混频、biquad 级联、表驱动配置四个惯用法，是整个 dsp.c 的骨架。
- **动手方向**：完成第 5 节综合实践后，可提前试做 u5-l4 的「新增解调模式」——一个带 600Hz 滤波器的窄带 CW 变体，所需知识本讲已全部覆盖。
