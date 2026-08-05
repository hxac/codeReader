# FPGA AFU 外壳与驱动

## 1. 本讲目标

Vortex 在前面所有讲义里都跑在 SimX（C++ 仿真）或 RTL 仿真（rtlsim）上。本讲回答一个新问题：**如何把 Vortex 这颗 RISC-V GPGPU 真正放到一块 FPGA 板卡上运行？**

学完本讲你应该能够：

- 说清「AFU 外壳（AFU shell）」是什么、为什么 Vortex 需要它才能上 FPGA。
- 对照理解两种 FPGA 平台路径——Xilinx/XRT 与 Intel/OPAE——在**控制通路**（MMIO 寄存器）、**数据通路**（设备内存 + 主机内存 DMA）、**中断**上的关键差异。
- 读懂 `sw/runtime/xrt` 与 `sw/runtime/opae` 这两个主机驱动后端如何被同一份 `callbacks_t` 契约（u3-l3）约束成「纯传输 HAL」。
- 按照 `docs/fpga_setup.md` 走通 Xilinx Alveo 板卡的综合、上板与运行流程，并知道为什么 OPAE 路径被标记为废弃。

本讲是专家层的「FPGA、综合与二次开发」单元首讲，承接 **u3-l3（驱动后端与 stub 动态分发）** 和 **u11-l3（命令处理器与 KMU）**——你会发现，AFU 外壳的本质就是「平台胶水 + 命令处理器（CP）」，CP 是唯一的数据搬运工与启动源。

## 2. 前置知识

- **FPGA 加速卡（Accelerator Card）**：一块插在主机 PCIe 插槽上的卡，上面有大量可编程逻辑（FPGA）和若干 DDR/HBM 内存。卡上跑的不是 CPU，而是用户用硬件描述语言（Verilog）描述的电路。
- **AFU（Acceleration Function Unit，加速功能单元）**：Intel OPAE 术语，指「用户放进 FPGA 加速卡的那块逻辑」。在 Xilinx 体系里类似概念叫 **RTL kernel**。本仓库里「AFU 外壳」就是把 Vortex 整颗 GPU 包起来、让它能被 PCIe 主机驱动的最外层 Verilog。
- **PCIe / AXI / AXI-Lite**：PCIe 是主机与板卡之间的物理/协议链路。AXI 是 ARM 发起的片上总线协议（有独立的读、写、写响应通道）；AXI-Lite 是 AXI 的「轻量」版本，每拍传一个数据，常用于寄存器读写。
- **CCI-P（Cache Coherent Interface for Accelerators）**：Intel 为 OPAE 加速卡定义的主机↔FPGA 接口，与 AXI 平级、互不兼容。它的 MMIO（Memory-Mapped IO，内存映射寄存器）用于控制，c0/c1 通道用于读写主机内存。
- **MMIO**：把设备寄存器映射到一段地址空间，主机像读写内存一样读写它来控制设备。
- **命令处理器（Command Processor, CP）**：Vortex 设备侧的单一控制平面，由 `VX_cp_core` 实现（u11-l3）。CP 从主机内存取命令环（command ring）、做 DMA、解包 launch/DCR。**CP 是本讲最重要的角色**：两条 FPGA 路径都把启动与数据搬运完全交给 CP，外壳本身只做协议翻译。
- **XCLBIN**：Xilinx 的 FPGA 比特流容器格式，`.xclbin` 文件里既有比特流也有内核元数据。
- **BO（Buffer Object）**：XRT 里「一段被设备可见的内存」的抽象，类似 CUDA 的 device buffer。

如果你对 stub 动态分发（`$VORTEX_DRIVER` → `dlopen` 后端 `.so`）和 `callbacks_t` 契约已经生疏，建议先回顾 u3-l3。

## 3. 本讲源码地图

本讲横跨 RTL 外壳、主机驱动后端与文档三处：

| 文件 / 目录 | 角色 |
|---|---|
| [docs/designs/fpga_afu_shell.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/fpga_afu_shell.md) | AFU 外壳的设计总纲：XRT 与 OPAE 两套外壳的结构、共享组件、关键不对称、未实现项。 |
| [docs/fpga_setup.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/fpga_setup.md) | 上板实操手册：Xilinx 综合、host 内存使能、运行；附废弃的 OPAE 流程。 |
| `hw/rtl/afu/xrt/` | Xilinx/XRT 外壳：`vortex_afu.v`（Vitis RTL-kernel 顶层）、`VX_afu_wrap.sv`（真正的外壳）、`VX_afu_ctrl.sv`（精简 AXI-Lite 从机）。 |
| `hw/rtl/afu/opae/` | Intel/OPAE 外壳：`vortex_afu.sv`（CP-only CCI-P AFU 垫片）、`ccip_std_afu.sv` 等平台管线。 |
| `sw/runtime/xrt/vortex.cpp` | XRT 主机驱动后端：设备生命周期、CP 寄存器通道、CP 可见主机内存。 |
| `sw/runtime/opae/vortex.cpp`、`driver.cpp`、`driver.h` | OPAE 主机驱动后端：同上三类能力，外加 `dlopen` 加载 OPAE 库的 `opae_drv_api_t` 函数表。 |
| `sw/runtime/common/callbacks.inc` | 所有后端共享的 `vx_dev_init` 模板，把后端 `vx_device` 类桥接到 `callbacks_t`（u3-l3）。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **AFU 外壳是什么**——为什么 Vortex 上 FPGA 必须多一层「平台胶水」。
2. **AFU 外壳 RTL（XRT ↔ OPAE 对照）**——控制、内存、中断三条通路的实现差异。
3. **主机驱动后端（runtime/xrt 与 runtime/opae）**——外壳的「软件镜像」。
4. **FPGA 构建与上板运行**——把上面的零件编进比特流并跑起来。

### 4.1 AFU 外壳是什么：平台胶水 + 命令处理器

#### 4.1.1 概念说明

