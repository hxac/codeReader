# Pixel Demux 内核与包头部

## 1. 本讲目标

上一讲（u5-l1）我们看了「数据广播内核」`data_broadcast_kern`：它把**每个内核都相同**的 slowtime 与 RC 数据复制给全部 224 个重建内核。本讲转向另一类数据——**目标像素**（每个像素的 X/Y/Z 坐标）。目标像素的特点正好相反：**每个重建内核需要的是不同的子集**，不能再广播。

负责把目标像素「按内核切分下发」的，就是本讲的主角 **`px_demux_kern`（像素解复用内核）**。学完本讲你应当：

1. 弄清为什么目标像素必须用**包交换（packet switching）**分发，而不能像 slowtime/RC 那样广播。
2. 掌握 `output_pktstream` 包流、`getPacketid`、`writeHeader` 三个原语：一条物理流如何承载 32 个逻辑包、又如何靠 `pkt_id` 路由到 32 个重建内核。
3. 会计算 `px_components_per_ai`（每个包携带多少个 float、折合多少个像素），并能解释 `tlast`（TLAST）标志在包边界上的关键作用。

## 2. 前置知识

本讲建立在 u4-l2 与 u5-l1 之上，承接而不再重复：

- **广播 vs. 切分（来自 u5-l1 / u4-l2）**：每份数据若**所有内核都一样**（slowtime、RC），用 `connect` 扇出 + `single_buffer` 广播即可；若**每个内核要不同的数据**（目标像素），就必须走包交换，靠包头里的 `pkt_id` 把数据路由到指定内核。
- **pktsplit / pktmerge（来自 u4-l2）**：`pktsplit<N>` 把一条物理流按 `pkt_id` 分发到 N 个输出；`pktmerge<N>` 把 N 路输入按 `pkt_id` 汇聚成一条流。N 上限是 32，这正是 `IMG_SOLVERS_PER_SWITCH` 上限 32 的由来。
- **AXI4-Stream 与「包」**：流（stream）是连续的「beat」（一次传输）。一个**包（packet）**= 一个 32 位**包头（header）** + 若干数据 beat，由最后一个 beat 上的 **TLAST** 信号标志结束。
- **重建内核如何读像素**：`img_reconstruct_kern` 的第三个输入 `input_pktstream *px_xyz_in` 就是本讲 demux 写出的包流；它会先读掉包头，再读出自己那份像素（详见 4.3 节的契约对照）。

> 关键反差（本讲核心动机）：slowtime/RC 是「**一份数据，全员共享**」→ 广播；目标像素是「**全员数据，各取一份**」→ 解复用。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [design/aie/backprojection.cc](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc) | AIE 内核实现 | `px_demux_kern` 定义（L13–L38），是本讲精读主体 |
| [design/aie/custom_kernels.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h) | 内核声明 | `px_demux_kern` 签名（L14–L15） |
| [design/aie/graph.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h) | ADF 图拓扑与接线 | demux 的接线：GMIO→demux→pktsplit→32 个重建内核（L49、L79、L88–L92） |
| [design/pl/dma_pkt_router.cpp](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp) | PL 侧包路由器 | 全仓库**唯一**写明包头位域表（UG1079）的地方（L36–L53），用于核对包头格式 |
| [design/common.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h) | 三域共享配置 | 本讲算术用到的 `PULSES`、`RC_SAMPLES`、`IMG_SOLVERS` 等宏 |

## 4. 核心概念与源码讲解

### 4.1 像素解复用：为什么需要、它在图里处于什么位置

#### 4.1.1 概念说明

反投影要把一整张 \( \text{PULSES} \times \text{RC\_SAMPLES} \)（默认 \(602 \times 512\)，共 308224 个像素）的目标网格**切成 224 份**，每个重建内核各管其中 1376 个像素。这意味着：

- slowtime / RC：224 个内核拿**同一份**数据 → 广播一次即可（u5-l1）。
- 目标像素：224 个内核拿**各自不同**的 1376 个像素 → 没法广播，必须**按内核寻址下发**。

`px_demux_kern` 就是这个「按内核寻址下发」的枢纽。它本质上是一个**串行→分包器**：从一个连续的 float 输入流里，依次取出一批批像素，**每批打上一个指向某个内核的包头**，写成一条包流（`output_pktstream`）。下游的 `pktsplit<32>` 读到包头里的 `pkt_id`，就把这个包送到对应的重建内核。

