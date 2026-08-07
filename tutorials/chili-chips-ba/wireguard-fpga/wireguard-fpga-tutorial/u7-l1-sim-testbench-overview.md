# 仿真测试台总体架构

## 1. 本讲目标

本讲是 Unit 7「仿真协同验证」的开篇。前面几单元讲的都是「设计本身」——RTL 数据面、CSR、软件控制面。但从这一讲开始，我们要回答一个不同的问题：**在没有真实 FPGA 板卡的情况下，怎么验证这一整套设计是对的？**

学完本讲，你应当能够：

1. 说清 `4.sim` 测试台（testbench）的整体结构——被测设计 `top` 周围接了哪些「外部世界」的模型（BFM）。
2. 理解「可替换 `soc_cpu`」策略：仿真时如何用虚拟处理器 VProc 顶替真实的 picoRV32 RTL 软核，以及这套替换是怎么靠处理 filelist 自动完成的。
3. 读懂 `MakefileVProc.mk` 的主要变量与目标，知道一条 `make` 命令背后发生了什么：用户代码编译、HDL 用 Verilator 编译、链接成可执行仿真程序。
4. 认识 Verilator + VProc「协同仿真（co-simulation）」的基本思路——HDL 跑硬件、C++ 跑「软件激励与检查」，二者通过 DPI-C 接口握手。

本讲只讲「总体架构」。VProc 的 C++ API 细节留到 u7-l2，rv32 指令集模拟器留到 u7-l3，以太网 VIP 留到 u7-l4，逐模块单元测试台留到 u7-l5。

## 2. 前置知识

在进入仿真之前，先建立几个概念。它们大多在前面单元已经讲过，这里只做回顾与补全。

