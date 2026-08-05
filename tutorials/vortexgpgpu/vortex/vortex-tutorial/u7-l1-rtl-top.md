# Vortex 顶层与 socket/cluster RTL

## 1. 本讲目标

本讲是「核心流水线 RTL」单元的第一讲，带读者从 SimX 的 C++ 仿真世界跨进真实的 SystemVerilog RTL 世界。读完本讲，你应当能够：

- 说清 Vortex GPU 的**三级硬件聚类层次**：cluster → socket → core，以及它们各自共享的缓存级别（L2 / L1）。
- 读懂顶层模块 [`Vortex.sv`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/Vortex.sv) 如何把 KMU、L3 与多个 cluster 用 `genvar` 循环拼装成一颗完整 GPU，并向外暴露 DRAM 接口。
- 读懂 [`VX_cluster.sv`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_cluster.sv) 与 [`VX_socket.sv`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_socket.sv) 这两个「中间层」如何用 arbiter（仲裁器）把上下层总线接起来。
- 理解 [`VX_gpu_pkg.sv`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_gpu_pkg.sv) 这个共享常量/类型包与 `VX_define.vh` 宏定义之间的关系。

本讲把 `VX_core`（核心流水线本身）当成黑盒，它的内部结构留给下一讲 u7-l2。

## 2. 前置知识

在进入 RTL 之前，先回忆 u5（SimX）和 u6 已经建立的两条心智模型，本讲会反复用到它们：

1. **GPU 是一个「聚类层次」结构**。在 SimX 里你已经见过 `Processor → Cluster → Socket → Core` 的实例化树（u5-l2）。RTL 的层次名字几乎一一对应，只是用 SystemVerilog 写成。回忆 microarchitecture 文档的一句话：
   - **Sockets**：把若干个 core 组在一起，**共享 L1 cache**；
   - **Clusters**：把若干个 socket 组在一起，**共享 L2 cache**。

2. **「channel 即连线」与基数规则**（u5-l1、u5-l3）。SimX 里模块之间只用 `SimChannel` 通信；在 RTL 里对应的角色是各种 `*_bus_if`（总线接口）。本讲你会看到大量 `VX_mem_bus_if`、`VX_kmu_bus_if`、`VX_dcr_bus_if`、`VX_gbar_bus_if`——它们就是连接 cluster / socket / core 的「channel」。

3. **配置宏的两层世界**（u2）。`VX_config.toml` 经 `gen_config.py` 生成 `VX_config.vh`，里头全是 `VX_CFG_*` 宏（如 `VX_CFG_NUM_CORES`、`VX_CFG_SOCKET_SIZE`）。本讲的 RTL 把这些宏「烘焙」成具体的电路规模。

> 一个关键直觉：**层次的存在是为了「共享」**。哪一层实例化了 cache，那一层就是该 cache 的共享边界。把这句话记牢，本讲的所有 RTL 接线都是在为它服务。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [`hw/rtl/Vortex.sv`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/Vortex.sv) | GPU 顶层 | 对外 DRAM/DCR 接口；实例化 KMU、L3、多个 cluster |
| [`hw/rtl/VX_cluster.sv`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_cluster.sv) | cluster 层（共享 L2） | L2 实例化、全局屏障 gbar、socket 实例化循环 |
| [`hw/rtl/VX_socket.sv`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_socket.sv) | socket 层（共享 L1） | icache/dcache 实例化、L1 仲裁、core 实例化循环 |
| [`hw/rtl/VX_gpu_pkg.sv`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_gpu_pkg.sv) | 共享常量/类型包 | 把 `VX_CFG_*` 宏派生为 localparam，定义全树共用的结构体 |

辅助但重要的文件：

| 文件 | 角色 |
|---|---|
| [`hw/rtl/VX_define.vh`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_define.vh) | 全树共用的「伞形」头文件，include 了 platform/config/types，并定义派生宏（如 `EXT_GFX_ANY_ENABLE`） |
| [`docs/designs/microarchitecture.md`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md) | 聚类架构（socket/cluster）的权威说明 |
| [`docs/designs/cache_subsystem.md`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/cache_subsystem.md) | 各级 cache 的共享边界与使能开关 |

---

## 4. 核心概念与源码讲解

### 4.1 顶层模块 Vortex.sv：把整颗 GPU 拼起来

#### 4.1.1 概念说明

`Vortex.sv` 是整颗 GPU 的**外壳**（top-level wrapper）。它的职责很纯粹：

- **向更上层（testbench 或 FPGA AFU）暴露三类接口**：通往 DRAM 的内存请求/响应、通往命令处理器（CP）的 DCR（Device Configuration Register）读写、以及一对控制信号 `start`/`busy`。
- **在内部把三大部件拼起来**：唯一的 KMU（Kernel Management Unit，u3-l4/ u11-l3 会详讲，本讲当成「发射 CTA 的源」）、唯一的 L3 cache、以及 `NUM_CLUSTERS` 个 cluster。
- **不做任何数据计算**，只做总线扇出（fan-out）与汇聚。

为什么顶层要实例化 L3 而不是 cluster 各自带 L3？因为 L3 是**全局共享**的最后一级 cache，所有 cluster 都要能看到它——共享点必须在共同祖先处实例化，这是「层次即共享边界」原则的必然结果。

#### 4.1.2 核心流程

顶层的数据/控制流可以画成：

