# 触发与斜率子状态机

## 1. 本讲目标

本讲深入 `TOP.v` 中**运行在 ADC 时钟域**的第二个状态机 `state2`，以及主状态机里的触发判别状态 `trig_state`。学完后你应当能够：

- 说清 `state2`（s1~s4）为何要独立于主 FSM `state`、用 `clock_adc_out` 时钟运行，以及它**同时承担的两件职责**：把采样写入 ram1、连续计算信号斜率 `slope`。
- 复述触发判别的三要素：**电平相等 + 斜率方向匹配 + 超时强制**，并能用 `trig_state` 的源码逐条解释。
- 推导 `Transcoder` 把 4 位 `timebase` 译成 16 位 `out_trans`，再由 `state2` 里的 `divider` 实现「每 N 拍存一个样本」的可变时基采样，并算出 `ADR` 写地址的真实递增节奏。

本讲是 u5-l1（主状态机）的延续：主 FSM 只负责「何时开始采集」，而**真正在 ADC 节拍下一边存样本、一边判斜率**的，是本讲的 `state2`。

## 2. 前置知识

- **时钟域（clock domain）**：一组由同一个时钟驱动的寄存器。跨时钟域传递信号需要同步器，否则可能产生亚稳态。本项目里 `state`（主 FSM）跑在 200 MHz 的 `clk`，而 `state2` 跑在 ADC 转换时钟 `clock_adc_out` 上，二者是不同时钟域。
- **触发（trigger）**：示波器在信号满足某个条件（通常是「穿过某个电平」）时才开始记录波形，目的是让屏幕上的波形稳定。边沿触发还要指定**上升沿**或**下降沿**。
- **斜率（slope）**：这里不是数学导数，而是一个 1 位标志，表示「信号相对于上一个样本是上升还是下降」。本讲里 `slope=1` 表示上升、`slope=0` 表示下降或持平。
- **时基（timebase）**：示波器水平方向的采样间隔设置。本项目用一个 4 位的 `timebase` 选择不同的分频比，从而改变有效采样率。
- **`out_trans`**：`Transcoder` 模块的输出，是一个 16 位的「分频设定值」，告诉 `state2`「每隔多少个 ADC 时钟拍才把写地址 +1」。详见 u2-l1。
- 承接 u2-l1（时钟域与 `Transcoder`）、u2-l3（`clock_adc_out` 的产生）、u5-l1（主 FSM 的 `trig_state`/`acq_state` 与 P/A/B/C/D 命令协议）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`verilog files/TOP.v`](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v) | 顶层。本讲关注三段：`state2` 的 always 块、主 FSM 里的 `trig_state`、以及 `wait_state` 中与触发/时基相关的命令解析。 |
| [`verilog files/Transcodor.v`](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/Transcodor.v) | `Transcoder` 模块（注意文件名拼写为 `Transcodor`）。把 4 位 `timebase` 译成 16 位单热式 `out_trans` 分频值。 |

> 命名提醒：文件叫 `Transcodor.v`、模块叫 `Transcoder`、顶层例化名叫 `t1`、输出网线叫 `out_trans`——四者又各不相同。读代码一律以 `module` 关键字后的名字为准（见 u1-l2、u2-l2 的命名陷阱提醒）。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **`state2` 子状态机总览**——它在 ADC 时钟域独立运行，是「采集」与「算斜率」的双职责机器。
2. **触发与斜率判别**——`slope` 怎么算出来，`trig_state` 怎么用电平 + 斜率 + 超时三件套决定何时进入采集。
3. **可变时基寻址**——`Transcoder` + `state2` 里的 `divider` 如何把 `timebase` 变成真实的采样间隔，驱动 `ADR` 写地址递增。

### 4.1 state2：ADC 时钟域的独立采集子状态机

#### 4.1.1 概念说明

主 FSM `state` 跑在 200 MHz 的 `clk` 上，节奏很快，但它**并不直接看 ADC 数据**。ADC 芯片是按自己的转换时钟 `clock_adc_out` 吐出 `adc_read[9:0]` 的（见 u2-l3）。如果用 200 MHz 去采样 ADC 数据，会与 ADC 的真实更新节拍错位，要么重复读同一个样本、要么漏读。