- **测试台（testbench, TB）**：一段不打算被综合成真实电路的 HDL 代码，专门用来「驱动 + 观察」被测设计（Design Under Test, DUT）。它产生时钟和复位、给 DUT 喂激励、检查 DUT 的输出。本项目的 DUT 就是 u2-l2 讲过的 `top.sv`。
- **总线功能模型（Bus Functional Model, BFM）**：真实板卡上 DUT 外面接的是 PHY 芯片、PC 串口、按键。仿真里没有这些物理器件，就用 BFM 来「扮演」它们——按协议时序产生信号、并解析 DUT 发来的信号。本项目里有 `bfm_ethernet`（扮演 4 个以太网 PHY）、`bfm_uart`（扮演 PC 串口）、`bfm_phy_mdio`（扮演 MDIO 从机）。
- **Verilator**：一个开源的「把 Verilog/SystemVerilog 翻译成 C++ 再用 g++ 编译」的仿真器。它不像商业仿真器（如 Vivado 自带的仿真器）那样逐事件调度，而是把整个设计编译成一个跑得很快的 C++ 程序。本项目用它做周期级（cycle-accurate）仿真。
- **协同仿真（co-simulation）**：纯 HDL 仿真里，连「跑在 CPU 上的软件」也得用 RTL 软核逐拍模拟，慢。协同仿真的思路是：硬件仍用 HDL 仿真，但 CPU 用一个「虚拟处理器」代替——它本质是 C++ 程序，用 DPI-C 接口与 HDL 侧通信，从而大幅提速。
- **VProc**：[wyvernSemi/vproc](https://github.com/wyvernSemi/vproc) 提供的虚拟处理器 IP。它有一组通用内存映射总线端口，外加 DPI-C 的 C/C++ API（`write`/`read`/`tick`），让 C++ 代码能像 CPU 一样驱动 HDL 总线。本项目把 VProc 包成 `soc_cpu.VPROC`，与真实 picoRV32 RTL 引脚兼容。
- **soc_if 总线**：u2-l4 讲过的控制面 `vld/rdy` 总线，CPU 是唯一主机。仿真里 VProc 就是通过这条总线访问 DMEM 与 CSR 的。
- **DPI-C**：SystemVerilog 的「直接编程接口」，让 HDL 与 C 函数互相调用。VProc 和 mem_model 的全部跨语言通信都走它。

一个关键心智模型：**测试台是真实板卡的「数字孪生」**。板卡上 `top` 芯片外面有什么（晶振、PHY、串口、按键），测试台里就有什么 BFM 去扮演它。理解了这一点，下面的源码读起来就很自然。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `4.sim/tb.sv` | 测试台顶层模块。生成时钟/复位，例化 DUT `top`，例化 `bfm_ethernet`/`bfm_uart`/`verilator_sim_ctrl`，是整个仿真的「接线板」。 |
| `4.sim/tb.filelist` | 列出构成测试台模块（含各 BFM 与 `soc_cpu.VPROC.sv`）的源文件清单，喂给 Verilator。 |
| `4.sim/top.filelist` | 由 Makefile 从 `1.hw/top.filelist` 自动生成的本地副本，**删掉了 `ip.cpu` 下的真实 CPU RTL**，只保留 DUT 的其余部分。 |
| `1.hw/top.filelist` | 真实综合用的源文件清单，包含 `ip.cpu/imem.sv`、`soc_cpu.PICORV32.sv` 等。仿真时被 Makefile 过滤。 |
| `4.sim/models/soc_cpu.VPROC.sv` | VProc 版 `soc_cpu` 包装器。把 VProc 通用总线翻译成 `soc_if`，并接一个 `mem_model` 当 IMEM。 |
| `4.sim/MakefileVProc.mk` | 协同仿真的编译/运行编排：编用户代码、调 Verilator 编 HDL、链接库、跑仿真。 |
| `4.sim/models/README.md` | 各 BFM 模型（`bfm_ethernet`/`bfm_phy_mdio`/`soc_cpu.VPROC`）的说明。 |
| `4.sim/README.md` | 仿真子系统的总文档，含工具版本、用法示例、协同仿真 HAL 说明。 |

## 4. 核心概念与源码讲解

### 4.1 测试台的整体结构

#### 4.1.1 概念说明

`4.sim` 的测试台采用经典的「DUT + 激励/监测」结构，但比一般教学例子里「给个输入、`$display` 个输出」要复杂得多——因为它要忠实扮演整块板卡。核心角色有三个：

1. **DUT**：`top`，即 u2-l2 讲过的 SoC 顶层。仿真里它就是被测对象，端口和真实上板时完全一样（差分时钟、复位、4 路 GMII 以太网、UART、按键、LED）。
2. **外部世界模型（BFM）**：扮演 `top` 在板卡上连接的外部器件。`bfm_ethernet` 扮演 4 个 PHY（收发 GMII 包 + 响应 MDIO），`bfm_uart` 扮演 PC 串口（收发字符）。
3. **时钟/复位源**：真实板卡有 200 MHz 晶振和复位电路；测试台用 `initial` 块里的 `forever` 翻转来「造」出这些时钟。

此外还有一个特殊角色 `verilator_sim_ctrl`（节点 15），它是一个跑在仿真内部的交互式 CLI，允许你在运行中用 `run for 100 ns` 这类命令控制仿真进度——这是本项目为了方便 GTKWave 看波形加的小工具。

#### 4.1.2 核心流程

一次仿真的生命周期可以这样描述：

```text
initial 块启动（t=0）
  ├─ fork 出多条并行线程：
  │    ├─ board_rst_n：等几个周期后拉高 rst_n（异步复位释放）
  │    ├─ clk_p_gen  ：forever 翻转 clk_p（200MHz） → clk_n 取反
  │    ├─ gmii_clock_gen ：forever 翻转 gmiiclk（125MHz）
  │    ├─ eth_txc_gen：forever 翻转 eth_txc（25MHz）
  │    ├─ clk_80_gen ：forever 翻转 clk_80（80MHz，给 BFM 用）
  │    ├─ gmii_arst_n：等若干周期后拉高 gmiiarst_n
  │    └─ run_sim    ：等 RUN_SIM_US 微秒后 $finish 结束仿真
  └─ join（这些线程任一结束才结束，实际上由 run_sim 的 $finish 收尾）

与此同时（一直在跑）：
  DUT top  ←时钟/复位←  tb
        ←GMII/MDIO→  bfm_ethernet（4 个 PHY，由 udpIpPg 的 VUserMain1..4 驱动）
        ←UART→       bfm_uart（串口回环）
  verilator_sim_ctrl（node 15）监听标准输入，按需 flush 波形
```

注意 `fork...join`：所有线程必须全部结束 `join` 才会继续，但其中 `forever` 线程永不退出，所以真正终结仿真的是 `run_sim` 线程里的 [`$finish(2)`](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/tb.sv#L121-L124)（`(2)` 表示打印结束时的仿真时间与内存统计）。

#### 4.1.3 源码精读

**参数与时钟周期**。`tb` 模块用一组参数控制仿真规模与外设地址，并把各时钟的「半周期」写成 localparam，方便换算频率：

- [4.sim/tb.sv:45-50](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/tb.sv#L45-L50)：`tb` 的参数。`RUN_SIM_US` 是仿真总时长（微秒），由 Makefile 的 `TIMEOUTUS` 经 Verilator 的 `-G` 参数传入；`ETH_START_NODE=1` 是以太网 BFM 的起始节点号；`DISABLE_SIM_CTRL` 控制是否启用交互式 CLI。

  换算关系：`HALF_CLK_P_PERIOD_PS=2500` → 周期 5000ps = 5ns → **200 MHz**（板卡晶振）；`HALF_GMII_PERIOD_PS=4000` → 周期 8ns → **125 MHz**（GMII，对应 1 Gbps @ 8 bit）；`HALF_PERIOD_OTHER_BFMS_PS=6250` → 周期 12.5ns → **80 MHz**（控制面/CSR 时钟，承接 u2-l3）。这与 u2-l3 讲的三个时钟域频率完全对得上。

**时钟与复位生成**。整个激励源都在一个 `initial ... fork ... join` 里：

- [4.sim/tb.sv:74-126](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/tb.sv#L74-L126)：先给所有信号赋初值，再 `fork` 出复位序列与多路 `forever` 时钟翻转，最后 `run_sim` 倒计时 `$finish`。这种「一个 initial 全包揽」是 Verilator 风格的写法（Verilator 对 `initial`/`always` 的调度比商业仿真器更接近 C 的顺序执行）。

**DUT 例化**。测试台把真实设计 `top` 当成一个普通模块例化，端口逐一连到本地信号：

- [4.sim/tb.sv:198-268](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/tb.sv#L198-L268)：例化 `top dut`，连接差分时钟 `clk_p/clk_n`、复位 `rst_n`、4 路以太网的 GMII/MDIO 信号、UART、按键 `key`、LED。这些信号名与 u2-l2 讲过的 `top.sv` 端口一一对应——这正是「测试台镜像板卡」的体现。

**以太网/MDIO 接线**。`bfm_ethernet` 是 4 口千兆以太网的「对端」，通过 `gmii_if` 接口数组与 DUT 互连：

- [4.sim/tb.sv:290-356](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/tb.sv#L290-L356)：声明 `gmii_if gmii[4]`（4 个 GMII 接口），把 DUT 的 `e1_txd/e1_txen/...`（发送侧）连进 `gmii[0]`，再把 DUT 的接收侧 `e1_rxd/e1_rxdv`（由 `assign` 从 `gmii[0]` 取）回喂 DUT；最后例化 `bfm_ethernet #(.START_NODE, .NUM_PORTS(4), .MDIO_BUFF_ADDR, .RGMII(0)) bfm_udp`。`.RGMII(0)` 表示用 GMII（而非 RGMII）模式。

**UART 模型**。`bfm_uart` 做了一个最简单的串口回环——把 DUT 发出的 `uart_tx` 当成它的 `uart_rx` 输入，再把它的 `uart_tx` 输出回喂 DUT 的 `uart_rx`：

- [4.sim/tb.sv:362-365](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/tb.sv#L362-L365)：注意端口的交叉——`.uart_rx(uart_tx)`、`.uart_tx(uart_rx)`。这模拟了「PC 串口收到板子发来的字节、又把回应发回去」。`bfm_uart` 固定 115200 波特率、8N1（见 [models/bfm_uart.sv:39-52](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/bfm_uart.sv#L39-L52)）。

**交互式控制**。

- [4.sim/tb.sv:275-284](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/tb.sv#L275-L284)：例化 `verilator_sim_ctrl`（节点 15）。默认 `DISABLE_SIM_CTRL=1` 关闭它；设为 0 后可在 `VerSimCtrl>` 提示符下用 `run for 100 ns`、`finish` 等命令，并自动 flush `wave.fst` 供 GTKWave 刷新（详见 [4.sim/README.md:45-64](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/README.md#L45-L64)）。

> **小结**：`tb.sv` 本身不写「测试逻辑」，它只负责「搭台子」——造时钟、摆 DUT、接 BFM。真正的测试逻辑（喂什么包、检查什么）写在 C++ 用户代码里（u7-l2/u7-l4 详述），通过 VProc 驱动这些 BFM 与 DUT。

### 4.2 可替换的 soc_cpu 策略

#### 4.2.1 概念说明

这是本项目仿真体系最巧妙的一点。回顾 u2-l1/u2-l2：真实 SoC 里有一个 picoRV32 软核 CPU（控制面），它通过 `soc_if` 总线访问 DMEM 与 CSR。如果仿真里也用 picoRV32 的 RTL，那连「取一条指令」都要逐时钟周期模拟，整段 WireGuard 握手软件跑起来会非常慢。

本项目的解法是 **「同一份 top，可换 CPU」**：

- 真实综合：`soc_cpu` = picoRV32 RTL（`1.hw/ip.cpu/soc_cpu.PICORV32.sv`）。
- 协同仿真：`soc_cpu` = `soc_cpu.VPROC`（VProc 虚拟处理器，`4.sim/models/soc_cpu.VPROC.sv`）。

两者**对外引脚完全一致**（都是 `soc_if.MST` + 一组 IMEM 写端口）。`top.sv` 不需要改一行——它只例化 `soc_cpu`，至于这个 `soc_cpu` 内部是 RTL 还是 VProc，由 filelist 决定谁被编进来。这样 DUT 的其余部分（fabric、CSR、DPE、以太网 MAC）始终是被真实验证的对象，只有 CPU 这个「最难仿真又最想跑软件」的部分被替换成了 C++。

#### 4.2.2 核心流程

替换是靠 **Makefile 在编译期处理 filelist** 自动完成的，分两步：

```text
① 拷贝并过滤 top.filelist
   1.hw/top.filelist（含 ip.cpu/*.sv 真实 CPU）
        │  sed -e "/ip.cpu/d"        ← 删掉所有匹配 "ip.cpu" 的行
        ▼
   4.sim/top.filelist（本地副本，已无 ip.cpu）
        + 真实 CPU RTL 被排除

② tb.filelist 补上 VProc 版 soc_cpu
   4.sim/tb.filelist 里显式列出：
        ${SIM_DIR}/models/soc_cpu.VPROC.sv   ← 仿真版 soc_cpu
        ${SIM_DIR}/models/cosim/f_VProc.sv   ← VProc 内核
        ${SIM_DIR}/models/cosim/mem_model.sv ← IMEM 存储模型
   ↓
   Verilator 同时读 top.filelist（DUT，无 CPU）+ tb.filelist（含 VProc 版 CPU）
   → "soc_cpu" 这个模块名最终由 soc_cpu.VPROC.sv 提供
```

关键点：`top.sv` 里写的是 `soc_cpu u_soc_cpu (...)`，它只认模块名 `soc_cpu`。真实综合时这个名字由 `soc_cpu.PICORV32.sv` 提供；仿真时由 `soc_cpu.VPROC.sv` 提供。**模块名相同、实现互换**，这就是「即插即用（plug-and-play）」。

匹配哪个字符串去删？由 `SOCCPUMATCH` 变量控制，默认 `ip.cpu`。所以你只要把真实 CPU 源文件放在 `ip.cpu` 目录下，仿真时就会被自动排除。

#### 4.2.3 源码精读

**真实 CPU 在综合版 filelist 里**。先看 `1.hw/top.filelist` 的 CPU 段，确认真实 picoRV32 源文件确实在 `ip.cpu` 下：

- [1.hw/top.filelist:60-67](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L60-L67)：列出 `imem.sv`、`picorv32.CHILI.sv`、`soc_cpu.PICORV32.sv`，路径全部含 `ip.cpu`。这正是仿真时要被排除的对象。

**Makefile 用 sed 过滤**。这条规则是整个替换机制的发动机：

- [4.sim/MakefileVProc.mk:265-268](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/MakefileVProc.mk#L265-L268)：目标 `$(TOPFILELIST)` 依赖源 `1.hw/top.filelist`，用 `sed -e "/$(SOCCPUMATCH)/d"` 把含 `ip.cpu` 的行删掉，输出到本地 `4.sim/top.filelist`。注释写得很直白：「Create local file list for top, with PicoRv32 files removed — So soc_cpu.VPROC can be used instead」。

**tb.filelist 补上 VProc 版**。仿真侧的 filelist 显式把 VProc 版 `soc_cpu` 加回来：

- [4.sim/tb.filelist:9-27](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/tb.filelist#L9-L27)：列出协同仿真内核 `f_VProc.sv`、`mem_model.sv`、以太网 VIP `udp_ip_pg.v`/`bfm_ethernet.sv`，以及关键的 `models/soc_cpu.VPROC.sv`、`bfm_uart.sv`、`bfm_phy_mdio.sv` 和几颗 Xilinx 原语的行为模型（`glbl.v`/`BUFG.v`/`BUFGMUX.v`/`IBUFGDS.v`，因为 `top` 里例化了它们，仿真器需要其行为模型而非综合原语）。最后一行 `${SIM_DIR}/${TB_NAME}.sv` 把 `tb.sv` 本身收进来。

**soc_cpu.VPROC 的内部**。VProc 版包装器把 VProc 的「通用内存映射接口」翻译成项目自有的 `soc_if` 协议。这套翻译逻辑非常轻量（README 称「不到十个组合门」）：

- [4.sim/models/soc_cpu.VPROC.sv:87-99](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/soc_cpu.VPROC.sv#L87-L99)：模块声明。端口 `soc_if.MST bus` 与真实 `soc_cpu.PICORV32` 完全一致；`NODE` 参数默认 0（即 VProc 的节点 0，对应用户入口 `VUserMain0`）。注释里的地址映射（IMEM `0x0000_0000`、DMEM `0x1000_0000`、CSR `0x2000_0000`）与 u6-l1 讲的 `link_map.lds` 一致。

- [4.sim/models/soc_cpu.VPROC.sv:126-145](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/soc_cpu.VPROC.sv#L126-L145)：协议翻译。`cpu_access = vp_addr[31:28]=='0` 判断是否访问本地 IMEM；`bus.we = vp_be & {4{vp_we}}` 把 VProc 的字节使能与写使能合成 `soc_if` 的 `we`；`bus.vld = (vp_we|vp_rd) & ~cpu_access` 在非 IMEM 访问时拉高总线 valid（承接 u2-l4 的 vld/rdy 握手）；`bus.addr = vp_addr[31:2]` 把字节地址转成字地址——正是 u2-l4 讲过的 `soc_addr_t` 约定。

- [4.sim/models/soc_cpu.VPROC.sv:180-205](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/soc_cpu.VPROC.sv#L180-L205)：例化 VProc 内核 `u_cpu`，连时钟与上述翻译信号。

- [4.sim/models/soc_cpu.VPROC.sv:212-244](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/soc_cpu.VPROC.sv#L212-L244)：例化 `mem_model u_imem` 当作 IMEM。它有两个口：一个供 VProc 读写程序（取指），另一个 `wr_port_*` 供 UART 在线烧写（承接 u2-l5 的 IMPR/IMWR）。两个口指向同一份稀疏内存，于是「PC 经 UART 烧的程序」和「VProc 取指的程序」是同一份。

文档对此机制的总结见 [4.sim/README.md:66-68](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/README.md#L66-L68) 与 [4.sim/models/README.md:22-26](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/README.md#L22-L26)：仿真构建会处理 `top.filelist` 生成去掉了 `ip.cpu` 的本地副本，而 `soc_cpu.VPROC.sv` 作为测试模型放在 `4.sim/models`，从而被选中。

#### 4.2.4 代码实践

**实践目标**：亲手确认「filelist 过滤」确实发生了，理解替换机制的物理证据。

**操作步骤**（源码阅读型，无需运行仿真）：

1. 打开 `1.hw/top.filelist`，找到 CPU 段（约第 60-67 行），确认 `imem.sv`、`picorv32.CHILI.sv`、`soc_cpu.PICORV32.sv` 三行的路径都含 `ip.cpu`。
2. 打开 `4.sim/MakefileVProc.mk` 第 265-268 行，读懂 `sed -e "/$(SOCCPUMATCH)/d"` 的作用：把上一条里那三行删掉。
3. 打开 `4.sim/tb.filelist`，找到 `models/soc_cpu.VPROC.sv` 那一行（约第 15 行）——这就是顶替 picoRV32 的「替身」。
4. 在脑中把这三步连起来：Verilator 同时读 `4.sim/top.filelist`（DUT，无 CPU）+ `4.sim/tb.filelist`（含 VProc 版 CPU），模块名 `soc_cpu` 因此由 VProc 版提供。

**需要观察的现象**：`1.hw/top.filelist` 里有 `ip.cpu`，而 `4.sim/top.filelist`（如果之前跑过 make 会生成）里没有 `ip.cpu`，但 `4.sim/tb.filelist` 里有 `soc_cpu.VPROC.sv`。

**预期结果**：能用自己的话讲清——「真实 CPU 被 sed 删掉、VProc 版 CPU 被 tb.filelist 加回来，两者模块名都叫 `soc_cpu`，所以 `top.sv` 无需改动即可在仿真里用上 VProc」。

> 注：`4.sim/top.filelist` 是 make 生成的中间产物（在 `.gitignore` 范围、`clean` 会删它）。若仓库里恰好存在一份，它就是某次构建后留下的本地副本；以 `1.hw/top.filelist` + sed 规则推导出的结果为准。

#### 4.2.5 小练习与答案

**练习 1**：如果将来要把 picoRV32 换成另一颗叫 `eduBOS5` 的 RISC-V 软核，仿真替换机制需要改什么？

**参考答案**：只要把新软核的源文件也放进 `ip.cpu` 目录（或任何路径含 `ip.cpu` 字样的目录），并在 `1.hw/top.filelist` 里登记，仿真时 sed 会照常把它删掉；`soc_cpu.VPROC.sv` 仍是同一份替身。整个替换机制对「具体是哪颗 RTL CPU」是无感的——这正是它的价值。若新软核不放在 `ip.cpu` 下，则需把 `SOCCPUMATCH` 改成匹配新路径的字符串。

**练习 2**：`soc_cpu.VPROC` 里 `bus.addr = vp_addr[31:2]`（取 30 位），为什么丢掉最低 2 位？

**参考答案**：`soc_if` 用的是**字地址**（每个地址对应一个 32 位字），而 VProc 给的是**字节地址**。一字节地址右移 2 位（除以 4）即得字地址，丢掉的最低 2 位正是字内字节偏移（由 `we`/字节使能承担，见 u2-l4 的 `soc_we_t`）。

### 4.3 MakefileVProc.mk 的编译与运行编排

#### 4.3.1 概念说明

`MakefileVProc.mk` 是仿真侧的「总指挥」。一条 `make -f MakefileVProc.mk run` 背后，它要协调三类完全不同的产物：

1. **用户 C/C++ 代码** → 编成静态库 `libuser.a`（驱动 BFM 的测试逻辑，如以太网收发包）。
2. **VProc + mem_model + ISS 用户代码** → 编进 `libvproc.a`（含 `soc_cpu` 节点 0 的 `VUserMain0`、VerilatorSimCtrl 节点 15）。
3. **HDL 设计 + 测试台** → 用 Verilator 翻译成 C++，再与上面两个库 + 预编译库（`libcosimlnx.a`/`libudplnx.a`）链接成最终的可执行仿真程序 `output/Vtb`。

它还用一组「命令行可覆盖变量」暴露旋钮，让你不动 Makefile 就能换测试程序、换构建类型、调超时。

#### 4.3.2 核心流程

```text
make -f MakefileVProc.mk run
   │
   ├─→ all → $(TOPFILELIST) + $(SIMEXE)
   │     │
   │     ├─ $(TOPFILELIST): sed 过滤 1.hw/top.filelist → 4.sim/top.filelist  （见 4.2）
   │     │
   │     └─ $(SIMEXE): output/Vtb
   │          │
   │          ├─ compile:
   │          │    ├─ $(USERLIB) libuser.a : gcc/g++ 编 USER_C + UDP_C → obj/ → ar 打包
   │          │    ├─ $(VLIB) libvproc.a   : 调 VProc 自带 makefile，编 VPROC_USER_C
   │          │    │                         （必要时 git clone VProc / mem_model 仓库）
   │          │    └─ verilator -F top.filelist -F tb.filelist ...
   │          │         → 生成 C++ → make -C output 编出 Vtb
   │          │
   │          └─ 链接 -lvproc -luser -ludplnx -lcosimlnx（+ rv32 库，若 BUILD=ISS）
   │
   └→ run: 执行 output/Vtb（批量模式）
```

两套「用户代码」要分清（这是读这个 Makefile 最容易绕晕的地方）：

- **`VPROC_USER_C`**（[MakefileVProc.mk:59-61](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/MakefileVProc.mk#L59-L61)）：VProc 的 `soc_cpu`（节点 0）+ 交互控制（节点 15）的用户代码，来自 `models/rv32/usercode/`，编进 `libvproc.a`。其中 `VUserMain0.cpp` 就是 `soc_cpu` 节点 0 的入口（见下）。
- **`USER_C` + `UDP_C`**（[MakefileVProc.mk:9,41](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/MakefileVProc.mk#L9-L41)）：以太网 BFM（节点 1-4）等的用户代码，来自 `usercode/`，编进 `libuser.a`。默认 `UDP_C=VUserMainUdp.cpp`（4 个以太网节点的入口都在这一个文件里）。

`BUILD` 变量决定 `soc_cpu` 节点 0 跑的是什么软件：

- `BUILD=DEFAULT`（默认）：节点 0 直接跑**原生编译**的 C++ 测试代码（用 VProc 的 `write`/`read`/`tick` API 驱动总线）。
- `BUILD=ISS`：节点 0 跑 **rv32 RISC-V 指令集模拟器**，由它加载并执行真正的 RISC-V 固件二进制（u7-l3 详述）。此时 `USER_C`/`USRCODEDIR` 被忽略，改用预编译的 `librv32lnx.a`。

#### 4.3.3 源码精读

**命令行可覆盖变量**。文件开头一整块都是带默认值的变量，全部可用 `make VAR=值` 覆盖：

- [4.sim/MakefileVProc.mk:9-21](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/MakefileVProc.mk#L9-L21)：`USER_C`（soc_cpu 的 libuser 源，默认空）、`USRCODEDIR`（用户代码目录，默认 `usercode/`）、`OPTFLAG=-g`（调试符号）、`TOPFILELIST=top.filelist`、`SOCCPUMATCH=ip.cpu`（filelist 过滤串，见 4.2）、`BUILD=DEFAULT`、`TIMEOUTUS=10000`（仿真超时微秒数，会传成 `tb` 的 `RUN_SIM_US`）、`DISABLE_SIM_CTRL=1`。

**BUILD=ISS 分支**。`ifeq ("$(BUILD)","" ISS")` 块切换到 ISS 构建：

- [4.sim/MakefileVProc.mk:73-92](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/MakefileVProc.mk#L73-L92)：按操作系统选 `rv32lnx`/`rv32win` 库，设置 `RV32DIR`、包含路径与链接选项。注意它**不覆盖 `UDP_C`**——4 个以太网 BFM 节点无论哪种 BUILD 都由 `VUserMainUdp.cpp` 驱动。

**Verilator 编译选项**。`SIMOPTS`/`SIMDEFS` 定义了怎么把 HDL 喂给 Verilator：

- [4.sim/MakefileVProc.mk:165-180](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/MakefileVProc.mk#L165-L180)：`--cc -sv --timing --trace-fst`（生成 C++、支持 SystemVerilog、开时序模型、FST 波形）；`-GRUN_SIM_US=$(TIMEOUTUS) -GDISABLE_SIM_CTRL=$(DISABLE_SIM_CTRL)` 把 `tb` 的两个 parameter 传进去（Verilator 的 `-G` 即泛型/参数实例化）；`SIMDEFS` 里 `+define+VPROC_BYTE_ENABLE`（让 VProc 带 BE 端口）、`+define+SIM_ONLY`（让 `top` 内的 PLL 等走仿真行为模型——承接 u2-l3 的 `SIM_ONLY`）、`+define+VPROC_SV`。还把 VProc 的 `verilator_sim_ctrl.sv` 一并编进来。

**compile 目标**。这是把 HDL 与 C++ 缝合在一起的核心规则：

- [4.sim/MakefileVProc.mk:241-257](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/MakefileVProc.mk#L241-L257)：依赖 `$(USERLIB)`（libuser.a）与 `$(VLIB)`（libvproc.a）后，调 `verilator -F top.filelist -F tb.filelist ...`（两个 filelist 同时喂入），并用 `-LDFLAGS` 把 `-lvproc -luser -ludplnx`（外加 `--whole-archive` 保证 DPI-C 符号全保留）链进来。`-ldl $(RV32LDOPTS)` 处理动态库与可选的 rv32 库。

**执行与清理目标**：

- [4.sim/MakefileVProc.mk:297-308](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/MakefileVProc.mk#L297-L308)：`run`（批量跑 `output/Vtb`）、`rungui`/`gui`（跑完用 GTKWave 打开 `wave.fst`，若存在 `WAVESAVEFILE` 指定的 `.gtkw` 则套用其信号布局）。
- [4.sim/MakefileVProc.mk:336-337](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/MakefileVProc.mk#L336-L337)：`clean` 删除 `output/`、`wave.fst`、生成的本地 `top.filelist`、`obj/`、`libuser.a` 等中间产物。

**help 目标**。最快的「速查表」：

- [4.sim/MakefileVProc.mk:310-330](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/MakefileVProc.mk#L310-L330)：列出所有目标（`help`/空/`run`/`rungui,gui`/`clean`）与全部可配置变量及默认值。第一次接触这个 Makefile，先 `make -f MakefileVProc.mk help` 是最稳的入门姿势。文档版的同一份说明见 [4.sim/README.md:245-265](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/README.md#L245-L265)。

> **工具版本**：这套协同仿真依赖一组固定版本的外部工具（[4.sim/README.md:587-592](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/README.md#L587-L592)）：Verilator v5.024、VProc v1.12.2、mem_model v1.0.0、rv32 ISS v1.1.4、udpIpPg v1.0.3。Makefile 会按版本号（如 `VPROCVERSION = VERSION_1_12_2`）自动 `git clone` 对应仓库，无需手动准备。

#### 4.3.4 代码实践

**实践目标**：跑通默认仿真，从输出里反推「`soc_cpu` 自动选了 VProc」这一事实。

**操作步骤**：

1. 先看一遍默认用户入口长什么样——它是节点 0 的 `VUserMain0`：

   - [4.sim/models/rv32/usercode/VUserMain0.cpp](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/usercode/VUserMain0.cpp)（参考简化版见 [4.sim/usercode/VUserMain0.cpp:39-71](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/usercode/VUserMain0.cpp#L39-L71)）：`VUserMain0` 创建一个 `VProc` 对象（节点 0），等 100 拍，向地址 `0x10001000` 写 `0x900dc0de`，读回比较，然后 `tick(GO_TO_SLEEP)` 永久休眠。`0x10001000` 落在 DMEM 区（`0x1000_0000`~`0x1FFF_FFFF`），所以这次写会经 `soc_if` 总线打到 `soc_fabric` 再到 DMEM——完全走的是真实数据通路，只是「CPU」换成了 VProc。

2. 在 `4.sim/` 目录下执行默认构建并运行（**待本地验证**，需要本机已装 Verilator 5.024 且能联网拉 VProc/mem_model 仓库）：

   ```bash
   cd 4.sim
   make -f MakefileVProc.mk help        # 先看一眼所有变量与目标
   make -f MakefileVProc.mk run          # 编译 + 批量跑默认 VUserMain0
   ```

3. 阅读标准输出。

**需要观察的现象**：仿真启动时应出现类似下面的关键行（参考 [4.sim/README.md:313-321](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/README.md#L313-L321) 的真实输出片段）：

```text
VInit(0): initialising DPI-C interface
VProc version 1.12.4. ...
```

以及用户代码经 `VPrint` 打出的：

```text
VProc soc_cpu entered VUserMain0()
Written   0x900dc0de  to  addr 0x10001000
Read back 0x900dc0de from addr 0x10001000
```

最后是 `tb.sv` 的 `$finish` 行（含仿真结束时间统计）。

**预期结果**：

- `Read back` 与 `Written` 的值一致（`0x900dc0de`），证明 VProc 经 `soc_if`→`soc_fabric`→DMEM 的来回是通的。
- 输出里出现 `VProc` 字样而非任何 picoRV32 取指/译码痕迹——这就是「`soc_cpu` 被自动替换成 VProc」的直接证据：因为 picoRV32 的 RTL 根本没被编译进来（被 4.2 的 sed 删掉了）。

**如何解释 `soc_cpu` 的自动选择**（本题第二问）：用一句话——「`1.hw/top.filelist` 里所有 `ip.cpu` 行被 Makefile 的 `sed` 删除，`soc_cpu.VPROC.sv` 由 `tb.filelist` 显式加入；二者模块名同为 `soc_cpu`，于是 Verilator 解析时 VProc 版胜出，DUT `top` 在不知情的情况下用上了虚拟处理器。」

> 若本机环境受限无法运行，可改为「源码阅读型实践」：对照 4.2.3 的三个文件（`1.hw/top.filelist`、`MakefileVProc.mk` 的 sed 规则、`tb.filelist`）口述一遍替换链路，并解释 `VUserMain0` 里那次 DMEM 写会经过哪些 HDL 模块（VProc→`soc_cpu.VPROC` 协议翻译→`soc_if`→`soc_fabric` 地址译码→DMEM）。

#### 4.3.5 小练习与答案

**练习 1**：`make -f MakefileVProc.mk run` 与 `make -f MakefileVProc.mk`（不带目标）有何区别？

**参考答案**：不带目标时走默认目标 `all`（[MakefileVProc.mk:219](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/MakefileVProc.mk#L219)），只编译出 `output/Vtb` **但不运行**；带 `run` 目标（[MakefileVProc.mk:297-298](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/MakefileVProc.mk#L297-L298)）会在编译后执行 `$(SIMEXE)` 跑仿真。前者适合只想检查「能否编译通过」时用。

**练习 2**：仿真超时是怎么控制的？改大它要动哪里？

**参考答案**：`TIMEOUTUS` 变量（默认 10000 微秒）在 [MakefileVProc.mk:170](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/MakefileVProc.mk#L170) 经 `-GRUN_SIM_US=$(TIMEOUTUS)` 传成 `tb` 模块的 `RUN_SIM_US` 参数；`tb.sv` 的 `run_sim` 线程等够 `RUN_SIM_US` 微秒就 `$finish`（[tb.sv:121-124](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/tb.sv#L121-L124)）。改大只需命令行覆盖：`make -f MakefileVProc.mk TIMEOUTUS=50000 run`，无需改任何源文件。

## 5. 综合实践

把本讲三个模块串起来，完成一次「画出测试台全景图 + 解释一次 DMEM 写的全链路」的综合任务。

**任务**：

1. **画一张测试台框图**。在纸上（或任意画图工具）画出：中央是 DUT `top`；左侧是时钟/复位源（标出 200/125/80 MHz 与 `rst_n`/`gmiiarst_n`）；右侧分三组外设模型——4 个 GMII 接到 `bfm_ethernet`（再标出其内部 4 个 udpIpPg 节点 1-4 + 4 个 `bfm_phy_mdio`）、UART 接到 `bfm_uart`、以及 `verilator_sim_ctrl`（节点 15）。在 `top` 内部用虚线标出 `soc_cpu` 模块，并注明「仿真时 = VProc」。

2. **追踪一次 DMEM 写**。从 `VUserMain0` 里的 `vp0->write(0x10001000, 0x900dc0de)` 出发，写出这次写穿越的全部边界：
   - C++ 侧：`VProc::write` → DPI-C → VProc HDL 内核 `u_cpu` 的 `Addr/DataOut/WE/BE` 输出；
   - 协议翻译（`soc_cpu.VPROC.sv`）：`vp_addr`→`bus.addr`（`[31:2]`）、`vp_we & vp_be`→`bus.we`、拉高 `bus.vld`；
   - 总线（`soc_if`/`soc_fabric`）：`vld`&`rdy` 同拍为 1 完成握手，地址 `0x10001000` 命中 DMEM 译码窗口（`addr[31:28]==1`，见 u2-l4）；
   - 落地：写入 DMEM；随后 `vp_rack`/`vp_wack` 回给 VProc 完成这次事务。

3. **自检**：回答——如果这次写的地址改成 `0x20001000`（CSR 区），链路会在哪一步分叉？又如果改成 `0x00001000`（IMEM 区），还会走 `soc_if` 总线吗？

**参考答案（第 3 问）**：写 `0x20001000` 时，`soc_fabric` 的地址译码会命中 CSR 窗口（`addr[31:29]==1`）而非 DMEM，事务改去 `soc_csr`→PeakRDL 生成的 `csr.sv`，最终落到某个 CSR 寄存器（正是 u3 单元讲的软硬件桥梁）。写 `0x00001000` 时，`soc_cpu.VPROC` 里 `cpu_access = vp_addr[31:28]=='0` 为真，于是 `bus.vld` 被屏蔽（[soc_cpu.VPROC.sv:127,133](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/soc_cpu.VPROC.sv#L126-L133)），写**不**走 `soc_if` 总线，而是走本地 `imem_write` 进 `mem_model u_imem`——即直接写进 VProc 自己的程序存储，这正是「UART 在线烧写 IMEM」的同一通路。

## 6. 本讲小结

- `4.sim` 测试台是真实板卡的数字孪生：DUT `top` 周围用 `bfm_ethernet`/`bfm_uart`/`bfm_phy_mdio` 扮演 PHY、PC 串口、MDIO 从机，外加自造的 200/125/80 MHz 时钟与复位。
- 真正的测试逻辑不在 `tb.sv` 里，而在 C++ 用户代码（`VUserMain0` 等节点入口）里，经 VProc 的 DPI-C API 驱动 DUT——`tb.sv` 只负责「搭台子」。
- 「可替换 `soc_cpu`」是核心巧思：`top.sv` 只认模块名 `soc_cpu`，仿真时 Makefile 用 `sed` 删掉 `1.hw/top.filelist` 里所有 `ip.cpu` 行，`tb.filelist` 再把 `soc_cpu.VPROC.sv` 加回来，于是 picoRV32 RTL 被同名的 VProc 包装器顶替，DUT 其余部分仍是真实验证对象。
- `soc_cpu.VPROC` 用不到十个组合门把 VProc 的通用内存映射接口翻译成项目自有的 `soc_if` 协议，并接一个 `mem_model` 当 IMEM（与 UART 在线烧写共享同一稀疏内存）。
- `MakefileVProc.mk` 编排三类产物（`libuser.a`、`libvproc.a`、Verilator 生成的 `output/Vtb`），用 `BUILD=DEFAULT/ISS` 切换节点 0 跑原生 C++ 还是 rv32 ISS，并用 `USER_C`/`TIMEOUTUS`/`SOCCPUMATCH` 等变量暴露旋钮。
- 常用入口：`make -f MakefileVProc.mk help`（查变量）、`run`（编译并批量跑）、`rungui`（跑完看波形）、`clean`（清中间产物）；工具链版本固定（Verilator 5.024、VProc 1.12.2 等），Makefile 按版本号自动拉取外部仓库。

## 7. 下一步学习建议

本讲只搭好了「台子」并解释了 CPU 怎么被替换。接下来：

- **u7-l2 VProc 虚拟处理器协同仿真**：深入 `soc_cpu.VPROC` 背后的 C++ API（`VProc::write/read/tick`）、`VUserMain0` 的写法，以及协同仿真 HAL 如何让同一份应用代码既能在硬件跑、又能在仿真跑——这是写自定义测试逻辑的基础。
- **u7-l3 rv32 RISC-V ISS 与软件集成**：当 `BUILD=ISS` 时，节点 0 不再跑原生 C++，而是跑 rv32 指令集模拟器加载的真实 RISC-V 固件（即 `2.sw` 编出来的那个二进制），用 `-x/-X` 划定哪些地址走 HDL、哪些走 C 内存模型。
- **u7-l4 以太网 VIP udpIpPg 与 BFM**：展开 `bfm_ethernet` 内部的 udpIpPg 包生成/回调接收 API，学会用 C++ 代码构造 UDP 包、注入 4 个 GMII 口、接收回包——这是端到端以太网数据面验证的手段。
- **u7-l5 mem_model、PCAP 回放与逐模块测试台**：稀疏内存模型的跨域共享、`VUserMainPcap` 的 PCAP 回放/录制，以及 `4.sim/rtl/` 下对 DPE/mux/demux/加密核等的逐模块单元测试台。

若想立刻动手，建议先把本讲的综合实践做完（画框图 + 追 DMEM 写链路），再进 u7-l2 学怎么自己写一段 VProc 测试代码去驱动 CSR 寄存器。
