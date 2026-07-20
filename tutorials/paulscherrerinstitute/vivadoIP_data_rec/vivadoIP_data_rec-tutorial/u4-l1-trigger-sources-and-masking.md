# 触发源总览与 TrigEna 掩码

## 1. 本讲目标

本讲是「触发机制」单元（u4）的总纲。读完本讲，你应该能够：

- 说清记录器有哪**三类触发源**（外部、软件、自触发），它们各自从哪里来、什么形态；
- 掌握 `TrigEna` 寄存器的三位掩码（Ext/Sw/Self）如何**选择性放行**这三类触发，并能算出常见配置下应写入的数值；
- 画出 `TrigNow_2` 的合成公式，解释为什么它最后还要 `and r.In_Vld(1)`；
- 区分**电平 / 边沿 / sticky（粘滞）**三种触发源的 pending（挂起）处理差异。

本讲只讲「触发是怎么被选中并合成成单拍 `TrigNow`」这一层；外部触发的多路 OR 细节（u4-l2）、软件触发的 free-running 用法（u4-l3）、自触发的范围/符号/进出方向（u4-l4）各自有专门一篇。本讲是它们共同的上层框架。

## 2. 前置知识

阅读本讲前，你应该已经掌握（这些来自依赖讲义）：

- **记录器五状态机**（u3-l2）：`Idle→PreTrig→WaitTrig→PostTrig→Done`。触发判断发生在 `WaitTrig` 状态——`TrigNow_2='1'` 时才从 `WaitTrig` 迁入 `PostTrig`。本讲讲的就是这个 `TrigNow_2` 从何而来。
- **两进程法与流水线**（u3-l3）：核心用组合进程 `p_comb` 计算下一拍 `r_next`、时序进程 `p_seq` 搬入。带数字后缀的信号名（如 `Trig_In(0/1)`、`ExtTrigPending_2`、`In_Vld(1)`）表示该信号所处的流水级。
- **寄存器地址地图**（u2-l2）：寄存器常量定义在 `data_rec_register_pkg` 中，封装层把 AXI 写入解码成各个配置端口。

几个本讲会用到的术语，先统一一下：

- **pending（挂起）**：一个触发请求被记录下来，但还没被消费（还没真正引发 `WaitTrig→PostTrig` 的迁移）。有的触发源会"记住"这个请求（锁存/sticky），有的不会。
- **掩码（mask）**：用一个多比特向量的某些位，按位 `and` 来决定是否放行某类信号。这里 `TrigEna` 就是一个三位掩码。
- **门控（gating）**：用一个条件信号 `and` 另一个信号，限制后者只能在条件成立时生效。这里 `r.In_Vld(1)` 就是门控条件。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [hdl/data_rec.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd) | 核心记录器 RTL | 触发源检测、pending 锁存、`TrigNow_2` 合成 |
| [hdl/data_rec_register_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd) | 寄存器地址与字段常量 | `TrigEna` 地址与三位索引常量 |
| [hdl/data_rec_vivado_wrp.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd) | Vivado 封装层 | `TrigEna`/`SwTrig`/`EnableExtTrig` 如何从 AXI 解码到核心端口 |
| [testbench/top_tb/top_tb_case3_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case3_pkg.vhd) | 软件触发测试用例 | 用真实断言佐证掩码与 sticky 行为 |

---

## 4. 核心概念与源码讲解

### 4.1 TrigEna 端口与三位触发源掩码

#### 4.1.1 概念说明

记录器在 `WaitTrig` 状态等待"那个时刻"——触发事件。但触发事件可能来自三种完全不同的源头：

1. **外部触发（External）**：FPGA 外部或其它逻辑送进来的硬件信号 `Trig_In`，比如一个探测器的脉冲、一个同步命令。这是"硬件告诉记录器：现在"。
2. **软件触发（Software）**：CPU 通过 AXI 写一个寄存器位 `SwTrig` 来人工触发。这是"软件告诉记录器：现在"。
3. **自触发（Self-trigger）**：记录器自己盯着数据，当某个通道的样本落入设定的范围（进入或离开）时自己触发。这是"数据自己告诉记录器：现在"。

