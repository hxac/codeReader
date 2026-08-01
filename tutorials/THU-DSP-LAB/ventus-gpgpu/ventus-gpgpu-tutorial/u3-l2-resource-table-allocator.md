# 资源表与分配器

## 1. 本讲目标

学完本讲后，你应当能够：

- 讲清 **`allocator`（分配器）** 如何用主状态机 `IDLE → CU_PREFER → RESOURCE_CHECK → ALLOC / REJECT` 把一个 workgroup「逐步选定」一个能容纳它的 CU，并说出每个状态的进入条件与转移条件。
- 解释分配器做容纳性判断时依据的三类信息：**RTcache**（每 CU 每资源缓存的最大若干块空闲片段大小）、**WG slot** 位图、**WF slot** 计数器，以及为什么这三者必须「同时满足」。
- 说明 **`RTcache`（资源表缓存）** 的「大于等于」语义、用 `size=0` 代替 valid 位的技巧、分配后「就地扣除」的更新方式，以及 `rtcache_writer` 如何在「分配扣除」与「资源表回填」两种写来源之间仲裁。
- 复述 **`resource_table_top`（资源表）** 如何用**双向链表**（`prev/next/addr1/addr2`）记录每个 CU 上每个 WG 占用的资源片段，如何在 ALLOC 时做「最佳适配」找最小可容纳片段、在 DEALLOC 时仅做 `O(1)` 摘链、在 SCAN 时重新扫描出最大几块空闲片段回填 RTcache。
- 理解资源表顶层那条「**全局最多只有一个 alloc 在途**」的强约束是怎么用 `alloc_record` 实现的，以及为什么三类资源（LDS/sGPR/vGPR）的 BaseAddr 必须「三路对齐」后才送给 cu_interface。

本讲是第 3 单元（CTA 任务调度器）的第 2 篇，承接 **u3-l1**（调度器总体与 `wg_buffer`）。u3-l1 把调度器看成「`wg_buffer → allocator → resource_table → cu_interface` 四大组件连起来 + 入口缓冲」；本讲打开中间两个最核心的零件——**`allocator`** 与 **`resource_table`**，以及夹在它们之间的 **`RTcache`**。本讲不展开 `cu_interface` 如何把 WG 拆成 warp（留到 u3-l3）。

## 2. 前置知识

在阅读本讲前，你最好已经了解：

- **u3-l1 的调度器总体**：四大组件的分工与连线。尤其要记得这条资源通路（u3-l1 的 4.1.3 (d)）：

  ```
  allocator ──rt_alloc──▶ resource_table                 // 申请分配，求基址
  resource_table ──rtcache_lds/sgpr/vgpr──▶ allocator     // 回填空闲片段大小
  resource_table ──cuinterface_wg_new──▶ cu_interface     // 回送 WG 级基址
  cu_interface ──rt_dealloc──▶ resource_table             // 触发释放
  resource_table ──slot_dealloc──▶ allocator              // WG/WF slot 释放
  ```

  本讲就是把这些线上「跑的是什么、怎么算出来的」彻底讲透。

- **GPU 编程模型（u2-l1）与规模参数（u2-l3）**：CU = SM；默认 `num_sm=2`、`num_warp=8`、`num_thread=32`。在调度器语境里对应 `NUM_CU = num_sm = 2`、`NUM_WG_SLOT = num_block = 8`（每 CU 可同时驻留的 WG 槽位数）、`NUM_WF_SLOT = num_warp = 8`（每 CU 可同时驻留的 WF 槽位数）。每 CU 的资源容量由 `num_vgpr = 128*num_warp = 1024`、`num_sgpr = 256*num_warp = 2048`、`sharemem_size`（LDS 字节数）给出。
- **Chisel 基础**：`DecoupledIO`（`valid/ready/bits`，`fire = valid && ready`）；`SyncReadMem`（同步读，给地址当拍拿不到数据，下一拍才出）；`RegEnable`、`Mux1H`、`PriorityEncoder`、`PriorityMux`。
- **一个关键直觉**：每个 SM（CU）上的寄存器堆（sGPR/vGPR）和共享内存（LDS）是**按地址连续切分**给各个驻留 WG 用的——就像把一长条货架切成几段，每段分给一个 WG。所以「分配资源」本质上是要回答：「在这个 CU 的货架里，找一段连续的、长度够用的空闲区间，把它的起始地址（基址）交给这个 WG」。本讲讲的就是这个「找区间 + 给基址」的过程。

> 术语对齐：源码与文档里 **WG = workgroup = block = CTA**，**WF = wavefront = warp**，**CU = SM**。资源表文档里把 `prev/next/addr1/addr2` 称为链表指针与首尾地址，本讲沿用。

## 3. 本讲源码地图

本讲围绕 `cta` 包下的两个核心文件与两篇设计文档：

| 文件 | 作用 |
|------|------|
| [`ventus/src/cta/allocator.scala`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala) | 定义 `allocator`（分配器主状态机）、`rtcache_writer`（RTcache 写控制器）、以及相关 IO bundle（`io_alloc2rt`、`io_rt2cache` 等）。本讲 4.1、4.2 节的主角。 |
| [`ventus/src/cta/resource_table.scala`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/resource_table.scala) | 定义 `resource_table_top`（资源表顶层与路由）、`resource_table_handler`（含 ALLOC/DEALLOC/SCAN 子状态机）、`resource_table_ram`（每 CU 的链表存储）。本讲 4.3 节的主角。 |
| `docs/cta_scheduler/Allocator.md` | 分配器官方设计文档，含 FSM 图与 RTcache 设计取舍说明（注意：原文将「Resource Table」缩写为 RT）。 |
| `docs/cta_scheduler/Resouce table.md` | 资源表官方设计文档，含链表硬件结构图、ALLOC/DEALLOC/SCAN 伪代码（注意：文件名与标题里 "Resouce" 为原文拼写）。 |

理解它们需要参考的辅助文件：

| 文件 | 作用 |
|------|------|
| [`ventus/src/cta/utils.scala`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/utils.scala) | `sort3`（三输入降序排序器，SCAN 用）、`DecoupledIO_1_to_3`（1 拆 3 的 DecoupledIO，资源表把一个请求拆给三类资源用）。 |
| [`ventus/src/cta/cta_scheduler.scala`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cta_scheduler.scala) | 定义 WG 资源需求字段（`ctainfo_host_to_alloc`：`num_wf/num_sgpr/num_vgpr/num_lds`）、以及 `ctainfo_alloc_to_cu`（`sgpr_base/vgpr_base/lds_base`）等 bundle。 |
| [`ventus/src/top/parameters.scala`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala) | `CTA_SCHE_CONFIG` 子对象：`GPU.NUM_CU/NUM_WG_SLOT/NUM_WF_SLOT`、`WG.NUM_LDS_MAX/NUM_SGPR_MAX/NUM_VGPR_MAX`、`RESOURCE_TABLE.NUM_RESULT`（默认 2，即 RTcache 每 CU 每资源缓存 2 块空闲片段）。 |

> 小贴士：调度器代码里位宽几乎都写成 `log2Ceil(CONFIG.WG.NUM_xxx_MAX+1).W`。阅读时不必死记数值，只要知道「它正好能装下该资源的最大值」。本讲为了举例会代入默认规模：`NUM_CU=2`、`NUM_VGPR_MAX=1024`、`RESOURCE_TABLE.NUM_RESULT=2`。

## 4. 核心概念与源码讲解

本讲按「**决策者 → 决策依据 → 真相源头**」的顺序拆成三个最小模块：先讲做决定的 `allocator`（4.1），再讲它查的 `RTcache`（4.2），最后讲填充 RTcache 并给出真实基址的 `resource_table`（4.3）。

### 4.1 allocator：分配器主状态机

#### 4.1.1 概念说明

`allocator` 是调度器的「分拣员」。`wg_buffer` 把一个待判定的 WG 递给它（带资源需求 `num_wf/num_sgpr/num_vgpr/num_lds`），它必须回答两个问题：

1. **哪个 CU 能装下这个 WG？** 一个 WG 要占用一定数量的 sGPR/vGPR/LDS，还要占用 WG 槽位与若干 WF 槽位。只有「剩余资源全部够用」的 CU 才是候选。
2. **选其中哪一个？** 若有多个候选，按某种优先级选一个；若一个都没有，就**拒绝**（reject），让 `wg_buffer` 把这个 WG 留着下一轮再试。

