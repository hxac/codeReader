# 频率配置：start/stop、center/span 与频点表

## 1. 本讲目标

学完本讲，你应该能够：

- 说清固件里「频率」的三层表示：`frequency0/frequency1`（扫描边界）、`frequencies[]`（整数频点表）、`markers[].frequency`（标记频率），以及它们之间谁来推导谁。
- 掌握 `set_sweep_frequency()` 对 ST_START / ST_STOP / ST_CENTER / ST_SPAN / ST_CW 五种设定模式的不同处理与边界钳制（`START_MIN` ~ `STOP_MAX`）。
- 读懂 `set_frequencies()` 用**整数误差扩散**（Bresenham 式）生成 101 个整数频点的算法，并能推导它与 `numpy.linspace` 的关系。
- 理解 `update_frequencies()` 的联动刷新：频点表 → marker 索引 → 网格刻度，以及 `sweep` / `scan` 两个 shell 命令在调用路径上的关键差异。

本讲位于测量链路（u2 系列）之后：u2-l5 已经讲清 `sweep()` 线程逐点消费 `frequencies[i]`；本讲回答「`frequencies[]` 这张表是怎么来的、怎么变的」。

## 2. 前置知识

- **扫描边界与频点表**：VNA 一次测量不是只测一个频率，而是在 `[start, stop]` 区间取 `points` 个离散频点依次测量。NanoVNA 里边界只有两个 32 位整数 `frequency0`（起点）与 `frequency1`（终点），而真正被逐点使用的是数组 `frequencies[POINTS_COUNT]`。
- **为什么频点必须是整数（Hz）**：si5351 时钟发生器的分频寄存器是整数/有理数运算，最终输出的频率以 1 Hz 为最小粒度配置（见 u2-l2）。因此频点表是 `uint32_t` 数组，小数部分必须被「分配」掉——这就是误差扩散要解决的问题。
- **center/span 与 start/stop 是同一事实的两种视角**：center = (start+stop)/2，span = stop-start。射频工程师习惯用「中心频率 + 扫宽」思考，固件两种都支持，用一个标志位 `freq_mode` 记录当前 UI 采用哪种视角。
- **CW（Continuous Wave，单频）模式**：start == stop，span 为 0，仪器固定在单一频率上连续测量，常用于监测某个频点的驻波随时间的变化。
- **溢出问题**：`uint32_t` 上限约 4.29e9，而 `STOP_MAX = 2.7e9`。两个接近 2.7 GHz 的频率直接相加会溢出，所以源码里求「中点」写成 `a/2 + b/2` 而不是 `(a+b)/2`——这是嵌入式代码里反复出现的手法。
- 前置讲义概念：`current_props` 别名宏（u1-l1）、sweep 线程消费 `frequencies[]`（u2-l5）、`CMD_WAIT_MUTEX` 命令移交（u2-l5）。

## 3. 本讲源码地图

| 文件 | 关键内容 | 作用 |
| --- | --- | --- |
| `nanovna.h` | `START_MIN`/`STOP_MAX`/`POINTS_COUNT` 常量、`stimulus_type` 枚举、`properties_t` 与别名宏、`marker_t` | 频率相关数据结构与接口契约 |
| `main.c` | `set_sweep_frequency()`、`get_sweep_frequency()`、`set_frequencies()`、`update_frequencies()`、`update_marker_index()`、`cmd_sweep()`、`cmd_scan()` | 频率配置的全部逻辑与 shell 入口 |
| `plot.c` | `update_grid()` | 频率变化后重算频率轴网格刻度（联动方） |

调用关系总览：

```text
shell 命令 / UI 菜单
        │
        ▼
set_sweep_frequency(type, freq)     ← 5 种模式 + 边界钳制（改 frequency0/1）
        │
        ▼
update_frequencies()                ← 联动刷新入口
        ├─ set_frequencies()         ← 误差扩散生成 frequencies[]
        ├─ update_marker_index()     ← 把 marker 吸附到新频点
        └─ update_grid()   (plot.c)  ← 重算频率轴刻度并请求重绘
```

另一条旁路：`cmd_scan()` 直接调 `set_frequencies()`，绕过 `set_sweep_frequency()`（见 4.4.3）。

## 4. 核心概念与源码讲解

### 4.1 频率状态存在哪里：properties_t 与别名宏

#### 4.1.1 概念说明

