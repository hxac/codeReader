# 自定义配置空间影子（cfgspace_shadow）

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清「影子配置空间（shadow configuration space）」解决的是什么问题，以及它和 Xilinx PCIe 硬核自带配置空间的关系。
- 看懂 `pcileech_tlps128_cfgspace_shadow.sv` 如何从一条 128 位 TLP 流里解析出 `CfgRd/CfgWr` 请求（地址 / 数据 / tag / 字节使能 / Requester ID），并据此拼装出合规的 `CplD/Cpl` 完成包回送。
- 解释 PCIe、USB 两路写源的优先级复用与「冲突丢弃」策略，以及为何默认情况下主机只能读、不能经 PCIe 写影子空间。
- 理解 `dshadow2fifo`（`IfShadow2Fifo`）接口如何把主机经 USB 下发的命令安全地桥接到运行在 `clk_pcie` 域的 BRAM。
- 独立完成把 `pcileech_fifo.sv` 中 `rw[203]`（CFGTLP ZERO DATA）从 `1` 改为 `0` 的定制，并用 `lspci` 验证自定义配置空间是否生效。

## 2. 前置知识

本讲是「专家层」第一篇，假定你已经读过 u3 单元（PCIe 核心与 TLP 处理）。下面几个概念会反复出现，先统一口径：

- **配置空间（Configuration Space）**：每个 PCIe 功能（function）都有一段标准格式的寄存器空间，早期是 256 字节，扩展配置空间到 4KB。前 64 字节是「头类型」、Vendor/Device ID、Class Code、BAR、状态命令等，操作系统（如 Linux 的 `lspci`）就是读这里来识别设备的。注意 4KB 是按 **DWORD（4 字节）** 寻址的，共 1024 个 DWORD。
- **CfgRd / CfgWr**：根复合体（root complex，通常即主机 CPU 侧）读写某设备配置空间的 TLP。`CfgRd` 必须被回一个「带数据完成包 `CplD`」；`CfgWr` 必须被回一个「无数据完成包 `Cpl`」。每个配置请求都**必须**有一个完成包，否则 CPU 侧会超时。
- **影子（shadow）**：Xilinx 7 系列 PCIe 硬核 `pcie_7x_0` 内部自带一份配置空间，由 Vivado 生成 IP 时的 GUI 决定（VID/PID/Class 等，见 u3-l2）。这份空间**在运行时基本不可改**，而且改它要重新生成 IP。所谓「影子配置空间」，就是用一片用户可控的 BRAM（4KB）再造一份配置空间，让运行时（甚至主机经 USB）也能改写其中一部分字段，从而实现更灵活的设备仿真。
- **TLP 头部 7 位指纹**：u3-l4 已讲过，一个 TLP 首拍 DW0 的 `tdata[31:25]` 恰好是 `{Fmt[2:0], Type[4:1]}` 共 7 位（故意丢掉 `Type[0]`），用这一段就能识别出 Cfg/Cpl/CplD 等包型。本讲会反复用到这个指纹。
- **`IfAXIS128` / `IfShadow2Fifo` 契约**：u2-l1、u3-l3 已建立。本讲的模块一边是 `IfAXIS128.sink_lite`（只读消费 TLP 流，无反压），另一边是 `IfAXIS128.source`（产出完成包，有反压），还有一条 `IfShadow2Fifo.shadow` 连回 fifo 控制中枢。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [PCIeSquirrel/src/pcileech_tlps128_cfgspace_shadow.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_cfgspace_shadow.sv) | 本讲主角。包含三个模块：`pcileech_tlps128_cfgspace_shadow`（顶层调度）、`pcileech_cfgspace_pcie_tx`（拼装 CplD/Cpl 完成包）、`pcileech_mem_wrap`（BRAM + 写掩码包装）。 |
| [PCIeSquirrel/src/pcileech_header.svh](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh) | 定义 `IfShadow2Fifo` 与 `IfAXIS128` 接口契约。 |
| [PCIeSquirrel/src/pcileech_fifo.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv) | `rw[203]` 等 `_pcie_core_config` 控制位的定义与 `dshadow2fifo.*` 的赋值，以及主机命令桥接到影子的解析逻辑。 |
| [PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv) | 例化本模块的位置，`tlps_rx` 被本模块与 bar_controller、filter 三路并行消费。 |
| [PCIeSquirrel/build.md](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md) | 给出「改 `rw[203]` + 编辑 `.coe` + `lspci` 验证」的官方定制流程。 |
| [PCIeSquirrel/ip/pcileech_cfgspace.coe](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/ip/pcileech_cfgspace.coe) | 4KB 影子配置空间的 BRAM 初始化数据。 |
| [PCIeSquirrel/ip/pcileech_cfgspace_writemask.coe](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/ip/pcileech_cfgspace_writemask.coe) | 每一位置是否允许被改写的「写掩码」初始化数据。 |

## 4. 核心概念与源码讲解

### 4.1 为什么需要「影子」配置空间：模块全貌

#### 4.1.1 概念说明

