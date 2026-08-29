# 二次开发实战：为固件添加新特性

> 本讲是全手册的毕业项目。我们把前面五个单元学到的知识——sweep 测量链路（u2）、DSP 累加（u2-l4）、线程模型（u2-l5、u5-l1）、properties_t 持久化（u3-l4）、菜单表驱动（u4-l5）、资源约束（u5-l3）——串成一个完整的二次开发案例：**为固件实现『多次测量平均』功能**。

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立规划一个横跨 `main.c`、`nanovna.h`、`flash.c`、`ui.c` 的新特性，清楚每一处改动挂在现有架构的哪个挂接点上。
2. 实现 `average {n}` shell 命令：在 sweep 链路中对 `measured` 数组做 n 遍（1-8）复数相干平均，把噪声标准差降到 1/√n。
3. 把平均次数存入 `properties_t` 的 `_reserved` 保留区，保证 `save`/`recall` 后可恢复，且与旧版本固件保存的槽位兼容。
4. 掌握「修改 → 编译 → 烧录 → shell 验证 → Python 抓数据验证」的完整迭代流程。

## 2. 前置知识

本讲不再从零解释基础概念，只做要点回顾与补充。如果你对下面任何一条感到陌生，请先回看对应讲义。

**回顾一：测量的数据通路。** 每个频点的复数 Γ 由 `calculate_gamma()` 算出后，经函数指针 `sample_func` 写入全局数组 `measured[2][101][2]`（通道 × 频点 × 实部/虚部）；若开启校准，`sweep()` 会在测量点原地做误差修正；整帧扫完后 `Thread1` 才调用 `plot_into_index()` 把数据换算成屏幕坐标（见 u2-l5、u4-l2）。

**回顾二：`properties_t` 与别名宏。** 所有「测量现场」状态（频点表、校准数据、trace、marker、带宽档……）都住在 `properties_t current_props` 这个 0x1200 字节的结构体里，掉电时整块写入 flash 槽位。`nanovna.h` 用 `#define bandwidth current_props._bandwidth` 这类别名宏让业务代码像访问普通全局变量一样访问它们（见 u3-l4）。

**回顾三：shell 命令表。** 新增一条命令 = 写一个 `VNAShell_FUNCTION(cmd_xxx)` 函数 + 在 `commands[]` 表里加一行；带 `CMD_WAIT_MUTEX` 标志的命令会被递交给 sweep 线程执行（见 u5-l1）。

**回顾四：表驱动菜单。** UI 菜单是 `menuitem_t` 数组，加一项功能 = 加一行表项 + 一个回调（见 u4-l5）。

**新概念：相干平均（coherent averaging）。** 这是本讲的核心数学工具，下面详细解释。

设某频点的真实反射系数为 \(\Gamma\)，第 k 次测量的噪声为复数随机变量 \(n_k\)（幅度记为 \(\sigma\)）：

\[
\hat{\Gamma}_k = \Gamma + n_k,\quad n_k \sim (0,\ \sigma^2)
\]

对 n 次独立测量取**复数算术平均**：

\[
\bar{\Gamma} = \frac{1}{n}\sum_{k=1}^{n}\hat{\Gamma}_k = \Gamma + \frac{1}{n}\sum_{k=1}^{n}n_k
\]

噪声方差降为 \(\sigma^2/n\)，标准差降为 \(\sigma/\sqrt{n}\)。换成对数刻度，噪声底的改善量是：

\[
\Delta = 20\log_{10}\sqrt{n} = 10\log_{10}n \approx 9\ \text{dB}\quad(n=8)
\]

两个必须理解的前提：

- **必须平均复数，不能平均 dB 值。** \(20\log_{10}|\cdot|\) 是非线性变换，由 Jensen 不等式，\(E[20\log_{10}|\Gamma+n|] < 20\log_{10}|\Gamma|\)——对 dB 平均不仅降噪更少，还会把读数系统性拉低。而 `measured` 里存的就是复数 Γ（实部/虚部），正好是线性域，这是把平均挂在 `measured` 上的数学依据。
- **各遍噪声须不相关。** 同一频点重新调谐（`set_frequency`）后再测，热噪声基本独立；但若两遍之间本振漂移或被测件（DUT）自身变化，平均会退化为「平均了两个不同的真值」。这正是把 n 上限设为 8 的工程理由。

**新术语：软浮点。** STM32F072 的 Cortex-M0 没有 FPU，所有 `float` 运算都由编译器插入软件例程完成。一次软浮点除法远贵于乘法（上百周期 vs 数十周期），所以热路径上要「一次除法换多次乘法」。这直接决定本讲示例代码的写法。

## 3. 本讲源码地图

| 文件 | 本讲关注的点 | 作用 |
| --- | --- | --- |
| [main.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c) | `Thread1`、`sweep()`、`measured`、`cmd_bandwidth`、`commands[]`、`load_default_properties` | 测量主循环、数据数组、shell 命令注册——新特性的主要战场 |
| [nanovna.h](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h) | `properties_t`、`_reserved[49]`、别名宏、`spi_buffer` | 结构体布局与接口契约，持久化字段要加在这里 |
| [flash.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c) | `caldata_save`/`caldata_recall`、`checksum` | 掉电保存与恢复，验证 `_reserved` 里的新字段能安全过 flash |
| [dsp.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c) | `dsp_process`、`calculate_gamma`、四个 float 累积器 | 理解「已有的平均」发生在哪一层，避免重复造轮子 |
| [ui.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c) | `menu_bandwidth`、`menu_bandwidth_cb`、`menu_display`、高亮分支 | 把新特性挂进 DISPLAY 菜单 |
| [python/nanovna.py](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py) | `send_command`、`data` | 上位机端验证噪声改善 |

## 4. 核心概念与源码讲解

### 4.1 sweep 数据通路：在哪一层做平均

#### 4.1.1 概念说明

「测量平均」这个词在 NanoVNA 里其实可以落在三个不同的层，效果与代价完全不同。动手改代码前必须先选型：

| 候选挂接点 | 做法 | 效果 | 代价/限制 |
| --- | --- | --- | --- |
| ① DSP 累积器层 | 加大 `accumerate_count`，让同一频点连续吃更多音频块 | 相干平均，等效「带宽档」功能 | 表只有 1/3/10/33/100 五档；`accumerate_count` 是 `volatile uint8_t`；连续采集依赖 5kHz 频偏与 48kHz 采样的长期一致性，块数太多会因相位漂移失效 |
| ② sweep 单频点层 | 在 `sweep()` 的 i 循环里对同一频点测 n 次再平均 | 频点间无需重调谐 | 要么引入临时缓冲、要么改动 `sweep()` 热路径，且打断粒度变粗 |
| ③ 整帧层（本讲方案） | 完整扫一遍后，再补扫 n-1 遍，对 `measured` 逐点复数平均 | 噪声降 1/√n；UI 在遍与遍之间仍可打断 | 帧刷新时间变为 n 倍 |

