# 标记与搜索：marker 定位、数值读取与极值搜索

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `marker_t` 三个字段的分工，解释「索引-频率双字段」这一冗余设计的目的，以及 `index` 为什么是权威字段。
2. 读懂 `marker_search` / `marker_search_left` / `marker_search_right` 的算法：它们不在原始数据上搜索，而是**借用 `trace_index` 缓存的屏幕坐标**完成极值搜索。
3. 理解 `greater` / `lesser` 函数指针比较器如何让一套扫描代码同时服务「最大值 / 最小值」两种语义。
4. 跟踪触摸拖动 marker 的完整链路：`touch_pickup_marker` → `marker_position` → `drag_marker` → `search_nearest_index`。
5. 读懂 `cell_draw_marker_info` 与 `trace_get_value_string` 系列如何按 12 种轨迹格式渲染读数与 delta 读数。
6. 独立实现一个新搜索模式（如 min-SWR），并知道如何挂进 ui.c 的菜单表。

## 2. 前置知识

本讲建立在 u4-l2（轨迹系统）之上，先把三个关键前置结论复述一遍：

**① 屏幕坐标系是「y 向下、值向上」。** LCD 的 y 轴原点在左上角，向右下增长。而轨迹绘制时换算出的 `v`（格子单位）越大、对应屏幕 y 越小（`trace_into_index` 里 `v -= logmag(coeff) * scale` 一类写法）。所以「显示值最大的点」永远是 **y 坐标最小** 的点。本讲的搜索函数全部围绕这个反转关系展开。

**② `trace_index` 是坐标缓存。** [plot.c:64-72](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L64-L72) 定义了 `index_t`（一个 `uint32_t`，高 16 位存 x、低 16 位存 y）和 `trace_index[TRACES_MAX][POINTS_COUNT]`。每次扫描完成后 `plot_into_index()` 把每个频点换算成像素坐标存进来，供绘图、命中测试、极值搜索三方复用。**本讲所有搜索都不碰 `measured[]` 原始复数，只碰这份缓存。**

**③ 函数指针比较器（comparator）。** C 语言里可以把「满足什么条件算更好」抽象成一个函数指针 `int (*compare)(int, int)`，运行时替换。这就是 `qsort` 的第三个参数的思路。NanoVNA 用同一个技巧让一次遍历代码既能找最大又能找最小。

另外两个本讲会用到的术语：

- **活动 marker（active_marker）**：当前被操作的那个 marker，读数、搜索、菜单操作都以它为对象；`-1` 表示没有。
- **delta 读数**：以一个参考 marker 为基准，显示两个 marker 之间的频率差与数值差，适合测量带宽、Q 值等。

## 3. 本讲源码地图

| 文件 | 本讲涉及内容 |
| --- | --- |
| [nanovna.h](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L253-L261) | `marker_t` 结构、`MARKERS_MAX`、marker 函数原型、`REDRAW_MARKER` 标志、`uistat_t` |
| [plot.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1044-L1189) | 本讲主战场：`marker_position`、`marker_search` 三兄弟、`search_nearest_index`、`markmap_marker` |
| [plot.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1498-L1622) | `cell_draw_marker_info` 读数标注绘制 |
| [plot.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L595-L759) | `trace_get_value_string` / `trace_get_value_string_delta` / `format_smith_value` |
| [main.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L942-L968) | `update_marker_index`（频率→索引反推）、`def_markers`、Thread1 里的 tracking、`cmd_marker`、`cmd_data` |
| [ui.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L756-L786) | 搜索菜单回调、菜单表 `menu_marker_search`、拨轮/触摸操作 marker |

## 4. 核心概念与源码讲解

### 4.1 marker 数据模型：marker_t 与索引-频率双字段

#### 4.1.1 概念说明

marker 是「钉在轨迹某频点上的图钉」，最多 4 个（`MARKERS_MAX`）。它要回答两个问题：钉在**哪个频点**（对仪器而言），以及钉在**屏幕哪里**（对绘制而言）。

第二个问题不需要存储——上一讲已经知道，屏幕位置可以随时从 `trace_index[t][index]` 查到。所以 `marker_t` 只需要解决第一个问题，而它用了**两个字段**：

```c
typedef struct marker {
  int8_t   enabled;    // 是否显示
  int16_t  index;      // 频点索引（0 .. sweep_points-1），权威字段
  uint32_t frequency;  // 频率（Hz），锚点/缓存
} marker_t;
```

见 [nanovna.h:253-261](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L253-L261)。

为什么冗余存两个？因为两边各有「更方便」的场景：

- **按索引走**：拨轮移动、搜索、绘制读数都按 `index` 走，直接对齐 `measured[]`、`frequencies[]`、`trace_index[]` 三个数组，O(1)。
- **按频率走**：用户改扫描范围（start/stop）后，频点表整个重建、索引全部失效。此时 `frequency` 充当「锚点」——固件拿旧频率去新频点表里**就近**找回一个新索引，marker 就能「留在原来的物理频率附近」而不是跳回起点。

整套 marker 状态放在 `properties_t` 里随校准槽掉电保存（[nanovna.h:375-378](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L375-L378)），并通过别名宏暴露（[nanovna.h:403-405](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L403-L405)）：

```c
#define markers current_props._markers
#define active_marker current_props._active_marker
```

出厂默认只开 M1、钉在索引 30，见 [main.c:812-814](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L812-L814)：

```c
static const marker_t def_markers[MARKERS_MAX] = {
  { 1, 30, 0 }, { 0, 40, 0 }, { 0, 60, 0 }, { 0, 80, 0 }
};
```

还有一个容易混淆的角色变量 `previous_marker`（定义在 [ui.c:64](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L64)，初值 -1）：它不是「上一个 marker」，而是 **delta 模式的参考 marker**——当你从 M1 切换到 M2 时，M1 就被记进 `previous_marker`，读数区随后显示两者的差值。

> **阅读提示（防坑）**：[nanovna.h:263-264](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L263-L264) 声明了 `extern int8_t previous_marker; extern int8_t marker_tracking;`，但全仓库**并没有**名为 `marker_tracking` 的全局变量定义——真正的状态是 `uistat.marker_tracking`（[ui.c:28-34](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L28-L34) 初始化为 FALSE）。这是一行过时声明，读代码时以 `uistat.marker_tracking` 为准。

#### 4.1.2 核心流程

