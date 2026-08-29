# FM 立体声：导频 PLL、去加重与和差矩阵

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 FM 立体声复合基带信号的频谱结构，说出 19kHz 导频与 38kHz 抑制载波副信道各自的作用。
2. 走读 `fm_demod_stereo()` 的五步流水线，说清每一步的输入输出。
3. 解释 `stereo_separate()` 中导频相关运算为什么能得到相位误差（dq/di 即相位差），以及「比例项 corr\*128 + 积分项 integrator」两级反馈如何构成一个软件 PLL。
4. 用 `stereo_matrix2()` 的和差运算推导出左右声道的恢复公式，并解释为什么用饱和加减指令。
5. 在 PC 上提取 `stereo_separate()` 代码，亲手观察 PLL 的收敛过程与失锁行为。

## 2. 前置知识

### 2.1 FM 立体声广播的「和差制」

如果直接把左声道 L 放一条 FM 副载波、右声道 R 放另一条，单声道收音机就无法兼容。所以 FM 立体声广播采用**和差制（sum-difference）**：不发 L 和 R，而发 `(L+R)` 和 `(L−R)`。

发端把三个分量叠加成一路**复合基带信号（composite signal）**再去调频：

\[ s(t) = (L+R) + P\sin(2\pi \cdot 19000\, t) + (L-R)\sin(2\pi \cdot 38000\, t) \]

- `(L+R)`：占 0~15kHz 基带，单声道收音机只解调这一段，天然兼容。
- `P·sin(2π·19kHz·t)`：**导频（pilot）**，幅度只有 10% 左右，专门留给接收机「对表」用。
- `(L−R)·sin(2π·38kHz·t)`：**抑制载波副信道（DSB-SC）**。载波 38kHz 被抑制掉（不浪费功率），只留上下边带（23~53kHz）。

关键难题：DSB-SC 解调必须有一个与发端**同频同相**的 38kHz 本地载波，否则解出的 `(L−R)` 会严重失真。发端不会替你发 38kHz 载波，但发了 19kHz 导频——而 38kHz 恰好是 19kHz 的二倍频。于是接收机的任务变成：**锁一个 19kHz 振荡器到导频上，再倍频得到 38kHz**。这正是本讲主角 `stereo_separate()` 做的事。

### 2.2 锁相环（PLL）的最小模型

一个 PLL 由三部分组成：

- **鉴相器（PD）**：比较输入信号与本地振荡的相位差，输出误差信号。
- **环路滤波器**：对误差做低通/积分，决定跟踪快慢与稳态精度。
- **压控振荡器（NCO）**：误差反过来微调振荡频率，形成负反馈。

CentSDR 的实现里这三部分全部由整数运算完成：鉴相器是「相关 + 除法」，环路滤波器是「比例 + 积分」，振荡器就是 u3-l1 学过的 NCO 相位累加器——只不过这次用的是 **32 位**版本。

### 2.3 相关运算与相位差

设本地 NCO 输出 \( \cos\theta_n, \sin\theta_n \)，其中 \( \theta_n = \omega_0 n + \varphi \)（\( \varphi \) 是本地与导频的相位偏差）；接收到的导频是 \( A\cos\omega_0 n \)。在一个数据块内做相关求和，利用三角恒等式并注意到快变交叉项在块内近似抵消：

\[ d_i = \sum_n \cos\theta_n \cdot A\cos\omega_0 n \approx \frac{AN}{2}\cos\varphi, \qquad d_q = \sum_n \sin\theta_n \cdot A\cos\omega_0 n \approx \frac{AN}{2}\sin\varphi \]

也就是说：**相关向量的辐角就是相位误差 \( \varphi \)，模长正比于导频幅度**。这是理解 `di`、`dq`、`corr` 三个变量的钥匙。

### 2.4 与上一讲的衔接

u3-l4 已经讲过：FM 信息藏在相邻样本的相位差里，鉴频器 `atan_2iq()` 输出就是瞬时频率，即复合基带信号；`fm_demod_state` 负责跨缓冲块保持状态。本讲从鉴频器的输出继续往下走。

## 3. 本讲源码地图

| 文件 | 关键内容 | 行号 |
|---|---|---|
| `dsp.c` | `fm_demod_stereo()` 立体声解调总装 | L806-L820 |
| `dsp.c` | `fm_adj_filter()` 频响校正滤波器 | L751-L789 |
| `dsp.c` | `fm_demod0()` 裸鉴频（无显示挂钩） | L791-L804 |
| `dsp.c` | `stereo_separate()` 导频 PLL + 38kHz 搬移 | L612-L683 |
| `dsp.c` | `stereo_separate_init()` / `PHASESTEP_NCO19KHz` | L593-L607 |
| `dsp.c` | `stereo_matrix()` / `stereo_matrix2()` / `stereo_matrix3()` 和差矩阵 | L686-L749 |
| `dsp.c` | `dsp_init()` 调用 PLL 初始化 | L894-L898 |
| `nanosdr.h` | `stereo_separate_state_t` 状态结构体 | L148-L162 |
| `main.c` | `mod_table` 模式表与 `set_modulation()` 接线 | L165-L194 |
| `main.c` | `i2s_end_callback()` 解调入口 | L258-L276 |
| `main.c` | `stat` 命令打印 PLL 内部状态 | L440-L442 |

## 4. 核心概念与源码讲解

### 4.1 立体声复合信号与解调总装流水线

#### 4.1.1 概念说明

