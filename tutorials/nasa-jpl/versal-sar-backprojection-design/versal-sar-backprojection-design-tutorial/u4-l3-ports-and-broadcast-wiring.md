# GMIO/PLIO 端口与广播布线

## 1. 本讲目标

在前两讲里，我们已经知道整张反投影图「长什么样」（u4-l1 的两层图与 1+7+224 个内核）、以及「包交换和 tile 布局怎么落地」（u4-l2）。但图里的内核还只是孤岛——它们要收到数据、送出结果，必须靠**端口（port）**与**连线（connect）**接到外部世界。

本讲就聚焦在这两件事上。读完本讲，你应当能够：

1. 说清 `input_gmio`（slowtime / RC / 目标像素三类输入）与 `output_plio`（图像输出）这四类端口是**在哪里、用什么参数、按什么命名规则**创建出来的。
2. 解释 `connect` 如何把一个端口的输出接到一个内核的输入，以及 `single_buffer` 为什么专门加在广播连线上。
3. 跟着源码把「1 个 Data Broadcast 内核的 2 个输出 → 224 个 Image Reconstruction 内核」这条广播扇出连线一帧一帧走通，并据此回答：**为什么 slowtime/RC 用 `connect + single_buffer`，而目标像素却要绕道包交换？**

本讲只看图的「接线」，不展开内核内部算法（那是 u5 的事），也不展开主机如何喂数据（u3-l5 已讲过）。

## 2. 前置知识

本讲假设你已经掌握下面几个概念（若生疏可回看对应讲义）：

- **GMIO 与 PLIO 的区别**（u2-l2）：GMIO 是 DDR↔AIE、**经 NoC** 的 DMA 通道；PLIO 是 AIE↔PL 的 **AXI4-Stream 直连**流，不走 NoC。判断口径：是否涉及 DDR。
- **buffer 端口 vs stream 端口**（u2-l2）：buffer（窗口）整块传输、框架默认做 ping-pong 双缓冲；stream 逐 beat 连续流动。
- **图的层次与内核角色**（u4-l1）：顶层 `BackProjectionGraph` 有一个 `data_bc_km`（Data Broadcast）；每个 `BackProjectionSubgraph`（即 bpCluster，共 7 个）有 1 个 `px_demux_km`、32 个 `img_rec_km`，外加 `pktsplit<32>`/`pktmerge<32>`。
- **广播判据**（u4-l2 已给结论）：每个内核**拿同一份数据**（广播）→ 用 `connect + single_buffer`；每个内核**拿不同数据**（按核切分）→ 走包交换 `pktstream`。本讲要用源码把这条判据**证出来**。
- **两个全局计数器**（u4-l2）：`bp_graph_insts`（顶层图实例号）与 `bp_subgraph_insts`（子图实例号），靠 C++ 成员按声明顺序构造而自增，本讲会看到它们被用来给端口生成**唯一名字**。

一句话复习数据流：主机把 **slowtime（天线几何，4 个 float）**、**RC（距离压缩样本，512 个 cfloat）**、**目标像素（X/Y/Z）** 三类数据从 DDR 经 GMIO 灌进图，图里把图像结果经 PLIO 流给 PL 包路由器。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [design/aie/graph.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h) | ADF 图的声明与构造（拓扑、端口、连线、布局） | 端口 `create`、`connect`、`single_buffer` 的全部代码 |
| [design/aie/graph.cpp](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp) | 仿真 `main`：实例化图、读 CSV、用 GMIO 驱动图 | 主机侧如何向这四类端口投数据（对应端口的数据来源） |
| [design/aie/custom_kernels.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h) | 三个内核的函数签名 | `in[0..3]` / `out[0..1]` 各是哪种端口类型（决定连线方式） |
| [design/common.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h) | 全局配置宏 | 端口数量、buffer 大小所依赖的 `AIE_SWITCHES`、`IMG_SOLVERS_PER_SWITCH` 等 |

---

## 4. 核心概念与源码讲解

### 4.1 端口创建：input_gmio 与 output_plio

#### 4.1.1 概念说明

ADF 图是一个封闭的进程网络：内核（`kernel`）之间可以互相连线，但**内核要想到达芯片外部**（读 DDR、或与 PL 对接），必须经过两类「对外插座」：

- **`input_gmio` / `output_gmio`**：AIE 与 DDR 之间的 DMA 通道，数据走片上网络 NoC。主机（或仿真 `main`）通过 `gm2aie_nb` 把 DDR 里的数据推进来，或用 `aie2gm_nb` 把结果拉回 DDR。
- **`input_plio` / `output_plio`**：AIE 与 PL（可编程逻辑）之间的 AXI4-Stream 直连，**不走 NoC、不进 DDR**，以固定位宽（如 128 位）逐 beat 流动。

本设计有三类输入、一类输出，正好把这两种端口都用上：

