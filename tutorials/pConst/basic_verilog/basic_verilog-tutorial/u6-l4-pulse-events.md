# 脉冲与事件发生：pulse / pulse_stretch / delayed_event / dynamic_delay

## 1. 本讲目标

本讲讲解 basic_verilog 里一组专门做「时间整形」的模块。学完后你应该能够：

- 说清楚 `pulse_gen` 如何用一个向下计数器产生「给定周期 + 给定脉宽」的周期脉冲，以及它如何用 `cntr_low` 在「常高 / 脉冲 / 常低」三种输出之间切换。
- 用 `pulse_stretch` 把一个可能只有 1 拍宽的窄脉冲（按键毛刺、传感器尖峰）可靠地拉宽成 `WIDTH` 拍，并能在「延迟线实现」和「计数器实现」之间做资源取舍。
- 理解 `delayed_event` 如何在复位后经过 `DELAY` 个使能拍产生**唯一一个**单拍事件，并能用 `before_event` / `after_event` 做事件级联。
- 读懂 `dynamic_delay` 用「移位寄存器 + 扁平化选择」实现对任意多位信号的**按位可选延迟」，而不仅仅是按字延迟。
- 把这几个积木和 `edge_detect`、`delay`（前置讲义 u2-l2 / u2-l3）串起来，搭出「按键毛刺 → 拉宽 → 延迟事件」的真实数据通路。

## 2. 前置知识

本讲是 u2 单元（基础时序原语）的延伸，默认你已经具备以下认知（来自前置讲义）：

- **时序逻辑与组合逻辑**：`always_ff @(posedge clk)` 写寄存器、用非阻塞 `<=`；`always_comb` 写组合逻辑、用阻塞 `=`。仓库统一用低有效同步复位 `nrst`（`delayed_event`）或低有效异步复位 `anrst`（`edge_detect`）。
- **`delay` 即移位寄存器**（u2-l3）：把信号整体向后平移 N 拍，满足 \( \text{out}(t)=\text{in}(t-N\cdot T_{clk}) \)。本讲的 `pulse_stretch` 延迟线版、`dynamic_delay` 的核心都是移位寄存器。
- **`edge_detect` 即延迟比较**（u2-l2）：把信号打一拍再和原信号比，得到一个单拍的 `rising` 脉冲。`delayed_event` 内部直接例化了它来产生「单拍事件」。
- **`$clog2` 位宽计算**（u2-l4）：`$clog2(n)` 回答「n 个表项需要几 bit 地址」。本讲里多处用它算计数器位宽。

一个统一的视角：**这四个模块都在做「把一个时间波形变成另一个时间波形」的事**——`pulse_gen` 凭空造脉冲，`pulse_stretch` 把窄变宽，`delayed_event` 把「现在」推迟成「将来某个时刻的一拍」，`dynamic_delay` 把信号搬到任意可选的历史时刻。它们是系统里负责「节拍与时机」的胶水模块。

> 说明：在 README 的模块表里，这四个文件都没有打 :green_circle: 或 :red_circle: 难度标签，属于难度居中的实用工具模块，正好承接 u2 的绿圈基础原语。

## 3. 本讲源码地图

| 文件 | 作用 | 是否可综合 |
|------|------|-----------|
| `pulse_gen.sv` | 周期脉冲发生器，按 `cntr_max`/`cntr_low` 产生给定周期与脉宽的脉冲，可输出常高/脉冲/常低 | 是 |
| `pulse_stretch.sv` | 脉冲拉伸器，把窄脉冲拉宽成 `WIDTH` 拍；延迟线与计数器两种实现可选 | 是 |
| `delayed_event.sv` | 延迟事件发生器，复位后经 `DELAY` 个使能拍产生唯一一个单拍 `on_event` | 是 |
| `dynamic_delay.sv` | 动态延迟，对多位信号按 `sel` 做按位可选延迟 | 是 |
| `delayed_event_tb.sv` | `delayed_event` 的 testbench，演示随机复位、异步时钟与 `DELAY=0..15` 扫描 | 否（testbench） |
| `edge_detect.sv`（u2-l2 已讲） | `delayed_event` 内部例化它来产生单拍事件 | 是 |

## 4. 核心概念与源码讲解

### 4.1 脉冲发生：pulse_gen

#### 4.1.1 概念说明

