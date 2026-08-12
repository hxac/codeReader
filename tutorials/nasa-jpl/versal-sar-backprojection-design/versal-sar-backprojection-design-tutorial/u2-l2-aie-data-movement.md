# AI Engine 数据搬运：GMIO、PLIO、缓冲、流与 RTP

## 1. 本讲目标

上一讲（u2-l1）我们从宏观上认识了 Versal ACAP 的三引擎分工与 NoC 片上网络。本讲把镜头推进到 **AI Engine（AIE）tile 这一粒度**，专门回答一个问题：

> AIE 内核要算的数据从哪里来、算完的数据到哪里去？框架提供了哪些「搬运管道」，它们各自适合搬什么？

学完本讲你应该能够：

1. 区分 **buffer 端口**与 **stream 端口**，说清它们的同步语义与适用场景。
2. 认识 **GMIO（DDR↔AIE）** 与 **PLIO（PL↔AIE）** 两类外部端口，说出本设计里各自搬运的是什么数据。
3. 理解 **RTP（运行时参数）** 如何在不重启图的情况下、在运行中给内核传一个小值（本设计中是「要不要把图像 dump 出来」）。
4. 拿着本项目的 `graph.h`，把上面的概念一一对应到真实端口与连线上。

本讲只讲「数据怎么搬」，不讲「反投影算法怎么算」——算法留到第 5 单元。先把管道看清，后面读内核才不会迷路。

## 2. 前置知识

读本讲前，你需要大致知道以下概念（不熟的话先看 u2-l1）：

- **AIE tile**：Versal 里一个个 VLIW 向量处理器核，每个核有自己的一小块 **32 KB 局部数据存储**。核与核之间、核与外部之间靠几种「管道」搬数据。
- **NoC**：片上网络，是所有进出 **DDR** 的数据必经的高速公路。
- **PL**：可编程逻辑（FPGA），挂在 AIE 阵列边上，既能和 AIE 用 AXI4-Stream 直连，也能经 NoC 访问 DDR。
- **ADF（Adaptable Dataflow）**：用 C++ 描述 AIE 数据流图的 API；我们用 `kernel`、`port`、`connect` 这些抽象来搭图。

再补两个本讲要用到的小术语：

- **beat**：流（stream）上一次传输的基本单位，对 AIE 来说通常是 32 位一个 beat，每个时钟一个 beat。
- **同步/异步**：这里指内核等待数据的策略——「等整块到齐再跑」还是「我自己用锁控制什么时候读」。

一句话直觉：**buffer 像快递柜（一次送/取一整箱），stream 像传送带（一拍一拍连续流），GMIO 是连到 DDR 的传送带，PLIO 是连到 PL 的传送带，RTP 是运行中塞进去的一张小纸条。** 下面我们把这个直觉逐条落实到源码。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲用它来看什么 |
|------|------|------------------|
| [doc/sections/versal_overview.tex](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/versal_overview.tex) | AIE 架构与数据搬运机制的官方说明文档（LaTeX） | buffer/stream、GMIO/PLIO、本地存储、时钟的权威描述 |
| [design/common.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h) | 三域共享的配置中心 | 决定各端口数据规模的宏（PULSES、RC_SAMPLES、BC_ELEMENTS 等） |
| [design/aie/custom_kernels.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h) | AIE 内核函数签名 | 哪些形参是 stream、哪些是 buffer、哪个是 RTP |
| [design/aie/graph.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h) | ADF 图拓扑（端口与连线） | GMIO/PLIO 端口、buffer/stream/pktstream 连线、RTP 连线 |
| [design/aie/graph.cpp](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp) | 仿真用的 main（`__AIESIM__`/`__X86SIM__`） | 怎么用 `GMIO::malloc` / `gm2aie_nb` / `update` 把数据喂进端口 |

记住一条主线：**端口（port）是图层面上的「插孔」，内核形参是「插头」，`connect` 是把它们插上的线。** 本质上就是给数据选一条合适的管道。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1** buffer 与 stream：内核的两种数据端口（含 buffer 的同步/异步、stream 的连续流、以及本设计用到的包流 pktstream 与广播）。
- **4.2** GMIO 与 PLIO：AIE 阵列与外部世界（DDR / PL）的两类端口。
- **4.3** RTP：运行时给内核传一个小参数。

### 4.1 buffer 与 stream：内核的两种数据端口

#### 4.1.1 概念说明

AIE 内核和外界交换数据，最基本有两种「端口风格」：

- **buffer（缓冲）端口**：对应 tile 局部存储里一块**连续的整块内存**。内核一次处理一整块。可以理解为「快递柜」：快递员把一整箱东西塞进柜子，内核等箱子满了再开柜取货。
- **stream（流）端口**：对应一条 **AXI4-Stream**，数据一拍（beat）一拍连续流过来，内核边到边处理。可以理解为「传送带」：只要带上有货就能拿，不必等满一箱。