#### 4.1.2 核心流程

分发是**两级**结构（与 u4-l1/u4-l3 的拓扑对应）：

```
主机 DDR
  │  目标像素（PULSES×RC_SAMPLES×3 个 float）
  │  按 switch 切成 7 组，每组经一个 GMIO 投递
  ▼
┌─────────────── 7 个 switch（AIE_SWITCHES）───────────────┐
│  switch #j：                                              │
│    gmio_in_xyz_px ──► px_demux_kern.in[0]   （普通流）    │
│                         │                                  │
│                         ▼  out[0]（1 条包流）              │
│                       pktsplit<32>.in[0]                  │
│                         │  按 pkt_id 分发                  │
│            ┌────────────┼────────────┐                    │
│            ▼            ▼            ▼                    │
│       img_rec_km[0] img_rec_km[1] … img_rec_km[31]        │
│       （每个内核收到的就是 demux 为它打好的那个包）          │
└──────────────────────────────────────────────────────────┘
```

也就是说：**顶层**主机把像素均分给 7 个 switch（走 7 个 `gmio_in_xyz_px` 端口）；**每个 switch 内部**，demux 再把自己那份均分给 32 个重建内核（走包流 + pktsplit）。本讲的 `px_demux_kern` 负责的就是第二级「1 → 32」。

#### 4.1.3 源码精读

demux 在子图里被创建并接线（[design/aie/graph.h:49](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L49)）：

```cpp
px_demux_km = kernel::create(px_demux_kern);
```

它的输入是一个**普通流**，由本 switch 自己的 GMIO 端口喂入（[design/aie/graph.h:79](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L79)）——每个 switch 各有一个独立的 `gmio_in_xyz_px`：

```cpp
connect(gmio_in_xyz_px.out[0], px_demux_km.in[0]);
```

demux 只有**一个** `output_pktstream` 输出（注意：`output_pktstream` 是单数端口），它直接喂给 `pktsplit<32>` 的唯一输入（[design/aie/graph.h:92](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L92)）：

```cpp
connect(px_demux_km.out[0], sp.in[0]);
```

`pktsplit<32>` 再把 32 个输出分别接到 32 个重建内核的**第三个输入** `in[2]`（[design/aie/graph.h:88-L89](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L88-L89)）：

```cpp
for (int i=0; i<IMG_SOLVERS_PER_SWITCH; i++)
    connect(sp.out[i], img_rec_km[i].in[2]);
```

> `pktsplit<IMG_SOLVERS_PER_SWITCH>` 与 `pktmerge<IMG_SOLVERS_PER_SWITCH>` 在子图中声明（[design/aie/graph.h:28-L29](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L28-L29)），其模板参数就是 32。

内核本身的签名极简——一入一出（[design/aie/backprojection.cc:13-L14](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L13-L14)）：输入是普通 `input_stream<float>`，输出是 `output_pktstream`。

```cpp
void px_demux_kern(input_stream<float>* __restrict px_xyz_in,
                   output_pktstream *px_xyz_out) {
```

这正是「串行→分包器」的体现：进来的不过是一条平平无奇的 float 流，出去的却是带路由标签的包流。

#### 4.1.4 代码实践

**实践目标**：在静态拓扑层面确认「1 个 demux 输出 → 32 个重建内核」这条通路，并数清 demux 一次调用写出多少个包。

**操作步骤**：
1. 打开 [design/aie/graph.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h)，定位 L79、L92、L88–L89 三处 `connect`。
2. 画出从 `gmio_in_xyz_px.out[0]` 到某个 `img_rec_km[i].in[2]` 的完整链路，标出每一段的端口类型（普通流 / 包流）。
3. 数一下：demux 的输出端口有几个？pktsplit 的输出端口有几个？二者如何对应？

**需要观察的现象**：demux 只有**1 个** `output_pktstream`，却要服务 32 个内核；这「1→32」的放大完全由 `pktsplit<32>` 在硬件里完成，demux 只负责**在一条流上依次塞进 32 个带不同 `pkt_id` 的包**。

