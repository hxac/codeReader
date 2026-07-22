# KV260 MPSoC 与 DPU 硬件架构

## 1. 本讲目标

本讲是第五单元「FPGA 硬件平台构建」的首篇，对应端到端工作流（见 u1-l3）的**硬件/固件部署阶段**的底层认知。读完本讲，你应当能够：

1. 读懂 Kria KV260 的 **PS + PL 协同架构**：为什么一颗芯片里既有 ARM CPU 又有 FPGA，二者如何分工、如何通信。
2. 读懂核心加速 IP **DPU 4.1（DPUCZDX8G）** 的关键参数：架构名 `DPUCZDX8G_ISA1_B4096`、325 MHz 工作频率、softmax 引擎使能，并能把这些参数对应到 Vivado 工程文件与板载 `xdputil query` 输出。
3. 读懂 Vivado 综合后给出的 **FPGA 资源利用率表**（LUT / Register / BRAM / URAM），并据此评估「这个设计还能不能再塞一个 PL 加速核」。

本讲不教你怎么点击 Vivado（那是 u5-l2 的事），而是先把「这块板子到底长什么样、DPU 是什么、资源被吃了多少」这三件事讲透，为后续 Vivado 硬件设计、PetaLinux 镜像、固件部署打下地基。

## 2. 前置知识

在进入源码前，先用通俗语言解释几个对初学者可能陌生的术语。

- **SOM（System-on-Module）**：把一颗主芯片（这里的 FPGA SoC）连同内存、电源、闪存等焊在一块小板上，再通过接插件插到一块「载板（carrier）」上。KV260 就是这种形态，好处是核心板可批量复用、载板按需定制。
- **MPSoC（Multi-Processor SoC）**：Xilinx Zynq UltraScale+ 系列把「ARM 处理器系统」和「FPGA 可编程逻辑」集成在同一颗芯片里。处理器那半边叫 **PS（Processing System）**，FPGA 那半边叫 **PL（Programmable Logic）**。
- **PS**：一块硬核的 ARM 子系统（KV260 上是 4 核 Cortex-A53），运行 Linux，负责调度、文件 I/O、跑你写的 C++ 推理程序。
- **PL**：FPGA 真正的「可编程门阵列」区域，神经网络推理真正的算力来源——DPU IP 就放在这里。
- **IP（Intellectual Property core）**：可复用的硬件功能模块，相当于硬件世界的「库」。DPU 就是一个由 AMD/Xilinx 提供的 IP。
- **AXI**：ARM 定义的一种片内总线协议，PS 和 PL 之间、PL 内各 IP 之间都靠它传数据。
- **DPU（Deep Learning Processor Unit）**：Xilinx 的神经网络推理加速 IP，专门针对 int8 定点算子做了硬件优化。本项目用的是 **DPUCZDX8G** 这一款（面向 Zynq UltraScale+ 系列）。
- **LUT / Register / BRAM / URAM**：FPGA 的四种核心「资源货币」：
  - **LUT（查找表）+ 寄存器**：构成组合逻辑与时序逻辑，相当于「逻辑门预算」，最直观反映设计复杂度。
  - **BRAM（Block RAM）**：FPGA 内嵌的专用存储块（KV260 上单块 36Kb），用来做片上缓存、FIFO。
  - **URAM（Ultra RAM）**：比 BRAM 更大更密集的高容量存储块，DPU 用它来堆算子所需的大 buffer。

一个直觉类比：PS 是「经理」（跑 Linux、发指令），PL 是「车间」（FPGA 逻辑 + DPU 干重活），AXI 是连接经理和车间的「内部电话线」，BRAM/URAM 是车间里的「货架」，LUT/寄存器是「工位」。本讲的资源表就是在回答：**这间车间现在有多满？还能再加一台机器吗？**

## 3. 本讲源码地图

本讲主要围绕 `platform/kv260/` 目录，重点读取以下文件：

| 文件 | 作用 |
| :--- | :--- |
| [platform/kv260/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md) | KV260 Vitis AI 自定义构建总指南：硬件设计、PetaLinux、固件、部署四阶段；本讲重点读其中的**资源利用率表**与 `xdputil query` 输出。 |
| [platform/kv260/hw/dpu_kv260.tcl](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/dpu_kv260.tcl) | Vivado Block Design 的 TCL 脚本，里面实例化了 DPU IP、PS、时钟向导。本讲用它确认 DPU 版本、softmax、URAM 用量、325 MHz 时钟等参数。 |
| [assets/Vivado_KV260_DPU_TRD.png](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/assets/Vivado_KV260_DPU_TRD.png) | Vivado 中 KV260 DPU 设计的 Block Diagram 截图，直观看 PS↔PL↔DPU 拓扑。 |

