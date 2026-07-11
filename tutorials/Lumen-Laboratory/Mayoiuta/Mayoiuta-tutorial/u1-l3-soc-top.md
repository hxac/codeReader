# 顶层 SoC 架构：NPU_SOC

## 1. 本讲目标

上一讲（u1-l2）我们已经在仓库里「点名」过顶层模块 `NPU_SOC`，把它当作整张源码地图的原点。但当时只是远远地认了一下地标——知道它在 `hardware/rtl/top/npu_soc.v`，知道它通过 `CORES` 参数和 `generate-for` 例化了多个核。本讲我们要真正走进这座建筑的图纸，搞懂它的承重结构。

具体来说，读完本讲你应当能够：

- 说清 `NPU_SOC` 这个顶层模块**对外暴露的四个接口**（`pcie_data`、`ddr_data`、`interrupt`、`status`）各自是什么角色。
- 看懂 `generate-for` 如何用一个循环例化出 `CORES` 个计算核，以及为什么它是 Mayoiuta「可扩展」愿景的落地点。
- 画出 NoC（片上网络）在多核之间的**环形（ring）拓扑**，并能解释 `noc_data[(i+1)%CORES]` 这个取模写法如何把一条链首尾相接成环。
- 区分 `NPU_SOC` 引用的三个子模块（`npu_controller` / `npu_core` / `performance_monitor`），并清楚地指出：**它们的源码当前并不在仓库里**，属于「待确认」环节，不能臆造其内部行为。

> 本讲是后续所有 RTL 讲义的「骨架」。后面讲到 PE 阵列、卷积引擎、存储控制器时，你都要能回答：它挂在 `NPU_SOC` 的哪条线上、由谁调度。

## 2. 前置知识

在拆解顶层之前，先用三段话建立直觉。已经熟悉的读者可以跳到第 3 节。

### 2.1 什么是 SoC 与「顶层模块」

**SoC**（System on Chip，片上系统）就是把一整个「小电脑系统」——多个处理核、存储、互连、控制逻辑——全部塞进一颗芯片里。在 RTL 设计里，我们用一个**顶层模块（top module）**来代表整颗 SoC：它对外的端口就是芯片的「引脚」，它的内部则是各子模块的连线图。

打个比方：顶层模块像一张「整机装配图」。图上不画每个零件内部的齿轮，只画「零件 A 的输出端连到零件 B 的输入端」。本讲的 `NPU_SOC` 就是这张装配图。

### 2.2 什么是片上网络 NoC

当一颗芯片里有多个计算核时，核与核之间、核与存储之间需要交换数据。专门负责搬运数据的连线结构叫 **NoC（Network on Chip，片上网络）**。它和软件里的「网络」是类比关系：核是节点，连线是链路，数据像包一样在节点间流动。

Mayoiuta 用的是最简单的一种 NoC 拓扑——**环形（ring）**：把若干个核串成一条链，再把链的尾接回头，形成一个环。数据沿着环逐跳（hop）传递。它的好处是连线规则简单、容易随核数扩展；代价是远端两个核之间要绕环多跳。

> 名词速查：RTL、Verilog module、例化、顶层模块、`generate-for`、环形 NoC 这些在 u1-l1 / u1-l2 已建立，本讲直接使用。u1-l2 中对 `npu_soc.v` 各代码段的「地标式」导读，本讲不再复述，而是深入讲解其含义。

## 3. 本讲源码地图

本讲只精读一个文件，但会把它逐行拆透。

| 文件 | 作用 | 本讲怎么用它 |
|---|---|---|
| [hardware/rtl/top/npu_soc.v](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/top/npu_soc.v) | 顶层 SoC 模块 `NPU_SOC` | 全文精读：外部接口、NoC 网线、三个子模块的例化与互连 |

