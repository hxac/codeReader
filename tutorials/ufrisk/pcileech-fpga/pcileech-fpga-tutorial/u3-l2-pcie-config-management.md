# PCIe 配置空间管理

## 1. 本讲目标

本讲精读 `pcileech_pcie_cfg_a7.sv`，讲清 PCIe 配置空间（Configuration Space）在 pcileech-fpga 中是如何被「读写、镜像、注入」的。学完后你应该能够：

- 说清 `pcileech_pcie_cfg_a7` 模块在三大子系统中的定位，以及它与 PCIe 硬核 `pcie_7x_0` 之间的契约接口 `IfPCIeSignals`。
- 画出主机软件经 USB→fifo→cfg 模块发起一次配置空间 `cfg_mgmt` 读/写的完整握手时序，并指出完成信号是谁。
- 看懂 `ro`/`rw` 两张寄存器表如何把 PCIe 的总线号、设备号、LTSSM、链路速率、中断等上百个状态位镜像成主机可读的字节图。
- 解释 `cfg_dsn`（设备序列号）为什么是改变设备特征最方便的途径，以及它与 VID/PID 修改方式的区别。
- 描述静态 TLP（`tlps_static`）的注入用途与节拍。

本讲承接 u3-l1（PCIe 核心封装 `pcileech_pcie_a7`）——那一讲把 cfg 子模块当成「黑盒」引出，本讲正是打开这个黑盒。

## 2. 前置知识

在进入源码前，先用通俗语言把几个概念建立起来。

**PCIe 配置空间（Configuration Space）**：每个 PCIe 设备都有一块 4KB 的特殊寄存器区，主机（root complex）通过它来「认识」设备——读出厂商 ID、设备 ID、类别码、BAR、链路状态，写入命令、中断使能等。它是设备上线时主机和设备「握手登记」的地方。

**cfg_mgmt（Configuration Management）**：Xilinx 7 系列 PCIe 硬核提供的一组端口，允许 FPGA 内部逻辑在运行时读写这片配置空间（不只是被动地被主机读写）。这相当于给固件开了一个「后门」，能动态修改配置寄存器或读取当前状态。相关端口有：

- `cfg_mgmt_rd_en` / `cfg_mgmt_wr_en`：发起一次读/写请求（模块→核）。
- `cfg_mgmt_dwaddr`：要读写的 DWORD 地址（配置空间以 32 位为单位编址）。
- `cfg_mgmt_di`：要写入的 32 位数据；`cfg_mgmt_byte_en`：4 个字节使能。
- `cfg_mgmt_do`：核返回的读出数据；`cfg_mgmt_rd_wr_done`：完成脉冲（核→模块）。

**DSN（Device Serial Number）**：PCIe 扩展能力之一，是一串 64 位的设备序列号，主机用 `lspci -vv` 就能读出来。它常被当作设备的「指纹」。

**LTSSM（Link Training and Status State Machine）**：PCIe 链路训练状态机，描述链路从 Detect、Polling、Configuration 一路到 L0（正常工作）所经历的状态。`pl_ltssm_state` 就是当前状态的镜像，是判断「链路到底通没通」的第一手信号。

**ro / rw 寄存器文件**：本模块沿用 pcileech-fpga 全工程统一的控制寄存器风格（u2-l5 已详细讲过协议本身）——`ro` 是只读状态镜像，`rw` 是可读写控制面板。本讲关注的是**字段内容**（即每个比特映射到哪个 PCIe 状态/控制位），而不是读写协议本身。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pcileech_pcie_cfg_a7.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv) | 本讲主角。配置空间读写管理、ro/rw 寄存器镜像、DSN/中断/静态 TLP 的产生地。 |
| [pcileech_header.svh](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh) | 定义 `IfPCIeSignals`（cfg 模块与硬核之间的契约）和 `IfPCIeFifoCfg`（fifo 与 cfg 之间的命令通道）。 |
| [pcileech_pcie_a7.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv) | 上一讲的封装层。此处只看它如何把 cfg 模块、`ctx` 与硬核连起来（例化点 + 端口对接）。 |
| [build.md](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md) | 官方构建说明，含「修改 DSN」「自定义配置空间」的官方建议，是本讲实践的依据。 |

## 4. 核心概念与源码讲解

### 4.1 IfPCIeSignals：cfg 模块与 PCIe 硬核之间的契约

#### 4.1.1 概念说明

`pcie_7x_0` 是 Xilinx 的 PCIe 硬核 IP，它有上百个端口。如果让每个使用方模块都各自去挑端口接线，会非常混乱。工程的做法是把这些端口集中收进一个叫 `IfPCIeSignals` 的「超级接口」，再由 `pcileech_pcie_cfg_a7` 这一个模块统一负责配置面（cfg_mgmt、DSN、中断、链路控制等），其余模块则各管各的数据面（TLP 收发）。这是 u3-l1 提到的「控制统一、数据分散」原则在 cfg 子系统上的落地。

