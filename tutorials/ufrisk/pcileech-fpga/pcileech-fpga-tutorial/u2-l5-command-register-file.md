# 命令/控制寄存器文件与读写协议

## 1. 本讲目标

本讲打开 `pcileech_fifo` 的「**控制面**」。在前两讲里我们已经看到 fifo 是一块卡片的「路由器 + 多路复用器」，负责把主机发来的 64 位数据按 MAGIC 分流到 TLP/CFG/Loopback/Command 四路，再把回程数据打包成 256 位大包送回主机。但还有一个一直没回答的问题：

> 主机到底是怎么「按住 PCIe 核的复位」「打开 BAR 处理」「触发一次 DRP 读写」「读到当前 LTSSM 链路状态」的？

答案就藏在 `pcileech_fifo` 内部两张寄存器表——只读的 `ro` 和可读写的 `rw`——以及一条把命令包解析成「字节地址 + 数值 + 掩码」的位寻址状态机里。这是整张卡片唯一一条「主机软件 → 硬件行为」的控制链路。

学完本讲你应当能够：

- 画出 `ro`（只读状态镜像，320 位）与 `rw`（读写控制，240 位）两张寄存器表的字段布局，并指出每个字段的字节偏移。
- 说清楚一个 64 位命令包是如何被拆成「地址 / 数值 / 掩码 / 读 / 写 / 路由标志」的，以及「位寻址 + 16 位窗口 + 逐位掩码」的写机制如何工作。
- 解释 `_pcie_core_config` 这一段拼接如何把 `rw` 里的 8 个控制位翻译成 `dpcie.pcie_rst_core`、`dshadow2fifo.cfgtlp_en`/`bar_en` 等真正的硬件输出信号。
- 跟踪一次主机发起的 DRP 读写：从写 `rw` 的 DRP 位，到 `drp_en`/`drp_we` 拉起，再到 `drp_rdy` 回送结果的全过程。
- 理解「读回写回」协议：命令 FIFO 的节流读取、读响应包的回送格式、以及 shadow 配置空间响应的特殊标签。

---

## 2. 前置知识

本讲建立在 **u2-l3（FIFO 控制中心与 MAGIC 路由）** 之上，默认你已经知道：

- **MAGIC 路由**：从 FT601 收到的 64 位数据，当 `dcom.com_dout[7:0]==0x77` 时是一帧，`[9:8]` 的 type 字段取 `00/01/10/11` 分别把它分流到 TLP / CFG / Loopback / Command 四路。
- **Command 路（type=11）**：本讲的主角。命令包不是发往 PCIe 的业务数据，而是写给 fifo 自己的「控制指令」。
- **接口契约**：fifo 通过 5 个 interface（`dcom`/`dcfg`/`dtlp`/`dpcie`/`dshadow2fifo`）与外界相连。本讲会频繁用到其中两个：`dpcie`（驱动 PCIe 核复位与 DRP）、`dshadow2fifo`（驱动配置空间影子与 BAR 控制器的功能开关）。
- **`tickcount64`**：一个每拍自增的 64 位自由计数器，既是「上电复位源」也当作「系统运行时间戳」。本讲里它还会充当命令 FIFO 的「节拍器」。

此外需要一点 PCIe 背景常识（不熟也没关系，记住结论即可）：

- **DRP（Dynamic Reconfiguration Port，动态重配置端口）**：Xilinx 7 系列 PCIe 核提供的一个端口，允许在运行时读写核内部寄存器（如链路速率、发送摆幅、均衡参数等），无需重新综合工程。它有一组握手信号：`drp_en`（发起）/`drp_we`（写使能）/`drp_addr`（地址）/`drp_di`（写数据）/`drp_do`（读回数据）/`drp_rdy`（完成）。
- **配置空间（Configuration Space）**：PCIe 设备每组 4096 字节、主机用 CfgRd/CfgWr 访问的寄存器空间，里面有 VID/PID/Class Code 等「设备身份证」。本讲的 `rw` 表里也镜像了一份 ID 字段，但标注为「NOT IMPLEMENTED」——后面会解释为什么。
- **BAR（Base Address Register）**：PCIe 设备宣布「我有一段内存空间，主机可以用内存读写访问它」的寄存器。本讲的 `rw[205]` 就是开关「板载 BAR PIO 处理」的总闸。

---

## 3. 本讲源码地图

本讲几乎全部内容都集中在一个文件里：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [PCIeSquirrel/src/pcileech_fifo.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv) | FIFO 路由 + 寄存器文件 + 命令状态机 | `ro`/`rw` 布局、`_pcie_core_config`、命令解析、DRP 握手、STARTUPE2 |
| [PCIeSquirrel/src/pcileech_header.svh](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh) | 接口契约定义 | `IfPCIeFifoCore`（DRP/复位信号）、`IfShadow2Fifo`（功能开关与命令透传）|

读源码时建议把 `pcileech_fifo.sv` 从第 203 行（`REGISTER FILE: COMMON`）读到第 456 行（`STARTUPE2` 结束），这一段就是完整的「控制面」。本讲会按这个顺序逐块拆解。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **寄存器文件 ro/rw**：两张表的整体布局、字段含义、初始化值，以及它们如何映射出整卡的状态与控制旋钮。
2. **命令解析状态机**：64 位命令包的拆解、位寻址读写机制、读回写回协议、节流与 anti-deadlock 设计。
3. **DRP 触发与 STARTUPE2 全局复位**：把寄存器位翻译成硬件动作的两条最关键副作用链。

### 4.1 寄存器文件 ro/rw：整板的「状态表」与「控制表」

#### 4.1.1 概念说明

任何一块受主机软件控制的 FPGA 板卡，本质上都需要两样东西：

- 一张 **状态表**：主机能读、不能写，反映硬件「现在是什么状态」——运行了多久、PCIe 链路在不在、DRP 读回了什么值、设备型号版本号是多少。
- 一张 **控制表**：主机能读也能写，告诉硬件「接下来要做什么」——要不要复位 PCIe 核、要不要打开 BAR 处理、要不要发起一次 DRP 写。

在 `pcileech_fifo` 里，这两张表分别是：

- `ro` —— **read-only**，320 位（40 字节），纯组合 `assign` 驱动，主机永远只能读。
- `rw` —— **read-write**，240 位（30 字节），`reg` 型，上电由 `task` 初始化，之后由命令状态机按位改写。