于是作者另起了一个小型状态机 `state2`，**直接用 `clock_adc_out` 作时钟**（`always @(posedge clock_adc_out)`）。这样它的每一次跳变都精准对齐「一个新 ADC 样本到来」的时刻。`state2` 只干两件事：

- **采集时**（主 FSM 处于 `acq_state`）：把 `adc_read` 写进 ram1，并通过递增写地址 `ADR` 决定「每隔多少拍存一个样本」（即可变时基）。
- **非采集时**（主 FSM 处于 `wait_state`/`trig_state`）：不断对比相邻两个样本，算出信号当前是在上升还是下降，把结果写进 1 位寄存器 `slope`，供触发判别使用。

也就是说，`state2` 是一台「**和 ADC 同呼吸**」的小机器，把时序敏感的样本处理从主 FSM 里剥离出来，让主 FSM 可以专心做宏观调度。

#### 4.1.2 核心流程

`state2` 有四个状态（参数定义见源码）：

```
        ┌───────────────────────── acq_state 期间 ─────────────────────────┐
        │                                                                  │
        │   s1 ──(state==acq_state)──> s2 ──(离开 acq)──> s1                │
        │    ^                          │                                  │
        │    │                          │ 每 (out_trans+1) 拍：ADR+1        │
        │    └──────────────────────────┘                                  │
        └──────────────────────────────────────────────────────────────────┘
        ┌───────────────────────── 非采集期间（wait/trig）──────────────────┐
        │                                                                  │
        │   s1 ──> s3 ──> s4 ──> s1（循环，每 3 拍更新一次 slope）          │
        │  捕获      捕获     比较                                          │
        │  trig1    trig2    slope=sign(trig2-trig1)                       │
        └──────────────────────────────────────────────────────────────────┘
```

`s1` 是唯一的入口/分派状态：它看主 FSM 是否在 `acq_state`，是则走采集分支 `s2`，否则走斜率分支 `s3→s4`。两条分支最终都回到 `s1`。

#### 4.1.3 源码精读

先看状态参数与寄存器声明：

[s1~s4 参数定义 — TOP.v:230-233](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L230-L233) 定义了 `s1=00 / s2=01 / s3=10 / s4=11` 四个编码。

[state2 寄存器声明 — TOP.v:240](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L240) 声明 `reg [1:0] state2=2'b00;`，上电即 `s1`。

与触发/斜率/时基相关的寄存器集中在一处：

[触发与斜率相关寄存器 — TOP.v:78-89](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L78-L89) 声明了 `trig_value`（8 位触发电平）、`trig1/trig2`（两个 8 位样本缓冲）、`slope`（1 位斜率标志）、`slope_adj`（1 位期望斜率方向）、`timebase`（4 位时基选择）、`trigger_counter`（30 位超时计数器）、`divider`（16 位分频计数器）、`out_trans`（16 位分频设定值）等。

> 注意第 78 行 `reg [7:0] trig_value=8'b1000000000;` 是一处源码笔误：位宽标 8 却写了 10 位字面量，综合时会截断。不过它在 [`init_state` — TOP.v:251-261](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L251-L261) 里被重新赋值为 `8'b10000000`（=128，8 位中点），所以运行时的实际初值是 128，笔误不影响行为。

整个 `state2` 状态机在一段独立的 always 块里：

[state2 always 块（ADC 时钟域） — TOP.v:461-495](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L461-L495) 用 `posedge clock_adc_out` 触发，与主 FSM 的 `posedge clk` 完全解耦。注释也写明：「This state machine is just for storing data from adc and for slope computation」。

逐状态看：

**s1（分派）** — [TOP.v:464-472](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L464-L472)：捕获当前样本到 `trig1`，把写地址 `ADR` 清零、`divider` 清零；然后看主 FSM——在 `acq_state` 就去 `s2`，否则去 `s3`。

**s2（采集寻址）** — [TOP.v:474-482](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L474-L482)：只要主 FSM 一离开 `acq_state` 就回 `s1`；否则按 `out_trans` 决定的间隔让 `ADR+1`（细节见 4.3）。

