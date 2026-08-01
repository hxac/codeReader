# CSR 寄存器与分支 branch_back

## 1. 本讲目标

本讲聚焦 SM 流水线中两条「控制类」通路：**CSR（控制与状态寄存器）** 与**标量分支/跳转**。学完后你应当能够：

- 说清 Ventus 的 CSR 地址空间由「RISC-V 标准机器态 CSR」与「Ventus 自定义 CSR」两部分组成，并列举关键自定义 CSR（`wg_id`、`knl_base`、`rpc`、`pds_baseaddr` 等）。
- 解释 CSR 读改写三种类型（CSR_W / CSR_S / CSR_C）在硬件上如何用一位多路选择实现。
- 理解 `csrfile` 如何在 warp 派发时把 CTA 调度器送来的派发参数「锁存」成 CSR，又如何把这些值分发给 LSU、operand_collector、FPU、simt_stack 等消费者。
- 看懂 `csrexe` 用 `generate for` 为每个 warp 例化一份 `csrfile`，并按 warp 号做多路读出。
- 跟踪一条标量分支（BEQ/JAL/JALR）从 `aluexe` 计算出跳转目标，经 `branch_back` 仲裁，回送到 `warp_scheduler` 更新 PC 的完整链路，并理解 `SETRPC` 与分支目标的关系。

## 2. 前置知识

阅读本讲前，建议你已经学完：

- **u3-l3（译码 decodeUnit）**：知道 `decodeUnit` 把 32 位指令查表成一个打包控制字 `ctrlSignals`，再切片成 `csr`、`branch`、`custom_signal_0` 等具名信号。本讲会反复用到这些译码产物。
- **u3-l4（发射 issue 与 scoreboard）**：知道 `issue` 是一个按优先级把指令路由到各执行单元（sALU / CSR / SIMT …）的组合路由器。CSR 指令和标量分支分别走 CSR 通路与 sALU 通路。
- **u2-l2 / u2-l3（CTA 派发）**：知道 CTA 调度器在派发一个 warp 时会附带 `wg_id`、`start_pc`、`sgpr/vgpr/lds base`、`wf_tag`、`pds_base`、`knl_base` 等一组「派发参数」。本讲会看到这些参数正是被写进 CSR 的。

几个需要先建立的术语：

- **CSR（Control and Status Register）**：RISC-V 里一类「系统寄存器」，与通用寄存器堆分离，用专门的 `CSR*` 指令通过 12 位地址访问。它常用于保存 CPU 状态、配置（如浮点舍入模式）、以及——在 GPU 里——保存线程/warp/workgroup 的身份与基地址。
- **标量分支（scalar branch）**：以标量寄存器或立即数为条件的、整个 warp 一起走的分支（BEQ/BNE/BLT…），区别于 warp 内线程各走各路的 SIMT 分支（后者由 simt_stack 处理，见 u5-l3）。
- **PC（Program Counter）**：取指地址。每个 warp 在 `warp_scheduler` 里维护自己的 PC。

> 本讲只讲「标量分支 + CSR」。warp 内部的发散/汇合（SIMT 栈）是下一讲 u5-l3 的内容，本讲会在涉及处点到为止。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v) | 定义全部 CSR 地址宏（标准 + 自定义）、分支类型 `B_*`、CSR 读改写类型 `CSR_*`、`SETRPC`/`JAL`/`JALR` 指令位模式 |
| [csrfile.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/csr/csrfile.v) | 单个 warp 的 CSR 存储体：读多路、写读改写、锁存派发参数、向各消费者输出 |
| [csrexe.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/csr/csrexe.v) | CSR 执行单元：为 `NUM_WARP` 个 warp 各例化一个 `csrfile`，按 warp 号选通写入选中的实例、并多路读出 frm/lsu_tid/pds/numw/simt_rpc 等 |
| [branch_back.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/branch_back.v) | 分支汇聚：把标量分支（来自 sALU）与向量分支（来自 simt_stack）按「标量优先」仲裁后送给 warp_scheduler |
| [aluexe.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/aluexe.v) | 标量 ALU 执行单元（sALU），同时承担标量分支：按 `B_B/B_J/B_R` 计算是否跳转与目标 PC |
| [decodeUnit.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v) | 译码表：为 CSR 指令填 `csr` 字段、为分支填 `branch` 字段、为 `SETRPC` 额外置 `custom_signal_0` |
| [pipe.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v) | 把上述模块连起来：例化 `csrexe`、`aluexe`、`branch_back`、`warp_scheduler` |

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：CSR 地址空间与读改写类型 → `csrfile` → `csrexe` → 标量分支执行与 `branch_back`。

### 4.1 CSR 地址空间与读改写类型

#### 4.1.1 概念说明

RISC-V 的 CSR 用指令里的 12 位字段（`inst[31:20]`）作地址，理论上有 4096 个 CSR。Ventus 并未实现全部，而是挑选了一组「够用」的 CSR，分为三类：

1. **标准机器态 CSR**：如 `mstatus`(0x300)、`mepc`(0x341)、`mcause`(0x342)、`mtvec`(0x305)、`mip/mie` 等，用于异常与中断。Ventus 实现了它们的数据通路，但 GPU 核实际运行 kernel 时很少触发机器态异常。
2. **浮点 CSR**：`fflags`(0x001)、`frm`(0x002)、`fcsr`(0x003)，其中 `frm`（浮点舍入模式）会被实时分发给 FPU，是「真正被读」的 CSR。
3. **Ventus 自定义 CSR**：地址集中在 `0x800~0x80c`，这是本讲的重点。它们不是给程序员随便读写的「状态」，而是 GPU 在派发 warp 时**硬件自动写入**的「身份与基地址」，供流水线各处读取。