很多场合需要一颗能「自己持续跳动」的时钟节拍源——驱动状态机轮询、给传感器周期性使能、产生 PWM/PDM 的载波（见 u6-l3）。`pulse_gen` 就是这样一个**周期脉冲发生器**：你告诉它周期有多长（`cntr_max`）、脉冲高电平占多少（`cntr_low`），它就持续输出对应方波，并且还能在「常高」「脉冲」「常低」三种状态间无缝切换。

它的设计哲学是「输入可随时变、输出不被打扰」：`cntr_low` 在每个周期开始时被缓存进 `cntr_low_buf`，整个周期内即使输入端 `cntr_low` 乱跳，本周期波形也不受影响。

#### 4.1.2 核心流程

`pulse_gen` 的核心是一个**自由向下计数器** `seq_cntr`，配合一个判别「当前是否处于脉冲周期内」的 `busy` 信号：

```text
每个 clk 上升沿：
  若 seq_cntr == 0（计数到底）：
      若 start 且 cntr_max != 0：   // 合法启动
          seq_cntr  <= cntr_max     // 重装，开始新周期
          cntr_low_buf <= cntr_low  // 缓存本周期用的脉宽参数
          start_strobe <= 1         // 周期起始标志，单拍
      否则：
          保持空闲（seq_cntr 停在 0），start_strobe <= 0
  否则：
      seq_cntr <= seq_cntr - 1      // 继续倒数

组合输出：
  busy     = ~(seq_cntr==0 且 上一拍也==0)   // 区分「周期末的 0」与「空闲的 0」
  pulse_out = busy 且 (seq_cntr >= cntr_low_buf) ? 1 : 0
```

关键数量关系（设 `0 < cntr_low <= cntr_max`）：

- 周期：\( T = \text{cntr\_max} + 1 \) 拍（计数器走过 cntr_max, cntr_max−1, …, 1, 0 共 cntr_max+1 个值）。
- 高电平宽度：\( W_{\text{high}} = \text{cntr\_max} - \text{cntr\_low} + 1 \) 拍（seq_cntr 从 cntr_max 倒数到 cntr_low_buf 期间）。
- 占空比：\( D = \dfrac{W_{\text{high}}}{T} = \dfrac{\text{cntr\_max} - \text{cntr\_low} + 1}{\text{cntr\_max} + 1} \)。

三个特例（来自模块 INFO 的 Example 1，可直接验证）：

| `cntr_low` 取值 | 输出 | 原因 |
|---|---|---|
| `== 0` | 常高（constant HIGH） | `seq_cntr >= 0` 恒成立 |
| `0 < cntr_low <= cntr_max` | 周期脉冲 | 高/低各占一段 |
| `> cntr_max` | 常低（constant LOW） | `seq_cntr` 永远到不了那么大 |

#### 4.1.3 源码精读

**端口与参数**：只有一个参数 `CNTR_WIDTH`（默认 32），决定计数器与 `cntr_max`/`cntr_low` 的位宽。

