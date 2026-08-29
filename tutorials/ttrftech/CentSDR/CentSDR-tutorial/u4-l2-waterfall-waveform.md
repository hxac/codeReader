# 时域之美：瀑布图与波形绘制

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `wfdispmode`（瀑布/波形渲染方式）与 `spdispmode`（采样抓取点）这两个枚举各自管什么、由谁修改。
- 读懂 `colormap` 五段线性插值表和 `pick_color()` 的索引计算，并能手工算出任意灰阶对应的颜色。
- 讲解 `draw_waterfall()` 如何把一次频谱快照变成一行像素、并用软件行指针实现"滚动"。
- 讲解 `draw_waveform()` 如何用 `v2ypos()` 把 q31 样本值映射成 y 坐标，以及它如何填补相邻样本之间的竖直空隙。
- 理解 `spi_buffer` 这个 4096 像素公共缓冲如何约束所有绘图函数的块大小，以及 `ili9341_draw_bitmap()` 的 DMA 批量送屏为什么比 `ili9341_fill()` 的 CPU 循环快。

本讲承接 u4-l1：上一讲我们搞清楚了"样本从哪里抓、怎么加窗、怎么做 FFT"，本讲回答"FFT 之后（和之前）的数据如何变成屏幕下方的瀑布图与波形图"。

## 2. 前置知识

### 2.1 RGB565 像素格式

ILI9341 屏幕每个像素用 16 位表示：高 5 位红色、中 6 位绿色、低 5 位蓝色（人眼对绿色更敏感，所以绿色多给一位）。固件用一个宏把 8 位的 R/G/B 打包成 16 位：

- [nanosdr.h:189](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L189) — `RGB565` 宏，注意形参顺序是 `(b, g, r)`，第三个参数才是红色。

这个"参数顺序反直觉"是本讲反复踩到的坑，记住它，4.2 节会专门分析。

### 2.2 伪彩色（pseudocolor）

瀑布图把信号强度（0~63 共 64 级）映射成颜色。人眼对亮度的分辨能力只有二十多级，但对色相的变化很敏感，所以把灰度"染色"成 黑→红→绿→蓝→白 一类的渐变，能一眼看出弱信号。做法就是一张 5 个控制点的查表 + 相邻点之间线性插值。

### 2.3 对数刻度回顾

u4-l1 讲过 `log2_i64()` 返回 8.8 定点数，数值上等于 \(256 \log_2 x\)。功率每翻一倍（1 个 \(\log_2\) 单位）对应 3.01dB，本讲的 dB 换算都用这个关系。

### 2.4 "一行瀑布图 = 一次频谱快照"

瀑布图本质上是把频谱图"侧过来按时间堆叠"：频谱图横轴是频率、纵轴是强度；瀑布图横轴仍是频率，但纵轴变成了时间——每来一帧新数据就画一行，越新的行越靠下（本固件的实际滚动方式见 4.3 节）。

## 3. 本讲源码地图

| 文件 | 本讲关注的内容 |
| --- | --- |
| `display.c` | `colormap`/`pick_color` 伪彩色、`draw_waterfall`、`draw_waveform`、`v2ypos`、`disp_process` 的分发顺序 |
| `ili9341.c` | `spi_buffer` 公共缓冲、`ili9341_draw_bitmap` 的 DMA 传输、`ili9341_fill` 的 CPU 填充 |
| `nanosdr.h` | `RGB565` 宏、`uistat_t` 中 `spdispmode`/`wfdispmode` 枚举、`spi_buffer` 声明 |
| `ui.c` | 旋钮在 SPDISP/WFDISP 档位时如何修改这两个枚举 |
| `main.c` | Thread2 每 10ms 调用一次 `disp_process()` |

## 4. 核心概念与源码讲解

### 4.1 屏幕分区与两种显示模式的分工

#### 4.1.1 概念说明

屏幕下方那块 320×88 的区域（y=152~239）有两种完全不同的画法：瀑布图和波形图。固件用两个独立的枚举描述"抓哪里"和"怎么画"：

- `uistat.spdispmode`（u4-l1 已讲）：在解调链的 4 个钩子（原始 IQ / 混频后 / 滤波后 / 输出音频）中选一个抓取点。
- `uistat.wfdispmode`：决定下方区域渲染成瀑布还是波形（含两种放大变体）。

两者定义在：

- [nanosdr.h:270-271](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L270-L271) — `spdispmode` 四档与 `wfdispmode` 四档（`WATERFALL, WAVEFORM, WAVEFORM_MAG, WAVEFORM_MAG2`）的枚举。

用户把旋钮档位调到 `SPDISP` 或 `WFDISP` 后旋转编码器即可修改它们：

- [ui.c:346-350](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L346-L350) — 旋钮在 SPDISP 档改 `spdispmode`，在 WFDISP 档改 `wfdispmode`，都经过 `minmax` 钳位。