| 端口对象 | 类型 | 所在图 | 数量（默认配置） | 搬运的数据 |
| --- | --- | --- | --- | --- |
| `gmio_in_st` | `input_gmio` | 顶层图 | 1 | slowtime（天线 X/Y/Z + ref_range，`BC_ELEMENTS`=4 个 float） |
| `gmio_in_rc` | `input_gmio` | 顶层图 | 1 | RC 距离压缩样本（`RC_SAMPLES`=512 个 cfloat） |
| `gmio_in_xyz_px` | `input_gmio` | 子图 bpCluster | 7（每簇 1 个） | 目标像素 X/Y/Z（float 三元组） |
| `plio_pkt_rtr_out` | `output_plio` | 子图 bpCluster | 7（每簇 1 个） | 重建后的图像（cfloat，128 位流） |

为什么 slowtime/RC/像素都走 GMIO？因为它们都来自 DDR（由主机从 CSV 读入后写入 device buffer，见 u3-l2/u3-l3）。为什么图像输出走 PLIO？因为图像要交给 PL 上的包路由器做重排（u6-l1），AIE↔PL 是直连流，没必要绕 DDR。

注意一个关键分布：**slowtime 和 RC 的 GMIO 放在顶层图（各 1 个），而像素 GMIO 放在每个子图里（7 个）**。原因下文会讲——前两者是全内核共享的广播数据，只需一个入口；后者是按簇切分的逐核数据，每簇要一个独立入口。

#### 4.1.2 核心流程

端口创建分三步，在图的构造函数里完成：

1. **起名字**：用字符串拼接出一个**全局唯一**的逻辑名（名字里编入实例号）。
2. **调 `create`**：用 `input_gmio::create(名字, 突发长度, 在途事务数)` 或 `output_plio::create(名字, 位宽, 仿真数据文件)` 实例化端口对象。
3. **（仅 PLIO）绑数据文件**：给 PLIO 指定一个 `.csv` 文件名，仿真时流数据会写入/读出该文件——这就是日后 PL 仿真 testbench 的入口（见 u6-l2）。

GMIO 不需要数据文件，因为它的数据由主机在运行时经 `gm2aie_nb` 动态投递；PLIO 则在仿真里靠文件 I/O 来模拟那条 AXI4-Stream。

#### 4.1.3 源码精读

先看**子图里**的像素 GMIO 与图像 PLIO 的创建（位于 `BackProjectionSubgraph` 构造函数）：

[design/aie/graph.h:65-73](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L65-L73) —— 用两个实例号编出唯一名字，再 `create` 出像素 GMIO 与图像 PLIO：

```cpp
std::string xyz_px_str = "gmio_in_xyz_px_" + std::to_string(bp_graph_insts) + "_" + std::to_string(bp_subgraph_insts);
gmio_in_xyz_px = input_gmio::create(xyz_px_str.c_str(), 256, 1000);

std::string plio_data_file_str = "aie_to_plio_switch_" + std::to_string(bp_graph_insts) + "_" + std::to_string(bp_subgraph_insts) + ".csv";
std::string plio_pkt_rtr_str   = "plio_pkt_rtr_out_" + std::to_string(bp_graph_insts) + "_" + std::to_string(bp_subgraph_insts);
plio_pkt_rtr_out = output_plio::create(plio_pkt_rtr_str.c_str(), plio_128_bits, plio_data_file_str.c_str());
```

几个要点：

- `input_gmio::create` 的后两个整数：`256` 是 DMA 突发长度（字节，ADF 允许的最大值），`1000` 是允许的**在途事务数（outstanding transactions）**，二者共同决定这条 GMIO 通道的吞吐与缓冲深度。
- `output_plio::create` 的 `plio_128_bits` 指定这条 AXI4-Stream 是 **128 位宽**——这与 PL 包路由器内核的 `ap_axiu<128>` 接口位宽严格对齐（见 u6-l1），位宽不一致会导致编译期或链路错误。
- PLIO 绑定的文件名 `aie_to_plio_switch_<g>_<s>.csv`：在 `aiesim`/`x86sim` 里，这个簇输出的图像流会落到该文件；PL testbench 正是读这一组 CSV 来验证重排（u6-l2）。
- 名字里编了 `bp_graph_insts`（顶层实例号）和 `bp_subgraph_insts`（子图实例号），保证 7 个簇的端口互不重名。

再看**顶层图里** slowtime/RC 两个 GMIO 的创建：

[design/aie/graph.h:165-166](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L165-L166) —— 顶层 GMIO 只编入顶层实例号（因为顶层每图只有一份）：

```cpp
gmio_in_st = input_gmio::create("gmio_in_st_" + std::to_string(bp_graph_insts), 256, 1000);
gmio_in_rc = input_gmio::create("gmio_in_rc_" + std::to_string(bp_graph_insts), 256, 1000);
```

