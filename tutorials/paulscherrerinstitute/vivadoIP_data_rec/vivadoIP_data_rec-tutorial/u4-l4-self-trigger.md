# 自触发：范围检测、符号与进入/退出

## 1. 本讲目标

本讲聚焦三类触发源中最「智能」的一种——**自触发（self-trigger）**。学完后你应当能够：

- 说清自触发与外部触发、软件触发的本质差别：它**不靠外部事件，而是直接对采集到的数据本身做范围判定**。
- 读懂 `data_rec.vhd` 中 Stage 1 的 `StInRange_1` 范围判定逻辑，理解为什么代码「先按 unsigned 试、再按 signed 试」。
- 读懂 Stage 2 的 `StEnter_2` / `StExit_2` 边沿检测，能区分 **OnEnter（进入范围触发）** 与 **OnExit（离开范围触发）**。
- 掌握 `SelfTrigChEna`（按通道使能）、`SelfTrigOnEnter` / `SelfTrigOnExit`（按方向选择）三个配置端口如何与寄存器 `Reg_SelftrigCfg_Addr_c (0x0018)` 的字段对应。
- 能针对一个跨零点的 signed 范围，手算出自触发会落在哪一个样本上。

本讲承接 [u4-l1](u4-l1-trigger-sources-and-masking.md) 建立的「三类源 + TrigEna 掩码 + TrigNow_2 合成」总框架，把其中 `StTrig_2` 这一分支彻底展开。

## 2. 前置知识

阅读本讲前，你需要先具备以下概念（均在前序讲义中讲过）：

- **两进程法与 Stage0–3 流水线**（[u3-l3](u3-l3-two-process-and-pipeline.md)）：信号名后的数字后缀就是它所处的流水级编号，例如 `Data_0` 在 Stage 0、`StInRange_1` 在 Stage 1、`StEnter_2` 在 Stage 2。
- **状态机 Idle→PreTrig→WaitTrig→PostTrig→Done**（[u3-l2](u3-l2-recorder-state-machine.md)）：自触发的最终输出 `TrigNow_2` 只在 `WaitTrig_s` 状态下被消费，用来推进到 `PostTrig_s`。
- **三类触发源与 TrigEna 掩码**（[u4-l1](u4-l1-trigger-sources-and-masking.md)）：自触发对应 `TrigEna` 的 bit2（`Reg_TrigEna_SelfIdx_c`），合成公式为
  \[
  \text{TrigNow\_2} = \big((\text{StTrig\_2}\wedge\text{TrigEna}(2)) \;\vee\; \ldots\big) \wedge \text{In\_Vld}(1)
  \]
- **VHDL `unsigned` 与 `signed` 的比较语义**：同一个 `std_logic_vector`，转成 `unsigned` 时按无符号数解释（最高位是数值位），转成 `signed` 时按二进制补码解释（最高位是符号位）。这是本讲「双判」设计的核心。
- **寄存器地图**（[u2-l2](u2-l2-register-and-memory-map.md)）：自触发相关寄存器位于 `0x0010`–`0x0018`。

> 一句话直觉：外部触发像「按门铃」，软件触发像「定时开关」，自触发像「装了一个光电传感器——只要光强落到某区间就报警」。本讲就是拆这个「光电传感器」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [hdl/data_rec.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd) | 核心记录器。本讲的全部运算都在这里：Stage 1 范围判定、Stage 2 边沿检测、`TrigNow_2` 合成。 |
| [hdl/data_rec_register_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd) | 寄存器地址与字段位常量。自触发的 4 个寄存器地址与 `SelfTrigCfg` 的字段移位量都定义在此。 |
| testbench/top_tb/top_tb_case2_pkg.vhd | 自触发专用测试用例，含跨零点 signed 范围的 Exit/Enter 场景（本讲实践的对照基准）。 |
| testbench/top_tb/top_tb_pkg.vhd | 测试平台公共过程 `InputSamplesNoCh`（生成线性激励）与 `CheckDataNoCh`（校验录制起点）。 |
| hdl/data_rec_vivado_wrp.vhd | 封装层。把 `Reg_SelftrigCfg_Addr_c` 寄存器拆成 `SelfTrigChEna` / `OnExit` / `OnEnter` 三路端口（补充阅读）。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **范围判定**：Stage 1 判断每个通道样本是否落在 `[SelfTrigLo, SelfTrigHi]` 内（先 unsigned 再 signed）。
2. **边沿检测**：Stage 2 把范围判定结果做前后拍比对，产生 `StEnter_2` / `StExit_2`，再合成 `StTrig_2`。
3. **通道使能与方向选择**：`SelfTrigChEna` 选通道、`OnEnter` / `OnExit` 选方向，以及它们在寄存器 `0x0018` 中的位映射。

