# 图与子图架构总览

## 1. 本讲目标

本讲是「AIE 图拓扑」单元的第一讲。在 [u2-l3](u2-l3-adf-graph-and-dataflow.md) 里我们已经建立了 ADF 图的基本抽象（`graph` / `kernel` / `port` / `connect`）和数据驱动执行的概念，并在高层概览中提到「1 个广播 + 7 个解复用 + 224 个重建」的两层结构。本讲要把这句话拆开，落到真实源码上：

学完本讲你应当能够：

- 说清 `BackProjectionGraph`（顶层图）和 `BackProjectionSubgraph`（子图）各自承担什么职责、各包含哪些内核。
- 解释 `bpCluster[AIE_SWITCHES]` 这个子图数组如何用一个 `AIE_SWITCHES` 宏把并行度复制若干份。
- 在默认配置（`AIE_SWITCHES=7`、`IMG_SOLVERS_PER_SWITCH=32`）下，准确统计三类内核的实例数量，并画出顶层与子图的包含关系。
- 理解「Data Broadcast 在顶层、Pixel Demux 与 Image Reconstruction 在子图」这种职责划分背后的原因。

本讲只讲**静态拓扑结构**（图里有谁、谁连到谁、数量怎么算），不深入包交换细节、端口位宽与广播布线的实现——那是 [u4-l2](u4-l2-pktswitch-instantiation-placement.md) 与 [u4-l3](u4-l3-ports-and-broadcast-wiring.md) 的内容；也不讲内核内部的算法——那是第 5 单元的内容。

## 2. 前置知识

阅读本讲前，请确认你已经掌握以下概念（来自前面几讲）：

- **ADF 图的四要素**（[u2-l3](u2-l3-adf-graph-and-dataflow.md)）：`graph` 是画布、`kernel` 是节点、`port` 是插口、`connect` 是连线。本讲大量出现 `kernel::create`、`connect(...)`、`source(...)`，默认你已经理解。
- **数据端口的两类语义**（[u2-l2](u2-l2-aie-data-movement.md)）：`buffer`（整块、框架自动 ping-pong 双缓冲）与 `stream`（逐 beat 连续流）。本讲的广播连线会再次出现 `single_buffer`，你需要记得它的作用是关闭双缓冲以省局部存储。
- **规模宏与整除约束**（[u1-l4](u1-l4-common-config-header.md)）：`AIE_SWITCHES=7`、`IMG_SOLVERS_PER_SWITCH=32`、`IMG_SOLVERS = AIE_SWITCHES × IMG_SOLVERS_PER_SWITCH = 224`、`PULSES=602`、`RC_SAMPLES=512`，以及「每核像素数 `(RC_SAMPLES×PULSES)/IMG_SOLVERS` 必须为整数」这条隐性约束。本讲的内核计数全部建立在这些宏之上。
- **C++ 成员构造顺序**：类的成员按**声明顺序**构造，与初始化列表的书写顺序无关。理解这一点才能看懂本讲中两个全局计数器为何能正确生成唯一编号。

如果你对上述任何一项还不熟，建议先回看对应讲义再继续。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲用它做什么 |
|------|------|----------------|
| [design/aie/graph.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h) | 定义 `BackProjectionSubgraph` 与 `BackProjectionGraph` 两个图类，是整张 AIE 图拓扑的唯一来源 | 本讲的主战场，逐段精读 |
| [design/aie/graph.cpp](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp) | 实例化顶层图对象，并在仿真宏下提供 `main()` 驱动整张图 | 用来确认「图被实例化几次」「主机如何喂数据」 |
| [design/aie/custom_kernels.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h) | 声明三个内核函数/类（`data_broadcast_kern`、`px_demux_kern`、`ImgReconstruct`）的签名 | 用来核对图里 `kernel::create` 绑定的函数与端口个数是否一致 |
| [design/common.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h) | 三域共享的规模宏 | 提供本讲所有的计数常数 |

> 提示：本仓库是多仓库项目之一，按 [u1-l2](u1-l2-repo-structure-and-test-data.md) 的说明，应经 versal-manifest 拉取依赖后再阅读。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：

- 4.1 **顶层图 `BackProjectionGraph`**：数据的入口与广播中心。
- 4.2 **子图 `BackProjectionSubgraph` 与 `bpCluster` 数组**：以子图为单位复制并行度。
- 4.3 **两类核心内核数组：`data_bc_km` 广播 与 `img_rec_km` 重建**。

### 4.1 顶层图 BackProjectionGraph：数据的入口与广播中心

#### 4.1.1 概念说明

整张反投影图在源码里就是一个 C++ 类 `BackProjectionGraph`，它继承自 `adf::graph`。它是**顶层图（top-level graph）**，承担两个不可下放的职责：

1. **接住主机从 DDR 送进来的原始雷达数据**。slowtime（天线几何）和 RC（距离压缩回波）两类数据都从主机经 GMIO 端口进入顶层图。
2. **把数据广播给所有图像重建内核**。反投影算法里，slowtime 和 RC 对**每一个**重建内核都是必需的（每个像素都要用全部脉冲的 RC 做相干累加），所以这两类数据必须从顶层扇出到全部 224 个重建内核。

