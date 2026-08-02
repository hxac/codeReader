# 向量 ALU valu

## 1. 本讲目标

本讲聚焦 SM 流水线中最常用的一类执行单元——**整数向量 ALU（vALU）**。学完本讲你应当能够：

1. 说清楚「一条向量指令如何在一个 warp 内被拆分到 NUM_THREAD 个 lane 并行执行」，理解 SIMT lane 级并行的硬件落地方式。
2. 读懂 `valu_top` → `valu` → `alu` 三层结构：为什么有一个外壳、为什么是 `generate` 循环例化的 lane 阵列、单 lane 内部又如何用一组功能码 `FN_*` 复用同一套加法器/移位器/比较器。
3. 区分**向量 ALU（vALU）**与**标量 ALU（aluexe，即 sALU）**：它们共用同一个 `alu` 内核，但一个并行处理一整个 warp、只负责向量写回，另一个处理单条标量数据并额外承担标量分支。
4. 在源码层面跟踪一条 `VADD_VV` 从 `issue` 到 `valu` 再到 `writeback` 的完整通路，并能解释 `VADD_VX`（标量-向量）在操作数采集上的差异。

## 2. 前置知识

在进入本讲前，请确认你已经掌握以下概念（它们在依赖讲义中已建立）：

- **warp 与 thread/lane**：Ventus 用 RISC-V 向量语义驱动 SIMT 硬件，一条向量指令广播给整个 warp，warp 内 `NUM_THREAD` 条线程（也即 `NUM_LANE` 个 lane）在同一拍各自执行同一条指令、处理各自的数据。规模总开关在 [define.v:11-13](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L11-L13)：`NUM_THREAD` 即每 warp 线程数，`NUM_LANE = NUM_THREAD`。
- **操作数采集（operand_collector）**：指令发射前，采集器会从向量/标量寄存器堆读出 `in1/in2/in3`（向量操作数是 `NUM_THREAD*XLEN` 位的「一整排」）和 `mask`（每线程一位的活跃掩码）。本讲的 vALU 就是这些操作数的「消费方」。一个关键性质：**标量操作数在采集阶段被广播成 NUM_THREAD 份**，所以等数据进到 vALU 时，标量-向量与向量-向量指令的数据排布看起来是一样的（详见 u4-l1）。
- **发射（issue）按指令类型路由**：`issue.v` 是一个纯组合的优先级路由器，把一条指令的握手接到唯一一个执行单元。向量整数指令走 vALU，标量整数指令与标量分支走 sALU（aluexe）。
- **`FN_*` 功能码**：`define.v` 中按执行单元分命名空间的功能码，6 位宽，编码可跨单元复用（如 `FN_ADD` 整数与浮点同名但分属不同通路）。
- **流握手（valid/ready）与 `stream_fifo_pipe_true`**：项目里执行单元普遍用一个深度为 1（或 2）的流水 FIFO 把「当拍算完的组合结果」打一拍，实现对上游的反压、对下游的解耦。本讲两个 ALU 单元都用到它。

> 提示：如果你还没读过 u3-l4（issue/scoreboard）和 u4-l1（operand_collector），建议先读，本讲会直接沿用其中的术语。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [src/gpgpu_top/sm/pipeline/valu/valu_top.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu_top.v) | 向量 ALU 顶层外壳。用 `ALU_NOT_FOLD` 宏在「非折叠（每拍处理一个完整 warp）」与「折叠（硬件 lane 数少于线程数，分多拍迭代）」两种实现间切换。默认走非折叠的 `valu`。 |
| [src/gpgpu_top/sm/pipeline/valu/valu.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu.v) | 非折叠向量 ALU 主体。用 `generate` 循环例化 `HARD_THREAD` 个 `alu` lane，逐 lane 喂入操作数与功能码，并把结果按 lane 重新拼成一整排；同时处理若干「伪指令」特例与 SIMT 分支掩码输出。 |
| [src/gpgpu_top/sm/pipeline/valu/alu.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/alu.v) | 单 lane 的纯组合标量 ALU 内核。实现加减、移位、逻辑、比较、min/max 等；向量与标量 ALU 都复用它。 |
| [src/gpgpu_top/sm/pipeline/aluexe.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/aluexe.v) | 标量 ALU 执行单元（sALU）。例化**单个** `alu`，除标量写回外还产出标量分支结果（jump/new_pc）送给 `branch_back`。 |
| [src/define/define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v) | 规模参数与 `FN_*` 功能码、分支类型 `B_*` 的定义来源。 |
| [src/gpgpu_top/sm/pipeline/pipe.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v) | 把 `valu_top`（例化名 `alu`）与 `aluexe`（例化名 `salu`）连入流水线的位置：上游接 `issue`，下游接 `writeback` 与 `branch_back`/`simt_stack`。 |