此外还有读改写类型。RISC-V 的 CSR 指令有三种写语义：

| 助记符 | 类型码 | 写入语义 |
| --- | --- | --- |
| CSRRW / CSRRWI | `CSR_W` (2'b01) | 直接写入新值 |
| CSRRS / CSRRSI | `CSR_S` (2'b10) | `old \| new`（置位） |
| CSRRC / CSRRCI | `CSR_C` (2'b11) | `old & ~new`（清位） |

不读写 CSR 的指令类型为 `CSR_N` (2'b00)。

#### 4.1.2 核心流程

```
指令 inst[31:20] ──► 12 位 csr_addr ──► 读多路选出一个 csr_rdata（旧值）
                                            │
ctrl_csr(2'b?)+立即数/寄存器值 ──► 按 W/S/C 算出 csr_wdata（新值）
                                            │
                        (命中某 CSR 时) 时序写入对应寄存器
```

读是纯组合（当拍出旧值），写是时序（下拍生效）。对 `CSRRS/CSRRC`，硬件需要**同时**拿到旧值（读）和新值（写），所以读路径必须先把旧值算出来，再与输入做或/与。

#### 4.1.3 源码精读

**自定义 CSR 地址**（[define.v:1218-1234](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L1218-L1234)）：注意它们连续分布在 `0x800~0x80c`。

```verilog
`define CSR_THREADID          12'h800   // 线程基址（由 wf_tag 派生）
`define CSR_WG_WF_COUNT       12'h801   // 本 workgroup 的 warp 总数
`define CSR_WF_SIZE_DISPATCH  12'h802   // 本 warp 的线程数
`define CSR_KNL_BASE          12'h803   // kernel 元数据基址
`define CSR_WG_ID             12'h804   // workgroup 全局编号
`define CSR_WF_TAG_DISPATCH   12'h805   // warp 的 tag（槽位+序号）
`define CSR_LDS_BASE_DISPATCH 12'h806   // 共享内存(LDS)基址
`define CSR_PDS_BASEADDR      12'h807   // 参数数据栈(PDS)基址
`define CSR_WG_ID_X/Y/Z       12'h808.. // workgroup 三维索引
`define CSR_PRINT             12'h80b   // 仿真打印用
`define CSR_RPC               12'h80c   // SIMT 分支用的返回/目标 PC
```

**标准机器态 CSR**（[define.v:1252-1262](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L1252-L1262)）：`CSR_MSTATUS`(0x300)、`CSR_MIE`(0x304)、`CSR_MTVEC`(0x305)、`CSR_MSCRATCH`(0x340)、`CSR_MEPC`(0x341)、`CSR_MCAUSE`(0x342)、`CSR_MTVAL`(0x343)、`CSR_MIP`(0x344)。

**读改写类型与分支类型宏**（[define.v:421-445](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L421-L445)）：

```verilog
`define B_N 2'b00  `define B_B 2'b01  `define B_J 2'b10  `define B_R 2'b11  // 分支类型
`define CSR_N 2'b00 `define CSR_W 2'b01 `define CSR_S 2'b10 `define CSR_C 2'b11 // CSR 类型
```

**译码表如何填这些字段**（[decodeUnit.v:307-312](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L307-L312)）：以 `CSRRW`/`CSRRS`/`CSRRC` 为例，译码出的控制字第 `csr` 字段分别填 `CSR_W`/`CSR_S`/`CSR_C`，其余字段（alu_fn 等）保持默认。这说明 CSR 指令的「运算」并不在 ALU 里做，而是由 `csrfile` 自己按 `csr` 字段完成读改写。

```verilog
`CSRRW : ctrlSignals_0 = {...,`CSR_W,...};
`CSRRS : ctrlSignals_0 = {...,`CSR_S,...};
`CSRRC : ctrlSignals_0 = {...,`CSR_C,...};
```

控制字各字段在 [decodeUnit.v:602-642](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L602-L642) 切片，其中 `csr` 在 `[34:33]`、`branch` 在 `[38:37]`、`custom_signal_0` 在 `[1]`。记住 `custom_signal_0` 这个位，4.2 节会看到它是 `SETRPC` 的关键。

#### 4.1.4 代码实践

**实践目标**：把「自定义 CSR 地址 ↔ 含义 ↔ 谁写谁读」三件事对应起来。

**操作步骤**：

1. 打开 [define.v:1218-1234](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L1218-L1234)，抄下 `0x800~0x80c` 这 13 个自定义 CSR 的地址与名字。
2. 打开 [csrfile.v:391-437](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/csr/csrfile.v#L391-L437)（派发参数锁存块）与 [csrfile.v:210-218](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/csr/csrfile.v#L210-L218)（输出块），判断每个 CSR 是「硬件派发时写入」还是「CSR 指令写入」，以及被哪个输出端口读走。
3. 整理成一张三列表格。

**预期结果**（节选）：

| CSR | 地址 | 谁写入 | 谁读取 |
| --- | --- | --- | --- |
| `CSR_WG_ID` | 0x804 | CTA 派发（`CTA2csr_valid`） | kernel 用 CSRRW 读，用于计算线程全局索引 |
| `CSR_PDS_BASEADDR` | 0x807 | CTA 派发 | `lsu_pds_o` → LSU 访存地址计算 |
| `CSR_RPC` | 0x80c | `SETRPC` 指令（经 `custom_signal_0`） | `simt_rpc_o` → simt_stack 作为分支目标 |
| `CSR_FRM` | 0x002 | CSRRW 指令 | `frm_o` → FPU 舍入模式 |

> 注意：`0x800~0x80b` 这一组绝大多数是**派发时硬件写入**、kernel 只读；而 `0x80c`(`RPC`) 是**指令写入**，这是它与众不同的地方，也是 `SETRPC` 存在的原因。

#### 4.1.5 小练习与答案

**练习 1**：`CSRRS x0, frm, x0`（源操作数为 0）的实际效果是什么？
**答案**：`CSR_S` 计算 `old | 0 = old`，即「不改变 frm，但能把旧值读到 x0」——然而 x0 永远为 0，所以这等价于一次纯读 `frm`。硬件上 `wen` 仍可能为真（`|ctrl_csr_i` 非零），但写入值与旧值相同，无副作用。

**练习 2**：为什么 `CSRRC` 的硬件实现是 `old & ~new` 而不是 `old & new`？
**答案**：`CSRRC`（Clear）的语义是「清掉 mask 中为 1 的那些位」，即 `new` 中为 1 的位要求结果为 0，故取反后相与：`old & ~new`。

---

### 4.2 csrfile：单 warp 的 CSR 存储与派发参数锁存

#### 4.2.1 概念说明

`csrfile` 是**单个 warp** 的 CSR 文件。它做三件事：

1. **读多路**：按 `csr_addr` 选出当前 CSR 的旧值 `csr_rdata`（组合逻辑）。
2. **写读改写**：按 `ctrl_csr_i`(W/S/C) 与输入值算出新值 `csr_wdata`，在 `wen` 有效时写进对应寄存器（时序逻辑）。
3. **锁存派发参数 + 多端口输出**：当 `CTA2csr_valid_i` 有效时，把 CTA 调度器送来的 `wg_id`、`pds_base`、`sgpr/vgpr base` 等锁存进对应 CSR；同时把这些值经 `sgpr_base_o`、`vgpr_base_o`、`lsu_pds_o`、`lsu_tid_o`、`lsu_numw_o`、`simt_rpc_o`、`frm_o` 等端口持续输出给消费者。

#### 4.2.2 核心流程

```
                  ┌──────────────── ctrl_inst_i[31:20] = csr_addr ──────────────┐
                  │                                                              │
   读：csr_addr ─► case ─► csr_rdata(旧值) ─────────────────────────────┐       │
                                                                        ▼       │
   写：ctrl_csr_i(W/S/C) ─► case ─► csr_wdata = 旧值 {直接/或/与~} 新值   │       │
                                                                        │       │
   wen = (|ctrl_csr_i) & write_i ──────────────────────────────────────► 写入对应 CSR
                                                                        │
   派发：CTA2csr_valid_i ─► 把 wg_id/pds_base/sgpr_base/... 锁存进 CSR（优先级高于 CSR 指令写）
                                                                        │
   输出：sgpr_base_o / vgpr_base_o / lsu_tid_o / lsu_pds_o / lsu_numw_o / simt_rpc_o / frm_o / wb_wxd_rd_o
```

一个关键细节：**`wen & ctrl_custom_signal_0_i`** 这条路径。当 `custom_signal_0` 为 1 时，写值不走「读改写」，而是**直接把输入写进 `rpc` 寄存器**，且写回给标量寄存器的值也是原始输入。这正是 `SETRPC` 指令的硬件落点（详见 4.4 节）。

#### 4.2.3 源码精读

**地址、写使能与写回值**（[csrfile.v:163-167](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/csr/csrfile.v#L163-L167)）：

```verilog
assign csr_addr = ctrl_inst_i[31:20];          // CSR 地址取自指令 31:20
assign csr_input = in1_i;                        // 写入源 = 操作数 in1
assign wdata = (wen & ctrl_custom_signal_0_i) ? csr_input :     // SETRPC: 直接写回原值
               ((wen & ctrl_isvec_i) ? ((csr_input < vlmax) ? csr_input : vlmax) : csr_rdata);
assign wen = (|ctrl_csr_i) & write_i;            // CSR_W/S/C 任一非零，且本拍允许写
```

`wdata` 是要回写给标量寄存器堆的「CSR 旧值」（RISC-V 的 CSR 指令总是把旧值写到 rd）。三种情况：`SETRPC`（custom）回写原输入；向量 `vsetvli` 类（isvec）把请求的 vl 限制在 `vlmax`；其余回写旧值 `csr_rdata`。

**读改写计算**（[csrfile.v:170-177](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/csr/csrfile.v#L170-L177)）：用 `ctrl_csr_i` 一个 2 位信号区分三种写语义。

```verilog
case(ctrl_csr_i)
  2'b01   : csr_wdata = csr_input;                 // CSR_W：直写
  2'b10   : csr_wdata = csr_rdata | csr_input;     // CSR_S：置位
  2'b11   : csr_wdata = csr_rdata & (~csr_input);  // CSR_C：清位
  default : csr_wdata = 'd0;
endcase
```

**读多路**（[csrfile.v:179-208](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/csr/csrfile.v#L179-L208)）：用一个大 `case(csr_addr)` 把每个 CSR 的当前值选出来。未列出的地址返回 0。

**派发参数锁存**（[csrfile.v:391-437](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/csr/csrfile.v#L391-L437)）：这是自定义 CSR 的「写入」入口——注意它由 `CTA2csr_valid_i` 触发，而不是 CSR 指令：

```verilog
else if(CTA2csr_valid_i) begin
  threadid  <= dispatch2cu_wf_tag_dispatch_i[`DEPTH_WARP-1:0] << `DEPTH_THREAD; // 线程基址
  wg_wf_count        <= dispatch2cu_wg_wf_count_i;
  knl_base           <= dispatch2cu_csr_knl_dispatch_i;
  wg_id              <= dispatch2cu_wg_id_i;
  pds_baseaddr       <= dispatch2cu_pds_base_dispatch_i;
  sgpr_base_dispatch <= dispatch2cu_sgpr_base_dispatch_i;
  vgpr_base_dispatch <= dispatch2cu_vgpr_base_dispatch_i;
  ...
end
```

> 这段说明：自定义 CSR 的「真值来源」是调度器。kernel 里读 `CSR_WG_ID` 拿到的，就是派发那一刻硬件写进去的 workgroup 编号。

**多端口输出**（[csrfile.v:210-218](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/csr/csrfile.v#L210-L218)）：

```verilog
assign wb_wxd_rd_o = wdata;                       // CSR 旧值回写标量寄存器堆
assign simt_rpc_o  = rpc;                          // RPC → simt_stack
assign sgpr_base_o = sgpr_base_dispatch;           // → operand_collector 标量基址
assign vgpr_base_o = vgpr_base_dispatch;           // → operand_collector 向量基址
assign frm_o       = frm;                          // → FPU 舍入模式
assign lsu_tid_o   = wf_tag_dispatch * `NUM_THREAD;// → LSU 线程基址
assign lsu_pds_o   = pds_baseaddr;                 // → LSU 参数栈基址
assign lsu_numw_o  = wg_wf_count;                  // → LSU workgroup 的 warp 数
```

其中 `lsu_tid_o = wf_tag_dispatch * NUM_THREAD`：每个 warp 含 `NUM_THREAD` 个线程，warp 的 tag 乘以线程数即得到该 warp 在全局线程空间中的起始 tid，LSU 用它计算每个 lane 的全局访存地址。

**RPC 写与普通 CSR 写的区别**（[csrfile.v:221-236](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/csr/csrfile.v#L221-L236)）：

```verilog
else if(wen & ctrl_custom_signal_0_i) begin
  rpc <= csr_input;              // SETRPC：直接写 rpc，忽略 csr_addr
end
else if(wen & csr_addr == `CSR_PRINT) begin
  csr_print <= csr_wdata;        // 普通 CSR 写：按地址选中目标
end
```

可见 `custom_signal_0` 把 `rpc` 提升为「有专用写指令」的 CSR——`SETRPC` 不靠 `csr_addr` 寻址，而靠这个特技位直接定位 `rpc`。

#### 4.2.4 代码实践

**实践目标**：搞清「向量 CSR `vtype` 为何是只读常量」。

**操作步骤**：

1. 阅读 [csrfile.v:475-508](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/csr/csrfile.v#L475-L508) 的 Vector CSR 块。
2. 观察 `vsew`、`vlmul`、`vlmax` 的赋值：它们**没有**出现在任何 `wen` 写分支里，而是在 `else`（每拍）被赋成常量。

**预期结果**：`vsew <= 3'd2`（仅支持 SEW=32 位）、`vlmul <= 'd0`（仅支持 LMUL=1）、`vlmax <= NUM_THREAD`。所以 Ventus 的向量 CSR 是「只读报告型」——读 `vtype`/`vl` 只会返回硬件固定的配置，写它没有效果。这印证了 u1-l3 提到的「Ventus 固定 FP32 / 固定 LMUL=1」。

**待本地验证**：你可以在测试程序里插入一条 `CSRRW x5, vtype, x0`，单步看 `x5` 是否等于 `{vill=0, vma=0, vta=0, vsew=3'd2, vlmul=0}` 组合出的值。

#### 4.2.5 小练习与答案

**练习 1**：若同一拍既有 `CTA2csr_valid_i=1`（正在派发新 warp），又有一条 CSR 写指令命中 `wg_id`，会发生什么？
**答案**：`wg_id` 等派发参数的写分支只响应 `CTA2csr_valid_i`，不响应 CSR 指令写（它们不出现在 `wen & csr_addr==...` 分支里）。所以派发优先，CSR 指令对这些自定义 CSR 写不进去；从软件视角，它们是「只读」的。

**练习 2**：`lsu_tid_o = wf_tag_dispatch * NUM_THREAD`，当 `NUM_THREAD=32`、`wf_tag=3` 时，该 warp 的起始 tid 是多少？为什么 LSU 需要它？
**答案**：起始 tid = 3 × 32 = 96。LSU 在做全局 load/store 时，要把 warp 内 lane 编号（0..31）映射成全局线程号，以计算每个线程各自要访问的地址，`lsu_tid_o` 给出了这个映射的偏移基准。

---

### 4.3 csrexe：每 warp 一份 CSR 与多消费者读出

#### 4.3.1 概念说明

`csrfile` 只是「一个 warp」的 CSR。而一个 SM 里有 `NUM_WARP` 个 warp 同时驻留，每个 warp 都有自己的 `wg_id`、`pds_base`、`frm`、`rpc`……所以 `csrexe` 用 `generate for` 把 `csrfile` 例化 `NUM_WARP` 次，构成一个「按 warp 分体」的 CSR 阵列。

除了例化，`csrexe` 还解决两个调度问题：

- **写选通**：CSR 指令带 `ctrl_wid_i`，只应写入选中 warp 的那份 csrfile，其余 warp 的实例不能被写。
- **派发选通**：CTA 派发带 `wid_i`（被派发的目标 warp 号），只应把派发参数写进目标 warp 的实例。
- **多消费者按 warp 读出**：LSU 需要「当前访存 warp」的 `pds/tid/numw`，FPU 需要「当前执行 warp」的 `frm`，simt_stack 需要「当前 SIMT warp」的 `rpc`——这三个 warp 号各不相同，`csrexe` 用三组选择信号分别多路读出。

#### 4.3.2 核心流程

```
                      ctrl_wid_i (CSR 指令的 warp)
                            │  write[i] = (i==ctrl_wid_i) & 握手
                            ▼
   NUM_WARP × csrfile  ◄── CTA2csr_valid[i] = (i==wid_i)  (派发选通，wid_i 来自 warpReq)
        │  ┌──────────────┼──────────────┬──────────────┐
        │  │ frm[i]       │ lsu_tid[i]   │ simt_rpc[i]  │  ... 每个实例各自输出
        │  └──────┬───────┴──────┬───────┴──────┬───────┘
        ▼         ▼              ▼              ▼
   按 wid 多路选：rm_o(frm[rm_wid])  lsu_*_[lsu_wid]   simt_rpc[simt_wid]
        │ sgpr_base/vgpr_base 是全 NUM_WARP 份拼接输出（每 warp 都要给 operand_collector）
```

注意 `sgpr_base_o` / `vgpr_base_o` 与其它输出不同：它们是**所有 warp 的基址拼接**在一起输出（`NUM_WARP*(ID_WIDTH+1)` 位），因为 operand_collector 要同时为多个 warp 的在途指令读寄存器；而 `frm`/`lsu_*`/`simt_rpc` 是**按当前执行 warp 号选一份**输出。

#### 4.3.3 源码精读

**generate 例化 + 写/派发选通**（[csrexe.v:118-170](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/csr/csrexe.v#L118-L170)）：

```verilog
for(i=0;i<`NUM_WARP;i=i+1) begin : B1
  csrfile U_vcsr_i ( ... );   // 每 warp 一个 csrfile
  // 只有被 ctrl_wid_i 选中的实例才允许 CSR 指令写
  assign write[i]         = (i == ctrl_wid_i) ? (in_valid_i & in_ready_o) : 1'b0;
  // 只有被 wid_i（派发目标）选中的实例才接收派发参数
  assign CTA2csr_valid[i] = (i == wid) ? CTA2csr_valid_i : 1'b0;
  // 基址按 warp 拼接输出
  assign vgpr_base_o[...] = vgpr_base[i];
  assign sgpr_base_o[...] = sgpr_base[i];
end
```

**按 warp 多路读出**（[csrexe.v:195-205](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/csr/csrexe.v#L195-L205)）：三组消费者各用各的 wid 索引到对应实例的输出。

```verilog
for(j=0;j<3;j=j+1)  // rm_o 是 3 组 3-bit，供最多 3 条在途浮点指令各自取 frm
  assign rm_o[((j+1)*3-1)-:3] = frm[rm_wid_i[...]];
assign lsu_tid_o  = lsu_tid [lsu_wid_i];   // LSU 当前 warp 的 tid 基址
assign lsu_pds_o  = lsu_pds [lsu_wid_i];   // LSU 当前 warp 的 pds 基址
assign lsu_numw_o = lsu_numw[lsu_wid_i];   // LSU 当前 warp 的 numw
assign simt_rpc_o = simt_rpc[simt_wid_i];  // simt_stack 当前 warp 的 RPC
```

> `rm_o` 有 9 位（3 组 × 3 位），对应浮点执行通路里最多 3 条在途指令各自的舍入模式，每组用 `rm_wid_i` 里对应的 wid 字段去选 `frm[wid]`。这与 u4-l4 讲的「FPU 三条在途」相呼应。

**输入反压与派发互斥**（[csrexe.v:206-216](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/csr/csrexe.v#L206-L216)）：

```verilog
assign in_ready_o = fifo_in_ready & (~CTA2csr_valid_i);  // 派发期间不接受 CSR 指令
```

派发正在写 CSR 的那一拍，CSR 指令的输入不能握手（`in_ready_o=0`），避免与派发写竞争同一批寄存器。输出侧用一个深度为 1 的 `stream_fifo_pipe_true` 缓冲 CSR 旧值与写回信息（`wb_wxd_rd`、`wxd`、`reg_idxw`、`warp_id`），切断组合路径并接续写回（[csrexe.v:217-230](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/csr/csrexe.v#L217-L230)）。

**在 pipe.v 中的连接**（[pipe.v:1822-1873](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1822-L1873)）：`CTA2csr_valid_i` 接到 `warpReq_valid_i`（即 cta2warp 派发有效），各 `dispatch2cu_*` 接到 warpReq 携带的派发参数，`wid_i` 接 `warpReq_wid_i`（被派发的本地 wid）；`lsu_wid_i`/`simt_wid_i`/`rm_wid_i` 分别由 LSU、operand_collector、FPU 回送的当前执行 warp 号驱动。这正是 u2-l3 所说「重数据在 sm_wrapper 旁路直达 pipe」在 CSR 侧的体现。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 warp 派发如何把参数写进「正确的那一份」csrfile。

**操作步骤**：

1. 在 [pipe.v:1842-1857](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1842-L1857) 找到 `csrexe` 的 `CTA2csr_valid_i` 与 `wid_i` 来源（`warpReq_valid_i` / `warpReq_wid_i`）。
2. 在 [csrexe.v:162-163](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/csr/csrexe.v#L162-L163) 确认只有 `i == wid` 的那个实例的 `CTA2csr_valid[i]` 为 1。
3. 顺着该实例进入 [csrfile.v:407-421](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/csr/csrfile.v#L407-L421)，看 `wg_id`/`pds_baseaddr`/`sgpr_base` 等被锁存。

**预期结果**：派发一个 wid=2 的 warp 时，只有 `U_vcsr_2` 的派发参数被更新，其余 7 个（假设 NUM_WARP=8）实例保持原值。这保证了不同 warp 的 CSR 互不干扰。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `sgpr_base_o`/`vgpr_base_o` 要把所有 warp 的基址「拼接」输出，而 `simt_rpc_o` 只输出一份？
**答案**：operand_collector 要为多个 warp 的在途指令同时读寄存器堆，需要「每 warp 一份」的基址来定位各自的寄存器窗口，故拼接输出全部；而 simt_stack 同一拍只处理一个 warp 的分支，按当前 `simt_wid` 选一份即可。

**练习 2**：`csrexe` 的输入反压 `in_ready_o = fifo_in_ready & (~CTA2csr_valid_i)` 去掉会怎样？
**答案**：若去掉，CSR 指令写可能在派发写同一拍发生，二者竞争同一组派发参数寄存器，可能导致刚派发的 `wg_id`/`pds_base` 被 CSR 指令的旧值覆盖（或反之），属于数据冒险。该反压是必需的互斥保护。

---

### 4.4 标量分支执行与 branch_back 汇聚

#### 4.4.1 概念说明

到这里 CSR 讲完了，但本讲还有另一半：**标量分支与跳转**（BEQ/BNE/BLT/BGE/JAL/JALR）。这些指令走 sALU（`aluexe`）执行，计算「是否跳 + 跳到哪」，再把结果送回 `warp_scheduler` 更新 PC。

但 SM 里有两类跳转源：

- **标量分支**（来自 sALU）：整个 warp 一起跳。
- **向量/SIMT 分支**（来自 simt_stack）：warp 内部线程发散后重新汇合时的 PC 控制（u5-l3 主题）。

二者都要改 `warp_scheduler` 的 PC，但 PC 同一拍只能接受一个。`branch_back` 就是这两路跳转的「仲裁器 + 二选一」。

关于 `SETRPC`：它本身**不是**分支指令，而是一条 CSR 写指令（写 `rpc`）。它的作用是**预设** simt_stack 在做 SIMT 跳转时用到的目标/返回地址（`rpc`）。也就是说：

- `SETRPC` → 写 `CSR_RPC`（`0x80c`）→ `simt_rpc_o` → 喂给 simt_stack；
- simt_stack 算出的跳转 → 进入 `branch_back` 的 `v_*`（向量）输入；
- 所以 `SETRPC` 是通过 simt_stack **间接**影响最终的 PC，而不是直接进 `branch_back`。

#### 4.4.2 核心流程

```
标量分支指令(B_B/B_J/B_R)
   │ issue 路由到 sALU
   ▼
aluexe(salu):  alu_cmp ─► jump_temp ──┐
               in3_i  ─► new_pc  ────┤ (带 1 拍 FIFO 缓冲)
   │                               s_valid/s_wid/s_jump/s_new_pc
   │                                       │
   │                       ┌───────────────┴──── branch_back ────┐
   │                       │   标量(s)优先 > 向量(v)              │
   simt_stack ─► v_valid/v_wid/v_jump/v_new_pc ──────────────────┘
                                                       │
                                              out_jump/out_wid/out_new_pc
                                                       │
                                                       ▼
                                     warp_scheduler.branch_*  ──► 更新该 warp 的 PC + flush
```

`branch_back` 的优先级很直白：**标量分支优先于 SIMT 分支**。当同一拍两者都有效时，标量获胜，SIMT 那一路的 `v_ready_o=0`（被反压），下拍重试。

#### 4.4.3 源码精读

**aluexe 计算跳转**（[aluexe.v:109-116](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/aluexe.v#L109-L116)）：用 `ctrl_branch_i` 区分三类。

```verilog
case(ctrl_branch_i)
 `B_B    : jump_temp = alu_cmp;   // 条件分支：跳不跳看 ALU 比较结果
 `B_J    : jump_temp = 1'b1;      // 无条件跳转(JAL)
 `B_R    : jump_temp = 1'b1;      // 寄存器跳转(JALR)
 default : jump_temp = 1'b0;
endcase
```

`alu_cmp` 来自 `alu` 内核的比较输出（[aluexe.v:70-79](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/aluexe.v#L70-L79)），由 `ctrl_alu_fn_i` 决定是 `SEQ/SNE/SLT/SGE...` 哪种比较（译码时 BEQ 填 `FN_SEQ`、BNE 填 `FN_SNE` 等，见 [decodeUnit.v:298-303](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L298-L303)）。

**分支目标与握手**（[aluexe.v:122-124](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/aluexe.v#L122-L134)）：把 `{wid, new_pc=in3_i, jump_temp}` 打包，过一个深度 2 的 `stream_fifo_pipe_true` 缓冲，再拆成 `br_wid_o/br_new_pc_o/br_jump_o`。`in3_i` 是 operand_collector 按 `sel_alu3`(BEQ/JAL/JALR 均为 `A3_PC`)与立即数选择算出的目标地址。

```verilog
assign result_br_data_in  = {ctrl_wid_i, in3_i, jump_temp};
assign result_br_in_valid = in_valid_i & (ctrl_branch_i != `B_N);  // 非分支指令不产生 br
...
assign br_new_pc_o = result_br_data_out[32:1];
assign br_jump_o   = result_br_data_out[0];
```

**branch_back 仲裁**（[branch_back.v:37-59](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/branch_back.v#L37-L59)）：纯组合，标量优先。

```verilog
assign s_ready_o = s_valid_i ? out_ready_i : 'h0;   // 标量有效时，下游 ready 全给它
assign v_ready_o = s_valid_i ? 'h0 : out_ready_i;   // 此时向量被挡住

always @(*) begin
  if(s_valid_i)      begin out_valid_o=s_valid_i; ... out_jump_o=s_jump_i;  out_new_pc_o=s_new_pc_i; end
  else if(v_valid_i) begin out_valid_o=v_valid_i; ... out_jump_o=v_jump_i;  out_new_pc_o=v_new_pc_i; end
  else               begin out_valid_o='h0; ... end
end
```

**在 pipe.v 中的接线**（[pipe.v:2061-2079](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L2061-L2079)）：`s_*` 来自 salu（`salu_out2br_*`），`v_*` 来自 simt_stack（`simt_stack_fetch_ctl_*`），`out_*` 送给 warp_scheduler（[pipe.v:927-930](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L927-L930)）。

```verilog
branch_back branch_back(
  .v_* (simt_stack_fetch_ctl_*),  // 向量分支源
  .s_* (salu_out2br_*),           // 标量分支源
  .out_* (branch_back_out_*)      // → warp_scheduler.branch_*
);
```

**SETRPC 的译码**（[decodeUnit.v:584](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L584)）：注意它的控制字里 `csr=CSR_W`、`wxd=Y`，且 `custom_signal_0[1]=Y`——后者是它区别于普通 CSRRW 的唯一标志。

```verilog
`SETRPC : ctrlSignals_0 = {...,`CSR_W,...,`Y(=wxd),...,`Y(=custom_signal_0),...};
```

`SETRPC` 的指令位模式定义在 [define.v:694](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L694)（`opcode=0x1b`，即 custom-0 空间）。结合 4.2 节：译码置 `custom_signal_0=1` → `csrfile` 把 `in1` 直接写入 `rpc` → `simt_rpc_o` → simt_stack 用作 SIMT 跳转目标。这就是「`SETRPC` 如何影响分支目标」的完整答案：**它预设 SIMT 分支的目标地址，最终经 simt_stack → branch_back → warp_scheduler 改写 PC**。

#### 4.4.4 代码实践

**实践目标**：用仿真验证 `SETRPC` 写入的 `rpc` 是否真的能被 `simt_rpc_o` 读到，并观察一次标量分支如何更新 PC。

**操作步骤（源码阅读型 + 可选仿真）**：

1. **静态跟踪 SETRPC**：
   - 在 [decodeUnit.v:584](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L584) 确认 `SETRPC` 置 `custom_signal_0=Y`、`csr=CSR_W`。
   - 在 [csrfile.v:226-228](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/csr/csrfile.v#L226-L228) 确认 `wen & custom_signal_0` 时 `rpc <= csr_input`。
   - 在 [csrfile.v:212](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/csr/csrfile.v#L212) 确认 `simt_rpc_o = rpc`，再到 [csrexe.v:205](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/csr/csrexe.v#L205) 确认按 `simt_wid` 选出。
2. **静态跟踪标量分支**：在 [aluexe.v:109-134](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/aluexe.v#L109-L134) 看 `jump_temp`/`new_pc` 如何打包；在 [branch_back.v:40-59](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/branch_back.v#L40-L59) 看仲裁；在 [pipe.v:927-930](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L927-L930) 看结果回到 warp_scheduler。
3. **（可选）仿真观察**：按 u1-l4 的方法跑一个含循环（`tc_gaussian` 或 `tc_vecadd`）的用例（`make run-vcs-4w4t`），用 Verdi 打开 `test.fsdb`，在 `branch_back.out_jump_o` / `out_new_pc_o` 上看到脉冲，并在 `csrfile.rpc`（wid=0 实例）上确认 `SETRPC` 写入的值。

**需要观察的现象**：
- 每次循环跳转，`branch_back_out_jump` 出现一个周期的高电平，`branch_back_out_new_pc` 等于循环头地址。
- `SETRPC` 执行后，对应 wid 的 `rpc` 寄存器值变为立即数/寄存器传入值。

**预期结果**：标量分支经 `branch_back` → `warp_scheduler` 改 PC；`SETRPC` 写入的 `rpc` 出现在 `simt_rpc_o` 上供 simt_stack 使用。

**待本地验证**：若手头暂无含 `SETRPC` 的程序，步骤 1~2 的静态跟踪已足以确认数据通路；步骤 3 的波形现象标注为「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`JAL` 和 `BEQ` 在 `aluexe` 里的 `jump_temp` 有何不同？`alu_cmp` 又由什么决定？
**答案**：`JAL` 是 `B_J`，`jump_temp` 恒为 1（无条件跳）；`BEQ` 是 `B_B`，`jump_temp = alu_cmp`，只有比较成立才跳。`alu_cmp` 由 `ctrl_alu_fn_i` 决定——译码时 BEQ 填 `FN_SEQ`（相等则 cmp=1）、BNE 填 `FN_SNE` 等。

**练习 2**：同一拍 sALU 有一条 JAL、simt_stack 也有一个汇合跳转，`branch_back` 会怎么处理？
**答案**：标量优先，JAL 获胜，`out_*` 输出 JAL 的 wid/jump/new_pc；simt_stack 那一路 `v_ready_o=0` 被反压，需等到下一拍 sALU 无效时再发。

**练习 3**：`SETRPC` 写 `rpc` 为什么不直接进 `branch_back`，而要绕道 simt_stack？
**答案**：`rpc` 是为 SIMT 栈的「预设目标/返回地址」服务的，属于 warp 内线程发散控制的一部分；`branch_back` 只是「两路跳转的二选一」，不产生跳转目标。`SETRPC` 写入 `rpc` → simt_stack 在需要时用它生成 `v_new_pc` → 再经 `branch_back` 才到 `warp_scheduler`。所以 `SETRPC` 是「装填」，不是「扣扳机」。

## 5. 综合实践

把本讲四块知识串起来，完成下面这张「CSR + 分支全链路」追踪任务：

**背景**：一个 warp 被 CTA 调度器派发到 SM，开始执行一段 kernel。kernel 开头有一条 `SETRPC`（设置 SIMT 返回点），中间有循环用 `BEQ`/`BNE` 跳转，循环体里有一条 `CSRRW x5, wg_id, x0`（读 workgroup 编号）。

**任务**：

1. **派发 → CSR 锁存**：从 [pipe.v:1842-1857](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1842-L1857) 出发，画出 `warpReq_valid_i` → `csrexe.CTA2csr_valid_i` → 某 wid 实例 `csrfile` 锁存 `wg_id`/`pds_base` 的路径，并说明为什么只有目标 wid 的实例被写（提示：[csrexe.v:163](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/csr/csrexe.v#L163)）。
2. **SETRPC → rpc**：说明 `SETRPC` 因 `custom_signal_0=1` 走专用写路径（[csrfile.v:226-228](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/csr/csrfile.v#L226-L228)）写入 `rpc`，再经 `simt_rpc_o` 送到 simt_stack。
3. **CSRRW 读 wg_id**：说明 `CSRRW` 因 `csr=CSR_W`、`custom_signal_0=0`，走读多路（[csrfile.v:179-208](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/csr/csrfile.v#L179-L208)）取出 `wg_id` 旧值，经 `wb_wxd_rd_o` 写回标量寄存器 `x5`。
4. **BEQ 改 PC**：说明 `BEQ` 经 sALU 算出 `alu_cmp`/`new_pc`（[aluexe.v:109-134](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/aluexe.v#L109-L134)），经 `branch_back`（标量优先）回送 `warp_scheduler` 更新 PC。

**交付物**：一张标注了模块名、信号名、关键行号的调用链草图，以及 3~5 行文字说明「哪些 CSR 是派发写、哪些是指令写、各自给谁读」。

> 提示：这条链路恰好覆盖了本讲全部四个最小模块（自定义 CSR / csrfile / csrexe / branch_back），也呼应了 u2-l3「重数据旁路直达 pipe」与 u3-l1「分支经 branch_back 闭环回取指」两条结论。

## 6. 本讲小结

- Ventus 的 CSR 分三类：标准机器态（异常用）、浮点（`frm` 实时分发给 FPU）、**Ventus 自定义 `0x800~0x80c`**（多为派发时硬件写入的身份/基地址）。
- `csrfile` 用一个 2 位 `ctrl_csr_i` 实现读改写三型（W 直写 / S 置位 / C 清位），读是组合、写是时序；`custom_signal_0` 位为 `rpc` 开了「专用直写」旁路，这是 `SETRPC` 的硬件落点。
- 自定义 CSR 的「谁写谁读」规律：`0x800~0x80b` 多由 CTA 派发写、kernel 只读（如 `wg_id`→软件、`pds_base`→LSU、`sgpr/vgpr base`→operand_collector）；`0x80c`(`rpc`) 由 `SETRPC` 指令写、simt_stack 读。
- `csrexe` 用 `generate for` 例化 `NUM_WARP` 份 `csrfile`，靠 `ctrl_wid_i` 选通写、靠 `wid_i` 选通派发，并按 `lsu_wid/simt_wid/rm_wid` 多路读出给不同消费者；`sgpr/vgpr_base` 则全 warp 拼接输出。
- 标量分支（BEQ/BNE/JAL/JALR）走 sALU：`alu_cmp`+`in3` 算出 `jump/new_pc`，经 `branch_back` 与 simt_stack 的向量分支做「标量优先」二选一，回送 `warp_scheduler` 改 PC。
- `SETRPC` 不直接分支，而是**预设** simt_stack 的目标地址（写 `rpc`），最终经 simt_stack → `branch_back` → `warp_scheduler` 间接改 PC。

## 7. 下一步学习建议

- **u5-l3（SIMT 栈与分支发散）**：本讲反复提到的 simt_stack 是下一讲的主角。学完后你会明白 `SETRPC` 写入的 `rpc` 在 JOIN/VBEQ 发散汇合时如何被用、`v_jump/v_new_pc` 如何产生，以及 `branch_back` 的 `v_*` 输入完整语义。
- **u5-l1（LSU）回顾**：现在你已知道 `lsu_tid_o/lsu_pds_o/lsu_numw_o` 来自 csrfile，可回头对照 LSU 如何用它们算每个 lane 的全局访存地址。
- **u4-l4（FPU）回顾**：`frm_o` 的 9 位（3 组）输出对应 FPU 三条在途指令的舍入模式，可结合 u4-l4 的「舍入模式三选一」再理解一遍。
- **源码延伸**：若对异常处理感兴趣，可继续精读 [csrfile.v:238-388](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/csr/csrfile.v#L238-L388) 的 `mstatus/mip/mie/mepc/mcause` 实现，理解 Ventus 对机器态 CSR 的精简支持。