> 为什么是两段连续的 `reg`/`wire` 而不是一个数组？因为 SystemVerilog 里用「位切片」`rw[200]`、`ro[271:256]` 访问比数组更方便和主机软件的「位/字节地址」直接对应。`$bits(ro)`/`$bits(rw)` 还能自动算出字节计数回送给主机。

这两张表不是凭空造的——它们的字段布局就是 **PCILeech 主机端驱动（LeechCore 的 `device_fpga.c`）约定的「控制寄存器映射」**。主机软件按相同的字节偏移读写，FPGA 这边按相同的位切片响应，两边靠这份「默契」通信。所以你改一个字段的位置，主机端软件就读错地方了。

#### 4.1.2 核心流程

寄存器文件的「数据流」很短：

```text
        ┌─────────────────────────── pcileech_fifo ────────────────────────────┐
        │                                                                       │
  上电  │  task pcileech_fifo_ctl_initialvalues  ──►  rw[239:0] = 初值         │
  复位  │  rst / tickcount64<8                   ──►  rw[239:0] = 初值         │
        │                                                                       │
 主机写 │  命令状态机  ──(位寻址+掩码)──►  rw[某 16 位窗口]                     │
        │                                                                       │
 硬件镜像│  tickcount64 ─────────────────►  ro[191:128] (UPTIME)               │
        │  pcie_present / pcie_perst_n ───►  ro[273]/ro[272]                   │
        │  dpcie.drp_do ─────────────────►  ro[271:256] (DRP 读回)             │
        │                                                                       │
 主机读 │  命令状态机  ──(读 16 位窗口)──►  _cmd_tx_din ──► mux ──► FT601 ──► 主机│
        │                                                                       │
  翻译  │  _pcie_core_config <= rw[207:128] ──► dpcie.* / dshadow2fifo.*        │
        └───────────────────────────────────────────────────────────────────────┘
```

关键点：`ro` 完全由硬件信号驱动（主机只读），`rw` 由主机驱动（硬件只做初始化和按命令改写），二者泾渭分明。`rw` 里有一段（`rw[207:128]`）会被原样搬进 `_pcie_core_config`，再由后者驱动真正的 PCIe 核 / shadow 控制信号——这就是「写一个寄存器位 → 硬件行为改变」的桥梁（4.1.4 与 4.3 详述）。

#### 4.1.3 源码精读

**ro/rw 的声明与特殊内部寄存器**（[pcileech_fifo.sv:212-220](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L212-L220)）：

```systemverilog
wire    [319:0]     ro;          // 只读状态，320 位 = 40 字节
reg     [239:0]     rw;          // 读写控制，240 位 = 30 字节

// 特殊内部寄存器（主机不能直接访问）
reg     [79:0]      _pcie_core_config = { 4'hf, 1'b1, 1'b1, 1'b0, 1'b0,
                                          8'h02, 16'h0666, 16'h10EE,
                                          16'h0007, 16'h10EE };
time                _cmd_timer_inactivity_base;
reg                 rwi_drp_rd_en;       // DRP 读「进行中」标志
reg                 rwi_drp_wr_en;       // DRP 写「进行中」标志
reg     [15:0]      rwi_drp_data;        // DRP 读回值缓存
```

注意 `_pcie_core_config` 的初值里藏着 PCIe 核的「CLK0 默认身份」（`DEV=0x0666 / VEND=0x10EE / SUBSYS=0x0007 / SUBSYS_VEND=0x10EE / REV=0x02`），以及末尾几个复位/开关位的初值。这行旁边的注释特别提醒：「initial CLK0 values may also be changed here, important on PCIeScreamer」——它保证上电后、`rw` 被 `task` 加载之前的第一个时钟周期，PCIe 核就有一个合理的复位/控制初态。

**ro 字段布局**（[pcileech_fifo.sv:226-251](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L226-L251)）。整理成表（字节偏移采用源码注释里的 hex 记法，bit 范围对照 `ro[...]`）：

| 字节偏移 | bit 范围 | 字段 | 含义 / 取值 |
| --- | --- | --- | --- |
| +000 | ro[15:0] | MAGIC | 固定 `0xAB89`，主机用来确认「这确实是 pcileech FPGA」 |
| +002 | ro[19:16] | SPECIAL | 保留 = 0 |
| +002 | ro[20] | rwi_drp_rd_en | DRP 读是否「进行中」 |
| +002 | ro[21] | rwi_drp_wr_en | DRP 写是否「进行中」 |
| +004 | ro[63:32] | SIZEOF | `ro` 的字节计数 = 40（`$bits(ro)>>3`）|
| +008 | ro[71:64] | VERSION MAJOR | 由顶层参数 `PARAM_VERSION_NUMBER_MAJOR` 传入 |
| +009 | ro[79:72] | VERSION MINOR | 由 `PARAM_VERSION_NUMBER_MINOR` 传入 |
| +00A | ro[87:80] | DEVICE ID | 由 `PARAM_DEVICE_ID` 传入，区分设备型号 |
| +00B | ro[127:88] | SLACK | 保留 = 0 |
| +010 | ro[191:128] | UPTIME | `tickcount64`，即「上电后 100MHz 时钟周期数」 |
| +018 | ro[255:192] | INACTIVITY TIMER | 最近一次「上行活动」的时间戳 |
| +020 | ro[271:256] | DRP DO | DRP 读回值（`rwi_drp_data` ← `dpcie.drp_do`）|
| +022 | ro[272] | PCIe PRSNT# | 金手指 PRSNT# 引脚（卡是否插到位）|
| +022 | ro[273] | PCIe PERST# | 复位引脚电平 |
| +024 | ro[319:288] | CUSTOM VALUE | 由 `PARAM_CUSTOM_VALUE` 传入，留给自定义 |

读这张表的心法：**「主机想知道的一切硬件状态，都映射在 ro 的某个 bit 上」**。例如主机想看链路有没有 training 完成，会去读 `pcie_present`/`pcie_perst_n`；想看一次 DRP 的结果，会去读 `+020`；想确认设备型号对不对，会去读 `+00A`。

**rw 字段布局**（`task pcileech_fifo_ctl_initialvalues`，[pcileech_fifo.sv:259-302](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L259-L302)）。同样整理成表：

