# 3-Wire SPI 与三态/读写位扩展

## 1. 本讲目标

本讲承接 u2-l2（spi_simple 核心架构）与 u2-l4（SPI 主控时序），聚焦当前 HEAD（提交 `fda4db7`，`DEVEL: 3-Wires SPI interface signal added`）刚刚引入、尚未写入 `Changelog.md` 的 3-Wire SPI 扩展能力。

学完后你应该能够：

1. 说清「4 线 SPI」与「3 线 SPI」的物理差别，以及为什么 3 线模式下需要一根额外的**三态控制信号** `spi_tri`。
2. 说清 `ReadBitPol_g`、`TriStatePol_g`、`SpiDataPos_g` 这三个新增 generic 各自配置什么：读写命令位的极性、三态控制信号的极性、以及数据字中有效数据的起始位置。
3. 画出从 Vivado GUI 参数 → `spi_vivado_wrp` → `spi_simple` → `psi_common_spi_master` 这条 generic/端口的**端到端透传链**，并解释为什么 `TriWiresSpi_g` 是纯 wrapper 级的「IP 端口可见性开关」而不进入核心 RTL。
4. 解释 `spi_tri` 端口为什么只在 `TriWiresSpi_g = true` 时才会出现在 IP 边界——这是 IP-XACT 的端口条件使能（port enablement），而不是 RTL 层的 `generate`。

## 2. 前置知识

阅读本讲前，请确认你已理解以下概念（前序讲义已建立）：

- **标准 SPI 四线**：`SCK`（时钟）、`MOSI`（主出从入）、`MISO`（主入从出）、`CS_n`（片选，低有效）。Master 在 `SCK` 驱动下经 `MOSI` 发、经 `MISO` 收，两条数据线物理独立，所以收发可以同时进行（u2-l4）。
- **IP-core 与 wrapper**：对外 IP 名 `spi_simple`，顶层实体却是 `spi_vivado_wrp`（u1-l2）。wrapper 把 AXI4 接口、`psi_common_spi_master` 引擎、`spi_simple` 核心捏在一起。
- **generic（参数）透传**：Vivado GUI 填的值 → `PARAM_VALUE` → `MODELPARAM_VALUE` → VHDL generic 实参 → 经 wrapper → 核心 → 引擎逐层传递，部分参数会改名（u2-l4、u3-l1）。
- **端口条件使能**：`package.tcl` 里用 `add_port_enablement_condition` 声明「某端口仅在某 generic 取某值时才在 IP 边界出现」，落到 `component.xml` 就是 `PORT_ENABLEMENT`（u1-l2、u3-l1）。
- **三态（tri-state）/ 高阻（high-Z）**：数字 IO 的一种状态，既不是强 0 也不是强 1，而是「放弃驱动、把总线让给别人」。FPGA 里用 IOBUF/OBUFT 之类的原语实现，靠一根 `T`（三态使能）控制「我驱动」还是「我放手」。

本讲新引入的术语：

| 术语 | 含义 |
|------|------|
| 3-Wire SPI | 只用 3 根线（`SCK` + 单根双向数据线 + `CS_n`）的 SPI 变体，数据线收发共用 |
| `spi_tri` | master 输出的三态控制信号，决定片外 IOBUF 当前是「驱动 MOSI 出去」还是「放手让 MISO 进来」 |
| R/W 位（读写位） | 3 线协议里数据字中带头的一位命令位，告诉从机这次是读还是写 |
| `ReadBitPol_g` | 「读操作」对应的 R/W 位电平极性 |
| `TriStatePol_g` | 三态控制信号的有效电平极性 |
| `SpiDataPos_g` | 数据字里有效载荷从第几位开始（跳过命令位之后的位置） |

## 3. 本讲源码地图

本讲围绕一条「3-Wire 信号链」展开，涉及以下文件：

| 文件 | 在本讲的作用 |
|------|--------------|
| `hdl/spi_vivado_wrp.vhd` | IP 顶层 wrapper：声明 `TriWiresSpi_g` 与 `spi_tri` 端口，把三个新 generic 与 `spi_tri` 透传给核心 |
| `hdl/spi_simple.vhd` | 核心：声明 `SpiTri` 端口与三个新 generic，把它们透传给底层引擎 `psi_common_spi_master` |
| `scripts/package.tcl` | 打包脚本（参数化唯一数据源）：声明 5 个新 GUI 参数，并用 `add_port_enablement_condition` 把 `spi_tri` 绑到 `TriWiresSpi_g` |
| `component.xml` | 打包产物（IP-XACT 清单）：记录 `spi_tri` 端口及其 `PORT_ENABLEMENT` 依赖、5 个新 `MODELPARAM`/`PARAM_VALUE` |