注意：硬核 IP 本身**不能**用 modport（IP 端口是扁平信号），所以 `pcileech_pcie_a7.sv` 里 `i_pcie_7x_0` 直接接到 `ctx` 的裸线上；而 cfg 模块用 `IfPCIeSignals.mpm ctx` 的 modport 视角接入。两端连的是**同一个** `ctx` 实例（在 `pcileech_pcie_a7.sv` 第 40 行声明 `IfPCIeSignals ctx();`），方向天然互补。

#### 4.1.2 核心流程

`IfPCIeSignals` 的信号按方向分两大组（见头文件注释「VALUES FROM PCIe TO module」和「VALUES FROM module TO PCIe」）：

1. **PCIe→模块（状态镜像，cfg 模块读）**：总线/设备/功能号、`cfg_command`、`cfg_status`、LTSSM、链路速率/宽度、`cfg_dcommand`/`cfg_dstatus`、AER 错误标志、中断使能状态、`tx_buf_av` 等。
2. **模块→PCIe（控制请求，cfg 模块写）**：`cfg_mgmt_*`（配置读写）、`cfg_dsn`（DSN）、`pl_directed_link_*`（链路定向控制）、`cfg_interrupt_*`（中断）、`cfg_pm_*`（电源管理）、`rx_np_ok`/`tx_cfg_gnt` 等流控位。

modport `mpm` 就是把这两组方向以契约形式固化下来。

#### 4.1.3 源码精读

接口声明与「PCIe→模块」的状态信号集中在头文件前半段（`cfg_mgmt_do`、`cfg_mgmt_rd_wr_done` 在这里出现，是 cfg_mgmt 读写的返回侧）：

[pcileech_header.svh:40-114](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L40-L114) — `IfPCIeSignals` 接口的前半段：PCIe 状态信号（bus/device/function、LTSSM、链路、命令/状态、AER、中断返回等）与 `cfg_mgmt_do`/`cfg_mgmt_rd_wr_done` 读回。

「模块→PCIe」的控制信号在后半段，包含 cfg_mgmt 请求侧、DSN、链路定向、中断、电源管理：

[pcileech_header.svh:106-141](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L106-L141) — cfg_mgmt 请求信号（`cfg_mgmt_rd_en`/`wr_en`/`di`/`dwaddr`/`byte_en`）、`cfg_dsn`、`pl_directed_link_*`、`cfg_interrupt_*`、`cfg_pm_*`、`rx_np_ok`/`tx_cfg_gnt`。

modport `mpm` 把方向写成契约（一大段 input 对应状态，一大段 output 对应控制）：

[pcileech_header.svh:142-158](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L142-L158) — `mpm` modport：左侧 input 全是「PCIe 给模块的状态」，右侧 output 全是「模块给 PCIe 的控制」。cfg 模块挂这个 modport，就等于承诺：我只驱动这些控制线、只读那些状态线。

在 `pcileech_pcie_a7.sv` 中，硬核的 cfg_mgmt / DSN 端口直接接 `ctx` 的裸线，与 cfg 模块形成闭环：

[pcileech_pcie_a7.sv:142-151](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L142-L151) — 硬核 `cfg_mgmt_*` 端口对接：`cfg_mgmt_dwaddr`/`byte_en`/`di`/`rd_en`/`wr_en` 是「模块→核」，`cfg_mgmt_do`/`cfg_mgmt_rd_wr_done` 是「核→模块」。

[pcileech_pcie_a7.sv:177](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L177) — `cfg_dsn` 由 cfg 模块产生，经 `ctx.cfg_dsn` 直接送进硬核（这是 DSN 的唯一注入点，4.4 节会展开）。

#### 4.1.4 代码实践

1. **实践目标**：建立「cfg 模块 = PCIe 配置面唯一代理人」的直觉。
2. **操作步骤**：
   - 打开 `pcileech_header.svh`，对照 `IfPCIeSignals` 的 `mpm` modport（142–158 行）。
   - 打开 `pcileech_pcie_a7.sv`，在 `i_pcie_7x_0` 例化里搜索这些信号名，确认每条 output 类的信号（如 `cfg_mgmt_rd_en`、`cfg_dsn`、`cfg_interrupt`）都标注了 `// <-`（核接收），每条 input 类信号（如 `cfg_status`、`pl_ltssm_state`、`cfg_mgmt_rd_wr_done`）都标注了 `// ->`（核产生）。
3. **需要观察的现象**：modport 中标记为 output 的信号，在硬核例化里**全部**是 `// <-`（核的输入）；反之亦然。
4. **预期结果**：你会看到方向严格互补，没有任何一个信号在两端都是输入或都是输出——这就是契约层的价值。