> 备注：`README.md` 还会引用 `main.tcl`、`.xdc` 约束、`helper_build_bsp.sh`、`shell.json` 等文件，但它们属于 u5-l2~u5-l4 的内容，本讲只在必要处顺带提及。

## 4. 核心概念与源码讲解

### 4.1 KV260 PS/PL 协同架构

#### 4.1.1 概念说明

KV260 SOM 上那颗芯片是一颗 Zynq UltraScale+ MPSoC，它物理上被切成两半：

- **PS（Processing System）**：一块固定的、不可重配的 ARM 硬核子系统。KV260 上是 4 核 Cortex-A53（在本工程 TCL 中配置为约 1333 MHz）外加 R5 实时核、Mali GPU。PS 负责启动 Linux、运行 u7 单元的 C++ 推理程序、管理文件与网络。
- **PL（Programmable Logic）**：FPGA 可编程逻辑阵列。本项目的真正算力——DPU 4.1 IP——就被综合、布局布线进这片区域。

两半之间通过 **AXI 总线**互联，且共享挂在外面 DDR 上的物理内存。这就形成了本项目最关键的工作分工：

- PS 上的 C++ 程序（见 u7）把待推理图像写到 DDR；
- PS 通过 AXI 控制（给寄存器）下达「跑一次推理」的指令；
- PL 里的 DPU 读 DDR 取输入、跑 int8 卷积、把结果写回 DDR；
- PS 再从 DDR 取结果做后处理。

这套「**PS 下指令 + PL 干重活 + 共享 DDR**」的模型，正是 u1-l1 所说的「<10W 功耗红线」能够成立的基础：推理这一最耗能的环节被卸载（offload）到了高度定制、只做 int8 神经网络算子的 PL 硬件上，而不是用通用 ARM 核心去软算。

#### 4.1.2 核心流程

PS↔PL 协同的一次推理，大致是：

1. **启动**：PS 上电、加载 PetaLinux 镜像，内核里启用了 DPU 驱动（见 u5-l3）。
2. **加载固件**：用 `xmutil loadapp` 把 PL 比特流（`.bit.bin`）连同设备树 overlay 加载进 PL，DPU IP 此刻在 PL 里「成形」。
3. **下发数据**：PS 程序把输入张量写进 DDR。
4. **触发推理**：PS 通过 AXI-lite 写 DPU 控制寄存器，触发一次计算。
5. **PL 计算**：DPU 经 AXI 高性能（HP）端口直读 DDR，执行 int8 算子，结果回写 DDR。
6. **回收结果**：PS 程序读 DDR 取输出，做 NMS 等后处理（见 u6、u7）。

整个过程里 PS 与 PL **共享 DDR**，因此 PS 程序「写入」和 DPU「读取」访问的是同一段物理内存，省去了数据搬移。

#### 4.1.3 源码精读

README 里那张 Block Diagram 截图直观展示了这套拓扑：

[platform/kv260/README.md:29-L30](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L29-L30)（引用 `assets/Vivado_KV260_DPU_TRD.png`，给出 Vivado 中 KV260 DPU 设计的框图：左侧 Zynq PS、中间 AXI 互联、右侧 DPU 与时钟。）

底层实现来自 Vivado Block Design 脚本。在 `dpu_kv260.tcl` 中可以看到它用到的全部 IP 类型，其中 PS 与 DPU 是两块核心：

