# 六个测试用例的覆盖设计

## 1. 本讲目标

本讲承接 [u6-l1（仿真测试平台架构与公共过程）](u6-l1-testbench-architecture.md)，把视线从「测试平台怎么搭」转到「测试平台到底验证了什么」。`top_tb` 顺序调度 `case0`~`case5` 六个用例，每个用例都瞄准记录器的一组特定行为。读完本讲你应当能够：

- 逐个说出 `case0`~`case5` 各自验证的核心功能点，以及它们覆盖了 `data_rec.vhd` 中哪些代码段。
- 理解 `case0` 如何用「先拉高触发再 Arm」来证明外部触发是**边沿敏感**而非电平敏感。
- 理解 `case4` 如何用「前触发数 > 总样本数」「总样本数 = 0」这类**非法配置**验证记录器能干净恢复。
- 把「验证功能点 ↔ 关键源码行 ↔ 期望状态」三者对应起来，具备为新功能补写测试用例的能力。

本讲只读、不改源码，重点是「读懂测试在断言什么」。

## 2. 前置知识

阅读本讲前，请确认你已经理解以下概念（均在前序讲义建立）：

- **五状态机** `Idle→PreTrig→WaitTrig→PostTrig→Done` 及其迁移条件（见 [u3-l2](u3-l2-recorder-state-machine.md)）。状态码 `0..4` 与寄存器包常量 `Reg_Stat_StateIdle_c..Reg_Stat_StateDone_c` 一一对应。
- **三类触发源 + TrigEna 掩码**：外部（bit0）、软件（bit1）、自触发（bit2），合成单拍 `TrigNow_2` 并与 `In_Vld(1)` 相与（见 [u4-l1](u4-l1-trigger-sources-and-masking.md)）。
- **两级使能**：`TrigEna` 是「源级总开关」（选哪类触发源），`EnableExtTrig` 是「外部触发的逐路开关」（选哪几路 Trig_In 参与 OR），二者层级不同（见 [u4-l2](u4-l2-external-trigger.md)）。
- **公共激励/校验过程** `InputSamples` / `InputSamplesNoCh` / `CheckData` / `CheckDataNoCh`（见 [u6-l1](u6-l1-testbench-architecture.md)），以及期望值公式 `ExpVal_v = ch*chStep + spl*cntStep + startValue`。
- **断言工具** `axi_single_expect(addr, expected, ...)`：当实际读回值 ≠ 期望值时打印 `###ERROR###`，这是 CI 判定失败的唯一信号源（见 [u1-l3](u1-l3-run-simulation.md)）。

补充一个本讲反复用到的事实：测试平台用共享变量 `MemoryDepth_v` 做地址计算（在 `p_control` 里被设为 `30`），DUT 的 `MemoryDepth_g` 则由 `sim/config.tcl` 分别取 `32`（二次幂）和 `30`（非二次幂）各跑一次。两类深度共享同一套地址地图（因为 `MemAddr` 的通道间距 `2**log2ceil(30)=32` 在两种深度下相同），所以同一份用例能同时覆盖二次幂与非二次幂两条地址处理分支。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [testbench/top_tb/top_tb_case0_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case0_pkg.vhd) | **Basic Functionality**：基本录制流程、边沿触发验证、源级使能、Done 中断与自动确认、触发计数器清零。 |
| [testbench/top_tb/top_tb_case1_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case1_pkg.vhd) | **Corner Conditions**：零/最大前触发、单样本、最大样本数、断流（duty cycle）、最小录制间隔 MinRecPeriod。 |
| [testbench/top_tb/top_tb_case2_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case2_pkg.vhd) | **Self Triggered Mode**：自触发范围判定、OnEnter/OnExit、通道选择、unsigned 与 signed（含跨零点）双判。 |
| [testbench/top_tb/top_tb_case3_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case3_pkg.vhd) | **SW Trigger**：软件触发 sticky pending、采样间/采样中触发、未 Arm 时无动作、源级禁用、先置 SwTrig 再 Arm。 |
| [testbench/top_tb/top_tb_case4_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case4_pkg.vhd) | **Recovery from illegal configurations**：重复 Arm、前触发 > 总样本、总样本 = 0，验证非法配置后能干净恢复。 |
| [testbench/top_tb/top_tb_case5_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case5_pkg.vhd) | **Handling of multiple external triggers**：逐路使能屏蔽、多路外部触发 OR 合成。 |
| [testbench/top_tb/top_tb_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_pkg.vhd) | 公共过程与常量（`NumOfInputs_c=4`、`TriggerInputs_c=4`、`InputWidth_c=16`、`MemoryDepth_v=30`）。 |
| [testbench/top_tb/top_tb.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb.vhd) | 顶层 TB：DUT 实例化、双时钟、`p_control` 顺序调度六个用例。 |
| [hdl/data_rec.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd) | 核心记录器 RTL：每个 case 验证的行为最终都落在这里。 |
| [hdl/data_rec_register_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd) | 寄存器地址与字段位常量，测试用例通过它们寻址。 |

---

## 4. 核心概念与源码讲解

六个 case 的总览（先看这张表建立印象，后续逐节展开）：

| Case | 名称 | 主线功能点 | 关键源码（data_rec.vhd） |
| --- | --- | --- | --- |
| 0 | Basic Functionality | 基本流程 / 边沿触发 / 源级使能 / 自动确认 / TrigCnt 清零 | L211-L215（边沿检测）、L269-L274（Done→Idle） |
| 1 | Corner Conditions | 零与最大前触发 / 单样本 / 最大样本数 / duty cycle / MinRecPeriod | L253-L254（PreTrig=0 跳过）、L231-L241（最小间隔） |
| 2 | Self Triggered Mode | 范围判定 / OnEnter·OnExit / 通道选择 / unsigned·signed 双判 | L172-L188（范围）、L196-L205（边沿） |
| 3 | SW Trigger | sticky pending / 采样间·采样中触发 / 未 Arm 无动作 / 先置后 Arm | L218-L222（sticky） |
| 4 | Recovery from illegal configs | 重复 Arm / PreTrig>Total / Total=0 后恢复 | L264-L268、L290-L291（计数器使其立即 Done） |
| 5 | Multiple external triggers | 逐路屏蔽 / 多路 OR | L211-L215（OR 循环 + EnableExtTrig） |

