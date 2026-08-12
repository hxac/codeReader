# ADF 图与数据流执行模型

## 1. 本讲目标

上一讲（u2-l2）我们从 **AIE tile 的视角** 看清了「数据怎么搬」：buffer / stream / 包流、GMIO / PLIO 端口、RTP 运行时参数。这些都是单个内核「家门口的接口」。

本讲再往上走一层：**整张图（graph）是怎么搭起来的、又怎么跑起来的**。学完本讲，你应该能够：

1. 理解 ADF（Adaptable Dataflow）API 中 `graph` / `kernel` / `port` / `connect` 四个核心抽象，并能读懂本项目的 `graph.h`。
2. 掌握 `source()` 源文件绑定、`runtime<ratio>` 运行时比例，以及 `single_buffer` / ping-pong 双缓冲如何影响编译器的调度与布局。
3. 明白 AIE 图遵循 **Kahn 进程网络** 的「数据驱动执行」模型，理解为什么这种模型天然带来多内核并发流水。

本讲只讲「图的构造与执行语义」，不展开任何具体内核内部算法（数据广播、像素解复用、图像重建留到第 5 单元），也不讲主机如何用 XRT 驱动图（留到 u3-l5）。

---

## 2. 前置知识

在进入图抽象前，请确认你已经理解以下概念（它们在前面讲义已建立）：

- **Versal 三引擎分工**（u2-l1）：ARM 管控制编排、AIE 管算、PL 管拼；AIE 是 VLIW + SIMD 的向量处理器阵列，每个 tile 有 32 KB 局部存储。
- **AIE 数据搬运机制**（u2-l2）：buffer 端口（整块、同步阻塞）与 stream 端口（逐 beat 连续流）；GMIO 是 DDR↔AIE 经 NoC 的 DMA 通道，PLIO 是 AIE↔PL 的 AXI4-Stream 直连；RTP 用 `connect<parameter>` 连线、`graph.update` 改值。
- **common.h 关键宏**（u1-l4）：`AIE_SWITCHES=7`、`IMG_SOLVERS_PER_SWITCH=32`、`IMG_SOLVERS=224`、`PULSES=602`、`RC_SAMPLES=512`、`BC_ELEMENTS=4`。

如果上面任何一项你觉得陌生，建议先回看对应讲义。本讲会把这些「零件」用「图的语法」串成一张完整的反投影数据流图。

> 一句话衔接：u2-l2 讲的是「每个端口是什么」，本讲讲的是「这些端口在 C++ 里怎么声明、怎么连、怎么跑」。

---

## 3. 本讲源码地图

本讲主要围绕两个文件，辅以两个支撑文件：

| 文件 | 作用 | 本讲用它做什么 |
| --- | --- | --- |
| [design/aie/graph.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h) | 用 ADF API 描述反投影数据流图（顶层图 + 子图） | 本讲的主样本：所有 `kernel`/`port`/`connect`/`source`/`runtime` 示例都来自这里 |
| [doc/sections/versal_overview.tex](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/versal_overview.tex) | 项目自带文档，讲 AIE 架构、kernel、graph、Kahn 模型 | 提供概念定义与权威出处（AMD UG1079） |
| [design/aie/custom_kernels.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h) | 三个内核函数的 C++ 签名声明 | 看 `source()` 绑定的内核到底长什么样 |
| [design/aie/graph.cpp](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp) | 图的实例化与仿真 `main`（`__AIESIM__`/`__X86SIM__`） | 用它说明 Kahn 图在运行时如何被 `init/run/update/wait/end` 驱动 |

---

## 4. 核心概念与源码讲解

本讲的三个最小模块分别是：

1. **graph / kernel / port / connect 抽象** —— ADF 的「四件套」语法。
2. **runtime ratio 与 source 绑定** —— 告诉编译器「内核代码在哪、占多大算力」。
3. **Kahn 数据驱动执行** —— 图怎么在没有中央时钟的情况下自动并发跑起来。

### 4.1 graph / kernel / port / connect 抽象

#### 4.1.1 概念说明

ADF（Adaptable Dataflow）API 是 AMD 提供的一套 **纯 C++ 的数据流图描述语言**。你不需要画框图、也不需要写硬件描述，只要用 C++ 类把下面四个抽象写出来，AIE 编译器就能把它映射到真实 tile 阵列上：

