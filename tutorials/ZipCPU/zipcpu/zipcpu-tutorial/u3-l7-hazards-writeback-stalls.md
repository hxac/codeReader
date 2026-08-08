# 流水线冒险与停顿

## 1. 本讲目标

本讲承接 [u3-l1 zipcore 总体结构与流水线阶段](u3-l1-zipcore-structure-pipeline.md)。在上一讲里，我们画出了 zipcore 的五级流水线骨架；本讲要回答的核心问题是：**当一条指令「抢不到」它需要的数据、或「猜错」了下一步该执行哪条指令时，CPU 怎么保证结果仍然正确？**

学完后你应该能够：

1. 识别流水线的三类冒险（数据冒险、控制冒险、结构冒险），并说出各自在 ZipCPU 里的典型场景。
2. 读懂 zipcore.v 中 `master_stall` / `op_stall` / `dcd_*_stall` 等停顿信号的「产生条件」，理解停顿（stall）与清空（flush/clear_pipeline）的触发时机。
3. 理解写回阶段为何「禁止停顿」，以及它如何用四入口仲裁（`wr_index`）保证寄存器堆被正确更新。

## 2. 前置知识

在阅读本讲前，你需要先掌握以下概念（均在 u3-l1、u3-l4、u3-l6 中建立）：

- **流水线与级（stage）**：ZipCPU 把一条指令的处理切成五级——取指（prefetch，前缀 `pf_`）、译码（decode，前缀 `dcd_`）、读操作数（read operands，前缀 `op_`）、执行/访存（ALU/MEM/DIV/FPU 四条并行轨道，前缀 `alu_`/`mem_`）、写回（writeback，前缀 `wr_`）。
- **时钟使能与反压**：每一级都有两个控制位——`*_ce`（clock enable，时钟使能，决定该级这一拍是否前移）和 `*_stall`（反压，决定该级是否需要原地等待）。所有级的 `*_ce` 最终汇成全局 `master_ce`，所有 `*_stall` 汇成 `master_stall`。
- **load/store 架构**：只有访存指令访问内存；ALU 指令一拍出结果，但 load 需要等总线应答，乘除法需要多个时钟。

三个本讲要用到的新术语：

| 术语 | 含义 |
|------|------|
| 冒险（hazard） | 流水线中后一条指令依赖前一条尚未就绪的结果，或两条指令争用同一个硬件资源，从而必须插入等待的现象 |
| 停顿（stall / bubble） | 让某一级「原地不动一拍或几拍」，插入一个不做事的空泡（bubble）来等待数据/资源就绪 |
| 清空（flush / clear_pipeline） | 把流水线后面几级的有效位直接作废、重新从新地址取指，用于分支跳转等控制冒险 |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [rtl/core/zipcore.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v) | CPU 内核。本讲的停顿/清空/写回逻辑几乎全部位于此文件 |
| [doc/src/spec.tex](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex) | ISA 规范。其中的 *Pipeline Operation* 与 *Pipeline Stalls* 子节用文字+时序图解释了为什么要停顿 |

> 说明：spec.tex 的 *Pipeline Operation* 一节（含 *Pipeline Stalls* 子节）在当前源码里被包在一段 `\iffalse … \fi` 注释里，并未编进正式 PDF，但 LaTeX 源文本身完整、可读，是理解停顿语义的最佳文档。

---

## 4. 核心概念与源码讲解

### 4.1 流水线冒险全景与停顿总控

#### 4.1.1 概念说明

「冒险」是流水线 CPU 必然要面对的问题。当多条指令同时在流水线里流动时，它们之间可能产生三种冲突：

1. **数据冒险（data hazard）**：后一条指令需要读的寄存器，正是前一条指令还没写回的「新值」。最常见的是 **RAW**（Read After Write，写后读）——「我刚算完还没存回去，你就来读了」。
2. **控制冒险（control hazard）**：遇到分支、跳转、中断时，CPU 在取指阶段并不知道「下一条该取哪」，只能先把顺序后面的指令取进来；一旦发现跳转真的发生，这些「取错」的指令必须作废。
3. **结构冒险（structural hazard）**：两条指令要在同一拍争用同一个硬件资源。在 ZipCPU 里这主要表现为：ALU 一拍出结果，但 **乘法、除法、访存** 都是多周期单元，它们运行时会让整条流水线等待。

spec.tex 用一句话点明了 ZipCPU 处理冒险的总原则——**不支持乱序执行**：

> The ZipCPU does not support out of order execution. Therefore, if the memory unit stalls, every other instruction stalls.
> （ZipCPU 不支持乱序执行。因此，一旦访存单元停顿，其它所有指令都得跟着停顿。）——见 [doc/src/spec.tex:1609-1613](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1609-L1613)

这句话是本讲的「宪法」：因为不乱序，所以一旦下游某一级卡住，上游只能整体停顿（stall），不能绕过去先做别的。

