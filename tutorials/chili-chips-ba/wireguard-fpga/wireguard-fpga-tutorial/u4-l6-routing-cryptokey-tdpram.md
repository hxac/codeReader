# 路由表与密钥表的 tdp_ram 实现

## 1. 本讲目标

本讲把视线收回到 Unit 4 一开始就反复提到的「两条大表」——路由表（routing_table）与密钥表（cryptokey_table）。学完本讲，你应当能够：

1. 说清 SystemRDL 里的 `external regfile` 在 RTL 里到底由什么物理存储实现，以及为什么是双口 RAM。
2. 读懂 `dpe.sv` 里两个 `tdp_ram` 实例的端口连接：A 口接 CSR、B 口预留给数据面。
3. 复述 `req`/`ack` 握手的读写时序差异（读一拍延迟、写同拍应答），并解释字节地址到字地址的 `>> 2` 转换。
4. 独立算出两条表各占多少字节，并解释密钥表为何需要 11 位地址。

本讲依赖 [u4-l1（DPE 总体结构）](u4-l1-dpe-overview-axis.md) 与 [u3-l1（SystemRDL 寄存器规格）](u3-l1-systemrdl-spec.md)。前者让你知道两条表挂在 DPE 上、被 mux/egress/demux 读写；后者让你知道 `external regfile` 这一关键字在规格文件里的含义。

## 2. 前置知识

- **external regfile（外部寄存器文件）**：在 SystemRDL 里，普通 `reg` 由 PeakRDL 自动生成触发器实现的存储体；而标了 `external` 的 `regfile` 只生成「地址译码 + 请求/应答握手」外壳，真正的存储体要由设计者**自己**在 RTL 里提供。后面会看到，本项目用双口 RAM 当这个存储体。
- **双口 RAM（Dual-Port RAM）**：一块可以被两个端口同时访问的存储器。每个端口都有自己的地址线、数据线、读/写控制。本项目用的是「真双口」（True Dual-Port，TDP），A、B 两口都能独立读写。
- **字地址 vs 字节地址**：CSR 总线用字节地址（每个字节一个地址），而 RAM 一般按字（word，这里是 32 位 = 4 字节）编址。把字节地址右移 2 位（`>> 2`）就得到字地址。
- **握手（handshake）**：一方发 `req`（请求），另一方回 `ack`（应答），用来跨模块同步一次访问的完成。本讲的 `req`/`ack` 是 PeakRDL 为 external regfile 自动生成的标准信号。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [1.hw/ip.infra/tdp_ram.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/tdp_ram.sv) | 通用的真双口 RAM 模块，是两条表的物理存储体。 |
| [1.hw/ip.dpe/dpe.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv) | 数据面引擎顶层；在这里实例化两个 `tdp_ram`，把 external regfile 的 CSR 握手对接到 RAM 的 A 口。 |
| [3.build/csr_build/csr.rdl](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl) | 单一真源；用 `external regfile` 声明两条表，规定了条目数、每条目字段和基地址。 |
| [3.build/csr_build/generated-files/csr_pkg.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr_pkg.sv) | PeakRDL 生成的类型包；定义了 external regfile 的请求/应答结构体，能看出地址位宽。 |

## 4. 核心概念与源码讲解

### 4.1 external regfile：从 SystemRDL 到 tdp_ram

#### 4.1.1 概念说明

在 [u3-l1](u3-l1-systemrdl-spec.md) 我们已经知道：普通寄存器（如 `gpio`、`hw_id`）PeakRDL 会自动用触发器（flip-flop）实现。但路由表有 64 条目、每条目 4 个字，密钥表更有 64 条目、每条目 30 个字——如果全用触发器实现，会吃掉几千个触发器，面积和功耗都划不来。

SystemRDL 的 `external` 关键字就是为这种场景准备的：它告诉生成器「存储体我自己来提供，你只管生成地址译码和访问握手」。于是 PeakRDL 不会为这两条表生成触发器阵列，而是生成一组标准的请求/应答信号，留给设计者接到任意存储体上。本项目选择用**双口 RAM**（`tdp_ram`）来填这个空，原因有二：

- **省面积**：RAM 综合后会映射到 FPGA 的 Block RAM（BRAM）硬核，远比触发器便宜。
- **要双口**：路由表既要被控制面（CPU 经 CSR）写入，又要被数据面（流水线查找）读取。双口让两个方向互不干扰、可同时访问。