> 说明：`NPU_SOC` 引用的三个子模块 `npu_controller` / `npu_core` / `performance_monitor` 在**当前仓库中没有对应的 `.v` 文件**（可用 `git ls-files 'hardware/rtl/**/*.v'` 验证，全仓共 9 个 `.v` 文件，无一声明它们）。因此本讲只讲「`NPU_SOC` 如何连线到它们」，不讲「它们内部如何工作」——后者需标注「待确认」。

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：先看顶层 `NPU_SOC` 的整体外壳与对外接口，再依次看它内部挂载的三类子模块——主控制器 `npu_controller`、计算核阵列 `npu_core`（含 NoC 环形互连）、性能监控 `performance_monitor`。

### 4.1 顶层外壳：NPU_SOC 的模块声明与对外接口

#### 4.1.1 概念说明

`NPU_SOC` 是整颗芯片的「门面」。它对外只暴露极少的端口，目的是把内部复杂的结构封装起来，让外部（主板、主机）只需要关心四件事：

- 怎么把数据/配置**送进**芯片（输入）。
- 怎么把计算结果**取回**（输出）。
- 怎么用一根信号**打断**芯片当前工作（中断）。
- 怎么**读取芯片当前状态**（状态字）。

这四个端口分别对应 `pcie_data`、`ddr_data`、`interrupt`、`status`。理解了它们，就理解了这颗 NPU 和外部世界签订的「合同」。

#### 4.1.2 核心流程

从主机视角，一次最粗粒度的交互流程如下：

```text
主机 ──pcie_data(128位)──▶ NPU_SOC ──拆分/分发──▶ 内部各子模块
                                          │
主机 ◀──ddr_data(128位)─── NPU_SOC ◀──结果汇总─── 计算核阵列
主机 ──interrupt───────▶ NPU_SOC ──▶ npu_controller（处理中断）
主机 ◀──status(32位)──── NPU_SOC ◀──performance_monitor（上报状态）
```

需要注意一个关键事实：`NPU_SOC` 自己几乎**不做任何运算**，它只负责「接线」——把外部端口和内部子模块的端口连起来。真正的逻辑都在子模块里（而子模块的源码当前缺失，属待确认）。

#### 4.1.3 源码精读

