# MCIF：主存接口（DBB/AXI）

## 1. 本讲目标

本讲深入 NVDLA 的 **primary 存储接口 MCIF**（Memory Controller InterFace）的内部实现。读完本讲，你应当能够：

- 说清 MCIF_read 与 MCIF_write 如何把内部多个引擎的 DMA 请求，仲裁成一组对外的 AXI（DBB）事务。
- 画出 READ 通路 `bpt → arb → spt → cvt → cq → eg` 的逐级数据流，并解释每一级解决什么问题。
- 区分 **背压（back-pressure，bpt）** 与 **单口拆分（single-port split，spt）** 两件不同的事。
- 理解 **命令队列（cq）** 如何用一块共享 RAM 保存“事务上下文”，再按 AXI 响应 id 把数据送回正确的引擎。
- 读懂 egress 如何对返回的 512-bit 数据做 demux、重排（swizzle）与掩码（drop），最终拼成各引擎期望的 514-bit 读响应。

本讲承接 u4-l1（存储接口架构总览），把 MCIF 这套 `IG / cq / eg` 三级结构从“概念”落到“源码”。

## 2. 前置知识

- **AXI 通道**：读事务走 `AR`（地址）/`R`（数据）两通道；写事务走 `AW`（写地址）/`W`（写数据）/`B`（写响应）三通道。每通道都用 `valid/ready` 握手。`arlen` 表示一次突发（burst）传 `arlen+1` 拍。本讲涉及的 AXI 数据位宽固定 **512 bit**。
- **原子块（atom / cache line）**：MCIF 对外以 64 字节对齐、256 字节为一个“行”（line）搬运。一行 = 512 bit × 8 拍 = 4096 bit = 512 B；半个行 = 256 B = 512 bit × 4 拍。引擎内部常以 256 B 为最小搬运粒度。
- **信用（credit）**：下游 FIFO 容量有限，上游不能无限灌数据。常见做法是上游持有一个“可用槽位”计数，发一笔就占用 `slot_needed` 个槽，下游消费一笔（`pop`）就归还，计数耗尽就停发——这叫信用反压。
- **DMA 请求包**：引擎给 MCIF 的读请求本质是“从地址 `addr` 起，搬 `size+1` 个 256 B 块”。本仓库里这个包是 79 bit：`{ size[14:0], addr[63:0] }`。
- **swizzle / odd / drop**：当一段数据不是整行对齐时，首拍可能只要“半行”、末拍也可能只要“半行”。`swizzle` 指半行高低位互换，`odd`/`fdrop`/`ldrop` 用来标记并丢弃不需要的那一半。这些位是 MCIF 内部为正确拼装数据而设的控制标记。

> 提示：本讲出现大量“数字+bit”的信号（如 `cq_wr_pd[6:0]`）。它们都是把多个小字段**拼接（pack）**进一个定宽总线以减少连线，读源码时先看 `PKT_PACK`/`assign` 注释里的字段顺序即可。

## 3. 本讲源码地图

本讲聚焦 `vmod/nvdla/nocif/` 下 MCIF 的读/写实现。`nocif` 这个目录名来自 “NoC（Network-on-Chip）Interface”，即挂在片上互联上的存储接口。

| 文件 | 角色 |
| --- | --- |
| [NV_NVDLA_MCIF_read.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_read.v) | 读通路顶层，例化 `IG / cq / eg` 三大子模块，对接 10 个读客户端与 AXI AR/R 通道 |
| [NV_NVDLA_MCIF_write.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_write.v) | 写通路顶层，例化 `IG / eg / cq`，对接 5 个写客户端与 AXI AW/W/B 通道 |
| [NV_NVDLA_MCIF_READ_ig.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_ig.v) | 读 ingress：把 10 路请求合并成 1 路 AXI AR 流（内含 bpt/arb/spt/cvt） |
| [NV_NVDLA_MCIF_READ_IG_bpt.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_IG_bpt.v) | 背压/事务切分：按行边界把请求切成首/中/尾事务，并做信用反压 |
| [NV_NVDLA_MCIF_READ_IG_spt.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_IG_spt.v) | 单口拆分：把每个事务再拆成 AXI 节拍 |
| [NV_NVDLA_MCIF_READ_IG_cvt.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_IG_cvt.v) | 协议转换：生成 AXI AR，并把“事务上下文”写入 cq |
| [NV_NVDLA_MCIF_READ_cq.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_cq.v) | 命令队列：一块 256×7 共享 RAM，存事务上下文，供 egress 按 id 回读 |
| [NV_NVDLA_MCIF_READ_eg.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_eg.v) | 读 egress：按 AXI R 的 id 分发数据、重排、掩码，送回各引擎 |

CVIF（连 CVSRAM）的结构与 MCIF 同构，原理完全一致，本讲不重复，参见 u4-l3。

## 4. 核心概念与源码讲解

### 4.1 MCIF 读写通路总览：IG → cq → eg

#### 4.1.1 概念说明

MCIF 要解决的核心矛盾是：**很多个引擎（CDMA、SDP、PDP、CDP、BDMA、Rubik……）都想访问同一个外部主存 DBB，但对外只有一组 AXI 总线。**