前面讲义里的 Vortex 顶层是 [`Vortex.sv`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/Vortex.sv)（membus 端口，供 OPAE/仿真用）与 [`Vortex_axi.sv`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/Vortex_axi.sv)（AXI 端口，供 XRT 用）。这两颗顶层暴露的是 Vortex 自定义的 `membus`/AXI 内存端口和 `start`/`busy`/`dcr_*` 控制端口——它们是「裸 GPU 核」，**不知道**自己被插在 PCIe 上、不知道主机怎么跟它说话。

FPGA 加速卡厂商（Xilinx、Intel）规定了卡上逻辑必须遵守的接口契约（XRT 的 Vitis RTL kernel 接口、OPAE 的 CCI-P 接口）。Vortex 的裸核端口与厂商契约之间存在「阻抗失配」：

```
主机程序 ──PCIe──▶ [厂商规定的接口契约] ──???──▶ [Vortex 裸核端口]
```

**AFU 外壳**就是中间那块「???」：它把厂商契约翻译成 Vortex 裸核能理解的信号，反之亦然。设计文档 [fpga_afu_shell.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/fpga_afu_shell.md) 用一句话概括了外壳的本质：

> Both shells reduce to **platform glue + the Command Processor**: the CP (`VX_cp_core`) is the sole launch and DCR source in both.

也就是说，外壳做两件事：

1. **平台胶水（platform glue）**：把厂商总线（AXI-Lite / CCI-P MMIO、AXI4 / CCI-P c0c1、Avalon 本地内存）接上 Vortex 的端口。
2. **实例化一个 CP**：CP 负责一切「有智能的」控制——取命令环、做 DMA、解包 launch、维护 DCR。外壳自己**没有**遗留的启动状态机、没有自己的 DCR 寄存器、没有自己的 DMA 引擎——这些在文档里被明确列为「已移除」（superseded directions）。

这点至关重要：**外壳越「傻」越好**。所有复杂度集中在 CP，而 CP 的行为在 SimX、RTL 仿真、FPGA 三种后端上完全一致（model_parity，见 u7-l4）。外壳只是 CP 的「PCIe 适配器」。

#### 4.1.2 核心流程

主机在 FPGA 上跑一个 Vortex 程序的完整路径（以 XRT 为例）：

```text
1. 主机 vx_dev_open()
     → stub 按 $VORTEX_DRIVER=xrt dlopen("libvortex-xrt.so")
     → XRT 后端 init(): 打开设备、load vortex_afu.xclbin、reset AP_CTRL
2. 主机在「CP 可见主机内存」里建命令环 + DMA 暂存区
     → XRT: 分配 host_only BO；OPAE: fpgaPrepareBuffer
3. 主机经 MMIO 写 CP 寄存器（CP_BASE + 偏移）配置 KMU 字段
     → 写 STARTUP_ADDR、KERNEL_ENTRY、grid/block/cluster 维度...
4. 主机发 CMD_LAUNCH（写入命令环，敲 doorbell）
5. CP 经 axi_host 主机内存主端口取命令、解包、把 KMU 请求派发给 Vortex 核
6. Vortex 核执行；CP 经 axi_dev 设备内存主端口做 DMA 搬运数据
7. 程序结束，主机读退出码
```

注意第 3 步：主机写的「CP 寄存器」地址不是裸 Vortex 的 `dcr_*` 端口，而是经外壳 MMIO 解复用（demux）后路由到 CP regfile 的地址——这是下一节的重点。

#### 4.1.3 源码精读

外壳顶层文件 [`vortex_afu.v`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/afu/xrt/vortex_afu.v) 是 Vitis RTL-kernel 的最薄封装，几乎只做端口声明与实例化：

