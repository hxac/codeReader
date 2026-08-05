# Warp 调度器、CTA 派发与屏障

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清一个 **warp** 在 SimX 里持有哪些状态（`PC`、`tmask`、`ipdom_stack`、CTA CSR 快照），以及调度器如何用 `active_warps_` / `stalled_warps_` 这几套位掩码管理它的生命周期。
- 跟踪一个 **CTA（协作线程阵列 / 线程块）** 从 KMU 产出、经 `CtaDispatcher` 被拆成多个 warp rank、再到 `activate_warp` 写入 warp 状态的完整派发过程，理解 block/grid 维度与线程掩码是如何算出来的。
- 解释 **屏障（barrier）** 的到达（arrive）、等待（wait）、释放（release）三段语义，以及为何用 `phase`（代际）来区分新旧一轮屏障。
- 用 **IPDOM 栈** 解释一次分支发散后，warp 内两条分支是如何被串行执行、最终在汇聚点重新合并的。

本讲是 SimX 核心流水线的第一站：它讲的是「流水线最前端——谁在本周期被发射」。后续 u6-l2（取指译码）、u6-l3（发射记分板）都建立在本讲描述的 warp 状态机之上。

## 2. 前置知识

在进入源码前，先建立三组直觉。这些概念在 u4-l2（SIMT 控制指令）和 u5-l1（SimObject 三基元）中已有铺垫，这里只做承接。

**（1）SIMT 的两层激活状态。** Vortex 采用 SIMT（单指令多线程）模型。一条指令在**一个 warp** 上发射，warp 内的所有线程共享同一个 `PC`，但每个线程是否真正执行、是否写回，由一个**线程掩码 `tmask`** 控制。所以"warp 级"管 PC，"线程级"管 tmask——这是后续所有调度逻辑的根本出发点。

**（2）CTA / block / grid / cluster 的层次。** 主机用 `vx_start` 启动一个 kernel 时给出 `grid_dim`、`block_dim`、`cluster_dim`：

- 一个 **block（CTA）** 由若干线程组成，是协作的基本单位（可共享本地内存、可做屏障同步）。
- 一个 **grid** 是所有 block 的集合，KMU 会遍历整个 grid，逐个产出 CTA。
- 一个 **warp** 是一个 block 被切成的大小为 `NUM_THREADS` 的执行碎片：`cta_size = ceil(block_size / NUM_THREADS)` 个 warp。
- 一个 **cluster** 是 K 个相邻 CTA 的编组，它们被放进连续的本地内存槽位，便于 DXA 多播（见 u9-l2）。本讲你只需要知道 cluster 影响"槽位分配"，细节由设计文档 `cta_clustering_and_dispatch.md` 承载。

**（3）SimObject 的 tick 模型。** SimX 里每个模块都是一个 `SimObject`，平台每周期调用它的 `do_tick()`。`Scheduler` 把 `CtaDispatcher` 和 `BarrierUnit` 作为**子 SimObject** 创建并登记到平台（见构造函数），所以它们各自有独立的 reset/tick 钩子，但逻辑上仍由 Scheduler 拥有——这正是设计文档强调的"Scheduler 拥有 warp 生命周期，CTA dispatcher 是它的孩子"。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [sim/simx/scheduler.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/scheduler.h) | 定义 `warp_t`（warp 全部状态）、`ipdom_entry_t`（发散栈条目）、`cta_csrs_t`（CTA CSR 快照）以及 `Scheduler` 类接口 |
| [sim/simx/scheduler.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/scheduler.cpp) | 本讲主角之一：warp 状态机、周期级 `schedule()`、`activate_warp`、`suspend/resume`、`setTmask`、trap 处理 |
| [sim/simx/cta_dispatcher.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/cta_dispatcher.h) | 定义 `cta_warp_record_t`（派发给单个 warp 的记录）和 `CtaDispatcher` 类 |
| [sim/simx/cta_dispatcher.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/cta_dispatcher.cpp) | 本讲主角之二：从 KMU 取 CTA、用固定步长槽位接纳、逐 warp rank 切分 |
| [sim/simx/barrier_unit.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/barrier_unit.cpp) | 本讲主角之三：本地/全局屏障的到达、等待、代际释放、异步事件计数 |
| [sim/simx/types.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/types.h) | `warp_barrier_t` 屏障状态结构 |
| [sim/simx/wctl_unit.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/wctl_unit.cpp) | SPLIT/JOIN/BAR 等 warp 控制指令的执行——IPDOM 栈的 push/pop 与屏障的实际触发都落在这里 |
| [sim/simx/kmu/kmu.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.h) | `kmu_req_t`（一个 CTA 的载荷）定义，是 CtaDispatcher 的输入 |
| [docs/designs/cta_clustering_and_dispatch.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/cta_clustering_and_dispatch.md) | CTA 派发与 cluster 聚类的设计文档，SimX 与 RTL 对齐的权威说明 |

## 4. 核心概念与源码讲解

### 4.1 Warp 的状态机与周期级调度（scheduler.cpp）

#### 4.1.1 概念说明

调度器要回答的核心问题是：**"本周期的取指槽，发射哪个 warp 的哪条指令？"** 要回答它，调度器必须知道每个 warp 当前的状态——是否活着、是否在等待、PC 在哪、哪些线程活跃。这就是 `warp_t` 的职责。

一个关键的设计取舍：**寄存器堆不在 `warp_t` 里**。整数/浮点寄存器堆放在 `OpcUnit`（操作数收集单元，见 u6-l3），`warp_t` 只持有"控制流"层面的状态。这让 `warp_t` 保持精简，也对应 RTL 里调度器只管控制状态、数据通路在别处的设计。

