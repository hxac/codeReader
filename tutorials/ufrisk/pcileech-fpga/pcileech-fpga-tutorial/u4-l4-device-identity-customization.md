# 设备身份定制：VID/PID/DSN/Class Code

## 1. 本讲目标

本讲是「配置空间影子与 BAR 设备仿真」单元的收尾篇。前面几讲我们已经分别讲清楚了三件事：PCIe 配置空间是怎么被 `pcileech_pcie_cfg_a7` 代理的（u3-l2）、影子配置空间 `cfgspace_shadow` 是怎么用 BRAM 再造一份配置空间的（u4-l1）、BAR PIO 控制器是怎么响应内存读写请求的（u4-l2、u4-l3）。

本讲把这些零散的能力**汇总成一张「设备身份定制地图」**。学完本讲，你应当能够：

1. 说清楚一块 PCIe 设备的「身份」到底由哪些字段构成，以及每个字段分别藏在 pcileech-fpga 工程的**哪个文件、哪一层**。
2. 掌握四条互相不等价的定制路径：Vivado PCIe 核 GUI 改 IDs/Class、改 `cfg_dsn`、改 `cfgspace.coe`、（以及一条看似可行实则无效的「改 rw 寄存器 VID/PID」陷阱）。
3. 理解每条路径的**可见性差异**：哪些会直接反映到 `lspci` 输出，哪些不会，哪些需要重新生成 IP。
4. 认识 build.md 反复强调的安全含义——「改 ID 并不足以隐藏 DMA 设备」。

## 2. 前置知识

在动手之前，先用通俗语言把几个会反复出现的术语对齐：

- **PCIe 配置空间（Configuration Space）**：每块 PCIe 设备都有一段「登记表」，主机通过读这段表来认识设备。前 256 字节是基础头（Type 0），256 字节之后是扩展配置空间（Extended Config Space，共 4096 字节）。`lspci -x` 看基础头，`lspci -xxxx` 看 4096 字节全量。
- **VID / DID**：Vendor ID（厂商标识）与 Device ID（设备标识），位于配置空间偏移 0x00，是 `lspci` 显示的 `10ee:0666` 这一对数字的来源。
- **Subsystem VID / DID**：子系统厂商标识 / 子系统设备标识，位于偏移 0x2C，常用来区分同一芯片的不同板卡。
- **Class Code**：位于偏移 0x09–0x0B 的三字节「设备类别」，决定 `lspci` 把设备归类成「以太网控制器」「显示控制器」等。pcileech-fpga 默认伪装成 *Xilinx Ethernet Adapter* 就是 Class Code 的作用。
- **Revision ID**：偏移 0x08 的一字节版本号。
- **DSN（Device Serial Number）**：设备序列号，存放在扩展配置空间的 DSN 扩展能力（Extended Capability）里，是一个 64 位值，常被当作「设备指纹」。
- **BDF（Bus/Device/Function）**：主机枚举时**动态分配**给设备的总线号/设备号/功能号，与上面的「身份字段」无关——不要把 `lspci` 开头的 `03:00.0` 误当成 VID/DID。

一句话：**VID/DID/Subsys/Class/Revision 决定「我是谁」，DSN 是附加指纹，BDF 是「我被插在哪里」。** 本讲只关心前三类。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [PCIeSquirrel/build.md](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md) | 官方定制手册，列出 IDs/DSN/配置空间/BAR 四类定制步骤与限制 |
| [PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv) | 配置空间代理模块，`cfg_dsn` 在这里一行赋值并驱动硬核 |
| [PCIeSquirrel/src/pcileech_fifo.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv) | 控制中枢，含 `rw[203]`（cfgtlp_zero 开关）与「NOT IMPLEMENTED」的 VID/PID 占位字段 |
| [PCIeSquirrel/src/pcileech_tlps128_cfgspace_shadow.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_cfgspace_shadow.sv) | 影子配置空间模块，`cfgtlp_zero` 在这里决定读返回 0 还是 BRAM 内容 |
| [PCIeSquirrel/ip/pcileech_cfgspace.coe](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/ip/pcileech_cfgspace.coe) | 4KB 影子配置空间的初始内容 |
| [PCIeSquirrel/ip/bram_pcie_cfgspace.xci](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/ip/bram_pcie_cfgspace.xci) / [drom_pcie_cfgspace_writemask.xci](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/ip/drom_pcie_cfgspace_writemask.xci) | 把上面 `.coe` 绑定到 BRAM 与写掩码 DROM 的 IP 定义 |

