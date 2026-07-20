# 最小录制间隔、计数器与 Trig_Out 转发

## 1. 本讲目标

本讲是触发机制单元（u4）的收尾篇。前三篇分别讲了外部触发、软件触发、自触发，关注的是「**一次**触发如何产生」。本讲关注三个**围绕触发、但属于"周边机制"**的问题：

1. **两次触发靠得太近怎么办？** —— `MinRecPeriod`（最小录制间隔）如何用倒计数器 `LastRecCnt_2` 抑制背靠背（back-to-back）触发。
2. **触发了多少次？录完后主机多久才来读？** —— `TrigCnt`（触发计数器）与 `DoneTime`（Done 状态持续时长计数器）两个诊断计数器。
3. **IP 内部"真正兑现"的触发如何引出去给别人用？** —— v2.4 新增的 `Trig_Out` 转发端口，以及它和 `Done_Irq` 中断在**时钟域**与**事件含义**上的区别。

学完后你应该能够：

- 说出 `LastRecCnt_2` 三个分支各自的触发条件，以及"抑制期丢弃 pending 而非延迟 pending"这一设计取舍。
- 推演 `top_tb_case1` 中"第二次 Arm 立即触发不录制、等 50 µs 后才录制"的完整时序。
- 区分 `TrigCnt_3` 与 `DoneTime_3` 的增/清条件与饱和保护。
- 说清 `Trig_Out`（数据时钟域，转发 Trigger_2，"录制开始"）与 `Done_Irq`（AXI 时钟域，转发 Done，"录制完成"）的差异。

## 2. 前置知识

本讲默认你已经掌握：

- **两进程法与 Stage 流水线**（u3-l3）：核心 RTL 用组合进程 `p_comb` 计算 `r_next`、时序进程 `p_seq` 搬入；带数字后缀的信号（如 `Trigger_2`、`LastRecCnt_2`、`TrigCnt_3`）表示其所处流水级。本讲的计数器全部是 `data_rec_r` record 的字段，命名后缀即流水级编号。
- **触发源总览与 TrigNow_2 合成**（u4-l1）：三类触发源经 `TrigEna` 三位掩码合成出单拍信号 `TrigNow_2`，再经状态机在 `WaitTrig_s` 兑现成 `Trigger_2` 脉冲。本讲的 `MinRecPeriod` 作用点就在 `TrigNow_2` 合成**之后、状态机消费之前**。
- **状态机五状态**（u3-l2）：`Idle→PreTrig→WaitTrig→PostTrig→Done`，`Trigger_2` 在 `WaitTrig_s` 命中时产生，`Done(2)` 在 `PostTrig_s` 采满时产生。
- **双时钟域**（u2-l1）：数据域 `Clk` 与 AXI 域 `s00_axi_aclk` 相互独立；测试平台里数据时钟 160 MHz（周期 6.25 ns），AXI 时钟 125 MHz（周期 8 ns）。

一个关键术语回顾：**pending（挂起）** 指触发请求被锁存下来"排队等候兑现"。外部触发是边沿锁存 pending、软件触发是电平 sticky pending（见 u4-l2、u4-l3）。本讲会看到：在 `MinRecPeriod` 抑制期内，这些 pending 会被**直接清除（丢弃）**，而不是延后兑现——这是理解 case1 行为的钥匙。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [hdl/data_rec.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd) | 核心记录器。本讲的 `LastRecCnt_2`、`TrigCnt_3`、`DoneTime_3`、`Trigger_2→Trig_Out` 全部在此 |
| [hdl/data_rec_vivado_wrp.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd) | Vivado 封装层。`Done_Irq` 经 `pulse_cc` 跨时钟域、`Trig_Out` 直通；`TrigForwarding_g` 仅在此影响 IP 端口暴露 |
| [hdl/data_rec_register_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd) | 寄存器地址常量。`MinRecPeriod`/`TrigCnt`/`DoneTime` 的地址与 `TrgCntClr` 字段位在此定义 |
| [testbench/top_tb/top_tb_case1_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case1_pkg.vhd) | "Corner Conditions"用例，含本讲实践任务的 `MinRecPeriod` 子测试 |
| [Changelog.md](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/Changelog.md) | 记录 v2.4 新增 `Trig_Out` 端口 |

> 提示：本仓库所有讲义中，链接里的 `f68c931...` 是当前 HEAD 的 commit，行号据此版本固定。

## 4. 核心概念与源码讲解

### 4.1 最小录制间隔：MinRecPeriod 与 LastRecCnt_2

#### 4.1.1 概念说明