`Allocator.md` 把这两件事概括为两个**相互独立**的子功能，并规定了「**先判优先级，再自最高优先级起逐个 CU 做资源判定，直到找到一个能容纳的 CU**」的流程（见 [`Allocator.md`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/docs/cta_scheduler/Allocator.md)）：

- 功能 1：**CU 优先级抉择**（当前实现是「轮询」——优先选「上一个被分配的 CU 的下一个」）。
- 功能 2：**CU 资源可容纳判定**（查 RTcache + WG/WF slot）。

⚠️ 一个极其重要的设计取舍：**分配器只用 RTcache（一份可能滞后的快照）做容纳性判断，它只保证「不超量分配」，不保证判断与真实情况完全一致。** 真正的基址（BaseAddr）采用**惰性求值（lazy evaluation）**——等 CU 选定之后，再由该 CU 的 `resource_table` 实际遍历链表算出一个基址。这样做的决定性好处是：资源表可以「选一个能容纳 WG 的**最小**片段」而不是「最大的那块」，从而**抑制资源碎片化**。这个取舍在 4.2、4.3 会反复体现。

#### 4.1.2 核心流程

`allocator` 的主状态机有 5 个状态（[`allocator.scala:172-174`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L172-L174)）：`IDLE, CU_PREFER, RESOURCE_CHECK, ALLOC, REJECT`。主干流程如下：

```
                 wg_buffer 送来 WG (fire)
   IDLE ──────────────────────────────▶ CU_PREFER
                                          │  计算「首选 CU」= 上次分配的 CU + 1（轮询）
                                          ▼
                                     RESOURCE_CHECK
                                     ┌──每周期检查 step(=1) 个 CU──────┐
                                     │  某 CU 的 RTcache 被更新(有新释放) │
                                     │  → 本周期原地重查，不前进          │
                                     │  找到一个能容纳的 CU ──────────────┼──▶ ALLOC
                                     │  全部 NUM_CU 查完仍无解 ───────────┼──▶ REJECT
                                     └──────────────────────────────────┘
   IDLE ◀──alloc_task_ok(所有任务完成)── ALLOC                    REJECT ──wgbuffer_result.fire──▶ IDLE
                                          │                       （回送 reject，让 WG 重试）
                                          ├── task0: 更新 WG/WF slot 占用
                                          ├── task1: 更新 RTcache（扣除已分配量）
                                          ├── task2: 发 rt_alloc 给 resource_table（求真实基址）
                                          ├── task3: 把分配结果发给 cu_interface
                                          └── task4: 回送 accept 给 wg_buffer（触发读 wgram2）
```

各状态含义与转移条件：

- **IDLE**：等待 `wg_buffer` 送来 WG。`wgbuffer_wg_new.fire` 时把 WG 的资源字段锁存进寄存器 `wg`，转到 `CU_PREFER`。`wgbuffer_wg_new.ready` 仅在 IDLE 拉高（[`allocator.scala:188`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L188)），即分配器**一次只处理一个 WG**。
- **CU_PREFER**：算出本轮的「首选 CU」。当前策略是 `cu_next = cu + 1`（[`allocator.scala:224-225`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L224-L225)），即从「上一次分配的 CU」的下一个开始（轮询公平）。随后无条件转 `RESOURCE_CHECK`，并把检查计数器 `cu_cnt` 清零。
- **RESOURCE_CHECK**：每个周期检查 `RESOURCE_CHECK_CU_STEP`（=1）个 CU。对当前 CU `cu`，组合逻辑算出 `resource_check_result`（详见 4.1.3 (c)）。转移（[`allocator.scala:368-373`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L368-L373)）：
  - 若**当前 CU 的 RTcache 刚被更新**（`resource_check_repeat`，意味着刚有新释放、余量变大）→ 原地重查（留在 `RESOURCE_CHECK`，`cu` 不前进）。
  - 若**找到能容纳的 CU**（`resource_check_result.orR`）→ 转 `ALLOC`，并把选中 CU 的编号锁定到 `cu`。
  - 若**已查完全部 `NUM_CU` 个 CU 仍无解**（`cu_cnt + step >= NUM_CU`）→ 转 `REJECT`。
- **ALLOC**：执行一揽子任务（task0~task4），全部完成后（`alloc_task_ok`）回 `IDLE`。详见 4.1.3 (d)。
- **REJECT**：把 `accept=false` 的结果回送 `wg_buffer`（`wgbuffer_result.fire`），让该 WG 清掉 alloc 位、下一轮重试；随后回 `IDLE`。

> 为什么 RESOURCE_CHECK 要「RTcache 更新就原地重查」？因为 RTcache 是滞后快照。如果某 CU 刚释放了一批资源，RTcache 的回填（`rt_result`）正巧在检查它时到达，分配器就应该用最新的余量再判一次，避免误拒。这个「重查」由 `resource_check_repeat`（[`allocator.scala:216-219`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L216-L219)）实现。

#### 4.1.3 源码精读

**(a) 模块端口：与三个邻居对接**

[`allocator.scala:108-117`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L108-L117) 定义了分配器的全部端口，对应它连接的三个邻居：

```scala
val wgbuffer_wg_new    = Flipped(DecoupledIO(new io_buffer2alloc))   // ← wg_buffer：取待判定 WG
val wgbuffer_result    = DecoupledIO(new io_alloc2buffer)            // → wg_buffer：回送 accept/reject
val cuinterface_wg_new = DecoupledIO(new io_alloc2cuinterface)       // → cu_interface：分配结果
val rt_alloc           = DecoupledIO(new io_alloc2rt)                // → resource_table：申请分配（求基址）
val rt_dealloc         = Flipped(DecoupledIO(new io_rt2dealloc))     // ← resource_table：WG/WF slot 释放
val rt_result_lds/sgpr/vgpr = Flipped(DecoupledIO(new io_rt2cache))  // ← resource_table：回填 RTcache
```

注意三类资源（LDS/sGPR/vGPR）各有独立的 `rt_result_*` 通道——因为资源表对每类资源各维护一张链表、各做一次 SCAN、各回填一份 RTcache。

**(b) RTcache 与 WG/WF slot 记录：分配器的「账本」**

分配器内部维护两套账本（`allocator.scala:131-166`）。第一套是 **RTcache**——每 CU 每资源一个 `datatype_rtcache`，含 `NUM_RT_RESULT`（默认 2）个 `size` 项：

```scala
// allocator.scala:131-145（节选 vgpr）
val rtcache_vgpr = RegInit(VecInit.fill(NUM_CU)(
  (new datatype_rtcache(NUM_RESOURCE = CONFIG.WG.NUM_VGPR_MAX, NUM_RT_RESULT = NUM_RT_RESULT)).Lit(
    c => c.size -> Vec.Lit((CONFIG.WG.NUM_VGPR_MAX.U +: Seq.fill(NUM_RT_RESULT-1)(0.U(...))):_*))))
```

复位值是 `size(0) = NUM_RESOURCE_MAX`（整块资源都空闲），其余项为 0。这是「最坏估计」：在资源表还没来得及 SCAN 回填之前，分配器先假设「整个 CU 都是空的」（复位时确实如此）。

第二套账本是 **WG slot 位图与 WF slot 计数器**（[`allocator.scala:162-163`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L162-L163)）：

```scala
val wgslot = RegInit(VecInit.fill(NUM_CU)(0.U(CONFIG.GPU.NUM_WG_SLOT.W)))           // 每 CU 一个 NUM_WG_SLOT 位位图
val wfslot = RegInit(VecInit.fill(NUM_CU)(0.U(log2Ceil(CONFIG.GPU.NUM_WF_SLOT+1).W))) // 每 CU 一个已占用 WF 计数
```

WG slot 用位图（一位一槽，`andR` 为真表示全满）；WF slot 用计数器（占用数）。这两类「槽位资源」由分配器自己管理（不进资源表的链表），而 LDS/sGPR/vGPR 这种「地址连续切分」的资源才由资源表的链表管理。

**(c) 容纳性判定：`resource_check_result`**

这是分配器决策的核心组合逻辑（[`allocator.scala:245-263`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L245-L263)）。对当前检查的每个 CU（`cuid`），它要同时满足五项条件：

