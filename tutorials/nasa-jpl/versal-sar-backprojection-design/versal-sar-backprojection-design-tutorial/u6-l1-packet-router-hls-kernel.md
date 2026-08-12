# PL 包路由器 HLS 内核

## 1. 本讲目标

本讲聚焦可编程逻辑（PL）上的唯一一个内核——`dma_pkt_router`（DMA 包路由器）。它是用 Xilinx Vitis HLS 写的 C++ 函数，综合后变成 FPGA 上的硬件数据通路。学完本讲你应当能够：

1. 说清楚**为什么必须有这个内核**：AIE 阵列末端的 packet merger 会把 224 个重建内核的输出打乱成乱序流，必须有人把它还原成连续图像。
2. 读懂 **HLS 接口 pragma**：`axis`、`m_axi`、`s_axilite` 各自把 C++ 函数参数映射成什么样的硬件接口，以及主机如何通过 `set_arg` 把 DDR 地址喂给 PL。
3. 逐位解析 **128 位包头**：哪个 32 位字放包交换头（`pkt_id` / `pkt_type` / 源行列 / 奇偶校验），哪个 32 位字放内核实例号 `instance_id`。
4. 解释 **`ddr_offset = instance_id * SAMPLES_PER_KERN`** 为什么能让乱序到达的图像数据被写回 DDR 中不重叠、连续的区域。

本讲是「PL DMA 包路由器」单元的第一讲，只讲内核本身；它的仿真 testbench 与 csim/cosim 流程留给下一讲 u6-l2。

## 2. 前置知识

在进入源码前，先回顾几个本讲要直接用到的、前面讲义已建立的认知：

- **PLIO 与 GMIO 的区别（u2-l2）**：GMIO 是 AIE 经片上网络（NoC）到 DDR 的 DMA 通道；PLIO 则是 AIE 与 PL 之间 128 位的 AXI4-Stream **直连流**，不过 NoC。本讲里包路由器的 `axis` 输入就是一条从 AIE 飞过来的 PLIO 流，而它的 `m_axi` 输出则要过 NoC 才能到 DDR。
- **包交换与 packet merger（u5-l2、u5-l5）**：每个 switch 上的 32 个图像重建内核，各自输出一个 `output_pktstream`，经 `pktmerge<32>` 合并成**一条**物理流，再由一个 `output_plio` 送出芯片到 PL。包头里的 `pkt_id`（5 位）让交换网络把包路由到正确目的地。
- **重建内核的输出布局（u5-l5）**：每个重建内核在最后一脉冲被 RTP 触发后，先写一个 128 位的「元数据拍」（含包交换头 + 自己的实例号 `m_id`），再写自己负责的那段聚焦图像（`SAMPLES` 个 cfloat），末拍带 `tlast`。
- **整除约束（u1-l4）**：每核样本数 \((\text{PULSES}\times\text{RC\_SAMPLES})/\text{IMG\_SOLVERS}\) 必须是整数，默认配置下为 1376。本讲里它记作 `SAMPLES_PER_KERN`，是路由器正确切包的前提。
- **HLS 是什么**：高层次综合（High-Level Synthesis），把 C++ 函数编译成 FPGA 的 RTL。函数参数加上 `#pragma HLS INTERFACE`，就规定了该参数在硬件上对应哪种总线接口。

一个关键数字贯穿全讲，先记住：默认配置下

\[
\text{SAMPLES\_PER\_KERN} = \frac{\text{PULSES}\times\text{RC\_SAMPLES}}{\text{IMG\_SOLVERS}} = \frac{602\times 512}{7\times 32} = \frac{308224}{224} = 1376
\]

即每个重建内核产出 1376 个 cfloat（复数像素），224 个内核合起来正好 \(308224 = \text{PULSES}\times\text{RC\_SAMPLES}\) 个 cfloat，就是一整幅图像。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [design/pl/dma_pkt_router.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.h) | 包路由器内核的头文件，声明顶层函数签名 | `ap_axiu<128>` 流输入与 `ap_uint<64>*` DDR 输出的类型约定 |
| [design/pl/dma_pkt_router.cpp](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp) | 包路由器内核的实现，HLS 顶层函数 | 四条 INTERFACE pragma、包头解析、`ddr_offset` 计算与图像重排循环 |
| [design/common.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h) | 三域共享配置头 | `PULSES`、`RC_SAMPLES`、`IMG_SOLVERS`、`IMG_SOLVERS_PER_SWITCH` 等规模宏 |
| [design/aie/backprojection.cc](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc) | AIE 侧重建内核，产生本路由器要消费的流 | dump 阶段写出的元数据拍与数据拍布局（理解输入的来源） |
| [design/aie/graph.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h) | ADF 图拓扑 | `pktmerge<32>` → `output_plio` 这条产生乱序的合并链路 |
| [design/host/sar_backproject.cpp](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp) | 主机编排 | 如何 `set_arg(1, buffer)` 把 DDR 地址传给 `ddr_mem`、如何启动 7 个路由器实例 |