频率不是散落的全局变量，而是集中存在 `properties_t` 结构体里——这样做是为了让整套「仪器状态」可以一次性写入 flash 掉电保存（细节在 u3-l4 展开）。`main.c` 里所有看似全局变量的 `frequency0`、`sweep_points`、`frequencies`，其实都是指向 `current_props` 成员的**别名宏**，这是 u1-l1 已经建立的关键认知，本讲正式用到它。

#### 4.1.2 核心流程

- `properties_t` 中的 `_frequency0`、`_frequency1` 是扫描起止频率（Hz，`uint32_t`）。
- `_sweep_points` 是扫描点数（`uint16_t`），本固件中固定为 `POINTS_COUNT`（101）。
- `_frequencies[POINTS_COUNT]` 是实际频点表；**未被使用的尾部槽位会被填 0**，这个 0 在 `sweep()` 里被当作循环终止符使用。
- `_freq_mode` 是一个标志位，记录 UI 处于 start/stop 视角还是 center/span 视角。

#### 4.1.3 源码精读

结构体定义，注意 `_frequencies` 紧跟在 `_sweep_points` 之后：

[nanovna.h:363-385](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L363-L385)

这段定义了 `properties_t`：`_frequency0/_frequency1` 为扫描边界，`_sweep_points` 为点数，`_frequencies[POINTS_COUNT]` 为频点表，注释标明整个结构体恰为 0x1200 字节（一个 flash 校准槽的容量）。

别名宏把结构体成员「展开」成惯用名：

[nanovna.h:395-410](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L395-L410)

`frequency0`、`sweep_points`、`frequencies`、`freq_mode` 等都是宏，编译后等价于 `current_props._xxx`。所以本讲看到的所有「全局变量」，物理上都存在 `current_props` 这一份里。

三个视角判断宏：

[nanovna.h:412-414](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L412-L414)

`FREQ_IS_CW()` 的判据就是「起点等于终点」，固件里没有单独的 CW 状态位。

边界常量与点数上限：

[nanovna.h:29-41](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L29-L41)

`START_MIN = 50000`（50 kHz，受限于 si5351 低频段性能，见 u2-l2 的 rdiv）、`STOP_MAX = 2700000000U`（2.7 GHz，谐波模式上限）、`POINTS_COUNT = 101`。

marker 的数据结构：

[nanovna.h:257-261](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L257-L261)

`marker_t` 用「频率 + 频点索引」双字段记录位置：`frequency` 是用户语义的频率，`index` 是它落到 `frequencies[]` 的下标，两者可能不一致（见 4.4.3）。

#### 4.1.4 代码实践

1. **实践目标**：把「别名宏 → 结构体成员」的映射抄写一遍，建立读码条件反射。
2. **操作步骤**：打开 `nanovna.h` 上述两段，画一张两列对照表：左列写 main.c 中出现的名字（`frequency0`、`frequency1`、`sweep_points`、`frequencies`、`freq_mode`、`markers`），右列写 `current_props._xxx`。
3. **需要观察的现象**：确认没有任何一个名字是真正的全局变量定义（可在 main.c 中 grep `uint32_t frequency0`，应无结果）。
4. **预期结果**：六个名字全部命中宏定义；由此理解为什么 `set_frequencies()` 直接写 `frequencies[i]` 就等于在改 `current_props._frequencies[i]`，也就是在改将来会被保存到 flash 的内容。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `properties_t` 里要同时保存 `_frequency0/_frequency1` 和 `_frequencies[]` 两份频率信息？只存一份行不行？

**答案**：可以只存边界、开机重建频点表，但 `frequencies[]` 也被一并保存（连同 `_cal_data[]`），因为校准误差项是**逐频点**采集的，恢复时必须保证频点表与校准数据严格对齐；直接存表可避免「重建算法变化导致旧校准错位」的问题。代价是 101×4 ≈ 404 字节 flash。

**练习 2**：`FREQ_IS_CW()` 为什么不用一个专门的布尔标志？

**答案**：CW 的定义就是 span 为 0，而 span 可由 `frequency0 == frequency1` 直接推出；用派生判断而不是冗余状态，避免了「标志与边界不一致」这一类状态同步 bug。

### 4.2 set_sweep_frequency / get_sweep_frequency：五种模式与边界钳制

#### 4.2.1 概念说明

`set_sweep_frequency()` 是**所有**频率修改的唯一正门：UI 触摸菜单、shell 命令最终都汇聚到它。它解决三个问题：