一句话总览数据流：`issue_out_vALU_*`（来自操作数采集器）→ `valu_top`（外壳）→ `valu`（lane 阵列）→ 各 lane 的 `alu` 并行算出 `alu_out[i]` → 拼成 `wb_wvd_rd` 打一拍 → `writeback` 写回向量寄存器堆；若是 SIMT 分支类指令，则改走 `if_mask` 通路送给 `simt_stack`。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，对应四层「洋葱」：从外到内依次是 `valu_top`（外壳与配置）→ `valu`（lane 阵列与并行调度）→ `alu`（单 lane 运算核）→ `aluexe`（标量兄弟与分支）。

### 4.1 valu_top：向量 ALU 的外壳与折叠/非折叠选择

#### 4.1.1 概念说明

`valu_top` 自己不做任何运算，它是一个**配置外壳**：用条件编译在两种实现间二选一。

- **非折叠（ALU_NOT_FOLD）**：硬件 lane 数（`HARD_THREAD`）等于线程数（`SOFT_THREAD`），一拍就能处理完一个 warp 的全部线程。这是默认且当前唯一启用的路径——文件第 17 行 `define ALU_NOT_FOLD` 直接把宏定义死。
- **折叠（ valu_v2）**：硬件 lane 数少于线程数，一个 warp 要分 `MAX_ITER = SOFT/HARD` 拍迭代处理完。`else` 分支保留了这个实现，但默认不编译。

这层外壳的意义在于**面积换性能的可调旋钮**：lane 是最贵的运算资源之一，若综合时面积吃紧，可以减少物理 lane 数、用多拍迭代换面积；默认仿真与综合都走「全并行」。

#### 4.1.2 核心流程

```text
valu_top 端口（SOFT_THREAD=NUM_THREAD 个线程位宽）
        │
        ├── ifdef ALU_NOT_FOLD ──► 例化 valu（非折叠，默认）
        │                          SOFT_THREAD = NUM_THREAD
        │                          HARD_THREAD = NUM_THREAD
        └── else               ──► 例化 valu_v2（折叠，含 MAX_ITER）
```

三个参数的默认关系在 [pipe.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1640-L1644) 例化时给出：

- `SOFT_THREAD = NUM_THREAD`（逻辑线程数）
- `HARD_THREAD = NUMBER_ALU`（物理 lane 数），而 [`NUMBER_ALU`](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L264) 在 define.v 中等于 `NUM_THREAD`
- `MAX_ITER = NUM_THREAD/NUMBER_ALU = 1`（非折叠时恰好 1 拍）

注意：非折叠 `valu` 实际并不使用 `MAX_ITER`，它的循环次数直接由 `HARD_THREAD` 决定。

#### 4.1.3 源码精读

外壳端口把向量操作数按线程排布打包：`in1_i` 的位宽是 `SOFT_THREAD*XLEN`，即 NUM_THREAD 个 32 位数据首尾相接。功能码 `ctrl_alu_fn_i` 是 6 位（与 define.v 的 `FN_*` 一致）：