频率变化时，`index` 由 `frequency` 反推（「就近取整」规则）：

```
update_frequencies()                        // main.c，改扫描范围后调用
  ├─ set_frequencies(start, stop, points)   // 重建整数频点表
  ├─ update_marker_index()                  // ★ 本讲：按 frequency 找回 index
  └─ update_grid()                          // 网格联动
```

`update_marker_index` 对每个启用的 marker：

1. 若 `frequency < fstart` → 钳到索引 0，并把 `frequency` 改写为 fstart；
2. 若 `frequency >= fstop` → 钳到最后一个点；
3. 否则在频点表里找区间 `frequencies[i] <= f < frequencies[i+1]`，比较 f 与区间**中点**，取较近一侧作为新索引。

#### 4.1.3 源码精读

反推逻辑在 [main.c:942-968](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L942-L968)：

```c
static void
update_marker_index(void)
{
  int m;
  int i;
  for (m = 0; m < MARKERS_MAX; m++) {
    if (!markers[m].enabled)
      continue;
    uint32_t f = markers[m].frequency;
    uint32_t fstart = get_sweep_frequency(ST_START);
    uint32_t fstop  = get_sweep_frequency(ST_STOP);
    if (f < fstart) {
      markers[m].index = 0;
      markers[m].frequency = fstart;
    } else if (f >= fstop) {
      markers[m].index = sweep_points-1;
      markers[m].frequency = fstop;
    } else {
      for (i = 0; i < sweep_points-1; i++) {
        if (frequencies[i] <= f && f < frequencies[i+1]) {
          markers[m].index = f < (frequencies[i] / 2 + frequencies[i + 1] / 2) ? i : i + 1;
          break;
        }
      }
    }
  }
}
```

这段代码做了三件事：越界钳制（两边都会**回写** `frequency`，保证双字段一致）、线性区间定位、中点就近二选一。注意中点的写法是 `frequencies[i]/2 + frequencies[i+1]/2` 而不是 `(frequencies[i] + frequencies[i+1]) / 2`——后者在 2.7GHz 频段会把两个 `uint32_t` 相加，\( 2.7\times10^9 + 2.7\times10^9 = 5.4\times10^9 > 2^{32} \approx 4.29\times10^9 \)，直接溢出回绕；先各自除二再相加则永远安全（代价是两频点都为奇数时中点偏小 0.5Hz，无关痛痒）。这是 u3-l1 讲过的「x/2 折半防溢出」手法的又一次出现。

shell 侧的 marker 操作入口是 `cmd_marker`（[main.c:1736-1779](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1736-L1779)）：无参数列出所有启用 marker 的 `编号 索引 频率` 三元组；`marker 1 50` 这类形式则把 M1 启用、移到索引 50，并**同时回写** `index` 与 `frequency = frequencies[index]`——所有「主动移动 marker」的代码路径都遵守这个双字段同步纪律。

#### 4.1.4 代码实践

**实践目标**：在 PC 上验证「就近取整」规则，理解双字段冗余的必要性。

**操作步骤**（无硬件，纯 Python，示例代码）：

```python
# update_marker_index 的等价实现（示例代码，运行于 PC）
def update_marker_index(freqs, marker_freq, sweep_points):
    fstart, fstop = freqs[0], freqs[sweep_points-1]
    if marker_freq < fstart:
        return 0, fstart
    if marker_freq >= fstop:
        return sweep_points-1, fstop
    for i in range(sweep_points - 1):
        if freqs[i] <= marker_freq < freqs[i+1]:
            mid = freqs[i]//2 + freqs[i+1]//2          # 折半防溢出写法
            return (i if marker_freq < mid else i+1), marker_freq
    return sweep_points-1, marker_freq

# 场景：原扫描 50k~900M、101 点，marker 钉在索引 30（频率 269,950,000 Hz 附近）
# 用户把扫描改成 100M~200M，用旧频率反推新索引
import numpy as np
old = np.linspace(50_000, 900_000_000, 101).astype(np.uint64)
new = np.linspace(100_000_000, 200_000_000, 101).astype(np.uint64)
idx, f = update_marker_index(list(map(int, new)), int(old[30]), 101)
print("新索引:", idx, " 对应频率:", f, " 频点表中该点:", new[idx])
```

**需要观察的现象**：旧频率 269.95MHz 落在新范围 [100M, 200M] 之外，触发钳制分支，marker 被钉到最后一个点（索引 100）；把 new 换成 `np.linspace(50_000, 900_000_000, 51)`（点数减半），则走区间定位分支，marker 会落到频率最接近旧值的那个点上。

**预期结果**：钳制分支返回 `(100, 200000000)`；点数减半分支返回的索引约为 15（269.95M 在新表里最接近的点），且 `frequency` 保持不变。若你的输出不符，先检查区间定位用的是左闭右开 `[i, i+1)`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `index` 用 `int16_t` 而不是 `uint8_t`？`sweep_points` 最大多少？

答案：`POINTS_COUNT = 101`，`uint8_t`（0~255）其实装得下；用 `int16_t` 主要是给「无效/中间状态」和潜在扩展留出负数空间，也与 `active_marker`（`int8_t`，-1 表示无）的风格一致——这套代码里「-1 表示没有」是惯例，无符号类型无法表达。

**练习 2**：marker 状态存在 `properties_t` 里意味着什么？`caldata_save` 之后 marker 会怎样？

答案：marker 的开关、位置会随校准槽一起掉电保存；`caldata_recall(id)` 调出某个槽时，当时保存的 marker 布局（包括 M1~M4 谁开着、钉在哪）会一并恢复——这是「测量现场快照」语义的一部分（u3-l4）。

**练习 3**：Thread1 的 tracking 分支（见 4.2.3）只改 `markers[active_marker].index`，不更新 `frequency`，会造成 bug 吗？

答案：不会立即出错。屏幕读数 `cell_draw_marker_info` 直接用 `frequencies[markers[mk].index]`（见 4.4.3），不读缓存字段；`frequency` 只在下一次 `update_marker_index`（改扫描范围）时被当作锚点使用，而那时它本来就要被区间定位/钳制重写。不过若用户在 tracking 移动后、改范围前执行 `marker`（无参）命令，看到的 `frequency` 是滞后值——这是可接受的轻微不一致，读代码时要知道。

### 4.2 极值搜索：marker_search 与 greater/lesser 比较器

#### 4.2.1 概念说明