[pulse_gen.sv:58-73](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pulse_gen.sv#L58-L73) — 模块声明。`start` 是「允许开启新周期」的开关，`pulse_out` 是高有效输出，`busy`/`start_strobe` 是状态。

**计数到底检测与 busy 判别**：这是全模块最巧妙的地方。

[pulse_gen.sv:76-93](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pulse_gen.sv#L76-L93) — `seq_cntr_0` 检测当前是否为 0；`seq_cntr_0_d1` 是它打一拍后的版本。`busy = ~(seq_cntr_0 && seq_cntr_0_d1)` 的含义是：**只有当「本拍是 0」且「上一拍也是 0」时才算空闲**。这样「周期末尾那一拍的 0」仍属于本周期（busy 为真），确保最后一段低电平完整走完，下一拍才进入真正的空闲。

**主计数器**：重装 / 倒数 / 缓存三合一。

[pulse_gen.sv:99-119](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pulse_gen.sv#L99-L119) — 计数到 0 且 `start && cntr_max!=0` 时重装 `seq_cntr <= cntr_max` 并缓存 `cntr_low_buf`、拉一拍 `start_strobe`；否则倒数。注意第 107 行特意排除 `cntr_max==0` 这个非法值，避免死循环。

**组合输出 pulse_out**：

[pulse_gen.sv:121-133](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pulse_gen.sv#L121-L133) — `pulse_out` 在 `always_comb` 里产生：`busy` 期间且 `seq_cntr >= cntr_low_buf` 时为高。> 注：作者在组合块里沿用了非阻塞 `<=` 写法，综合行为等价于阻塞赋值，阅读时按组合逻辑理解即可。

#### 4.1.4 代码实践

**实践目标**：验证 `pulse_gen` 的周期、脉宽与三种输出模式。

**操作步骤**：

1. 例化 `pulse_gen #(.CNTR_WIDTH(8))`，`start` 接 `1'b1`，`cntr_max = 8'd9`，`cntr_low = 8'd5`。
2. 在 testbench 里用 `initial forever #5 clk = ~clk;` 产生时钟，复位若干拍后释放。
3. 用 `$display` 或波形观察 `pulse_out` 与 `seq_cntr`（可临时把 `seq_cntr` 拉到模块外观察，或直接看 `start_strobe` 周期）。

**预期结果（基于源码分析，精确波形待本地验证）**：

- 周期 \( T = 9+1 = 10 \) 拍。
- 高电平 \( W_{\text{high}} = 9-5+1 = 5 \) 拍（`seq_cntr` 为 9,8,7,6,5 时），低电平 5 拍，占空比 50%。
- 把 `cntr_low` 改成 `8'd0` 应看到 `pulse_out` 常高；改成 `8'd10`（大于 `cntr_max`）应看到常低。

#### 4.1.5 小练习与答案

**练习 1**：想要一个周期 256 拍、占空比 25% 的脉冲，`cntr_max` 和 `cntr_low` 各取多少（`CNTR_WIDTH=9`）？

**答案**：周期 256 → `cntr_max = 255`；高电平 25% 即 64 拍 → \( 64 = 255 - \text{cntr\_low} + 1 \)，得 `cntr_low = 192`。

**练习 2**：为什么作者在第 107 行要求 `cntr_max != 0` 才允许重装？

**答案**：若 `cntr_max == 0`，重装后 `seq_cntr` 立刻又是 0，计数器永远停在 0，且每拍都试图重装，`start_strobe` 会变成持续高而不是单拍；排除该非法值可保证状态机行为确定。

---

### 4.2 脉冲拉伸：pulse_stretch

#### 4.2.1 概念说明

来自真实世界的脉冲往往「太短」：按键抖动产生的尖峰可能只有 1 拍宽，后续电路却要求一个足够宽的使能电平才能可靠动作。`pulse_stretch` 解决的就是**把任意触发沿拉宽成固定 `WIDTH` 拍的高电平**。它的 INFO 明确写道：若需要「可变」宽度，请改用 `pulse_gen`——本模块强调的是「宽度在编译期固定、电路极简」。

#### 4.2.2 核心流程

模块用 `generate` 按 `(WIDTH, USE_CNTR)` 在编译期选择实现：

```text
若 WIDTH == 0：        out 恒为 0
若 WIDTH == 1：        out = in（透传）
否则：
  USE_CNTR == 0（延迟线）：
      shifter 每拍左移并吞入 in
      out = (shifter != 0)            // 只要有一位 1 就输出高
  USE_CNTR == 1（计数器）：
      每拍：若 in==1，cntr 装入 WIDTH；否则若 out==1，cntr 减 1
      out = (cntr != 0)
```

两种实现都把 1 拍的 `in` 拓展成 `WIDTH` 拍的 `out`，差别只在资源：

| 实现 | 触发器用量 | 适合的 WIDTH |
|------|-----------|-------------|
| 延迟线（USE_CNTR=0） | `WIDTH` 个 FF | WIDTH 小（如 ≤8） |
| 计数器（USE_CNTR=1） | \( \lceil\log_2(\text{WIDTH}+1)\rceil \) 个 FF | WIDTH 大（如 100） |

例如 `WIDTH=100`：延迟线要 100 个 FF，计数器只要 \( \$clog2(101)=7 \) 个 FF——这就是综合实践里我们选计数器版的原因。

#### 4.2.3 源码精读

**参数与位宽计算**：

[pulse_stretch.sv:30-43](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pulse_stretch.sv#L30-L43) — `WIDTH` 是输出脉宽（拍数），`USE_CNTR` 选实现；`CNTR_W = $clog2(WIDTH+1)` 算出计数器位宽（用 `WIDTH+1` 是为了能装下 `WIDTH` 这个值本身）。

**延迟线实现**：

[pulse_stretch.sv:56-69](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pulse_stretch.sv#L56-L69) — 一个 `WIDTH` 位移位寄存器，每拍执行 `shifter <= {shifter[WIDTH-2:0], in}`。一个 1 拍的 `in` 会作为单个 '1' 位在移位寄存器里走 `WIDTH` 拍，期间 `out = (shifter != 0)` 一直为高，正好 `WIDTH` 拍后 '1' 被移出，`out` 回落。这正是 u2-l3「移位 = 延迟」思想的直接复用。

**计数器实现**：

[pulse_stretch.sv:71-90](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pulse_stretch.sv#L71-L90) — `in` 为高时把 `cntr` 装到 `WIDTH`，之后每拍减 1 直到归零。`out = (cntr != 0)` 同样给出 `WIDTH` 拍高电平。注意第 79 行的 `if(in)` 优先于第 82 行的 `else if(out)`——这意味着输入端持续为高时计数器会一直被重装，输出保持常高（与延迟线版行为一致）。

#### 4.2.4 代码实践

**实践目标**：对比两种实现的输出宽度与资源。

**操作步骤**：

1. 同时例化两份 `pulse_stretch`：`.WIDTH(8), .USE_CNTR(0)` 与 `.WIDTH(8), .USE_CNTR(1)`，共用 `clk`/`nrst`/`in`。
2. 在 testbench 里让 `in` 产生一个单拍脉冲，然后保持低电平至少 12 拍。

**预期结果（精确波形待本地验证）**：两个 `out` 都应在 `in` 触发后高电平持续 8 拍然后同时回落；查看综合报告时，延迟线版消耗约 8 个 FF，计数器版消耗 \( \$clog2(9)=4 \) 个 FF。

#### 4.2.5 小练习与答案

**练习 1**：若 `in` 是一个持续 3 拍宽的脉冲（而非 1 拍），`WIDTH=8` 的延迟线版 `out` 会高几拍？

**答案**：会高 `3 + 8 - 1 = 10` 拍。因为 3 个连续的 '1' 依次进入移位寄存器，最后一个 '1' 要走满 8 拍才移出；只要 `shifter` 里还有任何一个 '1'，`out` 就为高。

**练习 2**：为什么 `CNTR_W` 用 `$clog2(WIDTH+1)` 而不是 `$clog2(WIDTH)`？

**答案**：计数器需要能装入 `WIDTH` 这个值。例如 `WIDTH=8` 时，`$clog2(8)=3` 只能表示 0..7，装不下 8；`$clog2(9)=4` 才能。这是 u2-l4 讲过的「装下 0..N 用 `$clog2(N+1)`」的直接应用。

---

### 4.3 延迟事件：delayed_event

#### 4.3.1 概念说明

`delayed_event` 回答的问题是：「复位之后，我想在 `DELAY` 拍之后精确地产生**一个**单拍事件，用来触发初始化序列或下一级电路。」它和 `delay`（u2-l3）的区别在于：`delay` 是对一段**持续信号**整体平移；`delayed_event` 是对一个**时刻**做延迟，输出的是一根干净的单拍脉冲 `on_event`，外加 `before_event` / `after_event` 两根「阶段指示」线，方便把多个事件**菊花链式级联**。

INFO 里点明了三个特性：每次复位只触发一次事件；可用 `ena` 随时暂停计数；级联时把上一级的 `after_event` 接到下一级的 `ena`。

#### 4.3.2 核心流程

模块用 `generate` 把 `DELAY` 分成三种情形（`DELAY>=2` 是主路径）：

```text
DELAY == 0：   on_event = ena 的上升沿（经 edge_detect），即刻事件
DELAY == 1：   ena 打一拍后取上升沿，带 got_ena 阶段标志
DELAY >= 2（主路径）：
    seq_cntr 复位初值 = DELAY
    每拍：若 ena 且 seq_cntr != 0，则 seq_cntr <= seq_cntr - 1
    当 seq_cntr 减到 0：seq_cntr_is_0 = 1（此后保持，不再重装）
    on_event     = seq_cntr_is_0 的上升沿（edge_detect，单拍）
    before_event = ~seq_cntr_is_0    // 计数阶段为高
    after_event  =  seq_cntr_is_0    // 事件发生后为高，可驱动下一级
```

要点：

- **计数只在 `ena=1` 时推进**，所以延迟计量的是「使能拍数」而非绝对拍数；这恰好让「拉宽后的脉冲当作 `ena`」变得自然（见综合实践）。
- **只触发一次**：`seq_cntr` 到 0 后不再重装，`seq_cntr_is_0` 只产生一次上升沿，故 `on_event` 只脉冲一次；要再来一次必须重新复位。
- INFO 头部的 ASCII 波形图（[delayed_event.sv:17-31](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delayed_event.sv#L17-L31)）画的正是 `nrst` 释放后，经过 `DELAY` 拍 `on_event` 出现一个单拍脉冲、`before_event`/`after_event` 在事件前后互补的过程。

#### 4.3.3 源码精读

**主路径（DELAY>=2）的计数器**：

[delayed_event.sv:115-130](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delayed_event.sv#L115-L130) — `seq_cntr` 在声明处直接初始化为 `CNTR_W'(DELAY)`（复位值），第 126 行 `if( ena && ~seq_cntr_is_0 )` 才递减，体现「使能计数」与「到 0 即停」。

**用 edge_detect 产生单拍事件**：

[delayed_event.sv:132-140](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delayed_event.sv#L132-L140) — 这里直接例化了 u2-l2 的 `edge_detect`，输入是 `seq_cntr_is_0`，输出 `rising` 即 `on_event`。注意 `.anrst(1'b1)` 把异步复位 tie 死（内部计数器从不被异步复位，只靠外层 `nrst` 同步管理），且未覆盖 `REGISTER_OUTPUTS`，故 `on_event` 是组合上升沿、宽度一拍。

> 链接行号如显示异常，请以本地源码 `delayed_event.sv` 第 115–140 行为准（永久锚点已固定到当前 HEAD）。

**边界情形 DELAY==0 / DELAY==1**：

[delayed_event.sv:67-113](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delayed_event.sv#L67-L113) — `DELAY==0` 直接对 `ena` 取上升沿，`before_event=0`、`after_event=1`（永远「已发生」）；`DELAY==1` 多打一拍并引入 `got_ena` 锁存「是否已经发过事件」。这两个分支用 `generate` 在编译期消失，综合后不会引入多余电路。

#### 4.3.4 代码实践（读 testbench）

`delayed_event_tb.sv` 是一个信息量很大的「方法学样板」，它演示了**扫描一组参数**的做法：

[delayed_event_tb.sv:171-184](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delayed_event_tb.sv#L171-L184) — 用 `for(genvar i=0; i<16; i++)` 一次例化 16 个 `DELAY = 0..15` 的 `delayed_event`，共用同一套时钟/复位。这种「参数扫描例化」是验证时序模块边界行为的常用手法（u7-l1 会专门讲）。

**实践目标**：读懂这个 testbench 后，自己跑一次并观察 `on_event` 出现时刻随 `DELAY` 的变化。

**操作步骤**：

1. 阅读 [delayed_event_tb.sv:26-72](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delayed_event_tb.sv#L26-L72)：它用 `sim_clk_gen` 产生带抖动的 200 MHz 时钟，并另外起一个独立「异步」时钟域——这是在模拟跨域/抖动场景。
2. 用 u1-l3 介绍的方式编译运行（iverilog 需 `-g2012`，把 `delayed_event.sv`、`edge_detect.sv`、`sim_clk_gen.sv`、`clk_divider.sv` 与本 tb 一起编入）。
3. 把 16 个实例的 `on_event` 拉进波形，测量每个 `on_event` 相对 `nrst` 释放沿的延迟。

**预期结果（精确数值待本地验证）**：`DELAY=i` 的实例，其 `on_event` 应在 `nrst` 释放后约 `i` 个使能拍出现（`ena` 恒为 1），且每个实例只出现一次。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `on_event` 每次复位后只出现一次？如果想要「周期性」事件该怎么办？

**答案**：因为 `seq_cntr` 到 0 后不再重装，`seq_cntr_is_0` 只产生一次上升沿。要周期性事件应改用 `pulse_gen`（周期性），或周期性触发 `nrst` 重新装载 `delayed_event`。

**练习 2**：把第 1 级 `delayed_event` 的 `after_event` 接到第 2 级的 `ena`、第 2 级 `DELAY=5`，整体事件序列是什么？

**答案**：复位后第 1 级先等自己的 `DELAY1` 拍发 `on_event1`，此时 `after_event1` 变高，第 2 级才开始计数；再过 5 拍发 `on_event2`。于是得到一个「`DELAY1` → `DELAY1+5`」的两段定时序列，这正是 INFO 所说的菊花链级联。

---

### 4.4 动态延迟：dynamic_delay

#### 4.4.1 概念说明

`delay`（u2-l3）的延迟长度在例化时就写死了。`dynamic_delay` 则允许**运行时用 `sel` 端口选择延迟量**，而且粒度细到**位**——你既能整字延迟（把整个 `WIDTH` 位字搬到 k 拍之前），也能按位延迟（每个输出位来自不同历史时刻）。典型用途是数据对齐：把一路延迟可变的串行数据与另一路对齐到同一拍。

模块 INFO 给出一句重要告警：**故意不做「越界检查」**，`sel` 过大时会读到扁平化缓冲区之外，必须由调用方保证 `sel` 合法。

#### 4.4.2 核心流程

核心数据结构是一个深度 `LENGTH+1`、字宽 `WIDTH` 的移位寄存器，再把它**扁平化**成一根长线，用 `sel` 在上面选一段：

```text
data[0]            = in            （组合，零延迟，"当前输入"）
data[1..LENGTH]    每拍 ena 时整体右移：data[i] <= data[i-1]
                                   （data[k] 就是 k 拍前的输入）

pack_data[(LENGTH+1)*WIDTH-1:0] = 扁平化(data)   // data[0] 在最低位

输出选择（组合）：
  for j in 0..WIDTH-1:
      out[j] = pack_data[sel + j]
```

选择位宽由参数自动计算：

\[
\text{SEL\_W} = \lceil \log_2\big((\text{LENGTH}+1)\cdot\text{WIDTH}\big) \rceil
\]

`sel` 的语义（以 `WIDTH=4` 为例，`pack_data` 中每 4 位对应一拍）：

| `sel` | `out` 取自 | 含义 |
|-------|-----------|------|
| `0`  | `data[0]` | 零延迟（当前输入） |
| `4`  | `data[1]` | 整字延迟 1 拍 |
| `8`  | `data[2]` | 整字延迟 2 拍 |
| `1`  | `data[0]` 高 3 位 + `data[1]` 最低位 | **按位**混合延迟 |

可见 `sel` 每跨过 `WIDTH` 就多延迟一整拍；`sel` 落在 `WIDTH` 边界之内则做按位精调。

#### 4.4.3 源码精读

**二维移位寄存器与扁平化**：

[dynamic_delay.sv:56-60](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/dynamic_delay.sv#L56-L60) — `data` 是 `logic [(LENGTH+1)-1:0][WIDTH-1:0]` 的二维打包数组；`pack_data` 用一句 `assign pack_data[...] = data;` 直接把二维数组扁平化成一维长向量（SystemVerilog 的打包数组支持这种隐式转换），`data[0]` 落在最低有效位。

**移位（时序）**：

[dynamic_delay.sv:63-74](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/dynamic_delay.sv#L63-L74) — `for(i=1; i<LENGTH+1; i++) data[i] <= data[i-1];` 在每个使能拍把整条链右移一格。注意循环从 `i=1` 开始，`data[0]` 不参与移位——它由下面的组合块直接赋值。

**零延迟元素与输出选择（组合）**：

[dynamic_delay.sv:77-85](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/dynamic_delay.sv#L77-L85) — 第 79 行 `data[0] <= in` 把当前输入固定为「零延迟元素」；第 82–84 行的循环 `out[j] <= pack_data[sel+j]` 用 `sel` 在扁平化向量里截取连续 `WIDTH` 位作为输出。这段也解释了 INFO 的告警：当 `sel+j` 超出 `pack_data` 范围时没有保护，会读到无效位。

> 注：源码注释里「bit in[0] is the oldest / bit in[WIDTH] is the most recent」描述的是作者设想的位序约定，与上述可由 `pack_data` 实际布局验证的选择行为并存；精确位序建议在本地用固定图案扫描 `sel` 验证一次。

#### 4.4.4 代码实践

**实践目标**：用固定图案验证「整字延迟」与「按位延迟」。

**操作步骤**：

1. 例化 `dynamic_delay #(.LENGTH(3), .WIDTH(4))`。
2. testbench 里让 `in` 每拍递增（`4'd0, 4'd1, 4'd2, ...`），`ena=1`。
3. 分别设 `sel = 0`、`sel = 4`、`sel = 8`，观察 `out`。
4. 再设 `sel = 1`，观察 `out` 各位是否来自不同历史时刻。

**预期结果（精确波形待本地验证）**：

- `sel=0`：`out` 实时等于 `in`。
- `sel=4`：`out` 比 `in` 滞后 1 拍（`in` 为 5 时 `out` 为 4）。
- `sel=8`：滞后 2 拍。
- `sel=1`：`out` 的低位来自「当前字的高位」、高位来自「上一拍的低位」，呈现按位混合。

#### 4.4.5 小练习与答案

**练习 1**：`LENGTH=3, WIDTH=4` 时 `SEL_W` 是多少？`sel` 的合法最大值是多少？

**答案**：`SEL_W = $clog2((3+1)*4) = $clog2(16) = 4`。`pack_data` 共 16 位，下标 0..15；要保证 `sel + (WIDTH-1)` 不越界，即 `sel + 3 <= 15`，故 `sel` 合法最大值为 12（对应取到最高位 `pack_data[15]`）。

**练习 2**：为什么移位循环从 `i=1` 开始而不是 `i=0`？

**答案**：`data[0]` 是「零延迟元素」，由组合块第 79 行直接赋值为 `in`，不参与移位；若从 `i=0` 起做 `data[0]<=data[-1]` 既越界又破坏了「`data[0]` 永远是当前输入」的语义。

---

## 5. 综合实践

把本讲内容串起来，完成规格里设定的任务：**用一个按键脉冲（模拟毛刺）驱动 `pulse_stretch` 拉宽到 100 拍，再用 `delayed_event` 在拉宽后第 10 拍产生一个单拍事件。**

### 5.1 数据通路

```text
btn_glitch(1拍毛刺) ─▶ pulse_stretch(WIDTH=100, USE_CNTR=1) ─▶ stretched(100拍宽)
                                                              │
                                                              └─▶ delayed_event(DELAY=10).ena
                                                                        │
                                                                        └─▶ on_event(单拍事件)
```

设计要点（全部来自前文源码分析）：

- **毛刺只有 1 拍**，直接喂给多数后续电路不可靠，先用 `pulse_stretch` 拉宽。
- **`WIDTH=100` 选 `USE_CNTR=1`**：计数器版只要 7 个 FF，远省于延迟线版的 100 个。
- **`delayed_event` 的延迟按「使能拍」计量**：复位后 `seq_cntr` 停在 `DELAY=10`，在 `stretched` 为高（`ena=1`）期间才倒数，因此 `on_event` 恰好在拉宽窗口启动后约 10 拍出现。
- **`on_event` 只出现一次**：因为 `delayed_event` 每次复位只触发一次，正好符合「一次按键 → 一次事件」。

### 5.2 自检 testbench（示例代码）

下面是一个可直接编译的自检 testbench。它用 `event_count` 统计 `on_event` 次数，跑完后报告通过/失败。**作者声明：本 testbench 为示例代码，运行结果待本地验证。**

```systemverilog
// capstone_tb.sv —— 示例代码，演示 pulse_stretch + delayed_event 串联
`timescale 1ns/1ps

module capstone_tb();

  logic clk = 1'b0;
  always #5 clk = ~clk;            // 100 MHz

  logic rst = 1'b1;                // 高有效复位
  logic nrst;
  assign nrst = ~rst;

  initial begin
    rst = 1'b1;
    repeat(3) @(posedge clk);
    @(negedge clk);
    rst = 1'b0;                    // 释放复位
  end

  // 模拟按键毛刺：复位释放后第 10 拍产生一个单拍脉冲
  logic btn_glitch = 1'b0;
  initial begin
    @(negedge rst);
    repeat(10) @(posedge clk);
    btn_glitch = 1'b1;             // 单拍毛刺
    @(posedge clk);
    btn_glitch = 1'b0;
  end

  // 1) 拉宽到 100 拍（计数器实现）
  logic stretched;
  pulse_stretch #(
    .WIDTH( 100 ),
    .USE_CNTR( 1 )
  ) ps (
    .clk( clk ), .nrst( nrst ),
    .in( btn_glitch ),
    .out( stretched )
  );

  // 2) 拉宽后第 10 个使能拍产生单拍事件
  logic on_event, before_event, after_event;
  delayed_event #(
    .DELAY( 10 )
  ) de (
    .clk( clk ), .nrst( nrst ),
    .ena( stretched ),
    .on_event( on_event ),
    .before_event( before_event ),
    .after_event( after_event )
  );

  // 自检：统计 on_event 次数
  integer event_count = 0;
  always_ff @(posedge clk) begin
    if (on_event) begin
      event_count <= event_count + 1;
      $display("[T=%0t] on_event fired (count=%0d)", $realtime, event_count+1);
    end
  end

  initial begin
    repeat(200) @(posedge clk);    // 足够覆盖 100 拍拉伸 + 10 拍延迟
    if (event_count == 1)
      $display("PASS: exactly one on_event after stretched+delay");
    else
      $display("FAIL: event_count=%0d (expected 1)", event_count);
    $finish;
  end

endmodule
```

**操作步骤**：

1. 编译命令（iverilog，须 `-g2012`，参见 u1-l3）：把 `pulse_stretch.sv`、`delayed_event.sv`、`edge_detect.sv` 与 `capstone_tb.sv` 一起编入并仿真。
2. 观察波形里 `btn_glitch`、`stretched`、`on_event` 三者的时间关系。

**需要观察的现象与预期结果**：

- `btn_glitch` 是 1 拍尖峰；`stretched` 在其后立刻变高并持续约 100 拍。
- `on_event` 在 `stretched` 变高后约 10 拍出现一个单拍脉冲，且全程只有这一次。
- 仿真结束时控制台应打印 `PASS: exactly one on_event ...`。
- 由于 `pulse_stretch`/`delayed_event` 内部的逐拍时序存在若干寄存器延迟，`on_event` 相对 `btn_glitch` 的**绝对拍数**请以本地波形为准（待本地验证）；但「拉伸 100 拍、事件唯一、事件落在拉宽窗口内约第 10 拍」这三点是源码层面可确认的。

## 6. 本讲小结

- `pulse_gen` 用一个向下计数器产生周期 \( \text{cntr\_max}+1 \) 拍、脉宽由 `cntr_low` 控制的脉冲；`cntr_low=0` 常高、`>cntr_max` 常低，中间值出方波，输入被缓存故运行时可随意改。
- `pulse_stretch` 把窄脉冲拉宽成固定 `WIDTH` 拍；延迟线版耗 `WIDTH` 个 FF、计数器版只耗 \( \lceil\log_2(\text{WIDTH}+1)\rceil \) 个 FF，大宽度务必选计数器版。
- `delayed_event` 复位后经 `DELAY` 个**使能**拍产生唯一一个单拍 `on_event`；`before_event`/`after_event` 是阶段指示，把 `after_event` 接下一级 `ena` 即可实现事件菊花链。
- `dynamic_delay` 用「`LENGTH+1` 深 × `WIDTH` 宽」移位寄存器 + 扁平化选择，靠 `sel` 在编译期固定布局里做运行时可选延迟，粒度细到按位；故意不查越界，调用方需自保 `sel` 合法。
- 四个模块都是「时间整形」积木，常与 `edge_detect`（u2-l2）、`delay`（u2-l3）配合，构成系统里的节拍与对齐胶水。
- 读写源码时注意三类作者风格：`generate` 编译期多实现、INFO 里的 ASCII 波形图、以及「故意不做某项检查」的告警注释——它们都是 basic_verilog 模块的典型特征。

## 7. 下一步学习建议

- **向协议层走**：本讲的脉冲/事件模块是 u5 通信协议 IP（`uart_tx`/`spi_master`）的底层使能来源——例如 UART 的波特节拍、SPI 的片选时序本质上都是「定时脉冲/事件」。学完 u5-l1/l2 后可回头审视这些积木如何被组合进真实协议。
- **向测试方法学走**：`delayed_event_tb.sv` 里的「`genvar` 参数扫描 + 随机复位 + 异步时钟抖动」写法是 u7-l1（随机激励与自检 testbench）的预演，建议届时系统地学一遍。
- **继续精读相关源码**：`pulse_gen_tb.sv`、`pulse_stretch_tb.sv`、`dynamic_delay_tb.sv` 是与本讲配套的三个 testbench，阅读它们能补全每个模块的边界用例；`delay.sv`（u2-l3）和 `edge_detect.sv`（u2-l2）是本讲模块的直接依赖，值得对照重读。
- **动手扩展**：尝试给综合实践的通路再加一级——把 `on_event` 接到一个计数器，实现「按键 N 次后触发一次上报」，体会事件级联在系统里的用法。