#### 4.1.5 小练习与答案

**练习 1**：为什么硬核 `pcie_7x_0` 不直接用 `mpm` modport，而要接裸线？
**答**：硬核是 Xilinx 闭源 IP，端口是扁平信号、不支持 modport 语法；modport 只服务于用户 HDL 模块（这里是 cfg 模块）。两端连同一个 `ctx` 实例的裸线即可保证方向一致。

**练习 2**：`cfg_mgmt_rd_wr_done` 在 `IfPCIeSignals` 中属于哪一方向？
**答**：它是「PCIe→模块」（核完成读写后产生），所以在 modport 里是 input，在硬核例化里是 `// ->`。

---

### 4.2 cfg_mgmt 配置空间读写管理：双 FIFO 跨时钟域 + 握手

#### 4.2.1 概念说明

主机软件想读写 PCIe 配置空间时，不能直接碰到硬核——硬核跑在 `clk_pcie`（PCIe 用户时钟）域，而主机命令经 USB/ft601 到达时还停在 `clk_sys`（系统时钟）域。`pcileech_pcie_cfg_a7` 模块夹在中间，做三件事：

1. 用两个双时钟 FIFO 把命令/响应在 `clk_sys` 与 `clk_pcie` 之间安全搬运。
2. 解析命令（沿用全工程统一的「字节地址 + 16 位窗口 + 掩码」协议，u2-l5 已讲）。
3. 把对配置空间的读写请求翻译成 `cfg_mgmt_*` 握手，交给硬核执行，再把结果回送主机。

模块的端口清楚反映了它「跨时钟、跨域」的本质——同时吃 `clk_sys` 和 `clk_pcie` 两个时钟：

[pcileech_pcie_cfg_a7.sv:13-21](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L13-L21) — 模块声明：`clk_sys`/`clk_pcie` 双时钟；`dfifo`（`IfPCIeFifoCfg.mp_pcie`）是与 fifo 模块之间的命令通道；`ctx`（`IfPCIeSignals.mpm`）是与硬核之间的契约；`tlps_static` 是注入静态 TLP 的出口；`pcie_id` 回送总线/设备/功能号给 TLP 模块用作响应的 Requester ID。

#### 4.2.2 核心流程

下行（主机→配置空间）数据通路：

```
fifo模块(clk_sys) ──tx_data/tx_valid──> fifo_64_64 ──(clk_pcie)──> in_dout ──> 命令解析 ──> rw寄存器写 / cfg_mgmt触发
                  wr_clk=clk_sys                rd_clk=clk_pcie
```

上行（配置空间→主机）数据通路：

```
读回结果(out_data, clk_pcie) ──> fifo_32_32_clk2 ──(clk_sys)──> rx_data/rx_valid ──> fifo模块 ──> 主机
                                wr_clk=clk_pcie  rd_clk=clk_sys
```

cfg_mgmt 一次读写的完整握手（**本讲实践要求识别的关键点**）：

1. 主机把参数写进 `rw`：`cfg_mgmt_di`(rw[159:128])、`cfg_mgmt_dwaddr`(rw[169:160])、`cfg_mgmt_byte_en`(rw[175:172])，再置 `rw[16]`(RD_EN) 或 `rw[17]`(WR_EN) 为 1。
2. 模块状态机检测到 RD_EN/WR_EN，清零该触发位，并置内部 `rwi_cfg_mgmt_rd_en`/`wr_en = 1`。
3. 该内部位经门控输出到硬核：`ctx.cfg_mgmt_rd_en = rwi_cfg_mgmt_rd_en & ~cfg_mgmt_rd_wr_done`（电平保持到完成）。
4. 硬核处理后拉高 **`cfg_mgmt_rd_wr_done`**（完成信号）并给出 `cfg_mgmt_do`（读数据）。
5. 模块检测到 done，锁存结果到 `rwi_cfgrd_*`（镜像进 ro 供主机读），并清掉 rd_en/wr_en——握手结束。
6. 可选流控：主机置 `rw[18]`(WAIT_COMPLETE) 可让模块在当前 cfg_mgmt 未完成前**不再**从输入 FIFO 取新命令，避免背靠背命令 overrun。

#### 4.2.3 源码精读

下行双时钟 FIFO（`clk_sys`→`clk_pcie`，深 512×64）：

[pcileech_pcie_cfg_a7.sv:45-56](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L45-L56) — `fifo_64_64 i_fifo_pcie_cfg_tx`：写入侧接 `dfifo.tx_data/tx_valid`（`clk_sys`），读出侧 `in_dout` 进 `clk_pcie` 域，完成主机命令的时钟域搬迁。

上行双时钟 FIFO（`clk_pcie`→`clk_sys`，带 almost_full 反压）：

