# CBUF：卷积缓冲与存储分 Bank

> 前置讲义：本讲承接 u3-l2《CDMA：特征图与权重的取数引擎》。你已知道 CDMA 把特征图（dat）与权重（wt）从外部存储搬进卷积核心；但 CDMA 取来的数据并没有直接喂给乘加阵列，而是先落进一个「大水池」——这就是本讲的主角 **CBUF（Convolution Buffer，卷积缓冲）**。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 CBUF 在卷积主流水线（CDMA→CBUF→CSC→CMAC→CACC）中的定位：它是夹在「取数引擎 CDMA」与「分发引擎 CSC」之间的大容量片上 SRAM 工作缓冲。
- 读懂 `NV_NVDLA_cbuf.v` 的端口表：CDMA 怎么写进来、CSC 怎么读出去。
- 用「bank（存储块）+ column（列）」的语言解释 CBUF 的物理组织：16 个 bank、每个 bank 2 个 column、共 32 块 `256×512` 的 RAM，合计 512 KB。
- 解释「bank 0 只放数据、bank 15 只放权重、中间 bank 数据与权重共享」的分区策略，以及为什么这样设计。
- 区分两个容易混淆的缓冲：CBUF（跨模块大缓冲）vs CDMA 内部的 `shared_buffer`（小块暂存 SRAM）。

## 2. 前置知识

### 2.1 为什么卷积核心需要一个中间缓冲？

回忆软件里做卷积的过程：对一个输出像素，要把一个卷积窗口（如 3×3）内的输入特征图与对应权重逐对相乘再累加。硬件要跑这件事，面临两个时间不匹配的问题：

- **取数慢、算得快**：输入特征图和权重都在片外存储（DBB）里，从那里取一个数据要几十上百个时钟周期；而 MAC 阵列一个周期能算几千个乘加。如果让 MAC 阵列直连片外存储，阵列绝大多数时间都在干等数据。
- **同一个输入数据要被反复用**：卷积有权值共享（一个输入像素参与多个输出像素的计算）和权重复用（同一组权重滑过整张特征图）。从片外反复取同一个数据极其浪费。

解决办法是经典的 **“预取 + 缓存”**：CDMA 提前把这一层要用的特征图块和权重块从片外搬到一个片上 SRAM 里，CSC 再从这个 SRAM 按节拍读出来喂给 MAC 阵列。这个片上 SRAM 就是 CBUF。它让“慢吞吞的片外取数”和“飞快的阵列计算”解耦，互相都不必等对方。

### 2.2 关键术语

| 术语 | 含义 |
|------|------|
| **CBUF** | Convolution Buffer，卷积核心内部的 512 KB 片上 SRAM 工作缓冲 |
| **bank** | 存储块。CBUF 把容量切成 16 个 bank，便于并行读写与数据/权重分区 |
| **column（c0/c1）** | 每个 bank 又分 2 个 column，每个 column 是一块 512 位宽的 RAM |
| **dat / wt / wmb** | data=特征图数据；wt=权重；wmb=Weight Mask Bitmap 权重掩码位图（压缩权重解压时用） |
| **ATOM** | NVDLA 的最小搬运/计算粒度，CBUF 按 ATOM 为单位存取（详见 4.2） |
| **hsel** | column 选择信号，决定一次写操作落在本 bank 的 c0、c1 还是两者 |
| **NV_NVDLA_csc 里的 "sc"** | sc = Slot/Strip Controller，即 CSC；因此 `sc2buf_*` 表示「从 CSC 指向 CBUF」的信号 |

> 信号命名规律：`cdma2buf_xxx` = CDMA 写 CBUF；`sc2buf_xxx_rd` = CSC 向 CBUF 发读请求；`sc2buf_xxx_rd_data` = CBUF 把读出的数据返回给 CSC。`2` 就是 “to”。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [vmod/nvdla/cbuf/NV_NVDLA_cbuf.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cbuf/NV_NVDLA_cbuf.v) | **本讲核心**。CBUF 顶层，约 7269 行（大量是逐 bank 展开的生成代码），定义全部端口、bank 译码、读写流水与 32 个 RAM 例化 |
| [vmod/nvdla/top/NV_NVDLA_partition_c.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_c.v) | 卷积前段分区，例化 CDMA、CBUF、CSC 并把它们连线；CBUF 在此被例化为 `u_NV_NVDLA_cbuf` |
| [vmod/nvdla/cdma/NV_NVDLA_CDMA_shared_buffer.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_CDMA_shared_buffer.v) | CDMA **内部**的 shared_buffer（16 块 `16×256` 小 RAM），是 scatter-gather/像素处理的暂存，**不是 CBUF**，本讲用来做对比澄清 |
| [vmod/nvdla/csc/NV_NVDLA_csc.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_csc.v) | CSC，CBUF 的读侧消费者，通过 `sc2buf_*` 端口读 CBUF |

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：

