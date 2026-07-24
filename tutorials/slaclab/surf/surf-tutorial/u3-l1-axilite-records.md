# AXI-Lite 记录类型与包（AxiLitePkg）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 SURF 把一条 AXI-Lite 总线「折叠」成哪四个记录（`AxiLiteReadMasterType` / `AxiLiteReadSlaveType` / `AxiLiteWriteMasterType` / `AxiLiteWriteSlaveType`），以及每个记录里分别装了哪些 AXI 通道信号。
- 看懂每个记录对应的 `_INIT_C` 初值常量，并能解释为什么主机（Master）的初值把 `rready/bready` 设成 `'1'`、`wstrb` 设成全 `'1'`，而从机（Slave）的初值几乎全是 `'0'`。
- 记住 AXI-Lite 的四个响应码 `AXI_RESP_OK_C / EXOKAY / SLVERR / DECERR` 的取值与含义，并知道 `SLVERR` 会在哪些「不支持的访问」时被返回。
- 理解 SURF 为什么在内部代码里用「记录」而不是扁平端口，以及它如何在 Vivado IP integrator 边界用一个薄薄的「扁平器（flattener）」把记录拆回标准 `S_AXI_*` 端口。

## 2. 前置知识

本讲默认你已经掌握 u1-l4 的内容，尤其是：

- `sl` / `slv` 是 `std_logic` / `std_logic_vector` 的短别名，来自地基包 `StdRtlPkg`。
- `Type` 后缀表示一个 VHDL 记录（record），`_INIT_C` 后缀表示该记录的初值常量，`Array` 后缀表示某记录的数组类型。
- 模块复位/时序三件套泛型 `TPD_G` / `RST_POLARITY_G` / `RST_ASYNC_G`。

另外补充一点协议背景，方便后面读代码：

- **AXI 总线用 VALID/READY 握手**。一条通道上，生产方把 `xValid` 拉高表示数据有效，消费方把 `xReady` 拉高表示愿意收。只有当某一拍两者同时为 `'1'`，这一笔数据才算「成交」。
- **AXI-Lite 是 AXI 的精简子集**：地址固定 32 位、数据固定 32 位、**不支持突发（burst）**（每次只能搬一个字）、没有「独占访问」。所以 AXI-Lite 的每条流都很「瘦」，适合做寄存器映射。
- 一条完整的 AXI 总线有 **5 个通道**：读地址（AR）、读数据（R）、写地址（AW）、写数据（W）、写响应（B）。本讲的关键就是把这 5 个通道按「读/写」和「主/从」切成 4 个记录。

> 提示：如果你还不熟悉 VALID/READY 握手，记住一句口诀即可——**「有效遇上准备好，这一拍就成交」**。后面所有 `_INIT_C` 的默认值，都是围绕这句口诀设计的「安全静止态」。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [axi/axi-lite/rtl/AxiLitePkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd) | **本讲主角**。集中定义 AXI-Lite 的四个记录、初值常量、响应码，以及一整套从机地址解码辅助过程。 |
| [axi/axi-lite/rtl/AxiVersion.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd) | 真实使用范例：一个标准的 AXI-Lite 从机如何用四个记录声明端口。 |
| [axi/axi-lite/ip_integrator/SlaveAxiLiteIpIntegrator.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/ip_integrator/SlaveAxiLiteIpIntegrator.vhd) | **扁平器（flattener）**。把记录拆成 Vivado IP integrator 认识的 `S_AXI_*` 扁平端口，是「记录 vs 扁平端口」之争的答案所在。 |

本讲只聚焦这三个文件里的「数据定义」部分（记录、初值、响应码）。`AxiLitePkg.vhd` 后半段的 `axiSlaveRegister` 等地址解码辅助过程属于 u3-l2 的内容，本讲只作预告，不展开。

---

## 4. 核心概念与源码讲解

### 4.1 AXI-Lite 记录：把 5 个通道折叠成 4 个信号

#### 4.1.1 概念说明

