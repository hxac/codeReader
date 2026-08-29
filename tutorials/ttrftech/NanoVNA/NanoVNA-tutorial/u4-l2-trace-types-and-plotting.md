# 轨迹系统:12 种显示格式与坐标换算

## 1. 本讲目标

学完本讲,你应该能够:

1. 说出 `trace_t` 结构中 `enabled / type / channel / scale / refpos` 五个字段各自的含义,以及它们如何随 `properties_t` 一起掉电保存。
2. 逐行读懂 [plot.c](../../plot.c) 中的 `trace_into_index()`:理解 LOGMAG、PHASE、DELAY、SMITH、POLAR、LINEAR、SWR、REAL、IMAG、R、X、Q 共 12 种显示格式如何把 `measured[]` 里的一个复数换算成屏幕像素坐标 `(x, y)`。
3. 理解 `scale`(每格代表多少物理量)与 `refpos`(参考线距底部几格)这对参数的语义,以及 Smith/Polar 格式下 `scale` 变成"满量程"的特殊约定。
4. 读懂 `trace_get_value_string()` / `trace_get_info()` 如何把轨迹当前值格式化成屏幕顶部的读数字符串。
5. 读懂 `update_grid()` 如何按 1-2-5 序列自适应选取纵向网格步长,以及 `smith_grid()` / `polar_grid()` 如何用"逐像素分类"画出圆图网格。

## 2. 前置知识

本讲建立在前几讲已建立的心智模型之上,先快速回顾:

- **measured 数组是唯一数据源**:一次扫频的最终产物是 `measured[2][101][2]`——2 个通道、101 个频点、每点一对 float(实部/虚部),即每点一个复数 \( \Gamma \)(u2-l1、u2-l4)。
- **屏幕没有帧缓冲**:16KB SRAM 放不下 320×240×2 字节的整屏图像,所以一切绘制都拆成 64×32 的 cell,先画进复用的 `spi_buffer` 再上屏(u4-l1)。
- **绘制是两级流水**:sweep 线程扫完一帧后调用 `plot_into_index(measured)` 把轨迹坐标算好缓存进 `trace_index[]`,再由 `draw_all()` 按脏标记逐 cell 光栅化(u2-l5)。本讲只管"坐标怎么算、网格怎么画",脏矩形机制留给 u4-l4。
- **屏幕坐标系**:原点在绘图区左上角,\( x \) 向右、\( y \) 向下。绘图区宽 `WIDTH=300` 像素、高 `HEIGHT=232` 像素,纵向均分 8 格(`NGRIDY=8`),每格 `GRIDY=29` 像素。
- **术语**:dB(分贝对数幅度)、驻波比 VSWR、群延迟、Smith 圆图(以 \( \Gamma \) 平面上的等电阻圆/等电抗圆族为背景的阻抗图)、极坐标图。不熟悉的读者重点记住:同一个 \( \Gamma \) 可以用多种"透镜"观察,这正是本讲主题。

## 3. 本讲源码地图

| 文件 | 本讲关注的内容 |
|---|---|
| [plot.c](../../plot.c) | 核心:`trace_into_index()`、12 种格式的一组换算函数、`trace_get_value_string()` 系列、`update_grid()` 与三种网格绘制函数 |
| [main.c](../../main.c) | `trace_t` 的默认值表 `def_trace`、格式默认参数表 `trace_info`、`set_trace_type()` 等 setter、`cmd_trace` / `cmd_data` 两个 shell 命令、sweep 线程里调用 `plot_into_index` 的位置 |
| [nanovna.h](../../nanovna.h) | `trace_type` 枚举、`trace_t` 结构、`WIDTH/HEIGHT/NGRIDY/GRIDY/P_CENTER/P_RADIUS` 等布局常量、`trace` 别名宏 |

## 4. 核心概念与源码讲解

### 4.1 轨迹的数据结构与默认配置:trace_t、def_trace 与 trace_info

#### 4.1.1 概念说明

NanoVNA 最多同时显示 4 条**轨迹(trace)**。每条轨迹不是数据的一份拷贝,而是一组"观察方式":从哪个通道取数、用什么格式显示、每格多大、参考线放哪里。数据永远只有 `measured[]` 一份,轨迹只是视图。

这组视图配置存在 `trace_t` 结构里,而 `trace` 数组通过别名宏挂在整个测量现场快照 `properties_t` 下([nanovna.h:403](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L403)),所以轨迹设置会随校准槽一起被 `caldata_save` 写进 flash 掉电保存(u3-l4)。

#### 4.1.2 核心流程

```
trace[t](视图配置) ──┐
                     ├─→ trace_into_index(t, i, measured[ch]) ──→ trace_index[t][i](像素坐标)
measured[ch][i](数据)─┘
```

- 12 种格式由 `enum trace_type` 枚举编号 0~11,`TRC_OFF` 表示关闭该轨迹(不算格式)。
- 其中 10 种是"矩形图"格式(有横轴频率、纵轴物理量),用 `RECTANGULAR_GRID_MASK` 一次性标注;SMITH 和 POLAR 是平面图。

#### 4.1.3 源码精读

轨迹类型枚举与矩形格式掩码——注释里的顺序就是 `trace_info` 表的顺序:

- [nanovna.h:196-L200](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L196-L200) 定义 `TRC_LOGMAG=0 … TRC_Q=11, TRC_OFF` 共 13 个枚举值(12 种格式 + 关闭),以及 `RECTANGULAR_GRID_MASK`(10 个矩形格式按位或)。

`trace_t` 结构,五个有效字段一目了然:

- [nanovna.h:212-L219](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L212-L219) 定义 `enabled / type / channel / reserved / scale / refpos`;其上方 [nanovna.h:202-L207](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L202-L207) 的注释说明了各格式的 SCALE/REFPOS 约定。

出厂默认的 4 条轨迹:

- [main.c:805-L810](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L805-L810) `def_trace[]`:轨迹 0 = CH0 LOGMAG 10dB/格 参考线在第 7 格(距底),轨迹 1 = CH1 LOGMAG,轨迹 2 = CH0 SMITH,轨迹 3 = CH1 PHASE 90°/格。它被 `load_default_properties()` 整体 `memcpy` 进 `current_props._trace`([main.c:830](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L830)),仅在 flash 中无有效存档或清配置时生效。

每种格式的默认 scale/refpos 集中在 `trace_info` 表里,表序与枚举序严格一致(可用下标当类型号):

- [main.c:1552-L1569](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1552-L1569) 每项给出 `{ 名称, 默认 refpos, 默认 scale }`,如 LOGMAG 是第 `NGRIDY-1=7` 格、10.0 dB/格;SWR 是第 0 格、0.25/格;R/X 是 100Ω/格。

切换格式时由 `set_trace_type()` 自动把 refpos/scale 重置为该格式的默认值,并立即重算轨迹坐标:

- [main.c:1580-L1601](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1580-L1601) 类型变化时 `trace[t].refpos = trace_info[type].refpos; trace[t].scale = trace_info[type].scale_unit;`,然后 `plot_into_index(measured)` + `force_set_markmap()` 强制全屏重绘。

从 sweep 线程到坐标缓存的入口:

- [main.c:131-L135](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L131-L135) 一次扫描完成后(时域模式先做 `transform_domain()`,u3-l5)调用 `plot_into_index(measured)` 并置 `REDRAW_CELLS`。
- [plot.c:1191-L1211](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1191-L1211) `plot_into_index()` 对每条 enabled 轨迹按 `trace[t].channel` 选出 `measured[ch]`,逐点调用 `trace_into_index()` 填 `trace_index[t][i]`;末尾的 `mark_cells_from_index()`/`markmap_all_markers()` 属于脏矩形机制,留待 u4-l4。

shell 侧的运行时开关是 `trace` 命令:

- [main.c:1637-L1677](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1637-L1677) 无参数时列出每条轨迹的 `编号 格式 通道 scale refpos`;`trace 1 swr` 换格式;`trace 0 scale 10` / `trace 0 refpos 7` 单独调参;类型字符串表在 [main.c:1673](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1673)。

#### 4.1.4 代码实践

1. **实践目标**:在不改代码的前提下,用 shell 命令摸清 `trace_t` 五个字段的运行时表现。
2. **操作步骤**:USB 连接真机,打开串口终端(115200-8N1 的 CDC 口),依次执行:
   ```
   trace              # 查看默认 4 条轨迹
   trace 2 0          # 关闭 SMITH 轨迹(设为 TRC_OFF)
   trace 1 swr        # 把 CH1 轨迹换成 SWR 格式
   trace              # 再看一遍,scale/refpos 已被 set_trace_type 重置
   trace 1 logmag     # 换回 LOGMAG,确认 scale/refpos 又回到 10.0 / 7.0
   ```
3. **需要观察的现象**:每次切换格式后屏幕顶部的读数字符串(如 `CH1 SWR 0.25/`)与曲线形状变化;`trace` 回显中 scale/refpos 的自动重置。
4. **预期结果**:切换到 `swr` 后回显 `1 SWR CH1 0.250000 0.000000`,与 `trace_info` 表的 `{"SWR", 0, 0.25}` 一致。无真机的读者可只做源码推演,标注"待本地验证"。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `trace_info[]` 的数组下标可以直接用 `trace[t].type` 索引?
**答案**:该表按 `enum trace_type` 的枚举顺序逐项排列(LOGMAG=0 在最前,Q=11 在最后),枚举值即下标;[main.c:1669-L1671](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1669-L1671) 还用 `#if MAX_TRACE_TYPE != 12 #error` 编译期断言防止有人改动枚举后忘记同步。

**练习 2**:`trace` 数组为什么能掉电保存?保存的是数据还是视图?
**答案**:因为 `trace` 是 `current_props._trace` 的别名宏,而 `properties_t` 是 caldata 槽的完整快照,`caldata_save` 会把它写入 flash;保存的只是"视图配置"(5 个小字段),测量数据 `measured[]` 本身不保存,开机后重新扫频生成。

**练习 3**:固件同时显示 4 条轨迹时,屏幕上会有几份数据?
**答案**:仍只有一份数据(`measured[2][101][2]`);4 条轨迹只是 4 组视图参数,`plot_into_index` 按各自的 `channel/type/scale/refpos` 从同一份数据算出 4 组坐标。

### 4.2 trace_into_index:12 种格式的坐标换算

#### 4.2.1 概念说明