**s3（捕获第二样本）** — [TOP.v:484-487](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L484-L487)：把当前样本存进 `trig2`，下一拍去 `s4`。

**s4（比较出斜率）** — [TOP.v:489-493](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L489-L493)：`if(trig2>trig1) slope<=1; else slope<=0;` 回 `s1`。于是 `slope` 反映「上一拍到这一拍信号是升还是降」。

> **跨时钟域提示**：`state2` 读 `state==acq_state`（`state` 属 `clk` 域）、而 `slope` 又被主 FSM 的 `trig_state`（`clk` 域）读取。这里两个方向都**没有显式同步器**。1 位的 `slope` 跨域相对 benign；但 5 位的 `state` 跨域被解码比较，理论上存在亚稳态/毛刺风险。这是本工程一处「能跑但不够稳健」的工程细节，值得在二次开发时加固（例如加 2 级触发器同步，或把判别统一到一个时钟域）。

#### 4.1.4 代码实践

**实践目标**：通过源码阅读，确认 `state2` 的两条分支何时切换、`slope` 的更新周期。

**操作步骤**：

1. 打开 [TOP.v:461-495](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L461-L495) 的 `state2` 块。
2. 假设主 FSM 当前停在 `wait_state`（即 `state != acq_state`）。手工模拟 `state2`：从 `s1` 出发，列出每一拍 `clock_adc_out` 上升沿后 `state2`、`trig1`、`trig2`、`slope` 的取值，连续走 6 拍。
3. 再假设主 FSM 进入 `acq_state`，重复手工模拟，观察 `state2` 是否进入 `s2`、`ADR` 是否开始递增。

**需要观察的现象**：

- 非采集期：`state2` 在 `s1→s3→s4` 之间循环，**每 3 个 `clock_adc_out` 拍刷新一次 `slope`**。
- 采集期：`state2` 停在 `s2`，`ADR` 持续递增（递增节奏由 4.3 决定）。

**预期结果**（非采集期前 6 拍，设初始 `state2=s1`）：

| 拍 | 进入状态 | 动作 | 下一状态 |
| --- | --- | --- | --- |
| 1 | s1 | `trig1←样本A`、`ADR←0` | s3 |
| 2 | s3 | `trig2←样本B` | s4 |
| 3 | s4 | `slope←(B>A?1:0)` | s1 |
| 4 | s1 | `trig1←样本C` | s3 |
| 5 | s3 | `trig2←样本D` | s4 |
| 6 | s4 | `slope←(D>C?1:0)` | s1 |

可见 `slope` 每三拍才更新一次，比较的是「相隔一个 ADC 时钟拍」的两个样本（A 与 B、C 与 D）。具体数值需在硬件/仿真上验证，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `state2` 不和主 FSM 一样用 `clk`（200 MHz）作时钟？

**参考答案**：因为 ADC 数据 `adc_read` 是按 `clock_adc_out` 的节拍更新的（见 u2-l3）。用 200 MHz 采样会与 ADC 的真实更新沿错位，可能重复读或漏读样本；让 `state2` 直接吃 `clock_adc_out`，每一次状态跳变就精准对应一个新样本到来。

**练习 2**：`s1` 状态里有一句 `if(state==acq_state) state2<=s2; else state2<=s3;`。如果把这句改成「无条件去 `s2`」，系统会出什么问题？

**参考答案**：那就再也不会走 `s3→s4`，`slope` 永远停留在初值，`trig_state` 的斜率判别（见 4.2）就失效，边沿触发退化为纯电平触发；同时非采集期 `ADR` 也会被不停清零/递增，干扰下一次采集的起始地址。

---

### 4.2 触发与斜率判别

#### 4.2.1 概念说明

主 FSM 收到 `P` 命令后从 `wait_state` 跳到 `trig_state`（见 u5-l1），但**并不是立刻开始采集**。它要等信号满足触发条件才进入 `acq_state`。触发条件由三件事合取：

