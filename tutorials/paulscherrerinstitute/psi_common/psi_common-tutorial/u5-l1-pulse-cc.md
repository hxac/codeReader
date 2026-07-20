# 脉冲跨越 pulse_cc 与复位同步

## 1. 本讲目标

本讲是「时钟域跨越 CDC」单元的第一篇。读完本讲，你应当能够：

- 说清楚为什么单周期脉冲不能直接从 A 时钟域拉一根线到 B 时钟域，以及 `psi_common_pulse_cc` 用「翻转 → 同步 → 异或」三步解决它的原理；
- 读懂复位同步器「异步复位、同步释放」的写法，并理解它为何必须这么写；
- 理解 `a_rst_pol_g` / `b_rst_pol_g` 两个极性 generic 如何让同一份代码同时支持高有效/低有效复位；
- 认识 `ASYNC_REG`、`shreg_extract`、`syn_srlstyle` 三种综合属性在 CDC 场景下的作用，以及为什么文档敢写「本实体无需额外约束」。

本讲只读一个核心源文件 [`hdl/psi_common_pulse_cc.vhd`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_cc.vhd)，但会串起它的测试平台、它在 `simple_cc` 中的复用方式，以及 `sim/config.tcl` 里的回归注册。

## 2. 前置知识

在进入源码前，先用大白话把三个概念讲清楚。

**时钟域（clock domain）。** 一块 FPGA 里常常有多个独立的时钟，比如 100 MHz 的处理时钟和 50 MHz 的接口时钟。如果它们来自不同的晶振或不同的锁相环、彼此不锁定相位，就叫「完全异步时钟」。同一个信号在被不同时钟采样的两个区域之间流动时，就发生了「时钟域跨越（Clock Domain Crossing，CDC）」。

**亚稳态（metastability）。** 一个触发器（FF）在时钟沿到来时采样数据。如果数据正好在时钟沿附近变化，触发器可能既采不稳、也无法在规定建立/保持时间内决定是 0 还是 1，输出会停在一个非法电平上，要等「一会儿」才随机塌缩成 0 或 1。这种现象叫亚稳态。解决办法是给信号串一串专门的同步触发器（同步器，synchronizer），给亚稳态留出足够的塌缩时间，使最终被用的那一拍是稳定的。同步器级数越多、平均无故障时间（MTBF）越长。

**脉冲（pulse）。** 在本库里，「脉冲」指只拉高一个时钟周期的「事件」信号，比如「这一拍触发了一次采样」「这一拍收到一个有效的状态更新」。注意它和数据流（continuously valid 的数据）不同：脉冲是稀疏的事件，数据流是连续的字。

承接 [u1-l4](u1-l4-coding-conventions-handshaking.md) 已经建立的约定：本库端口用 `_i/_o` 后缀、`snake_case` 命名、AXI-S 的 VLD/RDY 握手语义。`pulse_cc` 不走 AXI-S（它传的是事件而非数据字），但它内部使用的复位信号、它被 `simple_cc` 复用后包装出的 `vld` 信号，都遵循同一套语义。

## 3. 本讲源码地图

| 文件 | 作用 |
|:-----|:-----|
| [`hdl/psi_common_pulse_cc.vhd`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_cc.vhd) | 唯一被精读的源文件。脉冲 + 复位双向 CDC 的全部实现都在这不到 170 行里。 |
| [`testbench/psi_common_pulse_cc_tb/psi_common_pulse_cc_tb.vhd`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_pulse_cc_tb/psi_common_pulse_cc_tb.vhd) | 自校验测试平台。用 100 MHz / 50 MHz 两个时钟，发一个脉冲并在 B 侧等待它出现。 |
| [`hdl/psi_common_simple_cc.vhd`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_simple_cc.vhd) | 数据值跨越组件。它直接例化 `pulse_cc` 来传「有效」脉冲，自己只额外锁存数据——是理解 `pulse_cc` 复用方式的最佳例子。 |
| [`sim/config.tcl`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl) | 回归测试注册表。`pulse_cc` 在此登记了 8 组 generic 组合，覆盖四种极性搭配 × 两种复位先后顺序。 |
| [`doc/files/psi_common_pulse_cc.md`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_pulse_cc.md) | 官方组件说明（接口表 + 三张时序图）。 |

先看实体声明，建立整体印象：