`trace_into_index()` 是整个显示子系统的"翻译官":输入一个频点上的复数 \( \Gamma \) 和一条轨迹的视图配置,输出这个点在屏幕上的像素坐标(打包成一个 32 位 `index_t`,高 16 位存 x、低 16 位存 y,供 u4-l4 的 cell 绘制快速取用)。

理解它的钥匙是两个参数的语义:

- **scale = 每格物理量**(如 LOGMAG 默认 10 dB/格)。代码里先取倒数 `scale = 1/trace.scale`,得到"物理量→格数"的换算系数。
- **refpos = 参考线距屏幕底部的格数**(LOGMAG 默认 7,即离顶 1 格)。代码先翻转成"距顶部格数":`refpos = NGRIDY - trace.refpos`,因为屏幕 \( y \) 向下。

对 Smith/Polar 格式,scale 的含义变为**满量程**:\( |\Gamma| = \text{trace.scale} \) 对应圆图外圆半径 `P_RADIUS`,`trace_get_info()` 打印时也用 `FS`(Full Scale)标注。

#### 4.2.2 核心流程

矩形格式的统一骨架(伪代码):

```
v = NGRIDY - refpos            # 参考线位置,单位:格(距顶部)
s = 1 / trace.scale            # 物理量 → 格
按格式计算物理量 value:
    v -= value * s             # 大多数格式:值越大越靠上
    (LINEAR/SWR 用 v +=,见下文符号技巧)
v 夹到 [0, NGRIDY]
x = round(i/(N-1) * WIDTH) + CELLOFFSETX    # 频点序号 → 像素列
y = round(v * GRIDY)                        # 格 → 像素
```

12 种格式的物理量与换算方向一览(`s = 1/scale_per_div`):

| 格式 | 物理量 | 代码算式 | 默认 scale/格 | 默认 refpos | 方向 |
|---|---|---|---|---|---|
| LOGMAG | \( 20\log_{10}\|\Gamma\| \) (dB) | `v -= logmag(g)*s` | 10 | 7 | 值大向上 |
| PHASE | \( \angle\Gamma \) (度) | `v -= phase(g)*s` | 90 | 4 | 正相位向上 |
| DELAY | 相邻点相位差/\( 2\pi\Delta f \) (s) | `v -= groupdelay*s` | 1e-9 | 4 | — |
| LINEAR | \( -\|\Gamma\| \) | `v += linear(g)*s` | 0.125 | 0 | 模大向上 |
| SWR | \( (1+\|\Gamma\|)/(1-\|\Gamma\|) \) | `v += (1-swr(g))*s` | 0.25 | 0 | SWR 大向上 |
| REAL | \( \mathrm{Re}\,\Gamma \) | `v -= re*s` | 0.25 | 4 | — |
| IMAG | \( \mathrm{Im}\,\Gamma \) | `v -= im*s` | 0.25 | 4 | — |
| R | \( \mathrm{Re}\,Z,\ Z=50\frac{1+\Gamma}{1-\Gamma} \) (Ω) | `v -= resitance(g)*s` | 100 | 4 | — |
| X | \( \mathrm{Im}\,Z \) (Ω) | `v -= reactance(g)*s` | 100 | 4 | — |
| Q | \( \|X/R\| \) | `v -= qualityfactor(g)*s` | 10 | 0 | — |
| SMITH | 复平面点位 | `cartesian_scale(re, im)` | 1.0(满量程) | — | 虚部向上 |
| POLAR | 复平面点位 | 同 SMITH | 1.0(满量程) | — | 虚部向上 |

两个容易看走眼的**符号技巧**:

- `linear()` 返回的是**负的** \( \|\Gamma\| \),于是 `v += linear*s` 与其他格式的 `v -=` 在视觉方向上保持一致(模越大越靠上)。
- SWR 对无源负载恒有 \( \mathrm{SWR}\ge 1 \),代码用 `(1 - swr)`:完美匹配(SWR=1)正好落在参考线(默认底部第 0 格),失配越严重越向上。

Smith/Polar 的坐标由 `cartesian_scale()` 单独处理:

\[
x = P_{cx} + \mathrm{clamp}(\mathrm{Re}\,\Gamma \cdot R \cdot s,\ [-R, R]),\qquad
y = P_{cy} - \mathrm{clamp}(\mathrm{Im}\,\Gamma \cdot R \cdot s,\ [-R, R])
\]

其中 \( R \) = `P_RADIUS` = 116 像素,圆心 \( (P_{cx}, P_{cy}) = (155, 116) \);虚部取负号是因为屏幕 \( y \) 向下。\( x \) 方向不随频点变化(整条轨迹是平面上的一条曲线),横轴频率信息只能靠 marker 读数(u4-l3)。

#### 4.2.3 源码精读

`trace_into_index()` 主体——注意开头两行的 refpos 翻转与 scale 取倒数:

- [plot.c:541-L593](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L541-L593) `switch (trace[t].type)` 逐格式把物理量折算成格数 `v`;SMITH/POLAR 分支走 `cartesian_scale()` 后 `goto set_index` 跳过矩形格式的钳制;最后
  ```c
  x = (i * (WIDTH) + (sweep_points-1)/2) / (sweep_points-1) + CELLOFFSETX;
  y = float2int(v * GRIDY);
  ```
  \( x \) 的整数映射先加 \( (N-1)/2 \) 再整除,等效于对 \( \frac{i}{N-1}\cdot W \) 四舍五入;`float2int()`([plot.c:75-L81](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L75-L81))按"远离零方向四舍五入"取整。