- **graph（图）**：继承自 `adf::graph` 的 C++ 类，是「一张数据流图」的容器。图里装内核、端口和连线。
- **kernel（内核）**：一个 C/C++ 函数（本设计中是 `data_broadcast_kern`、`px_demux_kern`、`ImgReconstruct::img_reconstruct_kern`）。内核是图里真正「干活」的节点，最终被编译成跑在某个 tile 上的 ELF。
- **port（端口）**：内核或图与外界交换数据的「插口」。端口分三类：
  - 内核自身的输入/输出端口（buffer 或 stream）；
  - **GMIO** 端口（DDR↔AIE）；
  - **PLIO** 端口（PL↔AIE）；
  - 此外还有 `input_port`/`output_port`，用来接 RTP 运行时参数。
- **connect（连线）**：把一个端口的输出接到另一个端口的输入，定义数据流向。

一句话总结：**graph 是画布，kernel 是节点，port 是节点的插口，connect 是节点之间的线**。这四样东西写完，整张反投影图就「画」好了。

#### 4.1.2 核心流程

本项目的图分两层（顶层图 + 子图），构造流程如下：

```text
BackProjectionGraph（顶层图，继承 graph）
├── 1 个 data_bc_km        （Data Broadcast 内核：广播 slowtime + RC）
├── AIE_SWITCHES 个 bpCluster   （= 子图 BackProjectionSubgraph 的实例数组）
│   每个 bpCluster 内部：
│   ├── 1 个 px_demux_km        （Pixel Demux 内核：按标签分发像素）
│   ├── IMG_SOLVERS_PER_SWITCH 个 img_rec_km   （图像重建内核）
│   ├── pktsplit / pktmerge     （包交换：一进多出 / 多进一出）
│   ├── input_gmio gmio_in_xyz_px   （像素输入端口）
│   └── output_plio plio_pkt_rtr_out（图像输出端口，接到 PL）
├── input_gmio gmio_in_st / gmio_in_rc   （slowtime / RC 输入端口）
└── input_port rtp_dump_img_in[IMG_SOLVERS] （RTP：是否输出图像）
```

关键数字（来自 [design/common.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h) 默认值）：`AIE_SWITCHES=7`，`IMG_SOLVERS_PER_SWITCH=32`，所以整张图里有 **1 个广播内核 + 7 个解复用内核 + 224 个重建内核**。

端口的「插口」用数组下标访问，例如 `kernel.in[0]`、`gmio.out[0]`、`sp.out[i]`。connect 的方向永远是 `connect(源端口的输出, 目的端口的输入)`。

#### 4.1.3 源码精读

**(a) 图是一个继承 `adf::graph` 的 C++ 类。** 子图和顶层图都这么声明：

```cpp
class BackProjectionSubgraph: public graph {
```
> 声明子图为一张 ADF 图。出处：[design/aie/graph.h:15](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L15)。顶层图同理声明为 `class BackProjectionGraph: public graph`（[graph.h:132](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L132)）。

**(b) 内核与端口都是类的成员变量。** 在子图里，内核、包交换、GMIO、PLIO 都是成员：

```cpp
kernel px_demux_km;
kernel img_rec_km[IMG_SOLVERS_PER_SWITCH];
pktsplit<IMG_SOLVERS_PER_SWITCH> sp;
pktmerge<IMG_SOLVERS_PER_SWITCH> mg;
input_gmio gmio_in_xyz_px;
output_plio plio_pkt_rtr_out;
```
> 这些就是「图里的节点与插口」。注意 `img_rec_km` 是 **内核数组**（共 32 个），`pktsplit/pktmerge` 是包交换对象。出处：[design/aie/graph.h:21-41](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L21-L41)。

**(c) 内核用 `kernel::create` 创建，并把名字绑定到一个 C++ 函数。**

```cpp
px_demux_km = kernel::create(px_demux_kern);
```
> `kernel::create(函数名)` 告诉编译器：这个内核跑的是 `px_demux_kern` 这个函数。出处：[design/aie/graph.h:49](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L49)。

重建内核稍有不同，因为它是一个**类成员函数**，需要带状态（每个内核要记住自己的 id 和累加图像），所以用 `create_object`：

```cpp
for (int i=0; i<IMG_SOLVERS_PER_SWITCH; i++)
    img_rec_km[i] = kernel::create_object<ImgReconstruct>(IMG_SOLVERS_PER_SWITCH*bp_subgraph_insts + i);
```
> `create_object<ImgReconstruct>(id)` 实例化一个 `ImgReconstruct` 对象，并传入唯一 `id`。`ImgReconstruct` 类里有一句 `REGISTER_FUNCTION(ImgReconstruct::img_reconstruct_kern)` 把成员函数注册为内核入口（见 [custom_kernels.h:32-35](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L32-L35)）。出处：[design/aie/graph.h:52-53](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L52-L53)。

**(d) 端口用 `::create` 创建，并给一个名字（编译器和 host 都靠名字找它）。**

