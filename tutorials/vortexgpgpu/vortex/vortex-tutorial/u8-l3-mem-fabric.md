# 访存合并、本地内存与 DRAM 模型

## 1. 本讲目标

本讲接着 [u8-l2（缓存标签、MSHR、替换与数据通路）](u8-l2-cache-internals.md) 继续向下走：缓存之下的「线」（warp 内多个线程的请求如何合并）、缓存之外的「近内存」（本地内存 LMEM，即 CUDA 的 shared memory），以及整条内存梯子的最末端（DRAM 时序模型）。

学完本讲你应当能够：

- 说清一个 warp 内多个线程的访存请求如何被 `MemCoalescer` 合并成更少的 cache 请求，以及为什么「合并」直接决定了带宽利用率。
- 解释本地内存（LMEM）的组织：地址如何被路由到这条短路、它如何用 bank 交叉的 SRAM 提供单周期访问、它和 DXA 的 DMA 端口如何共享同一块 SRAM。
- 说明 `Memory` 如何把「数据通路」（RAM 字节读写）与「时序模型」（ramulator2/HBM2）解耦，以及 ramulator2 如何用时钟比缩放为 Vortex 提供周期级 DRAM 时序。

贯穿本讲的主线仍然是 SimX 与 RTL 的 model_parity：合并器、本地内存、DRAM 三者都在两侧有对应实现，共享同一套 `VX_types.toml` 地址布局契约。

## 2. 前置知识

- **warp 与 lane**：Vortex 用 SIMT 执行，一条 warp 指令在多个线程（lane）上同时运行。访存指令（LD/ST/AMO）会在 AGU 阶段为每个活跃 lane 算出一个地址，于是同一条 warp load 天然带「一组地址」。
- **cache line 与 word**：缓存以 line（如 64 字节）为搬运单位，但合并器工作的粒度更细——是 dcache 的 *word*（`DCACHE_WORD_SIZE`，由 `VX_CFG_DCACHE_WORD_SIZE` 配置，默认等于 LSU line size）。参见 [u8-l1](u8-l1-mem-cache-overview.md) 关于 line/sector/word 三种粒度解耦的讨论。
- **通道（channel）**：dcache 可以有多个独立端口（`DCACHE_CHANNELS`），每个端口每周期接一个请求。合并器要把「一组 lane 地址」收敛到「若干通道请求」。
- **TLM（Transaction-Level Modeling）**：本讲的内存模块都采用 TLM 风格——请求/响应包里直接带数据载荷（`shared_ptr<mem_block_t>`，一条 cache line 大小的字节块），而不是只传地址再后门读 RAM。这条约定来自 [u5-l3 的基数规则](u5-l3-simx-cardinal-rule.md)：叶单元只能从输入 channel 读数据。
- **ramulator2**：一个开源的周期精确 DRAM 模型器，Vortex 把它当作第三方库（`third_party`）链接进来，为 `Memory` 提供 HBM2 的行缓冲/刷新/调度时序。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [sim/simx/mem/mem_coalescer.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mem_coalescer.cpp) | 访存合并器：把一组 lane 请求收敛成更少的 dcache 通道请求，再把多通道响应合并回一组 lane 响应。 |
| [sim/simx/mem/mem_coalescer.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mem_coalescer.h) | 合并器端口与内部状态（`pending_rd_reqs_`、`out_round_`、`sent_mask_`）声明。 |
| [sim/simx/mem/local_mem.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/local_mem.cpp) | 本地内存（共享内存）：bank 交叉的 SRAM，提供单周期读写的近存储。 |
| [sim/simx/mem/local_mem.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/local_mem.h) | 本地内存 `Config`（容量/线宽/请求数/bank 数/写响应）与端口。 |
| [sim/simx/mem/memory.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/memory.cpp) | DRAM 顶层：bank 交叉的 crossbar + 后备 RAM（数据）+ DramSim（时序）。 |
| [sim/common/dram_sim.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/dram_sim.cpp) | ramulator2 封装：HBM2 配置、时钟比缩放、通道拆分。被 simx 与 gem5 后端共用。 |
| [sim/simx/types.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/types.h) | `LsuReq`/`LsuRsp`/`MemReq`/`MemRsp` 载荷结构与 `AddrType`/`get_addr_type` 地址分类。 |
| [sim/simx/constants.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/constants.h) | `LSU_CHANNELS`/`DCACHE_CHANNELS`/`DCACHE_WORD_SIZE` 等派生宽度常量。 |
| [hw/rtl/mem/VX_local_mem.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/mem/VX_local_mem.sv) | 本地内存 RTL：请求/响应 stream crossbar + 每 bank SP-RAM + DMA 优先级 + RDW 冒险。 |

## 4. 核心概念与源码讲解

本讲按数据从 LSU 流向 DRAM 的顺序，拆成三个最小模块：先讲 LSU 出口处的**访存合并器**（4.1），再讲旁路出去的**本地内存**（4.2），最后讲梯子末端的 **DRAM 模型**（4.3）。

在进入细节前，先用一张图定位三者关系（SimX 视角，单个 LSU block）：

```
                                Shared 地址?
        LSU ──> lmem_switch ──┬──(是)──> lsu_lmem_adapter ──> LocalMem (LMEM, 近内存)
   (一组 lane 请求)            │
                              └──(否, Global)──> MemCoalescer ──> dcache ──> ... ──> L2 ──> L3 ──> Memory(DRAM)
                                  (合并 lane)                                (RAM 数据 + ramulator2 时序)
```

即 `lmem_switch` 是分叉点：命中 `AddrType::Shared`（本地内存区）的 lane 走 LMEM 短路，其余走合并器进 dcache，最终落到 DRAM。

### 4.1 访存合并器 MemCoalescer

#### 4.1.1 概念说明

一条 warp load 指令会为每个活跃 lane 各算出一个地址。如果这些地址彼此靠近（落在同一个 dcache word 内），把它们逐个送给 dcache 就是浪费——dcache 只会把同一个 word 反复读好几遍。**访存合并（coalescing）** 就是把这些「同 word」的 lane 请求折叠成一个 dcache 请求，从而把 warp 的并行性转成实际的带宽节省。