| 字节偏移 | bit 范围 | 字段 | 初值 | 含义 |
| --- | --- | --- | --- | --- |
| +000 | rw[15:0] | MAGIC | `0xEFCD` | rw 表的魔术字（与 ro 的 `0xAB89` 区分）|
| +002 | rw[16] | enable inactivity timer | 0 | 不活动计时器开关 |
| +002 | rw[17] | enable send count | 0 | 「周期性发送计数」开关 |
| +002 | rw[18] | WAIT_COMPLETE | 1 | DRP 未完成时暂停接受新命令（防丢包）|
| +002 | rw[20] | DRP RD EN | 0 | 主机写 1 触发一次 DRP 读 |
| +002 | rw[21] | DRP WR EN | 0 | 主机写 1 触发一次 DRP 写 |
| +002 | rw[31] | GLOBAL SYSTEM RESET | 0 | 写 1 经 STARTUPE2 触发整片 FPGA 复位 |
| +004 | rw[63:32] | bytecount | 30 | rw 表字节计数 |
| +008 | rw[95:64] | cmd_inactivity_timer | 0 | 不活动超时阈值（时钟周期数）|
| +00C | rw[127:96] | cmd_send_count | 0 | 周期发送计数的初值 |
| +010 | rw[143:128] | CFG_SUBSYS_VEND_ID | `0x10EE` | （**NOT IMPLEMENTED**）|
| +012 | rw[159:144] | CFG_SUBSYS_ID | `0x0007` | （**NOT IMPLEMENTED**）|
| +014 | rw[175:160] | CFG_VEND_ID | `0x10EE` | （**NOT IMPLEMENTED**）|
| +016 | rw[191:176] | CFG_DEV_ID | `0x0666` | （**NOT IMPLEMENTED**）|
| +018 | rw[199:192] | CFG_REV_ID | `0x02` | （**NOT IMPLEMENTED**）|
| **+019** | **rw[207:200]** | **控制字节** | 见下表 | **本讲最关键的 8 个开关位** |
| +01A | rw[223:208] | DRP di | 0 | DRP 写数据（16 位）|
| +01C | rw[232:224] | DRP addr | 0 | DRP 地址（9 位）|

注意 +010～+018 这一段的「设备身份证」字段都标了 **NOT IMPLEMENTED**——它们虽然被装进了 `rw`，但 `fifo` 并没有把它们接到 PCIe 核上去（真正的设备 ID 由 Vivado PCIe IP 的 GUI 配置和 `cfgspace.coe` 决定，详见 u4-l4）。它们更像「历史遗留的占位」，主机软件不会真靠这里改 ID。

**真正活跃的是 +019 这一字节**（[pcileech_fifo.sv:287-294](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L287-L294)）：

```systemverilog
rw[200] <= 1'b1;   // +019: PCIE CORE RESET                 核复位（1=保持复位）
rw[201] <= 1'b0;   //       PCIE SUBSYSTEM RESET            子系统复位
rw[202] <= 1'b1;   //       CFGTLP PROCESSING ENABLE        允许板载处理配置 TLP
rw[203] <= 1'b1;   //       CFGTLP ZERO DATA                配置读返回零（隐藏真实配置）
rw[204] <= 1'b1;   //       CFGTLP FILTER TLP FROM USER     过滤掉送往主机的配置 TLP
rw[205] <= 1'b1;   //       PCIE BAR PIO ON-BOARD ENABLE    打开板载 BAR 处理
rw[206] <= 1'b0;   //       CFGTLP PCIE WRITE ENABLE        允许配置空间写回 PCIe 核
rw[207] <= 1'b0;   //       TLP FILTER EXCEPT Cpl/CplD/Cfg  过滤其它 TLP（保留完成与配置包）
```

这 8 位就是 u2-l2 里那个「上电先按住核复位、主机软件起来后再松开」的旋钮集。`rw[200]` 上电就是 1（核复位），`com` 模块的 `initial_rx` 注入那条命令（写 `rw` 字节 0x18、掩码 `0x0300`、值 0）正是把 `rw[200]` 这一位清零，把 PCIe 核放出来。

#### 4.1.4 代码实践

> **实践目标**：把 `rw` 的 +019 控制字节与它真正驱动的硬件输出信号一一对应起来，建立「写一个寄存器位 → 硬件行为改变」的直觉。

**操作步骤**：

1. 打开 [pcileech_fifo.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv)，定位到 `_pcie_core_config <= rw[207:128]`（第 309-310 行）与紧随其后的 8 条 `assign`（第 311-318 行）。
2. 注意 `_pcie_core_config` 是 80 位，它被 `rw[207:128]` 整段加载，因此 `_pcie_core_config[k]` 就等于 `rw[k+128]`。
3. 推导 +019 字节每一位（`rw[200..207]`）映射到 `_pcie_core_config[72..79]`，再读 `assign` 看它驱动哪个 interface 信号。

**预期结果**（请自己先填一遍，再对照下表）：

| rw 位 | _pcie_core_config 位 | 驱动的输出信号 | 该位 = 1 时的硬件行为 |
| --- | --- | --- | --- |
| rw[200] PCIE CORE RESET | [72] | `dpcie.pcie_rst_core` | PCIe 核被按在复位状态 |
| rw[201] PCIE SUBSYSTEM RESET | [73] | `dpcie.pcie_rst_subsys` | PCIe 子系统复位 |
| rw[202] CFGTLP PROCESSING ENABLE | [74] | `dshadow2fifo.cfgtlp_en` | 允许 cfgspace_shadow 处理配置 TLP |
| rw[203] CFGTLP ZERO DATA | [75] | `dshadow2fifo.cfgtlp_zero` | 配置读返回 0（隐藏真实配置内容）|
| rw[204] CFGTLP FILTER TLP FROM USER | [76] | `dshadow2fifo.cfgtlp_filter` | 把配置类 TLP 从送往主机的流里过滤掉 |
| rw[205] PCIE BAR PIO ENABLE | [77] | `dshadow2fifo.bar_en` | 启用板载 BAR PIO 控制器 |
| rw[206] CFGTLP PCIE WRITE ENABLE | [78] | `dshadow2fifo.cfgtlp_wren` | 允许把配置写回写进 PCIe 核 |
| rw[207] TLP FILTER EXCEPT Cpl/CplD/Cfg | [79] | `dshadow2fifo.alltlp_filter` | 过滤其它 TLP，只放过完成包与配置包 |

**需要观察的现象**：这 8 条 `assign` 全部写到两个 interface——`dpcie`（连 PCIe 核封装）和 `dshadow2fifo`（连配置空间影子 / BAR 控制器）。也就是说，`rw` 的 +019 字节是「PCIe 核 + 设备仿真」这两套子系统的**总开关面板**。

> 本实践为「源码阅读型实践」，不需要上板。如果你后续在 u4 单元动手改 BAR/cfgspace 行为，这张表就是你的「控制旋钮速查表」。