**预期结果**：链路为 `gmio_in_xyz_px.out[0] → px_demux_km.in[0]`（普通流）→ `px_demux_km.out[0] → sp.in[0]`（包流）→ `sp.out[i] → img_rec_km[i].in[2]`（包流，32 路）。demux 单次调用写出 **32 个包**（每脉冲、每 switch）。

#### 4.1.5 小练习与答案

**练习 1**：既然 demux 的输出只有一条物理流，为什么不会把 32 个内核的数据「搅在一起」分不清？

**答案**：因为每个逻辑包都自带一个**包头**，包头里的 `pkt_id` 标明了它属于哪个内核；下游 `pktsplit<32>` 按包头里的 `pkt_id` 把包分发到对应输出，物理上是一条流、逻辑上是 32 路互不干扰的数据。

**练习 2**：顶层有 7 个 switch，每个 switch 一个 demux。为什么不在顶层用**一个** demux 直接分发给 224 个内核？

**答案**：`pktsplit` / `pktmerge` 的路数上限是 32（见 4.2 节，`pkt_id` 只有 5 位）。要在一片 AIE 里一次性分发给 224 个内核，单级包交换做不到；因此采用「主机分 7 组（7 个 GMIO）+ 每 switch 内部 demux 再分 32」的两级结构。

---

### 4.2 包头构造与 pkt_id 路由：getPacketid / writeHeader

#### 4.2.1 概念说明

要理解 demux 的核心循环，先得弄清三个原语：

- **`output_pktstream`（包流）**：一种特殊的 AIE 流。与普通流不同，它传输的是**带包头的包**，包头里的 `pkt_id` 让流交换网络（stream switch）能把包路由到指定目的地。本设计中，包流配 `pktsplit`/`pktmerge` 使用。
- **`getPacketid(port, index)`**：返回「要路由到第 `index` 个目的端口」所应使用的 `pkt_id`。关键在于：`pkt_id` 是 ADF 编译器在编译期**自动分配**的，取值未必连续、也未必等于 `index` 本身，因此**不能硬编码**，只能用 `getPacketid` 间接获取。源码注释说得很明白（[design/aie/backprojection.cc:24-L26](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L24-L26)）：

  > Packet ID is automatically given at compile time and must be fetched indirectly via an index.

- **`writeHeader(port, pkt_type, pkt_id)`**：在包流上写出一个 32 位**包头**，开启一个新包。`pkt_type` 是 3 位包类型标记，`pkt_id` 是 5 位路由标签。

#### 4.2.2 核心流程

demux 的主循环（伪代码）：

```
pkt_type = 0
对 switch_idx 从 0 到 IMG_SOLVERS_PER_SWITCH-1（即 0..31）：
    pkt_id = getPacketid(px_xyz_out, switch_idx)   # 取路由到第 switch_idx 个内核的 pkt_id
    writeHeader(px_xyz_out, pkt_type, pkt_id)       # 开启一个新包，包头带上 pkt_id
    对 px_idx 从 0 到 px_components_per_ai-1：
        val = readincr(px_xyz_in)                   # 从输入流取一个 float
        writeincr(px_xyz_out, val, px_idx==末尾)     # 写入包体，末 beat 置 tlast
```

也就是说，demux 顺序地「为第 0 个内核打包头→写它的像素→为第 1 个内核打包头→写它的像素→……」。每打一个包头，`pktsplit` 就知道接下来的数据要送到 `pkt_id` 对应的那个内核去。

#### 4.2.3 源码精读

核心三行在 [design/aie/backprojection.cc:23-L27](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L23-L27)：

```cpp
for (int switch_idx=0; switch_idx<IMG_SOLVERS_PER_SWITCH; switch_idx++) {
    uint32 pkt_id = getPacketid(px_xyz_out, switch_idx);
    writeHeader(px_xyz_out, pkt_type, pkt_id);
```

`pkt_type` 在循环外固定为 0（[design/aie/backprojection.cc:18](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L18)），本设计所有像素包都用同一类型；真正用来区分目的地的是 `pkt_id`。

**包头位域**：本仓库没有在 AIE 侧逐位写明包头格式，但 PL 侧的包路由器在解析输出包时，把 AMD UG1079 的包头位域表完整抄进了注释（[design/pl/dma_pkt_router.cpp:36-L50](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L36-L50)）。同一套 AXI4-Stream 包交换协议在 AIE 内部路由（demux→pktsplit）和 AIE→PL 输出时是通用的，所以这张表对本讲的 demux 包头同样适用：

