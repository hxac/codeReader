# 顶层实体：端口、泛型与外部接口

## 1. 本讲目标

前三个单元你已经把 `fpga_base` 当作「项目门面」「文件柜」「打包流水线」看了一圈，但一直没有真正打开它的电路本体。从本篇开始，我们终于要**读 VHDL 源码**了。

本篇聚焦一个文件、一个问题：**`fpga_base` 这个 IP 核对外暴露了哪些「接口」？** 也就是 VHDL 里 `entity`（实体）部分——它定义了这个 IP 的「插座」：有哪些引脚（端口 `port`）、有哪些可配置参数（泛型 `generic`）。

读完本讲，你应当能够：

- 读懂 **AXI4 从机**的五个独立通道（读地址、读数据、写地址、写数据、写响应）各自包含哪些信号，以及每对 `valid/ready` 握手信号的方向。
- 理解 `C_VERSION`、`C_FREQ_AXI_CLK_HZ`、`C_FREQ_BLINKING_LED_HZ`、`C_S00_AXI_ID_WIDTH`、`C_USE_INFO_FROM_SCRIPT` 等**泛型参数**各自的含义与默认值。
- 把握 `o_led`、`i_sw`、`o_blink` 三个**物理端口**与内部寄存器数组之间的对应关系。
- 说清楚为什么复位信号 `s00_axi_aresetn` 是**低有效**（名字带 `n`），而内部却又把它翻转成高有效来用。

本讲是整个第二单元（AXI4 从机寄存器接口）的「门口」。后续 `u2-l2` 会讲这些 AXI 信号如何被降级成简单的寄存器数组，`u2-l3` 会讲每个寄存器地址对应什么功能。但这一切的前提，是先认全今天这些端口和泛型。

## 2. 前置知识

本讲是 **intermediate** 级别，会真正读 VHDL。在进入源码前，先用大白话补几个本讲绕不开的概念：

- **VHDL 的 entity（实体）与 architecture（结构体）**：VHDL 描述一个电路时，`entity` 负责「**对外长什么样**」（有哪些引脚、参数），`architecture` 负责「**内部怎么实现**」（逻辑）。本篇只看 `entity`。打个比方：`entity` 是芯片的数据手册首页（引脚定义），`architecture` 是内部电路图。
- **泛型 generic**：写在 `entity` 里的「**可配置参数**」，相当于函数的默认参数。打包 IP 时，使用者可以在 Vivado GUI 里改这些值，从而让同一个 IP 适配不同的场景（比如不同的时钟频率）。泛型在综合时被固化成常量。
- **端口 port 与方向 `in/out`**：每个端口有一个方向。`in` 是输入（信号从外面进来），`out` 是输出（信号从 IP 往外送）。AXI 的握手信号里，主机（master）驱动的 `valid` 对从机是 `in`，从机驱动的 `ready` 对自己来说是 `out`。
- **AXI4 协议**：ARM 设计的高速总线协议，是 Xilinx Zynq/Zynq UltraScale+ 等 SoC 里处理器（PS）和可编程逻辑（PL）通信的主流接口。AXI4 把一次传输拆成**五个独立通道**，每个通道都靠一对 `valid`/`ready` 信号做**握手**：发送方拉高 `valid` 表示「数据有效」，接收方拉高 `ready` 表示「我能收」，二者同时为高那一拍，数据才算真正传过去。
- **低有效（active-low）信号**：平时为高电平（`'1'`）表示「无动作」，拉到低电平（`'0'`）才表示「触发」。在 AXI/AMBA 约定里，复位信号几乎总是低有效，名字末尾的 `n`（如 `aresetn`）就是 negative（低有效）的缩写。
- **`std_logic_vector` 与位宽 `downto`**：VHDL 里最常用的多比特信号类型。`std_logic_vector(7 downto 0)` 表示一个 8 比特向量，下标从 7（最高位 MSB）到 0（最低位 LSB）。`downto` 的方向约定是「MSB 在左/大下标」。

如果你对 AXI 完全陌生，不用紧张——本讲的「源码精读」会把每个通道拆开讲，读完你就有了直觉。承接 `u1` 单元已建立的术语（IP 核、Vivado、AXI、IP-XACT、PSI、`psi_common`），本篇不再重复定义它们。