为什么广播必须放在顶层？因为「广播」本质是「一份输入 → N 个输出」的扇出，而所有 224 个重建内核都跨在 7 个子图里。如果让每个子图自己接收 slowtime/RC，主机就得把同一份数据重复送 7 次，浪费 DDR 带宽。把广播提到顶层，主机只送一份，由 AIE 内部互联完成扇出。

顶层图里**只放一个**内核——`data_bc_km`（Data Broadcast 内核）。项目文档对它的描述是：「向所有 Image Reconstruction 内核分发 slow-time 雷达数据（天线坐标、到场景中心的距离）和距离压缩样本」。

#### 4.1.2 核心流程

顶层图构造时做的事情可以概括为下面这条流水：

```text
主机 DDR
   │  slowtime(天线 X/Y/Z + ref_range)
   │  rc(距离压缩复数样本)
   ▼
[gmio_in_st]  [gmio_in_rc]        ← 顶层 GMIO 端口(DDR↔AIE，经 NoC)
   │              │
   ▼              ▼
┌────────────────────────────┐
│  data_bc_km (Data Broadcast)│   ← 顶层唯一的内核
│   in[0]=slowtime(stream)    │
│   in[1]=rc(buffer)          │
│   out[0]=slowtime_out(stream)│  ──┐ 扇出到全部 224 个
│   out[1]=rc_out(buffer)     │  ──┘ 重建内核的 in[0]/in[1]
└────────────────────────────┘
   │              │
   └──────┬───────┘
          ▼
   bpCluster[0..AIE_SWITCHES-1]    ← 7 个子图(见 4.2)
```

此外顶层还持有一组 RTP（运行时参数）端口 `rtp_dump_img_in[IMG_SOLVERS]`，用来通知每个重建内核「是否把累加好的图像 dump 出去」。这部分会在 4.3 与后续讲义里展开。

#### 4.1.3 源码精读

先看顶层图的成员声明。`data_bc_km` 是私有内核，`bpCluster` 数组、GMIO 端口、RTP 端口是公有成员（公有是为了让主机和仿真 main 能访问到子图里的端口）：

数据广播内核与子图数组的声明（[design/aie/graph.h:L132-L153](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L132-L153)）：

```cpp
class BackProjectionGraph: public graph {
    private:
        // Data broadcaster kernel module
        kernel data_bc_km;          // 顶层唯一内核

    public:
        // Create multiple subgraphs of backprojection clusters
        BackProjectionSubgraph bpCluster[AIE_SWITCHES];  // 7 个子图

        input_gmio gmio_in_st;       // slowtime GMIO
        input_gmio gmio_in_rc;       // rc GMIO
        input_port rtp_dump_img_in[IMG_SOLVERS];  // 224 路 RTP
```

> 关键点：`bpCluster` 的类型是 `BackProjectionSubgraph`，元素个数是 `AIE_SWITCHES`（默认 7）。**把子图当成员数组嵌进顶层图**，是本项目实现「并行度可配置」的核心手法（详见 4.2）。

接着看构造函数里如何创建广播内核与两个输入 GMIO 端口（[design/aie/graph.h:L160-L172](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L160-L172)）：

```cpp
// Data broadcaster kernel
data_bc_km = kernel::create(data_broadcast_kern);

// Slow time splicer GMIO ports
gmio_in_st = input_gmio::create("gmio_in_st_" + std::to_string(bp_graph_insts), 256, 1000);
gmio_in_rc = input_gmio::create("gmio_in_rc_" + std::to_string(bp_graph_insts), 256, 1000);

// GMIO x, y, z and ref range to data broadcaster kernel
connect(gmio_in_st.out[0], data_bc_km.in[0]);
connect(gmio_in_rc.out[0], data_bc_km.in[1]);
```

`kernel::create(data_broadcast_kern)` 把 `data_broadcast_kern` 这个函数绑定成一个 ADF 节点。该函数的签名见 [design/aie/custom_kernels.h:L17-L20](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L17-L20)：两个输入（slowtime 流、rc 缓冲）+ 两个输出（slowtime 流、rc 缓冲）。

GMIO 端口名里出现了 `bp_graph_insts` 这个计数器——它会被拼成 `gmio_in_st_0`、`gmio_in_rc_0` 这样的唯一名字。这个计数器的来源在 [design/aie/graph.cpp:L7](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L7)：

```cpp
uint8_t bp_graph_insts = 0;
```

它配合 graph.h 顶部的 `extern` 声明（[design/aie/graph.h:L12-L13](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L12-L13)）工作。在顶层构造函数末尾有 `bp_graph_insts++`（[design/aie/graph.h:L203](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L203)），这样即便将来实例化多个顶层图，每个也能得到唯一编号。本设计当前只实例化 1 个（见 4.1.4）。

