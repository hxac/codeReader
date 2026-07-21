# vIS 计分板与冒险检测

## 1. 本讲目标

在 u2-l3、u2-l4 里，我们已经走完了 vRRM 这一级：每条向量指令被盖上了**物理寄存器号**、一个全局递增的 **ticket**，以及一组 **lock（锁定位）**；物理寄存器与真实存储则由 VRF/VRAT 承担。指令离开 vRRM 后，下一站就是 **vIS（Vector Issue，向量发射级）**。

vIS 是整个向量数据通路最「聪明」的一级。它不做任何运算，却决定了一条指令**什么时候可以发给执行级 vEX**。它要同时回答三个问题：

- 我要读的源寄存器，那一格数据现在是不是已经躺在 VRF 里了？（RAW 冒险）
- 我要写的目的寄存器，是不是正被某条在飞的访存指令占着？（lock 冒险）
- 既然执行级是一条带变延迟的流水线，源数据能不能不回写、直接从流水线半路「转发」给我？

本讲学完，你应该能：

- 看懂 vIS 的 **per-element（逐元素）计分板**：`pending`、`locked` 两张位矩阵 + 两套 ticket 表。
- 读懂冒险检测的核心逻辑 `src1_ok / src2_ok / rdst_ok / no_hazards`，以及 `can_issue` 如何把逐元素结果汇聚成「能不能发射」的一拍决定。
- 看懂**三处转发点**（frw_a / frw_b / wr）如何用「地址 + ticket」精确命中正确的生产者，从而消除 RAW 等待。
- 看懂 vIS 在发射访存指令时如何 **acquire（上锁）**、vMU 完成后如何通过 **unlock 接口** release（解锁），以及 ticket 如何在这两条数据通路之间消歧。

## 2. 前置知识

- **RAW 冒险（Read-After-Write）**：后一条指令要读的寄存器，正是前一条指令还没写完的目的寄存器。这时后一条指令要么等，要么走「转发」直接拿前一条指令半成品的结果。
- **计分板（scoreboard）**：用一张「位图」记录每个寄存器（或每个元素）当前是否「有待完成的生产者」。位图为 1 表示「还在生产，不能直接读」；变 0 表示「值已就绪」。这比传统的「整寄存器一粒度」计分板要细，能支持**部分写完成**。
- **变延迟执行（variable latency）**：vEX 里不同操作拍数不同——简单 ALU 1 拍，MUL 3 拍，DIV 4 拍（见 u2-l8）。这意味着同一条指令的不同 lane、甚至同一 lane 的不同时刻，结果就绪时间都不同。计分板必须**逐元素**跟踪，不能假设「一条指令整拍完成」。
- **ticket（票据）**：vRRM 给每条指令分配的全局递增序号（见 u2-l3）。它的作用是「**消歧**」：同一个物理寄存器号会被反复重用（硬件循环展开会让不同迭代写不同物理块，但物理号总量只有 32 个，迟早绕回），仅靠寄存器号无法判断「这个转发数据是不是我要的那次生产」，必须再比 ticket。
- **lock 位编码**（复习 u2-l3，由 vrrm.sv 设定）：
  - `2'b00` 整数指令——不上锁。
  - `2'b01` store——`lock[0]=1`，源被访存消费。
  - `2'b10` toeplitz 预取——`lock[1]=1`，目的由访存产生。
  - `2'b11` load——`lock[0]=1` 且 `lock[1]=1`，源被消费且目的由访存产生。
- **ready/valid 握手**：vRRM→vIS、vIS→vEX 之间都是 ready/valid 握手（见 u2-l1、u2-l2）。本讲里 `valid_in/ready_o` 是 vIS 的入口握手，`valid_o/ready_i` 是出口握手。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [rtl/vector/vis.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv) | **本讲主角**：计分板 + 逐元素冒险检测 + 转发 mux + lock/unlock 接口，内部还例化了 VRF |
| [rtl/vector/vrrm.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv) | 给指令打 ticket、设 lock 位、分配物理寄存器（vIS 的上游，提供 `instr_in`） |
| [rtl/vector/vstructs.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv) | 定义入口结构体 `remapped_v_instr`、出口 `to_vector_exec[_info]` 等字段形状 |
| [rtl/vector/vmacros.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv) | 转发点宏 `EX1..EX4_F`、FU 编码、存储操作位段 |
| [rtl/vector/vector_top.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv) | 把 vIS 的转发/写回端口接到 vEX、把 unlock 端口接到 vMU，是理解「三个转发点从哪来」的连线图 |

一句话定位：**vIS 是计算路与访存路的汇聚中枢**。它拿着 vRRM 给的物理号去读写住在自己内部的 VRF，同时为 vEX 提供源数据、为 vMU 维护锁表。本讲只看 vIS 的「控制」一面（计分板 + 冒险 + 转发 + 锁），VRF 的存储细节已在 u2-l4 讲过。

## 4. 核心概念与源码讲解

### 4.1 计分板位矩阵：pending / locked / ticket

#### 4.1.1 概念说明

vIS 的计分板不是「一个寄存器一个 bit」，而是「**一个元素一个 bit**」。原因是变延迟：一条 `vadd` 写 8 个 lane，可能 lane0 早 1 拍就好、lane7 还在算；如果用整寄存器粒度，lane0 早就绪了也得陪 lane7 等到全部完成。逐元素粒度让每个 lane 独立就绪、独立被消费，吞吐更高。

计分板用 **两套独立的位矩阵** + **两套 ticket 表** 实现，分别管两件不同的事：

