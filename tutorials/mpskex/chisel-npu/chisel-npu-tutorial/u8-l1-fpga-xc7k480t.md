# FPGA 验证平台 xc7k480t

## 1. 本讲目标

chisel-npu 的 RTL 在仿真里跑通后，最终要落到真实芯片上才能算「可用」。本讲带你认识项目的官方 FPGA 验证平台——一块搭载 Xilinx Kintex-7 `xc7k480tffg1156-2` 的定制板。学完本讲，你应当能够：

1. 画出从主机 PCIe 到片上 MMALU 的完整数据通路，说清每一级 AXI 桥（时钟转换 / 位宽转换 / 交叉开关）的作用。
2. 说清平台上的时钟域划分：125 MHz 的 `axi_aclk`、200 MHz 的 `fabric_aclk`、133 MHz 的 MIG `ui_clk`，以及被 `set_false_path` 豁免的 250 MHz PCIe PHY 时钟。
3. 读懂平台的「时序收敛史」表格，并解释 WNS = −0.151 ns 这条违例路径（脉动阵列 `reg_h` → PE 乘加链）为何靠插入流水寄存器修复，代价是延迟从 \(3n-2\) 变为 \(3n-1\)。
4. 走通 `make build-fpga` 的 Vivado 批处理流程，区分 `build_npu.tcl`（量产）与 `build_npu_with_ila.tcl`（调试带 ILA 核）两条构建线。

本讲是 U8「FPGA 平台与驱动」单元的基石，后续 u8-l2（Linux XDMA 驱动）与 u8-l3（Python 用户态驱动）都建立在它描述的硬件拓扑之上。

## 2. 前置知识

本讲假定你已学过 **u1-l2（开发环境与构建运行方式）**，知道：

- `make build` 会调用 `sbt run`，在仓库根目录生成 Chisel→FIRRTL→SystemVerilog 的产物 `top.sv`。
- `build-fpga` 目标**不**走 Docker，而是依赖**宿主机**安装的 Vivado（路径默认 `~/Xilinx/2025.2/Vivado/bin/vivado`），是上板综合流程的入口。
- Makefile 目标名 `top.v` 与实际产物 `top.sv` 存在命名不一致的 gotcha。

同时建议你回顾 **u4-l5（MMALU 顶层集成与流式归约）**，它已经建立了两个本讲要展开的关键事实：

- MMALU 内部为修 200 MHz 关键路径插入了流水寄存器 `pipe_a / pipe_b / pipe_ctrl`。
- 这使脉动阵列延迟从理论 \(3n-2\) 变为 \(3n-1\)。

本讲要做的，是把这些 RTL 层的结论放到**真实 FPGA 平台**的语境里：为什么是 200 MHz？为什么必须插流水线？芯片放得下多大的 K？下面先补几个硬件术语。

- **FPGA（现场可编程门阵列）**：可重构芯片，NPU 的 RTL 综合（synthesis）后「烧」进它的查找表（LUT）和触发器（FF）里，就成了可运行的真实硬件。
- **时序收敛（timing closure）**：让所有组合逻辑路径都能在**一个时钟周期内**稳定建立数据。衡量指标是 **WNS（Worst Negative Slack，最差负裕量）**：WNS ≥ 0 表示满足，WNS < 0 表示违例、芯片会跑飞。**TNS（Total Negative Slack）** 是所有违例端点的负裕量之和。
- **多周期路径（MCP, Multi-Cycle Path）**：某些路径被约束为允许 2 个周期才建立数据，给慢路径喘息空间。MMALU 的乘累加链就用了 2 周期 MCP。
- **AXI（Advanced eXtensible Interface）**：ARM 制定的片上总线协议。本平台大量使用它的三个变体：AXI4（高速数据，带突发）、AXI4-Lite（轻量控制寄存器）、以及 AXI 时钟/位宽转换 IP，实现跨时钟域和不同总线宽度器件的对接。
- **CDC（Clock Domain Crossing，跨时钟域）**：信号从一个时钟域进入另一个时需要同步器（通常异步 FIFO），否则会采样到亚稳态。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|:-----|:-----|
| [docs/implementations/FPGA_XC7K480T.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/FPGA_XC7K480T.md) | 平台的「圣经」：硬件拓扑图、时钟域表、时序收敛史、构建流程、V0→V10 调试阶梯、ILA 方法论 |
| [Makefile](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/Makefile) | `build-fpga` / `build-fpga-debug` / `build-fpga-clean` 三个目标的薄封装 |
| [src/main/scala/alu/mma/mma.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala) | MMALU 顶层，含 WNS = −0.151 ns 违例的原始时序注释与插入的流水寄存器定义 |

需要强调：FPGA 的「源码」不只是 Scala/Verilog，还包括 Vivado 工程下的 TCL 脚本与 XDC 约束。它们位于 `ip/vivado/xc7k480t/scripts/`，已纳入 git 管理：

- `build_npu.tcl` — 量产构建（无 ILA）
- `build_npu_with_ila.tcl` — 调试构建（带 `u_npu_ila` 核）
- `_apply_npu_topology.tcl` — 把 V1..V10 的 BD（Block Design）改动压扁成一份拓扑库
- `bootstrap_project.tcl` / `migrate_lib.tcl` — 首次工程引导与共享辅助函数

---

## 4. 核心概念与源码讲解

### 4.1 平台拓扑：从 PCIe 到 MMALU 的数据通路

#### 4.1.1 概念说明