> 提醒：`component.xml` 与 `xgui/spi_simple_v1_4.tcl` 都是 `package.tcl` 的**自动生成产物**，日常只读不手改；要看「设计意图」就看 `package.tcl`，要看「打包后到底长什么样」就看 `component.xml`。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先讲 3-Wire SPI 的原理与 `spi_tri` 三态信号本身；再讲三个极性/位置 generic 的配置含义；最后把 generic 与端口串成一条端到端透传链，并解释 `spi_tri` 的条件综合。

### 4.1 3-Wire SPI 原理与 spi_tri 三态信号

#### 4.1.1 概念说明

标准 4 线 SPI 里，Master 的发送（`MOSI`）和接收（`MISO`）走两根独立线，互不干扰。但在引脚紧张的场景（比如很多 MEMS 传感器、ADC、寄存器型芯片只给了 3 根线），从机只提供**一根**双向数据线，Master 也只能用一根线既发又收。这就是 3-Wire SPI（也叫 SISO / 3-wire 模式）。

3-Wire 的核心矛盾是：**同一根线，发的时候 Master 要驱动它，收的时候 Master 必须放手、让从机驱动它**。这需要一个明确的「何时切换收发方向」的控制。本 IP 的做法是：core 内部仍然维护 `MOSI`（出）和 `MISO`（入）两个端口，但额外输出一根三态控制信号 `spi_tri`；IP 使用者在自己的顶层用一根 `spi_tri` 去驱动一个片外 IOBUF（或 OBUFT）原语，把 `MOSI`/`MISO` 合并成一根物理双向引脚。换言之，`spi_tri` 是「方向指挥棒」，真正的物理合线发生在 IP 之外。

之所以把合线放在 IP 外，是因为三态原语必须紧贴 FPGA 的 IO Block（I/O Block，IOB）才能正确实现双向引脚，而 IP 内部逻辑通常被综合/布局到 fabric 深处，不方便直接对外三态。所以 IP 只负责「算出方向」，使用者负责「在 IOB 上接 IOBUF」。

#### 4.1.2 核心流程

一次 3-Wire 读事务的方向切换大致如下（实际时序由底层引擎 `psi_common_spi_master` 产生，本 IP 只透传）：

```text
[CS_n 拉低] ── 命令阶段 ───────────── 数据阶段 ──────── [CS_n 拉高]
              Master 驱动数据线         Master 放手(高阻)
              发送 R/W 位 + 命令        从机驱动数据线
              spi_tri = “驱动”          spi_tri = “放手”
                  ↑                          ↑
            （读 R/W 位经 ReadBitPol_g 解码，决定在何处切换）
```

要点的逻辑化描述：

1. `CS_n` 拉低选中从机，事务开始。
2. **命令阶段**：Master 把数据字里的最高若干位当作命令/RW 位发出，此时 `spi_tri` 指示「Master 驱动」，物理线 = `MOSI`。
3. 引擎根据 `ReadBitPol_g` 判断这是「读」命令 → 进入**数据阶段**时把 `spi_tri` 切换到「放手」，物理线进入高阻，从机开始驱动。
4. 从机驱动的内容经同一根线回到 `MISO`，引擎采样。
5. `CS_n` 拉高，事务结束。

> 注意：上面描述的「何时切换」完全由**外部** `psi_common_spi_master` 引擎实现，本仓库不包含它的源码。`spi_simple` 与 `spi_vivado_wrp` 在这条链上只做一件事——**把 generic 和端口原样接过去**。这一点是本讲反复强调的「透传」设计。

#### 4.1.3 源码精读

**wrapper 端口声明** —— `spi_tri` 作为单比特输出无条件出现在端口表里：

[spi_vivado_wrp.vhd:49-54](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L49-L54) 声明了 `spi_sck / spi_cs_n / spi_mosi / spi_miso`，紧接着第 53 行新增的 `spi_tri : out std_logic`。注意它在 VHDL 层是**无条件**声明的——所谓「只在 `TriWiresSpi_g=true` 综合」是 IP-XACT 层面的事（见 4.3），不是 `port` 表里写 `generate`。

**核心端口声明** —— `spi_simple` 同样新增 `SpiTri`：

[spi_simple.vhd:73-79](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L73-L79) 第 76 行 `SpiTri : out std_logic`，与 `SpiMosi/SpiMiso` 并列。这里**没有** `TriWiresSpi_g` 这个 generic——核心根本不知道「是否 3 线」，它只是无条件把三态信号接出来。

**wrapper 把 `SpiTri` 接到顶层 `spi_tri`**：

[spi_vivado_wrp.vhd:257-264](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L257-L264) 第 261 行 `SpiTri => spi_tri`，与 `SpiMosi => spi_mosi`（259）、`SpiMiso => spi_miso`（260）并列，构成 SPI 物理端口的完整透传。

**核心把 `SpiTri` 接到引擎的 `spi_tri_o`**：

