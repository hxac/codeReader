# 仿真与 testbench：data_gen 激励与逐级验证

## 1. 本讲目标

前几讲我们读完了「蝶形 → 延时 → 旋转因子 → 复数乘法」这条数据通路，也理解了 `fft_top` 如何把 14 级串成流水线。但 RTL 代码写对没有，最终只能靠**仿真**来回答。本讲就把目光从「设计源码」转到「如何驱动并观察设计」，学完之后你应当能够：

1. 看懂一个标准 Verilog testbench 的三段式骨架：声明 → 时钟/复位初始化 → 例化被测模块（DUT）与激励发生器。
2. 读懂 `data_gen.v` 这个参数化激励发生器：它是如何仅凭一个 `layer` 参数，同时派生出 `valid`（有效窗口）、`start`（启动脉冲）、`over`（结束脉冲）和递增的 `data_real` 测试数据的。
3. 理解 `data_gen` 与被测模块的**握手对接约定**：为什么 `data_gen` 的 `layer` 必须取成使 `data_gen.PERIOD == 被测模块.PERIOD`。
4. 掌握项目「由小到大」的分级验证策略：`data_gen_tb`（激励自身）→ `fft_8_tb`（单级低层）→ `fft_general_tb`（单级高层）→ `fft_top_tb`（全链路）。

## 2. 前置知识

在进入源码前，先统一几个概念。

- **DUT（Design Under Test，被测设计）**：仿真里被验证的那个模块，比如 `fft_8`、`fft_top`。
- **testbench（测试平台）**：一个**没有端口**的顶层模块（`module xxx_tb();` 后面括号空着），它内部产生时钟和复位、例化 DUT、喂入激励、观察输出。它本身不会被综合成硬件，只在仿真器（如 Vivado Simulator、ModelSim、Icarus Verilog）里运行。
- **激励（stimulus）**：喂给 DUT 输入端的各种信号波形。本项目的激励统一由 `data_gen` 产生，这样每个 testbench 的激励逻辑只写一遍、靠参数复用。
- **`reg` 与 `wire`**：testbench 里 `clk`/`rst` 这类由自己驱动的信号声明为 `reg`；DUT 输出、连到例化端口的信号声明为 `wire`。
- **复位有效电平**：本项目统一用**高有效**复位 `rst`（`rst==1` 表示复位），这和 `multiplier` 里的低有效 `rstn` 相反，所以会看到 `.rstn(~rst)` 这种取反写法（详见 u2-l2）。
- **SDF 与 PERIOD**：每级流水线都有一个 `PERIOD = 1<<layer`（即 \(2^{layer}\)），它是该级蝶形上下支切换的周期。`data_gen` 必须按相同的 PERIOD 节拍喂数据，这一点是本讲的核心，后面会反复用到。

> 本讲承接 u4-l2（`fft_8`/`fft_16` 的状态机与 PERIOD）与 u3-l3（级间握手 `start_next`/`start`）。建议先确认你记得 `fft_8` 内部 `S8` 控制信号在 `PERIOD/2-1` 与 `PERIOD-1` 处翻转这一结论，因为 `data_gen` 的 `valid` 会在**完全相同的两个时刻**翻转——这不是巧合。

## 3. 本讲源码地图

本讲涉及的关键文件分两类：激励发生器与各级 testbench。

| 文件 | 角色 | 作用 |
| --- | --- | --- |
| [src/data_gen.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/data_gen.v) | 激励发生器（设计源码） | 用 `layer` 参数派生 `valid/start/over/data_real`，是所有 testbench 的「数据源」 |
| [tb/fft_8_tb.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/tb/fft_8_tb.v) | 单级低层 testbench | 验证 `fft_8`，是「最简标准 testbench 模板」 |
| [tb/fft_4_tb.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/tb/fft_4_tb.v) | 单级低层 testbench | 验证 `fft_4`，本讲动手实践的目标 |
| [tb/fft_general_tb.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/tb/fft_general_tb.v) | 单级高层 testbench | 验证 `fft_1k`，代表 `fft_32~fft_16k` 一整套同构高层模块 |
| [tb/fft_top_tb.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/tb/fft_top_tb.v) | 全链路 testbench | 验证 `fft_top` 整条 14 级流水线，并计算频谱幅度 |
| [tb/data_gen_tb.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/tb/data_gen_tb.v) | 激励自检 testbench | 单独跑 `data_gen`，用波形确认激励本身正确 |