[platform/kv260/hw/dpu_kv260.tcl:132-L139](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/dpu_kv260.tcl#L132-L139)（`list_check_ips` 列出设计所需 IP：`zynq_ultra_ps_e` 就是 PS，`dpuczdx8g` 就是 DPU，其余 `clk_wiz`/`proc_sys_reset`/`xlconcat`/`xlslice` 是时钟与复位、中断拼接等辅助逻辑。）

PS 的主频由 `ACPU_CTRL` 频率参数设定，佐证了「PS 是 ARM A53」这一分工：

[platform/kv260/hw/dpu_kv260.tcl:556-L557](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/dpu_kv260.tcl#L556-L557)（`PSU__CRF_APB__ACPU_CTRL__FREQMHZ {1333.333}`，即 ARM A53 主频约 1.33 GHz——注意这是 PS 的频率，不是 DPU 的频率，二者独立。）

#### 4.1.4 代码实践

**实践目标**：把「PS 跑 Linux、PL 跑 DPU、二者共享 DDR」这套抽象对应到真实脚本里的具体 IP。

**操作步骤**：

1. 打开 `platform/kv260/hw/dpu_kv260.tcl`，定位到第 132–139 行的 IP 检查清单。
2. 在文件中分别搜索 `zynq_ultra_ps_e`、`dpuczdx8g`、`clk_wiz`，确认这三块 IP 各自的实例化位置。
3. 打开 `assets/Vivado_KV260_DPU_TRD.png`，对照框图找到 PS（Zynq UltraScale+ 块）与 DPU 块之间的连线方向（谁连到谁、是控制口还是数据口）。

**需要观察的现象**：

- `zynq_ultra_ps_e`（PS）会有 **M_AXI_GP**（通用主口，发控制指令）和 **HP/S_AXI_HP**（高性能从口，让 PL 直读 DDR）两类端口；
- `dpuczdx8g`（DPU）的 `S_AXI`（从口，接指令）连到 PS 的 GP 口，它的 `M_AXI`（主口，访存）连到 PS 的 HP 口。

**预期结果**：你会看到一条「PS 经 GP 口下指令 → DPU 经 HP 口直访 DDR 取/存数据」的闭环，正是上一节流程图描述的硬件基础。

> 由于本仓库不含 Vivado 工程，无法在本机直接打开 Block Design，以上为「源码阅读型实践」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 PS 不直接用 ARM 核心软算 YOLOv8，而非要把推理卸载到 PL 的 DPU？

> **参考答案**：ARM A53 是通用核，软跑 int8/float 卷积吞吐远低于专用硬件；而 DPU 是为 int8 神经网络算子量身定制的流水线，单位功耗下的算力高得多。在卫星 <10W 红线下，只有把最耗算力的推理交给专用 PL 硬件，才能在功耗预算内拿到可用的帧率。

**练习 2**：PS 和 PL 共享 DDR 带来了什么好处？

> **参考答案**：PS 程序写入的输入张量与 DPU 读取的是同一段物理内存，无需在 PS↔PL 之间显式搬运大块数据，降低延迟与功耗；后处理也可以原地读取 DPU 的输出。

---

### 4.2 DPU 4.1 IP 核心参数

#### 4.2.1 概念说明

**DPU（Deep Learning Processor Unit）** 是 Xilinx/AMD 提供的神经网络推理 IP 核。本项目使用的是 **DPUCZDX8G** 系列的 **4.1 版本**，其完整架构标识在板载运行时显示为：

```
DPUCZDX8G_ISA1_B4096
```

这个名字不是随意起的，每个字段都对应一项关键能力：

- **DPUCZDX8G**：面向 **Z**ynq UltraScale+ 系列（CZ）的 DPU 变体；`8` 指其指令集面向 v8/v8.2 ISA 家族，`G` 表示它跑在 Zynq MPSoC 的 PL 上（区别于 Alveo/Versal 上的其它 DPU 型号）。
- **ISA1**：Instruction Set Architecture 版本 1，即 DPU 支持的算子指令集代际。这决定了哪些算子能被原生加速——这也是 u4-l3 里要把 `sigmoid` 换成 `hsigmoid`、`silu` 换成 `hswish` 的根本原因：原生 ISA1 不含 `exp` 这类非线性指令，必须替换成 DPU 友好的分段线性近似。
- **B4096**：表征 DPU 的**峰值算力（每周期操作数）**，是这款 IP 的吞吐档位标识。`4096` 是 DPUCZDX8G 中较高的档位，对应更大的并行度、更强的 MAC 阵列。数值越大，单周期可完成的 int8 乘加越多，理论峰值算力越高。

除架构名外，本项目 DPU 还有两个值得关注的参数：

- **工作频率 325 MHz**：DPU 在 PL 里跑的时钟频率。注意它远低于 PS 的 1.33 GHz——FPGA 设计频率受布线和逻辑深度限制，但 DPU 靠巨大的片上并行度（B4096）弥补了频率劣势。
- **softmax 引擎使能（SFM_ENA=1）**：DPU 内置了一个可选的硬件 softmax 加速单元，本工程开启了它（板载查询输出 `"enable softmax": "True"`）。

理论峰值算力可粗略估算为：

\[
\text{Peak (GOPs)} = \frac{\text{峰值 OP/周期}}{\text{周期耗时}} = 4096 \times 325\,\text{MHz} \approx 1.33\,\text{TOPS}
\]

（此处 1 OP 计为 1 次 int8 操作；精确系数依 Xilinx 文档口径略有差异，本式仅供量级直觉，**待本地以官方 datasheet 校准**。）

#### 4.2.2 核心流程

DPU IP 在工程中的「定义 → 配置 → 时钟 → 验证」流程：

1. **声明 IP**：在 TCL 的 IP 检查清单中写明依赖 `xilinx.com:ip:dpuczdx8g:4.1`。
2. **实例化并配置**：用 `create_bd_cell` 创建 DPU 实例，再用 `set_property -dict` 批量设置参数——本工程设置了 `SFM_ENA`（softmax）、`URAM_N_USER`（用户 URAM 用量）、`CLK_GATING_ENA`（时钟门控省电）等。
3. **供给时钟**：用一个 `clk_wiz`（时钟向导）IP 把板上参考时钟转换出 DPU 所需频率（本工程为 325 MHz）。
4. **板载验证**：部署后用 `xdputil query` 读回 DPU 真实运行参数（架构名、频率、softmax 状态），确认设计与预期一致。

#### 4.2.3 源码精读

DPU IP 的版本与 softmax、URAM 用量在实例化处一目了然：

[platform/kv260/hw/dpu_kv260.tcl:1265-L1277](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/dpu_kv260.tcl#L1265-L1277)（创建 `dpuczdx8g:4.1` 实例并设置属性：`SFM_ENA {1}` 开启 softmax 引擎、`URAM_N_USER {40}` 指定 DPU 使用 40 块 URAM、`CLK_GATING_ENA {1}` 开启时钟门控省电。）

> 注意 `URAM_N_USER {40}` 与下一节资源表里 **URAM Used = 40 / 64** 完全吻合——这 40 块 URAM 几乎全部是 DPU 消耗的，是 DPU 占用资源的大头之一。

325 MHz 的 DPU 时钟由时钟向导 `dpu_clk_wiz` 产出：

[platform/kv260/hw/dpu_kv260.tcl:1290-L1295](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/dpu_kv260.tcl#L1290-L1295)（`clk_wiz` 的 `CLKOUT2_REQUESTED_OUT_FREQ {325}`——这就是 DPU 的工作时钟 325 MHz；`CLKOUT3` 的 100 MHz 一般用作 XRT/APB 控制时钟。）

部署到板子上之后，这些参数用 `xdputil query` 一眼可验。README 给出的真实输出：

[platform/kv260/README.md:240-L265](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L240-L265)（`xdputil query` 的 JSON 输出：`DPU IP Spec` 里 IP 版本 `v4.1.0`、`enable softmax: True`；`kernels[0]` 里 `DPU Arch: DPUCZDX8G_ISA1_B4096`、`DPU Frequency (MHz): 325`、`XRT Frequency (MHz): 100`、`cu_idx: 0`、`fingerprint` 是该 DPU 的唯一指纹。）

把三处串起来看，就形成了一个**「TCL 配置 → 硬件实现 → 板载回读」的闭环自洽**：脚本写 `SFM_ENA {1}` 和 325 MHz，板子查出来就是 `enable softmax: True` 和 `325 MHz`——这正是工程严谨性的体现，也是你日后排查「为什么 DPU 行为不对」时的核对清单。

#### 4.2.4 代码实践

**实践目标**：建立「DPU 参数三处一致」的核对能力。

**操作步骤**：

1. 在 `dpu_kv260.tcl` 第 1265–1277 行读出 DPU 的 `IP 版本`、`softmax`、`URAM 用量`。
2. 在第 1290–1295 行读出 DPU 时钟频率。
3. 对照 README 第 240–265 行 `xdputil query` 的 JSON，逐字段填表：

| 参数 | TCL 中的值 | `xdputil query` 中的值 | 是否一致 |
| :--- | :--- | :--- | :--- |
| IP 版本 | `dpuczdx8g:4.1` | `v4.1.0` | ✅ |
| softmax | `SFM_ENA {1}` | `enable softmax: True` | ✅ |
| DPU 频率 | `CLKOUT2 = 325` | `325 MHz` | ✅ |
| URAM 用量 | `URAM_N_USER {40}` | （资源表 40/64） | ✅ |

**需要观察的现象**：四项参数在「设计时」与「运行时」完全对得上。

**预期结果**：理解到 `xdputil query` 不是凭空冒出来的数字，而是 TCL 配置经过综合→实现→部署后在硬件上的真实回显；日后若发现频率不是 325 或 softmax 为 False，应回到 `dpu_kv260.tcl` 排查。

#### 4.2.5 小练习与答案

**练习 1**：架构名里的 `B4096` 反映了 DPU 的什么属性？它和 325 MHz 的关系是什么？

> **参考答案**：`B4096` 表征 DPU 每周期能完成的峰值操作数（并行度/MAC 吞吐档位）。DPU 的峰值算力 ≈ 峰值 OP/周期 × 频率，因此 `B4096` 与 325 MHz 共同决定峰值 TOPS；`B4096` 是「宽度」，325 MHz 是「速度」，二者相乘得吞吐。

**练习 2**：为什么本项目要在 `dpu_kv260.tcl` 里把 `SFM_ENA` 设为 1（开启 softmax）？

> **参考答案**：DPU 内置硬件 softmax 单元可在 PL 里高效计算 softmax，比用 ARM 软算或拆成多个 int8 算子更快、更省功耗；开启后训练/量化阶段依赖的 softmax 类运算在硬件上有原生加速，保证推理链路一致性。

---

### 4.3 FPGA 资源利用率解读

#### 4.3.1 概念说明

一个 FPGA 设计在 Vivado 里走完「综合 → 实现（布局布线）」之后，工具会产出一张**资源利用率表（Utilization Report）**，告诉你这个设计用了多少 LUT、寄存器、BRAM、URAM，各占芯片总量的百分之几。这张表是硬件工程师判断「设计能不能放下、还有多少余量」的核心依据。

本项目 README 把这张表折叠存放在了 `FPGA Resource Utilization` 详情块里。读懂它，需要先理解几条规则：

1. **每类资源都有 `Used / Available / Util%`**：用了多少、芯片上一共多少、占比多少。
2. **不同资源的稀缺程度不同**：KV260（ZU5EV 级别）上 LUT/寄存器相对充裕，而 **BRAM 与 URAM 总量有限**，往往是更早被吃满的「瓶颈资源」。
3. **余量要分类看**：LUT 余量大不代表 BRAM 余量大。评估「能不能再加一个 PL 加速核」，必须看目标核最依赖的那类资源。
4. **DPU 是资源大户**：DPU 是个高度并行的深度学习 IP，对 BRAM（权重/激活缓存）和 URAM（大 buffer）消耗很大，这正是资源表里这两项占比偏高的原因。

#### 4.3.2 核心流程

评估「设计还有没有空间加一个新 PL 核」的通用流程：

1. **读表**：列出关心的资源（LUT、BRAM、URAM）的 `Used / Available / Util%`。
2. **算余量**：

\[
\text{剩余量} = \text{Available} - \text{Used}, \qquad \text{剩余比例} = 1 - \frac{\text{Used}}{\text{Available}}
\]

3. **找瓶颈**：在关心的资源里找出**剩余比例最小**的那一类，它就是新核能否放入的「决定性约束」。
4. **估新核**：根据新核的综合报告（或经验估算）它最吃哪类资源，与瓶颈资源对比，给出「放得下 / 放不下 / 需实测」的结论。

#### 4.3.3 源码精读

README 中给出的资源表（综合实现后的真实数据）如下：

[platform/kv260/README.md:43-L62](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L43-L62)（FPGA 资源利用率表，本项目 DPU 设计实现后的占用情况。）

为便于讲解，把最关键的几行抽取成下表：

| 资源 | Used | Available | Util% |
| :--- | ---: | ---: | ---: |
| CLB LUTs | 62104 | 117120 | **53.03%** |
| CLB Registers | 106965 | 234240 | 45.66% |
| Block RAM Tile | 109 | 144 | **75.69%** |
| URAM | 40 | 64 | **62.50%** |

几点可直接读出的结论：

- **LUT / 寄存器比较宽裕**：LUT 用了 53.03%，寄存器仅 45.66%，逻辑资源还有近一半余量。
- **BRAM 是最紧的**：109 / 144 = 75.69%，是四类资源里占比最高的，剩余只有 24.31%。
- **URAM 全部给了 DPU**：40 / 64 = 62.50%，且这 40 块与 `URAM_N_USER {40}` 完全对应——URAM 几乎被 DPU 独占。

把余量算清楚（见 4.3.4 实践）：BRAM 剩 35 块（24.31%），URAM 剩 24 块（37.5%），LUT 剩约 47%。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：依据资源表，判断本设计是否还能容纳第八单元（u8）的 HLS 后处理解码核。

**操作步骤**：

1. 打开 README 第 43–62 行的资源表，记录 BRAM 与 URAM 的 `Used / Available`。
2. 计算两类资源的**绝对剩余量**与**剩余比例**：

   - BRAM：\(144 - 109 = 35\) 块；\(1 - 109/144 = 1 - 0.7569 = 0.2431\)，即 **24.31%**。
   - URAM：\(64 - 40 = 24\) 块；\(1 - 40/64 = 1 - 0.625 = 0.375\)，即 **37.5%**。

3. 找出瓶颈资源：四类资源中 BRAM 余量最小（24.31%），URAM 次之（37.5%），LUT/寄存器最宽裕（>45%）。
4. 结合 u8 解码核特性做判断（见下方「分析」）。

**需要观察的现象**：

- BRAM 与 URAM 的占比（75.69%、62.5%）明显高于 LUT（53.03%）——典型的「存大算子（DPU）吃存储资源」特征。
- URAM 的 Used=40 与 TCL 中 `URAM_N_USER {40}` 逐位对应，说明 URAM 几乎全归 DPU。

**预期结果 / 分析结论**：

> **能否再加一个 PL 加速核？答案是「大概率能，但 BRAM 是要盯紧的瓶颈」。**
>
> 判断依据：
>
> - **LUT/寄存器**：还剩约 47% / 54%，逻辑资源足够容纳一个 HLS 解码核（u8 的核以 `ap_uint<64>` 流式处理为主，靠 `UNROLL`/`PIPELINE` 用逻辑换吞吐，对 LUT 友好）。
> - **URAM**：剩 24 块（37.5%）。u8 解码核主要在片上做 int8 logit 的位运算与 softmax 加权，**不依赖大容量 URAM buffer**，因此 URAM 不是它的瓶颈。
> - **BRAM**：只剩 35 块（24.31%），是四类资源里最紧张的。u8 核若需要为输入/输出做若干行缓存或 FIFO，就要消耗 BRAM；能否放下，取决于该核综合后的 BRAM 占用。
>
> 因此结论是：**逻辑与 URAM 余量充足，主要风险在 BRAM**。是否真正放得下，需在 u8 把 HLS 核综合后看它的 BRAM 报告，与这剩余的 35 块比对；若超了，可考虑减小核内缓存深度或降低并行度来省 BRAM。这也正是本项目把后处理核设计成「轻量、流式、计算为主」的工程动机。

> 由于本仓库不提供可在本机运行的 Vivado 工程，上述 BRAM/URAM 数字基于 README 静态资源表，「能否加核」属基于已有数据的推理结论，最终需以实际综合报告为准（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：资源表里 LUT 占 53.03%、BRAM 占 75.69%。如果再塞一个核，应该优先担心哪类资源？为什么？

> **参考答案**：优先担心 **BRAM**。BRAM 余量（24.31%）远小于 LUT 余量（约 47%），是瓶颈资源；新增核能否放下，往往由最紧张的那类资源决定，而非最宽裕的。

**练习 2**：URAM 的 Used=40，与 TCL 中哪个参数一一对应？这说明 URAM 主要被谁占用？

> **参考答案**：与 `URAM_N_USER {40}` 一一对应，说明 URAM 几乎全部被 DPU 占用（DPU 用 URAM 实现大容量片上 buffer）。若新核也要用 URAM，只能竞争剩余的 24 块。

**练习 3**：为什么不能只看 LUT 余量就断言「还能加核」？

> **参考答案**：FPGA 有多类异构资源（LUT/寄存器/BRAM/URAM/DSP），不同设计对它们的消耗结构差异巨大。DPU 这类设计「吃存储甚于吃逻辑」，仅看 LUT 会高估余量；必须同时核对 BRAM/URAM，取最紧者作为约束。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「**从脚本配置到板载验证到资源评估**」的完整阅读：

**任务**：你刚拿到一块新烧好固件的 KV260，需要确认 DPU 是否如设计预期，并判断平台还有没有余量承接 u8 的后处理核。

**步骤**：

1. **核对设计意图**：打开 `platform/kv260/hw/dpu_kv260.tcl`，记录 DPU 的 IP 版本、`SFM_ENA`、`URAM_N_USER`、DPU 时钟频率四项（参考 4.2.3 的行号）。
2. **板载回读**：设想在板子上执行 `xdputil query`（命令见 [platform/kv260/README.md:240-L241](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L240-L241)），把输出的 `DPU Arch`、`DPU Frequency`、`enable softmax` 与第 1 步逐一对照，确认四项全部一致。
3. **资源体检**：打开资源表（[platform/kv260/README.md:43-L62](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L43-L62)），计算 BRAM、URAM、LUT 三类资源的剩余比例，找出瓶颈（应为 BRAM）。
4. **给出工程判断**：写一段结论——平台逻辑与 URAM 余量充足，BRAM 是瓶颈；u8 后处理核因流式轻量、不抢 URAM，大概率可放入，但最终需以该核综合后的 BRAM 报告与剩余 35 块比对为准。

**交付物**：一张「设计参数 vs 板载回读」对照表 + 一段「能否加 PL 核」的资源论证（不超过 200 字）。

## 6. 本讲小结

- KV260 的核心是一颗 **Zynq UltraScale+ MPSoC**，物理上分为 **PS（ARM A53，跑 Linux 与 C++ 推理程序）** 与 **PL（FPGA，承载 DPU）**，二者经 AXI 互联、共享 DDR。
- 神经网络推理的算力来自 PL 里的 **DPU 4.1（DPUCZDX8G）**，板载架构名为 **`DPUCZDX8G_ISA1_B4096`**，工作频率 **325 MHz**，开启了 **softmax 引擎（SFM_ENA=1）**。
- DPU 的关键参数在「TCL 配置 → 资源表 → `xdputil query`」三处自洽闭环：脚本写 `SFM_ENA {1}` / `URAM_N_USER {40}` / 325 MHz，资源表 URAM=40/64，板载回读 softmax=True / 325MHz。
- 资源利用率方面：**LUT 53.03%、寄存器 45.66% 较宽裕；BRAM 75.69% 最紧（剩 35 块/24.31%）；URAM 62.5%（剩 24 块/37.5%）几乎全归 DPU**。
- **BRAM 是当前设计的瓶颈资源**，是评估「能否再加 PL 加速核（如 u8 解码核）」时的决定性约束。
- 这些硬件事实（DPU 是 int8 定点加速器、ISA1 不含非线性指令、资源有限）正是后续 u4 量化、u6/u7 推理、u8 HLS 加速所有工程决策的物理根源。

## 7. 下一步学习建议

本讲建立了「板子与 DPU 长什么样、资源有多满」的硬件底座。接下来按工程链路：

- **u5-l2 Vivado 硬件设计与 XSA 导出**：本讲的 TCL 是怎么变成一个完整 Vivado 工程、再导出 `.xsa` 交接给软件团队的——讲 `main.tcl` 批处理构建与 `.xdc` 约束。
- **u5-l3 PetaLinux 软件镜像构建**：PS 侧的 Linux 镜像怎么生成、怎么在内核里把 DPU 驱动打开。
- **u5-l4 固件制作与板载部署**：本讲读到的 `xdputil query` 之前，那套「`.bit.bin` + `.dtbo` + `shell.json`」固件三件套是怎么做出来、怎么 `xmutil loadapp` 上板的。
- **横向联系 u4-l4**：DPU 架构名 `DPUCZDX8G_ISA1_B4096` 决定了量化模型编译时必须用与之匹配的 `arch.json`——回到 u4-l4 看「阶段 4 与阶段 5 唯一硬件耦合点」如何落地。
- **横向联系 u8**：本讲判断「BRAM 是瓶颈、u8 解码核大概率放得下」，可在学完 u8 HLS 核综合报告后回来验证这一结论。

建议在进入 u5-l2 之前，先重读本讲的资源表与 `xdputil query` 两段，确保你对「这块板子被吃掉了多少、还剩多少」有清晰直觉，因为后续每一步都会不断回到这张资源账本上。
