# 多级流水线 multi_pl_stage

## 1. 本讲目标

本讲在 u7-l1（`pl_stage` 单级流水线与二进程 record 设计法）的基础上，讲解 psi_common 如何把多个 `pl_stage` 串成一条**多级流水线** `psi_common_multi_pl_stage`。学完本讲你应当能够：

- 说清 `multi_pl_stage` 是「`pl_stage` 的级联包装器」，并画出 N 级链的数据/握手走向。
- 掌握 `stages_g` 这个 generic 的取值范围（`natural`，**包含 0**）以及 `stages_g = 0` 时的退化行为。
- 解释 AXI-S 握手（VLD/RDY）与反压如何沿多级链逐级向前传递，且不会丢数据、不会乱序。
- 估算多级流水线的延迟周期数与触发器资源占用，从而在「打几级、要不要 `use_rdy_g`」之间做权衡。
- 实例化一个 4 级 `multi_pl_stage` 并预测连续数据下的输出延迟。

## 2. 前置知识

阅读本讲前，请确认你已经掌握以下概念（均来自前置讲义）：

- **AXI-S 握手（VLD/RDY）**：传输只在 `vld_i` 与 `rdy_i` 同为高的那一拍发生；源端自主拉 VLD、宿端可随时进出 RDY（反压）。见 u1-l4。
- **`pl_stage` 单级流水线**：一个带 AXI-S 握手的寄存器级，用二进程 record 法实现，`use_rdy_g` 在编译期切两套实现；带反压时用**影子寄存器 `DataShad`** 吸收因「把 `rdy_o` 寄存一拍」而多出来的一个字。见 u7-l1。
- **可综合性与综合属性**：RAM/寄存器资源、`if generate` / `for generate` 在编译期展开。见 u1-l4、u3-l1。

一句话回顾 `pl_stage` 的关键时序：在无反压的连续流下，每个 `pl_stage` 把数据延迟恰好 **1 个时钟周期**；这是本讲推算多级延迟的基石。

## 3. 本讲源码地图

| 文件 | 作用 |
|:-----|:-----|
| [hdl/psi_common_multi_pl_stage.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_multi_pl_stage.vhd) | 本讲主角。用 `for generate` 把 `pl_stage` 串成 N 级，外露统一的 AXI-S 端口。 |
| [hdl/psi_common_pl_stage.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd) | 被级联的单级流水线。本讲只复用 u7-l1 的结论，重点看它的端口如何对接。 |
| [testbench/psi_common_multi_pl_stage_tb/psi_common_multi_pl_stage_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_multi_pl_stage_tb/psi_common_multi_pl_stage_tb.vhd) | 自校验测试平台，用 `stages_g => 20` 覆盖单点、流式、反压、valid 不等 ready 四类场景。 |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl) | 把该 TB 以 `handle_rdy_g=true/false` 两组 generic 登记进回归。 |

补充：本讲提到的 AXI 版多级流水线 `psi_common_axi_multi_pl_stage`（见 u9-l4）是把同样的「级联 + for generate」思路套到 AXI 五通道上，留到接口单元细讲。

## 4. 核心概念与源码讲解

### 4.1 级联架构：把 pl_stage 串成链

#### 4.1.1 概念说明

`multi_pl_stage` 要解决的问题很朴素：**我需要在一条数据路径上打 N 级寄存器，并且 N 是 generic**。

直接手写 N 个 `pl_stage` 实例既啰嗦又无法随 generic 变化。`multi_pl_stage` 的做法是把这件事变成一个**编译期展开的循环**：用 VHDL 的 `for generate` 在综合时实例化 `stages_g` 个 `pl_stage`，相邻两级之间用内部信号首尾相连，对外只暴露一组统一的 AXI-S 端口（`vld_i/rdy_o/dat_i` 与 `vld_o/rdy_i/dat_o`）。