方案①本质上已存在——`bandwidth` 命令与 `bandwidth_accumerate_count[]` 表（u2-l3）。方案③与它互补：带宽档在「毫秒级」内做相干累积，帧平均在「秒级」上对整帧做平均，两者可以叠加。

#### 4.1.2 核心流程

方案③的执行时序（插入到 Thread1 现有流程中）：

```text
Thread1 一次迭代（修改后）：
  ├─ 若 sweep_mode 含 SWEEP_ENABLE：
  │    第 0 遍：sweep(true) → 结果落在 measured[]
  │       └─ 若被 UI 打断（返回 false）→ 放弃本帧平均，走原有路径
  │    把 measured 复制进累加缓冲 acc（借用 spi_buffer）
  │    循环 k = 1 .. n-1：
  │       sweep(true) → measured ← 第 k 遍结果
  │          └─ 若被打断 → 放弃平均（measured 保持第 k 遍部分数据，
  │             completed=false，屏幕沿用旧轨迹，与原固件语义一致）
  │       acc[i] += measured[i]   （404 个 float 逐元素相加）
  │    measured[i] = acc[i] × (1/n)   （一次除法 + 404 次乘法）
  ├─ shell_function 执行（不变）
  ├─ ui_process()（不变）
  └─ completed 为真才 transform_domain / plot_into_index / draw_all（不变）
```

关键设计约束（与 u2-l5 的线程模型一致）：

- **所有对 `measured` 的读写都留在 sweep 线程内**，不新增任何跨线程共享数据，因此不需要锁。
- **打断语义沿用 `break_on_operation`**：遍与遍之间检查 `operation_requested`，UI 优先于数据完整。
- **平均必须发生在误差修正之后、`plot_into_index` 之前**：`sweep()` 内部每个点测完就做了 `apply_error_term_at`，所以进 `measured` 的已是修正后的复数 Γ。注意严格说误差修正是分式（ Möbius）变换，对修正后的值平均 ≠ 对原始值平均再修正；在噪声远小于信号的常规场景下两者差异可忽略，这也是仪器固件的通行做法。

#### 4.1.3 源码精读

**先看数据落点。** `measured` 的定义与体积：

- [main.c:612](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L612) 定义 `float measured[2][POINTS_COUNT][2]`——本特性的操作对象。`POINTS_COUNT=101`（[nanovna.h:40](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L40)），整个数组 2×101×2×4 = **1616 字节 = 404 个 float**。

**再看 `sweep()` 的返回值语义**——它是我们判断「一遍是否完整」的唯一依据：

- [main.c:857-897](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L857-L897) `sweep(bool break_on_operation)`：逐频点「设频率 → CH0 测反射 → CH1 测传输 → 误差修正 → 电延迟」，[main.c:891-892](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L891-L892) 处若有 UI 操作请求且允许打断则中途返回 `false`；完整扫完返回 `true`。
- [main.c:863-864](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L863-L864) 频点表尾部的 0 是循环哨兵——补扫的每一遍自动继承这一约定，无需额外处理。

**然后是挂接点：Thread1 的主循环。**

- [main.c:106-148](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L106-L148) sweep 线程主体。[main.c:113-115](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L113-L115) 处 `completed = sweep(true)` 是我们要替换成「平均版扫描」的位置；[main.c:131-144](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L131-L144) 表明只有 `completed` 为真才会做时域变换、`plot_into_index` 与 marker 重搜——平均失败（被打断）时整帧不刷新，语义天然自洽。
- 注意 [main.c:121-126](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L121-L126)：`CMD_WAIT_MUTEX` 命令（如 `scan`、`data`、`capture`）也在这里执行。它们与我们的平均循环同线程串行，不会并发踩 `measured` 或 `spi_buffer`。

**「已有的平均」长什么样**——避免与带宽档功能混淆：

- [main.c:604-610](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L604-L610) `bandwidth_accumerate_count[]` 五档累积次数；[main.c:614-627](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L614-L627) `dsp_start`/`dsp_wait` 用 volatile 计数器做「先丢再测 + 累积」。
- [dsp.c:82-85](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L82-L85) `dsp_process` 把每个音频块的四个 int32 中间结果累进 `acc_samp_s` 等 float 累积器——这就是层①的相干累积；[dsp.c:88-108](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L88-L108) `calculate_gamma` 最后做一次复数除法得到该频点 Γ。我们的帧平均是在这**之后**的 `measured` 层再做一次 √n 降噪，两层独立。

**最后是累加缓冲的来源**——u5-l3 讲过的缓冲区复用：

- [nanovna.h:308](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L308) 与 [nanovna.h:327](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L327)：`spi_buffer` 为 2048 个 `uint16_t`，即 **4096 字节 ≥ 我们需要的 1616 字节**。
- [main.c:197-199](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L197-L199) `transform_domain` 已有先例：`float* tmp = (float*)spi_buffer;` 把显示画布临时借作 FFT 工作区。复用的安全性依赖两条纪律（u5-l3）：**单写者**（只有 sweep 线程碰它）与**不与显示 DMA 并发**（平均期间不调 `draw_all`/`ili9341_bulk`）。我们的平均窗口在 Thread1 迭代顶部、绘图之前，两条纪律都满足。

#### 4.1.4 代码实践

**实践 A（无硬件可做）：用 Python 验证「复数平均 vs dB 平均」。**

1. **实践目标**：亲眼看到对 dB 平均会把读数拉低、对复数平均才能正确降噪，从而理解为什么必须挂在 `measured` 层。
2. **操作步骤**（在 PC 上运行，以下为示例代码）：

```python
# avg_sim.py —— 示例代码：对比复数相干平均与 dB 平均
import numpy as np
rng = np.random.default_rng(42)
G = 0.1 + 0.2j              # 真实 Γ
n_try, avg_n = 2000, 8      # 试验次数 / 平均遍数
cplx, db = [], []
for _ in range(n_try):
    gs = G + (rng.normal(0, .05, avg_n) + 1j*rng.normal(0, .05, avg_n))
    cplx.append(np.abs(gs.mean()))                 # 复数平均后取模
    db.append(10*np.log10((np.abs(gs)**2).mean())) # 对功率 dB 平均（错误示范）
print("真值 |Γ|      :", abs(G))
print("复数平均 |Γ|  :", np.mean(cplx))
print("|Γ| 的标准差   :", np.std(cplx), "（理论 √n 改善见练习 3）")
```

