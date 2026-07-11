# 仓库结构与源码导航

## 1. 本讲目标

上一讲（u1-l1）我们已经知道：Mayoiuta 是一个开源 NPU 项目，源码由「Verilog 硬件设计」和「Windows 内核驱动」两部分组成。但当你真正打开仓库时，仍会面对一连串问题：这些文件放在哪里？某个 `.v` 文件对应哪个硬件模块？驱动目录下的三个文件各自干什么？

本讲就要回答这些问题。读完本讲，你应当能够：

- 画出 Mayoiuta 仓库的目录树，并说出每个子目录的职责。
- 把仓库里的真实文件，对应到 README 里描述的 CU / MU / IN / CU 四类模块上。
- 理解 Verilog「文件名」与「顶层模块名（`module xxx`）」的对应关系，学会用模块名反查文件。
- 认识仓库的「地图原点」——顶层模块 `NPU_SOC`，并知道后续每一篇讲义都从它出发去深入各个子模块。

## 2. 前置知识

在进入目录之前，先建立三个最基本的直觉。如果你已经熟悉，可以跳过本节。

### 2.1 什么是 RTL 与 `.v` 文件

**RTL**（Register Transfer Level，寄存器传输级）是一种用代码来「描述硬件电路」的方式。我们用一种叫 **Verilog** 的硬件描述语言写出 `.v` 文本文件，再用专门的工具（综合器）把它「翻译」成真实的逻辑门电路。所以 `.v` 文件不是被 CPU 一行行执行的程序，而是一张**电路图纸**。

Verilog 里最核心的概念是 **module（模块）**。一个 `module` 描述一块有输入输出端口的电路，例如：

```verilog
module NPU_SOC #(parameter CORES = 4)(    // 模块名 NPU_SOC，带一个参数 CORES
    input wire clk,                        // 输入端口：时钟
    ...
);
```

一个 `.v` 文件里通常写一个或多个 `module`。**文件名和模块名往往相似但不完全相同**——这是本讲后面要重点练习的「命名约定」。

### 2.2 什么是设备驱动与 `.inf` 文件

硬件做好了，还要让操作系统能用。**设备驱动（device driver）** 就是夹在操作系统和硬件之间的一层软件。Mayoiuta 的 `driver/win32/` 目录下放的是 **Windows WDF（Windows Driver Framework）内核驱动**，用 C 语言写成。

其中 `.inf`（Information）文件是一种「安装信息清单」，告诉 Windows：当你在主板上插了一块某种 PCI 设备时，应该用哪个 `.sys` 驱动文件去驱动它。你可以把它理解成「硬件身份证 → 驱动程序」的对照表。

### 2.3 什么是「目录树」思维

阅读大型项目最重要的习惯，是先建立**空间感**：哪个文件该去哪个文件夹里找。Mayoiuta 的目录是按「功能域」分类的——同一类功能的硬件模块放在同一个子目录里。本讲的核心任务，就是帮你把这张分类地图印在脑子里。

> 名词速查：NPU、MAC、RTL、固定功能硬件、WDF 驱动、CU/MU/IN/CU 这些术语在 u1-l1 已建立，本讲不再重复，会直接使用。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲怎么用它 |
|---|---|---|
| [README.md](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/README.md) | 项目说明 | 读取官方的模块分类（CU/MU/IN/CU）与仓库现状 |
| [hardware/rtl/top/npu_soc.v](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/top/npu_soc.v) | 顶层 SoC 模块 | 作为「地图原点」精读，理解各子模块如何被串起来 |
| [driver/win32/setup.inf](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/setup.inf) | 驱动安装清单 | 认识驱动目录下文件的作用 |

> 说明：仓库的全部 14 个被跟踪文件都会在本讲中「点名」，但只有上面 3 个会被精读。其余文件的深入讲解留给后续讲义。

## 4. 核心概念与源码讲解

### 4.1 仓库全景：两棵源码树

#### 4.1.1 概念说明

打开 Mayoiuta 仓库根目录，你会看到它非常干净，只有两棵主要的源码树加两个说明文件：