并不是每次录制都需要同时开启这三种源。比如做示波器抓外部事件，你可能只想要外部触发，不希望数据偶尔越界就乱触发；做 free-running 连续录制，你可能只要软件触发循环。于是记录器用一个 **3 位掩码 `TrigEna`**，每一位对应一类触发源，置 1 才放行该类触发。

这三类源在物理上互不相关（一个来自引脚、一个来自总线、一个来自数据通路），所以它们被**独立检测、再 OR 合成**，最后由掩码挑选——这就是本单元后四篇要展开的"触发生态"。

#### 4.1.2 核心流程

掩码的作用可以用一句话概括：

```
某类触发源的有效请求  and  TrigEna 中对应的使能位  →  放行该类触发
```

三位掩码的位定义如下（**这是本讲最需要记住的一张表**）：

| 比特位 | 常量名 | 对应触发源 | 该位 = 1 的含义 |
| --- | --- | --- | --- |
| bit 0 | `Reg_TrigEna_ExtIdx_c` | 外部触发 | 放行外部触发 |
| bit 1 | `Reg_TrigEna_SwIdx_c` | 软件触发 | 放行软件触发 |
| bit 2 | `Reg_TrigEna_SelfIdx_c` | 自触发 | 放行自触发 |

因此写入 `Reg_TrigEna_Addr_c` 的数值就是 `2**Ext + 2**Sw + 2**Self` 的组合。例如：

- 只开外部：`2**0 = 1`（`0b001`）
- 只开软件：`2**1 = 2`（`0b010`）
- 只开自触发：`2**2 = 4`（`0b100`）
- 外部 + 自触发：`1 + 4 = 5`（`0b101`）
- 三类全开：`1 + 2 + 4 = 7`（`0b111`）
- 全关：`0`——记录器会一直卡在 `WaitTrig`，永远不会触发。

#### 4.1.3 源码精读

掩码地址与三位索引常量定义在寄存器包里：

- `Reg_TrigEna_Addr_c := 16#0028#`，以及 Ext=0、Sw=1、Self=2 三个位索引：[hdl/data_rec_register_pkg.vhd:50-53](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L50-L53)。注释里 `ExtIdx`/`SwIdx`/`SelfIdx` 即"外部/软件/自触发"的索引。

核心记录器的 `TrigEna` 端口是 3 位宽，且配套有外部逐路使能 `EnableExtTrig` 和软件触发 `SwTrig`：

- `TrigEna : in std_logic_vector(2 downto 0)`，注释 "Trigger source enable"：[hdl/data_rec.vhd:66](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L66)
- `EnableExtTrig`（外部触发的**逐路**使能，与 `TrigEna` 的全局 Ext 位是两层不同的使能，u4-l2 详讲）：[hdl/data_rec.vhd:67](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L67)
- `SwTrig`：[hdl/data_rec.vhd:61](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L61)

封装层把 AXI 写入的 `Reg_TrigEna_Addr_c` 低 3 位解码成核心的 `TrigEna` 端口（注意 `reg_trigena'left downto 0` 取的是与端口等宽的低段）：

- [hdl/data_rec_vivado_wrp.vhd:326](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L326)：`reg_trigena <= reg_wdata(...)(reg_trigena'left downto 0);`

测试用例 case3（软件触发）真实地写入了"只开软件"的掩码值，可以直接佐证上面的数值表：

