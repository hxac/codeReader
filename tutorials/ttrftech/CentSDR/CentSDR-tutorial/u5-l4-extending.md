# 二次开发实战：为 CentSDR 新增一种解调模式

## 1. 本讲目标

前四个单元我们把 CentSDR 拆开看了一遍：构建、外设、DSP、显示、UI、持久化。本讲把这些知识重新组装起来，回答一个工程问题：

> **我想给这台接收机加一种新的解调模式（比如带宽 600Hz 的窄带 CW 变体），到底要改哪里？为什么是这几处？**

学完本讲，你应该能够：

1. 准确指出固件的四个「插口」：`signal_process` 函数指针、`mod_table` 表、shell `commands` 表、`modulation_t` 枚举（外加 Makefile 的 `CSRC`），并说出每个插口各自的职责。
2. 独立完成新增一种解调模式所需的 `nanosdr.h` / `dsp.c` / `main.c` / `ui.c`（牵连 `icons.c`）的端到端改动。
3. 识别新增枚举值带来的三类隐蔽破坏：图标槽位偏移、Flash 配置兼容、shell 前缀匹配顺序。
4. 说明新源文件如何进入构建系统，以及 `mod_table` 的注册如何让解调函数在链接期「存活」。

本讲的实战载体是 **CW-600 窄带变体**（记作 `cwn`）：把 CW 的 150Hz 椭圆低通换成 600Hz，其余链路完全复用 `demod_weaver`。它改动量小，却被迫经过每一个扩展点，是理想的「打孔样板」。

## 2. 前置知识

本讲默认你已读过依赖讲义（u3-l2 Weaver 解调、u1-l4 shell 与 Python 工具、u4-l4 UI 状态机）。这里只补三个本讲要用到的通用概念：

- **函数指针分发（dispatch）**：C 语言里实现「运行时替换算法」的经典手法。把函数地址存进一个变量/表，调用点只写 `(*fp)(...)`，换算法就是换指针，调用点一行不动。Linux 内核的文件操作表、GUI 框架的虚函数表都是这个思路。
- **表驱动设计（table-driven）**：把「一组并列的配置」从 if-else 链改写成数组，每个元素聚合该变体的全部参数。新增变体 = 加一行表项，而不是复制一段逻辑。CentSDR 同时用了两种：`mod_table` 是「数据表」，`signal_process` 是「单变量指针」。
- **枚举即下标（enum as index）**：`modulation_t` 的枚举值不仅是个名字，它还是 `mod_table[]` 的数组下标、`icons48x20[]` 的图标下标、Flash 里存储的整数。**这个小小的整数同时是三种数据的「主键」**——这是本讲反复出现的坑源。

另外回忆两个事实（前面讲义已论证）：

- 解调函数运行在 **I2S 中断上下文**，每 5ms（48kHz 时）必须算完，不能阻塞、不能睡眠；
- 滤波器系数由 python/ 目录的 notebook 用 `scipy.signal.ellip(6, 1, 60, fc/24000)` 设计后定点化而来（u3-l2、u5-l5）。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注的要点 |
|---|---|---|
| `nanosdr.h` | 全局共享头 | `signal_process_func_t` 类型、`modulation_t` 枚举、demod 函数声明、`ICON_AGC_OFF` |
| `dsp.c` | 解调算法库 | `demod_weaver` 及其配置结构、三组滤波器系数表、`cw_demod` 的栈上配置模式 |
| `main.c` | 固件骨架 | `mod_table`、`set_modulation()`、`i2s_end_callback` 的分发点、`cmd_mode`、`commands` 表 |
| `ui.c` | 旋钮状态机 | MOD 档的旋钮处理、`ui_init`/`recall_channel` 对 `set_modulation` 的调用 |
| `display.c` / `icons.c` | 屏幕（牵连文件） | `uistat.modulation` 直接当图标下标用 |
| `Makefile` | 构建系统 | `CSRC` 源文件清单、`DSPLIBSRC`、`USE_LINK_GC` |
| `python/CW-Filter-Design.ipynb` | 算法设计工具 | 椭圆滤波器设计参数的原始出处 |

## 4. 核心概念与源码讲解

### 4.1 分发核心：`signal_process` 函数指针

#### 4.1.1 概念说明

整个固件有 27 条 shell 命令、两个工作线程、若干中断，但**全固件只有一个地方真正调用解调算法**——I2S DMA 回调。它不知道也不关心当前是什么模式，只认一个函数指针。这个指针就是第一个、也是最重要的扩展点：

- **算法作者**只管写一个符合签名的函数；
- **接线者**（`set_modulation`）负责把它的地址写进指针；
- **消费者**（中断回调）永远只写一行调用。

这带来一个关键性质：**新增算法对实时路径零侵入**。热路径代码不随模式数量增长，1 种模式和 100 种模式的回调代码完全一样。

#### 4.1.2 核心流程

```text
I2S DMA 半满/全满中断
  └─ i2s_end_callback(p, q, n)          [main.c:258]
       └─ (*signal_process)(p, q, n)    [main.c:267]  ← 唯一调用点
            └─ 当前指向的 demod 函数（dsp.c）

切换模式（shell 或旋钮触发）
  └─ set_modulation(mod)                [main.c:179]
       ├─ uistat.fs = mod_table[mod].fs
       ├─ set_fs(...)                   ← 必要时切换采样率
       ├─ signal_process = mod_table[mod].demod_func   ← 换指针，热切换完成
       ├─ mode_freq_offset / mode_freqoffset_phasestep ← 刷新调谐补偿
       └─ disp_update()                 ← 通知屏幕重画
```

下一次中断到来时，新函数就被调用——**模式切换不需要停 I2S、不需要重启任何东西**（除非采样率变了）。

#### 4.1.3 源码精读

指针类型与声明在共享头文件里，任何新 demod 函数都必须匹配这个签名（`int16_t*` 源/目的 + `size_t` 长度）：[nanosdr.h:L112-L114](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L112-L114) 定义了 `signal_process_func_t` 函数指针类型并 extern 声明了全局指针变量。同文件 [nanosdr.h:L116-L121](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L116-L121) 逐个声明了六个现有 demod 函数——**你的新函数也要在这里加一行声明**。