#### 4.1.2 核心流程

ZipCPU 用一套「两级握手 + 一个总闸」来统一管理停顿：

```text
                    master_ce  ──┐  (全局时钟使能：决定整条流水线是否前移)
                                 │
   各级 *_ce  =  master_ce  &&  !本级的停顿条件
                                 │
   master_stall ─────────────────┘  (全局反压：任一停顿条件命中都拉高)

  取指(dcd_stalled) → 译码/读操作数(op_stall) → 执行(alu_stall) → 访存(mem_stalled)
        ↑                  ↑                        ↑                 ↑
        └──────────── 任何一级 *_stall 拉高 → master_stall 拉高 → master_ce 拉低 ────┘
```

- `master_ce`（主时钟使能）为 0 时，整条流水线原地冻结；
- `master_stall`（主反压）把所有「需要停顿」的原因 OR 在一起；
- 两者互为表里：`master_stall` 命中就会让 `master_ce` 失效。

#### 4.1.3 源码精读

**全局时钟使能 `master_ce`** 只在四种「整机不前进」的情况下为 0：被暂停（`i_halt`）、CC 写保持（`cc_write_hold`）、命中断点（`o_break`）、休眠（`sleep`）。

[rtl/core/zipcore.v:373-374](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L373-L374) —— 全局时钟使能，是整条流水线能否前移的总闸。

```verilog
assign master_ce = (!i_halt || alu_phase)
            &&(!cc_write_hold)&&(!o_break)&&(!sleep);
```

**全局反压 `master_stall`** 才是冒险检测的真正汇聚点。它把数据冒险、结构冒险、控制冒险的触发条件全部「或」在一起：

[rtl/core/zipcore.v:596-605](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L596-L605) —— master_stall：任一停顿原因命中即拉高，对应 spec「不乱序、一停全停」。

```verilog
assign master_stall = (!master_ce)||(!op_valid)||(ill_err_i)
        ||(step && stepped)
        ||(ibus_err_flag)||(idiv_err_flag)
        ||(pending_interrupt && !o_bus_lock)&&(!alu_phase)
        ||(alu_busy)||(div_busy)||(fpu_busy)||(op_break)
        ||((OPT_PIPELINED)&&(
            prelock_stall
            ||((i_mem_busy)&&(op_illegal))
            ||((i_mem_busy)&&(op_valid_div))
            ||(alu_illegal)||(o_break)));
```

读懂这一段就能把冒险分类对上号：

| `master_stall` 中的条件 | 对应的冒险类型 | 含义 |
|---|---|---|
| `(!op_valid)` | —— | 流水线还没注入有效指令，空泡自然要停 |
| `step && stepped` | 控制冒险 | 调试单步模式，走一步就停 |
| `pending_interrupt` | 控制冒险 | 有挂起中断，准备切组，先停住 |
| `alu_busy`/`div_busy`/`fpu_busy` | **结构冒险** | 多周期单元（乘除/浮点）在跑，全流水线等待 |
| `i_mem_busy` 相关项 | **结构冒险** | 访存单元忙碌，全流水线等待 |
| `ill_err_i`/`ibus_err_flag`/`idiv_err_flag` | 异常 | 非法指令/总线错/除零，进入异常处理前先停 |

> 注意：上面这段全是 `OPT_PIPELINED` 的内容。若把 `OPT_PIPELINED` 设为 0（单周期、无流水线模式），则「流水线」退化成一条指令走完全程，**根本不存在冒险**——下文所有 `dcd_*_stall` 在该模式下都被直接赋成 `1'b0`（见 [rtl/core/zipcore.v:1404-1408](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1404-L1408) 的注释 *"There are no pipeline hazards, if we aren't pipelined"*）。这是理解「冒险 = 流水线的副产品」的最直接证据。

#### 4.1.4 代码实践

**目标**：在源码层面确认「冒险只在流水线模式下存在」。

**步骤**：
1. 在 zipcore.v 中分别搜索 `NO_OP_STALLS`、`NO_DCDA_STALL`、`NO_MEM_STALL`、`NO_PIPELINE_NO_STALL` 这几个 generate 分支名。
2. 观察它们对应的 `else` 分支（即 `OPT_PIPELINED == 0` 时）把每个 stall 信号都赋成了什么。
3. 再搜索 `GEN_OP_STALL`、`GEN_DCDA_STALL`、`GEN_MEM_STALL`，对照流水线模式下的赋值。

**预期结果**：你会看到非流水线分支里 `op_stall = 1'b0`、`dcd_A_stall = 1'b0`、`dcd_B_stall = 1'b0`、`dcd_F_stall = 1'b0`，即「没有流水线就没有冒险」。

**现象解释**：冒险是「多条指令同时在流水线里」才产生的；单周期模式同一时刻只有一条指令，前一条写完才轮到下一条读，天然不存在写后读。