两者关键差别在**同步语义**：

- **同步 buffer**（默认）：内核会**阻塞**，直到整块输入数据到齐（输入）或整块输出被消费完（输出）才开始/继续。如果声明了多个同步输入 buffer，内核要等**所有**输入都就绪才触发（fire）。这正是 ADF 图「数据驱动」的基础。
- **异步 buffer**：在 buffer 类型名里带 `async`，**不会自动阻塞**，改由内核代码自己用锁（lock/unlock）显式控制何时读写。灵活，但同步责任交给开发者。

stream 则天然是连续的：内核可以用一个迭代器每个时钟读/写一拍，不需要等整块。

> 还有一种特殊流——**包流（packet stream, `pktstream`）**：它在普通 AXI4-Stream 上多带了一层「包头」，用包头来区分数据该送给哪个内核。本设计用它把目标像素分发到众多图像重建内核，下一讲（u4-l2）会专门讲包交换；本讲只需把它当作「带标签的 stream」。

#### 4.1.2 核心流程

buffer 与 stream 的运作可以这样对比：

```text
同步 buffer 端口（以输入为例）
  外部填数据 ──> [整块局部存储] ──> 内核：等满 -> 一次性读 -> 处理 -> 释放
  特点：数据驱动触发；多 buffer 间天然同步；框架自动做 ping-pong 双缓冲

stream 端口
  外部送 beat ──> [流 FIFO] ──> 内核：每拍读一个 beat，连续处理
  特点：连续不断；适合点对点的高速流水；广播时把一条流扇出到多个接收者
```

框架对 buffer 连接会**自动实现双缓冲（ping-pong）**：内核在算 A 块时，下一块 B 已经在被填入。但如果一条输出要同时喂给很多内核（广播），双缓冲会浪费珍贵的局部存储，这时就显式声明 `single_buffer`（只用单缓冲）。

带宽上一条 32 位 AIE 流每个时钟传 32 位，AIE 时钟约 1.25 GHz，故单条流峰值带宽为：

\[
B_{\text{stream}} = \frac{32\,\text{bit}}{\text{cycle}} \times 1.25\times10^{9}\,\frac{\text{cycle}}{\text{s}} \times \frac{1\,\text{B}}{8\,\text{bit}} = 5\times10^{9}\,\text{B/s} = 5\,\text{GB/s}
\]

这就是 AIE 阵列内部「核到核」的高速管道能力。

#### 4.1.3 源码精读

**① 文档对 buffer/stream 的权威定义**

文档先讲 buffer 的同步/异步语义：同步端口会阻塞到整块就绪，多个同步输入 buffer 要全部就绪内核才触发；异步端口需自行加锁。

