# SM 流水线总览 pipe.v

> 前置讲义：本讲承接 **u2-l3（cta2warp 与 Warp 派发接口）** 与 **u1-l5（顶层模块 GPGPU_top）**。你已经知道：CTA 调度器把一个 workgroup 拆成若干 warp，经 `cta2warp` 翻译成 warp 级请求（`warpReq`/`warpRsp`）送进 SM 核；SM 核的顶层壳是 `sm_wrapper`，而真正干活的流水线就是这个 `pipe` 模块。

## 1. 本讲目标

学完本讲，你应当能够：

- 画出 SM 核流水线「取指 → 译码 → ibuffer → 发射 → 操作数采集 → 执行 → 写回」的整体数据通路图。
- 打开 `pipe.v` 后，能逐一指认它例化了哪些子模块、各自在第几行、彼此用什么信号相连。
- 说清楚三类存储请求（icache / dcache / shared_mem）分别从流水线的哪个出口离开 `pipe`。
- 理解 warp 级调度、flush（冲刷）、scoreboard 冒险反馈在整条流水线中的位置。

本讲是 **总览**：只讲「全景与连线」，每个子模块的内部细节（icache 怎么查 tag、LSU 怎么算地址、scoreboard 怎么判冒险）留给后续讲义。本讲好比给你一张「SM 流水线地铁线路图」，先知道有哪些站、怎么换乘，再决定下一站深入哪条线。

## 2. 前置知识

在开始前，用最通俗的话复习几个概念：

- **流水线（pipeline）**：把一条指令的执行切成若干「段」（取指、译码、执行、写回……），不同指令的不同段像工厂流水线一样重叠进行，以提高吞吐率。
- **warp / lane / thread**：在 Ventus GPGPU 里，一条向量指令广播给整个 warp，warp 内 `NUM_THREAD` 个线程（= `NUM_LANE` 个 lane）并行执行同一条指令（SIMT）。这是 GPU 与 CPU 单发射最大的区别。
- **取指（fetch）**：按 PC（程序计数器）从指令存储器取回 32 位指令字。
- **译码（decode）**：把 32 位机器码翻译成一组控制信号（做什么运算、读哪几个寄存器、写哪个寄存器……）。
- **发射（issue）**：判断一条已译码指令「现在能不能开始执行」，并把它送到对应的执行单元。
- **冒险（hazard）**：后续指令依赖前面尚未完成的结果（先写后读 RAW），必须等待，否则会读到旧值。
- **写回（writeback）**：把执行结果写回寄存器堆。
- **握手（ready/valid）**：组件间用 `valid`（我有数据）+ `ready`（我能收）成对信号传数据，双方都为 1 才算「成交」（fire）。

一个关键规模常识（来自 [`define.v`](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v)）：`NUM_FETCH=2`（一次取 2 条指令）、`NUM_ISSUE=1`（单发射）、`SIZE_IBUFFER=2`、`NUM_COLLECTORUNIT=NUM_WARP`（每个 warp 一个操作数采集单元）、`NUMBER_ALU=NUMBER_MUL=NUMBER_FPU=NUM_THREAD`（向量执行单元满 lane 宽度）。这些数字会在后面反复用到。

## 3. 本讲源码地图

本讲主要涉及三个文件：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [src/gpgpu_top/sm/pipeline/pipe.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v) | SM 核流水线主体（2118 行） | 它例化了哪些子模块、如何连线 |
| [src/gpgpu_top/sm/sm_wrapper.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v) | SM 核的顶层「壳」 | `pipe` 如何与 icache/dcache/shared_mem 对接 |
| [src/define/define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v) | 全局参数定义 | 流水线相关的规模参数 |

`pipe` 内部例化的子模块（按源码出现顺序）一览，这是本讲的「路线图」：

| 行号 | 实例名（模块名） | 角色 |
|------|------------------|------|
| 897 | `warp_sche`（warp_scheduler） | warp 调度 + 取指驱动 + PC 管理 |
| 948 | `decode`（decodeUnit） | 译码（一次 2 条指令） |
| 1055 | `ibuffer`（ibuffer） | 指令缓冲（按 warp 组织） |
| 1168 | `ibuffer2issue` | 从 ibuffer 选一条指令送下游 |
| 1280 | `scoreb`（scoreboard，每 warp 一个） | 冒险检测 |
| 1340 | `operand_collector`（operandcollector_top） | 操作数采集 + 寄存器堆 |
| 1461 | `issue` | 发射/路由到各执行单元 |
| 1640 | `alu`（valu_top） | 向量 ALU |
| 1679 | `lsu`（lsu_exe） | 访存单元 |
| 1792 | `salu`（aluexe） | 标量 ALU + 分支 |
| 1822 | `csrfile`（csrexe） | CSR 执行 |
| 1875 | `simt_stack` | SIMT 分支发散栈 |
| 1908 | `sfu`（sfu_exe） | 特殊功能单元 |
| 1945 | `mul`（vmul_top） | 向量乘法 |
| 1984 | `tensorcore`（tensor_core_exe） | 张量核 |
| 2017 | `fpu`（fpuexe） | 浮点单元 |
| 2061 | `branch_back` | 分支结果汇总 |
| 2081 | `wb`（writeback） | 写回仲裁 |