#### 4.1.5 小练习与答案

**练习 1**：`master_stall` 里没有直接出现「前一条指令写 R3、后一条读 R3」这样的字样，那数据冒险是被谁检测的？
**答案**：不是 `master_stall` 直接检测，而是由读操作数级的 `op_stall`（聚合了 `dcd_A_stall`/`dcd_B_stall`/`dcd_F_stall`）检测，再通过 `!adf_ce_unconditional` 反向汇入 `op_stall`，最终影响流水线前移。

**练习 2**：为什么 `master_stall` 里把 `alu_busy || div_busy || fpu_busy` 列为停顿条件，却没有把「ALU 一拍运算」列为停顿？
**答案**：ALU 是单周期组合逻辑，一拍出结果，不占用下一拍，不会成为瓶颈；而乘除/浮点是**多周期**单元，运行期间结果未就绪，后续指令必须等待，属于结构冒险。

---

### 4.2 数据冒险：读操作数级的停顿与转发

#### 4.2.1 概念说明

数据冒险中最常见的是 **RAW（写后读）**：指令 1 要写寄存器 R，指令 2 紧跟着读 R，但指令 1 的结果要等到第 5 级（写回）才真正落进寄存器堆，而指令 2 在第 3 级（读操作数）就要用 R 的值。

经典 RISC CPU 有两种解法：

1. **数据转发（forwarding / bypass）**：与其等结果写回寄存器堆再读，不如直接把「第 5 级马上要写进去的值」用组合逻辑抄送给第 3 级的输入。这样不用停顿。
2. **停顿（stall）**：当转发也救不了（比如 load 的数据要等总线应答、或要在读操作数级再做一个加法）时，就让后一条指令原地等一两拍。

ZipCPU **两种都用**：能转发的就转发，转发来不及的就停顿。

#### 4.2.2 核心流程

读操作数级（第 3 级，`op_` 前缀）是数据冒险的主战场。它的停顿条件 `op_stall` 由两块组成：

```text
op_stall 命中 (=1) 的两种情况：

(A) 当前 op 级指令有效，但下游执行级还没空接收
      → (!adf_ce_unconditional) && (!mem_ce)

(B) 译码级(dcd)有新指令要进来，但它要读的寄存器还没就绪
      → dcd_A_stall  (操作数 A 未就绪：通常是 CC 寄存器)
      → dcd_B_stall  (操作数 B 未就绪：典型 RAW + 立即数偏移)
      → dcd_F_stall  (要读标志位，但标志位还在算)
```

其中 `dcd_B_stall` 是最典型的 RAW 停顿，它精确对应 spec 里列举的场景（见 4.2.3）。而**多数普通 RAW** 则由转发通路 `op_Av`/`op_Bv` 免费解决，根本不进入停顿。

#### 4.2.3 源码精读

先看 **读操作数级的总停顿 `op_stall`**：

[rtl/core/zipcore.v:449-472](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L449-L472) —— op_stall：检测「下游没空」和「上游要读的寄存器未就绪」两类数据冒险。

```verilog
assign op_stall = (op_valid)&&(
    (!adf_ce_unconditional)&&(!mem_ce)     // 下游执行级没空接收
    )
    ||(dcd_valid)&&(                        // 译码级有新指令，但它要等
        i_halt
        || (dcd_A_stall)
        ||(dcd_B_stall)
        ||(dcd_F_stall)
    );
```

注释里写得清楚：`dcd_A_stall`/`dcd_B_stall`/`dcd_F_stall` 分别表示「要读的操作数 A / B / 标志位」是否需要等待。

再看 **转发通路**，它解释了「为什么多数 RAW 不需要停顿」。以操作数 A 为例：

[rtl/core/zipcore.v:1378-1379](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1378-L1379) —— op_Av 转发：若本拍写回的正是我要读的寄存器，直接抄写回值，免停顿。

```verilog
assign op_Av = ((wr_reg_ce)&&(wr_reg_id == op_Aid))
    ?  wr_gpreg_vl : r_op_Av;
```

意思是：「如果这一拍（写回级）正好要写寄存器 `op_Aid`，而我又正好要读它，那我别去读旧的寄存器堆值 `r_op_Av` 了，直接拿马上要写进去的新值 `wr_gpreg_vl`」。操作数 B 也有完全对称的转发（[rtl/core/zipcore.v:1414-1416](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1414-L1416)）。

那么 **什么时候必须停顿**？看 `dcd_B_stall`：

[rtl/core/zipcore.v:1430-1472](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1430-L1472) —— dcd_B_stall：读操作数 B 的停顿条件，最典型的是「前一条写了 B，而本条又要给 B 加立即数偏移」。

