# 讲义 u4-l2：包交换、内核实例化与 tile 布局

## 1. 本讲目标

上一讲（u4-l1）我们看清了反投影图的「静态拓扑」：1 个 Data Broadcast + 7 个 bpCluster，每个 bpCluster 里 1 个 Pixel Demux + 32 个 Image Reconstruction，三类内核总数 232 个。但那张拓扑图留下三个「怎么做」的悬念：

- 一个 Pixel Demux 内核只有 **一个** 输出端口，怎么把像素分发给 **32** 个重建内核？
- 224 个重建内核在阵列里长得一模一样，PL 侧的包路由器怎么分辨「这块图像数据是第几个内核产出的」？
- 几百个内核挤在 AIE 阵列里，编译器为什么不许它们乱跑、而要把每个 bpCluster 钉死在一小块矩形区域里？

学完本讲你应当能够：

1. 说清 `pktsplit` / `pktmerge` 这类**包交换对象**的作用，以及「最多 32 路」这一硬约束从何而来。
2. 掌握 `kernel::create_object<ImgReconstruct>(id)` 如何给每个重建内核传入**全局唯一 id**，并追踪这个 id 一路流向 PL 的 DDR 偏移。
3. 读懂 `location<graph>(*this) = area_group({...})` 如何把每个子图**约束**到固定 tile 区域，并解释列号为何按 7 递增。
4. 追踪 `bp_subgraph_insts` 与 `bp_graph_insts` 两个计数器如何借 C++ 成员构造顺序，为端口和内核生成全局唯一命名。

本讲只讲「图怎么搭、内核怎么造、tile 怎么摆」，**不**展开内核内部的反投影算法（那是 u5 的事），也**不**展开 PL 包路由器本身（那是 u6-l1 的事）。

## 2. 前置知识

在进入源码前，先建立三点直觉。本讲默认你已读过 u4-l1（图与子图架构总览）、u2-l2（GMIO/PLIO/buffer/stream/RTP）与 u2-l3（ADF 图与数据驱动执行）。

### 2.1 普通流端口「扇出」是有上限的

AIE tile 之间的 AXI4-Stream 连接走的是 tile 内部的 stream switch。一个普通流端口能挂载的点对点连接数有限。当我们需要「1 个源 → 32 个目的」时，靠 `connect` 硬连 32 条线既不现实（端口不够），也浪费路由资源。我们需要一种**复用**机制：用一条物理流，承载 32 路逻辑数据，靠标签区分。

### 2.2 包流（packet stream）：带标签的流

ADF 提供一种带包头的流类型 `pktstream`。每个数据包前面有一个 32 位包头，里面编码了一个 **pkt_id**（包标识）。发送方在写数据前先用 `writeHeader(port, pkt_type, pkt_id)` 贴标签；网络里的**包交换对象**（`pktsplit` / `pktmerge`）就根据这个标签把包路由到不同端口。

一句话区分（承接 u2-l2）：

| 机制 | 适用场景 | 本设计用于 |
| --- | --- | --- |
| `connect` + `single_buffer` | 同一份数据**广播**给多个内核 | slowtime / RC（每核都拿到完整副本） |
| `pktsplit` / `pktmerge`（包交换） | 不同数据**分发**给多个内核，或多个内核**汇合**到一路输出 | 目标像素的 1→32 分发、32→1 的图像汇合 |

### 2.3 类内核（class kernel）能持有「每实例状态」

`kernel::create(函数指针)` 造出的是「自由函数内核」，没有属于某个实例的成员变量。但反投影重建内核需要两样**属于单个实例**的东西：

- 一个唯一身份号 `m_id`（PL 要靠它把图像写回正确的 DDR 位置）；
- 一块跨多次调用持续累加的图像缓冲 `m_img`。

这就要求用类来写内核，并用 `kernel::create_object<类名>(构造参数)` 来实例化，让每个内核对象都有自己的成员。这是本讲第二个核心。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开，另外两个文件用于补充内核签名与全局计数。

| 文件 | 作用 |
| --- | --- |
| [design/aie/graph.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h) | 本讲主角。定义 `BackProjectionSubgraph` 与 `BackProjectionGraph`，包含包交换对象、`create_object` 实例化、端口命名与 `area_group` 布局约束。 |
| [design/aie/backprojection.cc](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc) | 内核实现。本讲只看 `px_demux_kern` 如何贴包头、`ImgReconstruct` 构造函数如何收 id、以及 dump 阶段如何把 `m_id` 写进输出包。 |
| [design/aie/custom_kernels.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h) | 内核声明。本讲看 `ImgReconstruct` 类的成员（`m_id`、`m_img`）与 `REGISTER_FUNCTION` 注册。 |
| [design/common.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h) | 提供 `IMG_SOLVERS_PER_SWITCH`（32）等规模宏及其「最多 32」的注释。 |

## 4. 核心概念与源码讲解

本讲三个最小模块互相独立又彼此配合：模块 4.1 解决「数据怎么分」，模块 4.2 解决「内核谁是谁」，模块 4.3 解决「内核摆在哪」。

### 4.1 pktsplit / pktmerge 包交换

#### 4.1.1 概念说明

