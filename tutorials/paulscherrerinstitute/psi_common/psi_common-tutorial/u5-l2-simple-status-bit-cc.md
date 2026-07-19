# 数据/状态/位跨越 simple_cc / status_cc / bit_cc

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `psi_common_simple_cc`、`psi_common_status_cc`、`psi_common_bit_cc` 三个组件各自解决什么问题、内部如何工作。
- 解释 `simple_cc` 为什么「借用」`pulse_cc` 来传 valid，以及它如何让多 bit 数据安全地跨越异步时钟域。
- 解释 `status_cc` 如何在 `simple_cc` 之上「自动」生成传输节拍，从而免掉用户对采样点的管理。
- 解释 `bit_cc` 为什么只能用于「相互独立」的单 bit 信号，而不能用于多 bit 总线。
- 面对一个具体的跨时钟域需求，能正确在「脉冲 / 数据 / 状态 / 位」四类跨越之间做选型。

本讲是 CDC 单元的第二篇，承接 [u5-l1 pulse_cc](u5-l1-pulse-cc.md)：`pulse_cc` 只能传「事件脉冲」，本讲讲的三件套都是在它之上、围绕「数据怎么跟着过去」做扩展。

## 2. 前置知识

在进入源码前，先用三段话把基础概念讲清楚。如果你已经熟悉，可以跳到第 3 节。

**多 bit 跨时钟域的「撕裂」危险。** 跨时钟域（CDC, Clock Domain Crossing）之所以难，不在于单 bit——单 bit 最坏只是采「旧值」或「新值」，加两级触发器同步器就能解决。真正危险的是多 bit 总线：当多个 bit 在同一个源时钟沿同时翻转，由于布线延迟不同，目的时钟可能在「某些 bit 已翻、某些 bit 还没翻」的中间态上采样，得到一个既不是旧值也不是新值的「撕裂值（torn value）」。例如 4'b0111 → 4'b1000 的过程中，目的域可能瞬间采到 4'b1111。本讲的三个组件，本质上都在回答同一个问题：**怎样让多 bit 数据跨域时不被撕裂。**

**AXI-S 的 valid 握手（复习）。** 在 [u1-l4](u1-l4-coding-conventions-handshaking.md) 已经讲过：一次有效传输发生在 `Vld` 与 `Rdy` 同时为高的时钟沿。本讲的 `simple_cc` 只用 `Vld`、不用 `Rdy`（即不处理反压），所以**调用者必须自己保证不要喂得太快**。这一点决定了它的速率上限。

**pulse_cc 的「翻转-同步-异或」手法（复习）。** 在 [u5-l1](u5-l1-pulse-cc.md) 讲过：`pulse_cc` 把 A 域的一个单周期脉冲先转成「长期稳定的电平翻转（toggle）」，经 B 域多级同步器采样，再用相邻两级同步值的「异或」把翻转还原成 B 域的单周期脉冲。本讲 `simple_cc` 直接例化 `pulse_cc` 来搬运 valid 信号，所以这一手法是本讲的底层。

## 3. 本讲源码地图

| 文件 | 作用 |
|:--|:--|
| `hdl/psi_common_simple_cc.vhd` | 传「带 valid 的单个数据样本」，借用 `pulse_cc` 传 valid，A 域锁存数据供 B 域读取。本讲主角之一。 |
| `hdl/psi_common_status_cc.vhd` | 传「慢变的状态/配置值」，在 `simple_cc` 之上自动生成 valid 节拍，形成 A↔B 握手回环。本讲主角之二。 |
| `hdl/psi_common_bit_cc.vhd` | 传「多个相互独立的单 bit 信号」，每 bit 一组两级同步器，最简。本讲主角之三。 |
| `hdl/psi_common_pulse_cc.vhd` | 上一讲的主角，本讲被 `simple_cc` 例化为底层，复习其 valid 跨越能力。 |
| `testbench/psi_common_simple_cc_tb/...` | `simple_cc` 的自校验测试平台，已登记在 `sim/config.tcl`。 |
| `testbench/psi_common_status_cc_tb/...` | `status_cc` 的自校验测试平台，已登记在 `sim/config.tcl`。 |