### 4.1 case0：基本功能与边沿触发验证

#### 4.1.1 概念说明

`case0`（`CaseName_c = "Basic Functionality"`）是「冒烟测试 + 边沿敏感性证明」。它做两次完整录制（确保不是「只在复位后第一次碰巧能用」），并刻意设计一个陷阱：**在 Arm 之前就把外部触发信号拉高**。如果记录器是电平敏感的，这一拉高就会在 Arm 后立刻触发；只有边沿敏感的设计才会老老实实等到一个真实的 `0→1` 上升沿。这个用例同时验证了源级使能（`TrigEna`）、Done 中断 (`Done_Irq`)、读状态自动确认回 Idle、以及触发计数器清零。

#### 4.1.2 核心流程

```
复位后检查：State=Idle, TrigCnt=0, DoneTime=0, TrigEna=0, Done_Irq=0
配置 PreTrigSpls、TotalSpls
循环 2 次（i=0,1）：
  1. Trig_In(0) <= '1'        ← 关键：先拉高触发
  2. Arm                       ← 进入 PreTrig
  3. 喂前触发样本 → 进入 WaitTrig
  4. Trig_In(0) <= '0'         ← 拉低，为制造上升沿做准备
  5. TrigEna <= 0（禁用源）→ 发一个上升沿 → 仍 WaitTrig（被源级屏蔽）
  6. TrigEna <= Ext 位 → 发上升沿 → 进入 PostTrig
  7. 喂后触发样本 → TotalSpls 满 → Done
  8. 读状态（Done）→ 自动 Ack → 回 Idle
  9. CheckData 校验波形；TrigCnt == i+1
最后：写 Cfg.TrgCntClr → TrigCnt 清零
```

#### 4.1.3 源码精读

**陷阱的设置**——先拉高再 Arm，注释直白说明了意图：