回到 u4-l1 留下的悬念：每个 bpCluster 里有 1 个 Pixel Demux 内核和 32 个 Image Reconstruction 内核。目标像素必须**按核切分**——第 0 个重建核拿第 0 段像素，第 1 个拿第 1 段……各不相同。这是一对三十二的**分发**，不是「每份都一样」的广播，所以 slowtime/RC 那套 `connect + single_buffer` 广播在这里用不上。

ADF 给的解法是**包交换（packet switching）**：Demux 内核只有一个 `output_pktstream` 输出端口，它把发往第 `k` 个重建核的像素打包、贴上 `pkt_id = k` 的包头后写出；`pktsplit` 对象收到这条单一流，按包头里的 `pkt_id` 把包分发到它的第 `k` 个输出端口，再接到第 `k` 个重建核的 `input_pktstream` 输入。

反方向，32 个重建核各自算完一段图像、要把结果送出 bpCluster 时，也面临「32 路汇成 1 路」的问题（因为 bpCluster 对外只有一个 PLIO 出口）。`pktmerge` 做相反的事：把 32 个 `output_pktstream` 输入按包合并成一路输出，包里仍带着来源标签，最终经 PLIO 流向 PL 包路由器。

> 关键直觉：包交换 = 用「包头标签」在一条物理流上**复用**多路逻辑数据。`pktsplit` 是「1 进 N 出」的多路分解，`pktmerge` 是「N 进 1 出」的多路汇合。

#### 4.1.2 核心流程

一个 bpCluster 内部，像素与图像的包交换通路如下（默认 `IMG_SOLVERS_PER_SWITCH = 32`）：

```
                 ┌─────────── writeHeader(pkt_id=0..31) 贴标签 ───────────┐
GMIO(像素) ──► px_demux_kern ──(output_pktstream, 单一流)──► pktsplit<32>.in[0]
                                                                  │
                                            ┌─────────────────────┼─────────────────────┐
                                            ▼                     ▼                     ▼
                                    sp.out[0]              sp.out[1]  ……         sp.out[31]
                                            │                     │                     │
                                    img_rec_km[0].in[2]  img_rec_km[1].in[2]  …  img_rec_km[31].in[2]
                                        （32 个重建核各自处理自己那一段像素）
                                    img_rec_km[0].out[0] img_rec_km[1].out[0] … img_rec_km[31].out[0]
                                            │                     │                     │
                                            ▼                     ▼                     ▼
                                            └─────────────────────┴─────────────────────┘
                                                         pktmerge<32>.in[0..31]
                                                                  │
                                                          mg.out[0]（合并成一路）
                                                                  │
                                                          plio_pkt_rtr_out ──► PL 包路由器
```

要点：

1. **Demux 端（1→32）**：`px_demux_kern` 只有一个 `output_pktstream` 端口，却要服务 32 个目的核。它靠循环为每个目的核贴一个唯一 `pkt_id` 再写数据；`pktsplit<32>` 据此把包分流。
2. **合并端（32→1）**：32 个重建核的输出汇入 `pktmerge<32>`，合成一路后从唯一 PLIO 端口流出。每个包仍带来源核的标签（见模块 4.2 的 `m_id`）。
3. **32 路硬上限**：`pktsplit` / `pktmerge` 各自最多支持 32 个端口，这正是 `common.h` 里 `IMG_SOLVERS_PER_SWITCH` 注释所说「max number of image solvers on a switch is 32」的由来。要扩到 32 以上，就得增加 `AIE_SWITCHES`（即多挂几个 bpCluster），而不是加大单个 switch。

#### 4.1.3 源码精读

包交换对象是 `BackProjectionSubgraph` 的成员，模板参数就是每 switch 的重建核数：

[design/aie/graph.h:27-29](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L27-L29) 声明一对 `pktsplit` / `pktmerge`（32 路）。

构造函数里用 `::create()` 把它们实例化为 ADF 运行时对象：

[design/aie/graph.h:57-59](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L57-L59) 创建包分割器与合并器对象。

连线分三组，正好对应流程图的三段。第一组，Demux 的单一流喂进 `pktsplit` 的唯一输入：

[design/aie/graph.h:91-92](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L91-L92) `px_demux_km.out[0]` → `sp.in[0]`。

第二组，`pktsplit` 的 32 个输出分别接到 32 个重建核的 `in[2]`（即 `px_xyz_in` 这个 `input_pktstream` 端口）：

[design/aie/graph.h:88-89](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L88-L89) 循环连接 `sp.out[i]` → `img_rec_km[i].in[2]`。

第三组，32 个重建核的输出汇入 `pktmerge`，再合并成一路 PLIO：

[design/aie/graph.h:82](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L82) 与 [design/aie/graph.h:95-97](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L95-L97) 先把 `img_rec_km[i].out[0]` → `mg.in[i]`，再把 `mg.out[0]` → `plio_pkt_rtr_out.in[0]`。

那么包头里的 `pkt_id` 从哪来？看 Demux 内核实现。它先用 `getPacketid(port, idx)` 取回编译期为每个输出索引分配的包 id，再用 `writeHeader` 贴标签：

