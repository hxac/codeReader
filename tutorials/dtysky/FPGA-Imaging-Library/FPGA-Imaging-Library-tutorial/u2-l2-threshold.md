# Threshold——灰度转二值

## 1. 本讲目标

学完本讲，读者应该能够：

- 看懂一个「带运行期模式选择」的点运算 IP 是如何用 `case` 实现的。
- 区分两类「模式」：编译期参数 `work_mode`（`generate` 二选一）与运行期输入 `th_mode`（`case` 多分支）。
- 说清 `th_mode`、`th1`、`th2` 三个新引入信号的语义与取值范围。
- 把 `Threshold` 与上一讲的 `ColorReversal` 在「通道数、输入输出位宽、算法是否可选」上做横向对比。
- 独立给 `Threshold` 增加一种新的阈值模式，并打通「RTL → IP 描述 → 软件黄金模型 → 数据生成」的软硬一致性闭环。

---

## 2. 前置知识

在进入源码前，先用三段白话建立直觉。

### 2.1 什么是二值化（Thresholding）

二值化是把一张灰度图变成只有「黑」和「白」两种颜色的图。给定一个像素值 \(p\)（本项目中 \(0 \le p \le 255\)），我们用一个判定规则把它映射成 0 或 1：

\[ \text{out}(p) = \begin{cases} 1, & \text{满足规则} \\ 0, & \text{否则} \end{cases} \]

最常见的规则是「超过某个阈值就置 1」，这就是 `Threshold` 的 **Base** 模式。有时我们只想要「落在某个区间内」的像素，例如提取中等亮度的物体轮廓，这就是 **Contour** 模式。

> 术语提示：二值图里 1 通常显示为白、0 显示为黑。在本项目的软件黄金模型里，1 被写成 255（8 位全亮），0 写成 0；而硬件输出是真正的 1 位（0/1），还原脚本再用 PIL 的 1 位图模式把它显示成黑白。两者表达的「同一张二值图」，这正是「软硬一致性」。

### 2.2 两类「模式」不要混淆

`Threshold` 这个 IP 里出现了两个都叫 mode 的东西，初学者最容易混：

| 名称 | 类型 | 取值 | 何时决定 | 实现方式 |
| --- | --- | --- | --- | --- |
| `work_mode` | **参数**（parameter） | 0=Pipeline, 1=Req-ack | **综合前固定**，运行中不可改 | `generate if/else` 二选一 |
| `th_mode` | **输入端口**（input） | 0=Base, 1=Contour | **运行中可改**，每帧/每组数据可不同 | `case` 多分支 |

一句话记忆：`work_mode` 决定「数据怎么流」（流水线 vs 请求响应），`th_mode` 决定「算法是哪一种」（普通阈值 vs 区间阈值）。前者是骨架，后者是算法开关。

### 2.3 与 ColorReversal 的关系

上一讲 `ColorReversal` 的算法是写死的（`~in_data`，按位取反），没有运行期可调的参数。本讲 `Threshold` 在同样的「统一接口骨架」上，多出了 `th_mode/th1/th2` 三个运行期输入，并且**只处理单通道灰度图**（没有 `color_channels`）。可以把本讲理解为「在统一骨架上，给点运算加上可配置的算法分支」。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Point/Threshold/HDL/Threshold.srcs/sources_1/new/Threshold.v](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/sources_1/new/Threshold.v) | RTL 主模块，本讲精读对象。定义端口、参数、`case` 阈值比较与双模式寄存。 |
| [Point/Threshold/HDL/Threshold.srcs/sim_1/new/Threshold_TB.sv](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/sim_1/new/Threshold_TB.sv) | SystemVerilog testbench，同时例化流水线与请求响应两份 DUT，从 `.dat` 读入 `th_mode/th1/th2`。 |
| [Point/Threshold/HDL/Threshold.srcs/component.xml](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/component.xml) | IP-XACT 描述，登记端口位宽随 `color_width` 变化、`th_mode` 的可选枚举（Base/Contour）。 |
| [Point/Threshold/SoftwareSim/sim.py](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/SoftwareSim/sim.py) | Python 软件黄金模型，用 `im.point` 实现 Base/Contour，作为硬件比对的「标准答案」。 |
| [Point/Threshold/ImageForTest/conf.json](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/ImageForTest/conf.json) | 仿真配置，每组 `{mode, th1, th2}` 对应一个独立输出文件。 |
| [Point/Threshold/HDLSimDataGen/create.py](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDLSimDataGen/create.py) | 把图片与配置转成 `.dat` 激励，含 `th_mode/th1/th2` 头部。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**th_mode 模式选择**、**阈值比较**、**双模式寄存**。

