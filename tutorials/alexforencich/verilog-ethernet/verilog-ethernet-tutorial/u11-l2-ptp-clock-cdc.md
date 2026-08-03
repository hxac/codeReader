# ptp_clock_cdc：跨时钟域传递 PTP 时钟

## 1. 本讲目标

本讲解决一个具体的工程难题：**如何把一个在 A 时钟域里自由运行的 PTP 时间戳，安全、平滑地搬到与它异步的 B 时钟域**。

学完后你应当能够：

1. 说清楚「为什么不能直接用两级触发器去同步一个 96 位的时间戳总线」，以及 `ptp_clock_cdc` 为什么选择「在目的域重建一个自由时钟」而不是「搬运瞬时值」。
2. 描述源域采样、toggle 握手、三级同步器这套把多位时间戳安全送过时钟域边界的经典手法。
3. 讲清目的域里的「三级闭环控制」：采样率锁定环、相位/频率锁定环、时间 PI 环，以及它们如何分工把目的域时钟锁到源域。
4. 解释 deskew（去偏）的本质——为什么闭环控制能自动抵消源/目的域之间的固定传播延迟，而不需要手动减去一个延迟值。
5. 理解 PPS（秒脉冲）如何在目的域里重新生成并与目的域的秒边界对齐。

---

## 2. 前置知识

在进入本讲前，你需要先具备以下概念（它们来自本手册前置讲义）：

- **AXI-Stream 握手与时钟域**（u1-l3、u5-l1）：FPGA 里不同模块常跑在不同时钟上，跨时钟域（Clock Domain Crossing, CDC）需要专门的同步手段。
- **亚稳态与两级同步器**（u5-l1）：单比特控制信号跨异步域，用 2 级（或更多）触发器串联来把亚稳态概率压到可接受水平；但**多位总线不能这么干**，因为各比特的建立/保持时间不同步，采样出的值可能「半新半旧」。
- **ptp_clock 模块**（u11-l1，本讲的直接依赖）：
  - 它每拍累加一个步长 `period_ns` 自行走时间，同时输出 **96 位 ToD**（秒 + 纳秒 + 小数纳秒）和 **64 位相对**两种时间戳。
  - **小数纳秒 fns** 是 16 位定点小数，把 `{ns, fns}` 当成一个宽整数相加即可获得亚纳秒分辨率。
  - 时间被人为调整（加载/微调）时，会在 `output_ts_step` 上拉出一个单周期脉冲；进秒那一拍在 `output_pps` 上输出一个脉冲。
  - 96 位进秒用「超前借位」提前一拍预判秒边界。
- **PLL（锁相环）的直觉**：一个自由振荡的本地振荡器，配一个鉴相器比较它和参考信号的相位差，再用误差去微调本地振荡器的频率，直到两者同频同相。本讲的模块就是一个**全数字的 PLL**——本地振荡器是「每拍累加 `period_ns` 的计数器」，参考信号是「源域送来的时间戳」。

> 一个关键认知：`ptp_clock_cdc` 不是「同步器」，它是「锁相环」。它的输出 `output_ts` 不是源域 `input_ts` 的延迟副本，而是目的域里**自己重新生成**的一个时钟，只是被一个闭环锁到了和源域同频同相。

---

## 3. 本讲源码地图

本讲几乎全部围绕一个文件，并参考它的真实使用点与仿真平台：

| 文件 | 作用 |
|------|------|
| [rtl/ptp_clock_cdc.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock_cdc.v) | 本讲主角：跨时钟域 PTP 时钟重建模块（数字 PLL）。 |
| [rtl/eth_mac_10g_fifo.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_10g_fifo.v) | 真实集成点：在 10G MAC FIFO 里为 TX/RX 两个时钟域各例化一个 `ptp_clock_cdc`。 |
| [tb/ptp_clock_cdc/test_ptp_clock_cdc.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/ptp_clock_cdc/test_ptp_clock_cdc.py) | cocotb 仿真平台：用不同频率的输入/输出时钟验证锁定与跟踪精度。 |
| [tb/ptp_clock_cdc/Makefile](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/ptp_clock_cdc/Makefile) | 仿真构建脚本，声明 RTL 源与参数。 |
| rtl/ptp_clock.v（u11-l1） | 源域时钟的提供者，本讲把它当「上游」黑盒：它的 `output_ts`/`output_ts_step` 正是 `ptp_clock_cdc` 的 `input_ts`/`input_ts_step`。 |

---

## 4. 核心概念与源码讲解

### 4.1 时间戳跨时钟域：为什么要在目的域重建

#### 4.1.1 概念说明

先建立直觉：一个时间戳是一根**多位总线**（64 位或 96 位），而且它的值**每一拍都在变**。跨异步时钟域时，最朴素的两种想法都行不通：

1. **「直接拿目的域时钟去采样 `input_ts`」**——危险。多位总线各比特的传播延迟不同，目的域的采样沿可能正好落在某些比特刚翻页的瞬间，于是你采到的是一个**从来不存在过的拼凑值**（高位是新值、低位是旧值）。这正是 CDC 设计里反复强调的「多位总线不能用两级同步器」。