对数幅度与相位——幅度用模的平方省去开方:

- [plot.c:424-L428](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L424-L428) `logmag() = log10f(re²+im²)*10`,即 \( 10\log_{10}|\Gamma|^2 = 20\log_{10}|\Gamma| \),Cortex-M0 无 FPU,省一次 `sqrtf` 是净赚。
- [plot.c:433-L437](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L433-L437) `phase() = 2*atan2f(im,re)/π*90`,即把弧度换算成度的另一种写法。

线性幅度与驻波比——注意负号技巧:

- [plot.c:458-L462](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L458-L462) `linear()` 返回 `-sqrtf(re²+im²)`。
- [plot.c:467-L474](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L467-L474) `swr()` 在 \( |\Gamma|\ge 1 \)(有源/超界)时返回 `INFINITY`,`(1-∞)*s` 会被钳到屏幕顶端。

阻抗类换算 R/X/Q——\( Z = 50\,(1+\Gamma)/(1-\Gamma) \) 的展开:

- [plot.c:476-L492](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L476-L492) `resitance()`/`reactance()`(注意源文件里 resistance 就拼作 `resitance`)先算公共分母 `d = 50/((1-re)²+im²)`,分子分别为 \( (1-re^2-im^2)\,d \) 与 \( 2\,im\,d \),把一次复数除法化成一次实数除法加乘法。
- [plot.c:494-L500](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L494-L500) `qualityfactor()` = \( |2\,im / (1-re^2-im^2)| \),正是 \( |X/R| \)(R、X 的公共因子 \( d \) 被约掉)。

群延迟——用相邻频点的相位差分:

- [plot.c:516-L523](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L516-L523) `groupdelay_from_array()` 取左右邻点(边界处钳制),`deltaf = frequencies[top]-frequencies[bottom]`。
- [plot.c:442-L453](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L442-L453) 用叉积/点积的 `atan2f` 求相位差,避免两个 `atan2` 相减时的 \( \pm\pi \) 卷绕跳变。

Smith/Polar 的坐标换算:

- [plot.c:502-L514](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L502-L514) `cartesian_scale()` 把 \( \mathrm{Re},\mathrm{Im} \) 各乘 \( R\cdot s \)、钳到 \( \pm R \),再加圆心得屏幕坐标,虚部方向取负。
- 布局常量在 [nanovna.h:137-L165](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L137-L165):`WIDTH=300`、`HEIGHT=232`、`NGRIDY=8`、`GRIDY=HEIGHT/NGRIDY=29`、`CELLOFFSETX=5`、`P_CENTER_X=CELLOFFSETX+WIDTH/2`、`P_CENTER_Y=P_RADIUS=HEIGHT/2`。

#### 4.2.4 代码实践

1. **实践目标**:在 PC 上用 Python 完整复现 `trace_into_index()` 的矩形格式部分,把同一份 \( \Gamma \) 数据按 LOGMAG / PHASE / SWR 三种格式换算成屏幕 y 像素并绘图,建立"数据↔屏幕"的精确手感。
2. **操作步骤**:保存以下**示例代码**为 `trace_y.py` 并运行(无硬件也能做;有真机者可把 `sim_gamma()` 换成从 `data 0` 导出的实测数据,格式为每行两个 float:实部 虚部,见 [main.c:682-L701](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L682-L701)):
   ```python
   # 示例代码:复现 plot.c trace_into_index() 的矩形格式换算
   import numpy as np, matplotlib.pyplot as plt

   WIDTH, HEIGHT, NGRIDY, CELLOFFSETX = 300, 232, 8, 5   # nanovna.h
   GRIDY, N = HEIGHT // NGRIDY, 101                      # GRIDY=29, POINTS_COUNT=101

   def float2int(v):                                     # plot.c L75:远离零四舍五入
       return np.where(v < 0, np.floor(v - 0.5), np.floor(v + 0.5)).astype(int)

   def trace_y(g, ttype, scale_per_div, refpos):         # 矩形格式部分
       v = NGRIDY - refpos                               # 距底格数 -> 距顶格数
       s = 1.0 / scale_per_div                           # 物理量 -> 格
       re, im = g.real, g.imag
       if ttype == 'logmag':
           v = v - 10*np.log10(re*re + im*im) * s        # plot.c L424
       elif ttype == 'phase':
           v = v - np.degrees(np.arctan2(im, re)) * s    # plot.c L433
       elif ttype == 'swr':
           x = np.hypot(re, im)
           swr = np.where(x >= 1, np.inf, (1+x)/(1-x))   # plot.c L467
           v = v + (1 - swr) * s
       return float2int(np.clip(v, 0, NGRIDY) * GRIDY)

   def sim_gamma():                                      # 模拟串联 RLC 谐振器的 S11
       f = np.linspace(50e6, 150e6, N)                   # 50~150MHz
       Z = 5 + 1j*(2*np.pi*f*100e-9 - 1/(2*np.pi*f*25.33e-12))
       return (Z - 50)/(Z + 50)

   g = sim_gamma()
   xpix = (np.arange(N)*WIDTH + (N-1)//2)//(N-1) + CELLOFFSETX
   for ttype, spd, rp in [('logmag', 10.0, 7), ('phase', 90.0, 4), ('swr', 0.25, 0)]:
       ypix = trace_y(g, ttype, spd, rp)
       plt.plot(xpix, ypix, label=f'{ttype} scale={spd} refpos={rp}')
   plt.gca().invert_yaxis()                              # 屏幕 y 向下
   plt.xlabel('screen x (px)'); plt.ylabel('screen y (px)'); plt.legend(); plt.show()
   ```