#### 4.1.2 核心流程

整个屏幕从上到下分成五块，各由不同函数负责：

| y 范围 | 内容 | 绘制函数 |
| --- | --- | --- |
| 0~47 | 频率大数字 | `draw_freq` |
| 48~71 | 状态栏（音量/模式/AGC/功率） | `draw_info`、`draw_power` |
| 72~135 | 频谱柱状图（64 像素高） | `draw_spectrogram` |
| 136~151 | 频率刻度 | `draw_tick` |
| 152~239 | 瀑布图 / 波形图（88 像素高） | `draw_waterfall`、`draw_waveform` |

Thread2 每 10ms 醒来一次处理显示标志位。当 DSP 侧攒满显示缓冲（置 `FLAG_SPDISP`）后，`disp_process` 按固定顺序调用三个绘制函数：

```text
Thread2 (main.c:906) ──每10ms──> disp_process()
    └─ FLAG_SPDISP 置位时：
        1. draw_waveform()    ← 读「时域」数据，wfdispmode==WATERFALL 时立即返回
        2. draw_spectrogram() ← 就地做 1024 点 CFFT，把缓冲变成「频域」
        3. draw_waterfall()   ← 读「频域」数据，wfdispmode!=WATERFALL 时立即返回
```

这个调用顺序是本讲最精妙的一处：**同一块 `SPDISP_BUFFER` 被用了两次**——波形图在 FFT 之前读它（时域样本），瀑布图在 FFT 之后读它（频率 bin）。

#### 4.1.3 源码精读

- [display.c:1411-1420](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1411-L1420) — `disp_process` 中 `FLAG_SPDISP` 分支，注意三个 draw 的调用次序：`draw_waveform(); draw_spectrogram(); draw_waterfall();`，最后清标志。
- [display.c:783](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L783) — `draw_spectrogram` 第一句 `arm_cfft_radix4_q31(&cfft_inst, buf)` 对 `spdispinfo.buffer` **原地**变换，这就是"缓冲第二次生命"的转折点。
- [display.c:866-867](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L866-L867) — `#define YPOS 152`、`#define HEIGHT 88`，152+88=240 正好到屏幕底边。
- [main.c:906-924](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L906-L924) — Thread2 循环体：`disp_process(); ui_process(); chThdSleepMilliseconds(10);`，显示与 UI 都由这个线程驱动。

顺带一提：`YPOS` 和 [display.c:886-894](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L886-L894) 的 `inrange()` 在当前代码里**没有被任何地方调用**（`draw_waveform` 用内联的 imin/imax 逻辑代替了它），属于历史遗留，读码时不要被它们迷惑。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：把屏幕五块分区与绘制函数的对应关系落到源码行号上。
2. **操作步骤**：在 `display.c` 中找出五个绘制函数里所有写死的 y 坐标（如 `draw_bitmap(sx, 72, ...)`、`ili9341_fill(0, 136, ...)`、`ili9341_draw_bitmap(xx, 152, ...)`），核对它们与上表一致。
3. **需要观察的现象**：每个函数只画自己那块，互不越界；`draw_spectrogram` 不检查 `wfdispmode`，所以柱状频谱永远在画。
4. **预期结果**：五个函数的 y 区间拼起来恰好覆盖 0~239，无缝隙也无重叠。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `disp_process` 必须先调 `draw_waveform` 再调 `draw_spectrogram`，顺序颠倒会怎样？

**答案**：两者共用 `SPDISP_BUFFER`，`draw_spectrogram` 会就地把缓冲变换成频域。先 FFT 的话，波形图读到的就是频域数据，画出来不再是时域波形，而是 FFT 结果的实部/虚部轨迹。

**练习 2**：`spdispmode` 和 `wfdispmode` 各自控制什么？

**答案**：`spdispmode` 决定从解调链的哪个钩子抓样本（数据来源），`wfdispmode` 决定下方 88 像素高的区域用瀑布还是波形方式渲染（呈现形式）。两者正交，任意组合都合法。

**练习 3**：瀑布图一行的更新周期大概是多少？

**答案**（推导）：`SPDISP_BUFFER` 有 2048 个 q31（1024 个复样本）。dsp.c 各抓取点每次回调最多写入 240 个 q31（`BT_IQ` 时 buflen=120、每个样本写 2 个 q31；交织型 buflen=240、写 240 个 q31），因此攒满约需 2048/240 ≈ 8.5 个回调。48kHz 时一个回调 5ms，一行约 43ms（约 23 行/秒），88 行画面约 3.8 秒才能整体刷一遍；192kHz 时回调周期 1.25ms，一行约 11ms。

### 4.2 伪彩色映射：colormap 与 pick_color

#### 4.2.1 概念说明