```cpp
gmio_in_xyz_px = input_gmio::create(xyz_px_str.c_str(), 256, 1000);
...
plio_pkt_rtr_out = output_plio::create(plio_pkt_rtr_str.c_str(), plio_128_bits, plio_data_file_str.c_str());
```
> GMIO 第二个参数 `256` 是 NoC 突发字节数，第三个 `1000` 是带宽（MB/s）提示；PLIO 第二个参数 `plio_128_bits` 指定 128 位位宽，第三个参数是把这个端口的流数据落盘成哪个 CSV（仿真用）。出处：[design/aie/graph.h:66](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L66) 与 [graph.h:73](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L73)。

**(e) connect 把端口接起来，定义数据流向。** 子图内部有几类典型连线：

```cpp
connect(gmio_in_xyz_px.out[0], px_demux_km.in[0]);   // GMIO → 解复用内核
connect(px_demux_km.out[0], sp.in[0]);               // 解复用 → 包分离器
for (int i=0; i<IMG_SOLVERS_PER_SWITCH; i++)
    connect(sp.out[i], img_rec_km[i].in[2]);         // 包分离器 → 各重建内核(像素输入)
for (int i=0; i<IMG_SOLVERS_PER_SWITCH; i++)
    connect(img_rec_km[i].out[0], mg.in[i]);         // 各重建内核 → 包合并器
connect(mg.out[0], plio_pkt_rtr_out.in[0]);          // 包合并器 → PLIO(出图到 PL)
```
> 注意 `img_rec_km[i].in[2]`：重建内核有 4 个输入端口（`in[0]`=slowtime、`in[1]`=RC、`in[2]`=像素、`in[3]`=RTP），像素走第 3 个。出处：[design/aie/graph.h:79-97](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L79-L97)。

**(f) 顶层图把广播内核的输出「扇出」到全部 224 个重建内核。**

```cpp
for (int j=0; j<AIE_SWITCHES; j++) {
    for (int i=0; i<IMG_SOLVERS_PER_SWITCH; i++) {
        connect(data_bc_km.out[0], bpCluster[j].img_rec_km[i].in[0]);  // slowtime 广播
        connect(data_bc_km.out[1], bpCluster[j].img_rec_km[i].in[1]);  // RC 广播
        single_buffer(bpCluster[j].img_rec_km[i].in[0]);
        single_buffer(bpCluster[j].img_rec_km[i].in[1]);
    }
}
```
> 一个广播内核的输出扇出到 224 个重建内核：slowtime 走 `out[0]`、RC 走 `out[1]`。`single_buffer(...)` 显式关闭双缓冲、只用单缓冲——因为这是「广播」：同一份数据要被所有内核读，单缓冲省局部存储。出处：[design/aie/graph.h:177-184](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L177-L184)。

#### 4.1.4 代码实践

> **实践目标**：把 graph / kernel / port / connect 四件套在本项目里对号入座。

操作步骤：

1. 打开 [design/aie/graph.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h)。
2. 在 `BackProjectionGraph` 类（L132 起）里，找出所有 **`kernel` 成员对象**，记下它们的名字与绑定的 C++ 函数。
3. 找出所有 **端口成员**（`input_gmio` / `output_plio` / `input_port`），并各自说明它搬运什么数据（提示：变量名 `gmio_in_st` / `gmio_in_rc` / `rtp_dump_img_in` 已经暗示了用途）。
4. 追踪一条从 GMIO 到 PLIO 的完整数据通路：`gmio_in_xyz_px → px_demux_km → sp → img_rec_km[i] → mg → plio_pkt_rtr_out`，用箭头画出来。

需要观察的现象 / 预期结果：

- 顶层图 `BackProjectionGraph` 里只有 **1 个** `kernel` 成员（`data_bc_km`），其余内核都在 7 个子图实例里。
- 子图 `BackProjectionSubgraph` 里有 **2 类内核**（`px_demux_km` × 1、`img_rec_km` × 32），加上包交换 `sp`/`mg`。
- 数据依赖草图（粗粒度）：

  ```text
  gmio_in_st ─┐
              ├─→ data_bc_km ──(广播 slowtime/RC)──→ img_rec_km[i].in[0/1]
  gmio_in_rc ─┘                                          ↑
                                                          │
  gmio_in_xyz_px → px_demux_km → sp → img_rec_km[i].in[2]─┘
                                     img_rec_km[i].out[0] → mg → plio_pkt_rtr_out → PL
  ```

> 本实践为「源码阅读型」，无需运行；若要核对，可对照 4.1.3 的引用行号。

#### 4.1.5 小练习与答案