3. **需要观察的现象**：复数平均的 |Γ| 均值收敛到 0.2236 附近（无偏）；把错误示范换成 `10*np.log10(np.abs(gs)**2)` 后逐点平均，会发现低信噪比时读数系统性偏低。
4. **预期结果**：复数平均结果无偏且方差缩小；dB 平均有偏。本脚本可立即运行验证。
5. 若你构造的场景与上述描述不符，请以实际输出为准（待本地验证的部分仅限真机行为）。

**实践 B：在源码上定位挂接点（不改代码）。** 用编辑器打开 `main.c`，在 [main.c:115](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L115) `completed = sweep(true);` 一行旁写注释标出「平均版入口」，在 [main.c:131](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L131) 标出「只有完整帧才进入绘图」。这是 4.2 动手前的地图作业。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接把 `accumerate_count` 改成 8 倍来实现「平均 8 次」？
**答案**：加大 `accumerate_count` 是层①的毫秒级相干累积，等效于把带宽档调窄一档以上（u2-l3 的 `bandwidth_accumerate_count[]` 已覆盖 1~100 块）；连续 800 块（约 0.8 秒）的相干累积要求 5kHz 频偏与 48kHz 采样在整个窗口内保持严格相位关系，温漂会使其失效。而帧平均在两次采集之间重新调谐，噪声独立，稳健且可与带宽档叠加。

**练习 2**：平均 8 遍后，噪声底（以 dB 计）理论上改善多少？
**答案**：\(10\log_{10}8 \approx 9.03\) dB。推导见 2 节公式：标准差降为 \(\sigma/\sqrt{8}\)，幅度取对数后即 \(20\log_{10}\sqrt{8}\)。

**练习 3**：若两遍之间 DUT 真值自身漂移了 \(\delta\)（复数），平均结果是什么？这说明什么？
**答案**：结果约为 \(\Gamma + \delta/2\)（两遍各占一半）——平均把「漂移」也混进了结果。说明帧平均对**漂移型**误差无效甚至有害，只对**不相关噪声**有效；这为 n 设上限（8）提供了工程依据。

### 4.2 在 sweep 链路中实现多遍平均

#### 4.2.1 概念说明

选型定了：写一个 `average_sweeps()` 包裹 `sweep()`，在 Thread1 里替换原调用。本模块要解决三个工程问题：

1. **累加缓冲从哪来**：16KB SRAM 不允许再开 1616 字节的静态数组（u5-l3 的栈水位分析显示余量极小），因此借用 `spi_buffer`——`transform_domain` 已示范过同一手法。
2. **浮点边界**：
   - **范围**：\(|\Gamma|\le 1\) 量级（修正后可能略超），8 次累加绝对值不超过个位数，float 的 24 位尾数毫无压力——真正的边界不在数值范围而在运算成本。
   - **除法**：M0 无 FPU，404 个元素若各做一次软除法开销可观。正确写法是先算一次 `float inv = 1.0f / n;`，循环内只做乘法。
   - **n 的合法性**：n 来自 flash 保留区，旧槽位读到的是 0，必须在读取处钳制到 1..8，避免除零。
3. **打断语义**：任何一遍中途被打断都放弃本帧平均，`completed` 返回 `false`，屏幕保持上一帧——与原固件「打断即不刷新」一致（u2-l5）。

#### 4.2.2 核心流程

`average_sweeps()` 的伪代码：

```text
average_sweeps():
  n ← 钳制(average_count, 1, 8)
  若 n == 1：返回 sweep(true)            # 原行为，零开销
  acc ← (float*)spi_buffer               # 借用 4096 字节画布
  若 sweep(true) == false：返回 false     # 第 0 遍（含误差修正）
  memcpy(acc, measured, 1616)
  循环 k = 1 .. n-1：
    若 sweep(true) == false：返回 false   # 被打断，放弃平均
    acc[i] += measured[i]，i = 0..403
  inv ← 1.0f / n                         # 全函数唯一一次浮点除法
  measured[i] ← acc[i] × inv
  返回 true
```

#### 4.2.3 源码精读

- [main.c:112-119](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L112-L119) Thread1 里发起扫描的原始位置——`completed = sweep(true)` 将被替换为 `completed = average_sweeps()`。
- [main.c:857-897](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L857-L897) `sweep()` 本体。补扫直接复用它：每遍都会重新 `set_frequency`、重选通道、重做修正，正是「重新建立测量」的语义；[main.c:861](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L861) 与 [main.c:895](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L895) 的 LED 亮灭顺带指示每遍进度。
- [main.c:197-199](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L197-L199) `transform_domain` 借用 `spi_buffer` 的先例。注意它同样运行在 sweep 线程、且在绘图之前——与我们的平均窗口时序上完全错开：先平均（占用 spi_buffer）→ 后时域变换（再次占用）→ 最后 `draw_all`（用 spi_buffer 当画布）。三者串行，永不并发。
- [main.c:899-940](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L899-L940) `cmd_scan`：注意 [main.c:927](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L927) 直接调 `sweep(false)`（不可打断），**不经过**我们的平均包裹——这是有意保留的差异：`scan` 语义是「尽快给上位机一帧数据」。若希望上位机也能享受平均，把此处一并替换即可（见 4.5 练习 2）。

#### 4.2.4 代码实践

**实践：写出并接入 `average_sweeps()`。**（以下均为示例代码，需你自己加入 `main.c`；改完用 4.5 节流程编译烧录）

1. **实践目标**：实现帧级多遍平均的核心逻辑并接入 Thread1，行为符合「n=1 时与原固件零差别、被打断时放弃平均」。

2. **操作步骤**：

在 `main.c` 中 `sweep()` 定义之后添加（示例代码）：

```c
// 读取平均次数：旧槽位/异常值一律钳制到 1..8（_reserved 未初始化时为 0）
static int average_valid(void)
{
  int n = current_props._reserved[0];
  return (n < 1 || n > 8) ? 1 : n;
}

// 帧级多遍相干平均：完整扫 n 遍，对 measured 逐点复数平均
static bool average_sweeps(void)
{
  int n = average_valid();
  if (n == 1)
    return sweep(true);                    // 原行为
  float *acc = (float *)spi_buffer;        // 借用 4096B 画布，需 1616B
  if (!sweep(true))                        // 第 0 遍（含误差修正）
    return false;
  memcpy(acc, measured, sizeof measured);
  for (int k = 1; k < n; k++) {
    if (!sweep(true))                      // 被打断：放弃平均
      return false;
    for (int i = 0; i < 2 * POINTS_COUNT * 2; i++)
      acc[i] += ((float *)measured)[i];
  }
  float inv = 1.0f / n;                    // 全函数唯一一次软浮点除法
  for (int i = 0; i < 2 * POINTS_COUNT * 2; i++)
    ((float *)measured)[i] = acc[i] * inv;
  return true;
}
```