瀑布图每个像素的输入是 0~63 的强度等级，输出是 16 位颜色。固件没有用 64 项颜色大表，而是只存 5 个"控制点"，中间颜色现算——这就是分段线性插值。5 个点把 0~63 分成 4 段，每段 16 级，插值只需一次乘加。

#### 4.2.2 核心流程

设强度为 \(m\)（0~63），查表索引和段内比例这样取：

\[ idx = \left\lfloor m/16 \right\rfloor \bmod 4, \qquad p = m \bmod 16 \]

插值公式（对 R/G/B 三通道各自执行）：

\[ C(m) = \frac{(16-p)\,C_{idx} + p\,C_{idx+1}}{16} \]

即"离哪个控制点近，颜色就偏向谁"。`16-p` 和 `p` 是权重，除以 16 用 `>>4` 实现。输出经 `RGB565` 打包后就是屏幕像素。

#### 4.2.3 源码精读

- [display.c:833-839](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L833-L839) — `colormap` 五个控制点，字面值依次是黑、蓝、绿、红、白。
- [display.c:841-851](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L841-L851) — `pick_color()`：`idx = (mag >> 4) & 0x3` 取段号，`prop = mag & 0x0f` 取段内位置，`nprop = 0x10 - prop` 是互补权重，三个通道分别做 `c0*nprop + c1*prop` 再 `>>4` 归一化，最后 `RGB565(r>>4, g>>4, b>>4)` 打包。

**关键陷阱——参数顺序互换**：`RGB565` 宏的形参顺序是 `(b, g, r)`（第三个参数进红色位），而 `pick_color` 按 `(r, g, b)` 的习惯传入。展开后可以发现 colormap 的 r 字段进了蓝色通道、b 字段进了红色通道。于是屏幕上实际显示的渐变是 **黑→红→绿→蓝→白**，而不是按字面读的"黑→蓝→绿→红→白"。同理，`draw_waveform` 里 `RGB565(255,255,0)` 字面是黄色，实际显示为**青色**；`RGB565(255,0,255)` 是**品红**（这组对称值不受顺序影响）。读这段代码时切勿把字面量当成 R,G,B。

#### 4.2.4 代码实践（纸面计算型）

1. **实践目标**：不运行代码，手算两个灰阶的颜色，验证你理解了索引与插值。
2. **操作步骤**：
   - 计算 `pick_color(0)`：idx=0, prop=0 → colormap[0]，纯黑。
   - 计算 `pick_color(20)`：idx=1, prop=4, nprop=12 → r=(0×12+0×4)>>4=0，g=(0×12+255×4)>>4=63，b=(255×12+0×4)>>4=191，即传入 `RGB565(0, 63, 191)`。
   - 计算 `pick_color(48)`：idx=3, prop=0 → colormap[3] 原样，屏显纯蓝。
3. **需要观察的现象**：手算结果与 5. 综合实践里 Python 版 `pick_color` 的输出一致。
4. **预期结果**：`pick_color(20)` 屏显为暗土黄色（红 191/255、绿 63/255、蓝 0），处于"红→绿"段的前 1/4 处。待本地验证（用综合实践的脚本打印）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 64 级强度只需要 5 个表项？

**答案**：64 级 = 4 段 × 16 级。段与段之间的颜色由两端控制点线性插值得出，只有段边界（0、16、32、48、63）需要精确给出颜色，所以 5 个表项够用，还省了 59 项存储。

**练习 2**：把 `colormap` 改成只有黑和白两个控制点（中间三项全填灰），插值结果是什么效果？

**答案**：变成 64 级灰度渐变——任何一段的两端都是灰，插出来仍是灰，整体从黑渐变到白（每段斜率不同但连续）。

**练习 3**：`idx` 为什么写成 `(mag >> 4) & 0x3` 而不是 `mag / 16`？

**答案**：`mag` 此处必在 0~63 内，`>>4` 等价于除 16 但更快；`& 0x3` 是防御性钳位，万一 mag 超过 63（例如调用者忘了裁剪），索引也不会越界访问 `colormap[idx+1]` 造成数组越界读。

### 4.3 瀑布图：draw_waterfall 的逐行滚动

#### 4.3.1 概念说明

`draw_waterfall` 每次只画**一行** 320 个像素：对每个 x 像素累加其对应频率 bin 的功率，取对数、量化到 0~63，经 `pick_color` 染色后写入 `spi_buffer`，最后用一次 `ili9341_draw_bitmap` 把这 640 字节送上屏幕。行的写入位置由变量 `vsa` 逐次递增，形成"新数据不断覆盖下一行"的滚动效果。

#### 4.3.2 核心流程

