# 寄存器映射与 up_axi 微处理器接口

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `up_axi` 这个小模块在「软件寄存器读写」与「IP 内部寄存器」之间扮演的「翻译桥」角色，并解释它为何能做到厂商无关。
- 把一份 `*_regmap.v`（如 `axi_dmac_regmap.v`）里的 `case` 分支，与 `docs/regmap/*.txt` 寄存器表里的地址、位域、读写属性一一对应起来。
- 解释同一份寄存器映射，为什么在 Zynq-7000、ZynqMP、Versal 上会被软件看到成不同的物理地址（架构地址偏移）。

本讲是「IP 库系统」单元的关键一篇：它把前面讲过的「AXI 总线」「IP 打包」与「软件如何控制硬件」这三件事缝合起来。

## 2. 前置知识

阅读本讲前，建议先理解下面几个通俗概念：

- **寄存器（register）**：FPGA 里最简单的「控制开关 / 状态灯」。一个 32 位寄存器就是 32 个比特，软件往某几位写 1/0 来配置硬件，或读某几位来查询硬件状态。
- **内存映射 I/O（memory-mapped I/O）**：CPU 不用专门的「读写外设」指令，而是把每个外设寄存器当成一段普通内存地址。往地址 `0x4000_0000` 写一个字，本质上是写进了某个 IP 的某个寄存器。
- **AXI4-Lite**：ARM 定义的一套轻量总线协议，专门用来做「CPU 读 / 写少量 32 位寄存器」。它有 5 个独立的通道（写地址、写数据、写响应、读地址、读数据），每个通道都用 `valid`/`ready` 握手。ADI 几乎所有 IP 的控制接口都是 AXI4-Lite。
- **字节地址 vs 字地址**：AXI 总线按**字节**编址，每个字节一个地址；而寄存器是 32 位 = 4 字节，因此连续两个寄存器的地址相差 4。本讲会频繁出现「去掉地址最低 2 位」的操作，就是从字节地址换算成「字地址」（4 字节为一个字的编号）。
- **`sys_zynq`**：ADI 块设计脚本里一个标识 PS（处理系统）家族的变量（0 = MicroBlaze/普通 FPGA，1 = Zynq-7000，2 = ZynqMP，3 = Versal）。它在 u3-l4 的 `ad_cpu_interconnect` 里决定地址如何平移。

如果你对 AXI4-Lite 的握手时序完全陌生，记住一句话即可：**主机（CPU）拉高 `valid`，从机（IP）准备好后拉高 `ready`，二者同时为高的那个时钟沿完成一次传输**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [library/common/up_axi.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/up_axi.v) | 全仓公用的 AXI4-Lite → 内部寄存器「翻译桥」，厂商无关 |
| [library/axi_dmac/axi_dmac_regmap.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_regmap.v) | axi_dmac 的寄存器映射实现：例化 `up_axi`，并按地址分派读写 |
| [docs/regmap/adi_regmap_dmac.txt](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/regmap/adi_regmap_dmac.txt) | axi_dmac 的寄存器表「文本源」，文档系统据此渲染成表格 |
| [docs/regmap/adi_regmap_axi_adc_template.txt](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/regmap/adi_regmap_axi_adc_template.txt) | ADC 类 IP 的公共寄存器模板，演示「寄存器名 + 位域名」式描述 |
| [docs/user_guide/architecture.rst](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst) | 官方架构说明，含 CPU/存储互连的架构地址偏移规则 |

---

## 4. 核心概念与源码讲解

本讲的三个最小模块，正好对应「软件一次寄存器读写」经历的三个层次：

1. **桥接层**（`up_axi`）：把 AXI4-Lite 的复杂握手，简化成一对「请求 / 应答」信号。
2. **映射层**（`*_regmap.v` + `docs/regmap/*.txt`）：把「地址」翻译成「具体哪一个寄存器的哪几位」。
3. **地址层**（架构偏移）：同一份映射，因 FPGA 架构不同而出现在不同的 CPU 物理地址。

### 4.1 up_axi 桥接原理

#### 4.1.1 概念说明