[spi_simple.vhd:294-299](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L294-L299) 第 297 行 `spi_tri_o => SpiTri`。真正「决定何时三态」的逻辑在外部 `psi_common_spi_master` 里，本仓库看不到，但可以确认信号被正确接到引擎输出。

> 阅读陷阱：`spi_vivado_wrp.vhd` 第 35 行 `TriWiresSpi_g` 的行尾注释写的是 `-- LSB or MSB first transmission`，这是从上一行 `LsbFirst_g` 复制粘贴漏改的，**与 3-Wire 无关**。真实含义看 `package.tcl` 第 107 行的描述 `"Enable 3-wires SPI interface"` 才对。不要被这条错误注释带偏。

#### 4.1.4 代码实践

**实践类型：源码阅读型实践。** 本特性尚处 DEVEL（开发中），仓库自带的 `top_tb.vhd` 还没接入 `spi_tri`，没有可直接跑的 3-Wire 自检场景，因此本练习以源码核对为主。

1. **实践目标**：确认 `spi_tri` 在 RTL 层是无条件声明、无条件驱动的，而不是用 `if TriWiresSpi_g generate` 包起来的。
2. **操作步骤**：
   - 打开 [spi_simple.vhd:73-79](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L73-L79)，确认 `SpiTri` 是 `port(...)` 里的一行，外面没有任何 `generate`/`if`。
   - 打开 [spi_simple.vhd:294-299](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L294-L299)，确认 `spi_tri_o => SpiTri` 是无条件端口映射。
   - 用只读检索确认 testbench 现状：在 `tb/` 下查找 `spi_tri`（见本讲 4.1.5 的练习）。
3. **需要观察的现象**：testbench 的 DUT 例化端口映射里**不会**出现 `spi_tri => ...`。
4. **预期结果**：本仓库当前 HEAD 的 `top_tb.vhd`（端口映射在 [top_tb.vhd:100-107](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L100-L107) 附近）只连了 `spi_mosi`、`spi_miso`、`spi_le`，没有连 `spi_tri`。这与提交信息 `DEVEL:`（开发中）一致——3-Wire 自检尚未补齐。VHDL 允许输出端口在例化时不连接（`open` 或省略），所以能编译通过。
5. 结论标注：**待本地验证**（若你有 Modelsim/Vsim 环境，可在 `sim` 目录 `source ./run.tcl` 确认回归仍绿，证明 `spi_tri` 悬空不影响现有功能）。

#### 4.1.5 小练习与答案

**练习 1**：在 `tb/top_tb.vhd` 里搜索 `spi_tri`，能找到吗？为什么找不到？这对回归测试意味着什么？

> **答案**：找不到（当前 HEAD 的 testbench 未连接 `spi_tri`）。因为 3-Wire 是刚加入的 DEVEL 特性，作者还没补对应的自检场景。这意味着现有回归**只覆盖 4 线模式**，`spi_tri` 的方向切换正确性目前**没有**被本仓库自测。

**练习 2**：`spi_tri` 在 `spi_simple` 里是「条件驱动」（受某 generic 控制）还是「无条件驱动」？依据是哪几行？

> **答案**：**无条件驱动**。依据：[spi_simple.vhd:76](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L76) 的 `SpiTri` 在端口表里没有条件，[spi_simple.vhd:297](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L297) 的 `spi_tri_o => SpiTri` 是无条件端口映射。核心根本不存在 `TriWiresSpi_g`，所以无从「条件」。

### 4.2 ReadBitPol / TriStatePol / SpiDataPos 配置

#### 4.2.1 概念说明

3-Wire 协议把「方向」与「读写意图」编码进了数据字本身。本 IP 用三个 generic 把这些约定参数化，好让同一份 RTL 适配不同从机芯片的习惯：

- **`ReadBitPol_g`**（`std_logic`，默认 `'1'`）：数据字里那位 R/W（读写命令）位，取什么电平表示「这是一次读操作」。不同芯片有的用 `1`=读、有的用 `0`=读，这个 generic 对齐极性。
- **`TriStatePol_g`**（`std_logic`，默认 `'1'`）：`spi_tri` 信号「让 IOBUF 驱动（输出 MOSI）」对应的有效电平。因为 Xilinx 的 `IOBUF` 其 `T` 是**高有效三态**（`T=1` 时输出高阻），而某些封装/电平转换场景可能要反一下，这个 generic 对齐三态方向。
- **`SpiDataPos_g`**（`positive`）：数据字中「有效数据」从第几位开始。3-Wire 协议常常先发一位 R/W 位（甚至若干命令位），再发真正的寄存器地址/数据；`SpiDataPos_g` 告诉引擎「跳过前面这些命令位之后，真正要移位的数据从哪里起算」。这就是 `package.tcl` 里注释 `"SPI data starting position in data word (necessary for 3-Wires SPI)"` 的含义。

