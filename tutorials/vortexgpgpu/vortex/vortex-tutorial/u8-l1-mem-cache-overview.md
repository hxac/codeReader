# 内存层次与缓存子系统总览

## 1. 本讲目标

学完本讲，你应该能够：

- 画出 Vortex 从 LSU（访存单元）到 DRAM 的完整全局内存层次，并标注每一级的共享范围。
- 说清楚「socket 共享 L1、cluster 共享 L2、全局共享 L3」这条共享边界规则从何而来。
- 区分两条截然不同的访存路径：**全局内存**（走 L1→L2→L3→DRAM 的缓存栈）与**本地内存**（绕过缓存、直达每核 SRAM 的共享内存）。
- 读懂 SimX 中各级缓存的实例化代码（`socket.cpp` / `cluster.cpp` / `processor.cpp` / `cache_cluster.cpp`），并理解「禁用某级缓存 = 透明旁路」这一可选性机制。
- 对照 `VX_config.toml` 找到控制缓存容量、相联度、MSHR、是否使能的配置开关。

## 2. 前置知识

在进入本讲前，你需要建立以下直觉（若不熟悉，可先回顾 u5-l1、u5-l2、u6-l4）：

- **缓存（cache）是什么**：一片比主存（DRAM）更小、更快、更靠近计算单元的 SRAM。CPU/GPU 访存时先查缓存，命中就直接用；不命中（miss）就向下一级要数据。缓存用「行（line）」为单位管理数据。
- **MSHR（Miss Status Holding Register）**：当缓存未命中时，用来记录「这个未命中请求正在等谁」的小表。有了它，缓存就可以在等一个 miss 的同时继续服务其他请求（非阻塞），这正是 Vortex 隐藏内存延迟的关键。
- **SIMT 与 warp**：Vortex 每周期发射一个 warp（一组线程），warp 内线程共享 PC。一个 warp 的一次 load 可能产生多个访存请求，需要被**合并（coalesce）**成更少的缓存行请求。
- **SimX 是 RTL 的预言机**：Vortex 用 C++ 写的周期精确仿真器 SimX 与 Verilog RTL 两套实现必须保持功能与时序一致（model_parity）。本讲引用的 SimX 代码，在 RTL 中都有逐级对应的模块。
- **VX_config.toml 是唯一真相来源**：所有缓存几何参数都集中在这个 TOML 文件里，由 `gen_config.py` 分发到各层（回顾 u2-l1、u2-l2）。

一句话总结：本讲讨论的是「warp 发出一条访存指令后，数据沿怎样一条梯子爬回运算单元」。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [docs/designs/cache_subsystem.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/cache_subsystem.md) | 缓存子系统的权威设计文档：核心性质、几何参数（line/sector/word）、各级配置开关、调优建议。 |
| [docs/designs/microarchitecture.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md) | 微架构总览，其末尾的「Clustering architecture / Cache Subsystem」段定义了 socket/cluster 与 L1/L2 的共享关系。 |
| [sim/simx/mem/cache_cluster.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/cache_cluster.cpp) | SimX 的「缓存簇」封装：把多个 `Cache` 实例 + 输入/输出仲裁器打包，是 icache/dcache 的实例化载体，也是「禁用即旁路」的实现点。 |
| [sim/simx/socket.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/socket.cpp) | socket 层：实例化共享的 icache 与 dcache（L1），并把二者访存流量汇聚后向 L2 输出。 |
| [sim/simx/cluster.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/cluster.cpp) | cluster 层：实例化单一 L2 缓存，把本 cluster 内所有 socket 的流量扇入 L2。 |
| [sim/simx/processor.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/processor.cpp) | 处理器顶层：实例化全局唯一的 L3 缓存与唯一的 DRAM（`Memory`），并把所有 cluster 扇入 L3。 |
| [sim/simx/mem/local_mem.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/local_mem.cpp) | 本地内存（共享内存）的 SimX 实现：带交叉开关的多 bank SRAM，**不走缓存栈**。 |
| [sim/simx/core.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp) | core 层：实例化每核独占的 `LocalMem`，并展示 LSU 如何经 `lmem_switch` 在「本地内存」与「全局缓存」两条路径间分流。 |
| [VX_config.toml](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml) | 所有缓存几何参数与使能开关的真相来源。 |

---

## 4. 核心概念与源码讲解

### 4.1 缓存子系统的核心性质与几何参数

#### 4.1.1 概念说明