仿真里的 MMALU 是一个被测试代码 `poke`/``peek` 的纯硬件对象；真实板子上它必须挂在一条能被主机访问的总线上。**平台拓扑**回答的就是：主机发出来的数据，经过哪些桥梁，最终怎么喂到 MMALU 的 `io.in_a / in_b`，结果又怎么读回来。

chisel-npu 选的是 **PCIe + XDMA + AXI** 这条成熟路径：

- 主机通过 **PCIe**（本槽位实际只能跑到 Gen1 ×4，设计上限 Gen2 ×8）连到板上的 **XDMA 4.2** IP——Xilinx 提供的 DMA 引擎，把 PCIe 事务翻译成片上 **128 位 AXI4** 总线。
- 片上再用一串 AXI 桥（**时钟转换 clkconv / 位宽转换 dwidth / 交叉开关 xbar**）把数据搬到 **MIG（Memory Interface Generator）** 控制的两片 **DDR3**，以及一个直连 MMALU 的控制寄存器 `ctrl_lite`。
- MMALU 旁边还有一个自带的 **`npu_dma_master`**，它作为 AXI 主设备从 DDR3 把 A/B/ACCUM 搬进 MMALU、再把 OUT 写回 DDR3。

一句话：主机把矩阵写到 DDR3，再向 `ctrl_lite` 写一个 `start` 位，`npu_dma_master` 就自主完成搬运-计算-回写。这个「主机只管写数据 + 踢一脚」的模型，正是后续 u8-l2 / u8-l3 驱动要对接的接口。

#### 4.1.2 核心流程

以一次 MMALU 计算为例，数据在平台上的旅行分两条路：

**控制路径（轻量）**

```
主机 ──PCIe──► XDMA M_AXI_BYPASS ──► axi_clkconv_byp(125→200MHz)
                                   ──► byp_dw(128→32b) ──► byp_pc(AXI4→AXI4-Lite)
                                   ──► npu_subsys/s_axil_* ──► ctrl_lite(BAR2+0x0)
主机向 32 位寄存器写 start；读回 done/busy。
```

**数据路径（重量）**

```
主机 ──PCIe──► XDMA M_AXI(128b@125MHz)
            ──► axi_cc_xdma_in(125→200MHz CDC)
            ──► axi_clkconv_xdma(200→133MHz CDC)
            ──► axi_dwidth_xdma(128→512b)
            ──► axi_xbar.S00 ──► MIG C0 ──► DDR3  (统一 4GB 地址空间)

npu_dma_master 从 DDR3 读 A/B/ACCUM ──► MMALU 计算 ──► 写 OUT 回 DDR3
                  (m_axi 128b@200MHz) ──► axi_clkconv_npu(200→133)
                                        ──► axi_dwidth_npu(128→512)
                                        ──► axi_xbar.S01 ──► MIG C0