3. **需要观察的现象**:三条曲线共用同一横轴(频点→像素列);LOGMAG 在谐振点 100MHz 附近急速**向上**凹陷(|Γ| 最小);PHASE 穿越谐振点时发生 180° 突跳,对应曲线从顶端跳到底端(被钳制在 0/NGRIDY 边界);SWR 在谐振点贴近 \( y=8\times29=232 \)(底部参考线),失配处向上抬升。
4. **预期结果**:LOGMAG 曲线的最高点像素 \( y \) 接近 `refpos*GRIDY = 7*29 = 203`(参考线);SWR 曲线最低点接近 232。与真机对比时,在仪器上设置相同扫描范围与格式(菜单或 `trace`/`sweep` 命令),曲线形状应一致到"逐像素"级别(用第 5 节综合实践的 `capture` 截图可做叠加比对)。以上为推演结果,真机数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**:LOGMAG 格式、scale=10dB/格、refpos=7 时,参考线在屏幕哪个像素?\( \Gamma \) 为多少时曲线恰好压在参考线上?
**答案**:\( v = 8-7 = 1 \) 格(距顶),\( y = 1\times29 = 29 \) 像素。\( 20\log_{10}|\Gamma| = 0 \) 即 \( |\Gamma|=1 \)(全反射,开路/短路)时曲线在参考线上——这正是把参考线放在离顶 1 格的用意:0dB 是最常见的工作点。

**练习 2**:SWR 格式下 \( |\Gamma| = 1/3 \) 的点画在哪里(scale=0.25,refpos=0)?
**答案**:\( \mathrm{SWR} = (1+1/3)/(1-1/3) = 2 \);\( v = 8 + (1-2)\times(1/0.25) = 4 \) 格,即屏幕正中 \( y=116 \)。

**练习 3**:为什么 Smith/Polar 分支要 `goto set_index` 跳过 `v` 的钳制和 \( x \) 的频率映射?
**答案**:这两种格式的横纵坐标都来自 \( \Gamma \) 本身(由 `cartesian_scale()` 内部钳到 \( \pm R \)),横轴不再是频率,频点序号映射 \( x \) 无意义;且不存在"参考线格数"的概念,`v` 钳制也不适用。

**练习 4**:把 SMITH 轨迹的 scale 从 1.0 改成 0.5,圆图上的轨迹会怎么变?
**答案**:等效于放大 2 倍:\( s=1/0.5=2 \),\( \Gamma=0.5 \) 的点就会画到外圆上,圆图"装不下"的部分被钳在外圆圆周上;`trace_get_info()` 会显示 `SMITH 0.5FS`。

### 4.3 轨迹读数的格式化字符串

#### 4.3.1 概念说明

坐标换算解决"曲线画在哪",`trace_get_value_string()` 系列解决"marker 处读数显示什么数字"。屏幕顶部一行典型内容是 `CH0 LOGMAG 10dB/ -12.34dB`,由三段拼成:通道号、`trace_get_info()` 的格式/每格标签、`trace_get_value_string()` 的当前值。所有字符串都通过 `plot_printf()`(main.c 提供的精简 printf)写进 24 字节小缓冲,再由 cell 字体渲染上屏。

注意显示值与绘图值用的是**同一组换算函数**(`logmag/phase/swr/...`),只是单位不同(物理量 vs 格数)——一处实现两处复用,这是 plot.c 值得学习的组织方式。

#### 4.3.2 核心流程

```
cell_draw_marker_info()                       # plot.c,画在 n==0 的 cell 顶部
   ├─ plot_printf("CH%d", channel)
   ├─ trace_get_info(t)                       # "LOGMAG 10dB/" / "SMITH 1.0FS" / "R 100/"
   └─ trace_get_value_string(t, measured[ch], idx)   # "-12.34dB" ...
        └─(SMITH 格式) format_smith_value()  # 按 marker_smith_format 五选一
```

#### 4.3.3 源码精读