`fm_demod_stereo()` 是六种解调模式中最长的链路：它要在单声道的「鉴频」之外，额外完成「再生 38kHz 载波、分离副信道、和差矩阵」三件事。理解它的最好方式是把整条链路看成一个五步流水线，每一步的产物都落到一个具名的缓冲区里。

#### 4.1.2 核心流程

```text
rx_buffer (192kHz 交织 IQ, 240 对/块)
   │
   ▼ ① fm_adj_filter()     原地频响校正（I/Q 各 3 抽头 FIR）
   ▼ ② fm_demod0()         鉴频：相位差分 → 实数基带, 存 buffer[0] (240 样本)
   ▼ ③ stereo_separate()   导频 PLL + 38kHz 下变频, 副信道存 buffer2[0]
   ▼ ④ stereo_matrix2()    和差矩阵：L=(s1+s2)/1, R=(s1−s2)/1, 交织写入 tx_buffer
   ▼ ⑤ DAC 双声道播放
```

注意长度单位的换算：回调传入的 `len` 以 **16 位样本**计，一块为 480（240 对 IQ）。鉴频后每个 IQ 对产出一个实数样本，所以从第③步起长度都除以 2，变成 240。

#### 4.1.3 源码精读

总装函数只有 10 行，每行对应流水线的一步：

[dsp.c:806-820](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L806-L820) —— `fm_demod_stereo()` 依次调用频响校正、鉴频、副信道分离与和差矩阵；`disp_fetch_samples()` 是显示模块的「搭便车」取样点（u4-l1 会展开），不影响信号流。

```c
void
fm_demod_stereo(int16_t *src, int16_t *dst, size_t len)
{
  // apply frequency response adjustment
  fm_adj_filter(src, len);

  disp_fetch_samples(B_CAPTURE, BT_C_INTERLEAVE, src, NULL, len);
  fm_demod0(src, buffer[0], len);
  disp_fetch_samples(B_IF1, BT_REAL, buffer[0], NULL, len/2);
  stereo_separate(buffer[0], buffer2[0], len/2);
  disp_fetch_samples(B_IF2, BT_REAL, buffer2[0], NULL, len/2);
  stereo_matrix2(buffer[0], buffer2[0], dst, len/2);
  disp_fetch_samples(B_PLAYBACK, BT_R_INTERLEAVE, dst, NULL, len);
}
```

它在模式表中的注册——`mod_table` 每行四个字段：解调函数、本振频率偏移、采样率、名字。`fms` 模式偏移为 0（载波置于 0Hz）、采样率 192kHz：

[main.c:165-177](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L165-L177) —— 表驱动注册：切换模式即换 `signal_process` 函数指针并设置采样率。

```c
} mod_table[] = {
  { cw_demod, AM_FREQ_OFFSET,  48, "cw" },
  ...
  { fm_demod,              0, 192, "fm" },
  { fm_demod_stereo,       0, 192, "fms" },
};
```

[main.c:179-194](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L179-L194) —— `set_modulation()` 把表项安装到 `signal_process`，并调用 `set_fs(192)` 完成编解码器与 I2S 的采样率切换握手（u2-l3）。

调用点在 I2S 中断回调里，`n` 就是一块的样本数 480：

[main.c:258-276](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L258-L276) —— `(*signal_process)(p, q, n)` 在中断上下文里执行整条解调链。

跨块状态集中在 `fm_demod_state`：`last` 是鉴频器上一块最后一个 IQ 样本（u3-l4），`pre1`/`pre2` 是频响校正滤波器的历史——本讲新增的部分：

[dsp.c:487-491](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L487-L491) —— FM 家族共享的状态结构体。

```c
struct {
  uint32_t last;
  uint32_t pre1;
  uint32_t pre2;
} fm_demod_state;
```

#### 4.1.4 代码实践

1. **实践目标**：在有硬件时直接观察 PLL 内部状态；无硬件时完成调用链走读。
2. **操作步骤**（硬件路径）：USB 连接收机后执行 `mode fms` 切到立体声模式，调到一个本地 FM 立体声电台，然后反复执行 `stat`。
3. **观察现象**：输出末尾三行是 PLL 专用的：`fm stereo: <sdi> <sdq>`、`corr: <corr> <corr_ave> <corr_std>`、`int: <integrator>`。
4. **预期结果**：锁定稳定后 `corr_ave` 应接近某个小数值、`corr_std` 小于 100（见 4.4 的锁定判据）；无导频的电台（单声道台）上 `di` 相关幅度小、数值漂移大。（待本地验证）
5. **无硬件替代**：走读调用链 `i2s_end_callback → fm_demod_stereo → fm_demod0 / stereo_separate / stereo_matrix2`，在纸上标注每一步的缓冲区与长度（480 → 480 → 240 → 240）。

#### 4.1.5 小练习与答案

**练习 1**：`fm_demod_stereo` 里 `stereo_separate(buffer[0], buffer2[0], len/2)` 的 `len/2` 为什么是 240？请从 `i2sconfig` 的缓冲区长度推出。

**答案**：[main.c:278-286](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L278-L286) 中 I2S 缓冲为 `AUDIO_BUFFER_LEN * 2` = 960 个 16 位样本，半满/全满中断各处理一半即 480；480 是交织 IQ 共 240 对，`fm_demod0` 每对产出一个实样本，故后续长度为 240。

**练习 2**：对比 `mod_table` 里 `fm` 和 `fms` 两行，说出它们的异同。