这两个端口**没有**编入 `bp_subgraph_insts`，因为它们声明在顶层图、整个图只有一对。而像素 GMIO 声明在子图里（[design/aie/graph.h:35](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L35)），随 7 个子图实例化成 7 份，所以名字里必须再加子图号去重。

> 命名到系统级的延续：这些 PLIO 名字会被 Makefile 在生成 `system.cfg` 时直接引用。例如 [Makefile:210-212](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L210-L212) 会为每个簇生成 `stream_connect=ai_engine_0.plio_pkt_rtr_out_0_$i:dma_pkt_router_$i.pl_stream_in`，把 AIE 的 PLIO 流接到 PL 包路由器内核的 `pl_stream_in`。这里的 `plio_pkt_rtr_out_0_$i` 正对应本讲创建的 `plio_pkt_rtr_out_<bp_graph_insts=0>_<bp_subgraph_insts=$i>`。命名一致，链路才通。

最后，确认端口对象本身的声明位置：顶层 GMIO 在 [design/aie/graph.h:149-150](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L149-L150)，子图 GMIO/PLIO 在 [design/aie/graph.h:35](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L35) 与 [design/aie/graph.h:41](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L41)。

#### 4.1.4 代码实践

**实践目标**：核验端口数量与命名，并理解仿真 `main` 如何向这些端口投数据。

**操作步骤**：

1. 在 [design/aie/graph.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h) 中数出：顶层图声明了几个 `input_gmio`？子图声明了几个 `input_gmio` 和几个 `output_plio`？
2. 结合 `AIE_SWITCHES=7`（[design/common.h:31](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L31)），计算整张图总共有几个 GMIO 输入端口、几个 PLIO 输出端口。
3. 打开 [design/aie/graph.cpp:188](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L188) 与 [design/aie/graph.cpp:193-196](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L193-L196)，对照仿真 `main` 里 `gm2aie_nb` 调用，确认每类端口的数据来源。

**需要观察的现象**：

- 仿真 `main` 对 `gmio_in_st` 只调用**一次** `gm2aie_nb`（整块 slowtime），对 `gmio_in_rc` **每脉冲调一次**，对 `gmio_in_xyz_px` **每簇、每脉冲各调一次**且传入的指针偏移随 `sw_id` 递增。

**预期结果**：

- 顶层 2 个 GMIO（`gmio_in_st`、`gmio_in_rc`）+ 子图 7 个像素 GMIO = **共 9 个 GMIO 输入**；7 个 PLIO 输出。
- 像素投递的指针 `xyz_px_array + sw_id*px_per_demux_kern*3` 表明：**每簇拿到的是像素数组的一段不同切片**，这正是像素端口必须「每簇一个」、而 slowtime/RC 只需「全局一个」的根本原因。

> 说明：本实践为源码阅读型，不依赖硬件；若要在本地运行 `aiesim` 复现，需先按 u1-l3 配好 Vitis 环境与 PLATFORM。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `AIE_SWITCHES` 从 7 改成 4，整张图的 GMIO 输入端口和 PLIO 输出端口各变成几个？

**参考答案**：GMIO 输入 = 顶层 2（slowtime+RC）+ 子图 4（像素）= **6 个**；PLIO 输出 = 子图数 = **4 个**。slowtime/RC 不随簇数变化，像素与图像端口随簇数线性变化。

**练习 2**：为什么 `gmio_in_st` 的名字里只编了 `bp_graph_insts`，而 `gmio_in_xyz_px` 的名字里却编了两个实例号？

**参考答案**：`gmio_in_st` 声明在顶层图，每个顶层图实例只有一份，只需顶层号去重；`gmio_in_xyz_px` 声明在子图，会随每个 bpCluster 实例化一份（默认 7 份），若不加子图号，7 个像素端口会重名，ADF 编译会报错。

---

### 4.2 connect 与 single_buffer

#### 4.2.1 概念说明

端口只是「插座」，还要用**连线 `connect(源, 目的)`** 把插座和内核的输入输出针脚接起来。ADF 里一条 `connect` 可以是：

- **普通流/窗口连接**：把一个端口的 `out[0]` 接到一个内核的 `in[k]`；或把一个内核的 `out[k]` 接到另一个内核的 `in[m]`。
- **广播连接（fan-out）**：把**同一个源**接到**多个目的**——框架会自动复制数据，让每个目的都收到一份。
- **参数连接**：`connect<parameter>(...)`，用来把运行时参数（RTP）接到内核。

本讲的第二个重点是 `single_buffer`。它只对 **buffer（窗口）端口**有意义：默认情况下，buffer 连接走 **ping-pong 双缓冲**——框架在局部存储里维护两份窗口，一份正在被内核读、另一份正在被 DMA 写，二者交替以隐藏延迟。`single_buffer(内核.in[k])` 这条指令**关掉双缓冲、只保留一份窗口**。