最后是最关键的**广播扇出连线**——把广播内核的两个输出连到所有 224 个重建内核（[design/aie/graph.h:L177-L184](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L177-L184)）：

```cpp
for (int j=0; j<AIE_SWITCHES; j++) {
    for (int i=0; i<IMG_SOLVERS_PER_SWITCH; i++) {
        connect(data_bc_km.out[0], bpCluster[j].img_rec_km[i].in[0]);  // slowtime
        connect(data_bc_km.out[1], bpCluster[j].img_rec_km[i].in[1]);  // rc
        single_buffer(bpCluster[j].img_rec_km[i].in[0]);
        single_buffer(bpCluster[j].img_rec_km[i].in[1]);
    }
}
```

读这段代码要抓住三件事：

1. 双重循环 `j × i` 遍历的是「7 个子图 × 每子图 32 个重建内核 = 224 个内核」，正好等于 `IMG_SOLVERS`。
2. `bpCluster[j].img_rec_km[i]`——顶层图**穿透子图边界**直接访问每个重建内核的端口。这说明子图并非封闭黑盒，它的内核端口对顶层是可见的公有成员。
3. `single_buffer(...)` 对广播 buffer 关闭默认 ping-pong 双缓冲。因为广播数据是「多读少写」（一份被 32 个内核读），双缓冲会无谓占用 32 份局部存储，所以改用单缓冲（[u2-l2](u2-l2-aie-data-movement.md) 已解释原理）。

#### 4.1.4 代码实践

**实践目标**：确认顶层图被实例化的次数，并理解 `INSTANCES` 这个常量。

**操作步骤**：

1. 打开 [design/aie/graph.cpp:L9-L10](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L9-L10)，读到：
   ```cpp
   const int INSTANCES = 1;
   BackProjectionGraph bpGraph[INSTANCES];
   ```
2. 在 graph.h 里搜索 `bp_graph_insts` 的所有出现位置（声明、`++`、拼名字），追踪它的取值变化。

**需要观察的现象**：

- 顶层图数组 `bpGraph` 当前只有 1 个元素，即整张图只被实例化一次。
- `bp_graph_insts` 在构造函数末尾才 `++`，因此构造第 0 个（也是唯一一个）顶层图期间它的值始终是 0，所有端口名都带 `_0` 后缀。

**预期结果**：仿真或硬件跑起来后，`Work` 目录里的端口名应是 `gmio_in_st_0`、`gmio_in_rc_0` 等。> 待本地验证（需 `make` 后查看生成的编译产物）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `INSTANCES` 改成 2，第二个顶层图里的 GMIO 端口会叫什么名字？为什么？

**参考答案**：会叫 `gmio_in_st_1`、`gmio_in_rc_1`。因为第一个顶层图构造结束时 `bp_graph_insts` 从 0 自增到 1，第二个图构造时拼名字用的就是 1。这正是用全局计数器生成唯一名的目的。

**练习 2**：广播扇出的双重循环里，为什么用 `AIE_SWITCHES` 和 `IMG_SOLVERS_PER_SWITCH` 两个上标，而不是直接用一个 `IMG_SOLVERS` 的单层循环？

**参考答案**：因为重建内核被组织在「子图数组 × 子图内数组」的二维结构里（`bpCluster[j].img_rec_km[i]`），下标必须拆成「第几个子图」和「子图内第几个内核」两维。换成单层循环会无法表达这个二维寻址。

---

### 4.2 子图 BackProjectionSubgraph 与 bpCluster 数组

#### 4.2.1 概念说明

`BackProjectionSubgraph` 是顶层图里被复制 7 份的那个子图类，项目里把它称为 **bpCluster**（backprojection cluster）。文档原文是：「包含 AIE Switch、Image Reconstruction 内核和 Pixel Demux 内核的计算单元叫做 bpCluster」。

为什么要把一组内核封装成子图、再用数组复制？这是为了**把并行度参数化**：

- 每个子图内部的结构是固定的：1 个 Pixel Demux + 32 个 Image Reconstruction + 一对 `pktsplit/pktmerge`。
- 想增加并行度时，不必逐个加内核、逐根加连线，只要把 `AIE_SWITCHES` 调大，`bpCluster[AIE_SWITCHES]` 数组就自动多出一整组结构相同的计算簇，连线由子图构造函数自动完成。

子图承担的职责是「**像素侧的解复用与重建**」，与顶层「slowtime/RC 广播」对称分工：

- 顶层：广播「对所有像素都一样」的数据（slowtime、RC）。
- 子图：分发「每个像素不一样」的数据（目标像素 XYZ），并真正做反投影计算。

每个子图有自己独立的输入端口（GMIO 进像素）和输出端口（PLIO 出图像），构成一条「像素进 → 解复用 → 32 路并行重建 → 合并 → 图像出」的完整流水。

#### 4.2.2 核心流程

一个 bpCluster 内部的数据流如下：