```verilog
assign dcd_B_stall = (dcd_rB)
        &&((op_valid)||(i_mem_rdbusy)||(div_busy)||(fpu_busy)||(alu_busy))
        &&(
        ((!dcd_zI)&&(                       // 本条指令带非零立即数偏移
            ((op_R == dcd_B)&&(op_wR))      // 前一条正好要写 B
            ||((i_mem_rdbusy)&&(!dcd_pipe))
            ||(((alu_busy || div_busy || i_mem_rdbusy))&&(alu_reg == dcd_B))
            ...))
        ||(((op_wF)||(cc_invalid_for_dcd))&&(dcd_Bcc))
        );
```

关键是 `(!dcd_zI) && ((op_R == dcd_B)&&(op_wR))`：**「本条指令带立即数偏移，且前一条指令要写回我正在读的寄存器 B」**。为什么这种情况下转发救不了？因为带偏移的寻址（如 `4(SP)`）需要在第 3 级把「寄存器值 + 立即数」加出来，而转发过来的新值要等到这一拍末尾才可用，来不及在同拍内完成加法并送进 ALU。所以必须**插一个停顿周期**，等加法稳定。

这一段源码与 spec 文字描述**一一对应**：

> When reading from a prior register while also adding an immediate offset（读前一条指令写过的寄存器，同时还要加一个立即数偏移时）——见 [doc/src/spec.tex:1658-1685](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1658-L1685)：

```text
1.  OPCODE ?,RA          ; 例如 SUB R1,R2   (写 R2)
2.  (stall)              ; 必须插一个停顿
3.  OPCODE I+RA,RB       ; 例如 ADD 3+R2,R3 (读 R2 且带立即数 3)
```

spec 还贴心地给出**调度优化建议**：把一条「不带偏移、也不读该寄存器」的指令塞进这个 stall 槽位，就能白捡这一拍（[doc/src/spec.tex:1669-1672](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1669-L1672)）。这正是编译器做指令调度的依据。

> 补充：另一种必须停顿的 RAW 是 **load-use**（load 后立即用），spec 用时序图 `fig:memrd` 说明 load 在等总线应答期间，后续指令必须停在译码级（[doc/src/spec.tex:1712-1749](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1712-L1749)）。store 则不同——它不回写寄存器，可以和后续非访存指令重叠，所以「只有 load 才停顿流水线」。

#### 4.2.4 代码实践

**目标**：亲手定位一个真实 RAW 停顿的产生条件，并给出能触发它的指令序列。

**步骤**：
1. 打开 [rtl/core/zipcore.v:1430-1472](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1430-L1472)，确认 `dcd_B_stall` 的核心子条件 `(!dcd_zI) && (op_R == dcd_B) && (op_wR)`。
2. 再读 [doc/src/spec.tex:1658-1685](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1658-L1685) 的文字说明，确认它讲的是同一件事。
3. 写出两条指令的序列（伪汇编即可），说明哪一条是「写 R2」、哪一条是「读 R2 + 立即数偏移」、停顿插在哪两条之间。

**参考序列**（这两条会产生 RAW 停顿）：

```asm
SUB  R1,R2          ; 指令1：R2 ← R2 - R1，写回 R2（op_wR=1, op_R=R2）
                    ; (stall)   ← dcd_B_stall=1 插入一个空泡
ADD  3+R2,R3        ; 指令2：读 R2 且要加立即数偏移 3（dcd_zI=0, dcd_B=R2）
```

**需要观察的现象**：在 Verilator 仿真中（见 4.2.5），这两条指令之间会多耗一个时钟周期；如果把第二条改成 `ADD R2,R3`（不带立即数偏移，即 `dcd_zI=1`），则停顿消失——因为这时转发通路 `op_Bv`（[rtl/core/zipcore.v:1414-1416](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1414-L1416)）能直接把新值抄过来，无需等待加法稳定。

**预期结果**：CPU 用「插一个停顿周期」解决了这个 RAW：等 `SUB` 的新 R2 值就绪并完成「+3」加法后，`ADD` 才进入 ALU，结果正确。

**待本地验证**：上述「多耗一个周期」的结论，可在 `sim/verilator` 下用 `make stest`（单步测试台）逐拍观察 `o_op_stall` 输出确认（见 [rtl/core/zipcore.v:3592](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L3592) 的 `o_op_stall` 统计信号）。

#### 4.2.5 小练习与答案

**练习 1**：把上面的 `ADD 3+R2,R3` 换成 `ADD R2,R3`（去掉立即数偏移），停顿还会发生吗？为什么？
**答案**：不会。因为 `dcd_zI` 变成 1（立即数为零），`dcd_B_stall` 中的 `(!dcd_zI)` 条件不成立；此时由转发通路 `op_Bv` 直接把 `SUB` 写回的新 R2 值抄送给 `ADD`，无需停顿。