1. **同一目标的五种说法**：给起点、给终点、给中心、给扫宽、给单频点，对同一对 `(frequency0, frequency1)` 做不同的改写；
2. **边界合法化**：任何输入都被钳制到 `[START_MIN, STOP_MAX]` 且保证 start ≤ stop；
3. **联动**：改完边界后自动触发 `update_frequencies()` 重建下游（频点表、marker、网格），并在需要时对已应用的校准做插值（u3-l3 展开）。

#### 4.2.2 核心流程

```text
set_sweep_frequency(type, freq):
  记录 cal_applied = cal_status 是否处于「校准已应用」状态
  ① 钳位：type != ST_SPAN 且 freq < START_MIN → freq = START_MIN
          freq > STOP_MAX → freq = STOP_MAX
     （SPAN 不做下限钳制 —— 扫宽允许为 0，即 CW）
  ② ensure_edit_config()   // 切到可编辑配置块（u3-l4 展开）
  ③ 按 type 改写 frequency0 / frequency1 与 freq_mode：
     ST_START  : 清 center-span 位；改 frequency0；若 stop < start 则把 stop 抬到 start
     ST_STOP   : 清 center-span 位；改 frequency1；若 start > stop 则把 start 压到 stop
     ST_CENTER : 置 center-span 位；保持 span，先钳 span 使 [c-span/2, c+span/2] 不越界
     ST_SPAN   : 置 center-span 位；保持 center，先钳 center 使新范围不越界
     ST_CW     : 置 center-span 位；frequency0 = frequency1 = freq
  ④ update_frequencies()   // 重建频点表 + marker + 网格（见 4.4）
  ⑤ 若自动插值开启且校准在应用 → cal_interpolate(lastsaveid)
```

get_sweep_frequency 则是「带自愈的读取器」：读取前若发现 `frequency0 > frequency1`，直接交换两者——注释直言这是防御陈旧数据（"Obsolete, ensure correct start/stop"）。注意它名为 get 却有写副作用，这是阅读时容易踩的坑。

#### 4.2.3 源码精读

模式枚举与函数原型：

[nanovna.h:85-91](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L85-L91)

`stimulus_type` 枚举依次为 ST_START/ST_STOP/ST_CENTER/ST_SPAN/ST_CW，`MAX_FREQ_TYPE = 5` 与之配套（cmd_sweep 里有一处编译期断言依赖它，见 4.4.3）。

主体函数，先看钳位与 ST_START/ST_STOP 分支：

[main.c:1008-1036](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1008-L1036)

这段做下限/上限钳位（注意 `type != ST_SPAN` 的豁免），然后在 ST_START 分支里：若新的 stop 拦不上 start，就把 `frequency1` 抬到 `freq`，保证 start ≤ stop；ST_STOP 分支对称。两个分支都先清 `FREQ_MODE_CENTER_SPAN` 位——用户一旦直接给起止频率，UI 视角就切回 start/stop。

再看 ST_CENTER / ST_SPAN / ST_CW 与收尾：

[main.c:1037-1077](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1037-L1077)

ST_CENTER 分支保持原 span，但当中心太靠近边界时收缩 span：`if (freq < START_MIN + span/2) span = (freq - START_MIN) * 2;`，随后 `frequency0 = freq - span/2; frequency1 = freq + span/2;`。注意中点/半宽全部用 `x/2` 形式计算，避免大数相加溢出；同时因整数除法截断，center/span 模式下重建出的 span 可能与设定值差 1 Hz。ST_SPAN 分支镜像地保持 center、钳 center。ST_CW 最简单：两端都设为 freq。最后统一调用 `update_frequencies()` 联动下游，并按需校准插值。

带自愈的读取器：

[main.c:1079-1096](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1079-L1096)

入口处的交换逻辑保证返回语义正确：ST_CENTER 返回 `frequency0/2 + frequency1/2`（又是防溢出的折半写法），ST_SPAN 返回差值，ST_CW 返回 `frequency0`。

默认值从哪来：

[main.c:817-836](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L817-L836)

`load_default_properties()` 设定出厂扫描范围 50 kHz ~ 900 MHz、101 点、`FREQ_MODE_START_STOP` 视角。这就是本讲实践任务选用「50000 → 900000000, 101 点」的出处。

#### 4.2.4 代码实践

