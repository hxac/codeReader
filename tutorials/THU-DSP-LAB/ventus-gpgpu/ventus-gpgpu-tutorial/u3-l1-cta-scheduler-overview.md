# CTA 调度器总体与 WG buffer

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 **CTA 调度器（`cta_scheduler_top`）** 在整个 GPU 里的位置：它夹在 host 接口和各 SM 之间，负责把 host 送来的「工作组（workgroup/block）」分配到合适的 SM，再把执行完毕的信号回报给 host。
- 复述调度器内部四大组件 `wg_buffer → allocator → resource_table → cu_interface` 的职责分工，以及它们之间用哪些 `DecoupledIO` 端口对接。
- 讲清 host 侧端口 `host_wg_new` / `host_wg_done` 与 CU 侧端口 `cu_wf_new` / `cu_wf_done` 的语义，理解「调度器收发的是 workgroup、而和 SM 之间收发的是 warp」这一关键转换发生在哪里。
- 拆解 **`wg_buffer`** 的内部结构：双口 RAM `wgram1`/`wgram2`、`wgram_valid`/`wgram_alloc` 两个位向量，以及用 `RRPriorityEncoder`（轮询优先级编码器）挑选读写地址的机制。
- 解释 `wg_buffer` 每个时钟周期并行执行的三个动作（写新 wg、读 wg 给 allocator、处理 allocator 的 accept/reject 结果）如何靠 `valid`/`alloc` 锁做到互不冲突。

本讲是第 3 单元（CTA 任务调度器）的第 1 篇，承接 u2-l1（编程模型与 grid/block/warp/thread）和 u2-l2（`GPGPU_top` 顶层）。本讲只看「调度器作为一个整体怎么连起来、`wg_buffer` 这个零件怎么工作」，**不**深入 allocator 的状态机（留到 u3-l2），也**不**深入 cu_interface 如何把 wg 拆成 warp（留到 u3-l3）。

## 2. 前置知识

在阅读本讲前，你最好已经了解：

- **GPU 编程模型（u2-l1）**：一个 kernel 被划分为若干 **workgroup（WG，即 block）**，每个 WG 又由若干 **wavefront（WF，即 warp）** 组成，每个 WF 含 `num_thread`（默认 32）个 thread。同一 WG 的所有 WF 必须落在同一个 SM 上。
- **`GPGPU_top` 顶层（u2-l2）**：顶层里有一个无状态的协议适配层 `CTAinterface`，它内部例化了 `cta_scheduler_top`，把 host 视角的 `host2CTA_data` 翻译成调度器能吃的 `io_host2cta`，并把派发结果转给各 SM 的 `CTA2warp`。
- **Chisel 基础**：`Bundle` 是一组线的集合；`DecoupledIO` 是标准握手接口，含 `valid`/`ready`/`bits`，`fire = valid && ready`；`Flipped(...)` 把方向取反；`<>` 把两个端口对接；`Mem(n, T)` 是可综合的随机存储器。
- **关键规模参数**：默认 `num_sm=2`、`num_warp=8`、`num_thread=32`。在调度器语境里，SM 被称为 **CU（Compute Unit）**，所以 `NUM_CU = num_sm = 2`。

一个对本讲至关重要的直觉：**host 一次只能「递交」一个完整的 workgroup，但 SM（CU）一次只能「吃下」一个 warp。** 中间这个「整体收、拆开喂」的过程，外加「哪个 SM 还有足够的寄存器/共享内存来装下这个 WG」的判断，就是 CTA 调度器要解决的核心问题。你可以把它想象成一个物流分拣中心：货物按整箱进场（WG），分拣员要查每个车间（CU）还剩多少货架（寄存器/LDS），选一个放得下的车间，再拆成小包裹（warp）逐件投递。

> 术语对齐：文档和源码里 **WG = workgroup = block = CTA**，**WF = wavefront = warp**，**CU = SM**。本讲在讲调度器内部时统一用源码的 WG/WF/CU 说法。

## 3. 本讲源码地图

本讲围绕 `cta` 包下的两个核心文件：

| 文件 | 作用 |
|------|------|
| [`ventus/src/cta/cta_scheduler.scala`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cta_scheduler.scala) | 定义调度器顶层 `cta_scheduler_top`，以及它对外、对内的全部 IO bundle（`io_host2cta`、`io_cta2host`、`io_cuinterface2cu` 等）。是本讲 4.1 节的主角。 |
| [`ventus/src/cta/wg_buffer.scala`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/wg_buffer.scala) | 定义 `wg_buffer` 模块——缓存 host 送来的 WG，并以轮询方式喂给 allocator。是本讲 4.2 节的主角。 |

理解它们需要参考的辅助文件：

| 文件 | 作用 |
|------|------|
| [`ventus/src/cta/utils.scala`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/utils.scala) | `wg_buffer` 用来挑选地址的 `RRPriorityEncoder`（轮询优先级编码器）定义在此。 |
| [`ventus/src/top/parameters.scala`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala) | `CTA_SCHE_CONFIG` 子对象，集中定义 `GPU.NUM_CU`、`WG_BUFFER.NUM_ENTRIES`、各资源上限等。调度器内所有位宽都由它推导。 |
| [`ventus/src/top/GPGPU_top.scala`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala) | `CTAinterface` 在此例化 `cta_scheduler_top`，并把字段逐一映射给 `host2CTA_data`；调度器的 `cu_wf_new` 经这里送往 `CTA2warp`。 |
| `docs/cta_scheduler/Top.md`、`docs/cta_scheduler/wg_buffer.md` | 官方设计文档，配有框图，是本讲实践任务的对照基准。 |

> 小贴士：调度器代码里几乎每个位宽都写成 `log2Ceil(CONFIG.WG.NUM_xxx_MAX+1).W` 这种形式。阅读时不必死记具体数值，只要知道「它正好能装下该资源的最大值」即可。具体数值在 u2-l3 的参数系统里讲过。

