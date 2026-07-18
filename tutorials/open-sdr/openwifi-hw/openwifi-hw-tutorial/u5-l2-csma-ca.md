# CSMA/CA 信道接入

## 1. 本讲目标

学完本讲后，读者应该能够：

- 说清楚 802.11 DCF（分布式协调功能）中 DIFS / SIFS / Slot / 随机退避 / NAV 各自的作用，以及它们之间的时间关系。
- 在 `csma_ca.v` 源码中定位 `DIFS`、`SIFS`、`slot_time`、`preamble_sig_time` 等时序参数的来源，并能解释它们如何由软件寄存器（`slv_reg9`）或频段（`band`）决定。
- 追踪随机退避计数器 `num_slot_random`、竞争窗指数 `cw_exp_used` 如何一步步影响 `backoff_done`（「我赢得信道、可以发包了」）的产生。
- 看懂 `csma_ca` 如何以 TSF 的 1µs 脉冲 `tsf_pulse_1M` 作为统一计时心跳，用纯数字逻辑把「等 DIFS → 抽随机数 → 数 slot → 报告空闲」整套流程跑通。

本讲只聚焦「**何时被允许发射**」这一件事。至于「赢得信道之后如何真正把帧送出去、如何等 ACK、如何重传」，那是下一阶段 `tx_control.v`（u5-l3）的职责；本讲把 `backoff_done` 交给它即可。

## 2. 前置知识

在进入源码前，先用一张「时间轴」建立直觉。Wi-Fi（尤其 802.11 a/g/n）在共享信道上避免冲突的核心机制叫 **CSMA/CA**（载波监听多址接入 / 冲突避免）。它不像以太网那样「先发再说、撞了重发」，而是「先听后说」：

1. **物理载波监听（CCA）**：射频前端报告当前信道能量是否低于门限（`ch_idle`，来自 `cca.v`）。
2. **虚拟载波监听（NAV）**：从别人发的帧头里读出「我还要占用信道多久」（Duration/ID 字段），自己倒数一个 NAV 定时器，没到 0 就当作「忙」。
3. 只有 **两者都说空闲**（`ch_idle_final`）才认为信道可用。
4. 想发包的站点要先等一个 **DIFS**（DCF 帧间间隔）的「静默期」，证明信道确实稳定空闲。
5. DIFS 之内若有多人同时想发，就各抽一个 **随机退避值**（若干个 slot），像倒计时一样数；谁先数到 0 谁先发，其余人冻结、等信道再次空闲后接着数（这就是「冲突避免」）。
6. 收发之间的紧凑握手（如 DATA→ACK）用更短的 **SIFS** 间隔，保证握手帧优先级高于新接入的帧。

几个关键时间量的关系（µs）：

\[
\text{DIFS} = \text{SIFS} + 2 \times \text{slot}
\]

\[
\text{EIFS} = \text{SIFS} + \text{DIFS} + \text{ACK\_time}
\]

EIFS 比 DIFS 更长，专门用在「刚收到一帧但没解出来（FCS 错）」时——既然解不出来，就保守地多等一会儿，给真正的接收方留出回 ACK 的时间。

随机退避值的范围由 **竞争窗 CW** 决定：

\[
\text{backoff} \in [0,\; 2^{\text{cw\_exp}} - 1]
\]

`cw_exp` 是竞争窗指数，重传一次就 +1（窗口翻倍，退避更狠），成功或放弃就回到最小值 `cw_min`。这正是源码里 `cw_exp` 的来历。

