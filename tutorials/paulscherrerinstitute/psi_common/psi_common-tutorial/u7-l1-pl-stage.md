# 流水线级 pl_stage 与二进程设计法

## 1. 本讲目标

`psi_common_pl_stage` 是 psi_common 库里被复用最多的「砖块」之一：一个带 AXI-S 握手的单级流水线寄存器。它体量极小（整个架构不到 100 行），却同时示范了三件在整个库里反复出现的东西——**二进程 record 设计法**、**`if generate` 分支选择**、以及**用影子寄存器补偿反压延迟**。

读完本讲你应当能够：

1. 读懂 PSI 库随处可见的「`r` / `r_next` + `p_comb` / `p_seq`」二进程写法，并能照着它写自己的时序逻辑。
2. 说清 `rdy_o` 为什么要被寄存、寄存之后会带来什么问题，以及影子寄存器（shadow register）如何补救。
3. 根据 `use_rdy_g` 在两套实现之间做选择，并理解 `if generate` 是如何把两份代码合在一个实体里的。
4. 手动跟踪一次下游反压过程中主寄存器与影子寄存器的状态变化。

本讲只依赖 u1-l4 建立的 AXI-S 握手语义（VLD/RDY 同高一拍才发生传输），不依赖任何存储或 CDC 知识。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：流水线级就是把信号「打一拍」。** 最朴素的流水线级就是一个寄存器：每个时钟上升沿把输入搬到输出。这样数据被延迟一个周期，但换来了时序上的好处——组合逻辑路径被打断，关键路径变短。`pl_stage` 在「不需要反压」时就是这样。

**直觉二：AXI-S 握手要求源端「不能丢、不能撤」。** 回顾 u1-l4：传输只在 `vld_i` 与 `rdy_i` 同为高的那一拍发生；源端一旦拉高 `vld_i`，在握手完成前不得撤销数据。所以一个「带握手」的流水线级不能像朴素寄存器那样无脑搬数据——它必须知道下游是否在消费，否则就会把还没被取走的数据覆盖掉。

**直觉三：反压（backpressure）路径常常是时序杀手。** 在流水线里，`rdy` 信号从最末端一路传回最前端。如果每一级都用组合逻辑把下游的 `rdy` 直接转发到上游，整条 ready 路径就是一条又长又深的组合链，很容易成为时序瓶颈。`pl_stage` 的核心设计动机就是**把 `rdy_o` 也寄存一拍**，从而把这条长组合链切成一段段单周期路径。但「寄存 `rdy_o`」会引入一个新的麻烦——这正是本讲要解决的关键问题。

## 3. 本讲源码地图

| 文件 | 作用 |
|:--|:--|
| [hdl/psi_common_pl_stage.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd) | 被精读的组件本体，含实体、`tp_r` 记录、两个 generate 分支 |
| [doc/files/psi_common_pl_stage.md](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_pl_stage.md) | 组件说明文档，给出 generic 与端口表 |
| [testbench/psi_common_pl_stage_tb/psi_common_pl_stage_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_pl_stage_tb/psi_common_pl_stage_tb.vhd) | 自校验测试平台，含反压与「valid 不等 ready」两组关键用例 |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl) | 把该 TB 以 `handle_rdy_g=true/false` 两种组合注册进回归 |

## 4. 核心概念与源码讲解

### 4.1 端口与 generic：pl_stage 的接口

#### 4.1.1 概念说明

`pl_stage` 的接口就是一个标准的 AXI-S 单级通路：一对 `(vld_i, dat_i, rdy_o)` 接上游，一对 `(vld_o, dat_o, rdy_i)` 接下游，外加时钟复位。三个 generic 控制行为：数据宽度、是否启用反压、复位极性。

#### 4.1.2 源码精读

实体声明集中了全部 generic 与端口，并带行内注释（PSI 库的规范做法）：

[psi_common_pl_stage.vhd:22-34](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L22-L34) —— 实体声明，关键点：

- `width_g`：数据位宽，默认 8。
- `use_rdy_g`：布尔量，**本讲的主角**。`true` 走带反压的完整握手实现，`false` 退化成最朴素的寄存器。
- `rst_pol_g`：复位极性，默认 `'1'` 高有效。
- `rdy_i` 带默认值 `:= '1'`：这意味着如果你根本不接 `rdy_i`，下游永远被视为「就绪」，等同于无反压——便于在不关心握手时简化例化。

