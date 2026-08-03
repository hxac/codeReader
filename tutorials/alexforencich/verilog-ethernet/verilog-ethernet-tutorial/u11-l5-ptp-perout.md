# ptp_perout：周期脉冲输出

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `ptp_perout` 是做什么的：它把一个外部 PTP 时间当作“绝对时间参考”，按用户配置的**起始时刻、周期、脉宽**，在 FPGA 引脚上生成一串精确的周期脉冲。
- 读懂它的核心机制——“当前时间 vs 计划边沿”的比较式调度状态机，以及它如何用借位检测处理秒进位。
- 理解运行时参数加载、`restart` 重对齐、`ffwd`（fast-forward）快进、`input_ts_step` 时间跳变下的 `error`/`locked` 状态语义。
- 掌握 `FNS_ENABLE`（小数纳秒）如何带来亚纳秒级的调度精度，以及它为何能消除“周期非整数倍时钟周期”时的长期相位漂移。
- 能基于仓库自带的 cocotb 仿真平台，把参数改成周期 1 ms、脉宽 1 µs 并验证输出脉冲的间隔与宽度。

## 2. 前置知识

本讲依赖 [u11-l1（ptp_clock）](u11-l1-ptp-clock.md)。在继续前，请确认你已经理解下面几个概念：

