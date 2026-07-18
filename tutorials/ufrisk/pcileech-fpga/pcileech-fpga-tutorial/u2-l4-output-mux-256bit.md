# 输出多路复用器与 256 位打包

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `pcileech_mux` 把 8 个 32 位输入源「合并打包」成一个 256 位输出字的动机与收益；
- 读懂 `p0_idx → p8_idx` 这条「索引推进链」如何用一行行加法实现优先级与紧凑打包；
- 解释 256 位输出包「1 个状态字 + 7 个数据字」的格式，以及 `tag` / `ctx` 两个字段分别承载什么语义；
- 说明内部「空闲端口 `p8`」和「输出缓冲 `dout_buf`」这两个边界机制如何分别解决「尾部残包」和「下游反压」两个问题；
- 对照 `pcileech_fifo.sv` 中 `i_pcileech_mux` 的例化，把 loopback / command / cfg / tlp 四类数据正确对应到 `p0..p6` 端口，并理解各端口 `tag` 值与接收方向 MAGIC 路由的对称关系。

本讲只聚焦一个最小模块：**`pcileech_mux`**。它是整张板卡「上行回主机」方向的汇流闸口，承接上一讲 `pcileech_fifo`（[u2-l3](u2-l3-fifo-control-and-magic-routing.md)）讲过的发送方向多路复用，把它从「一张示意图」落实到真实代码。

## 2. 前置知识

在进入本讲前，建议你已经建立以下认知（前面几讲的内容）：

- **PCIe TLP / CFG / Loopback / Command 四类数据**：在 `pcileech_fifo` 接收方向，主机下发的 64 位数据按 `[9:8]` 的 type 字段被分流到这四条通路（见 u2-l3）。
- **AXI-Stream 风格握手**：`tvalid`（数据有效）与 `tready`（下游准备好）同时为真时，一拍数据才算成功交接（见 u2-l1 的 `IfAXIS128`）。
- **`IfComToFifo` 契约**：com↔fifo 之间的上行通道是 **256 位** 的 `com_din`，配 `com_din_wr_en`（有效）与 `com_din_ready`（反压）两个控制位。本讲要讲的，正是这 256 位是怎么「攒」出来的。
- **FT601 是 32 位 USB3 桥**：它一次只能吞/吐 32 位数据（见 u2-l2）。所以「上行」方向天然存在一个「多路 32 位 → 单路 32 位」的汇合需求。

一个直觉问题先放在脑子里：**fifo 同时收到来自 PCIe 的 TLP、配置响应、命令响应、回环数据，而回主机的 USB3 通道一次只走 32 位——这些数据该怎么排队、怎么合流、怎么不让后来的把先来的冲掉？** `pcileech_mux` 就是回答这个问题的模块。

## 3. 本讲源码地图

本讲涉及两个关键文件：

| 文件 | 作用 |
| --- | --- |
| [PCIeSquirrel/src/pcileech_mux.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv) | 本讲主角。一个纯组合+少量寄存器的「打包器」，把最多 8 路 32 位输入按优先级紧凑塞进 256 位输出。 |
| [PCIeSquirrel/src/pcileech_fifo.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv) | mux 的「用户」。在发送方向例化 `i_pcileech_mux`，把 loopback/command/cfg/tlp 四类数据接到 `p0..p6`。 |
| [PCIeSquirrel/src/pcileech_header.svh](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh) | 定义 `IfComToFifo`，其中 `com_din[255:0]` 就是 mux 输出的落点。 |

> 本讲继续以推荐主参考工程 **PCIeSquirrel** 为对象。其他设备的 `pcileech_mux.sv` 与此处几乎完全一致，属于跨设备复用的公共 HDL。

## 4. 核心概念与源码讲解

### 4.1 多路复用的动机与整体数据流

#### 4.1.1 概念说明

「多路复用（Multiplex，简称 mux）」在数字电路里通常指「多选一」——多个输入里挑一个输出。但 `pcileech_mux` 做的事情比「多选一」更聪明：它是**「多路合流 + 紧凑打包」**。

要理解它为什么这么设计，先看一个矛盾：

- **输入侧**：fifo 同时有多类数据要回主机——PCIe 的 TLP（DMA 读到的内存数据，最海量）、配置空间读写响应（CFG）、命令寄存器读回值（Command）、以及用于链路自检的回环数据（Loopback）。其中 TLP 甚至有 4 路并行（对应 `dtlp.rx_data[0..3]`）。
- **输出侧**：回主机的 `IfComToFifo` 通道是 **256 位** 的 `com_din`，最终经 FT601 以 32 位为单位发往 USB3。

如果让各类数据「轮询」独占输出（典型 mux），那一个 256 位包里可能只有一个 32 位有效字、其余全是填充，效率极低。`pcileech_mux` 的做法是：**每一拍都把所有「就绪」的 32 位字尽量塞进同一个 256 位包里一起发**，塞满 7 个数据字就立即输出一个包。这样带宽利用率的理论上限为：

\[
\eta = \frac{7 \times 32}{256} = \frac{224}{256} = 87.5\%
\]

即一个 256 位包里有 224 位是真数据，只有 32 位（1 个状态字）是开销。这对「内存 DMA 读」这种持续大流量场景非常关键。