[design/aie/backprojection.cc:23-36](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L23-L36) 对每个 `switch_idx`（0..31）：取 `pkt_id` → `writeHeader` → 连续写 `px_components_per_ai` 个像素值，并在最后一个像素置 `tlast`（包结束标志）。

> 注意文件顶部这句注释 [design/aie/backprojection.cc:12](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L12) 「Supports up to 32 stream connections」——它和 `common.h` 的「max 32」共同印证了 `IMG_SOLVERS_PER_SWITCH` 的取值上限。

而 `common.h` 把这个上限写进了规模宏的注释里：

[design/common.h:33-35](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L33-L35) 注明「must be a power of 2 and the max number of image solvers on a switch is 32」，取值 32。

一个小结论：每个包携带的像素数 = 每核处理的像素数 × 3（X/Y/Z）。每核像素数由 u4-l1 的整除约束给出：

\[ \text{px\_per\_kern} = \frac{\text{PULSES} \times \text{RC\_SAMPLES}}{\text{IMG\_SOLVERS}} = \frac{602 \times 512}{224} = 1376 \]

于是 `px_components_per_ai = 1376 × 3 = 4128`，正是 [backprojection.cc:21](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L21) 的取值。

#### 4.1.4 代码实践

**实践目标**：把流程图里的「谁连到谁、搬的是什么」落到具体行号，确认包交换只用在「需要按核区分」的像素/图像通路上，而广播数据走的是另一套。

**操作步骤**：

1. 打开 `design/aie/graph.h`，在 [L88-L97](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L88-L97) 找到三组 `connect`，按本讲流程图标注每条线搬运的数据（像素 vs 图像）。
2. 对比顶层图里 slowtime/RC 的广播连法 [design/aie/graph.h:177-184](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L177-L184)，注意它们用的是 `connect(...).in[0]/in[1]`（buffer 端口）外加 `single_buffer(...)`，**没有**包交换对象。
3. 在 [backprojection.cc:13-14](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L13-L14) 与 [custom_kernels.h:26-30](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L26-L30) 确认：与包交换相连的端口类型是 `output_pktstream` / `input_pktstream`，而广播相连的是 `input_buffer` / `output_stream`。

**需要观察的现象**：

- 像素通路（`in[2]` / `out[0]`）一律是 `pktstream`；slowtime/RC 通路（`in[0]` / `in[1]`）一律是 `buffer`/`stream`。
- `pktsplit`/`pktmerge` 的模板参数与 `IMG_SOLVERS_PER_SWITCH` 完全一致（都是 32）。

**预期结果**：你会得出一张清晰的二分表——「广播数据（每核一份）走 buffer/stream + single_buffer；按核切分的数据（每核不同）走 pktstream + pktsplit/pktmerge」。这正是本设计区分两种数据搬运方式的判据。

**待本地验证**：若你有 Vitis 环境，把 `IMG_SOLVERS_PER_SWITCH` 改成 64 重编，应在 `pktsplit<64>` 处直接报错（超出 32 路上限），从而亲手验证「32」是硬约束。

#### 4.1.5 小练习与答案

**练习 1**：为什么目标像素用包交换分发，而 slowtime/RC 用 `single_buffer` 广播？

**参考答案**：slowtime/RC 对每个重建核都是**同一份完整数据**（每核都要用到全部距离样本和天线几何），属于「一发多收」的广播，ADF 的 `connect` + `single_buffer` 关掉双缓冲即可省存储地把同一份 buffer 共享给 224 个核。而目标像素是**按核切分**的——每核只处理 1376 个互不重叠的像素，必须按目的核分别寻址，这就需要包头标签 + `pktsplit` 来做一对三十二的分解。

**练习 2**：如果把 `AIE_SWITCHES` 从 7 改成 8（保持 `IMG_SOLVERS_PER_SWITCH=32`），包交换对象的数量与每对象端口数会怎么变？

**参考答案**：每对象端口数不变，仍是 `pktsplit<32>`/`pktmerge<32>`（受 32 上限约束，与 switch 数量无关）；对象**对数**从 7 对变成 8 对（每个 bpCluster 各有一对），重建核总数变成 8×32=256，此时需重新检查整除约束 `(PULSES×RC_SAMPLES)/256 = 308608/256 = 1204` 必须为整数（成立），但 `PULSES=602` 还需满足 u1-l4 提到的「PULSES 必须是 AIE_SWITCHES 的倍数」——602 不是 8 的倍数，故此改动会静默损坏图像。

---

### 4.2 用 create_object 实例化并传入唯一 id

#### 4.2.1 概念说明

224 个重建内核在 AIE 阵列里执行的是**同一段**代码 `img_reconstruct_kern`，但它们绝不是「无差别」的——每个内核负责 1376 个不同的像素，产出的图像段最终要拼回 DDR 里**不重叠**的位置。问题是：当 `pktmerge` 把 224 路输出按「先到先合并」汇成一路、且到达顺序无法保证（包合并是动态的）时，PL 包路由器怎么知道「当前这个包来自第几个内核，该写回 DDR 的哪一段」？

答案分两步：