## 4. 核心概念与源码讲解

### 4.1 cta_scheduler_top：调度器顶层与四大组件

#### 4.1.1 概念说明

**CTA 调度器**（CTA = Cooperative Thread Array，即 workgroup）是 GPU 里专门负责「任务分发」的硬件模块。它要回答两个问题：

1. **往哪个 SM 送？** 一个 WG 要占用一定数量的寄存器（sGPR/vGPR）和共享内存（LDS）。只有剩余资源足够的 SM 才能接下它；同一 WG 的所有 warp 又必须落在同一个 SM。所以调度器必须实时掌握每个 SM 的资源余量，挑一个「放得下」的。
2. **怎么送进去、怎么收尾？** host 递交的是整个 WG，而 SM 的流水线一次只接收一个 warp。调度器要负责把 WG 拆成逐个 warp 派发；等这些 warp 全部跑完，再回收资源、通知 host「这个 WG 完事了」。

为了把这两件事做清楚，`cta_scheduler_top` 内部例化了 **四个子模块**，各司其职：

| 子模块 | 职责（一句话） |
|--------|----------------|
| `wg_buffer` | 缓存 host 送来的 WG，挑一个喂给 allocator；按 allocator 的判定结果决定「转交 cu_interface」还是「留下来下一轮再试」。 |
| `allocator` | 决定每个 CU 的优先级、检验各 CU 剩余资源能否容纳此 WG，综合后挑一个目标 CU；并向 resource_table 申请生成资源基址。 |
| `resource_table` | 用链表记录每个 CU 上运行中的 WG 所占资源片段的基址与大小；处理 alloc/dealloc，并把「最大几块空闲资源」汇报给 allocator（经 RTcache）。 |
| `cu_interface` | 把成功分配的 WG 拆成 warp 逐个发给目标 CU；收集 CU 回报的「warp 完成」信号，当某 WG 的 warp 全部完成时触发资源释放并上报 host。 |

本讲的两个最小模块之一就是把这四个零件「用电线连起来」的 `cta_scheduler_top` 本身；本节聚焦它的**端口**和**连线**，不展开 allocator/resource_table/cu_interface 的内部（那是 u3-l2、u3-l3 的事）。

#### 4.1.2 核心流程

一个 workgroup 从进入调度器到被拆成 warp 发往 CU，走的是下面这条「主通路」（去程）：

```
host ──host_wg_new──▶ wg_buffer ──alloc_wg_new──▶ allocator ──rt_alloc──▶ resource_table
                          │                           │
                          │                           │  (生成 sgpr/vgpr/lds 基址)
                          │                           │  (经 RTcache 汇报空闲资源)
                          │                           ▼
                          ├──alloc_result(accept)──→ (allocator 内部选定 CU)
                          │
                          └──cuinterface_wg_new──▶ cu_interface ──cu_wf_new──▶ CU(=SM)
                                                     (拆 WG 为 WF)
```

用文字描述去程的每一步：

1. **进场**：host 通过 `host_wg_new` 把一个新 WG 的信息（含资源需求 `num_wf`/`num_sgpr`/`num_vgpr`/`num_lds` 和执行信息 `start_pc`/`pds_base` 等）送进 `wg_buffer`，被缓存起来。
2. **挑选与判定**：`wg_buffer` 以轮询方式挑一个 WG，把它的**资源相关信息**送给 `allocator`；`allocator` 结合 `resource_table` 经 RTcache 汇报的各 CU 空闲资源，判断哪些 CU 放得下，并选一个优先级最高的。
3. **生成基址**：`allocator` 向 `resource_table` 发 `rt_alloc`，为该 WG 在目标 CU 上分配 sGPR/vGPR/LDS 的基址（这些基址后续会从 WG 级换算成 WF 级）。
4. **拆分派发**：若 `allocator` 接受（accept），`wg_buffer` 把该 WG 的**其余执行信息**送给 `cu_interface`；`allocator` 也把分配结果（目标 `cu_id`、`wg_slot_id`、基址等）送给 `cu_interface`。`cu_interface` 据此把 WG 拆成一个个 warp，通过 `cu_wf_new(i)` 逐个发给第 i 个 CU。

回程（warp 完成 → WG 完成 → 通知 host）则是：

```
CU ──cu_wf_done──▶ cu_interface ──┬──(某 WG 的 WF 计数归零)──▶ resource_table(dealloc)
                                  │                            │
                                  │                            └──(WG/WF slot dealloc)
                                  └──host_wg_done──▶ host
```

- 每个 CU 跑完一个 warp，经 `cu_wf_done(i)` 回报给 `cu_interface`；`cu_interface` 把对应 WG 的「剩余 warp 数」减 1。
- 当某 WG 的剩余 warp 数归零，`cu_interface` 通知 `resource_table` 释放该 WG 占用的 LDS/sGPR/vGPR（以及 WG/WF slot），并经 `host_wg_done` 把完成的 `wg_id` 报给 host。

> 注意一个容易混淆的点：**调度器和 host 之间收发的是 WG（workgroup），和 CU 之间收发的是 WF（warp）。** 这两组端口名字里 `wg` 与 `wf` 的区别正是这一转换的体现，而「WG→WF」的拆分动作发生在 `cu_interface`（u3-l3 详讲）。

#### 4.1.3 源码精读

**(a) 顶层端口：四组 DecoupledIO**

