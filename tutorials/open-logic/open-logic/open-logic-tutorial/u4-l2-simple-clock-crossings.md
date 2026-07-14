# 简单跨时钟域：pulse / simple / status

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「为什么一个单周期脉冲不能直接送进同步器」，以及 Open Logic 用**翻转（toggle）协议**如何把它变成可安全跨域的信号。
- 读懂 `olo_base_cc_pulse`、`olo_base_cc_simple`、`olo_base_cc_status` 三个实体的源码，并指出它们各自「跨的到底是什么」。
- 理解 `cc_simple` 为什么**只跨 Valid、不跨 Data**，以及 `cc_status` 如何用一个「乒乓（req/ack）回环」实现**自洽的快照采样**。
- 看懂跨时钟域选择表（Selection Table），能根据「脉冲 / 采样值 / 缓变状态」这三类数据，选对最省资源的实体。

本讲是 [u4-l1 跨时钟域原理与约束、复位穿越](u4-l1-clock-crossing-principles.md) 的直接延续。u4-l1 讲了所有 `olo_base_cc_*` 共守的两件事——**同步器电路**与**时序约束**配套，以及**复位双向穿越**。本讲不再重复这些通则，而是聚焦三个「最简单、最常用、无 RAM」的跨越实体。

## 2. 前置知识

在进入源码前，先用三段话补齐三个直觉。

### 2.1 为什么脉冲不能直接过同步器

回顾 u4-l1：跨异步时钟域，最底层的积木是**同步器（synchronizer）**——一串级联触发器（`olo_base_cc_bits` 默认 2 级），用来把一个 bit 从源时钟域搬到目的时钟域，并吸收亚稳态。

现在你脑子里有一个冲动：「那我把源域的单周期脉冲 `Pulse`，直接喂给同步器，目的域不就拿到脉冲了吗？」——**这是 CDC 最经典的坑**，有两种失败方式：

- **脉冲被漏掉**：如果源时钟比目的时钟快，脉冲只亮一个源周期，而目的域可能正好在这个周期前后各采样了一次，两次都没采到「1」，脉冲凭空消失。
- **脉冲被拉长**：如果源时钟比目的时钟慢，脉冲亮着的好几个源周期里，目的域可能采样了好几次，原本一个脉冲被还原成多个。

解决办法就是**翻转协议（toggle protocol）**：不要让脉冲「亮一下就灭」，而是让它**永久翻转一个 bit 的电平**。电平是稳定的，同步器绝不会漏采；而目的域只要把「本拍电平」与「上一拍电平」做异或（XOR），电平每变一次，就还原出恰好一个脉冲。这一招是本讲三个实体的共同地基，务必先记住。

### 2.2 「慢且自稳」的数据可以搭便车

跨域数据总线（比如 32 bit）**绝不能**直接进 `olo_base_cc_bits`。因为各位各自经自己的同步器，到达目的域的时刻可能错开一拍，目的域在一个周期里看到的将是「半新半旧」的错位值——这就是多 bit 跨越必须用握手或 FIFO 的根本原因。

但有一类数据例外：**更新得很慢、且写进来后能稳稳保持很久的数据**。对这种数据，可以这样跨域：

1. 源域用 Valid 标记一次更新；
2. Valid 用翻转协议跨过去（这是单 bit，安全）；
3. 等 Valid 到达目的域时，**数据其实早在源域被锁存、并已稳定了好几个目的周期**——目的域直接拿走即可，数据线本身不需要同步器。

这就是 `cc_simple` 的核心思想。代价是：数据更新率必须足够低（低于慢时钟的 \(1/(3+\text{SyncStages\_g})\)），好让数据「赶在 Valid 到达之前」稳定下来。

### 2.3 状态/配置：连 Valid 都不想自己管

有些信号——比如某个 FIFO 的填充度、某个跨域的配置寄存器——你压根**不知道它什么时候变、也不在乎它具体在哪一拍被采样**，只要目的域「迟早能看到一个曾经真实存在过的值」就行。对这种「缓变状态」，连外部 Valid 都省了：让实体**自己**周期性地把当前值快照一份送过去。`cc_status` 就是干这个的。

## 3. 本讲源码地图

本讲聚焦三个实体，但它们层层复用，所以顺带也要认出两个更底层的积木（u4-l1 已讲过）：

| 文件 | 角色 |
| :--- | :--- |
| `src/base/vhdl/olo_base_cc_pulse.vhd` | **脉冲跨越**：把单周期脉冲用翻转协议搬到目的域，恢复成单周期脉冲。本讲主角之一。 |
| `src/base/vhdl/olo_base_cc_simple.vhd` | **采样值跨越**：复用 `cc_pulse` 跨 Valid，数据搭便车；输出带 AXI-S 风格 Valid，无反压。 |
| `src/base/vhdl/olo_base_cc_status.vhd` | **状态/配置跨越**：在 `cc_simple` 外面套一个 `cc_pulse` 回环，自洽地周期采样。 |
| `src/base/vhdl/olo_base_cc_bits.vhd` | （积木）多 bit 同步器，每位独立 2~4 级。`cc_pulse` 内部用它跨翻转信号。 |
| `src/base/vhdl/olo_base_cc_reset.vhd` | （积木）复位双向穿越。三个实体内部都例化它，输出 `RstOut`。 |
| `test/base/olo_base_cc_pulse/olo_base_cc_pulse_tb.vhd` | `cc_pulse` 的 VUnit 测试台，本讲代码实践的依据。 |

