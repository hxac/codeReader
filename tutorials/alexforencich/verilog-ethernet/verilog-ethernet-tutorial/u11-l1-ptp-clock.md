# ptp_clock：时间戳与频率微调

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 verilog-ethernet 里 `ptp_clock` 输出的两种时间戳——**64 位相对时间戳**与 **96 位 ToD（Time of Day，当日时间）时间戳**——各自的字段结构、含义与区别。
- 理解「小数纳秒（fns，fractional nanoseconds）」这种定点小数表示法，以及它如何让一个纯整数加法器表达亚纳秒级的时钟步长。
- 看懂三种「让时钟跑快或跑慢」的微调入口：`input_period`（改基准步长＝频率微调）、`input_adj`（瞬时偏移/相位微调）、`input_drift`（持续漂移补偿）。
- 解释 96 位时间戳如何在一秒边界处「进秒」、`output_pps`（每秒脉冲）如何在进秒时产生、以及 `output_ts_step`（时间跳变标志）在何时拉高。
- 会用 `tb/ptp_clock` 里现成的 cocotb 仿真跑通这个模块，并亲手注入一次 `input_adj`、配置一次 `input_drift`，观察时间值与步进标志的变化。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 为什么以太网 IP 里需要一个「硬件时钟」

PTP（Precision Time Protocol，精确时间协议，IEEE 1588）的目标是把网络上多个节点的时钟同步到亚微秒甚至纳秒级。软件协议栈只能告诉你「报文大概在某一刻发出」，但报文真正离开 PHY 管脚的精确时刻，只有紧贴硬件的**时间戳计数器**才能抓到。因此 verilog-ethernet 在 MAC 内部（见 `eth_mac_1g`/`eth_mac_10g` 的 `PTP_TS_ENABLE`）需要一个自由运行的硬件时钟，在帧的起始/结束瞬间给它打上时间戳。本讲的 `ptp_clock` 就是这个硬件时钟本身——它不依赖任何外部报文，只靠每个时钟周期累加一个步长来自行「走时间」。

### 2.2 用整数加法器表达「小数纳秒」

一个 156.25 MHz 的时钟，每个周期是 \(6.4\,\text{ns} \)，不是整数纳秒。纯整数累加器无法表达 \(0.4\,\text{ns}\) 这一截小数。解决办法是**定点小数**：把时间拆成

\[
\text{时间} = \text{纳秒整数部分（ns）} + \frac{\text{小数部分（fns）}}{2^{16}}
\]

即用一个 16 位的「小数纳秒（fns）」字段表示 \(0 \sim 2^{16}-1\)，对应 \(0 \sim\) 略小于 \(1\,\text{ns}\)。每周期把 `{ns, fns}` 当作一个宽整数整体相加，fns 自然向 ns 进位，就得到了亚纳秒分辨率。本模块里 `FNS_WIDTH`（默认 16）就是这个字段位宽。

### 2.3 「调时间」的三种姿态

一个可被伺服（servo）驯服的时钟，需要三种独立的调节手段，对应本模块三个输入口：

| 入口 | 作用 | 生效方式 | 何时用 |
|------|------|----------|--------|
| `input_period` | 改每拍基准步长 | 立即、持续 | 频率微调（把时钟长期调快/调慢） |
| `input_adj` | 在若干拍内叠加一个偏移 | 临时、持续 `count` 拍 | 瞬时偏移/相位校正（把时间整体前推或后挪） |
| `input_drift` | 每 `rate` 拍叠加一个小量 | 持续、周期性 | 漂移补偿（抵消晶振长期的固定频偏） |

记住这张表，源码里三条数据通路就是它的硬件实现。

## 3. 本讲源码地图

本讲只涉及一个核心文件，但会顺带引用它的 cocotb 仿真三件套作为实践依据：

| 文件 | 作用 |
|------|------|
| [rtl/ptp_clock.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock.v) | 本讲主角：自由运行的 PTP 硬件时钟，输出 64/96 位时间戳、PPS、step 标志 |
| [tb/ptp_clock/Makefile](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/ptp_clock/Makefile) | cocotb 仿真入口：声明参数、调用 iverilog |
| [tb/ptp_clock/test_ptp_clock.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/ptp_clock/test_ptp_clock.py) | cocotb 用例：默认速率、加载时间戳、进秒/PPS、频率微调、漂移补偿各一组断言 |