然后把 [main.c:115](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L115) 的 `completed = sweep(true);` 改为 `completed = average_sweeps();`，并在文件顶部原型声明区（[main.c:73-79](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L73-L79) 附近）补上两个 `static` 声明。

3. **需要观察的现象**：
   - `average 1`（4.3 实现后）时扫频速度、轨迹与改前完全一致；
   - `average 8` 时刷新明显变慢（约为 8 倍），轨迹噪声肉眼变小；
   - 平均过程中触摸屏幕能立即打断（LED 停止闪烁、屏幕回响应），松手后下一帧重新开始平均。
4. **预期结果**：n=1 零回归；n=8 噪声底降约 9 dB（定量验证见 4.5）。刷新周期与打断响应为待本地验证项。
5. **常见坑自查**：
   - 忘记 `n==1` 短路 → 即使关掉平均也白 memcpy 两次；
   - 循环里写成 `measured[i] /= n` → 404 次软除法，帧率进一步劣化；
   - 在平均期间调用任何绘图函数 → 与 `spi_buffer` 冲突，屏幕出现花块（对照 u5-l3 的两条纪律）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `average_sweeps()` 里第 k 遍被打断时不把已累加的部分除以 k 「将就着用」？
**答案**：`sweep()` 中途返回时 `measured` 只更新到被打断的频点，后半段还是上一遍的旧数据；拿它平均会得到「前半新后半旧」的混合帧，比显示旧帧更具欺骗性。返回 `false` 让 Thread1 跳过 `plot_into_index`，屏幕保持上一完整帧，语义最干净。

**练习 2**：把累加缓冲改成 `static float acc[2][POINTS_COUNT][2];` 有什么代价？
**答案**：多占 1616 字节 .bss/.data。对 16KB SRAM 的板子（u5-l3 实测线程栈余量仅几十字节）这很可能直接把链接挤爆或运行时栈溢出；即使侥幸放下，也违背了本项目「缓冲区复用」的既定手法。`spi_buffer` 复用是零成本方案。

**练习 3**：`average_sweeps()` 里 `acc` 与 `measured` 的循环为什么用 `((float*)measured)[i]` 一维展开而不是双层索引？
**答案**：`measured` 是连续数组（无 padding），按 404 个 float 一维展开让编译器生成最简单的增量寻址循环，省去双层下标乘法；对软浮点的 M0，循环骨架的开销与浮点运算同量级，值得省。`transform_domain` 中 `tmp[i*2+0]` 的写法同理（[main.c:243-253](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L243-L253)）。

### 4.3 shell 命令注册：`average {n}`

#### 4.3.1 概念说明

固件的参数必须能从外部设置与查询。NanoVNA 的标准做法（u5-l1）是：一个 `VNAShell_FUNCTION(cmd_xxx)` 函数加 `commands[]` 表里一行三元组 `{名字, 函数, flags}`。本模块仿照现成的 `cmd_bandwidth`（同样是「从列表里选一个档位」的命令）写 `cmd_average`，并重点决策一个新问题：**flags 该不该带 `CMD_WAIT_MUTEX`？**

判断依据回顾：`CMD_WAIT_MUTEX` 让命令延迟到 sweep 线程执行（main 线程睡眠轮询等待），用于必须与测量串行的命令（`data`/`scan`/`capture` 会碰 `measured` 与频点表）。`average {n}` 只写一个字节，读取方（`average_sweeps`）在每帧开始时取值，字节写入在 ARM 上天然原子——仿照 `bandwidth` 用 flags=0 即可，还能保住 shell 的即时响应。

#### 4.3.2 核心流程

```text
用户敲 "average 4"
  → VNAShell_readLine 收行回显
  → VNAShell_executeLine 切参数：argv[0]="average" argv[1]="4"
  → 线性扫 commands[] 命中 "average"（flags=0）
  → main 线程直接调用 cmd_average(1, argv+1)
       argc==0 → 打印当前值；argc==1 → 校验 1..8 → 写 current_props._reserved[0]
  → 下一帧 average_sweeps() 读到新值生效
```

#### 4.3.3 源码精读

**模板命令 `cmd_bandwidth`**——照抄它的骨架：

- [main.c:1965-1980](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1965-L1980) 参数个数校验 → `get_str_index` 在选择列表里查索引（[main.c:458](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L458) 定义）→ 写入全局档位变量 → 非法输入落到 `usage` 打印用法。
- `average` 的参数是连续整数而非枚举字符串，因此用 [main.c:375-386](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L375-L386) 的 `my_atoi`（固件自带的小型十进制转换）更直接。
- 「无参数时打印当前值」的范式见 [main.c:2039-2046](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2039-L2046) `cmd_vbat_offset`：`argc != 1` 分支直接回显现值。

**命令表与分发机制：**

- [main.c:2143-2149](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2143-L2149) `VNAShellCommand` 三元组结构（`#pragma pack(2)` 压缩，见 u5-l3）；[main.c:2151-2152](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2151-L2152) 定义 `CMD_WAIT_MUTEX` 标志并注释「部分命令只能在 sweep 线程执行」。
- [main.c:2153-2208](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2153-L2208) `commands[]` 命令表——新命令就加在这里。注意 `bandwidth` 在 [main.c:2170](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2170) 的 flags 是 0，而 `data`/`scan`/`capture`（[main.c:2165](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2165)、[2177](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2177)、[2190](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2190)）都是 `CMD_WAIT_MUTEX`——对照它们的副作用即可自行判断新命令该归哪一类。
- [main.c:2296-2308](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2296-L2308) 分发逻辑：命中表项后，有 `CMD_WAIT_MUTEX` 就把函数指针挂到 `shell_function` 由 sweep 线程执行（[main.c:121-126](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L121-L126)），否则 main 线程当场调用。
- [main.c:2210-2222](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2210-L2222) `cmd_help` 遍历命令表打印所有名字——注册成功后敲 `help` 应能看到 `average`。

#### 4.3.4 代码实践

**实践：实现并注册 `cmd_average`。**