1. **构造时**给每个重建内核打一个全局唯一编号 `m_id`；
2. **dump 时**内核把这个 `m_id` 写进输出包的元数据，PL 侧读出后按 `ddr_offset = m_id × 每核样本数` 计算偏移。

要让 224 个内核各自持有一个不同的 `m_id`（以及各自的累加缓冲 `m_img`），自由函数内核做不到——它没有「每实例」的成员。于是本设计把重建内核写成一个 **C++ 类 `ImgReconstruct`**，并用 ADF 的 `kernel::create_object<ImgReconstruct>(id)` 来实例化：模板参数指定类，函数实参 `id` 被转发给类的构造函数。

> 关键直觉：`kernel::create` 造「无状态自由函数内核」；`kernel::create_object<T>(args...)` 造「有状态类内核」，每个实例有独立成员、能接收构造参数。凡是内核需要「记住自己是老几」或「跨调用累加」的场合，就用类内核。

#### 4.2.2 核心流程

唯一 id 的生成与流转链路如下：

```
graph.h 构造函数：
  for i in 0..31:
      id = IMG_SOLVERS_PER_SWITCH * bp_subgraph_insts + i   ← 全局唯一（见 4.2.3 公式）
      img_rec_km[i] = kernel::create_object<ImgReconstruct>(id)
                                            │  转发构造参数
                                            ▼
      ImgReconstruct::ImgReconstruct(int id) : m_id(id) {}  ← 存进成员 m_id
                                            │
                                            │  ……运行期跨 PULSES 次调用累加 m_img……
                                            ▼
      dump 阶段（rtp_dump_img_in==1）：
          writeHeader(img_out, 0, getPacketid(img_out, 0))
          writeincr(img_out, m_id, false)        ← 把 m_id 写进包头后第一个 32 位字
          …写出图像样本…
                                            │
                                            ▼
      PL 包路由器读出 m_id → ddr_offset = m_id × SAMPLES_PER_KERN → 写回正确 DDR 段
```

注意 `bp_subgraph_insts` 在这里的双重身份：它既是端口命名的「子图序号」（见模块 4.3 与综合实践），又是 id 生成的「簇基址」。同一个值，两处用法。

#### 4.2.3 源码精读

先看类的长相。`ImgReconstruct` 持有两个私有成员——身份号 `m_id` 与累加缓冲 `m_img`，并显式声明构造函数接收 id：

[design/aie/custom_kernels.h:22-42](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L22-L42) 类定义。其中 [L25](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L25) `ImgReconstruct(int id);` 声明构造函数，[L38](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L38) `uint32 m_id;` 存身份号，[L41](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L41) `cfloat m_img[(PULSES*RC_SAMPLES)/IMG_SOLVERS];` 是跨调用累加的图像缓冲。

类内核还必须告诉 ADF 框架「哪个方法是入口」，这是固定的注册样板：

[design/aie/custom_kernels.h:32-35](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L32-L35) `REGISTER_FUNCTION(ImgReconstruct::img_reconstruct_kern);`。没有它，框架不知道该调用哪个成员函数当内核。

构造函数的实现只是把 id 存起来：

[design/aie/backprojection.cc:58-60](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L58-L60) `ImgReconstruct::ImgReconstruct(int id) : m_id(id) {}`。

现在看本模块最关键的一行——图构造函数里如何用 `create_object` 造内核并传 id：

[design/aie/graph.h:52-53](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L52-L53) 循环里 `img_rec_km[i] = kernel::create_object<ImgReconstruct>(IMG_SOLVERS_PER_SWITCH*bp_subgraph_insts + i);`。把它与同一文件里「自由函数内核」的造法对照——

[design/aie/graph.h:49](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L49) `px_demux_km = kernel::create(px_demux_kern);`（自由函数，无构造参数、无成员状态）。

id 的全局唯一性来自这条公式（`bp_subgraph_insts` 取该子图被构造时的值 0..6）：

\[ \text{id} = 32 \times \text{bp\_subgraph\_insts} + i, \quad i \in [0,31] \]

所以 7 个簇各贡献一段连续 id：

| 簇序号 `bp_subgraph_insts` | 该簇重建核 id 范围 |
| --- | --- |
| 0 | 0 – 31 |
| 1 | 32 – 63 |
| 2 | 64 – 95 |
| 3 | 96 – 127 |
| 4 | 128 – 159 |
| 5 | 160 – 191 |
| 6 | 192 – 223 |

合计 0 – 223，共 224 个全局唯一 id，与 `IMG_SOLVERS = AIE_SWITCHES × IMG_SOLVERS_PER_SWITCH = 7 × 32` 一致（[common.h:38](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L38)）。

最后，id 在 dump 阶段被写进输出包。这段代码只在最后一脉冲（`rtp_dump_img_in == 1`）执行，把累加好的 `m_img` 打包送出，并在包头之后紧跟着写入 `m_id` 作为「来源核」元数据：

[design/aie/backprojection.cc:197-200](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L197-L200) 先 `writeHeader(img_out, 0, getPacketid(img_out, 0))` 贴包头，再 `writeincr(img_out, m_id, false)` 写入来源核 id，其后还补了两个 32 位 `0` 凑 128 位总线宽度（[L203-L204](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L203-L204)）。

