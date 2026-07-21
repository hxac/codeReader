# 讲义 u2-l7：vEX 与 vex_pipe 执行流水

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清楚向量执行级 `vex` 在数据通路中的角色——它把 vIS 发射过来的一组（`VECTOR_LANES` 个）元素交给谁去算、算完如何写回。
2. 画出 `vex_pipe`（单条 lane）从 EX1 到 EX4 再到写回（WR）的四级流水线寄存器，标注 `dst/ticket/mask/head_uop/end_uop` 这些控制信息是如何逐级传递的。
3. 解释 FU（功能单元）路由：一条指令进入 lane 后，如何凭 2 位的 `fu` 字段被分发到整数 ALU（`v_int_alu`）、浮点 ALU（`v_fp_alu`）、定点 ALU（`v_fxp_alu`）三套数据通路。
4. 理解「变延迟」——简单整数运算 1 拍出结果、乘法 3 拍、除法 4 拍，但所有指令都走完同一条 4 级流水，差异只在于「第几拍可以开始转发」。
5. 看懂两处可配置转发点（`FWD_POINT_A/B`）与写回点是如何在 `generate` 里静态引出的，以及 `_F`（flopped）变体为何能换频率。
6. 说清楚 `v_fp_alu` 当前为何只是一个「占位」实现。

---

## 2. 前置知识

在进入执行级之前，请确认你已经理解下面这些来自前置讲义的概念：

- **数据通路全景（u2-l1）**：vRRM → vIS → vEX 是计算主路；vIS 持有物理向量寄存器堆 VRF 与计分板，是计算路与访存路的汇聚中枢。本讲的 `vex` 就是 vIS 之后、写回 VRF 之前的那个「执行」方框。
- **指令信息包（u1-l4 / u2-l5）**：vIS 向 vEX 传递两个 packed 结构体——`to_vector_exec`（每 lane 的数据：`valid/mask/data1/data2/immediate`）与 `to_vector_exec_info`（整条指令共享的控制：`dst/ticket/fu/microop/vl/head_uop/end_uop`）。
- **FU 编码（u1-l4）**：`vmacros.sv` 定义 `MEM_FU=2'b00`、`FP_FU=2'b01`、`INT_FU=2'b10`、`FXP_FU=2'b11`。访存指令（`MEM_FU`）根本不会进入 vEX，它们在 vRRM 就分叉去了 vMU；进入 vEX 的只有 `INT_FU/FP_FU/FXP_FU` 三类。
- **ticket 与计分板（u2-l3 / u2-l5）**：每条指令带一个 `ticket`（全局递增的序号），vEX 写回时连同 `ticket` 一起上报，vIS 据此清除 per-element 的 `pending` 位。所以 vEX 的写回接口里除了 `addr/data` 还必须有 `ticket`。
- **归约（u2-l6）**：归约指令把目的指针在展开组间错开，跨组累加由 vEX 的归约树用 `head_uop/end_uop` 协调。本讲会看到这两个信号如何流到 EX4 去驱动「中间结果暂存器」。

> 一句话定位：vEX 不做调度、不碰计分板，它只负责「拿到一组操作数 → 算 → 把结果连同 ticket 送回去（写回 + 转发）」。变延迟与转发是它仅有的两件「聪明事」。

---

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [vex.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv) | 执行级顶层 | 例化 `VECTOR_LANES` 个 `vex_pipe`、连线归约树、维护 EX1→WR 的控制寄存器、引出转发/写回 |
| [vex_pipe.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv) | 单条 lane 的流水 | FU 路由、四级共享数据寄存器、变延迟 `ready_res_*` 链、可配置转发点、归约中间结果 |
| [v_fp_alu.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_fp_alu.sv) | 浮点 ALU（占位） | 用来说明「占位实现」长什么样、为何不影响整数程序 |
| [v_int_alu.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv) | 整数 ALU（仅看接口与 `ready_res_*`） | 本讲只用到它的「第几级出结果」这一接口事实，内部算法留给 u2-l8 |
| [vmacros.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv) | FU 编码与转发点宏 | `INT_FU/FP_FU/FXP_FU`、`EX1..EX4_F` 的取值 |
| [params.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/params.sv) | 全局参数 | `VECTOR_FWD_POINT_A/B` 的默认值 |

---

## 4. 核心概念与源码讲解

### 4.1 执行级顶层结构（vex.sv）

#### 4.1.1 概念说明

`vex` 是向量执行级的**顶层包装**：它自己不做任何算术，只做三件事——

1. **例化** `VECTOR_LANES` 条相互独立的执行 lane（`vex_pipe`），每条 lane 负责一个元素位置的运算。
2. **连线归约树**：把所有 lane 的中间结果按对数深度跨 lane 连起来（lane 0 最终收口），让归约指令（VRADD/VRAND/VROR/VRXOR）能跨 lane 求和。
3. **维护流水控制寄存器**：把 `dst/ticket/head_uop/end_uop/valid` 从 EX1 一路打拍到 WR，用于写回与转发寻址。

之所以把「单 lane」和「顶层」分成两个模块，是因为**SIMD 并行 = 复制 lane + 跨 lane 互连**这两件事天然解耦：lane 内部不知道别的 lane 的存在，所有跨 lane 的连线都集中在 `vex.sv` 里，改 lane 数时只需调整 `generate` 的边界。