1. **实践目标**：新增可通过 USB 串口设置/查询平均次数的命令，理解 flags 的取舍。
2. **操作步骤**：

在 `main.c`（建议紧跟 `cmd_bandwidth` 之后）添加（示例代码）：

```c
VNA_SHELL_FUNCTION(cmd_average)
{
  if (argc == 0) {                        // 查询当前值
    shell_printf("%d\r\n", average_valid());
    return;
  }
  if (argc != 1)
    goto usage;
  int n = my_atoi(argv[0]);
  if (n < 1 || n > 8)
    goto usage;
  current_props._reserved[0] = (uint8_t)n;  // 直接写 RAM 工作副本
  return;
usage:
  shell_printf("usage: average {1-8}\r\n");
}
```

（`average_valid()` 已在 4.2 添加；若你把命令函数放在它之前，记得前移原型。）再在 [main.c:2170](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2170) `{"bandwidth", ...}` 一行后加：

```c
    {"average"    , cmd_average    , 0},
```

3. **需要观察的现象**：编译烧录后串口敲 `help` 列表里出现 `average`；`average` 回显 `1`；`average 8` 后再敲 `average` 回显 `8`；`average 0`、`average 9`、`average abc` 均打印 usage。
4. **预期结果**：如上；`average abc` 经 `my_atoi` 得 0 被拒（不会写坏字段）。串口行为待本地验证。
5. **思考题（动手前先答）**：若把 flags 改成 `CMD_WAIT_MUTEX`，功能还正确吗？会有什么副作用？（答：仍正确——命令会被 sweep 线程在测量间隙执行；副作用是 shell 在等待期间每 100ms 轮询一次、平均帧较长时命令延迟可达数秒，交互变迟钝。对照 [main.c:2299-2305](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2299-L2305)。）

#### 4.3.5 小练习与答案

**练习 1**：`cmd_average` 直接写 `current_props._reserved[0]`，为什么不像 `cmd_recall` 那样先 `ensure_edit_config()`？
**答案**：`ensure_edit_config()`（[main.c:842-852](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L842-L852)）解决的是 `active_props` 指向 flash 槽、而 `cal_data` 别名宏读的是 `active_props` 的问题——修改**校准类**状态前必须先把工作副本切换回 `current_props` 并作废校准状态。而 `_reserved[0]` 的别名目标就是 `current_props`（RAM，永远可写），与 `active_props` 指向无关；`menu_bandwidth_cb` 写 `_bandwidth`（[ui.c:634-639](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L634-L639)）同样不调用它。

**练习 2**：为什么不把命令做成 `average 1|2|4|8` 的字符串枚举（像 `bandwidth` 那样）？
**答案**：两者都成立。枚举法可用 `get_str_index` 少写几行校验，且天然限制取值集合；整数法对上位机脚本更友好（`"average %d" % n` 直接拼）。固件里 `marker`/`trace` 等命令也都用 `my_atoi` 整数参数，风格一致。

**练习 3**：`shell_printf` 与标准 `printf` 有何差别？
**答案**：`shell_printf`（[main.c:280-288](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L280-L288)）经 `chvprintf` 把格式化输出写到 USB CDC 流（`shell_stream`），是裁剪版实现（u5-l1）：不支持所有格式符，但新增了 `%q`（频率 SI 前缀）与 `%F`（自动前缀浮点）；`%f` 输出的是六位小数定点。写命令回显时用它而不是 `printf`。

### 4.4 properties_t 持久化：把 n 存进 `_reserved` 区

#### 4.4.1 概念说明

参数只存在 RAM 里，掉电就丢。NanoVNA 的持久化单位是 `properties_t`（0x1200 字节，含频点表与全部校准数据），由 `caldata_save(id)` 整块写入 5 个 flash 槽之一。给结构体加新字段有两条路：

- **路 A（扩字段）**：在 `_reserved[49]` 之前插入 `uint8_t _average;` 并把 `_reserved` 缩为 48 字节。结构体总长与 `checksum` 偏移都不变——这正是保留区的意义（u3-l4 已论证）。
- **路 B（吃保留区）**：直接用 `_reserved[0]` 当存储，配一个别名宏。零布局改动，旧固件源码与新固件源码生成的偏移完全一致。

本讲选路 B（改动最小、毕设验收快），路 A 作为练习。两种方式的**兼容性结论相同**：旧槽里该字节是 0，读取端用 `average_valid()` 把 0 钳制为 1，行为退化为「未开启平均」，不会出错；新值随下一次 `save id` 进 flash。这个「读到 0 就当默认值」的约定，是嵌入式中利用保留区做前向兼容的标准手法。

还有一个必须想清楚的时序问题：`recall id` 会把 flash 槽整块 `memcpy` 到 `current_props`（[flash.c:192](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L192)），于是 `average` 会被槽里的值覆盖——这不是 bug，恰恰是「测量现场快照」语义的正确表现：recall 恢复的就是保存那一刻的全部现场。

#### 4.4.2 核心流程

```text
设置：average 4 → current_props._reserved[0] = 4（RAM）
保存：save 2 → caldata_save(2)：
        current_props.magic = 'CONF'
        current_props.checksum = rotate_checksum(结构体除最后 4 字节外全部)
        擦除槽 2 的 3 页 → 逐半字写入整块 → active_props 指向 flash 槽
重启/recall：caldata_recall(2)：
        校验 magic + checksum → memcpy 回 current_props → average 读到 4
旧槽兼容：_reserved[0] == 0 → average_valid() 返回 1 → 功能关闭
```

#### 4.4.3 源码精读

- [nanovna.h:363-385](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L363-L385) `properties_t` 全貌：`magic` 打头、`checksum` 殿后（[nanovna.h:384](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L384)），[nanovna.h:383](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L383) 的 `_reserved[49]` 夹在 `_freq_mode` 与 `checksum` 之间——我们把第 0 个字节征用为平均次数。行注释 `sizeof(properties_t) == 0x1200`（[nanovna.h:387](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L387)）是必须守住的不变量。
- [nanovna.h:395-410](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L395-L410) 别名宏区。仿照 `#define bandwidth current_props._bandwidth`（[nanovna.h:409](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L409)）补一条 `average` 的别名，业务代码就能与 `bandwidth` 同风格地使用它。
- [flash.c:132-168](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L132-L168) `caldata_save`：注意 [flash.c:143-145](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L143-L145) 先置 magic 再算 checksum——校验范围是「整个结构体减去 checksum 字段自身」，`_reserved` **在覆盖范围内**，所以我们塞进去的字节不会破坏校验链（u3-l4 的 rotate 校验和见 [flash.c:67-76](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L67-L76)）。
- [flash.c:170-197](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L170-L197) `caldata_recall`：[flash.c:182-185](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L182-L185) 两级校验失败即回退默认；[flash.c:192](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L192) 的 `memcpy` 就是「recall 会覆盖 average」的来源。
- [main.c:817-840](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L817-L840) `load_default_properties`：回退默认时逐字段赋值（`_frequencies`、`_cal_data` 故意不填，见注释）。新字段要在这里给默认值，否则「恢复出厂」后 average 读到的是上次残留。
- [main.c:1518-1550](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1518-L1550) `cmd_save`/`cmd_recall`：shell 侧的保存/恢复入口，验收时直接用。