> 这条 id 链路的终点在 PL 包路由器：它解析包头、读出 `m_id`，按 `ddr_offset = m_id × SAMPLES_PER_KERN` 把这段图像写进连续 DDR。该内核的细节留待 u6-l1 展开，这里只需记住「AIE 侧的 `m_id` 是 PL 侧正确归位的钥匙」。

#### 4.2.4 代码实践

**实践目标**：亲手算出全部 224 个重建核的 id，并追踪 id 从构造参数一路到输出包元数据的完整链路。

**操作步骤**：

1. 在 [graph.h:53](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L53) 确认 id 表达式 `IMG_SOLVERS_PER_SWITCH*bp_subgraph_insts + i`，代入 `IMG_SOLVERS_PER_SWITCH=32`。
2. 按 `bp_subgraph_insts` 从 0 到 6、`i` 从 0 到 31，列出每个簇的 id 区间（应如上表）。
3. 顺着 [custom_kernels.h:25](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L25) → [backprojection.cc:58-60](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L58-L60) → [backprojection.cc:200](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L200) 追一遍，确认 id 被原样存进 `m_id` 再被原样写出。
4. 思考：若两个内核拿到相同 id，PL 侧会发生什么？

**需要观察的现象**：id 在 AIE 侧不参与任何计算，**只**作为元数据随包流出；它是「身份」，不是「数据」。

**预期结果**：224 个内核 id 互不重复；若两个内核 id 相同，PL 包路由器会把两段不同的图像写到 DDR 同一位置，后写覆盖先写，最终图像出现块状错乱/丢失。

**待本地验证**：把 [graph.h:53](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L53) 改成只传 `i`（去掉簇偏移），跑 `aiesim` 后检查 `output_img.csv`，应能看到图像某些区域被覆盖成另一个区域的值——这验证了 id 必须跨簇全局唯一。

#### 4.2.5 小练习与答案

**练习 1**：本设计里 `px_demux_kern` 和 `data_broadcast_kern` 用 `kernel::create`，唯独 `img_rec_km` 用 `kernel::create_object`。为什么后两者「不需要」类形式？

**参考答案**：`px_demux_kern` 与 `data_broadcast_kern` 都是无状态转发：前者只把像素重新打包分发，后者只把输入搬到输出，二者都不需要在多次调用之间记住任何「属于自己的」东西。而重建内核既要持有一个跨 `PULSES` 次调用累加的 `m_img`，又要持有一个区分自我的 `m_id`——这两种「每实例状态」只有类内核能给。

**练习 2**：`create_object<ImgReconstruct>(id)` 里的 `id` 为什么不能用 RTP（运行时参数）在运行期传入，而非要在构造期就定死？

**参考答案**：因为 `id` 决定了内核输出**在 DDR 中的最终落点**（经 PL 侧 `ddr_offset = m_id × SAMPLES_PER_KERN`），它必须在编译/链接期就确定，好让 system.cfg、PL 内核的地址映射、主机侧 img buffer 的切分三方一致。RTP 是运行期可变的值，若 id 可变，PL 与主机就无法预先约定图像布局。此外 `m_img` 的大小 `(PULSES*RC_SAMPLES)/IMG_SOLVERS` 也是编译期常量，与「类成员在构造时确定」的语义一致。

---

### 4.3 location area_group：把子图钉在固定 tile 区域

#### 4.3.1 概念说明

到上一步为止，我们已经造好了 232 个内核对象与一堆端口、包交换对象。**ADF 编译器本可以自动把它们铺到 AIE 阵列的任意 tile 上**——那为什么本设计还要手动写布局约束？

三个原因：

1. **PLIO 出口必须落在特定列**。每个 bpCluster 对外只有一个 PLIO 输出端口（`plio_pkt_rtr_out`），PLIO 必须经 **shim tile**（AIE 阵列边缘与 NoC/PL 接口的那一行）出阵列。如果让编译器乱放，PLIO 的走线可能绕远甚至无解。把整个子图约束到固定列范围、并显式保留对应 shim tile，才能保证 PLIO 有干净的出口。
2. **簇间隔离，避免布线拥塞**。7 个 bpCluster 各自密集、内部连线很多（一对 32 路包交换 + 广播扇出）。若让多个簇的内核交错铺放，簇内长线会暴涨。把每个簇钉在一块**独占的矩形区域**里，簇内连线就短，簇间只剩「顶层广播」和「对外 PLIO」这几根长线。
3. **确定性**。固定布局让「几号簇在几号列」可预测，与端口命名（`..._graph_sub_...`）、id 编号在逻辑上自洽，便于排错。

ADF 提供的约束工具是 `location<graph>(*this) = area_group({...})`：把它写在子图构造函数里，就把**整个子图**（它含的所有内核与包交换对象）限定到一组 tile 区域内。

> 关键直觉：`area_group` 给编译器画「地盘」。地盘之内编译器可自由摆，地盘之外的 tile 这个子图不能用。多个子图的地盘互不重叠，就实现了簇间隔离。

#### 4.3.2 核心流程

布局约束的核心是「按子图序号 `bp_subgraph_insts` 在列轴上等距铺开」：