> 小提示：`bit_cc` **没有专属测试平台**（仓库里不存在 `psi_common_bit_cc_tb`），因为它的行为就是「两级寄存器」，验证价值不高。

## 4. 核心概念与源码讲解

### 4.1 simple_cc：带 valid 的单样本数据跨越

#### 4.1.1 概念说明

`simple_cc` 解决的是：**我有一个会随事件出现的数据样本（比如一次 ADC 转换结果），需要从 A 时钟域搬到完全异步的 B 时钟域。** 它和 `pulse_cc` 的区别在于——`pulse_cc` 只告诉你「事件来了」，不带数据；`simple_cc` 既传「事件来了（valid）」，又把当时的数据一起带过去。

它的接口遵循 AXI-S 风格但**只用 valid、不用 ready**（不处理反压）。这一点很关键：因为没有 ready，源端无法知道宿端什么时候消化完，所以**必须由调用者保证数据速率足够低**。文档给出的硬约束是：**数据速率不得超过目的时钟频率的 1/4**。

#### 4.1.2 核心流程

`simple_cc` 的核心思想只有一句话：**让 valid 走 `pulse_cc` 跨域，让数据「原地锁存、保持稳定」，等 valid 到了 B 域再把数据读走。**

伪代码描述整体流程：

```
# A 域（源）
每个 a_clk 上升沿:
    若 a_vld_i == '1':
        DataLatchA <= a_dat_i      # 把数据锁存在 A 域，并保持

# valid 跨域（借用 pulse_cc）
a_vld_i --[翻转-同步-异或]--> VldBI   # B 域得到一个单周期脉冲

# B 域（目的）
每个 b_clk 上升沿:
    b_vld_o <= VldBI
    若 VldBI == '1':
        b_dat_o <= DataLatchA      # 此时数据早已稳定，多 bit 安全读取
```

为什么这样能避免「撕裂」？因为数据**不随 valid 一起跨域**，而是始终待在 A 域的 `DataLatchA` 里不动。valid 是单 bit，走 `pulse_cc` 没有撕裂问题。等 valid 经过两级同步器（至少 2 个 B 时钟周期）到达 B 域时，`DataLatchA` 里的数据早就稳定了好几个周期，B 域这时去读一个「长期不动」的多 bit 值，自然安全。

速率约束（≤ f_b/4）的来源：必须保证「下一次 valid 出现」之前，上一次的数据已经被 B 域读走、且读的时候数据还没被新值覆盖。

#### 4.1.3 源码精读

实体声明。注意 generic 名是 `width_g`，端口命名遵循库规范（`_i`/`_o` 后缀），valid 信号叫 `a_vld_i`/`b_vld_o`：