`up_axi` 是一个不到 200 行的「翻译桥」。它解决的核心问题是：

> AXI4-Lite 协议虽然标准，但握手时序繁琐（5 个通道、各自 valid/ready、还要回 bresp/rresp）。如果每个 IP 都自己写一遍 AXI 从机，代码会又长又容易出错。

`up_axi` 把这套繁琐的从机逻辑封装成一个**厂商无关**的通用模块，对外暴露一组极简的「pcore（peripheral core）接口」：

- 写：`up_wreq`（请求）+ `up_waddr` + `up_wdata` → `up_wack`（应答）
- 读：`up_rreq`（请求）+ `up_raddr` → `up_rdata` + `up_rack`（应答）

这样每个 IP 的 `*_regmap.v` 只需要关心「收到一个地址，我要把哪个寄存器的值送出去」，完全不用碰 AXI 握手。又因为它放在 `library/common/` 下、纯 RTL，所以 AMD / Intel / Lattice 三家都能用（呼应 u4-l4 讲过的 common 工具池）。

#### 4.1.2 核心流程

`up_axi` 内部把读写分成两条**完全独立**的通道（写通道与读通道互不阻塞）。每条通道的状态机思想一致：

```
收到 AXI 请求(AW+W 或 AR)
   │  锁存地址/数据，拉起 up_wreq/up_rreq
   ▼
等待 pcore 应答(up_wack/up_rack)
   │  同时启动一个看门狗计数器
   ├── 应答先到 → 正常完成，回 bvalid/rvalid
   └── 计数到上限仍未应答 → 强制超时应答（读返回 0xDEADDEAD）
```

读写各有一个 5 位看门狗计数器（`up_wcount` / `up_rcount`）。请求一发出，计数器从 `0x10` 开始往上数；数到 `0x1f`（即经过约 15 个时钟）若 pcore 仍未应答，桥就**强制完成**这次传输。这是一种自我保护：即便某个寄存器地址无人响应，AXI 总线也不会被永久挂死。

#### 4.1.3 源码精读