---

## 4. 核心概念与源码讲解

### 4.1 pipe 模块：流水线容器与端口全景

#### 4.1.1 概念说明

`pipe` 是 SM 核流水线的「集装箱」：它本身几乎不做运算，只负责**例化上面那一长串子模块并把它们用线连起来**。理解 `pipe` 的关键是先看懂它的「对外接口」（端口），因为这些端口就是流水线与外部世界（指令存储、数据存储、共享内存、CTA 调度器）的边界。

`pipe` 的端口可以分为五组：

1. **icache 取指接口**（出请求、入响应）——离开 `pipe` 去 `instruction_cache`。
2. **dcache 数据访存接口**——离开 `pipe` 去 `l1_dcache`，由 LSU 驱动。
3. **shared_mem 共享内存接口**——离开 `pipe` 去 `shared_mem`（LDS），也由 LSU 驱动。
4. **warpReq / warpRsp**——与 `cta2warp` 对接，接收新 warp、回报 warp 完成。
5. **杂项控制**：`flush_pipe_valid/wid`（冲刷流水线）、`wg_id_lookup/tag`（栅栏 barrier 支持）、`lsu_mshr_is_empty_o`（LSU 是否空闲，供缓存无效化判断）。

#### 4.1.2 核心流程

从最宏观看，一条指令在 `pipe` 里走过的路径是：

```
        warpReq (新warp进入)
            │
            ▼
     ┌─────────────┐  icache_req   ───► [instruction_cache] (在 sm_wrapper 里)
     │ warp_sche   │ ◄── icache_rsp ───
     │ (PC管理/取指)│
     └──────┬──────┘
            │ icache_rsp_data (取回的指令字)
            ▼
       decodeUnit ──► ibuffer ──► ibuffer2issue
            │                          │
            │                     scoreboard(每warp) ──delay──┐
            │                          │                     │ 反压
            │                          ▼                     │
            │                  operand_collector ◄───────────┘
            │                 (读寄存器堆/生成立即数)
            │                          │
            │                          ▼
            │                       issue (路由)
            │                          │
            │     ┌────────┬──────┬─────┴────┬────────┬─────────┐
            │     ▼        ▼      ▼          ▼        ▼         ▼
            │   valu     vmul   fpu        lsu      salu      tensor ...
            │     │        │      │          │        │
            │     └────────┴──────┴──────────┴────────┴──► writeback
            │                                              │
            └──────────────────────────────────────────────┘
                                  ▼
                          operand_collector (写回寄存器堆)
```

三个「存储出口」要特别记住：**icache 出口**由 `warp_sche` 驱动；**dcache 出口**和 **shared_mem 出口**都由 `lsu` 驱动。这是后续单元 6（存储子系统）的入口。

#### 4.1.3 源码精读

`pipe` 的端口定义见 [pipe.v:20-102](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L20-L102)。截取关键的三类存储接口：

```verilog
// icache 取指接口（pipe.v:26-36）
output icache_req_valid_o, output [`XLEN-1:0] icache_req_addr_o,
input  icache_rsp_valid_i, input [`NUM_FETCH*`XLEN-1:0] icache_rsp_data_i,
input  icache_rsp_status_i, // 0 is hit, 1 is miss

