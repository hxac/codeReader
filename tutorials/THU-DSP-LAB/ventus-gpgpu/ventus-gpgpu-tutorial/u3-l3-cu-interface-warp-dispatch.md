# CU 接口、warp 拆分与释放

## 1. 本讲目标

本讲是 CTA 调度器单元（u3）的收尾篇。前两讲我们讲了调度器如何把一个 workgroup（WG）**选到**某个 CU（=SM），以及 `resource_table` 如何算出这个 WG 的资源基址。但「选到」并不等于「真正开跑」——硬件还要做两件事：

1. 把一个 WG **拆成若干个 warp（wavefront/WF）**，一个一个喂给 SM；
2. 等这些 warp 全部跑完后，**回收资源**并向 host 回报「这个 WG 完成了」。

这两件事都由 `cu_interface` 模块负责。而在 SM 一侧，接收 warp、给每个 warp 分配一个**硬件 warp id（wid）**的则是 `CTA2warp` 模块。

学完本讲，你应当能够：

- 说清 `cu_interface` 的三件核心工作：拼合 WG 信息 → 拆分 WF 派发 → 收集完成并释放/回报；
- 解释 `wf_tag` 的位域编码（`{wg_slot_id, wf_id}`），以及它为何是 CU 与 SM 之间识别 warp 的「身份证」；
- 描述 `cu_interface` 的三态有限状态机（`GET_WF / UPDATE / DEALLOC`）以及它为何必须用多周期状态机；
- 理解 `CTA2warp` 如何用位图 `idx_using` 分配/回收硬件 wid，以及如何把 wid 与 wf_tag 对应起来。

## 2. 前置知识

本讲假设你已经读过 **u3-l1（CTA 调度器总体与 wg_buffer）** 与 **u3-l2（资源表与分配器）**。这里只补两个本讲反复用到的小概念。

### 2.1 粒度转换：host 收发的是 WG，CU 收发的是 WF

回顾 u3-l1 的结论：调度器对 host 的接口粒度是 **WG（workgroup/CTA/block）**，对 CU 的接口粒度是 **WF（wavefront/warp）**。一个 WG 含若干个 WF（数量由 `num_wf` 指示，本仓库里一个 WF 固定含 `num_thread` 个线程）。这一「WG→WF」的粒度转换，正是发生在 `cu_interface` 内部。本讲就是讲清楚这个转换怎么做的。

### 2.2 DecoupledIO 握手与 SyncReadMem

- **DecoupledIO**：Chisel 的标准握手接口，`valid` 表示发送方有数据，`ready` 表示接收方能收，当 `valid && ready` 同时为真时称为一次 **fire（成功传输）**。
- **SyncReadMem**：同步读 RAM。给它一个读地址后，**下一拍**才能拿到数据，单拍内读不出。这个特性会逼着 `cu_interface` 用一个多周期状态机来做「读计数→比较→更新」。

### 2.3 本讲的默认规模参数

后文举例统一采用仓库默认配置（见 [ventus/src/top/parameters.scala:L7-L9](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L7-L9) 与 [ventus/src/top/parameters.scala:L53](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L53)）：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `num_sm`（= `NUM_CU`） | 2 | CU / SM 数量 |
| `num_warp`（= `NUM_WF_SLOT`） | 8 | 每个 CU 最多同时持有 8 个 warp（硬件 wid 0~7） |
| `num_thread` | 32 | 每个 warp 含 32 个线程 |
| `num_block`（= `NUM_WG_SLOT`） | 8 | 每个 CU 最多同时持有 8 个 WG 槽位 |
| `num_warp_in_a_block`（= `NUM_WF_MAX`） | 8 | 一个 WG 最多含 8 个 WF |

由此可推出一个关键派生量——**wf_tag 位宽**（见 [ventus/src/top/parameters.scala:L186-L188](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L186-L188)）：

\[ \text{WF\_TAG\_WIDTH} = \lceil\log_2 \text{NUM\_WG\_SL\_OT}\rceil + \lceil\log_2 \text{NUM\_WF\_MAX}\rceil = 3 + 3 = 6 \text{ (bit)} \]

默认配置下 `TAG_WIDTH = 6`，高 3 位是 `wg_slot_id`，低 3 位是 `wf_id`。后文会反复用到。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [ventus/src/cta/cu_interface.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala) | **本讲主角**：拼合 WG 信息、拆分 WF 派发、收集完成并触发释放/回报 |
| [ventus/src/pipeline/CTA2warp.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CTA2warp.scala) | **SM 侧主角**：接收 warp、分配硬件 wid、回报完成 |
| [ventus/src/cta/cta_scheduler.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cta_scheduler.scala) | 定义 `cu_interface` 用到的 IO bundle 与 trait（信息分包），以及顶层连线 |
| [ventus/src/top/GPGPU_top.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala) | 把 `cu_interface` 的输出（`cu_wf_new`）逐字段映射成 SM 收到的 `CTAreqData`，并例化 `CTA2warp` |
| [ventus/src/pipeline/warp_schedule.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/warp_schedule.scala) | 下游：把 `wf_tag` 切回 `wg_slot_id` / `wf_id`，用于 barrier 同步 |
| [ventus/src/pipeline/CSR.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala) | 下游：warp 启动时用 `wf_tag` 低位的 `wf_id` 计算 `CSR_TID` 等寄存器 |

> 提示：`cu_interface` 属于 `package cta`，`CTA2warp` 属于 `package pipeline`，两者分属调度器与 SM 两个子系统，靠 `GPGPU_top` 顶层把它们连起来。

## 4. 核心概念与源码讲解

### 4.1 cu_interface 的定位与六大职能

#### 4.1.1 概念说明

回顾 u3-l1 的调度器总体：`cta_scheduler_top` 内部有四个组件——`wg_buffer`、`allocator`、`resource_table_top`、`cu_interface`。前三个在 u3-l1/u3-l2 已经讲过：`wg_buffer` 缓存 WG、`allocator` 选 CU、`resource_table` 算基址。

`cu_interface` 是这条链路上的**最后一棒**，它夹在「上面三个组件」与「下面的若干个 CU」之间。源码顶部的注释把它的职责讲得很清楚（[ventus/src/cta/cu_interface.scala:L8-L13](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L8-L13)）：

> 1. split WG into WF and send them to CU one-by-one
> 2. gather WF of WG. After all WF of a WG finish, send dealloc request to ResourceTable and report to Host

展开成**六大职能**（与代码里 `Main function 1~6` 的分段一一对应）：