**练习 1**：`connect` 的两个参数顺序写反了会怎样？比如把 `connect(gmio_in_xyz_px.out[0], px_demux_km.in[0])` 写成 `connect(px_demux_km.in[0], gmio_in_xyz_px.out[0])`。

> **答案**：`connect` 的签名是「源（输出端口）→ 目的（输入端口）」。写反会让编译器把内核的输入端口当成输出、GMIO 的输出当成输入，类型不匹配，编译期直接报错。ADF 的方向是强约束，不像软件函数调用可以随意。

**练习 2**：重建内核 `img_rec_km` 有 4 个输入端口（`in[0..3]`），分别对应什么数据？提示：结合 [custom_kernels.h:26-30](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L26-L30) 的函数签名。

> **答案**：`in[0]`=slowtime（buffer，`BC_ELEMENTS` 个 float）、`in[1]`=RC 距离压缩样本（buffer，`RC_SAMPLES` 个 cfloat）、`in[2]`=目标像素（pktstream，包流）、`in[3]`=RTP `rtp_dump_img_in`（运行时参数，决定是否输出图像）。

---

### 4.2 runtime ratio 与 source 绑定

#### 4.2.1 概念说明

把内核「挂」到图里只是声明了它的存在，编译器还需要两个关键信息才能把它映射到真实硬件：

- **`source(kernel) = "file.cc"`**：这个内核的 **函数实现在哪个源文件**。一个 `graph.h` 里可能引用很多内核函数，但它们的实现分散在 `.cc` 文件里，`source()` 就是「指名道姓」地告诉编译器去哪找。
- **`runtime<ratio>(kernel) = 1.0`**：这个内核运行时大约要占掉一个 tile **多少比例的算力**。取值 \( 0 < r \leq 1 \)。