```

V10 的关键升级是引入了 **2 主 2 从（2S:2M）的 `axi_xbar` 交叉开关**：主机（XDMA）和 NPU 自己的 DMA master 都能通过这同一个交叉开关访问两片 DDR3（C0/C1），地址空间统一成 4 GB。V9 之前两者是分区隔离的，主机够不到 NPU 读的那片内存。

#### 4.1.3 源码精读

平台拓扑的权威描述是 FPGA 文档里的 ASCII 框图，覆盖了主机→XDMA→各级 AXI 桥→MIG→DDR3，以及 `npu_subsys` 内部 `ctrl_lite ↔ npu_dma_master ↔ MMALU` 的三件套：

[docs/implementations/FPGA_XC7K480T.md:36-91](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/FPGA_XC7K480T.md#L36-L91) —— 这是平台的硬件架构总图，标注了每一级 AXI IP 的位宽与时钟域。

图中最关键的子结构是 V10 引入的单个 `npu_subsys` BD 单元，内部把三件 Verilog 模块封装成一个 cell：

[docs/implementations/FPGA_XC7K480T.md:74-81](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/FPGA_XC7K480T.md#L74-L81) —— `npu_subsys` 内部：`ctrl_lite`（BAR2+0x0）↔ `npu_dma_master`（AXI4, K=32）↔ `MMALU`（K=32, N=8, 32 位累加）。

统一的 4 GB 地址空间定义如下，主机和 NPU 看到的是同一张地图：

[docs/implementations/FPGA_XC7K480T.md:103-123](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/FPGA_XC7K480T.md#L103-L123) —— 地址映射：`0x0..0x7FFF_FFFF`→MIG C0（2GB），`0x8000_0000..`→MIG C1（2GB）；NPU 默认在 C0 的 `0x4000_0000` 区域摆 A/B/ACCUM/OUT 四块。

注意 NPU 的四块缓冲地址紧挨在 `0x0_4000_0000`：矩阵 A/B 各 32 字节（int8 × K=32），ACCUM/OUT 各 128 字节（int32 × K=32）。这个布局直接对应了 u1-l4 建立的 K 与 N(bits) 参数。

#### 4.1.4 代码实践

**实践目标**：在不碰真实板子的情况下，凭文档推导「一次 MMALU 计算」需要主机做哪几步。

**操作步骤**：

1. 打开 FPGA 文档第 36–91 行的拓扑图与第 103–123 行的地址表。
2. 假设主机要让 MMALU 算 `OUT[i] = A[i]·B[K-1] + ACCUM[i]`（V10 当前实际支持的单射语义），列出主机需要发起的几次 DMA/寄存器访问，标注每次落在哪个地址。
3. 在每一步旁边注明它走的是「控制路径」还是「数据路径」。

**预期结果**：你会得到类似下面这张表（**示例答案**，请用自己的话重写）：

| 步骤 | 方向 | 地址 | 路径 | 内容 |
|:-----|:-----|:-----|:-----|:-----|
| 1 | H2C（主机→DDR3） | `0x4000_0000` | 数据 | 写矩阵 A（32B） |
| 2 | H2C | `0x4000_0100` | 数据 | 写矩阵 B（32B） |
| 3 | H2C | `0x4000_0200` | 数据 | 写 ACCUM（128B） |
| 4 | 寄存器写 | BAR2+0x0 | 控制 | 写 `start=1` 踢一脚 |
| 5 | 寄存器轮询 | BAR2+0x0 | 控制 | 等 `done=1` |
| 6 | C2H（DDR3→主机） | `0x4000_0400` | 数据 | 读 OUT（128B） |

**观察现象**：注意控制路径和数据路径在 `axi_xbar` 之前是**物理分离**的两条线（BYPASS vs M_AXI），只在 DDR3 处汇合——这解释了为何控制寄存器访问极快（不经过 DDR），而数据搬运要走完整的位宽/时钟转换链。

> 待本地验证：上板时这 6 步的真实时延需用 `test_mmalu_compute.py` 抓取；文档记录 FSM kick→done 约 315 ns（仅硬件，不含 PCIe 往返）。

#### 4.1.5 小练习与答案

**练习 1**：V10 用单个 `axi_xbar`（2S:2M）统一了 4 GB 地址空间。如果**不**引入这个交叉开关、回到 V9 的「XDMA 只能访问 C0、NPU 只能访问 C1」的分区方案，主机驱动会遇到什么麻烦？

**参考答案**：主机把 A/B/ACCUM 写进了 C0，但 NPU 的 `npu_dma_master` 只能从 C1 读——主机写的数据 NPU 根本看不到。驱动不得不先做一次 C0→C1 的片上搬运，或者让主机直接通过某条共享通路写 C1，复杂度和时延都上升。`axi_xbar` 让两者看到同一张地图，从根上消除了这个不对称。

**练习 2**：拓扑图里 `byp_pc`（AXI4 → AXI4-Lite）这一级为什么必要？

**参考答案**：`ctrl_lite` 只是一个 32 位控制寄存器（`start/done/busy`），用完整 AXI4 的突发协议去访问它既浪费资源又不必要；转成 AXI4-Lite 后每次只读写单个 32 位字，接口更简单、面积更小。

---

### 4.2 时钟域划分：125 / 200 / 133 MHz 三级时钟

#### 4.2.1 概念说明

真实 SoC 几乎从不是单时钟的。PCIe PHY 有自己的参考时钟，DDR3 控制器（MIG）要求一个固定的 UI 时钟，而 MMALU 的组合逻辑深度决定了它最多能跑多快。把不同速度的部件硬塞进同一个时钟，要么慢的跟不上、要么快的被拖累。所以平台把时钟切成多个**域（domain）**，域之间用 CDC（异步 FIFO 或 AXI 时钟转换 IP）隔离。

本平台有五个时钟域，其中四个真正参与数据通路，一个被显式豁免。理解这张表，是理解后续「为何要插 200 MHz fabric MMCM」「为何要 set_multicycle_path」的前提。

#### 4.2.2 核心流程

平台的时钟生成链是这样的：

- **源头**是 XDMA PCIe GTX 收发器送出的 **`axi_aclk`（userclk2，125 MHz）**，XDMA 内部和 BYPASS 路径都跑在它上面。
- 一个 **MMCM**（混合模式时钟管理器，IP 名 `clk_wiz_fabric`）把 125 MHz 按 \( \times 8/5 \) 倍频得到 **`fabric_aclk`（200 MHz）**：\( 125 \times \frac{8}{5} = 200 \)。MMALU、DMA master、ctrl_lite、各 AXI 转换器的从端都跑在这个域——这是**计算核心域**。
- 两片 DDR3 各自的 MIG PLL 产生 **`c0_ui_clk` / `c1_ui_clk`（均 133 MHz）**，交叉开关 `axi_xbar` 及其 S00/S01/M00 端口跑在 C0 域，M01 跨到 C1 域时用内部异步 FIFO 做 CDC。
- PCIe PHY 内部还有 **`userclk1`（250 MHz）**，**仅** PHY 自用，对fabric 没有功能耦合，故用 `set_false_path` 显式豁免——这绕过了它带来的所有时序违例。

为什么要专门造一个 200 MHz fabric 域、而不直接用 125 MHz 或 250 MHz？因为 125 MHz 太慢（浪费算力），而 250 MHz（PCIe `userclk1`）MMALU 根本收不住时序。200 MHz 是工程上「算力够用 + 时序能闭合」的甜点——这一点会在 4.3 节展开。

#### 4.2.3 源码精读

时钟域表是平台的「速查卡」，列出每个域的频率、来源与消费者：

[docs/implementations/FPGA_XC7K480T.md:93-101](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/FPGA_XC7K480T.md#L93-L101) —— 五个时钟域的频率/来源/消费者对照表。

表中能直接读出 CDC 的实现方式：

- `fabric_aclk` 一行注明来源是 `clk_wiz_fabric` MMCM（×8/5 from 125 MHz），消费者含 MMALU、DMA master、ctrl_lite、各 NPU AXI 转换器的从端。
- `c1_ui_clk` 一行注明 `axi_xbar.M01` 通过「内部异步 FIFO」跨到 C1——这就是 M01 那一列的 CDC 机制。
- `userclk1`（250 MHz）一行注明「PCIe PHY internal only — waived via `set_false_path`」，明确它不参与 fabric 闭合。

关于 200 MHz 域的诞生动机，文档单独列了一节：

[docs/implementations/FPGA_XC7K480T.md:160-167](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/FPGA_XC7K480T.md#L160-L167) —— 「200 MHz fabric clock for formal timing closure」：插入 MMCM 派生 200 MHz，把 5 ns 周期（相对 250 MHz 的 4 ns）给所有 fabric 路径多留 1 ns 余量，并把 MMALU 的 2 周期 MCP 预算撑到 10 ns。

#### 4.2.4 代码实践

**实践目标**：把「时钟域 → 频率 → 消费者」三列内化为一张能默写的表。

**操作步骤**：

1. 读 FPGA 文档第 93–101 行的时钟域表。
2. 用手算验证 MMCM 倍频：\( 125 \text{ MHz} \times \frac{8}{5} = ? \) 应得 200 MHz。
3. 回答：为什么 `axi_xbar` 不跑在 200 MHz 的 fabric 域，而要跑在 133 MHz 的 `c0_ui_clk`？

**预期结果**：算式得 200 MHz。第三问的要点是——`axi_xbar` 紧挨着 MIG（DDR3 控制器），而 MIG 的 UI 接口固定跑在自己的 133 MHz PLL 上；把交叉开关也放在 133 MHz 域，可以让 xbar↔MIG 之间**零 CDC**，把跨时钟域的代价推到 xbar 的上游（与 fabric 域之间），由现成的 AXI 时钟转换 IP 处理。

**观察现象**：注意 `c0_ui_clk` 一行的消费者里包含 `axi_dwidth_xdma` 和 `axi_dwidth_npu`——位宽转换器被刻意放在 133 MHz 域（而非 fabric 200 MHz），这与下一节的 Tier-2.5 拓扑重排直接相关。

#### 4.2.5 小练习与答案

**练习 1**：`userclk1` 是 250 MHz，比 fabric 的 200 MHz 还快，为什么不干脆让 MMALU 跑在 250 MHz？

**参考答案**：两个原因。一是 `userclk1` 属于 PCIe PHY 内部域，用它驱动 fabric 会在 PHY 状态切换时引入抖动与耦合；二是 MMALU 的 `reg_h → PE 乘加链` 在 250 MHz（4 ns 周期）下 WNS 为负、收不住时序（这正是 WNS = −0.151 ns 那条路径的背景）。所以用 MMCM 派生一个干净的、时序可闭合的 200 MHz 域，并用 `set_false_path` 把 250 MHz 域从 fabric 闭合里豁免掉。

**练习 2**：跨时钟域有哪两种实现，分别用在平台的什么位置？

**参考答案**：一是 AXI 时钟转换 IP（如 `axi_cc_xdma_in`、`axi_clkconv_xdma`），用在 fabric↔XDMA、fabric↔MIG 的主数据通路上；二是异步 FIFO，用在 `axi_xbar.M01`（C0→C1 之间）——文档明确写「cross-clock to C1 via internal async FIFO」。

---

### 4.3 时序收敛史：从 K=32 缩放失败到 200 MHz 闭合

#### 4.3.1 概念说明

时序收敛是 FPGA/ASIC 设计里最熬人的环节。一个设计在仿真里「功能正确」不等于在真实时钟下「时序正确」——只要有一条组合逻辑路径在时钟周期内来不及建立数据，芯片就会偶发抓错值。

衡量时序的标尺：

- **WNS（最差负裕量）**：所有端点里最差的那条路径，离截止时间还差（或多出）多少纳秒。WNS ≥ 0 才算收敛。
- **TNS（总负裕量）**：所有违例端点的负裕量之和，反映违例的「总量」。
- **Failing EPs（违例端点数）**：有多少个终点没收住。

WNS 决定「能不能跑」，TNS/EPs 决定「烂得多严重」。本平台的时序收敛史，本质上是一部「把 K 从 16 放大到 32 时，违例端点从 0 涨到 22608，再一步步压回 0」的斗争史。

#### 4.3.2 核心流程

收敛史可以分成平台层（FPGA 文档记录）与 MMALU 内核层（mma.scala 注释记录）两条线，它们共同决定了 200 MHz 这个工作点。整个故事的脉络：

1. **K=16 基线**：WNS = +0.005 ns，0 违例——K=16 在平台上天然闭合。
2. **放大到 K=32**：脉动阵列从 16×16 变 32×32，组合逻辑与布线急剧膨胀，首次出现 WNS = −0.100 ns、218 个违例端点。
3. **施加全局 MCP 后更糟**：把所有 MMALU 内部路径标成 2 周期 MCP，反而触发了 22445 个违例端点、WNS = −0.564 ns——因为约束方式不对。
4. **Tier-2.5 拓扑重排（clkconv-first）**：把位宽转换器从 250 MHz 域挪到 133 MHz 域，消掉一整类慢性关键路径，但 WNS 反而到 −0.782 ns（因为暴露出更深的瓶颈）。
5. **DataFeeder 逐 lane 重构**：把单条 `Pipe(Vec(n,...))` 拆成 n 条独立 `Pipe(SInt(...))`，把 valid 信号的扇出从 1025 砸到 ≤2，违例端点从 22608 → 1299（降 96.3%），WNS 从 −0.782 → −0.265 ns。
6. **引入 200 MHz fabric**：用 MMCM 把 fabric 从 250 MHz 降到 200 MHz（周期 4→5 ns），违例端点 → 0，WNS = +0.020 ns，**正式闭合**。
7. **V10 加交叉开关 + 修 `S_WR_W`**：WNS 在 +0.027 ~ −0.133 ns 间随布局抖动，残余违例（若有）始终在 MMALU PE 内部的进位链上，不影响数据/控制平面。

与上面平台层并行的，是 **MMALU 内核那条 WNS = −0.151 ns 的关键路径**：从 `SystolicArray2D` 的水平移位寄存器 `reg_h` 出发，穿过 **13 级逻辑**（8×CARRY4 + 5×LUT）打进 PE 的乘累加链。它在 200 MHz 的 5 ns 周期（2 周期 MCP = 10 ns 预算）下仍负 0.151 ns。修复办法是在 SA→PE 与 CU→Collector 之间插入流水寄存器，把这条长组合路径切成两段各 6–7 级——代价是**多 1 拍延迟**，即脉动阵列延迟从 \( 3n-2 \) 变为 \( 3n-1 \)。

#### 4.3.3 源码精读

平台层的完整收敛史在 FPGA 文档的时序表里：

[docs/implementations/FPGA_XC7K480T.md:214-229](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/FPGA_XC7K480T.md#L214-L229) —— 时序收敛史表，7 行记录了从 K=16 闭合到 V10 的全部 WNS/TNS/违例端点/关键改动。

其中 DataFeeder 重构是「以小博大」的典范，靠拆分 Chisel `Pipe` 把 fanout 从 1025 降到 2：

[docs/implementations/FPGA_XC7K480T.md:139-158](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/FPGA_XC7K480T.md#L139-L158) —— DataFeeder 逐 lane `buffer_accum` 重构：单条共享 valid 寄存器（扇出 1025、7.9 ns 网络延迟）换成 32 条独立 `Pipe(SInt)`（扇出 ≤2），违例端点降 96.3%、WNS 改善 66%。

WNS = −0.151 ns 这条路径与流水寄存器的**精确出处**在 MMALU 顶部的注释里（这是 u4-l5 已提及、本讲放在平台语境下再确认的事实）：

[src/main/scala/alu/mma/mma.scala:13-22](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L13-L22) —— 时序注释：200 MHz 下 `reg_h` → 13 级逻辑（8×CARRY4 + 5×LUT）→ PE 乘加链导致 WNS = −0.151 ns；修复办法是插流水寄存器，延迟从 \(3n-2\) 变 \(3n-1\)。

对应的流水寄存器定义在同一文件，把 SA→PE 和 CU→Collector 的长路径切成两段：

[src/main/scala/alu/mma/mma.scala:47-58](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L47-L58) —— 流水寄存器定义：`pipe_a`/`pipe_b`/`pipe_ctrl`（n×n 个）切断 SA→PE 路径，`pipe_dat_clct`/`pipe_use_accum`/`pipe_clct` 切断 CU→Collector 路径，统一 +1 拍。

注意 `pipe_a`/`pipe_b`/`pipe_ctrl` 用 `RegInit(VecInit(Seq.fill(n*n)(...)))` 实现，规模是 \( n^2 \) 个寄存器（K=32 时即 1024 套）：

[src/main/scala/alu/mma/mma.scala:52-54](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L52-L54) —— 三条核心数据/控制流水寄存器的实例化，规模随 \(n^2\) 缩放。

把时序预算算清楚：200 MHz 周期 \( T = 1/(200\,\text{MHz}) = 5\,\text{ns} \)。MMALU 的 2 周期 MCP 给的建立预算是 \( 2T = 10\,\text{ns} \)，留给 8 位 MAC 的 CARRY4×8 链（约 4.3 ns 逻辑）+ 布线绰绰有余——这是约束文件里 `set_multicycle_path 2` 的依据：

[docs/implementations/FPGA_XC7K480T.md:430-443](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/FPGA_XC7K480T.md#L430-L443) —— MMALU MCP 约束：2 周期 setup + 1 周期 hold，作用域是 `mmalu_inst/*` 内部，200 MHz 下 10 ns 预算够 CARRY4×8 链使用。

#### 4.3.4 代码实践

**实践目标**：把 WNS = −0.151 ns 这条违例路径与它的修复「坐实」到代码行号，并能复述「+1 拍换时序」的算术。

**操作步骤**：

1. 打开 `mma.scala` 第 13–22 行的时序注释，抄下违例路径的起点、逻辑级数、WNS 值。
2. 打开第 52–58 行，列出插入的 6 组流水寄存器名，分组标注它们各自切断的是 SA→PE 还是 CU→Collector。
3. 打开 FPGA 文档第 214–229 行的时序表，找到「200 MHz fabric (V9)」这一行，确认其 WNS = +0.020 ns、0 违例端点——这是平台级闭合的里程碑。
4. 做一遍预算算术：200 MHz 的 5 ns 周期下，2 周期 MCP = 10 ns；插流水寄存器后每段 6–7 级逻辑大约需要多少 ns 才安全？

**预期结果**：

- 违例路径：起点 `reg_h`（SystolicArray2D 水平移位寄存器输出）→ 13 级逻辑（8×CARRY4 + 5×LUT）→ PE 乘累加链；WNS = −0.151 ns。
- 6 组流水寄存器分两类：切断 **SA→PE** 的 `pipe_a` / `pipe_b` / `pipe_ctrl`；切断 **CU→Collector** 的 `pipe_accum`（含 `pipe_dat_clct` / `pipe_use_accum` / `pipe_clct`）。
- 时序表 V9 行：WNS = +0.020 ns，0 违例。
- 算术：每段 6–7 级逻辑，若每级约 0.3–0.4 ns，则 6–7 级约 2 ns 逻辑 + 布线，远小于 5 ns 单周期预算，故能闭合。

**观察现象**：注意时序表里 V10 那一行 WNS 写的是「+0.027 to −0.133」，并在附注里强调残余违例「always inside an MMALU PE carry chain」——说明加交叉开关本身没引入新关键路径，平台数据平面是干净的。

> 待本地验证：WNS 随每次综合的布局布线结果有 ±0.1 ns 抖动，文档已声明这是 placement-dependent 的正常现象，需以实际 Vivado 时序报告为准。

#### 4.3.5 小练习与答案

**练习 1**：DataFeeder 重构把违例端点从 22608 降到 1299，降幅 96.3%。请解释「单条共享 valid 寄存器扇出 1025」为什么会产生 7.9 ns 的网络延迟。

**参考答案**：K=32 时单条 `_v_reg` 要驱动 \( n \times (2n-1) = 32 \times 63 = 2016 \) 个下游 CE 引脚（文档取其关键子集说 1025），这些引脚物理上散布在整个 die 的约 190 列 CLB 上。一个触发器要驱动上千个远端负载，走线网络呈巨大树状/星状，电容和布线长度叠加，于是网络延迟高达 7.9 ns，几乎吃满 8 ns 的 2 周期 MCP 预算。拆成 32 条独立 `Pipe` 后，每条只驱动 ≤2 个本地负载，网络延迟骤降。

**练习 2**：插流水寄存器让 MMALU 延迟从 \(3n-2\) 变 \(3n-1\)。对 K=32，这多出的一拍相对整个计算的占比是多少？这个代价「值得」吗？

**参考答案**：K=32 时 \(3n-2 = 94\) 拍，\(3n-1 = 95\) 拍，多 1 拍占比约 \(1/95 \approx 1.06\%\)。相比它换来的「能在 200 MHz 稳定闭合、违例端点清零、芯片真能跑」，1% 的吞吐损失微不足道。这就是 4.3 与综合实践要论证的「+1 拍换时序」的合理性。

**练习 3**：时序表里「Tier-2.5 reorder」一行的 WNS 是 −0.782 ns，比上一行 −0.564 ns **更差**，为何文档仍把它列为一个改进？

**参考答案**：因为这一步消除的是**一整类**慢性关键路径（`CMD_QUEUE → mi_register_slice`），只是它同时暴露了更深的瓶颈（DataFeeder 扇出问题），让 WNS 数字暂时变差。它是后续 DataFeeder 重构的前提——没有这步拓扑重排，DataFeeder 的修复也压不到 1299。看时序收敛不能只盯 WNS 的绝对值，还要看违例端点数和违例类型的演化。

---

### 4.4 build-fpga 目标：Vivado 批处理流程

#### 4.4.1 概念说明

u1-l2 已指出 `build-fpga` 不走 Docker、依赖宿主机 Vivado。本节把这层薄封装拆开，看清它到底执行了什么。

Vivado 有两种用法：图形化 IDE 和**批处理（batch mode）**。CI/自动化场景几乎只用后者——`vivado -mode batch -source <脚本.tcl>` 吃一份 TCL 脚本，跑完综合（synth_1）、实现（impl_1）、生成比特流（write_bitstream），全程无 GUI。chisel-npu 把这套流程封装进 Makefile 的 `build-fpga` 目标，背后调用的是 TCL 脚本 `build_npu.tcl`。

TCL（Tool Command Language）是 EDA 工具的通用脚本语言，Vivado 的所有工程操作都能用 TCL 表达，因此整个 FPGA 构建是可版本化、可复现的。

#### 4.4.2 核心流程

完整的上板构建链是「Chisel→Verilog→比特流→烧录」四段：

1. **生成 `top.sv`**：`make build`（即 `sbt run`）产出 K=32 的 MMALU 网表，落在仓库根目录。这是构建 FPGA 的**前置依赖**——`build-fpga` 目标在 Makefile 里就声明了 `: top.v` 依赖（注意命名 gotcha：目标名是 `top.v`，产物是 `top.sv`）。
2. **Vivado 批处理构建**：`vivado -mode batch -source build_npu.tcl`。脚本首次运行会自动引导 `proj/` 工程（冷启动约 25 分钟），再跑 synth + impl + write_bitstream（约 50 分钟），产出 `top_npu.bit`（约 18 MB）。重跑复用已有工程约 30 分钟。
3. **（可选）调试构建**：`build-fpga-debug` 调用 `build_npu_with_ila.tcl`，额外塞入 `u_npu_ila` 调试核，产出 `top_npu_with_ila.bit` + `.ltx` 探针文件——这是硬件调试用的。
4. **烧录 + 冒烟测试**：用 `tool/hw/bringup_flash.py` 烧 BPI flash + 跑 9 项冒烟测试。

注意两个构建脚本的拓扑**完全相同**，唯一区别是有没有 ILA 核。日常量产用 `build_npu.tcl`，抓时序/握手 bug 才用带 ILA 的版本。

#### 4.4.3 源码精读

Makefile 顶部先定义了三个可覆盖变量，把 Vivado 路径、芯片型号、日志目录参数化：

[Makefile:39-41](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/Makefile#L39-L41) —— `VIVADO`（默认 `~/Xilinx/2025.2/Vivado/bin/vivado`）、`CHIP`（默认 `xc7k480t`）、`VIVADO_LOGDIR`（默认 `build`）三个变量定义。

量产构建目标，依赖 `top.v`（实际产物 `top.sv`），用 `mkdir -p` 建日志目录后以 batch 模式跑 `build_npu.tcl`：

[Makefile:43-48](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/Makefile#L43-L48) —— `build-fpga` 目标：`mkdir -p build` 后调用 Vivado 批处理执行 `ip/vivado/xc7k480t/scripts/build_npu.tcl`，日志写到 `build/build_npu_xc7k480t.log`。

调试构建目标，结构与量产版一致，只换 TCL 脚本和日志名：

[Makefile:50-55](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/Makefile#L50-L55) —— `build-fpga-debug` 目标：调用 `build_npu_with_ila.tcl`，产出带 ILA 核的调试比特流。

清理目标，删掉 Vivado 工程目录以强制下次冷启动：

[Makefile:57-58](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/Makefile#L57-L58) —— `build-fpga-clean`：`rm -rf ip/vivado/xc7k480t/proj`。

两个 TCL 脚本的分工，文档的构建说明里讲得很清楚（含冷/热启动耗时与产物大小）：

[docs/implementations/FPGA_XC7K480T.md:248-284](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/FPGA_XC7K480T.md#L248-L284) —— Step 2 构建说明：两个 TCL 脚本产同拓扑，`build_npu.tcl` 出 `top_npu.bit`（量产），`build_npu_with_ila.tcl` 出 `top_npu_with_ila.bit` + `.ltx`（调试）；冷启动 ~25 min 引导工程 + ~50 min 综合/实现/比特流。

#### 4.4.4 代码实践

**实践目标**：在不实际拥有 Vivado 许可的前提下，能凭 Makefile + 文档复述 `make build-fpga` 会展开成哪条 shell 命令、产出哪些文件。

**操作步骤**：

1. 读 Makefile 第 39–48 行，把 `build-fpga` 目标按变量代入，写出它实际执行的完整命令（`VIVADO`、`CHIP`、`VIVADO_LOGDIR` 取默认值）。
2. 读 FPGA 文档第 248–284 行，列出本次构建会产出的所有文件路径与大小。
3. 回答：如果只想验证「RTL 能不能综合过」而不关心比特流，有没有更快的路径？

**预期结果**（**示例命令**）：

```bash
mkdir -p build
~/Xilinx/2025.2/Vivado/bin/vivado -mode batch \
    -source ip/vivado/xc7k480t/scripts/build_npu.tcl \
    -log build/build_npu_xc7k480t.log \
    -journal build/build_npu_xc7k480t.jou
```

产物（默认量产脚本）：`ip/vivado/xc7k480t/top_npu.bit`（约 18 MB）。若用 `build-fpga-debug`，额外得到 `top_npu_with_ila.bit` + `top_npu_with_ila.ltx`（约 80 KB 探针文件）。

第三问的答案在文档 V8 调试阶梯里——历史上曾用空壳 `mmalu_stub.v` 做过「仅综合」的可行性测试（V8 NPU stub），跳过 impl/write_bitstream 以快速验证 RTL 可综合性，但该 stub 现已移除。

**观察现象**：注意 `build-fpga` 依赖 `top.v` 而非 `top.sv`，结合 u1-l2 的 gotcha，这意味着**每次** `make build-fpga` 都会先重新跑一遍 `sbt run` 重新生成 `top.sv`（因为 `top.v` 这个目标名对应的文件永远不存在，make 总认为它需要重建）。

> 待本地验证：实际冷/热启动耗时、比特流大小需在有 Vivado 的机器上以 `make build-fpga` 实测；文档给出的 ~25 min 引导 + ~50 min 综合是参考值。

#### 4.4.5 小练习与答案

**练习 1**：`make build-fpga` 和 `make build-fpga-debug` 产出的比特流拓扑是否相同？该用哪个做日常上板？

**参考答案**：拓扑完全相同，唯一区别是 debug 版多了一个 `u_npu_ila` 调试核（接到所有 `(* mark_debug *)` 信号）并多产一份 `.ltx` 探针文件。日常上板用 `build-fpga`（量产版），它没有 ILA 的综合/实现开销，比特流略小；只有在抓 `npu_dma_master` FSM 或 AXI 握手 bug 时才切到 debug 版。

**练习 2**：为什么 `build-fpga` 走宿主机 Vivado 而不像 `build`/`test` 那样塞进 Docker？

**参考答案**：Vivado 是体积庞大、需要许可证、且强绑定宿主机硬件（JTAG/HW Manager 要访问本地 FPGA 板）的商业 EDA 工具，不适合也不必要打进通用 Chisel 开发镜像。而 `build`/`test` 只需要 firtool/verilator 这些开源工具链，自然封装在 Docker 里保证可复现。两者关注点不同，故 Makefile 有意把 FPGA 流程留在宿主机侧。

---

## 5. 综合实践

**任务**：把本讲贯穿起来——整理出 WNS = −0.151 ns 违例路径的完整来龙去脉，并论证「加 1 拍延迟换时序收敛」在 NPU 设计里是合理取舍。

**操作步骤**：

1. **定位违例路径**。打开 [mma.scala:13-22](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L13-L22)，抄出：违例起点（SystolicArray2D 的 `reg_h`）、逻辑级数（13 级 = 8×CARRY4 + 5×LUT）、终点（PE 乘累加链）、WNS 值（−0.151 ns）、工作频率（200 MHz）。

2. **定位修复**。打开 [mma.scala:52-58](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L52-L58)，列出插入的流水寄存器，并标注它们把长路径切成了「两段各 6–7 级」。

3. **对照平台层闭合史**。打开 [FPGA_XC7K480T.md:214-229](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/FPGA_XC7K480T.md#L214-L229) 的时序表，把 MMALU 内核的这条修复放进整条收敛链里理解：它和 DataFeeder 重构、200 MHz fabric 是**互补**的三层手段——DataFeeder 修扇出、200 MHz 修周期预算、流水寄存器修组合深度。

4. **量化代价**。K=32 时计算：原本 \(3n-2 = 94\) 拍，插流水线后 \(3n-1 = 95\) 拍，多 1 拍占比 \(\approx 1.06\%\)。再算收益：200 MHz 单周期 5 ns，2 周期 MCP 预算 10 ns；13 级逻辑若不切，单段关键路径违例 0.151 ns；切两段后每段 6–7 级，逻辑延迟降到约 2 ns，安全落入 5 ns 预算。

5. **写结论**。用 3–5 句话回答：「为什么在 NPU 设计中，加 1 拍延迟换时序收敛是合理取舍？」

**预期结论（示例）**：

- **代价极小**：1 拍 / 95 拍 ≈ 1.06% 吞吐损失，且脉动阵列本就是流水线结构，多一级寄存器不改变其「逐拍喂入、逐拍流出」的编程模型。
- **收益决定性**：它把 WNS 从 −0.151 ns 拉回正裕量，是 200 MHz 能否闭合的关键之一；不修则芯片在真实时钟下会偶发抓错值，功能正确性都无法保证。
- **符合 NPU 取舍哲学**：NPU 的算力来自「大规模并行 + 高吞吐」，对单条路径的**延迟**（拍数）不敏感，对**频率**（能否跑高）极敏感。用少量寄存器换更高频率，换来的 \( \text{频率} \times \text{并行度} \) 增益远超拍数损失——这与「K=64 放不下、退而求 K=32」是同一种工程权衡。
- **可复现**：修复完全用 Chisel `RegInit`/`RegNext` 表达（见 mma.scala），不依赖手写 Verilog 或工具特技，跨平台可移植。

> 待本地验证：若你有 Vivado 与 xc7k480t 板卡，可用 `make build-fpga` 跑两版——一版保留 `pipe_*`、一版临时注释掉它们直接把 `sarray.io.out_a` 接到 `pe_io.in_a`——对比两版的时序报告 WNS 与违例端点数，实地复现 −0.151 ns 与正裕量的差异。

## 6. 本讲小结

- **平台拓扑**：chisel-npu 的官方验证平台是 Kintex-7 `xc7k480t`，数据走「主机 PCIe → XDMA → AXI 时钟/位宽转换 → `axi_xbar` 交叉开关 → MIG → 双 DDR3」；V10 用单个 `npu_subsys`（`ctrl_lite` + `npu_dma_master` + MMALU）把 NPU 挂上统一 4 GB 地址空间。
- **时钟域**：125 MHz `axi_aclk`（PCIe）→ 200 MHz `fabric_aclk`（MMCM 倍频，MMALU 计算域）→ 133 MHz MIG `ui_clk`（DDR3）；250 MHz PCIe PHY 时钟被 `set_false_path` 豁免。
- **时序收敛**：K=32 缩放导致 22608 个违例端点，靠 DataFeeder 逐 lane 重构（扇出 1025→2）、200 MHz fabric、以及 MMALU 内部插流水寄存器三层手段，最终闭合到 0 违例。
- **WNS = −0.151 ns**：源于 `reg_h` → 13 级逻辑 → PE 乘加链，由 `pipe_a/pipe_b/pipe_ctrl` 等流水寄存器切断，代价是延迟从 \(3n-2\) 变 \(3n-1\)（K=32 时 +1 拍，约 1%）。
- **构建流程**：`make build-fpga` 走宿主机 Vivado 批处理，调 `build_npu.tcl`（量产）/`build_npu_with_ila.tcl`（调试带 ILA），产 `top_npu.bit` 比特流；它依赖 `make build` 生成的 `top.sv`。
- **诚实边界**：本平台当前 V10 仅验证了 MMALU 的「单射」语义（`OUT[i]=A[i]·B[K-1]+ACCUM[i]`），完整 M×K GEMM 流式是 Phase 2；K=64 因 LUT 超出芯片容量（1.69×）而不可用，故上板 K=32。

## 7. 下一步学习建议

本讲把「硬件平台长什么样、时序怎么收住、比特流怎么生成」讲清了。接下来的两讲从「板子」上升到「软件怎么驱动板子」：

- **u8-l2 Linux XDMA 内核驱动与 C 工具**：学习 `/dev/xdma0_*` 设备节点、`reg_rw` 与 `dma_to_device` 工具如何映射到本讲的控制路径（`ctrl_lite`）与数据路径（DDR3 staging），把第 4.1.4 节那张「6 步操作表」用 C 工具实地实现一遍。
- **u8-l3 Python 用户态驱动 chisel_npu_py**：看上层 Python API 如何把本讲的 `stage→kick→wait→collect` 四步封装成 numpy 友好的接口，理解 pybind11 边界为何独占所有 fd 与 DDR 地址。

如果你更想往「计算正确性」深挖，可回到 **u7-l1/u7-l2** 看端到端量化流水线如何在本讲这块硬件上被验证（`NCoreBackendQuantSpec` 等测试正是这套平台要承载的负载）。继续阅读建议从 `docs/implementations/FPGA_XC7K480T.md` 的「ILA Debug Methodology」与「Bring-up history (V0..V10)」两节入手，那是把一块板子从点不亮带到 9/9 冒烟 + 5/5 MMALU 测试全过的真实记录。