#### 4.1.2 核心流程

Scheduler 用**三套位掩码**（`WarpMask`，每一位对应一个 warp）管理 warp 的就绪状态：

```
active_warps_        : 这个 warp 是否在运行（被 CTA 派发或 wspawn 激活）
stalled_warps_       : 本周期 schedule() 实际读取的"暂停"状态（registered / 当前态）
stalled_warps_next_  : suspend()/resume() 写入的"下一拍"状态（next 态）
```

一个 warp 能在本周期被选中，必须同时满足三件事：**active、未 stalled、ibuf 未满**（第三个由调用方通过 `warp_mask` 传入）。`stalled_warps_` 与 `stalled_warps_next_` 的"双拍"设计是理解调度时序的钥匙——下面 4.1.3 会精读。

整个每周期调度流程（`schedule()`）：

```
1. cta_dispatcher_->step()          // 试着派发一个新 CTA warp（见 4.2）
2. 处理挂起的 wspawn（当只剩 1 个 active warp 时拉起其余 warp）
3. for wid in 0..NUM_WARPS:          // 选第一个 ready 的 warp
     if active(wid) && !stalled(wid) && warp_mask(wid): 选中，break
4. 给选中的 warp 分配 trace，填入 cid/wid/cta_id/PC/tmask/trap_epoch
5. suspend(选中 wid)                // 这条指令要跑完 fetch..commit 才能再调度
6. stalled_warps_ = stalled_warps_next_   // 周期末：把 next 态推进为当前态
```

#### 4.1.3 源码精读

先看 warp 持有哪些状态。`ipdom_stack` 是分支发散栈（4.4 节展开），`mscratch` 存内核参数指针（u4-l1 已讲），中间一组 `mepc/mcause/mtvec` 等是机器模式 trap CSR，`cta_csrs` 是 CTA 派发时写入的 block/grid/thread 索引快照：