注意：这三个 generic 的**真正消费者是外部引擎 `psi_common_spi_master`**。`spi_simple` 不解释它们，只搬运。所以「`ReadBitPol_g=1` 到底让哪一位在哪拍翻转」这种细节，要看 `psi_common_spi_master` 的实现（属 `psi_common` 库，不在本仓库）。

> 阅读陷阱（默认值不一致）：`SpiDataPos_g` 的默认值在两层不同——[spi_vivado_wrp.vhd:39](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L39) 是 `:= 8`，而 [spi_simple.vhd:40](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L40) 是 `:= 3`。由于 wrapper 实例化核心时把自己的 `SpiDataPos_g` 显式传下去（`SpiDataPos_g => SpiDataPos_g`），**经 IP 实际生效的是 wrapper 的 8**；核心里的 `3` 只在有人脱离 wrapper 直接例化 `spi_simple` 时才生效。看代码时要分清「我读的是哪一层」。

#### 4.2.2 核心流程

三个 generic 从 Vivado 用户到引擎的流向：

```text
Vivado GUI 下拉/输入
     │  (PARAM_VALUE)
     ▼
wrapper entity generic  (ReadBitPol_g / TriStatePol_g / SpiDataPos_g)
     │  generic map（同名透传）
     ▼
spi_simple entity generic  (同名)
     │  generic map（改名透传）
     ▼
psi_common_spi_master generic:
     read_bit_pol_g  <= ReadBitPol_g
     tri_state_pol_g <= TriStatePol_g
     spi_data_pos_g  <= SpiDataPos_g
     │
     ▼
引擎内部据这些值决定 R/W 位电平、spi_tri 翻转时机、数据移位起点
```

注意这条链上**没有改名损耗**（除核心→引擎那一段把 Pascal 风格名改成蛇形小写名），值原样到达消费者。

#### 4.2.3 源码精读

**wrapper 新增 generic 声明**：

[spi_vivado_wrp.vhd:35-39](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L35-L39) 集中声明了 3-Wire 相关的 5 个 generic：`TriWiresSpi_g`、`MosiIdleState_g`、`ReadBitPol_g`、`TriStatePol_g`、`SpiDataPos_g`。注意 `ReadBitPol_g/TriStatePol_g` 默认 `'1'`，`SpiDataPos_g` 默认 `8`。

**核心新增 generic 声明**：

[spi_simple.vhd:37-40](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L37-L40) 核心只声明了 3 个（`MosiIdleState_g/ReadBitPol_g/TriStatePol_g/SpiDataPos_g`），**没有 `TriWiresSpi_g`**——再次印证它只是 wrapper 级开关。

**wrapper → 核心 的 generic map**：

[spi_vivado_wrp.vhd:213-227](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L213-L227) 第 222–225 行把 `MosiIdleState_g/ReadBitPol_g/TriStatePol_g/SpiDataPos_g` 同名透传给 `spi_simple`（注意这里没有传 `TriWiresSpi_g`，因为它核心侧不存在）。

**核心 → 引擎 的 generic map（改名透传）**：

[spi_simple.vhd:271-284](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L271-L284) 第 280–283 行 `read_bit_pol_g => ReadBitPol_g`、`tri_state_pol_g => TriStatePol_g`、`spi_data_pos_g => SpiDataPos_g`、`mosi_idle_state_g => MosiIdleState_g`。这是三个 3-Wire generic 的最终落点。

**package.tcl 的 GUI 声明（参数化单一数据源）**：

[package.tcl:99-117](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L99-L117) 集中声明了 5 个新参数的控件：
- `MosiIdleState_g`（99–101，下拉 `{0 1}`）
- `ReadBitPol_g`（103–105，下拉 `{0 1}`）
- `TriWiresSpi_g`（107–109，**checkbox** 布尔开关）
- `TriStatePol_g`（111–113，下拉 `{0 1}`）
- `SpiDataPos_g`（115–117，**数值范围** `1 32`）

注意 `SpiDataPos_g` 在 GUI 用的是 `gui_parameter_set_range 1 32`（有范围约束），而 `ReadBitPol_g/TriStatePol_g` 用的是二值下拉。

**component.xml 里对应的 MODELPARAM 与 PARAM_VALUE（自动产物）**：

[component.xml:1266-1278](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L1266-L1278) 记录了 `ReadBitPol_g`（值 `"1"`）、`TriStatePol_g`（值 `"1"`）、`SpiDataPos_g`（值 `8`）作为 `MODELPARAM_VALUE`（喂给 VHDL generic 的实参）。

[component.xml:1539-1541](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L1539-L1541) 记录 `SpiDataPos_g` 作为 `PARAM_VALUE`（用户在 GUI 看到的值），`minimum=1 maximum=32`，默认 `8`——这与 `package.tcl` 的 `set_range 1 32` 对应。

#### 4.2.4 代码实践