- [testbench/top_tb/top_tb_case3_pkg.vhd:59](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case3_pkg.vhd#L59)：`axi_single_write(Reg_TrigEna_Addr_c, 1*2**Reg_TrigEna_SwIdx_c, ...)`——即写入 `1*2**1 = 2`，只放行软件触发。

#### 4.1.4 代码实践

**实践目标**：亲手把掩码数值表与源码常量对应起来。

**操作步骤**：

1. 打开 `hdl/data_rec_register_pkg.vhd` 第 50–53 行，确认三个 `Reg_TrigEna_*Idx_c` 的整数值。
2. 打开 `testbench/top_tb/top_tb_case3_pkg.vhd` 第 59 行，确认测试平台用 `1*2**Reg_TrigEna_SwIdx_c` 这个表达式而不是直接写 `2`——体会作者用常量名而非魔术数字的可读性。
3. 自己列一张表：把 `0b000` 到 `0b111` 八种组合的十进制值写出来，并标注每种组合放行哪些源。

**需要观察的现象 / 预期结果**：你列出的表应该与 4.1.2 节给出的表一致；其中 `0b010`（=2）就是 case3 用的软件触发专用配置。

**说明**：本实践是纯源码阅读与手算，无需运行仿真即可确认。

#### 4.1.5 小练习与答案

**练习 1**：如果应用只想要"外部触发 + 软件触发"双保险，应写入 `Reg_TrigEna_Addr_c` 的值是多少？

**答案**：`2**0 + 2**1 = 1 + 2 = 3`（`0b011`）。

**练习 2**：为什么把 `TrigEna` 写成 0 会导致记录器"Arm 之后再也不 Done"？

**答案**：三类触发源全被掩码屏蔽，`TrigNow_2` 恒为 0，状态机永远停在 `WaitTrig_s`，无法迁入 `PostTrig`，自然到不了 `Done`。case3 第 133 行就是把 `TrigEna` 写 0 后，即使发软件触发也保持在 `WaitTrig` 状态（见 [top_tb_case3_pkg.vhd:131-141](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case3_pkg.vhd#L131-L141)）。

---

### 4.2 三类触发源的电学性质与 pending 处理

#### 4.2.1 概念说明

把三类触发源"检测出来"的方式并不相同，这直接决定了它们各自的 **pending（挂起）行为**。这是本讲最微妙、也是初学者最容易困惑的一点，先建立直觉：

- **外部触发**——`Trig_In` 是异步于数据的硬件脉冲。记录器对它做**上升沿检测**：只在信号"从 0 跳到 1"的那一拍识别为一次有效触发请求。一旦识别到，这个请求被**锁存**进 `ExtTrigPending_2`，哪怕后面信号又落回 0，请求也一直在，直到被消费或被清除。所以外部触发是"**边沿检测 + 锁存 pending**"。
- **软件触发**——`SwTrig` 是一个电平信号（软件写 1 就持续为 1，写 0 才落回 0）。记录器只要看到 `SwTrig='1'` 就把 `SwTrigPending_2` 置 1，而且**不再自动清零**，一直粘住。这种"写一次就一直有效"的行为叫 **sticky（粘滞）pending**。所以软件触发是"**电平置位 + sticky pending**"。
- **自触发**——它是**数据本身**驱动的：当某个通道的样本"进入"或"离开"设定范围时触发。它每一拍都根据当前样本与上一拍样本**重新计算**，是一个组合变量 `StTrig_2`，**不锁存、不需要清除**——只在数据跨越边界的那一拍为 1，其它拍为 0。所以自触发是"**边沿检测 + 非锁存（瞬时电平）**"。

为什么这个区别重要？因为前两类（外部、软件）的 pending 会被"记住"，触发请求可以在数据流间隙（`In_Vld=0` 的时刻）先存起来，等到下一个有效样本再兑现；而自触发只在有数据跨越边界的那一拍存在，天然与数据同步。

#### 4.2.2 核心流程

三类源的检测与 pending 处理对照（伪代码）：

```
# 外部触发（Stage2 段）
if 状态 == PreTrig_s:          # 每次 (re-)Arm 进入 PreTrig 时，清掉上轮残留
    ExtTrigPending_2 := 0
for 每一路 i in 0..TrigInputs_g-1:
    if Trig_In(0)(i)==1 and Trig_In(1)(i)==0 and EnableExtTrig(i)==1:  # 上升沿 + 逐路使能
        ExtTrigPending_2 := 1                       # 一旦置 1 就锁存（v:=r 默认保持）

# 软件触发（Stage2 段）
if SwTrig == '1':
    SwTrigPending_2 := 1                            # 置位
elif 状态 == PreTrig_s:
    SwTrigPending_2 := 0                            # 只在进入 PreTrig 时清除 → sticky

# 自触发（Stage2 段，每拍重算的变量，无锁存）
StEnter_2 := StInRange_1  and not StInRangeLast_1   # 进入范围（上升沿）
StExit_2  := StInRangeLast_1 and not StInRange_1    # 离开范围（下降沿）
StTrig_2 := '0'
if (SelfTrigOnExit  and (StExit_2  & SelfTrigChEna 非零)): StTrig_2 := '1'
if (SelfTrigOnEnter and (StEnter_2 & SelfTrigChEna 非零)): StTrig_2 := '1'
```

两类 pending 的清除时机相同：**进入 `PreTrig_s` 状态时**（即每次 Arm 重新开始录制时清掉上一轮的残留），此外 **`MinRecPeriod` 倒计时期间**也会被清掉（见 4.3 节与 u4-l5）。

#### 4.2.3 源码精读

外部触发的边沿检测与锁存 pending：

- [hdl/data_rec.vhd:207-215](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L207-L215)：进入 `PreTrig_s` 时清 `ExtTrigPending_2`；随后遍历所有外部触发路，`r.Trig_In(0)(i)='1' and r.Trig_In(1)(i)='0'` 正是"当前拍为 1、上一拍为 0"的上升沿，再 `and EnableExtTrig(i)='1'` 做逐路使能；任一路满足就把共享的 `ExtTrigPending_2` 置 1。由于 `p_comb` 开头有 `v := r`（默认保持），置 1 后即使下拍无新边沿也会保持——这就是"锁存 pending"。

软件触发的 sticky pending：

- [hdl/data_rec.vhd:217-222](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L217-L222)：`SwTrig='1'` 时置 1；`elsif r.State_2 = PreTrig_s` 时清 0。注意清除**只发生在 PreTrig**——这意味着写一次 `SwTrig=1` 之后，pending 会一直粘住，直到下一次 Arm。这正是 sticky。

自触发的非锁存瞬时变量：

- [hdl/data_rec.vhd:196-205](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L196-L205)：`StTrig_2` 是 `p_comb` 内部的 `variable`（见 [data_rec.vhd:149](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L149) 声明），每拍开头先 `:= '0'` 再按条件置 1，不存在 record 字段里，因此**无记忆**。范围判定本身在 Stage1 完成（[data_rec.vhd:173-188](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L173-L188)），只在 `r.In_Vld(0)='1'`（有有效样本）时更新。

两类 pending 在 record 里的存储字段：

- [hdl/data_rec.vhd:118-119](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L118-L119)：`SwTrigPending_2` 与 `ExtTrigPending_2` 都是 `std_logic` 寄存器（自触发没有对应字段，因为它不锁存）。

封装层 `RegRstVal_c` 让外部触发的逐路使能 `EnableExtTrig` 上电默认全 1（即默认放行所有外部触发路）——这与本讲的 `TrigEna` 全局 Ext 位是两层独立的使能：

- [hdl/data_rec_vivado_wrp.vhd:222-223](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L222-L223)：复位默认值只在 `Reg_EnableExtTrig_Addr_c/4` 这个字填全 1，其余全 0。注意 `Reg_TrigEna` 本身复位是 0（三类源默认全关），所以上电后即便 `EnableExtTrig` 全 1，外部触发也不会生效，除非软件再把 `TrigEna` 的 Ext 位置 1。

#### 4.2.4 代码实践

**实践目标**：用 case3 的真实断言，验证 sticky pending 的存在。

**操作步骤**：

1. 打开 [top_tb_case3_pkg.vhd:150-159](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case3_pkg.vhd#L150-L159)，这段注释写着 "Check SW trigger set before arming (triggers since SW trigger is sticky)"。
2. 注意它**先**写 `Reg_SwTrig_Addr_c`（第 153 行），**再**写 `Reg_Cfg_Addr_c` 的 Arm 位（第 154 行），顺序与常规用例相反。
3. 跟踪其结果：第 158 行期望状态直接是 `Reg_Stat_StateDone_c`——也就是说，先置软件触发再 Arm，记录器一进入 `WaitTrig` 就立刻被兑现的 sticky pending 触发了。

**需要观察的现象 / 预期结果**：因为 `SwTrigPending_2` 在写 `SwTrig=1` 时就被置 1 并粘住，Arm 后进入 `WaitTrig` 时它仍为 1（还没经历 PreTrig 清除的时机在此之后），于是触发立即成立。这就是 sticky 的威力——它让"先请求、后准备"也能成立，是 free-running 模式的基础（u4-l3 详讲）。

**说明**：若要本地验证，可按 u1-l3 的 PsiSim 流程单独跑 `top_tb`（软件触发用例），观察 Transcript 中 `"Done Status 6"` 断言通过。无法本地运行时，上述代码行与注释本身已构成证据，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：自触发为什么**不需要**像外部、软件那样有一个 pending 寄存器？

**答案**：自触发由数据驱动，`StInRange_1` 只在有有效样本（`In_Vld(0)='1'`）时更新，跨越边界的那一拍 `StTrig_2` 才为 1；数据本身已经提供了"节拍"，不需要在数据间隙记忆请求。而外部/软件触发可能在没有有效样本的时刻到来，必须先存起来等下一个样本兑现，所以需要 pending。

**练习 2**：`ExtTrigPending_2` 一旦被上升沿置 1，会不会在 `WaitTrig` 期间因为外部信号落回 0 而自动清零？

**答案**：不会。置 1 后，`v := r` 使其在没有新边沿时保持为 1，只有两种途径清零：进入 `PreTrig_s`（re-Arm），或处于 `MinRecPeriod` 抑制期（见 [data_rec.vhd:236-238](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L236-L238)）。所以在单次 `WaitTrig` 期间，外部触发请求是"一次锁定、必被兑现或被显式清除"。

---

### 4.3 TrigNow_2 合成与 In_Vld 门控

#### 4.3.1 概念说明

把 4.1（掩码放行）与 4.2（三类源的 pending）合到一起，就得到记录器真正使用的、单拍的触发信号 `TrigNow_2`。它是状态机 `WaitTrig → PostTrig` 的唯一驱动（见 u3-l2）。

`TrigNow_2` 的设计有两个要点：

1. **OR 合成 + 掩码放行**：三类源各 `and` 自己的 `TrigEna` 位，再 OR 起来。哪一类先来、或同时来，都能触发。
2. **`In_Vld(1)` 总门控**：合成结果最后再 `and r.In_Vld(1)`。也就是说，**只有在存在有效数据样本的拍上，触发才真正生效**。

第二点是初学者最想问的：触发是触发，为什么还要看数据有效？答案是：记录器以"样本"为最小计时单位——每一次状态推进、每一次计数器递增、每一次存储写入，都只发生在 `In_Vld` 有效时（参见 u3-l3、u3-l4）。如果触发在一个**没有数据**的拍上兑现，那个时刻根本没有样本可记，触发点与第一个被记样本就会错位，前/后触发的窗口全部错乱。因此任何触发请求——哪怕已经锁存为 pending——都必须等到"下一个有效样本"那一拍才能兑现。`In_Vld(1)` 就是这个"等到有数据"的闸门。

#### 4.3.2 核心流程

`TrigNow_2` 的合成公式（与源码严格对应）：

\[
\text{TrigNow\_2} = \big(\;(\text{StTrig\_2}\cdot\text{TrigEna}_\text{Self}) \;\lor\; (\text{ExtTrigPending\_2}\cdot\text{TrigEna}_\text{Ext}) \;\lor\; (\text{SwTrigPending\_2}\cdot\text{TrigEna}_\text{Sw})\;\big) \;\land\; \text{In\_Vld}(1)
\]

合成后 `TrigNow_2` 进入状态机：

```
when WaitTrig_s =>
    if TrigNow_2 == '1':
        State_2   := PostTrig_s
        Trigger_2 := '1'          # 单拍脉冲，对外走到 Trig_Out（v2.4）
```

此外还有一个"时间过滤"——`MinRecPeriod`（两次录制之间的最小间隔）会在其倒计时期间强行把 `TrigNow_2` 拉回 0，并清掉两类 pending（见 4.3.3 与 u4-l5）。

#### 4.3.3 源码精读

合成公式本体（本讲最关键的一行）：

- [hdl/data_rec.vhd:224-228](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L224-L228)：三个括号分别对应自触发（`StTrig_2 and TrigEna(Reg_TrigEna_SelfIdx_c)`）、外部（`r.ExtTrigPending_2 and TrigEna(Reg_TrigEna_ExtIdx_c)`，行内注释 "Edge sensitive trigger"）、软件（`r.SwTrigPending_2 and TrigEna(Reg_TrigEna_SwIdx_c)`），三者 OR，最后 `and r.In_Vld(1)`。注意自触发用的是当拍变量 `StTrig_2`，外部/软件用的是锁存的 pending——这正呼应 4.2 节的性质差异。

`MinRecPeriod` 对 `TrigNow_2` 的抑制与 pending 清除：

- [hdl/data_rec.vhd:230-241](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L230-L241)：倒计时未到 0 时 `TrigNow_2 := '0'`，同时清掉 `ExtTrigPending_2` 与 `SwTrigPending_2`（注释 "clear pending triggers (they were too early)"）——即"来得太早"的触发请求被丢弃而非延后。

状态机消费 `TrigNow_2`：

- [hdl/data_rec.vhd:259-263](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L259-L263)：`WaitTrig_s` 下 `TrigNow_2='1'` 迁入 `PostTrig_s` 并打出一拍 `Trigger_2`。

`Trigger_2` 经端口转发出去（v2.4 新增 `Trig_Out`）：

- [hdl/data_rec.vhd:393](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L393)：`Trig_Out <= r.Trigger_2;`——转发的是**经裁决后真正兑现**的触发（即通过了掩码、通过了 `In_Vld`、通过了 `MinRecPeriod` 的那个），而非任何原始 `Trig_In`。

封装层把 `TrigEna`/`SwTrig`/`EnableExtTrig` 三组配置都经 `status_cc` 从 AXI 时钟域同步到数据时钟域（因为它们是多比特电平，走 status 而非 pulse；`SwTrig` 虽然在核心里被当电平处理，但跨域时按 status 同步）：

- [hdl/data_rec_vivado_wrp.vhd:381-382](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L381-L382)：`TrigEna` 与 `SwTrig` 一起拼进 `CcSFromAxIn` 送 `psi_common_status_cc`。

#### 4.3.4 代码实践

**实践目标**：解释 `TrigNow_2` 为什么必须 `and r.In_Vld(1)`，并验证三类源中谁能"提前到达"。

**操作步骤**：

1. 在 [hdl/data_rec.vhd:224-228](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L224-L228) 找到合成行，确认末尾的 `and r.In_Vld(1)`。
2. 回顾 [data_rec.vhd:259-263](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L259-L263)：`WaitTrig→PostTrig` 与 `Trigger_2` 都依赖 `TrigNow_2`，而 `Trigger_2` 决定 `FirstSpl_3`（触发时刻环形缓冲起点，见 u3-l5）。
3. 做一个思想实验：假设外部触发脉冲恰好在 `In_Vld=0`（数据断流）的一拍到来，问：(a) `ExtTrigPending_2` 会不会被置位？(b) `TrigNow_2` 会不会在这一拍为 1？(c) 什么时候才真正触发？

**需要观察的现象 / 预期结果**：
- (a) 会——边沿检测与 `EnableExtTrig` 都满足，pending 被锁存。
- (b) 不会——因为 `r.In_Vld(1)=0`，合成结果被门控为 0。
- (c) 要等到下一个 `In_Vld=1` 的有效样本那一拍，pending 仍在，`TrigNow_2` 才为 1，触发在"下一个有效样本"兑现。

**这正是 `and r.In_Vld(1)` 的全部意义**：保证触发点严格落在某个被记录的有效样本上，使前/后触发窗口与实际写入存储的样本一一对齐。自触发由于只在有效样本拍上产生 `StTrig_2`，天然满足这一约束；外部/软件则靠 pending + `In_Vld` 门控补齐。

**说明**：思想实验无需运行即可推导；如要本地观测，可在 `data_rec.vhd` 临时给 `TrigNow_2` 加一条仿真打印（仅调试，勿提交），在数据断流期送外部脉冲，确认 `TrigNow_2` 不跳变、`ExtTrigPending_2` 锁存。标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `TrigNow_2` 用 `r.In_Vld(1)`（Stage1 对齐）而不是 `r.In_Vld(2)`（Stage2 对齐）？

**答案**：状态机 `r.State_2` 虽以"2"命名，但触发裁决、计数器推进这一组 Stage2 逻辑实际读取的是 Stage1 对齐的有效信号 `r.In_Vld(1)`（与 `AdrCnt_2`、`SplCnt_2` 递增所用的 `r.In_Vld(1)` 一致，见 u3-l4）。这样 `TrigNow_2` 与计数器在同一拍兑现，触发样本的地址/序号才能正确记账。统一用 `In_Vld(1)` 是为了让"触发判断"与"样本记账"对齐到同一拍。

**练习 2**：如果同时有外部触发 pending 和软件触发 pending，且 `TrigEna` 两位都开着，会触发几次？

**答案**：只触发一次。二者 OR 后只产生一个 `TrigNow_2='1'`，状态机一次迁入 `PostTrig`；进入下一轮 Arm（PreTrig）时两类 pending 才被清零。所以多个源同时有效时，记录器只录一段，触发计数器 `TrigCnt` 只加 1。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个综合任务（对应本讲规格里的实践任务）。

**任务**：你是固件工程师，需要为一个新应用配置 `data_rec` 的触发源。请：

1. 计算三种配置下应写入 `Reg_TrigEna_Addr_c`（地址 `0x0028`）的数值：
   - (A) 只允许软件触发；
   - (B) 只允许自触发；
   - (C) 同时允许外部 + 自触发。
2. 解释 `TrigNow_2` 合成式末尾为何还要 `and r.In_Vld(1)`，并举一个"没有这个 AND 会出错"的具体场景。
3. （进阶）对照 case3 用例，说明"先写 SwTrig 再 Arm 也能立即触发"这一现象依赖 sticky pending 的哪一行代码，并回答：触发兑现后，软件是否**必须**把 `SwTrig` 写回 0？为什么？

**参考答案**：

1. 利用 [register_pkg.vhd:50-53](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L50-L53) 的索引常量：
   - (A) 软件：`1*2**Reg_TrigEna_SwIdx_c = 1*2**1 = 2`（`0b010`）。case3 第 59 行正是此值。
   - (B) 自触发：`1*2**Reg_TrigEna_SelfIdx_c = 1*2**2 = 4`（`0b100`）。
   - (C) 外部 + 自触发：`2**Reg_TrigEna_ExtIdx_c + 2**Reg_TrigEna_SelfIdx_c = 1 + 4 = 5`（`0b101`）。

2. `and r.In_Vld(1)` 保证触发只在"有有效样本"的拍上兑现（[data_rec.vhd:224-228](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L224-L228)）。没有它的出错场景：外部触发脉冲在数据断流（`In_Vld=0`）期到来，`Trigger_2` 立即拉高，`FirstSpl_3` 会在一个**没有写入样本**的地址上计算，前/后触发窗口与真正落盘的样本错位，读出的波形会整体偏移、甚至包含空洞。

3. 该现象依赖 [data_rec.vhd:217-222](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L217-L222) 的 sticky：`SwTrig='1'` 置位 `SwTrigPending_2`，清除只发生在 `PreTrig_s`，所以 Arm 之前置的请求会一直保留到进入 `WaitTrig` 兑现。**触发后必须把 `SwTrig` 写回 0**——否则 `SwTrigPending_2` 在下一轮 PreTrig 才被清，而一旦再次 Arm，又会因为 sticky 立即触发（这正是 free-running 的原理，但若不希望连续触发，就必须写回 0）。case3 在每次触发后都紧接着写 `Reg_SwTrig_Addr_c, 0`（如 [top_tb_case3_pkg.vhd:69](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case3_pkg.vhd#L69)、[第 82 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case3_pkg.vhd#L82)、[第 168 行](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case3_pkg.vhd#L168)），正是出于此。

## 6. 本讲小结

- 记录器有**三类触发源**：外部 `Trig_In`（硬件）、软件 `SwTrig`（AXI 写）、自触发（数据落范围）。
- `TrigEna` 是 **3 位掩码**：bit0=Ext、bit1=Sw、bit2=Self（常量见 [register_pkg.vhd:50-53](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L50-L53)），写入值 = 各使能位 `2**Idx` 之和。
- 三类源的电学性质不同：外部是**边沿检测 + 锁存 pending**，软件是**电平置位 + sticky pending**，自触发是**边沿检测 + 非锁存瞬时变量**。
- 两类 pending 都在进入 `PreTrig_s` 时清除，并在 `MinRecPeriod` 抑制期被丢弃。
- `TrigNow_2` = （三类源各 `and` 自己掩码位后 OR）`and r.In_Vld(1)`（[data_rec.vhd:224-228](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L224-L228)）——`In_Vld(1)` 门控保证触发点严格落在有效样本上。
- 经裁决兑现的触发由 `Trigger_2` 经 `Trig_Out` 转发（v2.4），转发的是"真正使用"的触发而非任何原始输入。

## 7. 下一步学习建议

本讲建立了"三类源 + 掩码 + 合成"的总框架，后续四篇各自深入一种触发机制：

- **u4-l2 外部触发**：展开 `Trig_In` 的两拍流水、上升沿检测、`EnableExtTrig` 逐路使能与多路 OR，以及进入 PreTrig 清 pending 的细节。
- **u4-l3 软件触发**：聚焦 sticky pending 如何实现 free-running 自循环模式。
- **u4-l4 自触发**：深入 `StInRange` 的 unsigned/signed 双重范围判定、`StEnter/StExit` 边沿、通道使能与进入/退出方向选择。
- **u4-l5 最小录制间隔与 Trig_Out**：展开本讲提到的 `MinRecPeriod` 抑制、`TrigCnt`/`DoneTime` 计数器，以及 `Trig_Out` 转发端口。

建议阅读顺序按 u4-l2 → u4-l3 → u4-l4 → u4-l5，每篇都回到本讲的合成公式与 pending 表对照理解。