| 结构 | 维度 | 置位含义 | 服务对象 |
|---|---|---|---|
| `pending` | `[32][8]`（寄存器 × lane） | 该元素值尚未写回 VRF（计算指令或 load 在路上） | **计算 RAW 冒险 + 转发** |
| `pending_ticket` | `[32][4]`（寄存器 × ticket） | 该寄存器当前生产者的 ticket | 转发命中比对 |
| `locked` | `[32][8]`（寄存器 × lane） | 该元素正被一条在飞的访存指令占用 | **访存 ordering / lock 冒险** |
| `locked_ticket` | `[32][4]`（寄存器 × ticket） | 占用该寄存器的访存指令 ticket | unlock 比对 |

注意 `pending_ticket` 和 `locked_ticket` 是「**每寄存器**」而不是「每元素」一个 ticket——因为同一个寄存器的所有有效元素是被**同一条**指令、即同一个 ticket 生产的，没必要每 lane 存一份。

为什么要 `pending` 和 `locked` 两张表分开？因为它们由不同的「release（释放）方」清除：

- `pending` 的清除方有两个：**vEX 的写回**（`wr`，计算完成）和 **vMU 的 load 写回**（`mem_wr`，load 数据回来）。
- `locked` 的清除方只有一个：**vMU 的 unlock 接口**（访存引擎完成）。

一个 load 的目的寄存器会**同时**被置 `pending`（计算要等它）和 `locked`（防止别的访存指令抢同一寄存器）；二者在不同时机被各自清除。这是后续 u4-l1「acquire-release 语义」的硬件落点。

#### 4.1.2 核心流程

```text
计分板的两套状态机（每个元素独立运转）：

  pending[i][k] 的生命周期：
    置 1：发射一条「写 i」的计算/load 指令 (do_issue, dst=i, k 在 vl_therm 内, 非dst_iszero)
          → 同时 pending_ticket[i] ← 该指令 ticket
    清 0：(a) vEX 写回命中   wr_en[k] & wr_addr==i & wr_ticket==pending_ticket[i]
          (b) vMU load 写回命中 mem_wr_en[k] & mem_wr_addr==i & mem_wr_ticket==pending_ticket[i]
          (c) 该元素不在本 uop 有效范围 (k 不在 vl_therm) → 写 0 (处理末尾不满一拍)

  locked[i][k] 的生命周期：
    置 1：发射访存指令时
          lock[1]=1 (load/toeplitz 产生目的) → 锁 dst=i
          lock[0]=1 (store 消费源)           → 锁 src2=i   (src1 暂未启用)
          → 同时 locked_ticket[i] ← 该指令 ticket
    清 0：vMU unlock 命中  unlock_en & (reg_a==i | reg_b==i) & unlock_ticket==locked_ticket[i]

  两个全局屏障信号：
    exec_finished = ~(|pending) & ~(|locked)   // 所有 bit 清零才算「排空」
    reconfigure 指令必须等到 exec_finished 才能执行（drain barrier）
```

一个关键直觉：`pending` 是给**计算通路**看的「数据就绪」标志，`locked` 是给**访存通路**看的「资源占用」标志。两者一张表管数据相关，一张表管资源互斥，分工明确。

#### 4.1.3 源码精读

**两张位矩阵 + 两套 ticket 的声明**：[rtl/vector/vis.sv:78-80](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L78-L80)

```systemverilog
logic [VECTOR_REGISTERS-1:0][VECTOR_TICKET_BITS-1:0] pending_ticket; // 每寄存器 1 个 ticket
logic [VECTOR_REGISTERS-1:0][VECTOR_TICKET_BITS-1:0] locked_ticket;  // 每寄存器 1 个 ticket
logic [VECTOR_REGISTERS-1:0][VECTOR_LANES-1:0] pending, locked;       // 每元素 1 个 bit
```

按默认例化规模，`pending`/`locked` 各是 `32×8` 的位矩阵，两张 ticket 表各是 `32×4`。这就是「逐元素计分板」的全部存储。

**排空屏障与重配**：[rtl/vector/vis.sv:114-116](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L114-L116)

```systemverilog
assign do_reconfigure = reconfig_instr & valid_in & exec_finished;
assign exec_finished  = ~(|pending) & ~(|locked);
```

`exec_finished` 把两张表所有位「或归约」再取反——任意一格 pending 或 locked 为 1，都不算排空。重配指令必须等到排空，才能动，避免在飞指令把状态写进「刚清零」的新表里。

**pending 状态机**（节选关键分支）：[rtl/vector/vis.sv:291-314](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L291-L314)

```systemverilog
always_ff @(posedge clk or negedge rst_n) begin: StatusPending
    if(!rst_n)                                   pending <= '0;
    else if(do_reconfigure)                      pending <= '0;   // 重配整表清零
    else if (!reconfig_instr) begin
        for (int k = 0; k < VECTOR_LANES; k++)
          for (int i = 0; i < VECTOR_REGISTERS; i++) begin
            if(dst_oh[i] && vl_therm[k] && do_issue && !instr_in.dst_iszero) begin
                pending[i][k]     <= 1;            // ACQUIRE：本 uop 有效元素置位
                pending_ticket[i] <= instr_in.ticket;
            end else if(dst_oh[i] && ~vl_therm[k] && do_issue && !instr_in.dst_iszero) begin
                pending[i][k]     <= 0;            // 有效范围外的元素清零(末尾 uop)
            end else if(wr_en[k] && wr_addr_oh[k][i] && ticket_match_pending) begin
                pending[i][k]     <= 0;            // RELEASE-a：vEX 写回
            end else if (mem_wr_en[k] && mem_wr_addr_oh[k][i] && mem_ticket_match_pending) begin
                pending[i][k]     <= 0;            // RELEASE-b：vMU load 写回
            end
          end
    end
end
```

