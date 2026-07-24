# AXI-Stream 记录与配置（AxiStreamPkg）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 SURF 把一条 AXI-Stream 数据流「折叠」成哪两个记录（`AxiStreamMasterType` 与 `AxiStreamSlaveType`），以及 Master 记录里装了哪 8 个侧带字段（`tValid / tData / tStrb / tKeep / tLast / tDest / tId / tUser`）。
- 看懂 `AxiStreamConfigType` 这张「流的身份证」如何用 7 个常量描述一条流的实际形态——数据有几个字节、侧带各几位、TKEEP 和 TUSER 用哪种编码模式——并理解为什么记录里的字段永远按「最大宽度」声明、真实宽度交给 config 在运行时裁剪。
- 解释 `tValid / tReady` 的采样握手语义，看懂 `AXI_STREAM_MASTER_INIT_C`、`AXI_STREAM_SLAVE_INIT_C`、`AXI_STREAM_SLAVE_FORCE_C` 三个初值常量各自代表的「安全静止态」。
- 会用 `axiStreamSetUserField / axiStreamSetUserBit / axiStreamGetUserField / axiStreamGetUserBit` 这一族包函数，按字节位置读写藏在宽达 1024 位的 `tUser` 里的逐字节侧带，并理解 `genTKeep / getTKeep` 在四种 TKEEP 模式下的互逆关系。

## 2. 前置知识

本讲默认你已经掌握 u1-l4 与 u3-l1 的内容，尤其是：

- `sl` / `slv` 是 `std_logic` / `std_logic_vector` 的短别名，来自地基包 `StdRtlPkg`；`Type` 后缀表示一个 VHDL 记录，`_INIT_C` 后缀表示该记录的初值常量，`Array` 后缀表示某记录的数组类型。
- AXI 总线用 **VALID/READY 握手**：生产方拉高 `xValid` 表示数据有效，消费方拉高 `xReady` 表示愿意收，只有某一拍两者同时为 `'1'`，这一笔数据才算「成交」。这句口诀在 u3-l1 里已经用过，本讲继续沿用。
- SURF 在内部代码里统一用**记录**而非扁平端口，目的是让一条「粗线」能整体连接、整体传给函数/过程。

另外补充两点协议背景，方便后面读代码：

- **AXI-Stream 是 AXI 的「数据平面」**。与 AXI-Lite（u3-l1，瘦地址/数据、单字无突发、专做寄存器映射）不同，AXI-Stream **没有地址**，只有一条「单向数据流」：主机源源不断地把数据拍（beat）推出去，从机源源不断地收。它适合搬移连续的、无地址概念的数据——比如以太网帧、视频像素流、ADC 采样、DMA 数据。
- **一拍（beat）= 一组同时有效的侧带**。除了核心的 `tData`，AXI-Stream 还定义了一堆可选侧带：`tKeep`（哪些字节有效）、`tStrb`（哪些字节是真实数据而非填充）、`tLast`（这是一帧的最后一拍）、`tDest`（这帧要送去哪个目的地）、`tId`（数据流标识）、`tUser`（用户自定义边带）。SURF 把这些全部塞进一个 `AxiStreamMasterType` 记录里。

> 一个关键直觉：**AXI-Lite 的复杂性在「地址解码」，AXI-Stream 的复杂性在「侧带解释」**。同一坨 `tData`，在不同的 `AxiStreamConfigType` 下，`tKeep` / `tUser` 的位含义完全不同。本讲的核心，就是讲清「记录是固定宽度的容器，config 决定容器里哪些位有意义」这套设计。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [axi/axi-stream/rtl/AxiStreamPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd) | **本讲主角**。集中定义 AXI-Stream 的两个记录、配置类型、各种初值常量、TUSER/TKEEP 工具函数，以及记录↔扁平 `slv` 的打包/解包函数。 |
| [axi/axi4/rtl/AxiPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiPkg.vhd) | 提供「最大宽度」上界常量 `AXI_MAX_DATA_WIDTH_C`（1024 位）与 `AXI_MAX_WSTRB_WIDTH_C`（128 字节），AXI-Stream 记录的字段宽度直接复用它们。 |
| [protocols/ssi/rtl/SsiPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPkg.vhd) | 真实使用范例：`ssiAxiStreamConfig` 如何构造一个 `AxiStreamConfigType`，并证明 `TDEST_BITS_C` 决定虚拟通道（VC）数量。 |

本讲只聚焦 `AxiStreamPkg.vhd` 的「类型与常量定义」以及 TUSER/TKEEP 这族工具函数。该包后半段的仿真发送/接收过程（`axiStreamSimSendFrame` 等）属于 u9 测试篇的内容，本讲只作预告，不展开。

---

## 4. 核心概念与源码讲解

### 4.1 AXI-Stream 记录：把一条流的侧带折叠成两个信号

#### 4.1.1 概念说明

AXI-Stream 是一条**单向**数据流：主机（Master，生产方）把数据推向从机（Slave，消费方）。和 AXI-Lite 的「读/写 × 主/从」四个记录不同，AXI-Stream 只有两个角色，因此只需**两个记录**：

