# 全系统数据通路与集成

## 1. 本讲目标

前 11 讲我们逐个拆解了 Mayoiuta 的叶子模块：顶层 `NPU_SOC`（u1-l3）、脉动阵列 `PE_Array`（u2-l1）、存储控制器 `Memory_Controller`（u3-l1）、数据重排 `Data_Reorder`（u3-l2），以及卷积引擎、自适应 PE、形状适配、稀疏引擎、DVFS 等。本讲是**专家层的收口**——不再引入新模块，而是把这些零件**拼回一颗完整的 NPU**，追踪一次计算请求如何端到端流经系统。

学完后你应当能够：

1. 画出 Mayoiuta **设计意图上的端到端数据通路**：SoC 仲裁 → NoC 传输 → 存储控制器取数 → 数据重排 → PE 阵列/卷积计算 → 结果写回。
2. 把这条"理想通路"与**仓库现状**逐段对照，识别出三类**连接断点**：黑盒断点（引用但无源码）、未例化断点（有源码但无人例化）、接口断点（位宽/类型对不上）。
3. 说清多核如何通过**环形 NoC** 协同、`performance_monitor` 如何以**旁路**方式采样负载，以及二者在整体架构中的位置。
4. 养成一个工程直觉：读 RTL 不只看"模块内部逻辑对不对"，更要看"模块之间**那根线**有没有真的连上、连得对不对"。

> 全局提醒：Mayoiuta 是**源码阅读型**项目，仓库不含仿真/综合脚手架。本讲所有"通路"都是依据端口与例化关系**重建**出来的设计意图；凡是仓库没有提供源码或没有连线的环节，一律标注**待确认**，不臆造其行为。

---

## 2. 前置知识

### 2.1 承接前讲

本讲直接复用以下已建立的认知，不再重复细节：

- **u1-l3（NPU_SOC）**：顶层模块只做"装配"，对外暴露 `pcie_data`(入)、`ddr_data`(出)、`interrupt`(入)、`status`(出) 四个接口；内部用 `parameter CORES` + `generate-for` 例化多核，用 `(i+1)%CORES` 把核串成**环形 NoC**。它引用的 `npu_controller` / `npu_core` / `performance_monitor` 三个子模块**仓库均无源码**。
- **u2-l1（PE_Array）**：脉动阵列靠 `opcode` 分派三种动作——`0x1` 装权重、`0x2` 乘加、`0x3` 直通推进数据流；只有 `0x3` 让数据在阵列里"流动"。
- **u3-l1（Memory_Controller）**：8 个 bank、256 位字宽、低位交叉地址解码（`addr[2:0]` 选 bank）；`ecc_checker` 无源码。
- **u3-l2（Data_Reorder）**：用 `data_format` 在 NCHW / NHWC / Blocked 三种排布间切换，把存储里的数据**整形**成 PE 阵列喜欢的格式；其 `mem_data` 是 **128 位**。

如果上述任何一条你已记不清，建议先回看对应讲义再继续——本讲会把它们当作已知事实来串联。

### 2.2 三个集成层面的概念

| 概念 | 通俗解释 |
|------|----------|
| **数据通路（data path）** | 一次计算从进到出，数据**依次经过**哪些模块、每段被怎样处理。像一条流水线，关注的是"上一站的输出，是不是下一站的输入"。 |
| **例化（instantiation）** | 在一个模块里"放上"另一个模块的实例并把端口连起来。例化关系决定了谁是父、谁是子，是 RTL 里**唯一的"接线方式"**。 |
| **互连断点（interconnect gap）** | 本该相连的两个模块之间，因为缺源码、没例化、或位宽/类型不匹配，导致数据**过不去**的地方。本讲的核心工作就是清点这些断点。 |

### 2.3 读集成的两条原则

1. **看例化，不看想象**：两个模块在物理上连通的唯一证据是某处 `module_B u_b ( .port(signal) )` 把它们的端口绑到同一根信号。没有例化语句，就没有连接——哪怕它们的名字听起来天生一对。
2. **端口要对得上**：即便例化了，端口位宽与类型也得匹配。一个 256 位的输出驱动一个 128 位的输入，或用一个"打包总线"去驱动一个"解包数组"，都是标准 Verilog 里连不上的"接口断点"。

---

## 3. 本讲源码地图

本讲不再读新文件，而是把四个关键文件**摆在一起**看它们如何（以及是否）互相连通：

| 文件 | 模块 | 在通路中的角色 | 本讲关注点 |
|------|------|----------------|------------|
| `hardware/rtl/top/npu_soc.v` | `NPU_SOC` | 全系统装配图、对外接口、NoC 互连 | 它**实际例化了谁**、哪些信号悬空/多驱动 |
| `hardware/rtl/memory/mem_ctl.v` | `Memory_Controller` | 取数环节：主机写入、NPU 读出 | 256 位 `npu_rd_data` 如何交给下游 |
| `hardware/rtl/memory/data_reorder.v` | `Data_Reorder` | 重排环节：存储格式 → PE 格式 | 128 位 `mem_data` 的来源、`pe_data` 的去向 |
| `hardware/rtl/core/pe_array.v` | `PE_Array` / `Processing_Element` | 计算环节：乘加阵列 | `north_in`/`west_in` 的位宽与数据流节奏 |

> 还有一个隐藏角色：`npu_core`（被 `NPU_SOC` 例化，但**无源码**）。按设计意图，存储、重排、计算这三站应当**住在 `npu_core` 内部**；但它的内部结构仓库没有提供，这正是本讲最大的"黑盒"。

---

## 4. 核心概念与源码讲解