#### 4.4.4 代码实践

**实践：给 average 加持久化并验证掉电恢复。**

1. **实践目标**：`average n` → `save 0` → 断电重启（或 `recall 0`）后平均设置仍在；未保存过的旧槽恢复后 average 自动回到 1。
2. **操作步骤**：

在 [nanovna.h:410](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L410) `#define freq_mode ...` 之后加一行（示例代码）：

```c
#define average current_props._reserved[0]   // 1..8，0 视为 1（旧槽兼容）
```

在 `main.c` 的 `load_default_properties()` 末尾（[main.c:836](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L836) 附近）加（示例代码）：

```c
  current_props._reserved[0] = 1;   // 默认关闭平均
```

（此前 4.2/4.3 中所有 `current_props._reserved[0]` 可顺手替换成 `average`，与 `bandwidth` 风格统一。）然后编译烧录，串口依次执行：

```text
average 8        ← 设置
save 0           ← 写入 flash 槽 0（约几百毫秒，期间扫描暂停）
recall 0         ← 从槽 0 恢复
average          ← 应回显 8
```

有真机的读者再补一步断电重启后 `average` 查询。

3. **需要观察的现象**：`recall 0` 与断电重启后 `average` 均回显 8；对从未保存过 average 的旧槽执行 `recall` 后 `average` 回显 1。
4. **预期结果**：如上。若 `recall` 后回显 0，说明你漏了 `average_valid()` 的钳制——0 会直接落进 `average_sweeps()` 的 n=1 分支，功能上仍安全，但查询值不整洁。flash 写入耗时与断电保持待本地验证。
5. **体积自查**：改完跑 `arm-none-eabi-size build/ch.elf`，.data+.bss 不应增长（`_reserved` 是既有数组）——对照 u5-l3 的内存账本确认没有引入新的静态分配。

#### 4.4.5 小练习与答案

**练习 1**：把路 A（正式加字段 `_average`、`_reserved` 缩到 48）与路 B（吃 `_reserved[0]`）各自的优缺点说清楚。
**答案**：路 A 类型明确、自文档化、便于将来把 `uint8_t` 升级为更大字段；代价是要动结构体定义并重新核对 0x1200 不变量，且新旧固件源码虽偏移一致但语义上「字段」与「保留区」混用的历史包袱更长。路 B 零布局改动、与 `checksum` 范围天然兼容；缺点是 `_reserved[0]` 这个名字不表意，必须靠别名宏与注释弥补，第二个、第三个新字段会出现 `_reserved[1]`、`_reserved[2]` 各指什么的记忆负担。

**练习 2**：如果新字段放在 `checksum` **之后**会怎样？
**答案**：`caldata_save` 的写入长度是 `sizeof(properties_t)/2` 个半字、校验和覆盖「结构体减 checksum 字段」——放在 checksum 之后意味着它根本不在结构体内（编译报错），若硬塞在 `checksum` 前但计算校验和之后再写，则值不受校验保护，flash 位翻转无法被发现。结论：新持久化字段必须放在 `_reserved` 区域内（即 checksum 之前）。

**练习 3**：为什么 `caldata_save` 之后 `active_props` 要改指向 flash 槽（[flash.c:163-165](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L163-L165)），而我们的 `average` 写的仍是 `current_props`？
**答案**：`active_props` 是「校准数据权威原文」的指针：`cal_data` 别名宏读 `active_props->_cal_data`（[nanovna.h:400](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L400)），让它指向 flash 可以保证 RAM 副本被误改后校准插值（`cal_interpolate`）仍能从 flash 原文取数（u3-l3、u3-l4）。`average` 不是校准数据，它的权威值就在 `current_props`，下次 `save` 时随整块一起写回——两条数据流各走各的轨道，互不干扰。

### 4.5 三端贯通：UI 菜单挂接与 Python 端验证

#### 4.5.1 概念说明

一个完整的仪器特性应当有三个入口：**shell 命令**（自动化/上位机）、**UI 菜单**（手持操作）、**上位机 API**（数据分析）。shell 与持久化已就绪，本模块补齐后两个：仿照 `BANDWIDTH` 菜单做一个 `AVERAGE` 子菜单挂进 DISPLAY 菜单；再用 `python/nanovna.py` 抓数据，定量验证 √n 降噪。UI 部分是把 u4-l5 的表驱动菜单再练一遍；Python 部分则是把 u5-l2 的 `data` 通道用于你自己的固件特性——这也是二次开发的常见收尾：**功能验收标准用上位机脚本固化下来**。

#### 4.5.2 核心流程

```text
UI 侧：
  menu_display 表加一项 { MT_SUBMENU, 0, "AVERAGE", menu_average }
  menu_average[] 八个 MT_CALLBACK 项 + BACK + MT_NONE 哨兵
  menu_average_cb(item)：average = item + 1; draw_menu();
  高亮分支：menu == menu_average && item == average_valid()-1 时反色

验证侧（PC）：
  nv = NanoVNA(); nv.pause()
  对 average=1 与 average=8 各执行 scan，data(0) 取回 S11
  在「无谐振的平坦段」计算 |S11| 的标准差与均值，比较两者
```

#### 4.5.3 源码精读

**UI 侧模板：**

