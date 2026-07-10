# 存储接口架构总览：MCIF/CVIF 与读写通路

## 1. 本讲目标

本讲是「存储接口子系统」单元的第一篇，目标是让读者从宏观上建立 NVDLA **对外存储访问**的整体地图。学完后你应当能够：

1. 说清 NVDLA 为什么有 **两套**存储接口：主存接口 MCIF（primary，连片外 DBB/主存）和卷积 SRAM 接口 CVIF（secondary，连片上 CVSRAM）。
2. 解释每套接口内部为什么都拆成 **READ 与 WRITE 两条独立通路**，以及它们各自对应 AXI 的哪些通道。
3. 画出每条通路通用的 **IG（ingress）→ cq（命令队列）→ eg（egress）三级流水**结构，并说清 IG 内部 `bpt → arb → spt → cvt` 四级子模块各自的职责。
4. 明白 MCIF 与 CVIF 为什么能共享同一套 `XXIF_libs` 原语（核心是加权轮询仲裁器）。
5. **纠正一个常见误解**：`partition_m` 并不是存储接口分区，而是卷积乘加阵列（CMAC）；真正容纳 MCIF/CVIF 的是中央枢纽 `partition_o`。

---

## 2. 前置知识

在进入存储接口前，请确认你已经理解以下几个概念（它们在前置讲义中已建立）：

- **加速器与存储的分离**：NVDLA 的计算引擎（卷积、后处理）本身不存储权重和特征图，这些数据放在芯片外部。引擎需要数据时，必须由专门的接口把数据从外部「搬」进来，算完再「搬」出去。
- **AXI 总线五通道**：读事务用 **AR**（读地址）+ **R**（读数据）两个通道；写事务用 **AW**（写地址）+ **W**（写数据）+ **B**（写响应）三个通道。每个通道都是独立的 `valid/ready` 握手。
- **DMA 请求**：引擎向接口发起的一次「请帮我读/写一段地址」的请求，通常用 `req_valid/req_ready/req_pd`（valid/ready/包数据）三件套握手，与 AXI 的握手在概念上类似但格式不同。
- **背压（backpressure）与信用（credit）**：当下游来不及接收时，通过拉低 `ready` 反压上游；为了避免上游无限发包把接口塞爆，常用「信用计数」限定在途（outstanding）请求数量。
- **分区（partition）**：顶层 `NV_nvdla.v` 把设计按功能/时钟域切成 `partition_o/c/ma/mb/a/p` 等实例（见 u1-l5）。**重要**：本仓库里 `partition_m` 模块被例化两次为 `u_partition_ma`、`u_partition_mb`，它们是卷积乘加阵列 **CMAC 的两半**，与存储接口无关。

> 关于最后一点：NVDLA 早期文档里流传过「a=配置、m=存储接口」的口诀，但**对本仓库源码不成立**。本讲会反复用源码澄清：MCIF 与 CVIF 都在 `partition_o` 里。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `vmod/nvdla/nocif/NV_NVDLA_mcif.v` | MCIF 顶层：例化 `u_read`/`u_write`/`u_csb`，对外接 AXI(DBB)，对内接各引擎 DMA 口。 |
| `vmod/nvdla/nocif/NV_NVDLA_cvif.v` | CVIF 顶层：结构与 MCIF 完全同构，对外接 AXI(CVSRAM)。 |
| `vmod/nvdla/nocif/NV_NVDLA_MCIF_read.v` | MCIF 读通路：例化 `u_ig`/`u_cq`/`u_eg`，把多引擎读请求合成 AXI AR、把 R 通道数据分发回各引擎。 |
| `vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_ig.v` | 读通路 ingress：内部再分 `bpt`×10 → `arb` → `spt` → `cvt` 四级。 |
| `vmod/nvdla/nocif/NV_NVDLA_XXIF_libs.v` | MCIF/CVIF 共享的库原语，核心是 `read_ig_arb`/`read_eg_arb` 加权轮询（wrr）仲裁器。 |
| `vmod/nvdla/top/NV_NVDLA_partition_o.v` | 中央枢纽分区：MCIF（行 1923）、CVIF（行 2108）真正被例化的位置。 |
| `vmod/nvdla/top/NV_NVDLA_partition_m.v` | **纠错参照**：这里只有 CMAC 阵列，没有存储接口。 |
| `vmod/nvdla/top/NV_nvdla.v` | 顶层：`u_partition_ma`（行 2523）、`u_partition_mb`（行 2817）即 CMAC 两半。 |

`nocif` 目录下你能看到清晰的命名对称：把文件名里的 `MCIF` 全部换成 `CVIF`，就是另一套接口的对应文件（`MCIF_read` ↔ `CVIF_read`、`MCIF_READ_IG_bpt` ↔ `CVIF_READ_IG_bpt` …）。这种对称正是「两套接口共享同一套结构」的直接证据。

---

