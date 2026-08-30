# 时域之美：瀑布图与波形绘制

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `wfdispmode`（下方区域的渲染方式：瀑布/波形三变体）与 `spdispmode`（采样抓取点）这两个枚举各自管什么、由谁修改。
- 读懂 `colormap` 五段线性插值表和 `pick_color()` 的索引计算，并能手工算出任意强度级对应的 16 位像素值。
- 讲解 `draw_waterfall()` 如何把一次频谱快照变成一行像素、并用软件行指针 `vsa` 实现"滚动"。
- 讲解 `draw_waveform()` 如何用 `v2ypos()` 把 q31 样本值映射成 y 坐标，I/Q 两路分别成迹，以及相邻样本之间"半段连线"的补线技巧。
- 理解 `draw_waveform()`、`draw_spectrogram()`、`draw_waterfall()` 三者的调用顺序为什么决定了同一块缓冲区先当"时域"用、再当"频域"用。
- 理解 `spi_buffer` 这个 4096 像素公共缓冲如何约束所有绘图函数的块大小（`BLOCK_WIDTH=46` 的由来），以及 `ili9341_draw_bitmap()` 的 DMA 批量送屏与 `ili9341_fill()` 的 CPU 循环相比差在哪里。

本讲承接 u4-l1：上一讲我们搞清楚了"样本从哪里抓、怎么加窗升位、怎么做 1024 点 CFFT、怎么折算成 dB 刻度"。本讲回答剩下的问题——**FFT 之后（和之前）的数据如何变成屏幕下方的瀑布图与波形图**。

## 2. 前置知识

### 2.1 RGB565 像素格式

ILI9341 每个像素 16 位，按惯例高 5 位是红、中 6 位绿、低 5 位蓝（人眼对绿色更敏感，所以绿色多给一位）。固件用一个宏把 8 位的 R/G/B 打包成 16 位：

- [nanosdr.h:189](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L189) — `RGB565` 宏。**注意形参顺序是 `(b, g, r)`**：宏体内第一个参数（名叫 `b`）被 `>>3` 后放进低 5 位（0x001f 场），第三个参数（名叫 `r`）被 `<<8` 后放进高 5 位（0xf800 场）。

这个"参数顺序反直觉"的宏是本讲的重要伏笔，4.2 节会专门分析它带来的一个细节。

### 2.2 伪彩色（pseudocolor）

瀑布图把信号强度（0~63 共 64 级）映射成颜色。人眼对亮度的分辨只有二十多级，但对色相变化很敏感，所以把灰度"染色"成 黑→蓝→绿→红→白 一类的渐变，能一眼分辨出埋在底噪里的弱信号。做法很朴素：一张 5 个控制点的查表，相邻控制点之间线性插值。

### 2.3 对数刻度回顾

u4-l1 讲过 `log2_i64()` 返回 8.8 定点数，数值上近似 \(256 \log_2 x\)。功率每翻一倍（\(\log_2\) 域加 1）对应 \(10\lg 2 \approx 3.01\,\mathrm{dB}\)。本讲所有 dB 换算都用这个关系。

### 2.4 "一行瀑布图 = 一次频谱快照"

瀑布图本质上是把频谱图按时间堆叠：频谱图横轴频率、纵轴强度；瀑布图横轴仍是频率，纵轴变成了时间——每来一帧新数据画一行。本固件没有使用屏幕的硬件垂直滚动，而是用一个软件行指针逐行覆盖（详见 4.2 节，`waterfall_init()` 里保留着硬件滚动方案的 `#if 0` 遗迹）。

## 3. 本讲源码地图

| 文件 | 本讲关注的内容 |
| --- | --- |
| `display.c` | `disp_process()` 的三连绘制顺序、`colormap`/`pick_color()`、`draw_waterfall()`、`v2ypos()`/`draw_waveform()`、`BLOCK_WIDTH` 分块、`mag_shift` 全局变量 |
| `ili9341.c` | `spi_buffer` 公共缓冲、`ili9341_fill()` 的 CPU 逐像素循环、`ili9341_draw_bitmap()` 的 DMA 批量传输 |
| `nanosdr.h` | `RGB565` 宏、`uistat_t` 中的 `spdispmode`/`wfdispmode` 枚举、`spi_buffer` 声明 |
| `ui.c`、`main.c`、`dsp.c` | 两个显示模式枚举由编码器在哪里切换、`disp_process()` 由哪个线程驱动、样本抓取钩子在哪里（回顾） |

## 4. 核心概念与源码讲解

### 4.1 一块缓冲区的三次生命：disp_process 的固定绘制顺序

#### 4.1.1 概念说明

