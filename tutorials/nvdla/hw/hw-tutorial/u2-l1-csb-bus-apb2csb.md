# CSB 总线协议与 apb2csb 桥

## 1. 本讲目标

NVDLA 是一个可编程的硬件加速器：它自己不会"想"该算什么，而是由 CPU 把要做的事情写到它内部的一堆**寄存器**里，它再照着执行。那么 CPU 用什么"笔"去写这些寄存器？答案就是本讲的主角——**CSB（Configuration Space Bus，配置空间总线）**。

学完本讲，你应该能够：

- 说清 CSB 总线的请求/响应信号组成，以及 valid/ready 握手是怎么工作的。
- 看懂 `NV_NVDLA_apb2csb.v` 是如何把标准的 APB 总线翻译成 NVDLA 内部的 CSB 总线的。
- 在顶层 `NV_nvdla.v` 上找到 `csb2nvdla_*` 端口，并追踪它连到了哪个分区、哪个模块。
- 理解 CSB 是 CPU 编程 NVDLA 各引擎（卷积、池化、DMA 等）的**唯一入口**，后续所有寄存器操作都建立在这条通路之上。

本讲承接 u1-l5（顶层 RTL 与分区结构）。在 u1-l5 中我们已经知道：顶层的 CSB 配置口、两组 AXI 存储接口与中断线都连到了 `partition_o`。本讲就深入这条 CSB 通路的"入口段"。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 什么是"配置寄存器"

加速器内部有成百上千个 32 位寄存器，每个寄存器控制一种行为：比如"输入特征图的地址""卷积核大小""是否启动本次运算"。CPU 想让加速器干活，本质就是**按地址写这些寄存器**，再读某些状态寄存器确认结果。这套"按地址读/写寄存器"的机制，就叫做**配置空间（Configuration Space）**。

### 2.2 什么是总线（bus）与握手（handshake）

总线就是一组按规则传递信号的"公路"。一条最简单的握手由两个信号控制：

- `valid`（有效）：发送方举起，表示"我这边数据准备好了，请你收"。
- `ready`（就绪）：接收方举起，表示"我这边能收"。

只有当 `valid` 和 `ready` **同一个时钟沿同时为高**，这一次传输才算成功（称为一次 "handshake" 或 "beat"）。这就像两人交接物品：给的人喊"给你了"（valid），接的人喊"我接着了"（ready），两人都确认的那一刻物品才真正过手。NVDLA 内部几乎所有数据通路都基于这种 valid/ready 握手，CSB 也不例外。

### 2.3 APB 是什么

APB（Advanced Peripheral Bus）是 ARM 定义的一种低速外设总线，广泛用于 SoC 里连接配置类外设。它的特点是**简单、低功耗、非流水线**：一次访问分两个阶段——

1. **Setup（建立）阶段**：`psel=1, penable=0`，给出地址、控制信号。
2. **Access（访问）阶段**：`penable=1`，真正完成一次读或写，并在 `pready=1` 时结束。

很多 SoC 的 CPU 侧只提供 APB 接口。NVDLA 为了既能在内部用自己更紧凑的 CSB，又能方便地接进 APB 系统，就提供了一个桥：`apb2csb`。

> 一个关键事实（务必记住）：NVDLA **顶层对外暴露的是原生 CSB 接口**（`csb2nvdla_*`），并不是 APB。`apb2csb` 是一个**可选的桥**，留给那些 SoC 集成者——如果你的 CPU 是 APB 口，就把 `apb2csb` 接在 NVDLA 前面；如果你的 CPU 能直接发 CSB，就根本不需要它。在本仓库的 RTL 里，`apb2csb` 并没有被例化进 `NV_nvdla` 主设计，它是一块"待集成"的独立 IP。我们稍后会用源码验证这一点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [vmod/nvdla/apb2csb/NV_NVDLA_apb2csb.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/apb2csb/NV_NVDLA_apb2csb.v) | **APB→CSB 桥**。把外部 APB 事务翻译成内部 CSB 请求/响应。本讲的核心模块之一。 |
| [vmod/nvdla/csb_master/NV_NVDLA_csb_master.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v) | **CSB 中央路由器**。CSB 进入芯片后的第一站，按地址把请求分发到各引擎，再把各引擎的响应汇拢回 CSB。本讲用它来精确定义 CSB 协议的包格式与握手。 |
| [vmod/nvdla/top/NV_nvdla.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v) | **顶层**。定义 `csb2nvdla_*` / `nvdla2csb_*` 端口，并把它们连到 `partition_o`。 |
| [vmod/nvdla/top/NV_NVDLA_partition_o.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v) | **枢纽分区**。`csb_master` 实际例化在这里。 |
| [vmod/nvdla/top/NV_NVDLA_partition_a.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_a.v) | **CACC（累加器）分区**。本讲用它当一个具体例子：展示 CSB 请求被路由后，是如何到达某一个引擎（CACC）的寄存器接口的。 |

