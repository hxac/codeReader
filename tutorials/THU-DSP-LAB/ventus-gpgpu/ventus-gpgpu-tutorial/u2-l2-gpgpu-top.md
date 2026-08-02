# GPGPU_top 顶层模块与集群互联

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `GPGPU_top` 这个顶层模块对外暴露了哪些端口（`host_req`/`host_rsp`、`out_a`/`out_d` 等），以及它内部例化了哪几大类子模块。
- 复述从单个 SM 发出访存请求，到最终到达 L2 缓存（`Scheduler`）所经过的三层互联：`SM2clusterArbiter → l2Distribute → cluster2L2Arbiter`，以及响应如何原路返回。
- 解释贯穿这三层互联的 **source 字段编码**——每一层如何在 `source` 高位「贴上自己的 id」，响应时再「剥掉自己的 id」把数据路由回正确的 SM 与缓存。
- 区分 `GPGPU_top`（裸顶层）、`GPGPU_axi_top`（包了 AXI 接口的顶层）和仿真用的 `GPGPU_SimTop` 三者的关系。

本讲是第 2 单元的第 2 篇，承接 u1-l3 的目录地图与 u2-l1 的编程模型概念。本讲只看「顶层怎么把零件拼起来」，不深入零件内部（CTA 调度细节留到 u3，SM 流水线留到 u4/u5，缓存留到 u6）。

## 2. 前置知识

在阅读本讲前，你最好已经了解：

- **Bundle 与 Module（Chisel 基础）**：`Bundle` 是一组线的集合（类似 struct），`Module` 是硬件模块。`DecoupledIO` 是 Chisel 的标准握手接口，含 `valid`/`ready`/`bits` 三根线，`Flipped(...)` 表示方向取反。
- **`<>` 操作符**：Chisel 里把两个端口「对接」的糖，等价于把双方的 `valid/ready/bits` 同时连上（方向自动匹配）。
- **TileLink（TL）协议的极简理解**：Ventus 的 L1↔L2 之间用的是精简版 TileLink。请求方向用 `TLBundleA_lite`（A 通道，A=Address/Access），响应方向用 `TLBundleD_lite`（D 通道，D=Data）。A 通道里关键字段是 `opcode`（操作类型，如 Get=4 表示读）、`source`（请求者标识，用于回程路由）、`address`、`data`、`mask`。
- **GPU 编程模型（u2-l1）**：grid→block(workgroup)→warp(wavefront)→thread 的层级，以及「同一 block 的所有 warp 必须落在同一 SM」的约束。
- **关键规模参数（u2-l3 将详解）**：默认 `num_sm=2`、`num_warp=8`、`num_thread=32`、`num_cluster=1`、`num_l2cache=1`。

一个对本讲至关重要的直觉：**GPU 里有很多个发出访存请求的角色（多个 SM，每个 SM 里又有 ICache 和 DCache），但 L2 缓存只有很少几个。** 于是需要把「多对一」的请求汇到 L2，又需要把 L2 的「一对多」响应精确地送回当初发出请求的那个 SM、那个 cache。这套「汇总 + 还原」就是本讲三层互联要解决的核心问题，而 `source` 字段就是它的「快递单号」。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 |
|------|------|
| `ventus/src/top/GPGPU_top.scala` | 顶层组装枢纽。定义了 host↔CTA 数据 bundle、`CTAinterface`、`GPGPU_top`、`SM_wrapper`，以及三层互联模块 `SM2clusterArbiter`/`l2Distribute`/`cluster2L2Arbiter` 全部都在这一个文件里。 |

理解这些模块用到的参数与 bundle 定义在：

| 文件 | 作用 |
|------|------|
| `ventus/src/L1Cache/RVGParameter.scala` | 定义 `NSms`/`NCluster`/`NSmInCluster`/`NL2Cache`/`NCacheInSM` 等「规模」参数（通过 `HasRVGParameters` trait 注入）。 |
| `ventus/src/top/parameters.scala` | 定义 `num_sm`/`num_cluster`/`num_l2cache`/`l1cache_sourceBits` 等原始数值与 `l2cache_params`/`l2cache_params_l` 两套 L2 参数。 |
| `ventus/src/L1Cache/L1Interfaces.scala` | 定义 `L1CacheMemReq`/`L1CacheMemReqArb`/`L1CacheMemRsp` 等 bundle，是 SM 与互联之间的「数据包」格式。 |
| `ventus/src/L2cache/Parameters.scala` | 定义 `Scheduler` 的 `source_bits`、`parseAddress`（按地址选 L2 bank 的路由函数）、`l2cBits`。 |
| `ventus/src/top/ExtMem_gen.scala` | `GPGPU_gen`，综合用 Verilog 的生成入口。 |

> 小贴士：本讲提到的 `NSms`、`NCluster` 等带 `N` 前缀的标识符，并不在 `parameters.scala` 里，而是在 `RVGParameter.scala` 的 `HasRVGParameters` trait 里。`GPGPU_top` 继承自 `RVGModule`，自动混入这套参数，所以能直接用 `NSms` 之类。这一点 u2-l3 会展开，本讲你只需知道它们分别等于 `num_sm`、`num_cluster` 等即可。

## 4. 核心概念与源码讲解

### 4.1 GPGPU_top：顶层组装与端口

#### 4.1.1 概念说明

`GPGPU_top` 是整个 GPU 的「裸顶层」：它不包含任何 AXI 接口转换，只暴露最本质的四组端口，把 CTA 调度器、SM 集群、L2 缓存、三层互联这四类零件「用电线连起来」。你可以把它理解为一块「主板」：CPU（host）从一侧插入，DDR 从另一侧接入，中间靠主板上的走线把各个芯片连通。

注意三个名字的区分（u1-l2 已提过，这里强化）：

- `GPGPU_top`：本节讲的裸顶层，端口是 TileLink 风格的 `host_req`/`out_a`/`out_d`，**不含 AXI**。综合入口 `GPGPU_gen` 和仿真顶层最终都基于它。
- `GPGPU_axi_top`：在 `GPGPU_top` 外面再包一层 AXI 适配（`AXI4Lite2CTA` + `AXI4Adapter`），对外暴露标准 AXI4-Lite slave（接 CPU）和 AXI4 master（接 DDR），FPGA 上板常用。
- `GPGPU_SimTop`（在 `Mem_SimWrapper.scala`）：仿真专用，在 `GPGPU_top` 外挂一个行为级 `Mem_SimWrapper` 当作 DDR 模型。

#### 4.1.2 核心流程