[warp_t 结构 — 控制流状态，寄存器堆在 OpcUnit](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/scheduler.h#L77-L109)

调度器的构造函数里，`warps_` 是一个大小为 `NUM_WARPS` 的 `warp_t` 数组，每个 warp 按 `NUM_THREADS` 初始化它的 `tmask`。注意它把 `CtaDispatcher` 和 `BarrierUnit` 作为子 SimObject 创建并登记到平台——它们有独立 tick，但 Scheduler 持有指针：

[Scheduler 构造 — 创建 warps 数组与两个子模块（CTA dispatcher + barrier unit）](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/scheduler.cpp#L61-L84)

下面是本节的灵魂——每周期 `schedule()`。读这段代码时把它分成三段：派发新 warp（行 167-198）、选下一个 ready warp（行 201-206）、装填 trace 并 suspend（行 208-252）：

[schedule() — 每周期派发+选择+装填 trace+suspend，末尾推进 stalled 状态](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/scheduler.cpp#L163-L253)

注意第 201-206 行的选择逻辑——一个**线性扫描、选中第一个就 break** 的轮转（round-robin 在低 wid 优先意义上）。选中后，trace 装填的关键字段：

```cpp
trace->cid    = core_->id();
trace->wid    = scheduled_warp;
trace->PC     = warp.PC;     // 取指从这个 PC 读指令字
trace->tmask  = warp.tmask;  // 后续写回只对掩码内的线程生效
trace->trap_epoch = trap_epoch_.at(scheduled_warp);  // 见 advance_pc
this->suspend(scheduled_warp);  // 暂停，直到 decode/commit 把它 resume
```

行 244 的 `suspend` 和行 251 的 `stalled_warps_ = stalled_warps_next_` 合起来实现了"本拍释放的 warp 不会本拍重新调度"这个时序不变量。看 `suspend`/`resume` 本体——它们**只改 next 态**，断言也检查的是 next 态：

[suspend()/resume() — 只写 stalled_warps_next_，由 schedule() 末尾统一 clock 进当前态](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/scheduler.cpp#L267-L279)

> **为什么要双拍？** 假设 warp A 的指令在本拍 commit 完成、resume(A) 把它重新标记为就绪。如果 `resume` 直接改 `stalled_warps_`，那么**同一拍**稍后执行的 `schedule()`（注意 Core 的 tick 顺序是 commit→...→schedule，schedule 在最后，见下文）就可能立刻又选中 A，导致一条指令在一个 warp 上"零延迟背靠背"发射，破坏流水线时序保真。双拍机制把 resume 的效果推迟到下一拍才对选择逻辑可见，与 RTL 里寄存器更新的语义一致——这正是 SimX↔RTL model_parity（u7-l4）要求的。

PC 的推进不在 `schedule()` 里，而在 decode 阶段调用的 `advance_pc`（行 281-291）：按 RVC 与否 `+2` 或 `+4`。它还带一个 `trap_epoch` 守卫——如果这条 trace 是在最近一次异步 trap 之前取的，就丢弃，避免把 PC 越过 trap 设的 `mtvec`：

[advance_pc — decode 阶段推进 PC，并用 trap_epoch 丢弃陈旧的 post-trap fetch](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/scheduler.cpp#L281-L291)

`setTmask`（行 293-313）改线程掩码，有一个重要副作用：**当 tmask 被清空（`!tmask.any()`），warp 被反激活，并通知 CTA dispatcher 这个 warp 退出了**（`cta_dispatcher_->warp_done(wid)`）。这是 warp 生命周期走向终结的正常路径（kernel 返回时 `tmc x0` 把掩码清零，见 u4-l1）：

[setTmask — 改线程掩码；清空则反激活 warp 并通知 dispatcher 回收](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/scheduler.cpp#L293-L313)

最后，谁在调用 `schedule()`？Core 的 `tick()` 按固定顺序驱动流水线各级，`schedule` 是最后一步——这解释了为什么"本拍 commit resume 的 warp 不会本拍 schedule"：commit 在前，schedule 在后，但 resume 只动 next 态、schedule 读的是当前态：

[Core::tick — 流水线各级顺序 commit→execute→issue→decode→fetch→schedule](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L294-L304)

`Core::schedule()` 先算出 `warp_mask`（ibuf 未满的 warp 集合），再调 `scheduler_->schedule(warp_mask)`，把返回的 trace 推进 `fetch_latch_` 并把该 warp 的 ibuf 在途计数 +1：

[Core::schedule — 算 warp_mask（ibuf 未满）、调 Scheduler::schedule、推进 fetch_latch](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L308-L337)

#### 4.1.4 代码实践

**实践目标**：理解 `stalled_warps_` / `stalled_warps_next_` 双拍机制如何阻止同一 warp 背靠背发射。

**操作步骤**（源码阅读型实践）：

1. 打开 `sim/simx/scheduler.cpp`，定位 `schedule()`（行 163）、`suspend()`（行 267）、`resume()`（行 274）。
2. 在 `suspend()` 的 `stalled_warps_next_.set(wid)` 这一行旁边，写下："此改动本拍不可见。"
3. 跟踪一次完整生命周期：`schedule()` 选中 wid=0 → `suspend(0)`（next 置位）→ 行 251 把 next 拷给当前 → 下一拍 `decode` 处理完这条指令 → 某处 `resume(0)`（next 清位）→ 再下一拍 `schedule()` 才可能重新选中 wid=0。
4. 思考：如果删掉行 251、让 `suspend`/`resume` 直接改 `stalled_warps_`，会发生什么？

**需要观察的现象**：一个 warp 被选中后，最快也要**隔一拍**才能被再次选中——中间至少经过"被 suspend → next→current 推进 → resume → 再 next→current 推进"两次状态推进。

**预期结果**：你应当能用"两个寄存器 + 末尾统一 clock"一句话概括这个时序不变量，并解释它为何是 model_parity 的必要条件。

**待本地验证**：若你想动态确认，可在 `schedule()` 选中分支加一行临时日志打印 `(wid, cycle)`，构建 SimX 跑 `demo`（见 u1-l4），观察同一 wid 的两次选中之间至少相差 1 个 cycle。

#### 4.1.5 小练习与答案

**练习 1**：`schedule()` 选择 ready warp 时用的是 `for (wid=0..) break` 的线性扫描。这对低 wid 的公平性有什么影响？如果想让 warp 更"轮流"，可以怎么改（仅讨论，不改源码）？

> **答案**：低 wid 永远优先，高 wid 只在低 wid 全都 stalled/未 active 时才被选中，存在"饥饿"风险。改为轮转起点（如维护一个上周期选中的 wid，从 `last+1` 开始扫描）可提升公平性。但实际 warp 数少、流水线深，低优先级 warp 通常很快因 stalled 而让出，饥饿在实践中不明显；且改变仲裁策略会破坏与 RTL 的 cycle 级一致，必须 RTL 同步修改。

**练习 2**：`warp_t` 里为什么没有整数/浮点寄存器堆？把它们放进 `warp_t` 会有什么问题？

> **答案**：寄存器堆放在 `OpcUnit`（u6-l3），与操作数收集、写回通路同居一处，数据通路更短、模型更清晰。若塞进 `warp_t`，调度器就要持有大批寄存器状态，职责膨胀，且与 RTL（寄存器堆在 issue/execute 段）的结构对不上，破坏 model_parity 对应关系。

---

### 4.2 CTA 派发器：把一个 CTA 拆成多个 warp（cta_dispatcher.cpp）

#### 4.2.1 概念说明

`Scheduler::schedule()` 第一步是 `cta_dispatcher_->step()`——这一步负责"把一个还没启动的 CTA 切成 warp，逐周期送进空闲的 warp 槽"。它是 warp 生命周期的**入口**：所有 `active_warps_` 的置位最终都来自这里（或 wspawn）。

`CtaDispatcher` 的输入是 `kmu_req_t`（一个完整的 CTA 描述），输出是 `cta_warp_record_t`（**一个** warp rank 的启动信息）。一个 block_size=64、NUM_THREADS=32 的 CTA，会被切成 cta_size=2 个 warp rank，分两个 `step()` 周期产出。

#### 4.2.2 核心流程

```
KMU.step()  ──产出──►  kmu_req_t (一个 CTA: block_idx, block_size, lmem_size, cluster_dim, ...)
                           │
                           ▼
CtaDispatcher.step(active_warps):
  1. 若手中没有正在切的 CTA (has_cta_=false):
       a. 从 KMU 取一个 pending CTA（若没有 pending 就先 step KMU）
       b. 算 stride = align(lmem_size, MEM_BLOCK_SIZE)        // 固定步长
       c. max_slots = usable_slots(stride) = min(NUM_WARPS, floor(LMEM容量/stride))
       d. round-robin 选 tail_slot；若是 first_of_cluster，预留 K 个连续槽（都须空闲）
       e. 接纳：cta_size = ceil(block_size / NUM_THREADS), rank = 0
  2. 找最低 index 的空闲 warp 槽（active_warps 未置位的 wid）
  3. next_warp() 产出第 rank 个 warp 的记录，rank++
  4. 当 rank == cta_size 时，这个 CTA 切完，has_cta_ = false
                           │
                           ▼
Scheduler.activate_warp(wid, rec)  ──写入──►  warp.PC / tmask / mscratch / cta_csrs, active_warps_.set(wid)
```

**固定步长 LMEM 槽位**（fixed-stride）是这套派发的精髓：每个 CTA 在本地内存（LMEM）里占 `slot × stride` 起的一片连续区域。一个 cluster 的 K 个成员占 K 个**连续**槽，于是第 r 个成员的 LMEM 基址正好是 `issuer_base + r × stride`——这正是 DXA 多播分发数据时的寻址契约（见设计文档 §3.1、§4.1）。

#### 4.2.3 源码精读

派发给单个 warp 的记录结构——它是 dispatcher 与 scheduler 之间的契约。注意 `do_init`：首次启动一个 CTA 的第一个 warp 时需要跑完整 prologue（设 gp/sp/tp 等），后续 warp 复用则跳过 prologue、回卷到固定 20 字节的派发窗口（见 4.2.4 与 u4-l1）：

[cta_warp_record_t — 派发给一个 warp 的全部启动信息](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/cta_dispatcher.h#L26-L42)

`step()` 的前半段是"接纳一个新 CTA"——算 stride、算 max_slots、round-robin 选槽、为 cluster 预留连续槽：

[step() 上半段 — 从 KMU 取 CTA、算 stride/usable_slots、round-robin 接纳、cluster 连续预留](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/cta_dispatcher.cpp#L72-L144)

注意 cluster 预留逻辑（行 100-119）：若 `is_first_of_cluster`，要一次性检查 K 个连续槽**全部空闲**，否则本拍 `return false` 等待；这保证 cluster 成员之间不会中途卡住。`usable_slots` 的实现是一个简单的 `floor(容量/stride)` 并 cap 到 `NUM_WARPS`：

[usable_slots — 占用上限 = min(NUM_WARPS, floor(LMEM容量/stride))](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/cta_dispatcher.cpp#L63-L70)

`step()` 后半段——找一个空闲 wid、调 `next_warp` 产出记录、登记 `wid_to_slot_`：

[step() 下半段 — 找空闲 wid、产出 warp 记录、登记 wid→slot](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/cta_dispatcher.cpp#L146-L165)

`next_warp()` 是"切一刀"的核心。重点看三处：

1. **tmask 的计算**（行 208-213）：本 warp 激活 `min(num_threads, 剩余线程数)` 个线程。满 warp 是全 1；最后一个 warp 若线程数不够则是部分掩码（partial warp）。
2. **thread_idx 的 3D 推进**（行 216-224）：用 `warp_step` 加上进位（X 进 Y、Y 进 Z）算下一个 warp 的线程基坐标，保证 warp 间的线程索引在三维 block 里连续铺开。
3. **rank 递增与收尾**（行 228-231）：`rank_` 到 `cta_size_` 时 `has_cta_ = false`，下一拍 `step()` 会去切下一个 CTA。

[next_warp — 切出一个 warp rank：tmask、thread_idx 3D 推进、rank 递增](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/cta_dispatcher.cpp#L177-L234)

warp 退出时 Scheduler 调 `warp_done`，它通过 `wid_to_slot_` 反查槽号，递减 `slot_rem_warps_`，到 0 则槽位**立即释放**（乱序回收，无需按序）：

[warp_done — warp 退出时递减槽计数，最后一个退出则立即释放槽](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/cta_dispatcher.cpp#L167-L175)

最后看接收端——`Scheduler::activate_warp` 把这份记录拷进 warp 状态。注意行 122 的 PC 设置：`do_init ? rec.PC : (warp.PC - 20)`——复用同一个 warp 槽跑下一个 CTA 时，跳过一次性 prologue，回卷 20 字节（5 条指令）到固定派发窗口重新装载入口与参数（承接 u4-l1 的派发窗口）：

[activate_warp — 把 cta_warp_record_t 写入 warp 状态：PC/tmask/mscratch/cta_csrs，清空 ipdom 栈，置 active](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/scheduler.cpp#L116-L161)

#### 4.2.4 代码实践

**实践目标**：手算一个 CTA 被切成几个 warp、每个 warp 的 tmask 和 thread_idx 是什么。这是本讲综合实践的前半部分，先单独练。

**操作步骤**：

1. 假设配置 `NUM_THREADS=32`，一个 kernel 的 `block_dim=(10, 8, 1)`、`warp_step=(32,1,1)`（这是 `thread_idx` 每个 warp 的步进，X 方向加 32）。
2. 按 `next_warp` 的逻辑手算：
   - `block_size = 10 × 8 × 1 = 80`
   - `cta_size = ceil(80 / 32) = 3`（切成 3 个 warp）
   - warp rank 0：`active = min(32, 80) = 32`，tmask = 32 个 1，`thread_idx=(0,0,0)`，`block_size_rem = 80-32 = 48`
   - warp rank 1：`active = min(32, 48) = 32`，tmask = 32 个 1，`thread_idx` 推进：X 方向 `0+32 ≥ 10` 进位到 Y → `(0+32-10, 0+1, 0)`? 

**需要观察的现象**：注意 `thread_idx` 的进位用 `warp_step` 而非固定 32，且 X 超过 `block_dim[0]` 时回绕并进位到 Y。请你自己按行 216-224 的公式把 rank 1、rank 2 的 `thread_idx` 算出来。

**预期结果**：你应当得到 3 个 warp，前两个是满 warp（tmask 全 1，32 线程），第三个是 partial warp——`active = min(32, 80-64) = 16`，tmask 只有低 16 位为 1。这解释了为什么"最后一个 warp 经常是部分活跃"。

**待本地验证**：用 `--debug=3` 跑 `tests/regression/demo`（block 通常是一维），在 `activate_warp` 的 `DP(3, ...)` 日志里找 `tmask=` 字段，核对 partial warp 的掩码是否如你所算。

#### 4.2.5 小练习与答案

**练习 1**：为什么 LMEM 槽位用"固定步长"而不是变长字节环（byte ring）？

> **答案**：KMU 同一时刻只跑一个 kernel，所有并发驻留的 CTA 拥有**相同**的 `aligned_lmem_size`。把 N 个等大块装进变长环得到的密度，与固定步长分区的 `floor(LMEM容量/stride)` 完全相同，但固定步长省掉了环的回绕填充、进位链与按序回收逻辑，关键路径更短、面积更小（设计文档 §3.2）。代价是不能并发驻留两个 lmem_size 不同的 kernel——而当前架构本就只支持单 kernel 驻留。

**练习 2**：`cta_warp_record_t::do_init` 为何对同一 CTA 的不同 warp 取值可能不同？它的值由谁决定？

> **答案**：`do_init` 来自 `next_warp(!warp_init_mask_[free_wid], ...)`（行 156）——它取决于**目标 warp 槽**是否已被本 kernel 初始化过，而非 CTA 本身。一个 warp 槽第一次被本 kernel 使用时要跑完整 prologue（`do_init=true`）；同一槽跑后续 CTA 时复用已初始化状态，`do_init=false`，PC 回卷 20 字节到派发窗口。`cur_kernel_pc_` 变化时（行 122-127）会清空 `warp_init_mask_`，强制下一个 kernel 重新初始化。

---

### 4.3 屏障单元：到达、等待与代际释放（barrier_unit.cpp）

#### 4.3.1 概念说明

warp 之间需要同步——最常见的就是 CUDA 的 `__syncthreads()`：让一个 block 内所有线程都到达某点后再一起往下走。Vortex 把这映射到一条 `BAR`（warp 控制指令），由 `BarrierUnit` 实现。

`BarrierUnit` 的核心思想是**计数 + 代际（phase）**：每个屏障记录"已经到达的 warp 数"和"一个代际标识"。当到达数凑齐，代际翻转，所有等待的 warp 被唤醒。代际的作用是让晚到的 `wait` 能判断自己面对的是"这一轮"还是"下一轮"屏障。

屏障分**本地**（local，core 内）和**全局**（global，跨 core，由 Socket 汇聚）两种，由 `bar_id` 的最高位区分。

#### 4.3.2 核心流程

```
warp 执行 BAR 指令 (wctl_unit.cpp)
   ├─ arrive(bar_id, count, wid, is_sync_bar)   // "我到了"
   │     本地: wait_mask 记录 sync 到达者；count+1==目标 && events==0 → 唤醒全部 wait, phase++
   │     全局: arrival_mask 记录；凑齐本 core 的 active warps 且 events==0 → 上报 Socket
   ├─ wait(bar_id, phase, wid)                   // "我等"
   │     若 barrier.phase == 传入 phase → 还在这一轮，加入 wait_mask 并 suspend
   └─ (异步) event_attach/event_release          // DMA 等异步事务的 expect_tx 计数
```

关键：`is_sync_bar` 表示这条 BAR 是"到达即等待"的同步屏障（如 `__syncthreads()`）——它既算一次到达，又把 warp 挂起；而非同步的 `arrive` 只记到达不挂起（fire-and-forget）。

#### 4.3.3 源码精读

屏障状态结构。`wait_mask` 是真正挂起等待的 warp 集，`arrival_mask` 是全局屏障在本 core 内的到达跟踪，`count`/`events` 是到达计数与异步事件计数，`phase` 是代际：

[warp_barrier_t — 屏障的计数、等待集、代际](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/types.h#L103-L117)

`arrive()` 是核心。分本地（行 69-93）与全局（行 52-68）两支。先看本地支：`sync_bar` 时把 wid 加入 `wait_mask`；当 `count+1 == 目标 count && events==0` 时，唤醒所有 `wait_mask` 中的 warp（调 `scheduler_->resume`），清空 `wait_mask` 并 `phase++`；最后 `count = (count+1) % count` 做模运算（用目标 count 作模，实现代际循环计数）：

[arrive() 本地支 — 到达计数到齐且无挂起事件则唤醒所有等待者并翻代际](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/barrier_unit.cpp#L69-L93)

全局支更微妙：`arrival_mask` 跟踪本 core 内已到达的 warp，只有当**本 core 全部 active warp 都到达**且 `events==0` 时，才向 Socket 上报 `global_barrier_arrive`。注意行 57-60：异步 arrive **不**加入 `wait_mask`（否则 `global_resume` 会错误地唤醒一个并没挂起的 warp）；`wait_mask` 只在 `is_sync_bar` 时才置位，且跨全局跳转保留、由 `global_resume` 清除：

[arrive() 全局支 — arrival_mask 凑齐本 core active warps 才上报 Socket](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/barrier_unit.cpp#L52-L68)

`wait()` 用 `phase` 判断是否还要等：若 `barrier.phase == 传入 phase`，说明这一轮屏障还没释放，加入 `wait_mask` 返回 `true`（调用方据此 suspend 自己）：

[wait — 用 phase 判断是否仍在本轮；是则加入 wait_mask](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/barrier_unit.cpp#L96-L111)

`global_resume()` 由 Socket 在所有 core 都到达后回调，唤醒本 core 在该屏障上等待的 warp 并翻代际：

[global_resume — Socket 回调，唤醒 wait_mask 中本 core 的等待者](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/barrier_unit.cpp#L113-L127)

最后，`event_attach` / `event_release` 处理**异步事务屏障**：DMA（DXA）拷贝完成前不能放行，于是用 `expect_tx` 预注册事件计数，每次完成调 `event_release` 减一；当 `events` 减到 0 时，才检查屏障是否可以释放（行 142-166）。这是 `vx_barrier` 与异步拷贝协作的基础（u9-l2 展开）：

[event_release — 异步事件计数减到 0 时才尝试释放屏障](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/barrier_unit.cpp#L135-L167)

> **谁触发这些调用？** `BarrierUnit` 的方法并不直接被硬件信号驱动，而是 warp 执行 `BAR` 指令时，由 `WctlUnit::process`（`wctl_unit.cpp` 的 `WctlType::BAR` 分支）翻译成 `barrier_arrive` / `barrier_wait` / `barrier_event_attach` 调用，最终落到 `BarrierUnit`。可参考 [wctl_unit.cpp BAR 分支](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/wctl_unit.cpp#L113-L140)。

#### 4.3.4 代码实践

**实践目标**：用代际（phase）解释"为什么屏障可以重复使用"。

**操作步骤**（源码阅读型）：

1. 假设一个 CTA 有 2 个 warp（wid=0,1），都执行 `__syncthreads()`（即 `is_sync_bar=true`，`count=2`）。
2. 跟踪 `arrive` 与 `wait` 的调用序列：
   - 初始：`count=0, phase=0, wait_mask={}`
   - warp 0 先到：`arrive(bar, 2, 0, true)` → `wait_mask={0}`，`count+1=1 ≠ 2` → 不释放，`count = 1%2 = 1`。warp 0 被 suspend。
   - warp 1 到：`arrive(bar, 2, 1, true)` → `wait_mask={0,1}`，`count+1=2 == 2 && events==0` → 唤醒 wid 0 和 1，清 `wait_mask`，`phase=1`，`count = 2%2 = 0`。
3. 假设循环里又有一次 `__syncthreads()`：此时 `phase=1`。warp 0 调 `wait(bar, phase=1, 0)` → `barrier.phase==1` → 还在这一轮，加入 `wait_mask`。

**需要观察的现象**：第二轮屏障复用了同一个 `warp_barrier_t`，靠 `phase` 翻转区分第一轮（phase=0）和第二轮（phase=1）。

**预期结果**：你能解释"到达计数用 `(count+1) % count` 回绕 + phase 翻转"这套组合如何让一个屏障槽被无限次复用，而不混淆两轮到达。

#### 4.3.5 小练习与答案

**练习 1**：为什么全局屏障要分 `arrival_mask` 和 `wait_mask` 两套？只用一个 `wait_mask` 行不行？

> **答案**：异步 `arrive`（`is_sync_bar=false`）不挂起 warp，如果把它也记进 `wait_mask`，`global_resume` 就会去 `resume` 一个根本没挂起的 warp，造成错误唤醒。`arrival_mask` 专门跟踪"到达与否"（含异步），`wait_mask` 只记真正挂起等待的 warp（仅 `sync_bar` 与 `bar_wait` 写入）。两者职责正交，缺一不可。

**练习 2**：`count = barrier_count_p1 % count` 这行（行 92）里，被模的 `count` 是参数（目标到达数），模数与加数同名容易混淆。它实现的是什么语义？

> **答案**：它实现"到达计数在 `[0, count)` 之间循环"。每到达一次 `+1`，到达 `count` 次时模回 0，配合 `phase++` 形成代际切换。这样下一个代际又从 0 开始计数，屏障槽即可被复用。注意 `count==0` 会在行 88-91 触发 `abort`（目标到达数不能为 0）。

---

### 4.4 分支发散与汇聚：IPDOM 栈（warp_t + wctl_unit.cpp）

#### 4.4.1 概念说明

warp 内的线程如果在一个分支上"分道扬镳"——比如奇数线程走 if、偶数线程走 else——这就是**分支发散（divergence）**。SIMT 硬件无法让一个 warp 内的线程同时走两条路，于是**串行**执行两条路径：先走一边，再走另一边，最后在一个**汇聚点（reconvergence point）**重新合并。

Vortex 用 **IPDOM 栈（Immediate Post-Dominator 栈）** 管理这件事。IPDOM 的直觉是：两条分支最可能合并的地点，就是它们**最近的共同后继**（immediate post-dominator）。发散时把"汇聚点"和"另一边的掩码"压栈；走完一边时弹栈，要么走另一边、要么恢复合并。

这条机制在 u4-l2 已经从 kernel API（`SPLIT`/`JOIN` 指令）角度讲过；本讲从调度器持有的状态角度补完它的实现。

#### 4.4.2 核心流程

```
SPLIT 指令（wctl_unit.cpp WctlType::SPLIT）:
  算 then_tmask (条件真) 与 else_tmask (条件假)
  is_divergent = 两者都非空
  if is_divergent:
      选较少的一边先执行（heuristic：减少被串行的线程数）
      压栈 ipdom_entry{orig_tmask=当前tmask, else_PC=下一条指令}
      setTmask(较少的一边)              // 先走这边
  else:
      setTmask(then_tmask 或 else_tmask) // 没发散，只走一边

JOIN 指令（wctl_unit.cpp WctlType::JOIN）:
  if 栈顶.fallthrough == false:          // 这条路径是第一边走完
      next_tmask = ~当前tmask & 栈顶.orig_tmask   // 换成另一边
      PC = 栈顶.else_PC                            // 跳到 else 入口
      栈顶.fallthrough = true                      // 标记：下次 JOIN 是第二边走完
  else:                                   // 第二边也走完了
      next_tmask = 栈顶.orig_tmask                 // 恢复合并后的全掩码
      弹栈
  setTmask(next_tmask)
```

每个 warp 有自己的 `ipdom_stack`（`std::stack<ipdom_entry_t>`），深度上限 `ipdom_size_ = NUM_THREADS - 1`（行 69）——理论最深是每个线程各自发散，但实际很少接近上限。

#### 4.4.3 源码精读

栈条目结构。`orig_tmask` 是发散前的完整活跃掩码（汇聚时要恢复），`else_PC` 是另一条路径的入口地址，`fallthrough` 标记"是否已经走过第一边"：

[ipdom_entry_t — 一个发散点的汇聚信息：原掩码、else 入口、是否已走完第一边](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/scheduler.h#L36-L46)

SPLIT 的处理。行 83 的启发式 `then_tmask.count() <= else_tmask.count() ? then_tmask : else_tmask` 选**较少线程**的一边先走——这样第一次串行时，被"晾在一边"的线程更少。压栈时存的是**当前完整 tmask**（不是较少那边的掩码），因为汇聚时要恢复的是"所有原本活跃的线程"。`dst_data[t] = stack_size`（行 73-75）把压栈前的栈深度写回给 kernel，作为 `JOIN` 指令的参数（kernel 用它定位要 JOIN 到哪一层）：

[SPLIT — 算 then/else 掩码，发散时压栈并先走较少的一边](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/wctl_unit.cpp#L58-L89)

JOIN 的处理。`stack_ptr`（kernel 传入的栈深度）若不等于当前栈大小，说明要汇聚到这一层。分两种情况：`top.fallthrough==false`（第一边刚走完）→ 切到另一边，PC 跳到 `else_PC`，置 `fallthrough=true`；`top.fallthrough==true`（第二边也走完）→ 恢复 `orig_tmask`，弹栈：

[JOIN — 第一边走完则切另一边并跳 else_PC，第二边走完则恢复原掩码并弹栈](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/wctl_unit.cpp#L90-L112)

> 注意：栈的实际 push/pop 发生在 `WctlUnit`（执行段），但**栈本身是 warp 状态**（`warp_t::ipdom_stack`，scheduler.h 行 80），由 Scheduler 拥有。`CtaDispatcher` 派发新 CTA 时会在 `activate_warp` 里清空它（scheduler.cpp 行 145：`while (!warp.ipdom_stack.empty()) warp.ipdom_stack.pop()`）——保证复用 warp 槽时不携带上一 CTA 的发散残留。

#### 4.4.4 代码实践

**实践目标**：用 IPDOM 栈走完一次"if-else 发散→汇聚"的全过程。这是综合实践的后半部分。

**操作步骤**：

1. 假设一个 warp 有 4 个线程，`tmask = {0,1,2,3}`（全活跃），PC=0x100 处是一条 SPLIT（条件分支），各线程的条件结果是：线程 0、1 为真，线程 2、3 为假。
2. 按源码推演：
   - `then_tmask = {0,1}`，`else_tmask = {2,3}`，`is_divergent = true`
   - `then.count()=2 == else.count()=2`，选 then 先走
   - 压栈：`ipdom_entry{orig_tmask={0,1,2,3}, else_PC=0x104, fallthrough=false}`
   - `setTmask({0,1})`：只有线程 0、1 执行 if 体，线程 2、3 暂停
   - 设 if 体末尾 PC=0x120 处是 JOIN，`stack_ptr=1`
3. 到 JOIN（`fallthrough=false` 支）：
   - `next_tmask = ~{0,1} & {0,1,2,3} = {2,3}`
   - `warp.PC = 0x104`（else 入口），`fallthrough = true`
   - `setTmask({2,3})`：线程 2、3 执行 else 体，线程 0、1 暂停
4. else 体回到 PC=0x120 的 JOIN（`fallthrough=true` 支）：
   - `next_tmask = orig_tmask = {0,1,2,3}`
   - 弹栈
   - `setTmask({0,1,2,3})`：四线程重新合并，从 0x120 之后一起往下走

**需要观察的现象**：整个 if-else 期间，warp 内 4 个线程被分成两批**串行**执行；同一时刻只有一半线程活跃，另一半在 tmask 里被屏蔽。这模拟了 GPU 处理发散时的"性能惩罚"——发散使有效并行度减半。

**预期结果**：你能画出 tmask 与 PC 随执行推进的变化表，并指出汇聚点（0x120）就是 if 体与 else 体的最近共同后继（immediate post-dominator）。

**待本地验证**：`tests/regression` 下有发散测试（如 `activation`、`branch` 类），可用 `--debug=3` 运行，在 `setTmask` 的 `DT(3, ...)` 日志里观察 tmask 在 `{0,1}`、`{2,3}`、`{0,1,2,3}` 之间切换的过程。

#### 4.4.5 小练习与答案

**练习 1**：SPLIT 为什么要选"线程数较少的一边"先走？

> **答案**：先把较少线程的一边跑完，意味着汇聚前被串行的第二批线程数更少，整体串行段更短，平均吞吐更高。这是一种贪心启发式（greedy heuristic），与 NVIDIA 早期 GPU 的 SIMT 调度策略一致。它不改变正确性，只影响性能。

**练习 2**：如果 `stack_ptr != stack_size`（JOIN 指明要汇聚到的层不等于当前栈深），代码进入处理；如果相等会怎样？

> **答案**：相等时（行 96 的 `if (stack_ptr != stack_size)` 为假）直接跳过整个 if 块，`next_tmask` 保持为 `warp.tmask` 不变，`setTmask` 是个 no-op。这对应"这个 JOIN 不对应任何待汇聚的发散点"（例如 kernel 显式发出的冗余 JOIN，或发散已被更早的 JOIN 处理），属于安全的不操作。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一个完整的"warp 生命周期"跟踪任务。

**任务**：给定一个 kernel 启动配置，画出从 CTA 派发到 warp 退出、再到屏障同步的完整状态轨迹。

**场景**：`NUM_WARPS=4`、`NUM_THREADS=4`，启动一个 kernel：`grid_dim=(2,1,1)`、`block_dim=(8,1,1)`（即每个 CTA 8 个线程）、`cluster_dim=(1,1,1)`、`lmem_size=64` 字节、`MEM_BLOCK_SIZE=64`。kernel 结构（伪代码）：

```c
// 示例代码（伪代码，非项目源文件）
void kernel_main() {
  int tid = get_thread_id();
  if (tid % 2 == 0) {
    // A 路径：偶数线程
  } else {
    // B 路径：奇数线程
  }
  __syncthreads();   // 屏障
}
```

**操作步骤**：

1. **CTA 切分**（4.2）：每个 CTA `block_size=8`，`cta_size = ceil(8/4) = 2` 个 warp。手算 warp rank 0 的 tmask（4 个 1）和 rank 1 的 tmask（4 个 1）。两个 CTA 各切 2 个 warp，但 `NUM_WARPS=4`，所以同一时刻最多 4 个 warp 共存——跟踪 `CtaDispatcher` 如何用 round-robin 槽位接纳它们。
2. **激活**（4.1）：对照 `activate_warp`，写出第一个 warp 被激活时 `warp.PC`、`warp.tmask`、`warp.cta_csrs.block_idx` 的值。注意 `do_init` 的取值与 PC 的回卷规则。
3. **分支发散**（4.4）：4 个线程中 tid=0,2 走 A，tid=1,3 走 B。按 4.4.4 的方法推演 SPLIT 压栈、JOIN 切换、JOIN 弹栈的 tmask 与 PC 变化。
4. **屏障同步**（4.3）：两个 warp 都到达 `__syncthreads()`（`count=2`，`is_sync_bar=true`）。按 4.3.4 的方法跟踪 `arrive`/`wait`、`wait_mask` 与 `phase` 的变化，确认两个 warp 都被正确唤醒。
5. **退出**（4.1）：kernel 返回时 `tmc x0` 清空 tmask，触发 `setTmask` → `warp_done` → 槽位释放。

**预期产出**：一张表格，列出每个关键周期里 `active_warps_`、`stalled_warps_`、各 warp 的 `PC`/`tmask`/`ipdom_stack 深度`，以及屏障的 `count`/`wait_mask`/`phase`。

**待本地验证**：构建 SimX（`../configure --xlen=64` 后 `make`），用类似 `./ci/blackbox.sh --driver=simx --app=<发散类测试> --threads=4 --warps=4` 跑一个发散测试，加 `--debug=3`，把日志里的 `dispatch CTA warp`、`warp-state`、`Barrier arrive` 行与你的手算表对照。

## 6. 本讲小结

- **warp 状态机**：`warp_t` 持有 PC、tmask、ipdom 栈、trap CSR 与 CTA CSR 快照（寄存器堆在 OpcUnit）；Scheduler 用 `active_warps_` + `stalled_warps_`（当前态）+ `stalled_warps_next_`（下一态）三套掩码管理就绪，末尾统一 clock 实现时序保真。
- **周期级调度**：`schedule()` 三步走——派发新 CTA warp、选第一个 ready warp、装填 trace 并 suspend；双拍机制保证"本拍释放的 warp 不会本拍重新调度"。
- **CTA 派发**：`CtaDispatcher` 把 KMU 产出的每个 CTA 按 `ceil(block_size/NUM_THREADS)` 切成 warp rank，用**固定步长 LMEM 槽位**接纳，cluster 成员占连续槽以支持 DXA 多播；输出经 `activate_warp` 写入 warp 状态。
- **屏障同步**：`BarrierUnit` 用"到达计数 + 代际（phase）"实现本地与全局屏障，`wait_mask` 只记真正挂起的 warp，`arrival_mask` 跟踪全局到达；异步事件（`expect_tx`）用 `events` 计数延迟释放。
- **分支发散**：IPDOM 栈在发散点压入"汇聚掩码 + else 入口"，SPLIT 先走线程较少的一边，JOIN 分两次（走另一边、再恢复合并），实现 warp 内串行处理分支、在最近共同后继汇聚。
- **职责边界**：Scheduler 拥有 warp 生命周期与状态，CtaDispatcher 是它的子模块负责派发，BarrierUnit 是它的子模块负责同步，IPDOM 栈虽由 WctlUnit 在执行段 push/pop，但栈本身是 warp 状态。

## 7. 下一步学习建议

- **u6-l2（取指、解压、译码）**：本讲只讲到"trace 被装填并推进 `fetch_latch_`"，下一步看 fetch 如何从 PC 取指令字、RVC 压缩指令如何解压、decode 如何把 `advance_pc` 调回 Scheduler。
- **u6-l3（发射、记分板与操作数收集）**：看 ibuf 如何接收本讲产出的 trace、scoreboard 如何在 `setTmask`/`resume` 之外用第二套机制管理数据冒险、OpcUnit 如何持有本讲刻意没放进 `warp_t` 的寄存器堆。
- **u7-l3（调度器与 warp 控制 RTL）**：把本讲的 `Scheduler`/`CtaDispatcher`/`BarrierUnit`/IPDOM 栈逐一对照 RTL 的 `VX_scheduler.sv`、`VX_cta_dispatch.sv`、`VX_ipdom_stack.sv`，体会 model_parity 要求的 cycle 级一致。
- **u9-l2（DXA 异步拷贝与多播）**：理解本讲 cluster 连续槽位（`issuer_base + r × stride`）为何是 DXA 多播的寻址契约，把 4.2 的"固定步长"与异步拷贝的接收端对齐。