Xilinx 7 系列 PCIe 硬核 `pcie_7x_0` 内部本来就维护着一份配置空间，其内容由 Vivado 生成 IP 时的 GUI 决定（IDs 标签页里的 Vendor/Device/Subsys/Class，见 u3-l2）。这份配置空间有两个不便：

1. **改一次要重新生成 IP**：改 VID/PID 需要在 Vivado 里打开 PCIe 核 GUI、改值、点 Generate，整个流程很重（u1-l3）。
2. **运行时不可改**：上电后这份空间基本是静态的，主机软件无法在运行时改写它。

文件头的注释一句话点出了「影子」模块的定位——只有当 Xilinx PCIe 核被配置成「把配置请求转发给用户应用」时，这些 CfgRd/CfgWr 才会流到本模块：

> PCIe custom shadow configuration space. Xilinx PCIe core will take configuration space priority; if Xilinx PCIe core is configured to forward configuration requests to user application such TLP will end up being processed by this module.  
> —— [pcileech_tlps128_cfgspace_shadow.sv:4-7](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_cfgspace_shadow.sv#L4-L7)

也就是说，本模块用一片用户可控的 4KB BRAM 再造一份配置空间，运行时能：

- **被 PCIe 总线读**：主机发 `CfgRd`，本模块回 `CplD`，数据来自 BRAM。
- **被 PCIe 总线写（可选）**：主机发 `CfgWr`，受 `cfgtlp_wren` 开关控制（默认关）。
- **被主机经 USB 改写**：经 `IfShadow2Fifo` 接口，主机软件可直接读写 BRAM，无需重建工程。

这正是 build.md 里「partly change the PCIe configuration space」所指的能力。

#### 4.1.2 核心流程

整个模块可以看作「**一个 BRAM + 两个时钟域 + 三路数据源**」的调度器：

```text
                      clk_pcie 域                           clk_sys 域
                ┌───────────────────────────┐         ┌────────────────────┐
  tlps_rx ──┐   │  ① 解析 CfgRd/CfgWr        │         │  主机命令 (fifo侧) │
 (128b AXIS)│   │  ② USB 命令经 CDC FIFO 进  │ ◀────── │  dshadow2fifo.rx_* │
            ▼   │  ③ 优先级复用 → BRAM 读/写 │         └────────────────────┘
   ┌─────────────┐                                   ┌────────────────────┐
   │ pcileech_   │                                   │ 回读响应 (USB)     │
   │ mem_wrap    │─── rd_data ──┐                    │ dshadow2fifo.tx_*  │
   │ (BRAM+掩码) │              │  ── CDC FIFO ──▶   │                    │
   └─────────────┘              ▼                    └────────────────────┘
                          cfgtlp_zero 门控
                                │
                 ┌──────────────┴───────────────┐
                 ▼                              ▼
      pcileech_cfgspace_pcie_tx        fifo_43_43_clk2
      （拼 CplD/Cpl → tlps_cfg_rsp）    （USB 读回应 → clk_sys）
```

数据流分四段：① 从 `tlps_rx` 解析 PCIe 配置请求；② 主机 USB 命令经双时钟 FIFO 跨到 `clk_pcie`；③ 两组请求按优先级复用进同一片 BRAM；④ 读结果按来源分别走 PCIe 完成包或 USB 回读路径。

#### 4.1.3 源码精读

模块的端口就是上面那张图的精确写照——三个时钟/复位、一条 `sink_lite` 的 TLP 输入、一条 `source` 的完成包输出、一条 `IfShadow2Fifo.shadow` 控制线，外加一个 `pcie_id`（本核的总线/设备/功能号，用来当完成包的 Completer ID）：

[pcileech_tlps128_cfgspace_shadow.sv:15-23](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_cfgspace_shadow.sv#L15-L23) —— 模块声明。`tlps_in` 用 `sink_lite`（只读、无反压），因为 `tlps_rx` 同时被 bar_controller、filter 三路消费（见 4.1.2 图与 [pcileech_pcie_tlp_a7.sv:44-52](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L44-L52)），多读者不能反压上游。

模块内部例化了三个子模块，对应「BRAM 包装」「PCIe 完成包拼装」「USB 跨域」三个职责：

[pcileech_tlps128_cfgspace_shadow.sv:96-140](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_cfgspace_shadow.sv#L96-L140) —— `pcileech_mem_wrap`（BRAM）、`pcileech_cfgspace_pcie_tx`（CplD/Cpl）、`fifo_43_43_clk2`（USB 回读响应从 `clk_pcie` 跨到 `clk_sys`）三个例化点。注意三者用同一组 `bram_rd_*` 信号串联：BRAM 读出的数据既喂给 PCIe 完成包拼装器，也喂给 USB 回读 FIFO。

#### 4.1.4 代码实践

**实践目标**：在源码里把「一条 CfgRd 从进模块到变成 CplD 出模块」的完整路径走一遍，建立整体直觉。

**操作步骤**：

1. 打开 [pcileech_pcie_tlp_a7.sv:44-52](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L44-L52)，确认本模块的 `tlps_in` 接的是同一个 `tlps_rx`（与 bar_controller、filter 并联），`tlps_cfg_rsp` 接的是 `sink_mux1` 的 `tlps_in1`（最高优先级，见 u3-l5）。
2. 在 `pcileech_tlps128_cfgspace_shadow.sv` 里依次定位四段：L27-28（识别 CfgRd/CfgWr）、L88-94（选 BRAM 读地址与来源类型）、L97-112（BRAM 读）、L114-126（拼 CplD 输出）。
3. 画一条时序线，标出从 `tlps_in.tvalid` 到 `tlps_cfg_rsp.tvalid` 经过了几个 `clk_pcie` 拍（提示：BRAM 读有 1 拍延迟，见 4.3.3 的 `wr_be_d` 对齐）。

**需要观察的现象 / 预期结果**：你能用一句话讲清「CfgRd 进 → BRAM 读 1 拍 → CplD 出」的最小延迟链路，并指出 `cfgtlp_zero`（`rw[203]`）在哪一拍把数据「清零」。延迟拍数与 BRAM 的固有读延迟相关，**待本地验证**（可在仿真里数拍）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `tlps_in` 用 `sink_lite` 而不是带 `tready` 的 `sink`？  
**答案**：因为 `tlps_rx` 同时被 bar_controller、cfgspace_shadow、filter 三个模块消费（多读者）。如果用带反压的 `sink`，多个读者会争抢 `tready`，无法一致地暂停上游；`sink_lite` 只读不反压，配合上游 `tlps128_src64` 保证「不丢包」的前提（见 u3-l3/u3-l5），是最简单也最安全的多读接法。

**练习 2**：本模块的 `pcie_id` 输入从哪来、用来干什么？  
**答案**：`pcie_id` 是本 PCIe 核被分配到的「总线号:设备号:功能号」（由硬核 `cfg_bus_number/cfg_device_number/cfg_function_number` 拼成，见 u3-l1/u3-l2）。它被填进完成包 DW1 的高 16 位当 **Completer ID**，让主机知道这个 `CplD` 是「谁」回的。

---

### 4.2 CfgRd/CfgWr TLP 解析与 CplD 响应生成

#### 4.2.1 概念说明

本模块的第一项核心职责：从 128 位 AXIS 流里把「配置请求」挑出来，并把请求里需要的字段（地址、数据、tag、字节使能、Requester ID）切出来，最后拼一个合规的完成包回送。

先复习一个 3DW 头的配置 TLP 在 128 位 `tdata` 里是怎么摆的（每格 32 位，小端 DWORD 序）：

| 字段 | bit 位置 | 含义 |
| --- | --- | --- |
| DW0 头 | `tdata[31:0]` | `Fmt[31:29]`、`Type[28:25]`、`Length[9:0]` 等 |
| DW1 头 | `tdata[63:32]` | Requester ID `[63:48]`、Tag `[47:40]`、字节使能 `[35:32]` |
| DW2 头 | `tdata[95:64]` | 总线/设备/功能号、扩展寄存器号、寄存器号 |
| DW3 数据 | `tdata[127:96]` | 仅 CfgWr 有：要写入的 32 位数据 |

关键：配置空间 4KB = 1024 个 DWORD，所以地址只需要 10 位。这 10 位恰好藏在 DW2 的 `[11:2]`（即 `{扩展寄存器号[3:0], 寄存器号[5:0]}`），对应到 128 位流里就是 `tdata[75:66]`。

#### 4.2.2 核心流程

```text
tlps_in.tvalid && tuser[0](首拍)
        │
        ├─ tdata[31:25]==7'b0000010 ?  →  CfgRd (Fmt=000 无数据)
        └─ tdata[31:25]==7'b0100010 ?  →  CfgWr (Fmt=010 带数据)
        │
        ▼ 切字段
 pcie_rx_addr = tdata[75:66]   (10 位 DWORD 地址)
 pcie_rx_data = tdata[127:96]  (写数据)
 pcie_rx_tag  = tdata[47:40]   (匹配请求/完成)
 pcie_rx_be   = {tdata[32..35]}(第一 DW 字节使能)
 pcie_rx_reqid= tdata[63:48]   (请求者 ID)
        │
        ▼ 经 BRAM 读 (1 拍)
        │
   ┌────┴───── 根据 rd_tlpwr 选 ─────┐
   ▼                                  ▼
 CplD (读完成, 带数据, 4DW)        Cpl (写完成, 无数据, 3DW)
 tkeepdw=1111                       tkeepdw=0111
```

CfgRd 与 CfgWr 的 7 位指纹分别是 `0000010` 与 `0100010`：高 3 位是 `Fmt`（`000`=读/无数据，`010`=写/带数据），低 4 位是 `Type[4:1]=0010`（配置型，丢掉 `Type[0]` 即可同时匹配 type0/type1）。

#### 4.2.3 源码精读

识别 + 切字段全部用纯组合逻辑完成，集中在一个块里：

[pcileech_tlps128_cfgspace_shadow.sv:27-33](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_cfgspace_shadow.sv#L27-L33) —— 识别 CfgRd/CfgWr 并切出地址/数据/tag/be/reqid。注意三个细节：① 只在首拍 `tuser[0]` 判一次；② `pcie_rx_be={tdata[32],tdata[33],tdata[34],tdata[35]}` 把 DW1 的「第一 DW 字节使能」按 BE0..BE3 的顺序排好，直接能喂给 BRAM 的字节写使能；③ 10 位地址 `tdata[75:66]` 正好索引 4KB。

完成包的拼装在子模块 `pcileech_cfgspace_pcie_tx` 里，用 4 个常量拼出 DW0..DW3：

[pcileech_tlps128_cfgspace_shadow.sv:160-167](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_cfgspace_shadow.sv#L160-L167) —— CplD/Cpl 的四个头字。读完成包 DW0 = `01001010...0001`（Fmt=010 带数据、Type=1010 完成包、Length=1 DW）；写完成包 DW0 = `00001010...0000`（Fmt=000 无数据、Type=1010、Length=0）。`cfg_tlpwr` 选读还是写。

DW1 里塞了 Completer ID（本设备 `pcie_id`，经 `_bs16` 字节序交换）和 `0x0004`（完成状态 `000`=成功，字节计数 4）；DW2 里塞了 Requester ID（`cfg_reqid`）、tag、低位地址 0；DW3 仅读完成包用，放 `cfg_data`。

最后用一个 129 位单时钟 FIFO（`{tp, tdata}`）做弹性缓冲，再据 `tp` 决定 `tkeepdw`：

[pcileech_tlps128_cfgspace_shadow.sv:171-188](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_cfgspace_shadow.sv#L171-L188) —— `fifo_129_129_clk1`（两个端口都在 `clk_pcie`，所以是 `clk1`）把拼好的 129 位包缓存，`tkeepdw = tx_tp ? 4'b1111 : 4'b0111`：读完成包 4 个 DWORD 全有效，写完成包只 3 个（DW3 是填充）。

#### 4.2.4 代码实践

**实践目标**：手工解码两个完成包 DW0 常量，验证它们确实是合规的 CplD / Cpl。

**操作步骤**：

1. 取 `cpl_tlp_data_dw0_rd = 32'b01001010000000000000000000000001`，按 `[31:29]=Fmt`、`[28:25]=Type`、`[9:0]=Length` 三段切开。
2. 取 `cpl_tlp_data_dw0_wr = 32'b00001010000000000000000000000000`，同样切三段。
3. 对照 PCIe 规范的完成包格式（Fmt：`000`=3DW 无数据、`010`=3DW 带数据；Type `01010`=完成包）核对。

**预期结果**：

| 常量 | Fmt[31:29] | Type[28:25] | Length[9:0] | 含义 |
| --- | --- | --- | --- | --- |
| dw0_rd | `010` | `1010` | `1` | CplD，3DW 头 + 1DW 数据 |
| dw0_wr | `000` | `1010` | `0` | Cpl，3DW 头，无数据 |

这是纯位运算推导，结论确定，无需上板。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `pcie_rx_be` 要写成 `{tdata[32], tdata[33], tdata[34], tdata[35]}` 这种「逐位拼」的顺序，而不是直接 `tdata[35:32]`？  
**答案**：PCIe 的「第一 DW 字节使能」字段里，bit0 对应字节 0（BE0）。`{tdata[32],...,tdata[35]}` 得到 `{BE0,BE1,BE2,BE3}`，即 `pcie_rx_be[0]=BE0`，这与 BRAM 字节写使能 `wea` 的位序一致，可以直接相接，省去再翻转一次。

**练习 2**：一个 CfgWr 请求会触发本模块回什么？回的包里有没有数据？  
**答案**：回一个 `Cpl`（无数据完成包），`cfg_tlpwr=1` 选 `cpl_tlp_wr`，`tkeepdw=0111`（只 3 个 DWORD 头，DW3 填充忽略）。CfgWr 必须有完成包，否则 CPU 侧写操作会超时。

---

### 4.3 三路写源优先级复用、冲突丢弃与 BRAM 写掩码

#### 4.3.1 概念说明

影子配置空间有「三个潜在的写来源」：① PCIe 总线上的 CfgWr；② 主机经 USB 下发的写命令；③ 内部状态机（预留）。它们都要写同一片 BRAM，于是在同一拍里可能撞车。模块用一个**固定优先级 + 冲突丢弃**的朴素多路复用器来处理：注释里写明优先级是 PCIe > USB > INTERNAL，并且「冲突会被丢弃（假定极少发生）」。

[pcileech_tlps128_cfgspace_shadow.sv:60-63](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_cfgspace_shadow.sv#L60-L63) —— 设计意图注释。

此外，BRAM 的写入并不是「整字覆盖」，而是**逐位受一份写掩码控制**：每一位置可以单独设成「只读」。这份掩码来自一片分布式 ROM（`drom_pcie_cfgspace_writemask`），其内容由 `pcileech_cfgspace_writemask.coe` 初始化（默认全 1 = 全可写）。这让你能把 Vendor ID 之类的关键字段锁死，只允许改次要字段。

#### 4.3.2 核心流程

写路径的优先级仲裁（伪代码）：

```text
if (CfgWr && cfgtlp_en)              # 来源①PCIe，最高优先
    we = (cfgtlp_wren ? pcie_rx_be : 0)
    data = pcie_rx_data
elif (usb_rx_wren)                   # 来源②USB
    we = usb_rx_be
    data = usb_rx_data
else                                 # 来源③INTERNAL（本版未接入）
    we = 0; data = 0                  # 等效无写
```

注意一个**默认只读**的关键设计：PCIe 写即使被识别（`bram_wr_1_tlp=1`），其字节使能还要再过一道 `cfgtlp_wren`（`rw[206]`，默认 `0`）。所以**默认情况下主机不能经 PCIe 写影子空间，只能读**；想经 PCIe 改配置必须先置 `rw[206]=1`。这把「被主机随便改配置」的风险关掉了。

BRAM 写入时的逐位掩码（伪代码）：

```text
对每个 bit i (0..31):
    wr_dina[i] = wr_mask[i] ? wr_data_d[i]    # 掩码允许 → 写新值
                            : rd_data[i]       # 掩码禁止 → 保持原值
```

由于 BRAM 读有 1 拍延迟，写地址/数据/使能都被寄存一拍（`wr_be_d`、`wr_data_d`、`rd_addr`）来与读端口对齐，注释写作 `DELAY TO FOLLOW BRAM DELAY`。

#### 4.3.3 源码精读

写多路复用就五行组合逻辑，优先级链一目了然：

[pcileech_tlps128_cfgspace_shadow.sv:65-68](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_cfgspace_shadow.sv#L65-L68) —— `bram_wr_1_tlp` 抢先；否则 `bram_wr_2_usb`；否则 `be=0/data=0`（无写）。`bram_wr_be` 里那层 `cfgtlp_wren ? pcie_rx_be : 4'b0000` 就是「PCIe 写默认禁用」的开关。

读侧的多路复用与此对称，并且把「来源类型」记进 `rdreq_tp`，供后面决定回包走哪条路：

[pcileech_tlps128_cfgspace_shadow.sv:84-94](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_cfgspace_shadow.sv#L84-L94) —— `bram_rd_data_z = cfgtlp_zero ? 0 : bram_rd_data`（`rw[203]` 在这里把读出数据清零，见 4.4）；`bram_rd_valid = (rd_tp==TLP)` 决定**只对 PCIe 读**回 CplD，USB 读的响应走另一条 FIFO。`bram_rdreq_tp` 三选一：TLP 优先、其次 USB、否则 IDLE。

BRAM 包装模块 `pcileech_mem_wrap` 是逐位掩码写入的核心：

[pcileech_tlps128_cfgspace_shadow.sv:225-258](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_cfgspace_shadow.sv#L225-L258) —— 寄存一拍对齐 BRAM 延迟（L225-234）；双端口 BRAM `bram_pcie_cfgspace`（A 口写、B 口读，L237-245）；分布式 ROM `drom_pcie_cfgspace_writemask` 提供 32 位逐位掩码（L248-251）；`generate` 循环按掩码逐位选「新值/原值」（L253-258）。

#### 4.3.4 代码实践

**实践目标**：理解写掩码 `.coe` 如何把某一字段锁成只读。

**操作步骤**：

1. 打开 [ip/pcileech_cfgspace_writemask.coe](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/ip/pcileech_cfgspace_writemask.coe)，确认默认是全 `ffffffff`（每一 DWORD 的 32 位都可写）。
2. 假设你想把 **配置空间偏移 0x00 处的 Vendor ID（低 16 位）** 锁成只读。0x00 是第 0 个 DWORD，对应 `.coe` 第 1 个向量。
3. 把第 1 个向量从 `ffffffff` 改成 `ffff0000`（低 16 位清 0 = 低 16 位禁止写）。
4. 对照 4.3.3 的 `generate` 循环说明：当 `wr_mask[i]=0` 时 `wr_dina[i]` 取 `rd_data[i]`（原值），即该位写不进去。

**需要观察的现象 / 预期结果**：改完后，任何来源（PCIe 或 USB）对该 DWORD 低 16 位的写都会被「静默忽略」，读回仍是原值；高 16 位照常可写。是否真的生效需要重新生成 BRAM IP 并上板验证，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：注释说写源优先级是「PCIe > USB > INTERNAL」，代码里真有三路吗？  
**答案**：代码里实际只接入了 PCIe（`bram_wr_1_tlp`）和 USB（`bram_wr_2_usb`）两路；第三路 INTERNAL 在本版未接线，落到 `be=0/data=0` 等于「无写」。注释保留了三路的说法，应是设计预留。

**练习 2**：为什么默认 `rw[206] (cfgtlp_wren)=0` 是一个安全设计？  
**答案**：它让主机经 PCIe 的 `CfgWr` 不真正写入 BRAM（字节使能被强制为 0），配置空间在 PCIe 侧呈「只读」。只有显式置 `rw[206]=1` 才开放 PCIe 写。这样既允许主机读自定义配置，又防止主机随意篡改影子空间，降低了被反向利用的风险。

---

### 4.4 IfShadow2Fifo 接口：主机经 USB 桥接配置空间 BRAM

#### 4.4.1 概念说明

前两节的写源里，「USB 那一路」其实是一整条跨时钟域的桥：主机软件发一条命令 → fifo 控制中枢把它解析成「读/写影子空间」的请求 → 跨到 `clk_pcie` 域 → 读写 BRAM → 把读结果跨回 `clk_sys` 域 → 打包成命令响应回主机。承载这条桥的就是 `IfShadow2Fifo` 接口。

这条桥的存在让「运行时改配置」成为可能：不用重建工程、不用碰 Vivado，主机软件（或脚本）就能直接读写 4KB 影子空间的任意 DWORD。这也是 build.md 里「partly change the PCIe configuration space」与「改 `rw[203]`」这套定制能落地的基础设施。

#### 4.4.2 核心流程

```text
主机命令包 (MAGIC type=11)
   │  in_cmd_address_byte[14]=1  (f_shadowcfgspace 标志位)
   ▼
 fifo 解析出 rx_rden/rx_wren/rx_be/rx_addr(10b)/rx_addr_lo/rx_data
   │  (clk_sys 域)
   ▼  fifo_49_49_clk2  (49 位, clk_sys → clk_pcie)
   │
   ▼  cfgspace_shadow: usb_rx_*  (clk_pcie 域)
   ▼  优先级复用 → BRAM 读
   │
   ▼  fifo_43_43_clk2  (43 位, clk_pcie → clk_sys)
   │  dout = {tx_addr_lo, tx_addr, tx_data}
   ▼
 fifo 把 tx_* 打包成命令响应 → 经 mux 回 FT601 → 主机
```

控制位（`cfgtlp_en`/`cfgtlp_zero`/`cfgtlp_wren` 等）则由 fifo 从 `rw` 寄存器整段搬运过来，单向 `clk_sys → clk_pcie`（它们变化慢，不需要专用 CDC FIFO）。

两个双时钟 FIFO 的位宽名字直接暴露了打包内容：`fifo_49_49_clk2` = 49 位 = `{rden(1), wren(1), addr_lo(1), addr(10), be(4), data(32)}`；`fifo_43_43_clk2` = 43 位 = `{tag[0](1), addr(10), data(32)}`。尾缀 `_clk2` = 双时钟，`_clk1` = 单时钟。

#### 4.4.3 源码精读

接口契约 `IfShadow2Fifo` 把「fifo↔shadow」之间所有信号打包，并按方向拆成两个 modport：

[pcileech_header.svh:267-295](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L267-L295) —— `fifo` modport 输出 `cfgtlp_*` 控制位与 `rx_*` 命令、输入 `tx_*` 响应；`shadow` modport 方向相反。注意它把「配置空间影子」和「BAR PIO」（`bar_en`）的开关位也塞进了同一条接口，因为两者都由 `_pcie_core_config` 统一驱动。

fifo 侧把主机命令路由到影子（关键标志 `f_shadowcfgspace = in_cmd_address_byte[14]`）：

[pcileech_fifo.sv:350-360](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L350-L360) —— 当命令地址的 bit14 置位时，读写不再走 `rw/ro` 寄存器文件，而是改走 `dshadow2fifo.rx_rden/rx_wren`。`rx_addr = in_cmd_address_byte[11:2]`（10 位 DWORD 地址），`rx_addr_lo = in_cmd_address_byte[1]`（选 16 位高低半字），`rx_be` 由 mask 与 addr_lo 译出，`rx_data` 把 16 位值复制成 32 位。

控制位来自 `_pcie_core_config`（即 `rw[207:128]`）：

[pcileech_fifo.sv:310-318](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L310-L318) —— `dshadow2fifo.cfgtlp_en = _pcie_core_config[74]`、`cfgtlp_zero = [75]`、`cfgtlp_filter = [76]`、`bar_en = [77]`、`cfgtlp_wren = [78]`、`alltlp_filter = [79]`。对照 [pcileech_fifo.sv:287-294](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L287-L294) 的 `rw` 初始化可知：`rw[202]=1`（CFGTLP PROCESSING ENABLE，开 cfgtlp_en）、`rw[203]=1`（CFGTLP ZERO DATA，开 cfgtlp_zero，默认清零）、`rw[204]=1`（cfgtlp_filter）、`rw[205]=1`（bar_en）、`rw[206]=0`（cfgtlp_wren 关）。

影子模块侧用两个 CDC FIFO 完成跨域：

[pcileech_tlps128_cfgspace_shadow.sv:47-58](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_cfgspace_shadow.sv#L47-L58) —— `fifo_49_49_clk2`（`wr_clk=clk_sys`、`rd_clk=clk_pcie`）把主机命令搬过来；`rd_en=1'b1` + `valid` 表示「来了才取」。

[pcileech_tlps128_cfgspace_shadow.sv:129-140](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_cfgspace_shadow.sv#L129-L140) —— `fifo_43_43_clk2`（`wr_clk=clk_pcie`、`rd_clk=clk_sys`）把 USB 读响应搬回 `clk_sys`，`wr_en = (bram_rd_tp==USB)` 即「只对 USB 读回送」。

最后 fifo 侧把 `tx_*` 打包成命令响应回主机：

[pcileech_fifo.sv:370-375](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L370-L375) —— `dshadow2fifo.tx_valid` 有效时，把 `{4'b1100, tx_addr, tx_addr_lo, 1'b0}` 当地址、`tx_data` 当数据塞进命令回送 FIFO。

#### 4.4.4 代码实践（本讲主实践：改 `rw[203]` 并用 `lspci` 验证）

**实践目标**：把 `rw[203]`（CFGTLP ZERO DATA）从 `1` 改为 `0`，解释配置空间影子模块的行为变化，并用 Linux `lspci` 验证。

**操作步骤**：

1. 先读懂代码层面的因果关系（这部分**确定**，无需上板）：
   - `rw[203]` 经 `_pcie_core_config[75]` → `dshadow2fifo.cfgtlp_zero`（[pcileech_fifo.sv:314](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L314)）。
   - `cfgtlp_zero` 在 [pcileech_tlps128_cfgspace_shadow.sv:84](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_cfgspace_shadow.sv#L84) 门控：`bram_rd_data_z = cfgtlp_zero ? 32'h0 : bram_rd_data`。
   - 这个 `bram_rd_data_z` 同时喂给 PCIe 完成包（`cfg_data`）和 USB 读响应（`fifo_43_43_clk2`）。
   - 因此：`rw[203]=1`（默认）→ 所有影子读返回 **0**；`rw[203]=0` → 返回 **真实 BRAM 内容**（即 `pcileech_cfgspace.coe` 初始化的值）。
2. 按 [build.md:42-57](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md#L42-L57) 的指引，在 `src/pcileech_fifo.sv` 把
   ```verilog
   rw[203]     <= 1'b1;   // CFGTLP ZERO DATA
   ```
   改成
   ```verilog
   rw[203]     <= 1'b0;   // CFGTLP ZERO DATA (0 = CUSTOM CONFIGURATION SPACE ENABLED)
   ```
   （对应 [pcileech_fifo.sv:290](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L290)）。
3. （可选，定制具体内容）编辑 `ip/pcileech_cfgspace.coe` 修改你想要的字段（注意 build.md 提醒：Xilinx 硬核会「in-part override」一部分字段）。
4. 重新构建并烧录（流程见 u1-l3）。
5. 在目标 Linux 主机上验证：
   ```bash
   lspci -d 10ee:0666 -xxxx
   ```
   （`-xxxx` 表示 dump 扩展配置空间的全 4KB）。

**需要观察的现象 / 预期结果**：
- `rw[203]=1`（默认）时，影子空间的读返回 0，`lspci` 看到的是 Xilinx 硬核自带的配置（默认「Xilinx Ethernet Adapter / Device ID 0x0666」，见 [build.md:16](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md#L16)）。
- `rw[203]=0` 后，影子 BRAM 的真实内容进入完成包，`lspci -xxxx` 的 dump 中会反映出 `pcileech_cfgspace.coe` 的值（受 Xilinx 硬核部分覆盖的限制）。
- build.md 明确说明：PCILeech 自身目前读不到这块自定义配置空间，**只能用 `lspci` 在 Linux 上看**（[build.md:55](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md#L55)）。
- 上述 `lspci` 输出对比需要真实硬件，**待本地验证**。代码层面的行为变化（清零 vs 真实数据）是确定的。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `rx_addr` 只取 `in_cmd_address_byte[11:2]` 这 10 位？  
**答案**：影子配置空间是 4KB = 1024 个 DWORD，10 位地址正好索引全部 1024 项。`[11:2]` 是把「字节地址」右移 2 位（除 4）转成「DWORD 地址」；bit1 留作 `rx_addr_lo`（选 16 位半字），bit14 用作 `f_shadowcfgspace` 路由标志，bit15 用作 `f_rw`（读/写标志）。

**练习 2**：USB 读响应为什么用 `fifo_43_43_clk2` 而 PCIe 完成包用 `fifo_129_129_clk1`？  
**答案**：两者跨越的时钟域不同。USB 响应要从 `clk_pcie` 跨回 `clk_sys`（给 fifo 控制中枢），所以用双时钟 FIFO（`_clk2`），43 位 = `{tag[0], addr(10), data(32)}`。PCIe 完成包从头到尾都在 `clk_pcie` 域（`tlps_cfg_rsp` 直接进同域的 `sink_mux1`），所以用单时钟弹性缓冲（`_clk1`），129 位 = `{tp(1), tdata(128)}`。

---

## 5. 综合实践

**任务**：在不重新生成 Xilinx PCIe IP 的前提下，仅靠「影子配置空间」把设备的 **Subsystem Vendor ID（偏移 0x2C 低 16 位）** 改成一个自定义值，并预测 `lspci` 的输出变化。

**建议步骤**：

1. **定位字段**：配置空间偏移 0x2C 是第 `0x2C >> 2 = 11` 号 DWORD（0-indexed）。打开 [ip/pcileech_cfgspace.coe](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/ip/pcileech_cfgspace.coe)，找到第 11 个向量（注意每行 4 个 DWORD，每行起始地址在该行注释里）。把它的低 16 位改成你的值（如 `0x1234`）。
2. **打开影子**：按 4.4.4 把 `rw[203]` 改成 `1'b0`，启用自定义配置空间。
3. **检查写掩码**：确认 [ip/pcileech_cfgspace_writemask.coe](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/ip/pcileech_cfgspace_writemask.coe) 第 11 个向量对应位为 1（允许该位被 Xilinx 硬核/USB 覆盖时不挡）。
4. **构建+烧录+验证**：重建工程烧录后，在目标机执行 `lspci -d 10ee:0666 -xxxx`，找到 `2c:` 这一行，检查低 16 位。
5. **运行时改写（进阶）**：尝试不重建工程，利用 4.4 讲的 USB 桥（`f_shadowcfgspace` 命令）在运行时直接写第 11 号 DWORD，再用 `lspci` 看变化。这需要你写一小段主机端命令（参考 u2-l5 的命令包格式：MAGIC=0x77、type=11、地址 bit14=1）。

**预期与提醒**：
- build.md 反复强调 Xilinx 硬核会「in-part override」用户配置值——Subsystem ID 这类字段是否被覆盖，**待本地验证**。
- 若某字段始终被硬核覆盖、影子改不动，正确做法是改在 Vivado PCIe 核 GUI 的 IDs 标签页（见 u3-l2、u4-l4），而不是死磕影子空间。这正是「影子」与「硬核 GUI」两条定制路径的分工。

## 6. 本讲小结

- 「影子配置空间」是用一片用户可控的 4KB BRAM 再造一份 PCIe 配置空间，弥补 Xilinx 硬核配置空间「改一次要重生 IP、运行时不可改」的两个不便。
- 本模块从 128 位 TLP 流里用 `tdata[31:25]` 的 7 位指纹识别 `CfgRd(0000010)`/`CfgWr(0100010)`，切出 10 位 DWORD 地址、写数据、tag、字节使能、Requester ID。
- 读请求回 `CplD`（带数据，4DW，`tkeepdw=1111`），写请求回 `Cpl`（无数据，3DW，`tkeepdw=0111`），完成包 DW0 常量里编码了 Fmt/Type/Length。
- 写源优先级为 PCIe > USB（INTERNAL 预留未接），冲突丢弃；且 PCIe 写默认被 `cfgtlp_wren (rw[206])=0` 关闭，主机在 PCIe 侧只能读、不能写影子。
- BRAM 写入是逐位受 `pcileech_cfgspace_writemask.coe` 控制的「写掩码」，能把关键字段锁成只读；写地址/数据/使能寄存一拍以对齐 BRAM 读延迟。
- `IfShadow2Fifo` 是一条完整的跨域桥：fifo 把主机命令（`f_shadowcfgspace` 标志）解析成 `rx_*`，经 `fifo_49_49_clk2`(clk_sys→clk_pcie) 送到 BRAM，读响应经 `fifo_43_43_clk2`(clk_pcie→clk_sys) 回送。
- `rw[203] (cfgtlp_zero)` 是「自定义配置空间总开关」：`1`=读返回 0（默认，影子不可见）；`0`=读返回真实 BRAM（影子生效），可用 `lspci -d 10ee:0666 -xxxx` 验证。

## 7. 下一步学习建议

- **u4-l2 BAR PIO 控制器：读写引擎**：本讲的「影子配置空间」只仿真了配置头空间；下一讲进入 BAR PIO（`pcileech_tlps128_bar_controller`），看主机访问 BAR 内存窗口时如何被读/写引擎拦截并产生 `CplD`，二者共同构成「设备仿真」的两条腿。
- **u4-l3 BAR 示例实现与 `.coe` 初始化**：继续深挖 `.coe` 文件如何初始化 BRAM/分布式 ROM，并对照本讲的 `pcileech_cfgspace.coe` / `pcileech_cfgspace_writemask.coe`，建立对 IP 初始化数据的整体认识。
- **u4-l4 设备身份定制**：把本讲的 `rw[203]` + `.coe` 路径，与 u3-l2 的 DSN、Vivado IDs GUI 路径放在一起对比，形成「改设备身份的完整工具箱」。
- **回看 u3-l4 / u3-l5**：如果对 CfgRd/CfgWr 如何从硬核 `m_axis_rx` 一路流到本模块、完成包又如何经 `sink_mux1` 仲裁回到硬核还不够清楚，建议回看 u3-l4（过滤与 FIFO 桥接）与 u3-l5（多路复用与 64/128 位转换）。