#### 4.1.5 小练习与答案

**练习 1**：主机读 `ro` 的 +00A 字节得到 `0x07`，读 +008 得到 `0x04`。这两个值分别表示什么？它们是由谁决定的？

> **答案**：`+00A` 是 DEVICE ID = `0x07`，`+008` 是 VERSION MAJOR = `0x04`。它们都不是运行时产生的，而是综合时由顶层 `pcileech_squirrel_top.sv` 例化 `pcileech_fifo` 时传入的参数 `PARAM_DEVICE_ID` / `PARAM_VERSION_NUMBER_MAJOR` 决定的。换言之，设备型号在编译期就烙进 bitstream 了。

**练习 2**：`ro[63:32]`（SIZEOF）的值是 40，`rw[63:32]`（bytecount）的值是 30。为什么不一样？

> **答案**：因为 `ro` 是 320 位 = 40 字节，`rw` 是 240 位 = 30 字节，二者位宽不同。这个字段用 `$bits(ro)>>3` 与 `$bits(rw)>>3` 自动计算，主机软件靠它知道「这张表有多长」，从而不会越界读。

**练习 3**：`rw` 里 +010～+018 的设备身份证字段为什么标「NOT IMPLEMENTED」？如果我想真改设备 ID，应该去哪里改？

> **答案**：因为 `fifo` 没有把这些位接到 PCIe 核的配置空间输出上，写了也不生效。要真正改设备 ID，应在 Vivado 工程里改 PCIe 核 GUI 的 IDs 标签页，或改 `cfgspace.coe`（较新设备的配置空间影子），详见 u4-l4。

---

### 4.2 命令解析状态机：位寻址读写协议

#### 4.2.1 概念说明

有了两张寄存器表，下一个问题就是：**主机怎么读写它们？** 总不能为每个 bit 拉一根线出来。pcileech 的做法是复用 Command 路（MAGIC type=11）：

- 主机把要做的读写操作打包成一个 64 位的「**命令包**」，按普通 Command 数据发进来（和 TLP 走同一条 USB 管道，只是 type 字段不同）。
- `pcileech_fifo` 把 Command 数据存进一个浅 FIFO（`fifo_64_64_clk1_fifocmd`），再由一个 `always` 块逐条取出、解析、执行。
- 读操作的结果也打包成一个 64 位的「**响应包**」，塞回 Command 回程 FIFO，最终经 `pcileech_mux` 上行回主机。

这套协议最巧妙的地方是 **位寻址（bit-addressing）+ 16 位窗口 + 逐位掩码**：

- 命令包里的「地址」字段虽然写的是一个**字节地址**，但它的低 15 位会被左移 3 位（×8）变成 **bit 地址**，指向 `ro`/`rw` 内部的某一位。
- 每条命令最多覆盖一个 **16 位的窗口**（从该 bit 地址开始的连续 16 位）。
- 一个 **16 位的掩码**逐位决定窗口内的每一位「是否要被写」。掩码位为 1 的位置才把 `value` 的对应位写进去，为 0 的位置保持原值。

这样，主机用一个字节地址 + 一个 16 位掩码，就能精确地只改一个寄存器 bit，而不会误伤相邻字段——这正是 `rw` 这种「 packed 位级控制表」最需要的写法。

#### 4.2.2 核心流程

命令包从入到出的完整生命周期：

```text
主机 USB ──64b(type=11)──► dcom.com_dout
                              │  _cmd_rx_wren = valid & MAGIC & TYPE_CMD
                              ▼
                   fifo_64_64_clk1_fifocmd   (命令 FIFO，跨节流)
                              │  cmd_rx_rd_en  = tickcount64[1] & (...)
                              ▼  cmd_rx_dout (64b)
                   ┌────────── 拆解 ──────────┐
                   │ address_byte = dout[31:16]   ◄── 同时承载路由标志
                   │   [15] = f_rw               (1=写 rw, 0=读 ro)
                   │   [14] = f_shadowcfgspace   (1=转给配置空间影子)
                   │   [14:0]<<3 = address_bit   (bit 地址)
                   │ value = {dout[55:48], dout[63:56]}   (字节倒序)
                   │ mask  = {dout[39:32], dout[47:40]}   (字节倒序)
                   │ read_flag  = dout[12]
                   │ write_flag = dout[13]
                   └────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        │ f_shadowcfgspace=1?  │                     │
        │   是 → 透传给 dshadow2fifo.rx_rden/rx_wren  │
        │        (由配置空间影子自己回响应)            │
        │   否 ↓                                       │
        │   read?  → 把 ro/rw 的 16 位窗口塞回 _cmd_tx_din (响应包)
        │   write? → 逐位掩码写 rw (要求 f_rw=1)
        └─────────────────────────────────────────────┘
                              │
                   _cmd_tx_din ──► fifo_34_34 i_fifo_cmd_tx ──► mux(p1) ──► FT601 ──► 主机
```

几个关键设计意图：

- **节流读取**：`cmd_rx_rd_en` 受 `tickcount64[1]` 控制，只在大约一半的时钟周期里读命令 FIFO。这是为了把上行 USB 带宽主要让给 TLP，命令只是低频控制流。
- **WAIT_COMPLETE 防丢包**：当 `rw[18]=1` 且有一次 DRP 正在进行时，暂停接受新命令，避免 DRP 结果被新命令覆盖。
- **写要求 `f_rw=1`**：地址字段最高位必须为 1 才允许写，天然防止「误把只读的 ro 当成 rw 写」。
- **shadow 旁路**：`f_shadowcfgspace=1` 的命令不读写 `ro`/`rw`，而是原样转发给 `dshadow2fifo`，由配置空间影子模块自己处理（这是 u4-l1 的入口）。

#### 4.2.3 源码精读

**命令 FIFO 与节流读取**（[pcileech_fifo.sv:328-343](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L328-L343)）：

```systemverilog
wire cmd_rx_rd_en_drp = rwi_drp_rd_en | rwi_drp_wr_en
                      | rw[RWPOS_DRP_RD_EN] | rw[RWPOS_DRP_WR_EN];
wire cmd_rx_rd_en = tickcount64[1] & ( ~rw[RWPOS_WAIT_COMPLETE] | ~cmd_rx_rd_en_drp);

fifo_64_64_clk1_fifocmd i_fifo_cmd_rx(
    .clk(clk), .srst(rst),
    .rd_en(cmd_rx_rd_en), .dout(cmd_rx_dout),
    .din(dcom.com_dout), .wr_en(_cmd_rx_wren),
    .valid(cmd_rx_valid)
);
```

