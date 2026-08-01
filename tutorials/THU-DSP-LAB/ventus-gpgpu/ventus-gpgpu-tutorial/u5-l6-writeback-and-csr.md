# 写回与 CSR

## 1. 本讲目标

本讲是 SM 流水线后端的收尾，回答两个问题：

1. **执行单元算出的结果，怎样有条不紊地写回寄存器堆？** 多个执行单元（ALU、vALU、FPU、LSU、SFU、MUL、TC、CSR）会同时产生结果，而寄存器堆每拍每 bank 只有一个写端口。本讲讲解 `Writeback` 模块如何用「每路一个输出 FIFO + 一个固定优先级仲裁器」把多路结果串行化写回。
2. **一个 warp 启动时，它的运行时上下文（thread id、warp id、共享内存基址、舍入模式……）从哪里来？** 本讲讲解 `CSRexe`/`CSRFile` 如何在 warp 派发瞬间把 CTA 调度器送来的 `wf_tag`、基址等信息写入该 warp 专属的 CSR，以及 `vsetl` 指令如何计算向量长度 `vl`。

学完后你应当能够：

- 画出「执行单元输出 → 输出 FIFO → Writeback 仲裁器 → 寄存器堆」的完整时序，并解释为何要加 FIFO。
- 说明 `Branch_back` 如何把 ALU 分支与 SIMT 分支两路结果合并回传给 `warp_scheduler`。
- 复述 CSR 的地址布局、读写语义（W/S/C），以及 warp 启动时 `threadid`、`wf_tag_dispatch`、`lds_base_dispatch` 三个关键寄存器的写入公式。
- 理解 `CSRexe` 为何要为每个 warp 各开一份 `CSRFile`，并解释 CSR 写指令与 warp 初始化之间的互斥关系。

## 2. 前置知识

阅读本讲前，请确认你已掌握以下概念（它们在前序讲义中已建立）：

- **warp / thread / CSR**：Ventus 以 32 个 thread 组成一个 warp 作为硬件调度单位（u2-l1）。每个 warp 有一组自定义 CSR（地址 `0x800~0x80c`），其中 `CSR_TID`（0x800）= 该 warp 的起始 thread id、`CSR_WID`（0x805）= warp 在 workgroup 内的编号、`CSR_LDS`（0x806）= 共享内存基址。
- **wf_tag 与硬件 wid**：CTA 调度器把一个 workgroup 拆成若干 warp 派发，每个 warp 携带 `wf_tag = {wg_slot_id, wf_id_in_wg}`（u3-l3）。SM 侧的 `CTA2warp` 用位图为新 warp 分配一个**硬件 wid**（0~7），并把 `wf_tag` 记入查找表。后续讲义出现的 `wid` 若无特别说明都指这个硬件 wid。
- **双发射与执行单元总览**：流水线例化两套 Issue（issueX 收标量、issueV 收向量），结果汇到写回模块 `wb` 的 `in_x`、`in_v` 各 6 路（u5-l1）。
- **数据通路 Bundle**：标量结果包 `WriteScalarCtrl`、向量结果包 `WriteVecCtrl`、分支结果包 `BranchCtrl`（u4-l4、u5-l2）。
- **记分板**：指令发射时把目标寄存器标忙，写回时释放（u4-l3）。本讲会看到写回的 `fire` 信号如何反馈给记分板。

> 术语提示：本讲的 `wid` 是 SM 内部的硬件 warp 编号（0~7），与 `wf_tag` 里的 `wf_id_in_wg`（workgroup 内编号）是两个不同概念，二者通过 `CTA2warp` 的查找表相互翻译。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [ventus/src/pipeline/writeback.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/writeback.scala) | 定义 `Writeback`（多路写回仲裁）与 `Branch_back`（分支结果合并） |
| [ventus/src/pipeline/CSR.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala) | 定义 `object CSR`（地址常量）、`CSRFile`（单 warp 的 CSR 文件）、`CSRexe`（per-warp 例化与执行入口） |
| [ventus/src/pipeline/pipe.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala) | SM 流水线总装，把 `wb`、`branch_back`、`csrfile` 与各执行单元、寄存器堆、记分板连起来 |
| [ventus/src/pipeline/operandCollector.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala) | `WriteScalarCtrl` / `WriteVecCtrl` 两个结果 Bundle 的定义，以及写回入寄存器堆的端口 |
| [ventus/src/pipeline/execution.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala) | `BranchCtrl` Bundle 与 `ALUexe` 的分支结果出口 |
| [ventus/src/pipeline/CTA2warp.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CTA2warp.scala) | `CTAreqData` / `warpReqData`：CTA 派发到 SM 的字段定义，是 CSR 初始化的数据来源 |
| [ventus/src/pipeline/LSU.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala) | `LSU2WB`：把 LSU 的访存结果适配成写回格式的中间模块 |

---

## 4. 核心概念与源码讲解

本讲覆盖 4 个最小模块：**Writeback**、**Branch_back**、**CSRFile**、**CSRexe**。

### 4.1 Writeback：多路结果的写回仲裁

#### 4.1.1 概念说明