u4-l1 讲过：解调回调（I2S 中断上下文）里 `disp_fetch_samples()` 顺路把加窗后的 q31 样本攒进 `SPDISP_BUFFER`，攒满 2048 个 q31（= 1024 个复数样本）后置起 `FLAG_SPDISP` 标志。显示线程（Thread2）每 10ms 调一次 `disp_process()`，看到标志后就依次调用三个绘制函数，最后清标志。

关键在于：**三个函数读的是同一块缓冲区，而 `draw_spectrogram()` 里的 FFT 是原地变换**。所以同一块缓冲区在一帧之内先后经历两种身份：

- `draw_waveform()` 第一个跑，读到的还是**时域样本**——所以它能画波形；
- `draw_spectrogram()` 第二个跑，把缓冲区原地变换成**频域复数 bin**——上方 64 像素高的柱状谱；
- `draw_waterfall()` 第三个跑，读到的是**FFT 之后的频域数据**——所以它按 bin 分组累加功率，每个 bin 组算出一个颜色，画成瀑布的一行。

也就是说，瀑布图显示的根本不是"波形的时间历程"，而是"频谱的时间历程"。这个顺序如果被打乱，两个图会立刻互相污染。

#### 4.1.2 核心流程

```text
I2S 回调（中断）                        Thread2（显示线程，每 10ms）
────────────────────                    ──────────────────────────────
dsp.c 各解调函数                          disp_process()
  └─ disp_fetch_samples()                  └─ 若 FLAG_SPDISP 置位：
       按 uistat.spdispmode 对齐                 ① draw_waveform()   ← 读时域样本
       抓样本入 SPDISP_BUFFER                     ② draw_spectrogram() ← 原地 FFT + 画柱状谱
       攒满 2048 个 q31 →                        ③ draw_waterfall()  ← 读频域 bin，画 1 行
       置 FLAG_SPDISP                             ④ 清 FLAG_SPDISP
```

#### 4.1.3 源码精读

- [display.c:1413-1420](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1413-L1420) — `disp_process()` 里 `FLAG_SPDISP` 分支：按 波形→频谱→瀑布 的固定顺序调用三个绘制函数，最后清标志。这个顺序就是本节的核心。
- [display.c:783](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L783) — `draw_spectrogram()` 开头的 `arm_cfft_radix4_q31(&cfft_inst, buf)`：对 `spdispinfo.buffer`（即 `SPDISP_BUFFER`）做原地 FFT，是缓冲区从时域变频域的"分水岭"。
- [display.c:768-772](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L768-L772) — `disp_fetch_samples()` 尾部：缓冲攒满后记录 `spdispinfo.buffer` 并置 `FLAG_SPDISP`，生产者一侧的交接点。
- [main.c:906-916](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L906-L916) — Thread2 主循环：`disp_process()` 与 `ui_process()` 每 10ms 一轮，`stat.fps_count++` 顺带统计帧率。
- [dsp.c:355-384](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L355-L384) — 回顾：Weaver 解调器里 `B_CAPTURE`/`B_IF1`/`B_IF2`/`B_PLAYBACK` 四个抓取钩子（u4-l1 已详述），决定 `SPDISP_BUFFER` 里装的是什么信号。

另外注意两个"互斥开关"：`draw_waveform()` 开头若发现 `wfdispmode == WATERFALL` 直接返回，`draw_waterfall()` 开头若发现 `wfdispmode != WATERFALL` 也直接返回。所以下方那块 88 像素高的区域（y=152~239）**同一时刻只归二者之一所有**，而上方的频谱图始终绘制。

#### 4.1.4 代码实践

1. **实践目标**：验证"调用顺序 = 缓冲区身份"这一结论。
2. **操作步骤**：在自己的副本里做一个思想实验（不必真改）：假设把 `draw_waterfall()` 挪到 `draw_spectrogram()` 之前，先在纸上写出你预测的画面变化；如果本地有构建环境，可以真改一处再 `make` 烧录对比。
3. **需要观察的现象**：瀑布图是否还呈现"频率轴"结构。
4. **预期结果**：瀑布图会读到时域样本——每个颜色列变成 3 个相邻时域样本的功率和，画面退化为随时间抖动的噪声条，失去按频率排列的谱线；波形图不受影响（它本来就在 FFT 之前）。真机效果**待本地验证**。
5. 改完记得还原——这只是理解顺序重要性的实验。

#### 4.1.5 小练习与答案