`tickcount64[1]` 是节流的核心——只有计数器第 1 位为高（约一半周期）时才读，保证命令处理不会独占带宽。括号里的 `~rw[18] | ~cmd_rx_rd_en_drp` 是「WAIT_COMPLETE 门」：若开了等待完成且有 DRP 在飞，就停读新命令。

**命令包拆解**（[pcileech_fifo.sv:345-360](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L345-L360)）——这是本讲最值得逐行读的一段：

```systemverilog
wire [15:0] in_cmd_address_byte = cmd_rx_dout[31:16];
wire [17:0] in_cmd_address_bit  = {in_cmd_address_byte[14:0], 3'b000};  // 低15位×8
wire [15:0] in_cmd_value        = {cmd_rx_dout[48+:8], cmd_rx_dout[56+:8]};  // 字节倒序
wire [15:0] in_cmd_mask         = {cmd_rx_dout[32+:8], cmd_rx_dout[40+:8]};  // 字节倒序
wire        f_rw                = in_cmd_address_byte[15];   // 1=rw, 0=ro
wire        f_shadowcfgspace    = in_cmd_address_byte[14];   // 1=配置空间影子

// 16 位窗口读出（带越界保护，越界返回 0）
wire [15:0] in_cmd_data_in = (in_cmd_address_bit < (f_rw ? $bits(rw) : $bits(ro)))
                           ? (f_rw ? rw[in_cmd_address_bit+:16] : ro[in_cmd_address_bit+:16])
                           : 16'h0000;

wire in_cmd_read  = cmd_rx_valid & cmd_rx_dout[12] & ~f_shadowcfgspace;
wire in_cmd_write = cmd_rx_valid & cmd_rx_dout[13] & ~f_shadowcfgspace & f_rw;

// shadow 旁路
assign dshadow2fifo.rx_rden = cmd_rx_valid & cmd_rx_dout[12] &  f_shadowcfgspace;
assign dshadow2fifo.rx_wren = cmd_rx_valid & cmd_rx_dout[13] &  f_shadowcfgspace & f_rw;
```

注意三个精妙之处：

1. **地址字段一物两用**：`address_byte[15]` 当 rw/ro 选择位，`address_byte[14]` 当 shadow 旁路位，只有 `[14:0]` 真正做地址。地址转 bit 的公式是
   \[
   \text{address\_bit} = (\text{address\_byte} \,\&\, \texttt{0x7FFF}) \ll 3 = (\text{address\_byte} \,\&\, \texttt{0x7FFF}) \times 8
   \]
   例如 `address_byte = 0x0018` → `address_bit = 0x18 × 8 = 192`，指向 `rw[192]`。
2. **字节倒序**：`value`/`mask` 都用 `{低字节段, 高字节段}` 的倒序拼法，匹配 FT601 链路的 little-endian 字节序。这解释了为什么 u2-l2 那条命令 `64'h00000003_80182377` 里，mask `0x03` 出现在第 4 字节、value 在第 6 字节。
3. **越界保护**：读窗口前先比较 `address_bit < $bits(...)`，越界返回 0 而不是综合报错——这是一种「软件友好」的防御。

**写机制（逐位掩码）**（[pcileech_fifo.sv:402-410](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L402-L410)）：

```systemverilog
if ( tickcount64 < 8 )
    pcileech_fifo_ctl_initialvalues();        // 上电前 8 拍强制重载初值
else if ( in_cmd_write )
    for ( i_write = 0; i_write < 16; i_write = i_write + 1 )
        if ( in_cmd_mask[i_write] )
            rw[in_cmd_address_bit+i_write] <= in_cmd_value[i_write];
```

这就是「位寻址 + 掩码」的真相：一个 16 次的循环，每次看 `mask[i]` 是否为 1，是则把 `value[i]` 写到 `rw[address_bit+i]`。`mask=0x0300` 只会让 `i=8,9` 两轮生效，于是只改 2 个 bit。

**读回 / 响应打包**（[pcileech_fifo.sv:370-398](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L370-L398)）：

```systemverilog
// 读寄存器响应：回送地址 + 倒序的 16 位数据
else if ( in_cmd_read ) begin
    _cmd_tx_wr_en      <= 1'b1;
    _cmd_tx_din[31:16] <= in_cmd_address_byte;                      // 原样回显地址
    _cmd_tx_din[15:0]  <= {in_cmd_data_in[7:0], in_cmd_data_in[15:8]}; // 倒序数据
end
// 周期性「send count」自发包（地址标签 0xFFFE）
else if ( ~_cmd_tx_almost_full & ~in_cmd_write & _cmd_send_count_enable ) begin
    _cmd_tx_din[31:16] <= 16'hfffe;
    _cmd_tx_din[15:0]  <= _cmd_send_count_dword;
    rw[63:32]          <= _cmd_send_count_dword - 1;                // 自减
end
// 不活动超时自发包（地址标签 0xFFFF，数据 0xCEDE）
else if ( ~_cmd_tx_almost_full & ~in_cmd_write & _cmd_timer_inactivity_enable
          & (_cmd_timer_inactivity_ticks + _cmd_timer_inactivity_base < tickcount64) ) begin
    _cmd_tx_din[31:16] <= 16'hffff;
    _cmd_tx_din[15:0]  <= 16'hcede;
    rw[16]             <= 1'b0;                                     // 一次性，自关
end
```

响应包的高 16 位是「地址/标签」、低 16 位是「数据」。除了「主机主动读」的回显，还有两种**硬件自发**的上行包：

- **`0xFFFE`**：周期性「send count」，主机用来对账带宽/丢包；
- **`0xFFFF` + `0xCEDE`**：不活动超时通知，主机长时间没响应时卡片主动报告。

shadow 配置空间的响应则带一个特殊标签（[pcileech_fifo.sv:370-375](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L370-L375)）：

```systemverilog
if ( dshadow2fifo.tx_valid ) begin
    _cmd_tx_din[31:16] <= {4'b1100, dshadow2fifo.tx_addr, dshadow2fifo.tx_addr_lo, 1'b0};
    ...
```

最高 4 位 `1100` 意味着 `f_rw=1, f_shadowcfgspace=1`，主机据此识别「这是配置空间影子的响应」而非普通寄存器读。

#### 4.2.4 代码实践

> **实践目标**：手工解码一条真实命令包，验证你对「地址/数值/掩码」拆解的理解。

**操作步骤**：