[pcileech_pcie_cfg_a7.sv:66-78](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L66-L78) — `fifo_32_32_clk2 i_fifo_pcie_cfg_rx`：写入侧是模块产生的 `out_data`（`clk_pcie`），读出侧回送 `dfifo.rx_data/rx_valid`（`clk_sys`）；`pcie_cfg_rx_almost_full` 反馈给输入侧做反压。

cfg_mgmt 请求的门控输出（请求电平保持到 done 拉高）：

[pcileech_pcie_cfg_a7.sv:262-263](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L262-L263) — `ctx.cfg_mgmt_rd_en = rwi_cfg_mgmt_rd_en & ~ctx.cfg_mgmt_rd_wr_done`（写使能同理）。`& ~done` 保证完成脉冲一出现就自动撤销请求，无需主机干预。

握手状态机（完成处理 + 触发起停）：

[pcileech_pcie_cfg_a7.sv:379-399](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L379-L399) — 三段式：① done 拉高时锁存 `cfg_mgmt_do`/`dwaddr`/`byte_en` 到 `rwi_cfgrd_*` 并清请求；② 否则若主机置 RD_EN 则启动一次读；③ 若置 WR_EN 则启动一次写。`rwi_cfg_mgmt_rd_en/wr_en` 与 `rw[RD_EN/WR_EN]` 一起汇成 `pcie_cfg_rw_en`（330 行），用于输入流控。

输入读取使能（反压 + WAIT_COMPLETE + 隔拍）：

[pcileech_pcie_cfg_a7.sv:330-335](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L330-L335) — `in_rden = tickcount64[1] & ~almost_full & (~WAIT_COMPLETE | ~pcie_cfg_rw_en)`。`tickcount64[1]` 隔一拍取一次（给状态机留反应时间）；`~almost_full` 是输出 FIFO 反压；最后一项表示「若开了 WAIT_COMPLETE，则 cfg_mgmt 进行中暂停取新命令」。

#### 4.2.4 代码实践

1. **实践目标**：亲手追踪一次 cfg_mgmt 读写的完整握手链路。
2. **操作步骤**：
   - 在 `pcileech_pcie_cfg_a7.sv` 中定位 4 个关键行：请求触发 `RWPOS_CFG_RD_EN`(188 行)、门控输出(262 行)、完成处理(379 行)、流控(335 行)。
   - 假设主机要读配置空间 DWORD 地址 1（命令/状态寄存器所在）：写出主机应依次写哪些 `rw` 字段、置哪些位；再标出硬核侧哪一根线会被拉高、模块靠哪一根线感知完成。
3. **需要观察的现象**：完成信号 `cfg_mgmt_rd_wr_done` 一旦为 1，下一拍 `rwi_cfg_mgmt_rd_en` 是否被清 0、`rwi_cfgrd_data` 是否更新为 `cfg_mgmt_do`。
4. **预期结果**：完成握手信号是 **`cfg_mgmt_rd_wr_done`**（核→模块），请求信号是 `cfg_mgmt_rd_en`/`cfg_mgmt_wr_en`（模块→核），二者构成「请求—完成」一对。读回数据出现在 `ro[383:352]`(`rwi_cfgrd_data`)，配 `ro[347]`(`rwi_cfgrd_valid`) 有效标志。

#### 4.2.5 小练习与答案

**练习 1**：为什么 cfg_mgmt 请求要做成 `& ~cfg_mgmt_rd_wr_done` 的电平门控，而不是单脉冲？
**答**：硬核处理一次 cfg_mgmt 需要若干拍，期间请求必须保持有效；而完成时机由硬核决定。门控表达式保证请求一直保持到 done 出现，再自动撤销，既稳妥又省去主机轮询。

**练习 2**：`RWPOS_CFG_WAIT_COMPLETE`(rw[18]) 关闭(=0) 和开启(=1) 时，输入 FIFO 的读取行为有何不同？
**答**：关闭时 `in_rden` 不关心 cfg_mgmt 是否在忙，命令可能背靠背涌入，吞吐高但若后续动作慢可能 overrun；开启时一旦有 cfg_mgmt 在进行就暂停取新命令，更安全但吞吐略低。

---

### 4.3 ro/rw 寄存器文件与 PCIe 状态镜像

#### 4.3.1 概念说明

`pcileech_pcie_cfg_a7` 把 PCIe 硬核上百个状态/控制位整理成两张表，与 PCILeech 主机驱动约定的「控制寄存器映射」严格对齐：

- `ro[383:0]`（48 字节，只读）：硬件状态镜像——把 `ctx` 上的状态信号原样「摊平」成一张字节图，主机按字节偏移读。
- `rw[703:0]`（88 字节，读写）：主机控制面板——上电由 task 初始化默认值，主机可改写，输出侧再 `assign` 到 `ctx` 的控制信号。