## 4. 核心概念与源码讲解

本讲按四个最小模块推进：先讲清楚乱序问题与路由器存在的必要性，再依次讲接口 pragma、包头解析、DDR 偏移计算。

### 4.1 为什么需要包路由器：packet merger 的乱序问题

#### 4.1.1 概念说明

反投影计算的末端，224 个图像重建内核各自产出一段聚焦好的复数图像。在 ADF 图里（见 u5-l5、graph.h），每个 switch 的 32 个重建内核都接到同一个 `pktmerge<32>` 上，后者把 32 条 `output_pktstream` 合并成**一条**物理流，再经一个 `output_plio` 以 128 位 AXI4-Stream 送出 AIE 阵列、进入 PL。

问题在于：`pktmerge` 是按包交换的 `pkt_id` 来路由数据的，但它**不保证 32 个内核的包在合并流上出现的先后顺序**。哪个内核先跑到 dump 阶段、先把图像写完，它的包就先出现在合并流上。这个先后由 AIE 流水线（pipelining）的运行时调度决定，本质上是**不确定的**——源码头注释就把这个内核描述为「undoes the randomization caused by AI pipelining optimization」（消除 AIE 流水优化引入的随机化）。

于是 PL 收到的流大致是这样的（每个 `[…]` 是一个内核的一整包数据）：

```
合并流（顺序不确定）：[…内核17的数据…][…内核3的数据…][…内核30的数据…]…
我们希望的 DDR 布局：  [内核0][内核1][内核2] … [内核223]   ← 连续、有序
```

如果直接把合并流顺序写进 DDR，每次运行的图像都会按不同的乱序排列，根本无法用。因此需要一个硬件模块在写入 DDR 之前**按内核身份把每段数据放回正确位置**——这就是 `dma_pkt_router` 存在的唯一理由。它是一个「去乱序器」/「重排序器」。

#### 4.1.2 核心流程

包路由器工作在「包」的粒度。对每一个到达的内核数据包，它做三件事：

1. **读一拍 128 位元数据**：里面既有包交换头（含 `pkt_id`），也有内核主动写入的全局实例号 `instance_id`。
2. **用 `instance_id` 算出这段数据在 DDR 里的基址偏移**：`ddr_offset = instance_id * SAMPLES_PER_KERN`。
3. **把后续的数据拍顺序写到 `ddr_mem[ddr_offset + …]`**，这样无论这个包是第几个到达，它的数据都落在属于自己的那段 DDR 里。

因为所有 224 个内核的 `instance_id` 互不相同、且每段长度都是 `SAMPLES_PER_KERN`，这些 DDR 区段两两不重叠，合起来恰好拼成完整图像。

> 注意一个关键区别：`pkt_id` 只在一个 switch 内部（0~31）唯一，跨 7 个 switch 会重复；而 `instance_id` 在全图 0~223 范围内全局唯一（由 `IMG_SOLVERS_PER_SWITCH*bp_subgraph_insts + i` 生成，见 u4-l2）。所以重排序用的「身份证」必须是 `instance_id`，而不是 `pkt_id`。

#### 4.1.3 源码精读

路由器的角色定位写在文件头注释里：

[design/pl/dma_pkt_router.cpp:1-5](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L1-L5) —— 头注释直接点明它是「消除 AIE 流水优化引入的随机化」的 AI 包路由交换。

AIE 侧产生这种乱序的源头在 graph.h 的合并连线上：

[design/aie/graph.h:81-82](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L81-L82) —— `pktmerge<32>` 的输出接到 `plio_pkt_rtr_out`，即 32 路先合并、再以一条 128 位 PLIO 流送出。合并是产生乱序的地方。

而每个重建内核在 dump 时写出的、供路由器识别身份的元数据，在 backprojection.cc 里：

[design/aie/backprojection.cc:196-204](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L196-L204) —— 先 `writeHeader` 写包交换头，紧接着 `writeincr(img_out, m_id, false)` 把自己的实例号 `m_id` 写进第二个 32 位字，再补两个 0 凑满 128 位（一个 PLIO 拍 = 4×32 位）。这就是路由器后面要读的「元数据拍」。

#### 4.1.4 代码实践

**实践目标**：建立「合并 → 乱序 → 重排」的直觉。

**操作步骤**：

1. 打开 graph.h，确认每个 switch 里 32 个 `img_rec_km` 的输出都汇入同一个 `mg`（`pktmerge<32>`），再经 `plio_pkt_rtr_out` 出芯片。
2. 想象 32 个内核按不确定顺序完成 dump，在纸上画出两种不同的到达顺序（例如 `17,3,30,…` 和 `0,21,9,…`），并在每种顺序下标注：若不加路由器，DDR 里图像会变成什么样。

