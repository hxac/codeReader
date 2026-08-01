# CTA 调度器与资源表

> 本讲是「任务调度」单元的第一篇。前置讲义 [u1-l5](u1-l5-gpgpu-top-and-dataflow.md) 已经建立了顶层数据流：主机的 `host_req` 进入 `cta_interface`，再由调度器把 workgroup 派发到各 SM。本讲就钻进这个 `cta_interface` 内部，看一个 workgroup「凭什么」能被分到某一个 SM。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 **workgroup（WG）/ block、warp / wavefront（WF）、CU（即 SM）** 这三层调度概念，以及它们之间的数量关系。
- 画出 `cta_interface` → `cta_scheduler` → `allocator` + `resource_table` 的模块协作框图，指出主机请求、资源判定、CU 选择、派发握手、完成回报五条信号流各走哪条线。
- 解释 **资源表（VGPR / SGPR / LDS / WF slot / WG slot）** 如何约束一个 workgroup 能否被派发，以及「CAM 比较 + 链表式空闲区管理」这套机制的工作原理。
- 说出 `NUMBER_WF_SLOTS`、`NUMBER_CU`、`NUM_VGPR` 等参数对调度能力（并发在途 workgroup 数、可派发的 CU 数）的影响。

## 2. 前置知识

在进入源码前，先用 GPU 的通俗语言建立几个概念。这部分是后续读 Verilog 的「语义底座」。

### 2.1 三层调度概念

GPU 上跑的一段程序（kernel）会被划分成大量 **workgroup（WG，也叫 thread block / CTA）**。一个 workgroup 内部又包含若干 **warp（也叫 wavefront / WF，wavefront）**，一个 warp 内部再包含若干 **thread（线程）**。三者是「包含」关系：

```
kernel
 └── workgroup (WG / CTA / block)        ← 本讲主角：被调度的基本单位
      └── warp (WF / wavefront)          ← SIMT 执行的基本单位
           └── thread (NUM_THREAD 个)     ← 真正并行执行的 lane
```