#### 4.1.2 核心流程

用一个文字流程图概括 mux 在每一拍（每个 `clk` 上升沿）做的事：

```
每一拍 clk 上升沿：
  1. 看下游（com）是否准备好接收：en = rd_en && !rst
  2. 同时采样 8 个输入端口 p0..p7 的 wr_en（谁这一拍有数据）
  3. 用「索引推进链」给每个有效数据分配一个紧凑槽位号 idx
  4. 把数据写进内部寄存器 data_reg[idx] / ctx_reg[idx]
  5. 若本拍累计满 ≥7 个字（idx_max >= 7）→ 拼一个 256 位包输出
     并把没发完的剩余字下移，等下一拍继续凑
  6. 若长时间空闲且还有尾部残包 → 由内部 p8 端口补一个 idle 字强行冲出
  7. 若下游反压（rd_en=0）→ 把该输出的包存进 dout_buf，不丢
```

关键点：**所有端口每拍都被采样**（不是「选一个赢家」），所以它不是仲裁器，而是「合流打包器」。只有在「一拍内就绪的字超过 7 个」这种饱和情况下，端口优先级才会真正决定「谁先走、谁等下一拍」。

#### 4.1.3 源码精读

模块的端口声明就透露了它的「合流」本质：8 组结构完全相同的输入端口（`p0` 到 `p7`），每组 5 根线——32 位数据 `din`、2 位 `tag`、2 位 `ctx`、写使能 `wr_en`、以及一个「请求数据」输出 `req_data`；外加一组 256 位输出。