为什么广播连接上几乎总要加 `single_buffer`？因为广播数据是**只读、全内核共享**的同一份内容，没有「生产者写新数据时消费者还在读旧数据」的交替需求；关掉双缓冲可以**省下局部存储**（每个 AIE tile 只有 32 KB，见 u2-l1）。后面会算一笔账：RC 窗口一价新就是 4 KiB，双缓冲就是 8 KiB，乘以广布的内核数量，开销不可忽视。

#### 4.2.2 核心流程

`connect` / `single_buffer` 的使用流程：

1. 先看**内核签名的端口类型**（buffer 还是 stream/pktstream）——这决定能不能加 `single_buffer`，也决定能不能做广播。
2. 用 `connect(源.out[k], 目的.in[m])` 建立点到点连线；需要广播时，对每个目的各写一条 `connect`（源相同）。
3. 对广播里的 buffer 目的，紧跟一句 `single_buffer(目的.in[m])`。

关键判据（再次印证 u4-l2 的结论）：

- **同一份数据给每个内核** → buffer 端口 + `connect` 扇出 + `single_buffer`。
- **不同数据给每个内核** → 不能用普通 `connect` 扇出（那样每个内核会拿到一样的数据）；改用**包交换 `pktstream`**，靠包头 `pkt_id` 在一条物理流上复用多路逻辑流。

#### 4.2.3 源码精读

先确认三个内核的端口签名，这是理解连线的地基。[design/aie/custom_kernels.h:14-30](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L14-L30)：

```cpp
void px_demux_kern(input_stream<float>*  px_xyz_in,        // in[0]  stream
                   output_pktstream      *px_xyz_out);      // out[0] pktstream

void data_broadcast_kern(input_stream<float>* slowtime_in,  // in[0]  stream
                         input_buffer<cfloat, extents<RC_SAMPLES>>& rc_in,  // in[1]  buffer
                         output_stream<float>* slowtime_out, // out[0] stream
                         output_buffer<cfloat, extents<RC_SAMPLES>>& rc_out);// out[1] buffer

// ImgReconstruct::img_reconstruct_kern
    input_buffer<float,  extents<BC_ELEMENTS>>&  slowtime_in, // in[0] buffer
    input_buffer<cfloat, extents<RC_SAMPLES>>&   rc_in,       // in[1] buffer
    input_pktstream       *px_xyz_in,                          // in[2] pktstream
    output_pktstream      *img_out,                            // out[0] pktstream
    int                    rtp_dump_img_in);                   // in[3] parameter(RTP)
```

把签名里的类型映射到内核的 `in[k]`/`out[k]` 序号（按声明顺序），就得到一张「针脚表」：

| 内核 | in[0] | in[1] | in[2] | in[3] | out[0] | out[1] |
| --- | --- | --- | --- | --- | --- | --- |
| `data_bc_km` | slowtime（stream） | rc（buffer） | — | — | slowtime（stream） | rc（buffer） |
| `px_demux_km` | 像素（stream） | — | — | — | 像素（pktstream） | — |
| `img_rec_km` | slowtime（buffer） | rc（buffer） | 像素（pktstream） | RTP（parameter） | 图像（pktstream） | — |

这张表立刻说明一个要点：`data_bc_km` 的输出与 `img_rec_km` 的输入**端口类型并不完全相同**（比如 slowtime 在广播端是 `output_stream`、在重建端是 `input_buffer`）。ADF 框架会在连线处自动做 **stream↔buffer 的转换**——这正是 Data Broadcast 「把 slowtime 经 stream 转出以触发广播」的实现机制（细节留待 u5-l1）。

接着看顶层图里**点到点**的 `connect`：把两个 GMIO 接到 `data_bc_km` 的前两个输入。

[design/aie/graph.h:171-172](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L171-L172) —— GMIO → Data Broadcast：

```cpp
connect(gmio_in_st.out[0], data_bc_km.in[0]);   // slowtime
connect(gmio_in_rc.out[0], data_bc_km.in[1]);   // rc
```

再看 `single_buffer` 的现身之处。它紧跟在广播 `connect` 之后，对每个重建内核的前两个输入各加一次：

[design/aie/graph.h:181-182](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L181-L182) —— 关掉广播窗口的双缓冲：

```cpp
single_buffer(bpCluster[j].img_rec_km[i].in[0]);   // slowtime 窗口
single_buffer(bpCluster[j].img_rec_km[i].in[1]);   // rc 窗口
```

注意：`single_buffer` 只加在 `in[0]`/`in[1]`（buffer 端口）上，**没有**加在 `in[2]`（pktstream 像素端口）上——因为 `single_buffer` 是 buffer 概念，对包流无意义。

