# u4-l4 markmap 脏矩形重绘机制

## 1. 本讲目标

学完本讲,你应该能够:

1. 说清 NanoVNA 为什么必须做"局部重绘":16KB SRAM 放不下帧缓冲,只能立即模式渲染,于是用 16 字节的 `markmap` 位图来管理 320×240 屏幕上"哪些格子需要重画"。
2. 解释双页交替(`markmap[2]` + `current_mappage`)如何让"旧轨迹被擦除、新轨迹被画出"在一次重绘中同时完成,以及为什么只有完整扫完一帧才允许 flush。
3. 掌握 `REDRAW_*` 六个标志位构成的"请求-响应"刷新模型:谁置位、`draw_all()` 如何按位分发、频率/校准/电池三个覆盖层各自的刷新时机。
4. 理解 `trace_index[]` 坐标缓存与 `draw_cell()` 以格子为单位的渲染流水线设计。

本讲是第 4 单元显示子系统的"调度层":u4-l1 讲了"像素怎么上屏"(SPI/DMA/字体),u4-l2 讲了"轨迹坐标怎么算"(12 种格式),本讲回答"什么时候、重画哪一块"。

## 2. 前置知识

**帧缓冲 vs 立即模式渲染。** 桌面程序通常先把整屏像素画在内存里的"帧缓冲"(framebuffer),再一次性送显。NanoVNA 的 STM32F072 只有 16KB SRAM,而一块 320×240×16bit 的帧缓冲需要:

\[ 320 \times 240 \times 2\,\text{B} = 153600\,\text{B} \approx 150\,\text{KB} \]

物理上不可能。所以固件采用**立即模式渲染**:没有整屏像素备份,每次重画都从"数据"(measured 数组 + 配置)重新生成像素,直接推给 LCD。u4-l1 已经建立过这个前提——一切绘制都被拆成"设窗口→连续写"的小块传输,全局唯一的像素画布是 2048 像素的 `spi_buffer`。

**脏矩形(dirty rectangle)。** 立即模式的代价是"重画必须从头生成像素",如果每帧都全屏重画,代价太高(源码注释里全屏 Smith 网格逐像素分类就要约 1000 个系统 tick)。但观察实际画面:连续扫频时,变化的只有轨迹线附近的一小片区域,网格、边框、菜单大多不动。"脏矩形"就是把屏幕划成小块,只记录并重画**内容发生变化的块**——"脏"意味着"过期,需要刷新"。

**位图(bitmap)作集合。** 用一个整数的每一位表示一个格子是否脏,置位用 `|=`,判位用 `& (1<<x)`。这是嵌入式里最省内存的集合表示法。

**请求-响应模型。** 承接 u2-l5 的结论:NanoVNA 的 UI 输入在中断里只"举旗"(置标志位),真正的处理延迟到 sweep 线程统一消化。显示刷新走同一条路:任何代码都可以置位 `redraw_request` 里的某个 `REDRAW_*` 位"下订单",`draw_all()` 是唯一消费者,处理完统一清零。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
|---|---|
| `plot.c` | 主角。markmap 数据结构、标记原语、`draw_cell` 渲染流水线、`draw_all_cells`/`draw_all` 调度、频率/校准/电池三个覆盖层 |
| `main.c` | `Thread1` 中扫频→`plot_into_index`→`draw_all` 的调用时序;`redraw_request` 定义;各处 `force_set_markmap` 触发点 |
| `nanovna.h` | `REDRAW_*` 六个标志位定义、`WIDTH/HEIGHT/AREA_*` 屏幕几何常量、`SPI_BUFFER_SIZE` |
| `ili9341.c` | `spi_buffer` 定义与 `ili9341_bulk`——格子渲染的最后一步(DMA 上屏) |
| `ui.c` | 菜单/键盘打开时收缩绘图区(`area_width/area_height`),关闭时请求恢复被遮挡的格子 |

## 4. 核心概念与源码讲解

### 4.1 markmap 脏标记:用 16 字节管理一块 320×240 的屏幕

#### 4.1.1 概念说明

markmap 解决的问题是:**在没有帧缓冲的立即模式渲染下,如何用最少的内存和传输量,把"上一帧到这一帧之间屏幕上变化了的部分"精确圈出来。**

思路是把屏幕划分成大小相同的"格子"(cell),再用一张位数组记录每个格子的新旧状态。格子的尺寸不是随便选的:它恰好等于 `spi_buffer` 的容量——64×32 = 2048 像素,一格的像素正好装满一次 DMA 传输的画布。这样"重画一个格子"就变成"清空 spi_buffer → 在里面重新生成内容 → `ili9341_bulk` 一次性上屏"的固定流程。

屏幕按 320×240、格子按 64×32 划分,得到:

\[ \lceil 320/64 \rceil \times \lceil 240/32 \rceil = 5 \times 8 = 40 \text{ 个格子} \]

markmap 本体是 `markmap[2][8]`,即**两张** 5 位有效的位图,共 16 字节——用 16 字节 SRAM 管理 150KB 屏幕的脏信息。为什么是两张,是 4.2 节的主题。

#### 4.1.2 核心流程

标记原语的分工:

```text
mark_map(x, y)          置位单个格子(坐标是格子索引,不是像素)
invalidate_rect(x0,y0,x1,y1)  把一个像素矩形覆盖到的所有格子置位
force_set_markmap()     全部置 0xff(整屏脏)
clear_markmap()         清空当前页
swap_markmap()          current_mappage ^= 1,双页交替
```

像素坐标 → 格子索引的换算就是整除:`格子x = 像素x / 64`,`格子y = 像素y / 32`。