> 提示：仓库里 `tb/test_ptp_clock.v` 与 `tb/test_ptp_clock.py`（带 `$from_myhdl`/`from myhdl import`）是 myhdl 时代的历史遗留，当前流程不再编译它们；真正在用的是 `tb/ptp_clock/` 目录下同名的 cocotb 版本（见第 1 单元 u1-l4 的说明）。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**(4.1) 两种时间戳格式与小数纳秒**、**(4.2) 每拍步长、瞬时偏移与频率微调**、**(4.3) 进秒回卷、PPS 与漂移补偿**。

---

### 4.1 两种时间戳格式与小数纳秒

#### 4.1.1 概念说明

`ptp_clock` 同时维护两个独立累加的时间值：

- **96 位 ToD 时间戳**：模拟现实世界里的「几点几分几秒」，结构是「秒 + 纳秒 + 小数纳秒」。秒数每过 \(10^9\,\text{ns}\)（即 1 秒）自增一次，纳秒在 \(0 \sim 999\,999\,999\) 之间回卷。这是 IEEE 1588 标准时间戳格式。
- **64 位相对时间戳**：一个不带「秒」字段的自由计数器，结构是「纳秒 + 小数纳秒」，永不进秒、永不回卷，单调累加。它适合表达「距离某个起点过了多少纳秒」，在截断位宽、跨域传递时比 96 位更紧凑。

两者**共用同一套每拍步长和微调逻辑**，只是 96 位多了「进秒回卷」机制。它们的差不是 bug，而是为不同下游用途（绝对时间 vs 相对计时）各备一份。

#### 4.1.2 核心流程

每个时钟上升沿，模块把一个「步长增量」同时加到两个累加器上：

```
每拍：
  inc = period + (adj 激活? adj : 0) + (drift 到期? drift : 0)
  ts_64  += inc            # 64 位：直接累加，不回卷
  ts_96  += inc            # 96 位：累加后若达到 1e9 ns 则「进秒 + 回卷」
```

定点小数的进位完全靠把 `{ns, fns}` 拼成一个宽整数做普通加法——fns 溢出 \(2^{16}\) 自然进到 ns，无需特殊处理。

#### 4.1.3 源码精读

