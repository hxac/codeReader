# cu_handler 与 inflight_wg_buffer 派发流程

## 1. 本讲目标

上一讲（u2-l1）我们看清了「一个 workgroup 凭什么被派发到某个 SM」——`allocator_neo` 用 CAM 比较选出候选 CU、`top_resource_table` 用链表管账。但那只是「**决定派给谁**」。

本讲要回答的是紧随其后的两个问题：

1. **决定派给谁之后，怎么真正把派发动作做出来？** 那一大堆派发字段（`start_pc`、`vgpr/sgpr/lds base`、`wf_tag`……）是谁组装、按什么时序送到 SM 的？
2. **SM 上的 warp 跑完后，完成信号怎么一路回传到主机？**

学完本讲你应当能够：

- 说清 `dis_controller`、`inflight_wg_buffer`、`gpu_interface`、`cu_handler` 四个模块在「派发」这件事上各自的角色与协作时序。
- 跟踪一组派发字段从 `cu_handler` 生成 tag，经 `gpu_interface` 组装，最终出现在 SM 侧 `cta2warp` 接口的完整路径。
- 跟踪一个 warp 完成信号 `wf_done` 从 SM 回传、被 `cu_handler` 计数、汇聚成 WG 完成再到主机的完整回收路径。
- 理解 tag 的位域编码、WG 槽位（slot）管理，以及派发节奏是如何被状态机与轮转选择控制的。

## 2. 前置知识

本讲假设你已读过 u2-l1（CTA 调度器与资源表）。回顾几个关键概念：

- **workgroup（WG）/ warp（WF）**：一个 WG 含若干 WF（wavefront/warp）。主机一次下发一个 WG，硬件再把它拆成 WF 逐个派发给 SM 执行。
- **CU = SM**：`NUMBER_CU = NUM_SM`（[define.v:149](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L149)），一个 CU 就是一个 SM 核。
- **WF slot / WG slot**：每个 CU 内部维护若干「WF 槽位」`NUMBER_WF_SLOTS = NUM_BLOCK = NUM_WARP`（[define.v:159](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L159)、[define.v:15](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L15)）。一个 WG 占用其中一个槽位，槽位号用于在 CU 内部区分「这是哪个 WG」。
- **tag**：派发时随每个 warp 附带的一个标识，`TAG_WIDTH = WG_SLOT_ID_WIDTH + WF_COUNT_WIDTH_PER_WG`（[define.v:199](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L199)）。它把「WG 槽位号」和「WG 内的 warp 序号」拼在一起，warp 完成时原样回传，硬件据此知道哪个 WG 的第几个 warp 结束了。

> 一句话直觉：上一讲是「**调度器的脑子**」（算账、选 CU），本讲是「**调度器的手脚**」（把决定落实成对 SM 的逐拍握手，并把完成信号收回来）。

## 3. 本讲源码地图

本讲涉及的关键文件，全部位于 `src/gpgpu_top/cta_top/` 下：

| 文件 | 作用 |
|------|------|
| [cta/dis_controller.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/dis_controller.v) | **派发总指挥**。状态机串起「请求分配→等待结果→回 ACK」全过程，并用 `cu_groups_allocating` 做互斥保护，控制派发节奏。 |
| [cta/inflight_wg_buffer.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/inflight_wg_buffer.v) | **主机请求缓存**。用两张 RAM 表（waiting/ready）缓存主机下发的 WG 元数据，给 allocator 提供「算账用」字段、给 gpu_interface 提供「派发用」字段；轮转选择下一个待分配 WG。 |
| [cta/gpu_interface.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/gpu_interface.v) | **派发字段组装器**。把 allocator 的决定与 inflight_wg_buffer 的元数据拼成对 CU 的派发握手，逐 warp 派发并维护「每 warp 递增的寄存器基址」；同时例化 `NUMBER_CU` 个 cu_handler。 |
| [cta/cu_handler.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/cu_handler.v) | **每 CU 的 WF 槽位管家**（每个 CU 一个）。管理本 CU 的 WG 槽位、为每个 warp 生成唯一 tag、计数完成的 warp、在本 WG 全部 warp 完成时上报 `wg_done`。 |
| [wf_done_interface_single.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/wf_done_interface_single.v) | **WF 完成信号缓冲**。用一个 `stream_fifo` 把 WG 完成事件排队后以 valid/ready 握手交给主机。 |
| [cta_interface.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v) | **CTA 子系统的门面**。例化 cta_scheduler 与 wf_done_interface_single，把派发字段广播到各 SM（`cta2warp`）、把 SM 的 warp 完成信号接回（`warp2cta`）。 |
| [cta/cta_scheduler.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/cta_scheduler.v) | **五子模块的连线顶层**。把上面四个模块（加 allocator/resource_table）连成闭环。 |

---

## 4. 核心概念与源码讲解

先看一张全局协作图，再逐模块拆解。