### 4.1 范围判定：先 unsigned 再 signed

#### 4.1.1 概念说明

自触发的第一步，是回答一个看似简单的问题：**当前样本是否落在我关心的区间里？** 区间由两个寄存器给定：

- `SelfTrigLo`（地址 `0x0010`）：下界；
- `SelfTrigHi`（地址 `0x0014`）：上界。

判定表达式直觉上就是 `Lo <= sample <= Hi`。难点在于：**`sample` 到底是无符号数还是有符号数？** 同一段比特，比如 16 位的 `0xFFF6`，作为 `unsigned` 是 `65526`，作为 `signed` 补码却是 `-10`。一个跨零点的区间（如 `-10..10`）只能用 `signed` 解释才有意义；而一个纯正数的区间（如 `5..10`）用 `unsigned` 就够了。

该 IP 的设计选择是「**不强制用户声明符号，而是两种解释都试一次**」：只要其中任意一种成立，就算「在范围内」。这样用户无论是测原始 ADC 码（无符号）还是测已转成补码的物理量（有符号），同一套寄存器都能工作。

#### 4.1.2 核心流程

范围判定在 **Stage 1** 完成，每来一个有效样本（`In_Vld(0)=1`）刷新一次：

```
对每个通道 i = 0 .. NumOfInputs_g-1:
    取本拍样本 Data_0(i)
    if  (Data_0(i) as unsigned) <= (Hi as unsigned) and
        (Data_0(i) as unsigned) >= (Lo as unsigned):
        StInRange_1(i) := '1'        -- 无符号命中
    elsif (Data_0(i) as signed) <= (Hi as signed) and
          (Data_0(i) as signed) >= (Lo as signed):
        StInRange_1(i) := '1'        -- 有符号命中
    else:
        StInRange_1(i) := '0'
同时把上一拍的 StInRange_1 暂存到 StInRangeLast_1   -- 供下一级边沿检测
```

注意两个细节：

- 判定**只在新样本到来时**进行（`if r.In_Vld(0) = '1'`），断流期间结果保持不变，避免重复触发。
- 同一拍里 `StInRangeLast_1` 被赋成「旧的 `StInRange_1`」，所以 `StInRangeLast_1` 永远比 `StInRange_1` 旧一个有效样本，正好留给 4.2 的边沿检测用。

#### 4.1.3 源码精读

判定逻辑全部在 `p_comb` 的 Stage 1 段：[hdl/data_rec.vhd:172-188](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L172-L188)

```vhdl
-- Self Trigger In Range detection
if r.In_Vld(0) = '1' then
    v.StInRangeLast_1 := r.StInRange_1;          -- 暂存上一拍结果
    for i in 0 to NumOfInputs_g-1 loop
        -- unsigned in range
        if  (unsigned(r.Data_0(i)) <= unsigned(SelfTrigHi)) and
            (unsigned(r.Data_0(i)) >= unsigned(SelfTrigLo)) then
            v.StInRange_1(i) := '1';
        -- signed in range
        elsif (signed(r.Data_0(i)) <= signed(SelfTrigHi)) and
              (signed(r.Data_0(i)) >= signed(SelfTrigLo)) then
            v.StInRange_1(i) := '1';
        else
            v.StInRange_1(i) := '0';
        end if;
    end loop;
end if;
```