## 3. 本讲源码地图

本讲**只精读一个文件**：

| 文件 | 作用 |
| --- | --- |
| [hdl/fpga_base_v1_0.vhd](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd) | IP 的顶层 VHDL 文件。它的 `entity` 定义了本讲要读的全部端口和泛型；`architecture` 则实例化了 `psi_common` 的 AXI 从机、日期寄存器、LED/blink/开关逻辑。本讲只聚焦 `entity`（第 27–102 行），`architecture` 留给后续讲义。 |

为佐证泛型参数在 GUI 里如何呈现，会**指路**（不精读）：

| 文件 | 作用 |
| --- | --- |
| [scripts/package.tcl](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl) | 打包脚本，里面用 `gui_create_parameter` 给每个泛型起了 GUI 上显示的名字，能帮我们理解泛型的真实意图。 |
| [component.xml](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml) | IP-XACT 元数据，登记了端口的位宽如何依赖泛型（如 ID 宽度）。 |

## 4. 核心概念与源码讲解

### 4.1 AXI4 信号通道

#### 4.1.1 概念说明

`fpga_base` 在处理器眼里，是一块**可以通过 AXI4 总线读写的「寄存器空间」**。处理器（或任何 AXI 主机）想读「版本号」，就发起一次 AXI 读事务；想点亮某个 LED，就发起一次 AXI 写事务。

AXI4 协议把一次完整的事务拆成**五个独立的通道**，每个通道是单向的、各自握手：

| 通道 | 方向 | 用于 |
| --- | --- | --- |
| **读地址 AR** | 主机→从机 | 主机告诉从机「我要从某个地址读」 |
| **读数据 R** | 从机→主机 | 从机把读到的数据送回主机 |
| **写地址 AW** | 主机→从机 | 主机告诉从机「我要往某个地址写」 |
| **写数据 W** | 主机→从机 | 主机把要写的数据送给从机 |
| **写响应 B** | 从机→主机 | 从机告诉主机「这次写完成/出错」 |

注意读事务用 AR + R 两个通道，写事务用 AW + W + B 三个通道。五个通道可以**并行、乱序**工作，这正是 AXI4 高吞吐的来源。`fpga_base` 作为一个 AXi **从机（slave）**，它的端口方向是「站在从机的视角」定义的：AR/AW/W 通道里 `valid` 是 `in`（主机驱动）、`ready` 是 `out`（从机驱动）；R/B 通道反过来。

#### 4.1.2 核心流程

一次最简单的**单拍读事务**（不考虑突发）大致这样发生：

```text
主机                    从机(fpga_base)
  |  AR: arvalid=1, araddr=0x00  -->   (读地址通道，主机发地址)
  |  <-- arready=1                  (从机表示收到地址)
  |  上面两者同拍为1 => 地址握手成功
  |                                 |
  |  <-- R: rvalid=1, rdata=版本号, rresp=00  (读数据通道，从机回数据)
  |  rready=1 -->                    (主机表示收到数据)
  |  同拍为1 => 数据握手成功，事务完成
```

一次**单拍写事务**则要走三个通道：

```text
主机                    从机(fpga_base)
  |  AW: awvalid=1, awaddr=0x60 -->  (写地址)
  |  <-- awready=1
  |  W:  wvalid=1, wdata=LED值  -->  (写数据)
  |  <-- wready=1
  |  <-- B: bvalid=1, bresp=00       (写响应：成功)
  |  bready=1 -->
```

五个通道都用同一个时钟 `s00_axi_aclk`，复位用 `s00_axi_aresetn`。值得强调的是：AXI4 与简化版 AXI4-Lite 不同——AXI4 **支持突发（burst）**，所以你会在地址通道看到 `arlen`/`arsize`/`arburst` 这些控制一次传输「读多少、每次几字节、地址怎么递增」的信号，还会在读数据通道看到 `rlast`（标记突发的最后一拍）。本讲先认全这些信号，它们如何被处理交给 `u2-l2`。

#### 4.1.3 源码精读

打开顶层文件，`entity` 从第 27 行开始。先看 AXI 总线接口的「系统信号」——时钟和复位：

