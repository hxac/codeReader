# 触发与超时机制

## 1. 本讲目标

上一讲（[u2-l3](u2-l3-core-fsm.md)）我们拆解了核心 FSM 的五态骨架，知道了它**怎么**走完一次读周期，但还留下一个关键问题没有回答：**FSM 是被谁、在什么时候推离 `Idle_s` 的？** 本讲就专门回答这个「读周期的启动机制」问题。

学完本讲，你应当能够：

- 说清 `Trig` 单拍脉冲与 `TimeoutCkCycles_g` 超时这**两种**启动方式各自如何产生 `Start` 信号；
- 解释 `Enable` 门控的三重作用：禁用时既不启动新周期、也把正在进行的周期硬拉回 `Idle_s`、并把超时计数器清零；
- 说明为什么「读周期进行中的 `Trig` 会被丢弃而不是排队」，并指出保证这一行为的具体代码行；
- 会用 wrapper 中的常量公式 `TimeoutCkCycles_c` 把「时钟频率 + 微秒数」换算成「时钟周期数」，并理解「只接超时、不接 `Trig`」即可实现周期性读取。

本讲只关注**启动与门控**，不重复 FSM 各状态内部动作（见 u2-l3），也不展开 AXI 主机握手细节（见 [u2-l6](u2-l6-axi-master-read.md)）。

## 2. 前置知识

本讲用到的几个概念，先用大白话点一下：

- **单拍脉冲（single-cycle pulse）**：一个信号只在一个时钟周期里为 `'1'`，下一个周期就回到 `'0'`。`Trig` 就被要求是这样的输入。
- **门控（gating）**：用一个使能信号去「卡住」另一个信号，使能无效时后者不产生效果。本讲里 `Enable` 就是闸门。
- **超时计数器（timeout counter）**：每个时钟周期自加 1，数到某个上限就「到点」，触发一次动作，然后清零重新数。本质是一个「把时间翻译成周期数」的沙漏。
- **RV / 软件视角寄存器**：上一讲 [u2-l2](u2-l2-register-map.md) 讲过的 `Ena`/`RegCnt` 等，软件通过 `s00_axi` 读写它们；本讲里 `Enable` 就是 `Ena` 寄存器的 bit0。
- **两进程方法（two-process）**：上一讲 [u2-l3](u2-l3-core-fsm.md) 讲过，组合进程先 `v := r` 整体复制、再按需覆盖，时序进程只打拍与复位。本讲的 `Start`、`TimeoutCnt` 都是这个 record 里的字段。

## 3. 本讲源码地图

| 文件 | 本讲关注的内容 |
| --- | --- |
| `hdl/axi_mm_reader.vhd` | 触发块（`Trig`+超时→`Start`）、`Enable` 全局覆盖、`TimeoutCnt` 的声明范围与复位 |
| `hdl/axi_mm_reader_wrp.vhd` | 把 GUI 的「时钟频率」「超时微秒数」换算成 `TimeoutCkCycles_c` 周期数，并传给核心 |
| `tb/top_tb.vhd` | 真实的「单次触发」「超时」「禁用」「触发过密造成背压」用例，作为实践的验证依据 |
| `doc/Documentation.md` | 官方对 `Trig`、超时、周期性读取的文字说明 |

## 4. 核心概念与源码讲解

### 4.1 Trig 脉冲触发

#### 4.1.1 概念说明

`Trig` 是 IP 对外的「启动按钮」。文档对它的约束写得很明确：

> 每当 trigger 输入出现一个脉冲，IP 核就执行一次寄存器读周期。**进行中的读周期里出现的 trigger 脉冲会被忽略**；只能用单拍脉冲（恰好一个高电平周期）作为输入。
> —— [doc/Documentation.md:25-27](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L25-L27)

注意文档同时承诺了两件事：**①有脉冲就启动**；**②进行中再来脉冲会被忽略**。这两件事在源码里分别由不同位置的代码保证，本模块先看第①件。

`Trig` 在核心 entity 里是一个带默认值 `'0'` 的输入，意味着不接外部触发时它恒为 0：[hdl/axi_mm_reader.vhd:36](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L36)。

#### 4.1.2 核心流程