```text
                 ┌──────────────────────── Vortex.sv (top) ────────────────────────┐
   DCR req ─────►│                                                                   │
                 │   ┌──────┐    kmu_bus_in[1]   ┌─────────┐   per_cluster_kmu_bus   │
   start ──────► │   │ KMU  │ ────────────────► │ kmu_arb │ ─────(1→N 扇出)────► ┐   │
                 │   └──────┘                    └─────────┘                     │   │
                 │                                                                 ▼   │
                 │   ┌───────────┐  per_cluster_dcr_bus    ┌────────────────────┐ │   │
                 │   │ dcr_arb   │ ─────(1→N 扇出)───────► │  cluster × N       │ │   │
                 │   └───────────┘                         │  (g_clusters 循环) │ │   │
                 │                                          └─────────┬──────────┘ │   │
                 │             per_cluster_mem_bus (↑ L2 miss)        │            │   │
                 │   ┌───────────┐  ◄────────────────────────────────┘            │   │
                 │   │  L3 cache │  (VX_cache_wrap, 全局共享 LLC)                  │   │
                 │   └─────┬─────┘                                                 │   │
                 │         │  mem_bus_if                                           │   │
                 │         ▼  (g_mem_bus_if 桥接)                                  │   │
   mem_req ◄──── │     mem_req / mem_rsp  (VX_MEM_PORTS, 连外部 DRAM)              │   │
   mem_rsp ────► │                                                                   │
   busy  ◄────── │  = kmu_busy | dcr_req_valid | (| per_cluster_busy)              │
                 └───────────────────────────────────────────────────────────────────┘
```

要点：

1. KMU 是 CTA 的**唯一发射源**，它产出的 `kmu_bus_in` 经 `kmu_arb` 扇出给每个 cluster（1→N）。
2. DCR 写请求经 `dcr_cluster_arb` 扇出给每个 cluster。
3. 每个 cluster 的 L2 miss 汇聚到 `per_cluster_mem_bus_if`，向上进 L3；L3 miss 再经 `mem_bus_if` 走到顶层 `mem_req/mem_rsp` 端口，连到外部 DRAM。
4. `busy` 是顶层向 testbench 报告「整颗 GPU 是否还在干活」的或运算结果。

#### 4.1.3 源码精读

**端口：内存、DCR、控制三类**。