AXI-Lite 总线虽然「瘦」，但一条完整的接口仍有约 20 多根线。如果每个 AXI-Lite 模块都把这些线一根根写进端口表，模块之间的连接会变成灾难：端口又长又重复，还容易接错。

VHDL 的 `record`（记录）正好解决这个问题：**把属于同一角色的若干信号打包成一个整体，像一条「粗线」一样整体连接**。SURF 把一条 AXI-Lite 接口按两个维度切成 4 个记录：

- **维度一：方向**——读（Read）还是写（Write）。
- **维度二：角色**——主机（Master，发起事务的一方）还是从机（Slave，响应事务的一方）。

于是得到 2×2 = 4 个记录：

| 记录 | 由谁驱动 | 装了哪些通道 |
|------|----------|------------|
| `AxiLiteReadMasterType` | 读主机驱动 | 读地址通道 AR（主机侧）+ 读数据通道 R 的 ready |
| `AxiLiteReadSlaveType` | 读从机驱动 | 读地址通道 AR 的 ready + 读数据通道 R（从机侧） |
| `AxiLiteWriteMasterType` | 写主机驱动 | 写地址 AW + 写数据 W（主机侧）+ 写响应 B 的 ready |
| `AxiLiteWriteSlaveType` | 写从机驱动 | 写地址 AW 的 ready + 写数据 W 的 ready + 写响应 B（从机侧） |

记忆窍门：**VALID 和它带着的数据归「生产方」那一侧的记录；READY 归「消费方」那一侧的记录**。例如读数据通道 R，数据（`rdata`）和有效（`rvalid`）由从机生产，所以装在 `AxiLiteReadSlaveType` 里；而 `rready`（主机愿不愿意收）由主机生产，所以装在 `AxiLiteReadMasterType` 里。

#### 4.1.2 核心流程

一次读事务在「记录」视角下的流转：

```
读主机(AxiLiteReadMasterType)        读从机(AxiLiteReadSlaveType)
  araddr/arprot/arvalid ─────────────► (解码地址)
                                      ◄───────────── arready   (AR 通道成交)
                                      ◄──── rdata/rresp/rvalid (R 通道：从机回数据)
  rready ───────────────────────────► (主机表示收下)
```

一次写事务类似，但要经过三个通道：

```
写主机(AxiLiteWriteMasterType)        写从机(AxiLiteWriteSlaveType)
  awaddr/awprot/awvalid ─────────────► ◄── awready   (AW 通道成交)
  wdata/wstrb/wvalid   ───────────────► ◄── wready    (W 通道成交)
                                       ──► bresp/bvalid (B 通道：从机回响应)
  bready ◄────────────────────────────  (主机表示收下响应)
```

注意写事务多一个 **B（写响应）通道**：从机收到地址和数据后，必须通过 B 通道告诉主机「这笔写成功了没有」（用 `bresp` 响应码）。读事务则把响应码合并在 R 通道的 `rresp` 里一起返回。

#### 4.1.3 源码精读

先看读主机记录，注释里清楚地标出了它属于哪个通道：

[axi/axi-lite/rtl/AxiLitePkg.vhd:L56-L74](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L56-L74) —— 读主机记录定义：`araddr`(32 位地址)、`arprot`(3 位保护位)、`arvalid`(AR 有效) 归读地址通道；`rready`(主机愿收) 归读数据通道。同一段还顺带声明了 `AxiLiteReadMasterArray`，用于「一个模块挂多条 AXI-Lite」的场合。

再看读从机记录，正好补齐读主机没有的那一半：

[axi/axi-lite/rtl/AxiLitePkg.vhd:L82-L97](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L82-L97) —— 读从机记录：`arready`(收下地址)、`rdata`(32 位返回数据)、`rresp`(2 位响应码)、`rvalid`(数据有效)。读主机 + 读从机合起来，就覆盖了 AXI-Lite 的两个读通道 AR 与 R。

写主机记录多一个 W 数据通道和一个 B 响应通道的 ready：