1. 回顾 u2-l2 里 `com` 模块 `initial_rx[4]` 注入的最后一条命令：`64'h00000003_80182377`。
2. 按 4.2.3 的公式，把它拆成 `address_byte / value / mask / read / write / f_rw / f_shadowcfgspace`。
3. 推算它修改了 `rw` 的哪几个 bit、写成什么值，对应 4.1 表里的哪个控制位。

**预期结果**（请先自己算再对照）：

- `cmd_rx_dout = 0x00000003_80182377`
- `address_byte = dout[31:16] = 0x8018`
  - `f_rw = address_byte[15] = 1`（写 rw）
  - `f_shadowcfgspace = address_byte[14] = 0`（普通寄存器，非 shadow）
  - `address_bit = 0x0018 × 8 = 192`
- `mask = {dout[39:32], dout[47:40]} = {0x03, 0x00} = 0x0300`
- `value = {dout[55:48], dout[63:56]} = {0x00, 0x00} = 0x0000`
- `write_flag = dout[13]`：`0x8018` 的 bit13 = `0x8018` 即 `1000 0000 0001 1000`，bit13=1 → 是写。
- 循环里 `mask` 为 1 的位是 `i=8` 和 `i=9`，分别写 `rw[192+8]=rw[200]`、`rw[192+9]=rw[201]`，值均为 0。
- 查 4.1 表：`rw[200]` = **PCIE CORE RESET**，`rw[201]` = PCIE SUBSYSTEM RESET。

**结论**：这条命令把 PCIe 核从「上电默认的复位态」里放出来，让它开始 link training——正是 u2-l2 讲的「先有鸡先有蛋」问题的硬件解法。这就是本讲协议在真实代码里的样子。

> 待本地验证：以上是纯静态推算，若有条件可在仿真里给 `dcom.com_dout` 注入这 64 位、观察 `rw[200]` 是否在第 8 拍之后变为 0。

#### 4.2.5 小练习与答案

**练习 1**：主机想只读 `rw[205]`（BAR PIO 开关）这一位的值，命令包该怎么构造？

> **答案**：`address_byte` 取 `0x8000 | (205/8)` —— 但注意读是按 16 位窗口读，地址要对齐到「包含目标位的最小窗口」。`rw[205]` 落在 bit 地址 205，对齐到 16 位窗口边界（16 的倍数）是 `192`，对应字节地址 `192/8 = 0x18`。所以 `address_byte = 0x8018`（`f_rw=1`），置 `read_flag=1`、`write_flag=0`。回送的 16 位数据里，`(data >> (205-192)) & 1` 就是 BAR PIO 位。掩码和 value 对读操作无意义。

**练习 2**：为什么写操作要求 `f_rw=1`，而读操作不要求？

> **答案**：写会改变状态，必须显式声明目标是可写的 `rw`，防止软件 bug 把只读的 `ro` 当 `rw` 写（虽然综合上 `ro` 是 wire 写不进去，但这条逻辑在仿真和语义上是保护）。读是无害的，`f_rw=0` 时读 `ro`、`f_rw=1` 时读 `rw` 都合法，地址字段因此兼作「rw/ro 选择」。

**练习 3**：`tickcount64 < 8` 时强制重载初值（第 403 行），为什么需要这个？如果删掉会怎样？

> **答案**：上电最初几拍 `task` 的 `initial` 可能还没完全生效、或外部复位刚释放时 `rw` 处于不确定态。这 8 拍强制重载保证「PCIe 核复位位等关键控制」一定从已知初值开始。删掉的话，上电瞬间 `rw[200]`（核复位）可能不是 1，PCIe 核可能在主机软件准备好之前就开始训练，导致链路异常。

---

### 4.3 DRP 触发与 STARTUPE2 全局复位：寄存器位的两条副作用链

#### 4.3.1 概念说明

`rw` 里的位分成三类：

1. **纯存储位**：如设备身份证字段、计数器初值，写了就存着，由别处来读。
2. **持续驱动位**：如 +019 的 8 个控制位，每拍都被 `_pcie_core_config` 搬出去驱动 `dpcie`/`dshadow2fifo`（4.1 已讲）。
3. **触发位（one-shot）**：写了 1 之后，硬件启动一个异步过程，完成后自动清零。**DRP 读写**和**全局复位**就属此类。

**DRP 触发位**是 `rw[20]`（DRP RD EN）和 `rw[21]`（DRP WR EN）。主机配好 `rw[224+:9]`（DRP 地址）和 `rw[208+:16]`（DRP 写数据）后，把 `rw[20]` 或 `rw[21]` 写 1，PCIe 核就开始一次 DRP 访问；完成后结果出现在 `ro[271:256]`，请求位自动清零。

**全局复位位**是 `rw[31]`（GLOBAL SYSTEM RESET）。主机写 1 后，它驱动 Xilinx 的 `STARTUPE2` 原语的 `GSR` 输入，触发**整片 FPGA 的全局复位**（Global Set/Reset）——效果等同于重新加载 bitstream 的复位阶段，所有 `reg` 回到初值。这是「软件重启卡片」的终极手段。

#### 4.3.2 核心流程

**DRP 一次读/写的状态流**：

```text
主机:  写 rw[224+:9]=drp_addr, rw[208+:16]=drp_di(写时), 再写 rw[20 或 21]=1
                                   │
         ┌───────────── always @(posedge clk) ─────────────┐
         │ 见 rw[20]|rw[21]=1：                            │
         │   rw[20]<=0, rw[21]<=0        (请求位立即清零)  │
         │   rwi_drp_rd_en <= 1 (或 wr_en) (锁存到进行中)  │
         └─────────────────────────────────────────────────┘
                                   │
         dpcie.drp_en  = rwi_*_en | rw[20|21]    (持续高)
         dpcie.drp_we  = rwi_drp_wr_en | rw[21]
         dpcie.drp_addr= rw[224+:9]
         dpcie.drp_di  = rw[208+:16]
                                   │
                                   ▼  PCIe 核若干拍后...
         dpcie.drp_rdy ────────────► 1
                                   │
         ┌───────────── always @(posedge clk) ─────────────┐
         │ 见 drp_rdy=1：                                   │
         │   rwi_drp_data <= dpcie.drp_do  (读回值入 ro)    │
         │   rwi_drp_rd_en <= 0, rwi_drp_wr_en <= 0         │
         └─────────────────────────────────────────────────┘
                                   │
         ro[271:256] = rwi_drp_data  ◄── 主机下一次读 +020 即得结果
```

