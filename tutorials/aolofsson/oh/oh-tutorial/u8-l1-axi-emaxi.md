# AXI 协议与 emaxi 主桥

> 本讲是第 8 单元（AXI、DMA 与多链路系统）的第一讲，承接 [u5-l1 emesh 包格式与协议](u5-l1-emesh-packet.md)。你已经熟悉 emesh 的 104 位事务包与 `access`/`wait` 握手；本讲把这套片上协议「翻译」成工业总线 **AXI4**，并拆解 `emaxi` 模块如何充当这个翻译官。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 AXI4 的**五个独立通道**（写地址 AW、写数据 W、写响应 B、读地址 AR、读数据 R）以及 `valid`/`ready` 握手规则。
- 理解 `emaxi` 在 elink 系统中的角色：把**进入** elink 的 emesh 事务变成**发出**的 AXI 主端口事务，再把 AXI 读回数据变回 emesh 读响应包。
- 读懂 `emaxi.v` 的通道生成逻辑，特别是 emesh 字段（`dstaddr`/`data`/`datamode`/`srcaddr`）与 AXI 信号（`AWADDR`/`WDATA`/`AWSIZE`/`WSTRB`…）之间的逐项映射。

## 2. 前置知识

### 2.1 什么是 AXI，为什么需要它

emesh 是 OH! 自定义的片上网络协议（定长 104 位包），在 Epiphany 芯片与 elink 链路内部使用。但 FPGA 这一侧（例如 Xilinx Zynq 的 PS 端、或普通 SoC）说的是另一套「官方语言」——**AXI（Advanced eXtensible Interface）**，由 ARM 定义、是当今 SoC 事实标准。要让 FPGA 上的 ARM 核能读写 Epiphany 芯片里的内存，就必须有一个**桥（bridge）**在 emesh 与 AXI 之间双向翻译。

AXI 的核心特征是**多通道、点对点、valid/ready 握手**：

- **通道（channel）**：把一次事务拆成若干独立的信息流，每个通道有自己的 `valid`/`ready`。读写**地址**分开走，写有**响应**、读数据自带响应。
- **主从（master/slave）**：发起方叫 master（源），应答方叫 slave（宿）。`emaxi` 是一个 **AXI master**——它代表 elink 主动去读写外部的 AXI slave（比如 DDR 内存）。
- **突发（burst）**：一次地址事务可以搬运一串连续数据，长度由 `len`、宽度由 `size` 描述。

> 关键直觉：emesh 把「读/写一条」压缩进一个 104 位包；AXI 把同样的事务摊开成「地址通道 + 数据通道 + 响应通道」，每条通道各自握手。`emaxi` 的工作就是**把压扁的包重新摊开**（emesh→AXI），以及把读回来的数据**重新压扁**（AXI→emesh）。

### 2.2 你需要回忆的 emesh 知识（来自 u5-l1）

emesh 包（AW=32 时 PW=104 位）字段从低到高：

| 字段 | 比特位 | 含义 |
|------|--------|------|
| `write` | [0] | 1=写事务，0=读 |
| `datamode[1:0]` | [2:1] | 数据宽度：00=8b, 01=16b, 10=32b, 11=64b |
| `ctrlmode[3:0]` | [6:3] | Epiphany 专用控制模式 |
| `dstaddr[31:0]` | [39:8] | 目标地址 |
| `data[31:0]` | [71:40] | 写数据 / 读响应数据 |
| `srcaddr[31:0]` | [103:72] | 读请求的回信地址；64 位写时作高 32 位数据 |

握手用 `access`（≈valid）与 `wait`（高有效反压，`~wait`≈ready）。这些是本讲字段的来源，后文不再重复解释。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [axi/hdl/emaxi.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/emaxi.v) | 本讲主角。AXI 主桥，左侧接 emesh 三通道（wr/rd/rr），右侧接 AXI4 master 五通道。 |
| [axi/hdl/esaxi.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v) | emaxi 的对偶：AXI **从**桥（外部 AXI master 经它访问 elink）。本讲只在「架构位置」处提及，细节留待 u8-l2。 |
| [axi/dv/aximaster_stub.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/dv/aximaster_stub.v) | 仿真用的 AXI master 接口桩，列全了所有 AXI 信号，可当「信号清单速查表」。 |
| [elink/README.md](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md) | emesh 包格式权威表 + elink 设计结构图（emaxi 的位置）。 |
| [elink/hdl/axi_elink.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/axi_elink.v) | elink 顶层，实际**实例化** emaxi 的地方，给出真实的连线对照。 |