[axi/axi-lite/rtl/AxiLitePkg.vhd:L117-L128](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L117-L128) —— 写主机记录：AW 通道的 `awaddr/awprot/awvalid`、W 通道的 `wdata/wstrb/wvalid`，以及 B 通道的 `bready`。注意 `wstrb` 是 4 位——32 位数据每字节一个写使能位，决定哪几个字节真正被写入。

写从机记录则补上 AW/W 的 ready 和 B 通道的响应：

[axi/axi-lite/rtl/AxiLitePkg.vhd:L150-L158](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L150-L158) —— 写从机记录：`awready`、`wready`、`bresp`(2 位响应码)、`bvalid`。

真实模块怎么用这四个记录声明端口？看 `AxiVersion`（一个标准的 AXI-Lite 从机）：

[axi/axi-lite/rtl/AxiVersion.vhd:L43-L50](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L43-L50) —— 一个从机的完整 AXI-Lite 端口只有 4 条「粗线」：`axiReadMaster/axiReadSlave`（in/out）+ `axiWriteMaster/axiWriteSlave`（in/out），外加 `axiClk/axiRst`。如果用扁平端口写，这里会是一长串 `S_AXI_ARADDR / S_AXI_ARVALID / ...` 共 20 多根线——记录让端口表干净了一个数量级。

#### 4.1.4 代码实践

**实践目标**：确认四个记录的字段构成，理解「VALID/数据归生产方、READY 归消费方」的切分规则。

**操作步骤**：

1. 打开 `AxiLitePkg.vhd`，分别定位 `AxiLiteReadMasterType`、`AxiLiteReadSlaveType`、`AxiLiteWriteMasterType`、`AxiLiteWriteSlaveType` 四个 `type ... is record`。
2. 画一张 2×2 表格（行=读/写，列=主机/从机），把每个字段填进对应格子。
3. 检查你的表格：读路径上，`arvalid` 应在「读主机」格，`arready` 应在「读从机」格；`rvalid/rdata` 在「读从机」格，`rready` 在「读主机」格。

**需要观察的现象**：填完后你会发现，**同一个通道的 valid 与 ready 永远不在同一个记录里**——它们必然分属主、从两侧。这是 AXI 把「生产」与「消费」职责硬性分开的结果。

**预期结果**：四个记录的并集恰好覆盖 AXI-Lite 全部 5 个通道（AR/R/AW/W/B），且无遗漏、无重复字段。

> 是否运行：这是源码阅读型实践，不需要运行仿真。

#### 4.1.5 小练习与答案

**练习 1**：`rready` 为什么不在 `AxiLiteReadSlaveType` 里，而在 `AxiLiteReadMasterType` 里？

> **答案**：`rready` 表示「读主机是否愿意接收读数据」，是由主机生产的信号；而记录是按「谁驱动这个信号」来归类的。读数据由从机生产（`rdata/rvalid` 在从机记录里），但「愿不愿意收」这件事的主语是主机，所以 `rready` 归主机记录。

**练习 2**：写路径比读路径多一个 B（写响应）通道。请说出读路径为什么不需要单独的响应通道。

> **答案**：读事务中，数据本身就由从机经 R 通道返回，响应码 `rresp` 可以「搭便车」和 `rdata/rvalid` 一起送回。而写事务的数据是从主机流向从机（W 通道），从机需要一个独立的方向（从机→主机）来回送「写成功了吗」，于是必须有专门的 B 通道承载 `bresp/bvalid`，主机再用 `bready` 表示收下。

---

### 4.2 INIT_C 初值：让每条记录都有一个「安全静止态」

#### 4.2.1 概念说明

记录定义了「形状」，但每个信号通电后是什么值？VHDL 允许给记录定义一个初值常量。SURF 的约定是每个 `Type` 都配一个 `<NAME>_INIT_C` 常量，本讲关心这四个：