`Trig` 本身不直接驱动 FSM，而是先经过一个「触发块」翻译成一个内部统一信号 `Start`，再由 FSM 在 `Idle_s` 里消费。流程如下：

```
Trig(单拍) ──┐
             ├──> [触发块] ──> v.Start(组合) ──> r.Start(寄存) ──> FSM 在 Idle_s 消费
TimeoutCnt到点 ─┘                       （默认每拍清零）
```

要点：

1. `v.Start` 每拍**默认先清零**（组合默认值）；
2. 只有当 `Trig='1'`（或超时到点）**且** `Enable='1'` 时，才把 `v.Start` 置 1；
3. `Start` 是**寄存**信号（`r.Start`），FSM 只在 `Idle_s` 检查它；
4. 因此 `Trig` 到 FSM 离开 `Idle_s` 之间有一拍寄存延迟，且 `Start` 是一个规整的单拍脉冲。

把 `Trig` 先并到 `Start` 再寄存，而不是直接喂给 FSM，是为了让「外部脉冲」和「内部超时」这两路来源在 FSM 眼里看起来完全一样——都是一拍 `Start`。

#### 4.1.3 源码精读

触发块全貌（同时处理 `Trig` 与超时，本模块先聚焦红字部分的 `Trig` 路径）：

```vhdl
-- *** Trigger ***
v.Start := '0';                                          -- 默认清零：保证 Start 是单拍
if Trig = '1' or r.TimeoutCnt = TimeoutCkCycles_g-1 then -- Trig 或 超时到点
    if Enable = '1' then                                 -- 仅在使能时才动作
        v.TimeoutCnt := 0;
        v.Start := '1';                                  -- 请求启动一次读周期
    end if;
else
    v.TimeoutCnt := r.TimeoutCnt + 1;                    -- 没触发就继续数
end if;
```

> 代码见 [hdl/axi_mm_reader.vhd:106-115](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L106-L115)。

`Start` 被 FSM 消费的位置——**只在 `Idle_s` 里看 `r.Start`**：

```vhdl
when Idle_s =>
    if r.Start = '1' then
        v.Fsm := ReadAddr_s;     -- 唯一的离场口
    end if;
```

> 代码见 [hdl/axi_mm_reader.vhd:120-123](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L120-L123)。

测试台里的真实用法——「单次触发读」用例，用 `PulseSig(Trig, aclk)` 打出一个单拍脉冲，随后校验一帧结果：

```vhdl
-- *** Trigger Single Read ***
StimCase <= 1;
ClockedWaitTime(100 ns, aclk);
PulseSig(Trig, aclk);
CheckResults(0, 1, ...);
```

> 代码见 [tb/top_tb.vhd:271-277](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L271-L277)。

#### 4.1.4 代码实践

**实践目标**：确认「`Trig` 单拍 → `Start` 单拍 → FSM 离开 Idle」的链路，并观察「进行中再触发会被丢弃」。

**操作步骤**（源码阅读型）：

1. 打开 `hdl/axi_mm_reader.vhd`，定位触发块（106-115 行）与 `Idle_s` 分支（120-123 行）。
2. 假设 FSM 当前在 `ApplyCmd_s`（读周期进行中），此时软件/外部打出一拍 `Trig`：
   - 跟踪 `v.Start`：本拍被置 1，下一拍进入 `r.Start`；
   - 跟踪 FSM：`ApplyCmd_s` 分支（142-146 行）**完全不读 `r.Start`**，所以状态不变；
   - 再下一拍，触发块把 `v.Start` 重新清成默认 0，`r.Start` 随之归零。
3. 结论：这一拍 `Trig` 被无声丢弃，没有排队，读周期结束后不会自动补触发。

**需要观察的现象**：进行中的读周期不会因为额外的 `Trig` 而中断或延长；`Trig` 也不会被「记住」到当前周期结束。

**预期结果**：进行中的 `Trig` 不产生任何效果，行为与「完全没有这拍 `Trig`」一致。这一行为对应测试台的「背压」用例（连续高频 `Trig`），见 [tb/top_tb.vhd:328-336](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L328-L336)：用 `for i in 0 to 5 loop PulseSig(Trig, aclk); ...` 连打 6 拍，多余的触发不丢失数据但会形成 FIFO 堆积（背压）。

