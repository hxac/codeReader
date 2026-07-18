# dot11 顶层状态机

## 1. 本讲目标

本讲是「控制平面」单元的第一篇。前面 u1-l5 给出了解码流水线的「数据平面导航图」（`power_trigger → sync_short → sync_long → equalizer → ofdm_decoder → crc32`），u3-l5 讲清了 `ofdm_decoder` 内部五级子流水线。但整条流水线「什么时候启动、什么时候切换、什么时候报错退出」是由 `dot11.v` 顶层的一个 **15 状态有限状态机（FSM）** 统一调度的。

学完本讲你应该能够：

- 记住主流程状态链 `S_WAIT_POWER_TRIGGER → S_SYNC_SHORT → S_SYNC_LONG → S_DECODE_SIGNAL → S_CHECK_SIGNAL`，并说清每个状态的进入与退出条件。
- 看懂在 `S_CHECK_SIGNAL` 处如何用 `legacy_rate == 4'b1011`（6 Mbps）这一判定把数据流 **分叉** 到 legacy DATA 分支或 HT 检测分支（`S_DETECT_HT`），并在 `S_DECODE_DATA` 处 **汇合**。
- 理解每个状态如何用 `enable`（区间 gating）与 `reset`（单拍脉冲）两种信号调度各子模块，以及错误时如何通过 `status_code` 与 `E_*` 错误状态恢复。
- 依据 `common_params.v` 的状态定义，独立画出一张覆盖 legacy 与 HT 两条路径的完整状态转移图。

## 2. 前置知识

在进入状态机之前，先用三段话对齐认知（细节都在前置讲义里）：

- **数据平面 vs 控制平面**：`dot11.v` 顶层几乎不写算法，它把算法都封装在子模块里，自己只做两件事——一是用一连串 `xxx_inst` 例化把数据从 `sample_in` 接到 `byte_out`（数据平面，见 u1-l5）；二是用一个 FSM 决定每个子模块何时复位、何时使能、何时把上一级数据放进下一级（控制平面，本讲主题）。
- **SIGNAL 字段的作用**：802.11a/g 紧跟长前导（LTS）之后的第一个 OFDM 符号是 24 bit 的 SIGNAL 字段，携带本包的 `rate`（速率/调制方式）和 `length`（PSDU 字节数）。收端必须先解出它，才知道「后面还有几个数据符号、用什么方式解」。HT-mixed 模式为了让 legacy 设备退避，把这个 L-SIG 的速率恒定写成 6 Mbps（见 `sig.rst`）。
- **HT-mixed 的判定依据**：legacy SIGNAL 用 BPSK 调在 I 路，而 HT-SIG 用 BPSK 调在 Q 路（星座点旋转 90°）。所以解完 L-SIGNAL 后，若速率是 6 Mbps，就再看下一个符号的星座点是不是「Q 大于 I」——够多就是 HT 包，否则就是普通的 6 Mbps legacy 包（见 `sig.rst` 的 HT-SIG 一节）。

> 提示：本讲只讲「状态机怎么转」，SIGNAL 各位域的精确拼装、`(legacy_len+3)<<4` 的长度推导、HT-SIG 的 CRC-8 多项式分别由 u4-l2、u4-l3 展开。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `verilog/dot11.v` | 顶层模块，FSM 主体 | 第 403–828 行的 `always` 块：复位入口 + `case(state)` 的 15 个状态分支 |
| `verilog/common_params.v` | 全局参数与状态码定义 | 第 27–41 行的 15 个 `S_*` 状态码、第 48–68 行的 `E_*` 错误码、第 71 行的 `EXPECTED_FCS` |
| `docs/source/sig.rst` | SIGNAL / HT-SIG 字段文档 | 说明 parity/rsvd/tail 校验、HT-mixed 的 6 Mbps 约定、HT-SIG 的 Q 路 BPSK 判定 |
| `verilog/rate_to_idx.v` | 速率→索引查表（旁证） | 用来印证 `4'b1011` 就是 6 Mbps 的 L-SIG 编码 |

永久链接基准（当前 HEAD）：`https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/`

## 4. 核心概念与源码讲解

### 4.1 状态机骨架：15 个状态与状态码

#### 4.1.1 概念说明

`dot11` 的控制核心是一个显式编码的 FSM：一个 `state` 寄存器（4 bit，见 [verilog/dot11.v:32](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L32)）在每个时钟沿根据「当前状态 + 输入信号」跳到下一个状态。之所以要把控制逻辑做成集中式 FSM，是因为解码流水线是 **严格顺序、一次性** 的——一个包来了必须依次走完「检测→同步→解 SIGNAL→（分叉）→解 DATA→校验 FCS」，任何一步失败都要立刻放弃当前包、回到等 trigger 的状态，不能把半成品喂给下游。

FSM 还顺带维护一个 `status_code`（4 bit）作为 **侧信道**：正常时为 `E_OK`，出错时写入对应的 `E_*`，最终配合 `fcs_ok` 一起输出，告诉上层「这个包解到哪一步、为什么失败」。

#### 4.1.2 核心流程

15 个状态可以分成 5 组：

