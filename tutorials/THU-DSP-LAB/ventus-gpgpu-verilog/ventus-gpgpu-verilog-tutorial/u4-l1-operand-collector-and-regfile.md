# 操作数采集 operand_collector 与寄存器堆

## 1. 本讲目标

本讲聚焦 SM 流水线「发射之前」的一道关键工序——**操作数采集（operand collection）**。一条指令在发射前必须先把参与运算的源操作数（标量寄存器值、向量寄存器值、立即数）准备好，否则执行单元无事可做。学完本讲你应该能够：

- 说清标量寄存器堆（SGPR）与向量寄存器堆（VGPR）为什么被切成 `NUM_BANK` 个体（bank），以及每个 bank 的深度由谁决定；
- 画出「一条向量指令的源操作数」从 collector_unit 发出读请求，经 operand_arbiter 仲裁、vector_regfile_bank 读出，再经 crossbar 送回 collector_unit 的完整闭环；
- 解释 collector_unit 的三态状态机（IDLE→ADD→OUT）如何逐个操作数地组装 `alu_src1/2/3` 与 `active_mask`；
- 理解 gen_imm 如何按指令类型抽取并符号扩展立即数。

本讲承接 [u3-l4 发射 issue 与记分板 scoreboard](u3-l4-issue-and-scoreboard.md)。在那一讲里，scoreboard 判定某条指令「不冒险」后，指令就带着译码控制信号进入操作数采集器；采集器把操作数凑齐后，再送到 `issue` 路由到各执行单元。可以理解为：scoreboard 是「能不能动」，本讲是「动之前把子弹装好」。

## 2. 前置知识

在开始前，先用通俗语言建立几个直觉。

**为什么需要「采集」这一步？** GPU 一个 warp 同时跑 `NUM_THREAD` 条线程，一条向量指令的源操作数是一个**向量**（`NUM_THREAD` 个 32 位），而不是标量 CPU 那样一个数。一条向量指令最多有 4 个源（src1/src2/src3/掩码），它们可能来自标量堆、向量堆、PC 或立即数。把这些来源各异、宽度巨大的操作数**分时复用**有限的寄存器堆读端口凑齐，就是采集器的工作。

**为什么寄存器堆要分 bank？** 一个 SM 里所有在飞的 warp 共享一个大寄存器堆。如果只有 1 个读端口，同一拍只能读一个寄存器，太慢；如果给每个采集器单独配一套堆，面积爆炸。折中方案是把寄存器堆切成 `NUM_BANK` 个**体（bank）**，每个 bank 有独立读端口，多个不冲突的读请求可以同拍并行进行——这就是 **bank 划分（banking）**。代价是：当多个请求落到同一个 bank 时要排队，即 **bank 冲突（bank conflict）**。

**关键概念：漏斗结构。** 回顾上一讲：`NUM_COLLECTORUNIT = NUM_WARP`（每个 warp 配一个采集器，并发准备操作数），而 `NUM_ISSUE = 1`（每拍单发射）。多个采集器同时去读数量有限的 bank，读到的结果再汇聚成单条发射流。这正体现了 GPU「用多 warp 切换隐藏延迟」的思想。

下表给出本讲反复出现的几个规模宏（定义于 `define.v`），先记下来后面会逐个用到：

| 宏 | 值 / 表达式 | 含义 |
|---|---|---|
| `NUM_COLLECTORUNIT` | `= NUM_WARP` | 采集器个数，等于每核 warp 数 |
| `NUM_BANK` | `4` | 寄存器堆被切成的体（bank）数 |
| `DEPTH_BANK` | `$clog2(NUM_BANK) = 2` | bank 编号位宽 |
| `NUM_VGPR` / `NUM_SGPR` | `1024` / `1024` | 向量 / 标量寄存器总槽位 |
| `DEPTH_REGBANK` | `$clog2(NUM_VGPR/NUM_BANK) = 8` | bank 内地址位宽（即每 bank 256 项） |
| `XLEN` | `32` | 数据位宽 |
| `REGIDX_WIDTH` / `REGEXT_WIDTH` | `5` / `3` | 指令字段寄存器号 / REGEXT 扩展位 |

## 3. 本讲源码地图

本讲所有模块都位于 `src/gpgpu_top/sm/pipeline/operand_collector/` 目录，外加一份配置总开关：

| 文件 | 作用 |
|---|---|
| `operandcollector_top.sv` | **顶层集装箱**：把下面所有子模块连起来，自身不做运算 |
| `inst_demux.v` | 把 1 条译码指令分发到 NUM_COLLECTORUNIT 个采集器中的某一个（1→N 分发） |
| `collector_unit.v` | **核心采集单元**：状态机驱动，发读请求、收数据、组装 4 个操作数 |
| `operand_arbiter.v` | 读端口仲裁：NUM_BANK 个 bank 各自做轮询仲裁，解决 bank 冲突 |
| `scalar_regfile_bank.v` | 标量寄存器堆的一个 bank（双端口 SRAM） |
| `vector_regfile_bank.v` | 向量寄存器堆的一个 bank（双端口 SRAM，含掩码写与 v0 跟踪） |
| `crossbar.sv` | 把 bank 读出的数据按来源路由回正确的采集器与操作数槽 |
| `gen_imm.v` | 立即数生成：按 `sel_imm` 类型抽取并符号扩展立即数（在 collector_unit 内例化） |
| `src/define/define.v` | 上述所有规模宏、`A1/A2/A3_*` 选择码、`IMM_*` 立即数类型宏的定义 |