**需要观察的现象**：到达顺序变化时，「直接顺序写入」得到的 DDR 内容会完全不同；而「按 `instance_id` 写入」得到的 DDR 内容在两种顺序下**完全一致**。

**预期结果**：你会直观体会到——路由器把「与运行时调度相关的到达顺序」转换成了「与内核身份绑定的固定位置」，从而把不确定性消除掉。

#### 4.1.5 小练习与答案

**练习 1**：为什么不能用 `pkt_id` 做 DDR 偏移的「身份证」？

**参考答案**：`pkt_id` 只有 5 位，取值 0~31，只在单个 switch 内部唯一；7 个 switch 上都会有自己的「`pkt_id=5`」内核，直接用 `pkt_id` 会发生区段碰撞。`instance_id` 在 0~223 全局唯一，区段才不会重叠。

**练习 2**：如果所有 224 个内核同时完成 dump、`pktmerge` 输出严格按 `instance_id` 从小到大排列，路由器还有用吗？

**参考答案**：仍然有用，但属于「恰好不需要纠错」的幸运情况。路由器的逻辑对到达顺序不敏感（它始终用 `instance_id` 定位），所以无论乱序与否结果都正确；把它放在那里是为了对冲 AIE 调度的不确定性，保证可复现。

---

### 4.2 HLS 接口 pragma：axis / m_axi / s_axilite

#### 4.2.1 概念说明

HLS 把 C++ 函数变成硬件时，函数的每个参数都要决定「在硬件上变成什么接口」。Vitis HLS 用 `#pragma HLS INTERFACE` 来声明。本内核只有两个参数，却用到了三类最常用的接口：

- **`axis`（AXI4-Stream）**：无地址的连续流，带 `TLAST`/`TKEEP` 等边带信号。本内核的输入 `pl_stream_in` 就是它——AIE 通过 PLIO 送来的 128 位图像流。
- **`m_axi`（AXI4 Master）**：PL 作为主设备（master），自己去读写一片地址空间（这里就是 DDR）。`ddr_mem` 用它，意味着 PL 内核会主动把数据写到 DDR 里。
- **`s_axilite`（AXI4-Lite Slave）**：轻量的控制寄存器接口。主机（ARM）用它来「填表」——写下要 PL 干活的参数（比如 DDR 基地址、启动信号）。

`ap_axiu<128,0,0,0>` 是 Xilinx 提供的 AXI4-Stream 数据类型模板：4 个模板参数依次是 **数据位宽 W、用户位宽 U、事务 ID 位宽 TI、目标位宽 TD**。所以 `<128,0,0,0>` 表示 128 位数据、不要 TUSER/TID/TDEST，但仍保留标准的 `TLAST`、`TKEEP`、`TSTRB` 等边带信号（可通过 `.data`、`.last`、`.keep` 访问）。

#### 4.2.2 核心流程

本内核的接口契约可以概括为「一流入、一出写、一控制」：

1. **数据流入**：`pl_stream_in`（axis）——AIE 经 PLIO 送来的 128 位流，每拍要么是元数据、要么是图像数据。
2. **数据写出**：`ddr_mem`（m_axi）——PL 作为 master，经 NoC 把重排后的图像写进 DDR。
3. **控制面**：两条 `s_axilite`——其中一条绑定到 `ddr_mem`（告诉内核「DDR 基地址在哪」），另一条绑定到 `return`（启动/完成握手）。

关键技巧是 `m_axi ... offset=slave`：它让 m_axi 的基地址**不由内核硬编码，而是由一个 s_axilite 控制寄存器在运行时写入**。同一条 `s_axilite port=ddr_mem bundle=control` 正是为此而设。主机侧的 `set_arg(1, buffer)` 就是往这个控制寄存器里写 buffer 的物理地址——这正是 u3-l2 提到的「`ddr_mem` 是 arg 1、`m_axi bundle=gmem` 直接寻址的普通 `xrt::bo`」的硬件对应面。

#### 4.2.3 源码精读

头文件给出顶层函数签名，两个参数的类型即决定了「流进 128 位、写出 64 位 DDR 字」：

