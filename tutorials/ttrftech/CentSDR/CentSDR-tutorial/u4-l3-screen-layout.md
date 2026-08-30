# 屏幕信息架构：状态栏、频率与功率显示

## 1. 本讲目标

学完本讲，你应该能够：

1. 描述 `disp_process()` 如何用 `FLAG_UI` / `FLAG_POWER` / `FLAG_AUX_INFO` / `FLAG_SPDISP` 四个标志位把「谁想刷新」和「谁去刷新」解耦成生产者-消费者模型。
2. 读懂 `draw_freq()` / `draw_channel_freq()` 的大字号数字排版逻辑，并理解 `numfont20x24` / `numfont32x24` 两套字库如何靠 `font_t` 的 `scaley` 参数复用出第三种 32×48 渲染。
3. 说明 `draw_dbm()` 如何把一个 8.8 定点的功率值格式化成带一位小数的 dBm 字符串，以及负数时的取整技巧。
4. 建立「屏幕像素所有权」意识：屏幕上每一块像素归哪个绘制函数所有、以什么频率重画，是给固件加任何 UI 之前必须先搞清楚的事。

## 2. 前置知识

- **RGB565 与 fg/bg**：LCD 每个像素 16 位，`RGB565(b,g,r)` 宏（[nanosdr.h:L189](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L189)）把三原色打包。所有绘制函数都吃 `fg`（前景）/`bg`（背景）两个颜色参数，文字/数字是「不透明整块重画」——先算好每个像素该是 fg 还是 bg，一次性送往屏幕，因此重画本身不会闪烁。
- **8.8 定点**：用一个 16 位整数表示带小数的分贝值——高 8 位是整数部分，低 8 位是 1/256 精度的小数部分，即 x = d×256 + f。它出现在 `log2_q31()` 的返回值、`measured_power_dbm`、`draw_db()`/`draw_dbm()` 三处，是本讲功率计的数学主线。
- **脏标志（dirty flag）/ 增量刷新**：显示线程并不知道自己该画什么，它只检查一组标志位；任何代码（shell 命令、UI 线程、统计线程、DSP 回调）想刷新屏幕时只置位、不直接画。这样画屏永远发生在同一个线程上下文里，既避免 SPI 总线竞争，也天然实现了「按需局部重画」。
- **uistat 与 stat**：`uistat_t`（[nanosdr.h:L256-L274](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L256-L274)）是整机运行状态（频率、模式、音量、`fs` 采样率档位等），`stat_t`（[nanosdr.h:L26-L45](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L26-L45)）是测量统计量（RMS、fps、电池电压等）。本讲所有绘制函数的数据来源就是这两个全局结构体。
- **像素所有权**：320×240 的屏幕被纵向切成几条带，每条带固定归某个绘制函数所有（见 4.1.2 的分区表）。一个 UI 元素想稳定显示，它的像素必须归「与它同频重画」的函数所有——这是本讲综合实践要亲手验证的关键点。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
|---|---|
| display.c | 显示调度核心：标志位定义、`disp_process()`、频率排版、状态栏、功率计、辅助信息全部在此 |
| ili9341.c | 绘制原语：`ili9341_fill`、`ili9341_draw_bitmap`、`ili9341_drawfont(_string)` 与 `font_t` 字库实例 |
| numfont20x24.c / numfont32x24.c | 两套大字号位图字库数据（按行取模的 32 位字数组） |
| icons.c | 48×20 的调制模式图标与 AGC 图标位图 |
| nanosdr.h | `uistat_t`、`stat_t`、`font_t`、字库与绘制函数的声明 |
| main.c | 消费者线程 Thread2（调 `disp_process`）、生产者线程 Thread1（功率/统计）、`measure_power_dbm()` |

## 4. 核心概念与源码讲解

### 4.1 模块一：disp_process 的标志位驱动增量刷新

#### 4.1.1 概念说明

CentSDR 有四类代码想刷新屏幕：DSP 回调（新样本到了）、shell 命令（用户改了参数）、UI 状态机（用户拧了旋钮）、统计线程（功率/电池每秒变了）。如果它们各自直接调 LCD 绘制函数，SPI 总线会被多个线程/中断上下文争抢。本固件的做法是把这些「意图」压缩成一个 8 位变量 `spdispinfo.update_flag` 的四个位：

- `FLAG_SPDISP`（位 0）：样本缓冲攒满，该画频谱/瀑布/波形了（上一讲 u4-l1 的 `disp_fetch_samples()` 在 DSP 上下文置位）。
- `FLAG_POWER`（位 1）：功率测量更新，该画功率计了。
- `FLAG_UI`（位 2）：任何 UI 状态变化，整块静态界面重画。
- `FLAG_AUX_INFO`（位 3）：退出辅助调节模式，先把那一小块擦掉。

生产者只「按位或」置位（`|=`），消费者 `disp_process()` 检查某位、画完再「按位与」清零（`&= ~`。这是无锁的单写者思路：真正动手画屏的只有 Thread2 一个线程。

#### 4.1.2 核心流程

标志位定义与容器（display.c:L582-L596）：