- 第 173 行的 `r.In_Vld(0)` 门控保证只在有效样本时刷新——这呼应了 [u3-l1](u3-l1-data-rec-entity.md) 讲过的「`In_Vld` 同时门控数据写入与触发判定」。
- 第 177–179 行是无符号判定，第 181–183 行是有符号判定，二者用 `elsif` 串联：**无符号先判，命中即跳过有符号分支**。绝大多数实际场景只会命中其中一个。

这两个信号在 record 里声明为每通道一比特的向量：[hdl/data_rec.vhd:114-115](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L114-L115)，下标范围是 `NumOfInputs_g-1 downto 0`，与通道一一对应。

#### 4.1.4 代码实践

**目标**：体会「unsigned 与 signed 双判」为什么会改变结果。

**步骤**（源码阅读型，无需综合）：

1. 设想 `InputWidth_g=16`，写一个样本值 `x = 0xFFF6`（十进制补码 `-10`）。
2. 配置 A：`SelfTrigLo=0`、`SelfTrigHi=10`（纯正数区间）。
3. 配置 B：`SelfTrigLo=-10`（即 `0xFFF6`）、`SelfTrigHi=10`。
4. 对照 [hdl/data_rec.vhd:177-183](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L177-L183)，分别手算两种配置下 `StInRange_1` 的值。

**需要观察的现象 / 预期结果**：

- 配置 A：`unsigned(0xFFF6)=65526`，`65526 <= 10` 不成立，无符号分支不命中；`signed(0xFFF6)=-10`，`-10 >= 0` 不成立，有符号分支也不命中 → `StInRange_1='0'`。
- 配置 B：无符号分支同样不命中；有符号分支 `-10 <= 10` 且 `-10 >= -10` 成立 → `StInRange_1='1'`。

结论：**同一个样本，区间写成 `0..10` 还是 `-10..10`，判定结果不同**。这正是「双判」带来的灵活性——也意味着用户配置时必须按数据的真实符号含义来填 `Lo/Hi`。

#### 4.1.5 小练习与答案

**练习 1**：若把整个判定写成「先 signed、再 unsigned」，对结果有影响吗？
**答案**：没有影响。因为 `signed` 命中与 `unsigned` 命中都只把 `StInRange_1` 置 `'1'`，两者是「或」关系，调换 `if/elsif` 顺序不改变最终布尔结果；代码先 unsigned 只是一个书写习惯。

**练习 2**：为什么 `StInRangeLast_1` 的赋值要放在 `if r.In_Vld(0)='1'` 内部，而不是无条件每拍搬运？
**答案**：因为断流（`In_Vld=0`）期间没有新样本，`StInRange_1` 保持不变；若此时也搬运 `StInRangeLast_1`，会让 `Last` 逐步追上 `Current`，破坏「上一拍 vs 本拍」的语义，下一级边沿检测就会出错。门控搬运保证二者始终相差「恰好一个有效样本」。

---

### 4.2 边沿检测：StEnter 与 StExit

#### 4.2.1 概念说明

仅知道「样本在范围内」还不够。设想一段始终在范围内的平稳信号——如果「在范围内」就直接触发，那从录制开始的第一拍就会触发，毫无意义。自触发真正关心的是**跳变的瞬间**：

- **OnEnter（进入）**：信号从「范围外」跳到「范围内」的那一刻。
- **OnExit（离开）**：信号从「范围内」跳到「范围外」的那一刻。

这与示波器里「在上升沿触发」「在下降沿触发」是同一类思想，只不过这里的「沿」是「进入/离开某个电平窗口」的沿，而不是单纯的信号上升/下降沿。边沿检测的本质是**比较相邻两个样本的范围判定结果**：

- 本拍在范围内、上一拍不在 → 进入边沿 `StEnter`。
- 上一拍在范围内、本拍不在 → 离开边沿 `StExit`。

这就是 4.1 里特意保留 `StInRangeLast_1` 的用途。

#### 4.2.2 核心流程

边沿检测在 **Stage 2** 完成，全部是组合变量（无寄存器，当拍生效）：