指针本体和初值在 [main.c:L113-L117](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L113-L117)：`signal_process` 初始化为 `am_demod`（所以刚开机、还没走到 `ui_init` 时跑的是 AM），旁边还住着 `mode_freq_offset` 等模式相关全局量。

唯一调用点在 [main.c:L258-L276](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L258-L276) 的 `i2s_end_callback`：[main.c:L267](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L267) 这一行 `(*signal_process)(p, q, n)` 就是全固件的 DSP 心跳（u2-l3、u5-l1 讲过它前后还夹着 DWT 负载测量）。

#### 4.1.4 代码实践

**实践目标**：用静态分析证实「解调函数只被 `mod_table` 引用，注册即存活」。

**操作步骤**：

1. 按 u1-l2 的流程构建出 `build/ch.elf`（无硬件也可，只需编译）。
2. 执行 `arm-none-eabi-nm build/ch.elf | grep demod`，列出所有带 `demod` 的符号及其地址。
3. 对照 `mod_table`（见 4.3.3）的六项，确认六个 demod 函数（`cw/lsb/usb/am/fm_demod`、`fm_demod_stereo`）**全部**有地址。
4. 思考：Makefile 里 `USE_LINK_GC = yes`（[Makefile:L21-L24](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L21-L24)）开了 `--gc-sections`，没被引用的函数会被链接器删除——为什么这六个函数没被删？

**需要观察的现象**：六个符号全部存在（`t` 类型，地址在 Flash 区）。

**预期结果**：因为 `mod_table` 在 `.data`/`.rodata` 里引用了它们的地址，链接器必须保留。**推论**：如果你写了新 demod 函数却忘了注册进 `mod_table`，链接器可能直接把它删掉，`nm` 里找不到——这是「忘了接线」的一个客观检测手段。

**待本地验证**：符号地址的具体数值依构建环境而异。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `signal_process` 的换值发生在 `set_modulation`（线程上下文），而读取发生在中断上下文，却不加锁？

**答案**：指针赋值是对齐字宽（32 位）的单条 store，Cortex-M 上是原子可见的；最坏竞态是「下一次回调还跑旧函数」，仅造成一个 5ms 块的延迟，无数据结构破坏。这是 u5-l1 讲过的「单写者 + 原子宽度」无锁惯例。

**练习 2**：如果把新 demod 函数写成带 `static` 的（只在本文件可见），会发生什么？

**答案**：`static` 函数若只被同文件的 `mod_table` 引用则没问题；但 `mod_table` 在 `main.c`，所以跨文件引用时绝不能加 `static`，且必须在 `nanosdr.h` 加声明，否则 `main.c` 编译报隐式声明错误/链接失败。

**练习 3**：开机后、`ui_init()` 执行前，屏幕还没初始化，此时跑的是什么解调？

**答案**：AM——`signal_process` 的静态初值是 `am_demod`（main.c:L113）。直到 `ui_init()`（main.c:L1042）调用 `set_modulation(uistat.modulation)` 才切换到配置里保存的模式。

### 4.2 算法侧：解调函数的契约与 `demod_weaver` 复用

#### 4.2.1 概念说明

写新解调不是「随便写个函数」，而是履行一份**中断上下文的契约**。从现有代码归纳，契约有六条：

1. **签名**：`void f(int16_t *src, int16_t *dst, size_t len)`（nanosdr.h:L112）。
2. **输入格式**：`src` 是交织 IQ 的 int16 流（`src[0]=I, src[1]=Q, ...`），`len` 是 int16 样本数（480），即 240 个复数样本。
3. **输出格式**：`dst` 是交织的双声道音频，左右声道通常填同一个值。
4. **时限**：必须在回调窗口内算完（48kHz 时 5ms），否则丢样（u5-l1 的 `overflow` 计数器会涨）。
5. **中断纪律**：不许 `chThdSleep*`、不许 `chprintf`、不许拿互斥量；I2C/SPI 等Blocking 调用一律禁止。
6. **搭便车义务**：在四个钩子处调用 `disp_fetch_samples`，让频谱/波形显示能顺路抓样本（u4-l1）。

好消息是：**CW 变体根本不用从零写**。`dsp.c` 里的 Weaver 解调器已经把契约第 2~6 条全部包办，可调参数被抽成了一个小配置结构。

#### 4.2.2 核心流程

`demod_weaver` 的三级流水（u3-l2 详细推导过）：

```text
src（交织 IQ）
  │ ① NCO 混频：相位步进 = -phasestep1，把目标边带中心搬到 0Hz
  ▼
buffer[2]（分离 IQ 平面格式）
  │ ② 椭圆低通：arm_biquad_cascade_df1_q15 × 2（I/Q 各一链）
  ▼
buffer2[2]
  │ ③ 二次 NCO 混频：相位步进 = +phasestep2，取实部搬回音频
  ▼
dst（交织双声道音频）
```

三个自由度构成一个「模式配置」：

- `phasestep1`：一次混频方向与深度（正负号选边带）；
- `phasestep2`：恢复音频的音调（CW 的 800Hz 侧音就是它）；
- `bq_i/bq_q`：滤波器实例（决定带宽）。

相位步进的换算（16 位相位累加器，fs=48kHz）：

\[ \text{phasestep} = \frac{65536 \times f}{f_s} = \texttt{PHASESTEP}(f) \]

**复用策略**：新 CW 变体 = 新滤波器系数表 + 一个三字段配置。核心流水一行不改。

#### 4.2.3 源码精读

配置结构体 [dsp.c:L332-L337](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L332-L337) 定义了 `weaver_demod_conf_t`：两个相位步进 + 两个滤波器实例指针。USB/LSB 各有一份静态配置 [dsp.c:L339-L344](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L339-L344)，唯一区别是步进取正取负——选边带的全部秘密就是一个符号。