| 组 | 状态（id） | 作用 |
| --- | --- | --- |
| ① 等待/检测 | `S_WAIT_POWER_TRIGGER`(0) | 空闲态，等 `power_trigger` 拉高 |
| ② 前端同步 | `S_SYNC_SHORT`(1)、`S_SYNC_LONG`(2) | 短/长前导同步，定位 FFT 窗口 |
| ③ SIGNAL 解码 | `S_DECODE_SIGNAL`(3)、`S_CHECK_SIGNAL`(4) | 解出并校验 24 bit legacy SIGNAL |
| ④ HT 分支 | `S_DETECT_HT`(5)、`S_HT_SIGNAL`(6)、`S_CHECK_HT_SIG_CRC`(7)、`S_CHECK_HT_SIG`(8)、`S_HT_STS`(9)、`S_HT_LTS`(10) | 仅当速率=6 Mbps 时进入，解 HT-SIG 并跳过 HT 训练字段 |
| ⑤ DATA 收尾 | `S_DECODE_DATA`(11)、`S_DECODE_DONE`(14) | legacy/HT 汇合后解数据 + FCS 比对 |
| ⑥ 错误恢复 | `S_SIGNAL_ERROR`(12)、`S_HT_SIG_ERROR`(13) | 一拍后回到 `S_WAIT_POWER_TRIGGER` |

整体形状像一个「漏斗 + 分叉 + 汇合」：前端同步收口到 `S_DECODE_SIGNAL`，在 `S_CHECK_SIGNAL` 处一分为二（legacy 直下 `S_DECODE_DATA`；HT 走 ④ 后也下 `S_DECODE_DATA`），最后在 `S_DECODE_DONE` 收口回空闲。

#### 4.1.3 源码精读

状态码全部定义在 `common_params.v`，是一段连续的 `localparam`：[verilog/common_params.v:27-41](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L27-L41) — 15 个状态从 0 编到 14。

```verilog
localparam S_WAIT_POWER_TRIGGER =   0;
localparam S_SYNC_SHORT =           1;
...
localparam S_DECODE_DATA =          11;
localparam S_SIGNAL_ERROR =         12;
localparam S_HT_SIG_ERROR =         13;
localparam S_DECODE_DONE =          14;
```

状态码（错误码）定义在 [verilog/common_params.v:48-68](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L48-L68)，并有一句关键注释「same value may have different meaning depend on the state」——同一个数值（如 1）在不同上下文里含义不同：

- SIGNAL 阶段：`E_PARITY_FAIL=1`、`E_WRONG_RSVD=3`、`E_WRONG_TAIL=4`
- HT-SIG 阶段：`E_UNSUPPORTED_MCS=1`、`E_UNSUPPORTED_CBW=2` … `E_WRONG_CRC=9`
- DATA 收尾：`E_WRONG_FCS=1`

因此 `status_code` 必须 **和 `state` 一起看** 才能解释。复位入口在 [verilog/dot11.v:404-407](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L404-L407)：`reset` 有效时把 `status_code` 清成 `E_OK`、`state` 拉回 `S_WAIT_POWER_TRIGGER`，这正是上电或异常后的唯一入口。

#### 4.1.4 代码实践

**实践目标**：建立 15 个状态的「速查表」。

1. 打开 `verilog/common_params.v` 第 27–41 行。
2. 用编辑器或纸笔，把每个 `S_*` 抄成一张三列表：`id | 名字 | 一句话职责`（职责可以暂时照本讲 4.1.2 的表格抄）。
3. 再打开第 48–68 行，把所有 `E_*` 按「SIGNAL 类 / HT-SIG 类 / FCS 类」分组，标出哪些数值是重复的（例如 1 出现了三次）。

**需要观察的现象**：状态 id 从 0 连续到 14，刚好塞进 4 bit（`state` 声明为 `[3:0]`）；错误码最大用到 9，也塞进 4 bit `status_code`。

**预期结果**：你应得到 15 行状态表 + 3 组错误码，并能指出数值 1 在三种上下文下分别表示 `E_PARITY_FAIL`、`E_UNSUPPORTED_MCS`、`E_WRONG_FCS`。

#### 4.1.5 小练习与答案

- **Q1**：为什么 `state` 用 4 bit 就够？如果将来要加第 17 个状态会怎样？
  - **答**：4 bit 可表示 0–15，当前最多用到 14，足够。若加到第 17 个状态（≥16），需把 `state` 扩到 5 bit，并同步修改端口声明 [verilog/dot11.v:32](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L32)。
- **Q2**：`status_code` 为什么允许不同错误共用同一个数值？
  - **答**：因为出错时 FSM 已经处在不同的错误状态（如 `S_SIGNAL_ERROR` vs `S_HT_SIG_ERROR` vs `S_DECODE_DONE`），上层可以 **联合 `state`/`status_code`** 解释，省下独立编码所需的位宽。

---

### 4.2 前端主链路：从功率触发到 SIGNAL 拼装

#### 4.2.1 概念说明

前三个工作状态解决「在采样流里截出一个包、对齐到符号边界、解出第一个符号（SIGNAL）」。这条链路与 u2 单元讲的检测/同步算法一一对应，本讲只关心 **FSM 视角下** 的进入/退出条件和它发出的控制信号。核心是两种控制信号：