1. **实践目标**：用 shell 命令亲手触发五种模式，观察边界钳制。
2. **操作步骤**（有真机时，通过 USB 串口终端；无真机时改为下面的代码推演）：
   - `sweep` —— 无参打印当前 `start stop points`；
   - `sweep center 300000000`，再 `sweep` 查看 start/stop 是否变为 300 MHz ± 原半宽；
   - `sweep span 1000000`，再 `sweep`；
   - `sweep cw 145000000`，观察 start == stop；
   - `sweep start 10000`（低于 START_MIN），再 `sweep`，确认被抬到 50000；
   - `sweep center 2699000000` + `sweep span 100000000`，观察 center 被压回 `STOP_MAX - span/2`。
3. **需要观察的现象**：任何越界输入回读后都落在合法区间；center/span 与 start/stop 两种输入方式可以互相换算。
4. **预期结果**：与上面 4.2.2 流程逐条对应。**无真机时的推演**：对照 [main.c:1042-1047](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1042-L1047) 手算——例如原范围 50k~900MHz（span=899950000），执行 `center 100000000`：100 MHz > START_MIN + span/2（≈450 MHz）不成立？span/2 = 449975000，START_MIN + 449975000 ≈ 450 MHz > 100 MHz，故 span 被收缩为 (100000000-50000)*2 = 199900000，得到 start = 100000000 - 99950000 = 50000，stop = 199950000。手算结果待本地用命令验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 ST_SPAN 分支不做 `freq < START_MIN` 的下限钳制，而其他四种都做？

**答案**：`freq` 在 ST_SPAN 语境下是**扫宽**而不是频率，扫宽为 0 是合法的（即 CW 模式），把它钳到 50 kHz 反而无法进入零跨度。上限仍然钳制（span > STOP_MAX 无意义）。

**练习 2**：`set_sweep_frequency(ST_START, freq)` 已经保证 start ≤ stop，为什么 `get_sweep_frequency()` 还要再做一次交换自愈？

**答案**：`frequency0/frequency1` 是保存在 flash 里的持久数据，可能来自旧版本固件、上位机直接写 props 或数据损坏；getter 作为所有下游（`update_frequencies`、marker、绘图）的唯一取值口，在这里兜底一次，比在每个使用点分别防御便宜。这体现了「在一个收口处做不变式修复」的思想。

**练习 3**：ST_CENTER 分支中 `uint32_t center = frequency0 / 2 + frequency1 / 2;` 若写成 `(frequency0 + frequency1) / 2` 会有什么后果？

**答案**：当两个频率都接近 2.7 GHz 时，和约 5.4e9，超过 uint32_t 上限 4294967295，发生回绕得到一个极小的错误中心值，进而算出错误的 start/stop。折半再相加保证中间量不超过 1.35e9+1.35e9 = 2.7e9，安全。

### 4.3 set_frequencies：误差扩散生成整数频点表

#### 4.3.1 概念说明

有了 start/stop/points，就要填出 `frequencies[]`。核心矛盾：理想频点是

\[ x_i = f_{start} + \frac{i \cdot S}{N}, \quad S = f_{stop} - f_{start},\ N = \text{points} - 1 \]

是小数序列，而 si5351 需要 1 Hz 粒度的整数。若简单地 `f_i = start + i * span / (points-1)`（整数除法），每个点都向下截断，累计误差可达近 1 Hz 且终点对不上 stop。`set_frequencies()` 采用**整数误差扩散**（与画直线段的 Bresenham 算法同族）：把除法拆成「整数部分 delta + 余数 error」，余数像找零一样每隔几个点补 1 Hz，使每个频点都是理想值的四舍五入。

#### 4.3.2 核心流程

```text
set_frequencies(start, stop, points):
  step  = points - 1          // 区间数（101 点 → 100 段）
  span  = stop - start
  delta = span / step         // 每段步进的整数部分
  error = span % step         // 每段「欠」下的余数（0 ~ step-1 Hz）
  f = start, df = step >> 1   // df 是误差累积器，初值 step/2 实现「四舍五入」
  for i = 0 .. step:
      frequencies[i] = f
      df += error
      if df >= step:  f += 1; df -= step    // 欠账凑满一段，补 1 Hz
      f += delta                // 进入下一段
  for i = step+1 .. POINTS_COUNT-1:
      frequencies[i] = 0        // 尾部清零 → sweep 循环的终止符
```

数学上可以证明（下面手算例子可验证），这样得到的

\[ f_i = f_{start} + i\,\delta + \left\lfloor \frac{N/2 + i \cdot r}{N} \right\rfloor = \operatorname{round}\!\left( f_{start} + \frac{i \cdot S}{N} \right) \]