算一笔存储账（用 [design/common.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h) 的宏）：

- RC 窗口大小：\( \text{RC\_SAMPLES} \times \text{sizeof(cfloat)} = 512 \times 8 = 4096 \text{ 字节} = 4\,\text{KiB} \)。
- slowtime 窗口：\( \text{BC\_ELEMENTS} \times \text{sizeof(float)} = 4 \times 4 = 16 \text{ 字节} \)（可忽略）。

若不做 `single_buffer`，每个重建内核的 RC 窗口会双缓冲占 \( 8\,\text{KiB} \)；而每个 tile 局部存储只有 32 KB，还要放 `m_img` 累加缓冲、像素缓冲、栈等。广播场景下数据只读共享，双缓冲带来的那点延迟隐藏收益远抵不上存储代价，故一律 `single_buffer`。

#### 4.2.4 代码实践

**实践目标**：把「针脚类型」与「是否加 `single_buffer`」的对应关系在源码里坐实。

**操作步骤**：

1. 在 [custom_kernels.h:17-20](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L17-L20) 确认 `data_broadcast_kern` 的 `rc_in` 是 `input_buffer<...>`。
2. 在 [custom_kernels.h:26-30](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L26-L30) 确认 `img_reconstruct_kern` 的 `in[2]`（像素）是 `input_pktstream`，不是 buffer。
3. 在 [graph.h:177-184](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L177-L184) 全局搜索 `single_buffer`，确认它只作用于 `in[0]`/`in[1]`。

**需要观察的现象**：

- 源码里**没有任何一行** `single_buffer(...in[2])`。这与像素走包交换、不碰 buffer 双缓冲完全自洽。

**预期结果**：

- 得出结论：`single_buffer` 只出现在 buffer 类型的广播输入上；stream / pktstream 端口从不出现 `single_buffer`。这正是判据在源码层的体现。

> 说明：本实践为源码阅读型，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：默认 `single_buffer`（双缓冲）的好处是什么？为什么本设计在广播连线上反而要关掉它？

**参考答案**：双缓冲让「DMA 写下一块」与「内核读当前块」并行，隐藏访存延迟。但广播数据是只读共享的同一份，没有「新旧交替」的需求；关掉它能省一半窗口存储（RC 窗口从 8 KiB 降回 4 KiB），在 32 KB 的 tile 局部存储预算下很划算。

**练习 2**：假如某个初学者「为了省事」给像素端口 `img_rec_km.in[2]` 也加一句 `single_buffer`，会发生什么？

**参考答案**：`in[2]` 是 `input_pktstream`（包流），`single_buffer` 只对 buffer/window 端口有效，对包流使用属于接口不匹配，编译期会被 ADF 工具拒绝（报参数类型错误）。这也反向印证了像素必须走包交换、而非 buffer 广播。

---

### 4.3 广播扇出连线：从 2 个源到 224 个内核

#### 4.3.1 概念说明

本模块把前两节串起来，走通整张图的「接线全景」。核心是一条**广播扇出**：顶层那一个 Data Broadcast 内核，有 2 个输出（slowtime、RC），要喂给 **全部 224 个** Image Reconstruction 内核（7 簇 × 32）。也就是说，从 2 个源端口扇出到 \( 2 \times 224 = 448 \) 条 `connect` 连线。

这里有一个本设计最漂亮的取舍，恰好回答了实践任务里的问题——**为什么 slowtime/RC 用 `connect + single_buffer`，而像素用包交换？**

- **slowtime / RC**：每个重建内核需要**完全相同**的那份天线几何与距离样本。这是天然的广播场景：一个源，多份相同副本。用 `connect` 扇出 + `single_buffer` 共享只读窗口即可，无需给每个内核单独传一份。
- **目标像素**：每个重建内核负责**不同**的像素子集（各自处理 1376 个像素，见 u4-l1）。若用普通 `connect` 扇出，每个内核会收到**同样的**像素，与需求相悖。所以像素必须走**包交换**：demux 内核给每路输出打上 `pkt_id` 包头，`pktsplit` 按包头把不同像素分发到不同内核——一条物理流复用 32 路逻辑流。

一句话：**广播（同份）用 buffer+single_buffer，切分（异份）用 pktstream**。这条判据在 u4-l2 已给出，本节用源码把它落到每一根线上。

#### 4.3.2 核心流程

完整连线分三组，按数据流向排列：

1. **输入侧（GMIO → 内核）**
   - slowtime：`gmio_in_st` → `data_bc_km.in[0]`
   - RC：`gmio_in_rc` → `data_bc_km.in[1]`
   - 像素（每簇）：`gmio_in_xyz_px` → `px_demux_km.in[0]`