- **`enable`（区间 gating）**：一个电平信号，拉高表示「这个模块现在归属当前状态，允许工作」。
- **`reset`（单拍脉冲）**：在状态切换的边界上打一拍，把子模块内部寄存器清零，避免上一个包的残留污染本次解码。

#### 4.2.2 核心流程

```
S_WAIT_POWER_TRIGGER
   │  power_trigger↑
   ▼  (打 sync_short_reset 脉冲)
S_SYNC_SHORT
   │  short_preamble_detected↑ ──► 打 sync_long_reset 脉冲、sync_long_enable=1
   │  power_trigger↓ 或超时 ──────► 回 S_WAIT_POWER_TRIGGER
   ▼
S_SYNC_LONG
   │  long_preamble_detected↑ ──► equalizer/ofdm 使能+复位, num_bits_to_decode=48
   │  sample_count>320 或 power_trigger↓ ──► 回 S_WAIT_POWER_TRIGGER（超时保护）
   ▼
S_DECODE_SIGNAL  (把 equalizer 输出喂给 ofdm_decoder，逐字节移入 signal_bits)
   │  收满 3 字节(byte_count==3)
   ▼
S_CHECK_SIGNAL  (见 4.3)
```

#### 4.2.3 源码精读

**S_WAIT_POWER_TRIGGER** 在 [verilog/dot11.v:464-476](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L464-L476)：先把三个下游模块的 enable 都拉低（空闲省功耗），一旦 `power_trigger` 有效就给 `sync_short` 打一个复位脉冲并切到短同步：

```verilog
if (power_trigger) begin
    sync_short_reset <= 1;      // 单拍脉冲：进 S_SYNC_SHORT 后第一拍自清
    state <= S_SYNC_SHORT;
end
```

**S_SYNC_SHORT** 在 [verilog/dot11.v:478-498](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L478-L498)：注意它有两条退出路径——检测到短前导就 **前进**，功率中途掉下来就 **回退**。前进时给 `sync_long` 打复位脉冲并拉高 `sync_long_enable`。

**S_SYNC_LONG** 在 [verilog/dot11.v:500-530](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L500-L530)：这里出现一个超时保护 `sample_count > 320`——若进入长同步后 320+ 个样本还没找到 LTS 尖峰，就认定同步失败、回空闲。成功检出 LTS 时一次性配好 SIGNAL 解码所需的全部参数：

```verilog
if (long_preamble_detected) begin
    do_descramble       <= 0;        // SIGNAL 字段不扰码
    num_bits_to_decode  <= 48;       // 24 bit SIGNAL × 2(1/2 码率) = 48 编码 bit
    ofdm_reset <= 1; ofdm_enable <= 1;
    equalizer_enable <= 1; equalizer_reset <= 1;
    state <= S_DECODE_SIGNAL;
end
```

**S_DECODE_SIGNAL** 在 [verilog/dot11.v:532-561](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L532-L561)：把 `equalizer_out` 接到 `ofdm_decoder` 的输入，并用一个移位寄存器 `signal_bits` 把 `byte_out` 逐字节拼起来——新字节进高位、旧内容右移 8 位：

```verilog
if (byte_out_strobe) begin
    signal_bits <= {byte_out, signal_bits[23:8]};  // 先到的字节最终落到低位
    byte_count  <= byte_count + 1;
end
if (byte_count == 3) state <= S_CHECK_SIGNAL;       // 3 字节 = 24 bit SIGNAL 凑齐
```

关于 `enable` vs `reset` 的调度差异，对比两处声明：`sync_short_enable` 是 **wire**（组合逻辑，[verilog/dot11.v:165](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L165) `= state == S_SYNC_SHORT`），而 `sync_long_enable` 是 **reg**（[verilog/dot11.v:166](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L166)），因为 `sync_long` 要在 `S_SYNC_LONG` **和** 后续 HT 分支的 `S_HT_STS/S_HT_LTS` 里都保持工作，用一个跨状态的 reg 更方便。复位脉冲则统一遵循「置 1 → 下一状态首拍自清零」的模式。

#### 4.2.4 代码实践

**实践目标**：看清「复位脉冲自清零」模式和超时保护。

1. 在 [verilog/dot11.v:500-503](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L500-L503) 处找到 `sync_long_reset` 的自清逻辑。
2. 在 `S_SYNC_LONG` 的 `sample_count > 320` 分支（[verilog/dot11.v:508-510](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L508-L510)）打思维断点：如果故意把 320 改成 60，会发生什么？
3. 用 gtkwave 打开一个 dot11a 样本的波形，量一下从 `long_preamble_detected` 跳变到 `state == S_DECODE_SIGNAL` 经过了几拍。

**需要观察的现象**：`sync_long_reset` 只在进入 `S_SYNC_LONG` 的那一拍为 1，下一拍立刻被同一段代码清成 0；`state` 在检出 LTS 的下一拍就变成 3（`S_DECODE_SIGNAL`）。

**预期结果**：复位脉冲宽度恒为 1 个时钟周期；若把超时阈值改太小，正常包也会在 LTS 还没出现时被误判失败、提前回 `S_WAIT_POWER_TRIGGER`（待本地验证）。

#### 4.2.5 小练习与答案