> 名词速查：**DCF**（Distributed Coordination Function，分布式协调功能）、**IFS**（InterFrame Space，帧间间隔）、**DIFS/SIFS/EIFS**（三种不同长度的 IFS）、**slot**（时隙，退避计数单位）、**NAV**（Network Allocation Vector，网络分配向量，虚拟载波监听）、**CCA**（Clear Channel Assessment，信道空闲评估）、**CW**（Contention Window，竞争窗）。这些在 u5-l1 已铺垫过它们在 `xpu` 中的信号归属，本讲深入其算法实现。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ip/xpu/src/csma_ca.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v) | 本讲主角。实现 DCF 的全部逻辑：NAV 状态机、随机数生成、退避状态机、DIFS/EIFS 计时，输出 `backoff_done`。 |
| [ip/xpu/src/tsf_timer.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tsf_timer.v) | 产生 1µs 心跳脉冲 `tsf_pulse_1M`，是 `csma_ca` 所有计时器的「滴答」来源。 |
| [ip/xpu/src/xpu.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v) | 例化 `csma_ca`、`tsf_timer`、`cw_exp`，把软件寄存器（`slv_reg9/5/6/19`）翻译成时序参数与 CW 配置，并把 `backoff_done` 送给 `tx_control`。 |
| [ip/xpu/src/cw_exp.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/cw_exp.v) | 维护竞争窗指数 `cw_exp`：按队列取 `cw_min/cw_max`，重传递增、成功/放弃复位。 |
| [ip/xpu/src/cca.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/cca.v) | 物理载波监听，输出 `ch_idle`（本讲把它当输入信号理解，细节见 u5-l5）。 |
| [ip/board_def.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/board_def.v) | 定义 `COUNT_TOP_1M` 等 1µs 计数宏，是 `tsf_timer` 的计数上限来源（详见 u2-l4）。 |

## 4. 核心概念与源码讲解

### 4.1 计时基石：TSF 1µs 脉冲与可配置时序参数

#### 4.1.1 概念说明

`csma_ca` 面对的第一个问题是：**怎么在 FPGA 里得到微秒级的「闹钟」？** DIFS、SIFS、slot 都是微秒量级（9µs、10µs、16µs、20µs……），用 100MHz 的基带时钟（周期 10ns）直接数会数到上千，写起来啰嗦。openwifi 的做法是：让 `tsf_timer` 每过 1µs 产生一个单周期脉冲 `tsf_pulse_1M`，所有 DCF 计时器**只在脉冲到来时减 1**。这样 `backoff_wait_timer`、`backoff_timer`、`nav` 这些量的单位天然就是「微秒」，代码可读性高，也和 802.11 协议参数一一对应。

另一块基石是**时序参数本身从哪来**。`slot_time`、`sifs_time`、`preamble_sig_time`、`ofdm_symbol_time` 并不是写死的常量，而是由 `xpu.v` 根据软件寄存器 `slv_reg9` 和当前频段 `band` 实时算出来再喂给 `csma_ca`。这让我们能在 2.4GHz（长/短 slot）和 5GHz（OFDM）之间切换，也能在调试时手动覆盖。

#### 4.1.2 核心流程

1. 基带时钟 `s00_axi_aclk`（频率 = `NUM_CLK_PER_US` MHz）驱动 `tsf_timer` 内部一个 8 位计数器 `counter_1M`。
2. 计数到 `COUNT_TOP_1M`（= `NUM_CLK_PER_US − 1`）清零，同时在清零那一拍把 `tsf_pulse_1M` 拉高一个周期。
3. `tsf_pulse_1M` 以 1µs 周期稳定出现，作为 `csma_ca` 全部计时器的递减使能。
4. `xpu.v` 用 `slv_reg9[31]` 选择「手动覆盖」还是「按频段默认」算出 `slot_time / sifs_time / ...`，连同软件可调的提前量 `difs_advance / backoff_advance`（`slv_reg5`）一起送进 `csma_ca`。

#### 4.1.3 源码精读

先看 1µs 脉冲怎么产生。[tsf_timer.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tsf_timer.v) 的核心计数逻辑：