```scala
for(j <- 0 until NUM_RT_RESULT) {  // 对该 CU 的每一个 RTcache 片段
  result_line_lds(j)  := rtcache_lds(cuid).size(j)  >= wg.num_lds    // 至少有一块 LDS 片段够大？
  result_line_sgpr(j) := rtcache_sgpr(cuid).size(j) >= wg.num_sgpr
  result_line_vgpr(j) := rtcache_vgpr(cuid).size(j) >= wg.num_vgpr
}
// 三类资源都「至少有一块够大」 AND WG slot 没满 AND WF slot 够 num_wf 个
resource_check_result(i) :=
  result_line_lds.asUInt.orR && result_line_sgpr.asUInt.orR && result_line_vgpr.asUInt.orR &&
  (!wgslot(cuid).andR) && (CONFIG.GPU.NUM_WF_SLOT.U - wfslot(cuid) >= wg.num_wf)
```

读法：一个 CU 能容纳该 WG，当且仅当 **LDS、sGPR、vGPR 三类资源各自都至少有一块 RTcache 片段不小于需求量**，**且**该 CU 还有空闲 WG 槽，**且**剩余 WF 槽 ≥ `num_wf`。三者（三类地址资源 + 两类槽位资源）是「与」关系，缺一不可。

若该 CU 通过，`PriorityMux` 还会选出「**优先级最高（即最大）的那块片段**」作为本次分配假定占用的片段（`resource_check_result_rtcache_sel_*`，[`allocator.scala:257-259`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L257-L259)）。这个选中片段在 ALLOC 阶段用于「就地扣除」RTcache（见 4.2）。

**(d) ALLOC 状态的五项任务**

`ALLOC` 要把「选定 CU」这件事落实成对各个邻居的实际动作。五项任务（`allocator.scala:283-353`）必须**全部完成**（`alloc_task_ok`，[`allocator.scala:286`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L286)）后才回 IDLE：

| 任务 | 动作 | 对应代码 |
|------|------|----------|
| task0 | 更新选中 CU 的 WG/WF slot 占用（置 WG slot 位、WF slot += num_wf），并服务可能同时到达的 `rt_dealloc`（释放 slot） | [`allocator.scala:288-296`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L288-L296) |
| task1 | 驱动三个 `rtcache_writer`，把已分配量从选中片段「就地扣除」 | [`allocator.scala:299-313`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L299-L313) |
| task2 | 向 resource_table 发 `rt_alloc`（`cu_id/wg_slot_id/num_lds/num_sgpr/num_vgpr`），请其实际分配基址 | [`allocator.scala:316-325`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L316-L325) |
| task3 | 把分配结果（`cu_id/wg_slot_id/num_wf/*_dealloc_en`）经一个深度 1 的 `Queue` 发给 cu_interface | [`allocator.scala:328-340`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L328-L340) |
| task4 | 回送 `wgbuffer_result`（`accept=true`，并附 `wgram_addr` 触发 wg_buffer 读 wgram2）；REJECT 状态也复用此端口送 `accept=false` | [`allocator.scala:344-353`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L344-L353) |

注意 task2 发出的 `rt_alloc` 里**不含基址**——基址要等资源表算出来再经 `cuinterface_wg_new` 回送。所以 cu_interface 实际收到的是「两路汇合」的信息：分配器直接给的「分配结果 + WG 信息」（task3），以及资源表回送的「三类资源基址」（见 4.3 (e)）。`io_alloc2rt` 的定义见 [`allocator.scala:36-43`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L36-L43)。

> 任务为什么要拆成「发请求 + 等回握」两段？因为 `rt_alloc`、`cuinterface_buf.enq`、`wgbuffer_result` 都是 `DecoupledIO`，下游可能不立刻 `ready`。分配器用一组 `*_reg`（如 `alloc_task_rt_reg`）记住「这个任务已经发出并握过手」，直到所有任务都握手完成才允许离开 ALLOC。这样保证一个 WG 的所有副作用都落地。

**(e) 状态转移逻辑**

完整的下一状态逻辑在 [`allocator.scala:360-381`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L360-L381)。其中 `RESOURCE_CHECK` 的三选一最关键：

```scala
is(FSM.RESOURCE_CHECK) {
  fsm_next := MuxCase(fsm, Seq(
    resource_check_repeat -> fsm,                              // RTcache 刚更新 → 重查
    resource_check_result.asUInt.orR -> FSM.ALLOC,             // 找到能容纳的 CU
    (cu_cnt + RESOURCE_CHECK_CU_STEP.U >= NUM_CU.U) -> FSM.REJECT, // 查完仍无解
  ))
}
```

注意 `MuxCase` 是「**按顺序取第一个为真的分支**」，所以三个条件的优先级如上。`cu_cnt` 每周期 `+= step`（[`allocator.scala:198-199`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L198-L199)），用于判断「是否已经查完一圈」。

#### 4.1.4 代码实践

**实践目标**：以默认规模 `NUM_CU=2` 为例，手工推演一个具体 WG 在 `allocator` 主状态机里的完整流转，验证你对「状态转移条件」与「CU 选定逻辑」的理解。

**给定场景**：

- WG_X 的资源需求：`num_wf = 4`、`num_vgpr = 500`、`num_sgpr = 600`、`num_lds = 0`（为简化，假设它不用 LDS）。
- 当前账本（仅列 vgpr，sgpr 假设两类 CU 都够）：
  - **CU0**：`rtcache_vgpr = {size0=1024, size1=0}`（CU0 全空），`wgslot=00000000`（无占用），`wfslot=0`。
  - **CU1**：`rtcache_vgpr = {size0=300, size1=0}`（已被别的 WG 占了 700），`wgslot=00000001`（占 1 个槽），`wfslot=6`。
- 假设上一个被分配的 CU 是 CU1，故 `cu` 寄存器当前 = CU1。

**操作步骤**：

1. 从 `IDLE` 开始：`wgbuffer_wg_new.fire` 锁存 WG_X，转 `CU_PREFER`。
2. `CU_PREFER`：`cu_next = cu + 1 = CU1 + 1 = CU2`，经 `cu_next_rounded`（≥ NUM_CU 则回绕）变成 **CU0**。`cu_cnt` 清零。转 `RESOURCE_CHECK`。
3. `RESOURCE_CHECK`（第 1 周期，检查 CU0）：
   - vgpr：`size0=1024 ≥ 500` ✓；`size1=0 ≥ 500` ✗ → `result_line_vgpr.orR = 1`。
   - sgpr：假设 ✓。lds：`num_lds=0`，任何 `size ≥ 0` 都成立 ✓。
   - WG slot：`wgslot(CU0)=00000000`，`.andR=false` → `!.andR=true` ✓。
   - WF slot：`NUM_WF_SLOT(8) - wfslot(CU0)(0) = 8 ≥ 4` ✓。
   - 故 `resource_check_result(0) = 1` → `.orR=1` → 转 `ALLOC`；选中片段 = `PriorityMux` 选出的 vgpr `size0`（index 0）。`cu = CU0`。
4. `ALLOC`：扣 RTcache（CU0 的 vgpr `size0: 1024→524`）、置 WG slot 位、`wfslot: 0→4`、发 `rt_alloc`（`cu_id=CU0, num_vgpr=500,...`）、发分配结果给 cu_interface、回送 `accept=true` 给 wg_buffer。全部握手后回 `IDLE`。

**需要观察的现象**：

- 首选 CU 是 **CU0** 而非 CU1，体现了「轮询」（从上次分配的 CU1 的下一个开始）。
- 容纳性判定是「**三资源与双槽位同时满足**」的与运算：CU1 虽然 sgpr 够、WG slot 有空，但 vgpr 最大片段才 300 < 500，会被判为不可容纳。
- 若把场景改成「CU0 的 vgpr `size0` 也只有 400」，则第 1 周期 CU0 不通过；`cu_cnt=1`，第 2 周期检查 CU1 也不通过；`cu_cnt+1 >= NUM_CU(2)` → 转 `REJECT`，回送 `accept=false`，WG_X 留在 wg_buffer 等下一轮重试。

**预期结果**：你会得到一张状态—条件—动作的三列表（IDLE/CU_PREFER/RESOURCE_CHECK/ALLOC 四行），并能指出「选中 CU = CU0」「选中片段 = vgpr.size0」「wgslot_id = PriorityEncoder(~wgslot(CU0)) = bit0」。

> 本实践为「源码阅读 + 状态推演型」，无需运行命令。WG_X 能否真的被接受，最终取决于资源表算出的真实基址（4.3），本实践只验证分配器侧的决策。

#### 4.1.5 小练习与答案