注意命名严格遵守 u1-l4 的规范：输入 `_i`、输出 `_o`、握手对用 `vld/rdy/dat` 共同前缀、架构名 `rtl`、显式 `end entity`。

> 端口表与 generic 表在文档 [doc/files/psi_common_pl_stage.md:14-32](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_pl_stage.md#L14-L32) 中有一份易读的复述，可对照阅读。

#### 4.1.3 小练习与答案

**练习**：如果把 `pl_stage` 例化在一个下游永远不会反压的环境里，`rdy_i` 不接会怎样？
**答案**：因为 `rdy_i` 在端口声明里带了默认值 `:= '1'`（[L32](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L32)），不接时它恒为 `'1'`，组件会认为下游始终就绪、每个有 `vld_i` 的周期都完成一次传输。

### 4.2 二进程 record 设计法

#### 4.2.1 概念说明

「二进程法」（two-process method）是 PSI 库写时序逻辑的**通用范式**，不止 `pl_stage` 在用——后续 `sync_fifo`、`async_fifo`、`sync_cc_*` 几乎都遵循它。它的核心思想是：

- 把**所有**寄存器打包进一个 `record`（此处叫 `tp_r`），再用一对信号 `r`（当前态）与 `r_next`（次态）表示它。
- 写一个**组合进程** `p_comb`：读输入和 `r`，算出 `r_next`。
- 写一个**时序进程** `p_seq`：只负责在时钟上升沿把 `r_next` 打进 `r`，并处理复位。

好处是：所有状态集中在 record 里，增删一个寄存器只要改 record 定义和组合进程里的几行，时序进程几乎永远不用动；复位也只列出「需要非默认初值」的字段，干净利落。

#### 4.2.2 核心流程

组合进程的固定四步：

1. **保初值**：`v := r;`（把工作变量初始化成当前态，未改的字段自然保持）。
2. **算次态**：按业务逻辑修改 `v` 的各字段。
3. **写信号**：`r_next <= v;`。
4. 时钟进程那边：`r <= r_next;`。

伪代码如下：

```
p_comb(所有输入, r):
    v := r                       -- 默认保持
    v.field := <根据输入算出的新值>
    r_next <= v

p_seq(clk):
    if 上升沿:
        r <= r_next              -- 统一打一拍
        if 复位有效:
            r.某些字段 <= 复位值   -- 只写需要非默认初值的
```

#### 4.2.3 源码精读

record 把本组件的全部寄存器收敛在一起：

[psi_common_pl_stage.vhd:37-46](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L37-L46) —— `tp_r` 定义与 `r`/`r_next` 信号。注意字段含义：

- `DataMain` / `DataMainVld`：**主寄存器**，直接驱动输出 `dat_o` / `vld_o`。
- `DataShad` / `DataShadVld`：**影子寄存器**，当下游反压、主寄存器满时，临时多存一个字（详见 4.4）。
- `rdy_o`：被寄存的 ready 输出。

二进程的骨架在带反压分支里：

[psi_common_pl_stage.vhd:53-92](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L53-L92) —— 组合进程 `p_comb`。开头两步是二进程法的标准动作：

```vhdl
-- *** Hold variables stable ***
v := r;
```

末尾则是标准收尾：

```vhdl
-- *** Assign to signal ***
r_next <= v;
```

[psi_common_pl_stage.vhd:98-108](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L98-L108) —— 时序进程 `p_seq`，只做两件事：上升沿 `r <= r_next`，复位时把 `DataMainVld`/`DataShadVld` 清 0、`rdy_o` 置 1。这就是二进程法里时序进程「极简、几乎不改」的典型形态。

#### 4.2.4 代码实践（源码阅读型）

**目标**：体会「增删寄存器只动 record + 组合进程」的好处。

**步骤**：

1. 假设你想给 `pl_stage` 再加一个寄存器，比如把输出 `vld_o` 多打一拍做对齐。
2. 对照源码想清楚：你需要改 `tp_r` record（加字段）、改 `p_comb`（算该字段的次态）、改 `p_seq`（可选：给它一个复位值）。
3. 注意 `p_seq` 里那条 `r <= r_next;` **完全不用动**——它对 record 的所有字段一视同仁。

**需要观察的现象**：时序进程的代码量与你要加的寄存器数量**无关**；所有「业务逻辑」都集中在组合进程里。这正是二进程法相对「一个进程里既写信号又写 next-state」的老写法的可维护性优势。

**预期结果**：你能口头描述出「加一个寄存器」需要改的三处位置，而不用重写时序进程。

#### 4.2.5 小练习与答案

**练习 1**：组合进程 `p_comb` 的敏感信号列表里为什么必须包含 `r`？
**答案**：因为组合逻辑要读「当前状态」来算「次态」（例如 `IsStuck_v` 依赖 `r.DataMainVld`），`r` 是输入之一，不放进敏感表会导致仿真里 `r_next` 不随 `r` 变化而更新（综合通常仍正确，但仿真会出错）。

**练习 2**：`p_seq` 的复位为什么写成「`r <= r_next;` 之后再覆盖个别字段」而不是用一个完整赋值？
**答案**：因为二进程法默认 `r_next` 已经是正确的次态；复位只需把少数几个必须回到非默认值的字段（这里的 valid 位和 `rdy_o`）覆盖掉，其余字段沿用 `r_next` 即可，避免在时序进程里重复一遍业务逻辑。

### 4.3 use_rdy 分支 generate：把两套实现合在一个实体里

#### 4.3.1 概念说明

`use_rdy_g` 是个布尔 generic。VHDL 里不能用 `if generic then ...` 直接在并发区写条件代码，但可以用 `if ... generate` 在**例化/编译期**选择两段互斥的硬件。`pl_stage` 用 `g_rdy` / `g_nrdy` 两个 generate 分支，把「带反压」与「不带反压」两套完全不同的实现塞进同一个实体，让调用方一个 generic 切换。

#### 4.3.2 核心流程

```
g_rdy  : if use_rdy_g     generate  -> 完整握手版（主/影寄存器）
g_nrdy : if not use_rdy_g generate  -> 朴素寄存器版
```

两段互斥：`use_rdy_g` 为真时只有 `g_rdy` 被综合出来，反之只有 `g_nrdy`。

#### 4.3.3 源码精读

不带反压的朴素分支非常短，可以一眼看懂：

[psi_common_pl_stage.vhd:112-125](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L112-L125) —— `g_nrdy` 分支：

```vhdl
rdy_o <= '0';
p_stg : process(clk_i)
begin
  if rising_edge(clk_i) then
    dat_o <= dat_i;
    vld_o <= vld_i;
    if rst_i = rst_pol_g then
      vld_o <= '0';
    end if;
  end if;
end process;
```

要点：

- 这就是「直觉一」里的朴素寄存器：每个上升沿 `dat_o <= dat_i`、`vld_o <= vld_i`，把数据和有效位各打一拍。
- 它**完全忽略 `rdy_i`**（不接下游反压），也把 `rdy_o` 恒置 `'0'`。这是一个约定：选 `use_rdy_g=false` 意味着调用方声明「我不需要反压」，因此组件不维护 ready 语义。
- 复位时只清 `vld_o`（输出在复位后不能误报有效），数据位不必清。
- 注意这个分支没有用二进程法——因为逻辑太简单，没必要，直接一个时钟进程写输出即可。这也说明二进程法是「工具」而非「教条」，简单逻辑可以简化。

> **回归覆盖**：`sim/config.tcl` 把同一个 TB 用 `handle_rdy_g=true` 和 `=false` 各跑一遍（[config.tcl:319-323](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L319-L323)），确保两个 generate 分支都被测到。TB 里的 generic 叫 `handle_rdy_g`，在例化时映射到 DUT 的 `use_rdy_g`（[psi_common_pl_stage_tb.vhd:68-71](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_pl_stage_tb/psi_common_pl_stage_tb.vhd#L68-L71)）。

#### 4.3.4 小练习与答案

**练习**：`use_rdy_g=false` 时 `rdy_o` 被恒置 `'0'`。若上游是一个会检查 `rdy_o` 的 AXI-S 源端，会发生什么？这算 bug 吗？
**答案**：上游会以为本级「永远不就绪」而停止发送。但这不是 bug，而是使用约定：`use_rdy_g=false` 表示调用方不需要反压，调用方就不应当去检查 `rdy_o`。若上游确实会检查 ready，就必须选 `use_rdy_g=true`。

### 4.4 主/影寄存器与反压：影子寄存器如何补偿反压延迟

这是本讲最核心、也最精妙的一段逻辑。

#### 4.4.1 概念说明：为什么要影子寄存器

回到「直觉三」：为了打断又长又深的 ready 组合链，我们把 `rdy_o` **寄存一拍**。但这样做的代价是一个**一周期的反压延迟**：

1. 某拍下游把 `rdy_i` 拉低（不再消费）。
2. 本级看到 `rdy_i=0`，于是在组合进程里决定把 `rdy_o` 拉低——但 `rdy_o` 是寄存器，要等到**下一个上升沿**才真正变 0。
3. 在这「滞后的一拍」里，上游还没看到 `rdy_o` 变低，仍可能往本级塞一个新的有效字。
4. 而此时主寄存器 `DataMain` 还满着（下游没消费），这个新字无处可去——**会被覆盖丢失**。

影子寄存器 `DataShad` 就是为这个「多出来的一个字」准备的临时车位：当主寄存器满、下游又反压、偏偏上游还在塞数据时，把新字存进影子而不是丢掉。本级于是能容纳两个字（主+影），刚好覆盖这一拍的反压延迟。

#### 4.4.2 核心流程：用 IsStuck 描述「卡住」

组合进程用一个布尔变量 `IsStuck_v` 一句话刻画「现在是否卡住」：

\[
\text{IsStuck} \;=\; (\text{DataMainVld}=1) \;\wedge\; (\text{rdy\_i}=0) \;\wedge\; (vld\_i=1 \;\vee\; \text{DataShadVld}=1)
\]

含义：**主寄存器满** × **下游不消费** × **且仍有数据压力**（要么上游正在送、要么影子里已经囤了一个）。三者同时成立，本级就是「卡住」状态。卡住时做两件事：

- **路由新数据到影子**：若有 `vld_i` 且 `rdy_o` 还高着，把 `dat_i` 写进 `DataShad`（而不是 `DataMain`，因为主还满着）。
- **下拉 `rdy_o`**：下一拍起告诉上游别再送了。

未卡住时，新数据直接进主寄存器，`rdy_o` 保持高。下游消费（`rdy_i=1`）时，影子顺位升格到主，主被取走。

一次完整的数据搬运用伪代码概括：

```
每拍 p_comb:
  v := r
  IsStuck := Main满 且 下游不收 且 (有新输入 或 影子已满)

  if Main满 且 下游收(rdy_i=1):     -- 输出被消费
      Main := Shadow; MainVld := ShadowVld
      ShadowVld := 0

  if rdy_o=1 且 vld_i=1:            -- 接收新输入
      if IsStuck:  Shadow := dat_i; ShadowVld := 1   -- 进影子
      else:        Main  := dat_i; MainVld  := 1     -- 直接进主

  rdy_o_next := not IsStuck         -- 卡住就下拉 ready
  r_next := v
```

#### 4.4.3 源码精读

**IsStuck 的定义**——一切逻辑围绕它展开：

[psi_common_pl_stage.vhd:60-61](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L60-L61) ——「简化变量」，把一个复杂条件命名出来，让后面的分支可读。

**处理输出事务**（下游消费主寄存器，影子顺位升格）：

[psi_common_pl_stage.vhd:63-68](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L63-L68) —— 当主寄存器有效且下游就绪，把影子升格为主、清空影子。

**锁存输入数据**（根据是否卡住决定进主还是进影）：

[psi_common_pl_stage.vhd:70-81](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L70-L81) —— 关键的 if/else：卡住时进影子，否则进主。行内注释直接点明了「ready 只在一周期后才撤销」的原因。

**下拉 ready**：

[psi_common_pl_stage.vhd:83-88](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L83-L88) —— 卡住则 `rdy_o` 次态为 0，否则为 1。

**输出连续赋值**——主寄存器就是输出：

[psi_common_pl_stage.vhd:94-96](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L94-L96) —— `dat_o`/`vld_o` 直接取自主寄存器，影子寄存器永远不出现在端口上，纯属内部「车位」。

#### 4.4.4 代码实践：跟踪一次下游反压的数据流动

**目标**：手动模拟一次「下游突然反压、随后恢复」的过程，画出主/影寄存器与 `rdy_o` 逐拍的状态，验证影子寄存器确实救回了一个本会丢失的字。

**约定**：状态在上升沿更新；下表中「周期 N 的 r」指第 N 个上升沿**之后**的态，「输入」是该周期内施加的信号，「r_next」是下一个上升沿打进 `r` 的态。记 `M=DataMainVld`、`S=DataShadVld`、`Rdy=rdy_o`，数据用字母 A/B/C/D 表示。

**操作步骤**：

1. 复位后初态：`M=0, S=0, Rdy=1`（来自 [L102-105](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L102-L105) 的复位分支）。
2. 按下表第一列的输入，**自己先填**「r_next」一列，再对答案。
3. 重点看周期 2（下游开始反压）与周期 4（下游恢复）：C 进了哪里？D 被怎样处理？

| 周期 | 输入 (vld_i, dat_i, rdy_i) | 该周期 r (M, S, Rdy, 主值) | 你算的 r_next | 参考答案 r_next |
|:--:|:--|:--|:--|:--|
| 1 | (1, A, 1) | 0, 0, 1, — | | 1, 0, 1, 主=A |
| 2 | (1, B, 1) | 1, 0, 1, A | | 1, 0, 1, 主=B（A 被下游取走，B 进主）|
| 3 | (1, C, **0**) | 1, 0, 1, B | | 1, **1**, **0**, 主=B、**影=C**（卡住，C 进影子，ready 下拉）|
| 4 | (1, D, 0) | 1, 1, 0, B/C | | 1, 1, 0, B/C（rdy_o 已低，D **不被接收**）|
| 5 | (0, —, **1**) | 1, 1, 0, B/C | | 1, **0**, **1**, 主=C（B 被取走，影子 C 升格为主，ready 恢复）|

**需要观察的现象**：

- 周期 3：下游刚拉低 `rdy_i`，但 `rdy_o`（Rdy）此刻仍是 1——因为 ready 是寄存的，要下一拍才变 0。这正是「反压延迟一拍」。这一拍里上游还在送 C，而主寄存器满着 B，于是 C 进了**影子**。
- 周期 4：`rdy_o` 已变为 0，上游即使送 D 也不被接收（`p_comb` 里「`rdy_o=1 且 vld_i=1`」条件不成立）。若上游遵守握手（看到 ready 低就停送），D 根本不会出现，无损失。
- 周期 5：下游恢复 `rdy_i=1`，B 被消费，影子里的 C **升格到主**，不丢、不乱序。`rdy_o` 同时恢复为 1。

**预期结果**：周期 3 进影子的 C 在周期 5 完整出现在主寄存器并随后输出——影子寄存器成功兜住了「ready 延迟一拍」期间多出来的那一个字。

**待本地验证**：上表是依据源码逻辑手工推导的。建议你在 Modelsim/GHDL 里跑 `psi_common_pl_stage_tb`（`handle_rdy_g=true` 这一组合），把 `DataMain`/`DataShad`/`rdy_o` 加进波形窗口，对照 TB 的「Back Pressure」用例（[psi_common_pl_stage_tb.vhd:162-186](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_pl_stage_tb/psi_common_pl_stage_tb.vhd#L162-L186) 与校验段 [243-261](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_pl_stage_tb/psi_common_pl_stage_tb.vhd#L243-L261)）观察真实波形，确认与上表一致。

#### 4.4.5 小练习与答案

**练习 1**：本级最多能同时容纳几个字？为什么刚好是这个数？
**答案**：2 个（主 1 + 影 1）。因为 `rdy_o` 寄存导致反压延迟正好一拍，至多多放一个字进来；主寄存器加一个影子寄存器刚好兜住这一个字，再多就浪费，再少就会丢数据。

**练习 2**：若把 `rdy_o` 改成组合输出（不寄存），还需要影子寄存器吗？
**答案**：不需要。若 `rdy_o` 是组合的，下游一拉低 `rdy_i`，`rdy_o` 当拍就能变低，上游当拍就看到、当拍就停送，不会有「多出来的一个字」。但代价是 ready 路径重新变成贯穿多级的组合链，违背了本组件「打断 ready 长路径」的初衷。所以影子寄存器是「为时序而寄存 ready」所付出的合理代价。

**练习 3**：TB 里有一组用例叫「Valid does not wait for Ready」（[L188-206](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_pl_stage_tb/psi_common_pl_stage_tb.vhd#L188-L206)），它检验什么？
**答案**：它让 `rdy_i` 长时间为低的同时连续送 `vld_i`，然后周期性放一拍 `rdy_i=1` 来逐个消费；校验段断言「在 `rdy_i` 低的若干拍内 `vld_o` 始终为高」（数据稳稳留在主寄存器不被撤），并在消费拍用 `StdlvCompareInt` 核对数据值与顺序。本质上就是检验影子寄存器机制在持续反压下不丢、不乱序。

## 5. 综合实践

把本讲的三块知识串起来：二进程法、generate 分支、影子寄存器。

**任务**：为 `pl_stage` 画一张「带反压分支的内部结构草图」，并在图上完成一次反压事件的故事讲解。

**步骤**：

1. 画出五个寄存器：`DataMain(+Vld)`、`DataShad(+Vld)`、`rdy_o`，以及输入 `vld_i/dat_i/rdy_i` 和输出 `vld_o/dat_o/rdy_o`。
2. 标出组合进程 `p_comb`（算 `r_next`）与时序进程 `p_seq`（打 `r`）的边界，体现二进程法。
3. 用箭头标出三种数据通路：
   - 新数据直接进主（未卡住）；
   - 新数据进影子（卡住）；
   - 影子升格为主（下游消费）。
4. 复述 4.4.4 的周期 3→4→5 故事，在图上指出 C 在哪条通路上「停车」又「挪车」。
5. 最后回答：如果把 `use_rdy_g` 设成 `false`，你这张图里哪些部分会被 `if generate` 抹掉？（答：主/影寄存器、`p_comb`、`rdy_o` 寄存器全部消失，只剩 `g_nrdy` 里那对朴素的 `dat_o<=dat_i` / `vld_o<=vld_i` 寄存器。）

**预期结果**：你能对着自己画的图，向别人讲清「为什么寄存 ready 需要影子寄存器」，以及「二进程法把这套逻辑组织得多么紧凑」。

## 6. 本讲小结

- `pl_stage` 是带 AXI-S 握手的单级流水线，三个 generic（`width_g`/`use_rdy_g`/`rst_pol_g`）控制其行为，`rdy_i` 带默认值 `'1'` 方便无反压例化。
- **二进程 record 法**是全库范式：所有寄存器打包进 record `tp_r`，`p_comb` 算 `r_next`，`p_seq` 只打拍 + 复位个别字段；增删寄存器只动 record 与组合进程。
- `use_rdy_g` 用 `g_rdy` / `g_nrdy` 两个 `if generate` 分支在编译期二选一：`false` 时退化成朴素的 `dat_o<=dat_i`/`vld_o<=vld_i` 寄存器，忽略握手。
- 带 ready 分支里，`rdy_o` 被**寄存**以打断 ready 长组合链；代价是反压延迟一拍，用 `IsStuck` 条件触发**影子寄存器** `DataShad` 兜住那一拍多出来的字。
- 影子升格、主被消费的逻辑全在 `p_comb` 的三段 if 里（输出事务 / 锁存输入 / 下拉 ready），输出连续赋值只暴露主寄存器。
- TB 以 `handle_rdy_g=true/false` 两种组合在 `config.tcl` 注册，「Back Pressure」与「Valid does not wait for Ready」两组用例专门检验影子寄存器在反压下不丢不乱序。

## 7. 下一步学习建议

- **u7-l2 多级流水线 multi_pl_stage**：看 `pl_stage` 如何被串成多级、反压如何在级间传递，是本讲的直接延伸。
- **u7-l3 可配置延迟 delay / delay_cfg**：对照另一种「打拍」实现（基于 RAM/SRL 的延迟线），理解寄存器流水线与存储延迟的取舍。
- **u4-l1 / u4-l2 FIFO**：FIFO 内部同样大量使用二进程 record 法与 ready 寄存，读完本讲再去看 FIFO 会非常顺。
- 想巩固二进程法，可在库里 `Grep` 搜 `r_next` 与 `type .* is record`，观察 `psi_common_sync_fifo.vhd`、`psi_common_async_fifo.vhd` 如何复用同一套写法。