往 markmap 里"写脏"的 callers 有五类:

1. `mark_cells_from_index()`——新轨迹折线经过的格子(扫频的核心路径);
2. `markmap_all_markers()`——marker 图标(7×10 像素)压住的格子 + 顶部读数区;
3. `update_grid()`——扫描范围变了,横轴网格间距要重算,直接 `force_set_markmap()`;
4. `set_trace_type/scale/refpos/channel`、`set_electrical_delay`——轨迹的显示参数变了,整屏重画;
5. `request_to_draw_cells_behind_menu()`——菜单关闭后,被菜单盖住的那片区域要恢复。

#### 4.1.3 源码精读

格子的尺寸由 spi_buffer 反推得出,并带编译期断言([plot.c:37-47](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L37-L47)):

```c
pixel_t *cell_buffer = (pixel_t *)spi_buffer;   // 格子渲染直接复用 spi_buffer
#define CELLWIDTH  (64)
#define CELLHEIGHT (32)
#if CELLWIDTH*CELLHEIGHT > SPI_BUFFER_SIZE      // 64*32 = 2048,恰好等于
#error "Too small spi_buffer size ..."          // SPI_BUFFER_SIZE(2048)
#endif
```

`cell_buffer` 就是 `spi_buffer` 的别名——plot.c 的格子渲染和 ili9341.c 的驱动共享同一块画布,这是 u4-l1 建立的约定。

markmap 的类型定义与位宽自适应([plot.c:49-62](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L49-L62)):

```c
// indicate dirty cells (not redraw if cell data not changed)
#define MAX_MARKMAP_X    ((320+CELLWIDTH-1)/CELLWIDTH)   // = 5
#define MAX_MARKMAP_Y    ((240+CELLHEIGHT-1)/CELLHEIGHT) // = 8
#if MAX_MARKMAP_X <= 8
typedef uint8_t map_t;      // 5 列 → 8 位足够,一行一个字节
#elif ...
map_t   markmap[2][MAX_MARKMAP_Y];   // 两页,每页 8 字节
uint8_t current_mappage = 0;
```

注意 `MAX_MARKMAP_*` 按整屏 320×240 计算,而实际循环按 `area_width/area_height`(见 4.3.3),菜单弹出时区域收缩,列数会小于 5。

四个基础原语([plot.c:796-819](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L796-L819)):

```c
static inline void mark_map(int x, int y) {
  if (y >= 0 && y < MAX_MARKMAP_Y && x >= 0 && x < MAX_MARKMAP_X)
    markmap[current_mappage][y] |= 1 << x;    // 写的是"当前页"
}
static inline void swap_markmap(void) { current_mappage ^= 1; }
static void clear_markmap(void)  { memset(markmap[current_mappage], 0, ...); }
void force_set_markmap(void)     { memset(markmap[current_mappage], 0xff, ...); }
```

矩形失效:整除即得格子区间([plot.c:821-832](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L821-L832)):

```c
void invalidate_rect(int x0, int y0, int x1, int y1) {
  x0 /= CELLWIDTH;  x1 /= CELLWIDTH;
  y0 /= CELLHEIGHT; y1 /= CELLHEIGHT;
  for (y = y0; y <= y1; y++)
    for (x = x0; x <= x1; x++)
      mark_map(x, y);
}
```

轨迹如何"写脏":`mark_cells_from_index` 遍历缓存坐标,对相邻两点跨到的格子矩形整体置位([plot.c:836-861](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L836-L861)):

```c
for (i = 1; i < sweep_points; i++) {
  int m1 = CELL_X(index[i]) / CELLWIDTH;
  int n1 = CELL_Y(index[i]) / CELLHEIGHT;
  if (m0 == m1 && n0 == n1) continue;      // 两点同格,无需新标记
  int x0 = m0; int x1 = m1; if (x0>x1) SWAP(x0, x1);
  int y0 = n0; int y1 = n1; if (y0>y1) SWAP(y0, y1);
  for (; y0 <= y1; y0++)
    for (j = x0; j <= x1; j++)
      map[y0] |= 1 << j;                   // 两点包围盒内的格子全标脏
}
```

为什么标"包围盒"而不是只标两端点所在格子?因为 Bresenham 画线是逐像素走的,一条从格子 (0,0) 斜穿到 (2,2) 的线段,中间会路过 (1,1) 的像素;若 (1,1) 上没有别的采样点落在里面,它就不会被标记,下次重画时会漏掉这段线。用包围盒保证线段路径上的格子全部入册。

marker 图标写脏:取图标左上角(轨迹点减偏移),按图标外接矩形失效([plot.c:1044-1058](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1044-L1058));`markmap_all_markers` 再补上顶部读数区([plot.c:1060-1070](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1060-L1070)):

```c
static inline void markmap_upperarea(void) {
  invalidate_rect(0, 0, AREA_WIDTH_NORMAL, 31);   // 顶部两行文字区,硬编码
}
```

配置类触发点以 `set_trace_scale` 为例——任何影响轨迹形状的参数变化都直接整屏标脏([main.c:1611-1617](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1611-L1617)):

```c
void set_trace_scale(int t, float scale) {
  if (trace[t].scale != scale) {
    trace[t].scale = scale;
    force_set_markmap();       // 老轨迹可能出现在任何位置 → 全屏重画
  }
}
```

#### 4.1.4 代码实践

用 Python 复刻格子网格与三个标记原语,直观看到"一条轨迹在 5×8 位图上留下了什么"。

**实践目标**:验证 `mark_map`/`invalidate_rect`/`mark_cells_from_index` 的语义,并亲眼看到轨迹移动前后脏格子的分布。