`GPGPU_top` 的组装流程可以概括为三步：

1. **声明端口**：四组对外接口。
2. **例化零件**：1 个 `CTAinterface`、`NSms` 个 `SM_wrapper`、`NL2Cache` 个 `Scheduler`（L2）、`NCluster` 个 `SM2clusterArbiter`、`NCluster` 个 `l2Distribute`、`NL2Cache` 个 `cluster2L2Arbiter`。
3. **连线**：用两层 `for` 循环把 SM↔CTA、SM↔SM2clusterArbiter、SM2clusterArbiter↔l2Distribute 接好；再用一个 `MMU_ENABLED match` 分支把 l2Distribute↔cluster2L2Arbiter↔L2↔`out_a/out_d` 接好。

数据通路的主干（请求方向）：

```
SM_wrapper.memReq ──► SM2clusterArbiter ──► l2Distribute ──► cluster2L2Arbiter ──► Scheduler(L2).in_a
                                                                                        │
                                                                                   Scheduler.out_a ──► io.out_a ──► (DDR)
```

响应方向恰好相反：`out_d → Scheduler.in_d → cluster2L2Arbiter → l2Distribute → SM2clusterArbiter → SM_wrapper.memRsp`。

#### 4.1.3 源码精读

**端口声明**：[ventus/src/top/GPGPU_top.scala:150-163](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L150-L163) 定义了 `GPGPU_top` 的全部对外端口。关键四组：

- `host_req` / `host_rsp`：接 CPU，用来下发/回收 workgroup（详见 4.2）。
- `out_a` / `out_d`：`Vec(NL2Cache, ...)`，即每个 L2 对应一组 TileLink A/D 端口，经 `AXI4Adapter` 接 DDR。默认 `NL2Cache=1`，所以只有一组。
- `cycle_cnt` / `icache_invalidate` 等控制输入；`inst_cnt` 等是可选的性能计数输出（受 `INST_CNT` 开关控制）。

注意 `GPGPU_top` 的类签名带隐式参数 `(implicit p: Parameters, FakeCache: Boolean = false, SV: Option[mmu.SVParam] = None)`，并继承 `RVGModule`——这正是它能用 `NSms`/`NCluster` 等参数的原因。

**例化零件**：[ventus/src/top/GPGPU_top.scala:164-169](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L164-L169) 用 `VecInit + map` 一次性例化所有 SM 与 L2。这里有个重要细节：

```scala
val sm_wrapper = VecInit((0 until NSms).map(i => Module(new SM_wrapper(FakeCache, i, SV)).io))
val l2cache    = VecInit(Seq.fill(NL2Cache)( Module(new Scheduler(l2cache_params)).io))
val sm2clusterArb = VecInit(Seq.fill(NCluster)(Module(new SM2clusterArbiter(l2cache_params_l)).io))
val l2distribute   = VecInit(Seq.fill(NCluster)(Module(new l2Distribute(l2cache_params_l)).io))
val cluster2l2Arb  = VecInit(Seq.fill(NL2Cache)(Module(new cluster2L2Arbiter(l2cache_params_l,l2cache_params)).io))
```

注意 `SM2clusterArbiter` 和 `l2Distribute` 用的是 `l2cache_params_l`（带 `_l` 后缀），而 `Scheduler` 用的是 `l2cache_params`。两者的差别在 `parameters.scala`：`l2cache_micro_l` 把 `num_cluster` 固定为 `1`（因为 cluster 内部这些模块只关心「本 cluster」，不需要知道全局 cluster 数）。`cluster2L2Arbiter` 同时接收两套参数：输入侧（cluster 维度）用 `_l`，输出侧（L2 维度）用完整 `l2cache_params`。

**SM↔CTA↔SM2clusterArbiter 连线**：[ventus/src/top/GPGPU_top.scala:172-198](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L172-L198)。外层 `for (i <- 0 until NCluster)`，内层 `for (j <- 0 until NSmInCluster)`，因此全局 SM 编号 = `i * NSmInCluster + j`。CTA 的派发/完成总线按这个编号与 `sm_wrapper` 对接；访存请求则接到 `sm2clusterArb(i)` 的第 `j` 路输入。这里作者**没有**用简写的 `<>`，而是逐字段展开（例如 `memReqVecIn(j).bits := sm_wrapper(...).memReq.bits`），是因为要在中间插入 `source` 字段重映射——但这一层（GPGPU_top 本身）只是直连，真正的 source 拼接发生在 `SM2clusterArbiter` 内部。

**接 L2 与对外端口**（MMU 关闭分支）：[ventus/src/top/GPGPU_top.scala:200-213](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L200-L213)。`MMU_ENABLED match` 的 `false` 分支是默认路径，把每个 L2 的 `in_a`/`in_d` 接到 `cluster2L2Arb`，`out_a`/`out_d` 接到顶层 `io.out_a`/`io.out_d`。`true` 分支（[L224-307](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L224-L307)）额外接入 L2TLB，并用 `source` 最低位区分「普通访存」与「TLB 走页表」两类请求——这是 u7-l1 的内容，本讲先不展开。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：在不看答案的前提下，从端口反推「这个顶层对外的数据通路有几条」。

**操作步骤**：
1. 打开 `GPGPU_top.scala`，定位到 [L150-L163](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L150-L163) 的 `io = IO(...)`。
2. 数清楚：哪几个端口是「输入方向」（host 把任务送进来）、哪几个是「输出方向」（结果送出）。
3. 注意 `out_a`/`out_d` 是 `Vec(NL2Cache, ...)`，查 `parameters.scala` 里 `num_l2cache` 的值，得出默认有几条通往 DDR 的通路。

**需要观察的现象 / 预期结果**：默认配置下应有 **2 个输入类端口**（`host_req` 下发 workgroup、`out_d` 回送 DDR 数据）和对应输出。`NL2Cache=1`，所以 `out_a`/`out_d` 各只有 1 路。你能解释为什么 `host_req` 用 `Flipped(DecoupledIO(...))` 而 `host_rsp` 用 `DecoupledIO(...)` 吗？（提示：站在 `GPGPU_top` 的视角，host 是输入方。）

#### 4.1.5 小练习与答案

**练习 1**：`GPGPU_top` 里 `sm_wrapper` 用 `VecInit(...map(i => Module(...).io))` 创建。这样写和写成 `val sm0 = Module(...); val sm1 = Module(...)` 相比，好处是什么？
> **答案**：数量由参数 `NSms` 决定，可在 `for` 循环里按下标统一连线（见 L172-L185），不必为每个 SM 手写一份；改 `num_sm` 时无需改顶层代码。