- **Q1**：为什么 `sync_short_enable` 用 wire、`sync_long_enable` 用 reg？
  - **答**：`sync_short` 只在单一的 `S_SYNC_SHORT` 状态工作，直接 `state == S_SYNC_SHORT` 组合判定即可；`sync_long` 要在 `S_SYNC_LONG` 及 HT 分支后续多个状态里持续工作，用 reg 在进入时置 1、在 `S_WAIT_POWER_TRIGGER` 置 0，比组合判定更省事也更易读。
- **Q2**：`num_bits_to_decode <= 48` 里的 48 是怎么来的？
  - **答**：SIGNAL 字段 24 bit，经 1/2 卷积编码后变成 48 个编码 bit，正好填满一个 OFDM 符号的 48 个数据子载波，所以喂给 Viterbi 的比特预算是 48。

---

### 4.3 SIGNAL 校验与 legacy/HT 分叉（重点）

#### 4.3.1 概念说明

`S_CHECK_SIGNAL` 是整个 FSM 的 **关键分叉点**。解出来的 24 bit SIGNAL 必须先通过三项合法性校验（parity、保留位、尾比特），任何一项不过就立刻判错退出。校验通过后，再用 `legacy_rate` 决定走哪条路：

- 若 `legacy_rate == 4'b1011`（即 6 Mbps，HT-mixed 包的 L-SIG 标记）→ 进入 `S_DETECT_HT`，去确认后面是不是真的 HT-SIG。
- 否则 → 这是一个真正的 legacy 802.11a/g 包，直接配好长度参数下到 `S_DECODE_DATA`。

`S_DETECT_HT` 的判定依据来自 `sig.rst`：HT-SIG 把 BPSK 调在 Q 路而非 I 路（相当于整体旋转 90°）。所以 FSM 数「均衡器输出里有多少个样本是 |Q|>|I|」，够 4 个就认定是 HT-SIG；反之若「正常朝向」的样本超过 4 个，就说明这其实是个 6 Mbps 的 legacy 数据符号，按 legacy 数据继续解。

#### 4.3.2 核心流程

```
S_CHECK_SIGNAL
   │  parity/rsvd/tail 任一不过 ──► S_SIGNAL_ERROR (status_code=E_*) ──► 回空闲
   │  三项全过：
   ├── legacy_rate != 4'b1011 ──► 配 num_bits_to_decode=(len+3)<<4, do_descramble=1
   │                              ──► S_DECODE_DATA   (legacy 分支)
   └── legacy_rate == 4'b1011 ──► S_DETECT_HT
                                   │  数 |Q|>|I| 样本
                                   ├── rot_eq_count >= 4  ──► S_HT_SIGNAL  (确认 HT)
                                   └── normal_eq_count > 4 ──► S_DECODE_DATA (误判,按 legacy)
```

#### 4.3.3 源码精读

校验与分叉在 [verilog/dot11.v:563-599](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L563-L599)。三项校验任一失败都写入对应 `E_*` 并跳 `S_SIGNAL_ERROR`：

```verilog
if (~legacy_sig_parity_ok)      begin status_code <= E_PARITY_FAIL; ... end
else if (legacy_sig_rsvd)       begin status_code <= E_WRONG_RSVD;  ... end
else if (|legacy_sig_tail)      begin status_code <= E_WRONG_TAIL;  ... end
else begin
    legacy_sig_stb <= 1; status_code <= E_OK;
    if (legacy_rate == 4'b1011) state <= S_DETECT_HT;   // 6 Mbps → 可能是 HT
    else begin
        num_bits_to_decode <= (legacy_len+3)<<4;        // legacy 长度换算
        do_descramble <= 1; pkt_ht <= 0; pkt_begin <= 1;
        state <= S_DECODE_DATA;
    end
end
```

parity 的计算用了一个很精巧的缩约运算（[verilog/dot11.v:206](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L206)）：`legacy_sig_parity_ok = ~^signal_bits[17:0]`——`^` 是 XOR 缩约，对前 18 bit（17 位 rate+len+rsvd + 1 位 parity）做偶校验，结果取反映映「8 位的 1 总数为偶」。`sig.rst` 第 25 行明确写道「Bit 17 is a even parity bit of the previous 17 bits」。