[fpga_base_v1_0.vhd:59-60](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L59-L60) 定义了全局时钟 `s00_axi_aclk`（输入）和全局复位 `s00_axi_aresetn`（输入，低有效，注释明确写了 `This signal is low active.`）。

> 端口名前缀 `s00_` 的含义：`s` = slave（从机），`00` = 第 0 个从机接口。Xilinx 打包向导默认就用这种命名。

接着是**读地址通道（AR）**，从机视角下大部分是 `in`：

[fpga_base_v1_0.vhd:61-71](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L61-L71) 声明了 `arid`/`araddr`/`arlen`/`arsize`/`arburst`/`arlock`/`arcache`/`arprot`/`arvalid`（均为主机驱动、对从机为 `in`）以及唯一的从机输出 `arready`。其中 `araddr` 被固定为 8 位宽（见 4.2 节解释）。

**读数据通道（R）**则反过来，从机输出：

[fpga_base_v1_0.vhd:72-78](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L72-L78) 声明了从机驱动的 `rid`/`rdata`(32 位)/`rresp`/`rlast`/`rvalid`，以及主机驱动的 `rready`。`rdata` 固定 32 位，说明这是一个 32 位寄存器接口。

**写地址通道（AW）**与读地址通道对称：

[fpga_base_v1_0.vhd:79-89](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L79-L89) 声明了 `awid`/`awaddr`(8 位)/`awlen`/`awsize`/`awburst`/`awlock`/`awcache`/`awprot`/`awvalid` 与从机输出的 `awready`。

**写数据通道（W）**——注意 AXI4 里写数据通道**没有 `wid`**（这是 AXI4 相对 AXI3 的一项改动，写数据靠 AW 通道的 `awid` 关联）：

[fpga_base_v1_0.vhd:90-95](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L90-L95) 声明了 `wdata`(32 位)/`wstrb`(4 位字节写使能)/`wlast`/`wvalid` 与从机输出的 `wready`。`wstrb` 的 4 个比特分别对应 32 位数据的 4 个字节，允许「只写其中几个字节」。

**写响应通道（B）**：

[fpga_base_v1_0.vhd:96-100](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L96-L100) 声明了从机驱动的 `bid`/`bresp`/`bvalid` 与主机驱动的 `bready`。`bresp` 的 2 比特编码响应类型（`00`=OKAY 正常，`10`=SLVERR 从机错误，`11`=DECERR 地址译码错误）。

把五个通道的端口方向整理成一张表（站在从机 `fpga_base` 视角）：

| 通道 | 主机驱动（从机看是 `in`） | 从机驱动（从机看是 `out`） |
| --- | --- | --- |
| AR 读地址 | arid, araddr, arlen, arsize, arburst, arlock, arcache, arprot, **arvalid** | **arready** |
| R 读数据 | **rready** | rid, rdata, rresp, rlast, **rvalid** |
| AW 写地址 | awid, awaddr, awlen, awsize, awburst, awlock, awcache, awprot, **awvalid** | **awready** |
| W 写数据 | wdata, wstrb, wlast, **wvalid** | **wready** |
| B 写响应 | **bready** | bid, bresp, **bvalid** |

规律：`valid` 永远由「发数据的一方」驱动，`ready` 永远由「收数据的一方」驱动。

#### 4.1.4 代码实践

**实践目标**：亲手把五个 AXI 通道的信号归类，建立「站在从机视角」的方向直觉。

**操作步骤**：

1. 用编辑器打开 `hdl/fpga_base_v1_0.vhd`，定位到第 56–100 行（AXI Slave Bus Interface 区块）。
2. 准备一张五行的表格（或用五支不同颜色的笔在打印件上标），每行对应一个通道：AR、R、AW、W、B。
3. 逐个端口读它的方向（`in` 还是 `out`）和注释，把信号名填进对应通道的行；`in` 的归到「主机驱动」列，`out` 的归到「从机驱动」列。
4. 特别留意每个通道里成对出现的 `valid`/`ready`：确认 AR/AW/W 通道的 `valid` 是 `in`、`ready` 是 `out`，而 R/B 通道恰好相反。