**练习 2**：为什么 `Scheduler` 用 `l2cache_params`，而 `SM2clusterArbiter` 用 `l2cache_params_l`？
> **答案**：`l2cache_params_l` 内部把 `num_cluster` 固定为 1。cluster 内部的仲裁器只面对本 cluster 的 SM，不需要知道全局 cluster 数；而 L2 是全局共享的，要用完整参数（含真实 `num_cluster`）来计算 `source_bits` 等位宽。

---

### 4.2 CTAinterface：host 与 CTA 调度器之间的协议适配层

#### 4.2.1 概念说明

CPU（host）想往 GPU 上派发一个 workgroup，它脑里的「字段」和硬件 CTA 调度器脑里的「字段」并不完全同名。`CTAinterface` 就是一个**翻译层/适配器**：它把 host 视角的 `host2CTA_data` 一对一搬成调度器视角的 `cta_sche.io.host_wg_new` 字段，再把调度器算出的资源基地址（`lds_base`/`sgpr_base`/`vgpr_base` 等）以 `CTA2warp` 总线发给每个 SM，最后把 SM 报告的「warp 完成」回收成 `host_wg_done`。

可以把 `CTAinterface` 理解成「前台接待」：host 在前台填一张表（`host2CTA_data`），接待员把表转交给后台调度器（`cta_scheduler_top`），后台把任务分给各个工位（SM），工位干完活再由接待员通知 host。

#### 4.2.2 核心流程

`CTAinterface` 内部其实只例化了一个 `cta_scheduler_top`（真正的调度逻辑在 u3 讲），自己只做「字段搬运」：

- **下行（host → 调度器）**：`io.host2CTA` 的每个字段赋给 `cta_sche.io.host_wg_new.bits.*`。
- **上行（调度器 → host）**：`cta_sche.io.host_wg_done` 接到 `io.CTA2host`，上报完成的 `wg_id`。
- **派发（调度器 → SM）**：对每个 CU（即 SM），把 `cta_sche.io.cu_wf_new(i)` 翻译成 `CTA2warp(i)` 的 `dispatch2cu_*` 字段。
- **完成回收（SM → 调度器）**：把 `warp2CTA(i)` 的完成信号回灌给 `cta_sche.io.cu_wf_done(i)`。

#### 4.2.3 源码精读

**host 视角的数据包**：[ventus/src/top/GPGPU_top.scala:32-50](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L32-L50) 定义了 `host2CTA_data`。它几乎是一个 workgroup 的「全部元信息」：`host_wg_id`（任务号）、`host_num_wf`（这个 block 含几个 warp）、`host_wf_size`（每 warp 几线程）、`host_start_pc`（程序入口）、`host_kernel_size_3d`（3D 维度）、各种 `*_size_total`/`*_size_per_wf`（VGPR/SGPR/LDS/PDS 资源需求量）、`host_pds_baseaddr`/`host_gds_baseaddr`（私有/全局数据区基址）。这些字段正是 u2-l1 提到的 `.metadata` 里那些信息在硬件侧的载体。

**CTAinterface 的端口**：[ventus/src/top/GPGPU_top.scala:54-60](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L54-L60)。注意 `CTA2warp`/`warp2CTA` 都是 `Vec(NUMBER_CU, ...)`，即每个 SM 一对派发/回收端口（`NUMBER_CU = num_sm`）。

**字段搬运**：[ventus/src/top/GPGPU_top.scala:61-86](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L61-L86) 把 `host2CTA_data` 逐字段赋给调度器。例如 `host_num_wf → num_wf`、`host_wf_size → num_thread_per_wf`、`host_vgpr_size_total → num_vgpr`。注意 [L80-L82](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L80-L82) 的 `if (MMU_ENABLED)` ——`asid` 字段只在开启 MMU 时才连接，否则调度器根本没有这个字段（用 `Option` 表示）。

**派发翻译**：[ventus/src/top/GPGPU_top.scala:88-114](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L88-L114) 是 `for (i <- 0 until NUMBER_CU)` 循环。这里能看到调度器把「分配好的资源基地址」交给了 SM：`dispatch2cu_lds_base_dispatch ← lds_base`、`dispatch2cu_sgpr_base_dispatch ← sgpr_base`、`dispatch2cu_vgpr_base_dispatch ← vgpr_base`，以及关键的 `dispatch2cu_wf_tag_dispatch ← wf_tag`（warp 的标签，u3-l3 会讲它的 `wg_slot_id`/`wf_id_in_wg` 编码）。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：理解「同一个信息在 host 侧和调度器侧叫不同名字」。

**操作步骤**：在 [L62-L82](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L62-L82) 里，挑出 3 对「host 字段 → 调度器字段」的映射，填入下表：

| host 侧字段（`host2CTA_data`） | 调度器侧字段（`host_wg_new.bits`） | 含义 |
|---|---|---|
| `host_num_wf` | `num_wf` | block 含几个 warp |
| … | … | … |

**预期结果**：你能列出如 `host_wf_size → num_thread_per_wf`、`host_vgpr_size_total → num_vgpr`、`host_start_pc → start_pc` 等映射，并意识到调度器侧命名更「硬件化」。

#### 4.2.5 小练习与答案

**练习 1**：`CTAinterface` 自己有没有任何状态机或仲裁逻辑？
> **答案**：没有。它只例化了 `cta_scheduler_top` 并做纯组合的字段搬运与 `valid/ready` 直连，本身是无状态的适配层；真正的调度逻辑全在 `cta_scheduler_top`（u3）。

**练习 2**：`CTA2warp` 端口的向量宽度是 `NUMBER_CU`，而 `GPGPU_top` 里 SM 的数量是 `NSms`。这两者关系是？
> **答案**：`NUMBER_CU = num_sm`（见 `parameters.scala`），而 `NSms` 也等于 `num_sm`（经 `RVGParameters` 默认值）。两者数值一致，都代表「SM/CU 的总数」，只是分别来自两套参数体系。

---

### 4.3 SM_wrapper：单个 SM 的封装

#### 4.3.1 概念说明

`SM_wrapper` 把一个完整 SM 的所有零件——`CTA2warp`（接收 warp 派发）、`pipe`（流水线）、`InstructionCache`、`DataCache`、`SharedMemory`、`L1Cache2L2Arbiter`，以及可选的 L1TLB——打包成一个模块，对外只暴露最少的端口：派发/完成总线（`CTAreq`/`CTArsp`）、与外界唯一的访存通道（`memReq`/`memRsp`）。