| 位 | 字段 | 说明 |
| --- | --- | --- |
| 4–0 | Packet ID | 5 位路由标签（0–31），即 `pkt_id` |
| 11–5 | 7'b0 | 保留 |
| 14–12 | Packet Type | 3 位包类型，即 `pkt_type` |
| 15 | 1'b0 | 保留 |
| 20–16 | Source Row | 源 tile 行（硬件填写） |
| 27–21 | Source Column | 源 tile 列（硬件填写） |
| 30–28 | 3'b0 | 保留 |
| 31 | Odd parity | bits[30:0] 的奇偶校验（硬件填写） |

两个关键推论：

1. **`pkt_id` 只有 5 位 → 取值 0–31**，这正是 `pktsplit`/`pktmerge` 路数上限为 32、进而 `IMG_SOLVERS_PER_SWITCH` 上限为 32 的物理根因（呼应 u4-l2）。
2. **demux 只需填 `pkt_type` 与 `pkt_id`**（通过 `writeHeader`），而源行列与奇偶校验由流交换硬件在包传输过程中自动补全。所以 demux 侧看不到、也不必关心 Source Row/Column/parity。

> PL 侧解析时正是把低 5 位掩出来当 `pkt_id`、把 14–12 位当 `pkt_type`（[design/pl/dma_pkt_router.cpp:51-L53](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L51-L53)）：`pkt_id = header & 0x1F;` `pkt_type = (header & 0x7000) >> 12;`。这与 demux 写入的字段位置完全对齐。

#### 4.2.4 代码实践

**实践目标**：手算一个包头，把 `writeHeader` 的两个参数落到 32 位字上。

**操作步骤**：
1. 假设 `getPacketid(px_xyz_out, 5)` 返回 `pkt_id = 9`（编译期分配，具体值待本地验证），`pkt_type = 0`。
2. 按 [design/pl/dma_pkt_router.cpp:38-L50](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L38-L50) 的位域表，写出 demux 能控制的那些位的值：bits[4:0] = `01001`（9），bits[14:12] = `000`（pkt_type=0），其余 demux 控制的位为 0。
3. 对照 PL 侧的掩码 `pkt_id = header & 0x1F`、`pkt_type = (header & 0x7000) >> 12` 验证：从这个字里能取回 `pkt_id=9`、`pkt_type=0`。

**需要观察的现象**：demux 写出的包头里，只有低 5 位（pkt_id）和 14–12 位（pkt_type）是有意义的；16–31 位此时还未被硬件填上 Source Row/Column/parity。

**预期结果**：demux 能控制的 32 位中，`pkt_id` 占 bits[4:0]、`pkt_type` 占 bits[14:12]，二者位置与 PL 侧的解析掩码一致；至于 `getPacketid` 的真实返回值，需在本地用 `aiesimulator` 打印 `pkt_id` 确认（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 demux 要用 `getPacketid(px_xyz_out, switch_idx)` 取 `pkt_id`，而不是直接写 `pkt_id = switch_idx`？

**答案**：因为 `pkt_id` 是 ADF 编译器在编译期自动分配的，未必等于目的端口的下标 `switch_idx`（可能不连续）。直接硬编码会与编译器分配的真实路由表对不上，导致 `pktsplit` 把包送到错误的内核。必须用 `getPacketid` 间接查询「想到达第 `switch_idx` 个目的应使用的真实 pkt_id」。

**练习 2**：注释说「`pkt_type` 是 3-bit pattern」（[design/aie/backprojection.cc:17-L18](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L17-L18)）。这与包头位域表里的哪几位对应？为什么是 3 位？

**答案**：对应包头 bits[14:12]，正好 3 位，所以取值范围 0–7。本设计所有目标像素包都用 `pkt_type = 0`；真正区分目的地的是 5 位的 `pkt_id` 而非 `pkt_type`。

---

### 4.3 每包像素数与 TLAST 包边界：px_components_per_ai 与 tlast

#### 4.3.1 概念说明

**TLAST** 是 AXI4-Stream 的「包结束」信号：一个 beat 上若 TLAST=1，表示这是当前包的最后一个数据。在包交换网络里，TLAST 是**包边界的唯一标记**——`pktsplit` 靠它知道「这个包到此结束、下一个 beat 是新包的包头」。如果没有 TLAST，整条流会被当成一个无限长的包，路由完全失效。