```
Mayoiuta/
├── README.md            项目说明（在 u1-l1 已读）
├── LICENSE              Apache 2.0 许可证（在 u1-l1 已读）
├── hardware/            ① 硬件设计源码树（Verilog RTL）
│   └── rtl/
└── driver/              ② 软件驱动源码树（Windows 内核驱动）
    └── win32/
```

这两棵树对应了 NPU 落地的两个阶段：

- **`hardware/`** 描述「**芯片长什么样**」——是电路图纸。
- **`driver/`** 描述「**操作系统怎么和芯片对话**」——是控制软件。

一句话区分：hardware 是「造硬件」，driver 是「用硬件」。二者缺一不可：只有硬件没有驱动，Windows 认不出这块卡；只有驱动没有硬件，驱动无处下发命令。

#### 4.1.2 核心流程：拿到一个问题，该去哪里找文件

建立一个简单的查找直觉：

```
我想了解"芯片整体怎么组织"      →  hardware/rtl/top/
我想了解"芯片怎么算乘加/卷积"   →  hardware/rtl/core/
我想了解"芯片怎么存数据"        →  hardware/rtl/memory/
我想了解"芯片怎么和电脑通信"    →  driver/win32/
我想了解"项目是什么/怎么用"     →  README.md
```

#### 4.1.3 源码精读