### 4.1 th_mode 模式选择

#### 4.1.1 概念说明

`th_mode` 是一个**运行期输入端口**，用来在两种阈值算法之间实时切换。它的关键特征是：

- 它是 **input**，不是 parameter——意味着每一帧、甚至每一组测试数据都可以用不同的算法（testbench 正是这么做的：从 `.dat` 文件头部读出 `th_mode` 再驱动 DUT）。
- 因为是运行期选择，硬件必须**同时保留所有算法分支的电路**，再用 `case` 多路选择。这与 `work_mode` 的「综合期二选一、另一支电路根本不存在」截然不同。
- 当前 `th_mode` 只有 1 位（`input th_mode;` 未显式指定位宽即 1 位，`component.xml` 中登记为 `std_logic`），所以最多只能编码 2 种模式（0 与 1）。这一点直接决定了「想加第三种模式」要做什么——见 4.1.4 的实践。

为什么要把算法做成运行期可选？因为在真实图像处理流水线里，常常希望「同一份硬件，软件寄存器写 0 就做普通二值化、写 1 就做轮廓提取」，避免为每种算法各做一份 IP。

#### 4.1.2 核心流程

`th_mode` 从「配置」走到「硬件输出」的链路如下：

1. `conf.json` 写 `"mode": "Base"` 或 `"Contour"`。
2. `create.py` 的 `conf_format` 把字符串模式转成数字 `'0'`/`'1'`，写进 `.dat` 头部第一行。
3. testbench `init_file` 用 `$fscanf(fi, "%b", imconf)` 读出，赋给 `th_mode`。
4. RTL 里 `case (th_mode)` 选中对应比较表达式。
5. 软件侧 `sim.py` 用 `if conf['mode'] == 'Base'` 走对应 `im.point` 分支，产出黄金结果。

也就是说，`th_mode` 这一位在「软件参考实现」和「硬件实现」里是**同一套语义**，这是软硬一致性的前提。

#### 4.1.3 源码精读

端口声明——`th_mode` 是与 `th1/th2` 并列的普通输入，宽度 1 位：