记录器每"兑现"一次触发，就完成一段录制（前触发 + 后触发），随后进入 `Done_s`，等主机经 AXI 读走数据并确认（Ack）后才能回到 `Idle_s` 重新 Arm。如果触发源非常密集——例如把 `Trig_In` 直接接到一个方波上——记录器可能被连续不断的触发"淹没"，主机来不及读出旧数据就被新录制覆盖。

`MinRecPeriod`（最小录制间隔）寄存器就是为此设计的**软件可配冷却闸门**：它定义"两次录制之间至少要间隔多少个数据时钟周期"。每次真正兑现一次触发后，启动一个长度为 `MinRecPeriod` 的倒计时；倒计时归零前，任何新的触发请求都被**抑制并丢弃**。设为 0 即关闭该机制（无最小间隔）。

注意它是"**冷却**"而非"**延迟**"：抑制期内到来的触发请求不会被排队等冷却结束后补发，而是当场清除 pending（见 4.1.3 源码注释 "they were too early"）。这保证冷却一结束，记录器看到的是"当下"的触发源状态，而不是冷却期里积压的陈旧事件。

#### 4.1.2 核心流程

冷却靠倒计数器 `LastRecCnt_2`（Stage 2 对齐，32 位）实现。在 `p_comb` 每个数据时钟周期，按下式更新（`n` 为当前周期，`n+1` 为下一周期）：

\[
\text{LastRecCnt}_{n+1} =
\begin{cases}
\text{MinRecPeriod} & \text{若 } \text{MinRecPeriod} < \text{LastRecCnt}_{n} \quad \text{(软件把间隔调小，向下钳位)} \\
\text{LastRecCnt}_{n} - 1 & \text{若 } \text{LastRecCnt}_{n} \neq 0 \quad \text{(冷却中，逐拍递减)} \\
\text{MinRecPeriod} & \text{若 } \text{Trigger}_{n} = 1 \quad \text{(刚兑现一次触发，重装冷却)} \\
0 & \text{否则}
\end{cases}
\]

与之配套的触发抑制规则：

\[
\text{若 } \text{LastRecCnt}_{n} \neq 0 \;\Rightarrow\; \text{TrigNow}_2 := 0,\;\; \text{清除 ExtTrigPending}_2,\;\; \text{清除 SwTrigPending}_2
\]

伪代码时序（`MinRecPeriod = P`，触发在第 0 拍兑现）：

```
周期 0 : 触发源命中 → Trigger_2 := 1（在状态机里置位，登记入 record）
周期 1 : r.Trigger_2 = 1 → LastRecCnt_2 := P        （重装冷却）
周期 2 : LastRecCnt_2 = P ≠ 0 → 抑制, 减为 P-1, 清 pending
周期 3 : LastRecCnt_2 = P-1 ≠ 0 → 抑制, 减为 P-2, 清 pending
   ...
周期 P+1: LastRecCnt_2 = 1 ≠ 0 → 抑制, 减为 0,   清 pending
周期 P+2: LastRecCnt_2 = 0 → 不抑制, 触发源重新生效
```

所以冷却恰好持续 `MinRecPeriod` 个数据时钟周期。关键点：**递减不受 `In_Vld` 门控，每拍都减**——所以 `MinRecPeriod` 的单位是"数据时钟周期数"，与有效采样率无关。这正是 case1 里用 `(50 us)/ClockPeriod_c` 把时间换算成周期数的原因。

#### 4.1.3 源码精读

冷却逻辑位于 `p_comb` 中，**在 `TrigNow_2` 合成之后、状态机 `case r.State_2` 之前**（[hdl/data_rec.vhd:L230-L241](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L230-L241)）：

```vhdl
-- Maximum trigger period counter
if unsigned(MinRecPeriod) < unsigned(r.LastRecCnt_2) then
    v.LastRecCnt_2 := MinRecPeriod;                       -- 软件把间隔调小→钳到新值
elsif unsigned(r.LastRecCnt_2) /= 0 then
    TrigNow_2 := '0';                                      -- 冷却中：撤销本拍触发
    v.LastRecCnt_2 := std_logic_vector(unsigned(r.LastRecCnt_2) - 1);
    -- clear pendign triggers (they were too early)        ← 原文拼写
    v.ExtTrigPending_2 := '0';                             -- 丢弃（不延迟）外部 pending
    v.SwTrigPending_2  := '0';                             -- 丢弃（不延迟）软件 pending
elsif r.Trigger_2 = '1' then
    v.LastRecCnt_2 := MinRecPeriod;                        -- 刚兑现触发→重装冷却
end if;
```

三点精读：