读这段要抓住三个动作：**ACQUIRE**（发射时按 `vl_therm` 给有效元素置位 + 记 ticket）、**末尾清零**（处理 VL 不是 lane 整数倍时最后一个不满的 uop）、**两种 RELEASE**（vEX 写回或 vMU load 写回，都要求 ticket 匹配）。

注意 `dst_iszero`：store 指令 `dst_iszero=1`（见 vrrm.sv），所以 store 永远不会置 `pending`——它不写目的，自然不产生计算相关。

**locked 状态机**（节选）：[rtl/vector/vis.sv:317-343](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L317-L343)

```systemverilog
assign ticket_match_locked = (unlock_ticket === locked_ticket[unlock_reg_a]);
...
        if(do_issue && vl_therm[k] && dst_oh[i] && instr_in.lock[1] && !instr_in.dst_iszero) begin
            locked[i][k]     <= 1;            // ACQUIRE-目的：load/toeplitz (lock[1])
            locked_ticket[i] <= instr_in.ticket;
        end else if(do_issue && vl_therm[k] && src2_oh[i] && instr_in.lock[0] && !instr_in.src2_iszero) begin
            locked[i][k]     <= 1;            // ACQUIRE-源：store (lock[0])，目前锁 src2
            locked_ticket[i] <= instr_in.ticket;
        end else if(unlock_en && unlock_reg_a_oh[i] && ticket_match_locked) begin
            locked[i][k]     <= 0;            // RELEASE：vMU unlock 命中 reg_a
        end else if(unlock_en && unlock_reg_b_oh[i] && ticket_match_locked) begin
            locked[i][k]     <= 0;            // RELEASE：vMU unlock 命中 reg_b
        end
```

对比 pending 状态机，能看到分工的精髓：locked **只能由 vMU 的 unlock 清除**（没有 `wr_en` 分支）。源码里还有一段被注释掉的 `src1` 分支（[rtl/vector/vis.sv:329-331](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L329-L331)），注释写着「for now mem ops dont use src1…might change」——说明当前 store 只锁 src2，这是一个已知的设计取舍，扩展时要注意。

**vMU 读取计分板的窗口**：[rtl/vector/vis.sv:345-357](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L345-L357)

```systemverilog
assign mem_pending_0 = pending[mem_addr_0][0];   // 只看 lane0 的 bit
assign mem_ticket_0  = pending_ticket[mem_addr_0];
...
        assign mem_prb_locked_o[i] = locked[mem_prb_reg_i[i]][0];
        assign mem_prb_ticket_o[i] = locked_ticket[mem_prb_reg_i[i]];
```

vMU 通过 3 个读端口探询某寄存器的 `pending`（注意只取 `[0]` 位——访存把一个寄存器视作整体），并通过 4 路 probe 端口探询 `locked`。这些信号是 vMU 决定「能不能取数 / 写回优先级 / 该不该等」的依据，具体怎么用在 u3-l1 展开。

#### 4.1.4 代码实践（源码阅读型）

**目标**：把计分板「两张表、两套 ticket」的状态流转在脑子里跑一遍，确认你对 ACQUIRE/RELEASE 的理解。

**操作步骤**：

1. 打开 [rtl/vector/vis.sv:291-343](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L291-L343)，并排对比 `StatusPending` 与 `StatusLocked` 两个 `always_ff`。
2. 对每张表，分别列出：**谁能在什么条件下置 1？谁能在什么条件下清 0？清 0 时要比对什么？**
3. 回答一个判断题：一条 `vld`（load，lock=2'b11，dst_iszero=0）发射后，它的目的寄存器 `pending` 和 `locked` 各自由谁清除？两者清除时刻一定相同吗？

**需要观察的现象 / 预期结果**：

- `pending[dst]` 在发射时置位，由 **vEX 写回 (`wr_en`)** 或 **vMU load 写回 (`mem_wr_en`)** 任一命中并 ticket 匹配时清除。
- `locked[dst]` 在发射时（因 lock[1]=1）置位，只能由 **vMU unlock (`unlock_en`)** 命中并 ticket 匹配时清除。
- 两者由 vMU 的**不同信号**清除，时刻不一定相同（写回数据 ≠ 发 unlock），这正是 pending/locked 必须分两张表的根本原因。
- 如果你的结论与此一致，说明你理解了「数据就绪（pending）」与「资源释放（locked）」的分工。若不一致，回到 4.1.1 的表格对照。