## 4. 核心概念与源码讲解

本讲拆三个最小模块：**AXI 协议** → **emaxi 主桥架构** → **通道映射**。

### 4.1 AXI 协议要点：五通道与 valid/ready 握手

#### 4.1.1 概念说明

AXI4 把一次总线事务拆成**五个独立通道**，每个通道都是单向的「源→宿」数据流，各自带一对 `valid`/`ready` 握手信号：

| 通道 | 方向 | 携带内容 | emaxi 端口前缀 |
|------|------|----------|----------------|
| **写地址 AW**（Write Address） | master→slave | 写地址 + 突发控制（`len`/`size`/`burst`） | `m_axi_aw*` |
| **写数据 W**（Write Data） | master→slave | 写数据 + 字节掩码 `wstrb` + `wlast` | `m_axi_w*` |
| **写响应 B**（Write Response） | slave→master | 写完成状态 `bresp` | `m_axi_b*` |
| **读地址 AR**（Read Address） | master→slave | 读地址 + 突发控制 | `m_axi_ar*` |
| **读数据 R**（Read Data） | slave→master | 读数据 + `rresp` + `rlast` | `m_axi_r*` |

一次**写事务**会依次走 AW、W、B 三个通道；一次**读事务**会走 AR、R 两个通道。读写彼此完全独立、可并发，这是 AXI 区别于旧总线（如 APB/AVALON 单通道）的核心优势。

#### 4.1.2 核心流程

**valid/ready 握手规则**（AXI 的铁律，emaxi 顶部注释也总结了）：

1. **双方都拉高才算一拍**：某通道在时钟上升沿采样到 `Xvalid & Xready` 同时为 1，则这一拍的信息有效，称为一次 *transfer*。
2. **valid 不许等 ready**：源端一旦有数据就必须拉高 `valid`，**不允许**为了等 `ready` 而压着 `valid` 不拉。
3. **ready 可以等 valid**：宿端**允许**先看到 `valid` 再决定要不要拉 `ready`。
4. **valid 一旦拉高须保持**：在握手成功（`valid & ready`）之前，`valid`、地址、数据、控制信号都必须保持不变。
5. **禁止组合环**：接口的输入与输出之间不能存在组合逻辑路径（否则握手机制会形成组合环，导致死锁或时序崩溃）。

**突发参数**：

- `size`（3 位）：每拍的字节数为 \(2^{\text{size}}\)。例如 `size=2` 表示每拍 4 字节。
- `len`（8 位）：一次突发的拍数 = `len+1`。`len=0` 即单拍（单次事务）。
- `burst`（2 位）：`01`=INCR（地址递增，最常用）。

#### 4.1.3 源码精读

`emaxi.v` 顶部注释把上述 AXI 要点浓缩成一份清单，是阅读本模块的「规则板」：