**需要观察的现象**：你会发现五个通道结构高度对称——AR 和 AW 几乎一模一样（只是一个读一个写）；R 和 B 都是从机回送、都有 `resp`；唯独 W 通道最「轻」（没有 id、有 `wstrb` 字节掩码）。

**预期结果**：你整理出的表格应当与上一小节末尾那张表一致。重点是确认：**每个通道各有一个 `valid` 和一个 `ready`**，共 5 对握手信号。

> 是否需要运行工具？不需要。这是一个「源码阅读型实践」，目的是建立端口方向直觉，为 `u2-l2` 读 AXI 从机实现打基础。

#### 4.1.5 小练习与答案

**练习 1**：`fpga_base` 用的是 AXI4-Lite 还是完整 AXI4？给出依据。

**参考答案**：是**完整 AXI4**（支持突发）。依据是地址通道里有 `arlen`/`arsize`/`arburst`、读数据通道有 `rlast`，这些都是 AXI4-Lite 没有的突发控制信号；AXI4-Lite 还会省掉所有 `id` 信号，而本设计有 `arid`/`rid`/`awid`/`bid`。

**练习 2**：为什么写数据通道（W）里没有 `wid` 信号，而读数据通道（R）里有 `rid`？

**参考答案**：这是 AXI4 协议相对 AXI3 的改动。AXI4 规定**写数据必须与写地址按顺序出现**，因此写数据用 AW 通道的 `awid` 来标识事务归属，W 通道不再需要单独的 `wid`；而读数据可以乱序返回，所以仍需 `rid` 让主机把数据与请求对应起来。

**练习 3**：`rresp`/`bresp` 是 2 比特，请说出至少两种取值的含义。

**参考答案**：`00` = OKAY（正常完成），`10` = SLVERR（从机错误），`11` = DECERR（地址译码错误，通常由互联矩阵而非从机给出），`01` = EXOKAY（独占访问）。`fpga_base` 这种简单寄存器从机正常情况下只会回 OKAY。

### 4.2 泛型参数

#### 4.2.1 概念说明

`entity` 里 `generic` 段定义的是**使用者在打包/例化时可调的参数**。`fpga_base` 的泛型一共 6 个，分三组：版本信息、闪烁 LED 配置、AXI ID 宽度。它们在综合时被固化为常量，决定了 IP 的具体行为。

#### 4.2.2 核心流程

泛型本身不参与运行时数据流动，它们影响的是「这个 IP 被综合成什么样」：

```text
打包时(Vivado GUI / package.tcl)
   使用者设置 generic 值  -->  Vivado 把 generic 当作常量代入 VHDL
                                  -->  综合出确定的电路
```

例如把 `C_FREQ_AXI_CLK_HZ` 设成 125 MHz、`C_FREQ_BLINKING_LED_HZ` 设成 2 Hz，综合时就会算出一个确定的分频常数，烧进 FPGA 后 blink 引脚就真的以 2 Hz 闪烁。改泛型、重新综合，行为就变——但**同一时刻一个比特流里这些值是固定死的**。

#### 4.2.3 源码精读

`entity` 的 `generic` 段在文件开头：

[fpga_base_v1_0.vhd:28-40](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L28-L40) 列出了全部 6 个泛型。逐个看：

**版本信息组**——

[fpga_base_v1_0.vhd:30-33](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L30-L33) 定义了：
- `C_VERSION : std_logic_vector := X"FFFFFFFF"`——一个 32 位版本号，默认全 1。它会被读到寄存器 0（地址 0x00）。
- `C_VERSION_MAJOR : string := "No Device"`——一个字符串（最长 16 字符）。
- `C_VERSION_MINOR : string := "No Project"`——又一个字符串（最长 16 字符）。

> 这里泛型名有点「名不副实」：虽然叫 `MAJOR/MINOR`，但它们实际承载的是两个 ASCII 字符串，最终展开成寄存器 0x40 和 0x50 附近的字符数据。打包脚本 [package.tcl:59-63](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L59-L63) 给它们的 GUI 标签是 `Major version (max 16 char)` / `Minor version (max 16 char)`。在 `u2-l3` 寄存器映射里你会看到 PSI 实际把它们当作「项目名 / 设施名」字符串来用。本讲只需记住：**它们是两个 16 字符以内的字符串泛型**。