> 说明：本仓库里 `partition_a` 装的是 **CACC（卷积累加器）**，并不是"配置分区"——这是 u1-l5 已经澄清过的、容易和旧资料混淆的地方。CSB 通路真正集中的地方是 `partition_o`。

---

## 4. 核心概念与源码讲解

### 4.1 csb2nvdla 端口：CSB 通路在顶层的入口

#### 4.1.1 概念说明

CSB 是一条**点对点**的配置总线：一个 master（CPU 侧）连一个 slave（NVDLA）。在 NVDLA 顶层，这条总线被明确地拆成两组端口：

- **请求方向（CPU → NVDLA）**：`csb2nvdla_*`，携带地址、数据、读/写控制。
- **响应方向（NVDLA → CPU）**：`nvdla2csb_*`，把读数据或"写完成"信号送回去。

理解这组端口，就理解了 CSB 在芯片边界的"合同"。

#### 4.1.2 核心流程

一次 CSB 访问在顶层边界的流程：

1. CPU（或桥）在 `dla_csb_clk` 时钟域里驱动 `csb2nvdla_valid` 拉高，同时给出 `csb2nvdla_addr`（地址）、`csb2nvdla_wdat`（写数据）、`csb2nvdla_write`（1=写, 0=读）、`csb2nvdla_nposted`（是否需要写完成回执）。
2. NVDLA 准备好接收时拉高 `csb2nvdla_ready`，握手完成，请求被吞入。
3. 若是**读**：NVDLA 稍后拉高 `nvdla2csb_valid` 并在 `nvdla2csb_data` 上给出 32 位读数据。
4. 若是**非投递写（nposted=1）**：NVDLA 稍后拉高 `nvdla2csb_wr_complete` 表示"这次写我已经落实了"。

#### 4.1.3 源码精读

顶层端口定义在 [NV_nvdla.v:102-112](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L102-L112)：

```verilog
input         csb2nvdla_valid;    /* data valid */
output        csb2nvdla_ready;    /* data return handshake */
input  [15:0] csb2nvdla_addr;     // 16 位「字地址」
input  [31:0] csb2nvdla_wdat;
input         csb2nvdla_write;
input         csb2nvdla_nposted;
output        nvdla2csb_valid;    // 读响应有效
output [31:0] nvdla2csb_data;     // 读数据
output  nvdla2csb_wr_complete;    // 写完成回执
```

注意 `csb2nvdla_addr` 只有 **16 位**——它不是字节地址，而是**字地址**（每个字 4 字节，稍后在 apb2csb 里会看到 `paddr[17:2]` 的截取）。16 位字地址覆盖 64K 字 = 256KB 的配置空间，对 NVDLA 的寄存器集合已经绰绰有余。

这组端口连接到的时钟是 `dla_csb_clk`，注释明确标注在 [NV_nvdla.v:91-92](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L91-L92)：

```verilog
input  dla_core_clk;  /* nvdla_core2dbb_..., nvdla_core2cvsram_... */
input  dla_csb_clk;   /* csb2nvdla, nvdla2csb, nvdla2csb_wr */
```

即：CSB 接口运行在 `dla_csb_clk`（配置时钟）域，而 AXI 存储接口运行在 `dla_core_clk`（核心时钟）域。这两个时钟不同步，是后面跨时钟域 FIFO 存在的根本原因。

顶层把 `csb2nvdla_*` 一股脑连给 `u_partition_o`，见 [NV_nvdla.v:1208-1213](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L1208-L1213)：

```verilog
,.csb2nvdla_valid  (csb2nvdla_valid)   // 透传进 partition_o
,.csb2nvdla_ready  (csb2cvsram_ready)  // （此处省略，partition_o 内部继续往下连）
...
```

