# 转发网络与变延迟执行

## 1. 本讲目标

本讲是专家层对向量执行级（vEX）的「内功」深入。在 u2-l7 里我们看到了 `vex_pipe` 的四级流水线骨架，在 u2-l5 里看到了 `vis` 计分板如何用 `pending` 位挡住冒险。本讲要回答两个把它们粘起来的关键问题：

1. **生产者的结果还没写回寄存器堆时，消费者凭什么能提前拿到数据？** —— 答案是**转发网络（forwarding network）**，而转发点（forward point）选在哪一级，是一个可以在 `params.sv` 里拧的旋钮。
2. **不同指令延迟不同（加法 1 拍、乘法 3 拍、除法 4 拍），计分板怎么和这种「变延迟」协作？** —— 答案藏在 `ready_res_*` 链与 `pending` 清零时机的配合里。

学完后你应该能够：

- 说清楚 `VECTOR_FWD_POINT_A/B` 两个旋钮的可取值、它们的整数编码规则，以及改它们会牵动哪些代码。
- 解释 `_F`（flopped，寄存型）转发变体为什么能提升主频、代价又是什么。
- 用一张图把「生产者在 EX 级产生结果 → 转发总线 → `vis` 命中 → 操作数 mux → 消费者进 EX1」这条组合路径画出来，并指出长组合路径的瓶颈在哪。
- 解释变延迟如何与计分板的 `pending` 位协作：转发是「乐观快通道」，写回才是 `pending` 的「官方清零点」。

## 2. 前置知识

本讲默认你已经读过 u2-l5（`vis` 计分板）和 u2-l7（`vex`/`vex_pipe` 执行流水）。为了自洽，先用最朴素的语言重温三个概念。

**转发（forwarding / bypass）**。考虑两条紧邻的向量指令：`P: vadd v2,v0,v1`（生产者，结果写 v2）和 `C: vadd v3,v2,v4`（消费者，要读 v2）。如果没有转发，`P` 必须一路走到写回级（WR）把 v2 写进 VRF，`C` 才能从 VRF 读到新值——这中间 `C` 只能干等，因为计分板里 v2 的 `pending` 位一直是 1。**转发**就是在 `P` 还没走到 WR 时，从 EX 流水线的中间级把结果「抄」出来，直接喂给 `vis` 里正在等待的 `C`，让 `C` 提前发射。其本质是**绕过「写 VRF → 读 VRF」这一圈往返**。

**计分板（scoreboard）与 `pending` 位**。`vis` 给每个物理向量寄存器的每个元素维护一个 `pending` 位：某条指令一旦发射，它的目的寄存器 `pending` 置 1；结果写回时清 0。消费者若发现源寄存器 `pending=1`，就被挡住（RAW 冒险）。所以 `pending` 是冒险检测的「保守默认闸」。

**变延迟（variable latency）**。在 `vex_pipe` 里，所有指令都走同一条 EX1→EX2→EX3→EX4→WR 的物理流水（等长），但不同运算「最早能给出可用结果」的级不同：简单整数运算 EX1 一拍就出，乘法要到 EX3，除法要到 EX4。所谓变延迟，不是流水线深度不同，而是**结果最早可转发/可消费的拍数不同**。

> 关键直觉：转发网络是「等不及写回」的快通道；变延迟决定了这条快通道对不同指令「最早何时打开」。本讲就是讲这两件事如何拧在一起。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [rtl/shared/params.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/params.sv) | 全局参数中央配置台 | 两个转发点旋钮 `VECTOR_FWD_POINT_A/B` 的默认值与注释 |
| [rtl/vector/vmacros.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv) | 宏定义头文件 | `EX1..EX4` 与 `EX2_F..EX4_F` 的整数编码，以及「non-flopped hurt freq」这条设计箴言 |
| [rtl/vector/vex_pipe.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv) | 单 lane 执行流水 | 转发点的 `generate` 八分支选择、`ready_res_*` 累积链、`data_exN` 短路 |
| [rtl/vector/vis.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv) | 计分板与发射 | 转发命中判定、操作数 mux、`src*_ok` 与 `pending` 清零 |

辅助理解（非本讲重点，但会引用）：[rtl/vector/vex.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv)（转发地址/票号路由）、[rtl/vector/v_int_alu.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv)（`ready_res_*` 变延迟源头）、[rtl/vector/vector_top.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv)（把 `vis` 与 `vex` 的转发总线对接）。

---

## 4. 核心概念与源码讲解

### 4.1 转发点配置：从需求到可调旋钮

#### 4.1.1 概念说明

转发要解决的核心矛盾是：**结果在 EX 流水里往前流，而消费者在 `vis` 里等着用**，二者空间上分离。解法是在 EX 的某一级「拉一根抽头线」，把当前这一级的结果连同它的目的寄存器号、ticket 一起广播到一条**转发总线**上；`vis` 在决定能否发射消费者时，顺手查一下这条总线，若「广播的寄存器号 === 我的源寄存器号」且「广播的 ticket === 我期望的生产者 ticket」，就把广播的数据直接选进操作数，消费者即刻可发射。

本设计给了一条 **三个转发点**的转发总线（见 `vis` 端口）：