- **4.1 CBUF SRAM 缓冲**：CBUF 是什么、容量多大、用什么 RAM 搭起来。
- **4.2 bank 组织**：地址怎么切成 bank/column/row，数据与权重如何分区共享 16 个 bank。
- **4.3 CDMA 写 / CSC 读**：两个写口、三个读口的握手与位宽，以及一次卷积窗口数据如何流经 CBUF。

---

### 4.1 CBUF SRAM 缓冲

#### 4.1.1 概念说明

CBUF 是卷积核心内部唯一的「大水池」。它的职责很纯粹：

- **只接两个邻居**：上游是 CDMA（往里写），下游是 CSC（从里读）。
- **不参与运算**：它只是存储，不做加法、不做格式转换（转换在 CDMA 的 cvt 阶段就完成了）。
- **容量大、位宽宽**：为了喂饱 MAC 阵列，CBUF 的读写口都是 **512 位起步**，一次能吞吐一个很宽的 ATOM。

之所以要做成“大而宽”，是因为 CMAC 阵列每半边有 1024 个 MAC，每周期要消耗海量数据——窄缓冲根本供不上。

#### 4.1.2 核心流程

CBUF 的内部可以理解为一个「多端口寄存器堆式 SRAM」：

```text
          ┌─────────────────── CBUF（512 KB SRAM 阵列）───────────────────┐
          │  bank0  bank1  bank2  ...  bank13  bank14  bank15            │
          │  ┌──┐   ┌──┐   ┌──┐        ┌──┐    ┌──┐    ┌──┐             │
写口(dat)─┼─>│c0│   │c0│   │c0│  ...   │c0│    │c0│    │  │             │
写口(wt) ─┼─>│c1│   │c1│   │c1│        │c1│    │c1│    │c0│             │
          │  └──┘   └──┘   └──┘        └──┘    └──┘    │c1│             │
          │   纯数据  <────── 数据与权重共享 ──────>   纯权重+wmb        │
读口(dat)<─┼───────────────────────────────────────────  (bank0~14)      │
读口(wt) <─┼───────────────────────────────────────────  (bank1~15)      │
读口(wmb)<─┼───────────────────────────────────────────  (bank15)        │
          └─────────────────────────────────────────────────────────────┘
```

整体只有三类操作：CDMA 写、CSC 读、（复位时）清零。没有任何仲裁或运算逻辑——CBUF 是被动存储，时序由 CDMA/CSC 协调。

#### 4.1.3 源码精读

CBUF 顶层模块的端口表清晰列出了它接的两组邻居：