**操作步骤**(示例代码,在 PC 上另存为 `markmap_sim.py` 运行):

```python
# 示例代码:复刻 plot.c 的 markmap 标记原语
CELLW, CELLH = 64, 32
MX = (320 + CELLW - 1) // CELLW    # 5
MY = (240 + CELLH - 1) // CELLH    # 8

class Markmap:
    def __init__(self):
        self.page = [0] * MY            # 当前页:每行一个位掩码
    def mark(self, x, y):               # mark_map()
        if 0 <= x < MX and 0 <= y < MY:
            self.page[y] |= 1 << x
    def invalidate_rect(self, x0, y0, x1, y1):
        for y in range(y0 // CELLH, y1 // CELLH + 1):
            for x in range(x0 // CELLW, x1 // CELLW + 1):
                self.mark(x, y)
    def force(self):                    # force_set_markmap()
        self.page = [0xFF] * MY
    def show(self, title):
        print(title)
        for row in self.page:
            print(''.join('#' if row & (1 << x) else '.' for x in range(MX)))

def mark_cells_from_index(mm, pts):     # pts: 像素坐标列表 [(x,y),...]
    m0, n0 = pts[0][0] // CELLW, pts[0][1] // CELLH
    mm.mark(m0, n0)
    for i in range(1, len(pts)):
        m1, n1 = pts[i][0] // CELLW, pts[i][1] // CELLH
        if (m0, n0) == (m1, n1):
            continue
        for y in range(min(n0, n1), max(n0, n1) + 1):
            for x in range(min(m0, m1), max(m0, m1) + 1):
                mm.mark(x, y)
        m0, n0 = m1, n1

mm = Markmap()
# 模拟一条 LOGMAG 轨迹:101 个点,横跨全屏、纵向上凸
pts_old = [(5 + i * 3, 200 - int(150 * (i / 100.0) ** 2)) for i in range(101)]
mark_cells_from_index(mm, pts_old)
mm.invalidate_rect(0, 0, 320, 31)       # 顶部 marker 读数区
mm.show("旧轨迹 + 顶部读数区的脏格子:")
```

**需要观察的现象**:打印出的 8 行×5 列点阵中,轨迹上凸的形状如何在位图上"量化"成格子;顶部两行(读数区)整行变 `#`。

**预期结果**:轨迹扫过第 0~4 列、纵跨多行的格子被标脏;由于轨迹是曲线,某些中间格子因"包围盒填充"也被标上。若把 `pts_old` 换成一条只走对角线的两点折线 `[(5,5),(300,230)]`,会看到对角线上的格子被整块标脏——这正是 4.1.3 分析的包围盒效果。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `CELLWIDTH×CELLHEIGHT` 必须小于等于 `SPI_BUFFER_SIZE`?把格子改成 128×32 会发生什么?

**答案**:一个格子的像素在渲染时全部驻留在 `cell_buffer`(即 `spi_buffer`)里,最后由 `ili9341_bulk` 一次性 DMA 上屏。128×32 = 4096 像素 > 2048,画布装不下,`#if` 断言会直接报编译错误;若去掉断言,写画布越界会破坏其他数据(该缓冲还被 capture、时域变换等复用,见 u5-l3)。反过来,格子也不能太小——格子数增多则 markmap 变大、每格的 bulk 传输次数与固定开销上升。

**练习 2**:`markmap` 为什么选 `uint8_t` 而不是 `uint32_t` 数组?

**答案**:`MAX_MARKMAP_X = 5`,5 个有效位,`uint8_t` 恰好容纳,两页共 \(2 \times 8 = 16\) 字节。类型由 `#if MAX_MARKMAP_X <= 8` 编译期自适应,若未来屏幕加宽到 9~16 列会自动换成 `uint16_t`。这是"位宽刚好够用"的极致省内存写法。

**练习 3**:`invalidate_rect(250, 0, 319, 239)` 会标脏哪些格子?

**答案**:`250/64 = 3`、`319/64 = 4`,`0/32 = 0`、`239/32 = 7`,即第 3、4 列全部 8 行。这正是 `request_to_draw_cells_behind_menu()` 恢复右侧 70 像素宽菜单遮挡区时的调用效果(见 [plot.c:1446-1452](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1446-L1452))。

---

### 4.2 draw_all 与双页交替:旧轨迹是怎么被擦掉的

#### 4.2.1 概念说明

立即模式渲染有个绕不开的问题:**擦除**。轨迹移动后,旧位置上的像素必须消失,但屏幕上没有"旧帧"可查,固件也不能从 LCD 把像素读回来重画(LCD 读回慢且占缓冲)。NanoVNA 的解法优雅而廉价:

- 每个格子被重画时,内容是从头生成的——先铺背景色,再画网格,再画**当前** `trace_index` 缓存的轨迹线。所以"擦除旧轨迹"不需要知道旧像素在哪,只需要保证"旧轨迹压过的格子"和"新轨迹压过的格子"**都**被重画。
- 这正是 markmap 有两页的原因:**当前页**累积"这一帧要画的格子"(新轨迹),**另一页**保留"上一帧画过的格子"(旧轨迹)。重画判定取两页的**并集**,一次遍历同时完成"画新"与"擦旧"。
- 一帧完整画完后 flush:交换两页(`current_mappage ^= 1`),再清空新的当前页。刚画完的那页标记自动成为下一轮的"旧轨迹记录"。

配套的还有 `trace_index[]`:101 个频点的像素坐标在 `plot_into_index()` 里一次算好,画格子、放 marker、极值搜索全都复用,把"浮点换算"与"像素绘制"彻底分离。