解调主体 [dsp.c:L346-L385](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L346-L385) 的 `demod_weaver`：三级流水之间在 [dsp.c:L355](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L355)、[L365](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L365)、[L371](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L371)、[L384](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L384) 四处埋了 `disp_fetch_samples` 显示钩子——新解调若不复用它，就得自己补这四拍，否则频谱显示在你这个模式下抓不到样本。

**最值得模仿的一段**是 CW 的包装函数 [dsp.c:L399-L407](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L399-L407)：`cw_demod` 在**栈上临时拼装**一个配置——一次混频用 `mode_freqoffset_phasestep`（对应 10kHz 中频，由 `set_modulation` 刷新），二次混频用 `cw_tone_phasestep`（对应可运行时调节的侧音，由 `cwtone` 命令/旋钮刷新），滤波器指向 CW 专用实例。你的 `cwn_demod` 几乎就是它的翻版。

滤波器系数表方面，[dsp.c:L322-L330](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L322-L330) 是 CW 的 150Hz 六阶椭圆（60dB）系数与实例。注意实例定义 `bq_cw_i = { 3, bq_i_state, bq_coeffs_150hz, 1}` 里**状态数组复用的是 `bq_i_state`**（[dsp.c:L290-L291](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L290-L291)，AM/SSB/CW 的实例共享同一份状态）——因为同一时刻只有一个模式在跑，这是安全的；新变体照抄即可。CMSIS biquad 的系数布局与定点化约定（`{b0,0,b1,b2,a1,a2}`、a 取反、×16384、postShift=1）在 u3-l2 已详述，此处不重复。

#### 4.2.4 代码实践

**实践目标**：设计出 CW-600 需要的滤波器系数表。

**操作步骤**：

1. 打开 `python/CW-Filter-Design.ipynb`，找到设计语句（notebook 第 142 行附近）：`signal.ellip(6, 1, 60, 150.0/24000, 'low')`——六阶椭圆、通带波纹 1dB、阻带 60dB、截止 150Hz、采样率 48kHz。
2. 把截止频率换成 600Hz，得到新滤波器。以下为**示例代码**（在 notebook 或独立 Python 脚本中运行）：

```python
# 示例代码：设计 600Hz 窄带低通并导出为 dsp.c 用的 q15 表
import numpy as np
from scipy import signal

z, p, k = signal.ellip(6, 1, 60, 600.0/24000, 'low', output='zpk')
sos = signal.zpk2sos(z, p, k)          # 3 个二阶节
# 每节增益归一化到 b0，并按 u3-l2 的约定定点化：
# {b0, 0, b1, b2, -a1, -a2} * 16384，四舍五入取整
for s in sos:
    b0, b1, b2, a1, a2 = s[0]/s[0], s[1]/s[0], s[2]/s[0], -s[3]/s[0], -s[4]/s[0]
    print([round(x*16384) for x in (b0, 0, b1, b2, a1, a2)])
```

3. 用 `signal.sosfreqz` 画新滤波器的幅频响应，确认 600Hz 通带与约 60dB 阻带。
4. 对照旧表自查：新系数的 a 系数（第 5、6 列）绝对值应与 150Hz 版本（`32593, -16210` 量级）接近，因为它们都贴近单位圆；b 系数（前 3 列）量级相近。

**需要观察的现象**：三行输出中，每行第 2 列恒为 0，反馈系数超过 16384（这正是 postShift=1 的原因）。

**预期结果**：得到可直接粘贴进 `dsp.c` 的 `q15_t bq_coeffs_600hz[3][6]`。**增益分配**若直接照抄会导致通带增益超标/不足，应参照 notebook 的做法逐节分配增益（u3-l2 与 u5-l5 讲过），最终以实测听感或 `data` 命令抓取的输出幅度为准。**待本地验证**（需要 Python 环境）。

#### 4.2.5 小练习与答案

**练习 1**：新 demod 函数想用一个 3000Hz 的低通，能直接抄 `PHASESTEP(3000)` 当 `phasestep2` 吗？

**答案**：能，但只在该模式 fs=48kHz 时成立。`PHASESTEP` 宏写死 `FS=48000`（[nanosdr.h:L125-L128](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L125-L128)）。若新模式在 mod_table 里登记 fs=192，编译期算出的步进就错了，必须按实际采样率现算（参考 FM 立体声用 `IF_RATE` 现算 19kHz 步进的做法，dsp.c:L593-L594）。CW-600 登记 fs=48，无此问题。

**练习 2**：为什么 `nco1_phase`/`nco2_phase` 必须是全局变量（dsp.c:L284-L285），而 `cw_demod` 的配置结构可以是栈上局部变量？

**答案**：相位累加器承载**跨音频块的状态**——每 5ms 的回调是同一连续振荡的接力，放栈上每次归零会产生相位跳变（表现为周期性咔哒声与频谱毛刺）；配置结构只是无状态参数快照，用完即弃。

**练习 3**：把新滤波器实例的状态数组换成自建的 `bq_cwn_state[4*3]` 有什么好处？

**答案**：模式切换瞬间，旧模式的残留状态不会串进新滤波器（共享状态时，切换后头几个样本是旧模式尾巴，通常听不出来但存在）。代价是多 24 个 int16 的 RAM。两种做法本项目都有先例，属可接受的风格选择。

### 4.3 注册侧：`mod_table` 表驱动与 `set_modulation` 接线

#### 4.3.1 概念说明

光有函数还不够，`set_modulation` 不知道它的存在。第二个扩展点是 `main.c` 里的 `mod_table`：每个模式一行，聚合该模式的**全部**接线参数——解调函数、调谐频率偏移、采样率、名字。它同时服务于：

- `set_modulation`：取函数指针/采样率/偏移（[main.c:L179-L194](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L179-L194)）；
- `cmd_show` / `cmd_channel list`：取名字打印（[main.c:L733](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L733)、[L781](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L781)）。