1. **顺序很关键**。`TrigNow_2` 是 `p_comb` 的局部 `variable`（见 [hdl/data_rec.vhd:L150](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L150) 与 [L225-L228](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L225-L228)），先由三类触发源合成，再被本块**同拍改写为 0**，最后才被 [L246 的 `case r.State_2`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L246) 消费。变量（而非信号）允许这种"同拍顺序计算"，这正是两进程法里用 `variable v` 的价值。
2. **重装用的是 `r.Trigger_2`（上一拍登记的触发）**。`Trigger_2` 在 `WaitTrig_s` 命中时由 [L262](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L262) 置位，经 `p_seq` 登记进 record；下一拍本块的 `elsif r.Trigger_2 = '1'` 才看到它并重装。因此冷却从"兑现触发的下一拍"开始，长度恰为 `MinRecPeriod`。
3. **第一分支是运行时调小保护**。若软件在冷却中把 `MinRecPeriod` 写得更小（新值 < 当前剩余），立即把计数器钳到新值，冷却随即缩短。注意反向不成立：把 `MinRecPeriod` 调大**不会**延长正在进行的冷却（条件是 `<`，新值更大不命中），要等下一次触发才会按新值重装。

`LastRecCnt_2` 的复位值在 `p_seq` 里显式置 0（[hdl/data_rec.vhd:L384](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L384)）——所以上电后/复位后没有冷却，**第一次触发总是允许的**。`MinRecPeriod` 端口定义在 [hdl/data_rec.vhd:L68](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L68)，对应软件寄存器 `Reg_MinRecPeriod_Addr_c = 16#002C#`（[hdl/data_rec_register_pkg.vhd:L55](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L55)）。

#### 4.1.4 代码实践

**目标**：用 `top_tb_case1` 的 MinRecPeriod 子测试验证"冷却期内触发被丢弃"。

**步骤**（源码阅读型实践，不需自行运行即可理解；若本地有 Modelsim 可按 u1-l3 实际跑）：