**闪烁 LED 与脚本信息组**——

[fpga_base_v1_0.vhd:34-37](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L34-L37) 定义了：
- `C_FREQ_AXI_CLK_HZ : integer := 125_000_000`——AXI 时钟频率，默认 125 MHz。下划线只是分隔符，等价于 125000000。
- `C_FREQ_BLINKING_LED_HZ : integer := 2`——希望 blink 引脚闪烁的频率，默认 2 Hz。
- `C_USE_INFO_FROM_SCRIPT : boolean := false`——版本/编译信息来源开关。`false` 表示用传统 TCL 综合钩子注入；`true` 表示改用 Python 脚本（git hash）注入。它直接影响寄存器 0 读到的是 `C_VERSION` 还是 git hash。

**AXI ID 宽度**——

[fpga_base_v1_0.vhd:38-39](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L38-L39) 定义了 `C_S00_AXI_ID_WIDTH : integer := 1`——AXI 事务 ID 的位宽，默认 1。这个泛型会被几个 `id` 端口的位宽引用（见下文 4.3 节）。

把泛型汇总成表：

| 泛型 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `C_VERSION` | `std_logic_vector` | `X"FFFFFFFF"` | 32 位版本号，读到寄存器 0 |
| `C_VERSION_MAJOR` | `string` | `"No Device"` | 字符串 A（≤16 字符），展开到寄存器 0x40 区 |
| `C_VERSION_MINOR` | `string` | `"No Project"` | 字符串 B（≤16 字符），展开到寄存器 0x50 区 |
| `C_FREQ_AXI_CLK_HZ` | `integer` | `125_000_000` | AXI 时钟频率（Hz），决定 blink 分频 |
| `C_FREQ_BLINKING_LED_HZ` | `integer` | `2` | blink 期望闪烁频率（Hz） |
| `C_USE_INFO_FROM_SCRIPT` | `boolean` | `false` | true=用 Python/git hash 注入版本信息，false=用 TCL 综合钩子 |
| `C_S00_AXI_ID_WIDTH` | `integer` | `1` | AXI 事务 ID 位宽 |

#### 4.2.4 代码实践

**实践目标**：用两个时钟频率泛型手算 blink 的分频常数，验证「泛型 → 电路行为」的因果关系。

**操作步骤**：

1. 在 `architecture` 里找到分频常数的定义（虽然属于实现细节，但能佐证泛型如何被消费）：
   - 顶层用 `C_FREQ_AXI_CLK_HZ / C_FREQ_BLINKING_LED_HZ` 算出「每个闪烁周期包含多少个时钟周期」。
2. 用默认值代入：`125_000_000 / 2 = 62_500_000`，再除以 2 减 1（因为一个完整方波周期要翻转两次），得到计数器最大值约 `31_249_999`。
3. 反过来算：每个计数周期 = `1 / 125_000_000 s = 8 ns`；计数 `31_250_000` 次翻转一次 → 每翻转耗时 `0.25 s` → 翻转两次为一个完整周期 → `0.5 s` 一个周期 = **2 Hz**，与 `C_FREQ_BLINKING_LED_HZ` 一致。
4. 思考：如果把 `C_FREQ_AXI_CLK_HZ` 改成 `100_000_000`（100 MHz）而忘记改其它，闪烁频率会变成多少？

**需要观察的现象**：泛型只是「编译期常量」，改变它必须重新综合；同一个比特流运行时频率不会变。

**预期结果**：100 MHz 时，`100_000_000 / 2 = 50_000_000`，分频常数翻倍但时钟也变慢，两者抵消，**闪烁频率仍是 2 Hz**。这说明设计是「与时钟频率自适应」的——这正是 `C_FREQ_AXI_CLK_HZ` 这个泛型存在的意义。

> 待本地验证：如果你有 Vivado 环境，可在 IP GUI 改这两个泛型、综合后看 `o_blink` 的仿真波形周期。

#### 4.2.5 小练习与答案

**练习 1**：`C_VERSION` 的默认值是 `X"FFFFFFFF"`，为什么不直接给一个有意义的版本号？