**待本地验证**：若你能在 Modelsim/GHDL 跑一次回归仿真（见 [u1-l3](u1-l3-running-simulation.md)），可在 `Trig` 信号与 `r.Start`（需临时加到波形观察窗口）上看到「进行中的 `Trig` 只产生一拍 `r.Start` 且不影响 FSM 状态」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `v.Start` 必须每拍先清零（第 107 行），而不是「被置 1 后一直保持」？
**答案**：若不清零，`Start` 会变成电平而非脉冲，FSM 一回到 `Idle_s` 就会被同一个 `Start` 反复推走，无法稳定停在 Idle；清零保证 `Start` 是单拍脉冲，对应文档「只用单拍脉冲」的约束。

**练习 2**：`Trig` 在 entity 里的默认值是 `'0'`（第 36 行）。这对「只想用超时做周期性读取」的用户有什么意义？
**答案**：用户可以把 `Trig` 端口悬空（wrapper 里 `Trig => Trig`，而 wrapper 的 `Trig` 端口同样有默认 `'0'`），核心会一直看到 `Trig='0'`，于是只有超时这一条路径能产生 `Start`，天然实现周期性读取（详见 4.4）。

---

### 4.2 TimeoutCnt 超时触发

#### 4.2.1 概念说明

很多场景下并没有（或不想依赖）外部脉冲：例如要「每 100 µs 抓一次状态寄存器」。IP 提供了一个内置沙漏 `TimeoutCnt`：使能状态下，只要没来 `Trig`，它就一拍一拍往上数；数到上限就自己产生一个 `Start`，然后清零重新数。文档原文：

> 如果 IP 核已使能且没有 trigger 输入到来，经过这个时间后会启动一次读周期。这个功能也可以用来配置周期性读取：设一个超时、不用 trigger 输入即可。
> —— [doc/Documentation.md:46-47](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L46-L47)

#### 4.2.2 核心流程

超时与 `Trig` 共用同一个触发块，二者是「或」的关系：

```
每拍：
  v.Start := 0                       -- 默认
  if (Trig=1) or (TimeoutCnt == 上限):  -- 任一到点
       if Enable=1: Start=1, TimeoutCnt=0   -- 启动并清沙漏
  else:
       TimeoutCnt += 1               -- 继续数
```

几个要点：

- 上限是 `TimeoutCkCycles_g - 1`（因为计数从 0 开始）。计数器在 record 里的范围被精确声明为 `0 to TimeoutCkCycles_g-1`，从硬件上杜绝越界。
- 超时与 `Trig` 在同一拍里是「或」关系：如果同一拍既来 `Trig` 又恰好到点，只算一次 `Start`，不会重复。
- **超时到点也只产生一拍 `Start`**，同样只在 `Idle_s` 生效；若到点时 FSM 正忙（不在 Idle），这拍 `Start` 被忽略，但计数器照常清零——所以长时间读周期内沙漏会被反复清零（见 4.2.4 末尾的细节）。

#### 4.2.3 源码精读

计数器字段的声明与范围（保证不会数爆）：[hdl/axi_mm_reader.vhd:78](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L78)。

```vhdl
TimeoutCnt : natural range 0 to TimeoutCkCycles_g-1;
```

超时判据出现在触发块的条件里（`or r.TimeoutCnt = TimeoutCkCycles_g-1`），见 [hdl/axi_mm_reader.vhd:106-115](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L106-L115)（与 4.1.3 同一段）。

**「微秒」是怎么变成「周期数」的？** 换算在 wrapper 里做，不在核心里。核心只认「周期数」`TimeoutCkCycles_g`，而 wrapper 用 GUI 的时钟频率和超时微秒数算出这个周期数常量：

```vhdl
constant TimeoutCkCycles_c : natural := integer(real(ClkFrequencyHz)*real(TimeoutUs_g)/1.0e6);
```

> 代码见 [hdl/axi_mm_reader_wrp.vhd:134](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L134)。两个 GUI 参数的声明见 [hdl/axi_mm_reader_wrp.vhd:27-28](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L27-L28)，随后在核心例化时作为 generic 传入：[hdl/axi_mm_reader_wrp.vhd:314-316](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L314-L316)。

换算公式（`integer()` 对正数即向零取整 = 下取整）：