`runtime<ratio>` 的作用是 **指导编译器把内核摆到 tile 上、决定是否共享 tile**。文档（[versal_overview.tex:73-80](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/versal_overview.tex#L73-L80)）原文说：

> Kernels are compiled ... into ELF images, which the graph runtime loads onto the assigned tiles. Based on the user-specified **runtime ratio** and available resources, the compiler may **co-locate two kernels to time-share a tile**. If not, it assigns one kernel to the tile.

也就是说，如果两个内核的 ratio 之和不超过 1，且局部存储装得下，编译器会把它们 **放在同一个 tile 上分时复用**；否则一个内核独占一个 tile。

与之配套的还有缓冲策略：默认情况下 ADF 框架对 buffer 连接 **自动做双缓冲（ping-pong）**——一块在算、另一块在搬，从而把访存和计算重叠起来（见 [versal_overview.tex:158-162](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/versal_overview.tex#L158-L162)）。`single_buffer` 则是显式关掉双缓冲，只用一块缓冲（广播场景下省存储）。

#### 4.2.2 核心流程

两个属性的设置都写在图的构造函数里，紧跟在 `connect` 之后：

```text
对每个内核 k：
  source(k)       = "backprojection.cc"   # 指向实现文件
  runtime<ratio>(k) = 1.0                  # 独占一个 tile
```

`runtime<ratio>` 的「共享判定」可以写成一条简单规则：

\[
\text{若 } \sum_{k \in tile} \text{ratio}(k) \leq 1 \text{ 且资源放得下 } \Longrightarrow \text{这些内核可共享一个 tile}
\]

本设计里 **所有内核的 ratio 都是 1.0**，所以每个内核都独占一个 tile，没有任何两个内核共享。这是合理的选择：反投影的每个内核都是计算密集型，几乎用满一个 tile 的算力，没有余量分享。

#### 4.2.3 源码精读

**(a) source() 把内核绑定到实现文件。** 子图里两个内核都绑到 `backprojection.cc`：

```cpp
source(px_demux_km) = "backprojection.cc";
for (int i=0; i<IMG_SOLVERS_PER_SWITCH; i++)
    source(img_rec_km[i]) = "backprojection.cc";
```
> 注意：`graph.h` 里只有函数 **声明**（来自 `custom_kernels.h`），真正的函数 **实现** 在 `backprojection.cc`。`source()` 就是把这两者连起来的那根线。出处：[design/aie/graph.h:115-117](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L115-L117)。顶层图的广播内核同理（[graph.h:197](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L197)）。

**(b) runtime<ratio> 全部设为 1.0。**

```cpp
runtime<ratio>(px_demux_km) = 1.0;
for (int i=0; i<IMG_SOLVERS_PER_SWITCH; i++)
    runtime<ratio>(img_rec_km[i]) = 1.0;
```
> 设成 1.0 意味着「这个内核需要一整个 tile 的算力」，编译器因此不会把它和别的内核塞进同一个 tile。出处：[design/aie/graph.h:122-124](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L122-L124)。

**(c) single_buffer 对广播连接关闭双缓冲。** 在 4.1.3 (f) 已贴过：

```cpp
single_buffer(bpCluster[j].img_rec_km[i].in[0]);
single_buffer(bpCluster[j].img_rec_km[i].in[1]);
```
> 默认 ADF 会给 buffer 连接做 ping-pong 双缓冲。但 slowtime/RC 是 **广播数据**（一份给 224 个内核读），双缓冲会白白多占局部存储，所以这里显式 `single_buffer`。出处：[design/aie/graph.h:181-182](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L181-L182)。

**(d) location 约束把每个子图钉在固定 tile 区域。** 这虽然不是「源/ratio」，但同样是「告诉编译器怎么摆」的关键属性：

```cpp
int col_start = base_col + bp_subgraph_insts * 7;
int col_end   = col_start + 6;
...
location<graph>(*this) = area_group({
    { aie_tile,  col_start, row_start, col_end, row_end },
    { shim_tile, col_start, 0, col_end, 0 }
});
```
> 每个子图被约束到一个 7 列 × 8 行的 tile 区域里，并且每实例 `col_start` 递增 7，所以 7 个子图横向铺开、互不重叠。`shim_tile` 那一行是给 PLIO 留的「接口列」。出处：[design/aie/graph.h:102-111](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L102-L111)。

#### 4.2.4 代码实践

> **实践目标**：弄清每个内核的「代码在哪、占多大算力」。

操作步骤：

1. 在 `graph.h` 里搜索所有 `source(`，列出哪些内核绑到哪个文件。
2. 搜索所有 `runtime<ratio>`，确认它们的值。
3. 打开 [design/aie/custom_kernels.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h)，对照三个内核的函数签名，理解 `source()` 绑定的函数到底接收什么参数。

需要观察的现象 / 预期结果：

| 内核 | source 文件 | runtime ratio | 含义 |
| --- | --- | --- | --- |
| `data_bc_km` | `backprojection.cc` | 1.0 | 独占一个 tile |
| `px_demux_km`（×7） | `backprojection.cc` | 1.0 | 独占一个 tile |
| `img_rec_km[i]`（×224） | `backprojection.cc` | 1.0 | 独占一个 tile |

**思考题（不必运行）**：如果把 `img_rec_km[i]` 的 ratio 改成 0.5，编译器是否会把两个重建内核塞进同一个 tile？什么情况下这是安全的、什么情况下会出问题？

> **预期分析**：理论上 ratio 之和 ≤ 1 允许共享，所以 0.5+0.5 可能被共置。但每个重建内核都要持有 `m_img[(PULSES*RC_SAMPLES)/IMG_SOLVERS]`（约 1 KB cfloat 累加缓冲，见 [custom_kernels.h:41](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L41)）以及 RC/像素等输入缓冲，两个内核的局部存储加起来必须仍放得进 32 KB；同时反投影计算量很大，0.5 可能不够，会被编译器拒绝或运行时降速。结论：本设计保持 1.0 是稳妥选择。**待本地验证**：实际能否共置需用 `aiesimulator --profile` 或编译报告确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `graph.h` 里只有内核函数的声明（`custom_kernels.h`），却还要用 `source()` 再指定一次实现文件？

> **答案**：ADF 编译器（`v++ --mode aie`）在编译图时，需要知道把哪个 `.cc` 源文件连同这个内核一起编译成 tile 上的 ELF。C++ 的声明只给出签名，`source()` 则是 ADF 特有的「把内核符号与实现文件绑定」的指令，编译器据此找到函数体并做特定于 AIE 的向量化、流水化编译。

**练习 2**：`single_buffer` 和默认的双缓冲（ping-pong）区别在哪？本设计为什么对广播连接用 `single_buffer`？

> **答案**：双缓冲用两块缓冲交替——一块算、一块搬，重叠访存与计算以提吞吐，代价是翻倍占用局部存储；单缓冲只一块，省存储但不能重叠。slowtime/RC 是「一份广播给全部 224 个重建内核读」的数据，没必要为它各备两块，所以用 `single_buffer` 省下宝贵的 32 KB 局部存储。

---

### 4.3 Kahn 数据驱动执行

#### 4.3.1 概念说明

前面两节讲的是「图怎么画」。这一节回答一个更本质的问题：**画好之后，图里的几百个内核，谁来指挥它们什么时候开始算？**

答案出乎意料地简单：**没人指挥，数据自己说了算**。

ADF 图遵循 **Kahn 进程网络** 模型。文档（[versal_overview.tex:164-169](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/versal_overview.tex#L164-L169)）原文说：

> Graph execution follows a **Kahn process-network model** where each kernel **fires only when its inputs are ready**, making kernel execution **data-driven**. This enables concurrency across the graph where one kernel may compute while another loads the next data block and a third writes results.

这里有两个关键词：

- **fire（触发）**：内核的一次执行。触发条件是「输入就绪」。
- **data-driven（数据驱动）**：没有全局时钟节拍说「现在该你算了」，而是「你的输入到齐了，你就自动开算」。

这个模型天然支持并发：因为每个内核只等自己的输入，**互不依赖的内核就可以同时跑**。于是你会看到这样的画面：广播内核在算第 N 个脉冲、重建内核 A 在算第 N-1 个脉冲的像素、重建内核 B 在把第 N-2 个脉冲的结果写出去——它们在同一时刻各自忙碌，整张图像一个流水车间。

#### 4.3.2 核心流程

Kahn 模型的触发规则可以形式化。设一个内核有输入端口集合 \( \{in_0, in_1, \dots, in_{m-1}\} \)，用 \( R(in_i) \) 表示「端口 \( in_i \) 的数据是否就绪」，则：

\[
\text{fire} \iff \bigwedge_{i=0}^{m-1} R(in_i)
\]

对 **同步 buffer** 端口，「就绪」= 整块缓冲已被生产者写满；只要有一个同步输入没满，内核就**阻塞等待**（见 [versal_overview.tex:120-129](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/versal_overview.tex#L120-L129)）。对 **stream** 端口，则是「有 beat 可读」。

把这个规则套到重建内核 `img_reconstruct_kern` 上（它有 4 个输入）：

```text
img_reconstruct_kern 触发条件：
  in[0] slowtime 缓冲满？  ──┐
  in[1] RC 缓冲满？        ──┼── 全部为真 → fire（开始算这一脉冲的累加）
  in[2] 像素包流有数据？   ──┤
  in[3] RTP 参数已设？      ──┘
```

于是整个反投影的「流水」是这样自驱运行的：

```text
主机/GMIO 把数据送进图
        │
        ▼
 ① 广播内核：slowtime+RC 就绪 → fire → 把数据广播给 224 个重建内核
        │
        ▼
 ② 每个重建内核：等齐 slowtime+RC+像素+RTP → fire → 算差分距离、相位校正、累加
        │ （第 N 脉冲的广播 vs 第 N-1 脉冲的重建 —— 并发！）
        ▼
 ③ 最后一脉冲 RTP=1 → 重建内核把累加图像 dump 到包流 → 包合并 → PLIO → PL
```

关键点：**步骤 ① ② ③ 在不同脉冲上是交叠的**。当广播内核在处理第 N 个脉冲的输入时，重建内核还在处理第 N-1 个脉冲，PL 可能正在收第 N-2 个脉冲的结果。这就是 Kahn 模型带来的「免费并发」。

#### 4.3.3 源码精读

图的运行时生命周期由宿主（host 或仿真 `main`）用几个 API 驱动。仿真 `main`（`graph.cpp`，`__AIESIM__`/`__X86SIM__` 保护）是看得最清楚的版本：

**(a) 实例化与初始化。** 图是一个全局数组对象，构造即声明，`init()` 完成运行时初始化：

```cpp
const int INSTANCES = 1;
BackProjectionGraph bpGraph[INSTANCES];
...
for(int inst=0; inst<INSTANCES; inst++) {
    bpGraph[inst].init();
}
```
> 注意 `INSTANCES=1`：本项目默认只实例化 1 张完整的反投影图（即只占满 7 个子图区域）。出处：实例化在 [design/aie/graph.cpp:9-10](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L9-L10)，`init()` 调用在 [graph.cpp:61-63](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L61-L63)。

**(b) `run(PULSES)` 启动图，并声明要跑多少轮。**

```cpp
for (int inst = 0; inst < INSTANCES; inst++) {
    bpGraph[inst].run(PULSES);
}
```
> `run(PULSES)` 告诉运行时：「这张图的数据驱动循环最多触发 PULSES 轮」。注意它**不阻塞**——启动后立刻返回，剩下的「哪个内核何时 fire」交给 Kahn 调度。出处：[design/aie/graph.cpp:174-176](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L174-L176)。

**(c) 用 GMIO 非阻塞投递数据（`gm2aie_nb`），让内核的输入「就绪」。**

```cpp
bpGraph[inst].gmio_in_st.gm2aie_nb(broadcast_data_array, PULSES*BC_ELEMENTS*sizeof(float));
for(int pulse_idx=0; pulse_idx<PULSES; pulse_idx++) {
    bpGraph[inst].gmio_in_rc.gm2aie_nb(rc_array + pulse_idx*RC_SAMPLES, RC_SAMPLES*sizeof(cfloat));
    for (int sw_id=0; sw_id<AIE_SWITCHES; sw_id++) {
        bpGraph[inst].bpCluster[sw_id].gmio_in_xyz_px.gm2aie_nb(...);
    }
    ...
}
```
> `_nb` = non-blocking。宿主逐脉冲把 slowtime/RC/像素 **推进图**，正是这些数据「就绪」后，对应内核才按 Kahn 规则 fire。slowtime 一次性整块投递（它要被全部脉冲共享），RC 与像素则逐脉冲投递。出处：[design/aie/graph.cpp:188-196](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L188-L196)。

**(d) 用 `update` 改 RTP，控制「最后一脉冲才 dump」。**

```cpp
for(int kern_id=0; kern_id<IMG_SOLVERS; kern_id++) {
    if (pulse_idx == PULSES-1)
        bpGraph[inst].update(bpGraph[inst].rtp_dump_img_in[kern_id], 1);
    else
        bpGraph[inst].update(bpGraph[inst].rtp_dump_img_in[kern_id], 0);
}
```
> RTP 是 Kahn 模型的「第四个输入」。前 PULSES−1 轮设 0（只累加、不输出），最后一轮设 1（触发图像写出）。这正是把 `in[3]` 设成「就绪/特定值」来控制内核行为。出处：[design/aie/graph.cpp:198-204](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L198-L204)。

**(e) `wait()` 阻塞到全部完成，`end()` 释放。**

```cpp
bpGraph[inst].wait();
bpGraph[inst].end();
```
> 因为前面全是非阻塞调用，宿主必须用 `wait()` 等整张图把 PULSES 轮跑完（特别是 PLIO 输出要写完文件），否则输出会是空的（注释也强调了这一点）。出处：[design/aie/graph.cpp:210-211](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L210-L211)。

#### 4.3.4 代码实践

> **实践目标**：把 Kahn「数据驱动」从抽象概念落到可观察的 API 调用顺序上。

操作步骤：

1. 打开 [design/aie/graph.cpp](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp) 的 `main`（L47 起）。
2. 列出图的生命周期 API 时序：`init() → run(PULSES) → [gm2aie_nb / update 循环] → wait() → end()`。
3. 对每个 API，回答两个问题：① 它是阻塞还是非阻塞？② 它在 Kahn 模型里扮演什么角色（生产数据 / 设阈值 / 等待完成）？
4. 思考：为什么 `run()` 之后宿主不直接 `wait()`，而要先循环投递数据？

需要观察的现象 / 预期结果：

- `run(PULSES)` 是 **非阻塞** 的「发令枪」，它只是把图置于运行态；真正让内核 fire 的是后续 `gm2aie_nb` 投进来的数据。
- 如果删掉 `gm2aie_nb` 循环直接 `wait()`，图会因为「输入永远不就绪」而卡死（或空跑 PULSES 轮后产出空图像）。
- 这恰好印证 Kahn 模型：**没有数据就没有 fire，没有 fire 就没有输出**。

> 本实践为「源码阅读 + 推理型」，无需上板；若要实测，可在 sw_emu 下删掉某条 `gm2aie_nb` 观察输出 CSV 是否变空（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：在 Kahn 模型里，为什么「同步 buffer 输入没满」会阻塞内核，而这反而有利于并发？

> **答案**：同步 buffer 要求整块就绪才 fire，看似「等」会拖慢；但正是这种「等齐才动」的确定性，让运行时可以安全地让 **不同内核在不同数据上同时忙碌**——A 在等它的第 N 块时，B 可以正在算它的第 N-1 块。阻塞的是单个内核，并发的是整张图。如果没有这种「等输入」的纪律，运行时就无法放心地把内核重叠调度。

**练习 2**：本设计里 `run(PULSES)` 的参数 `PULSES` 起什么作用？如果把它改小（比如改成 10），会发生什么？

> **答案**：`run(N)` 限定图的数据驱动循环最多跑 N 轮。改成 10 意味着图只会消费约 10 脉冲的数据、重建内核也只累加约 10 次，最后 RTP dump 出来的是一张「只聚焦了 10 个脉冲」的部分图像，分辨率/能量都远低于完整 602 脉冲的结果。它是一个上限/计划值，配合宿主实际投递的数据量生效。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「图阅读报告」任务：

**任务**：阅读 [design/aie/graph.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h) 与 [design/aie/graph.cpp](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp)，产出一份包含以下四部分的报告（文字或图均可）：

1. **节点清单**：列出 `BackProjectionGraph` 实例化后，整张图里共有多少个内核、几类、各绑定的 `source()` 与 `runtime<ratio>`。（提示：1 + 7 + 224 = 232 个内核，全部 ratio=1.0、全部 source=`backprojection.cc`。）
2. **端口与连线**：画出从三类 GMIO 输入（slowtime/rc/像素）到 PLIO 输出的粗粒度数据依赖图，标注哪里用了 `single_buffer`、哪里用了包交换（`pktsplit/pktmerge`）、哪里是 RTP。
3. **运行时序**：写出 `init → run → gm2aie_nb → update → wait → end` 的时序，并说明为什么 `run` 是非阻塞的、`wait` 是必须的。
4. **Kahn 解释**：用重建内核的 4 个输入端口说明「数据驱动 fire」——为什么前 601 个脉冲内核只累加不输出、最后一个脉冲才输出。

**进阶（可选）**：尝试回答——如果要把 `INSTANCES` 从 1 改成 2（实例化两张完整的反投影图），`bp_graph_insts` 这个计数器（[graph.h:12-13](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L12-L13) 与 [graph.cpp:7](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L7)）会起什么作用？它如何保证两张图的 GMIO/PLIO 端口名字不冲突？（提示：看构造函数里用 `bp_graph_insts`/`bp_subgraph_insts` 给端口拼名字的字符串。）

> 本任务为「源码阅读型综合实践」，全部信息都在本讲引用的源码里，无需运行硬件即可完成；若要核对端口命名与计数，可对照 `graph.h` 构造函数中 `std::to_string(bp_graph_insts)` 的拼接处。

---

## 6. 本讲小结

- **ADF 四件套**：`graph` 是继承 `adf::graph` 的 C++ 类（画布）；`kernel` 是节点（用 `kernel::create` / `create_object` 创建并绑定函数）；`port` 是插口（GMIO/PLIO/`input_port`）；`connect(源, 目的)` 连线。本项目用它们搭出了「1 广播 + 7 解复用 + 224 重建」的两层图。
- **source() 绑定实现**：`graph.h` 只有函数声明，`source(kernel) = "backprojection.cc"` 才把内核符号与实现文件绑定，供 AIE 编译器编译成 tile 上的 ELF。
- **runtime<ratio> 决定布局**：ratio 是「占一个 tile 多大算力」的比例，比值之和 ≤ 1 的内核可被共置到同一 tile 分时复用；本设计全部为 1.0，每个内核独占一个 tile。`single_buffer` 则对广播连接关闭双缓冲以省局部存储。
- **Kahn 数据驱动**：图遵循 Kahn 进程网络，内核「输入就绪才 fire」，没有全局节拍；同步 buffer 要求全部输入就绪才触发，正是这种纪律让互不依赖的内核天然并发流水。
- **运行时 API**：宿主用 `init → run(N) → gm2aie_nb/update → wait → end` 驱动图；`run` 非阻塞发令，数据靠 `gm2aie_nb` 非阻塞投递，`update` 改 RTP 控制输出时机，最后 `wait` 阻塞等全部完成。
- **本讲只讲结构与调度**：具体每个内核内部（广播怎么搬、解复用怎么打包头、重建怎么算差分距离与相位）留到第 5 单元；主机如何用 XRT 驱动同一张图留到 u3-l5。

---

## 7. 下一步学习建议

至此，你已经掌握了 Versal 平台（u2-l1）、AIE 数据搬运机制（u2-l2）和 ADF 图的构造与执行模型（本讲）。前置知识模块到此结束，接下来正式进入「读具体源码」的阶段。建议按以下顺序继续：

1. **第 3 单元（u3）主机应用**：从 `design/host/main.cpp` 看主机如何用 XRT 打开设备、加载 xclbin、分配缓冲，并用本讲提到的 `run/gm2aie_nb/update/wait` API 编排这张图（重点是 u3-l1 流程与 u3-l5 编排）。
2. **第 4 单元（u4）AIE 图拓扑细节**：如果你想先把「图怎么布线」吃透，可以进 u4-l1～u4-l3，深入看包交换、tile 布局约束（`location<area_group>`）和 GMIO/PLIO 端口的命名规则——它们就是本讲 `graph.h` 里那些循环与计数器的展开。
3. **第 5 单元（u5）内核实现**：等你想知道「重建内核到底算了什么」，再去读 `backprojection.cc` 里三个内核的函数体。

一句话：本讲让你看懂「图的骨架」，u3 让你看懂「谁在驱动这张图」，u4/u5 让你看懂「骨架里每块肉长什么样」。