[vortex_afu.v:16-68](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/afu/xrt/vortex_afu.v#L16-L68) —— 声明 Vitis 规定的端口：`ap_clk`/`ap_rst_n` 系统信号、若干 AXI4 主端口（`GEN_AXI_MEM`，设备内存 bank）、一个 AXI4 主机内存主端口（`GEN_AXI_HOST`，CP 命令环 + DMA）、一组 AXI-Lite 从端口（`s_axi_ctrl_*`，主机控制）、一个 `interrupt` 引脚。

[vortex_afu.v:70-109](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/afu/xrt/vortex_afu.v#L70-L109) —— 实例化 `VX_afu_wrap`，把上述端口原样接进去。真正的逻辑全在 `VX_afu_wrap.sv` 里。

`VX_afu_wrap.sv` 顶部的注释把外壳的角色写得很清楚（[VX_afu_wrap.sv:18-37](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/afu/xrt/VX_afu_wrap.sv#L18-L37)）：数据面是「Vortex 内存 bank 走平台 AXI、CP 有自己的 axi_m」；launch/DCR「完全由 CP 通过 cp_gpu_if 驱动」。

#### 4.1.4 代码实践

**实践目标**：建立「裸 Vortex 核」与「AFU 外壳」的边界直觉。

**操作步骤**：

1. 打开 [`hw/rtl/Vortex_axi.sv`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/Vortex_axi.sv) 的模块端口列表，找出 Vortex 自己定义的 AXI 内存端口和控制端口（`start`/`busy`/`dcr_*`）。
2. 打开外壳 [`vortex_afu.v`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/afu/xrt/vortex_afu.v)，对比它的端口：哪些是 Vitis 厂商契约规定的（`ap_clk`、`ap_rst_n`、`s_axi_ctrl_*`、`interrupt`），哪些是承接 Vortex 的（内存 bank）。
3. 在 [`VX_afu_wrap.sv`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/afu/xrt/VX_afu_wrap.sv) 里搜索 `Vortex_axi` 与 `VX_cp_core` 两个实例化，确认外壳里只有「一个 Vortex 核 + 一个 CP + 一堆胶水」。

**需要观察的现象**：Vortex 裸核没有任何 `ap_clk`/`ap_rst_n`/`interrupt` 之类 Vitis 专用端口——这些是外壳「凭空」加上去的厂商契约；反过来，Vitis 端口里看不到 Vortex 的 `dcr_req_*`——因为它被 CP 藏起来了。

**预期结果**：你能画出一张表，左列是 Vortex 裸核端口，右列是对应的 Vitis/外壳端口，中间填上「胶水模块名」。本讲不要求运行，结果待本地确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么外壳里要单独再实例化一个 `VX_cp_core`，而不是复用 Vortex 核内部已有的控制逻辑？

**参考答案**：因为 FPGA 加速卡需要从**主机内存**取命令并做 DMA——这是 PCIe 设备的职责，与 GPU 核内部的计算流水线属于不同抽象层。CP 是「主机侧命令到设备侧 CTA 派发」的单一桥（u11-l3），把它放在外壳里、与 Vortex 核并列，可以让 Vortex 核保持「纯计算」的纯净端口，同时让 CP 在 SimX/RTL/FPGA 三种后端上行为一致（model_parity）。

**练习 2**：文档说外壳「reduce to platform glue + CP」。列举一个被「移除」的遗留功能（superseded direction），并解释移除它的好处。

**参考答案**：例如「per-AFU DCR 寄存器」和「OPAE 的 `STATE_*` DMA-command FSM」。移除它们让 CP 成为唯一的命令/DCR/DMA 路径，消除了「外壳 FSM 与 CP 各做一套」的状态分叉风险，也让 SimX↔RTL parity 更容易维护——因为只有一套控制逻辑需要建模。

---

### 4.2 AFU 外壳 RTL：XRT 与 OPAE 对照

#### 4.2.1 概念说明

XRT（Xilinx）和 OPAE（Intel）是两套**互不兼容**的 FPGA 平台栈：总线协议、控制接口、内存模型、中断机制全都不一样。因此 Vortex 维护了**两套并行的外壳**，分别在 `hw/rtl/afu/xrt/` 与 `hw/rtl/afu/opae/`。

但两套外壳要解决的问题是同构的，都围绕三条通路：

| 通路 | 要解决的问题 |
|---|---|
| **控制通路** | 主机如何读写设备的寄存器（尤其是 CP 的 regfile）？ |
| **设备内存通路** | Vortex 的内存 bank 如何接到卡上 DDR/HBM？CP 的设备 DMA 怎么并进去？ |
| **主机内存通路** | CP 怎么越过 PCIe 读写主机内存（取命令环、搬数据）？ |
| **中断** | 设备完成时如何通知主机？（可选） |

记住三条通路的差异，就抓住了 XRT ↔ OPAE 的全部不对称。设计文档 [fpga_afu_shell.md §3](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/fpga_afu_shell.md) 把这些不对称归纳成一句：

> dedicated host-AXI master (`m_axi_host`) vs. a CCI-P host-DMA state machine; an interrupt pin vs. none; `VX_axi_arb2` vs. `VX_mem_arb` for bank-0 sharing; AXI-Lite `addr[12]` vs. CCI-P MMIO word-address bit 10 for the control demux.

#### 4.2.2 核心流程

**控制通路的地址解复用（demux）**——两条路径都把主机 MMIO 地址空间劈成两半：低半页给「外壳自己的最小控制块」（ap_ctrl 桩 / DFH 头 + SCOPE 调试探针），高半页给 CP regfile：

```
XRT  (AXI-Lite):     地址 bit[12] = 0 → VX_afu_ctrl   (0x0000-0x0FFF)
                     地址 bit[12] = 1 → CP regfile     (0x1000-0x1FFF, CP 看到的是减去 0x1000)

OPAE (CCI-P MMIO):   字地址 bit[10] = 0 → DFH/SCOPE   (主机字节 0x000-0xFFF)
                     字地址 bit[10] = 1 → CP regfile   (主机字节 0x1000+, CP 看到 0x000-based)
```

> 注意：CCI-P 的 MMIO 地址以 4 字节为单位，所以「字地址 bit 10」对应「字节地址 0x1000」，与 XRT 的 bit[12] 切分点字节地址一致。两套外壳的 CP_BASE 都是 `0x1000`。

**设备内存通路**：Vortex 的内存 bank 1..N 在 XRT 里直连平台 AXI 主端口；bank 0 与 CP 的设备 DMA 主端口（`axi_dev`）共享同一个物理端口，靠一个 2:1 仲裁器合并。OPAE 类似，但内存是 Avalon 本地内存，且所有 bank + CP 都汇入一个 `VX_mem_arb` 再经 `VX_avs_adapter` 转 Avalon。

**主机内存通路**：这是两套外壳最大的差异。XRT 给 CP 配了一条**专用的 AXI 主端口** `m_axi_host`，XRT 运行时把这条端口钉到卡的「host bank」（主机 RAM 暴露给卡的窗口），CP 发的 AXI 地址原样直通主机。OPAE 没有这种现成的主机 AXI 端口，于是外壳里手写了一个 **CCI-P c0/c1 单出（single-outstanding）状态机**，把 CP 的内存请求翻译成 CCI-P 的读（c0 RDLINE）/写（c1 WRLINE）事务打到主机。

**中断**：XRT 外壳把 CP 的 `cp_interrupt` 接到顶层 `interrupt` 引脚；OPAE 没有 FPGA 平台中断引脚，`cp_interrupt` 被显式标记为未使用（`UNUSED_VAR`），主机只能轮询。

#### 4.2.3 源码精读

**XRT 控制解复用**——用地址 bit[12] 选择目标从机：

[VX_afu_wrap.sv:159-161](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/afu/xrt/VX_afu_wrap.sv#L159-L161) —— `wire is_cp_aw = s_axi_ctrl_awaddr[12];` 与读地址同理。注释「Bit 12 picks the slave: host addr[12]=1 → CP regfile; addr[12]=0 → legacy」点明了切分。

**XRT 主机内存主端口**——CP 拥有专用 `m_axi_host`，地址原样直通主机（无 `PLATFORM_MEMORY_OFFSET`，因为那是设备内存专属偏移）：

[VX_afu_wrap.sv:283-326](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/afu/xrt/VX_afu_wrap.sv#L283-L326) —— 把 `VX_cp_core` 的 `cp_axi_host` 接口逐线 `assign` 到顶层 `m_axi_host_*` 端口。CP 是这条主机的唯一用户。其中 [VX_afu_wrap.sv:289-296](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/afu/xrt/VX_afu_wrap.sv#L289-L296) 是 `VX_cp_core` 实例化本身。

**XRT 中断**：

[VX_afu_wrap.sv:335](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/afu/xrt/VX_afu_wrap.sv#L335) —— `assign interrupt = cp_interrupt;` 把 CP 的 irq 接到 Vitis 的中断引脚。

**XRT bank-0 共享仲裁**——Vortex bank-0 与 CP 设备主端口经 `VX_mm_axi_arb` 合并，Vortex（index 0）享优先级：

[VX_afu_wrap.sv:500-540](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/afu/xrt/VX_afu_wrap.sv#L500-L540) —— 先把 CP 的窄 ID 补零到平台 ID 宽度（L503-506），把 CP 地址减去 `PLATFORM_MEMORY_OFFSET` 转成 bank 相对地址（L510-513），再用 `ARBITER="P"` 的 2:1 仲裁器合并两路 packed 通道（L533-540）。

**OPAE 控制解复用**——用 CCI-P 字地址 bit[10]：

[vortex_afu.sv:116-124](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/afu/opae/vortex_afu.sv#L116-L124) —— `wire is_cp_mmio_req = mmio_req_hdr.address[10];`，CP 写/读请求由它门控。注释说明 bit 10 对应字节地址 0x1000。读响应用一个 mux 在 DFH 处理器与 CP regfile 之间二选一（[vortex_afu.sv:113-114](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/afu/opae/vortex_afu.sv#L113-L114)）。

**OPAE 中断未使用**：

[vortex_afu.sv:325-326](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/afu/opae/vortex_afu.sv#L325-L326) —— `wire cp_interrupt;` 紧跟 `` `UNUSED_VAR(cp_interrupt) ``，OPAE 没有 FPGA 平台中断引脚。

**OPAE 主机内存状态机**——手写的 CCI-P c0/c1 单出事务机，是 OPAE 唯一的 CCI-P DMA 用户：

[vortex_afu.sv:396-433](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/afu/opae/vortex_afu.sv#L396-L433) —— 定义 `HB_IDLE/HB_RD/HB_RD_RSP/HB_WR` 四状态，读走 c0（`eRSP_RDLINE`），写走 c1（`eRSP_WRLINE`），每次只处理一笔在途事务（single-outstanding）。

**OPAE 设备内存合并**——CP 的 `axi_dev` 经 `VX_membus_from_axi` 转成 membus，再与 Vortex bank 流经 `VX_mem_arb`：

[vortex_afu.sv:511-549](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/afu/opae/vortex_afu.sv#L511-L549) —— `VX_membus_from_axi` 桥把 CP 的 AXI 设备主端口转成 Vortex membus 格式，喂给 2 路仲裁器 `cp_vx_mem_arb_in_if[2]`（[0]=Vortex bank0、[1]=CP）。

#### 4.2.4 代码实践

**实践目标**：亲手核对两条路径在三条通路上的差异，形成「不对称清单」。

**操作步骤**：

1. 在 [`VX_afu_wrap.sv`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/afu/xrt/VX_afu_wrap.sv) 中分别定位：控制解复用（搜索 `awaddr[12]`）、主机主端口（搜索 `m_axi_host`）、bank-0 仲裁（搜索 `VX_mm_axi_arb`）、中断（搜索 `cp_interrupt`）。
2. 在 [`vortex_afu.sv`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/afu/opae/vortex_afu.sv)（OPAE）中定位对应物：`address[10]`、`HB_IDLE`、`VX_membus_from_axi`、`UNUSED_VAR(cp_interrupt)`。
3. 填写下面这张对照表。

**需要观察的现象**：两套外壳的「控制解复用位」、bank-0 仲裁器型号、主机内存机制、中断是否接出，全部一一对应但互不相同。

**预期结果**（对照表）：

| 通路 | XRT | OPAE |
|---|---|---|
| 控制解复用 | AXI-Lite `addr[12]` | CCI-P MMIO 字地址 `address[10]` |
| CP 寄存器基址 | `0x1000` | `0x1000` |
| bank-0 共享仲裁 | `VX_mm_axi_arb`（Vortex 优先） | `VX_mem_arb` |
| 主机内存机制 | 专用 AXI 主端口 `m_axi_host` | CCI-P c0/c1 状态机（`HB_*`） |
| 中断 | `interrupt = cp_interrupt` | 未使用 |

本讲不要求综合运行，结果待本地确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 XRT 用地址 bit[12] 而 OPAE 用「字地址 bit[10]」来切分，实际切分点却都是字节地址 0x1000？

**参考答案**：因为 CCI-P 的 MMIO 地址以 4 字节（字）为单位，`address[10]` 表示字偏移 1024，即字节偏移 1024×4 = 4096 = 0x1000；XRT 的 AXI-Lite 地址直接是字节地址，bit[12] 也是 0x1000。两套协议的寻址单位不同，但切分点一致，所以两套外壳的 `CP_BASE` 都是 `0x1000`，主机驱动代码可以共用这个常量。

**练习 2**：OPAE 外壳的 `HB_*` 状态机为什么是「single-outstanding」（一次只处理一笔在途事务）？这会带来什么代价？

**参考答案**：CCI-P 的 c0/c1 通道响应可能乱序、且 OPAE 外壳用最简单的状态机把 CP 的 membus 请求翻译成 CCI-P 事务——single-outstanding 牺牲了并发（多笔 DMA 不能同时挂起），换取状态机的极简与可预测。代价是主机内存 DMA 吞吐受限。文档 [fpga_afu_shell.md §4.1](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/fpga_afu_shell.md) 把「把 OPAE DMA 迁到 CP 的 `CMD_MEM_*` 路径（像 XRT 那样）」列为**尚未开始**的改进项。

---

### 4.3 主机驱动后端：runtime/xrt 与 runtime/opae

#### 4.3.1 概念说明

外壳是「硬件镜像」，主机驱动后端是「软件镜像」。回顾 u3-l3：`libvortex.so` 的 stub 在首次 `vx_dev_open` 时按 `$VORTEX_DRIVER` `dlopen` 一个后端 `.so`（如 `libvortex-xrt.so`），调用后端的 `vx_dev_init` 填充一张 `callbacks_t` 函数指针表。后端被约束成**纯传输 HAL（Hardware Abstraction Layer，硬件抽象层）**——只暴露三类能力，不做任何「有智能的」事：

[callbacks.inc:20-31](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/callbacks.inc#L20-L31) —— 列出每个后端 `vx_device` 类必须实现的 5 个方法：`init`、`cp_reg_write`、`cp_reg_read`、`host_mem_alloc`、`host_mem_free`，并明确「设备内存分配、DMA、能力解码都在公共核心里；CP 是唯一的命令+DMA 引擎，后端只暴露寄存器通道与 CP 可见主机内存」。

这三个方法对应外壳的三条通路：

| 后端方法 | 对应外壳通路 | 干什么 |
|---|---|---|
| `init()` / 析构 | 控制通路 | 打开设备、加载比特流、reset |
| `cp_reg_write/read(off, value)` | 控制通路（CP regfile） | 经 MMIO 读写 CP 寄存器，自动加 `CP_BASE=0x1000` |
| `host_mem_alloc/free` | 主机内存通路 | 分配「CP 能越过 PCIe 看见的主机内存」，返回主机指针 + CP 地址 |

注意：**没有**「设备内存」方法——设备内存分配、地址翻译、DMA 编排全在公共核心 `sw/runtime/common` 里，后端只负责「把这段主机内存暴露给 CP」。

#### 4.3.2 核心流程

两个后端的 `init()` 流程对照：

```text
XRT init():                              OPAE init():
  读 XRT_DEVICE_INDEX / XRT_XCLBIN_PATH    drv_init() dlopen OPAE 库填 api_ 函数表
  打开 xrt::device(device_index)          按 AFU UUID 枚举加速卡 (fpgaEnumerate)
  load_xclbin(vortex_afu.xclbin)           找到后 fpgaOpen(accel_token)
  取 xrt::ip("vortex_afu") 内核             ——
  write ap_ctrl 的 RESET 位                 ——
```

`cp_reg_*` 流程对照：

```text
XRT:  cp_reg_write(off, val) → write_register(0x1000 + off, val) → xrtKernel_.write_register(...)
OPAE: cp_reg_write(off, val) → fpgaWriteMMIO64(fpga_, 0, 0x1000 + off, val)
```

`host_mem_alloc` 流程对照（这是两套平台差异最大的地方）：

```text
XRT (硬件):  分配 host_only BO → bo.map() 得主机指针, bo.address() 得 CP 地址
XRT (xrtsim): 直接 aligned_alloc, CP 地址 = 主机指针（仿真器在进程内解引用）
OPAE:        fpgaPrepareBuffer → 主机指针 ptr + workspace id (wsid)
             fpgaGetIOAddress(wsid) → IO 地址 ioaddr（CP 用的地址）
```

关键点：`host_mem_alloc` 返回**两个地址**——`host_ptr`（主机 CPU 用来读写命令环）和 `cp_addr`（CP 用来越过 PCIe 访问同一块内存）。在 XRT 硬件上 `cp_addr` 是 host bank 内的 IO 地址；在 OPAE 上是 CCI-P 的 IO 地址；在 xrtsim/opaesim 仿真上 `cp_addr` 往往就是主机指针本身。

#### 4.3.3 源码精读

**XRT 后端**——[`sw/runtime/xrt/vortex.cpp`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/xrt/vortex.cpp)。文件头注释（[vortex.cpp:14-25](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/xrt/vortex.cpp#L14-L25)）讲清了三类能力与「CP 是唯一内存引擎」的原则。

[vortex.cpp:62-76](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/xrt/vortex.cpp#L62-L76) —— 定义 MMIO 控制寄存器地址（ap_ctrl 在 `0x00`、SCOPE 在 `0x28`）和 `CP_BASE 0x1000`，注释说明 AXI-Lite demux 把 `0x1000..0x1FFF` 路由到 CP regfile。

[vortex.cpp:148-249](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/xrt/vortex.cpp#L148-L249) —— `init()`：读环境变量选设备与 xclbin 路径，打开设备、load xclbin、取名为 `"vortex_afu"` 的 IP 内核，然后写 `CTL_AP_RESET` 复位。XRT 的 C++ API 可能抛异常，跨 `extern "C"` 边界是 UB，所以每个 XRT 调用都包在 `XRT_TRY/XRT_CATCH` 宏里（[vortex.cpp:102-110](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/xrt/vortex.cpp#L102-L110)）。

[vortex.cpp:252-258](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/xrt/vortex.cpp#L252-L258) —— CP 寄存器通道：调用方传 CP 内部偏移，`cp_reg_*` 加 `CP_BASE` 后调 `write_register/read_register`。

[vortex.cpp:263-303](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/xrt/vortex.cpp#L263-L303) —— `host_mem_alloc`：硬件路径用 `xrt::bo(..., xrt::bo::flags::host_only, ...)` 分配仅主机可见的 BO，`bo.map()` 得主机指针、`bo.address()` 得 CP 地址；`host_bos_` map 用 mutex 保护（worker 线程会并发 alloc/free）。xrtsim 路径退化为 `aligned_alloc`，CP 地址即指针。

**OPAE 后端**——[`sw/runtime/opae/vortex.cpp`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/opae/vortex.cpp)。文件头注释（[vortex.cpp:14-24](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/opae/vortex.cpp#L14-L24)）同样声明三类能力。

[vortex.cpp:45-52](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/opae/vortex.cpp#L45-L52) —— SCOPE MMIO 地址与 `CP_BASE 0x1000`，与 XRT 完全一致。

[vortex.cpp:85-164](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/opae/vortex.cpp#L85-L164) —— `init()`：先 `drv_init(&api_)` 把 OPAE 库的函数指针填进 `opae_drv_api_t` 表（见下），再用 `fpgaPropertiesSetGUID` 按 AFU 的 UUID 枚举加速卡，`fpgaOpen` 打开。与 XRT 的「按设备索引打开」不同，OPAE 是「按 AFU UUID 枚举」。

[vortex.cpp:170-184](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/opae/vortex.cpp#L170-L184) —— CP 寄存器通道：`fpgaWriteMMIO64(fpga_, 0, CP_BASE + off, value)`。

[vortex.cpp:189-213](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/opae/vortex.cpp#L189-L213) —— `host_mem_alloc`：`fpgaPrepareBuffer` 分配 CCI-P 共享缓冲（得主机指针 ptr + wsid），`fpgaGetIOAddress` 把 wsid 换成 CP 用的 IO 地址。

**OPAE 的 dlopen 函数表**——OPAE 后端比 XRT 多一层间接：它不直接链接 OPAE 库，而是在运行时 `dlopen` 加载，把所有用到的 OPAE C 函数填进一张函数指针表：

[driver.h:41-60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/opae/driver.h#L41-L60) —— `opae_drv_api_t` 结构体，列举 `fpgaOpen/fpgaClose/fpgaPrepareBuffer/fpgaReleaseBuffer/fpgaGetIOAddress/fpgaWriteMMIO64/fpgaReadMMIO64/...` 等函数指针。这层间接让 `libvortex-opae.so` 不在链接期硬依赖 OPAE 库，便于在不同 Intel 工具链版本间移植。

两个后端文件都以 `#include <callbacks.inc>`（[xrt/vortex.cpp:352](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/xrt/vortex.cpp#L352)、[opae/vortex.cpp:228](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/opae/vortex.cpp#L228))收尾——这是 u3-l3 讲过的模板，把 `vx_device` 的 5 个方法桥接成 `callbacks_t` 的 C 函数指针，注册进 stub。

#### 4.3.4 代码实践

**实践目标**：验证「两个后端是同一份契约的两种实现」。

**操作步骤**：

1. 同时打开 [`sw/runtime/xrt/vortex.cpp`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/xrt/vortex.cpp) 与 [`sw/runtime/opae/vortex.cpp`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/opae/vortex.cpp)。
2. 对照两个文件的 `cp_reg_write`、`cp_reg_read`、`host_mem_alloc` 三个方法的签名与实现。
3. 打开 [`sw/runtime/common/callbacks.inc`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/callbacks.inc)，确认它调用的正是这 5 个方法名。

**需要观察的现象**：两个后端的方法签名完全一致（由 `callbacks.inc` 的契约规定），但实现里出现的平台 API 完全不同——XRT 用 `xrt::bo`/`write_register`，OPAE 用 `fpgaPrepareBuffer`/`fpgaWriteMMIO64`。这就是「HAL」的意义：上层（公共核心）只认 5 个方法，不关心底下是 XRT 还是 OPAE。

**预期结果**：你能解释为什么换一个 FPGA 厂商只需要新写一个 `vortex.cpp` 实现 5 个方法，而公共核心一行不用改。结果待本地确认。

#### 4.3.5 小练习与答案

**练习 1**：`host_mem_alloc` 为什么必须返回**两个**地址（`host_ptr` 和 `cp_addr`），而 `vx_mem_alloc`（设备内存）在公共核心里只需要一个？

**参考答案**：因为主机内存被两个角色共享——主机 CPU 用 `host_ptr`（进程虚拟地址）读写命令环；CP 越过 PCIe 用 `cp_addr`（IO 地址）访问同一块物理内存。在 FPGA 硬件上这两个地址不同（CPU 视角 vs 设备视角），所以必须都返回。设备内存则不同：它只在设备侧被 Vortex 核访问，主机从不直接读写（主机只通过 CP 的 DMA 间接触达），所以公共核心只需一个设备地址。这是 u3-l2「分配设备内存只是分发地址数字」的体现。

**练习 2**：OPAE 后端为什么要用 `drv_init` 在运行时 `dlopen` OPAE 库，而不是直接 `#include <opae/fpga.h>` 链接？

**参考答案**：为了解耦——OPAE 库随 Intel 工具链版本变化（路径、ABI 可能不同），运行时 dlopen 让 `libvortex-opae.so` 不在链接期硬依赖某个具体版本的 OPAE 库，部署更灵活。注意 `driver.h` 顶部（[driver.h:16-20](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/opae/driver.h#L16-L20)）区分 `OPAESIM` 用 `<fpga.h>`、否则用 `<opae/fpga.h>`，正是为了兼容仿真与真实库。

---

### 4.4 FPGA 构建与上板运行

#### 4.4.1 概念说明

把外壳 + Vortex 核变成能跑的比特流，并跑一个程序，是 [`docs/fpga_setup.md`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/fpga_setup.md) 的主题。这条流程承接 u1-l3 的构建体系（`configure`/`config.mk`）和 u1-l4 的 `blackbox.sh` 启动器，只是把 `--driver=simx` 换成了 `--driver=xrt`（或 `opae`）。

> **重要**：`fpga_setup.md` 把 **OPAE / Intel 流程明确标记为「废弃 / 不再维护」**（[fpga_setup.md:170-177](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/fpga_setup.md#L170-L177)）：目标 Intel PAC 卡（Arria 10 / Stratix 10）已停产，依赖 Intel 提供的平台文件，构建不保证在当前工具链可用。**新工作请走 Xilinx Alveo / XRT 流程**。本节以 XRT 为主线，OPAE 仅作对照。

两条路径支持的板卡：

| 路径 | 厂商 | 支持板卡 | 状态 |
|---|---|---|---|
| XRT | Xilinx | Alveo U50 / U280 / U55C（`xilinx_u50_gen3x16_xdma_*`、`xilinx_u280_gen3x16_xdma_*` 等） | 受支持、CI 主路径 |
| OPAE | Intel | Arria 10 / Stratix 10 PAC（`DEVICE_FAMILY=arria10\|stratix10`） | 废弃、仅供参考 |

#### 4.4.2 核心流程

XRT 上板的完整流程（来自 [fpga_setup.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/fpga_setup.md)）：

```text
1. 安装 Vortex 工具链（含 verilator，综合要用它生成 system Verilog）
     mkdir build && cd build && ../configure --tooldir=$HOME/tools
     ./ci/toolchain_install.sh
2. 综合比特流（在 hw/syn/xilinx/xrt）
     cd hw/syn/xilinx/xrt
     PREFIX=test1 PLATFORM=xilinx_u50_gen3x16_xdma_5_202210_1 \
       TARGET=hw NUM_CORES=1 make
     → 产物：<BUILD_DIR>/bin/vortex_afu.xclbin
3. 使能 host 内存（每卡一次，持久；仅 hw 流程需要，hw_emu 不需要）
     sudo xrt-smi configure --host-mem -d <BDF> --size 1G enable
4. 运行（在 Vortex build 目录）
     FPGA_BIN_DIR=<比特流目录> TARGET=hw PLATFORM=<平台名> \
       ./ci/blackbox.sh --driver=xrt --app=sgemm --args="-n128"
```

几个关键概念：

- **`TARGET=hw` vs `hw_emu`**：`hw` 产出真正烧进 FPGA 的比特流；`hw_emu` 是 Xilinx 的硬件仿真（在主机上软件模拟卡），慢但不需要真卡，也不需要使能 host 内存。
- **`NUM_CORES=N`**：是 `hw/syn/xilinx/xrt/Makefile` 的预设宏组合（[fpga_setup.md:102](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/fpga_setup.md#L102)），展开成 `-DVX_CFG_NUM_CLUSTERS=… -DVX_CFG_NUM_CORES=…`——这正是 u2 的 `VX_CFG_*` 配置体系。
- **使能 host 内存**：这是 4.3 讲的「CP 可见主机内存」在物理上的对应——CP 的 `m_axi_host` 主端口需要卡的 host bank（主机 RAM 暴露给卡的窗口）可用。该 bank 默认关闭，必须用 `xrt-smi configure --host-mem` 一次性打开，否则 `vx_device_open` 会失败（[fpga_setup.md:124-130](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/fpga_setup.md#L124-L130)）。`--size` 须为 2 的幂，256M 够用、1G 留余量。
- **`blackbox.sh`**：和 u1-l4 完全是同一个启动器，只是 `--driver=xrt` 让 stub 去 `dlopen libvortex-xrt.so`，`FPGA_BIN_DIR`/`PLATFORM`/`TARGET` 三个环境变量喂给 XRT 后端去找 xclbin。

#### 4.4.3 源码精读

综合入口的 `NUM_CORES` 简写说明在 [fpga_setup.md:102](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/fpga_setup.md#L102)：

> `NUM_CORES=N` here is a Makefile shorthand that selects a pre-defined cluster/core/L2 combination … it expands to `-DVX_CFG_NUM_CLUSTERS=… -DVX_CFG_NUM_CORES=…` under the hood.

host 内存使能的失败症状与修复（[fpga_setup.md:124-145](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/fpga_setup.md#L124-L145)）：未使能时 `vx_device_open` 报 `Failed to allocate host memory buffer`，且文档强调 `--size` 必须是 2 的幂、256M 足够、1G 安全，且 `hw_emu` 不需要这一步。

运行命令模板（[fpga_setup.md:151-165](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/fpga_setup.md#L151-L165)）：

```
FPGA_BIN_DIR=<比特流目录> TARGET=hw PLATFORM=<平台名> \
  ./ci/blackbox.sh --driver=xrt --app=sgemm --args="-n128"
```

OPAE 的运行入口（[fpga_setup.md:247-251](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/fpga_setup.md#L247-L251)）——`TARGET=fpga ./ci/blackbox.sh --driver=opae --app=sgemm --args="-n128"`，形式与 XRT 对称，只是 `--driver` 不同。

OPAE 后端的构建目标由其 `Makefile` 的 `TARGET` 控制（[sw/runtime/opae/Makefile:3,26-32](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/opae/Makefile#L3)）：默认 `opaesim`（仿真，依赖 `libopae-c-sim.so`），真卡走 `fpga`/`ase`（依赖综合产物 `vortex_opae.h`）。XRT 后端对称（[sw/runtime/xrt/Makefile:3,21-27](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/xrt/Makefile#L3)）：默认 `xrtsim`，真卡为空（用真实 XRT 库 `libxrt_coreutil`）。两个 Makefile 产物分别是 `libvortex-opae.so`、`libvortex-xrt.so`——正是 stub 要 `dlopen` 的后端库。

#### 4.4.4 代码实践

**实践目标**：在无 FPGA 硬件的环境下，仍能跑通「构建后端 + 仿真驱动」的闭环，验证整条软件链。

**操作步骤**（需已按 u1-l3 配置好 `build/`）：

1. 编译 XRT 仿真后端（不需要真卡）：
   ```bash
   cd build && make -C sw/runtime/xrt TARGET=xrtsim
   ```
   预期产物 `sw/runtime/libvortex-xrt.so` 与依赖的 `libxrtsim.so`。
2. 编译 OPAE 仿真后端（默认即 `opaesim`）：
   ```bash
   make -C sw/runtime/opae
   ```
3. 用 xrtsim 跑 demo（不需要比特流）：
   ```bash
   VORTEX_DRIVER=xrt ./ci/blackbox.sh --driver=xrt --app=demo
   ```
   若环境不支持 xrtsim，则改为阅读 [fpga_setup.md:147-165](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/fpga_setup.md#L147-L165) 的硬件运行命令，并标注「待本地在 FPGA 节点上验证」。

**需要观察的现象**：`make` 能成功产出 `libvortex-xrt.so`；`blackbox.sh --driver=xrt` 走的是 XRT 后端（而非 simx），程序仍打印 `PASSED!`。

**预期结果**：确认「换 `--driver` 即换后端」这条 u3-l3 的承诺在 XRT 上也成立。如果你没有 FPGA 节点，明确写「综合与上板部分待本地验证」，并保留仿真部分的观察结果。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `hw_emu` 流程不需要 `xrt-smi configure --host-mem`，而 `hw` 流程必须做？

**参考答案**：因为 `hw_emu`（硬件仿真）在主机软件里模拟整个卡，包括 host bank——它在仿真器内部就建模了「主机内存暴露给卡」的窗口，所以 `host_mem_alloc` 总能成功。而 `hw`（真比特流跑真卡）依赖卡固件实际开辟的 host bank 窗口，该窗口默认关闭，必须由 `xrt-smi` 一次性打开并持久化。文档 [fpga_setup.md:144-145](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/fpga_setup.md#L144-L145) 明确：「This applies to the `hw` flow only. `hw_emu` models host memory in the emulator and needs no card configuration.」

**练习 2**：`blackbox.sh --driver=xrt` 与 `--driver=simx` 在 stub 层面的区别是什么？

**参考答案**：仅是 stub `dlopen` 的后端库不同——`xrt` 加载 `libvortex-xrt.so`，`simx` 加载 `libvortex-simx.so`。两者都实现同一份 `callbacks_t` 契约，公共核心与上层 API 完全相同。`blackbox.sh` 多出来的 `FPGA_BIN_DIR`/`PLATFORM`/`TARGET` 环境变量只是喂给 XRT 后端去定位 xclbin，对 simx 后端无意义。

## 5. 综合实践

把四个最小模块串起来，完成本讲的实践任务：**对比 OPAE 与 XRT 两条 FPGA 路径，画出 Vortex RTL → AFU 外壳 → PCIe → 主机驱动的集成示意，并说明各自支持的 FPGA 板卡。**

具体步骤：

1. **画硬件侧集成图**（承接 4.1、4.2）。从左到右画出：
   - 主机 PCIe 根复合体
   - 卡上的「厂商接口契约」框（XRT：AXI-Lite + AXI4 + host bank；OPAE：CCI-P MMIO + c0/c1 + Avalon 本地内存）
   - **AFU 外壳**框（XRT：`vortex_afu.v` → `VX_afu_wrap.sv`；OPAE：`vortex_afu.sv`）——内部标注「平台胶水 + `VX_cp_core`」
   - **Vortex 裸核**框（`Vortex_axi.sv` / `Vortex.sv`）
   - 三条连线：控制通路（MMIO 解复用 → CP regfile）、设备内存通路（bank + CP axi_dev → 仲裁器 → 卡上内存）、主机内存通路（CP `m_axi_host` / CCI-P `HB_*` → 主机 RAM）

2. **画软件侧镜像图**（承接 4.3）。画出：主机程序 → `vortex.h` 同步 API → 公共核心 → `callbacks_t` 契约 → 两个后端 `.so`（`libvortex-xrt.so` / `libvortex-opae.so`），标注每个后端实现的 5 个方法（`init`/`cp_reg_*`/`host_mem_*`）。

3. **填板卡表**（承接 4.4）：XRT = Xilinx Alveo U50/U280/U55C（受支持）；OPAE = Intel Arria 10/Stratix 10 PAC（废弃）。

4. **标注不对称点**：在两条路径上分别圈出控制解复用位、bank-0 仲裁器、主机内存机制、中断是否接出这 4 处差异。

5. **关键自检**：在你的图上确认——无论走哪条路径，**CP 都是唯一的命令/DCR/DMA 引擎**，外壳只做协议翻译。如果你画出的图里外壳承担了任何「智能」逻辑，说明画错了。

完成这张图后，你应该能向别人解释：为什么 Vortex 能用「同一颗 GPU 核 + 同一个 CP + 两个薄外壳 + 两个薄后端」同时跑在 Xilinx 和 Intel 两家互不兼容的 FPGA 上。

## 6. 本讲小结

- **AFU 外壳 = 平台胶水 + 命令处理器**：外壳是 Vortex 裸核与 FPGA 厂商接口契约（Vitis RTL kernel / CCI-P）之间的适配层，所有智能集中在 `VX_cp_core`，外壳本身「越傻越好」。
- **CP 是唯一的数据搬运工与启动源**：两条 FPGA 路径都把 launch、DCR、DMA 完全交给 CP，外壳没有自己的启动状态机、DCR 寄存器或 DMA 引擎——这些是已被移除的遗留设计。
- **XRT 与 OPAE 的差异集中在三条通路**：控制解复用（AXI-Lite `addr[12]` vs CCI-P 字地址 `address[10]`，切分点都是字节 `0x1000`）、bank-0 共享（`VX_mm_axi_arb` vs `VX_mem_arb`）、主机内存（专用 `m_axi_host` vs CCI-P `HB_*` 状态机）、中断（接出 vs 未使用）。
- **主机后端是外壳的软件镜像**：`runtime/xrt` 与 `runtime/opae` 受同一份 `callbacks_t` 契约（5 个方法：`init`/`cp_reg_*`/`host_mem_*`）约束成纯传输 HAL，设备内存分配与 DMA 编排全在公共核心。
- **`host_mem_alloc` 返回两个地址**（主机指针 + CP 地址），因为主机内存被 CPU 与 CP 共享——这是 FPGA 后端与 simx 后端的关键差异。
- **XRT 是受支持的主路径**（Xilinx Alveo U50/U280/U55C），OPAE（Intel Arria 10/Strix 10 PAC）已废弃；上板前 `hw` 流程必须用 `xrt-smi configure --host-mem` 使能 host bank。

## 7. 下一步学习建议

- **u14-l2（综合流程与 PPA 分析）**：本讲只提到 `hw/syn/xilinx/xrt` 下的综合入口，下一讲深入 `hw/syn` 下各厂商（Altera/Xilinx/Synopsys/Yosys）的综合脚本组织与 PPA（性能/面积/功耗）报告，承接本讲的比特流生产环节。
- **u14-l3（扩展 Vortex：自定义 ISA 扩展）**：如果你想给 Vortex 加一个新的加速器，本讲的「外壳 = 平台胶水 + CP」模型是前提——新加速器在设备侧加 FuncUnit 后，FPGA 路径通常不需要改外壳（除非引入新的主机可见端口）。
- **重读 u11-l3（命令处理器与 KMU）**：本讲反复强调「CP 是唯一命令路径」，但 CP 内部的 launch 解包、CTA 派发细节都在 u11-l3。把 u11-l3 的 CMD_LAUNCH 三段握手与本讲的「主机写 CP regfile + 发 CMD_LAUNCH」对照，能形成完整闭环。
- **重读 u3-l3（stub 动态分发）**：本讲的 XRT/OPAE 后端就是 u3-l3 五后端里的两个；把 u3-l3 的 `callbacks_t` 分发表与本讲的 5 个方法实现对照，能彻底理解「后端只是传输 HAL」的工程意义。