**练习 2**：为什么 `dcd_A_stall` 的条件（[rtl/core/zipcore.v:1399-1403](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1399-L1403)）几乎只跟 `dcd_Acc`（要读 CC 寄存器）有关，而不像 `dcd_B_stall` 那样涉及普通寄存器？
**答案**：因为操作数 A 恒等于目的寄存器（见 u3-l3），其值由 `op_Av` 转发通路兜底；只有当 A 是 CC（条件码）寄存器时，标志位是在写回级用组合逻辑算出来的（见 spec [doc/src/spec.tex:1687-1710](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1687-L1710)），同拍内来不及反馈，所以才需要停顿。

---

### 4.3 控制冒险与流水线清空（flush）

#### 4.3.1 概念说明

控制冒险发生在「下一条指令地址不确定」时：分支（BRA/BC/BNZ 等）、跳转、中断、异常、RTU（从中断返回）。在取指阶段，CPU 还不知道分支会不会跳，只能先按顺序取；一旦在执行级判定「真的要跳」，之前按顺序取进来的若干条指令就是「取错」的，必须作废。

ZipCPU 处理控制冒险的方式是 **清空流水线（clear_pipeline）**：把译码级及之后的「有效位」全部清零，让取指从新地址重新开始。这等价于往流水线里塞几个空泡（bubble），代价是若干个停顿周期。

#### 4.3.2 核心流程

```text
触发清空的事件（任一发生 → new_pc = 1 → clear_pipeline = 1）：

  reset / clear_icache / 调试改写寄存器(dbg_clear_pipe)
  中断进入(switch_to_interrupt) / 中断返回(release_from_interrupt)
  写回阶段改写了 PC（任何跳转/分支最终都体现为写 PC）

        ┌─────────────────────────────────────────┐
        │  new_pc = 1  →  clear_pipeline = 1       │
        │  → 取指从新地址重启；dcd/op 级 valid 清零  │
        │  → 代价：约 4 个停顿周期（5 级流水 − 1）    │
        └─────────────────────────────────────────┘

  开启 OPT_EARLY_BRANCHING 时，部分无条件分支在译码级提前判定
        → 代价降为 1 个停顿周期
```

#### 4.3.3 源码精读

`clear_pipeline` 本质上就是「需要换 PC」的信号：

[rtl/core/zipcore.v:212](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L212) —— clear_pipeline 直接等于 new_pc：凡是要换 PC 就清空流水线。

```verilog
assign clear_pipeline = new_pc;
```

`new_pc` 的产生条件覆盖了所有控制冒险来源：

[rtl/core/zipcore.v:3310-3326](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L3310-L3326) —— new_pc：复位、清缓存、调试改写、中断进入/返回、以及「写回阶段改写了 PC」都会要求清空。

```verilog
always @(posedge i_clk)
if ((i_reset)||(o_clear_icache)||(dbg_clear_pipe))
    new_pc <= 1'b1;
else if (w_switch_to_interrupt)
    new_pc <= 1'b1;
else if (w_release_from_interrupt)
    new_pc <= 1'b1;
else if ((wr_reg_ce)&&(alu_gie == wr_reg_id[4])&&(wr_write_pc))
    new_pc <= 1'b1;            // 任何写 PC（分支/跳转最终都写 PC）→ 清空
else
    new_pc <= 1'b0;
```

最后一行 `(wr_reg_ce)&&(wr_write_pc)` 最关键：**所有分支和跳转最终都体现为「写回阶段改写 PC」**，一旦 PC 被改写，`new_pc` 拉高，流水线清空，从新 PC 重新取指。

**代价有多大？** spec 给出量化结论：一次普通条件分支要清掉约 4 个周期（5 级流水线 − 1 = 4 个空泡）：

> While waiting for the pipeline to load following any taken branch, jump, return from interrupt or switch to interrupt context (4 stall cycles, minimum)——见 [doc/src/spec.tex:1629-1642](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1629-L1642)。

**能否少停一些？** 可以，靠综合期参数 `OPT_EARLY_BRANCHING`（[rtl/core/zipcore.v:47](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L47)）。开启后，译码级会把 `ADD $X,PC`、`LW (PC),PC` 这类分支**提前一拍**识别并生成（即 `dcd_early_branch`），不在 flags 上条件的分支只要 1 个停顿周期，`LW (PC),PC` 要 2 个：

> When the OPT_EARLY_BRANCHING parameter is enabled ... can execute with only a single stall cycle (two for the LW (PC),PC instruction)——见 [doc/src/spec.tex:1644-1652](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1644-L1652)。

#### 4.3.4 代码实践

**目标**：理解「分支 = 写 PC = 清空」这条链路，并算出一次跳转的代价。

**步骤**：
1. 在 zipcore.v 中搜索 `dcd_early_branch`，看它如何被 `OPT_EARLY_BRANCHING` 包裹。
2. 对照 [doc/src/spec.tex:1629-1652](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1629-L1652)，确认「普通分支 4 周期、提前分支 1 周期」。
3. 思考：为什么清空代价是「级数 − 1」个周期？

