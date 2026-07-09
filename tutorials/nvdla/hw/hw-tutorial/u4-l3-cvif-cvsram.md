# CVIF：卷积 SRAM（CVSRAM）接口

> 承接：本讲是存储接口子系统（单元 4）的第三篇。u4-l1 给出了「MCIF/CVIF 双接口 + IG/cq/eg 三级」的整体架构，u4-l2 深读了 MCIF（主存接口）的源码。本讲聚焦 **CVIF**——结构上与 MCIF 几乎一模一样，但终点是片上 CVSRAM 的「二级存储接口」。我们会大量对比 MCIF，避免重复 u4-l2 已讲过的细节，把篇幅集中在「二者为何同构、差异在哪、CVSRAM 该放什么数据」。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 CVIF 在 NVDLA 存储体系中的定位：它是面向**片上 CVSRAM** 的二级（secondary）存储接口，与面向片外主存 DBB 的 MCIF 并列。
- 在源码层面确认 CVIF_read / CVIF_write 与 MCIF_read / MCIF_write 的 **IG→cq→eg 三级同构**，并指出它们各自挂多少个客户端。
- 解释 CVIF 与 MCIF 为何能共享 `NV_NVDLA_XXIF_libs.v` 里的加权轮询（wrr）仲裁原语。
- 判断哪些数据适合放 CVSRAM（热数据、复用数据），哪些只能走 DBB。

## 2. 前置知识

在进入源码前，先用三段话补齐直觉。

**（1）为什么要两套存储接口？** NVDLA 的卷积/后处理引擎需要海量读写特征图与权重。如果所有访问都打到片外 DRAM（经 DBB 出芯片），延迟高、带宽吃紧、功耗大。于是 NVDLA 预留了一块**片上 SRAM，代号 CVSRAM**（Convolution SRAM），作为低延迟的「二级缓存」。访问 CVSRAM 的那套接口就是 **CVIF**（CVSRAM InterFace），访问 DBB 的那套是 **MCIF**（Memory Controller InterFace）。两者都走标准 AXI 协议，但在物理上一快一慢、一内一外。

**（2）「同构」是什么意思？** 从 CPU/引擎视角看，它们只管向「一个存储接口」发 DMA 请求（带地址、长度），接口内部把它翻译成 AXI 事务。MCIF 和 CVIF 的内部翻译流水是一套图纸复制两份：**请求入口（IG）→ 命令队列（cq）→ 响应出口（eg）**。所以读懂 MCIF（u4-l2）几乎等于读懂 CVIF，本讲只需点出二者的少数差别。

**（3）谁负责在两套存储之间搬数据？** 是 **BDMA**（Bridge DMA，见 u4-l4）。典型用法是：BDMA 把权重从 DBB 预搬到 CVSRAM，之后卷积引擎直接走 CVIF 低延迟读取。这正是「CVSRAM 适合放热数据」的来源。

> 小贴士：源码里 CVIF 的对外 AXI 端口叫 `cvif2noc_axi_*`（noc = network on chip）。这个 `noc` 是历史命名，**实际连的是 CVSRAM，而不是某个片上网络**——后文会用顶层连线证明这一点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `vmod/nvdla/nocif/NV_NVDLA_cvif.v` | CVIF 顶层，例化 read/write/csb 三个子模块，是 10 个读客户端、5 个写客户端的汇聚点。 |
| `vmod/nvdla/nocif/NV_NVDLA_CVIF_read.v` | 读通路，内含 `IG→cq→eg` 三级；10 个读客户端、10 个线程槽。 |
| `vmod/nvdla/nocif/NV_NVDLA_CVIF_write.v` | 写通路，内含 `IG→cq→eg` 三级；5 个写客户端、5 个线程槽。 |
| `vmod/nvdla/nocif/NV_NVDLA_CVIF_READ_ig.v` | 读入口，例化 10 个 `bpt` + `arb` + `spt` + `cvt` 四级。 |
| `vmod/nvdla/nocif/NV_NVDLA_XXIF_libs.v` | MCIF/CVIF **共享**的仲裁原语库，含多个 `arbgen2` 生成的 wrr 仲裁器。 |
| `vmod/nvdla/nocif/NV_NVDLA_CVIF_CSB_reg.v` | CVIF 自己的配置寄存器文件：读/写权重、outstanding 计数、状态。 |
| `vmod/nvdla/top/NV_NVDLA_partition_o.v` | 中央枢纽分区，在这里并排例化 `u_NV_NVDLA_mcif` 与 `u_NV_NVDLA_cvif`。 |
| `vmod/nvdla/top/NV_nvdla.v` | 芯片顶层，把 CVIF 的 `cvif2noc_axi_*` 连到对外端口 `nvdla_core2cvsram_*`。 |