> **承接说明**：`cfgspace_shadow` 的 CfgRd/CfgWr 解析、CplD 拼装细节已在 u4-l1 精读；`cfg_dsn` 的寄存器布局与 cfg_mgmt 机制已在 u3-l2 讲过。本讲**不重复**这些内部机制，只从「定制设备身份」的外部视角把它们串成一条工作流。

## 4. 核心概念与源码讲解

### 4.1 设备身份总览：四条定制路径与一层「占位陷阱」

#### 4.1.1 概念说明

很多人第一次想「改 PCIe 设备身份」时，会本能地去源码里搜 `0666`、`10ee`，然后改掉、重新编译，结果发现 `lspci` 毫无变化。这是因为 pcileech-fpga 的设备身份**分散在至少四个不同层级**，改错层级就是白改。

把全部定制路径列成一张表，本讲后续四个小节就是逐行展开它：

| 路径 | 改什么 | 在哪里改 | 是否要重生 IP | lspci 可见性 |
| --- | --- | --- | --- | --- |
| ① Vivado 核 GUI | VID/DID/Subsys/Class/Revision/BAR0 | PCIe 核 `pcie_7x_0` 的 IDs/BARs 标签页 | **是**（最权威） | 直接可见（基础头） |
| ② 改 `cfg_dsn` | 64 位设备序列号 | `pcileech_pcie_cfg_a7.sv` 一行 | 否 | 扩展配置空间可见 |
| ③ 改 `cfgspace.coe` | 影子配置空间内容 | `ip/pcileech_cfgspace.coe` + 翻 `rw[203]` | 否（仅需重综合） | `lspci -xxxx` 可见 |
| ④ 改 rw 寄存器 VID/PID | 看似改身份 | `pcileech_fifo.sv` `rw[128:199]` | 否 | **无效（NOT IMPLEMENTED）** |

第④条是本讲要重点戳破的陷阱：`pcileech_fifo.sv` 里确实有一段长得像「VID/DID/Subsys」的 `rw` 字段，但它们的注释写着 `NOT IMPLEMENTED`，**根本没有接到 PCIe 核**。改它们不会有任何效果。

#### 4.1.2 核心流程

设备身份在「主机 `lspci` → PCIe 枚举 → FPGA」这条链路上的产生流程：

1. 主机上电，PCIe 根复合体发起枚举，向设备发 **CfgRd** 读偏移 0x00。
2. FPGA 侧的 Xilinx PCIe **硬核** `pcie_7x_0` 自己持有一份配置空间（内容来自路径①GUI），直接用它的 VID/DID 回 CplD——**这是 `lspci` 默认看到的身份**。
3. 仅当硬核被配置成「把配置请求转发给用户」时，CfgRd/CfgWr 才会流到 `cfgspace_shadow`（路径③），此时影子 BRAM 的内容才有机会覆盖部分字段。
4. DSN 走另一条线：硬核的 `cfg_dsn` 输入端口由 `pcileech_pcie_cfg_a7.sv` 的 `rw[127:64]` 直接驱动（路径②）。
5. `lspci` 把上述结果汇总显示。

#### 4.1.3 源码精读

先看陷阱——`pcileech_fifo.sv` 里那段「看着像身份」的 `rw` 初始化：

```
rw[143:128] <= 16'h10EE;   // +010: CFG_SUBSYS_VEND_ID (NOT IMPLEMENTED)
rw[159:144] <= 16'h0007;   // +012: CFG_SUBSYS_ID      (NOT IMPLEMENTED)
rw[175:160] <= 16'h10EE;   // +014: CFG_VEND_ID        (NOT IMPLEMENTED)
rw[191:176] <= 16'h0666;   // +016: CFG_DEV_ID         (NOT IMPLEMENTED)
rw[199:192] <= 8'h02;      // +018: CFG_REV_ID         (NOT IMPLEMENTED)
```