换句话说，`multi_pl_stage` 自身**几乎不含时序逻辑**——它是一个纯结构（structural）的「包装器」，所有握手与寄存器行为都委托给 `pl_stage`。理解了 u7-l1 的单级，就理解了这条链的全部行为。

#### 4.1.2 核心流程

N 级链的拓扑（N = `stages_g`）：

```
 dat_i/vld_i                                dat_o/vld_o
   │                                          ▲
   ▼                                          │
┌──────┐ dat/vld ┌──────┐ dat/vld      ┌──────┐
│stage0├────────►│stage1├────────► ... ├─stage(N-1)┤
└──────┘ ◄───────└──────┘ ◄─────── ... └──────┘
   ▲ rdy_o        ▲                          │ rdy_i
   │              └── rdy 向上游逐级回传 ─────┘
 rdy_o (对上游)                              (来自下游)
```

要点：

- **数据 / valid 向下游流**：每级的 `vld_o/dat_o` 接下一级的 `vld_i/dat_i`。
- **ready 向上游流**：每级的 `rdy_o` 接上一级的 `rdy_i`；最末级的 `rdy_i` 接外部消费者（`rdy_i`），最前级的 `rdy_o` 接外部生产者。
- 这正是 AXI-S 标准的「skid-buffer 链」结构：每一级都把 ready 也寄存一拍，从而**打断 ready 上的长组合链**。

#### 4.1.3 源码精读

先看实体声明，注意四个 generic 的类型：