[hdl/psi_common_simple_cc.vhd:19-33](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_simple_cc.vhd#L19-L33) —— 声明 `width_g` 数据位宽、两个复位极性 generic，以及 A/B 两域的 clk/rst/dat 加上 valid 握手信号。

核心：例化一个 `num_pulses_g => 1` 的 `pulse_cc`，专门用来搬运 valid：

[hdl/psi_common_simple_cc.vhd:46-61](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_simple_cc.vhd#L46-L61) —— 把 `a_vld_i` 接到 pulse_cc 的 `a_dat_i(0)`，从 `b_dat_o(0)` 取出还原后的脉冲 `VldBI`。**复位的跨域也顺手由这个 pulse_cc 完成了**：`a_rst_o`/`b_rst_o` 直接取自 pulse_cc 的 `a_rst_o`/`b_rst_o`（第 62-63 行），所以本组件无需再写复位同步逻辑。

A 域数据锁存进程：

[hdl/psi_common_simple_cc.vhd:66-77](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_simple_cc.vhd#L66-L77) —— 只在 `a_vld_i='1'` 时更新 `DataLatchA`，否则保持上一次的值。这就是「数据原地稳定」的实现。

B 域接收进程：

[hdl/psi_common_simple_cc.vhd:80-93](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_simple_cc.vhd#L80-L93) —— 把 `VldBI` 寄存到 `b_vld_o`；当 `VldBI='1'` 时把 `DataLatchA` 读入 `b_dat_o`。注意 `b_dat_o` 只在 valid 那一拍更新，**valid 撤销后 `b_dat_o` 保持上一次的值**（这一点会被测试平台专门验证，见 4.1.4）。

> 读源码 vs 读文档：组件文档 `doc/files/psi_common_simple_cc.md` 把 generic 写成 `data_width_g`，但**真实源码用的是 `width_g`**。源码是权威，文档表格里的名字偶尔会过时，实例化时一律以 `.vhd` 为准。

#### 4.1.4 代码实践

**目标**：通过阅读自校验测试平台，理解 `simple_cc` 在 valid 撤销后「保持上次数据」的行为，并预测仿真结果。

**操作步骤**：

1. 打开 [testbench/psi_common_simple_cc_tb/psi_common_simple_cc_tb.vhd:160-174](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/../testbench/psi_common_simple_cc_tb/psi_common_simple_cc_tb.vhd#L160-L174)（注意：这条链接指向 testbench 目录）。
2. 关注第 163-170 行：A 域在 `a_dat_i=X"AB"` 上拉一拍 `a_vld_i`，然后等 B 域出现 `b_vld_o='1'`，断言 `b_dat_o=X"AB"`。
3. 关注第 171-174 行：再等 10 个 B 时钟周期（此时 valid 早已撤销），断言 `b_dat_o` **仍然是** `X"AB"`。

**需要观察的现象**：valid 是单周期脉冲；脉冲过后，`b_dat_o` 不归零、不清空，而是**锁存住最近一次传输的值**，直到下一次 valid 才更新。

**预期结果**：两条 `assert` 都通过（不打印 `###ERROR###`）。如果你想亲手验证，可按 [u1-l3](u1-l3-dependencies-and-simulation.md) 的方式跑 `sim/run.tcl`，该 TB 已在 [sim/config.tcl:237](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L237) 注册为 `create_tb_run "psi_common_simple_cc_tb"`。**实际仿真波形待本地验证。**

#### 4.1.5 小练习与答案

**练习 1**：如果把一个 50 MHz 的目的时钟换成 200 MHz，`simple_cc` 允许的最大数据速率会如何变化？

**答案**：最大数据速率正比于目的时钟频率（约束是 ≤ f_b/4）。f_b 从 50 MHz 升到 200 MHz，允许的数据速率也提升 4 倍（从约 12.5 MS/s 提升到 50 MS/s）。

**练习 2**：为什么 `simple_cc` 不提供 `rdy`（ready）反压？这会带来什么使用限制？

**答案**：因为它用 valid 脉冲 + 数据锁存的简单方案，A 域不知道 B 域何时消化完。没有 ready 就意味着调用者必须**自行限速**（≤ f_b/4），不能用反压来动态节流；对于接近上限的连续数据流，应改用异步 FIFO（见 u4-l2）。

---

### 4.2 status_cc：慢变状态/配置值的跨越

#### 4.2.1 概念说明

`status_cc` 解决的是另一类问题：**我有一个慢变的值（比如一个增益寄存器、一个 FIFO 电平、一个配置字），它没有明确的「采样时刻」，只要 B 域能拿到一个正确（不撕裂）的值就行，偶尔漏掉中间值无所谓。**

它和 `simple_cc` 的关键差异在于：`simple_cc` 由用户用 `a_vld_i` 显式声明「这是一次有效传输」；而 `status_cc` **连 valid 都不要用户管**——实体自己决定什么时候发起一次传输。用户只管把 `a_dat_i` 摆好，组件保证 B 域的 `b_dat_o` 始终是「最近传过去的值或即将传过去的值」，绝不会是撕裂的中间值。

文档给出的约束是：**数据变化速率不得超过较慢时钟的 1/10**；变化更快时，组件会**主动跳过某些中间值**，这对「状态」类信号是可接受的。

#### 4.2.2 核心流程

`status_cc` 在 `simple_cc` 之上加了一个**自动节拍发生器**，形成一个 A→B→A 的握手回环：

```
# A 域：自动生成 valid 脉冲 VldA
复位释放后，若检测到 B 域也出了复位:
    发第一个 VldA 脉冲            # 把当前 a_dat_i 传过去
每当检测到 B 域「确认收到上一个值」(RecToggle 翻转):
    发下一个 VldA 脉冲            # 把最新的 a_dat_i 再传一次

# 数据传输（直接复用 simple_cc）
VldA + a_dat_i --[simple_cc]--> b_dat_o

# B 域：收到一拍 valid 就翻转一次 RecToggle 作为回执
每当 VldB == '1':
    RecToggle <= not RecToggle

# 回执跨域回到 A，驱动下一次发送
RecToggle --[两级同步]--> A 域，用于「检测到变化」判断
```

这是一个经典的「请求-确认」握手套件：A 发数据时附带一次 toggle，B 收到后回一个 toggle，A 检测到回执变化再发下一个。节拍完全由两端时钟自动适配，**不需要知道彼此频率**，因此天然满足 `simple_cc` 的速率上限。

检测「回执变化」用了一个小技巧：把同步后的 `RecToggle` 存成两级 `RecToggleSync`，当**最高位和次高位不同**时，说明刚刚捕获到一次翻转边沿，于是触发下一次发送。

#### 4.2.3 源码精读

A 域的自动 valid 生成进程，是本组件的全部「新增逻辑」：

[hdl/psi_common_status_cc.vhd:63-88](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_status_cc.vhd#L63-L88) —— `VldA` 默认每拍为 `'0'`（第 73 行）；第 75-76 行把 B 域的复位与回执 `RecToggle` 同步到 A 域；第 78-81 行在「B 域已出复位且还没发过第一个脉冲」时发首个 `VldA`；第 83-85 行在「检测到回执翻转」时发后续 `VldA`。

B 域的回执生成进程，每次收到 valid 就翻转一次 toggle：

[hdl/psi_common_status_cc.vhd:91-102](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_status_cc.vhd#L91-L102) —— `if VldB = '1' then RecToggle <= not RecToggle`，这就是回执。

数据搬运直接例化 `simple_cc`，把自动生成的 `VldA` 接到 `a_vld_i`：

[hdl/psi_common_status_cc.vhd:105-122](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_status_cc.vhd#L105-L122) —— `width_g`、复位极性透传；`a_vld_i => VldA`，`b_vld_o => VldB`。`a_rst_o`/`b_rst_o` 仍由内部 `simple_cc`（进而 `pulse_cc`）的复位同步链提供（第 123-124 行）。

可见 `status_cc` 是典型的**组合复用**：数据通路用 `simple_cc`，自己只负责「什么时候发 valid」这一件事。本组件同样挂了 `ASYNC_REG`/`shreg_extract`/`syn_srlstyle` 综合属性（第 47-58 行），保证 `RecToggle`、`RecToggleSync`、`RstIntBSync` 这些跨域寄存器被综合成真实触发器。

#### 4.2.4 代码实践

**目标**：跟踪一次完整的「发数据 → 回执 → 再发」握手回环，并验证 B 域最终能追上 A 域的慢变值。

**操作步骤**：

1. 打开 [testbench/psi_common_status_cc_tb/psi_common_status_cc_tb.vhd:154-168](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/../testbench/psi_common_status_cc_tb/psi_common_status_cc_tb.vhd#L154-L168)。
2. 第 162-165 行：复位释放后，把 `a_dat_i` 设为 `X"AB"`，等 12 个「较慢时钟周期」(`SlowerClockPeriod_c`)，断言 `b_dat_o = X"AB"`。
3. 第 166-168 行：改为 `X"CD"`，再等 12 个慢周期，断言 `b_dat_o = X"CD"`。
4. 对照源码：这「12 个慢周期」远大于一次握手回环所需的时间，所以 B 域有足够时间完成「发→确认→发」至少一轮，把最新值搬到 B 域。

**需要观察的现象**：用户**全程没有提供任何 valid 信号**，只改 `a_dat_i`；组件自己完成了所有节拍。`b_dat_o` 最终等于最近设置的值。

**预期结果**：两条断言通过。注意 TB 用 `SlowerClockPeriod_c`（两端较慢者）作等待单位，且故意等较长时间，正对应「数据变化必须比慢时钟慢得多（≤1/10）」的约束。该 TB 已在 [sim/config.tcl:245](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L245) 注册。**实际仿真波形待本地验证。**

#### 4.2.5 小练习与答案

**练习 1**：如果 `a_dat_i` 变化速率远快于握手回环速度（比如每拍都变），`b_dat_o` 会怎样？

**答案**：B 域只能拿到「握手回环恰好采样到的那一次」的值，中间大量变化会被跳过。`b_dat_o` 仍是一个**合法（未撕裂）**的值，但不保证是「最新的」。这对状态量可接受，对逐样本数据流则不可接受（应改用 FIFO）。

**练习 2**：`status_cc` 为什么能「不知道两端频率」也能安全工作，而 `simple_cc` 却要写死 1/4 速率上限？

**答案**：`status_cc` 有 B→A 的回执握手——A 必须等 B 确认上一个值收到后才发下一个，发送节拍被回环自动限制在「安全速率」之内。`simple_cc` 没有回执，全靠调用者遵守静态速率上限。

---

### 4.3 bit_cc：相互独立的单 bit 信号跨越

#### 4.3.1 概念说明

`bit_cc` 是三个组件里最简单的一个：它就是给「若干个**相互独立**的单 bit 信号」每人配一组两级同步器。适用场景比如：几个互不相关的使能标志、几个独立的中断位、几根控制线。

为什么强调「相互独立」？因为**每个 bit 各自走自己的两级同步器，彼此之间没有任何对齐机制**——第 0 bit 可能在这一拍到位，第 1 bit 可能在下一拍才到位（bit skew）。如果这些 bit 是「同一个多 bit 值的不同位」（比如一个计数器、一个地址），这种错位就会产生撕裂值。所以 `bit_cc` 的契约是：**你扔进来的每一个 bit 都必须能独立解释，不能把一个值拆成多 bit 喂进来。**

#### 4.3.2 核心流程

逻辑非常直白：

```
每个 clk_i（目的时钟）上升沿:
    Reg0 <= dat_i      # 第一级同步
    Reg1 <= Reg0       # 第二级同步
dat_o <= Reg1          # 输出取第二级
```

每个 bit 独立走一遍上述两级寄存器。源端只负责把 `dat_i` 摆成「无毛刺」的寄存器输出（标准 CDC 要求），目的端用一个时钟域、两级触发器采样。

注意它**只有一个时钟 `clk_i`（目的时钟）**，没有复位端口——寄存器靠声明时的初值 `(others => '0')` 初始化（依赖 FPGA 触发器上电初值或仿真器信号初值）。

#### 4.3.3 源码精读

实体声明，极简：

[hdl/psi_common_bit_cc.vhd:19-24](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_bit_cc.vhd#L19-L24) —— 只有 `dat_i`/`clk_i`/`dat_o` 三类端口，generic `width_g` 控制并行同步多少个 bit。没有复位、没有 valid。

内部寄存器与综合属性：

[hdl/psi_common_bit_cc.vhd:28-41](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_bit_cc.vhd#L28-L41) —— `Reg0`/`Reg1` 两级；`ASYNC_REG=TRUE` 告知综合工具这两级是跨域同步器（Xilinx 工具会据此加严时序、防止被优化进 SRL/移位寄存器 LUT）；`shreg_extract=no` 和 `syn_srlstyle=registers`（Synopsys）同理，强制落地为真实触发器。这些属性和 `pulse_cc` 的同步器完全一致（见 u5-l1）。

同步进程：

[hdl/psi_common_bit_cc.vhd:45-52](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_bit_cc.vhd#L45-L52) —— 标准 `Reg0 <= dat_i; Reg1 <= Reg0;` 输出取 `Reg1`。没有任何额外逻辑。

> 文档 `doc/files/psi_common_bit_cc.md` 把 generic 写成 `num_bits_g`，源码同样是 `width_g`——又一个「文档名过期、源码为准」的例子。

#### 4.3.4 代码实践

**目标**：通过思想实验（源码阅读型实践）理解「为什么 `bit_cc` 不能用于多 bit 总线」。

**操作步骤**：

1. 假设你用一个 4 bit 计数器 `a_dat_i`，值从 `4'b0111` 加到 `4'b1000`，跨一个异步时钟域接到 `bit_cc` 的 `dat_i`。
2. 对照源码第 45-52 行：4 个 bit 各自独立走 `Reg0`/`Reg1`，**互不等待**。
3. 想象目的时钟正好采样在「最高位已翻、低三位还没翻」的瞬间。

**需要观察的现象**：由于各 bit 的布线/翻转时刻不同，目的域可能瞬间采到 `4'b1111`（最高位新值 1 + 低三位旧值 111）或 `4'b0000` 等中间态，而这些值在原计数序列里根本不存在。

**预期结果**：出现「撕裂值」。结论：**`bit_cc` 严禁用于多 bit 相关总线**；这种场景应改用 `simple_cc`（数据锁存 + valid 单 bit 跨域）或 `status_cc`，让多 bit 数据在「保持稳定」的前提下被读走。本实践为推理型，无需运行仿真；若要观察，可自建一个最小 TB（库未提供 `bit_cc` 的 TB）。

#### 4.3.5 小练习与答案

**练习 1**：`bit_cc` 为什么不给复位端口？不上电时输出是什么？

**答案**：靠寄存器声明初值 `(others => '0')` 初始化（FPGA 触发器上电初值或仿真初值），所以省掉了复位端口与复位同步逻辑；上电后输出先为 0，随后跟随源端稳定值。若你的应用要求确定性的同步复位，应在源端保证 `dat_i` 复位为已知值。

**练习 2**：5 个独立的中断标志要从 200 MHz 域跨到 100 MHz 域，用 `bit_cc(width_g=>5)` 合适吗？

**答案**：合适。各中断相互独立，bit 间错位不影响语义；每个中断只是「有/无」事件，两级同步器足够。这正是 `bit_cc` 的典型用法。

---

### 4.4 三者选型：脉冲 / 数据 / 状态 / 位

#### 4.4.1 概念说明

到此你已经认识了 CDC 单元里四个「层级递进」的组件。它们各自适用于不同形态的信号，选错了要么浪费资源、要么出功能性 bug。本节给一张选型表，把上一讲的 `pulse_cc` 也一并纳入对比。

#### 4.4.2 核心流程（选型决策树）

按「要跨的是什么」走一遍：

```
要跨的信号是……
├─ 纯事件/脉冲（只关心「发生了」，无数据）        → pulse_cc        (u5-l1)
├─ 单个/多个相互独立的 1 bit 标志                  → bit_cc          (本讲 4.3)
├─ 带明确 valid 的逐样本数据（用户管节拍）          → simple_cc       (本讲 4.1)
└─ 慢变状态/配置值（不想管节拍，允许跳值）          → status_cc       (本讲 4.2)
   ※ 连续高速数据流（需要反压）                     → async_fifo      (u4-l2)
```

对比表：

| 组件 | 信号形态 | 是否带数据 | 谁管节拍 | 反压(rdy) | 典型场景 |
|:--|:--|:--|:--|:--|:--|
| `pulse_cc` | 单 bit 事件 | 否 | 用户(vld) | 否 | 触发、中断脉冲 |
| `bit_cc` | 多个独立单 bit | 是(各 bit 独立) | 源端持续驱动 | 否 | 独立标志、控制位 |
| `simple_cc` | 多 bit 样本 + valid | 是 | 用户(vld) | 否 | 偶发采样数据 |
| `status_cc` | 多 bit 慢变值 | 是 | 组件自动 | 否 | 状态/配置寄存器 |
| `async_fifo` | 连续数据流 | 是 | 握手 | 是 | 高速连续流 |

#### 4.4.3 源码精读（复用关系）

三件套的复用关系一目了然，全部可在源码里指认：

- `simple_cc` 例化 `pulse_cc` 传 valid：[hdl/psi_common_simple_cc.vhd:46-61](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_simple_cc.vhd#L46-L61)。
- `status_cc` 例化 `simple_cc` 传数据：[hdl/psi_common_status_cc.vhd:105-122](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_status_cc.vhd#L105-L122)。
- `bit_cc` 不依赖任何其他组件，自含两级同步器：[hdl/psi_common_bit_cc.vhd:45-52](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_bit_cc.vhd#L45-L52)。

因此形成一条清晰的依赖链：`pulse_cc` → `simple_cc` → `status_cc`，每往上一层就多解决一个问题（数据怎么带、节拍谁来管）；`bit_cc` 则另起一路，专攻「独立单 bit」这个最便宜的需求。理解这条链，就能推断：改 `pulse_cc` 的同步器深度会影响 `simple_cc` 和 `status_cc` 的延迟（但不会影响 `bit_cc`）。

#### 4.4.4 代码实践

**目标**：用一张检查清单，把一个真实需求映射到正确组件。

**操作步骤**：对下面三个跨域需求，逐项填表（信号形态 / 是否带数据 / 节拍归属 / 是否容忍跳值），然后给出组件选择。

1. 100 MHz 域的一个按键消抖输出（单 bit「按下/松开」），跨到 50 MHz 域。
2. 100 MHz 域 SPI 控制器偶发更新的 16 bit 增益寄存器，跨到 50 MHz 域的数据通路。
3. 100 MHz 域 ADC 每个有效周期吐一个 16 bit 样本（带 valid），跨到 50 MHz 域。

**需要观察的现象 / 预期结果（参考答案）**：

| 需求 | 形态 | 带数据 | 节拍 | 容忍跳值 | 选择 |
|:--|:--|:--|:--|:--|:--|
| 1 | 独立单 bit | 各 bit 独立 | 源端持续 | 是 | `bit_cc` |
| 2 | 慢变 16 bit | 是 | 不想管 | 是 | `status_cc` |
| 3 | 逐样本 16 bit+vld | 是 | 用户(vld) | 否 | `simple_cc`（若速率 ≤ f_b/4）；若超出则 `async_fifo` |

需求 3 是 `simple_cc` 与 `async_fifo` 的分水岭：当数据接近连续、可能超过 f_b/4 时，必须改用带反压的异步 FIFO（[u4-l2](u4-l2-async-fifo.md)），否则会丢样本。

#### 4.4.5 小练习与答案

**练习**：同样是「把一个 8 bit 值从 A 跨到 B」，什么情况下选 `simple_cc`、什么情况下选 `status_cc`、什么情况下两个都不对？

**答案**：若该值是「事件触发的单样本」且你愿意自己拉 valid，选 `simple_cc`；若该值是「持续存在、慢变、没有明确采样点」的状态量且你不想管 valid，选 `status_cc`；若是「连续高速、需要反压」的数据流，两者都不对，选 `async_fifo`。

## 5. 综合实践

把本讲内容串起来，完成一个「跨域方案设计 + 仿真验证」的小任务。

**场景**：某 FPGA 有两个完全异步的时钟域 ClkA = 100 MHz、ClkB = 50 MHz。需要从 A 跨到 B 传递下列三类信号：

- **S1**：FIFO「半满」告警标志（1 bit，慢变，B 域只要知道当前告警状态）。
- **S2**：16 bit 增益寄存器（由 A 域 SPI 偶发更新，B 域数据通路需要用到最新值，偶尔漏一个中间值无所谓）。
- **S3**：16 bit 测量结果（A 域每个有效周期产出一个，附带 valid；B 域需要逐个接收，不能丢）。

**任务**：

1. 为 S1、S2、S3 各选一个本讲（或 u5-l1/u4-l2）的组件，并各写一句理由。
2. 指出 S3 在什么速率条件下会从「`simple_cc` 够用」切换到「必须用 `async_fifo`」，给出临界数据速率。
3. 为这些 A→B 跨域路径写一条 Vivado 约束（参考文档给出的范例）。
4. 验证：跑 `psi_common_status_cc_tb`（已注册于 [sim/config.tcl:245](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L245)），观察 B 域 `b_dat_o` 是否在若干慢时钟周期后追上 `a_dat_i`，作为 S2 这类「慢变值」行为的佐证。

**参考答案**：

1. S1 → `bit_cc(width_g=>1)`：独立单 bit 标志，两级同步器最省。S2 → `status_cc(width_g=>16)`：慢变配置值，免管 valid、允许跳值。S3 → `simple_cc(width_g=>16)`：逐样本带 valid；若速率过高则升 `async_fifo`。
2. 临界速率 = f_b / 4 = 50 MHz / 4 = 12.5 MS/s。S3 数据速率 ≤ 12.5 MS/s 用 `simple_cc`；超过则必须用 `async_fifo`（带反压，不会丢样本）。
3. 约束范例（参考 `doc/files/psi_common_status_cc.md` 与 `simple_cc.md`）：`set_max_delay --datapath_only --from [get_clocks ClkA] -to [get_clocks ClkB] 20.0`（20 ns = ClkB 一个周期，50 MHz）。其含义是：源域到目的域的跨域路径延迟不得超过目的时钟一个周期，保证 valid/toggle 跨域稳定。
4. 仿真：按 [u1-l3](u1-l3-dependencies-and-simulation.md) 的流程运行 `status_cc` 回归；TB 在 [testbench/psi_common_status_cc_tb/psi_common_status_cc_tb.vhd:163-168](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_status_cc_tb/psi_common_status_cc_tb.vhd#L163-L168) 处断言 `b_dat_o` 最终等于设置值，通过即说明慢变值被正确搬运。**实际仿真波形待本地验证。**

## 6. 本讲小结

- `simple_cc` 用「valid 走 `pulse_cc`、数据原地锁存」的方式，让多 bit 样本安全跨域；它只用 valid、不带 ready，调用者须保证数据速率 ≤ f_b/4。
- `status_cc` 在 `simple_cc` 之上加了一个 A↔B 握手回环（发 valid → B 翻转回执 → A 检测回执再发），从而**自动**生成传输节拍，适合慢变状态/配置值；允许跳过中间值，约束是变化率 ≤ f_slow/10。
- `bit_cc` 是给「相互独立的单 bit」每人配一组两级同步器，最便宜；但因各 bit 互不对齐，**严禁用于多 bit 相关总线**，否则产生撕裂值。
- 三件套形成依赖链 `pulse_cc → simple_cc → status_cc`，`bit_cc` 独立成路；选型看「信号形态 + 谁管节拍 + 是否容忍跳值 + 是否需要反压」。
- 复位跨域在 `simple_cc`/`status_cc` 中都顺带由底层 `pulse_cc` 完成；`bit_cc` 无复位，靠寄存器初值。
- 文档表格里的 generic 名（`data_width_g`/`num_bits_g`）偶尔与源码（`width_g`）不一致，**实例化时一律以 `.vhd` 源码为准**。

## 7. 下一步学习建议

- 进入 [u5-l3](u5-l3-sync-ratio-cc.md)，学习**同步整数比**时钟域之间的跨越（`sync_cc_n2xn`/`sync_cc_xn2n`）。本讲的三件套针对「完全异步」时钟；当两个时钟是整数倍频（同源、相位可预测）时，可以用更轻量的同步跨越，不必走异步同步器。
- 若你的需求是「连续高速数据流」，跳到 [u4-l2 async_fifo](u4-l2-async-fifo.md)，对比带反压的异步 FIFO 与本讲 `simple_cc` 的边界。
- 想了解这些组件如何被「按位宽批量生成」，可预习 [u11-l2 代码生成器](u11-l2-code-generators.md)，其中 `generators/psi_common_simple_cc_X.py` 正是为 `simple_cc` 生成特定位宽实例的脚本。