**铁律**：表项顺序必须与 `modulation_t` 枚举顺序严格一致——运行时全靠 `mod_table[mod]` 下标访问，没有任何名字匹配的兜底。

#### 4.3.2 核心流程

`set_modulation(mod)` 的接线顺序（[main.c:L179-L194](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L179-L194)）：

1. 越界保护：`mod >= MOD_MAX` 直接返回；
2. 采样率：`uistat.fs` ← 表项 `fs`，调 `set_fs()`（先停编解码器时钟再重启的握手，u2-l3）；
3. **换指针**：`signal_process` ← 表项 `demod_func`；
4. 调谐补偿：`mode_freq_offset` ← 表项 `freq_offset`，并折算 `mode_freqoffset_phasestep = PHASESTEP(mode_freq_offset)`；
5. 刷新 CW 侧音步进（`cw_tone_phasestep`，对所有模式统一执行，无害）；
6. `uistat.modulation = mod` 并 `disp_update()` 刷屏。

其中 `freq_offset` 的下游是 `set_tune`：[main.c:L196-L201](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L196-L201) 里 `center_frequency = hz - mode_freq_offset`——AM/CW 用 10kHz 低中频避开直流黑洞（u3-l3），LSB/USB/FM 用 0。**新模式的 `freq_offset` 决定了载波在频谱上的落点与解调器的搬移量，两者必须配套**。

#### 4.3.3 源码精读

枚举定义在 [nanosdr.h:L240-L248](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L240-L248)：`MOD_CW, MOD_LSB, MOD_USB, MOD_AM, MOD_FM, MOD_FM_STEREO, MOD_MAX`。**新增 `MOD_CWN` 必须插在 `MOD_FM_STEREO` 之后、`MOD_MAX` 之前**（追加到末尾），原因见 4.5。

`mod_table` 本体在 [main.c:L165-L177](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L165-L177)：匿名结构体数组，四字段 `{ demod_func, freq_offset, fs, name }`。注意 CW 行 `{ cw_demod, AM_FREQ_OFFSET, 48, "cw" }`——CW 复用了 AM 的 10kHz 偏移宏，这就是「中频策略」在表里的体现。**你的新行是 `{ cwn_demod, AM_FREQ_OFFSET, 48, "cwn" }`，追加在 fms 行之后。**

`set_modulation` 在 [main.c:L179-L194](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L179-L194)，逐行对应 4.3.2 的六步。值得注意它**没有**重置任何滤波器状态或相位累加器——切换瞬间的过渡噪声被直接容忍了，这是嵌入式实时路径里典型的「能不管就不管」取舍。

#### 4.3.4 代码实践

**实践目标**：在改代码之前，用「纸上推理」验证表项与接线逻辑的正确性。

**操作步骤**：

1. 写出你的表项 `{ cwn_demod, AM_FREQ_OFFSET, 48, "cwn" }`。
2. 预测 `set_modulation(MOD_CWN)` 执行后各变量的值：`uistat.fs`=?（48）`signal_process`=?（`&cwn_demod`）`mode_freq_offset`=?（10000）`mode_freqoffset_phasestep`=?（`65536*10000/48000` = 13653）。
3. 再预测调谐 7.100MHz 时：`set_tune(7100000)` → `center_frequency` = 7090000，SI5351 实际下发 7090000×4 = 28360000（四倍频供正交检波，u2-l1）。
4. 有硬件的话，用 `python/centsdr.py` 发送 `tune 7100000` 后执行 `freq` 类命令或观察频谱，核对载波显示在中心 +10kHz 处（u4-l1 讲过 AM/CW 模式下载波位于中心右侧约 +10kHz）。

**需要观察的现象**：第 2、3 步的预测值与代码推演一致；第 4 步频谱上载波位置与预测一致。

**预期结果**：全部吻合即说明你对 `freq_offset` → `set_tune` → 频谱落点这条链的理解无误。**待本地验证**（第 4 步需硬件）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `MOD_CWN` 插在 `MOD_AM` 的位置（枚举中间），会发生什么？

**答案**：三处灾难：① `MOD_AM` 及之后的枚举值整体 +1，Flash 里已存的旧配置（信道表、uistat.modulation）含义错位——存的是 AM(3) 读出来变 USB；② `mod_table` 若不同步重排，模式与函数错配；③ 图标下标错位（见 4.5）。**追加到末尾是唯一安全做法。**

**练习 2**：`mod_table` 为什么不写成 `static`？

**答案**：它被 `cmd_show`、`cmd_channel` 等同文件函数引用，写成 `static` 其实可行；但现状是非 static 的全局（main.c:L170），若其他文件（如显示）将来要按名字查模式也能访问。这是风格选择，不是硬约束。真正不能省的是与枚举的对齐关系。

**练习 3**：新模式的 `freq_offset` 想用 5000Hz 而不是 10000Hz，只改 `mod_table` 够吗？

**答案**：不够。`set_modulation` 会自动算 `mode_freqoffset_phasestep`（这一半是自动的），但解调函数里一次混频若硬编码了 10kHz 相关常量（CW 用的是 `mode_freqoffset_phasestep` 变量，恰好自动跟随），且 `bq` 滤波器带宽、频谱几何参数表（u4-l1）都以 10kHz 中频为前提画的。结论：改 `freq_offset` 要同时审视解调器与显示两端的假设。

### 4.4 命令侧：shell `mode` 命令与 `commands` 表

#### 4.4.1 概念说明

第三个扩展点让新模式**可达**（reachable）：用户通过 USB 虚拟串口输入 `mode cwn` 触发切换。它分两层：

- **命令注册层**：`commands[]` 表（NULL 哨兵结尾）把命令名映射到 C 函数——只有要加**全新命令**时才动它；
- **命令内部层**：`cmd_mode` 的 if-else 链把模式名前缀映射到 `set_modulation(MOD_xxx)`——加新模式在这里接线。

u1-l4 讲过这套框架（校验参数→调底层→更新 uistat→刷屏），本讲聚焦**接线时的顺序陷阱**。