本讲 demux 在写出每个包体时，用 `writeincr` 的第三个参数显式控制 TLAST（[design/aie/backprojection.cc:35](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L35)）：

```cpp
writeincr(px_xyz_out, px_xyz_val, px_idx==(px_components_per_ai-1));
```

即：只有当写到一个包体的**最后一个 float** 时，TLAST 才置 1。

#### 4.3.2 核心流程

先算清楚「每个包有多少个 float」。由 [design/common.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h) 的默认值：`PULSES=602`、`RC_SAMPLES=512`、`IMG_SOLVERS = AIE_SWITCHES × IMG_SOLVERS_PER_SWITCH = 7 × 32 = 224`。

每个重建内核分到的像素数为：

\[
\text{SAMPLES} = \frac{\text{PULSES} \times \text{RC\_SAMPLES}}{\text{IMG\_SOLVERS}} = \frac{602 \times 512}{224} = 1376
\]

每个像素有 X、Y、Z 三个分量，所以 demux 给每个内核打出的包要携带：

\[
\text{px\_components\_per\_ai} = \text{SAMPLES} \times 3 = 1376 \times 3 = 4128 \text{ 个 float}
\]

这正是源码里的定义（[design/aie/backprojection.cc:21](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L21)）：

```cpp
int px_components_per_ai = ((PULSES*RC_SAMPLES)/IMG_SOLVERS)*3;
```

> 这里隐藏着 u1-l4 强调的**整除约束**：\((\text{PULSES}\times\text{RC\_SAMPLES})\) 必须能被 \(\text{IMG\_SOLVERS}=224\) 整除，否则 `px_components_per_ai` 会被整数除法截断，导致 demux 写出的像素数与重建内核期望读到的像素数对不上，图像 silently 损坏。\(602\times512=308224\)，\(308224/224=1376\) 整除，安全。

**demux 与重建内核之间的契约**（这是本讲最值得记住的一点）：demux 每个包写「1 个包头 + 4128 个 float（最后一个带 TLAST）」；重建内核 `img_reconstruct_kern` 恰好读「1 个包头（读后丢弃）+ 4128 个 float 重新解释成 1376 个像素的 X/Y/Z」。

对照重建内核一侧（[design/aie/backprojection.cc:78-L87](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L78-L87)）：

```cpp
uint32 header = readincr(px_xyz_in);          // 读掉包头（demux 写的那个）
for(int px_idx=0; px_idx < SAMPLES; px_idx++) // SAMPLES=1376，每次读 X/Y/Z 三个
    int raw_x = readincr(px_xyz_in);
    int raw_y = readincr(px_xyz_in);
    int raw_z = readincr(px_xyz_in);
    // 把 int 位模式 reinterpret_cast 还原成 float（包流数据以 int 传输）
```

两侧的「1376 像素 × 3 = 4128 float」完全对齐：demux 的 `px_components_per_ai` 等于重建内核读到的 `SAMPLES*3`。**TLAST 保证 `pktsplit` 把这连续 4128 个 float 当作一个完整包原封不动送到同一个内核**，不会与下一个内核的包混淆。

#### 4.3.3 源码精读

内层循环负责把 4128 个 float 逐个搬过包流（[design/aie/backprojection.cc:29-L36](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L29-L36)）：

```cpp
for (int px_idx=0; px_idx<px_components_per_ai; px_idx++) {
    float px_xyz_val = readincr(px_xyz_in);
    writeincr(px_xyz_out, px_xyz_val, px_idx==(px_components_per_ai-1));
}
```

注意几个细节：

- `readincr(px_xyz_in)`：从**普通输入流**逐个读 float（标量读，因为像素本就是主机按顺序推入的）。
- `writeincr(px_xyz_out, val, tlast)`：向**包流**写一个 beat，第三参数是 TLAST。
- `px_idx==(px_components_per_ai-1)`：只有写第 4128 个（下标 4127）float 时 TLAST=1，标记本包结束。下一个 `switch_idx` 迭代再 `writeHeader` 开启新包。