1. **问**：`FLAG_SPDISP` 由谁置位、由谁清零？
   **答**：I2S 解调回调里的 `disp_fetch_samples()` 攒满缓冲后置位（display.c:768-772，中断上下文）；Thread2 的 `disp_process()` 画完三个函数后清零（display.c:1419）。这就是 u4-l1 说的"忙则丢帧、绝不阻塞解调"的无锁生产者-消费者。
2. **问**：为什么 `draw_waveform()` 必须第一个执行？
   **答**：它需要时域样本，而 `draw_spectrogram()` 的 CFFT 是原地变换，跑完之后时域数据就被频域数据覆盖了。
3. **问**：`uistat.spdispmode` 和 `uistat.wfdispmode` 分别控制什么？
   **答**：`spdispmode`（nanosdr.h:270）决定**抓哪里的样本**（原始 IQ / 一混频后 / 滤波后 / 输出音频），同时影响频谱、波形、瀑布三者；`wfdispmode`（nanosdr.h:271）只决定**下方 88 像素区域怎么渲染**（瀑布 / 波形 / 放大波形 / 再放大波形）。二者在 UI 的 SPDISP、WFDISP 两个调节档下分别由编码器循环切换（[ui.c:346-350](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L346-L350)）。

### 4.2 瀑布图 draw_waterfall：逐行滚动与 64 级伪彩色

#### 4.2.1 概念说明

`draw_waterfall()` 每帧只画**一行 320 个像素**：对 FFT 后的 1024 个 bin 按 `stride` 个一组累加功率、取对数折算成 0~63 的强度级，再经 `pick_color()` 查伪彩色表得到一个 16 位颜色。写完一行后行指针 `vsa` 下移一行，写满下方区域 88 行后回到顶部覆盖最老的一行——"滚动"就是这么来的。

对比一下频谱图的刻度（u4-l1）：`draw_spectrogram()` 用 `(log2_i64(acc) - (36<<8)) / 77`，约 1dB/像素；瀑布图用 `(log2_i64(acc) - (34<<8)) >> 6`，基准低 2 个 \(\log_2\) 单位、每级跨度更细。换算成 dB：

\[ \text{每级} = 2^{6/256 \times 256} \to \text{8.8 域中一级} = 64 = 0.25\,\log_2 \text{单位} = 10\lg 2^{0.25} \approx 0.75\,\mathrm{dB} \]

64 级满量程即 \(2^{16}\approx 65536\) 倍功率，约 48dB。基准点：累加值 \(acc = 2^{34}\) 时强度为 0。

#### 4.2.2 核心流程

```text
draw_waterfall()
  ├─ 若 wfdispmode != WATERFALL：直接返回
  ├─ 按 spdispmode 取几何参数（offset 起始 bin、stride 每 bin 组大小）
  ├─ for x in 0..319:                     # 屏幕一列
  │    acc = Σ (I²+Q²) over stride 个 bin  # int64 累加，负索引用 &1023 回卷
  │    v = (log2_i64(acc) - 34*256) >> 6    # 0..63 强度级
  │    c = pick_color(v)
  │    if c == 0: c = bg                   # 黑色替换为背景色
  │    spi_buffer[x] = c
  ├─ vsa++（152→239 循环回 152）
  └─ ili9341_draw_bitmap(0, vsa, 320, 1, spi_buffer)   # 一行 DMA 送屏
```

#### 4.2.3 源码精读

- [display.c:833-839](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L833-L839) — `colormap`：5 个 RGB 控制点，黑→蓝→绿→红→白。
- [display.c:841-851](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L841-L851) — `pick_color(mag)`，输入 0~63：
  - `idx = (mag >> 4) & 0x3`：高 2 位选出 4 个插值段之一（0~3）；
  - `prop = mag & 0x0f`：低 4 位是段内位置（0~15），`nprop = 16 - prop` 是互补权重；
  - 三通道各自做 \(c = c_{idx} \cdot nprop + c_{idx+1} \cdot prop\)，最后 `>>4` 除以 16 归一。`mag=16` 时 `idx=1, prop=0`，结果恰好等于 `colormap[1]`，所以段与段之间严格连续。