[valu_top.v:19-55](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu_top.v#L19-L55) 定义了完整的输入输出端口；其中端口里保留了 `in3_i`，但默认的非折叠 `valu` 并不接收它（见 4.1.4）。

[valu_top.v:57-92](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu_top.v#L57-L92) 是默认分支：在 `ALU_NOT_FOLD` 下例化 `valu`，端口一一对应转发；注意第 71 行 `.in3_i` 被注释掉，说明非折叠向量 ALU **不使用第三操作数**（乘加类在 vmul/tensor 通路，不在 vALU）。

[valu_top.v:93-130](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu_top.v#L93-L130) 是折叠分支 `valu_v2`，会用到 `MAX_ITER` 与 `in3_i`，默认不编译，了解其存在即可。

#### 4.1.4 代码实践：确认你跑的是哪条路径

1. **实践目标**：确认当前仿真/综合使用的是非折叠 `valu`，而非 `valu_v2`。
2. **操作步骤**：打开 [valu_top.v:17](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu_top.v#L17)，看到文件顶部写死了 `` `define ALU_NOT_FOLD ``；再打开 pipe.v 例化处，确认 `HARD_THREAD` 取 `NUMBER_ALU`、`MAX_ITER` 为 1。
3. **需要观察的现象**：因为宏在文件内硬编码，只要 `valu_top.v` 被编译，`valu`（非折叠）就一定会被例化。
4. **预期结果**：lane 数 = `NUMBER_ALU` = `NUM_THREAD`；若你在 define.v 把 `NUM_THREAD` 改成 8，则 vALU 内会例化 8 个 `alu` lane（4.2 节会看到这个循环）。
5. 待本地验证：若要切换到折叠实现，需要注释掉第 17 行的 `define` 并相应调小 `HARD_THREAD`，验证 `MAX_ITER` 的迭代行为——本讲不展开。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `valu_top` 要用条件编译而不是用参数 `parameter` 来切换两套实现？
**参考答案**：两套实现例化的子模块不同（`valu` vs `valu_v2`），且端口集合略有差异（如 `in3_i`、`MAX_ITER`）。例化哪个子模块是「结构选择」而非「数值配置」，用 `generate`/`ifdef` 在**编译期**决定结构，比运行期参数更自然，也避免把不用的子模块例化进设计、浪费面积或引入悬空端口。

---

### 4.2 valu：lane 阵列与逐线程并行

#### 4.2.1 概念说明

`valu` 是「SIMT lane 级并行」真正落地的地方。它的核心思想一句话：**同一个 `alu` 内核，用 `generate for` 循环复制 `HARD_THREAD` 份，每一份（一个 lane）处理一个线程的数据；所有 lane 共享同一组控制信号（功能码、wid、写回寄存器号），只是数据各走各的。**

这正是 GPU「一条指令广播、多数据并行」的硬件写照：控制信号是广播的，数据是排开的。于是：

\[ \text{一拍完成的运算量} = \text{NUM\_THREAD} \times (\text{一条 32 位标量运算}) \]

除了「常规」的逐 lane 运算，`valu` 还处理几类无法简单「逐 lane 复制」的特例——统称伪指令：归约型掩码逻辑（`VMANDNOT/VMORNOT/VMNAND/VMNOR/VMXNOR`）、写出线程编号的 `VID`、按掩码二选一的 `VMERGE`，以及操作数前后换序的 `ctrl_reverse_i`。

#### 4.2.2 核心流程

```text
对每个 lane i = 0..HARD_THREAD-1（并行）：
  1. 按 ctrl_alu_fn_i / ctrl_reverse_i 判定本 lane 的：
       alu_op[i]   ← 送给 alu 的 5 位功能码
       alu_in1[i]  ← 32 位输入1（可能被 reverse 对调）
       alu_in2[i]  ← 32 位输入2
       wb_wvd_rd[i]← 本 lane 的 32 位结果（多数=alu_out[i]，特例外）
       wvd_mask[i] ← 本 lane 是否写回（多数=mask_i[i]）
  2. alu 内核纯组合算出 alu_out[i] 与比较结果 alu_cmp[i]
  3. if_mask[i] = ~alu_cmp[i]   （供 SIMT 分支使用）

所有 lane 的 wb_wvd_rd 拼成 SOFT_THREAD*XLEN 位，连同 wvd_mask/wid/reg_idxw
  → 打一拍 stream_fifo(U_result)  → writeback
若该指令是 SIMT 分支类（ctrl_simt_stack_i）：
  {wid, if_mask} → 打一拍 stream_fifo(U_result_simt) → simt_stack
```

#### 4.2.3 源码精读

**lane 阵列的核心**是 [valu.v:87-171](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu.v#L87-L171) 的 `generate for` 循环。每个 lane 例化一个 `alu`（[valu.v:88-98](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu.v#L88-L98)）：

```verilog
alu #(.OPCODE_WIDTH(5)) U_alu_i(
  .op_i (alu_op[(i+1)*5-1-:5]        ),  // 本 lane 的 5 位功能码
  .in1_i(alu_in1[(i+1)*`XLEN-1-:`XLEN]),  // 本 lane 的 32 位输入1
  .in2_i(alu_in2[(i+1)*`XLEN-1-:`XLEN]),  // 本 lane 的 32 位输入2
  .out_o(alu_out[i]                   ),  // 本 lane 的 32 位结果
  .cmp_o(alu_cmp[i]                   )); // 本 lane 的比较结果
```

注意 `alu_op` 是 5 位（`alu` 内核 `OPCODE_WIDTH=5`），而外部 `ctrl_alu_fn_i` 是 6 位——`valu` 在赋值时只取低 5 位 `ctrl_alu_fn_i[4:0]`，这一点在特例分支里随处可见（如 [valu.v:162-164](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu.v#L162-L164) 的默认分支）。所以 define.v 里 6 位的 `FN_*` 与 alu.v 里 5 位的 `localparam FN_*` 是靠「截低 5 位」对齐的，二者数值必须一致。

**特例处理**用一个大 `always @(*)` 表达（[valu.v:103-169](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu.v#L103-L169)），按优先级：

1. **操作数换序** `ctrl_reverse_i`（[valu.v:104-111](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu.v#L104-L111)）：把 `in1` 与 `in2` 对调后再送 alu。这用于「反向减法/反向比较」类指令——同一个 `FN_SUB`，正着算 `a-b`、反着算 `b-a`，硬件只做一次换序即可复用。
2. **掩码归约伪指令** `VMANDNOT/VMORNOT/VMNAND/VMNOR/VMXNOR`（[valu.v:113-141](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu.v#L113-L141)）：这类指令没有专门的运算码，而是**映射成基本逻辑码再取反**。例如 `VMANDNOT` 映射成 `{4'd3, ctrl_alu_fn_i[0]}`，即 `5'b00111 = 7 = FN_AND`，并把 `in1` 取反（`~in1`）；`VMXNOR` 映射成 `FN_XOR` 再对结果取反（`wb_wvd_rd = ~alu_out[i]`）。巧妙之处在于用基本 AND/OR/XOR 复用同一套逻辑门。
3. **`FN_VID`**（[valu.v:143-150](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu.v#L143-L150)）：结果是 lane 编号 `i` 本身——给每个线程写回自己的编号（vector index），不需要运算。
4. **`FN_VMERGE`**（[valu.v:152-159](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu.v#L152-L159)）：按掩码二选一 `mask_i[i] ? in1 : in2`，且 `wvd_mask[i] = 1'b1`（merge 指令无论掩码都写回）。
5. **默认**（[valu.v:161-168](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu.v#L161-L168)）：`alu_op = ctrl_alu_fn_i[4:0]`，结果 `= alu_out[i]`，掩码 `= mask_i[i]`。绝大多数向量算术/逻辑/比较指令（加、减、与、或、异或、移位、SLT…）都走这里。

**SIMT 分支掩码**：[valu.v:101](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu.v#L101) 定义 `if_mask[i] = ~alu_cmp[i]`。对于向量条件分支（如 VBEQ），比较结果 `alu_cmp[i]` 表示「该线程条件成立」，取反后 `if_mask` 表示「该线程该走 else 分支」的掩码，送给 `simt_stack` 处理发散/汇合（详见 u5-l3）。

**输出缓冲与双通路**：[valu.v:199-225](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu.v#L199-L225) 例化两个深度为 1 的 `stream_fifo_pipe_true`：`U_result` 走常规向量写回，`U_result_simt` 走 SIMT 掩码。两条通路由 `ctrl_simt_stack_i` 二选一：

- 写回打包 [valu.v:227](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu.v#L227)：`{ctrl_wid, wb_wvd_rd, ctrl_reg_idxw, ctrl_wvd, wvd_mask}`；有效条件 [valu.v:228](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu.v#L228) 为 `in_valid_i & ctrl_wvd_i & (!ctrl_simt_stack_i)`——即「是写回类且非 SIMT 分支类」。
- 上游反压 [valu.v:257](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu.v#L257)：`in_ready_o = ctrl_simt_stack_i ? result_simt_in_ready : result_in_ready`。

> 这条「打一拍」很关键：`alu` 是纯组合的，组合结果直接送回 `writeback` 会形成长组合路径；插入深度 1 的流水 FIFO 既切断了关键路径，又提供了标准的 valid/ready 反压接口。

#### 4.2.4 代码实践：以 VADD_VV 跟踪 lane 阵列

1. **实践目标**：理解一条向量加法如何被「摊」到各 lane。
2. **操作步骤**：
   - 在 [valu.v:161-168](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu.v#L161-L168) 确认默认分支：`alu_op[i] = ctrl_alu_fn_i[4:0]`。
   - 查 define.v 得 [`FN_ADD = 6'd0`](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L471)；VADD_VV 译码后 `ctrl_alu_fn_i = FN_ADD`，故每个 lane 的 `alu_op = 5'd0`。
   - 于是 lane `i` 的 `alu_in1[i] = in1_i` 第 i 段、`alu_in2[i] = in2_i` 第 i 段；`alu` 内核执行 `FN_ADD`（加法），得到 `alu_out[i] = in1[i] + in2[i]`；`wb_wvd_rd[i] = alu_out[i]`。
3. **需要观察的现象**：在波形（test.fsdb）中找到 `alu` 例化名 `U_alu_i`（i=0..NUM_THREAD-1），应看到同一拍所有 lane 的 `op_i` 都是 `5'd0`，而 `in1_i/in2_i` 各不相同、`out_o` = 对应 lane 的和。
4. **预期结果**：NUM_THREAD 个 lane 在同一拍各自完成一次 32 位加法，结果按 lane 顺序拼成 `wb_wvd_rd`，打一拍后写回目的向量寄存器。
5. 待本地验证：具体波形信号名与编译层次相关，建议在 Verdi 中沿 `issue_out_vALU_vExeData_in1` → `valu.in1_i` → `U_alu_i.in1_i` 层次展开确认。

#### 4.2.5 小练习与答案

**练习 1**：`VMANDNOT` 为什么映射成 `FN_AND` 并对 `in1` 取反，而不是新设一个功能码？
**参考答案**：硬件上 `a AND-NOT b = a & (~b)`，完全可以用现成的 AND 门加一个输入端取反实现，不必新增运算通路。`valu` 在 [valu.v:115-117](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu.v#L115-L117) 把 `alu_op` 设为 `{4'd3, ctrl_alu_fn_i[0]}`（即 AND/OR），再把 `in1` 取反喂入，从而复用同一套逻辑门，节省面积。

**练习 2**：`if_mask[i] = ~alu_cmp[i]` 中的取反有什么语义？
**参考答案**：`alu_cmp[i]` 为 1 表示该线程比较条件「成立」（走 then 分支），取反后 `if_mask` 标记的是「条件不成立、需走 else 分支」的线程集合。这个掩码正是 SIMT 栈判断 warp 内发散的依据，故取反后直接送给 `simt_stack`。

---

### 4.3 alu：单 lane 的组合逻辑运算核

#### 4.3.1 概念说明

`alu` 是被 `valu`（每 lane 一个）和 `aluexe`（单个）共同复用的**纯组合标量运算核**：输入两个 32 位操作数和一个 5 位功能码，输出一个 32 位结果和一个 1 位比较标志。它没有时钟、没有状态，就是一张大的组合查找/计算表。

它的设计哲学是**资源共享**：加法、减法、比较共用一个加法器；左移、右移共用一个桶形移位器（靠「位反转」技巧让左移复用右移电路）；AND/OR/XOR 共用一组逻辑门。最后用多路选择器按 `op_i` 选出 `out_o`。

#### 4.3.2 核心流程

```text
解码 op_i → 几个 1 位控制：isSub / isCmp / cmpUnsigned / cmpInverted / cmpEq / isMIN

加法/减法（共享加法器）：
  in2_inv = isSub ? ~in2 : in2
  adder_out = in1 + in2_inv + isSub        // 减法=补码加

比较 SLT/SLTU/SGE/SGEU（复用 adder_out 与符号位）：
  slt = (同号) ? adder_out[31] : (无符号?in2[31]:in1[31])
  cmp_o = cmpInverted ^ (cmpEq ? (in1==in2) : slt)

移位 SLL/SRL/SRA（左移靠位反转复用右移）：
  in1_rev = reverse(in1);  shin = 右移类? in1 : in1_rev
  shout_r = 算术/逻辑右移 shin >> shamt
  shout_l = reverse(shout_r)               // 反转回来即左移结果

逻辑 AND/OR/XOR：and_or_xor
min/max（有符号/无符号）

out_o = op 选择：FN_A1ZERO?in2 : FN_A2ZERO?in1 : isMIN?minmaxout : out
```

#### 4.3.3 源码精读

**功能码与解码**：[alu.v:31-58](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/alu.v#L31-L58) 定义 5 位 `localparam`：基本运算 `FN_ADD=0, FN_SL=1, FN_SEQ=2, FN_SNE=3, FN_XOR=4, FN_SR=5, FN_OR=6, FN_AND=7, FN_A1ZERO=8, FN_A2ZERO=9`，加减比较族 `FN_SUB=10, FN_SRA=11, FN_SLT=12, FN_SGE=13, FN_SLTU=14, FN_SGEU=15`，极值 `FN_MAX=16, FN_MIN=17, FN_MAXU=18, FN_MINU=19`，以及 `FN_MUL..FN_NMSUB=20..27`（**仅声明、未实现**——乘法走 vmul 通路，本模块不处理，对应的 `isMUL/isMAC` 解码在 [alu.v:100-101](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/alu.v#L100-L101) 被注释掉）。注意这些数值与 define.v 的 6 位 `FN_*` 低 5 位完全一致，这是「截位对齐」能成立的基础。

**解码控制位**用位段比较实现（[alu.v:94-99](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/alu.v#L94-L99)）：`isSub` 覆盖 SUB/SRA/SLT/SGE/SLTU/SGEU（10–15），`isCmp` 覆盖比较类（12–15），并从 `op_i` 各 bit 抽取无符号/反向/相等标志。

**加减与比较共享加法器**（[alu.v:104-112](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/alu.v#L104-L112)）：

```verilog
assign in2_inv   = isSub ? (~in2_i) : in2_i;
assign adder_out = in1_i + in2_inv + isSub;   // 减法 = 补码加
```

减法把 `in2` 取反再加 1（`+isSub` 即 `+1`），与加法共享同一个 32 位加法器。`slt`（小于）则直接复用加法器结果的符号位 `adder_out[31]`，再按符号位是否一致、是否有符号做修正——这是经典的有符号比较器实现。

**移位器位反转复用**（[alu.v:117-136](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/alu.v#L117-L136)）：先用 `generate` 把 `in1` 按位反转成 `in1_rev`，对反转后的数据做右移，再把结果反转回来——这样就得到了左移，左移与右移共用同一套桶形移位逻辑。算术右移通过 `{{32{isSub & shin[31]}}, shin} >> shamt` 用符号位填充实现。

**最终选择**（[alu.v:158-160](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/alu.v#L158-L160)）：

```verilog
assign out_o = op_i == FN_A1ZERO ? in2_i :     // 把输入1清零，结果=in2
               (op_i == FN_A2ZERO ? in1_i :     // 把输入2清零，结果=in1
               (isMIN ? minmaxout : out));      // 否则：min/max 或加减移位逻辑比较
```

`FN_A1ZERO/FN_A2ZERO` 是「屏蔽某一路输入」的辅助码，配合伪指令使用。

#### 4.3.4 代码实践：对照 FN_SLT 验证比较器

1. **实践目标**：验证有符号小于（SLT）的实现路径。
2. **操作步骤**：
   - 取 `FN_SLT = 6'd12`（[define.v:483](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L483)），截低 5 位 = `5'd12`。
   - 在 [alu.v:94-99](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/alu.v#L94-L99) 确认：`isSub=1`、`isCmp=1`、`cmpUnsigned=op_i[1]=0`、`cmpInverted=op_i[0]=0`、`cmpEq=~op_i[3]=0`。
   - 因此 [alu.v:112](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/alu.v#L112) `cmp_o = 0 ^ (0 ? ... : slt) = slt`，即有符号小于的结果。
3. **需要观察的现象**：构造 `in1 = -1 (0xFFFFFFFF)`、`in2 = 1 (0x00000001)`，因为同号（都是负/正？实际符号位 in1[31]=1, in2[31]=0 不同号），`slt = cmpUnsigned? in2[31] : in1[31] = in1[31] = 1`，即「-1 < 1」成立。
4. **预期结果**：`cmp_o = 1`。
5. 待本地验证：可在 testbench 里临时驱动一个 `alu` 例化或写一个最小 stim 验证上述真值。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `alu` 里没有看到乘法的实现，却有 `FN_MUL` 等局部参数？
**参考答案**：`alu` 是「加减/移位/逻辑/比较/极值」的组合核，乘法（`FN_MUL/MULH/MULHU/MULHSU` 与乘加类）延迟大、用专门的阵列乘法器实现，走 `vmul` 通路（见 u4-l3）。这里的 `FN_MUL` 等 `localparam` 只是占位、未接线，`isMUL/isMAC` 解码行已被注释（[alu.v:100-101](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/alu.v#L100-L101)）。

**练习 2**：左移（SLL）如何复用右移电路？
**参考答案**：先把输入按位反转（`in1_rev`），对反转后的数据做逻辑右移，再把结果按位反转回来，等价于对原数据左移相同位数。这样左移与右移/算术右移共用同一个桶形移位器，省下一半移位逻辑（[alu.v:117-136](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/alu.v#L117-L136)）。

---

### 4.4 aluexe：标量 ALU 与分支的接入

#### 4.4.1 概念说明

`aluexe`（在 pipe.v 里例化名为 `salu`）是 vALU 的「标量兄弟」：它**只例化一个 `alu`**（不是 lane 阵列），处理标量整数运算，并额外承担**标量分支**。`issue` 路由的优先级里（详见 u3-l4），凡是落到默认分支的指令（非向量、非 mem、非 csr、非 fp、非 mul、非 sfu、非 tc、非 barrier）都走 sALU。

它和 `valu` 有两点本质不同：

1. **单数据 vs 一排数据**：`aluexe` 的 `in1/in2/in3` 各只有 `XLEN`（32）位，结果 `wb_wxd_rd_o` 也是 32 位标量，写回标量寄存器堆（SGPR/x 寄存器），写回标志是 `ctrl_wxd_i`。
2. **多了分支输出**：除写回通路外，还有 `br_wid_o/br_jump_o/br_new_pc_o` 送给 `branch_back`，用于标量跳转（如 `JAL/JALR/SETRPC`、标量条件分支）。

#### 4.4.2 核心流程

```text
in1/in2 → alu（单个）→ alu_out / alu_cmp
                          │
        ┌─────────────────┴──────────────────┐
        ▼                                     ▼
  写回通路 U_result（深度1）            分支通路 U_result_br（深度2）
  打包 {wid, alu_out, reg_idxw, wxd}    分支判定：
  有效：in_valid & ctrl_wxd             case(ctrl_branch_i)
                                            B_B → jump = alu_cmp    （条件分支）
                                            B_J → jump = 1          （无条件跳转）
                                            B_R → jump = 1          （寄存器跳转）
                                            B_N → jump = 0          （不跳）
                                        打包 {wid, in3(new_pc), jump}
                                        有效：in_valid & (branch != B_N)
```

#### 4.4.3 源码精读

**例化单个 alu**：[aluexe.v:70-79](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/aluexe.v#L70-L79)，输入 `op_i = ctrl_alu_fn_i[4:0]`、`in1_i`、`in2_i`，与 valu 里的 lane 完全一样的内核，只是「只有一份」。

**分支判定**（[aluexe.v:109-116](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/aluexe.v#L109-L116)）依据 [define.v:421-424](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L421-L424) 的分支类型宏：`B_N=00`（不分支）、`B_B=01`（条件分支，跳否取决于 `alu_cmp`）、`B_J=10`（无条件跳转）、`B_R=11`（寄存器间接跳转）。`B_B` 时跳转条件正是 alu 的比较结果 `alu_cmp`。

**双 FIFO 与打包**：写回 FIFO `U_result`（[aluexe.v:81-93](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/aluexe.v#L81-L93)，深度 1）打包 `{ctrl_wid, alu_out, ctrl_reg_idxw, ctrl_wxd}`（[aluexe.v:118](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/aluexe.v#L118)）；分支 FIFO `U_result_br`（[aluexe.v:95-107](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/aluexe.v#L95-L107)，**深度 2**）打包 `{ctrl_wid, in3_i(新PC), jump_temp}`（[aluexe.v:122](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/aluexe.v#L122)）。分支 FIFO 深度 2 是为了容纳在途的多个分支，避免分支背压卡住流水。

**反压选择**（[aluexe.v:137-138](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/aluexe.v#L137-L138)）：`in_ready_o` 按分支类型决定——纯条件分支 `B_B` 只看分支 FIFO 就绪；不分支 `B_N` 只看写回 FIFO 就绪；跳转类 `B_J/B_R` 两个都要就绪（因为既要写回 PC 又要送分支目标）。

**在 pipe.v 中的接入**：`aluexe` 例化为 `salu`（[pipe.v:1792-1819](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1792-L1819)），上游接 `issue_out_sALU_*`；写回结果 `salu_out_*` 进入标量写回总线 [pipe.v:880-884](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L880-L884) 的最高位（6 路标量写回：`{mul, sfu, csr, lsu, fpu, salu}`）；分支结果 `salu_out2br_*` 送给 `branch_back`（[pipe.v:2068-2072](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L2068-L2072)），由后者更新 PC 并在需要时 flush 流水线。

#### 4.4.4 代码实践：标量条件分支的跳转来源

1. **实践目标**：搞清一条标量条件分支（`B_B`）的跳转判定与目标来源。
2. **操作步骤**：
   - 在 [aluexe.v:110-111](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/aluexe.v#L110-L111) 确认 `B_B` 时 `jump_temp = alu_cmp`。
   - 在 [aluexe.v:122](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/aluexe.v#L122) 确认分支目标 `new_pc = in3_i`（由译码/采集阶段算好的目标地址，作为第三操作数送入）。
2. **需要观察的现象**：条件分支同时产生「是否跳（jump）」与「跳到哪（new_pc）」两个量，前者来自 alu 比较、后者来自 in3。
3. **预期结果**：当 `alu_cmp=1` 时 `br_jump_o=1`，`branch_back` 据此把 PC 更新为 `br_new_pc_o` 并触发 flush。
4. 待本地验证：沿 `salu_out2br_jump` → `branch_back` 路径在波形中确认一次跳转发生。

#### 4.4.5 小练习与答案

**练习 1**：`aluexe` 的 `in_ready_o` 为什么对 `B_J/B_R` 要求「写回 FIFO 与分支 FIFO 都就绪」？
**参考答案**：无条件跳转与寄存器跳转既要写回返回地址（写到标量寄存器堆），又要向 `branch_back` 递交跳转目标，二者必须同时成功才能「接收」这条指令，否则任一通路丢失都会出错（[aluexe.v:137-138](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/aluexe.v#L137-L138)）。而 `B_B`（条件分支）默认不写回、只送分支结果，故只看分支 FIFO；`B_N`（不分支）只写回、无分支结果，故只看写回 FIFO。

**练习 2**：为什么 `aluexe` 只例化一个 `alu`，而 `valu` 要例化 NUM_THREAD 个？
**参考答案**：标量指令一次只处理一个 32 位数据，一个 `alu` 即可；向量指令要在一个 warp 内同时处理 NUM_THREAD 个线程的数据，故必须复制 NUM_THREAD 份 `alu`（lane 阵列）才能一拍完成。

---

## 5. 综合实践

把本讲四个模块串起来，做一个「VADD_VV 全通路追踪 + VADD_VX 对比」的综合任务。

**任务背景**：向量加法有两种形式——`VADD_VV`（两个向量寄存器对应元素相加）与 `VADD_VX`（一个向量寄存器加上一个标量寄存器，标量广播到所有 lane）。二者最终都走 vALU，但在**操作数采集阶段**的处理不同。

**操作步骤**：

1. **画出通路图**：以本讲源码为依据，绘制从 `issue` → `valu_top` → `valu`（lane 阵列）→ `alu`（单 lane）→ `stream_fifo` → `writeback` 的完整框图，标出：
   - 数据位宽在何处从 `NUM_THREAD*XLEN` 收敛到单 lane 的 `XLEN`（答：在 lane 阵列的位切片 `in1_i[(i+1)*XLEN-1-:XLEN]`）。
   - 结果在何处从单 lane 的 `XLEN` 重新拼回 `NUM_THREAD*XLEN`（答：`wb_wvd_rd[(i+1)*XLEN-1-:XLEN]`）。
   - 功能码 6→5 位的截断发生在何处（答：`valu` 把 `ctrl_alu_fn_i[4:0]` 赋给 `alu_op`）。
2. **追踪 VADD_VV**（向量-向量）：在 [valu.v:161-168](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu.v#L161-L168) 默认分支确认 lane `i` 执行 `in1[i] + in2[i]`。此时 `in1_i` 与 `in2_i` 的各 lane 段是不同的向量元素。
3. **追踪 VADD_VX**（标量-向量）：回到 u4-l1 的操作数采集器——标量操作数在采集阶段被**广播复制成 NUM_THREAD 份**，于是到达 vALU 时 `in2_i`（标量那一源）的各 lane 段完全相同。对 vALU 而言两种指令的执行代码**没有任何区别**，差异完全由上游 operand_collector 消化。
4. **回答关键问题**：为什么 vALU 不需要区分 VV/VX/VI（标量-向量/立即数）？
   - **参考答案**：因为 operand_collector 已经把标量、立即数都「拉齐」成 NUM_THREAD 路宽的操作数排（标量广播、立即数由 `gen_imm` 生成并广播），送到 vALU 时三种形式在数据排布上不可区分，vALU 只需逐 lane 做同一种运算即可。这种「把异构性上推到采集器、让执行单元纯并行」的设计，使 vALU 极其简洁。
5. **可选验证**：若本地有仿真环境，在 `tc_vecadd` 用例下用 Verdi 观察 `issue_out_vALU_vExeData_in1/in2` 与 `valu.wb_wvd_rd` 的对应关系，确认每个 lane 的加法结果。

## 6. 本讲小结

- **SIMT lane 并行的硬件写照**：`valu` 用 `generate for` 把同一个 `alu` 内核复制 NUM_THREAD 份，控制信号（功能码/wid/写回寄存器号）全广播、数据各 lane 独立，一拍完成一个 warp 的运算。
- **三层结构各司其职**：`valu_top` 是外壳（折叠/非折叠切换，默认非折叠）；`valu` 是 lane 阵列与伪指令特例处理；`alu` 是纯组合的单 lane 运算核，被向量与标量两条通路复用。
- **功能码靠截位对齐**：define.v 的 6 位 `FN_*` 取低 5 位喂给 `alu` 的 5 位 `op_i`，二者数值须一致；`alu.v` 内用 `localparam` 重新声明同样的数值。
- **资源共享是 alu 的设计精髓**：加减共享加法器、比较复用加法器符号位、左右移靠位反转复用一个桶形移位器、掩码归约伪指令映射成基本 AND/OR/XOR 再取反。
- **vALU vs sALU 的本质差异**：vALU（`valu`）例化 lane 阵列、只做向量写回并附带 SIMT 掩码输出；sALU（`aluexe`）例化单个 `alu`、做标量写回并额外承担标量分支（`B_B/B_J/B_R`），分支结果送 `branch_back`。
- **异构性上推到采集器**：VV/VX/VI 的区别在 operand_collector 处理（标量广播、立即数生成），到 vALU 时已无差别，使执行单元保持极简。

## 7. 下一步学习建议

- **u4-l3（乘法器 vmul 与 array_multiplier）**：本讲提到 `alu` 里的 `FN_MUL/MULH/...` 只是占位、未实现，真正的乘法在 `vmul` 通路用阵列乘法器完成。下一讲将讲清 32×32 有符号/无符号乘法与高低位选取。
- **u4-l4（浮点 FPU 与 vFPU）**：与 vALU 结构高度对称——vFPU 也是 lane 阶列、复用 `scalar_fpu` 内核，可对照本讲理解「向量执行单元」的通用范式。
- **u5-l3（SIMT 栈与分支发散）**：本讲 `valu` 的 `if_mask = ~alu_cmp` 输出会送到 `simt_stack`，那里讲清 warp 内不同线程走不同分支时的发散与汇合机制，是把本讲的「掩码」用起来的关键。
- 若想加深对路由的理解，可重读 u3-l4 的 `issue.v` 优先级链，确认「向量整数指令落 vALU、标量整数与分支落 sALU」的判定条件（`isvec` 与默认分支）。