于是 MCIF 把每个方向（读/写）做成三级流水：

- **IG（Ingress，入口）**：把 N 路引擎请求**仲裁**成 1 路 AXI 命令流，并在仲裁前对每路做**背压**，防止某一路把下游 FIFO 灌满。
- **cq（Command Queue，命令队列）**：每发一笔 AXI 命令，就把“这笔命令回来后该怎么拼数据”的**上下文（context）**写进一块共享 RAM；等 AXI 响应回来时，再按响应 id 把上下文取出来。
- **eg（Egress，出口）**：接收 AXI 返回的数据，按 id 分发到对应引擎，必要时做半行重排与掩码。

读和写的客户端数量不同，因为**只有产生数据的引擎才写、所有需要输入数据的引擎都读**：

- READ：10 个客户端——`bdma, sdp, pdp, cdp, rbk, sdp_b, sdp_n, sdp_e, cdma_dat, cdma_wt`。
- WRITE：5 个客户端——`bdma, sdp, pdp, cdp, rbk`（CDMA/CSC/CMAC/CACC 只读不写）。

#### 4.1.2 核心流程

读通路的数据流（请求方向自上而下，响应方向自下而上）：

```text
                  10 个读客户端 (bdma / sdp / pdp / cdp / rbk / sdp_b/n/e / cdma_dat / cdma_wt)
                                       │  每路: req_pd(79b addr+size) + valid + cdt_lat_fifo_pop
                                       ▼
  ┌──────────────────── MCIF_READ (顶层) ────────────────────┐
  │                                                            │
  │   u_ig (ingress)                                           │
  │     10× bpt ──► 1× arb(加权轮询) ──► spt(拆beat) ──► cvt   │
  │                                              │             │
  │                                  发 AXI AR    │  写上下文    │
  └──────────────────────────────────────────────┼─────────────┘
                                                 ▼
                              mcif2noc_axi_ar_araddr/arid/arlen/arvalid  ──► DBB
                              noc2mcif_axi_r_rdata(512b)/rid/rlast/rvalid ◄── DBB
                                                 │
  ┌──────────────────────────────────────────────┼─────────────┐
  │   u_eg (egress)                              │             │
  │     按 rid demux ──► 10× lat_fifo ──► RR arb ──► 10× ro_fifo ──► 10 路 rd_rsp
  │     ▲                                                                              │
  │     └── 回读上下文 ◄──────── u_cq (256×7 RAM) ◄──────── cvt 写入上下文            │
  └────────────────────────────────────────────────────────────────────────────────────┘
```

写通路结构对称，只是 AR/R 换成 AW/W/B，5 路输入，输出只是“写完成”脉冲 `wr_rsp_complete`（写响应没有数据载荷）。

#### 4.1.3 源码精读