- [display.c:978-1027](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L978-L1027) — `draw_waterfall()` 主体。注意 [display.c:993-994](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L993-L994) 的互斥返回、[display.c:1000-1003](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1000-L1003) 的逐 bin 组功率累加（与频谱图同一套 `(i&1023)` 回卷手法）、[display.c:1008](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1008) 的对数刻度、[display.c:1011-1014](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1011-L1014) 的取色与黑色替换、[display.c:1018-1026](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1018-L1026) 的行指针推进与单行送屏。
- [display.c:853-864](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L853-L864) — `waterfall_init()`：整段 `#if 0`。里面是 ILI9341 的垂直滚动区域定义命令（VSCRDEF, 0x33），以及 `draw_waterfall()` 里 `#if 0` 的 VSCSAD（0x37）滚动地址写入——作者曾尝试用屏幕硬件滚动实现"整屏上移、新行固定"，最终改用软件行指针。弃用的具体原因代码中没有说明（待确认）。
- 关于 `bg` 的细节：`bg = uistat.mode == WFDISP ? BG_ACTIVE : BG_NORMAL`（display.c:990）。`BG_ACTIVE` 是 `RGB565(15,10,10)` 的暗红色，当 UI 焦点在 WFDISP 档时，瀑布图的"静默黑"被替换成暗红底，提示当前正在调节这个区域。

**一个值得仔细读的细节：RGB565 宏的参数顺序与 pick_color 的通道互换。**
`RGB565(b,g,r)` 宏把**第三个参数**放进高 5 位（0xf800 场），**第一个参数**放进低 5 位（0x001f 场）。而 `pick_color()` 调用的是 `RGB565(r>>4, g>>4, b>>4)`——红色强度进了第一个参数（低 5 位场），蓝色强度进了第三个参数（高 5 位场），**R 与 B 在位域上互换了**。这是纯算术事实，从宏定义即可复现。但它在屏幕上的"观感"还取决于面板的 RGB/BGR 配置：初始化序列写入 `MADCTL=0x28`（[ili9341.c:152](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L152)），其 bit3 按 ILI9341 数据手册是 BGR 滤色片顺序选择，若面板确为 BGR 型，这次互换恰好被面板顺序抵消，屏幕上仍是注释所写的黑→蓝→绿→红→白。这解释了为什么这类"看起来像 bug"的代码能长期正常工作。本讲综合实践里我们会在 PC 上同时渲染"位域原样"和"通道修正"两个版本，把这件事看清楚。

#### 4.2.4 代码实践

1. **实践目标**：把瀑布图的色阶刻度看成一个可调参数，体会基准与级差的意义。
2. **操作步骤**：在 display.c:1008 把 `(34<<8)` 改成 `(38<<8)`（基准抬高 4 个 \(\log_2\) 单位，约 12dB），重新编译烧录；在瀑布模式下观察强弱信号的颜色变化。
3. **需要观察的现象**：整幅瀑布图颜色整体"变冷"（向黑端压缩），原来绿色的中等信号掉到蓝区，只有强信号还能爬到红/白区。
4. **预期结果**：与推导一致——基准每抬高 1，画面动态范围向下压缩约 0.75dB；抬高 4 约压 12dB。真机效果**待本地验证**。

#### 4.2.5 小练习与答案

1. **问**：手工计算 `pick_color(20)`。
   **答**：`idx = (20>>4)&3 = 1`，`prop = 20&15 = 4`，`nprop = 12`。段 1 是 {0,0,255}，段 2 是 {0,255,0}：\(r = 0\)，\(g = (0\times12 + 255\times4)>>4 = 63\)，\(b = (255\times12 + 0\times4)>>4 = 191\)。最终调用 `RGB565(0, 63, 191)`——即 63 的绿色强度进绿场、191 的蓝色强度进高位场（见 4.2.3 的通道互换讨论）。
2. **问**：瀑布图一级色阶对应多少 dB？整个 64 级覆盖多少？
   **答**：一级 = 8.8 域的 64 = 0.25 个 \(\log_2\) 单位 ≈ 0.75dB；64 级 = \(2^{16}\) ≈ 48.2dB。
3. **问**：`vsa` 推进到 239 之后会发生什么？
   **答**：回到 152（display.c:1018-1020），下一行覆盖 88 帧之前画的最老一行。所以某一行像素会在屏幕上原样保留约 88 帧。

### 4.3 波形图 draw_waveform：v2ypos 坐标映射与 IQ 双迹示波器

#### 4.3.1 概念说明

`draw_waveform()` 把下方 88 像素高的区域当成一台**双迹示波器**：I 路样本画一条迹线，Q 路画另一条，零电平在区域正中。屏幕每列 x 显示一个复数样本——具体说，是 1024 个样本缓冲里**居中的 320 个**（第 352~671 号，即 `(512-160)` 到 `(512+160-1)`）。左右各让出 160 个样本，是为了画相邻样本间的连线时数组不越界。

