# 综合约束与时序

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清以太网 PHY 接口为什么是「源同步（source-synchronous）」接口，以及为什么这类接口必须写专门的时序约束，不能交给工具默认分析。
- 读懂 `syn/` 下三类约束文件：Quartus 的 `.sdc`、Quartus Pro 的 `.sdc`、Vivado 的 `.tcl`，并能解释它们各自用什么对象查询语法去定位待约束的单元。
- 逐行解释 RGMII 接收侧 DDR 约束里的 `create_clock` / `set_input_delay` / `set_false_path` 三件套在做什么，以及为什么 DDR 必须做「分边沿」处理（即 `-edge_shift` 之类的技术要解决的根本问题）。
- 理解 Vivado 脚本用 `foreach inst [get_cells -hier -regexp ...]` 自动发现实例、用 `ASYNC_REG` + `set_max_delay -datapath_only`（必要时加 `set_bus_skew`）约束跨时钟域（CDC）路径的通用套路。
- 对比同一逻辑在 Quartus / Quartus Pro / Vivado 三套工具下的约束写法差异，知道把本库移植到一块新板子时要改什么。

## 2. 前置知识

本讲默认你已经学过 **u4-l4（PHY 接口、时钟与三模 GMII MAC）**，知道 `gmii_phy_if` / `rgmii_phy_if` 是 MAC 与外部 PHY 芯片之间的 IO 桥梁，知道 RGMII 用时钟双边沿（DDR）传 4 位数据。下面补充三个本讲要用、但前面没专门讲的概念。

### 2.1 什么是时序约束（SDC / XDC）

FPGA 里每个触发器（flip-flop，简称 FF）都有「建立时间 setup」和「保持时间 hold」要求：数据必须在时钟沿之前稳定至少 setup 时间、之后继续稳定至少 hold 时间，才能被正确采样。综合/布局布线后的「静态时序分析（STA）」工具会检查所有寄存器到寄存器的路径是否满足。

但工具自己并不知道：

- 一根外部时钟从哪个管脚进来、频率多少；
- 外部数据相对这个时钟偏移多少；
- 哪些路径是跨时钟域的、不该按同域路径分析。

这些信息必须由设计者用约束文件告诉工具。业界通用格式是 **SDC（Synopsys Design Constraints）**，Quartus / Quartus Pro 直接用；Vivado 用语法兼容的 **XDC（Xilinx Design Constraints）**，本质是一段 Tcl 脚本。

### 2.2 什么是源同步接口

普通接口里，FPGA 用自己内部的某个时钟去采样外部数据，时钟和数据各走各的。而 **源同步接口** 把「采样时钟」和「数据」一起从同一个源（比如 PHY 芯片）送到 FPGA：PHY 既给 `rx_clk`，又给 `rxd[3:0]`，二者相位有固定的约定关系。因此约束的重点不是「数据能跑多快」，而是「**外部数据相对外部时钟的到达关系**」。这就是为什么本库的 RGMII 约束几乎全在描述 `rx_clk` 与 `rxd` 之间的相位。

### 2.3 什么是 DDR 与跨时钟域（CDC）

- **DDR（Double Data Rate）**：时钟上升沿和下降沿各采样一次数据，一个时钟周期传两拍。RGMII 用 125 MHz 时钟的上下沿各传 4 位，等效每周期 8 位 → 1 Gbps。DDR 让 setup/hold 分析翻倍复杂，因为要分别处理上升沿关系和下降沿关系。
- **CDC（Clock Domain Crossing）**：`mii_select`、`rx_prescale`、PTP 时间戳等信号从 `rx_clk` 域进 `tx_clk`/`logic_clk` 域。跨域路径没有公共时钟，工具无法算出有意义的余量，必须用约束告诉它「这是异步跨域，请按最大延迟限制 + 同步器寄存器来处理」。

## 3. 本讲源码地图

本讲只看 `syn/` 目录与少量被约束的 RTL，不涉及协议逻辑。`syn/` 下按工具链分三档：

| 目录 | 工具 | 文件类型 | 定位对象的主要语法 |
|------|------|----------|--------------------|
| `syn/quartus/` | Quartus（经典） | `.sdc` | `get_registers "inst\|reg[*]"`，过程接受实例路径 |
| `syn/quartus_pro/` | Quartus Pro | `.sdc` | 同上，但可用 `set_data_delay ... -value_multiplier` |
| `syn/vivado/` | Vivado | `.tcl`（XDC） | `get_cells -hier -regexp ...` 自动发现实例 |

本讲精读的关键文件：

- [`syn/quartus/rgmii_io.sdc`](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/quartus/rgmii_io.sdc) —— **本讲的核心**，RGMII 管脚级源同步 DDR 约束的两个过程，setup/hold 注释最完整。
- [`syn/quartus/rgmii_phy_if.sdc`](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/quartus/rgmii_phy_if.sdc) —— PHY IF 内部复位同步的 false path。
- [`syn/vivado/rgmii_phy_if.tcl`](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/vivado/rgmii_phy_if.tcl) —— Vivado 下 PHY IF 的复位同步 + 转发时钟路径约束。
- [`syn/vivado/eth_mac_1g_rgmii.tcl`](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/vivado/eth_mac_1g_rgmii.tcl) —— MAC 顶层三条 CDC 路径约束的范例。
- [`syn/vivado/ptp_clock_cdc.tcl`](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/vivado/ptp_clock_cdc.tcl) —— 最复杂的 CDC 约束，演示 `set_bus_skew`。
- [`syn/quartus/eth_mac_1g_rgmii.sdc`](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/quartus/eth_mac_1g_rgmii.sdc) 与 [`syn/quartus_pro/eth_mac_1g_rgmii.sdc`](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/quartus_pro/eth_mac_1g_rgmii.sdc) —— 同一逻辑在两种 Quartus 下的写法对比。