注意「请求位」和「进行中位」是分开的两套：`rw[20/21]` 是主机写的请求，`rwi_drp_*_en` 是硬件锁存的「进行中」状态（也回显到 `ro[20/21]` 让主机判断是否完成）。这种「请求 → 锁存 → 完成 → 清锁存」的两段式握手，保证了即便主机不再维持请求位，DRP 操作也会跑完。

#### 4.3.3 源码精读

**DRP 信号驱动**（[pcileech_fifo.sv:319-322](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L319-L322)）：

```systemverilog
assign dpcie.drp_en   = rw[RWPOS_DRP_WR_EN] | rw[RWPOS_DRP_RD_EN];  // = rw[21]|rw[20]
assign dpcie.drp_we   = rw[RWPOS_DRP_WR_EN];                        // = rw[21]
assign dpcie.drp_addr = rw[224+:9];
assign dpcie.drp_di   = rw[208+:16];
```

`RWPOS_*` 是文件上半部的 localparam（[pcileech_fifo.sv:207-210](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L207-L210)）：

```systemverilog
localparam RWPOS_WAIT_COMPLETE       = 18;
localparam RWPOS_DRP_RD_EN           = 20;
localparam RWPOS_DRP_WR_EN           = 21;
localparam RWPOS_GLOBAL_SYSTEM_RESET = 31;
```

**DRP 两段式握手状态机**（[pcileech_fifo.sv:416-429](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L416-L429)）：

```systemverilog
if ( dpcie.drp_rdy ) begin                                   // 完成：收结果、清进行中
    rwi_drp_rd_en <= 1'b0;
    rwi_drp_wr_en <= 1'b0;
    rwi_drp_data  <= dpcie.drp_do;
end
else if ( rw[RWPOS_DRP_RD_EN] | rw[RWPOS_DRP_WR_EN] ) begin  // 收到新请求：锁存、清请求位
    rw[RWPOS_DRP_RD_EN] <= 1'b0;
    rw[RWPOS_DRP_WR_EN] <= 1'b0;
    rwi_drp_rd_en <= rwi_drp_rd_en | rw[RWPOS_DRP_RD_EN];
    rwi_drp_wr_en <= rwi_drp_wr_en | rw[RWPOS_DRP_WR_EN];
end
```

读 `ro[271:256]`（DRP DO）的来源是 `rwi_drp_data`（[pcileech_fifo.sv:245](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L245)）；`ro[20]/[21]` 的来源是 `rwi_drp_rd_en/wr_en`（[pcileech_fifo.sv:230-231](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L230-L231)），主机据此轮询「DRP 是否完成」。

**STARTUPE2 全局复位**（[pcileech_fifo.sv:436-456](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L436-L456)）：