\[
\text{TimeoutCkCycles\_c} = \left\lfloor \dfrac{f_{\text{clk}}\;(\text{Hz}) \;\times\; T_{\text{timeout}}\;(\mu\text{s})}{10^{6}} \right\rfloor
\]

复位的处理：时序进程里 `Rst='1'` 时把 `TimeoutCnt` 也归零，避免上电后立刻误触发：[hdl/axi_mm_reader.vhd:194-199](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L194-L199)。

#### 4.2.4 代码实践

**实践目标**：把 4.2.3 的换算公式代一次真实数值，并用测试台的「Timeout」用例印证超时确实会自动启动读周期。

**操作步骤**：

1. 计算 `ClkFrequencyHz = 100 MHz`、`TimeoutUs_g = 100` 时的 `TimeoutCkCycles_c`：

\[
\text{TimeoutCkCycles\_c} = \left\lfloor \dfrac{100\,000\,000 \times 100}{1\,000\,000} \right\rfloor = \lfloor 10\,000 \rfloor = 10\,000
\]

   100 MHz 下一拍 10 ns，10000 拍 = 100 µs，与 `TimeoutUs_g=100` 自洽。

2. 注意测试台**并没有**用默认的 100 µs，而是把超时配小到 `TimeoutUs_g => 10` 以缩短仿真时间：[tb/top_tb.vhd:160](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L160)（对应 1000 拍 ≈ 10 µs）。

3. 阅读测试台「Timeout」用例（StimCase 3）：AXIS 模式下先用 `CheckNoActivity(m_axis_tvalid, 8 us, 0, ...)` 确认这 8 µs 内**没有**数据（说明没被别的机制提前触发），再用 `WaitForValueStdl(m_axis_tvalid, '1', 3 us, "Timout not occurred")` 等到 10 µs 左右 valid 拉高——证明是超时自动启动了读周期：

```vhdl
-- *** Timeout ***
StimCase <= 3;
if OutputType_g = "AXIS" then
    CheckNoActivity(m_axis_tvalid, 8 us, 0, "Timeout interrupted");
    WaitForValueStdl(m_axis_tvalid, '1', 3 us, "Timout not occurred");
```

> 代码见 [tb/top_tb.vhd:293-311](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L293-L311)。

**需要观察的现象**：在没有任何 `Trig` 的情况下，`m_axis_tvalid` 在约 10 µs 处自行拉高。

**预期结果**：1000 拍（10 µs）到点 → `Start` → FSM 走完一次读周期 → 数据出现在 AXIS/FIFO。

**待本地验证**：若运行仿真，可测量 `m_axis_tvalid` 拉高时刻距使能时刻的差值，应接近 10 µs。

> **进阶细节**：读周期进行中（FSM 不在 Idle），触发块每拍仍在运行。若一次读周期长得超过了超时周期，沙漏会在中途反复到点、反复清零，并产生被忽略的 `Start`。因此当 FSM 终于回到 Idle 时，沙漏往往不在最大值，下一次超时启动会略有抖动（小于一个完整周期）。典型应用里读周期远短于超时周期，此抖动可忽略。

#### 4.2.5 小练习与答案

**练习 1**：把 `ClkFrequencyHz` 改成 50 MHz、`TimeoutUs_g` 仍为 100，`TimeoutCkCycles_c` 变成多少？
**答案**：\(\lfloor 50\,000\,000 \times 100 / 10^6 \rfloor = \lfloor 5000 \rfloor = 5000\) 拍；50 MHz 下一拍 20 ns，5000 拍 = 100 µs，仍与 100 µs 一致（换算的目的正是让结果与频率解耦）。

**练习 2**：超时的判据为什么写成 `r.TimeoutCnt = TimeoutCkCycles_g-1`，而不是 `> =`？
**答案**：因为 record 已把范围卡死在 `0 to TimeoutCkCycles_g-1`，计数器根本不可能达到 `TimeoutCkCycles_g`；用「等于上限」既精确又能在到点的那一拍立刻触发并清零，避免多耗一拍。若误写成 `>=`，在范围受限的情况下行为相同，但语义上多余、且在去掉范围约束时会产生一拍延迟。