**参考答案**：因为它是一个**占位符**。真实版本号要么由 TCL 综合钩子在每次编译时回写（`u3-l2`），要么由 Python 脚本用 git hash 替换（`u3-l3`，此时 `C_USE_INFO_FROM_SCRIPT=true` 让寄存器 0 读 `BuildGitHash_c` 而不是 `C_VERSION`）。默认全 1 是为了在未配置时一眼能看出「没设置」。

**练习 2**：`C_S00_AXI_ID_WIDTH` 这个泛型被哪些端口引用了？

**参考答案**：被四个带 `id` 的端口引用：`s00_axi_arid`、`s00_axi_rid`、`s00_axi_awid`、`s00_axi_bid`，它们的位宽都是 `C_S00_AXI_ID_WIDTH-1 downto 0`。改变这个泛型，这四个端口的位宽会同步变化（见 4.3.3）。

### 4.3 物理 IO 端口

#### 4.3.1 概念说明

除了 AXI 总线，`fpga_base` 还对外暴露了三个**物理端口**，它们是这个 IP 与「真实世界」打交道的地方：

- `o_led`：8 位输出，驱动板子上的 LED 灯。
- `i_sw`：8 位输入，读板子上的 DIP 拨码开关。
- `o_blink`：1 位输出，一个按设定频率自动闪烁的信号（常用来做「心跳灯」，确认 FPGA 在正常运行）。

这三个端口和 AXI 寄存器空间是**同一套数据的两个面孔**：软件通过 AXI 写寄存器来点 LED，读寄存器来获取开关状态；而物理引脚则把同样的值送到板级硬件上。

#### 4.3.2 核心流程

物理端口与寄存器数组的对应关系（这是 4.3 的核心，寄存器数组的详细语义在 `u2-l2` 展开）：

```text
软件写 0x60 寄存器  -->  reg_wdata(24)(7:0)  -->  o_led 引脚     (LED 输出)
板子上的 DIP 开关   -->  i_sw 引脚           -->  reg_rdata(25)(7:0) (软件读 0x64 看到)
自由运行计数器      -->  翻转 blink_led      -->  o_blink 引脚   (心跳，不受软件控制)
```

注意 `o_led` 既是寄存器 24 的回读内容，也是物理输出——软件写进去什么，LED 就亮什么，回读也能读到。`o_blink` 则完全由硬件自由运行，软件无法控制（它不占用寄存器）。

#### 4.3.3 源码精读

三个物理端口在 `entity` 的 `port` 段最前面：

[fpga_base_v1_0.vhd:43-54](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L43-L54) 集中声明了：
- `o_led : out std_logic_vector(7 downto 0)`——LED 输出。
- `i_sw : in std_logic_vector(7 downto 0)`——DIP 开关输入。
- `o_blink : out std_logic`——闪烁输出（单比特）。

接着才是 AXI 系统信号和五通道（4.1 节已讲）。注意 `arid`/`rid`/`awid`/`bid` 的位宽写成了 `C_S00_AXI_ID_WIDTH-1 downto 0`，例如：

[fpga_base_v1_0.vhd:62](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L62) 中 `s00_axi_arid` 的位宽直接引用了泛型 `C_S00_AXI_ID_WIDTH`，泛型如何「穿透」到端口位宽在这一行体现得最清楚。

物理端口如何与寄存器数组挂钩，在 `architecture` 里（虽属实现，但能印证对应关系）：

[fpga_base_v1_0.vhd:291-292](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L291-L292) 把寄存器 24 的低 8 位同时回读（`reg_rdata(24)`）和输出到 `o_led`——证实「写寄存器 24 = 点 LED」。

[fpga_base_v1_0.vhd:318](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L318) 把 `i_sw` 接到寄存器 25 的低 8 位——证实「读寄存器 25 = 读 DIP 开关」。

[fpga_base_v1_0.vhd:297-313](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L297-L313) 是一个时钟分频进程，自由运行地翻转 `blink_led`，最后 `o_blink <= blink_led`（第 313 行）输出。

最后回到本讲练习题的另一半：**复位极性**。`s00_axi_aresetn` 是低有效，但内部代码（如 blink 进程第 300 行）判断的却是 `if s00_axi_aresetn = '0'` 来复位。这里还有一个关键翻转：