## 4. 核心概念与源码讲解

### 4.1 双存储接口：MCIF（primary）与 CVIF（secondary）

#### 4.1.1 概念说明

NVDLA 面向两类外部存储：

- **主存（DBB / Main Memory）**：容量大但延迟高，通常是片外 DDR。权重、输入特征图、最终输出都放在这里。NVDLA 通过 **MCIF（Memory Controller Interface）** 访问它。
- **卷积 SRAM（CVSRAM）**：片上私有 SRAM，容量小但延迟极低，适合放当前层正在反复读取的「热」数据（例如某层权重）。NVDLA 通过 **CVIF（CVSRAM Interface）** 访问它。

于是 NVDLA 设计了 **两套结构完全相同的接口**：MCIF 是 primary（主），CVIF 是 secondary（次）。哪个引擎的数据走哪一套，由该引擎自己的 DMA 配置决定——同一类请求格式同时存在 `xxx2mcif_*` 和 `xxx2cvif_*` 两套连线，引擎按需选择。

> 为什么不只用一套？因为片外主存延迟高、带宽被多方争抢；把高频重用的数据预取到片上 CVSRAM 能显著降低延迟和功耗。BDMA 引擎（见 u4-l4）正是用来在 DBB 与 CVSRAM 之间搬运数据的。

#### 4.1.2 核心流程

两套接口都遵循同一个「多对一汇入、一对多分发」模型：

```
        bdma ─┐
   cdma_dat ──┤        ┌─── MCIF ────► AXI 五通道 ──► DBB（主存）
   cdma_wt ───┤  read  │   (primary)
   sdp ───────┤ ─────► │
   sdp_b/n/e ─┤        ├─── MCIF ────► AXI 五通道
   pdp ───────┤  write │
   cdp ───────┤ ─────► │
   rubik ─────┘        └──────────────
   （同样的引擎集合也连到 CVIF，再经 AXI 到 CVSRAM）
```

每个引擎既有「读请求 → 读响应」通路，也有「写请求 → 写完成」通路，分别接入 MCIF/CVIF 的 read 与 write 子模块。

#### 4.1.3 源码精读

先看 **MCIF 与 CVIF 真正被例化的位置**。在 `partition_o.v` 中，两段注释直接点明了它们的角色：