本讲按"通路分段"组织五个最小模块：**4.1 端到端通路总览**、**4.2 NPU_SOC 装配图与三类断点**、**4.3 存储取数环节（Memory_Controller → Data_Reorder）**、**4.4 计算环节（Data_Reorder → PE_Array）**、**4.5 多核协同与性能监控旁路**。

### 4.1 端到端数据通路：设计意图 vs 仓库现状

#### 4.1.1 概念说明

读一颗芯片的源码，最终要回答一个问题：**"用户给一个输入，芯片吐一个输出，中间发生了什么？"** 这条"输入→中间各站→输出"的链路就是**数据通路**。

对 NPU 而言，一次神经网络推理的典型通路是：

```
主机(CPU/驱动) ──指令+数据──► NPU ──► 结果回主机
```

进了 NPU 内部，数据要经历"取数 → 整形 → 计算 → 写回"四道工序。本讲要做的，就是把前 11 讲学过的零件**填进这四道工序**，看它们能不能首尾相接。

#### 4.1.2 核心流程

**设计意图上的端到端通路**（依据各模块端口与命名推断的理想流水线）：

```
          ┌──────────────────── NPU_SOC（顶层装配）────────────────────┐
          │                                                            │
主机 ──pcie_data──► [npu_controller 仲裁/下发配置]                      │
          │              │                                              │
          │              ▼ 配置 + NoC 指令                               │
          │     ┌─── 环形 NoC (noc_data / noc_ctrl) ───┐                │
          │     ▼                                      ▼                │
          │  [npu_core #0]  →  [npu_core #1]  → ... → (#CORES-1) → 回 #0│
          │     │  内部应有：                                            │
          │     │   ① Memory_Controller 取主机写好的权重/激活            │
          │     │   ② Data_Reorder 把 NCHW/NHWC 重排成 PE 格式           │
          │     │   ③ PE_Array / Conv_Engine 做乘加/卷积                 │
          │     ▼   ④ 结果写回 DDR                                       │
          └──ddr_data◄── (各核结果汇总)                                  │
          │                                                            │
主机 ◄──status──── [performance_monitor 旁路采样负载/带宽] ◄────────────┘
```

伪代码（一个请求的生命周期）：

```
1. 主机把权重和激活经 Memory_Controller 的 host 接口写入 memory_bank
2. npu_controller 解析 pcie_data[95:0] 的全局配置，经 NoC 把任务分发给各核
3. 某核从 Memory_Controller 的 npu 接口读出数据 (256 位字)
4. Data_Reorder 按 data_format 把数据整形为 PE 友好排布
5. PE_Array 在 opcode 节奏下加载权重、乘加、输出部分和
6. 结果写回 ddr_data，performance_monitor 顺便把负载/带宽填进 status
```

**仓库现状**（把理想通路逐段点亮后，真正"通"的只有寥寥几段）：

```
主机 ──pcie_data──► npu_controller  ··(无源码)··►  ???  ··(无源码)··►  ddr_data ──► 主机
                                          ▲                    ▲
                              Memory_Controller、Data_Reorder、PE_Array 都有源码，
                              但在【可见源码里没有任何模块例化它们】——它们是孤岛
```

核心结论先抛出来，后文逐段论证：

> **三类连接断点**贯穿全系统：
> 1. **黑盒断点**：`npu_controller` / `npu_core` / `performance_monitor` 被例化却无源码，整个"控制 + 计算核内部"不可见。
> 2. **未例化断点**：`Memory_Controller` / `Data_Reorder` / `PE_Array` 等叶子模块有源码，但可见源码里没有任何 `module` 例化它们（理想情况下应住在 `npu_core` 内部，而 `npu_core` 无源码）。
> 3. **接口断点**：即便想强行连线，位宽/类型也对不上——256 位 vs 128 位、打包总线 vs 解包数组。

#### 4.1.3 源码精读

