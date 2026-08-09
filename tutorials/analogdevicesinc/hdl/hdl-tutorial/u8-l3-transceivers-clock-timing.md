# 收发器、时钟与时序约束

## 1. 本讲目标

本讲是专家层「测试、规范与高级主题」的一篇，聚焦高速设计里最容易卡住人的三件事：**收发器（GT）怎么连**、**GT IP 怎么自动生成**、**跨时钟域与时序怎么收敛**。

学完后你应该能够：

- 说清 `ad_xcvrcon` / `ad_xcvrpll` 这两个 Tcl 助手把「收发器物理核 → 收发器配置核 → JESD204 链路核」三者、以及它们的时钟（lane clock、device clock、link clock）和复位如何织成一张完整的串行通路。
- 描述 `gtwizard_generator.tcl` 如何用纯 Tcl 把 lane rate 反推出参考时钟候选、再调 Vivado 的 `gtwizard` / `gtwizard_ultrascale` IP 生成 GT，并理解它和工程里常用的 `xcvr_automation.tcl` 的分工。
- 解释 `auto_timing_fix_xilinx.tcl` 作为 `ROUTE_DESIGN` 之后的钩子脚本（POST_ROUTE_SCRIPT）怎样在布线后自动修时序，以及 IP 级 `ttcl`/`sdc` 约束如何处理跨时钟域。

## 2. 前置知识

本讲默认你已经读过：

- **u3-l4 板级连线助手 Tcl：adi_board.tcl**——知道 `ad_connect`、`ad_ip_instance`、`ad_cpu_interconnect` 等块设计原语的语义。
- **u6-l1 JESD204 框架**——知道 JESD204 分「物理 / 链路 / 传输 / 应用」四层，`LINK_MODE` 区分 JESD204B（8B10B）与 JESD204C（64B66B），以及 lane、link clock、device clock、SYSREF 等概念。

下面补充两个本讲会用到的术语：

- **GT（Gigabit Transceiver）**：FPGA 芯片里自带的千兆串行收发器硬核（Xilinx 家族里叫 GTX/GTH/GTY，按代际命名 GTXE2、GTHE3/4、GTYE3/4 等）。JESD204 的物理层就跑在它上面。它不是 RTL 写出来的，而是通过 Vivado 的 `gtwizard`（7 系列）或 `gtwizard_ultrascale`（UltraScale/UltraScale+）IP「实例化」出来。
- **PLL（Phase-Locked Loop，锁相环）**：把一个低速参考时钟（reference clock，通常几十到几百 MHz）倍频成 lane rate（几 Gbps 到十几 Gbps）的电路。GT 里分 **CPLL**（每通道一个，per-channel）和 **QPLL**（一个管四条通道，quad-shared）两种。
- **WNS / TNS**：时序报告里的核心指标。WNS（Worst Negative Slack，最差负裕量）是整张设计里最紧的那条路径的裕量，负值表示时序违例；TNS（Total Negative Slack）是所有违例路径裕量之和。`phys_opt_design` 是 Vivado 的物理综合优化，能局部挪寄存器、复制触发器来修这些违例。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [projects/scripts/adi_board.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl) | 块设计连线助手库；本讲聚焦其中的 `ad_xcvrcon`（收发器连线）与 `ad_xcvrpll`（PLL 时钟/复位扇出）。 |
| [projects/scripts/gtwizard_generator.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/gtwizard_generator.tcl) | 独立工具脚本：给定 lane rate 与 PLL 类型，反推参考时钟并调用 Vivado 生成 GT IP。 |
| [projects/scripts/auto_timing_fix_xilinx.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/auto_timing_fix_xilinx.tcl) | 布线后自动修时序的钩子脚本（Auto Timing Fix, ATF）。 |
| [projects/fmcomms2/zcu102/system_constr.xdc](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_constr.xdc) | 工程级约束：引脚分配、电平标准与板级时钟声明。 |
| [library/axi_dmac/axi_dmac_constr.sdc](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_constr.sdc) | IP 级 Intel 侧约束（跨时钟域 false path）。 |
| [library/axi_dmac/axi_dmac_constr.ttcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_constr.ttcl) | IP 级 Xilinx 侧约束的 Tcl 模板（按参数生成 `.xdc`）。 |
| [projects/scripts/adi_project_xilinx.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl) | 工程流程 Tcl；把 ATF 脚本挂到 `impl_1` 的 `ROUTE_DESIGN.TCL.POST`。 |
| [projects/daq2/common/daq2_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/daq2/common/daq2_bd.tcl) | 真实 JESD204 工程的块设计脚本，演示 `ad_xcvrcon`/`ad_xcvrpll` 的实际调用。 |

---

## 4. 核心概念与源码讲解

### 4.1 ad_xcvrcon 收发器连线

#### 4.1.1 概念说明

一条 JESD204 串行通路在块设计（BD）里通常由三个 IP 协同：

1. **`util_adxcvr`**——收发器**物理核**。它包裹 Vivado 生成的 GT wizard，对外暴露串行差分引脚（`rx_n/rx_p`、`tx_n/tx_p`）、恢复出来的并行时钟（`rx_out_clk_*`/`tx_out_clk_*`）以及 DRP/复位等管理接口。
2. **`axi_adxcvr`**——收发器**配置核**。CPU 经 AXI4-Lite 读写它，用来复位 PLL、复位通道、查询 `reset_done` 等状态。
3. **`axi_jesd204_rx`/`tx`**——JESD204**链路核**。消费物理核送来的并行数据与时钟，完成 8B10B/64B66B 解码、ILAS 对齐等链路层工作。