#### 4.2.2 核心流程

一次完整扫频帧的显示流水线(Thread1 循环内):

```text
sweep(true) 完整扫完 → completed = true
    │
    ├─ plot_into_index(measured)
    │     ├─ trace_into_index(): 101 点复数 Γ → 像素坐标,打包进 trace_index[t][]
    │     ├─ mark_cells_from_index(): 新轨迹格子 → 写脏【当前页】
    │     └─ markmap_all_markers(): marker 图标 + 顶部读数区 → 写脏【当前页】
    │
    ├─ redraw_request |= REDRAW_CELLS | REDRAW_BATTERY
    │
    └─ draw_all(flush = completed)
          └─ draw_all_cells(flush):
                对 40 个格子: if (markmap[0][n] | markmap[1][n]) & (1<<m) → draw_cell
                if (flush): swap_markmap(); clear_markmap();
```

若 sweep 被 UI 操作打断(`operation_requested` 且 `break_on_operation`),`sweep()` 提前返回 `false`,`plot_into_index` **不会**执行(trace_index 仍是上一帧坐标),`draw_all(false)` 也不 flush——当前页标记继续累积。这样从上一次完整帧以来"画过的一切"都不会被遗忘,直到下一帧完整数据到来。

#### 4.2.3 源码精读

坐标缓存与打包宏([plot.c:64-72](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L64-L72)):

```c
//   CELL_X[16:31] x position
//   CELL_Y[ 0:15] y position
typedef uint32_t index_t;
static index_t trace_index[TRACES_MAX][POINTS_COUNT];   // 4 条轨迹 × 101 点
#define INDEX(x, y) ((((index_t)x)<<16)|(((index_t)y)))
#define CELL_X(i)  (int)(((i)>>16))
#define CELL_Y(i)  (int)(((i)&0xFFFF))
```

`plot_into_index` 只做两件事:重算全部坐标、按新坐标写脏([plot.c:1191-1211](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1191-L1211)):

```c
void plot_into_index(float measured[2][POINTS_COUNT][2]) {
  for (t = 0; t < TRACES_MAX; t++) {
    if (!trace[t].enabled) continue;
    for (i = 0; i < sweep_points; i++)
      index[i] = trace_into_index(t, i, measured[ch]);   // u4-l2 讲过的换算
  }
  mark_cells_from_index();    // 新轨迹格子 → 当前页
  markmap_all_markers();      // marker + 顶部区 → 当前页
}
```

调用时序在 Thread1 主循环里([main.c:129-147](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L129-L147)),注意 `draw_all` 的实参就是 `completed`:

```c
if (sweep_mode & SWEEP_ENABLE && completed) {
  if ((domain_mode & DOMAIN_MODE) == DOMAIN_TIME) transform_domain();
  // Prepare draw graphics, cache all lines, mark screen cells for redraw
  plot_into_index(measured);
  redraw_request |= REDRAW_CELLS | REDRAW_BATTERY;
  ...
}
// plot trace and other indications as raster
draw_all(completed);  // flush markmap only if scan completed to prevent
                      // remaining traces
```

`draw_all_cells` 是机制的心脏:判定条件取**两页并集**,画完后按需 flush([plot.c:1386-1407](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1386-L1407)):

```c
static void draw_all_cells(bool flush_markmap) {
  for (m = 0; m < (area_width+CELLWIDTH-1) / CELLWIDTH; m++)
    for (n = 0; n < (area_height+CELLHEIGHT-1) / CELLHEIGHT; n++) {
      if ((markmap[0][n] | markmap[1][n]) & (1 << m))   // 两页并集
        draw_cell(m, n);
    }
  if (flush_markmap) {
    swap_markmap();     // 当前页变历史页(记录"本帧画过哪里")
    clear_markmap();    // 清空新的当前页,开始积累下一帧
  }
}
```

`draw_cell` 的渲染流水线([plot.c:1213-1384](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1213-L1384)),六个阶段顺序固定:

```text
① 裁剪:格子右/下边超出 area_width/area_height 的部分截掉
② 清背景:32 位直写一次填 4 个 uint32(源码注释:比 memset 快 10 倍,
   350 ticks → 35 ticks)
③ 画网格:按启用轨迹的类型掩码选矩形/Smith/Polar 网格,逐像素判定
④ 画轨迹:矩形图先用 search_index_range_x 二分出落在本格 x 区间内的
   采样点(注释:省 50~70 ticks),Smith/Polar 则检查全部 101 点;
   cell_drawline 用 |= 把线色"或"进格子(与网格色混合)
⑤ 画 marker 图标与顶部读数(n==0 的格子顺带 cell_draw_marker_info)、参考位置小三角
⑥ 若被裁剪则行内紧凑,最后 ili9341_bulk DMA 上屏
```

其中二分查找点区间的实现([plot.c:900-938](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L900-L938)):矩形图的 trace_index 按 x 递增排列,先二分找到一个落在格子内的点,再向两侧扩散到边界外一点,使跨格线段的首尾都能画上:

```c
// Give a little speedup then draw rectangular plot
static int search_index_range_x(int x1, int x2, index_t index[], int *i0, int *i1)
{
  // 二分找格子内一点,再 do-while 向左/向右扩展到 [i0, i1]
}
```

`cell_drawline` 是"格子内"的 Bresenham,每个像素都做边界检查,用 `|=` 叠色([plot.c:873-896](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L873-L896)):

```c
while (1) {
  if (y0 >= 0 && y0 < CELLHEIGHT && x0 >= 0 && x0 < CELLWIDTH)
    cell_buffer[y0 * CELLWIDTH + x0] |= c;    // 轨迹色与网格色按位或
  ...
}
```