**练习 3**：`TimeoutCkCycles_g` 的 entity 默认值是 `10_000_000`（[hdl/axi_mm_reader.vhd:24](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L24)），但打包成 IP 后实际生效的是 wrapper 算出来的 `TimeoutCkCycles_c`。这两者是什么关系？
**答案**：entity 默认值只在「直接例化核心、不传该 generic」时才生效，是个兜底；wrapper 在例化核心时显式传入 `TimeoutCkCycles_g => TimeoutCkCycles_c`（[hdl/axi_mm_reader_wrp.vhd:315](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L315)），所以打包后的 IP 里核心用的是 GUI 配出的周期数，entity 默认值被覆盖。

---

### 4.3 Enable 门控

#### 4.3.1 概念说明

`Enable` 是软件通过 `Ena` 寄存器（地址 0x00 的 bit0，见 [u2-l2](u2-l2-register-map.md)）写进来的总开关。它对启动机制起到三重门控：

1. **不让启动**：`Trig` 或超时到点时，若 `Enable='0'`，不会产生 `Start`；
2. **强制回 Idle**：只要 `Enable='0'`，无论 FSM 当前在哪个状态，都被立刻拉回 `Idle_s`；
3. **冻结沙漏**：`Enable='0'` 时 `TimeoutCnt` 被强制清零，禁用期间不「攒时间」，重新使能后从 0 开始数满一个完整周期才到点。

文档对 `RegCnt` 有「不要在使能时修改」的告诫（[doc/Documentation.md:73](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L73)），隐含的工程模式是：**改配置前先 `Enable=0` 停下 IP，改完再 `Enable=1`**。这也正是 C 驱动 `SetRegTable` 要求 IP 处于禁用状态的根因（见 [u3-l1](u3-l1-c-driver.md)）。

#### 4.3.2 核心流程

`Enable` 在组合进程里出现**两次**，分工不同：

```
位置 A（触发块内层）：决定"要不要产生 Start"
   if (Trig 或 超时):
       if Enable=1:  Start=1, 清沙漏        ← 禁用时这一句被跳过
位置 B（FSM 之后全局覆盖）：决定"要不要把状态机按回去"
   if Enable=0:
       Fsm := Idle_s                          ← 硬拉回 Idle
       TimeoutCnt := 0                        ← 冻结沙漏
```

两处合起来，才完整实现「禁用时既不启动、又把正在做的事打断」。

#### 4.3.3 源码精读

**位置 A**——触发块内层的使能判断：[hdl/axi_mm_reader.vhd:108-112](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L108-L112)（`if Enable = '1' then ... end if;`）。注意当 `Enable='0'` 时，这一整段被跳过，于是 `v.Start` 保持第 107 行的默认 `'0'`，沙漏也不在此复位。

**位置 B**——FSM case 之后的全局覆盖，这是「禁用即回 Idle」的关键：

```vhdl
if Enable = '0' then
    v.Fsm := Idle_s;
    v.TimeoutCnt := 0;
end if;
```

> 代码见 [hdl/axi_mm_reader.vhd:156-159](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L156-L159)。它写在 `case r.Fsm is ... end case;` **之后**，所以无论 case 里算出什么状态，只要 `Enable='0'` 就会被这两行盖成 `Idle_s` 与清零——这是组合进程里「后写覆盖先写」的典型用法。

**软件怎么把 `Enable` 喂进来**：wrapper 从 AXI 从机解码出的 `reg_wdata` 里取 `Ctrl` 寄存器的使能位接给核心：

```vhdl
Enable => reg_wdata(RegIdx_Ctrl_c)(BitIdx_Ctrl_Ena_c),
```

> 代码见 [hdl/axi_mm_reader_wrp.vhd:327](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L327)。即软件写 `Ena` 寄存器的 bit0，经 `reg_wdata` 直达核心的 `Enable` 端口。

#### 4.3.4 代码实践

**实践目标**：用测试台的「Disabled」用例印证「禁用时 `Trig` 与超时都无效」，并解释重新使能后为何要等满一个完整超时周期。

**操作步骤**：

1. 阅读测试台「Disabled」用例（StimCase 4）：软件先写 0 禁用（`axi_single_write(RegIdx_Ctrl_c*4, 0, ...)`），打一拍 `Trig`，随后 `CheckNoActivity(m_axis_tvalid, 12 us, 0, "Timeout interrupted")`——注意这里等了 **12 µs**，超过了 10 µs 的超时周期，却仍然没有数据，证明**禁用状态下超时也被压制**：