## 4. 核心概念与源码讲解

### 4.1 操作数采集子系统总览：inst_demux 与 collector_unit 的协作

#### 4.1.1 概念说明

`operandcollector_top` 是一个典型的「广播 + 选中」结构，它要把**每拍 1 条**译码指令，正确地分给 `NUM_COLLECTORUNIT` 个采集器中的某一个空闲者；多个采集器凑齐操作数后，又要把它们的输出**汇聚成 1 条**发射流。因此数据通路是一个「瘦-胖-瘦」的沙漏：

```
        ibuffer2issue (1 条/拍)
              │
       ┌──────▼──────┐
       │  inst_demux │  广播控制信号 + 优先选中 1 个空闲采集器
       └──────┬──────┘
      ┌───────┼───────…   （NUM_COLLECTORUNIT 路）
   ┌──▼──┐ ┌──▼──┐  …
   │ CU0 │ │ CU1 │ …     每个采集器独立状态机，发读请求、收数据
   └──┬──┘ └──┬──┘
      │  outArbiter_*（读请求：bankID/rsType/rsAddr）   │
      └───────┼─────────────… ──┐
         ┌────▼─────┐      ┌────▼─────┐
         │ operand  │      │ regfile  │  仲裁 + bank 读
         │ arbiter  │─────▶│  banks   │
         └────┬─────┘      └────┬─────┘
              │  chosen（命中来源）  │ data（读出数据）
              └────────┬──────────┘
                  ┌────▼────┐
                  │ crossbar│  按来源把数据路由回原采集器的原操作数槽
                  └────┬────┘
      ┌───────┬────────┘…   （回到各 CU）
   ┌──▼──┐ ┌──▼──┐  …
   │ CU0 │ │ CU1 │ …      凑齐 4 个操作数 → S_OUT
   └──┬──┘ └──┬──┘
      └───────┼───────…  优先仲裁汇聚
            ┌─▼─┐
            │out│  → issue（1 条/拍，NUM_ISSUE=1）
            └───┘
```

#### 4.1.2 核心流程

1. **分发（inst_demux）**：来自 `ibuffer2issue` 的一条指令及其控制信号被广播给所有采集器，但 `out_valid_o[i]` 只对**被选中的那一个**采集器拉高——选中规则是 `fixed_pri_arb`（固定优先级，选最低编号的空闲采集器）。
2. **请求（collector_unit → arbiter）**：被选中的采集器进入状态机，逐个操作数计算 `bankID` 与 `rsAddr`，向 `operand_arbiter` 发读请求。
3. **仲裁与读出（arbiter → bank → crossbar）**：每个 bank 用 `round_robin_arb` 在冲突请求里轮询挑一个，读出数据；同时输出 `chosen` 编号记录「这次读的是哪个采集器的第几个操作数」。`crossbar` 据此把数据送回原处。
4. **组装与汇聚（collector_unit → out）**：采集器把 4 个操作数凑齐后进入 `S_OUT`，多个就绪的采集器再经一次 `fixed_pri_arb` 汇聚到唯一对外接口，送给 `issue`。

注意两侧的不对称：**入端分发**与**出端汇聚**都用固定优先级（`fixed_pri_arb`），而**中间的 bank 读仲裁**用轮询（`round_robin_arb`）以保证多采集器公平共享 bank。

#### 4.1.3 源码精读

顶层例化清单一目了然，6 个子模块按 `inst_demux → collector_unit×N → operand_arbiter → regfile bank×N + crossbar` 的顺序连接：