```text
draw_waterfall()
  ├─ wfdispmode != WATERFALL ? 返回
  ├─ 取几何参数表（offset / stride，与频谱图共用同一张表）
  ├─ for x in 0..319：
  │    acc = Σ (I² + Q²)  over stride 个 bin   ← 从 FFT 后的缓冲取数
  │    v = (log2_i64(acc) − 34·256) >> 6        ← 8.8 定点对数，量化
  │    v 裁剪到 [0, 63]
  │    c = pick_color(v)；若 c==0 则换成背景色
  │    spi_buffer[x] = c
  ├─ vsa++ ；若 vsa≥240 则回到 152
  └─ ili9341_draw_bitmap(0, vsa, 320, 1, spi_buffer)   ← 只写一行
```

量化公式展开（`log2_i64` 返回 \(256\log_2 x\)）：

\[ v = 4\,(\log_2 acc - 34) \]

即每级对应 0.25 个 \(\log_2\) 单位（功率约 0.75dB），底电平门限取 34。对比频谱图的 [display.c:816](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L816)（÷77、门限 36，注释"1dB/pixel"）：瀑布图门限低 2 个 \(\log_2\) 单位（约 6dB），弱信号更容易显色；整幅动态范围约 47dB，比频谱图窄一些。（以上为推导值。）

#### 4.3.3 源码精读

- [display.c:978-993](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L978-L993) — `draw_waterfall` 开头：取 `spdispmode` 对应的几何参数、准备 `block = spi_buffer`、背景色在 `WFDISP` 档位激活时用 `BG_ACTIVE`，最后一句 `if (uistat.wfdispmode != WATERFALL) return;` 是模式的自我把关。
- [display.c:996-1016](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L996-L1016) — 主循环：`acc` 用 int64 累加 `stride` 个 bin 的 \(I^2+Q^2\)（下标 `i & 1023` 让负数 offset 回绕到 FFT 输出的上半区，即负频率 bin）；`v` 裁剪到 0~63 后经 `pick_color` 染色写入行缓冲。
- [display.c:1008](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1008) — 对数量化公式 `(log2_i64(acc) - (34<<8)) >> 6`。
- [display.c:1011-1013](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1011-L1013) — `c = pick_color(v); if (c == 0) c = bg;`：最弱信号本该是纯黑（0），但纯黑和背景色数值相同，这里把它替换成 `bg`，使得旋钮处于 WFDISP 档时安静背景呈现 `BG_ACTIVE` 的极暗色，提示"这块区域正在被调谐"。
- [display.c:1018-1026](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1018-L1026) — 滚动机制：`vsa` 从 152 递增到 239 后回绕到 152，每次把新行写到当前 `vsa` 行。注意 [display.c:1021-1025](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1021-L1025) 的 ILI9341 硬件垂直滚动命令（`0x37 VSCRSADD`）被 `#if 0` 注释掉了，[display.c:853-864](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L853-L864) 的 `waterfall_init` 里滚动窗口定义（`0x33 VSCRDEF`）同样被禁用。也就是说当前固件用的是**纯软件滚行**：新行按下移方向逐行覆盖，写满 88 行后下一行跳回顶部——视觉上是"从上往下刷新、到底后重来一次跳变"，而不是平滑上移。
- [display.c:562-576](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L562-L576) — `spdispparam_tbl` / `spdispparam_tbl_192khz`：瀑布图与频谱图共用的几何表，`offset` 是起始 bin（负值表示从负频率侧开始），`stride` 是每像素聚合的 bin 数。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：弄清滚动行为与"新行到底画在哪"。
2. **操作步骤**：跟踪 `vsa` 的完整生命周期——[display.c:976](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L976) 初值 152，[display.c:1018-1020](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1018-L1020) 递增与回绕，[display.c:1026](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1026) 作为 `draw_bitmap` 的 y 参数。
3. **需要观察的现象**（推断）：有硬件时把 `vsa++` 改成 `vsa--`、初值改 239、回绕条件改 `if (vsa < 152) vsa = 239;`，瀑布应变成"从下往上刷新"。
4. **预期结果**：修改后重新编译烧录，滚动方向反转。待本地验证（需要硬件）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `draw_waterfall` 里功率累加用 `int64_t acc` 而不是 `int32_t`？

**答案**：单个 q31 样本的平方就接近 int32 上限（\(2^{30} \times 2^{30} = 2^{60}\)），再乘以 stride（最多 3 个 bin、I/Q 两路）必然溢出。int64 累加是 u4-l1 已建立的惯例。

**练习 2**：把 `(34<<8)` 改成 `(38<<8)`，画面会怎么变？

**答案**：门限抬高 4 个 \(\log_2\) 单位（约 12dB），原来显色的弱信号现在被裁剪成 0 级（背景色），画面整体变"空"，只有强信号可见。反之调低门限会让噪声底也显色、画面变"花"。

**练习 3**：瀑布图一行只有 320 个像素、640 字节，为什么不用硬件的垂直滚动命令？