三个渲染变体由 `wfdispmode` 区分（WAVEFORM / WAVEFORM_MAG / WAVEFORM_MAG2）。有趣的是，"放大"并不发生在绘制端，而是在**抓取端**：`draw_waveform()` 只设置全局变量 `mag_shift`（3 或 6），真正的 `<< mag_shift` 发生在 I2S 回调里的 `window_*_15to31()` 家族中——下一次缓冲填充时样本被左移 3 或 6 位，即整体放大 \(2^3=8\) 倍或 \(2^6=64\) 倍。这是一个跨执行上下文的全局量：写者在 Thread2，读者在中断，好在取值只有 0/3/6 三种且写坏一帧也无碍，属于良性竞争（并发话题在 u5-l1 展开）。

#### 4.3.2 核心流程

```text
v2ypos(v):  v 是 q31 样本
  v >>= 24            # 取高 8 位（带符号），分辨率 2^24
  v += HEIGHT/2 (44)  # 零电平居中
  钳位到 [0, 87]

draw_waveform():
  ├─ 若 wfdispmode == WATERFALL：返回
  ├─ 按 wfdispmode 设 mag_shift = 6 / 3 / 0
  ├─ 预取 4 个初始 y：i0,q0（样本 351）、i1,q1（样本 352）
  └─ for x = 0..319，按 46 列一块分批：
       i2,q2 = 下一列样本的 y
       imin/imax 由 {i1, (i0+i1)/2, (i1+i2)/2} 取最小/最大   # 半段连线
       for y = 0..87:
         c = 背景黑
         y == 44        → 中线（fg）
         x % 48 == 0    → 1ms 时刻刻度线（按 48kHz 标定）
         y 落在 I 迹范围 → |= 黄(255,255,0)
         y 落在 Q 迹范围 → |= 品红(255,0,255)
         spi_buffer[y*w + i] = c
       ili9341_draw_bitmap(xx, 152, w, 88, spi_buffer)   # 一块 DMA 送屏
```

#### 4.3.3 源码精读

- [display.c:876-884](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L876-L884) — `v2ypos()`：`>>24` 后加 `HEIGHT/2` 再双向钳位。上方注释 `FS=+-44` 与随后的 `+-352`、`+-2816` 正是 44 的 8 倍与 64 倍——满幅信号在 MAG/MAG2 视图下必然削顶，放大的真正目的是把底噪里的小信号抬进可见范围。
- [display.c:898-923](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L898-L923) — `draw_waveform()` 开头：WATERFALL 早退、`mag_shift` 三选一（913-918）、四个初始 y 值的预取（920-923，索引 `(512-160)*2±…` 展开后是元素下标 702~705）。
- [display.c:926-973](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L926-L973) — 主循环。931-946 的 min/max 扩展是"半段连线"技巧：像素 x 只画自己负责的半段（从 (x-1,x) 中点经 x 到 (x,x+1) 中点），相邻像素各画一半，拼出完整线段——既保证迹线连续，又不会重复绘制。948-962 逐像素填 `spi_buffer`：中线、48 像素间隔的 1ms 刻度（48kHz 采样下 48 个样本 = 1ms，320 列 ≈ 6.67ms 窗口；采样率改变时刻度不再准确）、I 迹与 Q 迹用 `|=` 叠加颜色。971 行 `ili9341_draw_bitmap(xx, 152, w, HEIGHT, spi_buffer)` 每块一次 DMA。
- [display.c:533](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L533) 与 [display.c:649-652](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L649-L652) — `mag_shift` 的声明与它在 `window_complex_15to31()` 里的消费点（`<< mag_shift`），放大发生在抓取端的实证。
- [display.c:886-894](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L886-L894) — `inrange()`：一个当前无人调用的辅助函数（全文件检索仅有定义），疑似旧版连线逻辑的遗留。同理 [display.c:866](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L866) 定义的 `YPOS` 也未被引用（代码里用的是字面量 152）。读源码时能识别"死代码"同样是重要能力。
- 数据来源提醒：无论 `spdispmode` 选哪一档，波形图读的都是同一块 `SPDISP_BUFFER`。选 `SPDISP_AUD`（音频输出，实信号）时，抓取函数 `window_real_15to31()` 会把 Q 位置零（display.c:665-673 写入 `I,0,I,0`），于是屏幕上 Q 迹是一条贴着中线的直线——这是检验"实信号模式 Q 恒为零"的最直观方式。

#### 4.3.4 代码实践

1. **实践目标**：用 MAG/MAG2 视图观察放大发生在"下一次缓冲填充"而非当帧。
2. **操作步骤**：在 UI 的 WFDISP 档下旋转编码器，依次切到 WAVEFORM → WAVEFORM_MAG → WAVEFORM_MAG2（[ui.c:348-349](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L348-L349) 的循环切换；shell 没有直接设置 `wfdispmode` 的命令）。观察切换瞬间的第一帧。
3. **需要观察的现象**：切换瞬间画面是否立即变化；强信号在放大视图里是否削顶成"矩形"；底噪是否从看不见变成满屏毛刺。
4. **预期结果**：第一帧仍是旧倍率（`mag_shift` 要等 I2S 回调下一次填缓冲才生效），下一帧起才放大 ±8/±64 倍；强信号削顶、底噪可见。切换瞬间的"迟一帧"**待本地验证**。