1. **电平条件**：当前样本的高 8 位 `adc_read[9:2]` 等于触发电平 `trig_value`。
2. **斜率条件**：刚刚由 `state2` 算出的 `slope` 等于期望方向 `slope_adj`。
3. **超时兜底**：如果等了太久还没碰到满足条件的时刻，就别等了，强制开始采集。

这套逻辑对应示波器里最常见的「带边沿选择的电平触发 + 自动触发（auto trigger）超时」。

`slope_adj` 的含义由 PC 命令 `C` 设置（见 u5-l1 的命令协议）：

- 收到 `C` 后再收到 `'H'`（0x48）→ `slope_adj=1` → **要求上升沿**（因为 `slope=1` 表示上升）。
- 收到 `C` 后再收到 `'L'`（0x4C）→ `slope_adj=0` → **要求下降沿**（`slope=0` 表示下降或持平）。

触发电平 `trig_value` 由命令 `B` 设置（8 位，直接来自参数字节）。

#### 4.2.2 核心流程

`trig_state` 的判定逻辑（伪代码）：

```
每个 clk（200 MHz）上升沿：
    if (adc_read[9:2] == trig_value):
        # 信号正好在触发电平上
        if (slope == slope_adj):
            -> acq_state            # 电平对、方向也对：触发！
        else:
            -> 留在 trig_state，且 trigger_counter 冻结
    else:
        # 信号不在触发电平上
        if (trigger_counter == 2097151):   # 2^21 - 1
            -> acq_state                    # 超时：强制触发
        else:
            trigger_counter += 1
```

超时阈值是一个 30 位字面量 `30'b000000000111111111111111111111`（9 个 0、21 个 1），数值为 \(2^{21}-1 = 2\,097\,151 \)。主 FSM 跑在 200 MHz，故超时时间约为

\[
T_{\text{timeout}} \approx \frac{2^{21}-1}{200\times10^{6}} \approx 10.49\ \text{ms}
\]

即「约 10.5 毫秒内等不到合适触发就强制采集」。这让示波器在信号罕见或触发电平设错时，也不会无限卡死。

> 一个值得注意的细节：`trigger_counter` **只在信号不在触发电平上时**才递增。如果信号恰好长期停留在触发电平、但斜率方向不对，计数器会一直冻结，超时反而不触发。这是源码的实际行为。

#### 4.2.3 源码精读

[trig_state 源码 — TOP.v:316-327](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L316-L327) 是整个触发判别的核心。其上方的 [注释 — TOP.v:313-315](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L313-L315) 也讲明了意图：「evaluate the difference of two consecutive samples ... When the signal equals a certain value on the preferred slope, the acquisition process is started. If the trigger is not found, the system waits a time and starts the acquisition anyway.」

几个要点：

- `adc_read[9:2]==trig_value`：比较只取 `adc_read` 的高 8 位，丢弃低 2 位。这是一种「粗比较」，相当于在 1024 级 ADC 码上以 256 级精度判电平，能容忍低 2 位的抖动。
- `if(slope==slope_adj) state<=acq_state;`：电平对了之后，还要斜率方向对得上才真正触发。
- 超时分支里 `trigger_counter` 与 `30'b...111` 比较，到顶后清零并强制 `acq_state`。

`trig_value` 与 `slope_adj` 的配置入口在 `wait_state`：

[B 命令设触发电平 — TOP.v:290-293](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L290-L293)：`trig_value<=dserial_in;`（直接把参数字节当 8 位电平）。

[C 命令设斜率方向 — TOP.v:295-299](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L295-L299)：`'L'`→`slope_adj=0`（下降沿），`'H'`→`slope_adj=1`（上升沿）。

`trigger_counter` 在每次进入 `wait_state` 时被清零，保证下一次触发的超时窗口重新开始：

[wait_state 清零 trigger_counter — TOP.v:269](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L269)。

#### 4.2.4 代码实践

**实践目标**：亲手用 `slope` 与 `slope_adj` 解释上升/下降沿触发，并说明 `trigger_counter` 超时的作用。

**操作步骤**：