而在 `partition_o` 内部，`csb2nvdla_*` 直接接到了 `csb_master` 实例上，见 [NV_NVDLA_partition_o.v:1771](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v#L1771) 起的例化。也就是说，**CSB 进入 NVDLA 后的第一站就是 `csb_master`**。

#### 4.1.4 代码实践

**实践目标**：亲手确认"CSB 端口连到了 partition_o，且 partition_o 把它交给了 csb_master"。

**操作步骤**：

1. 打开 `vmod/nvdla/top/NV_nvdla.v`，跳到第 102 行，确认 `csb2nvdla_*` 是 `input`、`nvdla2csb_*` 是 `output`。
2. 搜索 `u_partition_o`（约 1165 行），在其端口列表里找到 `.csb2nvdla_valid (csb2nvdla_valid)`（约 1208 行），确认 CSB 信号被原样透传进 partition_o。
3. 打开 `vmod/nvdla/top/NV_NVDLA_partition_o.v`，搜索 `csb_master`，确认约 1771 行处 `NV_NVDLA_csb_master u_NV_NVDLA_csb_master (` 例化，且 `.csb2nvdla_valid` 接到模块的 `csb2nvdla_valid` 端口。

**需要观察的现象**：从顶层端口 → partition_o → csb_master，`csb2nvdla_valid/addr/wdat/write/nposted` 这 5 根请求线是**一路直通**的，中间没有任何译码（译码发生在 csb_master 内部）。

**预期结果**：你能画出一条无分叉的直线 `顶层端口 → partition_o → csb_master`。

#### 4.1.5 小练习与答案

**练习 1**：`csb2nvdla_addr` 是 16 位，NVDLA 的配置空间最大能寻址多少字节？

**参考答案**：16 位是**字地址**，每字 4 字节，故可寻址 \(2^{16} \times 4 = 262144\) 字节 = 256 KB。

**练习 2**：为什么 `nvdla2csb_wr_complete` 是单独一根信号，而不是复用 `nvdla2csb_valid/data`？

**参考答案**：因为读响应要带 32 位数据（`nvdla2csb_data`），而写完成只需要一个"写好了"的脉冲、无需数据。把它们分开，可以让"读通路"和"写完成通路"互不阻塞——尤其配合 `nposted`（非投递写）机制时，CPU 能精确知道某次写何时真正落地。

---

### 4.2 CSB 请求/响应握手协议

#### 4.2.1 概念说明

CSB 是 NVDLA 自己定义的轻量协议，比 AXI 简单得多：**一条请求通路 + 一条响应通路**，都是 valid/ready（或 valid-only）握手。理解它的最佳位置是 `csb_master.v`——因为 csb_master 既要"收"CSB 请求，又要按 CSB 格式"发"响应，它完整体现了协议契约。

CSB 协议里有两种"包"：

- **请求包（request packet）**：CPU 给 NVDLA 的，包含 `{nposted, write, wdat[31:0], addr[15:0]}`。
- **响应包（response packet）**：NVDLA 回给 CPU 的，包含 `{type, error, data[31:0]}`，其中 `type` 区分"这是读响应还是写完成响应"。

#### 4.2.2 核心流程

CSB 请求在 csb_master 内部的旅程：

1. **打包**：csb_master 把 5 根 `csb2nvdla_*` 请求线打包成一个 50 位的内部请求 `csb2nvdla_pd`。
2. **跨时钟域**：因为 CSB 在 `dla_csb_clk`（falcon 域）、而各引擎在 `dla_core_clk`（core 域），请求先过一个异步 FIFO（`falcon2csb_fifo`）进入 core 域。
3. **地址译码 + 分发**：csb_master 用请求地址的高位判断"这次访问属于哪个引擎"，然后只把请求发给那一个引擎（`csb2<engine>_req_pvld/prdy/pd`）。
4. **响应汇拢**：每个引擎各自回一个响应（`<engine>2csb_resp_valid/pd`），csb_master 把所有引擎的响应"或"起来，选出当前有效的那个，组成统一的响应包。
5. **跨时钟域回程**：响应再过一个异步 FIFO（`csb2falcon_fifo`）回到 falcon 域，最终变成 `nvdla2csb_valid/data` 或 `nvdla2csb_wr_complete`。

请求与响应的时序关系（读事务）：

\[
T_{\text{read}} = T_{\text{req\_submit}} + T_{\text{CDC}} + T_{\text{engine}} + T_{\text{CDC}}
\]

即一次读的往返延迟 = 请求提交 + 两次跨时钟域穿越 + 引擎内部寄存器读延迟。这也是为什么 apb2csb 桥在读事务时会"拉长"APB 的 access 阶段——它必须等这个往返结束。

#### 4.2.3 源码精读

**(a) 请求包打包**：[NV_NVDLA_csb_master.v:452](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L452) 把 5 根请求线拼成 50 位：

```verilog
assign  csb2nvdla_pd[49:0] = {csb2nvdla_nposted, csb2nvdla_write,
                             csb2nvdla_wdat, csb2nvdla_addr};
// 位域: [49]=nposted, [48]=write, [47:16]=wdat, [15:0]=addr
```

这就是 CSB 请求包的标准格式：`{nposted, write, wdat[31:0], addr[15:0]}`。

**(b) 跨时钟域 FIFO（请求方向）**：[NV_NVDLA_csb_master.v:454-466](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L454-L466) 例化了 `falcon2csb_fifo`：

```verilog
NV_NVDLA_CSB_MASTER_falcon2csb_fifo u_fifo_csb2nvdla (
   .wr_clk   (nvdla_falcon_clk)    // 写侧 = falcon(csb) 域
  ,.rd_clk   (nvdla_core_clk)      // 读侧 = core 域
  ,.wr_req   (csb2nvdla_valid)     // 接收外部 CSB 请求
  ,.wr_ready (csb2nvdla_ready)     // 向外部回 ready
  ,.rd_req   (core_req_pvld)       // core 域侧输出请求
  ...
);
```

注意 `nvdla_falcon_clk` 这个名字——它在顶层 `NV_nvdla.v:1884` 处被连到 `dla_csb_clk`。所以"falcon 域"在本项目里其实就是"csb 配置时钟域"。FIFO 的存在告诉我们：CSB 请求天然是异步进来的，csb_master 第一件事就是把它同步到 core 时钟域。

**(c) 地址译码**：csb_master 把地址空间按 4KB 块切给各引擎。译码用 `addr_mask` 提取字节地址的高位，见 [NV_NVDLA_csb_master.v:621](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L621) 与每个引擎的比较，例如 CACC 在 [NV_NVDLA_csb_master.v:967](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L967)：

```verilog
assign addr_mask = {{16-10{1'b1}},{12{1'b0}}};  // 比较 core_byte_addr[17:12]
...
select_cacc = ((core_byte_addr & addr_mask) == 32'h00009000); // CACC 基址 0x9000
```

各引擎的基址（4KB 对齐）整理如下，这是后续讲义会反复用到的"CSB 地址地图"：

| 引擎 | 基址 | 行号 |
| --- | --- | --- |
| glb（全局/中断） | 0x0000 | L1032 |
| gec | 0x1000 | L837 |
| mcif（主存接口） | 0x2000 | L1552 |
| cvif（CVSRAM 接口） | 0x3000 | L1097 |
| bdma（桥 DMA） | 0x4000 | L1422 |
| cdma（卷积 DMA） | 0x5000 | L1292 |
| csc（卷积调度） | 0x6000 | L772 |
| cmac_a（乘加 A 半） | 0x7000 | L642 |
| cmac_b（乘加 B 半） | 0x8000 | L1162 |
| cacc（累加器） | 0x9000 | L967 |
| sdp_rdma | 0xa000 | L707 |
| sdp（单点处理） | 0xb000 | L1357 |
| pdp_rdma | 0xc000 | L1487 |
| pdp（池化） | 0xd000 | L1227 |
| cdp_rdma | 0xe000 | L1617 |
| cdp（LRN） | 0xf000 | L902 |
| rbk（Rubik 重排） | 0x10000 | L1682 |

> 这张表是 u2-l2（csb_master 路由）的预告，本讲你只需记住"CACC 的配置寄存器在 0x9000 这一段"。

**(d) 响应包格式与汇拢**：响应是 34 位包，位 [33] 区分读/写类型，位 [32] 是错误标志，[31:0] 是数据。判别逻辑见 [NV_NVDLA_csb_master.v:486-488](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L486-L488)：

```verilog
assign nvdla2csb_rresp_is_valid = (resp_pvld && (resp_pd[33:33]==1'd0)); // 读响应
assign nvdla2csb_wresp_is_valid = (resp_pvld && (resp_pd[33:33]==1'd1)); // 写完成
```

19 路引擎响应最终被"或"成一个 `core_resp_pvld`，并用 MUX 选出有效的那一路数据，见 [NV_NVDLA_csb_master.v:2234-2272](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L2234-L2272)。代码里还配了一条断言 `nv_assert_zero_one_hot`（[L2303](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L2303)）保证同一时刻**最多只有一路引擎在回响应**——因为一次 CSB 请求只发给一个引擎，响应自然也只会从那一个回来。

**(e) 响应回程寄存器**：读响应被寄存成 `nvdla2csb_valid/data`（[L538-555](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L538-L555)），写完成被寄存成 `nvdla2csb_wr_complete`（[L565-571](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L565-L571)），两者都在 falcon 时钟沿更新，最终送回 CPU 侧。

#### 4.2.4 代码实践

**实践目标**：用 csb_master 的源码，把 CSB 协议的"请求包/响应包"格式抄成一张速查表。

**操作步骤**：

1. 打开 `vmod/nvdla/csb_master/NV_NVDLA_csb_master.v`。
2. 在第 452 行附近，根据 `csb2nvdla_pd` 的拼接顺序，画出**请求包 50 位**的位域图（哪几位是 nposted、write、wdat、addr）。
3. 在第 486–488 行附近，根据 `resp_pd[33]` 的判别，画出**响应包 34 位**的位域图（type / error / data）。

**需要观察的现象**：请求包里地址占最低 16 位、写数据占中间 32 位；响应包里最高位 [33] 是"读/写"类型标签。

**预期结果**（你可据此自查）：

- 请求包：`[49] nposted | [48] write | [47:16] wdat | [15:0] addr`
- 响应包：`[33] type(0=读,1=写完成) | [32] error | [31:0] data`

#### 4.2.5 小练习与答案

**练习 1**：csb_master 里 `core_req_prdy = 1'b1`（[L468](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L468)），意味着 core 域对 CSB 请求"永远就绪"。那请求的背压（backpressure）发生在哪里？

**参考答案**：发生在**每个引擎各自的 `csb2<engine>_req_prdy`** 上。csb_master 把请求无背压地接进 FIFO，但向具体引擎转发时，要看那个引擎的 `req_prdy`。这样 CSB 入口的吞吐不受某个慢引擎拖累，慢引擎的背压被局限在它自己的通路上。

**练习 2**：为什么 csb_master 需要"零一热（zero-one-hot）"断言来约束 19 路响应？

**参考答案**：因为每次 CSB 请求只发给一个引擎，所以同一时刻最多只有一路响应有效。如果出现两路同时有效，就说明地址译码或引擎逻辑出错了，MUX 会把两路数据"或"出脏数据。断言在仿真时一旦命中即报错，是协议正确性的安全网。

---

### 4.3 apb2csb：把外部 APB 桥接成 CSB

#### 4.3.1 概念说明

`NV_NVDLA_apb2csb` 是一个**纯组合 + 少量状态**的薄桥，它的全部职责是：把 APB 的两阶段访问，翻译成 CSB 的请求/响应握手，并处理二者时序上的差异。它运行在自己的 `pclk`（APB 时钟）域，假设 `pclk` 与 CSB 的 `dla_csb_clk` 同频或同源（桥内部不做跨时钟域，跨域由下游 csb_master 的 FIFO 负责）。

桥要解决三个核心问题：

1. **信号映射**：APB 的 `paddr/pwdata/pwrite` → CSB 的 `addr/wdat/write`。
2. **握手对齐**：APB 的 access 阶段要在 CSB 完成后才能结束（控制 `pready`）。
3. **读去重**：APB 读期间 `psel&penable` 会持续多个周期，但 CSB 请求只能发一次，需要一个状态位防止重复发射。

#### 4.3.2 核心流程

**写事务**（CPU 经 APB 写一个 NVDLA 寄存器）：

1. APB setup：`psel=1, penable=0, pwrite=1, paddr=A, pwdata=D`。
2. APB access：`penable=1`，桥检测到 `wr_trans_vld = psel & penable & pwrite` 为真，立即拉高 `csb2nvdla_valid` 并输出 `addr/wdat/write`。
3. 由于 `csb2nvdla_nposted` 恒为 0（投递写，不要回执），写请求被 csb 接收（`csb2nvdla_ready=1`）即完成；`pready` 随之拉高，APB access 阶段结束。

**读事务**（CPU 经 APB 读一个 NVDLA 寄存器）：

1. APB setup：`psel=1, penable=0, pwrite=0, paddr=A`。
2. APB access：`penable=1`，桥检测到 `rd_trans_vld`，在还没发过请求时（`~rd_trans_low`）拉高一次 `csb2nvdla_valid` 发出读请求，并置位 `rd_trans_low` 防止重发。
3. 桥**保持 `pready=0`**，直到 CSB 读响应回来（`nvdla2csb_valid=1`）。
4. 响应到达：`prdata = nvdla2csb_data`，`pready=1`，同时 `rd_trans_low` 被清零，APB access 结束。

读事务的 access 阶段长度可表示为：

\[
N_{\text{access}} = 1 + \lceil T_{\text{csb\_round\_trip}} / T_{\text{pclk}} \rceil
\]

即 APB 会被"拉长"若干个 pclk 周期，直到 CSB 那一趟往返结束。这是 APB（非流水线）接 CSB（带往返延迟）的必然代价。

#### 4.3.3 源码精读

模块端口分两组：左侧 APB、右侧 CSB，见 [NV_NVDLA_apb2csb.v:35-53](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/apb2csb/NV_NVDLA_apb2csb.v#L35-L53)：

```verilog
// apb interface
input         psel, penable, pwrite;
input  [31:0] paddr, pwdata;
output [31:0] prdata;
output        pready;
// csb interface
output        csb2nvdla_valid;
input         csb2nvdla_ready;
output [15:0] csb2nvdla_addr;
output [31:0] csb2nvdla_wdat;
output        csb2nvdla_write;
output        csb2nvdla_nposted;
input         nvdla2csb_valid;
input  [31:0] nvdla2csb_data;
```

**(a) 识别 APB access 阶段**：[NV_NVDLA_apb2csb.v:78-79](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/apb2csb/NV_NVDLA_apb2csb.v#L78-L79) 把 `psel & penable` 定义为"access 命中"，再按 `pwrite` 区分读写：

```verilog
assign wr_trans_vld = psel & penable & pwrite;   // 写事务命中
assign rd_trans_vld = psel & penable & ~pwrite;  // 读事务命中
```

**(b) 读去重状态位**：[NV_NVDLA_apb2csb.v:81-90](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/apb2csb/NV_NVDLA_apb2csb.v#L81-L90) 用一个寄存器 `rd_trans_low` 保证一个 APB 读只发一次 CSB 请求：

```verilog
always @(posedge pclk or negedge prstn) begin
  if (!prstn)                rd_trans_low <= 1'b0;
  else if (nvdla2csb_valid & rd_trans_low) rd_trans_low <= 1'b0; // 响应来了，清
  else if (csb2nvdla_ready & rd_trans_vld) rd_trans_low <= 1'b1; // 已发出，置
end
```

**(c) 关键映射赋值**：[NV_NVDLA_apb2csb.v:92-100](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/apb2csb/NV_NVDLA_apb2csb.v#L92-L100) 是整座桥的"翻译表"：

```verilog
assign csb2nvdla_valid   = wr_trans_vld | (rd_trans_vld & ~rd_trans_low);
assign csb2nvdla_addr    = paddr[17:2];   // 字节地址→字地址：截掉低 2 位
assign csb2nvdla_wdat    = pwdata[31:0];
assign csb2nvdla_write   = pwrite;
assign csb2nvdla_nposted = 1'b0;          // 桥固定用「投递写」，不要回执
assign prdata            = nvdla2csb_data[31:0];
assign pready = ~(wr_trans_vld & ~csb2nvdla_ready   // 写：等 CSB 收下
                | rd_trans_vld & ~nvdla2csb_valid); // 读：等响应回来
```

两个细节值得品味：

- `paddr[17:2]`：APB 给的是 32 位**字节地址**，CSB 要 16 位**字地址**。截取 `[17:2]` 既完成了"字节→字"的除以 4（丢掉低位 `[1:0]`），又限定了 NVDLA 256KB 配置空间范围（保留 18 位字节地址）。
- `csb2nvdla_nposted = 1'b0`：桥固定走"投递写"（posted write）。也就是说，APB 写只要被 CSB 接收（`ready=1`）就算完成，不等寄存器真正落地。对配置写来说这通常足够——CPU 写完配置后会再去 kick 引擎并轮询状态，不依赖单笔写的完成回执。

**(d) 桥是"待集成"模块**：如前置知识所述，`apb2csb` 在本仓库 RTL 里并未被例化进 `NV_nvdla`。你可以在整棵 `vmod/` 树里搜索它的例化（形如 `NV_NVDLA_apb2csb u_...`），结果是**没有**——它和 `apb2csb/` 目录一起，作为给 SoC 集成者的可选桥存在。需要它时，集成者在自己顶层里把 APB master 接到 `NV_NVDLA_apb2csb` 的 APB 侧，再把它的 CSB 侧接到 `NV_nvdla` 的 `csb2nvdla_*` 端口即可。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：对照源码，整理出"APB 写→CSB 写"的完整信号映射表，亲手把桥的翻译规则说清楚。

**操作步骤**：

1. 打开 `vmod/nvdla/apb2csb/NV_NVDLA_apb2csb.v`，定位到第 92–100 行的赋值块。
2. 对一次 APB 写事务，逐根信号填写下表左列（APB 侧）→ 右列（CSB 侧）的映射，并注明源码行号。
3. 再对一次 APB 读事务，补充读方向的映射（含响应）。

**需要观察的现象**：地址从 32 位被压缩到 16 位（丢低 2 位）；`nposted` 是写死的常量；`pready` 是由 CSB 侧 `ready`/`valid` 反推出来的。

**预期结果**（你可以此核对答案）：

| APB 信号 / 条件 | CSB 信号 | 映射规则 | 源码行 |
| --- | --- | --- | --- |
| `pwrite` | `csb2nvdla_write` | 直传 | L95 |
| `paddr[17:2]` | `csb2nvdla_addr[15:0]` | 字节地址→字地址 | L93 |
| `pwdata[31:0]` | `csb2nvdla_wdat[31:0]` | 直传 | L94 |
| `psel & penable & pwrite` | `csb2nvdla_valid`（写路径） | access 命中即有效 | L78, L92 |
| 常量 `1'b0` | `csb2nvdla_nposted` | 恒为投递写 | L96 |
| `csb2nvdla_ready`（反相参与） | `pready`（写时） | CSB 收下才 ready | L100 |
| 读：`~pwrite` | `csb2nvdla_write=0` | 读时写使能为 0 | L95 |
| 读：`nvdla2csb_data` | `prdata` | 读数据直传 | L98 |
| 读：`nvdla2csb_valid`（反相参与） | `pready`（读时） | 响应回来才 ready | L100 |

**关于波形验证**：若你想在仿真中观察上述时序（尤其 `pready` 在读事务期间被拉低若干周期的现象），需要搭建一个带 APB master + `apb2csb` + NVDLA 的最小测试平台。本仓库默认的 `verif/sim` 仿真走的是**原生 CSB 激励**（见 u1-l4、u7-l2），并不经过 `apb2csb`，因此默认 sanity 仿真**不会**触发这条桥路径——其波形行为**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果集成者希望 APB 写能够得到"写完成"回执（而非投递写），需要改 `apb2csb` 的哪一处？会带来什么副作用？

**参考答案**：把 [L96](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/apb2csb/NV_NVDLA_apb2csb.v#L96) 的 `csb2nvdla_nposted = 1'b0` 改为 `1'b1`（非投递写），并让 `pready` 额外等待 `nvdla2csb_wr_complete`。副作用是：APB 写的 access 阶段会被拉长，要等 CSB 把写真正落到目标寄存器并回执，写吞吐下降。这也是原版桥默认选投递写的原因——配置写追求的是"尽快提交"，完成性由软件后续轮询保证。

**练习 2**：`paddr[17:2]` 给出的是 16 位字地址。如果某寄存器在 NVDLA 内的**字节地址**是 `0x9004`（CACC 段内），APB 侧 `paddr` 应填多少？CSB 侧 `csb2nvdla_addr` 又是多少？

**参考答案**：APB 侧 `paddr = 32'h0000_9004`（字节地址）。CSB 侧 `csb2nvdla_addr = paddr[17:2] = 0x9004 >> 2 = 16'h2401`（字地址）。记住 CSB 地址总是字节地址除以 4。

---

## 5. 综合实践

**任务**：把本讲三条线索串起来——画出一张"CPU 经 APB 写一个 CACC 寄存器"的端到端信号流图。

请按下列顺序追踪，并标注每一段涉及的模块与时钟域：

1. **APB 侧**：SoC 的 APB master 在 `pclk` 下发起一次写，给出 `paddr/pwdata/pwrite/psel/penable`。
2. **桥**：`NV_NVDLA_apb2csb` 把它翻译成 `csb2nvdla_valid/addr/wdat/write`（`nposted=0`）。说明 `paddr[17:2]` 是怎么变成 `csb2nvdla_addr` 的。
3. **顶层透传**：`csb2nvdla_*` 经 `NV_nvdla.v` 直通进 `partition_o`（[L1208-1213](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L1208-L1213)）。
4. **csb_master**：请求在 `dla_csb_clk`(falcon) 域进 FIFO，跨到 `dla_core_clk`(core) 域，按地址高位译码（CACC 基址 0x9000）选通 `csb2cacc_req_*`（[L967](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L967)）。
5. **到达 CACC**：`csb2cacc_req_*` 经顶层 wire 与一级 retiming 到达 `partition_a` 的 `csb2cacc_req_dst_*`（[partition_a.v:84-86](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_a.v#L84-L86)），最终被 CACC 的寄存器文件接收。

**交付物**：一张标注了"模块名 + 时钟域 + 关键信号名"的方框流程图，并在图旁用一句话回答：这条通路上一共穿过了**几次时钟域**？（答案：2 次——falcon↔core 各一次，由 csb_master 的一对异步 FIFO 完成；apb2csb 假定 pclk 与 falcon 同源，不算独立跨域。）

> 提示：如果你暂时无法跑仿真，可以纯靠源码阅读完成这张图——本实践是"源码阅读型实践"，重点是理清调用链与时钟域边界。

## 6. 本讲小结

- **CSB 是 CPU 编程 NVDLA 的唯一入口**：顶层 `csb2nvdla_*`（请求）+ `nvdla2csb_*`（响应）两组端口，定义在 [NV_nvdla.v:102-112](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_nvdla.v#L102-L112)，运行在 `dla_csb_clk` 域。
- **CSB 请求包 = `{nposted, write, wdat[31:0], addr[15:0]}`**，地址是 16 位**字地址**；响应包 = `{type, error, data[31:0]}`，用 type 区分读响应与写完成。包格式由 [csb_master.v:452](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L452) 与 [:486-488](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csb_master/NV_NVDLA_csb_master.v#L486-L488) 体现。
- **CSB 进入芯片的第一站是 `csb_master`**（例化在 `partition_o:1771`），它用一对异步 FIFO 完成 falcon↔core 跨时钟域，再按地址译码分发到各引擎，响应汇拢后回程。
- **`apb2csb` 是可选的外部桥**，把标准 APB 翻译成 CSB；核心翻译表在 [apb2csb.v:92-100](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/apb2csb/NV_NVDLA_apb2csb.v#L92-L100)：`paddr[17:2]→addr`、`pready` 由 CSB 侧 ready/valid 反推、`nposted` 恒为 0。
- **APB 读会被"拉长"**：桥用 `rd_trans_low` 防止读请求重发，并保持 `pready=0` 直到 CSB 读响应返回；写则因投递写模式而较快结束。
- **本仓库主设计不例化 `apb2csb`**——顶层直接给原生 CSB 口，桥留给走 APB 的 SoC 集成者；默认仿真激励也走原生 CSB。

## 7. 下一步学习建议

本讲只讲了 CSB 的"入口段"——协议、桥、顶层端口。CSB 请求进入 `csb_master` 之后的**地址译码与多路分发**细节（那张引擎地址地图的完整含义、`csb2<engine>_req_*` 的握手、跨域 FIFO 的结构）是下一讲 u2-l2《csb_master：中央配置路由器》的主题，建议紧接着读。

之后可以沿着两条线深入：

- **寄存器侧**：u2-l3《寄存器文件与影偶配置机制》会讲 CSB 请求最终落到的 `_CSB_reg.v` / `_dual_reg.v` 寄存器文件，以及 producer/consumer 影偶切换。
- **中断侧**：u2-l4《GLB 全局配置与中断聚合》会讲 CSB 配的 GLB 寄存器如何聚合各引擎的 done 中断。

如果你更关心验证，可以先跳到 u7-l2《CSB 激励序列与 trace 格式》，看看 `verif/synth_tb` 里的 `csb_master_seq` 是如何直接用原生 CSB 协议（而非 APB）把一串寄存器写灌进 DUT 的——那正好印证了"默认仿真不走 apb2csb"这一结论。