顶层端口声明在 [Vortex.sv:16-51](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/Vortex.sv#L16-L51)。其中 `mem_req_*` / `mem_rsp_*` 是数组，宽度由 `VX_MEM_PORTS` 决定（下文 4.4 会看到它等于 `L3_MEM_PORTS`）；`dcr_req_*` 是单条 CP 写入通道；`start`/`busy` 是控制信号。

**编译期静态断言：规模必须是 2 的幂**。

[Vortex.sv:53-55](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/Vortex.sv#L53-L55) 用三条 `STATIC_ASSERT` 强制 `NUM_CLUSTERS`、`NUM_CORES`、`SOCKET_SIZE` 必须是 2 的幂。这是因为地址解码、仲裁、`CLOG2` 位宽计算处处依赖「幂次」假设，不是幂次会在很多地方产生非法电路，干脆在 elaboration 阶段就拦住。

**一条精妙的架构约束：AMO 要求中间级 write-through**。

[Vortex.sv:57-68](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/Vortex.sv#L57-L68) 的注释和断言值得细读：当使能原子扩展（`EXT_A`）时，**LLC 之上的所有 cache 都必须是 write-through（写直达）**，不能是 write-back（写回）。原因是：若中间级是 write-back，它会「吞掉」某个 hart 的 store 而不让 LLC 看到，于是另一个 hart 在同一行的 SC（Store-Conditional）可能**错误地成功**。RVA 规范允许 SC「假失败」，但绝不允许「假成功」。这是顶层用静态断言守护 ISA 正确性的典型例子——u11-l2 讲 AMO 时会再回到这点。

**实例化 KMU**。

[Vortex.sv:84-99](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/Vortex.sv#L84-L99) 实例化唯一的 `VX_kmu`。它直接消费顶层的 `dcr_req_*` 与 `start`，输出一条 `kmu_bus_in[0]`。本讲把它当黑盒：它根据主机写入的 DCR 把 CTA 请求灌进 `kmu_bus`。

**实例化 L3（全局 LLC）**。

[Vortex.sv:127-163](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/Vortex.sv#L127-L163) 实例化 `VX_cache_wrap`（L3 包装器）。注意几个参数的语义：

- `PASSTHRU (!VX_CFG_L3_ENABLED)`：L3 未使能时，它退化成一个**透明仲裁器**（不做缓存，只把请求直通），这样上层代码无需为「有没有 L3」分叉。
- `IS_LLC (L3_IS_LLC)`：标记自己是最后一级。
- `AMO_ENABLE (VX_CFG_EXT_A_ENABLED)`：LLC 是 AMO 的一致性点。

它的 `core_bus_if` 接所有 cluster（`per_cluster_mem_bus_if`），`mem_bus_if` 接外部 DRAM。

**把 L3 输出桥接到顶层 mem 端口**。

[Vortex.sv:165-179](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/Vortex.sv#L165-L179) 用一个 `for` 循环把 `mem_bus_if[i]` 的字段逐根 assign 到顶层 `mem_req_*[i]` / `mem_rsp_*[i]` 端口。SystemVerilog 接口（interface）不能直接跨模块边界出到 testbench，所以要「拆线」成普通 wire。

**KMU 扇出仲裁器**。

[Vortex.sv:184-193](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/Vortex.sv#L184-L193) 的 `VX_kmu_arb` 把 1 条 `kmu_bus_in` 扇出成 `NUM_CLUSTERS` 条 `per_cluster_kmu_bus_if`。注意 `OUT_BUF` 在多 cluster 时为 3、单 cluster 时为 0——这是为了在多 cluster（往往跨 FPGA SLR）时插一级寄存器 skid buffer，缓解跨片时序。

**DCR 扇出仲裁器**同理，见 [Vortex.sv:208-216](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/Vortex.sv#L208-L216)。

**cluster 实例化循环**。

[Vortex.sv:219-246](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/Vortex.sv#L219-L246) 用 `genvar cluster_id` 循环实例化 `VX_CLUSTER_NUM_CLUSTERS` 个 `VX_cluster`，每个传进自己的 `CLUSTER_ID`、对应的 mem/kmu/dcr bus 切片。`per_cluster_mem_bus_if[cluster_id * L2_MEM_PORTS +: L2_MEM_PORTS]` 这种「基址 + 宽度」切片（part-select）是 SystemVerilog 把总线均分给各实例的惯用法。

**busy 汇聚**。

[Vortex.sv:247-249](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/Vortex.sv#L247-L249)：

```verilog
wire busy_r;
`BUFFER_EX(busy_r, kmu_busy | dcr_bus_if.req_valid | (|per_cluster_busy), 1'b1, 1, (`VX_CFG_NUM_CLUSTERS > 1));
assign busy = busy_r | kmu_busy | dcr_bus_if.req_valid;
```

整颗 GPU「忙」当且仅当 KMU 在忙、有 DCR 请求在处理、或任意 cluster 在忙。这与 SimX 里 `Processor::any_running()` 的语义对应（u5-l2），只是 RTL 用组合/寄存器实现。注意 `busy` 里**没有**把 L3 单列出来——L3 的活动会通过它所服务的 cluster 的 `per_cluster_busy` 间接反映。

#### 4.1.4 代码实践

**实践目标**：用顶层在仿真启动时打印的 CONFIGS 行，反向核对当前配置下整颗 GPU 的拓扑。

**操作步骤**：

1. 在 `build/` 目录下用 `../configure --xlen=64` 配置一棵树（若已有则跳过）。
2. 打开 [Vortex.sv:296-300](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/Vortex.sv#L296-L300)，阅读那段 `initial begin ... `TRACE(0, ("CONFIGS: ..."))`。它会在仿真 `time 0` 打印一行配置摘要。
3. 用 `./ci/blackbox.sh --driver=simx --app=demo`（或 `--driver=rtlsim`）跑一次 demo，**注意第一条 `CONFIGS:` 输出**。

**需要观察的现象**：日志里应出现类似

```
CONFIGS: num_threads=..., num_warps=..., num_cores=..., num_clusters=..., socket_size=..., local_mem_base=0x..., num_barriers=...
```

**预期结果**：把这条记录的 `num_cores / num_clusters / socket_size` 抄下来，验证 `num_cores == num_clusters * (num_cores/socket_size) * socket_size`，并与下一节 4.4 给出的 `NUM_SOCKETS` 派生公式对上。若用 `--cores=4 --clusters=2` 重跑，这行数字应随之变化。

> 如果只能读到源码、无法运行仿真，请标注「待本地验证」，并直接据 `VX_config.toml` 默认值手算拓扑。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Vortex.sv` 顶层要实例化 L3，而不是让每个 cluster 自带 L3？

**参考答案**：L3 是所有 cluster **共享**的最后一级 cache。按「层次即共享边界」原则，共享点必须在所有使用者的共同祖先处实例化——而所有 cluster 的共同祖先正是顶层。若把 L3 放进 cluster，就成了 per-cluster 私有 L2.5，跨 cluster 数据无法共享，多 cluster 一致性也无从实现。

**练习 2**：把 [Vortex.sv:53-55](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/Vortex.sv#L53-L55) 的三条断言翻译成一句话的工程纪律。

**参考答案**：「聚类规模的三个旋钮（cluster 数、core 总数、每个 socket 的 core 数）必须是 2 的幂」，否则地址解码与位宽计算会产生非法/不可综合电路，在 elaboration 阶段直接报错而非留到仿真出疑难 bug。

---

### 4.2 VX_cluster.sv：L2 的共享边界

#### 4.2.1 概念说明

`VX_cluster` 是**共享 L2 的那一层**。一个 cluster 内部装着：

- `NUM_SOCKETS` 个 socket（下一节讲）；
- 一个 L2 cache（`VX_cache_wrap`），是本 cluster 内所有 core 的共享点——也是「不同 core 的 store 第一次变得互相可见」的地方；
- 一个全局屏障单元 `gbar_unit`（cluster 是跨核全局屏障的自然作用域）；
- 可选的 **DXA core**（异步 DMA 引擎，u9-l2）与 **VX_graphics** 图形块（u10-l1），它们都是 cluster 级共享资源。

> 为什么全局屏障放在 cluster？因为 `vx_barrier(is_global)` 的会合范围是「整个 cluster 的所有 core」（见 u6-l1 的 BarrierUnit 与 u4-l3 的 `gbarrier`）。把 `gbar_unit` 放在 cluster 顶层，恰好让它的物理作用域匹配语义作用域。

#### 4.2.2 核心流程

```text
   ┌──────────────────── VX_cluster.sv ────────────────────────┐
   │                                                            │
   │  kmu_bus_if ──► kmu_arb(1→NUM_SOCKETS) ──► per_socket_kmu  │
   │                                                            │
   │                            ┌──────────────────────────┐    │
   │  socket × NUM_SOCKETS ────►│ socket_mem_bus_if (L1出口)│    │
   │  (g_sockets 循环)          └─────────────┬────────────┘    │
   │                                          ▼                 │
   │  [可选] DXA gmem ──► dxa_l2_priority_arb ──► (LSU 高优先)   │
   │                                          │                 │
   │  [可选] graphics(tcache/rcache/ocache/   │                 │
   │          rtcache) ─────────────────────►│                 │
   │                                          ▼                 │
   │                                ┌──────────────────┐        │
   │                                │   L2 cache       │        │
   │                                │ (VX_cache_wrap)  │        │
   │                                └────────┬─────────┘        │
   │                                         │ l2_mem_bus_if    │
   │  gbar_bus_if ◄─ gbar_arb ◄─ per_socket  │                  │
   │                       │                 ▼                  │
   │                       ▼            mem_bus_if (↑ 上交顶层)  │
   │                 ┌────────────┐                          │   │
   │                 │ gbar_unit  │  (全局屏障会合点)          │   │
   │                 └────────────┘                          │   │
   └────────────────────────────────────────────────────────────┘
```

要点：

1. cluster 把上层来的 1 条 `kmu_bus_if` 再次扇出给 `NUM_SOCKETS` 个 socket。
2. 各 socket 的 L1 出口（`socket_mem_bus_if`）、可选的 DXA 全局内存口、可选的各图形 cache 口，**全部汇聚进 L2**。
3. L2 的 miss 经 `l2_mem_bus_if` → `mem_bus_if` 上交顶层的 L3。

#### 4.2.3 源码精读

**模块声明与参数**。

[VX_cluster.sv:16-50](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_cluster.sv#L16-L50)：模块 import 了 `VX_gpu_pkg`（拿到 `NUM_SOCKETS` 等常量），参数 `CLUSTER_ID` 用于给自己内部实例编号。端口 `mem_bus_if [L2_MEM_PORTS]` 是上交 L3 的出口，`kmu_bus_if[1]` 是从顶层 KMU 来的入口。

**KMU 再次扇出给 socket**。

[VX_cluster.sv:98-107](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_cluster.sv#L98-L107)：`VX_kmu_arb` 把 1 条 kmu bus 扇出成 `NUM_SOCKETS` 条。注意这里与顶层的 `kmu_arb` 形成了**两级扇出**：顶层 1→NUM_CLUSTERS，cluster 1→NUM_SOCKETS。KMU 发出的 CTA 最终经这两级抵达具体 socket/core。

**全局屏障单元**。

[VX_cluster.sv:109-129](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_cluster.sv#L109-L129)：每个 socket 的 `per_socket_gbar_bus_if` 经 `gbar_arb` 汇聚到一条 `gbar_bus_if`，再接 `VX_gbar_unit`。这就是 `vx_barrier(..., is_global=true)` 在硬件上的会合点。

**L2 cache 实例化**。

[VX_cluster.sv:211-245](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_cluster.sv#L211-L245) 实例化 `VX_cache_wrap l2cache`。和 L3 一样，`PASSTHRU (!VX_CFG_L2_ENABLED)`：L2 未使能时它退化为透明仲裁器（注意 cache_subsystem 文档强调：**多核配置必须开 L2**，因为它是不同 core store 第一次互相可见的点）。`IS_LLC (L2_IS_LLC)` 在「开了 L2 但没开 L3」时为真。

**socket 实例化循环**。

[VX_cluster.sv:394-444](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_cluster.sv#L394-L444)：用 `genvar socket_id` 循环实例化 `NUM_SOCKETS` 个 `VX_socket`，每个传 `(CLUSTER_ID * NUM_SOCKETS) + socket_id` 作为全局 `SOCKET_ID`。注意 `mem_bus_if` 切片用 `socket_mem_bus_if[socket_id * L1_MEM_PORTS +: L1_MEM_PORTS]`。

**DXA 的优先级仲裁（值得细读的工程细节）**。

当使能 DXA 时，[VX_cluster.sv:320-363](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_cluster.sv#L320-L363) 在 socket 的 LSU 出口与 DXA 的 gmem 出口之间插了一个**优先级仲裁器** `dxa_l2_priority_arb`，仲裁策略 `"P"`（priority），**LSU 绑在低索引（高优先级）**。注释 [VX_cluster.sv:320-322](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_cluster.sv#L320-L322) 解释：防止 DXA 的批量 DMA 流量在 L2 把 core 自己的 icache/dcache 饿死。这是「同一共享点要保护弱者」的典型设计。

**可选的 VX_graphics 块**。

[VX_cluster.sv:463-502](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_cluster.sv#L463-L502) 在 `EXT_GFX_ANY_ENABLE` 时实例化 `VX_graphics`，它把 TEX/RASTER/OM/RTU 的图形 cache 出口（tcache/rcache/ocache/rtcache）经 [VX_cluster.sv:504-518](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_cluster.sv#L504-L518) 绑到 `per_socket_mem_bus_if` 的专用槽位（`L2_GFX_TEX_IDX` 等）。本讲只需知道「图形 cache 也是 L2 的输入源之一」。

#### 4.2.4 代码实践

**实践目标**：在源码里数清楚「L2 一共有几类输入源」。

**操作步骤**：

1. 打开 [VX_cluster.sv:1529](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_gpu_pkg.sv#L1529) 附近的 `L2_NUM_REQS` 定义（在 `VX_gpu_pkg.sv` 中）：`L2_NUM_REQS = L2_SOCKET_REQS + L2_GFX_REQS`。
2. 回到 [VX_cluster.sv:206-245](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_cluster.sv#L206-L245)，确认 `l2cache` 的 `core_bus_if` 接的是 `per_socket_mem_bus_if`，其宽度正是 `L2_NUM_REQS`。
3. 列出 `per_socket_mem_bus_if` 的三类填充者：socket 的 LSU/L1（`socket_mem_bus_if`，[L367-369](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_cluster.sv#L367-L369) 无 DXA 时直连）、DXA gmem（经优先级仲裁）、图形 cache（`L2_GFX_*_IDX` 槽位）。

**需要观察的现象**：L2 的输入请求数 = `NUM_SOCKETS * L1_MEM_PORTS`（socket）+ `L2_GFX_REQS`（图形）。

**预期结果**：在不开任何图形扩展、不开 DXA 时，L2 输入就是 `NUM_SOCKETS * L1_MEM_PORTS` 路 socket 流量；开图形后会多出 `tcache/rcache/ocache/rtcache` 中已使能项的额外槽位。这是理解 L2 仲裁压力来源的关键。若无法运行，按公式手算即可，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：cache_subsystem 文档说「L2 是多核配置的必需项」，请用本讲源码解释为什么。

**参考答案**：不开 L2 时，`VX_cache_wrap l2cache` 的 `PASSTHRU=true`，退化为透明仲裁器，不做缓存也不做一致性管理。这样不同 core 的 store 在到达 L3 之前没有任何「互相可见」的中间点；而 L3 是全局共享的，把所有一致性负担压到 L3 会严重损失性能且增大 L3 的 MSHR 压力。因此多核时必须开 L2，让 cluster 成为第一级共享点。

**练习 2**：DXA 的 DMA 流量与 core 的 LSU 流量在 L2 入口抢带宽，RTL 用什么策略保护 core？

**参考答案**：见 [VX_cluster.sv:345-363](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_cluster.sv#L345-L363) 的 `dxa_l2_priority_arb`，仲裁策略为 `"P"`（优先级），LSU 绑在低索引（高优先），从而保证 core 的 icache/dcache miss 不会被 DXA 批量传输饿死。

---

### 4.3 VX_socket.sv：L1 的共享边界

#### 4.3.1 概念说明

`VX_socket` 是**共享 L1 的那一层**，也是距离 core 最近的一层。一个 socket 内部装着：

- `SOCKET_SIZE` 个 core（下一讲 u7-l2 才打开 `VX_core` 内部）；
- 一个共享的 **icache**（指令 cache）和一个共享的 **dcache**（数据 cache），都由 `VX_cache_cluster` 实现——它允许多个 core 共享同一组 cache 实例（`NUM_ICACHES`/`NUM_DCACHES = SOCKET_SIZE/4`，即最多 4 个 core 共享一个 L1 实例）；
- 把 icache 与 dcache 出口仲裁合并成 L1 出口的 **mem_arb**；
- 各种图形/加速器扩展的 per-socket 仲裁器（TEX/OM/RASTER/RTU/DXA），把各 core 的同类请求汇聚成一条 per-socket 总线上交 cluster。

> 直觉：socket = 「一组紧挨在一起、共用 L1 的 core」。它是 Vortex 用「少量大 L1」换取高利用率的基本单元。

#### 4.3.2 核心流程

```text
   ┌──────────────────────── VX_socket.sv ─────────────────────────┐
   │                                                                │
   │  kmu_bus_if ──► kmu_arb(1→SOCKET_SIZE) ──► per_core_kmu ──┐    │
   │                                                            │    │
   │  core × SOCKET_SIZE ──► per_core_icache_bus ──► icache ──┐ │    │
   │  (g_cores 循环)                                          │ │    │
   │  core × SOCKET_SIZE ──► per_core_dcache_bus ──► dcache ──┤ │    │
   │                                            (共享 L1)      │ │    │
   │                                                          ▼ ▼    │
   │                              mem_arb(icache 优先 "P") ◄───┘      │
   │                                          │                      │
   │                                          ▼ mem_bus_if[L1_MEM_PORTS]
   │                                          (上交 cluster L2)       │
   │  gbar_bus_if ◄── gbar_arb ◄── per_core_gbar                     │
   │  dcr_bus_if ──► dcr_core_arb ──► per_core_dcr                   │
   │  [可选] tex/om/rtu/raster/dxa 各自的 per_core → socket arb      │
   └──────────────────────────────────────────────────────────────────┘
```

要点：

1. 每个 core 各自连到共享的 icache / dcache（per_core_*_bus 是「多写者 → 共享 cache」的入口）。
2. icache 与 dcache 的 miss 经 `mem_arb` 合并成 L1 出口（icache 优先），上交 cluster 的 L2。
3. KMU/DCR/gbar 在 socket 内再做一次 1→SOCKET_SIZE 扇出，分别送到每个 core。

#### 4.3.3 源码精读

**模块声明**。

[VX_socket.sv:16-75](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_socket.sv#L16-L75)：参数 `SOCKET_ID` 给本 socket 全局编号；端口 `mem_bus_if[L1_MEM_PORTS]` 是上交 cluster 的 L1 出口；`kmu_bus_if[1]`/`dcr_bus_if`/`gbar_bus_if` 是来自 cluster 的入口。注意一堆 `ifdef VX_CFG_EXT_*` 条件端口——图形/DXA 扩展是可裁剪的。

**KMU 扇出到 core**。

[VX_socket.sv:84-93](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_socket.sv#L84-L93)：`VX_kmu_arb` 把 1 条 kmu bus 扇出给 `SOCKET_SIZE` 个 core。这是 KMU CTA 扇出的**第三级**（顶层 → cluster → socket）。

**共享 icache**。

[VX_socket.sv:122-163](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_socket.sv#L122-L163)：`VX_cache_cluster icache` 以 `NUM_INPUTS=SOCKET_SIZE`、`NUM_UNITS=NUM_ICACHES` 实例化——`NUM_INPUTS` 是「连进来的 core 数」，`NUM_UNITS` 是「实际 cache 实例数」。当 `NUM_ICACHES < SOCKET_SIZE` 时，多个 core 共享同一个 icache 实例（这正是「socket 共享 L1」的实现）。注意 `WRITE_ENABLE=0`：指令 cache 不可写。

**共享 dcache**。

[VX_socket.sv:167-213](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_socket.sv#L167-L213)：`VX_cache_cluster dcache`，`NUM_INPUTS=SOCKET_SIZE`、`WRITE_ENABLE=1`、`WRITEBACK(VX_CFG_DCACHE_WRITEBACK)`、`AMO_ENABLE(VX_CFG_EXT_A_ENABLED)`。数据 cache 可写、可回写（当它是 LLC 时）、可做 AMO。

**L1 仲裁器：icache vs dcache**。

[VX_socket.sv:217-258](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_socket.sv#L217-L258) 是 socket 里最值得看的一段。它把 icache 与 dcache 的 mem 出口用一个 `VX_mem_bus_arb` 合并：

```verilog
.ARBITER    ("P"), // prioritize the icache
```

策略 `"P"` 表示优先级仲裁，注释明确说**优先 icache**——因为取指停顿会直接卡住整条流水线，比数据 miss 更致命。合并后的 `mem_bus_if` 就是 L1 出口，上交 cluster 的 L2。注意当 `L1_MEM_PORTS > 1` 时（`for (i ...)` 循环），dcache 的其余端口直连出口而不经 icache 仲裁（见 `else` 分支 [L249-257](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_socket.sv#L249-L257)）。

**core 实例化循环**。

[VX_socket.sv:450-513](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_socket.sv#L450-L513) 用 `genvar core_id` 实例化 `SOCKET_SIZE` 个 `VX_core`，全局 `CORE_ID = SOCKET_ID * SOCKET_SIZE + core_id`。每个 core 拿到自己的 dcache/icache/kmu/dcr/gbar 以及（可选）图形总线切片。注意 [L451-461](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_socket.sv#L451-L461) 有一段被注释掉的 `core_clk` 时钟门控代码——预留的功耗优化钩子，当前未启用。

**图形扩展的 per-socket 仲裁**。

[VX_socket.sv:263-423](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_socket.sv#L263-L423) 这一大段在每个图形扩展 `ifdef` 块里，都把 `SOCKET_SIZE` 个 per-core 请求（如 `per_core_tex_bus_if`）汇聚成一条 per-socket 总线（如 `per_socket_tex_bus_if`）上交 cluster。策略多为 `"R"`（轮转 Round-Robin）。本讲只需理解「它们都是 socket 内的 1←N 汇聚」。

#### 4.3.4 代码实践

**实践目标**：验证「icache 与 dcache 的 miss 在 L1 出口被合并，且 icache 享有高优先级」。

**操作步骤**：

1. 打开 [VX_socket.sv:217-248](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_socket.sv#L217-L248)，定位 `mem_arb` 的 `ARBITER` 参数。
2. 用 Grep 在 `hw/rtl` 内搜索 `ARBITER.*"P"` 与 `ARBITER.*"R"`，统计哪些仲裁点用优先级、哪些用轮转。

**需要观察的现象**：`mem_arb`（icache vs dcache）用 `"P"`；图形扩展的 socket 仲裁多用 `"R"`。

**预期结果**：你能总结出一条经验法则——**「取指/LSU 这种卡住流水线的路径用优先级，多个对等请求者用轮转」**。这是阅读 Vortex 仲裁器的通用钥匙。若本地无法 grep，直接据本讲引用的行号阅读即可。

#### 4.3.5 小练习与答案

**练习 1**：`VX_cache_cluster` 的 `NUM_INPUTS` 与 `NUM_UNITS` 两个参数有什么区别？为什么 socket 里 `NUM_INPUTS=SOCKET_SIZE` 而 `NUM_UNITS=NUM_ICACHES`？

**参考答案**：`NUM_INPUTS` 是连进该 cache 集群的**请求者数**（这里是 SOCKET_SIZE 个 core），`NUM_UNITS` 是实际例化的 **cache 实例数**（`NUM_ICACHES = SOCKET_SIZE/4`）。当 `NUM_UNITS < NUM_INPUTS` 时，cache_cluster 内部把多个 core 的请求仲裁到少量共享实例上，实现「多 core 共享 L1」。用更少更大的共享 L1 提升对 coherent working set 的利用率。

**练习 2**：为什么 icache 与 dcache 抢 L1 出口时，RTL 选择优先 icache？

**参考答案**：取指 miss 会立刻让整条流水线断流（没有指令就没有发射），代价是全流水线停顿；而 dcache miss 通常只阻塞依赖该 load 的后续指令，乱序/多 warp 调度还能部分掩盖。因此把 icache 设为高优先级能更好地保护整体吞吐。

---

### 4.4 VX_gpu_pkg.sv 与 VX_define.vh：RTL 的共享字典

#### 4.4.1 概念说明

`VX_gpu_pkg.sv` 是一个 SystemVerilog `package`，是全树所有 RTL 模块共用的**常量、类型、函数字典**。它解决一个工程问题：

- 配置宏（`VX_CFG_*`）来自 `VX_config.vh`，是**预处理器宏**，作用域是文件级的、扁平的；
- 但 RTL 模块需要的是**带类型的、可被 `import` 的** localparam、typedef、function。

`VX_gpu_pkg` 就是这两者之间的桥：它 `include "VX_define.vh"` 把宏吃进来，再把它们「重新包装」成 package 里的 localparam / typedef / function，供各模块 `import VX_gpu_pkg::*`。

`VX_define.vh` 则是一个「伞形头文件」：它依次 include `VX_platform.vh`、`VX_config.vh`、`VX_types.vh`，再定义一批派生便利宏（如 `EXT_GFX_ANY_ENABLE`）。几乎每个 `.sv` 文件开头都 `` `include "VX_define.vh" ``。

#### 4.4.2 核心流程

```text
   VX_config.toml  ──gen_config.py──►  VX_config.vh  (VX_CFG_* 宏)
                                                    │
   VX_types.toml   ──gen_config.py──►  VX_types.vh  (VX_DCR_*/VX_MEM_* 宏)
                                                    │
                              VX_platform.vh        │
                                   │                │
                                   └──► VX_define.vh ◄┘  (+ 派生宏 EXT_GFX_ANY_ENABLE 等)
                                                │
                                                ▼
                                     VX_gpu_pkg.sv   (package: localparam / typedef / function)
                                                │
                                  import VX_gpu_pkg::*
                                                │
                                                ▼
                          Vortex.sv / VX_cluster.sv / VX_socket.sv / VX_core.sv ...
```

关键：**宏的世界是扁平字符串替换，package 的世界是带作用域的类型系统**。`VX_gpu_pkg` 是从前者到后者的唯一闸口。

#### 4.4.3 源码精读

**把宏镜像成 localparam**。

[VX_gpu_pkg.sv:25-30](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_gpu_pkg.sv#L25-L30) 把 `VX_CFG_DCACHE_NUM_BANKS`、`L1_MEM_PORTS`、`L2_MEM_PORTS` 等宏镜像成本 package 的 localparam。注释明确说这些是「mirror」——给后续定义引用，避免在 package 内部到处写反引杠宏。

**位宽常量**。

[VX_gpu_pkg.sv:33-41](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_gpu_pkg.sv#L33-L41)：

```verilog
localparam NC_BITS = `CLOG2(`VX_CFG_NUM_CORES);
localparam NW_BITS = `CLOG2(`VX_CFG_NUM_WARPS);
localparam NT_BITS = `CLOG2(`VX_CFG_NUM_THREADS);
...
localparam NC_WIDTH = `UP(NC_BITS);   // 向上取整到合法位宽（至少 1 位）
```

这是全树共用的「core id / warp id / thread id 位宽」。`_BITS` 是 `CLOG2` 原值（可能为 0，比如只有 1 个 warp 时），`_WIDTH = UP(_BITS)` 保证至少 1 位宽——这是 SystemVerilog 里 `[$clog2(1)-1:0]` 会变负数的常见坑，`UP` 宏专门兜底。

**NUM_SOCKETS 的派生（本讲最重要的公式）**。

[VX_gpu_pkg.sv:144](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_gpu_pkg.sv#L144)：

```verilog
localparam NUM_SOCKETS = `UP(`VX_CFG_NUM_CORES / `VX_CFG_SOCKET_SIZE);
```

这就是「一个 cluster 里有多少个 socket」的真相：core 总数除以每个 socket 的 core 数。本讲所有 `VX_cluster` 实例化 `NUM_SOCKETS` 个 socket，源头就是这一行。`VX_cluster.sv` 与 `VX_socket.sv` 里用的 `NUM_SOCKETS`，都是从这里 `import` 来的。

**LLC 判定三连**。

[VX_gpu_pkg.sv:1363](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_gpu_pkg.sv#L1363)、[L1560](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_gpu_pkg.sv#L1560)、[L1586](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_gpu_pkg.sv#L1586)：

```verilog
localparam DCACHE_IS_LLC = !`VX_CFG_L2_ENABLED && !`VX_CFG_L3_ENABLED;
localparam L2_IS_LLC     = `VX_CFG_L2_ENABLED && !`VX_CFG_L3_ENABLED;
localparam L3_IS_LLC     = `VX_CFG_L3_ENABLED;
```

这三个互斥的布尔量决定「哪一级是最后一级 cache（LLC）」。回忆 4.1 里 L3 实例的 `IS_LLC (L3_IS_LLC)`、4.2 里 L2 的 `IS_LLC (L2_IS_LLC)`，源头就是这里。它也决定了 `WRITEBACK` 能否开启（只有 LLC 且是单一一致性点才能 write-back，见 cache_subsystem 文档）。注意 `L3_IS_LLC = L3_ENABLED`——只要开了 L3，它就一定是 LLC。

**顶层的 VX_MEM_PORTS**。

[VX_gpu_pkg.sv:1590-1594](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_gpu_pkg.sv#L1590-L1594)：

```verilog
localparam VX_MEM_PORTS       = L3_MEM_PORTS;
localparam VX_MEM_BYTEEN_WIDTH= L3_SECTOR_SIZE;
localparam VX_MEM_ADDR_WIDTH  = (`VX_CFG_MEM_ADDR_WIDTH - `CLOG2(L3_SECTOR_SIZE));
...
```

顶层 `Vortex.sv` 端口里那个 `[VX_MEM_PORTS]` 数组的宽度，等于 L3 的内存端口数。也就是说：**顶层对外暴露的 DRAM 通道宽度，由 L3 的端口数决定**。不开 L3 时 `L3_MEM_PORTS` 仍由宏给出（透明仲裁器也需定义端口数），所以顶层端口宽度恒定。

#### 4.4.4 代码实践

**实践目标**：体会「宏 → package」的桥接，并确认本讲引用的关键常量都在 pkg 里有据可查。

**操作步骤**：

1. 在 `hw/rtl` 下用 Grep 搜 `localparam NUM_SOCKETS`，确认它只定义于 `VX_gpu_pkg.sv`，其他文件都是 `import` 来的。
2. 在 `VX_cluster.sv` / `VX_socket.sv` 的模块头搜 `import VX_gpu_pkg`，确认它们确实 import 了该 package。
3. 阅读 [VX_define.vh:14-19](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_define.vh#L14-L19)，确认它 include 了 platform/config/types 三个 `.vh`。

**需要观察的现象**：`NUM_SOCKETS` 唯一定义点在 pkg；`VX_define.vh` 是宏的聚合入口；每个 `.sv` 都先 `` `include "VX_define.vh" `` 再 `import VX_gpu_pkg::*`。

**预期结果**：你能在脑中画出 4.4.2 的那张「宏 → package → 模块」流转图，并能解释「为什么不能直接在模块里用 `VX_CFG_NUM_CORES / VX_CFG_SOCKET_SIZE` 而要绕一道 package」——答案是为了得到带类型、可被工具检查、可被 `import` 复用的常量，而不是散落各处的字符串替换。

#### 4.4.5 小练习与答案

**练习 1**：`NC_BITS = CLOG2(NUM_CORES)`，而 `NC_WIDTH = UP(NC_BITS)`。为什么需要 `UP` 这一层？

**参考答案**：当 `NUM_CORES=1` 时 `CLOG2(1)=0`，于是 `NC_BITS=0`。若直接用 `[NC_BITS-1:0]` 做位宽，会得到 `[-1:0]` 这种非法/负位宽。`UP(x)` 宏把 0 抬到 1，保证 `NC_WIDTH` 至少为 1，得到合法的 `[0:0]`。这是 Vortex RTL 处理「单元素退化配置」的通用兜底。

**练习 2**：如果某个配置开了 L3，那么 dcache 的 `WRITEBACK` 会被允许吗？结合 4.1 的 AMO 断言回答。

**参考答案**：开了 L3 时 `L3_IS_LLC=1`、`L2_IS_LLC=0`、`DCACHE_IS_LLC=0`。dcache 不是 LLC，按 cache_subsystem 规则它不能 write-back；同时 4.1 的 [Vortex.sv:61-67](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/Vortex.sv#L61-L67) 断言：开了 AMO 且 L3 是 LLC 时，必须 `DCACHE_WRITEBACK=0`、`L2_WRITEBACK=0`。所以 dcache 在该配置下必须 write-through。

---

## 5. 综合实践：画出 socket→cluster→core 实例化层次与缓存共享边界

这是本讲的总练习，把四个最小模块串起来。

### 实践目标

在一张图上同时表达「实例化包含关系」与「缓存共享边界」，并标注总线名称。

### 操作步骤

1. 阅读顶层 [Vortex.sv:219-246](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/Vortex.sv#L219-L246)、cluster [VX_cluster.sv:394-444](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_cluster.sv#L394-L444)、socket [VX_socket.sv:450-513](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_socket.sv#L450-L513) 三段实例化循环。
2. 画一棵包含关系树（用一个具体配置算数字，例如 `NUM_CLUSTERS=2, NUM_CORES=4, SOCKET_SIZE=2`，则 `NUM_SOCKETS=UP(4/2)=2`）：
   - Vortex：实例化 2 个 cluster + 1 个 L3 + 1 个 KMU；
   - 每个 cluster：实例化 2 个 socket + 1 个 L2 + 1 个 gbar_unit；
   - 每个 socket：实例化 2 个 core + 1 个共享 icache + 1 个共享 dcache。
3. 在树上用三种颜色/记号标出三类共享边界：
   - **L1 边界**画在 socket 框内（icache+dcache 被 2 个 core 共享）；
   - **L2 边界**画在 cluster 框内（L2 被 2 个 socket 共享）；
   - **L3 边界**画在顶层框内（L3 被 2 个 cluster 共享）。
4. 在每条「层间连线」上标注总线名：cluster→顶层是 `mem_bus_if[L2_MEM_PORTS]`（L2 miss 上交）；socket→cluster 是 `mem_bus_if[L1_MEM_PORTS]`（L1 出口）；KMU 三级扇出分别叫 `per_cluster_kmu_bus_if` / `per_socket_kmu_bus_if` / `per_core_kmu_bus_if`。

### 需要观察的现象

- 同一份 RTL（`Vortex.sv`/`VX_cluster.sv`/`VX_socket.sv`）用 `genvar` 循环 + 配置宏，就能表达任意 `NUM_CLUSTERS × NUM_SOCKETS × SOCKET_SIZE` 的拓扑。
- 总线条数与命名呈现严格的「per_<下层>`」模式，便于追溯数据流。

### 预期结果

你应当得到一张类似下面的层次图（以 `NUM_CLUSTERS=2, NUM_SOCKETS=2, SOCKET_SIZE=2` 为例）：

```text
Vortex (top)
├── KMU ──kmu_arb(1→2)──► per_cluster_kmu_bus_if[2]
├── L3 (LLC, 全局共享) ◄── per_cluster_mem_bus_if  (来自 2 个 cluster 的 L2 miss)
├── cluster#0
│   ├── L2 (共享自 2 个 socket) ◄── socket_mem_bus_if  (来自 2 个 socket 的 L1 出口)
│   ├── gbar_unit (全局屏障会合点)
│   ├── socket#0
│   │   ├── icache + dcache (共享自 2 个 core) ◄── per_core_icache/dcache_bus
│   │   ├── mem_arb(icache 优先) ──► mem_bus_if[L1_MEM_PORTS] ──► 上交 L2
│   │   ├── core#0 ──(黑盒, 下讲拆)
│   │   └── core#1
│   └── socket#1 └── ... (同上)
└── cluster#1 └── ... (同 cluster#0)
```

并用一句话总结：**共享边界 = 该 cache 被实例化的那一层**。

> 若无法运行仿真核对数字，请据 `VX_config.toml` 默认值与 [VX_gpu_pkg.sv:144](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_gpu_pkg.sv#L144) 的公式手算，并标注「待本地验证」。

## 6. 本讲小结

- Vortex 的 RTL 硬件聚类分三级：**cluster（共享 L2）→ socket（共享 L1）→ core**；`NUM_SOCKETS = UP(NUM_CORES / SOCKET_SIZE)` 是这条链的关键派生量。
- 顶层 [`Vortex.sv`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/Vortex.sv) 只做拼装：实例化唯一的 KMU、唯一的 L3（全局 LLC）与 `NUM_CLUSTERS` 个 cluster，并用 `kmu_arb`/`dcr_arb` 做扇出；`busy` 是三级或运算。
- [`VX_cluster.sv`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_cluster.sv) 是 L2 共享层，汇聚 socket、DXA、图形 cache 三类流量进 L2，并托管全局屏障 `gbar_unit`；DXA 与 LSU 用优先级仲裁保护 core。
- [`VX_socket.sv`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_socket.sv) 是 L1 共享层，用 `VX_cache_cluster` 让 `SOCKET_SIZE` 个 core 共享 icache/dcache，并用 `mem_arb`（icache 优先 `"P"`）合并 L1 出口。
- [`VX_gpu_pkg.sv`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_gpu_pkg.sv) 是把 `VX_CFG_*` 宏桥接成带类型 localparam/typedef 的共享字典；`VX_define.vh` 是聚合 platform/config/types 三套宏的伞形头文件。
- 仲裁策略的通用钥匙：**卡流水线的路径（取指/LSU）用优先级 `"P"`，对等请求者用轮转 `"R"`**。

## 7. 下一步学习建议

本讲把 `VX_core` 当成黑盒。下一讲 **u7-l2 核心流水线各级 RTL** 会打开这个黑盒，对照 SimX 的 6 级流水线（u6 系列）在 `hw/rtl/core/` 下找到 `VX_fetch/VX_decode/VX_issue/VX_execute/VX_commit` 等模块。

建议提前做两件事：

1. 重读 u6 系列讲的 SimX 流水线（schedule→fetch→decode→issue→execute→commit），把每一级的输入输出结构体（`fetch_t`/`decode_t`/`ibuffer_t` 等，已在 [VX_gpu_pkg.sv:882-1037](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_gpu_pkg.sv#L882-L1037) 定义）记下来——RTL 各级之间传递的，正是这些结构体对应的物理管线寄存器。
2. 在 `hw/rtl/core/` 下用 `ls` 浏览文件名，预先猜一猜哪个 `.sv` 对应哪一级，下一讲来对答案。

读完 u7-l2 后，可以继续 u7-l3（调度器与 warp 控制的 RTL，对应 SimX 的 Scheduler/IBuffer/Scoreboard/IPDOM）与 u7-l4（SimX↔RTL model parity），把 RTL 与 SimX 的对应关系彻底打通。