详见 [pcileech_fifo.sv:282-286](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L282-L286)，每行末尾的 `(NOT IMPLEMENTED)` 是作者亲笔标注。这段字段虽然落在 `rw[207:128]` 区间（被整体搬运进 `_pcie_core_config`），但只有高 8 位 `rw[207:200]` 被翻译成真实输出信号（见 [pcileech_fifo.sv:310-318](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L310-L318)），低 72 位 `rw[199:128]`（即 VID/DID/Subsys/Rev）**不驱动任何端口**，纯属占位。换句话说，真正能改身份的不是这里。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认「rw 里的 VID/PID 是死字段」。
2. **步骤**：打开 `pcileech_fifo.sv`，定位 [第 310-322 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L310-L322)。
3. **观察**：列出 `_pcie_core_config[79:0]` 这一 80 位寄存器里，**到底有哪些位**被 `assign` 到了 `dpcie.*` 或 `dshadow2fifo.*`。
4. **预期结果**：只有 `[79:72]` 这 8 个控制位（复位、cfgtlp 开关、bar_en、drp 等）有去向；`[71:0]` 这 72 位（恰好覆盖 VID/DID/Subsys/Rev）没有任何 `assign` 消费它们——证明它们是死字段。
5. 待本地验证：用 Vivado 综合后查看这些位的 fanout（应为 0）。

#### 4.1.5 小练习与答案

- **Q1**：`lspci` 显示的 `03:00.0` 是 VID/DID 吗？
  - **A**：不是，那是 BDF（Bus:Device.Function），由主机枚举时动态分配。VID/DID 是后面方括号里的 `[10ee:0666]`。
- **Q2**：把 `pcileech_fifo.sv` 里 `rw[191:176] <= 16'h0666` 改成别的值，`lspci` 会变吗？
  - **A**：不会。该字段标注 `(NOT IMPLEMENTED)`，没有连到 PCIe 核，改了也只是改了一段没人读的寄存器。

### 4.2 路径①：Vivado PCIe 核 GUI 改 IDs / Class Code

#### 4.2.1 概念说明

VID/DID/Subsys/Class/Revision 这些「最硬」的身份字段，**只认 Xilinx PCIe 硬核 `pcie_7x_0`**。它们在 IP 内部被固化进配置空间，主机 CfgRd 偏移 0x00/0x2C/0x09 时，硬核直接用自己的值回答。要改它们，必须在 Vivado 的 PCIe 核 GUI 里改，然后**重新生成 IP**——这是四条路径里唯一需要重生 IP 的，也正因为如此它最「贵」但也最权威。

#### 4.2.2 核心流程