读通路顶层 `NV_NVDLA_MCIF_read` 只做一件事：例化三个子模块并把它们用内部 wire 串起来。核心例化在 [NV_NVDLA_MCIF_read.v:286-458](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_read.v#L286-L458)：

- `u_ig`（行 286）：吃 10 路 `*2mcif_rd_req_*`，吐出 AXI AR 与一组给 cq 的写信号 `cq_wr_pvld/prdy/thread_id/pd`，并接收 eg 反馈的 `eg2ig_axi_vld`。
- `u_cq`（行 349）：一路写口 `cq_wr_*`，10 路读口 `cq_rd0..9_*`，对应 10 个引擎线程。
- `u_eg`（行 388）：收 AXI R，从 cq 的 10 个读口取上下文，输出 10 路 `mcif2*_rd_rsp_*`。

注意几条跨模块的握手 wire（[NV_NVDLA_MCIF_read.v:250-284](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_read.v#L250-L284)）：每个引擎线程在 cq 里有一根独立的 7-bit 读数据线 `cq_rdN_pd`，这正是 egress 按 id 取回上下文的通道。

读写两路在引擎侧的请求宽度不同（[NV_NVDLA_MCIF_read.v:142-148](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_read.v#L142-L148) 的 `*2mcif_rd_req_pd [78:0]` 只有地址+大小；[NV_NVDLA_MCIF_write.v:62-68](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_write.v#L62-L68) 的 `*2mcif_wr_req_pd [514:0]` 还要带上 512-bit 数据），这印证了“读请求只描述去哪取、写请求要带要写的数据”。

#### 4.1.4 代码实践

1. **目标**：建立 MCIF_read 三级结构的全局印象。
2. **步骤**：打开 `NV_NVDLA_MCIF_read.v`，定位 `u_ig / u_cq / u_eg` 三个例化；数一数 `u_ig` 端口列表里 `*2mcif_rd_req_valid` 出现了多少次（应当是 10），对应 10 个读客户端。
3. **观察**：`u_cq` 有 1 个写口、10 个读口；`u_eg` 有 10 路 `mcif2*_rd_rsp_*` 输出。
4. **预期结果**：你能把“10 进 1 出（AXI）1 进（AXI R）10 出（响应）”这张拓扑默画出来。

#### 4.1.5 小练习与答案

- **练习 1**：为什么写客户端只有 5 个，而读客户端有 10 个？
  - **答**：只有产生输出数据的引擎（BDMA、SDP、PDP、CDP、Rubik）需要写回主存；而卷积前段（CDMA_dat/CDMA_wt）以及 SDP 的多个子通路（b/n/e）都需要从主存读输入，故读客户端更多。
- **练习 2**：读响应 `mcif2*_rd_rsp_pd` 是 514 bit，它由哪两部分组成？
  - **答**：高 2 bit 是 `mask`（哪半行有效），低 512 bit 是数据（见 4.5 节）。

### 4.2 IG 仲裁与背压：bpt / arb / spt / cvt

#### 4.2.1 概念说明

`IG` 是 ingress 的缩写，把 10 路请求捏成 1 路 AXI。它内部再分四级，每级职责单一：

| 子级 | 名字含义 | 职责 |
| --- | --- | --- |
| **bpt** | Back-Pressure / Transaction split | ①对每路做信用反压；②按 256 B 行边界把请求切成“首事务 / 中间事务 / 尾事务”，并打上 `ftran/ltran` 标记 |
| **arb** | Arbiter | 10 选 1 的**加权轮询**仲裁器，权重由 CSB 寄存器配置 |
| **spt** | Single-Port split | 把单个事务再拆成 AXI 节拍（一行 = 多 beat），逐拍给 cvt |
| **cvt** | Converter | 生成 AXI AR 信号，并把事务上下文写进 cq |

> **关键区分**：bpt 和 spt 都叫“拆分”，但拆的对象不同。bpt 按 **256 B 行边界**拆一个请求为多个事务（每个事务不跨行）；spt 按 **AXI beat** 把一个事务拆成多拍送给 cvt。bpt 还额外承担**信用反压**，spt 不做反压只做拆拍。

#### 4.2.2 核心流程

bpt 的工作可以拆成两件事：

1. **事务切分**：一个 DMA 请求要搬 `size+1` 个 256 B 块，但起始地址未必行对齐。bpt 用起始偏移 `stt_offset = addr[7:5]` 算出：
   - `ftran`（首事务）：从起始地址到第一个行尾的部分；
   - `mtran`（中间若干整行事务）；
   - `ltran`（尾事务）：最后一个不完整行。
   - 全程用 `count_req` 计数当前发到第几个事务，`is_ftran / is_mtran / is_ltran` 由它导出。

2. **信用反压**：bpt 维护一个 `lat_cnt_cur` 计数，代表“本路在下游 lat_fifo 里已占的槽位”。
   - 每发一个事务（`bpt2arb_accept`）就 `+= slot_needed`（这个事务会占几个槽）；
   - 下游每消费一笔（`dma2bpt_cdt_lat_fifo_pop`，即 egress 那边的 lat_fifo 弹出）就 `-= 1`；
   - 当 `slot_needed > lat_fifo_free_slot` 时拉低 `req_enable`，停止对本路取请求。

arb 的工作则是 10 路加权轮询，权重 `reg2dp_rd_weight_<engine>`（每路 8 bit），权重为 0 可软禁用某一路——这与 u4-l1 提到的 XXIF_libs 的 wrr 原语一致。

#### 4.2.3 源码精读

IG 顶层把 10 个 bpt + 1 个 arb + 1 个 spt + 1 个 cvt 例化串联（[NV_NVDLA_MCIF_READ_ig.v:214-419](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_ig.v#L214-L419)）。注意每个 bpt 例化时绑定了两个常量：`tieoff_axid`（该路固定的 4-bit id，0..9）和 `tieoff_lat_fifo_depth`（该路的信用额度）。例如 bdma 是 0 号、额度 245；cdma_dat / cdma_wt 额度为 0：

[NV_NVDLA_MCIF_READ_ig.v:214-226](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_ig.v#L214-L226)（u_bpt0，bdma，axid=0，depth=245）：

```verilog
NV_NVDLA_MCIF_READ_IG_bpt u_bpt0 (
   ...
  ,.dma2bpt_req_pd            (bdma2mcif_rd_req_pd[78:0])
  ,.dma2bpt_cdt_lat_fifo_pop  (bdma2mcif_rd_cdt_lat_fifo_pop)
  ,.bpt2arb_req_pd            (bpt2arb_req0_pd[74:0])
  ,.tieoff_axid               (4'd0)
  ,.tieoff_lat_fifo_depth     (8'd245)
);
```

信用反压的核心几行在 bpt 内部（[NV_NVDLA_MCIF_READ_IG_bpt.v:228-337](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_IG_bpt.v#L228-L337)）：

```verilog
assign lat_fifo_stall_enable = (tieoff_lat_fifo_depth!=0);
assign lat_count_inc = (bpt2arb_accept && lat_fifo_stall_enable) ? slot_needed : 0;
...
assign {mon_lat_fifo_free_slot_c,lat_fifo_free_slot[7:0]} = tieoff_lat_fifo_depth - lat_count_cnt;
assign req_enable = (!lat_fifo_stall_enable) || ({{5{1'b0}}, slot_needed} <= lat_fifo_free_slot);
```

含义：`depth==0` 的路（cdma_dat/cdma_wt）关闭信用反压（`lat_fifo_stall_enable==0`，`req_enable` 恒真）——因为卷积数据通路有自己的反压链（参见 u3-l3 提到的 accu2sc_credit）；其它引擎靠 bpt 信用防止溢出下游 lat_fifo。

事务切分用 `count_req` 状态机判定首/中/尾（[NV_NVDLA_MCIF_READ_IG_bpt.v:492-517](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_IG_bpt.v#L492-L517)）：

```verilog
assign is_ftran = (count_req==0);
assign is_mtran = (count_req>0 && count_req<req_num-1);
assign is_ltran = (count_req==req_num-1);
...
assign bpt2arb_axid  = tieoff_axid[3:0];
```

打包给 arb 的 75-bit 包结构见 [NV_NVDLA_MCIF_READ_IG_bpt.v:528-534](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_IG_bpt.v#L528-L534)：`{ ftran, ltran, odd, swizzle, size[2:0], addr[63:0], axid[3:0] }`。其中 `swizzle/odd` 由地址奇偶性导出（`out_swizzle = stt_offset[0]`），供 egress 拼数据用。

arb 的 10 路加权轮询在 [NV_NVDLA_MCIF_READ_ig.v:345-391](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_ig.v#L345-L391)，权重端口 `reg2dp_rd_weight_*` 一一对应 10 路。spt 与 cvt 分别在 [NV_NVDLA_MCIF_READ_ig.v:392-419](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_ig.v#L392-L419)。

spt 的拆拍逻辑（[NV_NVDLA_MCIF_READ_IG_spt.v:228-289](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_IG_spt.v#L228-L289)）用 `beat_count` 计数、`is_last_beat = (beat_count==beat_size)` 控制何时向 arb 回 `ready`，并把每个 beat 的地址用 `out_addr_offset` 重算成 64 B 对齐的 AXI 地址。

cvt 负责真正生成 AXI 并写 cq（详见 4.4 节）。

#### 4.2.4 代码实践

1. **目标**：理解 bpt 的“信用反压”与“事务切分”两件事，并确认它们互相独立。
2. **步骤**：
   - 在 `NV_NVDLA_MCIF_READ_ig.v` 里找到 10 个 bpt 例化，列表记录每路的 `tieoff_axid` 与 `tieoff_lat_fifo_depth`。
   - 在 `NV_NVDLA_MCIF_READ_IG_bpt.v` 里分别定位 `lat_count_*`（信用）与 `is_ftran/is_mtran/is_ltran`（切分）两组逻辑。
3. **观察**：cdma_dat（u_bpt8）与 cdma_wt（u_bpt9）的 `tieoff_lat_fifo_depth` 都是 `8'd0`，且它们的 `dma2bpt_cdt_lat_fifo_pop` 被接到常量 0（[NV_NVDLA_MCIF_READ_ig.v:318-343](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_ig.v#L318-L343)），说明这两路完全不走信用机制。
4. **预期结果**：你能解释“bpt 的反压开关由 `tieoff_lat_fifo_depth` 决定，切分逻辑对所有路都生效”这一结论。

> 待本地验证：若你能跑仿真，可在 bpt 内 `lat_count_cur` 上加波形，观察某一路灌满后 `req_enable` 是否被拉低。

#### 4.2.5 小练习与答案

- **练习 1**：`bpt` 和 `spt` 都在做“拆分”，区别是什么？
  - **答**：bpt 把一个 DMA 请求按 **256 B 行边界**拆成若干个“事务”（带 ftran/ltran 标记，且不跨行）；spt 把单个事务按 **AXI beat** 拆成逐拍地址送给 cvt。bpt 还做信用反压，spt 不做。
- **练习 2**：为什么 cdma_dat/cdma_wt 的 `tieoff_lat_fifo_depth = 0`？
  - **答**：卷积数据通路（CBUF/CSC/CACC）有自身的反压链（accu2sc_credit 等），不需要 MCIF 再加一层信用；设为 0 即 `lat_fifo_stall_enable=0`，关闭 bpt 信用反压。

### 4.3 cq 命令队列：共享 RAM 存上下文

#### 4.3.1 概念说明

AXI 允许**多笔未完成（outstanding）事务**同时在路上，且返回顺序可能乱序（按 id 区分）。egress 收到一笔 R 数据时，必须知道“这拍数据该怎么拼”——是首拍要丢半行？还是末拍？要不要 swizzle？

这些信息在 cvt 发命令时就已知，但 R 数据要很多拍之后才回来。**cq 就是一个“记事本”**：cvt 发命令时把上下文写进去，egress 收响应时按 id 取出来。这样 cvt 和 eg 就不必各自保存状态，只需共享一块 RAM。

cq 的设计要点：

- **单写口、多读口**：写口接 cvt（1 路），读口按引擎分（读路径 10 路、写路径 5 路），每个引擎线程在自己的读口上“弹出”属于自己的上下文。
- **共享存储**：只有一块 `nv_ram_rws_256x7`（256 行 × 7 bit），所有线程共用，靠一个 255-bit 的 `free_adr_mask` 管理“哪些 RAM 行是空的”。
- **深度即 outstanding 上限**：最多 256 笔在途事务。

#### 4.3.2 核心流程

cq 内部维护：

- `cq_wr_count`（9 bit）：当前在途事务总数，到 256 就 `cq_wr_busy`，反压 cvt 别再写。
- `free_adr_mask`（255 bit one-hot）：每一位代表一个 RAM 行是否空闲。写入时 `free_adr_index` 给出第一个空闲行号作为写地址；该路引擎在 egress 消费完这笔上下文时归还（对应位置 1）。
- `nv_ram_rws_256x7 ram`：写入 `cq_wr_pd`（7 bit 上下文），按 `cq_rd_adr` 读出。

上下文 7-bit 的字段（由 cvt 打包，见 4.4）：`{ ldrop, fdrop, ltran, odd, swizzle, lens[1:0] }`。

#### 4.3.3 源码精读

cq 顶层与端口见 [NV_NVDLA_MCIF_READ_cq.v:14-97](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_cq.v#L14-L97)：1 个写口 `cq_wr_*`，10 个读口 `cq_rd0..9_*`，每个读口 7-bit `pd`。

核心 RAM 例化（[NV_NVDLA_MCIF_READ_cq.v:208-217](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_cq.v#L208-L217)）：

```verilog
nv_ram_rws_256x7 #(...) ram (
      .clk        ( nvdla_core_clk )
    , .wa         ( cq_wr_adr )      // 写地址 = 空闲行号
    , .we         ( wr_pushing )
    , .di         ( cq_wr_pd )       // 写入 7-bit 上下文
    , .ra         ( cq_rd_adr_p )
    , .re         ( rd_enable )
    , .dout       ( cq_rd_pd_p )     // 读出上下文给对应引擎
);
```

空闲行管理：`cq_wr_adr = free_adr_index`，`free_adr_mask` 是 255-bit 寄存器，每一位在写入时清 0、在对应引擎消费（`rd_popping`）时置 1（[NV_NVDLA_MCIF_READ_cq.v:235-255](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_cq.v#L235-L255)）。该文件 256~1327 行的大段 `if` 就是把 255 位一位一位地更新（自动生成的展开代码），原理相同。

在途计数与满判定（[NV_NVDLA_MCIF_READ_cq.v:130-169](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_cq.v#L130-L169)）：

```verilog
assign cq_wr_prdy = !cq_wr_busy_int;          // 没满才收
wire cq_wr_busy_next = wr_count_next_is_256 || ...;
```

> 注：cq 内部还接了 `NV_CLK_gate_power`（[NV_NVDLA_MCIF_READ_cq.v:112-116](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_cq.v#L112-L116)），在无活动时可门控时钟以省电，这是 u6-l1 会讲的 slcg 机制。

#### 4.3.4 代码实践

1. **目标**：理解 cq 是“单 RAM 多线程共享 + 按线程读口弹出”的设计。
2. **步骤**：在 `NV_NVDLA_MCIF_READ_cq.v` 中确认：写口只有 1 个（`cq_wr_*`），读口有 10 个（`cq_rd0..9_*`）；找到唯一的 `nv_ram_rws_256x7 ram` 例化。
3. **观察**：RAM 只有 256 行，但 10 个引擎线程共用——说明“线程”不是物理分队列，而是逻辑上靠 `free_adr_mask` 分配与归还。
4. **预期结果**：能用一句话说清“cq 如何用一块 RAM 服务 10 个可能并发的事务流”。

#### 4.3.5 小练习与答案

- **练习 1**：cq 最多能存多少笔在途事务？由什么决定？
  - **答**：256 笔（RAM 为 `256x7`，`cq_wr_count` 到 256 即 `cq_wr_busy`）。另外 cvt 还有一个独立的 `os_cnt` 上限（见 4.4），两者共同约束 outstanding。
- **练习 2**：为什么 cq 用“单 RAM + 空闲位图”而不是给每个引擎一个独立 FIFO？
  - **答**：共享 RAM 面积更省、利用率更高（忙的引擎可借用空闲引擎的容量）；按 id 写入、按线程读口弹出即可保证每引擎只取到自己的上下文。

### 4.4 cvt：协议转换与上下文写入

#### 4.4.1 概念说明

cvt 是 IG 的最后一级，做两件事：(1) 把内部命令翻译成标准 AXI AR；(2) 把这笔事务的上下文写进 cq。同时它还维护一个独立的 **outstanding 计数 `os_cnt`**，受 CSB 寄存器 `reg2dp_rd_os_cnt` 约束，防止未完成事务数失控。

#### 4.4.2 核心流程

- 地址对齐：`axi_addr = cmd_addr & 64'hffff_ffff_ffff_ffc0`（强制低 6 位为 0，即 64 B 对齐）。
- 长度：`axi_len = cmd_size[2:1] + inc`（`inc` 处理边界跨行的情况）。
- id：`arid = {4'b0, cmd_axid}`（高 4 位补 0，低 4 位是引擎号 0..9）。
- 上下文打包：`cq_wr_pd = { ldrop, fdrop, ltran, odd, swizzle, lens[1:0] }`，`cq_wr_thread_id = cmd_axid`。
- outstanding 反压：`cq_wr_pvld = cmd_vld & axi_cmd_rdy & !os_cnt_full`，三者（命令有效、AXI 可收、未超 outstanding）同时成立才真正下发。

#### 4.4.3 源码精读

AXI 信号生成在 [NV_NVDLA_MCIF_READ_IG_cvt.v:239-244](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_IG_cvt.v#L239-L244)：

```verilog
assign axi_axid  = cmd_axid;
assign axi_addr  = cmd_addr & 64'hffff_ffff_ffff_ffc0;   // 64B 对齐
assign inc = cmd_ftran & cmd_ltran & (cmd_size[0]==1) & cmd_swizzle;
assign {mon_axi_len_c, axi_len[1:0]} = cmd_size[2:1] + inc;
```

上下文写入 cq 与三路互锁（[NV_NVDLA_MCIF_READ_IG_cvt.v:302-320](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_IG_cvt.v#L302-L320)）：

```verilog
assign cq_wr_pvld = cmd_vld & axi_cmd_rdy & !os_cnt_full;   // 互锁: 命令/AXI/outstanding
...
assign ig2cq_fdrop = cmd_ftran & stt_addr_is_32_align;      // 首拍要丢半行?
assign ig2cq_ldrop = cmd_ltran & end_addr_is_32_align;      // 末拍要丢半行?
assign cq_wr_pd[1:0] = ig2cq_lens[1:0];
assign cq_wr_pd[2]   = ig2cq_swizzle;
...
assign cq_wr_thread_id = cmd_axid;                           // 上下文归属哪个引擎
```

outstanding 计数（[NV_NVDLA_MCIF_READ_IG_cvt.v:326-345](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_IG_cvt.v#L326-L345)）：每发一笔 AXI 加 `axi_len+1`，每收到一个 eg 的反馈（`eg2ig_axi_vld`）减 1，超过 `reg2dp_rd_os_cnt` 即 `os_cnt_full`。

AXI AR 对外输出经一级 skid pipe（[NV_NVDLA_MCIF_READ_IG_cvt.v:459-485](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_IG_cvt.v#L459-L485)），把 `arid/araddr/arlen` 送出顶层 `mcif2noc_axi_ar_*`。

#### 4.4.4 代码实践

1. **目标**：看清“一笔内部命令如何同时产生 AXI AR 与 cq 上下文写”。
2. **步骤**：在 `NV_NVDLA_MCIF_READ_IG_cvt.v` 里跟踪 `cmd_vld` 这一个信号——它同时驱动 `cq_wr_pvld`（写 cq）和 `axi_cmd_vld`（发 AXI），且都被 `!os_cnt_full` 门控。
3. **观察**：`cq_wr_thread_id = cmd_axid`，意味着上下文与 AXI `arid` 用同一个引擎号绑定，egress 正是靠 R 通道的 `rid` 找回这个号。
4. **预期结果**：能解释“为什么 AR 和 cq 写必须原子发生”（否则数据回来时找不到上下文）。

#### 4.4.5 小练习与答案

- **练习 1**：`axi_addr` 为什么要 `& ...ffc0`？
  - **答**：强制 64 B 对齐（低 6 位清零），满足 AXI 对 `ARSIZE`/对齐的要求；半行取舍交给 `fdrop/ldrop` 在 egress 处理。
- **练习 2**：`os_cnt` 与 cq 的 `cq_wr_count` 都是 outstanding 限制，它们冲突吗？
  - **答**：不冲突，是两层保护。`os_cnt` 限总未完成 AXI 事务数（按 beat 计，受 `reg2dp_rd_os_cnt` 配置）；`cq_wr_count` 限 cq RAM 占用（≤256）。任一满都会反压。

### 4.5 egress：按 id 分发、重排与掩码

#### 4.5.1 概念说明

egress（eg）接收 AXI R 通道返回的 512-bit 数据，要做四件事：

1. **按 id demux**：根据 `rid`（低 4 位 = axid）把数据分流到 10 个 `lat_fifo`（每引擎一个）。
2. **回读上下文**：从 cq 的对应读口取出这笔事务的 `swizzle/odd/ltran/fdrop/ldrop`。
3. **重排（swizzle）与掩码（drop）**：按上下文把 512-bit 拆成两个 256-bit 半行，必要时互换（swizzle）、丢弃无效半行（mask）。
4. **轮询输出**：10 个引擎的数据经一个 RR 仲裁器轮流送到对外响应口。

注意 egress 的仲裁是**纯轮询（RR），不带权重**——这与 ingress 的加权轮询不同（源码里有注释明说）。

#### 4.5.2 核心流程

```text
noc2mcif_axi_r (512b data + rid) ──► 按 rid 选通 ──► lat_fifo<axid> ──► (cq 取上下文) ──►
   read_eg_arb (10→1 RR) ──► 按 src_gnt 选数据 ──► ro_fifo (2 deep) ──► 拼mask+data ──► mcif2*_rd_rsp_pd(514b)
```

- 每个 `lat_fifo` 缓存本引擎返回的 512-bit 数据拍。
- `read_eg_arb` 是 10 输入 RR，权重全部为 `8'hFF`（等价于等权轮询）。
- 仲裁选中哪路（`srcN_gnt`），就把那路的 `rqN_rd_pd`（512b）和 `cttN_cq_pd`（来自 cq 的 7b 上下文）一起选出。
- 用上下文里的 `fdrop/ldrop` 算 `arb_wen`（2 bit，哪半行有效），用 `swizzle` 决定半行高低位是否互换，拼成 `{mask[1:0], data[511:0]}` = 514-bit 响应。

#### 4.5.3 源码精读

按 id demux 写入 10 个 lat_fifo（[NV_NVDLA_MCIF_READ_eg.v:643-656](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_eg.v#L643-L656)）：

```verilog
assign noc2mcif_axi_r_pd = {noc2mcif_axi_r_rid[3:0], noc2mcif_axi_r_rdata};
...
assign rq0_wr_pvld = ipipe_axi_vld & (ipipe_axi_axid == 0);   // id==0 的数据进 lat_fifo0
assign rq0_wr_pd   = rq_wr_pd;
```

`read_eg_arb` 的权重全部相等（[NV_NVDLA_MCIF_READ_eg.v:836-870](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_eg.v#L836-L870)）：

```verilog
read_eg_arb u_read_eg_arb (
   .req0(src0_req) ... .req9(src9_req)
  ,.wt0({8{1'b1}})  ... .wt9({8{1'b1}})   // 等权 → 纯 RR
  ,.gnt0(src0_gnt) ... .gnt9(src9_gnt)
);
// NOTE:ezhang, we dont need Weighted But only RR in EG side
```

按 `srcN_gnt` 选数据与上下文的大 case 在 [NV_NVDLA_MCIF_READ_eg.v:881-1022](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_eg.v#L881-L1022)。swizzle 与 mask 拼装（[NV_NVDLA_MCIF_READ_eg.v:1032-1057](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_eg.v#L1032-L1057)）：

```verilog
arb_data0 = arb_data[255:0];
arb_data1 = arb_data[511:256];
arb_data0_swizzled = arb_cq_swizzle ? arb_data1 : arb_data0;   // 半行互换
...
if (arb_first_beat && arb_cq_fdrop) arb_wen = 2'b10;           // 首拍丢低半行
else if (arb_last_beat && arb_cq_ldrop) arb_wen = 2'b01;       // 末拍丢高半行
else arb_wen = 2'b11;                                          // 两半行都有效
```

之后每个引擎有一对 `ro_fifo`（存两个半行）+ 一级 pipe 输出（[NV_NVDLA_MCIF_READ_eg.v:1065-1113](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_eg.v#L1065-L1113) 是 bdma 的一路，其余 9 路结构相同），最终送出 `mcif2bdma_rd_rsp_pd[513:0]`（2 bit mask + 512 bit data）。

egress 与 cq 的衔接在 [NV_NVDLA_MCIF_READ_eg.v:1593-1677](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_eg.v#L1593-L1677)：每个线程有一个 `cttN`（context）小状态机，当本线程的数据在 RR 中被选中且发完最后一拍（`cttN_last_beat`）时，才向 cq 的对应读口回 `cq_rdN_prdy`，从而“弹出”并归还这一条上下文。

#### 4.5.4 代码实践

1. **目标**：把“数据回来 → 按 id 进 lat_fifo → 取上下文 → swizzle/mask → 输出”这条链走通。
2. **步骤**：在 `NV_NVDLA_MCIF_READ_eg.v` 中跟踪 id==0（bdma）这一路：`rq0_wr_pvld`（入 lat_fifo0）→ `src0_req/src0_gnt`（参与 RR）→ `dma0_*`（拼装）→ `mcif2bdma_rd_rsp_*`（输出）。
3. **观察**：`read_eg_arb` 的权重全是 1，确认 egress 是纯 RR；对比 ingress arb 的 `reg2dp_rd_weight_*` 加权。
4. **预期结果**：能解释“为什么读响应是 514 bit 而数据只有 512 bit”——多出的 2 bit 是 mask，告诉引擎哪半行有效。

> 待本地验证：可在 `arb_wen` 与 `dma0_pd` 上加波形，构造一个首地址 32 B 对齐（触发 `fdrop`）的请求，观察首拍 mask 是否为 `2'b10`。

#### 4.5.5 小练习与答案

- **练习 1**：为什么 egress 用 RR 而不像 ingress 那样加权？
  - **答**：ingress 加权是为了**调度**谁先发请求（可配优先级，控制引擎间带宽）；egress 只是**按到达顺序把已有数据送出**，没必要再排序，RR 已足够公平。
- **练习 2**：`cttN_last_beat` 触发 `cq_rdN_prdy` 的作用是什么？
  - **答**：一笔事务的所有数据拍都送出后，才从 cq 弹出（消费）这条上下文并归还 RAM 行，保证上下文生命周期恰好覆盖整笔事务。

## 5. 综合实践

**任务：完整追踪一笔 MCIF 读请求从引擎到响应的旅程，并标注每级做了什么。**

设 BDMA 要从地址 `0x...0040`（64 B 对齐、但相对 256 B 行是第 2 个 32 B 半行，即 `stt_offset=2`）读 3 个 256 B 块。

1. **入口**：BDMA 发 `bdma2mcif_rd_req_pd = {size=2, addr=0x...0040}`，`bdma2mcif_rd_req_valid=1`。
2. **bpt0**（bdma，axid=0，depth=245）：
   - 事务切分：算出首/中/尾事务，逐个发 `bpt2arb_req0_pd`（带 `ftran/ltran/swizzle/odd`）；检查 `lat_fifo_free_slot` 足够才置 `req_enable`。
3. **arb**：bdma 这路与其它 9 路按 `reg2dp_rd_weight_bdma` 权重竞争，胜出后送 `arb2spt_req_pd`。
4. **spt**：把选中的事务按 `beat_count` 拆成逐拍 64 B 对齐地址，送 `spt2cvt_req_pd`。
5. **cvt**：生成 `mcif2noc_axi_ar_araddr/arid=0/arlen`；同时把 `{ldrop,fdrop,ltran,odd,swizzle,lens}` 写入 cq 的 `thread_id=0` 项；检查 `os_cnt` 未满。
6. **AXI/DBB**：AR 发出，DBB 返回 `noc2mcif_axi_r_rdata`（512b），`rid=0`。
7. **eg**：`rid==0` → 数据进 `lat_fifo0`；`ctt0` 从 `cq_rd0` 取回上下文；RR 选中 src0 → 按 `fdrop/swizzle` 算 `arb_wen` 与 `dma0_data` → 经 `ro_fifo0`/`pipe_p2` 输出 `mcif2bdma_rd_rsp_pd[513:0]`（mask+data）与 `valid`。
8. **收尾**：bdma 这笔事务末拍发出后，`ctt0_last_beat` 触发 `cq_rd0_prdy`，弹出并归还 cq 中该条上下文。

**交付物**：画一张包含以上 8 步的时序/数据流图，并在 bpt 与 spt 两处分别注明“bpt 解决信用反压与行边界切分、spt 解决 AXI 拍级拆分”。

> 待本地验证：如能跑 sanity trace（见 u1-l4），用 `DUMP=1 DUMPER=VERDI` 抓波形，在 `mcif2noc_axi_ar_arvalid` 与 `mcif2bdma_rd_rsp_valid` 之间对照观察上述信号。

## 6. 本讲小结

- MCIF 每个方向是 **IG → cq → eg** 三级：IG 把多路请求仲裁成一路 AXI，cq 存事务上下文，eg 按 id 把数据送回各引擎。
- **READ 有 10 个客户端、WRITE 有 5 个**，因为只有产出数据的引擎才写。
- IG 内部四级：**bpt（背压+行切分）→ arb（加权轮询）→ spt（拍级拆分）→ cvt（生成 AXI+写 cq）**。
- **bpt 与 spt 都做拆分但对象不同**：bpt 按 256 B 行切事务并做信用反压，spt 按 AXI beat 拆拍；`tieoff_lat_fifo_depth=0` 可关闭某路信用（cdma_dat/cdma_wt 即如此）。
- **cq 是单块 256×7 共享 RAM**，单写口多读口，靠 255-bit 空闲位图分配/归还；存放 `{ldrop,fdrop,ltran,odd,swizzle,lens}` 上下文，按 `thread_id=axid` 归属。
- **egress 按 rid demux 到 10 个 lat_fifo，再经纯 RR 仲裁输出**；用上下文里的 `swizzle/fdrop/ldrop` 做半行重排与掩码，最终产出 514-bit（2 mask + 512 data）读响应。

## 7. 下一步学习建议

- **u4-l3（CVIF）**：对照 CVIF 的 `CVIF_READ_ig/cq/eg`，体会 MCIF 与 CVIF 的同构性，以及 CVSRAM 场景下哪些路径被简化。
- **u4-l4（BDMA）**：看 BDMA 如何作为独立 DMA 引擎调用本讲讲的 MCIF/CVIF 通路在两套存储间搬数据。
- **u6-l2（FIFO 与 vlibs 原语）**：本讲反复出现的 `pipe_p*`（skid buffer + bubble collapse）、`lat_fifo`、`ro_fifo`、`read_eg_arb` 都来自 vlibs/XXIF_libs，学完会更清楚这些原语的统一形态。
- **u8-l1（spec/defs）**：本讲的 `reg2dp_rd_os_cnt`、`reg2dp_rd_weight_*` 都来自 CSB 寄存器，而这些寄存器的位宽/存在性由 spec 宏决定，可回去对照规格源头。