[psi_common_multi_pl_stage.vhd:20-33](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_multi_pl_stage.vhd#L20-L33) — 实体声明。中文说明：`width_g`（数据位宽，`positive`）、`use_rdy_g`（是否处理反压，`boolean`）、`stages_g`（流水级数，**`natural`，可为 0**）、`rst_pol_g`（复位极性）。端口与 `pl_stage` 完全一致，只是多了一个 `stages_g`。

再看架构体里「内部信号」如何为链预留接点：

[psi_common_multi_pl_stage.vhd:38-41](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_multi_pl_stage.vhd#L38-L41) — 中文说明：声明三类内部信号 `data_s/vld_s/rdy_s`，下标范围都是 `0 to stages_g`，即 **`stages_g + 1` 个接点**。下标 `0` 代表「链的输入端」，下标 `stages_g` 代表「链的输出端」；中间每相邻两个接点之间夹一个 `pl_stage` 实例。`Data_t` 是元素位宽钉死为 `width_g` 的无约束数组（回顾 u2-l3 的数组类型用法）。

接着是真正的级联循环（`stages_g > 0` 分支）：

[psi_common_multi_pl_stage.vhd:45-72](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_multi_pl_stage.vhd#L45-L72) — 中文说明：

- 第 46–48 行：把外部输入接到下标 `0`（`vld_s(0)<=vld_i`、`data_s(0)<=dat_i`），并把下标 `0` 的 ready 引出为 `rdy_o`。
- 第 50–67 行：`for i in 0 to stages_g-1 generate` 实例化第 `i` 个 `pl_stage`。端口映射遵循「本级的输入用下标 `i`，本级的输出用下标 `i+1`」——`vld_i=>vld_s(i)`、`vld_o=>vld_s(i+1)`、`rdy_i=>rdy_s(i+1)`、`rdy_o=>rdy_s(i)`。这样第 `i` 级的输出正是第 `i+1` 级的输入，ready 则反向回传。
- 第 69–71 行：把下标 `stages_g` 引出为外部输出（`vld_o<=vld_s(stages_g)`、`dat_o<=data_s(stages_g)`），并把外部 `rdy_i` 注入下标 `stages_g`。

被实例化的 `pl_stage` 端口可对照 [psi_common_pl_stage.vhd:22-34](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L22-L34)，端口名一一对应，确认链的接线无误。

#### 4.1.4 代码实践

**源码阅读型实践：画出 4 级链的接线图。**

1. 实践目标：通过阅读 `for generate` 体，手工展开 `stages_g = 4` 时的实例与连线，验证「N 级 = N 个 `pl_stage`」。
2. 操作步骤：
   - 令 `stages_g = 4`，则内部信号下标为 `0 to 4`（共 5 个接点）。
   - 循环 `i` 取 `0,1,2,3`，共实例化 **4 个** `pl_stage`：`i_stg(0)..i_stg(3)`。
   - 逐个写出每个实例的端口连接，例如 `i_stg(0)`：`vld_i=>vld_s(0)`、`rdy_o=>rdy_s(0)`、`dat_i=>data_s(0)`、`vld_o=>vld_s(1)`、`rdy_i=>rdy_s(1)`、`dat_o=>data_s(1)`。
3. 需要观察的现象：第 `i` 级输出与第 `i+1` 级输入共用同一个下标信号，因此相邻级天然首尾相连。
4. 预期结果：4 个 `pl_stage` 串联，输入端 `vld_s(0)/data_s(0)` 来自 `vld_i/dat_i`，输出端 `vld_s(4)/data_s(4)` 送往 `vld_o/dat_o`；ready 反向贯通。
5. 待本地验证：可选——把展开结果与综合后的原理图（synthesis schematic）对照，确认实例数等于 `stages_g`。

#### 4.1.5 小练习与答案

**练习 1**：若 `stages_g = 1`，`for generate` 会实例化几个 `pl_stage`？内部信号有几个下标？
> **答**：实例化 1 个（`i` 只取 0）；内部信号下标为 `0 to 1`，共 2 个接点（输入端 0、输出端 1）。所以 `stages_g = 1` 的 `multi_pl_stage` 等价于单个 `pl_stage`。

**练习 2**：为什么内部信号下标范围是 `0 to stages_g`（`stages_g+1` 个），而不是 `0 to stages_g-1`（`stages_g` 个）？
> **答**：因为「接点」比「级」多一个——N 个 `pl_stage` 串联需要 N+1 个连接点（输入侧、N-1 个中间级间、输出侧）。代码用下标 `i` 作本级输入、下标 `i+1` 作本级输出，恰好覆盖 `0..stages_g`。

### 4.2 stages_g 参数与 0 级特例

#### 4.2.1 概念说明

`stages_g` 的类型是 `natural`（自然数，**包含 0**），而不是 `positive`。这不是疏忽，而是一个刻意的设计点：它允许把流水线深度配置为 **0**，使组件退化为**纯组合直通**。

为什么要支持 0 级？在 generic 化的工程里，流水线深度常常是从外部配置（比如时序报告驱动、或寄存器映射）算出来的一个参数。当代码写 `stages_g => ComputedDepth` 时，`ComputedDepth` 在某些配置下可能就是 0。如果组件不支持 0，设计者就得在外面再套一层 `if generate` 来旁路它；支持 0 则把这份繁琐吸收进了组件内部。

对比：同库的 [psi_common_axi_multi_pl_stage.vhd:19-24](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_multi_pl_stage.vhd#L19-L24) 把 `stages_g` 声明为 `positive`（≥1），不允许 0 级——这是两兄弟组件一个微妙却真实的差异。

#### 4.2.2 核心流程

组件用两个互斥的 `if generate` 分支处理深度：

```
if stages_g > 0:  实例化 stages_g 个 pl_stage（4.1 节的链）
if stages_g = 0:  纯直通——vld_o<=vld_i, dat_o<=dat_i, rdy_o<=rdy_i
```

`stages_g = 0` 时：

- **延迟 = 0 拍**（组合直通，不引入寄存器）。
- 握手依然成立：`rdy_o` 直接接 `rdy_i`，等价于「上下游直接对接」。
- **不消耗任何触发器**（除可能的布线外）。

#### 4.2.3 源码精读

`g_zero` 分支只有三行，却撑起了「深度可配为 0」的能力：

[psi_common_multi_pl_stage.vhd:74-78](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_multi_pl_stage.vhd#L74-L78) — 中文说明：当 `stages_g = 0` 时，三个输出直接由输入驱动——`vld_o<=vld_i`、`dat_o<=dat_i`、`rdy_o<=rdy_i`。没有任何寄存器，是一个完全透明的组合旁路。它与第 45 行的 `g_nonzero`（`stages_g > 0`）互斥，二者必有其一被综合选中。

注意 `g_nonzero` 用的是 `stages_g > 0`、`g_zero` 用的是 `stages_g = 0`，两者合起来穷尽了 `natural` 的全部取值，所以综合器永远不会遇到「两个分支都不命中」的空结构报错。

#### 4.2.4 代码实践

**改参数观察行为：对比 `stages_g = 0` 与 `stages_g = 4`。**

1. 实践目标：理解 `stages_g` 对延迟与资源的影响。
2. 操作步骤：
   - 在心里（或新建一个顶层）实例化两个 `multi_pl_stage`：一个 `stages_g => 0`，一个 `stages_g => 4`，其余 generic 相同（如 `width_g => 16, use_rdy_g => true`）。
   - 对 `stages_g => 0` 的实例：在 `vld_i` 拉高的同一拍，`vld_o` 应当也为高（组合直通）。
   - 对 `stages_g => 4` 的实例：在 `vld_i` 拉高的那一拍，`vld_o` 仍为低，要等若干拍后才出现该数据。
3. 需要观察的现象：`stages_g = 0` 时输入输出波形完全对齐；`stages_g = 4` 时输出滞后若干拍（具体拍数见 4.3 节推算）。
4. 预期结果：`stages_g = 0` 实例综合后**零触发器**（仅组合连线）；`stages_g = 4` 实例综合后含 4 个 `pl_stage`。
5. 待本地验证：用 Vivado/Quartus 综合两个实例，对照资源报告中的 FF 数量。

#### 4.2.5 小练习与答案

**练习 1**：把 `stages_g` 从 `natural` 改成 `positive` 会破坏什么用法？
> **答**：会禁止 `stages_g => 0`，从而失去「纯组合直通」的退化能力。调用方一旦把深度算成 0（例如某配置下不需要打拍），就必须在 `multi_pl_stage` 外面再套一层 `if generate` 来旁路，增加了上层代码的复杂度。

**练习 2**：`stages_g = 0` 时，`for i in 0 to stages_g-1 generate` 的循环范围是什么？会被实例化几个 `pl_stage`？
> **答**：范围是 `0 to -1`，是空范围（VHDL 允许递减为空的 for-loop），实例化 0 个。不过源码用 `g_nonzero`/`g_zero` 两个 `if generate` 互斥分支，在 `stages_g = 0` 时根本不会进入含 `for generate` 的分支，所以这个空循环实际上不会被综合器看到。

### 4.3 握手与反压在多级中的传递

#### 4.3.1 概念说明

`multi_pl_stage` 最核心的价值不是「打 N 拍延迟」——单纯打延迟用一串普通寄存器就行——而是**在打 N 拍的同时，完整保留 AXI-S 握手与反压能力，且每一级的 ready 都被寄存**。

这一点在 u7-l1 里已经讲过单级动机：ready 信号常常被组合地向前回传，一条长路径上会累积很深的组合逻辑，导致时序不收敛。`pl_stage` 通过把 `rdy_o` 寄存一拍来打断这条链；`multi_pl_stage` 把多个 `pl_stage` 串联，等于在 ready 路径上**每级都插一个寄存器断点**，N 级链的 ready 路径深度就被切成了 N 段短逻辑。这正是组件头注释里强调的「all signals are registered in both directions (including RDY)」。

反压（back-pressure）的传递方向与数据相反：当下游消费者拉低 `rdy_i`，末级 `pl_stage` 先停下输出、用影子寄存器暂存多出来的一个字并拉低自己的 `rdy_o`；这个 `rdy_o` 又是上一级的 `rdy_i`，于是反压逐级向前传播，直到最前级拉低对外 `rdy_o`，告诉生产者「别再送了」。整条链在任何时刻都**不丢数据、不乱序**。

#### 4.3.2 核心流程

无反压的连续流（`rdy_i` 恒为 1）下，每个数据字穿过 N 级需要 N 拍：

```
拍号:    T     T+1    T+2    T+3    T+4
vld_i:   D0▲   D1▲    D2▲    D3▲    ...
stage0:  ←D0   ←D1    ←D2    ←D3
stage1:        ←D0    ←D1    ←D2
stage2:               ←D0    ←D1
stage3:                      ←D0
vld_o:                       D0▲   D1▲ ...  (滞后 stages_g=4 拍)
```

故稳态下：

\[ \text{latency} = \text{stages\_g} \quad \text{（时钟周期，无反压时）} \]

反压场景下，每级都可能临时停下，但数据不会丢——这一点由 `pl_stage` 的影子寄存器保证（u7-l1 已详述）。反压传播有「最多滞后 1 拍到达上游」的固有特性（因为 `rdy_o` 被寄存），影子寄存器正是为此而存在。

#### 4.3.3 源码精读

反压传播的「接线」全部体现在端口映射的方向上，值得再读一遍 ready 的连接：

[psi_common_multi_pl_stage.vhd:57-66](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_multi_pl_stage.vhd#L57-L66) — 中文说明：注意 `rdy_i => rdy_s(i+1)`（本级的 ready 输入来自下游接点）与 `rdy_o => rdy_s(i)`（本级的 ready 输出送往上游接点）。这使 ready 沿 `stages_g → stages_g-1 → … → 0` 反向流动。末级 `rdy_s(stages_g) <= rdy_i`（第 70 行）注入消费者反压；首级 `rdy_o <= rdy_s(0)`（第 47 行）输出给生产者。

每一级的 `rdy_o` 都是 `pl_stage` 内部**寄存过**的信号（见 u7-l1：`rdy_o <= r.rdy_o`，在 `p_seq` 里打拍）。所以 N 级链的 ready 路径被切成了 N 段，每段只跨一个 `pl_stage` 的组合深度——这是组件存在的根本理由。

测试平台用 `stages_g => 20` 的深链来压力测试这条反压通路：

[psi_common_multi_pl_stage_tb.vhd:165-188](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_multi_pl_stage_tb/psi_common_multi_pl_stage_tb.vhd#L165-L188) — 中文说明：`testcase 2`（Back Pressure）用嵌套循环 `inDel(3..0) × outDel(0..3) × val(1..80)` 制造输入/输出两侧各种节奏的停顿，向 20 级链持续施加与释放反压，再在 `p_check` 里用 `StdlvCompareInt(val, dat_o, "Wrong Data")` 逐字核对，验证整条链在反复反压下既不丢字也不乱序。

更严苛的是 `testcase 3`（Valid does not wait for Ready），它把消费者 `rdy_sti` 长期拉低，让 20 级链从输出端一路填满直到输入端：

[psi_common_multi_pl_stage_tb.vhd:265-289](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_multi_pl_stage_tb/psi_common_multi_pl_stage_tb.vhd#L265-L289) — 中文说明：在 `rdy_sti='0'` 期间断言 `vld_o` 一旦拉高就**保持不变**（`assert vld_o = '1'`），证明末级 `pl_stage` 用主寄存器稳稳持住数据；之后周期性放出一个 ready 拍，逐字消费并核验数据正确。这覆盖了「链被填满」的极端反压场景。

#### 4.3.4 代码实践

**运行型实践：跑回归 TB，观察 20 级链的延迟与反压。**

1. 实践目标：用现成 TB 直观看到 `stages_g` 决定的延迟与反压下的数据完整性。
2. 操作步骤：
   - 按 u1-l3 描述的 PsiSim/Modelsim 流程，在 `sim/` 下运行回归。该 TB 已在 [sim/config.tcl:325-328](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L325-L328) 以 `handle_rdy_g=true` 与 `handle_rdy_g=false` 两组 generic 登记。
   - 在波形窗口聚焦 `testcase 1`（Streaming）：观察从 `vld_i` 首次拉高到 `vld_o` 首次拉高之间相隔的时钟周期数。
3. 需要观察的现象：`testcase 1` 中第一个数据字 `0` 从输入到输出滞后约 **20 个时钟周期**（因 DUT 实例化时 `stages_g => 20`，见 [TB 第 72 行](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_multi_pl_stage_tb/psi_common_multi_pl_stage_tb.vhd#L72)）；`testcase 2/3` 中即使反复反压，`p_check` 侧的 `StdlvCompareInt` 不报 `###ERROR###`。
4. 预期结果：延迟周期数 = `stages_g`；反压下数据序列 1..80（及 1..40）原样到达，顺序与数值均正确。
5. 待本地验证：若手头没有仿真器，可改为「源码阅读型」——跟随 `p_stim` 与 `p_check` 的 `done` 握手，说明两个进程如何通过 `done`/`testcase` 信号同步发数与校验（多进程协调，回顾 u11-l1）。

#### 4.3.5 小练习与答案

**练习 1**：为什么说 `multi_pl_stage` 能「打断 ready 上的长组合链」？请结合端口映射解释。
> **答**：因为每级 `pl_stage` 的 `rdy_o` 都是内部寄存过的信号，且 `rdy_o` 作为上一级的 `rdy_i`。于是从末级 `rdy_i` 到首级 `rdy_o` 的 ready 路径被 N 个寄存器断点切成 N 段，每段只跨一个 `pl_stage` 的组合逻辑深度，而不是一整条贯穿 N 级的组合路径。

**练习 2**：在 `testcase 3` 里，消费者长期不取数。20 级链最多能「吞下」几个字后才不得不向生产者反压？
> **答**：每个带 `use_rdy_g=true` 的 `pl_stage` 有一个主寄存器和一个影子寄存器，共可暂存约 2 个字。20 级链在全部填满前可容纳约 `2 × 20 = 40` 个字；填满后首级 `rdy_o` 拉低，向生产者反压。TB 里 `val` 循环到 40 正好与此量级吻合（具体边界取决于握手时序细节，待本地验证精确值）。

### 4.4 资源权衡：影子寄存器与深度代价

#### 4.4.1 概念说明

「打几级」和「要不要 `use_rdy_g`」是两个会直接换成硅片面积的决定。`multi_pl_stage` 把选择权留给设计者，但它本身不省资源——每一级都实打实占用触发器。

核心权衡点在于 `use_rdy_g`：

- **`use_rdy_g = false`**：每级就是一个朴素寄存器，只寄存 `dat_o` 与 `vld_o`。代价小，但**完全不处理反压**（`rdy_i` 不接，见 `pl_stage` 的 `g_nrdy` 分支）——下游一旦不 ready，数据就会被覆盖丢失。
- **`use_rdy_g = true`**：每级除主寄存器外，还要一个**影子寄存器** `DataShad` 来吸收反压，且 `rdy_o` 也占一个寄存器。数据寄存器数量约翻倍，但换来完整的反压能力与 ready 路径的寄存断点。

#### 4.4.2 核心流程

粗略估算单级 `pl_stage` 的触发器数量（回顾 u7-l1 的 record `tp_r`）：

| 模式 | 数据 FF | 控制/握手 FF | 单级合计 |
|:-----|:--------|:-------------|:---------|
| `use_rdy_g = false` | \(width\_g\) | 1（`vld_o`） | \(width\_g + 1\) |
| `use_rdy_g = true` | \(2 \times width\_g\)（`DataMain` + `DataShad`） | 约 3（`DataMainVld`、`DataShadVld`、`rdy_o`） | \(2 \times width\_g + 3\) |

N 级链的总触发器数约为：

\[ FF_{\text{total}} \approx N \times \begin{cases} width\_g + 1 & \text{若 } use\_rdy\_g = false \\ 2 \times width\_g + 3 & \text{若 } use\_rdy\_g = true \end{cases} \]

举例：`width_g = 16`、`stages_g = 4`：

- `use_rdy_g = false`：约 \(4 \times 17 = 68\) 个 FF。
- `use_rdy_g = true`：约 \(4 \times 35 = 140\) 个 FF——约为前者的两倍。

延迟则与模式无关（无反压时均为 `stages_g` 拍），所以「要不要 ready」本质上是用约 2 倍的数据寄存器换「反压安全 + ready 路径可时序收敛」。

#### 4.4.3 源码精读

`use_rdy_g` 如何在每一级里切两套实现，详见 `pl_stage` 的两个 `if generate` 分支：

[psi_common_pl_stage.vhd:51-109](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L51-L109) — 中文说明：`g_rdy` 分支（带反压）使用 record `tp_r` 里的 `DataMain/DataMainVld` 与 `DataShad/DataShadVld` 共两组数据寄存器，外加寄存的 `rdy_o`——这就是 4.4.2 公式里「约 \(2 \times width\_g + 3\)」的来源。

[psi_common_pl_stage.vhd:113-125](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L113-L125) — 中文说明：`g_nrdy` 分支（无反压）只有一个把 `dat_i/vld_i` 打一拍的进程，对应「约 \(width\_g + 1\)」的最小资源。

而 `multi_pl_stage` 把 `use_rdy_g` 原样透传给每一级：

[psi_common_multi_pl_stage.vhd:51-56](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_multi_pl_stage.vhd#L51-L56) — 中文说明：generic 映射 `use_rdy_g => use_rdy_g`，即整条链要么全部带反压、要么全部不带——不存在「前几级带、后几级不带」的混合模式。设计者只需在顶层做一次取舍，全链一致。

#### 4.4.4 代码实践

**估算型实践：为给定配置预估资源。**

1. 实践目标：在综合前就能粗估 `multi_pl_stage` 的 FF 占用，判断是否可接受。
2. 操作步骤：
   - 设定 `width_g = 32`、`stages_g = 6`。
   - 用 4.4.2 公式分别计算 `use_rdy_g = true` 与 `false` 两种取法的 FF 数。
3. 需要观察的现象：带 ready 的版本比不带 ready 的版本多占多少 FF。
4. 预期结果：
   - `use_rdy_g = false`：\(6 \times (32 + 1) = 198\) 个 FF。
   - `use_rdy_g = true`：\(6 \times (2 \times 32 + 3) = 402\) 个 FF。
   - 差值约 204 个 FF（主要来自每级的影子寄存器 `DataShad`）。
5. 待本地验证：综合后读取工具报告的 FF 数，与估算对照（综合器可能因控制信号优化而略有出入）。

#### 4.4.5 小练习与答案

**练习 1**：如果一条路径只需要打 2 拍延迟，且**上游保证不会在下游不 ready 时继续发数**，应该选 `use_rdy_g = true` 还是 `false`？
> **答**：选 `false`。既然上游自带流控、不会在反压时硬塞数据，影子寄存器就无用武之地；选 `false` 可省掉每级约 \(width\_g + 2\) 个 FF，延迟仍然是 2 拍。

**练习 2**：为什么 `multi_pl_stage` 不允许「只在前两级带 ready、后几级不带」？
> **答**：因为 `use_rdy_g` 是整链统一透传的 generic（见 4.4.3 的映射）。混用会破坏 ready 路径的连续性——不带 ready 的级会把 `rdy_i` 直接忽略，导致反压在该级断裂、数据丢失。如果确需混合，应在外层手动级联多个 `pl_stage` 并分别配置，而非用 `multi_pl_stage`。

## 5. 综合实践

**任务：实例化一个 4 级 `multi_pl_stage`，预测连续数据下的延迟，并设计一个最小自检思路。**

1. 在一个新顶层里实例化：
   ```vhdl
   -- 示例代码（非项目原有，仅作演示）
   i_pipe : entity work.psi_common_multi_pl_stage
     generic map(
       width_g   => 16,
       use_rdy_g => true,
       stages_g  => 4,
       rst_pol_g => '1'
     )
     port map(
       clk_i => clk,
       rst_i => rst,
       vld_i => in_vld,
       rdy_o => in_rdy,
       dat_i => in_dat,
       vld_o => out_vld,
       rdy_i => '1',          -- 下游恒 ready，观察纯延迟
       dat_o => out_dat
     );
   ```
2. **预测延迟**：由于 `rdy_i` 恒为 1（无反压），每个 `pl_stage` 延迟 1 拍，4 级共 **4 个时钟周期**。即若 `in_vld` 在拍 `T` 首次为高、`in_dat = X`，则 `out_vld` 在拍 `T+4` 首次为高、`out_dat = X`。
3. **设计自检**：仿照官方 TB（u11-l1）写一个 `p_stim` 连续发 `0,1,2,…,15`，一个 `p_check` 用 `StdlvCompareInt(i, out_dat, "Wrong Data")` 核对；重点验证第一个字滞后 4 拍到达、且序列完整无乱序。
4. **进阶**：把 `rdy_i` 改成周期性拉低（如每 3 拍放 1 拍 ready），观察 `in_rdy` 会被反压拉低、但输出序列仍保持 `0..15` 顺序——这正是 4.3 节描述的反压穿透。
5. 待本地验证：用 Modelsim/GHDL 跑上述 TB，确认延迟与数据完整性。

## 6. 本讲小结

- `psi_common_multi_pl_stage` 是一个**结构型包装器**：用 `for generate` 把 `stages_g` 个 `pl_stage` 串成链，自身几乎不含时序逻辑，行为完全继承自 `pl_stage`（u7-l1）。
- 内部用 `0 to stages_g`（共 `stages_g+1` 个）接点信号承载级间连线：数据/vld 向下游流、ready 反向回传至上游，是标准的 AXI-S skid-buffer 链。
- `stages_g` 类型为 `natural`（**可为 0**）；`stages_g = 0` 触发 `g_zero` 分支，组件退化为纯组合直通，零寄存器、零延迟——这是相对 AXI 版（`positive`）的一个真实差异。
- 组件的核心价值在于**每一级都把 ready 寄存一拍**，把贯穿 N 级的 ready 长组合链切成 N 段短逻辑，利于时序收敛，同时影子寄存器保证反压下不丢数据、不乱序。
- 无反压时延迟恰为 `stages_g` 个时钟周期；资源约随 `stages_g` 线性增长，且 `use_rdy_g = true` 因影子寄存器使数据 FF 翻倍——「要不要 ready」是用约 2 倍数据寄存器换反压安全与可时序性。
- 官方 TB 用 `stages_g => 20` 的深链覆盖单点、流式、反压、valid-不等-ready 四类场景，并在 `sim/config.tcl` 以 `handle_rdy_g=true/false` 两组运行登记入回归。

## 7. 下一步学习建议

- **u7-l3（delay / delay_cfg）**：同样是「在路径上插延迟」，但 `delay` 关注的是**可配置的、可能很深的延迟线**，并能在 BRAM/SRL/寄存器之间选择存储资源，与本章的「握手流水线」互补。
- **u8-l1（wconv_n2xn / wconv_xn2n）**：宽度转换组件内部会用到流水线与握手，可对照观察 `pl_stage`/`multi_pl_stage` 如何被复用来给数据通路打拍。
- **u9-l4（axi_master_full / axi_multi_pl_stage）**：把本章的「级联 + for generate」思路推广到 AXI 五通道，看同一套结构如何承载更复杂的总线接口。
- **延伸阅读**：动手画一张 `stages_g = 4`、`use_rdy_g = true` 的完整时序图（含 `DataMain/DataShad` 在反压下的占用），把 u7-l1 的单级时序与本讲的多级链串起来理解。