**答案**：代码里曾经尝试过（`#if 0` 的 `0x33`/`0x37` 命令），现在禁用了。纯软件滚行少了两条寄存器写入、也不依赖 ILI9341 滚动窗口与 RAMWR 地址的交互时序，代价是到底部后有一次视觉跳变。这是"简单够用"的取舍。

### 4.4 波形图：draw_waveform 与 v2ypos 坐标映射

#### 4.4.1 概念说明

波形图把缓冲当作示波器：I 通道画一条轨迹、Q 通道画另一条，横轴是时间（每像素 1 个样本），纵轴是样本幅值。核心是 `v2ypos()` 这个从 q31 数值到 0~87 像素行号的映射。`WAVEFORM_MAG` / `WAVEFORM_MAG2` 两个变体通过 `mag_shift` 在抓取阶段把样本左移 3/6 位，等效于垂直方向放大 8/64 倍，用于观察小信号。

#### 4.4.2 核心流程

\[ y = \mathrm{clamp}_{[0,\,87]}\left( \left\lfloor v / 2^{24} \right\rfloor + 44 \right) \]

其中 \(v\) 是 q31 样本。满幅 q15 样本（±32767）乘满幅窗值约得 ±\(2^{30}\)，右移 24 位得 ±64，加上中心 44 后落到 44±64，经裁剪正好用满 88 像素高度（推导值）。

绘制采用分块策略：每次处理 `BLOCK_WIDTH=46` 列，46×88=4048 像素恰好塞进 4096 项的 `spi_buffer`；320 列需要 7 块（最后一块 44 列）。每列像素的颜色按"背景 → 中心线/时间刻度 → I 轨迹 → Q 轨迹"的优先级用**按位或**叠加。

由于一列只画一个样本，相邻两点若跳变超过 1 行，中间会出现竖直空隙。代码用"把 imin/imax 扩展到相邻样本中点"的办法补隙——相当于用零成本近似画连接线。

#### 4.4.3 源码精读

- [display.c:876-884](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L876-L884) — `v2ypos()`：`v >>= 24` 把 q31 压缩到 ±128 量级，`v += HEIGHT/2` 平移到屏幕中心，双重裁剪到 [0, 87]。
- [display.c:898-911](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L898-L911) — `draw_waveform` 开头：轨迹底色/刻度色按 WFDISP 档是否激活选择（[display.c:896](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L896) `FG_SCALE` 为暗灰）；`WATERFALL` 模式立即返回。
- [display.c:913-918](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L913-L918) — `mag_shift` 选择：`WAVEFORM_MAG2`→6（×64）、`WAVEFORM_MAG`→3（×8）、否则 0。注意 `mag_shift` 是在 `window_*_15to31` 抓取时消费的（[display.c:649-652](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L649-L652)），而这里是在**绘制时**设置，所以切换放大倍数要等下一帧缓冲攒满才生效（约一帧延迟）。
- [display.c:920-923](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L920-L923) — 起读位置 `(512-160)*2`：从 1024 点缓冲的第 352 号样本开始，显示以缓冲中点为中心的 320 个复样本。
- [display.c:926-929](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L926-L929) — 分块：`w = BLOCK_WIDTH`，最后一块收窄到剩余列数（320 − 6×46 = 44）。
- [display.c:932-946](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L932-L946) — 取当前列样本 `i2/q2`，并把 imin/imax（qmin/qmax 同理）扩展到 `(i0+i1)/2` 与 `(i1+i2)/2`，即把轨迹的竖直覆盖范围拉到相邻样本的中点，填补跳变空隙。
- [display.c:948-963](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L948-L963) — 逐像素着色写入 `spi_buffer[y*w + i]`：先取背景，`y == HEIGHT/2` 画中心线，`x % 48 == 0` 画时间刻度（48kHz 下 48 样本 = 1ms，注释"draw 1ms tick"），随后 I 轨迹、Q 轨迹用 `|=` 叠加 `RGB565(255,255,0)`（屏显**青**）与 `RGB565(255,0,255)`（屏显**品红**）——又是 4.2 节那个形参顺序陷阱。
- [display.c:971-972](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L971-L972) — 每块完成后 `ili9341_draw_bitmap(xx, 152, w, HEIGHT, spi_buffer)` 送屏，`xx` 前移。

#### 4.4.4 代码实践（纸面 + 修改型）

1. **实践目标**：掌握 `v2ypos` 的量纲，能从样本值推出屏幕位置。
2. **操作步骤**：
   - 手算：\(v = 2^{26}\)（即 0x04000000）→ \(y = 4 + 44 = 48\)；\(v = -2^{26}\) → \(y = 40\)；\(v = 2^{30}\) → \(y = 64 + 44 = 108\) → 裁剪为 87（贴底）。
   - 修改实验：把 [display.c:954](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L954) 的 `x % 48` 改成 `x % 24`，时间刻度密度翻倍。