- **96 位 ToD 时间戳格式**：verilog-ethernet 的 PTP 子系统统一用 96 位打包时间戳表示“时刻”，字段切分为秒 `[95:48]`、纳秒 `[45:16]`（30 位，足以容纳 0–999 999 999）、小数纳秒 `[15:0]`（16 位定点小数，记作 fns），`[47:46]` 两位恒为 0。这正是 [ptp_clock.v:200-203](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock.v#L200-L203) 输出的格式。
- **小数纳秒（fns）**：把 `{ns, fns}` 当作一个宽定点整数做加减，就能在纳秒之下再细分。默认时钟步长 6.4 ns（156.25 MHz）即 `ns=6, fns=0x6666` 左右。
- **`input_ts_step`**：`ptp_clock` 在时间发生不连续跳变（被瞬时应调整）时拉高的单拍脉冲，下游用它判定“时间参考已失效”。

一句话定位 `ptp_perout`：`ptp_clock` 负责“自由走时间”，`ptp_perout` 负责“对照这个时间，在指定的绝对时刻翻转一根输出线”。二者是“时间源”与“时间消费者”的关系。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [rtl/ptp_perout.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_perout.v) | 本讲主角。纯时序模块，输入 96 位 PTP 时间，输出 `output_pulse` 周期脉冲，并报告 `locked`/`error`。 |
| [rtl/ptp_clock.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock.v) | 时间源。产生 `input_ts_96` 与 `input_ts_step` 喂给 `ptp_perout`。本讲把它当黑盒，只用到它的输出格式。 |
| [tb/ptp_perout/Makefile](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/ptp_perout/Makefile) | cocotb + Icarus Verilog 仿真工程，声明源文件与 `PARAM_` 参数。 |
| [tb/ptp_perout/test_ptp_perout.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/ptp_perout/test_ptp_perout.py) | cocotb 测试，用 `cocotbext-eth` 的 `PtpClock` 驱动 96 位时间，给 DUT 注入 start/period/width。 |

注意：`tb/test_ptp_perout.py`（在 `tb/` 根目录）是 myhdl 时代的历史遗留，当前流程不再编译，被 `tox.ini` 用 `--ignore-glob` 排除。真正在用的是 `tb/ptp_perout/` 目录里的三件套（本例缺 `test_*.v`，因为 cocotb 直接例化顶层 `ptp_perout`，无需 Verilog 包装）。

## 4. 核心概念与源码讲解

本讲按三个最小模块拆分：**4.1 基于时间的脉冲生成**（核心比较调度）、**4.2 起始/周期/宽度配置**（参数与运行时加载、秒进位、快进重对齐）、**4.3 亚纳秒精度与状态信号**（FNS_ENABLE、locked/error）。

### 4.1 基于时间的脉冲生成

#### 4.1.1 概念说明

很多场合需要 FPGA 输出一串“在绝对时间轴上对齐”的脉冲：给传感器送采样节拍、给电机控制器送同步信号、产生 1PPS（每秒一拍）等等。最朴素的想法是用一个计数器数时钟周期——但这有两个问题：第一，时钟周期未必能整除想要的周期（比如 6.4 ns 时钟做不出精确的 100 ns），长期累积会漂移；第二，计数器不知道“现在几点”，断电再来无法对齐到整秒/整毫秒边界。

`ptp_perout` 换了个思路：**不数周期，而是“看表”**。它接收一个持续走动的 PTP 绝对时间 `input_ts_96`，自己维护一张“计划表”——下一个上升沿该在什么时刻、下一个下降沿该在什么时刻；每个时钟周期把“当前时间”和“下一个计划边沿”比一次，一旦当前时间越过计划边沿，就翻转输出线。这样：

- 脉冲的**绝对时刻**由 PTP 时间决定，天然对齐到整秒/整毫秒；
- 周期/脉宽可以是非整数倍时钟周期，靠 fns 累积消除长期漂移；
- 计划表在时间跳变后能自动重对齐。

它的输出 `output_pulse` 就是这根被周期翻转的线：每个周期内先高 `width` 时间、再低直到下一个周期边界，形成一串占空比可调的脉冲。

#### 4.1.2 核心流程

模块是一个三状态机（`STATE_IDLE` / `STATE_UPDATE_RISE` / `STATE_UPDATE_FALL`），核心循环是“等待边沿 → 翻转 → 算下一个边沿”：

```text
每个时钟上升沿：
  1. 把 input_ts_96 锁存进 time_{s,ns,fns}_reg（得到“当前时间”）
  2. 在 IDLE 中比较：
        当前时间 > next_edge（下一个计划边沿）？
     ┌─ 否：继续等（IDLE）
     └─ 是：根据当前电平决定这是“上升沿”还是“下降沿”
           · 若处于高电平（level=1）或快进（ffwd=1）→ 下降沿：
                output=0, level=0 → UPDATE_RISE（算下一上升沿 = 上次上升沿 + period）
           · 否则 → 上升沿：
                output=enable, level=1, locked=1 → UPDATE_FALL（算下一下降沿 = 上次上升沿 + width）
  3. UPDATE_RISE / UPDATE_FALL：把 next_edge 更新为刚算出的新边沿，回 IDLE
```

注意两个关键设计：

- **严格大于 `>`**：边沿在“当前时间刚刚越过计划边沿”的那一拍触发，因此实际翻转会有最多一个时钟周期（6.4 ns）的量化抖动——这是“看表法”的固有代价，4.3 节会讲 fns 如何把**平均**周期做准。
- **上升沿是基准**：下降沿 = 上次上升沿 + `width`，下一上升沿 = 上次上升沿 + `period`。所有计划时刻都锚定在“上一次上升沿”上，因此即便某次边沿晚了一拍，误差不会向未来传播——下一个计划时刻仍从干净的 `next_rise` 起算。

#### 4.1.3 源码精读

模块端口与参数见 [rtl/ptp_perout.v:34-78](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_perout.v#L34-L78)：输入 `input_ts_96`、`input_ts_step`、`enable` 与三组运行时配置（start/period/width 各带 `_valid`），输出 `locked`、`error`、`output_pulse`。

核心比较在 `STATE_IDLE` 中（[rtl/ptp_perout.v:150-183](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_perout.v#L150-L183)）。先看“是否越过计划边沿”的判定：

```verilog
// 当前时间（秒, ns, fns 三段）是否已严格越过 next_edge
if ((time_s_reg > next_edge_s_reg) ||
    (time_s_reg == next_edge_s_reg &&
     {time_ns_reg, time_fns_reg} > {next_edge_ns_reg, next_edge_fns_reg})) begin
```

这是一次跨 96 位三字段（秒、{ns,fns}）的字典序比较：先比秒，秒相等再比 46 位的 `{ns,fns}` 拼接整数。一旦成立，按 `ffwd_reg || level_reg` 区分这是下降沿（该回落了）还是上升沿（该拉高了）：

```verilog
if (ffwd_reg || level_reg) begin
    // 下降沿：拉低，准备算下一个上升沿
    output_next = 1'b0;
    level_next  = 1'b0;
    state_next  = STATE_UPDATE_RISE;
end else begin
    // 上升沿：拉高（受 enable 控制），锁定，准备算下一个下降沿
    locked_next = 1'b1;
    error_next  = 1'b0;
    output_next = enable;
    level_next  = 1'b1;
    state_next  = STATE_UPDATE_FALL;
end
```

可以看到 `output_next = enable`：`enable` 只是个输出掩膜——即使关掉，内部计划表（`level`、`next_edge`）仍在跑，`locked` 仍会置位；只是引脚上不出脉冲。这在“临时静音但保持相位同步”时很有用。

当前时间在每个时钟沿从 `input_ts_96` 锁存（[rtl/ptp_perout.v:242-246](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_perout.v#L242-L246)），字段切分与 `ptp_clock` 的 96 位格式完全对齐：

```verilog
time_s_reg  <= input_ts_96[95:48];   // 秒
time_ns_reg <= input_ts_96[45:16];   // 纳秒（30 位）
if (FNS_ENABLE) time_fns_reg <= input_ts_96[15:0];  // 小数纳秒
```

#### 4.1.4 代码实践（源码阅读型）

**目标**：在不跑仿真的前提下，靠读源码确认“下降沿计划时刻 = 上次上升沿 + width”。

**步骤**：

1. 打开 [rtl/ptp_perout.v:150-214](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_perout.v#L150-L214)。
2. 在 `STATE_IDLE` 的 `else`（上升沿）分支里，找到为下一下降沿预计算的加法：`{ts_96_ns_inc_next, ts_96_fns_inc_next} = {next_rise_ns_reg, next_rise_fns_reg} + {width_ns_reg, width_fns_reg}`（[L160](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_perout.v#L160)）。注意加的是 `next_rise_*`（上次上升沿）而不是 `next_edge_*`。
3. 进入 `STATE_UPDATE_FALL`（[L201-214](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_perout.v#L201-L214)），确认它把 `next_edge` 更新为 `next_rise + width`（含秒进位），而 `next_rise` 保持不变。

**预期结论**：下降沿时刻确实锚定在“上次上升沿 + width”，与上升沿是否因量化晚触发无关——这正是相位误差不传播的根源。

#### 4.1.5 小练习与答案

**练习 1**：如果把比较里的 `>` 改成 `>=`，输出脉冲会怎样变化？

> **答案**：边沿会在“当前时间等于计划边沿”的那一拍就触发，整体提前最多一拍（6.4 ns）。但因为 `next_edge` 仍从 `next_rise` 起算，长期平均周期不变，只是相位整体前移、且更易在“时间恰等于计划”时多翻一次，引入毛刺风险。仓库刻意用 `>` 以保证“越过才翻转”的单调性。

**练习 2**：`output_pulse` 的翻转抖动上限是多少？为什么？

> **答案**：上限为一个时钟周期（默认 6.4 ns）。因为时间只在每个时钟沿更新一次，边沿检测也只能在时钟沿发生——“当前时间越过计划边沿”这一事件最早只能在跨越后的第一个时钟沿被察觉。

---

### 4.2 起始/周期/宽度配置

#### 4.2.1 概念说明

`ptp_perout` 有三组配置：**start**（第一个上升沿的绝对时刻）、**period**（相邻上升沿间隔）、**width**（高电平持续时间）。每组都既能在**编译期**用参数给默认值，也能在**运行时**通过 `input_*_valid` 端口动态加载。默认值是一个“每秒一拍、每拍 1 µs 宽”的类 1PPS 输出（`OUT_PERIOD_S=1`、`OUT_WIDTH_NS=1000`）。

配置变更的语义并不对称，这是本模块最易踩坑的地方：

- 改 **start** 或 **period**：会触发 `restart`——把计划表整个推倒重排，回到 start 时刻重新等，`locked` 被清零，直到下一次正常上升沿才重新置位。
- 改 **width**：**不触发** `restart`。因为宽度只影响“未来下降沿 = 上次上升沿 + width”，下一拍自然就用新宽度，无需重对齐。

还有一个关键难点：纳秒字段相加可能跨过 10⁹ ns（进位到秒），但秒、ns 是分开设的，需要一个不引入额外时钟周期的组合进位检测——模块用的是和 `ptp_clock` 同源的“借位预扣 + 看符号位”技巧。

#### 4.2.2 核心流程

**运行时加载**（[rtl/ptp_perout.v:248-272](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_perout.v#L248-L272)）：

```text
input_start_valid  → 锁存 start_{s,ns,fns}，置 restart=1
input_period_valid → 锁存 period_{s,ns,fns}，置 restart=1
input_width_valid  → 锁存 width_{s,ns,fns}，不置 restart
```

**秒进位检测**（组合，零周期）。以“下一上升沿 = next_rise + period”为例，IDLE 里同时算两个版本（[L155-156](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_perout.v#L155-L156)）：

```text
inc = {next_rise_ns, next_rise_fns} + {period_ns, period_fns}            # 原始和
ovf = inc - {1_000_000_000, 0}                                            # 预扣一秒
```

随后在 `STATE_UPDATE_RISE`（[L184-200](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_perout.v#L184-L200)）用 `ovf` 的第 30 位判定是否真的跨秒：

```text
若 !ovf.ns[30]（bit30=0）→ 说明 ovf 是个 < 2^30 的小余数 → 跨了一秒
    next_edge.s  = next_rise.s + period.s + 1
    next_edge.ns = ovf.ns（扣完一秒后的纳秒余数）
否则（bit30=1）→ ovf 是个负数回绕成的大值 → 没跨秒
    next_edge.s  = next_rise.s + period.s
    next_edge.ns = inc.ns（原始和）
```

**为什么看 bit30 成立？** 关键在于 \(10^9 < 2^{30} < 2\times10^9\)，即 \(2^{30} = 1\,073\,741\,824\)。设 `sum = next_rise.ns + period.ns`，它最大约 \(2\times10^9 < 2^{31}\)，装得进 31 位。

- 若 `sum ≥ 10⁹`（跨秒）：\(0 \le \text{ovf} = sum - 10^9 < 10^9 < 2^{30}\)，bit30 = 0；
- 若 `sum < 10⁹`（没跨秒）：\(\text{ovf} = sum - 10^9 < 0\)，按 31 位无符号回绕成 \(2^{31} + (sum-10^9)\)，结果落在 \([2^{30},\, 2^{31})\)，bit30 = 1。

所以“bit30 是否为 0”精确等价于“纳秒和是否越过 10⁹ 边界”。这正是 [ptp_clock.v:264-268](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock.v#L264-L268) 用的同一套手法——`ptp_perout` 直接复用了时间源的进位逻辑。

**重对齐与快进**（`restart` / `ffwd`）。`restart` 把 `next_rise` 和 `next_edge` 都重置为 `start`、清 `locked`、置 `ffwd=1`（[L217-235](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_perout.v#L217-L235)）。`ffwd` 解决一个现实问题：如果配置时 `start` 已经过去（例如 `start=0` 而当前已是第 100 ns），不应输出一个迟到的旧脉冲，而应跳过整数个 `period` 直到计划时刻来到未来。`ffwd=1` 期间，IDLE 始终走“下降沿分支”——不输出、不置 `locked`，只反复执行 `next_rise += period` 把计划表往后推；直到某个 `next_edge` 终于落在未来（当前时间 ≤ next_edge），`ffwd` 被清零，下一个越过事件就是真正的上升沿。

#### 4.2.3 源码精读

重启块在组合 `always @*` 的最末尾，**位于 `case(state_reg)` 之后**，因此它覆盖状态机的任何输出（[rtl/ptp_perout.v:217-235](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_perout.v#L217-L235)）：

```verilog
if (restart_reg || input_ts_step) begin
    next_rise_s_next  = start_s_reg;        // 计划表推倒，回到 start
    next_edge_s_next  = start_s_reg;
    ...                                     // ns/fns 同理（受 FNS_ENABLE 门控）
    locked_next = 1'b0;                      // 失锁
    ffwd_next   = 1'b1;                      // 进入快进
    output_next = 1'b0;
    level_next  = 1'b0;
    error_next  = input_ts_step;             // 仅时间跳变才报错
    state_next  = STATE_IDLE;
end
```

两个触发源的语义差异就在 `error_next = input_ts_step` 这一行：

- `restart_reg`（复位或加载 start/period）：`input_ts_step=0`，故 `error=0`，只是静默重排；
- `input_ts_step`（PTP 时间源跳变）：`error=1`，表示“时间参考刚断裂过，输出相位可能不可信”，直到下一次正常上升沿才由 `error_next = 1'b0` 清除。

参数加载与 `restart` 的联动在时序块里（[rtl/ptp_perout.v:248-264](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_perout.v#L248-L264)）：`input_start_valid`、`input_period_valid` 都会 `restart_reg <= 1'b1`，而 `input_width_valid` 不会——这印证了 4.2.1 里“改宽度不重排”的设计。

#### 4.2.4 代码实践

**目标**：跑仓库自带的 cocotb 仿真，观察“start 设在当前时刻之前”如何被 `ffwd` 处理。

**步骤**：

1. 确认已装好 cocotb、cocotbext-eth、iverilog（详见 [u1-l4](u1-l4-testbench-and-simulation.md)）。
2. 进目录跑测试：
   ```bash
   cd tb/ptp_perout
   make
   ```
3. 对照 [test_ptp_perout.py:82-97](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/ptp_perout/test_ptp_perout.py#L82-L97)。test 1 把 `input_start = 100<<16`（ns 字段=100，即 start 在未来 100 ns），test 2 把 `input_start = 0<<16`（start=0，复位后必然在过去）。
4. 把波形打开重跑，重点看 `ffwd` 与 `locked`：
   ```bash
   make clean && make WAVES=1
   # 用 GTKWave 查看 dump.fst：output_pulse, locked, next_edge_*
   ```

**需要观察的现象**：

- test 1：复位后约 100 ns 处出现第一个上升沿，`locked` 随之拉高，之后每 100 ns 一拍、每拍高 50 ns。
- test 2（start=0，在过去）：复位后 `ffwd` 立即为 1，`output_pulse` 保持 0；模块把 `next_edge` 按 100 ns 步进反复推进，直到它来到当前时间之后，`ffwd` 清零，`locked` 才在第一个真正上升沿处拉高——期间不产生任何迟到脉冲。

**预期结果 / 待本地验证**：因为 `period=100 ns` 不是 6.4 ns 的整数倍（100/6.4=15.625），单个脉冲间隔会在 15、16 个时钟周期之间抖动，但跨多拍的平均间隔趋近 100 ns。具体每拍的量化分布「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `input_width_valid` 不触发 `restart`，而 `input_period_valid` 触发？

> **答案**：宽度只参与“下一下降沿 = 上次上升沿 + width”，是相对量，改了下一拍就自然生效，不影响已排好的上升沿序列；周期是相邻上升沿的间隔，改了意味着整张计划表的时间栅格变了，必须从 start 重新排起，所以触发 `restart`。

**练习 2**：设 `next_rise.ns = 600_000_000`、`period.ns = 700_000_000`，`STATE_UPDATE_RISE` 会把秒字段加几？`next_edge.ns` 取哪个值？

> **答案**：`sum = 1_300_000_000 ≥ 10⁹`，跨了一秒。`ovf = 300_000_000`（< 2³⁰，bit30=0），走 `!ovf[30]` 分支：秒 `+1`（再加上 `period.s`），`next_edge.ns = 300_000_000`。

---

### 4.3 亚纳秒精度与状态信号

#### 4.3.1 概念说明

本模块用 `FNS_ENABLE`（默认 1）开启小数纳秒。它的价值不是“让单次翻转精确到亚纳秒”——那受限于时钟周期量化，做不到——而是**让长期平均周期精确**。举例：用 6.4 ns 时钟生成 100 ns 周期，100/6.4=15.625，若每次都数 15 或 16 拍，单次都有 ±6.4 ns 误差；但 `ptp_perout` 把 `period` 的 0.625 拍部分存进 fns，每拍累加，使“计划边沿”在时间轴上以亚纳秒精度前移，于是“当前时间越过计划边沿”的判定跨周期地正确分布，长期平均周期恰好 100 ns，无累积漂移。关掉 `FNS_ENABLE`（=0）时，fns 字段被丢弃，周期/脉宽只能取纳秒整数，资源更省但失去这层精度补偿。

状态信号有三个：`locked`、`error`、`enable`。`locked` 表示“已与计划表同步并在产出脉冲”——它在第一个正常上升沿处置 1，在 `restart` 或时间跳变时清 0。`error` 表示“时间参考刚发生过不连续跳变”，由 `input_ts_step` 触发，并在下一次正常上升沿自动清除。`enable` 是输出掩膜：为 0 时计划表照跑、`locked` 照置位，只是 `output_pulse` 被强制拉低。

#### 4.3.2 核心流程

亚纳秒的“长期无漂移”可这样理解。设周期 \(T\) 不是时钟步长 \(\Delta\)（默认 6.4 ns）的整数倍：

\[
T = k\Delta + r,\qquad 0 < r < \Delta
\]

每次上升沿后，计划表把 `next_rise` 增加 \(T\)，其中整数拍 \(k\Delta\) 进 ns 字段、余数 \(r\) 进 fns 字段累加。第 \(n\) 个计划上升沿的绝对时刻为

\[
t_n = t_0 + nT = t_0 + nk\Delta + nr
\]

而“当前时间越过 \(t_n\)”这一事件发生在第一个满足 `time > t_n` 的时钟沿，即 \(t_n\) 之后的下一个 \(\Delta\) 网格点。把所有上升沿的**实际**触发时刻取平均，它精确等于 \(t_0 + nT\)——fns 的累加保证 \(nr\) 这部分被忠实地分摊到各拍，不会因每次取整而系统性偏移。换言之：**单拍有最多 \(\Delta\)（6.4 ns）抖动，多拍平均无偏**。

状态流转：

```text
复位 / 加载start+period        ──┐
PTP时间跳变(input_ts_step) ────┐│  → locked=0, error=步变标志, ffwd=1
                                 ││
                        等到 next_edge 来到未来（ffwd 清 0）
                                 │
                        当前时间越过 next_edge（正常上升沿）
                                 │
                          → locked=1, error=0, output=enable
```

#### 4.3.3 源码精读

`FNS_ENABLE` 在源码里是一把“编译期开关”，凡涉及 fns 的寄存器与运算都被它门控。例如时间锁存（[rtl/ptp_perout.v:244-246](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_perout.v#L244-L246)）：

```verilog
time_ns_reg <= input_ts_96[45:16];
if (FNS_ENABLE) time_fns_reg <= input_ts_96[15:0];   // 关闭则不锁存 fns
```

重启块里同样（[L221-228](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_perout.v#L221-L228)）：`next_rise_fns_next`、`next_edge_fns_next` 都只在 `FNS_ENABLE` 时赋值；时序块里所有 fns 寄存器的更新（[L276-294](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_perout.v#L276-L294)）也都被 `if (FNS_ENABLE)` 包裹。综合时若 `FNS_ENABLE=0`，这些逻辑被优化为零面积。

`locked` / `error` 的置位与清除逻辑分布在两处：置 1 在上升沿分支（`locked_next = 1'b1; error_next = 1'b0`，[L173-174](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_perout.v#L173-L174)），清 0 在重启块（`locked_next = 1'b0; error_next = input_ts_step`，[L229-233](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_perout.v#L229-L233)）。复位时三者归零（[L302-321](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_perout.v#L302-L321)）。

#### 4.3.4 代码实践

**目标**：把参数改成**周期 1 ms、脉宽 1 µs**，端到端验证 `output_pulse` 的间隔与宽度。

**步骤**：

1. 复制测试目录做隔离（不要改原文件以免影响回归）：
   ```bash
   cp -r tb/ptp_perout tb/ptp_perout_1ms
   ```

2. 编辑 `tb/ptp_perout_1ms/test_ptp_perout.py`，把激励改成下面的**示例代码**（基于 [test_ptp_perout.py:82-97](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/ptp_perout/test_ptp_perout.py#L82-L97) 改写）：

   ```python
   # 示例代码：周期 1 ms，脉宽 1 µs，start 设在未来 2 µs
   PERIOD_NS = 1_000_000     # 1 ms
   WIDTH_NS  = 1000          # 1 µs
   START_NS  = 2000          # 2 µs 后起步，避开快进

   dut.enable.value = 1
   await RisingEdge(dut.clk)
   dut.input_start.value  = START_NS  << 16   # ns 进 [45:16]，fns=0
   dut.input_period.value = PERIOD_NS << 16
   dut.input_width.value  = WIDTH_NS  << 16
   dut.input_start_valid.value  = 1
   dut.input_period_valid.value = 1
   dut.input_width_valid.value  = 1
   await RisingEdge(dut.clk)
   dut.input_start_valid.value  = 0
   dut.input_period_valid.value = 0
   dut.input_width_valid.value  = 0

   # 1 ms 周期：至少要仿真几毫秒才能看到多个脉冲
   await Timer(5_000_000, 'ns')   # 5 ms，约 3 个完整周期
   ```

   说明：`<< 16` 是因为低 16 位是 fns；把整数纳秒左移 16 位即把它放进 ns 字段、fns 留 0。

3. 跑仿真（1 ms 周期意味着要仿几毫秒，比原测试慢，耐心等待）：
   ```bash
   cd tb/ptp_perout_1ms
   make clean && make WAVES=1
   ```

4. 用 GTKWave 打开 `dump.fst`，在 `output_pulse` 上用“上升沿测量”量两件事：
   - **脉冲间隔**：相邻两个上升沿之间的时间；
   - **脉冲宽度**：同一脉冲上升沿到下降沿的时间。

**需要观察的现象 / 预期结果**：

- **脉冲间隔 ≈ 1 ms（1 000 000 ns）**。由于 1 ms 远大于 6.4 ns 量化，单次抖动 ≤ 6.4 ns，相对误差极小。
- **脉冲宽度 ≈ 1 µs（1000 ns）**。1000/6.4 ≈ 156.25，所以高电平持续 156 或 157 个时钟周期（约 998.4 ns 或 1004.8 ns），**单次宽度有 ±6.4 ns 抖动**——这正是 4.3.2 讲的量化效应。
- 复位后约 2 µs（start）出现第一个上升沿，`locked` 同时拉高并保持。
- 若想验证“长期无漂移”，可把周期设成 `100<<16 + (0xA000)`（即 100 ns 加一点 fns 余量）并仿几十个周期，量平均间隔应严格趋近设定值。具体量化序列「待本地验证」。

> 提示：1 ms 周期下仿真较慢。若只想快速验证宽度，可临时把周期改回 1 µs（`PERIOD_NS=1000`），用 `Timer(100_000,'ns')` 即可在 100 µs 内看到约 100 个脉冲。

#### 4.3.5 小练习与答案

**练习 1**：用 6.4 ns 时钟生成 1 µs 脉宽，单次高电平持续多少拍？为何不是定值？

> **答案**：1000/6.4=156.25。因为不是整数，`ptp_perout` 计划的下降沿落在两个时钟网格之间，“当前时间越过计划下降沿”发生在其后的第一个网格点，所以高电平拍数在 156、157 之间交替（对应约 998.4 ns / 1004.8 ns），单次有 ±6.4 ns 抖动，但平均趋近 1000 ns。

**练习 2**：`enable=0` 期间，`locked` 会怎样？给出你的判断并说明依据。

> **答案**：`locked` 仍会置 1。依据是上升沿分支里 `locked_next = 1'b1` 与 `output_next = enable` 是两条独立赋值（[L173-175](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_perout.v#L173-L175)）：内部计划表照常同步，只是输出被掩膜拉低。所以 `locked` 反映“相位已同步”，与 `enable` 是否驱动引脚无关。

## 5. 综合实践

把三个最小模块串起来，做一个“**带相位补偿的亚纳秒周期脉冲观测**”小任务：

1. **接线**：实例化一个 `ptp_clock`（提供 6.4 ns 步长 96 位时间）与一个 `ptp_perout`，把前者的 `output_ts_96`/`output_ts_step` 接到后者的 `input_ts_96`/`input_ts_step`。可参考 [test_ptp_perout.py:47-53](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/ptp_perout/test_ptp_perout.py#L47-L53) 里 `cocotbext-eth` 的 `PtpClock` 用法。
2. **配置一个非整数倍周期**：令周期 = 100 ns + 0.4 ns（即 `input_period = (100<<16) | 0x6666` 大致表示 100 ns 加约 0.4 ns），脉宽 30 ns，start 设在未来 200 ns。
3. **观测三件事并解释**：
   - 单个脉冲间隔在哪些时钟周期数之间抖动？（量化，对应 4.3）
   - 取连续 20 个脉冲的间隔平均，是否趋近 100.4 ns？（fns 累积消漂移，对应 4.3）
   - 仿真中途注入一次 `input_ts_step`（在测试里给 `dut.input_ts_step.value = 1` 一拍），观察 `error` 立刻拉高、`locked` 清零、输出停顿后从 start 重新快进对齐——这把 4.2 的 `restart`/`ffwd`/`error`/`locked` 全部串起来。
4. **记录**：把上述三类现象各写一句话，附波形截图或测量值。

这个任务需要你同时理解“看表式调度（4.1）”“配置与重对齐（4.2）”“亚纳秒与状态（4.3）”，是检验本讲掌握程度的综合练习。

## 6. 本讲小结

- `ptp_perout` 是“时间消费者”：对照外部 96 位 PTP 时间，按 start/period/width 在绝对时间轴上生成周期脉冲，本质是“当前时间 vs 计划边沿”的比较式调度，而非数周期。
- 核心是一个三状态机（IDLE / UPDATE_RISE / UPDATE_FALL），上升沿是基准——下降沿 = 上次上升沿 + width，下一上升沿 = 上次上升沿 + period，故单次量化误差不会向未来传播。
- 纳秒相加的秒进位用“预扣 10⁹ + 看 bit30”的组合借位检测，与 `ptp_clock` 同源，零额外时钟周期。
- 配置语义不对称：改 start/period 触发 `restart` 重排，改 width 不重排；`start` 落在过去时由 `ffwd` 快进整数个周期，避免迟到脉冲。
- `locked`（已同步）、`error`（时间源刚跳变）由 `input_ts_step` 区分；`enable` 只是输出掩膜，不影响内部同步。
- `FNS_ENABLE` 提供亚纳秒调度：单次翻转有 ≤6.4 ns 量化抖动，但 fns 累积使长期平均周期/脉宽无漂移。

## 7. 下一步学习建议

- **回看时间源**：本讲把 `ptp_clock` 当黑盒，建议重读 [u11-l1](u11-l1-ptp-clock.md) 中关于 `input_ts_step`、步长与 fns 的部分，理解“时间跳变”是如何产生的。
- **跨域分发**：若你的 `ptp_perout` 与 `ptp_clock` 不在同一时钟域，应接一个 [u11-l2（ptp_clock_cdc）](u11-l2-ptp-clock-cdc.md) 把时间安全搬到 `ptp_perout` 所在域，再用本模块生成脉冲。
- **多叶分发**：[u11-l4（ptp_td_phc/leaf）](u11-l4-ptp-time-distribution.md) 讲的串行时间分发可在多个时钟域各放一个 `ptp_perout`，实现多路相位对齐的周期脉冲输出。
- **进阶阅读**：直接对照 [rtl/ptp_perout.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_perout.v) 与 [rtl/ptp_clock.v:260-268](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock.v#L260-L268)，体会二者共用同一套 inc/ovf 进位技巧的设计一致性。新项目可关注继任仓库 **taxi** 中对应的 period output 模块，接口与本库高度相似。