**预期结果**：5 级流水线里，分支结果在第 4 级（执行）才确定，此时取指级已经多取了约 4 条「错误」指令；清空作废它们再重新取，正好损失 4 拍。`OPT_EARLY_BRANCHING` 把判定提前到第 2 级（译码），损失降到 1 拍。

**待本地验证**：在 `sim/verilator` 下分别用默认配置与 `OPT_EARLY_BRANCHING=0` 重新编译 zipcore（需改 rtl/Makefile 的参数后 `make rtl`），跑同一段含跳转的测试程序，比较 `o_op_stall`/`o_pf_stall` 计数差异。

#### 4.3.5 小练习与答案

**练习 1**：为什么「写回阶段改写 PC」就要清空整条流水线，而不是只清空取指级？
**答案**：因为分支结果到写回级才确定，此时译码、读操作数、执行各级里都已经装着「按顺序取错的」指令。这些指令若不清空就会被执行，导致错误；所以必须把 `new_pc` 之后各级的有效位全部清零。

**练习 2**：中断进入（`switch_to_interrupt`）和普通分支跳转，都会把 `new_pc` 置 1，它们的清空代价一样吗？
**答案**：从「清空流水线」这个动作看是一样的，都损失约 4 个周期。但中断还额外伴随「切寄存器组」（见 u2-l5），硬件开销主要在切组而非访存；而分支跳转的代价纯粹是「重取指令」。

---

### 4.4 写回阶段：四入口仲裁与提交逻辑

#### 4.4.1 概念说明

写回是流水线最后一级，负责把结果真正写进寄存器堆。ZipCPU 的写回有个非常严格的设计约束——**「禁止停顿」**。原因是：写回是「承诺」——上一级算出结果时，已经按「下一拍必能写回」的前提排好了时序；如果写回还能反压，整条流水线的握手会变得无法收敛。

但 ZipCPU 有四条并行轨道（ALU、访存 load、除法、FPU）都可能产出结果，同一拍里可能不止一个要写回。所以写回级必须解决两个问题：

1. **仲裁（选谁写）**：同一拍多个结果同时 valid，按固定优先级挑一个，其余的靠上游停顿保留到下一拍。
2. **保证正确（写对地方、写对值）**：要把结果写到正确的寄存器号，并且读 CC/PC 这类特殊寄存器时要触发对应的副作用（如改 PC 要清空流水线）。

#### 4.4.2 核心流程

```text
写回级每拍的工作：

  1. 谁要写？  wr_reg_ce = dbgv(调试) || i_mem_valid(load)
              || (alu_wR && alu_valid) || (div_valid && !div_error)
              || (fpu_valid && !fpu_error)        —— 四/五个来源

  2. 选哪一个？ wr_index 用一个 3 位寄存器按优先级编码：
                dbg(000) > mem(001) > alu(010) > div(011) > fpu(1??)

  3. 写到哪？  wr_reg_id = load ? i_mem_wreg : alu_reg
              同时派生 wr_write_cc / wr_write_pc / wr_write_scc / wr_write_ucc

  4. 写什么值？wr_gpreg_vl 按 wr_index 从 dbg_val/i_mem_result/div_result/fpu_result/alu_result 里挑

  5. 真正写入： if (wr_reg_ce) regset[wr_reg_id] <= wr_gpreg_vl;
```

#### 4.4.3 源码精读

先看 **「禁止停顿」的设计声明**：

[rtl/core/zipcore.v:2231-2236](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2231-L2236) —— 写回级注释：结果一旦就绪就必须写回，休眠/调试/暂停都不能阻止。

```text
// This stage is not allowed to stall.  If results are ready to be
// written back, they are written back at all cost.  Sleepy CPU's
// won't prevent write back, nor debug modes, halting the CPU, nor
// anything else.
```

**写使能 `wr_reg_ce`**——把五个来源（调试、load、ALU、除法、FPU）的「我要写」信号合并，同时受 `clear_pipeline` 保护（清空期间不写，避免把作废指令的结果误写入）：

[rtl/core/zipcore.v:2262-2269](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2262-L2269) —— wr_reg_ce：四入口（ALU/MEM/DIV/FPU）外加调试写，任何一个 valid 即触发写回。

```verilog
always @(*)
begin
    wr_reg_ce = dbgv || i_mem_valid;
    if ((alu_wR && alu_valid)
            ||(div_valid && !div_error)
            ||(fpu_valid && !fpu_error))
        wr_reg_ce = wr_reg_ce || !clear_pipeline;
end
```

**仲裁器 `wr_index`**——这是写回级的核心。它是一个 3 位寄存器，按优先级把「谁的结果该被写」编码出来（注释里保留了旧的 case 写法，实际只用三行位运算）：

[rtl/core/zipcore.v:1680-1707](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1680-L1707) —— wr_index：四入口优先级编码，决定本拍写哪个来源的结果。