#### 4.1.2 核心流程

```text
csr.rdl (external regfile)
        │  PeakRDL 生成
        ▼
csr_pkg.sv:  定义请求结构体 csr__...__external__out_t  (req, addr, req_is_wr, wr_data, ...)
             定义应答结构体 csr__...__external__in_t   (rd_ack, rd_data, wr_ack)
        │  设计者在 dpe.sv 里手动接线
        ▼
tdp_ram 实例:  A 口 ↔ CSR 请求/应答 (控制面 CPU 读写)
              B 口 ↔ 数据面查找      (当前 HEAD 预留未接)
```

#### 4.1.3 源码精读

两条表在 `csr.rdl` 里都用 `external regfile` 声明，注意行尾的基地址 `@ 0x0400` 与 `@ 0x2000`：

[csr.rdl:L527-L579](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L527-L579) —— 路由表声明：`external regfile routing_table`，内含 `entry[64]`（64 条目），每条目 4 个 32 位寄存器 `ip / mask / peer_idx / dst`，基地址 `0x0400`。

[csr.rdl:L581-L927](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L581-L927) —— 密钥表声明：`external regfile cryptokey_table`，内含 `entry[64]`，每条目 30 个 32 位寄存器（本地/远端身份、加解密各 256 位密钥、收发计数器），基地址 `0x2000`。

PeakRDL 为 external regfile 生成的请求/应答类型在 `csr_pkg.sv` 里。**注意地址位宽的不同**——这是后续 `ADDR_WIDTH` 取值的依据：