其中 \(\delta = \lfloor S/N \rfloor,\ r = S \bmod N\)。也就是说它等价于「理想小数频点逐点四舍五入」，与 `numpy.linspace(start, stop, points)` 的差不超过 0.5 Hz，且首尾两点精确等于 start/stop。

手算例子：start=0, stop=11, points=5（step=4, delta=2, error=3, df 初值 2）：

| i | 存入 f[i] | df += 3 后 | 补 1？ | f += 2 后 |
| --- | --- | --- | --- | --- |
| 0 | 0 | 5 | 是（f=1, df=1） | 3 |
| 1 | 3 | 4 | 是（f=4, df=0） | 6 |
| 2 | 6 | 3 | 否 | 8 |
| 3 | 8 | 6 | 是（f=9, df=2） | 11 |
| 4 | 11 | — | — | — |

结果 `[0, 3, 6, 8, 11]`；理想值 `[0, 2.75, 5.5, 8.25, 11]` 四舍五入后完全一致，尾点精确落在 stop=11。

为什么不用浮点：Cortex-M0 无 FPU，浮点除法要走软浮点库，而本算法只用了整数加减与一次除法/取余；更重要的是整数路径**精确可重现**，与 flash 里保存的校准数据逐点对齐不会有任何漂移。

#### 4.3.3 源码精读

完整实现（含尾部清零）：

[main.c:970-990](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L970-L990)

这就是 4.3.2 伪代码的原文：`df` 从 `step>>1` 起步实现舍入；循环体内先存值再补 1 再加 `delta`，与 C 的 `for (i = 0; i <= step; i++, f+=delta)` 第三子句执行顺序一一对应。第 987-989 行把未用槽位清零，注释 "disable at out of sweep range"——这些 0 的作用见下一条引用。

尾部清零的真正消费者在 sweep 循环：

[main.c:860-863](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L860-L863)

`if (frequencies[i] == 0) break;` 使 `sweep()` 不需要知道「本次实际用了几个点」——只要表尾部是 0，循环自然停在正确位置。这也解释了为什么 `cmd_scan` 允许临时使用少于 101 的点数（见 4.4.3）。

`frequencies[i] == 0` 会被误伤吗？合法频点最小是 `START_MIN = 50000`，恒非 0，所以 0 可以安全地充当哨兵。

#### 4.3.4 代码实践

1. **实践目标**：用 Python 逐行复现 `set_frequencies`，并与 `numpy.linspace` 对比误差。
2. **操作步骤**：保存以下脚本为 `set_frequencies.py` 并运行（示例代码，非项目原有代码）：

```python
#!/usr/bin/env python3
# 示例代码：复现 main.c set_frequencies() 的整数误差扩散
import numpy as np

def set_frequencies(start, stop, points, POINTS_COUNT=101):
    step = points - 1
    span = stop - start
    delta = span // step          # 对应 C 的 span / step
    error = span % step
    freqs = [0] * POINTS_COUNT
    f, df = start, step >> 1      # df 初值 = step/2，实现四舍五入
    for i in range(step + 1):
        freqs[i] = f
        df += error
        if df >= step:
            f += 1
            df -= step
        f += delta                # C 的 for 第三子句在循环体之后执行
    return freqs[:points]

def compare(start, stop, points):
    fw = np.array(set_frequencies(start, stop, points), dtype=np.uint64)
    ideal = np.linspace(start, stop, points)
    err = np.abs(fw.astype(float) - ideal)
    print(f"start={start:>11} stop={stop:>11} points={points:>3} | "
          f"max|err|={err.max():.3f} Hz | 首点={fw[0]} 尾点={fw[-1]} (stop={stop})")

compare(50000, 900000000, 101)    # 固件默认范围
compare(50000, 900000001, 101)    # span 不整除：观察 ±0.5 Hz 内的抖动
compare(0, 11, 5)                 # 4.3.2 的手算例子
```

3. **需要观察的现象**：三个案例的 `max|err|`；尾点是否恒等于 stop；非整除案例中相邻步进是否只有 delta 与 delta+1 两种取值。
4. **预期结果**（依据算法推导，待本地运行验证）：默认范围 50k~900MHz/101 点时 span=899950000 恰被 100 整除（error=0），`max|err| = 0.000`，与 linspace 逐点相同；stop=900000001 时 error=1，`max|err| ≈ 0.5`（即四舍五入误差）；`compare(0,11,5)` 输出 `[0,3,6,8,11]`、`max|err| = 0.250`。

#### 4.3.5 小练习与答案

**练习 1**：把 `df` 初值从 `step>>1` 改成 0，行为会怎么变？