2. **广播扇出（Data Broadcast → 全部 224 个重建核）**
   - `data_bc_km.out[0]`（slowtime）→ 每个 `img_rec_km.in[0]`，加 `single_buffer`
   - `data_bc_km.out[1]`（RC）→ 每个 `img_rec_km.in[1]`，加 `single_buffer`
3. **像素分发与图像汇合（每簇内部）**
   - 像素：`px_demux_km.out[0]` → `pktsplit.in[0]`；`pktsplit.out[i]` → `img_rec_km[i].in[2]`
   - 图像：`img_rec_km[i].out[0]` → `pktmerge.in[i]`；`pktmerge.out[0]` → `plio_pkt_rtr_out.in[0]`
4. **RTP（每核一条参数线）**
   - `rtp_dump_img_in[k]` → `img_rec_km.in[3]`，控制最后一脉冲才 dump 图像

下面的伪代码勾勒出这条全景（省略循环边界）：

```
# 顶层
gmio_in_st  ──connect──▶ data_bc_km.in[0]
gmio_in_rc  ──connect──▶ data_bc_km.in[1]

for j in 0..AIE_SWITCHES:            # 7 簇
  for i in 0..IMG_SOLVERS_PER_SWITCH: # 每簇 32 核
    data_bc_km.out[0] ──connect──▶ bpCluster[j].img_rec_km[i].in[0]  # +single_buffer
    data_bc_km.out[1] ──connect──▶ bpCluster[j].img_rec_km[i].in[1]  # +single_buffer
    rtp_dump_img_in[32*j+i] ──connect<parameter>──▶ bpCluster[j].img_rec_km[i].in[3]

# 每簇子图内部
gmio_in_xyz_px ──connect──▶ px_demux_km.in[0]
px_demux_km.out[0] ──connect──▶ sp.in[0]            # 进 pktsplit<32>
for i in 0..32:
  sp.out[i] ──connect──▶ img_rec_km[i].in[2]       # 按包头分发(异份)
  img_rec_km[i].out[0] ──connect──▶ mg.in[i]        # 汇入 pktmerge<32>
mg.out[0] ──connect──▶ plio_pkt_rtr_out.in[0]       # 流向 PL 包路由器
```

#### 4.3.3 源码精读

**广播扇出**（本讲的核心代码段）。[design/aie/graph.h:177-184](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L177-L184) —— 双重循环把 2 个源扇出到 7×32=224 个目的，并对每个目的关掉双缓冲：

```cpp
for (int j=0; j<AIE_SWITCHES; j++) {
    for (int i=0; i<IMG_SOLVERS_PER_SWITCH; i++) {
        connect(data_bc_km.out[0], bpCluster[j].img_rec_km[i].in[0]);
        connect(data_bc_km.out[1], bpCluster[j].img_rec_km[i].in[1]);
        single_buffer(bpCluster[j].img_rec_km[i].in[0]);
        single_buffer(bpCluster[j].img_rec_km[i].in[1]);
    }
}
```

展开后就是 448 条 `connect`、448 条 `single_buffer`——全部从顶层那一个 `data_bc_km` 扇出。把广播放在顶层、只设一对 GMIO 的好处在这里兑现：主机只需把 slowtime/RC 各送一份进图，框架自动复制到 224 个核，**避免了主机重复送 224 份数据**。

注意这条广播**穿透了子图边界**：`data_bc_km` 在顶层图，`img_rec_km` 在子图 bpCluster 里。ADF 允许顶层图直接连线到子图成员（这里是 `bpCluster[j].img_rec_km[i]`），从而把顶层广播下沉到每个子图内核。

**RTP 参数连线**。[design/aie/graph.h:189-193](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L189-L193) —— 用 `connect<parameter>` 给每个重建核接一个运行时参数：

```cpp
for (int j=0; j<AIE_SWITCHES; j++) {
    for (int i=0; i<IMG_SOLVERS_PER_SWITCH; i++) {
        connect<parameter>(rtp_dump_img_in[IMG_SOLVERS_PER_SWITCH*j + i], bpCluster[j].img_rec_km[i].in[3]);
    }
}
```

`rtp_dump_img_in` 是声明在顶层的 [input_port 数组，共 `IMG_SOLVERS`=224 个](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L153)。主机在最后一脉冲用 `graph.update(rtp_dump_img_in[k], 1)` 把它置 1，触发对应内核把累加好的 `m_img` 打包 dump 出去（见 u3-l5 与 u5-l5）。

**像素分发与图像汇合**（子图内部）。[design/aie/graph.h:79](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L79) 与 [design/aie/graph.h:82](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L82) —— GMIO→demux、pktmerge→PLIO：

```cpp
connect(gmio_in_xyz_px.out[0], px_demux_km.in[0]);   // 像素进入 demux
connect(mg.out[0], plio_pkt_rtr_out.in[0]);           // 汇合后的图像流交给 PLIO
```