#### 4.4.2 核心流程

```text
用户输入 "mode cwn"
  └─ ChibiOS shell 框架查 commands[] 表 → cmd_mode(chp, 1, ["cwn"])
       └─ if-else 前缀链逐个 strncmp(cmd, "xx", n)
            ├─ "am"  (n=1)：首字符 'a'？否
            ├─ "lsb" (n=1)：'l'？否
            ├─ "usb" (n=1)：'u'？否
            ├─ "cw"  (n=1)：'c'？是 ← 会抢先命中 "cwn"！
            └─ ...
```

**关键规则：长前缀必须排在短前缀之前**。现有代码里 `"fms"`（3 字符）排在 `"fm"`（1 字符）之前就是这个原因。加 `cwn` 时同理——`cwn` 分支必须放在 `cw` 分支**前面**，否则 `mode cwn` 会被 `strncmp(cmd, "cw", 1)`（只比首字符 `'c'`）截胡，切换成普通 CW。

#### 4.4.3 源码精读

`cmd_mode` 在 [main.c:L657-L679](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L657-L679)：[main.c:L661](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L661) 的 usage 字符串（顺带说它漏印了 `cw`，加新模式时应一并补全为 `mode {lsb|usb|am|fm|fms|cw|cwn}`）；[main.c:L666-L678](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L666-L678) 的前缀链——注意 [L674-L675](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L674-L675) `"fms"` 用 `strncmp(cmd, "fms", 3)` 且排在 `"fm"` 之前，这正是你要模仿的排序模式。

命令注册表在 [main.c:L874-L904](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L874-L904)：`commands[]` 以 `{ NULL, NULL }` 结尾（框架靠哨兵判断表尾）。`mode` 项在 [L891](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L891)。本讲场景不改这张表；若你的新功能需要新命令（比如 `cwn` 的带宽调节），照抄一行 `{ "cwnbw", cmd_cwnbw }` 即可。

#### 4.4.4 代码实践

**实践目标**：体证前缀匹配的顺序敏感性，学会用现有命令做「无硬件实验」。

**操作步骤**：

1. **读代码推演**：对照 [main.c:L666-L678](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L666-L678)，写下输入 `mode f`、`mode fm`、`mode fms`、`mode fmsx` 各自命中哪个分支。
2. **有硬件时验证**：用 `python/centsdr.py -c "mode fms"` 与 `-c "mode fm"` 分别切换，随后 `python/centsdr.py -s`（或 `show mode`）读回模式名，核对与推演一致。
3. **接一个 bug**：故意把你的 `cwn` 分支写到 `cw` 分支之后，重复推演 `mode cwn` 的实际效果；再把它挪到前面，确认修复。

**需要观察的现象**：`mode fmsx` 也会命中 `fms`（前缀匹配只查前 3 个字符）——这是既有设计（容忍简写），不是 bug，但你要知道。

**预期结果**：顺序正确时 `mode cwn` → `show mode` 返回 `cwn`；顺序错误时返回 `cw`。**待本地验证**（第 2 步需硬件）。

#### 4.4.5 小练习与答案

**练习 1**：用户输入 `mode`（无参数），会发生什么？

**答案**：`argc == 0` 分支打印 usage 后返回（main.c:L660-L663），不切换。这是 u1-l4 总结的「先校验参数」套路。

**练习 2**：为什么所有分支用 `strncmp(cmd, "am", 1)` 这种 1 字符比较，而不是 `strcmp`？

**答案**：作者有意支持唯一前缀简写——`mode a` 等价 `mode am`，方便串口手动输入。代价就是练习 1 讨论的顺序敏感与超集误匹配（`fmsx` 命中 `fms`）。改成 `strcmp` 可消除歧义但丢掉简写便利。

**练习 3**：给 CW-600 加一条独立的 `cwnbw {300|600|150}` 命令切换三套系数，需要动哪些地方？

**答案**：dsp.c 增加三套系数表与按 uistat 某字段选择实例的逻辑（仿 `cw_tone_phasestep` 的 `update_cwtone` 模式，main.c:L228-L232）；main.c 增加 `cmd_cwnbw` 函数与 `commands[]` 一行；若要旋钮可调再动 ui.c。注意 `signal_process` 与 `mod_table` 都不用动——算法内部参数不属于模式注册层。

### 4.5 交互侧：UI 档位、图标槽位与配置兼容

#### 4.5.1 概念说明

第四个扩展点决定新模式能否被**旋钮够到**、被**屏幕正确显示**、被**Flash 正确记住**。三件事各有牵连：

- **旋钮**：`uistat.mode == MOD` 档旋转时对 `uistat.modulation` 加减并调 `set_modulation`（u4-l4）。好消息：这段代码用 `MOD_MAX` 做边界，**枚举追加后旋钮自动覆盖新模式，ui.c 一行都不用改**。
- **图标**：`display.c` 把 `uistat.modulation` **直接当 `icons48x20[]` 的下标**画模式图标。数组前 6 个图标是六种调制、后 4 个是 AGC 档位（靠 `ICON_AGC_OFF` 偏移访问）。新增第 7 种调制会挤占 AGC 的槽位——**必须同步插图标并平移 `ICON_AGC_OFF`**。
- **配置**：`config_t` 把 `uistat.modulation` 存进 Flash（u4-l5）。枚举只追加不插入，旧配置里的 0~5 含义不变，**兼容性免费保住**。

#### 4.5.2 核心流程

```text
旋钮在 MOD 档旋转（ui_process）
  └─ uistat.modulation ± 1，钳位 [0, MOD_MAX-1]
       └─ set_modulation(uistat.modulation)
            └─ update_frequency()   ← 补偿 freq_offset 变化引起的本振偏移

屏幕状态栏（draw_info）
  └─ ili9341_drawfont(uistat.modulation, &ICON48x20, ...)
       ↑ 枚举值 == 图标下标（当前 0..5 = CW,LSB,USB,AM,FM,STEREO）

AGC 图标
  └─ ili9341_drawfont(uistat.agcmode + ICON_AGC_OFF, &ICON48x20, ...)
       ↑ 6..9 = OFF,SLOW,MID,FAST（ICON_AGC_OFF == 6）
```