**答案**：采样率都是 192kHz、本振偏移都是 0；唯一区别是解调函数——`fm_demod` 输出单声道（左右声道复制同值），`fm_demod_stereo` 额外做副信道分离与和差矩阵。这就是 u1-l4 讲过的「函数指针热切换」：换模式不改框架，只换一行表项。

### 4.2 fm_adj_filter()：鉴频前的频响校正

#### 4.2.1 概念说明

从天线到鉴频器，信号经过了正交检波器、codec 的模拟前端与片内 mini-DSP 滤波（u2-l2），每级都会带来小幅度的**幅频/相频失真**。对单声道收听这点失真听不出来；但对立体声是致命的：`(L−R)` 副信道在 38kHz 附近，若 I/Q 两路在 19~38kHz 频段的相位失真不对称，解出的副载波相位就与导频不一致，分离度立刻恶化。所以在鉴频**之前**先用一个轻量 FIR 把频响「扳平」。

#### 4.2.2 核心流程

- 每支路一个 3 抽头横向滤波器：当前样本与两个历史样本的加权和。
- 两个 16 位系数打包进一个 32 位字 `k12`，用 `__SMLAD`/`__SMLADX` 各做一次「双 16 位乘加」，两条指令算完一个输出。
- 历史样本 `pre1`/`pre2` 存在 `fm_demod_state` 里跨块延续。
- 结果右移 14 位归一化，`__SSAT` 饱和到 16 位后写回（原地更新）。

#### 4.2.3 源码精读

[dsp.c:751-789](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L751-L789) —— `fm_adj_filter()` 全文。系数 `k12 = 0x5ae1eccd` 打包了两个 q15 系数：高半字 0x5ae1 = 23265（约 +0.710），低半字 0xeccd 按有符号 16 位解读为 −4915（约 −0.150）。被注释掉的 `0x51ecea3d` 是另一组备选系数，说明作者调过参。

```c
uint32_t k12 = 0x5ae1eccd;
...
uint32_t i12 = __PKHBT(x2, x1, 16);
uint32_t i0_ = __PKHBT(zero, x0, 16);
acc_i = __SMLAD(k12, i12, acc_i);      // k12 与 (x2,x1) 两个半字的乘加
acc_i = __SMLADX(k12, i0_, acc_i);     // 半字交叉配对的乘加，覆盖 (x0,0)
...
acc_i = __SSAT(acc_i >>14, 16);        // 归一化并饱和
```

I 支路用 `__PKHBT` 打包、Q 支路用 `__PKHTB` 打包（后者取的是高半字），两者分别对本支路的相邻样本组合做乘加——即 I、Q 两路**各自独立**地滤波。输出 `*s++ = __PKHBT(acc_i, acc_q, 16)` 又打包回交织格式，原地覆盖输入。

滤波器状态的收尾：

```c
fm_demod_state.pre1 = x1;
fm_demod_state.pre2 = x2;
```

块与块之间样本流不能断，所以最后两个样本必须存起来留给下一块当历史。

#### 4.2.4 代码实践

1. **实践目标**：把 SIMD 滤波器「翻译」成普通 C，并画出它的频率响应。
2. **操作步骤**：
   - 查阅 CMSIS 文档确认 `__SMLAD(a,b,c) = c + a.lo×b.lo + a.hi×b.hi`、`__SMLADX(a,b,c) = c + a.lo×b.hi + a.hi×b.lo`（有符号半字乘）。
   - 按 `__PKHBT`/`__PKHTB` 的半字打包关系，把 `fm_adj_filter` 的 I 支路展开成 `y[n] = k_a·x[n] + k_b·x[n-1] + k_c·x[n-2]` 形式的普通 C（示例代码，非项目原有）。
   - 在 Python 中用 `numpy.fft` 或直接算 \( H(e^{j\omega}) = k_a + k_b e^{-j\omega} + k_c e^{-j\omega\cdot 2} \) 画幅频/相频曲线。
3. **观察现象**：三个实系数 FIR 的频响在 0~96kHz（192kHz 采样）上的增益与相位曲线。
4. **预期结果**：低频段接近某一平坦增益，接近 19~38kHz 频段有明显的相位/幅度修正量；把结果与设计者意图（改善 38kHz 附近的对称性）对照。（待本地验证——展开式的半字配对需以你对 PKHBT/PKHTB 语义的查证为准）
5. 若无把握，可先只算系数值：`0x5ae1 = 23265`、`(int16_t)0xeccd = -4915`，换算成 q15 即 +0.7099 与 −0.1499。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `pre1`/`pre2` 要放进 `fm_demod_state` 而不是函数内 static 变量？

**答案**：功能上等价，但集中在一个结构体里便于 `stat` 命令统一观测、便于理解「跨块状态都住在这里」这一约定；也和 `last`（鉴频历史）形成一组，说明整条 FM 链的连续性依赖。

**练习 2**：把 `acc_i >>14` 改成 `>>15` 会发生什么？

**答案**：增益减半（−6dB），信号变小但不会溢出；反之改小移位会放大信号，配合 `__SSAT` 会产生削顶失真。`>>14` 说明三抽头加权和的满度会超出 q15 一档，需要多右移一位防溢出。

### 4.3 stereo_separate()（上）：32 位 NCO、倍频与导频鉴相

#### 4.3.1 概念说明

`stereo_separate()` 一个函数同时干三件事：