1. 打开 [testbench/top_tb/top_tb_case1_pkg.vhd:L140-L159](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case1_pkg.vhd#L140-L159)，定位 "Minimum Trigger Recording Period not respected" 段。
2. 注意 [L141](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case1_pkg.vhd#L141) 写入 `MinRecPeriod = integer((50 us)/ClockPeriod_c)`。`ClockPeriod_c = 1/160 MHz = 6.25 ns`（[top_tb_pkg.vhd:L27-L28](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_pkg.vhd#L27)），故 `MinRecPeriod ≈ 8000` 个数据时钟周期。
3. 跟踪三段录制：
   - **第一次**（[L144-L148](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case1_pkg.vhd#L144-L148)）：Arm 后 `InputSamples(10, …, trigAt=2)` 在第 2 个样本发外部触发脉冲。此时 `LastRecCnt_2=0`（复位后未冷却），触发被允许 → 期望 `Done`。
   - **第二次**（[L150-L154](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case1_pkg.vhd#L150-L154)）：从 `Done_s` 直接再 Arm，几乎立刻又发触发脉冲。但第一次兑现触发已重装 `LastRecCnt_2≈8000`，仍在冷却中 → 触发被撤销、pending 被清 → 期望状态停在 **`WaitTrig_c`**（注释 "Period Not Respected"）。
   - **第三次**（[L155-L158](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case1_pkg.vhd#L155-L158)）：`wait for 50 us` 让计数器减到 0，再发触发 → 期望 `Done`（"Recording happens after period"）。最后 [L159](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case1_pkg.vhd#L159) 把 `MinRecPeriod` 写回 0，关闭冷却。

> 说明：case1 自身没有写 `TrigEna`，外部触发（bit0）是由 [top_tb_case0_pkg.vhd:L108](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case0_pkg.vhd#L108) 在前一用例里使能并保留下来的（寄存器跨用例不清零）。

**需要观察的现象**：第二次 Arm 后状态寄存器读到 `WaitTrig_c(=2)` 而非 `Done_c(=4)`；等待 50 µs 后第三次才能进入 `Done_c`。

**预期结果**：若 4.1.3 的三个分支正确，三段 expect 全部通过。若本地无仿真器，标「待本地验证」，但上述时序推演可直接据源码得出。

#### 4.1.5 小练习与答案

**练习 1**：若把 `MinRecPeriod` 设为 5、`TrigInputs_g=1`、外部触发为一个持续高电平的信号（非脉冲），冷却结束后会立刻录制吗？

**答案**：不会立刻录制。外部触发是**上升沿**检测（[hdl/data_rec.vhd:L211-L215](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L211-L215)，本拍为 1 且上一拍为 0）。冷却期内即便信号一直为高，pending 也被反复清除；冷却结束后信号仍是高电平、没有新的上升沿，故不会触发。要再次触发必须先拉低再拉高。（这正是 case0 验证过的边沿检测语义。）

**练习 2**：软件在冷却中（剩余 3000）把 `MinRecPeriod` 从 8000 改写为 1000，再改写为 20000。两次改写后剩余计数各是多少？

**答案**：第一次 8000→1000 时，`1000 < 3000` 命中第一分支，计数器钳到 **1000**（冷却缩短）。第二次 1000→20000 时，`20000 < 1000` 不成立，落到第二分支继续递减，冷却**不延长**；要等下一次触发兑现才按 20000 重装。

---

### 4.2 触发计数器：TrigCnt_3 与 TrigCntClr

#### 4.2.1 概念说明

`TrigCnt` 是一个 32 位诊断计数器，统计"自上次清零以来，记录器**真正兑现**了多少次触发"。注意它计的不是"触发源翻转次数"，而是"实际进入 `PostTrig_s`、产生 `Trigger_2` 脉冲的次数"——因此被 `MinRecPeriod` 抑制掉、或因 `TrigEna` 关闭而未生效的触发都不计数。它是评估"现场到底抓了多少段波形"的最直接指标。

软件可通过 `Cfg` 寄存器的 `TrgCntClr` 位发一个单拍脉冲把它清零（例如每次开 run 前清零，之后读出的就是本次 run 的触发数）。

#### 4.2.2 核心流程

计数器 `TrigCnt_3`（Stage 3 对齐，32 位 `unsigned`）的更新规则：

\[
\text{TrigCnt}_{n+1} =
\begin{cases}
0 & \text{若 } \text{TrigCntClr} = 1 \quad \text{(软件清零，优先级最高)} \\
\text{TrigCnt}_{n} + 1 & \text{若 } \text{Trigger}_{n} = 1 \quad \text{(兑现一次触发)} \\
\text{TrigCnt}_{n} & \text{否则（保持）}
\end{cases}
\]

清零优先于递增（`if/elsif` 顺序）。计数器无饱和保护，但 32 位在典型采样率下足够长时间不溢出。

#### 4.2.3 源码精读

计数逻辑在 `p_comb` 的 Stage 3 段（[hdl/data_rec.vhd:L323-L328](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L323-L328)）：

```vhdl
-- trigger counter
if TrigCntClr = '1' then
    v.TrigCnt_3 := (others => '0');          -- 软件清零（优先）
elsif r.Trigger_2 = '1' then
    v.TrigCnt_3 := r.TrigCnt_3 + 1;          -- 兑现触发则 +1
end if;
```

注意一个**流水级跨级**细节：清零条件用的是输入端口 `TrigCntClr`（Stage 0 时刻），而递增条件用的是 `r.Trigger_2`（Stage 2 寄存值）。这是因为 `TrigCntClr` 来自 AXI 域、经 `pulse_cc` 跨域后的脉冲（见封装层 [data_rec_vivado_wrp.vhd:L416/L435](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L416)），与 `Trigger_2` 的时序基准不同；只要两者不在同一拍同时命中即可，而实际应用中软件清零与触发兑现几乎不可能同拍，`if/elsif` 的优先级 further 保证了确定性。

对外输出在 [hdl/data_rec.vhd:L355](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L355)（`TrigCnt <= std_logic_vector(r.TrigCnt_3)`），复位值 0（[L381](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L381)）。寄存器侧：读地址 `Reg_TrigCnt_Addr_c = 16#0020#`（[data_rec_register_pkg.vhd:L46](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L46)），清零位 `Reg_Cfg_TrgCntClr_Idx_c = 16`（[L31](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L31)，即 `Cfg` 寄存器 bit16）。封装层把该位解码成单拍脉冲 `reg_cfg_trigcntclr`（[data_rec_vivado_wrp.vhd:L317](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L317)）。

#### 4.2.4 代码实践

**目标**：理解"清零优先于递增"与"只计兑现的触发"。

**步骤**：

1. 在 [hdl/data_rec.vhd:L323-L328](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L323-L328) 把 `if TrigCntClr = '1'` 与 `elsif r.Trigger_2 = '1'` 对调（仅思想实验，**不要改源码**），分析会发生什么。
2. 对照 case1 第二次"被抑制的触发"：那次外部触发产生了上升沿、置过 `ExtTrigPending_2`，但因 `MinRecPeriod` 冷却被清掉、`TrigNow_2` 被撤销，故 `Trigger_2` 从未置 1。

**需要观察的现象 / 预期结果**：

- 对调分支后，若软件恰在兑现触发同拍写 `TrgCntClr`，计数器会变成 1 而非 0——清零不再"绝对优先"。原代码的 `if/elsif` 顺序保证清零总是胜出。
- case1 第二次不录制，意味着 `TrigCnt` 在整段 MinRecPeriod 测试里**只 +1 两次**（第一次和第三次），第二次"看起来发了触发"但被抑制，不计入。这就是"只计兑现的触发"的含义。

#### 4.2.5 小练习与答案

**练习**：软件想"读当前触发数的同时清零，下一段 run 从 0 开始"，能否用一次 AXI 读改写实现？为什么本设计额外提供了 `TrgCntClr` 脉冲位？

**答案**：不宜用"读改写"（先读 `0x0020`、软件清零、再写回 0），因为在读与写之间可能又有新触发兑现，读到的值已过期，写回 0 会丢掉这段时间的计数。本设计提供 `Cfg.bit16` 的 `TrgCntClr` 单拍脉冲清零（封装层 `reg_cfg_arm`/`reg_cfg_trigcntclr` 都由 `reg_wr and wdata(bit)` 解码成单拍，见 [L316-L317](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L316-L317)），硬件在同一拍原子地把计数器置 0，后续触发从 0 重新累加，无竞态。

---

### 4.3 Done 持续时长计数器：DoneTime_3

#### 4.3.1 概念说明

`DoneTime` 回答的是另一个诊断问题：**一段录制完成后，记录器在 `Done_s` 状态"干等"了多久，主机才来读走数据并 Ack？** 它以数据时钟周期为单位度量"主机读出延迟"。如果这个值经常很大，说明主机处理太慢、数据长时间滞留在 RAM 没被读走，可能成为系统瓶颈；配合 `MinRecPeriod` 一起看，就能判断录制间隔是否留足了读出时间。

#### 4.3.2 核心流程

`DoneTime_3`（Stage 3，32 位 `unsigned`）的更新规则：

\[
\text{DoneTime}_{n+1} =
\begin{cases}
0 & \text{若 } \text{Trigger}_{n} = 1 \quad \text{(新一段录制开始，归零)} \\
\text{DoneTime}_{n} + 1 & \text{若 } \text{State}_{n} = \text{Done\_s 且 } \text{DoneTime}_{n} \neq 2^{32}-1 \quad \text{(在 Done 中计时，饱和)} \\
\text{DoneTime}_{n} & \text{否则（保持）}
\end{cases}
\]

两个要点：① 归零发生在**下一次触发兑现**（`Trigger_2=1`），而非进入 Done 时——因为一段录制的 Done 期紧接在下一次触发之前结束；② 有饱和保护，到 `0xFFFFFFFF` 后不再增加，避免回绕成 0 造成误读。

#### 4.3.3 源码精读

计时逻辑在 [hdl/data_rec.vhd:L330-L337](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L330-L337)：

```vhdl
-- Done time counter
if r.Trigger_2 = '1' then
    v.DoneTime_3 := (others => '0');                 -- 新触发→归零
elsif r.State_2 = Done_s then
    if r.DoneTime_3 /= X"FFFFFFFF" then              -- 饱和保护
        v.DoneTime_3 := r.DoneTime_3 + 1;            -- 每拍 +1
    end if;
end if;
```

精读：

- `r.State_2 = Done_s` 时**每拍**（不限 `In_Vld`）+1，所以单位是数据时钟周期，与采样率无关——这和 `LastRecCnt_2` 的逐拍递减一致。
- 饱和保护用 `X"FFFFFFFF"` 比较挡住 +1，确保 32 位无符号不会回绕。
- 与 4.2 的 `TrigCnt_3` 对比：两者都在 Stage 3、都用 `r.Trigger_2` 作为关键事件，但一个递增（计数事件）、一个归零（重开窗口），且 `DoneTime` 多了饱和保护而 `TrigCnt` 没有——因为触发次数"溢出取模"在工程上可接受，而时长"回绕成小数"会严重误导诊断。

对外输出 [L356](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L356)，复位值 0（[L382](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L382)）。读地址 `Reg_DoneTime_Addr_c = 16#0024#`（[data_rec_register_pkg.vhd:L48](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L48)）。封装层把它和 `State`、`TrigCnt` 一起经 `status_cc` 跨到 AXI 域供主机读取（[data_rec_vivado_wrp.vhd:L352/L371](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L352)）。

#### 4.3.4 代码实践

**目标**：用 `DoneTime` 评估主机读出延迟，并理解饱和点。

**步骤**：

1. 假设数据时钟 160 MHz，一段录制进入 `Done_s` 后主机 1 ms 才来读。估算 `DoneTime` 读数。
2. 在源码里把 [L334](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L334) 的饱和保护去掉（思想实验），分析长时间挂 Done 的后果。

**需要观察的现象 / 预期结果**：

- 1 ms × 160 MHz = 160 000 个周期，`DoneTime` 读数约为 160 000（0x27100），远未饱和。
- 去掉饱和保护后，若主机长时间不来读（> 2³² 周期 ≈ 26.8 s @160 MHz），计数器回绕成 0 继续涨，读数会从大数突变到小数，无法判断"到底等了多久"，这正是饱和保护存在的理由。

> 待本地验证：若有仿真环境，可在 case1 各段录制后读 `Reg_DoneTime_Addr_c`，观察其随 `wait` 时长增长。

#### 4.3.5 小练习与答案

**练习**：`DoneTime` 的归零为什么用 `r.Trigger_2 = '1'`（下一次触发），而不用"进入 `Done_s`"？两种选择在"只录制一次就停"的场景下读数有何不同？

**答案**：用"下一次触发"归零，使 `DoneTime` 表达的是"上一段录制完成后、到下一段开始前的等待时长"，符合"读出延迟"语义。若改用"进入 Done"归零，则计数会从进入 Done 那拍重新开始——在"只录一次就停"（之后再无触发）的场景下，`Trigger_2` 永不再来，当前实现会让 `DoneTime` 一路涨到饱和，正确反映"主机一直没来读/确认"；而"进入 Done 归零"的版本会从 0 重新计，同样能反映等待时长。两者在单次场景差别不大，但当前实现对"连续录制"更有意义：它测的是**每两次录制之间**的空隙。

---

### 4.4 Trig_Out 转发与 TrigForwarding_g

#### 4.4.1 概念说明

前三篇讲的都是"触发如何**进入**记录器"。v2.4 新增的 `Trig_Out` 解决的是反方向的需求：把记录器**内部裁决后真正使用的触发**引出去，给片上其他逻辑用。典型场景是把多个记录器级联（一个记录器触发时，通过 `Trig_Out` 通知另一个记录器同步录制），或与触发计数、时间戳逻辑同步。

关键区别于 `Trig_In`：`Trig_Out` 转发的**不是**原始外部触发输入，而是经 `TrigEna` 掩码、`MinRecPeriod` 冷却、`In_Vld` 门控之后**真正兑现**的 `Trigger_2`——即"这一拍确实开始了一段录制"。因此接到下游逻辑时，含义明确无歧义。

`TrigForwarding_g` 是一个布尔 generic，**默认 false**。它只决定 `Trig_Out` 这个端口在 Vivado IP 里**是否对外暴露**，并不影响核心 RTL 的任何逻辑——核心里 `Trig_Out <= r.Trigger_2` 永远存在。不需要转发时设 false，IP 引脚更干净；需要时设 true，端口出现。

#### 4.4.2 核心流程

核心记录器内：

```
触发源 → TrigEna 掩码 → MinRecPeriod 冷却 → In_Vld 门控 → TrigNow_2
                                                                ↓ (WaitTrig_s 命中)
                                                            Trigger_2 (单拍脉冲)
                                                                ↓
                                              ┌─────────────────┴────────────────┐
                                              ↓                                   ↓
                                          Trig_Out 端口                        内部使用
                                        (数据时钟域 Clk)                  (驱动 TrigCnt/DoneTime/FirstSpl)
```

封装层里 `Trig_Out` 直通核心（同一数据时钟域，无需跨域）；与之相对，`Done` 信号要送到 AXI 域产生中断 `Done_Irq`，必须跨时钟域。

#### 4.4.3 源码精读

核心的转发只有一行（[hdl/data_rec.vhd:L393](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L393)）：

```vhdl
-- Trigger output
Trig_Out <= r.Trigger_2;        -- 转发"真正兑现"的触发，数据时钟域
```

`Trig_Out` 是核心 entity 的固定输出端口（[hdl/data_rec.vhd:L49](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L49)），record 里也有对应字段（[L121](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L121)）。它是 `r.Trigger_2`（已寄存的 Stage 2 信号）的组合输出，因此本质是**数据时钟域的单拍脉冲**，宽度为一个 `Clk` 周期。

封装层把核心的 `Trig_Out` 直接接到顶层端口（[hdl/data_rec_vivado_wrp.vhd:L484](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L484) `Trig_Out => Trig_Out`）——**没有任何跨域逻辑**，因为顶层 `Trig_Out` 端口（[L54](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L54)）就属于数据时钟域，留给用户的级联逻辑也跑在数据时钟域。

`TrigForwarding_g` 的**唯一作用**在 IP 打包层面：在 [scripts/package.tcl:L102](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/scripts/package.tcl#L102) 用 `add_port_enablement_condition "Trig_Out" "\$TrigForwarding_g = true"` 声明端口使能条件；生成的 IP-XACT 描述 [component.xml:L774](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/component.xml#L774) 把它落地为 `PORT_ENABLEMENT.Trig_Out` 依赖 `$TrigForwarding_g = true`。注意封装层 VHDL 里 `TrigForwarding_g` 虽作为 generic 声明（[data_rec_vivado_wrp.vhd:L31](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L31)），但**架构体中并未用它做任何逻辑选择**——它是纯打包开关。Changelog 记载该特性由 v2.4 引入（[Changelog.md:L1-L3](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/Changelog.md#L1-L3)）。

**与 `Done_Irq` 的对照**（本讲实践任务的第二问）：`Done_Irq` 是顶层中断端口（[data_rec_vivado_wrp.vhd:L55](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L55)），它的来源是核心的 `Done` 脉冲（[L497 `Done => port_done`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L497)），经 `psi_common_pulse_cc` 从数据域跨到 AXI 域（[L438/L440-L453](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L438)，[L455](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L455) `Done_Irq <= CcPToAxOut(...)`）。跨域用 `pulse_cc`（而非 `status_cc`）正是因为 Done 是单拍事件、要保证跨域后仍是单拍（跨时钟域策略详见 u5-l2）。

| 信号 | 所在时钟域 | 转发的事件 | 含义 | 跨域处理 |
|------|-----------|-----------|------|---------|
| `Trig_Out` | 数据域 `Clk` | `Trigger_2` | 一段录制**开始**（触发已兑现） | 无，直通 |
| `Done_Irq` | AXI 域 `s00_axi_aclk` | `Done` | 一段录制**完成**（数据就绪可读） | `pulse_cc` 数据→AXI |

#### 4.4.4 代码实践

**目标**：在测试平台里观测 `Trig_Out` 脉冲，确认它与"录制开始"对齐、且在数据时钟域。

**步骤**：

1. 看顶层测试平台把 `TrigForwarding_g` 设为 `TRIG_FWD = true`（[top_tb.vhd:L43/L106](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb.vhd#L43)），并把 `Trig_Out` 接入 `FromDut` 记录（[top_tb_pkg.vhd:L56](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_pkg.vhd#L56) 的 `Out_t` 含 `Trig_Out` 字段）。
2. 在任意一个会录制的用例（如 case0）里，设想在波形窗中把 `FromDut.Trig_Out` 与 `State`（状态寄存器）对齐观察。

**需要观察的现象 / 预期结果**：`Trig_Out` 出现单拍高电平的那一拍，状态机恰好从 `WaitTrig_s` 迁入 `PostTrig_s`（`Trigger_2` 命中拍）；该脉冲只持续一个 `Clk` 周期（160 MHz → 6.25 ns），与 AXI 时钟（125 MHz → 8 ns）异步。

> 待本地验证：波形观测需仿真器。无仿真器时，上述对齐关系可直接由 [L262 `v.Trigger_2 := '1'`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L262) 与 [L393](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L393) 推得。

#### 4.4.5 小练习与答案

**练习 1**：`Trig_Out` 与外部触发输入 `Trig_In` 在时间上是否完全对齐？为什么？

**答案**：不对齐。`Trig_In` 进入核心后要经两拍流水（Stage 0/1）做上升沿检测、置 `ExtTrigPending_2`，再经 `TrigEna`/`MinRecPeriod`/`In_Vld` 裁决，在 `WaitTrig_s` 才产生 `Trigger_2` 并由 `Trig_Out` 转发。所以 `Trig_Out` 相对外部触发沿有数拍延迟，且只有在触发真正被采纳时才出现——这正是"转发裁决后触发"的价值（下游不会收到被丢弃的触发）。

**练习 2**：若用户把 `Trig_Out` 接到一个跑在 AXI 时钟域（125 MHz）的计数器，会出什么问题？应如何处理？

**答案**：`Trig_Out` 是 160 MHz 数据域的单拍脉冲（6.25 ns 宽），直接给 125 MHz（8 ns 周期）逻辑采样可能因跨时钟域采样不稳而漏采或亚稳态。正确做法是在两者之间插入一个 `psi_common_pulse_cc`（与封装层处理 `Done`→`Done_Irq` 的方式一致），把脉冲可靠地搬到 AXI 域。

---

## 5. 综合实践

把本讲四个模块串起来，完成规格里的综合任务：

**任务一：解释 case1 "第二次 Arm 立即触发不录制、等 50 µs 才生效"。**

参考 [top_tb_case1_pkg.vhd:L140-L159](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case1_pkg.vhd#L140-L159) 与 4.1 的推演，组织如下解释：

1. [L141](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case1_pkg.vhd#L141) 设 `MinRecPeriod = 8000` 周期（50 µs @160 MHz）。
2. 第一次录制兑现触发后，[hdl/data_rec.vhd:L239-L240](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L239) 的 `elsif r.Trigger_2 = '1'` 分支把 `LastRecCnt_2` 重装为 8000，冷却启动。
3. 第二次 Arm 紧接着发外部触发（[L150-L152](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case1_pkg.vhd#L150-L152)）。此时 [L233](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L233) `r.LastRecCnt_2 /= 0` 仍成立，进入冷却分支：`TrigNow_2` 被强制为 0（[L234](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L234)），`ExtTrigPending_2` 被清（[L237](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L237)）。状态机在 `WaitTrig_s` 因 `TrigNow_2=0` 不迁移，停在 `WaitTrig_c`（[L154](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case1_pkg.vhd#L154) 期望通过）。
4. [L155 `wait for 50 us`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case1_pkg.vhd#L155) 期间 `LastRecCnt_2` 每拍减 1，约 8000 拍后归 0，冷却结束。
5. 第三次发触发（[L156](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case1_pkg.vhd#L156)）时 `LastRecCnt_2=0`，三个分支都不命中，`TrigNow_2` 不被撤销，状态机正常迁入 `PostTrig_s` 并完成 → [L158 期望 `Done`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case1_pkg.vhd#L158)。

核心一句：**冷却期内触发的命运是"被丢弃"而非"被延迟"**，所以必须等冷却结束、重新发一个触发才能录制；旧的触发请求不会在冷却结束后补发。

**任务二：说明 `Trig_Out` 与 `Done_Irq` 的时钟域与转发事件。**

- `Trig_Out`：**数据时钟域 `Clk`**（160 MHz）；转发 `Trigger_2`，事件是"**一段录制开始**（触发经裁决兑现）"；封装层直通、不跨域（4.4.3 表格与 [L393](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L393)）。
- `Done_Irq`：**AXI 时钟域 `s00_axi_aclk`**（125 MHz）；转发 `Done`，事件是"**一段录制完成**（数据就绪可读）"；经 `psi_common_pulse_cc` 从数据域跨到 AXI 域（[L438-L455](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L438)）。

二者一前一后勾勒出一次录制的"起"与"讫"，且分处两个时钟域——这正体现了本 IP 双时钟域设计的典型分工：数据域管采集与触发，AXI 域管主机中断与读出。

## 6. 本讲小结

- `MinRecPeriod` 通过倒计数器 `LastRecCnt_2` 实现"两次录制最小间隔"冷却：触发兑现后重装、每拍递减、归零前强制 `TrigNow_2:=0` 并**丢弃**（而非延迟）外部/软件 pending；递减不受 `In_Vld` 门控，单位是数据时钟周期。
- 三个分支的优先级与语义：软件调小→钳位、冷却中→递减并清 pending、刚兑现触发→重装；复位后 `LastRecCnt_2=0`，首次触发必放行。
- `TrigCnt_3` 只在 `Trigger_2=1`（真正兑现）时 +1、`TrigCntClr` 单拍清零（优先），是"实际录制段数"指标。
- `DoneTime_3` 在 `Done_s` 每拍 +1、下次触发归零、到 `0xFFFFFFFF` 饱和保护，度量主机读出延迟。
- `Trig_Out <= r.Trigger_2` 转发"裁决后兑现的触发"，处于数据时钟域；`TrigForwarding_g`（默认 false）仅在 IP 打包层控制端口是否暴露，不影响核心逻辑。
- `Trig_Out`（数据域，录制开始）与 `Done_Irq`（AXI 域，录制完成，经 `pulse_cc` 跨域）一起界定一次录制的起讫。

## 7. 下一步学习建议

至此触发机制单元（u4）完结，你已完整理解三类触发源、它们的掩码合成、以及本讲的冷却与计数/转发周边。接下来：

- **进入 u5（Vivado 封装）**：本讲多次提到 `pulse_cc`/`status_cc` 跨时钟域与 AXI 解码，但都一带而过。u5-l1 讲 AXI4 Slave 寄存器/存储解码，u5-l2 系统讲解 `status_cc` 与 `pulse_cc` 的选用策略——为什么 `Done` 走 `pulse_cc` 而 `State/TrigCnt/DoneTime` 走 `status_cc`，答案在那里。
- **进入 u6（验证与集成）**：u6-l2 逐一覆盖 case0~case5，本讲的 case1 MinRecPeriod 子测试是其中一环；u6-l3 讲 EPICS 模板如何把 `TrigCnt`、`DoneTime`、`MinRecPeriod` 这些寄存器暴露成控制系统的 PV。
- **源码再读**：把 [hdl/data_rec.vhd:L225-L241](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L225-L241) 的"触发合成 + 冷却"两段连起来通读一遍，确认你能在脑中跑通"一个外部触发从 Trig_In 到 Trig_Out"的完整链路——这是检验是否真正掌握 u4 全单元的最好方式。