2. **「每过一阵子去抓一次，存到寄存器里再用」**——安全了（如果配合握手），但抓到的值是**过去某一拍**的快照，而且目的域时钟频率和源域不同，两次抓取之间目的域里没有任何「时间在走」的概念，时间戳会**跳变**（staircase），无法给需要连续、平滑时间参考的逻辑（比如给帧打精确时间戳）使用。

`ptp_clock_cdc` 的解法是第三条路：**在目的域里重新生成一个自由运行的 PTP 时钟**，再用一个闭环控制回路把它「锁」到源域。这样：

- 目的域的 `output_ts` 每拍都在平滑递增（因为它是本地自己累加出来的），**没有跳变**。
- 它的**频率和相位被闭环锁住**，长期看和源域 `input_ts` 同步，误差被压到亚纳秒级。
- 跨域传递的只是**少量单比特握手信号**和**周期性采样的时间值**，这些可以用安全的同步器处理。

所以这个模块本质上是一个**全数字锁相环（DPLL）**：本地振荡器 = 累加 `period_ns` 的计数器，参考 = 源域时间戳，鉴相 + 环路滤波 = 一段组合逻辑 + 寄存器。

> 命名提示：模块名里的 `cdc` 是 Clock Domain Crossing（跨时钟域）的缩写，但它做的远不止「跨域搬运」——它是在目的域**重建**时钟。

#### 4.1.2 核心流程：三个时钟域与整体数据流

模块同时涉及**三个**时钟域，理解它们各自的角色是读懂全模块的钥匙：

```
              input_clk 域                 output_clk 域（目的）
         (源 PTP 时钟所在域)             (要拿到时间戳的域)
         +------------------+           +---------------------------+
input_ts | 每隔 2^LOG_RATE  |  toggle   | 三级同步器收下 toggle     |
 ------> | 拍采样一次,     | --------> | 检测到新样本到达时,       |
         | 锁存时间值,      |  (单比特) | 锁存源域送来的时间值       |
         | 翻转 src_sync   |           |                           |
         +------------------+           |  + 本地自由时钟 ts_s/ts_ns |
                  ^                     |  |  (每拍累加 period_ns)   |
                  |                     |  v                         |
                  |   +-----------------| 比较源值 vs 本地值         |
                  |   |                 |  => 时间误差 ts_ns_diff    |
                  |   v                 |  => PI 控制器调 period_ns  |
         +------------------+           +---------------------------+
         |   sample_clk 域  |               ^
         | 鉴频器比较       |  速率误差      |
         | src/dest 采样率  | --------------+
         +------------------+  (调 dest_phase_inc)
```

- **`input_clk`（源域）**：源 PTP 时钟（`ptp_clock`）在这里运行。模块在这里**周期性采样** `input_ts`，把样本和「样本有效」的 toggle 标志送出去。
- **`output_clk`（目的域）**：在这里重建自由时钟 `ts_s`/`ts_ns`，并把环路滤波、PI 控制、PPS 生成全部放在这里。最终 `output_ts` 也是这个域的输出。
- **`sample_clk`（参考域）**：一个**既独立于 input 又独立于 output** 的第三时钟。它唯一的作用是给「采样率鉴频器」当裁判——因为它和两边都异步，能公平地数出两边的采样速率比，不会偏向任何一方。

整体流程一句话概括：**源域定期采样并翻转握手位 → 三级同步进目的域 → 目的域拿样本和本地时钟求差 → PI 控制器微调本地步长 → 本地时钟被锁到源域**。

#### 4.1.3 源码精读：端口、参数与源域采样

先看端口与参数。模块支持 64 位与 96 位两种时间戳宽度：