```c
typedef struct {
	q31_t *buffer;
	uint32_t buffer_rest;
	uint8_t update_flag;
} spectrumdisplay_t;

// update_flag
#define FLAG_SPDISP 	(1<<0)
#define FLAG_POWER 		(1<<1)
#define FLAG_UI 		(1<<2)
#define FLAG_AUX_INFO	(1<<3)

spectrumdisplay_t spdispinfo;
```

消费者 `disp_process()` 的处理顺序（伪代码）：

```
若 FLAG_SPDISP:   画波形 → 画频谱 → 画瀑布        （高频，最快 ~10ms 一次）
若 FLAG_AUX_INFO: 擦除辅助信息区 (0,48,184,24)
若 FLAG_UI:       画频率刻度 → 画频率(或信道+频率) → 画状态栏(或辅助信息)
若 FLAG_POWER:    画功率计（仅当不在 RFGAIN 调节模式）
每位处理完立即清零
```

生产者入口三个薄封装（display.c:L1450-L1466）：

```
disp_update()        → update_flag |= FLAG_UI
disp_update_power()  → update_flag |= FLAG_POWER
disp_clear_aux()     → update_flag |= FLAG_AUX_INFO
```

**屏幕纵向分区与所有权表**（本讲全图的坐标系，务必记住）：

| y 范围 | 归属函数 | 重画时机 |
|---|---|---|
| 0–47 | `draw_freq()` / `draw_channel_freq()` | FLAG_UI |
| 48–71 | `draw_info()` / `draw_aux_info()`，x≥184 为 `draw_power()` | FLAG_UI / FLAG_POWER |
| 72–135 | `draw_spectrogram()`（无条件执行，任何 wfdispmode 下都画） | FLAG_SPDISP，最高约 100Hz |
| 136–151 | `draw_tick()` / `draw_tick_abs()` | FLAG_UI |
| 152–239 | `draw_waveform()` 或 `draw_waterfall()`（按 `wfdispmode` 二选一） | FLAG_SPDISP |

注意 `draw_spectrogram()` 内部没有任何 `wfdispmode` 判断——频谱带永远在画；是波形/瀑布带在切换。所以「音量行正下方」（y=72 起）其实是全屏重画频率最高的区域。

#### 4.1.3 源码精读

调度主循环：[display.c:L1411-L1448](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1411-L1448) —— `disp_process()` 按固定顺序检查四个标志位，每个分支处理完立刻清位；FLAG_UI 分支里还根据 `uistat.mode` 决定画常规状态栏还是辅助信息、画八位频率还是信道模式。

消费者线程：[main.c:L906-L924](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L906-L924) —— Thread2（`button`）每 10ms 调一次 `disp_process()` 和 `ui_process()`，并累加 `stat.fps_count`、检查编解码器溢出标志。全固件只有这里会真正执行绘制。

周期性生产者：[main.c:L22-L47](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L22-L47) —— Thread1（`blink`）每 100ms 做 `calc_stat` + `measure_power_dbm` + `disp_update_power()`（置 FLAG_POWER）；每第 10 次循环（约 1 秒）快照 fps/溢出计数、读 ADC（电池/温度/基准），再 `disp_update()`（置 FLAG_UI）。这也解释了辅助信息里 RMS/FPS/OVF 的刷新节奏约 1Hz。顺带一个阅读发现：`int count;`（main.c:L26）未初始化，首次自增依赖栈残留值，属于历史遗留的小瑕疵。

事件型生产者示例：[ui.c:L253-L305](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L253-L305) —— UI 状态机在 `uistat.mode` 档位切换和旋钮调值后调 `disp_update()`；离开 AGC_MAXGAIN/CWTONE/IQBAL 这些辅助档位时调 `disp_clear_aux()` 请求擦除。shell 命令（`tune`/`volume`/`agc`/`gain`）也是同样的套路（main.c:L93-L95、L552-L553）。

初始化：[display.c:L1468-L1475](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1468-L1475) —— `disp_init()` 初始化 1024 点 CFFT 实例、清全屏黑、把 `update_flag` 置成 `FLAG_UI`，让 Thread2 的第一轮循环完成首次全量绘制。

另外注意 [display.c:L582](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L582) 和 [display.c:L733](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L733) 的注释提到 "M4 core / M0APP"——这是从多核前代项目继承的痕迹，CentSDR 实际是单核上「中断 + 线程」的组合，读注释时要能分辨。

#### 4.1.4 代码实践：给刷新源分类

1. **实践目标**：把所有会触发屏幕刷新的代码点找全，并归入「周期 / 事件 / 初始化」三类，验证生产者-消费者模型。
2. **操作步骤**：
   - 在仓库根目录执行 `grep -n "disp_update\|disp_update_power\|disp_clear_aux" *.c`；
   - 对每个调用点阅读上下文，标注它运行在哪个线程/上下文（Thread1、Thread2、shell 线程、DSP 回调间接路径）；
   - 用 `grep -n "FLAG_" display.c` 对照四个标志位各自的生产者。