[fpga_base_v1_0.vhd:140](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L140) 把外部低有效复位翻转成内部高有效信号 `s00_axi_areset <= not s00_axi_aresetn`。也就是说：**对外遵循 AXI 的低有效约定（`aresetn`），对内按 VHDL 习惯用高有效复位**，两者用一句 `not` 桥接。

#### 4.3.4 代码实践

**实践目标**：解释 `s00_axi_aresetn` 为何是低有效，并追踪它如何被内部使用。

**操作步骤**：

1. 在 `entity` 的端口列表找到 `s00_axi_aresetn`（第 60 行），注意注释 `This signal is low active.`，以及名字末尾的 `n`。
2. 在 `architecture` 找到第 140 行 `s00_axi_areset <= not s00_axi_aresetn;`，理解这是把低有效翻转成内部高有效。
3. 在 blink 进程（第 297–312 行）里，看复位分支 `if s00_axi_aresetn = '0'` 是如何把计数器清零的——注意这里直接用了低有效信号本身（`='0'` 即代表「复位生效」）。

**需要观察的现象**：同一个复位信号在代码里出现了两种用法——第 140 行把它翻转给（可能的）下游高有效逻辑，而进程里又直接判断它的低电平。两条路径表达的是同一件事：**信号为 0 时复位生效**。

**预期结果**：你能用自己的话讲清楚——
- **为什么低有效？** 因为这是 ARM AXI/AMBA 总线的约定（复位信号统一命名 `ARESETn`、低有效），Xilinx 的 PS 复位输出也是低有效，对齐约定能直接对接、减少极性转换。
- **为什么历史上有低有效约定？** 在上电瞬间，芯片引脚常被弱上拉为高，低有效复位意味着「默认不复位（高电平）」，复位需要主动拉低，更抗毛刺；这与许多老式总线一脉相承。
- **内部为什么又要翻转？** VHDL 代码里用 `if reset = '1' then` 的写法更直观（高有效），所以代码用一个 `not` 把外部低有效适配成内部高有效，两种风格各取所长。

> 是否需要运行？不需要，这是源码阅读实践。如果你有仿真环境，可在 testbench 里把 `s00_axi_aresetn` 先拉低一段时间再拉高，观察计数器从 0 开始计数。

#### 4.3.5 小练习与答案

**练习 1**：软件想让最低位的 LED 亮起，应该往哪个地址写什么值？`o_led` 和回读寄存器是什么关系？

**参考答案**：往寄存器 24（地址偏移 0x60，因为寄存器 24 × 4 字节 = 0x60）写 `0x01`。`o_led` 引脚和 `reg_rdata(24)` 的低 8 位接的是同一个来源（`reg_wdata(24)`），所以写进去的值既驱动 LED，也能被软件回读，是「写后可回读」的寄存器。

**练习 2**：`o_blink` 占用任何一个寄存器吗？软件能通过 AXI 改变它的频率吗？

**参考答案**：不占用寄存器，软件不能通过 AXI 控制 `o_blink`。它的频率完全由泛型 `C_FREQ_AXI_CLK_HZ` 和 `C_FREQ_BLINKING_LED_HZ` 在综合时决定。要改频率只能改泛型、重新综合。

**练习 3**：`i_sw` 是 8 位，但寄存器 25 是 32 位，多出来的 24 位会读到什么？

**参考答案**：`reg_rdata(25)(7 downto 0)` 接的是 `i_sw`，高 24 位没有被这个赋值驱动，会保持该寄存器数组的默认值（复位默认值，见 `u2-l2` 的 `ResetVal_g` 全 0）。所以读到的是低 8 位为开关状态、高位为 0。

## 5. 综合实践

把本讲的三个模块串起来，做一次「端口与泛型审计」：

**任务**：假设你要把这个 IP 例化到一个新板子上，该板子的 AXI 时钟是 **100 MHz**、只需要 **4 个 LED**（其余 4 位不用）、不需要 DIP 开关。请基于本讲读到的 `entity`，回答：