关键点：**每个 SM 对外只有一组 `memReq`/`memRsp`**。SM 内部 ICache 和 DCache 都要访存，但它们先经过 SM 内部的 `L1Cache2L2Arbiter` 合并成一路，再由 `memReq` 送出去。这是 source 字段编码的「第一站」。

#### 4.3.2 核心流程

SM_wrapper 内部的访存合并：

```
InstructionCache.memReq ─┐
                         ├─► L1Cache2L2Arbiter ─► SM_wrapper.memReq ─► (SM2clusterArbiter)
DataCache.memReq ────────┘
                         ◄─ L1Cache2L2Arbiter ◄─ SM_wrapper.memRsp ◄─ (SM2clusterArbiter)
                         ├─► InstructionCache.memRsp
                         └─► DataCache.memRsp
```

`L1Cache2L2Arbiter` 在 `source` 最高位拼上 1 位 `cache_id`（0=ICache, 1=DCache），这样回程响应就能区分该还给 ICache 还是 DCache。

#### 4.3.3 源码精读

**端口**：[ventus/src/top/GPGPU_top.scala:332-350](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L332-L350)。注意 `memReq = DecoupledIO(new L1CacheMemReq)`、`memRsp = Flipped(DecoupledIO(new L1CacheMemRsp()(param)))`——这正是上一节 `GPGPU_top` 里连到 `sm2clusterArb` 的那对端口。`MMU_ENABLED` 时还多出 `l2tlbReq`/`l2tlbRsp`（向量，每个 cache 一路）。

**内部例化与连接骨架**：[ventus/src/top/GPGPU_top.scala:351-367](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L351-L367)。`cta2warp` ↔ `pipe` 对接 warp 派发；`L1Cache2L2Arbiter` 把对外 `memReq`/`memRsp` 与内部两个 cache 桥接：

```scala
val l1Cache2L2Arb = Module(new L1Cache2L2Arbiter()(param))
io.memReq <> l1Cache2L2Arb.io.memReqOut
l1Cache2L2Arb.io.memRspIn <> io.memRsp
```

**ICache 的 memReq 固定为 Get（读）**：[ventus/src/top/GPGPU_top.scala:379-389](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L379-L389) 里 `a_opcode := 4.U(3.W)`，TileLink 中 4 = `Get`（读请求）。指令缓存是只读的，所以恒为 Get。DCache 则直接 `<> dcache.io.memReq.get`（[L424](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L424)），因为 DCache 自己会根据读/写产生不同 opcode。

**资源约束断言**：[ventus/src/top/GPGPU_top.scala:435](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L435) `assert(num_cache_in_sm == 2, ...)`——目前强制每个 SM 恰好 2 个 L1 cache（1 个 ICache + 1 个 DCache）。