> 「A synchronous buffer port blocks until the entire block is available (for inputs) or consumed (for outputs) before the kernel proceeds… If multiple synchronous input buffers are declared, the kernel will not fire until all are ready.」——见 [doc/sections/versal_overview.tex:120-129](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/versal_overview.tex#L120-L129)

随后讲 stream：内核可以不停地按 beat 读写，PL 侧的 AXI 流可经 PLIO 喂进 AIE，内核每时钟推进一次流迭代器。

> 见 [doc/sections/versal_overview.tex:131-139](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/versal_overview.tex#L131-L139)

**② 在内核签名里识别 buffer 与 stream**

内核形参的类型直接告诉你它走哪种端口。看 `data_broadcast_kern` 的签名（数据广播内核）：

```cpp
void data_broadcast_kern(input_stream<float>* __restrict slowtime_in,                 // stream 入
                         input_buffer<cfloat, extents<RC_SAMPLES>>& __restrict rc_in, // buffer 入
                         output_stream<float>* __restrict slowtime_out,               // stream 出
                         output_buffer<cfloat, extents<RC_SAMPLES>>& __restrict rc_out);// buffer 出
```

> [design/aie/custom_kernels.h:17-20](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L17-L20)：`slowtime_in/out` 是流（`input_stream`/`output_stream`），`rc_in/out` 是缓冲（`input_buffer`/`output_buffer`，大小 `RC_SAMPLES=512` 个 cfloat）。

再看图像重建内核 `img_reconstruct_kern`，它把两种风格和包流、RTP 都集齐了：

```cpp
void img_reconstruct_kern(input_buffer<float, extents<BC_ELEMENTS>>& __restrict slowtime_in,  // buffer
                          input_buffer<cfloat, extents<RC_SAMPLES>>& __restrict rc_in,        // buffer
                          input_pktstream *px_xyz_in,           // 包流入（带标签的 stream）
                          output_pktstream *img_out,            // 包流出
                          int rtp_dump_img_in);                 // RTP（4.3 节讲）
```

> [design/aie/custom_kernels.h:26-30](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L26-L30)

**③ 广播与 single_buffer：一条流扇出到全部重建内核**

顶层图里，`data_bc_km` 的两个输出要同时喂给全部 `IMG_SOLVERS`（默认 7×32=224）个重建内核。这段连线是 buffer/stream 在本设计里最有代表性的一处：

```cpp
for (int j=0; j<AIE_SWITCHES; j++) {
    for (int i=0; i<IMG_SOLVERS_PER_SWITCH; i++) {
        connect(data_bc_km.out[0], bpCluster[j].img_rec_km[i].in[0]); // slowtime（stream 出 -> buffer 入）
        connect(data_bc_km.out[1], bpCluster[j].img_rec_km[i].in[1]); // rc（buffer 出 -> buffer 入）
        single_buffer(bpCluster[j].img_rec_km[i].in[0]);
        single_buffer(bpCluster[j].img_rec_km[i].in[1]);
    }
}
```

> [design/aie/graph.h:177-184](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L177-L184)

两个值得记的细节：

1. **`out[0]`（slowtime 流）接到 224 个 buffer 输入**：ADF 把「一条流扇出到多个 buffer 输入」当作**广播（broadcast）**——这正是这个内核名叫 `data_broadcast` 的原因，也是为什么 slowtime 特意从流端口转出（用流来触发广播机制）。
2. **`single_buffer(...)`**：广播场景下若每个接收端都开双缓冲，局部存储会被重复副本吃光。显式声明单缓冲，省一半存储。224 个副本各自只存一份。

**④ 算一算局部存储装得下吗**

`img_reconstruct_kern` 持有一个跨多次调用累加的图像缓冲 `m_img`（成员变量），大小为：

```cpp
alignas(aie::vector_decl_align) cfloat m_img[(PULSES*RC_SAMPLES)/IMG_SOLVERS];
```

> [design/aie/custom_kernels.h:41](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L41)

代入默认宏（[design/common.h:17-38](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L17-L38)）：\(PULSES=602, RC\_SAMPLES=512, IMG\_SOLVERS=7\times32=224\)。

\[
\text{元素数} = \frac{602\times512}{224} = \frac{308224}{224} = 1376\;\text{个 cfloat}
\]

每个 cfloat 8 字节，所以：

\[
\text{m\_img} = 1376\times 8\,\text{B} = 11008\,\text{B} \approx 10.75\,\text{KB}
\]

再加上 rc 缓冲 \(512\times8=4096\,\text{B}=4\,\text{KB}\) 和 slowtime 缓冲 \(4\times4=16\,\text{B}\)，三者合计约 15 KB，远小于单 tile 的 32 KB 局部数据存储（[doc/sections/versal_overview.tex:201-211](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/versal_overview.tex#L201-L211)）。这就是为什么这套设计能把每个重建核的数据都放进局部存储、不必频繁访问 DDR。

#### 4.1.4 代码实践

**实践目标**：学会用「内核形参类型」一眼判断端口风格。

**操作步骤**：

1. 打开 [design/aie/custom_kernels.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h)。
2. 对 `px_demux_kern`、`data_broadcast_kern`、`ImgReconstruct::img_reconstruct_kern` 三个内核，逐个形参标注它是 `stream` / `buffer` / `pktstream` / `RTP`。
3. 回到 [design/aie/graph.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h)，找到每个形参对应的 `connect(...)`，确认 `in[N]`/`out[N]` 的下标与形参顺序一致。

**需要观察的现象**：

- `px_demux_kern` 只有 stream/pktstream 形参，没有 buffer——它是个纯「流到流」的中转内核。
- `data_broadcast_kern` 的 `in[0]`/`out[0]`（slowtime）是 stream，`in[1]`/`out[1]`（rc）是 buffer。

**预期结果**：你会得到一张「形参 → 端口风格」对照表，与 4.1.3 的结论一致。

> 本实践为源码阅读型，无需运行；若想验证，可跳到 4.2.4 用仿真实际跑一遍。

#### 4.1.5 小练习与答案

**练习 1**：本设计中所有 buffer 端口都是同步的（类型名里没有 `async`）。如果某个内核有两个同步输入 buffer A 和 B，但 A 的数据总是比 B 晚到，会发生什么？

**参考答案**：内核会一直等到 A 和 B **都就绪**才触发。慢的 A 会拖住快的 B——这正是「数据驱动、多 buffer 同步」的代价。设计时要么让两边速率匹配，要么改用异步 buffer 自行用锁协调。

**练习 2**：为什么广播连线（graph.h:181-182）要显式写 `single_buffer`？删掉会怎样？

**参考答案**：广播把同一份数据复制给 224 个接收内核。若每个接收端都默认开双缓冲（ping-pong），局部存储会被两份副本占用，可能超出 32 KB 上限。`single_buffer` 强制只用一份缓冲，省存储。删掉后框架按默认双缓冲分配，存储翻倍，极端情况下编译报资源不足。

**练习 3**：一条 32 位 AIE 流峰值约 5 GB/s。一个 cfloat 占 8 字节，那么一条流每秒最多能搬多少个 cfloat？

**参考答案**：\(5\times10^9\,\text{B/s} \div 8\,\text{B/cfloat} = 6.25\times10^8\) 个 cfloat/秒，约 6.25 亿个/秒。

---

### 4.2 GMIO 与 PLIO：AIE 阵列与外部的端口

#### 4.2.1 概念说明

buffer 和 stream 解决的是「核内/核间」怎么搬数据。但数据最初在 **DDR** 里，最终也要写回 **DDR** 或交给 **PL**——这就需要两类「对外端口」：

- **GMIO（Graph Memory-mapped I/O）**：AIE 阵列与 **DDR** 之间的 DMA 通道，**经 NoC**。主机（ARM）在 DDR 里准备好一块 buffer，用 GMIO 把它搬进/搬出 AIE。本设计的全部输入数据（slowtime、rc、目标像素）都走 GMIO 进入 AIE。
- **PLIO**：AIE 阵列与 **PL** 之间的 **AXI4-Stream** 直连，**不走 NoC**（因为它不涉及 DDR）。本设计把 AIE 算完的图像从 PLIO 端口流出，交给 PL 上的包路由器内核做后处理。

一句话区分：**涉及 DDR 的走 GMIO（过 NoC）；AIE 直连 PL 的走 PLIO（不过 NoC）。**

GMIO 与 PLIO 的关键规格（来自文档 [doc/sections/versal_overview.tex:176-189](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/versal_overview.tex#L176-L189)）：

| 端口 | 连接对象 | 是否经 NoC | 位宽/突发 | 数量上限 |
|------|----------|-----------|-----------|----------|
| GMIO | DDR ↔ AIE | 是 | 突发 64/128/256 B | 系统级各 32 个输入、32 个输出 |
| PLIO | PL ↔ AIE | 否 | 32/64/128 位 | 取决于 PLIO-capable 列 |

注意 PLIO「32 bits per AI Engine clock」——即 PLIO 在 AIE 侧按 32 位/时钟节拍化；选 `plio_128_bits` 是指 **PL 侧总线宽 128 位**，能更好地匹配 PL 时钟域、喂饱 AIE 侧的 32 位流。

#### 4.2.2 核心流程

本设计的整体数据通路（先看输入侧与输出侧各走哪条管道）：

```text
输入侧（全部走 GMIO，过 NoC，从 DDR 进 AIE）
  DDR[slowtime] --gmio_in_st-->  data_broadcast_kern        （4 个 float：天线 X/Y/Z + ref_range）
  DDR[rc]       --gmio_in_rc-->  data_broadcast_kern        （512 个 cfloat 距离压缩样本）
  DDR[xyz_px]   --gmio_in_xyz_px--> px_demux_kern（每个 switch 一个） （目标像素 X/Y/Z）

输出侧（走 PLIO，不过 NoC，从 AIE 进 PL）
  img_rec_km --pktmerge--> plio_pkt_rtr_out --(AXI4-Stream)--> PL 包路由器 --> DDR
```

GMIO 的一次搬运是「分配 DDR buffer → 用 DMA 描述符触发突发」。本设计在创建 GMIO 时写死了突发大小 256 B、以及 1000 个缓冲槽位（用于排队多个 DMA 描述符）。运行时用 `gm2aie_nb`（GMIO to AIE, non-blocking）发起一次 DDR→AIE 的非阻塞搬运。

#### 4.2.3 源码精读

**① 文档对 GMIO/PLIO 的定义**

文档在「数据搬运机制」清单里列了两类端口，并给出带宽特征：

> GMIO：经 NoC 在外部 DDR 与 AIE 之间搬数据，支持突发传输（64/128/256 B）；PLIO：AIE 与 PL 之间的 AXI4-Stream，常见 32/64/128 位宽。——见 [doc/sections/versal_overview.tex:55-64](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/versal_overview.tex#L55-L64)

规格速查表见 [doc/sections/versal_overview.tex:176-189](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/versal_overview.tex#L176-L189)（32 个 GMIO 输入/输出、PLIO 32 位/时钟）。

**② 在图里创建 GMIO 与 PLIO 端口**

顶层图声明两个输入 GMIO（slowtime、rc）：

```cpp
// Data broadcaster GMIO ports
input_gmio gmio_in_st;
input_gmio gmio_in_rc;
```

> [design/aie/graph.h:149-150](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L149-L150)

构造函数里用 `input_gmio::create(名字, 256, 1000)` 创建：`256` 是突发字节数，`1000` 是缓冲槽位数：

```cpp
gmio_in_st = input_gmio::create("gmio_in_st_" + std::to_string(bp_graph_insts), 256, 1000);
gmio_in_rc = input_gmio::create("gmio_in_rc_" + std::to_string(bp_graph_insts), 256, 1000);
```

> [design/aie/graph.h:165-166](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L165-L166)。名字带 `bp_graph_insts` 计数后缀，保证多实例时每个端口有唯一名。

子图里声明目标像素的 GMIO（每个 switch 一个）和输出 PLIO：

```cpp
// Pixel demux GMIO port
input_gmio gmio_in_xyz_px;
// Packet router PLIO port
output_plio plio_pkt_rtr_out;
```

> [design/aie/graph.h:35](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L35) 与 [design/aie/graph.h:41](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L41)

PLIO 创建时指定**位宽 `plio_128_bits`** 和**绑定的数据文件名**（仿真时该端口的数据会落盘成这个 CSV，供 PL testbench 读取）：

```cpp
std::string plio_data_file_str = "aie_to_plio_switch_" + ... + ".csv";
plio_pkt_rtr_out = output_plio::create(plio_pkt_rtr_str.c_str(), plio_128_bits, plio_data_file_str.c_str());
```

> [design/aie/graph.h:71-73](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L71-L73)。注意 PLIO 这里把 AIE 的输出流（包流）交给 PL——这就是 u2-l1 里说的「PL 管拼、AIE 管算」的物理连接。

GMIO/PLIO 到内核的连线：

```cpp
connect(gmio_in_st.out[0], data_bc_km.in[0]);   // slowtime: GMIO -> data_broadcast
connect(gmio_in_rc.out[0], data_bc_km.in[1]);   // rc:       GMIO -> data_broadcast
connect(gmio_in_xyz_px.out[0], px_demux_km.in[0]); // 像素: GMIO -> px_demux（子图内）
connect(mg.out[0], plio_pkt_rtr_out.in[0]);     // 合并后的包流: pktmerge -> PLIO
```

> 分别见 [design/aie/graph.h:171-172](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L171-L172)、[design/aie/graph.h:79](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L79)、[design/aie/graph.h:82](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L82)。

**③ 仿真里如何用 GMIO 喂数据**

`graph.cpp` 的仿真 main（`__AIESIM__`/`__X86SIM__` 宏保护，见 [design/aie/graph.cpp:12](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L12)）演示了 GMIO 的三步用法：先 `GMIO::malloc` 在 DDR 分 buffer，再 `gm2aie_nb` 发起搬运。

```cpp
float* broadcast_data_array = (float*) GMIO::malloc(PULSES*BC_ELEMENTS*sizeof(float));  // slowtime
cfloat* rc_array            = (cfloat*) GMIO::malloc(PULSES*RC_SAMPLES*sizeof(cfloat)); // rc
float*  xyz_px_array        = (float*)  GMIO::malloc(PULSES*RC_SAMPLES*sizeof(float)*3); // 像素
```

> [design/aie/graph.cpp:66](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L66)、[design/aie/graph.cpp:96](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L96)、[design/aie/graph.cpp:155](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L155)

发起搬运的核心三行（注意 RC 和像素是**逐脉冲**投递，slowtime 是一次性整块）：

```cpp
bpGraph[inst].gmio_in_st.gm2aie_nb(broadcast_data_array, PULSES*BC_ELEMENTS*sizeof(float));           // slowtime：整块
for(int pulse_idx=0; pulse_idx<PULSES; pulse_idx++) {
    bpGraph[inst].gmio_in_rc.gm2aie_nb(rc_array + pulse_idx*RC_SAMPLES, RC_SAMPLES*sizeof(cfloat));   // rc：逐脉冲
    for (int sw_id=0; sw_id<AIE_SWITCHES; sw_id++)
        bpGraph[inst].bpCluster[sw_id].gmio_in_xyz_px.gm2aie_nb(xyz_px_array + sw_id*px_per_demux_kern*3, px_per_demux_kern*sizeof(float)*3); // 像素：逐 switch 逐脉冲
}
```

> [design/aie/graph.cpp:188-196](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L188-L196)

这段非常关键：它揭示了**为什么用 GMIO、以及喂法的差异**。slowtime 体小（\(602\times16\,\text{B}\approx9.6\,\text{KB}\)）一次发完；rc 体大（\(602\times4\,\text{KB}\approx2.3\,\text{MB}\)）且每个内核每脉冲都要新的一份，所以**逐脉冲**发；像素也逐脉冲按 switch 分片发。`gm2aie_nb` 末尾的 `nb` 表示 non-blocking——调用立即返回，DMA 在后台搬，主机可以继续投递下一脉冲，从而让「搬运」与「计算」重叠。

#### 4.2.4 代码实践

**实践目标**：跑通 AIE 仿真，亲眼看 GMIO/PLIO 端口工作并产出文件。

**操作步骤**：

1. 先 source 环境（参考 u1-l3）：`source helper_scripts/env_setup.sh`，确认 `PLATFORM` 已导出。
2. 在仓库根目录运行 AIE 仿真目标（具体目标名以 Makefile 为准，通常形如 `make aiesim`，TARGET 用默认 hw 下的 aiesim 分支）：
   ```bash
   make aiesim
   ```
   > 待本地验证：确切目标名以本机 Makefile 当前版本为准；若不确定，先 `make -n aiesim` 看 dry-run 命令。
3. 仿真会在 `build/hw/aiesim/`（graph.cpp 注释所写的工作目录，见 [design/aie/graph.cpp:68](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L68)）下产出每个输出 PLIO 绑定的 CSV 文件，文件名形如 `aie_to_plio_switch_<g>_<s>.csv`（名字来自 [design/aie/graph.h:71](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L71)）。

**需要观察的现象**：

- 终端会打印 `Rand Seed: ...` 与逐像素的 `xyz_px_array[k] = {x, y, z}`（见 [design/aie/graph.cpp:55](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L55) 与 [design/aie/graph.cpp:169](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L169)），说明 GMIO 端口已被驱动。
- 仿真结束后 `aie_to_plio_switch_*.csv` 非空——说明 PLIO 端口确有数据流出。

**预期结果**：7 个 switch 各对应一个非空 CSV（main 默认 `AIE_SWITCHES=7`）。若 CSV 为空，通常是因为 `bpGraph.wait()` 没等到 PLIO 写完（graph.cpp 末尾 [design/aie/graph.cpp:210-211](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L210-L211) 的 `wait()/end()` 缺一不可）。

> 若本地无 Vitis 环境，本实践可降级为「源码阅读型」：追踪 graph.cpp:188-196 三条 `gm2aie_nb` 的数据指针与字节数，说明它们分别对应哪个 GMIO 端口、搬的是什么。

#### 4.2.5 小练习与答案

**练习 1**：本设计为什么把**输入**全走 GMIO、而**输出**走 PLIO？

**参考答案**：输入数据（slowtime/rc/像素）最初都在 DDR 里，必须经 NoC 才能进 AIE，所以用 GMIO。输出要交给 PL 上的包路由器做后处理（把乱序的包重排成连续图像），AIE↔PL 直连走 PLIO 即可，不必绕 DDR。这正对应 u2-l1 的分工：AIE 管算、PL 管拼。

**练习 2**：`input_gmio::create(name, 256, 1000)` 里两个数字分别是什么？为什么 RC 数据要逐脉冲 `gm2aie_nb` 而不是一次性发？

**参考答案**：`256` 是单次 DMA 突发字节数，`1000` 是可排队的缓冲描述符数量。RC 数据每个脉冲对应一组新的距离压缩样本，且每个内核每脉冲都要消费一份新 rc，所以必须**逐脉冲**投递；一次性发的话内核无法区分「这一份属于哪个脉冲」。`nb`（non-blocking）让投递立即返回，搬运与计算重叠。

**练习 3**：GMIO 与 PLIO 谁过 NoC、谁不过？为什么？

**参考答案**：GMIO 过 NoC（因为它连 DDR，所有进出 DDR 的流量都走 NoC）；PLIO 不过 NoC（AIE 与 PL 之间是 AXI4-Stream 直连，不涉及 DDR）。

---

### 4.3 RTP：运行时给内核传参

#### 4.3.1 概念说明

**RTP（Runtime Parameter，运行时参数）** 用来在**图已经跑起来之后**，给内核传一个（或一小串）值。它不是数据流，更像运行中递进去的一张「小纸条」。

为什么需要它？有些控制信息无法在编译期定死，要在运行中根据时机改变。本设计最典型的例子：图像重建内核在 602 个脉冲里要**不断累加**聚焦图像，只在**最后一脉冲**才把累加好的图像 dump 出去。这个「现在是最后一脉冲吗」的开关，就是用 RTP 传进去的：

- 大部分脉冲：RTP = 0 → 继续累加，不输出。
- 最后一脉冲：RTP = 1 → 把 `m_img` 打包输出。

RTP 的特点（来自文档 [doc/sections/versal_overview.tex:154-156](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/versal_overview.tex#L154-L156)）：主要用于传**单个值或小数组**，不适合大数据。

#### 4.3.2 核心流程

RTP 的完整链路是三段：声明 → 连线 → 运行时更新。

```text
1. 图里声明：input_port rtp_dump_img_in[N];          // 一组「参数端口」
2. 连线：     connect<parameter>(rtp_dump_img_in[i], kernel.in[3]); // 用 parameter 连到内核的 int 形参
3. 运行时：   graph.update(rtp_dump_img_in[i], 1或0); // 主机/仿真在运行中改值
```

注意第 2 步用的是 `connect<parameter>`（而不是普通的 `connect`）——`parameter` 表明这条线传的是「参数」而非「数据流」。内核一侧的形参就是个普通的 `int`（见 `img_reconstruct_kern` 的最后一个形参 `int rtp_dump_img_in`，[design/aie/custom_kernels.h:30](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L30)）。

#### 4.3.3 源码精读

**① 声明一组 RTP 端口**

顶层图里为每个图像重建内核准备一个独立的 RTP 端口（共 `IMG_SOLVERS=224` 个）：

```cpp
//***** RTP PORT OBJECTS *****//
input_port rtp_dump_img_in[IMG_SOLVERS];
```

> [design/aie/graph.h:152-153](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L152-L153)

**② 用 `connect<parameter>` 把参数端口接到每个内核**

```cpp
for (int j=0; j<AIE_SWITCHES; j++) {
    for (int i=0; i<IMG_SOLVERS_PER_SWITCH; i++) {
        connect<parameter>(rtp_dump_img_in[IMG_SOLVERS_PER_SWITCH*j + i], bpCluster[j].img_rec_km[i].in[3]);
    }
}
```

> [design/aie/graph.h:188-193](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L188-L193)。下标 `IMG_SOLVERS_PER_SWITCH*j + i` 把二维 (switch, kern) 展平成一维 RTP 索引，与 graph.cpp 里的更新顺序严格对应。

**③ 运行时按脉冲时机更新 RTP**

仿真 main 的双层循环里，只有最后一脉冲把 RTP 置 1：

```cpp
for(int kern_id=0; kern_id<IMG_SOLVERS; kern_id++) {
    if (pulse_idx == PULSES-1)
        bpGraph[inst].update(bpGraph[inst].rtp_dump_img_in[kern_id], 1); // 最后一脉冲：dump
    else
        bpGraph[inst].update(bpGraph[inst].rtp_dump_img_in[kern_id], 0); // 否则：继续聚焦累加
}
```

> [design/aie/graph.cpp:198-204](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L198-L204)

这段把本讲的三个机制串起来了：外层逐脉冲循环驱动 **GMIO**（4.2）送新数据，内核用 **buffer/stream**（4.1）收数据并累加，最后用 **RTP**（本节）通知「该输出了」。真实主机程序（u3-l5 会讲）的 `bp()` 也是同样的节奏，只是把 `gm2aie_nb` 换成 XRT 的 GMIO async、把 `update` 换成 `xrt::graph::update`。

#### 4.3.4 代码实践

**实践目标**：通过修改 RTP 行为，直观看到「dump 时机」对输出的影响。

**操作步骤**：

1. 阅读 [design/aie/graph.cpp:198-204](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L198-L204)，确认 `pulse_idx == PULSES-1` 是 dump 条件。
2. **阅读型实验（不改源码）**：回答——如果把条件改成 `pulse_idx == 0`，每个内核会在什么时候输出？输出的图像会是什么样子？
3. **进阶（可选，改源码须自行还原）**：把条件临时改成 `pulse_idx == PULSES/2`（约第 301 脉冲），重新 `make aiesim`，对比 `aie_to_plio_switch_*.csv` 的大小或内容规模。

**需要观察的现象**：

- 条件为 `PULSES-1`：内核累加满 602 脉冲后才输出，图像聚焦最完整。
- 条件为 `0`：第一脉冲就 dump，图像几乎没有相干累加，基本是噪声/单脉冲回波。
- 条件为 `PULSES/2`：只累加了一半孔径，图像方位分辨率下降（孔径变短）。

**预期结果**：dump 越晚，累加的脉冲越多，图像聚焦越好；这正好对应 u1-l1 讲的「相干累加使目标同相增强」。**待本地验证**：具体输出数值需在有 Vitis 环境的机器上运行确认。

> 注意：本实践涉及修改源码，仅建议在本地实验分支上做，且实验后还原；本讲义默认不改源码。

#### 4.3.5 小练习与答案

**练习 1**：为什么 RTP 用 `connect<parameter>` 而不是普通 `connect`？内核一侧的形参是什么类型？

**参考答案**：`<parameter>` 告诉 ADF 这条线传的是「参数」而不是数据流，运行时可由主机/仿真通过 `update` 修改。内核一侧是个普通 `int` 形参（`int rtp_dump_img_in`），框架会在每次内核触发前把最新值塞进去。

**练习 2**：本设计有 224 个图像重建内核，于是声明了 224 个 RTP 端口。能不能只声明 1 个 RTP 端口、连给全部 224 个内核？

**参考答案**：理论上可以广播同一个参数，但本设计要求**每个内核独立判断** dump 时机（虽然目前时机相同，但 RTP 下标与内核一一对应，便于将来按内核差异化控制）。更重要的是，RTP 与 `create_object` 传入的实例 id（u4-l2 讲）配合，让每个内核知道自己是谁。一端口广播会丢失这种逐核寻址能力。

**练习 3**：RTP 适合传「602×512 的整张图像」吗？为什么？

**参考答案**：不适合。文档明确 RTP 主要传「单个值或小数组」（[versal_overview.tex:154-156](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/versal_overview.tex#L154-L156)）。大数据应走 GMIO/buffer/stream。本设计也正是用 RTP 传 0/1 这一个开关值，图像本身走 PLIO 流出。

---

## 5. 综合实践：在 graph.h 里给五种管道列表归位

这是本讲规格要求的综合任务，把 4.1～4.3 串起来。

**任务**：打开 [design/aie/graph.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h)，找出 **GMIO、PLIO、buffer、stream、RTP** 五种机制各自对应的端口或连线，填出下表（「搬运的数据」一栏要写清是 slowtime / rc / 像素 / 图像）。

| 机制 | 对应的端口/连线（给出 graph.h 行号） | 搬运的数据 | 方向 |
|------|--------------------------------------|------------|------|
| GMIO | | | |
| PLIO | | | |
| buffer（同步） | | | |
| stream | | | |
| RTP | | | |

**参考答案**（填好后应大致如此）：

| 机制 | 端口/连线（行号） | 搬运的数据 | 方向 |
|------|-------------------|------------|------|
| GMIO | `gmio_in_st`/`gmio_in_rc`（149-150, 165-166）；子图 `gmio_in_xyz_px`（35, 65-66） | slowtime / rc / 目标像素 | DDR → AIE |
| PLIO | `plio_pkt_rtr_out`（41, 71-73）；`connect(mg.out[0], plio_pkt_rtr_out.in[0])`（82） | 合并后的图像（包流） | AIE → PL |
| buffer（同步） | `rc_in`/`rc_out`（custom_kernels.h:18,20）、`slowtime_in`（重建核, custom_kernels.h:26）；连线 graph.h:179-180；`single_buffer`（181-182） | rc（512 cfloat）/ slowtime（4 float） | 核内/核间整块 |
| stream | `slowtime_in/out`（广播核, custom_kernels.h:17,19）；`px_xyz_in`（demux, custom_kernels.h:14）；广播连线 graph.h:179 | slowtime（流式广播）/ 像素（流） | 核间连续流 |
| RTP | `rtp_dump_img_in[IMG_SOLVERS]`（152-153）；`connect<parameter>`（188-193）；运行时 `update`（graph.cpp:201,203） | 「是否 dump 图像」的 0/1 开关 | 主机 → 内核参数 |

**进阶追问**（不要求写答案，用来检验理解）：

- 为什么 slowtime 既出现在 stream 列、又出现在 buffer 列？（提示：广播核以流接收、以流扇出；重建核以 buffer 接收。）
- 把「走 NoC 的」与「不走 NoC 的」分开——哪些端口过 NoC，哪些不过？（GMIO 过，PLIO 不过。）
- 整个设计里，**唯一的输出通路**是哪条？（PLIO → PL 包路由器。）

完成这张表，你就把 AIE 阵列「数据怎么进、怎么算、怎么出、怎么控制」的管道图在脑中建起来了。

## 6. 本讲小结

- AIE 内核有 **buffer**（整块、阻塞到就绪、框架自动双缓冲）和 **stream**（连续 beat、点对点高速流）两种端口；本设计还在 stream 之上用了带包头的 **pktstream** 做按标签分发。
- **同步 buffer** 等全部输入就绪才触发，是「数据驱动」的基础；广播场景用 `single_buffer` 省局部存储。本设计每个重建核的 `m_img`≈10.75 KB、rc 缓冲 4 KB，都轻松住进 32 KB 局部存储。
- **GMIO** 是 DDR↔AIE 的 DMA 通道（**过 NoC**），用 `GMIO::malloc` + `gm2aie_nb` 搬数据；本设计三类输入（slowtime/rc/像素）全走 GMIO，rc 与像素逐脉冲投递。
- **PLIO** 是 AIE↔PL 的 AXI4-Stream 直连（**不过 NoC**），本设计用 128 位 PLIO 把图像流交 PL 包路由器做后处理。
- **RTP** 是运行时塞给内核的小参数，用 `connect<parameter>` 连线、`graph.update` 改值；本设计用它传「最后一脉冲才 dump 图像」的 0/1 开关。
- 三者协同：GMIO 持续喂数据 → buffer/stream 收数据并累加 → RTP 通知输出时机 → PLIO 流出结果。

## 7. 下一步学习建议

本讲讲清了「管道」，但还没讲「图怎么搭、内核怎么排」。建议：

1. **下一讲 u2-l3（ADF 图与数据流执行模型）**：把本讲的 port/connect 放到完整的 `BackProjectionGraph` 拓扑里，理解 kernel/runtime ratio/source 绑定与 Kahn 数据驱动执行如何让多核并发。
2. **u4-2（包交换、内核实例化与 tile 布局）**：本讲只点到 pktstream，u4-l2 会深入 `pktsplit`/`pktmerge` 与 `create_object` 传实例 id、`location` area_group 约束。
3. **u3-5（用 XRT 编排 AIE 图与 PL 内核）**：本讲的 `gm2aie_nb`/`update` 是仿真版；u3-l5 讲真实主机程序怎么用 XRT 的 GMIO async 与 `xrt::graph::update` 实现同样的节奏。

读完这三篇，你就能从「一条管道」放大到「整张图怎么跑、主机怎么驱动」。