#### 4.3.5 小练习与答案

1. **问**：一个幅度约 \(2^{30}\)（接近 q31 满幅）的样本会画在哪里？幅度 \(2^{29}\) 呢？
   **答**：\(2^{30} \gg 24 = 64\)，加 44 得 108，钳位到 87（顶/底削平）；\(2^{29}\gg24 = 32\)，加 44 得 76，在屏内。可见 ±44 像素（半屏）对应幅度 \(44 \times 2^{24} \approx 0.69 \times 2^{30}\)——加窗后的典型满幅正弦恰好占满 88 像素，这就是注释 `FS=+-44` 的含义。
2. **问**：为什么连线用 `(i0+i1)/2`、`(i1+i2)/2` 的中点，而不是直接把 i0~i2 整段涂满？
   **答**：每个像素只负责自己左右各半段，相邻像素的半段首尾相接拼成完整折线；如果每列都画到前后样本的全值，同一段线会被相邻两列重复绘制，且斜率呈现为"阶梯过冲"。半段法是示波器绘制的经典折中。
3. **问**：`x % 48 == 0` 的刻度线在什么前提下才是 1ms 一条？
   **答**：采样率 48kHz 的前提下（48 个样本 = 1ms）。若 `uistat.fs` 为 96/192kHz，或 `spdispmode` 选了抽取后的 IF 缓冲（每回调只有 len/2 个样本，等效采样率减半），刻度间隔就不再是 1ms——代码按 48kHz 硬编码标定。

### 4.4 spi_buffer 分块送屏：BLOCK_WIDTH=46 的由来与性能账本

#### 4.4.1 概念说明

u2-l4 讲过：所有绘图函数共享 [ili9341.c:31](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L31) 的 `uint16_t spi_buffer[4096]`——4096 个像素（8KB）。凡是"先在内存里拼好一块位图、再一次性送屏"的绘制，单块面积都不能超过这个容量。波形图高 88 像素，于是每块宽度上限是 \(\lfloor 4096/88 \rfloor = 46\)——这就是 `BLOCK_WIDTH=46` 的全部来历。320 列除以 46 得 6.96，所以整幅波形要分 7 块（前 6 块各 46 列，最后一块 44 列）。

#### 4.4.2 核心流程

```text
拼图式绘制（以 draw_waveform 为例）：
  for 每一块 (宽 w ≤ 46, 高 88):
    ① CPU 逐像素计算颜色 → 写 spi_buffer（二维按 [y*w+i] 列优先）
    ② ili9341_draw_bitmap(x0, 152, w, 88, spi_buffer)
         CASET(0x2A)/PASET(0x2B) 设窗口 → RAMWR(0x2C)
         → DMA 内存→SPI，MINC 自动递增，一次传 w*88 像素
         → dmaWaitCompletion 等待完成
  对比 ili9341_fill()：同一窗口命令后，CPU 循环
  while (len--) ssp_senddata16(color);   # 每个像素都忙等 FIFO 槽位
```

#### 4.4.3 源码精读

- [display.c:866-870](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L866-L870) — `YPOS/HEIGHT/BLOCK_WIDTH` 定义及注释：46×88=4048 像素 < 4096，320/46≈6.96 → 7 块。
- [ili9341.c:225-235](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L225-L235) — `ili9341_fill()`：设窗口后 CPU 逐像素忙等发送。适合小面积清理；大面积（如 `clear_background()` 整屏 320×240=76800 像素）时每个像素都要轮询 FIFO，是全固件最慢的送屏路径。
- [ili9341.c:237-252](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L237-L252) — `ili9341_draw_bitmap()`：同样的窗口命令后，配置 DMA（内存地址、传输数、`MINC` 内存自增）一次性把整块位图灌进 SPI FIFO。CPU 不再参与逐像素搬运，只需在 `dmaWaitCompletion()` 处等 DMA 收尾。
- [ili9341.c:40-62](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L40-L62) — `ssp_wait_slot()`/`ssp_senddata16()`：`fill` 路径逐像素轮询的底层实现，对照即可看出 DMA 的意义。
- 每帧 SPI 像素量账本（下方区域 + 始终重绘的 64 行频谱）：
  - 瀑布模式：`draw_waterfall` 320 像素（1 行）+ `draw_spectrogram` 320×64=20480 像素（10 块 32×64）≈ **2.1 万像素/帧**；
  - 波形模式：`draw_waveform` 320×88=28160 像素（7 块）+ 频谱 20480 ≈ **4.9 万像素/帧**。
  像素量差一倍以上，这就是切换到瀑布模式后 `stat` 命令的 `fps` 读数通常变高的原因。