**答案**：累积器初值 0 相当于把 `floor((i·r)/N)` 当修正量，即「向下取整」而非「四舍五入」，每个频点整体偏低最多 1 Hz；首点仍为 start，尾点仍为 stop，但中段与理想值的最大偏差从 ≤0.5 Hz 变为 <1 Hz。

**练习 2**：若 `points` 传入 1，这段代码会发生什么？

**答案**：`step = 0`，`span / step` 除零——C 语言中是未定义行为（本平台上软除法例程的返回值不可预期）。上游 `cmd_scan` 只挡住了 `points <= 0`，没有挡 1；实际使用中点数恒为 101 或用户给的 ≥2 值，这是阅读时值得注意的边界。

**练习 3**：为什么循环上界是 `i <= step` 而不是 `i < points`？

**答案**：两者数值相同（step = points-1），但语义不同：频点数是 points 个，而**区间数**是 points-1 个，步进 delta/error 都定义在区间上。用 `step` 做上界使「points 个点、step 个区间」的关系在代码里显式可见。

### 4.4 update_frequencies 联动刷新与 cmd_sweep / cmd_scan

#### 4.4.1 概念说明

频率边界一变，三个下游都要跟着变：频点表（`set_frequencies`）、4 个 marker 的落点（`update_marker_index`）、屏幕频率轴刻度（`update_grid`）。`update_frequencies()` 就是这次联动的编排者，它在 `set_sweep_frequency()` 尾部、开机 `main()`、`resume` 和 `recall` 四处被调用。而 shell 层的 `sweep` 与 `scan` 两个命令代表了两种不同的频率使用哲学：**改配置**（持久、走正门）与**借表一用**（临时、走旁路）。

#### 4.4.2 核心流程

```text
update_frequencies():
  start/stop ← get_sweep_frequency()      // 含 start>stop 自愈
  set_frequencies(start, stop, sweep_points)
  update_marker_index()                   // marker 吸附到新频点
  update_grid()                           // 重算频率轴刻度，置 REDRAW_FREQUENCY

update_marker_index():  对每个已启用 marker：
  f < fstart          → index=0,        frequency=fstart        （钳到首点）
  f >= fstop          → index=points-1, frequency=fstop         （钳到尾点）
  fstart <= f < fstop → 找 i 使 frequencies[i] <= f < frequencies[i+1]，
                        比较 f 与中点 freq[i]/2+freq[i+1]/2，取较近者的下标，
                        只改 index，不改 frequency

cmd_sweep（改配置）: 解析数字或 start|stop|center|span|cw 关键字 → set_sweep_frequency()
cmd_scan（借表一用）: set_frequencies() 直接改表 → pause → sweep(false) → 按 mask 打印数据
```

#### 4.4.3 源码精读

联动编排者：

[main.c:992-1006](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L992-L1006)

注意被注释掉的 `OP_FREQCHANGE`（第 1000 行）——频率变化不再单独举旗，因为 `update_grid()` 内部已经会 `force_set_markmap()` 并置 `REDRAW_FREQUENCY`。第 1002、1005 行分别更新 marker 与网格，顺序不能颠倒：marker 索引依赖**新**频点表，而网格只是显示层。

marker 吸附逻辑：

[main.c:942-968](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L942-L968)

三个分支：越下界钳到首点（连 `frequency` 也改写为 fstart）、越上界钳到尾点、中间则线性扫描找到覆盖 f 的区间，用 `frequencies[i]/2 + frequencies[i+1]/2` 这个**防溢出中点**做就近选择。中间分支只更新 `index` 不回写 `frequency`——marker 的「频率」保留用户语义值，「索引」才是取数下标，两者允许不一致。线性查找 O(points)，对 101 点毫无压力。

`cmd_sweep` 的两种参数形态：

[main.c:1098-1132](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1098-L1132)

无参时回读当前配置；`argc == 2 && value0 == 0` 时把 argv[0] 当关键字，用 `get_str_index()` 在 `"start|stop|center|span|cw"` 里查枚举——`my_atoui()` 对非数字串返回 0，正好充当「是不是数字」的判别。第 1110-1112 行的 `#if MAX_FREQ_TYPE != 5 #error` 是**编译期断言**：枚举一旦增删，关键字串必须同步改，否则这里直接编译失败，把运行期隐患提前到构建期。

`cmd_scan` 走的旁路：

[main.c:899-940](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L899-L940)