[emaxi.v:6-33](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/emaxi.v#L6-L33) —— 顶部 NOTES，逐条列出五通道划分、valid/ready 规则、`AWREADY`/`WREADY` 默认电平建议、`WLAST` 语义。其中两条特别值得注意：`--valid is asserted uncondotionally`（valid 无条件拉高，对应规则 2）与 `--there can be no combinatorial path between input and output of interface`（对应规则 5）。

五通道的端口声明集中在 AXI master 接口段：

[emaxi.v:83-129](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/emaxi.v#L83-L129) —— 这是认识 AXI 信号最好的速查表。例如写地址通道有 `m_axi_awaddr[31:0]`/`m_axi_awlen[7:0]`/`m_axi_awsize[2:0]`/`m_axi_awvalid`/`m_axi_awready`；读数据通道有 `m_axi_rdata[63:0]`/`m_axi_rresp[1:0]`/`m_axi_rlast`。注意 AXI 数据总线是 **64 位**（`m_axi_wdata[63:0]`、`m_axi_rdata[63:0]`），而 emesh 数据只有 32 位——这正是 4.3 节「对齐」要解决的问题。

> 旁证：[aximaster_stub.v:24-70](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/dv/aximaster_stub.v#L24-L70) 是仿真桩，把 AXI master 的全部信号按通道分组原样列出，可与 emaxi 端口一一对照。

#### 4.1.4 代码实践

**实践目标**：用源码注释自检对 AXI 握手的理解。

**操作步骤**：

1. 打开 [emaxi.v:6-33](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/emaxi.v#L6-L33)。
2. 找到这两句注释：`--source is not allowed to wait for READY to assert VALID` 与 `--destination is permitted to wait for valud before asserting READY`。
3. 把它们对应到 4.1.2 的规则 2 与规则 3。

**需要观察的现象**：注意注释里有一句带问号的 `--AWVALID must remain asserted until the rising clock edge after slave asserts AWREADY??`——作者自己也留了疑问，说明这是真实工程代码（而非教科书），存在不确定处。

**预期结果**：你能用自己的话讲清「valid 不能等 ready、ready 可以等 valid」这一不对称性，以及它为何能避免组合环。具体硬件行为**待本地仿真验证**。

#### 4.1.5 小练习与答案

**练习 1**：一次 AXI 写事务最少要经过哪几个通道？读事务呢？

> **答案**：写事务走 AW（写地址）→ W（写数据）→ B（写响应）三个通道；读事务走 AR（读地址）→ R（读数据）两个通道。读写地址通道彼此独立。

**练习 2**：`size` 字段为 3 时，每拍传输多少字节？

> **答案**：每拍 \(2^{3}=8\) 字节（64 位）。

---

### 4.2 emaxi 主桥：整体架构与在 elink 中的角色

#### 4.2.1 概念说明

`emaxi` 是一个 **AXI 主桥（master bridge）**。它的左边是三个 emesh 通道（沿用 OH! 全项目的 emesh 接口风格），右边是 AXI4 master 五通道：

- **左侧 emesh 输入**：
  - `wr_*`（write request，写请求）：来自链路对端、要写出去的 emesh 写包。
  - `rd_*`（read request，读请求）：要发出的 emesh 读请求包。
  - `rr_*`（read response，读响应）：**输出**，把读回来的数据组装成 emesh 包送回链路。
- **右侧 AXI master 输出**：把上述 emesh 事务翻译成对外部 AXI slave（如 DDR）的访问。

关键认知是**方向**：emaxi 代表 elink 这一方，**主动去读写外部 AXI slave**。所以「写请求 emesh 包」进来后，emaxi 在 AXI 侧发起一次写（驱动 `AW`/`W` 通道）；「读请求 emesh 包」进来后，emaxi 在 AXI 侧发起一次读（驱动 `AR` 通道），等 `R` 通道读回数据，再装配成 `rr` 包返回。

#### 4.2.2 核心流程

emaxi 内部数据流可概括为三条路径：

```
写路径:  wr_packet(104b) ──解包──> wr_dstaddr/data/datamode
                                       │
                            ┌──────────┴──────────┐
                            ▼                     ▼
                       AW 通道(地址)          W 通道(数据+wstrb)
                            └──────────┬──────────┘
                                       ▼
                              (B 通道: 写响应, 本模块基本忽略)

读请求路径:  rd_packet(104b) ──解包──> rd_dstaddr(→ARADDR), rd_srcaddr(回信地址)
                                                  │
                                          存入 readinfo FIFO
                                                  ▼
                                       AR 通道(地址)

读响应路径:  R 通道(rdata) ──对齐──> rr_data
             readinfo FIFO ──出队──> rr_dstaddr/datamode/ctrlmode
                                  │
                            └─────┴──────┐
                            ▼            ▼
                       emesh2packet   rr_packet(104b) ──> 送回链路
```

两个细节决定了这条流水线的形状：

1. **读请求要「记账」**：AXI 读是异步的——发出去地址后，数据可能很多拍后才回来，且可能多个读请求乱序返回。emaxi 用一个 **FIFO** 把每个读请求的元信息（回信地址 `srcaddr`、宽度、控制模式）存起来，等数据回来时按顺序（假设保序）取出来重组响应包。
2. **写无需记账**：AXI 写有显式的 B 响应通道，且本模块把写当「即发即忘」（注释 `--there is no acknowledge on write, treated as buffered`），所以写路径不需要类似 FIFO。

#### 4.2.3 源码精读

模块声明与参数：

[emaxi.v:36-55](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/emaxi.v#L36-L55) —— 模块名 `emaxi`，关键参数：`M_IDW=12`（AXI ID 宽度）、`PW=104`（emesh 包宽）、`AW=32`（地址宽）、`DW=32`（emesh 数据宽）。注意 **DW=32**，而 AXI 数据是 64 位，差出的 32 位由对齐电路处理。

左侧 emesh 三通道接口：

[emaxi.v:57-74](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/emaxi.v#L57-L74) —— 写请求（`wr_access`/`wr_packet`/`wr_wait`）、读请求（`rd_access`/`rd_packet`/`rd_wait`）、读响应（`rr_access`/`rr_packet`/`rr_wait`）。这正是 OH! 全项目统一的 emesh 接口三件套（参见 u5-l1）。

包 ⇄ 字段的解包/打包用了两个辅助模块：

[emaxi.v:198-234](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/emaxi.v#L198-L234) —— 实例化 `packet2emesh`（包→字段，用于 wr/rd 输入解包）与 `emesh2packet`（字段→包，用于 rr 输出打包）。读响应打包时 `.write_out(1'b1)`——读响应包在 emesh 里也用 write=1 标记（因为它携带数据，与 u5-l3 emesh_readback 一致）。

> ⚠️ **工程现实**：全仓库搜索不到 `packet2emesh` 与 `emesh2packet` 的模块定义（参见本系列 [u6-l2](u6-l2-gpio-module.md)、[u7-l2](u7-l2-etx-pipeline.md) 都提到过同样的缺失）。这意味着 **emaxi.v 不能原样编译**——这些是预期的「emesh 工具库」组件，在当前仓库中未提供实现。阅读与理解不受影响，但若要仿真必须自行补桩或用 `emesh_unpack`/`emesh_pack`（见 emesh/hdl）替代。

emaxi 在系统中的真实位置——看 elink 顶层如何实例化它：

[axi_elink.v:314-359](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/axi_elink.v#L314-L359) —— 这里用 verilog-mode 的 `AUTO_TEMPLATE` 把 emaxi 的 `wr_*` 接到 elink 的 `rxwr_*`（链路 RX 写通道）、`rd_*` 接到 `rxrd_*`、`rr_*` 接到 `txrr_*`（链路 TX 读响应通道）。即：**从链路收到的写/读请求，经 emaxi 翻译成对外部 AXI 的访问；读回的数据再经 emaxi 送回链路**。这印证了 4.2.1 对数据方向的判断。

elink README 的设计结构图也标注了同一关系：

[elink/README.md:137-172](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L137-L172) —— `elink |----emaxi (AXI master interface)` 与 `|----esaxi (AXI slave interface)`，两个 AXI 桥互为对偶。

#### 4.2.4 代码实践

**实践目标**：在真实顶层中验证 emaxi 的「数据方向」。

**操作步骤**：

1. 打开 [axi_elink.v:314-360](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/axi_elink.v#L314-L360)。
2. 对照 `AUTO_TEMPLATE` 的三行映射规则：`.rr_\(.*\) (txrr_\1)`、`.rd_\(.*\) (rxrd_\1)`、`.wr_\(.*\) (rxwr_\1)`。
3. 回答：emaxi 的 `wr_access`（输入）实际连到哪个网络？方向是 emaxi 收还是发？

**需要观察的现象**：注意 `wr_wait` 是 emaxi 的**输出**（连到 `rxwr_wait`），而 `wr_access`/`wr_packet` 是输入。这与 emesh 接口约定一致：master 端收 `access`/`packet`、发 `wait`。

**预期结果**：`wr_access` 连到 `rxwr_access`——即 elink 从链路 RX 侧收到的写事务，灌给 emaxi 去发起 AXI 写。**待本地仿真验证**完整事务。

#### 4.2.5 小练习与答案

**练习 1**：为什么读路径需要一个 FIFO，而写路径不需要？

> **答案**：AXI 读是异步的——地址发出后数据要等若干拍才返回，且 emesh 读请求里携带的「回信地址 `srcaddr`」必须在数据回来时重新塞进响应包。FIFO 用来暂存每个未完成读请求的元信息（`srcaddr`/`datamode`/`ctrlmode`/地址低位），待数据返回时重组。写路径有专门的 B 响应通道且本模块按「即发即忘」处理，故无需记账。

**练习 2**：`emesh2packet` 实例化时为什么把 `.write_out` 固定接 `1'b1`？

> **答案**：读响应包（`rr_*`）携带数据，在 emesh 协议中凡携带数据的事务 `write` 位都置 1（与 u5-l3 emesh_readback 装配读响应时的处理一致），所以打包时 write 恒为 1。

---

### 4.3 通道映射：emesh ↔ AXI 字段翻译

#### 4.3.1 概念说明

本模块是 emaxi 的核心肉身：把 emesh 包的每个字段精确地搬到对应的 AXI 信号上。映射关系总体很直观，但有两处需要专门电路：

- **地址**：`dstaddr` → `AWADDR`/`ARADDR`，几乎直通。
- **宽度**：`datamode[1:0]` → `AWSIZE`/`ARSIZE`，**数值上恰好相等**（见下）。
- **写数据对齐**：emesh 数据是 32 位、右对齐；AXI 数据总线是 64 位。需要把数据**广播/拼装**到 64 位总线的正确字节通道，并用 `wstrb` 指明哪些字节真正有效。
- **读数据对齐**：反过来，把 64 位 AXI 读数据中**正确的那一段**抽取出来、右对齐到 32 位 `rr_data`。

#### 4.3.2 核心流程

**宽度映射的数学关系**：emesh 的 `datamode[1:0]` 与 AXI 的 `size` 字段都按「每拍字节数」编码，且数值相等：

\[
\text{每拍字节数} = 2^{\text{datamode}} = 2^{\text{size}},\qquad \text{size} = \text{datamode}
\]

| datamode | 宽度 | 字节数 | AXI size | AW/ARSIZE 取值 |
|----------|------|--------|----------|----------------|
| 00 | 8b  | 1 | 0 | `{1'b0, 2'b00}` = 0 |
| 01 | 16b | 2 | 1 | `{1'b0, 2'b01}` = 1 |
| 10 | 32b | 4 | 2 | `{1'b0, 2'b10}` = 2 |
| 11 | 64b | 8 | 3 | `{1'b0, 2'b11}` = 3 |

所以代码里 `m_axi_awsize = {1'b0, wr_datamode[1:0]}`——高位补 0，低位直接用 datamode，干净利落。

**写数据广播 + 字节掩码**（这是 emaxi 的一个设计选择）：对 8/16/32 位写，它不去做复杂的左移对齐，而是把数据**复制广播**到整个 64 位总线（字节写时复制 8 份、半字 4 份、字 2 份），同时用 `wstrb`（每 8 位数据 1 个 strobe 位，共 8 位）指出只有某几个字节是真的。AXI slave 只会写 strobe 为 1 的字节，其余被忽略，效果等价于精确对齐。

对 64 位写（datamode=11），emesh 的 32 位 `data` 装不下，于是用 `srcaddr` 作高 32 位（呼应 u5-l1：srcaddr 在写事务时兼作高 32 位数据）：`{srcaddr[31:0], data[31:0]}`。

**读数据抽取**：读回的 64 位 `rdata` 中，目标字节位置由请求时的地址低位决定。emaxi 用两级 `case`（先按 datamode、再按地址低位）把正确的字节切到 `rr_data[31:0]` 的最低位（右对齐），因为 Epiphany 侧要求所有读数据右对齐（代码注释明示）。

#### 4.3.3 源码精读

先看一批被「写死」的 AXI 控制信号（emaxi 不支持的 AXI 特性）：

[emaxi.v:236-260](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/emaxi.v#L236-L260) —— `m_axi_awid`/`m_axi_arid`（ID）全 0；`m_axi_awburst`/`m_axi_arburst = 2'b01`（只支持 INCR 递增突发）；`m_axi_bready = 1'b1`（永远接受写响应，基本忽略 B 通道）；`m_axi_wid` 全 0。这告诉我们：emaxi 用的是 AXI **简化子集**，所有事务当作 ID=0、单 beat、INCR。

**写地址通道**：

[emaxi.v:262-315](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/emaxi.v#L262-L315) —— 关键映射：`m_axi_awaddr <= wr_dstaddr`（地址直通）、`m_axi_awsize <= {1'b0, wr_datamode[1:0]}`（宽度映射，见上式）、`m_axi_awlen <= 8'b0`（单拍突发）。这段还实现了 `awvalid_b`（b=buffer）一级缓冲：当 AXI 侧暂未 ready、而 emesh 侧又来了新写请求时，把第二笔请求暂存进 `awvalid_b`/`awaddr_b`/`awsize_b`，避免反压丢包。`wr_wait = awvalid_b | wvalid_b` 即「缓冲满则向 emesh 侧反压」。

**写数据对齐电路**（广播 + 掩码）：

[emaxi.v:321-358](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/emaxi.v#L321-L358) —— 第一个 `case` 算 `wdata_aligned`：8b 时 `{8{wr_data[7:0]}}`（复制 8 份），16b 时 `{4{wr_data[15:0]}}`，32b 时 `{2{wr_data[31:0]}}`，64b 时 `{wr_srcaddr, wr_data}`。第二个嵌套 `case` 算 `wstrb_aligned`：按地址低位点亮对应字节。例如字节写、地址 `[2:0]=3` 时 `wstrb=8'h08`（只写第 3 字节）；字写、`addr[2]=1` 时 `wstrb=8'hf0`（写高 4 字节）。

**写数据通道**：

[emaxi.v:364-403](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/emaxi.v#L364-L403) —— 把 `wdata_aligned`/`wstrb_aligned` 推到 `m_axi_wdata`/`m_axi_wstrb`，结构与写地址通道对称（也有 `wvalid_b` 一级缓冲）。注意 [第 370 行](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/emaxi.v#L370) `m_axi_wlast <= 1'b1` 并附 `// TODO:bursts!!`——目前每个写都是单拍、恒 last，突发未真正实现。

**读地址通道**（很简洁，纯组合）：

[emaxi.v:449-459](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/emaxi.v#L449-L459) —— `m_axi_araddr = rd_dstaddr`（地址直通）、`m_axi_arsize = {1'b0, rd_datamode}`、`m_axi_arlen = 0`（单拍）、`m_axi_arvalid = rd_access & ~fifo_prog_full`。`fifo_wr_en` 在 AR 握手成功时把请求元信息存进记账 FIFO；`rd_wait = ~arready | prog_full` 向 emesh 侧反压。

读请求的「记账」内容（打包成 41 位存入 FIFO）：

[emaxi.v:413-447](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/emaxi.v#L413-L447) —— `readinfo_in = {rd_srcaddr, rd_dstaddr[2:0], rd_ctrlmode[3:0], rd_datamode[1:0]}`，存的是回信地址、地址低 3 位（对齐用）、控制模式与宽度。FIFO 实例名是 `fifo_async`，但模块是 `oh_fifo_sync`（DW=104、DEPTH=32）——**实例名与类型不一致**（命名遗留），以源码为准。

**读响应通道**（数据抽取/右对齐）：

[emaxi.v:464-523](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/emaxi.v#L464-L523) —— 先 `m_axi_rready = ~rr_wait`（链路侧反压则不收 AXI 读数据）；然后把 `m_axi_rdata` 打一拍进 `m_axi_rdata_fifo`，与 FIFO 出队的元信息对齐，最后用嵌套 `case` 抽取：字节读时按 `alignaddr[2:0]` 选 8 个字节之一并补 0 到 32 位；半字读按 `[2:1]` 选；字读按 `[2]` 选高/低字；双字读把低 32 位放 `rr_data`、高 32 位放 `rr_srcaddr`。

> ⚠️ 注意 [第 456-464 行](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/emaxi.v#L456-L464) 有多处 `//BUG` 注释（例如 `m_axi_arvalid` 行删掉了 `~rr_wait` 项、`m_axi_rready` 标了 `BUG!: 1'b1`）。作者明确标记了对反压处理的修订与存疑处。阅读时把这些注释当作「设计笔记」，最终行为**待本地仿真确认**。

#### 4.3.4 代码实践

**实践目标**：亲手把 emesh 字段对应到 AXI 信号，完成一张映射表（本讲指定实践任务）。

**操作步骤**：

1. 准备一张三列表格：`emesh 字段` | `AXI 信号` | `源码位置/说明`。
2. 逐行填写。下面给出前几行作为示范，其余自行补全：

   | emesh 字段 | AXI 信号 | 源码位置 / 说明 |
   |------------|----------|-----------------|
   | `wr_dstaddr` | `m_axi_awaddr` | emaxi.v:298 地址直通 |
   | `wr_datamode[1:0]` | `m_axi_awsize[2:0]` | emaxi.v:300，`{1'b0,datamode}` |
   | `wr_data` | `m_axi_wdata`（经广播对齐） | emaxi.v:323-326，复制到 64 位 |
   | (由 datamode+dstaddr 推导) | `m_axi_wstrb` | emaxi.v:332-356，字节掩码 |
   | `wr_access`(经握手) | `m_axi_awvalid`/`m_axi_wvalid` | emaxi.v:297、387 |

3. 继续补全 `rd_dstaddr → m_axi_araddr`、`rd_datamode → m_axi_arsize`、`m_axi_rdata → rr_data`（经右对齐）、`rd_srcaddr → rr_dstaddr`（回信地址变成响应目标地址）等行。
4. 对照 [emaxi.v:453-455](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/emaxi.v#L453-L455) 与 [emaxi.v:488-521](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/emaxi.v#L488-L521) 校对。

**需要观察的现象**：注意有些映射不是「字段直搬」，而是**需要电路生成**的（`wstrb`、对齐后的 `wdata`/`rdata`），有些是**字段身份切换**的（读请求里的 `srcaddr` 在响应里变成 `dstaddr`）。

**预期结果**：得到一张完整映射表，能据此解释「一个 emesh 写包进来后，AW/W 通道上各信号分别取什么值」。无需运行即可完成；如要验证，**待本地仿真**（需先补 `packet2emesh`/`emesh2packet` 桩）。

#### 4.3.5 小练习与答案

**练习 1**：一次 datamode=10（32 位）、`dstaddr=0x80800004` 的 emesh 写，到达 AXI 侧时 `wstrb` 是多少？

> **答案**：字写（datamode=10）按 `addr[2]` 选掩码。`0x80800004` 的 `[2]=1`，故 `wstrb=8'hf0`（写 64 位总线的高 4 字节），`wdata` 是 `{2{data[31:0]}` 的复制。

**练习 2**：为什么读响应里 `rr_dstaddr` 取的是「请求时的 `rd_srcaddr`」？

> **答案**：emesh 的 `srcaddr` 是读请求的**回信地址**——请求方把自己的地址写在 srcaddr 里，要求「把数据回送到这个地址」。读响应包的 `dstaddr` 就是要送达的目标，因此正好等于请求里的 `srcaddr`。emaxi 在读请求时把 `rd_srcaddr` 存入记账 FIFO，响应时取出填入 `rr_dstaddr`。

**练习 3**：`m_axi_awlen` 和 `m_axi_arlen` 都固定为 0，意味着什么？

> **答案**：`len=0` 表示突发长度为 1（单拍事务）。即 emaxi 把每个 emesh 事务翻译成**单 beat** 的 AXI 事务，不做多拍突发（`wlast` 因此恒为 1）。

## 5. 综合实践

**任务**：跟踪一个完整的「32 位写 + 32 位读」事务，画出它穿过 emaxi 的全字段变换链。

请用 elink README 里的测试样例作为输入（[elink/README.md:507-516](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L507-L516)），例如这一行写事务（`srcaddr_datahi_data_datalo_dstaddr_ctrlmode` 格式）：

```
AAAAAAAA_11111111_80800000_05_0010   // 32 位写到 0x80800000
```

要求：

1. **写事务**：拆出 `dstaddr=0x80800000`、`data=0x11111111`、`datamode=10`（32b）、`write=1`。然后填写它进入 emaxi 后，AXI 写地址通道（`AWADDR`/`AWSIZE`/`AWLEN`/`AWVALID`）、写数据通道（`WDATA`/`WSTRB`/`WLAST`）各为何值。注意 `WDATA` 要写出广播后的 64 位值。
2. **读事务**：取样例里的 `810D0000_DEADBEEF_80800000_04_0010`（32 位读）。说明 `ARADDR`/`ARSIZE` 取值，记账 FIFO 里存了什么（特别是 `srcaddr=0x810D0000`），以及当 AXI 侧读回某个 `RDATA` 时，`rr_packet` 里的 `dstaddr` 字段为何变成 `0x810D0000`。
3. 画出两条路径的框图（输入 emesh 包 → 解包 → AXI 通道；AXI R 通道 → 对齐 + FIFO 出队 → emesh 响应包）。

**验收**：你能向同伴讲清「emesh 的 `srcaddr` 在写时是高 32 位数据、在读请求时是回信地址」这一字段双重身份，以及 emaxi 用什么电路处理它（写对齐的 default 分支 vs 读 FIFO 记账）。完整时序**待本地仿真验证**。

## 6. 本讲小结

- AXI4 用**五个独立通道**（AW/W/B/AR/R）和 **valid/ready 握手**组织事务；铁律是「valid 不许等 ready、ready 可以等 valid」「接口禁止组合环」，`emaxi.v` 顶部注释是这套规则的速查板。
- `emaxi` 是 **AXI 主桥**：把 elink 从链路收到的 emesh 写/读请求翻译成对外部 AXI slave 的访问，读回的数据再装配成 emesh 读响应包送回链路；它在 `axi_elink.v` 中的连线印证了这一方向。
- 字段映射总体直观：`dstaddr→A*ADDR`、`datamode→A*SIZE`（数值相等 `size=datamode`）、`data→WDATA`/`RDATA`；两处需要专门电路——**写数据广播 + `wstrb` 掩码**、**读数据按地址低位右对齐抽取**。
- 读路径用 `oh_fifo_sync`（实例名误叫 `fifo_async`）做**未完成读请求的记账**，关键是把回信地址 `srcaddr` 存起来，响应时变成 `rr_dstaddr`。
- emaxi 用的是 AXI **简化子集**：ID=0、`len=0`（单拍）、`burst=INCR`、`wlast` 恒 1、`bready` 恒 1，突发未真正实现（`//TODO:bursts!!`）。
- **工程现实**：`packet2emesh`/`emesh2packet` 在仓库中无定义，emaxi 无法原样编译；读响应段还留有多处 `//BUG` 注释——一律以源码实际文本为准，关键行为待本地仿真确认。

## 7. 下一步学习建议

- 下一篇 [u8-l2 esaxi 从桥与 axi_elink 桥接](u8-l2-esaxi-axi-elink.md) 讲解 emaxi 的对偶 `esaxi`（AXI slave），以及 `axi_elink.v` 如何把 emaxi/esaxi 与 elink 链路整体拼成端到端通路。建议先重读本讲的 4.2.3（axi_elink 实例化片段），带着「master 桥已懂、slave 桥如何反向」的问题进入。
- 若想巩固 AXI 握手细节，可先读 [aximaster_stub.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/dv/aximaster_stub.v) 与 [axislave_stub.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/dv/axislave_stub.v)，对照两套信号的异同。
- 继续深入前，建议复习 [u5-l1](u5-l1-emesh-packet.md)（emesh 包字段）与 [u5-l3](u5-l3-pack-unpack-readback.md)（pack/unpack/readback），它们是本讲字段映射的协议基础。