1. 阅读 [trig_state — TOP.v:316-327](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L316-L327)。
2. 假设 PC 已经发过 `C` 然后 `H`（即 `slope_adj=1`，要求上升沿），`trig_value=128`（中点）。现在有一个正弦信号从小到大穿过中点。请回答：当 `adc_read[9:2]` 第一次等于 128 时，`slope` 大概率是 0 还是 1？系统能否立刻触发？
3. 把第 2 步里的 `H` 换成 `L`（`slope_adj=0`，下降沿），重做判断。
4. 假设信号是一个恒定的直流电平、且恰好不等于 `trig_value`。持续观察 `trigger_counter`，估算大约多久后系统会强制采集。

**需要观察的现象**：

- 上升沿设置下，信号上升穿过 `trig_value` 的那一刻（`slope=1`）才触发；下降穿越时（`slope=0`）不触发。
- 下降沿设置则相反。
- 长时间不满足条件时，约 10.5 ms 后强制进入 `acq_state`。

**预期结果**：

| 设置 (`slope_adj`) | 信号穿越方向 | `slope` | 是否触发 |
| --- | --- | --- | --- |
| 1（上升沿，`H`） | 向上穿过 trig_value | 1 | 是 |
| 1（上升沿，`H`） | 向下穿过 trig_value | 0 | 否（继续等/超时） |
| 0（下降沿，`L`） | 向下穿过 trig_value | 0 | 是 |
| 0（下降沿，`L`） | 向上穿过 trig_value | 1 | 否（继续等/超时） |

超时强制采集的作用：保证在「信号罕见」「触发电平设错」「斜率方向设反」等情况下，示波器仍能定期刷新画面，不至于永久卡在 `trig_state`。具体超时时长（≈10.5 ms）**待本地验证**（取决于 `clk` 实际频率是否精确 200 MHz）。

#### 4.2.5 小练习与答案

**练习 1**：为什么触发比较用 `adc_read[9:2]`（高 8 位）而不是完整的 10 位？

**参考答案**：丢弃低 2 位相当于把 1024 级量化「粗化」成 256 级，使触发电平比较对低 2 位的噪声/抖动不敏感，避免信号在电平附近抖动时反复误触发。同时也让 8 位的 `trig_value` 与 `trig1/trig2` 位宽一致。

**练习 2**：把超时阈值从 \(2^{21}-1\) 改成 \(2^{16}-1\)，对示波器使用体验会有什么影响？

**参考答案**：超时窗口从约 10.5 ms 缩短到约 0.33 ms。在信号稀疏或触发条件难满足时，强制采集会更频繁地发生，画面刷新更快但波形更难稳定（更接近「自动/滚动」模式）；反之加大阈值则更「耐心」地等待真正触发，波形更稳但刷新更慢。

---

### 4.3 可变时基：Transcoder + divider 分频寻址

#### 4.3.1 概念说明

示波器需要在「看高频细节」（快采样）和「看长时间趋势」（慢采样）之间切换，这由 4 位的 `timebase` 控制。本工程用两级机制把 `timebase` 变成真实的采样间隔：

- **第一级（粗）**：`timebase==0` 时，`wait_state` 把 `adc_div_sel=1`，`ADC_clock_mux` 选 200 MHz、再经 `read_adc` 二分频，得到 `clock_adc_out=100 MHz`（这就是项目名里的 100 MSPS 快档）；`timebase!=0` 时 `adc_div_sel=0`，选 50 MHz 二分频得 25 MHz 慢档（见 u2-l1、u2-l3）。
- **第二级（细）**：`Transcoder` 把 `timebase` 译成 16 位 `out_trans`，`state2` 的 `s2` 用一个小计数器 `divider` 实现「每 `out_trans+1` 拍才把写地址 `ADR+1`」，从而在 `clock_adc_out` 之上再做一次软件分频。

也就是说，`timebase` 同时影响「ADC 时钟快慢」和「写地址递增间隔」两层，最终的有效采样率为

\[
f_{\text{sample}} = \frac{f_{\text{clock\_adc\_out}}}{\text{interval}},\qquad
\text{interval} = \begin{cases}1 & \text{当 } out\_trans = 0\\ out\_trans + 1 & \text{当 } out\_trans \neq 0\end{cases}
\]

#### 4.3.2 核心流程