「搜索」要回答的问题是：把活动 marker 跳到轨迹的峰（或谷）上。NanoVNA 的实现有一个非常省事、也非常值得学习的设计决策：

> **不在原始数据 `measured[]` 上搜索，而在屏幕坐标缓存 `trace_index[]` 上搜索。**

好处有三：

1. **格式无关**。`measured` 里只有复数 Γ，什么叫「最大」取决于轨迹格式（LOGMAG 最大？SWR 最小？相位最正？）。而 `trace_into_index` 已经把任何格式统一换算成了「值越大、y 越小」的屏幕坐标——搜索只需在 y 上做文章，一套代码覆盖全部 10 种矩形格式。
2. **免重复计算**。坐标本来就要为绘图算好，搜索直接复用，Cortex-M0 上没有浪费。
3. **所见即所得**。削顶（y 被 `trace_into_index` 钳制到屏幕外）之后，屏上看起来一样的点，搜索结果也一样——这既是特性也是局限（见练习）。

代价是搜索结果依赖 `uistat.current_trace`（当前选中哪条轨迹）与 `trace_index` 是新鲜的（刚被 `plot_into_index` 刷新过）。函数开头 `if (uistat.current_trace == -1) return -1;` 就是在防「没有选中轨迹」。

#### 4.2.2 核心流程

**比较器**：两个一行函数加一个默认指向 `lesser` 的函数指针（[plot.c:1080-1083](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1080-L1083)）：

```c
static int greater(int x, int y) { return x > y; }
static int lesser(int x, int y) { return x < y; }

static int (*compare)(int x, int y) = lesser;
```

注意语义是反的：`greater(x,y)` 为真表示「y 更小」。由于屏幕 y 反转，**y 更小 = 显示值更大**，所以装上 `greater` 后算法收敛到最小 y，即搜索**最大值**（菜单 MAXIMUM）；装 `lesser` 则搜索**最小值**。`set_marker_search(mode)` 完成切换（[plot.c:1106-1110](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1106-L1110)）：`mode == 0`（菜单第 0 项 MAXIMUM）装 `greater`，否则装 `lesser`。

**全程搜索 `marker_search`** 的流程：

```
输入：uistat.current_trace（当前轨迹 t），compare（当前模式）
1. 若没有选中轨迹 → 返回 -1
2. value ← 第 0 点的 CELL_Y，found ← 0
3. 对 i = 0 .. sweep_points-1：
     若 compare(value, CELL_Y(index[i])) 为真   // 该点 y 比 value 更极端
        value ← CELL_Y(index[i])；found ← i     // 收敛
4. 返回 found
```

**左侧局部搜索 `marker_search_left(from)`** 是个两阶段算法（右侧对称）：

```
阶段一（下坡）：从 from-1 向左走，只要当前点不比 value 更极端就继续，
              value 随走随更新 —— 跨过山谷；
阶段二（上坡）：遇到第一个「更极端」的点后改为记录模式，每个仍在改善的
              点都记为 found —— 爬到坡顶；
停止：遇到第一个不再改善（更差）的点，返回最后一次记录的 found。
```

效果：从 `from` 出发向左找到**最近的同类型局部极值**（MAXIMUM 模式下是左边的第一个局部峰）。这正好是「拨轮上拨一下，跳到下一个峰」的交互。

#### 4.2.3 源码精读

全程搜索在 [plot.c:1085-1104](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1085-L1104)，注意更新条件用的是**严格不等号**：

```c
int
marker_search(void)
{
  int i;
  int found = 0;

  if (uistat.current_trace == -1)
    return -1;

  int value = CELL_Y(trace_index[uistat.current_trace][0]);
  for (i = 0; i < sweep_points; i++) {
    index_t index = trace_index[uistat.current_trace][i];
    if ((*compare)(value, CELL_Y(index))) {
      value = CELL_Y(index);
      found = i;
    }
  }

  return found;
}
```

严格不等意味着并列时保留**最先出现**的索引——轨迹削顶（大量点被钳到 y=0）时搜到的是第一个出屏点，这是「所见即所得」的副作用。

左侧局部搜索在 [plot.c:1112-1138](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1112-L1138)，两个循环正是上面流程图的「下坡/上坡」两阶段：

```c
int
marker_search_left(int from)
{
  int i;
  int found = -1;

  if (uistat.current_trace == -1)
    return -1;

  int value = CELL_Y(trace_index[uistat.current_trace][from]);
  for (i = from - 1; i >= 0; i--) {
    index_t index = trace_index[uistat.current_trace][i];
    if ((*compare)(value, CELL_Y(index)))
      break;                        // 阶段一结束：碰到第一个更极端的点
    value = CELL_Y(index);          // 还在 valley 里，继续走
  }

  for (; i >= 0; i--) {
    index_t index = trace_index[uistat.current_trace][i];
    if ((*compare)(CELL_Y(index), value))
      break;                        // 阶段二结束：开始变差 → 峰已过
    found = i;                      // 记录仍在改善的点
    value = CELL_Y(index);
  }
  return found;
}
```

右侧版本 [plot.c:1140-1166](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1140-L1166) 只是把循环方向反过来（`from + 1` 向上走到 `sweep_points`）。

**UI 挂接**：菜单表在 [ui.c:994-1003](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L994-L1003)，五项分别对应 `menu_marker_search_cb` 的 `item` 0~4（BACK 是 MT_CANCEL 不进回调）：

```c
const menuitem_t menu_marker_search[] = {
  //{ MT_CALLBACK, "OFF", menu_marker_search_cb },
  { MT_CALLBACK, 0, "MAXIMUM", menu_marker_search_cb },
  { MT_CALLBACK, 0, "MINIMUM", menu_marker_search_cb },
  { MT_CALLBACK, 0, "\2SEARCH\0" S_LARROW" LEFT", menu_marker_search_cb },
  { MT_CALLBACK, 0, "\2SEARCH\0" S_RARROW" RIGHT", menu_marker_search_cb },
  { MT_CALLBACK, 0, "TRACKING", menu_marker_search_cb },
  ...
```

回调 [ui.c:756-786](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L756-L786) 把 item 映射到动作：MAXIMUM/MINIMUM 先 `set_marker_search(item)` 再 `marker_search()`；LEFT/RIGHT 调 `marker_search_left/right` 并**顺手关掉 tracking**（手动搜索与自动跟踪互斥）；TRACKING 翻转 `uistat.marker_tracking`。命中后统一 `markers[active_marker].index = i;` 并 `redraw_marker(active_marker)` 快速重绘。