上屏的最后一步是 u4-l1 讲过的 bulk 传输([ili9341.c:457-471](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L457-L471)):设窗口三条命令后,DMA 从 `spi_buffer` 搬 `w*h` 个半字——一个格子恰好 2048 像素一次搬完。

初始化时 `plot_init()` 只做一件事:全屏标脏,保证开机后第一次 `draw_all` 把整块绘图区画出来([plot.c:1733-1737](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1733-L1737))。

#### 4.2.4 代码实践

**实践目标**:在 Python 里完整模拟双页交替,亲手制造"轨迹移动后旧像素如何被清除"的场景,并验证"单页会留残影、双页不会"。

**操作步骤**(示例代码,接 4.1.4 的 `Markmap` 类):

```python
# 示例代码:双页 markmap + 逐格重生成,验证旧轨迹擦除
CELLS = {}                     # (mx,my) -> 该格当前"亮着"的像素集合
def draw_cell(mx, my, pts, w=310, h=233):
    """重生成一个格子:背景 + 当前轨迹落在本格的像素(真实固件还有网格/marker)"""
    x0p, y0p = mx * CELLW, my * CELLH
    lit = set()
    for (px, py) in bresenham_polyline(pts):        # 当前轨迹的全部像素
        if x0p <= px < x0p + CELLW and y0p <= py < y0p + CELLH \
           and px < w and py < h:
            lit.add((px, py))
    CELLS[(mx, my)] = lit                           # 整格覆盖 = 旧像素自然消失

def draw_all_cells(mm, flush, pts):
    n_drawn = 0
    for n in range(MY):
        for m in range(MX):
            if (mm.page[n] | mm.old[n]) & (1 << m): # 两页并集 → 重画
                draw_cell(m, n, pts); n_drawn += 1
    if flush:
        mm.old, mm.page = mm.page, [0] * MY         # swap + clear
    return n_drawn

def bresenham_polyline(pts):                        # 简化:用直线插值代替
    out = []
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        steps = max(abs(x1-x0), abs(y1-y0), 1)
        out += [(round(x0+(x1-x0)*t/steps), round(y0+(y1-y0)*t/steps))
                for t in range(steps+1)]
    return out

# ---- 场景:第 1 帧旧轨迹 → flush → 第 2 帧轨迹下移 → 观察残影 ----
mm = Markmap(); mm.old = [0] * MY; mm.page = [0] * MY
mm.force()                                          # 开机:全屏脏
pts_old = [(5 + i*3, 60 + int(20 * (i/50.0 - 1)**2)) for i in range(101)]
print("帧1 重画格子数:", draw_all_cells(mm, flush=True, pts=pts_old))

pts_new = [(p[0], p[1] + 60) for p in pts_old]      # 轨迹整体下移 60px
mark_cells_from_index(mm, pts_new)                  # 只标新轨迹格子
print("帧2 重画格子数:", draw_all_cells(mm, flush=True, pts=pts_new))

stale = []                                          # 检查残影:旧像素还在吗?
for (mx, my), lit in CELLS.items():
    for (px, py) in lit:
        if (px, py) not in bresenham_polyline(pts_new):
            stale.append((px, py))
print("残影像素数:", len(stale))
```

(需给 `Markmap.__init__` 增加 `self.old = [0]*MY`,并把 4.1.4 的 `force` 改为只清/置 `self.page`。)

**需要观察的现象**:帧 2 只重画了新旧轨迹并集覆盖的格子;`stale` 列表为空——旧轨迹的像素全部消失,尽管代码从未"擦"过任何东西。

**预期结果**:残影像素数为 0。随后做个对照实验:`draw_all_cells` 里把判定条件改成只看当前页 `mm.page[n] & (1<<m)`(模拟单页),帧 2 后 `stale` 会大于 0——旧轨迹上方那段像素无人重画,残影出现。这从反面证明了双页并集的必要性。

#### 4.2.5 小练习与答案

**练习 1**:`draw_all(completed)` 的注释说 "flush markmap only if scan completed to prevent remaining traces"。如果去掉这个守卫,每次 `draw_all_cells` 都 flush,什么场景下会出残影?

**答案**:sweep 被 UI 打断时(`sweep()` 在 [main.c:891-892](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L891-L892) 提前返回),`plot_into_index` 没有执行,但 `draw_all_cells` 若照常 flush,当前页会被清空、历史页被"漂白"成打断时刻的标记集合。之后若再发生多次非完整帧的标记(如 marker 拖动、菜单开关),每次 flush 都会把历史页覆盖成更小的集合,先前真正画过像素的格子信息丢失;下一次完整帧只重画新旧轨迹并集,那些"被遗忘"的格子里旧像素就留在了屏上——即 remaining traces。flush 守卫保证:两次完整帧之间,标记只在当前页**单调累积**,画过的一切都不会被遗忘。

**练习 2**:为什么擦除旧轨迹不需要读回 LCD 像素,也不需要额外内存记录旧轨迹坐标?

**答案**:因为格子重画是"整格从头生成":清背景→网格→当前轨迹。旧像素所在格子只要被重画,旧像素就被背景覆盖了。而"旧轨迹压过哪些格子"这一信息,恰好在上一帧 flush 时被保留在历史页里(当时的当前页变成了历史页)——双页结构用 8 字节顺便记下了"上一帧画过哪里"。

**练习 3**:`draw_cell` 里轨迹线用 `|=` 叠色、marker 图标用 `=` 覆盖,为什么不同?