| 记录 | 由谁驱动 | 装了什么 |
|------|----------|----------|
| `AxiStreamMasterType` | **主机**驱动（数据生产方） | 数据和几乎全部侧带 + `tValid`（数据有效） |
| `AxiStreamSlaveType` | **从机**驱动（数据消费方） | 只有一根 `tReady`（愿意收） |

归类口诀延续 u3-l1 的思路——**「VALID 与数据归生产方，READY 归消费方」**。在 AXI-Stream 里，生产方是主机，所以 `tValid` 和所有数据/侧带都在 Master 记录里；唯一的反方向信号 `tReady` 归从机，于是 Slave 记录瘦到只有一根线。

> 注意：和 AXI4 的五通道不同，AXI-Stream **没有独立的握手通道**。`tValid`/`tReady` 是和数据同拍出现的「伴随握手」——每一拍数据都自带一张「我有效（tValid）」的票，从机每拍都回答「我愿意收（tReady）」。两者同拍为 `'1'`，这一拍数据就过手。

#### 4.1.2 核心流程

一条 AXI-Stream 流的一次「成交」拍（handshake beat）：

```text
   时钟沿:      |----1----|----2----|----3----|----4----|
   master.tValid:  0        1        1        1        0
   master.tData:   X       D0       D1       D1       X
   slave.tReady:   X        0        1        1        X
                            ↑未成交   ↑成交    ↑成交
```

- 第 1 拍 `tValid=0`：主机没东西发，忽略。
- 第 2 拍 `tValid=1` 但 `tReady=0`：主机发了 `D0`，从机还没准备好，**这一拍作废**，主机必须把 `D0` **保持到下一拍**。
- 第 3 拍 `tValid=1` 且 `tReady=1`：`D1` 成交付出。
- 第 4 拍同样成交。

两条铁律：**「未成交的数据必须保持不变」**（主机不能在从机没收下时改数据），以及**「只有同时为 1 才算成交」**。这与 u3-l1 的 AXI-Lite 握手完全同构，只是 AXI-Stream 把它简化成单方向、伴随数据的形态。

#### 4.1.3 源码精读

先看「最大宽度」上界从哪来。`AxiStreamPkg` 复用了 `AxiPkg` 的两个常量，而不是自己重新定义：

[axi/axi-stream/rtl/AxiStreamPkg.vhd:L27-L28](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L27-L28) —— `AXI_STREAM_MAX_TDATA_WIDTH_C`（最大数据位宽，等于 `AXI_MAX_DATA_WIDTH_C`）和 `AXI_STREAM_MAX_TKEEP_WIDTH_C`（最大字节使能宽度）。