[`cta_scheduler.scala:106-115`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cta_scheduler.scala#L106-L115) 定义了 `cta_scheduler_top` 的全部对外端口：

```scala
class cta_scheduler_top(val NUM_CU: Int = CONFIG.GPU.NUM_CU) extends Module {
  val io = IO(new Bundle{
    val host_wg_new = Flipped(DecoupledIO(new io_host2cta))     // 从 host 收新 WG
    val host_wg_done = DecoupledIO(new io_cta2host)             // 向 host 报完成的 WG
    val cu_wf_done = Vec(NUM_CU, Flipped(DecoupledIO(new io_cu2cuinterface))) // 从 CU 收完成的 WF
    val cu_wf_new = Vec(NUM_CU, DecoupledIO(new io_cuinterface2cu))           // 向 CU 发新 WF
  })
```

四组端口的方向（`Flipped` 与否）和「WG/WF」命名直接对应 4.1.2 的流程：

- `host_wg_new`：`Flipped(DecoupledIO(...))`，说明对调度器而言这是**输入**（host 是生产者）。
- `host_wg_done`：`DecoupledIO(...)`，是**输出**（调度器是生产者，向 host 报完成）。
- `cu_wf_done`：`Vec(NUM_CU, Flipped(...))`，每个 CU 一路，输入（CU 报 warp 完成）。
- `cu_wf_new`：`Vec(NUM_CU, DecoupledIO(...))`，每个 CU 一路，输出（调度器派发新 warp）。`NUM_CU` 默认取 `CONFIG.GPU.NUM_CU`（即 `num_sm`，默认 2）。

**(b) 例化四大子模块**

[`cta_scheduler.scala:117-120`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cta_scheduler.scala#L117-L120) 一口气例化了前述四个零件：

```scala
val wg_buffer_inst = Module(new wg_buffer)
val allocator_inst = Module(new allocator)
val resource_table_inst = Module(new resource_table_top)
val cu_interface_inst = Module(new cu_interface)
```

**(c) init_ok 闸门：复位期间不收 WG**

[`cta_scheduler.scala:122-125`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cta_scheduler.scala#L122-L125) 用 `cu_interface` 的 `init_ok` 信号给 `host_wg_new` 加了一道闸门：

```scala
val init_ok = cu_interface_inst.io.init_ok
wg_buffer_inst.io.host_wg_new.valid := io.host_wg_new.valid && init_ok
io.host_wg_new.ready := wg_buffer_inst.io.host_wg_new.ready && init_ok
```

`init_ok` 来自 `cu_interface` 内部对其 `wf_gather` 计数器的初始化完成标志（见 [`cu_interface.scala:168`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cu_interface.scala#L168) `io.init_ok := (init_wf_gather_cnt_idx === (NUM_CU * NUM_WG_SLOT).U)`）。也就是说，**在 `cu_interface` 把自己的内部状态初始化好之前，调度器不会接受任何 host 送来的 WG**——这是为了避免复位早期把 WG 派发到尚未就绪的状态机里。

**(d) 子模块之间的连线**

去程主通路（[`cta_scheduler.scala:127-130`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cta_scheduler.scala#L127-L130)）：

```scala
wg_buffer_inst.io.alloc_wg_new      <> allocator_inst.io.wgbuffer_wg_new   // buffer → allocator：待判定的 WG
allocator_inst.io.wgbuffer_result   <> wg_buffer_inst.io.alloc_result     // allocator → buffer：accept/reject 结果
wg_buffer_inst.io.cuinterface_wg_new <> cu_interface_inst.io.wgbuffer_wg_new  // buffer → cu_interface：WG 执行信息
allocator_inst.io.cuinterface_wg_new <> cu_interface_inst.io.alloc_wg_new     // allocator → cu_interface：分配结果
```

资源通路（[`cta_scheduler.scala:133-139`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cta_scheduler.scala#L133-L139)）：

```scala
allocator_inst.io.rt_alloc        <> resource_table_inst.io.alloc          // allocator 申请分配
allocator_inst.io.rt_result_lds   <> resource_table_inst.io.rtcache_lds    // 三类资源的「空闲片段」缓存
allocator_inst.io.rt_result_sgpr  <> resource_table_inst.io.rtcache_sgpr
allocator_inst.io.rt_result_vgpr  <> resource_table_inst.io.rtcache_vgpr
resource_table_inst.io.dealloc    <> cu_interface_inst.io.rt_dealloc        // cu_interface 触发释放
resource_table_inst.io.cuinterface_wg_new <> cu_interface_inst.io.rt_wg_new
allocator_inst.io.rt_dealloc      <> resource_table_inst.io.slot_dealloc    // WG/WF slot 的释放
```

CU 侧收发与 host 完成回报（[`cta_scheduler.scala:131`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cta_scheduler.scala#L131) 与 [`cta_scheduler.scala:141-144`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cta_scheduler.scala#L141-L144)）：

```scala
cu_interface_inst.io.host_wg_done <> io.host_wg_done
...
for(i <- 0 until NUM_CU) {
  io.cu_wf_new(i)  <> cu_interface_inst.io.cu_wf_new(i)
  io.cu_wf_done(i) <> cu_interface_inst.io.cu_wf_done(i)
}
```

> 读线技巧：调度器里端口名采用「**生产者侧_消费者侧**」或「**本模块视角的语义**」命名。例如 `wgbuffer_wg_new` 表示「wg_buffer 发往对面的新 WG」，`alloc_result` 表示「allocator 给出的判定结果」。配合 `<>`（双向对接）和 `Flipped`（方向取反）就能判断数据流向。

**(e) 数据包长什么样：关键 bundle**

调度器在不同子模块之间传递的「数据包」是用 trait 组合拼出来的（[`cta_scheduler.scala:14-104`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cta_scheduler.scala#L14-L104)）。按「谁能看/用来干什么」分成几组，这是理解端口语义的钥匙：

| bundle / trait | 谁生产 → 谁消费 | 关键字段 | 用途 |
|----------------|------------------|----------|------|
| `ctainfo_host_to_alloc` | host → allocator | `num_wf`、`num_sgpr`、`num_vgpr`、`num_lds` | 判断 CU 能否容纳（资源量） |
| `ctainfo_host_to_cuinterface` | host → cu_interface | `num_sgpr_per_wf`、`num_vgpr_per_wf`、`num_pds_per_wf` | 把 WG 拆成 WF 时换算每 WF 资源 |
| `ctainfo_alloc_to_cuinterface` | allocator → cu_interface | `cu_id`、`wg_slot_id`、`num_wf`、`*_dealloc_en` | 告诉 cu_interface 派发目标与释放使能 |
| `ctainfo_alloc_to_cu` | allocator → CU | `sgpr_base`、`vgpr_base`、`lds_base` | 资源基址（初始为 WG 级，cu_interface 内换成 WF 级） |
| `ctainfo_host_to_cu` | host → CU | `num_thread_per_wf`、`start_pc`、`pds_base`、`csr_kernel`、`num_wg_x/y/z`、`gds_base` | 与代码执行相关的信息 |
| `io_cuinterface2cu` | cu_interface → CU | 上面两组 + `wg_id` + `wf_tag` + `num_wf` | 派发单个 warp 时携带的完整信息 |
| `io_cu2cuinterface` | CU → cu_interface | `wf_tag` | warp 完成回报（用 tag 标识是哪个 warp） |

注意 `ctainfo_alloc_to_cu` 的注释特别强调：`sgpr_base`/`vgpr_base`/`lds_base` 一开始是 **WG 级**的基址，`cu_interface` 会把它换算成 **WF 级**的基址再发给 CU（[`cta_scheduler.scala:57-61`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cta_scheduler.scala#L57-L61)）。这正是「WG→WF 拆分」要做的关键换算之一。

**(f) 调度器在顶层怎么被包起来**

在 `GPGPU_top.scala` 里，`CTAinterface` 例化了 `cta_scheduler_top`，并把 host 视角的 `host2CTA_data` 逐字段映射到 `host_wg_new.bits`（[`GPGPU_top.scala:61-82`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L61-L82)），例如：

```scala
cta_sche.io.host_wg_new.bits.wg_id             := io.host2CTA.bits.host_wg_id
cta_sche.io.host_wg_new.bits.num_wf            := io.host2CTA.bits.host_num_wf
cta_sche.io.host_wg_new.bits.num_thread_per_wf := io.host2CTA.bits.host_wf_size
cta_sche.io.host_wg_new.bits.start_pc          := io.host2CTA.bits.host_start_pc
cta_sche.io.host_wg_new.bits.num_lds           := io.host2CTA.bits.host_lds_size_total
...
```

而调度器派发出的 `cu_wf_new(i)` 被转接成 `CTA2warp` 能接收的 `CTAreqData`（[`GPGPU_top.scala:88-94`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L88-L94)），再由 `CTA2warp` 分配硬件 warp id 并送入 SM 流水线（[`GPGPU_top.scala:351-362`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L351-L362)）。这就是「调度器 → SM」的完整接力。

#### 4.1.4 代码实践

**实践目标**：在不开仿真、不重新编译的前提下，仅靠阅读源码与文档，把 `cta_scheduler_top` 的内部连接图画出来，并验证你对「数据流向」的理解与文档一致。

**操作步骤**：

1. 打开 [`ventus/src/cta/cta_scheduler.scala`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/cta_scheduler.scala)，定位 4 个 `Module(new ...)` 例化点（第 117–120 行）。
2. 准备一张白纸（或绘图工具），画出 4 个方框：`wg_buffer`、`allocator`、`resource_table`、`cu_interface`，再加 host 与 CU 两个外部方框。
3. 逐行读第 127–144 行的 `<>` 连线，每读一行就在图上画一根箭头，并标注端口名（如 `alloc_wg_new`）和方向。**判断方向的办法**：去 `wg_buffer.scala`、`allocator.scala` 等文件里看该端口是 `DecoupledIO(...)`（输出）还是 `Flipped(DecoupledIO(...))`（输入）。
4. 对照 `docs/cta_scheduler/Top.md` 的「top_general」框图，检查你画的箭头方向和模块顺序是否与官方文档一致。

**需要观察的现象**：

- `wg_buffer` 与 `allocator` 之间有**两根**反向的箭头（`alloc_wg_new` 去程、`alloc_result` 回程）——这是一个典型的「请求—判定」往返。
- `wg_buffer` 与 `cu_interface` 之间、`allocator` 与 `cu_interface` 之间**各有一根**去程箭头（一个送执行信息、一个送分配结果），二者在 `cu_interface` 汇合后才能完成 WG→WF 的拆分。
- `resource_table` 同时和 `allocator`（分配/查空闲）、`cu_interface`（释放）相连，是资源账本的单点。

**预期结果**：你得到的图应当与 `docs/cta_scheduler/Top.md` 中描述的四大组件主通路一致——`WG buffer → Allocator → CU interface`，`Resource table` 横向服务于 `Allocator` 与 `CU interface`。若发现某根箭头方向画反，回到源码核对该端口的 `Flipped` 与否即可纠正。

> 本实践为「源码阅读型」，不需要运行命令，结果可在纸上或绘图工具中产出。

#### 4.1.5 小练习与答案

**练习 1**：调度器的 `cu_wf_new` 是 `Vec(NUM_CU, DecoupledIO(...))`。如果要把 GPU 从 2 个 SM 扩展到 4 个 SM，这个向量的宽度由谁决定？需要改 `cta_scheduler.scala` 吗？

**参考答案**：宽度由 `NUM_CU` 决定，而 `NUM_CU` 默认取 `CONFIG.GPU.NUM_CU`，后者等于 `num_sm`（见 [`parameters.scala:167`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L167)）。所以只要把 `num_sm` 从 2 改成 4，`cta_scheduler_top` 里 `for(i <- 0 until NUM_CU)` 的循环次数和向量宽度会自动跟着变，**不需要**改 `cta_scheduler.scala` 本身。

**练习 2**：`init_ok` 闸门把 `host_wg_new.valid` 和 `host_wg_new.ready` 都「与」上了 `init_ok`。如果只门控 `valid` 不门控 `ready`，会有什么隐患？

**参考答案**：若只门控 `valid` 而 `ready` 仍可能为真，那么在 `init_ok` 为假期间，host 看到 `ready=1` 就可能发起握手（`fire`），但调度器侧 `valid` 被强制拉低，会导致双方对「是否成交」的判断不一致——host 以为送出了一个 WG，调度器却没收到。同时门控 `valid` 和 `ready` 才能保证：`init_ok=0` 时双方都不会出现 `fire`，WG 被稳妥地挡在门外直到初始化完成。

---

### 4.2 wg_buffer：工作组缓冲与轮询调度

#### 4.2.1 概念说明

`wg_buffer` 是调度器的「入口仓库」。它解决三个现实问题：

1. **解耦 host 与 allocator 的时序**：host 可能在 allocator 忙于上一个 WG 时就送来新 WG，需要一个 FIFO 式的缓冲把它们暂存起来。
2. **给 allocator 喂「够用的」信息**：allocator 判定一个 WG 能否被容纳，只需要资源相关字段（`num_wf`/`num_sgpr`/`num_vgpr`/`num_lds`）；而真正派发给 CU 还需要执行信息（`start_pc`/`pds_base` 等）。`wg_buffer` 把同一份 WG 信息拆成两半分别存放，**只在需要时才读出对应那一半**，节省了读端口带宽。
3. **处理被拒绝的 WG**：如果 allocator 发现当前所有 CU 都放不下某个 WG（reject），这个 WG 不能被丢弃，要留着下一轮再试。`wg_buffer` 用一个标志位记录「这个 WG 是否正在被 allocator 处理」，被拒后清掉这个标志，允许它重新参与挑选。

为此 `wg_buffer` 内部用了 **两块异步读 RAM**（`wgram1`/`wgram2`）加 **两个一位宽的位向量寄存器**（`wgram_valid`/`wgram_alloc`）来实现上述功能。这是本讲的第二个最小模块。

#### 4.2.2 核心流程

`wg_buffer` 把每个时钟周期分成三个**相互独立**的动作（独立性靠 `valid`/`alloc` 锁保证，下面 4.2.3 详述）：

```
┌───────────────────────────── 每个时钟周期并行执行 ─────────────────────────────┐
│                                                                               │
│  ① 写入新 WG（function 1）          来源：host_wg_new                         │
│     在 wgram_valid 找一个空位 → 写 wgram1+wgram2 → 置 valid                   │
│                                                                               │
│  ② 读 WG 给 allocator（function 2） 来源：wgram1（valid && !alloc 的项）       │
│     轮询挑一个 → 读 wgram1 → 准备发给 allocator → 置 alloc                    │
│                                                                               │
│  ③ 处理 allocator 判定（function 3）来源：alloc_result                        │
│     accept：读 wgram2 → 发给 cu_interface → 清 valid（WG 出仓）               │
│     reject：清 alloc（允许此 WG 下轮再被挑）                                   │
└───────────────────────────────────────────────────────────────────────────────┘
```

挑选地址用的是**轮询优先级编码器 `RRPriorityEncoder`**（Round-Robin）：每次不是固定从第 0 项开始找，而是从「上一次选中的下一项」开始循环找，从而让所有 WG 都有公平的机会被服务，避免低编号项「饿死」高编号项。

`wgram_valid` 与 `wgram_alloc` 两位向量的状态机可以这样理解（针对某个地址 `addr`）：

| 状态（valid, alloc） | 含义 | 进入条件 | 离开条件 |
|----------------------|------|----------|----------|
| `(0, 0)` | 空槽，可写入 | 复位 / WG 被 accept 出仓 | host 写入新 WG → `(1, 0)` |
| `(1, 0)` | 已缓存，等待送给 allocator | host 刚写入 | function 2 挑中并读出 → `(1, 1)` |
| `(1, 1)` | 正在被 allocator 判定中 | function 2 置 alloc | accept → 清 valid 成 `(0,0)`；reject → 清 alloc 成 `(1,0)` |

> 关键不变量：`alloc=1` 必然蕴含 `valid=1`（一个 WG 必须先在仓里才能被送去判定）。function 2 只挑 `valid && !alloc` 的项，所以**同一个 WG 不会同时存在多份副本**被 allocator 重复 accept——这正是 `wgram_alloc` 存在的意义（见 [`wg_buffer.md`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/docs/cta_scheduler/wg_buffer.md) 的说明）。

#### 4.2.3 源码精读

**(a) 模块端口与双口 RAM**

[`wg_buffer.scala:35-58`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/wg_buffer.scala#L35-L58) 定义了端口和存储结构：

```scala
class wg_buffer(NUM_ENTRIES: Int = CONFIG.WG_BUFFER.NUM_ENTRIES) extends Module {
  val io = IO(new Bundle{
    val host_wg_new       = Flipped(DecoupledIO(new io_host2cta))             // 从 host 收新 WG
    val alloc_wg_new      = DecoupledIO(new io_buffer2alloc(NUM_ENTRIES))     // 送 WG 给 allocator
    val alloc_result      = Flipped(DecoupledIO(new io_alloc2buffer()))       // 收 allocator 判定
    val cuinterface_wg_new = DecoupledIO(new io_buffer2cuinterface)           // 送执行信息给 cu_interface
  })
  ...
  val wgram1 = Mem(NUM_ENTRIES, new ram1datatype)           // allocator 用：资源相关字段（异步读）
  val wgram2 = Mem(NUM_ENTRIES, new io_buffer2cuinterface)  // cu_interface 用：执行相关字段（异步读）
  val wgram_valid = RegInit(Bits(NUM_ENTRIES.W), 0.U)       // 每项是否有效
  val wgram_alloc = RegInit(Bits(NUM_ENTRIES.W), 0.U)       // 每项是否在 allocator 处理中
```

`NUM_ENTRIES` 默认取 `CONFIG.WG_BUFFER.NUM_ENTRIES`，在 [`parameters.scala:190-192`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L190-L192) 里定义为 **8**：

```scala
object WG_BUFFER {
  val NUM_ENTRIES = 8
}
```

也就是说 `wg_buffer` 最多同时缓存 8 个 WG。`wgram1` 存 allocator 判定要用的资源字段（`ctainfo_host_to_alloc` + 调试态下的 `wg_id`），`wgram2` 存 cu_interface/CU 要用的执行字段。两块 RAM 的注释明确写明是「combinational/asynchronous-read memory」（组合/异步读，0 周期读延时）。

**(b) 用 RRPriorityEncoder 挑地址**

[`wg_buffer.scala:72-73`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/wg_buffer.scala#L72-L73) 用两个轮询编码器分别找「下一个可写空位」和「下一个可读项」：

```scala
val wgram_wr_next  = RRPriorityEncoder(~wgram_valid)                // 找空位（valid 取反后为 1 的位）
val wgram1_rd_next = RRPriorityEncoder(wgram_valid & ~wgram_alloc)  // 找 valid 且未被 alloc 的项
```

`RRPriorityEncoder` 的实现在 [`utils.scala:15-36`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/utils.scala#L15-L36)。核心思想是：用一个 `last` 寄存器记住「上一次选中谁」，本轮把输入向量循环右移 `shift = last + 1` 位，再对移位后的向量做普通 `PriorityEncoder`，最后把结果加回 `shift` 还原：

```scala
val shift = last + 1.U
in_RR := (in >> shift) | (in << (n.U - shift))          // 循环右移 shift 位
io.out.bits := Mux(in.orR, PriorityEncoder(in_RR) + shift, DontCare)
when(io.out.fire) { last := PriorityEncoder(in_RR) + shift }   // 选中后更新起点
```

直观地说，它就像一个「轮流值班表」：每次从上次值班的人之后开始找下一位有空的人，找到就让他值班，并把「下次起点」设为他。

**(c) function 1：写入新 WG**

[`wg_buffer.scala:85-93`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/wg_buffer.scala#L85-L93) 实现写入：

```scala
io.host_wg_new.ready := wgram_wr_next.valid               // 有空位才接收
wgram_wr_next.ready  := io.host_wg_new.valid              // host 有数据才消费空位
wgram_wr_act := wgram_wr_next.fire                        // 双方都就绪 → 写入生效
when(wgram_wr_act){
  wgram1.write(wgram_wr_next.bits, wgram1_wr_data)
  wgram2.write(wgram_wr_next.bits, wgram2_wr_data)
}
```

注意这里把 host 的 `valid/ready` 和编码器的 `valid/ready` 做了「交叉」连接：`host.ready := wr_next.valid`、`wr_next.ready := host.valid`。其语义是「**有新 WG 进场 ⇔ 找到了一个空位**」——只有双方同时为真（`fire`）时才真正写入 `wgram1`/`wgram2`，并在稍后（function 的 valid 更新里）把对应位置为有效。

**(d) function 2：读 WG 给 allocator**

[`wg_buffer.scala:100-117`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/wg_buffer.scala#L100-L117) 负责把一个候选 WG 的资源信息从 `wgram1` 读出、送上 `alloc_wg_new`：

```scala
val alloc_wg_new_valid_r = RegInit(false.B)
io.alloc_wg_new.valid := alloc_wg_new_valid_r
alloc_wg_new_valid_r := Mux(wgram1_rd_act, true.B,
                        Mux(io.alloc_wg_new.ready, false.B, alloc_wg_new_valid_r))
...
wgram1_rd_next.ready := (!alloc_wg_new_valid_r || io.alloc_wg_new.ready)   // 输出口空闲才允许读
wgram1_rd_act := wgram1_rd_next.fire                                      // 读生效
val wgram1_rd_data = RegEnable(wgram1.read(wgram1_rd_next.bits), wgram1_rd_act)
```

读出后顺手把 `wgram_alloc` 对应位置 1（在 4.2.3 (f) 的 alloc 更新里），表示「这个 WG 已经送给 allocator 了，别再重复送」。读出的数据用 `RegEnable` 打一拍寄存，所以 `alloc_wg_new.valid` 是寄存器输出（文档强调 valid/bits 均为 reg 输出，ready 可为组合逻辑）。

**(e) function 3：处理 accept / reject**

[`wg_buffer.scala:125-157`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/wg_buffer.scala#L125-L157) 处理 allocator 回送的 `alloc_result`，分两种情况：

- **accept（被接受）**：从 `wgram2` 读出该 WG 的执行信息，送上 `cuinterface_wg_new`，并清掉 `wgram_valid` 对应位（WG 出仓）。
- **reject（被拒绝）**：清掉 `wgram_alloc` 对应位，让该 WG 下一轮能重新被 function 2 挑中。

```scala
// accept：读 wgram2 → 发给 cu_interface，清 valid
wgram_rd2_clear_act := (io.alloc_result.fire && io.alloc_result.bits.accept) && wgram_rd2_clear_ready
...
// reject：清 alloc，允许重试
val wgram_alloc_clear_act = io.alloc_result.fire && !io.alloc_result.bits.accept && wgram_alloc_clear_ready
...
io.alloc_result.ready := Mux(io.alloc_result.bits.accept, wgram_rd2_clear_ready, wgram_alloc_clear_ready)
```

`alloc_result.bits.wgram_addr` 是 allocator 回送时附带的「当初这个 WG 存在哪个地址」，buffer 用它定位要读/清的 RAM 项。`accept` 标志位定义在 [`wg_buffer.scala:9-13`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/wg_buffer.scala#L9-L13) 的 `io_alloc2buffer` 里。

**(f) 两位向量的维护与互斥性检查**

`wgram_valid` 的置位/复位（[`wg_buffer.scala:163-168`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/wg_buffer.scala#L163-L168)）：

```scala
val wgram_valid_setmask = wgram_wr_act << wgram_wr_next.bits        // 写入位置 1
val wgram_valid_rstmask = wgram_rd2_clear_act << wgram_rd2_clear_addr  // accept 出仓位 1（清零用）
assert((wgram_valid_setmask & wgram_valid_rstmask).orR === false.B) // 同周期不能对同一位既写又清
wgram_valid := wgram_valid & ~wgram_valid_rstmask | wgram_valid_setmask
```

`wgram_alloc` 的维护（[`wg_buffer.scala:170-176`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/wg_buffer.scala#L170-L176)）逻辑类似：function 2 读出时置位，写入新 WG（复用旧项前）或 reject 时清位，同样带互斥断言。

这两处 `assert` 是「相互排斥性检查」——它们保证了**三个功能在同一周期访问的必然是不同的 RAM 项**，从而三个功能可以并行运行而互不干扰。这正是文档里「三个功能的运行相互独立」那句话的源码依据。

> 关于 `useless` 信号：代码里有一处 `wgram2_wr_data.useless := 0.U` 和带中文注释的 `dontTouch`（[`wg_buffer.scala:66-69`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/cta/wg_buffer.scala#L66-L69)）。这是为了规避 Verilator 仿真时 `wgram2` 写端口第 238 bit 总被置 1 的一个已知 bug 而插入的占位信号，属于实现层面的 workaround，不影响功能理解。

#### 4.2.4 代码实践

**实践目标**：以一个具体 WG 为例，手工推演它在 `wg_buffer` 内部经历的状态转移，验证 `wgram_valid`/`wgram_alloc` 两位向量的行为符合 4.2.2 的状态表。

**操作步骤**：

1. 假设 `wg_buffer` 复位后 `wgram_valid = 00000000`、`wgram_alloc = 00000000`（8 个表项）。
2. 推演下面这个事件序列，逐拍填写两个位向量的值（用 8 位二进制表示，bit 0 在右）：
   - **T1**：host 送来 `WG_A`，`host_wg_new` 握手成功（`RRPriorityEncoder` 从 `last` 起点找到第一个空位，假设是 bit 0）。
   - **T2**：function 2 挑中 `WG_A`，读 `wgram1` 送给 allocator，置 alloc。
   - **T3**：allocator 回送 `alloc_result` 且 `accept=false`（所有 CU 都放不下），reject。
   - **T4**：function 2 再次挑中 `WG_A`，重新置 alloc。
   - **T5**：allocator 这次 `accept=true`，function 3 读 `wgram2` 发给 cu_interface 并清 valid。
3. 在每一步旁标注此时该 WG 处于状态表里的哪个状态 `(valid, alloc)`。

**需要观察的现象**：

- T1 之后 bit 0 的 `valid` 变 1，`alloc` 仍为 0 → 状态 `(1,0)`。
- T2 之后 bit 0 的 `alloc` 变 1 → 状态 `(1,1)`。
- T3 reject 后 `alloc` 清回 0，`valid` 仍是 1 → 退回 `(1,0)`，证明被拒的 WG 没有丢失。
- T5 accept 后 `valid` 清 0 → 状态 `(0,0)`，WG 出仓。

**预期结果**：两个位向量的取值序列应为（仅看 bit 0）：

| 时刻 | 事件 | valid | alloc | 状态 |
|------|------|-------|-------|------|
| T1 | host 写入 WG_A | 1 | 0 | (1,0) |
| T2 | 送给 allocator | 1 | 1 | (1,1) |
| T3 | reject | 1 | 0 | (1,0) |
| T4 | 重试送给 allocator | 1 | 1 | (1,1) |
| T5 | accept 出仓 | 0 | 0 | (0,0) |

> 本实践为「源码阅读 + 状态推演型」，无需运行命令。若想用波形验证，可在 sim-verilator 仿真中对 `wg_buffer` 实例的 `wgram_valid`/`wgram_alloc` 寄存器抓波形观察（具体能否抓到内部寄存器取决于仿真可见性，**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `wgram1_rd_next` 挑选的条件是 `wgram_valid & ~wgram_alloc`，而不是只用 `wgram_valid`？

**参考答案**：如果只用 `wgram_valid`，那么一个已经送给 allocator、正在等待判定结果（`alloc=1`）的 WG 可能被再次挑中并送给 allocator，于是同一个 WG 在 allocator 里会出现多份副本，可能被 accept 多次、派发多份 warp，造成资源重复占用和错误。加上 `~wgram_alloc` 条件，确保「正在处理中的 WG 不会被重复送出」。文档把这一点列为 `wgram_alloc` 存在的核心理由。

**练习 2**：`wg_buffer` 用了两块独立的 RAM（`wgram1`、`wgram2`）而不是一块大 RAM，这样做的好处是什么？

**参考答案**：因为 allocator 判定只需资源字段，cu_interface 派发只需执行字段，二者的消费者不同、读取时机也不同。分两块 RAM 可以让 function 2 只读 `wgram1`（资源字段）反复送给 allocator 判定，而不必每次都读出冗长的执行字段；只有当 accept 后才读一次 `wgram2`。这样降低了读端口的数据宽度和翻转活跃度，也让两块 RAM 的读端口可以独立时序。代价是同一份 WG 信息要分别写入两块 RAM（function 1 里 `wgram1.write` 和 `wgram2.write` 同时发生）。

**练习 3**：`RRPriorityEncoder` 相比固定从 bit 0 开始的 `PriorityEncoder`，解决了什么问题？

**参考答案**：固定优先级编码器总是优先选编号最小的有效项，当低编号项长期有效时，高编号项可能一直得不到服务（「饿死」）。轮询优先级编码器通过记住 `last`、每次从 `last+1` 起循环找，让所有有效项被轮流选中，保证了公平性。对 `wg_buffer` 而言，这意味着缓存里的各个 WG 都有均等机会被送给 allocator，而不是固定优先服务低地址项。

---

## 5. 综合实践

把 4.1 和 4.2 串起来，完成下面这个贯穿全讲的任务（即本讲的指定实践任务）。

**任务**：对照 `docs/cta_scheduler/Top.md` 与 `docs/cta_scheduler/wg_buffer.md`，画出 **`cta_scheduler_top` 的内部模块连接图**，并在图上标注 **一个 workgroup 从进入调度器到被拆分为 warp 发往 CU 的完整路径**。

**要求你的图至少包含**：

1. `host`、`wg_buffer`、`allocator`、`resource_table`、`cu_interface`、`CU(=SM)` 六个方框。
2. 用带箭头的连线标出下列端口，并写出方向（可参考 4.1.3 的连线清单）：
   - `host_wg_new`（host → wg_buffer）
   - `alloc_wg_new`（wg_buffer → allocator）
   - `alloc_result`（allocator → wg_buffer，标注 accept/reject 两条分支）
   - `cuinterface_wg_new`（wg_buffer → cu_interface）
   - `alloc_wg_new`/`rt_alloc`（allocator → cu_interface / resource_table）
   - `rtcache_lds/sgpr/vgpr`（resource_table → allocator）
   - `cu_wf_new`（cu_interface → CU）
   - `cu_wf_done`（CU → cu_interface）
   - `rt_dealloc`（cu_interface → resource_table）
   - `host_wg_done`（cu_interface → host）
3. 在图上用**不同颜色或粗细**标出「去程主通路」（host → … → cu_wf_new → CU）和「回程完成通路」（CU → cu_wf_done → … → host_wg_done → host）。
4. 在 `wg_buffer` 方框内部，再画一个**小 inset**，标出 `wgram1`/`wgram2`/`wgram_valid`/`wgram_alloc` 以及 `RRPriorityEncoder` 的位置，并用箭头表示 function 1/2/3 各自访问哪块存储。

**操作步骤**：

1. 先按 4.1.4 的方法画出四大组件骨架与连线（仅端口名、方向）。
2. 再按 4.2 的内容把 `wg_buffer` 内部结构补成 inset。
3. 最后用彩笔描出去程与回程两条主通路，并在旁边用一段文字（3–5 句）描述「WG_A 从 host 进入后，依次经过哪些端口、被谁判定、在哪被拆成 WF、最终如何回到 host」。

**预期结果**：你会得到一张既包含宏观四大组件连接、又包含 `wg_buffer` 微观结构的「双层」示意图。它应当能回答这些问题——WG 在哪里被缓存？谁判断它能否被容纳？被接受后执行信息从哪块 RAM 读出？WG→WF 的拆分发生在哪个模块？warp 完成信号经谁汇总、何时触发资源释放与 host 回报？这张图也是后续 u3-l2（allocator/resource_table）和 u3-l3（cu_interface/CTA2warp）的学习导航。

> 本实践为「源码阅读 + 文档对照型」，产出为示意图与文字说明，无需运行仿真。

## 6. 本讲小结

- **CTA 调度器**夹在 host 与各 SM 之间，解决「把 WG 分配到资源足够的 SM、再把 WG 拆成 warp 喂给 SM、最后回收资源并回报 host」三件事。
- `cta_scheduler_top` 例化 **`wg_buffer`/`allocator`/`resource_table`/`cu_interface`** 四大组件，靠一组 `DecoupledIO` 连成主通路，并对外暴露 `host_wg_new`/`host_wg_done`（WG 粒度）与 `cu_wf_new`/`cu_wf_done`（WF 粒度，每 CU 一路）四组端口。
- **「WG↔host、WF↔CU」的粒度转换发生在 `cu_interface`**；调度器对 host 收发 WG、对 CU 收发 WF，命名上的 `wg`/`wf` 之别正对应这一点。
- `init_ok` 闸门保证 `cu_interface` 完成内部初始化前，调度器不会接收任何 host WG。
- **`wg_buffer`** 用双口 RAM `wgram1`/`wgram2` 分别存「资源字段」和「执行字段」，配 `wgram_valid`/`wgram_alloc` 两位向量管理状态；每周期并行执行「写新 WG、读 WG 给 allocator、处理 accept/reject」三个动作，靠 valid/alloc 锁与互斥断言保证三者访问不同表项。
- 地址挑选用 **`RRPriorityEncoder`**（轮询优先级编码器），保证缓存的各 WG 被公平轮转服务，被 reject 的 WG 清掉 alloc 位后可下轮重试而不丢失。

## 7. 下一步学习建议

本讲把调度器当作「黑盒 + wg_buffer」来看。接下来：

- **u3-l2（资源表与分配器）**：打开 `wg_buffer` 的下游——`allocator` 如何用 FSM（IDLE/CU_PREFER/RESOURCE_CHECK/ALLOC/REJECT）选 CU、`resource_table` 如何用链表管理 LDS/sGPR/vGPR 的基址与余量、`RTcache` 如何加速余量查询。建议先重读本讲 4.1.3 (d) 的资源通路连线，带着「`rt_alloc`/`rtcache_*` 这些线上跑的是什么」去读 u3-l2。
- **u3-l3（CU 接口、warp 拆分与释放）**：打开 `cu_interface`——它如何把一个 WG 逐个 warp 派发、用 `wf_tag` 标识每个 warp、收齐完成信号后触发释放，以及 SM 侧 `CTA2warp` 如何接收并分配硬件 warp id。建议结合本讲 4.1.3 (b) 里 `io_cuinterface2cu` 的 `wf_tag` 字段一起读。
- 继续阅读 `docs/cta_scheduler/` 下的 `Allocator.md`、`Resouce table.md`（注意原文拼写）以及 `cu_interface` 相关文档，作为后续两讲的预习材料。