**Transcoder 译码**（纯组合逻辑 `always @(*)`）：把 4 位 `in` 译成 16 位单热式 `out`：

| `timebase` | `out_trans`（16 位） | `interval = out_trans+1` |
| --- | --- | --- |
| 0 | `0x0000` | 1（特例：每拍都存） |
| 1 | `0x0002` | 3 |
| 2 | `0x0004` | 5 |
| 3 | `0x0008` | 9 |
| 4 | `0x0010` | 17 |
| 5 | `0x0020` | 33 |
| 6 | `0x0040` | 65 |
| 7 | `0x0080` | 129 |
| 8 | `0x0100` | 257 |
| 9 | `0x0200` | 513 |
| 10 (0xA) | `0x0400` | 1025 |
| 11 (0xB) | `0x0800` | 2049 |
| 12 (0xC) | `0x1000` | 4097 |
| 13 (0xD) | `0x2000` | 8193 |
| 14 (0xE) | `0x4000` | 16385 |
| 15 (0xF) | `0xFFFF` | 65536 |

可见 `timebase` 1~14 对应 `out_trans = 2^timebase`，间隔约为 \(2^{\text{timebase}}+1\)；`timebase=0` 是最快的特例；`timebase=15` 把分频器塞满（间隔 65536）。

**s2 里的 divider 循环**（决定 `ADR` 何时 +1）：

```
s2:
  if (out_trans == 0):           # 最快档
      ADR <= ADR + 1             # 每个 clock_adc_out 拍都 +1
  else if (divider == out_trans):
      ADR <= ADR + 1             # 数到头，存一个样本
      divider <= 0
  else:
      divider <= divider + 1     # 继续数
```

`divider` 从 0 数到 `out_trans` 共经历 `out_trans+1` 拍，正好对应上表的 `interval`。注意「+1」的来源：判据是 `divider==out_trans` 而非 `divider==out_trans-1`，所以多了一拍。

#### 4.3.3 源码精读