- **Forward Point #1（`frw_a`）**：可配置，默认接 EX1。
- **Forward Point #2（`frw_b`）**：可配置，默认接 EX4_F。
- **Forward Point #3（`wr`/`frw_c`）**：写回级，**始终存在、不可配置**，是兜底。

「可配置」的意思是：A、B 两个点各自可以接 EX1/EX2/EX2_F/EX3/EX3_F/EX4/EX4_F 中的任意一个（或 none）。接哪一级，是综合后**主频 vs 冒险解除速度**的权衡——这正是本讲反复强调的主线。

#### 4.1.2 核心流程

一条从生产者 `P` 到消费者 `C` 的转发链路，一拍内的组合数据流如下：

```text
P 在 EXn 级:
  vex_pipe  ──► frw_{a,b}_en/data   (组合或寄存, 取决于是否 _F)
  vex       ──► frw_{a,b}_addr/ticket (P 的 dst/ticket, 按所选级寄存)
                         │
                         ▼
vis (同拍评估 C 是否可发射):
  ① 命中判定: frw_x_src = frw_x_en & (frw_x_addr===src) & (frw_x_ticket===pending_ticket[src])
  ② 供数:    data1 = frw_a_hit?frw_a_data : frw_b_hit?frw_b_data : wr_hit?wr_data : VRF读
  ③ 解险:    src1_ok = src1_iszero | frw_*_hit | ~pending[src1]
  ④ 发射:    can_issue = &(... & src1_ok & src2_ok & rdst_ok ...)
                         │
                         ▼
C 的操作数 data_to_exec 进入 vex 的 EX1
```

四个要点：

1. **三级合一**：转发命中信号 `frw_*_src` 同时用于「供数（②）」和「解险（③）」。同一根信号两用，保证了「判定能发射」与「取到的数据正确」绝不会矛盾。
2. **优先级**：操作数 mux 的优先级是 `frw_a > frw_b > wr > VRF`，即**最新的（抽头最早的）结果优先**。
3. **ticket 双重匹配**：因为寄存器重映射会让架构寄存器号被复用（见 u2-l3），光比寄存器号不够，必须再比 ticket，才能确认这条转发确实是「我这个版本」的生产者发出的。
4. **写回是 `pending` 的官方清零点**：转发只是让消费者「无视 `pending`」提前走；`pending` 位本身要等 `wr_en`（写回）才清零（见 4.3）。

#### 4.1.3 源码精读

**旋钮在哪：** 两个转发点是 `params.sv` 里的 `localparam`，默认 A 接 EX1、B 接 EX4_F，注释里列出了全部合法取值：