- [ui.c:941-949](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L941-L949) `menu_bandwidth[]` 表：五个 `MT_CALLBACK` 项 + `MT_CANCEL` 返回项 + [ui.c:948](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L948) 的 `MT_NONE` 哨兵（`draw_menu_buttons` 靠它停笔，见 [ui.c:1419-1421](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1419-L1421)）。
- [ui.c:634-639](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L634-L639) `menu_bandwidth_cb`：一行赋值 + `draw_menu()` 重画菜单。注意它的形参只有 `(int item)`，而回调类型 `menuaction_cb_t` 是两参的 `void (*)(int, uint8_t)`（[ui.c:436](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L436)）——这在 ARM EABI 下能工作（多出的寄存器实参被无视），但**新代码建议写全两个形参**，严格匹配类型。
- [ui.c:951-960](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L951-L960) `menu_display[]`：`BANDWIDTH` 在 [ui.c:957](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L957)，我们的 `AVERAGE` 加在它后面。
- [ui.c:1390-1394](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1390-L1394) 菜单绘制时的「当前档位高亮」分支：`menu == menu_bandwidth && item == bandwidth` 时反色。照此为 `menu_average` 加分支，选中状态才可见。
- [ui.c:527-536](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L527-L536) `menu_save_cb` 顺带复习：UI 上的 SAVE 最终也调 `caldata_save`——4.4 的持久化对 UI 路径同样生效，average 会被一起存进槽位。

**Python 侧通道：**