- `AXI_LITE_READ_MASTER_INIT_C`
- `AXI_LITE_READ_SLAVE_INIT_C`
- `AXI_LITE_WRITE_MASTER_INIT_C`
- `AXI_LITE_WRITE_SLAVE_INIT_C`

初值不是随便填的，而是精心设计的**「安全静止态」**——让总线在没有任何事务时既不丢东西、也不瞎响应：

- **从机初值几乎全 `'0'`**：`arready/awready/wready`、`rvalid/bvalid` 都是 `'0'`，意思是「我不准备好接收」「我没有有效数据/响应」——总线安静地空转。
- **主机初值把「被动接收」的 ready 默认拉高**：`rready => '1'`、`bready => '1'`，意思是「只要从机给我数据/响应，我随时能收」，避免主机成为吞吐瓶颈；同时 `wstrb => (others => '1')` 表示「写整字（4 字节全写）」。主动发起的 `*valid` 全为 `'0'`，所以不会误发事务。

#### 4.2.2 核心流程

判断一个初值是否「安全」的口诀：

1. 我（这一侧）**主动**驱动 `valid` 的信号 → 初值给 `'0'`（默认不发）。
2. 我（这一侧）**被动**驱动 `ready` 的信号 → 视情况给 `'1'`（默认愿意收，主机侧尤其如此）。
3. 地址/数据/保护位等「载荷」→ 初值给全 `'0'` 或全 `'1'`（如 `wstrb`）即可，反正 `valid='0'` 时它们无意义。

对照表：

| 记录 | 关键初值选择 | 含义 |
|------|------------|------|
| 读主机 | `rready => '1'`，`arvalid => '0'` | 随时愿收数据，但默认不发读请求 |
| 读从机 | `arready/rvalid => '0'` | 默认不收地址、无数据返回 |
| 写主机 | `bready => '1'`，`wstrb => (others => '1')`，`awvalid/wvalid => '0'` | 随时愿收响应，默认整字写，但默认不发写 |
| 写从机 | `awready/wready/bvalid => '0'` | 默认不收、无响应 |

#### 4.2.3 源码精读

读主机初值——注意 `rready => '1'`：

[axi/axi-lite/rtl/AxiLitePkg.vhd:L66-L71](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L66-L71) —— `AXI_LITE_READ_MASTER_INIT_C`：地址/保护位全 0，`arvalid='0'`（默认不发读请求），唯独 `rready='1'`（默认愿意收数据）。

读从机初值——清一色 `'0'`：

[axi/axi-lite/rtl/AxiLitePkg.vhd:L92-L97](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L92-L97) —— `AXI_LITE_READ_SLAVE_INIT_C`：`arready='0'`、`rvalid='0'`，从机默认「既不接地址、也不给数据」。

写主机初值——`bready='1'`、`wstrb` 全 `'1'`：

[axi/axi-lite/rtl/AxiLitePkg.vhd:L131-L139](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L131-L139) —— `AXI_LITE_WRITE_MASTER_INIT_C`：`awvalid/wvalid='0'`（默认不发写），但 `bready='1'`（愿收响应）、`wstrb=(others=>'1')`（整字写）。

写从机初值——同样清一色 `'0'`：

[axi/axi-lite/rtl/AxiLitePkg.vhd:L161-L166](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L161-L166) —— `AXI_LITE_WRITE_SLAVE_INIT_C`：`awready/wready/bvalid` 全为 `'0'`。

包里还提供了「带特定响应码的空响应」初值，用一个工厂函数按需生成 `EMPTY_OK/SLVERR/DECERR` 三种，例如：

[axi/axi-lite/rtl/AxiLitePkg.vhd:L99-L106](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L99-L106) —— `axiLiteReadSlaveEmptyInit` 函数与 `AXI_LITE_READ_SLAVE_EMPTY_OK_C` 等常量：一次性把 `arready/rvalid='1'` 并填入指定 `rresp`，方便那些「收到任何访问都立刻回一个固定响应」的占位从机。

实际模块里如何用这些初值？看扁平器内部声明的四条信号——直接拿 `_INIT_C` 当初值：