> 本实践为源码阅读型，不实际运行；结论可在后续 u4-l1 用波形验证（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pending_ticket` 是「每寄存器一个」而不是「每元素一个」？

> **答案**：同一个寄存器的所有有效元素是由**同一条指令**（同一个 ticket）一次性生产的，逐 lane 存 ticket 是冗余。位矩阵 `pending` 需要 per-element 是因为变延迟让各 lane 就绪时间不同；ticket 只用来标识「哪一次生产」，每寄存器一份就够。

**练习 2**：假设一条 `vadd` 写 `v3`（ticket=6），随后 vEX 在某一拍把 lane2、lane5 的结果写回。这时 `pending[3]` 的 8 个 bit 各是什么状态？

> **答案**：`pending[3][2]` 和 `pending[3][5]` 在该拍之后被清 0（`wr_en` 命中、ticket=6 匹配 `pending_ticket[3]`），其余 6 个 bit 仍为 1，直到各自 lane 被写回。这就是「部分写完成」——计分板允许同一寄存器的不同 lane 先后就绪。

---

### 4.2 逐元素冒险检测：src1_ok / src2_ok / rdst_ok / no_hazards

#### 4.2.1 概念说明

计分板只是「状态」，冒险检测才是把它变成「能不能发射」的决策器。vIS 的决策是**逐 lane 独立判断、再整体归约**：

- 对每个 lane `p`，分别判断：源 1 就绪吗？源 2 就绪吗？目的没被锁吗？三者都满足 → 这个 lane 无冒险 `no_hazards[p]=1`。
- 一条指令只有在**所有有效 lane** 都无冒险时才能发射（`can_issue = &issue_masked`）。

「有效 lane」由 `vl_therm` 决定。当 VL 不是 lane 数的整数倍，最后一个 uop 只用到部分 lane，用不到的 lane 会被屏蔽，不参与归约——否则那些「本不该用」的 lane 会因为读到无意义的 pending 而误判冒险。

源「就绪」有三条等价路径（任一成立即可）：

1. **零寄存器**：`src1_iszero` / `src2_iszero`——读的是零寄存器，永远就绪。
2. **转发命中**：三个转发点之一正好提供这个源（见 4.3）。
3. **非 pending**：`~pending[src][p]`——该元素已经写回 VRF，直接读就行。

目的「可用」只有一条：`~locked[dst][p]`——目的元素没被在飞访存指令占着。

#### 4.2.2 核心流程

```text
对每个 lane p ∈ [0, LANES):

  src1_ok[p] = src1_iszero                      // 路径1: 零寄存器
             | frw_a_src_1[p] | frw_b_src_1[p] | frw_c_src_1[p]   // 路径2: 转发命中
             | ~pending[src_1][p]                // 路径3: 已写回VRF

  src2_ok[p] = src2_iszero | frw_a_src_2[p] | frw_b_src_2[p] | frw_c_src_2[p]
             | ~pending[src_2][p]

  rdst_ok[p] = ~locked[dst][p]                   // 目的没被访存占用

  no_hazards[p] = src1_ok[p] & src2_ok[p] & rdst_ok[p]

整体归约:
  vl_therm        = 有效元素掩码 (本 uop 实际用到的 lane)
  issue_masked[p] = no_hazards[p] | ~vl_therm[p]     // 无效 lane 直接放行
  can_issue       = &issue_masked                    // 所有有效 lane 都无冒险才发射