```
StEnter_2 := StInRange_1      and not StInRangeLast_1   -- 0→1 跳变
StExit_2  := StInRangeLast_1  and not StInRange_1       -- 1→0 跳变

StTrig_2 := '0'
if (SelfTrigOnExit  = '1') and (any bit of (StExit_2  AND SelfTrigChEna) = '1'): StTrig_2 := '1'
if (SelfTrigOnEnter = '1') and (any bit of (StEnter_2 AND SelfTrigChEna) = '1'): StTrig_2 := '1'
```

随后 `StTrig_2` 进入 [u4-l1](u4-l1-trigger-sources-and-masking.md) 讲过的总合成公式，与 `TrigEna(2)` 相与后参与 `TrigNow_2`。

关键点：

- `StEnter_2` / `StExit_2` 是**每通道一比特的向量**，所以「本拍」指的是「当前到达 Stage 1 的那个样本」。
- 触发样本的归属：`StEnter` 置位的那一拍，对应的「当前样本」是**第一个落入范围内的样本**；`StExit` 置位的那一拍，对应的「当前样本」是**第一个落在范围外的样本**。这一点对 4.2.4 的手算至关重要。
- 与外部触发（pending 锁存）不同，自触发**不锁存 pending**：`StTrig_2` 是当拍组合变量，错过这一拍就没了。所以自触发只在 `WaitTrig_s` 状态被即时消费。

#### 4.2.3 源码精读

边沿检测与方向选择：[hdl/data_rec.vhd:196-205](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L196-L205)

```vhdl
-- Self Trigger
StEnter_2 := r.StInRange_1 and not r.StInRangeLast_1;
StExit_2  := r.StInRangeLast_1 and not r.StInRange_1;
StTrig_2  := '0';
if (SelfTrigOnExit = '1') and (unsigned(StExit_2 and SelfTrigChEna) /= 0) then
    StTrig_2 := '1';
end if;
if (SelfTrigOnEnter = '1') and (unsigned(StEnter_2 and SelfTrigChEna) /= 0) then
    StTrig_2 := '1';
end if;
```

- 第 197–198 行就是经典的「异或型边沿检测」拆成两半：`A and not B` 抓 0→1，`B and not A` 抓 1→0。
- 第 200、203 行的 `unsigned(... ) /= 0` 是一个常用小技巧：把一个 `std_logic_vector` 当无符号整数，判断它「是否非零」，即「**是否有任意一个使能通道命中**」。`StExit_2 and SelfTrigChEna` 先按位把未使能通道屏蔽，再整体归约成一个触发标志。
- `StTrig_2` 是 `p_comb` 里声明的 **variable**（见 [hdl/data_rec.vhd:147-149](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L147-L149)），所以它在同一拍内立即被下面的 `TrigNow_2` 表达式使用。

`StTrig_2` 随后进入总合成：[hdl/data_rec.vhd:225-228](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L225-L228)，与 `TrigEna(Reg_TrigEna_SelfIdx_c)`（即 bit2）相与，最后再 `and r.In_Vld(1)`，确保触发点落在有效样本上（与 [u4-l1](u4-l1-trigger-sources-and-masking.md) 的总框架一致）。

#### 4.2.4 代码实践（本讲主实践）

**目标**：参考 `top_tb_case2` 的跨零点用例，预测一段线性扫描下 OnExit / OnEnter 分别在哪个样本触发，并用代码行佐证。这正是本讲规格里要求的核心实践。

**场景配置**（与 case2 的「Signed range accross zero」子用例一致）：

- 范围：`SelfTrigLo = -10`、`SelfTrigHi = 10`（跨零点的 signed 区间）。
- 通道：仅使能 ch0。
- 激励：样本从 `-20` 起，每个有效样本 `+1`，线性增长到 `+20`，即 `-20, -19, …, -1, 0, +1, …, +20`。

**操作步骤**：

1. 先列出每个样本是否在 `[-10, 10]` 内：

   | 样本区间 | 是否在范围内 |
   |----------|--------------|
   | `-20 … -11` | 否 |
   | `-10 … +10` | 是 |
   | `+11 … +20` | 否 |