#### 4.1.2 核心流程

一条向量 micro-op（一组 `VECTOR_LANES` 个元素）进入 vEX 后的顶层流程：

```text
vIS.issue ──► valid_i + exec_data_i[Lanes] + exec_info_i
                        │
            ┌───────────┴────────────┐
            │  for k in 0..Lanes-1:  │   例化 Lanes 条 vex_pipe
            │   vex_pipe[k]          │   每条吃 data1/data2/imm/mask
            │   valid = valid_i &    │   共享 microop/fu/vl
            │            data[k].valid│
            └───────────┬────────────┘
                        │
   归约树连线 rdc_data_exN_i[k] = rdc_data_exN_o[k+stride]
                        │
   控制寄存器: dst/ticket/head/end 从 EX1→EX2→EX3→EX4→WR
                        │
            三类输出：
              ① frw_a (转发点A, 默认 EX1)   ──► 回 vIS 计分板
              ② frw_b (转发点B, 默认 EX4_F) ──► 回 vIS 计分板
              ③ wr    (写回, WR 级)         ──► 写 VRF + 回 vIS
```

关键握手：`ready_o = &ready`，即**所有 lane 都 ready 才向上游报告 ready**。目前每条 lane 的 `ready_o` 恒为 1（见 4.2.3），所以执行级实际上不反压 vIS——这是「变延迟但非阻塞」设计的体现。

#### 4.1.3 源码精读

**(a) 端口分组**——把 [vex.sv:10-45](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L10-L45) 的端口按功能分成四组最易理解：

- **发射接口**：`valid_i / exec_data_i[Lanes] / exec_info_i / ready_o`。注意 `exec_data_i` 是**每 lane 一份**（数组），而 `exec_info_i` 全 lane 共享一份。
- **转发点 #1（EX1）**：`frw_a_en[Lanes]`（每 lane 一个就绪标志）、`frw_a_addr/frw_a_ticket`（全 lane 共享一个目的寄存器号与 ticket）、`frw_a_data[Lanes]`（每 lane 一个数据）。
- **转发点 #2（EX\*）**：同上，`frw_b_*`。
- **写回**：`wr_en[Lanes] / wr_addr / wr_data[Lanes] / wr_ticket`。

> 为什么 `addr/ticket` 共享而 `en/data` 每 lane 一份？因为同一条指令的所有 lane 写的是**同一个目的寄存器、同一个 ticket**，只是每个 lane 负责自己那一个元素位置（per-element 写使能由 `mask` 与 `wr_en[k]` 共同决定）。

**(b) lane 例化与 per-lane valid**：[vex.sv:67-120](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L67-L120)

```systemverilog
assign ready_o = &ready;                              // L65
...
assign vex_pipe_valid[k] = valid_i & exec_data_i[k].valid;   // L70
vex_pipe #(... .VECTOR_LANE_NUM(k) ...) vex_pipe (            // L71
    .valid_i (vex_pipe_valid[k]), .ready_o(ready[k]),
    .mask_i  (exec_data_i[k].mask), .data_a_i(exec_data_i[k].data1),
    .data_b_i(exec_data_i[k].data2), .immediate_i(exec_data_i[k].immediate),
    .microop_i(exec_info_i.microop), .fu_i(exec_info_i.fu), .vl_i(exec_info_i.vl),
    ...
);
```

两个要点：① 每条 lane 通过 `VECTOR_LANE_NUM=k` 知道自己是第几号 lane（归约树据此决定该 lane 是否参与某一级的归约，见 4.1.3(d) 与 u2-l8）；② `valid` 是 **顶层 `valid_i` 与该 lane 自身 `exec_data_i[k].valid` 相与**，所以尾部不满一组时（`vl` 不是 LANES 的整数倍），无效 lane 不会产生写回。

**(c) 控制寄存器逐级打拍**：[vex.sv:155-217](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L155-L217)

顶层用四组 `always_ff` 把 `dst/ticket/head_uop/end_uop` 从 EX1 一路搬到 WR：

```systemverilog
// EX1/EX2
if(valid_i) begin dst_ex2 <= exec_info_i.dst; ticket_ex2 <= exec_info_i.ticket;
                  head_ex2 <= exec_info_i.head_uop; end_ex2 <= exec_info_i.end_uop; end
// EX2/EX3、EX3/EX4 同理用 dst_ex3<=dst_ex2 ... dst_ex4<=dst_ex3
// EX4/WR
if(valid_ex4) begin dst_wr <= dst_ex4; ticket_wr <= ticket_ex4; end
```

注意 `valid_exN` 带异步复位（`negedge rst_n`），而 `dst/ticket/head/end` **不带复位**——它们只有在对应 `valid` 拉高时才更新，靠 valid 来保证语义正确（无谓时其值是 X 也无所谓）。

**(d) 归约树连线**：[vex.sv:121-153](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L121-L153)