**模块端口：左侧 AXI4-Lite，右侧 pcore**（[library/common/up_axi.v:38-78](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/up_axi.v#L38-L78)）。

关键参数是 `AXI_ADDRESS_WIDTH`（默认 16）。注意右侧 pcore 地址位宽是 `AXI_ADDRESS_WIDTH-3`：

```verilog
output  [(AXI_ADDRESS_WIDTH-3):0] up_waddr,   // 字地址
output  [(AXI_ADDRESS_WIDTH-3):0] up_raddr,
```

为什么是 `-3`？因为 AXI 地址按字节编址，最低 2 位是「字节内偏移」（32 位寄存器里选哪一个字节）；而 `-1` 是 Verilog 位宽从 0 起算的惯例。所以 `AXI_ADDRESS_WIDTH` 位字节地址，去掉最低 2 位后，得到 `AXI_ADDRESS_WIDTH-2` 位的字地址，写成位宽就是 `[ (AXI_ADDRESS_WIDTH-2)-1 : 0 ] = [AXI_ADDRESS_WIDTH-3 : 0]`。

**字节地址 → 字地址的换算**发生在写通道（[library/common/up_axi.v:161](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/up_axi.v#L161)）：

```verilog
up_waddr_int <= up_axi_awaddr[(AXI_ADDRESS_WIDTH-1):2];  // 丢掉最低 2 位
```

即：

\[ \text{word\_addr} = \left\lfloor \frac{\text{byte\_addr}}{4} \right\rfloor = \text{byte\_addr}[N\!-\!1:2] \]

读通道同理（[library/common/up_axi.v:226](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/up_axi.v#L226)）。

**看门狗与超时**（[library/common/up_axi.v:141](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/up_axi.v#L141) 与 [L204-205](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/up_axi.v#L204-L205)）：

```verilog
// 写：计数到 0x1f 强制 ack，否则等 pcore 的 up_wack
assign up_wack_s = (up_wcount == 5'h1f) ? 1'b1 : (up_wcount[4] & up_wack);
// 读：计数到 0x1f 强制 ack，并返回 0xDEADDEAD
assign up_rack_s = (up_rcount == 5'h1f) ? 1'b1 : (up_rcount[4] & up_rack);
assign up_rdata_s = (up_rcount == 5'h1f) ? {2{16'hdead}} : up_rdata;
```

`{2{16'hdead}}` 就是把 `0xdead` 复制两份拼接，得到 `0xDEADDEAD`。这个魔数是 ADI 驱动排查问题时最常遇到的「信号」：**当你在软件里读到一个寄存器返回 `0xDEADDEAD`，几乎可以断定这个地址在硬件里没有被任何 `case` 分支响应**（要么地址写错，要么这个变体里该寄存器被裁掉了）。

> 小贴士：计数器只在请求发出后从 `0x10` 起跳（[L168-169](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/up_axi.v#L168-L169)），到 `0x1f` 截止，所以正常 pcore 只要在一个时钟内应答（ADI 的 regmap 都是组合 / 单拍应答），就永远不会触发超时。

#### 4.1.4 代码实践

**实践目标**：亲手验证「字节地址 → 字地址」的换算与超时魔数。

**操作步骤（源码阅读型）**：

1. 打开 [library/common/up_axi.v:159-162](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/up_axi.v#L159-L162)，确认 `up_waddr_int` 取的是 `awaddr` 的高位、丢掉最低 2 位。
2. 假设 CPU 要写一个位于字节地址 `0x4410_0400` 的寄存器（低 12 位 `0x400` 是 IP 内偏移），手算：`0x400 >> 2 = 0x100`，即字地址 `0x100`。
3. 打开 [library/common/up_axi.v:205](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/up_axi.v#L205)，把 `{2{16'hdead}}` 展开，确认等于 `32'hdead_dead`。

**需要观察的现象 / 预期结果**：

- 字节地址 `0x400`（最低 2 位为 0）对应字地址 `0x100`；字节地址 `0x404` 对应字地址 `0x101`。
- 若读一个不存在的寄存器，AXI `rdata` 通道最终会出现 `0xDEADDEAD`。

> 待本地验证：若你有仿真环境，可在 testbench 里对 `up_axi` 发一个无人应答的读请求，观察 `up_axi_rdata` 是否在大约 15 个 `up_clk` 周期后变成 `0xDEADDEAD`。

#### 4.1.5 小练习与答案

**练习 1**：`up_axi` 的 `AXI_ADDRESS_WIDTH` 设为 11 时，pcore 侧 `up_waddr` 是几位？最多能寻址多少个 32 位寄存器？

**参考答案**：位宽 = `11-3 = 8`，即 `[8:0]` 共 9 位字地址（注意 `AXI_ADDRESS_WIDTH-3` 是位宽上界索引，`[8:0]` 是 9 位）。最多寻址 \(2^9 = 512\) 个 32 位寄存器，对应字节空间 \(512 \times 4 = 2048\) 字节 = `0x800`。

**练习 2**：为什么读超时返回 `0xDEADDEAD` 而不是 `0x00000000`？

**参考答案**：`0x00000000` 是很多寄存器的合法值（如禁用状态、清零计数器），无法与「真读到 0」区分；`0xDEADDEAD` 是一个几乎不可能作为真实寄存器值的醒目魔数，便于软件和调试者一眼识别「这个地址没人响应」。

---

### 4.2 regmap.v 与寄存器表的对应关系

#### 4.2.1 概念说明

`up_axi` 只解决了「AXI 握手 → 请求/应答」的简化；它不知道某个地址**对应哪个寄存器**。这第二层翻译由每个 IP 自己的 `*_regmap.v` 完成。以 `axi_dmac` 为例：

- [axi_dmac_regmap.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_regmap.v) 例化一个 `up_axi`，把它输出的 `up_raddr`/`up_waddr` 接到一个 `case` 语句；
- 每个 `case` 分支对应一个寄存器，决定读时把什么值送回 `up_rdata`、写时把 `up_wdata` 存到哪个 `reg`；
- 与此同时，[docs/regmap/adi_regmap_dmac.txt](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/regmap/adi_regmap_dmac.txt) 用纯文本描述同一份寄存器（地址、位域、读写属性、说明），文档系统（adi_doctools 的 `hdl-regmap` 指令）据此渲染成人可读的表格。

关键点：**`.v` 里的 `case` 地址 与 `.txt` 里的 `0xXXX` 是同一套「字地址」，一一对应**。这一对应关系是本模块的核心。

#### 4.2.2 核心流程

一次「软件读 axi_dmac 的版本号」的完整旅程：

```
CPU 发起 AXI 读, 字节地址 = IP基地址 + 0x000
        │  (架构偏移在此之后才加, 见 4.3)
        ▼
up_axi 去掉低 2 位 → up_raddr = 9'h000, 拉起 up_rreq
        ▼
axi_dmac_regmap 的读 case 命中 9'h000:
        up_rdata <= PCORE_VERSION;            // 'h00040565
        up_rack 在下一拍置 1
        ▼
up_axi 收到 up_rack → 把 up_rdata 回送到 AXI rdata 通道
        ▼
CPU 读到 0x00040565 → 解析为 主版本4 / 次版本5 / 补丁0x65
```

写流程对称：`up_waddr` 命中某个 `case`，把 `up_wdata` 写进对应的 `reg`（如控制寄存器），并通过 `up_wack` 应答。

#### 4.2.3 源码精读

**例化 up_axi**（[axi_dmac_regmap.v:346-375](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_regmap.v#L346-L375)）：注意 `AXI_ADDRESS_WIDTH` 被设为 11，与上一模块练习一致；左侧 AXI 信号直连 IP 顶层 `s_axi_*`，右侧 pcore 信号连到本文件内部的 `up_wreq/up_raddr/...`。

**版本号寄存器**（[axi_dmac_regmap.v:150](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_regmap.v#L150) 与 [L248](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_regmap.v#L248)）：

```verilog
localparam PCORE_VERSION = 'h00040565;
...
9'h000: up_rdata <= PCORE_VERSION;
```

对照寄存器表 [adi_regmap_dmac.txt:8-31](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/regmap/adi_regmap_dmac.txt#L8-L31)：

```
REG
0x000
VERSION
...
FIELD [31:16] 0x00000004  VERSION_MAJOR  RO
FIELD [15:8]  0x00000005  VERSION_MINOR  RO
FIELD [7:0]   0x00000065  VERSION_PATCH  RO
```

`0x00040565` 正好拆成 `04.05.65`，与文本「Current version 4.05.65」完全吻合——这就是 `.v` 与 `.txt` 同源的最好证据。

**控制寄存器 CONTROL（0x100）** 是读写的，最能体现「位域」概念。写逻辑（[axi_dmac_regmap.v:226-231](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_regmap.v#L226-L231)）：

```verilog
9'h100: begin
  ctrl_flock   <= up_wdata[3] & FRAMELOCK;
  ctrl_hwdesc  <= up_wdata[2] & DMA_SG_TRANSFER;
  ctrl_pause   <= up_wdata[1];
  ctrl_enable  <= up_wdata[0];
end
```

读逻辑（[L263](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_regmap.v#L263)）把这几位重新拼回：

```verilog
9'h100: up_rdata <= {28'b0, ctrl_flock, ctrl_hwdesc, ctrl_pause, ctrl_enable};
```

对照寄存器表 [adi_regmap_dmac.txt:255-295](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/regmap/adi_regmap_dmac.txt#L255-L295)：bit0=`ENABLE`、bit1=`PAUSE`、bit2=`HWDESC`、bit3=`FRAMELOCK`，全部 `RW`。注意 `.v` 里 `ctrl_flock <= up_wdata[3] & FRAMELOCK`——即使软件写 1，若该 IP 综合时没开 `FRAMELOCK` 参数，写进来也被「与」成 0。这种「写权限受参数裁剪」是 ADI regmap 的常见保护手法。

**地址分派：default 兜底**（[axi_dmac_regmap.v:271](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_regmap.v#L271)）：

```verilog
default: up_rdata <= up_rdata_request;
```

axi_dmac 把寄存器分成了两份：控制 / 状态 / 配置类（如版本、中断、CONTROL）留在本文件；与「传输编程」相关的（地址、长度、stride、submit）委派给子模块 `i_regmap_request`（[L276-344](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_regmap.v#L276-L344)）。本文件没列出的地址，读时由 `default` 转发到子模块的 `up_rdata_request`。这是一种「按地址段划分所有权」的可扩展写法——寄存器再多也不用把一个 `case` 撑到无限长。

> 文档侧的对应：在 [docs/library/axi_dmac/index.rst:214-215](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/library/axi_dmac/index.rst#L214-L215) 用 `.. hdl-regmap:: :name: DMAC` 把 `adi_regmap_dmac.txt`（其 TITLE 简称 `DMAC`）渲染成该 IP 页面的寄存器表。`name` 必须匹配 `.txt` 里的简称，这正是 u2-l3 讲过的「文本源与渲染分离」机制。

#### 4.2.4 代码实践

**实践目标**：把 `.v` 的 `case` 与 `.txt` 的寄存器表对齐，理解三个真实寄存器的地址、位域与读写语义。

**操作步骤**：

1. 打开 [axi_dmac_regmap.v:245-274](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_regmap.v#L245-L274) 的读 `case`，挑三个寄存器：`9'h000`(VERSION)、`9'h022`(IRQ_SOURCE)、`9'h100`(CONTROL)。
2. 在 [adi_regmap_dmac.txt](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/regmap/adi_regmap_dmac.txt) 里找到对应 `0x000` / `0x022` / `0x100` 的条目。
3. 对照填写下表（示例答案见「预期结果」）。

**需要观察的现象 / 预期结果**（参考答案）：

| 字地址 | 寄存器名 | 关键位域 | 属性 | 软件读写语义 |
| --- | --- | --- | --- | --- |
| `0x000` | VERSION | `[31:16]`MAJOR=4 / `[15:8]`MINOR=5 / `[7:0]`PATCH=0x65 | RO | 读出 IP 版本，用于驱动匹配；写无效 |
| `0x022` | IRQ_SOURCE | `[1]`TRANSFER_COMPLETED / `[0]`TRANSFER_QUEUED | RO（随 `0x021` 的 RW1C 清除） | 查询原始中断源；写 `0x021` 对应位清中断 |
| `0x100` | CONTROL | `[3]`FRAMELOCK / `[2]`HWDESC / `[1]`PAUSE / `[0]`ENABLE | RW | bit0 置 1 启动 DMA；bit1 暂停；高 2 位受参数裁剪 |

> 额外发现：`0x021`(IRQ_PENDING) 是 `RW1C`（写 1 清除），对应 `.v` 里 [L186](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_regmap.v#L186) 的 `up_irq_source_clear = up_wdata[1:0]`——软件往 `0x021` 写哪一位为 1，就清掉哪一位中断源。这正是「写 1 清（write-1-to-clear）」在 RTL 里的实现。

#### 4.2.5 小练习与答案

**练习 1**：寄存器表里 `INTERFACE_DESCRIPTION_1 (0x004)` 标注全是 `R`（只读），它描述的是什么？软件为何只能读？

**参考答案**：它把 IP 综合时的硬件参数（如 `BYTES_PER_BEAT_DEST_LOG2`、`DMA_TYPE_DEST/SRC`、`BYTES_PER_BURST_WIDTH`）汇报给软件（见 [adi_regmap_dmac.txt:82-149](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/regmap/adi_regmap_dmac.txt#L82-L149)）。这些是综合期就定死的配置，运行时无法改，所以只读；软件靠它知道「这个 DMA 的源 / 目的数据位宽是多少」，从而正确计算传输长度与地址对齐。

**练习 2**：如果一个寄存器在 `.txt` 里有，但在 `.v` 的读 `case` 里既没有显式分支、也没被 `default` 兜到子模块，软件读它会得到什么？

**参考答案**：`up_rdata` 保持默认（本文件里 `up_rdata` 初值为 `32'h00`），同时由于 regmap 仍会回 `up_rack`，`up_axi` 不会超时，软件读到的是 `0x00000000`。只有当**连 `up_rack` 都没人给**时才会触发 4.1 讲的 `0xDEADDEAD`。

---

### 4.3 CPU 地址的架构偏移

#### 4.3.1 概念说明

前两模块讲的都是「IP 内部」的地址（从 `0x000` 开始的相对偏移）。但软件真正发出的 CPU 物理地址，还要叠加两层：

1. **IP 基地址**：块设计里 `ad_cpu_interconnect` 给每个 IP 分配的起始地址（呼应 u3-l4）。
2. **架构偏移**：同一个块设计地址，在不同 FPGA 架构（Zynq-7000 / ZynqMP / Versal）上，会被 PS 映射到不同的物理地址段。

第 2 层是本模块重点。它的存在是因为：Zynq-7000 是 ADI 最早支持的 target，它的地址空间被当作「参考地址」；后续架构的 PS 地址映射表不同，于是 `ad_cpu_interconnect`（[projects/scripts/adi_board.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl)）会根据 `sys_zynq` 给地址**加上一个架构相关的常数**，把外设搬到该架构实际可用的地址段。

#### 4.3.2 核心流程

软件地址的最终构成：

\[ \text{CPU\_ADDR} = \underbrace{\text{ARCH\_OFFSET}}_{\text{架构偏移}} + \underbrace{\text{IP\_BASE}}_{\text{块设计里的基址}} + \underbrace{\text{REG\_OFFSET}}_{\text{寄存器字地址}\times 4} \]

其中 `REG_OFFSET` 由本 IP 的 regmap 决定（4.2），`IP_BASE` 由块设计连线决定（u3-l4），`ARCH_OFFSET` 由下表决定。三者相加才是 no-OS / Linux 驱动里写的那个物理地址。

#### 4.3.3 源码精读

官方规则在 [architecture.rst:160-205](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst#L160-L205)（标题 *CPU/Memory interconnects addresses*）。摘录要点：

> The memory addresses that will be used by software are based on the HDL addresses of the IP register map, to which an offset is added, depending on the architecture of the used FPGA ... architecture is specified by `sys_zynq` variable.

具体偏移（来自同一节）：

| 架构 | 块设计地址段 | 偏移量 | 软件看到的地址段 |
| --- | --- | --- | --- |
| Zynq-7000 / 7 Series | （参考地址） | 无（基准） | 与块设计一致 |
| ZynqMP (PS8) | `0x4000_0000 – 0x4FFF_FFFF` | `+0x4000_0000` | `0x8000_0000 – 0x8FFF_FFFF` |
| ZynqMP (PS8) | `0x7000_0000 – 0x7FFF_FFFF` | `+0x2000_0000` | `0x9000_0000 – 0x9FFF_FFFF` |
| Versal | `0x4400_0000 – 0x4FFF_FFFF` | `+0x6000_0000` | `0xA400_0000 – 0xAFFF_FFFF` |
| Versal | `0x7000_0000 – 0x7FFF_FFFF` | `+0x4000_0000` | `0xB000_0000 – 0xBFFF_FFFF` |

来源：[architecture.rst:171-196](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst#L171-L196)。

**这为什么重要？** 因为 ADI 的评估板基设计（第二层，u2-l1）是「载板无关」的——同一份 `fmcomms2_bd.tcl` 里，`ad_cpu_interconnect` 给 `axi_ad9361` 分配的块设计地址（比如 `0x4000_0000` 起的某段）是固定的；但这份设计被 source 到 Zynq-7000 载板和 ZynqMP 载板时，软件看到的物理地址会不同。`ad_cpu_interconnect` 内部按 `sys_zynq` 自动加上表中的偏移，从而**让评估板层脚本无需为每种载板改地址**。这正是三层架构（u2-l1）能实现 N+M 维护量的底层支撑之一。

> Intel 平台没有这么整齐的公式：[architecture.rst:198-204](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst#L198-L204) 指出 Intel（DE10-Nano / C5SoC）地址「通常（但不总是）从 `0x0002_0000` 起」，需要在 Quartus 块设计里具体确认。

#### 4.3.4 代码实践

**实践目标**：用架构偏移公式，算出同一个寄存器在 Zynq-7000 与 ZynqMP 上的不同软件地址。

**操作步骤**：

1. 假设块设计里某 IP 被分到 `IP_BASE = 0x4000_0000`，要读它的 VERSION 寄存器（字地址 `0x000`，即 `REG_OFFSET = 0x000`）。
2. 对 Zynq-7000：`CPU_ADDR = 0 + 0x4000_0000 + 0x000 = 0x4000_0000`。
3. 对 ZynqMP：该地址段落在 `0x4000_0000–0x4FFF_FFFF`，查表得偏移 `+0x4000_0000`，故 `CPU_ADDR = 0x4000_0000 + 0x4000_0000 = 0x8000_0000`。

**需要观察的现象 / 预期结果**：

- 同一个 VERSION 寄存器，Zynq-7000 上软件读 `0x4000_0000`，ZynqMP 上读 `0x8000_0000`，读到的值都是 `0x00040565`（如果是 axi_dmac 的话）。
- 这解释了为什么 ADI 的 no-OS / Linux 驱动里，同一个 IP 的基地址会随平台不同——不是 IP 变了，而是 PS 的地址窗口变了。

> 待本地验证：若你手头有同一块 ADI 评估板分别跑在 ZedBoard（Zynq-7000）与 ZCU102（ZynqMP）上的工程，可对比两个 `system_bd.tcl` 里 `ad_cpu_interconnect` 的调用参数与生成的地址映射（在 Vivado Address Editor 里），确认上述偏移。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Zynq-7000 没有「偏移」，而 ZynqMP / Versal 都要加一个正偏移？

**参考答案**：Zynq-7000 是 ADI 最早支持的 target，其块设计地址被直接当作「参考地址」原样暴露给软件，故偏移为 0。ZynqMP / Versal 的 PS 把 PL（FPGA）外设映射到了与 Zynq-7000 不同的物理窗口，为了让同一份载板无关的块设计脚本不动，就在 `ad_cpu_interconnect` 里按 `sys_zynq` 自动加上一个常数，把外设「搬」到该架构实际可用的窗口。

**练习 2**：软件读到一个寄存器返回 `0xDEADDEAD`，与「架构偏移算错」导致的读不到，现象上有什么区别？

**参考答案**：`0xDEADDEAD` 是 `up_axi` 在 **IP 内部**（地址已正确路由到该 IP）但无 `case` 应答时返回的（4.1）。而架构偏移算错通常意味着 AXI 事务根本没送到该 IP（地址落在空地址段），多数 PS 会回总线错误（SLVERR / DECERR，即 `bresp/rresp != 0`）或直接异常，而不是一个干净的 `0xDEADDEAD` 数据。所以看到 `0xDEADDEAD` 反而说明「地址路由对了，只是这个寄存器没人实现」。

---

## 5. 综合实践

把三个模块串起来，完成一次「从软件地址到寄存器位域」的完整追踪。

**任务**：假设在 ZCU102（ZynqMP）上有一个 axi_dmac，块设计里给它分配的基址为 `0x4000_0000`（落在 `0x4000_0000–0x4FFF_FFFF` 段）。请回答：

1. 软件要**启动一次 DMA**，应该往哪个 CPU 物理地址写什么值？
2. 软件想**确认这次启动是否产生了「传输已排队」中断**，应读哪个地址、看哪一位？中断如何清除？
3. 如果误把地址算成了 `0x4000_0000`（漏了架构偏移），最可能观察到什么现象？

**参考思路**：

1. 启动 DMA = 写 CONTROL 寄存器的 ENABLE 位。CONTROL 字地址 `0x100`，`REG_OFFSET = 0x100 × 4 = 0x400`。ZynqMP 段偏移 `+0x4000_0000`，故 `CPU_ADDR = 0x4000_0000 + 0x4000_0000 + 0x400 = 0x8000_0400`，写入 `0x1`（bit0=ENABLE）。对应 [axi_dmac_regmap.v:230](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_regmap.v#L230) 的 `ctrl_enable <= up_wdata[0]`。
2. 「传输已排队」= IRQ_SOURCE 的 bit0（TRANSFER_QUEUED），字地址 `0x022`，`REG_OFFSET = 0x88`，读地址 `0x8000_0088`，看 bit0。但更常用的是读 IRQ_PENDING(`0x021`，`0x84`)；清除时往 `0x8000_0084` 的对应位写 1（RW1C），对应 [axi_dmac_regmap.v:186](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_regmap.v#L186)。
3. 漏掉架构偏移会把事务发到 `0x4000_0400`——这是块设计里的「逻辑地址」，未经 PS 地址翻译，通常会触发总线错误（SLVERR/DECERR）或读到全 0/随机值，而**不会**是干净的 `0xDEADDEAD`（因为根本没路由到 IP）。

> 提示：本练习无需上板，全部可在源码与文档里完成；若要验证，需在 Vivado 的 Address Editor 与 no-OS / Linux 的设备树 / 寄存器工具里对照确认。

## 6. 本讲小结

- `up_axi`（[library/common/up_axi.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/up_axi.v)）是厂商无关的 AXI4-Lite → 寄存器「翻译桥」，把繁琐的 5 通道握手简化成 `up_wreq/up_wack`、`up_rreq/up_rack` 两对请求/应答；它去掉字节地址最低 2 位得到字地址，并自带看门狗，读超时返回 `0xDEADDEAD`。
- 每个 IP 用 `*_regmap.v`（如 [axi_dmac_regmap.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_regmap.v)）按字地址 `case` 分派读写；同一套地址在 [docs/regmap/*.txt](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/regmap/adi_regmap_dmac.txt) 里用人话描述，二者一一对应（如 `9'h000` ↔ `0x000` VERSION = `0x00040565`）。
- `.txt` 用 TITLE/REG/FIELD 块描述「地址 + 位域 + 读写属性 + 说明」，由 adi_doctools 的 `hdl-regmap` 指令按 `:name:` 简称渲染成文档表格——这是「源与渲染分离」。
- 位域语义直接体现在 RTL：`RW1C`（写 1 清）对应 `up_irq_source_clear = up_wdata[...]`；`R`（只读配置）对应综合期参数拼接汇报（如 `INTERFACE_DESCRIPTION_1`）。
- CPU 物理地址 = 架构偏移 + IP 基地址 + 寄存器偏移；架构偏移由 `sys_zynq` 在 `ad_cpu_interconnect` 里决定（[architecture.rst:160-205](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst#L160-L205)），Zynq-7000 为基准、ZynqMP/Versal 各加不同常数，使同一份评估板层脚本可跨载板复用。
- 诊断口诀：读到 `0xDEADDEAD` = 地址路由到了 IP 但该寄存器无人实现；总线错误 = 地址根本没路由对（常见于架构偏移算错）。

## 7. 下一步学习建议

- **向深**：进入 u5-l1（axi_dmac 深入），看 `i_regmap_request`（本讲 4.2 的 `default` 兜底去向）如何把「寄存器写」变成一次真实的 DMA 传输请求——这是「控制平面 → 数据平面」的衔接点。
- **向广**：用本讲的方法，自选另一个 IP（如 [library/axi_ad9361/](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/) 或 JESD204 相关 IP），对照它的 `*_regmap.v` 与 `docs/regmap/*.txt`，练习「地址—位域—语义」三栏对齐。
- **向应用**：阅读 u8-l1（仿真与测试平台），看 `regmap_tb` 如何用 AXI slave 模型驱动 `up_axi` 验证寄存器行为——那是对本讲时序（握手、看门狗、`0xDEADDEAD`）最直接的实验场。
- **跨平台**：结合 u3-l4（`ad_cpu_interconnect`）与 u7-l3（多厂商构建），对比 AMD / Intel 在「寄存器地址映射」上的差异，理解为何 Intel 没有整齐的架构偏移公式。