源码里还留了一段被注释掉的 `printf`（[design/aie/backprojection.cc:32-L33](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L32-L33)），可用于调试时观察每个 beat 的 `switch_idx / pkt_id / px_xyz_val / tlast`——这是本讲「代码实践」里建议你打开的调试手段。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：亲手算出 `px_components_per_ai`，说清每个包携带多少像素的 X/Y/Z，并解释 TLAST 在 `px_idx==(px_components_per_ai-1)` 时置位的意义。

**操作步骤**：
1. 用默认宏值计算：
   - \(\text{SAMPLES} = (602 \times 512)/224 = 1376\)
   - \(\text{px\_components\_per\_ai} = 1376 \times 3 = 4128\)
2. 回答：每个包携带多少个像素？每个像素含哪三个分量？
   - 每个包 = 4128 个 float = **1376 个像素**，每个像素含 **X、Y、Z** 三个 float。
3. 解释 `px_idx==(px_components_per_ai-1)`（即 `px_idx==4127`）时 TLAST 置 1 的意义。
4. （可选调试）打开 [design/aie/backprojection.cc:32-L33](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L32-L33) 的 `printf` 注释，用 `aiesimulator` 跑一次，观察每个包最后一个 beat 的 `tlast` 是否为 1、且 `pkt_id` 是否随 `switch_idx` 变化（待本地验证）。

**需要观察的现象 / 预期结果**：

- 每个包 4128 个 float，最后 1 个 float 的 TLAST=1，其余 4127 个 TLAST=0。
- TLAST 置位的意义：它是**包边界标记**，告诉 `pktsplit`「这个内核的数据到此结束」；接着 demux 写下一个 `writeHeader` 开启新包，`pktsplit` 依据新包头的 `pkt_id` 把后续数据送到下一个内核。若没有 TLAST，`pktsplit` 无法区分包边界，32 个内核的数据会粘连、路由错乱；若 TLAST 提前置位，则包被截断，内核读到的像素数不足 1376。

**关于「若是质数会怎样」的延伸**：若把 `PULSES` 改成不能让 \((\text{PULSES}\times512)\) 被 224 整除的值，则 \(\text{SAMPLES}\) 不再是整数，`px_components_per_ai` 被截断，demux 写出的 float 数与重建内核期望读到的 float 数失配——能编译，但图像损坏（呼应 u1-l4 的整除约束）。

#### 4.3.5 小练习与答案

**练习 1**：每个 demux 包含 4128 个 float，那一次完整的 demux 调用（32 个内核）共读写多少个 float？

**答案**：\(32 \times 4128 = 132096\) 个 float（读入 132096 个、写出 132096 个，外加 32 个包头）。这 132096 个 float 对应一个 switch 管辖的 \(32 \times 1376 = 44032\) 个像素（每个像素 3 个 float）。

**练习 2**：把 `writeincr` 的第三参数改成常量 `false`（永不置 TLAST），系统会出现什么现象？

**答案**：包永远不会结束。`pktsplit` 无法识别包边界，会把 demux 写出的全部数据当成一个超长包，路由到某一个 `pkt_id` 对应的内核；其余 31 个内核的像素输入流永远收不到「属于自己的包」，重建结果错误甚至死锁。

**练习 3**：为什么像素在包流里以 `float` 写出，却要在重建内核里先按 `int` 读再 `reinterpret_cast` 回 `float`（[design/aie/backprojection.cc:80-L86](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L80-L86)）？

**答案**：包交换流（pktstream）的数据类型按 ADF 规约以整型（`int`/`uint32`）传输；float 没有专门的包流重载，所以 demux 以 float 写入、接收侧以 `int` 读出其位模式，再用 `reinterpret_cast` 把同样的 32 位位模式重新解释成 float。值并未改变，只是类型系统的「口径转换」。（这一点的细节将在 u5-l3 详讲。）

## 5. 综合实践

**任务**：手工「单步执行」一次 demux 调用的前两个包，把整条包流的 beat 序列画出来，并跟踪一个具体像素从 GMIO 到重建内核的全过程。

**操作步骤**：