```text
主机 DDR(目标像素 XYZ)
   │
   ▼
[gmio_in_xyz_px]              ← 子图自己的 GMIO 端口
   │
   ▼
┌──────────────────┐
│ px_demux_km      │  Pixel Demux：把像素流按包头分到 32 路
│ in[0]=像素流     │
│ out[0]=打包像素流│
└──────────────────┘
   │
   ▼
┌──────────────────┐
│ pktsplit<32> (sp)│  包交换分流器：1 路进 → 32 路出
└──────────────────┘
   │  … 32 路 …
   ▼
┌─────────────────────────────────────────┐
│ img_rec_km[0..31]  (Image Reconstruction)│ ← 同时还从顶层收到
│   in[0]=slowtime(来自顶层广播)           │   slowtime(in[0])
│   in[1]=rc       (来自顶层广播)           │   rc(in[1])
│   in[2]=像素包   (来自 pktsplit)          │
│   in[3]=RTP dump (来自顶层)              │
│   out[0]=图像包                          │
└─────────────────────────────────────────┘
   │  … 32 路 …
   ▼
┌──────────────────┐
│ pktmerge<32> (mg)│  包交换合并器：32 路进 → 1 路出(可能乱序)
└──────────────────┘
   │
   ▼
[plio_pkt_rtr_out]            ← 子图自己的 PLIO 端口(到 PL 包路由器)
```

注意 `img_rec_km` 有 **4 个输入**：`in[0]/in[1]` 来自顶层广播（slowtime/rc），`in[2]` 来自本子图的 pktsplit（像素包），`in[3]` 来自顶层 RTP。这说明一个重建内核同时被顶层和本子图两条线喂数据——这正是把它放在子图里、但端口对顶层可见的原因。

#### 4.2.3 源码精读

子图类的成员声明（[design/aie/graph.h:L15-L41](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L15-L41)）：

```cpp
class BackProjectionSubgraph: public graph {
    public:
        kernel px_demux_km;                           // 1 个解复用内核
        kernel img_rec_km[IMG_SOLVERS_PER_SWITCH];    // 32 个重建内核

        pktsplit<IMG_SOLVERS_PER_SWITCH> sp;          // 1 分 32
        pktmerge<IMG_SOLVERS_PER_SWITCH> mg;          // 32 合 1

        input_gmio gmio_in_xyz_px;                    // 像素输入端口
        output_plio plio_pkt_rtr_out;                 // 图像输出端口
```

`pktsplit<32>` / `pktmerge<32>` 的模板参数就是 `IMG_SOLVERS_PER_SWITCH`。文档指出「packet splitter/merger 最多 32 路」，所以 `IMG_SOLVERS_PER_SWITCH` 的上限就是 32（这也是 common.h 注释里「max 32」的由来）。包交换的细节留待 [u4-l2](u4-l2-pktswitch-instantiation-placement.md)。

子图构造函数里创建 32 个重建内核的循环（[design/aie/graph.h:L52-L53](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L52-L53)）：

```cpp
for (int i=0; i<IMG_SOLVERS_PER_SWITCH; i++)
    img_rec_km[i] = kernel::create_object<ImgReconstruct>(IMG_SOLVERS_PER_SWITCH*bp_subgraph_insts + i);
```

这里有两点值得注意：

1. 用的是 `kernel::create_object<ImgReconstruct>(...)` 而不是普通的 `kernel::create`，因为 `ImgReconstruct` 是一个**类**（带构造函数参数 `int id`），不是普通函数。`create_object` 会调用 `ImgReconstruct::registerKernelClass()` 注册的成员函数作为入口，并用括号里的实参构造对象。
2. 传入的 id 是 `IMG_SOLVERS_PER_SWITCH*bp_subgraph_insts + i`。`bp_subgraph_insts` 是子图实例计数器（在 [design/aie/graph.h:L13](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L13) 定义为 0，在每个子图构造函数末尾自增，见 [design/aie/graph.h:L126](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L126)）。于是 7 个子图的 224 个内核拿到的 id 恰好是 0..223，全局唯一。这个 id 会被存进 `ImgReconstruct::m_id`（[design/aie/custom_kernels.h:L38](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L38)），最终用于 PL 侧把乱序的图像包写回正确的 DDR 偏移（见 [u6-l1](u6-l1-packet-router-hls-kernel.md)）。

> **关于 C++ 构造顺序的一个洞察**：`bpCluster[AIE_SWITCHES]` 是顶层图的成员数组，它的 7 个元素在**顶层图构造函数体执行之前**就按声明顺序依次构造了。因此当第 0 个子图构造时 `bp_subgraph_insts=0`，第 1 个时为 1，……第 6 个时为 6，构造完时变成 7。这套「构造期自增全局计数器」的模式，巧妙地让每个子图在没有任何外部编排代码的情况下，自动获得唯一编号。

子图内部的连线相对直白：GMIO → demux → pktsplit → 各重建内核 → pktmerge → PLIO（[design/aie/graph.h:L79-L97](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L79-L97)）：