字节偏移与比特位置的换算遵循「小端字节图」约定：第 N 字节对应比特区间 \([8N, 8N+7]\)。例如 `ro[79:64]` 即字节 +008（含 bus/device/function 号）。这套路数与 u2-l5 讲过的 fifo 模块完全一致。

#### 4.3.2 核心流程

**镜像写入（硬件→ro）**：用一长串 `assign ro[...] = ctx....` 把每个状态信号钉死在固定比特位置，注释里标注字节偏移（+008、+00A、+010 …）。主机读 ro 即等于读这些 PCIe 状态。

**控制输出（rw→硬件）**：对称地用 `assign ctx.... = rw[...]` 把主机写进 rw 的值送到硬核控制端口。

**主机访问协议**：与 fifo 模块共用——64 位命令包里，`address_byte[15]` 区分 rw(1)/ro(0)，低 15 位左移 3 位得比特地址，16 位窗口按 `mask` 逐位写（仅 rw 可写），命令包里的 read/write 位决定动作。本节不重复协议细节，重点看**字段布局**。

#### 4.3.3 源码精读

ro 寄存器布局（PCIe 状态镜像，注释里的 +008 等是字节偏移）：

[pcileech_pcie_cfg_a7.sv:103-180](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L103-L180) — 关键镜像点：`ro[15:0]`=MAGIC(0x2301)；`ro[71:64]/[76:72]/[79:77]`=bus/device/function 号；`ro[85:80]`=`pl_ltssm_state`(LTSSM)；`ro[97:96]`=`pl_sel_lnk_width`、`ro[98]`=`pl_phy_lnk_up`(链路通断)；`ro[127]`=`cfg_mgmt_rd_wr_done`；`ro[159:128]`=`cfg_mgmt_do`(cfg_mgmt 读回数据)；`ro[175:160]`=`cfg_command`；`ro[207:192]`=`cfg_dcommand`、`ro[239:224]`=`cfg_dstatus`、`ro[287:272]`=`cfg_status`；`ro[345:336]/[347]/[351:348]/[383:352]`=`rwi_cfgrd_*`(本模块自己产生的配置读回结果)。

rw 控制位定义与上电默认值（task 形式，便于 initial 与复位复用）：

[pcileech_pcie_cfg_a7.sv:188-260](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L188-L260) — `RWPOS_CFG_RD_EN=16`/`WR_EN=17`/`WAIT_COMPLETE=18`/`STATIC_TLP_TX_EN=19`/`CFGSPACE_STATUS_CL_EN=20`/`CFGSPACE_COMMAND_EN=21` 等控制位；rw[127:64]=DSN 默认 `0x0000000101000A35`；rw[159:128]/[169:160]/[175:172]=cfg_mgmt_di/dwaddr/byte_en；rw[176:184]=`pl_directed_link_*`(链路定向)；rw[199:207]=中断控制；rw[256+:384]=静态 TLP 内容；rw[672+:32]=状态寄存器清除周期(默认 62500≈1ms@62.5MHz)。

rw→ctx 的控制输出汇聚：

[pcileech_pcie_cfg_a7.sv:265-296](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L265-L296) — 一段 `assign ctx.... = rw[...]`：`cfg_dsn`=rw[127:64]、`cfg_mgmt_di`/`dwaddr`/`byte_en`、`pl_directed_link_*`、`cfg_interrupt_*`、`cfg_pm_*`、`rx_np_ok`/`tx_cfg_gnt` 等。主机改 rw 的对应比特，就等于直接拨动了这些硬核控制端口。

主机命令解析（地址/值/掩码/读写标志，与 fifo 同协议）：