```

对访存指令，决策逻辑不同（不查 pending，只查 locked），见 4.3.4。

#### 4.2.3 源码精读

**有效元素掩码 vl_therm**：[rtl/vector/vis.sv:119](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L119) 与 [rtl/vector/vis.sv:126](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L126)

```systemverilog
assign total_remaining_elements = instr_in.vl - (current_exp_loop*VECTOR_LANES);
assign vl_therm                  = ~('1 << total_remaining_elements);
```

`'1` 是全 1，左移 `total_remaining_elements` 位再取反，得到「低 N 位为 1」的掩码（类热码）。例如本 uop 还剩 5 个元素，`vl_therm = 8'b00011111`，即只有 lane0~4 有效。这就是上一小节「末尾 uop 部分写」与「无效 lane 屏蔽」的同一根源头。

**逐元素冒险判定**：[rtl/vector/vis.sv:244-258](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L244-L258)

```systemverilog
assign can_issue    = &issue_masked;
assign issue_masked = no_hazards | ~vl_therm;
generate for (genvar p = 0; p < VECTOR_LANES; p++) begin: g_iss_logic
    assign src1_ok[p] = (instr_in.src1_iszero)                             |
                        (frw_a_src_1[p] | frw_b_src_1[p] | frw_c_src_1[p]) |
                        (~pending[src_1][p]);
    assign src2_ok[p] = (instr_in.src2_iszero)                             |
                        (frw_a_src_2[p] | frw_b_src_2[p] | frw_c_src_2[p]) |
                        (~pending[src_2][p]);
    assign rdst_ok[p]    = ~locked[dst][p];
    assign no_hazards[p] = src1_ok[p] & src2_ok[p] & rdst_ok[p];
end endgenerate
```

读这段有两个要点：

1. `src1_ok` 的三条路径用 `|` 连起来，正好对应 4.2.1 的「零寄存器 / 转发 / 已写回」。
2. `&issue_masked` 是「与归约」——所有有效 lane 必须同时无冒险。`issue_masked = no_hazards | ~vl_therm` 把无效 lane 强制变 1，让它们不拖后腿。

注意一个优美的耦合：`src1_ok` 里用到的 `frw_a_src_1[p]` 等转发命中信号，**和下面 4.3 数据 mux 用的命中信号是同一组**。这保证了「判定为就绪」与「取到正确数据」永远一致——只要 `src1_ok` 因为转发而成立，数据 mux 就一定会选中那份转发数据。

**访存指令的冒险判定**：[rtl/vector/vis.sv:259-267](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L259-L267)

```systemverilog
assign can_issue_m    = &issue_m_masked;
assign issue_m_masked = no_hazards_m | ~vl_therm;
generate for (genvar l = 0; l < VECTOR_LANES; l++) begin: g_iss_logic_mem
    assign can_lock_sources[l]     = instr_in.lock[0] ? (~locked[src_1][l] & ~locked[src_2][l]) : 1'b1;
    assign can_lock_destination[l] = instr_in.lock[1] ? ~locked[dst][l] : 1'b1;
    assign no_hazards_m[l]         = can_lock_sources[l] & can_lock_destination[l];
end endgenerate
```

访存指令**不查 pending**（它不靠 VRF 的 ALU 读端口取运算源，store 另有取数端口、load 是写不是读），只检查「我要锁的资源现在空不空」：`lock[0]`（store）要求源没被锁，`lock[1]`（load/toeplitz）要求目的没被锁。这保证两条访存指令不会同时抢同一个寄存器。

**发射决定与反压**：[rtl/vector/vis.sv:128-136](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L128-L136)

```systemverilog
assign do_issue = memory_instr ? (valid_in & can_issue_m) :
                               (valid_in & output_ready & can_issue);
assign ready_o = reconfig_instr ? exec_finished                                   :
                 memory_instr   ? (can_issue_m & expansion_finished)              :
                 valid_in       ? (can_issue & expansion_finished & output_ready) : 1'b0;
```

- 计算指令要 `can_issue`（冒险全消）**且** `output_ready`（vEX 愿意收）才能发。
- 访存指令只要 `can_issue_m`，不要求 `output_ready`——因为它根本不发给 vEX，而是拐去 vMU。
- `ready_o`（告诉上游 vRRM「我收下了」）的条件里多了 `expansion_finished`：一条指令要展开成多个 uop（见 u2-l6），必须等所有 uop 展开完，才算这条指令真正吃掉。

#### 4.2.4 代码实践（源码阅读型 · 本讲核心实践）

**目标**：给定两条相继的向量指令，分析它们在哪些 lane/元素上存在 RAW 冒险，并说明三处转发点如何消除等待。

**场景设定**（VECTOR_LANES=8）：

- 指令 A：`vadd v3, v1, v2`，ticket=5，VL=8（一个满 uop）。
- 指令 B：`vadd v4, v3, v1`，ticket=6，VL=8。注意 B 的 **src1 是 v3**，正是 A 的目的——存在 RAW。

**操作步骤**：

1. A 在 vIS 发射（`do_issue=1`）那一拍，`pending[v3][0..7]` 全部置 1，`pending_ticket[v3]=5`（见 4.1.3 的 StatusPending）。
2. B 进来后，计算 `src_1 = v3`。逐 lane 判断 `src1_ok[p]`：
   - 假设此刻**没有任何转发**：`frw_*_src_1[p]=0`，`pending[v3][p]=1` → `~pending=0` → `src1_ok[p]=0` → `no_hazards[p]=0` → `can_issue=0` → B **停顿**。
   - 这说明：没有转发时，B 必须等 A 把 v3 **写回 VRF**（`pending[v3]` 全清）才能发射，等的是 A 的完整执行延迟（1~4 拍视操作而定）。
3. 现在加入转发：A 在 vEX 流水线里行进，依次驱动三个转发点（均带 `ticket=5, addr=v3`）：
   - Forward #1 `frw_a`（最早，对应 EX1）
   - Forward #2 `frw_b`（中段，对应 EX3）
   - Writeback `wr`（最后回写 VRF，对应 EX4）
4. 看 [rtl/vector/vis.sv:233-234](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L233-L234)：`frw_a_src_1[p] = frw_a_en[p] & (frw_a_addr===v3) & (frw_a_ticket===5)`。当 A 走到 EX1，三条件全成立 → `frw_a_src_1[p]=1` → `src1_ok[p]` 经 `|` 路径**立刻变 1**，同时数据 mux（4.3.3）选中 `frw_a_data[p]`。
5. 于是 B 不必等 A 写回 VRF，在 A 进 EX1 的下一拍即可发射——**RAW 等待被转发消除**。

**需要观察的现象 / 预期结果**：

- 无转发：B 卡在 `can_issue=0`，直到 `pending[v3]` 全清（A 写回完成）。
- 有转发：B 在 A 触达第一个转发点的下一拍即可发射；越早的转发点（EX1）消除的等待越多。
- 把 VL 改成 5：A 只置位 `pending[v3][0..4]`，`pending[v3][5..7]=0`；B 的 `vl_therm=8'b00011111`，lane5~7 被 `issue_masked` 屏蔽，不影响 `can_issue`。这说明逐元素 + vl_therm 让部分写也能正确判断。

> 本实践为源码阅读与手工推演型；如需看真实波形，可在 u1-l5 的仿真里用 `vadd` 后接依赖 `vadd` 的程序，在波形里观察 `can_issue` 拉高的时刻（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`issue_masked = no_hazards | ~vl_therm` 里的 `~vl_therm` 起什么作用？去掉会怎样？

> **答案**：它把「本 uop 用不到的 lane」强制记为「无冒险」。若去掉，VL 不是 lane 整数倍时，最后一个 uop 里用不到的 lane 会去查 `pending[src][p]`，读到无意义的旧值，可能误判冒险、卡住发射。

**练习 2**：为什么访存指令用 `can_issue_m`（只查 locked）而计算指令用 `can_issue`（查 pending + locked）？

> **答案**：计算指令的源来自 VRF 的 ALU 读端口，要等数据写回（pending）或转发；访存指令不靠这个端口取运算源（store 有专门取数端口，load 是写 VRF），所以不查 pending，只检查「我要锁的源/目的寄存器现在空不空」（locked），避免和别的在飞访存指令抢同一寄存器。

---

### 4.3 转发网络与解锁接口：三处转发点 + lock/unlock

#### 4.3.1 概念说明

4.2 已经看到「转发命中」能让源立即就绪。这一节我们正面看**转发网络**本身，以及与它对称的**解锁接口**。二者一进一出，构成 vIS 与外部（vEX、vMU）的「数据回流」与「控制回流」。

**三处转发点**（都从 vEX 流入 vIS，见 [rtl/vector/vector_top.sv:363-377](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L363-L377)）：

| 转发点 | 信号前缀 | 来自 vEX 的哪一段 | 时效 |
|---|---|---|---|
| Forward #1 | `frw_a_*` | EX1（最早） | 结果刚算出、最早可转 |
| Forward #2 | `frw_b_*` | EX2/EX3（中段） | 结果半熟 |
| Forward #3 = Writeback | `wr_*`（在 vis 内叫 `frw_c`） | EX4 写回 VRF | 最终值，同时也清 pending |

每个转发点都带四件套：`en[8]`（每 lane 使能）、`addr`（物理寄存器号）、`data[8][32]`（每 lane 数据）、`ticket`。转发命中需要**三个条件同时成立**：`en[p]` & `addr===src` & `ticket===pending_ticket[src]`。第三个条件是命根子——它保证「这个转发数据正是我等待的那次生产」，而非同名物理寄存器的某次历史写入。

转发点从早到晚是「**冗余覆盖**」关系：同一条生产者指令会先后出现在 EX1、EX3、EX4，越靠后的越「新鲜」也越晚。数据 mux 用「**或运算优先**」选最早命中的那份（见 4.3.3），既消除冒险又不重复取数。

转发点位置可配置：`params.sv` 里 `VECTOR_FWD_POINT_A/B` 取自 `vmacros.sv` 的 `EX1/EX4_F` 等宏（`_F` = flopped，寄存一拍换频率，见 u1-l3、u4-l2）。本讲只把它们当「三路 inbound 总线」用，具体拍数与可配置性在 u2-l7/u4-l2 展开。

**解锁接口**（从 vMU 流入 vIS，见 [rtl/vector/vis.sv:50-54](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L50-L54)）：

```text
unlock_en        // 本拍有一次解锁
unlock_reg_a/_b  // 被解锁的寄存器 (一次最多解两个, 如 load 的 src2 和 dst)
unlock_ticket    // 配对的 ticket
```

它与 4.1 的 `locked` 表严格对应：vIS 在发射访存指令时 **acquire**（置 locked + 记 locked_ticket），vMU 完成访存后 **release**（unlock_en + reg + ticket 命中即清 locked）。这就是后续 u4-l1 要讲的 **acquire-release 解耦执行语义**——计算流与访存流靠这对 lock/unlock 异步推进，ticket 跨两条数据通路消除生产者-消费者歧义。

#### 4.3.2 核心流程

```text
对每个 lane j ∈ [0, LANES):

  Forward #1 命中:
    frw_a_src_1[j] = frw_a_en[j] & (frw_a_addr === src_1) & (frw_a_ticket === pending_ticket[src_1])
    frw_a_src_2[j] = ... (同上, 比对 src_2)

  Forward #2 命中:
    frw_b_src_1[j] = frw_b_en[j] & (frw_b_addr === src_1) & (frw_b_ticket === pending_ticket[src_1])
    ...

  Forward #3 (Writeback) 命中:
    frw_c_src_1[j] = wr_en[j] & (wr_addr === src_1) & (wr_ticket === pending_ticket[src_1])
    ...

数据 mux (优先取最早命中的转发, 否则取 VRF 读):
  data1[k] = (frw_a_src_1[k] ? frw_a_data[k]) |
             (frw_b_src_1[k] ? frw_b_data[k]) |
             (frw_c_src_1[k] ? wr_data[k])    |       // 写回的数据
             (~pending[src_1][k] ? data_1[k])         // VRF 直读
  若 pending 且无任何转发命中 → 该项为 0 (此时 src1_ok=0, 本就不会发射, 数据无意义)

解锁 (与 locked 表联动, 见 4.1.3):
  ACQUIRE : 发射访存指令时按 lock[1]/lock[0] 置 locked + 记 locked_ticket
  RELEASE : unlock_en & reg 匹配 & unlock_ticket===locked_ticket[reg] → 清 locked
```

#### 4.3.3 源码精读

**转发命中判定**：[rtl/vector/vis.sv:230-241](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L230-L241)

```systemverilog
generate for (genvar j = 0; j < VECTOR_LANES; j++) begin: g_frw_en
    //Forward Point #1
    assign frw_a_src_1[j] = frw_a_en[j] & (frw_a_addr === src_1) & (frw_a_ticket === pending_ticket[src_1]);
    assign frw_a_src_2[j] = frw_a_en[j] & (frw_a_addr === src_2) & (frw_a_ticket === pending_ticket[src_2]);
    //Forward Point #2
    assign frw_b_src_1[j] = frw_b_en[j] & (frw_b_addr === src_1) & (frw_b_ticket === pending_ticket[src_1]);
    assign frw_b_src_2[j] = frw_b_en[j] & (frw_b_addr === src_2) & (frw_b_ticket === pending_ticket[src_2]);
    //Forward Point #3 (Writeback)
    assign frw_c_src_1[j] = wr_en[j] & (wr_addr === src_1) & (wr_ticket === pending_ticket[src_1]);
    assign frw_c_src_2[j] = wr_en[j] & (wr_addr === src_2) & (wr_ticket === pending_ticket[src_2]);
end endgenerate
```

注意三个细节：① 用 `===`（四态严格相等），避免 X 态误命中；② ticket 比对的是 `pending_ticket[src]`，即「当前我等的那个生产者」；③ 三个转发点结构完全对称，只是数据源（EX1/EX3/写回）不同。

**数据 mux（核心转发选择）**：[rtl/vector/vis.sv:207-228](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L207-L228)

```systemverilog
assign data_to_exec[k].data1  = ({32{frw_a_src_1[k]}} & frw_a_data[k]) |
                                ({32{frw_b_src_1[k]}} & frw_b_data[k]) |
                                ({32{frw_c_src_1[k]}} & wr_data[k])    |
                                ({32{~pending[src_1][k]}} & data_1[k]);
```

这是「**位掩码或**」式 mux：每个候选源用 32 位宽的掩码（`{32{cond}}`）选通，再或在一起。因为同一拍最多一个条件成立（生产者指令在某个时刻只处于一个流水段），或运算等价于优先级选择。最后一项 `{32{~pending[src_1][k]}} & data_1[k]` 是 VRF 直读——只有「非 pending」时才放行 VRF 的数据；pending 时这一项被掩成 0，迫使走转发（若转发也没有，则 `src1_ok=0`，本就不发射，数据是 0 也无所谓）。

`data_1[k]` 来自 VRF 的元素级读端口（见 u2-l4）。所以这个 mux 就是「**转发优先，否则读 VRF**」的硬件实现。

**解锁接口端口**：[rtl/vector/vis.sv:50-54](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L50-L54)

```systemverilog
//Unlock ports
input  logic                                                                       unlock_en    ,
input  logic          [$clog2(VECTOR_REGISTERS)-1:0]                               unlock_reg_a ,
input  logic          [$clog2(VECTOR_REGISTERS)-1:0]                               unlock_reg_b ,
input  logic          [      VECTOR_TICKET_BITS-1:0]                               unlock_ticket,
```

一次最多解锁两个寄存器（`reg_a`、`reg_b`）——因为一条 load 既消费源（src2）又产生目的（dst），完成时要同时解锁两个。配合 4.1.3 的 `StatusLocked` RELEASE 分支（`unlock_reg_a_oh[i]` / `unlock_reg_b_oh[i]` + `ticket_match_locked`），整个 acquire-release 闭环就成立了。

**写回掩码——locked 也保护 VRF 写**：[rtl/vector/vis.sv:358-364](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L358-L364)

```systemverilog
logic [VECTOR_LANES-1:0] wr_en_masked;
always_comb begin : WBmask
    for (int i = 0; i < VECTOR_LANES; i++) begin
        wr_en_masked[i] = wr_en[i] & ~locked[wr_addr][i];
    end
end
```

这是 locked 表的「第二个用途」：vEX 写回（`wr_en`）如果指向一个还被访存锁着的寄存器元素，会被 `& ~locked` 屏蔽掉，不写进 VRF。直觉解释：该元素正被访存占用，计算通路不该擅自改写它。这也解释了为什么 `wr_en` 能同时「转发数据」又能「清 pending」——转发用的是原始 `wr_en`，而真正写 VRF 用的是 `wr_en_masked`，两者被 locked 区分开。

#### 4.3.4 代码实践（源码阅读型 · 解耦执行预演）

**目标**：用一条 `vld → vadd` 的序列，预演 lock/unlock 与 ticket 如何让「取数」与「计算」两条流异步推进，为 u4-l1 打基础。

**场景设定**（VECTOR_LANES=8）：

- 指令 L：`vld v3, (addr)`，load，ticket=7，lock=`2'b11`（lock[0]=1 消费源、lock[1]=1 产生目的），`dst_iszero=0`。
- 指令 C：`vadd v4, v3, v1`，ticket=8，src1=v3（RAW 于 L 的目的）。

**操作步骤**：

1. L 在 vIS 发射：
   - 因 lock[1]=1，`locked[v3][0..7]` 置 1，`locked_ticket[v3]=7`（4.1.3 StatusLocked 的 ACQUIRE-目的 分支）。
   - 因 lock[0]=1，src2 被锁（ACQUIRE-源 分支，注意 src1 当前未启用）。
   - 因非 dst_iszero，`pending[v3][0..7]` 也置 1，`pending_ticket[v3]=7`（StatusPending）。
   - L 走访存岔路进入 vMU，**不进 vEX**。
2. C 进来，src_1=v3：
   - `src1_ok[p]`：`pending[v3][p]=1` 且此刻无转发（load 数据还没回）→ `src1_ok[p]=0` → C **停顿**（等 load 数据）。
3. 一段时间后，vMU 把 load 数据写回：驱动 `mem_wr_en / mem_wr_addr=v3 / mem_wr_ticket=7`：
   - StatusPending 的 RELEASE-b 分支命中（ticket 匹配）→ `pending[v3]` 清 0。
   - 此后 C 的 `src1_ok` 经 `~pending` 路径变 1 → C **可以发射**（前提是 v3 的元素已进 VRF）。
4. vMU 访存引擎真正完成时，驱动 unlock：`unlock_en=1, unlock_reg_a=v3, unlock_ticket=7`：
   - StatusLocked RELEASE 分支命中 → `locked[v3]` 清 0 → 释放资源，后续访存指令可再用 v3。

**需要观察的现象 / 预期结果**：

- `pending[v3]` 由 **vMU 的 load 写回 (`mem_wr`)** 清除——它管「数据是否进 VRF」。
- `locked[v3]` 由 **vMU 的 unlock** 清除——它管「寄存器是否还被访存占用」。
- 两者**不是同一信号**清除，时刻可能不同：C 只要 `pending` 清零（数据到了）就能算，不必等 `unlock`（资源释放）；而下一条**访存**指令要重用 v3，才需要等 `locked` 清零。
- 这正是「取数流（vMU）」与「计算流（vEX）」**解耦**的硬件基础：计算只看 pending，访存互斥看 locked，ticket 保证比对的是同一次访存。

> 本实践为源码阅读与推演型；完整时序图与波形验证留到 u4-l1（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：转发命中为什么要比对 ticket？只比 `addr` 不够吗？

> **答案**：不够。物理寄存器号会被反复重用（硬件循环展开用不同物理块，但 32 个号迟早绕回）。只比 `addr` 可能把「同名寄存器的某次历史/未来写入」误当作当前生产者。加上 `ticket===pending_ticket[src]`，保证命中的是「我正在等的那一次生产」。

**练习 2**：`wr_en_masked = wr_en & ~locked[wr_addr]`。如果删掉这个屏蔽，直接用 `wr_en` 写 VRF，会在 `vld→vadd` 场景里出什么问题？

> **答案**：在 `locked[v3]` 还没被 unlock 清掉时，若有别的计算指令试图写 v3，就会和访存对 v3 的占用冲突，把访存还没读完/还没写完的数据覆盖掉。屏蔽确保「被访存锁着的元素，计算写回不得擅入」，维护 lock 的排他性。

---

## 5. 综合实践

把本讲三个模块串起来，做一个完整的状态推演。继续用 VECTOR_LANES=8， VL=8。

**任务**：手工推演下面 3 条指令在 vIS 计分板里的状态变化，并解释每条指令「为什么能/不能在那一拍发射」。

```text
I1: vadd  v3, v1, v2      ticket=3   (整数, lock=00, dst_iszero=0)
I2: vadd  v4, v3, v1      ticket=4   (RAW: src1=v3 是 I1 的目的)
I3: vld   v5, (addr)      ticket=5   (load, lock=11, dst_iszero=0)
```

**推演要求**：

1. **画出每条指令发射后**，`pending`、`locked`、`pending_ticket`、`locked_ticket` 四张表里被改动的项。
2. **判断 I2 相对 I1 的 RAW**：在 I1 尚未写回时，I2 的 `src1_ok` 各路径取值是什么？三个转发点（EX1/EX3/写回）如何让 I2 尽早发射？参考 4.2.4。
3. **判断 I3 的 lock 行为**：I3 发射时哪些表项被 ACQUIRE？它的目的 v5 何时能被后续计算指令读（pending 何时清）、何时能被后续访存指令重用（locked 何时清）？参考 4.3.4。
4. **回归 idle**：写出 [rtl/vector/vis.sv:397](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L397) 的 `is_idle_o = ~valid_in & ~|pending & ~|locked` 在本序列里何时才为真（提示：必须等 I3 的 unlock 到达）。

**预期结果（自检要点）**：

- I1 发射：`pending[v3]` 全置 1、`pending_ticket[v3]=3`；`locked` 不变（整数指令 lock=00）。
- I2：靠转发（或等 I1 写回清 `pending[v3]`）解除 RAW；一旦 `can_issue=1` 即发射，并置 `pending[v4]`、`pending_ticket[v4]=4`。
- I3 发射：`pending[v5]` 置 1（`pending_ticket[v5]=5`）**且** `locked[v5]` 置 1（`locked_ticket[v5]=5`）；I3 走 vMU 不进 vEX。
- `is_idle_o` 只有在 I1/I2 的 `pending` 全清、**且** I3 收到 unlock 使 `locked[v5]` 清零、**且** 入口 `valid_in=0` 时才为 1——这印证了 u2-l1 里「任何一条访存未 unlock 都会拖低整体空闲」。

> 若你想在仿真里验证推演，可在 u1-l5 的 `examples/` 下仿照 vvadd 写一段含 `vadd→vadd→vld` 的 CSV，跑仿真后用波形核对 `pending/locked`（信号在 vis 内部，需在 `wave_simulator.do` 里手动添加，待本地验证）。

## 6. 本讲小结

- vIS 用**两张逐元素位矩阵** `pending[32][8]` / `locked[32][8]` + **两套 per-register ticket** 构成 per-element 计分板，支撑变延迟下的**部分写完成**。
- `pending` 服务**计算 RAW**（由 vEX 写回 `wr` 或 vMU load 写回 `mem_wr` 清除）；`locked` 服务**访存资源互斥**（只能由 vMU `unlock` 清除）。一张管数据就绪，一张管资源占用，分而治之。
- 冒险检测逐 lane 判定 `src1_ok / src2_ok / rdst_ok` → `no_hazards`，再 `&issue_masked`（用 `vl_therm` 屏蔽无效 lane）归约成 `can_issue`。源就绪有三条等价路径：零寄存器 / 转发命中 / 非 pending。
- **三处转发点** frw_a（EX1）/ frw_b（EX3）/ wr=frw_c（写回）各带 `en+addr+data+ticket`，命中靠 `addr===src & ticket===pending_ticket[src]`；数据 mux「转发优先，否则读 VRF」，转发命中信号同时驱动 `src_ok`，保证判定与取值一致。
- 访存指令走 `can_issue_m`（只查 locked），发射时按 lock 位 **acquire**，vMU 完成 `unlock` 时 **release**；ticket 跨计算路与访存路消歧，是解耦执行的硬件基础。
- `exec_finished = ~(|pending) & ~(|locked)` 是重配前的排空屏障；`is_idle_o` 还要求入口无新指令，故任何在飞访存都会让向量核非空闲。

## 7. 下一步学习建议

- **u2-l6 vIS 硬件循环展开与掩码**：本讲的 `current_exp_loop`、`vl_therm`、`expansion_finished` 都服务于「一条指令展开成多个 uop」。下一讲正面讲展开次数怎么算、归约指令的特殊目标偏移、以及 `v1[mask_src]` 如何门控写回。
- **u2-l7 vEX 与 vex_pipe 执行流水**：本讲把三个转发点当「黑盒 inbound 总线」。下一讲进 vEX 内部，看 EX1→EX4 的数据通路、转发点具体在哪一级引出、以及 FU 如何路由到 int/fp/fxp ALU。
- **u4-l1 解耦执行与 acquire-release 语义**：本讲埋下的 lock/unlock + ticket 伏笔，在 u4-l1 上升为完整的「解耦执行」模型，用一张时序图把 vld→vadd 两条流的异步推进讲透。
- **延伸阅读**：对照 [rtl/vector/vrrm.sv:89-93](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L89-L93) 复习 lock 编码来源；对照 [rtl/vector/vector_top.sv:238-298](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L238-L298) 看 vIS 的全部对外端口如何与 vEX、vMU 接驳。