```cpp
connect(gmio_in_xyz_px.out[0], px_demux_km.in[0]);   // 像素进 demux
connect(mg.out[0], plio_pkt_rtr_out.in[0]);          // 合并结果出子图

for (int i=0; i<IMG_SOLVERS_PER_SWITCH; i++)
    connect(sp.out[i], img_rec_km[i].in[2]);         // 分流到各重建核 in[2]
connect(px_demux_km.out[0], sp.in[0]);               // demux → splitter

for (int i=0; i<IMG_SOLVERS_PER_SWITCH; i++) {
    connect(img_rec_km[i].out[0], mg.in[i]);         // 各重建核 → merger
}
```

最后，每个子图还被**位置约束**钉在芯片上一块固定区域（[design/aie/graph.h:L99-L111](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L99-L111)）：

```cpp
int base_col = 0;
int col_start = base_col + bp_subgraph_insts * 7;   // 每个子图横向偏移 7 列
int col_end   = col_start + 6;
...
location<graph>(*this) = area_group({
    { aie_tile, col_start, row_start, col_end, row_end },
    { shim_tile, col_start, 0, col_end, 0 }
});
```

`col_start` 随 `bp_subgraph_insts` 每次加 7，所以 7 个子图在阵列上横向排开、互不重叠，每个占 7 列 × 8 行。这块约束保证 7 组计算簇在物理 tile 上不抢资源，也便于后续性能分析。`shim_tile` 那一行是为了给 PLIO 留出与 PL 对接的列。布局细节留待 [u4-l2](u4-l2-pktswitch-instantiation-placement.md)。

#### 4.2.4 代码实践

**实践目标**：验证「子图实例计数器」在 7 个子图构造期间确实走过了 0..6。

**操作步骤**：

1. 在 [design/aie/graph.h:L126](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L126) 的 `bp_subgraph_insts++;` 上方，临时加一行（**示例代码，仅供阅读理解，不要真的修改源码**）：
   ```cpp
   printf("subgraph %d built, col_start=%d\n", bp_subgraph_insts, col_start);
   ```
2. 在 [design/aie/graph.h:L65](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L65) 与 [L71-L72](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L71-L72) 处观察端口名如何用 `bp_graph_insts` 与 `bp_subgraph_insts` 拼接。

**需要观察的现象**：7 个子图依次构造，打印出的编号是 0,1,2,3,4,5,6；端口名形如 `gmio_in_xyz_px_0_0`、`gmio_in_xyz_px_0_1`、……、`gmio_in_xyz_px_0_6`（第一个 `_0` 是顶层图编号，第二个是子图编号）。

**预期结果**：`aiesimulator` 编译/运行后，`Work` 目录下应能看到 7 个形如 `aie_to_plio_switch_0_*.csv` 的 PLIO 数据文件名占位。> 待本地验证。

> 注意：按本讲义的写作约束，不应修改源码。上述 printf 仅为「阅读型实践」的示意；实际可通过阅读构造顺序与命名拼接逻辑，在纸上推导出结果，不一定要真改代码。

#### 4.2.5 小练习与答案

**练习 1**：`pktsplit<IMG_SOLVERS_PER_SWITCH>` 的模板参数为什么不能超过 32？

**参考答案**：因为 Versal 的 AXI4-Stream 包交换 splitter/merger 单个对象最多支持 32 路（项目文档与 common.h 注释都明确这一点）。一旦超过 32，`IMG_SOLVERS_PER_SWITCH` 就无法用一对 pktsplit/pktmerge 完成扇出/合并。

**练习 2**：如果想让并行度翻倍到 14 个 bpCluster、每子图仍 32 个重建核，需要改哪些宏？会带来什么连锁影响？

**参考答案**：把 `AIE_SWITCHES` 从 7 改成 14 即可（`IMG_SOLVERS_PER_SWITCH` 不变）。`IMG_SOLVERS` 会自动变成 14×32=448，于是 `bpCluster[14]`、重建内核总数 448、RTP 端口 `rtp_dump_img_in[448]` 都跟着变；同时 `PULSES` 必须重新选取，使 `(RC_SAMPLES×PULSES)/IMG_SOLVERS` 仍为整数（见 [u1-l4](u1-l4-common-config-header.md) 的整除约束）；tile 布局上 14 个子图横向需 14×7=98 列，要确认目标器件（VCK190 的 AIE 阵列）列数足够。

---

### 4.3 两类核心内核数组：data_bc_km 广播 与 img_rec_km 重建

#### 4.3.1 概念说明

前两节分别看了顶层和子图，本节把它们合到一起，聚焦两类**数量悬殊**的内核：

- **`data_bc_km`（Data Broadcast）**：全图**只有 1 个**，挂在顶层。它是「数据集散中心」，把主机送来的一份 slowtime + 一份 RC，扇出给全部重建内核。
- **`img_rec_km`（Image Reconstruction）**：全图**有 224 个**（默认配置），分散在 7 个子图里、每子图 32 个。它是真正干反投影计算的「算力主力」。