这三者之间的连线既多又机械：每条 lane 都要把物理核的并行数据接到链路核、把配置核的状态/复位接到物理核、还要把 SYSREF/sync/device_clk/link_clk 这几路时钟对齐。`ad_xcvrcon` 就是把这一坨连线**按 lane 数循环展开**的高阶原语。它的口号是：**你只告诉我三个 IP 的名字和 lane 映射，剩下我来连。**

#### 4.1.2 核心流程

`ad_xcvrcon` 的执行过程可以概括为四步：

```text
ad_xcvrcon  u_xcvr  a_xcvr  a_jesd  [lane_map]  [link_clk]  [device_clk]  [num_of_max_lanes] ...
   │
   ├─ 1. 读三个 IP 的配置参数
   │     QPLL_ENABLE / TX_OR_RX_N / XCVR_TYPE / LINK_MODE / NUM_LANES
   │     → 判定方向(tx/rx)、是否走 204C 双倍时钟、本链路几条 lane
   │
   ├─ 2. 决定时钟源
   │     link_clk   默认取 u_xcvr/rx_out_clk_0（lane rate 的分频）
   │     device_clk 默认 = link_clk
   │     → 为该时钟建一个 proc_sys_reset 复位生成器（每时钟一个，去重）
   │
   ├─ 3. 按 lane 循环连线（核心）
   │     对每条逻辑 lane n（映射到物理 lane phys_lane）：
   │       · a_xcvr/up_ch_n     → u_xcvr/up_rx|tx_<phys>   （配置↔物理核通道管理）
   │       · a_jesd/<dir>_phy_n → u_xcvr/rx|tx_<phys>       （链路层并行数据）
   │       · link_clk           → u_xcvr/rx|tx_clk_<phys>   （每通道恢复时钟）
   │       · 串行差分引脚        → BD 顶层 rx_data_<m>_p/n
   │
   └─ 4. 把 link/device 时钟、SYSREF、sync 接到 a_jesd
         a_jesd/device_clk ← device_clk
         a_jesd/link_clk   ← link_clk
         a_jesd/sysref     ← 顶层 sysref 端口
         a_jesd/sync       ← 顶层 sync 端口（仅 8B10B / subclass 需要）
```

其中最关键的「时钟层级」是理解收发器时序的钥匙：lane rate 是串行比特率（如 10 Gbps）；GT 把它分频出 **link clock**（链路层与 transport 层用的并行时钟，等于 `lane_rate / (编码率 × 数据位宽)`）；**device clock** 则是给数据转换器（ADC/DAC）采样用的、与链路同步的时钟。这三者必须同源或有确定的频率比，否则 JESD 链路无法同步。

#### 4.1.3 源码精读

`ad_xcvrcon` 的签名与文档注释定义了全部 8 个参数，先看它的「契约」：