- [operandcollector_top.sv:499-625](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/operandcollector_top.sv#L499-L625)：用 `generate` 例化 `NUM_COLLECTORUNIT` 个 `collector_unit`，这是「胖」的一层。
- [operandcollector_top.sv:628-648](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/operandcollector_top.sv#L628-L648)：例化唯一的 `operand_arbiter`，输入是所有采集器的读请求，输出是每个 bank 的地址与命中编号。
- [operandcollector_top.sv:650-680](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/operandcollector_top.sv#L650-L680)：每个 bank 例化一对 `vector_regfile_bank` + `scalar_regfile_bank`，读口接仲裁器，写口接写回（writeback）。
- [operandcollector_top.sv:682-698](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/operandcollector_top.sv#L682-L698)：例化 `crossbar`，把 bank 数据路由回采集器。
- [operandcollector_top.sv:786-888](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/operandcollector_top.sv#L786-L888)：例化 `inst_demux`，完成 1→N 分发。

`inst_demux` 的「广播但只选一个」体现在这一行——`out_valid_o[i]` 只有当 `outReady_bin`（优先编码选中的采集器编号）等于 `i` 时才拉高：

```verilog
// inst_demux.v:179
assign out_valid_o[i] = (outReady_bin==i ? 1'b1 : 1'b0) && in_valid_i;
```

而 `outReady_bin` 来自 [inst_demux.v:187-204](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/inst_demux.v#L187-L204) 的 `fixed_pri_arb`（选最低编号的 `out_ready_i`）+ `one2bin`（独热转二进制）。上游握手 `in_ready_o = !(|widCmp_i) && (|out_ready_i)`（[inst_demux.v:183](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/inst_demux.v#L183)）：只要有一个采集器空闲，本次分发就能成交。

#### 4.1.4 代码实践

**实践目标**：在源码层面确认「分发=广播+选中」与「汇聚=优先仲裁」两件事。

**操作步骤**：
1. 打开 `operandcollector_top.sv`，定位 `inst_demux` 例化（L786 起）与输出侧的 `fixed_pri_arb`（L891 起）。
2. 对比 `inst_demux.v` 内部的 `fixed_pri_arb`（L187）与顶层输出侧的 `fixed_pri_arb`（L891），注意它们一个作用于输入分发、一个作用于输出汇聚。
3. 在 `operand_arbiter.v` 中找到 `round_robin_arb`（L187、L209），确认中间读仲裁用的是轮询而非固定优先级。

**预期结果**：你能指出三类仲裁器各自的位置与策略，并解释「为什么不全用固定优先级」（轮询避免低编号采集器长期饿死）。

#### 4.1.5 小练习与答案

**练习 1**：`NUM_COLLECTORUNIT` 等于多少？为什么这样设？
**答案**：`NUM_COLLECTORUNIT = NUM_WARP`（[define.v:21](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L21)）。因为每个 warp 需要一个独立的采集器来并发准备操作数，从而在多 warp 间切换隐藏延迟。

**练习 2**：`inst_demux` 的 `in_ready_o` 在什么条件下为 1？
**答案**：当 `widCmp_i` 全 0（当前没有同 wid 在飞冲突，本实现里 `widCmp` 被固定为 0，见 `operandcollector_top.sv:318`）且至少有一个采集器 `out_ready_i` 为 1 时。

---

### 4.2 寄存器堆 bank：scalar_regfile_bank 与 vector_regfile_bank

> 本节同时覆盖 `scalar_regfile_bank`（标量）与 `vector_regfile_bank`（向量）两个最小模块，它们结构几乎一致，差别仅在数据宽度与向量特有的掩码写/v0 跟踪。

#### 4.2.1 概念说明

寄存器堆是采集子系统的「仓库」。Ventus 把 1024 个标量槽（`NUM_SGPR`）和 1024 个向量槽（`NUM_VGPR`）**交错（interleaved）**地分到 `NUM_BANK=4` 个体里。所谓交错，就是把逻辑寄存器号按模分配到 bank：

- 逻辑寄存器 \(R\) 落在 bank \(R \bmod \text{NUM\_BANK}\)；
- 该 bank 内的行地址为 \(R / \text{NUM\_BANK}\)。

这样连续编号的寄存器天然分散到不同 bank，便于并行读取。采集器在请求时还会把 **warp 编号的低位也加进 bankID**（`bankID = (wid + regIdx) % NUM_BANK`），目的是让「不同 warp 的同名寄存器」也散到不同 bank，进一步降低冲突。

每个 bank 是一块**双端口 SRAM**：读口（`rsidx_i`/`rsren_i`/`rs_o`）供采集器读，写口（`rdidx_i`/`rdwen_i`/`rd_i`）供写回通路写。

#### 4.2.2 核心流程：bank 深度与地址计算

每个 bank 承担的项数为：

\[
\text{每 bank 项数} = \frac{\text{NUM\_VGPR}}{\text{NUM\_BANK}} = \frac{1024}{4} = 256
\]

地址位宽 `DEPTH_REGBANK` $= \$clog2(256) = 8$ 位（[define.v:49](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L49)）。两 bank 的差别在于**每项位宽**：

- 标量 bank：每项 `XLEN = 32` 位；
- 向量 bank：每项 `XLEN * NUM_THREAD` 位（一条向量寄存器含 `NUM_THREAD` 个 lane）。

采集器把逻辑地址 `(wid, regIdx)` 映射成物理 bank 地址的过程（见 [collector_unit.v:758-784](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/collector_unit.v#L758-L784)）：

\[
\text{bankID} = (\text{wid}_{\text{低位}} + \text{regIdx}_{\text{低位}}) \bmod \text{NUM\_BANK}
\]
\[
\text{rsAddr} = (\text{vgpr\_base}_{\text{wid}} \gg \text{DEPTH\_BANK}) + (\text{regIdx} \gg \text{DEPTH\_BANK})
\]

其中 `vgpr_base_wid` 是该 warp 在向量寄存器堆中的分配基址（由 CTA 调度器在派发时写入，每个 warp 一份，见 `sgpr_base_i`/`vgpr_base_i` 接口）。

#### 4.2.3 源码精读

**标量 bank** 就是一块 32 位宽、256 深的双端口 SRAM，并用 `T28_MEM` 宏在「真实 SRAM 编译器单元」与「行为级 `dualportSRAM`」间切换：

```verilog
// scalar_regfile_bank.v:140-168
`ifdef T28_MEM  //256x32
  GPGPU_RF_2P_256X32M U_GPGPU_RF_2P_256X32M_0 (
    .AA(rdidx_i), .D(rd_i), .WEB(!rdwen_i), .CLKW(clk),   // 写口
    .AB(rsidx_i), .REB(!rsren_i), .CLKR(clk), .Q(rs_o)    // 读口
  );
`else
  dualportSRAM #(.BITWIDTH(`XLEN), .DEPTH(`DEPTH_REGBANK)) U_dualportSRAM (
    .CLK(clk), .D(rd_i), .Q(rs_o), .REB(rsren_i), .WEB(rdwen_i),
    .AA(rdidx_i), .AB(rsidx_i)
  );
`endif
```

含义：仿真（不开 `T28_MEM`）用参数化的 `dualportSRAM`；流片综合（开 `T28_MEM`）替换为固定的 `GPGPU_RF_2P_256X32M`（256×32 双端口宏）。`rdidx_i/rdwen_i` 是写口（接写回），`rsidx_i/rsren_i/rs_o` 是读口（接仲裁器）。

**向量 bank** 比标量多了两样东西（[vector_regfile_bank.v:17-32](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/vector_regfile_bank.v#L17-L32)）：

1. **按线程掩码写**：写向量寄存器时，每个 lane 可以单独使能（`rdwmask_i[NUM_THREAD-1:0]`）。只有 `rdwmask_i[j]` 为 1 的 lane 才用 `rd_i` 的新值，否则写 0，由 `ram_mask` 组合出实际写入字（[vector_regfile_bank.v:60-71](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/vector_regfile_bank.v#L60-L71)）。这正是 SIMT 掩码执行的落地点。
2. **v0（掩码寄存器）单独跟踪**：v0 是专门的掩码寄存器。bank 用一个独立寄存器 `v0_mem` 捕获所有对索引 0 的写（[vector_regfile_bank.v:50-58](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/vector_regfile_bank.v#L50-L58)），并通过 `v0_o` 输出，供带掩码的向量指令读取操作数 4（`bankIn_v0_i`）。

向量 bank 的 `T28_MEM` 分支用 **8 块 256×128 的宏**拼出 256×1024 的容量（[vector_regfile_bank.v:324-427](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/vector_regfile_bank.v#L324-L427)），对应 `NUM_THREAD=32`、`XLEN=32` 时每项 1024 位。综合时 `NUM_THREAD=32`，8×128=1024 正好对齐；仿真默认 `NUM_THREAD=4` 时每项仅 128 位有效，但宏仍按 1024 例化。

#### 4.2.4 代码实践

**实践目标**：亲手算出 bank 深度，确认源码参数自洽。

**操作步骤**：
1. 打开 [define.v:27-49](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L27-L49)，记录 `NUM_BANK=4`、`NUM_VGPR=1024`、`DEPTH_REGBANK=$clog2(NUM_VGPR/NUM_BANK)`。
2. 手算：$1024/4 = 256$，$clog2(256) = 8$。
3. 回到 `scalar_regfile_bank.v` 注释 `//256x32`（L140）与 `vector_regfile_bank.v` 的 `256X...` 宏（L324），核对一致。

**预期结果**：每 bank 256 项；标量每项 32 位，向量每项 `32×NUM_THREAD` 位。这三处参数互相印证，无矛盾。本步可纯阅读完成，**无需运行仿真**。

#### 4.2.5 小练习与答案

**练习 1**：若把 `NUM_BANK` 改成 8（其余不变），`DEPTH_REGBANK` 变为多少？bank 容量如何变？
**答案**：$1024/8=128$，$clog2(128)=7$ 位。每 bank 变浅（128 项），但 bank 数翻倍，读端口带宽更大、bank 冲突更少，代价是面积与端口数上升。

**练习 2**：向量 bank 的写为什么要 `rdwmask_i` 逐 lane 使能？
**答案**：SIMT 执行中只有活跃 lane 该更新结果；被掩码关闭的 lane 必须保留原值，因此需要逐 lane 写使能，避免误写。

---

### 4.3 读端口仲裁 operand_arbiter

#### 4.3.1 概念说明

采集器向寄存器堆发读请求时，所有请求是「平面铺开」的：`NUM_COLLECTORUNIT` 个采集器，每个最多 4 个操作数，共 `4×NUM_COLLECTORUNIT` 个潜在请求。但只有 `NUM_BANK=4` 个物理读端口（每个 bank 一个）。`operand_arbiter` 的职责就是**把请求按 bank 归类、冲突时排队**，把海量请求收敛到每 bank 每拍至多一个。

它还做一件重要的事：**把标量与向量分开仲裁**。因为标量 bank 与向量 bank 是两套独立的存储体，需要分别喂地址。

#### 4.3.2 核心流程

1. **归类**：对每个请求 `(采集器 j, 操作数 k)`，先判断它命中哪个 bank（`bankID==i`），再判断它是标量（`rsType==2'b01`）还是向量（`rsType==2'b10` 或 `2'b00`），据此累加到 `arbiter_scalar_valid_in[i]` / `arbiter_vector_valid_in[i]`。
2. **轮询仲裁**：每个 bank 各跑一个 `round_robin_arb`，在命中该 bank 的所有标量请求里轮询选一个，向量同理。
3. **输出地址与命中号**：把被选中请求的 `rsAddr` 送到对应 bank 的读口，同时输出 `chosen`（被选中者的全局编号），供后续 `crossbar` 路由数据回送。

#### 4.3.3 源码精读

请求归类与标量/向量分流的核心在 [operand_arbiter.v:118-151](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/operand_arbiter.v#L118-L151)：

```verilog
// operand_arbiter.v:123-126
assign arbiter_scalar_valid_in[4*`NUM_COLLECTORUNIT*i+j*4+k] =
       arbiter_valid_i[j*4+k]
    && (arbiter_bankID_i[...]==i)               // 命中 bank i
    && (arbiter_rsType_i[...]==2'b01);          // 且是标量
assign arbiter_vector_valid_in[...] =
       arbiter_valid_i[j*4+k]
    && (arbiter_bankID_i[...]==i)
    && (arbiter_rsType_i[...]==2'b10 || ...==2'b00);  // 或向量
```

每个 bank 是否有标量/向量请求，是对归类结果做按位或（[operand_arbiter.v:94-95](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/operand_arbiter.v#L94-L95)）：

```verilog
assign arbiter_scalar_valid_out[n] = |arbiter_scalar_valid_in[ ... bank n 的所有请求 ... ];
```

每个 bank 用 `round_robin_arb`（输入宽度 `4*NUM_COLLECTORUNIT`）挑一个、再用 `one2bin` 把独热结果转成二进制编号 `chosen`（[operand_arbiter.v:183-230](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/operand_arbiter.v#L183-L230)）。`chosen` 是「全局请求编号」，它的高位编码采集器号、低 2 位编码操作数槽号——这正是后面 crossbar 路由的依据。

#### 4.3.4 代码实践

**实践目标**：理解 bank 冲突如何被仲裁吸收。

**操作步骤**：
1. 在 `operand_arbiter.v` 的 L185-L230 找到每个 bank 例化了**两个** `round_robin_arb`（标量一个、向量一个）。
2. 设想：CU0 要读向量寄存器 v1，CU1 也要读 v1（假设二者 wid+regIdx 落到同一 bank）。确认同一拍只有一个能被选中，另一个要等下一拍轮到。

**需要观察的现象**：同一 bank 上的两个请求不会同拍都得到服务；轮询保证两者相继获得服务而非一方饿死。

**预期结果**：你能复述「bank 冲突 → 轮询排队」的机制，并解释 `chosen` 为何同时携带采集器号与操作数槽号。

#### 4.3.5 小练习与答案

**练习 1**：为什么标量和向量要各跑一套仲裁器，而不是合并？
**答案**：标量 bank 与向量 bank 是两套独立存储体、各有读端口；分开仲裁才能让标量读与向量读在同一拍并行喂给各自的 bank。

**练习 2**：`chosen` 编号低 2 位代表什么？
**答案**：代表操作数槽号（regOrder：0=src1、1=src2、2=src3、3=掩码），高位代表采集器号（cu_id = chosen >> 2，见 `crossbar.sv:88-91`）。

---

### 4.4 立即数生成 gen_imm

#### 4.4.1 概念说明

很多指令的某个源操作数不是寄存器，而是直接编码在指令字里的**立即数（immediate）**。RISC-V 不同指令格式的立即数散布在指令字的不同位段，且需要**符号扩展**。`gen_imm` 就是一张「按指令类型抽取并拼装立即数」的查表器。它在每个 `collector_unit` 内例化（[collector_unit.v:836-841](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/collector_unit.v#L836-L841)），当采集器发现某操作数类型是立即数时，调用它生成 32 位结果，直接填入对应操作数槽（无需访问寄存器堆）。

#### 4.4.2 核心流程

输入三件套：`inst_i`（32 位指令字）、`sel_i`（4 位立即数类型，即 `sel_imm`）、`imm_ext_i`（REGEXT 前缀带来的 7 位扩展，向量立即数用）。输出 32 位 `out_o`。

`sel_i` 的取值在 `define.v` 里定义（[define.v:449-458](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L449-L458)）：

| `sel_i` 宏 | 值 | 含义 |
|---|---|---|
| `IMM_I` | 0 | I 型：load/算术/logic/jalr，`inst[31:20]` |
| `IMM_S` | 1 | S 型：store，`inst[31:25] + inst[11:7]` |
| `IMM_B` | 2 | B 型：分支，`inst[31]+inst[7]+inst[30:25]+inst[11:8]+0` |
| `IMM_U` | 3 | U 型：lui/auipc，`inst[31:12]+12'b0` |
| `IMM_J` | 5 | J 型：jal |
| `IMM_Z` | 7 | CSR 立即数，`inst[19:15]` |
| `IMM_V` | 6 | 向量立即数（结合 `imm_ext_i`） |

#### 4.4.3 源码精读

`gen_imm` 用 `casex({sel_i, inst_i[31]})` 一次既区分类型又处理符号扩展（最高位 `inst_i[31]` 决定补 0 还是补 1）：

```verilog
// gen_imm.v:62-77
casex({sel_i,inst_i[31]})
  {`IMM_I,1'b1}: out_o = {{20{1'b1}},inst_i[31:20]};   // 负：符号扩展
  {`IMM_I,1'b0}: out_o = {20'b0,inst_i[31:20]};         // 正：零扩展
  {`IMM_S,1'b1}: out_o = {{20{1'b1}},inst_i[31:25],inst_i[11:7]};
  ...
  {`IMM_U,1'bx}: out_o = {inst_i[31:12],12'b0};          // U 型与符号位无关
  {`IMM_Z,1'bx}: out_o = {27'b0,inst_i[19:15]};
```

注意 `{`IMM_U,1'bx}` 用了 `x`：U 型立即数本来就占满高位，不需要看 `inst[31]`。文件里还预先算了两个常用量 `imm_result_inst_1`/`imm_default_result_inst_0`（[gen_imm.v:55-57](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/gen_imm.v#L55-L57)），供 `sel_i` 取 `4'b1010`~`4'b1111` 的几条指令复用。

#### 4.4.4 代码实践

**实践目标**：以 I 型立即数为例验证符号扩展。

**操作步骤**：
1. 读 [gen_imm.v:63-65](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/gen_imm.v#L63-L65)。
2. 假设 `inst_i[31:20] = 12'hFFF`（即 -1）、`sel_i = IMM_I`。则 `inst_i[31]=1`，命中 `{`IMM_I,1'b1}` 分支，输出 `{{20{1'b1}}, 12'hFFF} = 32'hFFFFFFFF`（即 -1）。

**预期结果**：立即数被正确符号扩展为 32 位的 -1。这是纯逻辑推导，**待本地验证**可在仿真里强制驱动 `gen_imm` 输入观察输出。

#### 4.4.5 小练习与答案

**练习 1**：`IMM_B` 立即数最低位为什么恒为 0？
**答案**：分支目标按字（4 字节）对齐，地址偏移以字节计但末位必为 0，故 B 型立即数末位补 0（见 `gen_imm.v:70-71`）。

**练习 2**：向量立即数 `IMM_V` 与其它类型有何不同？
**答案**：它会结合 `imm_ext_i`（REGEXT 前缀指令扩展出来的位），形成更宽/带符号控制的立即数（见 `gen_imm.v:79` 的三元判断链）。

---

### 4.5 collector_unit：状态机与操作数组装

#### 4.5.1 概念说明

`collector_unit` 是采集子系统的**心脏**。每个采集器只服务一条指令（同一时刻只装一条指令的「子弹」）。它做四件事：

1. 接收 `inst_demux` 分发来的译码控制信号与寄存器号；
2. 为 4 个操作数槽（src1、src2、src3、掩码）逐一判定**来源类型**（标量/向量/立即数/PC/size）；
3. 对需要读寄存器堆的来源，算出 `bankID` 与 `rsAddr` 并发请求；对立即数/PC/size 直接就地生成；
4. 4 个槽全部就绪后，把组装好的 `alu_src1/2/3` 与 `active_mask` 送到 `issue`。

#### 4.5.2 核心流程：三态状态机

采集器用 `S_IDLE → S_ADD → S_OUT` 三态机驱动（[collector_unit.v:214-289](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/collector_unit.v#L214-L289)）：

```
S_IDLE ──control_fire──▶ 若所有操作数已就绪(立即数/PC) ──▶ S_OUT
   │                   否则(需读寄存器堆) ──────────────▶ S_ADD
   ▼
S_ADD  ──所有 valid_reg 对应的 ready_reg 都为 1──▶ S_OUT   (逐个收齐读回数据)
   │
   ▼
S_OUT  ──issue_fire(被输出仲裁选中并成交)──▶ S_IDLE      (腾出采集器接下一条)
```

- `control_ready_o = (S_IDLE) && !(|valid_reg)`：采集器只在 IDLE 且无在途请求时才接收新指令。
- `control_fire` 触发后，状态机先把控制信号锁存进 `controlReg_*`，并据 `sel_alu*` 计算 `ready_wire`：若某槽是立即数/PC/size，它当场就绪（`ready_wire=1`）；否则需读堆（`ready_wire=0`）。
- 若全部就绪则直奔 `S_OUT`；否则进 `S_ADD`，每拍接收一个经 crossbar 回送的数据，置对应 `ready_reg`，直到 4 槽全齐。

#### 4.5.3 源码精读

**操作数来源类型映射**。状态机在 `S_IDLE` 命中 `control_fire` 时，依据译码给出的 `sel_alu1/2/3_i` 计算每个槽的 `rsType_wire`（2 位：0=PC/size/mask源、1=标量、2=向量、3=立即数），并据此决定哪些槽需读堆（[collector_unit.v:703-719](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/collector_unit.v#L703-L719)）：

```verilog
// collector_unit.v:703-712
rsType_wire[1:0] = control_sel_alu1_i;            // src1 类型
rsType_wire[3:2] = control_sel_alu2_i;            // src2 类型
case(control_sel_alu3_i)
  `A3_VRS3:  rsType_wire[5:4] = 2'b10;            // src3 = 向量
  `A3_FRS3:  rsType_wire[5:4] = 2'b01;            // src3 = 标量(浮点)
  `A3_PC:    rsType_wire[5:4] = ...;              // src3 = PC/分支
  ...
```

这些 `A1_*/A2_*/A3_*` 宏定义在 [define.v:424-439](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L424-L439)，例如 `A1_IMM=2'b11`、`A1_VRS1=2'b10`、`A2_RS2=2'b01`。

**bankID 与 rsAddr 计算**。这是连接采集器与寄存器堆的关键（[collector_unit.v:748-784](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/collector_unit.v#L748-L784)）：

```verilog
// collector_unit.v:750-751  bankID = (wid低位 + regIdx低位)
outArbiter_bankID_o[...] = control_wid_i[`DEPTH_BANK-1:0]
                         + regIdx_wire[...+`DEPTH_BANK-1:...];
// collector_unit.v:768-770  向量 rsAddr = (vgpr_base + regIdx) / NUM_BANK
outArbiter_rsAddr_o[...] = (vgpr_base_i[...wid...] >> `DEPTH_BANK)
                         + (regIdx_wire[...] >> `DEPTH_BANK);
```

读请求的 valid 只在该槽未就绪时拉高（[collector_unit.v:787-790](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/collector_unit.v#L787-L790)）。

**数据回填与组装**。crossbar 把读出的数据连同 `regOrder`（0/1/2/3 对应 src1/src2/src3/掩码）送回，采集器据 `bankIn_regOrder_i` 把数据填进 `rs_reg` 的对应段并置 `ready_reg`。例如 src2 的回填（[collector_unit.v:510-516](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/collector_unit.v#L510-L516)）：

```verilog
// collector_unit.v:510-515  regOrder==01 表示填 src2
else if(bankIn_fire[0] && bankIn_regOrder_i[...]==2'b01) begin
  case(rsType)  // 标量广播成 NUM_THREAD 份；向量原样
    `A2_RS2:  rs_reg[...src2...] <= {`NUM_THREAD{bankIn_data_i[...0号...]}};
    `A2_VRS2: rs_reg[...src2...] <= bankIn_data_i[...向量整段...];
  endcase
  ready_reg[1] <= 1'b1;
end
```

注意标量操作数会被**复制（广播）成 `NUM_THREAD` 份**送入执行单元——这正是「标量-向量」运算（如 `VADD_VX`）的硬件实现：标量值广播到所有 lane。最后，组装好的操作数由 [collector_unit.v:827-830](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/collector_unit.v#L827-L830) 输出，`issue_valid_o = (current_state==S_OUT)`。

#### 4.5.4 代码实践

**实践目标**：跟踪一条向量加 `v3 = v1 + v2`（wid=1）的源操作数采集全过程。

**操作步骤**：
1. 假设 `reg_idx1=v1`、`reg_idx2=v2`、`reg_idxw=v3`，`sel_alu1=A1_VRS1`、`sel_alu2=A2_VRS2`（src1/src2 均向量）。
2. **采集器发起读请求**：在 [collector_unit.v:750](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/collector_unit.v#L750) 算出 `bankID(v1) = (wid + v1) % 4`，在 [collector_unit.v:770](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/collector_unit.v#L770) 算出 `rsAddr(v1) = (vgpr_base[wid] + v1) / 4`，`rsType=2'b10`（向量）；v2 同理。
3. **仲裁与读出**：`operand_arbiter` 把 v1、v2 各自命中 bank 的请求挑出（标量/向量分流，[operand_arbiter.v:123-126](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/operand_arbiter.v#L123-L126)），喂给对应 `vector_regfile_bank` 读口，1 拍后数据与 `chosen` 一同出现。
4. **数据路由回填**：`operandcollector_top` 把 `chosen` 延 1 拍对齐 SRAM 读延迟（[operandcollector_top.sv:440-452](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/operandcollector_top.sv#L440-L452)），`crossbar` 据 `chosen` 把 v1 数据填回 src1 槽（regOrder=0）、v2 填回 src2 槽（regOrder=1）。
5. **组装发射**：两槽 `ready_reg` 都置 1 → 状态机进 `S_OUT` → `issue_alu_src1_o/src2_o` 输出，经输出仲裁送给执行单元。

**需要观察的现象**：从 `control_fire` 到 `issue_valid_o` 拉高，至少经历 1 拍读堆延迟（若两源落不同 bank 可同拍并行；落同 bank 则多花 1 拍）。

**预期结果**：你能画出 v1、v2 两条读请求在 `outArbiter → arbiter → bank → crossbar → collector_unit` 上的完整往返路径，并指出标量广播（`{NUM_THREAD{...}}`）与向量原样的区别。运行级波形验证**待本地验证**（可用 `make verdi` 在 `collector_unit` 内观察 `current_state` 与 `ready_reg`）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `control_ready_o` 要包含 `!(|valid_reg)` 条件？
**答案**：保证一个采集器同一时刻只装一条指令——只要还有在途的读请求（`valid_reg` 非全 0），就不接收新指令，避免新旧指令的操作数互相覆盖。

**练习 2**：若一条指令的两个源操作数恰好落到同一个 bank，会发生什么？
**答案**：`operand_arbiter` 的 `round_robin_arb` 该拍只服务其中一个，另一个下一拍才得到服务，采集器因此多停留一拍 `S_ADD`，表现为 bank 冲突带来的额外延迟。

**练习 3**：标量-向量加 `v3 = v1 + x5`（x5 为标量），x5 的值如何变成 `NUM_THREAD` 路？
**答案**：回填时命中 `A2_RS2` 分支，用 `{NUM_THREAD{bankIn_data_i[...0号lane...]}}` 把单个标量复制成 `NUM_THREAD` 份（[collector_unit.v:512](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/collector_unit.v#L512)），实现标量广播。

---

## 5. 综合实践

**任务**：以「规模参数 → bank 几何 → 一条向量指令的采集路径」为主线，把本讲知识串起来，亲手算一遍并对照源码验证。

**步骤**：

1. **算几何**。读 [define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L27-L49)，填表：

   | 量 | 表达式 | 你的计算值 |
   |---|---|---|
   | 每 bank 项数 | `NUM_VGPR/NUM_BANK` | 256 |
   | `DEPTH_REGBANK` | `$clog2(上)` | 8 |
   | 向量 bank 每项位宽（NUM_THREAD=4） | `XLEN*NUM_THREAD` | 128 |
   | 向量 bank 每项位宽（NUM_THREAD=32，综合） | `XLEN*NUM_THREAD` | 1024 |

2. **画路径**。选 `VADD_VV v3,v1,v2`（wid=1），在纸上画出：
   - 采集器算 `bankID`、`rsAddr`（标出公式）；
   - `operand_arbiter` 标量/向量分流 + 每 bank 轮询；
   - `vector_regfile_bank` 读出 → `chosen` 延 1 拍 → `crossbar` 按 `cu_id`/`regOrder` 回填；
   - `collector_unit` 两槽就绪 → `S_OUT` → 输出仲裁 → issue。

3. **设冲突**。改成「v1、v2 落同一 bank」的情形，在图上标出多出的 1 拍 `S_ADD`，并解释 `round_robin_arb` 如何保证两者都得到服务。

4. **核对源码**。把图上每个箭头对应到本讲给出的永久链接（行号），确认无遗漏。

**验收标准**：能脱稿讲清「一条向量指令的源操作数如何经 operand_arbiter → collector_unit → vector_regfile_bank 读出并组装」，并能解释 bank 划分与冲突处理。波形级验证**待本地验证**。

## 6. 本讲小结

- 操作数采集子系统是一个「1→N 分发 → bank 仲裁读出 → N→1 汇聚」的沙漏，顶层 `operandcollector_top` 只做连线。
- 寄存器堆按 `NUM_BANK=4` 交错分体，每 bank 256 项；标量 bank 每项 32 位，向量 bank 每项 `32×NUM_THREAD` 位并支持逐 lane 掩码写与 v0 跟踪。
- `operand_arbiter` 把海量读请求按 bank 归类、标量/向量分流，每 bank 用 `round_robin_arb` 解决冲突，输出 `chosen` 供回程路由。
- `collector_unit` 用 `IDLE→ADD→OUT` 三态机，为 4 个操作数槽逐一判定来源（标量/向量/立即数/PC），读回数据后组装 `alu_src1/2/3` 与 `active_mask`。
- `gen_imm` 按指令类型（I/S/B/U/J/Z/V）抽取并符号扩展立即数，使立即数操作数免访问寄存器堆。
- 标量操作数在回填时被广播成 `NUM_THREAD` 份，这是「标量-向量」运算的硬件基础。

## 7. 下一步学习建议

- 下一讲 [u4-l2 向量 ALU valu](u4-l2-vector-alu.md) 将接住本讲送出的 `alu_src1/2/3`，讲解它们如何被分发到各 lane 的 `alu` 并行执行 `FN_ADD` 等运算，建议先回顾本讲的「标量广播」概念。
- 想加深对回程路由的理解，可精读 `crossbar.sv` 全文，重点关注 `cu_id = chosen>>2`、`regOrder = chosen[1:0]` 的拆分。
- 想理解写回侧，可在 `operandcollector_top.sv` 中跟踪 `writeScalar_*`/`writeVector_*` 接口如何驱动 bank 的写口（注意 `wb_*_bankID`/`wb_*_rsAddr` 与读侧用同一套地址映射）。