#### 4.4.4 代码实践

1. **实践目标**：用帧率数据验证上面的性能账本。
2. **操作步骤**：保持信号与模式不变，分别在 WFDISP 档的瀑布与波形两种视图下，用 u1-l4 介绍过的 `stat` 命令（或 python/centsdr.py 脚本周期发送）连续读取 20 次 `fps`。
3. **需要观察的现象**：两种视图的 fps 差值；顺带观察 `load`（DSP 负载）是否同时变化。
4. **预期结果**：瀑布模式 fps 明显更高；`load` 基本不变（送屏开销在显示线程，不在 I2S 中断的 DSP 回调里）。无硬件时此项**待本地验证**。

#### 4.4.5 小练习与答案

1. **问**：为什么 `BLOCK_WIDTH` 是 46 而不是 47 或 64？
   **答**：约束是 `w × 88 ≤ 4096`（spi_buffer 容量），\(4096/88 = 46.5\)，向下取整 46；47×88=4136 已溢出。取 64 则需要 64×88=5632 的缓冲。
2. **问**：320 列波形要几次 DMA 传输？各多宽？
   **答**：7 次——6 次 46 列加最后 1 次 44 列（320 − 6×46 = 44），由 display.c:927-929 的收尾调整处理。
3. **问**：`clear_background()`（display.c:1402-1409）为什么慢？
   **答**：它用 `ili9341_fill()` 逐条清除 24 个 320×10 条带，共 76800 像素全部走 CPU 忙等路径，没有任何 DMA。改用"内存里预清一块 spi_buffer 再 draw_bitmap"会快得多——这是读者可以自己动手的优化点。

## 5. 综合实践：给瀑布图换一张"黑→蓝→白"色表，并在 PC 上预览

这个任务把本讲三条线索串起来：`colormap` 的五段插值结构、`pick_color()` 的索引计算（以及那个 R/B 位域互换）、伪彩色刻度与 64 级强度的对应关系。

### 5.1 改固件色表（保留 5 个插值点）

把 [display.c:833-839](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L833-L833) 的色表换成黑→深蓝→纯蓝→浅蓝→白的单调蓝通道渐变：

```c
/* 示例代码：替换 display.c 中的 colormap */
const struct { uint8_t r,g,b; } colormap[] = {
		{   0,   0,   0 },
		{   0,   0, 128 },
		{   0,   0, 255 },
		{ 128, 160, 255 },
		{ 255, 255, 255 }
};
```

`make` 重新编译（构建链路见 u1-l2），有硬件则烧录后进入瀑布模式观察。

### 5.2 在 PC 上等价复刻 pick_color

下面这个 Python 3 脚本（示例代码，不依赖项目里的 python/centsdr.py）逐行复刻固件算法，包括 `>>4` 权重归一和 `RGB565` 的参数顺序，然后输出一张 PPM 渐变测试图：第一行是 64 级灰度基准条，第二行是"按位域原样渲染"的新色表效果，第三行是"交换 R/B 通道修正后"的效果。

```python
#!/usr/bin/env python3
# pick_color_preview.py —— 输出 pick_color_preview.ppm（示例代码）
COLORMAP = [(0,0,0), (0,0,128), (0,0,255), (128,160,255), (255,255,255)]

def rgb565(b, g, r):                     # 形参顺序与固件宏一致：(b, g, r)
    return (((r) << 8) & 0xf800) | (((g) << 3) & 0x07e0) | (((b) >> 3) & 0x001f)

def pick_color(mag):                     # mag: 0-63
    idx, prop = (mag >> 4) & 0x3, mag & 0x0f
    nprop = 0x10 - prop
    c0, c1 = COLORMAP[idx], COLORMAP[idx+1]
    r = (c0[0]*nprop + c1[0]*prop) >> 4
    g = (c0[1]*nprop + c1[1]*prop) >> 4
    b = (c0[2]*nprop + c1[2]*prop) >> 4
    return rgb565(r, g, b)               # 与固件同样把 r,g,b 传进 (b,g,r)

def unpack_std(pix):                     # 按标准 RGB565 约定拆回 8 位 RGB
    return ((pix >> 8) & 0xf8, (pix >> 3) & 0xfc, (pix << 3) & 0xf8)

CELL, CW, BARS = 8, 16, 3                # 每级 8px 宽，每条色带 16 行高，共 3 条
W, H = 64*CELL, BARS*CW                  # 512 x 48

gray = [(m*4, m*4, m*4) for m in range(64)]            # ① 64 级灰度基准
raw  = [unpack_std(pick_color(m)) for m in range(64)]  # ② 位域原样渲染
fix  = [(r,g,b)[::-1] for (r,g,b) in raw]              # ③ R/B 互换修正后

def bar(palette):                        # 把 64 级横向铺成一整行像素
    row = []
    for c in palette: row += [c] * CELL
    return row

palettes = [gray, raw, fix]
img = b""
for y in range(H):
    line = bar(palettes[y // CW])
    img += bytes(v for p in line for v in p)
with open("pick_color_preview.ppm", "wb") as f:
    f.write(b"P6\n%d %d\n255\n" % (W, H))
    f.write(img)
print("written pick_color_preview.ppm", W, "x", H)
```