[NV_NVDLA_cbuf.v:12-36](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cbuf/NV_NVDLA_cbuf.v#L12-L36) —— 模块声明，列出全部端口名（`cdma2buf_*` 写入、`sc2buf_*` 读出、时钟复位、`pwrbus_ram_pd` 电源门控）。

端口位宽声明（这是本讲最该先记住的一张表）：

[NV_NVDLA_cbuf.v:46-72](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cbuf/NV_NVDLA_cbuf.v#L46-L72) —— 写口 dat 为 1024 位、写口 wt 为 512 位；三个读口 dat/wt/wmb 均为 1024 位：

```verilog
input          cdma2buf_dat_wr_en;    // CDMA 写特征图：使能
input   [11:0] cdma2buf_dat_wr_addr;  // 12 位地址（高 4 位选 bank）
input    [1:0] cdma2buf_dat_wr_hsel;  // 选 c0/c1
input [1023:0] cdma2buf_dat_wr_data;  // 一次写 1024 位 = c0(512)+c1(512)

input         cdma2buf_wt_wr_en;      // CDMA 写权重：使能
input  [11:0] cdma2buf_wt_wr_addr;
input         cdma2buf_wt_wr_hsel;    // 选 c0 还是 c1（1 位）
input [511:0] cdma2buf_wt_wr_data;    // 一次写 512 位

output [1023:0] sc2buf_dat_rd_data;   // CSC 读特征图：1024 位
output [1023:0] sc2buf_wt_rd_data;    // CSC 读权重：1024 位
output [1023:0] sc2buf_wmb_rd_data;   // CSC 读权重掩码位图：1024 位
```

注意端口注释里透露了关键规格（[NV_NVDLA_cbuf.v:41-42](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cbuf/NV_NVDLA_cbuf.v#L41-L42)）：`sc2buf_dat_rd_nvdla_ram_addr_ADDR_WIDTH_12_BE_1`、`DATA_WIDTH_1024`，即 dat/wt 读地址 12 位、数据 1024 位；`sc2buf_wmb_rd_nvdla_ram_addr_ADDR_WIDTH_8`，即 wmb 只有 8 位地址（因为它固定只读 bank 15，无需 bank 位，见 4.2）。

**真正构成 CBUF 的 32 块 RAM** 在文件中段被例化。每一块的类型都是 `nv_ram_rws_256x512`（rws = 一块可读可写的单口宏，256 行 × 512 列）：

[NV_NVDLA_cbuf.v:3098-3107](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cbuf/NV_NVDLA_cbuf.v#L3098-L3107) —— bank 0 的两个 column 例化：

```verilog
nv_ram_rws_256x512 u_cbuf_ram_bank0_column0 (
   .clk           (nvdla_core_clk)
  ,.ra            (cbuf_ra_b0c0[7:0])     // 读地址（8 位行号）
  ,.re            (cbuf_re_b0c0)          // 读使能
  ,.dout          (cbuf_rdat_b0c0[511:0]) // 读出 512 位
  ,.wa            (cbuf_wa_b0c0_d2[7:0])  // 写地址
  ,.we            (cbuf_we_b0c0_d2)       // 写使能
  ,.di            (cbuf_wdat_b0c0_d2[511:0]) // 写入 512 位
  ,.pwrbus_ram_pd (pwrbus_ram_pd[31:0])   // 电源门控
);
// bank0_column1 紧随其后，结构完全相同
```

容量算式（独立公式）：

\[
\text{Capacity} = 16\ \text{bank} \times 2\ \text{column} \times 256\ \text{行} \times 512\ \text{bit} = 4\,194\,304\ \text{bit} = 512\ \text{KiB}
\]

这 32 块 RAM 用同样的模板展开，命名规律是 `u_cbuf_ram_bank{0..15}_column{0,1}`。

> ⚠️ **千万别和 CDMA 的 `shared_buffer` 搞混**。CDMA 内部还有一个 [NV_NVDLA_CDMA_shared_buffer.v:2180](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_CDMA_shared_buffer.v#L2180) 里的 `u_shared_buffer_00..15`，它们用的是 `nv_ram_rws_16x256`（16 行 × 256 位，很小），是 CDMA 做 scatter-gather 与像素处理的**内部暂存**，数据经它和 cvt 转换后**才**通过 `cdma2buf_dat_wr` 写进 CBUF。一句话：`shared_buffer` 是 CDMA 的私人物品，CBUF 是 CDMA 和 CSC 共享的公共水池。

#### 4.1.4 代码实践

**实践目标**：亲手核实 CBUF 的物理构成与总容量，建立“512 KB / 32 块 RAM”的直觉。

**操作步骤**：

1. 打开 [NV_NVDLA_cbuf.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cbuf/NV_NVDLA_cbuf.v)，搜索字符串 `nv_ram_rws_256x512`，统计它被例化了多少次。
2. 搜索 `u_cbuf_ram_bank15_column1`，确认 bank 编号确实从 0 跨到 15、column 到 1。
3. 打开 [vmod/rams/synth/nv_ram_rws_256x512.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/rams/synth/nv_ram_rws_256x512.v)（或同名 model），看它的位宽与深度，验证 “256×512” 的含义。

**需要观察的现象**：

- `nv_ram_rws_256x512` 应当出现 **32 次**（16 bank × 2 column）。
- 每块 RAM 的读地址端口都是 `[7:0]`（8 位 = 256 行），数据口都是 512 位。

**预期结果**：32 块 × 256 × 512 bit = 4 194 304 bit = 512 KiB，与 4.1.2 的算式吻合。如果你得到的不是 32，说明你统计时把 `nv_ram_rws_16x256`（CDMA shared_buffer，同文件目录下另一种型号）也算进来了——注意区分型号后缀。

#### 4.1.5 小练习与答案

**练习 1**：如果把 CBUF 的 bank 数从 16 翻倍到 32（每块 RAM 规格不变），总容量和数据口位宽分别会怎样变化？

**参考答案**：总容量翻倍到 1 MiB（32×2×256×512 bit）。但数据口位宽不变——位宽由「每 bank 的 column 数 × 每块 RAM 位宽」决定，与 bank 数无关；bank 数只影响可并行访问的存储块数量和地址中 bank 域的位数。

**练习 2**：CBUF 的 RAM 为什么用 `pwrbus_ram_pd` 这个端口？它和讲义里提到的 `slcg`（时钟门控）是不是一回事？

**参考答案**：`pwrbus_ram_pd` 是 RAM 自身的电源/Retention 门控输入，让空闲 RAM 进入保持或断电以省漏电。它与 `slcg`（second-level clock gating，关时钟）是两种不同机制——`pwrbus_ram_pd` 管电源/漏电，`slcg` 管动态翻转功耗，二者常配合使用。

---

### 4.2 bank 组织

#### 4.2.1 概念说明

“bank（存储块）”是把一大块 SRAM 切成多块小 SRAM 的组织方式。CBUF 用 bank 化设计有三个好处：

1. **并行度**：每个 bank 是独立的 RAM 宏，可并行读写，避免单块大 RAM 成瓶颈。
2. **数据/权重分区**：同一层里，特征图和权重都要住进 CBUF。把 16 个 bank 切成「数据区 + 共享区 + 权重区」，就能灵活决定这一层给数据多少、给权重多少。
3. **位宽拼装**：一次 1024 位的 dat 访问 = 2 个 column × 512 位，天然映射到同一 bank 的 c0、c1 两块 RAM 上。

CBUF 的 12 位地址被切成两段：

```text
addr[11:0]  =  { bank[3:0] , row[7:0] }
                  addr[11:8]  addr[7:0]
                  选 16 个     选 bank 内
                   bank         256 行
```

#### 4.2.2 核心流程

CBUF 的 16 个 bank 按“谁能写/谁能读”分成三类：

| bank | 写入者（CDMA） | 读出者（CSC） | 角色 |
|------|----------------|---------------|------|
| **bank 0** | 仅 dat（p0） | 仅 dat（p0） | 数据专用 |
| **bank 1 ~ 14** | dat（p0）**或** wt（p1） | dat（p0）**或** wt（p1） | **数据与权重共享** |
| **bank 15** | 仅 wt（p1） | wt（p1）**或** wmb（p2） | 权重 + 权重掩码专用 |

这种“两端专用、中间共享”的布局让数据/权重的容量配比可以在 bank 粒度上滑动：若某层特征图大、权重小，CDMA 就把数据多铺到中间共享 bank；反之则把权重往中间铺。bank 0 永远归数据、bank 15 永远归权重（及其掩码），保证两类数据各自至少有一个落脚点，互不挤占到底。

> 为什么 wmb 只读 bank 15？因为 wmb（权重掩码位图）只在权重压缩模式下存在，量很小，和权重同住权重区即可；它用一个独立的 8 位地址（256 行）寻址 bank 15 内的空间。

#### 4.2.3 源码精读

**地址切片**——把 12 位地址的高 4 位拿来当 bank 选择：

[NV_NVDLA_cbuf.v:887-888](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cbuf/NV_NVDLA_cbuf.v#L887-L888) —— 写地址切 bank（读侧同理见 4.3）：

```verilog
assign cbuf_p0_wr_bank = cbuf_p0_wr_addr[12-1:8]; // = addr[11:8]
assign cbuf_p1_wr_bank = cbuf_p1_wr_addr[12-1:8];
```

**写时 bank 译码（one-hot）**——把 4 位 bank 号展开成 16 选 1 的使能。bank 0 只可能被 dat 写：

[NV_NVDLA_cbuf.v:894-904](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cbuf/NV_NVDLA_cbuf.v#L894-L904) —— dat（p0）写 bank 0 的两个 column（只有 `cbuf_p0_wr_bank==0` 一种条件，没有 p1）：

```verilog
always @( cbuf_p0_wr_bank or cbuf_p0_wr_lo_en_d1_w ) begin
    cbuf_p0_wr_sel_ram_b0c0_w = (cbuf_p0_wr_bank == 4'd0)
                                && (cbuf_p0_wr_lo_en_d1_w == 1'b1);
end
always @( cbuf_p0_wr_bank or cbuf_p0_wr_hi_en_d1_w ) begin
    cbuf_p0_wr_sel_ram_b0c1_w = (cbuf_p0_wr_bank == 4'd0)
                                && (cbuf_p0_wr_hi_en_d1_w == 1'b1);
end
```

对比之下，bank 15 只可能被 wt 写：

[NV_NVDLA_cbuf.v:1354-1368](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cbuf/NV_NVDLA_cbuf.v#L1354-L1368) —— wt（p1）写 bank 15（条件是 `cbuf_p1_wr_bank==15`，没有 p0）：

```verilog
always @( cbuf_p1_wr_bank or cbuf_p1_wr_lo_en_d1_w ) begin
    cbuf_p1_wr_sel_ram_b15c0_w = (cbuf_p1_wr_bank == 4'd15)
                                 && (cbuf_p1_wr_lo_en_d1_w == 1'b1);
end
```

而**中间的共享 bank**，写使能是 dat 与 wt 两路的“或”：

[NV_NVDLA_cbuf.v:1454-1460](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cbuf/NV_NVDLA_cbuf.v#L1454-L1460) —— bank 1 的写使能 = p0（dat） OR p1（wt）：

```verilog
always @(posedge nvdla_core_clk or negedge nvdla_core_rstn) begin
  if (!nvdla_core_rstn) cbuf_we_b1c0 <= 1'b0;
  else cbuf_we_b1c0 <= cbuf_p0_wr_sel_ram_b1c0_w | cbuf_p1_wr_sel_ram_b1c0_w;
end
```

这正是“共享 bank”在电路上的体现：同一块 RAM，dat 和 wt 都可能写它，谁的地址落到这个 bank、谁就驱动写使能（时序由 CDMA 内部保证 dat 与 wt 不会同周期争抢同一行）。bank 0 的写使能则只有 `cbuf_p0_wr_sel_ram_b0c0_w` 一项（见 [NV_NVDLA_cbuf.v:1442-1446](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cbuf/NV_NVDLA_cbuf.v#L1442-L1446)），印证了“bank 0 数据专用”。

#### 4.2.4 代码实践

**实践目标**：用一张表把 16 个 bank 的“写入者归属”穷举清楚，验证“两端专用、中间共享”。

**操作步骤**：

1. 在 [NV_NVDLA_cbuf.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cbuf/NV_NVDLA_cbuf.v) 中定位写使能寄存器段（约 1440–1590 行，`cbuf_we_bNcM <= ...`）。
2. 逐个 bank 抄下 `cbuf_we_bNc0` 的右值表达式，记录它是 `p0 only`、`p1 only` 还是 `p0 | p1`。

**需要观察的现象**：

- `cbuf_we_b0c0` 右值 = `cbuf_p0_wr_sel_ram_b0c0_w`（p0 only）
- `cbuf_we_b1c0` ~ `cbuf_we_b14c0` 右值 = `..._p0_... | ..._p1_...`（共享）
- `cbuf_we_b15c0` 右值 = `cbuf_p1_wr_sel_ram_b15c0_w`（p1 only）

**预期结果**：得到一张 16 行的表，首尾两行分别是 p0 only / p1 only，中间 14 行都是 p0|p1，与 4.2.2 的分区表完全一致。这从电路层面坐实了 bank 分区策略。

#### 4.2.5 小练习与答案

**练习 1**：若某层权重极小（只占 1 个 bank 都嫌多），数据能否用到 bank 15？

**参考答案**：不能。bank 15 的写使能只接 wt（p1），dat（p0）的写译码根本不产生 `cbuf_p0_wr_sel_ram_b15*` 信号，所以 dat 写不进 bank 15。这是硬接线决定的，bank 15 永远留给权重（与 wmb）。数据最多用到 bank 14。

**练习 2**：地址 `cdma2buf_dat_wr_addr = 12'hB24` 对应哪个 bank、哪一行？

**参考答案**：`addr[11:8] = 0xB = 11`，故 bank 11；`addr[7:0] = 0x24 = 36`，故 bank 11 内第 36 行。由于 bank 11 在共享区，这个写若来自 dat 口会落在 bank 11 的 c0/c1（由 hsel 决定）。

---

### 4.3 CDMA 写 / CSC 读

#### 4.3.1 概念说明

CBUF 有 **2 个写口**（都在 CDMA 侧）和 **3 个读口**（都在 CSC 侧）：

- **写口**：`cdma2buf_dat_wr`（写特征图，1024 位）、`cdma2buf_wt_wr`（写权重，512 位）。
- **读口**：`sc2buf_dat_rd`（读特征图）、`sc2buf_wt_rd`（读权重）、`sc2buf_wmb_rd`（读权重掩码位图），均 1024 位。

注意一个不对称：**权重写是 512 位、读是 1024 位**。原因是权重按 512 位粒度写入某一 column，但读出时 CSC 一次把同一 bank 的 c0+c1 两块（共 1024 位）一起读出来凑成宽 ATOM 喂阵列。特征图则是写读都 1024 位（一次写满/读满两个 column）。

读操作是**固定延迟**的（无 ready/valid 握手，纯时序）：发地址后若干拍，数据稳定回来。这种设计让 CSC 可以精确按节拍调度，不必每拍都握手。

#### 4.3.2 核心流程

一次卷积窗口数据流经 CBUF 的全过程：

```text
1. CDMA 经 MCIF/CVIF 从片外取来一批特征图块/权重块
2. CDMA 内部 shared_buffer 暂存、cvt 做格式/像素处理
3. CDMA 拉高 cdma2buf_dat_wr_en，给 addr(=bank|row)+1024 位 data+hsel
      └─ CBUF 把数据写进对应 bank 的 c0/c1（dat 写 bank0~14）
4. CDMA 拉高 cdma2buf_wt_wr_en，512 位 data 写进权重区（wt 写 bank1~15）
5. CSC 计算好下一步要哪些 ATOM，拉高 sc2buf_dat_rd_en + addr
6. CBUF 经若干拍读延迟，回送 sc2buf_dat_rd_valid + 1024 位 data
7. CSC 把数据按节拍分发给 CMAC 阵列
```

读延迟链（以 dat 读为例，命名里的 `dN` 表示第 N 拍寄存器）：

```text
sc2buf_dat_rd_addr ─▶ bank 译码(d1~d3) ─▶ RAM(1 拍) ─▶ d4 选 bank ─▶ d5 ─▶ d6 输出
       p0_rd_en ────────────────────────────────────────────────▶ p0_rd_valid_d6
```

最终 `sc2buf_dat_rd_data = cbuf_p0_rd_data_d6`，即地址发出后第 6 拍数据有效。读延迟的存在正是 CBUF 能让 CDMA 和 CSC 解耦的关键——CSC 只要提前几拍把读请求发出去，就能保证 MAC 阵列不断粮。

#### 4.3.3 源码精读

**写侧**：CDMA 的写信号先改名/切位宽，再生成 bank 写使能。

[NV_NVDLA_cbuf.v:860-871](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cbuf/NV_NVDLA_cbuf.v#L860-L871) —— 把 `cdma2buf_*` 端口接到内部 `cbuf_p0/p1_wr_*`，并用 `hsel` 拆分高低 512 位：

```verilog
assign cbuf_p0_wr_en       = cdma2buf_dat_wr_en;
assign cbuf_p0_wr_lo_en    = cdma2buf_dat_wr_en & cdma2buf_dat_wr_hsel[0]; // c0
assign cbuf_p0_wr_hi_en    = cdma2buf_dat_wr_en & cdma2buf_dat_wr_hsel[1]; // c1
assign cbuf_p0_wr_lo_data  = cdma2buf_dat_wr_data[512-1:0];               // 低 512 位
assign cbuf_p0_wr_hi_data  = cdma2buf_dat_wr_data[512*2-1:512];           // 高 512 位
...
assign cbuf_p1_wr_data     = cdma2buf_wt_wr_data;  // 权重仅 512 位
```

**读侧**：CSC 的读请求经 bank 译码，得到每块 RAM 的读使能与行地址。

[NV_NVDLA_cbuf.v:3485-3499](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cbuf/NV_NVDLA_cbuf.v#L3485-L3499) —— 三个读口的地址接入与 bank 切片：

```verilog
assign cbuf_p0_rd_en    = sc2buf_dat_rd_en;
assign cbuf_p0_rd_addr  = sc2buf_dat_rd_addr[12-1:0];  // dat 读：12 位
assign cbuf_p1_rd_en    = sc2buf_wt_rd_en;
assign cbuf_p1_rd_addr  = sc2buf_wt_rd_addr[12-1:0];   // wt 读：12 位
assign cbuf_p2_rd_en    = sc2buf_wmb_rd_en;
assign cbuf_p2_rd_addr  = sc2buf_wmb_rd_addr;          // wmb 读：8 位（固定 bank15）

assign cbuf_p0_rd_bank = cbuf_p0_rd_addr[12-1:8];      // 读 bank 切片
assign cbuf_p1_rd_bank = cbuf_p1_rd_addr[12-1:8];
```

[NV_NVDLA_cbuf.v:3501-3515](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cbuf/NV_NVDLA_cbuf.v#L3501-L3515) —— dat 读 bank 0 译码（只有 `cbuf_p0_rd_bank==0`）：

```verilog
always @( cbuf_p0_rd_bank or cbuf_p0_rd_en ) begin
    cbuf_p0_rd_sel_ram_b0c0_w = (cbuf_p0_rd_bank == 4'd0) && (cbuf_p0_rd_en == 1'b1);
end
```

**wmb 读固定指向 bank 15**（没有 bank 比较条件，只要 `p2_rd_en` 有效就选 bank 15）：

[NV_NVDLA_cbuf.v:3981-3991](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cbuf/NV_NVDLA_cbuf.v#L3981-L3991)：

```verilog
always @( cbuf_p2_rd_en ) begin
    cbuf_p2_rd_sel_ram_b15c0_w = (cbuf_p2_rd_en == 1'b1);  // 无条件指向 bank15
end
```

**共享 bank 的读使能**同样是 dat 与 wt 两路 OR：

[NV_NVDLA_cbuf.v:4005-4010](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cbuf/NV_NVDLA_cbuf.v#L4005-L4010)：

```verilog
always @( cbuf_p0_rd_sel_ram_b1c0_w or cbuf_p1_rd_sel_ram_b1c0_w ) begin
    cbuf_re_b1c0_w = cbuf_p0_rd_sel_ram_b1c0_w | cbuf_p1_rd_sel_ram_b1c0_w;
end
```

**读出数据的 bank 合并**——把 16 个 bank 各自读出的 512 位“或”到一起（只有命中那一 bank 的数据透传，其余被掩码清零），再拼成 1024 位：

[NV_NVDLA_cbuf.v:5885-5889](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cbuf/NV_NVDLA_cbuf.v#L5885-L5889)：

```verilog
cbuf_p0_rd_data_d4_w =
   ({{512{cbuf_p0_rd_sel_ram_b0c0_d3}}, {512{cbuf_p0_rd_sel_ram_b0c1_d3}}} & {cbuf_rdat_b0c0_d3, cbuf_rdat_b0c1_d3}) |
   ({{512{cbuf_p0_rd_sel_ram_b1c0_d3}}, {512{cbuf_p0_rd_sel_ram_b1c1_d3}}} & {cbuf_rdat_b1c0_d3, cbuf_rdat_b1c1_d3}) |
   ... // 共 16 项，每项一个 bank，命中项的 sel 展开成全 1 掩码
```

这是典型的 **AND-OR 多路选择**：每个 bank 的 512 位读出与“自己的 sel（复制 512 份）”相与，再把 16 个 bank 的结果相或——因为同一时刻只有一个 bank 命中，等价于 16 选 1。`{c0, c1}` 拼接给出 1024 位。

**最终输出**：经过流水寄存器到第 6 拍送出：

[NV_NVDLA_cbuf.v:6290-6296](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cbuf/NV_NVDLA_cbuf.v#L6290-L6296)：

```verilog
assign sc2buf_dat_rd_data  = cbuf_p0_rd_data_d6;
assign sc2buf_dat_rd_valid = cbuf_p0_rd_valid_d6;
assign sc2buf_wt_rd_data   = cbuf_p1_rd_data_d6;
assign sc2buf_wmb_rd_data  = cbuf_p2_rd_data_d6;
```

最后，**CBUF 在哪里被连进卷积分区**：

[NV_NVDLA_partition_c.v:1669-1683](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_c.v#L1669-L1683) —— CBUF 被例化为 `u_NV_NVDLA_cbuf`，CDMA 的 `cdma2buf_dat_wr_*` 写口与 CSC 的 `sc2buf_dat_rd_*` 读口在这里交汇：

```verilog
NV_NVDLA_cbuf u_NV_NVDLA_cbuf (
   ...
  ,.cdma2buf_dat_wr_en   (cdma2buf_dat_wr_en)
  ,.cdma2buf_dat_wr_addr (cdma2buf_dat_wr_addr[11:0])
  ,.cdma2buf_dat_wr_data (cdma2buf_dat_wr_data[1023:0])
  ...
  ,.sc2buf_dat_rd_en     (sc2buf_dat_rd_en)
  ,.sc2buf_dat_rd_addr   (sc2buf_dat_rd_addr[11:0])
  ,.sc2buf_dat_rd_valid  (sc2buf_dat_rd_valid)
  ...
);
```

而 CSC 一侧的对应端口可在 [NV_NVDLA_csc.v:615-631](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_csc.v#L615-L631) 看到（`output sc2buf_dat_rd_en/addr`、`input sc2buf_dat_rd_data/valid`），与本讲端口表一一对应。

#### 4.3.4 代码实践

**实践目标**：用波形/信号的思路跟踪一次「CDMA 写 → CSC 读」穿越 CBUF 的完整时序，理解固定读延迟。

**操作步骤**：

1. 在 [NV_NVDLA_cbuf.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cbuf/NV_NVDLA_cbuf.v) 中搜索 `cbuf_p0_rd_en_d5`、`cbuf_p0_rd_valid_d6`、`cbuf_p0_rd_data_d6`，把读延迟链上每一拍寄存器列出来（d1→d2→d3→RAM→d4→d5→d6）。
2. 确认 `sc2buf_dat_rd_data` 与 `sc2buf_dat_rd_valid` 都取自 `_d6` 后缀信号。
3. （可选）在仿真里（参考 u1-l4 的 `make run`）对 `sc2buf_dat_rd_en` 与 `sc2buf_dat_rd_valid` 抓波形，数两者之间相隔几拍。

**需要观察的现象**：

- 读使能 `sc2buf_dat_rd_en` 拉高后，约第 6 个 `nvdla_core_clk` 上升沿，`sc2buf_dat_rd_valid` 才跟着拉高、数据同时有效。
- 中间 d1~d5 是 bank 译码、RAM 访问、数据选通与流水对齐的各级寄存器。

**预期结果**：读延迟为固定 6 拍（地址寄存到数据有效）。这也解释了为什么 CSC 必须提前若干拍发读请求——它是 CBUF 与 CSC 之间“预取式”协作的时序基础。

> 如果你没有仿真环境，无法确认波形，请标注「待本地验证」；但 d6 后缀在源码里是确定的，读延迟为 6 拍这一结论可直接从信号命名得出。

#### 4.3.5 小练习与答案

**练习 1**：权重写口是 512 位、读口是 1024 位。一次权重读返回的 1024 位里，c0 和 c1 分别对应什么？

**参考答案**：对应同一 bank 的两个 column 在同一行上的两块 512 位数据。权重写入时由 `cdma2buf_wt_wr_hsel` 指定落到 c0 或 c1，连续两次写分别填满 c0、c1 后，CSC 一次读就把 c0+c1 共 1024 位一起取出，凑成一个宽 ATOM 供阵列使用。

**练习 2**：读数据合并为什么用“AND-OR 掩码”而不是 `case` 语句做 16 选 1？

**参考答案**：AND-OR 结构对综合友好：16 个 bank 的读出数据并行做 `与各自 sel` 后相或，是一级组合逻辑，时序可控、面积可预期；而多路 `case` 在宽位（512/1024 位）下可能退化成大型多路选择器。此外由于“同一时刻只有一个 bank 命中”的不变式，AND-OR 在语义上等价于 16 选 1。

**练习 3**：CDMA 与 CSC 会同时访问 CBUF 吗？会冲突吗？

**参考答案**：会同时访问（CBUF 是双口使用：一边写一边读）。但 CBUF 内部靠 bank 划分与 CDMA/CSC 各自的调度保证不会同周期对同一 RAM 同一地址既写又读产生不确定——dat 写与 dat 读可并行（不同 bank 或由上层时序错开），读是固定延迟、写是单拍。真正的“满/空”协调发生在更上层：CSC 通过向 CDMA 回送信用/待取信息来决定何时读，确保读到的都是 CDMA 已写完的有效数据。

## 5. 综合实践

把三个最小模块串起来，完成一次「纸面追踪」：

**任务**：一个卷积层，输入特征图较大、权重较小。请按以下步骤，把数据在 CBUF 里的存放与读取讲清楚。

1. **规划容量**：假设该层输入特征图需要占用约 6 个 bank、权重需要约 2 个 bank。参照 4.2 的分区规则，写出数据与权重各自落在哪些 bank 编号上（提示：数据从 bank 0 起占，权重从 bank 15 往前占）。
2. **写地址构造**：CDMA 要把一段特征图写到 bank 3 的第 0x10 行、两个 column 都写，请给出 `cdma2buf_dat_wr_addr`、`cdma2buf_dat_wr_hsel`、`cdma2buf_dat_wr_en` 的取值。
3. **读时序**：CSC 要从 bank 3 读这批数据，请说明 `sc2buf_dat_rd_addr` 的高 4 位应是多少，并指出拉高 `sc2buf_dat_rd_en` 后第几拍能在 `sc2buf_dat_rd_data` 上拿到 1024 位结果。
4. **对照源码核验**：用本讲给出的永久链接，分别到 [NV_NVDLA_cbuf.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cbuf/NV_NVDLA_cbuf.v) 的写译码段（约 894 行）、读地址段（约 3485 行）、输出段（约 6290 行）核对你的答案。

**参考要点**：

1. 数据占 bank 0~5（6 个），权重占 bank 15、14（2 个）；中间 bank 6~13 这一层留空或作弹性共享区。
2. `addr = {4'd3, 8'h10} = 12'h310`；`hsel = 2'b11`（c0、c1 都写）；`wr_en = 1'b1`。
3. `sc2buf_dat_rd_addr[11:8] = 4'd3`；第 6 拍拿到 `sc2buf_dat_rd_data`。
4. 写译码：bank 3 命中 `cbuf_p0_wr_sel_ram_b3c0/c1_w`；读地址：`cbuf_p0_rd_bank==3`；输出：`sc2buf_dat_rd_data = cbuf_p0_rd_data_d6`。

## 6. 本讲小结

- **CBUF 是卷积核心的 512 KB 片上 SRAM 工作缓冲**，夹在 CDMA（写）与 CSC（读）之间，用 32 块 `nv_ram_rws_256x512`（16 bank × 2 column）拼成，容量 \(16\times2\times256\times512=4\,194\,304\) bit。
- **地址 12 位 = bank[11:8] + row[7:0]**：高 4 位选 16 个 bank，低 8 位选 bank 内 256 行。
- **bank 分区策略**：bank 0 数据专用、bank 15 权重+wmb 专用、bank 1~14 数据与权重共享（写/读使能为 dat OR wt）。
- **两个写口 + 三个读口**：dat 写/读 1024 位；wt 写 512 位、读 1024 位；wmb 读 1024 位（固定 bank 15）。读为固定延迟，约 6 拍。
- **读出用 AND-OR 掩码做 16 选 1**：命中 bank 的 sel 展开成全 1 掩码透传数据，{c0,c1} 拼成 1024 位。
- **切勿与 CDMA 的 `shared_buffer`（`16×256` 小暂存）混淆**：后者是 CDMA 内部 scatter-gather/像素处理暂存，数据经它和 cvt 后才写进 CBUF。

## 7. 下一步学习建议

- **向后看（u3-l4）**：CBUF 把数据交给谁？下一讲《CSC：时隙/条带控制器与权重分发》将讲清 CSC 如何用 `sc2buf_*` 读口按节拍把 CBUF 里的 ATOM 取出来、组装成 slot，再分发给 CMAC 阵列。届时你会看到 CBUF 读侧的 `sc2buf_dat_rd_en/addr` 是如何被 CSC 的 slot 调度器驱动的。
- **横向深入**：想了解 CBUF 这类大缓冲在综合时如何映射到真实工艺 RAM，可先读 u6-l3《RAM 行为模型与综合模型》，对照 `nv_ram_rws_256x512` 的 model 版与 synth 版。
- **回头印证**：重读 u3-l2 CDMA 的 `shared_buffer→cvt→CBUF` 路径描述，现在你应该能精确指出 shared_buffer 与 CBUF 各自的 RAM 型号与容量差异。