这两类内核的数量关系决定了整张图的并行结构。把它们和数量为 7 的 `px_demux_km`（每子图 1 个）放一起，就是本项目的三类 AIE 内核：

| 内核 | 所在层 | 数量公式 | 默认值 |
|------|--------|----------|--------|
| Data Broadcast (`data_bc_km`) | 顶层 | 固定 1 | 1 |
| Pixel Demux (`px_demux_km`) | 每子图 1 个 | `AIE_SWITCHES` | 7 |
| Image Reconstruction (`img_rec_km`) | 每子图 32 个 | `AIE_SWITCHES × IMG_SOLVERS_PER_SWITCH = IMG_SOLVERS` | 224 |
| **合计** | | `1 + AIE_SWITCHES + IMG_SOLVERS` | **232** |

这个「1 + 7 + 224」的分布对应了反投影算法本身的特性：**slowtime/RC 是共享数据（只需 1 个广播者），而像素是可分的（可以铺到几百个核上并行算）**。

#### 4.3.2 核心流程

把工作量的分配用数学写清楚。设：

- 像素总数 \(N_{px} = \text{PULSES} \times \text{RC\_SAMPLES}\)
- 重建内核总数 \(N_{solvers} = \text{IMG\_SOLVERS}\)

则每个重建内核负责的像素数为：

\[
N_{px/kern} = \frac{\text{PULSES} \times \text{RC\_SAMPLES}}{\text{IMG\_SOLVERS}}
\]

代入默认值：

\[
N_{px/kern} = \frac{602 \times 512}{224} = \frac{308224}{224} = 1376
\]

这正是每个 `ImgReconstruct` 对象内部累加缓冲 `m_img` 的大小（[design/aie/custom_kernels.h:L41](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L41)）：

```cpp
cfloat m_img[(PULSES*RC_SAMPLES)/IMG_SOLVERS];  // = 1376 个 cfloat
```

也就是说，224 个内核各持有一块 1376 元素的图像缓冲，加起来正好覆盖整幅 \(602 \times 512\) 的图像。这条等式能成立，靠的就是 [u1-l4](u1-l4-common-config-header.md) 强调的整除约束——否则整数除法会截断丢像素。

像素侧的分配还有一层「先到子图、再到内核」的两级划分。主机把像素按 switch 切成 7 段，每段交给一个 demux；demux 再分发给自己子图内的 32 个重建核：

\[
N_{px/switch} = \frac{\text{PULSES} \times \text{RC\_SAMPLES}}{\text{AIE\_SWITCHES}} = \frac{308224}{7} = 44032
\]

\[
N_{px/kern} = \frac{N_{px/switch}}{\text{IMG\_SOLVERS\_PER\_SWITCH}} = \frac{44032}{32} = 1376
\]

两级划分结果一致（1376），这并非巧合，而是 `IMG_SOLVERS = AIE_SWITCHES × IMG_SOLVERS_PER_SWITCH` 的代数必然。

#### 4.3.3 源码精读

主机侧把像素按 switch 切段的代码在仿真 main 里（[design/aie/graph.cpp:L179](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L179)）：

```cpp
int px_per_demux_kern = ((PULSES*RC_SAMPLES)/AIE_SWITCHES);  // 44032
```

随后在喂数据的循环里，每个 switch 拿到不重叠的一段（[design/aie/graph.cpp:L194-L196](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L194-L196)）：

```cpp
for (int sw_id=0; sw_id<AIE_SWITCHES; sw_id++) {
    bpGraph[inst].bpCluster[sw_id].gmio_in_xyz_px.gm2aie_nb(
        xyz_px_array + sw_id*px_per_demux_kern*3,        // ×3: 每像素 X/Y/Z 三个 float
        px_per_demux_kern*sizeof(float)*3);
}
```

`sw_id*px_per_demux_kern*3` 这个偏移（`×3` 是因为每个像素有 X、Y、Z 三个 float）保证 7 个子图拿到的是图像上**不重叠**的 7 段像素。这与重建内核各自累加到不重叠的 `m_img` 区段是一致的——224 个内核的 `m_img` 拼起来就是完整图像。

再看广播侧。`data_bc_km` 只有一个，但它的两个输出端口被连到 224 个目的端口（见 4.1.3 的双重循环）。这种「单源 → 多目的」正是 ADF 广播的写法——`connect` 同一个 `out` 到多个 `in`，框架会生成所需的广播互联。Data Broadcast 的两个输出语义不同（[design/aie/custom_kernels.h:L17-L20](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L17-L20)）：

```cpp
void data_broadcast_kern(input_stream<float>* slowtime_in,
                         input_buffer<cfloat, extents<RC_SAMPLES>>& rc_in,
                         output_stream<float>* slowtime_out,    // 流：扇出做 single_buffer
                         output_buffer<cfloat, extents<RC_SAMPLES>>& rc_out); // 缓冲：广播
```