**tracking 的联动**：sweep 线程每完成一趟扫描都会跑一遍 [main.c:131-147](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L131-L147)：

```c
    if (sweep_mode & SWEEP_ENABLE && completed) {
      if ((domain_mode & DOMAIN_MODE) == DOMAIN_TIME) transform_domain();
      // Prepare draw graphics, cache all lines, mark screen cells for redraw
      plot_into_index(measured);
      redraw_request |= REDRAW_CELLS | REDRAW_BATTERY;

      if (uistat.marker_tracking) {
        int i = marker_search();
        if (i != -1 && active_marker != -1) {
          markers[active_marker].index = i;
          redraw_request |= REDRAW_MARKER;
        }
      }
    }
```

执行顺序很关键：**先** `plot_into_index` 刷新坐标缓存，**再** `marker_search`——tracking 拿到的永远是本趟扫描的新坐标，于是 marker 像「贴在峰上的磁铁」，轨迹每重排一次就自动重新吸附。`REDRAW_MARKER` 标志（[nanovna.h:294](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L294)）随后被 `draw_all` 消费（多刷一块顶部读数区，见 4.4.3）。

> **术语澄清**：大纲里把这条联动称作「marker_tracking 与 REFLOW 的联动」，但当前源码（HEAD d02db79）中**不存在**任何名为 REFLOW 的标识符。真实机制就是上面这条 `plot_into_index → marker_search → REDRAW_MARKER → draw_all` 循环——「轨迹每次重排（reflow）后 marker 重新贴合极值」。请以源码为准，不要在代码里找 REFLOW 这个词。

**拨轮路径**：`lever_search_marker`（[ui.c:1690-1703](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1690-L1703)）在 `LM_SEARCH` 模式下把拨轮上/下拨映射为 `marker_search_right/left`，与菜单共用同一套比较器状态。

#### 4.2.4 代码实践

**实践目标**：在 PC 上复现 `marker_search_left` 的两阶段算法，验证「先下坡、后上坡」的行为。

**操作步骤**（示例代码，运行于 PC）：

```python
# marker_search 系列的等价实现（示例代码）
def make_index(ys):                       # 模拟 CELL_Y：值越大 y 越小
    return [232 - y for y in ys]          # HEIGHT=232，简化为线性映射

def marker_search(idx, compare):
    value, found = idx[0], 0
    for i, y in enumerate(idx):
        if compare(value, y):             # y 比 value 更极端
            value, found = y, i
    return found

def marker_search_left(idx, frm, compare):
    value = idx[frm]
    i = frm - 1
    while i >= 0:
        if compare(value, idx[i]): break  # 碰到第一个更极端的点
        value = idx[i]; i -= 1            # 下坡
    found = -1
    while i >= 0:
        if compare(idx[i], value): break  # 开始变差
        found, value = i, idx[i]; i -= 1  # 上坡
    return found

greater = lambda a, b: a > b              # 装 greater = 搜显示值最大
lesser  = lambda a, b: a < b

# 双峰曲线：谷在索引 40，左峰索引 20（值 8），右峰索引 70（值 9）
ys = [5]*10 + [6,7,8,7,6,5,4,3]*5 + [3]*30 + [4,5,6,7,8,9,8,7,6,5] + [5]*21
ys[20], ys[70] = 8, 9
idx = make_index(ys[:101])

print("全程最大:", marker_search(idx, greater))          # 期望 70
print("从 50 向左的局部峰:", marker_search_left(idx, 50, greater))  # 期望 20
print("从 30 向左的局部峰:", marker_search_left(idx, 30, greater))  # 期望 20
```

**需要观察的现象**：从索引 50（谷底右侧）出发，阶段一一直走到索引 41 附近才开始「上坡」，最终停在 20；从索引 30（左峰右坡上）出发，阶段一立刻在 29 处中止（下坡方向上第一个点就更好），阶段二直接爬到 20。

**预期结果**：三行输出依次为 `70`、`20`、`20`。若第二行输出 40 附近的值，说明你的阶段一循环条件把「改善」和「变差」弄反了。

#### 4.2.5 小练习与答案

**练习 1**：当前轨迹是 SWR 格式时，菜单 MINIMUM 搜到的是什么？还需要专门写一个「min-SWR 搜索」吗？

答案：搜到驻波比最小的频点。因为 `trace_into_index` 对 SWR 同样满足「值大向上」（`v += (1 - swr) * scale`），最小 SWR = 最大 y，装 `lesser` 即命中。**交互层面**不需要新代码——只有当你要规避「削顶并列取先」或想在数据域（而非屏幕坐标域）搜索时才需要新实现，这正是本讲综合实践的选题动机。

**练习 2**：一条 LOGMAG 轨迹有 12 个点都被钳到屏幕顶端（y=0 且彼此并列），`marker_search`（greater 模式）返回哪一个？

答案：返回其中**索引最小**的那个。更新条件是严格 `value > CELL_Y(index)`，并列点不触发更新，`found` 停留在第一次达到 y=0 的位置。

**练习 3**：为什么 LEFT/RIGHT 菜单项要显式 `uistat.marker_tracking = false`，而 MAXIMUM/MINIMUM 不用？

答案：手动搜索把 marker 钉到某个局部峰后，若 tracking 仍开着，下一趟扫描完成时 `marker_search()` 会把它强行拉回**全程**极值，手动定位立刻失效——所以方向搜索必须先解除跟踪。MAXIMUM/MINIMUM 找的就是全程极值，与 tracking 的目标一致，甚至常配合使用（先点 MINIMUM 定模式，再开 TRACKING 持续跟踪最小值），无需关闭。

### 4.3 坐标定位：marker_position 与 search_nearest_index

#### 4.3.1 概念说明

搜索解决「数据→marker」，定位解决两个反问题：

- **marker→屏幕坐标**：绘制菱形标记、判断手指是否点中它（`marker_position`）；
- **屏幕坐标→频点**：手指按在轨迹附近时，把触摸点换算回频点索引（`search_nearest_index`）。

两者都只做一件事：查 `trace_index`。这再次印证上一讲的观点——`trace_index` 是绘制、交互、搜索三方共用的「屏幕坐标系中间层」。也正因为 Smith/Polar 格式的坐标同样缓存在 `trace_index` 里（只是 x 不再代表频率），本节的拖动逻辑**对圆图同样有效**，无需特判。