模块声明与对外端口定义在 [hardware/rtl/top/npu_soc.v:1-12](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/top/npu_soc.v#L1-L12)：

```verilog
module NPU_SOC #(
    parameter CORES = 4                 // 计算核数量，默认 4，可由上层改写
)(
    input wire clk,                     // 全局时钟
    input wire rst_n,                   // 低有效复位（_n 表示 active-low）
    // 外部接口
    input wire [127:0] pcie_data,       // 主机经 PCIe 送来的 128 位数据/配置
    output reg [127:0] ddr_data,        // 写回 DDR 的 128 位结果
    // 控制信号
    input wire interrupt,               // 外部中断输入
    output reg [31:0] status            // 32 位状态字，上报给主机
);
```

逐行说明：

- `parameter CORES = 4`：这是整颗芯片「可扩展」的关键旋钮。把 `CORES` 改成 8，下面的 `generate` 循环就会例化 8 个核；改成 2 就只有 2 个核。这正是 README「可配置参数」愿景的落地处。
- `clk` / `rst_n`：所有时序电路的心跳与清零。`rst_n` 的 `_n` 后缀是硬件设计的常见约定，表示「低电平有效」——平时是 1，拉到 0 时才触发复位。
- `pcie_data [127:0]`：128 位宽的输入。名字暗示它来自 **PCIe**（主机与加速卡之间常用的高速总线）。后面会看到，只有低 96 位被传给主控制器作 `global_config`。
- `ddr_data [127:0]`：128 位宽的输出，声明为 `output reg`。名字暗示它去向 **DDR**（外部显存/主存）。注意所有计算核**共用**这一根线（见 4.3.3），这一点值得警惕。
- `interrupt`：单比特中断输入，由外部（或驱动）拉高来打断 NPU。
- `status [31:0]`：32 位状态字。后面会看到，只有高 16 位 `status[31:16]` 被性能监控驱动，低 16 位 `status[15:0]` 在本文件里**从未被赋值**（待确认，见 4.4）。

> 命名直觉：Mayoiuta 的端口命名很「语义化」——`pcie_` 前缀表示与 PCIe 总线相关，`ddr_` 表示与 DDR 内存相关。读懂前缀就能猜到信号去向。

#### 4.1.4 代码实践

**实践目标**：建立「端口 = 芯片对外合同」的直觉，并亲手发现一处「端口未被完整使用」的疑点。

**操作步骤**：

1. 打开 [hardware/rtl/top/npu_soc.v](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/top/npu_soc.v)。
2. 在第 1–12 行圈出四个外部端口：`pcie_data`、`ddr_data`、`interrupt`、`status`。
3. 用搜索功能在文件里分别查找 `status[`、`pcie_data[`、`ddr_data[`，统计每个端口被「读取」和「被驱动（赋值）」的次数。

**需要观察的现象**：

- `pcie_data` 只在 `global_config(pcie_data[95:0])` 处被使用，即只取了低 96 位，高 32 位 `pcie_data[127:96]` 未见使用。
- `ddr_data` 既被核阵列当作输出接口（`.ddr_interface(ddr_data)`），又被性能监控当作输入采样（`.ddr_usage(ddr_data[127:96])`）。
- `status` 只在 `.power_status(status[31:16])` 处被驱动高 16 位。

**预期结果**：你会得出一张「端口使用情况表」，其中至少有 `status[15:0]` 这一段在 `NPU_SOC` 内部**从未被赋值**——它综合后会悬空或保持默认值。这是真实存在的「待确认」疑点，不是 bug 传闻。

**待本地验证**：以上为源码阅读结论；若要确认综合后 `status[15:0]` 的实际电平，需要引入仿真/综合工具，而仓库未提供此类脚手架，故标注待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `CORES` 参数从 4 改成 8，`NPU_SOC` 对外的端口数量会变多吗？为什么？

**参考答案**：不会。`CORES` 只影响**内部**例化的核数量（由 `generate` 循环决定），而对外端口 `clk/rst_n/pcie_data/ddr_data/interrupt/status` 是写死在模块声明里的，与 `CORES` 无关。这正是参数化设计的好处：对外接口稳定，内部可伸缩。

**练习 2**：`rst_n` 中的 `_n` 表示什么？如果不带 `_n` 的 `rst`，触发条件通常有何不同？

**参考答案**：`_n` 表示低电平有效（active-low），即信号为 0 时触发复位、为 1 时正常工作。若命名为 `rst`（无 `_n`），通常约定为高电平有效，即信号为 1 时触发复位。读 RTL 时先看后缀判断有效电平，是避免把逻辑看反的基本功。

---

### 4.2 主控制器：npu_controller

#### 4.2.1 概念说明

一颗多核 NPU 不能让所有核各自为政——谁来决定「现在该算什么」「数据从哪取」「中断来了怎么办」？这个「总指挥」就是**主控制器**。在 Mayoiuta 里，它由子模块 `npu_controller` 承担，例化实例名为 `u_controller`。

可以把它理解成 SoC 的「前台经理」：接收来自主机的全局配置，汇总各核上报的状态，并响应外部中断。需要再次强调：**`npu_controller` 的源码不在仓库里**，我们只能从 `NPU_SOC` 对它的连线「反推」它应当承担什么职责，内部如何实现则待确认。

#### 4.2.2 核心流程

主控制器在系统中的信息流如下：

```text
主机 ──pcie_data[95:0]──▶ global_config  ──┐
各核 ──noc_ctrl[i]──────▶ cores_status   ──┤── npu_controller ──▶ (调度各核，内部待确认)
外部 ──interrupt───────────────────────────┘
```

也就是说，控制器「吃」进三类输入：全局配置、各核状态、中断；至于它「吐」出什么调度决策，在本文件里**看不到**——它没有显式的输出端口连到 `NPU_SOC` 的对外端口。这一点很关键：从顶层看，`npu_controller` 像是个「只读输入、不见输出」的黑盒，它的输出很可能是通过 `noc_ctrl` 反向回灌给各核，但该回灌路径在本文件中未体现，属于待确认。

#### 4.2.3 源码精读

主控制器的例化在 [hardware/rtl/top/npu_soc.v:18-25](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/top/npu_soc.v#L18-L25)：

```verilog
// 主控制器
npu_controller u_controller(
    .clk(clk),
    .rst_n(rst_n),
    .cores_status(noc_ctrl),           // 接收所有核的控制/状态总线
    .global_config(pcie_data[95:0]),   // 接收主机配置（取低 96 位）
    .interrupt(interrupt)              // 接收外部中断
);
```

逐行说明：

- `u_controller` 是实例名（`u_` 前缀是「unit/实例」的常见约定）。被例化的模块类型是 `npu_controller`。
- `.cores_status(noc_ctrl)`：把整组 `noc_ctrl` 数组（`CORES` 条 32 位总线）整体连给控制器的 `cores_status` 端口，让控制器「看见」每个核的状态。这是一种**总线束（bus bundle）**的连法。
- `.global_config(pcie_data[95:0])`：把主机 128 位输入的**低 96 位**作为全局配置。为什么是 96 位、高 32 位去了哪里？本文件未交代，属于待确认。
- `.interrupt(interrupt)`：外部中断透传给控制器。

> 注意：`u_controller` 的例化里**没有任何输出端口连线**（没有形如 `.xxx(某外部信号)` 的输出）。在 Verilog 中这并不违法（输出端口可以悬空），但它意味着主控制器的调度结果在顶层文件里无从观察。这是真实源码呈现出的「断点」，阅读时要如实标注。

#### 4.2.4 代码实践

**实践目标**：通过「端口对照」判断一个子模块在顶层里是「可见输出」还是「黑盒」。

**操作步骤**：

1. 阅读上面的 `u_controller` 例化块（第 19–25 行）。
2. 列出它的所有端口连接，分成两栏：左栏「信号方向可能是 input 的」（连到模块内部用），右栏「信号方向可能是 output 的」（应当驱动外部信号）。
3. 对照事实：本块中所有连接要么把外部信号喂进去，要么是 `clk/rst_n`。

**需要观察的现象**：右栏（输出）为空——没有任何外部信号由 `u_controller` 驱动。

**预期结果**：得出结论——从 `NPU_SOC` 顶层看，`npu_controller` 是一个**纯输入黑盒**，它的控制行为无法在顶层端口上观测。若要观测，必须等到 `npu_controller.v` 源码出现（当前待确认）。

#### 4.2.5 小练习与答案

**练习 1**：`.global_config(pcie_data[95:0])` 用了 `pcie_data` 的哪一段？剩下那一段在本文件里有没有被用到？

**参考答案**：用了 `pcie_data` 的低 96 位（第 0–95 位）。剩下高 32 位 `pcie_data[127:96]` 在整个 `npu_soc.v` 中**没有被任何例化使用**。它要么是预留位、要么是设计尚未完成的接线，属待确认。

**练习 2**：`u_controller` 的例化没有显式输出连线，这是否意味着它「什么都不做」？

**参考答案**：不能这么下结论。输出端口悬空在 Verilog 中是合法的；控制器完全可能在内部产生调度信号并通过它「自己」去驱动各核——但在本顶层文件里我们看不到这条路径。所以正确表述是：「`u_controller` 在顶层不可观测，其内部行为待确认」，而不是「它什么都不做」。

---

### 4.3 参数化多核与环形 NoC：npu_core + generate-for

#### 4.3.1 概念说明

这是 `NPU_SOC` 最精彩的一段，也是 Mayoiuta「可扩展」愿景的真正落地点。它做了两件事：

1. **参数化多核**：用 `generate-for` 循环，按 `CORES` 的取值自动例化对应数量的计算核 `npu_core`，并给每个核分配唯一的 `CORE_ID`。
2. **环形 NoC**：把核 i 的数据输出 `noc_out` 连到核 `(i+1)` 的数据输入 `noc_in`，首尾用取模运算相接，形成一个闭环的环形网络。

同样需要强调：`npu_core` 的源码当前不在仓库中，属待确认。我们这里讲的是「`NPU_SOC` 如何把它们连成环」，而非「单个核内部如何计算」。

#### 4.3.2 核心流程

环形 NoC 的连接规则可以用一个简单的映射函数表达。设核总数为 \(N\)（即 `CORES`），核的编号为 \(i \in \{0,1,\dots,N-1\}\)，则核 i 的数据输出会送到下一个核的输入：

\[
\text{next}(i) = (i+1) \bmod N
\]

当 \(N=4\) 时，连接关系为：

\[
0 \rightarrow 1 \rightarrow 2 \rightarrow 3 \rightarrow 0
\]

也就是一条首尾相接的链：

```text
         ┌──────────── 数据沿环顺时针流动 ────────────┐
         ▼                                              │
   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
  │  core 0  │──▶│  core 1  │──▶│  core 2  │──▶│  core 3  │──┘
   └──────────┘   └──────────┘   └──────────┘   └──────────┘
         ▲                                              │
         └────────────── noc_data[(3+1)%4]=noc_data[0] ─┘
```

取模 `%CORES` 的作用正是「把尾接回头」。若没有这个取模，核 \(N-1\) 的输出将无处可去（下标越界），环就断了。环形网络的**直径**（最远两跳之间的距离）为 \(\lfloor N/2 \rfloor\)，因此核数越多，远端通信延迟越大——这是环形拓扑的固有代价。

#### 4.3.3 源码精读

多核例化与环形互连在 [hardware/rtl/top/npu_soc.v:27-41](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/top/npu_soc.v#L27-L41)：

```verilog
// 计算核心阵列
generate
for (genvar i = 0; i < CORES; i = i + 1) begin
    npu_core #(
        .CORE_ID(i)                         // 每个核拿到唯一编号 0,1,...,CORES-1
    ) u_core (
        .clk(clk),
        .rst_n(rst_n),
        .noc_in(noc_data[i]),               // 本核的 NoC 数据输入
        .noc_out(noc_data[(i+1)%CORES]),    // 输出连到下一个核的输入（成环）
        .ctrl_in(noc_ctrl[i]),              // 接收控制器/状态总线
        .ddr_interface(ddr_data)            // 所有核共用同一根 DDR 输出线！
    );
end
endgenerate
```

逐行说明：

- `generate ... endgenerate` + `for (genvar i ...)`：这是 Verilog 的**参数化例化**惯用法。`genvar` 是专供 `generate` 用的循环变量，综合时被展开成静态硬件——`CORES=4` 就在芯片里「长」出 4 个 `npu_core`，`CORES=8` 就长出 8 个。循环在「综合时」展开，不是「运行时」执行。
- `.CORE_ID(i)`：把循环变量 `i` 作为参数传给每个核，于是每个核都知道自己是「几号核」。这是让多核协同、分工的基础。
- `.noc_in(noc_data[i])` 与 `.noc_out(noc_data[(i+1)%CORES])`：这是环形拓扑的核心。注意 NoC 网线本身早在 [第 14–16 行](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/top/npu_soc.v#L14-L16) 就声明好了——`noc_data` 与 `noc_ctrl` 都是长度为 `CORES` 的**数组型 wire**：

  ```verilog
  // 多核互连网络
  wire [127:0] noc_data [0:CORES-1];   // 每核一条 128 位数据线
  wire [31:0]  noc_ctrl [0:CORES-1];   // 每核一条 32 位控制/状态线
  ```

- `.ddr_interface(ddr_data)`：**所有核都连到同一根 `ddr_data` 线上**。这是本段最值得警惕的一处——如果有两个核在同一时钟周期都想写 `ddr_data`，就会产生**总线竞争（多驱动冲突）**。如何避免冲突（总线仲裁）在本文件里没有体现，必然依赖 `npu_core` 内部或某个未出现的仲裁器，属待确认。

> 阅读窍门：Verilog 里 `wire [W-1:0] name [0:N-1];` 声明的是「N 条位宽为 W 的线」组成的数组，用 `name[k]` 取第 k 条。`noc_data` 是数据线数组，`noc_ctrl` 是控制线数组——它们共同构成了「隐式的 NoC」。这也印证了 u1-l2 的结论：Mayoiuta 的互连网络（IN）没有独立目录，而是由顶层这几行 wire 数组实现。

#### 4.3.4 代码实践

**实践目标**：在 `CORES=4` 下，亲手追踪 `noc_data[i]` 与 `noc_ctrl[i]` 的连接关系，画出多核 NoC 拓扑，并发现潜在的「多驱动」风险点。

**操作步骤**：

1. 设 `CORES=4`，列出 `i = 0,1,2,3` 时 `(i+1)%CORES` 的取值，应得 `1,2,3,0`。
2. 对每个核 i，写下「`noc_in` 来自 `noc_data[i]`」「`noc_out` 驱动 `noc_data[?]`」的配对。
3. 据此画一张四核环形拓扑图（可参考上面的示意）。
4. 再统计：`ddr_data` 这根线被几个核驱动？

**需要观察的现象**：

- `noc_data` 的每一根线都恰好被「一个核的 `noc_out`」驱动、被「另一个核的 `noc_in`」读取，形成干净的环，无多驱动冲突。
- `ddr_data` 被**所有 4 个核**同时作为 `ddr_interface` 驱动——这是潜在的多驱动点。

**预期结果**：得到一张明确的环形拓扑，并标注「`ddr_data` 为多核共享输出线，存在多驱动风险，仲裁机制待确认」。

**待本地验证**：以上为静态连线分析。`npu_core` 是否真的会同时写 `ddr_data`、以及由谁仲裁，需要其源码或仿真确认，当前待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`CORES=2` 时，环形 NoC 长什么样？核 0 和核 1 之间的最远跳数是多少？

**参考答案**：`CORES=2` 时，`(0+1)%2=1`，`(1+1)%2=0`，于是 `0 → 1 → 0`，两个核互为彼此的上下游，构成最小环。最远跳数为 \(\lfloor 2/2 \rfloor = 1\) 跳。

**练习 2**：如果把 `noc_out(noc_data[(i+1)%CORES])` 里的 `%CORES` 去掉，写成 `noc_data[i+1]`，当 `i = CORES-1` 时会发生什么？

**参考答案**：当 `i = CORES-1`（如 `CORES=4` 时的 `i=3`），`i+1 = CORES = 4`，而 `noc_data` 的合法下标是 `0` 到 `CORES-1=3`，于是访问 `noc_data[4]` 越界。综合工具会报错或引入未知行为，环也会在最后一个核处断开。`%CORES` 正是用来避免这一点的。

---

### 4.4 性能监控：performance_monitor

#### 4.4.1 概念说明

一颗实用的 NPU 必须能回答「我现在有多忙？功耗如何？带宽吃紧吗？」这些问题——否则上层软件无从做调度与调优。**性能监控模块** `performance_monitor`（实例名 `u_monitor`）就是干这件事的「仪表盘」：它采样各核活动度、DDR 带宽使用，再把汇总结果写进状态字 `status` 上报给主机。

同样，`performance_monitor` 的源码当前不在仓库中，属待确认。我们这里讲的是「`NPU_SOC` 把哪些信号喂给它、又从它取走哪些信号」。

#### 4.4.2 核心流程

性能监控的采样与上报链路：

```text
各核活动度 noc_ctrl ──▶ cores_active ──┐
DDR 带宽   ddr_data[127:96] ──▶ ddr_usage ──┤── performance_monitor ──▶ status[31:16]（上报主机）
```

它「吃」进两类采样信号（核活动、DDR 占用），「吐」出 16 位状态写入 `status` 的高半字。注意它**不参与数据通路计算**，只在一旁观察、计数、上报——典型的「旁路监控（telemetry）」角色。

#### 4.4.3 源精读

性能监控的例化在 [hardware/rtl/top/npu_soc.v:43-49](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/top/npu_soc.v#L43-L49)：

```verilog
// 性能监控
performance_monitor u_monitor(
    .clk(clk),
    .cores_active(noc_ctrl),            // 复用 noc_ctrl 作为各核活动度采样
    .ddr_usage(ddr_data[127:96]),       // 取 DDR 的高 32 位当作带宽占用指示
    .power_status(status[31:16])        // 把监控结果写入 status 的高 16 位
);
```

逐行说明：

- `.cores_active(noc_ctrl)`：把整组控制总线再次喂给监控器，让它能感知「哪些核正在活跃」。这里和 `npu_controller` 复用了**同一组** `noc_ctrl` 信号——一条线被两个模块同时读取是合法的（读多驱一）。
- `.ddr_usage(ddr_data[127:96])`：取 `ddr_data` 的**高 32 位**当作「DDR 占用度」采样。这种把输出数据线「反过来」当监控采样的接法有点不寻常——直观上 `ddr_data[127:96]` 是计算结果的一部分，为什么能代表「带宽占用」？本文件未解释，属待确认。它暗示 `npu_core` 可能在 `ddr_data` 的高位复用了某种「元数据/状态位」，但这只是推测。
- `.power_status(status[31:16])`：监控结果写到 `status` 的**高 16 位**。结合 4.1 的发现——`status[15:0]` 从未被任何模块驱动——可以确认：`status` 这个 32 位状态字，目前只有高半字有意义，低半字悬空待确认。

> 一致性核对：`status` 被声明为 `output reg [31:0]`，但本文件里只有 `performance_monitor` 通过 `status[31:16]` 驱动它的高半字，且 `status` 没有在 `NPU_SOC` 内部被显式赋值。这种「`output reg` 由子模块端口驱动」的写法在 Verilog 中依赖工具行为，建议结合仿真确认，属待确认。

#### 4.4.4 代码实践

**实践目标**：用「信号溯源法」把 32 位状态字 `status` 的每一位来源都追清楚，从而定位「悬空位」。

**操作步骤**：

1. 在 [npu_soc.v](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/top/npu_soc.v) 中搜索 `status`，记录它每一次出现的位置。
2. 建一张 32 格表格，标号 `status[31]` 到 `status[0]`。
3. 凡是被某个例化端口驱动的位，在格子里填上「来源模块.端口」；找不到来源的位标「悬空」。

**需要观察的现象**：`status[31:16]` 由 `u_monitor.power_status` 驱动；`status[15:0]` 找不到任何驱动来源。

**预期结果**：得到一张明确显示「低 16 位悬空」的状态字映射表。这是源码阅读型实践的核心收获——**用证据定位设计缺口**，而不是凭空猜测。

#### 4.4.5 小练习与答案

**练习 1**：`performance_monitor` 的 `.ddr_usage(ddr_data[127:96])` 把一条「输出数据线」当作「占用度采样」连了进来。这种接法在语义上为什么值得怀疑？

**参考答案**：因为 `ddr_data` 语义上是「写回 DDR 的计算结果」，它的内容随计算结果变化；而「DDR 带宽占用」通常应当是一个反映「访问频次/利用率」的统计量。把结果数据的高 32 位当作占用度，缺乏明确的语义依据，所以值得怀疑（待确认）。合理推测是核在高位复用了状态元数据，但这需源码佐证。

**练习 2**：`status` 的低 16 位 `status[15:0]` 在本文件中无人驱动。综合后它最可能呈现什么行为？

**参考答案**：未被驱动的 `output reg` 在综合时行为依赖工具与工艺：可能被优化为常量 0、可能保持为未初始化值（仿真中为 `x`）。在没看到目标工艺约束前无法确定，因此只能标注「悬空/待确认」，不应断言它一定是 0。

---

## 5. 综合实践

本讲的综合任务是把第 4 节的四个模块串成一张完整的「顶层装配图」。这是源码阅读型实践，目的是让你把零散的连线知识固化为一张可以随时回想的整体图。

**实践目标**：绘制 `CORES=4` 时 `NPU_SOC` 的端到端互连框图，标出所有数据流向，并区分「已实现」与「待确认」的环节。

**操作步骤**：

1. 在画布中央画 4 个 `npu_core` 方块（标 `CORE_ID=0..3`），按本讲 4.3 的连接关系用箭头连成环（`noc_data[i] → noc_data[(i+1)%4]`）。
2. 在环的上方画 `npu_controller (u_controller)`，用箭头表示它读取 `noc_ctrl`（cores_status）和 `pcie_data[95:0]`（global_config）与 `interrupt`；用虚线箭头表示「它的调度输出路径在本文件不可见」。
3. 在环的下方画 `performance_monitor (u_monitor)`，用箭头表示它读取 `noc_ctrl`（cores_active）和 `ddr_data[127:96]`（ddr_usage），并向 `status[31:16]`（power_status）写出。
4. 在整张图外围标出四个对外端口：`pcie_data`（入）、`ddr_data`（出，注意标「多核共享、潜在多驱动」）、`interrupt`（入）、`status`（出，注意标「低 16 位悬空」）。
5. 用两种颜色或线型区分：**实线/绿色 = 仓库已实现的连线**（即 `NPU_SOC` 本文件的接线）；**虚线/灰色 + 「待确认」标签 = 子模块内部行为**（`npu_controller` / `npu_core` / `performance_monitor` 的源码均缺失）。

**需要观察的现象**：你会得到一张「外壳完整、内核空心」的装配图——顶层接线清晰可见，但三个子模块都是待确认的灰盒。

**预期结果**：一张标注完整的 `NPU_SOC` 互连框图，至少包含：四核环形 NoC、控制器对配置/中断/核状态的读取、监控器对核活动/DDR 占用的采样与状态上报、`ddr_data` 多驱动风险点、`status[15:0]` 悬空点，以及三个子模块的「待确认」标记。

**待本地验证**：本实践为静态源码阅读产出，不涉及运行；若要验证「多驱动是否真的发生」「悬空位的实际电平」，需引入仿真环境，仓库暂未提供，标注待本地验证。

## 6. 本讲小结

- `NPU_SOC` 是整颗 SoC 的顶层装配模块，对外只暴露 `pcie_data`（入）、`ddr_data`（出）、`interrupt`（入）、`status`（出）四类端口，自己几乎不运算，只负责把外部端口与内部子模块「接线」。
- `parameter CORES = 4` 配合 `generate-for`，实现了**参数化多核**——改一个数字就能伸缩核数，这是 README「可扩展」愿景的真正落地点。
- 多核之间的 NoC 是由 `noc_data` / `noc_ctrl` 两组 wire 数组实现的**环形网络**，连接规则是 `noc_out(i) → noc_in((i+1)%CORES)`，取模把链尾接回头成环。
- 三个子模块 `npu_controller`（总指挥）、`npu_core`（计算）、`performance_monitor`（仪表盘）分工明确，但**它们的源码当前都不在仓库里**，只能从连线反推职责，内部行为一律「待确认」。
- 源码阅读中发现了三处真实「待确认」疑点：`ddr_data` 被所有核共享输出（潜在多驱动）、`status[15:0]` 从未被驱动（悬空）、`pcie_data[127:96]` 未被使用。

## 7. 下一步学习建议

本讲建立了顶层骨架，但骨架里的「肌肉」——具体的计算与存储逻辑——还没展开。建议按以下顺序继续：

1. **先钻进计算核内部**：进入第 2 单元（u2），从 [hardware/rtl/core/pe_array.v](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v) 的 `PE_Array` / `Processing_Element` 开始，理解 NPU 最核心的脉动阵列是怎么做乘加（MAC）的。这是 `npu_core` 内部最可能复用的计算单元。
2. **再看卷积与多精度**：u2-l2 的 [conv_engine.v](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/conv_engine.v) 与 u2-l3 的 [adaptive_pe.v](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/adaptive_pe.v)，补齐「计算通路」的拼图。
3. **回看本讲悬而未决的问题**：等读完 `pe_array.v` 等计算单元后，回头思考本讲标注的 `ddr_data` 多驱动与 `npu_core` 内部结构——届时你会对「核内部如何与顶层互连协作」有更具体的判断。
4. **若想理解软硬件边界**：可跳到第 5 单元（u5）阅读 [driver/win32/npudriver.c](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c)，看 Windows 驱动是如何通过 `pcie_data` / `interrupt` / `status` 这类接口与本讲的 SoC 对话的——那是顶层端口在「软件侧」的对应物。

> 持续提醒：本讲及后续凡涉及 `npu_controller` / `npu_core` / `performance_monitor` 内部实现的描述，在它们的源码进入仓库之前，都应保持「待确认」标注，切勿臆造。