```
        主机 host2cta_*  ──▶  inflight_wg_buffer  (双表缓存 + 轮转选 WG)
                                   │   │
                  (alloc_*算账字段) │   │ (gpu_*派发字段)
                                   ▼   ▼
   dis_controller ──start_alloc──▶ allocator_neo / resource_table   (选 CU、算基址)
        ▲  │                           │ allocator_cu_id / vgpr_start / ...
        │  └─wg_alloc_valid/rejected─▶ │
        │                              ▼
        │                        gpu_interface  (组装派发握手 + 例化 N 个 cu_handler)
        │                              │  │
        │   wg_done(本CU汇总)          │  │ dispatch2cu_wf_dispatch / tag / base...
        │        ◀─────────────────────┘  ▼
        │                          cta_interface ──cta2warp──▶ SM (cta2warp.v)
        │                              ▲
        │   warp2cta(wf_done+tag)      │
        └──────────────────────────────┘
                  ▼
   inflight_wg_buffer.host_wf_done ──▶ wf_done_interface_single(stream_fifo) ──▶ 主机 cta2host
```

四个最小模块按职责划分：`dis_controller` 是「指挥」、`inflight_wg_buffer` 是「仓库」、`gpu_interface` 是「组装车间」、`cu_handler` 是「每条产线的工位」。下面逐个讲。

### 4.1 dis_controller —— 派发总指挥状态机

#### 4.1.1 概念说明

`dis_controller` 自己不做任何数据搬运或算账，它只发**控制脉冲**，决定「现在该不该开始一次分配、分配结果如何、要不要回 ACK」。它是整个派发流程的节拍器。

它的输入来自三处：

- `inflight_wg_buffer`：有没有 WG 在排队、仓库忙不忙（`alloc_valid`、`alloc_available`）。
- `allocator`：分配结果（`allocator_cu_valid` / `allocator_cu_rejected` / `allocator_cu_id_out`）。
- `gpu_interface`：派发端与回收端是否就绪（`alloc_available`、`dealloc_available`、`cu_id`）。
- `top_resource_table`：账本更新完成（`grt_wg_alloc_done` / `grt_wg_dealloc_done`）。

它的输出是给各模块的指挥信号：`start_alloc`（让 inflight_wg_buffer 选一个 WG 给 allocator）、`alloc_ack`（让 allocator 确认收到）、`wg_alloc_valid`（通知 inflight_wg_buffer/gpu_interface：这次分配成功，可以派发了）、`wg_dealloc_valid`（通知：这次回收成立，可以释放资源并上报主机）、`wg_rejected_valid`（通知：放不下，驳回）、`cu_busy`（给 allocator 的互斥掩码）。

#### 4.1.2 核心流程

分配主状态机有四态（独热编码，便于互斥判断）：

```
ST_AL_IDLE ──(buffer有WG排队 且 无组在分配)──▶ ST_AL_ALLOC
   ▲                                              │ 发 start_alloc
   │                                              ▼ 等待 allocator_cu_valid
   │                                         ST_AL_HANDLE_RESULT   记下 alloc_waiting_cu_id
   │                                              │ 等 alloc_waiting / 资源就绪
   │                                              ▼ 发 alloc_ack
   │                                         ST_AL_ACK_PROPAGATION
   └──────────────────────────────────────────────┘ 回到 IDLE
```