#### 4.3.2 核心流程

```
手指按下 (touch irq → ui_process → touch_pickup_marker)
  ├─ 对每个 启用marker × 启用trace：
  │     marker_position(m,t,&x,&y)          // 查 trace_index 得标记尖点坐标
  │     若 触摸点到(x,y)距离 < 20px  → 命中
  │         previous_marker = active_marker; active_marker = m
  │         进入 drag_marker(t,m) 循环：
  │             touch_position → 减去 OFFSETX/OFFSETY
  │             search_nearest_index(x,y,t) // 在该轨迹上找最近频点
  │             markers[m].index = index
  │             markers[m].frequency = frequencies[index]
  │             redraw_marker(m)            // 快速重绘
  │         直到 touch_check() == EVT_TOUCH_RELEASED
```

`search_nearest_index` 内部是带阈值的最近邻：对每个频点算 \( d = dx^2 + dy^2 \)，任一方向超过 20px 直接跳过，取 d 最小者。

#### 4.3.3 源码精读

`marker_position` 只有三行（[plot.c:1072-1078](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1072-L1078)）——把 `trace_index` 里的打包坐标拆成 x/y：

```c
void
marker_position(int m, int t, int *x, int *y)
{
  index_t index = trace_index[t][markers[m].index];
  *x = CELL_X(index);
  *y = CELL_Y(index);
}
```

`search_nearest_index` 在 [plot.c:1168-1189](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1168-L1189)：

```c
int
search_nearest_index(int x, int y, int t)
{
  index_t *index = trace_index[t];
  int min_i = -1;
  int min_d = 1000;
  int i;
  for (i = 0; i < sweep_points; i++) {
    int16_t dx = x - CELL_X(index[i]);
    int16_t dy = y - CELL_Y(index[i]);
    if (dx < 0) dx = -dx;
    if (dy < 0) dy = -dy;
    if (dx > 20 || dy > 20)
      continue;
    int d = dx*dx + dy*dy;
    if (d < min_d) {
      min_d = d;
      min_i = i;
    }
  }
  return min_i;
}
```

三个细节：

1. **阈值 20px** 是快速剪枝：曼哈顿距离超限直接 `continue`，避免乘法；
2. **`min_d` 初值 1000 是哨兵**：\( 20^2 + 20^2 = 800 < 1000 \)，任何通过阈值的点都必然打败它，因此「没找到」自然表现为返回 -1，不需要额外标志；
3. **逐点线性扫**：101 点的规模下 O(n) 完全够快（plot.c 里另有一个用于绘图的二分版本 `search_index_range_x`，[plot.c:900-938](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L900-L938)，注释说明再快的算法也带不来可感知收益）。

触摸侧的拾取与拖动在 [ui.c:2109-2148](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L2109-L2148)。命中判定用的同样是 20px 半径的圆（`x*x + y*y < 20*20`），命中后记 `previous_marker`、切 `active_marker`、选中该轨迹并切到 `LM_MARKER` 拨轮模式；随后 [ui.c:2090-2107](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L2090-L2107) 的 `drag_marker` 在 `do...while` 里循环「读触摸→换索引→双字段同步→快速重绘」，直到手指抬起：

```c
static void
drag_marker(int t, int m)
{
  /* wait touch release */
  do {
    int touch_x, touch_y;
    int index;
    touch_position(&touch_x, &touch_y);
    touch_x -= OFFSETX;
    touch_y -= OFFSETY;
    index = search_nearest_index(touch_x, touch_y, t);
    if (index >= 0) {
      markers[m].index = index;
      markers[m].frequency = frequencies[index];
      redraw_marker(m);
    }
  } while (touch_check()!= EVT_TOUCH_RELEASED);
}
```

拨轮路径 `lever_move_marker`（[ui.c:1669-1688](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1669-L1688)）做同样的事，只是把「连续坐标」换成 `index ± 1` 的步进。

#### 4.3.4 代码实践

**实践目标**：验证 20px 阈值与「距离最近」规则，理解为什么拖动「感觉跟手」。

**操作步骤**（示例代码，运行于 PC）：

```python
# search_nearest_index 的等价实现 + 拖动模拟（示例代码）
WIDTH, POINTS = 300, 101
def cell_x(i):                                   # plot.c L589 的 x 换算
    return (i * WIDTH + (POINTS-1)//2) // (POINTS-1) + 5   # +CELLOFFSETX

def search_nearest_index(x, y, pts):             # pts: [(x,y), ...]
    min_i, min_d = -1, 1000
    for i, (px, py) in enumerate(pts):
        dx, dy = abs(x - px), abs(y - py)
        if dx > 20 or dy > 20:
            continue
        d = dx*dx + dy*dy
        if d < min_d:
            min_d, min_i = d, i
    return min_i

pts = [(cell_x(i), 100) for i in range(POINTS)]  # 一条平直轨迹
print("点(160,105) 命中:", search_nearest_index(160, 105, pts))   # 期望 ~52
print("点(160,130) 命中:", search_nearest_index(160, 130, pts))   # 期望 -1（dy=30 超限）
print("点(-5,100)  命中:", search_nearest_index(-5, 100, pts))    # 期望 0
```

**需要观察的现象**：垂直偏离 5px 仍能命中且索引随 x 移动连续变化；垂直偏离 30px 返回 -1（`drag_marker` 里 `if (index >= 0)` 会跳过更新，marker 停在原地，直到手指回到轨迹 20px 范围内）。

**预期结果**：三行输出约为 `52`、`-1`、`0`。（具体数值待本地验证——取决于你代入的 `cell_x` 实现是否与固件一致。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `dx`、`dy` 声明为 `int16_t` 而不是 `int`？

答案：触摸坐标与屏幕坐标都不超过 320/240，差的绝对值必然落在 `int16_t` 范围内；用窄类型是嵌入式代码的省字习惯。但要注意若两边可能是「大正数减小负数」就会回绕——这里输入域有界，安全。

**练习 2**：拖动 marker 时 `drag_marker` 只按轨迹 t 搜索，如果屏幕上同时显示两条轨迹会怎样？