1. 哪些**泛型**需要修改、改成什么值？（提示：`C_FREQ_AXI_CLK_HZ` 至少要改。）
2. `o_led` 仍是 8 位端口，你例化时多余的 4 位如何处理？（提示：VHDL 例化时可以把未用的输出位悬空，但想想这是否会影响寄存器 24 的回读值。）
3. `i_sw` 这个输入如果板子上根本没有 DIP 开关，例化时应该怎么接？（提示：输入不能悬空，可接常量。）
4. AXI 五通道中，如果处理器侧的 AXI ID 宽度是 4，你需要改哪个泛型？改完后哪些端口的位宽会跟着变？

**参考答案要点**：

1. `C_FREQ_AXI_CLK_HZ` 改成 `100_000_000`；`C_FREQ_BLINKING_LED_HZ` 视需求保持或调整。LED 位宽无法通过泛型调（端口固定 8 位），所以泛型层面改不了 LED 数量。
2. 多余的 4 位在 VHDL 例化时可悬空（`open`），但寄存器 24 仍是 8 位可写可读，软件写高 4 位不会有物理效果（引脚悬空），回读却能读到写入值——这是一种「软约束」。
3. `i_sw` 必须接一个确定值（如全 `'0'`），否则综合工具会警告悬空输入；接全 0 则软件读寄存器 25 永远看到 0。
4. 改 `C_S00_AXI_ID_WIDTH := 4`，则 `arid`/`rid`/`awid`/`bid` 四个端口位宽从 1 变成 4。注意这需要和 BD 里的主机协商（`u4-l3` 会讲 BD 如何自动传播 ID 宽度）。

这个任务逼你把「泛型影响什么」「端口如何例化」「AXI 通道位宽如何随泛型变化」三件事联系起来，为下一讲 `u2-l2`（这些 AXI 信号如何被降级成寄存器数组）做好准备。

## 6. 本讲小结

- `fpga_base` 的 `entity` 暴露三类接口：**AXI4 从机总线**（五个独立通道）、**三个物理端口**（`o_led`/`i_sw`/`o_blink`）、以及**6 个可配置泛型**。
- AXI4 共有 **AR / R / AW / W / B** 五个通道，每个通道一对 `valid`/`ready` 握手；站在从机视角，AR/AW/W 的 `valid` 是输入，R/B 的 `valid` 是输出。本设计是**完整 AXI4**（有 `len/size/burst/last/id`），不是 AXI4-Lite。
- 6 个泛型分三组：版本信息（`C_VERSION` + 两个字符串）、闪烁 LED 配置（两个频率 + 信息源开关）、AXI ID 宽度。它们是**编译期常量**，改了要重新综合。
- 物理端口与寄存器数组一一对应：`o_led` ↔ 寄存器 24，`i_sw` ↔ 寄存器 25，`o_blink` 由硬件自由运行不占寄存器。
- 复位 `s00_axi_aresetn` 是**低有效**（遵循 ARM AXI 约定），内部通过 `not` 翻译成高有效使用——对外对内各取所长。
- AXI 地址宽度固定 **8 位** → 寻址空间 256 字节 → 配合 32 位数据 = **64 个寄存器**，这就是下一讲 `C_NUM_REG=64` 的由来。

## 7. 下一步学习建议

本讲你只读了 `entity`（插座），还没看「插座后面接了什么」。下一步：

- **`u2-l2` 复用 psi_common 的 AXI 从机寄存器接口**：精读 `architecture` 里如何用 `psi_common_axi_slave_ipif` 把这一大堆 AXI 信号**降级**成简单的三个数组 `reg_rdata`/`reg_wdata`/`reg_wr`，这是理解整个 IP 如何工作的关键一步。
- **`u2-l3` 寄存器映射：从偏移到功能**：把本讲提到的「寄存器 0、24、25、0x40 区、0x50 区」整理成完整的地址映射表，并对照 C 驱动头文件验证硬件/软件契约。

如果你对 AXI 协议本身想更系统，建议同时翻阅 ARM 的 *AMBA AXI and ACE Protocol Specification*，重点看「Chapter A: Signal Descriptions」与本讲五通道的信号一一对照。读完 `u2` 三篇，你就掌握了 `fpga_base` 最核心的「主干」，后面 `u3`（版本/编译时间机制）和 `u5`（软件栈）都是建立在这套寄存器接口之上。