**关于 `4'b1011` 为什么是 6 Mbps**：SIGNAL 的 4 bit 速率字段在空中是 R1 先发（R1 是最低有效位），而 `bits_to_bytes` 总是最先到达的比特落 LSB（见 u3-l6）。所以标准记法下的 6 Mbps 编码 R1R2R3R4=1101，落到 `legacy_rate[3:0]={R4,R3,R2,R1}=4'b1011`。这可由 `rate_to_idx.v` 印证——它对 `{rate[7], rate[2:0]}` 做查表，6 Mbps 命中的是 `4'b0011`（即 rate[2:0]=3'b011，正是 4'b1011 的低 3 位）：[verilog/rate_to_idx.v:23-27](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rate_to_idx.v#L23-L27)。

HT 检测在 [verilog/dot11.v:605-636](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L605-L636)：每个 `equalizer_out_strobe` 取 I/Q 绝对值比较，累加两个计数器：

```verilog
abs_eq_i <= eq_out_i[15]? ~eq_out_i+1: eq_out_i;   // 补码取绝对值
abs_eq_q <= eq_out_q[15]? ~eq_out_q+1: eq_out_q;
if (abs_eq_q > abs_eq_i) rot_eq_count    <= rot_eq_count + 1;     // Q>I: 像旋转过
else                      normal_eq_count <= normal_eq_count + 1; // 正常朝向
...
if (rot_eq_count >= 4)        state <= S_HT_SIGNAL;   // 确认 HT-SIG
else if (normal_eq_count > 4) state <= S_DECODE_DATA; // 其实是 6Mbps legacy 数据
```

#### 4.3.4 代码实践

**实践目标**：验证 `4'b1011 == 6 Mbps`，并理解 `rot_eq_count >= 4` 的阈值。

1. 打开 `verilog/rate_to_idx.v` 第 23–55 行，把 8 个 legacy 速率的 `{rate[7],rate[2:0]}` 查表值抄下来，反向推出每个速率对应的 `legacy_rate[3:0]`（注意 R1 在 LSB）。
2. 对照 `sig.rst` 的「HT-SIG vs SIGNAL」星座图说明（第 66–80 行），解释为什么阈值取 4 而不是更大——HT-SIG 一个符号有 48 个数据子载波，理论上应几乎全部 |Q|>|I|，4 是一个非常宽松的下限，用于尽快决策。

**需要观察的现象**：6 Mbps 的查表键是 `4'b0011`，反推 `legacy_rate[3:0]=4'b1011`；其余速率的低 3 位各不相同，所以 `rate_to_idx` 才能省掉 `rate[3]`。

**预期结果**：得到一张「速率 → R1R2R3R4 → legacy_rate[3:0]」对照表，其中 6 Mbps 行的 `legacy_rate` 正好是 `4'b1011`，与本状态的分支条件一致。

#### 4.3.5 小练习与答案

- **Q1**：`legacy_sig_parity_ok = ~^signal_bits[17:0]` 中，为什么对 18 bit 而不是 17 bit 做缩约？
  - **答**：第 17 bit 本身就是前 17 bit 的偶校验位，把它一起纳入缩约后，正确的偶校验会使 18 bit 里 1 的总数为偶，XOR 缩约为 0、取反为 1（OK）。
- **Q2**：在 `S_DETECT_HT` 中，如果既没有达到 `rot_eq_count >= 4`、也没有达到 `normal_eq_count > 4`（例如样本很噪），FSM 会停在原地吗？
  - **答**：会一直停在 `S_DETECT_HT` 累加计数，直到其中一个计数器越过阈值才离开；它没有独立的超时退出，依赖外部 `power_trigger` 之外的上层重启（实践中靠下一个包的 `reset` 或样本流结束来终结）。

---

### 4.4 HT 分支解析与两条路径的汇合收尾

#### 4.4.1 概念说明

一旦 `S_DETECT_HT` 确认是 HT-SIG，FSM 就要走完一段只在 HT 包里出现的 6 状态长链：先把后续两个符号 **按 90° 旋转后** 解出来拼成 `ht_sig1/ht_sig2`，算 CRC-8 并与字段里的 CRC 比对，再做一连串「能力校验」（只支持 MCS 0–7、20 MHz、无 STBC、BCC、等），通过后跳过 HT 短训练（HT-STS）、读完 HT 长训练（HT-LTS）更新信道估计，最后 **带着 HT 的长度/速率参数汇入** `S_DECODE_DATA`——和 legacy 分支汇合到同一个状态。`S_DECODE_DATA` 解够 `pkt_len` 字节后做 FCS-32 校验，进入 `S_DECODE_DONE` 输出 `fcs_ok`，再回空闲。

#### 4.4.2 核心流程

```
S_HT_SIGNAL      旋转90°解 2 符号 → 拼 ht_sig1/ht_sig2 (各3字节)
   │ byte_count==6
   ▼
S_CHECK_HT_SIG_CRC  逐位喂 34 bit 给 ht_sig_crc, 36 拍后比对
   │ crc_out==crc ?
   ├── 否 ─► S_HT_SIG_ERROR (E_WRONG_CRC) ─► 回空闲
   ▼
S_CHECK_HT_SIG    MCS/CBW/STBC/FEC/SGI/num_ext/tail/rsvd 逐项能力校验
   │ 任一不符 ─► S_HT_SIG_ERROR (E_UNSUPPORTED_* / E_HT_WRONG_*)
   ▼
S_HT_STS          数 64 个 sync_long 样本（跳过 HT 短训练）
   ▼
S_HT_LTS          short_gi<=ht_sgi; 再数 64 样本（HT-LTS, 触发 equalizer 更新信道）
   │ ht_next=0, 配 (ht_len+3)<<4, pkt_rate={1'b1,ht_mcs}
   ▼
S_DECODE_DATA  ◄──── (legacy 分支也从 S_CHECK_SIGNAL 直接到这里)
   │ byte_count >= pkt_len
   ▼  fcs 比对 pkt_fcs == EXPECTED_FCS
S_DECODE_DONE     输出 fcs_out_strobe + fcs_ok
   │
   ▼
S_WAIT_POWER_TRIGGER
```

#### 4.4.3 源码精读

**S_HT_SIGNAL** 在 [verilog/dot11.v:638-678](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L638-L678)：关键是 **在送入 ofdm_decoder 之前手工把星座旋转 90°**（Q 路当 I、I 路取负当 Q），这样下游解调才能按正常 BPSK 处理：

```verilog
ofdm_in_i <= eq_out_q_delayed;            // 顺时针 90°
ofdm_in_q <= ~eq_out_i_delayed+1;         // -I（补码取负）
```

`num_bits_to_decode <= 96`（[verilog/dot11.v:622](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L622)）= 48 bit HT-SIG 数据 × 2，`do_descramble <= 0` 因为 HT-SIG 也不扰码（与 SIGNAL 一样走 flush 分支，见 u3-l5）。

**S_CHECK_HT_SIG_CRC** 在 [verilog/dot11.v:680-707](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L680-L707)：用 `crc_count` 从 0 数到 35，前 24 拍喂 `ht_sig1` 的位、接着 10 拍喂 `ht_sig2` 的位（共 34 bit），第 34 拍停喂、第 35 拍比对：

```verilog
if (crc_count < 24) begin crc_in <= ht_sig1[crc_count]; ... end
else if (crc_count < 34) begin crc_in <= ht_sig2[crc_count-24]; ... end
else if (crc_count == 35) begin
    if (crc_out ^ crc) begin status_code <= E_WRONG_CRC; state <= S_HT_SIG_ERROR; end
    else begin ht_sig_crc_ok <= 1; state <= S_CHECK_HT_SIG; end
end
```

CRC-8 的多项式与「逐位更新 + 末尾取反」的数学在 u4-l3 精讲；这里只需记住它是一条 **36 拍的固定时序**，期间 FSM 不做别的。

**S_CHECK_HT_SIG** 在 [verilog/dot11.v:709-740](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L709-L740)：一连串 `else if` 做「能力围栏」，把项目不支持的特性全部挡掉（MCS>7、40 MHz、STBC≠0、LDPC、SGI、多空间流、尾比特非零、保留位非 1）。注意这里每个失败分支都写一个独立的 `E_UNSUPPORTED_*` 状态码，便于上层定位拒绝原因。

**S_HT_STS / S_HT_LTS** 在 [verilog/dot11.v:747-775](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L747-L775)：各数 64 个 `sync_long_out_strobe`，分别对应「跳过 HT 短训练」和「读完 HT-LTS」。`S_HT_LTS` 进入时置 `ht_next<=1` 通知 equalizer 用 HT-LTS 更新信道（见 u3-l1），配好 `(ht_len+3)<<4`、`pkt_rate<={1'b1, ht_mcs}` 后汇入 `S_DECODE_DATA`，与 legacy 分支在 [verilog/dot11.v:777](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L777) 汇合。

**S_DECODE_DATA / S_DECODE_DONE** 在 [verilog/dot11.v:777-821](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L777-L821)：每收到一个字节 `byte_count++`，到达 `pkt_len` 时取 `crc32` 的结果 `pkt_fcs` 与常数 `EXPECTED_FCS`（[verilog/common_params.v:71](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L71)，值 `32'hc704dd7b`）比对，置 `fcs_ok` 与 `status_code`，然后经 `S_DECODE_DONE` 撤销 `fcs_out_strobe` 回到空闲：

```verilog
if (byte_count >= pkt_len) begin
    fcs_out_strobe <= 1;
    if (pkt_fcs == EXPECTED_FCS) begin fcs_ok <= 1; status_code <= E_OK; end
    else                          begin fcs_ok <= 0; status_code <= E_WRONG_FCS; end
    state <= S_DECODE_DONE;
end
```

#### 4.4.4 代码实践

**实践目标**：看清 CRC 的 36 拍时序与错误码的「上下文相关」语义。

1. 在 [verilog/dot11.v:680-707](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L680-L707) 数清楚 `crc_count` 从 0 到 35 一共经历多少拍、其中多少拍在喂数据（24+10=34 拍）。
2. 在仿真里用一个 dot11n 样本（如 `testing_inputs` 下的 HT 样本）跑到 `S_CHECK_HT_SIG`，故意思考：若把 `ht_mcs` 字段改成 8，会落到哪个 `status_code`？
3. **观察一处代码细节**：`S_CHECK_HT_SIG` 里有一行 `else if (short_gi)`（[verilog/dot11.v:727](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L727)）检查的是 reg `short_gi`，但这个 reg 直到稍后的 `S_HT_LTS`（[verilog/dot11.v:759](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L759)）才会被赋值为 `ht_sgi`。请确认：在 `S_CHECK_HT_SIG` 时刻 `short_gi` 仍是复位默认值 0，因此这一分支在此处永远不会触发——若作者本意是拒绝 SGI 包，这里检查的应是 `ht_sgi`。

**需要观察的现象**：CRC 计算占用固定的 36 个时钟周期，期间 `state` 保持 `S_CHECK_HT_SIG_CRC` 不变；`short_gi` 在进入 `S_CHECK_HT_SIG` 时确实还是 0。

**预期结果**：你会得到「36 拍 CRC、其中 34 拍喂位」的结论，并发现 `short_gi` 检查的时序问题（这是一个真实的源码阅读发现，留作与作者确认的疑点，待本地验证其行为）。

#### 4.4.5 小练习与答案

- **Q1**：为什么 `S_HT_SIGNAL` 要在喂 `ofdm_decoder` 之前手工把 I/Q 旋转 90°？
  - **答**：HT-SIG 用 Q 路 BPSK（星座整体旋转 90°），而下游 `demodulate` 是按标准 I 路 BPSK 判决的；预先旋转就是把 HT-SIG 的星座「掰回」到 demodulate 期望的朝向，复用同一套解调逻辑。
- **Q2**：`status_code <= E_WRONG_FCS`（值=1）和 `status_code <= E_PARITY_FAIL`（值也=1）冲突吗？
  - **答**：不冲突。前者在 `S_DECODE_DONE` 写入（DATA 阶段失败），后者在 `S_CHECK_SIGNAL` 写入（SIGNAL 阶段失败），二者处于不同的收尾状态，上层联合 `state`/`status_code` 即可区分，这正是 `common_params.v` 注释「same value may have different meaning depend on the state」的含义。

---

### 4.5 控制平面的调度：reset/enable 脉冲与错误恢复

#### 4.5.1 概念说明

把前面四个状态串起来后，值得单独提炼一条 **横切** 的规律：FSM 到底是怎么「指挥」数据平面的？答案是两套互补的信号——

- **区间使能 `enable`**：决定子模块在某段时间内是否工作。例如 `sync_long_enable` 在进入 `S_SYNC_LONG` 时置 1、回到 `S_WAIT_POWER_TRIGGER` 时置 0，让 `sync_long` 在不属于自己的阶段自动停摆。
- **边界复位 `reset` 脉冲**：在状态切换的临界拍打一个 1 拍宽的高电平，接到子模块的 `reset(reset | xxx_reset)` 上，把它的内部寄存器清零。典型用法是「置 1 后在下一状态的首拍自清零」。

这套机制还顺带完成了 **资源共享**：`phase` 模块用 `state == S_SYNC_SHORT` 的 MUX 在 `sync_short` 与 `equalizer` 间分时复用，`rot_lut` 用双口 RAM 同时服务 `sync_long` 与 `equalizer`（细节在 u6-l2）。错误恢复则统一走「写 `status_code` → 进 `S_*_ERROR` → 一拍后回 `S_WAIT_POWER_TRIGGER`」的短路径。

#### 4.5.2 核心流程

```
状态切换边界:
  进入某状态时:  xxx_reset <= 1;  xxx_enable <= 1;   // 打复位脉冲 + 开使能
  该状态首拍:    if (xxx_reset) xxx_reset <= 0;      // 自清脉冲
  回到空闲时:    xxx_enable <= 0;                     // 关使能

错误恢复:
  检测到错误:  status_code <= E_*;  state <= S_*_ERROR;
  S_*_ERROR:   (一拍)  state <= S_WAIT_POWER_TRIGGER;
```

#### 4.5.3 源码精读

复位/使能信号的声明集中在 [verilog/dot11.v:163-169](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L163-L169) 与 [verilog/dot11.v:184-186](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L184-L186)。注意 `sync_short_enable` 是 wire、其余是 reg：

```verilog
reg sync_short_reset;
reg sync_long_reset;
wire sync_short_enable = state == S_SYNC_SHORT;   // 组合使能
reg sync_long_enable;
reg equalizer_reset, equalizer_enable;
reg ofdm_reset, ofdm_enable;
```

这些信号接到子模块的方式完全一致——`reset` 端口「或」上脉冲，`enable` 端口「与」上区间使能。以 `sync_short_inst` 为例（[verilog/dot11.v:272-275](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L272-L275)）：

```verilog
sync_short sync_short_inst (
    .clock(clock),
    .reset(reset | sync_short_reset),       // 全局复位 或 单拍脉冲
    .enable(enable & sync_short_enable),    // 全局使能 与 区间使能
    ...
);
```

`ofdm_reset` 是被复用最多的一条脉冲线——它在进入 `S_DECODE_SIGNAL`、`S_CHECK_SIGNAL`、`S_HT_SIGNAL`、以及 legacy/HT 各自下到 `S_DECODE_DATA` 时都被置 1（见 [verilog/dot11.v:521](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L521)、[558](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L558)、[624](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L624)、[768](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L768)），因为 `ofdm_decoder` 在每一段解码（SIGNAL、HT-SIG、DATA）前都必须清空自己的子流水线。

错误恢复的两个一拍状态在 [verilog/dot11.v:601-603](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L601-L603) 与 [verilog/dot11.v:742-745](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L742-L745)，二者都只做一件事：撤销 strobe、回空闲。最后 `default` 分支（[verilog/dot11.v:823-825](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L823-L825)）保证任何未定义状态都能回到 `S_WAIT_POWER_TRIGGER`，是一道防跑飞的安全网。

#### 4.5.4 代码实践

**实践目标**：把「控制信号 → 受控模块」的对应关系整理成表。

1. 通读 [verilog/dot11.v:257-400](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L257-L400) 的所有子模块例化。
2. 做一张四列表：`模块 | reset 表达式 | enable 表达式 | 在哪些状态下被置复位脉冲`。
3. 特别标记 `ofdm_reset` 这一行，列出它被置 1 的全部位置。

**需要观察的现象**：每个子模块的 reset 都是「`reset | 自有脉冲`」、enable 都是「`enable & 自有区间`」的统一模式；`ofdm_reset` 在状态机里被置 1 的次数最多（每段解码前一次）。

**预期结果**：得到 5 行（power_trigger/sync_short/sync_long/equalizer/ofdm_decoder）调度表，`ofdm_reset` 至少出现在 SIGNAL、HT-SIG、legacy DATA、HT DATA 四个入口处。

#### 4.5.5 小练习与答案

- **Q1**：为什么所有模块的 reset 都写成 `reset | xxx_reset` 而不是直接用 `xxx_reset`？
  - **答**：要同时支持两种复位源——全局异步/同步 `reset`（上电、整片重置）和 FSM 在状态切换时打的局部单拍脉冲 `xxx_reset`（只清这一个模块），二者「或」起来即可用同一套清零逻辑。
- **Q2**：`default` 分支回 `S_WAIT_POWER_TRIGGER` 有什么用？
  - **答**：`state` 是 4 bit 可表达 0–15，但只定义了 0–14；万一因毛刺或未覆盖路径进入 15，`default` 保证它立刻回到空闲态而不是死锁，是一道防御性设计。

## 5. 综合实践

**任务**：依据 `common_params.v` 的状态定义，绘制 `dot11` 顶层 FSM 的 **完整状态转移图**。

要求：

1. 把 15 个状态全部画进图中，按本讲 4.1.2 的 5 组分区排版（等待/同步/SIGNAL/HT 分支/收尾/错误）。
2. 在 **legacy 路径** 上标注关键判定：`power_trigger`、`short_preamble_detected`、`long_preamble_detected`、`parity_ok & ~rsvd & ~tail`、`legacy_rate != 4'b1011`、`byte_count>=pkt_len`、`pkt_fcs==EXPECTED_FCS`。
3. 在 **HT 路径** 上标注：`legacy_rate == 4'b1011`、`rot_eq_count>=4`、`crc_out==crc`、各项 `E_UNSUPPORTED_*` 能力校验、`sync_long_out_count==64`。
4. 用虚线标出所有进入 `S_SIGNAL_ERROR` / `S_HT_SIG_ERROR` 的失败边，并在边上写出对应的 `E_*` 状态码。
5. 标出 legacy 与 HT 两条路径在 `S_DECODE_DATA` 的 **汇合点**，以及所有错误状态/`S_DECODE_DONE` 回到 `S_WAIT_POWER_TRIGGER` 的 **回边**。

完成后，把这张图与 [verilog/dot11.v:463-826](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L463-L826) 的 `case(state)` 逐状态对照，确认每条边都能在源码里找到对应行。这张图将作为后续 u4-l2/u4-l3 精读 SIGNAL 与 HT-SIG 字段时的导航。

## 6. 本讲小结

- `dot11` 顶层用一个 15 状态 FSM 统一调度整条解码流水线，状态码定义在 `common_params.v`（0–14），复位入口唯一指向 `S_WAIT_POWER_TRIGGER`。
- 主流程链是 `S_WAIT_POWER_TRIGGER → S_SYNC_SHORT → S_SYNC_LONG → S_DECODE_SIGNAL → S_CHECK_SIGNAL`，前端同步带 `sample_count>320` 超时保护与功率掉线回退。
- `S_CHECK_SIGNAL` 是关键分叉点：三项校验（parity/rsvd/tail）通过后，用 `legacy_rate == 4'b1011`（6 Mbps）决定走 legacy DATA 还是 HT 检测。
- HT 分支用「Q 路 BPSK、|Q|>|I| 样本≥4」确认 HT-SIG，再走 旋转90°解码 → CRC-8(36 拍) → 能力校验 → 跳 HT-STS → 读 HT-LTS 的长链，最后与 legacy 分支在 `S_DECODE_DATA` 汇合。
- 控制平面靠两套信号驱动数据平面：区间 `enable`（gating）+ 边界 `reset` 脉冲（`reset | xxx_reset`、下一拍自清），并辅以 `status_code`（上下文相关）与两个一拍错误状态做恢复。
- `default` 分支保证任何未定义状态回到空闲，是一道防跑飞的安全网。

## 7. 下一步学习建议

- **u4-l2 legacy SIGNAL 字段解析与校验**：精读 `signal_bits` 的位域切片（`legacy_rate`/`legacy_len`/`legacy_sig_parity`/`legacy_sig_tail`）与 `(legacy_len+3)<<4` 的长度推导，把本讲「SIGNAL 校验」一节展开到比特级。
- **u4-l3 HT-SIG 解析、CRC 与格式检测**：精读 `ht_sig_crc.v` 的 CRC-8 多项式与「末尾取反」原因，把本讲的 36 拍 CRC 时序与 `S_DETECT_HT` 的旋转检测讲透。
- **u4-l4 配置寄存器机制**：了解 `set_stb/set_addr/set_data` 总线如何改写 `SR_*` 参数（如 `power_trigger` 的门限），理解运行时如何调参。
- **u4-l5 FCS 校验与 CRC32**：精读 `crc32.v` 与位反转喂入，把本讲 `S_DECODE_DATA` 末尾的 `pkt_fcs == EXPECTED_FCS` 比对展开。