**练习 1**：`RESOURCE_CHECK` 里 `cu_cnt` 的清零发生在哪个状态？为什么不在进入 `RESOURCE_CHECK` 的同一拍清零？

**参考答案**：清零发生在 `CU_PREFER` 向 `RESOURCE_CHECK` 转移时（[`allocator.scala:193-195`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L193-L195)：`is(FSM.CU_PREFER){ cu_cnt := Mux(fsm_next === FSM.RESOURCE_CHECK, 0.U, DontCare) }`）。也就是说，`CU_PREFER` 这一拍算好下一状态是 `RESOURCE_CHECK`，顺手把 `cu_cnt` 清零，于是进入 `RESOURCE_CHECK` 第 1 拍时 `cu_cnt` 已经是 0。这避免了「进入 RESOURCE_CHECK 当拍既想清零又想 +1」的竞争。

**练习 2**：如果所有 CU 都装不下某个 WG（走到 `REJECT`），这个 WG 会被丢弃吗？它是怎么得到重试机会的？

**参考答案**：不会丢弃。`REJECT` 状态通过 `wgbuffer_result` 回送 `accept=false`（[`allocator.scala:345-346`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L345-L346)）。u3-l1 讲过，wg_buffer 收到 reject 后只是清掉该 WG 的 `wgram_alloc` 位（退回 `(valid=1, alloc=0)`），WG 仍留在 `wgram` 里，下一轮 `function 2` 会用 `RRPriorityEncoder` 重新挑中它再送给分配器。只有等别的 WG 跑完释放了资源、RTcache 回填变大后，它才可能被接受。

**练习 3**：分配器做容纳性判断时为什么用 RTcache 这份「可能滞后」的快照，而不直接去查资源表的链表？

**参考答案**：因为资源表的链表用 `SyncReadMem` 存储且要遍历，查询慢（一次 SCAN 需要 `n+2` 拍，n 为链表节点数）。若每个候选 CU、每拍都去查链表，分配器会长时间卡住，严重拖慢调度吞吐。RTcache 把「最近一次 SCAN 得到的最大几块空闲片段大小」缓存下来，分配器每拍就能组合地判完所有 CU。代价是判断可能滞后，但配合「就地扣除」与资源表的惰性求值，能严格保证**不超量分配**（见 4.2 的语义论证）。

---

### 4.2 RTcache：资源表缓存与 rtcache_writer

#### 4.2.1 概念说明

`RTcache`（Resource Table Cache）是分配器内部、对资源表「空闲资源片段大小」的一份**缓存副本**。它的存在让分配器不必每拍都去遍历链表，就能快速判断「这个 CU 放不放得下」。但缓存就意味着可能与真值不一致——`Allocator.md` 花了大篇幅论证「即使不一致也安全」，这是理解 RTcache 的关键。

RTcache 的核心设计（见 [`Allocator.md` 的 "Resouce table cache" 一节](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/docs/cta_scheduler/Allocator.md)）：

1. **只缓存 `size`，不缓存 `base addr`，且 `size` 的语义是「大于等于」。** 即 RTcache 里某项 `size=k` 表示「该 CU 至少存在一块大小 ≥ k 的空闲片段」，而不是「恰好等于 k」。这样一次 alloc 后只需把 `size` 扣除已分配量即可，内容仍然有效；任何 dealloc 也不会让记录「立即失效」——只是真值可能比缓存值更大，属于「保守估计」。
2. **用 `size=0` 代替 valid 位。** 某项 `size=0` 等价于「无效」（任何需求都 ≥ 0 但「≥ 需求」当需求>0 时不成立）。这省掉了一位 valid。
3. **每 CU 每资源缓存 `NUM_RESULT`（默认 2）块片段。** 多缓存一块能让「最坏估计」更贴近真值。资源表 SCAN 时会把最大的几块片段从低位移入（低位在分配器里优先级最低，故推荐把最大片段放低位，见 [`Allocator.md`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/docs/cta_scheduler/Allocator.md) 的说明）。
4. **BaseAddr 惰性求值。** RTcache 不存基址；分配器只负责「选 CU」，真实基址由资源表在分配后遍历链表算出，且**选最小可容纳片段**以防碎片化。

#### 4.2.2 核心流程

RTcache 有**两个写来源**，由 [`rtcache_writer`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L61-L100) 统一仲裁写入每 CU 的 RTcache 寄存器：

```
            ┌── alloc_wr（ALLOC 上升沿）：把选中片段 size 就地扣除 alloc_size ──┐
rt_result ──┤                                                            ├──▶ rtcache_wr ──▶ rtcache(cu)
(资源表回填) └── rt_result.fire：直接写入资源表 SCAN 给出的最新 size 向量 ──┘
```

- **来源 A：分配扣除**。分配器在 ALLOC 状态，对选中 CU 的选中片段 `alloc_sel`，把 `size(alloc_sel) := size(alloc_sel) - alloc_size`（其余片段不变）。这发生在 `alloc_en` 的**上升沿**（`alloc_wr = !alloc_en_r1 && alloc_en`），保证一次分配只扣一次。
- **来源 B：资源表回填**。资源表完成一次 SCAN 后经 `rt_result_*` 送来最新的 `size` 向量，直接整体覆盖该 CU 的 RTcache。这是「真相同步」。

**写锁定**（保证正确性的关键）：在分配器处于 ALLOC 状态、且即将/正在对一个 CU 发 `rt_alloc` 期间，该 CU 的 RTcache 被「锁住」，不再接受新的 `rt_result`（[`allocator.scala:80`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L80)）。因为新的 alloc 请求马上要发给资源表，资源表里那些「即将生成 / 已经生成但还没送出」的旧 SCAN 结果马上就要失效了——锁住可以避免把失效数据写进 RTcache。等资源表处理完这次 alloc、重新 SCAN 出新结果再回填。

> 「不超量分配」的直觉论证：RTcache 的 `size` 是「≥」语义，且任何 dealloc 只会让真实空闲更大、不会更小。所以「RTcache 说不放得下」时真值也放不下（保守拒绝，至多损失一点性能）；而「RTcache 说放得下」时，真值至少有缓存值那么大，必然真的放得下——再加上 ALLOC 时立刻扣除，后续判断继续保守。故永远不会超量分配。

#### 4.2.3 源码精读

**(a) RTcache 的数据类型**

[`allocator.scala:25-34`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L25-L34) 定义了 RTcache 的条目类型：一个长度为 `NUM_RT_RESULT` 的 `size` 向量，`io_rt2cache` 额外带一个 `cu_id`（指明这份回填属于哪个 CU）：

```scala
trait datatype_rtcache_trait extends Bundle {
  def NUM_RESOURCE: Int
  def NUM_RT_RESULT: Int = CONFIG.RESOURCE_TABLE.NUM_RESULT
  val size = Vec(NUM_RT_RESULT, UInt(log2Ceil(NUM_RESOURCE+1).W))
}
class io_rt2cache(...) extends datatype_rtcache_trait {
  val cu_id = UInt(log2Ceil(CONFIG.GPU.NUM_CU).W)
}
```

注意 `size` 的位宽是 `log2Ceil(NUM_RESOURCE+1)`——能装下「整块资源全空」这个最大值。

**(b) rtcache_writer：两来源仲裁**

[`allocator.scala:61-100`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L61-L100) 是 RTcache 的写控制器。核心三段：

```scala
val alloc_en_r1 = RegNext(io.alloc_en)
val alloc_wr = !alloc_en_r1 && io.alloc_en                          // ALLOC 上升沿：只扣一次
io.rt_result.ready := !io.alloc_en || (!alloc_wr && (io.rt_result.bits.cu_id =/= io.alloc_cuid))  // 写锁定
io.rtcache_wr_en := io.rt_result.fire || alloc_wr                   // 两种来源之一
when(alloc_wr) {                                                    // 来源 A：就地扣除
  io.rtcache_wr_cuid := io.alloc_cuid
  for(i <- 0 until NUM_RT_RESULT)
    io.rtcache_wr_data.size(i) := io.alloc_rawdata.size(i) - Mux(i.U === io.alloc_sel, io.alloc_size, 0.U)
} .elsewhen(io.rt_result.fire) {                                    // 来源 B：整体回填
  io.rtcache_wr_data.size := io.rt_result.bits.size
  io.rtcache_wr_cuid := io.rt_result.bits.cu_id
}
```