答案：命中判定（`touch_pickup_marker`）遍历所有 启用marker×启用轨迹 组合，谁先进入 20px 圆就选中谁（连带把 `uistat.current_trace` 切到那条轨迹）；但一旦进入拖动，就锁定在当初选中的轨迹 t 上搜索最近点——手指滑到另一条轨迹附近时，marker 仍沿原轨迹的 x 投影移动。

**练习 3**：`redraw_marker` 为什么在 `draw_all_cells(TRUE)` 之后还要 `redraw_request |= REDRAW_AREA`？

答案：这是快速路径的「擦屁股」策略（[plot.c:1430-1444](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1430-L1444)）：拖动时只重画 marker 新位置覆盖的 cell 追求跟手，但旧位置残影、轨迹被压住的部分可能没擦干净，于是顺手申请一次全区域重绘，让下一帧 `draw_all` 兜底。机制细节正是 u4-l4 的主题。

### 4.4 读数绘制：cell_draw_marker_info 与 trace_get_value_string

#### 4.4.1 概念说明

marker 的价值最终落在屏幕顶部那几行读数上：`M1 100.000000MHz -3.25dB`。生成这些文字需要解决两个问题：

1. **数值→字符串**：按当前轨迹格式把复数 Γ 变成带单位的文本（`trace_get_value_string` / `trace_get_value_string_delta` / `format_smith_value`）；
2. **字符串→屏幕**：把文本排进绘图区顶部 32px 高的信息带，且要与 markmap 脏矩形机制配合，只在必要的 cell 里绘制（`cell_draw_marker_info`）。

读数有两套布局，由 `previous_marker` 与 `uistat.current_trace` 是否有效决定：

- **多 marker 模式**（`previous_marker != -1 && current_trace != -1`）：每个启用的 marker 一条「M编号 + 频率 + 当前轨迹格式的读数」，两列排布；开了 `marker_delta` 时非活动 marker 显示与活动 marker 的**差值**；
- **单 marker 模式**（else 分支）：每条**启用轨迹**一条「CH编号 + 格式信息 + 读数」——当你只关心一个 marker、但想同时看多条轨迹在该频点的值时用这套。

#### 4.4.2 核心流程

```
cell_draw_marker_info(x0, y0)        // 仅在绘制第 0 行 cell（n==0）时被调用
  ├─ active_marker < 0 → 直接返回
  ├─ 多 marker 模式：
  │    for 每个启用 marker mk：
  │       位置 = (1 + (j%2)*(WIDTH/2), 1 + (j/2)*8)     // 两列、行距 8px
  │       画 S_SARROW（若是活动 marker）、"M编号"
  │       marker_delta 且非活动 → Δ频率 + trace_get_value_string_delta
  │       否则                →  频率  + trace_get_value_string
  │    未开 delta 时额外画一行 "Δa-b: 频率差"（时域则显示 时间差(距离差)）
  ├─ 单 marker 模式：
  │    for 每条启用轨迹 t：画 "CH编号 + trace_get_info + trace_get_value_string"
  │    再画一行 "M编号: 频率"（时域显示 时间(距离)）
  └─ electrical_delay != 0 → 追加一行 "Edelay ..."
```

频率差的换算：`delta = |freq - freq1|`，格式串 `"%.10qHz"`、`"%c%.13qHz"` 里的 `%q` 是 `plot_printf` 的工程单位前缀（k/M/G）扩展，`%F` 则按数量级自动选择单位——这套裁剪版 printf 在 u5-l1 还会遇到。

#### 4.4.3 源码精读

绘制主体在 [plot.c:1498-1541](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1498-L1541)，多 marker 循环的核心：

```c
static void
cell_draw_marker_info(int x0, int y0)
{
  char buf[24];
  int t;
  if (active_marker < 0)
    return;
  int idx = markers[active_marker].index;
  int j = 0;
  if (previous_marker != -1 && uistat.current_trace != -1) {
    int t = uistat.current_trace;
    int mk;
    for (mk = 0; mk < MARKERS_MAX; mk++) {
      if (!markers[mk].enabled)
        continue;
      int xpos = 1 + (j%2)*(WIDTH/2) + CELLOFFSETX - x0;
      int ypos = 1 + (j/2)*(FONT_GET_HEIGHT+1) - y0;
      ...
      uint32_t freq = frequencies[markers[mk].index];
      if (uistat.marker_delta && mk != active_marker) {
        uint32_t freq1 = frequencies[markers[active_marker].index];
        uint32_t delta = freq > freq1 ? freq - freq1 : freq1 - freq;
        plot_printf(buf, sizeof buf, S_DELTA"%.9qHz", delta);
      } else {
        plot_printf(buf, sizeof buf, "%.10qHz", freq);
      }
      cell_drawstring(buf, xpos, ypos);
      xpos += 67;
      if (uistat.marker_delta && mk != active_marker)
        trace_get_value_string_delta(t, buf, sizeof buf, measured[trace[t].channel], markers[mk].index, markers[active_marker].index);
      else
        trace_get_value_string(t, buf, sizeof buf, measured[trace[t].channel], markers[mk].index);
      ...
```

注意两个坐标细节：所有 xpos/ypos 都减了 `x0`/`y0`，因为绘制发生在以 cell 为单位的 `cell_buffer` 里（u4-l1）；布局用 `(j%2)`、`(j/2)` 把最多 4 个 marker 排成 2×2，行距 `FONT_GET_HEIGHT+1 = 8`。

读数本体 `trace_get_value_string`（[plot.c:640-697](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L640-L697)）是按格式查表-求值-格式化的三段式 switch：

```c
static void
trace_get_value_string(int t, char *buf, int len, float array[POINTS_COUNT][2], int i)
{
  float *coeff = array[i];
  float v;
  char *format;
  switch (trace[t].type) {
  case TRC_LOGMAG:
    format = "%.2fdB";
    v = logmag(coeff);
    break;
  ...
  case TRC_SWR:
    format = "%.4f";
    v = swr(coeff);
    break;
  ...
  case TRC_SMITH:
    format_smith_value(buf, len, coeff, frequencies[i]);
    return;
```

求值函数就是 u4-l2 见过的那组：`logmag`、`phase`、`swr`（[plot.c:467-474](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L467-L474)）等。**绘图与读数复用同一组换算函数**，保证曲线位置和标注数字永远一致——这与「搜索复用 trace_index」是同一个设计哲学。