[Transcoder 译码表 — Transcodor.v:6-30](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/Transcodor.v#L6-L30) 用一个 `case(in)` 把 4 位输入一一映射到 16 位单热输出。`0xF` 是特例，输出全 1（`0xFFFF`）。

顶层例化（注意命名四重奏）：

[Transcoder 例化为 t1 — TOP.v:210-211](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L210-L211)：`Transcoder t1(.in(timebase), .out(out_trans));`——`timebase`→`out_trans`。

`timebase` 的配置入口（`A` 命令）：

[A 命令设时基 — TOP.v:285-288](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L285-L288)：`timebase<=dserial_in[5:2];`——取参数字节的 bit5~bit2 作为 4 位时基。

`adc_div_sel`（第一级粗分频选择）的设置：

[wait_state 选 ADC 时钟源 — TOP.v:267-268](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L267-L268)：`timebase==0` 选 200 MHz（快档），否则选 50 MHz（慢档）。

`s2` 的分频寻址主体：

[s2 分频递增 ADR — TOP.v:474-482](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L474-L482)。结合上面伪代码即可读懂。

`ADR` 最终喂给 ram1 的写地址端口：

[ram1 例化，addr=ADR — TOP.v:128-134](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L128-L134)：`SRAM ram_adc(.clk(clk), .addr(ADR), .we(we), .data_in(adc_read), ...)`。

> **写使能与地址分属不同时钟域**：ram1 在 `clk`（200 MHz）域写、写使能 `we` 由主 FSM 在 `acq_state` 拉高；而写地址 `ADR` 在 `clock_adc_out` 域递增。由于 `clock_adc_out` 远慢于 `clk`，且 `ADR` 在两次递增之间保持稳定，ram1 会在同一地址上被连续写多次（都是同一个 `adc_read` 值），直到 `ADR` 切换——等价于「每个 ADC 样本被可靠锁存一次」。这是一种用「高速重复写」规避跨域同步的实用写法，但同样属于「能跑但不够严谨」的细节。

#### 4.3.4 代码实践

**实践目标**：标出 `state2` 中 `ADR` 写地址如何随 `out_trans` 分频递增，并算出几种 `timebase` 下的有效采样率。

**操作步骤**：

1. 对照 [s2 — TOP.v:474-482](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L474-L482) 与 [Transcoder 表 — Transcodor.v:11-28](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/Transcodor.v#L11-L28)，填写下表（第一行已给出）。

| `timebase` | `clock_adc_out` | `out_trans` | `interval` | 有效采样率 |
| --- | --- | --- | --- | --- |
| 0 | 100 MHz | 0x0000 | 1 | 100 MSPS |
| 1 | ? | 0x0002 | ? | ? |
| 4 | ? | ? | ? | ? |
| 14 | ? | ? | ? | ? |
| 15 | ? | 0xFFFF | 65536 | ? |

2. 对 `timebase=1`，手工模拟 `s2` 里 `divider` 的取值序列，确认 `ADR` 确实每 3 个 `clock_adc_out` 拍 +1。

**需要观察的现象**：

- `timebase=0` 是唯一达到 100 MSPS 的档位；其余档位都在 25 MHz 之上再分频，采样率随 `timebase` 指数级下降。
- `divider` 序列为 `0,1,2,0,1,2,...`（`out_trans=2` 时），每完成一个 `0→1→2` 循环 `ADR` 才 +1。

**预期结果**（待本地验证精确时钟频率）：

| `timebase` | `clock_adc_out` | `out_trans` | `interval` | 有效采样率 |
| --- | --- | --- | --- | --- |
| 0 | 100 MHz | 0x0000 | 1 | 100 MSPS |
| 1 | 25 MHz | 0x0002 | 3 | ≈8.33 MSPS |
| 4 | 25 MHz | 0x0010 | 17 | ≈1.47 MSPS |
| 14 | 25 MHz | 0x4000 | 16385 | ≈1.526 kHz |
| 15 | 25 MHz | 0xFFFF | 65536 | ≈381 Hz |

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Transcoder` 在 `timebase=0` 时输出 `0x0000`，而 `s2` 里要专门用一句 `if(out_trans==0) ADR<=ADR+1;` 来处理？

**参考答案**：若直接套用 `divider==out_trans` 的逻辑，`out_trans=0` 时 `divider` 初值就是 0、立刻相等，行为上也是每拍 +1——但代码作者显然希望「最快档」语义清晰、不依赖「初值即相等」这种边界巧合，于是单独写一条无条件递增分支，既好读又避免歧义。

**练习 2**：`s2` 的判据是 `if(divider==out_trans)`，如果改成 `if(divider==out_trans-1)`，采样率会怎么变？

**参考答案**：`divider` 会少走一拍，`interval` 从 `out_trans+1` 变成 `out_trans`，采样率略升（例如 `timebase=1` 时 interval 从 3 变 2，采样率从 8.33 MSPS 升到 12.5 MSPS）。这是典型的「差一」误差，也是本讲提醒读者注意 `interval = out_trans+1` 的原因。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「端到端」的触发与采集时序推演。

**任务**：假设 PC 依次发送命令序列 `A`、`0x08`、`B`、`0x80`、`C`、`H`、`P`（即：设 `timebase=2`、触发电平=128、上升沿触发、开始）。请回答：

1. 收完前三个命令后，`timebase`、`trig_value`、`slope_adj` 各是多少？`adc_div_sel` 是 0 还是 1？`clock_adc_out` 是 100 MHz 还是 25 MHz？
2. 收到 `P` 后主 FSM 进入 `trig_state`。此时 `state2` 在哪个状态循环？`slope` 多久更新一次？
3. 设信号是一个缓慢上升的斜坡，最终向上穿过 128。在穿越那一刻，`adc_read[9:2]==trig_value` 与 `slope==slope_adj` 能否同时成立？系统接下来进入哪个状态？
4. 进入 `acq_state` 后，`state2` 切到 `s2`。`out_trans` 是多少？`ADR` 每隔多少个 `clock_adc_out` 拍递增一次？ram1 以怎样的有效采样率被填满？
5. （进阶）如果把 `C` 后的字符从 `H` 换成 `L`，第 3 步的结论会怎样改变？若信号一直不满足触发条件，大约多久后系统会强制采集？

**参考答案要点**：

1. `timebase=2`（`0x08` 的 bit5~2 = `0010`=2）；`trig_value=0x80=128`；`slope_adj=1`；`timebase≠0` 故 `adc_div_sel=0`，`clock_adc_out=25 MHz`。
2. `state2` 在 `s1→s3→s4` 循环；`slope` 每 3 个 `clock_adc_out` 拍更新一次。
3. 上升斜坡向上穿过 128 时，`adc_read[9:2]` 会等于 128、且信号在上升（`slope=1=slope_adj`），条件同时成立，进入 `acq_state`。
4. `timebase=2` → `out_trans=0x0004` → `interval=5`；`ADR` 每 5 个 `clock_adc_out` 拍 +1；有效采样率 ≈25 MHz/5 = 5 MSPS。ram1 在 `carry` 拉高（写满 2048 项）后由主 FSM 切到 `fft_state`（见 u5-l1、u2-l2）。
5. 改成 `L`（`slope_adj=0`）后，上升穿越时 `slope=1≠0`，不触发；要等信号下降穿越才触发。若始终不满足，约 10.5 ms 后 `trigger_counter` 到顶，强制进入 `acq_state`。

（以上数值依赖 `clk`/`clock_adc_out` 的精确频率，**待本地验证**。）

## 6. 本讲小结

- `state2`（s1~s4）是**运行在 ADC 时钟域 `clock_adc_out` 的独立小状态机**，与主 FSM `state`（`clk` 域）解耦，专门处理「与 ADC 同步」的时序敏感任务。
- 它有**双重职责**：采集期（`acq_state`）在 `s2` 里按可变间隔递增 `ADR`、把样本写入 ram1；非采集期在 `s1→s3→s4` 循环里比较相邻样本、刷新 1 位 `slope`。
- `slope=1` 表示信号上升、`slope=0` 表示下降或持平；它由 `trig2>trig1` 决定，每 3 个 ADC 拍更新一次。
- 触发判别在主 FSM 的 `trig_state` 里完成，是「**电平相等 (`adc_read[9:2]==trig_value`) 且斜率方向匹配 (`slope==slope_adj`)**」的合取；`slope_adj` 由 `C` 命令 + `'H'/'L'` 设置成上升/下降沿。
- 超时兜底：`trigger_counter` 数到 \(2^{21}-1\)（约 10.5 ms @200 MHz）后强制 `acq_state`，避免示波器卡死；但它只在信号不在触发电平上时才计数。
- 可变时基由两级分频实现：`timebase` 经 `Transcoder` 译成 `out_trans`，`s2` 用 `divider` 实现「每 `out_trans+1` 拍存一个样本」；`timebase=0` 是唯一 100 MSPS 档，其余档位在 25 MHz 之上再分频。
- 命名陷阱：文件 `Transcodor.v` ↔ 模块 `Transcoder` ↔ 例化 `t1` ↔ 网线 `out_trans`；`clock_adc_out`（驱动 ADC 芯片与 `state2`）与 `clock_adc_in`（MUX 输出）名字相近、方向直觉相反，需对照 u2-l1/u2-l3 理清。
- 工程细节提醒：`state`↔`state2`、`slope`、`ADR` 都跨时钟域且无显式同步器，属「能跑但不够稳健」，是二次开发时可加固之处。

## 7. 下一步学习建议

- **下一篇 u5-l3** 讲 `state3` 发送打包子状态机：ram1（波形）与 ram3（频谱）的数据如何加帧头 `F`/`T`、经 `serialt` 上传给 PC，与本讲的「采集填满 ram1」直接衔接。
- **复习 u4-l1/u4-l2**：`slope_adj`、`trig_value`、`timebase` 都是通过 UART 接收的命令设置的，结合 UART 收发机能补全「PC 命令 → 触发参数 → 采集行为」的完整闭环。
- **进阶方向**：尝试在仿真里给 `state`↔`state2` 的跨域信号加 2 级同步器，或把「电平 + 斜率」判别统一搬进 `clock_adc_out` 域，观察是否能消除潜在的触发抖动——这是一个很好的课程设计选题。