```systemverilog
// EX1: 步长 1 —— lane 0 收 lane 1，lane 2 收 lane 3 ...
for (k=0; k<VECTOR_LANES; k=k+2) assign rdc_data_ex1_i[k] = rdc_data_ex1_o[k+1];
// EX2: 步长 2 —— lane 0 收 lane 2，lane 4 收 lane 6（仅 LANES>2）
if (VECTOR_LANES > 2) for (k=0; k<=VECTOR_LANES/2; k=k+4) assign rdc_data_ex2_i[k] = rdc_data_ex2_o[k+2];
// EX3: 步长 4 —— lane 0 收 lane 4（仅 LANES>4）
if (VECTOR_LANES > 4) for (k=0; k<=VECTOR_LANES/4; k=k+8) assign rdc_data_ex3_i[k] = rdc_data_ex3_o[k+4];
// EX4: 步长 8 —— lane 0 收 lane 8（仅 LANES>8）
if (VECTOR_LANES > 8) for (k=0; k<=VECTOR_LANES/8; k=k+16) assign rdc_data_ex4_i[k] = rdc_data_ex4_o[k+8];
```

这是一个对数深度的二叉归约网络，每级步长翻倍：

\[ \text{归约级数} = \lceil \log_2(\text{VECTOR\_LANES}) \rceil \]

- 8 lane：EX1（步长 1）→ EX2（步长 2）→ EX3（步长 4），在 lane 0 收口，共 3 级。
- 16 lane：再多一级 EX4（步长 8）。

这就解释了 u1-l3 提到的「最多 16 lane」硬上限：归约树被**静态生成了 4 级**（EX1–EX4），最大步长 8，最多支撑 \(2^4=16\) 条 lane。要支持 32 lane 必须新增一级 EX5 的归约与背压。**归约的算术（加/与/或/异或）发生在 `v_int_alu` 内部**，本讲只关心「线怎么连」，算术细节留给 u2-l8。

**(e) 转发/写回寻址的 generate**：[vex.sv:218-257](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L218-L257)

顶层用一个 `generate if/else` 根据 `FWD_POINT_A/B` 选择 `frw_a_addr/frw_a_ticket` 取自哪一级寄存器：

```systemverilog
if (FWD_POINT_A == `EX1) begin assign frw_a_addr = exec_info_i.dst;      assign frw_a_ticket = exec_info_i.ticket; end
else if (FWD_POINT_A == `EX2_F | FWD_POINT_A == `EX2) begin assign frw_a_addr = dst_ex2; ... end
...
assign wr_addr = dst_wr; assign wr_ticket = ticket_wr;   // 写回恒取 WR 级
```

注意顶层只管 `addr/ticket`（共享信号），而 `en/data`（每 lane 信号）在 `vex_pipe` 内部同样用 `generate` 引出——**两处的 `FWD_POINT_*` 必须配同一套参数**，否则会出现「转发的 addr 对了但 data 取自不同级」的错位。

**(f) idle 判据**：[vex.sv:259](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L259)

```systemverilog
assign vex_idle_o = ~valid_i & ~valid_ex2 & ~valid_ex3 & ~valid_ex4;
```

只要 EX1–EX4 任一级有指令在飞，vEX 就不空闲。这个信号会上报到 `vector_top` 参与 `vector_idle_o` 的汇聚（见 u2-l1）。

#### 4.1.4 代码实践

> **实践目标**：在顶层把「指令信息包」与「四级控制寄存器」对上号，建立「同一条指令在 EX1/EX2/EX3/EX4 分别能看到什么控制信息」的直觉。

操作步骤：

1. 打开 [vex.sv:155-217](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L155-L217)。
2. 假设一条 `vadd` 在第 `T` 拍被发射（`valid_i=1`，`exec_info_i.dst=5`，`exec_info_i.ticket=9`，`head_uop=1`，`end_uop=0`）。
3. 用纸笔填写下表（每格填「在第几拍稳定 / 值是多少」）：

| 信号 | 第 T 拍 | T+1 | T+2 | T+3 | T+4 |
| --- | --- | --- | --- | --- | --- |
| `valid_i` | 1 | （视下条指令） | | | |
| `valid_ex2` | 0 | 1 | 0→？ | | |
| `dst_ex2` | X | 5 | 5 | | |
| `dst_ex4` | | | | 5 | 5 |
| `dst_wr` | | | | X | 5 |

4. 需要观察的现象：`valid_exN` 比 `valid_i` 晚一拍、逐级下移；`dst_*` 在对应 valid 拉高的**下一拍**才稳定成 5。
5. 预期结果：写回 `wr_addr=5`、`wr_ticket=9` 在第 `T+4` 拍稳定（因为 dst_wr 来自 EX4/WR 寄存器，见 L212-217）。
6. 若无法本地跑仿真，标注「待本地验证」并把推理过程写清楚即可——本实践是「源码阅读 + 时序推演」型，不依赖运行。

#### 4.1.5 小练习与答案

**练习 1**：把 `VECTOR_LANES` 从 8 改成 4，[vex.sv:149-153](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L149-L153) 里哪几段 `generate` 会被综合掉？

**答案**：`g_rdc_ex3` 的条件是 `VECTOR_LANES > 4`，4 不大于 4 → 假；`g_rdc_ex4` 条件 `VECTOR_LANES > 8` → 假。所以 EX3、EX4 两级归约连线被剔除，只剩 EX1（步长 1）和 EX2（步长 2），lane 0 在 EX2 收口，共 2 级，正好 \(\lceil\log_2 4\rceil=2\)。

**练习 2**：为什么 `frw_a_addr` 是全 lane 共享的、而 `frw_a_en` 必须每 lane 一个？

**答案**：同一条指令的所有 lane 写同一个目的寄存器、同一个 ticket，所以 addr/ticket 共享；但每个 lane 的结果是否「就绪可转发」取决于该 lane 的 `mask` 与 ALU 的 `ready_res_*`（不同 lane 可能因 mask 被屏蔽而不产出有效结果），所以 `en` 必须 per-lane。

---

### 4.2 单 lane 流水与 FU 路由（vex_pipe.sv）

#### 4.2.1 概念说明

`vex_pipe` 是一条 lane 的全部内容：一个**被三种 ALU 共享的 4 级数据流水线**。它的设计哲学是——

> ALU 的「位宽」决定了流水线寄存器的宽度。

不同的运算中间结果位宽不同（乘法有部分积、除法要保留余数），但一条 lane 只有一套数据寄存器。所以 `vex_pipe` 把数据寄存器做得**足够宽，能装下最宽的中间结果**，三种 ALU 共用它，按需往里写。

`localparam` 直观反映了这一点（[vex_pipe.sv:59-62](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv#L59-L62)）：

```systemverilog
localparam int EX1_W = 4*(DATA_WIDTH+8); // 160 bit：装得下乘法的 4 段部分积
localparam int EX2_W = 3*DATA_WIDTH;     // 96  bit：装得下除法的 商/余数/除数
localparam int EX3_W = 3*DATA_WIDTH;     // 96  bit
localparam int EX4_W = DATA_WIDTH;       // 32  bit：最终 32 位结果
```

注释里写得很明白：「The Data Flops are shared between the execution units. The biggest data to be saved dictates the size of the flop used.」

#### 4.2.2 核心流程

一条指令在单 lane 内的旅程：

```text
valid_i, data_a/b/imm, microop, fu, mask
        │
   ┌────┴───── FU 路由（按 fu 字段三选一）────────────┐
   │ valid_int_ex1 = (fu==INT_FU)  ──► v_int_alu      │   ← 整数/乘/除/归约都在这
   │ valid_fp_ex1  = (fu==FP_FU)   ──► v_fp_alu       │
   │ valid_fxp_ex1 = (fu==FXP_FU)  ──► v_fxp_alu      │
   └────┬─────────────────────────────────────────────┘
        │ 三个 ALU 各自给出 res_*_ex1 + ready_res_*_ex1
   EX1/EX2 数据寄存器: if(mask|rdc) 按 int>fp>fxp 优先级写 data_ex1
        │           控制寄存器: ready_res_ex2 <= 三个 ready 之或
   EX2/EX3 ──► data_ex2：若上一级已 ready 则直通(pass)，否则取本级 ALU 结果
        │
   EX3/EX4 ──► data_ex3（同理）
        │
   EX4/WR ──► data_ex4（同理） + 归约中间结果暂存（仅 lane 0）
        │
   写回: wr_data = use_temp_rdc_result ? 暂存值 : data_ex4, 再 & mask
   转发: frw_a/frw_b 按 FWD_POINT_* 在 generate 里引出