---

## 4. 核心概念与源码讲解

### 4.1 CVIF 二级接口定位：通往片上 CVSRAM

#### 4.1.1 概念说明

CVIF 是 NVDLA 的**二级（secondary）存储接口**。一级（primary）是 MCIF，终点是片外主存 DBB；二级是 CVIF，终点是片上 CVSRAM。

- **primary / secondary 的选择权在引擎**：每个引擎的 DMA 在配置时选定本次访问走 MCIF 还是 CVIF。从 CVIF 顶层端口看，它收集的是 `bdma2cvif_*`、`cdma_dat2cvif_*`、`cdma_wt2cvif_*`、`cdp2cvif_*`、`pdp2cvif_*`、`rbk2cvif_*`、`sdp2cvif_*`（含 `sdp_b/e/n` 三个子口）等「`xxx2cvif`」请求——引擎名后缀 `_cvif` 即表示「我这次要走 CVSRAM」。
- **物理上是片上 SRAM**：CVSRAM 与 DLA 同处一颗芯片，访问延迟远低于经 DBB 出片到 DRAM。它容量有限（设计上由 SoC 集成者给定），因此是稀缺资源，只该放「热数据」。

#### 4.1.2 核心流程

CVIF 顶层把对外职责切成三块，分别交给三个子模块：

```text
                 csb2cvif_req ──► u_csb (寄存器配置: 权重/outstanding)
                                        │ reg2dp_*
   各引擎 ──读请求──►  u_read  ──► cvif2noc_axi_ar (AR 读地址通道)
   各引擎 ◄──读响应──  u_read  ◄── noc2cvif_axi_r  (R  读数据通道)
   各引擎 ──写请求──►  u_write ──► cvif2noc_axi_aw/w (AW 地址 + W 数据)
   各引擎 ◄──写完成──  u_write ◄── noc2cvif_axi_b   (B  写响应通道)
```

- `u_read`：把多路读请求仲裁成一路 AXI AR，并把 AXI R 响应按 id 分发回各引擎。
- `u_write`：把多路写请求仲裁成 AXI AW/W，并用 AXI B 通道向各引擎回报写完成。
- `u_csb`：接收 CPU 经 CSB 下发的 CVIF 配置（仲裁权重、outstanding 上限），翻译成 `reg2dp_*` 控制信号。

#### 4.1.3 源码精读