```verilog
wr_index[0] <= (op_valid_mem | op_valid_div);
wr_index[1] <= (op_valid_alu | op_valid_div);
wr_index[2] <= (op_valid_fpu);
```

解码对照（注释 [rtl/core/zipcore.v:1686-1694](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1686-L1694) 给出）：`3'b000`=调试写、`3'b001`=访存 load、`3'b010`=ALU、`3'b011`=除法、`3'b1??`=FPU。当两条轨道同一拍都想写时，靠 `wr_index` 选一个；落选的那个会让上游（如 `mem_stalled`、`alu_stall`）保持 valid，下一拍再写——**这就是结构冒险的「排队等待」体现**。

**真正写入寄存器堆**：

[rtl/core/zipcore.v:2357-2365](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2357-L2365) —— regset 更新：wr_reg_ce 命中即把 wr_gpreg_vl 写入对应寄存器。

```verilog
always @(posedge i_clk)
if (wr_reg_ce)
    regset[wr_reg_id] <= wr_gpreg_vl;   // OPT_USERMODE 时按 5 位编号
```

**特殊寄存器写**——当写的是 CC 或 PC 时，要触发额外逻辑。CC 写由 `wr_flags_ce` 处理（标志位 Z/N/C/V 来自 ALU/除法/FPU 的运算），PC 写则会让 `new_pc` 拉高从而清空流水线（见 4.3）：

[rtl/core/zipcore.v:2322-2326](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2322-L2326) —— 派生写 CC/PC 标志，驱动后续副作用。

```verilog
assign wr_write_cc  = (wr_reg_id[3:0] == CPU_CC_REG);
assign wr_write_scc = (wr_reg_id[4:0] == {1'b0, CPU_CC_REG});
assign wr_write_ucc = (wr_reg_id[4:0] == {1'b1, CPU_CC_REG});
assign wr_write_pc  = (wr_reg_id[3:0] == CPU_PC_REG);
```

[rtl/core/zipcore.v:2397-2403](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2397-L2403) —— wr_flags_ce：是否更新标志位，由 alu_wF 与各单元 valid 决定。

```verilog
always @(*)
begin
    wr_flags_ce = alu_valid || (div_valid && !div_error)
            || (fpu_valid && !fpu_error);
    if (!alu_wF || clear_pipeline)
        wr_flags_ce = 1'b0;
end
```

把 4.2、4.3、4.4 串起来看，整套机制形成闭环：
- **数据冒险** 多数靠转发（`op_Av`/`op_Bv`）免费解决，少数靠 `dcd_B_stall` 等停顿解决；
- **控制冒险** 靠 `clear_pipeline` 清空重取；
- **结构冒险** 靠 `master_stall`（`alu_busy`/`div_busy`/`mem_busy`）让全流水线等待多周期单元；
- 而无论停顿还是清空，**写回级永远不反压**，结果一旦就绪必由 `wr_index` 仲裁后写回，从而保证寄存器堆最终被正确更新。

#### 4.4.4 代码实践

**目标**：验证「写回级禁止停顿 + 四入口仲裁」的协同。

**步骤**：
1. 读 [rtl/core/zipcore.v:2262-2269](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2262-L2269)，确认 `wr_reg_ce` 不含任何 stall 条件，只受 `clear_pipeline` 限制。
2. 读 [rtl/core/zipcore.v:1680-1707](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1680-L1707)，把 `wr_index` 的三位编码与注释里的 `case` 对照，列出五档优先级。
3. 思考：如果同一拍 ALU 和除法都 valid，`wr_index` 会选谁？落选者怎么保证不丢？

**预期结果**：按编码，`wr_index=3'b011`（div）会让 `[0]` 和 `[1]` 同时为 1，覆盖 alu 的 `3'b010`，即 **除法优先于 ALU**；ALU 结果因 `alu_stall`/`adf_ce_unconditional` 未前移而保留，下一拍再写。

**待本地验证**：可在 `sim/verilator` 下用 `make div_tb`（除法组件测试台）观察除法结果产出与写回时序，确认除法 valid 期间 ALU 通道被压制。

#### 4.4.5 小练习与答案

**练习 1**：写回级「禁止停顿」，那如果写回时正好 `clear_pipeline=1`（正在清空）怎么办？结果会丢吗？
**答案**：不会丢，但也不会误写。`wr_reg_ce` 对 ALU/DIV/FPU 结果额外加了 `!clear_pipeline` 条件（[rtl/core/zipcore.v:2265-2268](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2265-L2268)）：清空期间被作废的指令结果不写回；而属于「有效流」的结果此时不会和 clear 同拍发生。可见「禁止停顿」是对**有效结果**而言的，作废结果本就不该写。