CUDA 程序员熟悉的「连续访问 = 高带宽、跨步访问 = 低带宽」现象，在硬件层面就是由合并器决定的。Vortex 用一个计数器 `VX_CSR_MPM_COALESCER_MISS`（合并未命中）来量化合并效率：合并得越彻底，该计数越小。

合并器还有一个对称职责：dcache 给的是**按通道**的响应（每个通道一条 line），合并器要把它**展开回按 lane** 的响应送给 LSU——同一份 line 数据被「复制」（其实是 `shared_ptr` 别名，零拷贝）给所有命中它的 lane。

#### 4.1.2 核心流程

合并器有「请求侧合并」与「响应侧展开」两条路，且请求侧采用**构建拍/排空拍交替**的节拍：

```text
请求侧（每拍 on_tick）：
  1. 若有待排空的合并轮次 out_round_ → flush_out_round()，本拍不再构建
  2. 否则从 ReqIn 取一条 LsuReq（一组 lane 地址）
  3. 对每个输出通道 o：
       a. 取该通道组内第一个未发送的 lane i 作 seed
       b. seed_addr = addrs[i] 对齐到 DCACHE_WORD_SIZE
       c. 扫描同组其余 lane，地址对齐相等者并入 cur_mask（非 AMO 才合并）
       d. 该通道产出一个 MemReq（addr=seed_addr, 覆盖 cur_mask）
  4. 读/AMO 分配一个响应 tag，记下 cur_mask 到 pending_rd_reqs_[tag]
  5. 标记 out_round_.valid，下一拍由 flush_out_round 逐通道发出去
  6. 排空完成后 sent_mask_ |= cur_mask；当 sent_mask_ 覆盖全部活跃 lane → 弹出 LsuReq

响应侧（每拍 on_tick 优先处理）：
  1. 扫各通道 RspIn，把同 tag 的分片聚成一组 lane_mask
  2. 用 pending_rd_reqs_[tag].mask 把通道数据展开到对应 lane（shared_ptr 别名）
  3. 组装一条 LsuRsp 经 RspOut 送回 LSU
  4. 清掉已收到的 lane；当 entry.mask 全清 → 释放 tag
```

关键约束：

- **合并不跨通道**：内层扫描只在 `output_ratio_` 范围内（同一通道组的 lane），因为不同通道本来就负责互不重叠的地址粒度区域。
- **一条 LsuReq 可能要多个轮次**：若同一通道组内的 lane 散落到多个 word，第一轮只合并 seed 所在 word 的 lane，剩下的留待下一轮（由 `sent_mask_` 跟踪）。
- **AMO 不合并**：RVA 对 AMO 操作数不保证交换律，故每条 AMO lane 各自独立出请求（见 4.1.3）。
- **节拍**：一批请求至多每两周期发一次（构建拍 + 排空拍），这是与 RTL cycle 级对齐的刻意设计。

#### 4.1.3 源码精读

**端口与尺寸**。合并器有两侧：输入侧是单条 `LsuReq`（含一组 lane 地址），输出侧是 `output_size_` 条 `MemReq` 通道，直连 dcache。每条输入 lane 与输出通道的关系是 `i = o * output_ratio_ + r`，即每个输出通道负责 `output_ratio_` 条连续 lane：