```vhdl
-- *** Test Disabled ***
axi_single_write(RegIdx_Ctrl_c*4, 0, axi_ms, axi_sm, aclk);   -- 禁用
ClockedWaitTime(100 ns, aclk);
PulseSig(Trig, aclk);                                          -- Trig 被忽略
CheckNoActivity(m_axis_tvalid, 12 us, 0, "Timeout interrupted");-- 超时也被压制
axi_single_write(RegIdx_Ctrl_c*4, 1, axi_ms, axi_sm, aclk);   -- 重新使能
CheckNoActivity(m_axis_tvalid, 2 us, 0, "Timeout interrupted");-- 才过 2us，还没到点
```

> 代码见 [tb/top_tb.vhd:313-322](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L313-L322)。

2. 解释最后那行 `CheckNoActivity(..., 2 us, ...)`：重新使能后沙漏从 0 开始数（因为禁用期间 `TimeoutCnt` 被位置 B 强制清零），到 2 µs 时离 10 µs 还早，所以没有数据。这正好印证了 4.3.1 的第 ③ 点。

**需要观察的现象**：禁用期间，无论 `Trig` 还是超时都不产生输出；重新使能后要等约一个完整超时周期（10 µs）才会出现第一帧。

**预期结果**：禁用 12 µs 内 `m_axis_tvalid` 全程为 0；重新使能后再过约 10 µs 才出现数据。

**待本地验证**：可在仿真波形里观察 `Enable`（即 `reg_wdata(Ctrl)(Ena)`）与 `m_axis_tvalid` 的时间关系。

> **诚实提示**：位置 B 只强制了 FSM 状态与 `TimeoutCnt`，**并没有**显式撤销可能在飞行中的 AXI 读命令（如 `AxiM_CmdRd_Vld`）。本讲只描述代码可证明的「FSM 立即回 Idle、沙漏清零」这一行为，不妄下「禁用能干净地中止 AXI 事务」的结论；AXI 协议层面的细节留到 [u2-l6](u2-l6-axi-master-read.md) 讨论。

#### 4.3.5 小练习与答案

**练习 1**：禁用期间 `TimeoutCnt` 为什么不会偷偷往上数，等使能时「立刻到点」？
**答案**：因为位置 B（156-159 行）每拍都把 `v.TimeoutCnt := 0`，且它写在触发块之后、覆盖触发块的结果。所以禁用期间沙漏被钉死在 0，重新使能后必须数满 `TimeoutCkCycles_g` 拍才到点。

**练习 2**：如果只保留位置 A、删掉位置 B，IP 还能正常工作吗？
**答案**：表面看似可行（禁用时不产生新 `Start`），但有两个问题：① 进行中的读周期会**继续跑完**才停，无法「立即停止」，与「禁用即停」的语义不符；② 禁用期间沙漏仍会往上数（位置 A 只在到点时才清零），重新使能后可能很快甚至立刻到点，行为不可预期。位置 B 正是用来堵这两个洞的。

**练习 3**：`Enable` 端口在 entity 里**没有**默认值（[hdl/axi_mm_reader.vhd:43](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L43)），而 `Trig` 有默认 `'0'`。为什么？
**答案**：`Enable` 是必须由软件显式控制的「总开关」，没有合理默认值——若默认成 `'1'`，上电即跑可能用到未配置的 RegTable；若默认成 `'0'`，等于强制软件每次都去开。所以让它无默认值、强制例化者（wrapper）从 `reg_wdata` 显式接入，语义最清晰。`Trig` 则是「可选」触发源，悬空即不用，所以给 `'0'` 默认值方便。

---

### 4.4 协同：周期性读取与禁用立即回 Idle

#### 4.4.1 概念说明

把 4.1～4.3 三个机制合起来看，`Trig`、超时、`Enable` 并不是三个独立功能，而是**同一个「是否产生 `Start`」决策的三个输入维度**：

- **来源维度**：`Trig`（外部脉冲）或 超时（内部沙漏），二者或运算；
- **许可维度**：`Enable`（软件总开关），对来源做门控；
- **时机维度**：`Start` 只在 `Idle_s` 生效，进行中的周期对新触发免疫。