说明：脚本刻意保留与固件完全相同的调用方向 `rgb565(r, g, b)`，因此"②位域原样"一行在标准 RGB565 解释下会呈现红/蓝对调的颜色，"③修正"一行才是直觉上的黑→蓝→白。用 GIMP 或 `feh` 打开 PPM 查看。

### 5.3 验证要点与预期结果

1. `mag=16/32/48` 三处应分别精确等于色表的第 2/3/4 控制点颜色（`prop=0` 时插值退化为查表）——验证插值连续性。
2. ②与③两行颜色红蓝互补——这就是 4.2.3 分析的"通道互换"在 PC 上的可视化。
3. 烧录后真机瀑布图应呈现蓝调渐变；若屏幕上看到的是"黑→偏红→白"，说明面板并非 BGR 型，把色表里每个控制点的 `r` 与 `b` 字段对调即可补偿（**待本地验证**）。
4. 强弱信号在 64 级中的分布仍由 4.2 的 0.75dB/级刻度决定，换色表不改变刻度，只改变观感。

## 6. 本讲小结

- `disp_process()` 按 **波形→频谱→瀑布** 的固定顺序绘制：`draw_spectrogram()` 的原地 CFFT 是分水岭，波形图读它之前的时域样本，瀑布图读它之后的频域 bin——顺序即数据身份。
- 瀑布图每帧只画一行 320 像素：`stride` 个 bin 功率累加 → `(log2_i64(acc) − 34·256) >> 6` 得 0~63 强度级（约 0.75dB/级，全程约 48dB）→ `pick_color()` 查五段插值色表；软件行指针 `vsa` 在 152~239 间循环覆盖实现滚动（硬件滚动方案以 `#if 0` 保留）。
- `pick_color()` 用 `idx=(mag>>4)&3` 选段、`prop=mag&15` 做段内线性插值，段间严格连续；`RGB565` 宏形参顺序是 `(b,g,r)`，而调用传入 `(r,g,b)`，R/B 在位域上互换——观感是否正常取决于面板 RGB/BGR 配置。
- 波形图是 IQ 双迹示波器：`v2ypos()` 把 q31 右移 24 位加 44 居中钳位，显示 1024 样本缓冲居中的 320 个；半段中点连线避免重复绘制；WAVEFORM_MAG/MAG2 的放大实际发生在抓取端（`window_*()` 里的 `<< mag_shift`）。
- 所有块状绘制共享 4096 像素的 `spi_buffer`，`BLOCK_WIDTH = ⌊4096/88⌋ = 46`，320 列分 7 块 DMA 送屏；瀑布模式每帧 SPI 像素量约为波形模式的一半，fps 更高。

## 7. 下一步学习建议

本讲把"下方区域"的两条渲染路径讲完了。下一讲 **u4-l3 屏幕信息架构** 转向屏幕上方的固定 UI：`FLAG_UI/FLAG_POWER/FLAG_AUX_INFO` 标志位驱动的增量刷新、`draw_freq` 的大字号数字排版、`draw_dbm` 的 8.8 定点功率格式化。如果你想先动手，建议：

- 用 5.2 的 Python 脚本继续实验：把色表改成"热成像"（黑→紫→红→黄→白）观察观感差异；
- 回读 [display.c:779-831](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L779-L831) 的 `draw_spectrogram()`，对照本讲的功率累加代码，找出它与瀑布图共用的部分与刻度常数的差异；
- 提前浏览 [ui.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c)，看看 `uistat.mode` 的 WFDISP 档是如何与 `wfdispmode` 联动的（u4-l4 将完整拆解这台"一只旋钮走天下"的状态机）。