[pcileech_pcie_cfg_a7.sv:322-329](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L322-L329) — `in_cmd_address_byte`=dout[31:16]，`in_cmd_address_bit`={address_byte[14:0],3'b000}（字节地址×8 得比特地址），`in_cmd_value`/`in_cmd_mask` 为交换过字节序的 16 位值/掩码，`f_rw`=address_byte[15] 选 rw/ro，`in_cmd_read`=dout[12]，`in_cmd_write`=dout[13] 且要求 `f_rw`。

读回与按掩码写入的实际执行：

[pcileech_pcie_cfg_a7.sv:344-358](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L344-L358) — 读：把命中的 16 位窗口（来自 ro 或 rw）经字节序交换后写入上行 `out_data`；写：16 次 for 循环按 `in_cmd_mask[i]` 逐位把 `in_cmd_value` 写进 `rw` 的对应比特。

#### 4.3.4 代码实践

1. **实践目标**：学会「按字节偏移」在 ro 里定位一个 PCIe 状态字段。
2. **操作步骤**：
   - 在 ro 布局(103–180 行)中找出：LTSSM、链路通断(`pl_phy_lnk_up`)、当前协商的链路宽度(`pl_sel_lnk_width`)、链路速率(`pl_sel_lnk_rate`)各自位于哪个字节偏移、哪几位。
   - 再算一下：若主机想读「LTSSM」，应当在命令包里把字节地址设为多少？（提示：字节地址 = 比特地址÷8，且最高位 0 表示读 ro。）
3. **需要观察的现象**：LTSSM 是 6 位，占 `ro[85:80]`，正好落在字节 +00A 的低位；链路通断 `pl_phy_lnk_up` 在 `ro[98]`。
4. **预期结果**：LTSSM 字节偏移为 +00A（比特 80–85）。主机读命令里 `address_byte` 应取 `16'h000A`（最高位 0=ro，低 15 位=0x000A）。这一题与 u6-l3「读回 LTSSM」的调试流程直接衔接。

#### 4.3.5 小练习与答案

**练习 1**：`ro[159:128]`（字节 +010）是什么？它与 `rwi_cfgrd_data`（ro[383:352]）有何区别？
**答**：`ro[159:128]`=`cfg_mgmt_do`，是硬核 cfg_mgmt 读操作的直接数据输出；`rwi_cfgrd_data` 是本模块在 `cfg_mgmt_rd_wr_done` 时把 `cfg_mgmt_do` 连同地址/字节使能一起锁存下来的「最近一次配置读结果快照」，含有效标志，方便主机事后取用。

**练习 2**：为什么写操作只允许落在 rw，不允许写 ro？
**答**：ro 是硬件状态的实时镜像，写它没有意义（下一拍又被 `assign` 覆盖）。代码用 `in_cmd_write = dout[13] & f_rw & in_valid` 强制写操作必须 `f_rw=1`（即地址最高位=1，指向 rw）。

---

### 4.4 DSN、中断与静态 TLP 发送

#### 4.4.1 概念说明

这一节覆盖 cfg 模块的三个「特殊用途」输出：

- **DSN（设备序列号）**：一个 64 位值，经 `cfg_dsn` 端口注入硬核，最终出现在 PCIe 扩展能力里。它**完全由 SystemVerilog 源码决定**（`rw[127:64]`），改一行即可、无需重生成 Xilinx IP，因此是改变设备特征最廉价的途径——这也是 `build.md` 官方推荐改它的原因。
- **中断（cfg_interrupt_*）**：传统 PCIe INTx/MSI/MSI-X 的控制位，主机软件可经 rw 触发 `cfg_interrupt`/`cfg_interrupt_assert`，硬核返回 `cfg_interrupt_rdy`/`cfg_interrupt_mmenable` 等状态。
- **静态 TLP（tlps_static）**：固件可预先在 rw 里塞好一整个 TLP（最多 8 个 DWORD = 256 位），由模块按节拍重复发往 PCIe 链路。常用于上电后主动向主机注入某些报文（如模拟设备初始化握手）。它经 `tlps_static`（`IfAXIS128.source`）送到 `pcileech_pcie_tlp_a7`，再汇入发送多路复用。

#### 4.4.2 核心流程

**DSN 链路**：

```
rw[127:64] (上电默认 0x0000000101000A35) ──assign──> ctx.cfg_dsn ──> pcie_7x_0.cfg_dsn ──> PCIe配置空间「Device Serial Number」扩展能力
```

**静态 TLP 节拍**：rw[256+:384] 存 8 个 DWORD 的 TLP，rw[224+:8]/[232+:8] 存每个 DWORD 的 {last, valid} 标志，rw[240+:16] 存发送间隔（tick 掩码），rw[640+:32] 存重发次数。状态机在空闲且 timer 命中、重发次数>0 时，把 256 位 TLP 拆成两个 128 位 beat（DWORD3..0、DWORD7..4）经 `tlps_static` 推出，重发次数减到 0 自动关闭。

**状态寄存器自动清零**：rw[20]/[21] 可让模块周期性地（默认 62500 拍≈1ms）自动发起一次 cfg_mgmt 写，清除配置空间状态寄存器里的 master abort 标志、置位命令寄存器的 bus master 位——避免某些主机因探测遗留的错误标志而把设备踢下线。这是 `pcileech_pcie_cfg_a7.sv:361-376` 那段「自行构造 cfg_mgmt 写」的逻辑。

#### 4.4.3 源码精读

DSN 的唯一注入点（默认值 + assign + 硬核对接）：

[pcileech_pcie_cfg_a7.sv:215](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L215) — 上电默认 `rw[127:64] <= 64'h0000000101000A35;  // cfg_dsn`。

[pcileech_pcie_cfg_a7.sv:265](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L265) — `assign ctx.cfg_dsn = rw[127:64];`（主机也可在运行时改写 rw 来动态换 DSN）。

中断与链路定向、电源管理控制位输出（rw→ctx）：

[pcileech_pcie_cfg_a7.sv:280-296](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L280-L296) — `cfg_interrupt_di`/`cfg_pciecap_interrupt_msgnum`/`cfg_interrupt_assert`/`cfg_interrupt`/`cfg_interrupt_stat`=rw[199:207]；`cfg_pm_force_state`/`cfg_pm_halt_aspm_*`/`cfg_trn_pending`/`rx_np_ok`/`tx_cfg_gnt` 等一一映射。

静态 TLP 的 128 位打包（按 `rwi_tlp_static_2nd` 选高低两半）：

[pcileech_pcie_cfg_a7.sv:298-313](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L298-L313) — `tlps_static.tdata` 在 `2nd` 为 0 时取 DWORD3..0（首拍，`tuser[0]=1`），为 1 时取 DWORD7..4（末拍）；`tlast`/`tkeepdw` 由各 DWORD 的 valid/last 标志组合；`tvalid = rwi_tlp_static_valid && tkeepdw[0]`。

静态 TLP 状态机（空闲节拍 + 两拍发送 + 计数自停）：

[pcileech_pcie_cfg_a7.sv:401-420](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L401-L420) — ① `STATIC_TLP_TX_EN` 关闭或已发完末拍→停；②/③ 两个 beat 在 `tlps_static.tready` 时交替送出；④ 空闲态当 `tickcount64` 低 16 位与睡眠掩码全等且重发次数>0 且 DWORD0 有效时启动新一轮，重发次数自减，减到 1 时自动清 `STATIC_TLP_TX_EN`。

状态寄存器自动清零（周期性自构造 cfg_mgmt 写）：

[pcileech_pcie_cfg_a7.sv:361-376](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L361-L376) — 计数到 `rw[672+:32]`(默认 62500) 后，自动向 rw 里写一组 cfg_mgmt 参数（di=0x0007/0xff00、dwaddr=1、byte_en 按使能位组合）并置 WR_EN，借 4.2 节的握手完成对命令/状态寄存器的周期维护。

`build.md` 关于 DSN 的官方建议（本讲实践的依据）：

[build.md:34-39](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md#L34-L39) — 明确建议在 `src/pcileech_pcie_cfg_a7.sv` 里改 `rw[127:64] <= 64'h...;  // cfg_dsn` 这一行来修改设备序列号。

#### 4.4.4 代码实践

1. **实践目标**：定位 DSN 赋值、理解它为何是「改设备特征最方便的手段」，并指出 cfg_mgmt 完成握手信号。
2. **操作步骤**：
   - 在 `pcileech_pcie_cfg_a7.sv` 找到 DSN 赋值（215 行）与 assign（265 行），在 `pcileech_pcie_a7.sv` 找到 `cfg_dsn` 对接硬核（177 行），在 `build.md` 找到官方说明（34–39 行）。
   - 思考：默认值 `0x0000000101000A35` 是**所有出厂 pcileech-fpga 固件共享**的固定值，`lspci -vv` 即可读到；而 VID/PID 改起来要在 Vivado GUI 里重生成 PCIe IP（见 build.md「Customizing…」一节，需 Generate）。对比两者的改造成本。
   - 指出 cfg_mgmt 完成握手信号：完成侧是 `cfg_mgmt_rd_wr_done`（核→模块，门控见 262–263 行，锁存见 379 行）；请求侧是 `cfg_mgmt_rd_en`/`cfg_mgmt_wr_en`。
3. **需要观察的现象**：DSN 只需改 1 行普通 HDL、重新综合即可生效；而 VID/PID 改动要走 IP GUI 重生成。两者在 `lspci` 输出里落在不同字段（DSN 在扩展能力的「Device Serial Number」，VID/PID 在配置头 00–03 字节）。
4. **预期结果**：把默认 DSN 改成自定义 64 位值（如 `64'hDEAD_BEEF_CAFE_BABE`，仅作示意）后，Linux 上 `lspci -d 10ee:0666 -vvv` 的 「Device Serial Number: xxxx-xxxxxxxx」会随之变化。**待本地验证**（需 FPGA 硬件 + 目标机，本环境无法运行）。同时能清晰说出：完成握手信号是 `cfg_mgmt_rd_wr_done`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `build.md` 把「改 DSN」和「改 VID/PID」分在两处、用两种方式？
**答**：VID/PID 存在硬核 IP 内部，必须用 Vivado PCIe 核 GUI 修改并重生成 IP（build.md「Customizing…」节）；而 DSN 是固件经 `cfg_dsn` 端口动态注入硬核的，源码里改 `rw[127:64]` 一行即可，无需碰 IP——成本天差地别，所以官方单独提示。

**练习 2**：静态 TLP 经哪个接口、送到哪个下游模块？
**答**：经 `tlps_static`（`IfAXIS128.source`）送到 `pcileech_pcie_tlp_a7`（在该模块是 `tlps_static.sink`，见 `pcileech_pcie_a7.sv` 第 79/101 行），与配置影子响应、BAR 响应等一起进入 TLP 发送多路复用（详见 u3-l3）。

**练习 3**：状态寄存器自动清零（rw[20]/[21]）是靠什么机制实现的？
**答**：靠周期性自构造 cfg_mgmt 写——计数器到周期后，模块在 rw 里填好 di/dwaddr/byte_en 并置 WR_EN，复用 4.2 节的 cfg_mgmt 握手完成一次对命令/状态寄存器的写入（361–376 行）。

## 5. 综合实践

**任务**：为一次「主机读 PCIe 配置空间命令寄存器」画一张端到端时序与寄存器映射表。

要求：

1. 起点：主机软件经 USB 发出一个读 `ro` 的命令包，目标字节偏移 +014（即 `cfg_command`，ro[175:160]）。给出命令包里 `address_byte` 的 16 位值（区分 ro/rw）。
2. 中段：标注该命令经哪些模块、哪些时钟域、哪些 FIFO（`clk_sys`→`clk_pcie`）到达 cfg 模块。
3. 终点：说明读取结果从哪个 `ro` 比特区间产出、经哪个上行 FIFO（`clk_pcie`→`clk_sys`）回送主机。
4. 进阶：若把目标改成「写命令寄存器的 bus master 位」，应改写 rw 的哪一位触发？它会不会经 cfg_mgmt 握手？完成信号是谁？（提示：参考 4.4 的状态寄存器自动清零逻辑。）

参考答案要点：
1. `address_byte = 16'h0014`（最高位 0=ro，低 15 位=0x0014）。
2. 主机→ft601→com→fifo→`IfPCIeFifoCfg`(`dfifo.tx_data/tx_valid`)→`fifo_64_64`(`clk_sys`→`clk_pcie`)→cfg 命令解析。
3. `ro[175:160]`(`cfg_command`)→`out_data`→`fifo_32_32_clk2`(`clk_pcie`→`clk_sys`)→`dfifo.rx_data/rx_valid`→fifo→主机。
4. 直接写 rw 无法直达命令寄存器（命令寄存器在硬核内部），必须经 cfg_mgmt 写：置 rw[17](WR_EN)，填 cfg_mgmt_di/dwaddr=1/byte_en，靠 `cfg_mgmt_rd_wr_done` 完成；状态寄存器自动清零功能正是周期性地这么做。

## 6. 本讲小结

- `pcileech_pcie_cfg_a7` 是 PCIe 配置面的**唯一代理人**：经 `IfPCIeSignals`(`ctx`) 收拢硬核上百个状态/控制信号，经 `IfPCIeFifoCfg`(`dfifo`) 与主机间接相通。
- 它用两个双时钟 FIFO（`fifo_64_64` 下行、`fifo_32_32_clk2` 上行）安全跨越 `clk_sys`↔`clk_pcie` 两个时钟域。
- cfg_mgmt 读写的握手是**请求 `cfg_mgmt_rd_en`/`wr_en`（模块→核）— 完成 `cfg_mgmt_rd_wr_done`（核→模块）**；请求电平靠 `& ~done` 自动撤销。
- `ro`(48B 只读状态镜像) 与 `rw`(88B 主机控制面板) 严格按字节偏移铺排，注释里的 +008/+00A/+010 即字节地址；LTSSM 在 +00A、链路通断 `pl_phy_lnk_up` 在 ro[98]。
- DSN(`cfg_dsn`=`rw[127:64]`) 是改变设备特征**最廉价**的途径——改一行 HDL 即可，无需像 VID/PID 那样重生成 Xilinx IP，这是 `build.md` 单独推荐它的原因。
- 静态 TLP(`tlps_static`) 让固件可主动注入最多 8 DWORD 的报文；状态寄存器自动清零则周期性自构造 cfg_mgmt 写维护命令/状态寄存器。

## 7. 下一步学习建议

- 下一篇 **u3-l3（TLP 处理总览与 128 位流）** 会打开 `pcileech_pcie_tlp_a7.sv`，讲清本讲提到的 `tlps_static`、配置影子响应、BAR 响应、rx FIFO 四路如何汇入 TLP 发送多路复用——与本讲的「静态 TLP 出口」直接对接。
- 若想立刻动手改设备身份，可结合 **u4-l4（设备身份定制）** 一次性把 VID/PID/DSN/Class Code 的修改途径对比清楚（本讲只覆盖了 DSN 一条）。
- 想深入「主机软件如何读写这些 rw/ro 字段」，可回头对照 PCILeech 主机侧驱动（独立仓库 LeechCore）里的控制寄存器映射，验证本讲字段偏移是否与之一致。