[csr_pkg.sv:L421-L435](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr_pkg.sv#L421-L435) —— 请求结构体：路由表 `addr` 是 10 位（覆盖 1024 字节），密钥表 `addr` 是 13 位（覆盖 8192 字节）；还带一个 `wr_biten`（字节写使能）。

[csr_pkg.sv:L183-L193](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr_pkg.sv#L183-L193) —— 应答结构体：只有 `rd_ack / rd_data / wr_ack` 三个字段。

> 提示：`from_csr` 是 `csr__out_t`（CSR 块**输出**到外部存储的请求），`to_csr` 是 `csr__in_t`（外部存储返回给 CSR 块的应答）。名字是站在 CSR 块的视角起的，初学时容易搞反。

#### 4.1.4 代码实践

**实践目标**：确认「external → 自己提供存储体」这条链路确实成立。

**操作步骤**：

1. 打开生成的 [csr.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr.sv)。
2. 搜索 `routing_table`，观察它只产生 `req / addr / req_is_wr / wr_data` 等请求信号、并把 `rd_data / rd_ack / wr_ack` 当输入，**没有**任何 `logic [...] mem [...]` 存储阵列。
3. 对比同文件里 `gpio` 等普通寄存器，它们有触发器实现。

**需要观察的现象**：external regfile 在生成 RTL 里只是「端口外壳」，存储体确实缺位，等着 `dpe.sv` 来补。

**预期结果**：你会清楚看到存储体不在 `csr.sv` 里，而在 `dpe.sv` 的 `tdp_ram` 实例里。这正是 `external` 的全部意义。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `external` 关键字从 `routing_table` 上去掉，会发生什么？

> **答案**：PeakRDL 会自动用触发器实现 64×4=256 个字的存储体，面积暴涨；同时 `dpe.sv` 里那两个 `tdp_ram` 实例的请求/应答信号会失去对接对象（结构体类型变了），编译报错。`external` 是「我要自己接管存储」的明确声明。

**练习 2**：`csr_pkg.sv` 里请求结构体带 `wr_biten`（字节写使能），但 `dpe.sv` 的 `tdp_ram` 实例根本没接它，为什么这样仍能正常工作？

> **答案**：两条表里每个字段都独占一个 32 位对齐的字（如 `peer_idx` 占自己的一个字、`dst` 占另一个字），写任何字段都是整字写，不存在「只改一字节」的需求，因此字节使能可忽略。

---

### 4.2 tdp_ram 双口 RAM 与 A/B 端口连接

#### 4.2.1 概念说明

`tdp_ram` 是一个参数化的真双口 RAM：A、B 两口各有独立的写使能、地址、输入、输出，共用同一块 `mem` 阵列。两口的地位完全对等，都能读写同一存储空间。在本项目里：

- **A 口**接控制面 CSR——CPU 经由它读写表项（配置路由、下发密钥）。
- **B 口**预留给数据面——流水线在转发时用它并行查表（如 TCAM 命中后读 peer/dst，加解密时读密钥）。

这种「一表两口、各司其职」的分工，正是数据面能线速查表、同时控制面能在线改表（配合 u3-l4 的 FCR 原子更新）的硬件基础。

#### 4.2.2 核心流程

`tdp_ram` 的访问时序是「**同步读、写优先**」：

```text
每个 posedge clk:
  Port A:  if (we_a) mem[addr_a] <= din_a;   // 先判写
           dout_a    <= mem[addr_a];          // 再读出（寄存输出）
  Port B:  if (we_b) mem[addr_b] <= din_b;
           dout_b    <= mem[addr_b];
```

要点：

- 读出的 `dout` 是**寄存后的**——地址在 T 拍给出，数据在 T+1 拍才出现在 `dout`。
- 同地址同拍既写又读时，写优先：写入新值，读出的是**旧值**（`<=` 非阻塞赋值，读用的是更新前的 `mem`）。

#### 4.2.3 源码精读

[tdp_ram.sv:L43-L59](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/tdp_ram.sv#L43-L59) —— 模块端口与参数：默认 `DATA_WIDTH=32`、`ADDR_WIDTH=8`，`DEPTH = (1 << ADDR_WIDTH)`，两口对称。

```systemverilog
module tdp_ram #(
   parameter DATA_WIDTH = 32,
   parameter ADDR_WIDTH = 8,
   parameter DEPTH = (1 << ADDR_WIDTH)
)(
   input  logic                    clk,
   input  logic                    we_a,    // A 口写使能
   input  logic [ADDR_WIDTH-1:0]   addr_a,
   input  logic [DATA_WIDTH-1:0]   din_a,
   output logic [DATA_WIDTH-1:0]   dout_a,
   ...  // B 口对称
);
```

[tdp_ram.sv:L60-L76](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/tdp_ram.sv#L60-L76) —— 存储阵列与两口读写逻辑，体现了上面说的「写优先、读寄存」。

在 `dpe.sv` 里，两个实例的端口连接如下（注意 A 口接 CSR、B 口全部预留）：

[dpe.sv:L105-L117](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L105-L117) —— 路由表实例，`ADDR_WIDTH=8`：

```systemverilog
tdp_ram #(
   .ADDR_WIDTH             (8)
) u_routing_table (
   .clk                    (from_cpu.clk),
   .we_a                   (from_csr.routing_table.req & from_csr.routing_table.req_is_wr),
   .addr_a                 (from_csr.routing_table.addr >> 2),  // 字节地址→字地址
   .din_a                  (from_csr.routing_table.wr_data),
   .dout_a                 (to_csr.routing_table.rd_data),
   .we_b                   (1'b0),   // B 口预留：未写
   .addr_b                 ('0),
   .din_b                  ('0),
   .dout_b                 ()        // 悬空：未读
);
```

[dpe.sv:L123-L135](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L123-L135) —— 密钥表实例，结构与路由表完全对称，只是 `ADDR_WIDTH=11`。

两个实例的对照如下：

| 实例 | `ADDR_WIDTH` | `DEPTH`（字数） | A 口 | B 口 |
|------|:---:|:---:|------|------|
| `u_routing_table` | 8 | 256 | 接 CSR 请求/应答 | 预留（`we_b=0`, `dout_b` 悬空） |
| `u_cryptokey_table` | 11 | 2048 | 接 CSR 请求/应答 | 预留（`we_b=0`, `dout_b` 悬空） |

> **关于 B 口现状（重要）**：当前 HEAD 处于 Phase1 PoC，数据面用的是直通的 `dpe_dummy_switch`（见 [dpe.sv:L78-L92](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L78-L92)），真正会查路由表/密钥表的 `dpe_egress_ip_lookup` 与 WireGuard 加解密块都被注释掉了。所以现在 B 口确实「空着」——`we_b` 恒 0、`dout_b` 不接。等加密流水线（Unit 5）上线后，B 口才会被驱动去读密钥、查路由。本讲讲清这套接线的「骨架」，上线后只是把 B 口的 `addr_b/dout_b` 连到数据面查找逻辑而已。

#### 4.2.4 代码实践

**实践目标**：把「字节地址 → 字地址」的转换对应到具体位宽。

**操作步骤**：

1. 在 [csr_pkg.sv:L421-L435](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr_pkg.sv#L421-L435) 读出两条表的 `addr` 位宽（路由表 10 位、密钥表 13 位）。
2. 在 [dpe.sv:L109,L128](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L105-L117) 找到 `addr >> 2`。
3. 核对：10 − 2 = 8（= 路由表 `ADDR_WIDTH`）；13 − 2 = 11（= 密钥表 `ADDR_WIDTH`）。

**需要观察的现象**：CSR 给的是字节地址，右移 2 位（除以 4）后正好等于 RAM 的字地址位宽。

**预期结果**：地址位宽的「10/13 − 2 = 8/11」与 `dpe.sv` 里两个实例的 `ADDR_WIDTH` 完全吻合，说明接线正确。

#### 4.2.5 小练习与答案

**练习 1**：为什么 A 口的 `we_a` 写成 `req & req_is_wr`，而不是直接用 `req`？

> **答案**：`req` 只表示「有一次访问请求」，不区分读写；`req_is_wr` 才标明这是写请求。只有在「有请求 **且** 是写」时才应改写 RAM 内容；读请求时 `we_a` 必须为 0，否则会把 `din_a`（此时的无效数据）误写进去。

**练习 2**：若数据面在 B 口对某字写、控制面同时在 A 口对同一字读，会怎样？

> **答案**：真双口 RAM 不提供硬件互斥。两口同时写同地址是未定义行为（设计上必须避免）；一写一读时读到旧值或新值不确定。正因如此，本项目用 FCR（[u3-l4](u3-l4-fcr-atomic-update.md)）在包边界暂停数据面后再改表，保证不会「写到一半被读」。

---

### 4.3 req/ack 握手与读写时序

#### 4.3.1 概念说明

external regfile 有一套标准的「请求—应答」握手：

- CSR 块（请求方）拉高 `req`，给出 `addr`、`req_is_wr`、`wr_data`。
- 存储体（应答方，即这里的 `tdp_ram`）完成访问后回 `rd_ack`/`wr_ack`，读访问还要回 `rd_data`。

这套握手存在的意义是：PeakRDL 不知道你背后接的存储体是几个时钟周期的延迟（BRAM 通常 1 拍，但也可更多），所以用 `ack` 来解耦——CSR 侧一直等到 `ack` 才认为访问完成。

#### 4.3.2 核心流程

读和写的时序**不对称**，这是本节最重要的结论：

```text
写访问 (write):
  T 拍:  req=1, req_is_wr=1, addr=..., wr_data=...
         → we_a = 1, 在 T 拍上升沿写入 mem
         → wr_ack = req & req_is_wr (组合逻辑, 同拍回)
  CSR 侧在 T 拍就拿到 wr_ack, 访问完成。

读访问 (read):
  T 拍:  req=1, req_is_wr=0, addr=...
         → we_a = 0, 不写; dout_a 在 T+1 拍才出现读出值
         → rd_ack <= req & ~req_is_wr (寄存器, T+1 拍回)
  CSR 侧在 T+1 拍同时拿到 rd_data 和 rd_ack, 访问完成。
```

为什么读要慢一拍？因为 `tdp_ram` 的读是**寄存输出**（`dout_a <= mem[addr_a]`），地址在 T 拍有效，数据 T+1 拍才出。为了和这慢一拍的数据对齐，`rd_ack` 也特意做成了寄存器，比 `req` 晚一拍。

#### 4.3.3 源码精读

[dpe.sv:L118-L121](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L118-L121) —— 路由表的应答逻辑（密钥表 [dpe.sv:L136-L139](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L136-L139) 完全对称）：

```systemverilog
// 读应答: 寄存器, 比 req 晚一拍, 与 rd_data 对齐
always_ff @(posedge from_cpu.clk) begin
   to_csr.routing_table.rd_ack <= from_csr.routing_table.req & ~from_csr.routing_table.req_is_wr;
end
// 写应答: 组合逻辑, 与 req 同拍
assign to_csr.routing_table.wr_ack = from_csr.routing_table.req & from_csr.routing_table.req_is_wr;
```

逐行解读：

- `rd_ack` 用 `always_ff`（寄存器），右端是 T 拍的 `req & ~req_is_wr`（有请求且是读），左端在 T+1 拍才生效——正好和 `dout_a`（也是 T+1 拍）对齐。
- `wr_ack` 用 `assign`（组合逻辑），当拍有效——因为写在 T 拍上升沿已完成，没有理由再等。
- `~req_is_wr` / `req_is_wr` 这一对保证了读、写应答互斥：一次访问只会触发其中一个。

> 命名小提示：`rd_data` 来自 RAM 的 `dout_a`，但「读哪个字」的地址仍由 CSR 在 `req` 那拍驱动到 `addr_a`；数据在下一拍返回。CSR 侧据此判断访问结束。

#### 4.3.4 代码实践

**实践目标**：亲手追踪一次 CPU 读路由表项的时序。

**操作步骤（源码阅读型）**：

1. 假设 CPU 要读路由表第 0 条目的 `ip` 字段（字节地址 = 基地址 `0x0400` + 0 = `0x0400`）。
2. 在 [dpe.sv:L105-L121](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L105-L121) 画出 T、T+1 两拍的信号值。

**需要观察的现象**（待本地验证/人工推演）：

| 时刻 | `req` | `req_is_wr` | `addr`(字节) | `addr>>2`(字) | `we_a` | `dout_a`/`rd_data` | `rd_ack` |
|------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| T | 1 | 0 | `0x100`(=0x400>>2 之前) | 0 | 0 | （旧值） | 0 |
| T+1 | 0/1 | … | … | … | … | `mem[0]`（第0条目 ip） | 1 |

> 注：`addr` 是字节地址，`0x0400 >> 2 = 0x100`，即字地址 0；上表「addr(字节)」列以字地址 0 对应的请求为准。

**预期结果**：T+1 拍 `rd_data` 与 `rd_ack` 同时有效，CSR 侧据此确认「读到了第 0 条目的 ip」。写访问则相反——`wr_ack` 在 T 拍即有效。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `rd_ack` 也改成组合逻辑（`assign`），会出什么问题？

> **答案**：`rd_ack` 会比 `rd_data` 早一拍（T 拍就回 ack，但数据 T+1 拍才到）。CSR 侧会在 T 拍看到 ack 并去取 `rd_data`，拿到的是旧值——读错。所以 `rd_ack` 必须寄存，与寄存输出的 `dout_a` 严格对齐。

**练习 2**：写访问的 `wr_ack` 为什么敢用组合逻辑、不担心数据没写进去？

> **答案**：`tdp_ram` 的写在 T 拍上升沿就完成了（`if (we_a) mem[addr_a] <= din_a`），到 T 拍结束时存储已更新。组合的 `wr_ack` 在 T 拍就反映「写请求已被接受且执行」，不存在数据滞后问题，所以无需等待。

---

## 5. 综合实践

把本讲的三块知识（external regfile、双口容量、握手时序）串起来，完成下面这张「两条表全口径核算表」。所有数字都要从源码推出来，不要背结论。

**任务**：填完下表的每一个空格，并回答末尾两个问题。

| 项目 | routing_table | cryptokey_table |
|------|---------------|-----------------|
| 条目数 | 64 | 64 |
| 每条目字数 | ?（A） | ?（B） |
| 总字数（实际用） | ?（C） | ?（D） |
| `ADDR_WIDTH` | 8 | 11 |
| `DEPTH`（分配字数 = 2^ADDR_WIDTH） | ?（E） | ?（F） |
| 字节地址位宽（看 csr_pkg.sv） | 10 | 13 |
| 分配容量（字节） | ?（G） | ?（H） |
| 基地址（看 csr.rdl） | `0x0400` | `0x2000` |

**步骤**：

1. 数 [csr.rdl:L531-L578](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L531-L578) 里路由表每条目的 `reg` 个数，得 (A)。
2. 数 [csr.rdl:L585-L926](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L585-L926) 里密钥表每条目的 `reg` 个数，得 (B)。提示：分 6 组数——本地身份(5)、远端身份(5)、encrypt_key(8)、decrypt_key(8)、send_cnt(2)、recv_cnt(2)。
3. (C) = 64 × (A)，(D) = 64 × (B)。
4. (E) = 2^8，(F) = 2^11。
5. (G) = (E) × 4，(H) = (F) × 4。

**问题 1**：cryptokey_table 实际只用 (D) 个字，为什么 `ADDR_WIDTH` 是 11、而不是刚好够用的位数？

> **参考答案**：(D) = 64 × 30 = 1920 字。RAM 的地址位宽必须取 2 的整数次幂，而 ≥ 1920 的最小 2 的幂是 2048 = 2^11（2^10 = 1024 不够）。所以即便实际只用 1920 字，也得分配 2048 字、地址 11 位，尾部空出 128 字不用。这也是 csr_pkg 里字节地址位宽为 13（= 11 + 2）的由来。

**问题 2**：routing_table 的 (C) 和 (E) 恰好相等（都 256），巧合吗？

> **参考答案**：不是巧合。路由表每条目 4 字、64 条目共 256 字，正好等于 2^8；所以实际用量和分配量一致、没有尾部浪费。密钥表则因为每条目 30 字（非 2 的幂）导致 64×30=1920 落在 1024 与 2048 之间，必须向上取整到 2048。设计时让每条目字数尽量是 2 的幂，可避免这种浪费——但密钥表的字段数由协议（256 位密钥 + 身份信息）决定，无法人为凑整。

> **复核提示**：本实践不依赖运行任何工具，纯源码阅读与算术。若你填出的 (B) 不是 30，请重新数密钥表条目里的 `reg`（注意 send_cnt/recv_cnt 各占 2 个字、加解密密钥各 8 个字）。

## 6. 本讲小结

- SystemRDL 的 `external regfile` 让 PeakRDL 只生成地址译码与 `req/ack` 握手外壳，存储体由设计者自备——本项目用 `tdp_ram` 双口 RAM 填这个空，省面积又支持双方向同时访问。
- `dpe.sv` 实例化两个 `tdp_ram`：`u_routing_table`（`ADDR_WIDTH=8`，256 字）与 `u_cryptokey_table`（`ADDR_WIDTH=11`，2048 字）。A 口一律接 CSR 请求/应答，B 口预留给数据面查找（当前 PoC 阶段未接，配合 `dpe_dummy_switch` 直通）。
- 字节地址到字地址用 `addr >> 2` 转换：路由表 10 位字节地址→8 位字地址，密钥表 13 位→11 位，与各自 `ADDR_WIDTH` 严丝合缝。
- 握手时序不对称：**读**是一拍延迟——`rd_data` 和 `rd_ack` 都在请求的下一拍有效（`rd_ack` 特意做成寄存器以对齐）；**写**是同拍应答——`wr_ack` 用组合逻辑，因为写在该拍上升沿已完成。
- 容量核算：路由表 64×4=256 字 = 1024 字节（无浪费）；密钥表 64×30=1920 字，向上取整到 2048 字 = 8192 字节（尾部浪费 128 字）。
- 当前 HEAD 处于 Phase1 PoC：两条表的 A 口（CPU 配置）已可用，B 口（数据面查表）因加密流水线尚未上线而悬空——骨架就绪，待 Unit 5 的 ChaCha20-Poly1305 与路由查找接入后即可激活。

## 7. 下一步学习建议

- 想看「B 口被数据面真正驱动」的样子，可阅读（当前被注释的）`1.hw/ip.dpe/dpe_egress_ip_lookup.sv` 与 `1.hw/ip.dpe/dpe_route_mem.sv`，那是 TCAM 路由查找如何读这两条表的设计意图，对应 [u4-l4（TCAM 最长前缀路由查找）](u4-l4-tcam-ip-lookup.md)。
- 密钥表的 256 位加解密密钥如何被加解密核使用，留到 Unit 5：建议从 [u5-l1（AEAD/ChaCha20-Poly1305 原理）](u5-l1-aead-chacha-poly-theory.md) 入手。
- 控制面如何经 HAL 写这两条表、并配合 FCR 做原子更新，见 [u6-l4（软件控制流：收发包与表更新）](u6-l4-sw-control-flow.md) 与 [u3-l4（FCR 流控寄存器与原子更新）](u3-l4-fcr-atomic-update.md)。