三个实体的依赖关系一目了然：`cc_status` → `cc_simple` → `cc_pulse` → (`cc_bits`, `cc_reset`)。所以本讲会**从最底层的 `cc_pulse` 讲起，逐层向上**。

---

## 4. 核心概念与源码讲解

### 4.1 cc_pulse：单周期脉冲跨越

#### 4.1.1 概念说明

`olo_base_cc_pulse` 解决的问题：**在两个完全异步的时钟域之间，把单周期脉冲一对一地搬过去——不丢、不重**。

它**只搬「事件」，不搬「数据」**。所以它的端口里没有 `Data`，只有一组脉冲位：

```vhdl
In_Pulse  : in  std_logic_vector(NumPulses_g - 1 downto 0);
Out_Pulse : out std_logic_vector(NumPulses_g - 1 downto 0);
```

`NumPulses_g` 路**相互独立**的单 bit 脉冲，每路各自走一套翻转协议。注意文档里强调的一条限制：**只保证所有脉冲都被送达，不保证「同一源周期出现的多个脉冲，在目的域也落在同一周期」**——因为每路独立跨域，到达时刻会错开。所以它只适合传递「各自独立的单脉冲」，不适合用来跨一条「需要多位同时一致」的向量。

约束条件（来自文档）：脉冲频率必须**低于慢时钟频率的一半**，即相邻两个脉冲的间隔至少要 2 个慢时钟周期以上。

#### 4.1.2 核心流程

翻转协议的时序，可以用下面这段伪流程描述（单路脉冲为例）：

```
源域 (In_Clk):
  每来一个 In_Pulse，就把 toggle 信号翻转一次:   toggle ^= In_Pulse
                                                   // toggle 是电平，不是脉冲
[olo_base_cc_bits 同步器] (2~4 级 FF):
  toggle ──────────────────────────────────────► toggle_out   // 电平安全过域

目的域 (Out_Clk):
  Out_Pulse <= toggle_out_prev XOR toggle_out      // 电平每变一次 => 恰好一个脉冲
  toggle_out_prev <= toggle_out                     // 记住上一拍，供下拍异或
```

关键不变量：**toggle 电平每翻转一次，目的域恰好还原出一个单周期脉冲**。由于 toggle 是稳定电平，同步器绝不会漏采；由于 XOR 是「本拍 vs 上一拍」，电平的一次翻转只会产生一个周期的 `'1'`，绝不会拉长。这就是「不丢、不重」的来源。

复位同样要跨域：实体内部例化 `olo_base_cc_reset`，两侧各输出一个 `RstOut`，所有内部寄存器都由跨域后的复位驱动，保证复位**不会**凭空产生假脉冲（这点 u4-l1 已详述）。

#### 4.1.3 源码精读

先看实体的泛型与端口，端口命名延续 u4-l5 的规范（无 `_i/_o` 后缀，复位成对出现 `RstIn`/`RstOut`）：