CVIF 顶层只做「连线 + 例化三子模块」，自身无逻辑。三个例化点见 [NV_NVDLA_cvif.v:L321-L322](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_cvif.v#L321-L322)（`u_read`）、[NV_NVDLA_cvif.v:L415-L416](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_cvif.v#L415-L416)（`u_write`）、[NV_NVDLA_cvif.v:L460-L461](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_cvif.v#L460-L461)（`u_csb`）：

```verilog
NV_NVDLA_CVIF_read  u_read  ( ... );   // L321: 读通路
NV_NVDLA_CVIF_write u_write ( ... );   // L415: 写通路
NV_NVDLA_CVIF_csb   u_csb   ( ... );   // L460: CSB 配置适配
```

对外的 AXI 读地址与读数据端口在 [NV_NVDLA_cvif.v:L195-L199](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_cvif.v#L195-L199)（AR：valid/arid[7:0]/arlen[3:0]/araddr[63:0]）与 [NV_NVDLA_cvif.v:L247-L251](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_cvif.v#L247-L251)（R：rvalid/rid[7:0]/rlast/rdata[511:0]）。注意这里与 MCIF 完全一致：512 位数据、8 位 id、4 位 len——说明 CVIF 同样按 512 位原子块、最多 16 拍（`arlen` 4 位）的 AXI 突发访问 CVSRAM。

**证明 CVIF 终点是 CVSRAM 的关键连线**在顶层：[NV_nvdla.v:L1228-L1232](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L1228-L1232) 把 CVIF 的 `cvif2noc_axi_ar_*` 直接连到对外端口 `nvdla_core2cvsram_ar_*`：

```verilog
.cvif2noc_axi_ar_arvalid (nvdla_core2cvsram_ar_arvalid)  // L1228
.cvif2noc_axi_ar_araddr  (nvdla_core2cvsram_ar_araddr)   // L1232
```

而 `nvdla_core2cvsram_*` 这组端口在 [NV_nvdla.v:L56-L78](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L56-L78) 定义，名字直指 CVSRAM。所以「`cvif2noc` 其实通向 CVSRAM」不是猜测，是顶层连线的直接结论。

#### 4.1.4 代码实践

**实践目标**：在源码里亲自验证「CVIF 与 MCIF 是并排存在于同一个分区的两套接口」。

**操作步骤**：

1. 打开 `vmod/nvdla/top/NV_NVDLA_partition_o.v`，定位 [L2108](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v#L2108) 的 `u_NV_NVDLA_cvif` 例化。
2. 向上滚动到 [L1923](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v#L1923)，确认这里还有一个 `u_NV_NVDLA_mcif` 例化。
3. 观察两者的引擎侧端口：CVIF 全是 `xxx2cvif_*`，MCIF 全是 `xxx2mcif_*`——同一批引擎（bdma/cdma/cdp/pdp/rbk/sdp）各有两套请求口。

**需要观察的现象**：同一个引擎（如 sdp）在 `partition_o` 里同时连到 mcif 和 cvif 两个实例，分别对应「走 DBB」和「走 CVSRAM」两种选择。

**预期结果**：确认 mcif 与 cvif 是 partition_o 内**并列**的两个模块实例，从而印证 u4-l1「两套接口共存于中央枢纽」的结论。

#### 4.1.5 小练习与答案

**练习 1**：CVIF 的 AXI 读通道叫 `cvif2noc_axi_ar_*`，它到底连到哪里？

**参考答案**：连到芯片顶层对外端口 `nvdla_core2cvsram_ar_*`（见 `NV_nvdla.v` L1228–1232），最终通往片上 CVSRAM。`noc` 是历史命名，并非真实片上网络。

**练习 2**：如果一个引擎想「这次 DMA 走片上 SRAM」，它该把请求发给 `xxx2mcif_*` 还是 `xxx2cvif_*`？

**参考答案**：发给 `xxx2cvif_*`。后缀 `_cvif` 表示走 CVIF→CVSRAM 这条二级通路；`_mcif` 表示走 MCIF→DBB 主存通路。

---

### 4.2 读写通路：CVIF_read 与 CVIF_write 的 IG→cq→eg 三级结构

#### 4.2.1 概念说明

CVIF 的读、写是两条**互不相干**的通路（读走 AXI 的 AR/R 通道，写走 AW/W/B 通道），各自内部都是 u4-l2 讲过的 **IG→cq→eg** 三级：

- **IG（ingress，入口）**：把多个引擎的请求仲裁成「一路」AXI 流。内部再细分 `bpt→arb→spt→cvt` 四级。
- **cq（command queue，命令队列）**：用若干「线程槽」记录每个在途事务的来源与上下文，等响应回来时凭此把数据送回正确的引擎。
- **eg（egress，出口）**：接收 AXI 返回的读数据 / 写响应，按 id 查 cq 里的上下文，分发回各引擎。

CVIF 与 MCIF 在这一层**结构完全相同**，差别只是客户端数量、线程槽数量。

#### 4.2.2 核心流程

**读通路**（10 客户端 → 10 线程 → 10 路回送）：

```text
10 个引擎读请求 ──► [bpt0..bpt9] ──► [arb 10选1 wrr] ──► [spt] ──► [cvt] ──► AXI AR
                              │ 写入 cq_wr (附 thread_id 0..9)
                              ▼
        AXI R (按 rid) ──► [eg] 查 cq_rd0..cq_rd9 ──► 10 路读响应回送各引擎
```

- 读客户端共 10 个：`bdma / cdma_dat / cdma_wt / cdp / pdp / rbk / sdp / sdp_b / sdp_e / sdp_n`。
- cq 用 `cq_rd0..cq_rd9`（10 个读口）+ 1 个写口 `cq_wr`，`thread_id` 为 4 位（可表达 0–15，实际用 0–9）。
- 每个客户端配一个 `*_rd_cdt_lat_fifo_pop` 信用（credit）信号，用于背压。

**写通路**（5 客户端 → 5 线程 → 5 路写完成）：

```text
5 个引擎写请求 ──► [IG: arb 5选1 wrr + 生成 AW/W] ──► AXI AW/W
                              │ 写入 cq_wr (thread_id 0..4)
                              ▼
        AXI B (按 bid) ──► [eg] 查 cq_rd0..cq_rd4 ──► 5 路 wr_rsp_complete 回送
```

- 写客户端只有 5 个：`bdma / cdp / pdp / rbk / sdp`——只有「会产出数据」的引擎才写存储。
- cq 用 `cq_rd0..cq_rd4`（5 个读口），`thread_id` 为 3 位。

> 为什么 cdma（dat/wt）只有读口没有写口？因为 CDMA 是卷积取数引擎，它把外部数据**拉进**片上 CBUF 供 CSC 消费，只读不写存储。输出类引擎（sdp/cdp/pdp/rbk/bdma）才需要写回结果。

#### 4.2.3 源码精读

**读通路三级例化**见 [NV_NVDLA_CVIF_read.v:L286-L288](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_CVIF_read.v#L286-L288)（`u_ig`）、[L349-L350](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_CVIF_read.v#L349-L350)（`u_cq`）、[L388-L389](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_CVIF_read.v#L388-L389)（`u_eg`）。中间连接 cq 的 wire 在 [NV_NVDLA_CVIF_read.v:L250-L283](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_CVIF_read.v#L250-L283) 声明了 `cq_rd0_pd..cq_rd9_pd`（各 7 位上下文）与一个 `cq_wr_pd`/`cq_wr_thread_id[3:0]`：

```verilog
wire [6:0] cq_rd0_pd;  ...  wire [6:0] cq_rd9_pd;  // L250-L279: 10 个线程读口
wire [6:0] cq_wr_pd;                          // L280: 写口上下文
wire [3:0] cq_wr_thread_id;                   // L283: 4 位线程 id (0..9)
```

读客户端的请求宽度与 MCIF 完全一致：读请求 `*_rd_req_pd` 为 79 位（[78:0]，见 [L144](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_CVIF_read.v#L144)），读响应 `*_rd_rsp_pd` 为 514 位（[513:0]，见 [L162](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_CVIF_read.v#L162)）——即 2 位掩码 + 512 位数据的原子块。

**写通路三级例化**见 [NV_NVDLA_CVIF_write.v:L154](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_CVIF_write.v#L154)（`u_ig`）、[L197](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_CVIF_write.v#L197)（`u_eg`）、[L227](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_CVIF_write.v#L227)（`u_cq`）。写客户端请求宽度 `*_wr_req_pd` 为 515 位（[514:0]，见 [L64](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_CVIF_write.v#L64)），注释 `pkt_widths=78,514` 表明它由请求头 + 数据载荷拼接，与 MCIF 写请求同构。

读 IG 内部的四级 `bpt→arb→spt→cvt` 例化见 [NV_NVDLA_CVIF_READ_ig.v:L214-L344](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_CVIF_READ_ig.v#L214-L344)（10 个 `bpt`：`u_bpt0..u_bpt9`）、[L345-L346](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_CVIF_READ_ig.v#L345-L346)（`u_arb`）、[L392](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_CVIF_READ_ig.v#L392)（`u_spt`）、[L402](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_CVIF_READ_ig.v#L402)（`u_cvt`）。每个 bpt 对应一个客户端做事务边界切分与信用背压，arb 做 10 选 1 加权轮询，spt 按 AXI beat 拆拍，cvt 生成 AR 并写线程上下文（受 outstanding 反压）。这四级的作用与 u4-l2 在 MCIF 里讲的完全一致，此处不再展开。

#### 4.2.4 代码实践

**实践目标**：亲手数出 CVIF 读/写各自的客户端与线程槽数量，体会「读多写少」的设计。

**操作步骤**：

1. 在 `NV_NVDLA_CVIF_read.v` 中统计形如 `output  *_rd_req_ready;` 的端口数量（即可接收读请求的客户端数）。
2. 统计 `cq_rd*_pvld` 的个数（即线程槽数）。
3. 在 `NV_NVDLA_CVIF_write.v` 中重复以上两步，统计写客户端与写线程槽。

**需要观察的现象**：读侧得到 10 个客户端、10 个线程槽；写侧得到 5 个客户端、5 个线程槽。

**预期结果**：与 MCIF（读 10、写 5）完全相同。说明「哪些引擎会读存储、哪些会写存储」是 NVDLA 引擎本身的功能决定的，与接口终点是 DBB 还是 CVSRAM 无关——这正是 CVIF 能照搬 MCIF 结构的根本原因。

#### 4.2.5 小练习与答案

**练习 1**：CVIF 读通路的 cq 为什么需要 10 个读口（`cq_rd0..cq_rd9`）？

**参考答案**：因为读通路最多有 10 个引擎的读事务同时在途，cq 用 10 个线程槽分别记录每个事务的来源/上下文；eg 收到 AXI R 响应时按 `rid` 选通对应读口，把数据送回正确的引擎。

**练习 2**：为什么写客户端（5 个）比读客户端（10 个）少？

**参考答案**：只有会**产出结果**的引擎才需要写存储：sdp/cdp/pdp/rbk/bdma。而 cdma（dat/wt）只把外部数据拉进片上 CBUF，只读不写，所以读客户端里多出 `cdma_dat`、`cdma_wt` 两路。

---

### 4.3 CVIF 与 MCIF 的同构与差异，以及 XXIF_libs 共享

#### 4.3.1 概念说明

u4-l1 已点明「MCIF 与 CVIF 结构同构」，本讲在源码层落实这句话，并讲清两点：

- **同构到什么程度**：读入口 `READ_ig` 里的子模块布局连**行号都一样**——`bpt0..bpt9 / arb / spt / cvt` 的例化顺序与位置完全对应。
- **为何能共享 `XXIF_libs`**：仲裁逻辑（加权轮询 wrr）与客户端数无关，是被工具 `arbgen2` 按参数（客户端数 n、权重位宽 wt_width）生成的通用原语；CVIF 和 MCIF 的读 egress 都是 10 选 1、写 ingress/egress 都是 5 选 1，于是能复用同一组 wrr 模块。`XXIF` 里的 `XX` 即「MCIF 或 CVIF」的占位。

而 CVIF 与 MCIF 真正的差异只有两点：①终点（CVSRAM vs DBB）；②可编程参数（仲裁权重、outstanding 上限）独立配置。

#### 4.3.2 核心流程

共享仲裁原语的调用关系：

```text
   CVIF_READ_eg ┐
                ├─► 共享 XXIF_libs: read_eg_arb (10选1 wrr)
   MCIF_READ_eg ┘

   CVIF_WRITE_ig ┐   CVIF_WRITE_eg ┐
                 ├─► write_ig_arb  ├─► write_eg_arb   (均为 5选1 wrr)
   MCIF_WRITE_ig ┘   MCIF_WRITE_eg ┘
```

加权轮询的直观规则：每个客户端有一个 8 位权重 `wt`，仲裁器在当前被授权客户端 `wrr_gnt` 仍有「剩余权重」且仍有请求时持续授权给它，权重耗尽才轮到下一个有请求的客户端；权重为 0 可软禁用该客户端。形式上，若记在途授权的剩余权重为 \( w_{\text{left}} \)，则：

\[
\text{保持当前授权} \;\Longleftrightarrow\; (w_{\text{left}}>0)\;\land\;(\text{req}_{\text{cur}}=1)
\]

否则跳到下一个有请求且权重非零的客户端。

#### 4.3.3 源码精读

**同构的铁证**：把 `NV_NVDLA_CVIF_READ_ig.v` 与 `NV_NVDLA_MCIF_READ_ig.v` 对照，二者 `module` 声明都在第 12 行，`bpt0..bpt9` 例化都在第 214–331 行，`u_arb` 都在 345 行，`u_spt` 都在 392 行，`u_cvt` 都在 402 行，`endmodule` 都在 421 行——除模块名前缀 `CVIF_`/`MCIF_` 外逐行对应。可对照 [CVIF_READ_ig.v:L214-L402](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_CVIF_READ_ig.v#L214-L402) 与 [MCIF_READ_ig.v:L214-L402](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_ig.v#L214-L402)。

**共享仲裁原语**在 `NV_NVDLA_XXIF_libs.v` 里由 `arbgen2` 生成，文件内有清晰的生成注释与客户端数：

- 读 egress 10 选 1：[XXIF_libs.v:L2074-L2075](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_XXIF_libs.v#L2074-L2075) `// arbgen2 -m read_eg_arb -n 10 ... -t wrr -wt_width 8`，对应 `wrr_gnt[9:0]`（见 [L2157](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_XXIF_libs.v#L2157)）。
- 写 ingress 5 选 1：[XXIF_libs.v:L4092-L4093](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_XXIF_libs.v#L4092-L4093) `// arbgen2 -m write_ig_arb -n 5 ... -gnt_busy -t wrr -wt_width 8`，对应 `wrr_gnt[4:0]`（见 [L4147](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_XXIF_libs.v#L4147)）。
- 写 egress 5 选 1：[XXIF_libs.v:L5223-L5224](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_XXIF_libs.v#L5223-L5224) `// arbgen2 -m write_eg_arb -n 5 ... -t wrr -wt_width 8`，对应 `wrr_gnt[4:0]`（见 [L5276](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_XXIF_libs.v#L5276)）。

这些 wrr 模块对 CVIF/MCIF 透明：客户端数（10 或 5）与权重位宽（8）一旦相同，仲裁器代码就完全一致，自然可被两个接口共用一份 `XXIF_libs.v`。

**真正的差异点**：CVIF 的对外 AXI 连 `nvdla_core2cvsram_*`（片上），MCIF 连 `nvdla_core2dbb_*`（片外）；二者寄存器（权重、outstanding）各自独立、由各自的 `CSB_reg` 配置。所以 CVIF 与 MCIF 是「同一图纸的两个实例」，差异只在终点与可编程参数。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：用最直接的方式证明 CVIF/MCIF 同构、定位共享原语，并给出 CVSRAM 该放什么数据的判断。

**操作步骤**：

1. **对比两份 READ_ig**：并排打开 `NV_NVDLA_CVIF_READ_ig.v` 与 `NV_NVDLA_MCIF_READ_ig.v`，逐行比对 `bpt/arb/spt/cvt` 的例化（行号应一一对应）。把模块名前缀 `CVIF_` 全局替换成 `MCIF_`，看是否基本得到另一份文件。
2. **定位共享原语**：在 `NV_NVDLA_XXIF_libs.v` 里搜索 `arbgen2`，记录三个 wrr 仲裁器（`read_eg_arb -n 10`、`write_ig_arb -n 5`、`write_eg_arb -n 5`）。说明 CVIF 和 MCIF 为何能共用：客户端数相同 → 同一段 wrr 代码即可服务两者。
3. **判断 CVSRAM 用途**：结合「CVSRAM 片上、低延迟、容量有限」与「BDMA 可在 DBB↔CVSRAM 间搬运」（见 u4-l4），列出适合放 CVSRAM 的数据类型。

**需要观察的现象**：两份 READ_ig 结构镜像；XXIF_libs 的 wrr 模块按 `-n` 参数生成、与接口名无关。

**预期结果（CVSRAM 适合存放的数据）**：

- **被多次复用的权重**（同一组卷积核在多输入通道/多批次间反复读取）——放 CVSRAM 可显著省带宽、降延迟。
- **层间中间特征图**（上一引擎写回、下一引擎立即读取的热数据）。
- **不宜放**：只读一次的流式输入、最终输出（这些走 DBB 即可，CVSRAM 容量宝贵）。

**说明为何能共享 XXIF_libs**：加权轮询仲裁只依赖「客户端数 n、权重位宽 wt_width、是否需要 gnt_busy」这三个参数；CVIF 与 MCIF 的读 egress 同为 10 客户端、写 ingress/egress 同为 5 客户端，故 `arbgen2` 生成的同一组 wrr 模块能同时服务两套接口，无需为 CVIF 另写一份。

> 若本地已按 u1-l4 配好 VCS/Verilator 仿真环境（可选）：跑一个 sanity trace，在波形或日志里过滤 `cvsram` 相关地址，观察某引擎是否同时存在 `mcif` 与 `cvif` 两类访问；若无法运行，记「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 NVDLA 不为 CVIF 单独写一套仲裁器，而要和 MCIF 共享 `XXIF_libs`？

**参考答案**：因为两者的客户端数（读 10、写 5）与权重位宽（8）完全相同，而 wrr 仲裁逻辑只依赖这几个参数。共享同一份 `arbgen2` 生成的原语，既减少重复代码、降低维护成本，又保证两套接口的仲裁行为一致，便于综合与时序收敛。

**练习 2**：列出 CVIF 与 MCIF 的所有真实差异。

**参考答案**：①终点不同：CVIF→片上 CVSRAM（`nvdla_core2cvsram_*`），MCIF→片外 DBB（`nvdla_core2dbb_*`）；②可编程参数独立：各自的 `CSB_reg` 单独配置仲裁权重与 outstanding 上限。其余结构（IG→cq→eg、客户端数、AXI 位宽、wrr 仲裁）完全相同。

---

### 4.4 CVIF 的寄存器配置：仲裁权重与 outstanding 计数

#### 4.4.1 概念说明

CVIF 自己有一组配置寄存器（由 RDL/Ordt 自动生成的 `NV_NVDLA_CVIF_CSB_reg.v`），CPU 经 CSB 写入，用来调两类参数：

- **仲裁权重（weight）**：每个读/写客户端一个 8 位权重，喂给 `XXIF_libs` 的 wrr 仲裁器，控制各引擎争用 CVSRAM 带宽的优先级。权重为 0 可软禁用该客户端。
- **outstanding 计数上限（os_cnt）**：限制读/写方向同时在途的 AXI 事务数，防止 CVSRAM 侧被过多未完成事务淹没。

这些寄存器与 MCIF 的同名寄存器一一对应，只是基地址不同、独立编程。

#### 4.4.2 核心流程

```text
CSB 写 (csb2cvif_req_pd)
   │ reg_offset/reg_wr_data/reg_wr_en
   ▼
NV_NVDLA_CVIF_CSB_reg  ──译码──► 命中某 weight / os_cnt 寄存器
   │ reg2dp_rd_weight_*  /  reg2dp_wr_weight_*  /  reg2dp_*_os_cnt
   ▼
CVIF_read.ig / CVIF_write.ig  ──► 调整 wrr 仲裁与 outstanding 反压
```

形式上，读方向允许的未完成事务数受上限约束：

\[
N_{\text{rd,out}} \le \texttt{rd\_os\_cnt}, \qquad N_{\text{wr,out}} \le \texttt{wr\_os\_cnt}
\]

#### 4.4.3 源码精读

寄存器译码（地址→写使能）见 [NV_NVDLA_CVIF_CSB_reg.v:L129-L135](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_CVIF_CSB_reg.v#L129-L135)。注意地址比较用的是 `0x3xxx & 0xfff`，即 **CVIF 寄存器页基址为 0x3000**，页内偏移 0x000–0x018 分别对应 7 个寄存器：

| 页内偏移 | 绝对地址 | 寄存器 | 字段 |
| --- | --- | --- | --- |
| 0x000 | 0x3000 | `RD_WEIGHT_0` | rd_weight_{cdp, pdp, sdp, bdma} |
| 0x004 | 0x3004 | `RD_WEIGHT_1` | rd_weight_{cdma_dat, sdp_e, sdp_n, sdp_b} |
| 0x008 | 0x3008 | `RD_WEIGHT_2` | rd_weight_{rsv_0, rsv_1, rbk, cdma_wt} |
| 0x00c | 0x300c | `WR_WEIGHT_0` | wr_weight_{cdp, pdp, sdp, bdma} |
| 0x010 | 0x3010 | `WR_WEIGHT_1` | wr_weight_{rsv_0, rsv_1, rsv_2, rbk} |
| 0x014 | 0x3014 | `OUTSTANDING_CNT` | {wr_os_cnt[15:8], rd_os_cnt[7:0]} |
| 0x018 | 0x3018 | `STATUS` | idle（只读） |

读权重 3 个字拼成 12 个 8 位字段（10 个活跃客户端 + 2 个保留 `rsv`），见输出拼装 [L138-L140](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_CVIF_CSB_reg.v#L138-L140)；写权重 2 个字拼成 8 个字段（5 活跃 + 3 保留），见 [L141-L142](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_CVIF_CSB_reg.v#L141-L142)。outstanding 计数把读写拼成一个 32 位寄存器，见 [L137](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_CVIF_CSB_reg.v#L137)：

```verilog
assign nvdla_cvif_cfg_outstanding_cnt_0_out = { 16'b0, wr_os_cnt, rd_os_cnt };  // L137
```

**复位默认值**见 [L191-L212](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_CVIF_CSB_reg.v#L191-L212)：所有权重默认为 `8'b00000001`（即所有客户端等权轮询），`rd_os_cnt`/`wr_os_cnt` 默认为 `8'b11111111`（255，允许最大 256 个 outstanding）。也就是说开箱即用、各引擎公平分享 CVSRAM 带宽；SoC 集成者可按负载改写权重给某引擎更高优先级。

#### 4.4.4 代码实践

**实践目标**：理解 CVIF 寄存器的默认行为，知道不动它时各引擎如何分享 CVSRAM 带宽。

**操作步骤**：

1. 打开 `NV_NVDLA_CVIF_CSB_reg.v`，定位复位块 [L189-L213](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_CVIF_CSB_reg.v#L189-L213)。
2. 读出每个 `rd_weight_*`、`wr_weight_*` 的复位值。
3. 读出 `rd_os_cnt`、`wr_os_cnt` 的复位值。

**需要观察的现象**：权重全为 1，os_cnt 全为 0xFF。

**预期结果**：默认配置下，10 个读客户端、5 个写客户端等权轮询 CVSRAM 带宽；读/写各自最多 256 个 outstanding 事务。这意味着若不给 CVIF 写任何寄存器，它也能以「公平、不卡死」的默认策略工作。

#### 4.4.5 小练习与答案

**练习 1**：若想让卷积取数（`cdma_dat`）在争用 CVSRAM 时优先于其他引擎，应改写哪个寄存器字段？

**参考答案**：改写 `RD_WEIGHT_1`（偏移 0x3004）里的 `rd_weight_cdma_dat` 字段（最高字节 [31:24]，见 CSB_reg.v L139、L245-L247），把它的值调大于其他客户端的权重（默认为 1）。

**练习 2**：复位后 `rd_os_cnt` 的值是多少？它代表什么？

**参考答案**：复位值为 `8'hFF`（255）。它限制读方向同时在途的 AXI 事务数 \( N_{\text{rd,out}}\le 255 \)，用于防止 CVSRAM 侧被过多未完成读事务淹没。

---

## 5. 综合实践

**任务**：为一个「权重会被反复复用」的卷积层，规划存储路径并把 CVIF 串进端到端数据流。

背景：某卷积层的权重在多个输入特征图上要做大量乘加，反复从片外 DRAM 取权重代价很高。请利用 CVSRAM 缓存权重。

请完成：

1. **选路径**：说明权重的「预装」阶段应让 BDMA 从 DBB 搬到 CVSRAM（即 BDMA 写请求走 `bdma2cvif_wr_req_*`），搬运完成后权重落在 CVSRAM。
2. **配接口**：指出卷积取数阶段 CDMA 读权重时应走 `cdma_wt2cvif_rd_req_*`（CVIF 读通路，低延迟），而非 `cdma_wt2mcif_*`。
3. **画数据流**：画出 `DBB ──BDMA──► CVSRAM ──CVIF.read──► CDMA.wt ──► CBUF ──► CSC ──► CMAC` 的链路，标注每段用的是哪个接口/引擎。
4. **调参数（可选）**：若想让权重取数优先，说明应把 `RD_WEIGHT_2` 的 `rd_weight_cdma_wt`（偏移 0x3008，字节 [7:0]）调高。
5. **回归同构**：用一句话说明，若集成者没有 CVSRAM、全走 DBB，只需让所有引擎的 DMA 选 `*_2mcif_*` 而非 `*_2cvif_*`，CVIF 整套逻辑可不参与——因为 MCIF 与 CVIF 同构、可互相替代。

**预期产出**：一张标注清晰的存储路径图 + 一段说明「CVSRAM 放热数据、CVIF 提供低延迟读取、BDMA 负责预装」的结论。这一步把本讲（CVIF）与 u4-l1（架构）、u4-l2（MCIF）、u4-l4（BDMA）串成了一条完整数据链。

## 6. 本讲小结

- **CVIF 是二级存储接口**：终点是片上 CVSRAM（`nvdla_core2cvsram_*`），与终点为片外 DBB 的 MCIF 并列；二者都例化在中央枢纽 `partition_o`。
- **结构同构到逐行**：CVIF_read / CVIF_write 与 MCIF 一样是 `IG→cq→eg` 三级；读 10 客户端 / 10 线程槽，写 5 客户端 / 5 线程槽，连 READ_ig 的 `bpt/arb/spt/cvt` 例化行号都一一对应。
- **共享 XXIF_libs**：加权轮询（wrr）仲裁只依赖客户端数与权重位宽，CVIF/MCIF 参数相同，故共用一份 `arbgen2` 生成的 `read_eg_arb(-n10)`、`write_ig_arb(-n5)`、`write_eg_arb(-n5)`。
- **真实差异仅两点**：终点（CVSRAM vs DBB）与可编程参数（各自的权重 / outstanding 寄存器，独立编程）。
- **CVIF 寄存器页基址 0x3000**：含读/写权重（默认全 1，等权轮询）、outstanding 上限（默认 0xFF，最大 256）、只读 status。
- **CVSRAM 该放热数据**：被反复复用的权重、层间中间特征图；只读一次的流数据与最终输出走 DBB 即可。

## 7. 下一步学习建议

- **u4-l4（BDMA）**：本讲多次提到「BDMA 在 DBB↔CVSRAM 间搬运」。下一步应深读 BDMA 的 load/store/cq 子模块，看它如何编程一次完整搬运并上报 done 中断——这是让 CVSRAM 真正发挥作用（预装权重）的操作手段。
- **回顾 u4-l2（MCIF）的 bpt/spt/cvt 细节**：本讲刻意没重复 IG 内部四级的实现细节。若想彻底搞懂 CVIF 的 IG，请回到 u4-l2 把 `bpt`（事务边界切分 + 信用反压）、`spt`（AXI beat 拆拍）、`cvt`（生成 AR + outstanding 反压）的实现读懂——它们在 CVIF 里完全照搬。
- **u8-l2（RDL/Ordt 寄存器生成）**：本讲的 `NV_NVDLA_CVIF_CSB_reg.v` 是自动生成的。学完 u8-l2 后，你会明白这组权重/outstanding 寄存器是如何从 SystemRDL 单一可信源一路生成到 RTL/RAL/cmod 的。