build.md 给出的官方步骤（[build.md:24-31](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md#L24-L31)）：

1. 先按构建流程的第 1–4 步生成初始工程（即运行 `vivado_generate_project.tcl`）。
2. 在生成的 `pcileech_squirrel` 子目录里双击 `pcileech_squirrel.xpr` 打开 Vivado。
3. 在 PROJECT MANAGER 展开层级 `pcileech_squirrel_top → i_pcileech_pcie_a7`。
4. 双击 `i_pcie_7x_0` 打开 PCIe 核设计器 GUI。
5. 进 **IDs** 标签页，改 ID Initial Values 与 Class Code。
6. （可选）进 **BARs** 标签页，改 BAR0（默认 4KB，不建议再小）。
7. 点 OK → Generate 重建 IP。
8. 退出 Vivado，从构建流程第 5 步继续（`vivado_build.tcl`）。

#### 4.2.3 源码精读

注意第 4 步打开的对象是 `i_pcie_7x_0`——它就是 u3-l1 讲过的 PCIe 硬核封装 `pcileech_pcie_a7` 内部例化的那个 IP。GUI 里改的 IDs 值，最终被 Xilinx 工具写进 `PCIeSquirrel/ip/pcie_7x_0.xci` 这个 IP 定义文件（不在 `.sv` 源码里，所以光看 SystemVerilog 看不到 VID/DID）。

build.md 在这一节开头有两段重要警告（[build.md:18-22](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md#L18-L22)）：

- 很多 VID/DID/Class 组合会让目标机**无法启动、卡死或行为异常**，遇到就换一组值再试。
- **「只改 VID/DID 并不足以让设备对查 DMA 的软件隐身」**——PCIe 配置向导里还有其他设置会改变配置空间，这点在 4.5 节展开。

另外，默认身份在 [build.md:16](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md#L16) 一句话点明：设备默认显示为 *Xilinx Ethernet Adapter*，Device ID `0x0666`。

#### 4.2.4 代码实践（操作型，需 Vivado 环境）

1. **目标**：把默认 `10ee:0666` 改成一个不会让目标机挂起的自定义组合，并确认 Class Code 的作用。
2. **步骤**：按 4.2.2 的 8 步操作；在 IDs 标签页把 Vendor ID 设为 `10ee`、Device ID 设为一个不常见值（如 `0x1234`）；把 Class Code 暂时改成「网络控制器」之外（例如 `0x0c0330`，USB 主机控制器）观察 `lspci` 的分类变化。
3. **观察**：重建 IP → 综合 → 烧录后，在目标机执行 `lspci -nn -d 10ee:1234`。
4. **预期结果**：能看到一行设备，`lspci` 的类别描述会随 Class Code 改变（如变成 USB controller）。
5. 待本地验证：本步骤依赖真实 FPGA 板卡与目标机，无硬件时只能停留在 GUI 操作层面。

#### 4.2.5 小练习与答案

- **Q1**：为什么改 IDs 必须重新 Generate IP，而改 DSN 不用？
  - **A**：VID/DID 被固化在 PCIe 硬核 IP 内部，属于 IP 参数，改参数要重生 IP；DSN 是硬核的一个**输入端口** `cfg_dsn`，由外部 `.sv` 一根线驱动，改那根线即可，不动 IP。
- **Q2**：build.md 说 BAR0 不建议低于 4KB，为什么？
  - **A**：BAR PIO 控制器（u4-l2）的读引擎按固定节拍拼装 CplD，4KB 是示例实现（如 `zerowrite4k`）所依赖的最小尺寸；过小可能破坏示例实现的读延迟约定。

### 4.3 路径②：`cfg_dsn`——一行 HDL 改设备序列号

#### 4.3.1 概念说明

DSN 是四条路径里**性价比最高**的指纹定制手段：不用重生 IP，不用碰 GUI，只在 `pcileech_pcie_cfg_a7.sv` 改一个 64 位常量、重新综合烧录即可。build.md 把它单独列为一节（[build.md:34-39](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md#L34-L39)），正是因为它廉价又有「换指纹」的价值。

#### 4.3.2 核心流程

DSN 的数据通路非常短：

1. 上电时，`pcileech_pcie_cfg_a7` 的初始化 task 把一个 64 位常量写进 `rw[127:64]`。
2. 一条 `assign` 把 `rw[127:64]` 直接接到硬核输入端口 `ctx.cfg_dsn`。
3. Xilinx 硬核把这个值塞进扩展配置空间的 DSN 扩展能力里，供主机读取。

整条链路里**没有任何 IP 参数参与**，所以无需重生 IP。

#### 4.3.3 源码精读

初始化赋值（[pcileech_pcie_cfg_a7.sv:215](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L215)）：

```verilog
rw[127:64]  <= 64'h0000000101000A35;    // +008: cfg_dsn
```

驱动硬核端口（[pcileech_pcie_cfg_a7.sv:265](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L265)）：

```verilog
assign ctx.cfg_dsn  = rw[127:64];
```

build.md 给出的就是这两行里的常量（[build.md:38](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md#L38)）。改 `64'h0000000101000A35` 为任意 64 位值即可换 DSN。

> **避免混淆**：本模块还有一个 16 位输出 `pcie_id`（[pcileech_pcie_cfg_a7.sv:315](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L315)），它取自 `ro[79:64]`，内容是**主机分配的 BDF**，不是 VID/DID，也不是 DSN——它的用途是在 `cfgspace_shadow` 拼装 CplD 时填「完成者 ID（Completer ID）」（见 u4-l1）。

#### 4.3.4 代码实践（源码修改型）

1. **目标**：把默认 DSN 改成一个你能一眼认出的值。
2. **步骤**：把 [第 215 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L215) 的 `64'h0000000101000A35` 改成例如 `64'hDEADBEEFCAFEBABE`，重新 `source vivado_build.tcl`。
3. **观察**：烧录后在目标机执行 `lspci -xxxx -d 10ee:0666`，在 4096 字节扩展转储里找到 DSN 扩展能力（能力 ID `0x03`，其后跟 8 字节序列号）。
4. **预期结果**：序列号字段变成你写入的字节（注意字节序，PCIe 配置空间是小端）。
5. 待本地验证：扩展能力偏移随硬核版本而变，需在真实 `lspci -xxxx` 输出里按能力链查找。

#### 4.3.5 小练习与答案

- **Q1**：为什么 DSN 比 VID/PID 更适合做「指纹」？
  - **A**：DSN 改一行 HDL 即可，成本低；且许多取证/检测软件会把 DSN 当作设备唯一标识做比对。
- **Q2**：改 DSN 需要重新生成 `pcie_7x_0` IP 吗？
  - **A**：不需要。`cfg_dsn` 是硬核的运行时输入端口，由 `.sv` 驱动，不属于 IP 参数。

### 4.4 路径③：`cfgspace.coe`——自定义影子配置空间内容

#### 4.4.1 概念说明

路径①②改的都是硬核持有的字段。路径③走的是 u4-l1 讲过的**影子配置空间**：用一片 4KB BRAM 再造一份配置空间，当硬核把 CfgRd 转发给用户时，由 `cfgspace_shadow` 用 BRAM 内容回答。BRAM 的初始内容来自 `ip/pcileech_cfgspace.coe`，而「读时返回 BRAM 内容还是返回 0」由 `rw[203]`（cfgtlp_zero）这一个开关决定。

注意 build.md 的提醒（[build.md:44](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md#L44)）：**「Xilinx PCIe core will in-part override user-configured values」**——影子并不能完全覆盖硬核的字段，硬核对关键字段（如 VID/DID）有优先权，所以路径③主要用来补充/伪装配置空间的「次要内容」，而不是顶替路径①。

#### 4.4.2 核心流程

启用自定义影子配置空间的完整流程：

1. 把 `pcileech_fifo.sv` 的 `rw[203]` 从 `1'b1` 改成 `1'b0`（[build.md:46-53](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md#L46-L53)）。
2. 编辑 `ip/pcileech_cfgspace.coe` 填入想要的 4KB 内容。
3. 重新综合烧录。
4. 在 Linux 用 `lspci -d 10ee:0666 -xxxx` 验证（[build.md:55-57](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md#L55-L57)）。

`rw[203]` 的位映射：`rw[207:128]` 整段被搬进 `_pcie_core_config`（[pcileech_fifo.sv:310](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L310)），其中

\[ \text{cfgtlp\_zero} = \_pcie\_core\_config[75] = rw[203+75-72] \]

即 `rw[203]`（见 [pcileech_fifo.sv:314](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L314)）。默认 `rw[203] <= 1'b1`（[pcileech_fifo.sv:290](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L290)）。

#### 4.4.3 源码精读

`cfgtlp_zero` 在影子模块里只起一个作用——决定 CplD 里回送的是 BRAM 真值还是全零（[pcileech_tlps128_cfgspace_shadow.sv:84](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_cfgspace_shadow.sv#L84)）：

```verilog
wire [31:0] bram_rd_data_z = dshadow2fifo.cfgtlp_zero ? 32'h00000000 : bram_rd_data;
```

- `cfgtlp_zero = 1`（默认）：读永远回 0 → 影子「不可见」，`lspci` 看到的是硬核原生配置空间。
- `cfgtlp_zero = 0`：读回 BRAM 真值 → 影子生效，`.coe` 内容显现。

BRAM 本体由 `pcileech_mem_wrap` 例化，其初始化文件绑定关系在 IP 定义里（[bram_pcie_cfgspace.xci:54](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/ip/bram_pcie_cfgspace.xci#L54) 指向 `pcileech_cfgspace.coe`），代码侧的例化见 [pcileech_tlps128_cfgspace_shadow.sv:237-245](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_cfgspace_shadow.sv#L237-L245)。BRAM 的写入还受一片写掩码 DROM 约束（[pcileech_tlps128_cfgspace_shadow.sv:248-258](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_cfgspace_shadow.sv#L248-L258)）：掩码位为 0 时该 bit 只读，写入时保留原值 `rd_data[i]`；掩码位为 1 时才允许新值 `wr_data_d[i]` 写入。

`.coe` 文件本体是一个自描述的占位模式（[pcileech_cfgspace.coe:1-4](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/ip/pcileech_cfgspace.coe#L1-L4)）：

```
memory_initialization_radix=16;
memory_initialization_vector=
fffff000,fffff004,fffff008,fffff00c,
```

可以看到，第 N 个 DWORD 的值恰好是 `0xFFFFF000 + 4*N`，即「值等于 `0xFFFFF000 + 自身字节偏移`」。这是一个一眼可辨的递增测试图案：一旦 `lspci -xxxx` 里看到这种整齐递增的 `fffff000 / fffff004 / …`，就说明影子已生效。

> **关于 PCIe 写影子**：默认 `rw[206]`（cfgtlp_wren）为 0（[pcileech_fifo.sv:293](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L293)），意味着主机从 PCIe 侧**只能读影子、不能写影子**（写使能被屏蔽成 0，见 [pcileech_tlps128_cfgspace_shadow.sv:67](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_cfgspace_shadow.sv#L67)）；要改影子内容只能走 USB→`IfShadow2Fifo` 那条路（u4-l1）。

#### 4.4.4 代码实践（源码修改 + 验证型）

1. **目标**：启用影子，并用 `lspci` 看到 `.coe` 内容。
2. **步骤**：
   - 把 [pcileech_fifo.sv:290](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L290) 的 `rw[203] <= 1'b1` 改成 `rw[203] <= 1'b0`。
   - （可选）在 `pcileech_cfgspace.coe` 第一个 DWORD `fffff000` 处改成 `deadbeef` 做标记。
   - 重新综合烧录。
3. **观察**：在目标机执行 `lspci -d 10ee:0666 -xxxx`。
4. **预期结果**：扩展转储的相应位置出现 `fffff000 / fffff004 / …` 递增图案（或你写入的 `deadbeef`），而改之前这些位置读回 0。
5. 待本地验证：取决于硬核是否把对应配置请求转发给用户；build.md 已提示「Xilinx PCIe core will in-part override」。

#### 4.4.5 小练习与答案

- **Q1**：为什么把 `rw[203]` 从 1 改成 0 就能让 `.coe` 显现？
  - **A**：`rw[203]` 即 `cfgtlp_zero`。=1 时 CplD 数据被强制清零（影子不可见）；=0 时回送 BRAM 真值（`.coe` 内容显现）。
- **Q2**：能否通过 `lspci` 的写操作（如 `setpci`）改影子内容？
  - **A**：默认不能。`cfgtlp_wren`（`rw[206]`）默认 0，PCIe 侧的 CfgWr 写使能被屏蔽，只能读。要在线改影子须走 USB 命令路径。

### 4.5 安全视角：「改 ID ≠ 隐身」

#### 4.5.1 概念说明

build.md 在 IDs 小节末尾有一句容易被略过、但极其重要的安全声明（[build.md:22](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md#L22)）：**「changing the device and vendor ID is not in itself sufficient to make the device 'undetectable' by software looking for malicious DMA devices.」**

这背后的道理是：检测恶意 DMA 设备的软件并不只看 VID/DID。它们还会检查配置空间里大量「行为特征」字段——而这些字段散落在 PCIe 核 GUI 的多个标签页、影子配置空间、BAR PIO 行为里。改一个 VID 顶多骗过最粗粒度的黑名单。

#### 4.5.2 核心流程

一台「正常设备」与一块「改了 ID 的 DMA 板」的差异，通常体现在：

1. **配置空间完整性**：影子只能部分覆盖，硬核会「in-part override」（[build.md:44](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md#L44)），导致某些字段组合「不像真卡」。
2. **能力链（Capability List）**：真卡有一套与 Class Code 匹配的能力（如 MSI/MSI-X、PM、Device Serial Number），改了 ID 但能力链对不上会暴露。
3. **BAR 行为**：u4-l2/u4-l3 的示例 BAR（`zerowrite4k`/`loopaddr`）行为简陋，读延迟固定，与真设备驱动期望不符。
4. **DSN 与其他指纹**：DSN、LTSSM 行为、链路速率协商等都是可观测的次级特征。

#### 4.5.3 源码精读

本节没有新的代码点，而是把前面四节的代码事实重新组织成一个结论：

- 路径①改的字段最「像」，但 build.md 明确说不保证隐身（[build.md:22](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md#L22)）。
- 路径②的 DSN 是双刃剑：它既是廉价指纹，也是检测软件常查的特征。
- 路径③的影子受硬核「部分覆盖」限制，留下不一致痕迹。
- 路径④的 rw VID/PID 根本是死字段（[pcileech_fifo.sv:282-286](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L282-L286)），指望它隐身是徒劳。

本讲的定位是**教育与防御研究**：理解这些字段在哪、怎么改、为什么改不干净，才能反过来写出更可靠的 DMA 设备检测规则。

#### 4.5.4 代码实践（分析型）

1. **目标**：列出一份「改 ID 后仍可能暴露的特征清单」。
2. **步骤**：以本讲四条路径为线索，逐一指出每条路径留下的「不一致痕迹」。
3. **观察**：结合 `lspci -vvv -xxxx` 输出，核对能力链、DSN、BAR 大小、LTSSM 状态（LTSSM 信号在 `pcileech_pcie_cfg_a7.sv` 的 `ro` 寄存器 [第 85 行附近](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L85) 一一映射，详见 u3-l2 与 u6-l3）。
4. **预期结果**：得到一张「特征—来源—是否易被改」的三列表。
5. 待本地验证：无硬件时可先据源码与 build.md 推演。

#### 4.5.5 小练习与答案

- **Q1**：把 VID/DID 改成某知名网卡芯片的值，是否就能骗过 IOMMU/检测软件？
  - **A**：不能保证。检测软件还会看能力链、BAR 行为、DSN、链路协商等次级特征，单一字段改写不足以全面伪装。
- **Q2**：本讲内容用于什么场景是正当的？
  - **A**：用于防御研究、设备指纹分析、取证与学习 PCIe 协议；用这些知识去规避目标系统的安全检测则属恶意用途，不在本讲倡导范围内。

## 5. 综合实践

**任务**：按 build.md 步骤，规划一次「把默认 `10ee:0666` 改为自定义 VID/PID 并同步修改 DSN」的完整操作，并指出每个改动分别影响 `lspci` 输出的哪一部分。本练习是「规划型」，不要求立刻有硬件——重点是把你对四条路径的理解落成一张可执行的清单。

**操作清单（请按顺序填写每一步对应的文件/动作）**：

| 步骤 | 动作 | 涉及文件 / 位置 | 影响的 `lspci` 输出 |
| --- | --- | --- | --- |
| 1 | 生成初始工程 | 运行 `vivado_generate_project.tcl` | 无（仅建工程） |
| 2 | 打开 PCIe 核 GUI，改 Vendor/Device ID | `i_pcie_7x_0` → IDs 标签页 | `lspci -nn` 行尾的 `[VVVV:DDDD]`、`lspci -x` 偏移 0x00 |
| 3 | 改 Subsystem VID/ID（如需） | IDs 标签页 | `lspci -x` 偏移 0x2C–0x2F |
| 4 | 改 Class Code（如需改类别） | IDs 标签页 | `lspci` 的设备类别描述、偏移 0x09–0x0B |
| 5 | Generate IP、保存 | `pcie_7x_0.xci` | — |
| 6 | 改 DSN | [pcileech_pcie_cfg_a7.sv:215](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L215) 的 64 位常量 | `lspci -xxxx` 扩展能力里的 DSN 字段 |
| 7 | （可选）启用影子配置空间 | [pcileech_fifo.sv:290](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L290) `rw[203]` 改 0；编辑 `pcileech_cfgspace.coe` | `lspci -xxxx` 中影子覆盖的部分（受硬核部分覆盖限制） |
| 8 | 构建并烧录 | 运行 `vivado_build.tcl`；按设备方式烧录（PCIeSquirrel 用 OpenOCD） | — |
| 9 | 验证 | 目标机执行 `lspci -xxxx -d <新VID>:<新DID>` | 确认上述各字段 |

**需要你回答的检查点**：

1. 为什么步骤 2/3/4 必须重生 IP，而步骤 6 不用？（提示：硬核内部固化 vs. 运行时输入端口）
2. 步骤 7 的影子内容，`lspci` 在偏移 0x00（VID/DID）处能看到 `.coe` 写的值吗？为什么？（提示：硬核 in-part override）
3. 如果有人提议「顺便把 `pcileech_fifo.sv` 里 `rw[191:176]`（CFG_DEV_ID）也改一下」，你该如何基于 [pcileech_fifo.sv:282-286](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L282-L286) 劝阻他？

## 6. 本讲小结

- PCIe 设备身份散落在**四个层级**：Vivado 核 GUI（VID/DID/Subsys/Class/Rev/BAR0）、`cfg_dsn`（一行 HDL）、影子配置空间（`.coe` + `rw[203]`）、以及一段**无效的** rw 占位字段。
- `pcileech_fifo.sv` 里 `rw[128:199]` 那段 VID/DID/Subsys/Rev 字段标注 `(NOT IMPLEMENTED)`，只占位不接线，改了毫无效果——这是最常见的「改了没反应」陷阱。
- VID/DID/Class 改动需在 PCIe 核 GUI 操作并重生 IP，是最权威也最贵的路径；DSN 改 `rw[127:64]` 一行即可，是最廉价的指纹定制。
- 影子配置空间由 `cfgtlp_zero`（`rw[203]`）开关：默认 `=1` 读回 0（不可见），改成 `0` 才显现 `.coe` 内容，用 `lspci -xxxx` 验证；且硬核会「in-part override」，影子不能完全顶替硬核字段。
- PCIe 侧默认对影子只读（`cfgtlp_wren`=`rw[206]`=0），在线改影子只能走 USB→`IfShadow2Fifo`。
- **改 ID 并不足以让设备对 DMA 检测隐身**：能力链、BAR 行为、DSN、链路状态等次级特征都会暴露不一致。

## 7. 下一步学习建议

- 想把「设备仿真」做到更逼真，继续精读 **u4-l2 / u4-l3**（BAR PIO 控制器与示例实现），把 BAR 行为也纳入身份伪装的考量。
- 想理解 `lspci` 看到的链路状态字段（LTSSM、链路宽度、速率）从哪来，进入 **u6-l3（LTSSM、链路状态、性能与调试）**，那里会逐位讲解 `pcileech_pcie_cfg_a7.sv` 的 `ro` 寄存器映射。
- 若要把整套定制流程落到一块**新板卡**上，参考 **u6-l2（移植到新板卡）**，注意移植时 IDs 仍在 PCIe 核 GUI、DSN 仍在那行 HDL，这套「身份地图」跨设备通用。
- 建议同时翻阅 [PCIeSquirrel/build.md](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md) 原文与本讲对照，确认每条定制建议都能在官方文档里找到出处。