**答案**:`|=` 让黄色轨迹叠在灰色网格上时颜色按位或,线仍可见且不用先判断底色(省一次读改判);marker 图标自带背景(位图里 0 的位置写 `DEFAULT_BG_COLOR`),必须整像素覆盖才能形成清晰的图标轮廓,混合反而会糊。

---

### 4.3 REDRAW_* 标志:请求-响应式刷新模型

#### 4.3.1 概念说明

markmap 只回答"重画**哪些格子**",而 `redraw_request` 回答"屏幕上**哪一类内容**过期了"。它是一个 `volatile uint8_t`,6 个位各管一类刷新需求:

| 标志 | 值 | 典型置位者 | `draw_all` 的响应 |
|---|---|---|---|
| `REDRAW_CELLS` | 1<<0 | Thread1 每完整帧([main.c:135](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L135));菜单/键盘关闭([plot.c:1446-1460](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1446-L1460)) | `draw_all_cells(flush)` |
| `REDRAW_FREQUENCY` | 1<<1 | `update_grid()`(频率变,plot.c:107);频率编辑 UI([ui.c:422](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L422)) | `draw_frequencies()` |
| `REDRAW_CAL_STATUS` | 1<<2 | 校准类命令(如 [main.c:1356](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1356) 等六处) | `draw_cal_status()` |
| `REDRAW_MARKER` | 1<<3 | marker 追踪([main.c:141](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L141));`set_electrical_delay`([main.c:1716](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1716)) | `markmap_upperarea()` + `draw_all_cells` |
| `REDRAW_BATTERY` | 1<<4 | Thread1 每完整帧([main.c:135](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L135)) | `draw_battery_status()` |
| `REDRAW_AREA` | 1<<5 | `redraw_marker` 尾部(plot.c:1443);`set_color`(改配色,[main.c:2104-2105](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2104-L2105)) | `force_set_markmap()` + `draw_all_cells` |

这是一个典型的**请求-响应**(request-response)模型:任何模块在任何时刻用 `|=` 累积需求(只置 1 不清 0,多个请求自动合并),sweep 线程在每轮循环末尾调 `draw_all()` 统一消费,处理完整体清零。它和 u2-l5 讲的 `sweep_mode`/`operation_requested`/`shell_function` 是同一套"标志交接"并发手法的显示侧版本——大多数字母类命令经 `CMD_WAIT_MUTEX` 移交到 sweep 线程执行,置位与消费天然串行,无需锁。

注意三类覆盖层的差别:`REDRAW_FREQUENCY/CAL_STATUS/BATTERY` 对应的内容(底部频率条、左侧校准状态列、左上角电池图标)都**不走 markmap**,由各自的 `draw_*` 函数用 `ili9341_fill` 自擦自画。

#### 4.3.2 核心流程

```text
任意代码: redraw_request |= REDRAW_XXX        (下单,可多单合并)
        ↓
Thread1 循环末尾: draw_all(flush = completed)  (统一配送)
    ├─ REDRAW_AREA    → force_set_markmap()      把整屏标脏(升级为全画)
    ├─ REDRAW_MARKER  → markmap_upperarea()      顶部读数区标脏
    ├─ CELLS|MARKER|AREA 任一 → draw_all_cells(flush)   走格子流水线
    ├─ REDRAW_FREQUENCY → draw_frequencies()     底部条,fill+drawstring 自擦
    ├─ REDRAW_CAL_STATUS→ draw_cal_status()      左侧列,同上
    ├─ REDRAW_BATTERY   → draw_battery_status()  左上角位图
    └─ redraw_request = 0                        清空订单簿
```

绘图区的动态收缩:`area_width/area_height` 是全局变量,菜单/键盘弹出时 ui.c 把它改小,`draw_all_cells` 的循环上限随之收缩——菜单盖住的列根本不参与遍历;菜单关闭时再由 `request_to_draw_cells_behind_menu()` 把那片区域标脏恢复。

#### 4.3.3 源码精读

标志位定义([nanovna.h:290-297](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L290-L297)):

```c
// _request flag for update screen
#define REDRAW_CELLS      (1<<0)
#define REDRAW_FREQUENCY  (1<<1)
#define REDRAW_CAL_STATUS (1<<2)
#define REDRAW_MARKER     (1<<3)
#define REDRAW_BATTERY    (1<<4)
#define REDRAW_AREA       (1<<5)
extern volatile uint8_t redraw_request;
```

定义与初始化在 [main.c:88-89](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L88-L89):`volatile uint8_t redraw_request = 0; // contains REDRAW_XXX flags`。

`draw_all` 的分发逻辑([plot.c:1409-1425](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1409-L1425)):

```c
void draw_all(bool flush) {
  if (redraw_request & REDRAW_AREA)
    force_set_markmap();                       // 整屏脏
  if (redraw_request & REDRAW_MARKER)
    markmap_upperarea();                       // 顶部读数区脏
  if (redraw_request & (REDRAW_CELLS | REDRAW_MARKER | REDRAW_AREA))
    draw_all_cells(flush);                     // 格子流水线(先画)
  if (redraw_request & REDRAW_FREQUENCY)  draw_frequencies();
  if (redraw_request & REDRAW_CAL_STATUS) draw_cal_status();
  if (redraw_request & REDRAW_BATTERY)    draw_battery_status();
  redraw_request = 0;                          // 清空订单
}
```

顺序有讲究:格子先画,三个覆盖层后画,电池图标、校准列、频率条压在最上层不会被子区重画冲掉——这也是每帧同时置 `REDRAW_CELLS|REDRAW_BATTERY` 的原因([main.c:135](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L135)):电池电压随时在变,顺带每帧刷新。