关键设计：`cu_groups_allocating` 是一个按「资源表组」索引的占用位图。`ST_AL_IDLE` 进入 `ST_AL_ALLOC` 的条件里有 `!(&cu_groups_allocating)`（[dis_controller.v:85](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/dis_controller.v#L85)）——只要还有任一组没在分配，才允许开新一轮分配。这就是**派发节奏**的第一道闸门：避免两次分配同时改动同一份资源账本造成竞争。

`wg_alloc_valid` / `wg_dealloc_valid` / `wg_rejected_valid` 这三个“结果脉冲”由另一个 always 块按优先级产生（[dis_controller.v:146-179](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/dis_controller.v#L146-L179)）：**回收优先于分配**——若 `gpu_interface_dealloc_available_i` 先成立，则发 `wg_dealloc_valid`；否则若正处于 `alloc_waiting` 且资源就绪，才发 `wg_alloc_valid`（被拒绝则发 `wg_rejected_valid`）。这保证完成回收不被新分配饿死。

#### 4.1.3 源码精读

端口与参数（默认 `NUMBER_CU=2`）：

[dis_controller.v:16-45](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/dis_controller.v#L16-L45) —— 模块端口。注意 `RES_TABLE_ADDR_WIDTH` 决定了 CU 号到「资源表组地址」的映射高位（见下方第 72 行 `gpu_interface_cu_res_tbl_addr` 的切片）。

分配状态机主体：

[dis_controller.v:82-142](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/dis_controller.v#L82-L142) —— 四态机：IDLE 置 `start_alloc`、ALLOC 等 `allocator_cu_valid`、HANDLE_RESULT 置 `alloc_ack`、ACK_PROPAGATION 回 IDLE。

互斥位图更新（派发节奏的核心）：

[dis_controller.v:196-220](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/dis_controller.v#L196-L220) —— `cu_groups_allocating` 在「开始回收」「开始成功分配」时置位，在 `grt_wg_alloc_done` / `grt_wg_dealloc_done`（账本更新完成）时清位。它在 `[CU_ID_WIDTH-1:CU_ID_WIDTH-RES_TABLE_ADDR_WIDTH]` 这几位上索引，即「同组的 CU 共享一把锁」。

#### 4.1.4 代码实践

**实践目标**：理解分配状态机为何不会在同一拍同时驱动分配与回收。

**操作步骤**：

1. 打开 `dis_controller.v`，定位 [L146-L179](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/dis_controller.v#L146-L179) 的结果输出 always 块。
2. 注意第一个 `else if` 分支（回收）与第二个 `else if` 分支（分配）是互斥的 `if-else if` 链。

**需要观察的现象 / 预期结果**：任何一拍，`wg_dealloc_valid`、`wg_alloc_valid`、`wg_rejected_valid` 三者至多一个为 1，永远不会同时拉起两个。这是源码静态可确认的结论（**待本地验证**：可在仿真中对这三个输出加 `assert` 断言验证）。

#### 4.1.5 小练习与答案

**练习 1**：如果 `cu_groups_allocating` 这把「组锁」被去掉（即 IDLE 态不再判断它），最坏会发生什么？

**参考答案**：两个针对同一资源表组的分配可能先后进入 `ST_AL_ALLOC`，进而同时读写同一份 `top_resource_table` 的链表与 CAM，导致资源账本（VGPR/SGPR/LDS 空闲块）被错误更新，出现「同一块寄存器分给两个 WG」的脏分配。

**练习 2**：为什么回收（dealloc）被设计为优先于分配（alloc）？

**参考答案**：回收意味着有 SM 上的资源被释放，及时处理回收既能让排队的新 WG 更早获得资源，也避免「明明有空闲资源却被正在进行的分配流程挡住」的死锁/饥饿。

---

### 4.2 inflight_wg_buffer —— WG 元数据双表缓存与轮转选择

#### 4.2.1 概念说明

主机下发的 WG 携带大量元数据（wg_id、wf 数、wf_size、start_pc、各类 baseaddr、各类资源需求……）。如果主机一发 WG 就立刻去分配，硬件会非常难做（分配可能要好几拍，期间主机若再发就会丢）。`inflight_wg_buffer` 就是这个「**缓冲仓库**」：先把 WG 元数据收下并回 ACK，再从容地交给后续流程。

它的精妙之处在于用**两张并行的 RAM 表**分别服务两类用途：

- **waiting 表**（`U_ram_wg_waiting_allocation`）：存「给 allocator 算账用」的字段——wg_id、wf_count、vgpr/sgpr/lds/gds **总量**需求。allocator 需要这些来判断「放不放得下」。
- **ready 表**（`U_ram_wg_ready_start`）：存「派发给 SM 用」的字段——wf_size、start_pc、gds/pds/csr baseaddr、kernel_size_3d、vgpr/sgpr **每 warp**需求。这些在分配成功后由 gpu_interface 取走组装派发。

两表共用同一个 entry 地址（`new_index`），同一个 WG 在两表里位置一致。表深 `NUMBER_ENTRIES = 2`（[define.v:171](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L171)）。

#### 4.2.2 核心流程

**入队（host 状态机）**：

```
ST_RD_HOST_IDLE ──(host_wg_valid 且 表未满)──▶ ST_RD_HOST_GET_FROM_HOST
   ▲   选 new_index(fixed_pri_arb 找空位)             │ 写两表、置 waiting_tbl_valid、拉起 rcvd_ack
   │                                                   ▼
   │                                              ST_RD_HOST_ACK_TO_HOST  (rcvd_ack=0)
   │                                                   ▼
   └─────────────── ST_RD_HOST_IDLE_BUBBLE ◀──────────┘
```

**出队分配（alloc 状态机，8 态）**：当 `dis_controller_start_alloc` 来时，把当前轮转选中的 entry 标 pending；等 allocator 结果（accepted/rejected）；用 `tbl_walk` 逐项读 waiting 表、按 `wg_id` 匹配找到那个 entry；若 accepted 则从 ready 表读出派发字段并拉起 `gpu_valid`（送给 gpu_interface），若 rejected 则清除 pending 让该 WG 重新参与下一轮选择；最后用 RR（轮转）选下一个 entry 喂给 allocator。

**回收上报**：收到 `dis_controller_wg_dealloc_valid` 时，拉起 `host_wf_done` + `wf_done_wg_id`（取自 `gpu_interface_dealloc_wg_id`），交给 wf_done_interface_single。

#### 4.2.3 源码精读

主机入队状态机（注意 `host_wg_ready_o = !(&waiting_tbl_valid)`，表满则反压主机）：

[inflight_wg_buffer.v:370-425](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/inflight_wg_buffer.v#L370-L425) —— `GET_FROM_HOST` 态把主机字段拆成两表分别写入（[L398-L399](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/inflight_wg_buffer.v#L398-L399)），并拉起 `host_rcvd_ack`。

两张 RAM 的例化（共用 `new_index` 与 `tbl_walk_idx`）：

[inflight_wg_buffer.v:337-367](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/inflight_wg_buffer.v#L337-L367) —— waiting 表与 ready 表。

accepted 分支：找到 entry 后从 ready 表切片输出派发字段并拉 `gpu_valid`：

[inflight_wg_buffer.v:529-543](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/inflight_wg_buffer.v#L529-L543) —— `ST_ALLOC_CLEAR_ACCEPTED`：把 start_pc、pds/csr/gds baseaddr、wf_size、per-wf vgpr/sgpr 等从 ready 表读出送到 gpu_interface。

轮转选择下一个待分配 WG（派发节奏的第二道闸门，保证公平）：

[inflight_wg_buffer.v:712-739](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/inflight_wg_buffer.v#L712-L739) —— 用 `last_chosen_entry_rr` 作旋转起点，对 `valid_not_pending`（有效且未在处理中）位图做循环移位，再用 `fixed_pri_arb` 选最低位，实现**轮询公平**：每完成一次分配，下一次就从上次选中的下一位开始找。

回收上报脉冲：

[inflight_wg_buffer.v:284-297](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/inflight_wg_buffer.v#L284-L297) —— `dis_controller_wg_dealloc_valid` 到来时拉起 `host_wf_done` 与 `wf_done_wg_id`（一拍脉冲）。

#### 4.2.4 代码实践

**实践目标**：验证「表满反压」与「轮转公平」两个机制。

**操作步骤**：

1. 在 [L204](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/inflight_wg_buffer.v#L204) 确认 `host_wg_ready_o = !(&waiting_tbl_valid)`。
2. 在 [L652-L665](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/inflight_wg_buffer.v#L652-L665) 看 `waiting_tbl_valid` 的置位（ACK_TO_HOST 态）/清位（CLEAR_ACCEPTED 态）时机。
3. 设想 host 连续下发 4 个 WG（`NUMBER_ENTRIES=2`，表只能存 2 个）。

**需要观察的现象 / 预期结果**：前 2 个被接收并回 ACK；第 3 个到来时 `waiting_tbl_valid` 全 1 → `host_wg_ready_o=0`，主机被反压，必须等某个 WG 被分配走（`CLEAR_ACCEPTED` 清掉一个 valid）后才能继续接收。**待本地验证**：可在 tc 用例的 testbench 里观察 `host_wg_ready_o` 与下发时序的关系。

#### 4.2.5 小练习与答案

**练习 1**：为什么要把元数据拆成 waiting 表和 ready 表两张，而不是合并成一张宽表？

**参考答案**：两类用途读取时机与字段不同——allocator 只需要「资源总量」字段来算账，gpu_interface 只需要「派发执行」字段。拆表后每张表的位宽更窄、读端口更简单；且 alloc 状态机遍历 waiting 表按 `wg_id` 匹配时只读窄表，节省读带宽与功耗。

**练习 2**：`last_chosen_entry_rr` 的作用是什么？若改成固定从 entry 0 开始选会有什么问题？

**参考答案**：它记录上一次选中的 entry，下一次从其下一位开始轮转（RR）。若固定从 0 开始，则 entry 0 里的 WG 会被反复优先选中，高地址 entry 的 WG 可能长期得不到分配（饥饿）；RR 保证各 entry 公平轮到。

---

### 4.3 gpu_interface —— 派发字段组装与逐 warp 派发

#### 4.3.1 概念说明

allocator 只告诉你「派给 CU 几号、寄存器从哪开始」，inflight_wg_buffer 只给你「这个 WG 的元数据」。真正把这些信息**拼成对 SM 的逐拍握手**的是 `gpu_interface`。它还做了一件关键的事：**为每个 warp 计算递增的寄存器基址**——同一个 WG 的多个 warp 要占用连续但不重叠的 VGPR/SGPR 空间，每派发一个 warp，基址就加上「每 warp 所需大小」。

此外，`gpu_interface` 用 `generate` 例化了 `NUMBER_CU` 个 `cu_handler`（[gpu_interface.v:146-166](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/gpu_interface.v#L146-L166)），每个 CU 配一个专属管家。gpu_interface 把「分配给 CU_i」这件事翻译成对 `handler_wg_alloc_en[i]` 的单拍脉冲，剩下的「拆 warp、生成 tag」交给 cu_handler 自己逐拍完成。

#### 4.3.2 核心流程

**分配（派发）状态机**：

```
ST_ALLOC_IDLE ──(alloc_valid 且 buffer就绪)──▶ 置 handler_wg_alloc_en[cu_id] (通知该 CU 管家)
        │            │                              │
        │            └─buffer未就绪─▶ ST_ALLOC_WAIT_BUFFER (等 gpu_valid)
        │                                           │
        │                                      ST_ALLOC_WAIT_HANDLER (等 cu_handler 发出首个 dispatch)
        │                                           │ 组装全部派发字段、发首拍
        │                                      ST_ALLOC_PASS_WF
        │                                           │ 每拍：tag 取自 cu_handler；vgpr/sgpr base += per_wf_size
        │                                           │   直到 cu_handler 不再发 dispatch → 回 IDLE
        └───────────────────────────────────────────┘
```

`ST_ALLOC_PASS_WF` 是逐 warp 派发的核心：每拍若 cu_handler 发来 `dispatch2cu_wf_dispatch_handlers[cu_id]`，gpu_interface 就把 `dispatch2cu_wf_dispatch_o` 拉成 `1<<cu_id` 的 one-hot，并更新一次 tag 与递增后的基址；直到该 WG 全部 warp 派发完。

**派发字段（SM 侧 `cta2warp`/`dispatch2cu` 接口最终看到的所有信号）**：

| 字段 | 来源 | 含义 |
|------|------|------|
| `dispatch2cu_wf_dispatch` | gpu_interface 生成 one-hot | 指向被派发的 CU（SM）|
| `dispatch2cu_wf_tag_dispatch` | **cu_handler 生成** | {WG slot 号, warp 序号} |
| `dispatch2cu_wg_wf_count` | allocator_wf_count | 本 WG 的 warp 总数 |
| `dispatch2cu_wf_size_dispatch` | inflight_wg_buffer(wf_size) | 每 warp 的线程规模 |
| `dispatch2cu_vgpr_base_dispatch` | allocator 起点 + **逐 warp 递增** | 本 warp 的 VGPR 基址 |
| `dispatch2cu_sgpr_base_dispatch` | allocator 起点 + **逐 warp 递增** | 本 warp 的 SGPR 基址 |
| `dispatch2cu_lds_base_dispatch` | allocator_lds_start | 本 WG 的 LDS 基址 |
| `dispatch2cu_start_pc_dispatch` | inflight_wg_buffer | 取指起始 PC |
| `dispatch2cu_pds/csr_knl/gds_base_dispatch` | inflight_wg_buffer | 参数/CSR/GDS 基址 |
| `dispatch2cu_kernel_size_3d_dispatch` | inflight_wg_buffer | WG 的三维规模 |

> 注意：SM 侧并没有直接收到 `wg_id`（在 `cta_interface` 中 `cta2warp_dispatch2cu_wg_id_o` 被硬连线为 `'d0`，见 [cta_interface.v:160](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v#L160)）。WG 的身份由 `start_pc`、各 base 与 tag 隐式携带。

#### 4.3.3 源码精读

cu_handler 的 generate 例化（每 CU 一个）：

[gpu_interface.v:146-166](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/gpu_interface.v#L146-L166) —— 把 `handler_wg_alloc_en[i]`、`handler_wg_alloc_wg_id`、`handler_wg_alloc_wf_count` 喂给每个 handler，回收它的 `dispatch2cu_wf_dispatch_handlers_w`、`wg_done_valid_w`。

派发状态机（含逐 warp 基址递增）：

[gpu_interface.v:317-358](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/gpu_interface.v#L317-L358) —— `ST_ALLOC_WAIT_HANDLER` 在 [L318-L333](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/gpu_interface.v#L318-L333) 组装首拍全部字段（tag 取自 handler），`ST_ALLOC_PASS_WF` 在 [L340-L347](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/gpu_interface.v#L340-L347) 每拍把 sgpr/vgpr base 加上 per-wf size。

回收（dealloc）状态机——在多个 CU 间仲裁出已完成 WG：

[gpu_interface.v:386-431](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/gpu_interface.v#L386-L431) —— IDLE 态用 `chosen_done_cu_valid` 选一个上报了 `wg_done` 的 CU，拉 `dealloc_available` + `dealloc_wg_id`，并回 `handler_wg_done_ack` 给该 handler。

CU 间完成仲裁（固定优先级）：

[gpu_interface.v:433-452](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/gpu_interface.v#L433-L452) —— `fixed_pri_arb` 在 `handler_wg_done_valid` 上选最低位 CU，`one2bin` 转成二进制 CU 号。

#### 4.3.4 代码实践

**实践目标**：看清「逐 warp 派发时寄存器基址如何递增」，从而理解同一 WG 的多个 warp 为何不会争用同一片寄存器。

**操作步骤**：

1. 打开 [L340-L347](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/gpu_interface.v#L340-L347)，找到这两行：
   ```verilog
   dispatch2cu_sgpr_base_dispatch_reg <= dispatch2cu_sgpr_base_dispatch_reg + inflight_wg_buffer_gpu_sgpr_size_per_wf_reg;
   dispatch2cu_vgpr_base_dispatch_reg <= dispatch2cu_vgpr_base_dispatch_reg + inflight_wg_buffer_gpu_vgpr_size_per_wf_reg;
   ```
2. 假设一个 WG 有 3 个 warp，每 warp 需 8 个 VGPR，allocator 给的 `vgpr_start=100`。
3. 逐拍推算第 1/2/3 个 warp 收到的 `vgpr_base`。

**预期结果**：warp0→100、warp1→108、warp2→116。三个 warp 各占不重叠的 8 个 VGPR，首地址由本模块逐拍累加得出。这是静态可推算的（**待本地验证**：仿真中 dump 这三个值核对）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 LDS 基址（`lds_base`）不需要像 VGPR/SGPR 那样逐 warp 递增？

**参考答案**：LDS（共享内存）是**整个 WG 共享**的一片存储，同一 WG 的所有 warp 访问同一块 LDS（基址相同、由 WG 共用），而 VGPR/SGPR 是**每 warp 独占**的，所以基址必须按 warp 递增以防重叠。这是 SIMT/GPU 编程模型里「共享内存 vs 私有寄存器」语义差异在硬件上的直接体现。

**练习 2**：`ST_ALLOC_WAIT_HANDLER` 等的是什么？为什么不直接在 IDLE 态就发派发字段？

**参考答案**：它等 cu_handler 找到空闲 WG slot 并发出首个 `dispatch2cu_wf_dispatch_handlers`（因为 tag 由 cu_handler 生成，gpu_interface 必须等 handler 把 tag 准备好）。slot 分配、tag 生成都需要 cu_handler 内部的状态机走一拍，所以中间隔了 `WAIT_HANDLER` 这一态。

---

### 4.4 cu_handler —— WF 槽位管理、tag 生成与 WG 完成判定

#### 4.4.1 概念说明

`cu_handler` 是「**每个 CU 一个**」的本地管家（gpu_interface 用 generate 例化 `NUMBER_CU` 个）。它的职责：

1. **分配 WG slot**：收到 `wg_alloc_en_i` 时，在本 CU 的 `NUMBER_WF_SLOTS` 个槽位里找一个空闲的，存下 {wg_id, wf_count}。
2. **生成 tag 并逐 warp 派发**：为该 WG 的每个 warp 生成唯一 tag = {slot 号, warp 序号}，每拍发一个，直到 wf_count 个 warp 全发完。
3. **计数完成的 warp**：每收到一个 `cu2dispatch_wf_done_i`（带 tag），按 tag 里的 slot 号把对应 WG 的「待完成计数」减一。
4. **判定 WG 完成**：某 WG 的待完成计数减到 0 时，上报 `wg_done_valid` + `wg_id`。

tag 的位域（理解本模块的关键）：

```
TAG_WIDTH = WG_SLOT_ID_WIDTH + WF_COUNT_WIDTH_PER_WG
            └── 上位: WG slot 号 ──┘ └─ 下位: WG 内 warp 序号 ─┘
```

#### 4.4.2 核心流程

**分配侧（alloc）两态**：

```
ST_ALLOC_IDLE ──(wg_alloc_en 且 有空闲slot)──▶ ST_ALLOCATING
   ▲  选 next_free_slot、写 info_ram{wg_id,wf_count}        │ 每拍(若 CU 就绪):
   │  置 used_slot_bitmap[slot]                              │   tag = {slot, count-1}
   │                                                         │   count-- , 发 dispatch2cu_wf_dispatch
   └─────────────────────────────────────────────────────────┘ 直到 count==0 回 IDLE
```

**回收侧（dealloc）三态**——这部分与分配侧**并行**运行（两个独立状态机 `alloc_st` / `dealloc_st`）：

```
每当 cu2dispatch_wf_done_i 到来：
   pending_wf_count[tag.slot]--     (剩余 warp 数减一)
   若 pending_wf_count[tag.slot]==0：置 pending_wg_bitmap[slot]=1  (该 WG 全部 warp 完成)

ST_DEALLOC_IDLE ──(有 pending_wg_bitmap)──▶ ST_DEALLOC_READ_RAM
   ▲  读 info_ram[slot] 取 wf_count                │ 等 rd_valid
   │                                               │ 比较 curr_wf_count==0?
   │                                          ST_DEALLOC_PROPAGATE
   │                                               │ 发 wg_done_valid + wg_done_wg_id(从info_ram取)
   └───────────────────────────────────────────────┘ 等 wg_done_ack 回 IDLE
```

> 这里有个细节：`pending_wf_count` 在分配时被初始化为 `wg_alloc_wf_count_i`（[cu_handler.v:312](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/cu_handler.v#L312)），每来一个 wf_done 减一；`info_ram` 里则始终存着原始的 {wg_id, wf_count}，供 dealloc 时把 wg_id 取出来上报。

#### 4.4.3 源码精读

端口与 tag 位域定义：

[cu_handler.v:17-45](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/cu_handler.v#L17-L45) —— 输入 `wg_alloc_en_i`/`cu2dispatch_wf_done_i`/`cu2dispatch_wf_tag_done_i`，输出 `dispatch2cu_wf_dispatch_o`/`dispatch2cu_wf_tag_dispatch_o`/`wg_done_valid_o`/`wg_done_wg_id_o`。`TAG_WG_SLOT_ID_*` 与 `TAG_WF_COUNT_*` 定义了 tag 的切片。

分配态发 tag 的核心语句：

[cu_handler.v:218-228](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/cu_handler.v#L218-L228) —— 在 `ready_for_dispatch2cu_i` 就绪时，`dispatch2cu_wf_tag_dispatch_reg <= {curr_alloc_wf_slot, (curr_alloc_wf_count-1)}`，并把 `curr_alloc_wf_count` 递减。注释说明「Send the counter just to make sure the cu does not have two wf with the same tag」——即 tag 全局唯一，避免 SM 把两个 warp 的完成信号搞混。

wf_done 到达时按 slot 计数：

[cu_handler.v:301-326](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/cu_handler.v#L301-L326) —— `cu2dispatch_wf_tag_done_slot` 从 tag 取上位得 slot；`pending_wf_count[slot]--`；归零则 `pending_wg_bitmap[slot]<=1`。

WG 完成上报（dealloc 状态机的读 RAM 与 propagate）：

[cu_handler.v:246-272](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/cu_handler.v#L246-L272) —— `info_ram_rd_valid` 且 `curr_wf_count==0` 时拉 `wg_done_valid`，并从 `info_ram_rd_reg` 切出 `wg_id` 上报，同时清 `used_slot_bitmap` 释放 slot。

空闲 slot 查找（固定优先级仲裁 + one2bin）：

[cu_handler.v:356-375](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/cu_handler.v#L356-L375) —— `fixed_pri_arb` 在 `~used_slot_bitmap` 上选最低位空 slot，`one2bin` 转成 slot 号。

待回收 WG 查找：

[cu_handler.v:334-354](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/cu_handler.v#L334-L354) —— `fixed_pri_arb` 在 `pending_wg_bitmap` 上选一个已完成 WG 进入 dealloc 流程。

#### 4.4.4 代码实践

**实践目标**：把 tag 的「slot + warp 序号」编码与完成计数逻辑串起来，能凭 tag 推断回收过程。

**操作步骤**：

1. 设 `NUM_WARP=4`（默认）→ `WG_SLOT_ID_WIDTH=$clog2(4)=2`、`WF_COUNT_WIDTH_PER_WG=$clog2(4)+1=3`、`TAG_WIDTH=5`（[define.v:189-199](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L189-L199)）。
2. 假设某 WG 被分到 slot=2，wf_count=3。逐拍写出 alloc 阶段发出的 tag（5 位，高 2 位 slot、低 3 位序号）。
3. 推算：3 个 warp 依次完成回传同样的 tag 时，`pending_wf_count[2]` 如何从 3 变到 0。

**预期结果**：

| 拍 | tag_dispatch（二进制 slot=2,wf_idx） | 含义 |
|----|--------------------------------------|------|
| 1 | `10_010`（slot=2, idx=2）| warp2 |
| 2 | `10_001`（slot=2, idx=1）| warp1 |
| 3 | `10_000`（slot=2, idx=0）| warp0 |

回收时 `pending_wf_count[2]`：3→2→1→0，第三次归零后 `pending_wg_bitmap[2]=1`，触发该 WG 的 `wg_done`。注意派发序号是 `count-1` 递减（先发 idx=2，最后 idx=0）。这是静态推算（**待本地验证**：仿真中 dump `dispatch2cu_wf_tag_dispatch_o` 与 `pending_wf_count` 核对）。

#### 4.4.5 小练习与答案

**练习 1**：tag 里为什么必须包含 slot 号？只用 warp 序号会怎样？

**参考答案**：一个 CU 可同时驻留多个 WG（多个 slot 占用），每个 WG 内部都有「warp0/warp1/...」。若 tag 只有 warp 序号，回传完成信号时就无法区分「是 slot0 的 warp1 完成了，还是 slot1 的 warp1 完成了」，计数会错乱。slot 号提供了 WG 维度的寻址。

**练习 2**：cu_handler 的 `alloc_st` 和 `dealloc_st` 为什么是两个独立状态机而不是合并成一个？

**参考答案**：分配（往 CU 派 warp）与回收（CU 上 warp 跑完）在时间上完全异步——CU 一边在接收新 WG 的 warp 派发，另一边已有 WG 的 warp 陆续完成。两者用独立状态机并行处理、用各自的 always 块维护，并通过 `used_slot_bitmap`/`pending_wg_bitmap`/`info_ram` 共享状态，避免互相阻塞。

---

## 5. 综合实践：跟踪一个 WG 从派发到 wf_done 的完整闭环

本实践把四个模块串起来，亲手走一遍「派发字段下行 → warp 完成上行」的全链路。这是本讲最重要的练习。

**实践目标**：能够在源码中标注出下列每个环节的精确位置，并解释信号如何在模块间流转。

### 任务 A：下行派发链（host → SM）

按顺序在源码中定位并说明每一步：

1. **主机入队**：`host2cta_*` → `inflight_wg_buffer` 写入 waiting/ready 双表（[inflight_wg_buffer.v:396-401](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/inflight_wg_buffer.v#L396-L401)）。
2. **指挥启动**：`dis_controller` 发 `start_alloc`（[dis_controller.v:85-89](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/dis_controller.v#L85-L89)）。
3. **选 CU**：allocator/resource_table 决定 `allocator_cu_id_out`（u2-l1 已讲）。
4. **回成功脉冲**：`dis_controller` 发 `wg_alloc_valid`（[dis_controller.v:163-166](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/dis_controller.v#L163-L166)），inflight_wg_buffer 据此从 ready 表读出派发字段并拉 `gpu_valid`（[inflight_wg_buffer.v:529-543](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/inflight_wg_buffer.v#L529-L543)）。
5. **通知工位**：`gpu_interface` 置 `handler_wg_alloc_en[cu_id]`（[gpu_interface.v:281-284](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/gpu_interface.v#L281-L284)）。
6. **生成 tag、逐 warp 派发**：`cu_handler` 找 slot、每拍发 tag（[cu_handler.v:218-228](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/cu_handler.v#L218-L228)）；`gpu_interface` 组装字段并递增基址（[gpu_interface.v:317-347](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/gpu_interface.v#L317-L347)）。
7. **广播到 SM**：`cta_interface` 把单路 `dispatch2cu_*` 广播成 `NUMBER_CU` 路 `cta2warp_*`，靠 `dispatch2cu_wf_dispatch` 的 one-hot 点选目标 SM（[cta_interface.v:143-160](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v#L143-L160)）。

**预期产出**：一张标注了上述 7 步、每步附文件:行号的下行链路图。

### 任务 B：上行回收链（SM → host）

1. **SM 回报单 warp 完成**：`warp2cta_valid_i` + `warp2cta_cu2dispatch_wf_tag_done_i` → 经 `cta_interface` 接成 `cu2dispatch_wf_done_i` / `cu2dispatch_wf_tag_done_i`（[cta_interface.v:165-167](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v#L165-L167)）。
2. **逐 warp 计数**：`cu_handler` 按 tag 的 slot 号把 `pending_wf_count[slot]` 减一（[cu_handler.v:306-309](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/cu_handler.v#L306-L309)），归零置 `pending_wg_bitmap`。
3. **本 CU 汇总成 wg_done**：dealloc 状态机读 info_ram、发 `wg_done_valid`+`wg_id`（[cu_handler.v:246-255](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/cu_handler.v#L246-L255)）。
4. **多 CU 间仲裁**：`gpu_interface` 用 `fixed_pri_arb` 选一个完成的 CU，拉 `dealloc_available`+`dealloc_wg_id`（[gpu_interface.v:396-403](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/gpu_interface.v#L396-L403)）。
5. **指挥确认回收**：`dis_controller` 发 `wg_dealloc_valid`（[dis_controller.v:152-156](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/dis_controller.v#L152-L156)），同时让 resource_table 释放资源。
6. **触发主机完成脉冲**：`inflight_wg_buffer` 拉起 `host_wf_done`+`wf_done_wg_id`（[inflight_wg_buffer.v:289-292](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/inflight_wg_buffer.v#L289-L292)）。
7. **FIFO 缓冲后交主机**：`wf_done_interface_single` 用 `stream_fifo`（深 `WG_NUM_MAX`）排队，以 valid/ready 握手交主机 `cta2host_valid_o`（[wf_done_interface_single.v:30-44](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/wf_done_interface_single.v#L30-L44)；连线见 [cta_interface.v:130-138](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v#L130-L138)）。

> 关于「派发节奏」的补充：本讲的四个核心模块中，节奏由三处共同控制——`dis_controller` 的组锁 `cu_groups_allocating`、`inflight_wg_buffer` 的 RR 轮转 `last_chosen_entry_rr`、`gpu_interface` 受 `ready_for_dispatch2cu_i` 门控的逐 warp 派发。题目中提到的 `throttling_engine` 在仓库中确有同名文件（[throttling_engine.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/throttling_engine.v)，用于按 `wg_count_available` 限流），但当前 `cta_scheduler.v` 的派发主链路并未例化它，故本讲不展开；`prefer_select`（allocator 在候选 CU 中轮转挑一个）属于 u2-l1 的 allocator 范畴。

**预期产出**：一张完整的「SM warp 完成 → 主机 wf_done」上行链路图，并写一段话说明为什么回收链上要加一个 `stream_fifo`（提示：多个 CU 可能短时间内集中完成多个 WG，而主机侧的 `cta2host_ready_i` 握手不一定能立刻消费，FIFO 起到削峰缓冲作用）。

> **运行验证（可选）**：在 `testcase/test_gpgpu_axi_top/` 下选一个用例（如 `tc_vecadd`），按 u1-l4 的方法 `make run-vcs-4w4t`，用 Verdi 打开 `test.fsdb`，在上述下行/上行信号上打波形，观察一次完整 WG 的派发与回收时序。若环境不具备，以上「源码阅读型实践」已可独立完成。

## 6. 本讲小结

- `dis_controller` 是**纯控制**模块：用四态机串起「请求分配→等结果→回 ACK」，用 `cu_groups_allocating` 组锁做互斥，且**回收优先于分配**。
- `inflight_wg_buffer` 用 **waiting/ready 双表 RAM** 缓存主机 WG 元数据，表满则反压主机；用 **RR 轮转**（`last_chosen_entry_rr`）公平选择下一个待分配 WG。
- `gpu_interface` 是**派发字段组装车间**：把 allocator 的决定与 buffer 的元数据拼成对 CU 的握手，逐 warp 派发并**递增 VGPR/SGPR 基址**；用 generate 例化 `NUMBER_CU` 个 `cu_handler`，并在多 CU 间仲裁回收。
- `cu_handler`（每 CU 一个）管理 **WF/WG slot**，生成唯一 **tag = {slot, warp 序号}**，逐 warp 计数完成，本 WG 全部 warp 完成时上报 `wg_done`。
- SM 侧收到的派发字段里**不含 wg_id**（硬连线为 0），WG 身份由 `start_pc`、各 base 与 tag 隐式携带。
- 回收链末端用 `wf_done_interface_single` 的 **stream_fifo** 对 WG 完成事件削峰缓冲，再以 valid/ready 握手交主机。

## 7. 下一步学习建议

到这里，从「主机下发 WG」到「SM 收到 warp 派发」、再到「warp 完成回报主机」的**整条 CTA 调度链**已全部打通。下一步建议：

- **u2-l3（cta2warp 与 Warp 派发接口）**：本讲止步于 `cta2warp_*` 接口。下一讲进入 SM 侧，看 `cta2warp.v` 如何把 CTA 发来的「一个 WG 的逐 warp 派发」拆成 warp 级请求（`warpReq`）、分配 wid、并把 warp 完成信号回送（`warp2cta`）——这正是本讲 `cu2dispatch_wf_done_i` 信号的真正发源地。
- 若对调度公平性感兴趣，可对比阅读 allocator 中的 `prefer_select.v`（CU 侧轮转）与 `inflight_wg_buffer` 的 `last_chosen_entry_rr`（WG 侧轮转），体会「两级轮转」的设计。
- 若关注限流机制，可阅读 `throttling_engine.v` 与 `resource_table_group.v`，思考它为何在当前主链路中未被启用。