3. **需要观察的现象**：`disp_update()` 的调用点数量远超另外两个；没有任何绘制函数（`ili9341_*`）在 Thread2 之外被调用。
4. **预期结果**：得到一张「标志位 × 生产者 × 上下文」表格，例如 FLAG_POWER 只有 Thread1 一个生产者、FLAG_UI 有 shell 命令 + ui_process + Thread1 三个来源。
5. 有硬件时可再用 `python/centsdr.py` 连发 `volume 5`/`volume 6`，观察只有状态栏带变化、频谱不受影响（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `disp_update()` 置位用 `|=` 而不是直接赋值 `=`？
答案：`update_flag` 是多个生产者共享的位集合。若shell 命令刚置了 FLAG_UI、统计线程又想置 FLAG_POWER，直接赋值会抹掉对方的需求；按位或只会添加自己的位。清零则由唯一的消费者用 `&= ~FLAG_x` 完成。

**练习 2**：如果 Thread1 把 `disp_update()` 也放进 100ms 循环（而不是每秒一次），屏幕会有什么变化？
答案：静态 UI（频率、刻度、状态栏）会以 10Hz 全量重画，SPI 带宽被大量占用、波形/瀑布帧率下降，而且功率计本不需要这么频繁的 UI 重画——这正是「按需置位」设计要避免的。当前设计里 1 秒一次的 FLAG_UI 恰好服务于辅助信息中 RMS/FPS/OVF 的低频刷新。

**练习 3**：`spdispinfo.update_flag` 是 uint8_t，目前在 DSP 回调（中断上下文）与 Thread2 之间无锁共享，为什么没出问题？
答案：置位是对单个字节的原子「读-改-写」由指令序列近似保证（且 `|=` 编译为 read-modify-write，中断与线程的交错最坏情况只是丢一次置位或延迟一轮），清零只由消费者做、且每位只被自己的生产者置位；即使某次 FLAG_SPDISP 被延迟，下一个缓冲攒满会再次置位，属于「最终一致」的丢弃式同步——显示丢了帧没关系，DSP 绝不能等。

### 4.2 模块二：频率排版与两套大字号字库

#### 4.2.1 概念说明

频率是 SDR 接收机上最显眼的信息，CentSDR 用 32×48 像素的巨型数字铺满屏幕顶部一整行（y=0–47）。这里有两个要分开理解的东西：

- **排版逻辑**（display.c）：怎么把 `uistat.freq`（一个 uint32，单位 Hz）拆成 8 个字符、在哪些字符之间留「千分位」空隙、编辑焦点落在哪一位。
- **字库机制**（ili9341.c + numfont*.c）：位图数据长什么样、`font_t` 结构如何用几个参数让同一份位图渲染出 20×24、32×24、32×48 三种尺寸。

#### 4.2.2 核心流程

排版用的是 `itoap(value, buf, dig, pad)`（display.c:L1029-L1053）：把整数转成十进制字符串，右对齐定宽 `dig`，不足位用 `pad`（空格或 '0'）填充，负号尽量放在数字前。空格占位符在绘制时被当作「留空」处理。

`draw_freq()` 的排版算法：

```
itoap(uistat.freq, str, 8, ' ')        // 例：7100000 -> "  710000"
x = 0
for i in 0..7:
    c = str[i] - '0'
    若 c 合法:  画 32x48 数字 c 于 (x, 0)
    否则若该位是焦点: 画数字 0（用高亮色提示）
    否则:      用背景色填 32x48 矩形（擦除）
    x += 32
    若 i==1 或 i==4: 填 16px 宽空隙, x += 16   // xsim 表
最后画 "Hz" 符号（字形 10）于 x=288
```

宽度验算：8×32 + 2×16 = 288，加上 Hz 符号 32px 恰好 320——整行铺满，空隙正好出现在「MHz 位之后」和「kHz 位之后」，视觉上是千分位分隔。

字库机制：`font_t`（[nanosdr.h:L191-L198](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L191-L198)）有六个字段——`width/height` 是渲染尺寸，`slide` 是每个字形占多少个 32 位字，`stride` 是一「逻辑行」占几个字，`scaley` 是纵向放大倍数。三个实例（[ili9341.c:L282-L285](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L282-L285)）：

| 实例 | 参数 | 含义 |
|---|---|---|
| `NF20x24` | 20,24,1,24,1 | numfont20x24，每字形 24 个字 = 24 行 × 1 字 |
| `NF32x24` | 32,24,1,24,1 | numfont32x24，32px 宽一行 1 个字 |
| `NF32x48` | 32,48,**2**,24,1 | **同一份 numfont32x24 位图**，scaley=2 纵向加倍变 48 高 |

也就是说 32×48 的大数字并不需要单独一套位图——把 32×24 的每一行画两遍即可。这是「数据与渲染参数分离」带来的复用。

#### 4.2.3 源码精读

`draw_freq()`：[display.c:L1140-L1173](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1140-L1173) —— 八位数字排版、`xsim[]` 定义千分位空隙、焦点位判断 `uistat.mode == FREQ && uistat.digit == 7-i`（`digit` 从个位往左数，0~5）。焦点位数字用 `FG_ACTIVE`（绿色）高亮，前导空白位若恰为焦点则画一个高亮的 0 提示「这里可以拧」。