读法：
- `alloc_wr` 是 `alloc_en` 的**上升沿**，所以即便 ALLOC 状态持续多拍（等下游握手），「扣除」也只发生一次。
- 扣除时，**只对选中片段 `alloc_sel` 减去 `alloc_size`**，其余片段原样保留（`Mux(i===alloc_sel, alloc_size, 0)`）。这保证「向一块片段 alloc 不影响另一块的有效性」。
- 写锁定表达式：当 `alloc_en` 为真（在 ALLOC）时，除非「不是上升沿 且 这个 rt_result 不是给当前 alloc CU 的」，否则不接受 rt_result。换言之，正在被分配的 CU 的 rt_result 会被挡住。

**(c) 分配器里三份 RTcache 与三个 writer 的接线**

分配器对 LDS/sGPR/vGPR 各维护一份 `Vec(NUM_CU)` 的 RTcache 寄存器，并各接一个 `rtcache_writer`（[`allocator.scala:131-155`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L131-L155)）：

```scala
val writer_lds  = Module(new rtcache_writer(NUM_RESOURCE = CONFIG.WG.NUM_LDS_MAX, ...))
when(writer_lds.io.rtcache_wr_en)  { rtcache_lds(writer_lds.io.rtcache_wr_cuid)  := writer_lds.io.rtcache_wr_data }
// ... sgpr / vgpr 同理
writer_lds.io.rt_result  <> io.rt_result_lds   // 资源表回填接进来
```

在 ALLOC 状态，分配器把选中 CU 的当前 RTcache 值、选中片段、扣除量喂给 writer（[`allocator.scala:299-313`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L299-L313)），由 writer 在上升沿完成扣除。

#### 4.2.4 代码实践

**实践目标**：用一个具体的 RTcache 状态，验证「就地扣除」与「≥ 语义」如何保证一次 alloc 之后 RTcache 仍有效、且不超量。

**给定场景**：CU0 的 `rtcache_vgpr = {size0=1024, size1=0}`（即「至少有一块 ≥1024 的空闲片段」，复位值）。一个 WG 要 `num_vgpr=500`，RESOURCE_CHECK 选中了 `size0`（index 0）。

**操作步骤**：

1. 进入 ALLOC，`alloc_wr` 上升沿触发扣除：
   - `size0 := 1024 - 500 = 524`（因为 `i===alloc_sel(0)` 减 `alloc_size=500`）。
   - `size1 := 0 - 0 = 0`（不变）。
2. 扣除后 `rtcache_vgpr(CU0) = {524, 0}`。
3. 现在假设资源表真值为：CU0 原本全空（1024 全部空闲），分配 500 后真实剩余 `[500, 1023]` 共 524。比较：缓存 `size0=524` 与真实最大空闲片段 524——**恰好相等**，缓存有效。
4. 再考虑一个「真值比缓存大」的例子：假设刚才 `size0` 其实是因为之前一次 dealloc 还没回填，真值是 800。缓存是 524（< 真值 800）。此时若再来一个 WG 要 600：缓存说 524 < 600，**不放行**（保守拒绝）；但真值 800 是够的——这是「性能损失」而非「正确性错误」。反之，缓存说放得下时真值必然 ≥ 缓存值，必真的放得下。

**需要观察的现象**：

- 扣除只发生在**选中片段**，另一片段 `size1` 不受影响（即便它非零）。
- 扣除后 `size0` 仍满足「≥ 真实最大空闲片段」的关系（这里恰好相等；dealloc 场景下缓存值是下界）。

**预期结果**：你会得到一张「扣除前 / 扣除后 / 真实值」的对照表，并能在文字里论证：「由于 `size` 是 ≥ 语义且单调随 alloc 递减、随 dealloc 只增不减（真值层面），RTcache 始终是真实最大空闲片段的**下界**，故据此判断永远不会超量分配。」

> 本实践为「源码阅读 + 数值推演型」，无需运行命令。若想用仿真观察，可在 sim-verilator 中抓 `allocator` 实例的 `rtcache_vgpr` 寄存器波形，但能否抓到内部寄存器取决于仿真可见性，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 RTcache 用「`size=0` 表示无效」而不是单独加一个 valid 位？

**参考答案**：因为容纳性判定的条件是 `size >= 需求量`。当某项 `size=0` 时，只要需求量 > 0，`0 >= 需求` 必为假，等价于这一项「不参与容纳性判定」——也就是「无效」。所以 `size=0` 天然具备 valid=0 的语义，单独加 valid 位是冗余的（`Allocator.md` 明确指出这一点）。复位时把不存在的片段置 0 即可。

**练习 2**：`rtcache_writer` 的 `alloc_wr` 用了「上升沿」（`!alloc_en_r1 && alloc_en`）而不是「电平」（`alloc_en`）。如果改成电平触发，会出什么问题？

**参考答案**：ALLOC 状态可能持续多拍（要等 `rt_alloc`、cu_interface、wg_buffer 三个下游都握手）。若用电平触发（`alloc_en` 为真就扣），则每一拍都会扣一次 `alloc_size`，RTcache 会被多扣成负数（代码里有 `assert(alloc_rawdata.size(alloc_sel) >= alloc_size)` 会触发）。用上升沿保证「一次分配只扣除一次」。

**练习 3**：资源表 SCAN 回填时，为什么推荐「把最大片段放在 RTcache 的低位（index 0）」，而分配器优先选用高位片段？