**实践类型：源码阅读型实践。**

1. **实践目标**：核对三个 3-Wire generic 的「值链」一以贯之，没有中途改名导致失配。
2. **操作步骤**：
   - 在 `package.tcl` 找到三个参数的声明（[package.tcl:103-117](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L103-L117)），记下默认值（`ReadBitPol_g=1`、`TriStatePol_g=1`、`SpiDataPos_g` 范围 1..32）。
   - 在 `spi_vivado_wrp.vhd` 找 wrapper 的 generic 声明（[L35-L39](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L35-L39)）与 generic map（[L222-L225](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L222-L225)）。
   - 在 `spi_simple.vhd` 找核心的 generic 声明（[L37-L40](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L37-L40)）与到引擎的 generic map（[L280-L283](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L280-L283)）。
3. **需要观察的现象**：值链上 wrapper 与核心的 `SpiDataPos_g` 默认值不同（8 vs 3），但实际生效的是 wrapper 的 8。
4. **预期结果**：列出一张表：

   | 层 | `ReadBitPol_g` | `TriStatePol_g` | `SpiDataPos_g` 默认 |
   |----|----------------|-----------------|---------------------|
   | `package.tcl`（GUI） | 下拉，默认 `1` | 下拉，默认 `1` | 范围 1..32，默认 `8` |
   | `spi_vivado_wrp` | `:= '1'` | `:= '1'` | `:= 8` |
   | `spi_simple` | `:= '1'` | `:= '1'` | `:= 3` ⚠️ 与 wrapper 不一致 |
   | 引擎 generic 名 | `read_bit_pol_g` | `tri_state_pol_g` | `spi_data_pos_g` |

5. 结论标注：引擎内部如何使用这三个值的具体行为**待本地验证**（需读 `psi_common` 库的 `psi_common_spi_master.vhd`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ReadBitPol_g` 和 `TriStatePol_g` 用下拉 `{0 1}`，而 `SpiDataPos_g` 用范围 `1 32`？

> **答案**：前两者是**单比特电平极性**，只有 0/1 两种取值，用下拉避免用户填非法值；后者是**数据字里的位位置**，可取 1 到 32（受 `TransWidth_g` 上限约束），是数值而非枚举，所以用数值范围。

**练习 2**：如果你脱离 wrapper 直接例化 `spi_simple` 且不指定 `SpiDataPos_g`，生效值是多少？经 IP（wrapper）例化呢？

> **答案**：直接例化 `spi_simple` 不覆盖 → 用核心默认 `3`（[spi_simple.vhd:40](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L40)）；经 IP 例化 → wrapper 把自己的 `8` 透传下去（[spi_vivado_wrp.vhd:225](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L225)），覆盖核心默认，生效 `8`。

### 4.3 generic 与端口的端到端透传 + spi_tri 的条件综合

#### 4.3.1 概念说明

把前两节串起来：3-Wire 能力的「信号」（`spi_tri`）和「配置」（三个 generic）从 Vivado 用户一路透传到引擎，中间任何一层都不做解释。唯一在 wrapper 层「动脑子」的是 `TriWiresSpi_g`——它**不进入核心**，只用来回答一个问题：**`spi_tri` 这个端口要不要暴露在 IP 边界？**

这是 IP-XACT 的**端口条件使能（port enablement）**机制，与 RTL 的 `generate` 是两回事：

- **RTL 层**：`spi_tri` 在 `spi_simple` 与 `spi_vivado_wrp` 的 `port(...)` 里**无条件**存在，引擎**无条件**驱动它。哪怕 `TriWiresSpi_g=false`，内部这根线也在跑，只是没人用。
- **IP-XACT 层**：`component.xml` 给 `spi_tri` 标了 `PORT_ENABLEMENT`，依赖 `$TriWiresSpi_g = true`。当用户在 GUI 把 `TriWiresSpi_g` 设为 `false`（默认），Vivado 的 IP 打包器**不在 IP 边界生成这个端口**；OOC（Out-Of-Context，带边界约束的单独）综合时，综合器会把这根「内部驱动但边界不导出」的线优化掉。当 `TriWiresSpi_g=true`，端口才出现在 IP 外部接口上，用户才能把它接到自己的 IOBUF。

所以「`spi_tri` 只在 `TriWiresSpi_g=true` 时综合」这句话的准确含义是：**IP 边界端口 `spi_tri` 只在 `TriWiresSpi_g=true` 时被使能/导出**，而不是「RTL 里有段 `generate` 代码按条件生成三态逻辑」。这个区分对二次开发至关重要：你想让 3-Wire 行为可测，不能去 RTL 里找 `if TriWiresSpi_g`，因为根本没有；要么改 IP 打包配置，要么在 testbench 里直接挂 IOBUF。

设计上这样做的好处：**核心 RTL 与是否 3-Wire 完全解耦**。同一份 `spi_simple` 既能用于 4 线（不接 `spi_tri`），也能用于 3 线（接 `spi_tri` 到 IOBUF），开关只在 IP 边界可见性那一层。

#### 4.3.2 核心流程

端到端透传与条件综合的总览：

```text
[Vivado 用户在 GUI 勾选 TriWiresSpi_g = true]
        │
        ├─► PARAM_VALUE.TriWiresSpi_g = true   (component.xml)
        │         │
        │         └─► PORT_ENABLEMENT.spi_tri  依赖求值 → isEnabled = true
        │                   │
        │                   └─► IP 边界生成 spi_tri 端口（可连线到 IOBUF）
        │
        └─► PARAM_VALUE.ReadBitPol_g/TriStatePol_g/SpiDataPos_g
                    │ (MODELPARAM_VALUE 拷贝)
                    ▼
              wrapper generic  ──同名──►  spi_simple generic
                                                │ (改名)
                                                ▼
                                  psi_common_spi_master:
                                    read_bit_pol_g / tri_state_pol_g / spi_data_pos_g
                                                │
                                                ▼
                                  引擎计算 spi_tri_o 方向序列
                                                │
                                                ▼ (经 SpiTri => spi_tri 透传)
                                  IP 边界 spi_tri 输出（因 isEnabled=true 而导出）