[design/aie/graph.h:88-97](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L88-L97) —— demux→pktsplit→各重建核，再各重建核→pktmerge：

```cpp
for (int i=0; i<IMG_SOLVERS_PER_SWITCH; i++)
    connect(sp.out[i], img_rec_km[i].in[2]);     // 包交换分发(异份)
connect(px_demux_km.out[0], sp.in[0]);
for (int i=0; i<IMG_SOLVERS_PER_SWITCH; i++) {
    connect(img_rec_km[i].out[0], mg.in[i]);     // 包交换汇合
}
```

对比 4.3 节的广播段与这里的像素段，差异一目了然：

| 维度 | slowtime / RC（广播） | 目标像素（切分） |
| --- | --- | --- |
| 每个内核拿到 | **相同**的一份 | **不同**的子集 |
| 端口类型 | buffer（窗口） | pktstream（包流） |
| 连线方式 | `connect` 扇出 + `single_buffer` | demux 打包头 + `pktsplit`/`pktmerge` |
| 源端口数 | 顶层各 1 个 | 每簇 1 个 GMIO |

#### 4.3.4 代码实践

**实践目标**：把四类端口的数据来源/去向、以及「广播 vs 包交换」的取舍写成一张可核对的总表。

**操作步骤**：

1. 在 [graph.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h) 里逐条追踪下面四类端口，记录它们各自的「源（谁喂它）」和「目的（它喂谁）」：
   - `gmio_in_st`（[L165](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L165), [L171](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L171)）
   - `gmio_in_rc`（[L166](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L166), [L172](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L172)）
   - `gmio_in_xyz_px`（[L66](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L66), [L79](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L79)）
   - `plio_pkt_rtr_out`（[L73](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L73), [L82](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L82)）
2. 在 [graph.cpp:188-196](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L188-L196) 核对主机侧 `gm2aie_nb` 的投递指针，确认像素是按 `sw_id` 切片、而 slowtime/RC 不切片。
3. 用一句话回答：为什么 slowtime/RC 的 `connect` 要加 `single_buffer`，而像素流用包交换？

**需要观察的现象**：

- slowtime/RC 的源是**同一个** `data_bc_km.out[k]`，被 224 条 `connect` 复用；像素的源 `sp.out[i]` 对每个重建核都**不同**（不同的 `i`）。

**预期结果**：得到如下总表（与本讲 4.1.1 表互补，聚焦「源/目的」）：

| 端口 | 数据来源（上游） | 数据去向（下游） | 连线方式 |
| --- | --- | --- | --- |
| `gmio_in_st` | DDR（主机 `gm2aie_nb`，整块） | `data_bc_km.in[0]`，再广播到 224 个 `img_rec_km.in[0]` | buffer + `single_buffer` |
| `gmio_in_rc` | DDR（主机 `gm2aie_nb`，逐脉冲） | `data_bc_km.in[1]`，再广播到 224 个 `img_rec_km.in[1]` | buffer + `single_buffer` |
| `gmio_in_xyz_px` | DDR（主机按 `sw_id` 切片投递） | `px_demux_km.in[0]` → `pktsplit` → 各 `img_rec_km[i].in[2]` | pktstream（包交换） |
| `plio_pkt_rtr_out` | `pktmerge.out[0]`（224 路汇合后） | PL 包路由器（`stream_connect` 到 `dma_pkt_router.pl_stream_in`） | 128 位 AXI4-Stream |

**结论**：slowtime/RC 是「一份共享给所有核」的只读广播，用 `single_buffer` 关掉双缓冲最省存储；像素是「每核一份不同子集」，只能靠包交换按 `pkt_id` 分发，`single_buffer` 在这里既无意义也不被允许。

> 说明：本实践为源码阅读型，结论可直接从源码得出，无需运行硬件。

#### 4.3.5 小练习与答案

**练习 1**：默认配置下，`data_bc_km.out[0]` 这一个源端口一共连到多少个目的？整张图里 `connect` 的总数（只算广播段）是多少？

**参考答案**：`out[0]`（slowtime）连到 7×32=**224 个** `img_rec_km.in[0]`。广播段（`out[0]` 与 `out[1]` 各 224 条）共 **448 条** `connect`。

**练习 2**：如果把 `data_bc_km` 从顶层图挪到每个子图里（每簇一个广播内核），主机投递 slowtime/RC 的次数会变成什么样？这会带来什么代价？

**参考答案**：那样每簇都要各自收到一份 slowtime/RC，主机对 `gmio_in_st`/`gmio_in_rc` 的投递要从「每脉冲各 1 次」变成「每脉冲各 7 次」（或设 7 对 GMIO），DDR 带宽与主机开销翻 7 倍。这正是把广播内核放顶层、只设一对 GMIO 的设计价值：**用一次投递 + 框架内广播，换掉 7 次重复投递**。