// dcache 数据访存接口（pipe.v:38-54）
output dcache_req_valid_o, output [`NUM_THREAD-1:0] dcache_req_activemask_o,
output [2:0] dcache_req_opcode_o,

// shared_mem 共享内存接口（pipe.v:56-71）
output shared_req_valid_o, output shared_req_iswrite_o,
input  shared_rsp_valid_i, input [`XLEN*`NUM_THREAD-1:0] shared_rsp_data_i,
```

> 说明：`icache_rsp_status_i`（0=命中、1=缺失）是取指的关键状态位；`dcache_req_activemask_o` 是 `NUM_THREAD` 位的活跃掩码，标记 warp 内哪些线程真正参与这次访存（SIMT 掩码）；`shared_*` 用同样的掩码语义。

`pipe` 与外部的最终衔接在 `sm_wrapper` 里完成。`sm_wrapper` 例化了 `cta2warp`、`pipe`、`l1cache_arb`、`instruction_cache`、`shared_mem`、`l1_dcache`，见 [sm_wrapper.v:430-558](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v#L430-L558)。其中 `pipe` 的 icache/dcache 请求经 `l1cache_arb` 仲裁后统一从 SM 对外接口出去，而 `shared_mem` 是 SM **内部**存储（不对外）。

#### 4.1.4 代码实践

- **实践目标**：建立「端口 ↔ 外部模块」的对应关系。
- **操作步骤**：
  1. 打开 [pipe.v:20-102](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L20-L102)，把端口分成 icache / dcache / shared / warpReq-Rsp / 杂项 五类。
  2. 再打开 [sm_wrapper.v:465-558](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v#L465-L558)，对照确认：`pipe_icache_req_*` 连到 `instruction_cache`；`pipe_dcache_req_*` 连到 `l1_dcache`；`pipe_shared_req_*` 连到 `shared_mem`。
- **需要观察的现象**：三类存储请求分别由哪个 `pipe` 内部模块驱动（见 4.5 的源码）。
- **预期结果**：你会看到 icache 请求由 `warp_sche` 驱动，dcache 与 shared 请求都由 `lsu` 驱动。
- 运行结果：待本地验证（本实践为源码阅读型，无需仿真）。

#### 4.1.5 小练习与答案

**练习 1**：`pipe` 模块本身有 `always` 时序逻辑吗？它主要承担什么职责？
**答案**：`pipe` 几乎不含运算逻辑，主要是线声明（`wire`）、`assign` 拼接与子模块例化。它承担「连线集装箱」职责，把各子模块组装成一条流水线。

**练习 2**：为什么 `dcache_req_activemask_o` 是 `NUM_THREAD` 位宽，而不是 1 位？
**答案**：因为向量访存按 lane 进行，warp 内不同线程可能各自访问不同地址、各自有效或无效（活跃掩码），需要逐 lane 标记。

---

### 4.2 取指译码前端：warp_scheduler / decodeUnit / ibuffer

#### 4.2.1 概念说明

流水线的「前端」负责把指令从存储器搬进来、翻译成控制信号、暂存起来等待发射。Ventus 的前端有三个角色：

- **warp_scheduler（warp_sche）**：每个 warp 维护自己的 PC。它接收新 warp（`warpReq`），按 PC 发取指请求；收到分支跳转结果后更新 PC；当 warp 执行结束发出 `warpRsp`。它是前端的「总指挥」。
- **decodeUnit（decode）**：把取指返回的 32 位机器码翻译成几十个控制信号（操作类型、源/目的寄存器选择、立即数类型、是否访存、是否浮点……）。一次译码 2 条指令（`NUM_FETCH=2`）。
- **ibuffer**：译码后的指令不能立刻执行（要等操作数、要等执行单元），所以先按 warp 暂存进指令缓冲（深度 `SIZE_IBUFFER=2`）。

#### 4.2.2 核心流程

取指返回的数据（`icache_rsp`）同时被三处消费，这是理解前端的关键：

1. `warp_scheduler` 用它更新 PC、记录命中/缺失状态。
2. `decodeUnit` 把指令字译成控制信号。
3. `ibuffer` 在「命中」时把译码结果存起来。

当 icache 缺失（`status=1`），指令无效，不译码、不入 ibuffer，`warp_scheduler` 会在缺失处理完后重新取指。当 ibuffer 满，则通过把状态「伪装成缺失」来反压 warp_scheduler 停止喂数据。

#### 4.2.3 源码精读

`pipe` 里这段「胶水逻辑」把取指、译码、ibuffer 缝在一起，见 [pipe.v:706-715](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L706-L715)：

```verilog
assign {decode_in_inst1,decode_in_inst0} = icache_rsp_data_i;        // 64位拆成2条32位指令
assign warp_sche_status = ibuffer_in_ready ? icache_rsp_status_i : 1'b1; // ibuffer满→伪装成miss
assign {decode_inst_mask_1,decode_inst_mask_0} =
       (icache_rsp_valid_i && (!icache_rsp_status_i)) ? icache_rsp_mask_i : 'b0; // 命中才有效
assign ibuffer_in_valid = icache_rsp_valid_i && (!icache_rsp_status_i);         // 命中才入队
assign ibuffer_in_control_mask = {decode_control_mask_1,decode_control_mask_0};
```

> 这段是本讲的「彩蛋」：第 710 行的 `ibuffer_in_ready ? ... : 1'b1` 是一处巧妙反压——当 ibuffer 没空接收时，直接告诉 warp_scheduler「这次取指算 miss」，于是 warp_scheduler 不会推进 PC，从而避免溢出。

三个子模块的例化：

- `warp_scheduler` 例化见 [pipe.v:897-946](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L897-L946)。注意它的 `pc_req_*` 输出就是 `pipe` 的 `icache_req_*`；它的 `scoreboard_busy_i` 接 `scoreb_delay`，`ibuffer_ready_i` 接 `ibuffer_ready`——也就是说前端的总指挥会被「下游是否就绪」反向控制。
- `decodeUnit` 例化见 [pipe.v:948-1053](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L948-L1053)，输入 `inst_0_i/inst_1_i`、`pc_i`、`wid_i`，输出大量 `control_Signals_*`。
- `ibuffer` 例化见 [pipe.v:1055-1166](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1055-L1166)，参数 `SIZE_IBUFFER`、`NUM_FETCH` 来自 `define.v`，还接收 `warp_sche_flush_*` 做冲刷。

#### 4.2.4 代码实践

- **实践目标**：追踪一条指令从前端进入的全过程。
- **操作步骤**：
  1. 在 [pipe.v:706](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L706) 确认 `icache_rsp_data_i`（64 位）被拆成 `decode_in_inst0`（低 32）和 `decode_in_inst1`（高 32）。
  2. 顺着 `decode_in_inst0` 到 [pipe.v:951](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L951) 进入 `decodeUnit`，挑一个控制信号（如 `control_Signals_alu_fn_0_o`）一直跟到 `ibuffer` 的同名输入。
  3. 在 [pipe.v:714](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L714) 确认只有命中（`!icache_rsp_status_i`）时 `ibuffer_in_valid` 才为 1。
- **需要观察的现象**：当 `icache_rsp_status_i=1`（缺失）时，`ibuffer_in_valid` 和 `decode_inst_mask_*` 的取值。
- **预期结果**：两者均为 0，即缺失时既不译码有效指令也不入队。
- 运行结果：待本地验证（源码阅读型）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `decodeUnit` 一次译码 2 条指令？
**答案**：因为 `NUM_FETCH=2`，一次取指就带回 2 条指令（64 位数据），译码器并行处理这两条以提高前端吞吐。

**练习 2**：ibuffer 满时，warp_scheduler 是如何被「叫停」的？
**答案**：`pipe.v:710` 把传给 warp_scheduler 的 `pc_rsp_status_i` 在 `ibuffer_in_ready=0` 时强制为 `1'b1`（miss），warp_scheduler 见到 miss 就不推进 PC，实现反压。

---

### 4.3 冒险检测 scoreboard 与 warp 调度反馈

#### 4.3.1 概念说明

「发射」之前必须回答一个问题：这条指令现在能进执行单元吗？最大的障碍是**数据冒险**——比如指令 B 要读寄存器 R5，而前一条指令 A 还没把结果写回 R5，B 就必须等。`scoreboard`（记分板）就是干这个的：它用一张「位图」记录每个寄存器是否「有待完成的写」，发射前查一下，命中就拉高 `delay`（延迟）信号阻止该 warp 的指令继续往前走。

Ventus 给**每个 warp 配一个独立的 scoreboard**（`generate for NUM_WARP`），因为各 warp 之间互不干扰，只有同一 warp 内的指令才有依赖关系。

#### 4.3.2 核心流程

scoreboard 的工作可以抽象为：

```
某warp指令进入ibuffer2issue:
   if (它要读的寄存器 在 vectorReg/scalarReg 位图里被置1)
   或 (它在等待 branch / fence / operand_collector 忙):
        delay_o = 1   → warp_scheduler 停止推进该warp
   else:
        delay_o = 0   → 允许前进

位图维护:
   一条指令被「发射」(if_fire) 且会写某向量寄存器 → 该寄存器位置1
   该寄存器「写回」完成(wb_fire)              → 该寄存器位清0
```

注意 `delay_o` 是**回送给 warp_scheduler**（`scoreboard_busy_i`）的，而不是回送给 ibuffer。也就是说：冒险检测的结果最终体现在「warp_scheduler 不再给这个 warp 取新指令/推进」上。这就是「warp 级调度反馈」在流水线里的落点。

#### 4.3.3 源码精读

`pipe` 用 `generate` 给每个 warp 例化一个 scoreboard，见 [pipe.v:1270-1338](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1270-L1338)：

```verilog
generate for(i=0;i<`NUM_WARP;i=i+1) begin:B1
  scoreboard scoreb(
    ...
    .if_fire_i            (scoreb_if_fire[i]),   // 该warp指令前进
    .wb_v_fire_i          (scoreb_wb_v_fire[i]), // 向量写回完成
    .wb_x_fire_i          (scoreb_wb_x_fire[i]), // 标量写回完成
    .br_ctrl_i            (scoreb_br_ctrl[i]),   // 分支/控制冲刷
    .delay_o              (scoreb_delay[i])      // 冒险信号→warp_scheduler
  );
end endgenerate
```

scoreboard 内部位图与 `delay_o` 的产生见 [scoreboard.v:64-93](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/scoreboard.v#L64-L93)（以 `vectorReg` 为例）：

```verilog
reg [(1<<(`REGIDX_WIDTH+`REGEXT_WIDTH))-1:0] vectorReg;  // 每个向量寄存器一位
// 发射且写该寄存器→置1；写回完成→清0
vectorReg[j] <= (if_fire_i && if_wvd_i && (j==if_reg_idxw_i)) ? 1'b1 :
                ((wb_v_fire_i && wb_v_wvd_i && (j==wb_v_reg_idxw_i)) ? 1'b0 : vectorReg[j]);
...
assign delay_o = read_rs1|read_rs2|read_rs3|read_mask|read_wb|read_beq|read_opcol|read_fence;
```

> 说明：`delay_o` 是多种冲突（读源寄存器 rs1/rs2/rs3、读掩码、写后读、分支 beq、operand_collector 忙、fence 未完成）的「或」结果，任一成立就阻塞。`scoreb_delay` 数组又反馈到 [pipe.v:937](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L937) 的 `warp_scheduler.scoreboard_busy_i`。

#### 4.3.4 代码实践

- **实践目标**：理解 scoreboard 位图的置位/清零时机。
- **操作步骤**：
  1. 打开 [scoreboard.v:83-93](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/scoreboard.v#L83-L93)，看清 `vectorReg[j]` 在什么条件下变 1、变 0。
  2. 回到 [pipe.v:1271-1275](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1271-L1275)，看 `scoreb_if_fire[i]`、`scoreb_wb_v_fire[i]` 是怎么按 wid 选通的。
- **需要观察的现象**：构造一个 RAW 序列（A 写 R5，B 紧接着读 R5），思考 A 发射后 `vectorReg[5]` 何时变 1、B 为何被 `delay`。
- **预期结果**：A 发射时 `if_fire && if_wvd && reg_idxw==5` → `vectorReg[5]=1`；B 读 rs 且命中该位 → `read_rs*=1` → `delay_o=1`；A 写回完成 `wb_v_fire` → `vectorReg[5]=0` → B 解除阻塞。
- 运行结果：待本地验证（源码阅读型；细节在 u3-l4 详讲）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 scoreboard 要「每 warp 一个」，而不是全 SM 共用一个？
**答案**：不同 warp 之间没有数据依赖（各自独立的寄存器空间），只有同一 warp 内的指令才有 RAW 关系。每 warp 独立 scoreboard 既正确又简单。

**练习 2**：`delay_o` 最终影响的是哪个模块的什么行为？
**答案**：影响 `warp_scheduler`：当某 warp 的 `scoreb_delay` 为 1，warp_scheduler 不再推进该 warp 的取指/发射，从而等待冒险消除。

---

### 4.4 操作数采集 operand_collector

#### 4.4.1 概念说明

指令要运算，必须先拿到操作数（源寄存器的值、立即数）。在 GPU 里这是件「大事」：一条向量指令可能要为 `NUM_THREAD` 个 lane 各读 2~3 个向量寄存器，数据量巨大。`operand_collector`（操作数采集器）专门负责：

- 从**标量寄存器堆（SGPR）**和**向量寄存器堆（VGPR）**读出操作数。
- 根据 `sel_imm` 选择**生成立即数**（gen_imm）。
- 把采集好的操作数（`alu_src1/2/3`）连同控制信号一起打包送给 `issue`。
- 同时它也是**写回目标**：执行结果经 `writeback` 后，由 operand_collector 写回寄存器堆。

Ventus 采用「每 warp 一个采集单元（collector unit）」的设计（`NUM_COLLECTORUNIT=NUM_WARP`），让多个 warp 的操作数采集可以重叠进行。

#### 4.4.2 核心流程

```
ibuffer2issue 送出一条(已译码)指令:
        │  (reg_idx1/2/3=源寄存器号, sel_alu*=操作数选择, sel_imm=立即数类型)
        ▼
  operand_collector
   ├─ 用 reg_idx 经 operand_arbiter 仲裁 → 读 scalar/vector_regfile_bank
   ├─ gen_imm 按 sel_imm 生成立即数
   ├─ 按 sel_alu1/2/3 在 {寄存器值, 立即数, PC, ...} 中选出 alu_src1/2/3
   └─ 打包 {alu_src1/2/3, 全部控制信号} → issue
        ▲
        │ writeScalar/writeVector (写回)
   writeback 结果 → 写回寄存器堆
```

#### 4.4.3 源码精读

`pipe` 中 operand_collector 的例化见 [pipe.v:1340-1460](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1340-L1460)。它的输入来自 `ibuffer2issue`（经一拍寄存器 `reg_ibuffer2issue_*`），输出 `operand_collector_out_*` 喂给 `issue`。关键连接：

```verilog
operandcollector_top operand_collector(
  .in_valid_i     (reg_ibuffer2issue_out_valid),        // 来自ibuffer2issue(打一拍)
  .in_reg_idx1_i  (reg_ibuffer2issue_warps_control_Signals_reg_idx1), // 源寄存器1
  .in_sel_imm_i   (reg_ibuffer2issue_warps_control_Signals_sel_imm),  // 立即数类型
  ...
  .writeScalar_valid_i (wb_out_x_valid),   // 标量写回入口
  .writeVector_valid_i (wb_out_v_valid),   // 向量写回入口
  ...
);
```

> 说明：注意 `in_*` 用的是 `reg_ibuffer2issue_*`（带 `reg_` 前缀），说明 ibuffer2issue 的输出在这里先打了一拍寄存器再进采集器——这是流水线插寄存器对齐时序的常见手法。

operand_collector 内部结构（collector_unit / operand_arbiter / scalar_regfile_bank / vector_regfile_bank / gen_imm）属于本讲的「下一层」，将在 **u4-l1** 精读。这里只需记住它在流水线里的位置：**夹在 ibuffer2issue 与 issue 之间，是寄存器堆的门户**。

#### 4.4.4 代码实践

- **实践目标**：定位 operand_collector 的「读入口」与「写回入口」。
- **操作步骤**：
  1. 在 [pipe.v:1344-1390](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1344-L1390) 找到所有 `in_*` 输入（读操作数）。
  2. 在 [pipe.v:1391-1400](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1391-L1400) 找到 `writeScalar_*` / `writeVector_*`（写回入口）。
  3. 确认写回数据源是 `wb_out_x_*` / `wb_out_v_*`（来自 writeback 模块）。
- **需要观察的现象**：operand_collector 既接 ibuffer2issue（前向），又接 writeback（反向写回），是流水线里少数「双向」连寄存器堆的模块。
- **预期结果**：能画出「读口朝前、写口朝后」的采集器在数据通路中的位置。
- 运行结果：待本地验证（源码阅读型）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `in_*` 用的是 `reg_ibuffer2issue_*` 而不是直接 `ibuffer2issue_*`？
**答案**：为了时序对齐，ibuffer2issue 的输出打了一拍寄存器再进 operand_collector，降低关键路径延迟。

**练习 2**：operand_collector 的写回数据从哪里来？
**答案**：来自 `writeback` 模块的 `wb_out_x_*`（标量）和 `wb_out_v_*`（向量），即各执行单元结果经 writeback 仲裁后的输出。

---

### 4.5 发射 issue 与执行单元群

#### 4.5.1 概念说明

采集到操作数后，`issue`（发射单元）根据指令类型把它**路由到对应的执行单元**。Ventus 的 SM 是**单发射**（`NUM_ISSUE=1`，每个周期至多把一条指令送进执行单元群），但执行单元群种类很多，正好对应 GPU 丰富的指令集。

`issue` 模块本质上是一个**多路选择/分发状态机**：根据译码控制信号（`mem`、`fp`、`mul`、`tc`、`sfu`、`csr`、`branch`……）决定把这条指令送到 9 个出口中的哪一个。

执行单元群包括：

| 执行单元 | 实例名 | 功能 |
|---------|--------|------|
| 标量 ALU | `salu`（aluexe） | 标量整数运算 + 标量分支 |
| 向量 ALU | `alu`（valu_top） | 向量整数运算（满 lane） |
| 向量乘法 | `mul`（vmul_top） | 向量乘法 |
| 浮点 | `fpu`（fpuexe） | 向量/标量浮点 |
| 特殊功能 | `sfu`（sfu_exe） | 除法、开方等慢速运算 |
| 访存 | `lsu`（lsu_exe） | load/store（连 dcache/shared） |
| CSR | `csrfile`（csrexe） | CSR 读写 |
| 张量核 | `tensorcore`（tensor_core_exe） | 矩阵乘 |
| SIMT 栈 | `simt_stack` | 分支发散/汇合（与 vALU 协作） |
| 分支汇总 | `branch_back` | 把标量分支与 SIMT 跳转汇总回 warp_scheduler |

#### 4.5.2 核心流程

```
operand_collector 打包好的 {操作数+控制信号}
        │
        ▼
      issue  ──按指令类型选择──┐
   ┌────────┬────────┬────────┬───────┬────────┬─────────┬────────┐
   ▼        ▼        ▼        ▼       ▼        ▼         ▼        ▼
 salu     valu     vmul     fpu     sfu      lsu      csr     tensor ...
 (标量)   (向量)   (向量乘) (浮点)  (特殊)   (访存→dcache/shared)        │
   │        │        │        │       │        │
   └────────┴────────┴────────┴───────┴────────┴──► writeback (结果汇总写回)

   salu 的分支结果 ┐
   simt_stack 跳转 ┴──► branch_back ──► warp_scheduler (更新PC/冲刷)
```

注意两个「侧链」：
- **LSU** 不只产出结果，还直接驱动 `dcache_req_*` 和 `shared_req_*` 两个存储出口（见 4.5.3）。
- **分支/SIMT** 结果经 `branch_back` 回到 `warp_scheduler`，影响取指（这是控制流闭环）。

#### 4.5.3 源码精读

`issue` 例化见 [pipe.v:1461-1639](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1461-L1639)。它的 9 个分发出口在 [issue.v:75-201](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L75-L201) 声明（`issue_out_sALU/vALU/vFPU/LSU/SFU/warps/CSR/MUL/TC_valid_o`），内部用状态机按指令类型选通（见 [issue.v:456-640](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L456-L640) 一段典型分发代码）：

```verilog
// 例如张量指令：只拉 TC 的 valid，其余清0
issue_out_TC_valid_o = inputBuf_valid;
issue_out_sALU_valid_o = 1'b0;
...
```

各执行单元例化与要点：

- 向量 ALU：[pipe.v:1640-1677](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1640-L1677)。注意其参数 `.SOFT_THREAD(`NUM_THREAD), .HARD_THREAD(`NUMBER_ALU), .MAX_ITER(`NUM_THREAD/`NUMBER_ALU)`——`NUMBER_ALU=NUM_THREAD`，故默认 `MAX_ITER=1`（满 lane 一次算完）；这套 `SOFT/HARD` 参数是为「硬件 lane 数少于线程数时多拍迭代」预留的设计。
- LSU：[pipe.v:1679-1760](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1679-L1760)，它的 `.dcache_req_valid_o(dcache_req_valid_o)` 与 `.shared_req_valid_o(shared_req_valid_o)` 直接接到 `pipe` 的对外端口——这就是 dcache/shared 两个存储出口的源头。
- 标量 ALU：[pipe.v:1792-1820](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1792-L1820)，结果除写回外还送 `branch_back`（`out2br_*`）。
- CSR：[pipe.v:1822-1874](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1822-L1874)，注意它还向 LSU/simt/FPU 提供运行时 CSR（如 `pds_base`、`numw`、`rpc`、舍入模式 `csrfile_rm`）。
- SIMT 栈：[pipe.v:1875-1906](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1875-L1906)，与 vALU 的 `valu_out2simt_if_mask` 协作维护活跃掩码。
- SFU / vmul / 张量核 / FPU：[pipe.v:1908-2059](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1908-L2059)。
- 分支汇总：[pipe.v:2061-2079](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L2061-L2079)，汇总标量分支（来自 salu）与 SIMT 跳转（来自 simt_stack），输出回 `warp_scheduler`。

#### 4.5.4 代码实践

- **实践目标**：确认 dcache/shared 两个存储出口由 LSU 驱动。
- **操作步骤**：
  1. 打开 [pipe.v:1719-1746](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1719-L1746)。
  2. 看到 `lsu_exe` 的 `.dcache_req_valid_o(dcache_req_valid_o)`、`.shared_req_valid_o(shared_req_valid_o)` 直接连到 `pipe` 的对外端口。
  3. 对比确认其他执行单元（valu/fpu/…）没有这样的存储出口。
- **需要观察的现象**：只有 LSU 例化里出现 `dcache_req_*` 和 `shared_req_*`。
- **预期结果**：证实「访存是唯一驱动数据存储出口的执行单元」。
- 运行结果：待本地验证（源码阅读型）。

#### 4.5.5 小练习与答案

**练习 1**：`NUM_ISSUE=1` 意味着什么？它和「9 个执行单元」矛盾吗？
**答案**：单发射指每周期最多把**一条**指令送进执行单元群；9 个执行单元是「可供选择的目的地」，二者不矛盾——某周期这条指令去 valu，下周期那条去 fpu，不会同时发两条。

**练习 2**：valu_top 的 `MAX_ITER` 在默认配置下是多少？为什么？
**答案**：`MAX_ITER = NUM_THREAD/NUMBER_ALU = NUM_THREAD/NUM_THREAD = 1`。因为默认向量 ALU 是满 lane 宽度（`NUMBER_ALU=NUM_THREAD`），一次就能算完全部线程，无需多拍迭代。

**练习 3**：哪些信号构成「控制流闭环」（执行结果影响取指）？
**答案**：`salu` 的标量分支结果与 `simt_stack` 的跳转结果，经 `branch_back` 汇总成 `branch_back_out_*` 回送 `warp_scheduler`，后者据此更新 PC 并发出 flush。

---

### 4.6 写回 writeback

#### 4.6.1 概念说明

各执行单元算完结果后，要写回寄存器堆。但执行单元有多个，它们可能同时产生结果，而寄存器堆的写口有限。`writeback` 模块就是一个**仲裁器**：把多个执行单元的结果汇集成固定数量的写口，再送回 `operand_collector`（寄存器堆门户）。

`pipe` 里把执行单元分成「标量结果源」和「向量结果源」两类，各 6 路（`NUM_X=6, NUM_V=6`，见 [pipe.v:103-104](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L103-L104)）。

#### 4.6.2 核心流程

```
标量结果源(6路): mul, sfu, csr, lsu2wb, fpu, salu        ┐
                                                          ├──► writeback ──► operand_collector (写回)
向量结果源(6路): tensorcore, mul, sfu, lsu2wb, fpu, valu  ┘
```

`writeback` 仲裁后输出 `wb_out_x_*`（标量）和 `wb_out_v_*`（向量），同时产生 `wb_out_x/v_fire` 反馈给 scoreboard（用于清空冒险位图，见 4.3）。

#### 4.6.3 源码精读

6 路标量源的拼接见 [pipe.v:880-884](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L880-L884)，6 路向量源见 [pipe.v:886-891](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L886-L891)：

```verilog
// 标量6路: {mul, sfu, csr, lsu2wb, fpu, salu}  (高位→低位)
assign writeback_in_x_valid = {mul_out_x_valid, sfu_out_x_valid, csrfile_out_valid,
                               lsu2wb_out_x_valid, fpu_out_x_valid, salu_out_valid};
// 向量6路: {tensorcore, mul, sfu, lsu2wb, fpu, valu}
assign writeback_in_v_valid = {tensorcore_out_v_valid, mul_out_v_valid, sfu_out_v_valid,
                               lsu2wb_out_v_valid, fpu_out_v_valid, valu_out_valid};
```

`writeback` 例化见 [pipe.v:2081-2114](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L2081-L2114)，参数 `NUM_X=6, NUM_V=6`，输出 `out_x_valid_o/out_v_valid_o` 接到 operand_collector 的 `writeScalar/writeVector` 入口。

另外，`pipe` 末尾 [pipe.v:2116](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L2116) 有一句对外的 LSU 空闲指示：

```verilog
assign lsu_mshr_is_empty_o = &lsu_fence_end; // 所有warp的fence结束=LSU空闲
```

> 说明：这个信号供上层（`sm_wrapper`/GPGPU_top）判断「缓存无效化前 LSU 是否已排空」，与 u1-l5 讲的 wg 完成后冲刷缓存的握手相连。

#### 4.6.4 代码实践

- **实践目标**：核对写回源的位序与执行单元的对应。
- **操作步骤**：
  1. 打开 [pipe.v:880](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L880) 和 [pipe.v:886](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L886)。
  2. 按「高位→低位」列出标量 6 路和向量 6 路分别对应哪个执行单元。
  3. 在各执行单元例化处确认其 `out_ready_i` 接的是 `writeback_in_x/v_ready[i]` 中正确的位（例如 salu 接 `writeback_in_x_ready[0]`，见 [pipe.v:1796](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1796)）。
- **需要观察的现象**：执行单元的 ready 反馈位序与 valid 拼接位序一一对应。
- **预期结果**：例如 `salu` 在标量拼接中是最低位（`[0]`），故其 `out_ready_i` 接 `writeback_in_x_ready[0]`。
- 运行结果：待本地验证（源码阅读型）。

#### 4.6.5 小练习与答案

**练习 1**：为什么把写回源分成「标量 6 路」和「向量 6 路」两组，而不是混在一起？
**答案**：因为标量寄存器堆（SGPR）和向量寄存器堆（VGPR）是两套独立存储、各自有写口；标量结果写 SGPR、向量结果写 VGPR，分组仲裁更清晰。

**练习 2**：`lsu_mshr_is_empty_o` 在系统里有什么用？
**答案**：它表示所有 warp 的 LSU 操作（含 fence）已排空，上层在 workgroup 完成后做 dcache 无效化/回写前，需要确认它为真，避免还有未完成的访存。

---

## 5. 综合实践

**任务：以 `pipe.v` 为蓝本，绘制 SM 流水线框图并标注关键信号路径。**

把前面 6 节的知识串起来，完成一份「SM 流水线全景图」：

1. **画主干**：按 `warpReq → warp_scheduler → decodeUnit → ibuffer → ibuffer2issue → operand_collector → issue → {执行单元群} → writeback → operand_collector(写回)` 画出主干框图。
2. **标三个存储出口**：在框图上标出
   - icache 出口（由 `warp_sche` 驱动，连 [pipe.v:26-29](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L26-L29)）；
   - dcache 出口（由 `lsu` 驱动，见 [pipe.v:1719-1729](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1719-L1729)）；
   - shared_mem 出口（由 `lsu` 驱动，见 [pipe.v:1737-1746](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1737-L1746)）。
3. **标两条反馈回路**：
   - 冒险反馈：`scoreb_delay → warp_scheduler.scoreboard_busy_i`（[pipe.v:937](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L937)）；
   - 控制流闭环：`salu/simt_stack → branch_back → warp_scheduler`（[pipe.v:2061-2079](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L2061-L2079)）。
4. **标写回汇聚**：在执行单元群右侧画出 writeback 的 6+6 路输入（[pipe.v:880-891](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L880-L891)）。
5. **进阶（可选）**：在 Verdi 波形里（参见 u1-l4 的 `make verdi`）选中一个 warp，跟踪它的某条指令从 `icache_rsp_valid` 到 `wb_out_v_valid` 的若干拍，验证你画的框图顺序。

**交付物**：一张标注完整的 SM 流水线框图（手绘或工具画均可），并在每个箭头上写出对应的信号名或行号。

> 本实践为源码阅读 + 画图型，无需运行仿真即可完成；进阶步骤需本地仿真环境，结果待本地验证。

## 6. 本讲小结

- `pipe` 是 SM 核流水线的「连线集装箱」，自身几乎不做运算，靠例化 18 个子模块并用 `wire`/`assign` 拼接出完整数据通路。
- 前端 `warp_scheduler → decodeUnit → ibuffer` 负责取指、译码、暂存；[pipe.v:706-715](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L706-L715) 的胶水逻辑把三者缝合，并用「伪装 miss」实现 ibuffer 反压。
- 每 warp 一个 `scoreboard` 用位图检测 RAW 等冒险，`delay_o` 回送 `warp_scheduler` 形成发射前的就绪反馈。
- `operand_collector` 夹在 ibuffer2issue 与 issue 之间，是寄存器堆（SGPR/VGPR）的读写门户，也是写回落点。
- `issue` 单发射、多出口，按指令类型路由到 valu/vmul/fpu/sfu/lsu/salu/csr/tensor/simt 等 9 类执行单元；**LSU 是唯一驱动 dcache 与 shared_mem 两个存储出口的单元**。
- `writeback` 把 6 路标量源 + 6 路向量源仲裁后写回寄存器堆，并反馈 `fire` 给 scoreboard 清空冒险位；分支/SIMT 结果经 `branch_back` 闭环回取指。

## 7. 下一步学习建议

本讲给了「地铁线路图」，接下来的讲义带你逐条线深入：

- **u3-l2 取指与指令缓存 icache**：深入 `instruction_cache`、tag 检查、mshr 缺失处理（对应本讲的 icache 出口外侧）。
- **u3-l3 指令缓冲 ibuffer 与译码 decodeUnit**：精读译码如何利用 `define.v` 位模式生成控制信号（对应本讲 4.2）。
- **u3-l4 发射 issue 与记分板 scoreboard**：深入冒险判定的全部细节（对应本讲 4.3）。
- **u4 系列执行单元**：valu/vmul/fpu/sfu 的内部实现（对应本讲 4.5 的各执行单元）。
- **u5-l1 访存单元 LSU**：LSU 内部如何算地址、用 MSHR 跟踪未完成访存（对应本讲 dcache/shared 出口的源头）。

建议先做本讲的「综合实践」框图，再带着它进入 u3-l2，这样每读一个子模块都能知道它在图里的位置。