```

当 `TriWiresSpi_g = false`（默认）时，右侧「IP 边界生成 spi_tri 端口」这步被跳过；左侧 generic 链仍在，但 `spi_tri` 无处可去，被综合器裁掉。

#### 4.3.3 源码精读

**`TriWiresSpi_g` 只在 wrapper**：

[spi_vivado_wrp.vhd:35](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L35) `TriWiresSpi_g : boolean := false`。通读整个 [wrapper 的 i_spi generic map（L213-L227）](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L213-L227)，**没有** `TriWiresSpi_g => ...`——它不被传给核心，是纯 wrapper 级开关。

**package.tcl 声明端口使能条件**：

[package.tcl:124](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L124) `add_port_enablement_condition "spi_tri" "\$TriWiresSpi_g = true"`。这是「`spi_tri` 仅在 3-Wire 使能时导出」的唯一设计意图来源。

**component.xml 落地为 PORT_ENABLEMENT**：

[component.xml:528-548](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L528-L548) 记录 `spi_tri` 端口（第 530 行 `<spirit:name>spi_tri</spirit:name>`，方向 `out`，类型 `std_logic`，带默认驱动值 `0`），并在第 547 行 `PORT_ENABLEMENT.spi_tri` 写明 `xilinx:dependency="$TriWiresSpi_g = true"`、默认 `isEnabled=false`。这就是 IP 打包器判断端口可见性的依据。

**component.xml 记录 TriWiresSpi_g 参数本身**：

[component.xml:1519-1521](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L1519-L1521) `PARAM_VALUE.TriWiresSpi_g` 默认 `false`（用户可改），[component.xml:1256-1258](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L1256-L1258) `MODELPARAM_VALUE.TriWiresSpi_g` 同样 `false`（喂给 wrapper generic）。

**版本号同步抬升**：

[package.tcl:17](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L17) `set IP_VERSION 1.4`，[component.xml:6](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L6) `<spirit:version>1.4</spirit:version>`，两边一致。这次 3-Wire 改动把 IP 从 1.3 抬到 1.4（同时 `xgui/spi_simple_v1_3.tcl` 被删、新增 `xgui/spi_simple_v1_4.tcl`）。

#### 4.3.4 代码实践（本讲的主实践）

**实践类型：源码阅读型实践（变更影响清单）。** 任务要求：结合最近提交 `3-Wires SPI interface signal added`，写一份变更影响清单，并说明 `spi_tri` 为何只在 `TriWiresSpi_g=true` 时综合。

1. **实践目标**：用 `git show` 复盘这次提交改了哪些文件/端口/generic，证明 3-Wire 是「wrapper+打包配置层」的改动，核心 RTL 只做了端口/generic 的透传性扩展。
2. **操作步骤**：
   - 运行 `git show fda4db7 --stat` 看改动文件清单。
   - 运行 `git show fda4db7 -- hdl/spi_vivado_wrp.vhd hdl/spi_simple.vhd scripts/package.tcl` 看 RTL 与打包脚本的具体 diff。
   - 运行 `git show fda4db7 -- component.xml | grep -n "spi_tri\|TriWiresSpi_g\|PORT_ENABLEMENT"` 看 IP-XACT 落点。
3. **需要观察的现象**：RTL 改动只有「加 generic 声明 + 加端口声明 + 加 generic/port map 行」，没有任何 `if generate` 条件逻辑；`package.tcl` 改动是「加 GUI 参数 + 加一行 `add_port_enablement_condition`」。
4. **预期结果——变更影响清单（参考答案）**：

   | 文件 | 改动内容 | 性质 |
   |------|----------|------|
   | `hdl/spi_vivado_wrp.vhd` | 新增 generic `TriWiresSpi_g/MosiIdleState_g/ReadBitPol_g/TriStatePol_g/SpiDataPos_g`；新增端口 `spi_tri`；`i_spi` 的 generic map 加 4 行（不含 `TriWiresSpi_g`）、port map 加 `SpiTri => spi_tri` | 透传性扩展 |
   | `hdl/spi_simple.vhd` | 新增 generic `MosiIdleState_g/ReadBitPol_g/TriStatePol_g/SpiDataPos_g`；新增端口 `SpiTri`；到引擎的 generic map 加 4 行（`read_bit_pol_g/tri_state_pol_g/spi_data_pos_g/mosi_idle_state_g`）、port map 加 `spi_tri_o => SpiTri` | 透传性扩展 |
   | `scripts/package.tcl` | IP 版本 1.3→1.4；加 5 个 GUI 参数声明；加 `add_port_enablement_condition "spi_tri" "$TriWiresSpi_g = true"` | 打包配置 |
   | `component.xml` | 版本 1.4；新增 `spi_tri` 端口及其 `PORT_ENABLEMENT.spi_tri`（依赖 `$TriWiresSpi_g=true`）；新增 5 个 `MODELPARAM`/`PARAM_VALUE`；logicalName 批量从 `spi_simple_1_3` 改成 `spi_simple_1_4` | 自动产物 |
   | `xgui/spi_simple_v1_3.tcl` → `spi_simple_v1_4.tcl` | 重命名+重生成 GUI 布局 | 自动产物 |
   | `doc/spi_simple.pdf/.docx` | 文档更新 | 文档 |
   | `drivers/...` | 仅 Makefile/tcl 微调（与本特性无直接关系） | 附带 |

   **为什么 `spi_tri` 只在 `TriWiresSpi_g=true` 时综合**：因为 `package.tcl` 用 `add_port_enablement_condition "spi_tri" "$TriWiresSpi_g = true"`（[package.tcl:124](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L124)）给该端口挂了条件使能，落到 `component.xml` 的 `PORT_ENABLEMENT.spi_tri`（[component.xml:547](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L547)，依赖 `$TriWiresSpi_g = true`）。`TriWiresSpi_g=false`（默认）时 IP 打包器不在边界导出 `spi_tri`，OOC 综合把内部这根悬空输出优化掉；`TriWiresSpi_g=true` 时端口才出现在 IP 外部接口，可接到片外 IOBUF。RTL 层（`spi_simple`/`spi_vivado_wrp`）始终无条件声明并驱动 `spi_tri`，**没有** `generate` 条件代码。

5. 结论标注：本清单基于 `git show` 静态分析得出；**待本地验证**的部分是「在 Vivado 里分别以 `TriWiresSpi_g=true/false` 打包 IP，观察端口是否如预期出现/消失」。

#### 4.3.5 小练习与答案

**练习 1**：有人想在 testbench 里直接验证 3-Wire 行为，去 `spi_simple.vhd` 找 `if TriWiresSpi_g generate ... end generate`，能找到吗？应该去哪里找「是否导出 spi_tri」的依据？

> **答案**：找不到。核心根本没 `TriWiresSpi_g`。端口导出的依据在打包配置：[package.tcl:124](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L124) 的 `add_port_enablement_condition` 与 [component.xml:547](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L547) 的 `PORT_ENABLEMENT.spi_tri`。在 testbench 里验证 3-Wire，应直接在 DUT 例化时把 `spi_tri` 接出来并挂一个行为级 IOBUF 模型，与 `TriWiresSpi_g` 无关（testbench 例化的是 wrapper 实体，端口表里 `spi_tri` 恒在）。

**练习 2**：为什么把「是否 3-Wire」做成 wrapper 级的端口使能开关，而不是在核心 RTL 里用 `generate` 条件实现？

> **答案**：解耦。核心 `spi_simple` 保持一份不变，既能跑 4 线（不接 `spi_tri`）也能跑 3 线（接 IOBUF），引擎始终算出 `spi_tri`，只是边界是否导出由 IP 打包决定。这样核心逻辑可复用、可单独仿真；「用不用 3 线」变成纯 IP 集成期的可见性问题，符合 IP-XACT 的端口使能语义，也避免在 RTL 里散布 `generate` 增加维护负担。

**练习 3**：`TriWiresSpi_g` 会不会作为 generic 传进 `spi_simple`？为什么？

> **答案**：不会。[wrapper 的 i_spi generic map（spi_vivado_wrp.vhd:213-227）](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L213-L227) 只传了 `MosiIdleState_g/ReadBitPol_g/TriStatePol_g/SpiDataPos_g`，没有 `TriWiresSpi_g`；而且 `spi_simple` 的 entity 根本没声明它。它只在 wrapper 存在，仅服务于 IP 端口使能。

## 5. 综合实践

**任务：为 3-Wire 模式补一条端到端的自检思路（设计级，不要求写完整代码）。**

结合本讲三个模块，请你完成一份「让 3-Wire 可被仿真验证」的设计草案，覆盖以下要点：

1. **顶层接线**：在 testbench 里把 DUT（`spi_vivado_wrp`）的 `spi_mosi`、`spi_miso`、`spi_tri` 通过一个行为级 IOBUF 模型合并成一根双向线 `sio`，写出该 IOBUF 模型如何用 `spi_tri`（注意 `TriStatePol_g` 极性）在「驱动 `spi_mosi`」与「高阻读 `spi_miso`」间切换。
2. **从机模型**：设计一个简化从机，在命令阶段采样 R/W 位（按 `ReadBitPol_g` 判读），若是读则在数据阶段驱动 `sio` 回送一个固定模式（如 `0xA5`），若是写则持续采样。
3. **数据对齐**：说明 `SpiDataPos_g` 在你的从机模型里如何体现——即从机应从数据字的第几位开始当作有效载荷。
4. **断言**：给出至少两条断言，例如「读事务的数据阶段 `spi_tri` 必须处于放手电平」「写事务全程 `spi_tri` 必须处于驱动电平」。
5. **局限说明**：指出本仓库当前 HEAD 还缺什么（提示：testbench 未连 `spi_tri`，引擎实现在外部库），所以哪些环节「待本地验证」。

完成后，把你草案里「读事务」的 `spi_tri` 切换时刻，与 [spi_simple.vhd:294-299](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L294-L299) 的 `spi_tri_o => SpiTri` 透传点对应起来——你要验证的时序，最终都来自外部 `psi_common_spi_master` 引擎经这一根线输出。

## 6. 本讲小结

- 3-Wire SPI 用一根双向数据线代替 `MOSI`+`MISO`，需要一根三态控制信号 `spi_tri` 指示「Master 驱动」还是「放手让从机驱动」；真正的物理合线（IOBUF）在 IP 之外，IP 只输出方向。
- `ReadBitPol_g`（R/W 位读极性）、`TriStatePol_g`（三态有效电平）、`SpiDataPos_g`（有效数据起始位）三个 generic 配置 3-Wire 协议约定，它们的真正消费者是外部引擎 `psi_common_spi_master`。
- `spi_simple` 与 `spi_vivado_wrp` 在 3-Wire 链路上**只做透传**：core 新增 `SpiTri` 端口与三个 generic 并改名传给引擎；wrapper 新增 `spi_tri` 端口与同名 generic 传给 core。
- `TriWiresSpi_g` 是**纯 wrapper 级**开关，不进入核心；它只通过 `add_port_enablement_condition`（→`component.xml` 的 `PORT_ENABLEMENT.spi_tri`）控制 `spi_tri` 是否在 IP 边界导出。
- 「`spi_tri` 只在 `TriWiresSpi_g=true` 时综合」是 **IP-XACT 端口使能**语义，不是 RTL `generate`；核心始终无条件驱动 `spi_tri`，边界不导出时被综合器裁掉。
- 阅读陷阱：wrapper 第 35 行的 `TriWiresSpi_g` 行尾注释是复制粘贴错误；`SpiDataPos_g` 默认值在 wrapper(8) 与 core(3) 不一致，经 IP 实际生效的是 8。

## 7. 下一步学习建议

- **横向承接**：本讲的 `spi_le`（Latch Enable）姊妹特性在 u3-l3「LE 锁存使能输出时序」详讲——`spi_le` 与 `spi_tri` 同属「每从机一根的辅助输出」，但 `spi_le` 无条件存在、`spi_tri` 条件使能，对比阅读能加深对端口使能机制的理解。
- **向上深挖引擎**：三个 3-Wire generic 的实际行为（R/W 位在哪拍发、`spi_tri` 在哪拍翻转、`SpiDataPos_g` 如何决定移位起点）都在 `psi_common_spi_master.vhd`（`psi_common` 库，本仓库外）。建议按 u1-l3 的依赖获取方式拉到 `psi_common`，精读其 3-Wire 实现。
- **打包流程**：若你想给本 IP 新增类似 `spi_tri` 的条件端口，依次阅读 u3-l4「IP 打包与发布流程」，掌握 `add_port_enablement_condition` 在 `package.tcl`/`component.xml`/`bd.tcl` 三处的联动。
- **回归补齐**：本特性尚处 DEVEL，`top_tb.vhd` 未覆盖 3-Wire；可参考 u2-l8「测试平台结构与 AXI/SPI 自检」的 `p_spi` 从机模型思路，尝试补一个最小 3-Wire 自检场景作为进阶练习。