**source 字段第一站（在 L1Cache2L2Arbiter 内）**：见 `L1Cache2L2Arbiter.scala` [L34-L36](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1Cache2L2Arbiter.scala#L34-L36)：

```scala
memReqArb.io.in(i).bits.a_source := Cat(i.asUInt, io.memReqVecIn.get(i).bits.a_source)
```

这里 `i` 是 cache 编号（0/1），拼到 `a_source` 最高位。`L1CacheMemReqArb.a_source` 的宽度（见 `L1Interfaces.scala` [L97-L108](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1Interfaces.scala#L97-L108)）正好比 DCache 的 `a_source` 多 `log2Up(NCacheInSM)=1` 位。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：确认「SM 对外只有一路 memReq，内部两路 cache 的区分靠 source 高位」。

**操作步骤**：
1. 在 `SM_wrapper` 内找到 `l1Cache2L2Arb` 的例化（[L365](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L365)）。
2. 打开 `L1Cache2L2Arbiter.scala`，阅读其 memRsp 分支（[L41-L47](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1Cache2L2Arbiter.scala#L41-L47)），看它如何用 `d_source` 的高位把响应路由回 ICache 或 DCache。

**预期结果**：响应路由判断条件里出现 `d_source(... +3+log2Up(dcache_MshrEntry)+log2Up(dcache_NSets)-1, ...) === i.asUInt`，即取 source 中「cache_id 那一位」与 `i` 比较，命中者接收响应。

#### 4.3.5 小练习与答案

**练习 1**：为什么 ICache 的 memReq `a_opcode` 写死为 `4.U`，而 DCache 用 `<>` 直连？
> **答案**：ICache 只读，请求恒为 TileLink `Get`（opcode=4）；DCache 既要读又要写（写穿策略），opcode 由 DCache 内部根据操作动态产生，故直连让其自行决定。

**练习 2**：`SM_wrapper` 有个 `Counter(10)` 和 `pipe.io.pc_reset`（[L358-L360](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L358-L360)），它在做什么？
> **答案**：上电复位后保持 `pc_reset` 有效 5 拍，第 6 拍释放，给流水线一个确定性的启动时刻，避免取指 PC 在复位瞬间处于不定态。

---

### 4.4 source 字段编码：贯穿三层互联的「快递单号」

> 本节是一个**贯穿性概念**，理解了它，后面三个互联模块（4.5/4.6/4.7）就只是「同一套手法」的重复。请务必先读这一节。

#### 4.4.1 概念说明

设想 2 个 SM，每个 SM 有 2 个 cache（ICache+DCache），它们都往同一个 L2 发请求，L2 处理完要原路返回。L2 怎么知道这笔响应该回给「SM0 的 DCache」还是「SM1 的 ICache」？

Ventus 的方案：**每个请求自带一个 `source` 字段，每经过一层仲裁器，就在它的最高位「贴上本层的 id」；响应原路返回时，每层仲裁器「剥掉自己当年贴的那几位」，用剥下来的 id 把响应选路到正确的下游。** 这就是 TileLink 协议里 `source` 的典型用法——它既是请求者标识，也是回程路由依据。

`source` 字段在三层互联中逐层累加（从低位到高位）：

```
低位 ◄────────────────────────────────────────────► 高位
[ 3b ][ mshr_entry ][ set_idx ]│[ cache_id ]│[ sm_in_cluster ]│[ cluster_id ]
└── l1cache_sourceBits = 13 ─┘   └─ L1Cache2L2 ┘  └─ SM2cluster ┘  └─ cluster2L2 ┘
   (cache 内部产生)                (SM 内, +1b)     (+1b)           (+0b, 默认1簇)
```

各字段含义：

| 字段 | 位数 | 含义 | 由谁贴上 |
|------|------|------|----------|
| `set_idx` + `mshr_entry` + `3b` | `l1cache_sourceBits`（默认 13） | cache 内部用来找回 MSHR 条目（哪笔在途 miss）的依据 | ICache/DCache 自身 |
| `cache_id` | `log2Up(NCacheInSM)` = 1 | 0=ICache, 1=DCache | `L1Cache2L2Arbiter` |
| `sm_in_cluster` | `log2Up(NSmInCluster)` = 1 | cluster 内的 SM 编号 | `SM2clusterArbiter` |
| `cluster_id` | `log2Ceil(NCluster)` = 0 | cluster 编号（默认 1 簇，故 0 位） | `cluster2L2Arbiter` |

其中 `l1cache_sourceBits` 定义在 [ventus/src/top/parameters.scala:111](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L111)：

```scala
def l1cache_sourceBits: Int = 3 + log2Up(dcache_MshrEntry) + log2Up(dcache_NSets)  // = 3 + 2 + 8 = 13
```

L2 侧需要容纳所有这些位，其 `source_bits` 见 [ventus/src/L2cache/Parameters.scala:159](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/Parameters.scala#L159)，比上述总和还多 2 位（留作 MMU 模式下区分 TLB 请求等）。

#### 4.4.2 核心流程（请求贴标 / 响应剥标）

请求方向（去程），每层在最高位 `Cat(本层id, 旧source)`：

```
cache:        source = [set|mshr|3b]                      宽 13
L1Cache2L2:   source = [cache_id | set|mshr|3b]           宽 14   (+1)
SM2cluster:   source = [sm_id | cache_id | set|mshr|3b]   宽 15   (+1)
cluster2L2:   source = [cl_id | sm_id | cache_id | ...]   宽 15   (+0, 默认)
                → 送入 Scheduler.in_a
```

响应方向（回程），每层取最高几位与自己比对来选路，并把这几位剥掉后向下游传递：

```
Scheduler.in_d.source = [cl_id | sm_id | cache_id | set|mshr|3b]
cluster2L2: 取顶 cl_id 位选 cluster；剥掉 cl_id，传 [sm_id | cache_id | ...]
SM2cluster: 取顶 sm_id 位选 SM；     剥掉 sm_id，传 [cache_id | ...]
L1Cache2L2: 取顶 cache_id 位选 cache；剥掉，传 [set|mshr|3b] 给 cache 找 MSHR
```

关键不变量：**每层只「认识」自己当年贴的那几位，对其它位原样透传**。这样三层可以独立、可组合。

#### 4.4.3 源码精读（汇总三层贴标点）

- L1Cache2L2Arbiter 贴 `cache_id`：[L1Cache2L2Arbiter.scala:35](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1Cache2L2Arbiter.scala#L35) `Cat(i.asUInt, ...a_source)`。
- SM2clusterArbiter 贴 `sm_id`：[GPGPU_top.scala:538](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L538) `Cat(i.asUInt, io.memReqVecIn(i).bits.a_source)`。
- cluster2L2Arbiter 贴 `cluster_id`：[GPGPU_top.scala:618](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L618) `Cat(i.asUInt, io.memReqVecIn(i).bits.source)`。

> 注：以上 `Cat` 都在「该层向量宽度 > 1」时才生效；当 `NSmInCluster==1` 或 `NCluster==1` 时，对应层会走特判分支直接透传 source（因为只有一路，无需 id 区分）——见 [L534-L539](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L534-L539) 与 [L614-L619](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L614-L619)。

#### 4.4.4 代码实践（推演型）

**实践目标**：用默认参数手工推算一次完整 source 字段的位布局。

**操作步骤**：取 `dcache_MshrEntry=4`、`dcache_NSets=256`、`NCacheInSM=2`、`NSmInCluster=2`、`NCluster=1`，填出下表（位数 = 该字段的 bit 宽）：

| 字段 | 位数 | 本例数值范围 |
|------|------|------|
| `set_idx` | ? | 0..255 |
| `mshr_entry` | ? | 0..3 |
| `3b` | 3 | — |
| `cache_id` | ? | 0/1 |
| `sm_in_cluster` | ? | 0/1 |

**预期结果**：`set_idx`=8、`mshr_entry`=2、`cache_id`=1、`sm_in_cluster`=1，总宽 = 8+2+3+1+1 = 15 位（cluster_id=0 位）。于是同一个 15 位 source 既编码了「谁发的」（高 2 位：sm+cache），也编码了「发的是哪笔」（低 13 位：set+mshr）。

#### 4.4.5 小练习与答案

**练习 1**：如果 `dcache_NSets` 从 256 翻倍到 512，source 字段总宽变不变？影响哪一层？
> **答案**：变。`set_idx` 从 8 位变 9 位，`l1cache_sourceBits` 从 13 变 14，导致从 `L1Cache2L2Arbiter` 往后的所有 source 位宽 +1，L2 的 `source_bits` 也要随之放大。

**练习 2**：为什么 `cluster_id` 用 `log2Ceil` 而 `sm_in_cluster` 用 `log2Up`？
> **答案**：`log2Ceil(N)` 对 N=1 返回 0（不需要位），`log2Up(N)` 对 N=1 也返回 0，两者在 N≥2 时一致；这里混用主要是历史风格，对默认配置（NCluster=1、NSmInCluster=2）数值结果都正确。本质都表示「表示这么多 id 至少需要几位」。

---

### 4.5 SM2clusterArbiter：cluster 内多 SM 请求的仲裁

#### 4.5.1 概念说明

一个 cluster 里有 `NSmInCluster` 个 SM（默认 2 个），它们的 `memReq` 都要送往本 cluster 的下游。`SM2clusterArbiter` 把这多路请求**仲裁成一路**（用 Chisel 的 `Arbiter`，固定优先级，编号小者优先），并在 source 高位贴上 `sm_id`；同时把下游返回的一路响应，按 source 里的 `sm_id` **拆回**对应的 SM。

#### 4.5.2 核心流程

请求侧：`NSmInCluster` 路 `memReqVecIn` → `Arbiter` → `Queue`（深度 2 的缓冲）→ `memReqOut`。仲裁时每路把 `source` 重写为 `Cat(sm_id, 旧source)`。

响应侧：单路 `memRspIn` → 按 source 的 `sm_id` 字段（最高那几位）选通 `memRspVecOut(i)` 的 `valid`，并用 `Mux1H` 把对应 SM 的 `ready` 选回 `memRspIn.ready`。

#### 4.5.3 源码精读

模块与端口：[ventus/src/top/GPGPU_top.scala:518-526](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L518-L526)。

**请求仲裁与贴标**：[L529-L550](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L529-L550)。核心几行：

```scala
val memReqArb = Module(new Arbiter(new TLBundleA_lite(L2param), NSmInCluster))
val memReqBuf = Module(new Queue(new TLBundleA_lite(L2param), 2))   // 深度2缓冲
...
memReqArb.io.in(i).bits.source := Cat(i.asUInt, io.memReqVecIn(i).bits.a_source)  // 贴 sm_id
...
memReqBuf.io.enq <> memReqArb.io.out
io.memReqOut <> memReqBuf.io.deq
```

注意它从 `L1CacheMemReqArb`（SM 侧格式）转成 `TLBundleA_lite`（L2 侧 TileLink 格式）：`opcode ← a_opcode`、`address ← a_addr`、`mask ← a_mask`、`data ← a_data`。`size := 0.U` 表示一个完整的 block 传输。

**响应拆路**：[L554-L578](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L554-L578)。`NSmInCluster==2` 时（默认），用 source 的最高 1 位（即 `sm_id`）判断响应归哪个 SM：

```scala
io.memRspVecOut(i).valid := io.memRspIn.bits.source(log2Up(NSmInCluster)+log2Ceil(NCacheInSM)+l1cache_sourceBits-1) === i.asUInt && io.memRspIn.valid
```

这一长串下标正是「source 里的 sm_id 那一位」。`ready` 用 `Mux1H(UIntToOH(...), Reverse(Cat(io.memRspVecOut.map(_.ready))))` 做一对一热点选择。

#### 4.5.4 代码实践（源码阅读型）

**实践目标**：验证「请求侧贴标」与「响应侧剥标」用的是同一个 `sm_id` 位。

**操作步骤**：
1. 读 [L538](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L538) 的贴标表达式，确认 `sm_id` 在 source 的最高位。
2. 读 [L562](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L562)（NSmInCluster==2 分支）的取位下标，确认取的正是最高位。

**预期结果**：两者位宽与位置一致；`memReqBuf` 的存在使请求路径多一拍以上缓冲，但 `source` 内容不变，所以响应仍能正确对齐。

#### 4.5.5 小练习与答案

**练习 1**：`Arbiter` 之后为何还要加一个深度 2 的 `Queue`？
> **答案**：避免下游（l2Distribute）未及时接收时丢请求，给仲裁输出加一级缓冲，解耦仲裁速率与下游接收速率。

**练习 2**：若 `NSmInCluster==1`（cluster 里只有 1 个 SM），这套贴标/剥标逻辑会怎样？
> **答案**：走 [L534-L536](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L534-L536) 的特判，`source` 直接透传不贴 id；响应侧（[L559-L560](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L559-L560)）也直接全通——因为只有一路，无需区分。

---

### 4.6 l2Distribute：按地址把请求分发到对应 L2 bank

#### 4.6.1 概念说明

`SM2clusterArbiter` 输出的是「本 cluster 的单路请求」。下一步要把请求送往 L2。当系统有多个 L2 bank（`NL2Cache > 1`）时，需要决定送给哪一个 bank——这是**按地址分发**。`l2Distribute` 就是干这个的：它根据请求地址落在哪个 L2 bank 的地址区间，把请求送到对应的 `memReqVecOut(i)`，并把多个 L2 的响应汇总成一路回给 cluster。

注意：默认 `num_l2cache=1`，所以 `l2Distribute` 实际只有 1 路输出，退化为直通。但它的代码是为多 bank 设计的，理解它有助于看懂可扩展性。

#### 4.6.2 核心流程

请求侧（1 路 → N 路，按地址选）：对每个 L2 bank `i`，`memReqVecOut(i).valid = memReqIn.valid && (parseAddress(addr)._2 == i)`，即「地址算出来的 bank 号等于 i 才有效」。

响应侧（N 路 → 1 路，仲裁）：用 `Arbiter` 把 N 个 L2 的响应汇总成 `memRspOut`。

#### 4.6.3 源码精读

模块：[ventus/src/top/GPGPU_top.scala:587-598](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L587-L598)。核心：

```scala
val memRspArb = Module(new Arbiter(new TLBundleD_lite_plus(l2param), NL2Cache))
for (i <- 0 until NL2Cache){
  io.memReqVecOut(i).bits  := io.memReqIn.bits
  io.memReqVecOut(i).valid := io.memReqIn.valid && (i.asUInt === l2param.parseAddress(io.memReqIn.bits.address)._2)
  memRspArb.io.in(i) <> io.memRspVecIn(i)
}
io.memReqIn.ready := Mux1H(UIntToOH(l2param.parseAddress(io.memReqIn.bits.address)._2), Reverse(Cat(io.memReqVecOut.map(_.ready))))
io.memRspOut <> memRspArb.io.out
```

`parseAddress` 是 L2 参数对象的方法，见 [ventus/src/L2cache/Parameters.scala:219-225](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/Parameters.scala#L219-L225)。它把地址拆成 `(tag, l2c, set, offset)` 四元组，其中第二个返回值 `l2c` 就是 **L2 bank 编号**：

```scala
def parseAddress(x: UInt): (UInt, UInt, UInt, UInt) = {
  val offset = x
  val set = offset >> offsetBits
  val l2c = set >> setBits          // bank 号
  val tag = l2c >> l2cBits
  (tag(tagBits-1,0), if(l2cBits!=0) l2c(l2cBits-1,0) else 0.U, set(setBits-1,0), offset(offsetBits-1,0))
}
```

`l2cBits = log2Ceil(cache.l2cs)`（[L191](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/Parameters.scala#L191)），而 `l2cs = num_l2cache`。默认 `num_l2cache=1` ⇒ `l2cBits=0` ⇒ `l2c` 恒为 0 ⇒ 所有请求都走 bank 0。当 `num_l2cache=2` 时，`l2cBits=1`，地址最高位即 bank 选择位，实现地址交织（interleaving）到两个 L2。

#### 4.6.4 代码实践（推演型）

**实践目标**：理解「多 L2 bank 时地址如何分发」。

**操作步骤**：假设把 `num_l2cache` 改为 2（即 `NL2Cache=2`），此时 `l2cBits=1`。请回答：
1. 一个地址 `0x0000_0000` 会送到 bank 几？地址 `0x8000_0000` 呢？
2. `memReqVecOut` 的宽度（路数）变成多少？

**预期结果**：bank 号由 set 字段之上的 1 位决定（具体落在地址的哪一位取决于 `offsetBits + setBits`）。`memReqVecOut` 变成 2 路，每路对应一个 L2 bank。**待本地验证**：精确的位位置需要按 `l2cache_NSets=64`、`l2cache_BlockWords=32` 算出 `offsetBits` 与 `setBits` 后才能确定。

#### 4.6.5 小练习与答案

**练习 1**：`l2Distribute` 在请求方向上是「1 拆 N」，为什么不会同时给多个 bank 发请求？
> **答案**：因为 `parseAddress(addr)._2` 对一个地址只产生一个 bank 号，所以同一时刻只有一路 `memReqVecOut(i).valid` 为真（一对一热点）。

**练习 2**：默认配置下 `l2Distribute` 是不是多余的？
> **答案**：功能上退化为单路直通（始终选 bank 0），但保留它使得「把 `num_l2cache` 调大」无需改动顶层连线——这是面向可扩展性的设计。

---

### 4.7 cluster2L2Arbiter：多 cluster 请求汇聚到 L2 + 响应还原

#### 4.7.1 概念说明

经过 `l2Distribute` 后，请求已经选好了目标 L2 bank。但一个 L2 bank 可能接收来自**多个 cluster** 的请求（当 `NCluster > 1` 时）。`cluster2L2Arbiter` 负责把多个 cluster 的请求仲裁成一路送入 `Scheduler.in_a`，并在 source 最高位贴上 `cluster_id`；响应方向则按 `cluster_id` 把 `Scheduler.in_d` 拆回各 cluster。它是三层互联的最后一站，也是 source 字段「贴到最满」的地方。

#### 4.7.2 核心流程

请求侧：`NCluster` 路 `memReqVecIn` → `Arbiter` → `memReqOut`（接 L2 `in_a`）。仲裁时每路 source 重写为 `Cat(cluster_id, 旧source)`。

响应侧：单路 `memRspIn`（接 L2 `in_d`）→ 按 source 顶部的 `cluster_id` 位选通 `memRspVecOut(j)`，并把 source **剥掉 cluster_id 那几位**后再传给下游（下游 cluster 侧的 `SM2clusterArbiter` 只认识 `sm_id` 及其以下的位）。

#### 4.7.3 源码精读

模块：[ventus/src/top/GPGPU_top.scala:606-629](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L606-L629)（请求侧）。

**请求贴 cluster_id**（[L612-L619](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L612-L619)）：

```scala
memReqArb.io.in(i).bits.source := Cat(i.asUInt, io.memReqVecIn(i).bits.source)  // 贴 cluster_id
```

注意它同时接收两套参数：`cluster2L2Arbiter(L2paramIn=l2cache_params_l, L2paramOut=l2cache_params)`——输入侧（cluster 来的）用 `_l` 参数的 bundle，输出侧（去 L2 的）用完整参数的 bundle，两者 source 位宽不同（输出多 `log2Ceil(NCluster)` 位），由这个 `Cat` 完成「位宽提升」。

**响应剥 cluster_id 并拆路**（[L633-L646](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L633-L646)）：

```scala
// 剥掉顶部的 cluster_id 位，只保留 sm_id 及以下
io.memRspVecOut(i).bits.source := io.memRspIn.bits.source(
  log2Ceil(NSmInCluster)+log2Ceil(NCacheInSM)+3+log2Up(dcache_MshrEntry)+log2Up(dcache_NSets)-1, 0)
...
// 用顶部 cluster_id 位选择输出给哪个 cluster
io.memRspVecOut(i).valid := io.memRspIn.bits.source(<顶部 cluster_id 位>) === i.asUInt && io.memRspIn.valid
```

这正是 4.4 说的「剥掉自己当年贴的那几位，并用它选路」。剥完后，下游 `SM2clusterArbiter` 看到的 source 顶部就只剩 `sm_id`，可以继续剥。

> MMU 模式特例：在 `GPGPU_top` 的 `MMU_ENABLED=true` 分支里（[L286](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L286)），cluster2L2Arbiter 的请求 source 还会在最低位补一个 `0`（`Cat(...source, 0.U(1.W))`），与 TLB 请求（补 `1`）区分；响应侧用 `source(0)` 判断该响应归 cache 还是归 TLB。这是 u7-l1 的内容，这里只作提醒。

#### 4.7.4 代码实践（源码阅读型）

**实践目标**：确认 cluster2L2Arbiter「剥掉的位数 = 当年贴的位数」。

**操作步骤**：
1. 在 [L618](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L618) 读出贴的位数（`log2Ceil(NCluster)`）。
2. 在 [L638](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L638) 读出响应保留的范围，反推剥掉了顶部多少位。

**预期结果**：保留范围是 `[log2Ceil(NSmInCluster)+log2Ceil(NCacheInSM)+l1cache_sourceBits - 1 : 0]`，剥掉的正是顶部 `log2Ceil(NCluster)` 位。默认 `NCluster=1` 时 `log2Ceil(1)=0`，等于不剥，且走 [L614-L615](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L614-L615) 的特判直接透传。

#### 4.7.5 小练习与答案

**练习 1**：为什么 `cluster2L2Arbiter` 需要两套参数（`L2paramIn`/`L2paramOut`），而 `SM2clusterArbiter` 只需要一套？
> **答案**：cluster2L2Arbiter 跨越了「cluster 域」与「L2 全局域」，两侧 bundle 的 source 位宽不同（输出多 cluster_id 位），需要两套参数描述两种 bundle；SM2clusterArbiter 输入输出都在 cluster 域内（同一套 `_l` 参数），只是 source 宽度变化由 `Cat` 处理。

**练习 2**：响应经过 cluster2L2Arbiter 后，source 字段里还剩哪些信息？
> **答案**：剩 `[sm_id | cache_id | set|mshr|3b]`，即去掉了 cluster_id。接下来 `SM2clusterArbiter` 会再剥掉 `sm_id`，`L1Cache2L2Arbiter` 再剥掉 `cache_id`，最终 cache 拿到原始的 `set|mshr|3b` 去定位 MSHR。

---

## 5. 综合实践：追踪一次 SM 访存请求的完整旅程

这是本讲的核心实践，把 4.1～4.7 串起来。任务：**追踪 SM0 的 DCache 发出的一次 miss 读请求，到它从 DDR 拿到数据并回到 DCache 的全过程**，并画出请求/响应数据流、标注每一层对 `source` 字段的改动。

### 实践目标

- 把「贴标—传输—剥标」的完整闭环在脑中走一遍。
- 用真实代码行号佐证每一步。

### 操作步骤

1. **起点（SM0 内部）**：DCache 因 miss 发出 `memReq`，其 `a_source` 此时只有低 13 位（`set|mshr|3b`），`a_opcode` 为 Get/某写操作码。参考 [SM_wrapper.scala:424](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L424)。
2. **L1Cache2L2Arbiter（贴 cache_id=1）**：source 变成 14 位 `[1 | set|mshr|3b]`（DCache 编号为 1）。参考 [L1Cache2L2Arbiter.scala:35](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1Cache2L2Arbiter.scala#L35)。
3. **出 SM_wrapper**：经 `io.memReq` 进入 `GPGPU_top` 的 cluster 循环（[L178-L180](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L178-L180)），送到 `sm2clusterArb(0).memReqVecIn(0)`（SM0 在 cluster0，编号 j=0）。
4. **SM2clusterArbiter（贴 sm_id=0）**：source 变成 15 位 `[0 | 1 | set|mshr|3b]`，经 `Queue` 输出到 `memReqOut`。参考 [GPGPU_top.scala:538](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L538)。
5. **l2Distribute（按地址选 bank）**：默认 `l2cBits=0`，选中 bank 0，透传给 `cluster2l2Arb(0).memReqVecIn(0)`。参考 [GPGPU_top.scala:593](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L593)。
6. **cluster2L2Arbiter（贴 cluster_id，默认 0 位）**：source 不变（NCluster=1 特判透传），送入 `Scheduler.in_a`。参考 [GPGPU_top.scala:614-L615](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L614-L615)。
7. **L2 → DDR**：L2 也 miss，经 `Scheduler.out_a` → `io.out_a(0)` → `AXI4Adapter` → DDR。参考 [GPGPU_top.scala:211](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L211)。
8. **回程（DDR → L2）**：数据经 `io.out_d(0)` → `Scheduler.in_d`，source 字段被原样带回（仍是 `[0|1|set|mshr|3b]`）。
9. **cluster2L2Arbiter（剥 cluster_id）**：默认不剥，按 cluster_id 选路给 cluster0。参考 [L633-L646](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L633-L646)。
10. **l2Distribute（汇总）**：经 `memRspArb` 回到 cluster0 的 `SM2clusterArbiter`。参考 [L597](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L597)。
11. **SM2clusterArbiter（剥 sm_id=0）**：按最高 sm_id 位选通 `memRspVecOut(0)`，source 剥成 `[1|set|mshr|3b]` 回到 SM0。参考 [L554-L568](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L554-L568)。
12. **L1Cache2L2Arbiter（剥 cache_id=1）**：按 cache_id 位选通 DCache，source 剥成原始 13 位 `[set|mshr|3b]`。参考 [L1Cache2L2Arbiter.scala:41-L47](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1Cache2L2Arbiter.scala#L41-L47)。
13. **终点**：DCache 用低 13 位里的 `mshr_entry`+`set` 找回当初那笔 miss，把数据填给对应的 warp。

### 需要观察的现象 / 预期结果

请画出一张数据流图，标出：
- 每一跳的模块名与对应源码行号；
- 请求方向每一跳后 `source` 的位宽与含义（13→14→15→15）；
- 响应方向每一跳后 `source` 的位宽与含义（15→15→14→13）；
- `GPGPU_top.scala` 末尾的 `printf` 调试钩子（[L315-L326](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L315-L326)）打印的 `SM ${sm_id} CACHE ${cache_id}`，正是从 source 字段里反解出来的——你可以用它来核对上面的旅程。

> ⚠️ 本实践为**源码阅读 + 推演型**，无需运行仿真即可完成。若想用波形验证，可在 sim-verilator 跑 vecadd 时观察 `out_a`/`out_d` 上的 source 字段（详见 u1-l4 的 `--dump-mem` 与 u7-l3 的仿真框架）。

## 6. 本讲小结

- `GPGPU_top` 是裸顶层，用 `VecInit + map` 例化 1 个 `CTAinterface`、`NSms` 个 `SM_wrapper`、`NL2Cache` 个 `Scheduler`（L2），以及 `NCluster`/`NL2Cache` 个三层互联模块，靠两层 `for` 循环连线。
- `CTAinterface` 是无状态的协议适配层，把 host 视角的 `host2CTA_data` 翻译成 CTA 调度器与各 SM 的字段，并回收 warp 完成信号。
- `SM_wrapper` 把一个 SM 的 `CTA2warp`+`pipe`+ICache+DCache+SharedMem 打包，对外只暴露一路 `memReq`/`memRsp`；内部 `L1Cache2L2Arbiter` 已把 ICache/DCache 合并。
- 三层互联 `SM2clusterArbiter → l2Distribute → cluster2L2Arbiter` 分别完成「cluster 内多 SM 仲裁」「按地址选 L2 bank」「多 cluster 汇聚到 L2」。
- **source 字段**是贯穿三层的「快递单号」：去程每层在最高位 `Cat(本层id, source)`，回程每层剥掉自己的 id 并据此选路；低 13 位（`l1cache_sourceBits`）始终是 cache 找回 MSHR 的依据。
- 默认规模（`num_sm=2, num_cluster=1, num_l2cache=1`）下，cluster_id 和 L2 bank 选择都退化为单路直通，但代码已为多 cluster、多 L2 bank 留好扩展点。

## 7. 下一步学习建议

- **向下（参数系统）**：若你想彻底搞懂 `NSms`/`NCluster`/`source_bits` 这些位宽是怎么算出来的，接着读 u2-l3（参数系统与配置机制），重点看 `parameters.scala` 与 `RVGParameter.scala` 的 `HasRVGParameters`。
- **向深处（CTA 调度）**：本讲把 `CTAinterface` 当黑盒，真正的「wg_buffer → resource_table → allocator → cu_interface」流程在 u3 单元（CTA 任务调度器）展开，`wf_tag` 的编码细节在 u3-l3。
- **向深处（SM 内部）**：`SM_wrapper` 里的 `pipe` 是 SM 流水线，u4/u5 单元逐级拆解。
- **向深处（缓存）**：`L1Cache2L2Arbiter` 之后的 `Scheduler`（L2）来自 inclusive-cache 框架，u6-l5 会讲它的 Directory/BankedStore/MSHR。
- **关于 MMU 分支**：本讲多次提到 `MMU_ENABLED=true` 时的额外 source 低位、L2TLB 接入，完整链路在 u7-l1（MMU 与 TLB/PTW）。