**练习 2**：load 结果（`i_mem_valid`）的写使能没有受 `!clear_pipeline` 保护（见 `wr_reg_ce = dbgv || i_mem_valid;`），为什么？
**答案**：load 是多周期访存，其 `i_mem_valid` 是总线应答回来的「真结果」，对应的是 `clear_pipeline` 发生之前就已发出的访存请求；总线回来的数据必须接住，否则就丢了。所以它无条件写回，不受清空影响——这是「禁止停顿」的又一体现。

---

## 5. 综合实践

把本讲三个机制（停顿、清空、写回仲裁）串起来，做一次「读图 + 写指令」的综合练习。

**任务**：阅读 spec 的 *Pipeline Stalls* 子节（[doc/src/spec.tex:1618-1785](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1618-L1785)），它列出了五类典型停顿。请按下表完成分析（参考答案见后）：

| 场景 | 指令序列 | 冒险类型 | 对应源码信号 | spec 行号 |
|------|----------|----------|--------------|-----------|
| 取指缓存未命中 | —— | （非本讲重点） | `dcd_stalled` / `i_pf_valid` | 1623-1627 |
| 读前一条写的寄存器 + 加立即数 | `SUB R1,R2` / stall / `ADD 3+R2,R3` | 数据 RAW | `dcd_B_stall` | 1658-1685 |
| 设标志后立即读 CC | `CMP R2,R1` / stall / `BZ target` | 数据（标志位）RAW | `dcd_A_stall`/`dcd_F_stall` | 1687-1710 |
| load 后立即用 | `LW (R1),R2` / stall / `ADD R2,R3` | 结构（访存）+ 数据 | `mem_stalled`/`dcd_B_stall` | 1712-1749 |
| store 后紧跟 load | `SW ...` / stall / `LW ...` | 结构（访存单元占用） | `mem_stalled` | 1751-1768 |

**进阶**：任选上表一行，在 zipcore.v 里定位到对应的 stall 信号，画一张「该信号拉高 → master_stall 拉高 → master_ce 拉低 → 流水线冻结一拍」的因果链草图。

**参考答案（第一行示例因果链）**：
`SUB R1,R2` 写 R2，紧接 `ADD 3+R2,R3` 要读 R2 且带立即数偏移 → 译码级算出 `dcd_B=R2`、`dcd_zI=0`，而 op 级 `op_R=R2` 且 `op_wR=1` → `dcd_B_stall=1`（[zipcore.v:1456-1457](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1456-L1457)）→ 汇入 `op_stall=1`（[zipcore.v:468](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L468)）→ `op_ce=0`，译码级不前移 → `ADD` 原地等一拍 → 待 `SUB` 的 R2 新值经写回落定、完成「+3」加法后，`ADD` 才进 ALU。

## 6. 本讲小结

- ZipCPU **不支持乱序执行**，所以一旦访存/乘除/浮点等多周期单元忙碌，整条流水线都得停（结构冒险，由 `master_stall` 汇总）。
- 数据冒险（RAW）多数由 **转发通路** `op_Av`/`op_Bv` 免费解决；只有「读前一条写的寄存器且带立即数偏移」「设标志后立即读 CC」「load-use」等少数情况必须靠 `dcd_B_stall`/`dcd_A_stall`/`dcd_F_stall` **插停顿周期**。
- 控制冒险靠 **清空流水线**（`clear_pipeline = new_pc`）解决，普通分支代价约 4 个停顿周期，开启 `OPT_EARLY_BRANCHING` 后无条件分支可降到 1 个周期。
- 写回级 **禁止停顿**：结果一旦就绪必须写回，由 `wr_index` 在 ALU/load/除法/FPU 四入口间按固定优先级仲裁，落选者靠上游停顿保留到下一拍。
- 停顿只在流水线模式下存在；`OPT_PIPELINED=0` 时所有 `dcd_*_stall` 被直接置零——「没有流水线就没有冒险」。
- spec.tex 的 *Pipeline Stalls* 子节（虽被 `\iffalse` 注释）是理解停顿语义与编译器调度优化的权威依据。

## 7. 下一步学习建议

本讲解完了内核 zipcore 内部的冒险与停顿。接下来建议：

1. **向「外」看总线封装**：进入 [u4-l1 Wishbone 封装 zipwb 与 zipbones](u4-l1-wishbone-wrapper-zipwb.md)，看 zipcore 对外的访存请求如何经 `mem_stalled`/`i_mem_pipe_stalled` 与 Wishbone 总线握手——本讲的 `mem_stalled` 正是总线侧反压的接收端。
2. **向「深」看形式化验证**：阅读 [u5-l2 形式化验证体系（SymbiYosys）](u5-l2-formal-verification.md)，看 bench/formal 如何用属性断言证明「停顿与写回的握手契约永不被违反」。
3. **动手验证**：在 `sim/verilator` 下跑 `make stest`（[sim/verilator/Makefile](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/Makefile)），用单步测试台逐拍观察 `o_op_stall`/`o_pf_stall` 计数，把本讲的理论在真实波形上对上号。