关键在第 923 行：它**直接调 `set_frequencies()`**，完全绕过 `set_sweep_frequency()`——`frequency0/frequency1`（也就是会被保存的配置）原封不动，只临时替换了频点表，随后 `pause_sweep()` + `sweep(false)` 一次扫完并按 outmask（bit0=频率、bit1=CH0 数据、bit2=CH1 数据）逐行打印。所以 scan 之后配置仍是旧的，下一次任何 `update_frequencies()`（例如 `resume` 命令）就会把表还原——见 [main.c:297-308](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L297-L308) 中 `cmd_resume` 的注释 "restore frequencies array and cal"。这就是 Python 上位机 `scan_gamma` 能反复用不同范围抓数据而不弄乱仪器配置的原因（u5-l2 会用到）。点数校验允许 1~POINTS_COUNT，少于 101 时尾部 0 哨兵让 `sweep()` 提前收工（4.3.3）。

两个命令在命令表中的注册差异：

[main.c:2177-2178](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2177-L2178)

`scan` 带 `CMD_WAIT_MUTEX`（移交 sweep 线程执行，因为它要真的跑测量，见 u2-l5），`sweep` 标志为 0（在 shell 线程立即执行，只改配置不碰测量数据结构）。

`update_grid` 如何挑「好看」的刻度：

[plot.c:83-110](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L83-L110)

从 1e8 开始按 5×10^k、2×10^k、1×10^k 逐级缩小，找到能让屏幕至少放下 4 格的最粗刻度，然后换算成像素宽并请求 `REDRAW_FREQUENCY` 重绘频率轴。这保证刻度永远是「5/2/1 × 10 的幂」这类读得懂的数。

#### 4.4.4 代码实践

1. **实践目标**：复现 `update_marker_index`，验证 marker 频率落在两频点之间时的就近选择。
2. **操作步骤**：接 4.3.4 的脚本（示例代码）：

```python
def update_marker_index(freqs, points, f):
    fstart, fstop = freqs[0], freqs[-1]
    if f < fstart:
        return 0, fstart                      # 钳到首点（连频率一起改）
    if f >= fstop:
        return points - 1, fstop              # 钳到尾点
    for i in range(points - 1):
        if freqs[i] <= f < freqs[i + 1]:
            near = i if f < (freqs[i] // 2 + freqs[i+1] // 2) else i + 1
            return near, f                    # 中间分支不改 frequency

freqs = set_frequencies(50000, 900000000, 101)
for probe in [10, 50000, 4_550_000, 4_549_700, 900_000_000, 2_000_000_000]:
    print(f"marker f={probe:>12} -> (index, freq)= {update_marker_index(freqs, 101, probe)}")
```

3. **需要观察的现象**：相邻频点约为 8 999 500 Hz 间隔；4 550 000 与 4 549 700 两个探针应落在同一点位两侧、得到相邻的两个 index；越界探针被钳到 0 或 100。
4. **预期结果**（由频点间隔推得，待本地运行验证）：`freqs[0]=50000`、`freqs[1]=9049500`、间隔 8999500 Hz；4 550 000 落在 [50000, 9049500) 内且过中点（中点≈4549750），故 index=1；4 549 700 未过中点，index=0；2 GHz 越界钳为 (100, 900000000)。有真机的读者可对照：把 marker 移到某频率后执行 `scan 50000 900000000 101 1`，用输出的频率表核对 marker 实际读取的频点。

#### 4.4.5 小练习与答案

**练习 1**：`cmd_scan` 之后紧跟一个 `sweep`（无参）命令，打印的 start/stop 是扫描前的旧值还是 scan 的值？为什么？

**答案**：旧值。`cmd_scan` 只调 `set_frequencies()` 改写频点表，不经过 `set_sweep_frequency()`，`frequency0/frequency1` 从未变化，所以 `sweep` 回读的仍是仪器持久配置。

**练习 2**：如果删掉 `set_frequencies()` 尾部的清零循环，`cmd_scan 50000 300000000 51 1` 会出现什么现象？

**答案**：前 51 个点是新范围的频点，第 51~100 点仍是上一次的旧频点（非 0），`sweep()` 的 `frequencies[i] == 0` 哨兵失效，扫描会继续用旧频率测满 101 点并写进 `measured[]`，输出数据后半段频率与实际测量频率错位。

**练习 3**：`update_marker_index` 的中间分支为什么用线性扫描而不是二分查找？

**答案**：points=101，最坏 100 次比较，且该函数只在频率配置变化时执行一次（非每帧），线性扫描的常数开销与代码复杂度都最低；二分查找在此没有任何可感知收益——这是「按实际规模选算法」的典型例子。