SM 后端有 8 类执行单元，但它们的延迟各不相同：ALU 约 1 拍、MUL 约 2 拍、LSU 要等访存返回、SFU 的除法甚至几十拍。这些单元会**乱序、随时**地产出结果，而寄存器堆每个 bank 每拍只能接受一个写。如果没有仲裁，两个单元同拍写同一个 bank 就会冲突。

`Writeback` 模块就是结果汇聚的「漏斗」：它把标量结果和向量结果分成两条**相互独立**的数据通路，每条通路里，每个执行单元先接一个**输出级 FIFO** 缓冲突发，再用一个**固定优先级仲裁器（Arbiter）**每拍挑一路写出去。

为什么标量与向量要分两套？因为标量寄存器堆（`x` 寄存器）和向量寄存器堆（`v` 寄存器）是物理上分开的存储体（见 u4-l4），写端口也独立，分开仲裁可以同拍各写一个，提高吞吐。

#### 4.1.2 核心流程

以标量通路为例，向量通路完全对称：

```
alu.out ──┐
fpu.out_x ┐│        每 路 一 个            固定优先级          标 量
lsu2wb.x  ┤├──FIFO──┤ 仲裁器 Arbiter  ──── 写回 ────► 寄存器堆 (operandCollector)
csr.out   ├┘  (深度0)  num_x=6 选 1        out_x
sfu.out_x │
mul.out_x │
          └─  6 路 in_x
```

关键点：

1. **输入 FIFO 解耦时序**：每个执行单元的输出本身已经带了一个 `pipe=true` 的结果队列（如 `ALUexe` 内部的 `Queue(...,1,pipe=true)`，见 u5-l2）。`Writeback` 里又对每路套了一层 `Queue.apply(io.in_x(i), 0)`。`Queue(..., 0)` 表示**深度 0 的寄存器级缓冲**——它不增加存储深度，只把 Decoupled 接口的握手标准化，方便仲裁器统一处理。
2. **固定优先级仲裁**：用的是 Chisel 的 `Arbiter`（注意不是 `RRArbiter`），**编号小的优先级高**。即同拍多路同时 valid 时，`in_x(0)`（ALU）优先获准写回，其余路保留到下一拍。
3. **背压传递**：若下游寄存器堆暂时不能收（`out_x.ready=0`），仲裁结果停留在 FIFO 里，并经 `ready` 反向压回各执行单元，自然形成反压。

#### 4.1.3 源码精读

`Writeback` 的 IO 声明，标量、向量各 6 路输入、各 1 路输出：