Vortex 的缓存不是「一块单一的 SRAM」，而是一套**可配置、多 bank、非阻塞**的缓存模板。同一份缓存 RTL/SimX 模型，用不同的配置参数实例化出 icache、dcache、L2、L3 四种角色。理解本讲的第一步，是先抓住这份模板的六条核心性质，它们决定了整条内存梯子的「形状」。

这六条性质也是后续讲义（u8-l2 缓存内部、u8-l3 访存合并、u8-l4 LSU 流水线）的纲领。

#### 4.1.2 核心流程

缓存子系统的六条主要性质（直接对应设计文档开头）：

1. **多 bank 并行**：一个缓存内部拆成多个 bank，可并行服务多个请求，提供高带宽。
2. **非阻塞流水线 + 每 bank MSHR + fill forwarding**：miss 不阻塞后续请求；每个 bank 有自己的 MSHR 跟踪未命中；数据从下级返回时直接喂给等待者（fill forwarding），省掉「先写回数组再读出」的一来回。
3. **可配置**：dcache、icache、L2、L3 都可选，按需实例化。
4. **写透（write-through）或写回（write-back）**：按该级在**一致性角色**中的位置自动决定（见 4.4）。
5. **末级缓存采用分段行（sectored lines）**：解耦 tag 粒度与填充粒度。
6. **原子操作（AMO）在末级缓存（LLC）执行**。

每级缓存解耦三种粒度（这是理解调优表的基础）：

| 粒度 | 含义 | 作用 |
|---|---|---|
| **Line（行）** `LINE_SIZE` | tag 粒度，一个 tag 覆盖一行 | bank 间按行交织（整行落在一个 bank） |
| **Sector（段）** `SECTOR_SIZE` | 填充/驱逐/内存事务粒度 | 一行含 `LINE/SECTOR` 个段，各有自己的 valid/dirty 位 |
| **Word（字）** `WORD_SIZE` | 合并器输出/单请求访问粒度 | 决定请求端口数与 bank 数 `NUM_REQS = footprint / WORD` |

L2 与 L3 是分段的：行大小 = `2 × MEM_BLOCK`（减半 tag 数量），段大小 = `MEM_BLOCK`（内存总线事务大小）。icache/dcache 不分段：`LINE = SECTOR = MEM_BLOCK`。

#### 4.1.3 源码精读

缓存子系统的六条核心性质定义在设计文档开头：