marker 移动的"快路径"([plot.c:1428-1444](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1428-L1444))不等下一轮 `draw_all`,立即画,但随后主动申请一次全屏重画来收拾伪影:

```c
void redraw_marker(int marker) {
  if (marker < 0) return;
  markmap_marker(marker);        // 新位置的格子标脏
  markmap_upperarea();           // 读数区标脏
  draw_all_cells(TRUE);          // 立即重画(此处强制 flush)
  // Force redraw all area after (disable artifacts after fast marker update area)
  redraw_request |= REDRAW_AREA; // 下一轮全屏重画兜底
}
```

ui.c 拨轮/拖动 marker 的代码正是调它([ui.c:784](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L784)、[ui.c:1676-1701](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1676-L1701) 多处)。

菜单开/关对绘图区的收缩与恢复([ui.c:1600-1612](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1600-L1612)):

```c
static void ui_mode_menu(void) {
  ...
  /* narrowen plotting area */
  area_width  = AREA_WIDTH_NORMAL - MENU_BUTTON_WIDTH;   // 310-70=240
  area_height = AREA_HEIGHT_NORMAL;
  ensure_selection();
  draw_menu();
}
```

`ui_mode_normal()` 恢复满幅([ui.c:1656-1666](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1656-L1666));`leave_ui_mode` 在关闭菜单时调用恢复函数([plot.c:1446-1452](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1446-L1452)):

```c
void request_to_draw_cells_behind_menu(void) {
  // Values Hardcoded from ui.c
  invalidate_rect(320-70, 0, 319, 239);   // 菜单占的右侧 70 列
  redraw_request |= REDRAW_CELLS;
}
```

三个覆盖层自擦自画的例子——底部频率条([plot.c:1624-1652](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1624-L1652)):

```c
void draw_frequencies(void) {
  ...
  ili9341_fill(0, FREQUENCIES_YPOS, 320, FONT_GET_HEIGHT, DEFAULT_BG_COLOR);
  ...                     // 先整行刷背景,再画两段文字
  ili9341_drawstring(buf1, FREQUENCIES_XPOS1, FREQUENCIES_YPOS);
```

校准状态列([plot.c:1654-1681](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1654-L1681))画在 `x=0, y=100` 起的左侧边条,电池图标([plot.c:1688-1715](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1688-L1715))按电压逐格生成位图后 `blit8BitWidthBitmap` 上屏——三者的屏幕位置(左列、左上角、底行)都在 64×32 格子铺出来的绘图区之外或边缘,所以不参与 markmap,由标志位直接驱动。

#### 4.3.4 代码实践

**实践目标**:量化"脏更新"与"强制全屏"的差别,理解 `REDRAW_AREA` 的代价;有真机的读者再做一个闪烁对比实验。

**操作步骤**:

1. (PC 端,示例代码)统计两种策略每帧重画的格子数:

```python
# 示例代码:统计每帧重画格子数 —— 正常脏更新 vs 强制 REDRAW_AREA
import random
def frame_cost(use_area):
    mm = Markmap(); mm.old = [0]*MY
    mm.force(); draw_all_cells(mm, True, pts_old)          # 首帧(复用 4.2.4)
    total = 0
    for k in range(1, 30):                                 # 模拟 29 帧连续扫频
        noise = [p[1] + random.randint(-8, 8) for p in pts_old]
        pts = [(p[0], y) for p, y in zip(pts_old, noise)]
        mark_cells_from_index(mm, pts)                     # 正常:只标新轨迹格子
        if use_area: mm.force()                            # 实验:模拟 REDRAW_AREA
        total += draw_all_cells(mm, True, pts)
    return total / 29
print("正常脏更新平均每帧格子数:", frame_cost(False))      # 全部 40 格
print("强制 REDRAW_AREA 平均每帧格子数:", frame_cost(True)) # 恒为 40
```

2. (真机端,可选)在 [main.c:128](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L128) 的 `ui_process();` 之后临时加一行 `redraw_request |= REDRAW_AREA;`,重新编译烧录,连续扫频时观察屏幕;看完效果删掉这行恢复原状。

**需要观察的现象**:PC 端两个数字的对比(轨迹只占屏幕一角时,脏更新的格子数远小于 40);真机端加行后轨迹、网格连同背景整屏一起闪,UI 响应变钝。

**预期结果**:PC 端"正常脏更新"平均值显著小于 40,"强制 AREA"恒为 40。真机端整屏重画意味着每个格子都要重跑网格逐像素判定(源码注释:Smith 网格全屏约 1000 tick、bulk 上屏约 500 tick,系统 tick 频率 10000Hz 即 1 tick=100µs),帧率明显下降、可见闪烁。若无法烧录真机,标注「待本地验证」,以 PC 端统计为准。

#### 4.3.5 小练习与答案

**练习 1**:为什么 `draw_all` 末尾直接 `redraw_request = 0` 整体清零,而不是逐位清除刚处理过的标志?

**答案**:整个函数就是一个原子批次:开头读到的是本批全部订单,分发处理覆盖了所有 6 位对应的动作,末尾清零即"订单全部完成"。若在处理中途有新请求置位,`|= `只置 1,清零会把它一起抹掉——理论上存在极窄的丢失窗口;但如 4.3.1 所述,大多数置位者(shell 命令、Thread1 自身)与 `draw_all` 同在 sweep 线程串行执行,窗口实际不存在。这是"单写者纪律"的又一次体现(承接 u2-l5)。

**练习 2**:marker 用拨轮连续拖动时,走的是哪条刷新路径?为什么之后还要补一次全屏?