`draw_channel_freq()`：[display.c:L1175-L1215](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1175-L1215) —— 信道模式复用同一行：左边两位信道号用 NF20x24 画在 y=24（在 48px 高的行里垂直居中），中间空 52px，右边五位「kHz」频率用 NF32x48，最后画字形 11（kHz）和字形 10（Hz）。

字形渲染器 `ili9341_drawfont()`：[ili9341.c:L287-L308](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L287-L308) —— 按 `slide * ch` 定位字形位图，外层扫行、内层把每个 32 位字按 MSB→LSB 展开成 32 个像素写入 `spi_buffer`，`scaley` 是「每行重复写 j 次」的纵向放大，最后整块 DMA 送屏。

字符串包装 `ili9341_drawfont_string()`：[ili9341.c:L310-L327](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L310-L327) —— 只认数字、控制字符 1~6（映射到字形 10~15 的符号区）、'.'（字形 10）和 '-'（字形 11），其余字符用背景色填一块（等效空格）。

字库数据样例：[numfont20x24.c:L23-L51](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/numfont20x24.c#L23-L51) 与 [numfont32x24.c:L23-L40](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/numfont32x24.c#L23-L40) —— 每个字形是 24 个 32 位二进制字面量（`0b...` 直接写成 C 字面量），一行一个字、MSB 在最左像素。注意看每个字形最后两行全 0——字库自带 2px 的行间距，这就是后面实践中「状态栏底部有 2px 空白」的来源。字形并不只有数字：代码里用到了 10（小数点/Hz）、11（负号/kHz）、12（无穷，来自 "-∞" 音量显示）、13（dB）、14（喇叭）、15（天线）、22/23（d、Bm）等符号槽位（见 [nanosdr.h:L171-L174](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L171-L174) 的声明与 display.c 各调用点注释）。

#### 4.2.4 代码实践：在 PC 上复现排版坐标

1. **实践目标**：不靠硬件，验证你对 `draw_freq()` 排版算法的理解——给定频率算出每个字符的 x 坐标与内容。
2. **操作步骤**：把 `itoap()` 抄进一个 PC 端小程序（示例代码，纯标准 C）：

```c
/* 示例代码：PC 端复现 draw_freq 排版 */
void itoap(int value, char *buf, int dig, int pad) {
  char neg = 0;
  if (dig == 0) { sprintf(buf, "%d", value); return; }
  if (value < 0) { neg = '-'; value = -value; }
  buf[dig--] = '\0';
  do { buf[dig--] = (value % 10) + '0'; value /= 10; } while (value > 0 && dig >= 0);
  if (neg && dig >= 0) buf[dig--] = neg;
  while (dig >= 0) buf[dig--] = pad;
}
int main(void) {
  char str[10]; const int xsim[] = {0,16,0,0,16,0,0,0};
  int x = 0;
  itoap(7100000, str, 8, ' ');          /* "  710000" */
  for (int i = 0; i < 8; i++) {
    printf("i=%d char='%c' x=%d\\n", i, str[i], x);
    x += 32; if (xsim[i] > 0) x += xsim[i];
  }
  printf("Hz symbol at x=%d\\n", x);
  return 0;
}
```

3. **需要观察的现象**：输出里 i=0、i=1 是空格（对应 x=0、32 的两块被背景色填充），i=2 起是 '7','1','0','0','0','0'。
4. **预期结果**：'7' 在 x=80、'1' 在 x=128（i=2、i=3，中间隔了 i=1 之后的 16px 空隙），Hz 符号在 x=288，总宽 320。
5. 此为纯 PC 实验，可直接编译运行验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `draw_freq()` 里焦点位判断是 `uistat.digit == 7-i` 而不是 `uistat.digit == i`？
答案：`str` 从左到右是高位到低位（i=7 是个位），而 `uistat.digit` 的语义是个位为 0 往高位数（nanosdr.h 注释 `/* 0~5 */`）。旋钮调的「当前位」从右数，屏幕坐标从左数，所以要镜像：7-i。

**练习 2**：`draw_channel_freq()` 里信道号为什么画在 y=24 而频率画在 y=0？
答案：两种字号共享 48px 高的一行：频率用 NF32x48 占满 0–47；信道号是 NF20x24（高 24），画在 y=24 恰好落在行的下半段，视觉上与巨型频率基线对齐、又不会重叠——用垂直偏移实现混排。

**练习 3**：若新增第 9 位频率（支持 100MHz 以上），`draw_freq()` 需要改什么？放得下吗？
答案：需要 `itoap` 位数改 9、`xsim` 扩到 9 项、循环上界改 9。宽度 9×32+2×16 = 320，恰好铺满整行，但 "Hz" 符号就没有位置了——要么去掉千分位空隙（9×32=288，剩 32px 给 Hz），要么缩短符号，这是排版空间的取舍题。

### 4.3 模块三：状态栏 draw_info 与辅助信息 draw_aux_info

#### 4.3.1 概念说明

y=48–71 这条 24px 高的状态栏是整机的「仪表盘」，但它要表达的信息比面积多，于是固件用了两个手法：

1. **互斥复用**：常规档位显示 `draw_info()`（音量/模式/AGC/功率）；旋钮进入 AGC_MAXGAIN、CWTONE、IQBAL 三个「辅助调节」档位时，同一块区域切换为 `draw_aux_info()` 的 5×7 小字网格。`disp_process()` 按 `uistat.mode` 二选一（display.c:L1433-L1436）。
2. **颜色即焦点**：当前正在调节的元素用 `FG_ACTIVE`（绿色）高亮；`draw_aux_info()` 更进一步，把正在调的那一行前景/背景**反转**（fg=黑、bg=白），醒目地指出「现在拧旋钮改的是这一行」。

#### 4.3.2 核心流程

`draw_info()` 的横向布局（y=48，单位 px）：

```
[喇叭图标14][音量2位][dB] [调制图标48x20] [AGC图标48x20] [功率计区(见4.4)]
 0          20      60  82              134             184
```

音量特殊值 -7（负无穷，静音）不显示 "-7" 而是显示 "-∞"（字符串 `"-\003"`，控制字符 3 经 `drawfont_string` 映射到字形 12 的无穷符号）。

`draw_aux_info()` 的三列小字网格（每行 8px，5×7 字体）：

```
x=0                x=65         x=115
AGCMAX <6位数值>   BATT <5位>   RMS  <5位>     y=48
CWTONE <4位>Hz     TEMP <5位>   FPS  <5位>     y=56
IQBAL  <6位>       VREF <5位>   OVF  <5位>     y=64
```

三列分别是「可调节量」（当前调节行反色）、「片上 ADC 测量值」（Thread1 每秒刷新）、「运行统计」（同样每秒刷新）。数据来自 `config.agc.maximum_gain`、`uistat.cw_tone_freq`、`uistat.iqbal` 和 `stat` 结构体。

#### 4.3.3 源码精读

`draw_info()`：[display.c:L1258-L1297](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1258-L1297) —— 逐段画音量（含 "-∞" 分支）、调制图标（`uistat.modulation` 直接作字形索引，0~5 对应 CW/LSB/USB/AM/FM/立体声）、AGC 图标（`uistat.agcmode + ICON_AGC_OFF`，`ICON_AGC_OFF` 为 6，见 [nanosdr.h:L182](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L182)）；RFGAIN 档位时在 x=184 处改画射频增益编辑器（`draw_db(uistat.rfgain << 7, ...)`，每位增益 0.5dB 即 8.8 定点的 128）和天线图标，并按增益是否越界（<0 或 ≥96）换图标颜色。

`draw_aux_info()`：[display.c:L1299-L1384](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1299-L1384) —— 三列五行小字；每个可调节行开头都有 `if (uistat.mode == XXX) { fg = BG_NORMAL; bg = FG_NORMAL; }` 的反色逻辑；数值统一用 `itoap` 定宽输出，避免旧数字残留。

`clear_aux_info()`：[display.c:L1386-L1389](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1386-L1389) —— 退出辅助模式时只擦 x=0..183 这 184px 宽，因为 x≥184 属于功率计（`draw_power` 所有），擦多了会把功率计一起抹黑。这是「像素所有权」最直观的一处代码证据。

图标字库：[icons.c:L23-L47](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/icons.c#L23-L47) —— `icons48x20[][2*20]`，48px 宽的每行需要 2 个 32 位字，20 行共 40 字，与 `font_t` 实例 `ICON48x20 = {48,20,1,40,2,...}` 的 `slide=40, stride=2` 严格对应；槽位顺序就是 `modulation_t` 枚举顺序，之后接 AGC 图标组。

调度互斥：[display.c:L1427-L1438](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1427-L1438) —— FLAG_UI 分支中 `if (uistat.mode != AGC_MAXGAIN && != CWTONE && != IQBAL) draw_info(); else draw_aux_info();`，且 FLAG_AUX_INFO 的擦除分支排在 FLAG_UI 之前，保证「先擦后画」的顺序。

#### 4.3.4 代码实践：手工推导状态栏布局

1. **实践目标**：不看屏幕，仅凭源码算出状态栏每个元素的精确像素框，画出布局草图——这是后面任何 UI 改动的前置功课。
2. **操作步骤**：
   - 逐行读 `draw_info()`，跟着 `x += 20 / 40 / 20 / 48+4 / 48+4` 累加，记录每个元素的 (x, y, 宽, 高)；
   - 同样处理 `draw_aux_info()` 的三列；
   - 把结果画成一张 320×24 的方框图（纸或绘图软件均可）。
3. **需要观察的现象**：`draw_info()` 自己只用到 x=0..183（RFGAIN 档除外），x=184 起留给功率计；`draw_aux_info()` 三列分别止于约 x=60/110/160，全部 <184。
4. **预期结果**：一张标注了「喇叭 0–20、音量 20–60、dB 60–80、调制图标 82–130、AGC 图标 134–182、功率计 184–320」的草图，与 4.1.2 的所有权表一致。
5. 纯纸面推导，无需硬件；有硬件时可用屏幕对照（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`draw_info()` 里 `strcpy(str, "-\003")` 的 `\003` 是什么？
答案：八进制转义的控制字符 ETX（值为 3）。`ili9341_drawfont_string()` 把 1~6 的控制字符映射到字形 10~15（`c + 9`），所以 `\003` 取到字形 12——字库中预置的「∞」符号，用来表达音量 -7 = 负无穷（静音）。

**练习 2**：为什么退出辅助模式要走独立的 `FLAG_AUX_INFO`（先擦后画），而不是让 `draw_info()` 直接覆盖？
答案：`draw_aux_info()` 的 5×7 小字网格高度只有 24px 中的底部一带、宽度只到 x≈160，而 `draw_info()` 的图标和功率计之间有不少「不画」的空隙；直接覆盖会留下旧小字的残影。独立的擦除步骤用一块黑矩形兜底，成本只是一次 `ili9341_fill`。

**练习 3**：把 `draw_aux_info()` 三列的刷新频率说清楚——它们各自由谁更新、多久一次？
答案：三列都由 FLAG_UI 路径重画。可调节列的数值在用户拧旋钮时立即变（ui_process 置位）；BATT/TEMP/VREF 来自 Thread1 每秒一次的 `measure_adc()`；RMS/FPS/OVF 同样每秒随 Thread1 的 `disp_update()` 刷新（fps/overflow 本身也是 1 秒快照）。

### 4.4 模块四：功率计——8.8 定点、draw_db/draw_dbm 与 draw_power

#### 4.4.1 概念说明

屏幕右上角那块 184–320px 的区域是一个不断跳动的 dBm 功率计。它的数据链是：

```
Thread1(每100ms) → calc_stat() 算 rx_buffer 的 RMS
                 → measure_power_dbm() 换算成 8.8 定点分贝
                 → disp_update_power() 置 FLAG_POWER
Thread2(每10ms)  → disp_process() 看到 FLAG_POWER → draw_power() → draw_dbm()
```

「8.8 定点」是贯穿始终的数据格式：用 16 位整数的低 8 位表示小数，分辨率 1/256 dB，整数部分直接可读。全链路没有任何浮点 printf——格式化全靠整数位运算。

#### 4.4.2 核心流程

`measure_power_dbm()` 的换算公式（增益补偿）：

\[ P_{\mathrm{dBm}} = 6 \cdot \log_2(\mathrm{rms}) - 0.5\,g - 116 \]

其中 rms 是 `stat.rms[0]`（左声道全幅 RMS 的整数刻度），g 是生效的模拟增益（手动模式取 `uistat.rfgain`，AGC 模式改从编解码器读回实际增益）。代码里每一项都是 8.8 定点：`6 * log2_q31(...)`（`log2_q31` 返回的本身就是 8.8 格式的 log2 值，见 [display.c:L10-L58](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L10-L58)）；`agcgain << 7` 把「每位 0.5dB」换算成 8.8 的 128；`116 << 8` 是标定偏移。全式都乘了 256，所以减法可以直接在整数域完成。

`draw_dbm()` 把 8.8 值拆成「整数部分 + 一位小数」：

```
d = db >> 8                        // 算术右移取整数部分（负数向下取整）
若 d<0 且小数部分非 0: d++          // 修正为向零截断
整数部分: itoap(d, 4位, 空格补齐)
小数部分: 若 db<0, 小数 = 0x100 - (db & 0xff)   // 负数先取补
          一位小数 = (小数 * 10) >> 8            // 256 进制 → 10 进制
最后画 'd'、'Bm' 字形
```

负数补码技巧的直觉：-1.17dB 存成 0xFEB4。整数部分取 -2 再修正为 -1；小数字节 0xB4=180，对负数而言真正的「绝对值小数」是 256-180=76，76×10÷256≈2，于是显示 -1.2（截断）。

#### 4.4.3 源码精读

测量与换算：[main.c:L378-L393](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L378-L393) —— `measured_power_dbm` 是全局 int16_t（8.8 格式）；AGC 开启时增益从 `tlv320aic3204_get_left_agc_gain()` 读回，这样功率读数不会被 AGC 的自动增益「骗」掉。

绘制入口与区域守卫：[display.c:L1391-L1400](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1391-L1400) —— `draw_power()` 只在 `uistat.mode != RFGAIN` 时画。原因：RFGAIN 档位时 `draw_info()` 在同一块 x=184 区域画增益编辑器（见 4.3.3），两者共享像素，必须靠模式互斥防止互相覆盖。这就是「draw_power 的做法」：**独立标志位（FLAG_POWER，100ms 节奏）+ 共享区域时的模式守卫**。

dBm 格式化：[display.c:L1241-L1256](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1241-L1256) —— 先画小数点（字形 10，位置 x+80-6）、'd'（字形 22，x+96）、'Bm'（字形 23，x+116），整数 4 位右对齐，一位小数画在 x+100-14。

带符号变体 `draw_db()`：[display.c:L1222-L1239](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1222-L1239) —— 与 `draw_dbm()` 同构，单位是 "dB"（字形 13），被 RFGAIN 档的增益编辑器复用；结尾多一次 `ili9341_fill(x, y, 6, 32, bg)` 擦掉宽度变化时的残影。

同款逻辑的 shell 版：[main.c:L465-L466](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L465-L466) —— `power` 命令用 `measured_power_dbm >> 8` 和 `((measured_power_dbm&0xff) * 10) >> 8` 打印，与屏幕格式化完全一致，可用来在 PC 侧核对屏幕读数。

#### 4.4.4 代码实践：PC 端复现 8.8 格式化

1. **实践目标**：把 `draw_dbm()` 的整数运算格式化逻辑提取成 PC 程序，验证几个边界值的显示结果。
2. **操作步骤**（示例代码，纯标准 C）：

```c
/* 示例代码：复现 draw_db/draw_dbm 的取整与小数算法 */
void split88(int db, int *d_out, int *frac_out) {
  int d = db >> 8;
  if (d < 0 && (db & 0xff)) d++;            /* 向零截断 */
  *d_out = d;
  if (db < 0) db = 0x100 - (db & 0xff);     /* 负数小数取补 */
  *frac_out = ((db & 0xff) * 10) >> 8;      /* 1/256 -> 1/10 */
}
int main(void) {
  int vals[] = { 465, -300, -128, -256, 0 };
  for (int i = 0; i < 5; i++) {
    int d, f; split88(vals[i], &d, &f);
    printf("%5d -> %+d.%01d dB\\n", vals[i], d, f);
  }
  return 0;
}
```

3. **需要观察的现象**：465（即 +1.82dB）显示 +1.8；-300（-1.17dB）显示 -1.1；-256（-1.0dB）显示 -1.0；-128（-0.5dB）显示 0.5。
4. **预期结果**：与上面手算一致；特别注意 -128 的符号丢失——整数部分成了 0，屏幕上看不到负号，这是该算法对 (-1, 0) 区间值的边界行为。
5. 纯 PC 实验，编译即可验证；硬件上可用 `power` 命令对照（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `agcgain << 7` 对应 0.5dB？
答案：8.8 定点里 1dB = 256（即 1<<8）。0.5dB = 128 = 1<<7，所以每个增益单位左移 7 位就换算成 8.8 格式；`measure_power_dbm()` 的注释 `// 0.5dB/agcgain` 与 `draw_info()` 里 `draw_db(uistat.rfgain << 7, ...)` 用的是同一换算。

**练习 2**：功率计每 100ms 重画一次，为什么不会闪烁？
答案：`ili9341_drawfont_string()`/`drawfont()` 画的是不透明位图——每个字符的每个像素要么 fg 要么 bg，整块经 `spi_buffer` 一次性 DMA 送屏；没有「先擦黑再画」的两步过程，人眼看不到中间态。真正要防闪烁的是「清底+变宽/变窄」的组合，`draw_db()` 结尾那次 6px 宽的 fill 就是为这种情况擦残影的。

**练习 3**：AGC 开与关，功率计读数的含义有何不同？
答案：AGC 关闭（AGC_MANUAL）时补偿项用 `uistat.rfgain`（设定的静态增益）；AGC 开启时改用从编解码器实时读回的 `tlv320aic3204_get_left_agc_gain()`。如果两种情况都用设定值，AGC 一压增益，RMS 变小，读数会假性下降——读回实际增益才能让 dBm 反映天线口的真实功率。

## 5. 综合实践

**任务：给状态栏增加一行「当前采样率 fs（48/96/192kHz）」显示，并实现只在数值变化时重绘。**

背景动机：`uistat.fs` 不只由 `fs` 命令改变——`mode` 命令切换解调模式时也会带上该模式推荐的采样率（main.c:L184 `uistat.fs = mod_table[mod].fs;`），但屏幕上没有任何地方显示当前 fs，用户只能靠听带宽或 `stat` 命令猜。我们要把它画出来。

### 第 0 步：先做冲突分析（本实践最重要的一步）

对照 4.1.2 的所有权表：「音量行（y=48–71）正下方」是 y=72 起——归 `draw_spectrogram()` 所有，且该函数**没有** `wfdispmode` 守卫、每次 FLAG_SPDISP 都整块重画（包括背景），频率可达每秒几十帧。把静态文本放那里，会在下一次频谱重画时立刻被抹掉。再核对状态栏内部：24px 高的带子里，20/24 高的字库墨迹占到 y≈69，仅底部 2px 是字库自带的空白行——塞不下 5×7 文字。结论：**屏幕上不存在「免费」的整行像素**，任何新增 UI 都必须回答「这块像素归谁、跟谁同频重画」。

### 第 1 步：实现 draw_fs()（示例代码）

在 display.c 中新增（示例代码）：

```c
/* 示例代码：采样率行，只在数值变化时重绘 */
static int last_fs = -1;

void
draw_fs(void)
{
	char str[12];
	int x = 0;
	int y = 64;                 /* 状态栏带内底部一行(与aux网格同风格) */
	uint16_t bg = BG_NORMAL;
	if (uistat.fs == last_fs)
		return;             /* 数值未变，跳过全部绘制 */
	last_fs = uistat.fs;
	strcpy(str, "FS ");
	itoap(uistat.fs, str + 3, 3, ' ');
	strcat(str, "kHz");
	ili9341_fill(x, y, 45, 8, bg);          /* 先清底,防旧串残影 */
	ili9341_drawstring_5x7(str, x, y, FG_NORMAL, bg);
}
```

调用点放在 `disp_process()` 的 FLAG_UI 分支里、`draw_info()` 之后（示例代码）：

```c
	if (spdispinfo.update_flag & FLAG_UI) {
		...
		else
			draw_aux_info();
		draw_fs();              /* 新增 */
		spdispinfo.update_flag &= ~FLAG_UI;
	}
```

同时给 nanosdr.h 的 display.c 声明区补一行 `void draw_fs(void);`。

### 第 2 步：验证增量重绘逻辑

1. **实践目标**：新增 UI 元素 + 值缓存重绘 + 像素所有权分析，三件事一次做完。
2. **操作步骤**：
   - 按 u1-l2 的流程 `make` 编译；无硬件时至少保证编译通过、`arm-none-eabi-size` 确认增量可忽略；
   - 有硬件时烧录后依次执行 `fs 48`、`fs 96`、`fs 192`，再执行 `mode fm`（观察模式切换连带改 fs）；
   - 用 `python/centsdr.py -s` 读回状态交叉核对。
3. **需要观察的现象**：只有 fs 变化那一刻该行重画（值缓存生效）；`mode` 切换后该行随 FLAG_UI 自动更新；其余时间这 45×8 像素不再产生 SPI 流量。
4. **预期结果**：y=64 一行出现 "FS 192kHz" 之类的文本；因落点在状态栏带内，不与频谱区冲突。注意该落点与 `draw_aux_info()` 的 IQBAL 行(y=64, x=0..30)同位——进入辅助档位时会被 aux 网格覆盖、退出时被 `clear_aux_info()` 擦除并在下次 FLAG_UI 时重画，这正是需要你在实验里确认的边界行为；若介意，可把 `draw_fs()` 的调用挪到 `disp_process()` 中 FLAG_UI 分支的 `draw_aux_info()` 之后并保持现位，或改放 x=65 的 BATT 列下方并相应压缩 aux 网格。
5. 屏幕实际效果与边界行为：待本地验证。

### 第 3 步（选做，进阶）：把重绘判断做成真正的标志位

仿照 `FLAG_POWER` 的三件套（宏定义、`disp_update_fs()` 封装、`disp_process()` 分支），把 fs 变化提升为第五个标志位 `FLAG_FS`，并在 `fs`/`mode` 命令处调用 `disp_update_fs()`。对比两种方案：值缓存（本步之前）零接线、但初次全量重画依赖 FLAG_UI；标志位方案与既有架构完全一致、但要改三个文件。写出你倾向哪种的理由。

## 6. 本讲小结

- `disp_process()` 用 `spdispinfo.update_flag` 的四个位把「想刷新的代码」（shell/UI 线程/统计线程/DSP 回调）与「动手画屏的 Thread2」解耦；置位用 `|=`、清位用 `&= ~`，绘制只发生在单一线程上下文。
- 屏幕五条纵向带各有唯一所有者：频率行（FLAG_UI）、状态栏（FLAG_UI/FLAG_POWER）、频谱（FLAG_SPDISP，无模式守卫、永远在画）、刻度带（FLAG_UI）、波形/瀑布（FLAG_SPDISP，二选一）。
- `draw_freq()` 用 `itoap` 定宽排版 + `xsim[]` 千分位空隙铺满 320px；`font_t` 的 `scaley=2` 让 numfont32x24 一份位图渲染出 NF32x48，数据与渲染参数分离。
- 状态栏用「互斥复用 + 颜色即焦点」表达超量信息：常规档位 `draw_info()`，辅助调节档位切换为 `draw_aux_info()` 三列网格且当前调节行反色；`clear_aux_info()` 只擦自己拥有的 184px。
- 功率计是 8.8 定点一条龙：`measure_power_dbm()` 整数域换算（6×log2 − 0.5×增益 − 116），`draw_dbm()` 用移位与补码技巧拆出整数和一位小数，全程无浮点格式化。
- `draw_power()` 的 `uistat.mode != RFGAIN` 守卫展示了共享像素区域的互斥写法——给固件加 UI 前必须先回答「像素归谁、跟谁同频重画」。

## 7. 下一步学习建议

- 下一讲 u4-l4 转向输入侧：`ui.c` 的 `btn_check()` 消抖/双击/长按判定与正交编码器状态机如何产生本讲反复出现的 `disp_update()` 置位，把「生产者」的另一半补齐。
- 想巩固本讲的排版与格式化，可重读 `draw_tick_abs()`（[display.c:L1055-L1090](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1055-L1090)）——它把绝对频率换算成浮点像素坐标再逐格画刻度，是 `itoap` + 坐标计算的又一组合练习。
- 对功率链感兴趣可顺藤摸瓜读 `calc_stat()`（[main.c:L351-L376](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L351-L376) 的 RMS/min/max 统计）与 u2-l2 讲过的 AGC 增益读回，理解读数的误差来源。
- 单元五 u5-l1 会从 RTOS 视角重新审视本讲的 Thread1/Thread2 与 DSP 回调的优先级与数据竞争，届时可回看 4.1.5 练习 3 的无锁同步问题。