[docs/designs/cache_subsystem.md:3-10](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/cache_subsystem.md#L3-L10) —— 上面列出的六条性质正是这一段的翻译，注意第 4 条「write-through or write-back, selected per level by coherence role」点明了写策略不是随便选的，而是由一致性角色推导。

三种粒度的权威定义：

[docs/designs/cache_subsystem.md:16-31](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/cache_subsystem.md#L16-L31) —— 这一段说明 L2/L3 为什么分段：行翻倍以减半 tag 数（省 BRAM、改善时序），段仍保持总线粒度；而 L1 不分段以降低 miss 填充代价。

对应的 SimX `Cache::Config` 结构体把上述几何参数一一映射成字段：

[sim/simx/mem/cache.h:29-46](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/cache.h#L29-L46) —— 注意 `C/L/S/W/A/B` 六个 `log2` 字段分别对应容量、行、段、字、相联度、bank 数；`bypass` 字段是实现「禁用即旁路」的开关（见 4.4）；`is_llc` 标记是否末级，决定 AMO 在哪里执行。

#### 4.1.4 代码实践

**实践目标**：把抽象的「三种粒度」落到具体数字上。

**操作步骤**：
1. 打开 `VX_config.toml`，找到 `VX_CFG_MEM_BLOCK_SIZE`（第 58 行，默认 64 字节）。
2. 找到 `VX_CFG_L2_LINE_SIZE` 与 `VX_CFG_L2_SECTOR_SIZE`（第 66-67 行），它们是表达式：启用 L2 时，行 = `2×64 = 128B`，段 = `64B`，即每行含 2 个段。
3. 对照 `VX_CFG_DCACHE_LINE_SIZE` / `VX_CFG_DCACHE_SECTOR_SIZE`（第 188-189 行），二者都等于 `L1_LINE_SIZE = MEM_BLOCK = 64B`，即 L1 不分段。

**预期结果**：你能说清「L2 一个 tag 覆盖 128B、却按 64B 一段向下级取数据；L1 一个 tag 就只管 64B」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 L2/L3 选择分段（行大于段），而 L1 不分段？

**参考答案**：L2/L3 容量大（1MB/2MB），tag 数量是 BRAM 与时序的主要压力来源；把行翻倍可减半 tag 数。但实际访存往往只命中一段（如跨步访问），按段填充可省一半填充带宽。L1 容量小（16KB）、离运算单元最近，miss 填充代价对延迟敏感，故保持行=段，简单且填充代价低。

**练习 2**：`Cache::Config` 里 `C` 字段存的是容量本身还是其 log2？

**参考答案**：存的是 `log2(容量)`。例如 dcache 16KB 对应 `C = log2ceil(16384) = 14`（见 socket.cpp 第 57 行）。这样硬件/SimX 可以直接用位运算切片出 tag/index/offset。

---

### 4.2 内存层次与共享边界

#### 4.2.1 概念说明

Vortex 的全局内存是一条**四级梯子**：LSU → L1（icache/dcache）→ L2 → L3 → DRAM。梯子的每一级挂在不同层次的聚类上，于是「共享范围」逐级扩大。理解这条梯子的关键不是记忆四个名字，而是掌握一条贯穿全栈的规则——**层次即共享边界**：缓存实例化在哪一层聚类，就被该层内的所有核心共享。

这条规则直接来自 Vortex 的聚类架构：socket 把多个 core 组在一起共享 L1，cluster 把多个 socket 组在一起共享 L2。

#### 4.2.2 核心流程

完整的全局内存路径（自底向上的共享范围）：

```
DRAM (全局唯一)                    ← 全 GPU 共享
  ↑
L3 cache (全局唯一，每处理器一个)   ← 所有 cluster 共享
  ↑
L2 cache (每 cluster 一个)          ← cluster 内所有 socket/core 共享
  ↑
L1 icache + dcache (每 socket 一组) ← socket 内最多 4 个 core 共享
  ↑
LSU / mem_coalescer (每 core)       ← 单 core 私有
```

关键派生量（回顾 u5-l2、u7-l1）：

\[ \text{NUM\_SOCKETS} = \lceil \text{NUM\_CORES} / \text{SOCKET\_SIZE} \rceil \]

L1 实例数 `NUM_ICACHES = NUM_DCACHES = ceil(SOCKET_SIZE/4)`，即一个 socket 内最多 4 个 core 共享一组 L1。

共享边界的设计意义：
- **L2 是多核配置的必需品**：它是第一个「不同 core 的 store 互相可见」的汇聚点。没有 L2（也没有 L3），多个 core 的写就无法对彼此可见。
- **L3 是多 cluster 一致性的必需品**：多 cluster 时同理，需要一个全局汇聚点。

> 注意：上面这条梯子是**全局内存（global memory）**路径。CUDA 语义里的「共享内存（shared memory）」在 Vortex 中叫**本地内存（local memory / LMEM）**，它走的是另一条完全旁路的路径，见 4.4。

#### 4.2.3 源码精读

共享边界的权威定义在微架构文档的聚类段：

[docs/designs/microarchitecture.md:78-85](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L78-L85) —— 「Sockets: Grouping multiple cores sharing L1 cache」「Clusters: Grouping of sockets sharing L2 cache」这两句就是 socket/cluster 与 L1/L2 共享关系的源头。

设计文档里「Hierarchy enables」一表进一步说明 L2/L3 不只是容量扩展，更是「互相可见性」的汇聚点：

[docs/designs/cache_subsystem.md:119-128](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/cache_subsystem.md#L119-L128) —— 注意 L2 一栏的备注「**Required for multi-core configurations** — it is the first shared point where stores from different cores become mutually visible」，这解释了为什么多核必开 L2。

#### 4.2.4 代码实践

**实践目标**：用源码验证「socket 共享 L1、cluster 共享 L2」不是文档空话，而是实例化代码的真实结构。

**操作步骤**：
1. 打开 `sim/simx/socket.cpp`，看其构造函数（第 33-51 行）：icache 与 dcache 都是用 `cores_per_socket` 作为输入数创建的 `CacheCluster`，说明这一组 L1 被 socket 内所有 core 共享。
2. 打开 `sim/simx/cluster.cpp`，看第 70-91 行：L2 是**单一** `Cache::Create`（不是 CacheCluster），其 `core_req_in` 接收所有 socket 的流量（第 125-130 行直接把每个 socket 的 `mem_req_out` 绑到 L2 的 core 请求口）。
3. 打开 `sim/simx/processor.cpp`，看第 104-109 行：所有 cluster 的 `mem_req_out` 都绑到**唯一**的 L3 上。

**预期结果**：你能在三份文件里分别指认「L1 在 socket、L2 在 cluster、L3 在 processor」的实例化位置，确认共享范围随层次递增。

#### 4.2.5 小练习与答案

**练习 1**：默认配置下（`NUM_CORES` 通常很小、L2/L3 默认关闭），dcache 是 LLC（末级缓存）。为什么？

**参考答案**：默认 `VX_CFG_L2_ENABLE = false`、`VX_CFG_L3_ENABLE = false`。根据 4.4 的 `dcache_is_llc` 表达式（`L2 未使能 且 L3 未使能`），dcache 就是缓存栈的最末级，所有未命中直接进 DRAM，AMO 也在 dcache 执行。

**练习 2**：为什么说「多核场景必须开 L2」？用一个 store 可见性的例子说明。

**参考答案**：若 core A 与 core B 各有私有 dcache 且无共享 L2/L3，A 写一个地址只更新自己的 dcache（写透则直达 DRAM，但 B 的 dcache 仍持有旧副本且无人通知）。L2 作为第一个共享汇聚点，保证 A 的 store 到达 L2 后对 B 可见（配合写透策略让 store 穿透到 L2）。所以 L2 是多核可见性的必需品。

---

### 4.3 SimX 中各级缓存的实例化与连接

#### 4.3.1 概念说明

4.2 讲了「挂在哪一层」，本模块讲「具体怎么挂」。SimX 用三种构建块组装整条梯子：

- **`Cache`**：单个缓存实例（含 bank、MSHR、tag store 等）。
- **`CacheCluster`**：把若干个 `Cache` + 输入/输出仲裁器打包成一个「缓存簇」，用于 L1（icache/dcache）。
- **`MemArbiter`**：内存仲裁器，把多路请求汇成一路（或反之）。

L1 用 `CacheCluster`（多实例 + 仲裁），L2/L3 用单个 `Cache`（因为每层只有一个）。所有跨层连接都用 `bind()` 把上游的 `mem_req_out` 接到下游的 `core_req_in`——这正是 u5-l3「基数规则」的体现：模块只通过 channel 通信。

#### 4.3.2 核心流程

自顶向下的连接流水：

```
core.icache_req_out ──► icaches_ (CacheCluster) ──┐
core.dcache_req_out ──► dcaches_ (CacheCluster) ──┤
                                                   ▼
                                    socket 内 L1 仲裁器 (l1_arb)
                                                   │
                              socket.mem_req_out ◄─┘
                                                   │
                              (可选 l2arb：sockets + 各扩展 cache)
                                                   ▼
                                       cluster.l2cache_ (单个 Cache)
                                                   │
                              cluster.mem_req_out ◄┘
                                                   ▼
                                      processor.l3cache_ (单个 Cache)
                                                   │
                                                   ▼
                                      memsim_ (Memory / DRAM)
```

`CacheCluster` 内部做的事（以 L1 为例）：对每个 input 创建一个输入仲裁器（轮转）把 `num_inputs` 个 core 的请求汇到 `num_units` 个 Cache；对每个 memory port 创建一个输出仲裁器把各 Cache 的内存请求汇出。当 `num_units == 0`（即该级缓存被禁用，`NUM_ICACHES=0`）时，它把自己标记为 `bypass`，变成透明转发。

#### 4.3.3 源码精读

**L1（socket 层）**：icache 与 dcache 都用 `CacheCluster::Create` 实例化，注意 dcache 的 `is_llc` 字段是一个表达式：

[sim/simx/socket.cpp:33-72](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/socket.cpp#L33-L72) —— 第 71 行 `(VX_CFG_DCACHE_ENABLED != 0) && (VX_CFG_L2_ENABLED == 0) && (VX_CFG_L3_ENABLED == 0)` 正是「dcache 是 LLC 当且仅当 L2/L3 都关闭」的代码化；第 34、55 行用 `cores_per_socket` 作输入数，体现 socket 内共享。

socket 把 icache 与 dcache 的内存侧经一个 L1 仲裁器汇出：

[sim/simx/socket.cpp:75-102](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/socket.cpp#L75-L102) —— 这是「icache 与 dcache 共享通往 L2 的端口」的扇出/扇入点。

**L1→core 的回连**：

[sim/simx/socket.cpp:111-120](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/socket.cpp#L111-L120) —— 每个 core 的 `icache_req_out` / `dcache_req_out` 绑到对应 CacheCluster 的 `core_req_in`，构成请求/响应闭环。

**L2（cluster 层）**：单一 `Cache`，把所有 socket 扇入：

[sim/simx/cluster.cpp:70-98](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/cluster.cpp#L70-L98) —— 第 74 行 `Cache::Create`（单实例）；第 90 行 `is_llc = (L2 使能 且 L3 未使能)`；第 95-98 行把 L2 的内存侧绑到 cluster 对外的 `mem_req_out`。

无扩展时，socket 直接接到 L2（不经额外仲裁器）：

[sim/simx/cluster.cpp:123-131](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/cluster.cpp#L123-L131) —— 每个 socket 的 `mem_req_out` 直接 `bind` 到 L2 的 `core_req_in`，证明 L2 被 cluster 内所有 socket 共享。

**L3 + DRAM（processor 层）**：

[sim/simx/processor.cpp:46-52](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/processor.cpp#L46-L52) —— 创建唯一的 DRAM 仿真器 `memsim_`，bank 数取 `PLATFORM_MEMORY_NUM_BANKS`（默认 2），与 L3 的内存口数 `L3_MEM_PORTS` 对齐。

[sim/simx/processor.cpp:62-82](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/processor.cpp#L62-L82) —— 创建唯一 L3，注释第 62-64 行明确「L3 使能时是 LLC，否则是透明旁路仲裁器，由 L2 或 L1 担任 LLC」；`bypass = !VX_CFG_L3_ENABLED`。

L3 的扇入（cluster→L3）与扇出（L3→DRAM）：

[sim/simx/processor.cpp:103-115](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/processor.cpp#L103-L115) —— 所有 cluster 绑到 L3 的 `core_req_in`，L3 的 `mem_req_out` 绑到 `memsim_`。

**CacheCluster 的内部结构与旁路逻辑**：

[sim/simx/mem/cache_cluster.cpp:18-56](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/cache_cluster.cpp#L18-L56) —— 第 30-34 行：当 `num_units == 0` 时强制 `num_units = 1` 并把 `cache_config2.bypass = true`，于是这一级变成一个不缓存的透明转发器；第 39-56 行创建输入/输出仲裁器。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 dcache miss 的下游路径，看清它如何穿过多级仲裁到达 DRAM。

**操作步骤**（源码阅读型）：
1. 从 `socket.cpp` 第 55 行的 `dcaches_` 出发，沿 `mem_req_out` 找到第 78-102 行的 L1 仲裁器，确认 dcache 请求被汇出为 `socket.mem_req_out`。
2. 跳到 `cluster.cpp` 第 125-130 行，确认 `socket.mem_req_out` 接到 `l2cache_->core_req_in`。
3. 跳到 `processor.cpp` 第 104-109 行，确认 `cluster.mem_req_out` 接到 `l3cache_->core_req_in`，第 112-115 行确认 L3 接到 `memsim_`。

**预期结果**：你能在三份文件里画出一条从 dcache 到 DRAM 的连续 channel 链，且每一跳都标注了 `bind()` 的行号。**待本地验证**：若手边有可运行的 SimX 构建，可用 `--debug=3` 跑一个简单程序，在 trace 里找到同一条 cache line 的请求依次出现在 dcache、L2、L3、memsim 的日志中。

#### 4.3.5 小练习与答案

**练习 1**：为什么 L1 用 `CacheCluster`（多实例 + 仲裁），而 L2/L3 用单个 `Cache`？

**参考答案**：L1 在 socket 内可能由多个实例共享（`NUM_ICACHES/NUM_DCACHES` 可达 `SOCKET_SIZE/4`），且要接受多个 core 的请求，需要「多实例 + 多输入仲裁」的簇结构。L2（每 cluster 一个）与 L3（全局一个）本身就是单一缓存实例，只需把上游多路请求直接绑到它的多个 `core_req_in` 口即可，无需簇封装。

**练习 2**：`CacheCluster` 在 `num_units == 0` 时如何行为？为什么需要这种行为？

**参考答案**：它把自己设为 `bypass`，保留 channel 接线但不再缓存，请求透明穿透到下一级。这是「禁用某级缓存」的统一实现：上游无需感知某级是否存在，channel 拓扑不变，只是中间多了一个不命中的转发器，便于按需开启/关闭缓存级而不重写连线。

---

### 4.4 本地内存（共享内存）与缓存可选性配置

#### 4.4.1 概念说明

Vortex 有两类截然不同的片上存储，初学者极易混淆：

| 概念 | Vortex 术语 | CUDA 对应 | 位置 | 是否走缓存 |
|---|---|---|---|---|
| 全局内存 | global memory | global memory | DRAM | 是（L1→L2→L3→DRAM） |
| 本地内存 / 共享内存 | local memory (LMEM) | shared memory | 每核私有 SRAM | **否**（旁路直达） |

**本地内存（LMEM）是每核独占的一块 SRAM**（默认 16KB，`LMEM_LOG_SIZE=14`），由 LSU 按地址译码直接访问，**完全绕过缓存栈**。它低延迟、高带宽，用于 warp 内/CTA 内线程交换中间数据。这是本讲必须强调的第二个重点：4.2 那条梯子只描述了全局内存路径，LMEM 是并行的另一条路。

本模块还讲清「缓存可选性」：每一级缓存都能在 `VX_config.toml` 里开关，关闭后变成透明旁路；以及一组关键配置开关的默认值。

#### 4.4.2 核心流程

**LMEM 访问路径**（与全局路径在 LSU 处分流）：

```
LSU 发出访存请求
   │
   ▼
lmem_switch（地址译码：addr 命中 VX_MEM_LMEM_BASE_ADDR？）
   │
   ├──是──► lsu_lmem_adapter ──► LocalMem（每核 SRAM，多 bank 交叉）  ← 不进缓存
   │
   └──否──► mem_coalescer ──► dcache ──► L2 ──► L3 ──► DRAM          ← 全局内存路径
```

`LocalMem` 内部是一个带优先级交叉开关的多 bank RAM：请求按 `(addr >> line_size) & (num_banks-1)` 做行交织分配到 bank，每个 bank 独立读写自己的 RAM。

**缓存可选性与旁路**：`VX_config.toml` 顶部有五个使能开关，任一关闭即把该级变成旁路：

| 开关 | 默认 | 关闭后 |
|---|---|---|
| `ICACHE_ENABLE` | true | fetch 直达下一级 |
| `DCACHE_ENABLE` | true | LSU 流量直达下一级 |
| `LMEM_ENABLE` | true | 不实例化本地内存 |
| `L2_ENABLE` | false | L2 透明旁路 |
| `L3_ENABLE` | false | L3 透明旁路（注释明说） |

**写策略自动推导**（回顾 4.1 第 4 条性质）：`WRITEBACK` 不是自由布尔，而是由「该级是否既是 LLC 又是唯一一致性点」推导：

\[ \text{WRITEBACK} = \text{is\_llc} \land (\text{该级是唯一汇聚点}) \]

例如 `L3_WRITEBACK = l3_is_llc`（L3 使能时总是写回）；`L2_WRITEBACK = l2_is_llc 且 单 cluster`；`DCACHE_WRITEBACK = dcache_is_llc 且 单核`。**私有缓存必须写透**，否则 store 会被私有缓存吸收而不被其他核心看见。

#### 4.4.3 源码精读

**五个使能开关**定义在 toml 顶部：

[VX_config.toml:13-17](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L13-L17) —— 注意默认 `L2_ENABLE = false`、`L3_ENABLE = false`，所以开箱即用的最小配置里，缓存栈可能短到只有 L1。

**写策略的推导表达式**：

[VX_config.toml:155-157](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L155-L157) —— `l2_is_llc`、`dcache_is_llc`、`l3_is_llc` 三个中间量；结合第 179、205、220 行的 `*_WRITEBACK` 表达式，可看出写回只在该级担任 LLC 且是唯一汇聚点时才开启。

**各级缓存的容量/相联度/MSHR 默认值**：

[VX_config.toml:162-229](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L162-L229) —— icache/dcache 各 16KB 4 路，L2 1MB 8 路，L3 2MB 8 路；所有级别 MSHR 默认 16；替换策略默认 FIFO；延迟随容量推导（`L1_LATENCY = 2 + …`，`L2/L3_LATENCY = 4 + …`）。

**LMEM 的配置**：

[VX_config.toml:232-234](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L232-L234) —— `LMEM_LOG_SIZE = 14`（16KB），`LMEM_NUM_BANKS = NUM_LSU_LANES`。

**LMEM 的实例化（每核一个）**：

[sim/simx/core.cpp:123-132](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L123-L132) —— `LocalMem::Create` 在 core 的 Impl 里调用，证明 LMEM 是每核私有；其输入请求数 `LSU_NUM_REQS + TCU + DXA`，说明除了 LSU，张量核（tbuf）与 DXA 也能直写 LMEM。

**LSU→LMEM 的分流连接**：

[sim/simx/core.cpp:153-159](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L153-L159) —— 每个 LSU block 经 `lsu_lmem_adapter` 接到 `local_mem_->Inputs`，这是 LMEM 的入口；全局内存路径则在 4.3 已展开。

**LocalMem 内部：交叉开关 + bank 交织 RAM**：

[sim/simx/mem/local_mem.cpp:32-56](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/local_mem.cpp#L32-L56) —— 第 47-51 行的 lambda 用 `(addr >> line_size) & (num_banks-1)` 做行交织，与缓存的 bank 交织策略一致；第 40 行 `RAM ram_(config.capacity)` 说明它就是一块裸 RAM，没有 tag、没有 MSHR、没有 miss 概念。

**LocalMem 的 tick（无 miss、立即响应）**：

[sim/simx/mem/local_mem.cpp:64-109](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/local_mem.cpp#L64-L109) —— 读请求直接从 RAM 读出整行作为响应（第 90-94 行），写请求按 byteen 写入 RAM（第 75-83 行）。对比缓存的 tag 查找/MSHR 路径，这里没有任何「未命中」分支，印证 LMEM 不走缓存。

**L3 旁路的注释证据**：

[sim/simx/processor.cpp:62-64](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/processor.cpp#L62-L64) —— 注释明确「L3 使能=LLC，否则=透明旁路仲裁器」。

#### 4.4.4 代码实践

**实践目标**：动手改变缓存使能配置，观察缓存栈长度的变化（配置型实践）。

**操作步骤**：
1. 阅读上述 toml 与 `cache_cluster.cpp` 第 30-34 行，确认「禁用即旁路」的机制。
2. 用统一启动器 `ci/blackbox.sh`（回顾 u1-l4）在 SimX 上分别跑：
   - 默认配置（L2/L3 关闭）：`./ci/blackbox.sh --driver=simx --app=demo`
   - 开启 L2：`./ci/blackbox.sh --driver=simx --app=demo --l2cache`
   - 开启 L2 + L3：`./ci/blackbox.sh --driver=simx --app=demo --l2cache --l3cache`
3. 对比三次运行的 PERF 输出（或至少对比退出码与运行时间）。

**需要观察的现象**：开启更深的缓存级后，DRAM 访问数（`mem_reads`/`mem_writes`）应下降（更多命中被 L2/L3 吸收），但每次 L1 miss 的延迟会因多穿过若干级而略有上升。

**预期结果**：三次都应打印 `PASSED!` 且退出码 0（功能不变），但性能计数器反映缓存栈变深后的命中/延迟权衡。**若手边无运行环境，本步骤标注为「待本地验证」**，可改为纯阅读：在 toml 第 13-17 行确认默认值，并解释为何最小配置下 dcache 是 LLC。

#### 4.4.5 小练习与答案

**练习 1**：一个 warp 要在 CTA 内所有线程间共享一组中间结果，应该用全局内存还是本地内存？为什么？

**参考答案**：用本地内存（LMEM）。它每核独占、低延迟、按 bank 交织提供高带宽，且专为线程间数据交换设计。全局内存要走缓存栈、跨核共享，延迟高得多。CUDA 程序员视角下这对应 `__shared__` 内存。

**练习 2**：为什么 `DCACHE_WRITEBACK` 在多核 + 开 L2 的配置下会被推导为 0（写透）？

**参考答案**：多核时 dcache 是私有缓存。若它写回，一个 core 的 store 会被吸收在私有 dcache 里、不下沉到共享的 L2，其他 core 看不见，破坏一致性。所以私有缓存必须写透，让 store 穿透到第一个共享汇聚点（L2）。`DCACHE_WRITEBACK = dcache_is_llc 且 单核` 这个表达式正是此约束的代码化。

**练习 3**：禁用 L3 时，L3 实例是否真的不存在？

**参考答案**：实例仍然创建（`processor.cpp` 第 64 行的 `Cache::Create` 总会调用），但 `bypass = !VX_CFG_L3_ENABLED = true`，使它变成一个不缓存、只转发的透明仲裁器。这种「实例恒在、行为旁路」的设计让连线拓扑固定，避免上游因 L3 是否存在而分支。

---

## 5. 综合实践

**任务**：画出 Vortex 的完整内存层次图（全局内存 + 本地内存两条路径合一）。

要求在一张图里标出：

1. **全局内存路径**：`LSU → mem_coalescer → dcache(L1) → L1 仲裁器 → (L2 仲裁器) → L2 → L3 → DRAM`，其中 icache 从 fetch 单独接入 L1。
2. **各级共享范围**：在 dcache 旁标「socket 内 ≤4 core 共享」、在 L2 旁标「cluster 内共享」、在 L3 旁标「全 cluster 共享」、在 DRAM 旁标「全局」。
3. **本地内存路径**：从 LSU 引出一条分叉，经 `lmem_switch`（地址命中 `VX_MEM_LMEM_BASE_ADDR`）到每核私有 `LocalMem`，并标注「旁路缓存、不进 L1/L2/L3」。
4. **写策略与 LLC**：在默认配置（L2/L3 关闭）下，用箭头标出 dcache 即 LLC、AMO 在 dcache 执行；并另画一个「L3 使能」的小图，标出此时 L3 是 LLC、L3 写回、dcache/L2 写透。
5. **配置开关**：在每级缓存旁注上对应的 toml 开关名（`ICACHE_ENABLE`/`DCACHE_ENABLE`/`L2_ENABLE`/`L3_ENABLE`/`LMEM_ENABLE`）与默认值。

**验证方法**：画完后，对照本讲引用的 `socket.cpp`、`cluster.cpp`、`processor.cpp`、`core.cpp`、`local_mem.cpp` 五份源码逐条核对——图上每一条线都应能在源码里找到一个 `bind()` 调用作为依据。若发现某条线找不到 `bind()`，说明图画错了。

> 进阶（可选）：用 `ci/blackbox.sh --l2cache --l3cache` 跑一次 `sgemm`，在 PERF 输出里找到 icache/dcache/L2/L3/memsim 各自的读写与 miss 计数，把它们标到图上对应位置，让图变成一张「带实测数据的热力图」。

## 6. 本讲小结

- Vortex 的全局内存是一条四级梯子：`LSU → L1(icache/dcache) → L2 → L3 → DRAM`，共享范围逐级扩大。
- 贯穿规则是**「层次即共享边界」**：L1 在 socket 内共享（≤4 core）、L2 在 cluster 内共享、L3 全局共享；这条规则同时定义了多核可见性——L2/L3 是 store 互相可见的汇聚点。
- SimX 用 `Cache`（单实例，L2/L3）与 `CacheCluster`（多实例+仲裁，L1）两种构建块组装，跨层全靠 `bind()` 连 channel，体现基数规则。
- **本地内存（LMEM）是另一条路**：每核私有 SRAM，由 LSU 地址译码直达，**不走缓存**，对应 CUDA 的 shared memory。
- 每级缓存可在 `VX_config.toml` 开关，**禁用即透明旁路**（实例恒在、`bypass=true`）；写策略由 LLC 角色自动推导，私有缓存必写透。
- 默认几何：L1 各 16KB 4 路、L2 1MB 8 路（分段）、L3 2MB 8 路（分段），各级 MSHR=16、替换策略 FIFO；LMEM 默认 16KB。

## 7. 下一步学习建议

- **向下钻入缓存内部**：下一讲 **u8-l2（缓存标签、MSHR、替换与数据通路）** 会打开单个 `Cache` 的黑盒，精读 tag store、MSHR 链式合并、fill forwarding、替换策略的 SimX 与 RTL 实现。
- **理解访存合并**：**u8-l3（访存合并、本地内存与 DRAM 模型）** 会展开 `mem_coalescer` 如何把一个 warp 的多线程访存合并成更少的 cache line 请求，以及 DRAM 用 ramulator2 建模的时序。
- **回到 LSU 上游**：**u8-l4（LSU 流水线设计）** 讲 AGU 地址生成与 slice 处理，补全「访存指令如何到达本讲的梯子入口」。
- **横向延伸**：若对一致性感兴趣，可跳读 u11-l2（原子内存操作与多缓存一致性），看 AMO 如何在 LLC 执行。
- **建议继续阅读的源码**：精读 `sim/simx/mem/cache.cpp`（单缓存实现）与 `hw/rtl/cache/VX_cache.sv`（RTL 对应），提前建立 SimX↔RTL 一一对应的直觉。