这套组合带来两个常用的工程套路：① **纯周期性读取**——不接 `Trig`，只靠超时；② **安全改配置**——先 `Enable=0`（立即回 Idle 且冻结沙漏），改 RegTable/RegCnt，再 `Enable=1`。

#### 4.4.2 核心流程

把三个机制叠在一拍里的决策伪代码：

```
v.Start := 0
fire = (Trig==1) OR (TimeoutCnt == TimeoutCkCycles_g-1)   # 来源
if fire:
    if Enable==1:                        # 许可
        Start := 1
        TimeoutCnt := 0
    # Enable==0 时：什么都不做（但位置B会兜底）
else:
    TimeoutCnt += 1
... FSM case ...
if Enable==0:                             # 时机/兜底
    Fsm := Idle_s
    TimeoutCnt := 0
```

纯周期性读取的时序（`Trig` 悬空 = 恒 0）：

```
使能 ──> [数 TimeoutCkCycles_g 拍] ──> Start ──> 读周期 ──> DoneIrq ──> 回 Idle
                                          ^                                    │
                                          └──────────── 再次数满 ─────────────┘
```

#### 4.4.3 源码精读

「纯周期性读取」的依据就藏在默认值里——核心 `Trig` 端口默认 `'0'`：[hdl/axi_mm_reader.vhd:36](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L36)，wrapper 的 `Trig` 端口同样默认 `'0'`：[hdl/axi_mm_reader_wrp.vhd:48](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L48)。因此 `Trig` 悬空时触发块里 `Trig='1'` 永不成立，`Start` 只可能来自超时那一支。

「立即回 Idle」的依据即 4.3.3 的位置 B：[hdl/axi_mm_reader.vhd:156-159](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L156-L159)。

周期性读取的端到端验证即 4.2.4 引用的「Timeout」用例（StimCase 3）。

#### 4.4.4 代码实践

**实践目标**：把三个机制串起来，预测一个真实场景的时间线。

**场景**：测试台配置（`TimeoutUs_g => 10`，100 MHz → 1000 拍超时）。软件在 `t=0` 使能 IP，`Trig` 全程悬空。

**操作步骤**：

1. 按 4.4.2 的时序图，预测 `m_axis_tvalid` 第一次拉高的时刻（约 `t = 10 µs`）。
2. 预测若在 `t=5 µs` 软件突然写 `Enable=0`，会发生什么：位置 B 立即把 FSM 拉回 Idle、沙漏清零；原定 `t=10 µs` 的超时**不会**出现。这正对应测试台「Disabled」用例里 `CheckNoActivity(..., 12 us, ...)` 通过的事实。
3. 预测若随后在 `t=20 µs` 重新使能，下一次数据出现在何时：沙漏从 `t=20 µs` 起从 0 重数，约 `t=30 µs` 出现。

**需要观察的现象**：使能、禁用、再使能三个动作对 `m_axis_tvalid` 节奏的精确影响。

**预期结果**：与步骤 1～3 的预测一致；可用测试台 StimCase 3（超时）与 StimCase 4（禁用）两段拼起来对照。

**待本地验证**：建议在仿真里把 `Enable`（`reg_wdata(Ctrl)(Ena)`）、`Trig`、`m_axis_tvalid`、`DoneIrq` 同窗观察，手动对齐上述时间点。

#### 4.4.5 小练习与答案

**练习 1**：若用户既接了 `Trig`（每 50 µs 一拍）又配了 100 µs 超时，实际读周期频率是多少？
**答案**：以 `Trig` 为主——每次 `Trig` 都会启动并清沙漏，所以沙漏永远数不到 100 µs 就被清零，读周期跟随 `Trig` 每 50 µs 一次。超时只在 `Trig`「漏拍」超过 100 µs 时才兜底。

**练习 2**：文档 [doc/Documentation.md:26](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L26) 说「进行中的 trigger 脉冲会被忽略」。结合本讲源码，这个「忽略」具体是哪几行实现的？
**答案**：两处合起来实现：① FSM 只在 `Idle_s` 检查 `r.Start`（[hdl/axi_mm_reader.vhd:120-123](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L120-L123)），其他状态对 `r.Start` 视而不见；② `v.Start` 每拍默认清零（第 107 行），使 `Trig` 只产生一拍 `r.Start`、不排队。两者缺一不可。