- [adi_board.tcl:L298-L319](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl#L298-L319)——注释解释 `u_xcvr`/`a_xcvr`/`a_jesd` 三个核心参数、`lane_map`（逻辑 lane 到物理 lane 的映射）以及 `link_clk` 的语义：「应为 lane rate / (编码率 × 数据位宽)，其中编码率 8B10B 为 10/8、64B66B 为 66/64」。第 319 行是过程签名本体。

进入函数体，第一段先**读参数、定方向**：

```tcl
set qpll_enable  [get_property CONFIG.QPLL_ENABLE  [get_bd_cells $a_xcvr]]
set tx_or_rx_n   [get_property CONFIG.TX_OR_RX_N   [get_bd_cells $a_xcvr]]
set link_mode    [get_property CONFIG.LINK_MODE    [get_bd_cells $u_xcvr]]
# ...
if {$tx_or_rx_n == 1} {   ;# TX 方向：数据输出、控制输入
    set txrx "tx"; set data_dir "O"; set ctrl_dir "I"; set index $xcvr_tx_index
}
```

见 [adi_board.tcl:L326-L365](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl#L326-L365)：它靠 `TX_OR_RX_N` 区分收发方向，并据此翻转数据/控制端口的方向（`I`/`O`）。`xcvr_index`/`xcvr_tx_index`/`xcvr_rx_index` 这组全局变量用来在同一个 `util_adxcvr` 上挂多个链路时给端口名加序号，避免重名。

第二段是**时钟选择**，里面藏着一条针对 JESD204C 的特殊逻辑：

- [adi_board.tcl:L393-L409](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl#L393-L409)——当 `LINK_MODE == 2`（即 JESD204C / 64B66B）且收发器是特定 GTH 型号（`xcvr_type == 5 || 8`）时，PCS 需要一个 2 倍频率的时钟来驱动，于是函数把「主时钟」取成 2x 的 `tx_out_clk`、把「link clock」取成它的 `div2` 输出。这正解释了「lane 时钟、device 时钟与 JESD 接口」三者如何被关联起来：**它们最终都源自 GT 自己恢复出来的 `out_clk`，只是经过不同的分频抽头**。

第三段（设备时钟与复位生成器）：

- [adi_board.tcl:L418-L430](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl#L418-L430)——`device_clk` 默认等于 `link_clk`；若调用者另传了 `device_clk`，就改用它。无论哪种，都为该时钟实例化一个 `proc_sys_reset`（复位同步器），并用 `get_bd_cells -quiet` 判重，**保证一个时钟只建一个复位生成器**。

第四段是**逐 lane 循环**，把物理核、配置核、链路核三方接起来（节选最典型的分支）：

```tcl
for {set n 0} {$n < $no_of_lanes} {incr n} {
  # ... 计算 phys_lane（受 lane_map 控制）...
  ad_connect  ${a_xcvr}/up_ch_${n}        ${u_xcvr}/up_${txrx}_${phys_lane}   ;# 配置核↔物理核
  ad_connect  ${link_clk}                 ${u_xcvr}/${txrx}_clk_${phys_lane} ;# 每通道时钟
  ad_connect  ${u_xcvr}/${txrx}_${phys}   ${a_jesd}/${txrx}_phy${n}           ;# 链路层数据
  create_bd_port -dir ${data_dir} ${m_data}_${m}_p                          ;# 顶层串行差分引脚
  ad_connect  ${u_xcvr}/${txrx}_${m}_p      ${m_data}_${m}_p
}
```

见 [adi_board.tcl:L489-L529](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl#L489-L529)。注意它还会按 `%4 == 0` 把 `up_cm_*`（common/quad 级管理，仅 QPLL 时）接上——因为 QPLL 是 4 通道共享的，每 4 条 lane 才需要接一个 common 管理。

最后一段把**链路核的全局信号**接好：

- [adi_board.tcl:L557-L570](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl#L557-L570)——把 `sysref`、`sync`（仅 8B10B）、`device_clk`、`link_clk` 接到 `a_jesd`。这一步正是把「时钟体系」和「JESD 接口」正式挂钩：device_clk 喂给链路核的 `core_clk`，link_clk 喂给 `link_clk`，二者在链路核内部一起参与 LMFC（Local Multi-frame Clock）的建立。

`ad_xcvrpll` 则是**配套的时钟/复位扇出助手**：

- [adi_board.tcl:L586-L591](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl#L586-L591)——它用一个 `foreach` 遍历 `m_dst`（通常是带通配符的一组 PLL 引脚，如 `qpll_ref_clk_*`），把同一个时钟/复位源 `m_src` 扇出到所有这些引脚。这就是「一个参考时钟喂给 quad 里所有 PLL」的实现。

#### 4.1.4 代码实践

**实践目标**：在真实工程里追踪一条完整的收发器时钟/数据通路。

**操作步骤**：

1. 打开 [projects/daq2/common/daq2_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/daq2/common/daq2_bd.tcl)，找到 [L151-L157](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/daq2/common/daq2_bd.tcl#L151-L157) 的四行 `ad_xcvrpll`。
2. 注意 [daq2_bd.tcl:L40-L44](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/daq2/common/daq2_bd.tcl#L40-L44)：TX 侧 `axi_ad9144_xcvr` 设了 `QPLL_ENABLE 1`；[daq2_bd.tcl:L87-L91](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/daq2/common/daq2_bd.tcl#L87-L91)：RX 侧 `axi_ad9680_xcvr` 设了 `QPLL_ENABLE 0`。
3. 对照看 L154-L157：TX 走 `qpll_ref_clk_*`、RX 走 `cpll_ref_clk_*`；PLL 复位也分别走 `up_qpll_rst_*` 与 `up_cpll_rst_*`。

**需要观察的现象**：同一个 `util_daq2_xcvr` 物理核里，TX 与 RX **可以混用不同的 PLL 类型**（TX 用 QPLL、RX 用 CPLL），因为参考时钟和复位引脚是按 PLL 类型分组、各自独立的。

**预期结果**：你能用一段话说明 daq2 这条 TX 链路里 `tx_ref_clk_0` 这一个参考时钟如何经 `ad_xcvrpll` 扇出到 `qpll_ref_clk_*`，再经 `ad_xcvrcon` 内部把恢复出的 `tx_out_clk_0` 同时作为 link clock 与 device clock 接到 `axi_ad9144_jesd`。

> 待本地验证：若你有 daq2 硬件或能在 Vivado 里打开生成的工程，可在 BD 里点击 `util_daq2_xcvr` 的 `tx_out_clk_0`，看它同时驱动了 `axi_ad9144_jesd/link_clk`、`axi_ad9144_tpl/link_clk`、`axi_ad9144_upack/clk` 等多个模块。

#### 4.1.5 小练习与答案

**练习 1**：`ad_xcvrcon` 里 `link_clk` 和 `device_clk` 都不传时，它们分别取什么值？为什么可以不传？

参考答案：`link_clk` 默认取 `${u_xcvr}/${txrx}_out_clk_${index}`（即 GT 第 0 通道恢复出的并行时钟）；`device_clk` 默认就等于这个 `link_clk`。可以不传是因为大多数 JESD204 设计里 device clock 与 link clock 同频或可由同一时钟派生，让二者共用 GT 恢复时钟最简单也最安全（保证二者同源）。

**练习 2**：为什么 [adi_board.tcl:L426-L430](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl#L426-L430) 在建 `proc_sys_reset` 之前要用 `get_bd_cells -quiet` 判重？

参考答案：因为一个链路可能有多条 lane、循环里会多次进入这段代码，但**同一个时钟只需要一个复位同步器**。不判重就会重复实例化同名 `*_rstgen` 单元导致 BD 报错。

---

### 4.2 gtwizard 自动生成

#### 4.2.1 概念说明

`gtwizard_generator.tcl` 是一个**独立的实验性工具脚本**，目的不是给工程用，而是给开发者「批量试算」GT 配置用的：你给它一个 lane rate（比如 10 Gbps）和想用的 PLL 类型（CPLL/QPLL0/QPLL1），它就反推出所有合法的参考时钟候选，并对每种组合调一次 Vivado 的 `gtwizard` IP 生成一个 GT 实例，最后可选地用配套的 `gtwiz_parser.pl` 把「非默认属性」抽出来写成一个配置文件。

它和工程里真正用的 `xcvr_automation.tcl`（见 daq2_bd.tcl 顶部 `source .../xcvr_automation.tcl`）是**两条不同的路径**：

| | `xcvr_automation.tcl`（工程主路径） | `gtwizard_generator.tcl`（实验路径） |
| --- | --- | --- |
| 何时跑 | 每次构建工程 | 开发者手动调用 |
| 产出 | 工程内联的 GT 配置 | 一堆供观察的 GT IP + `*_cfng.txt` |
| 配套 | `util_adxcvr` 自动参数化 | `gtwiz_parser.pl` 文本解析 |

理解这条实验路径的价值在于：它把「lane rate → 参考时钟」这层物理换算关系**完全暴露成 Tcl 算式**，是学习 GT 时钟体系最好的活教材。

#### 4.2.2 核心流程

GT 的 PLL 本质是一个分频/倍频组合，满足固定的频率方程。对 **CPLL**：

\[
f_{\text{LineRate}} = \frac{f_{\text{REF}} \times (FBDIV \times FBDIV\_45)}{REFCLK\_DIV \times OUT\_DIV}
\]

对 **QPLL**（不含小数 N）：

\[
f_{\text{LineRate}} = \frac{(f_{\text{REF}} \times FBDIV) \times 2}{REFCLK\_DIV \times OUT\_DIV \times 2} \times \frac{2}{OUT\_DIV}
\]

脚本的策略是**枚举**：把所有合法的 `OUT_DIV`/`REFCLK_DIV`/`FBDIV`/`FBDIV_45` 组合试一遍，凡能使 VCO 落在器件允许频段、且算出的参考时钟 ≤ 800 MHz 的，都收进候选列表。整体流程：

```text
ad_gth_generator  lane_rate_l  pll_type  [ref_clk_l]  [jesd_mode] ...
   │
   ├─ 1. 按 PART 前缀查表选 gt_type（GTXE2/GTHE3/GTYE3/GTHE4/GTYE4…）
   ├─ 2. 按 pll_type 选 VCO 频段（min/max range）；64B66B 模式频段 ×2
   ├─ 3. 对每个 lane_rate：
   │      若没给 ref_clk_l，调 cpll_ref_clk_gen / qpll_ref_clk_gen 枚举出候选
   │      对每个 ref_clk：
   │        组 IP 名 "<gt>_<pll>_<laneRate>_<refClk>"
   │        若该 IP 未存在：
   │          create_ip  gtwizard(7系) / gtwizard_ultrascale(US/US+)
   │          set_property -dict { 线速率/PLL/编码/数据位宽/comma… }
   │          generate_target + create_ip_run
   └─ （由 get_diff_params 调）gtwiz_parser.pl 抽取非默认属性 → *_cfng.txt
```

#### 4.2.3 源码精读

CPLL 的参考时钟枚举器，开头注释就是频率方程：

- [gtwizard_generator.tcl:L13-L54](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/gtwizard_generator.tcl#L13-L54)——第 16 行注释 `fLineRate = (REF_CLK * (FBDIV * FBDIV_45) / REFCLK_DIV) / OUT_DIV`。第 30 行先把 lane rate「上采样」（乘 1e6 再处理）以提高精度，第 31 行卡 VCO 频段 `[2e12, 6.25e12]`，第 44 行卡参考时钟上限 `8e11`（800 MHz，注释说「Wizard 支持的最大 800 MHz」）。

QPLL 版本结构相同，只是参数表更宽（GTYE3/4 的 `OUT_DIV` 多到 32）：

- [gtwizard_generator.tcl:L66-L110](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/gtwizard_generator.tcl#L66-L110)。

主过程 `ad_gth_generator` 第一步是**按器件前缀查 GT 型号**：

- [gtwizard_generator.tcl:L129-L200](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/gtwizard_generator.tcl#L129-L200)——取 `PART` 属性前 7 位，`switch` 到对应的 `gt_type`（如 `xczu9eg` → `GTHE4`、`xcvu9p-` → `GTYE4`），同时定下默认的 `channel_enable`（用哪些 X0Yn 通道）与 `ref_clk_source`。这是理解「同一套 ADI IP 为何能跨器件」的关键：型号差异被收敛进这一张表。

第二步是 **PLL 频段与 64B66B 翻倍**：

- [gtwizard_generator.tcl:L207-L238](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/gtwizard_generator.tcl#L207-L238)——不同 PLL 有不同的 VCO 合法范围（如 CPLL 是 0.5–12.5 GHz）；第 235 行 `if {$jesd_mode eq "64B66B"}` 把 min/max 都 `×2`，因为 64B66B 编码下等效线速率翻倍。

第三步是**真正创建 IP**，分 7 系列与 UltraScale 两支：

- [gtwizard_generator.tcl:L378](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/gtwizard_generator.tcl#L378)（7 系列）：`create_ip -name gtwizard ... -version 3.6`。
- [gtwizard_generator.tcl:L464-L496](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/gtwizard_generator.tcl#L464-L496)（UltraScale/UltraScale+）：`create_ip -name gtwizard_ultrascale ... -version 1.7`，随后用一大段 `set_property -dict` 把 `preset`（GTH-JESD204 / GTY-JESD204）、线速率、PLL 类型、编码（8B10B/64B66B）、数据位宽、comma 对齐值等一次性灌进去。其中 [L493-L495](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/gtwizard_generator.tcl#L493-L495) 开启正/负 comma 检测，这正是 JESD204B 链路 ILAS 对齐所必需的 K28.5 等控制字符识别。

最外层的 `get_diff_params` 把生成与解析串起来：

- [gtwizard_generator.tcl:L529-L626](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/gtwizard_generator.tcl#L529-L626)——先调 `ad_gth_generator` 生成 IP，再 `cd` 到 `*.gen/sources_1/ip` 目录执行 `gtwiz_parser.pl $gt_type`，把每个生成 IP 里**与默认值不同的属性**抽成 `$gt_type _cfng.txt`。这段脚本注释明确说明：「输出应覆盖 `system_bd.tcl` 里现有的实例」，并指向文档（注：源码注释引用的 `library/jesd204/xgt_wizard/index.html` 在当前 HEAD 已不存在，可作历史参考，不作为可点击文档使用）。

#### 4.2.4 代码实践

**实践目标**：动手用脚本里的算式预测一个参考时钟值，验证你对频率方程的理解。

**操作步骤**：

1. 读 [gtwizard_generator.tcl:L19-L21](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/gtwizard_generator.tcl#L19-L21)，记下 CPLL 的取值集合：`OUT_DIV = {1,2,4,8}`、`REFCLK_DIV = {1,2}`、`FBDIV = {1,2,3,4,5}`、`FBDIV_45 = {4,5}`。
2. 手算：取 lane rate = 10 Gbps、`OUT_DIV = 4`、`REFCLK_DIV = 1`、`FBDIV = 5`、`FBDIV_45 = 5`，代入方程 `f_REF = f_LineRate × OUT_DIV × REFCLK_DIV / (FBDIV × FBDIV_45)`。
3. 用 Tcl（或 Python）在本地复现 `cpll_ref_clk_gen`，输入 `[expr 10 * 1e6]`（kHz），打印返回列表，看你算的那个值是否在列表里。

**需要观察的现象**：一个 lane rate 会算出**多个**合法参考时钟候选（脚本返回的是 `lsort -real` 后的列表），工程里通常挑一个板上恰好有的晶振频率（如 250 MHz、500 MHz）来用。

**预期结果**：手算结果应出现在脚本返回的有序列表中，且所有候选都 ≤ 800 MHz、落在 CPLL 的 VCO 频段内。

> 待本地验证：脚本的注释说返回单位是「millihertz」，但实际表达式里又 `/ 1e9` 把结果转成 GHz（见 [L45](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/gtwizard_generator.tcl#L45)），注释与代码单位不一致——以代码为准，返回的是 GHz 浮点值。这类注释瑕疵在阅读脚本时要注意甄别。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `jesd_mode eq "64B64B"` 时要把 PLL 的 min/max range 都乘 2？

参考答案：64B66B 编码下，同样的有效数据吞吐需要更高的线路速率（每 64 bit 数据要带 2 bit 同步头），等效 lane rate 比 8B10B 高。PLL 的 VCO 频段是按线路速率定义的，所以合法范围要整体上移（×2）。

**练习 2**：`ad_gth_generator` 如何判断某块板子「不支持」？

参考答案：`switch $board` 的 `default` 分支会 `puts "ERROR ... Unsupported device."` 并 `return 1`（见 [L196-L199](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/gtwizard_generator.tcl#L196-L199)）。它取的是 PART 号前 7 位，所以新增器件必须在两张表（`ad_gth_generator` 与 `get_diff_params` 各一张）里补上对应前缀。

---

### 4.3 时序约束与自动修复

#### 4.3.1 概念说明

ADI 的数据通路几乎处处是**跨时钟域（CDC）**：DMA 的源端跑在数据转换器时钟上、目的端跑在 PS DDR 时钟上、寄存器面又跑在 CPU 时钟上。综合器不会自动处理 CDC——你得显式告诉它「这两组寄存器之间的路径是异步的，别按同步路径分析它」，否则它会把跨域路径报成时序违例，掩盖真正的问题。

约束分两层：

- **IP 级约束**：每个 IP 自带约束。Xilinx 侧用 **ttcl**（Tcl 模板，综合时展开成 `.xdc`），Intel 侧用 **sdc**（直接文本）。它们按 IP 的 `ASYNC_CLK_*` 参数决定生成哪些 false path / max delay。
- **工程级约束**：工程目录的 `system_constr.xdc`，主要做引脚分配（`PACKAGE_PIN`）、电平标准（`IOSTANDARD`）与板级时钟声明（`create_clock`）。

即便约束写得对，Vivado 偶尔仍会在布线后留下少许违例（尤其某些版本工具的已知 bug）。`auto_timing_fix_xilinx.tcl`（Auto Timing Fix, ATF）就是**布线之后的最后一道自动补救**：它在 `route_design` 完成后自动跑几轮 `phys_opt_design`，尽量把残留的负裕量修平。

#### 4.3.2 核心流程

ATF 的主循环是一个**带阈值与上限的迭代修整器**：

```text
进入 ATF（作为 ROUTE_DESIGN.TCL.POST 钩子）
  │
  ├─ 1. 读环境变量（缺省值）
  │     ADI_AUTOFIX_WNS_THRESHOLD = -2.0   （只修比 -2.0 ns 好的违例）
  │     ADI_AUTOFIX_MAX_ATTEMPTS  = 5      （最多迭代 5 次）
  │
  ├─ 2. Vivado 2024/2025 hold 违例 workaround
  │     若存在 hold(min) 违例 → 先 route_design 重跑一次
  │
  └─ 3. ATF 主循环（最多 max_attempts 次）：
        a. 取最差路径 worst_path，读其 SLACK 与 DELAY_TYPE
        b. SLACK >= 0      → success，退出
        c. SLACK <= 阈值   → threshold_exceeded，放弃（避免越修越乱）
        d. 否则：
             min(hold) → phys_opt_design -hold_fix
             max(setup)→ phys_opt_design
           回到 a
  最终：写检查点(_success/_failure/_aborted.dcp) + timing_summary
```

阈值的意义在于**止损**：如果最差裕量已经差到离谱（比如 -5 ns），说明约束或设计本身有问题，自动微调救不回来，强行修反而会拖垮其它路径，不如把决策交还给人。

#### 4.3.3 源码精读

先看 ATF 如何被**挂进构建流程**。工程在 `system_project.tcl` 里设变量：

- [projects/daq2/zcu102/system_project.tcl:L9](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/daq2/zcu102/system_project.tcl#L9)——`set ADI_POST_ROUTE_SCRIPT [file normalize $ad_hdl_dir/projects/scripts/auto_timing_fix_xilinx.tcl]`。fmcomms2/zcu102 同样如此（见 grep 结果，本仓库大量 JESD/LVDS 工程都挂了这个脚本）。

这个变量在 `adi_project_xilinx.tcl` 里被消费两处：

- [adi_project_xilinx.tcl:L344-L346](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L344-L346)（在 `adi_project_files` 里）——把脚本文件加进 `utils_1` 文件集，这样它就属于工程、可被 `get_files` 找到。
- [adi_project_xilinx.tcl:L410-L412](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L410-L412)（在 `adi_project_run` 里）——`set_property STEPS.ROUTE_DESIGN.TCL.POST <脚本> [get_runs impl_1]`，正式把它注册为「布线步骤之后」执行的 Tcl 钩子。紧接着 [L414](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L414) `launch_runs impl_1 -to_step write_bitstream` 启动实现，[L417](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L417) 再 `report_timing_summary` 落日志。

接下来看 ATF 脚本本体。开头注释把两个可调环境变量讲得很清楚：

- [auto_timing_fix_xilinx.tcl:L5-L17](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/auto_timing_fix_xilinx.tcl#L5-L17)——`ADI_AUTOFIX_WNS_THRESHOLD` 限定「只自动修比该阈值好的违例」；并说明针对 Vivado 2024.x/2025.x 的 hold 问题有专门 workaround。

主循环 `run_atf_loop`：

- [auto_timing_fix_xilinx.tcl:L27-L69](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/auto_timing_fix_xilinx.tcl#L27-L69)——每轮 [L35](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/auto_timing_fix_xilinx.tcl#L35) 取一条最差路径，[L40-L41](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/auto_timing_fix_xilinx.tcl#L40-L41) 读 `SLACK` 与 `DELAY_TYPE`（`min` = hold、`max` = setup）；[L59-L62](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/auto_timing_fix_xilinx.tcl#L59-L62) 据此选 `phys_opt_design -hold_fix`（修 hold）或裸 `phys_opt_design`（修 setup）。循环退出后 [L84-L95](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/auto_timing_fix_xilinx.tcl#L84-L95) 按四种状态（success/no_paths/threshold_exceeded/failure）分别写不同名字的检查点，便于事后定位。

主脚本里那段 Vivado 2024/2025 workaround：

- [auto_timing_fix_xilinx.tcl:L113-L134](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/auto_timing_fix_xilinx.tcl#L113-L134)——先单独查 hold 违例，若有就再调一次 `route_design`。注释 [L116-L119](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/auto_timing_fix_xilinx.tcl#L116-L119) 给出了 AMD 官方论坛的两个链接作为依据，说明这是工具特定版本的已知问题、`phys_opt_design -hold_fix` 对它无效，只能重布线。这正是本讲依赖讲义 u1-l3 提到的「发布分支绑定特定工具版本」的实践动机之一：换工具版本可能引入这类回归。

现在看**约束**。工程级约束（fmcomms2/zcu102）做的是引脚与时钟声明：

- [system_constr.xdc:L9-L42](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_constr.xdc#L9-L42)——给 ad9361 的 LVDS 差分信号分配 `PACKAGE_PIN` 与 `IOSTANDARD LVDS`，并在输入侧加 `DIFF_TERM_ADV TERM_100`（100Ω 差分终端）。
- [system_constr.xdc:L67](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_constr.xdc#L67)——`create_clock -name rx_clk -period 4.00 [get_ports rx_clk_in_p]`，声明 4 ns（250 MHz）的输入时钟。这一行是后续所有衍生时序分析的「锚点」：Vivado 据此推算 GT/MMCM 衍生时钟，进而分析整条通路的 setup/hold。

IP 级约束则处理 CDC。Intel 侧的 `axi_dmac_constr.sdc` 是手写的 false path 清单：

- [axi_dmac_constr.sdc:L6-L9](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_constr.sdc#L6-L9)——对 `cdc_sync_stage1`（跨域同步器的第一级，见 u5-l3 的 `sync_bits`）等寄存器设 false path，告诉工具「这些是异步跨域点，别分析」。[L12](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_constr.sdc#L12) 的 `burst_len_mem`、[L15-L29](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_constr.sdc#L15-L29) 的 reset_manager 都是同源思路——凡是复位释放或异步指针，都显式标 false path。

Xilinx 侧的等价物是 ttcl 模板，它**按参数动态生成**：

- [axi_dmac_constr.ttcl:L11-L16](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_constr.ttcl#L11-L16)——读取 `ASYNC_CLK_DEST_REQ`/`ASYNC_CLK_REQ_SRC`/`ASYNC_CLK_SRC_DEST` 等 IP 参数，判断哪些时钟域对是异步的。
- [axi_dmac_constr.ttcl:L60-L65](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_constr.ttcl#L60-L65)——只要存在任一异步对，就把同步器两级寄存器标 `ASYNC_REG TRUE`（指导工具正确放置与复制触发器）。
- [axi_dmac_constr.ttcl:L66-L148](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_constr.ttcl#L66-L148)——对每个异步域对，用 `set_max_delay -datapath_only -from <src_clk> -to <cdc_reg> [周期]` 把跨域路径约束成单周期最大延迟（这是 Xilinx 处理 CDC 的推荐写法，比 `set_false_path` 更安全，仍保留一定时序检查）。

这套 ttcl/sdc 与 u4-l2 讲过的「bd.tcl 的 propagate 写 `ASYNC_CLK_*` → ttcl 读之生成约束」构成完整闭环：**块设计时判断时钟关系，综合时据此生成约束，布线后再由 ATF 兜底**。

#### 4.3.4 代码实践

**实践目标**：把 ATF 的钩子挂载、阈值调参与 CDC 约束三件事串起来理解。

**操作步骤**：

1. 打开 [auto_timing_fix_xilinx.tcl:L104-L111](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/auto_timing_fix_xilinx.tcl#L104-L111)，记下两个默认值：`ADI_AUTOFIX_WNS_THRESHOLD = -2.0`、`ADI_AUTOFIX_MAX_ATTEMPTS = 5`。
2. 假设你要让某个工程「更激进地自动修时序」（修到 -3 ns、最多 8 轮），写出在 shell 里构建时该传什么环境变量。
3. 对照 [adi_project_xilinx.tcl:L410-L414](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L410-L414) 说出：ATF 脚本到底是在 `launch_runs` 的哪个步骤之后执行的。

**需要观察的现象**：ATF 是挂在 `ROUTE_DESIGN` **之后**的（`STEPS.ROUTE_DESIGN.TCL.POST`），而不是 `write_bitstream` 之后；它跑完，控制权才回到 `launch_runs` 继续后面的步骤。若 ATF 把 WNS 修到 ≥ 0，工程最终产出的就是正常 `system_top.xsa`，否则是 `system_top_bad_timing.xsa`（见 u3-l3）。

**预期结果**：

- 步骤 2 的命令形如（示例代码，未实测运行）：
  ```bash
  ADI_AUTOFIX_WNS_THRESHOLD=-3.0 ADI_AUTOFIX_MAX_ATTEMPTS=8 make -C projects/daq2/zcu102
  ```
- 步骤 3 的答案：在 `route_design`（`impl_1` 的 ROUTE 步骤）完成后、`write_bitstream` 之前。

> 待本地验证：实际构建需安装对应版本 Vivado 与板卡支持；若仅做源码阅读，可只完成步骤 1、3，并在 ATF 脚本注释里确认 workaround 适用的工具版本。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `axi_dmac_constr.ttcl` 用 `set_max_delay -datapath_only` 而不是简单地 `set_false_path` 来处理 CDC？

参考答案：`set_false_path` 会完全放弃对该路径的时序检查（包括布线后的实际延迟），过于宽松、可能掩盖真正的CDC 悖论。`set_max_delay -datapath_only` 只忽略时钟偏斜（skew）影响、但仍约束数据路径最大延迟，是 Xilinx 官方推荐的同步器 CDC 约束写法——既不误报，又保留了对同步器延迟的基本控制。

**练习 2**：如果一个工程构建后 WNS = -0.3 ns，ATF 默认会怎么处理？如果 WNS = -5 ns 呢？

参考答案：-0.3 ns 在默认阈值 `-2.0` 之内（`0 > -0.3 > -2.0`），ATF 会进入循环尝试用 `phys_opt_design` 修，最多 5 轮，修平则写 `_success` 检查点。-5 ns 超过阈值，ATF 直接判定 `threshold_exceeded` 放弃自动修复、写 `_aborted` 检查点，把问题留给开发者人工分析（可能需要改 RTL 或约束，而非微调）。

---

## 5. 综合实践

把三个模块串起来，完成一次「从收发器连线到时序收敛」的完整源码追踪。

以 [projects/daq2/common/daq2_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/daq2/common/daq2_bd.tcl) 为对象，完成以下任务：

1. **连线层**：找到 [L154-L157](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/daq2/common/daq2_bd.tcl#L154-L157) 的 `ad_xcvrpll` 与 [L161](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/daq2/common/daq2_bd.tcl#L161)/[L189](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/daq2/common/daq2_bd.tcl#L189) 的 `ad_xcvrcon`。画一张草图，标出 `rx_ref_clk_0` 如何进入 `util_daq2_xcvr`、被 `ad_xcvrcon` 内部转成 `link_clk`/`device_clk` 喂给 `axi_ad9680_jesd`。
2. **GT 层**：daq2 用的是 `xcvr_automation.tcl`（[L139-L144](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/daq2/common/daq2_bd.tcl#L139-L144) 的 `adi_xcvr_parameters`）而非 `gtwizard_generator.tcl`。请说明二者分工：哪个负责「工程里真正生成 GT」，哪个负责「开发者离线试算参考时钟」。
3. **时序层**：daq2/zcu102 的 [system_project.tcl:L9](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/daq2/zcu102/system_project.tcl#L9) 挂了 ATF。请说明 ATF 在构建流程中的执行时机，以及它兜底的是哪类问题（CDC false path 已由 ttcl 处理，ATF 主要兜底工具残留违例）。

**交付物**：一张标注了「参考时钟 → PLL → lane rate → link/device clock → JESD 链路核」的数据流图，加一段话说明 ATF 在其中扮演的「最后一道防线」角色。

> 待本地验证：若有 daq2 硬件，可在 Vivado GUI 里打开生成的工程，对照 `report_timing_summary` 与 ATF 写出的 `*_final_timing_summary.txt`，验证 ATF 前后的 WNS 变化。

## 6. 本讲小结

- `ad_xcvrcon` 是把「`util_adxcvr`（物理核）/ `axi_adxcvr`（配置核）/ `axi_jesd204`（链路核）」三者，连同 lane clock、device clock、link clock、SYSREF、sync、复位，按 lane 数循环接好的高阶连线原语；它靠读三个 IP 的 `CONFIG.*` 参数自适应收发方向、PLL 类型与 JESD 模式。
- 时钟层级是收发器时序的钥匙：lane rate（串行）→ GT 分频出 link clock（并行）→ 兜底作为 device clock；JESD204C 在特定 GTH 上需要 2x 时钟驱动 PCS，`ad_xcvrcon` 里有专门分支处理。`ad_xcvrpll` 负责把一个参考时钟/复位扇出到 quad 里所有 PLL 引脚。
- `gtwizard_generator.tcl` 是离线实验工具，用纯 Tcl 把 lane rate 代入 PLL 频率方程枚举出所有合法参考时钟，再批量调 Vivado 的 `gtwizard`/`gtwizard_ultrascale` IP 生成 GT；它与工程主路径 `xcvr_automation.tcl` 互补。
- 约束分两层：IP 级（Xilinx 用 ttcl 按 `ASYNC_CLK_*` 生成 `set_max_delay -datapath_only` 与 `ASYNC_REG`，Intel 用 sdc 手写 false path）处理 CDC；工程级 xdc 处理引脚与板级 `create_clock`。
- `auto_timing_fix_xilinx.tcl` 作为 `ROUTE_DESIGN.TCL.POST` 钩子，在布线后用带阈值（默认 -2.0 ns）与上限（默认 5 次）的 `phys_opt_design` 循环自动修残留违例，并对 Vivado 2024/2025 的 hold bug 有专门 `route_design` 重跑的 workaround。

## 7. 下一步学习建议

- **回到 u6-l1（JESD204 框架）**对照阅读：本讲讲的是「物理核怎么连到链路核」，u6-l1 讲的是「链路核内部链路层/传输层如何工作」，两者合起来才是完整 JESD 通路。
- **接 u8-l4（Boot 镜像生成）**：时序收敛、拿到合法 `system_top.xsa` 之后，下一步就是把比特流打包成可上电启动的 `BOOT.BIN`。
- **深读收发器 IP**：想理解 `util_adxcvr`/`axi_adxcvr` 内部如何包装 GT、如何做 DRP 与复位管理，可阅读 `library/jesd204/` 下 `util_adxcvr` 与 `axi_adxcvr` 的源码及 `docs/library/` 对应 IP 文档（按 u2-l3 的导航查找）。
- **想动手调时序**：拿一个挂了 ATF 的工程（如 daq2/zcu102），按本讲实践调整 `ADI_AUTOFIX_*` 环境变量重新构建，对比 `timing_impl.log` 与 ATF 产出的 `*_timing_summary.txt`，直观感受自动修复的效果与边界。