[tsf_timer.v:35-47](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tsf_timer.v#L35-L47) —— 当 `counter_1M` 数到 `COUNT_TOP_1M` 就归零；只要 `counter_1M==0` 就把 `tsf_pulse_1M` 拉高并让 64 位 `tsf_runtime_val` +1。于是 `tsf_pulse_1M` 恰好每 `NUM_CLK_PER_US` 个时钟（即每 1µs）闪一次。

`COUNT_TOP_1M` 的值在构建期生成，见 [board_def.v:12](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/board_def.v#L12)：`` `define COUNT_TOP_1M ((`NUM_CLK_PER_US)-1) ``。100MHz 时它等于 99，计数 0→99 共 100 拍 = 1µs；换到 200MHz/250MHz 板卡时自动变成 199/249，无需改 Verilog（这套宏机制详见 u2-l4）。

再看参数怎么从软件寄存器算出来。[xpu.v:331-335](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L331-L335) —— 用 `slv_reg9[31]` 当开关：

- 开关为 1（手动覆盖）：各时间直接取 `slv_reg9` 的固定位段；
- 开关为 0（按频段默认）：例如 `slot_time` 在 2.4GHz（`band==1`）下由 `erp_short_slot` 决定取 9（短 slot）或 20（长 slot），5GHz 取 9；`sifs_time` 取 10（2.4G）/16（5G）。这些数值与 802.11 标准一致。

`csma_ca` 在 `xpu.v` 里的例化见 [xpu.v:466-523](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L466-L523)，注意几个关键接线：时钟 `s00_axi_aclk`、复位 `s00_axi_aresetn&(~slv_reg0[6])`（写 `slv_reg0[6]=1` 可单独复位它）、`tsf_pulse_1M`、`slot_time`、`sifs_time`，以及提前量 `.difs_advance(slv_reg5[7:0])`、`.backoff_advance(slv_reg5[15:8])`。

> 「提前量」是什么？硬件从「信道真正变空闲」到 `csma_ca` 观察到 `ch_idle=1` 之间存在检测延迟。`difs_advance / backoff_advance` 让装载计时器时先减掉这个量，相当于把倒计时起点前移，从而补偿掉这段延迟，使总的退避时间准确。它是软件可调的「校准旋钮」。

#### 4.1.4 代码实践

**目标**：验证 1µs 脉冲的来源，并把 802.11 的几个标准时序值在源码里对上号。

1. 打开 `ip/xpu/src/tsf_timer.v` 与 `ip/board_def.v`，确认 `COUNT_TOP_1M` 在 100MHz 时为 99。
2. 打开 `ip/xpu/src/xpu.v` 第 331–335 行，对 5GHz（`band==0`）写一张表：

   | 参数 | band==0 默认值 | 含义 |
   | --- | --- | --- |
   | `slot_time` | 9 | OFDM 时隙 9µs |
   | `sifs_time` | 16 | 5GHz SIFS 16µs |
   | `preamble_sig_time` | 20 | 前导+SIG 的参考时长 |

3. **预期结果**：你能口头算出 5GHz 的 DIFS = SIFS + 2×slot = 16 + 18 = 34µs，并在下一节的源码里找到这个加法。若无法运行综合，标注「待本地验证」即可。

#### 4.1.5 小练习与答案

- **练习**：如果把板卡基带时钟从 100MHz 换到 200MHz，`tsf_pulse_1M` 的周期会变吗？需要改 `csma_ca.v` 吗？
- **答案**：不会变，仍然是每 1µs 一拍，因为 `COUNT_TOP_1M` 随 `NUM_CLK_PER_US` 自动变成 199；`csma_ca.v` 里所有计时器都以「`tsf_pulse_1M` 个数」为单位，与底层时钟频率解耦，所以无需改动。

---

### 4.2 DIFS/EIFS/SIFS 计时

#### 4.2.1 概念说明

「等空闲」不是一瞬间的事，而是一段固定长度的倒数。`csma_ca` 用 `backoff_wait_timer` 这个寄存器承载这段等待：它被装载成 DIFS（或 EIFS）的微秒数，然后每个 `tsf_pulse_1M` 减 1，减到 0 就算「静默期熬过了」。`csma_ca` 把 DIFS、EIFS 的表达式写成几条简单的组合/时序逻辑，本节就是把它们逐条对上 802.11 的定义。

#### 4.2.2 核心流程

- 由 `slot_time`、`sifs_time` 组合出 `sifs_time_plus_2slot`（即 DIFS 原始值）。
- `difs_time` = 开关 `difs_enable` 打开时取 DIFS，否则 0（用于实验时关闭 DIFS 机制）。
- `eifs_time` = 开关 `eifs_enable` 打开时取 `SIFS + DIFS + longest_ack_time`，否则退化为 DIFS。
- 减去提前量得到真正装载进计时器的 `difs_time_used / eifs_time_used`。
- 决策点：**上一帧是接收失败（`last_rx_fail` 且不是 14/32 字节特例）或发送失败（`last_tx_fail`）→ 用 EIFS；否则用 DIFS**。

#### 4.2.3 源码精读

DIFS 的组装在 [csma_ca.v:143-144](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L143-L144)：

```verilog
assign sifs_time_plus_2slot = {1'b0, sifs_time} + {2'd0, slot_time, 1'b0}; // SIFS + 2*slot
assign difs_time = ( difs_enable?sifs_time_plus_2slot:0 );
```

`{2'd0, slot_time, 1'b0}` 就是 `slot_time << 1` = `2×slot`。这正是 \(\text{DIFS}=\text{SIFS}+2\cdot\text{slot}\)。

EIFS 在退避主状态机里每拍重算，见 [csma_ca.v:335](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L335)：`eifs_time <= eifs_enable ? (sifs_time + difs_time + longest_ack_time) : sifs_time_plus_2slot;`，其中 `longest_ack_time=44`（见 [csma_ca.v:89](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L89)），对应「最长 ACK 帧时长」。

装载计时器时减去提前量，见 [csma_ca.v:337-338](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L337-L338)：

```verilog
eifs_time_used <= (eifs_time==0?0:(eifs_time - difs_advance));
difs_time_used <= (difs_time==0?0:(difs_time - difs_advance));
```

决定「这一轮用 DIFS 还是 EIFS」的判据在 [csma_ca.v:154](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L154)：

```verilog
assign last_rx_fail_tx_fail_flag_used =
    ( (last_rx_fail && (~rx_len_14_or_32)) || last_tx_fail );
```

即「上次接收失败（且不是 14/32 字节特例，见 [csma_ca.v:114](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L114) 注释引用的 802.11-2020 p1682）或上次发送失败」时用 EIFS，否则用 DIFS。三处使能开关 `nav_enable/difs_enable/eifs_enable` 由 `xpu.v` 暴露为软件位，见 [xpu.v:381-383](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L381-L383)（`slv_reg6` 高 3 位取反），方便实验时单独关掉某项机制。

#### 4.2.4 代码实践

**目标**：把「EIFS vs DIFS 选择」与「提前量」两条逻辑在脑中跑一遍。

1. 在 `csma_ca.v` 找到 `last_rx_fail`、`last_tx_fail` 的更新（[csma_ca.v:326-328](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L326-L328)）：`last_rx_fail` 在 FCS 错时置位，`last_tx_fail` 在 `tx_try_complete` 且 `tx_status` 全 0 时置位。
2. 假设软件设 `difs_advance = 3`，DIFS=34µs：推算 `difs_time_used = 31`，于是 `backoff_wait_timer` 从 31 开始数 31 个 `tsf_pulse_1M`。
3. **预期结果**：能说出「接收失败一次后，下一次退避会先等 EIFS 而不是 DIFS，因而等待更长」。具体数值「待本地验证」。

#### 4.2.5 小练习与答案

- **练习**：为什么 14/32 字节的坏帧例外（`rx_len_14_or_32`）会把 EIFS 降回 DIFS？
- **答案**：802.11-2020 p1682 规定，若引发 EIFS 的那个 PPDU 只含一个长度为 14 或 32 字节的 MPDU，则 EIFS = DIFS。源码用 `rx_len_14_or_32` 屏蔽掉 `last_rx_fail`，使这种特例退回普通 DIFS。
- **练习**：把 `eifs_enable` 设为 0（`slv_reg6[29]=1`）会发生什么？
- **答案**：`eifs_time` 退化成 `sifs_time_plus_2slot`（即 DIFS），即使接收失败也只等 DIFS——用于实验中关掉 EIFS 观察行为差异。

---

### 4.3 随机退避状态机与 backoff_done

#### 4.3.1 概念说明

这是 `csma_ca` 真正的「大脑」。它用一个 7 状态的有限状态机 `backoff_state` 回答：**在 DIFS 熬过之后，还要不要数随机 slot？数完了就报告 `backoff_done`。** 区分两种触发来源很重要：

- **新包/放弃重传**（`high_trigger` / `quit_retrans`）：只等 DIFS，不抽随机数（路径 `WAIT_1 → WAIT_FOR_OWN`），因为没有竞争压力。
- **重传**（`retrans_trigger`）：等完 DIFS 后还要数随机退避（路径 `WAIT_2 → RUN → WAIT_FOR_OWN`），因为重传意味着发生了冲突，需要退避避让。

随机数本身来自一个 32 位 LFSR（线性反馈移位寄存器），按当前 `cw_exp` 取低若干位得到 `num_slot_random`；它乘以 `slot_time` 再减提前量，就是 `backoff_timer` 要数的微秒数。`backoff_timer` 在信道忙时**冻结**（进入 `BACKOFF_SUSPEND`），空闲时接着数——这正是 802.11「计数器在忙时暂停」的精髓。

最终，`backoff_done = (backoff_state == BACKOFF_WAIT_FOR_OWN)`（见 [csma_ca.v:147](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L147)），这个信号被 `tx_control`（u5-l3）消费，真正启动一次发射。

#### 4.3.2 核心流程

退避状态机的全景（伪代码）：

```
IDLE  ──(有触发 & 信道空闲)──►  WAIT_1(新包/放弃) 或 WAIT_2(重传)
                                装载 backoff_wait_timer = DIFS_used 或 EIFS_used
IDLE  ──(有触发 & 信道忙)────►  CH_BUSY(等空闲)

CH_BUSY ──(信道转空闲)──► WAIT_2，重装 DIFS/EIFS

WAIT_1 ──(wait_timer 数到 0)──► WAIT_FOR_OWN   # 新包只等 DIFS
WAIT_2 ──(wait_timer==2 时推进 LFSR)──(数到 0)──► RUN，装载 backoff_timer = num_slot_random*slot - advance

RUN ──(信道空闲 & backoff_timer 数到 0)──► WAIT_FOR_OWN  # backoff_done!
RUN ──(信道忙 & timer 未到)──► SUSPEND(冻结 timer)
RUN ──(信道忙 & timer 已到)──► CH_BUSY

SUSPEND ──(信道转空闲)──► RUN(接着数)
WAIT_FOR_OWN ──(tx_bb_is_ongoing)──►  发的是 ACK? CH_BUSY : IDLE
```

随机数侧：`cw_exp` 由 `cw_exp.v` 维护——重传 +1（封顶 `cw_max`），成功/放弃/换队列复位到 `cw_min`；`num_slot_random` 是 `random_number[9:0]` 与 `random_seed` 的组合函数，按 `cw_exp` 取位宽。

#### 4.3.3 源码精读

状态定义见 [csma_ca.v:76-82](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L76-L82)（IDLE/CH_BUSY/WAIT_1/WAIT_2/RUN/SUSPEND/WAIT_FOR_OWN），`backoff_done` 的简单赋值见 [csma_ca.v:147](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L147)。

随机数发生器是一个 32 位 LFSR，[csma_ca.v:251-257](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L251-L257)：每个 `take_new_random_number` 拍移位一次，反馈多项式为

\[
b_0 \leftarrow \neg(b_{31} \oplus b_{21} \oplus b_1 \oplus b_0)
\]

注意熵源 `random_seed` 来自实时 ADC 样点 `{ddc_q[2],ddc_i[0]}`（见 `xpu.v` 例化 [xpu.v:506](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L506)），用天线上的噪声给随机数「加料」，避免多台相同设备同步退避。

`num_slot_random` 是按 `cw_exp_used` 取位的纯组合查表，[csma_ca.v:259-299](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L259-L299)。例如 `cw_exp=3` 时取 3 位得到 \( \text{num\_slot\_random} \in [0, 2^3-1]=[0,7] \)；`cw_exp=0` 时恒为 0（窗口最小，不随机）。这与 802.11 的 \(\text{backoff}\in[0,2^{e}-1]\) 完全对应。

退避计时器的装载在 `WAIT_2 → RUN` 跳转处，[csma_ca.v:421-425](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L421-L425)：

```verilog
backoff_state<=BACKOFF_RUN;
backoff_timer<=(num_slot_random==0?0:num_slot_random_times_slot_time_minus_backoff_advance);
cw_exp_log          <=cw_exp_used;
num_slot_random_log <= num_slot_random;
```

其中 `num_slot_random_times_slot_time_minus_backoff_advance = num_slot_random*slot_time - backoff_advance`（[csma_ca.v:334](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L334)）。注意 [csma_ca.v:410-414](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L410-L414) 在 `WAIT_2` 里当 `backoff_wait_timer==2` 时拉一拍 `take_new_random_number`，确保装载 `backoff_timer` 时随机数已刷新。

`RUN` 状态倒数 `backoff_timer`，[csma_ca.v:444-461](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L444-L461)：信道空闲且 `tsf_pulse_1M` 到来才减 1；减到 0 跳 `WAIT_FOR_OWN`（即拉高 `backoff_done`）；信道变忙且 timer 未到则跳 `SUSPEND`，[csma_ca.v:462-472](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L462-L472)。`SUSPEND` 里 `backoff_timer<=backoff_timer`（冻结），见 [csma_ca.v:477](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L477)，等信道空闲再回 `RUN` 接着数。

竞争窗指数由独立的 `cw_exp.v` 维护：按 `tx_queue_idx` 从 `cw_combined`（`slv_reg19`）里取每队列的 `cw_min/cw_max`（[cw_exp.v:34-57](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/cw_exp.v#L34-L57)）；`retrans_trigger` 时 `cw_exp` 递增直到 `cw_max`，`tx_try_complete/quit_retrans/换队列` 时复位到 `cw_min`（[cw_exp.v:59-75](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/cw_exp.v#L59-L75)）。`xpu.v` 再用 `cw_en` 在「动态 CW」与「软件固定 CW（`slv_reg6[19:16]`）」之间选择，见 [xpu.v:323](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L323)。

#### 4.3.4 代码实践

**目标**：从「重传触发」一路追到 `backoff_done`，看 `num_slot_random` 和 `cw_exp` 在哪几行真正起作用。

1. 在 `csma_ca.v` 第 354 行找到 `retrans_trigger==1` 分支：进入 `BACKOFF_WAIT_2`，`backoff_wait_timer` 装载 `difs_time_used`（或 EIFS）。
2. 跟到 `BACKOFF_WAIT_2`（[csma_ca.v:407-438](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L407-L438)）：`wait_timer==2` 时推进 LFSR；`wait_timer==0` 时跳 `RUN`，第 423 行用 `num_slot_random` 算出 `backoff_timer`。
3. 跟到 `RUN`（[csma_ca.v:440-473](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L440-L473)）：`backoff_timer` 数到 0 → `WAIT_FOR_OWN` → `backoff_done=1`。
4. 再打开 `cw_exp.v`：确认第 68 行 `retrans_trigger && cw_exp<cw_max` 时 `cw_exp+1`，所以重传次数越多，`num_slot_random` 的取值范围越大、退避越久。
5. **预期结果**：你能画出「`retrans_trigger` → `WAIT_2`（数 DIFS）→ `RUN`（数 `num_slot_random` 个 slot）→ `WAIT_FOR_OWN`（`backoff_done=1`）」这条链路，并指出 `cw_exp` 控制随机数位宽、`num_slot_random` 控制具体退避长度。

#### 4.3.5 小练习与答案

- **练习**：新包（`high_trigger`）和重传（`retrans_trigger`）走的状态路径有何不同？为什么？
- **答案**：新包走 `WAIT_1 → WAIT_FOR_OWN`，只等 DIFS、不数随机 slot；重传走 `WAIT_2 → RUN → WAIT_FOR_OWN`，等完 DIFS 还要数随机退避。因为重传意味着已经发生冲突，需要用随机退避降低再次冲突概率，而新包第一次接入没有冲突迹象。
- **练习**：`BACKOFF_SUSPEND` 为什么要把 `backoff_timer` 冻结而不是清零？
- **答案**：802.11 规定退避计数器在信道忙时**暂停**（保留剩余值），信道空闲后从断点继续数；若清零就等于白退避，会导致多个站点在信道刚恢复时同步发包、再次冲突。

---

### 4.4 NAV 虚拟载波监听

#### 4.4.1 概念说明

光听射频能量（CCA）不够。别人发的帧头里写着「我接下来还要占用信道多久」（Duration/ID），听到的站点应据此把自己「虚拟地」置忙一段时间，这就是 NAV。`csma_ca` 把 NAV 做成一个独立的状态机 + 一个每 µs 减 1 的倒数计数器 `nav`，并把它与 `ch_idle` 合成最终的 `ch_idle_final = ch_idle && (nav_for_mac==0)`（[csma_ca.v:146](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L146)）。这样退避状态机只需看一个 `ch_idle_final`，就同时考虑了物理与虚拟两种载波监听。

#### 4.4.2 核心流程

NAV 状态机随接收帧的生命周期推进：

```
收到帧头(pkt_header_valid_strobe)
   └─ 有效? ─► NAV_WAIT_FOR_DURATION  否则 ─► NAV_IDLE
NAV_WAIT_FOR_DURATION ──(FC解出 & duration<32768)──► NAV_CHECK_RA
NAV_CHECK_RA ──(不是发给我的: addr1_valid && addr1!=self)──► NAV_UPDATE
                  （发给我的则停留，不设 NAV，802.11 9.3.2.4）
NAV_UPDATE ──(FCS校验通过)──► nav_set=1, nav_new=duration(或 PS-Poll 特例) ──► NAV_IDLE
```

随后 `nav` 在 `tsf_pulse_1M` 上每 µs 减 1，减到 0 即「虚拟空闲」。另有 RTS 特例：收到 RTS 后设了短 NAV，若迟迟等不到后续 CTS/Data，超时就复位 NAV（避免被永远「卡住」）。

#### 4.4.3 源码精读

NAV 主 always 块在 [csma_ca.v:167-248](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L167-L248)，状态定义见 [csma_ca.v:84-87](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L84-L87)。`nav` 的三态更新见 [csma_ca.v:181-188](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L181-L188)：

```verilog
if (nav_reset)        nav <= 0;                       // 强制复位
else if (nav_set)     nav <= (nav_new>nav?nav_new:nav); // 只增不减(802.11规则)
else                  nav <= (nav!=0?(tsf_pulse_1M?(nav-1):nav):nav); // 每µs减1
```

注意 `nav <= (nav_new>nav?nav_new:nav)`：NAV 只能被更大的新值更新，防止后到的短帧覆盖前面正确的长 NAV。

「是否设 NAV」的关键判断在 `NAV_CHECK_RA`，[csma_ca.v:221-223](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L221-L223)：只有当帧的接收地址 `addr1` 有效且**不是**本机地址（即「不是发给我的」）时才进入 `NAV_UPDATE`——发给我的帧，其 Duration 描述的是我自己的后续交互，不需要为它设 NAV（标准 9.3.2.4）。`NAV_UPDATE` 里 `nav_new` 的取值见 [csma_ca.v:228-232](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L228-L232)：PS-Poll 帧取 `ackcts_time+sifs_time`，其余取 Duration 字段。RTS 特例与超时复位见 [csma_ca.v:234-242](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L234-L242) 与 [csma_ca.v:202-204](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L202-L204)。

最终 `nav` 是否影响信道判定由 `nav_enable` 控制：`nav_for_mac = nav_enable?nav:0`（[csma_ca.v:141](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L141)），`nav_enable=~slv_reg6[31]`（[xpu.v:381](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L381)）。

#### 4.4.4 代码实践

**目标**：理解 NAV 如何与 CCA 合成 `ch_idle_final`，并验证「发给我的帧不设 NAV」。

1. 读 [csma_ca.v:146](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L146) 与 `ch_idle` 来源 [cca.v:61](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/cca.v#L61)（CCA 细节见 u5-l5）。确认 `ch_idle_final` 同时要求射频空闲且 NAV=0。
2. 跟踪 `NAV_CHECK_RA`：构造场景「收到一个 Duration=100、addr1 不是本机」的帧，预测 `nav` 会被设为 100，随后每 µs 减 1；若 addr1 是本机，则 `nav` 保持不变。
3. **预期结果**：能说出「NAV 是软件可关的（`slv_reg6[31]`），关掉后 `ch_idle_final` 只看物理 CCA」。数值「待本地验证」。

#### 4.4.5 小练习与答案

- **练习**：为什么 NAV 更新要用「只取更大值」`nav<=（nav_new>nav?nav_new:nav)`？
- **答案**：环境中可能先后收到多个帧的 Duration，较短的那个不应覆盖较长的、尚未数完的 NAV，否则会过早地认为信道空闲而造成冲突。标准要求 NAV 取当前剩余值与新值中的较大者。
- **练习**：`pkt_header_valid_strobe` 在 NAV 状态机里起什么作用？
- **答案**：它既是新帧开始的处理触发，也充当 NAV 状态机的「兜底复位」——万一 `openofdm_rx` 异常导致 FCS strobe 永不出现，状态机不会卡死在中间态，下个帧头一到就重新开始（见 [csma_ca.v:192](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L192) 注释）。

---

## 5. 综合实践

把本讲四个最小模块串起来，做一次「**端到端跟踪一次重传接入**」的源码阅读：

1. **起点**：假设 `tx_control` 因上一帧没收到 ACK 而发出 `retrans_trigger`（同时 `cw_exp.v` 已把 `cw_exp` 从 3 升到 4）。
2. **第一段等待**：在 `csma_ca.v` 的 `IDLE` 状态（[csma_ca.v:354-362](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L354-L362)），因 `last_tx_fail` 用 EIFS 装载 `backoff_wait_timer`，进入 `WAIT_2`。
3. **抽随机数**：`WAIT_2` 倒数到 2 时推进 LFSR（[csma_ca.v:410-414](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L410-L414)），倒数到 0 时按 `cw_exp=4` 从 `num_slot_random` 查表得到一个 0..15 的值，算出 `backoff_timer`，进 `RUN`（[csma_ca.v:421-425](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L421-L425)）。
4. **数 slot**：`RUN` 里每 `tsf_pulse_1M` 减 1；若期间 `ch_idle_final` 因别人的帧变 0（注意 NAV 状态机可能正让 `nav_for_mac≠0`），进 `SUSPEND` 冻结（[csma_ca.v:475-502](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L475-L502)），空闲后回 `RUN` 接着数。
5. **终点**：`backoff_timer` 数到 0 → `WAIT_FOR_OWN` → `backoff_done=1`（[csma_ca.v:147](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L147)），交给 `tx_control` 启动发射。

**产出要求**：画一张时序图，横轴为 `tsf_pulse_1M` 的 µs 计数，画出 `backoff_state`、`backoff_wait_timer`、`backoff_timer`、`ch_idle_final`、`nav`、`backoff_done` 这几条线在一次「EIFS 等待 + 随机退避 5 个 slot + 中途信道忙冻结 3µs」过程中的变化。如果手头有 Vivado 工程，可用 `XPU_ENABLE_DBG` 宏（u7-l2、u7-l6）把这些信号挂上 ILA 实测验证；否则在时序图上标注「待本地验证」的关键时间点。

## 6. 本讲小结

- `csma_ca.v` 用 `tsf_timer` 产生的 1µs 脉冲 `tsf_pulse_1M` 作为所有 DCF 计时器（DIFS/EIFS/backoff/NAV）的统一滴答，使时间单位天然是微秒，与 802.11 协议参数一一对应。
- DIFS = SIFS + 2×slot，EIFS = SIFS + DIFS + longest_ack_time（44）；用 `last_rx_fail/last_tx_fail` 决定本轮等 EIFS 还是 DIFS，并用软件可调的 `difs_advance/backoff_advance` 补偿检测延迟。
- 退避状态机区分两条路径：新包/放弃只等 DIFS（`WAIT_1`），重传额外数随机 slot（`WAIT_2→RUN`）；`RUN` 中信道忙时进 `SUSPEND` 冻结计数器，这是 802.11「忙时暂停」的关键。
- 随机退避值 `num_slot_random` 由 32 位 LFSR（以 ADC 噪声为种子）按 `cw_exp` 取位得到，范围 \([0, 2^{\text{cw\_exp}}-1]\)；`cw_exp` 由 `cw_exp.v` 维护，重传递增、成功/放弃复位。
- `backoff_done = (backoff_state==BACKOFF_WAIT_FOR_OWN)` 是本模块唯一对外「可以发包了」的信号，交给 `tx_control`（u5-l3）消费。
- NAV 虚拟载波监听用独立状态机解析接收帧 Duration/ID，每 µs 减 1，与物理 CCA 合成 `ch_idle_final`；NAV 只增不减、不是发给本机的帧才设 NAV，并保留 `nav_enable` 软件开关。

## 7. 下一步学习建议

- **u5-l3（TX 控制、重传与 ACK）**：本讲的 `backoff_done` 一旦拉高，`tx_control` 就接手——它会管理「SIFS 后回 ACK」「等 ACK 超时」「决定是否再次 `retrans_trigger`」等流程，并反过来给 `csma_ca` 送 `retrans_trigger/quit_retrans`。两讲合起来才是完整的低层 MAC 接入闭环。
- **u5-l4（TSF 与接收包解析/过滤）**：深入 `tsf_timer.v` 的 64 位定时与 `phy_rx_parse.v` 如何从字节流解析出 FC/addr1/duration——后者正是本讲 NAV 与 `NAV_CHECK_RA` 所依赖的输入。
- **u5-l5（CCA、RSSI 与 SPI）**：本讲一直把 `ch_idle` 当输入，u5-l5 会拆开 `cca.v` / `rssi.v`，讲清「能量低于门限且未在解调」是如何被判定的。
- 建议在阅读 u5-l3 前，先在 `csma_ca.v` 里自行定位 `tx_status[79:16]==0`（[csma_ca.v:328](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L328)）这一句，思考它如何把「发送结果」翻译成 `last_tx_fail`，作为衔接两讲的桥梁。