- [python/nanovna.py:46](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py#L46) `send_command`：命令以 `\r` 结尾发出并吞回显——`average 8` 这类自定义命令直接可用。
- [python/nanovna.py:162-170](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py#L162-L170) `data(array)`：发 `data N` 读回 `measured[N]` 的 101 行「实部 虚部」文本并拼成 numpy 复数数组。`data` 带 `CMD_WAIT_MUTEX`（[main.c:2165](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2165)），由 sweep 线程在测量间隙执行，读到的必是完整帧——平均后的数据同样经此通道出来。
- [python/nanovna.py:148-152](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py#L148-L152) `resume`/`pause`：抓数前暂停、抓完恢复，避免边扫边读（u5-l2 的快照一致性约定）。

#### 4.5.4 代码实践

**实践 A：挂接 AVERAGE 菜单。**

1. **实践目标**：在 DISPLAY 菜单下用触摸屏设置 1..8 档平均并高亮当前档。
2. **操作步骤**（均为示例代码，加入 `ui.c`）：

```c
static void menu_average_cb(int item, uint8_t data)
{
  (void)data;
  static const uint8_t avg_choice[] = { 1, 2, 4, 8 };
  average = avg_choice[item];   // item 0..3 → n 1/2/4/8（别名为 4.4 所加）
  draw_menu();
}

const menuitem_t menu_average[] = {
  { MT_CALLBACK, 0, "1", menu_average_cb },
  { MT_CALLBACK, 0, "2", menu_average_cb },
  { MT_CALLBACK, 0, "4", menu_average_cb },
  { MT_CALLBACK, 0, "8", menu_average_cb },
  { MT_CANCEL, 0, S_LARROW" BACK", NULL },
  { MT_NONE, 0, NULL, NULL } // sentinel
};
```

（只放 1/2/4/8 四档是刻意的：2 的幂在各档间换来整 dB 数改善，菜单也更短；也正因为表项值与下标不连续，回调里必须查 `avg_choice[]` 而不是 `item + 1`。）在 `menu_display[]`（[ui.c:957](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L957) 的 BANDWIDTH 行后）插入 `{ MT_SUBMENU, 0, "AVERAGE", menu_average },`；若 `menu_average` 定义在 `menu_display` 之后，需在其前面加 `extern const menuitem_t menu_average[];`（仓库已有同款先例 [ui.c:450](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L450)）。跨文件共享注意：`average` 别名宏定义在 `nanovna.h`，ui.c 一并可用；但 `average_valid()` 在 4.2 的示例中是 `static`，若高亮分支要用它，需去掉 `static` 并把声明加进 `nanovna.h`（与 `caldata_save` 等接口同区），或在 ui.c 里直接按 `current_props._reserved[0]` 比对。高亮：仿 [ui.c:1390-1394](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1390-L1394) 加 `menu == menu_average` 分支，条件为 `avg_choice[item] == average_valid()`——经 shell 设成 3 这类表中没有的值时，不高亮任何项即可。

3. **需要观察的现象**：DISPLAY → AVERAGE 出现子菜单；点 8 后对应项反色，扫频变慢、轨迹变细；shell 端 `average` 回显 8——两个入口改的是同一个变量。
4. **预期结果**：如上；UI 与 shell 状态一致。真机显示效果待本地验证。

**实践 B：Python 定量验证 √n 降噪（毕业验收）。**

先说清一个坑：**不要用 `scan` 命令做本验证**。`cmd_scan` 在 [main.c:927](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L927) 直接调 `sweep(false)`，绕过了我们挂在 Thread1 上的 `average_sweeps()`，读回的永远是单遍数据（详见 4.5.5 练习 1）。正确做法是让仪器保持连续扫描，平均后的整帧会持续刷新进 `measured`，等几帧后再用 `data 0`（`CMD_WAIT_MUTEX`，在测量间隙读取最近完整帧）取数。

1. **实践目标**：用数据证明平均 8 遍比 1 遍的噪声标准差低约 √8 倍（≈9 dB）。
2. **操作步骤**（示例代码，PC 端运行；DUT 建议接负载或一段空载电缆，选远离谐振的平坦频段）：

```python
# verify_average.py —— 示例代码：对比平均前后的 S11 噪声
import time
import numpy as np
from nanovna import NanoVNA

nv = NanoVNA()
nv.resume()                                    # 保持连续扫描，Thread1 持续输出平均帧
def grab(n):
    nv.send_command("average %d\r" % n)
    time.sleep(3 * n)                          # 帧周期∝n，等 3n 秒保证读到新设置下的完整帧
    return nv.data(0)                          # 读回 CH0 最近一帧（含平均）
g1, g8 = grab(1), grab(8)
band = slice(20, 80)                           # 取平坦段
for tag, g in (("n=1", g1), ("n=8", g8)):
    mag = np.abs(g[band])
    print(tag, "mean=%.4f std=%.5f" % (mag.mean(), mag.std()))
print("std ratio (期望≈√8=2.83):", np.abs(g1[band]).std()/np.abs(g8[band]).std())
```

3. **需要观察的现象**：两组 mean 基本一致（无偏）；std 之比接近 2.83；把 `np.abs(...)` 换成 `20*np.log10(np.abs(...))` 再算，n=8 组的标准差约为 n=1 组的 1/2.83（dB 尺度同样按 √n 缩）。
4. **预期结果**：std 之比落在 2 ~ 3.5 区间即算通过（真实设备存在漂移与残余相关，达不到理论值是正常的）；若两组几乎无差别，优先检查 `average_sweeps()` 是否真的被调用（`average` 查询值、刷新率是否变慢）。本验证需要真机，待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么实践 B 特意不用 `scan` 命令取数？
**答案**：`cmd_scan` 在 [main.c:927](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L927) 直接调用 `sweep(false)`，目的是「尽快给上位机一帧完整数据」（u2-l5），因此**不经过**我们挂在 Thread1 上的 `average_sweeps()`——用 `scan` 取数读到的永远是单遍数据，验证必然失败。改固件时必须逐个审计同一功能的旧入口（挂接点决定语义边界）。若确实希望上位机 `scan` 也能平均，把该处替换为 `average_sweeps()` 的不可打断变体（把内部的 `sweep(true)` 换成 `sweep(false)`）即可；实践 B 采用的规避方案则完全不依赖这条路径。

**练习 2**：把 `average` 暴露给 Python 的更优雅方式是什么？
**答案**：在 `python/nanovna.py` 里仿照 `resume`/`pause`（[python/nanovna.py:148-152](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py#L148-L152)）加一对方法：`def set_average(self, n): self.send_command("average %d\r" % n)`，把命令细节封装进类，脚本只面对 `nv.set_average(8)`。这就是 u5-l2 讲的「上位机库是固件命令表的镜像」——固件加命令，库同步加方法。

**练习 3**：为什么验证脚本取「平坦段」（slice(20, 80)）而不是全段？
**答案**：S11 曲线在谐振/匹配点附近是真值本身在快速变化，其「起伏」不是噪声；只有在真值近似常数的平坦段，`std` 才主要反映噪声幅度。这是把 4.1.5 练习 3 的「漂移混入」问题从数据侧排除的标准做法。

## 5. 综合实践

**毕业项目总验收：把三个模块的改动合起来走一遍完整迭代。**

建议按以下顺序实施并逐关验收（每关通过再进下一关）：

| 关卡 | 内容 | 通过标准 |
| --- | --- | --- |
| 0 编译基线 | 未改动前 `make` 一次，记录 `arm-none-eabi-size` 的 Flash/RAM 基数 | 编译零警告基线建立（u1-l2） |
| 1 核心算法 | 加入 `average_valid()` + `average_sweeps()`，替换 Thread1 调用 | 编译通过；RAM 增量为 0（spi_buffer 复用）；`average_sweeps` 未接命令前 n 恒为 1，行为与基线一致 |
| 2 shell 命令 | `cmd_average` + 命令表注册 | `help` 出现 average；设/查/非法参数三种输入行为正确 |
| 3 持久化 | 别名宏 + `load_default_properties` 默认值 | `average 8` → `save 0` → `recall 0` → `average` 回显 8；旧槽 recall 后回显 1 |
| 4 UI 菜单 | `menu_average` 表 + 回调 + 高亮分支 | 触摸可设档，与 shell 状态同步，当前档反色 |
| 5 Python 验收 | `verify_average.py` | n=1 与 n=8 的 mean 一致、std 之比 ≈ 2~3.5；画出两条 logmag 曲线肉眼可辨平滑差异 |

**最终交付物**：一份改动 diff（预计 `main.c` 约 50 行、`nanovna.h` 2 行、`ui.c` 约 20 行）、一张 n=1/n=8 对比截图、一段不超过 200 字的实现取舍说明（须至少覆盖：挂接层选择、spi_buffer 复用的安全性依据、`CMD_WAIT_MUTEX` 的取舍、旧槽 0 值兼容策略）。

**加分挑战**（选做）：

- 把 `cmd_scan` 路径也接入平均（4.5.5 练习 1），并在 Python 端封装 `set_average()`（练习 2）。
- 改用「指数移动平均」：\( \bar{\Gamma}_k = (1-\alpha)\bar{\Gamma}_{k-1} + \alpha\hat{\Gamma}_k \)，每帧只扫一遍但历史按权重衰减——刷新率不降，代价是阶跃响应变慢。思考它为什么对「边扫边看」的交互体验更友好。
- 用 `START_PROFILE/STOP_PROFILE`（[nanovna.h:492-493](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L492-L493)）实测一帧与八遍平均的耗时占比。

## 6. 本讲小结

- **挂接点选型先于写代码**：三个候选层（DSP 累积器 / 单频点 / 整帧）效果与代价完全不同；帧级 `measured` 平均与既有带宽档互补，且天然落在「误差修正之后、绘图之前」的正确位置。
- **复用优于新增**：16KB SRAM 下累加缓冲借 `spi_buffer`（4096B ≥ 1616B），安全性由「单写者 + 不与显示 DMA 并发」两条纪律保证——这是 `transform_domain` 已验证过的手法。
- **软浮点意识**：M0 无 FPU，热路径上「一次 `1.0f/n` + 404 次乘法」优于 404 次除法；数值范围（|Γ|≤1、累加≤8）反而从不是问题。
- **新命令 = 函数 + 一行表项**：flags 是否带 `CMD_WAIT_MUTEX` 取决于命令是否碰测量共享数据；`average` 只写一个字节，flags=0 即安全。
- **保留区是前向兼容的接口**：`_reserved[0]` 处于 checksum 覆盖范围内，存新参数零布局成本；读取端以「0 视为默认」消化旧槽，这正是 `_reserved` 存在的意义。
- **一个特性三个入口**：shell（自动化）、UI 菜单（手持）、Python（验收）共享同一个底层变量与数据通道，三者一致性本身就是对架构正确性的检验。

## 7. 下一步学习建议

到本讲为止，你已经沿着「信号源 → 采集 → 解调 → 校准 → 显示 → 扩展」的顺序读完了 NanoVNA 固件的全部主干。接下来有三个方向：

1. **对比社区演进**：NanoVNA 的后继固件（如 NanoVNA-D / NanoVNA-H 的 dylib 分支、tinySA 家族）在同样的 16KB 级硬件上实现了更多轨迹格式、触摸拖动 marker 等特性。带着本讲的方法论去 diff 它们与本仓库的差异，重点关注它们如何解决 `properties_t` 布局演进与 UI 栈扩展——你会发现保留区、表驱动菜单这些手法是共通的。
2. **深入信号链极限**：重读 [si5351.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c) 与 [dsp.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c)，思考：若把频偏从 5kHz 改成 10kHz，`sincos_tbl`、`AUDIO_BUFFER_LEN`、`bandwidth_accumerate_count` 的 RBW 标注各要怎么联动改？这是一份检验你是否真正打通 u2 全部四讲的答卷。
3. **做成自己的工具**：以毕业项目的 diff 为起点，继续实现你自己的需求——R/X 门限判断、简单峰值列表、或把 `capture` 帧做成上位机录屏。每加一个特性，都按本讲的流程走一遍「选型 → 最小改动 → 三端入口 → 数据验收」，这套习惯比任何单个知识点都值钱。