被约束的 RTL（用来解释「为什么约束指向这些名字」）：

- [`rtl/rgmii_phy_if.v`](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/rgmii_phy_if.v) —— `rgmii_tx_clk_1/2` 分频寄存器与 `clk_oddr_inst` 转发时钟原语。
- [`rtl/eth_mac_1g_rgmii.v`](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_rgmii.v) —— `mii_select_reg` 与两级 `*_mii_select_sync` 同步器。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：源同步 DDR 时序约束、Vivado Tcl 封装、跨工具链约束差异。

### 4.1 源同步 DDR 时序约束

#### 4.1.1 概念说明

RGMII 接收侧有四类信号从 PHY 芯片进入 FPGA：`rx_clk`（125 MHz 时钟）、`rxd[3:0]`（4 位数据）、`rx_ctl`（控制线）。它们是**源同步**的——PHY 同时驱动时钟和数据，并且按 RGMII 规范，数据跳变沿相对时钟边沿有约 1.5~2 ns 的内部延迟（称为 RGMII-ID 或 RGMII with internal delay）。FPGA 要在时钟的上升沿和下降沿各采样一次 `rxd`（DDR）。

如果不写任何约束，时序工具会犯两种错：

1. 它不知道 `rx_clk` 是一个真实时钟，于是和它相关的路径要么不被分析，要么被当成与某个内部时钟「虚假相关」而过约束。
2. 它默认按单沿（SDR）分析，对 DDR 接口会得出错误的 setup/hold 余量。

所以必须写一套约束，**显式声明**：外部时钟的周期与相位、外部数据相对该时钟的到达偏移（`set_input_delay`）、以及哪些边沿组合是「真实的采样关系」（保留分析）、哪些是「虚假的跨边沿关系」（用 `set_false_path` 切掉）。

#### 4.1.2 核心流程：virtual clock + 双边沿 input delay + false path

本库 `rgmii_io.sdc` 用的技术可以概括为三步：

1. **建两个时钟**：一个虚拟时钟 `virt_*_rx_clk_125m`（相位为 0，仅作参考），一个真实管脚时钟 `*_rx_clk_125m`（带 90° 相移，`-waveform {2 6}`，模拟 PHY 送来的、相对数据偏移过的时钟）。
2. **用虚拟时钟描述数据到达**：对数据管脚写 `set_input_delay`，分别给出 `-max`（最晚到达，影响 setup）和 `-min`（最早到达，影响 hold），并且**既写上升沿又写 `-clock_fall`（下降沿）**——这一份 `-clock_fall` 就是 DDR 的标记，告诉工具「下降沿也采一次数据」。
3. **切掉虚假的边沿组合**：DDR 接口里，上升沿数据应由上升沿采样、下降沿数据应由下降沿采样。但工具会算出四种边沿组合的余量，其中「跨边沿」的组合是虚假的，必须用 `set_false_path` 切掉，否则会报假违规。规则是：
   - setup 分析保留同向边沿（rise→rise、fall→fall），切掉反向（rise→fall、fall→rise）；
   - hold 分析保留反向边沿，切掉同向。

这套「虚拟时钟 + 分边沿 false path」的技巧，**功能上等价于另一些 SDC 流程里的 `-edge_shift`**：二者都是为了让工具按源同步 DDR 的真实采样关系去算 setup/hold，而不是把所有边沿组合一锅端。`-edge_shift` 的做法是直接平移参考时钟的边沿位置去对齐数据有效窗口；本库则用虚拟时钟加 false path 达到同样的「只保留真实边沿关系」的效果。

发送侧（FPGA→PHY）是对称的：用 `create_generated_clock` 在 `tx_clk` 输出管脚上声明一个由内部时钟派生的时钟，再用 `set_output_delay`（同样带 `-clock_fall`）描述数据相对它的关系，最后同样切虚假边沿组合。

#### 4.1.3 源码精读