- `out[0]`（slowtime）是 **stream**，逐 beat 流出，给重建内核的 `in[0]`。
- `out[1]`（rc）是 **buffer**（大小 `RC_SAMPLES`），整块广播，给重建内核的 `in[1]`。

顶层把这两个输出分别连到每个重建内核的 `in[0]` 与 `in[1]`，并对两者都施加了 `single_buffer`（[design/aie/graph.h:L181-L182](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L181-L182)）。slowtime/rc 为何一个走流、一个走缓冲、又为何都关双缓冲，是 [u4-l3](u4-l3-ports-and-broadcast-wiring.md) 与 [u5-l1](u5-l1-data-broadcast-kernel.md) 的主题；本节只需记住：**两类数据都从同一个 `data_bc_km` 广播到全部 224 个重建核**。

最后，把三类内核的数量在源码里逐一核对：

- `data_bc_km`：[graph.h:L160](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L160) 的 `kernel::create(data_broadcast_kern)` 调用 1 次 → **1 个**。
- `px_demux_km`：[graph.h:L49](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L49) 在子图构造函数里调用 1 次，子图被实例化 `AIE_SWITCHES=7` 次 → **7 个**。
- `img_rec_km`：[graph.h:L52-L53](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L52-L53) 的循环每子图跑 `IMG_SOLVERS_PER_SWITCH=32` 次，7 个子图 → **224 个**。

#### 4.3.4 代码实践

**实践目标**：用一个最小表格把「每个重建内核处理多少像素、缓冲多大」算清楚，并与源码里的 `m_img` 声明对账。

**操作步骤**：