先看顶层对外只暴露的四个端口（[npu_soc.v:1-12](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/top/npu_soc.v#L1-L12)）：

```verilog
module NPU_SOC #( parameter CORES = 4 )(
    input  wire         clk,
    input  wire         rst_n,
    input  wire [127:0] pcie_data,   // 主机 → SoC（指令+数据）
    output reg  [127:0] ddr_data,    // SoC → 主机（结果）
    input  wire         interrupt,   // 主机 → SoC（中断）
    output reg  [31:0]  status       // SoC → 主机（状态/负载）
);
```

这就是整颗 SoC 与外部世界的**全部通道**：进两路（`pcie_data`、`interrupt`）、出两路（`ddr_data`、`status`）。所有我们讨论的"通路"，起点和终点都必须落在这四个端口上。

再看内部例化了什么（[npu_soc.v:19-49](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/top/npu_soc.v#L19-L49)）：顶层只例化了 `npu_controller`、`npu_core`、`performance_monitor` **三件事**——而这三件**全部没有源码**。反过来说，本课程前 11 讲精读过的所有叶子模块（`Memory_Controller`、`Data_Reorder`、`PE_Array`、`Conv_Engine`、`Adaptive_PE`、`Shape_Adaptor`、`Sparse_Engine`、`DVFS_Controller`），在可见源码里**没有任何一处被例化**。它们是一抽屉精致的零件，却没有螺丝把它们拧到 SoC 上。

> 这正是 Mayoiuta 当前状态的本质：**一份模块齐全、互连缺失的设计草图**。后文 4.2–4.4 会逐站讲清"断点具体长什么样"。

#### 4.1.4 代码实践（断点清点型·源码阅读）

**实践目标**：用"有没有例化语句"这把尺子，亲手量出整仓库的互连现状，建立对"断点"的第一手感觉。

**操作步骤**：

1. 在仓库根目录用只读检索，找出**每一个叶子模块名**是否在**任何 `.v` 文件里作为被例化对象**出现。例如检索 `Memory_Controller`、`Data_Reorder`、`PE_Array` 等名字。
2. 制作一张三列表格：`模块名 | 是否有 module 定义 | 是否被任何模块例化`。
3. 把"有定义但无例化"的模块单独圈出来。

**需要观察的现象 / 预期结果**：

- `NPU_SOC` 内部只出现 `npu_controller u_controller(...)`、`npu_core #(...) u_core(...)`、`performance_monitor u_monitor(...)` 三条例化语句。
- `Memory_Controller`、`Data_Reorder`、`PE_Array` 等都只有 `module` 定义、没有任何例化——它们是"孤岛"。
- `npu_core` 是例化方与被例化方的交界：它被 `NPU_SOC` 例化，但自身无 `module` 定义；理想中它应当是那些叶子模块的**宿主**，可宿主的内部图纸上空白。

> 待本地验证：用 `git grep -n "模块名"` 或编辑器全局搜索复核；仓库不含综合脚本，无法靠工具自动报"unreferenced module"，只能手工核对。

#### 4.1.5 小练习与答案

**练习 1**：为什么说"模块写得很完整，但系统可能根本跑不起来"？

**参考答案**：RTL 里模块之间靠**例化**连线。一个模块再完整，只要没有父模块把它例化并把端口接到信号上，它在综合时就是"顶层不可达"的死代码，不会参与任何数据流。Mayoiuta 的叶子模块大多处于这种状态——逻辑漂亮，但没被拧进系统。

**练习 2**：本节列出了"三类连接断点"，请各举一个仓库中的例子。

**参考答案**：①黑盒断点——`npu_core` 被 `NPU_SOC` 例化却无 `module` 定义；②未例化断点——`PE_Array` 有完整定义却无人例化；③接口断点——`Memory_Controller` 的 `npu_rd_data`（256 位）与 `Data_Reorder` 的 `mem_data`（128 位）位宽不等（详见 4.3）。

---

### 4.2 NPU_SOC：装配图与三类断点

#### 4.2.1 概念说明

`NPU_SOC` 是整颗芯片的**装配图**：自己不算一行 MAC，只负责把对外端口接到内部子模块、把子模块之间用 NoC 连起来。读装配图的正确姿势是**逐信号追问两个问题**：

- 这根信号**从哪来**（谁驱动它）？
- 这根信号**到哪去**（哪些模块的哪个端口用到它）？

凡是"没人驱动"或"没人用"的信号，就是装配图上的**断头线**。

#### 4.2.2 核心流程

`NPU_SOC` 的装配可拆成三块：

```
① 控制块：npu_controller  ← pcie_data[95:0]、interrupt、noc_ctrl
② 计算块：npu_core × CORES ← noc_data（环形）、noc_ctrl、ddr_data
③ 监控块：performance_monitor ← noc_ctrl、ddr_data[127:96]  → status[31:16]
```

逐信号流向：

```
pcie_data[95:0]   ──► npu_controller.global_config
pcie_data[127:96] ──► ✗ 无人使用（断头）
interrupt         ──► npu_controller.interrupt
noc_ctrl[i]       ──► npu_controller.cores_status  （上报）
                  ──► npu_core[i].ctrl_in          （下发）
                  ──► performance_monitor.cores_active（采样）
noc_data[i]       ──► npu_core[i].noc_in
npu_core[i].noc_out ──► noc_data[(i+1)%CORES]       （环形回环）
ddr_data          ◄── npu_core[i].ddr_interface（每个核都驱动！）
ddr_data[127:96]  ──► performance_monitor.ddr_usage（采样）
status[31:16]     ◄── performance_monitor.power_status
status[15:0]      ──► ✗ 无人驱动（悬空）
```

#### 4.2.3 源码精读

**控制块**（[npu_soc.v:19-25](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/top/npu_soc.v#L19-L25)）：

```verilog
npu_controller u_controller(
    .clk(clk), .rst_n(rst_n),
    .cores_status(noc_ctrl),
    .global_config(pcie_data[95:0]),
    .interrupt(interrupt)
);
```

注意两点：其一，`pcie_data` 是 128 位，这里只取了低 96 位作 `global_config`，**高 32 位 `pcie_data[127:96]` 全仓库无任何引用**——这是装配图上的第一根断头线。其二，`u_controller` 没有任何输出端口接到顶层信号，意味着控制器的决策结果（即使它有源码）在顶层**看不到出口**，只能经由无源码的 `noc_ctrl` 间接体现。

**计算块**（[npu_soc.v:28-41](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/top/npu_soc.v#L28-L41)）：

```verilog
generate
for (genvar i = 0; i < CORES; i = i + 1) begin
    npu_core #(.CORE_ID(i)) u_core (
        .clk(clk), .rst_n(rst_n),
        .noc_in(noc_data[i]),
        .noc_out(noc_data[(i+1)%CORES]),   // 环形回环
        .ctrl_in(noc_ctrl[i]),
        .ddr_interface(ddr_data)            // ← 每个核都驱动同一根线
    );
end
endgenerate
```

这里有一个**多驱动（multi-driver）断点**：`ddr_data` 是 `output reg [127:0]`，却被 `CORES`（默认 4）个 `npu_core` 实例的 `ddr_interface` 同时驱动。在真实硬件里，一根线被多个源驱动会产生冲突（短路/X 态）；这是 `npu_core` 无源码掩盖下的一个接口隐患——它的 `ddr_interface` 究竟是输出、输入还是 inout，端口方向未知，待确认。`noc_data[(i+1)%CORES]` 的取模回环是**环形 NoC**的标志：核 0 的输出进核 1，……，核 `CORES-1` 的输出绕回首核 0，闭合成环（详见 4.5）。

**监控块**（[npu_soc.v:44-49](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/top/npu_soc.v#L44-L49)）：

```verilog
performance_monitor u_monitor(
    .clk(clk),
    .cores_active(noc_ctrl),                 // 采样各核活跃度
    .ddr_usage(ddr_data[127:96]),            // 采样 DDR 高 32 位作带宽指示
    .power_status(status[31:16])
);
```

`performance_monitor` 是典型的**旁路监控**：它只**读**系统信号（`noc_ctrl`、`ddr_data` 的高位切片），不参与数据通路，只把统计结果写到 `status[31:16]`。由此也暴露第二根断头线：`status[15:0]` **没有任何驱动源**，永远悬空（或为 X）。主机读 `status` 时，低 16 位是无效的。

把三块合起来，`NPU_SOC` 的"三类断点"全景：

| 断点类型 | 具体表现 | 位置 |
|----------|----------|------|
| 黑盒 | `npu_controller`/`npu_core`/`performance_monitor` 均无 `module` 定义 | L19/L30/L44 |
| 接口·悬空 | `pcie_data[127:96]` 无人使用 | L23 |
| 接口·悬空 | `status[15:0]` 无人驱动 | L48 |
| 接口·多驱动 | `ddr_data` 被 `CORES` 个核同时驱动 | L38 |

#### 4.2.4 代码实践（信号追溯型）

**实践目标**：对 `NPU_SOC` 的每一个对外端口，追溯它的"源"与"汇"，亲手找出悬空与多驱动。

**操作步骤**：

1. 打开 `npu_soc.v`，对 `pcie_data`、`ddr_data`、`interrupt`、`status` 四个端口各画一条"流向线"。
2. 对 `pcie_data`：检索它在体内被引用的所有位段，确认哪些位被用到、哪些位被丢弃。
3. 对 `status`：找出谁在驱动它的每一位段，标出没有驱动源的位段。
4. 对 `ddr_data`：数一数有几个 `always`/实例在驱动它，判断是否多驱动。

**需要观察的现象 / 预期结果**：

- `pcie_data` 只有 `[95:0]` 被接进 `u_controller`，`[127:96]` 全无引用。
- `status` 只有 `[31:16]` 被 `u_monitor.power_status` 驱动，`[15:0]` 无人驱动。
- `ddr_data` 被 `CORES`（默认 4）个 `npu_core` 实例驱动，存在多驱动隐患。

> 待本地验证：因 `npu_core` 无源码，`ddr_interface` 的真实方向（in/out/inout）未知；若它实际是 inout 且各核分时驱动，则多驱动结论需修订——标注待确认。

#### 4.2.5 小练习与答案

**练习 1**：`pcie_data[127:96]` 这 32 位被丢弃，可能意味着什么？

**参考答案**：可能两种情况：①预留未用——作者保留了高 32 位作未来扩展（如额外的指令段或校验），但当前设计未启用；②接线遗漏——本该接到某个控制器端口却被漏掉。无论哪种，对读者而言它当前就是"死位"，从这 32 位进来的数据不会影响任何行为。属待确认。

**练习 2**：为什么"多驱动 `ddr_data`"是一个严重隐患？在 `npu_core` 无源码的前提下，我们能否 100% 断定它一定出错？

**参考答案**：一根线被多个源同时驱动时，若两源输出不同电平，结果为 X（冲突），综合工具会报 multi-driver 错误。但严格地说，我们**不能 100% 断定**——因为 `npu_core` 无源码，`ddr_interface` 的端口方向未知；如果它其实是 inout 且各核分时驱动（类似总线仲裁），那么 `NPU_SOC` 这一层缺一个仲裁器才是问题，而非裸的多驱动。故标"多驱动隐患，方向待确认"。

---

### 4.3 存储取数环节：Memory_Controller → Data_Reorder 的宽度断点

#### 4.3.1 概念说明

数据通路的第一道工序是**取数**：主机提前把权重和激活写进片上存储，计算时再读出来。`Memory_Controller`（u3-l1）正是这道工序的执行者——它有**主机写口**和**NPU 读口**两个接口，扮演"主机与 NPU 之间的存储中转站"。

按设计意图，`Memory_Controller` 读出的数据应当交给 `Data_Reorder` 做格式整形。本节要回答：**这两站之间，端口真的对得上吗？**

#### 4.3.2 核心流程

理想的取数环节：

```
主机 ──host_wr_data(256位)──► Memory_Controller ──npu_rd_data(256位)──► Data_Reorder ──► 计算单元
                                   memory_bank (8 bank × 256 位)
```

关键数字：

- `Memory_Controller` 的 `DATA_WIDTH = 256`，故 `host_wr_data` / `host_rd_data` / `npu_rd_data` 都是 **256 位**。
- `Data_Reorder` 的 `mem_data` 是 **128 位**。

两站之间隔着一道**位宽断点**：256 位的输出要喂给 128 位的输入，必须有一个"拆字/选半字"的环节（例如取高 128 位或低 128 位，或跨周期拼装）。而仓库里**没有任何模块承担这个角色**，也没有例化语句把两者连起来。

#### 4.3.3 源码精读

`Memory_Controller` 的端口与字宽（[mem_ctl.v:1-17](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/mem_ctl.v#L1-L17)）：

```verilog
module Memory_Controller #(
    parameter ADDR_WIDTH = 32,
    parameter DATA_WIDTH = 256,   // ← 256 位字宽
    parameter BANK_NUM = 8
)(
    ...
    output reg [DATA_WIDTH-1:0] npu_rd_data   // ← 256 位读出
);
```

存储体声明与 NPU 读逻辑（[mem_ctl.v:19-30](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/mem_ctl.v#L19-L30)）：

```verilog
reg [DATA_WIDTH-1:0] memory_bank [0:BANK_NUM-1][0:(1<<(ADDR_WIDTH-3))-1];
...
if (npu_rd_en) begin
    npu_rd_data <= memory_bank[npu_addr[2:0]][npu_addr[ADDR_WIDTH-1:3]];
end
```

读出的 `npu_rd_data` 是完整 256 位字。再看 `Data_Reorder` 的输入端口（[data_reorder.v:1-10](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/data_reorder.v#L1-L10)）：

```verilog
module Data_Reorder #( parameter CHANNELS=64, parameter TILE_SIZE=8 )(
    input  wire         clk,
    input  wire         rst_n,
    input  wire [127:0] mem_data,   // ← 只有 128 位！
    output reg  [127:0] pe_data,
    input  wire [1:0]   data_format
);
```

两相对照：**256 位 → 128 位**，差了一倍。这在标准 Verilog 里要么是综合报错（位宽不匹配），要么是被强行截断（只接低 128 位，丢失高 128 位）——无论哪种，都不是设计意图上的"完整传递"。

容量旁注（承接 u3-l1 结论）：默认参数下每 bank 深 \(2^{32-3}=2^{29}\) 字，8 bank × 256 位，总容量为

\[
8 \times 2^{29} \times 256\text{ bit} = 2^{40}\text{ bit} = 2^{37}\text{ B} = 128\text{ GiB}
\]

这个数字远超现实片上 SRAM，说明默认参数是占位值；真实部署会大幅调小 `ADDR_WIDTH`，或把大容量挪到 `ddr_data` 对应的片外 DDR。

#### 4.3.4 代码实践（位宽对齐型·源码阅读）

**实践目标**：体会"位宽断点"如何阻断数据流，并设计一个最小的对齐方案。

**操作步骤**：

1. 分别记录 `Memory_Controller.npu_rd_data` 与 `Data_Reorder.mem_data` 的位宽。
2. 回答：若要把两者直连，256 位多出的 128 位该往哪放？是丢弃、分两次传、还是把 `Data_Reorder` 的 `mem_data` 扩成 256 位？
3. 写出一段**示例代码**（非仓库原有代码），在两站之间加一个"选半字"的寄存器：用一个 `use_high` 标志选择 `npu_rd_data[127:0]` 或 `npu_rd_data[255:128]`，输出给 `mem_data`。

```verilog
// 示例代码：256→128 位宽适配器（占位，仓库中不存在）
wire [127:0] mem_data = use_high ? npu_rd_data[255:128]
                                 : npu_rd_data[127:0];
```

**需要观察的现象 / 预期结果**：

- 直连会触发位宽不匹配告警/错误；加适配器后位宽一致，但 `use_high` 的控制逻辑、以及"一个 256 位字需要两拍才能送完"的时序问题，仓库都没有提供，属待确认。
- 这一步揭示：即使两站的**功能**都对，**端口宽度**不一致也会让它们连不起来——集成阶段最常踩的坑。

> 待本地验证：仓库无仿真环境，上述适配器仅为示意，未在仓库内验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么"位宽不匹配"比"逻辑错误"更隐蔽？

**参考答案**：逻辑错误往往会让某个测试用例失败，容易被发现；而位宽不匹配在弱类型/宽松的综合设置下可能只给一个 warning，甚至被悄悄截断——数据"看起来在流"，实则丢了高位，行为半对半错，非常难调试。所以集成时要养成"对每一根连线核对两边位宽"的习惯。

**练习 2**：把 `Data_Reorder` 的 `mem_data` 直接改成 256 位，能否彻底解决问题？

**参考答案**：只能解决"位宽"这一项。改成 256 位后，`Data_Reorder` 内部的位拼接（如 `mem_data[127:112]`）需要整体重写以匹配新位宽，且其输出 `pe_data` 又是 128 位，下游 `PE_Array` 的入口也要重新对齐。位宽是"牵一发而动全身"的全局参数，不能只改一处。

---

### 4.4 计算环节：Data_Reorder → PE_Array 的类型断点

#### 4.4.1 概念说明

数据整形好之后，进入**计算环节**——`PE_Array` 做矩阵乘加。按设计意图，`Data_Reorder` 的输出 `pe_data` 应当就是 `PE_Array` 的输入。本节要回答：**这两站之间，除了位宽，还有没有别的对不上的地方？**

答案是：有，而且是更微妙的一类——**打包总线（packed bus）与解包数组（unpacked array）的类型断点**。

#### 4.4.2 核心流程

理想连接：

```
Data_Reorder.pe_data (128 位打包总线) ──?──► PE_Array.north_in / west_in
```

- `Data_Reorder.pe_data` 是 `reg [127:0]`——一根**连续的 128 位打包总线**。
- `PE_Array.north_in` 是 `wire [DATA_WIDTH-1:0] north_in [ARRAY_SIZE-1:0]`——一个 **8 元素的解包数组，每元素 16 位**。

总宽恰好都是 128 位（\(8 \times 16 = 128\)），但**形态不同**：一个是单根粗线，一个是 8 股细线。在标准 Verilog 里，不能把一根打包总线直接连到一个解包数组端口上——必须有一段"**拆线成股**"的赋值。这段赋值仓库里没有。

更现实的连接还需要考虑**数据流节奏**：`PE_Array` 不是"一拍吃满 128 位就出结果"，它要按 `opcode` 分多拍——`0x1` 装权重、`0x2` 乘加、`0x3` 直通（u2-l1）。所以 `Data_Reorder` 的输出还得配合一个 `opcode` 发生器、一个 `start` 脉冲，才能驱动阵列。这些控制器，仓库同样没有。

#### 4.4.3 源码精读

`Data_Reorder` 的输出形态（[data_reorder.v:7-8](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/data_reorder.v#L7-L8)）：

```verilog
input  wire [127:0] mem_data,
output reg  [127:0] pe_data,   // ← 单根 128 位打包总线
```

`PE_Array` 的输入形态（[pe_array.v:1-14](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/pe_array.v#L1-L14)）：

```verilog
module PE_Array #(
    parameter DATA_WIDTH = 16,
    parameter ARRAY_SIZE = 8
)(
    input  wire [DATA_WIDTH-1:0] north_in [ARRAY_SIZE-1:0],  // ← 8 元素解包数组
    input  wire [DATA_WIDTH-1:0] west_in  [ARRAY_SIZE-1:0],
    ...
    input  wire [3:0] opcode,
    input  wire       start,
    output wire       done
);
```

两站对照：

| 属性 | `Data_Reorder.pe_data` | `PE_Array.north_in` |
|------|------------------------|----------------------|
| 总宽 | 128 位 | \(8 \times 16 = 128\) 位 |
| 形态 | 打包总线（单根） | 解包数组（8 股） |
| 驱动方式 | `reg`，受 `opcode` 无关的时序驱动 | 需配合 `opcode`/`start` 节奏 |

总宽相同是巧合的好兆头，但形态不同意味着**不能直连**。需要一个"总线 → 多车道"的拆分器：

```
pe_data[15:0]   → north_in[0]
pe_data[31:16]  → north_in[1]
...
pe_data[127:112]→ north_in[7]
```

这段赋值，仓库里没有。此外 `PE_Array` 还需要 `opcode` 与 `start` 的驱动者（u2-l1 指出 `start` 端口实际未被使用、`done` 被所有 PE 多驱动），这些控制信号的来源也都待确认。

> 补充：`Data_Reorder` 自身还有 `read_ptr` 未声明、`pe_data` 被两个 `always` 块多驱动等内部缺陷（u3-l2 已述）。即便把它接上，输出也不是干净的——这是"计算环节"在入口处就带着的隐患。

#### 4.4.4 代码实践（总线拆股型·源码阅读）

**实践目标**：写出让 `pe_data` 与 `north_in` 类型匹配的"拆股"赋值，体会类型断点的存在。

**操作步骤**：

1. 确认 `pe_data` 是 128 位打包总线、`north_in` 是 8×16 位解包数组。
2. 写一段**示例代码**（非仓库原有代码），把打包总线按 16 位一组拆给数组各元素：

```verilog
// 示例代码：128 位打包总线 → 8 股 16 位车道（仓库中不存在）
genvar k;
generate
    for (k = 0; k < 8; k = k + 1) begin : split
        assign north_in[k] = pe_data[k*16 +: 16];
    end
endgenerate
```

3. 思考：除了 `north_in`（北向激活），`west_in`（西向权重）从哪来？`opcode` 由谁按节奏产生？

**需要观察的现象 / 预期结果**：

- 加了拆股赋值后，类型匹配问题解决；但 `west_in`、`opcode`、`start` 仍无来源——说明计算环节不是"一根线"的事，而是一组**控制+数据**协同的时序，需要专门的调度器，仓库未提供。

> 待本地验证：示例赋值仅为说明类型适配，未在仓库内综合/仿真。

#### 4.4.5 小练习与答案

**练习 1**：总宽都是 128 位，为什么还不能直连 `pe_data` 到 `north_in`？

**参考答案**：因为两者的**数据形态**不同。`pe_data` 是一根打包总线（`[127:0]`），`north_in` 是解包数组（`[15:0] ... [7:0]`）。标准 Verilog 不允许把打包总线直接连到解包数组端口，必须显式把总线切成等长的车道分别赋值。位宽相同是必要条件，不是充分条件。

**练习 2**：要让 `PE_Array` 真正算出一次矩阵乘加，除了数据，还需要哪些控制信号？

**参考答案**：至少还需要 `opcode`（按拍切换 `0x1` 装权重 → `0x2` 乘加 → `0x3` 直通）、`start`（启动脉冲）、复位后对 `accumulator`/`weight_reg` 的初始化，以及一个判断计算完成、拉高 `done` 的机制（u2-l1 指出 `done` 当前被多驱动，本身也有问题）。这些调度逻辑在本仓库里没有提供，属待确认。

---

### 4.5 多核协同与性能监控旁路

#### 4.5.1 概念说明

前面四节看的是"单核内部"的取数→重排→计算流水。但 `NPU_SOC` 是**多核**设计（`CORES=4`）。本节看两个**横切**全系统的问题：

1. **多核怎么协同**？数据/任务在核间如何流动？
2. **性能监控在哪**？它如何在不干扰数据通路的前提下观测系统？

#### 4.5.2 核心流程

**多核协同：环形 NoC。** `NPU_SOC` 没有星型或网格互连，而是把核串成一个**环**：

```
核0 ──noc_out──► 核1 ──noc_out──► 核2 ──noc_out──► 核3 ──noc_out──► (绕回核0)
```

环的闭合靠取模：核 `i` 的输出送到核 `(i+1) % CORES`。环形拓扑的好处是每个核只需固定两个 NoC 端口（进/出），扩展 `CORES` 数时端口数不变；代价是跨环通信有多跳延迟。

**性能监控：旁路采样。** `performance_monitor` 不在数据通路上，它**只读不写**主通路信号：

```
noc_ctrl(各核活跃) ──► 采样 ──┐
                              ├─► 统计 ──► status[31:16]
ddr_data[127:96]   ──► 采样 ──┘
```

这种"只接观测线、不接数据线"的设计叫**旁路（bypass）/探针（tap）**，好处是监控逻辑即使出错也不会污染计算结果。

#### 4.5.3 源码精读

环形 NoC 的接线（[npu_soc.v:35-37](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/top/npu_soc.v#L35-L37)）：

```verilog
.noc_in(noc_data[i]),
.noc_out(noc_data[(i+1)%CORES]),
.ctrl_in(noc_ctrl[i]),
```

`noc_data` / `noc_ctrl` 的声明（[npu_soc.v:15-16](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/top/npu_soc.v#L15-L16)）：

```verilog
wire [127:0] noc_data [0:CORES-1];   // 每核一组 128 位数据线
wire [31:0]  noc_ctrl [0:CORES-1];   // 每核一组 32 位控制/状态线
```

`noc_ctrl` 是一根"既上报又下发"的复用线：它被同时接到 `npu_controller.cores_status`（核状态上报）、`npu_core[i].ctrl_in`（控制下发）、`performance_monitor.cores_active`（采样）三处。这暗示 `noc_ctrl[i]` 承载核 `i` 的双向状态信息，但具体协议（谁何时写、格式如何）因 `npu_core`/`npu_controller` 无源码而**待确认**。

旁路监控的接线（[npu_soc.v:44-49](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/top/npu_soc.v#L44-L49)）：

```verilog
performance_monitor u_monitor(
    .cores_active(noc_ctrl),            // 读：各核活跃度
    .ddr_usage(ddr_data[127:96]),       // 读：DDR 高 32 位作带宽指示
    .power_status(status[31:16])        // 写：唯一输出，进状态寄存器
);
```

监控块**只有 `power_status` 一个输出**，且只写 `status` 的高半字；它对 `noc_ctrl`、`ddr_data` 都只读。这与 u4-2 讲的 DVFS 形成一条**本应存在却未连上的反馈环**：`performance_monitor` 本该把负载统计喂给 `DVFS_Controller` 做调档，但 `DVFS_Controller` 在 `npu_soc.v` 中**根本未被例化**（u4-l2 已述）——反馈环断开。

把多核与监控合起来，得到一张"协同 + 反馈"的全景：

| 角色 | 接什么 | 通了吗 |
|------|--------|--------|
| 环形 NoC（`noc_data`） | 核 `i` → 核 `(i+1)%CORES` | 接线在，但核内部无源码，**数据流向待确认** |
| 状态/控制（`noc_ctrl`） | 上报 + 下发 + 采样三处共用 | 接线在，协议待确认 |
| 旁路监控 | 读 `noc_ctrl` + 读 `ddr_data` | 接线在，监控内部无源码，**输出待确认** |
| DVFS 反馈环 | `performance_monitor` → `DVFS_Controller` | **未连**（DVFS 未被例化） |

#### 4.5.4 代码实践（拓扑追踪型）

**实践目标**：在 `CORES=4` 下画出环形 NoC 的具体连线，并标注反馈环的断点。

**操作步骤**：

1. 令 `CORES=4`，列出 `i=0,1,2,3` 时 `noc_data[i]` 的源（谁的 `noc_out`）与汇（谁的 `noc_in`）。
2. 画出 4 个核节点 + 4 条有向边构成的环。
3. 在图上标出 `performance_monitor` 的两条采样线（`noc_ctrl`、`ddr_data[127:96]`）和一条输出（`status[31:16]`）。
4. 用虚线画出"本应存在"的反馈：`performance_monitor` → `DVFS_Controller` → 时钟/电源，并打上"未例化"标记。

**需要观察的现象 / 预期结果**：

- `i=3` 时 `noc_out → noc_data[(3+1)%4] = noc_data[0]`，闭环。
- `performance_monitor` 的所有输入都是"读"，不与数据通路竞争驱动权，符合旁路设计。
- DVFS 反馈环在图上是断的——`DVFS_Controller` 是孤岛。

> 待本地验证：核间数据具体如何分发（广播？令牌？）取决于无源码的 `npu_controller`/`npu_core`，本实践只画"看得见的线"，协议层待确认。

#### 4.5.5 小练习与答案

**练习 1**：环形 NoC 相比星型互连，扩核时的主要好处是什么？

**参考答案**：环形拓扑下每个核固定只有两个 NoC 端口（一个进、一个出），增加核数时单核端口数不变，连线复杂度是 \(O(\text{CORES})\)；而星型/全连接互连的端口数或中心交换复杂度会随核数快速增长。代价是环上跨核通信要逐跳转发，延迟与跳数成正比。

**练习 2**：为什么说 `performance_monitor` 是"旁路"设计？它和 DVFS 的反馈环"断了"具体指什么？

**参考答案**：旁路指它只**读取**主通路信号（`noc_ctrl`、`ddr_data` 高位）做统计，不向主通路写任何东西，因此不干扰计算结果、也不竞争驱动权。"反馈环断了"指：按能效设计意图，`performance_monitor` 统计出的负载本应驱动 `DVFS_Controller` 调节电压/频率，但 `DVFS_Controller` 在 `npu_soc.v` 中**未被例化**，于是负载统计只写进 `status` 给主机看，却无法回流成自动调档动作——闭环变成了开环。

---

## 5. 综合实践

**任务：绘制一张"端到端数据通路框图"，并完成系统级互连体检。**

这是本讲的中心实践，把前面五节串成一张图、一张表。

**操作步骤**：

1. **画框图**。横向画出一次推理的完整通路，节点至少包含：`主机/驱动` → `npu_controller` → `NoC(环)` → `npu_core #k` → `Memory_Controller` → `Data_Reorder` → `PE_Array` → `ddr_data` → `主机`；并旁挂 `performance_monitor`。在 `npu_core` 节点下用虚线框标注"内部应有 ①②③④ 四道工序"。

2. **用三种标记区分连接状态**（这是本实践的硬性要求）：
   - ✅ **已连通**：两端都有源码、有例化、端口对得上。
   - ⚠️ **接口隐患**：有例化但存在悬空 / 多驱动 / 位宽或类型不匹配。
   - ❓ **待确认**：引用了无源码模块，或叶子模块无人例化。

3. **逐段标注**，至少覆盖以下环节：
   - 主机 ↔ `NPU_SOC`（`pcie_data` / `ddr_data` / `interrupt` / `status`）
   - `NPU_SOC` ↔ `npu_controller`（黑盒）
   - `npu_controller` ↔ `npu_core`（经 `noc_ctrl`，协议待确认）
   - 核间环形 NoC（`noc_data[(i+1)%CORES]`）
   - `npu_core` ↔ `Memory_Controller`（**未例化 + 256/128 位宽断点**）
   - `Memory_Controller` ↔ `Data_Reorder`（**位宽断点**）
   - `Data_Reorder` ↔ `PE_Array`（**类型断点 + 缺 opcode/start 调度**）
   - `performance_monitor` ↔ `status` / `DVFS_Controller`（DVFS 未例化，反馈环断）

4. **写一份"系统级互连体检表"**（示例首行）：

   | 段 | 起点 → 终点 | 状态 | 原因 |
   |----|-------------|------|------|
   | 取数 | `Memory_Controller` → `Data_Reorder` | ❓待确认 | 无例化；256 位 vs 128 位位宽断点 |
   | ... | ... | ... | ... |

5. **反思题**：如果让你补全这颗 NPU，让数据真正从主机流到结果再流回，你会**优先补哪三处**？为什么？

> 参考思路（反思题）：优先级最高的通常是"打通主数据通路"——①补 `npu_core` 的 `module` 定义（或在其中例化 `Memory_Controller`/`Data_Reorder`/`PE_Array`），②补存储→重排的位宽适配器，③补重排→阵列的类型适配器与 `opcode` 调度器。监控、DVFS、稀疏等"锦上添花"的环节可后补——因为它们不影响主通路能否跑通，只影响跑得多省、多快。这也正解释了为何本课程把它们放在专家层、靠后讲。

> 待本地验证：本实践为源码阅读型，框图与体检表均依据端口/例化关系推断；仓库无仿真环境，"能否跑通"无法上机验证，仅作设计层面分析。

---

## 6. 本讲小结

- **本讲的视角**：从"看每个模块内部"切换到"看模块之间那根线"。集成的本质是例化与端口对接，没有例化语句就没有连接。
- **设计意图上的端到端通路**：主机 → `npu_controller` 仲裁 → 环形 NoC 分发 → `npu_core` 内部"取数(`Memory_Controller`)→重排(`Data_Reorder`)→计算(`PE_Array`)→写回" → `ddr_data` 回主机，旁挂 `performance_monitor` 做负载/带宽采样。
- **三类连接断点**：①**黑盒断点**——`npu_controller`/`npu_core`/`performance_monitor` 被例化却无源码；②**未例化断点**——`Memory_Controller`/`Data_Reorder`/`PE_Array` 等叶子模块有源码但可见源码无人例化；③**接口断点**——位宽不匹配（256 vs 128）、类型不匹配（打包总线 vs 解包数组）、悬空（`pcie_data[127:96]`、`status[15:0]`）、多驱动（`ddr_data` 被 `CORES` 个核驱动）。
- **多核协同**：用 `noc_data[(i+1)%CORES]` 取模回环构成环形 NoC，每核固定两端口，利于扩核；跨核通信协议因核无源码而待确认。
- **性能监控是旁路**：`performance_monitor` 只读 `noc_ctrl` 与 `ddr_data[127:96]`，只写 `status[31:16]`，不污染主通路；但它本应驱动的 `DVFS_Controller` 未被例化，**节能反馈环断开**。
- **本质判断**：Mayoiuta 当前是一份"模块齐全、互连缺失"的设计草图——叶子模块作为零件库质量可观，但把它们拧成系统的连线与无源码的核内装配，是后续真正落地时最大的工作量所在。

---

## 7. 下一步学习建议

- **转入第 5 单元（设备驱动与软硬件接口）**：本讲止步于"芯片内部"。下一讲 **u5-l1（Windows WDF 驱动骨架与 INF）** 会站到主机一侧，看 `pcie_data`/`ddr_data`/`interrupt`/`status` 这四个对外端口在软件侧如何被驱动收发——软硬件接口是端到端通路的"最后一公里"。
- **回看 u1-l3（NPU_SOC）**：本讲对顶层装配图的断点分析是 u1-l3 的深化；若对 `generate-for`、环形 NoC 的基本机制已生疏，可对照重读。
- **动手验证建议**：本讲所有"断点"结论均源自对端口与例化关系的人工核对，强烈建议自建一个最小 testbench，把 `Memory_Controller` + `Data_Reorder` + `PE_Array` 手动例化在一起（补上 4.3.4、4.4.4 的适配代码），观察数据能否真正从存储流到阵列输出——这是检验"断点清单"是否完整的最好方式。仓库不含仿真脚手架，需自行用 Icarus Verilog / Verilator 搭建。
- **延伸思考**：若想理解真实 NPU 如何补齐这些断点，可课外查阅"片上网络（NoC）路由协议""张量核的数据喂给调度（operand staging）""片上 SRAM 多 bank 与计算阵列的带宽匹配"等主题；这些都不在仓库源码内，标注为外部知识。