```systemverilog
`ifdef ENABLE_STARTUPE2
STARTUPE2 #(
  .PROG_USR("FALSE"), .SIM_CCLK_FREQ(0.0)
) i_STARTUPE2 (
  .CLK  ( clk ),                                        // <-
  .GSR  ( rw[RWPOS_GLOBAL_SYSTEM_RESET] | rst_cfg_reload ), // <- 全局复位
  .GTS  ( 1'b0 ), ...
);
`endif
```

`STARTUPE2` 是 Artix-7 里 always-on 的专用原语，它的 `GSR`（Global Set/Reset）一旦拉高，整片 FPGA 的所有寄存器同时回到初值。这里把 `GSR` 接到 `rw[31] | rst_cfg_reload`，于是「主机写 rw[31]=1」或「顶层请求 cfg 重载」都能软重启整卡。文件顶部 `define ENABLE_STARTUPE2（[第 14 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L14)）默认开启。

#### 4.3.4 代码实践

> **实践目标**：跟踪一次「主机发起的 DRP 写」从命令包到 PCIe 核、再到完成的完整路径，把 4.1、4.2、4.3 串起来。

**操作步骤**：

1. 假设主机要向 DRP 地址 `0x1F` 写数据 `0xBEEF`。按 4.2 的协议，主机需要发三条命令包（每次写一个 16 位窗口）：
   - 写 `rw[224+:9]`（DRP addr）= `0x001F`：`address_byte = 0x8000 | (0x1F*8 ... )` —— 注意 9 位字段要对齐窗口。请你自己算出每条包的 `address_byte / mask / value`。
   - 写 `rw[208+:16]`（DRP di）= `0xBEEF`。
   - 写 `rw[21]`（DRP WR EN）= 1：这一条只用 `mask` 选中 bit21。
2. 在源码里跟踪这三条命令各自的副作用：
   - 前两条走 4.2 的 `for` 循环写入，落到 `rw` 对应位。
   - 第三条置 `rw[21]=1` 后，下一拍 [第 319-320 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L319-L320) 立刻让 `dpcie.drp_en=1, drp_we=1`，同时 `drp_addr/drp_di` 也已就绪。
3. 继续跟踪 [第 423-429 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L423-L429)：`rw[21]` 被清 0、`rwi_drp_wr_en` 锁存为 1（回显到 `ro[21]`，主机看到「进行中」）。
4. 等到 `dpcie.drp_rdy=1`（[第 417-422 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L417-L422)），`rwi_drp_*_en` 清 0，`ro[21]` 回 0——主机轮询到 `ro[21]=0` 即知完成。

**需要观察的现象**：整个 DRP 读写过程中，主机只用了「写 rw 的若干位 + 轮询 ro[20]/[21] + 读 ro[271:256]」这几样原语，完全复用 4.2 的命令协议，没有专用命令码。这就是「**一张寄存器表 + 一条通用读写协议 = 全部控制能力**」的设计哲学。

> 待本地验证：DRP 完成需要多少拍取决于 PCIe 核内部，本讲无法给出确切数字；若要实测需在 Vivado 仿真里跑 `pcie_7x_0`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `dpcie.drp_en = rw[21] | rw[20]`，而不是 `rwi_drp_wr_en | rwi_drp_rd_en`？两种写法有什么区别？

> **答案**：两者实际等价（因为 `rwi_*_en` 一旦锁存就和 `rw[20/21]` 同时为高过）。这里用 `rw[21]|rw[20]` 的好处是「请求发出的当拍 `drp_en` 就拉高」，不浪费一拍；而 `rwi_*_en` 主要用于「即使主机清了请求位、操作也要跑完」的锁存语义。两者一起在 `cmd_rx_rd_en_drp` 里用作「DRP 进行中」判断。

**练习 2**：`rw[18]`（WAIT_COMPLETE）= 1 时，DRP 操作期间命令 FIFO 的读取会怎样？

> **答案**：[第 330-331 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L330-L331) 里 `cmd_rx_rd_en = tickcount64[1] & (~rw[18] | ~cmd_rx_rd_en_drp)`。当 `rw[18]=1` 且 `cmd_rx_rd_en_drp=1`（有 DRP 在飞）时，括号项为 0，命令 FIFO 停止读新命令。这避免新命令在 DRP 完成前挤进来改写 `rw` 的 DRP 字段，造成结果错乱。

**练习 3**：`STARTUPE2.GSR` 触发的全局复位和 `rst` 信号触发的复位，作用范围有何不同？

> **答案**：`rst` 是模块级的软复位，只让 `pcileech_fifo` 内部受 `if(rst)` 保护的 `reg`（如 `rw`）回初值。`STARTUPE2.GSR` 是芯片级硬复位，会让**整片 FPGA 所有模块**的所有寄存器同时回初值，包括 PCIe 核、com、mux 等不受 `rst` 管的寄存器。所以「写 `rw[31]=1`」是「重启整卡」，而 `rst` 只是「重置控制面」。

---

## 5. 综合实践

把本讲三个模块串成一个端到端的小任务：**手工仿真一次「主机打开 BAR PIO」的完整控制链路**。

**背景**：默认上电时 `rw[205]`（PCIE BAR PIO ON-BOARD PROCESSING ENABLE）已经是 1。现在假设你想做一个实验：先把它关掉（让 BAR 请求直接透传给主机），观察现象，再打开。请按以下步骤推演（纯源码阅读 + 纸面推算，不需上板）：

1. **构造命令包（关 BAR PIO）**：目标是只把 `rw[205]` 改成 0，其余位不动。
   - `rw[205]` 落在 bit 地址 205，对齐到 16 位窗口边界是 `192`（字节地址 `0x18`）。
   - 写命令：`address_byte = 0x8018`（`f_rw=1`），`write_flag=1`，`mask` 只选中窗口内 `(205-192)=13` 那一位 → `mask = 0x2000`，`value = 0x0000`。
   - 按字节倒序公式，写出完整的 64 位 `cmd_rx_dout`。
2. **跟踪副作用**：在 [第 313-318 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L313-L318) 确认 `rw[205]` → `_pcie_core_config[77]` → `dshadow2fifo.bar_en`。`bar_en=0` 后，配置空间影子模块（u4 单元）就不会把 BAR 请求交给板载 PIO 处理。
3. **读回验证**：构造一条读命令（`address_byte=0x8018, read_flag=1`），回送的 16 位数据里 bit13 应为 0，确认写入生效。
4. **再打开**：把 `value` 改成 `0x2000`、`mask` 不变再发一次，`bar_en` 回到 1。

**预期结果**：你能用「字节地址 + 16 位窗口 + 逐位掩码」这一套协议，精确控制 `rw` 的任意一个 bit，并预测它通过 `_pcie_core_config` 影响哪个 interface 输出。这就是 PCILeech 主机驱动控制这块卡片的核心机制。

> 进阶（可选）：如果你在做 u4 单元的 BAR 实验，把这套「先关 `bar_en` → 透传 → 观察 → 再开」的步骤当作调试手段，会非常有用。

---

## 6. 本讲小结

- `pcileech_fifo` 维护两张寄存器表：只读的 `ro`（320 位，硬件状态镜像）和可读写的 `rw`（240 位，主机控制面板）。二者的字段布局是 PCILeech 主机驱动的「控制寄存器映射」约定，改位置会破坏兼容性。
- `rw` 的 +019 字节（`rw[207:200]`）是 8 个核心控制位，经 `_pcie_core_config <= rw[207:128]` 整段搬运后，翻译成 `dpcie.pcie_rst_core`、`dshadow2fifo.cfgtlp_en`/`bar_en` 等真正的硬件输出——这是「软件控制硬件」的总开关面板。
- 命令包是一个 64 位结构：`address_byte[15:0]` 同时承载路由标志（`f_rw`/`f_shadowcfgspace`）和字节地址，低 15 位左移 3 位得到 bit 地址；`value`/`mask` 各 16 位、按字节倒序；`[12]/[13]` 是读/写标志。
- 写机制是「位寻址 + 16 位窗口 + 逐位掩码」：一个 16 次循环按 `mask` 逐位把 `value` 写进 `rw`，能精确改一个 bit 而不伤相邻字段；写要求 `f_rw=1`。
- 读机制回送一个响应包，高 16 位回显地址、低 16 位是倒序的 16 位窗口数据；此外还有 `0xFFFE`（send count）和 `0xFFFF/0xCEDE`（不活动超时）两种硬件自发包。
- DRP 读写是 one-shot 触发位（`rw[20/21]`）：主机配好地址/数据后写请求位，硬件锁存到 `rwi_drp_*_en`、驱动 `dpcie.drp_*`，完成后把 `dpcie.drp_do` 收进 `ro[271:256]` 并清请求；`rw[18]`（WAIT_COMPLETE）期间暂停新命令防丢包。`rw[31]` 经 `STARTUPE2.GSR` 触发整片 FPGA 全局复位。

---

## 7. 下一步学习建议

本讲把「主机软件 → fifo 寄存器 → 硬件行为」这条控制链讲透了。接下来：

- **进入 PCIe 核内部**：本讲里反复出现的 `dpcie`（DRP/复位）和 `dcfg`（配置读写）接口，另一端连着 PCIe 核封装 `pcileech_pcie_a7.sv`。建议进入 **u3-l1（PCIe 核心封装与 pcie_7x IP）**，看 DRP 请求是怎么被喂给 `pcie_7x_0` IP 的。
- **追踪 LTSSM 调试**：u3-l2（PCIe 配置空间管理）会讲 `ro` 里那些 PCIe 状态位（`pcie_present`/`pcie_perst_n` 以及更丰富的 LTSSM/链路状态）的来源；如果你对本讲的 ro 状态镜像感兴趣，u3-l2 是它的「PCIe 状态全集」。
- **动手 BAR 实验**：本讲的 `rw[205]`（`bar_en`）和 `rw[202]`（`cfgtlp_en`）是 u4 单元的入口。建议在读完 u3 后直接进入 **u4-l1（自定义配置空间影子）** 和 **u4-l2（BAR PIO 控制器）**，把本讲的「控制旋钮」接到真正的设备仿真逻辑上。
- **进阶调试**：u6-l3（高级主题：LTSSM、性能与调试）会用到本讲的「读 ro 寄存器」协议来远程读取链路状态，可作为本讲知识的综合应用场景。