[rtl/shared/params.sv:L86-L97](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/params.sv#L86-L97) — 这是「转发点是什么、能取什么」的权威说明。注意注释把 `EX2_F` 写成 `20`、`EX4_F` 写成 `40`，这直接对应宏定义里的整数值。

```systemverilog
localparam int VECTOR_FWD_POINT_A = `EX1;
localparam int VECTOR_FWD_POINT_B = `EX4_F;
```

**宏的整数编码：** 为什么 `EX2_F` 是 20 而不是 5？这是设计者的小技巧——用一个整数同时编码「第几级」和「是否寄存」：

[rtl/vector/vmacros.sv:35-L44](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv#L35-L44) — 非 flopped 用级数本身（1/2/3/4），flopped 用级数乘以 10（20/30/40）。这样一个 `int` 参数就能在 `generate` 里用 `==` 精确区分七种抽头位置。注释 `_F stands for flopped` / `non-flopped hurt freq` 是整段设计的灵魂，下一节展开。

```systemverilog
`define EX1   1
`define EX2   2
`define EX2_F 20
`define EX3   3
`define EX3_F 30
`define EX4   4
`define EX4_F 40
```

编码规律可写成 \(\text{value} = n\)（非 flopped）或 \(\text{value} = 10n\)（flopped），其中 \(n\in\{1,2,3,4\}\) 是级号。注意**没有 `EX1_F`**：EX1 是第一级，它的输入就是发射级送来的组合逻辑，再 flop 就等于多加一级流水了。

**`vex_pipe` 里的选择器：** 两个转发点各有一段 `generate if/else if`，在** elaboration 阶段**就把对应级的 `assign` 选出来，其余分支被剔除，零运行时开销。以 Forward Point #1 为例：

[rtl/vector/vex_pipe.sv:474-L518](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv#L474-L518) — 八个分支（EX1/EX2/EX2_F/EX3/EX3_F/EX4/EX4_F/none）。默认配置 `FWD_POINT_A==EX1`，命中第一个分支：

```systemverilog
generate if (FWD_POINT_A == `EX1) begin :g_fwd_pnt_a_ex1
    assign frw_a_en_o   = ready_res_int_ex1;            // EX1 组合结果就绪
    assign frw_a_data_o = res_int_ex1[0 +: DATA_WIDTH]; // EX1 组合结果数据
```

EX1 分支只认 `ready_res_int_ex1`（仅简单整数运算与归约第 1 级在 EX1 就绪），所以**只有 1 拍运算能从 EX1 转发**——乘除法此时还没算完，不会误转发。

**`vex` 顶层的地址/票号路由：** 转发点选在哪一级，不仅决定数据从哪个 `data_ex*`/`res_*_ex*` 取，还决定广播哪个 `dst`/`ticket`。`vex.sv` 里有一段与之配套的 `generate`：

[rtl/vector/vex.sv:222-L254](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L222-L254) — EX1 用当前输入的 `exec_info_i.dst`（组合），EX2/EX2_F 共用寄存的 `dst_ex2`，依此类推。注意**同一级的 flopped 与非 flopped 共用同一个 `dst_exN`/`ticket_exN` 寄存器**，区别只在数据来源。

```systemverilog
generate if (FWD_POINT_A == `EX1) begin :g_fwd_pnt_a_ex1
    assign frw_a_addr   = exec_info_i.dst;     // 当前拍进入 EX1 的指令
    assign frw_a_ticket = exec_info_i.ticket;
end else if (FWD_POINT_A == `EX2_F | FWD_POINT_A == `EX2) begin :g_fwd_pnt_a_ex2
    assign frw_a_addr   = dst_ex2;              // 上一拍进 EX1、本拍在 EX2 的指令
    assign frw_a_ticket = ticket_ex2;
```

**`vis` 端的命中与供数：** 转发总线到了 `vis`，先做命中判定，再喂操作数 mux：

[rtl/vector/vis.sv:230-L241](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L230-L241) — 三处转发点的命中判定完全对称，都是「en & 寄存器号相等 & ticket 相等」三条件与。`===` 是四态严格相等，避免 X 误命中。

```systemverilog
assign frw_a_src_1[j] = frw_a_en[j] & (frw_a_addr === src_1) & (frw_a_ticket === pending_ticket[src_1]);
```

[rtl/vector/vis.sv:207-L228](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L207-L228) — 操作数 mux，优先级 `frw_a > frw_b > wr > VRF`。用 `{32{hit}} & data` 做位掩码按位或实现多路选择，是本工程常见的写法。

```systemverilog
assign data_to_exec[k].data1 = ({32{frw_a_src_1[k]}} & frw_a_data[k]) |
                               ({32{frw_b_src_1[k]}} & frw_b_data[k]) |
                               ({32{frw_c_src_1[k]}} & wr_data[k])    |
                               ({32{~pending[src_1][k]}} & data_1[k]);
```

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：把「转发点配置如何一路传导到硬件」这条链走通。
2. **步骤**：
   - 在 `rtl/shared/params.sv` 找到 `VECTOR_FWD_POINT_A/B` 两个 `localparam`，记下默认值（`EX1`、`EX4_F`）。
   - 全局搜索 `FWD_POINT_A`，确认它作为 `parameter` 一路从 `vector_top` → `vex` → `vex_pipe` 下传（在 `vector_top.sv:350-L351` 例化 `vex` 时传入）。
   - 在 `vex_pipe.sv` 的 Forward Point #1 段（L479 起）数一下共几个 `else if` 分支，对应几种合法配置。
   - 在 `vis.sv` 找到 `frw_a_en`/`frw_a_addr`/`frw_a_data`/`frw_a_ticket` 四个端口，确认它们正好是 `vex` 那段 `generate` 的输出对接过来的。
3. **观察现象**：你会看到「一个 `localparam` 整数 → 宏值匹配 → elaboration 时选一组 `assign` → 跨模块接到 `vis` 的命中逻辑」这条完整路径，中间没有任何寄存器或 mux2，纯组合 + 编译期选择。
4. **预期结果**：能口述「改 `VECTOR_FWD_POINT_A` 的值，`vex_pipe` 里被综合的 `assign` 就换一组，`vis` 的命中逻辑代码不变」。
5. 运行结果：**待本地验证**（综合或 elaboration 后查看网表/原理图可确认分支裁剪）。

#### 4.1.5 小练习与答案

**Q1**：如果想让 Forward Point #1 完全关闭（不转发，只靠写回），`VECTOR_FWD_POINT_A` 应设为什么？对应的 `assign` 会变成什么样？

> **答**：设为任何不等于七个宏值的整数（代码里没有专门的 none 宏，但 `generate` 末尾的 `else :g_fwd_pnt_a_none` 分支会兜底，见 `vex_pipe.sv:515-L517`），`frw_a_en_o=1'b0`，`frw_a_data_o='x`。`vis` 端 `frw_a_en[j]=0` 导致 `frw_a_src_*` 恒为 0，该转发点失效。

**Q2**：为什么 `vis` 的命中判定要用 `===`（四态相等）而不是 `==`？

> **答**：因为 `frw_a_addr` 在 none 分支被赋为 `'x`，且未初始化的 `pending_ticket` 也可能是 X。用 `==` 时 X 参与比较会得到 X，在 `if` 里被当作假还好，但作为位向量参与 `&` 时可能污染结果；`===` 要求四态严格相等，X 只匹配 X，行为确定、便于仿真排查。同时也防止「地址恰好为 X 时误命中」。

---

### 4.2 `_F`（flopped）变体与频率取舍

#### 4.2.1 概念说明

`vmacros.sv` 那句 `non-flopped hurt freq`（非寄存型伤主频）是本节的全部主题。

一条**非 flopped** 的转发点（如 `EX2`）直接把 EX2 级的**组合结果** `res_int_ex2` 拉到转发数据线上。`res_int_ex2` 是什么？对于乘法，它是「四个字节部分积对齐求和」的组合输出（`v_int_alu` 里一长串加法）；对于除法，是「逐位组恢复除法」的组合输出。这些组合块本身就深，再把它们的输出送到 `vis` 的命中比较器、再送进操作数 mux、再进消费者 `vex_pipe` 的 EX1——这条**跨模块的长组合路径**很容易成为关键路径，压低芯片主频（fmax）。

**flopped** 变体（`EX2_F`/`EX3_F`/`EX4_F`）的解法是：不接组合结果，改接**已经寄存好的数据触发器** `data_ex{N-1}`。由于触发器输出是「现成的」，组合路径被一刀切断，长度大幅缩短，主频得以提升。代价是：被转发的数据必须「上一拍就已经算好并锁存」，因此 flopped 变体本质上是**晚一拍**的转发，且只能转发那些「在更早的级就已就绪」的结果。

一句话总结这条取舍：

| | 非 flopped（`EXn`） | flopped（`EXn_F`） |
|---|---|---|
| 数据来源 | 组合结果 `res_*_exN` + 多路 mux | 已寄存的 `data_ex{N-1}` |
| 组合路径 | 长（含本级 ALU 计算）→ **伤主频** | 短（从触发器出发）→ **保主频** |
| 能转发的结果 | 本级及之前就绪的所有结果 | 仅「上一级或更早」就绪的结果 |
| 相对延迟 | 早一拍 | 晚一拍 |

#### 4.2.2 核心流程

以 `EX2` 与 `EX2_F` 的对比说明「切断组合路径」是怎么做到的：

```text
非 flopped EX2:
  data_a/b ──► [EX2 ALU 组合计算(部分积求和等)] ──► res_int_ex2 ──┐
                                                                 ├─► 4:1 mux ──► frw_a_data (组合, 长路径)
  data_ex1(寄存) ──────────────────────────────────────────────┘

flopped EX2_F:
  data_ex1(寄存) ──► frw_a_data   (从触发器直出, 短路径)
  ready_res_ex2(寄存) ──► frw_a_en
```

关键在于：flopped 分支的 `assign` 既不读 `res_int_ex2`，也不读 `valid_int_ex2` 这些组合量，只读 `data_ex1`（数据触发器）和 `ready_res_ex2`（就绪标志触发器），二者都是上一个时钟沿已锁存的值。组合路径从「EX2 整个 ALU」缩短为「一个 2:1 mux」，关键路径自然变短。

#### 4.2.3 源码精读

先看非 flopped `EX2` 分支——注意它的 `en` 是四个就绪信号的或、`data` 是五路 mux（含 `res_int_ex2` 这个重型组合源）：

[rtl/vector/vex_pipe.sv:483-L492](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv#L483-L492)

```systemverilog
end else if (FWD_POINT_A == `EX2) begin :g_fwd_pnt_a_ex2
    assign frw_a_en_o   = ready_res_ex2 | ready_res_int_ex2 | ready_res_fp_ex2 | ready_res_fxp_ex2;
    assign frw_a_data_o = ~mask_ex2         ? '0                           :
                          ready_res_ex2     ? data_ex1[0    +: DATA_WIDTH] :  // 早就绪: 直通
                          ready_res_int_ex2 ? res_int_ex2[0 +: DATA_WIDTH] :  // 本级刚算出(组合!)
                          ready_res_fp_ex2  ? res_fp_ex2[0  +: DATA_WIDTH] :
                                              res_fxp_ex2[0 +: DATA_WIDTH];
end else if (FWD_POINT_A == `EX2_F) begin :g_fwd_pnt_a_ex2_f
    assign frw_a_en_o   = ready_res_ex2;                              // 只认"上一级就绪"
    assign frw_a_data_o = ~mask_ex2 ? '0 : data_ex1[0 +: DATA_WIDTH]; // 只用寄存值
```

对比之下，`EX2_F` 分支只有两行、数据只来自 `data_ex1`。这就是「non-flopped hurt freq」的具体含义：`res_int_ex2` 这条线在非 flopped 分支里进入了转发数据 mux，进而进入 `vis` 的比较器和操作数 mux，形成长组合链；flopped 分支里它彻底缺席。

**默认配置为何是 A=EX1、B=EX4_F：** 这是一组很讲究的搭配：

- **A=EX1**：EX1 是第一级，本身没有「上一级寄存」可接，想最早转发就只能用组合 `res_int_ex1`。设计者把这条组合路径接受为关键路径，换取**1 拍运算的背靠背零等待转发**（最常见的 `vadd` 链）。
- **B=EX4_F**：第二个点放在最后一级、且用 flopped，数据来自 `data_ex3`（寄存）。它专门用来「兜住那些 2–3 拍才就绪的运算（如乘法，EX3 就绪后被锁进 `data_ex3`）」，同时因为走寄存、不碰 EX4 的除法组合逻辑，**不拖累主频**。

[rtl/vector/vex_pipe.sv:512-L514](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv#L512-L514) — 默认的 B 点（`EX4_F`）实现：

```systemverilog
end else if (FWD_POINT_A == `EX4_F) begin :g_fwd_pnt_a_ex4_f   // (B 点同构, 见 L555)
    assign frw_a_en_o   = ready_res_ex4;                              // 累积到 EX3 的就绪
    assign frw_a_data_o = ~mask_ex4 ? '0 : data_ex3[0 +: DATA_WIDTH]; // 寄存值
```

**一个重要后果**：`ready_res_ex4` 是「EX3 及之前就绪」的累积值（见 4.3.3 的链式定义），它**不含** `ready_res_int_ex4`（除法，EX4 才就绪）。所以默认配置下，**除法结果无法经 `frw_b` 转发，只能等写回（`frw_c`）**。若想让除法也能被第二个点转发，必须把 B 改成非 flopped 的 `EX4`，接纳 `res_int_ex4` 进组合路径——用主频换除法的转发延迟。

#### 4.2.4 代码实践（源码阅读型 + 参数修改）

1. **目标**：亲手「看见」flopped 与非 flopped 在数据来源上的差别，并理解为何后者伤主频。
2. **步骤**：
   - 在 `vex_pipe.sv` 并排打开 `g_fwd_pnt_a_ex2`（L483）与 `g_fwd_pnt_a_ex2_f`（L490），对比 `frw_a_data_o` 的右值：前者是五路 mux 且含 `res_int_ex2`，后者只有 `data_ex1`。
   - 在 `v_int_alu.sv` 找到 `result_mul_ex2` 的定义（L350-L358，四个部分积扩展相加），体会 `res_int_ex2` 背后的组合深度。
   - **修改实验（仅本地，勿提交）**：把 `params.sv` 里 `VECTOR_FWD_POINT_B` 从 `` `EX4_F `` 改成 `` `EX4 ``，重新 elaboration。
3. **观察现象**：elaborate 后，`vex_pipe` 里 Forward Point #2 被选中的分支从 `g_fwd_pnt_b_ex4_f` 变成 `g_fwd_pnt_b_ex4`，`frw_b_data_o` 的来源从 `data_ex3`（寄存）变成含 `res_int_ex4`（除法组合结果）的 mux。
4. **预期结果**：逻辑上除法结果从此可经 `frw_b` 转发；但 `res_int_ex4 → frw_b_data → vis 比较 → 操作数 mux → 消费者 EX1` 这条组合路径变长，综合后该路径的 slack 应变小（主频潜力下降）。具体 slack 数值**待本地验证**（需运行综合）。
5. **注意**：此修改仅为观察，完成后请还原 `params.sv`，不要改动源码仓库。

#### 4.2.5 小练习与答案

**Q1**：为什么不存在 `EX1_F` 这个宏？

> **答**：flopped 的意义是「接上一级已寄存的数据」。EX1 是第一级，它的输入直接来自 `vis` 的发射逻辑（`data_to_exec`），中间没有数据触发器；要 flop EX1 的结果就等于在 EX1 前再加一级流水寄存器，改变了流水线深度。所以最早的 flopped 点是 `EX2_F`（接 `data_ex1`）。

**Q2**：默认 B=`EX4_F` 转发不了除法，会不会导致除法相关的程序出错？

> **答**：不会出错，只会慢。除法结果在 EX4 算出后，会正常走完到 WR 级写回，届时第三个转发点 `wr`（`frw_c`，始终存在）会把它转发出去，并把 `pending` 清零。消费者只是多等几拍，功能完全正确。这正是「写回是兜底」的设计意图。

---

### 4.3 变延迟与计分板协作

#### 4.3.1 概念说明

「变延迟」在本设计里是一个容易误解的词。它**不是**指不同指令走不同深度的流水线——所有指令都走满 EX1→EX2→EX3→EX4→WR 这五级。它指的是：**结果「最早可被转发/消费」的级，因运算类型而异**。

| 运算类型 | 最早就绪级 | 就绪标志 | 典型指令 |
|----------|-----------|----------|----------|
| 简单整数 ALU | EX1 | `valid_int_ex1` | VADD、VSUB、VAND、VSLL… |
| 乘法 | EX3 | `valid_mul_ex3` | VMUL、VMULH |
| 除法 | EX4 | `valid_div_ex4` | VDIV、VREM |
| 归约树 | EX1..EX4（按 tree 深度） | `valid_rdc_exN` | VRADD、VRAND |

「就绪」的硬件含义是：从这一级起，结果数据是正确的，可以被转发或被后续级使用。在此之前，对应级的数据寄存器里是未完成的中间值。

计分板与变延迟的协作关系是：

- **`pending` 位是保守闸**：它只在一个地方被清零——写回（`wr_en`）命中时。也就是说，从计分板的视角，**所有指令的结果都是在 WR 级「正式可用」**，与变延迟无关。
- **转发是乐观快通道**：`src_ok` 里除了 `~pending`，还有一条「转发命中」的或项。只要任一转发点命中，即使 `pending` 还是 1，源也被视为就绪、消费者可立即发射。
- **变延迟决定快通道何时打开**：转发点的 `en` 信号归根结底来自 `ready_res_*`，而 `ready_res_*` 的源头正是上表里的 `valid_int_ex1`/`valid_mul_ex3`/`valid_div_ex4`。所以加法 1 拍就开快通道、乘法 3 拍、除法 4 拍（或等写回）。

#### 4.3.2 核心流程

变延迟在硬件上靠两个机制叠加实现：

1. **`ready_res_*` 累积链**：每一级把「上一级的就绪」与「本级新就绪」或起来，向后传递。于是 `ready_res_exN` 表示「结果在 EXN 或之前就已就绪」。
2. **数据寄存器短路（short-circuit）**：一旦 `ready_res_exN` 为真，本级的数据寄存器就不再保存本级 ALU 的输出，而是直接直通上一级的数据（`data_exN <= data_ex{N-1}`）。这样最早就绪的结果会一路「滑」到 WR，而中途各级的 ALU 输出被忽略。

二者合起来的效果：流水线对每条指令都是五级等长（写回时机固定），但**结果的「最早可见拍」随运算类型提前**，转发点据此采样。

```text
变延迟 + 计分板 时序示意 (P: vadd → C: 依赖 P):

拍 T    : vis 发射 P 进 EX1; 计分板置 pending[P.dst]=1
拍 T+1  : P 在 EX1, res_int_ex1 已就绪 → EX1 转发点(A=EX1)广播 P 的结果
          vis 同拍评估 C: frw_a 命中 → src_ok=1 (尽管 pending 还是 1!) → C 发射
拍 T+2..: P 继续滑向 WR; C 在 EX1...
拍 T+4  : P 到 WR, wr_en 命中 → pending[P.dst] 才被官方清零
```

注意：C 在 T+1 就因为转发而发射了，比 pending 清零（T+4）早了 3 拍——这就是转发带来的收益，也是「乐观快通道 vs 保守官方闸」的体现。

#### 4.3.3 源码精读

**变延迟的源头：** `v_int_alu` 的四个 `ready_res_*` 输出直接编码了「哪一级就绪」：

[rtl/vector/v_int_alu.sv:847-L869](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L847-L869)

```systemverilog
assign ready_res_ex1_o = valid_int_ex1  | valid_rdc_ex1;  // 简单 ALU + 归约第1级
assign ready_res_ex2_o = valid_rdc_ex2;                   // 仅归约第2级
assign ready_res_ex3_o = valid_mul_ex3  | valid_rdc_ex3;  // 乘法 + 归约第3级
assign ready_res_ex4_o = valid_div_ex4  | valid_rdc_ex4;  // 除法 + 归约第4级
```

这就是「加法 1 拍、乘法 3 拍、除法 4 拍」的硬件根源：只有对应类型的 valid 信号在对应级才为真。

**`ready_res_*` 累积链 + 数据短路：** 在 `vex_pipe` 里，这三个机制交织在一起。先看就绪标志如何累积与寄存：

[rtl/vector/vex_pipe.sv:301-L389](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv#L301-L389) — 每一级的 `ready_res_ex{N+1}` 寄存了「上一级累积就绪 | 本级 ALU 新就绪」：

```systemverilog
ready_res_ex2 <= ready_res_int_ex1 | ready_res_fp_ex1 | ready_res_fxp_ex1;          // L313
ready_res_ex3 <= ready_res_ex2 | ready_res_int_ex2 | ready_res_fp_ex2 | ready_res_fxp_ex2; // L350
ready_res_ex4 <= ready_res_ex3 | ready_res_int_ex3 | ready_res_fp_ex3 | ready_res_fxp_ex3; // L385
```

再看数据寄存器如何「短路」：

[rtl/vector/vex_pipe.sv:324-L336](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv#L324-L336)

```systemverilog
always_ff @(posedge clk) begin
    if(mask_ex2 | use_reduce_tree_ex2) begin
        if(ready_res_ex2) begin
            data_ex2 <= data_ex1;          // ← 短路: 早就绪的结果直通
        end else if(valid_int_ex2) begin
            data_ex2 <= res_int_ex2;       // 本级才就绪(如乘法在 EX2 还没好, 走这条)
        end else ...
    end
end
```

解读：若 `ready_res_ex2` 为真（结果在 EX1 就已算好，如加法），则 EX2 的数据寄存器直接搬 EX1 的数据（`data_ex1`），跳过 EX2 的 ALU；若为假（如乘法，EX2 仍未就绪），则保存本级 ALU 的中间结果。EX3、EX4 的数据寄存器同构（L359-L371、L451-L463）。最终 `data_ex4` 在 WR 前总是持有正确结果——最早就绪的运算靠一路短路「滑」过来，最晚的（除法）在 EX4 才填进去。

**计分板的 `pending` 清零（官方闸）：** `pending` 只在写回或访存写回时清零，与转发无关：

[rtl/vector/vis.sv:291-L314](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L291-L314)

```systemverilog
if(dst_oh[i] && vl_therm[k] && do_issue && !instr_in.dst_iszero) begin
    pending[i][k]     <= 1;                 // 发射时置位
    pending_ticket[i] <= instr_in.ticket;
end ...
else if(wr_en[k] && wr_addr_oh[k][i] && ticket_match_pending) begin
    pending[i][k] <= 0;                      // ← 仅写回清零
end else if (mem_wr_en[k] && mem_wr_addr_oh[k][i] && mem_ticket_match_pending) begin
    pending[i][k] <= 0;                      // 或访存写回清零
end
```

**转发的「解险」或项（乐观快通道）：** `src1_ok` 里 `frw_*_hit` 与 `~pending` 是或的关系：

[rtl/vector/vis.sv:243-L258](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L243-L258)

```systemverilog
assign src1_ok[p] = (instr_in.src1_iszero)                             |
                    (frw_a_src_1[p] | frw_b_src_1[p] | frw_c_src_1[p]) |  // ← 转发命中即就绪
                    (~pending[src_1][p]);                                 // ← 或已写回
assign no_hazards[p] = src1_ok[p] & src2_ok[p] & rdst_ok[p];
```

把 4.3.3 三段连起来读，就看到了完整的协作：`v_int_alu` 用 `valid_*` 标记就绪级 → `vex_pipe` 用 `ready_res_*` 链 + 数据短路让结果在正确的级可见、并被转发点采样 → `vis` 用转发命中提供「绕过 pending」的快通道 → `pending` 仍按部就班在写回时清零，作为正确性的保底。

#### 4.3.4 代码实践（调用链跟踪型）

1. **目标**：分别跟踪 VADD、VMUL、VDIV 三条指令，看清它们「最早就绪级」如何不同，以及消费者分别要等几拍。
2. **步骤**：
   - **VADD**：在 `v_int_alu.sv` 找到 `7'b0000001` 分支（L117），确认它令 `valid_int_ex1=valid_i` → `ready_res_ex1_o=1`。结论：EX1 就绪，默认 A=EX1 可立即转发。
   - **VMUL**：找到 `7'b0000111`（L266），`valid_mul_ex1` 在 EX1 置位，但 `ready_res_ex1_o` 不含 `valid_mul_ex1`；一路追 `valid_mul_ex2`→`valid_mul_ex3`，直到 `ready_res_ex3_o = valid_mul_ex3`（L860）。结论：EX3 就绪，默认配置下由 B=EX4_F（接 `data_ex3`）转发。
   - **VDIV**：找到 `7'b0001100`（L395），类似追到 `ready_res_ex4_o = valid_div_ex4`（L867）。结论：EX4 就绪，默认 B=EX4_F 的 `ready_res_ex4` 不含除法，**只能等写回 `wr` 转发**。
3. **观察现象**：三类指令的就绪标志分别只在 EX1/EX3/EX4 拉高；`ready_res_*` 累积链把它们正确地传到对应转发点。
4. **预期结果**：你能填出下面这张表。

   | 指令 | 最早就绪 | 默认配置由谁转发 | 若无转发需等到 |
   |------|---------|----------------|--------------|
   | VADD | EX1 | frw_a (EX1) | WR 写回 |
   | VMUL | EX3 | frw_b (EX4_F, 用 data_ex3) | WR 写回 |
   | VDIV | EX4 | frw_c (写回) | WR 写回 |

5. 精确的「消费者等待拍数」依赖于 uop 展开与程序交错，**待本地验证**（用 `results.log` 的 `stall_pending` 计数对照）。

#### 4.3.5 小练习与答案

**Q1**：既然所有指令都要走完到 WR 才清 `pending`，那变延迟还有什么意义？反正都得等到 WR？

> **答**：意义在于消费者**不必等到 WR**。`pending` 清零是「保守的最晚可用时刻」，但 `src_ok` 里的转发命中项让消费者在结果「最早就绪级」就能拿到数据并发射。加法链因此可以做到近乎背靠背发射，而不必每条等 4–5 拍写回。变延迟决定的是「快通道何时开」，不是「结果何时算完」。

**Q2**：`vex_pipe` 里 `data_ex2 <= data_ex1`（短路）与 `data_ex2 <= res_int_ex2`（本级结果）这两个分支，会不会在同一条指令上同时成立？

> **答**：不会。`if(ready_res_ex2) ... else if(valid_int_ex2) ...` 是互斥优先级（L326-L329）。对乘法而言，EX2 时 `ready_res_ex2=0`（还没就绪）、`valid_int_ex2=1`，走第二分支保存中间结果；到 EX3 就绪后，后续级的 `ready_res_ex3=1`，走第一分支直通。同一条指令在不同级走不同分支，但同一拍内只有一个生效。

---

## 5. 综合实践

把本讲三个最小模块（转发点配置、`_F` 变体与频率、变延迟与计分板协作）串起来，做一个**参数调优 + 影响分析**的实战。

**任务**：把转发点从默认的 `EX1 / EX4_F` 改为 `EX2 / EX3`，分析这一改动对（a）关键路径（主频）与（b）冒险解除速度的影响。

**操作步骤**：

1. **基线测量**：先用默认配置跑一个有 RAW 依赖的示例（如 `examples/dot_product`，它含连续乘加依赖），记录 `results.log` 里的 `total_cycles`、`stall_pending`，作为对照基线。若手头无 QuestaSim，可跳过实跑、做纯源码分析。
2. **修改配置**：在 `rtl/shared/params.sv` 把
   ```systemverilog
   localparam int VECTOR_FWD_POINT_A = `EX1;
   localparam int VECTOR_FWD_POINT_B = `EX4_F;
   ```
   改为
   ```systemverilog
   localparam int VECTOR_FWD_POINT_A = `EX2;
   localparam int VECTOR_FWD_POINT_B = `EX3;
   ```
   （仅本地实验，**勿提交**。）
3. **静态分析 A 点（`EX1 → EX2`）**：在 `vex_pipe.sv` 确认 Forward Point #1 现在选中 `g_fwd_pnt_a_ex2`（L483）。分析：
   - **频率影响**：数据源从 `res_int_ex1`（EX1 组合，较浅）变成含 `res_int_ex2`（EX2 组合，含乘法部分积求和）的 mux，组合路径变长 → **关键路径变差、主频下降**。
   - **冒险解除影响**：原本加法在 EX1 就能转发给下一拍发射的消费者；现在要等到生产者进 EX2 才转发 → **背靠背的加法链多等 1 拍**，`stall_pending` 会上升。
4. **静态分析 B 点（`EX4_F → EX3`）**：确认 Forward Point #2 选中 `g_fwd_pnt_b_ex3`（L537）。分析：
   - **频率影响**：从 flopped（接 `data_ex3`）变成非 flopped（接 `res_int_ex3` 组合）→ **组合路径变长、主频下降**。这是「non-flopped hurt freq」的直接体现。
   - **冒险解除影响**：乘法（EX3 就绪）现在能在生产者进 EX3 当拍就经 `frw_b` 转发，比原来（`EX4_F`，等进 EX4、且只用 `data_ex3`）**早 1 拍**，乘法链的 `stall_pending` 下降。
5. **综合权衡**：把两点的影响列表汇总，回答：这次改动是「用主频换冒险解除」还是反过来？对**乘法密集型**程序（如点积、FIR）和**加法密集型**程序（如 vvadd）分别有利还是有弊？

**需要观察的现象 / 预期结果**：

- 改成 `EX2/EX3` 后，两处转发点都变成非 flopped，**主频层面的代价是确定的（变差）**；收益是乘法类依赖的转发延迟降低 1 拍。
- 对乘法密集程序：`stall_pending` 下降，但若关键路径恶化导致主频降幅大于 stall 收益，实际 wall-clock 时间可能反而变长——这正是「转发点选择是主频与 IPC 的权衡」的体现。
- 对纯加法程序：A 点从 EX1 退到 EX2，加法链 stall 增加，且主频也变差，**双向变慢**，是明显的劣化。
- 具体数值（cycles、slack）**待本地验证**（需 QuestaSim 仿真 + 综合）。

**思考延伸**：如果你是设计者，面对「加法链」和「乘法链」两种典型负载，你会如何在这七个取值里为 A、B 选配？提示——A 点照顾高频的加法链（宜早，`EX1`），B 点照顾慢速乘除（宜覆盖到 EX3/EX4），且尽量用 `_F` 保主频。默认的 `EX1 / EX4_F` 正是这套思路的体现。

> 完成实验后请把 `params.sv` 还原，不要把实验改动留在仓库里。

## 6. 本讲小结

- **转发网络**用三处抽头（可配 A、可配 B、固定写回 C）把 EX 流水线中间级的结果直接喂给 `vis` 的操作数 mux，绕过「写 VRF → 读 VRF」的往返；命中判定靠「寄存器号 + ticket」双重匹配，消除寄存器复用歧义。
- **转发点配置**是 `params.sv` 里两个 `localparam`（默认 `EX1`/`EX4_F`），经 `vector_top → vex → vex_pipe` 下传，在 `vex_pipe` 的 `generate` 八分支里 elaboration 期选定，零运行时开销；宏用「级数 / 级数×10」的整数编码同时区分级号与是否寄存。
- **`_F`（flopped）变体**通过改接已寄存的 `data_ex{N-1}` 切断含 ALU 组合输出的长路径，提升主频；代价是晚一拍、且只能转发更早级就绪的结果。`vmacros.sv` 的 `non-flopped hurt freq` 是其设计箴言。
- **变延迟**不是流水线深度不同，而是「最早就绪级」不同：简单 ALU=EX1、乘法=EX3、除法=EX4，由 `v_int_alu` 的 `valid_*` 与 `vex_pipe` 的 `ready_res_*` 累积链 + 数据短路实现；所有指令仍等长走到 WR。
- **与计分板协作**：`pending` 位只在写回清零（保守官方闸），而 `src_ok` 里的转发命中项提供「绕过 pending」的乐观快通道；变延迟决定快通道对不同指令「何时打开」。默认配置下除法只能靠写回转发。
- 两个转发点的取值是**主频（组合路径长度）与冒险解除速度（IPC）**的权衡旋钮，没有全局最优，只有面向负载的折中。

## 7. 下一步学习建议

- **向存储侧延伸**：本讲的转发解决的是「计算 → 计算」的 RAW。存储 load 的结果如何回到 `vis`？建议读 u3-l2（VMU 加载引擎）与 u4-l1（解耦执行 acquire-release 语义），看清 load 写回（`mem_wr`）如何清 `pending`、`unlock` 如何清 `locked`，与本讲的写回转发（`frw_c`）形成对照。
- **向验证侧延伸**：转发与变延迟的正确性极易出 X 或时序错配，建议结合 u4-l6（SVA 断言）阅读 `vex_sva.sv` / `vis_sva.sv`，看断言如何捕获「转发命中但 ticket 不匹配」「`pending` 与 `ready_res` 时序错位」等错误。
- **动手调参**：在 u4-l7（性能指标与参数调优）里，你会用 `results.log` 的 `stall_pending`/`stall_locked` 把本讲的「转发点 → stall 计数」关系量化，用真实仿真数据回答本讲综合实践里「待本地验证」的问题。
- **源码再读**：重读 `vex_pipe.sv:282-L573` 这一段，把「数据短路实现的变延迟」与「`generate` 选择的转发点」在脑子里对齐到同一张流水线图，是掌握本讲的关键标志。