[design/pl/dma_pkt_router.h:15-16](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.h#L15-L16) —— `hls::stream<ap_axiu<128,0,0,0>>& pl_stream_in` 与 `ap_uint<64>* ddr_mem`。

四条 pragma 集中在函数开头，是整个接口契约的核心：

[design/pl/dma_pkt_router.cpp:13-16](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L13-L16) —— 逐条含义：

- `axis port=pl_stream_in`：把流参数映射成 AXI4-Stream 输入接口。
- `m_axi port=ddr_mem offset=slave bundle=gmem depth=PULSES*RC_SAMPLES`：PL master 到 DDR；`offset=slave` 表示基地址运行时可配；`bundle=gmem` 把它归到一组数据总线上；`depth=308224` 告诉 HLS 仿真阶段最大访问范围。
- `s_axilite port=ddr_mem bundle=control`：把 `ddr_mem` 的**基地址指针**也挂到一个 AXI-Lite 控制寄存器上，主机可写。
- `s_axilite port=return bundle=control`：内核启动/返回的握手寄存器。

主机侧如何往这套控制接口里「填表」，见 sar_backproject.cpp：

[design/host/sar_backproject.cpp:320-324](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L320-L324) —— `set_arg(1, buffers_img_out[buff_idx])` 把图像 buffer 的地址写进 arg 1（即 `ddr_mem` 的 s_axilite 寄存器），随后 `start()` 拉高启动信号。这就是 `offset=slave` 的运行时兑现。

#### 4.2.4 代码实践

**实践目标**：把 C++ 参数 → 硬件接口 → 主机调用的三段对应关系打通。

**操作步骤**：

1. 在 dma_pkt_router.cpp 的四条 pragma 旁，分别用一句话标注它对应的硬件接口（axis 流 / m_axi 主口 / s_axilite 控制寄存器 ×2）。
2. 跳到 sar_backproject.cpp:320-324，确认 `set_arg(1, …)` 的「1」正对应函数签名里第二个参数 `ddr_mem`（第一个参数是 `pl_stream_in`，索引 0；PLIO 流的连接由 system.cfg 里的 `stream_connect` 完成，不经 arg，见 u4-l3）。
3. 回顾 u3-l2 的结论：`m_img_buffers` 用 `kernels[0].group_id(1)` 选 DDR bank，正是因为 arg 1 是 `m_axi bundle=gmem`，bank 必须与 m_axi 绑定一致。

**需要观察的现象**：`ddr_mem` 既是 m_axi（数据面）又是 s_axilite（控制面）——同一参数身兼两职，由 `offset=slave` 串联起来。

**预期结果**：你能画出「ARM 写控制寄存器（设基地址 + 启动）→ PL 内核通过 m_axi 主动写 DDR → AIE 的 PLIO 流经 axis 灌入 PL」这条三域协作链路。

#### 4.2.5 小练习与答案

**练习 1**：`ap_axiu<128,0,0,0>` 的四个模板参数分别是什么？为什么本设计把后三个设为 0？

**参考答案**：依次是数据位宽 W、用户位宽 U、事务 ID 位宽 TI、目标路由位宽 TD。本设计只靠 `pkt_id`（放在数据载荷里、由软件解析）来区分包，不需要 AXIS 硬件层的 TUSER/TID/TDEST，故后三个设为 0 以节省硬件资源。

**练习 2**：如果把 `offset=slave` 改成 `offset=direct`，主机的调用方式要怎么变？

**参考答案**：`offset=direct` 表示 m_axi 的基地址在综合时固定、运行时不可改，内核永远写到同一个硬编码地址。那样主机就**无法**用 `set_arg(1, buffer)` 切换目标 buffer；要把基地址编死在内核里、或换别的机制传地址，灵活性大降。这就是本设计选 `offset=slave` 的原因。

---

### 4.3 包头部解析：pkt_id、pkt_type 与 instance_id

#### 4.3.1 概念说明

路由器要为每段数据算出正确的 DDR 偏移，前提是先从流里「认出」这段数据属于哪个内核。这件事靠的是每个内核数据包开头的那个 128 位元数据拍。这一拍在 AIE 侧由 `writeHeader` + 三个 `writeincr` 拼成（见 4.1.3 引用的 backprojection.cc:196-204），它的 4 个 32 位字含义如下：

| 字 | 比特位 | 内容 | 谁写的 |
|----|--------|------|--------|
| 字 0 | `[31:0]` | **包交换头**（packet switch header） | `writeHeader`，硬件/编译器填充 |
| 字 1 | `[63:32]` | **内核实例号 `instance_id`**（即 `m_id`） | 内核自己 `writeincr(img_out, m_id)` |
| 字 2 | `[95:64]` | 0（占位，留作将来扩展） | 内核写 0 |
| 字 3 | `[127:96]` | 0（占位） | 内核写 0 |

其中**包交换头**这 32 位是 AMD AIE 包交换网络定义的固定格式（见 AMD 文档 UG1079）。源码里直接把它抄成了一张位域表，本讲实践任务就要逐位拆它：

| Bits | Field |
|------|-------|
| 4–0 | Packet ID（`pkt_id`） |
| 11–5 | 保留 7'b0000000 |
| 14–12 | Packet Type（`pkt_type`） |
| 15 | 保留 1'b0 |
| 20–16 | Source Row（源 tile 行） |
| 27–21 | Source Column（源 tile 列） |
| 30–28 | 保留 3'b000 |
| 31 | 奇偶校验位（bits[30:0] 的奇偶） |

两个关键提醒：

- 路由器**解析了** `pkt_id` 和 `pkt_type`，但**没有用它们**来决定 DDR 偏移（偏移只用 `instance_id`）。这两者被解码出来，更像是为了可观测性或未来扩展（源码注释也提到 bits 64–128「将来可用于额外元数据」）。
- 真正用于重排序的「身份证」是字 1 里的 `instance_id`——它是内核在 dump 时主动写进去的自己的全局编号（0~223）。

#### 4.3.2 核心流程

解析一个元数据拍的步骤：

1. 从 `pl_stream_in` 读一拍 128 位，存进 `metadata`（`ap_axiu<128>`）。
2. 取低 32 位作为包交换头 `header = metadata.data.range(31, 0)`。
3. 按位掩码提取：
   - `pkt_id = header & 0x1F`（取 bits 4:0）。
   - `pkt_type = (header & 0x7000) >> 12`（取 bits 14:12，再右移到低位）。
4. 取第二个 32 位作为身份号 `instance_id = metadata.data.range(63, 32)`。
5. bits 64–128 是占位，忽略。

位掩码小贴士：`0x1F` = 二进制 `...0001_1111`，正好选中最低 5 位；`0x7000` = `0111_0000_0000_0000`，正好选中 bits 14:12 这 3 位。

#### 4.3.3 源码精读

元数据相关的局部变量声明，类型位宽直接反映了位域宽度：

[design/pl/dma_pkt_router.cpp:20-28](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L20-L28) —— `ap_uint<5> pkt_id`（5 位对应 `pkt_id` 位域）、`ap_uint<3> pkt_type`（3 位对应 `pkt_type` 位域）、`ap_uint<32> instance_id`。

整个解析逻辑紧跟在「读元数据拍」之后，源码自带完整的 UG1079 位域注释：

[design/pl/dma_pkt_router.cpp:33-56](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L33-L56) —— 先 `metadata = pl_stream_in.read()` 读入元数据拍；第 36–50 行是包交换头位域表；第 51–53 行用掩码解出 `header`、`pkt_id`、`pkt_type`；第 56 行 `instance_id = metadata.data.range(63, 32)` 取第二字作为身份号；第 58–60 行注明 bits 64–128 为占位、留作未来扩展。

#### 4.3.4 代码实践

**实践目标**：把 `metadata.data.range(31,0)` 这个 32 位包交换头逐位拆开，与本模块给出的位域表一一对应。

**操作步骤**：

1. 假设某次仿真读到 `header` 的 32 位十六进制值为 `0x008AB60Bu`（示例值）。把它展开成二进制，按下表逐段切分：

   | Bits | 字段 | 掩码 | 你切出的值 |
   |------|------|------|-----------|
   | 4–0 | `pkt_id` | `& 0x1F` | 待填 |
   | 14–12 | `pkt_type` | `& 0x7000` 再 `>>12` | 待填 |
   | 20–16 | Source Row | `& 0x001F_0000` 再 `>>16` | 待填 |
   | 27–21 | Source Column | `& 0x0FE0_0000` 再 `>>21` | 待填 |
   | 31 | 奇偶校验 | `& 0x8000_0000` 再 `>>31` | 待填 |

2. 对照源码注释里的位域表（dma_pkt_router.cpp:39–50），确认你切的每一段都和注释一致。
3. 验证 bits 11–5、15、30–28 在你的示例值里是否如注释所说为保留位（应为 0 或可忽略）。

**需要观察的现象**：仅用「掩码 + 移位」就能无歧义地取出每个字段；`pkt_id` 与 `instance_id` 来自**不同的 32 位字**，互不干扰。

**预期结果**：你能流畅地把一个 32 位 header 解释成「包号 / 包类型 / 源 tile 行列 / 校验」五个字段，并理解为什么路由器算偏移时只用 `instance_id` 而把 `pkt_id` 摘出来却不用。本步结果取决于你给的示例值，**具体数值待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`header & 0x7000` 得到的结果最大是多少？为什么还要 `>> 12`？

**参考答案**：`0x7000` 选中 bits 14:12，最大值为 `0x7000`（即 `pkt_type` 字段为全 1 时）。`>>12` 把这 3 位从 bits 14:12 搬到 bits 2:0，使 `pkt_type` 成为 0~7 的整数，便于直接使用。

**练习 2**：既然路由器不靠 `pkt_id` 定位 DDR，为什么还要读它？

**参考答案**：源码解析 `pkt_id`/`pkt_type` 但未用于偏移，属于「摘出来备查/留作扩展」。真正定位用的是 `instance_id`。保留解析能让调试时观察包的来源，也与 UG1079 文档对齐；但就「正确重排图像」这一功能而言，`instance_id` 一个字段就够了。

**练习 3**：`instance_id` 的取值范围是多少？为什么 `ap_uint<32>` 装 0~223 绰绰有余？

**参考答案**：`instance_id` 即 `m_id`，全局范围 0~223（共 224 个重建内核）。8 位即可表示（最大 255），用 32 位是因为它和 PLIO 的 32 位通道宽度对齐、由 `writeincr(img_out, m_id)` 按 32 位写入，便于和包交换头共占同一拍的相邻字。

---

### 4.4 ddr_offset 计算与图像重排

#### 4.4.1 概念说明

认出每段数据的「主人」（`instance_id`）之后，最后一步是算出它该写到 DDR 的哪里。公式极其简单：

\[
\text{ddr\_offset} = \text{instance\_id} \times \text{SAMPLES\_PER\_KERN}
\]

其中 `SAMPLES_PER_KERN = (PULSES*RC_SAMPLES)/IMG_SOLVERS = 1376`，即每个内核负责的 cfloat 像素数。`ddr_mem` 的类型是 `ap_uint<64>*`——一个 64 位整型数组，每个元素正好放一个 cfloat（32 位实部 + 32 位虚部）。所以 `ddr_offset` 的单位是「cfloat 个数」。

为什么这就保证了图像连续且不重叠？因为 `instance_id` 在 0~223 全局唯一，每段长度都是 1376，所以：

- 内核 0 占 `[0, 1376)`
- 内核 1 占 `[1376, 2752)`
- ……
- 内核 \(k\) 占 \([1376k,\ 1376(k+1))\)

224 段首尾相接，总长 \(224\times 1376 = 308224 = \text{PULSES}\times\text{RC\_SAMPLES}\)，正好是一整幅图像，没有任何缝隙或重叠。这正是 u1-l4「整除约束」的回报——只有当 `SAMPLES_PER_KERN` 是整数时，这个等分才成立。

#### 4.4.2 核心流程

整个内核由两层循环构成，外层按包（内核）计数，内层按数据拍计数：

```
外层 IMG_KERNEL_LOOP：循环 IMG_SOLVERS_PER_SWITCH（=32）次   ← 每个 switch 上 32 个内核
    读 1 拍元数据 → 解析 instance_id
    ddr_offset = instance_id * SAMPLES_PER_KERN
    内层 IMG_DATA_LOOP：idx 从 0 到 SAMPLES_PER_KERN（=1376），步进 2
        读 1 拍 128 位图像数据（含 2 个 cfloat）
        ddr_mem[ddr_offset + idx]     = img_data.data.range(63, 0)    ← 第 1 个 cfloat
        ddr_mem[ddr_offset + idx + 1] = img_data.data.range(127, 64)  ← 第 2 个 cfloat
```

两个细节值得点出：

- **为什么内层步进 2？** 因为 PLIO 是 128 位、一个 cfloat 是 64 位，所以**一拍装 2 个 cfloat**。内层每迭代处理一拍，写 2 个 `ap_uint<64>` 元素，故 `idx += 2`，共 \(1376/2 = 688\) 次迭代。
- **外层只循环 32 次，但全局有 224 个内核？** 因为一个 `dma_pkt_router` 实例只服务**一个 switch 的那条 PLIO 流**（流上承载的就是该 switch 32 个内核的包）。7 个 switch 对应 7 个 PL 内核实例（见 sar_backproject.cpp:317-325 的循环），每个实例各自处理 32 段；它们的 `instance_id` 区段互不重叠（switch \(s\) 上的内核 `instance_id` 为 \(32s+i\)），所以 7 个实例可以并行写到同一个 DDR buffer 而不冲突。

> 关于 `tlast`：AIE 侧在每包末拍置 `tlast=true`（backprojection.cc:212），但本路由器**不检查** `img_data.last`——它完全靠「外层 32 次、内层 688 次」的固定计数来切包。这种纯计数定帧能工作，前提正是整除约束成立、每包数据量严格相等。

#### 4.4.3 源码精读

`SAMPLES_PER_KERN` 在函数开头算出，是后续切包与定偏移的基础：

[design/pl/dma_pkt_router.cpp:18](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L18) —— `const int SAMPLES_PER_KERN = (PULSES*RC_SAMPLES)/IMG_SOLVERS;` 默认为 1376。

外层循环 + 元数据读取：

[design/pl/dma_pkt_router.cpp:31-34](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L31-L34) —— `IMG_KERNEL_LOOP` 循环 `IMG_SOLVERS_PER_SWITCH`（32）次，每次先 `pl_stream_in.read()` 读一拍元数据。

偏移计算在解析完包头之后：

[design/pl/dma_pkt_router.cpp:62-63](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L62-L63) —— `int ddr_offset = instance_id*SAMPLES_PER_KERN;` 这是整个内核「去乱序」的核心一行。

内层数据搬运循环，把每拍 2 个 cfloat 写到正确的 DDR 位置：

[design/pl/dma_pkt_router.cpp:65-71](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L65-L71) —— `IMG_DATA_LOOP` 步进 2，`ddr_mem[ddr_offset+idx]` 取低 64 位、`ddr_mem[ddr_offset+idx+1]` 取高 64 位，恰好对应一拍里的两个 cfloat。

主机侧如何并行启动 7 个实例、且都指向同一个图像 buffer：

[design/host/sar_backproject.cpp:317-325](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L317-L325) —— 对每个 switch 各启动一个 `dma_pkt_router` 实例（`pl_kern_id = buff_idx*m_instances + sw_id`），全部 `set_arg(1, buffers_img_out[buff_idx])` 指向同一块图像 buffer，再各自 `start()`。由于 `instance_id` 区段不重叠，7 路并行写入不会冲突。

#### 4.4.4 代码实践

**实践目标**：验证 `ddr_offset = instance_id * SAMPLES_PER_KERN` 真的能让 224 段数据两两不重叠、拼成连续图像。

**操作步骤**：

1. 计算 `SAMPLES_PER_KERN`（默认 1376），并算出内核 `instance_id = 0, 1, 2, …, 223` 各自的 `ddr_offset` 区段 \([1376k,\ 1376(k+1))\)。
2. 验证：相邻两段是否首尾相接？最后一段（\(k=223\)）的上界 \(1376\times 224\) 是否正好等于 `PULSES*RC_SAMPLES`（308224）？
3. 进一步：switch 0 处理 `instance_id` 0~31（区段 \([0, 44032)\)），switch 1 处理 32~63（区段 \([44032, 88064)\)），…，确认 7 个 switch 的区段恰好把 \([0, 308224)\) 七等分、互不重叠。这就是 7 个 PL 实例能并行写同一 buffer 的依据。
4. 把 `IMG_DATA_LOOP` 的迭代次数算出来：`SAMPLES_PER_KERN/2 = 688`，并解释为什么每迭代写 2 个 `ap_uint<64>`。

**需要观察的现象**：`instance_id` 从 0 递增到 223 时，`ddr_offset` 区段像铺地砖一样无缝拼接，没有任何两个内核争抢同一片 DDR。

**预期结果**：\(224 \times 1376 = 308224 = 602 \times 512\)，与一整幅 \(602\times 512\) 的图像 cfloat 数一致。图像被连续、无重叠地重建出来。

**代码阅读型变体（无需硬件）**：在 dma_pkt_router.cpp 里确认内层循环边界是 `idx < SAMPLES_PER_KERN` 且 `idx += 2`，因此正好读 `SAMPLES_PER_KERN/2` 拍、写 `SAMPLES_PER_KERN` 个 `ap_uint<64>`。若把步进误改为 `idx++`，会发生什么？（答：每个 `ap_uint<64>` 会被一拍的低 64 位和高 64 位重复写两次相邻元素，且循环次数翻倍、越界读流——这是很好的边界条件检验。）

#### 4.4.5 小练习与答案

**练习 1**：默认配置下，`ddr_mem` 至少要多少个 `ap_uint<64>` 元素？合多少字节？

**参考答案**：\( \text{PULSES}\times\text{RC\_SAMPLES} = 308224\) 个元素（与 pragma `depth=PULSES*RC_SAMPLES` 及 testbench 的 `malloc(PULSES*RC_SAMPLES*8)` 一致）。每个 64 位 = 8 字节，合计 \(308224\times 8 = 2465792\) 字节 ≈ 2.35 MiB。

**练习 2**：为什么 7 个 PL 实例同时写同一块 `ddr_mem` 不会产生数据竞争？

**参考答案**：每个 switch 上 32 个内核的 `instance_id` 为 \(32s+i\)（\(s\) 为 switch 号），不同 switch 的 `instance_id` 集合不相交（switch 0 占 0~31，switch 1 占 32~63，…）。`ddr_offset = instance_id*1376`，因此 7 个实例写入的 DDR 区段两两不重叠，并行写也不会互相覆盖。

**练习 3**：如果把 `PULSES` 从 602 改成一个让 `SAMPLES_PER_KERN` 不是整数的值，路由器会在哪一步出错？

**参考答案**：能综合通过，但运行时数据会被错位：整数除法 `PULSES*RC_SAMPLES/IMG_SOLVERS` 会截断，`SAMPLES_PER_KERN` 与每个内核实际产出的 cfloat 数对不上，导致内层循环要么少读（丢像素）、要么多读（越界读流、与下一个包的元数据拍错位），最终图像错位甚至损坏。这正是 u1-l4 强调「整除约束」的现实后果。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个端到端的小任务。

**任务**：用一段话 + 一张表，向一个没读过本内核的同事解释「一段 AIE 图像数据包，从离开 AIE 阵列到落进正确 DDR 位置，中间发生了什么」。

**建议步骤**：

1. **画输入**：画出 32 个重建内核 → `pktmerge<32>` → `output_plio`（128 位 AXI4-Stream）→ `dma_pkt_router` 的 `axis` 输入这条链路；并标注「合并后的顺序不确定」。
2. **拆包头（对应 4.3 实践）**：对照 AMD UG1079 包头位域表，逐位说明 `metadata.data.range(31,0)` 里 `pkt_id`（bits 4:0）、`pkt_type`（bits 14:12）、Source Row（bits 20:16）、Source Column（bits 27:21）、奇偶校验（bit 31）的位置；再指出 `instance_id` 在 `metadata.data.range(63,32)`。
3. **解释偏移（对应 4.4 实践）**：说明 `ddr_offset = instance_id * SAMPLES_PER_KERN` 如何保证 224 段数据落到不重叠的 DDR 区段、首尾相接拼成一幅连续图像（\(224\times 1376 = 308224 = \text{PULSES}\times\text{RC\_SAMPLES}\)）。
4. **点出接口（对应 4.2 实践）**：说明 `axis` 是 AIE→PL 的流入口、`m_axi(offset=slave)` 是 PL→DDR 的写出口、`s_axilite` 是主机用来填 DDR 基地址（`set_arg(1, buffer)`）的控制面。

**自检清单**（如果都能答出，本讲就掌握了）：

- [ ] 路由器存在的唯一理由是什么？（消除 packet merger 的乱序）
- [ ] 为什么用 `instance_id` 而不是 `pkt_id` 定位 DDR？（全局唯一 vs switch 内唯一）
- [ ] 一拍 128 位能装几个 cfloat？内层循环为什么步进 2？（2 个；步进 2）
- [ ] `offset=slave` 配合 `s_axilite` 解决了什么问题？（运行时由主机配置 DDR 基地址）
- [ ] 7 个 PL 实例并行写同一 buffer 为何不冲突？（`instance_id` 区段不重叠）

## 6. 本讲小结

- `dma_pkt_router` 存在的唯一理由，是消除 AIE 末端 `pktmerge<32>` 因流水调度而产生的**到达顺序随机化**，把乱序流还原成 DDR 中连续、有序的图像。
- 内核用三类 HLS 接口：`axis`（128 位 AXI4-Stream，AIE→PL 流入）、`m_axi offset=slave bundle=gmem`（PL→DDR 主写口）、`s_axilite`（主机控制面，写 DDR 基地址与启动）。
- 每个数据包以一拍 128 位元数据开头：低 32 位是 UG1079 包交换头（含 `pkt_id`/`pkt_type`/源行列/奇偶校验），第二 32 位是内核全局实例号 `instance_id`。
- 真正用于重排序的「身份证」是 `instance_id`（全局 0~223），`pkt_id`/`pkt_type` 被解析但未用于偏移。
- `ddr_offset = instance_id * SAMPLES_PER_KERN`（默认 `SAMPLES_PER_KERN=1376`），让 224 段数据落入不重叠区段，首尾相接拼成 \(308224 = \text{PULSES}\times\text{RC\_SAMPLES}\) 个 cfloat 的完整图像。
- 内层循环步进 2 是因为「一拍 128 位 = 2 个 64 位 cfloat」；整个切包纯靠固定计数、不依赖 `tlast`，其正确性建立在 u1-l4 的整除约束之上。

## 7. 下一步学习建议

本讲只讲了「内核本身」。要把它真正跑起来，还需要：

- **下一讲 u6-l2（PL 包路由器仿真与 testbench）**：阅读 `dma_pkt_router_tb.cpp` 与 `run_dma_pkt_router_tb.tcl`，看 testbench 如何把 aiesim 产出的 CSV 流数据喂进 `hls::stream`、调用本内核、再把 DDR 结果写成 `output_img.csv` 验证重排正确性，并理解 csim/cosim 流程与 VCK190 器件/312.5 MHz 时钟设置。
- **回看 u7-l1（系统集成）**：理解本内核的 `axis` 流如何由 system.cfg 里的 `stream_connect` 与 AIE 的 PLIO 接通、`nk=` 如何实例化 7 个 `dma_pkt_router`，以及整个 XSA 如何链接打包。
- **延伸阅读**：AMD 文档 **UG1079**（Versal AIE 包交换 / Packet Switching）是包头位域的权威出处，建议对照本讲 4.3 的位域表精读一次；同时可关注 UG1393（Vitis HLS）对 `INTERFACE` pragma 的完整说明。