[olo_base_cc_pulse.vhd:33-48](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_pulse.vhd#L33-L48) — 实体声明。`NumPulses_g` 默认 1，`SyncStages_g` 限定 2~4 默认 2；`In_RstIn`/`Out_RstIn` 都带默认值 `'0'`，可选端口可不连。

源域的翻转产生在这里——一行组合逻辑，把「上一拍的 toggle」与「本拍脉冲」异或，得到「本拍要写入的 toggle」：

[olo_base_cc_pulse.vhd:94-94](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_pulse.vhd#L94-L94) — `ToggleIn <= ToggleLast xor In_Pulse;` 每个脉冲让翻转信号变一次电平。注释特意说明「做成组合逻辑，是因为真正的寄存器放在 `olo_base_cc_bits` 里」。

`ToggleLast` 是这个翻转信号的「上一拍寄存器」，由源域进程维护，复位清零：

[olo_base_cc_pulse.vhd:83-91](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_pulse.vhd#L83-L91) — `p_reg` 进程：`ToggleLast <= ToggleIn;` 并在 `RstInI='1'` 时清零。注意复位写在进程**末尾的覆盖**位置，符合 Open Logic 规范（u4-l5）。

翻转信号随后交给同步器跨域（这里 `NumPulses_g` 位**各自独立**同步，所以多位也安全）：

[olo_base_cc_pulse.vhd:97-109](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_pulse.vhd#L97-L109) — 例化 `olo_base_cc_bits`，宽度为 `NumPulses_g`，把 `ToggleIn` 同步到 `Out_Clk` 域得到 `ToggleOut`。

目的域用 XOR 还原脉冲，并寄存「上一拍」：

[olo_base_cc_pulse.vhd:112-124](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_pulse.vhd#L112-L124) — `Out_Pulse <= ToggleOutLast xor ToggleOut;`（组合输出）+ `p_pulseout` 进程维护 `ToggleOutLast`。这就是「电平变一次 → 恰好一个脉冲」的实现。

复位穿越由这个例化完成，两侧各引出 `RstOut`：

[olo_base_cc_pulse.vhd:69-80](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_pulse.vhd#L69-L80) — 例化 `olo_base_cc_reset`，把 `RstInI`/`RstOutI` 引到实体的 `In_RstOut`/`Out_RstOut`，供周围逻辑挂接。

> 顺带一提：如果你想知道 `olo_base_cc_bits` 内部那串同步 FF 长什么样，可对照 [olo_base_cc_bits.vhd:120-139](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_bits.vhd#L120-L139) 的 `p_outff` 进程——注意它带了一组跨厂商综合属性（`shreg_extract`/`async_reg`/`dont_merge`…），目的是阻止工具把这串同步 FF 抽成移位寄存器或合并掉（u4-l1 已解释）。

#### 4.1.4 代码实践

**实践目标**：用 `cc_pulse` 在两个异步时钟域间传递脉冲，验证「每个源脉冲在目的域恰好产生一个脉冲，无重复无丢失」。

Open Logic 已经为 `cc_pulse` 写好了测试台，我们就用它做实践依据：

**操作步骤**：

1. 进入仿真目录，运行 `cc_pulse` 的测试台（VUnit 默认用 GHDL，见 u1-l4）：

   ```bash
   cd sim
   python3 run.py '*cc_pulse*'
   ```

   （`*cc_pulse*` 是 VUnit 的测试名通配过滤，匹配所有 `olo_base_cc_pulse_tb` 的配置；具体命令行风格以本地 `run.py -h` 为准。）

2. 测试台 [`olo_base_cc_pulse_tb.vhd:152-169`](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/base/olo_base_cc_pulse/olo_base_cc_pulse_tb.vhd#L152-L169) 在 `Normal-Operation` 用例里做的事正是本实践的核心：逐位发一个脉冲 → 用 `wait_for_value_stdlv` 等待 `Out_Pulse` 出现对应位 → 再等两拍确认脉冲被收回。阅读这段，理解「发一个、收一个」的断言。

3. 源码阅读型延伸：对照测试台 [`olo_base_cc_pulse_tb.vhd:27-32`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_cc_pulse/olo_base_cc_pulse_tb.vhd#L27-L32) 的 generic（`ClockRatio_N_g`/`ClockRatio_D_g`/`SyncStages_g`），结合 [`sim/test_configs/olo_base.py:22-34`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L22-L34)，说出测试覆盖了哪些时钟比（如 1:3、3:1、19:20、20:19）和几级同步（2、4）。

**需要观察的现象**：所有 `cc_pulse` 配置全部 PASS；`Normal-Operation` 用例中，单脉冲与多脉冲（`In_Pulse <= "0101"`）都被正确还原。

**预期结果**：每个 `In_Pulse` 的脉冲，`Out_Pulse` 端在若干目的周期后恰好出现一个等宽脉冲；脉冲过去后 `Out_Pulse` 回零。完整仿真结果日志（具体每条用例名）**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把两个脉冲**连续**打在同一个 `In_Pulse` 位上（间隔小于 2 个慢时钟周期），会发生什么？为什么文档要求脉冲频率低于慢时钟的一半？

**参考答案**：翻转协议中，两个靠得太近的脉冲会让 toggle 在被目的域采样到之前就「翻了又翻回来」，净效果是 toggle 没变 → 目的域还原不出脉冲（两个脉冲「抵消」丢失）；或者只翻了一次 → 还原出一个脉冲（少了一个）。所以必须保证相邻脉冲间隔足够大，让每一次翻转都被目的域稳稳采到。

**练习 2**：`NumPulses_g=4` 时，四路脉冲共享同一个 `olo_base_cc_bits` 例化吗？这样安全吗？

**参考答案**：共享——一个 `olo_base_cc_bits` 实例宽度为 4，但内部是**每一位各自独立**的同步 FF 链（见 `cc_bits` 的 `Width_g` 数组实现）。因为每一路都是单 bit 的翻转电平，不存在多 bit 一致性问题，所以安全。这正是它能「多位」却仍无需握手的原因。

---

### 4.2 cc_simple：带 Valid 的采样值跨越

#### 4.2.1 概念说明

`olo_base_cc_simple` 解决的问题：**跨域搬运「带 Valid 的单值采样」**——也就是 AXI4-Stream 风格的、有 `Valid` 但**没有 `Ready`（无反压）**的数据。

它的关键设计判断，正是 2.2 节那条直觉：**数据线不需要同步器，只有 Valid 需要跨域**。原因：当 Valid（经翻转协议）到达目的域时，数据已经在源域被锁存，并稳定了足够多个目的周期。所以它复用 `cc_pulse` 把 Valid 当成单脉冲跨过去，数据则「搭便车」直接接过去。

约束条件（来自文档）：数据率必须低于慢时钟频率的 \(1/(3+\text{SyncStages\_g})\)。也就是说，两次 `In_Valid` 之间至少要隔 \(3+\text{SyncStages\_g}\) 个慢时钟周期。这个余量就是留给「数据在 Valid 到达前稳定下来」的时间。

#### 4.2.2 核心流程

```
源域 (In_Clk):
  if In_Valid='1':  DataLatchIn <= In_Data        // 数据先锁存（保持住）

[olo_base_cc_pulse, NumPulses_g=1]:
  In_Valid ───────────────────────────────────► VldOutI   // Valid 当脉冲跨域

目的域 (Out_Clk):
  Out_Valid <= VldOutI                            // Valid 跟随
  if VldOutI='1': Out_Data <= DataLatchIn         // Valid 到达时，数据早已稳定，直接取
```

注意 `Out_Data` 是**保持型**输出：Valid 拉高那一拍数据被更新，之后数据**保持不变**直到下一次 Valid——不是脉冲式的一闪而过。这与 `cc_pulse` 的脉冲式输出形成对比。

一个常被忽略的细节：`DataLatchIn`（源域锁存）与 `Out_Data_Sig`（目的域锁存）之间的连线路径上**没有同步器**。这条「裸」跨域数据线之所以安全，完全靠「数据已稳定」这个时序前提——也正因为如此，它必须配 `set_max_delay` 约束（见 u4-l1）来告诉工具「我知道这是一条 CDC 路径，别按同域时序去检查」。

#### 4.2.3 源码精读

实体声明，注意它**有 `In_Data`/`Out_Data` 与 `In_Valid`/`Out_Valid`，但没有 Ready**：

[olo_base_cc_simple.vhd:33-50](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_simple.vhd#L33-L50) — 实体声明。`Width_g` 默认 1。

它直接把 Valid 接到 `cc_pulse` 的脉冲输入，把 `cc_pulse` 的输出当作跨域后的 Valid：

[olo_base_cc_simple.vhd:73-87](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_simple.vhd#L73-L87) — 例化 `olo_base_cc_pulse`，`NumPulses_g => 1`，`In_Pulse(0) => In_Valid`、`Out_Pulse(0) => VldOutI`。这就是「Valid 当脉冲跨域」。

源域一侧：只要 Valid 拉高，就把数据锁住：

[olo_base_cc_simple.vhd:93-100](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_simple.vhd#L93-L100) — `p_data_a`：`if In_Valid='1' then DataLatchIn <= In_Data;`。注意这个进程**不复位 `DataLatchIn`**——数据一旦写进就保持，复位由 `cc_pulse` 里的 `cc_reset` 间接保证时序。

目的域一侧：Valid 到达时取走数据，并寄存 `Out_Valid`：

[olo_base_cc_simple.vhd:103-117](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_simple.vhd#L103-L117) — `p_data_b`：`Out_Valid <= VldOutI;`，且 `if VldOutI='1' then Out_Data_Sig <= DataLatchIn;`，复位时只清 `Out_Valid`（不清数据）。

这里有一处工程细节值得留意——`Out_Data_Sig` 上挂了 AMD（Vivado）综合属性：

[olo_base_cc_simple.vhd:66-69](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_simple.vhd#L66-L69) — `attribute dont_touch / keep of Out_Data_Sig`。注释写明「仅用于自动约束、因此仅 Vivado」。它的作用是：阻止 Vivado 把这条跨域数据线优化掉，好让 scoped constraints 能识别出这条 CDC 路径并自动加 `set_max_delay`（u4-l1 提到的 AMD 自动约束）。其他厂商不影响功能，靠你手写约束。

#### 4.2.4 代码实践

**实践目标**：通过阅读与运行，确认「数据不在同步器里、只靠稳定时间过域」这一设计。

**操作步骤**：

1. 运行 `cc_simple` 测试台，确认它跨时钟比矩阵下都能正确搬值：

   ```bash
   cd sim
   python3 run.py '*cc_simple*'
   ```

2. 源码追踪型实践——「画一条数据路径」：在纸上标出 `In_Data` → `DataLatchIn` →（跨域裸线）→ `Out_Data_Sig` → `Out_Data`，并在 `In_Valid` → `cc_pulse` → `VldOutI` → `Out_Valid` 旁标出各自所在的时钟域。确认**数据通路跨越时钟域的那一段没有任何寄存器/同步器**，唯一的「安全保证」是「Valid 到达时数据已稳定」。

3. 反例思考：如果把 `In_Valid` 的频率提高到超过 \(1/(3+\text{SyncStages\_g})\) 的慢时钟比例，会发生什么？（提示：上一次数据还没被目的域取走，新数据就覆盖了 `DataLatchIn`；或 Valid 脉冲靠太近触发 4.1.5 练习 1 的丢失。）

**需要观察的现象**：测试台在不同时钟比、不同 `SyncStages_g` 下，目的域收到的 `(Out_Data, Out_Valid)` 与源域发出的 `(In_Data, In_Valid)` 在「值」上一一对应（时序会延迟，但值不丢不变）。

**预期结果**：所有 `cc_simple` 配置 PASS。完整日志**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`cc_simple` 的 `Out_Data` 是脉冲式输出（一拍就消失）还是保持型输出？从源码哪一行看出来？

**参考答案**：保持型。从 [olo_base_cc_simple.vhd:103-115](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_simple.vhd#L103-L115) 的 `p_data_b` 可见，`Out_Data_Sig` 只在 `VldOutI='1'` 时被更新，其余拍保持原值；只有 `Out_Valid` 是跟随 `VldOutI` 的脉冲式信号。

**练习 2**：为什么 `cc_simple` 不能用来跨「连续高速数据流」？该用什么？

**参考答案**：因为它没有 `Ready`/反压，且数据率受限（\(1/(3+\text{SyncStages\_g})\)）。高速连续流应选 `olo_base_cc_handshake`（有反压、逐拍握手）或 `olo_base_fifo_async`（有缓冲、满吞吐），见选择表。

---

### 4.3 cc_status：缓变状态/配置跨越

#### 4.3.1 概念说明

`olo_base_cc_status` 解决的问题：**跨域搬运「慢变的状态或配置」**，典型场景是把一个时钟域里的 FIFO 填充度、或一个配置寄存器值，搬给另一个时钟域，而且**你不知道也不在乎它具体在哪一拍被采样**。

它的接口最简——**连 Valid 都没有**：

```vhdl
In_Data  : in  std_logic_vector(Width_g - 1 downto 0);
Out_Data : out std_logic_vector(Width_g - 1 downto 0);
```

注意 `Width_g` **没有默认值**（`generic ( Width_g : positive; ...)`），实例化时必须显式给出。

它的语义是「最终一致」：目的域的 `Out_Data` 在某一拍出现的值，**一定等于源域 `In_Data` 在某个历史周期真实存在过的值**。但若源域变化太快，中间的瞬态值会被跳过（采样间隔大于变化间隔时）。所以它的约束更严：数据率要低于慢时钟的 \(1/(6+2\cdot\text{SyncStages\_g})\)。

#### 4.3.2 核心流程

`cc_status` 的精髓是一个**乒乓（req/ack）回环**：它不让用户来产生采样时机，而是**自己**在两个时钟域之间反复传递一个 Valid 脉冲，数据跟着脉冲一起过去。

```
源域 (In_Clk):                          目的域 (Out_Clk):
  p_vldgen:
    复位后第一次:  VldIn <= '1'            ┐
    之后:          VldIn <= VldFb   ◄──────┤  [cc_pulse 回环 Out->In]
                   │                        │
                   ▼                        │
  [cc_simple, in->out]:                     │
    VldIn 携带 In_Data ──────────────────►  Out_Data + VldOut
                                            │
                                            └─ VldOut 作为 ack，经 cc_pulse 送回源域 => VldFb
```

机制要点：

- 复位释放后，`p_vldgen` 立刻发第一个 `VldIn` 脉冲（`Started` 标志保证只「自发」一次）。
- 这个 `VldIn` 经 `cc_simple` 把 `In_Data` 快照送到目的域，产生 `VldOut`。
- `VldOut` 又经一个 `cc_pulse`（方向 **out→in**）回送为 `VldFb`。
- 源域看到 `VldFb`，才发**下一个** `VldIn`：`VldIn <= VldFb`。

于是 Valid 脉冲在两个域之间「一来一回」地循环，每一来回搬运一份当前 `In_Data` 的快照。由于「发下一个前必须收到上一个的 ack」，这天然保证了**恰好一次（exactly-once）**的投递，既不丢也不重。

#### 4.3.3 源码精读

实体声明，`Width_g` 无默认值、无 Valid 端口：

[olo_base_cc_status.vhd:33-48](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_status.vhd#L33-L48) — 实体声明。端口与 `cc_simple` 相比，去掉了 `In_Valid`/`Out_Valid`。

自洽采样时机的产生——`p_vldgen` 进程：

[olo_base_cc_status.vhd:69-88](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_status.vhd#L69-L88) — 两个关键赋值：`VldIn <= VldFb;`（收到回执才发下一个），以及 `if (Started = '0') then VldIn <= '1'; Started <= '1';`（复位后自发第一个脉冲）。复位时把 `RstOutI_Sync` 置全 1、`Started`/`VldIn` 清零。

前向通路（in→out）复用 `cc_simple`，把 `VldIn` 当作它的 Valid：

[olo_base_cc_status.vhd:91-107](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_status.vhd#L91-L107) — 例化 `olo_base_cc_simple`，`In_Valid => VldIn`、`Out_Valid => VldOut`。数据与 Valid 经此到达目的域。

反向 ack 通路（out→in）复用 `cc_pulse`：

[olo_base_cc_status.vhd:113-125](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_status.vhd#L113-L125) — 例化 `olo_base_cc_pulse`，注意方向**反过来**：`In_Clk => Out_Clk`、`Out_Clk => In_Clk`，把目的域的 `VldOut` 当脉冲送回源域得到 `VldFb`。这就构成了「一来一回」的回环。

> 读到这里你会发现一个优美的分层：`cc_status` 自己几乎不写时序逻辑，只靠「`cc_simple`（前向）+ `cc_pulse`（反向）+ 一个 `p_vldgen` 调度」就拼出了自洽采样。这正是 Open Logic「一个实体只做一件事、用组合复用」哲学（u1-l1）的体现。

#### 4.3.4 代码实践

**实践目标**：验证 `cc_status` 在两侧时钟频率差异大、且源数据缓慢变化时，目的域能稳定跟随。

**操作步骤**：

1. 运行 `cc_status` 测试台：

   ```bash
   cd sim
   python3 run.py '*cc_status*'
   ```

2. 源码阅读型实践——「数延迟」：从 [olo_base_cc_status.vhd:69-125](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_status.vhd#L69-L125) 出发，估算一次「源数据变化 → 目的域看到新值」的最少来回周期数，解释文档里 \(6+2\cdot\text{SyncStages\_g}\) 这个因子的来源（前向 `cc_simple` 占 \(3+\text{SyncStages\_g}\)，反向 `cc_pulse` 占若干拍，再加同步余量）。

3. 想象一个真实用法：把某 `olo_base_fifo_async` 的 `Occupancy`（填充度）从写时钟域搬到读时钟域做监控。说明为什么用 `cc_status`（而非 `cc_simple`）更省心——你不需要在写侧生成 Valid，实体自己会周期采样。

**需要观察的现象**：当源侧 `In_Data` 缓慢递增时，目的侧 `Out_Data` 会**滞后但单调地**跟随；瞬态的快速抖动会被跳过。

**预期结果**：所有 `cc_status` 配置 PASS；目的域最终收敛到源域的值。完整日志**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`cc_status` 为什么不暴露 Valid 端口？它的「采样时刻」由谁决定？

**参考答案**：因为它是为「无固定采样率的状态/配置」设计的，用户无需也不应指定采样时机。采样时刻由实体内部的 `p_vldgen` + req/ack 回环自洽产生，对用户未知。这也是它「不保证显示瞬态」的根源。

**练习 2**：把 `cc_status` 的反向 `cc_pulse` 回环（`i_bcc`）删掉、把 `VldIn` 改成恒 `'1'`，会出什么问题？

**参考答案**：`VldIn` 恒高会让前向 `cc_simple` 里的 `cc_pulse` 不断尝试翻转一个已经是连续脉冲的信号——违反「脉冲频率低于慢时钟一半」的前提，导致 Valid 跨域丢失/错乱，目的域取到的是随机时刻的、可能正在变化中的数据（撕裂值），失去「恰好一次、值一致」的保证。回环的作用正是把连续的「想采样」节流成离散的、确认投递的采样事件。

---

### 4.4 适用场景对比

#### 4.4.1 概念说明

三个实体都「简单、无 RAM」，但跨的东西不同。选错实体是 CDC 最常见的实现错误，所以 Open Logic 在 `doc/base/clock_crossing_principles.md` 里给了一张**选择表（Selection Table）**，本节把它聚焦到这三个实体上做对比。

#### 4.4.2 核心流程：按数据特性选型

用下面这张「决策树」来选：

| 你要跨的是… | 关键特征 | 选哪个 | 理由 |
| :--- | :--- | :--- | :--- |
| 单周期**事件**（如「启动一次转换」「清中断」） | 无数据，只要事件不丢 | **`cc_pulse`** | 只跨事件，最省；`NumPulses_g` 可一次跨多路独立脉冲 |
| 带时间点的**采样值**（如 ADC 偶发采样、跨域单个读数） | 有 Data + Valid，无反压，低更新率 | **`cc_simple`** | 数据搭 Valid 的便车过域，无需 RAM |
| 缓变**状态/配置**（如填充度、寄存器配置） | 不知道何时变、不在乎何时采 | **`cc_status`** | 自洽采样，无需外部 Valid |

对比官方选择表（节选自 [`clock_crossing_principles.md:60-69`](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/doc/base/clock_crossing_principles.md#L60-L69)）里这三行的能力位：

| 能力位（✓=支持） | cc_pulse | cc_simple | cc_status |
| :--- | :---: | :---: | :---: |
| Async. Clocks（全异步时钟） | ✓ | ✓ | ✓ |
| Data（搬数据） | — | ✓ | ✓ |
| Multi Bits（多位安全） | — | ✓ | ✓ |
| Valid (Sampled)（带 Valid） | ✓ | ✓ | — |
| Ready（反压） | — | — | — |
| Reset Crossing（复位穿越） | ✓ | ✓ | ✓ |
| 100% Perf.（满吞吐） | ✓ | — | — |
| No RAM（无 RAM） | ✓ | ✓ | ✓ |

读这张表的要点：

- `cc_pulse` 是唯一**不搬数据**的——它的「Data」位是空的。
- 只有 `cc_pulse` 标了 **100% Perf.**：因为它不会因等待而插入空闲周期（前提是你满足脉冲间隔约束）。`cc_simple`/`cc_status` 都有节流，故非满吞吐。
- 三者都**无 RAM、无 Ready**。一旦你需要反压或缓冲，就必须升级到 `cc_handshake` 或 `fifo_async`（那是 u4-l3 与 u3-l1 的主题）。

#### 4.4.3 源码精读

选择表本身就在文档里，对照原文逐行确认：

[clock_crossing_principles.md:55-89](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/doc/base/clock_crossing_principles.md#L55-L89) — 完整选择表及各列含义说明（Async. Clocks / Data / Multi Bits / Valid / Ready / Reset Crossing / 100% Perf. / No RAM）。

把这张表和 4.1~4.3 的源码对应起来看，会发现表里的每个「✓」都能在源码里找到落点：例如 `cc_simple` 的「Multi Bits ✓」来自 [olo_base_cc_simple.vhd:93-117](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_simple.vhd#L93-L117) 的数据锁存（而非逐位同步），「Reset Crossing ✓」来自三个实体内部共有的 [`olo_base_cc_reset`](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_reset.vhd) 例化。

#### 4.4.4 代码实践

**实践目标**：为三段真实需求，各选一个最省资源的实体，并写出对应的时序约束。

**操作步骤**：

1. 阅读三个需求并选型：
   - **需求 A**：把「按一下按钮产生的一次『开始采集』脉冲」从 100 MHz 域送到 50 MHz 域。
   - **需求 B**：把一个 100 MHz 域里**偶发**的 ADC 单点采样（带 `Valid`，约每 1 µs 一次）送到 156.25 MHz 域。
   - **需求 C**：把 200 MHz 域里某 FIFO 的 12 bit 占用率（缓变）送到 50 MHz 域做 LED 显示。

2. 写约束：对每个选定的实体，按 u4-l1 的模板写一对 `set_max_delay -datapath_only <较快时钟周期>`（双向）。

3. 反思：为什么需求 B 不能用 `cc_status`（即便它也能搬数据）？为什么需求 C 用 `cc_simple` 会更麻烦？

**需要观察的现象/预期结果**：

- A → `cc_pulse`（纯事件，最省）。
- B → `cc_simple`（有 Valid、无反压、低更新率，正好匹配；用 `cc_status` 会丢掉「这个点是什么时候采的」时间信息）。
- C → `cc_status`（缓变状态、不关心采样时刻；用 `cc_simple` 则你必须在 200 MHz 侧自己造一个周期性 Valid，多此一举）。

约束 TCL（示例，需替换 `<src-clock>`/`<dst-clock>`/较快时钟周期值，**待确认**实际工程中的时钟名）：

```tcl
# 通用 CDC 约束模板（u4-l1），双向都要写
set_max_delay -from [get_clocks <src-clock>] -to [get_clocks <dst-clock>] -datapath_only <较快时钟周期ns>
set_max_delay -from [get_clocks <dst-clock>] -to [get_clocks <src-clock>] -datapath_only <较快时钟周期ns>
```

> 在 Vivado（AMD）里，只要用 `import_sources.tcl` 导入 Open Logic，scoped constraints 会自动为这三个实体加上述约束，无需手写；其他厂商必须手写。

#### 4.4.5 小练习与答案

**练习 1**：你需要跨一条「8 bit 配置寄存器」，配置只在系统启动时写一次、之后基本不变。三个实体里选哪个？为什么不用 `cc_pulse`？

**参考答案**：选 `cc_status`。配置是缓变状态，不关心采样时刻，`cc_status` 自洽采样最省心。不用 `cc_pulse` 是因为它**不搬数据**——`cc_pulse` 只能跨事件，8 bit 的寄存器值没法走它（`cc_pulse` 没有 Data 端口，且即便用 8 路脉冲也是 8 个独立事件，不是一致的 8 位向量）。

**练习 2**：从选择表看，`cc_simple` 同时具备「Data ✓」和「Valid ✓」，那它和 `cc_handshake` 的差别核心在哪一列？

**参考答案**：核心在 **Ready（反压）** 列——`cc_simple` 没有，`cc_handshake` 有。这决定了前者只能用于低更新率、无反压场景，后者可用于连续流。

---

## 5. 综合实践

设计一个小型「三类信号混合跨域」模块，把本讲三个实体串起来用，巩固选型直觉。

**场景**：你有一个运行在 `Clk_Fast`（200 MHz）的数据采集子系统，需要把三类信号送到 `Clk_Slow`（50 MHz）的控制子系统：

| 信号 | 类型 | 选用的实体 |
| :--- | :--- | :--- |
| `TriggerStart` | 单周期启动脉冲（事件） | `olo_base_cc_pulse` |
| `LastSample` + `LastSample_Valid` | 偶发采样值（约每 2 µs 一点） | `olo_base_cc_simple` |
| `BufLevel[11:0]` | FIFO 占用率（缓变） | `olo_base_cc_status` |

**任务**：

1. **画方框图**：画出 `Clk_Fast` 域与 `Clk_Slow` 域，把三个实体摆在中间，标清每个实体的 `In_*`/`Out_*`、`RstIn`/`RstOut` 接法。特别注意：三个实体各自的 `RstOut`（两侧都要）应驱动各自周围的逻辑（u4-l1 规则）。
2. **写实例化代码（示例代码，非项目原有）**：参照本讲引用的源码端口，分别例化三个实体，给出 `NumPulses_g`、`Width_g`、`SyncStages_g` 的取值。注意 `cc_status` 的 `Width_g => 12` 必须显式给。
3. **核对约束**：确认三条跨域路径都已配 `set_max_delay`（Vivado 用 scoped constraints 自动覆盖，其他工具手写）。
4. **仿真验证**：写一个简单的 testbench（两个不同频率时钟），分别注入：一个 `TriggerStart` 脉冲、一次带 Valid 的采样、一次 `BufLevel` 变化，观察 `Clk_Slow` 域三类输出是否各自正确到达。
   - 期望：脉冲被还原成恰好一个脉冲；采样值在 Valid 到达时出现并保持；占用率滞后但单调跟随。具体波形**待本地验证**。

**验收标准**：能用一句话说清「为什么这三类信号各选了这个实体、而没用更重的 `cc_handshake`/`fifo_async`」——因为它们都满足「无反压、低更新率、无 RAM」的前提。

## 6. 本讲小结

- 三个实体共享同一地基——**翻转（toggle）协议**：源域脉冲翻转一个电平，同步器安全过域，目的域用「本拍 XOR 上一拍」还原出恰好一个脉冲，做到不丢不重。
- **`cc_pulse`** 只跨「事件」，`NumPulses_g` 路独立单 bit；是三者中最省、唯一标 100% Perf. 的，但要求脉冲频率低于慢时钟的一半。
- **`cc_simple`** 跨「带 Valid 的采样值」：复用 `cc_pulse` 跨 Valid，**数据不进同步器、只靠稳定时间过域**，故无反压、更新率须低于慢时钟的 \(1/(3+\text{SyncStages\_g})\)。
- **`cc_status`** 跨「缓变状态/配置」：用「`cc_simple` 前向 + `cc_pulse` 反向」构成 req/ack 回环，**自洽产生采样时机**，恰好一次投递；约束最严（\(1/(6+2\cdot\text{SyncStages\_g})\)）。
- 三者都**无 RAM、无 Ready、含复位穿越**；一旦需要反压或缓冲，升级到 `cc_handshake` 或 `fifo_async`。
- 选型看「数据特性」：事件 → `cc_pulse`，低频采样值 → `cc_simple`，缓变状态 → `cc_status`；官方选择表是最权威的速查表。

## 7. 下一步学习建议

- 下一讲 [u4-l3 握手与相位对齐跨时钟域](u4-l3-handshake-phase-aligned-crossings.md) 会把跨域推进到「**有反压**」的场景：`olo_base_cc_handshake` 用标准 Valid/Ready 跨异步时钟域，`olo_base_cc_n2xn`/`cc_xn2n` 在**相位对齐**的整数倍时钟间做低成本跨越。学完后你会补齐选择表里「Ready ✓」那一列。
- 若你已先接触过 [u3-l1 异步 FIFO](u3-l1-async-fifo.md)，可对比体会：本讲的三个实体都是「无 RAM」的轻量跨越，而 `fifo_async` 用双时钟 RAM + 格雷码指针换来**满吞吐 + 缓冲**——是「重炮」与「轻步兵」的取舍。
- 想深入理解翻转协议的安全边界，建议阅读 [olo_base_cc_pulse.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_cc_pulse.md) 文档中的时序波形图，并对照本讲的测试台 [`olo_base_cc_pulse_tb.vhd`](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/base/olo_base_cc_pulse/olo_base_cc_pulse_tb.vhd) 跑一遍 `Normal-Operation` 用例。