[axi/axi-lite/ip_integrator/SlaveAxiLiteIpIntegrator.vhd:L110-L113](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/ip_integrator/SlaveAxiLiteIpIntegrator.vhd#L110-L113) —— 四条内部信号 `S_AXI_ReadMaster/ReadSlave/WriteMaster/WriteSlave` 全部用对应的 `_INIT_C` 初始化，这是 SURF 里最常见的写法：声明记录信号时顺手带上初值常量。

#### 4.2.4 代码实践

**实践目标**：定位四个 `_INIT_C` 常量，并模仿真实代码写出一个最小从机的「内部信号声明 + 端口声明」。

**操作步骤**：

1. 在 `AxiLitePkg.vhd` 里搜索 `AXI_LITE_READ_MASTER_INIT_C`、`AXI_LITE_READ_SLAVE_INIT_C`、`AXI_LITE_WRITE_MASTER_INIT_C`、`AXI_LITE_WRITE_SLAVE_INIT_C`，确认它们的行号与取值。
2. 仿照 `AxiVersion` 的端口风格，写一个最小从机的实体骨架（**示例代码**，非项目原有文件）：

```vhdl
-- 示例代码：最小 AXI-Lite 从机骨架（只演示端口与内部信号声明）
library ieee;
use ieee.std_logic_1164.all;

library surf;
use surf.StdRtlPkg.all;
use surf.AxiLitePkg.all;

entity TinyAxiLiteSlave is
   port (
      axiClk         : in  sl;
      axiRst         : in  sl;
      axiReadMaster  : in  AxiLiteReadMasterType;
      axiReadSlave   : out AxiLiteReadSlaveType;
      axiWriteMaster : in  AxiLiteWriteMasterType;
      axiWriteSlave  : out AxiLiteWriteSlaveType);
end entity TinyAxiLiteSlave;

architecture rtl of TinyAxiLiteSlave is
   -- 用 _INIT_C 给内部信号一个安全静止态（仿照 SlaveAxiLiteIpIntegrator 的写法）
   signal rReadSlave   : AxiLiteReadSlaveType  := AXI_LITE_READ_SLAVE_INIT_C;
   signal rWriteSlave  : AxiLiteWriteSlaveType := AXI_LITE_WRITE_SLAVE_INIT_C;
begin
   axiReadSlave  <= rReadSlave;
   axiWriteSlave <= rWriteSlave;
   -- 真正的地址解码逻辑会在 u3-l2 用 axiSlaveWaitTxn/axiSlaveRegister 补上
end architecture rtl;
```

**需要观察的现象**：声明内部从机信号时若**漏掉** `:= AXI_LITE_READ_SLAVE_INIT_C`，信号上电初值会变成 `'U'`（未初始化），可能导致仿真刚开始就出现非法的 `xvalid='U'`，进而引发 X 传播。

**预期结果**：带上 `_INIT_C` 后，上电瞬间 `rReadSlave.rvalid='0'`、`rWriteSlave.bvalid='0'`，总线处于干净的静止态，等待主机发起事务。

> 是否运行：本步是代码编写型实践。若要验证，可在 u3-l2 学完地址解码后，把这个骨架接入一个 cocotb 测试台跑通（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `AXI_LITE_WRITE_MASTER_INIT_C` 把 `wstrb` 设成 `(others => '1')` 而不是全 `'0'`？

> **答案**：`wstrb` 是字节写使能，某位为 `'1'` 表示对应字节要写入。默认全 `'1'` 意味着「整字写（4 字节都写）」，这是最常用、最不易出错的默认行为；若默认全 `'0'`，则任何未显式设置 `wstrb` 的写事务都会「什么都不写」，容易隐藏 bug。

**练习 2**：如果一个从机模块的内部信号忘了用 `_INIT_C` 初始化，仿真第一拍最可能看到什么异常？

> **答案**：记录信号上电为 `'U'`（未初始化）。于是 `rvalid`/`bvalid` 等可能是 `'U'`，主机侧的 ready 与这些 `'U'` 做 AND 判断时会得到 `'U'`，造成 X 传播，整条总线的握手逻辑都进入未知态，难以定位。

---

### 4.3 响应码：2 比特回答「这笔访问成功了吗」

#### 4.3.1 概念说明

每一笔 AXI 事务结束，从机都要回一个 2 比特的响应码，告诉主机结果。这 2 比特装在读从机记录的 `rresp` 和写从机记录的 `bresp` 字段里。AXI 定义了四种响应：

| 常量 | 值 | 含义 |
|------|----|----|
| `AXI_RESP_OK_C` | `"00"` | 访问成功（OKAY） |
| `AXI_RESP_EXOKAY_C` | `"01"` | 独占访问成功（EXOKAY） |
| `AXI_RESP_SLVERR_C` | `"10"` | 从机错误（Slave Error） |
| `AXI_RESP_DECERR_C` | `"11"` | 解码错误（Decode Error） |

对 AXI-**Lite** 而言有两个要点：

1. **没有独占访问**，所以 `AXI_RESP_EXOKAY_C` 只是占位常量，实际用不到（源码注释明确说明了这一点）。
2. 真正常用的是 `OK`、`SLVERR`、`DECERR` 三种：`OK` 表示正常；`SLVERR` 表示从机虽然认得这个地址、但不支持这种访问方式；`DECERR` 表示地址根本没匹配到任何从机（典型场景是交叉开关/地址解码时，主机访问了一段「没人管」的地址）。

#### 4.3.2 核心流程

响应码的产生与消费链路：

```
从机内部逻辑
   ├─ 地址命中且访问合法     → bresp/rresp = AXI_RESP_OK_C
   ├─ 地址命中但访问不合法   → bresp/rresp = AXI_RESP_SLVERR_C   (如非对齐、用了 WSTRB)
   └─ 地址未命中(无人解码)   → bresp/rresp = AXI_RESP_DECERR_C   (通常由交叉开关/默认处理填)
        │
        ▼
   装进 AxiLiteReadSlaveType.rresp / AxiLiteWriteSlaveType.bresp
        │
        ▼
   主机收到后据此判断本笔成功/失败
```

`SLVERR` 的触发条件在 `AxiLitePkg.vhd` 的注释里列得清清楚楚（见下），核心是：AXI-Lite 只接受「32 位访问、单字（AWLEN=0）、对齐、不用 WSTRB 选择性写」这一种规整访问，任何偏离都应回 `SLVERR`。

#### 4.3.3 源码精读

四个响应码常量集中在包的开头：

[axi/axi-lite/rtl/AxiLitePkg.vhd:L28-L49](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L28-L49) —— 响应码定义。注意 `AXI_RESP_EXOKAY_C` 旁的注释「AXI-Lite 没有 exclusive access，这只是占位」，以及 `AXI_RESP_SLVERR_C` 注释里列出的 4 种「不支持的访问」（非 32 位 SIZE、AWLEN≠0、非对齐、使用了 WSTRB），还有 `AXI_RESP_DECERR_C` 注释里说的「任何解码不到合法目标的访问都回 DECERR」。

响应码随后被写进读/写从机记录的 `rresp`/`bresp` 字段（见 4.1.3 引用的 L82-L97、L150-L158），也出现在初值常量里（默认 `OK`）。

在扁平器里还能看到一个有趣的「美化响应」技巧：当 `EN_ERROR_RESP=false` 时，强制把对外端口回成 `OK`，隐藏内部错误：

[axi/axi-lite/ip_integrator/SlaveAxiLiteIpIntegrator.vhd:L141-L141](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/ip_integrator/SlaveAxiLiteIpIntegrator.vhd#L141-L141) —— `S_AXI_RRESP <= S_AXI_ReadSlave.rresp when(EN_ERROR_RESP) else AXI_RESP_OK_C;`：可选地把读响应钳位成 `OK`，写响应（L154）同理。这在把 SURF 从机接到对错误「过敏」的外部 IP 时很有用。

仿真侧的从机响应检查也在包里：`axiLiteBusSimWrite` 在收到 `SLVERR`/`DECERR` 时会 `report ... severity warning`：

[axi/axi-lite/rtl/AxiLitePkg.vhd:L1139-L1143](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L1139-L1143) —— 仿真写过程序在收到非 `OK` 响应时打印告警，方便测试时立刻发现「访问打到了错误地址」或「从机拒绝了访问」。

#### 4.3.4 代码实践

**实践目标**：搞清 `SLVERR` 与 `DECERR` 的区别，并能在地址解码里正确选用。

**操作步骤**：

1. 读 `AxiLitePkg.vhd` L33-L49 的注释，把 `SLVERR` 的 4 个触发条件抄成一张小卡片。
2. 回顾 4.1.3 中 `AxiVersion` 的端口，设想它的寄存器空间只覆盖了 `0x000`–`0x0FF`。
3. 用文字推演两种访问的响应：
   - 主机写 `0x000`（合法地址、整字、对齐、`wstrb=0xF`）→ 应回什么？
   - 主机写 `0x000` 但 `wstrb=0x1`（只想写最低字节，违反 AXI-Lite 规则）→ 应回什么？
   - 主机读 `0x500`（地址超出该从机空间，无人解码）→ 应回什么？

**需要观察的现象**：你会体会到——**同样是「这次访问没成功」**，`SLVERR` 强调「我（从机）在，但我拒绝这种访问」，`DECERR` 强调「这段地址根本没我（从机）」。

**预期结果**：三种情况依次应为 `OK`、`SLVERR`、`DECERR`。其中 `DECERR` 通常不是叶子从机自己产生的，而是由交叉开关 / 默认地址处理逻辑对「无人认领」的访问统一兜底（u3-l3 会讲 `AxiLiteCrossbar`）。

> 是否运行：这是协议理解型实践，可结合 u9 的 cocotb 测试在仿真里实际触发 `SLVERR`/`DECERR` 并观察告警（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `AXI_RESP_EXOKAY_C` 在 AXI-Lite 里只是占位？

> **答案**：EXOKAY 表示「独占访问（exclusive access）成功」，而独占访问是完整 AXI4 才有的原子访问机制（LDREX/STREX 那一类）。AXI-Lite 作为精简子集根本不支持独占访问，所以这个码永远不会在 AXI-Lite 链路上真实产生，包里保留它只是为了让常量集合与 AXI 规范一一对应、便于和完整 AXI4 代码（u8-l1）共用同一套语义。

**练习 2**：一个 SURF 从机用 `axiSlaveDefault(..., axiResp => AXI_RESP_DECERR_C)` 处理「未映射地址」（这种用法将在 u3-l2 详解）。请解释为什么未映射地址应回 `DECERR` 而不是 `SLVERR`。

> **答案**：未映射地址意味着「主机访问了一段该从机根本不负责的地址」，语义上是「解码失败」，正好对应 `DECERR`（Decode Error）。`SLVERR` 的语义是「从机认得这个地址但拒绝这种访问方式」（如非对齐、用了 WSTRB），两者来源不同，混用会让主机误判故障性质。当然，在交叉开关架构里，叶子从机通常只回 `OK`/`SLVERR`，`DECERR` 由交叉开关对「无人认领」的访问统一兜底。

---

## 5. 综合实践

把本讲三个模块（记录、初值、响应码）串起来，完成一个「最小 AXI-Lite 从机接口块」的设计与阅读任务：

1. **端口（用记录）**：参照 `AxiVersion`，为一个名叫 `MyRegSlave` 的从机写出端口——`axiClk/axiRst` + 四条记录线（`axiReadMaster/Slave`、`axiWriteMaster/Slave`），不用任何扁平 `S_AXI_*` 端口。
2. **内部信号（用 INIT_C）**：在架构里声明两条从机内部信号 `locReadSlave` / `locWriteSlave`，并用 `AXI_LITE_READ_SLAVE_INIT_C` / `AXI_LITE_WRITE_SLAVE_INIT_C` 初始化，再 `axiReadSlave <= locReadSlave; axiWriteSlave <= locWriteSlave;` 把它们驱动到端口。
3. **响应码预案**：在注释里写明——本从机将来对「合法地址」回 `AXI_RESP_OK_C`，对「越界/未映射地址」预案回 `AXI_RESP_DECERR_C`（具体地址解码逻辑留给 u3-l2）。
4. **对照扁平器**：打开 `SlaveAxiLiteIpIntegrator.vhd` 的 L134-L155，确认它正是把你这里的记录字段（如 `locReadSlave.arready`、`rdata`、`rresp`）逐根映射到 Vivado 的 `S_AXI_ARREADY/RDATA/RRESP` 上——这就是「内部用记录、边界用扁平器」的完整闭环。

**预期结果**：你会得到一个约 30 行、端口干净、初值安全、响应策略明确的从机骨架。它本身还不能真正读写寄存器（缺地址解码），但已经把本讲的三个最小模块全部用上，是 u3-l2「寄存器端点模式」的最佳起点。

> 是否运行：综合实践以设计与阅读为主，可在学完 u3-l2 后接入 cocotb 测试台验证端到端读写（待本地验证）。

## 6. 本讲小结

- SURF 把一条 AXI-Lite 总线按「读/写 × 主/从」切成 **4 个记录**：`AxiLiteReadMasterType`、`AxiLiteReadSlaveType`、`AxiLiteWriteMasterType`、`AxiLiteWriteSlaveType`，每个记录装着它那一侧驱动的通道信号；记忆口诀是「VALID/数据归生产方，READY 归消费方」。
- 四个记录的并集恰好覆盖 AXI-Lite 全部 5 个通道（AR/R/AW/W/B），读路径用 2 个通道，写路径用 3 个通道（多一个写响应 B）。
- 每个记录都有 `_INIT_C` 初值常量：从机默认全 `'0'`（安全静止），主机默认把被动接收的 `rready/bready` 拉高、`wstrb` 设全 `'1'`，既不发误事务、也不当瓶颈。
- 响应码是 2 比特的 `AXI_RESP_OK_C/EXOKAY_C/SLVERR_C/DECERR_C`；AXI-Lite 不用 `EXOKAY`，常用的是 `OK`（成功）、`SLVERR`（访问方式不合法）、`DECERR`（地址无人解码）。
- SURF 在内部代码统一用记录（端口干净、可整体传给过程、有安全初值），只在 IP integrator 边界用 `SlaveAxiLiteIpIntegrator` 这类薄扁平器一次性拆成 `S_AXI_*` 标准端口——这是「记录 vs 扁平端口」之争的答案。

## 7. 下一步学习建议

- **下一篇 u3-l2「AXI-Lite 寄存器端点模式」** 是本讲的直接续集：它讲 `AxiLitePkg.vhd` 后半段的 `axiSlaveWaitTxn` / `axiSlaveRegister` / `axiSlaveRegisterR` / `axiSlaveDefault` 辅助过程，教你怎么在双进程寄存器块里把本讲的骨架变成「真正能读写寄存器、并对未映射地址回 `DECERR`」的从机。建议带着本讲综合实践的 `MyRegBlock` 骨架进入 u3-l2。
- 想看一个「完整、真实」的从机范例，可以直接读 [axi/axi-lite/rtl/AxiVersion.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd)，它同时演示了记录端口、初值、响应码与版本寄存器布局（u3-l4 会专门讲它）。
- 想了解多个 AXI-Lite 主/从如何被一条总线串起来（这正是 `DECERR` 的主要来源），可预读 [axi/axi-lite/rtl/AxiLiteCrossbar.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteCrossbar.vhd) 与包里的 `AxiLiteCrossbarMasterConfigType`（u3-l3 详解）。