- [plot.c:640-L697](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L640-L697) `trace_get_value_string()` 与 `trace_into_index()` 同构的 switch:每种格式给定 `format` 字符串和 `v`(复用 4.2 节那组换算函数),如 LOGMAG 用 `"%.2fdB"`、SWR 用 `"%.4f"`、R/X 用 `"%.2F"S_OHM`;SMITH 分支转交 `format_smith_value()`。
- [plot.c:595-L638](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L595-L638) `format_smith_value()` 按 `marker_smith_format`(枚举 `MS_LIN/MS_LOG/MS_REIM/MS_RX/MS_RLC`,见 [nanovna.h:443-L446](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L443-L446),默认 `MS_RLC`,[main.c:835](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L835))五种样式显示同一点:幅相、dB+相位、实虚部、R±jX、以及 R + 串联 L(感抗 \( X>0 \):\( L=X/(2\pi f) \))或 C(容抗 \( X<0 \):\( C=-1/(2\pi f X) \))。
- [plot.c:699-L759](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L699-L759) `trace_get_value_string_delta()` 是差值读数版本(对参考 marker 求差,SWR 的 ∞ 做了特判),`S_DELTA` 是小三角字形转义符。
- [plot.c:761-L782](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L761-L782) `trace_get_info()` 生成格式标签:LOGMAG/PHASE 用整数每格(`10dB/`、`90°/`),SMITH/POLAR 在 scale≠1 时显示 `%.1fFS`,其余用 `%F/`。
- 格式串里的 `%F`(紧凑浮点,去尾零)与 `%q`(带 SI 词头的量值)是本固件对 chprintf 的扩展,实现见 [chprintf.c:370-L393](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chprintf.c#L370-L393);`S_DEGREE`/`S_OHM` 等是字体里的专用字形([nanovna.h:182-L190](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L182-L190))。
- 调用点在 [plot.c:1499-L1585](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1499-L1585) `cell_draw_marker_info()`:它以 `frequencies[markers[mk].index]` 显示频率、以 `trace_get_value_string(t, buf, sizeof buf, measured[trace[t].channel], idx)` 显示当前轨迹在该 marker 处的读数。

#### 4.3.4 代码实践

1. **实践目标**:用 Python 复现 `format_smith_value()` 的 RX 与 RLC 两种样式,学会"从 \( \Gamma \) 直接读阻抗与等效 L/C"。
2. **操作步骤**(示例代码):
   ```python
   import numpy as np
   def smith_RX(g):                                  # plot.c L595: z = 50*(1+g)/(1-g)
       re, im = g.real, g.imag
       d = 50.0 / ((1-re)**2 + im**2)
       return ((1+re)*(1-re) - im*im) * d, 2*im * d  # zr, zi
   def smith_RLC(g, f):
       zr, zi = smith_RX(g)
       if zi < 0:  return f"{zr:.2f}Ω {-1/(2*np.pi*f*zi):.3f}F"
       else:       return f"{zr:.2f}Ω { zi/(2*np.pi*f):.3f}H"
   g = 0.5 + 0.2j;  f = 10e6
   print(smith_RLC(g, f))                            # 与真机 SMITH 格式 marker 读数比对
   ```
3. **需要观察的现象**:同一 \( \Gamma \) 在 \( X>0 \) 时给出串联电感(H),\( X<0 \) 时给出串联电容(F);频率不同,同样的 \( X \) 对应的 L/C 数值不同。
4. **预期结果**:对 \( g=0.5+0.2j \)、\( f=10\,\mathrm{MHz} \),\( Z\approx 90+40j\Omega \)(读者可手算验证),RLC 显示约 `90.00Ω 0.64µH` 量级的感抗结果;真机读数受校准与实际频率影响,精确数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**:为什么 `trace_get_value_string()` 里 SMITH 格式需要频率参数而其他格式不需要?
**答案**:LIN/LOG/REIM 等只对 \( \Gamma \) 本身做变换;而 RLC 样式要把电抗 \( X \) 换算成 L 或 C,公式 \( L=X/(2\pi f) \)、\( C=-1/(2\pi f X) \) 都依赖该频点的频率,所以函数签名里有 `uint32_t frequency`,调用处传入 `frequencies[index]`。

**练习 2**:读数字符串的缓冲区多大?为什么够用?
**答案**:`cell_draw_marker_info()` 里是 `char buf[24]`。最长的 RLC 样式如 `999.9kΩ 9.99mH` 也在 20 字符以内;16KB SRAM 下为每条轨迹留大缓冲是浪费,24 字节是量过的紧约束。

**练习 3**:`%F` 和 `%f` 有什么区别?
**答案**:`%f` 是标准浮点(默认 9 位精度,见 [chprintf.c:43](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chprintf.c#L43) 的 `FLOAT_PRECISION`),`%F` 走 `ftoaS()` 紧凑输出,自动去掉无意义的尾零,适合小屏幕读数。

### 4.4 update_grid:网格绘制(矩形网格与 Smith 圆图)

#### 4.4.1 概念说明

网格是"图纸"。矩形图的横轴网格必须跟随扫描范围自适应:900MHz 跨度用 200MHz一格,10MHz 跨度就得用 2MHz 一格,否则要么挤成一团要么空空荡荡。`update_grid()` 用经典的 1-2-5 优选数序列选步长,目标是**至少 4 个分度**;`grid_offset`/`grid_width` 两个静态变量记录结果,供 `rectangular_grid_x()` 逐像素判断。

Smith 与 Polar 网格则完全是另一套思路:**没有线段表,只有"像素分类器"**——`smith_grid(x, y)` 对每个像素回答"你是否落在某条网格线上",画 cell 时逐像素调用。这避开了在小内存上生成/存线段列表的开销,代价是每格 ~2000 次整数判断(源码注释实测整屏约 1000 系统滴答,可接受)。

#### 4.4.2 核心流程

```
update_grid():                       # 频率变化时由 update_frequencies() 调用
    gdigit = 1e8
    循环:依次尝试 5×gdigit、2×gdigit、1×gdigit
        若 fspan/grid >= 4 → 选定,gdigit /= 10 继续
    grid_offset = WIDTH·((fstart mod grid)/100)/(fspan/100)     # 起点相位偏移(像素)
    grid_width  = WIDTH·(grid/100)/(fspan/1000)                 # 步长,单位 0.1 像素!
    force_set_markmap() + REDRAW_FREQUENCY
```

注意 `grid_width` 的量纲陷阱:分子分母的预除(防 32 位溢出)不对称(`/100` 与 `/1000`),使结果恰好是**像素间距的 10 倍**;配套地,`rectangular_grid_x()` 里判断条件是 `((x+grid_offset)*10) % grid_width < 10`,两处的 ×10 互相抵消,实际画线间距仍是 \( W\cdot\text{grid}/\text{fspan} \) 像素。单看任何一边都会误以为差了 10 倍。

Smith 网格的两族圆(源码注释 [plot.c:158-L161](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L158-L161)):

\[
\text{等电阻圆:}\ (u-\tfrac{r}{r+1})^2 + v^2 = \tfrac{1}{(r+1)^2},
\qquad
\text{等电抗圆:}\ (u-1)^2 + (v-\tfrac{1}{x})^2 = \tfrac{1}{x^2}
\]

固件画了等电阻圆 \( r=3,1,1/3 \) 和等电抗弧 \( x=\pm2,\pm1,\pm0.5 \),加外圆与实轴;Polar 画 5 个同心圆(\( r, 4r/5, …, r/5 \))加正交轴与 45° 对角线。

#### 4.4.3 源码精读

- [plot.c:83-L108](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L83-L108) `update_grid()` 的 1-2-5 步长选择与两个布局变量计算;它由 `update_frequencies()` 在每次频率变化后调用([main.c:1005](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1005))。
- [plot.c:363-L373](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L363-L373) `rectangular_grid_x()`:先减 `CELLOFFSETX` 回到绘图区坐标,边界(x=0 或 WIDTH)必画,内部按上述 ×10 取模判断;[plot.c:375-L383](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L375-L383) `rectangular_grid_y()` 每 `GRIDY=29` 像素一条水平线,与频率无关。
- [plot.c:110-L119](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L110-L119) `circle_inout()`:`d = x²+y²−r²` 与 \( \pm r \) 比较,|d|≤r 视为"在圆上"——判据量纲是\( d \) 而非距离,带宽 \( |d|/(2r)\approx 0.5 \) 像素,天然适配任意半径,无需浮点开方。
- [plot.c:162-L207](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L162-L207) `smith_grid()`:先平移到圆心,再把原点挪到圆图右端点(`x -= P_RADIUS`),随后逐圆判断——`circle_inout(x+P_RADIUS/4, y, P_RADIUS/4)` 即等电阻圆 \( r=3 \)(圆心 \( u=3/4 \)、半径 \( 1/4 \),换算到像素正是 R/4),依此类推;先判小圆、命中即返回的顺序兼作加速。
- [plot.c:121-L156](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L121-L156) `polar_grid()`:同心圆从外到内逐个尝试,轴与对角线用整数等式判断。
- [plot.c:1256-L1301](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1256-L1301) `draw_cell()` 里按"当前启用的轨迹类型集合"选网格:所有 enabled 轨迹的类型按位或进 `trace_type`,命中 `RECTANGULAR_GRID_MASK` 画矩形网格;有 SMITH 轨迹画 Smith(优先),否则有 POLAR 才画 Polar——所以矩形图与圆图可以同屏共存(默认 4 轨迹正是 2 条矩形 + 1 条 Smith),而 Smith 与 Polar 互斥。

#### 4.4.4 代码实践

1. **实践目标**:在 PC 上复现 `update_grid()` 的步长选择,验证"任意跨度至少 4 分度"与像素间距计算。
2. **操作步骤**(示例代码):
   ```python
   W = 300
   def choose_grid(fspan):                       # plot.c L90-L101
       gdigit = 100_000_000
       while gdigit > 100:
           for k in (5, 2, 1):
               if fspan // (k*gdigit) >= 4:
                   return k*gdigit
           gdigit //= 10
       return gdigit
   for fspan in (899_950_000, 100_000_000, 10_000_000, 1_000_000):
       g = choose_grid(fspan)
       gw = W*(g//100)//(fspan//1000)            # plot.c L104,"0.1 像素"单位
       print(f"span={fspan:>11,}  grid={g:>11,}Hz  div={fspan/g:.1f}  "
             f"grid_width={gw}  实际间距≈{gw/10:.1f}px")
   ```
3. **需要观察的现象**:跨度每缩一个量级,步长也跟着缩一档;`grid_width/10` 才是真正的像素间距。
4. **预期结果**:约 `span=899,950,000 → grid=200MHz → 4.5 分度 → grid_width=666,间距≈66.6px`;`span=100MHz → 20MHz → 5 分度`;`span=10MHz → 2MHz`;`span=1MHz → 250kHz`(5 分度)。读者可据此在真机上改变 span,数一数屏幕竖线数量是否吻合(待本地验证)。

#### 4.4.5 小练习与答案

**练习 1**:为什么 1-2-5 序列要求"至少 4 分度"而不是"尽量多"?
**答案**:分度太多线会密到干扰读曲线;太少则插值误差大。≥4 是经验下限:1-2-5 序列下实际分度数落在 4~(略小于 20) 之间,视觉密度稳定。

**练习 2**:Smith 与 Polar 轨迹同时启用时屏幕显示哪种圆图?
**答案**:Smith。`draw_cell()` 里 Smith 分支在前、Polar 分支带 `else if`([plot.c:1281-L1291](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1281-L1291)),两者画在同一绘图区,只能二选一。

**练习 3**:`circle_inout()` 为什么用 \( d=x^2+y^2-r^2 \) 与 \( \pm r \) 比较来判"在圆上",而不是算距离?
**答案**:避免开方。\( |d| \le r \) 等价于距离偏差 \( \le r/(\rho+r)\approx 0.5 \) 像素(\( \rho \) 为像素到圆心距离),判据随半径自适应,整数运算即可,符合 M0 无 FPU 的约束。

## 5. 综合实践

把本讲三个模块串起来,做一个"改默认视图 + PC 端像素级比对"的小项目:

1. **修改出厂默认轨迹**:把 [main.c:807](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L807) 的 CH1 默认轨迹从 LOGMAG 改为 SWR:
   ```c
   static const trace_t def_trace[TRACES_MAX] = {//enable, type, channel, reserved, scale, refpos
     { 1, TRC_LOGMAG, 0, 0, 10.0, NGRIDY-1 },
     { 1, TRC_SWR,    1, 0, 0.25, 0 },          // 原: { 1, TRC_LOGMAG, 1, 0, 10.0, NGRIDY-1 }
     ...
   ```
   scale/refpos 照抄 `trace_info[]` 中 SWR 行的默认值(`0` 和 `0.25`),保证与 `set_trace_type()` 运行时切换的效果一致。
2. **编译烧录**:按 u1-l2 的流程 `make` + `make flash`。
3. **让默认值生效**:flash 里若存有旧配置,`def_trace` 不会生效——用串口执行 `clearconfig 1234`([main.c:565-L576](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L565-L576))擦除保存区后复位,`config_recall`/`caldata_recall` 失败回退到编译期默认值(u3-l4)。
4. **验证**:开机后屏幕应有 CH0 LOGMAG、CH1 SWR、CH0 SMITH、CH1 PHASE 四条轨迹,顶部读数出现 `CH1 SWR 0.25/`;串口 `trace` 回显 `1 SWR CH1 0.250000 0.000000`。
5. **像素级比对(可选,需真机)**:串口执行 `scan` 完成一次测量后 `data 0` 导出 101 行实虚部,喂给 4.2.4 的 Python 脚本(`sim_gamma()` 换成 `np.loadtxt`),再把屏幕截图(`capture` 命令,python 端封装见 u5-l2)与脚本输出的像素曲线叠加,检验你对换算公式的理解是否分毫不差。

## 6. 本讲小结

- 轨迹是**视图不是数据**:4 条 `trace_t`(enabled/type/channel/scale/refpos)从同一份 `measured[]` 取数,随 `properties_t` 掉电保存;`trace_info[]` 集中管理 12 种格式的默认 scale/refpos,`set_trace_type()` 切格式时自动重置。
- `trace_into_index()` 的骨架:`v = NGRIDY − refpos`(参考线翻转)+ `s = 1/scale`(物理量→格)+ 按格式加减 + 钳制到 \([0, NGRIDY]\),\( x \) 由频点序号整数四舍五入映射,\( y=v\times29 \) 像素;LINEAR 与 SWR 的 `v +=` 负号技巧统一了"值大向上"的视觉方向。
- Smith/Polar 走 `cartesian_scale()`:\( \Gamma \) 直接映射到以 (155,116) 为圆心、半径 116 的平面,虚部向上;此时 scale 语义变为"满量程 FS"。
- R/X/Q 与 Smith 读数共享 \( Z=50(1+\Gamma)/(1-\Gamma) \) 的实数化展开;读数字符串与绘图复用同一组换算函数。
- `update_grid()` 用 1-2-5 序列保证 ≥4 分度;`grid_width` 的量纲是 0.1 像素,与 `rectangular_grid_x()` 里的 ×10 配对使用。
- Smith/Polar 网格是"逐像素分类器":`circle_inout()` 用 \( d=x^2+y^2-r^2 \) 与 \( \pm r \) 比较实现免开方的半像素带宽圆判断。

## 7. 下一步学习建议

- **u4-l3(标记与搜索)**:`trace_index[]` 缓存好的像素坐标如何被 marker 体系消费——`search_nearest_index()` 反查频点、`marker_search()` 直接在屏幕 y 上做极值搜索,正是本讲坐标换算的直接下游。
- **u4-l4(markmap 脏矩形重绘)**:`plot_into_index()` 末尾的 `mark_cells_from_index()`、`draw_cell()` 的调用时机与双 markmap 交替,补全显示管线的最后一环。
- 建议顺手源码阅读:`plot.c` 中 `search_index_range_x()`(矩形轨迹绘制时如何二分出落在 cell 内的点区间)与 `draw_refpos()`(参考线小三角如何用 `HEIGHT − refpos×GRIDY` 印证 refpos 语义)。