[sim/simx/mem/mem_coalescer.cpp:20-40](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mem_coalescer.cpp#L20-L40) —— 构造函数，`output_ratio_ = input_size / output_size`，`pending_rd_reqs_` 是按 tag 索引的待响应表。

这些尺寸来自 `core.cpp` 的实例化，`input_size=LSU_CHANNELS`（lane 数）、`output_size=DCACHE_CHANNELS`（dcache 通道数）、`line_size=DCACHE_WORD_SIZE`（合并粒度）：

[sim/simx/core.cpp:117-121](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L117-L121) —— `MemCoalescer::Create(sname, LSU_CHANNELS, DCACHE_CHANNELS, DCACHE_WORD_SIZE, LSU_QUEUE_OUT_SIZE, 1)`。

而 `LSU_CHANNELS`、`DCACHE_CHANNELS`、`DCACHE_WORD_SIZE` 又是从配置派生的：

[sim/simx/constants.h:52-64](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/constants.h#L52-L64) —— `LSU_CHANNELS = NUM_LSU_LANES`；`DCACHE_CHANNELS = __UP((NUM_LSU_LANES * XLENB) / DCACHE_WORD_SIZE)`。注释明确：`DCACHE_WORD_SIZE`（合并器输出粒度）与 `LSU_LINE_SIZE` 解耦，使通道/bank 能独立于 LSU 流水线数量缩放。

> 注意：合并器**并非总是存在**。只有当 `NUM_LSU_LANES > 1` 且 `DCACHE_WORD_SIZE > LSU_WORD_SIZE` 时才实例化合并器，否则用一个直通适配器 `lsu_dcache_adapter` 旁路掉。见 [sim/simx/core.cpp:165-190](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L165-L190) 的 if/else 分支。

**请求合并核心**。合并的精髓是把地址对齐到 word 粒度再比对相等：

[sim/simx/mem/mem_coalescer.cpp:147-181](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mem_coalescer.cpp#L147-L181) —— 关键三行：

```cpp
uint64_t addr_mask = ~uint64_t(line_size_-1);          // 对齐掩码
...
uint64_t seed_addr = in_req.addrs.at(i) & addr_mask;   // seed 对齐到 word
cur_mask.set(i);
if (!in_is_amo) {
  for (uint32_t s = r + 1; s < output_ratio_; ++s) {
    ...
    uint64_t match_addr = in_req.addrs.at(j) & addr_mask;
    if (match_addr == seed_addr) {                     // 同 word → 合并
      cur_mask.set(j);
    }
  }
}
```

即：seed 取对齐地址，同组内对齐地址相等的 lane 都并入 `cur_mask`。

**AMO 特例**。AMO（原子操作）刻意不合并，且地址保持字节级（不对齐到 word），这样下游 bank 才能把 read-modify-write 结果放回 line 内正确偏移；同时把该 lane 的原始 `tid` 透传，让内存边界处的 hart id 能命名到具体 lane：

[sim/simx/mem/mem_coalescer.cpp:168-190](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mem_coalescer.cpp#L168-L190) —— 注释「RVA gives no commutativity guarantee across AMO operands」，以及 `out_addrs.at(o) = in_is_amo ? in_req.addrs.at(i) : seed_addr`（[L222](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mem_coalescer.cpp#L222-L222)）。

**tag 分配**。只有读和 AMO 需要响应（store 通常不需要），故只为它们分配 tag，并把 `cur_mask` 存进 `pending_rd_reqs_` 供响应展开用：

[sim/simx/mem/mem_coalescer.cpp:229-235](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mem_coalescer.cpp#L229-L235) —— `tag = pending_rd_reqs_.allocate(pending_req_t{in_req.tag, cur_mask})`。注释指出 AMO 必须走这条分支，否则响应会误走 store 路径、LSU MSHR 重放永不触发。

**合并效率计数**。「miss」定义为「本轮合并到的 lane 数 ≠ 该请求全部活跃 lane 数」，即没能一次合并干净：

[sim/simx/mem/mem_coalescer.cpp:262-263](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mem_coalescer.cpp#L262-L263) —— `perf_stats_.misses += (cur_mask.count() != in_req.mask.count())`。该值经 CSR 暴露为 `VX_CSR_MPM_COALESCER_MISS`，见 [sim/simx/csr_unit.cpp:211-223](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/csr_unit.cpp#L211-L223)。

**构建拍/排空拍**。构建好的轮次不在当拍发出，而是标记 `out_round_.valid`，下一拍由 `flush_out_round` 逐通道发；这保证「一批至多每两周期发一次」：

[sim/simx/mem/mem_coalescer.cpp:265-269](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mem_coalescer.cpp#L265-L269) 与 [sim/simx/mem/mem_coalescer.cpp:273-293](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mem_coalescer.cpp#L273-L293)（`flush_out_round`：逐通道 `try_send`，全部接受后 `sent_mask_ |= cur_mask`，覆盖全部活跃 lane 则 `ReqIn.pop()`）。

**响应展开**。响应侧把同 tag 的多通道分片聚成 `lane_mask`，再用 `entry.mask`（请求侧记下的 `cur_mask`）把每条通道 line 数据映射回输入 lane——注意是 `shared_ptr` 别名赋值，**零拷贝**：

[sim/simx/mem/mem_coalescer.cpp:89-109](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mem_coalescer.cpp#L89-L109) —— 组装 `LsuRsp`，`out_rsp.data.at(i) = lane_data.at(j)`。当 `entry.mask` 清空（[L113-118](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mem_coalescer.cpp#L113-L118)）即整组响应收齐，释放 tag。

#### 4.1.4 代码实践

**实践目标**：用一个具体地址序列，手工演算合并器如何把一组 lane 请求收敛成 dcache 请求，并理解「连续 vs 跨步」对合并效率的影响。

**操作步骤**（源码阅读 + 数值演算型实践）：

1. 假设参数：`input_size=4`（4 个 lane），`output_size=1`（1 个 dcache 通道），`output_ratio_=4`，`line_size=DCACHE_WORD_SIZE=16` 字节，`LSU_WORD_SIZE=4` 字节。于是 `addr_mask = ~0xF`（低 4 位清零，对齐到 16 字节）。
2. **场景 A（连续访问）**：4 个 lane 的 load 地址分别为 `0x100, 0x104, 0x108, 0x10c`。逐 lane 对齐：`0x100, 0x100, 0x100, 0x100`——全部相等。
3. 对照 [mem_coalescer.cpp:159-225](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mem_coalescer.cpp#L159-L225) 推演：seed=lane0=`0x100`，内层扫描把 lane1/2/3 全并入 `cur_mask={0,1,2,3}`；产出 **1 条** MemReq（addr=`0x100`），一轮排空后 `sent_mask_` 覆盖全部活跃 lane，`ReqIn.pop()`。
4. **场景 B（跨步访问）**：地址改为 `0x100, 0x110, 0x108, 0x10c`（lane1 落到下一个 16 字节 word）。对齐后 lane1=`0x110`，其余=`0x100`。
5. 推演第一轮：seed=lane0=`0x100`，内层扫描 lane2(`0x108`→`0x100`)✓、lane3(`0x10c`→`0x100`)✓、lane1(`0x110`)✗；`cur_mask={0,2,3}`。产出第 1 条 MemReq。第二轮：只剩 lane1，`cur_mask={1}`，产出第 2 条 MemReq。共 **2 条** dcache 请求。

**需要观察的现象**：

- 场景 A：1 条请求覆盖 4 个 lane，`cur_mask.count()==in_req.mask.count()` → `misses` **不增加**。
- 场景 B：第一轮 `cur_mask.count()=3 != 4` → `misses++`；最终 2 条请求才服务完一条 warp load，`VX_CSR_MPM_COALESCER_MISS` 计数变大。

**预期结果**：连续访问被完全合并成 1 个 dcache 请求；跨步访问导致合并不充分、请求数翻倍、coalescer miss 上升。这正是 GPU 编程中「warp 内线程应访问连续地址」的硬件根源。

> 若本地可运行，建议用 `./ci/blackbox.sh --driver=simx --app=demo` 加 `--debug=4` 跑一次，在 trace 里搜索 `mem-req: coalesced=` 行（[mem_coalescer.cpp:260](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mem_coalescer.cpp#L260-L260)），可直接看到 `coalesced=<合并到的 lane 数>, lanes=<用到的通道数>`。该运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么合并器只在 `output_ratio_` 范围内（同一通道组）扫描匹配地址，而不跨通道合并？

**参考答案**：不同输出通道负责地址空间里互不重叠的 word 粒度区域（lane 到通道的映射 `i = o*output_ratio_ + r` 是按地址连续切分的），分属不同通道的 lane 地址对齐后不可能相等，跨通道扫描只会白费比较；并且每个通道每周期独立接受一个请求，跨通道合并会破坏「每通道一请求」的并行结构。

**练习 2**：一条 warp store（4 lane，地址连续）经过合并器，会分配响应 tag 吗？为什么？

**参考答案**：不会。代码 [mem_coalescer.cpp:230](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mem_coalescer.cpp#L230-L230) 的条件是 `!in_req.is_write() || in_is_amo`——普通 store 是 write、非 AMO，两个条件都不满足，故不分配 tag、不需要响应。AMO 虽然也是「写」，但它要返回读旧值（rd），所以例外地分配 tag。

### 4.2 本地内存 LocalMem（共享内存）

#### 4.2.1 概念说明

本地内存（LMEM，Local Memory）对应 CUDA 的 **shared memory**：一块**每核私有**的小容量 SRAM，位于 LSU 旁边，延迟远低于经过整条缓存栈到 DRAM。它的用途是让一个 CTA（线程块）内的线程共享中间数据——先把全局内存的数据搬进 LMEM，线程间即可在 LMEM 上高速交互，最后再写回全局内存。

LMEM 的关键特征：

- **短路**：LSU 的请求若地址落在 LMEM 区（`AddrType::Shared`），由 `lmem_switch` 直接分流到 LMEM，**完全绕过 dcache/L2/L3/DRAM**。
- **bank 交叉**：SRAM 被切成多个 bank，按 word 交叉编址，可每周期并行服务多个不冲突 bank 的请求。
- **单周期语义**：访问时序由 SRAM 的固定延迟建模（RTL 里是带 `OUT_REG=1` 的单周期 SP-RAM），不像 DRAM 那样数据相关。

DXA（DMA 加速器，见 [u9-l2](u9-l2-dxa-async.md)）也能往 LMEM 写数据（多播 DMA 把数据分发到各 cluster 的 LMEM），所以 RTL 的 `VX_local_mem` 还带一个 DMA 端口。

#### 4.2.2 核心流程

```text
地址路由（lmem_switch，每拍）：
  对每条 LsuReq 的每个活跃 lane：
    type = get_addr_type(addrs[i])
    type == Shared → 进 LMEM 分组 (out_lmem_req)
    其他 (Global/IO) → 进 DC 分组 (out_dc_req)   // 交给合并器/dcache
  AMO on Shared → 断言失败（LMEM 不支持原子）
  两组分别经 ReqOutLmem / ReqOutDC 送出

LocalMem 访问（Impl::tick，每拍）：
  对每个 bank：
    取 crossbar 分发到该 bank 的请求
    写：按 byteen 逐字节写入 RAM（byte-enabled write）
    读：从 RAM 读出整条 line → make_mem_block 装入响应
    store 默认不回响应；read 总是回响应（带 line 数据）
  crossbar：Priority 仲裁，按 (addr >> lg2_line_size) & (num_banks-1) 选 bank
```

LMEM 用一个 `MemCrossBar`（优先级仲裁 + bank 交叉）把 `num_reqs` 个请求端口分发到 `2^B` 个 bank，再把各 bank 响应汇聚回各端口。地址先用 `to_local_addr` 截断到本地容量位数内。

#### 4.2.3 源码精读

**地址如何被判为 Shared**。`get_addr_type` 把落在 `[VX_MEM_LMEM_BASE_ADDR, +SIZE)` 区间的地址归为 `Shared`，这就是「近内存」的地址窗口（由 `VX_types.toml` 的 `[memmap]` 段定义，是软硬件共享契约）：

[sim/simx/types.h:853-869](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/types.h#L853-L869) —— `enum class AddrType { Global, Shared, IO }` 与 `get_addr_type`。

`lmem_switch` 据此把一条 LsuReq 拆成两半：Shared lane 走 LMEM，其余走 DC；并对「LMEM 上的 AMO」显式断言，避免静默误路由：

[sim/simx/mem/local_mem_switch.cpp:75-94](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/local_mem_switch.cpp#L75-L94) —— `if (type == AddrType::Shared) { out_lmem_req.mask.set(i); ... } else { out_dc_req.mask.set(i); ... }`，以及 [L78-81](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/local_mem_switch.cpp#L78-L81) 的 AMO 断言。

**LocalMem 的内部结构**。`Impl` 持有一块 `RAM`（容量 = `1 << VX_CFG_LMEM_LOG_SIZE`）、一个 bank 交叉的 `MemCrossBar`（优先级仲裁）。`to_local_addr` 把设备地址截到本地地址位数：

[sim/simx/mem/local_mem.cpp:23-56](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/local_mem.cpp#L23-L56) —— 构造：`ram_(config.capacity)`、`addr_bits_ = log2ceil(config.capacity)`；crossbar 的 bank 选择函数 `(req.addr >> lg2_line_size) & (num_banks-1)`（[L48-50](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/local_mem.cpp#L48-L50)）即「按 line 交叉到 bank」。

实例化参数（每核一个 LMEM，端口数 = LSU 请求数 + TCU + DXA）：

[sim/simx/core.cpp:123-132](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L123-L132) —— `LocalMem::Create(sname, Config{(1<<VX_CFG_LMEM_LOG_SIZE), LSU_WORD_SIZE, lmem_num_reqs, log2ceil(VX_CFG_LMEM_NUM_BANKS), false})`。`lmem_num_reqs = LSU_NUM_REQS + TCU_ENABLED + DXA_ENABLED` 说明 TCU/DXA 也接 LMEM。

**字节级写与 line 级读**。每拍遍历各 bank：写按 `byteen` 逐字节写入（支持 sub-word 写）；读则从 RAM 读出整条 `VX_CFG_MEM_BLOCK_SIZE` 大小的 line 装进响应（TLM 载荷）。store 默认不回响应，除非 `write_reponse` 全局打开或请求带 `MEM_FLAG_STRSP`（store-response）标志：

[sim/simx/mem/local_mem.cpp:64-109](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/local_mem.cpp#L64-L109) —— 关键片段：

```cpp
if (bank_req.is_write() && bank_req.data) {            // byte-enabled 写
  uint64_t line_addr = to_local_addr(bank_req.addr) & ~uint64_t(VX_CFG_MEM_BLOCK_SIZE - 1);
  for (uint32_t b = 0; b < VX_CFG_MEM_BLOCK_SIZE; ++b)
    if (bank_req.byteen & (1ull << b))
      ram_.write(&value, line_addr + b, 1);
}
if (!bank_req.is_write() || config_.write_reponse || bank_req.flags.strsp) {
  MemRsp bank_rsp{bank_req.tag, bank_req.hart_id, bank_req.uuid};
  if (!bank_req.is_write()) {                          // 读：带回 line 数据
    ram_.read(rsp_data->data(), line_addr, VX_CFG_MEM_BLOCK_SIZE);
    bank_rsp.data = rsp_data;
  }
  ...try_send(bank_rsp)...
}
```

**RTL 对应：`VX_local_mem.sv`**。RTL 用 `VX_stream_xbar`（优先级仲裁 `ARBITER="P"`）做请求分发与响应汇聚，每个 bank 是一块 `VX_sp_ram`（`OUT_REG=1`，单周期输出寄存器）。bank 选择取地址低位：

[hw/rtl/mem/VX_local_mem.sv:73-80](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/mem/VX_local_mem.sv#L73-L80) —— `assign req_bank_idx[i] = lsu_bus_if[i].req_data.addr[0 +: BANK_SEL_BITS]`（SimX 的 `(addr >> lg2_line_size) & (num_banks-1)` 在 RTL 体现为取地址的 bank 选择位）。

请求 crossbar：

[hw/rtl/mem/VX_local_mem.sv:123-146](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/mem/VX_local_mem.sv#L123-L146) —— `VX_stream_xbar req_xbar`，`NUM_INPUTS=NUM_REQS, NUM_OUTPUTS=NUM_BANKS`。

**DMA 端口优先级与 RDW 冒险**（RTL 特有细节）。当 `DMA_ENABLE=1`（DXA 用），DMA 端口对每个 bank SRAM 的地址/数据/写使能有**优先级**，LSU 被让道；同时为防止「上周期写、本周期读同地址」的 SRAM 读旧值冒险，设了 `is_rdw_hazard` 停顿：

[hw/rtl/mem/VX_local_mem.sv:233-306](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/mem/VX_local_mem.sv#L233-L306) —— DMA 优先 mux（`bank_sram_addr = dma_active ? ... : per_bank_req_addr[i]`）与 RDW 冒险停顿（`is_rdw_hazard = last_wr_valid && ~per_bank_req_rw[i] && (addr == last_wr_addr)`）。SimX 侧因 RAM 即时读写不建模此 SRAM 冒险，这是 RTL 实现细节。

#### 4.2.4 代码实践

**实践目标**：跟踪一个落在 LMEM 区的 load，看清它如何从 LSU 经 `lmem_switch` 直达 `LocalMem`，完全不碰 dcache。

**操作步骤**（源码阅读型实践）：

1. 在 `VX_types.toml`（或生成的 `VX_types.h`）中找到 `VX_MEM_LMEM_BASE_ADDR` 与 `VX_CFG_LMEM_LOG_SIZE`，记下 LMEM 地址窗口的起点和大小。
2. 读 [local_mem_switch.cpp:66-94](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/local_mem_switch.cpp#L66-L94)，确认 Shared lane 进 `out_lmem_req`、其余进 `out_dc_req`。
3. 读 [core.cpp:147-159](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L147-L159)，确认 `lmem_switch->ReqOutLmem` 经 `lsu_lmem_adapter` 接到 `local_mem_->Inputs`，即 LMEM 请求路径与 dcache 路径是两条独立接线。
4. 读 [local_mem.cpp:74-98](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/local_mem.cpp#L74-L98)，确认 LMEM 读响应直接从 `ram_` 取 line 并 `try_send`，中间没有 cache 查找、没有 MSHR、没有 DRAM 时序。

**需要观察的现象**：LMEM 路径上没有任何 cache 模块参与，访问延迟等于 crossbar + SRAM 的固定拍数。

**预期结果**：你能画出一条「LSU → lmem_switch(Shared 分支) → lsu_lmem_adapter → LocalMem RAM → 响应原路返回」的独立通路，与「LSU → lmem_switch(DC 分支) → 合并器 → dcache → … → DRAM」的缓存通路并行存在、互不干扰。

> 若本地可运行，可在 `tests/regression` 下找一个使用 shared/local memory 的 kernel（或自行用 `vx_shared_*` 类 API），用 `--debug=4` 观察是否出现 `lmem-req` trace 行而不出现对应的 `dc-req` 行，以此验证分流。具体测试名待本地确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `lmem_switch` 要对「AMO on Shared」断言失败，而不是把 AMO 也送进 LMEM？

**参考答案**：LMEM 的 bank SRAM 路径没有原子 read-modify-write 机制（SimX 的 `LocalMem::tick` 只做普通字节写/line 读，RTL 的 `VX_local_mem` 也没有 AMO 单元）。AMO 的正确性依赖 [u11-l2](u11-l2-amo-coherence.md) 讲的 AMO 一致性机制，那套机制挂在缓存栈/LLC 上。若放任 AMO 走 LMEM，会静默丢失原子性，故用断言把这种用法挡掉。

**练习 2**：LMEM 的 bank 选择函数是 `(addr >> lg2_line_size) & (num_banks-1)`。若 4 个线程同时访问地址 `0x0, 0x4, 0x8, 0xc`（`line_size=4`，4 bank），会发生什么？若访问 `0x0, 0x4, 0x8, 0x10` 呢？

**参考答案**：`line_size=4`、4 bank，bank 索引 = `(addr>>2) & 0x3`。前一组地址 `0x0/0x4/0x8/0xc` 的 bank 索引为 `0,1,2,3`——四请求分属四个不同 bank，可一周期并行服务，无冲突（`bank_stalls` 不增）。后一组 `0x0/0x4/0x8/0x10` 的索引为 `0,1,2,0`——lane0 与 lane3 同落 bank0，发生 bank 冲突，crossbar 仲裁只能先服务其一，另一个被记一次 `collisions`（暴露为 `VX_CSR_MPM_LMEM_BANK_ST`）。

### 4.3 DRAM 模型 Memory 与 DramSim（ramulator2）

#### 4.3.1 概念说明

`Memory` 是整条内存梯子的末端：L3 cache miss 后，请求最终来到这里。它要同时回答两个问题——**数据是什么**（读写了哪些字节）和**这次访问要多久**（DRAM 时序）。Vortex 的设计把这两件事干净地解耦：

- **数据通路**：一块简单的后备 `RAM`（字节数组），请求到达时立刻读出/写入一条 line。
- **时序通路**：一个 `DramSim`（封装 ramulator2），按 HBM2 的行缓冲、刷新、控制器调度规则，给出这次访问从发起到可响应的周期数。

二者解耦的关键在于 TLM：读请求在**到达时刻**就把 line 数据从 RAM 捕获进 `shared_ptr`，随请求一起塞进 DramSim；等 DramSim 的回调（表示时序已到）触发时，再把早就备好的数据包成 `MemRsp` 送回。于是「数据早就在手，只是按时序节奏释放」——时序模型只决定「何时回」，不参与「读什么」。

这种「数据与计时分离」的设计让 `Memory` 既能被 simx 用，也能被 gem5 后端复用（`dram_sim.cpp` 位于 `sim/common`，是共享代码），还能通过 `pre_send_hook` 把请求同时转发给 SST 等外部模拟器做遥测。

#### 4.3.2 核心流程

```text
Memory::Impl::tick（每拍）：
  1. dram_sim_.tick()                          // 推进 ramulator2，触发到期回调
  2. 对每个 bank 的 crossbar 输出请求：
       a. 计算 line_addr（对齐到 MEM_BLOCK_SIZE）
       b. 临时关闭 RAM 的 ACL（缓存回填/写回是仿真器内部流量，无内核读写意图）
       c. 写：按 byteen 逐字节写 RAM
          读：make_mem_block + ram_.read 捕获整条 line 到 rsp_data
       d. （可选）调 pre_send_hook(mem_req)     // 遥测，不改数据通路
       e. 把 {request, bank_id, rsp_data} 打包成 DramCallbackArgs
       f. dram_sim_.send_request(addr, is_write, 回调, args)
       g. 弹出 crossbar 该请求

回调（DramSim 计时到期时被反复调用，直到返回 true）：
  - 写：直接 delete args，返回 true（无数据响应）
  - 读：用 args 里早就备好的 rsp_data 组装 MemRsp，
        经 mem_xbar_->RspIn[bank_id].try_send 送回；背压时返回 false（下拍重试）

DramSim::Impl::tick（每拍）：
  cpu_cycles_ += 1000                           // 每 SimX 拍记 1000 个 CPU 周期
  handle_pending_responses()                    // 推进已就绪回调
  while (cpu_cycles_ >= scaled_dram_cycles_):   // clock_ratio 缩放
    handle_pending_requests()                   // 把 pending 请求喂给 ramulator
    ramulator_memorysystem_->tick()             // ramulator2 前进一拍
    cpu_cycles_ -= scaled_dram_cycles_
```

要点：

- **时钟比缩放**：SimX 的 `Memory` 与核心不一定同频。`dram_sim.cpp` 用「每 tick 累加 1000 个 CPU 周期、每 `clock_ratio*1000` 个 CPU 周期推进 ramulator 一拍」实现异步时钟域，`clock_ratio` 由 `MEM_CLOCK_RATIO`（默认 1）控制。
- **通道宽度适配**：CPU 侧的一个请求可能比 DRAM 通道宽（128 位 = 16 字节），`send_request` 会把它拆成多个 16 字节子请求，只有第一个带回调，其余只占时序位。

#### 4.3.3 源码精读

**Memory 顶层结构与 crossbar**。`Impl` 持有 `MemCrossBar`（**RoundRobin** 轮转仲裁，与 LMEM 的 Priority 不同）与 `DramSim`。crossbar 同样按 bank 交叉，但粒度是 `block_size`（= `VX_CFG_MEM_BLOCK_SIZE`，一条 cache line）：

[sim/simx/mem/memory.cpp:32-66](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/memory.cpp#L32-L66) —— 构造：`dram_sim_(config.num_banks, config.block_size, config.clock_ratio)`，crossbar bank 选择 `(req.addr >> lg2_block_size) & (num_banks-1)`（[L58-61](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/memory.cpp#L58-L61)）。`Memory::Create` 的实参见 [processor.cpp:47-52](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/processor.cpp#L47-L52)：`num_banks=VX_CFG_PLATFORM_MEMORY_NUM_BANKS, num_ports=VX_CFG_L3_MEM_PORTS, block_size=VX_CFG_MEM_BLOCK_SIZE, clock_ratio=MEM_CLOCK_RATIO`。

**数据与计时分离的核心**。请求到达时立刻读写 RAM（关闭 ACL，因为缓存回填是仿真器内部流量、无内核意图），读则把 line 捕获进 `rsp_data`；然后把这个 `rsp_data` 连同请求塞进 DramSim，**等回调才发响应**：

[sim/simx/mem/memory.cpp:79-148](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/memory.cpp#L79-L148) —— 关键片段：

```cpp
ram_->enable_acl(false);                                  // 旁路 ACL
if (mem_req.is_write()) {
  for (b ...) if (mem_req.byteen & (1ull<<b)) ram_->write(...);   // 字节写
} else {
  rsp_data = make_mem_block();
  ram_.read(rsp_data->data(), line_addr, VX_CFG_MEM_BLOCK_SIZE); // 立刻捕获 line
}
ram_->enable_acl(true);
...
auto req_args = new DramCallbackArgs{this, mem_req, i, rsp_data};
dram_sim_.send_request(mem_req.addr, mem_req.is_write(),
  [](void* arg)->bool {                                   // 计时到期回调
    if (rsp_args->request.is_write()) { delete rsp_args; return true; }
    MemRsp mem_rsp{..., rsp_args->rsp_data};              // 用早就备好的数据
    if (RspIn.at(bank_id).try_send(mem_rsp)) { delete rsp_args; return true; }
    return false;                                         // 背压，下拍重试
  }, req_args);
```

注意 `rsp_data` 是在**请求到达拍**捕获的，回调只是「到点了把它发出去」。`pre_send_hook_`（[L117-119](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/memory.cpp#L117-L119)、[memory.h:51-60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/memory.h#L51-L60)）是可选的 SST 遥测钩子，不影响数据通路。

**DramSim：ramulator2 的 HBM2 配置**。用 YAML 内联配置一颗 HBM2：`HBM2_8Gb` 密度、`num_channels` 通道、`HBM2_2Gbps` 时序、`Generic` 控制器 + `FRFCFS` 调度器（先就绪先服务）+ `AllBank` 刷新 + `OpenRowPolicy` 行策略 + `RoBaRaCoCh`（Row-Bank-Rank-Column-Channel）地址映射：

[sim/common/dram_sim.cpp:84-118](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/dram_sim.cpp#L84-L118) —— YAML 配置；`dram_config["MemorySystem"]["DRAM"]["org"]["channel"] = num_channels`（[L92](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/dram_sim.cpp#L92-L92)），地址映射 `RoBaRaCoCh`（[L108](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/dram_sim.cpp#L108-L108)）。`DRAM_TRACE` 环境变量可打开逐命令 trace 录制（默认关，因开销巨大，见 [L98-107](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/dram_sim.cpp#L98-L107) 的注释）。

**时钟比缩放**。每 SimX 拍累加 1000 个 CPU 周期；`scaled_dram_cycles_ = clock_ratio * 1000`；每消耗掉这么多 CPU 周期就让 ramulator 前进一拍并尝试喂入 pending 请求：

[sim/common/dram_sim.cpp:135-143](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/dram_sim.cpp#L135-L143) —— `cpu_cycles_ += tick_cycles_`；`while (cpu_cycles_ >= scaled_dram_cycles_) { handle_pending_requests(); ramulator_memorysystem_->tick(); cpu_cycles_ -= scaled_dram_cycles_; }`。

**通道宽度拆分**。CPU 侧 `cpu_channel_size_`（= `block_size` = 一条 line）可能大于 DRAM 通道宽 `dram_channel_size_=16`（128 位）。`send_request` 把一个宽请求拆成多个 16 字节子请求，仅第一个携带回调，其余 `callback=nullptr`（只占时序、无响应）：

[sim/common/dram_sim.cpp:145-163](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/dram_sim.cpp#L145-L163) —— `if (cpu_channel_size_ > dram_channel_size_) { n = ...; for i: pending_reqs_.push({addr, is_write, i==0?cb:nullptr, ...}); }`。Ramulator 本身不处理写响应，故写请求的回调由 DramSim 自行推进（[handle_pending_requests L75-78](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/dram_sim.cpp#L75-L78)）。

#### 4.3.4 代码实践

**实践目标**：说明 `memory.cpp` 如何用 ramulator2 提供时序，并验证「数据在请求到达时捕获、时序到期才释放」这一解耦设计。

**操作步骤**（源码阅读型实践）：

1. 读 [memory.cpp:88-115](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/memory.cpp#L88-L115)，找出 RAM 读写的位置，确认它发生在 `dram_sim_.send_request` **之前**（数据先就绪）。
2. 读 [memory.cpp:122-143](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/memory.cpp#L122-L143)，确认传给 `dram_sim_.send_request` 的回调才是「发响应」的动作，且回调里用的是 `req_args->rsp_data`（请求到达时捕获的那份），而非回调触发时再读 RAM。
3. 读 [dram_sim.cpp:84-118](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/dram_sim.cpp#L84-L118)，列出 ramulator2 的关键配置项（DRAM impl=HBM2、调度=FRFCFS、行策略=OpenRowPolicy、地址映射=RoBaRaCoCh）。
4. 读 [dram_sim.cpp:135-143](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/dram_sim.cpp#L135-L143)，解释时钟比缩放：若 `clock_ratio=1`，则每 SimX 拍 ramulator 前进一拍；若 `clock_ratio=2`（DRAM 频率减半），则每两拍 ramulator 才前进一拍，访问延迟显式变大。

**需要观察的现象**：把 RAM 想象成「立即返回正确字节」的组合逻辑，把 DramSim 想象成「计时器」——响应的 *内容* 由 RAM 决定，响应的 *时刻* 由 DramSim 决定，二者互不影响。

**预期结果**：你能用一句话说清「memory.cpp 如何用 ramulator2 提供时序」——*请求到达时 RAM 立刻产出 line 数据并随请求进入 DramSim 计时队列；ramulator2 按 HBM2 时序推进，计时到期后触发回调，把早已备好的 line 包成 MemRsp 送回*。

> 若本地可运行，可在 `ci/blackbox.sh` 跑 sgemm 时设环境变量 `DRAM_TRACE=./trace/ram.log`（见 [dram_sim.cpp:102-107](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/dram_sim.cpp#L102-L107)），打开逐命令 DRAM trace，观察行命中/行冲突命令序列；再用 `--perf=1` 读 `VX_CSR_MPM_MEM_LT`（内存延迟）与 `VX_CSR_MPM_MEM_BANK_ST`（bank 停顿）印证时序模型生效。该运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Memory::tick` 在读写 RAM 前要 `enable_acl(false)`、之后又 `enable_acl(true)`？

**参考答案**：ACL（访问控制）用于守护主机 upload/download 的合法区域。但缓存回填/写回是仿真器内部流量，不携带内核的读写意图——例如写回式缓存在 write-miss 时会为了填充 line 而去读一个「只写」缓冲（[memory.cpp:91-96](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/memory.cpp#L91-L96) 注释），内存总线无法区分这种越权读。故在内部流量期间临时关掉 ACL，结束后恢复——ACL 仍守护着 upload/download 入口。

**练习 2**：`Memory` 的 crossbar 用 RoundRobin 仲裁，而 `LocalMem` 用 Priority 仲裁。这种差异合理吗？为什么？

**参考答案**：合理。LMEM 是每核私有、端口请求者地位对等（各 LSU lane/TCU/DXA），用轮转（RoundRobin）或优先级差异不大，但 LMEM 选 Priority 是为了让请求尽快定序、且其性能影响可忽略（RTL 注释 [VX_local_mem.sv:364](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/mem/VX_local_mem.sv#L364-L364) 明说「negligible impact」）。`Memory` 面向全芯片多个 cluster 的 L3 流量汇聚，公平性更重要，故用 RoundRobin 防止某端口饿死。两者都是 [u7-l1](u7-l1-rtl-top.md) 总结的仲裁通则的具体应用：对等请求者用轮转 R。

## 5. 综合实践

把三个最小模块串起来，追踪一条「warp 连续 load」从 LSU 到 DRAM 再回来的完整旅程，并解释每一步如何影响带宽与延迟。

**任务**：选定一个矩阵乘法 kernel（如 `tests/regression/sgemm`），假设一个 warp 的 4 个线程分别读取矩阵 B 的连续 4 个元素（地址 `0x1000, 0x1004, 0x1008, 0x100c`，全局内存，32 位 float）。完成下列分析与一张全程数据流图：

1. **合并器层**：推演这 4 个地址在 `MemCoalescer`（设 `DCACHE_WORD_SIZE=16`）里如何被合并。给出合并后产出的 dcache 请求数、`cur_mask`、是否触发 `coalescer miss`。再设想线程改为跨步读取（stride=16 字节，地址 `0x1000, 0x1010, 0x1020, 0x1030`），重做推演并对比请求数。
2. **缓存层**（承接 [u8-l2](u8-l2-cache-internals.md)）：合并后的请求进 dcache。若 miss，描述 MSHR 如何合并同 set/tag 的 miss、fill forwarding 如何避免重复取，最终如何向 L2/L3 逐级未命中。
3. **DRAM 层**：L3 也 miss 后请求落到 `Memory`。说明 RAM 在请求到达时读出 line、DramSim 按 HBM2 时序排队；画出「数据在到达拍就绪、计时到期才经回调释放」的时间线。指出 `clock_ratio` 与 `FRFCFS` 调度器如何影响这条 load 的可观察延迟。
4. **LMEM 对照**：若程序员把这 4 个 float 先用一次连续 load 取进 LMEM（shared memory），后续 warp 内交互改在 LMEM 上进行，画出新路径（`lmem_switch` 的 Shared 分支直达 `LocalMem` SRAM），并解释为何这条路径不再付出 DRAM 延迟、bank 冲突条件又是什么。

**交付物**：

- 一张「连续访问 vs 跨步访问」的请求数对照表（合并器出口请求数、coalescer miss、最终 DRAM 访问次数）。
- 一张全局内存路径的时序时间线（请求到达 RAM 拍 → DramSim 计时 → 响应回 LSU）。
- 一张 LMEM 短路路径图，标注与全局路径的分叉点（`lmem_switch`）。

> 这是源码阅读 + 数值推演型实践，无需运行即可完成；若本地可运行 sgemm，可用 `--perf=1` 读 `VX_CSR_MPM_COALESCER_MISS`、`VX_CSR_MPM_MEM_LT`、`VX_CSR_MPM_LMEM_READS` 三类计数器，与你推演的定性结论互相印证（运行结果待本地验证）。

## 6. 本讲小结

- **合并器是带宽杠杆**：`MemCoalescer` 把同 word 的 lane 请求折叠成更少的 dcache 请求，连续访问一次合并干净、跨步访问合并不充分（`VX_CSR_MPM_COALESCER_MISS` 上升）。它只在「多 lane 且 dcache word 比 lane 宽」时启用，否则被直通适配器旁路。
- **合并的细节纪律**：合并不跨通道；一条 LsuReq 可能要多个「构建拍/排空拍」轮次；AMO 刻意不合并且保留字节级地址；响应侧用 `shared_ptr` 别名零拷贝地把 line 展开回 lane。
- **LMEM 是短路近内存**：地址落在 `Shared` 区的请求由 `lmem_switch` 直接分流到 `LocalMem`，绕过整条缓存栈；bank 交叉的 SRAM 提供单周期语义；RTL 里 DMA（DXA）端口对 bank SRAM 有优先级，并有 RDW 冒险保护。
- **DRAM 数据与计时解耦**：`Memory` 在请求到达时即用 RAM 产出 line，把数据随请求塞进 `DramSim` 计时队列；ramulator2（HBM2/FRFCFS）只决定响应「何时」发，不参与「读什么」。
- **时钟比与通道拆分**：`dram_sim.cpp` 用「每拍累加 1000 周期、按 `clock_ratio` 缩放推进 ramulator」实现异步时钟域，并把宽 CPU 请求拆成多个 16 字节 DRAM 通道子请求。
- **parity 与契约**：合并器、LMEM、DRAM 都在 SimX 与 RTL 两侧有对应实现，共享 `VX_types.toml` 的地址布局（`VX_MEM_LMEM_BASE_ADDR` 等），是 model_parity 在内存子系统末端的体现。

## 7. 下一步学习建议

- 想看请求在落到 DRAM *之前* 如何被缓存栈逐级处理与合并未命中，继续读 [u8-l2（缓存标签、MSHR、替换与数据通路）](u8-l2-cache-internals.md)，重点对照 MSHR 的链式合并与本讲合并器的 lane 合并之异同。
- 想了解「DXA 多播 DMA 把数据分发到各 cluster 的 LMEM」这一 LMEM DMA 端口的真正使用者，进入 [u9-l2（DXA 异步拷贝与多播）](u9-l2-dxa-async.md)，理解 `VX_local_mem.sv` 的 `DMA_ENABLE` 端口为何要有优先级与响应保持寄存器。
- 想看 LSU 如何在 AGU 阶段算出本讲合并器消费的那组 lane 地址、packed load 又如何展开，进入 [u8-l4（LSU 流水线设计）](u8-l4-lsu-pipeline.md)。
- 对 DRAM 之上的虚拟地址翻译与 TLB miss 如何也走这条内存通路感兴趣，进入 [u11-l1（虚拟内存子系统）](u11-l1-virtual-memory.md)，看 PTW 发出的 PTE 取如何与普通 load 共用 cache 层次。
```