## 5. 综合实践

**任务**：用一句话＋一张时间线图，把本讲三个机制讲清楚，并用测试台两个真实用例佐证。

1. 画出下面六个信号在时间轴上的关系（手绘即可）：`Enable`、`Trig`、`TimeoutCnt`（示意，标注「到点」时刻）、`Start`（单拍）、FSM 状态、`DoneIrq`。时间窗口覆盖：使能 → 超时启动一次周期 → 周期进行中误来一拍 `Trig`（被忽略）→ 周期结束发 `DoneIrq` → 禁用立即回 Idle。
2. 在图上**标出每段行为由哪一行代码负责**，至少引用：触发块（106-115）、`Idle_s` 离场（120-123）、`Enable` 全局覆盖（156-159）。
3. 用 [tb/top_tb.vhd:293-311](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L293-L311)（超时用例）佐证「超时自启动」，用 [tb/top_tb.vhd:313-322](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L313-L322)（禁用用例）佐证「禁用即停且冻结沙漏」。
4. 回答：若把 `TimeoutUs_g` 设得比一次最长读周期还短，会出现什么现象？（提示：回顾 4.2.4 末尾的「进阶细节」——沙漏会在读周期内反复到点、反复清零，并产生被忽略的 `Start`，系统行为依然正确但有抖动。）

**交付物**：一张带行号标注的时间线图 ＋ 一段不超过 5 行的文字结论。**待本地验证**：若条件允许，跑一次仿真对照你的时间线。

## 6. 本讲小结

- 读周期有**两种启动来源**：外部 `Trig` 单拍脉冲、内部 `TimeoutCnt` 超时到点；二者在触发块里做「或」运算，统一产生寄存信号 `Start`（[hdl/axi_mm_reader.vhd:106-115](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L106-L115)）。
- `Start` 只在 `Idle_s` 被消费（[hdl/axi_mm_reader.vhd:120-123](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L120-L123)），加上 `v.Start` 每拍默认清零，共同保证「进行中的 `Trig` 被忽略、不排队」。
- 超时把「微秒」换算成「周期数」在 wrapper 完成：`TimeoutCkCycles_c = ⌊ClkFrequencyHz × TimeoutUs_g / 10⁶⌋`（[hdl/axi_mm_reader_wrp.vhd:134](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L134)）；100 MHz / 100 µs → 10000 拍。
- `Enable` 起三重门控：触发块内层挡住 `Start` 的产生（[hdl/axi_mm_reader.vhd:108-112](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L108-L112)），case 之后的全局覆盖把 FSM 硬拉回 `Idle_s` 并冻结沙漏（[hdl/axi_mm_reader.vhd:156-159](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L156-L159)）。
- 因为 `Trig` 端口默认 `'0'`（核心与 wrapper 都是），**不接 `Trig`、只配超时**即可实现周期性读取，对应测试台 StimCase 3。
- 禁用 → 改配置 → 使能，是安全的工程模式；测试台 StimCase 4 证明了禁用期间 `Trig` 与超时双失效。

## 7. 下一步学习建议

- 想看「`Start` 之后 FSM 如何逐条发起 AXI 读」的握手细节，进入 [u2-l6 AXI 主机读取通路](u2-l6-axi-master-read.md)，重点看 `SetCmd_s`/`ApplyCmd_s` 与 `CmdRd`/`RdDat` 通道如何配合。
- 想理解 `Enable`/`RegCnt`/`RegTable` 这些配置如何从 AXI 事务到达核心端口，进入 [u2-l5 AXI 从机配置接口](u2-l5-axi-slave-wrapper.md)。
- 想从软件侧复现「禁用→改配置→使能→读 FIFO」的完整套路，进入 [u3-l1 C 软件驱动](u3-l1-c-driver.md)，对照 `SetEnable`/`SetRegTable`/`ReadFifoPacket` 的错误码（`IpMustBeDisabled` 等）。
- 建议同步阅读 `tb/top_tb.vhd` 的「Buffered Double Read」（[top_tb.vhd:279-291](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L279-L291)）与「Back Pressure」（[top_tb.vhd:324-336](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L324-L336)）用例，加深对「触发节奏 × FIFO 缓冲」的理解。