**练习 3**：RTP 连线 `connect<parameter>(rtp_dump_img_in[k], img_rec_km.in[3])` 为什么不能用 `single_buffer`？

**参考答案**：`in[3]` 是 `parameter`（RTP 标量参数），既不是 buffer 也不是 stream/pktstream，`single_buffer` 只作用于 buffer 窗口端口，对参数无效。

---

## 5. 综合实践

**任务**：画出本设计「端口与连线」的完整接线示意图，并标注每一段的机制。

**步骤**：

1. 在一张图上画出：顶层图 `BackProjectionGraph`（含 `data_bc_km`、`gmio_in_st`、`gmio_in_rc`、`rtp_dump_img_in[224]`）和 7 个子图 `bpCluster[0..6]`（每个含 `gmio_in_xyz_px`、`px_demux_km`、`pktsplit<32>`、32 个 `img_rec_km`、`pktmerge<32>`、`plio_pkt_rtr_out`）。
2. 用**三种线型**分别画出：
   - **实线 + `single_buffer` 标注**：两条广播线（slowtime、RC），从顶层 `data_bc_km` 扇出到全部 224 个重建核的 `in[0]`/`in[1]`。
   - **虚线**：像素包交换路径 `gmio_in_xyz_px → px_demux_km → pktsplit → img_rec_km[i].in[2]`，以及图像汇合 `img_rec_km[i].out[0] → pktmerge → plio_pkt_rtr_out`。
   - **点线**：RTP 参数线 `rtp_dump_img_in[k] → img_rec_km.in[3]`。
3. 在图旁列出 4 类端口（`gmio_in_st`/`gmio_in_rc`/`gmio_in_xyz_px`/`plio_pkt_rtr_out`）的数量、位宽/参数、以及仿真数据文件绑定。
4. 写一段话解释：为什么顶层只有 2 个 GMIO（slowtime/RC）就能喂饱 224 个核，而像素却需要 7 个 GMIO？

**预期结果**：一张能让人一眼看清「广播（同份，buffer+single_buffer）」与「切分（异份，pktstream）」两种机制如何分工的接线图，以及「广播放顶层省投递、切分放子图按簇进」的设计动因。

> 说明：本实践为设计/文档型，不依赖运行环境；若想用 Vitis 的图形化分析工具核验，可在 `aiesim` 编译产物里查看自动生成的图连接报告。

---

## 6. 本讲小结

- 本设计的对外端口共四类：顶层 2 个 `input_gmio`（`gmio_in_st`、`gmio_in_rc`）、子图 7 个 `input_gmio`（`gmio_in_xyz_px`）、子图 7 个 `output_plio`（`plio_pkt_rtr_out`），默认配置下合计 9 入 7 出。
- GMIO 走 NoC 接 DDR、由主机 `gm2aie_nb` 动态投递；PLIO 是 128 位 AXI4-Stream 直连 PL，仿真时绑定 `aie_to_plio_switch_*.csv` 文件，系统级被 Makefile 生成的 `stream_connect` 接到 PL 包路由器。
- 端口名用 `bp_graph_insts`/`bp_subgraph_insts` 两个计数器编出全局唯一字符串，保证多实例下不重名。
- `connect(源, 目的)` 建立点到点连线，同一个源可扇出到多个目的形成广播；`single_buffer` 只作用于 buffer 窗口端口，关掉默认 ping-pong 双缓冲以省局部存储。
- 核心广播段在 [graph.h:177-184](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L177-L184)：`data_bc_km` 的 2 个输出扇出到 224 个重建核（共 448 条 `connect`），让主机一次投递即可喂饱全部内核。
- 判据坐实：**同份共享数据（slowtime/RC）→ buffer + `connect` + `single_buffer`；异份切分数据（像素）→ `pktstream` + `pktsplit`/`pktmerge`**。`single_buffer` 绝不出现在 stream/pktstream/parameter 端口上。

## 7. 下一步学习建议

- **向内读内核**：本讲只看到端口的「针脚类型」，接下来该看 Data Broadcast 如何把 stream 转成 buffer 触发广播（u5-l1）、Pixel Demux 如何打包头（u5-l2）、以及 Image Reconstruction 如何消费这些端口（u5-l3 ~ u5-l5）。
- **向外读 PL**：`plio_pkt_rtr_out` 这条流出了 AIE 之后去哪？去看 PL 包路由器 HLS 内核如何接收 128 位 AXI-Stream 并按包头重排（u6-l1），以及 testbench 如何用本讲提到的 `aie_to_plio_switch_*.csv` 做仿真验证（u6-l2）。
- **向上读系统**：本讲提到的端口命名如何被 Makefile 翻译成 `system.cfg` 里的 `nk`/`stream_connect`/`sp` 行，将在 u7-l1（系统集成与打包）中完整展开。