新模式落地后的图标排布（**示例方案**，追加式）：

| 下标 | 0 | 1 | 2 | 3 | 4 | 5 | **6** | **7** | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 内容 | CW | LSB | USB | AM | FM | STEREO | **CWN(新插图)** | OFF | SLOW | MID | FAST |
| 访问方 | `uistat.modulation`（0~6） | | | | | | | `agcmode + ICON_AGC_OFF`（ICON_AGC_OFF 改为 **7**） | | | |

#### 4.5.3 源码精读

旋钮侧 [ui.c:L328-L336](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L328-L336)：MOD 档分支用 `< MOD_MAX-1` 与 `> 0` 钳位后调 `set_modulation` 并 `update_frequency()`（重算本振，因为不同模式 `freq_offset` 不同）。**因为边界写的是 `MOD_MAX` 而非魔法数字 6，枚举追加后此代码零改动自动生效**——这是「用枚举边界常量」的回报。

另外两处自动覆盖新模式的调用点：开机初始化 [ui.c:L221-L235](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L221-L235) 的 `ui_init` 里 `set_modulation(uistat.modulation)`（恢复上次模式），以及信道召回 [ui.c:L208-L219](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L208-L219) 的 `recall_channel`——存了 `MOD_CWN` 的信道召回时同样走通。

图标消费点在 [display.c:L1278-L1283](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1278-L1283)：调制图标直接用 `uistat.modulation` 当字形下标；两行之后的 AGC 图标用 `uistat.agcmode + ICON_AGC_OFF`。`ICON_AGC_OFF` 定义在 [nanosdr.h:L182](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L182)，当前为 6。