**答案**:走 `redraw_marker()` 快路径:立即把新位置格子和顶部读数区标脏并 `draw_all_cells(TRUE)` 马上上屏,获得跟手的拖动体验。但快路径只标了新位置,旧位置 marker 图标的像素靠双页并集机制在本次重画中顺带清掉,快速连续更新时边角格子可能照顾不到,所以尾部置 `REDRAW_AREA`,让下一轮 `draw_all` 全屏重画兜底——用"立即反馈 + 延迟全屏"换体验与正确性兼得。

**练习 3**:菜单弹出时 `area_width` 从 310 缩到 240,这对 markmap 和 `draw_all_cells` 分别意味着什么?

**答案**:markmap 本体不变(仍按整屏 320 定义的 5 列);`draw_all_cells` 的列循环上限变为 \(\lceil 240/64 \rceil = 4\),第 4 列格子完全不遍历——菜单盖住的区域不浪费渲染。菜单关闭时 `ui_mode_normal()` 恢复 area_width,`request_to_draw_cells_behind_menu()` 用 `invalidate_rect(250,0,319,239)` 把第 3、4 列标脏,下一轮 `draw_all` 只重画这片恢复区。

---

## 5. 综合实践

把三个模块串成一个完整的"迷你脏矩形渲染器",回答一个总问题:**连续扫频时,markmap 机制到底省了多少渲染量,flush 守卫又在什么时候发挥作用?**

在 4.1.4/4.2.4/4.3.4 已有代码的基础上(示例代码),完成三件事:

1. **动画统计**:让一条带噪声的轨迹跑 100 帧,逐帧记录重画格子数,画出(或打印)每帧格数曲线;再混入 3 次"UI 打断"(帧中途放弃更新,`draw_all_cells(flush=False)`),验证打断帧之后那一帧的重画格子数会偏大——因为当前页累积了打断期间的标记。
2. **残影对照**:同时维护"单页模拟器"跑同样场景,报告两者的残影像素总数差异。
3. **闪烁代价**:统计"强制 REDRAW_AREA"版本 100 帧的总重画格子数,与正常版对比,给出百分比结论。

预期结果示例(具体数值取决于构造的轨迹,**待本地验证**):正常版每帧约重画 10~20 格(轨迹细、只占部分行),强制 AREA 版恒 40 格,总代价高出数倍;单页模拟器在轨迹大跨度移动的帧后出现残影,双页版始终为 0。

有真机的读者可追加实机验证:用 `scan` 命令触发一次完整扫频,对比正常固件与 4.3.4 第 2 步的改动版在扫频过程中的画面稳定性差异。

## 6. 本讲小结

- **markmap 脏标记**:立即模式渲染 + 无帧缓冲的约束下,屏幕被划成与 `spi_buffer` 等大的 64×32 格子(5 列×8 行=40 格),`markmap[2][8]` 共 16 字节的位图记录"哪些格子需要重画";标记原语是 `mark_map`/`invalidate_rect`/`force_set_markmap`,轨迹由 `mark_cells_from_index` 按"相邻点包围盒"写脏,防止斜穿格子的线段漏画。
- **双页交替**:`draw_all_cells` 以两页**并集**为重画条件——历史页记"上一帧画过哪里"(旧轨迹),当前页记"这帧要画哪里"(新轨迹);格子重画时从头生成内容,旧像素被背景自然覆盖,无需读回 LCD。只有完整扫完一帧才 flush(交换+清空),打断的帧保持标记单调累积,杜绝残影。
- **trace_index 缓存**:`plot_into_index` 把 101 点复数 Γ 一次换算成打包的像素坐标(uint32 高 16 位 x、低 16 位 y),画格子、放 marker、极值搜索、触摸命中全部复用,计算与绘制彻底解耦。
- **REDRAW_* 请求-响应模型**:6 个标志位是"订单簿",任意代码 `|=` 下单,`draw_all` 统一配送后清零;`AREA` 升级为全屏、`MARKER` 补顶部读数区、格子类走 `draw_all_cells`,而频率/校准/电池三个覆盖层不走 markmap,由各自 `draw_*` 自擦自画,且画在格子之后。
- **area 动态收缩**:菜单/键盘弹出时 `area_width/area_height` 缩小,`draw_all_cells` 少遍历被遮挡的列;关闭时 `invalidate_rect` 恢复遮挡区。
- **性能意识贯穿始终**:源码注释留满 tick 数(memset 350→直写 35、Smith 网格 1000、bulk 500),每个阶段都在 Cortex-M0 上精打细算——这是本机制存在的根本理由。

## 7. 下一步学习建议

本讲之后,显示子系统的"数据→坐标→像素→刷新时机"链条已经完整。建议:

1. 下一讲 **u4-l5(ui.c:触摸、拨轮、菜单树与数值输入)**——本讲多次出现的 `ui_process`、`operation_requested`、`redraw_marker` 调用者将在那里展开;重点体会 UI 模式切换与 `area_width` 收缩的配合。
2. 回读源码:对照 [plot.c:1213-1384](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1213-L1384) 的 `draw_cell`,把 4.2.3 的六阶段流水线逐段核对,特别留意 `search_index_range_x` 的二分边界(为什么 `i1` 要多走一格)。
3. 为 u5-l3(RTOS 资源约束与优化)做铺垫:思考 `cell_buffer` 复用 `spi_buffer` 与 `transform_domain` 复用同一缓冲的潜在冲突(提示:两者都在 sweep 线程,靠调用时序隔离)。
4. 若想加深理解,可尝试一个改造练习:把 `REDRAW_BATTERY` 的刷新从"每帧"改为"电压变化超过阈值才置位",评估这对帧渲染时间的意义。