Smith 格式走 [plot.c:595-638](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L595-L638) 的 `format_smith_value`，按 `marker_smith_format` 五选一（LIN/LOG/REIM/RX/RLC），其中 RLC 把电抗换算成等效电感/电容：\( L = X/(2\pi f) \)、\( C = -1/(2\pi f X) \)。

delta 版本 [plot.c:699-759](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L699-L759) 结构相同，只是多数格式做 `v = f(coeff) - f(coeff_ref)`，并在格式串前加 `S_DELTA`（Δ 符号，[nanovna.h:182](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L182)）。两个诚实的阅读发现：SWR 分支先判 `v != INFINITY` 再做差（Γ≥1 时 `swr` 返回无穷，无穷减无穷是 NaN）；而 DELAY、R、X、Q 四个分支**并没有真正做差**，直接显示绝对值——读代码时不要想当然认为带 `_delta` 后缀的每个分支都是差值。

时域分支（[plot.c:1553-1559](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1553-L1559)、[L1600-L1604](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1600-L1604)）：`domain_mode` 为时域时，横轴不再是频率，读数改用 `time_of_index(idx)` / `distance_of_index(idx)`（[plot.c:784-794](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L784-L794)），距离 \( = \frac{idx \cdot c \cdot v_f}{2 \,\Delta f \, N_{fft}} \)，除 2 对应雷达式往返路径（u3-l5）。

最后，这块信息带的**重绘时机**由 `markmap_upperarea()` 控制（[plot.c:863-868](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L863-L868)，硬编码标记 0..31 高的矩形为脏），而 `draw_all` 看到 `REDRAW_MARKER` 就多标一次这块区域（[plot.c:1409-1425](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1409-L1425)）：

```c
void
draw_all(bool flush)
{
  if (redraw_request & REDRAW_AREA)
    force_set_markmap();
  if (redraw_request & REDRAW_MARKER)
    markmap_upperarea();
  if (redraw_request & (REDRAW_CELLS | REDRAW_MARKER | REDRAW_AREA))
    draw_all_cells(flush);
  ...
```

#### 4.4.4 代码实践

**实践目标**：用 shell 的 `data` 命令导出真实测量数据，在 PC 上复现读数换算，验证「屏幕数字 = 换算公式输出」。

**操作步骤**：

1. （有真机）通过 USB 串口终端执行 `data 0`，固件逐点打印 CH0 的 `实部 虚部`（[main.c:682-701](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L682-L701)）；需要频点表再执行 `frequencies`。没有真机就用手工构造的模拟数据（示例代码如下）。

```python
# 复现 trace_get_value_string 的 LOGMAG/SWR/相位读数（示例代码）
import cmath
def logmag(g):  return 20*cmath.log10(abs(g))
def phase(g):   return cmath.phase(g)*180/cmath.pi
def swr(g):
    x = abs(g)
    return float('inf') if x >= 1 else (1+x)/(1-x)

measured = [complex(0.1, -0.2)] * 101          # 模拟 data 0 导出的 101 行
g = measured[30]                                # marker 钉在索引 30
print(f"M1 读数：{logmag(g):.2f}dB  {phase(g):.1f}°  SWR={swr(g):.4f}")
g2 = measured[80]
print(f"Δ(30→80)：{logmag(g)-logmag(g2):+.2f}dB")
```

2. 把输出与固件同参数下（CH0 LOGMAG、scale 10dB/、refpos 8）屏幕顶部读数逐位对比。

**需要观察的现象**：LOGMAG 与 SWR 的数值能在小数点后两位内对上；相位有 \( \pm360° \) 整数倍的歧义（固件 `phase()` 用 `atan2f` 折到 ±180°，见 [plot.c:433-437](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L433-L437)）。

**预期结果**：`g = 0.1-0.2j` 时，`logmag ≈ -13.01dB`、`phase ≈ -63.43°`、`swr ≈ 1.6559`。真机对比结果待本地验证（依赖你的 DUT 与校准状态）。

#### 4.4.5 小练习与答案

**练习 1**：`cell_draw_marker_info` 为什么只在 `n == 0` 时被调用（[plot.c:1353-1356](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1353-L1356)）？信息带高 32px，而 cell 高也是 32px，有没有越界风险？

答案：信息带画在绘图区顶部，只覆盖第 0 行 cell，所以在 `draw_cell(m, 0)` 里触发一次即可。cell 高 `CELLHEIGHT=32` 与 `markmap_upperarea` 标脏的 0..31px 恰好同高，文字 y 从 1 开始、行距 8，最多 4 行占 33px 时最底行可能被裁掉一像素——`cell_drawstring` 对越界行做了 `continue` 防护（[plot.c:1489](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1489)），只是不画，不会写越界内存。

**练习 2**：多 marker 模式下读数用 `trace[t]`（当前轨迹）统一渲染所有 marker 的数值，单 marker 模式却按轨迹各自的格式渲染。两种模式分别适合什么场景？

答案：多 marker 模式适合「同一物理量上多点比较」（如滤波器通带内几个 marker 比插损），统一格式便于横向对比，还支持 delta；单 marker 模式适合「一个频点上看多个物理量」（该频点的 S11 幅度、相位、阻抗同时读出）。

**练习 3**：`buf[24]` 这么小的缓冲，RLC 格式的 `"%FΩ %FF"` 之类输出塞得下吗？

答案：`%F` 是 plot_printf 的自适应单位格式，把数值压成 3~4 位有效数字加单位后缀，整行通常不超过 16 字符；24 字节是按最坏情况留的。这也是嵌入式省 RAM 的典型取舍——用精度上限换栈空间。

## 5. 综合实践：给固件添加一个「min-SWR」搜索模式

这是一个贯穿本讲三个模块的扩展任务：**仿照 `set_marker_search` 的比较器机制，在值域（而非屏幕坐标域）实现最小驻波比搜索，并挂进 UI 菜单**。有真机的读者走完整链路；没有硬件的读者用 Python 路径验证算法。

### 5.1 动机分析（先想清楚再动手）

4.2 的练习 1 已经指出：把轨迹切成 SWR 格式后按 MINIMUM，交互上等价于 min-SWR。那这个实践还有什么意义？有——现有实现有两个真实弱点：

1. **削顶并列**：SWR 很小的点在屏幕上挤在最底部（或被钳出屏），并列时取最先出现者，不一定是真正最小；
2. **依赖当前轨迹格式**：搜索发生在 `uistat.current_trace` 的显示坐标上，若当前轨迹是 LOGMAG，就搜不到 min-SWR。