[rtl/ptp_clock_cdc.v:34-69](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock_cdc.v#L34-L69) —— 这是模块的全部端口：三个时钟/复位对（`input_*`、`output_*`、外加一个独立的 `sample_clk`），源域时间戳输入 `input_ts`/`input_ts_step`，目的域输出 `output_ts`/`output_ts_step`/`output_pps`，以及一个 `locked` 状态位。

参数里几个关键值（[L36-L39](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock_cdc.v#L36-L39)）：

- `TS_WIDTH`：96（默认，ToD）或 64（相对）。在 elaboration 期断言只能是这两个值（[L72-L77](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock_cdc.v#L72-L77)）。
- `LOG_RATE`：源域采样分频系数的以 2 为底对数，默认 3 → 每 \(2^3 = 8\) 个 `input_clk` 周期采样一次。
- `NS_WIDTH`：时间步长 `period_ns` 的整数纳秒部分位宽（默认 4；在 `eth_mac_10g_fifo` 里被改成 6）。小数部分 `FNS_WIDTH` 固定 16 位。

接着看源域采样逻辑（`input_clk` 域）：

```verilog
// rtl/ptp_clock_cdc.v:253-271 （节选）
always @(posedge input_clk) begin
    input_ts_step_reg <= input_ts_step || input_ts_step_reg;   // 累积 step，不丢事件
    src_phase_sync_reg <= input_ts[16+8];                       // 取时间戳的 bit24 当相位参考
    {src_update_reg, src_phase_reg} <= src_phase_reg+1;         // 分频计数器，进位即 src_update

    if (src_update_reg) begin
        // 采样源时间戳（fns 截到 CMP_FNS_WIDTH=4 位，避免追逐亚 LSB 噪声）
        if (TS_WIDTH == 96) begin
            src_ts_s_capt_reg   <= input_ts[95:48];
            src_ts_ns_capt_reg  <= input_ts[45:0] >> (16-CMP_FNS_WIDTH);
        end else begin
            src_ts_ns_capt_reg  <= input_ts >> (16-CMP_FNS_WIDTH);
        end
        src_ts_step_capt_reg <= input_ts_step || input_ts_step_reg;
        input_ts_step_reg <= 1'b0;
        src_sync_reg <= !src_sync_reg;                          // 翻转握手位，通知目的域
    end
    ...
```

这段做了三件事，每一件都对应一个跨域要点：

1. **分频采样**：`src_phase_reg` 是一个 `LOG_RATE` 位计数器，每拍加 1，溢出（进位）那一拍 `src_update_reg` 为 1。于是采样周期 = \(2^{\text{LOG\_RATE}}\) 个 `input_clk` 周期。
2. **多位值只在「样本有效」那一拍被锁存**：`input_ts` 是多位、每拍在变的总线，绝不能直接跨域；这里把它**先寄存进 `*_capt_reg`**，让它变成一个稳定的快照，稍后才跨域。比较时只取 `CMP_FNS_WIDTH=4` 位小数（[L84](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock_cdc.v#L84)），刻意丢弃更低 12 位 fns，避免环路去追逐远低于分辨率的噪声。
3. **toggle 握手**：`src_sync_reg` 是一个每出一次样本就**翻转**一次的单比特。这是跨异步域通知「有新数据」的标准手法——单比特可以安全地用多级同步器送过去，目的域靠「检测到翻转」来判断新样本到达。

注意 `src_phase_sync_reg <= input_ts[16+8]`（bit 24）：它每拍都跟新，不依赖采样脉冲。这是把时间戳里的「纳秒 bit 8」（每 256 ns 翻转一次）当作一个**连续的相位参考信号**单独跨域，供后面 4.2 的相位鉴相器使用。

跨域部分用三级同步器接收这些单比特信号：

[rtl/ptp_clock_cdc.v:283-293](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock_cdc.v#L283-L293) 把 `src_sync_reg`、两个相位参考位分别经 `sync1/2/3` 三级寄存器送进 `output_clk` 域。用三级（而非两级）是为了在亚稳态概率上更保守——这里对长期稳定性要求高。

> 为什么是「检测翻转」而不是「检测高电平」？因为 toggle 信号每来一个样本才翻一次，电平会长时间停留在 0 或 1；用 `sync2 ^ sync3`（前后两拍异或）能可靠地在这翻一次时打出**一个**脉冲，不会漏报也不会重报。

#### 4.1.4 代码实践：读源域采样路径

这是一个**源码阅读型实践**，目标是让你亲手验证上面讲的采样机制。

1. **实践目标**：算出源域的采样周期，并画出一次样本从产生到被目的域看见所经过的寄存器链。
2. **操作步骤**：
   - 打开 [rtl/ptp_clock_cdc.v:253-280](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock_cdc.v#L253-L280)，确认 `src_phase_reg` 的位宽是 `LOG_RATE`。
   - 假设 `input_clk` = 156.25 MHz（周期 6.4 ns）、`LOG_RATE` = 3，计算两次 `src_update` 之间的时间间隔。
   - 跟踪一次样本：`input_ts` → `src_ts_*_capt_reg`（源域锁存）→（跨域）→ `src_ts_*_sync_reg`（目的域锁存，见 [L494-L503](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock_cdc.v#L494-L503)）。
3. **需要观察的现象**：样本值在跨域前先被冻结成稳定快照；跨域的只有 toggle 与相位参考位这些单比特。
4. **预期结果**：采样周期 = \(2^3 \times 6.4\,\text{ns} = 51.2\,\text{ns}\)。跨域链是「`src_ts_*_capt_reg`（input 域，稳定值）→ `src_sync_reg`（toggle）经 3 级同步 → `output_clk` 域检测到翻转 → 锁进 `src_ts_*_sync_reg`」。
5. 时间间隔部分可手算确认；寄存器链按源码点名核对。

#### 4.1.5 小练习与答案

**练习 1**：如果把多位 `input_ts` 直接接到 `output_clk` 域的一组触发器上（不做握手），最坏情况会发生什么？

> **答案**：由于各比特传播延迟不等，`output_clk` 的采样沿可能正好卡在某些比特翻转的瞬间，采到一个「部分新、部分旧」的非法值；这些比特还可能进入亚稳态，导致目的域拿到一个从未存在过的时间戳，且无法预测何时恢复。

**练习 2**：`CMP_FNS_WIDTH = 4` 意味着比较时只保留 4 位小数纳秒。这相当于丢弃了多少纳秒以下的细节？为什么要丢？

> **答案**：4 位 fns 对应分辨率 \(2^{-4} \approx 0.061\) ns，丢弃的是比这更低的 12 位（约 0.000015 ns 量级）。原因是这些低位远低于系统能稳定分辨的精度，让环路去追逐它们只会引入抖动；截断后鉴相更干净，控制更稳。

---

### 4.2 去偏 deskew：三级闭环控制如何抵消域间延迟

#### 4.2.1 概念说明

「deskew（去偏）」要解决的问题是这样的：即便我们把样本安全地送过了时钟域，**源域采样的瞬间**和**目的域拿到并使用它的瞬间**之间，隔着若干拍同步延迟、若干拍流水线延迟。如果只是「把源域时间值原样写到目的域」，目的域的时间会**始终滞后一个固定偏移量**（skew）。

`ptp_clock_cdc` 的 deskew 思路不是去「测量延迟然后减掉」，而是**用闭环让偏移自动归零**：

- 目的域在**自己的**采样事件（`dest_update`）发生时，也抓一份**本地**时间戳。
- 把「源域样本」和「目的域同时刻样本」做**差**，得到时间误差 `ts_ns_diff`。
- 把这个误差喂给一个 **PI 控制器**（比例 + 积分），用它去微调本地时钟的步长 `period_ns`。
- 只要误差不为零，积分项就会持续累积、持续调整，**直到误差被压到零**才停下来。

这就是 deskew 的本质：**固定延迟会在误差里表现为一个常数偏置，而 PI 控制器的积分项专门用来消除常数偏置**——它把这个偏置吸收成 `period_ns` 上的一个小偏移，于是稳态下「目的域本地时间」恰好等于「源域时间」，延迟被自动抵消，不需要任何手工补偿。

为了让这个主环路能稳、能快、能从冷启动拉进来，模块在主环路之外还套了两个辅助环路，构成**三级锁定**：

| 环路 | 所在域 | 比较什么 | 调什么 | 产出哪个锁定位 |
|------|--------|----------|--------|----------------|
| ① 采样率鉴频 | `sample_clk` | `src_sync` 与 `dest_sync` 的边沿速率 | `dest_phase_inc`（采样分频步长） | `dest_sync_locked` |
| ② 相位/频率鉴相 | `output_clk` | 时间戳 bit24 的边沿（`src_phase_sync` vs `dest_phase_sync`） | 粗调 `ts_ns_diff` + `freq_locked` 判据 | `freq_locked` |
| ③ 时间 PI 主环 | `output_clk` | 源样本时间值 vs 目的本地时间值 | `period_ns`（本地时钟步长） | `ptp_locked` |

只有三者同时为真，`locked` 才置位：

```verilog
// rtl/ptp_clock_cdc.v:549
assign locked = ptp_locked_reg && freq_locked_reg && dest_sync_locked_reg;
```

[rtl/ptp_clock_cdc.v:549](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock_cdc.v#L549)

#### 4.2.2 核心流程：目的域自由时钟与三级环路

**(a) 目的域自由时钟**——这是被锁的对象。它和 `ptp_clock`（u11-l1）的走时逻辑同源：每拍把步长 `period_ns` 累加进 `ts_ns`，96 位下用「超前借位」预判进秒：

```verilog
// rtl/ptp_clock_cdc.v:577-598 （节选，96 位分支）
period_ns_delay_next = period_ns_reg;
period_ns_ovf_next   = {NS_PER_S, {FNS_WIDTH{1'b0}}} - period_ns_reg;  // 1e9 ns 倒数
ts_ns_inc_next = ts_ns_inc_reg + period_ns_delay_reg;                  // 正常累加
ts_ns_ovf_next = ts_ns_inc_reg - period_ns_ovf_reg;                    // 超前借位预判
ts_ns_next     = ts_ns_inc_reg;
if (!ts_ns_ovf_reg[30+FNS_WIDTH]) begin   // 借位位为 0 ⇒ 本拍将跨过秒边界
    ts_ns_inc_next = ts_ns_ovf_reg + period_ns_delay_reg;
    ts_s_next      = ts_s_reg + 1;        // 进秒
end
```

[rtl/ptp_clock_cdc.v:577-598](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock_cdc.v#L577-L598)。冷启动时 `period_ns = 0`，本地时钟不走；靠下面的环路把它拉起来并锁住。

**(b) 环路①：采样率鉴频（`sample_clk` 域）**。它比较「源域每秒出多少个样本」和「目的域每秒做多少次本地采样」。两者都是 toggle 信号（`src_sync`、`dest_sync`），送进 `sample_clk` 域用一个**鉴频鉴相器**（`edge_1`/`edge_2`）数出谁快谁慢，再用一个累加器 `sample_acc` 在一个窗口里求和：

[rtl/ptp_clock_cdc.v:309-341](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock_cdc.v#L309-L341)。结果送回 `output_clk` 域，用一个积分器去调 `dest_phase_inc`（[L358-L414](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock_cdc.v#L358-L414)）。

`dest_phase_inc` 控制的是 `dest_phase_reg` 这个计数器的步长；`dest_phase_reg` 溢出（进位）那一拍就是 `dest_update`，也就是「目的域抓一次本地时间戳」的事件。所以**环路①把目的域的采样速率锁到源域的采样速率**——保证两边在比较时间值时，是在「同等节奏」的采样点上比，否则主环路没法工作。它收敛后给出 `dest_sync_locked`。

**(c) 环路②：相位/频率鉴相（`output_clk` 域）**。它比较源/目的**时间戳本身**的某个高位（bit 24，即纳秒 bit 8，每 256 ns 翻转一次）的边沿：

[rtl/ptp_clock_cdc.v:447-478](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock_cdc.v#L447-L478)。这个鉴相器比环路①更「贴近真实时间」，用来做**粗的频率判定**：当相位误差 `phase_err_out` 的绝对值连续足够久落在 ±4 以内，就置 `freq_locked`（[L651-L665](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock_cdc.v#L651-L665)）。未锁定时，它还把相位误差放大成 `ts_ns_diff`，给主环路一个**快速牵引**信号（[L667-L670](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock_cdc.v#L667-L670)）。

**(d) 环路③：时间 PI 主环（`output_clk` 域）**——deskew 真正发生的地方。当源样本和目的样本都有效时，做差：

```verilog
// rtl/ptp_clock_cdc.v:618-619 （时间差计算）
ts_ns_diff_next = src_ts_ns_sync_reg - dest_ts_ns_capt_reg;   // 源时间 − 目的时间 = 误差
ts_diff_next    = (src_ts_s_sync_reg != dest_ts_s_capt_reg) || ... ;  // 是否秒/高位不一致
```

然后这段误差被一个**带增益调度的 PI 控制器**消费（[L673-L745](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock_cdc.v#L673-L745)）。增益调度（gain scheduling，[L677-L689](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock_cdc.v#L677-L689)）根据误差大小切换两套增益：

- 误差大 → `gain_sel=1`，高增益（系数 ×64），快速把时钟拉过来；
- 误差小 → `gain_sel=0`，低增益（系数 ×4），慢慢收敛、避免在平衡点附近抖动。

PI 输出就是新的步长 `period_ns`：

\[ \text{period\_ns} \;=\; K_p \cdot e \;+\; \text{time\_err\_int}, \qquad \text{time\_err\_int} \leftarrow \text{time\_err\_int} + K_i \cdot e \]

其中 \(e = \text{ts\_ns\_diff}\)。稳态时 \(e \to 0\)，而 `time_err_int` 保留着一个固定偏置——**正是这个偏置把固定延迟给抵消掉了**，这就是 deskew。`ptp_locked` 在「频率已锁 + 用低增益 + 稳定足够多窗」后置位（[L728-L744](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock_cdc.v#L728-L744)）。

**特殊处理：源域 step 与强制重载**。如果源时钟被人为调整（`input_ts_step`），本地时钟不能再去慢慢追，而是**直接硬加载**到源值（[L600-L636](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock_cdc.v#L600-L636)），并在 `output_ts_step` 上照样输出一个 step 脉冲，把「时间被跳变」这件事如实传递给下游。若误差持续不收敛（`mismatch_cnt` 累计），也会强制重载一次（[L638-L649](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock_cdc.v#L638-L649)），作为环路跑飞的兜底。

#### 4.2.3 仿真证据：精度有多高？

这套三级环路的实际表现，有现成的 cocotb 测试平台给出量化结论。`tb/ptp_clock_cdc/test_ptp_clock_cdc.py` 在多种输入/输出时钟频率组合下断言：**锁定后，目的域时间与源域时间的平均误差小于 5 ns**：

```python
# tb/ptp_clock_cdc/test_ptp_clock_cdc.py:191-198 （节选）
for i in range(100000):
    await RisingEdge(dut.input_clk)
assert tb.dut.locked.value.integer                    # 必须锁定
diffs = await tb.measure_ts_diff()
assert abs(mean(diffs)*1e9) < 5                        # 平均误差 < 5 ns
```

该平台覆盖了同频、±10 ppm、±200 ppm、相干跟踪（连续慢变频率），甚至 **6.4 ns vs 2.56 ns（156 MHz vs 390 MHz，频率比约 2.5×）** 这种悬殊比例，全部能锁定且误差 < 5 ns。这说明 deskew + 三级环路在很宽的频率失配范围内都成立。

#### 4.2.4 代码实践：跑仿真观察锁定与跟踪

这是一个**可运行实践**，直接用现成 testbench。

1. **实践目标**：亲眼看 `locked` 从 0 变 1，并量化目的域时间相对源域的误差。
2. **操作步骤**（需先装好 cocotb、cocotbext-eth、iverilog，参考 u1-l4）：
   ```bash
   cd tb/ptp_clock_cdc
   make                        # 默认 TS_WIDTH=96，跑 run_test
   # 或单独跑一个参数：
   pytest "tb/ptp_clock_cdc/test_ptp_clock_cdc.py::test_ptp_clock_cdc[ts_width96]"
   ```
3. **需要观察的现象**：仿真日志会按顺序打印 `Same clock speed`、`10 ppm slower`、`200 ppm faster`、`Coherent tracking`、`Significantly faster (390.625 MHz)` 等阶段，每个阶段后打印一行 `Difference: ... ns (stdev: ... ns)`。
4. **预期结果**：每个阶段的 `Difference` 绝对值都应 < 5 ns，且对应 `assert tb.dut.locked...` 不报错。
5. 若本地尚未配好工具链，相关数值标「待本地验证」；但断言阈值（< 5 ns）与覆盖的频率组合直接取自 [test_ptp_clock_cdc.py:191-415](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/ptp_clock_cdc/test_ptp_clock_cdc.py#L191-L415)，可在源码中核对。

#### 4.2.5 小练习与答案

**练习 1**：为什么主环路用 **PI**（带积分）而不是单纯的比例（P）控制？积分项和 deskew 有什么关系？

> **答案**：纯比例控制对**常数偏置**（固定延迟造成的 skew）有稳态误差——误差不为零才能产生控制量。积分项会把历史误差累加起来，即使当前误差很小也能持续输出一个偏置去抵消固定延迟，使稳态误差趋于零。所以积分项就是 deskew 的承担者。

**练习 2**：增益调度里，误差大时用高增益、误差小时切低增益。如果一直用高增益会怎样？

> **答案**：高增益响应快，但在平衡点附近会让 `period_ns` 过度反应、来回超调，导致时间戳在锁定后仍有可见抖动（stdev 变大），甚至无法满足 `ptp_locked` 的「低增益 + 稳定」判据而锁不定。低增益牺牲速度换取平稳收敛。

**练习 3**：环路①（`sample_clk` 域）锁的是「采样速率」而不是「时间值」。如果它没锁住，主环路③为什么也工作不了？

> **答案**：主环路③比较的是「源样本值」和「目的域同时刻样本值」。只有当两边以**相同节奏**采样（环路①保证），「同时刻」才成立；否则拿源域 t1 时刻的值去减目的域 t2 时刻的值，差里混进了采样节奏不同步带来的系统性误差，主环路会被误导、无法收敛到真正的零偏。

---

### 4.3 目的域 PPS 再生与输出格式

#### 4.3.1 概念说明

源 PTP 时钟除了时间戳，还会输出一个 **PPS（Pulse Per Second，每秒一脉冲）**信号（u11-l1）。到了目的域，我们不能直接把这个脉冲跨域传过去——它是一个极窄（一拍）的单周期脉冲，跨异步域时很可能被采样沿错过而**漏掉**。

`ptp_clock_cdc` 的做法是**在目的域里重新生成 PPS**：因为目的域已经有了一个被锁住的本地时钟，而进秒事件完全由本地时钟的「秒边界」决定，所以**直接从本地时钟派生 PPS**即可。这样 PPS 天然与目的域时钟同步、与目的域的秒边界对齐，不存在跨域丢脉冲的问题。

#### 4.3.2 核心流程：从本地时钟的进秒事件派生 PPS

96 位格式下，PPS 复用了 4.2 里那段「超前借位」进秒判定：当借位位 `ts_ns_ovf_reg[30+FNS_WIDTH]` 在某拍为 0，意味着这一拍本地时钟刚好跨过秒边界，于是在 `pps_reg` 上打出一个脉冲：

```verilog
// rtl/ptp_clock_cdc.v:776-781
if (TS_WIDTH == 96) begin
    pps_reg <= !ts_ns_ovf_reg[30+FNS_WIDTH];        // 进秒那一拍拉高
end else if (TS_WIDTH == 64) begin
    pps_reg <= 1'b0; // not currently implemented for 64 bit timestamp format
end
```

[rtl/ptp_clock_cdc.v:776-781](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock_cdc.v#L776-L781)。注意两点：

- 因为本地时钟被锁到了源域，所以这个再生 PPS 的**频率和相位**也跟着锁到了源域的秒边界——这正实现了「PPS 在目的域对齐」。
- 64 位相对格式**没有进秒概念**（它单调累加、不回卷），所以 PPS 在 64 位下不实现，恒为 0。这是格式选型时要注意的取舍。

#### 4.3.3 核心流程：`output_ts` 的位打包

`output_ts` 直接由本地寄存器 `ts_s_reg`/`ts_ns_reg` 组合而成，96 位与 64 位打包不同（在一段 `generate` 里按 `PIPELINE_OUTPUT` 与 `TS_WIDTH` 选择）：

[rtl/ptp_clock_cdc.v:221-235](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock_cdc.v#L221-L235)（无流水线分支）：

```verilog
if (TS_WIDTH == 96) begin
    assign output_ts[95:48] = ts_s_reg;              // 高 48 位：秒
    assign output_ts[47:46] = 2'b00;                 // 保留
    assign output_ts[45:0]  = {ts_ns_reg, 16'd0} >> FNS_WIDTH;  // 低 46 位：ns+fns
end else if (TS_WIDTH == 64) begin
    assign output_ts = {ts_ns_reg, 16'd0} >> FNS_WIDTH;         // 全部：相对 ns+fns
end
assign output_ts_step = ts_step_reg;
assign output_pps     = pps_reg;
```

96 位格式：`{秒[47:0], 2'b0, ns[29:0], fns[15:0]}`，与 `ptp_clock` 的 96 位 ToD 格式一致（u11-l1）。`PIPELINE_OUTPUT > 0` 时（[L170-L219](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock_cdc.v#L170-L219)）改为寄存器流水线输出，用 `(* shreg_extract = "no" *)` 阻止综合工具把移位寄存器折叠进 SRL，以改善时序——这条属性在本库的 CDC/FIFO 模块里是常见套路（参见 u5-l1）。

#### 4.3.4 真实用法：一份 PTP 时钟分发给多个域

看 `eth_mac_10g_fifo` 怎么用它，最能说明本模块的存在意义。10G MAC 的 TX/RX 各自跑在 `tx_clk`/`rx_clk` 两个独立时钟域，但整个系统只有**一个**权威 PTP 时钟（在 `logic_clk` 域）。于是为 TX、RX **各例化一个** `ptp_clock_cdc`，把同一份源时间戳分别搬到两个域：

```verilog
// rtl/eth_mac_10g_fifo.v:226-242 （TX 侧，节选）
ptp_clock_cdc #(
    .TS_WIDTH(PTP_TS_WIDTH),
    .NS_WIDTH(6)                       // 10G 用例把整数纳秒位宽加到 6
)
tx_ptp_cdc (
    .input_clk(logic_clk),  .input_rst(logic_rst),
    .output_clk(tx_clk),    .output_rst(tx_rst),
    .sample_clk(ptp_sample_clk),       // 两个实例共用同一个参考时钟
    .input_ts(ptp_ts_96),  .input_ts_step(ptp_ts_step),
    .output_ts(tx_ptp_ts_96), ...
);
```

[rtl/eth_mac_10g_fifo.v:226-242](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_10g_fifo.v#L226-L242)（TX）与 [L301-L317](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_10g_fifo.v#L301-L317)（RX）。注意三个细节：

1. `input_clk = logic_clk`（源时钟所在域），`output_clk = tx_clk` 或 `rx_clk`（目的域）——正是「跨域」的方向。
2. 两个实例**共用同一个 `ptp_sample_clk`**（端口声明见 [eth_mac_10g_fifo.v:73](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_10g_fifo.v#L73)），因为鉴频参考域可以复用。
3. `NS_WIDTH` 在这里改成 6，给步长更大的整数纳秒范围，以适配 10G 数据通路的时间戳精度需求。

这正是「自由运行 PTP 时钟跨域分发」的标准模式：**一份源、多个目的域副本，每个副本各自锁定、各自再生 PPS**。

#### 4.3.5 代码实践：抓 PPS 并核对打包

1. **实践目标**：在仿真里看到目的域 PPS，并验证它落在本地时钟的秒边界上。
2. **操作步骤**：
   - 在 `tb/ptp_clock_cdc` 下以波形模式跑：`make WAVES=1`（Makefile 会生成 `iverilog_dump.v` 并 dump 出 `.fst`，见 [Makefile:44-48](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/ptp_clock_cdc/Makefile#L44-L48)）。
   - 用 GTKWave 打开 `ptp_clock_cdc.fst`，观察 `pps_reg`、`ts_s_reg`、`ts_ns_reg`、`locked`。
   - 把 `output_ts` 按 4.3.3 的格式拆开（高 48 位 = 秒，低字段 = ns+fns），看 `pps_reg` 拉高那一拍 `output_ts` 是否正跨越整数秒。
3. **需要观察的现象**：`pps_reg` 每秒一个单周期脉冲；脉冲出现的拍上，秒字段 `+1`、纳秒字段刚回卷到 0 附近；稳态下该脉冲与源域 PPS 同相。
4. **预期结果**：PPS 周期 ≈ 1 s（仿真时间），且 `output_ts` 的秒字段在脉冲处递增。注意 64 位参数（`PARAM_TS_WIDTH=64`）下 `pps_reg` 恒为 0，这是设计预期，不是 bug。
5. 完整 1 秒的仿真耗时较长（需跑到 `input_ts` 跨秒），若机器较慢可只确认 `pps_reg` 与秒字段递增的对应关系，标「待本地验证」。

#### 4.3.6 小练习与答案

**练习 1**：为什么 PPS 是「在目的域重新生成」，而不是把源域的 PPS 脉冲跨域传过来？

> **答案**：源域 PPS 是一拍宽的单周期脉冲，跨异步域时目的域采样沿很可能正好落在脉冲之外而**漏采**。改用「从已被锁住的本地时钟派生 PPS」后，脉冲天然同步于目的域时钟、不会丢，且因本地时钟锁在源域，其频率/相位也与源域 PPS 一致。

**练习 2**：64 位格式下 `output_pps` 恒为 0。如果你在 64 位系统里确实需要 PPS，该怎么办？

> **答案**：64 位「相对」时间戳单调累加、不回卷，没有「秒边界」概念，故无法从时间戳派生 PPS。需要 PPS 就应改用 96 位 ToD 格式；或在 64 位值上自行用一个对 \(10^9\) ns 取模的比较器另造秒边界（但那样不如直接用 96 位格式来得一致）。

---

## 5. 综合实践

把本讲三件事（跨域、deskew、PPS 再生）串起来，做一个端到端的小验证。

**任务**：参照 `eth_mac_10g_fifo` 的用法，在仿真里搭建「一个源 PTP 时钟 → `ptp_clock_cdc` → 两个频率不同的目的域」的最小系统，验证两个目的域的 96 位 ToD 在秒边界对齐。

**建议步骤**：

1. 复用 `tb/ptp_clock_cdc/test_ptp_clock_cdc.py` 里现成的驱动：它已经用 `cocotbext.eth.PtpClock`（`ts_tod` 模式，周期 6.4 ns，见 [test_ptp_clock_cdc.py:48-65](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/ptp_clock_cdc/test_ptp_clock_cdc.py#L48-L65)）当源，并用自定义的 `_run_input_clock`/`_run_output_clock` 跑出**两个不同频率**的时钟（默认都 6.4 ns，可改）。
2. 把 `output_clk` 调成与 `input_clk` 不同（例如 `set_output_clock_period(4.0)`，对应 250 MHz），等待 `locked` 置 1。
3. 用平台里的 `measure_ts_diff()`（[L135-L173](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/ptp_clock_cdc/test_ptp_clock_cdc.py#L135-L173)）量化目的域 96 位 ToD 相对源域的误差，确认均值 < 5 ns——这同时验证了 deskew 把跨域延迟抵消掉了。
4. 在波形里观察 `output_pps`：在 `locked` 之后，目的域 PPS 应与源域秒边界对齐（频率一致、相位锁定）。
5. （进阶）像 `eth_mac_10g_fifo` 那样**同时例化两个** `ptp_clock_cdc`，共用一个 `sample_clk`，分别输出到 `tx_clk`/`rx_clk`，验证两份目的域时间戳彼此也一致——这就是「一份源、多域分发」。

**验收点**：`locked` 在各种频率比下都能置 1；`measure_ts_diff` 均值 < 5 ns；PPS 周期约 1 s 且相位稳定。无法本地运行时，以上阈值与行为均可在 [test_ptp_clock_cdc.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/ptp_clock_cdc/test_ptp_clock_cdc.py) 的断言中核对。

---

## 6. 本讲小结

- **跨域不靠搬运、靠重建**：96 位时间戳是「多位、每拍在变」的总线，不能直接同步；`ptp_clock_cdc` 在目的域用数字 PLL **重新生成**一个自由时钟，再把闭环锁过去，从而得到平滑、无跳变的时间。
- **安全跨域靠 toggle 握手 + 三级同步**：源域周期性把时间值**冻结成快照**再锁存，只让单比特 toggle 与相位参考位跨域；目的域靠「检测翻转」可靠地知道新样本到达。比较时只保留 4 位 fns，避免追逐噪声。
- **三个时钟域分工**：`input_clk`（源采样）、`output_clk`（本地时钟与主环路）、`sample_clk`（独立的鉴频裁判）。
- **deskew = PI 的积分项**：目的域同时抓本地时间，与源样本做差；PI 控制器的积分项把固定跨域延迟吸收成 `period_ns` 上的偏置，使稳态误差归零——无需手工测延迟。带增益调度的三级环路（采样率锁 → 频率锁 → 时间锁）协同收敛，`locked` 三者皆真才置位。
- **PPS 在目的域再生**：从已锁定的本地时钟的进秒事件直接派生，天然与目的域同步、与源域同相；64 位格式不提供 PPS。
- **真实用法 = 一源多域分发**：`eth_mac_10g_fifo` 为 TX/RX 各例化一个实例、共用 `sample_clk`，把单一权威 PTP 时钟安全分发到每个以太网时钟域。

---

## 7. 下一步学习建议

- **继续 PTP 子系统**：本讲是 u11（PTP）的第二讲。接着读 **u11-l3（PTP 时间戳标记与 MAC 集成）**，看 `ptp_ts_extract` 如何从 AXI-Stream 的 `tuser` 旁带里把时间戳取出来，以及 `eth_mac_*` 在 `PTP_TS_ENABLE` 下如何把本讲产生的 `output_ts` 旁路到帧上——那正是 `ptp_clock_cdc` 输出被真正消费的地方。
- **时间分发专题**：如果你对「把一份时间分发给多个域」感兴趣，**u11-l4（PTP 时间分发：PHC 与 leaf）** 讲解 `ptp_td_phc`/`ptp_td_leaf`/`ptp_td_rel2tod`——那是另一套（基于串行总线的）分发机制，与本讲的数字 PLL 互补，可对比两者的取舍。
- **深入源码**：想彻底吃透控制环路，建议逐行走读 [ptp_clock_cdc.v:309-341](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock_cdc.v#L309-L341)（环路①鉴频）与 [L673-L745](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock_cdc.v#L673-L745)（环路③ PI + 增益调度），并结合 4.2.4 的仿真在波形上对照每个锁定位的置位时机。
- **对照 u11-l1**：本讲的本地自由时钟与进秒逻辑直接继承自 `ptp_clock`（u11-l1）；遇到 `period_ns`、fns、`ts_step`、超前借位等概念不清时，回头读 u11-l1 即可。