> 提示：仓库里 `_tb` 文件**同时散落在 `tb/` 和 `src/` 两个目录**（例如 `src/data_gen_tb.v`、`src/fft_general_tb.v`、`src/top_tb.v` 都是 testbench 而非设计源码，参见 u1-l2 的提醒）。下文统一以 `tb/` 目录下的版本为准。

## 4. 核心概念与源码讲解

### 4.1 testbench 的标准骨架：时钟、复位与例化（以 fft_8_tb 为例）

#### 4.1.1 概念说明

一个 testbench 要回答三件事：**什么时候开始**（复位与启动）、**节拍是什么**（时钟）、**把激励和被测模块怎么接起来**（例化与连线）。`fft_8_tb` 是项目里最干净的标准模板，只有 50 行，却把这四件事都讲清楚了。我们先把它当作「样板间」逐段拆解。

#### 4.1.2 核心流程

`fft_8_tb` 的执行流程可以概括为三段：

1. **复位与时钟初始化**：上电时 `clk=1, rst=1`，维持 30 ns 后 `rst=0` 释放复位；时钟由 `always #(period/2) clk = ~clk;` 永远翻转产生。
2. **例化 DUT**：把 `fft_8` 的端口接到一组 `wire` 上，其中 `start8/end8` 由激励驱动，`out_real8/out_img8` 留给观察。
3. **例化激励**：例化一个 `data_gen #(.layer(3))`，由它产生 `data_real/data_img/valid/start/over`，并把这些信号连到 DUT 对应端口。

#### 4.1.3 源码精读

先看信号声明与时钟/复位初始化：