值域搜索直接对 `measured[trace[t].channel][i]` 调 `swr()`，两个问题一起解决。

### 5.2 固件侧步骤（有真机）

1. **plot.c 加函数**（放在 `marker_search` 附近，示例代码——你需要自己动手加入，本讲义不改动仓库）：

```c
/* 示例代码：值域最小 SWR 搜索，返回频点索引，无有效点返回 -1 */
int
marker_search_swr_min(int t)
{
  int i, found = -1;
  float best = INFINITY;
  float (*array)[2] = measured[trace[t].channel];
  for (i = 0; i < sweep_points; i++) {
    float v = swr(array[i]);           /* plot.c:467，|Γ|>=1 返回 INFINITY */
    if (v < best) { best = v; found = i; }
  }
  return found;
}
```

   同时在 nanovna.h 的 marker 原型区（[nanovna.h:283-288](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L283-L288)）补一行声明。

2. **ui.c 挂菜单**：在 `menu_marker_search[]` 表（[ui.c:994-1003](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L994-L1003)）TRACKING 之前插入一项 `{ MT_CALLBACK, 0, "MIN SWR", menu_marker_search_cb }`，并在回调（[ui.c:756-786](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L756-L786)）的 switch 里为**新的 item 序号**（注意：插入位置会使原 item 2/3/4 顺移为 3/4/5，LEFT/RIGHT/TRACKING 的 case 必须同步改号）增加分支：调 `marker_search_swr_min(uistat.current_trace)`，并像 LEFT/RIGHT 一样 `uistat.marker_tracking = false`。

3. **编译烧录**：按 u1-l2 的流程 `make` → `make flash`。

4. **验证**：接一个已知谐振频率的 LC 谐振器或天线，把某条轨迹设为 SWR 格式；点 MENU → MARKER → SEARCH → MIN SWR，观察 marker 是否跳到谐振点、屏幕读数是否为该轨迹的最小驻波比；再故意把当前轨迹切成 LOGMAG 重试，确认新菜单项**不依赖**显示格式（这正是与 MINIMUM 的差异）。烧录验证待本地完成。

### 5.3 Python 侧步骤（无硬件）

用 `data 0` 导出的数据（或模拟数据）验证算法等价性：

```python
# min-SWR 值域搜索 vs 屏幕坐标域搜索的对比（示例代码）
import cmath
def swr(g):
    x = abs(g)
    return float('inf') if x >= 1 else (1+x)/(1-x)

# 模拟一条深谐振曲线：谐振点索引 62，Γ 很小
data = [0.3*cmath.exp(2j*cmath.pi*i/101) for i in range(101)]
data[62] = 0.02 + 0.01j
data[61] = data[63] = 0.05 - 0.05j          # 制造屏幕上并列的次低点

swrs   = [swr(g) for g in data]
print("值域 argmin:", min(range(101), key=lambda i: swrs[i]))   # 期望 62

# 屏幕坐标域：SWR 值映射到 y 后并列会取先出现者
def y_of(v):  return round(232 * (1 - min(v, 4)/4))             # 4 格满量程示意
ys = [y_of(v) for v in swrs]
best = 0
for i in range(1, 101):
    if ys[i] < ys[best]: best = i
print("坐标域 argmin:", best)                                    # 期望 61（并列取先）
```

**需要观察的现象**：两种方法给出不同索引（62 vs 61）——这就是坐标域搜索在浅量程、密集点场景下的削顶并列效应，也正是值域搜索的价值证明。

**预期结果**：如上注释所示；具体数值取决于你构造的曲线，待本地验证。

## 6. 本讲小结

- **marker_t 用「索引-频率双字段」**：`index` 是与 `measured`/`frequencies`/`trace_index` 三个数组直接对齐的权威字段；`frequency` 是改扫描范围后找回索引的锚点（`update_marker_index` 就近取整、越界钳制，中点用折半相加防 `uint32_t` 溢出）。
- **极值搜索发生在屏幕坐标缓存上**：`marker_search` 系列只比较 `CELL_Y(trace_index[t][i])`，借「值大 y 小」的反转关系与 `greater`/`lesser` 函数指针，一套代码覆盖全部矩形格式；`marker_search_left/right` 是「先下坡后上坡」的两阶段局部峰值搜索。
- **tracking 是每趟扫描后的重新吸附**：`plot_into_index` 刷新坐标 → `marker_search` 重贴 → `REDRAW_MARKER` 请求重绘；源码中不存在名为 REFLOW 的标识符，大纲中的说法指的就是这条联动。
- **定位与搜索共用 `trace_index`**：`marker_position` 查坐标供绘制与 20px 命中判定，`search_nearest_index` 用阈值剪枝的最近邻把触摸点映回频点，拖动/拨轮路径都遵守双字段同步纪律。
- **读数与绘图复用同一组换算函数**：`trace_get_value_string` 系列按 12 种格式求值，`cell_draw_marker_info` 分多 marker/单 marker 两套布局把文本画进顶部信息带，其重绘时机由 `REDRAW_MARKER` + `markmap_upperarea` 控制。
- **两处诚实提醒**：nanovna.h:264 的 `extern int8_t marker_tracking` 是没有定义的过时声明（真实状态在 `uistat.marker_tracking`）；`trace_get_value_string_delta` 的 DELAY/R/X/Q 分支并未真正做差。

## 7. 下一步学习建议

本讲多次出现 `redraw_marker`「只重画脏 cell、再申请全屏兜底」、`markmap_upperarea` 硬编码标脏 0..31px、`draw_all` 按位消费 `REDRAW_*` 标志——这些机制的完整原理正是下一讲 **u4-l4「markmap 脏矩形重绘机制」**的主题，建议接着精读 `plot.c` 的 markmap 双页交替与 `draw_cell` 渲染管线。之后再进入 **u4-l5（ui.c 触摸与菜单树）**看 `menu_marker_search_cb` 这类回调在事件循环中的完整生命周期；若你对「读数数字怎么变成像素」更感兴趣，可先回看 u4-l1 的 `cell_drawstring` 与字体位图一节。最后一个巩固练习：合上讲义，画出从「手指按下」到「读数刷新」的完整调用图（应包含 `touch_pickup_marker`、`drag_marker`、`search_nearest_index`、`redraw_marker`、`draw_all_cells`、`cell_draw_marker_info` 六个节点）。