```
对每个 bpCluster（bp_subgraph_insts = 0,1,2,3,4,5,6）：
    col_start = 0 + bp_subgraph_insts × 7      → 0, 7, 14, 21, 28, 35, 42
    col_end   = col_start + 6                  → 6,13, 20, 27, 34, 41, 48
    aie_tile  区域：列 [col_start, col_end] × 行 [0, 7]
    shim_tile 区域：列 [col_start, col_end] × 行 0   ← 给 PLIO 留出口
```

7 个簇占据列 0–48（共 49 列），互不重叠、紧凑相邻：

```
列号:  0...6 | 7...13 | 14...20 | 21...27 | 28...34 | 35...41 | 42...48 | 49
簇:    [ bpCluster 0 ][ bpCluster 1 ][ bpCluster 2 ][ bpCluster 3 ][ bpCluster 4 ][ bpCluster 5 ][ bpCluster 6 ] (余 1 列)
每簇: 7 列宽 × 8 行高（第 0..7 行），且保留同列的 shim_tile(第 0 行) 供 PLIO 出口
```

为什么正好是「7」？因为每个簇要塞下 1（demux）+ 32（重建）= 33 个计算内核（`runtime<ratio> = 1.0` 意味着每个内核独占一个 tile），外加 `pktsplit` / `pktmerge` 占用的交换资源。7 列 × 8 行 = 56 个 aie tile 足够容纳这 ~35 个对象并留出布线余量；列方向步长 7 则保证相邻簇地盘首尾相接、不重叠不浪费。

> 备注：VCK190（VC1902 die）的 AIE 阵列为 50 列 × 8 行 = 400 个 AIE tile。7 个簇占 49 列，恰好留 1 列余量，整体放得下。源码注释 [graph.h:101](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L101) 提到「On VC1902 only columns 6-44 are PLIO-capable」，提示 PLIO 列存在工艺约束；本设计实际用 `base_col = 0` 起步，该注释与代码的精确对应关系可结合你手头的器件手册核对。

#### 4.3.3 源码精读

布局代码位于子图构造函数末尾、`source` 与 `runtime<ratio>` 之前：

[design/aie/graph.h:99-111](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L99-L111) 完整的位置约束块。

逐行拆解：

- [L102](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L102) `int base_col = 0;` 起始列。
- [L103](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L103) `int col_start = base_col + bp_subgraph_insts * 7;` ——本讲的核心一行。`bp_subgraph_insts` 是「我是第几个簇」，每簇让出 7 列，于是第 `n` 个簇从第 `7n` 列起步。
- [L104](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L104) `int col_end = col_start + 6;` 闭区间终点，跨 7 列。
- [L105-L106](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L105-L106) 行范围 `row_start=0, row_end=7`，覆盖整列高度。

约束本身由两条 `area_group` 条目组成：

[design/aie/graph.h:108-111](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L108-L111) 把 `*this`（整个子图）限定为：一条 `aie_tile` 矩形（计算区）+ 一条 `shim_tile` 矩形（同列第 0 行，注释写明「Needed for PLIOs」）。

`shim_tile` 那条为何不可少？因为 PLIO 物理上必须经 shim tile 出 AIE 阵列进入 PL/NoC。保留同列 shim tile，就是把本簇 PLIO 的「出口」一并圈进地盘，编译器无须再去别处找出口。

> 旁注：`location<graph>(*this)` 作用在「子图」这一层，所以约束会自动覆盖子图内全部 `img_rec_km`、`px_demux_km`、`sp`、`mg` 以及端口对象——无需逐个内核写 `location<kernel>`。这是把约束写在子图构造函数里的好处。

#### 4.3.4 代码实践

**实践目标**：验证 7 个簇的列地盘在 50 列阵列里不重叠、不留洞，并理解「步长 7」与「簇内 33 个内核」的关系。

**操作步骤**：

1. 读 [graph.h:103](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L103)，代入 `bp_subgraph_insts = 0..6`，算出 7 个簇各自的 `[col_start, col_end]`。
2. 在纸上画一条 0–49 的列轴，把 7 个 7 列宽的方块填进去，确认首尾相接（0–6, 7–13, …, 42–48）且只余第 49 列。
3. 把 `AIE_SWITCHES` 假想改成 8（即 8 个簇），重算列占用：0–55，超出 50 列阵列——说明在本器件上 `AIE_SWITCHES` 不能任意加大，布局约束是规模上限的隐性栅栏之一。

**需要观察的现象**：每个簇的列宽恒为 7（`col_end - col_start + 1 = 7`），与簇序号无关；行范围对所有簇都是 0–7。

**预期结果**：7 簇合占 49 列，正好放进 VCK190 的 50 列 AIE 阵列；若簇数加到 8 则列溢出。

**待本地验证**：若你能在 Vitis 中生成布局报告（如 `aiesimulator --dump-vcd` 或编译器布局输出），对照 `bp_subgraph_insts` 序号核对该簇内核实际落在 `[7n, 7n+6]` 列内。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `col_start` 的步长是 7，而不是 6 或 8？