先看接收侧过程头与建钟部分（[rgmii_io.sdc:23-45](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/quartus/rgmii_io.sdc#L23-L45)）：

```tcl
proc constrain_rgmii_input_pins { name clk_pin data_pins } {
    ...
    # Virtual clock has no phase shift
    create_clock -name "virt_${name}_rx_clk_125m" -period 8.000
    # input clock has 90 degree phase shift
    create_clock -name "${name}_rx_clk_125m" -period 8.000 "$clk_pin" -waveform {2 6}

    ## Constraint the path to the rising/falling edge of the phy clock
    ## setup time: 2ns-0.75ns=1.25ns, 0.75ns skew,
    ## hold time: 0.75ns skew, 2-1.5-0.75=-0.25ns
    ## clock edge is 1.5 ns delay with data
    set_input_delay -add_delay -clock "virt_${name}_rx_clk_125m" -max 1.25 [get_ports "$data_pins"]
    set_input_delay -add_delay -clock "virt_${name}_rx_clk_125m" -min -0.25 [get_ports "$data_pins"]
    set_input_delay -add_delay -clock "virt_${name}_rx_clk_125m" -clock_fall -max 1.25 [get_ports "$data_pins"]
    set_input_delay -add_delay -clock "virt_${name}_rx_clk_125m" -clock_fall -min -0.25 [get_ports "$data_pins"]
```

要点：

- 过程接受三个参数：`name`（约束命名前缀）、`clk_pin`（`rx_clk` 管脚）、`data_pins`（数据管脚列表）。真实管脚名由使用者在板级 XDC/SDC 里传入，所以这个脚本本身**不绑定具体板子**，可复用。
- `-period 8.000` = 125 MHz。`-waveform {2 6}` 让上升沿在 2 ns、下降沿在 6 ns，相当于把时钟相对 0 相位移了 90°，模拟 PHY 输出的已延迟时钟。
- 注释把数字来历说得很清楚：PHY 内部延迟 2 ns、偏斜 0.75 ns，所以 setup 的外部到达 = `2 - 0.75 = 1.25 ns`，hold 的外部到达 = `2 - 1.5 - 0.75 = -0.25 ns`。
- **第 4 条 `set_input_delay` 带 `-clock_fall` 就是 DDR 的关键标记**——它声明下降沿也有一拍数据需要满足约束。没有这一条，工具只分析上升沿那一拍。

接着看切虚假边沿的部分（[rgmii_io.sdc:69-82](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/quartus/rgmii_io.sdc#L69-L82)）：

```tcl
    ## setup time, set false path, rise-->fall, fall-->rise
    set_false_path -rise_from [get_clocks "virt_${name}_rx_clk_125m"] -fall_to [get_clocks "${name}_rx_clk_125m"] -setup
    set_false_path -fall_from [get_clocks "virt_${name}_rx_clk_125m"] -rise_to [get_clocks "${name}_rx_clk_125m"] -setup

    ## hold time, set false path, rise-->rise, fall-->fall
    set_false_path -rise_from [get_clocks "virt_${name}_rx_clk_125m"] -rise_to [get_clocks "${name}_rx_clk_125m"] -hold
    set_false_path -fall_from [get_clocks "virt_${name}_rx_clk_125m"] -fall_to [get_clocks "${name}_rx_clk_125m"] -hold
```

这里的 `from` 是数据参考用的虚拟时钟（无相移），`to` 是真实管脚时钟（带 90° 相移）。setup 切掉反向边沿组合、hold 切掉同向边沿组合——这就是上面 4.1.2 说的「只保留真实采样关系」。被注释掉的 25 M / 2.5 M 段（[rgmii_io.sdc:30-31, 35-36](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/quartus/rgmii_io.sdc#L30-L36)）说明这套约束本可同时覆盖 100 M / 10 M 速率，用 `set_clock_groups -exclusive` 声明三档时钟互斥；默认只启用 1 G（125 MHz）。

发送侧 `constrain_rgmii_output_pins` 结构对称（[rgmii_io.sdc:91-106](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/quartus/rgmii_io.sdc#L91-L106)）：

```tcl
    create_generated_clock -name "${name}_tx_clk_125m" -source [get_pins "$clk_src"] [get_ports "$clk_pin"]
    set_output_delay -add_delay -clock [get_clocks "${name}_tx_clk_125m"] -max 1 [get_ports "$data_pins"]
    set_output_delay -add_delay -clock [get_clocks "${name}_tx_clk_125m"] -min -1 [get_ports "$data_pins"]
    set_output_delay -add_delay -clock [get_clocks "${name}_tx_clk_125m"] -max 1 -clock_fall [get_ports "$data_pins"]
    set_output_delay -add_delay -clock [get_clocks "${name}_tx_clk_125m"] -min -1 -clock_fall [get_ports "$data_pins"]
```

注意发送侧多一个参数 `clk_src`：`create_generated_clock -source` 要指明输出时钟是从哪个内部寄存器/引脚派生的，这样工具能把 TX 数据路径与转发时钟路径关联起来分析。`-max 1 / -min -1` 表示数据相对 TX 时钟 ±1 ns 的输出延迟需求（即 PHY 端的 setup/hold 要求 1 ns）。

> 区分两个文件：`rgmii_io.sdc`（管脚级 DDR 约束，**由使用者在顶层调用**并传入真实管脚名）与 `rgmii_phy_if.sdc`（模块内部约束，**随模块一起自动应用**）。后者见 4.3 节，目前只含复位同步的 false path（[rgmii_phy_if.sdc:23-32](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/quartus/rgmii_phy_if.sdc#L23-L32)）。

#### 4.1.4 代码实践

**实践目标**：亲手把 `rgmii_io.sdc` 的 RX 侧 setup/hold 约束语句整理出来，并算清楚为什么 DDR 必须分边沿处理。

**操作步骤**（纯源码阅读型，不需要跑综合）：

1. 打开 [syn/quartus/rgmii_io.sdc](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/quartus/rgmii_io.sdc)，定位 `constrain_rgmii_input_pins`。
2. 列出「建钟」语句：第 29 行虚拟时钟、第 34 行真实管脚时钟（注意 `-waveform {2 6}`）。
3. 列出「数据到达」语句：第 42-45 行四条 `set_input_delay`，标注哪些是 `-max`（setup）、哪些是 `-min`（hold）、哪两条带 `-clock_fall`。
4. 列出「切虚假边沿」语句：第 70-71 行（setup，切反向）、第 81-82 行（hold，切同向）。
5. 回答关键问题：**如果删掉带 `-clock_fall` 的两条 `set_input_delay`，会发生什么？**（提示：工具只分析上升沿那一拍数据，下降沿那 4 位变成 unconstrained，DDR 的下半拍没人管。）
6. 回答：**为什么不用一条 `set_input_delay` 覆盖两个边沿，而要靠 `set_false_path` 切四种组合？**（提示：源同步接口里数据参考的是无相移虚拟时钟，采样的是有相移真实时钟，两个时钟边沿错位；若不切，工具会把「虚拟时钟上升沿发出的数据被真实时钟下降沿采样」这类组合也拿来算 setup，得到虚假的负余量。这就是 `-edge_shift` 类技术要解决的本质问题——把参考边沿挪到与数据有效窗口对齐的位置。）

**需要观察的现象**：在一个真实 Quartus 工程里，若漏写第 81-82 行的 hold false path，TimeQuest 会报出大量 rise→rise / fall→fall 的 hold 违规；补上后这些违规消失，只剩同向 setup 与反向 hold 的真实余量。

**预期结果**：你能写出一张表，把 RX 侧的 setup/hold 各自「保留哪两种边沿组合、切掉哪两种」说清楚。

**待本地验证**：以上对 Quartus 报错现象的描述需在真实工程中确认，本仓库 `example/` 下无 Quartus RGMII 工程，故标注待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`-waveform {2 6}` 相对默认的 `{0 4}` 差了多少相位？为什么 RX 时钟要带这个相移？
**答案**：`{2 6}` 把上升沿从 0 ns 挪到 2 ns、下降沿从 4 ns 挪到 6 ns，相当于整体相移 +2 ns（在 8 ns 周期里是 90°）。因为 PHY 输出的 `rx_clk` 相对数据有约 1.5~2 ns 内部延迟，相移后的时钟边沿正好落在数据有效窗口中央，约束里必须反映这个真实相位。

**练习 2**：为什么 setup 的 false path 切的是反向边沿（rise→fall、fall→rise），而 hold 切的是同向边沿？
**答案**：setup 衡量「数据到下一个采样沿前还能留多少时间」，真实的同向关系（本拍数据由本沿或同向沿采样）才是需要保证 setup 的；反向组合是虚假的，切掉。hold 衡量「数据相对采样沿之后还要稳定多久」，对应的是相邻的反向边沿关系，所以反过来切同向。两者一正一反，把四种组合里各自虚假的一半切掉，只留真实的分析。

### 4.2 Vivado Tcl 封装：自动发现与跨时钟域约束

#### 4.2.1 概念说明

Quartus 的约束过程需要使用者手动传入实例路径（如 `constrain_eth_mac_1g_rgmii_inst "u0|eth_inst"`），每实例化一份就要调一次。Vivado 的脚本换了一个更强的思路：**脚本自己用正则去全设计里搜索所有某类模块的实例**，找到几个就约束几个。这样使用者只要把 `.tcl` 加进工程的约束文件列表，所有实例自动被约束，零手动维护。

这套自动发现要解决两类内部路径的约束：

1. **复位同步**：`*_rst_reg` 两级同步器的复位端是异步来的，要设 `ASYNC_REG` 并对复位管脚设 false path，否则工具会按同步路径分析并报违规。
2. **跨时钟域（CDC）数据**：`mii_select`、`rx_prescale`、PTP 时间戳等从源域跨到目的域的位宽总线，要设 `ASYNC_REG`（告诉工具这是同步器第一级，别挪位置、别优化），并用 `set_max_delay -datapath_only` 限定最大延迟（通常取一个源时钟周期），对多 bit 总线还要加 `set_bus_skew` 限制各位之间的相对偏斜。

#### 4.2.2 核心流程：foreach + get_cells 自动发现 → ASYNC_REG + set_max_delay

一个典型的 Vivado 约束脚本骨架：

```tcl
foreach inst [get_cells -hier -regexp -filter {(ORIG_REF_NAME =~ "模块名(__\w+__\d+)?" ||
        REF_NAME =~ "模块名(__\w+__\d+)?")}] {
    # 1. 用 get_cells + 正则定位本实例内的同步器触发器
    # 2. set_property ASYNC_REG TRUE $sync_ffs
    # 3. 查源时钟周期
    # 4. set_max_delay -from 源寄存器 -to 同步器第一级 -datapath_only $周期
}
```

四个关键点：

- `get_cells -hier -regexp -filter`：`-hier` 跨层级、`-regexp` 用正则、`-filter` 按属性筛。`ORIG_REF_NAME`/`REF_NAME` 是单元的原始/当前模块名。
- `(__\w+__\d+)?`：Vivado 在层次优化（hierarchy restructuring）后会给单元名加 `__ModuleName_N` 之类的后缀，这个可选后缀让正则不被它绊倒。
- `ASYNC_REG TRUE`：标记同步器链，防止综合/布局把这几级触发器拆开或重排，是 CDC 的必备属性。
- `set_max_delay -datapath_only`：`-datapath_only` 表示「只算数据路径、不计时钟偏斜」，这正是跨域路径的正确分析方式（跨域没有公共时钟，算偏斜无意义）。约束值通常取一个源时钟周期。

对多位总线跨域，还要再加：

```tcl
set_bus_skew -from $src -to $dst $另一个时钟周期
```

`set_bus_skew` 限制总线各位到达同步器的**最大相对偏斜**（而不是绝对延迟），配合模块内部的 toggle 握手采样，保证整条总线被同一拍采到、不会某几位前一拍某几位后一拍。

#### 4.2.3 源码精读

**例一：复位同步 + 转发时钟路径**（[rgmii_phy_if.tcl:23-42](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/vivado/rgmii_phy_if.tcl#L23-L42)）：

```tcl
foreach inst [get_cells -hier -regexp -filter {(ORIG_REF_NAME =~ "rgmii_phy_if(__\w+__\d+)?" ||
        REF_NAME =~ "rgmii_phy_if(__\w+__\d+)?")}] {
    # reset synchronization
    set reset_ffs [get_cells -hier -regexp ".*/(rx|tx)_rst_reg_reg\\\[[0-9]\\\]" -filter "PARENT == $inst"]
    set_property ASYNC_REG TRUE $reset_ffs
    set_false_path -to [get_pins -of_objects $reset_ffs -filter {IS_PRESET || IS_RESET}]

    # clock output
    set_property ASYNC_REG TRUE [get_cells $inst/clk_oddr_inst/oddr[0].oddr_inst]
    set src_clk [get_clocks -of_objects [get_pins $inst/rgmii_tx_clk_1_reg/C]]
    set src_clk_period [if {[llength $src_clk]} {get_property -min PERIOD $src_clk} {expr 8.0}]
    set_max_delay -from [get_cells $inst/rgmii_tx_clk_1_reg] -to [get_cells $inst/clk_oddr_inst/oddr[0].oddr_inst] -datapath_only [expr $src_clk_period/4]
```

逐行对照 RTL（[rtl/rgmii_phy_if.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/rgmii_phy_if.v)）：

- `rgmii_tx_clk_1_reg` 对应 RTL 第 110-111 行的分频寄存器 `reg rgmii_tx_clk_1 = 1'b1; reg rgmii_tx_clk_2 = 1'b0;`，它们产生发给 PHY 的转发时钟。
- `clk_oddr_inst` 对应 RTL 第 209-214 行用 `oddr` 原语把 `rgmii_tx_clk_1/2` 在双边沿合并输出的实例（`rgmii_phy_if.v:209`）。
- 这条 `set_max_delay ... -datapath_only [expr $src_clk_period/4]` 约束的是「分频寄存器 → 转发时钟 ODDR」这段路径，限值取源时钟周期的 1/4（125 MHz 时即 2 ns），确保转发时钟两半边沿的偏斜受控——这正是发送侧能稳定工作、PHY 能正确采样的前提。
- `[if {...} {get_property -min PERIOD $src_clk} {expr 8.0}]` 是防御式写法：能查到源时钟就用真实周期，查不到就回退到 8 ns（125 MHz）。

**例二：MAC 顶层三条 CDC 路径**（[eth_mac_1g_rgmii.tcl:23-62](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/vivado/eth_mac_1g_rgmii.tcl#L23-L62)）。这里约束的是 `mii_select_reg` 跨到 `tx/rx` 域、以及 `rx_prescale` 跨域。以 TX 侧为例：

```tcl
    set select_ffs [get_cells -hier -regexp ".*/tx_mii_select_sync_reg\\\[\\d\\\]" -filter "PARENT == $inst"]
    if {[llength $select_ffs]} {
        set_property ASYNC_REG TRUE $select_ffs
        set src_clk [get_clocks -of_objects [get_pins $inst/mii_select_reg_reg/C]]
        set src_clk_period [if {[llength $src_clk]} {get_property -min PERIOD $src_clk} {expr 8.0}]
        set_max_delay -from [get_cells $inst/mii_select_reg_reg] -to [get_cells $inst/tx_mii_select_sync_reg[0]] -datapath_only $src_clk_period
    }
```

对照 RTL（[rtl/eth_mac_1g_rgmii.v:112-125](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_rgmii.v#L112-L125)）：

```verilog
reg mii_select_reg = 1'b0;          // 源域寄存器
...
reg [1:0] tx_mii_select_sync = 2'd0; // 两级同步器
always @(posedge tx_clk) tx_mii_select_sync <= {tx_mii_select_sync[0], mii_select_reg};
```

`-to ... tx_mii_select_sync_reg[0]` 精确指向同步器的**第一级**（`ASYNC_REG` 也只标在同步器上），`-from ... mii_select_reg_reg` 指向源域寄存器，限值取一个源时钟周期。`rx_prescale` 路径（第 51-61 行）写法完全相同，只是源寄存器换成 `rx_prescale_reg[2]`（[eth_mac_1g_rgmii.v:129-139](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_rgmii.v#L129-L139)）。注意每段都包了 `if {[llength $select_ffs]}`——因为 `eth_mac_1g_rgmii` 在某些参数配置下可能不实例化某同步器，缺省时跳过，避免 `get_cells` 返回空时报错。

**例三：PTP 跨域的 set_bus_skew**（[ptp_clock_cdc.tcl:35-48](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/vivado/ptp_clock_cdc.tcl#L35-L48)）。这里时间戳是多位总线跨域，所以除了 `set_max_delay` 还要 `set_bus_skew`：

```tcl
    set_max_delay -from [get_cells "$inst/src_ts_ns_capt_reg_reg[*]"] -to [get_cells "$inst/src_ts_ns_sync_reg_reg[*]"] -datapath_only $output_clk_period
    set_bus_skew  -from [get_cells "$inst/src_ts_ns_capt_reg_reg[*]"] -to [get_cells "$inst/src_ts_ns_sync_reg_reg[*]"] $input_clk_period
```

`src_ts_*_capt` 是源域捕获寄存器，`src_ts_*_sync` 是目的域同步器。`set_max_delay` 用目的域周期限绝对延迟、`set_bus_skew` 用源域周期限各位相对偏斜——二者配合，保证 96 位 ToD 时间戳在跨域时被整体采样、不会撕裂。秒位（`src_ts_s`）和 step 位都加了 `if {[llength ...]}`，因为这些字段在 64 位相对格式下不存在（参见 u11-l1 讲的两种时间戳格式）。

#### 4.2.4 代码实践

**实践目标**：搞清楚「自动发现」为什么能让约束零维护，并追踪一条 CDC 约束从源寄存器到同步器的完整对应关系。

**操作步骤**（源码阅读 + 文本检索型）：

1. 在 [syn/vivado/](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/vivado/) 下，统计有多少个 `.tcl` 脚本以 `foreach inst [get_cells -hier -regexp ...]` 开头，验证「自动发现」是本库 Vivado 约束的统一模式。
2. 打开 [eth_mac_1g_rgmii.tcl](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/vivado/eth_mac_1g_rgmii.tcl)，找到 TX `mii_select` 那段（第 27-37 行）。
3. 打开 [rtl/eth_mac_1g_rgmii.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_rgmii.v)，在第 112 行找到 `mii_select_reg`、第 115 行找到 `tx_mii_select_sync`，确认约束里的 `mii_select_reg_reg` 与 `tx_mii_select_sync_reg[0]` 正好对应源寄存器和同步器第一级。
4. 回答：**如果把 `ASYNC_REG TRUE` 这行删掉，工具可能出什么问题？**（提示：综合时可能把同步器两级触发器合并或重排位置，破坏跨域采样的可靠性。）
5. 回答：**为什么 `set_max_delay` 要加 `-datapath_only`？不加会怎样？**（提示：跨域路径两端时钟无公共源头，算时钟偏斜没有物理意义；不加会得到偏大且无意义的余量计算。）

**需要观察的现象**：约束里的对象名（`*_reg_reg`、`[*]`、`[0]`）能在 RTL 里逐个对上，且每条 `set_max_delay` 的 `-from/-to` 都正好是「源域寄存器 → 同步器第一级」。

**预期结果**：你能画出一条「`mii_select_reg`（源域）→ `tx_mii_select_sync[0]`（同步级 1，标 ASYNC_REG）→ `tx_mii_select_sync[1]`（同步级 2）」的路径图，并标出 `set_max_delay` 作用在哪一段。

#### 4.2.5 小练习与答案

**练习 1**：`get_cells -hier -regexp -filter` 里的 `(__\w+__\d+)?` 可选后缀是干什么用的？
**答案**：Vivado 在综合/层次优化后，会把某些单元的名字加上形如 `__FooBar_3` 的后缀（用来区分被复制或重构的实例）。这个可选后缀让正则在「带后缀」和「不带后缀」两种情况下都能匹配到实例，保证自动发现不会漏。

**练习 2**：`ptp_clock_cdc.tcl` 里为什么对时间戳总线既写 `set_max_delay` 又写 `set_bus_skew`，而 `eth_mac_1g_rgmii.tcl` 里 `mii_select` 只写 `set_max_delay`？
**答案**：`mii_select` 是单 bit 信号，没有「各位之间相对偏斜」的问题，只要限制绝对延迟即可。时间戳是多 bit 总线，跨域时若各位到达同步器的时间差太大，会落在不同拍被采样导致数据撕裂，所以额外用 `set_bus_skew` 把各位的相对偏斜限制在一个源时钟周期内，配合 toggle 握手实现整体采样。

### 4.3 跨工具链约束差异

#### 4.3.1 概念说明

同一份 RTL 要能在 Intel（Altera）和 AMD（Xilinx）两家、且 Intel 新旧两代工具上都能综合，约束就必须按工具各自的方言来写。本库 `syn/` 下分了三档目录，对应三种方言。理解这些差异，是为了在把本库移植到一块新板子（尤其换了工具版本）时，知道哪些地方要改、改成什么。

三者的本质区别在于两点：**怎么定位待约束的对象**，以及**怎么表达 CDC 的延迟限值**。

#### 4.3.2 核心流程：三种方言对照

- **Quartus（经典）**：用 `.sdc`，过程 `constrain_<模块>_inst { inst }` 接收使用者传入的实例层次路径；对象查询用 `get_registers "inst|reg[*]"`（竖线 `|` 是 Quartus 层次分隔符）；CDC 限值用 `set_max_delay -from ... -to ... 8.000`，把周期**写死成 8 ns**。
- **Quartus Pro**：同样 `.sdc`、同样过程签名，但 CDC 限值改用 `set_data_delay ... -get_value_from_clock_period dst_clock_period -value_multiplier 0.8`——**从目的时钟周期动态推导**（取 80%），不再写死 8 ns，移植性更好。
- **Vivado**：用 `.tcl`（XDC），靠 `get_cells -hier -regexp` **自动发现**所有实例（使用者无需传实例路径）；CDC 用 `set_max_delay -datapath_only` + Xilinx 专有属性 `ASYNC_REG`、`set_bus_skew`，周期用 `get_property -min PERIOD` 动态获取。

#### 4.3.3 源码精读

**Quartus 经典**（[eth_mac_1g_rgmii.sdc:23-34](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/quartus/eth_mac_1g_rgmii.sdc#L23-L34)）：

```tcl
proc constrain_eth_mac_1g_rgmii_inst { inst } {
    # MII select sync
    set_max_delay -from [get_registers "$inst|mii_select_reg"] -to [get_registers "$inst|tx_mii_select_sync[0]"] 8.000
    set_max_delay -from [get_registers "$inst|mii_select_reg"] -to [get_registers "$inst|rx_mii_select_sync[0]"] 8.000
    # RX prescale sync
    set_max_delay -from [get_registers "$inst|rx_prescale[2]"] -to [get_registers "$inst|rx_prescale_sync[0]"] 8.000
    constrain_rgmii_phy_if_inst "$inst|rgmii_phy_if_inst"
}
```

注意最后一句 `constrain_rgmii_phy_if_inst "$inst|rgmii_phy_if_inst"`——MAC 约束过程**手动链式调用**了子模块 PHY IF 的约束过程，把子实例路径拼接进去（`$inst|rgmii_phy_if_inst` 对应 RTL [eth_mac_1g_rgmii.v:191](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f1ffb/rtl/eth_mac_1g_rgmii.v#L191) 的 `rgmii_phy_if_inst` 例化名）。这是 Quartus 风格：层次关系靠手动拼接传递。`8.000` 是写死的 125 MHz 周期。

**Quartus Pro**（[eth_mac_1g_rgmii.sdc:27-31](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/quartus_pro/eth_mac_1g_rgmii.sdc#L27-L31)）：

```tcl
    set_data_delay -from [get_registers "$inst|mii_select_reg"] -to [get_registers "$inst|tx_mii_select_sync[0]"] -override -get_value_from_clock_period dst_clock_period -value_multiplier 0.8
```

同样三条路径、同样对象名，但把 `set_max_delay 8.000` 换成了 `set_data_delay ... -get_value_from_clock_period dst_clock_period -value_multiplier 0.8`。含义：「限值 = 目的时钟周期 × 0.8」，工具自动按实际时钟算，不再写死。这是 Quartus Pro（基于新引擎）推荐的、与时钟频率解耦的写法。

**Vivado** 的等价约束见 4.2.3，用的是 `set_max_delay -datapath_only $src_clk_period`（动态取周期）+ `ASYNC_REG`，且无需手动拼接子实例路径——`foreach` 会分别自动发现 MAC 和 PHY IF 的实例各自约束。

三档对照表：

| 维度 | Quartus（经典） | Quartus Pro | Vivado |
|------|-----------------|-------------|--------|
| 文件 | `.sdc` | `.sdc` | `.tcl`（XDC） |
| 对象查询 | `get_registers "inst\|reg"` | 同左 | `get_cells -hier -regexp` |
| 实例定位 | 使用者传 `inst` 路径 | 同左 | 脚本自动发现 |
| CDC 限值 | `set_max_delay 8.000`（写死） | `set_data_delay ... -value_multiplier 0.8`（动态） | `set_max_delay -datapath_only $period`（动态） |
| CDC 辅助属性 | 无 | 无 | `ASYNC_REG`、`set_bus_skew` |
| 子模块约束 | 手动链式调用过程 | 同左 | 各自 `foreach` 自动发现 |

#### 4.3.4 代码实践

**实践目标**：把同一逻辑（`mii_select` CDC 路径）在三种工具下的约束写法并排比对，亲手发现「写死 vs 动态」「手动传路径 vs 自动发现」的区别。

**操作步骤**：

1. 打开三份文件：
   - [syn/quartus/eth_mac_1g_rgmii.sdc](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/quartus/eth_mac_1g_rgmii.sdc) 第 27 行；
   - [syn/quartus_pro/eth_mac_1g_rgmii.sdc](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/quartus_pro/eth_mac_1g_rgmii.sdc) 第 27 行；
   - [syn/vivado/eth_mac_1g_rgmii.tcl](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/vivado/eth_mac_1g_rgmii.tcl) 第 27-37 行。
2. 确认三者的 `-from/-to` 对象名（`mii_select_reg`、`tx_mii_select_sync[0]`）一致——**约束的对象是工具无关的，差异只在限值表达和定位方式**。
3. 回答：**若把这块 MAC 跑在 100 MHz（而非 125 MHz）的逻辑时钟下，三份约束哪份需要手改、哪份不用改？**
   - 提示：Quartus 经典写死 8 ns，若实际周期变化需手改；Quartus Pro 与 Vivado 都从时钟周期动态推导，不用改。

**需要观察的现象**：三份文件针对的是同一条 RTL 路径，但限值一个写死、两个动态。

**预期结果**：你能填出上面的三档对照表，并说清「把本库用于一块新板子时，如果是 Vivado 流程，基本只需把 `.tcl` 加进约束列表；如果是 Quartus 经典流程，还要在顶层 SDC 里为每个实例手动调用 `constrain_*_inst` 过程并留意写死的周期值」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Quartus 的 `constrain_eth_mac_1g_rgmii_inst` 要在末尾显式调用 `constrain_rgmii_phy_if_inst "$inst|rgmii_phy_if_inst"`，而 Vivado 不需要类似的链式调用？
**答案**：Quartus 约束靠使用者传入实例路径，过程只约束自己模块内的对象，子模块的约束必须手动拼接子实例路径再调一次子模块过程。Vivado 的 `foreach` 会独立扫描整个设计，自动找到 `rgmii_phy_if` 和 `eth_mac_1g_rgmii` 各自的所有实例分别约束，不需要人为串接层次。

**练习 2**：Quartus 经典写 `set_max_delay ... 8.000`，Quartus Pro 写 `-value_multiplier 0.8`。后者取 0.8 而非 1.0 的工程意义是什么？
**答案**：0.8 表示把限值设为目的时钟周期的 80%，留出 20% 余量给工具/工艺变化和同步器两级之间的不确定性。这比经典版写死一个绝对纳秒值更稳健，也随实际时钟频率自动缩放，换速率不用改约束。

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「约束审计」：

1. **画出两类约束的分层关系**：在一张图上标出「管脚级 DDR 约束（`rgmii_io.sdc`，使用者调）」与「模块内部约束（`rgmii_phy_if.sdc` / `*.tcl`，随模块自动应用）」分别覆盖哪些对象，说明为什么管脚级必须由板级工程提供（因为只有板级才知道真实管脚名）。
2. **追踪一条完整 RX 链路的约束覆盖**：从 `rx_clk` 管脚进来的时钟 → `rgmii_phy_if` 内部 DDR 采样 → 跨到 `logic_clk` 域。指出这条链路上，哪段靠 `rgmii_io.sdc` 的源同步 DDR 约束保证、哪段靠 Vivado 的 `ASYNC_REG` + `set_max_delay` 保证。
3. **跨工具改写练习**：把 [syn/quartus/eth_mac_1g_rgmii.sdc](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/quartus/eth_mac_1g_rgmii.sdc) 的 `rx_prescale` 那条 `set_max_delay ... 8.000`，分别改写成 Quartus Pro 风格（`set_data_delay ... -value_multiplier`）和 Vivado 风格（`set_max_delay -datapath_only` + `ASYNC_REG`），体会三者表达力的差异。
4. **回答总问题**：本库为什么把约束拆成「RTL 内部（自动应用）」和「IO（板级提供）」两层，而不是全写在一份顶层约束里？（提示：可复用性——内部约束与板子无关、可随模块分发；IO 约束与具体 PCB 走线和 PHY 型号强绑定，只能板级定制。）

如果你装了 Vivado，可进一步：把 [example/KC705/fpga_rgmii/fpga/Makefile](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/KC705/fpga_rgmii/fpga/Makefile) 第 49-53 行列出的 `XDC_FILES`（`rgmii_phy_if.tcl`、`eth_mac_1g_rgmii.tcl`、`eth_mac_fifo.tcl` 等）跑一次综合，打开 timing report，找到 `mii_select` 那条 CDC 路径，确认它被归类为「被 `set_max_delay -datapath_only` 约束的跨时钟域路径」而非普通 intra-clock 路径。

> 说明：本仓库 `example/KC705` 等板级工程在该 vendoring 布局下，约束脚本路径形如 `lib/eth/syn/vivado/...`；在本独立仓库根目录下则直接是 `syn/vivado/...`。两处文件内容一致，仅前缀不同。

**待本地验证**：步骤 4 的 Vivado timing report 观察需在装有 Vivado 与 KC705（或兼容）工程的环境中完成，仓库本身不含综合结果，故标注待本地验证。

## 6. 本讲小结

- 以太网 PHY 接口是**源同步接口**：采样时钟与数据一起从 PHY 进 FPGA，必须用约束显式描述二者相位，否则时序分析无意义。
- RGMII 是 **DDR**，约束用「虚拟时钟 + 带 `-clock_fall` 的 `set_input_delay` + 分边沿 `set_false_path`」三件套建模两个边沿；这等价于 `-edge_shift` 类技术要解决的「只保留真实采样关系」问题。
- 本库约束分两层：**IO 层**（`rgmii_io.sdc`，板级调用、传真实管脚名）与**模块内部层**（`rgmii_phy_if.sdc` / `*.tcl`，自动应用，覆盖复位同步与 CDC）。
- Vivado 脚本靠 `get_cells -hier -regexp` **自动发现**所有实例，用 `ASYNC_REG` + `set_max_delay -datapath_only` 约束 CDC，多位总线再配 `set_bus_skew`——加进 XDC 列表即零维护。
- 三种工具方言差异：Quartus 经典写死 `set_max_delay 8.000` 且手动传实例路径；Quartus Pro 用 `set_data_delay -value_multiplier 0.8` 动态推导；Vivado 自动发现 + 动态取周期 + Xilinx 专有属性。
- 约束对象名（`mii_select_reg`、`*_sync_reg[0]`、`clk_oddr_inst` 等）与工具无关，都能在 RTL 里逐一对上，这是跨工具可移植的基础。

## 7. 下一步学习建议

- **横向扩展阅读**：用本讲的「自动发现 + CDC 约束」套路去读其余 Vivado 脚本——[eth_mac.tcl](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/vivado/eth_mac.tcl)、[gmii_phy_if.tcl](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/vivado/gmii_phy_if.tcl)、[ptp_td_leaf.tcl](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/syn/vivado/ptp_td_leaf.tcl)，验证模式是否一致。
- **回到 RTL 验证**：重读 u4-l4、u4-l5 的 `rgmii_phy_if` / `iddr` / `oddr`，对照本讲的转发时钟 `set_max_delay` 约束，理解「clk90 移相 + DDR 原语」与「转发时钟路径约束」是如何配合保证发送侧时序的。
- **进阶到 10G**：10G/25G 用 SERDES 接口而非源同步 RGMII，约束形态完全不同（靠 SERDES IP 自带的约束、`eth_phy_10g` 的块锁定见 u10-l2）；可对比 RGMII 源同步约束与 10G SERDES 约束的差异。
- **真正跑一遍**：选一个与本仓库约束脚本匹配的板级工程（如 `example/KC705/fpga_rgmii`）做一次综合，在 timing report 里验证本讲描述的 CDC 路径分类是否如预期，这是把「读约束」变成「会调约束」的关键一步。