1. **拼合 WG 信息**：从 `wg_buffer`、`allocator`、`resource_table` 三处各拿一部分信息，拼成一份完整的 WG 描述。
2. **拆分 WG 为 WF**：按 `num_wf` 把一个 WG 拆开，逐个 WF 发给对应 CU。
3. **存档用于收尾**：把判定完成、释放、回报所需的信息存进 `SyncReadMem`。
4. **收集 WF 完成**：仲裁各 CU 回报的「某个 WF 跑完了」，统计每个 WG 已完成的 WF 数。
5. **触发资源释放**：某 WG 的 WF 全部完成时，向 `resource_table` 发 dealloc。
6. **向 host 回报**：同一时刻向 host 报告该 WG 完成。

#### 4.1.2 核心流程

`cu_interface` 的整体数据流可以画成两条方向相反的通路：

```
                  ┌──────────────────────── cu_interface ────────────────────────┐
  去程(派发WF)：   │ wg_buffer ─┐                                                ┌─→ CU#0 │
                  │ allocator ─┼→ [拼合] → Queue → [splitter拆WF] → 选CU ────────┼─→ CU#1 │
                  │ resource_  ─┘                                                └────────┘
                  │   table                                    ↓ 存档           ↑
                  │                                   wf_gather_ram(完成判定)   │ 回报WF完成
  回程(收集/释放)：│  CU#0 ─┐                                                      │
                  │  CU#1 ─┴→ [RRArbiter] → FSM(GET_WF→UPDATE→DEALLOC) ─┬→ rt_dealloc → resource_table
                  │                                                          └→ host_wg_done → host
                  └────────────────────────────────────────────────────────────────┘
```

- **去程**：三路信息先汇合（职能 1），进一个深度为 2 的小 FIFO，再由 `splitter` 逐拍拆成 WF 派发（职能 2），同时把收尾信息写进 `wf_gather_ram`（职能 3）。
- **回程**：各 CU 报回的 WF 完成信号经 `RRArbiter` 选一个（职能 4），由三态 FSM 在多拍内完成计数、比较、释放（职能 5）与回报（职能 6）。

#### 4.1.3 源码精读

**(a) IO 端口一览**（[ventus/src/cta/cu_interface.scala:L16-L25](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L16-L25)）：

```scala
val wgbuffer_wg_new = Flipped(DecoupledIO(new io_buffer2cuinterface))   // 来自 wg_buffer（host 侧执行信息）
val alloc_wg_new     = Flipped(DecoupledIO(new io_alloc2cuinterface))   // 来自 allocator（cu_id/wg_slot_id 等）
val rt_wg_new        = Flipped(DecoupledIO(new io_rt2cuinterface))      // 来自 resource_table（WG 基址）
val cu_wf_new   = Vec(NUM_CU, DecoupledIO(new io_cuinterface2cu))       // 派发给各 CU 的 WF
val cu_wf_done  = Vec(NUM_CU, Flipped(DecoupledIO(new io_cu2cuinterface))) // 各 CU 回报的完成 WF
val rt_dealloc  = DecoupledIO(new io_cuinterface2rt)                    // 向 resource_table 释放
val host_wg_done= DecoupledIO(new io_cta2host)                          // 向 host 回报
```

注意三个输入（`wgbuffer/alloc/rt`）携带的是**同一个 WG 的不同侧面**，必须三者同时 valid 才能拼合。这正是职能 1 的来源。