**参考答案**：步长必须等于每簇的列宽，才能让相邻簇「首尾相接不重叠」。代码里 `col_end = col_start + 6` 是闭区间，列宽为 7（0,1,…,6 共 7 列），所以下一个簇必须从 `col_start + 7` 起步。步长 6 会让相邻簇列范围重叠一列（冲突）；步长 8 会在簇间留 1 列空洞（浪费）。7 这个具体值则来自「每个 1.0 占空比的内核独占一个 tile，33 个内核 + 包交换资源需要约 7 列 × 8 行的容量」。

**练习 2**：删掉 `shim_tile` 那条 `area_group` 条目，PLIO 还能正常工作吗？

**参考答案**：大概率会出问题或迫使编译器把 PLIO 出口挪到别处的 shim tile，导致额外长走线甚至无法满足时序。PLIO 经 shim tile 出阵列是硬性物理通路，`shim_tile` 条目的作用就是「把本簇列范围内的 shim tile 一并圈下，给 PLIO 留出口」。源码注释 `// Needed for PLIOs` 已点明这一点。

---

## 5. 综合实践

本讲的指定实践任务，把三个模块串起来：追踪两个全局计数器 `bp_subgraph_insts` 与 `bp_graph_insts` 如何为端口和内核生成唯一命名，并解释 `col_start` 的步长。

### 实践目标

理解「C++ 成员构造顺序」如何被巧妙地用作全局编号生成器，从而让端口名、内核 id、tile 地盘三方共享同一个 `bp_subgraph_insts` 序号。

### 背景知识：两个计数器的声明与自增

两个计数器一外一内，都在 `graph.h` 顶部声明，其中一个在 `graph.cpp` 里定义并初始化：

[design/aie/graph.h:12-13](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L12-L13) 声明 `extern uint8_t bp_graph_insts;`（顶层图计数器）与 `uint8_t bp_subgraph_insts;`（子图计数器）。

[design/aie/graph.cpp:7](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L7) 定义 `uint8_t bp_graph_insts = 0;`。

它们各自在**所属构造函数的最末尾**自增，意思是「我先把自己造好（用当前序号命名一切），再把序号让给下一个兄弟」：

[design/aie/graph.h:126](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L126) 子图构造函数末尾 `bp_subgraph_insts++;`。

[design/aie/graph.h:203](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L203) 顶层图构造函数末尾 `bp_graph_insts++;`。

### 操作步骤

**第 1 步：看清成员声明顺序如何决定构造顺序。**

在 `BackProjectionGraph` 里，子图数组先于计数器自增被声明：

[design/aie/graph.h:144](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L144) `BackProjectionSubgraph bpCluster[AIE_SWITCHES];`。

C++ 规则：成员按声明顺序构造。所以构造一个 `BackProjectionGraph` 时，会先把 `bpCluster[0]`、`bpCluster[1]`、…、`bpCluster[6]` 依次构造（每个子图构造函数都会在末尾 `bp_subgraph_insts++`），**然后**才执行顶层图构造函数体（在 [L203](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L203) `bp_graph_insts++`）。

**第 2 步：追踪子图里如何用 `bp_graph_insts` 与 `bp_subgraph_insts` 拼出唯一端口名。**

子图的 GMIO 像素端口名带两个序号：

[design/aie/graph.h:65-66](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L65-L66) `gmio_in_xyz_px_<graph>_<sub>`。

子图的 PLIO 端口名与仿真数据文件名同样带这两个序号：

[design/aie/graph.h:71-73](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L71-L73) `plio_pkt_rtr_out_<graph>_<sub>` 与 `aie_to_plio_switch_<graph>_<sub>.csv`。

顶层的 GMIO 端口只带顶层序号：

[design/aie/graph.h:165-166](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L165-L166) `gmio_in_st_<graph>`、`gmio_in_rc_<graph>`（slowtime/RC 在顶层广播，只需顶层唯一）。

**第 3 步：追踪内核 id 如何用到 `bp_subgraph_insts`。**

回到 [graph.h:53](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L53)，id = `32 × bp_subgraph_insts + i`，用同一个 `bp_subgraph_insts` 作为「簇基址」。

**第 4 步：追踪 `col_start` 如何用到 `bp_subgraph_insts`。**

回到 [graph.h:103](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L103)，`col_start = bp_subgraph_insts × 7`，又用同一个序号作「列基址」。

### 需要观察的现象

`bp_subgraph_insts` 这**一个**值，在单个子图构造期间被同时用于四处：端口名（GMIO/PLIO）、仿真数据文件名、内核 id 基址、tile 列基址。也就是说，「第 `n` 个簇」的端口叫 `..._0_n`、id 是 `32n..32n+31`、地盘在第 `7n..7n+6` 列——四处编号严格同步。

### 预期结果

以默认 `INSTANCES = 1`（[graph.cpp:9-10](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L9-L10)）为例，`bp_graph_insts` 全程为 0，你应该能填出下表：