```

FU 路由的代码非常短（[vex_pipe.sv:124-126](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv#L124-L126)）：

```systemverilog
assign valid_int_ex1 = valid_i ? (fu_i === `INT_FU) : 1'b0;
assign valid_fp_ex1  = valid_i ? (fu_i === `FP_FU)  : 1'b0;
assign valid_fxp_ex1 = valid_i ? (fu_i === `FXP_FU) : 1'b0;
```

> 注意一个容易混淆的点：`INT_FU` 这一路**不仅仅**是简单整数算术。`v_int_alu` 内部还会根据 `microop` 进一步路由到「简单 ALU / 乘法 / 除法 / 归约树」四条子通路（见 u2-l8）。所以从 `vex_pipe` 的视角，「整数功能单元」是一个大一统的整数流水线，浮点和定点才是另两条独立通路。

#### 4.2.3 源码精读

**(a) ALU 例化与 stub**：[vex_pipe.sv:132-281](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv#L132-L281)

- `v_int_alu` **无条件例化**（L132-180）——整数核是必需的。
- `v_fp_alu` 用 `generate if (VECTOR_FP_ALU)` 包裹（L185-231）：参数为 1 时例化，为 0 时给 `ready_res_fp_*` 全接 0（stub）。
- `v_fxp_alu` 的例化代码**被整段注释掉了**（L236-275），`else` 分支直接给 0（L276-281）——定点 ALU 当前根本不存在。

三个 ALU 的结果在每级数据寄存器里按 `int > fp > fxp` 的优先级二选一/三选一写入（因为同一拍只有一种 FU 有效，优先级只是为了综合友好）。

**(b) 共享数据寄存器与 `ready_res_*` 直通**：以 EX2/EX3 为例 [vex_pipe.sv:324-336](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv#L324-L336)

```systemverilog
always_ff @(posedge clk) begin
  if(mask_ex2 | use_reduce_tree_ex2) begin
    if(ready_res_ex2)      data_ex2 <= data_ex1;        // 上一级已算完 → 直通
    else if(valid_int_ex2) data_ex2 <= res_int_ex2;     // 否则取本级整数结果
    else if(valid_fp_ex2)  data_ex2 <= res_fp_ex2;
    else if(valid_fxp_ex2) data_ex2 <= res_fxp_ex2;
  end
end
```

`ready_res_ex2` 是关键：它表示「这条指令的结果在 EX1 或之前就已经算好了」。一旦它为 1，数据寄存器就退化为一个**单纯的打拍寄存器**（把上一级数据原样传下去），不再消费本级 ALU 的组合结果。三级 `ready_res_*` 的传递见 [vex_pipe.sv:313](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv#L313)、[L350](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv#L350)、[L385](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv#L385)，是「上一级 ready 或本级任一 ALU ready」的累或。

**(c) 反压**：[vex_pipe.sv:123](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv#L123)

```systemverilog
assign ready_o = 1'b1; // so far no multi-cycle blocking ops exist
```

注释说明了一切：目前为止没有「多周期阻塞型」运算（除法也是流水的，不阻塞发射），所以 lane 永远 ready。这也回扣了 4.1 里 `vex.ready_o = &ready = 1`。

**(d) 归约中间结果暂存（仅 lane 0）**：[vex_pipe.sv:395-446](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv#L395-L446)

归约指令跨多个 micro-op 累加，需要一个能跨拍保存的「累加器」，这个累加器只在 lane 0 存在（`generate if (VECTOR_LANE_NUM == 0)`）。它根据 `head_uop_ex4_i/end_uop_ex4_i` 决定是「装载新累加值」还是「继续累加」还是「输出最终值」：

```systemverilog
assign nxt_temp_rdc_result_ex4 = ( head_uop &  ready_res_ex4) ? data_ex3    :  // 头组且就绪 → 装载
                                 ( head_uop &  use_reduce_tree_ex4) ? res_int_ex4 :
                                 nxt_tmp_rslt;                                   // 否则继续 +/&/|/^
```

累加运算用 `rdc_op_ex4`（来自 `v_int_alu` 的 `rdc_op_ex4_o`）选择加/与/或/异或。最终 `use_temp_rdc_result` 在末组（`end_uop & ~head_uop`）拉高，让写回选用这个累加值而非 `data_ex4`（[vex_pipe.sv:565](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv#L565)）。这正是 u2-l6 所说「真正写回只有末组 0 号元素」的硬件落点。

**(e) 写回**：[vex_pipe.sv:563-566](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv#L563-L566)

```systemverilog
assign wr_en_o   = valid_result_wr;
assign wr_data_o = use_temp_rdc_result ? temp_rdc_result_ex4[0+:DATA_WIDTH] & {DATA_WIDTH{mask_wr}}
                                       : data_ex4[0+:DATA_WIDTH]            & {DATA_WIDTH{mask_wr}};
```

写回数据永远 `& {DATA_WIDTH{mask_wr}}`——被 mask 屏蔽的 lane 写回 0，这就是逐元素掩码门控（u2-l6）。

#### 4.2.4 代码实践

> **实践目标**：跟踪一条 `vadd` 在单 lane 内 EX1→EX4 的数据通路，体会「共享数据寄存器 + ready 直通」。

操作步骤：

1. 假设 `vadd v5, v1, v2`，lane 0 的输入：`data_a_i=10`、`data_b_i=20`、`fu_i=INT_FU(2'b10)`、`microop_i=7'b0000001(VADD)`、`mask_i=1`、`valid_i=1`。
2. 查 [v_int_alu.sv:117-121](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L117-L121) 确认 `VADD` 算 `data_a+data_b=30`，且 [v_int_alu.sv:847](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L847) 给出 `ready_res_ex1_o = valid_int_ex1 | valid_rdc_ex1 = 1`。
3. 在 [vex_pipe.sv:289-319](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv#L289-L319) 推演 EX1/EX2 寄存器：`valid_int_ex1=1` → `data_ex1 <= res_int_ex1 = 30`；`ready_res_ex2 <= ready_res_int_ex1 = 1`。
4. 在 EX2/EX3、EX3/EX4、EX4/WR 三处（L324、L359、L451）观察：因为 `ready_res_exN=1`，数据寄存器走 `data_exN <= data_exN-1` 的**直通分支**，30 一路原样打到 `data_ex4`。
5. 写回：`wr_data_o = data_ex4 & mask = 30`。
6. 需要观察的现象：从 EX2 起，`v_int_alu` 的组合结果其实已经不被消费了（`ready_res` 命中直通分支），ALU 在这些级上虽然仍输出，但对结果无贡献。
7. 预期结果：lane 0 在第 T+4 拍写出 30 到 v5 的元素 0。
8. 「待本地验证」：可在仿真里给 `vex_pipe` 内部的 `data_ex1..data_ex4` 加波形，确认 30 是逐级「直通」而非每级重算。

#### 4.2.5 小练习与答案

**练习 1**：`vex_pipe` 里 `v_int_alu / v_fp_alu / v_fxp_alu` 三个 ALU 的例化条件分别是什么？当前默认配置下哪几个真正存在？

**答案**：`v_int_alu` 无条件例化；`v_fp_alu` 由 `generate if (VECTOR_FP_ALU)` 控制（默认参数为 1，所以**例化但只是占位**，见 4.3）；`v_fxp_alu` 的例化被整段注释，`else` 分支给 0。所以当前真正「在线」的只有 `v_int_alu`。

**练习 2**：为什么数据寄存器写入条件是 `if(mask_i | use_reduce_tree_ex1)`？如果某 lane 本拍被 mask 屏蔽，数据寄存器会怎样？

**答案**：被 mask 屏蔽的 lane（`mask_i=0`）且不参与归约时，整个 `if` 块不执行，`data_ex1` 保持旧值——即该 lane 的流水线**冻结一拍**。这与写回时 `& mask_wr`（写 0）配合，保证屏蔽元素既不消费 ALU、也写回 0。

---

### 4.3 变延迟与转发

#### 4.3.1 概念说明

「变延迟（variable latency）」不是指不同指令**离开** vEX 的时间不同——**所有指令都走完 EX1→EX2→EX3→EX4→WR 这同一条 5 拍路径**。它指的是「结果**最早可被转发/使用**的时刻」随运算类型而异：

| 运算类型 | 最早 ready 的级 | 来源（v_int_alu） |
| --- | --- | --- |
| 简单整数 ALU（VADD/VSLL/VAND…） | EX1 | `ready_res_ex1_o = valid_int_ex1`（[L847](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L847)） |
| 乘法 VMUL | EX3 | `ready_res_ex3_o = valid_mul_ex3`（[L860](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L860)） |
| 除法 VDIV | EX4 | `ready_res_ex4_o = valid_div_ex4`（[L867](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L867)） |
| 归约（各级递进） | EX1–EX4 之一 | `valid_rdc_exN`（[L854/L860/L867](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L854-L867)） |

「转发（forwarding）」就是为了把这个「早算完」的好处喂给 vIS 的计分板：后继指令的源操作数不必等写回 VRF，直接从 vEX 流水线中间截取。于是就有了两处**可配置**的转发点 `frw_a / frw_b`，外加一个固定的写回点 `wr`——它们对应 u2-l5 里计分板的三条命中路径。

#### 4.3.2 核心流程

转发点的设计围绕一个频率/冒险权衡（见 [vmacros.sv:35-44](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv#L35-L44)）：

```systemverilog
`define EX1   1   // 非寄存（组合）——最早，但 hurt freq
`define EX2   2   // 非寄存
`define EX2_F 20  // 寄存（flopped）——晚一拍，但切断组合路径
`define EX3   3
`define EX3_F 30
`define EX4   4
`define EX4_F 40
```

- **非 `_F` 变体**（`EX1/EX2/EX3/EX4`）：转发 `en` 直接组合依赖 ALU 的 `ready_res_int_exN`（这些是 ALU 组合输出），转发数据也从本级 ALU 结果 mux 出来。优点：转发早、解除冒险快；缺点：形成一条「ALU 组合逻辑 → 转发 mux → vIS 计分板」的组合路径，**拖低主频**（注释里的 "non-flopped hurt freq"）。
- **`_F` 变体**（`EX2_F/EX3_F/EX4_F`）：转发 `en` 只取**已经寄存过**的 `ready_res_exN`（即上一级打拍进来的标志），数据也取自寄存器 `data_exN-1`。优点：切断组合路径、保频率；代价：转发比非 `_F` 晚一拍生效。

默认配置（[params.sv:96-97](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/params.sv#L96-L97)）：

```systemverilog
localparam int VECTOR_FWD_POINT_A = `EX1;   // 转发点 A：EX1（组合，最早）
localparam int VECTOR_FWD_POINT_B = `EX4_F; // 转发点 B：EX4_F（寄存，保频率）
```

即「一个早转发 + 一个晚转发」的组合：A 抢在最早时刻解除大多数 RAW 冒险（VADD 这类 1 拍运算），B 作为兜底从寄存器转发（DIV 这类 4 拍运算的最终结果）。

#### 4.3.3 源码精读

**(a) 转发点 A 的 generate（以 EX1 与 EX4_F 为例）**：[vex_pipe.sv:479-518](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv#L479-L518)

```systemverilog
if (FWD_POINT_A == `EX1) begin :g_fwd_pnt_a_ex1
    assign frw_a_en_o   = ready_res_int_ex1;                 // 组合：ALU 的 ready
    assign frw_a_data_o = res_int_ex1[0 +: DATA_WIDTH];      // 组合：ALU 的结果
end
...
else if (FWD_POINT_A == `EX4_F) begin :g_fwd_pnt_a_ex4_f
    assign frw_a_en_o   = ready_res_ex4;                     // 寄存：上一级打拍来的 ready
    assign frw_a_data_o = ~mask_ex4 ? '0 : data_ex3[0+:DATA_WIDTH]; // 寄存数据
end
```

对比可见 `_F` 用的是 `ready_res_ex4`（flop）+ `data_ex3`（flop），非 `_F` 用的是 `ready_res_int_ex4`（组合）+ `res_int_ex4`（组合）。

**(b) 非寄存变体的数据 mux**：[vex_pipe.sv:505-511](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv#L505-L511)

```systemverilog
assign frw_a_data_o = ~mask_ex4        ? '0 :
                      ready_res_ex4    ? data_ex3[0+:32] :   // 早算完 → 直通数据
                      ready_res_int_ex4? res_int_ex4[0+:32]: // 本级整数结果
                      ready_res_fp_ex4 ? res_fp_ex4[0+:32] :
                                         res_fxp_ex4[0+:32];
```

这个 mux 把「直通数据」与「三种 ALU 本级结果」按优先级选出，正是变延迟与转发耦合在一起的地方：转发数据要么来自已经 ready 的上游寄存器，要么来自本级 ALU。

**(c) `v_fp_alu` 为何是占位**：[v_fp_alu.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_fp_alu.sv) 全文给出的证据有三条：

1. **LUT 全是 0**（[L63-65](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_fp_alu.sv#L63-L65)）：
   ```systemverilog
   assign lut_res_tan = '0;  assign lut_res_sig = '0;  assign lut_res_cos = '0;
   ```
   EX1 的 VTAN/VSIN/VCOS 三个分支只是把这三个 0 LUT 当结果输出（[L66-88](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_fp_alu.sv#L66-L88)）。

2. **EX2 的运算无意义**（[L104-122](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_fp_alu.sv#L104-L122)）：VTAN/VCOS 算的是 `data_ex2_i + data_ex2_i`（翻倍，不是 tan/cos），VSIN 算的是 `data_ex2_i - data_ex2_i`（恒为 0）。这显然不是真正的三角函数。

3. **EX3/EX4 直接拉 0**（[L124-125](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_fp_alu.sv#L124-L125)）：
   ```systemverilog
   assign valid_fp_ex3 = 1'b0;  assign valid_fp_ex4 = 1'b0;
   ```
   后两级根本不产出有效结果。

综合这三点：`v_fp_alu` 占据了 lane 里 FP 那条数据通路的位置（让 `generate if (VECTOR_FP_ALU)` 为真、接口连上），但**没有任何真实浮点运算**。它的作用是「**预留接口与流水线槽位**」——日后实现真正的 FPU 时，只需替换 `v_fp_alu` 内部、不必动 `vex_pipe` 的连线。当前所有公开示例（vvadd/saxpy/dot/fir）都是整数程序，`fu` 永远是 `INT_FU`，所以这个占位 ALU 从不被真正激活，对仿真结果无影响。

> 这也呼应了 u1-l1 记录的边界：「暂无浮点 lane」——更准确地说，浮点 lane 的「骨架」已经搭好（`vex_pipe` 的 FU 路由与三级数据寄存器都为它留了位），但「肌肉」（真正算法）尚未实现。

#### 4.3.4 代码实践

> **实践目标**：量化「改转发点」对冒险解除时机的影响，初步建立频率/性能取舍的直觉。

操作步骤：

1. 设定场景：两条相邻指令 `I1: vadd v5,v1,v2`（1 拍 ready）→ `I2: vadd v6,v5,v3`（RAW on v5）。`VECTOR_LANES=8`。
2. 在默认配置 `FWD_POINT_A=EX1`、`FWD_POINT_B=EX4_F` 下，分析 I2 能否在 I1 发射后紧接着的下一拍就拿到 v5：因为 A 点在 EX1 组合转发，I2 的源 v5 命中 frw_a，**无额外等待**。
3. 现把 [params.sv:96](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/params.sv#L96) 改成 `VECTOR_FWD_POINT_A = `EX3_F`（与 B 点错开但都寄存）。
4. 分析改动后的效果：
   - A 点的 `en` 现在取 `ready_res_ex3`（寄存），数据取 `data_ex2`。
   - I1 的 vadd 在 EX1 就 ready，但 A 点要到 EX3 才转发；I2 若想从 A 点命中，需要 I1 走到 EX3——这意味着 I2 可能多等 2 拍（或转去命中 B 点 / 写回点）。
   - 收益：A 点的组合路径被切断，关键路径变短，**主频有望提升**。
5. 需要观察的现象：转发时机（拍数）与组合路径长度（可用综合工具的时序报告衡量）此消彼长。
6. 预期结果：转发点越靠后/越寄存，冒险解除越慢但频率越高；反之亦然。这是 u4-l2「转发网络与变延迟执行」要深入量化的内容，本讲只要求建立定性直觉。
7. 「待本地验证」：真实拍数与频率影响需用 QuestaSim 波形 + 综合时序报告确认。

#### 4.3.5 小练习与答案

**练习 1**：默认配置下，一条 `vdiv`（4 拍）的结果最早能从哪个转发点被后继指令命中？

**答案**：`vdiv` 在 EX4 才 ready（`ready_res_ex4_o = valid_div_ex4`）。A 点在 EX1（太早，DIV 还没算完），所以命中不了 A；B 点默认 `EX4_F`，其 `en = ready_res_ex4`，正好在 EX4 之后一拍生效——所以 DIV 结果最早从 B 点（或写回点 wr）被命中。

**练习 2**：`_F`（flopped）转发变体为什么能提高频率？代价是什么？

**答案**：`_F` 的 `en/data` 都取自寄存器输出，切断了「ALU 组合逻辑 → 转发 mux → vIS 计分板」这条组合长路径，关键路径变短，主频提升。代价是转发比非 `_F` 晚一拍生效，可能让某些 RAW 冒险多等一拍才解除（性能略降）。这正是「延迟 vs 频率」的经典权衡。

**练习 3**：如果要把 `v_fp_alu` 从「占位」变成真正可用的浮点单元，最小改动集是什么？

**答案**：只需替换 [v_fp_alu.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_fp_alu.sv) 内部——把 0 LUT 换成真实三角函数实现、把 EX2 的无意义运算换成真正的 FP 运算、把 `valid_fp_ex3/ex4` 按真实流水深度拉高。`vex_pipe` 与 `vex` 的连线**无需改动**，因为接口（EX1–EX4 的 `result_*/ready_res_*`）已经预留好。这正体现了「占位」的设计意图。

---

## 5. 综合实践

把本讲三个模块串起来：**给一条 `vadd` 画出它从进入 vEX 到写回 VRF 的完整端到端通路，并标注所有「控制信号在哪一级产生、被谁消费」。**

要求完成以下子任务：

1. **顶层视角**（4.1）：在 [vex.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv) 里标出 `vadd` 的 `exec_info_i.dst/ticket` 如何经 `dst_ex2→dst_ex3→dst_ex4→dst_wr` 到达 `wr_addr/wr_ticket`；标出 `valid_i→valid_ex2→…→valid_ex4` 的链路，以及它如何决定 `vex_idle_o`。
2. **单 lane 视角**（4.2）：在 [vex_pipe.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv) 里画出 lane 0 的 `data_ex1→data_ex2→data_ex3→data_ex4` 通路，标注：① FU 路由选中 `v_int_alu`；② `ready_res_int_ex1=1` 导致从 EX2 起走「直通」分支；③ 最终 `wr_data_o = data_ex4 & mask`。
3. **转发视角**（4.3）：在图上标出两处转发引出点——`frw_a`（默认 EX1，组合引出 `res_int_ex1`）与 `frw_b`（默认 EX4_F，寄存引出 `data_ex3`），以及固定的写回点 `wr`。说明这三路信号都会回到 vIS 计分板（u2-l5）参与命中判定。
4. **自检问题**：如果把这条 `vadd` 换成 `vmul`（3 拍），你的图里哪几处会变？（提示：`ready_res_int_ex1=0`、EX2/EX3 不再直通而消费 `res_int_ex2/res_int_ex3`、EX3 起 `ready_res_ex3=1` 才开始直通。）

完成后面出一张包含「顶层寄存器链 + 单 lane 数据通路 + 三处转发引出」的合并图，作为本讲的成果物。无法本地验证的部分明确标注「待本地验证」。

---

## 6. 本讲小结

- `vex` 是执行级**顶层包装**，只做三件事：例化 `VECTOR_LANES` 条 `vex_pipe`、连线对数深度的归约树（EX1–EX4 步长 1/2/4/8，上限 16 lane）、把 `dst/ticket/head/end/valid` 从 EX1 打拍到 WR。
- 转发与写回的「addr/ticket」是全 lane 共享（一条指令一个目的），「en/data」是 per-lane（每 lane 独立就绪与屏蔽）。
- `vex_pipe` 是一条**被三种 ALU 共享**的 4 级数据流水，寄存器宽度由最宽中间结果决定（EX1=160/EX2=EX3=96/EX4=32 bit）；FU 路由凭 2 位 `fu` 在 `INT_FU/FP_FU/FXP_FU` 间三选一，其中 `INT_FU` 实际涵盖整数/乘/除/归约。
- **变延迟 ≠ 提前离队**：所有指令都走完同一 5 拍流水，差异只在「最早可转发级」——简单 ALU 在 EX1、MUL 在 EX3、DIV 在 EX4，由 `ready_res_*` 链把「已 ready」标志往后传，并让数据寄存器退化为直通。
- 两处转发点 `FWD_POINT_A/B` 可配置，`_F` 变体寄存 en/data 以切组合路径、保频率，代价是晚一拍；默认 `A=EX1`（早，组合）、`B=EX4_F`（晚，寄存）。
- `v_fp_alu` 当前是**占位实现**（LUT 全 0、EX2 运算无意义、EX3/EX4 恒 0），只为预留接口与流水槽位；公开示例全为整数程序，不会激活它。

---

## 7. 下一步学习建议

1. **u2-l8 整数 ALU 与跨 lane 归约树**：本讲刻意把 `v_int_alu` 当黑盒，只用了它的 `ready_res_*` 接口事实。下一讲打开这个黑盒，讲清楚 VMUL 的 3 拍部分积、VDIV 的 4 拍恢复除法、以及归约树在 `v_int_alu` 内部按 `VECTOR_LANE_NUM` 的逐级激活。
2. **u4-l2 转发网络与变延迟执行**：本讲建立了「转发点 vs 频率」的定性直觉，专家层那一讲会用综合时序报告与计分板交互来定量分析改转发点的代价。
3. **回顾 u2-l5**：现在再读 vIS 计分板，把 `frw_a_en/frw_a_addr/frw_a_data/frw_a_ticket` 与本讲引出的信号一一对应，确认「命中判定（addr===src 且 ticket===pending_ticket）」用的就是本讲送回去的这组信号。
4. **动手准备**：若你有 QuestaSim 环境，建议按 u1-l5 跑一遍 vvadd，给 `vex` 内部的 `dst_ex2..dst_wr`、`vex_pipe` 内部的 `data_ex1..data_ex4`、`ready_res_*` 加波形，亲眼验证本讲描述的「直通」与「变延迟」。