README 里把 NPU 的架构划分为四类核心模块（[README.md:24-32](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/README.md#L24-L32)）：

- **Compute Unit (CU)**：执行卷积、矩阵乘等基本运算。
- **Memory Unit (MU)**：存放权重、激活值、中间结果。
- **Interconnect Network (IN)**：在 CU 与 MU 之间搬运数据。
- **Control Unit (CU)**：控制 NPU 整体运行。

注意 README 这里出现了**两个 CU**（Compute Unit 和 Control Unit 缩写相同），这是源文档里的一处歧义，后续我们用「计算单元」和「控制单元」来区分，避免混淆。

这四类是「逻辑分类」，而仓库的目录是「物理分类」。下一节我们会把它们一一对应起来。

#### 4.1.4 代码实践

> **实践目标**：用命令亲眼确认仓库的两棵树，而不是凭记忆。

操作步骤：

1. 在仓库根目录执行 `git ls-files`（列出所有被 git 跟踪的文件）。
2. 观察输出，统计以 `hardware/` 开头和以 `driver/` 开头的文件各有几个。

需要观察的现象：你会看到 14 行输出，其中 `hardware/` 下有 9 个 `.v` 文件，`driver/` 下有 3 个文件，外加 `README.md` 与 `LICENSE`。

预期结果：`9 (hardware) + 3 (driver) + 2 (README/LICENSE) = 14`。

> 「待本地验证」：如果你的工作区里还看到 `Mayoiuta-tutorial/` 等额外目录，那是本学习手册本身（不在 git 跟踪的项目源码内），不应计入。

#### 4.1.5 小练习与答案

**练习 1**：如果有人说「我去 `driver/` 目录改一行代码让卷积算得更快」，这句话对吗？

**参考答案**：不对。卷积运算在 `hardware/rtl/core/` 里描述，改的是电路；`driver/` 只负责软件通信与调度，改驱动改变不了卷积电路本身的算力。要加速卷积得改硬件 RTL。

---

### 4.2 RTL 目录的六大功能域

#### 4.2.1 概念说明

进入 `hardware/rtl/` 后，文件按**功能域**被分到 6 个子目录。这是整个仓库最关键的一张分类表：

```
hardware/rtl/
├── top/           顶层 SoC：把所有模块拼成一颗完整的芯片
│   └── npu_soc.v
├── core/          计算单元 (CU)：脉动阵列、卷积、多精度计算
│   ├── pe_array.v
│   ├── conv_engine.v
│   └── adaptive_pe.v
├── memory/        存储单元 (MU)：存储控制器、数据重排
│   ├── mem_ctl.v
│   └── data_reorder.v
├── power/         功耗与能效：动态电压频率调节 (DVFS)
│   └── dvfs_ctrl.v
├── sparse/        稀疏计算加速：跳过零值、压缩
│   └── sparse_engine.v
└── control/       控制与适配：动态形状适配
    └── shape_adaptor.v
```

#### 4.2.2 核心流程：README 的四类模块 → 仓库的六个目录

把上一节 README 的逻辑分类，和上面的物理目录对应起来：

| README 逻辑分类 | 含义 | 对应仓库目录 | 对应文件 |
|---|---|---|---|
| Compute Unit（计算单元） | 做运算 | `core/` | pe_array.v、conv_engine.v、adaptive_pe.v |
| Memory Unit（存储单元） | 存数据 | `memory/` | mem_ctl.v、data_reorder.v |
| Interconnect Network（互连） | 搬数据 | **无独立目录** | 由 `top/npu_soc.v` 内部的 `noc_data`/`noc_ctrl` 网线实现 |
| Control Unit（控制单元） | 控全局 | `control/` + `top/` | shape_adaptor.v；npu_soc.v 中例化的 npu_controller |

你会注意到两点：

1. **互连网络（IN）没有自己的目录**。它不是单独一块模块，而是顶层 `npu_soc.v` 里的一组连线（`noc_data`、`noc_ctrl`），把多个核串起来。这是 NPU 设计的常见做法——互连往往体现在顶层。
2. **多出了 `power/` 和 `sparse/` 两个目录**。它们在 README 的四类分类里没有直接出现，属于额外的「加速能力」与「能效管理」，分别在第 4 单元（高级加速与能效）细讲。

#### 4.2.3 源码精读

每个 `.v` 文件的第一行（或前几行）都会用 `module` 关键字声明它的顶层模块名。用一条 grep 命令就能把所有模块名捞出来：

```
hardware/rtl/top/npu_soc.v:1:     module NPU_SOC #(
hardware/rtl/core/pe_array.v:1:   module PE_Array #(
hardware/rtl/core/pe_array.v:44:  module Processing_Element #(
hardware/rtl/core/conv_engine.v:1:module Conv_Engine #(
hardware/rtl/core/adaptive_pe.v:1:module Adaptive_PE #(
hardware/rtl/memory/mem_ctl.v:1:  module Memory_Controller #(
hardware/rtl/memory/data_reorder.v:1:module Data_Reorder #(
hardware/rtl/power/dvfs_ctrl.v:1: module DVFS_Controller #(
hardware/rtl/sparse/sparse_engine.v:1:module Sparse_Engine #(
hardware/rtl/control/shape_adaptor.v:1:module Shape_Adaptor #(
```

（上面是 `grep -n "^\s*module" hardware/rtl/**/*.v` 的输出形式，属于示例命令的输出，不是项目自带脚本。）

这里有个**关键发现**：9 个 `.v` 文件里，一共声明了 **10 个模块**。原因是 [hardware/rtl/core/pe_array.v](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v) 一个文件里塞了两个模块：`PE_Array`（阵列，第 1 行）和 `Processing_Element`（单个处理单元，第 44 行）。这提醒我们：**文件数 ≠ 模块数**，数模块要以 `module` 关键字为准。

#### 4.2.4 代码实践

> **实践目标**：亲手确认每个文件的模块名，而不是相信本讲的表格。

操作步骤：

1. 在仓库根目录执行 `grep -rn "^module\|^ *module" hardware/rtl/`。
2. 把输出整理成「文件 → 模块名」的笔记。

需要观察的现象：你会得到和 4.2.3 节一致的 10 行模块声明。

预期结果：9 个 `.v` 文件，10 个模块；`pe_array.v` 贡献了两个模块。

#### 4.2.5 小练习与答案

**练习 1**：根据 4.2.2 的对应表，`sparse_engine.v` 属于 README 的哪一类模块？

**参考答案**：严格说，README 的四类（计算/存储/互连/控制）里没有「稀疏」这一类。`sparse_engine.v` 是一种**计算加速手段**（通过跳过零值来让计算单元更快），最接近「Compute Unit（计算单元）」。它体现了「仓库目录比 README 的概述更细」这一现实——这也是为什么我们要直接读源码。

**练习 2**：为什么互连网络（IN）没有独立目录？

**参考答案**：因为互连在本项目里不是一块独立的「功能模块」，而是顶层 `npu_soc.v` 中把多个核串成环网的连线（`noc_data` / `noc_ctrl`）。它「融入」了顶层设计，所以没有单独的文件夹。

---

### 4.3 顶层模块 NPU_SOC：仓库的「地图原点」（最小模块）

#### 4.3.1 概念说明

`NPU_SOC`（位于 [hardware/rtl/top/npu_soc.v](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/top/npu_soc.v)）是整个仓库的**顶层模块（top module）**。SoC 的意思是 System-on-Chip（片上系统），即把一整颗芯片的系统集成在一颗芯片里。

之所以把它叫做「地图原点」，是因为：

- 它**例化（instantiation）** 了项目里的几乎所有关键子模块——就像一张总图，把各零件拼到一起。
- 后续每一篇讲义深入某个子模块时，都可以回到 `NPU_SOC` 看它「在整体中处于什么位置」。

理解顶层模块，就理解了 Mayoiuta 的整体骨架。

#### 4.3.2 核心流程

`NPU_SOC` 做了三件事，把整颗芯片组织起来：

```
① 定义对外接口
   pcie_data（PCIe 进来的数据/配置）
   ddr_data（写到 DDR 的数据）
   interrupt（外部中断）
   status（状态回报）
        │
        ▼
② 例化主控制器 npu_controller
   接收全局配置、收集各核状态、响应中断
        │
        ▼
③ 用 generate-for 循环例化 CORES 个计算核 npu_core
   每个核的 noc_out 接到下一个核的 noc_in（环形 NoC 互连）
        │
        ▼
④ 例化性能监控 performance_monitor
   监测核活动、DDR 带宽占用、功耗状态
```

其中最巧妙的是第 ③ 步：用一条 `generate-for` 循环，配合取模运算 `(i+1)%CORES`，把多个核自动连成一个**环形网络（Ring NoC）**。这正是「互连网络（IN）没有独立目录」的原因——它就藏在顶层这几行循环里。

> **重要提醒（待确认）**：`NPU_SOC` 里例化的 `npu_controller`、`npu_core`、`performance_monitor` 三个子模块，在当前仓库里**找不到对应的 `.v` 源码文件**。也就是说，顶层目前只画出了「总图」，而这些被引用的零件暂未提供。我们后续把它们标注为「待确认 / 未提供」，绝不凭想象编造它们的内部实现。这不会影响我们理解顶层**如何组织**它们。

#### 4.3.3 源码精读

**(1) 模块声明与参数化多核** —— [hardware/rtl/top/npu_soc.v:1-12](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/top/npu_soc.v#L1-L12)

这段定义了顶层模块名 `NPU_SOC`、参数 `CORES = 4`（默认 4 核），以及对外的四个接口（PCIe 数据、DDR 数据、中断、状态）。

```verilog
module NPU_SOC #(
    parameter CORES = 4
)(
    input wire clk,
    input wire rst_n,
    input wire [127:0] pcie_data,
    output reg [127:0] ddr_data,
    input wire interrupt,
    output reg [31:0] status
);
```

`#(parameter CORES = 4)` 是 Verilog 的参数化写法，意味着「这颗芯片有几个核可以配置」。把 `CORES` 改大，下面的循环就会自动多生成几个核——这就是 README 说的「可扩展（Scalability）」在代码层面的体现。

**(2) 互连网络网线声明** —— [hardware/rtl/top/npu_soc.v:15-16](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/top/npu_soc.v#L15-L16)

```verilog
wire [127:0] noc_data [0:CORES-1];
wire [31:0] noc_ctrl [0:CORES-1];
```

这两行声明了两组**数组型连线**：`noc_data`（128 位数据）和 `noc_ctrl`（32 位控制），各有 `CORES` 条。它们就是「互连网络（IN）」的实体——不是某个目录里的模块，而是顶层里的两组网线。下标 `[0:CORES-1]` 表示按核编号。

**(3) 多核环形互连（generate-for）** —— [hardware/rtl/top/npu_soc.v:28-41](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/top/npu_soc.v#L28-L41)

这是顶层最精彩的部分：

```verilog
generate
for (genvar i = 0; i < CORES; i = i + 1) begin
    npu_core #(
        .CORE_ID(i)
    ) u_core (
        .clk(clk),
        .rst_n(rst_n),
        .noc_in(noc_data[i]),
        .noc_out(noc_data[(i+1)%CORES]),   // ← 环形：本核输出接到下一核输入
        .ctrl_in(noc_ctrl[i]),
        .ddr_interface(ddr_data)
    );
end
endgenerate
```

要点：

- `generate ... for` 是 Verilog 的**硬件生成循环**：它不是在运行时循环执行，而是在综合时把这段电路「展开」复制 `CORES` 份，硬件上真的会出现 `CORES` 个核。
- `.CORE_ID(i)` 给每个核打上编号 `0, 1, 2, 3`。
- `.noc_in(noc_data[i])` 和 `.noc_out(noc_data[(i+1)%CORES])`：第 `i` 个核的数据输出，接到第 `(i+1)%CORES` 个核的输入。`%` 是取模，让最后一个核（`i=CORES-1`）的输出绕回首核（`i=0`），形成一个**环**。

> 待确认：被例化的 `npu_core` 模块源码仓库未提供，所以我们目前只能从端口名推测它「吃一条 `noc_in`，吐一条 `noc_out`，还能访问 `ddr_interface`」，内部实现待后续提供。

**(4) 控制器与性能监控的例化** —— [hardware/rtl/top/npu_soc.v:19-25](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/top/npu_soc.v#L19-L25) 与 [hardware/rtl/top/npu_soc.v:44-49](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/top/npu_soc.v#L44-L49)

`npu_controller`（主控制器）接收 `pcie_data[95:0]` 作为全局配置、收集 `noc_ctrl` 作为各核状态、响应 `interrupt`；`performance_monitor`（性能监控）则观测核活动、DDR 带宽（`ddr_data[127:96]`）和功耗（`status[31:16]`）。两者源码同样**待确认（未提供）**。

#### 4.3.4 代码实践

> **实践目标**：把「`CORES` 参数」和「环形互连」从抽象变成具体的手算。

操作步骤：

1. 假设 `CORES = 4`（默认值），在纸上写下核编号 `0, 1, 2, 3`。
2. 对每个核 `i`，按 `.noc_out(noc_data[(i+1)%CORES])` 算出它的输出连到谁：
   - 核 0 → `(0+1)%4 = 1`
   - 核 1 → `(1+1)%4 = 2`
   - 核 2 → `(2+1)%4 = 3`
   - 核 3 → `(3+1)%4 = 0`
3. 画出这条连接：`0 → 1 → 2 → 3 → 0`，这就是环形 NoC 拓扑。

需要观察的现象：你会得到一个闭合的环，没有「尽头」，每个核都有且只有一个下游邻居。

预期结果：四核时是一条 4 节点的有向环。如果把 `CORES` 改成 3，环就变成 `0 → 1 → 2 → 0`——这印证了「改参数即可改规模」。

> 「待本地验证」：本仓库不含 Verilog 仿真脚手架（如 testbench、Makefile），无法直接跑仿真验证。本实践属于「源码阅读 + 手算」型。

#### 4.3.5 小练习与答案

**练习 1**：`generate-for` 和软件里的 `for` 循环最大的区别是什么？

**参考答案**：软件 `for` 是在运行时反复执行同一段代码；Verilog 的 `generate-for` 是在**综合时**把电路「复制展开」成多份硬件。`CORES=4` 不是循环 4 次，而是真的在芯片上生成 4 个独立的核。

**练习 2**：如果把 `(i+1)%CORES` 改成 `(i+1)`（去掉取模），当 `i=CORES-1` 时会发生什么？

**参考答案**：`i=CORES-1` 时 `(i+1) = CORES`，越出了 `noc_data[0:CORES-1]` 的合法下标范围，会综合失败或连到不存在的网线。取模的作用正是把最后一个核「绕回」首核，闭合环形。这正是 NoC「环」拓扑的关键。

---

### 4.4 驱动目录：三个文件

#### 4.4.1 概念说明

`driver/win32/` 目录只有 3 个文件，却覆盖了一个 Windows 内核驱动的标准三件套：

| 文件 | 类型 | 作用 |
|---|---|---|
| `npudriver.c` | C 源码（194 行） | 驱动的主体实现：入口函数、设备初始化、IOCTL 处理、DMA、中断 |
| `npudriver.h` | C 头文件（36 行） | 寄存器定义、IOCTL 命令码、设备上下文结构 |
| `setup.inf` | 安装信息（32 行） | 告诉 Windows「这块 PCI 卡用哪个驱动」 |

#### 4.4.2 源码精读：INF 如何把硬件绑到驱动

我们看最小的那个文件 [driver/win32/setup.inf](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/setup.inf)。其中最关键的一行是 [driver/win32/setup.inf:14](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/setup.inf#L14)：

```ini
%DeviceDesc%=DeviceInstall, PCI\VEN_1ACE&DEV_BEEF
```

这一行是「硬件身份证 → 安装节」的映射：

- `PCI\VEN_1ACE&DEV_BEEF`：厂商 ID（Vendor）是 `1ACE`，设备 ID（Device）是 `BEEF`。
- 当 Windows 在 PCI 总线上发现一块「厂商 1ACE、设备 BEEF」的卡时，就用 `DeviceInstall` 节来安装驱动。

巧的是，这两个 ID 正好出现在头文件里（[driver/win32/npudriver.h:6-7](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.h#L6-L7)）：

```c
#define NPU_DEVICE_ID 0xBEEF
#define NPU_VENDOR_ID 0x1ACE
```

这就是「软硬件接口」的最早一瞥：**驱动代码里的宏（`0xBEEF`/`0x1ACE`）必须和 INF 里的 PCI 字符串（`DEV_BEEF`/`VEN_1ACE`）一致**，否则 Windows 就不会把这块卡和这个驱动绑定。这部分会在第 5 单元（设备驱动与软硬件接口）详细展开。

> 提示：`npudriver.c`（194 行）是驱动里最大的文件，本讲只做「点名」，不展开。它的精读留给 u5-l1 / u5-l2。

#### 4.4.3 小练习与答案

**练习 1**：如果换了一块 PCI 卡，厂商变成 `10EE`、设备变成 `CAFE`，要改哪两处文件？

**参考答案**：要同时改两处并保持一致——`driver/win32/setup.inf` 里的 `PCI\VEN_xxxx&DEV_xxxx` 字符串，以及 `driver/win32/npudriver.h` 里的 `NPU_VENDOR_ID` / `NPU_DEVICE_ID` 宏。两边不一致就会绑不上。

---

## 5. 综合实践：制作「文件 → 模块 → 功能域」对照表

本讲最重要的产出，是一张贯穿全仓库的导航对照表。请你自己动手完成它。

**实践目标**：把本讲所有零散信息汇成一张可长期查阅的总表，作为后续每一篇讲义的「索引页」。

**操作步骤**：

1. 在仓库根目录运行 `git ls-files`，拿到全部文件清单。
2. 对每个 `.v` 文件，用 `grep -n "module" <文件>` 找到模块名（注意 `pe_array.v` 有两个模块）。
3. 根据文件所在子目录（top/core/memory/power/sparse/control），填入「功能域」。
4. 对 `driver/` 下文件，按 `.c` / `.h` / `.inf` 填入用途。
5. 整理成下面的表格形式。

**参考答案（你应该得到类似下面的表）**：

| 文件路径 | 顶层模块名 | 功能域 / 用途 |
|---|---|---|
| `hardware/rtl/top/npu_soc.v` | NPU_SOC | 顶层 SoC（地图原点） |
| `hardware/rtl/core/pe_array.v` | PE_Array、Processing_Element | 计算单元（脉动阵列） |
| `hardware/rtl/core/conv_engine.v` | Conv_Engine | 计算单元（卷积） |
| `hardware/rtl/core/adaptive_pe.v` | Adaptive_PE | 计算单元（多精度） |
| `hardware/rtl/memory/mem_ctl.v` | Memory_Controller | 存储单元（存储控制器） |
| `hardware/rtl/memory/data_reorder.v` | Data_Reorder | 存储单元（数据重排） |
| `hardware/rtl/power/dvfs_ctrl.v` | DVFS_Controller | 能效（动态电压频率调节） |
| `hardware/rtl/sparse/sparse_engine.v` | Sparse_Engine | 加速（稀疏计算） |
| `hardware/rtl/control/shape_adaptor.v` | Shape_Adaptor | 控制（动态形状适配） |
| `driver/win32/npudriver.c` | （C 函数，非 module） | 驱动主体实现 |
| `driver/win32/npudriver.h` | （C 头文件） | 寄存器 / IOCTL / 上下文定义 |
| `driver/win32/setup.inf` | （INF 配置） | 驱动安装清单（PCI 绑定） |

**需要观察的现象**：你会确认「9 个 `.v` 文件 → 10 个模块」这一不对称（因 `pe_array.v` 含双模块），并确认 `NPU_SOC` 里引用的 `npu_controller` / `npu_core` / `performance_monitor` 在表中**找不到**对应文件——它们属于「待确认（未提供）」。

**预期结果**：得到一张 12 行的对照表（9 个 RTL 文件 + 3 个驱动文件）。把它保存好，后续每读一篇讲义，就在对应行旁边补注「已掌握」。

> 「待本地验证」：表格中「模块名」列由源码静态分析得出，可直接用 grep 复现；不依赖运行环境。

## 6. 本讲小结

- Mayoiuta 仓库有**两棵源码树**：`hardware/rtl/`（Verilog 硬件电路）与 `driver/win32/`（Windows 内核驱动），外加 `README.md` 与 `LICENSE`。
- RTL 按**六大功能域**分目录：`top/`（顶层）、`core/`（计算）、`memory/`（存储）、`power/`（能效）、`sparse/`（稀疏加速）、`control/`（控制适配）。
- README 的四类模块（CU 计算 / MU 存储 / IN 互连 / CU 控制）与目录的对应关系里，**互连网络（IN）没有独立目录**，它由顶层 `npu_soc.v` 中的 `noc_data` / `noc_ctrl` 网线实现。
- **文件数 ≠ 模块数**：9 个 `.v` 文件包含 10 个模块，因为 `pe_array.v` 一个文件里有 `PE_Array` 和 `Processing_Element` 两个模块。
- 顶层模块 `NPU_SOC` 是仓库的「地图原点」：用参数 `CORES` 和 `generate-for` 循环例化多个核，靠 `(i+1)%CORES` 把它们连成**环形 NoC**。
- `NPU_SOC` 引用的 `npu_controller` / `npu_core` / `performance_monitor` 当前仓库**未提供源码**，统一标注为「待确认」，不臆造。

## 7. 下一步学习建议

现在你已经有了仓库的全局地图。建议下一步：

- **直接承接 → u1-l3「顶层 SoC 架构：NPU_SOC」**：本讲我们对 `NPU_SOC` 只做了「导航级」的精读，u1-l3 会更深入地拆解它的多核实例化、NoC 互连拓扑与对外接口，建议紧接着读。
- **横向阅读**：用本讲第 5 节的对照表，随便挑一个感兴趣的子目录（例如 `core/pe_array.v`），先扫一眼它的 `module` 声明和端口列表，建立「这块模块有哪些输入输出」的直觉，再进入第 2 单元（核心计算通路）的精读。
- **驱动方向**：如果你更关心软件侧，可以直接跳到第 5 单元（设备驱动与软硬件接口），但建议先确认你已理解「`NPU_SOC` 对外有 PCIe/DDR/中断接口」这一事实——驱动正是通过这些接口与硬件对话的。