| 簇序号 `bp_subgraph_insts` | GMIO 像素端口名 | PLIO 输出端口名 | 仿真数据文件 | 内核 id 区间 | tile 列范围 |
| --- | --- | --- | --- | --- | --- |
| 0 | `gmio_in_xyz_px_0_0` | `plio_pkt_rtr_out_0_0` | `aie_to_plio_switch_0_0.csv` | 0–31 | 0–6 |
| 1 | `gmio_in_xyz_px_0_1` | `plio_pkt_rtr_out_0_1` | `aie_to_plio_switch_0_1.csv` | 32–63 | 7–13 |
| … | … | … | … | … | … |
| 6 | `gmio_in_xyz_px_0_6` | `plio_pkt_rtr_out_0_6` | `aie_to_plio_switch_0_6.csv` | 192–223 | 42–48 |

### 解释：为何 `col_start` 随子图实例递增 7

`col_start = bp_subgraph_insts × 7`，递增 7 的根本原因是「每簇地盘列宽 = 7」：

- 每簇要容纳 1 个 demux + 32 个重建（共 33 个 `runtime<ratio>=1.0` 的内核）加上 `pktsplit<32>`/`pktmerge<32>` 的交换资源，需要约 7 列 × 8 行的 tile 容量。
- 代码用闭区间 `col_end = col_start + 6` 定义列宽为 7；要让相邻簇地盘不重叠、不留洞，步长就必须等于列宽，故为 7。
- 结果是 7 个簇恰好铺满 0–48 列，与 VCK190 的 50 列阵列相容。

### 进阶思考（选做）

如果将来 `INSTANCES > 1`（即实例化多个顶层 `BackProjectionGraph`），第二个顶层图在构造时 `bp_graph_insts` 已经是 1，于是它下面 7 个簇的端口名都会变成 `..._1_n`、id 仍从 `32n` 起算——注意此时 **id 会与第一个顶层图的 id 重复**（因为 id 公式只用 `bp_subgraph_insts`，没用 `bp_graph_insts`）。这是当前代码在多顶层图场景下的一个潜在隐患，值得在阅读时留意。

## 6. 本讲小结

- **包交换**：`pktsplit<32>` / `pktmerge<32>` 用包头 `pkt_id` 在一条物理流上复用 32 路逻辑数据，解决「1 个 demux 端口 → 32 个重建核」的分解与「32 路输出 → 1 个 PLIO」的汇合；`pktsplit`/`pktmerge` 各最多 32 路，这是 `IMG_SOLVERS_PER_SWITCH` 上限 32 的由来。
- **类内核与 id**：重建内核用 `kernel::create_object<ImgReconstruct>(id)` 实例化，构造参数 `id = 32×bp_subgraph_insts + i` 给每个内核一个全局唯一 `m_id`；该 id 不参与计算，只在 dump 时写进输出包，供 PL 包路由器按 `ddr_offset = m_id × 每核样本数` 把图像写回正确 DDR 段。
- **状态成员**：`ImgReconstruct` 同时持有跨调用累加的 `m_img`，这正是「必须用类内核、而非自由函数内核」的根本理由；`REGISTER_FUNCTION` 负责向框架注册入口方法。
- **布局约束**：`location<graph>(*this) = area_group({...})` 把每个子图钉在 7 列 × 8 行的矩形 aie_tile 区域，外加同列 shim_tile 给 PLIO 留出口；`col_start = bp_subgraph_insts × 7` 让 7 个簇在列轴上首尾相接、互不重叠。
- **计数器三用**：`bp_subgraph_insts` 在单个子图构造期间同时服务于端口命名、内核 id 基址、tile 列基址三处，靠的是「C++ 成员按声明顺序构造」这一特性；`bp_graph_insts` 则为顶层图实例编号。
- **隐性规模栅栏**：32 路包交换上限与 50 列阵列上限，共同把 `IMG_SOLVERS_PER_SWITCH` 与 `AIE_SWITCHES` 的可调范围框死，任何调参都须同时满足 u1-l4 的整除约束与本讲的两个物理约束。

## 7. 下一步学习建议

本讲把「图里有什么对象、它们怎么连、摆在哪」彻底讲清了，但始终**没有**进入内核内部。接下来的学习路线：

1. **u4-l3（GMIO/PLIO 端口与广播布线）**：本模块（4.3）提到了端口命名与 PLIO 出口，但还没有完整画出「顶层 GMIO（slowtime/rc）→ 广播扇出到 224 个核」与「子图 GMIO（像素）→ demux → 包交换」的全套布线。u4-l3 会把端口位宽、数据文件绑定、`single_buffer` 与包交换的分工一次讲透，是本讲的自然续篇。
2. **u5-l1（Data Broadcast 内核）与 u5-l2（Pixel Demux 内核）**：本讲只看到了 `px_demux_kern` 的包头贴标签部分，u5-l2 会完整拆解它如何按 `px_components_per_ai` 切分像素并在末尾置 `tlast`；u5-l1 则讲对侧的广播内核。
3. **u6-l1（PL 包路由器 HLS 内核）**：本讲多次提到「`m_id` 决定 PL 侧 DDR 偏移」，那里的 `ddr_offset = instance_id × SAMPLES_PER_KERN` 与包头解析将在 u6-l1 给出完整源码，闭环整条 id 链路。
4. **动手建议**：在进入 u4-l3 前，先用本讲「综合实践」的表格在本地 `aiesim` 产物里核对端口名与 CSV 文件名，确认你真的掌握了计数器命名机制，再继续往下读会更踏实。