[`hdl/psi_common_pulse_cc.vhd:22-34`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_cc.vhd#L22-L34) —— 定义了三个 generic 与两套对称的端口（A 侧、B 侧各有 `clk/rst/rst_o/dat`）。

注意 generic `num_pulses_g` 后面的行内注释写的是「fifo width」，这是一个有误导性的历史遗留注释——它其实是**并行跨越的独立脉冲通道数**，与 FIFO 没有任何关系（见 4.1.3）。

## 4. 核心概念与源码讲解

### 4.1 翻转-同步-异或机制（脉冲是怎么过去的）

#### 4.1.1 概念说明

为什么不能直接把 A 域的单周期脉冲接到 B 域？因为 B 时钟可能比 A 慢，也可能恰好和脉冲错开——这个只存在一拍的窄脉冲很可能根本没被 B 的任何一个时钟沿采到，于是脉冲「丢了」。即便采到，由于亚稳态，采到的值也不可信。

`pulse_cc` 的思路是**把「事件」先变成「电平」**：在 A 域，每来一个脉冲就把一个电平信号翻转一次（toggle）。电平信号是长期稳定的，可以被 B 域的多级同步器可靠采样。到了 B 域，再用「相邻两级同步寄存器做异或」把电平的「跳变沿」还原成单周期脉冲。这就是经典的 **toggle → synchronize → XOR** 三步法。

文件头部的注释也明确说明了它的能力边界：

[`hdl/psi_common_pulse_cc.vhd:10-15`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_cc.vhd#L10-L15) —— 「脉冲频率必须明显低于两个时钟中较慢者」「只保证所有脉冲都被传过去，不保证同一 A 拍到达的脉冲还在同一 B 拍到达」。

#### 4.1.2 核心流程

设 `f_b` 为目标域（B 侧）时钟频率。一条脉冲通道的完整数据通路是：

```
A 域 (a_clk)                       B 域 (b_clk)
----------                         -----------------------------
a_dat_i ──► [xor] ◄── ToggleA ──► FF ──► ToggleSyncB(0) ──► FF ──► ToggleSyncB(1) ──► FF ──► ToggleSyncB(2)
              ▲                                                                                   │
              │                                                                              [xor]──► b_dat_o
              └────────────────────────────────────────────────────────────◄── ToggleSyncB(1) ──┘
```

逐步解释：

1. **翻转（A 侧）。** `ToggleA <= ToggleA xor a_dat_i`。每个通道独立：脉冲到来那一拍，该比特翻转一次；没有脉冲则保持。于是 `ToggleA` 成了一个「累计脉冲数的奇偶性」电平。
2. **同步（跨域）。** `ToggleA` 进入 B 域的三级移位寄存器 `ToggleSyncB`，逐拍搬移，等价于一个 3-FF 同步器。
3. **异或还原（B 侧）。** `b_dat_o <= ToggleSyncB(2) xor ToggleSyncB(1)`。当同步后的电平在相邻两拍之间发生了跳变，异或结果为 1，维持一个 B 时钟周期——这就是被还原出来的脉冲。

可靠性约束可以用一个不等式表达。设某一通道上相邻两个脉冲在 A 域的时间间隔为 \(T_{pp}\)。为了让每一次翻转都能被 B 域稳定采到（即两次翻转之间，电平至少要在 B 域保持足够多拍），需要：

\[
T_{pp} \;\ge\; \frac{n_{\text{sync}}}{f_b}
\]

其中 \(n_{\text{sync}}=3\) 是 B 域同步器级数。直观上：**同一通道上两个脉冲不能挨得太近**，否则两次翻转相互抵消（toggle 两次 = 没变），脉冲被「吃掉」。这就是注释里「脉冲频率必须明显低于较慢时钟」的数学含义。

#### 4.1.3 源码精读

先看 A 侧的翻转进程：

[`hdl/psi_common_pulse_cc.vhd:143-152`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_cc.vhd#L143-L152) —— 复位时清零，否则 `ToggleA` 与输入脉冲逐拍异或。注意 `ToggleA` 是 `std_logic_vector(num_pulses_g-1 downto 0)`，**每个比特是一条独立的脉冲通道**，所以 `num_pulses_g` 的真实含义是「并行通道数」。这也解释了为什么注释写「fifo width」是误导：这里根本没有 FIFO。

再看 B 侧的「同步 + 异或还原」：

[`hdl/psi_common_pulse_cc.vhd:155-166`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_cc.vhd#L155-L166) —— 关键两行：

```vhdl
ToggleSyncB <= ToggleSyncB(ToggleSyncB'left - 1 downto 0) & ToggleA;          -- 三级移位 = 3-FF 同步器
b_dat_o     <= ToggleSyncB(ToggleSyncB'left) xor ToggleSyncB(ToggleSyncB'left - 1);  -- 沿检测还原脉冲
```

`ToggleSyncB` 的类型见声明 [`hdl/psi_common_pulse_cc.vhd:39-50`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_cc.vhd#L39-L50)：它是一个数组 `Pulse_t(2 downto 0)`，即 3 级、每级宽度为 `num_pulses_g` 的寄存器组——所有通道共享同一套移位/异或结构，互不影响。

`num_pulses_g > 1` 时的真实复用例子见 `simple_cc` 的兄弟组件 `par_tdm` 等；而在 [`hdl/psi_common_simple_cc.vhd:46-61`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_simple_cc.vhd#L46-L61) 里，`simple_cc` 把 `num_pulses_g` 设为 1，只用通道 0 来传「valid 脉冲」，数据本身则在 A 侧用寄存器锁存、在 B 侧等 `vld` 到来时再读出——这是理解 `pulse_cc` 用途的最佳参考。

#### 4.1.4 代码实践

**实践目标：** 在源码上标注出「A→B」与「B→A」两条同步路径，并解释 `num_pulses_g` 的作用。

**操作步骤（源码阅读型，无需运行）：**

1. 打开 [`hdl/psi_common_pulse_cc.vhd`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_cc.vhd)。
2. **A→B 脉冲路径**：从 `a_dat_i`（L29）→ `PulseA_p` 里的 `ToggleA`（L149）→ `PulseB_p` 里的移位 `ToggleSyncB`（L162）→ 异或输出 `b_dat_o`（L163）。用三种颜色各画一段。
3. **B→A 复位路径**（复位同步在 4.2 详讲，这里先标路径）：`b_rst_i`（L31）→ `ARstSync_p` 的 `RstSyncB2A`（L73-L80）→ `ARst_p` 合并出 `RstAI`（L87/L89）→ `a_rst_o`（L101/L104）。对称地，`a_rst_i`→`RstSyncA2B`→`RstBI`→`b_rst_o` 是 A→B 的复位路径。
4. 把 `num_pulses_g` 从 1 想象成 4：在脑海里把 `ToggleA`、`ToggleSyncB`、`a_dat_i`、`b_dat_o` 都加宽到 4 比特，确认 4 条通道完全并行、互不干扰。

**需要观察的现象：** A→B 走的是「数据/脉冲」通路（xor 同步），B→A 与 A→B 的复位走的是「复位同步器」通路（移位寄存器）——两套通路物理上分开，分别优化。

**预期结果：** 你应当能画出一张含 4 个信号节点的小图，并说出 `num_pulses_g` = 并行通道数（不是 FIFO 宽度）。

#### 4.1.5 小练习与答案

**Q1.** 假设 B 时钟 50 MHz，同步器 3 级。某通道上脉冲间隔 \(T_{pp}\) 分别为 20 ns 和 80 ns，哪个一定安全、哪个有风险？

**答：** 阈值 \(n_{\text{sync}}/f_b = 3 / 50\,\text{MHz} = 60\,\text{ns}\)。20 ns < 60 ns，有风险（两次翻转可能被采成「没变」，脉冲丢失）；80 ns > 60 ns，安全。

**Q2.** 为什么还原脉冲用的是 `ToggleSyncB(2) xor ToggleSyncB(1)`，而不是直接对 `ToggleSyncB(2)` 做某种「变化检测」？

**答：** 异或相邻两级已经稳过的寄存器，等价于「这一拍相比上一拍是否翻转」，能干净地输出一个 B 周期宽的脉冲；而且参与运算的两级都已经过同步、无亚稳态风险，比直接组合检测更可靠。

**Q3.** 把 `num_pulses_g` 设为 4，资源（触发器）大约增加在哪？

**答：** `ToggleA`、`ToggleSyncB`（3 级）、`b_dat_o` 都按比特数线性增长，每通道约 \(1+3+1\) 个寄存器位；复位同步链 `RstSyncB2A/RstSyncA2B` 不随通道数变化。

---

### 4.2 复位同步链（异步复位、同步释放）

#### 4.2.1 概念说明

CDC 组件还有一个绕不开的问题：**复位**。如果 A 域按了复位，B 域怎么知道？反过来呢？`pulse_cc` 给出的方案是：在 B 域放一条复位同步链，输入是 A 域的 `a_rst_i`；只要 A 复位一有效，B 侧同步链**立刻（异步）**全部进入复位态；等 A 复位撤销后，B 侧同步链**一拍一拍（同步于 b_clk）**地把「已撤销」状态移出来。这就是数字设计里反复强调的「**异步复位、同步释放**（async assert, sync de-assert）」。

为什么要同步释放？因为复位撤销沿如果和时钟沿不对齐，会让受复位控制的寄存器在同一时刻有的解除、有的没解除，导致整个域里不同寄存器脱离复位的时刻不一致——轻则功能错乱，重则再次引入亚稳态。同步释放保证「整个域在同一拍、同一个时钟沿一起脱离复位」。

#### 4.2.2 核心流程

以「把 B 域复位搬进 A 域」为例（`ARstSync_p` + `ARst_p`），流程是：

1. **异步置位：** `b_rst_i` 一旦等于有效极性，无论 `a_clk` 此刻是什么，同步链 `RstSyncB2A` 立刻被全量写成复位值。
2. **同步释放：** `b_rst_i` 撤销后，每个 `a_clk` 上升沿向链里移入一个「非复位值」，经过若干拍后，链的最左端（`RstSyncB2A(left)`，即 bit 3）才变成非复位值。
3. **合并本域复位：** 把同步后的远端复位与 A 域自己的 `a_rst_i` 合并，得到本域内部使用的 `RstAI`。
4. **输出复位：** `a_rst_o` 再由 `RstAI` 与 `a_rst_i` 合并得到，保证「只要任一域复位有效，两侧复位输出都有效」。

整条链是 4 级（`std_logic_vector(3 downto 0)`），比最小 metastability 需求更宽裕，给复位撤销留出充足的同步裕量。文档 [`doc/files/psi_common_pulse_cc.md:20`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_pulse_cc.md#L20) 明确说复位跨越用的就是这套「async assert, sync de-assert」链。

#### 4.2.3 源码精读

B→A 的复位同步进程（注意敏感列表里同时有 `a_clk_i` 和 `b_rst_i`，这就是「异步复位」的标志）：

[`hdl/psi_common_pulse_cc.vhd:72-80`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_cc.vhd#L72-L80) —— 关键两行：

```vhdl
if b_rst_i = b_rst_pol_g then
  RstSyncB2A <= (others => b_rst_pol_g);                                  -- 异步置位
elsif rising_edge(a_clk_i) then
  RstSyncB2A <= RstSyncB2A(RstSyncB2A'left - 1 downto 0) & not b_rst_pol_g;  -- 同步释放（左移）
```

这里 `b_rst_pol_g` 同时出现在「异步置位值」和移位方向里，所以无论 B 复位是高有效还是低有效，链都能正确表达（这点在 4.3 再展开）。链的输出取最左端 `RstSyncB2A(3)`，它是「已经同步进 a_clk 域」的 B 复位。

合并本域复位的进程：

[`hdl/psi_common_pulse_cc.vhd:82-99`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_cc.vhd#L82-L99) —— 把 `RstSyncB2A(3)` 与 `a_rst_i` 合并出 `RstAI`。注意这里没有异步复位分支（进程敏感列表只有 `a_clk_i`），因为 `RstAI` 是「同步释放后」才用的内部信号，本身不需要再异步。A→B 的复位链完全对称，见 [`hdl/psi_common_pulse_cc.vhd:108-134`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_cc.vhd#L108-L134)。

最后是 `RstAI` / `RstBI` 的初值声明：

[`hdl/psi_common_pulse_cc.vhd:42-46`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_cc.vhd#L42-L46) —— `RstAI := a_rst_pol_g`、`RstBI := b_rst_pol_g`。这个初值是 2024 年的一次 bugfix（commit `8df502c`）特意加上的：上电后、第一个时钟沿到来之前，内部复位就已经是「有效」态，避免仿真/上电瞬间出现复位反相的毛刺。

#### 4.2.4 代码实践

**实践目标：** 通过测试平台观察「同步释放」造成的复位延迟。

**操作步骤（仿真阅读型）：**

1. 打开 [`testbench/psi_common_pulse_cc_tb/psi_common_pulse_cc_tb.vhd`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_pulse_cc_tb/psi_common_pulse_cc_tb.vhd)。
2. 看复位/时钟进程 [`...tb.vhd:42-94`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_pulse_cc_tb/psi_common_pulse_cc_tb.vhd#L42-L94)：A 复位先撤销、B 复位后撤销，两者时间错开。
3. 看激励进程 [`...tb.vhd:113-132`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_pulse_cc_tb/psi_common_pulse_cc_tb.vhd#L113-L132)：它 `wait until b_rst_obs=not b_rst_pol_g and a_rst_obs=not a_rst_pol_g`，即**等到两侧复位输出都撤销**才发脉冲。

**需要观察的现象：** `a_rst_i` 撤销后，`a_rst_obs` 不会立刻撤销，而是要等同步链移位完毕（几个 a_clk 周期）后才撤销。

**预期结果（待本地验证）：** 在波形上测量 `a_rst_sti` 撤销沿到 `a_rst_obs` 撤销沿之间的 a_clk 周期数，应与同步链级数同量级（≈ 几拍），这正是「同步释放」的可视证据。

#### 4.2.5 小练习与答案

**Q1.** `ARstSync_p` 的进程敏感列表为什么必须包含 `b_rst_i`？

**答：** 这样 `b_rst_i` 一旦有效，进程不必等 `a_clk` 沿就能立刻把 `RstSyncB2A` 写成复位值，实现「异步复位」。如果只把 `a_clk_i` 放进敏感列表，复位就变成纯同步的，无法在无时钟或时钟异常时强制复位。

**Q2.** 复位同步链是 4 级，而脉冲同步器是 3 级。为什么复位链给得更宽裕？

**答：** 复位关系到整个域所有寄存器同时脱离复位，对亚稳态和时序对齐更敏感，多一级换来更长的 metastability 塌缩时间与更高的 MTBF，是「关键路径宁多勿少」的保守设计。

**Q3.** 如果删掉 `RstAI := a_rst_pol_g` 的初值，上电瞬间可能出什么问题？

**答：** 仿真里 `RstAI` 初值会是 `'U'`，在第一个 a_clk 沿之前 `PulseA_p` 的 `if RstAI = a_rst_pol_g` 判断不确定，可能产生不确定态/毛刺；初值保证上电即处于确定的有效复位态。

---

### 4.3 复位极性 generic（a_rst_pol_g / b_rst_pol_g）

#### 4.3.1 概念说明

不同 FPGA / 不同项目的复位约定不一样：有的复位是「高有效」（`'1'`=复位），有的是「低有效」（`'0'`=复位）。`pulse_cc` 用两个 generic `a_rst_pol_g`、`b_rst_pol_g`（默认都是 `'1'`）让 A、B 两域各自独立选择极性。这是本库「全 generic 化」风格（见 [u2-l1](u2-l1-math-pkg.md)）在接口层的体现：一份代码、四种极性搭配，全部由综合时的静态 `if generate` / 常量分支决定，不产生额外运行时开销。

#### 4.3.2 核心流程

极性处理贯穿三处，每处都按 generic 选分支：

1. **同步链的复位值/移位值**：异步置位写 `b_rst_pol_g`、移位写入 `not b_rst_pol_g`（见 4.2.3）。
2. **内部复位合并 `RstAI`/`RstBI`**：`b_rst_pol_g='1'` 时用 `or` 合并、`'0'` 时用 `and` 合并。
3. **复位输出 `a_rst_o`/`b_rst_o`**：用 `if a_rst_pol_g='1' generate` 在两段并发赋值里二选一。

最终对外语义统一：`a_rst_o` 在「`a_rst_i` 或 `b_rst_i` 任一有效」时有效，并以 `a_rst_pol_g` 极性输出（见实体端口注释 [`hdl/psi_common_pulse_cc.vhd:28`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_cc.vhd#L28) 与 [`:32`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_cc.vhd#L32)）。

#### 4.3.3 源码精读

合并逻辑里的极性分支（以 A 侧为例，B 侧对称）：

[`hdl/psi_common_pulse_cc.vhd:85-97`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_cc.vhd#L85-L97) —— `b_rst_pol_g='1'`（远端高有效）时 `RstAI <= RstSyncB2A(left) or a_rst_i`；`'0'` 时用 `and`。复位输出则用 generate 二选一：

[`hdl/psi_common_pulse_cc.vhd:100-105`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_cc.vhd#L100-L105) —— `a_rst_pol_g='1'` 时 `a_rst_o <= RstAI or a_rst_i`，`'0'` 时 `a_rst_o <= RstAI and a_rst_i`。注意这是 `generate`，不是 `if`：两段互斥的并发赋值语句在综合时只保留命中的那一段，另一段不存在于网表中。

回归测试覆盖了全部四种极性搭配，见注册表：

[`sim/config.tcl:167-177`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L167-L177) —— 8 组 generic：`a_rst_pol_g/b_rst_pol_g` 取 `(1,1)/(0,0)/(1,0)/(0,1)` 各两组，再 ×`a_rst_before_g=true/false`（控制 A、B 复位谁先撤销）。

> **实践提示：** 两侧同极性（默认 `(1,1)` 高有效）是绝大多数项目的用法，合并用 `or`、语义直观。当需要混合极性（如 A 高有效、B 低有效）时，组合逻辑由 generic 在综合时静态选择；由于这种用法较少见，建议在仿真中专门确认「只在一侧单独施加复位时，对侧复位输出能否正确跟随」，避免集成时踩坑。

#### 4.3.4 代码实践

**实践目标：** 用现有回归覆盖确认四种极性搭配都被测过。

**操作步骤（脚本阅读型）：**

1. 打开 [`sim/config.tcl`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl) 第 167–177 行。
2. 列出 8 条 `tb_run_add_arguments`，把每条的 `-ga_rst_pol_g` 和 `-gb_rst_pol_g` 配对摘出来。
3. 对照 TB 实体的默认值 [`...tb.vhd:19-20`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_pulse_cc_tb/psi_common_pulse_cc_tb.vhd#L19-L20)（注意 TB 默认 `a_rst_pol_g='1'`、`b_rst_pol_g='0'`，但运行时被 config 覆盖）。

**需要观察的现象：** 四种极性搭配各有覆盖；`a_rst_before_g` 让复位撤销先后顺序也变化。

**预期结果：** 得到一张 4×2 的小表，确认极性与复位顺序两个维度都被回归覆盖。

#### 4.3.5 小练习与答案

**Q1.** 为什么 `a_rst_o` 的两段赋值用 `if ... generate` 而不是 `if ... then`？

**答：** 这是并发赋值（`a_rst_o <= ...`），不在进程内，必须用 `generate` 在结构层二选一；进程内的顺序逻辑才用 `if then`。`generate` 在综合时只实例化命中分支，不增加多路选择逻辑。

**Q2.** 端口注释说 `a_rst_o`「active if `a_rst_i` or `b_rst_i` is asserted」。在默认 `(1,1)` 配置下，这句话在代码里由哪几行兑现？

**答：** `ARstSync_p` 把 `b_rst_i` 同步进 A 域（L73-L80），`ARst_p` 用 `or` 把它与 `a_rst_i` 合并进 `RstAI`（L87），再由 generate 用 `or` 与 `a_rst_i` 合并出 `a_rst_o`（L101）。三处共同保证「任一有效即输出有效」。

**Q3.** 把 `a_rst_pol_g` 改成 `'0'`，`a_rst_o` 的有效电平会怎样？

**答：** 变成低有效：复位时 `a_rst_o='0'`，正常时 `a_rst_o='1'`；`generate` 选中 `a_rst_o <= RstAI and a_rst_i` 分支（L104）。

---

### 4.4 综合属性约束（ASYNC_REG / shreg_extract / syn_srlstyle）

#### 4.4.1 概念说明

CDC 电路最怕综合工具「好心办坏事」：把同步器的几个触发器优化进一个查找表（LUT）做成的移位寄存器（SRL）、或者把它们重定时（retiming）打散到别处、或者不识别这是异步路径从而报一堆时序违例。`pulse_cc` 用三种综合属性把这条路堵死：

- **`ASYNC_REG`（Xilinx）**：标记「我会采样异步信号」的触发器，让工具对这些 FF 禁止重定时、尽量放在同一个 slice 以缩短布线延迟，并自动给其输入端施加异步路径约束。
- **`shreg_extract`（Xilinx）= "no"**：禁止把移位寄存器链抽成 SRL（shift-register LUT），强制每一级都是真实触发器——这是同步器能正确工作的前提。
- **`syn_srlstyle`（Synopsys Synplify）= "registers"**：对 Synplify 流程做同样的事——用寄存器实现，不要 SRL。

正因为这些属性已经「内建」了正确的约束，官方文档才敢写「本实体无需额外约束」（见 [`doc/files/psi_common_pulse_cc.md:56-58`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_pulse_cc.md#L56-L58)）。

#### 4.4.2 核心流程

属性声明的对象是 4 组信号：A 域复位同步链 `RstSyncB2A`、B 域复位同步链 `RstSyncA2B`、脉冲翻转源 `ToggleA`、脉冲同步链 `ToggleSyncB`。每种属性一次性贴到这 4 组上：

```
对每个 CDC 相关信号组：
    ASYNC_REG   = "TRUE"   （Xilinx：标记为异步接收 FF，禁重定时、自动约束）
    shreg_extract = "no"   （Xilinx：禁 SRL 抽取，强制真实 FF）
    syn_srlstyle = "registers"  （Synplify：同上，跨厂商对齐）
```

三种属性针对不同厂商/工具，但目的一致：**保证同步器是「若干个紧挨着的真实触发器」，且被正确识别为异步路径。**

#### 4.4.3 源码精读

全部属性声明集中在一段：

[`hdl/psi_common_pulse_cc.vhd:52-68`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_cc.vhd#L52-L68) —— 三组 `attribute ... : string;` 加 `attribute ... of <信号> : signal is "...";`。注意 VHDL 写法：先声明属性名是 `string` 类型，再用 `of ... : signal is` 把字符串值贴到具体信号上。

被贴属性的信号，其作用回顾：

| 信号 | 所属域 | 角色 |
|:-----|:------|:-----|
| `RstSyncB2A` | A（a_clk） | 接收 B 域复位，是异步源 |
| `RstSyncA2B` | B（b_clk） | 接收 A 域复位，是异步源 |
| `ToggleA` | A（a_clk） | 被采样源（送入 B 域同步器） |
| `ToggleSyncB` | B（b_clk） | 采样 `ToggleA` 的 3-FF 同步器 |

也就是说，凡是在两个时钟域之间直接相连的寄存器，全部被标注；纯域内逻辑不受影响。

#### 4.4.4 代码实践

**实践目标：** 评估「删掉这些属性」会带来什么风险。

**操作步骤（源码阅读 + 推理型）：**

1. 重读 [`hdl/psi_common_pulse_cc.vhd:52-68`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_cc.vhd#L52-L68)，把每个属性对应到它保护的信号。
2. 假设删掉 `shreg_extract="no"`：`ToggleSyncB`（3 级 FF）可能被 Xilinx 工具合并成一个 SRL16，于是「同步器」实际只剩一个时钟周期的 metastability 窗口——MTBF 暴跌。
3. 假设删掉 `ASYNC_REG="TRUE"`：工具不知道这些 FF 收的是异步信号，可能报 `setup` 违例，或把它们 retiming 到别处打乱同步链结构。

**需要观察的现象（待本地验证）：** 如果有 Vivado/Synplify 环境，分别综合「带属性」与「手动删属性」两版，对比时序报告里 `RstSyncB2A`/`ToggleSyncB` 是否仍由独立 FF 构成、是否出现异步路径报告。

**预期结果：** 带属性版本应看到这些寄存器被标注为 async-synchronizer、无 setup 违例；删属性版本可能出现 SRL 抽取或时序告警，体现属性的保护作用。

#### 4.4.5 小练习与答案

**Q1.** `shreg_extract` 和 `syn_srlstyle` 干的是同一件事，为什么要写两个？

**答：** 它们分别面向 Xilinx 工具链和 Synopsys Synplify 工具链。本库目标是厂商无关（见 [u1-l1](u1-l1-project-overview.md)），所以两个属性都写，让同一份代码在不同综合器下行为一致；不识别的属性会被对应工具忽略，互不冲突。

**Q2.** `ASYNC_REG` 贴在 `ToggleA` 上，但 `ToggleA` 是 a_clk 域的、由 a_clk 驱动，为什么也要标？

**答：** `ToggleA` 是被 B 域同步器采样的「异步源端」。标记它有助于工具理解整条跨域路径（源端 + 接收端），在布线时让源端到第一级接收 FF 的延迟尽量小，进一步抬高 MTBF。

**Q3.** 文档说「无需任何约束」，那用户就真的什么都不用做吗？

**答：** 对 `pulse_cc` 内部的同步器确实不用手写 XDC/SDC——属性已自动生成异步路径约束。但这只覆盖组件**内部**的跨域路径；用户在顶层把 `a_dat_i`、复位等连进组件时，仍需保证这些信号在各自源域内是寄存器输出、时序干净。

---

## 5. 综合实践

把本讲四个模块串起来，做一个小集成任务。

**场景：** A 域（100 MHz）每 1 ms 产生一个「采样触发」脉冲，需要送到 B 域（50 MHz）去点一个状态灯；两侧逻辑都需要受统一的复位控制。

**任务：**

1. **选型与实例化。** 用 `pulse_cc` 把「采样触发」从 A 送到 B。由于脉冲间隔 1 ms 远大于 B 域阈值 \(3/f_b = 60\,\text{ns}\)，安全。参照 [`hdl/psi_common_simple_cc.vhd:46-61`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_simple_cc.vhd#L46-L61) 的写法，写一个 `pulse_cc` 的例化（`num_pulses_g => 1`）。
2. **复位接线。** 把 A、B 两域各自的复位接到 `a_rst_i`、`b_rst_i`；用 `pulse_cc` 输出的 `a_rst_o`、`b_rst_o` 去复位两侧的本地逻辑，从而保证「任一域复位，两侧一起复位且同步释放」。
3. **画图与验证。** 画一张标注图，标出：① A→B 脉冲路径（`a_dat_i`→`ToggleA`→`ToggleSyncB`→`b_dat_o`）；② 两条复位同步路径（`a_rst_i`→A2B、`b_rst_i`→B2A）；③ 四组被综合属性保护的信号。
4. **仿真核对。** 仿照 [`testbench/psi_common_pulse_cc_tb/psi_common_pulse_cc_tb.vhd:127-128`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_pulse_cc_tb/psi_common_pulse_cc_tb.vhd#L127-L128) 的做法：在 A 侧发一个 `PulseSig`，在 B 侧用 `WaitForValueStdl(b_dat_obs(0), '1', 4*b_period, ...)` 等待脉冲出现，确认脉冲无丢失、延迟在数个 B 周期内。

**预期结果（待本地验证）：** B 侧灯每 1 ms 闪一次；复位期间两侧逻辑都被 hold；释放复位后两侧在同一数量级周期内一起恢复。

## 6. 本讲小结

- `pulse_cc` 用 **toggle → 3-FF 同步 → XOR 沿检测** 三步把单周期脉冲跨异步时钟域；脉冲必须稀疏（间隔 \(\ge n_{\text{sync}}/f_{\text{slow}}\)），否则翻转会相互抵消。
- `num_pulses_g` 是**并行脉冲通道数**（代码里「fifo width」注释有误导），各通道共享复位逻辑、数据通路完全独立。
- 复位用**异步复位、同步释放**的 4 级同步链；`RstAI/RstBI` 带初值，保证上电即处于确定复位态（2024 年 bugfix 引入）。
- `a_rst_pol_g`/`b_rst_pol_g` 让一份代码支持高/低有效复位的四种搭配，靠 `if generate` 与按极性选 `or`/`and` 实现。
- 三种综合属性 `ASYNC_REG`/`shreg_extract`/`syn_srlstyle` 把同步器固定为真实触发器并自动施加异步约束，因此组件**无需手写时序约束**。
- `pulse_cc` 是 CDC 单元的基石：`simple_cc` 直接例化它来传 valid 脉冲，后续 `status_cc`、`async_fifo` 的复位跨越也复用同一套思想。

## 7. 下一步学习建议

- 下一篇 **u5-l2（simple_cc / status_cc / bit_cc）**：看 `simple_cc` 如何在 `pulse_cc` 之上加一层「数据锁存」传整字数据，以及 `status_cc`、`bit_cc` 传慢变状态和单 bit 时的取舍——它们都建立在本讲的脉冲与复位同步机制之上。
- 想深入复位同步的工程实践，可对比 [`hdl/psi_common_async_fifo.vhd`](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd) 里同样使用 `pulse_cc` 做复位跨越的位置。
- 想亲手验证，按 [u1-l3](u1-l3-dependencies-and-simulation.md) 的 PsiSim/GHDL 流程跑一遍 `sim/config.tcl` 中 `pulse_cc` 的 8 组回归，观察不同极性下的复位释放波形。