**参考答案**：这是文档约定的「优先级」配合（[`Allocator.md`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/docs/cta_scheduler/Allocator.md)）。分配器在 RESOURCE_CHECK 里用 `PriorityMux(...,result_line.reverse, ...)` 选「优先级最高」的片段，并把高 index 作为高优先级（[`allocator.scala:257-259`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/allocator.scala#L257-L259)）。于是分配器**优先消耗较小的片段（高 index）**，把最大片段（低 index）尽量留作「保底」，这样后续更大的 WG 仍有机会被容纳，间接降低了碎片化与拒绝率。

---

### 4.3 resource_table：链表式资源管理与基址求值

#### 4.3.1 概念说明

如果说 RTcache 是分配器的「粗略地图」，那么 `resource_table` 就是「精确的地籍册」。它为**每个 CU、每类资源（LDS/sGPR/vGPR）** 各维护一张**双向链表**，链表按资源地址从低到高，串联起该 CU 上所有正在运行的 WG 各自占用的资源片段。给出 alloc 请求时，它遍历链表找出一段空闲区间并把基址交给 cu_interface；给出 dealloc 请求时，它把对应节点从链表上摘掉。

`Resouce table.md` 把它概括为四点（见 [文档「整体概述」](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/docs/cta_scheduler/Resouce%20table.md)）：

1. 每类资源**独立**拥有一套资源表（独立链表、独立 SCAN）。
2. 每个 CU 拥有一个独立的 RT RAM，长度 `NUM_WG_SLOT`；按 `WG_SLOT_ID` 寻址写入/释放。每项 4 字段：首地址、尾地址、`prev`、`next`。双向链表按地址升序串联。
3. 由于 alloc/dealloc 不频繁，多个 CU 的 RTram **可以共享**一套处理逻辑（Table Handler）；当前实现先用「直接映射」（每个 CU 一个 handler）。
4. Handler 与它服务的 RTram 在硬件上作为一个整体，称为一个 **group**。

`resource_table_top` 还要满足顶层强约束（[文档「来自Top的要求」](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/docs/cta_scheduler/Resouce%20table.md)）：

- 收到某 CU 的 alloc 后，**立即丢弃**该 CU 已生成/正生成的 RT result（因为马上要变）。
- **整个资源表同一时刻最多处理一个 alloc 请求**（保证三路基址对齐）。
- cache update（回填 RTcache）要给出几段互相独立的空闲片段大小。

#### 4.3.2 核心流程

**(a) 链表结构（每 CU 每资源一份）**

用 C 风格伪代码表示（取自 [`Resouce table.md` 的 "Resouce table RAM" 与 alloc/dealloc 伪代码](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/docs/cta_scheduler/Resouce%20table.md)）：

```c
struct rt_node {            // 存于 RTram[NUM_WG_SLOT]，按下标 = WG_SLOT_ID 寻址
    slotid_t prev, next;    // 双向链表指针（按资源地址升序）
    addr_t   addr1, addr2;  // 本 WG 占用区间的首地址、尾地址（闭区间）
};
slotid_t head, tail;        // 链表首/末节点的 WG_SLOT_ID
count_t  cnt;               // 链表节点数
```

一个有 `cnt` 个 WG 的链表，把整段资源 `[0, NUM_RESOURCE-1]` 切成 `cnt+1` 段（每两个相邻 WG 之间一段、链头之前一段、链尾之后一段），其中可能有大小为 0 的段。

**(b) handler 主状态机**

每个 `resource_table_handler` 跑一个主状态机（[`resource_table.scala:110-112`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/resource_table.scala#L110-L112)）：`IDLE, CONNECT_ALLOC, ALLOC, CONNECT_DEALLOC, DEALLOC, SCAN, OUTPUT`。主干：

```
IDLE ─(alloc)─▶ [CONNECT_ALLOC] ─▶ ALLOC ─▶ SCAN ─▶ OUTPUT ─▶ IDLE
       (dealloc)─▶ [CONNECT_DEALLOC]─▶ DEALLOC ─▶ SCAN ─▶ OUTPUT ─▶ IDLE
```

- `CONNECT_*`：当请求的目标 CU 与 handler 当前接的 RTram 不同时，先花一拍切换总线 MUX（当前直接映射下是纯组合，常被跳过）。
- `ALLOC`：跑子状态机 `FSM_A`，遍历链表找最小可容纳片段 → 写入新节点 → 输出基址。
- `DEALLOC`：2 拍摘除指定节点（仅 relink，不合并地址区间）。
- `SCAN`：3 级流水遍历链表，重新算出最大的 `NUM_RT_RESULT` 块空闲片段。
- `OUTPUT`：把 SCAN 结果经 `rtcache_update` 回填给分配器的 RTcache。

主状态机**允许同 CU 的 alloc/dealloc 抢占** SCAN/OUTPUT（黄色/绿色状态可抢占，灰色不可），但**不同 CU 的请求必须等回 IDLE**（[`resource_table.scala:136-145`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/resource_table.scala#L136-L145)）——因为 handler 一次只接一个 CU 的 RTram。

**(c) ALLOC 子状态机的「最佳适配」**

`FSM_A`（[`resource_table.scala:235-242`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/resource_table.scala#L235-L242)）：`IDLE → FIND → WRITE_OUTPUT`。

- `FIND`：3 级流水（更新指针 → 取 addr1/addr2 → 算片段大小并比较），逐段检查 `cnt+1` 段空闲区间。选中条件是「**能容纳 (`size >= wgsize`) 且比当前候选更小 (`size < found_size`)**」（[`resource_table.scala:305`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/resource_table.scala#L305)）。初始 `found_size = NUM_RESOURCE`（假设占整块），所以最终选到的是「**能容纳的最小片段**」——最佳适配（best-fit），防碎片化。
- `WRITE_OUTPUT`：把新 WG 的节点写入 RTram（`addr1=base, addr2=base+wgsize-1`，链接 `prev/next`，更新 `head/tail/cnt`），并把 `base` 经 `io.baseaddr` 输出。若 `wgsize==0`（该 WG 不消耗此项资源），跳过遍历与插链，直接给一个基址（[`resource_table.scala:400`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/resource_table.scala#L400)）。

**(d) DEALLOC 与 SCAN**

- `DEALLOC`（[`resource_table.scala:412-444`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/resource_table.scala#L412-L444)）：读出待删节点的 `prev/next`，把前驱的 `next` 指向后继、后继的 `prev` 指向前驱，并按需更新 `head/tail/cnt`。固定 2 拍。**不做地址区间合并**——因为空闲片段大小每次都由 SCAN 重新算。
- `SCAN`（[`resource_table.scala:449-517`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/resource_table.scala#L449-L517)）：结构与 FIND 几乎相同，但逐段算出空闲片段大小后，用 `sort3`（[`utils.scala:56-91`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/utils.scala#L56-L91)）维护「至今最大的 NUM_RT_RESULT 块」到一个 `rtcache_data` 寄存器，扫描完经 OUTPUT 回填。

#### 4.3.3 源码精读

**(a) RTram：每 CU 的链表存储**

[`resource_table.scala:574-615`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/resource_table.scala#L574-L615) 是一个 CU 一类资源的存储体。`prev/next/addr1/addr2` 用 `SyncReadMem`（省面积，但读要打一拍——这是子状态机做成流水的根本原因），`cnt/head/tail` 用普通寄存器：

```scala
val prev  = SyncReadMem(NUM_WG_SLOT, UInt(log2Ceil(NUM_WG_SLOT).W))
val next  = SyncReadMem(NUM_WG_SLOT, UInt(log2Ceil(NUM_WG_SLOT).W))
val addr1 = SyncReadMem(NUM_WG_SLOT, UInt(log2Ceil(NUM_RESOURCE).W))
val addr2 = SyncReadMem(NUM_WG_SLOT, UInt(log2Ceil(NUM_RESOURCE).W))
val cnt = RegInit(...); val head = Reg(...); val tail = Reg(...)
```

`io_rtram`（[`resource_table.scala:61-74`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/resource_table.scala#L61-L74)）把这些字段包装成统一的读写端口给 handler 用。DEBUG 模式下还多一组 `wgid/valid` 寄存器阵列，并断言 `PopCount(valid) === cnt`（[`resource_table.scala:613`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/resource_table.scala#L613)）——随时自检链表节点数与有效位一致。

**(b) ALLOC 子状态机的 FIND（3 级流水）**

`SyncReadMem` 给地址后下一拍才出数据，所以 FIND 做成 3 级流水（[`resource_table.scala:281-325`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/resource_table.scala#L281-L325)）：

```
stage0：更新遍历指针 ptr1←ptr2，向 rtram.next 发读请求（ptr2）
stage1：ptr2←head 或 next.rd.data；向 rtram.addr1/addr2 发读请求
stage2：得到 addr1/addr2，算本段 size，与候选比较，更新 found_*
```

片段大小与基址的计算（[`resource_table.scala:301-307`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/resource_table.scala#L301-L307)）：

```scala
// addr1 = 本段起始地址 - 1（首段用全 1 即 -1），addr2 = 本段结束地址（末段用 NUM_RESOURCE-1）
addr1 := Mux(fsm_a_init_p1,   (~0.U).asUInt,             rtram_alloc.addr2.rd.data)
addr2 := Mux(fsm_a_finish_p1, (NUM_RESOURCE-1).U,        rtram_alloc.addr1.rd.data - 1.U)
val size = addr2 - addr1                                       // 本段空闲大小
val result_update = (size >= wgsize) && (size < fsm_a_found_size)
fsm_a_found_size := Mux(fsm_a_valid_p1 && result_update, size, fsm_a_found_size)
fsm_a_found_addr := Mux(fsm_a_valid_p1 && result_update, addr1 + 1.U, fsm_a_found_addr)  // 基址 = addr1+1
```

逻辑很巧妙：用 `addr1 = 段首-1`、`addr2 = 段尾`，则 `size = addr2 - addr1`、`base = addr1 + 1`，把「链头之前的虚拟边界」和「链尾之后的虚拟边界」用 `-1` 与 `NUM_RESOURCE-1` 统一处理。最佳适配由 `size < found_size` 实现——初始 `found_size = NUM_RESOURCE`，所以总选最小可容纳段。

**(c) DEALLOC 的 O(1) 摘链**

[`resource_table.scala:414-428`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/resource_table.scala#L414-L428)：

```scala
fsm_d_prev := rtram_dealloc.prev(wgslot)   // 待删节点的前驱
fsm_d_next := rtram_dealloc.next(wgslot)   // 待删节点的后继
rtram_dealloc.next.wr.en := (fsm_d===1.U) && (wgslot =/= head)   // 非头节点：前驱.next = 后继
rtram_dealloc.prev.wr.en := (fsm_d===1.U) && (wgslot =/= tail)   // 非尾节点：后继.prev = 前驱
rtram_dealloc.head.wr.en := (fsm_d===1.U) && (wgslot === head)   // 删的是头：head = 后继
rtram_dealloc.tail.wr.en := (fsm_d===1.U) && (wgslot === tail)   // 删的是尾：tail = 前驱
```

`head/tail` 的特判是为了避免读写无效的边界指针。摘链后 `cnt - 1`。注意它**不碰 `addr1/addr2`**——节点逻辑上删除即可，空闲区间留给下次 SCAN 重算。

**(d) SCAN 维护最大若干块**

SCAN 的结构与 FIND 同构，只是 stage2 不再「比较找最小」，而是把每段 `size` 与当前 `rtcache_data` 一起送进 `sort3` 降序排序，保留前 `NUM_RT_RESULT` 大（[`resource_table.scala:476-484`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/resource_table.scala#L476-L484)）。`NUM_RT_RESULT=2` 时用 `sort3(已有size0, 已有size1, 本段size)` 取前两个。扫描完进 `OUTPUT`，把 `rtcache_data` 经 `rtcache_update` 回填（[`resource_table.scala:522-525`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/resource_table.scala#L522-L525)）。

**(e) resource_table_top：路由与「三路基址对齐」**

顶层 [`resource_table.scala:620-877`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/resource_table.scala#L620-L877) 例化 `NUM_HANDLER`（默认 = `NUM_CU`）组「handler + rtram」，并构筑三类资源的路由。三个关键点：

1. **ALLOC 路由 + 「全局最多一个 alloc 在途」**（[`resource_table.scala:714-725`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/resource_table.scala#L714-L725)）：

   ```scala
   val alloc_record = RegInit(false.B)
   alloc_record := MuxCase(alloc_record, Seq(io.alloc.fire -> true.B, io.cuinterface_wg_new.fire -> false.B))
   val alloc_allowed = WireInit(!alloc_record || io.cuinterface_wg_new.fire)
   ```

   `alloc.fire` 后置位，直到三路基址汇齐送出（`cuinterface_wg_new.fire`）才清位。期间 `alloc_allowed=0`，新的 alloc 被挡住。alloc 请求经 `DecoupledIO_1_to_3` 拆成 LDS/sGPR/vGPR 三股，按 `convert_cu_id` 算出的 group 路由到对应 handler（[`resource_table.scala:726-759`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/resource_table.scala#L726-L759)）。

2. **DEALLOC 路由 + WG/WF slot 释放转发**（[`resource_table.scala:768-814`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/resource_table.scala#L768-L814)）：dealloc 也拆三股，但用 `IGNORE` 标志跳过「该 WG 用量为 0 的资源」（`lds_dealloc_en` 等，来自 alloc 时记录）。同时 WG/WF slot 这两类「槽位资源」不进链表，由顶层用一个 `Queue` 转发给分配器的 `slot_dealloc`（[`resource_table.scala:769, 777-778`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/resource_table.scala#L769-L778)）。

3. **三路基址对齐**（[`resource_table.scala:853-862`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/resource_table.scala#L853-L862)）：三类资源的 `baseaddr` 各经一个 `Queue(flow=true)`，三者 `valid` 相与后才送 `cuinterface_wg_new`：

   ```scala
   io.cuinterface_wg_new.valid := baseaddr_lds_reg.valid && baseaddr_sgpr_reg.valid && baseaddr_vgpr_reg.valid
   io.cuinterface_wg_new.bits.lds_base  := baseaddr_lds_reg.bits.addr
   io.cuinterface_wg_new.bits.sgpr_base := baseaddr_sgpr_reg.bits.addr
   io.cuinterface_wg_new.bits.vgpr_base := baseaddr_vgpr_reg.bits.addr
   ```

   DEBUG 下还断言三者 `wg_id` 一致（[`resource_table.scala:864-868`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/resource_table.scala#L864-L868)），保证 cu_interface 收到的 `lds_base/sgpr_base/vgpr_base` 属于同一个 WG。这就是「全局最多一个 alloc 在途」的意义——只有串行化 alloc，才能让三路基址天然对齐。

#### 4.3.4 代码实践

**实践目标**：用一个两步场景（先 alloc 一个 WG、再 dealloc 它），手工推演一张 vGPR 链表的演化，验证「最佳适配找基址」与「O(1) 摘链」的行为，并算出回送给 cu_interface 的 `vgpr_base`。

**给定场景**：CU0 的 vGPR，`NUM_VGPR_MAX = 1024`，链表初始为空（`cnt=0, head/tail=DontCare`）。

**操作步骤**：

1. **Alloc WG_A（`num_vgpr=200`，落在 `wg_slot_id=3`）**：
   - FIND：`cnt=0`，只有 1 段 = 全部资源 `[0,1023]`，size=1024。`1024 >= 200` 且 `1024 < found_size(1024)` 为假，故不更新——保留初始假设 `found_addr=0, found_size=1024, head_flag=tail_flag=true`。
   - WRITE_OUTPUT：写入节点 3：`addr1=0, addr2=199, prev/next=DontCare`，`head=tail=3, cnt=1`。输出 `base=0`。
   - 结果：`vgpr_base(WG_A) = 0`，链表 `[3: 0~199]`。
2. **Alloc WG_B（`num_vgpr=300`，`wg_slot_id=5`）**：
   - FIND：`cnt=1`，检查 2 段。
     - 段 0（节点 3 之前）：`addr1=-1`（init），`addr2 = node3.addr1-1 = -1`，`size=0`，不满足。
     - 段 1（节点 3 之后）：`addr1 = node3.addr2 = 199`，`addr2 = 1023`（finish），`size = 1023-199 = 824`。`824>=300` 且 `824<1024` → 更新：`found_addr=200, found_size=824, tail_flag=true`。
   - WRITE_OUTPUT：写入节点 5：`addr1=200, addr2=499`，链接 `node3.next=5, node5.prev=3`，`tail=5, cnt=2`。输出 `base=200`。
   - 结果：`vgpr_base(WG_B) = 200`，链表 `[3:0~199] → [5:200~499]`。
3. **Dealloc WG_A（`wg_slot_id=3`）**：
   - 读 `node3.prev=DontCare`（头）、`node3.next=5`。
   - 因 `wgslot==head`：`head := next = 5`；因 `wgslot != tail`：`node5.prev := node3.prev`（新头的前驱置 DontCare）。`cnt=1`。
   - 链表变为 `[5:200~499]`（节点 3 逻辑删除，其 `addr` 字段留着但不参与）。注意：删除后真实空闲 = `[0,199] ∪ [500,1023]`，但**此刻还没合并体现到任何缓存**——要等下一次 SCAN 重算。

**需要观察的现象**：

- WG_B 得到 `base=200` 而非 0，因为最佳适配在唯一可容纳段 `[200,1023]` 上取了起始地址。
- Dealloc 只改指针、不改 addr，2 拍完成；真实空闲区间的「碎片」要靠 SCAN 重算（下一个 alloc 到来前的 SCAN 会把 `{200, 824}` 之类回填进 RTcache）。
- 三类资源的基址要全部就绪，顶层才向 cu_interface 发一次 `cuinterface_wg_new`。

**预期结果**：你会得到一张「操作 / 链表状态 / 输出基址 / cnt / head / tail」的演化表，并能指出 WG_A 的 `vgpr_base=0`、WG_B 的 `vgpr_base=200`，以及 dealloc WG_A 后链表只剩节点 5。

> 本实践为「源码阅读 + 数据结构推演型」，无需运行命令。SCAN 在 dealloc 后会重新算出空闲片段 `{size0=200(段[0,199]), size1=524(段[500,1023])}`（降序），回填 RTcache——这一步可结合 4.2 自行验证。

#### 4.3.5 小练习与答案

**练习 1**：ALLOC 的 FIND 为什么初始把 `found_size` 设成 `NUM_RESOURCE`（整块资源大小），而不是 0？如果把初值设成 0 会怎样？

**参考答案**：因为选中条件是 `size >= wgsize && size < found_size`——要找「比当前候选**更小**的可容纳段」。初值设成 `NUM_RESOURCE`（最大可能值）意味着「假设一开始选中了整块资源这个最大的段」，之后任何比它小的可容纳段都会取代它，最终得到最小可容纳段（best-fit）。若初值设成 0，则 `size < 0` 永假，`result_update` 永远不成立，`found_*` 永远停在初值——找不到任何段，分配失败。所以初值必须是「上界」。

**练习 2**：DEALLOC 只摘链、不合并相邻空闲地址区间。这会不会导致之后 ALLOC 算错基址？

**参考答案**：不会。因为 ALLOC 的 FIND 和 SCAN 都是**当场遍历整条链表、根据现存节点的 `addr1/addr2` 重新计算每段空闲大小**，而不是依赖任何「预合并」的结构。dealloc 摘掉节点后，那个节点不再出现在链表上，它原本占用的区间自然被算进相邻的空闲段（相当于隐式合并）。所以「不显式合并」不影响正确性，反而省掉了 dealloc 时的合并逻辑——这是用「SCAN 重算」换「dealloc 简化」的取舍。

**练习 3**：顶层为什么强制「整个资源表同一时刻最多处理一个 alloc 请求」？如果不限制，会出什么问题？

**参考答案**：因为 alloc 到基址的路径**不是 FIFO**的（三个资源的 handler 各自独立跑 FIND，耗时随各自链表长度不同），而且最终要把 LDS/sGPR/vGPR **三路基址对齐**后送给 cu_interface（[`resource_table.scala:856-859`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/resource_table.scala#L856-L859)）。若同时有 WG_X 和 WG_Y 两个 alloc 在途，三路基址可能交叉错位，无法保证「这三路基址属于同一个 WG」。用 `alloc_record` 串行化后（[`resource_table.scala:717-722`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/resource_table.scala#L717-L722)），任何时刻只有一个 WG 的三路在跑，天然对齐。代价是 alloc 吞吐受限，但 alloc 本就不频繁，可接受。

---

## 5. 综合实践

把 4.1～4.3 串起来，完成下面这个贯穿全讲的任务（即本讲的指定实践任务）。

**任务**：梳理 `allocator` 的状态机转移条件，编写一段**文字 + 伪代码**，描述「当一个请求 4 个 warp、占用若干 VGPR/SGPR/LDS 的 block 到来时，调度器如何逐步选定 CU 并返回基地址」的完整过程。

**给定输入**：

- block（WG）资源需求：`num_wf = 4`，`num_vgpr = 500`，`num_sgpr = 600`，`num_lds = 4096`（字节）。
- 默认规模 `NUM_CU = 2`；`NUM_VGPR_MAX = 1024`，`NUM_SGPR_MAX = 2048`，`NUM_LDS_MAX = sharemem_size`。
- 假设 CU0 全空（RTcache 三类资源 `size0` 均为各自上限、`size1=0`，`wgslot=0, wfslot=0`）；CU1 已有较多占用（vgpr 最大片段 300 < 500，放不下）。上一个被分配的 CU 是 CU1。

**要求你的回答包含**：

1. **分配器侧（4.1）**：用伪代码写出 `IDLE → CU_PREFER → RESOURCE_CHECK → ALLOC` 的转移条件与每步动作，指明：
   - 首选 CU 是谁、为什么（轮询：`cu = last_alloc_cu + 1`）。
   - RESOURCE_CHECK 如何对 CU0 算出 `resource_check_result=1`（列出三类资源与双槽位的五个「与」条件）。
   - ALLOC 的五项任务各做了什么，特别指出 `rt_alloc` 里发了哪些字段、**不含基址**。
2. **RTcache 侧（4.2）**：说明 ALLOC 上升沿如何把 CU0 的三类 RTcache 选中片段「就地扣除」（写出 vgpr `size0: 上限→上限-500` 的变化），以及为什么此时 CU0 的 `rt_result` 被写锁定。
3. **资源表侧（4.3）**：说明 `resource_table_top` 收到 `rt_alloc` 后，如何拆成 LDS/sGPR/vGPR 三股路由到 CU0 的三个 handler；每个 handler 的主状态机走 `IDLE → ALLOC(FIND+WRITE_OUTPUT) → SCAN → OUTPUT`，FIND 如何遍历（空的）链表得到基址（首节点基址必为 0），三类基址如何在顶层 `Queue` 里「三路对齐」后经 `cuinterface_wg_new` 回送 `lds_base/sgpr_base/vgpr_base`。
4. 用一段话总结：基址从「分配器不知道」到「资源表算出来」是**惰性求值**；选最小可容纳片段是**最佳适配**；RTcache 的「≥语义 + 就地扣除」保证了**不超量分配**。

**操作步骤**：

1. 先按 4.1.4 的方法列出分配器状态转移表。
2. 再按 4.2.4 写出 RTcache 扣除前后的数值。
3. 最后按 4.3.4 推演 CU0 三类资源（空链表）的 ALLOC，得到三个基址（`lds_base=0, sgpr_base=0, vgpr_base=0`，因为 CU0 全空、首节点基址必为 0）。
4. 用箭头图把「分配器发 rt_alloc → 资源表三股 handler → 三路基址对齐 → cu_interface」画出来。

**预期结果**：你会得到一段可读的伪代码 + 一张端到端流程图，能回答：分配器在 RESOURCE_CHECK 选定 CU0；资源表为 CU0 的三类资源各分配基址 0（空 CU，首段从地址 0 开始）；三路基址对齐后回送 cu_interface，cu_interface 再把它从 WG 级换算成 WF 级（u3-l3 详讲）派发给 CU。这张图也直接为 u3-l3（cu_interface 与 warp 拆分）做了铺垫。

> 本实践为「源码阅读 + 伪代码推演型」，产出为伪代码与流程图，无需运行仿真。

## 6. 本讲小结

- **`allocator`** 用主状态机 `IDLE → CU_PREFER → RESOURCE_CHECK → ALLOC/REJECT` 把一个 WG 逐步选定一个能容纳它的 CU：`CU_PREFER` 用轮询（`上次分配的 CU + 1`）定首选，`RESOURCE_CHECK` 逐 CU 检查，找到即 `ALLOC`、查完无解则 `REJECT`（让 wg_buffer 下轮重试）。
- 容纳性判定是「**三类地址资源（LDS/sGPR/vGPR）的 RTcache 各至少一块够大** AND **WG slot 没满** AND **WF slot 余量 ≥ num_wf**」的「与」运算；分配器同时自管 WG/WF slot（位图/计数），自管 LDS/sGPR/vGPR 的 RTcache（缓存）。
- **`RTcache`** 只缓存 `size`、语义为「≥」，用 `size=0` 代 valid；`rtcache_writer` 在「ALLOC 上升沿就地扣除选中片段」与「资源表 SCAN 回填」两种写来源间仲裁，并在 ALLOC 期间写锁定目标 CU，保证不把失效数据写入——整体保证**不超量分配**。
- **基址惰性求值**：分配器只选 CU、不算基址；真实基址由 `resource_table` 在 alloc 后遍历链表算出，且选**最小可容纳片段**（best-fit，`size < found_size`，初值 `NUM_RESOURCE`）以**抑制碎片化**。
- **`resource_table`** 用双向链表（`prev/next/addr1/addr2`，存于 `SyncReadMem`）按地址升序管理每 CU 每类资源；ALLOC 子状态机 3 级流水找片段+插节点，DEALLOC 仅 `O(1)` 摘链不合并，SCAN 3 级流水 + `sort3` 重算最大若干块回填 RTcache。
- **`resource_table_top`** 用 `alloc_record` 强制「全局最多一个 alloc 在途」，从而让 LDS/sGPR/vGPR 三路基址在 `Queue` 里天然对齐后回送 cu_interface；dealloc 用 `IGNORE` 跳过用量为 0 的资源，并把 WG/WF slot 释放转发给分配器。

## 7. 下一步学习建议

本讲把「分配器 + 资源表 + RTcache」这三个最难的调度零件讲透了。接下来：

- **u3-l3（CU 接口、warp 拆分与释放）**：本讲末尾 `cuinterface_wg_new` 送出的是 **WG 级**的 `lds_base/sgpr_base/vgpr_base` 与分配结果。u3-l3 会讲 `cu_interface` 如何把这些换算成 **WF 级**、把 WG 拆成逐个 warp 派发、用 `wf_tag` 标识每个 warp，以及 warp 全部完成后如何触发本讲的 dealloc 路径（`rt_dealloc`）。建议带着「基址怎么从 WG 级换成 WF 级」「dealloc 是谁触发的」去读。
- **回流阅读文档**：重读 `docs/cta_scheduler/Resouce table.md` 里 ALLOC/DEALLOC/SCAN 的伪代码与本讲 4.3 的源码对照，检验你对「3 级流水线各 stage 做什么」的理解；重读 `Allocator.md` 的 RTcache 一节，体会「≥ 语义 + 惰性求值」这套设计取舍的工程动机。
- **向更下层延伸**：WG 拿到 `vgpr_base` 等基址后，SM 流水线如何用它们定位寄存器堆与共享内存，将在第 4、5 单元（SM 流水线）与第 6 单元（缓存与共享内存）展开——尤其是 `operandCollector` 怎么用 `sgpr_base/vgpr_base` 读操作数、`SharedMemory` 怎么用 `CSR_LDS`（= `lds_base`）寻址。