2. 对照 [hdl/data_rec.vhd:197-198](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L197-L198) 的边沿定义做跳变分析。

**需要观察的现象 / 预期结果**：

- **OnEnter 触发在样本 `-10`**：因为样本 `-11` 不在范围、样本 `-10` 在范围，`StInRange_1=1` 且 `StInRangeLast_1=0`，命中 `StEnter_2 := r.StInRange_1 and not r.StInRangeLast_1`（[L197](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L197)）。所以「进入」对应的当前样本是**第一个落入范围的样本 `-10`**。
- **OnExit 触发在样本 `+11`**：因为样本 `+10` 在范围、样本 `+11` 不在范围，`StInRangeLast_1=1` 且 `StInRange_1=0`，命中 `StExit_2 := r.StInRangeLast_1 and not r.StInRange_1`（[L198](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L198)）。所以「离开」对应的当前样本是**第一个走出范围的样本 `+11`**。

**用测试平台佐证**（这些是 case2 中真实存在的断言，不是模拟运行结果）：

- Exit 子用例见 [top_tb_case2_pkg.vhd:148-156](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case2_pkg.vhd#L148-L156)：配置 `Lo=-10, Hi=10, OnExit`，激励从 `-20` 起；校验 `CheckDataNoCh(3, startValue=10, …)`，即录到的第一个样本是 `10`。结合 `PreTrigSpls=1`，这反推出**触发样本是 `11`**（前一个样本 `10` 作为唯一的前触发样本被记下），与上面的预测一致。
- Enter 子用例见 [top_tb_case2_pkg.vhd:158-167](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case2_pkg.vhd#L158-L167)：同样激励，但 `OnEnter`；校验 `CheckDataNoCh(3, startValue=-11, …)`，即录到的第一个样本是 `-11`，反推出**触发样本是 `-10`**，同样与预测一致。

> 结论一句话：**OnEnter 锚定「首个入范围样本」，OnExit 锚定「首个出范围样本」**；二者恰好擦肩而过，相差的就是跳变那一拍。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `SelfTrigOnEnter` 和 `SelfTrigOnExit` **同时**设为 1，会发生什么？
**答案**：看 [L200-205](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L200-L205)，两个 `if` 是并列的「或」关系，任一边沿都把 `StTrig_2` 置 1。所以信号**进出范围各触发一次**。在 `WaitTrig_s` 下，先到的那个边沿就会兑现成 `TrigNow_2`，记录器随即进入 `PostTrig_s`，后一个边沿不会被记录。

**练习 2**：为什么 `StEnter_2`/`StExit_2` 用的是 `r.StInRange_1`（已经是寄存器），而 `StInRangeLast_1` 也是寄存器，却不在 record 里再缓一拍？
**答案**：边沿检测需要的是「同一时刻看相邻两个有效样本」。`StInRange_1` 是本样本的判定结果，`StInRangeLast_1` 在 4.1 中已被刻意安排成「旧一个有效样本」的结果（受同一个 `In_Vld(0)` 门控刷新）。两者在 Stage 2 同拍出现，做组合比较即可得到跳变，无需再多一级寄存器——这正是把范围判定放在 Stage 1、把边沿检测放在 Stage 2 的用意。

---

### 4.3 通道使能与方向选择：SelfTrigChEna / OnEnter / OnExit

#### 4.3.1 概念说明

范围判定与边沿检测都是**每通道独立**算的（向量的每一位对应一个通道）。但用户通常只想让**其中某几个通道**参与自触发——例如 4 路采集里只想监控 ch0，其余 3 路就算越界也不该触发。这就需要两个层次的选择：

- **选哪些通道**：`SelfTrigChEna`，一个每通道一比特的掩码，bit i 置 1 表示「允许通道 i 触发」。
- **选哪个方向**：`SelfTrigOnEnter` / `SelfTrigOnExit`，两个单比特，分别决定「进入范围」和「离开范围」是否算作触发事件。

这三者在 [hdl/data_rec.vhd:58-60](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L58-L60) 是核心记录器的三个独立输入端口；在软件侧，它们被**打包进同一个 32 位寄存器** `Reg_SelftrigCfg_Addr_c (0x0018)`，由封装层拆开。

#### 4.3.2 核心流程

寄存器 `0x0018` 的字段布局（定义在 [data_rec_register_pkg.vhd:38-41](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L38-L41)）：

| 字段 | 位 | 含义 |
|------|-----|------|
| `SelfTrigChEna` | bit `0 .. NumOfInputs_g-1` | 每通道自触发使能（bit0=ch0, bit1=ch1, …） |
| `SelfTrigOnExit` | bit 8 | 离开范围时触发 |
| `SelfTrigOnEnter` | bit 16 | 进入范围时触发 |

软件写一个 32 位整数即可同时配置通道掩码与方向。封装层 `data_rec_vivado_wrp` 把这个字拆成三路送到核心：

```
reg_selftrigchena  <= reg_wdata(0x18/4)(NumOfInputs_g-1 downto 0)   -- 取低 N 位
reg_selftrigonexit <= reg_wdata(0x18/4)(8)                          -- 取 bit8
reg_selftrigonenter<= reg_wdata(0x18/4)(16)                         -- 取 bit16
```

随后这三路再经 `status_cc` 跨时钟域（AXI 域 → 数据域），送到核心的对应端口（跨域细节见 [u5-l2](u5-l2-clock-domain-crossing.md)）。

#### 4.3.3 源码精读

寄存器字段定义：[hdl/data_rec_register_pkg.vhd:38-41](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L38-L41)

```vhdl
constant Reg_SelftrigCfg_Addr_c     : integer := 16#0018#;
constant Reg_SelftrigCfg_ChEnaSft_c : integer := 0;    -- 通道掩码从 bit0 起
constant Reg_SelftrigCfg_ExitSft_c  : integer := 8;    -- OnExit 在 bit8
constant Reg_SelftrigCfg_EnterSft_c : integer := 16;   -- OnEnter 在 bit16
```

封装层从寄存器字中抽字段（注意用 `ToWordAddr` 把字节地址 `0x18` 换算成数组下标 `6`）：[hdl/data_rec_vivado_wrp.vhd:322-324](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L322-L324)

```vhdl
reg_selftrigchena  <= reg_wdata(ToWordAddr(Reg_SelftrigCfg_Addr_c))(NumOfInputs_g-1 downto 0);
reg_selftrigonexit <= reg_wdata(ToWordAddr(Reg_SelftrigCfg_Addr_c))(Reg_SelftrigCfg_ExitSft_c);
reg_selftrigonenter<= reg_wdata(ToWordAddr(Reg_SelftrigCfg_Addr_c))(Reg_SelftrigCfg_EnterSft_c);
```

核心侧，掩码在边沿检测这一级生效——见 4.2 里引用的 [hdl/data_rec.vhd:200-205](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L200-L205)：`StExit_2 and SelfTrigChEna` 先把未使能通道的边沿屏蔽掉，再用 `unsigned(...) /= 0` 归约成「是否有任意使能通道命中」。所以**未使能的通道连边沿都不会贡献**，这与外部触发里 `EnableExtTrig` 的逐路使能思想一致（见 [u4-l2](u4-l2-external-trigger.md)）。

> 区分两个「使能」：`SelfTrigChEna` 作用在**通道**之间（哪几路参与自触发），`TrigEna(2)` 作用在**触发源**之间（自触发这一类是否参与总合成）。前者是本讲，后者是 [u4-l1](u4-l1-trigger-sources-and-masking.md)。

#### 4.3.4 代码实践

**目标**：根据测试用例反推 `Reg_SelftrigCfg_Addr_c` 应写入的值，巩固字段布局。

**步骤**（源码阅读型）：

1. 阅读case2 中「ch2 only, exit」子用例 [top_tb_case2_pkg.vhd:87-96](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case2_pkg.vhd#L87-L96)。
2. 找到它写入 `Reg_SelftrigCfg_Addr_c` 的语句：`axi_single_write(Reg_SelftrigCfg_Addr_c, 1*2**2+2**Reg_SelftrigCfg_ExitSft_c, …)`。
3. 对照字段表，解释这个值每一位的含义。

**需要观察的现象 / 预期结果**：

- `1*2**2 = 4 = 0b100` → `SelfTrigChEna` 的 bit2=1，即**仅使能 ch2**。
- `2**Reg_SelftrigCfg_ExitSft_c = 2**8 = 256` → bit8=1，即 **OnExit**。
- 合计写入值 `4 + 256 = 260 = 0x104`。
- 该子用例随后用 `InputSamplesNoCh` 让所有通道同步线性扫描，但只有 ch2 越界会触发；校验 `CheckDataNoCh(3, startValue=10-2, …)`（[L96](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case2_pkg.vhd#L96)）也确认了触发来自 ch2 的 `+chStep*2` 偏移。

**待本地验证**：如果你手头有 Modelsim/Questa，可在 `sim/` 下按 [u1-l3](u1-l3-run-simulation.md) 跑回归，观察 case2 的这些断言全部通过；若仅做源码阅读，上述字段拆解即为结论。

#### 4.3.5 小练习与答案

**练习 1**：要「同时使能 ch0 和 ch1，方向为 OnEnter」，应向 `0x0018` 写什么值？
**答案**：`SelfTrigChEna` 的 bit0、bit1 置 1 → `0b0011 = 3`；`OnEnter` 在 bit16 → `2**16 = 65536`；合计 `3 + 65536 = 0x10003`。在 VHDL 里写成 `1*2**0 + 1*2**1 + 2**Reg_SelftrigCfg_EnterSft_c`。

**练习 2**：若 `SelfTrigChEna` 写成全 0，但 `TrigEna(2)`（自触发总开关）仍为 1，记录器会怎样？
**答案**：看 [L200-205](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L200-L205)：`StEnter_2 and SelfTrigChEna` 与 `StExit_2 and SelfTrigChEna` 都会因掩码全 0 而归零，`unsigned(...) /= 0` 永不成立，`StTrig_2` 恒为 0。即使自触发总开关开着，也没有任何通道能产生自触发，记录器会一直停在 `WaitTrig_s`（case2 末尾「Check no effect if not enabled」那段正是验证类似的「关闭后不触发」行为，见 [top_tb_case2_pkg.vhd:191-200](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case2_pkg.vhd#L191-L200)）。

---

## 5. 综合实践

把三个最小模块串起来，完成下面这个**贯穿设计任务**（纸面推演，不要求上板）。

**场景**：4 通道采集（`NumOfInputs_g=4`），`InputWidth_g=16`，`MemoryDepth_g=128`，`PreTrigSpls=4`，`TotalSpls=20`。希望「当 ch0 的有符号样本首次进入 `[-5, +5]` 范围时」启动一次录制。

**任务**：

1. **寄存器配置**：写出需要配置的寄存器地址与值——
   - `Reg_TrigEna_Addr_c (0x0028)`：只允许自触发 → 写 `1*2**Reg_TrigEna_SelfIdx_c = 4`。
   - `Reg_SelftrigLo_Addr_c (0x0010)`：写 `-5`。
   - `Reg_SelftrigHi_Addr_c (0x0014)`：写 `+5`。
   - `Reg_SelftrigCfg_Addr_c (0x0018)`：ch0 使能 + OnEnter → 写 `1*2**0 + 2**Reg_SelftrigCfg_EnterSft_c = 1 + 65536 = 0x10001`。
   - `Reg_Pretrig_Addr_c (0x0008)`：写 `4`；`Reg_Totspl_Addr_c (0x000C)`：写 `20`。
   - `Reg_Cfg_Addr_c (0x0004)`：写 `1*2**Reg_Cfg_ArmIdx_c = 1` 执行 Arm。
2. **触发样本预测**：若 ch0 样本序列为 `-20, -18, -16, …, -2, 0, +2, …`（步长 2），第一个落入 `[-5, +5]` 的样本是哪个？
   - 解析：`-6` 不在范围，`-4` 在范围。所以 **OnEnter 触发在样本 `-4`**（依据 [L197](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L197) `StEnter_2 := r.StInRange_1 and not r.StInRangeLast_1`）。
3. **录制窗口预测**：触发样本是 `-4`，`PreTrigSpls=4`，所以录到的第一个样本是 `-4` 往前数 4 个有效样本，即 `-12`；总共录 `TotalSpls=20` 个样本，从 `-12` 到 `+24`（步长 2，共 20 个）。可类比 case2 中 Enter 子用例 `CheckDataNoCh(3, startValue=-11)` 的校验思路（[L167](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case2_pkg.vhd#L167)）。
4. **自检**：如果误把 `Reg_SelftrigCfg_Addr_c` 写成 `0x101`（ch0 + **OnExit** 而非 OnEnter），触发会推迟到哪个样本？
   - 答案：OnExit 锚定「首个出范围样本」。样本从 `-20` 递增，会在 `+6` 首次离开 `[-5,+5]`，于是触发推迟到样本 `+6`，录制窗口整体后移。这正是 4.2 里 OnEnter/OnExit 区别的直接体现。

> 若你想验证：可仿照 [top_tb_case2_pkg.vhd:158-167](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case2_pkg.vhd#L158-L167) 的写法，在测试平台里加一段「ch0、OnEnter、`Lo=-5/Hi=5`」的子用例，用 `CheckDataNoCh(20, startValue=-12, chStep=…)` 校验起点。实际运行「待本地验证」。

## 6. 本讲小结

- 自触发**不依赖外部事件**，而是直接对采集数据做范围判定，是三类触发源里最「智能」的一种。
- 范围判定在 **Stage 1**（[L172-188](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L172-L188)）：每个有效样本刷新 `StInRange_1`，并暂存上一拍结果到 `StInRangeLast_1`；用「先 unsigned、再 signed」的双判兼容无符号码与补码。
- 边沿检测在 **Stage 2**（[L197-205](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L197-L205)）：`StEnter_2` 抓「进入范围」，`StExit_2` 抓「离开范围」，二者经通道掩码与方向选择归约成单比特 `StTrig_2`。
- **OnEnter 锚定首个入范围样本，OnExit 锚定首个出范围样本**——对跨零点区间 `[-10,10]`、样本从 `-20` 升到 `+20` 的扫描，分别在 `-10` 与 `+11` 触发，已由 case2 的校验值反推确认。
- 通道选择 `SelfTrigChEna`（bit0..N-1）与方向选择 `OnExit`(bit8)/`OnEnter`(bit16) 共用寄存器 `0x0018`，由封装层拆成三路端口（[wrp L322-324](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L322-L324)）。
- 自触发**不锁存 pending**，是当拍瞬时变量，只在 `WaitTrig_s` 被即时消费，区别于外部/软件触发的 pending 语义。

## 7. 下一步学习建议

- 阅读 [u4-l5](u4-l5-minrepperiod-counters-trigout.md)，理解 `MinRecPeriod` 如何抑制两次自触发之间过短的间隔，以及 `Trig_Out` 如何把这里产生的 `Trigger_2` 转发给级联记录器。
- 回到 [u5-l1](u5-l1-axi-register-memory-decode.md) / [u5-l2](u5-l2-clock-domain-crossing.md)，看清 `SelfTrigLo/Hi/Cfg` 三个寄存器如何经 AXI 写入、再经 `status_cc` 跨时钟域送达本讲这三个端口，把「软件写寄存器 → 自触发端口」的完整链路补齐。
- 想动手扩展的话，参考 [u6-l4](u6-l4-extension-and-customization.md)：例如把「单一全局 `Lo/Hi`」改成「每通道独立阈值」，需要同时改动范围判定循环、寄存器地图与 EPICS 模板，本讲的 `StInRange_1` 循环就是改动的核心现场。