1. 打开 [design/common.h:L17-L38](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/../common.h#L17-L38)，记下 `PULSES=602`、`RC_SAMPLES=512`、`AIE_SWITCHES=7`、`IMG_SOLVERS_PER_SWITCH=32`、`IMG_SOLVERS=224`。
2. 用计算器算 \(602 \times 512 / 224\)，确认等于 1376。
3. 打开 [design/aie/custom_kernels.h:L41](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L41)，核对 `m_img` 的数组大小表达式 `(PULSES*RC_SAMPLES)/IMG_SOLVERS` 与你算出的数一致。

**需要观察的现象 / 预期结果**：

| 量 | 公式 | 值 |
|----|------|----|
| 像素总数 | `PULSES × RC_SAMPLES` | 308224 |
| 每个重建核像素数 | `像素总数 / IMG_SOLVERS` | 1376 |
| `m_img` 大小 | `(PULSES*RC_SAMPLES)/IMG_SOLVERS` | 1376 个 cfloat |
| 每 switch 像素数 | `像素总数 / AIE_SWITCHES` | 44032 |
| 224 个 `m_img` 拼起来 | `1376 × 224` | 308224 = 像素总数 ✓ |

如果第 2 步算出来不是整数，说明 `PULSES` 取值违反了整除约束，图像会损坏（见 [u1-l4](u1-l4-common-config-header.md)）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `data_bc_km` 只有 1 个，而 `img_rec_km` 有 224 个？这种数量悬殊反映了反投影算法的什么特性？

**参考答案**：因为 slowtime 和 RC 是**所有像素共享**的输入数据（每个像素都要用全部脉冲的 RC 做累加），只需一份、由一个广播者扇出即可；而目标像素之间互相独立，可以把像素铺到大量内核上并行计算，所以重建核越多、并行度越高。这种「少广播、多计算」正是反投影「P×N 量级计算、但数据高度复用」特性的直接体现（见 [u1-l1](u1-l1-project-overview.md)）。

**练习 2**：`img_rec_km[i]` 的 4 个输入 `in[0..3]` 分别来自哪里？

**参考答案**：`in[0]`=slowtime（来自顶层 `data_bc_km.out[0]` 广播）、`in[1]`=rc（来自顶层 `data_bc_km.out[1]` 广播）、`in[2]`=目标像素包（来自本子图 `pktsplit`）、`in[3]`=RTP dump 开关（来自顶层 `rtp_dump_img_in[...]`）。前两个和第四个跨子图边界来自顶层，第三个来自子图内部。

**练习 3**：如果把 `RC_SAMPLES` 改成 256（common.h 支持的合法值之一），每个重建核处理的像素数变成多少？整除约束还满足吗？

**参考答案**：像素总数变成 \(602 \times 256 = 154112\)，每核 \(154112 / 224 = 688\)，是整数，整除约束仍满足（因为 602 是 7 的倍数，224=7×32，只要 `RC_SAMPLES` 是整数，`(RC_SAMPLES×602)/224 = RC_SAMPLES×(602/224) = RC_SAMPLES × (301/112)`……需逐一核算；对 RC_SAMPLES=256：256×602=154112，/224=688，整数 ✓）。`m_img` 也随之缩成 688 个 cfloat。

---

## 5. 综合实践

**任务**：按 `main` 分支默认配置（`AIE_SWITCHES=7`、`IMG_SOLVERS_PER_SWITCH=32`），完成下面两件事，把本讲的三类内核与两层图结构串起来。

**第一部分——内核清点**：

打开 [design/aie/graph.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h)，逐个统计以下三类内核在默认配置下的实例总数，并写出统计依据（哪一行代码、循环几次）：

| 内核类型 | 实例数 | 统计依据（行号 + 推理） |
|----------|--------|------------------------|
| Data Broadcast | 1 | 顶层构造函数调用 `kernel::create` 一次（L160） |
| Pixel Demux | 7 | 子图构造函数调用一次（L49）× `AIE_SWITCHES=7` 个子图 |
| Image Reconstruction | 224 | 子图内循环 32 次（L52-53）× 7 个子图 |
| **总计** | **232** | 1 + 7 + 224 |

**第二部分——画包含关系图**：

用纸笔或任意画图工具，画出「顶层图 ↔ 子图 ↔ 内核」的包含关系。要求：

1. 最外层画一个大框，标 `BackProjectionGraph`（顶层）。
2. 在顶层里画 1 个 `data_bc_km` 框，和 7 个并排的 `bpCluster[j]` 子框（`j=0..6`）。
3. 每个 `bpCluster[j]` 子框里画 1 个 `px_demux_km` + 1 对 `pktsplit/pktmerge` + 32 个 `img_rec_km[i]`（32 个可以用一个标了「×32」的框表示）。
4. 用箭头标出三类数据流：
   - 顶层 `gmio_in_st`/`gmio_in_rc` → `data_bc_km` →（穿透子图边界）→ 每个 `img_rec_km.in[0]/in[1]`。
   - 子图 `gmio_in_xyz_px` → `px_demux_km` → `pktsplit` → `img_rec_km.in[2]`。
   - `img_rec_km.out[0]` → `pktmerge` → `plio_pkt_rtr_out`。
5. 在图旁注明每类内核的总数（1 / 7 / 224）和每个重建核的像素数（1376）。

**预期结果**：一张能让人一眼看出「1 个广播内核在顶层、224 个重建内核分散在 7 个子图里、slowtime/rc 从顶层广播下去、像素从子图进来」的结构图。这是后续阅读包交换（[u4-l2](u4-l2-pktswitch-instantiation-placement.md)）、端口布线（[u4-l3](u4-l3-ports-and-broadcast-wiring.md)）与内核算法（第 5 单元）的「地图」。

> 如果想验证你的图是否画对，可以对照项目文档 `doc/sections/implementation.tex` 里 Figure `fig:aie_graph` 的描述（文档用的是 `AIE_SWITCHES=2`、`IMG_SOLVERS_PER_SWITCH=2` 的小例子来画示意图，结构关系与本讲默认配置完全一致，只是数量少）。

## 6. 本讲小结

- 整张反投影图分为两层：**顶层 `BackProjectionGraph`** 负责接收 slowtime/RC 并广播；**子图 `BackProjectionSubgraph`（即 bpCluster）** 负责像素解复用与重建。
- 顶层用 `bpCluster[AIE_SWITCHES]` 数组把一整组计算簇复制 `AIE_SWITCHES`（默认 7）份，并行度由这一个宏参数化控制。
- 三类 AIE 内核的数量是 **1（广播）+ 7（解复用）+ 224（重建）= 232**，对应算法「共享数据少广播、独立像素多并行」的特性。
- 重建内核总数 `IMG_SOLVERS = AIE_SWITCHES × IMG_SOLVERS_PER_SWITCH`，每个核处理 `(PULSES×RC_SAMPLES)/IMG_SOLVERS = 1376` 个像素，由 `m_img` 缓冲持有。
- 顶层**穿透子图边界**把 slowtime/rc 广播到每个 `img_rec_km.in[0]/in[1]` 并施加 `single_buffer`；RTP 也从顶层连到每个重建核的 `in[3]`。
- 两个全局计数器 `bp_graph_insts` / `bp_subgraph_insts` 利用 C++ 构造顺序，自动为端口与内核实例生成全局唯一编号。

## 7. 下一步学习建议

本讲只画了拓扑的「骨架」。接下来建议：

- **[u4-l2 包交换、内核实例化与 tile 布局](u4-l2-pktswitch-instantiation-placement.md)**：深入本讲只点到为止的 `pktsplit/pktmerge`、`create_object` 传 id、以及 `location<graph> area_group` 把每个 bpCluster 钉到 7 列 × 8 行 tile 区域的细节。
- **[u4-l3 GMIO/PLIO 端口与广播布线](u4-l3-ports-and-broadcast-wiring.md)**：深入四类端口（`gmio_in_st`/`gmio_in_rc`/`gmio_in_xyz_px`/`plio_pkt_rtr_out`）的位宽、数据文件绑定，以及 `single_buffer` 对广播连线的具体影响。
- 等第 4 单元三讲都读完，再进入**第 5 单元**看三类内核内部到底怎么算（广播搬运、解复用打包、重建的差分距离—相位校正—插值累加三段）。