[Threshold.v:94-98](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/sources_1/new/Threshold.v#L94-L98) 注释说明 `th_mode` 的取值（0 for Base, 1 for Contour）。

[Threshold.v:98](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/sources_1/new/Threshold.v#L98) `input th_mode;` ——未指定位宽，默认 1 位 `std_logic`。

`case` 在两个分支里各出现一次（流水线支与请求响应支），这是运行期选择的标志——综合后两条比较表达式都在硬件里：

[Threshold.v:146-150](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/sources_1/new/Threshold.v#L146-L150) 流水线支的 `case (th_mode)`，含 `default` 空分支。

对比 `work_mode` 的编译期选择——它用 `generate` 包裹整个 `if/else`：

[Threshold.v:143](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/sources_1/new/Threshold.v#L143) `if(work_mode == 0) begin` 与 [Threshold.v:153](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/sources_1/new/Threshold.v#L153) `end else begin`，综合时只保留其中一支。

`th_mode` 在 IP 描述里登记为 1 位 `std_logic`，并把两种模式做成下拉枚举：

[component.xml:101-113](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/component.xml#L101-L113) `th_mode` 端口为 `std_logic`（1 位）。

[component.xml:233-237](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/component.xml#L233-L237) `choices_0` 把 `Base`→0、`Contour`→1 列为枚举。

#### 4.1.4 代码实践

**实践目标**：把 `th_mode` 想象成「算法开关」，先不动代码，纯靠阅读把它的来龙去脉画出来。

**操作步骤**：

1. 打开 `ImageForTest/conf.json`，找到 `"mode": "Contour"` 这一组。
2. 打开 `HDLSimDataGen/create.py`，定位 `conf_format`，确认 `mode=='Contour'` 时头部写的是 `'1'`。
3. 打开 `HDL/.../Threshold_TB.sv` 的 `init_file`，确认第一行 `%b` 读进 `th_mode`。
4. 打开 `Threshold.v` 的 `case (th_mode)`，确认 `1` 对应区间比较。

**需要观察的现象**：`th_mode` 这一位在四个文件里语义完全一致——`"Contour"` ⇄ `'1'` ⇄ `th_mode=1` ⇄ `case 1`。

**预期结果**：画出一条「conf.json → create.py → .dat → TB → case」的信号传递链。

> 待本地验证：若手边有 Python 2.7 + PIL，可运行 `create.py` 后用文本编辑器打开生成的 `.dat`，第 3 行应能看到对应模式的 `0` 或 `1`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `work_mode` 用 `generate`、而 `th_mode` 用 `case`？

> **答**：`work_mode` 是综合前就固定的参数，用 `generate` 二选一可以让另一支电路完全不出现，省资源且不可运行时改；`th_mode` 是运行期输入，需要在同一份硬件里随时切换算法，所以必须用 `case` 把所有分支都保留下来。

**练习 2**：当前 `th_mode` 是几位？最多支持几种模式？若要支持 4 种，需要改成几位？

> **答**：1 位，最多 2 种（0、1）。要支持 4 种需改成 2 位（`input [1:0] th_mode;`），因为 \(2^2=4\)。

---

### 4.2 阈值比较

#### 4.2.1 概念说明

两种阈值算法的数学定义如下（\(p\) 为输入像素，均为无符号整数）：

Base 模式（单阈值，超过即置 1）：

\[ \text{out}_{\text{Base}}(p) = \begin{cases} 1, & p > th_1 \\ 0, & \text{otherwise} \end{cases} \]

Contour 模式（双阈值区间，落在 \((th_1, th_2]\) 内置 1）：

\[ \text{out}_{\text{Contour}}(p) = \begin{cases} 1, & th_1 < p \le th_2 \\ 0, & \text{otherwise} \end{cases} \]

两个细节值得注意：

- Base 用的是**严格大于** `p > th1`，所以 \(p = th_1\) 时输出 0。
- Contour 用 `p > th1 && p <= th2`，左开右闭；要让它有意义，需满足 \(th_2 > th_1\)。
- `th2` 只在 Contour 模式下被使用，Base 模式下它虽然存在却不影响结果（这也是 `conf.json` 里 Base 组 `"th2": "0"` 的原因）。

#### 4.2.2 核心流程

比较本身是纯组合逻辑（一个比较器 + 一个与门），但因为输出要走统一接口的寄存器，所以被包进 `always` 块里打一拍：

```
in_data ──► ( > th1 ) ──────────────┐
                                     ├──► case(th_mode) ──► reg_out_data ──► out_data
in_data ──► ( > th1 && <= th2 ) ─────┘
```

`th_mode` 作为多路选择器的选择信号，决定哪一路比较结果被锁存进 `reg_out_data`。

#### 4.2.3 源码精读

流水线支的比较与 `case`：

[Threshold.v:145-151](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/sources_1/new/Threshold.v#L145-L151) `posedge clk` 锁存，`case 0` 为 Base（`in_data > th1 ? 1 : 0`），`case 1` 为 Contour（`in_data > th1 && in_data <= th2 ? 1 : 0`），`default` 为空。

请求响应支的比较与 `case`（逻辑完全相同，仅敏感沿不同）：

[Threshold.v:155-161](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/sources_1/new/Threshold.v#L155-L161) `posedge in_enable` 锁存，两支 `case` 表达式与上一段一字不差。

软件黄金模型做的是同一件事，用 `im.point` 逐像素映射：

[sim.py:76-79](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/SoftwareSim/sim.py#L76-L79) Base 为 `255 if p > th1 else 0`，Contour 为 `255 if p > th1 and p <= th2 else 0`。注意软件把 1 写成 255 以便直接存成可见的灰度图，而硬件输出真正的 1 位 0/1。

阈值信号本身是运行期输入，位宽随 `color_width` 变化：

[Threshold.v:100-108](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/sources_1/new/Threshold.v#L100-L108) `th1` 用于所有模式，`th2` 仅 Contour 用，二者宽度均为 `[color_width-1:0]`。

#### 4.2.4 代码实践

**实践目标**：用纸笔或脑算验证「严格大于」与「左开右闭」的边界行为。

**操作步骤**：

1. 设 `color_width=8`、Base 模式、`th1=128`。
2. 分别取 \(p = 127, 128, 129\)，套用 \(p > th_1\) 判断输出。
3. 再设 Contour 模式、`th1=50, th2=200`，取 \(p = 50, 51, 200, 201\)，套用 \(th_1 < p \le th_2\) 判断输出。

**需要观察的现象 / 预期结果**：

| 模式 | 参数 | \(p\) | 输出 |
| --- | --- | --- | --- |
| Base | th1=128 | 127 | 0 |
| Base | th1=128 | 128 | 0（严格大于，等于不算） |
| Base | th1=128 | 129 | 1 |
| Contour | th1=50, th2=200 | 50 | 0（左开） |
| Contour | th1=50, th2=200 | 51 | 1 |
| Contour | th1=50, th2=200 | 200 | 1（右闭） |
| Contour | th1=50, th2=200 | 201 | 0 |

> 待本地验证：可临时在 testbench 里把这几个 \(p\) 值喂给 DUT，比对 `out_data` 是否与上表一致。

#### 4.2.5 小练习与答案

**练习 1**：Base 模式下，`th1=128`、像素值正好是 128，输出是多少？为什么？

> **答**：0。因为表达式是 `in_data > th1`，128 > 128 为假。若想「≥」语义，应把阈值设成 127。

**练习 2**：Contour 模式下，若误把 `th2` 设成比 `th1` 还小的值（如 `th1=200, th2=50`），会发生什么？

> **答**：条件 \(p > 200 \,\land\, p \le 50\) 恒为假，全图输出 0。所以使用 Contour 时必须保证 \(th_2 > th_1\)。

**练习 3**：为什么 `case` 里要保留 `default : /* default */` 空分支？

> **答**：`th_mode` 虽是 1 位、理论上只有 0/1，但写 `default` 是良好的可综合习惯，能避免综合工具对未覆盖分支插入锁存器，也方便日后扩展新模式时不会漏掉某一支。

---

### 4.3 双模式寄存

#### 4.3.1 概念说明

「双模式」在这里指 `work_mode` 选出的流水线 / 请求响应两套寄存写法，它们的**算法完全相同，只是锁存时机不同**。这与 `ColorReversal` 是同一套骨架，区别只在锁存的表达式从 `~in_data` 换成了 `case(th_mode)` 的比较结果。

回顾两种模式的时序形态（承接 u1-l4 / u2-l1）：

- **Pipeline（work_mode=0）**：每个 `posedge clk` 都锁存一次，吞吐 1 像素/时钟，适合连续数据流。
- **Req-ack（work_mode=1）**：只在 `posedge in_enable`（请求上升沿）锁存一次，适合节奏不定的逐笔握手。

此外，`out_ready` 与 `out_data` 的门控逻辑与 `ColorReversal` **一字不差**——这说明全库点运算 IP 共享同一套握手外壳，差别只在「算法那一行」。这正是 F-I-L「统一接口」的威力：学会一个，就能读懂一片。

#### 4.3.2 核心流程

```
                   ┌─────────────────────────────────────────┐
in_enable ─┬──────►│ reg_out_ready: 三沿敏感 + 联合复位       │──► out_ready
rst_n ─────┼──────►│ (posedge clk / negedge rst_n /          │
           │       │  negedge in_enable)                     │
           │       └─────────────────────────────────────────┘
           │       ┌─────────────────────────────────────────┐
           │  work_mode==0: always @(posedge clk)            │
in_data ───┼──────►│   case(th_mode): 比较表达式             │──► reg_out_data ──┐
th_mode ───┤       │ work_mode==1: always @(posedge in_enable)│                   │
th1, th2 ──┘       └─────────────────────────────────────────┘                   ▼
                                                                       out_ready==0 ? 0 : reg_out_data
                                                                              ──► out_data
```

`out_ready` 的上升比 `in_enable` 晚一拍（上升沿不在敏感列表），下降则因 `negedge in_enable` 在敏感列表里而立刻清零；`out_data` 在 `out_ready==0` 时被门控为 0，给下游一个确定的无效占位。

#### 4.3.3 源码精读

`out_ready` 寄存器——与 `ColorReversal` 完全同构：

[Threshold.v:135-141](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/sources_1/new/Threshold.v#L135-L141) 三沿敏感列表 + `if(~rst_n || ~in_enable)` 联合清零，否则置 1。

`work_mode` 编译期二选一，包住「算法 `always`」：

[Threshold.v:143-163](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/sources_1/new/Threshold.v#L143-L163) `if(work_mode==0)` 保留 `posedge clk` 支，`else` 保留 `posedge in_enable` 支；两支内部 `case` 表达式完全一致。

`out_ready` / `out_data` 的连续赋值与门控：

[Threshold.v:165-166](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/sources_1/new/Threshold.v#L165-L166) `assign out_ready = reg_out_ready;` 与 `assign out_data = out_ready == 0 ? 0 : reg_out_data;`。

testbench 同时例化两份 DUT 来对比两种模式：

[Threshold_TB.sv:86-91](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/sim_1/new/Threshold_TB.sv#L86-L91) `Threshold #(0, 8)` 为流水线版，`Threshold #(1, 8)` 为请求响应版（参数顺序对应 `work_mode, color_width`）。

两种模式各自的驱动任务：

[Threshold_TB.sv:119-142](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/sim_1/new/Threshold_TB.sv#L119-L142) `work_pipeline` 每拍拉高 `in_enable` 并读入数据；`work_regack` 拉高 `in_enable` 后等 `out_ready` 再读、随后拉低 `in_enable`。两种驱动方式产出 `-pipeline.res` 与 `-reqack.res` 两份结果。

#### 4.3.4 代码实践

**实践目标**：通过阅读 testbench 理解「同一份 RTL、两种模式产生两份结果文件」的机制。

**操作步骤**：

1. 打开 `Threshold_TB.sv` 的 `initial` 块，找到对每个文件先跑 `work_pipeline` 写 `-pipeline.res`、再跑 `work_regack` 写 `-reqack.res` 的两段循环。
2. 对比 `work_pipeline` 与 `work_regack` 对 `in_enable` 的不同用法。
3. 确认两份 `.res` 最终都会被 `SimResCheck/convert.py` 还原成 `-hdlfun.bmp`，并与软件 `-soft.bmp` 做 PSNR 比对。

**需要观察的现象**：两种模式虽然时序不同，但喂入同一张图、同一组 `th_mode/th1/th2` 时，逐像素输出应当**完全相同**（仅时间分布不同）。

**预期结果**：两种模式的 PSNR 都应为 \(10^6\)（完全一致）。

> 待本地验证：完整跑通 ModelSim 仿真 + `convert.py` + `compare.py` 后查看报告；无法跑工具链时，至少能在源码层面确认两支 `case` 表达式一致。

#### 4.3.5 小练习与答案

**练习 1**：`out_data` 为什么要写成 `out_ready == 0 ? 0 : reg_out_data`，而不是直接 `assign out_data = reg_out_data;`？

> **答**：为了在无效拍（`out_ready=0`）把输出钳到确定的 0，避免下游读到残留的旧比较结果，给下游一个干净的无效占位。

**练习 2**：请求响应模式下，`reg_out_data` 在哪个信号的动作沿被更新？

> **答**：在 `posedge in_enable`（请求上升沿），而不是 `posedge clk`。所以只有「发起一次请求」时才会锁存新结果。

**练习 3**：把 `Threshold` 的 `out_ready` 电路与 `ColorReversal` 对照，结论是什么？

> **答**：两者 `out_ready` 的三沿敏感列表、联合复位、门控输出**完全相同**。差别只在 `reg_out_data` 的算法行——`ColorReversal` 是 `~in_data`，`Threshold` 是 `case(th_mode)` 的比较结果。这印证了 F-I-L 点运算 IP 共享同一套握手外壳。

---

## 5. 综合实践：给 Threshold 增加第三种阈值模式「Outside」

本任务把三个最小模块串起来：你要新增一种「窗口取反」模式——像素落在 \([th_1, th_2]\) 区间**之外**时输出 1，即 \(p \le th_1 \lor p > th_2\)。这恰好是 Contour 的逻辑取反，可用来「抠掉中间灰度、保留两端」。

### 5.1 实践目标

- 学会把一个 1 位的运行期选择信号扩展为多位。
- 让 RTL、IP 描述、软件黄金模型、数据生成四处保持语义一致。
- 体会「软硬一致性」要求每一层同步修改。

> 重要提示：因为 `th_mode` 当前是 **1 位**，只能编码 0/1 两种模式。要加入第三种，**必须先把 `th_mode` 扩到 2 位**。这是本实践的核心难点。

### 5.2 操作步骤

下面所有代码片段均为**示例代码**（非项目原有内容），改完请勿提交到源码仓库——本讲只允许写到 `FPGA-Imaging-Library-tutorial/` 下。

**第 1 步：扩展 RTL（`Threshold.v`）。** 把 `th_mode` 改成 2 位，并在两个 `case` 里各加一支：

```verilog
// 示例代码：端口改为 2 位
input [1:0] th_mode;
```

```verilog
// 示例代码：在 case 中新增 mode 2（Outside）
case (th_mode)
    0 : reg_out_data <= in_data > th1 ? 1 : 0;                       // Base
    1 : reg_out_data <= in_data > th1 && in_data <= th2 ? 1 : 0;     // Contour
    2 : reg_out_data <= in_data <= th1 || in_data > th2 ? 1 : 0;     // Outside（新增）
    default : /* default */;
endcase
```

注意：流水线支（`posedge clk`）和请求响应支（`posedge in_enable`）**都要加**这一行，否则两种模式下行为不一致。

**第 2 步：更新 IP 描述（`component.xml`）。** 把 `th_mode` 端口位宽改为 2 位，并在枚举里加 `Outside`→2：

```xml
<!-- 示例代码：th_mode 改为 2 位向量，参考 th1 的 vector 写法 -->
```

并在 `choices_0` 里追加一行 `<spirit:enumeration spirit:text="Outside">2</spirit:enumeration>`。

**第 3 步：更新 testbench（`Threshold_TB.sv`）。** `TBInterface` 里 `bit th_mode;` 改成 `bit[1:0] th_mode;`，并确认 `init_file` 读 `th_mode` 时能容纳 2 位（`imconf` 已是 8 位，可继续承接）。

**第 4 步：更新软件黄金模型（`SoftwareSim/sim.py`）。** 在 `transform` 与 `debug` 里加入 Outside 分支：

```python
# 示例代码：软件黄金模型新增 Outside
if conf['mode'] == 'Base':
    im_res = im.point(lambda p : 255 if p > th1 else 0)
elif conf['mode'] == 'Outside':
    im_res = im.point(lambda p : 255 if p <= th1 or p > th2 else 0)
else:  # Contour
    im_res = im.point(lambda p : 255 if p > th1 and p <= th2 else 0)
```

同时把 `if conf['mode'] not in ['Base', 'Contour']:` 的合法集合扩成 `['Base', 'Contour', 'Outside']`。

**第 5 步：更新数据生成（`HDLSimDataGen/create.py`）。** 在 `conf_format` 里让 `Outside` 写出头部 `'2'`，并把合法模式集合同步扩展。

**第 6 步：新增一组配置（`ImageForTest/conf.json`）。** 例如：

```json
{
    "mode": "Outside",
    "th1": "50",
    "th2": "200"
}
```

### 5.3 需要观察的现象与预期结果

- 重新跑五步仿真后，应多出一个 `-Outside-50-200-soft.bmp`（软件）与对应的 `-hdlfun.bmp`（硬件）。
- 两张图的语义应是「原图中亮度 ≤50 或 >200 的像素为白，其余为黑」——即 Contour 结果的取反。
- `compare.py` 报告里 Outside 这一组的 PSNR 应为 \(10^6\)（完全一致）。

> 待本地验证：本实践涉及 Python 2.7 + PIL、ModelSim、Vivado 三套工具链的完整闭环，作者未在本环境实际运行；若暂时无法跑通工具链，可降级为「源码阅读型实践」——只做第 1 步 RTL 修改，然后逐层比对 4.1.4 里那条信号链，确认 `th_mode` 从 1 位扩到 2 位后，四个文件的语义仍自洽。

---

## 6. 本讲小结

- `Threshold` 是一个**单通道灰度**二值化 IP：没有 `color_channels`，`in_data`/`out_data` 都是单条总线，且 `out_data` 只有 1 位（0/1）。
- 它引入了**运行期算法开关** `th_mode`（0=Base、1=Contour），用 `case` 实现；这与 `work_mode`（编译期、`generate` 二选一）是两类完全不同的「模式」。
- `th1` 用于所有模式、`th2` 仅 Contour 用；Base 是严格大于 \(p > th_1\)，Contour 是左开右闭 \(th_1 < p \le th_2\)。
- 「双模式寄存」与 `ColorReversal` 共享同一套 `out_ready`/`out_data` 握手外壳，差别只在算法那一行——印证 F-I-L 点运算 IP 的统一接口。
- `th_mode` 当前是 1 位，故最多 2 种模式；新增模式必须先扩位宽，并同步 RTL、`component.xml`、软件模型、`create.py`、`conf.json` 五处。
- 软硬一致性要求 `th_mode` 在 conf.json / create.py / testbench / `case` / sim.py 里保持同一套语义。

---

## 7. 下一步学习建议

- **回到通道相关运算**：本讲是单通道、且 `case` 比较天然「通道无关」。下一站可阅读一个**多通道、通道相关**的点运算（如 `Graying` 或带增益的点运算），观察 `genvar i` 按通道展开的写法，与本讲形成对照。
- **进入常系数乘法 / 定点增益**：阈值只是「比较」，后续点运算会引入乘法与定点量化，届时会看到 `color_width` 之外的中间位宽问题。
- **自己加一个模式**：把第 5 节的「Outside」实践做完，是检验是否真正理解 `th_mode` + 软硬一致性闭环的最佳方式——做通它，你就有了按 F-I-L 规范二次开发点运算 IP 的能力。