1. 驱动一个 19kHz 的 **32 位**相位累加器（即 PLL 里的受控振荡器）；
2. 用恒等式 \( \sin 2t = 2\sin t\cos t \）从 19kHz 正余弦**倍频**出 38kHz 载波，把 `(L−R)` 副信道搬回基带；
3. 把输入信号与 19kHz 正余弦做**相关**，得到相位误差 `corr`（鉴相器）。

为什么不用 u3-l1 的 16 位 `PHASESTEP` 宏？看数字：16 位 NCO 在 fs=192kHz 下步进为 `19000×65536/192000 ≈ 6485.33`，只能取整数 6485，对应实际频率 18998.5Hz——差 1.5Hz。导频相位每秒会滑走 1.5 圈，根本锁不住；而且 PLL 稳态需要 0.001Hz 量级的微调分辨率。32 位累加器的频率分辨率是 \( 192000/2^{32} \approx 4.47\times10^{-5} \) Hz，绰绰有余。

#### 4.3.2 核心流程

```text
每块 (240 样本)：
  for 每个样本 x[i]:
      phase_accum += phase_step          # 32 位累加（高 16 位送查表 NCO）
      c, s ← cos_sin(phase_accum >> 16)  # 19kHz 本地正交载波
      ss = (c * s) >> 14                 # sin(2t)：38kHz 载波
      dest[i] = (ss * x[i]) >> 15        # 副信道下变频到 0Hz
      di += (c * x[i]) >> 16             # 与 cos 相关
      dq += (s * x[i]) >> 16             # 与 sin 相关
  块末：di,dq 平滑 → 鉴相 corr = 1024·dq/di（4.4 详述）
```

#### 4.3.3 源码精读

[dsp.c:593-607](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L593-L607) —— 32 位步进常量与初始化。`PHASESTEP_NCO19KHz = (19.0×65536×65536)/192.0 ≈ 425022805`，即「每样本前进 19kHz/192kHz 圈」的 32 位定点表示。`stereo_separate_init()` 把相位、步进、相关积累全部清零，由 `dsp_init()`（[dsp.c:894-898](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L894-L898)）在 [main.c:1015](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L1015) 于 I2S 启动后调用一次。

```c
#define IF_RATE 192.0
#define PHASESTEP_NCO19KHz 	((19.0*65536.0*65536.0)/IF_RATE)
```

[dsp.c:612-641](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L612-L641) —— 主循环。核心四行：

```c
uint32_t cs = cos_sin(phase_accum >> 16);   // 复用 256 点表 NCO（u3-l1）
int16_t s = cs & 0xffff;                    // 取低半字
int16_t c = cs >> 16;                       // 取高半字

// sin(2t) = 2sin(t)cos(t)
int16_t ss = (int32_t)(c * s) >> (16 - 2);  // q15 乘法 >>15 后再 ×2 → >>14

dest[i] = (ss * x) >> (16 - 1);             // 38kHz 下变频
di += (c * x) >> 16;                        // 导频相关 I
dq += (s * x) >> 16;                        // 导频相关 Q
phase_accum += phase_step;                  // 反馈回来的步进在这里起作用
```

倍频那一行值得细品：`c*s` 是两个 q15 相乘，`>>15` 得 `0.5·sin(2t)`（q15），`>>14` 比 `>>15` 少右移一位即乘 2，恰好抵消恒等式里的系数 2，得到 `sin(2t)`。**用同一个 `phase_accum` 倍频**，保证 38kHz 载波与 19kHz 本地振荡永远同频同相——这正是「先锁 19kHz 再倍频」方案的全部好处：PLL 只需对付一个频率。

注意 `di`/`dq` 的相关是「乘完逐样本累加」——240 个样本的和即是 2.3 节公式里的 \( \sum_n \)。

`phase_step` 不再是常量而是从状态里读出、由反馈动态修改的变量（4.4 展开），这是它与 u3-l1 那个「编译期定死」的 NCO 的本质区别。

#### 4.3.4 代码实践

1. **实践目标**：用数字验证「16 位 NCO 锁不住 19kHz 导频」。
2. **操作步骤**：在 PC 上（任何语言）计算：`step16 = 19000*65536/192000`，分别对 `floor(step16)` 和 `round(step16)` 求 `f = step×192000/65536`，再算与 19000Hz 的偏差及「每秒相位滑动圈数」。
3. **观察现象**：取整误差带来的频差。
4. **预期结果**：`floor = 6485 → 18998.54Hz`，差约 −1.46Hz，即每秒相位滑 1.46 圈；`round = 6485`（6485.33 四舍五入仍为 6485）结论相同。所以必须用 32 位：`PHASESTEP_NCO19KHz` 的相对量化误差只有 \( 1/425022805 \)。
5. 此计算纯算术，可直接手算验证。

#### 4.3.5 小练习与答案

**练习 1**：`dest[i] = (ss * x) >> 15` 把实信号乘 38kHz 正弦，频谱上会发生什么？镜像分量去哪了？

**答案**：实混频把 \( X(f) \) 同时搬到 \( f\pm38000 \) 两处：`L−R` 副信道（38k 附近）落到 0Hz 附近为所需项；同时 0~15kHz 的 `L+R` 主信道被搬到 76kHz 附近成为超声残留。本实现后续没有低通把它滤掉（见 4.5 的观察点），靠 192kHz 高采样率把它推到可听频带之外。

**练习 2**：如果导频幅度翻倍，`di`、`dq` 会怎样变化？`corr` 呢？

**答案**：`di`、`dq` 同比例翻倍（相关幅度 ∝ 导频幅度）；但 `corr = 1024·dq/di` 是**比值**，分子分母同乘一个幅度因子，`corr` 不变。这正是用除法而不是直接用 `dq` 当误差的原因——鉴相增益与导频电平无关。

### 4.4 stereo_separate()（下）：两级环路滤波与锁定检测

#### 4.4.1 概念说明

鉴相器输出 `corr` 之后，剩余的问题是：**相位误差应该以多大力度、按什么规律换算成频率修正？** 这就是环路滤波器的职责。CentSDR 用了最经典的 **PI 控制器**（比例 + 积分）：

- **比例项**：`− corr×128`。误差大就猛拉，响应快，但纯比例环会留下与频差成正比的稳态相差。
- **积分项**：`− integrator`。误差的缓慢积累，专门消掉剩余频差，把稳态相差拉到零。

另外还有一个工程细节：**锁定检测**。用 `corr_std`（误差的滑动方差）判断环路是否已经安静下来，只有锁定后才让积分器工作，避免未锁定阶段的剧烈摆动被积分进去。

#### 4.4.2 核心流程

```text
块末（每 1.25ms 执行一次）：
  ① di,dq 一阶平滑：sdi = (sdi×15 + di)/16     # 时间常数 16 块 = 20ms
  ② 鉴相：di>0 时 corr = 1024×dq/di，限幅 ±4095
          di≤0 时（相差≥90°）corr 饱和到 ±4095 全力牵引
  ③ 监测量：corr_ave = (corr_ave×15+corr)/16
            corr_std = (corr_std×15+(corr_ave−corr)²)/16   # 方差
  ④ 环路方程：phase_step = default − integrator − corr×128
  ⑤ 锁定时（corr_std<100）：integrator += corr_ave
```

把单位换算成 Hz 更直观（1 LSB 步进 = \( 192000/2^{32} \approx 4.47\times10^{-5} \) Hz）：

| 反馈量 | 表达式 | 频率当量 |
|---|---|---|
| 比例项最大值 | 4095×128 = 524160 步进 | ±23.4 Hz |
| 积分项满量程 | int16 即 ±32767 步进 | ±1.46 Hz |
| 导频偏 50Hz 所需修正 | 50/192000×2³² ≈ 1118481 步进 | 超出上两项之和 |

最后一行预告了综合实践里要观察的现象：50Hz 频偏已超出这个环路的捕获范围。

#### 4.4.3 源码精读

[dsp.c:644-662](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L644-L662) —— 相关平滑与鉴相：

```c
// averaging correlation
di = (stereo_separate_state.sdi * 15 + di) / 16;
dq = (stereo_separate_state.sdq * 15 + dq) / 16;
stereo_separate_state.sdi = di;
stereo_separate_state.sdq = dq;
if (di > 0) {
    corr = 1024 * dq / di;          # tan(φ) 的 Q10 定点
    if (corr > 4095)      corr = 4095;
    else if (corr < -4095) corr = -4095;
} else {
    if (dq > 0)      corr = 4095;   # 相差接近 ±90°，全力牵引
    else if (dq < 0) corr = -4095;
}
```

`di > 0` 的含义：相关向量横坐标为正，即 \( |\varphi| < 90^\circ \)，此时 \( dq/di = \tan\varphi \approx \varphi \)（小角度下），且除法天然做了幅度归一化。`di ≤ 0` 说明相差已经接近 ±90°（远未锁定），鉴相器输出直接打到限幅值，用最大力度把频率拉回来。

[dsp.c:664-682](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L664-L682) —— 监测量与环路方程，整个 PLL 的心脏：

```c
if (corr != 0) {
    stereo_separate_state.corr = corr;
    stereo_separate_state.corr_ave = (stereo_separate_state.corr_ave * 15 + corr) / 16;

    int32_t d = stereo_separate_state.corr_ave - corr;
    int32_t sd = (stereo_separate_state.corr_std * 15 + d * d) / 16;
    if (sd > 32767) sd = 32767;
    stereo_separate_state.corr_std = sd;

    // feedback phase step
    phase_step = stereo_separate_state.phase_step_default
      - stereo_separate_state.integrator - corr * 128;
    stereo_separate_state.phase_step = phase_step;
    if (stereo_separate_state.corr_std < 100)
      stereo_separate_state.integrator += stereo_separate_state.corr_ave;
}
```

三条线各司其职：

- `corr_ave` 是误差的低通平均值（用于观测与积分输入）。
- `corr_std` 是 \( (corr\_ave - corr)^2 \) 的滑动平均，即**误差方差**——环路锁定后误差只在噪声意义上抖动，方差迅速下降。
- 环路方程 `phase_step = default − integrator − corr*128`：`default` 是理想 19kHz 步进，两项修正叠上去。当频差为零、相差为零时 `corr→0`、`integrator` 停止增长，`phase_step` 回到 `default`，环路达到平衡。

状态结构体的完整定义（`integrator` 是 16 位，其余 32 位）：

[nanosdr.h:148-162](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L148-L162) —— `stereo_separate_state_t`，注意注释「average of correlation vector angle」点明了 sdi/sdq 的语义。

#### 4.4.4 代码实践（本讲主实践）

按规格在 PC 上运行提取出的 `stereo_separate()`，观察 PLL 收敛与失锁。下面是完整可编译的示例代码（**示例代码，非项目原有**；`stereo_separate` 主体逐行照抄 dsp.c，仅用标准 cos/sin 实现 `cos_sin` 桩、并加上记录语句）：

```c
// stereo_pll_sim.c —— 示例代码
// gcc -O2 -o stereo_pll_sim stereo_pll_sim.c -lm && ./stereo_pll_sim > log.txt
#include <stdio.h>
#include <stdint.h>
#include <math.h>

typedef struct {
    uint32_t phase_step_default, phase_step, phase_accum;
    int32_t sdi, sdq, corr, corr_ave, corr_std;
    int16_t integrator;
} stereo_separate_state_t;

static stereo_separate_state_t st;
#define IF_RATE 192.0
#define PHASESTEP_NCO19KHz ((19.0*65536.0*65536.0)/IF_RATE)
#define BLK 240

// 桩：用理想 cos/sin 替代 256 点查表（等效于一个固定相位偏置，
// PLL 会把它吸收为静态相差，不影响收敛动力学）
static inline uint32_t cos_sin(uint16_t phase) {
    double t = 2.0*M_PI*phase/65536.0;
    int32_t c = (int32_t)(32767.0*cos(t));
    int32_t s = (int32_t)(32767.0*sin(t));
    return ((uint32_t)(uint16_t)c << 16) | (uint32_t)(uint16_t)s;
}

static void stereo_separate(int16_t *src, int16_t *dest, int32_t length)
{
    int i, corr = 0;
    int32_t di = 0, dq = 0;
    uint32_t pa = st.phase_accum, ps = st.phase_step;
    for (i = 0; i < length; i++) {
        uint32_t cs = cos_sin(pa >> 16);
        int16_t s = cs & 0xffff, c = cs >> 16;
        int16_t ss = (int32_t)(c * s) >> (16 - 2);
        int32_t x = src[i];
        dest[i] = (ss * x) >> (16 - 1);
        di += (c * x) >> 16;
        dq += (s * x) >> 16;
        pa += ps;
    }
    st.phase_accum = pa;
    di = (st.sdi * 15 + di) / 16;  st.sdi = di;
    dq = (st.sdq * 15 + dq) / 16;  st.sdq = dq;
    if (di > 0) {
        corr = 1024 * dq / di;
        if (corr > 4095) corr = 4095; else if (corr < -4095) corr = -4095;
    } else {
        if (dq > 0) corr = 4095; else if (dq < 0) corr = -4095;
    }
    if (corr != 0) {
        st.corr = corr;
        st.corr_ave = (st.corr_ave * 15 + corr) / 16;
        int32_t d = st.corr_ave - corr;
        int32_t sd = (st.corr_std * 15 + d * d) / 16;
        if (sd > 32767) sd = 32767;
        st.corr_std = sd;
        st.phase_step = st.phase_step_default - st.integrator - corr * 128;
        if (st.corr_std < 100)
            st.integrator += st.corr_ave;
    }
}

int main(int argc, char **argv)
{
    double pilot = 19000.0;
    if (argc > 1) pilot = atof(argv[1]);      // 允许 ./stereo_pll_sim 19050
    st.phase_step_default = PHASESTEP_NCO19KHz;
    st.phase_step = st.phase_step_default;
    static int16_t in[BLK], sub[BLK];
    for (int blk = 0; blk < 4000; blk++) {    // 4000 块 = 5 秒
        for (int i = 0; i < BLK; i++) {
            double t = (blk*BLK + i) / 192000.0;
            double m = 0.4*sin(2*M_PI*300.0*t);                  // 音频
            double sg = m*sin(2*M_PI*38000.0*t);                 // (L-R) DSB-SC
            double pl = 0.1*sin(2*M_PI*pilot*t);                 // 导频
            in[i] = (int16_t)(32767.0*(0.5*m + sg + pl));        // 复合基带
        }
        stereo_separate(in, sub, BLK);
        printf("%d %u %d %d %d %d\n", blk,
               st.phase_step, st.corr, st.corr_ave, st.corr_std,
               st.integrator);
    }
    return 0;
}
```

1. **实践目标**：亲眼看一个软件 PLL 从冷启动收敛到锁定，再观察超出捕获范围时的失锁。
2. **操作步骤**：编译运行 `./stereo_pll_sim`；用 Python 读 `log.txt` 画 `phase_step`、`corr_ave`、`corr_std`、`integrator` 随块号（时间）的曲线；再运行 `./stereo_pll_sim 19050` 把导频偏移 50Hz 重复。
3. **观察现象**：19kHz 时 `phase_step` 应从默认值出发、经比例项快速拉动后趋稳，`corr_std` 降到 100 以下后 `integrator` 开始缓慢走动；19050Hz 时对比各量的行为。
4. **预期结果**：19kHz 下约数百块内收敛（一阶平滑时间常数为 16 块 = 20ms，整体收敛秒级）；19050Hz 时按 4.4.2 的换算表，需要的步进修正 1118481 超出比例项+积分项可达的约 556927（524160+32767），预期 `corr` 长期贴在 ±4095、`corr_std` 居高不下、`integrator` 在 int16 范围内饱和甚至回绕，PLL 无法锁定。（待本地验证——这正是本实践要确认的结论）
5. 若观察到 19050Hz 下短暂「假锁定」（`corr_std` 偶尔低于 100），思考原因：鉴相器 19050Hz 误差信号相对 800 块/s 的更新率仍是慢变量，短窗口内可能显得安静。

#### 4.4.5 小练习与答案

**练习 1**：把 `corr * 128` 改成 `corr * 8`，环路的捕获范围和跟踪速度分别怎么变？

**答案**：比例增益缩小 16 倍：捕获范围从 ±23.4Hz 缩到约 ±1.5Hz，同样的频差下稳态相差增大 16 倍（校正同样的步进偏差需要 16 倍的 `corr`），跟踪变慢、抗噪更平滑。反之增大增益扩范围但更抖、甚至失稳抖振——经典的 P 增益取舍。

**练习 2**：`integrator` 声明为 `int16_t`（nanosdr.h:160），它能补偿的最大频偏是多少？这个设计对 50Hz 频偏意味着什么？

**答案**：±32767 步进 ≈ ±1.46Hz。积分项只负责消除**小的**剩余频偏（如采样率误差、发端频偏几 Hz）；50Hz 频偏主要得靠比例项（上限 ±23.4Hz），超限即失锁——与主实践的预期一致。

**练习 3**：为什么积分更新用的是 `corr_ave` 而不是 `corr`？

**答案**：`corr` 是瞬时鉴相输出、噪声大；`corr_ave` 是 16 块平滑值。用平滑值积分可避免噪声被积分器长期累积（随机游走），提高稳态频率精度。

### 4.5 stereo_matrix2()：和差矩阵分离左右声道

#### 4.5.1 概念说明

经过 4.3/4.4，手里有两个实数序列：

- `buffer[0]`（s1）：鉴频输出，含 `L+R`（0~15kHz）+ 导频 + 残留超声镜像；
- `buffer2[0]`（s2）：搬回 0Hz 的 `L−R` 副信道。

和差制的收端公式就是一行加减法：

\[ L = \frac{(L+R) + (L-R)}{2}, \qquad R = \frac{(L+R) - (L-R)}{2} \]

本实现不做除 2，直接 `l = s1+s2 = 2L`、`r = s1−s2 = 2R`，整体增益 +6dB（FM 鉴频输出本就偏小，顺带补偿动态范围）。相加可能溢出 16 位，所以用**饱和加减指令** `__QADD16`/`__QSUB16`——溢出时钳在 ±32767/−32768 而不是回绕，听感上是瞬间轻微削顶而非爆裂噪声。

#### 4.5.2 核心流程

```text
for i in 0..len:
    x1 = s1[i]        # L+R 支路
    x2 = s2[i]        # L−R 支路
    l  = __QADD16(x1, x2)   # 饱和加 → 2L
    r  = __QSUB16(x1, x2)   # 饱和减 → 2R
    *dst++ = l; *dst++ = r  # 交织写入 tx_buffer（左右交替）
```

#### 4.5.3 源码精读

[dsp.c:699-711](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L699-L711) —— `stereo_matrix2()`，从两个连续实数序列生成交织双声道输出：

```c
void
stereo_matrix2(int16_t *s1, int16_t *s2, int16_t *dst, int len)
{
	int i;
	for (i = 0; i < len; i++) {
		uint32_t x1 = *s1++;
		uint32_t x2 = *s2++;
		uint32_t l = __QADD16(x1, x2);
		uint32_t r = __QSUB16(x1, x2);
		*dst++ = l;
		*dst++ = r;
	}
}
```

同族还有两个变体，对照阅读能看清作者的演进思路：

- [dsp.c:686-697](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L686-L697) `stereo_matrix()`：原地把 s1/s2 覆写为 l/r，不交织——用于两个独立单声道缓冲的场景。
- [dsp.c:713-749](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L713-L749) `stereo_matrix3()`：**未被调用**（[dsp.c:818](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L818) 已注释）。它每 4 个样本一组：先 `/4` 再累加 4 次，等于边求平均边和差——即 **4:1 抽取**，把 192kHz 输出降到 48kHz。被弃用的原因可推测：简单平均的抗混叠性能不足，且 192kHz 直播更简单。

一个诚实的观察点：[dsp.c:635](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L635) 里 `//src[i] = src[i] / 2;` 被注释掉了——曾用「主信道减半」来给矩阵求和腾动态范围，现在改由饱和指令兜底。同时 s1 支路**没有**低通滤除 19kHz 导频与 76kHz 镜像，这些超声分量会一路进到耳机，靠其超出可听频带而无感。这是一个「先跑通、再精化」的教学级取舍，也是留给二次开发的改进点（见第 7 节）。

#### 4.5.4 代码实践

1. **实践目标**：验证和差矩阵的增益与饱和行为。
2. **操作步骤**：在 4.4 的 `stereo_pll_sim.c` 里追加（示例代码）：

   ```c
   static void stereo_matrix2(int16_t *s1, int16_t *s2, int16_t *dst, int len) {
       for (int i = 0; i < len; i++) {
           int32_t a = s1[i], b = s2[i];
           int32_t l = a + b; if (l > 32767) l = 32767; if (l < -32768) l = -32768;
           int32_t r = a - b; if (r > 32767) r = 32767; if (r < -32768) r = -32768;
           dst[i*2] = l; dst[i*2+1] = r;
       }
   }
   ```

   在主循环里对每块调用 `stereo_matrix2(in, sub, out, BLK)`，并人工构造 `in[i]=20000`、`sub[i]=20000` 的极端输入单测一次。
3. **观察现象**：正常信号下 `out` 的左右声道幅度约是 `in` 的两倍；极端输入下 `l` 钳在 32767 而非回绕成负数。
4. **预期结果**：与 4.4 实践合起来：19kHz 导频锁定后，`out` 左/右声道应分别恢复出本实践信号源里的 L 与 R（本信号源取 R=0、L=m，即左声道有 300Hz 音频、右声道近似静音）。（待本地验证）
5. 注意输出采样率仍是 192kHz——本固件没有降采样级。

#### 4.5.5 小练习与答案

**练习 1**：若把 `__QSUB16` 换成普通减法，什么时候会出问题？

**答案**：当 `s1≈−32768` 且 `s2` 为正时，差值低于 −32768，普通 16 位减法回绕成大正数，波形瞬间从负满度跳到正满度，产生强烈爆音；饱和减法则钳在 −32768，只是轻微削波。

**练习 2**：发端公式里 `(L+R)` 与 `(L−R)` 通常等幅，为什么收端 `s2`（搬移后的 L−R）幅度往往明显小于 `s1`，导致分离度下降？本固件在鉴频前做了什么来缓解？

**答案**：副信道占据 23~53kHz 高频段，接收链路（正交检波器滚降、codec 滤波器）在该频段的幅度/相位失真比 15kHz 以内的主信道大，DSB-SC 解调对载波相位误差又极敏感。缓解手段就是 4.2 的 `fm_adj_filter()` 频响校正——先扳平坦再鉴频。

## 5. 综合实践

**任务：在 PC 上搭建完整的 FM 立体声解调仿真链，端到端验证左右声道恢复。**

把 4.4 与 4.5 的代码合并，并在最前面补一个「理想 FM 发射机 + 理想鉴频器」：

1. **信号源**：取 L 为 300Hz、R 为 3kHz 的两个不同音频（幅度 0.3），按 2.1 的复合公式叠加（导频 0.1），再对 76MHz 载波调频（频偏 75kHz）生成复基带 IQ：\( e^{j\phi(t)} \)，其中 \( \phi(t)=2\pi k_f\int s(t)dt \)。
2. **鉴频**：用 `atan2(q[n], i[n]) − atan2(q[n-1], i[n-1])` 替代 `atan_2iq`/`fm_adj_filter`（PC 仿真中链路无失真，可跳过校正）。
3. **分离**：接 4.4 提取的 `stereo_separate()` 与 4.5 的 `stereo_matrix2()`。
4. **验收标准**：
   - PLL 在 2 秒内锁定（`corr_std` 持续低于 100）；
   - 输出左声道 FFT 在 300Hz 有峰、右声道在 3kHz 有峰，交叉分量（左声道里的 3kHz）比主峰低 20dB 以上；
   - 把导频频率改为 19010Hz（+10Hz，仍在捕获范围内）重复，验证 `integrator` 随时间累积、`phase_step` 收敛到 `default − integrator − corr_ave×128 ≈ 19010Hz 对应步进`。
5. **记录**：把三组（0Hz / +10Hz / +50Hz 偏移）的 `phase_step` 收敛曲线和左右声道频谱图保存下来，对照 4.4.2 的换算表解释差异。（整个任务在 PC 上完成，结果待本地验证）

## 6. 本讲小结

- FM 立体声采用和差制：`(L+R)` 在基带保证单声道兼容，`(L−R)` 调制在抑制载波的 38kHz 副载波上，19kHz 导频专为接收机恢复 38kHz 而设。
- `fm_demod_stereo()` 是五步流水线：频响校正 → 鉴频 → 导频 PLL 分离副信道 → 和差矩阵 → 双声道输出，全部在 1.25ms 一次的 I2S 中断回调里完成。
- 导频鉴相的诀窍是**相关**：块内与本地正交载波相关得到向量 \( (d_i, d_q) \)，辐角即相位误差，`1024·dq/di` 的除法使鉴相增益与导频电平无关。
- 环路滤波是 PI 结构：`corr×128` 比例项快速捕获（上限 ±23.4Hz），`integrator` 积分项消除稳态频差（上限 ±1.46Hz），`corr_std<100` 的方差判据作为锁定检测门控积分。
- 38kHz 载波用 \( \sin 2t = 2\sin t\cos t \) 从同一个 32 位相位累加器倍频而来，天然与导频同频同相；32 位步进提供 \( 4.5\times10^{-5} \) Hz 的微调分辨率。
- 和差矩阵只是饱和加减（`__QADD16`/`__QSUB16`）；主信道的导频与超声镜像未滤除，靠 192kHz 采样率推出可听频带——一个明确的可改进点。

## 7. 下一步学习建议

- **u4-l1（频谱显示）**：本讲多次出现的 `disp_fetch_samples()` 挂钩点（B_CAPTURE/B_IF1/B_IF2/B_PLAYBACK）将在下一单元展开——你会看到如何在 `fms` 模式下抓取鉴频前后的实信号观察导频谱线。
- **u5-l1（并发与实时）**：`fms` 是六种模式中 DSP 负载最高的，用 `stat` 的 `load` 指标量化它与 CW 模式的差距，理解 1.25ms 回调窗口的实时约束。
- **改进实验（承接二次开发）**：给 s1 支路加一个 15kHz 低通（可仿照 u3-l2 用 `arm_biquad_cascade_df1_q15` 与椭圆滤波器 notebook 设计系数），滤除导频与镜像后主观评价音质与分离度变化；或把未启用的 `stereo_matrix3()` 4:1 抽取改为真正的 192k→48k 抗混叠重采样。
- **延伸阅读**：关于导频 PLL 的带宽选择与立体声分离度指标，可对照广播工程资料中「pilot rejection / SCA rejection」的常规指标来评估本实现。