本项目的 CTA 调度器来自开源项目 **MIAOW** 的 ultra-threads dispatcher（见 [README.md:209](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/README.md#L209)），它沿用了 MIAOW 的术语：**CU（Compute Unit）= 一个 SM 核**，**WF（wavefront）= warp**，**WG（workgroup）= CTA**。所以当你看到源码里的 `NUMBER_CU`、`cu_id`，对应的就是 SM 的数量与编号；`wf_count` 就是 warp 数。

### 2.2 为什么需要「资源表」

一个 workgroup 要在某个 SM 上跑起来，必须先占用该 SM 的几类硬件资源：

| 资源 | 缩写 | 说明 |
|------|------|------|
| 向量寄存器 | VGPR | 每个 warp 用的向量寄存器，全 SM 共享一大块 |
| 标量寄存器 | SGPR | 每个 warp 用的标量寄存器 |
| 共享内存 | LDS | workgroup 内线程通信的低延迟存储 |
| warp 槽位 | WF slot | SM 能同时驻留的 warp 数上限 |
| workgroup 槽位 | WG slot | SM 能同时驻留的 workgroup 数上限 |

这些资源都是 **有限的**。如果把一个需要 100 个 VGPR 的 workgroup 派发到一个只剩 50 个 VGPR 的 SM，程序就会跑错。所以调度器在派发前必须先「查账」——这就是 **资源表（resource table）** 的作用：它实时记录每个 SM 还剩多少各类资源，调度器据此判断「这个 workgroup 能不能放到这个 SM」。

### 2.3 一句话直觉

> **CTA 调度器 = 一个「收件 + 查库存 + 分配货架 + 回收」的仓库管理员。** 主机把 workgroup（包裹）送来，管理员查每个 SM（货架）还剩多少 VGPR/SGPR/LDS/槽位，挑一个放得下的货架把包裹摆上去；包裹处理完了再把占的资源还回来。

带着这个直觉，我们看源码。

## 3. 本讲源码地图

本讲涉及的关键文件，按「由外到内、由接口到机制」排列：

| 文件 | 作用 |
|------|------|
| [cta_interface.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v) | 调度子系统对外的「门面」：连接主机、例化调度器、把派发结果广播给所有 SM |
| [cta_scheduler.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/cta_scheduler.v) | 调度器顶层，例化 5 个子模块并把它们连起来 |
| [dis_controller.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/dis_controller.v) | 控制状态机，指挥「分配 / 回收 / 拒绝」的节奏 |
| [allocator_neo.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/allocator_neo.v) | CAM 比较器：把 WG 需求与每个 SM 的剩余资源对比，挑出一个能放下的 SM |
| [top_resource_table.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/top_resource_table.v) | 资源表顶层：把分配 / 回收命令路由到对应 SM 组 |
| [resource_table.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/resource_table.v) | 最底层：用链表管理单个 SM 单类资源的「空闲区」，找出最大可用块 |
| [define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v) | 所有规模与位宽参数的总开关 |

> 阅读建议：先看 `cta_interface.v` 建立外部接口观，再看 `cta_scheduler.v` 看清五个子模块怎么连，然后挑两条主线深挖——**分配主线**（`dis_controller` → `allocator` → `resource_table`）和 **回收主线**（`wf_done` → `resource_table` 回填）。`gpu_interface`、`inflight_wg_buffer` 的细节留到 [u2-l2](u2-l2-cu-handler-and-inflight-wg.md) 讲。

## 4. 核心概念与源码讲解

### 4.1 cta_interface：顶层接口与广播式 CU 分发

#### 4.1.1 概念说明

`cta_interface` 是整个 CTA 调度子系统对外的「门面模块」。它本身 **不做任何运算决策**，只做三件事：

1. 接收主机送来的 workgroup 描述（`host2cta_*` 一组信号）。
2. 把描述原样转交给内部的 `cta_scheduler` 去做调度决策。
3. 把调度结果（选了哪个 SM、派发参数）**广播** 给所有 SM，并用 one-hot 信号点选目标 SM；同时把 SM 的 warp 完成信号回送给调度器、把 WG 完成信号回报给主机。

理解它的关键，是看清「**一组**调度输出」如何变成「**NUM_SM 路**派发」。

#### 4.1.2 核心流程

```
            host2cta_valid_i / host2cta_*  (一组 WG 描述)
                          │
                          ▼
                   ┌─────────────┐
                   │ cta_scheduler│ ──► cta2host_rcvd_ack_o   (收到确认)
                   │             │ ──► host_wf_done          (WG 完成回报)
                   └──────┬──────┘
        dispatch2cu_* (一组，单路)  │  warp2cta_* (各 SM 的 warp 完成回报)
                          ▼
        ┌─────────────────────────────────┐
        │ generate 广播 (NUMBER_CU 路)     │
        │  cta2warp_valid_o[i] =           │  ← one-hot 点选目标 SM
        │     dispatch2cu_wf_dispatch[i]   │
        └─────────────────────────────────┘
                          │
                          ▼
                  各 SM 的 cta_req 接口
```

注意一个反直觉的设计：`cta_scheduler` 输出的派发参数（start_pc、vgpr_base、wf_count 等）是 **单路** 的（只描述「这次要派发的那个 WG」），但 `cta2warp_*_o` 是 **按 SM 展开的 `NUMBER_CU` 路总线**。`cta_interface` 用一个 `generate` 循环把同一组参数复制到所有 SM 上，再用 one-hot 的 `cta2warp_valid_o[i]` 决定真正激活哪一个 SM——这与 [u1-l5](u1-l5-gpgpu-top-and-dataflow.md) 讲的「广播 + one-hot 点选」一致。

#### 4.1.3 源码精读

**主机接口（输入侧）**——主机把一个 workgroup 的完整描述打包送来：

[cta_interface.v:21-37](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v#L21-L37) 描述了这组输入：`host_wg_id`（WG 编号）、`host_num_wf`（这个 WG 含几个 warp）、`host_wf_size`（每个 warp 多少线程）、`host_start_pc`（入口 PC），以及四类资源需求（`vgpr/sgpr/lds/gds_size_total` 与 `vgpr/sgpr_size_per_wf`）。这些正是后续资源判定的依据。

**例化调度器并转接主机信号**——`host2cta_*` 原样接入 `cta_scheduler` 的 `host_wg_*`：

```verilog
cta_scheduler cta_sche (
    .host_wg_valid_i (host2cta_valid_i),
    .host_wg_ready_o (host2cta_ready_o),
    .host_wg_id_i    (host2cta_host_wg_id_i),
    // ... 其余 host_* 一一对应 ...
);
```

见 [cta_interface.v:88-125](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v#L88-L125)。`cta2host_rcvd_ack_o` 直接取自调度器的 `inflight_wg_buffer_host_rcvd_ack`（[cta_interface.v:128](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v#L128)），表示「这个 WG 已被调度器收下」。

**广播派发（generate 循环）**——这是本模块最值得读的一段：

[cta_interface.v:140-163](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v#L140-L163) 对每个 SM `i`，把单路的 `dispatch2cu_*` 复制到第 `i` 路总线上，并把 one-hot 选择信号接上：

```verilog
assign cta2warp_valid_o[i] = cta_sche_dispatch2cu_wf_dispatch[i]; // one-hot 点选
assign cta2warp_dispatch2cu_wg_wf_count_o[...] = cta_sche_dispatch2cu_wg_wf_count;
// ... start_pc / vgpr_base / sgpr_base / lds_base / wf_tag ... 全部复制
```

其中 `dispatch2cu_wf_dispatch` 是 `NUMBER_CU` 位的 one-hot（由调度器内部选定的 `cu_id` 译码而来），所以只有目标 SM 的 `cta2warp_valid_o[i]` 为 1。

**完成回报的反向通路**——SM 侧 warp 跑完会拉起 `warp2cta_valid_i`，这里转接给调度器：

[cta_interface.v:165-167](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v#L165-L167)：

```verilog
assign cta_sche_cu2dispatch_wf_done            = warp2cta_valid_i;
assign cta_sche_cu2dispatch_wf_tag_done        = warp2cta_cu2dispatch_wf_tag_done_i;
assign cta_sche_cu2dispatch_ready_for_dispatch = cta2warp_ready_i;
```

WG 全部 warp 完成后，调度器经 `wf_done_interface_single`（一个深度为 `WG_NUM_MAX` 的 FIFO，见 [wf_done_interface_single.v:30-44](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/wf_done_interface_single.v#L30-L44)）把完成的 `wg_id` 排队回报主机。

#### 4.1.4 代码实践

**实践目标**：确认「单路调度输出 → 多路 SM 派发」的广播结构，并理解 one-hot 点选。

**操作步骤**（源码阅读型）：
1. 打开 [cta_interface.v:44-63](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v#L44-L63)，数一下 `cta2warp_*_o` 有多少路输出，它们的位宽都包含 `NUMBER_CU*` 因子。
2. 对照 [cta_interface.v:143](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v#L143)，确认只有 `cta2warp_valid_o[i]` 是按 `i` 区分的，其余参数字段对所有 `i` 都填同一个值。
3. 回答：如果 `dispatch2cu_wf_dispatch = 2'b10`（即 `NUMBER_CU=2`，选中 CU1），哪条 `cta2warp_valid_o` 会被拉高？

**预期现象 / 结果**：`cta2warp_valid_o[1] = 1`，`cta2warp_valid_o[0] = 0`；只有 SM[1] 收到有效派发，但 SM[0] 的参数总线也被填上了相同的值（只是 valid 为 0，SM[0] 会忽略）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cta2warp_dispatch2cu_*` 要做成 `NUMBER_CU` 路总线，而不是只连到被选中的那一个 SM？

> **答案**：硬件例化是静态的——每个 SM 的 `cta_req` 接口在综合时就固定连好了。调度器每周期动态选不同的 SM，只能靠「把参数广播给所有 SM + 用 one-hot 的 valid 点选目标」来实现动态路由，无法在运行时改接线。

**练习 2**：`cta2warp_dispatch2cu_wg_id_o[32*(i+1)-1-:32]` 被赋成 `'d0`（[cta_interface.v:160](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v#L160)），说明什么？

> **答案**：在这个 Verilog 版本里，下发给 SM 的 `wg_id` 字段目前恒为 0（未真正接通调度器的 wg_id）。SM 侧若依赖该字段需注意——这是一个「待完善」点，阅读时要留意实际驱动 SM 行为的是 `wf_tag` 等其它字段。

---

### 4.2 cta_scheduler 与 dis_controller：五模块协同与控制状态机

#### 4.2.1 概念说明

`cta_scheduler.v`（注释写「cta 模块顶层」）是调度器本体。它 **只做例化和连线**，把五个子模块搭成一个闭环：

- **inflight_wg_buffer**：收件箱。缓存主机送来的 WG，跟踪「在途（尚未跑完）」的 WG，是分配请求的源头。
- **allocator_neo**：选货员。把 WG 的资源需求与各 SM 剩余资源对比，挑出一个能放下的 SM（或判定放不下→拒绝）。
- **top_resource_table**：账本。维护每个 SM 各类资源的剩余量；每次分配 / 回收后更新账本，并把最新剩余量回填给 allocator 的比较器。
- **gpu_interface**：发货员。把分配结果（cu_id + 资源基址 + start_pc 等）组装成对 SM 的派发握手，并处理 warp 完成回收。
- **dis_controller**：调度长。用一个状态机指挥上述四个模块的先后顺序，避免同一个 SM 的账本被并发写坏。

本节重点讲 `dis_controller` 这条控制骨干；allocator 与 resource_table 的内部机制放到 4.3、4.4。

#### 4.2.2 核心流程

**分配（alloc）一个 WG 的完整往返**（状态机视角，来自 [dis_controller.v:49-52](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/dis_controller.v#L49-L52)）：

```
ST_AL_IDLE ──(alloc_valid & 有空闲组)──► ST_AL_ALLOC      start_alloc=1，启动 allocator
                                              │
                                   allocator_cu_valid=1
                                              ▼
ST_AL_ACK_PROPAGATION ◄────────────── ST_AL_HANDLE_RESULT  记下 alloc_waiting_cu_id，
        alloc_ack=1                       等资源表+gpu_interface 都 ready
        │                                 才发 wg_alloc_valid
        ▼
     ST_AL_IDLE  （allocator 收到 ack，清 pipeline_waiting，准备下一次）
```

控制器的三条核心输出语义：
- `dis_controller_wg_alloc_valid`：资源已扣减、SM 已准备好 → 真正派发。
- `dis_controller_wg_dealloc_valid`：某个 WG 全部 warp 完成 → 回收资源。
- `dis_controller_wg_rejected_valid`：所有 SM 都放不下这个 WG → 拒绝（退回 inflight_wg_buffer 等下次重试）。

**互斥保护**：`cu_groups_allocating`（[dis_controller.v:196-220](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/dis_controller.v#L196-L220)）按「资源表组」记录哪些组正在被写。状态机在 IDLE 态用 `!(&cu_groups_allocating)` 判断「是否所有组都空闲」才启动新分配（[dis_controller.v:85](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/dis_controller.v#L85)）；这个 busy 掩码最终通过 `dis_controller_cu_busy_o` 反馈给 allocator，让 allocator 在 AND 时排除正在写的 SM。

#### 4.2.3 源码精读

**五模块例化全景**：[cta_scheduler.v:123-325](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/cta_scheduler.v#L123-L325) 依次例化了 `allocator_neo`、`top_resource_table`、`inflight_wg_buffer`、`gpu_interface`、`dis_controller`。看连线就能看出数据流，例如 allocator 的输出 `allocator_cu_id_out` 同时送给 `top_resource_table`（去扣资源）和 `gpu_interface`（去派发），见 [cta_scheduler.v:151 与 264](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/cta_scheduler.v#L151)。

**控制状态机主循环**——IDLE 态启动分配：

```verilog
ST_AL_IDLE :
  if(inflight_wg_buffer_alloc_valid_i && !(&cu_groups_allocating)) begin
    alloc_st <= ST_AL_ALLOC;
    dis_controller_start_alloc_i <= 'h1;   // 启动 allocator
  end
```

见 [dis_controller.v:83-97](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/dis_controller.v#L83-L97)。`inflight_wg_buffer_alloc_valid_i` 表示「收件箱里有待分配的 WG」。

**等结果、发派发许可**——HANDLE_RESULT 态等到资源表与 gpu_interface 都可用才发 `wg_alloc_valid`：

```verilog
else if(gpu_interface_alloc_available_i && inflight_wg_buffer_alloc_available_i) begin
  dis_controller_wg_alloc_valid_i <= 'h1;
end
else if(allocator_cu_rejected_i) begin
  dis_controller_wg_rejected_valid_i <= 'h1;  // 放不下→拒绝
end
```

见 [dis_controller.v:157-172](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/dis_controller.v#L157-L172)。注意 dealloc（回收）有更高优先级，单独成支（[dis_controller.v:152-156](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/dis_controller.v#L152-L156)）。

**cu_busy 反馈**：`cus_allocating[i]` 由组级 busy 掩码按地址译出，再赋给 `dis_controller_cu_busy_o`（[dis_controller.v:222-228](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/dis_controller.v#L222-L228)），构成「分配→写账本→期间该 SM 不可再选」的闭环。

#### 4.2.4 代码实践

**实践目标**：用一个 `$display` 把每一次调度决策打印出来，直观看「谁被分给了哪个 SM」。

**操作步骤**：
1. 在 [dis_controller.v:163-166](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/dis_controller.v#L163-L166) 的 `wg_alloc_valid` 置 1 分支内，临时加一行（仅用于调试，勿提交）：
   ```verilog
   `ifdef DBG_CTA
     $display("[CTA] time=%0t alloc WG -> cu_id=%0d", $time, alloc_waiting_cu_id);
   `endif
   ```
2. 在 rejected 分支同样加打印 `"[CTA] REJECT"`。
3. 按 [u1-l4](u1-l4-simulation-and-testcases.md) 的方法，在 VCS 编译选项里加 `+define+DBG_CTA +define+CASE_4w4t`，跑 `tc_vecadd`。

**预期现象**：仿真 log 中应周期性地出现 `[CTA] alloc WG -> cu_id=0/1`，且数量与该用例的 workgroup 数一致；若 WG 资源需求大，可能出现少量 `[CTA] REJECT` 后重试。具体打印条数与周期 **待本地验证**（取决于用例的 WG 数与 `NUM_SM`）。

> 注意：本实践要求临时改源码做调试打印，**做完请还原**，不要把调试代码留在仓库里。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `dis_controller` 要等 `gpu_interface_alloc_available_i` 才发 `wg_alloc_valid`，而不是 allocator 一选出 CU 就立刻派发？

> **答案**：因为「选中 CU」只是第一步，还要等资源表把该 SM 的资源扣减完成（账本更新）、且 gpu_interface 的派发通路空闲，才能保证下发给 SM 的资源基址正确、握手不冲突。控制器在这里做同步，避免「选了但还没扣资源」的竞态。

**练习 2**：`cu_groups_allocating` 是按「资源表组」而非「单个 SM」记录的。本项目 `NUMBER_RES_TABLE=1` 时，这意味着什么？

> **答案**：`NUMBER_RES_TABLE=1` 表示所有 SM 共用同一个资源表组（见 [define.v:151](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L151)）。此时 `cu_groups_allocating` 只有 1 位，任何一次分配 / 回收都会锁住全组——即同一时刻只能处理一个 SM 的账本更新。这是一种简化，SM 数多时可增大 `NUMBER_RES_TABLE` 来分组并行。

---

### 4.3 allocator_neo：CAM 资源比较与目标 CU 选择

#### 4.3.1 概念说明

模块头注释一语中的：「**存储对应 SM 的剩余资源信息，并与该次 WG 所需要的资源进行对比**」（[allocator_neo.v:12](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/allocator_neo.v#L12)）。

它用的是 **CAM（Content-Addressable Memory，内容寻址存储）** 思路：给每个 SM 维护一项「剩余资源量」，查询时 **并行** 把「本次 WG 的需求」与所有 SM 的剩余量同时比较，凡「放得下」的 SM 对应位置 1，得到一个候选位掩码；再从候选里挑一个（由 `prefer_select` 做）。

allocator 共有 **5 个 CAM**，对应 5 类资源：VGPR、SGPR、LDS（这三类用 `cam_allocator_neo`，还要返回可用区起始地址）、WF slot、WG slot（这两类用 `cam_allocator`，只比数量）。一个 WG 必须 **五类资源在同一个 SM 上同时放得下**，才会被选中。

#### 4.3.2 核心流程

allocator 内部是一条小型流水线（受 `pipeline_waiting` 节流，一次只处理一个 WG，见 [allocator_neo.v:221-234](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/allocator_neo.v#L221-L234)）：

```
S1: 捕获需求      alloc_vgpr/sgpr/lds_size_reg ← WG 的资源需求; cu_busy_reg
        │
S2: CAM 查询      5 个 CAM 并行比较 → vgpr/sgpr/lds/wf/wg_search_out (每位=一个SM是否放得下)
        │
S3: 相与选候选     anded_cam_out = vgpr & sgpr & lds & wf & wg & (~cu_busy)
        │
S4: 选一个 CU      prefer_select(anded_cam_out, prefer=wg_id低位) → encoded_cu_id
        │             同时从各 CAM 的 start 向量里取出该 CU 的可用区起始地址
S5: 输出           cu_id_out, vgpr/sgpr/lds_start_out, *_size_out
                   若候选为空 → allocator_cu_rejected_o = 1
```

选 CU 的判定条件可以浓缩成一行布尔式（来自 [allocator_neo.v:298](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/allocator_neo.v#L298)）：

\[ \text{anded\_cam\_out}[i] = V_i \wedge S_i \wedge L_i \wedge W_i \wedge G_i \wedge \neg \text{cu\_busy}[i] \]

其中 \(V_i, S_i, L_i, W_i, G_i\) 分别表示 CU \(i\) 的 VGPR、SGPR、LDS、WF 槽、WG 槽是否放得下。任一为 0，该 CU 就出局。

#### 4.3.3 源码精读

**5 个 CAM 的例化**：[allocator_neo.v:145-219](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/allocator_neo.v#L145-L219)。VGPR/SGPR/LDS 用 `cam_allocator_neo`（带 `res_search_out_start_o`，返回该 SM 最大空闲块的起始地址），WF/WG 用普通 `cam_allocator`（只比大小）。注意 WG 槽的查询值恒为 1（`{{WG_SLOT_ID_WIDTH{1'b0}},1'b1}`，[allocator_neo.v:217](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/allocator_neo.v#L217)）——因为每个 WG 只占一个 WG 槽。

**CAM 的比较内核**（[cam_allocator_neo.v:54-74](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/cam_allocator_neo.v#L54-L74)）：

```verilog
if(!cam_valid_entry[i])               // 该 SM 从未被写过账本
  decoded_output[i] = 1'b1;           //   → 视为全空，放得下
else if(cam_ram[i] >= res_search_size_reg)  // 剩余 >= 需求
  decoded_output[i] = 1'b1;           //   → 放得下
else
  decoded_output[i] = 1'b0;
```

CAM 写入（账本回填）在 [cam_allocator_neo.v:82-86](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/cam_allocator_neo.v#L82-L86)：每次资源表更新后，把该 SM 的「最大空闲块大小与起始」写入对应项。

**相与选候选**：[allocator_neo.v:298](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/allocator_neo.v#L298) 把 5 个 CAM 输出与「非 busy」按位与。

**prefer_select 选 CU**：[allocator_neo.v:426-435](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/allocator_neo.v#L426-L435)。它以 `wg_id` 的低位作为「偏好起点」做 **轮转优先级**（[prefer_select.v:40-50](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/prefer_select.v#L40-L50)）：不同的 WG 偏好不同的 CU，从而把负载在多个 SM 间打散，而不是总选 CU0。

**未初始化 SM 的起始地址归零**：[allocator_neo.v:421-423](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/allocator_neo.v#L421-L423)。若选中的 CU 还从未被分配过（`!cu_initialized`），资源基址强制为 0——因为它的寄存器堆/共享内存从地址 0 开始是空的。

#### 4.3.4 代码实践

**实践目标**：手工模拟一次 CAM 比较，验证「放得下」的判定。

**操作步骤**（纯源码推导型）：
1. 假设 `NUMBER_CU=2`，CU0 剩余 VGPR=200、CU1 剩余 VGPR=80；本次 WG 需求 VGPR=100。
2. 按 [cam_allocator_neo.v:63-69](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/cam_allocator_neo.v#L63-L69) 的比较逻辑，写出 `vgpr_search_out` 的两位值。
3. 假设其它 4 类资源两个 CU 都放得下（全 1）、且无 busy，求 `anded_cam_out`。
4. 再读 [prefer_select.v:37](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/prefer_select.v#L37)，说明当 `wg_id=0` 时偏好哪个 CU、`wg_id=1` 时偏好哪个。

**预期结果**：`vgpr_search_out = 2'b01`（仅 CU0 满足 200≥100，CU1 的 80<100）；`anded_cam_out = 2'b01`；无论偏好如何，最终都选 CU0（因为只有它候选）。若把需求降到 50，则 `anded_cam_out = 2'b11`，此时 `wg_id` 低位决定偏好——CU 数为 2 时偏好位即 wg_id[0]。

#### 4.3.5 小练习与答案

**练习 1**：为什么对「从未写过的 SM」，CAM 比较直接返回「放得下」（`decoded_output[i]=1`）？

> **答案**：账本尚未写入意味着该 SM 还没被任何 WG 占用，资源是满的，自然放得下。配合 allocator 里 `cu_initialized` 的判断（基址归零），保证新 SM 第一次分配从地址 0 开始。

**练习 2**：5 个 CAM 为什么必须用「与」而不是「或」来合并？

> **答案**：WG 需要 5 类资源 **同时** 满足才能在一个 SM 上运行。只有「与」才表达「全部满足」；「或」会变成「只要一类资源够就行」，会把 WG 派到其实放不下的 SM。

---

### 4.4 resource_table 系列：链表式空闲资源管理与回填

#### 4.4.1 概念说明

CAM 只负责「查询」，真正维护「每个 SM 还剩多少资源」的是 **resource_table 系列**。它采用 MIAOW 的 **链表式空闲区管理**：把每个 SM 的某一类资源（如 VGPR 的 1024 个槽位）想象成一条一维地址空间，已分配的块用一条 **按起始地址排序的双向链表** 串起来；链表节点之间的「缝隙」就是空闲区。

分配一个新块时，把空闲区一分为二；回收时，把块从链表摘掉、愈合相邻的空闲区。每次操作后，状态机会 **遍历整条链表找出最大的那个空闲缝隙**（`cam_biggest_space_size`），这正是回填给 allocator CAM 的「剩余量」。

调用层次：`top_resource_table`（按 SM 组路由）→ `resource_table_group`（每类资源一个）→ `resource_table`（单类资源的链表管理器）。

#### 4.4.2 核心流程

**resource_table 的主状态机**（[resource_table.v:61-64](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/resource_table.v#L61-L64)）：

```
ST_M_IDLE ──alloc──► ST_M_ALLOC ──► ST_M_FIND_MAX ──► ST_M_IDLE
        └──dealloc──► ST_M_DEALLOC ──► ST_M_FIND_MAX ──► ST_M_IDLE
```

即：每次分配或回收完成后，都要进入 `ST_M_FIND_MAX` 重新计算最大空闲块，再把结果（`cam_biggest_space_size_o` / `_addr_o`，[resource_table.v:888-889](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/resource_table.v#L888-L889)）上报。

**find_max 如何算「最大空闲缝隙」**：遍历链表，对每两个相邻已分配块之间的空隙做减法：

\[ \text{gap} = \text{start}_{\text{next}} - (\text{start}_{\text{prev}} + \text{size}_{\text{prev}}) \]

取所有 gap 与「链表尾到资源末尾」空隙的最大值。若链表为空（SM 全空），直接返回 `NUMBER_RES_SLOTS`（全量）。

**回填闭环**：`resource_table_group` 把 VGPR/SGPR/LDS 三张表的结果汇成 `res_tbl_done`（三者都完成才算 done，[resource_table_group.v:347](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/resource_table_group.v#L347)）→ `top_resource_table` 经 `fixed_pri_arb` 仲裁后，把更新的 (size, start) 通过 `grt_cam_up_*`（[top_resource_table.v:239-247](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/top_resource_table.v#L239-L247)）回填给 allocator 的 CAM，同时发 `grt_wg_alloc_done` 通知 `dis_controller`（[top_resource_table.v:280-281](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/top_resource_table.v#L280-L281)）。

#### 4.4.3 源码精读

**RAM 条目布局**——链表节点长什么样（[resource_table.v:44-57](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/resource_table.v#L44-L57)）：每个条目含 `RES_STRT`（块起始）、`RES_SIZE`（块大小）、`PREV_ENTRY`（前驱节点 id）、`NEXT_ENTRY`（后继节点 id）。RAM 容量 `NUM_ENTRIES = NUMBER_CU * (NUMBER_WF_SLOTS+1)`（[resource_table.v:53](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/resource_table.v#L53)），按 `NUMBER_WF_SLOTS*cu_id + wg_slot` 寻址（[resource_table.v:875](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/resource_table.v#L875)）——即每个 (SM, WG槽) 组合占一项。

**find_max 状态机**——在 `ST_F_SEARCHING` 态计算相邻块间的空隙并取最大（[resource_table.v:530-560](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/resource_table.v#L530-L560)）；`ST_F_LAST_ITEM` 态再算「最后一个块到资源末尾」的空隙（[resource_table.v:562-573](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/resource_table.v#L562-L573)）。若 SM 全空则直接返回满量 `NUMBER_RES_SLOTS`（[resource_table.v:488-491](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/resource_table.v#L488-L491)）。

**三层例化关系**：
- `resource_table_group` 内为 VGPR/SGPR/LDS 各例化一个 `resource_table`（[resource_table_group.v:159-229](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/resource_table_group.v#L159-L229)），参数化在 `RES_ID_WIDTH` 与 `NUMBER_RES_SLOTS`（如 VGPR 用 `VGPR_ID_WIDTH` 与 `NUMBER_VGPR_SLOTS`）。
- `top_resource_table` 用 `generate` 为每个资源表组例化一个 `resource_table_group`（[top_resource_table.v:100-203](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/top_resource_table.v#L100-L203)），并按 CU id 高位把 alloc/dealloc 路由到对应组（[top_resource_table.v:102-103](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/top_resource_table.v#L102-L103)）。

**关键参数对照**（来自 [define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v)）：

| 参数 | 定义行 | 默认值 | 含义 |
|------|--------|--------|------|
| `NUMBER_CU` | [L149](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L149) | =`NUM_SM`=2 | 可派发的 SM 数 |
| `NUMBER_WF_SLOTS` | [L159](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L159) | =`NUM_BLOCK`=8 | 单个 SM 可同时驻留的 WG 数上限 |
| `NUMBER_VGPR_SLOTS` | [L153](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L153) | =`NUM_VGPR`=1024 | 单 SM 的 VGPR 槽总数 |
| `NUMBER_LDS_SLOTS` | [L157](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L157) | 131072（128kB） | 单 SM 的共享内存字节数 |
| `WG_SLOT_ID_WIDTH` | [L189](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L189) | =clog2(8)=3 | WG 槽编号位宽 |
| `WG_ID_WIDTH` | [L161](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L161) | 2+3+1=6 | WG 全局编号位宽 |
| `TAG_WIDTH` | [L199](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L199) | 3+4=7 | warp 标签（WG槽+warp号）位宽 |

其中 `WG_ID_WIDTH` 与 `TAG_WIDTH` 的组成尤其值得记住：

\[ \text{WG\_ID} = \text{高位} \;+\; \lceil\log_2 \text{NUMBER\_WF\_SLOTS}\rceil \;+\; \lceil\log_2 \text{NUMBER\_CU}\rceil \]

\[ \text{TAG} = \text{WG\_SLOT\_ID} \;+\; \text{WF\_COUNT\_PER\_WG} \]

`TAG` 用来在 warp 完成回报时（`warp2cta_cu2dispatch_wf_tag_done_i`）唯一标识「哪个 SM 的哪个 WG 槽里的第几个 warp」跑完了。

#### 4.4.4 代码实践

**实践目标**：把一个 WG 从 `host2cta_valid` 到「被选中分配给某 CU」的判定条件串成一条完整因果链。

**操作步骤**（源码跟踪型，回答「判定条件」）：
1. 从 [cta_interface.v:22](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v#L22) 的 `host2cta_valid_i` 出发，沿 `host_wg_valid_i` 进入 `cta_scheduler`，再经 `inflight_wg_buffer` 变成 `alloc_valid`。
2. 在 [dis_controller.v:85](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/dis_controller.v#L85) 写下启动分配的前置条件：`alloc_valid && !(&cu_groups_allocating)`。
3. 在 [allocator_neo.v:298](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/allocator_neo.v#L298) 写下 CU 被选中的五资源 + 非 busy 条件。
4. 在 [resource_table.v:488-491](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/resource_table.v#L488-L491) 确认「SM 全空时返回满量」，对应 CAM 里「未初始化=放得下」。

**需要观察 / 回答的判定条件清单**：
- **资源条件**：目标 CU 的 VGPR、SGPR、LDS 最大空闲块各 ≥ WG 需求，且 WF 槽 ≥ `num_wf`、WG 槽 ≥ 1。
- **slot 条件**：该 CU 的 WG 驻留数未超 `NUMBER_WF_SLOTS`、warp 驻留数未超 `WF_COUNT_MAX`。
- **tag 条件**：派发时分配唯一的 `wf_tag`（WG槽 + warp号），用于完成后回收资源。
- **互斥条件**：该 CU 所属资源表组当前未被占用（`~cu_busy`）。

**参数影响**：
- `NUMBER_WF_SLOTS`（默认 8）越大，单个 SM 可同时驻留越多 WG，资源表 RAM 越深、`find_max` 遍历越久；它直接决定 `WG_SLOT_ID_WIDTH` 与 RAM 容量。
- `NUMBER_CU`（默认 2，=`NUM_SM`）越大，CAM 比较位越宽、可并行的 SM 越多；它决定 `CU_ID_WIDTH` 与广播总线宽度。
- `NUM_VGPR`/`NUM_SGPR`/`NUMBER_LDS_SLOTS` 决定各资源表的地址空间大小与位宽。

#### 4.4.5 小练习与答案

**练习 1**：`resource_table` 的 RAM 为什么按 `NUMBER_WF_SLOTS*cu_id + wg_slot` 寻址，而不是简单地按 `cu_id`？

> **答案**：一个 SM 可以同时驻留多个 WG（最多 `NUMBER_WF_SLOTS` 个），每个 WG 占用各自的资源块（不同的起始地址和大小）。因此每个 (SM, WG槽) 组合都需要独立的链表节点来记录它占的那一块，RAM 必须按二维索引寻址。

**练习 2**：为什么每次 alloc/dealloc 后都要跑一遍 `find_max`，而不是直接维护一个「剩余量」计数器？

> **答案**：因为资源是「带地址」的——allocator 不仅要知道剩多少，还要知道最大连续空闲块的 **起始地址**（派发给 SM 作 vgpr_base 等）。简单计数器无法给出「最大连续空洞」的位置；遍历链表找最大缝隙才能同时给出 size 和 start。

**练习 3**：把 `NUMBER_WF_SLOTS` 从 8 改成 4，`WG_ID_WIDTH` 会怎样变化？

> **答案**：`WG_ID_WIDTH = 2 + clog2(NUMBER_WF_SLOTS) + clog2(NUMBER_CU)`（[define.v:161](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L161)）。`clog2(8)=3`、`clog2(4)=2`，故 `WG_ID_WIDTH` 从 6 变成 5。这会连带影响 `WG_NUM_MAX`、RAM 深度与 `TAG_WIDTH` 等下游参数。

---

## 5. 综合实践

**任务**：画一张完整的「CTA 调度全流程图」，并标注一次 WG 从到达到完成的每一个判定与握手。

要求在你的图里至少体现以下要素，并标注对应源码位置：

1. **入站**：`host2cta_valid_i`（[cta_interface.v:22](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v#L22)）→ `inflight_wg_buffer` 收件 → `host_rcvd_ack` 回主机。
2. **启动分配**：`dis_controller` 的 `ST_AL_IDLE→ST_AL_ALLOC`（[dis_controller.v:85-88](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/dis_controller.v#L85-L88)），条件含 `!(&cu_groups_allocating)`。
3. **CAM 比较**：5 个 CAM 相与（[allocator_neo.v:298](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta/allocator_neo.v#L298)）→ `prefer_select` 选 CU → `cu_id_out` 或 `cu_rejected`。
4. **扣资源 + 派发**：`wg_alloc_valid` → `top_resource_table` 扣减并 `find_max` → `grt_cam_up_*` 回填 CAM；`gpu_interface` 组装 `dispatch2cu_*`。
5. **广播**：`cta_interface` 的 generate 把单路输出展开成 `NUMBER_CU` 路，one-hot 点选目标 SM（[cta_interface.v:143](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cta_top/cta_interface.v#L143)）。
6. **完成回收**：warp 完成经 `warp2cta_valid` → 全部 warp done 触发 `wg_dealloc` → 资源表愈合链表 → `wf_done_interface_single` 把 `wg_id` 回报主机。

**进阶**（选做）：在图上用不同颜色标出「控制流」（dis_controller 的状态与握手）与「数据流」（资源量、start_pc、tag 的数值通路），体会这两条流是如何在 `dis_controller` 的同步点汇合的。

## 6. 本讲小结

- **三层概念**：workgroup（WG/CTA）→ warp（WF/wavefront）→ thread；CU 即 SM。本项目 CTA 调度器源自 MIAOW 的 ultra-threads dispatcher。
- **门面与广播**：`cta_interface` 把主机的 WG 描述转交 `cta_scheduler`，并把单路调度结果用 `generate` 广播成 `NUMBER_CU` 路、靠 one-hot 的 `cta2warp_valid_o[i]` 点选目标 SM。
- **五模块闭环**：`cta_scheduler` 例化 `inflight_wg_buffer`（收件）、`allocator_neo`（选 CU）、`top_resource_table`（账本）、`gpu_interface`（派发/回收）、`dis_controller`（指挥），构成「收件→查库存→选货→扣账→发货→回收」的闭环。
- **控制骨干**：`dis_controller` 用 `IDLE→ALLOC→HANDLE_RESULT→ACK_PROPAGATION` 状态机串起分配，并用 `cu_groups_allocating` 做互斥，避免账本被并发写坏。
- **CAM 选择**：`allocator_neo` 用 5 个 CAM 并行比较 5 类资源，相与得候选 SM 掩码，`prefer_select` 按 wg_id 轮转挑一个；放不下则 `cu_rejected`。
- **链表账本**：`resource_table` 用双向链表管理每个 (SM, WG槽) 的资源占用块，每次操作后 `find_max` 找出最大连续空闲块（size+start），回填给 CAM 并上报完成。

## 7. 下一步学习建议

本讲聚焦「调度器如何选 CU、如何判定资源」，但有意把两个子模块留到了下一讲：

- **[u2-l2 cu_handler 与 inflight_wg_buffer 派发流程](u2-l2-cu-handler-and-inflight-wg.md)**：深入 `inflight_wg_buffer` 如何跟踪在途 WG、`gpu_interface`/`cu_handler` 如何把分配结果组装成对 SM 的派发握手（start_pc、base、wf_size、tag 等字段的时序），以及 warp 完成信号 `wf_done` 如何回收。建议接着读 `gpu_interface.v`、`inflight_wg_buffer.v`、`cu_handler.v`。
- **[u2-l3 cta2warp 与 Warp 派发接口](u2-l3-cta2warp-interface.md)**：看 SM 侧的 `cta2warp.v` 如何把一个 WG 拆成若干 warp 请求、维护 wid 分配，这是调度器与 SM 流水线（u3 单元）之间的桥梁。

读源码时，建议带着本讲的「全流程图」对照，先沿「控制流」走一遍状态机，再沿「数据流」追一组资源数值，两遍下来调度子系统就能融会贯通。