[writeback.scala:L44-L50](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/writeback.scala#L44-L50) — 定义 `class Writeback(num_x, num_v)`，`in_x`/`in_v` 是 `Vec` 个 Decoupled 输入，`out_x`/`out_v` 是仲裁后的单路输出。

为每路输入套一个深度 0 的 `Queue`，并例化两个仲裁器：

[writeback.scala:L52-L65](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/writeback.scala#L52-L65) — `fifo_x`/`fifo_v` 用 `for ... yield` 生成各路缓冲；`arbiter_x`/`arbiter_v` 分别是 `new Arbiter(new WriteScalarCtrl, num_x)` 和 `new Arbiter(new WriteVecCtrl, num_v)`，最后把仲裁输出直连 `io.out_x`/`io.out_v`。

在 `pipe.scala` 中能看到这 6+6 路的具体来源：

[pipe.scala:L416-L427](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L416-L427) — 标量 6 路依次是 `alu / fpu.out_x / lsu2wb.out_x / csrfile.out / sfu.out_x / mul.out_x`；向量 6 路是 `valu / fpu.out_v / lsu2wb.out_v / sfu.out_v / mul.out_v / tensorcore.out_v`。注意 LSU 的结果不是直接进 wb，而是先经 `LSU2WB` 适配。

仲裁后的结果送去哪里？送回操作数收集器（寄存器堆的写端口），并同时驱动记分板释放：

[pipe.scala:L290-L291](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L290-L291) — `wb.io.out_v` / `wb.io.out_x` 用 `<>` 直连 `operand_collector` 的 `writeVecCtrl` / `writeScalarCtrl`，完成物理写回。

[pipe.scala:L271-L272](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L271-L272) — 把 `wb.io.out_x.fire` / `wb.io.out_v.fire` 按结果的 `warp_id` 送给对应 warp 的记分板（`scoreb(...).wb_x_fire` / `wb_v_fire`），让 u4-l3 中被标忙的目标寄存器在写回完成的**当拍**释放。这与 u4-l3 讲的「写回到依赖指令可发射有 1 拍间隔」相呼应。

> 写回的 Bundle 结构（[operandCollector.scala:L8-L22](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/operandCollector.scala#L8-L22)）：`WriteScalarCtrl` 含 `wb_wxd_rd`（数据）、`wxd`（是否真写标量）、`reg_idxw`（目标寄存器号）、`warp_id`；`WriteVecCtrl` 把数据换成 `Vec(num_thread, ...)` 并多一个 `wvd_mask`（逐 lane 写掩码）。`warp_id` 是仲裁后回写与记分板释放的关键索引。

#### 4.1.4 代码实践

**实践目标**：观察写回模块在仿真中的输出格式，理解一条指令的写回时序。

**操作步骤**（源码阅读 + 仿真观察型）：

1. 在 `Writeback` 中找到 `SPIKE_OUTPUT` 控制的两段 `printf`：[writeback.scala:L67-L81](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/writeback.scala#L67-L81)。当 `SPIKE_OUTPUT=true` 时，每次 `out_x.fire` 会打印 `sm <sm_id> warp <wid> <pc> <inst> x<reg_idxw> <data>`，`out_v.fire` 会打印 `v<reg_idxw> <mask> <data...>`。
2. 按 u1-l4 在 `sim-verilator` 下构建并运行 `vecadd` 测试用例（若构建系统支持开启 SPIKE 输出，则在配置中打开它）。
3. 在仿真日志中找一行标量写回，例如形如 `sm 0 warp 0 0x... 0x... x5 0x...`。

**需要观察的现象**：

- 同一个 `warp_id` 的写回行是**乱序**出现的（MUL 在 ALU 之后若干拍），证明执行单元延迟不同。
- 偶尔可见两行写回时间戳相邻但 `warp_id` 不同——因为标量通路 `out_x` 与向量通路 `out_v` 是两条独立通路，可同拍各写一个。

**预期结果**：写回日志的条数应与程序实际产生结果的指令数一致。若你无法本地运行，明确标注「待本地验证」，但上述 printf 格式可直接从源码确认。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `arbiter_x` 从 `Arbiter` 换成 `RRArbiter`，对性能公平性有什么影响？

**参考答案**：`Arbiter` 是固定优先级，`in_x(0)`（ALU）只要 valid 就永远抢先，可能导致排在后面的 SFU/LSU 结果在突发时被长时间推迟（饥饿）。换成 `RRArbiter`（轮转优先级）后，各路轮流获准，公平性更好但单路最坏延迟略增。Ventus 这里用固定优先级，是因为 ALU 结果最频繁且最关键。

**练习 2**：为什么标量和向量要分成两套独立的 FIFO + Arbiter，而不是合成一套 12 路仲裁？

**参考答案**：标量寄存器堆和向量寄存器堆是物理分离的存储体，写端口互不相关。分成两套，标量、向量可以同拍各写一个，吞吐翻倍；若合成一套，每拍只能写一个，且仲裁器规模翻倍，得不偿失。

---

### 4.2 Branch_back：分支结果的合并回传

#### 4.2.1 概念说明

分支指令有两类来源，都需要把「是否跳转 + 跳转目标」回传给取指调度器 `warp_scheduler`，以便它更新该 warp 的 PC：

1. **标量分支**（`ALUexe` 解析的 `beq`/`jal`/`jalr` 等），从 `alu.io.out2br` 来（见 u5-l2）。
2. **SIMT 分支**（`vbeq` 等向量分支经 `branch_join`/`simt_stack` 处理后的汇合跳转，见 u5-l5），从 `simt_stack.io.fetch_ctl` 来。

`Branch_back` 就是把这两路 `BranchCtrl` 用一个 2 输入仲裁器合成一路，再送给 `warp_scheduler.io.branch`。它和 `Writeback` 的结构几乎一模一样，只是数据类型换成了 `BranchCtrl`。

#### 4.2.2 核心流程

```
alu.io.out2br ───► Branch_back.in0 ─┐
                                    ├─► Arbiter(2) ──► out ──► warp_sche.io.branch (更新 PC/冲刷)
simt_stack.io.fetch_ctl ──► in1 ────┘
```

`warp_scheduler` 收到 `branch.fire & jump` 后，会改写该 warp 的 PC 并触发流水线冲刷（flush）。这一点在 u4-l1 的取指、u5-l5 的 SIMT 分支中都已涉及，本讲只补齐「两路分支如何汇合」这一段。

#### 4.2.3 源码精读

`Branch_back` 的定义极其简洁：

[writeback.scala:L18-L36](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/writeback.scala#L18-L36) — 两个 `Queue.apply(_, 0)` 缓冲 + 一个 `new Arbiter(new BranchCtrl, 2)`，输入 `in0`/`in1` 分别接两路分支，输出 `out` 统一回传。

`BranchCtrl` 的字段（注意此处用 `execution.scala` 中的定义）：

[execution.scala:L20-L25](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L20-L25) — `wid`（哪个 warp）、`jump`（是否真跳）、`new_pc`（跳转目标地址）。

在 `pipe.scala` 中的接线：

[pipe.scala:L388](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L388) 与 [pipe.scala:L392](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L392) — `simt_stack.io.fetch_ctl <> branch_back.io.in1`（SIMT 分支路）、`alu.io.out2br <> branch_back.io.in0`（标量分支路）。

[pipe.scala:L148](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L148) — `warp_sche.io.branch <> branch_back.io.out`，合并后的分支结果送回取指调度器。

> SPIKE 对拍模式下，`Branch_back` 也会打印每次跳转：[writeback.scala:L30-L35](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/writeback.scala#L30-L35) 输出 `Jump? <0/1> <new_pc>`，便于核对控制流。

#### 4.2.4 代码实践

**实践目标**：追踪一条标量分支指令的结果通路，看清它与普通 ALU 结果的分叉点。

**操作步骤**（源码阅读型）：

1. 打开 [execution.scala:L26-L60](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L26-L60)（`ALUexe`）。注意它有**两个出口**：`out`（普通运算结果，送 `wb.in_x(0)`）和 `out2br`（分支结果，送 `branch_back.in0`）。
2. 跟踪 `result_br.io.enq.bits.jump` 的计算：它由 `ctrl.branch`（`B_B`/`B_J`/`B_R`/`B_N`）决定，`B_B` 时取 `alu.io.cmp_out`（比较器输出），`B_J`/`B_R` 时恒为 `true`，`B_N`（非分支）时为 `false`。
3. 确认 `new_pc` 取自 `in3`（即 `ALUexe` 中预留的第三操作数槽，见 u5-l2 的 `in3`）。

**需要观察的现象 / 预期结果**：一条条件分支 `beq` 在 `ALUexe` 中同时驱动 `out`（写回比较结果，若 `wxd`=1）和 `out2br`（送分支仲裁）。若 `cmp_out=1`，则 `jump=1`，`new_pc=in3`，经 `Branch_back` 到 `warp_scheduler` 改写 PC。请把这条链路画成时序图，标注每一拍在哪个模块。

#### 4.2.5 小练习与答案

**练习 1**：`Branch_back` 里 `in0`（标量分支）和 `in1`（SIMT 分支）同拍都 valid 时，谁优先？这合理吗？

**参考答案**：`Arbiter` 固定优先级，`in0` 优先。通常同一个 warp 不会同拍既执行标量分支又执行 SIMT 分支（一条指令只走一条路），所以两路同时 valid 一般来自**不同 warp**，此时谁先谁后只影响更新 PC 的次序，不影响正确性。落选的一路留在 FIFO 里下一拍再发。

**练习 2**：`Branch_back` 与 `Writeback` 结构如此相似，为什么没有合并成一个通用模块？

**参考答案**：二者数据类型不同（`BranchCtrl` vs `WriteScalarCtrl`/`WriteVecCtrl`）、去向不同（分支结果回 `warp_scheduler` 改 PC，运算结果回寄存器堆），语义完全不同。结构相似只是因为「N 选 1 仲裁 + FIFO 缓冲」是一个通用模式，分开实现更清晰，也便于各自加对拍打印。

---

### 4.3 CSRFile：单个 warp 的 CSR 寄存器文件

#### 4.3.1 概念说明

CSR（Control and Status Register，控制与状态寄存器）存放的是「控制流和运行时上下文」而非运算数据。Ventus 的 CSR 分三大类：

- **自定义 warp/CTA CSR**（`0x800~0x80c`）：thread id、warp 数、kernel 基址、wg_id、wf_tag、LDS 基址、pds 基址、3D 维度、rpc 等。这些是 GPU 编程模型专属的。
- **浮点 CSR**（`0x001`~`0x003`）：`fflags`（异常标志）、`frm`（舍入模式）、`fcsr`（二者合并视图）。
- **机器模式 CSR**（`0x300`~`0xF14`）：`mstatus`/`mip`/`mie`/`mepc`/`mcause` 等，源自 RISC-V 特权架构，Ventus 大多保留位但功能极简（基本不触发真正的中断/异常）。
- **向量 CSR**（`0x008`~`0xC22`）：`vstart`、`vl`（向量长度）、`vtype`（向量类型）等。

`CSRFile` 是**单个 warp** 的 CSR 文件实例。它支持标准 RISC-V 的四种 CSR 访问语义：N（不操作）、W（写）、S（置位）、C（清零），并对几条特殊指令（`vsetl`、`setrpc`）做专门处理。

#### 4.3.2 核心流程

一条 CSR 指令（如 `csrrw`/`csrrs`/`vsetvli`/`setrpc`）进入 `CSRFile` 后：

1. **取地址**：`csr_addr = inst[31:20]`（指令字的 12 位 CSR 地址域）。
2. **读旧值**：用 `Lookup` 在 `csrFile` 表里按地址查当前值 `csr_rdata`，同时把它作为「读出的旧值」送回写回通路（CSR 指令通常把旧值写入目的寄存器）。
3. **算写值**：依 `ctrl.csr`（2 位操作码）算实际写入值 `csr_wdata`：
   - `W`（写）= `csr_input`（源操作数）；
   - `S`（置位）= `csr_rdata | csr_input`；
   - `C`（清零）= `csr_rdata & ~csr_input`。
4. **写生效**：`wen = ctrl.csr.orR & write`（是 CSR 操作且本拍允许写）。再按地址把 `csr_wdata`（或专门计算的值）写回对应寄存器。其中两条指令有**特殊旁路**：
   - `isvec`（向量 `vsetl`/`vsetvli`）：`vl = min(AVL, VLMAX)`，其中 `AVL = csr_input`（应用给出的长度）、`VLMAX = num_thread`（默认 32）。
   - `custom_signal_0`（`setrpc`）：把 `csr_input` 直接写入 `rpc`（SIMT 汇合点 PC，供 u5-l5 的 `branch_join` 使用）。

#### 4.3.3 源码精读

先看地址常量布局。`object CSR` 集中定义所有 CSR 地址：

[CSR.scala:L17-L86](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L17-L86) — 自定义 warp CSR 在 [L28-L42](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L28-L42)：`threadid=0x800`、`wg_wf_count=0x801`、`wf_size_dispatch=0x802`、`knl_base=0x803`、`wg_id=0x804`、`wf_tag_dispatch=0x805`、`lds_base_dispatch=0x806`、`pds_baseaddr=0x807`、`wg_id_x/y/z=0x808/9/a`、`csr_print=0x80b`、`rpc=0x80c`。

> 注意 `0x807` 在源码注释中是 `pds_baseaddr`（私有内存栈基址），这与 u2-l1 强调的「源码中 0x807 实为 pds_baseaddr，非文档所称的全局基址」一致。始终以源码为准。

四种操作语义的常量与写值计算：

[CSR.scala:L17-L22](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L17-L22) 定义 `N/W/S/C`；

[CSR.scala:L216-L223](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L216-L223) — `wen = ctrl.csr.orR & write`，并用 `MuxLookup` 按 W/S/C 算出 `csr_wdata`。

读旧值用 `Lookup` 在表里查：

[CSR.scala:L225-L254](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L225-L254) — `csrFile` 是 `Seq(BitPat(addr) -> reg)`，`csr_rdata := Lookup(csr_addr, 0.U, csrFile)`。读出的旧值经 `wdata` 送到 `io.wb_wxd_rd`，构成 CSR 指令「读旧值→写目的寄存器」的语义。

特殊写逻辑（`vsetl` 与 `setrpc`）：

[CSR.scala:L257-L264](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L257-L264) — `custom_signal_0`（`setrpc`）把 `csr_input` 写入 `rpc`；`isvec`（`vsetl`/`vsetvli`）计算 `wdata = Mux(AVL < VLMAX, AVL, VLMAX)`，即向量长度 `vl = min(AVL, num_thread)`。其余按地址分支写各自的物理寄存器。

`CSRFile` 还向 LSU、SIMT、操作数收集器输出若干「上下文值」：

[CSR.scala:L314-L317](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L314-L317) — `io.lsu_tid = wf_tag_dispatch * num_thread`（LSU 用 thread id 算私有内存偏移）、`io.lsu_pds = pds_baseaddr`、`io.lsu_numw = wg_wf_count`；而 `io.simt_rpc = rpc` 把汇合点 PC 送给 SIMT stack（u5-l5）。

#### 4.3.4 代码实践

**实践目标**：手工推导一条 `csrrw` 指令在 `CSRFile` 中的读写全过程。

**操作步骤**（源码阅读型）：

1. 假设指令为 `csrrw rd, frm, rs1`（把 `frm` 旧值写到 `rd`，把 `rs1` 写入 `frm`）。查地址：`frm = 0x002`（[CSR.scala:L25](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L25)）。
2. 译码后 `ctrl.csr = W`（写），`csr_input = in1 = rs1` 的值。
3. 走 [L217-L223](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L217-L223)：`csr_wdata = csr_input`。
4. 走 [L273-L274](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L273-L274)：`frm := csr_wdata(2,0)`，即取低 3 位作为新舍入模式。
5. 同时 `csr_rdata`（旧 `frm`）经 `wdata` 送 `io.wb_wxd_rd`，最终写回 `rd`。

**需要观察的现象 / 预期结果**：写出每一步的信号取值表（`csr_addr / ctrl.csr / csr_input / csr_rdata / csr_wdata / frm(新) / wb_wxd_rd`），确认「读旧值、写新值」同时发生。

#### 4.3.5 小练习与答案

**练习 1**：`vsetvli` 指令给出 `AVL=48`，Ventus 默认 `num_thread=32`，则 `vl` 被设为多少？为什么？

**参考答案**：`vl = min(48, VLMAX=32) = 32`。因为硬件一个 warp 只有 32 个 lane（`VLMAX=num_thread=32`），向量化运算最多同时处理 32 个元素，超出部分由编译器拆成多个 chime（见 u5-l2/u5-l3 的 lane 复制概念）。所以 `vl` 被钳位到 32。

**练习 2**：`csrrs`（置位）和 `csrrw`（写）在 `csr_wdata` 计算上有什么区别？

**参考答案**：`csrrw`（W）直接用源操作数覆盖：`csr_wdata = csr_input`；`csrrs`（S）是按位或：`csr_wdata = csr_rdata | csr_input`，只把 `csr_input` 中为 1 的位置成 1，其余保持。`csrrc`（C）则是按位与取反：`csr_rdata & ~csr_input`，清零对应位。

---

### 4.4 CSRexe：per-warp 例化与 warp 启动初始化

#### 4.4.1 概念说明

`CSRFile` 只服务于**一个** warp，而 SM 里有 `num_warp`（默认 8）个 warp 同时活跃，每个 warp 的 CSR 上下文必须彼此隔离（thread id、基址各不相同）。`CSRexe` 就是「容器」：它用 `VecInit` 例化 `num_warp` 份 `CSRFile`，并根据当前指令的 `wid` 把读写路由到对应那一份。

`CSRexe` 还承担两个关键职责：

1. **warp 启动初始化**：当 CTA 调度器派发一个新 warp 到 SM 时，`CTA2csr` 有效，`CSRexe` 把派发数据写入「该硬件 wid 对应的 CSRFile」，计算出 `threadid`、`lds_base` 等运行时上下文。
2. **舍入模式分发**：浮点类单元（FPU/SFU/TC）需要当前 warp 的 `frm`，`CSRexe` 按各单元提供的 `rm_wid` 查对应 warp 的 `frm` 并输出。

#### 4.4.2 核心流程

**A. CSR 指令的读写路由**

```
csrExeData(含 ctrl.wid, in1) ──► CSRexe.in
   │  按 wid 路由
   └─► vCSR(ctrl.wid).write := in.fire
       vCSR(ctrl.wid) 算出 wb_wxd_rd
       ──► result Queue(pipe) ──► CSRexe.out ──► wb.in_x(3)
```

所有 `num_warp` 份 CSRFile 共享同一份 `ctrl`/`in1`（广播），但**只有 `ctrl.wid` 那一份**的 `write` 拉高，确保只写目标 warp。

**B. warp 启动初始化（CTA2csr）**

派发数据来源链路（串起 u3-l3 → u4-l1 → 本讲）：

```
CTA调度器 ──CTAreqData──► CTA2warp(分配硬件wid)
              ──warpReqData{CTAdata, wid}──► warp_scheduler
                  └─(透传)──► CTA2csr(Valid)
                       └──► CSRexe ──► vCSR(CTA2csr.bits.wid).CTA2csr.valid := valid
```

在目标 CSRFile 内（`CSR.scala` 的 `when(io.CTA2csr.valid)` 块），一次性写入所有派发寄存器，并按公式算出 `threadid` 与 `lds_base`。

**C. 互斥关系**：CSR 写指令与 warp 初始化**不能同拍**作用于同一 CSRFile。`CSRexe` 用 `io.in.ready := result.io.enq.ready & !io.CTA2csr.valid` 强制：当某 warp 正在被初始化（`CTA2csr.valid`）时，暂停接收 CSR 执行指令，避免初始化值被一条碰巧同拍的 CSR 指令覆盖（或反之）。

#### 4.4.3 源码精读

`CSRexe` 例化 `num_warp` 份 CSRFile 并广播输入：

[CSR.scala:L337-L348](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L337-L348) — `vCSR = VecInit(Seq.fill(num_warp)(Module(new CSRFile).io))`；`foreach` 把所有 CSRFile 的 `ctrl/in1` 接到公共输入、`write/CTA2csr.valid` 默认清零（随后按 wid 选择性置位）。`sgpr_base/vgpr_base` 按 warp 输出送给操作数收集器。

按 wid 选择写目标与初始化目标：

[CSR.scala:L354-L355](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L354-L355) — `vCSR(io.in.bits.ctrl.wid).write := io.in.fire`（CSR 指令只写目标 warp）；`vCSR(io.CTA2csr.bits.wid).CTA2csr.valid := io.CTA2csr.valid`（初始化只作用于派发的那个硬件 wid）。

互斥与结果队列：

[CSR.scala:L356-L365](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L356-L365) — `result` 是 `Queue(..., 1, pipe=true)`，把读出的旧值（`vCSR(ctrl.wid).wb_wxd_rd`）打包成 `WriteScalarCtrl` 送 `io.out`。`io.in.ready := result.io.enq.ready & !io.CTA2csr.valid` 实现前述互斥。

> 舍入模式分发：[CSR.scala:L368-L370](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L368-L370) — `io.rm(x) := vCSR(io.rm_wid(x)).frm`，三个 `rm_wid` 分别由 FPU/SFU/TC 在 `pipe.scala` 中回填（[pipe.scala:L403-L408](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L403-L408)），让每个浮点单元拿到它正在处理的那个 warp 的舍入模式。

**warp 启动初始化的核心计算**（本讲重点，位于 `CSRFile` 内）：

[CSR.scala:L296-L313](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L296-L313) — `when(io.CTA2csr.valid)` 块把 `CTAdata` 的各字段写入对应 CSR。三条最关键的公式：

- **wf_tag_dispatch（CSR_WID）**：[L302](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L302) 取 `dispatch2cu_wf_tag_dispatch(depth_warp-1, 0)`，即 wf_tag 的低 `depth_warp` 位（= `wf_id_in_wg`）。
- **threadid（CSR_TID）**：[L312](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L312) `threadid := wf_tag_dispatch[depth_warp-1:0] << depth_thread`。

  这正是 u2-l1 所说的 `CSR_TID = wf_id_in_wg × num_thread`：`depth_thread = log2Ceil(num_thread) = 5`，左移 5 位即乘以 32。例如 `wf_id_in_wg=2`，则 `threadid = 2<<5 = 64`，该 warp 的线程 id 范围是 64~95。

- **lds_base_dispatch（CSR_LDS）**：[L304](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L304) `Cat(LDS_BASE(31, LDS_ID_WIDTH+1), dispatch2cu_lds_base_dispatch)`，把 `LDS_BASE=0x70000000` 的高位与调度器给的偏移拼接，得到该 workgroup 在共享内存中的真实物理基址（与 u2-l1 讲的「`[LDS_BASE, +128KiB)` 走 sharedmem」对应）。

此外，`sgpr_base_dispatch` / `vgpr_base_dispatch`（[L300-L301](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L300-L301)）经 `io.sgpr_base`/`io.vgpr_base` 输出，送操作数收集器作为寄存器堆寻址基址（u4-l4 的 `sgpr_base`/`vgpr_base`），把 u3 的资源分配与本讲的 CSR 上下文闭环。

派发数据的源头字段定义在 `CTAreqData`：

[CTA2warp.scala:L17-L40](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CTA2warp.scala#L17-L40) — `CTAreqData` 含 `dispatch2cu_wf_tag_dispatch`、`dispatch2cu_sgpr_base_dispatch`、`dispatch2cu_vgpr_base_dispatch`、`dispatch2cu_lds_base_dispatch`、`dispatch2cu_pds_base_dispatch`、3D 维度等；`warpReqData = {CTAdata, wid}`，`wid` 是 `CTA2warp` 分配的硬件 wid。`warp_scheduler` 把 `warpReq` 透传成 `CTA2csr`：[warp_schedule.scala:L58-L59](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/warp_schedule.scala#L58-L59)，再由 `pipe.scala` [L163](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L163) 接到 `csrfile.io.CTA2csr`。

#### 4.4.4 代码实践

**实践目标**：追踪 warp 启动时 CSR_TID / CSR_WID / CSR_LDS 的计算与写入，验证它们与 `wf_tag` 的关系。

**操作步骤**（源码阅读 + 推算型）：

1. 假设一个 workgroup 含 4 个 warp（`wg_wf_count=4`），每个 warp 32 线程。CTA 调度器派发第 2 个 warp（`wf_id_in_wg=2`，`wf_tag = {wg_slot_id=1, wf_id_in_wg=2}`，默认 6 bit）。CTA2warp 为它分配硬件 wid=5。
2. 跟随数据走一遍：
   - `CTA2warp` 生成 `warpReqData{CTAdata.dispatch2cu_wf_tag_dispatch = 6'b010010（wg_slot=1=2'b01, wf_id=2=3'b010，拼接高3低3）, wid=5}`；
   - `warp_scheduler` 透传为 `CTA2csr.valid`，`CTA2csr.bits.wid=5`；
   - `CSRexe` 选中 `vCSR(5)`，触发其 `when(io.CTA2csr.valid)` 块。
3. 在 [CSR.scala:L302](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L302) 与 [L312](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L312) 推算：
   - `wf_tag_dispatch = wf_tag[2:0] = 3'b010 = 2`（即 `wf_id_in_wg`）；
   - `threadid = 2 << 5 = 64`。
4. 在 [L304](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L304) 推算 `lds_base`：设 `dispatch2cu_lds_base_dispatch` 为该 WG 的偏移 `OFF`，则 `lds_base = 0x70000000 的高位 | OFF`。

**需要观察的现象 / 预期结果**：把四个 warp（`wf_id_in_wg` = 0/1/2/3）的 `threadid` 填成下表：

| wf_id_in_wg | threadid（十进制） | 线程 id 范围 |
|-------------|-------------------|--------------|
| 0 | 0 | 0~31 |
| 1 | 32 | 32~63 |
| 2 | 64 | 64~95 |
| 3 | 96 | 96~127 |

这与 u2-l1 的 `CSR_TID = wf_id_in_wg × num_thread` 完全吻合。若 `wf_tag` 拼接位序或 `depth_thread` 取值你不确定，请以本地生成的 Verilog 或 `parameters.scala`（`depth_thread = log2Ceil(num_thread)`，[parameters.scala:L36](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L36)）为准，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `CSRexe` 要为每个 warp 各开一份 `CSRFile`，而不是像通用寄存器堆那样按 bank 交织共享？

**参考答案**：CSR 是「每 warp 私有」的上下文（thread id、基址、舍入模式都不同），且 CSR 访问频率远低于通用寄存器。按 warp 各开一份，物理隔离、无需 bank 仲裁、寻址简单；而通用寄存器堆吞吐要求高，才用 bank 交织提升端口数（u4-l4）。这是「低频私有 → 复制」「高频共享 → 交织」的设计取舍。

**练习 2**：`io.in.ready := result.io.enq.ready & !io.CTA2csr.valid` 中的 `!io.CTA2csr.valid` 起什么作用？去掉会怎样？

**参考答案**：它保证 warp 初始化（`CTA2csr.valid`）与 CSR 写指令互斥。若去掉，可能出现：同拍内一条 CSR 写指令正在改某 warp 的 CSR，而 `CTA2csr` 又要把派发初值写入**同一个 CSRFile**，二者竞争 `when` 块的赋值，导致初始化值被覆盖（或写指令失效），warp 启动上下文错误。保留这处互斥是正确性所必需的。

**练习 3**：FPU、SFU、TensorCore 三个浮点单元各自处理不同 warp 的指令，`CSRexe` 如何让它们各取所需 warp 的 `frm`？

**参考答案**：`pipe.scala` 在每个浮点单元拿到指令时，把该指令的 `ctrl.wid` 回填给 `csrfile.io.rm_wid(i)`（[pipe.scala:L403-L408](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L403-L408)），`CSRexe` 据此索引 `vCSR(rm_wid(i)).frm` 输出（[CSR.scala:L368-L370](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L368-L370)）。即「单元报上自己正在服务的 wid → CSRexe 查该 wid 的 frm」。

---

## 5. 综合实践

把本讲四个模块串成一条端到端链路：**warp 启动初始化 → CSR 读取 → 运算 → 写回**。

**任务**：在源码中跟踪下面这个场景，画出完整的时序-数据流图，并回答末尾的问题。

**场景**：

1. 一个新 warp 被派发到 SM，硬件 wid=3，`wf_id_in_wg=1`，每 warp 32 线程。
2. 该 warp 的第一条指令是 `vsetvli`，设 `AVL=20`；第二条是向量加法 `vadd`，结果写回向量寄存器。

**跟踪要求**：

1. **初始化阶段**：标出 `CTA2csr.valid` 期间，`CSRexe` 选中 `vCSR(3)`，写出 `threadid`、`wf_tag_dispatch`、`wg_wf_count` 的写入值。指出此期间 `io.in.ready` 为何为 0。
2. **vsetvli 阶段**：在 `CSRFile`（wid=3）中，`isvec` 分支算出 `vl = min(20, 32) = 20`。指出该值经哪条通路（`wb_wxd_rd` → `result` Queue → `CSRexe.out` → `wb.in_x(3)` → 仲裁 → 寄存器堆）写回 `vsetvli` 的目的标量寄存器。
3. **vadd 写回阶段**：`vALUv2` 算出结果后，经 `valu.io.out → wb.in_v(0)`，与同拍可能的其它向量结果一起被 `arbiter_v` 仲裁，最终从 `wb.out_v` 写回向量寄存器堆，并把 `wb_v_fire` 送记分板释放。
4. **时序**：在图上标出每一步发生在第几拍（相对拍数即可），重点标注「写回 fire → 记分板释放 → 依赖指令可发射」的 1 拍间隔。

**思考题**（综合本讲与 u4-l3、u5-l1）：

- 若 `vadd` 依赖的前一条向量指令尚未写回，记分板如何把 `vadd` 按在队头？写回 `fire` 后 `vadd` 最早可在第几拍发射？
- `vsetvli` 的结果走的是**标量**写回通路（`wb.in_x(3)`），而 `vadd` 走**向量**通路（`wb.in_v(0)`）。这说明 `vsetvli` 的目的寄存器是标量还是向量？为什么？

> 参考结论：`vsetvli` 把 `vl` 写入一个**标量**目的寄存器（`rd`），因此进标量写回通路；这与 RISC-V 向量扩展一致——`vsetvli` 的 `rd` 是 GPR。`vadd` 的结果才是向量，进向量写回通路。两条通路并行，互不干扰。

## 6. 本讲小结

- **Writeback** 是结果汇聚漏斗：标量、向量各一套「每路输出 FIFO + 固定优先级 Arbiter」，把 8 类执行单元的乱序结果串行化写回各自寄存器堆；写回 `fire` 当拍释放记分板对应位。
- **Branch_back** 用一个 2 输入 Arbiter 把 ALU 标量分支（`in0`）与 SIMT 分支（`in1`）合并回传 `warp_scheduler`，驱动 PC 更新与流水线冲刷。
- **CSRFile** 是单 warp 的 CSR 文件，支持 N/W/S/C 四种读写语义，并对 `vsetl`（`vl=min(AVL,num_thread)`）和 `setrpc`（写 `rpc` 汇合点）做专门处理。
- **CSRexe** 为 `num_warp` 个 warp 各开一份 `CSRFile`，按 `wid` 路由读写；并在 warp 派发时依据 `wf_tag` 计算 `threadid = wf_id_in_wg<<5`、拼接出 `lds_base`，把 u3 的资源分配闭环到寄存器堆寻址。
- **互斥与隔离**：CSR 写指令与 warp 初始化通过 `!io.CTA2csr.valid` 互斥；各 warp CSR 物理隔离；标量/向量写回通路独立并行。

## 7. 下一步学习建议

至此，SM 流水线从取指（u4-l1）到写回（本讲）的完整数据通路已经讲完。建议：

1. **横向串读**：回到 [pipe.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala)，从 `wb` 模块出发，沿 `out_x`/`out_v` 反向追到每个执行单元，正向追到操作数收集器与记分板，把 u4/u5 全单元的连接在一张大图上画全。
2. **进入缓存层（u6）**：本讲的 LSU 写回依赖访存返回，下一步学习 L1 指令缓存（u6-l1）、数据缓存（u6-l2）、MSHR 机制（u6-l3）与共享内存（u6-l4），理解「访存结果从哪里来、何时到」。
3. **系统视角（u7）**：若想理解 CSR 里的 `wg_id`、3D 维度等如何从 host 经 AXI4-Lite 写入（u7-l2），或 GVM 对拍如何比对写回结果（u7-l4），可继续学习第 7 单元。
4. **动手验证**：尝试在 `sim-verilator` 中开启 SPIKE 输出，对照本讲给出的 `printf` 格式，在日志中找到一条标量写回、一条向量写回、一次分支跳转，把源码与实际波形/日志一一对应。