[testbench/top_tb/top_tb_case0_pkg.vhd:78-79](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case0_pkg.vhd#L78-L79) — 把 `Trig_In(0)` 置 1，注释「check edge-sensitivity (and not level sensitivity)」。

随后拉低以便后续制造上升沿：

[testbench/top_tb/top_tb_case0_pkg.vhd:95-96](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case0_pkg.vhd#L95-L96) — `Trig_In(0) <= '0'`，注释「if trigger was level sensitive, it would already have caused the recorder to trigger」。

**源级屏蔽验证**——把 `TrigEna` 写 0 后发上升沿，期望仍停在 WaitTrig：

[testbench/top_tb/top_tb_case0_pkg.vhd:98-109](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case0_pkg.vhd#L98-L109) — 写 `Reg_TrigEna=0`、发脉冲、期望 `WaitTrig`；随后写回 `1*2**Reg_TrigEna_ExtIdx_c`（仅 Ext 位）。

这些行为之所以成立，根源在核心 RTL 的外部触发边沿检测：

[hdl/data_rec.vhd:211-215](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L211-L215) — 循环检测每一路 `r.Trig_In(0)(i)='1' and r.Trig_In(1)(i)='0'`（本拍高、上一拍低 = 上升沿）且 `EnableExtTrig(i)='1'`，才置 `ExtTrigPending_2`。注意这里**只有上升沿**这一种条件，所以预先拉高的电平不会触发。而 `TrigEna` 的屏蔽发生在更下游的合成式：

[hdl/data_rec.vhd:224-228](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L224-L228) — `TrigNow_2` 把三类 pending 各自与对应 `TrigEna` 位相与再或起来。`TrigEna=0` 时即使 `ExtTrigPending_2=1` 也被清零。

**自动确认回 Idle**——读状态寄存器（Done 态）即产生 `Ack` 脉冲：

[testbench/top_tb/top_tb_case0_pkg.vhd:124-133](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case0_pkg.vhd#L124-L133) — 读 `Reg_Stat` 得 `Done`，等 400 ns 后期望 `Idle`（无需显式写 Ack）。对应核心 RTL：

[hdl/data_rec.vhd:269-274](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L269-L274) — `Done_s` 下 `Ack='1'` 则回 `Idle_s`（`Arm='1'` 则直接重开 `PreTrig_s`）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「边沿敏感 vs 电平敏感」这一断言。

1. 打开 `testbench/top_tb/top_tb_case0_pkg.vhd`，定位 L78-L79 与 L95-L96。
2. 假想一个对照实验：若把 [hdl/data_rec.vhd:212](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L212) 的判定改成「`r.Trig_In(0)(i)='1'`（去掉 `and r.Trig_In(1)(i)='0'`）」，即变成电平敏感。
3. 在不改源码的前提下推理：case0 第 1 步「先拉高再 Arm」之后，记录器会在哪个状态被错误触发？
4. **预期结果**：电平敏感版会在 Arm 进入 PreTrig 后，于第一个 `In_Vld=1` 的样本上立刻满足条件，从而在尚未喂满前触发时就跳到 WaitTrig 甚至 PostTrig，导致 L84 的 `PreTrig` 状态断言失败、最终 `CheckData` 数据错位。（本结论基于源码静态推理，**待本地用修改后的源码运行仿真确认**。）

#### 4.1.5 小练习与答案

**练习 1**：case0 在 L99 把 `Reg_TrigEna` 写成 0 来「禁用触发」，case5 则用 `Reg_EnableExtTrig` 来禁用。两者屏蔽的是同一个东西吗？

**答案**：不是。`TrigEna` 是**源级总开关**（bit0 选「要不要外部触发这一整类源」），作用在 [hdl/data_rec.vhd:226](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L226) 的合成式里；`EnableExtTrig` 是**外部触发的逐路开关**（选「8 路里哪几路参与」），作用在 [hdl/data_rec.vhd:212](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L212) 的边沿检测里。前者关掉后连 pending 都合成不出；后者关掉后某一路连 pending 都不产生。

**练习 2**：case0 末尾 L153 写 `Cfg.TrgCntClr` 后，为什么不用再 Arm 就能确认 `TrigCnt` 归零？

**答案**：`TrigCntClr` 是独立于状态机的单拍清零位，只要写它就在下一拍把 `TrigCnt_3` 清 0（见 [hdl/data_rec.vhd:324-325](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L324-L325)），与当前处于哪个状态无关。

---

### 4.2 case1：边界条件与最小录制间隔

#### 4.2.1 概念说明

`case1`（`CaseName_c = "Corner Conditions"`）专打**边界**。它用两轮 duty cycle（`d=0→DutyCycle=1` 全速、`d=1→DutyCycle=11` 断流）把前触发数与总样本数在 `[0, MemoryDepth_v]` 范围内推到各种极端组合：零前触发、最大前触发、单样本（中间/首个）、恰好等于深度、断流时正好采满等。末尾还专门测**最小录制间隔 `MinRecPeriod`**——两次录制之间若靠得太近，第二次触发应被抑制。

#### 4.2.2 核心流程

```
for d in 0..1（duty cycle = 1 或 11）:
  [零前触发]   Pretrig=0, Totspl=3/4·depth → Arm → 喂样本 → Done → CheckData
  [最大前触发] Pretrig=Totspl-1            → 同上
  [单样本/中]  Totspl=1, Pretrig=0         → 同上
  [单样本/首]  Totspl=1, 起始值=100        → 同上
  [恰好采满]   Pretrig=10, 恰好送 Totspl 个 → 同上
  [最大样本/常]  Totspl=depth, Pretrig=depth/2
  [最大样本/大]  Totspl=depth, Pretrig=depth-1
  [最大样本/零]  Totspl=depth, Pretrig=0
  [MinRecPeriod]
    写 MinRecPeriod = 50us / 时钟周期（=8000 拍）
    第一次录制 → Done（冷却开始）
    立即再 Arm + 发触发 → 期望 WaitTrig（被抑制）
    等 50us（冷却结束）
    再发触发 → Done（恢复录制）
    写 MinRecPeriod = 0（关闭冷却）
```

#### 4.2.3 源码精读

**零前触发的特例**——`PreTrigSpls=0` 时不能走 `AdrCnt = PreTrigSpls-1` 的判断（会下溢），故显式跳过 PreTrig：

[testbench/top_tb/top_tb_case1_pkg.vhd:59-61](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case1_pkg.vhd#L59-L61) — 写 `Pretrig=0` 后 Arm。对应核心 RTL 的保护分支：

[hdl/data_rec.vhd:251-258](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L251-L258) — `if unsigned(PreTrigSpls)=0 then State:=WaitTrig_s`（直接跳过），否则才判 `AdrCnt_2 = PreTrigSpls-1`。

**最小录制间隔**——倒计数器实现「冷却」：

[testbench/top_tb/top_tb_case1_pkg.vhd:140-159](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case1_pkg.vhd#L140-L159) — 写 `MinRecPeriod = integer((50 us)/ClockPeriod_c)`；首次 Done 后立即再 Arm+触发，期望 `WaitTrig`（L154 注释「Period Not Respected」）；等 50 us 后再触发，期望 `Done`（L158）。对应核心 RTL：

[hdl/data_rec.vhd:231-241](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L231-L241) — 若 `LastRecCnt_2 /= 0`（冷却中）：强制 `TrigNow_2 := '0'`、每拍 `LastRecCnt_2 - 1`、并**直接清除** `ExtTrigPending_2`/`SwTrigPending_2`（来得太早的请求被丢弃而非延迟）；若 `r.Trigger_2='1'`（本拍兑现了一次触发）：重装 `LastRecCnt_2 := MinRecPeriod`。复位时 `LastRecCnt_2=0`，故首次触发必放行。

> 关键点：`MinRecPeriod` 的单位是**数据时钟 `Clk` 的周期数**（160 MHz → 6.25 ns），`LastRecCnt_2` 每个时钟节拍递减、不受 `In_Vld` 门控。`50 us / 6.25 ns = 8000`。

#### 4.2.4 代码实践

**实践目标**：理解 `MinRecPeriod` 抑制是「丢弃」而非「延迟」。

1. 阅读 [hdl/data_rec.vhd:231-241](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L231-L241)。
2. 回答：冷却期内来了一个外部触发脉冲，冷却结束后它会被补录吗？
3. **预期结果**：不会。冷却分支里 `v.ExtTrigPending_2 := '0'`（L237）在每拍都清 pending，因此「太早的请求」被直接丢弃。冷却结束后若没有新的上升沿，就不会触发——case1 L152-L158 之所以恢复录制，是因为等待 50 us 后**又发了一次新的触发样本**（L156 的 `InputSamples(..., 0, 2)` 带 `trigAt=2`），不是旧请求被补上。

#### 4.2.5 小练习与答案

**练习 1**：case1 的「最大样本数 + 最大前触发」（L120-L128）配置是 `Totspl=depth, Pretrig=depth-1`。此时 `CheckData` 的 `startValue` 是 `1`（L127），意味着只有 1 个后触发样本。请用 `SplCnt` 的预置规则解释为何这样也能正常 Done。

**答案**：进入 WaitTrig 时 `SplCnt_2` 被预置为 `PreTrigSpls+1 = depth`（见 [hdl/data_rec.vhd:290-291](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L290-L291)）。触发后只需再计到 `>= TotalSpls(=depth)`，即再采 1 个有效样本即满足 `SplCnt_2 >= TotalSpls`（见 [hdl/data_rec.vhd:264-268](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L264-L268)），故后触发段恰好 1 个样本。

**练习 2**：为什么 case1 要用 duty cycle = 11 这一轮断流再测一遍？

**答案**：`In_Vld` 断流时地址计数器 `AdrCnt_2` 与采样计数器 `SplCnt_2` 都应**保持不变**（见 [hdl/data_rec.vhd:281-294](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L281-L294)，它们都门控在 `r.In_Vld(1)='1'`）。duty cycle = 11 制造大量「空拍」，验证计数器不会在空拍上误进、录制窗口与数据仍然对齐。

---

### 4.3 case2：自触发（含 signed 跨零点）

#### 4.3.1 概念说明

`case2`（`CaseName_c = "Self Triggered Mode"`）验证最智能的触发源——**自触发**：不靠外部事件，而是判断采集数据是否落入 `[SelfTrigLo, SelfTrigHi]` 区间，并在「进入」(OnEnter) 或「离开」(OnExit) 时触发。它通过一组子用例覆盖：通道 0/通道 2 选择、OnEnter/OnExit、未触碰边界、unsigned 区间、**signed 跨零点区间**（如 `-10..10`）、signed 全负区间（如 `-20..-10`），以及禁用源时不触发。

#### 4.3.2 核心流程（以「signed 跨零点，OnEnter」子用例为例）

```
公共配置：Totspl=3, Pretrig=1, TrigEna=Self(bit2)
[signed 跨零点 OnEnter]
  SelfTrigLo=-10, SelfTrigHi=10
  SelfTrigCfg = ch0 使能(bit0) + OnEnter(bit16)
  Arm
  InputSamplesNoCh(..., startCnt=-20, chStep=1, cntStep=1)   ← 样本从 -20 递增
  → 期望 Done
  CheckDataNoCh(3, startValue=-11, chStep=1, cntStep=1)       ← 校验窗口 [-11,-10,-9]
```

样本序列（通道 0）为 `-20, -19, …, -10, …, 0, …, 10, 11, …`。第一个落入 `[-10,10]` 的样本是 `-10`，OnEnter 在「上一拍不在区间 → 本拍在区间」时触发。

#### 4.3.3 源码精读

**范围判定（先 unsigned 再 signed 双判）**：

[hdl/data_rec.vhd:172-188](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L172-L188) — 先用 `unsigned` 比较 `Lo ≤ Data ≤ Hi`；不满足再用 `signed` 比较一次。负数（如 `-20`）的 unsigned 值是巨大的正数（16 位下 `-20 = 0xFFEC = 65516`），必然 ≥ `Hi=10`，故 unsigned 分支失败、落到 signed 分支判定——这正是跨零点区间必须靠 signed 分支捕获的原因。

**进入/离开边沿检测**：

[hdl/data_rec.vhd:196-205](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L196-L205) — `StEnter_2 := r.StInRange_1 and not r.StInRangeLast_1`（本拍在区间、上一拍不在）；`StExit_2 := r.StInRangeLast_1 and not r.StInRange_1`（反之）。二者各自与 `SelfTrigChEna` 相与后归约，再由 `SelfTrigOnEnter`/`SelfTrigOnExit` 选通，得到当拍瞬时变量 `StTrig_2`。

测试侧的配置写入与校验：

[testbench/top_tb/top_tb_case2_pkg.vhd:148-167](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case2_pkg.vhd#L148-L167) — OnExit 子用例 `CheckDataNoCh(3, 10, 1, 1)`（窗口 `[10,11,12]`），OnEnter 子用例 `CheckDataNoCh(3, -11, 1, 1)`（窗口 `[-11,-10,-9]`）。

#### 4.3.4 代码实践

**实践目标**：手算「signed 跨零点 OnEnter」子用例的首个触发样本值。

1. 配置：`Lo=-10, Hi=10, OnEnter, ch0 使能`，样本从 `-20` 起每次 `+1`。
2. 逐样本判断是否在区间内（注意用 **signed** 比较，因为区间跨零点）：
   - `-20, -19, …, -11`：均 `< -10`，**不在**区间。
   - `-10`：`-10 ≥ -10` 且 `-10 ≤ 10`，**首次落入**区间。
3. OnEnter 在 `-11（不在）→ -10（在）` 的跳变沿触发，故**首个触发样本值 = -10**。
4. 用 `CheckDataNoCh(3, startValue=-11, 1, 1)` 验证：期望值 `ExpVal_v = ch·1 + spl·1 + (-11)`，得 `spl=0→-11`（前触发）、`spl=1→-10`（触发样本）、`spl=2→-9`（后触发）。触发样本位于窗口正中（`PreTrig=1` 决定 1 个前触发样本），与 `-10` 吻合。
5. **预期结果**：首个触发样本值 = **-10**（十进制），对应 16 位 signed 二进制 `0xFFF6`。

> 对照 OnExit 子用例：首个离开区间的样本是 `11`（`10` 仍在区间、`11` 出区间），故 OnExit 的首个触发样本值 = **11**，窗口 `[10,11,12]` 与 `CheckDataNoCh(3, 10, 1, 1)` 一致。

#### 4.3.5 小练习与答案

**练习 1**：case2 L191-L206 有一个「禁用源」子用例：先 `TrigEna=0` 发数据（期望 WaitTrig），再 `TrigEna=Self` 发数据（期望 Done）。这验证了什么？

**答案**：验证自触发受 `TrigEna` bit2 的源级屏蔽（见 [hdl/data_rec.vhd:225](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L225) 的 `StTrig_2 and TrigEna(SelfIdx)`）。禁用时即便数据落入区间也不会触发；重新使能后下一次范围事件才触发。注意自触发是**非锁存的瞬时变量**，禁用期间的事件不会被「攒」下来。

**练习 2**：case2 L111-L134 的「通道选择性」子用例：`SelfTrigChEna` 同时使能 ch0、ch1，但数据让 ch2 落入区间、ch0/ch1 不落入，期望不触发。哪行代码保证了「没使能的通道即使落入区间也不触发」？

**答案**：[hdl/data_rec.vhd:200 与 L203](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L200-L205) 中 `unsigned(StExit_2 and SelfTrigChEna) /= 0` 与 `unsigned(StEnter_2 and SelfTrigChEna) /= 0`——边沿向量先与通道使能掩码按位相与再归约，未使能通道的位被清零，故不参与触发。

---

### 4.4 case3：软件触发与 sticky pending

#### 4.4.1 概念说明

`case3`（`CaseName_c = "SW Trigger"`）验证软件触发。软件触发由 AXI 写 `Reg_SwTrig` 的 bit0 产生，行为是 **sticky（粘滞）pending**：写 1 后 `SwTrigPending_2` 一直保持，直到进入 PreTrig 状态才被清除。因此「先写 SwTrig=1、再 Arm」也能触发——这正是 free-running（自循环）模式的基础。本用例覆盖：采样间触发、采样中触发（`In_Vld` 持续为 1 时写 SwTrig）、未 Arm 时写 SwTrig 无动作、源级禁用、以及 sticky 的「先置后 Arm」。

#### 4.4.2 核心流程

```
配置 Pretrig=5, Totspl=10, TrigEna=Sw(bit1)
[采样间触发] Arm → 喂样本 → 写 SwTrig=1 → 写 SwTrig=0 → 喂样本 → Done → CheckData(10,95)
[第二次执行]  Arm → ... → CheckData(10,195)
[采样中触发] Arm，In_Vld 持续拉高期间写 SwTrig=1 → Done
[未 Arm 无动作] Idle 下写 SwTrig → 仍 Idle（连续 20 拍确认）
[最大样本/零前触发] Pretrig=0, Totspl=depth → CheckData(depth,100)
[源级禁用]   TrigEna=0 → 写 SwTrig → 仍 WaitTrig；TrigEna=Sw → 再写 → Done
[先置后 Arm] 先写 SwTrig=1 → 再 Arm → 喂 15 样本 → 立即 Done → CheckData(10,5)
```

#### 4.4.3 源码精读

**sticky pending 的核心两行**：

[hdl/data_rec.vhd:218-222](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L218-L222) — `if SwTrig='1' then SwTrigPending_2:='1'; elsif State_2=PreTrig_s then SwTrigPending_2:='0'; end if;`。注意是 `if/elsif`：**置位优先于 PreTrig 清除**。所以当 `SwTrig` 恒为 1 时，即便状态机走到 PreTrig 想清除它，同一拍又被 `SwTrig='1'` 重新置位——每次重 Arm 都能立即触发，free-running 得以成立。

**「先置后 Arm」子用例**：

[testbench/top_tb/top_tb_case3_pkg.vhd:150-159](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case3_pkg.vhd#L150-L159) — L153 先写 `SwTrig=1`，L154 才写 `Arm`。因为 sticky，pending 已挂着，Arm 进入 PreTrig 后喂满前触发即触发，`CheckData(10, startValue=5)` 反映「触发点很早」。L168 在用例结尾把 `SwTrig` 写回 0（free-running 用完要清，否则后续用例会被「免费」触发）。

**未 Arm 时无动作**：

[testbench/top_tb/top_tb_case3_pkg.vhd:102-117](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case3_pkg.vhd#L102-L117) — Idle 下连续写 SwTrig 并发样本，期望状态始终 `Idle`。因为 `SwTrigPending_2` 只在 `WaitTrig` 状态经 `TrigNow_2` 兑现（[hdl/data_rec.vhd:259-263](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L259-L263)），Idle 态下状态机只看 `Arm`，pending 挂着也无出口。

#### 4.4.4 代码实践

**实践目标**：理解 sticky pending 对 free-running 模式的意义。

1. 阅读 [hdl/data_rec.vhd:218-222](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L218-L222) 与 case3 L150-L159。
2. 推理：若把 L218-L222 的 `if/elsif` 改成「先判 PreTrig 清除、再判 SwTrig 置位」（即交换顺序），「先置后 Arm」子用例还能通过吗？
3. **预期结果**：不能。若清除优先，进入 PreTrig 那拍会把已挂着的 pending 清掉（因为 `SwTrig` 在 AXI 域、经 status_cc 跨域后可能恰在该拍仍为 1，但取决于跨域时序）；更关键的是 free-running 模式下「Done 后再 Arm」时，状态会先经 PreTrig 把 pending 清除，导致无法连续触发。当前 `if/elsif` 顺序确保「只要 SwTrig 还写着 1，pending 就一直在」，这是 free-running 成立的必要条件。（**待本地用交换顺序后的源码运行确认具体失败点。**）

#### 4.4.5 小练习与答案

**练习 1**：case3 L67-L69 写 `SwTrig=1` 后很快又写 `SwTrig=0`，但触发仍然发生。为什么写回 0 没有取消触发？

**答案**：因为 sticky。`SwTrig=1` 一旦写入，`SwTrigPending_2` 立即被置位并保持；之后写 `SwTrig=0` 只是让 `SwTrig` 电平变低，但 pending 不会被「写 0」清除——它只在进入 PreTrig 时清除（[hdl/data_rec.vhd:220-221](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L220-L221)）。此时记录器已在 WaitTrig，pending 兑现为 `TrigNow_2` 完成触发。

**练习 2**：case3 L131-L148 的「源级禁用」子用例里，禁用期间写的 SwTrig，在重新使能后会触发吗？

**答案**：会。`SwTrigPending_2` 是 sticky 的，禁用源（`TrigEna` bit1=0）只是让它在合成式 ([hdl/data_rec.vhd:227](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L227)) 里被屏蔽，pending 本身仍挂着；重新使能源后，下一拍 `TrigNow_2` 即可兑现。注意这和外部触发的 pending 不同——外部 pending 在 MinRecPeriod 冷却期会被丢弃，软件 pending 在冷却期同样被清（[hdl/data_rec.vhd:238](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L238)），但在单纯的源级禁用下不会被清。

---

### 4.5 case4：异常配置的恢复

#### 4.5.1 概念说明

`case4`（`CaseName_c = "Recovery from illegal configurations"`）验证记录器在**不合理配置**下不会卡死、并在事后能恢复正常工作。覆盖三类异常：①重复 Arm（已 Arm 时再写 Arm）；②前触发数 > 总样本数（`Pretrig=15, Totspl=10`）；③总样本数 = 0（`Totspl=0, Pretrig=15`）。对后两类，测试**不校验数据**（注释明说「data is not important but clean recovery is」），只要求状态能走到 Done，随后再用正常配置录一次、用 `CheckData` 证明记录器完好如初。

#### 4.5.2 核心流程

```
公共：TrigEna=Ext
[重复 Arm]   Arm → 再 Arm → 喂样本 → Done → CheckData(10, depth-5)
[PreTrig>Total] Pretrig=15, Totspl=10 → Arm → PreTrig → 喂样本 → Done（不校验数据）
              → 正常配置 Pretrig=5,Totspl=10 → Arm → Done → CheckData(10, 100+depth-5)
[Total=0]    Totspl=0, Pretrig=15 → Arm → PreTrig → 喂样本 → Done（不校验数据）
              → 正常配置 → Done → CheckData(...)
```

#### 4.5.3 源码精读

**重复 Arm**——状态机对重复 Arm 是幂等的：

[testbench/top_tb/top_tb_case4_pkg.vhd:60-68](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case4_pkg.vhd#L60-L68) — 连写两次 Arm。对应 [hdl/data_rec.vhd:247-250](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L247-L250)：`Idle_s` 下 `Arm='1'` 进 `PreTrig_s`。第二次 Arm 到达时状态已是 PreTrig（或更后），`case` 里只有 Idle/Done 两个分支响应 Arm，故忽略，不影响录制。

**前触发 > 总样本的恢复**——靠采样计数器「预置值已超 TotalSpls」自然终结：

[testbench/top_tb/top_tb_case4_pkg.vhd:70-79](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case4_pkg.vhd#L70-L79) — `Pretrig=15, Totspl=10`。机制：进入 WaitTrig 时 `SplCnt_2` 被预置为 `PreTrigSpls+1 = 16`（[hdl/data_rec.vhd:290-291](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L290-L291)），已经 `> TotalSpls(10)`；触发进 PostTrig 后，[hdl/data_rec.vhd:264-268](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L264-L268) 立即判定 `SplCnt_2 >= TotalSpls` → Done。记录器不会卡在 PostTrig。

**总样本 = 0 的恢复**——同理：

[testbench/top_tb/top_tb_case4_pkg.vhd:96-105](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case4_pkg.vhd#L96-L105) — `Totspl=0`。`SplCnt_2` 预置为 16，`16 >= 0` 恒成立，触发后立即 Done。

#### 4.5.4 代码实践

**实践目标**：理解「非法配置为何不会卡死」。

1. 阅读 [hdl/data_rec.vhd:264-268](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L264-L268) 与 [hdl/data_rec.vhd:290-291](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L290-L291)。
2. 回答：`Pretrig=15, Totspl=0` 时，触发后 `SplCnt_2` 的值是多少？记录器停在哪个状态？
3. **预期结果**：触发进 PostTrig 时 `SplCnt_2 = PreTrigSpls+1 = 16`；`16 >= TotalSpls(0)` 立即满足，状态机在该拍直接进 Done（`Done(2):='1'`）。所以记录器停在 `Done_s`，不会卡在 PostTrig，等待软件读状态后自动 Ack 回 Idle。

#### 4.5.5 小练习与答案

**练习 1**：case4 在每个非法配置之后，都要紧跟一次正常录制并用 `CheckData` 校验（如 L82-L94）。为什么不直接在非法配置那次就校验数据？

**答案**：非法配置下录制窗口的语义本身就不合理（后触发段为 0 或负），数据没有「正确值」可言；测试关心的是**控制通路的健壮性**——状态机能走到 Done、内部计数器不溢出/卡死。校验数据放在随后的正常录制里，是为了证明「异常没有留下任何后遗症」（寄存器、计数器、状态都已复位到可正常工作的初值）。

**练习 2**：`Totspl=0` 时，为什么记录器没有在 Arm 后立刻 Done，而是要等到一个触发？

**答案**：`Arm` 只把状态从 Idle 推到 PreTrig/WaitTrig（[hdl/data_rec.vhd:247-258](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L247-L258)）；`TotalSpls` 的比较 `SplCnt_2 >= TotalSpls` 只发生在 `PostTrig_s` 分支（[hdl/data_rec.vhd:264-268](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L264-L268)）。所以即便 `TotalSpls=0`，也必须先经 WaitTrig 等到一次触发进入 PostTrig，才会评估该条件并 Done。

---

### 4.6 case5：多外部触发的 OR 与逐路使能

#### 4.6.1 概念说明

`case5`（`CaseName_c = "Handling of multiple external triggers"`）验证外部触发的「多路」语义。记录器最多支持 `TrigInputs_g` 路外部触发（本 TB 里 `TriggerInputs_c=4`），所有使能路做 OR 合成单一 pending。本用例分两部分：①**逐路屏蔽**——只使能第 `ti_ena` 路，然后依次在第 `ti_cur` 路发触发，仅当 `ti_cur = ti_ena` 时才应录制；②**多路 OR**——使能全部 4 路，依次在每一路发触发，每一路都应录制。

#### 4.6.2 核心流程

```
公共：TrigEna=Ext, Totspl=10, Pretrig=5
[逐路屏蔽]
  for ti_ena in 0..3:                       ← 只使能这一路
    写 EnableExtTrig = 2**ti_ena
    for ti_cur in 0..3:                     ← 依次在每一路发触发
      Arm；InputSamples(..., trigIdx=ti_cur)
      if ti_cur == ti_ena: 期望 Done + CheckData
      else:                 期望 WaitTrig（被该路屏蔽）
[多路 OR]
  写 EnableExtTrig = 0xFFFFFFFF（全部使能）
  for ti_cur in 0..3:
    Arm；InputSamples(..., trigIdx=ti_cur) → 期望 Done + CheckData
```

#### 4.6.3 源码精读

**逐路使能 + OR 合成**：

[testbench/top_tb/top_tb_case5_pkg.vhd:62-91](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case5_pkg.vhd#L62-L91) — 双重循环，`EnableExtTrig = 1*2**ti_ena`（仅一位）。对应核心 RTL：

[hdl/data_rec.vhd:211-215](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L211-L215) — `for i in 0 to TrigInputs_g-1 loop if Trig_In(0)(i)='1' and Trig_In(1)(i)='0' and EnableExtTrig(i)='1' then ExtTrigPending_2:='1'; end if; end loop;`。这是一个 OR 循环：任何一路满足「上升沿 + 该路使能」即置 pending。未使能的路（`EnableExtTrig(i)='0'`）即使有上升沿也不贡献，故 `ti_cur ≠ ti_ena` 时 pending 不产生、状态停在 WaitTrig。

**全使能多路 OR**：

[testbench/top_tb/top_tb_case5_pkg.vhd:93-111](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case5_pkg.vhd#L93-L111) — `EnableExtTrig = 16#FFFFFFFF#`，每一路单独发触发都能触发录制。

> 注意 `InputSamples` 的 `trigIdx` 参数把触发脉冲打到指定路（见 [testbench/top_tb/top_tb_pkg.vhd:118-119](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_pkg.vhd#L118-L119)：`inp.Trig_In(trigIdx) <= '1'`），其余路保持 0。

#### 4.6.4 代码实践

**实践目标**：用 case5 的结构，设计「两路同时触发」的扩展测试。

1. 阅读 [testbench/top_tb/top_tb_case5_pkg.vhd:62-91](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case5_pkg.vhd#L62-L91) 与 [hdl/data_rec.vhd:211-215](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L211-L215)。
2. 思考：若 `EnableExtTrig = 0b0011`（使能路 0 和路 1），并在同一拍给路 0 和路 1 都发上升沿，记录器会触发几次？
3. **预期结果**：触发 **1 次**。OR 循环把多路上升沿合并成单一标量 `ExtTrigPending_2`（一个 sticky 位，不是计数器），同拍两路都满足也只是把它置 1 一次；进入 WaitTrig 后一次 `TrigNow_2` 兑现，进入 PostTrig，pending 在下一个 PreTrig 才清除。所以多路同拍触发等价于一次触发。（**待本地仿真确认。**）

#### 4.6.5 小练习与答案

**练习 1**：case5 用 `EnableExtTrig`（逐路）做屏蔽，case0 用 `TrigEna`（源级）做屏蔽。若把 case5 的 `EnableExtTrig` 全置 0、但 `TrigEna` 仍保留 Ext 位，会怎样？

**答案**：不触发。`EnableExtTrig(i)='0'` 使 [hdl/data_rec.vhd:212](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L212) 的条件全部不满足，`ExtTrigPending_2` 始终为 0；下游 `TrigEna` 即便使能 Ext 位也无 pending 可兑现。两级使能是「与」关系：源级放行 + 逐路放行，缺一不可。

**练习 2**：case5 的 `CheckData(10, dataStart+MemoryDepth_v-5, ...)`，其中 `dataStart = 100*ti_ena+ti_cur`。为什么 `startValue` 要随 `ti_ena`、`ti_cur` 变化？

**答案**：为了在每个子迭代里用**不同的数据起点**，避免上一轮残留样本与本轮期望值混淆。`InputSamples` 用全局共享变量 `PatternCnt_v` 当基底（见 [testbench/top_tb/top_tb_pkg.vhd:104-111](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_pkg.vhd#L104-L111)），`dataStart` 经 `startCnt` 重置 `PatternCnt_v`，使每次录制的窗口首样本可控，`CheckData` 才能用闭式公式 `ExpVal_v = ch·2**(W-3) + spl + startValue` 精确比对。

---

## 5. 综合实践

把六个用例串成一张「**验证功能点 — 关键代码行 — 期望状态**」对照表，并完成一次手算。这是本讲的核心交付物。

### 5.1 对照表（请补全「期望状态」列后与下文核对）

| Case | 验证功能点 | 关键代码行（data_rec.vhd，除非另注） | 期望状态 |
| --- | --- | --- | --- |
| 0 | 边沿触发（非电平） | [L211-L215](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L211-L215) | 先拉高再 Arm 不触发；上升沿才触发 |
| 0 | 源级使能 TrigEna | [L224-L228](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L224-L228) | `TrigEna=0` 时发沿仍 WaitTrig |
| 0 | 读状态自动 Ack 回 Idle | [L269-L274](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L269-L274) | Done 态读 Stat → 400 ns 后 Idle |
| 1 | 零前触发特例 | [L251-L258](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L251-L258) | Pretrig=0 时跳过 PreTrig 直入 WaitTrig |
| 1 | 最小录制间隔 | [L231-L241](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L231-L241) | 冷却期内再触发 → WaitTrig；过期后 → Done |
| 2 | signed 跨零点范围判定 | [L172-L188](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L172-L188) | 负数经 signed 分支判定入区间 |
| 2 | OnEnter/OnExit 边沿 | [L196-L205](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L196-L205) | 进入/离开区间各触发一次 |
| 3 | 软件 sticky pending | [L218-L222](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L218-L222) | 先置 SwTrig 再 Arm 也能触发 |
| 3 | 未 Arm 时无动作 | [L259-L263](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L259-L263) | Idle 下写 SwTrig 始终 Idle |
| 4 | PreTrig>Total 恢复 | [L264-L268](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L264-L268) + [L290-L291](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L290-L291) | 触发后立即 Done，事后正常录制无误 |
| 4 | Total=0 恢复 | 同上 | 同上 |
| 5 | 逐路屏蔽 EnableExtTrig | [L211-L215](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L211-L215) | 仅使能路触发；其余路 WaitTrig |
| 5 | 多路 OR | 同上 | 全使能时每路都能触发 |

### 5.2 手算：case2「signed 跨零点，OnEnter」首个触发样本值

- 配置：`Lo=-10, Hi=10, OnEnter, ch0`；样本自 `-20` 起，`cntStep=1`（即 `-20, -19, …`）。
- 区间判定用 signed 比较（因区间跨零点，unsigned 分支对负数必失败）。
- 首个满足 `Lo ≤ x ≤ Hi` 的样本是 `x = -10`（`-10 ≥ -10` 成立）。
- OnEnter 在 `-11 → -10` 的「不入→入」跳变沿触发。
- **首个触发样本值 = -10**（signed 16 位 = `0xFFF6`）。
- 校验：`CheckDataNoCh(3, -11, 1, 1)` 期望窗口 `[-11, -10, -9]`，触发样本 `-10` 位于 `spl=1`（`PreTrig=1` 决定其前有一个前触发样本 `-11`），完全吻合。

> 建议你把 case2 的其余 7 个自触发子用例也按此方式手算一遍（注意「首个触发样本值」指窗口中 `spl=1` 那个样本，即 `CheckDataNoCh` 的 `startValue+1`）：OnExit 跨零点 `[-10,10]` → 首触发值 `11`（窗口 `[10,11,12]`）；signed 全负 `[-20,-10]` OnExit → `-9`（窗口 `[-10,-9,-8]`）、OnEnter → `-20`（窗口 `[-21,-20,-19]`）；unsigned 区间 `[5,10]` 等。与各自的 `CheckDataNoCh` 期望值对照，是检验你是否真正理解自触发边界检测的最快方式。

## 6. 本讲小结

- 六个用例**按风险递进**组织：case0 冒烟+边沿、case1 边界+断流+最小间隔、case2 自触发（含 signed 跨零点）、case3 软件 sticky、case4 异常恢复、case5 多路 OR，共同覆盖核心 RTL 的绝大多数分支。
- **case0 的精髓**是「先拉高触发再 Arm」——这唯一地证明外部触发是边沿敏感（[hdl/data_rec.vhd:212](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L212) 要求本拍高、上一拍低），电平敏感设计会在这一步穿帮。
- **两级使能**在测试中分工明确：`TrigEna`（源级）由 case0/case3 验证，`EnableExtTrig`（逐路）由 case5 验证，case2 验证自触发的源级屏蔽与通道掩码。
- **case4 不校验非法配置下的数据**，只验证「状态机能走到 Done 且事后恢复正常」——`SplCnt_2` 进入 WaitTrig 时被预置为 `PreTrigSpls+1`（[hdl/data_rec.vhd:290-291](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L290-L291)），使 `PreTrig>Total` 与 `Total=0` 都触发后立即 Done，不卡死。
- **期望值公式** `ExpVal_v = ch·chStep + spl·cntStep + startValue` 是所有 `CheckData*` 的核心；`startValue` 编码了窗口首样本，配合 `InputSamples` 对 `PatternCnt_v` 的重置，实现「每次录制窗口可控、可精确比对」。
- 所有断言失败都经 `axi_single_expect` 打印 `###ERROR###`，被 `ciFlow.py` 捕获作为 CI 失败信号——这是「测试逻辑对错」与「仿真流程成败」之间的唯一桥梁。

## 7. 下一步学习建议

- **横向读完验证体系后，建议进入 [u6-l3（EPICS 控制系统集成）](u6-l3-epics-integration.md)**：看看这些寄存器（`TrigEna`、`SwTrig`、`SelfTrigCfg`、`EnableExtTrig`、`MinRecPeriod`）如何被 EPICS db 模板暴露成操作员可点的记录，以及 EPICS 状态机如何监听 Done 并触发数据回读与自动 re-arm。
- **若关注二次开发**，可直接读 [u6-l4（扩展通道、触发源与寄存器）](u6-l4-extension-and-customization.md)：本讲的六个 case 提供了「为新功能补测试」的模板——例如新增一类触发源时，应仿照 case0/case5 补「源级使能 + 边沿检测 + 屏蔽」三组断言。
- **源码再挖掘**：可对照 [hdl/data_rec.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd) 的 `p_comb` 逐段核对，本讲引用的每一行都能在状态机/计数器/触发合成三处找到对应；建议用一张大图把「六个 case → 触发的代码行」画出来，作为个人验证地图长期维护。