[axi/axi4/rtl/AxiPkg.vhd:L25-L26](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiPkg.vhd#L25-L26) —— 上界常量的真身：`AXI_MAX_DATA_WIDTH_C := 1024`（位），`AXI_MAX_WSTRB_WIDTH_C := 128`（字节，即 1024/8）。

> 这意味着 SURF 全仓库的 AXI-Stream 记录里，`tData` / `tUser` 一律按 **1024 位**声明、`tKeep` / `tStrb` 一律按 **128 字节**声明。这是「容器」的最大尺寸；一条具体的流用多少，由 4.2 的 config 决定。**记录的物理宽度在全仓库是固定的、统一的**——这是 SURF 能让任意两个 AXI-Stream 模块直接对接而不需位宽转换记录的前提。

接着看 Master 记录本体：

[axi/axi-stream/rtl/AxiStreamPkg.vhd:L30-L39](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L30-L39) —— `AxiStreamMasterType` 记录定义。8 个字段含义如下：

| 字段 | 宽度 | 含义 |
|------|------|------|
| `tValid` | 1 位 | 本拍数据有效（主机驱动） |
| `tData` | 最大 1024 位 | 数据载荷 |
| `tStrb` | 最大 128 字节 | 字节级「真实数据/填充」指示（1=真实数据，0=填充/null 字节） |
| `tKeep` | 最大 128 字节 | 字节级「有效字节」指示（1=有效，0=无效/位置空洞） |
| `tLast` | 1 位 | 本拍是一帧的最后一拍 |
| `tDest` | 8 位 | 目的地路由（常作虚拟通道 VC 号） |
| `tId` | 8 位 | 数据流 ID |
| `tUser` | 最大 1024 位 | 用户自定义边带（SURF 在这里塞逐字节的侧带，见 4.3） |

> `tStrb` 与 `tKeep` 的区别容易混：`tKeep` 标「这个字节位置是否存在数据」，`tStrb` 标「这个字节是否是真实内容（而非为了对齐塞进去的字节）」。以太网帧末尾的 padding 就是「`tKeep=1` 但 `tStrb=0`」的典型——位置在帧里、但不是真实数据。

再看 Master 记录的初值常量：

[axi/axi-stream/rtl/AxiStreamPkg.vhd:L41-L49](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L41-L49) —— `AXI_STREAM_MASTER_INIT_C`。注意三点：`tValid => '0'`（复位时不乱发数据）、`tKeep / tStrb => (others => '1')`（默认「全字有效」而不是全 0，这样即使有人忘了设 tKeep，一拍满字也能被正确识别）、其余侧带清零。这是一个「不发数据、但一旦开始发就是满字」的安全静止态。

紧随其后是一族数组类型与子类型：

[axi/axi-stream/rtl/AxiStreamPkg.vhd:L50-L57](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L50-L57) —— `AxiStreamMasterArray`（一维数组）、`AxiStreamMasterVectorArray`（二维数组），以及预定义宽度的 `AxiStreamDualMasterType`(2) / `QuadMasterType`(4) / `OctalMasterType`(8) 子类型。它们让「一组流」可以整体声明，避免手写 `array(0 to 7) of AxiStreamMasterType`。

Slave 记录相比之下极简：

[axi/axi-stream/rtl/AxiStreamPkg.vhd:L59-L61](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L59-L61) —— `AxiStreamSlaveType` 只有 `tReady` 一根线。这印证了「READY 归消费方」的口诀。

[axi/axi-stream/rtl/AxiStreamPkg.vhd:L72-L76](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L72-L76) —— Slave 两个初值常量：`AXI_STREAM_SLAVE_INIT_C`（`tReady => '0'`，复位时先不收，安全）和 `AXI_STREAM_SLAVE_FORCE_C`（`tReady => '1'`，强制永远收——用于下游永远不反压的场合，省去握手逻辑）。

> 这套 `_INIT_C` / `_FORCE_C` 的设计与 u3-l1 的 AXI-Lite 初值一脉相承：**复位初值一律保守（不乱发、不乱收）**，需要「永远就绪」时另设一个 `_FORCE_C` 常量显式表达。

#### 4.1.4 代码实践

**实践目标**：用真实源码的写法，声明一条 AXI-Stream 流的主/从端口，并体会「记录=一根粗线」的简洁。

**操作步骤**：

1. 打开 [AxiStreamPkg.vhd:L30-L49](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L30-L49)，确认 Master 记录有 8 个字段、Slave 记录只有 1 个字段。
2. 仿照下面的「示例代码」写一个最小实体的端口声明（注意：这是**示例代码**，不在 SURF 源码里，仅用于练习）：

```vhdl
-- 示例代码：用记录声明一条 AXI-Stream 流的端口
library ieee;
use ieee.std_logic_1164.all;
library surf;
use surf.StdRtlPkg.all;
use surf.AxiStreamPkg.all;

entity MyPassthrough is
   port (
      -- 时钟与复位（沿用 u1-l4 的三件套泛型在 entity header 里，此处略）
      axisClk    : in  sl;
      axisRst    : in  sl;
      -- 一条 AXI-Stream 流：进来一对 Master/Slave，出去一对 Master/Slave
      sAxisMaster : in  AxiStreamMasterType;   -- 上游发来的数据
      sAxisSlave  : out AxiStreamSlaveType;    -- 我对上游的反压
      mAxisMaster : out AxiStreamMasterType;   -- 我发给下游的数据
      mAxisSlave  : in  AxiStreamSlaveType     -- 下游对我的反压
   );
end entity MyPassthrough;
```

**需要观察的现象**：

- 若改成「扁平端口」，上面 4 个端口会被拆成约 8（master 字段）×2 + 1（slave）×2 = 十多根线，且每根都要手写宽度。用记录后只有 4 个端口、且宽度由包统一管理。
- 一个最简单的「直通（passthrough）」架构体只需 `mAxisMaster <= sAxisMaster; sAxisSlave <= mAxisSlave;`——整条粗线整体赋值，这正是记录的价值。

**预期结果**：

- 端口表极其干净；直通架构体两行即可（实际工程会在中间插 FIFO/流水，见 u4-l2）。
- 若在 GHDL 里对这段做语法分析（`--std=08`），应能通过 elaboration，因为 `AxiStreamMasterType` / `AxiStreamSlaveType` 都来自已编译的 `surf` 库。

> 待本地验证：是否真能通过 `make MODULES=$PWD analysis` 取决于你是否把它放进一个带 `ruckus.tcl` 的目录；本练习只要求读懂端口声明，不强求综合。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `AxiStreamSlaveType` 只有一根 `tReady`，而 `AxiStreamMasterType` 却有 8 个字段？

**参考答案**：因为 AXI-Stream 是单向流，数据与所有侧带都由生产方（主机）驱动、随数据同拍出现，所以全归 Master；唯一的反方向信号是「我愿意收」的 `tReady`，归消费方（从机），所以 Slave 记录只有它一根。这正是「VALID 与数据归生产方，READY 归消费方」口诀的体现。

**练习 2**：`AXI_STREAM_MASTER_INIT_C` 为什么把 `tKeep` 设成全 `'1'` 而不是全 `'0'`？

**参考答案**：全 `'1'` 表示「整字所有字节都有效」。这样即使使用者忘记显式设置 `tKeep`，一拍满字数据也会被下游按「全字有效」正确接收；若设成全 `'0'`，则任何未显式赋值的拍都会被解读为「没有有效字节」，等同于丢数据。这是「安全静止态」要既不发数据（`tValid=0`）、又为「一旦发就正确」做准备。

---

### 4.2 AxiStreamConfigType：一条流的「身份证」

#### 4.2.1 概念说明

4.1 说过，记录的字段永远按 **最大宽度**（1024 位 / 128 字节）声明。但一条真实的流通常窄得多——比如 64 位数据、只有 4 个用户位、不用 `tStrb`。**谁来告诉模块「这条流实际用几位」？**

答案就是 `AxiStreamConfigType`。它是一个**编译期常量记录**（实例化时定值），像一张「身份证」，描述一条 AXI-Stream 流的实际形态。几乎每个 SURF AXI-Stream 模块都有一个 `AXIS_CONFIG_G : AxiStreamConfigType` 泛型，上下游模块必须传**同一个** config（或等价 config）才能正确对接。

config 和记录的分工：

- **记录（Master/Slave）**：运行时在信号之间流动的「容器」，宽度固定为最大值，全仓库统一。
- **配置（Config）**：编译期定值的「说明书」，告诉模块在容器里**实际关心哪些位、如何解释**。

#### 4.2.2 核心流程

config 的 7 个字段各管一件事，下表先给全貌（精确含义在 4.2.3 逐字段展开）：

| config 字段 | 类型/范围 | 管什么 |
|------------|----------|--------|
| `TSTRB_EN_C` | boolean | 是否启用 `tStrb`（字节真实/填充指示） |
| `TDATA_BYTES_C` | 1..128 | 数据宽度（**字节**数）。位宽 = 此值 × 8 |
| `TDEST_BITS_C` | 0..8 | `tDest` 用几位（决定虚拟通道 VC 数 = 2^此值） |
| `TID_BITS_C` | 0..8 | `tId` 用几位 |
| `TKEEP_MODE_C` | TKeepModeType | `tKeep` 的编码模式（见下） |
| `TUSER_BITS_C` | 0..8 | 每个字节携带的 TUSER 位数 |
| `TUSER_MODE_C` | TUserModeType | TUSER 的布局模式（见 4.3） |

`TKEEP_MODE_C` 有四种取值（[L80](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L80)）：

| 模式 | tKeep 用多少位 | 含义 |
|------|---------------|------|
| `TKEEP_NORMAL_C` | `TDATA_BYTES_C` 位（每字节 1 位） | bit i = 字节 i 有效；有效位须从字节 0 起连续 |
| `TKEEP_COMP_C` | `ceil(log2(TDATA_BYTES_C))` 位 | 压缩成「有效字节数」的二进制计数 |
| `TKEEP_COUNT_C` | 固定较宽位 | 直接用二进制计有效字节数 |
| `TKEEP_FIXED_C` | 0 位 | 不用 tKeep，每拍恒为满字 |

`TUSER_MODE_C` 有四种取值（[L78](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L78)）：`TUSER_NORMAL_C`（每字节都带 TUSER）、`TUSER_FIRST_LAST_C`（只在首字节和末字节带 TUSER）、`TUSER_LAST_C`（只在末字节带）、`TUSER_NONE_C`（完全不用 TUSER）。

#### 4.2.3 源码精读

先看两个枚举类型的定义：

[axi/axi-stream/rtl/AxiStreamPkg.vhd:L78](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L78) —— `TUserModeType` 枚举（4 种 TUSER 布局模式）。

[axi/axi-stream/rtl/AxiStreamPkg.vhd:L80](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L80) —— `TKeepModeType` 枚举（4 种 tKeep 编码模式）。

接着是 config 记录本体：

[axi/axi-stream/rtl/AxiStreamPkg.vhd:L82-L91](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L82-L91) —— `AxiStreamConfigType` 记录定义。注意每个字段的子类型范围（`TDATA_BYTES_C : natural range 1 to AXI_STREAM_MAX_TKEEP_WIDTH_C` 即 1..128，`TDEST_BITS_C` 0..8，等等），这些范围就是上一节表格里范围的来源，VHDL 会在 elaboration 时检查越界。

默认 config 常量：

[axi/axi-stream/rtl/AxiStreamPkg.vhd:L93-L101](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L93-L101) —— `AXI_STREAM_CONFIG_INIT_C`。默认值：16 字节（128 位）数据、4 位 tDest（16 个 VC）、不用 tStrb、`TKEEP_NORMAL_C`、每字节 4 个 TUSER 位、`TUSER_NORMAL_C`。这是「一条中等宽度、常见配置」的合理默认。

> 注意 `TDEST_BITS_C => 4` 这个默认值：它意味着默认支持 **2^4 = 16 个虚拟通道（VC）**。在 SURF 里 `tDest` 几乎总是被当作「虚拟通道号」来路由（u4-l3 的 `AxiStreamDeMux` 按 `tDest` 分发，u5-l1 的 SSI 用 `tDest` 作 VC）。可在 SSI 包里直接看到这条约定的注释佐证：

[protocols/ssi/rtl/SsiPkg.vhd:L161](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPkg.vhd#L161) —— `ret.TDEST_BITS_C := tDestBits;` 旁边的注释 `-- 4 TDEST bits for VC`，明示 4 位 tDest 用于 VC 寻址。

来看一个真实的 config 构造函数，理解各字段如何被填进记录：

[protocols/ssi/rtl/SsiPkg.vhd:L149-L167](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPkg.vhd#L149-L167) —— `ssiAxiStreamConfig` 函数。它接受 `dataBytes` 等参数，逐字段填入一个 `AxiStreamConfigType` 并返回。可以看到 SSI 协议的典型选择：`TKEEP_COMP_C`（压缩 tKeep）、`TUSER_FIRST_LAST_C`（TUSER 只在首/末字节有效，用来放 SOF/EOF）、每字节 2 个 TUSER 位（放 SOF 与 EOFE）、4 位 tDest（16 个 VC）、不用 tStrb。这正是 u5-l1 要展开的 SSI 侧带编码，本讲只需看到「config 是如何被一行行组装出来的」。

`AXI_STREAM_CONFIG_INIT_C` 之外，还有一个会按 config 计算初值的函数：

[axi/axi-stream/rtl/AxiStreamPkg.vhd:L106](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L106) 与 [L197-L204](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L197-L204) —— `axiStreamMasterInit(config)`。它先拷贝 `AXI_STREAM_MASTER_INIT_C`，再按 config 把 `tKeep / tStrb` 设成「满字有效」（`genTKeep(config)`）。这是模块复位时最常用的初始化函数——它会按当前流的实际字节宽度生成正确的满字 tKeep，而不是 128 字节全 1。

#### 4.2.4 代码实践

**实践目标**：亲手构造一个 `AxiStreamConfigType`，并推算它对应的字节宽度与 VC 数。这就是任务规格里要求的实践。

**操作步骤**：

1. 在 [AxiStreamPkg.vhd:L82-L101](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L82-L101) 确认 config 的 7 个字段名与默认值。
2. 在 [SsiPkg.vhd:L149-L167](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPkg.vhd#L149-L167) 参考真实构造函数的写法。
3. 写出下面这段「示例代码」（**示例代码**，仅用于练习，不在源码中）：

```vhdl
-- 示例代码：构造一个 64 位数据、启用 TKEEP/TLAST/TDEST 的流配置
constant MY_AXIS_CONFIG_C : AxiStreamConfigType := (
   TSTRB_EN_C    => false,          -- 不用 tStrb
   TDATA_BYTES_C => 8,              -- 64 位 / 8 = 8 字节
   TDEST_BITS_C  => 4,              -- 2^4 = 16 个虚拟通道 (VC)
   TID_BITS_C    => 0,              -- 不用 tId
   TKEEP_MODE_C  => TKEEP_NORMAL_C, -- 每字节 1 位 tKeep（共 8 位）
   TUSER_BITS_C  => 2,              -- 每字节 2 个 TUSER 位
   TUSER_MODE_C  => TUSER_NORMAL_C  -- 每个字节都带 TUSER
);
```

**需要观察的现象 / 推算**：

- **字节宽度**：`TDATA_BYTES_C => 8`，即 8 字节 = 64 位数据。
- **VC 数**：`TDEST_BITS_C => 4`，即可寻址 \(2^{4} = 16\) 个虚拟通道。
- **tKeep 实际位数**：`TKEEP_NORMAL_C` 模式下，每字节 1 位，共 8 位（容器里 128 位中只用低 8 位）。
- **tUser 实际位数**：`TUSER_NORMAL_C` 且 `TUSER_BITS_C => 2`，则总 TUSER 位 = `TDATA_BYTES_C × TUSER_BITS_C = 8 × 2 = 16` 位（容器里 1024 位中只用低 16 位）。
- 容器宽度不变（`tData` 仍是 1024 位、`tKeep` 仍是 128 字节、`tUser` 仍是 1024 位），但本流只关心各自的低位。

**预期结果**：

- 写出的 config 在 elaboration 时通过范围检查（`TDATA_BYTES_C=8` 在 1..128 内、`TDEST_BITS_C=4` 在 0..8 内，等等）。
- 你能用一张表回答：字节宽 = 8，VC 数 = 16。

> 用数学式概括 VC 数与 TDEST 位宽的关系：

\[\text{VC 数} = 2^{\text{TDEST\_BITS\_C}}\]

\[ \text{TUSER 总位数} = \text{TDATA\_BYTES\_C} \times \text{TUSER\_BITS\_C} \quad (\text{当 } \text{TUSER\_MODE\_C} = \text{TUSER\_NORMAL\_C}) \]

#### 4.2.5 小练习与答案

**练习 1**：如果一条流的数据是 128 字节（1024 位），且 `TDEST_BITS_C = 8`，它能支持多少个 VC？

**参考答案**：\(2^{8} = 256\) 个 VC。`TDEST_BITS_C` 最大就是 8，对应 256 个可寻址目的地。

**练习 2**：为什么 config 字段的宽度范围（如 `TDATA_BYTES_C : 1 to 128`）和记录字段的固定宽度（`tData : 1024 位`）不一致？

**参考答案**：记录是全仓库统一的「最大容器」，必须容得下最宽的流（128 字节/1024 位），这样任意两条流的记录类型完全相同、可直接相连。config 则描述「本流实际用几位」，范围 1..128 是真实可能的取值。两者分离，使得「容器统一、内容可变」——这是 SURF AXI-Stream 能在任意宽度间互操作的根基。

---

### 4.3 握手、TUSER 函数与 TKEEP 工具

#### 4.3.1 概念说明

`AxiStreamConfigType` 之所以重要，一个直接后果是：**`tUser` 的位含义随 config 变化**。在 `TUSER_NORMAL_C` 模式下，tUser 是「逐字节平铺」的——第 `pos` 个字节对应 tUser 的第 `pos × TUSER_BITS_C` 到 `(pos+1) × TUSER_BITS_C - 1` 位。当总线宽到 128 字节、每字节 8 个用户位时，tUser 高达 1024 位，手算位偏移极易出错。

因此 SURF 提供了一族**包函数/过程**来封装「按字节位置读写 TUSER」：

- `axiStreamSetUserField(config, master, fieldValue, bytePos)` —— 把 `fieldValue` 写到指定字节位置的 TUSER 段。
- `axiStreamSetUserBit(config, master, bitPos, bitValue, bytePos)` —— 写单个 TUSER 位。
- `axiStreamGetUserField(config, master, bytePos)` —— 读指定字节位置的 TUSER 段。
- `axiStreamGetUserBit(config, master, bitPos, bytePos)` —— 读单个 TUSER 位。

`bytePos = -1` 是一个特殊约定，表示「最后一个有效字节」（由 tKeep 解码得出）。这让「在帧末字节打 EOF 标记」这类操作无需手算末字节位置。

类似地，`tKeep` 也有 `genTKeep`（生成）/ `getTKeep`（解码）一对函数，封装四种 TKEEP 模式的互逆转换。

#### 4.3.2 核心流程

**写一个 TUSER 位**（以在末字节打 SOF 标记为例）：

```text
1. 算 bytePos：若调用者传 bytePos=-1，先用 getTKeep(tKeep, config) 解出有效字节数 N，取末字节 pos = N-1
2. 算该字节 TUSER 段的最低位 lsb = TUSER_BITS_C * pos
3. 把 bitValue 写进 master.tUser(lsb + bitPos)
```

**读一个 TUSER 位**是上面的逆过程。

**生成 / 解码 tKeep**（NORMAL 模式为例）：

```text
genTKeep(bytes=3)  ->  tKeep = 0b00000111  （低 3 位置 1）
getTKeep(tKeep)    ->  数 tKeep 低位连续 1 的个数 = 3
```

四种模式下 `getTKeep` 的解读方式不同（COMP/COUNT 当计数读、FIXED 恒返回满字、NORMAL 数连续 1），但调用者无需关心——函数内部按 `TKEEP_MODE_C` 分支处理。

#### 4.3.3 源码精读

先看「定位末字节」的核心——`axiStreamGetUserPos`：

[axi/axi-stream/rtl/AxiStreamPkg.vhd:L221-L243](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L221-L243) —— `bytePos = -1` 时，调 `getTKeep(axisMaster.tKeep, axisConfig)` 解出有效字节数，再减 1 得末字节下标，并做上下界钳位。这正是「末字节约定」的实现：写/读 TUSER 时不写死字节号，而是跟着实际有效字节走。

接着看读字段：

[axi/axi-stream/rtl/AxiStreamPkg.vhd:L245-L275](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L245-L275) —— `axiStreamGetUserField`。核心是 `lsb := axisConfig.TUSER_BITS_C * pos`（[L257](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L257)），按字节位置算出 TUSER 段在 `tUser` 里的起始位。若 `TUSER_BITS_C = 0` 或 `TUSER_MODE_C = TUSER_NONE_C`，直接返回全 0（[L270-L272](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L270-L272)）。

[axi/axi-stream/rtl/AxiStreamPkg.vhd:L277-L290](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L277-L290) —— `axiStreamGetUserBit`，复用 `axiStreamGetUserField` 后取一位。

再看写字段（过程，`inout` 修改 master）：

[axi/axi-stream/rtl/AxiStreamPkg.vhd:L292-L311](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L292-L311) —— `axiStreamSetUserField`。同样的 `lsb := TUSER_BITS_C * pos`，然后把 `fieldValue` 平移到 `tUser` 的对应段并赋值（[L306](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L306)）。若模式为 NONE，则把整个 `tUser` 清零。

[axi/axi-stream/rtl/AxiStreamPkg.vhd:L313-L327](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L313-L327) —— `axiStreamSetUserBit`，直接定位 `tUser(TUSER_BITS_C*pos + bitPos)` 写一位（[L325](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L325)）。

> 真实使用范例：SSI 协议把 SOF/EOFE 编进 TUSER 的第 1/0 位，于是 [SsiPkg.vhd:L169-L177](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPkg.vhd#L169-L177) 的 `ssiGetUserEofe` 只是一行 `axiStreamGetUserBit(axisConfig, axisMaster, SSI_EOFE_C)`（`SSI_EOFE_C = 0`）。协议层完全不用手算 TUSER 位偏移，全交给本讲这族函数。

最后看 tKeep 工具：

[axi/axi-stream/rtl/AxiStreamPkg.vhd:L359-L369](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L359-L369) —— `genTKeep(bytes)`（按字节数生成 NORMAL 模式 tKeep，低 `bytes` 位置 1）。

[axi/axi-stream/rtl/AxiStreamPkg.vhd:L385-L389](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L385-L389) —— `genTKeep(config)`，按 config 的字节宽度生成满字 tKeep。

[axi/axi-stream/rtl/AxiStreamPkg.vhd:L391-L432](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L391-L432) —— `getTKeep(tKeep, axisConfig)`，四种模式的解码：`TKEEP_COUNT_C` 当二进制计数读（[L399-L400](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L399-L400)）；`TKEEP_FIXED_C` 恒返回满字（[L403-L405](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L403-L405)）；NORMAL 数连续 1（[L425-L426](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L425-L426)）；COMP 假设低位有效、返回 i+1（[L421-L423](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L421-L423)）。注释 [L411-L419](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L411-L419) 还专门记录了仿真确认过的 for 循环顺序，避免读者怀疑迭代次序。

#### 4.3.4 代码实践

**实践目标**：用包函数（而非手算位偏移）在末字节写一个 TUSER 位，并验证 tKeep 的生成/解码互逆。

**操作步骤**：

1. 读 [L313-L327](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L313-L327) 确认 `axiStreamSetUserBit` 的签名：`(axisConfig, axisMaster, bitPos, bitValue, bytePos := -1)`。
2. 读 [L359-L369](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L359-L369) 与 [L391-L432](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L391-L432) 确认 `genTKeep` / `getTKeep` 的语义。
3. 写出下面这段「示例代码」（**示例代码**，仅用于练习）：

```vhdl
-- 示例代码：用包函数操作 TUSER 与 tKeep（在一个 comb 进程的变量区里）
-- 假设 v : AxiStreamMasterType, AXIS_CONFIG_C : AxiStreamConfigType (TDATA_BYTES_C=8, TUSER_BITS_C=2)

-- 1) 准备一拍「3 个有效字节」的数据
v.tKeep := genTKeep(3);              -- NORMAL 模式 -> 低 3 位为 1，即 0b00000111

-- 2) 在「最后一个有效字节」(bytePos=-1 -> 第 2 字节) 的 TUSER 里打一位标记
axiStreamSetUserBit(AXIS_CONFIG_C, v, bitPos => 0, bitValue => '1', bytePos => -1);
-- 效果：v.tUser 的第 (TUSER_BITS_C*2 + 0) = 4 位置 1

-- 3) 反向验证：读回末字节的该位
flag := axiStreamGetUserBit(AXIS_CONFIG_C, v, bitPos => 0, bytePos => -1);
-- 期望 flag = '1'

-- 4) tKeep 互逆验证
n := getTKeep(v.tKeep, AXIS_CONFIG_C);  -- 期望 n = 3
```

**需要观察的现象**：

- `genTKeep(3)` 后 `v.tKeep` 的低 3 位为 1，其余为 0；`getTKeep` 反解回 3。
- `bytePos => -1` 自动定位到第 2 字节（因 `getTKeep` 返回 3、末字节 = 3−1 = 2），无需手写「2」。
- TUSER 位偏移由 `TUSER_BITS_C * pos` 决定，与 4.2 推算一致。

**预期结果**：

- `flag = '1'`、`n = 3`，验证了「写进去的位能按相同 config 读回来」、以及 `genTKeep`/`getTKeep` 在 NORMAL 模式下互逆。

> 待本地验证：以上行为可在 cocotb/GHDL 测试台里用一个最小 TB 复现（参见 u9 的测试方法学）；本练习只需在阅读层面确认位偏移计算正确。

#### 4.3.5 小练习与答案

**练习 1**：`axiStreamSetUserBit` 的 `bytePos` 参数默认是 `-1`，它代表什么？

**参考答案**：代表「最后一个有效字节」。函数内部先用 `getTKeep(tKeep, config)` 解出本拍有效字节数 `N`，再取 `pos = N - 1` 作为目标字节。这样「在帧末字节打标记」的代码无需关心当前拍到底有几个有效字节。

**练习 2**：`TKEEP_COMP_C` 与 `TKEEP_NORMAL_C` 模式下，8 字节总线的 tKeep 各占多少位？`getTKeep` 分别如何解读？

**参考答案**：NORMAL 模式占 8 位（每字节 1 位），`getTKeep` 数低位连续 1 的个数；COMP 模式占 `ceil(log2(8)) = 3` 位，`getTKeep` 把它当一个二进制计数（有效字节数）来读。COMP 用更少的线换更复杂的解读，适合引脚/位宽紧张但双方都了解模式的场合。

**练习 3**：为什么 SURF 要把 TUSER 的位偏移计算封装成函数，而不让使用者直接 `master.tUser(3) <= '1'`？

**参考答案**：因为 TUSER 的位含义完全由 config 决定——不同的 `TUSER_BITS_C` / `TDATA_BYTES_C` / `TUSER_MODE_C` 组合下，同一个逻辑「第 N 字节的第 K 个用户位」对应的物理位偏移不同。直接写死物理下标会让代码与某个具体 config 绑死、换总线宽度就错。封装成函数后，调用者只表达逻辑意图（哪个字节、哪一位），位偏移由函数按 config 算，代码可在不同宽度间复用。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一个小任务：**为一条自定义的 AXI-Stream 流写出「配置 + 端口 + 复位初值 + 一次侧带写入」的完整骨架**。

任务要求：

1. **定义 config**：数据 32 位（4 字节）、启用 tStrb、tDest 用 3 位（算一下 VC 数）、`TKEEP_NORMAL_C`、`TUSER_BITS_C = 1`、`TUSER_NORMAL_C`。
2. **声明端口**：用 `AxiStreamMasterType` / `AxiStreamSlaveType` 声明一对入/出流端口（参考 4.1.4 的示例代码）。
3. **复位初值**：在 `seq` 进程的复位分支里，用 `axiStreamMasterInit(MY_CONFIG_C)` 初始化输出 master（参考 [L197-L204](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L197-L204)），用 `AXI_STREAM_SLAVE_INIT_C` 初始化输入 slave。
4. **侧带写入**：在 `comb` 进程里，构造一拍「2 个有效字节、末字节 TUSER 位 0 = 1」的输出——用 `genTKeep(2)` 设 tKeep，用 `axiStreamSetUserBit(..., bytePos => -1)` 设末字节标记（参考 4.3.4）。

完成后，你应当能回答：

- 这条流的字节宽度 = **4 字节（32 位）**。
- VC 数 = \(2^{3} = 8\) 个。
- 末字节下标（当 tKeep 表示 2 字节有效时）= **第 1 字节**，对应的 TUSER 物理位 = `TUSER_BITS_C × 1 + 0` = **第 1 位**。
- 整条流的记录类型与一条 1024 位流的记录类型**完全相同**（都是 `AxiStreamMasterType`），区别只在 config。

> 这是一个**源码阅读 + 骨架编写型实践**：不强求你跑综合，而是要求你把「记录是固定宽容器、config 决定有效位、包函数封装 config 相关位运算」三件事在一段不到 50 行的骨架里体现出来。写完后对照 u4-l2（`AxiStreamFifoV2`）看真实模块如何用同样的 config 泛型串联 FIFO，会非常自然。

## 6. 本讲小结

- AXI-Stream 是 AXI 的**单向数据平面**，SURF 用两个记录折叠它：`AxiStreamMasterType`（主机驱动，含 `tValid` + 数据 + 全部侧带）和 `AxiStreamSlaveType`（从机驱动，只有 `tReady`）。握手口诀仍是「VALID 与数据归生产方，READY 归消费方」。
- 记录字段**一律按最大宽度声明**（`tData`/`tUser` 1024 位、`tKeep`/`tStrb` 128 字节，源自 `AxiPkg` 的 `AXI_MAX_DATA_WIDTH_C`/`AXI_MAX_WSTRB_WIDTH_C`），这让任意两条流的记录类型相同、可直接相连。
- `AxiStreamConfigType` 是一条流的「身份证」，用 7 个编译期常量（`TDATA_BYTES_C` / `TDEST_BITS_C` / `TID_BITS_C` / `TSTRB_EN_C` / `TKEEP_MODE_C` / `TUSER_BITS_C` / `TUSER_MODE_C`）描述实际形态；其中 `TDEST_BITS_C` 决定虚拟通道数 \(2^{\text{TDEST\_BITS\_C}}\)。
- `AXI_STREAM_MASTER_INIT_C`（复位不发、tKeep 全 1）、`AXI_STREAM_SLAVE_INIT_C`（`tReady=0`）、`AXI_STREAM_SLAVE_FORCE_C`（`tReady=1`）提供三种安全静止态；`axiStreamMasterInit(config)` 按实际字节宽度生成满字初值。
- `axiStreamSetUserBit/Field` 与 `axiStreamGetUserBit/Field` 一族函数封装了「按字节位置读写 TUSER」的位偏移计算，`bytePos = -1` 表示末字节；`genTKeep`/`getTKeep` 在四种 TKEEP 模式下互逆转换。协议层（如 SSI 的 SOF/EOFE）全部通过这族函数操作 TUSER，从不手算偏移。

## 7. 下一步学习建议

- **u4-l2（AXI-Stream FIFO、流水与位宽调整）**：看 `AxiStreamFifoV2` 如何用本讲的 `AXIS_CONFIG_G` 泛型配置一条流式 FIFO，以及 `AxiStreamPipeline` / `AxiStreamResize` 如何在不破坏帧边界和 TKEEP 语义的前提下做流水与位宽变换。
- **u4-l3（AXI-Stream 路由：Mux / DeMux / Gearbox）**：看 `AxiStreamDeMux` 如何按本讲的 `tDest`（VC 号）把一条流分发到多路输出——本讲的 `TDEST_BITS_C` 在那里变成真实的路由表。
- **u5-l1（SSI 侧带与帧边界）**：看 SSI 如何复用本讲的 `axiStreamSetUserBit` 把 SOF/EOF/EOFE 编进 TUSER，把本讲的「TUSER 函数」用到真实协议里。建议先回看本讲引用的 [SsiPkg.vhd:L149-L177](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPkg.vhd#L149-L177) 作为预习。