**(b) 三路信息为什么是「分包」的？** 这些 IO bundle 都是用 trait 拼出来的（定义在 [ventus/src/cta/cta_scheduler.scala:L28-L82](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cta_scheduler.scala#L28-L82)）。设计者按「信息由谁产生、给谁用」切分：

| trait | 产生者 | 关键字段 | 用途 |
|---|---|---|---|
| `ctainfo_host_to_cuinterface` | host | `num_sgpr_per_wf`、`num_vgpr_per_wf`、`num_pds_per_wf` | 拆 WF 时基址步进量 |
| `ctainfo_host_to_cu` | host | `start_pc`、`gds_base`、`pds_base`、`csr_kernel`、`num_wg_x/y/z`、`num_thread_per_wf` | WF 执行所需，原样转发给 CU |
| `ctainfo_alloc_to_cuinterface` | allocator | `cu_id`、`wg_slot_id`、`num_wf`、`*_dealloc_en` | 选目标 CU、收尾判定/释放 |
| `ctainfo_alloc_to_cu` | resource_table | `sgpr_base`、`vgpr_base`、`lds_base` | **WG 级**基址（拆 WF 时会改成 WF 级） |

这就是为什么需要三个输入——它们分别来自三个不同的生产者模块，而 `cu_interface` 把它们缝合成一份完整的 `cta_data`（[ventus/src/cta/cu_interface.scala:L43-L45](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L43-L45)）：

```scala
class cta_data extends Bundle
  with ctainfo_alloc_to_cuinterface with ctainfo_alloc_to_cu
  with ctainfo_host_to_cuinterface with ctainfo_host_to_cu {
  val wg_id = UInt(CONFIG.WG.WG_ID_WIDTH)
}
```

**(c) 三路汇合 = 一个 DecoupledIO 的 3-to-1 与门**（职能 1，[ventus/src/cta/cu_interface.scala:L55-L63](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L55-L63)）：

```scala
// 三路都 valid 且下游 FIFO 可收，才各自 ready、一起 fire
io.wgbuffer_wg_new.ready := io.alloc_wg_new.valid && io.rt_wg_new.valid && fifo.io.enq.ready
io.alloc_wg_new.ready    := io.wgbuffer_wg_new.valid && io.rt_wg_new.valid && fifo.io.enq.ready
io.rt_wg_new.ready       := io.wgbuffer_wg_new.valid && io.alloc_wg_new.valid && fifo.io.enq.ready
fifo.io.enq.valid := io.wgbuffer_wg_new.valid && io.alloc_wg_new.valid && io.rt_wg_new.valid
```

汇合后压入 `Queue(new cta_data, 2)`（深度 2，[L46](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L46)）。DEBUG 模式下还会断言三路带的是同一个 `wg_id`（[L48-L53](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L48-L53)），防止三个组件对不齐。

> 小细节：汇合用 `viewAsSupertype` 把每一路的对应 trait 片段整体搬进 `cta_data`（[L60-L63](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L60-L63)），避免逐字段赋值漏字段。

**(d) 顶层如何连出这三个输入**：在 `cta_scheduler_top` 里，`cu_interface` 的三个输入分别接 `wg_buffer`、`allocator`、`resource_table`（[ventus/src/cta/cta_scheduler.scala:L129-L138](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cta_scheduler.scala#L129-L138)）：

```scala
wg_buffer_inst.io.cuinterface_wg_new     <> cu_interface_inst.io.wgbuffer_wg_new
allocator_inst.io.cuinterface_wg_new     <> cu_interface_inst.io.alloc_wg_new
resource_table_inst.io.cuinterface_wg_new<> cu_interface_inst.io.rt_wg_new
resource_table_inst.io.dealloc           <> cu_interface_inst.io.rt_dealloc
```

这条连线印证了 u3-l2 讲过的结论：`resource_table` 把算好的 WG 基址经 `cuinterface_wg_new` 送给 `cu_interface`，而 `cu_interface` 在 WG 完成后又经 `dealloc` 把资源还回去——一来一回，闭环。

#### 4.1.4 代码实践

**实践目标**：在不跑仿真的前提下，靠读源码验证「三路输入确实属于同一个 WG、且字段互补不重叠」。

**操作步骤**：

1. 打开 [ventus/src/cta/cta_scheduler.scala:L28-L82](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cta_scheduler.scala#L28-L82)，把四个 trait 的字段抄进一张表。
2. 打开 [ventus/src/cta/cu_interface.scala:L43-L63](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L43-L63)，对照 `cta_data` 用了哪几个 trait。
3. 检查 `io_buffer2cuinterface`（[wg_buffer.scala:L20-L23](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/wg_buffer.scala#L20-L23)）、`io_alloc2cuinterface`（[allocator.scala:L18-L20](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L18-L20)）、`io_rt2cuinterface`（[allocator.scala:L21-L23](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L21-L23)）各自混入了哪些 trait。

**需要观察的现象**：三个输入 bundle 混入的 trait 集合**两两不重叠**，并集恰好等于 `cta_data` 的全部字段；只有 `wg_id` 在三处都出现（所以 DEBUG 断言能比对）。

**预期结果**：你会得到一张「字段无冗余、刚好覆盖」的分配表，这解释了为什么必须三路齐备才能拼出一个 WG。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `Queue(new cta_data, 2)` 的深度改成 1，会有什么隐患？

> **答案**：深度 1 时 FIFO 几乎没有缓冲，三路汇合的 fire 频率会被下游 splitter 的消费速度紧紧卡住；更关键的是 splitter 拆一个 WG 需要多拍（每个 WF 一拍），深度 1 会让上一个 WG 还没拆完时，上游三个组件无法把下一个 WG 推进来，降低并发。深度 2 给了 1 个 WG 的缓冲余量。

**练习 2**：DEBUG 断言（[L49-L52](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L49-L52)）为什么只比较 `wg_id`，不比较 `num_wf`？

> **答案**：`num_wf` 同时出现在 `ctainfo_alloc_to_cuinterface`（allocator 路）和（经 `cta_data`）会被使用，但 host 路（`io_buffer2cuinterface`）并不携带 `num_wf`，所以三路里只有部分路有 `num_wf`，无法三路同字段比较；而 `wg_id` 三路都带，是唯一能对齐身份的字段，故只比它。

---

### 4.2 WG→WF 拆分与 wf_tag 编码

#### 4.2.1 概念说明

一个 WG 含 `num_wf` 个 WF。`cu_interface` 不能把整个 WG 一次性塞给 CU——CU 的接口（`io_cuinterface2cu`）一次只收一个 WF。所以需要一个**拆分器（splitter）**：从 FIFO 取出一个 WG，然后连续 `num_wf` 拍，每拍吐出一个 WF 给指定 CU。

每吐出一个 WF，要解决两个问题：

1. **基址递增**：`sgpr/vgpr/pds` 这些寄存器资源是**按 WF 划分**的（每个 WF 占用自己的一段），所以每派发一个 WF，基址要加上「每个 WF 占用的数量」。唯独 `lds`（局部数据共享）是整个 WG 共享的，基址**不递增**。
2. **wf_tag 身份证**：CU 一侧需要知道「这个 WF 属于哪个 WG 的第几个 WF」。这用 `wf_tag` 编码，它是后续 barrier 同步、完成回报的关键标识。

#### 4.2.2 核心流程

splitter 的本质是一个**倒计数器 + 一组步进寄存器**：

```
splitter_load_new = (splitter_cnt==0) && fifo.deq.valid   // 装载一个新 WG
  ├─ splitter_cnt      ← num_wf            // 倒计数器置为该 WG 的 WF 数
  ├─ splitter_sgpr_addr ← sgpr_base(WG级)  // 各基址复位到 WG 起点
  ├─ splitter_vgpr_addr ← vgpr_base(WG级)
  └─ splitter_pds_addr  ← pds_base(WG级)

每拍若 cu_wf_new(cu_id).fire（成功派发一个 WF）：
  ├─ splitter_cnt      ← splitter_cnt - 1
  ├─ splitter_sgpr_addr += num_sgpr_per_wf
  ├─ splitter_vgpr_addr += num_vgpr_per_wf
  ├─ splitter_pds_addr  += num_pds_per_wf
  └─ wf_tag ← {wg_slot_id,  wf_id = num_wf - splitter_cnt}
                                   └─ 第一发=0，第二发=1，…
```

`fifo.io.deq.ready := (splitter_cnt===1) && ...`（[L77](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L77)）是个精妙之处：FIFO **直到最后一个 WF（cnt==1）被取走才释放**这一项，保证拆分期间 `fifo.io.deq.bits` 这份 WG 信息保持稳定，splitter 多拍读的都是同一份数据。

#### 4.2.3 源码精读

**(a) 倒计数器与步进寄存器**（[ventus/src/cta/cu_interface.scala:L71-L97](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L71-L97)）：

```scala
val splitter_cnt = RegInit(0.U(log2Ceil(NUM_WF_MAX + 1).W))   // 还剩几个 WF 待发
val splitter_lds_addr  = WireInit(fifo.io.deq.bits.lds_base)   // LDS 不步进，用 Wire 直通
val splitter_sgpr_addr = Reg(UInt(log2Ceil(NUM_SGPR).W))       // 步进寄存器
val splitter_vgpr_addr = Reg(UInt(log2Ceil(NUM_VGPR).W))
val splitter_pds_addr  = Reg(UInt(MEM_ADDR_WIDTH))             // 步进寄存器
val splitter_load_new  = (splitter_cnt === 0.U) && fifo.io.deq.valid

splitter_cnt := MuxCase(0.U, Seq(
  (splitter_cnt =/= 0.U) -> Mux(io.cu_wf_new(cu_id).fire, splitter_cnt - 1.U, splitter_cnt),
  (splitter_load_new)    -> (fifo.io.deq.bits.num_wf),
))
// sgpr/vgpr/pds 三个步进寄存器同理：装载时复位，fire 时加步进量
```

注意 `splitter_lds_addr` 是 `Wire` 而非 `Reg`——它直接等于 WG 的 `lds_base`，所有 WF 共用同一个 LDS 基址，正对应「LDS 是 WG 内共享」的语义。

**(b) 派发与 wf_tag 构造**（[ventus/src/cta/cu_interface.scala:L99-L113](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L99-L113)）：

```scala
for(i <- 0 until NUM_CU) {
  io.cu_wf_new(i).valid := (splitter_cnt =/= 0.U) && (fifo.io.deq.bits.cu_id === i.U)
  // ... 转发 host/alloc 信息，注入拆分后的 WF 级基址 ...
  io.cu_wf_new(i).bits.sgpr_base := splitter_sgpr_addr
  io.cu_wf_new(i).bits.vgpr_base := splitter_vgpr_addr
  io.cu_wf_new(i).bits.lds_base  := splitter_lds_addr
  io.cu_wf_new(i).bits.pds_base  := splitter_pds_addr
  io.cu_wf_new(i).bits.wf_tag := { val wftag = Wire(new wftag_datatype)
    wftag.wg_slot_id := fifo.io.deq.bits.wg_slot_id      // 高位：WG 在 CU 里的槽位号
    wftag.wf_id      := fifo.io.deq.bits.num_wf - splitter_cnt  // 低位：本 WF 在 WG 内的序号
    wftag.asUInt
  }
}
```

`valid` 同时绑定了「还有 WF 要发」和「目标 CU 就是 i」两个条件，所以同一个周期只会有一个 CU 的 `cu_wf_new` 有效。

**(c) wf_tag 的位域布局**。`wftag_datatype` 定义在 [L34-L37](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L34-L37)：

```scala
class wftag_datatype extends Bundle {
  val wg_slot_id = UInt(log2Ceil(NUM_WG_SLOT).W)   // 3 bit
  val wf_id      = UInt(log2Ceil(NUM_WF_MAX).W)    // 3 bit
}
```

当它 `.asUInt` 后，**低位是 `wf_id`、高位是 `wg_slot_id`**（这与源码注释 `WF tag = cat(wg_slot_id_in_cu, wf_id_in_wg)` 一致，见 [parameters.scala:L186](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L186)）。这个布局不必死记 Chisel 的拼接规则，**下游的切片用法就是证据**：

- `warp_schedule.scala` 把它切回两段（[L112-L115](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/warp_schedule.scala#L112-L115)）：
  ```scala
  val WF_ID_WIDTH = log2Ceil(num_warp_in_a_block)                 // = 3
  val new_wg_id = ...dispatch2cu_wf_tag_dispatch(TAG_WIDTH-1, WF_ID_WIDTH)  // [5:3] = wg_slot_id
  val new_wf_id = ...dispatch2cu_wf_tag_dispatch(WF_ID_WIDTH-1, 0)          // [2:0] = wf_id
  ```
- `CSR.scala` 用低 `depth_warp`=3 位算线程起始号（[L302](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L302)、[L312](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L312)、[L315](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CSR.scala#L315)）：
  ```scala
  wf_tag_dispatch := io.CTA2csr.bits.CTAdata.dispatch2cu_wf_tag_dispatch(depth_warp-1, 0) // 取低3位=wf_id
  threadid := wf_tag_dispatch << depth_thread   // CSR_TID = wf_id × num_thread
  io.lsu_tid := wf_tag_dispatch * num_thread.asUInt
  ```

两处都默认「低 3 位 = wf_id」，反向印证了 `asUInt` 的布局。这也呼应 u2-l1 的结论：`CSR_TID = wf_id_in_wg × num_thread`。

#### 4.2.4 代码实践

**实践目标**：手工推演一个「含 2 个 WF 的 WG」被拆分时，每个 WF 携带的关键字段值。

**操作步骤**：假设某 WG 被分配到 `cu_id=1`、`wg_slot_id=3`、`num_wf=2`，WG 级基址 `sgpr_base=100`、`vgpr_base=200`、`pds_base=0x1000`，且 `num_sgpr_per_wf=16`、`num_vgpr_per_wf=32`、`num_pds_per_wf=64`。

1. 仿照 [L79-L94](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L79-L94) 列出 `splitter_cnt`、`splitter_*_addr`、`wf_tag` 在装载拍、第一发 fire 后、第二发 fire 后的取值。

**需要观察/推导的现象**：

| 时刻 | splitter_cnt | sgpr_base | vgpr_base | pds_base | wf_id | wf_tag(6'b) |
|---|---|---|---|---|---|---|
| 装载（load_new） | 2 | 100 | 200 | 0x1000 | （待发） | — |
| 第 1 发 fire 时 | 2→1 | 100 | 200 | 0x1000 | 2-2=0 | {3,0}=6'b011_000=0x18 |
| 第 2 发 fire 时 | 1→0 | 116 | 232 | 0x1040 | 2-1=1 | {3,1}=6'b011_001=0x19 |

> （`wf_id = num_wf - splitter_cnt`，注意它用的是 fire **当时**的 `splitter_cnt`，故第 1 发为 2-2=0、第 2 发为 2-1=1。）

**预期结果**：两个 WF 的 `wf_tag` 分别是 `0x18`（wg_slot=3,wf=0）与 `0x19`（wg_slot=3,wf=1）；两个 WF 的 `sgpr_base` 分别是 100、116，互不重叠；`lds_base` 两发相同（共享）。若你的推导与此一致，就说明你理解了拆分器。

**待本地验证**：上表的绝对数值依赖 `sgpr_base` 的实际硬件值，本仓库不提供单拍波形工具，故数值推导以源码逻辑为准；如需在波形中核对，可在仿真时抓取 `cu_interface_inst.io.cu_wf_new(1).bits` 信号（见 4.4 综合实践）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `splitter_lds_addr` 是 `Wire` 而 `splitter_sgpr_addr` 是 `Reg`？

> **答案**：LDS 是整个 WG 共享的，所有 WF 用同一个基址，不需要逐拍累加，所以直接用组合逻辑直通 `fifo.io.deq.bits.lds_base`（Wire）；而 sgpr/vgpr/pds 是每 WF 独占一段，基址要随派发逐拍累加，必须用 Reg 保存上一次的值。

**练习 2**：若某 WG 的 `num_wf` 比 `NUM_WF_MAX` 还大，会发生什么？

> **答案**：`num_wf` 字段宽度是 `log2Ceil(NUM_WF_MAX+1)`（见 trait 定义），硬件上根本无法表达超过 `NUM_WF_MAX` 的值；且 `splitter_cnt` 宽度也是 `log2Ceil(NUM_WF_MAX+1)`。所以这种 WG 在更上层（allocator 的容纳性判定）就会被拒绝，`cu_interface` 内部假设 `num_wf ≤ NUM_WF_MAX`，断言 [L95-L96](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L95-L96) 也据此检查基址不越界。

---

### 4.3 完成收集、资源释放与回报

#### 4.3.1 概念说明

派发出去的 WF 终究会跑完。CU 每跑完一个 WF，就通过 `cu_wf_done` 回报一个 `wf_tag`。`cu_interface` 要做的事是：

1. **仲裁**：`NUM_CU` 个 CU 可能同时有 WF 完成，用 `RRArbiter` 选一个处理。
2. **计数**：对每个（CU, wg_slot）维护一个「已完成 WF 数」计数器，每收到一个完成就 +1。
3. **判定完成**：当计数 `+1 == num_wf`（这个 WG 应有的 WF 数），说明整个 WG 跑完了。
4. **释放 + 回报**：WG 完成时，向 `resource_table` 发 dealloc、向 host 发 `host_wg_done`。

这里有个**时序难点**：完成计数存在 `SyncReadMem` 里，给地址后要下一拍才出数据，所以「读计数→比较→写回」拆不开，必须用**多周期状态机**。

#### 4.3.2 核心流程

三态 FSM（[ventus/src/cta/cu_interface.scala:L142-L147](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L142-L147)）：

```
GET_WF ：arbiter 选一个 cu_wf_done，fire → 同时发起两笔 SyncReadMem 读
          （读已完成计数 wf_gather_cnt、读 WG 存档 wf_gather_ram）→ 转 UPDATE

UPDATE ：（数据已到）已完成计数 +1，
          若 +1 == num_wf  → wf_gather_finish=1（WG 完成）
          ├ 写回计数（完成则清 0，否则写 +1）
          ├ 尝试同时发 rt_dealloc 与 host_wg_done
          │   两个都 fire → 回 GET_WF
          │   否则        → 转 DEALLOC
          └ 若未完成 → 回 GET_WF

DEALLOC：补发上一拍没成功的（dealloc / host_wg_done），
          两个 ok 标志都置位 → 回 GET_WF
```

`DEALLOC` 态的存在是为了应对「`rt_dealloc` 和 `host_wg_done` 的下游没在同一拍都 ready」的情况——用 `rt_dealloc_ok`、`host_wg_done_ok` 两个锁存位分别记录各自是否已成功，确保释放与回报都**不会丢**。

#### 4.3.3 源码精读

**(a) 存档与初始化闸门**。派发 WG 的同时，把收尾要用的信息（`num_wf`、`wg_id`、三个 `*_dealloc_en`）写进 `wf_gather_ram`（职能 3，[L127-L136](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L127-L136)）。地址用「全局 wg_slot 号」`cu * NUM_WG_SLOT + wg_slot`（[L129](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L129)）。

完成计数器 `wf_gather_cnt` 也是 `SyncReadMem`，上电时**必须先清零**。这段初始化逻辑同时充当了整个调度器的启动闸门 `init_ok`（[L165-L168](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L165-L168)）：

```scala
val init_wf_gather_cnt_idx = RegInit(0.U(...))
init_wf_gather_cnt_idx := Mux(init_wf_gather_cnt_idx === (NUM_CU*NUM_WG_SLOT).U, ..., +1.U)
io.init_ok := (init_wf_gather_cnt_idx === (NUM_CU * NUM_WG_SLOT).U)
```

这个 `init_ok` 被 `cta_scheduler_top` 用来**门控** host 输入（[cta_scheduler.scala:L122-L124](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cta_scheduler.scala#L122-L124)）：初始化未完成前，`host_wg_new` 的 valid 被强制压低，调度器不收任何 WG。这正是 u3-l1 提到的「init_ok 闸门」的真正来源——它在 `cu_interface` 里。

**(b) GET_WF：仲裁 + 发起读**（职能 4，[L154-L176](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L154-L176)）：

```scala
val arb_inst = Module(new RRArbiter[io_cu2cuinterface](new io_cu2cuinterface, NUM_CU))
arb_inst.io.out.ready := (fsm === FSM.GET_WF)
// fire 的同一拍发起两笔 SyncReadMem 读（下一拍出数据）
val wf_gather_cnt_read_data = wf_gather_cnt.read(global_wgslot_calc(wf_cu_wire, wf_wgslot_wire), arb_inst.io.out.fire)
val wf_gather_ram_read_data = ... // 还要用 RegNext/RegEnable 把读出数据多保持几拍
```

`wf_gather_ram_read_data` 那段 hold 逻辑（[L172-L176](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L172-L176)）很关键：UPDATE 和 DEALLOC 可能跨多拍，而 SyncReadMem 只在 GET_WF fire 那拍发起读，所以要把读出值锁存住供后续几拍用。

**(c) UPDATE：判定完成 + 写回计数**（[L179-L194](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L179-L194)）：

```scala
val wf_gather_finish = (wf_gather_cnt_read_data + 1.U === wf_gather_ram_read_data.num_wf)
val wf_gather_cnt_write_data = Mux(...,
  Mux(wf_gather_finish, 0.U, wf_gather_cnt_read_data + 1.U))  // 完成则清0，否则+1
```

注意 `wf_gather_finish` 用的是「读出值 +1 == num_wf」，即「**这次完成的是最后一个 WF**」。完成后计数清 0，该 wg_slot 就能被下一个 WG 复用。

**(d) UPDATE/DEALLOC：释放与回报**（职能 5、6，[L200-L223](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L200-L223)）：

```scala
// dealloc：仅在 WG 完成时发
io.rt_dealloc.valid := (fsm === FSM.UPDATE && wf_gather_finish) || (fsm === FSM.DEALLOC && !rt_dealloc_ok)
io.rt_dealloc.bits.cu_id := wf_cu_reg
io.rt_dealloc.bits.wg_slot_id := wf_wgslot_reg
io.rt_dealloc.bits.lds_dealloc_en := wf_gather_ram_read_data.lds_dealloc_en  // num_lds==0 时不必释放
...
// host 回报：同样仅在完成时发，回带 wg_id 与 cu_id（供调度策略研究）
io.host_wg_done.valid := (fsm === FSM.UPDATE && wf_gather_finish) || (fsm === FSM.DEALLOC && !host_wg_done_ok)
io.host_wg_done.bits.wg_id := wf_gather_ram_read_data.wg_id
io.host_wg_done.bits.cu_id := wf_cu_reg
```

`io_cta2host` 这个 bundle 特意带了 `cu_id`（[cta_scheduler.scala:L101-L104](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cta_scheduler.scala#L101-L104)），注释写明 `// For CTA schedule strategy research`——为上层研究「WG 落在哪个 CU」提供数据。

**(e) FSM 转移**（[L229-L236](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L229-L236)）：

```scala
fsm := MuxLookup(fsm.asUInt, FSM.GET_WF)(Seq(
  FSM.GET_WF.asUInt  -> Mux(arb_inst.io.out.fire, FSM.UPDATE, fsm),
  FSM.UPDATE.asUInt  -> MuxCase(FSM.DEALLOC, Seq(
                          !wf_gather_finish -> FSM.GET_WF,                         // WG 没跑完，回去继续收
                          (dealloc.fire && host.fire) -> FSM.GET_WF)),             // 都发出去了，结束
  FSM.DEALLOC.asUInt -> Mux(rt_dealloc_ok && host_wg_done_ok, FSM.GET_WF, fsm),   // 补发成功才结束
))
```

> 全局串起来：**WF 完成回报 → FSM 多拍处理 → 触发 `rt_dealloc` → resource_table 摘链回收资源 → 同时 `host_wg_done` 通知 host**。资源的前向分配（u3-l2）与反向回收（本节）在此闭环。

#### 4.3.4 代码实践

**实践目标**：跟踪「一个含 2 个 WF 的 WG，两个 WF 先后完成」时 FSM 与计数器的演化。

**操作步骤**：沿用 4.2.4 的 WG（`cu_id=1, wg_slot_id=3, num_wf=2`）。假设 WF#0 先完成、WF#1 后完成，两笔完成之间没有别的 WG 完成。

1. 对照 [L229-L236](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L229-L236) 与 [L179-L182](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L179-L182)，列出每个 FSM 状态切换与 `wf_gather_cnt`（地址 = global(1,3) = 1×8+3 = 11）的值。

**需要观察/推导的现象**：

| 事件 | FSM 路径 | 读出 cnt | +1 后 | finish? | 写回 cnt | 动作 |
|---|---|---|---|---|---|---|
| WF#0 完成 | GET_WF→UPDATE→GET_WF | 0 | 1 | 1==2? 否 | 1 | 仅计数，不发 dealloc |
| WF#1 完成 | GET_WF→UPDATE→（DEALLOC?）→GET_WF | 1 | 2 | 2==2? 是 | 清 0 | 发 rt_dealloc + host_wg_done |

**预期结果**：WF#0 完成只更新计数（0→1），不触发释放；WF#1 完成时 `wf_gather_finish=1`，FSM 进入释放/回报，计数清 0，wg_slot#3 重新可用。若下游 `rt_dealloc` 或 `host_wg_done` 在 UPDATE 拍没 ready，则 FSM 转去 DEALLOC 补发。

**待本地验证**：FSM 是否真走 DEALLOC 取决于下游反压，需在仿真波形中观察 `cu_interface_inst.fsm` 与 `io.rt_dealloc.valid/ready`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 FSM 不能合并成「单拍完成」？

> **答案**：完成计数和 WG 存档都存在 `SyncReadMem`，给地址后下一拍才出数据；「读→比较→写」无法在一拍内闭环，所以至少要 GET_WF（发起读）→ UPDATE（用数据）两态。再加 DEALLOC 是为了容忍下游反压。

**练习 2**：`init_ok` 为什么要由 `cu_interface` 产生，而不是由 `wg_buffer` 或 `allocator` 产生？

> **答案**：因为 `wf_gather_cnt` 这个决定「完成判定正确性」的 SyncReadMem 就在 `cu_interface` 里，它必须先清零才能保证计数从 0 起算。若没清零就开始收 WG，完成计数会读到随机值，提前误判 WG 完成。所以闸门放在数据的主人这里最自然。

---

### 4.4 CTA2warp：接收 warp 并分配硬件 wid

#### 4.4.1 概念说明

`cu_interface` 把 WF 派发到 CU 的端口叫 `cu_wf_new`，但这还不是 SM 内部流水线直接用的接口。SM 一侧有两个名字相近、角色不同的标识：

- **wf_tag**（6 bit，逻辑标识）：`{wg_slot_id, wf_id}`，描述「这个 warp 属于哪个 WG 的第几条」，是**软件/调度器视角**的身份证。
- **wid**（硬件 warp id，本仓库 `depth_warp`=3 bit）：SM 流水线内部用来索引 PC 表、寄存器堆分区、记分板等的**物理槽位号**（0~7）。

一个 SM 同时最多持有 `num_warp`=8 个 warp，但任意时刻哪些 wf_tag 占用哪些 wid 是**动态**的。`CTA2warp` 就是这座桥：收到一个新 warp（带 wf_tag）→ 分配一个空闲 wid → 把 wf_tag 与 wid 的对应关系记下来 → 转发给流水线取指；warp 跑完时 → 按 wid 回收 → 把完成（带原 wf_tag）回报给调度器。

#### 4.4.2 核心流程

```
去程（接收 warp）：
  CTAreq(带 wf_tag) 到来
   ├ idx_using(wid 位图) 不全占满 → CTAreq.ready=1
   ├ idx_next_allocate = PriorityEncoder(~idx_using)   // 找最低位空闲 wid
   ├ fire 时：
   │    ├ idx_using[idx_next_allocate] ← 1              // 占用该 wid
   │    ├ data[idx_next_allocate] ← wf_tag              // 记录 wid↔wf_tag
   │    └ warpReq(wid=idx_next_allocate, CTAdata) → 流水线 warp_scheduler
   └

回程（warp 完成）：
  warpRsp(带 wid) 从流水线回来
   ├ 经一个深度 16 的 FIFO（CTArsp_fifo）缓冲
   ├ fire 时：idx_using[wid] ← 0                         // 释放该 wid
   └ CTArsp(把 data[wid]=原 wf_tag 还回去) → 调度器 cu_wf_done

旁路查询（barrier 用）：
  wg_id_lookup(wid) → data[wid] → wg_id_tag(=wf_tag)
  供 warp_scheduler 在 barrier/endprg 时反查 wid 对应的 wf_tag
```

#### 4.4.3 源码精读

**(a) warp 的「行李」：CTAreqData**（[ventus/src/pipeline/CTA2warp.scala:L17-L33](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CTA2warp.scala#L17-L33)）。每个字段都对应 `cu_interface` 派发时填入的值（映射关系见 `GPGPU_top.scala` 的 [L88-L109](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L88-L109)）：

```scala
class CTAreqData extends Bundle{
  val dispatch2cu_wg_wf_count        = ... // num_wf
  val dispatch2cu_wf_size_dispatch   = ... // num_thread_per_wf
  val dispatch2cu_sgpr_base_dispatch = ... // WF 级 sgpr 基址
  val dispatch2cu_vgpr_base_dispatch = ... // WF 级 vgpr 基址
  val dispatch2cu_lds_base_dispatch  = ... // LDS 基址（WG 共享）
  val dispatch2cu_wf_tag_dispatch    = ... // wf_tag = {wg_slot_id, wf_id}
  val dispatch2cu_start_pc_dispatch  = ... // 起始 PC
  val dispatch2cu_pds_base_dispatch  = ... // PDS 基址
  ...
}
```

**(b) wid 位图与分配**（[CTA2warp.scala:L54-L69](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CTA2warp.scala#L54-L69)）：

```scala
val idx_using = RegInit(0.U(num_warp.W))                 // 位图：1=该 wid 被占用
io.CTAreq.ready := (~idx_using.andR)                     // 只要还有 0 位就能收
val idx_next_allocate = PriorityEncoder(~idx_using)      // 选最低位的空闲 wid

// 一拍内同时处理「占用新 wid」与「释放完成 wid」
idx_using := (idx_using | ((1.U << idx_next_allocate) & Fill(num_warp, io.CTAreq.fire)))
          & (~((Fill(num_warp, io.warpRsp.fire)) & (1.U << io.warpRsp.bits.wid)))

when(io.CTAreq.fire) { data(idx_next_allocate) := io.CTAreq.bits.dispatch2cu_wf_tag_dispatch }
io.warpReq.valid := io.CTAreq.fire
io.warpReq.bits.CTAdata := io.CTAreq.bits
io.warpReq.bits.wid := idx_next_allocate                 // 给流水线带上分配到的 wid
```

`PriorityEncoder(~idx_using)` 选最低位空闲 wid，是简单的「先来先占」策略。位图更新用一条组合表达式同时处理分配与释放，保证一拍内既能收新 warp 又能回收旧 warp。

**(c) 完成回报与 FIFO 缓冲**（[CTA2warp.scala:L71-L78](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CTA2warp.scala#L71-L78)）：

```scala
// warp_scheduler 要求 warpRsp.ready 恒为 1，用一个深度 16 的 FIFO 解耦
val CTArsp_fifo = Queue(io.warpRsp, 16)
assert(io.warpRsp.ready, "warpRsp port requires ready=1 ...")
CTArsp_fifo.ready := io.CTArsp.ready
io.CTArsp.bits.cu2dispatch_wf_tag_done := data(CTArsp_fifo.bits.wid)  // 把 wid 翻译回原 wf_tag
io.CTArsp.valid := CTArsp_fifo.valid
```

这里有个**关键设计点**：流水线回报完成时**只带 wid**（`warpRsp.bits.wid`），但调度器 `cu_interface` 需要的是 **wf_tag**（见 4.3 节，`cu_wf_done.bits.wf_tag`）。所以 `CTA2warp` 在回报路径上用 `data(wid)` 把 wid **翻译回当初记下的 wf_tag**（`cu2dispatch_wf_tag_done`）。这张 `data` 表就是 wid↔wf_tag 的「号码牌寄存器」。

> 旁路 `wg_id_tag`（[L57-L58](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CTA2warp.scala#L57-L58)）让 warp_scheduler 在执行 barrier/endprg 时，能用 wid 当下标反查 wf_tag，进而得到 `wg_slot_id`/`wf_id`（见 [warp_schedule.scala:L117-L118](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/warp_schedule.scala#L117-L118)），用于 block 内 warp 同步计数。

**(d) 在顶层的位置**。`CTA2warp` 被 `SM_wrapper` 例化，夹在顶层 `CTA2warp` 端口与 `pipe` 之间（[GPGPU_top.scala:L351-L364](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L351-L364)）：

```scala
val cta2warp = Module(new CTA2warp)
cta2warp.io.CTAreq <> io.CTAreq          // 接 cu_interface 派发来的 warp
cta2warp.io.warpReq → pipe 的 warp_scheduler
cta2warp.io.warpRsp ← pipe 的 warp_scheduler
cta2warp.io.wg_id_lookup := pipe.io.wg_id_lookup
pipe.io.wg_id_tag := cta2warp.io.wg_id_tag
```

而 `io.CTAreq` 又由 `GPGPU_top` 的循环从 `cta_sche.io.cu_wf_new(i)` 逐字段映射而来（[GPGPU_top.scala:L88-L109](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L88-L109)）。于是完整去程链路为：

```
cu_interface.cu_wf_new(i)  ──(GPGPU_top 字段映射)→  SM_wrapper.CTAreq  →  CTA2warp  →  warpReq  →  warp_scheduler
```

#### 4.4.4 代码实践

**实践目标**：验证 `CTA2warp` 的「wid↔wf_tag 双向翻译」闭环。

**操作步骤**：

1. 读 [CTA2warp.scala:L54-L78](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CTA2warp.scala#L54-L78)，确认：去程把 `wf_tag` 存进 `data(wid)`；回程用 `data(wid)` 还原 `wf_tag`。
2. 读 [GPGPU_top.scala:L88-L109](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L88-L109)，确认 `cu_interface` 的 `wf_tag` 字段对应 `CTAreqData` 的 `dispatch2cu_wf_tag_dispatch`。
3. 读 [CTA2warp.scala:L34-L36](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CTA2warp.scala#L34-L36)，确认回程 `CTArspData` 只回传 `cu2dispatch_wf_tag_done`（即还原的 wf_tag）。

**需要观察的现象**：去程 SM 内部用 wid 索引一切（PC、regfile），但 SM 对外（对调度器）始终用 wf_tag；wid 是 SM 私有的、对调度器不可见。`data` 这张表是唯一的翻译字典，且在回收 wid 的同一拍之后该表项即失效（因为 wid 已被重新分配）。

**预期结果**：你能画出 `wf_tag ──(去程存)→ data[wid] ──(回程取)→ wf_tag` 的闭环，并解释为什么 `cu_interface` 的 `cu_wf_done` 只需带 `wf_tag` 就足够（因为它从 SM 收到的已经是还原后的 wf_tag，见 [cta_scheduler.scala:L91-L94](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cta_scheduler.scala#L91-L94)）。

#### 4.4.5 小练习与答案

**练习 1**：`io.CTAreq.ready := (~idx_using.andR)`——为什么用 `andR` 而不是 `orR`？

> **答案**：`idx_using` 是占用位图，某位为 1 表示该 wid 被占。`andR` 为真意味着**所有位都是 1**（8 个 wid 全占满）；取反 `~idx_using.andR` 即「没有全占满」=「至少还有一个空闲 wid」，这时才能再收。若用 `orR`，只要有一位占用就 ready=0，会错误地拒绝收新 warp。

**练习 2**：为什么回程要套一个 `Queue(io.warpRsp, 16)`？

> **答案**：注释（[L71-L74](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/CTA2warp.scala#L71-L74)）说明：下游 `warp_scheduler` 的 `warpRsp` 端口要求 `ready` 恒为 1，但 `CTA2warp` 把完成信号再转给调度器（`cu_interface`）时，调度器侧可能因 FSM 忙而反压。这个深度 16 的 FIFO 用来吸收两者的速率差，并用 assert 保证它不会溢出。

---

## 5. 综合实践

把本讲两块拼起来，追踪**一个含 2 个 WF 的 WG 从派发到全部完成的端到端全过程**。建议画一张时序表格，逐拍（cycle）记录关键字段。目标：把 4.2、4.3、4.4 三节串成一条完整链路。

**场景设定**：

- 该 WG 被分配到 `cu_id=1`、`wg_slot_id=3`、`num_wf=2`，WG 级基址 `sgpr_base=100`、`vgpr_base=200`、`pds_base=0x1000`，每 WF 占 `num_sgpr_per_wf=16`、`num_vgpr_per_wf=32`、`num_pds_per_wf=64`。
- SM#1 初始 `idx_using = 0b00000000`（全空），故 `idx_next_allocate` 将依次给出 wid=0、wid=1。

**任务**：填写下表（Cycle 仅作相对顺序，不追求精确拍数）。

| Cycle | 模块/信号 | 关键事件 | splitter_cnt | 派发/接收的 wf_tag | wid | idx_using | FSM(cu_interface) |
|---|---|---|---|---|---|---|---|
| c1 | cu_interface | 三路汇合 fire，WG 入 FIFO | — | — | — | — | — |
| c2 | cu_interface | load_new，开始拆 | 2 | — | — | — | — |
| c3 | cu_interface→SM#1 | 派发 WF#0 | 2→1 | 0x18 | — | — | — |
| c4 | CTA2warp | 收到 WF#0，分 wid=0 | — | 0x18 | 0 | 0b…00000001 | — |
| c5 | cu_interface→SM#1 | 派发 WF#1 | 1→0 | 0x19 | — | — | — |
| c6 | CTA2warp | 收到 WF#1，分 wid=1 | — | 0x19 | 1 | 0b…00000011 | — |
| ... | （两个 warp 在流水线里执行） | | | | | | |
| ck | CTA2warp←pipe | WF#0 完成（wid=0） | | | 0 | 0b…00000010 | |
| ck+1 | CTA2warp→cu_interface | 还原 wf_tag=0x18 回报 | | | | | GET_WF→UPDATE |
| ck+2 | cu_interface | cnt 0→1，未完成 | | | | | →GET_WF |
| cm | CTA2warp←pipe | WF#1 完成（wid=1） | | | 1 | 0b…00000000 | |
| cm+1 | CTA2warp→cu_interface | 还原 wf_tag=0x19 回报 | | | | | GET_WF→UPDATE |
| cm+2 | cu_interface | cnt 1→2==num_wf，finish | | | | | UPDATE→（DEALLOC）→GET_WF，发 rt_dealloc + host_wg_done |

**需要观察/回答**：

1. 4.2.4 表里两个 WF 的 `wf_tag`（0x18、0x19）与本表是否一致？
2. CTA2warp 收到 WF 时分配的 wid（0、1）与它回报完成时用的 wid 是否对应？`data[0]=0x18`、`data[1]=0x19` 是否在回报时被正确还原？
3. `cu_interface` 在 WF#0 完成时**不**触发 dealloc（因为 cnt 1≠2），只有在 WF#1 完成时才释放资源并回报 host——这与你 4.3.4 的推导是否吻合？
4. 全部完成后 `idx_using` 回到全 0、`wf_gather_cnt[global(1,3)=11]` 回到 0，两个 wg_slot/wid 资源都重新可用。

**预期结果**：你能用一张连贯的表说明「WG 拆成 2 个 WF → 各自拿到 wid 与递增的基址 → 分别完成后计数累加 → 第 2 个完成时触发一次性释放与回报 → 资源全部回收」。这张表就是本讲两最小模块（`cu_interface` 与 `CTA2warp`）协作的完整证明。

**待本地验证**：Cycle 编号与中间执行拍数需在仿真波形中确认；可在 sim-verilator 中 dump `cta_sche.cu_interface_inst` 与各 SM 的 `cta2warp_inst` 信号核对（参见 u1-l4 的仿真方法）。

## 6. 本讲小结

- `cu_interface` 是 CTA 调度器的最后一棒，承担**拼合 WG 信息 → 拆分 WF 派发 → 收集完成并释放/回报**六大职能，实现了 host 侧 WG 粒度到 CU 侧 WF 粒度的转换。
- 三路输入（`wg_buffer`/`allocator`/`resource_table`）携带同一 WG 的不同侧面，靠 3-to-1 与门汇合后压入深度 2 的 FIFO；这一「信息分包」由 `cta_scheduler.scala` 里的 trait 体系定义。
- splitter 用倒计数器 `splitter_cnt` + 步进寄存器把一个 WG 拆成逐个 WF；`sgpr/vgpr/pds` 基址每发步进，`lds` 基址全 WG 共享不步进；每个 WF 带上 `wf_tag = {wg_slot_id, wf_id}`（默认 6 bit）作为身份证。
- 完成收集因 `SyncReadMem` 多周期读而采用三态 FSM（`GET_WF/UPDATE/DEALLOC`）：仲裁完成信号 → 读计数比较 → 命中 `num_wf` 即释放资源（`rt_dealloc`）并回报 host（`host_wg_done`），构成与 u3-l2 资源分配的反向闭环。
- `cu_interface` 的 `init_ok`（清零 `wf_gather_cnt`）是整个调度器的启动闸门，初始化完成前不收任何 WG。
- `CTA2warp` 在 SM 侧用位图 `idx_using` + `PriorityEncoder` 分配/回收硬件 wid，用 `data` 表维护 wid↔wf_tag 的双向翻译：去程存 wf_tag、回程还原 wf_tag，使 wid 作为 SM 私有标识而对调度器透明。

## 7. 下一步学习建议

本讲讲完了「WG 如何进入 SM」。接下来应进入 **u4 单元（SM 流水线前端）**，看 warp 拿到 wid 与 `start_pc` 之后，SM 流水线如何取指、译码、送入 ibuffer：

- **u4-l1（SM 流水线总体与取指）**：从 `pipe.scala` 整体连接出发，重点看 `warp_scheduler` 如何用 wid 管理 PC、调度取指——本讲的 `warpReq`/`warpRsp` 正是它的输入输出。
- 建议同时翻一眼 `warp_schedule.scala` 全文（本讲引用了其中 wf_tag 切片与 barrier 计数），它是承接 `CTA2warp` 的下一站，提前建立整体印象会让 u4-l1 更顺。
- 如果你对调度器如何在「多个 WG、多个 CU」间做选择还想再深入，可回看 u3-l2 的 allocator FSM；本讲的 `cu_interface` 正是吃 allocator 的决策结果。