3. **需要观察的现象**：刻度线间距变为原来的一半；若采样率切到 96kHz，48 样本只对应 0.5ms，刻度含义随之改变（代码写死按 48kHz 注释）。
4. **预期结果**：重新编译后刻度线数量翻倍。待本地验证（需要硬件；无硬件时只需确认 `make` 通过）。

#### 4.4.5 小练习与答案

**练习 1**：`draw_waveform` 为什么从第 352 号样本开始画，而不是从 0 号？

**答案**：显示区只有 320 列，而缓冲有 1024 个复样本。从 352 开始恰好让 320 个样本以缓冲中点（512）为中心，展示"最近一小段"的对称窗口，而不是偏向最老的样本。

**练习 2**：`WAVEFORM_MAG` 和直接把 `v2ypos` 里的 `>>24` 改成 `>>21` 效果一样吗？

**答案**：数值上都是放大 8 倍，但作用点不同。`mag_shift` 在抓取阶段左移样本，会同时影响**同一缓冲**后续的 FFT/频谱/瀑布等一切消费者（缓冲是共享的）；改 `v2ypos` 只影响波形自身的 y 映射。固件选择在抓取端移位，意味着放大档位下频谱图也会跟着变亮。实际上由于 `mag_shift` 在绘制时才更新，切档还有一帧延迟。

**练习 3**：为什么 I/Q 两条轨迹用"按位或"而不是"覆盖"合成颜色？

**答案**：按位或让重叠处两种颜色分量叠加（青 | 品红 ≈ 白），一眼能看出 I、Q 同时经过该区域；覆盖则后画的会"吃掉"先画的，丢失重叠信息。代价是或运算在个别颜色组合下可能产生不在调色板里的中间色，但对示波器显示无关紧要。

### 4.5 送屏通道：spi_buffer 与 DMA 位图传输

#### 4.5.1 概念说明

所有绘图函数都不直接碰屏幕，而是先把像素攒进 `spi_buffer` 这个 4096 项（8KB）的公共暂存区，再一次性交给 `ili9341_draw_bitmap()` 用 DMA 搬到屏幕。这个"中转站"尺寸就成了所有绘图块的上限约束——波形图的 `BLOCK_WIDTH` 正是为塞进它而精心选择的。

#### 4.5.2 核心流程

```text
ili9341_fill(x,y,w,h,color)      ← CPU 循环逐像素 ssp_senddata16(color)，适合小面积/清屏
ili9341_draw_bitmap(x,y,w,h,buf) ← DMA 从 buf 批量搬运 w*h 个像素，CPU 在传输期间可做别的
```

两者都先用 `0x2A/0x2B/0x2C` 命令设好 CASET/PASET/RAMWR 窗口（u2-l4 讲过窗口机制），区别只在像素怎么发：`fill` 每个 16 位都过一次 CPU 寄存器写，`draw_bitmap` 配好 DMA 源地址、计数和 MINC（地址自增）后一键启动。

#### 4.5.3 源码精读

- [ili9341.c:31](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L31) — `uint16_t spi_buffer[4096];`：全固件共享的像素暂存区，声明在 [nanosdr.h:205](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L205)。字符、字体、频谱块、波形块、瀑布行全用它。
- [ili9341.c:225-235](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L225-L235) — `ili9341_fill`：设窗口后 `while (len--) ssp_senddata16(color);`，CPU 全程忙等。
- [ili9341.c:237-252](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L237-L252) — `ili9341_draw_bitmap`：设窗口后 [ili9341.c:247-251](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L247-L251) 配置 DMA（`dmaStreamSetMemory0` 指向位图、`SetTransactionSize(len)`、`SetMode(... | STM32_DMA_CR_MINC)` 开内存自增）再 `dmaWaitCompletion` 等完成。
- [display.c:868-870](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L868-L870) — 注释原文：`/* 46 * 88 = 4048 pixels < sizeof spi_buffer (4096) */`、`/* 320 / 46 = 6.96 -> draw block 7 times */`，把块宽选择的理由写在了代码里。
- [display.c:1026](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1026) — 瀑布图一行只有 320 像素（640 字节），一次 DMA 就送完，这是它能做到"每帧一行"依然轻松的原因。

注意 `spi_buffer` 是无保护的共享暂存区：好在所有绘图都发生在 Thread2 的 `disp_process` 一条调用链上（shell 命令只置标志位、不直接画屏），所以不会出现两个执行流同时写它的竞争。

#### 4.5.4 代码实践（计算 + 阅读型）