## 5. 综合实践

**任务：在 PC 上实现一个「迷你频率配置引擎」，并用它预判真机行为。**

把本讲三个模块串成一个约 80 行的 Python 脚本 `freq_engine.py`（示例代码）：

1. 实现 `set_sweep_frequency(type, freq)`：用一个字典保存 `frequency0/frequency1/freq_mode`，完整复刻 4.2.3 中五种模式的改写与钳制（含 `START_MIN/STOP_MAX`、ST_SPAN 豁免、center/span 的收缩逻辑、ST_START 抬 stop）。
2. 实现 `get_sweep_frequency(type)` 的交换自愈，以及 `set_frequencies`、`update_marker_index`（前文已给出）。
3. 实现一个 `update_frequencies()` 把三者按 [main.c:992-1006](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L992-L1006) 的顺序串起来。
4. 用它回放一个场景并打印每步结果：
   - `set_sweep_frequency(ST_START, 50000)`、`ST_STOP, 900000000`（默认范围）；
   - `ST_CENTER, 100000000`——按 4.2.4 的手算，span 应收缩为 199900000，start/stop = 50000/199950000；
   - `ST_SPAN, 400000000`——center 被钳到 `STOP_MAX - span/2`；
   - `ST_CW, 145000000`——start == stop，`FREQ_IS_CW()` 为真；
   - 每步之后打印频点表首尾 3 个点、marker=145000000 在 CW 模式下的 index（应为 points-1）。
5. 有真机的读者：把同样的命令序列发给真机（`sweep center 100000000` 等，每步用 `sweep` 回读），与脚本输出逐行对照。

**验收标准**：脚本能复现 4.2.4 的手算收缩结果；CW 模式下频点表全部退化为同一个频率（start=stop 时 delta=error=0）；marker 在 CW 模式被钳到最后一个点。真机对照部分待本地验证。

## 6. 本讲小结

- 频率有三层表示：`frequency0/frequency1`（持久边界）→ `frequencies[]`（整数频点表，与校准数据逐点对齐）→ `markers[].index`（显示/取数下标）；`update_frequencies()` 负责逐层推导。
- `set_sweep_frequency()` 是频率修改的唯一正门：五种模式共用一套边界钳制（`START_MIN=50 kHz` ~ `STOP_MAX=2.7 GHz`，ST_SPAN 豁免下限），center/span 改写中大量使用 `x/2` 折半运算规避 uint32 溢出。
- `set_frequencies()` 用整数误差扩散（`delta + error` 累积补 1 Hz）生成频点，等价于对理想小数频点逐点四舍五入，与 `numpy.linspace` 差 ≤0.5 Hz 且首尾精确；尾部清零的 0 同时充当 `sweep()` 循环的终止哨兵。
- `update_marker_index()` 把 marker 钳到新范围或吸附到最近频点，中间分支只改 index 不改 frequency；`update_grid()` 用 5/2/1×10^k 启发式挑选频率轴刻度。
- `cmd_sweep` 走正门改配置（shell 线程立即执行）；`cmd_scan` 直接改频点表借表一用（`CMD_WAIT_MUTEX` 移交 sweep 线程），配置不动、`resume` 即还原——这是上位机批量抓数据的机制基础。
- 阅读技巧收获：`#if MAX_FREQ_TYPE != 5 #error` 展示了用编译期断言保护枚举-字符串表同步；`get_sweep_frequency()` 展示了在唯一取值口做不变式自愈。

## 7. 下一步学习建议

频率表就位后，下一个自然问题是：`measured[]` 里测出来的原始复数如何变成「真实」的 S 参数？下一讲 **u3-l2（SOL 校准：一端口/二端口误差模型）** 将精读 `cal_collect()` 如何在当前频点表上采集标准件数据、`cal_done()` 如何解出 Ed/Es/Er/Et/Ex 五个误差项——校准数据与频点表的「逐点对齐」正是本讲反复强调的设计动机。随后 **u3-l3（误差修正应用与校准插值）** 会讲 `cal_interpolate()`：当用户像本讲 4.2 那样改了扫描范围后，旧频点上的误差项如何插值到新频点（`set_sweep_frequency()` 尾部那句 `cal_interpolate(lastsaveid)` 的完整故事）。想先动手的读者，可以此时用 `scan` 命令配合 u5-l2 的 Python 封装抓一组真实数据，为校准讲义做准备。