[NV_NVDLA_partition_o.v:1920-1923](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v#L1920-L1923) —— `// AXI Interface to MC` 注释下例化 `NV_NVDLA_mcif u_NV_NVDLA_mcif`，即「连主存」的接口。

[NV_NVDLA_partition_o.v:2105-2108](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v#L2105-L2108) —— `// AXI Interface to CVSRAM` 注释下例化 `NV_NVDLA_cvif u_NV_NVDLA_cvif`，即「连片上 CVSRAM」的接口。

再看 **partition_m 的真实内容**，用来证伪「m = 存储接口」：

[NV_NVDLA_partition_m.v:632-637](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_m.v#L632-L637) —— 注释 `// NVDLA Partition M: Convolution MAC Array`，例化的是 `NV_NVDLA_cmac`，端口全是 `sc2mac_dat_*`/`sc2mac_wt_*`（CSC 喂给 MAC 的数据）和 `mac2accu_*`（MAC 送往累加器的部分和）。这与存储接口毫无关系。

最后看顶层如何把 `partition_m` 例化成两个实例：

[NV_nvdla.v:2523](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L2523) 与 [NV_nvdla.v:2817](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L2817) —— `u_partition_ma` 与 `u_partition_mb`，分别对应 CMAC 阵列的 a 半与 b 半（见 u3-l5）。所以 `ma`/`mb` 是 **MAC 两半**，不是「主/副存储接口」。

> 小结：MCIF（→DBB）和 CVIF（→CVSRAM）都在 `partition_o`；`partition_m` 的 ma/mb 是卷积乘加阵列。后续讲到「ma/mb」时请自动脑补为「CMAC 两半」。

#### 4.1.4 代码实践

1. **实践目标**：亲手在源码中确认 MCIF/CVIF 的归属，纠正「m=存储」的误解。
2. **操作步骤**：
   - 打开 `vmod/nvdla/top/NV_NVDLA_partition_o.v`，搜索 `NV_NVDLA_mcif`，记录它所在的行号与上方注释；再搜 `NV_NVDLA_cvif`，同样记录。
   - 打开 `vmod/nvdla/top/NV_NVDLA_partition_m.v`，确认它例化的唯一大模块是 `NV_NVDLA_cmac`，且没有任何 `mcif`/`cvif`/`axi` 字样的端口。
   - 打开 `vmod/nvdla/top/NV_nvdla.v`，搜索 `u_partition_ma`、`u_partition_mb`，确认二者都是 `NV_NVDLA_partition_m` 的实例。
3. **需要观察的现象**：MCIF/CVIF 出现在 `partition_o.v`；`partition_m.v` 里只有 CMAC；`ma`/`mb` 是 CMAC 两半。
4. **预期结果**：你会得到一张「存储接口 → partition_o；乘加阵列 → partition_m(ma/mb)」的正确对照表，并能指出哪段注释（`AXI Interface to MC` / `AXI Interface to CVSRAM`）对应哪套接口。
5. 本实践为源码阅读型，**无需运行仿真**即可完成。

#### 4.1.5 小练习与答案

- **练习 1**：有人说「NVDLA 的两组 AXI 存储接口分别属于 partition_ma 和 partition_mb」，这句话错在哪里？
  - **答案**：`partition_ma`/`partition_mb` 是卷积乘加阵列（CMAC）的两半，不含任何 AXI 存储接口。两组存储接口 MCIF、CVIF 都位于中央枢纽 `partition_o`，分别对应顶层 `core2dbb`（→DBB）和 `core2cvsram`（→CVSRAM）两组 AXI 端口。
- **练习 2**：为什么需要 CVIF 而不是只用 MCIF？
  - **答案**：片外主存延迟高、带宽争抢激烈；把高复用数据（如当前层权重）预取到片上低延迟 CVSRAM 可降低访问延迟与功耗。CVIF 就是访问 CVSRAM 的专用接口，结构上与 MCIF 同构。

---

### 4.2 READ 与 WRITE 双通路

#### 4.2.1 概念说明

无论 MCIF 还是 CVIF，内部都进一步拆成两条**相互独立**的通路：

- **READ 通路**：收集各引擎的**读请求**，合成 AXI **AR** 通道发出，再从 AXI **R** 通道收数据，分发回各引擎。
- **WRITE 通路**：收集各引擎的**写请求**（带地址 + 数据），合成 AXI **AW**（写地址）+ **W**（写数据）通道发出，并从 AXI **B** 通道收写响应，回告各引擎「写完成」。

之所以读写分离，是因为读请求只有地址、响应是数据，而写请求同时带地址和数据、响应只是一个完成信号——二者数据宽度、握手节奏完全不同，独立通路更便于仲裁与流控。

#### 4.2.2 核心流程

以 MCIF 为例，顶层 `NV_NVDLA_mcif` 例化三个子模块，把读写拆开：

```
NV_NVDLA_mcif
  ├── NV_NVDLA_MCIF_read   u_read    ◄── 各引擎 *2mcif_rd_req_*  ──► AR / R
  ├── NV_NVDLA_MCIF_write  u_write   ◄── 各引擎 *2mcif_wr_req_*  ──► AW / W / B
  └── NV_NVDLA_MCIF_csb    u_csb     ◄── csb2mcif_req_*（CSB 配置：仲裁权重、os_cnt 等）
```

AXI 五通道在顶层端口上的归属：

| AXI 通道 | 方向 | 归属通路 | 关键端口（MCIF） |
| --- | --- | --- | --- |
| AR（读地址） | mcif→noc | READ | `mcif2noc_axi_ar_arvalid/araddr/arid/arlen` |
| R（读数据） | noc→mcif | READ | `noc2mcif_axi_r_rvalid/rdata/rid/rlast` |
| AW（写地址） | mcif→noc | WRITE | `mcif2noc_axi_aw_awvalid/awaddr/awid/awlen` |
| W（写数据） | mcif→noc | WRITE | `mcif2noc_axi_w_wvalid/wdata/wstrb/wlast` |
| B（写响应） | noc→mcif | WRITE | `noc2mcif_axi_b_bvalid/bid` |

注意数据宽度：读响应 `rdata` 与写数据 `wdata` 都是 **512 位**（一次搬一个 64 字节的原子块）；而回给引擎的 `rd_rsp_pd` 是 **514 位**（512 数据 + 2 位附加信息），写请求 `wr_req_pd` 是 **515 位**（包格式 `pkt_widths=78,514`，即 78 位地址头 + 514 位数据/掩码）。

#### 4.2.3 源码精读

[NV_NVDLA_mcif.v:321-414](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_mcif.v#L321-L414) —— 例化读通路 `NV_NVDLA_MCIF_read u_read`。它的左侧连着所有引擎的读请求（如 `bdma2mcif_rd_req_*`、`cdma_dat2mcif_rd_req_*`、`sdp2mcif_rd_req_*`…），右侧连出 AR 通道（`mcif2noc_axi_ar_*`）和 R 通道（`noc2mcif_axi_r_*`）。

[NV_NVDLA_mcif.v:415-458](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_mcif.v#L415-L458) —— 例化写通路 `NV_NVDLA_MCIF_write u_write`。左侧是各引擎写请求（如 `bdma2mcif_wr_req_*`，515 位），右侧连出 AW、W 通道并接收 B 通道。

[NV_NVDLA_mcif.v:460-491](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_mcif.v#L460-L491) —— 例化 CSB 配置口 `NV_NVDLA_MCIF_csb u_csb`。它把寄存器里的仲裁权重（`reg2dp_rd_weight_*`、`reg2dp_wr_weight_*`）和在途计数上限（`reg2dp_rd_os_cnt`、`reg2dp_wr_os_cnt`）分别下发给 read/write。注意 `dp2reg_idle` 被直接接到 `1'b1`（恒空闲占位），说明这套接口没有复杂的空闲状态上报。

> CVIF 的结构与之**逐字对称**：[NV_NVDLA_cvif.v:321-414](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_cvif.v#L321-L414) 例化 `NV_NVDLA_CVIF_read`，端口名把 `mcif` 换成 `cvif` 即可。

#### 4.2.4 代码实践

1. **实践目标**：把 AXI 五通道逐一对应到 read/write 通路，并验证读写通路的引擎集合是否一致。
2. **操作步骤**：
   - 在 `NV_NVDLA_mcif.v` 中，分别统计 `u_read` 与 `u_write` 各连接了哪些引擎（按 `*2mcif_rd_req_*` / `*2mcif_wr_req_*` 前缀）。
   - 把 AR/R/AW/W/B 五个通道的端口名抄下来，标注它们属于 read 还是 write。
3. **需要观察的现象**：读通路服务的引擎比写通路多（例如 `cdma_dat`、`cdma_wt` 只有读请求，没有写请求，因为卷积取数只读不写）。
4. **预期结果**：你会看到 `u_read` 同时接 `cdma_dat2mcif_rd_req_*` 和 `cdma_wt2mcif_rd_req_*`，但 `u_write` 里没有对应的 cdma 写请求——卷积引擎只读外部存储、把结果留在内部，写回由 SDP/PDP 等后处理引擎完成。
5. 源码阅读型实践，**待本地验证**你对引擎集合的统计。

#### 4.2.5 小练习与答案

- **练习 1**：读数据是 512 位，为什么回给引擎的 `rd_rsp_pd` 是 514 位？多出的 2 位可能是什么？
  - **答案**：多出的位用于携带每笔 64 字节原子块的附加元信息（例如有效字节掩码、错误标志等），让接收引擎知道这拍数据里哪些字节有效。具体语义需结合引擎侧解码（待进一步阅读引擎 RDMA 确认）。
- **练习 2**：为什么 CDMA 的两个读口（dat、wt）只出现在 read 通路，不在 write 通路？
  - **答案**：CDMA 是卷积取数引擎，职责是把输入特征图与权重**读**进来；卷积结果并不由 CDMA 写回外部存储，而是交给 CACC→SDP，最终由后处理引擎写回。所以 CDMA 只用读通路。

---

### 4.3 IG → cq → eg 三级流水结构

#### 4.3.1 概念说明

READ 与 WRITE 通路各自内部都遵循一个统一的**三级流水**：

- **IG（ingress，入口）**：把多个引擎并发到达的请求，仲裁成**单一**串行流，并翻译成 AXI 命令（AR 或 AW）。
- **cq（command queue，命令队列）**：记住「每一个在途的 AXI 事务属于哪个引擎/线程」，这样当数据或响应回来时，能按 AXI 的 `id` 路由回正确的引擎。
- **eg（egress，出口）**：接收 AXI 返回的数据（R 通道）或写响应（B 通道），借助 cq 提供的路由信息，分发回各引擎。

这套结构解决的核心矛盾是：**N 个引擎并发请求 → 1 条 AXI 总线串行传输 → N 个引擎并发接收**。IG 负责「多合一路」，cq 负责「记住来源」，eg 负责「一路分发回多」。

#### 4.3.2 核心流程

以 READ 通路为例（WRITE 同理，只是没有 R 通道、多了 W/B）：

```
                       ┌─ cq_rd0 ─┐
  bdma  ─►┐            │ cq_rd1   │            ┌─► mcif2bdma_rd_rsp
  sdp   ─►│            │ ...      │  rid 路由  ├─► mcif2sdp_rd_rsp
  pdp   ─►│ IG(仲裁)   │ cq_rd9   │ ◄──────── │eg ◄── noc2mcif_axi_r
  ...  ─►│ ──► AR      └──────────┘   (eg 读cq)└─► mcif2cdma_dat_rd_rsp ...
  cdma ─►│   cq_wr ◄── IG 同时登记线程 id
```

关键点：IG 在发 AR 的同时，向 cq 写入一条「线程 id + 元信息」（`cq_wr_*`）；当 R 通道数据带着 `rid` 回来时，eg 用 `rid` 去查 cq 的 10 个读线程槽（`cq_rd0`..`cq_rd9`），命中哪个就把数据送给对应引擎。eg 还会通过 `eg2ig_axi_vld` 反馈给 IG，用于在途数量流控。

#### 4.3.3 源码精读

[NV_NVDLA_MCIF_read.v:286-348](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_read.v#L286-L348) —— 例化 ingress `NV_NVDLA_MCIF_READ_ig u_ig`。它一边接收所有引擎读请求，一边向仲裁后发出 AR 通道，并把线程信息写入 cq（`cq_wr_pvld/cq_wr_thread_id/cq_wr_pd`）。

[NV_NVDLA_MCIF_read.v:349-387](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_read.v#L349-L387) —— 例化命令队列 `NV_NVDLA_MCIF_READ_cq u_cq`。它内部维护 **10 个读线程**（`cq_rd0`..`cq_rd9`）与 1 个写口（`cq_wr_*`），每个线程槽 7 位（`cq_rdN_pd[6:0]`）。线程号正好对应 10 个引擎，这就是为什么 AR 的 `arid` 宽度为 8 位、而 bpt 给每个引擎分配的 `axid` 只有 4 位（0..9）。

[NV_NVDLA_MCIF_read.v:388-458](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_read.v#L388-L458) —— 例化 egress `NV_NVDLA_MCIF_READ_eg u_eg`。它接收 R 通道（`noc2mcif_axi_r_*`），按 `rid` 查 cq，再向各引擎发 `mcif2<engine>_rd_rsp_*`；同时回送 `eg2ig_axi_vld` 给 ingress 做流控。

> cq 的「10 读线程 + 1 写线程」结构是理解整条通路的关键：**线程号 = 引擎编号 = AXI id 的来源**。这也意味着同一时刻在途的同一引擎事务数量，受 cq 该线程槽深度限制。

#### 4.3.4 代码实践

1. **实践目标**：跟踪一次读请求从 IG 到 eg 的完整旅程，验证 cq 的「线程号 ↔ 引擎」对应关系。
2. **操作步骤**：
   - 在 `NV_NVDLA_MCIF_read.v` 中，确认 `u_ig`、`u_cq`、`u_eg` 三者之间的连线：IG→cq（`cq_wr_*`）、cq→eg（`cq_rd0..9_*`）、eg→IG（`eg2ig_axi_vld`）。
   - 结合 4.4 节 bpt 给每个引擎分配的 `tieoff_axid`，把 10 个 axid 与引擎名一一对应（0=bdma, 1=sdp, 2=pdp, 3=cdp, 4=rbk, 5=sdp_b, 6=sdp_n, 7=sdp_e, 8=cdma_dat, 9=cdma_wt）。
3. **需要观察的现象**：eg 用 `noc2mcif_axi_r_rid` 选择 `cq_rdN`，再把数据发往对应引擎的 `mcif2<engine>_rd_rsp_*`。
4. **预期结果**：你能画出一张表，说明 `arid==0` 的读数据最终送到 `mcif2bdma_rd_rsp_*`，`arid==8` 送到 `mcif2cdma_dat_rd_rsp_*`，依此类推。
5. 源码阅读型实践；具体 axid 与引擎的精确映射以 `NV_NVDLA_MCIF_READ_ig.v` 中各 `u_bptN` 的 `tieoff_axid` 为准（**待本地验证**无重排）。

#### 4.3.5 小练习与答案

- **练习 1**：cq 里为什么是 10 个读线程，而不是 1 个？
  - **答案**：因为有 10 类并发读客户端（bdma/sdp/pdp/cdp/rbk/sdp_b/sdp_n/sdp_e/cdma_dat/cdma_wt），每类在途事务的状态需要独立追踪，才能用 AXI `rid` 把返回数据路由回正确的引擎。1 个线程槽无法区分来源。
- **练习 2**：`eg2ig_axi_vld` 这根从 eg 反馈回 IG 的线，起什么作用？
  - **答案**：egress 把「AXI 侧是否有有效数据在处理」告知 ingress，配合 `reg2dp_rd_os_cnt`（在途计数上限）做反压，防止 IG 在 AXI 侧已经拥塞时继续发 AR，避免在途事务数失控。

---

### 4.4 IG 内部子级 bpt / arb / spt / cvt 分工

#### 4.4.1 概念说明

ingress（IG）本身又是一条四级小流水，每一级解决一个具体问题：

- **bpt（back-pressure / 背压级）**：**每引擎一个**（共 10 个）。负责接收该引擎的 DMA 读请求，管理一个**信用/延迟 FIFO**（`cdt_lat_fifo`，深度由 `tieoff_lat_fifo_depth` 指定），用来做该引擎自己的在途背压；同时把多拍的大请求**切分成单笔事务**（首笔 ftran / 末笔 ltran），并给该引擎打上固定的 **AXI id**（`tieoff_axid`）。
- **arb（arbiter / 仲裁级）**：把 10 路 bpt 输出**加权轮询**（weighted round-robin）成单一串行流。每个引擎有一个 8 位权重（`reg2dp_rd_weight_*`，由 CSB 配置），权重为 0 表示禁用该客户端。
- **spt（single-port / 单口级）**：把仲裁选中的那一笔请求用一个 skid 缓冲「稳住」，处理首拍/末拍边界，确保下游 cvt 能在一个稳定窗口内取走完整事务。
- **cvt（conversion / 转换级）**：把内部 75 位请求包**翻译成真正的 AXI AR 通道**（`arvalid/araddr/arid/arlen`），同时把线程信息写入命令队列（`cq_wr_*`），并依据 `eg2ig_axi_vld` 与 `reg2dp_rd_os_cnt` 决定是否继续发。

#### 4.4.2 核心流程

ingress 内部数据流（请求宽度从 79 位 → 75 位逐级规整）：

```
引擎读请求(79b) ─► bpt0..bpt9(切事务+背压+打axid, 75b) ─► arb(wrr 选1) ─► spt(skid稳住) ─► cvt(转AR + 写cq_wr)
                                                                                          │
                                                                                          ▼
                                                                                mcif2noc_axi_ar_*
```

加权轮询的判定式（摘自 `XXIF_libs`）为：只有当某客户端有请求 **且** 其剩余权重非零时才参与仲裁；被选中后该客户端权重减 1，减到 0 轮到下一个客户端。数学上即对客户端集合 \(C\)，每轮从满足 \(\text{req}_i \land (\text{wt}_i \neq 0)\) 的 \(i\) 中按轮转顺序选一个，并令 \(\text{wt}_i \leftarrow \text{wt}_i - 1\)；当所有非零权重耗尽时重新装载。这保证带宽按权重比例在引擎间分配。

#### 4.4.3 源码精读

[NV_NVDLA_MCIF_READ_ig.v:214-226](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_ig.v#L214-L226) —— 例化 `u_bpt0`，连的是 `bdma2mcif_rd_req_*`，`tieoff_axid = 4'd0`，`tieoff_lat_fifo_depth = 8'd245`。即 bdma 引擎占用 axid 0、背压 FIFO 深 245。

[NV_NVDLA_MCIF_READ_ig.v:318-343](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_ig.v#L318-L343) —— 例化 `u_bpt8`（`cdma_dat`，`axid=8`，深度 0）与 `u_bpt9`（`cdma_wt`，`axid=9`，深度 0）。注意 cdma 的延迟 FIFO 深度为 0，说明卷积取数这条路不使用这一级背压（它在上游 CDMA/shared_buffer 已有自己的流控）。

[NV_NVDLA_MCIF_READ_ig.v:345-391](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_ig.v#L345-L391) —— 例化 `u_arb`（`NV_NVDLA_MCIF_READ_IG_arb`），接收 10 路 `bpt2arb_req*`，输出单一 `arb2spt_req*`，并接入全部 10 个 `reg2dp_rd_weight_*` 权重。

[NV_NVDLA_MCIF_READ_ig.v:392-401](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_ig.v#L392-L401) 与 [NV_NVDLA_MCIF_READ_ig.v:402-419](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_MCIF_READ_ig.v#L402-L419) —— 例化 `u_spt` 与 `u_cvt`。cvt 的输出既有 AXI AR 通道，也有 `cq_wr_pvld/cq_wr_thread_id/cq_wr_pd`（向命令队列登记），并接收 `eg2ig_axi_vld` 与 `reg2dp_rd_os_cnt` 做流控。

[NV_NVDLA_XXIF_libs.v:109-120](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_XXIF_libs.v#L109-L120) —— 加权轮询仲裁器 `read_ig_arb` 的请求合成逻辑：`req[i] = req_i & (|wt_i)`，即「有请求且权重非零」才参与仲裁。该文件头部还标注了生成方式 `arbgen2 ... -t wrr -wt_width 8`（见 [NV_NVDLA_XXIF_libs.v:2071-2074](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_XXIF_libs.v#L2071-L2074)），表明这是一个 10 客户端、8 位权重、轮询类型的自动生成仲裁器，**MCIF 与 CVIF 共用**。

#### 4.4.4 代码实践

1. **实践目标**：用一个真实引擎的读请求，走完 IG 四级，定位它在每一级的信号名与 axid。
2. **操作步骤**：
   - 选定引擎 `pdp`（其 axid 应为 2）。
   - 在 `NV_NVDLA_MCIF_READ_ig.v` 中找到连 `pdp2mcif_rd_req_*` 的那个 `u_bptN`，记录它的 `tieoff_axid` 与 `tieoff_lat_fifo_depth`。
   - 跟随 `bpt2arb_req2_*` → `arb` → `arb2spt_*` → `spt` → `spt2cvt_*` → `cvt` → `mcif2noc_axi_ar_*` 与 `cq_wr_thread_id`。
3. **需要观察的现象**：`arid` 低位应等于该 bpt 的 `tieoff_axid`；`cq_wr_thread_id` 同样携带这个编号，供 eg 路由。
4. **预期结果**：你能写出 pdp 读请求的四级信号链：`pdp2mcif_rd_req_pd` → `bpt2arb_req2_pd` → `arb2spt_req_pd` → `spt2cvt_req_pd` → `mcif2noc_axi_ar_araddr/arid`。
5. 源码阅读型实践；**待本地验证** arid 低位与 axid 的等价关系（需结合 cvt 内部对 `arid` 的拼装逻辑确认）。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 cdma_dat/cdma_wt 的 `tieoff_lat_fifo_depth` 是 0，而 bdma 是 245？
  - **答案**：延迟/信用 FIFO 用来吸收「请求已发但响应未到」的在途波动。bdma 作为独立搬运引擎，需要较深的 FIFO 维持高吞吐；而 cdma 的取数路径在上游（CDMA/shared_buffer/CBUF）已有完善的信用反压（见 u3-l2、u3-l3 的 credit 机制），这里再设深度意义不大，故置 0。
- **练习 2**：如果把某引擎的 `reg2dp_rd_weight_*` 配成 0，会发生什么？
  - **答案**：由 `req[i] = req_i & (|wt_i)` 可知，权重为 0 时该客户端的请求被仲裁器视为不存在，永远拿不到授权（gnt）。这是一种「软关闭某引擎读带宽」的配置手段；要恢复只需把权重写回非零值。

---

### 4.5 XXIF_libs：MCIF 与 CVIF 的共享原语

#### 4.5.1 概念说明

`NV_NVDLA_XXIF_libs.v` 里的 `XXIF` 是「任意接口」的占位符——它存放 MCIF 和 CVIF **共同使用**的可复用 RTL 原语，最核心的就是仲裁器：

- `read_ig_arb`：ingress 的 10 客户端加权轮询仲裁器（用于读）。
- `read_eg_arb`：egress 侧的 10 客户端加权轮询仲裁器（用于把返回数据路由出 eg）。
- 同文件还提供 write 侧对应的 `write_ig_arb` 等（命名对称）。

这些仲裁器都由工具 `arbgen2` 按 `-n 10 -t wrr -wt_width 8`（10 客户端、加权轮询、8 位权重）统一生成。正因为结构完全一致，MCIF 与 CVIF 才能复用同一份 `XXIF_libs`，只在各自 wrapper 里换前缀、换连接。

#### 4.5.2 核心流程

仲裁器内部用一个大的 `case (wrr_gnt)` 状态机实现「记住上一次授权谁、从下一个有请求的客户端继续轮转」的轮询逻辑，并用 `wt_left` 记录当前授权者的剩余权重。它带三条断言保护（在 `ASSERT_ON` 下编译）：

- 授权是 **zero-one-hot**（同一周期至多授权一个客户端）；
- 不会授权给**没有请求**的客户端；
- 有请求时**不会一个都不授权**（除非 `gnt_busy`）。

这些断言确保多客户端公平性，不会出现「饿死」或「撞车」。

#### 4.5.3 源码精读

[NV_NVDLA_XXIF_libs.v:2071-2074](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_XXIF_libs.v#L2071-L2074) —— 注释 `arbgen2 -m read_eg_arb -n 10 -stdout -t wrr -wt_width 8`，说明 `read_eg_arb` 是自动生成的 10 客户端加权轮询仲裁器，与前面的 `read_ig_arb`（[NV_NVDLA_XXIF_libs.v:12](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_XXIF_libs.v#L12) 起）参数一致，只是输入端 `gnt_busy` 的处理略有差别。

[NV_NVDLA_XXIF_libs.v:704-705](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_XXIF_libs.v#L704-L705) —— 断言 `nv_assert_zero_one_hot ... "gnt not zero one hot"`，保证授权位独热。

[NV_NVDLA_XXIF_libs.v:797](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/nocif/NV_NVDLA_XXIF_libs.v#L797) —— 断言 `nv_assert_never ... "no gnt even if at least 1 client requesting"`，保证只要有请求就不会全员落空。

> 因为这套库原语对 MCIF/CVIF 完全通用，后续 u4-l2（MCIF 细节）、u4-l3（CVIF 细节）都会反复引用它。当你看到任意 `_IG_arb`/`_eg_arb` 模块，就知道它来自 `XXIF_libs`。

#### 4.5.4 代码实践

1. **实践目标**：确认 MCIF 与 CVIF 真的共用同一份仲裁原语。
2. **操作步骤**：
   - 在 `vmod/nvdla/nocif/` 下分别打开 `NV_NVDLA_MCIF_READ_IG_arb.v` 与 `NV_NVDLA_CVIF_READ_IG_arb.v`，比较它们的模块体。
   - 再打开 `NV_NVDLA_XXIF_libs.v`，确认 `read_ig_arb` 与上述两个文件的结构一致（仅端口/命名差异）。
3. **需要观察的现象**：三者的核心 `case (wrr_gnt)` 仲裁逻辑完全相同，差别只在被哪个 wrapper 例化、连到哪组权重寄存器。
4. **预期结果**：你会得出结论——MCIF/CVIF 是「同构异名」的两套接口，维护者只需维护一份仲裁原语即可，改一处两套接口都受益。
5. 源码阅读型实践，**无需运行**即可完成对比。

#### 4.5.5 小练习与答案

- **练习 1**：为什么 NVDLA 要把仲裁器抽到 `XXIF_libs` 共享，而不是在 MCIF、CVIF 里各写一份？
  - **答案**：两套接口结构完全同构，共享原语能保证仲裁行为一致、减少重复代码、便于综合与时序收敛（同一个已验证单元到处复用），也方便后续用 `arbgen2` 重新生成参数化变体。
- **练习 2**：`read_ig_arb` 与 `read_eg_arb` 名字相近，它们分别用在通路的哪一级？
  - **答案**：`read_ig_arb` 用在 ingress，把多引擎读请求仲裁成单一 AR 流；`read_eg_arb` 用在 egress，把返回的 R 通道数据按权重/路由分发回多引擎。二者都是 10 客户端 wrr，但数据流向相反（多→一 vs 一→多）。

---

## 5. 综合实践

把本讲四个模块串起来，完成一张「NVDLA 存储接口全景图」：

1. 在一张大图上画出顶层 `NV_nvdla` 的 `core2dbb`（→DBB）和 `core2cvsram`（→CVSRAM）两组 AXI 端口，并标注它们各自经 `partition_o` 连到 `u_NV_NVDLA_mcif` / `u_NV_NVDLA_cvif`。
2. 在 MCIF 内部画出 read（→AR/R）与 write（→AW/W/B）两条通路，标出 AXI 五通道归属与数据宽度（512/514/515 位）。
3. 在 read 通路内部画出 IG（bpt×10→arb→spt→cvt）→ cq（10 读线程）→ eg 的三级流水。
4. 用不同颜色标注 10 个引擎（bdma/sdp/pdp/cdp/rbk/sdp_b/sdp_n/sdp_e/cdma_dat/cdma_wt）在 bpt 的 axid（0..9）与各自延迟 FIFO 深度（如 bdma=245、pdp/cdp=61、cdma=0）。
5. 在图上**显式标注纠错信息**：`partition_m(ma/mb)` 是 CMAC 乘加阵列两半，**不是**存储接口；存储接口在 `partition_o`。

完成后，你应当能用这张图向别人解释：一次 SDP 读请求如何从 `sdp2mcif_rd_req_*` 进入 bpt（axid=1）→ 仲裁 → cvt 发 AR → 外部主存返回 R → eg 按 rid=1 经 cq 路由回 `mcif2sdp_rd_rsp_*`。

---

## 6. 本讲小结

- NVDLA 有**两套结构同构**的存储接口：MCIF（primary，→DBB 主存）与 CVIF（secondary，→片上 CVSRAM），二选一由各引擎 DMA 配置决定。
- 每套接口内部都拆成**独立的 READ 与 WRITE 两条通路**，分别对应 AXI 的 AR/R 与 AW/W/B 通道；读数据 512 位、回引擎 514 位、写请求 515 位。
- 每条通路都是 **IG → cq → eg** 三级流水：IG「多合一」并发请求并翻译 AXI 命令，cq 记录每个在途事务的来源（10 个线程槽），eg 用 AXI `id` 把返回数据「一分发多」回各引擎。
- IG 内部又是 **bpt（背压/切事务/打 axid）→ arb（加权轮询）→ spt（单口稳住）→ cvt（转 AXI + 写 cq）** 四级。
- 仲裁器抽到 `XXIF_libs` 共享，由 `arbgen2` 生成 10 客户端、8 位权重的 wrr 单元，MCIF 与 CVIF 复用同一份；权重为 0 可软禁用某客户端。
- **重要纠错**：`partition_m` 是卷积乘加阵列（CMAC），其 `ma`/`mb` 实例是 MAC 两半；MCIF 与 CVIF 都位于中央枢纽 `partition_o`。

---

## 7. 下一步学习建议

- **u4-l2 MCIF 主存接口细节**：深入 MCIF 的 read/write 通路，重点看 `bpt` 如何切分事务、`cq` 的线程槽实现、`eg` 如何按 `rid` 路由。
- **u4-l3 CVIF 卷积 SRAM 接口**：对照 MCIF，确认 CVIF 的同构性，并讨论何时应把数据放到 CVSRAM。
- **u4-l4 BDMA 桥 DMA**：理解 BDMA 如何在 DBB 与 CVSRAM 之间搬运数据，把本讲的「两套接口」与实际的数据预取用法连起来。
- 横向可回顾 **u1-l5（顶层分区）** 巩固「partition_o 是中央枢纽」的认知，并预习 **u6-l1（时钟复位/slcg）** 了解 `pwrbus_ram_pd` 等电源信号如何作用到这些接口的 RAM。