模块参数与端口集中定义了所有可调维度。[rtl/ptp_clock.v:34-47](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock.v#L34-L47) 是参数：其中 `PERIOD_NS`/`PERIOD_FNS` 设默认步长，`DRIFT_*` 设默认漂移，`FNS_WIDTH` 设小数纳秒位宽，`PIPELINE_OUTPUT` 可在输出端加寄存器级以改善时序。

[rtl/ptp_clock.v:48-95](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock.v#L48-L95) 是端口：除了 `clk`/`rst`、两组时间戳加载口、三个微调口，输出端给出 `output_ts_96`、`output_ts_64`、`output_ts_step`（时间跳变脉冲）、`output_pps`（每秒脉冲）。

两个累加器的内部寄存器在 [rtl/ptp_clock.v:120-129](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock.v#L120-L129) 声明：96 位拆成 `ts_96_s_reg[47:0]`（秒）、`ts_96_ns_reg[29:0]`（纳秒，30 位足以容纳 \(10^9\)）、`ts_96_fns_reg`（小数纳秒）；64 位拆成 `ts_64_ns_reg[47:0]`（纳秒）、`ts_64_fns_reg`（小数纳秒）。96 位里多出的 `ts_96_ns_inc_reg/ts_96_fns_inc_reg/ts_96_ns_ovf_reg/ts_96_fns_ovf_reg` 是为「进秒回卷」做的预计算寄存器（4.3 详述）。

两个时间戳如何拼成输出总线，见 [rtl/ptp_clock.v:200-208](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock.v#L200-L208)（`PIPELINE_OUTPUT=0` 的组合输出分支）：

```verilog
assign output_ts_96[95:48] = ts_96_s_reg;          // 48 位秒
assign output_ts_96[47:46] = 2'b00;                // 2 位保留
assign output_ts_96[45:16] = ts_96_ns_reg;         // 30 位纳秒
assign output_ts_96[15:0]  = {ts_96_fns_reg, 16'd0} >> FNS_WIDTH;   // 16 位小数纳秒
assign output_ts_64[63:16] = ts_64_ns_reg;         // 48 位纳秒
assign output_ts_64[15:0]  = {ts_64_fns_reg, 16'd0} >> FNS_WIDTH;   // 16 位小数纳秒
```

可以看到 96 位总线是 `[秒(48) | 保留(2) | 纳秒(30) | 小数纳秒(16)]`，64 位总线是 `[纳秒(48) | 小数纳秒(16)]`。末尾那行 `>> FNS_WIDTH` 的移位是为了在 `FNS_WIDTH` 恰好为 16 时退化为直通、而在位宽不同时把 fns 缩放到统一的 16 位线上格式（对齐 IEEE 1588 的 16 位小数纳秒约定）。

默认步长对应时钟频率：`PERIOD_NS=6`、`PERIOD_FNS=0x6666=26214`，所以每拍步长

\[
T = 6 + \frac{26214}{2^{16}} \approx 6.4\,\text{ns},\qquad f = \frac{1}{T} \approx 156.25\,\text{MHz}
\]

这正是 10G/25G 以太网的参考时钟频率，cocotb 仿真里也是用 `Clock(dut.clk, 6.4, units="ns")` 来驱动它的（见 [tb/ptp_clock/test_ptp_clock.py:44](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/ptp_clock/test_ptp_clock.py#L44)）。

#### 4.1.4 代码实践

**实践目标**：直观看到 64 位与 96 位两个时间戳都在按仿真墙钟时间同步累加。

**操作步骤**：

1. 配好 cocotb + iverilog（见第 1 单元 u1-l4），进入 `tb/ptp_clock` 目录运行 `make`。
2. 重点看用例 `run_default_rate`（[tb/ptp_clock/test_ptp_clock.py:77-114](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/ptp_clock/test_ptp_clock.py#L77-L114)）：它在复位后记录起始的仿真时间与两个时间戳，空跑 10000 拍，再记录终值。
3. 用例把 `output_ts_96` 折算回秒（`(ts>>48) + (低48位/2^16)*1e-9`），把 `output_ts_64` 折算成秒（`ts/2^16*1e-9`），与仿真时间增量比较。

**需要观察的现象**：日志会打印 `sim time delta`、`96 bit ts delta`、`64 bit ts delta` 三个值。

**预期结果**：三者近似相等，且 `96 bit ts diff`、`64 bit ts diff` 都在 \(10^{-12}\,\text{s}\) 量级以内（断言 `assert abs(ts_diff) < 1e-12`）。这证明两个累加器都忠实反映了「每拍 +6.4 ns」。

> 说明：本实践是「跑现成用例 + 读断言」，命令结果依赖你本机的工具链版本，若断言数值不通过请优先核对 iverilog/cocotb 版本（`tox.ini` 锁定了可复现版本）。

#### 4.1.5 小练习与答案

**练习 1**：若把 `PERIOD_FNS` 从 `0x6666` 改成 `0x0000`（`PERIOD_NS` 仍为 6），时钟频率会变成多少？

**答案**：步长变为 \(6\,\text{ns}\)，频率 \(1/6\,\text{ns} \approx 166.67\,\text{MHz}\)。

**练习 2**：96 位时间戳的纳秒字段是 30 位，为什么不是 32 位？

**答案**：因为它只需存放 \(0 \sim 999\,999\,999\)（一秒内的纳秒），\(2^{30}\approx 1.07\times10^9\) 已足够且更省位宽；进秒逻辑会保证它不越过 \(10^9\)。30 位配上 2 位保留、48 位秒、16 位 fns，正好拼成 96 位总线。

---

### 4.2 每拍步长、瞬时偏移与频率微调

#### 4.2.1 概念说明

「让时钟跑快或跑慢」本质是改变每拍加进累加器的步长 `inc`。本模块用**一个加法式**把三种调节合流：

\[
\text{inc} = \text{period} \;+\; (\text{adj 激活时})\,\text{adj} \;+\; (\text{drift 到期时})\,\text{drift}
\]

- `input_period`：直接覆盖基准步长 `period`，是**频率微调**（长期改变时钟速率）。
- `input_adj`：在连续 `count` 拍内叠加一个偏移量，是**瞬时偏移/相位微调**。它的总效果是 \(\text{count}\times\text{adj}\)，把时间整体平移一段；用 `count` 把这段平移摊薄到很多拍，是为了避免单拍增量过大、超出加法器与下游时序预算。期间会拉高 `output_ts_step` 告诉下游「时间正在被人为跳变」。

两者都既能加（时钟跑快、时间前推）也能减（时钟跑慢、时间后挪），因为相关项在加法式里按**有符号数**解释。

#### 4.2.2 核心流程

```
每拍：
  if input_period_valid : period_reg <= 输入        # 频率微调（立即换基准步长）
  if input_adj_valid    : 锁存 adj_ns/adj_fns/adj_count
  if adj_count > 0      : adj_count--; adj_active=1; step=1   # 偏移进行中
                         else adj_active=0

  inc = period + (adj_active ? adj : 0) + (drift到期 ? drift : 0)
  ts_64 += inc
  ts_96 += inc   # 进秒处理见 4.3
```

注意时序：参数先锁存进寄存器，`inc` 用的是**上一拍锁存好的**值；改 `input_period` 后要等几拍新步长才流过流水线（cocotb 用例里专门 `await RisingEdge` 几次「冲掉旧 period」就是这个原因）。

#### 4.2.3 源码精读

参数锁存与三合一加法集中在主 `always` 块里。[rtl/ptp_clock.v:218-233](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock.v#L218-L233) 依次锁存 `period`、`adj`、`drift` 三组参数（注意 drift 的锁存还被 `DRIFT_ENABLE` 参数门控）。

核心的步长计算在 [rtl/ptp_clock.v:236-238](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock.v#L236-L238)：

```verilog
{ts_inc_ns_reg, ts_inc_fns_reg} <= $signed({1'b0, period_ns_reg, period_fns_reg}) +
    (adj_active_reg ? $signed({adj_ns_reg, adj_fns_reg}) : 0) +
    ((DRIFT_ENABLE && drift_cnt == 0) ? $signed({drift_ns_reg, drift_fns_reg}) : 0);
```

这里 `period` 项前显式补了 `1'b0`（恒为正），而 `adj`/`drift` 项用 `$signed({...})` 把拼接值按有符号数解释——因此把 `adj_ns` 的最高位（`OFFSET_NS_WIDTH` 位的符号位）置 1 即可得到负偏移，让时钟跑慢。结果写入 `ts_inc_ns_reg/ts_inc_fns_reg`，再喂给两个累加器。

偏移调整的计数与激活逻辑在 [rtl/ptp_clock.v:240-247](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock.v#L240-L247)：

```verilog
if (adj_count_reg > 0) begin
    adj_count_reg <= adj_count_reg - 1;
    adj_active_reg <= 1;
    ts_step_reg <= 1;          # 偏移期间持续标 step
end else begin
    adj_active_reg <= 0;
end
```

可见 `adj_active_reg` 在整个 `count` 拍内为 1，使每拍 `inc` 都叠加一次 `adj`；同时 `ts_step_reg` 每拍置 1。`input_adj_active` 输出（[rtl/ptp_clock.v:139](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock.v#L139) 的 `assign`）直接引出该状态，供伺服查询「上次偏移是否还在进行」。

频率微调的现成验证见 `run_frequency_adjustment`（[tb/ptp_clock/test_ptp_clock.py:237-287](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/ptp_clock/test_ptp_clock.py#L237-L287)）：它把 `input_period_fns` 改成 `0x6624`，断言时间增量与仿真时间增量满足比值

\[
\frac{\text{仿真时间增量}}{\text{时间戳增量}} = \frac{6.4}{6 + (0x6624 + 2/5)/2^{16}}
\]

分母里的 `6 + (...)/2^{16}` 就是新基准步长（含默认漂移 `2/5`），证明改 `input_period` 等价于改时钟频率。

#### 4.2.4 代码实践

**实践目标**：亲手算一次瞬时偏移的总效果，再用源码验证。

**操作步骤**：

1. 阅读历史用例里的偏移设置（[tb/test_ptp_clock.py:268-271](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_ptp_clock.py#L268-L271)）：`input_adj_fns=64`、`input_adj_count=1024`。
2. 手算总偏移量：

\[
\Delta = \text{count}\times\frac{\text{adj\_fns}}{2^{16}} = 1024 \times \frac{64}{65536} = 1\,\text{ns}
\]

3. 对照其断言（[tb/test_ptp_clock.py:285-286](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_ptp_clock.py#L285-L286)）：`|Δtime − Δts + 1e-9| < 1e-12`，即时间戳比仿真时间**多走 1 ns**。

**需要观察的现象**：因为 `adj` 是正的、叠加进了 `inc`，时钟在那 1024 拍里跑得比仿真墙钟快，累计净赚 1 ns。

**预期结果**：偏移结束后 `input_adj_active` 回 0，`output_ts_step` 停止脉冲；两个时间戳都比「不调」时领先 1 ns。

> 待本地验证：当前 cocotb 版用例 `tb/ptp_clock/test_ptp_clock.py` 没有专门的 `input_adj` 场景，你可在第 5 节综合实践里自行补一个；上面的手算与断言来自历史 myhdl 用例，逻辑等价。

#### 4.2.5 小练习与答案

**练习 1**：为什么偏移要用 `count` 拍摊薄，而不是直接在一拍里把时间戳加一个大量？

**答案**：单拍大增量会冲爆 `inc` 加法器的位宽（`INC_NS_WIDTH` 是按 `PERIOD/OFFSET/DRIFT` 三宽度之和的 `clog2` 算的，见 [rtl/ptp_clock.v:97](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock.v#L97)），且对下游连续打时间戳的逻辑不友好；摊薄后每拍增量小、平滑，同时 `step` 标志仍能告知「这是一次人为调整」。

**练习 2**：要让时钟**变慢**实现负偏移，`input_adj_ns` 该怎么填？（端口声明为无符号 `[OFFSET_NS_WIDTH-1:0]`）

**答案**：因为加法式里 `adj` 按 `$signed` 解释、最高位为符号位，所以填一个 `OFFSET_NS_WIDTH` 位最高位为 1 的值（例如 4 位时填 `0x8`~`0xF` 范围）即表示负数，时钟会在这段时间内跑慢。

---

### 4.3 进秒回卷、PPS 与漂移补偿

#### 4.3.1 概念说明

本模块覆盖三个收尾机制：

- **进秒回卷**：96 位时间戳的纳秒字段不能无限涨，到 \(10^9\,\text{ns}\)（1 秒）必须回卷并把秒字段 +1。难点在于每拍都在做加法，要「提前一拍」预判下一拍是否会越过秒边界，才能在不拖慢主加法路径的前提下完成进秒。
- **PPS（Pulse Per Second）**：`output_pps` 在每次进秒的瞬间产生一个单周期脉冲，供外部电路做「整秒对齐」（如 PTP perout、PPS 指示灯）。
- **漂移补偿**：`input_drift` 每 `drift_rate` 拍叠加一次 `drift_ns/drift_fns`，用于抵消晶振长期的固定频偏，是**持续的频率微调**（与 `input_adj` 的「一次性」相对）。

#### 4.3.2 核心流程

漂移用一个简单分频计数器实现：

```
每拍：
  if drift_cnt == 0 : drift_cnt <= rate-1;  本拍 drift 生效（并入 inc）
                   else drift_cnt--
```

因此漂移的**平均每拍贡献**为

\[
d = \frac{\text{drift\_ns} + \text{drift\_fns}/2^{16}}{\text{drift\_rate}}
\]

有效步长 = `period + d`。

进秒回卷用一个「超前借位」机制：

```
预计算 margin = NS_PER_S − inc                      # 距离一秒还差多少（再扣一步长）
预计算 ovf    = 下一拍累加值 − margin                 # 若不借位(≥0)说明即将跨过一秒
if !ovf 的符号位(借位位) :                           # 预测到要进秒
    ts_96_ns <= ovf(回卷后的余数)
    ts_96_s  <= ts_96_s + 1
    置借位位，防止下一拍重复触发
pps <= !借位位                                       # 进秒那一拍 PPS 拉高
```

`output_ts_step` 在两类事件拉高：偏移进行中（见 4.2）、或用 `input_ts_*_valid` 硬加载时间戳（见下）。

#### 4.3.3 源码精读

漂移分频计数器在 [rtl/ptp_clock.v:249-254](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock.v#L249-L254)，到 0 则重装 `drift_rate_reg-1`，且「到 0 这一拍」正是 [rtl/ptp_clock.v:238](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock.v#L238) 里把 `drift` 并入 `inc` 的那一拍。

进秒回卷的核心在 [rtl/ptp_clock.v:256-271](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock.v#L256-L271)：

```verilog
{ts_inc_ns_delay_reg, ts_inc_fns_delay_reg} <= {ts_inc_ns_reg, ts_inc_fns_reg};            # 步长延迟一拍
{ts_inc_ns_ovf_reg,  ts_inc_fns_ovf_reg } <= {NS_PER_S,{FNS_WIDTH{1'b0}}} - {ts_inc_ns_reg, ts_inc_fns_reg};   # 预算 margin

{ts_96_ns_inc_reg, ts_96_fns_inc_reg} <= {ts_96_ns_inc_reg, ts_96_fns_inc_reg} + {ts_inc_ns_delay_reg, ts_inc_fns_delay_reg};  # 下一拍累加值
{ts_96_ns_ovf_reg, ts_96_fns_ovf_reg} <= {ts_96_ns_inc_reg, ts_96_fns_inc_reg} - {ts_inc_ns_ovf_reg, ts_inc_fns_ovf_reg};      # 超前借位判定
{ts_96_ns_reg, ts_96_fns_reg} <= {ts_96_ns_inc_reg, ts_96_fns_inc_reg};

if (!ts_96_ns_ovf_reg[30]) begin
    // 超前借位没有借位 => 已经过去一秒
    // 秒字段+1，纳秒回卷到余数，置位借位位防止重复
    {ts_96_ns_inc_reg, ts_96_fns_inc_reg} <= {ts_96_ns_ovf_reg, ts_96_fns_ovf_reg} + {ts_inc_ns_delay_reg, ts_inc_fns_delay_reg};
    ts_96_ns_ovf_reg[30] <= 1'b1;
    {ts_96_ns_reg, ts_96_fns_reg} <= {ts_96_ns_ovf_reg, ts_96_fns_ovf_reg};
    ts_96_s_reg <= ts_96_s_reg + 1;
end
```

关键点是借位标志 `ts_96_ns_ovf_reg[30]`：模块用它**提前一拍**判断「下一拍是否会完成一整秒」，把宽位（30+ 位）比较挪出主加法关键路径；命中进秒时秒字段 +1、纳秒回卷到 `ts_96_*_ovf_reg`（越过边界后的余数），并把该位置 1 以免下一拍重复进秒。代码注释（[rtl/ptp_clock.v:265-266](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock.v#L265-L266)）正是这个意思：「if the overflow lookahead did not borrow, one second has elapsed」。复位时该位被初值 `31'h7fffffff`（[rtl/ptp_clock.v:125](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock.v#L125)）置 1，确保上电时不会误触发一次进秒。

PPS 就是借位判定的直接副产品，见 [rtl/ptp_clock.v:293](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock.v#L293)：

```verilog
pps_reg <= !ts_96_ns_ovf_reg[30];
```

即「进秒那一拍 PPS 为 1」，单周期脉冲，与秒边界严格对齐。

64 位累加器因为没有「秒」字段，无需回卷，所以异常简洁，见 [rtl/ptp_clock.v:284-291](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock.v#L284-L291)：直接 `+= inc`，并在 `input_ts_64_valid` 时整体加载新值并置 step。

硬加载时间戳（绝对赋值）见 [rtl/ptp_clock.v:273-282](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock.v#L273-L282)：`input_ts_96_valid` 时把秒/纳秒/小数纳秒及进秒预计算寄存器一并重载，并置 `ts_step_reg`——这是 PTP 主从同步时「把本地时钟硬拉到主时钟时间」的入口。

PPS 与进秒的现成验证是 `run_seconds_increment`（[tb/ptp_clock/test_ptp_clock.py:175-234](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/ptp_clock/test_ptp_clock.py#L175-L234)）：它把时间戳加载到 \(999\,990\,000\,\text{ns}\)（离一秒只差 \(10\,\mu\text{s}\)），随后空跑，断言在 `output_pps` 拉高那一刻秒字段恰为 1、纳秒很小。漂移验证是 `run_drift_adjustment`（[tb/ptp_clock/test_ptp_clock.py:290-339](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/ptp_clock/test_ptp_clock.py#L290-L339)）：设 `drift_fns=20`、`drift_rate=5`，断言有效步长为

\[
6 + \frac{0x6666 + 20/5}{2^{16}}\,\text{ns}
\]

分母里 `20/5 = 4` 正是上面公式里的平均每拍漂移贡献。

#### 4.3.4 代码实践

**实践目标**：跑通进秒/PPS 与漂移两组现成断言，理解 PPS 时机与漂移累积。

**操作步骤**：

1. 在 `tb/ptp_clock` 下 `make TESTCASE=run_seconds_increment`（或直接 `make` 跑全部用例），重点看 `run_seconds_increment`。
2. 把时间戳加载值改成 `999_000_000 ns`（离一秒差 \(1\,\text{ms}\)），按 \(6.4\,\text{ns/拍}\) 估算大约需要多少拍会看到 PPS（约 \(10^6/6.4 \approx 156\,250\) 拍）。
3. 再跑 `run_drift_adjustment`，对比「开 drift_fns=20」与「不开 drift」两种情况下，10000 拍后时间戳增量的差值。

**需要观察的现象**：`run_seconds_increment` 中 `output_pps` 恰好在秒字段从 0 跳到 1 的那一拍为 1；`run_drift_adjustment` 中开了漂移的时间戳增量略大于不开时。

**预期结果**：PPS 与秒边界严格同步（断言通过）；10000 拍下漂移净贡献 \(= 10000 \times 4 / 2^{16} \approx 0.61\,\text{ns}\)，与手算一致。

> 说明：步骤 2 的拍数为估算，请以实际仿真波形为准；若工具链版本与 `tox.ini` 不符，断言容差可能不满足。

#### 4.3.5 小练习与答案

**练习 1**：为什么复位时 `ts_96_ns_ovf_reg[30]` 要初始化为 1？

**答案**：该位为 1 表示「借过位、尚未到秒」，初值置 1 可防止上电/复位的第一个周期被误判成「刚刚过了一秒」而触发一次假进秒与假 PPS。

**练习 2**：若 `drift_rate=0`（用户误配），分频计数器会怎样？

**答案**：[rtl/ptp_clock.v:251](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_clock.v#L251) 会重装 `drift_rate_reg-1`，若 `drift_rate=0` 则重装为全 1（16 位最大值），相当于漂移几乎永不生效；这是参数误配的退化行为，正常使用 `drift_rate` 应 ≥ 1。

**练习 3**：`output_pps` 的脉宽是几个时钟周期？为什么？

**答案**：1 个周期。因为 `pps_reg <= !ts_96_ns_ovf_reg[30]`，而进秒判定只在跨秒那一拍使借位位为 0，下一拍又被置回 1，所以 PPS 是与秒边界对齐的单拍脉冲；若下游需要更宽脉冲，应在外部展宽（`ptp_td_leaf` 等模块就提供了 stretched PPS）。

---

## 5. 综合实践

把本讲三块知识串起来：**实例化 `ptp_clock`，注入一次 `input_adj` 观察 `output_ts_step` 与时间值变化，再配置一个 `input_drift` 观察累积偏移。**

利用现成的 cocotb 平台 [tb/ptp_clock/test_ptp_clock.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/ptp_clock/test_ptp_clock.py)，仿照其中的 `TB` 类与用例风格，新增一个用例（示例代码，非项目原有）：

```python
# 示例代码：在 tb/ptp_clock/test_ptp_clock.py 中追加
@cocotb.test()
async def run_adj_and_drift(dut):
    tb = TB(dut)
    await tb.reset()
    await RisingEdge(dut.clk)

    # 1) 注入一次瞬时偏移：count=1024, fns=64 => 总共前推 1 ns
    dut.input_adj_ns.value = 0
    dut.input_adj_fns.value = 64
    dut.input_adj_count.value = 1024
    dut.input_adj_valid.value = 1
    await RisingEdge(dut.clk)
    dut.input_adj_valid.value = 0

    step_pulses = 0
    for _ in range(1024):
        await RisingEdge(dut.clk)
        if dut.output_ts_step.value.integer:
            step_pulses += 1
    # 预期：偏移期间每拍都标 step
    assert step_pulses == 1024, f"step pulses={step_pulses}"

    # 2) 配置漂移：每 5 拍加 20 fns => 平均每拍 4 fns
    dut.input_drift_ns.value = 0
    dut.input_drift_fns.value = 20
    dut.input_drift_rate.value = 5
    dut.input_drift_valid.value = 1
    await RisingEdge(dut.clk)
    dut.input_drift_valid.value = 0
    await RisingEdge(dut.clk); await RisingEdge(dut.clk); await RisingEdge(dut.clk)

    start = dut.output_ts_64.value.integer
    N = 10000
    for _ in range(N):
        await RisingEdge(dut.clk)
    stop = dut.output_ts_64.value.integer

    # 10000 拍下漂移净贡献(以 2^16 定点计)：N * drift_fns / drift_rate
    drift_added = N * 20 // 5
    measured = stop - start
    print(f"measured inc={measured}, drift_only part={drift_added} (units of 2^-16 ns)")
```

**验证要点**：

1. 第 1 步：`output_ts_step` 在偏移的 1024 拍内**每拍**为 1，结束后停止脉冲；时间戳比「不调」净多约 1 ns。
2. 第 2 步：10000 拍内，由漂移多累加的定点量 ≈ \(10000 \times 20 / 5 = 40000\) 个 \(2^{-16}\,\text{ns}\)，即约 \(0.61\,\text{ns}\)。
3. 思考：把 `input_drift_fns` 改成使最高位为 1 的值（按 `$signed` 即负值），观察时间戳增量是否**变小**（漂移用于把时钟调慢、抵消偏快的晶振）。

> 待本地验证：以上为示例用例，断言数值与实际仿真输出请在本地跑通后核对；`TB` 类的时钟周期固定为 6.4 ns（156.25 MHz）。

## 6. 本讲小结

- `ptp_clock` 是 verilog-ethernet 里自由运行的硬件 PTP 时钟，靠「每拍累加一个步长」自行走时间，不依赖任何报文。
- 它同时输出两种时间戳：**96 位 ToD**（秒+纳秒+小数纳秒，进秒回卷）与**64 位相对**（纳秒+小数纳秒，单调不回卷），两者共用步长与微调逻辑。
- 「小数纳秒（fns）」用定点小数把 `{ns, fns}` 当宽整数相加，使纯加法器具备亚纳秒分辨率；默认步长 \(6.4\,\text{ns}\) 对应 156.25 MHz。
- 三种微调各有分工：`input_period`（频率）、`input_adj`（瞬时偏移，摊到 `count` 拍并标 `step`）、`input_drift`（每 `rate` 拍叠加一次的持续漂移）。
- 96 位的进秒靠「超前借位」提前一拍预判秒边界，`output_pps` 即进秒那一拍的单周期脉冲，`output_ts_step` 在偏移或硬加载时拉高。
- 仿真走 `tb/ptp_clock/`（cocotb + iverilog），现成用例覆盖了默认速率、加载、进秒/PPS、频率微调、漂移补偿五大场景。

## 7. 下一步学习建议

本讲只讲了「时钟本身」。要把它接进真实系统，建议继续：

- **u11-l2 `ptp_clock_cdc`**：当 MAC 的 PTP 时钟与应用逻辑不在同一时钟域时，如何把这个自由时钟安全地跨域传递并去偏（deskew）。
- **u11-l3 PTP 时间戳标记与 MAC 集成**：看 `eth_mac_1g`/`eth_mac_10g` 如何在 `PTP_TS_ENABLE` 下，于帧的 SFD/帧尾把本讲的 `output_ts_*` 打进 `tuser` 或旁带总线。
- **u11-l4 PTP 时间分发**：`ptp_td_phc`/`ptp_td_leaf`/`ptp_td_rel2tod` 如何把一个主时钟串行分发给多片叶时钟，并用「共享小数纳秒」从截断的相对时间戳还原 96 位 ToD。
- **u11-l5 `ptp_perout`**：如何基于本讲的时间，按绝对起始时刻、周期、脉宽精确生成周期脉冲输出。

阅读时建议带着一个问题：**「这个下游模块到底用的是 64 位还是 96 位时间戳？为什么？」**——答案几乎都指向本讲讲过的两种格式之差。