- 8 组输入端口声明：[PCIeSquirrel/src/pcileech_mux.sv:14-69](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv#L14-L69) —— 注意 `p0` 上方注释写着 `input highest priority`，即端口号越小优先级越高。

模块头部注释一句话点明设计意图：

- [PCIeSquirrel/src/pcileech_mux.sv:4-6](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv#L4-L6)：把多个 32 位字合并成「1 个 32 位状态字 + 7 个数据字」的 256 位字，目的是为了在 FT601 上「相对高效」地传输，并附带一些额外信息。

而输出最终落到 `IfComToFifo` 的 `com_din`/`com_din_wr_en`/`com_din_ready` 三根线上，这组契约定义在：

- [PCIeSquirrel/src/pcileech_header.svh:19-35](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L19-L35)：`com_din[255:0]` 即 mux 的 256 位输出；`mp_fifo` modport 里它和 `com_din_wr_en` 都是 output，`com_din_ready` 是 input（反压）。

#### 4.1.4 代码实践

**实践目标**：用「数据流图」建立 mux 在系统中的位置感。

**操作步骤**：

1. 打开 [PCIeSquirrel/src/pcileech_fifo.sv:82-100](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L82-L100)，阅读那段 ASCII 注释图（它画了 TLP/CFG/Loopback/Command 四路汇入「MULTIPLEXER」再经缓冲 FIFO 到 FT601 的走向）。
2. 在纸上画出三个框：左边 4 类输入源（TLP×4 / CFG / Loopback / Command），中间一个 `pcileech_mux` 框，右边一个 `FT601/USB3` 框。
3. 标注中间框的输出位宽（256 位）和右边框的实际吞叶位宽（32 位），并在中间框上方写明「7 数据 + 1 状态」。

**需要观察的现象**：你会注意到，fifo 内部其实还有一层「缓冲 FIFO（NATIVE OR DRAM）」夹在 mux 和 FT601 之间——也就是说 mux 产出的 256 位包并不会直接逐拍送 USB，而是先被一个 256→32 的缓冲 FIFO 平滑成 32 位流。这一层不是本讲主角，但要知道它存在。

**预期结果**：得到一张清晰的「四类数据 → mux(256 位包) → 缓冲 FIFO → FT601(32 位) → 主机」的走向图，为后面 4.2~4.5 拆解 mux 内部做准备。

#### 4.1.5 小练习与答案

**练习 1**：如果改成一个「每拍只选一个端口输出」的普通 8 选 1 mux，在持续 DMA 读流量下，256 位通道的带宽利用率最差会变成多少？

**答案**：最差只有 \(1 \times 32 / 256 = 12.5\% \)——一个包里只有 1 个有效 32 位字。这正是 `pcileech_mux` 选择「合流打包」而非「多选一」的根本原因。

**练习 2**：模块端口列表里有 8 组输入端口（`p0..p7`），但模块内部还出现了一个 `p8`。`p8` 是从哪里来的？

**答案**：`p8` 不是外部端口，而是模块内部用 `wire` 凭空构造的「空闲端口」（idle port），数据恒为 `32'hffffffff`，用来在长时间空闲时把缓冲区里没凑满 7 个字的「尾部残包」强行冲出去。详见 4.4 节。

---

### 4.2 优先级索引链：p0_idx → p8_idx 的推进

#### 4.2.1 概念说明

mux 要在「一拍内可能有多路同时来数据」的情况下，给每个有效字分配一个**紧凑且不冲突的槽位号**，并保证「端口号小 = 槽位号小 = 优先级高」。这听起来像需要一个复杂的仲裁器，但本模块只用了一行接一行的「加法链」就搞定了，非常巧妙。

核心思想是**前缀和（prefix sum）**：从 `idx_base`（本拍的起始槽位）开始，每遇到一个 `wr_en=1` 的端口，槽位号就 +1。于是：

\[
p_k^{\text{idx}} = \text{idx\_base} + \sum_{i=0}^{k-1} p_i^{\text{wr\_en}}
\]

即第 k 个端口的槽位号 = 起始槽位 + 它前面所有「真有数据」的端口个数。这样：

- 所有有效数据拿到**连续且唯一**的槽位号，绝不会撞车；
- 端口号越小，槽位号越小，数据出现得越靠前 → 优先级越高。

#### 4.2.2 核心流程

用一个具体例子说明。假设某一拍 `idx_base = 0`，且 p1（command）、p3（TLP#0）、p6（TLP#3）三路同时有数据（`wr_en=1`），其余为 0：

| 端口 | wr_en | idx 计算 | 分配到的槽位 |
| --- | --- | --- | --- |
| p0 | 0 | `p0_idx = 0` | —（无数据） |
| p1 | 1 | `p1_idx = 0 + 0 = 0` | **槽位 0** |
| p2 | 0 | `p2_idx = 0 + 1 = 1` | — |
| p3 | 1 | `p3_idx = 1 + 0 = 1` | **槽位 1** |
| p4 | 0 | `p4_idx = 1 + 1 = 2` | — |
| p5 | 0 | `p5_idx = 2 + 0 = 2` | — |
| p6 | 1 | `p6_idx = 2 + 0 = 2` | **槽位 2** |
| p7 | 0 | `p7_idx = 2 + 1 = 3` | — |
| p8（内部） | 0 | `p8_idx = 3 + 0 = 3` | — |
| — | — | `idx_max = 3 + 0 = 3` | 本拍累计 3 个字 |

三个字被紧凑塞进槽位 0/1/2，没有空洞。由于 `idx_max = 3 < 7`，本拍**不输出**，`idx_base` 下一拍保持不变（`idx_max - 0 = 3`），继续累积，直到某拍 `idx_max >= 7` 才拼包输出。

> **关于「优先级」的一个关键澄清**：因为所有端口每拍都被采样、所有有效字都会被打包，所以**在非饱和状态下根本没有「谁先谁后」的竞争**——大家都被收下。优先级只在「一拍内就绪字 > 7」的饱和瞬间才起作用：此时只有槽位 0~6 会被发出去，槽位 ≥7 的字要等下一拍。端口号小 → 槽位小 → 一定落在 0~6 内 → 优先发出。所以「优先级高」=「饱和时 Guaranteed 发出」。

#### 4.2.3 源码精读

索引推进链是纯组合逻辑，9 行连续赋值：

- [PCIeSquirrel/src/pcileech_mux.sv:89-100](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv#L89-L100)：从 `p0_idx = idx_base` 开始，每一行把上一行的结果加上本端口的 `wr_en`（0 或 1）。`p8_idx` 是内部空闲端口的位置，`idx_max` 是最终累计字数。每行右侧注释点明 `p8` 是 idle port、`idx_max` 是最大下标。

数据真正写入寄存器发生在时序逻辑里，按计算出的 `pX_idx` 定位：

- [PCIeSquirrel/src/pcileech_mux.sv:150-158](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv#L150-L158)：`if (p0_wr_en) begin data_reg[p0_idx] <= p0_din; ctx_reg[p0_idx] <= {p0_ctx, p0_tag}; end` …… 8 个端口（含内部 p8）各一条。注意 `ctx_reg` 写入的是 `{ctx, tag}` 拼成的 4 位——这正是后面状态字里每个 nibble 的来源。

`idx_base` 的推进（决定下一拍从哪个槽位继续累积）：

- [PCIeSquirrel/src/pcileech_mux.sv:146](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv#L146)：`idx_base <= idx_max - ((idx_max >= 7) ? 7 : 0);` —— 如果本拍凑够了 7 个字并输出了一个包，就把基准前移 7（剩余的字下移到 0 起）；否则基准 = idx_max（即把本拍新写的字自然接在后面）。

> 配套的「剩余字下移」逻辑在 [PCIeSquirrel/src/pcileech_mux.sv:161-170](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv#L161-L170)：当 `dout_valid` 时，把槽位 7~13 里没发完的字搬回 0~6 区间，避免被覆盖。

#### 4.2.4 代码实践

**实践目标**：亲手跑一遍索引链，验证「紧凑无空洞」。

**操作步骤**：

1. 打开 [PCIeSquirrel/src/pcileech_mux.sv:91-100](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv#L91-L100)。
2. 假设 `idx_base = 2`，且某拍 `p0_wr_en=1, p1_wr_en=0, p2_wr_en=1, p3..p7_wr_en=0`。
3. 在纸上逐行算出 `p0_idx, p1_idx, ..., p8_idx, idx_max`。
4. 写出这一拍哪几个槽位被写入了数据。

**需要观察的现象**：p0（wr_en=1）拿到槽位 `idx_base = 2`；p2（wr_en=1）紧接其后拿到槽位 3。两个有效字落在连续的槽位 2、3，中间没有空洞。

**预期结果**：`p0_idx=2, p2_idx=3, idx_max=3`（p8_idx 也是 3，idx_max=3）。p0 的数据进 `data_reg[2]`，p2 的数据进 `data_reg[3]`。这验证了「加法链 = 紧凑前缀和分配」。

#### 4.2.5 小练习与答案

**练习 1**：如果同一拍内 8 个外部端口（p0~p7）全部 `wr_en=1`，`idx_base=0`，会发生什么？

**答案**：`p7_idx = 8`，`p8_idx = 9`（p8 不触发，因为不满足 idle 条件），`idx_max = 9 ≥ 7`，于是本拍输出一个 256 位包（槽位 0~6），剩余槽位 7、8 的两个字下移到槽位 0、1，`idx_base` 下一拍变为 `9 - 7 = 2`。8 个字里有 7 个立即发出、2 个字的「溢出」被保留——这正是优先级起作用的饱和场景。

**练习 2**：为什么说这套加法链「天然互斥、不会撞槽位」？

**答案**：因为每个 `pX_idx` 都严格等于「它之前所有 wr_en 的累加」，而两个相邻的 wr_en=1 端口之间，后者的 idx 恰好比前者大 1。任意两个有效端口的 idx 之差等于它们之间（含起点）的 wr_en 个数，至少为 1，故槽位号两两不同。

---

### 4.3 256 位包格式：1 状态字 + 7 数据字

#### 4.3.1 概念说明

`pcileech_mux` 输出的 256 位不是「7 个 32 位数据」简单拼接，而是 **「1 个 32 位状态字 + 7 个 32 位数据字」**。为什么要把宝贵的 32 位「浪费」在一个状态字上？

因为主机收到这 256 位后，需要知道**每个数据字来自哪一路、属于哪个包**，才能正确拆分还原。没有状态字，主机面对一串 32 位字根本无法分辨「这是 TLP 的第 3 拍还是命令响应」。状态字就是这 7 个数据字的「随附标签」。

每个数据字的标签是 4 位，拆成两个字段：

- **`tag[1:0]`（2 位）**：标识**数据大类**，与接收方向 MAGIC 路由的 type 字段完全对称——`00`=TLP、`01`=CFG、`10`=Loopback、`11`=Command。主机靠它把字路由回对应子流。
- **`ctx[1:0]`（2 位）**：**子流内的元数据**，含义随 tag 不同而变。例如对 TLP，`ctx = {first, last}`，标记本 32 位字是不是一个 TLP 包的首拍/末拍（包边界）；对 Loopback/Command，则透传 FIFO 里携带的 2 位上下文。

#### 4.3.2 核心流程

256 位输出字的内存布局（高位在前）：

```
┌─────────────────────── dout[255:0] ───────────────────────┐
│  状态字 (32 bit)  │ 数据字#0 │ #1 │ #2 │ #3 │ #4 │ #5 │ #6 │
│  8 × 4-bit nibble │     （7 × 32 bit = 224 bit 数据）      │
└───── [255:224] ───┴── [223:0] 数据区，每个 32 位 ──────────┘
```

状态字内部的 8 个 nibble（每个 4 位）分别是 7 个数据字的 `{ctx, tag}` 标签 + 1 个常量标记 `0xE`，按一个**固定排列**交织在拼接表达式里。这个排列由主机端（LeechCore 的 C 解码代码）镜像匹配——本讲不必死记，只要知道「状态字的每个 nibble 描述一个数据字的来源，且有一个常量 0xE 作边界标记」即可。

```
状态字 = { ctx_reg[1], ctx_reg[0], ctx_reg[3], ctx_reg[2],
           ctx_reg[5], ctx_reg[4], 4'hE,          // 常量标记
           ctx_reg[6] }
```

每个 `ctx_reg[i]` 本身又是 `{ctx, tag}` 共 4 位（见 4.2.3）。所以一个数据字的完整身份信息 = 它在状态字里对应的那个 4 位 nibble。

#### 4.3.3 源码精读

256 位打包表达式集中在一行 `wire`：

- [PCIeSquirrel/src/pcileech_mux.sv:114](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv#L114)：`dout_data = { ctx_reg[1], ctx_reg[0], ctx_reg[3], ctx_reg[2], ctx_reg[5], ctx_reg[4], 4'hE, ctx_reg[6], data_reg[0..6] }`。最高 32 位是状态字（8 个 nibble），低 224 位是 `data_reg[0]` 到 `data_reg[6]` 共 7 个数据字。

`ctx_reg` 的写入（每个 nibble 的构成）：

- [PCIeSquirrel/src/pcileech_mux.sv:150-158](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv#L150-L158)：`ctx_reg[p0_idx] <= {p0_ctx, p0_tag};`——高位是 `ctx`、低位是 `tag`。

输出有效判定：

- [PCIeSquirrel/src/pcileech_mux.sv:142](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv#L142)：`dout_valid <= en && (idx_max >= 7);`——只有当下游就绪（`en`）且累计满 7 个字，才置输出有效。

再看 `pcileech_fifo.sv` 里 `tag` 值是怎么填的，验证它与接收方向 type 的对称性：

- Loopback → p0，`p0_tag = 2'b10`（对应 RX type LOOP=10）：[PCIeSquirrel/src/pcileech_fifo.sv:153-158](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L153-L158)
- Command → p1，`p1_tag = 2'b11`（对应 RX type CMD=11）：[PCIeSquirrel/src/pcileech_fifo.sv:159-164](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L159-L164)
- PCIe CFG → p2，`p2_tag = 2'b01`（对应 RX type CFG=01）：[PCIeSquirrel/src/pcileech_fifo.sv:165-170](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L165-L170)
- PCIe TLP → p3~p6，`tag = 2'b00`（对应 RX type TLP=00），`ctx = {rx_first, rx_last}`：[PCIeSquirrel/src/pcileech_fifo.sv:171-194](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L171-L194)

接收方向的 type 定义可对照 [PCIeSquirrel/src/pcileech_fifo.sv:65-69](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L65-L69)：`CHECK_TYPE_TLP=00 / CFG=01 / LOOP=10 / CMD=11`。发送 `tag` 与接收 `type` 取值完全一致，主机才能「无损还原」。

#### 4.3.4 代码实践

**实践目标**：把「tag 对称」和「ctx 语义」两件事对上号。

**操作步骤**：

1. 打开接收方向的类型定义 [pcileech_fifo.sv:65-69](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L65-L69) 和发送方向的端口标签 [pcileech_fifo.sv:153-194](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L153-L194)。
2. 填下面这张表：

| 端口 | 数据来源 | tag（二进制） | 对应 RX type | ctx 含义 |
| --- | --- | --- | --- | --- |
| p0 | Loopback | ? | ? | `_loop_dout[33:32]`（透传） |
| p1 | Command | ? | ? | `_cmd_tx_dout[33:32]`（透传） |
| p2 | PCIe CFG | ? | ? | 常量 `2'b00` |
| p3~p6 | PCIe TLP×4 | ? | ? | `{rx_first, rx_last}` |

3. 对 TLP 端口，解释为什么 `ctx` 要放 `{first, last}`。

**需要观察的现象**：四类数据的 `tag` 与 RX 的 `type` 取值一一相等；TLP 的 `ctx` 用两位分别标记「包首」和「包末」。

**预期结果**：p0=tag10/type LOOP；p1=tag11/type CMD；p2=tag01/type CFG；p3~p6=tag00/type TLP。TLP 一个包可能横跨多个 32 位字（一个内存读完成 CplD 可能几十上百字节），主机必须靠 `first/last` 才能把连续若干个 32 位字重新拼回一个完整 TLP，所以 `ctx` 承载包边界信息。

#### 4.3.5 小练习与答案

**练习 1**：状态字里那个常量 `4'hE` 是干什么用的？

**答案**：它是一个固定不变的 nibble（`4'b1110`），嵌在每个状态字的固定位置。它的具体作用需要对照主机端 LeechCore 的解码代码确认（**待对照主机端 C 代码**），但至少可以确定：它让每个状态字都有一个可预测的「特征位」，主机可据此做帧对齐/校验。本讲不臆测其完整语义。

**练习 2**：如果一个数据字是「某个 TLP 包的最后一拍」，它在状态字里对应的 nibble 大概会长什么样（按 `{ctx, tag}` 格式）？

**答案**：tag=TLP=`00`；`ctx={first, last}={0, 1}`（非首拍、是末拍）。所以 nibble = `{ctx, tag} = {0,1,0,0} = 4'b0100`。

---

### 4.4 空闲端口 p8：尾部残包的「强制冲刷」

#### 4.4.1 概念说明

4.2 节的机制有一个隐患：**只有累计满 7 个字才输出一个包**。那如果某条数据流只来了 3 个字之后就长期不再有数据呢？这 3 个字会一直卡在 `data_reg[0..2]`，永远凑不到 7，主机也就永远收不到——典型的「尾部残包」死锁。

`pcileech_mux` 用一个很巧妙的内部「假端口」`p8` 解决：当检测到「还有未发出的残包（`idx_base > 0`）」且「已经连续空闲 8 拍没有任何新数据」时，`p8` 就主动注入一个内容为 `0xffffffff` 的「填充字」，硬把字数凑到 7，触发一次输出，把残包连同填充字一起冲出去。

#### 4.4.2 核心流程

```
p8 触发条件（全部满足才注入一个填充字）：
  ① en 为真（下游就绪）
  ② idx_base > 0（缓冲区里确实有残包没发）
  ③ idx_base == p8_idx（本拍 p0~p7 没有任何新数据，否则先处理真数据）
  ④ idle_count > 7（已经连续空闲超过 8 拍，确认是真的没数据了）

注入后：data_reg[p8_idx] <= 32'hffffffff，tag=11，ctx=11
       这个填充字参与索引链，把 idx_max 顶过 7，触发 dout_valid
       残包 + 填充字一起作为 256 位包发出
```

`idle_count` 是空闲计数器：只要还满足「有残包且本拍无新数据」就 +1，一旦有新数据就清 0。

#### 4.4.3 源码精读

p8 完全是内部 `wire` 构造的「虚拟端口」：

- [PCIeSquirrel/src/pcileech_mux.sv:102-107](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv#L102-L107)：`p8_din = 32'hffffffff`（全 1 填充字）、`p8_tag = 2'b11`、`p8_ctx = 2'b11`（注意 tag=11 本是 Command 的值，但这里 ctx=11 配合全 1 数据，主机端可识别为「idle 填充」而非真命令）；`p8_wr_en` 由那四个条件的与门驱动。

`idle_count` 的累加与清零：

- [PCIeSquirrel/src/pcileech_mux.sv:148](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv#L148)：`idle_count <= ((idx_base > 0) && (idx_base == p8_idx)) ? (idle_count + 1) : 0;`

p8 的数据写入和其他端口共用同一套写入逻辑（因为 `p8_idx` 已进入索引链）：

- [PCIeSquirrel/src/pcileech_mux.sv:158](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv#L158)：`if (p8_wr_en) begin data_reg[p8_idx] <= p8_din; ctx_reg[p8_idx] <= {p8_ctx, p8_tag}; end`

> 注意：`p8_wr_en` 一旦为真，`idx_max` 就会比 `p8_idx` 大 1，很容易把 `idx_max` 顶到 ≥7，于是 [行 142](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv#L142) 的 `dout_valid` 立即生效，残包被冲出。

#### 4.4.4 代码实践

**实践目标**：理解 p8 触发时序，预测它的副作用。

**操作步骤**：

1. 假设缓冲区里已有 3 个残包字（`idx_base = 3`），且之后连续 9 拍没有任何新数据、下游一直就绪。
2. 逐步推演第 1~9 拍 `idle_count` 的值（从 0 开始）。
3. 指出哪一拍 `p8_wr_en` 首次为真，那一拍发出的 256 位包里包含什么。

**需要观察的现象**：`idle_count` 从 0 逐拍累加；当它超过 7（即第 8 拍之后）时，p8 注入填充字。

**预期结果**：第 1~8 拍 `idle_count = 1..8`，到第 8 拍 `idle_count` 已 >7，但需注意 `idle_count` 是寄存器、`p8_wr_en` 用的是它的当前值——具体首拍为真的时机取决于寄存器延迟，**精确时序建议在仿真波形中确认**。其效果是：发出一个包，其中 3 个字是真残包、4 个字是 `0xffffffff` 填充，状态字相应位置标识为 idle（tag=11/ctx=11）。

#### 4.4.5 小练习与答案

**练习 1**：为什么不直接「每拍都把缓冲区里有的字发出去」，而要等满 7 个？

**答案**：因为输出是 256 位的整包格式（1 状态 + 7 数据），不满 7 个就没法填满一个包；如果每拍都发半满的包，会让状态字开销（32 位）占比飙升，带宽利用率暴跌。p8 机制是个折中：平时等满 7 个以保高效率，只在「确实闲死了」时才补填充字冲尾部，避免死锁。

**练习 2**：p8 的 `tag=11` 和真 Command 端口（p1）的 `tag=11` 一样，主机会不会把填充字误当成命令响应？

**答案**：仅靠 tag 确实无法区分。但 p8 同时设了 `ctx=11`（真命令的 ctx 来自 `_cmd_tx_dout[33:32]`，几乎不会是 `11`），且数据是特征明显的 `0xffffffff`。主机端解码时综合 tag/ctx/数据内容来判定。完整的判别规则需对照主机端代码确认（**待对照主机端 C 代码**）。

---

### 4.5 输出缓冲 dout_buf：下游反压时不丢包

#### 4.5.1 概念说明

mux 的输出最终要喂给 FT601（经一层缓冲 FIFO），但 FT601 受 USB3 主机调度影响，不一定每拍都能收（即 `rd_en` / `com_din_ready` 可能为 0，这叫**反压 backpressure**）。

问题来了：mux 的内部逻辑是「凑满 7 个就生成一个 256 位包」。如果某个包生成的那一拍恰好下游反压（`rd_en=0`），这个包怎么办？直接丢掉显然不行——那是真实的 DMA 数据。`dout_buf` 就是为这种情况准备的「一拍深度的影子寄存器」：当下游不能收时，把生成的包暂存进 `dout_buf`，等下游恢复后再发。

#### 4.5.2 核心流程

```
每一拍：
  if (en)                 // 下游就绪 → 正常直通，清掉缓冲
      dout_buf_valid <= 0
  else if (dout_valid)    // 下游不收，但本拍有新包生成 → 存进缓冲
      dout_buf_data <= dout_data; dout_buf_valid <= 1

输出选择：
  valid = rd_en && (dout_buf_valid || dout_valid)
  dout  = dout_buf_valid ? dout_buf_data : dout_data
```

也就是说：缓冲里只要有未发包（`dout_buf_valid`），就优先发它；否则发当拍新生成的（`dout_valid`）。只要下游 `rd_en` 一就绪，`valid` 就会拉起。

> 这是一个**一拍深度**的缓冲，只能吸收「单拍的瞬时反压」。如果下游连续多拍反压，靠的是下游那一侧的「256→32 缓冲 FIFO」继续吞（见 4.1.4），而 mux 本身会因为 `en=0` 而**暂停推进 `idx_base`**（见 [行 146](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv#L146) 在 `en` 为假时不进入该分支），新数据继续在 `data_reg` 里累积，不会丢。

#### 4.5.3 源码精读

输出与缓冲的声明：

- [PCIeSquirrel/src/pcileech_mux.sv:113-118](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv#L113-L118)：`dout_data` 是当拍组合出的 256 位；`dout_buf_valid/dout_buf_data` 是影子寄存器；`valid` 和 `dout` 的选择逻辑正对应上面流程。

缓冲写入逻辑：

- [PCIeSquirrel/src/pcileech_mux.sv:134-139](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv#L134-L139)：`if (en) dout_buf_valid <= 0; else if (dout_valid) begin dout_buf_data <= dout_data; dout_buf_valid <= 1; end`

`en` 的定义（带复位、并对齐时序）：

- [PCIeSquirrel/src/pcileech_mux.sv:71-75](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv#L71-L75)：`en <= rd_en && !rst;` 注释明确 `en` 比 `rd_en` 延迟 1 拍，是为了和输入数据对齐。

所有端口的 `req_data` 都恒等于 `rd_en`：

- [PCIeSquirrel/src/pcileech_mux.sv:77-84](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv#L77-L84)：`assign p0_req_data = rd_en; ...`——mux 每拍都向所有上游 FIFO 请求数据（读），上游 FIFO 的 `valid` 配合 `req_data` 完成握手。注意 `rd_en` 直接用（不等 `en`），这是为了让上游在读出的同一拍就送上数据，`en` 的延迟在 mux 内部消化。

#### 4.5.4 代码实践

**实践目标**：理解反压下数据不丢的完整链路。

**操作步骤**：

1. 打开 [pcileech_mux.sv:134-148](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv#L134-L148)。
2. 假设第 N 拍 `idx_max >= 7` 首次成立（生成包），但恰好 `rd_en = 0`（下游反压）；第 N+1 拍 `rd_en = 1`。
3. 推演第 N 拍、N+1 拍 `dout_valid`、`dout_buf_valid`、`valid`、`dout` 各是什么。

**需要观察的现象**：第 N 拍 `dout_valid=1`、`valid=0`（因 `rd_en=0`）、`dout_buf_valid` 在拍末被置 1；第 N+1 拍 `rd_en=1`，`valid=1`，`dout` 取自 `dout_buf_data`。

**预期结果**：包在第 N+1 拍被下游收走，没有丢失。这验证了「一拍反压靠 dout_buf 顶住」。

#### 4.5.5 小练习与答案

**练习 1**：`dout_buf` 只有一拍深度，如果下游连续 3 拍反压，mux 会丢数据吗？

**答案**：不会。连续反压时 `en` 持续为 0，[行 146](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv#L146) 的 `idx_base` 推进和 [行 150-158](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv#L150-L158) 的写入都在 `if (en)` 保护下，整条流水线原地暂停，新数据在上游 FIFO 里排队。`dout_buf` 只需顶住「已生成但还没发」的那一个包即可。

**练习 2**：为什么 `valid = rd_en && (dout_buf_valid || dout_valid)` 里要乘上 `rd_en`？

**答案**：`valid` 是向下游声明「本拍给你的是有效数据」。只有当下游声明 `rd_en`（我准备好收）时，上游才应声明 `valid`——这是 AXI-Stream 式双向握手的正确语义，避免下游没准备好吃进半截。

## 5. 综合实践

把本讲所有要点串起来，完成下面这个「端口映射 + 优先级辨析」任务。这是本讲规格里指定的核心实践。

### 实践目标

对照 `pcileech_fifo.sv` 中 `i_pcileech_mux` 的真实例化，厘清四类数据各接哪个端口、`tag` 是多少，并**用代码事实**回答「TLP 的优先级到底高不高」这个容易混淆的问题。

### 操作步骤

1. **读例化代码**：打开 [PCIeSquirrel/src/pcileech_fifo.sv:146-201](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L146-L201)。
2. **填映射表**：

| 端口 | 数据来源 | tag | 是否使用 |
| --- | --- | --- | --- |
| p0 | ? | ? | ? |
| p1 | ? | ? | ? |
| p2 | ? | ? | ? |
| p3 | ? | ? | ? |
| p4 | ? | ? | ? |
| p5 | ? | ? | ? |
| p6 | ? | ? | ? |
| p7 | ? | ? | ? |
3. **辨析优先级**：再读紧邻例化上方的 ASCII 注释图 [pcileech_fifo.sv:84-99](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L84-L99)。注释声称「1st priority PCIe TLP，2nd PCIe CFG，3rd Loopback，4th Command」。把它和你刚填的映射表对照，找出**注释与代码不一致**的地方。
4. **解释 TLP 占 4 个端口的原因**：结合 4.1 的「合流打包」思想，说明 TLP 占 p3~p6 四个端口究竟是为了「优先级」还是为了「带宽」。

### 需要观察的现象

- 注释画的优先级顺序（TLP 最高）与代码端口号顺序（p0=Loopback 才是最小端口号=最高优先级）**恰好相反**。这是一个典型的「注释过时、以代码为准」的案例。
- TLP 占 4 个端口，与 `dtlp.rx_data[0..3]` 四路并行接收一一对应。

### 预期结果（参考答案）

**映射表**：

| 端口 | 数据来源 | tag | 使用 |
| --- | --- | --- | --- |
| p0 | Loopback（`_loop_dout`） | 10 | 是 |
| p1 | Command（`_cmd_tx_dout`） | 11 | 是 |
| p2 | PCIe CFG（`dcfg.rx_data`） | 01 | 是 |
| p3 | TLP#0（`dtlp.rx_data[0]`） | 00 | 是 |
| p4 | TLP#1（`dtlp.rx_data[1]`） | 00 | 是 |
| p5 | TLP#2（`dtlp.rx_data[2]`） | 00 | 是 |
| p6 | TLP#3（`dtlp.rx_data[3]`） | 00 | 是 |
| p7 | 未接（`p7_wr_en = 1'b0`） | 11 | 否 |

**关于「TLP 优先级最高」的辨析**：

- **按索引链的延迟优先级看，TLP 反而是最低的**：端口号越小、槽位越靠前、饱和时越优先发出。代码里 p0=Loopback、p1=Command、p2=CFG 才是优先级最高的三个；TLP 在 p3~p6，发生饱和（一拍 >7 字）时反而是最先被「延到下一拍」的。
- **TLP 占 4 个端口是为带宽，不是为延迟**：DMA 读返回的内存数据（CplD 型 TLP）是整个系统最海量的上行流量。给它 4 个端口，意味着一个 256 位包里最多可有 4 个字是 TLP，对应 `dtlp.rx_data[0..3]` 的 4 路并行接收。这是**吞吐量分配**，与「谁先发」的延迟优先级是两回事。
- **ASCII 注释已过时**：[pcileech_fifo.sv:86-99](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L86-L99) 的注释还停留在「TLP 1st」的旧布局，与现行例化代码矛盾。读源码时**以代码为准**，注释只作历史参考。

> 这个练习最重要的收获不是「记住谁优先」，而是养成「**注释会过时、代码才是事实**」的源码阅读习惯。当你看到注释和代码冲突时，永远相信代码，并顺手记下这个分歧点。

## 6. 本讲小结

- `pcileech_mux` 不是普通「多选一」仲裁器，而是**合流打包器**：每拍把所有就绪的 32 位字尽量塞进同一个 256 位包（1 状态字 + 7 数据字）一起发，带宽利用率上限 \( 224/256 = 87.5\% \)。
- 优先级由一条**前缀和加法链** `p0_idx → p8_idx` 实现：端口号小 → 槽位小 → 饱和时优先发出。所有端口每拍都被采样，非饱和时无竞争。
- 每个数据字带 4 位标签 `{ctx, tag}`：`tag` 标大类（与接收方向 MAGIC type 完全对称：TLP=00/CFG=01/LOOP=10/CMD=11），`ctx` 标子流元数据（如 TLP 的 `{first, last}` 包边界）。
- **空闲端口 `p8`** 是内部虚拟端口：长时间无新数据且仍有残包时，注入 `0xffffffff` 填充字把残包强行凑满 7 字冲出，避免「尾部残包死锁」。
- **输出缓冲 `dout_buf`** 是一拍深度的影子寄存器：下游反压时暂存已生成的包，配合 `idx_base` 在 `en=0` 时暂停推进，保证连续反压也不丢数据。
- 实际例化中 TLP 占 p3~p6 共 4 个端口，是**带宽分配**（匹配 4 路 `dtlp.rx_data`），而非延迟优先级最高；fifo 里的 ASCII 优先级注释已过时，以代码为准。

## 7. 下一步学习建议

本讲把「上行回主机」方向的合流打包机制讲透了。建议接下来：

- **横向对照接收方向**：回到 [u2-l3](u2-l3-fifo-control-and-magic-routing.md)，把本讲的「发送 tag 对称于接收 type」和 4.3 节的映射表，与接收方向的 `CHECK_MAGIC`/`CHECK_TYPE_*` 宏互相印证，形成「主机下发 → fifo 分流 → PCIe 处理 → fifo 合流 → 主机上送」的完整闭环理解。
- **进入命令寄存器层**：本讲多次提到 Command 端口（p1）承载命令响应，下一步可学 [u2-l5 命令/控制寄存器文件与读写协议](u2-l5-command-register-file.md)，看 `_cmd_tx_din` 里那些响应（读回值、影子配置空间响应、不活动计时器）是怎么一格一格填进去的。
- **追踪 TLP 的来源**：本讲把 TLP 当作「p3~p6 的输入」黑盒处理。后续 [u3 PCIe 核心与 TLP 处理](u3-l3-tlp-handling-overview.md) 单元会打开这个黑盒，讲 `dtlp.rx_data[0..3]` 这 4 路 TLP 是怎么从 PCIe 核心一路过滤、桥接送过来的。
- **想动手验证时序**：若你有 Vivado 仿真环境，可给 `pcileech_mux` 写一个简单 testbench，构造「多端口同时就绪」「下游间歇反压」「尾部残包」三种场景，观察 `dout`/`valid`/`dout_buf_valid` 波形，验证本讲 4.2/4.4/4.5 的推演。