1. **实践目标**：体会 4096 像素的缓冲上限如何反过来决定绘图分块策略。
2. **操作步骤**：
   - 计算：若想整幅 320 列宽一次送屏，`4096 / 320 = 12.8`，最多 12 行——远小于波形的 88 行，所以波形必须分 7 块。
   - 验证 `BLOCK_WIDTH` 是 88 行高时的最大整块宽：`4096 / 88 = 46.5 → 46`，47 就会超（47×88=4136 > 4096）。
   - 思考：如果 `spi_buffer` 扩到 8192 项，`BLOCK_WIDTH` 可以改成多少？（`8192/88 = 93.09 → 93`，320 列只需 4 块，代价是 RAM 多占 8KB——对只有 40KB RAM 的 F303 来说不现实。）
3. **需要观察的现象**：块数减少意味着 `send_command` 窗口设置次数减少，但总像素数不变、DMA 总耗时基本不变。
4. **预期结果**：以上为纯计算，无需硬件即可完成。

#### 4.5.5 小练习与答案

**练习 1**：瀑布图每帧只传输 640 字节，波形图要传约 56KB，两者为何都不卡？

**答案**：瀑布一行数据量极小；波形 320×88=28160 像素 ×2 字节 ≈ 55KB 走 DMA，CPU 只负责算像素颜色（约 2.8 万次循环内计算），发送本身不占 CPU。且这一切发生在 10ms 周期的 Thread2 里，显示刷新率（`stat.fps`）有富余。

**练习 2**：`ili9341_fill` 为什么不也走 DMA？

**答案**：`fill` 发送的是**同一个值**的重复，DMA 需要内存自增读不同数据；要发常数就得要么先在 RAM 里铺一块同色缓冲（浪费暂存区），要么用不带 MINC 的 DMA 直接循环读同一个地址。固件选择了最简单的 CPU 忙等——清屏类操作对性能不敏感。

**练习 3**：`draw_spectrogram` 里 `uint16_t (*block)[32] = (uint16_t (*)[32])spi_buffer;` 这个写法在做什么？

**答案**：把一维的 `spi_buffer` 重新解释成 32 列宽的二维数组，随后 `block[63-y][x]` 按行列访问，代码更可读。这是 C 里"用不同镜头看同一块内存"的常见技巧（[display.c:797](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L797)），本质仍是那 4096 个像素的暂存区。

## 5. 综合实践

**任务**：把瀑布图配色从现在的五点渐变改成"黑→蓝→白"三色渐变，并在 PC 上先预览效果。

### 第一步：确定新的 colormap 表（关键：绕开 RGB565 形参陷阱）

目标是屏显黑→蓝→白，按 5 个控制点采样这条渐变：黑、深蓝、蓝、浅蓝白、白。根据 4.2 节的分析，colormap 的 `r` 字段实际进蓝色通道、`b` 字段实际进红色通道，所以要把想要的屏幕颜色做一次 R/B 对调再填表。

修改 [display.c:833-839](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L833-L839)：

```c
/* 示例代码：屏显 黑→蓝→白 渐变（注意 r/b 字段经 RGB565(b,g,r) 互换后落屏） */
const struct { uint8_t r,g,b; } colormap[] = {
        { 0, 0, 0 },           /* 屏显 黑 */
        { 64, 0, 0 },          /* 屏显 深蓝 */
        { 255, 0, 0 },         /* 屏显 蓝 */
        { 255, 128, 128 },     /* 屏显 浅蓝白 */
        { 255, 255, 255 }      /* 屏显 白 */
};
```

验证方法：对每个表项调用 `RGB565(r, g, b)` 展开宏，第一参数（蓝通道）取的是 `r` 字段，第三参数（红通道）取的是 `b` 字段。例如 `{255,0,0}` 落屏为蓝。若你希望"字面即所见"，也可以把 [display.c:850](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L850) 的调用改成 `RGB565(b>>4, g>>4, r>>4)`，但要意识到这会同时改变现有瀑布图的实际配色。

### 第二步：重新编译

按 u1-l2 的流程执行 `make`，确认编译链接通过。有硬件则烧录观察瀑布图新配色（待本地验证）；无硬件时编译通过即可，效果由第三步的 PC 预览代替。

### 第三步：PC 端等价预览

下面的 Python 脚本（Python 3，仅标准库）复刻 `pick_color` 与 `RGB565` 的算法，把 64 级伪彩色条和 64 级灰度参考条渲染成一张 PPM 图：