1. 假设当前脉冲下，主机经 `gmio_in_xyz_px` 推入了一个 switch 的全部 132096 个 float（32 个内核 × 4128）。设 `getPacketid(px_xyz_out, 0)` 返回 \(id_0\)、`getPacketid(px_xyz_out, 1)` 返回 \(id_1\)（真实值待本地验证）。
2. 仿照下表，列出前两个包的 beat 序列（H=包头，D=数据，TLAST 列标 0/1）：

   | switch_idx | beat 序号 | 内容 | TLAST | 说明 |
   | --- | --- | --- | --- | --- |
   | 0 | 0 | Header(pkt_type=0, pkt_id=\(id_0\)) | — | 开启第 0 个内核的包 |
   | 0 | 1 | 像素0的 X | 0 | |
   | 0 | 2 | 像素0的 Y | 0 | |
   | 0 | 3 | 像素0的 Z | 0 | |
   | 0 | … | … | 0 | |
   | 0 | 4128 | 像素1375的 Z | **1** | 第 0 个包结束 |
   | 1 | 4129 | Header(pkt_type=0, pkt_id=\(id_1\)) | — | 开启第 1 个内核的包 |
   | 1 | … | … | 0 | |
   | 1 | 8256 | （第 1 个包的最后一个 float） | **1** | 第 1 个包结束 |

3. **跟踪一个具体像素**：取「第 2 个内核的第 0 个像素的 X 分量」。它从主机 `m_xyz_px_array` 出发 → 经 `gmio_in_xyz_px`（GMIO，[design/aie/graph.h:79](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L79)）→ 被 demux 在 `switch_idx=1` 那轮的 `readincr` 读出（[design/aie/backprojection.cc:30](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L30)）→ 作为该包的第 1 个数据 beat 写入包流 → `pktsplit` 依据包头 `pkt_id=\(id_1\)` 把整个包送到 `img_rec_km[1].in[2]`（[design/aie/graph.h:88-L89](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L88-L89)）→ 重建内核读完包头后在 `px_idx=0` 处以 `raw_x` 读到它（[design/aie/backprojection.cc:80](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L80)）。

**预期结果**：你能清晰地说明——demux 用 `writeHeader` + TLAST 把一条物理流切成了 32 个逻辑包，每个包 4128 个 float、对应 1376 个像素，靠 `pkt_id` 各自路由；而重建内核一侧的读取顺序与 demux 的写入顺序**逐 float 对齐**。这就是 demux 与重建内核之间的完整协议。

## 6. 本讲小结

- **目标像素不能广播**：每个重建内核要的是 1376 个**不同**像素，所以必须用包交换按内核分发，由 `px_demux_kern` 担任「串行→分包器」。
- **接线是 1→32**：demux 只有一个 `output_pktstream`，经 `pktsplit<32>` 把一条物理流按 `pkt_id` 分发给 32 个重建内核的 `in[2]`；顶层再用 7 个 switch 复制成 224 路。
- **`getPacketid` 间接取路由标签**：`pkt_id` 由 ADF 编译期分配、未必连续，必须用 `getPacketid(port, index)` 查询，再由 `writeHeader(port, pkt_type, pkt_id)` 写入包头。
- **包头位域**：`pkt_id` 占低 5 位（故上限 32）、`pkt_type` 占 bits[14:12]；源行列与奇偶校验由硬件补全（UG1079）。
- **`px_components_per_ai = 4128`**：每个包 = 1376 像素 × 3(X/Y/Z)；它依赖 u1-l4 的整除约束成立。
- **TLAST 是包边界**：`px_idx==(px_components_per_ai-1)` 时置 1，`pktsplit` 靠它分辨包的起止；demux 写「包头 + 4128 float（末 beat 置 TLAST）」与重建内核读「包头 + 1376 像素」严格对齐。

## 7. 下一步学习建议

demux 把像素打包送出后，下一站就是**重建内核如何消费这些像素**。建议接着学：

- **u5-l3 图像重建(一)：像素提取与差分距离**：讲 `img_reconstruct_kern` 如何读掉 demux 写的包头、把 4128 个 float 重新解释成 1376 个 X/Y/Z 像素（即本讲 4.3.5 练习 3 埋下的 `reinterpret_cast`），并用 SIMD 计算双程距离差。
- 之后再进 **u5-l4（相位校正）** 与 **u5-l5（插值累加与 RTP dump）**，看像素如何最终变成聚焦图像。
- 若想看 demux 写出的包头在「输出侧」如何被 PL 解析、进而理解整个包头协议的闭环，可跳读 **u6-l1（PL 包路由器 HLS 内核）**。