图标库本体 [icons.c:L23-L264](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/icons.c#L23-L264)：`icons48x20[][2*20]` 共 10 幅 48×20 图标，源码注释顺序为 CW(L24)、LSB(L48)、USB(L72)、AM(L96)、FM(L120)、STEREO(L144)、OFF(L168)、SLOW(L192)、MID(L216)、FAST(L240)。**你的新图标要插在 STEREO 之后（新下标 6），原 OFF~FAST 顺移为 7~10，同时把 `ICON_AGC_OFF` 从 6 改成 7。** u2-l4 讲过该数组的按行取模格式（`font_t` 的 stride/slide 字段），复制 STEREO 的位图行改画即可。

配置侧 [nanosdr.h:L284-L299](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L284-L299)：`channel_t` 与 `uistat_t` 都含 `modulation_t`，随 `config_t` 整体落 Flash。枚举追加在末尾时，旧固件存下的 0~5 在新固件里语义不变；反向（新固件存的 6 被**旧固件**读到）会表现为越界图标/未知模式——降级场景需知晓，但本项目无此约束。

#### 4.5.4 代码实践

**实践目标**：把图标槽位算清楚，形成改动清单。

**操作步骤**：

1. 数一遍 `icons.c` 的图标（用注释行定位：24/48/72/96/120/144/168/192/216/240 共 10 个），确认前 6 后 4 的布局。
2. 画出追加 `MOD_CWN` 后的槽位表（同 4.5.2 的表格），标出哪些下标变了、哪些没变。
3. 写出三处改动：① icons.c 在 STEREO 后插入 48×20 新图标（初始可先复制 STEREO 位图占位）；② nanosdr.h 的 `ICON_AGC_OFF` 6→7；③ 确认 display.c 两处 drawfont **无需修改**（它们的表达式 `uistat.modulation` 与 `agcmode + ICON_AGC_OFF` 都用的是变量）。
4. 若暂不想做图标：可以接受 `mode cwn` 后状态栏显示成 OFF 图标（下标 6 恰是 OFF）的临时状态，把改图标留作后续——但要在代码注释里写明 TODO。

**需要观察的现象**：第 3 步中你会发现自己**改的是数据与一个宏，而非 display.c 的逻辑**——这正是「枚举即下标」设计的维护代价与便利所在。

**预期结果**：得到一份三行改动清单，改动全部落在数据侧。**待本地验证**（最终效果需硬件烧录后目视确认）。

#### 4.5.5 小练习与答案

**练习 1**：不插图标、不改 `ICON_AGC_OFF`，直接追加 `MOD_CWN` 会怎样？

**答案**：分两种情形。**只追加枚举、不插图标**：状态栏调制图标显示为 OFF 图标（新枚举值 6 恰好落在 OFF 槽位），而 AGC 图标暂时不受影响（`agcmode + 6` 仍指向原 OFF~FAST 四格）——功能能用，只是图标错得难看。**插了新图标、却忘了把 `ICON_AGC_OFF` 改成 7**：AGC 四档整体左移一格，manual 档会画出新 CWN 图标、fast 档画成 MID。结论：**插入图标与平移宏必须同一次提交完成**。

**练习 2**：`uistat.mode`（编辑焦点档位）与 `uistat.modulation`（调制模式）名字很像，如何快速区分？

**答案**：`uistat.mode` 是 UI 状态机的档位（CHANNEL/FREQ/VOLUME/MOD/…，决定旋钮此刻调什么，nanosdr.h:L257-L258）；`uistat.modulation` 是射频解调模式（CW/LSB/…，nanosdr.h:L263）。前者是「旋钮正在编辑谁」，后者是「接收机是什么」。

**练习 3**：长按旋钮保存配置（u4-l4）会把 `MOD_CWN` 写进 Flash。用 `channel list` 能读回 `cwn` 吗？

**答案**：能——`cmd_channel` 的 list 分支打印 `mod_table[config.channels[channel].modulation].name`（main.c:L779-L781），新表项的 `"cwn"` 自动生效。这也是 4.3 强调表项必须有 `name` 字段的原因之一。

### 4.6 构建侧：Makefile `CSRC` 与新源文件

#### 4.6.1 概念说明

第五个扩展点最简单也最容易漏：**新建 `.c` 文件必须手工登记进 `CSRC`**。这个 Makefile 没有通配符（`*.c`），源文件是显式枚举的——ChibiOS 子系统靠 `.mk` 片段批量引入（Makefile:L92-L105），CMSIS-DSP 精选五个文件（Makefile:L113-L117），自有代码逐个手列（Makefile:L131-L134）。

本讲的 CW-600 变体直接把代码写进现有 `dsp.c`，**不需要动 Makefile**；但当你为新算法开了独立文件（比如 `demod_sam.c`），就必须登记。

#### 4.6.2 核心流程

```text
make
  ├─ include ChibiOS 各 .mk → STARTUPSRC/KERNSRC/... （框架源，免管）
  ├─ DSPLIBSRC = 5 个 CMSIS 文件（biquad、CFFT、位反转、公共表）
  ├─ CSRC = $(STARTUPSRC) ... $(DSPLIBSRC) + usbcfg.c si5351.c ...
  │                                          └─ 自有源手工清单 ← 在这里加行
  └─ rules.mk 按 CSRC 逐个编译、链接、objcopy 出 bin/hex/elf
```

#### 4.6.3 源码精读

CMSIS-DSP 的精选清单在 [Makefile:L111-L117](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L111-L117)：只编了 `arm_biquad_cascade_df1_q15`（Weaver/AM 滤波）与 CFFT 三件套加公共表（频谱显示）。**如果你的新算法要用别的 CMSIS 函数（如 `arm_fir_q15`），必须在这里加行**，否则链接报 undefined reference。

自有源清单在 [Makefile:L121-L134](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L121-L134)，末尾 `dsp.c main.c flash.c crt2.c` 一目了然。加新文件就是在 [L134](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L134) 的行尾续写，例如 `demod_sam.c`。

顺带两个相关开关：`USE_LINK_GC = yes`（[Makefile:L21-L24](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L21-L24)）意味着未注册的算法会被裁掉（呼应 4.1.4）；`ULIBS = -lm`（[Makefile:L228](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L228)）链接了数学库，`am_demod` 之外的浮点调用（如 `sinf`）可用，但中断上下文慎用软浮点（FPU 是硬的，`USE_FPU=hard`，性能尚可，见 u5-l1 的负载预算）。

#### 4.6.4 代码实践

**实践目标**：走一遍「新文件进构建」的全流程（用后即删的演练）。

**操作步骤**：

1. 新建 `demod_sam.c`，内容只有一行 `#include "nanosdr.h"` 加一个空函数 `void sam_demod(int16_t *s, int16_t *d, size_t l) { (void)s; (void)d; (void)l; }`。
2. 在 `nanosdr.h` 的 demod 声明区（L116-L121 附近）加 `void sam_demod(int16_t *src, int16_t *dst, size_t len);`。
3. **先不改 Makefile**，执行 `make`（无工具链时用 `make -n` 看命令序列即可），观察链接是否报 undefined reference（若没人引用它，实际上连报错都没有——文件根本没被编译）。
4. 把 `demod_sam.c` 追加进 CSRC，再 `make`，确认 `build/` 下出现 `demod_sam.o`。
5. 在 `mod_table` 里临时加一行引用 `sam_demod`，`make` 通过后用 `arm-none-eabi-nm build/ch.elf | grep sam_demod` 确认符号存在。
6. 演练完毕，回滚全部改动（`git checkout .` 与删除新文件）。

**需要观察的现象**：第 3 步与第 4 步的差别——不登记 CSRC 时文件被构建系统完全无视。

**预期结果**：登记后编译产物包含新对象文件；链接期因被 `mod_table` 引用而保留符号。**待本地验证**（需 arm-none-eabi 工具链）。

#### 4.6.5 小练习与答案

**练习 1**：为什么 `UISRC` 这样的变量不存在，ui.c 却被编译了？

**答案**：ui.c 被直接写进 `CSRC` 的手工清单（Makefile:L132）。ChibiOS 框架源才走 `.mk` 片段变量；应用源就是平铺列举。

**练习 2**：新算法用了 `arm_fir_q15` 但忘了加进 `DSPLIBSRC`，编译期能发现吗？

**答案**：编译期不能（各 .c 独立编译），**链接期**报 `undefined reference to arm_fir_q15`。把报错当提示，回 Makefile:L113-L117 加上 `.../FilteringFunctions/arm_fir_q15.c` 即可。

**练习 3**：把新文件放进子目录 `dsp/sam.c` 并写 `dsp/sam.c` 进 CSRC，还需要什么？

**答案**：若它只 include 项目根的 `nanosdr.h`，通常还要保证头文件搜索路径覆盖项目根——`INCDIR`（Makefile:L163-L166）当前未显式列 `.`，但编译规则隐含工作目录；稳妥做法是先照现有平铺布局放根目录，与仓库风格一致（CLAUDE 风格匹配：仓库本就是平铺结构）。

## 5. 综合实践

**任务：端到端实现 `cwn` 模式——带宽 600Hz 的窄带 CW 变体，走通全部五个扩展点。**

以下 diff 以伪 diff 形式给出关键改动（**示例代码**，基于当前 HEAD 源码推导；`bq_coeffs_600hz` 的具体数值由 4.2.4 的 Python 流程产出）：

**改动 1：枚举与声明（nanosdr.h）**——追加到末尾保兼容：

```diff
 typedef enum {
   MOD_CW, MOD_LSB, MOD_USB, MOD_AM, MOD_FM, MOD_FM_STEREO,
+  MOD_CWN,
   MOD_MAX
 } modulation_t;
```

同文件 demod 声明区追加：`void cwn_demod(int16_t *src, int16_t *dst, size_t len);`
理由：枚举是三种数据的共享主键，只许追加；声明让 main.c 可引用。

**改动 2：算法（dsp.c）**——新系数表 + 翻版 cw_demod：

```diff
+// 6th order elliptic lowpass filter fc=600Hz, 60dB  （数值来自 4.2.4 的设计流程）
+q15_t bq_coeffs_600hz[] = { /* 3 行 × {b0,0,b1,b2,a1,a2}，q15 定点 */ };
+arm_biquad_casd_df1_inst_q15 bq_cwn_i = { 3, bq_i_state, bq_coeffs_600hz, 1};
+arm_biquad_casd_df1_inst_q15 bq_cwn_q = { 3, bq_q_state, bq_coeffs_600hz, 1};
+
+void
+cwn_demod(int16_t *src, int16_t *dst, size_t len)
+{
+  weaver_demod_conf_t dc = {
+    mode_freqoffset_phasestep, cw_tone_phasestep, &bq_cwn_i, &bq_cwn_q
+  };
+  demod_weaver(src, dst, len, &dc);
+}
```

理由：完全复用 Weaver 流水（含四个显示钩子），只换滤波器；实例沿用共享状态数组的既有惯例（dsp.c:L329-L330 同款）。

**改动 3：注册（main.c 的 mod_table）**：

```diff
   { fm_demod_stereo,       0, 192, "fms" },
+  { cwn_demod, AM_FREQ_OFFSET,  48, "cwn" },
 };
```

理由：与 `MOD_CWN` 的枚举位置严格对齐（末位对末位）；沿用 CW 的 10kHz 低中频与 48kHz 采样率，使 `PHASESTEP` 编译期假设成立。

**改动 4：shell 接线（main.c 的 cmd_mode）**——注意长前缀在前：

```diff
     cmd = argv[0];
+    if (strncmp(cmd, "cwn", 3) == 0) {
+      set_modulation(MOD_CWN);
+    } else if (strncmp(cmd, "am", 1) == 0) {
-    if (strncmp(cmd, "am", 1) == 0) {
```

同时把 usage 补成 `mode {lsb|usb|am|fm|fms|cw|cwn}`。理由：`cwn` 的首字符 `c` 会被 `strncmp(cmd,"cw",1)` 截胡，必须排在其前——完全复刻 `fms` 先于 `fm` 的既有排序（main.c:L674-L677）。

**改动 5：图标（icons.c + nanosdr.h）**：在 STEREO 图标（icons.c:L144 起）之后插入一幅 48×20 新图标（可先复制占位），并把 `#define ICON_AGC_OFF 6` 改为 `7`。理由：图标下标 = 枚举值，插入后 AGC 四档顺移，宏必须同步（4.5.4）。

**改动 6（本例不需要，演练过即可）**：Makefile CSRC——代码进了现有 dsp.c，无需登记；若你拆了新文件，回看 4.6。

**验证清单**：

1. `make` 编译通过，`arm-none-eabi-size build/ch.elf` 确认 Flash 增量在几百字节内（一张系数表 + 一个小函数 + 一幅图标）。
2. 有硬件：烧录后 `mode cwn` → `show mode` 返回 `cwn`；旋钮调到 MOD 档旋转可循环出新模式；`stat` 看负载与普通 CW 相当（同为 Weaver 六阶滤波）；收一个 CW 信号对比 150Hz 与 600Hz 的听感带宽；`channel save` 后长按重启验证召回。
3. 无硬件：以上第 2 步全部标注**待本地验证**，至少完成 4.3.4 的纸上推演与 4.2.4 的系数设计。

**交付物**：一份完整 `git diff`，以及按上述六条逐一说明「为什么改这里」的备忘（可直接沿用本节各改动的「理由」段落）。

## 6. 本讲小结

- CentSDR 的模式扩展是**五个插口**的流水作业：`signal_process` 函数指针（热切换）、`mod_table`（参数聚合注册）、shell `cmd_mode`/`commands` 表（可达性）、`modulation_t` 枚举（共享主键）与 Makefile `CSRC`（构建登记）。
- 解调函数是一份**中断上下文契约**：固定签名、交织 IQ 进、交织双声道出、限时完成、禁止阻塞、并在四个钩子喂 `disp_fetch_samples`——复用 `demod_weaver` 可一次性继承全部义务。
- `modulation_t` 枚举值同时是 `mod_table` 下标、`icons48x20` 图标下标和 Flash 存储整数，因此**只许追加到末尾**；插入会同时破坏配置兼容与图标映射。
- shell `cmd_mode` 是前缀匹配链，**长前缀必须排前面**（`fms`>`fm`、`cwn`>`cw`），这是接线时最容易踩的一字之差。
- `mod_table` 的注册还有链接期意义：`USE_LINK_GC` 下，被表引用的算法才不会被 `--gc-sections` 裁掉，`nm` 可用来客观检测「忘了接线」。
- 新源文件必须手工进 `CSRC`，用到新的 CMSIS-DSP 函数还要扩 `DSPLIBSRC`，缺漏表现为链接期 undefined reference。

## 7. 下一步学习建议

- 下一讲（u5-l5）把视角拉到**算法设计与验证工作流**：用 scipy/notebook 设计滤波器、用 `centsdr.py` 抓波形验证固件行为——正好衔接本讲 4.2.4 产出的系数如何被系统性验证。
- 若想挑战真正的 SAM（同步 AM）解调：在 CW-600 的骨架上，把一次混频的固定步进换成**载波跟踪 PLL**（参考 `stereo_separate` 的 19kHz 导频环实现，dsp.c:L613-L683，鉴相→积分→反馈相位步进的三件套可直接移植），这就是一份完整的「高级练习」。
- 建议重读三处源码巩固本讲：`set_modulation`（main.c:L179-L194）体会接线顺序、`cw_demod`（dsp.c:L399-L407）体会配置拼装、`ui_process` 的 MOD 分支（ui.c:L328-L336）体会用 `MOD_MAX` 做边界的自适应性。
- 改完后跑一遍 u1-l4 学的 `python/centsdr.py` 脚本化验证（`mode`/`show`/`stat`/`data` 四连），把「烧录→手敲命令→听」升级为「一条脚本回归」——这是二次开发从样品走向可维护工程的分水岭。