```python
#!/usr/bin/env python3
# 示例代码：PC 端预览 pick_color 渐变，输出 waterfall_preview.ppm
def rgb565(r, g, b):
    # 与固件宏同形：形参顺序 (b,g,r)，第三参数进红色位
    return (((b << 8) & 0xf800) | ((g << 3) & 0x07e0) | ((r >> 3) & 0x001f))

def unpack(c):
    return ((c >> 8) & 0xf8, (c >> 3) & 0xfc, (c << 3) & 0xf8)  # 还原 8bit R,G,B

COLORMAP = [(0,0,0), (64,0,0), (255,0,0), (255,128,128), (255,255,255)]  # 屏显黑→蓝→白

def pick_color(mag):           # 与 display.c:841-851 等价
    idx, prop = (mag >> 4) & 0x3, mag & 0x0f
    nprop = 0x10 - prop
    c0, c1 = COLORMAP[idx], COLORMAP[idx+1]
    ch = [(a * nprop + b * prop) >> 4 for a, b in zip(c0, c1)]
    return unpack(rgb565(*ch))

SCALE, H = 8, 64               # 每级 8 像素宽、每条 64 像素高
rows = []
for m in range(64):            # 上：64 级灰度参考条
    g = m * 4
    rows += [(g, g, g)] * SCALE
for m in range(64):            # 下：pick_color 伪彩色条
    rows += [pick_color(m)] * SCALE
img = bytearray()
for _ in range(H):
    img += b''.join(bytes(p) for p in rows)
with open('waterfall_preview.ppm', 'wb') as f:
    f.write(b'P6\n%d %d\n255\n' % (64 * SCALE, H))
    f.write(img)
print('pick_color(0) =', pick_color(0), ' pick_color(20) =', pick_color(20),
      ' pick_color(48) =', pick_color(48))
```

运行 `python3 waterfall_preview.py` 生成 `waterfall_preview.ppm`，用图片查看器打开（或 `python3 -c "from PIL import Image; Image.open('waterfall_preview.ppm').save('waterfall_preview.png')"` 转 PNG）。

**需要观察的现象**：上半部分是线性灰阶；下半部分应为 黑→深蓝→蓝→浅蓝白→白 的平滑渐变，4 段之间无跳变；终端打印的 `pick_color(20)` 应与你 4.2.4 节的手算（屏显红 191、绿 63、蓝 0 的暗土黄——注意这是**旧表**的手算，换新表后此值会变）形成对照。

**预期结果**：新表下 `pick_color(20)` 落在"深蓝→蓝"段的前 1/4，应打印出接近深蓝的 RGB 值。待本地验证。

### 第四步（选做，需硬件）

烧录后在 WFDISP 档旋转编码器切换 `WATERFALL` 与 `WAVEFORM`，对比两者：瀑布图的横轴是频率（信号移动=左右移动），波形图横轴是时间（正弦信号=周期轨迹）。这正是 4.1 节"同一缓冲、两次生命"的直观体现。

## 6. 本讲小结

- `spdispmode` 决定抓哪个解调链钩子的样本，`wfdispmode` 决定下方 320×88 区域画成瀑布还是波形，旋钮在 SPDISP/WFDISP 档时修改它们（ui.c:346-350）。
- `disp_process` 按 `draw_waveform → draw_spectrogram → draw_waterfall` 的顺序调用，让同一块 `SPDISP_BUFFER` 先以时域身份供波形读取、再被就地 FFT、最后以频域身份供瀑布读取。
- `pick_color` 用 5 个控制点做 4 段线性插值覆盖 64 级强度；由于 `RGB565(b,g,r)` 形参顺序与调用习惯相反，colormap 的 r/b 在屏幕上互换，实际渐变是黑→红→绿→蓝→白，波形 I/Q 轨迹是青/品红。
- `draw_waterfall` 每帧只算并送一行 320 像素：stride 个 bin 功率累加 → `log2_i64` 对数量化（门限 34、每级 0.25 个 log2 单位）→ 裁剪 0~63 → 染色；`vsa` 行指针软件滚动，硬件垂直滚动命令被 `#if 0` 禁用。
- `draw_waveform` 用 `v2ypos`（q31 右移 24 位加中心 44 再裁剪）映射 y 坐标，用"扩到相邻样本中点"的 imin/imax 补竖直空隙；MAG/MAG2 档通过 `mag_shift` 在抓取端放大 8/64 倍，且有一帧延迟。
- 所有绘图先攒进 4096 像素的 `spi_buffer` 再由 `ili9341_draw_bitmap` DMA 送屏；`BLOCK_WIDTH=46` 正是 46×88=4048 ≤ 4096 的最大整块，`ili9341_fill` 则用 CPU 循环发同色。

## 7. 下一步学习建议

本讲搞定了"下方动态区域"的渲染，下一讲 **u4-l3 屏幕信息架构** 转向静态区域：`disp_process` 的 `FLAG_UI/FLAG_POWER/FLAG_AUX_INFO` 标志位如何驱动增量刷新、`numfont20x24/numfont32x24` 大字号数字字库的排版、`draw_dbm` 的 8.8 定点功率格式化，以及 `draw_aux_info` 对 AGC 增益等辅助量的呈现。建议先自行阅读 [display.c:1140-1297](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1140-L1297) 的 `draw_freq` 与 `draw_info`，带着"为什么刷新状态栏不会闪屏"这个问题进入下一讲。