[tb/fft_8_tb.v:16-23](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/tb/fft_8_tb.v#L16-L23) —— 定义 `period=10`（10 ns 时钟周期），`initial` 块里先让 `rst=1`，`#30` 后拉低；`always #(period/2) clk=~clk` 产生方波时钟。注意 `clk/rst` 是 `reg`（testbench 自己驱动），其余连接线是 `wire`。

接着是 DUT 例化：

[tb/fft_8_tb.v:25-36](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/tb/fft_8_tb.v#L25-L36) —— 把 `fft_8` 的 `start8` 接到 `w_start8`、`end8` 接到 `w_ending8`、`A_real/A_img` 接到激励数据、`out_real8/out_img8` 接到观察线。`start4/end4` 是 `fft_8` 输出给下一级 `fft_4` 的握手信号，这里用 `w_start4/w_ending4` 挂空观察（本级不级联下一级）。

最后是激励例化：

[tb/fft_8_tb.v:39-48](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/tb/fft_8_tb.v#L39-L48) —— `data_gen #(.layer(3))`，把它的 `start` 连到 `w_start8`、`over` 连到 `w_ending8`。**关键就是这一行的 `.layer(3)`**——它决定了整个激励的节拍，也是下一节要展开的重点。

> 对照 `src/fft_8.v` 可见，`fft_8` 内部 `PERIOD=8`（[src/fft_8.v:23-24](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_8.v#L23-L24)），而 `data_gen #(.layer(3))` 的 `PERIOD` 也是 \(2^3=8\)。两者相等，这是 testbench 能跑通的隐含前提。

#### 4.1.4 代码实践

**实践目标**：在不开仿真器的前提下，靠阅读确认 testbench 的「上电时序」。

**操作步骤**：

1. 打开 [tb/fft_8_tb.v:17-23](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/tb/fft_8_tb.v#L17-L23)。
2. 回答：`rst` 在仿真开始后第几纳秒被释放？此时 `clk` 已经翻转了几次？
3. 找到 `period=10`，确认时钟半周期是 5 ns，即 100 MHz。

**需要观察的现象 / 预期结果**：`#30` 表示 30 ns 后释放复位；30 ns 内时钟每 5 ns 翻转一次，约翻转 6 次，足以让所有 `posedge rst` 触发的寄存器进入已知初值。预期复位在 30 ns 时刻由 1→0。

**待本地验证**：以上时间点需在实际仿真波形中确认；不同仿真器的 `timescale` 处理一致，但建议在波形里直接量 `rst` 的下降沿时刻。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `clk` 和 `rst` 声明为 `reg`，而 `data_real/out_real8` 声明为 `wire`？

**答案**：`clk/rst` 由 testbench 内部的 `initial`/`always` 块驱动（过程赋值），必须用 `reg`；`data_real` 由 `data_gen` 例化的输出端口驱动、`out_real8` 由 `fft_8` 例化的输出端口驱动（连续驱动），必须用 `wire`。

**练习 2**：如果把 `always #(period/2) clk = ~clk;` 改成 `always #period clk = ~clk;`，时钟频率会变成多少？

**答案**：半周期从 5 ns 变成 10 ns，整周期从 10 ns 变成 20 ns，频率从 100 MHz 降到 50 MHz。

---

### 4.2 data_gen 激励发生器：layer 参数如何派生 valid/start/over 与递增数据

#### 4.2.1 概念说明

`data_gen` 是整个仿真体系的「心脏」。它的妙处在于：**只有一个参数 `layer`，却能同时产出四个意义不同的信号**——标记有效数据的 `valid`、启动 DUT 的单拍 `start`、结束 DUT 的单拍 `over`、以及递增的测试数据 `data_real`。这四个信号全部由 `layer` 派生的几个常量统一步调，所以只要 `layer` 选对，激励就自然与 DUT 的 PERIOD 对齐。

#### 4.2.2 核心流程

`data_gen` 内部由三个计数器 + 一组派生常量协同工作：

1. **派生常量**（由 `layer` 决定）：
   - `PERIOD = 1<<layer`（即 \(2^{layer}\)），节拍周期；
   - `WAIT_CLK_NUM = 1<<layer = PERIOD`，`start` 脉冲的触发点；
   - `DATA_NUM = 1<<(layer+2) = 4*PERIOD`，一帧测试数据的总长度（4 个 PERIOD）。
2. **`counter_for_valid`**：0→`PERIOD-1` 循环。`valid` 在它等于 `PERIOD/2-1` 和 `PERIOD-1` 两个点翻转，于是 `valid` 在每个 PERIOD 的**后半段**（`PERIOD/2` 拍）为高。
3. **`counter`**：0→`DATA_NUM-1` 循环，标记一帧内的时间位置。`start` 在 `counter==PERIOD-2` 时发一个单拍脉冲（帧首发启动）；`over` 在 `counter==DATA_NUM-2` 时发一个单拍脉冲（帧尾发结束）。
4. **`data_real_tmp`**：0→`PERIOD-1` 自由循环递增，是一把锯齿波测试数据；`data_img` 直接硬接 0（即只测实信号输入）。

用公式概括三个关键节拍：

\[
\text{PERIOD} = 2^{\text{layer}}, \quad \text{DATA\_NUM} = 4\cdot\text{PERIOD}, \quad \text{valid 高电平宽度} = \text{PERIOD}/2
\]

#### 4.2.3 源码精读

先看端口与派生常量：

[src/data_gen.v:2-14](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/data_gen.v#L2-L14) —— 模块端口只有 `clk/rst` 输入和 `data_real/data_img/valid/start/over` 五个输出；`WAIT_CLK_NUM/DATA_NUM/PERIOD` 三个本地常量全部由 `layer` 算出。

`valid` 的翻转逻辑（这是和 DUT 的 `S` 控制同节拍的关键）：

[src/data_gen.v:23-47](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/data_gen.v#L23-L47) —— `valid_tmp` 在 `counter_for_valid==PERIOD/2-1` 与 `==PERIOD-1` 两处翻转。对比 [src/fft_8.v:103](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_8.v#L103) 中 `S8` 的翻转点 `S8_counter==PERIOD/2-1 | S8_counter==PERIOD-1`，**两者完全是同一对时刻**——这就是为什么激励能和蝶形上下支天然对齐。

`start` 与 `over` 的单拍脉冲：

[src/data_gen.v:62-84](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/data_gen.v#L62-L84) —— `r_start` 仅在 `counter==WAIT_CLK_NUM-1-1`（即 `PERIOD-2`）那一拍为 1，其余为 0，是一帧唯一的启动脉冲；`r_ending` 仅在 `counter==DATA_NUM-2`（帧倒数第二拍）为 1，是结束脉冲。这两个脉冲分别喂给 DUT 的 `start`（触发 `STATE_IDLE→STATE_START`）和 `end/over`（触发 `STATE_PROCESSING→STATE_END`）。

测试数据与输出赋值：

[src/data_gen.v:87-118](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/data_gen.v#L87-L118) —— `data_real_tmp` 在 0~`PERIOD-1` 间循环递增（锯齿波）；注意 [src/data_gen.v:111-115](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/data_gen.v#L111-L115) 处把原本的定点放大 `data_real_tmp << 16`（Q16）注释掉了，**当前生效的是 `assign data_real = data_real_tmp;`**，即喂进去的是 0~`PERIOD-1` 的原始小整数；`data_img` 硬接 0。这一点对后续和 MATLAB 黄金参考比对很关键（见综合实践）。

#### 4.2.4 代码实践

**实践目标**：手算 `layer=3`（即 `fft_8_tb` 的配置）时 `data_gen` 一帧内的关键节拍，把「读代码」变成「能预报波形」。

**操作步骤**：

1. 取 `layer=3`，算出 `PERIOD=8`、`DATA_NUM=32`、`WAIT_CLK_NUM=8`。
2. 列出 `counter_for_valid` 从 0 到 7 时 `valid` 的取值（提示：在 cfv=3 和 cfv=7 处翻转）。
3. 找出 `start=1` 的那个 `counter` 值、`over=1` 的那个 `counter` 值。
4. 写出 `data_real_tmp` 在前 12 拍的序列。

**需要观察的现象 / 预期结果**：

- `valid` 在 `counter_for_valid ∈ {4,5,6,7}` 为高、`{0,1,2,3}` 为低（后半周期高）。
- `start=1` 出现在 `counter==6`（`PERIOD-2`），整帧只此一拍。
- `over=1` 出现在 `counter==30`（`DATA_NUM-2`），整帧只此一拍。
- `data_real_tmp` 序列：`0,1,2,3,4,5,6,7,0,1,2,3,...`（8 循环锯齿）。

**待本地验证**：在仿真波形里把 `data_real/valid/start/over` 四条信号拉出来，逐一核对手算结果。

#### 4.2.5 小练习与答案

**练习 1**：`layer=3` 时一帧 `DATA_NUM` 是多少？`data_gen` 在一帧里发出几个 `start` 脉冲、几个 `over` 脉冲？

**答案**：`DATA_NUM = 1<<(3+2) = 32`。`start` 和 `over` 各只发 1 个单拍脉冲（`counter` 在一帧内分别只有一拍等于 `PERIOD-2` 和 `DATA_NUM-2`）。

**练习 2**：为什么 `valid` 的翻转点要选 `PERIOD/2-1` 和 `PERIOD-1`，而不是别的位置？

**答案**：为了让 `valid` 的高电平窗口（后半 `PERIOD/2` 拍）与 DUT 内部 `S` 控制信号的上下支切换节拍严格同相——`fft_8` 的 `S8` 恰好也在 `PERIOD/2-1` 与 `PERIOD-1` 翻转。只有 `data_gen.PERIOD == DUT.PERIOD` 时这种同相才成立，这正是 `layer` 取值的硬约束。

**练习 3**：[src/data_gen.v:114](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/data_gen.v#L114) 当前输出原始整数而非 Q16 定点数，这对验证意味着什么？

**答案**：意味着当前 testbench 主要是**功能/时序级冒烟测试**（看握手和流水节拍对不对），喂入幅度极小（0~`PERIOD-1`）；若要与 MATLAB 的 Q16 定点黄金参考逐数值比对，需要恢复 `data_real_tmp << 16` 那一行，否则数量级不一致。

---

### 4.3 激励与被测模块的握手对接：layer 取值约定（本讲主实践）

#### 4.3.1 概念说明

光有 `data_gen` 还不够，还要把它的五路输出**正确地连到 DUT 的对应端口**。不同层级的 DUT 端口命名不一样：低层 `fft_8` 用 `start8/end8`、`fft_4` 用 `start4/end4`；高层 `fft_1k` 用 `start/over`；顶层 `fft_top` 只接 `start`。但对接规则是统一的：

- `data_gen.start` → DUT 的启动输入（`start8`/`start4`/`start`）；
- `data_gen.over` → DUT 的结束输入（`end8`/`end4`/`over`）；
- `data_gen.data_real/data_img` → DUT 的数据输入；
- `data_gen.valid` → 标记有效数据（多数 testbench 把它接到 `valid` 线上观察，DUT 不一定直接用它）。

而**最易出错的一处**，是 `data_gen` 的 `layer` 参数取值。约定是：

> **`data_gen` 的 `layer` 必须取成使 `data_gen.PERIOD == DUT.PERIOD`，即 `layer = log2(DUT.PERIOD) = DUT 的层级数。`**

#### 4.3.2 核心流程

把项目里几个 testbench 的 `layer` 取值排在一起，约定就一目了然：

| testbench | DUT | DUT.PERIOD | `data_gen.layer` | `data_gen.PERIOD` | 是否一致 |
| --- | --- | --- | --- | --- | --- |
| `data_gen_tb` | `data_gen` 自身 | — | 4 | 16 | 自检 |
| `fft_8_tb` | `fft_8` | 8 | 3 | 8 | ✅ |
| `fft_16_tb` | `fft_16` | 16 | 4 | 16 | ✅ |
| `fft_general_tb` | `fft_1k` | 1024 | 10 | 1024 | ✅ |
| `fft_top_tb` | `fft_top` | 16384 | 14 | 16384 | ✅ |
| `fft_4_tb` | `fft_4` | 4 | **3**（现仓库） | 8 | ⚠️ 待确认 |

前五行都严格满足 `data_gen.PERIOD == DUT.PERIOD`；唯独已提交的 `fft_4_tb` 写成了 `.layer(3)`，与约定不符（按约定应是 `layer=2`）。这一处是本讲动手实践要纠正/验证的对象。

#### 4.3.3 源码精读

`fft_8_tb` 的对接（正确范例）：

[tb/fft_8_tb.v:39-48](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/tb/fft_8_tb.v#L39-L48) —— `layer=3`，`start→w_start8`、`over→w_ending8`，再由 [tb/fft_8_tb.v:28-31](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/tb/fft_8_tb.v#L28-L31) 把 `w_start8/w_ending8` 接到 `fft_8` 的 `start8/end8`。通路完整。

`fft_4_tb` 的对接（待修正范例）：

[tb/fft_4_tb.v:23-43](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/tb/fft_4_tb.v#L23-L43) —— `fft_4` 的端口为 `start4/end4/A_real/A_img/out_real4/out_img4/start2/end2`（见 [src/fft_4.v:2-13](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_4.v#L2-L13)），`data_gen` 把 `start→w_start`、`over→w_ending` 接到 `start4/end4`。但 `data_gen #(.layer(3))` 使 `data_gen.PERIOD=8`，与 `fft_4` 的 `PERIOD=4`（[src/fft_4.v:16](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_4.v#L16)）不一致；并且例化里**漏接了** `fft_4` 的 `start2/end2` 输出端口（虽不影响编译，但不便观察）。

> 说明：本讲不会修改任何源码（包括 `tb/fft_4_tb.v`）。下面的「仿写」是在你自己的练习文件里另写一份正确版本，原文件保持不动。`layer=3` 是否仍能让 `fft_4_tb`「勉强跑通」属于待本地验证事项——因为 `fft_4` 只看 `start4` 脉冲触发状态机、靠 `end4` 结束，多余的节拍不一定致命，但时序观察窗口会错位。

#### 4.3.4 代码实践（本讲主实践：仿写一个 fft_4_tb）

**实践目标**：基于 `fft_8_tb` 的结构，仿写一个 `fft_4_tb`，**正确**设置 `data_gen` 的 `layer` 参数并补全 `fft_4` 的端口连接，最后说明如何在波形里观察 `out_real4/out_img4`。

**操作步骤**：

1. 新建练习文件 `fft_4_tb_mine.v`（自己的练习目录，不要覆盖仓库原文件）。
2. 抄 `fft_8_tb` 的三段骨架：信号声明 → 时钟/复位 `initial`+`always` → 例化。
3. 按 `fft_4` 的端口表连接：`start4←start`、`end4←over`、`A_real/A_img←data_real/data_img`、`out_real4/out_img4` 接观察线、并把 `start2/end2` 也接出来观察。
4. **把 `data_gen` 的 `layer` 设为 2**（使 `data_gen.PERIOD=4 == fft_4.PERIOD`），而不是仓库里的 3。

参考实现（**示例代码·练习参考**，非仓库原有文件）：

```verilog
`timescale 1ns / 1ps
module fft_4_tb_mine();
    reg             clk, rst;
    wire            valid;
    wire [32-1:0]   data_real, data_img;
    wire [32-1:0]   out_real4, out_img4;
    wire            w_start, w_ending;   // 接 fft_4 的 start4 / end4
    wire            w_start2, w_end2;     // 接 fft_4 的 start2 / end2（输出，用于观察）

    parameter period = 10;
    initial begin
        clk = 1;  rst = 1;
        #30 rst = 0;                       // 复位 30 ns 后释放
    end
    always #(period/2) clk = ~clk;         // 100 MHz 时钟

    fft_4 fft_4(
        .clk        ( clk        ),
        .rst        ( rst        ),
        .start4     ( w_start    ),        // ← data_gen.start
        .end4       ( w_ending   ),        // ← data_gen.over
        .A_real     ( data_real  ),
        .A_img      ( data_img   ),
        .out_real4  ( out_real4  ),
        .out_img4   ( out_img4   ),
        .start2     ( w_start2   ),        // 补接：输出给下一级的启动
        .end2       ( w_end2     )         // 补接：输出给下一级的结束
    );

    data_gen #(.layer(2))                   // ★ 关键：PERIOD=4 == fft_4.PERIOD
    data_gen4(
        .clk        ( clk        ),
        .rst        ( rst        ),
        .data_real  ( data_real  ),
        .data_img   ( data_img   ),
        .valid      ( valid      ),
        .start      ( w_start    ),
        .over       ( w_ending   )
    );
endmodule
```

**需要观察的现象 / 预期结果**：

1. 用仿真器（Vivado Simulator / ModelSim / Icarus + GTKWave）跑该 testbench，把 `clk/rst/valid/data_real/w_start/w_ending/out_real4/out_img4` 加入波形窗口。
2. 复位释放（30 ns）后，`w_start` 应在 `counter==PERIOD-2 == 2` 处出现一个单拍高脉冲，`fft_4` 的状态机进入 `STATE_START`。
3. 由于 `fft_4` 是 4 点 FFT、反馈延时 2 拍、乘法器又是 3 级流水，`out_real4/out_img4` 在若干拍延时后开始输出有效结果；对照 `data_real=0,1,2,3,0,1,...`（实部、虚部恒 0）的 4 点 DFT 手算值核对数量级。
4. `out_real4/out_img4` 的观察要点：先看它们**何时离开 0**（确定流水延时拍数），再看一个 PERIOD（4 拍）内的 4 个输出值是否构成一组完整的 4 点频谱（注意本项目输出是 **bit-reverse 倒序**，未做最终重排，见 u1-l4）。

**待本地验证**：本项目无 Makefile/CI，也未指定仿真器与运行脚本（README 的 `## tb` 段为空），具体运行命令与波形需本地搭建后验证；上面的时序描述基于源码静态分析，仿真中请以实际波形为准。

#### 4.3.5 小练习与答案

**练习 1**：如果仿写 `fft_4_tb` 时把 `data_gen.layer` 误填成 4（而不是 2），`data_gen.PERIOD` 会是多少？会出现什么错位？

**答案**：`data_gen.PERIOD = 1<<4 = 16`，与 `fft_4.PERIOD=4` 不一致。结果是 `valid` 的高电平窗口变成 8 拍（而非 2 拍），`start` 脉冲出现的位置也偏移到 `counter==14`，与 `fft_4` 内部 `S4` 的翻转节拍（`PERIOD/2-1=1`、`PERIOD-1=3`）错相，激励和蝶形上下支不再对齐，观察到的输出时序会乱。

**练习 2**：`fft_4` 的输出端口 `start2/end2` 在单独测 `fft_4` 时该不该悬空？

**答案**：可以悬空（Verilog 允许输出端口不连，编译不出错），但悬空后在波形里就观察不到「`fft_4` 何时打算启动下一级」这一信息。本实践的参考实现选择把它们接成 `wire` 以便观察，便于理解级间握手（承接 u3-l3）。

---

### 4.4 分级验证策略：从 data_gen_tb 到 fft_top_tb

#### 4.4.1 概念说明

一个流水线项目最忌「一上来就跑全链路、错了不知道是哪一级坏」。本项目的 testbench 采用了清晰的**由小到大、逐级隔离**策略：先验证激励源自身，再验证单级低层、单级高层，最后才验证整条流水线。每一级都能在前一级「已知正确」的基础上隔离出新问题。

#### 4.4.2 核心流程

分级验证的层次与目的：

1. **激励自检（`data_gen_tb`）**：单独跑 `data_gen`，只接 `clk/rst/data_real/data_img/valid`，确认 `valid` 的占空比、`data_real` 的锯齿波符合预期。先把「信号源」本身验对。
2. **单级低层（`fft_2/4/8/16_tb`）**：验证手写低层模块的状态机、`S` 控制、寄存器/`RAM` 延时、旋转因子、复数乘法这条局部通路。`fft_8_tb` 是其中的样板。
3. **单级高层（`fft_general_tb`）**：验证参数化的 `butterfly_general` 通路（`fft_1k` 为代表）。因为 `fft_32~fft_16k` 结构同构（见 u4-l4），验好 `fft_1k` 就大致覆盖了所有高层。
4. **全链路（`fft_top_tb`）**：验证 14 级级联、级间握手 `start_next→start`，并在输出端计算频谱幅度 `fft_abs`，最终对整条流水线把关。

#### 4.4.3 源码精读

激励自检：

[tb/data_gen_tb.v:23-30](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/tb/data_gen_tb.v#L23-L30) —— 用 `layer=4` 单独例化 `data_gen`，只观察 `data_real/data_img/valid`，连 `start/over` 都不接，纯粹看信号源波形对不对。

单级高层：

[tb/fft_general_tb.v:34-57](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/tb/fft_general_tb.v#L34-L57) —— 例化 `fft_1k`（`current_layer=10`），`data_gen #(.layer(10))`，对接 `start/over/data_in_real/data_in_img/data_out_real/data_out_img/start_next/end_next`。`layer=10` 使 `data_gen.PERIOD=1024 == fft_1k.PERIOD`。`fft_1k` 即所有高层模块（`fft_32`…`fft_16k`）的同构代表，换 `layer` 与 ROM 实例名即可迁移到其他高层。

全链路与频谱幅度：

[tb/fft_top_tb.v:31-42](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/tb/fft_top_tb.v#L31-L42) —— 例化 `fft_top`，`data_config` 接 `reg=1`（注意 `data_config` 在 `fft_top` 内部声明却未实际使用，见 u1-l4），`over` 输入端口**未被连接**（与「over/end 链未贯通」一致）。

[tb/fft_top_tb.v:54-67](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/tb/fft_top_tb.v#L54-L67) —— `data_gen #(.layer(14))`（`PERIOD=16384 == fft_top` 总点数），并在 `always @(posedge clk)` 里计算 `fft_abs = $signed(out_real)² + $signed(out_img)²`，即输出复数幅度的平方（功率）。在波形里看 `fft_abs` 就等于在「看频谱的能量」，这是全链路验证最直观的观察量。

#### 4.4.4 代码实践

**实践目标**：在不改动 `fft_top_tb` 的前提下，规划「如何用波形定位某一级行为」。

**操作步骤**：

1. 打开 [tb/fft_top_tb.v:65-67](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/tb/fft_top_tb.v#L65-L67)，理解 `fft_abs` 是复数输出幅度的平方。
2. 由于 `data_real` 是 0~16383 的锯齿波（`layer=14` → `PERIOD=16384`），它近似一个周期斜坡，其频谱能量应集中在某些离散频点上。
3. 在波形里把 `fft_top` 内部各级 `fft_N` 的中间信号（如 `w_D_real`、`w_rotator_valid`）也拉出来（需要打开 `fft_top` 例化层次），对照 `fft_abs` 出现峰值的时刻。

**需要观察的现象 / 预期结果**：`fft_abs` 在复位释放并经过整条流水线填满（约一个总点数的延时）后开始出现非零值；由于输出未做 bit-reverse 倒序，频谱峰值的**位置顺序是打乱的**，但能量数值仍可对应。

**待本地验证**：锯齿波输入下的精确频谱形状、流水线填满延时拍数，需在仿真中实测。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `fft_general_tb` 只测 `fft_1k` 一个高层模块，就能代表 `fft_32~fft_16k`？

**答案**：因为从 `fft_32` 起的所有高层模块结构同构——都是 `butterfly_general + Rotator_address + 两块 ROM + multiplier` 的组合，差别仅在 `layer` 参数与 ROM 实例名（见 u4-l3、u4-l4）。验好其中一个，就验证了这套同构模板的正确性。

**练习 2**：`fft_top_tb` 里 `fft_top` 的 `over` 输入端口没有连任何信号，这会带来什么影响？

**答案**：`over` 悬空（默认为 0），意味着 `fft_top` 永远收不到「帧结束」信号；这与 `fft_top` 内部 `over/end` 链本来就未贯通的现状一致（见 u1-l4、u4-l4）。对功能验证的影响是：流水线靠 `start_next→start` 启动链持续运转，但帧尾不会被显式标记，需结合 `out_last`/数据流自行判断帧边界。

---

## 5. 综合实践

把本讲三件事（读懂 testbench 骨架、读懂 `data_gen`、按约定对接）串成一个端到端小任务。

**任务**：搭建一个「`fft_8` 单级仿真 + 黄金参考比对」的最小验证流程。

1. **搭仿真**：以 [tb/fft_8_tb.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/tb/fft_8_tb.v) 为基础，在自己的练习目录里加上 `$dumpfile`/`$dumpvars`（Icarus+GTKWave）或在 Vivado 里直接设为顶层跑仿真，得到 `out_real8/out_img8` 波形。
2. **导数据**：在 testbench 里加一段 `always @(posedge clk) $display(...)` 或 `$fwrite`，把 `out_real8/out_img8` 在 `valid` 有效期间的取值按拍导成文本。
3. **造参考**：回到 u5-l1 的 [matlab/FFT_iterative_DIF.m](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/matlab/FFT_iterative_DIF.m)，导出对应 `fft_8` 这一级的 `X_FFT_middle_result` 中间向量，作为黄金参考。
4. **对齐与比对**：注意两点坑——(a) `data_gen` 当前喂的是**原始整数**而非 Q16 定点（[src/data_gen.v:114](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/data_gen.v#L114)），数量级与 MATLAB 默认不一致；(b) 硬件输出是 **bit-reverse 倒序**且逐拍时间排列受 SDF 反馈延时影响，不能与 MATLAB 行向量直接逐拍一一对应（见 u5-l1 提醒），需先确认下标映射再比对取值集合。

**预期结果**：在数量级与下标都对齐后，硬件输出与 MATLAB 黄金参考的取值集合应当一致（允许定点截断误差）；若不一致，则可定位是 `fft_8` 这一级算错，而非其他级。

**待本地验证**：本任务涉及实际运行仿真器与 MATLAB，且项目未提供现成运行脚本，完整比对结果需本地搭建后确认。

## 6. 本讲小结

- 一个标准 testbench 由「时钟/复位初始化 + DUT 例化 + 激励例化」三段组成，`fft_8_tb` 是最干净的 50 行样板。
- `data_gen` 是项目唯一的参数化激励源，靠一个 `layer` 同时派生 `valid/start/over/data_real`，四个信号节拍全部由 `PERIOD=2^layer` 统一。
- `data_gen` 的 `valid` 翻转点（`PERIOD/2-1`、`PERIOD-1`）与 `fft_8` 内部 `S` 控制信号的翻转点**完全相同**，因此激励与蝶形上下支天然同相——前提是 `data_gen.PERIOD == DUT.PERIOD`。
- 对接约定：`data_gen.start→DUT.start*`、`data_gen.over→DUT.end*/over`；`layer` 必须取 `log2(DUT.PERIOD)`。已提交的 `fft_4_tb` 写成 `layer=3`，与约定（应为 2）不一致，是实践环节的典型案例。
- 项目采用分级验证：`data_gen_tb`（激励自检）→ 单级低层 `fft_8_tb` → 单级高层 `fft_general_tb`（代表 `fft_32~fft_16k`）→ 全链路 `fft_top_tb`（计算 `fft_abs` 频谱幅度）。
- 仓库无 Makefile/CI、未指定仿真器，README 的 `## tb` 段为空，所有运行命令与波形需本地搭建；且部分 `_tb` 文件混在 `src/` 目录里。

## 7. 下一步学习建议

- **横向（验证纵深）**：接下来读 u5-l3《Xilinx / Anlogic 双平台移植与 IP 依赖》——因为仿真要跑通，必须先准备好 `mult2`、`Delay`、`rotator_*_real/img`、`blk_mem_gen_0` 等厂商 IP，了解 IP 依赖才能让 testbench 真正编译通过。
- **纵向（架构反思）**：读 u5-l4《架构反思与扩展》，从全局角度回看 testbench 暴露的设计问题（如 `over/end` 链未贯通、输出未倒序、`data_config` 未启用），并思考改进方向。
- **动手延伸**：尝试把综合实践里的「`fft_8` 仿真 + MATLAB 黄金参考比对」真正跑起来；若能跑通，再尝试给 `fft_top_tb` 加一个 `$fwrite` 把 `fft_abs` 导出，用 Python/MATLAB 画成频谱图，把「仿真波形」变成「频谱曲线」，你会对这套流水线的输出有直观体感。
