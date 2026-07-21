# 仓库目录结构与构建流程

## 1. 本讲目标

上一讲（u1-l1）我们从架构层面俯瞰了 RISC-V² 向量处理器：它挂在一个双发射超标量标量核旁边，向量数据通路走 vRRM → vIS → vEX，并旁路 vMU。本讲把镜头从「架构图」拉近到「工程仓库」，要解决三个非常实际的问题：

1. 这个仓库到底有哪些目录？每个目录各自负责什么？
2. 几十个 `.sv` 源文件是怎么被组织起来、交给仿真器的？
3. 在 QuestaSim 里点一下就能跑起来的那个 `compile_vector_simulator.do`，每一步到底做了什么？

学完本讲，你应该能够：

- 说出 `rtl/shared`、`rtl/vector`、`sva`、`vector_simulator` 四个目录的职责分工；
- 看懂 `files_rtl.f` / `files_sim.f` 两份文件清单（filelist）的组织方式；
- 解释 `vlog` 命令里三个 `+incdir` 路径分别让哪些 `\`include` 能够被解析；
- 读懂 `compile_vector_simulator.do` 里 `vlib` → `vlog` → `vsim` → `run -all` 的完整编译运行流程。

> 本讲只讲「项目骨架与构建方式」，不深入任何模块的内部实现。后续讲义会逐个进入 vRRM、vIS、vEX、vMU 的源码细节。真正「端到端跑一次仿真」的操作流程放在 u1-l5，本讲只把构建脚本本身讲透。

## 2. 前置知识

在开始之前，先用一两句话解释几个硬件工程里的常见术语。如果你已经熟悉，可以直接跳到第 3 节。

- **RTL（Register Transfer Level，寄存器传输级）**：用硬件描述语言描述电路在寄存器之间如何流动数据。本项目的 RTL 全部用 **SystemVerilog**（`.sv` 文件）写成，它们是「可综合（synthesisable）」的，也就是理论上能被工具转成真实芯片电路。
- **TB（Testbench，测试台）**：包围在 RTL 外面的激励与检查代码。RTL 是被测对象（DUT，Design Under Test），TB 负责给它喂输入、观察输出。本项目把 TB 放在 `vector_simulator/`。
- **filelist（`.f` 文件）**：一个纯文本清单，每行一个文件路径。仿真器读它，就等于「一次性把这堆文件编译进去」，避免在命令行上写一长串文件名。
- **`\`include`**：SystemVerilog 的预处理指令，把另一个文件的内容在编译前「粘贴」到当前位置。被 include 的文件通常是参数、结构体、宏定义等共享定义。
- **`+incdir`**：仿真器（如 QuestaSim）的命令行选项，告诉它「去这些目录里找 `\`include` 引用的文件」。
- **QuestaSim / ModelSim**：业界常用的仿真工具。本项目脚本里的 `vlib`、`vlog`、`vsim` 都是它的命令。QuestaSim 在编译时会自动定义一个叫 `MODEL_TECH` 的宏，本项目的断言注入就靠它来区分「仿真」和「真实综合」（详见第 4.3 节）。

> 一句话直觉：**RTL 是「产品」，TB 是「质检流水线」，filelist 是「零件清单」，`.do` 脚本是「开机按钮」**。本讲就是带你熟悉后两样。

## 3. 本讲源码地图

本讲涉及的文件不多，但都是「骨架级」的文件，先把它们列清楚：

| 文件 | 角色 | 本讲用来讲什么 |
|:---|:---|:---|
| [README.md](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/README.md) | 仓库总说明 | 给出顶层目录层级（Directory Hierarchy）的权威描述 |
| [rtl/README.md](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/README.md) | RTL 目录说明 | 解释各向量单元（vrrm/vis/vex/vmu…）的职责 |
| [vector_simulator/files_rtl.f](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/files_rtl.f) | 可综合 RTL 清单 | 列出所有要编译进设计的 `.sv`，按子系统分组 |
| [vector_simulator/files_sim.f](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/files_sim.f) | 仿真专用文件清单 | 列出只在仿真中存在、不可综合的文件（主存模型、驱动、TB 顶层） |
| [vector_simulator/compile_vector_simulator.do](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/compile_vector_simulator.do) | QuestaSim 构建/运行脚本 | 把两份清单编译、启动仿真、加载波形的完整流程 |

辅助理解（本讲会引用、但属于后续讲义重点）：[vector_simulator/README.md](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/README.md) 描述了 TB 的整体工作方式，u1-l5 会专门讲它。

## 4. 核心概念与源码讲解

本讲按三个最小模块推进：**目录层级与职责** → **文件清单组织** → **编译运行脚本**。三者正好对应「东西放在哪」「怎么打包」「怎么开跑」。

### 4.1 目录层级与职责

#### 4.1.1 概念说明

一个硬件仓库通常会把「能上芯片的代码」「只用于验证的代码」「文档与图片」分开存放。本项目也是如此，README 里给出了顶层目录层级（Directory Hierarchy）的官方说明：

- `images`：原理图与「指令到 microop 的映射」示意图；
- `rtl`：所有**可综合**的 RTL 文件；
- `sva`：设计相关的 **X 检查与断言**（SystemVerilog Assertions）；
- `vector_simulator`：运行向量数据通路仿真的 **TB**，以及一些示例激励。

这段说明来自 README 的 Directory Hierarchy 段落，原文可以在下面「源码精读」里看到。

`rtl` 内部又再分两个子目录：

- `rtl/shared/`：**跨标量/向量复用**的通用 IP。比如各种 FIFO、LRU、仲裁器、弹性缓冲、数据缓存、参数和结构体定义。这些单元不是向量特有的，标量核（未来发布）也会用到。
- `rtl/vector/`：**向量数据通路专用**的 RTL。上一讲提到的 vRRM、vIS、vEX、vMU 全部在这里。

#### 4.1.2 核心流程

把上面的描述画成一张目录树（带职责注释），就得到了本项目的「工程地图」：

```text
RISC-V-Vector/
├── README.md            # 仓库总说明 + Directory Hierarchy
├── LICENSE              # MIT 许可证
├── images/              # 原理图、microop 映射图
├── rtl/                 # ★ 可综合 RTL
│   ├── README.md        #   RTL 单元职责表 + 支持的指令列表
│   ├── shared/          #   通用 IP（FIFO/LRU/仲裁/缓存/参数/结构体）
│   └── vector/          #   向量数据通路（vector_top/vrrm/vis/vex/vmu…）
├── sva/                 # ★ 断言与 X 检查（仅仿真用）
└── vector_simulator/    # ★ TB + 驱动 + 生成脚本 + 示例
    ├── README.md        #   TB 工作方式说明
    ├── compile_vector_simulator.do  # 构建/运行脚本
    ├── files_rtl.f      #   可综合 RTL 清单
    ├── files_sim.f      #   仿真专用文件清单
    ├── sim_generator.py #   把 CSV 转成解码信息（u1-l5 详讲）
    ├── vector_sim_top.sv#   TB 顶层
    ├── vector_driver.sv #   TB 驱动
    ├── wave_simulator.do#   波形配置
    ├── examples/        #   vvadd / saxpy / dot_product / fir 示例
    └── decoder_results/ #   生成脚本的产物（被 TB 读取）
```

记忆要点：**三个带「★」的目录是本讲主角**——`rtl`（造什么）、`sva`（查什么）、`vector_simulator`（怎么跑）。其中 `rtl` 再二分为「共享 IP」和「向量专用」，这个二分会贯穿后续所有讲义。

#### 4.1.3 源码精读

README 的 Directory Hierarchy 段落是这张地图的权威出处：

[README.md:21-28](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/README.md#L21-L28) — 用列表给出顶层四个目录的职责。注意它明确写了 `rtl` 是「all the synthesisable RTL files」（全部可综合 RTL），而 `sva` 是「x-checks and assertions」（X 检查与断言）。

[rtl/README.md:1-6](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/README.md#L1-L6) — 说明 `rtl` 目录只放可综合 RTL，并指出参数集中在 `params.sv`（只有一部分可调）、结构体与宏集中在 `vmacros.sv` / `vstructs.sv`。这里还透露一个重要信息：`params.sv` 里有部分**标量参数**，但「在标量核也发布之前」不会被本仓库使用。这与 u1-l1 讲的「标量核 RTL 暂未公开」相呼应。

[rtl/README.md:10-21](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/README.md#L10-L21) — 用表格列出向量数据通路的主要单元（vrrm/vis/vex/vex_pipe/vmu/vmu_ld_eng/vmu_st_eng/vmu_tp_eng）及其一句话职责。这张表是后续 u2/u3 单元的「目录索引」，本讲只需要知道**这些单元对应的 `.sv` 文件都在 `rtl/vector/` 下**。

[sva/README.md:1-3](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/README.md#L1-L3) — 明确说明 `sva/` 里的断言「so far 只在仿真中使用，没有进入任何形式验证流程」。这呼应 README「Repo State」里的同一句话，也解释了为什么断言依赖仿真器宏 `MODEL_TECH`（见 4.3）。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目的是让你亲手把「目录」和「单元」对应起来。

1. **实践目标**：验证 `rtl/README.md` 表格里列出的 8 个向量单元，是否都能在 `rtl/vector/` 目录下找到同名 `.sv` 文件。
2. **操作步骤**：
   - 打开 [rtl/README.md:10-21](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/README.md#L10-L21) 的表格；
   - 列出 `rtl/vector/` 目录下的所有文件（可以用 `ls rtl/vector/`）；
   - 把表格里的每个单元名，去文件列表里找同名 `.sv`。
3. **需要观察的现象**：8 个单元（vrrm、vis、vex、vex_pipe、vmu、vmu_ld_eng、vmu_st_eng、vmu_tp_eng）应该都能一一对应到 `rtl/vector/` 下的 `.sv` 文件；同时你会发现 `rtl/vector/` 里还有几个表格**没提到**的文件（如 `vrat.sv`、`vrf.sv`、`v_int_alu.sv`、`v_fp_alu.sv`、`vmacros.sv`、`vstructs.sv`、`vdata_operation.sv`、`vld_st_buffer.sv`、`vector_top.sv`）。
4. **预期结果**：表格只列「主要单元」，并不穷举。比如 `vrat.sv`（别名表）和 `vrf.sv`（寄存器堆）是 vRRM 阶段的内部组件，`v_int_alu.sv` 是 vEX 的整数 ALU——这些都会在后续讲义里展开。本步只需建立「单元名 ↔ 文件名」的直觉。

#### 4.1.5 小练习与答案

**练习 1**：`rtl/shared/` 和 `rtl/vector/` 的根本区别是什么？如果把 `fifo_duth.sv` 从 `shared` 挪到 `vector`，会改变它在电路里的功能吗？

> **答案**：区别在于**通用 vs 专用**——`shared/` 是标量核和向量核都能复用的通用 IP（FIFO、LRU、仲裁器、缓存等），`vector/` 是向量数据通路专用。挪动文件只改变路径，不改变电路功能；但会破坏「通用 IP 放 shared」的组织约定，也会让 filelist 里的路径失效。

**练习 2**：`sva/` 里的文件能不能被综合成真实电路？为什么？

> **答案**：不能。`sva/README.md` 明确说这些断言只用于仿真、未进入形式验证流程；断言（`assert property`）是验证构造，综合工具会忽略或剔除它们。本项目通过 `\`ifdef MODEL_TECH` 保证它们只在仿真编译时生效（见 4.3）。

---

### 4.2 文件清单组织

#### 4.2.1 概念说明

项目有几十个 `.sv` 文件，逐个敲进命令行既容易漏也难维护。硬件工程的标准做法是写一份 **filelist（`.f` 文件）**：每行一个文件路径，仿真器用 `-f 文件名` 一次性读入。本项目把文件清单拆成两份：

- `files_rtl.f`：**可综合 RTL**——也就是真正构成向量数据通路的设计文件。
- `files_sim.f`：**仿真专用**——只在仿真里存在、不能上芯片的文件（主存行为模型、TB 驱动、TB 顶层）。

为什么要分开？因为这两类文件的「归宿」不同：RTL 将来要能被综合工具接受，而仿真专用文件里常常带有 `$readmemb`、`$error`、行为级存储模型等不可综合构造。分开清单 = 分开关注点，也方便日后把 RTL 单独拿去综合。

#### 4.2.2 核心流程

`files_rtl.f` 不是把文件随便堆在一起，而是**按子系统分组**，组与组之间用空行隔开，读起来像一份「物料 BOM 表」。它的逻辑顺序其实暗合数据通路：

```text
1. 参数与共享定义     params / structs / vmacros / vstructs   ← 所有人都依赖的底层定义
2. 通用 IP            fifo / lru / arbiter / eb_buff / sram …  ← 共享积木
3. 存储子系统         data_cache / ld_st_buffer / wait_buffer … ← 标量+向量的存储底座
4. vector_top         顶层 wrapper                            ← 把下面这些连起来
5. vRRM 阶段          vrrm / vrat
6. vMU 阶段           vmu / vmu_st_eng / vmu_ld_eng / vmu_tp_eng
7. vIS 阶段           vis / vrf
8. vEX 阶段           vex / vex_pipe / v_int_alu / v_fp_alu
```

> 注意：filelist 里的顺序**并不**代表编译依赖顺序（SystemVerilog 的包/宏才需要讲究顺序，这里大多靠 `\`include` 解决依赖）。这里的分组纯粹是为了**人类阅读和维护**——看到哪一段，就知道在编译哪个子系统。

`files_sim.f` 则很短，只列 4 个文件：主存模型、自动生成的参数、驱动、TB 顶层。其中 `decoder_results/autogenerated_params.sv` 是脚本 `sim_generator.py` 动态生成的（u1-l5 详讲），所以它放在 `decoder_results/` 下而不是版本库里手写。

#### 4.2.3 源码精读

`files_rtl.f` 的第一行是一个开关，告诉仿真器「这些文件按 SystemVerilog 语法解析」：

[files_rtl.f:1](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/files_rtl.f#L1) — `-sv` 选项。其余每行是一个相对路径。注意路径都以 `../rtl/...` 开头，因为这份清单是从 `vector_simulator/` 目录里被读取的（见 4.3 的 `do` 脚本），`../` 就是回到仓库根再进 `rtl/`。

参数与共享定义这一组：

[files_rtl.f:5-8](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/files_rtl.f#L5-L8) — `params.sv`、`structs.sv`（标量侧结构体）、`vmacros.sv`、`vstructs.sv`（向量侧结构体与宏）。这是全设计的「公共字典」。u1-l3 和 u1-l4 会专门讲它们。

通用 IP 这一大组：

[files_rtl.f:10-25](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/files_rtl.f#L10-L25) — 各种 FIFO（`fifo_duth`、`fifo_dual_ported`、`fifo_flush`、`fifo_overflow`、`fifo_initialized`）、LRU（`lru`/`lru_two`/`lru_more`）、仲裁器（`arbiter`/`rr_arbiter`）、弹性缓冲（`eb_buff_generic`/`eb_one_slot`/`eb_two_slot`）、`sram`、`onehot_detect`、`and_or_mux`。它们是 u2-l2、u4-l3 会用到的「积木」。

存储子系统的底座：

[files_rtl.f:27-32](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/files_rtl.f#L27-L32) — `data_cache`（非阻塞数据缓存）、`ld_st_buffer`、`wait_buffer`（实现 miss-under-miss）、`data_operation`，再加上向量侧的 `vdata_operation`、`vld_st_buffer`。这一段横跨 `shared/` 与 `vector/`，对应 u4-l3 的存储子系统。

接着是向量数据通路的四大阶段，按 vRRM → vMU → vIS → vEX 排列：

[files_rtl.f:34](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/files_rtl.f#L34) — `vector_top.sv`，向量数据通路顶层 wrapper。
[files_rtl.f:36-37](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/files_rtl.f#L36-L37) — vRRM 阶段：`vrrm` + `vrat`（别名表）。
[files_rtl.f:39-42](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/files_rtl.f#L39-L42) — vMU 阶段：`vmu` + 三个子引擎（store/load/tile-prefetch）。
[files_rtl.f:44-45](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/files_rtl.f#L44-L45) — vIS 阶段：`vis`（计分板）+ `vrf`（向量寄存器堆）。
[files_rtl.f:47-50](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/files_rtl.f#L47-L50) — vEX 阶段：`vex` + `vex_pipe` + `v_int_alu` + `v_fp_alu`。

再看仿真专用清单：

[files_sim.f:1-7](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/files_sim.f#L1-L7) — 只有四项：`../rtl/shared/main_memory.sv`（主存/L2 行为模型，故意放在 `shared/` 但只仿真用）、`decoder_results/autogenerated_params.sv`（脚本生成）、`vector_driver.sv`、`vector_sim_top.sv`。注意它**没有** `../` 前缀的两项（`decoder_results/...` 和两个 `vector_simulator/` 文件），因为它们就在 `vector_simulator/` 目录内。

#### 4.2.4 代码实践

把 `files_rtl.f` 里每个文件归到一个子系统。这是本讲规格要求的实践的第一半。

1. **实践目标**：为 `files_rtl.f` 的每一段建立一个「文件 → 子系统」对照表，子系统用五类：**共享 / 向量前端 / 执行 / 存储 / 断言**。
2. **操作步骤**：
   - 打开 [files_rtl.f](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/files_rtl.f)；
   - 对每个文件，根据它的目录（`shared/` 还是 `vector/`）和它在数据通路中的位置判断归属；
   - 注意 `sva/` 里的断言文件**并不在** `files_rtl.f` 里，它们通过 `\`include` 被拉进来（见 4.3）。
3. **需要观察的现象**：`files_rtl.f` 的空行分段本身就是提示——同一段里的文件多半属于同一子系统。
4. **预期结果**（参考答案，按下表归类）：

| 子系统 | 包含的文件（来自 files_rtl.f） |
|:---|:---|
| **共享**（定义+通用 IP） | `params.sv`、`structs.sv`、`vmacros.sv`、`vstructs.sv`、`and_or_mux`、`arbiter`、`eb_buff_generic/one_slot/two_slot`、`fifo_dual_ported/duth/flush/overflow/initialized`、`lru/lru_two/lru_more`、`onehot_detect`、`rr_arbiter`、`sram` |
| **存储**（存储子系统底座） | `data_cache`、`ld_st_buffer`、`wait_buffer`、`data_operation`、`vdata_operation`、`vld_st_buffer` |
| **向量前端**（顶层 + 重映射 + 发射） | `vector_top`、`vrrm`、`vrat`、`vis`、`vrf` |
| **存储单元**（vMU 引擎） | `vmu`、`vmu_st_eng`、`vmu_ld_eng`、`vmu_tp_eng` |
| **执行**（vEX 流水） | `vex`、`vex_pipe`、`v_int_alu`、`v_fp_alu` |
| **断言**（不在 files_rtl.f，经 include 注入） | `sva/` 下的 9 个 `*_sva.sv` |

> 说明：上表把「存储」拆成了「存储子系统底座（缓存/缓冲）」和「存储单元（vMU 引擎）」两层，因为它们分别对应 u4-l3 和 u3 单元。如果你只用题目给的五类，可把「存储单元」并入「存储」、「向量前端」归到「向量前端」。`v_fp_alu.sv` 当前是占位实现（u2-l7 会讲），但结构上仍属执行阶段。

#### 4.2.5 小练习与答案

**练习 1**：`files_sim.f` 里为什么没有 `vector_top.sv`？它不是仿真必须的吗？

> **答案**：`vector_top.sv` 是**可综合 RTL**，已经在 `files_rtl.f` 里了。`files_sim.f` 只放「仅仿真」的文件。两份清单在 `vlog` 命令里被一起编译（`-f files_rtl.f -f files_sim.f`，见 4.3），所以 `vector_top` 会作为子模块被 `vector_sim_top` 例化进来。把它放进两份清单会造成重复编译。

**练习 2**：`files_rtl.f` 里的路径写成 `../rtl/shared/params.sv`，这个 `../` 是相对于哪个目录的？

> **答案**：相对于**执行 `vlog` 时的工作目录**，也就是 `vector_simulator/`（`compile_vector_simulator.do` 要求你在这个目录里 `do` 它）。`../` 从 `vector_simulator/` 回到仓库根，再进 `rtl/shared/`。这也是为什么 `files_sim.f` 里的 `vector_driver.sv` 不需要 `../`——它本来就在 `vector_simulator/` 里。

---

### 4.3 编译运行脚本

#### 4.3.1 概念说明

有了目录和清单，还需要一个「开机按钮」把它们串起来。本项目用的是 QuestaSim 的 `.do` 脚本 `compile_vector_simulator.do`。它做四件事：

1. **清理并建库**：删掉旧的 `work` 库，重新 `vlib work` 建一个空的工作库（QuestaSim 把编译产物放在 `work/` 里）。
2. **编译**：用 `vlog` 读两份 filelist，把所有 `.sv` 编译进 `work`。同时用 `+incdir` 指定 `\`include` 的搜索目录。
3. **加载仿真**：用 `vsim` 加载顶层 `vector_sim_top`，并记录所有信号（`log -r /*`）。
4. **运行并看波形**：加载波形配置 `wave_simulator.do`，`run -all` 跑到结束，最后 `wave zoom full` 缩放全图。

这一节最关键的概念是 **`+incdir` 与 `\`include` 的配合**。`\`include "xxx.sv"` 只写了文件名，没写路径；仿真器需要知道去哪些目录里找 `xxx.sv`，这就是 `+incdir+目录` 的作用。

#### 4.3.2 核心流程

`compile_vector_simulator.do` 的执行流程（伪代码）：

```text
quit -sim                     # 退出当前仿真（如有）
file delete -force work       # 删除旧工作库
vlib work                     # 新建工作库 work/

vlog -f files_rtl.f \         # 编译可综合 RTL 清单
     -f files_sim.f \         # 编译仿真专用清单
     +incdir+../rtl/shared/ \ # ← include 搜索目录 1
     +incdir+../rtl/vector/ \ # ← include 搜索目录 2
     +incdir+../sva/          # ← include 搜索目录 3

vsim -novopt work.vector_sim_top -onfinish "stop"   # 加载顶层模块
log -r /*                     # 记录所有信号到波形
do wave_simulator.do          # 加载波形分组配置
onbreak {wave zoom full}      # 遇到 break 时缩放波形
run -all                      # 跑完仿真
wave zoom full                # 最终全图缩放
```

三个 `+incdir` 路径分别让哪类 `\`include` 能被解析？把全仓库的 `\`include` 收集起来对照，就能得到确定答案（下一节给出证据）：

| `+incdir` 路径 | 目录里实际存在的文件 | 它让哪些 `\`include` 能解析 |
|:---|:---|:---|
| `../rtl/shared/` | `params.sv`、`structs.sv` | `` `include "params.sv" ``、`` `include "structs.sv" `` |
| `../rtl/vector/` | `vstructs.sv`、`vmacros.sv` | `` `include "vstructs.sv" ``、`` `include "vmacros.sv" `` |
| `../sva/` | 9 个 `*_sva.sv` | `` `include "vex_sva.sv" ``、`` `include "vis_sva.sv" `` 等 9 个断言文件 |

这里有一个精妙的设计：**断言文件（`sva/`）是被 RTL 文件主动 `\`include` 进去的**，而不是写在 filelist 里。例如 `rtl/vector/vex.sv` 内部会 `` `include "vex_sva.sv" ``，把断言「注射」进 `vex` 模块内部，从而断言里能直接用模块的内部信号。而整个 include 被 `\`ifdef MODEL_TECH ... \`endif` 包住——`MODEL_TECH` 是 QuestaSim/ModelSim 仿真时自动定义的宏，真实综合工具不会定义它，于是断言只在仿真时生效、综合时自动消失。这正是 `sva/README.md` 所说「只在仿真中使用」的实现手段。

#### 4.3.3 源码精读

清理与建库两行：

[compile_vector_simulator.do:1-4](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/compile_vector_simulator.do#L1-L4) — `quit -sim` 结束上次仿真，`file delete -force work` 删除旧库，`vlib work` 重建。每次 `do` 都从干净状态开始，避免旧编译产物干扰。

核心的编译命令：

[compile_vector_simulator.do:11](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/compile_vector_simulator.do#L11) — 一行 `vlog` 同时吃下两份清单（`-f files_rtl.f -f files_sim.f`），再跟上三个 `+incdir`。脚本第 7–9 行还留了两条被注释掉的旧命令（`#set cmd ...`），可以看出作者早期是用 `-F`（把文件名展开成命令行）后来改用 `-f`（清单文件），这是个演进痕迹。

加载与运行：

[compile_vector_simulator.do:13-18](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/compile_vector_simulator.do#L13-L18) — `vsim -novopt work.vector_sim_top` 加载 TB 顶层；`-onfinish "stop"` 表示仿真结束时不自动退出，方便你看波形；`log -r /*` 递归记录所有信号（`-r` = recursive）；`do wave_simulator.do` 加载预先配置好的波形分组；`run -all` 跑到结束；`wave zoom full` 把波形缩放到全屏。

`+incdir` 对应 `\`include` 的证据（这些都是真实存在于源码里的 include）：

- `vector_sim_top.sv` 里有 `` `include "structs.sv" ``、`` `include "params.sv" `` → 靠 `+incdir+../rtl/shared/` 解析；
- 几乎每个 `rtl/vector/*.sv` 都有 `` `include "vstructs.sv" ``，`params.sv` 和 `vex_pipe.sv` 里有 `` `include "vmacros.sv" `` → 靠 `+incdir+../rtl/vector/` 解析；
- `vex.sv` 里有 `` `include "vex_sva.sv" ``、`vis.sv` 里有 `` `include "vis_sva.sv" ``，依此类推 9 个 → 靠 `+incdir+../sva/` 解析。

以 `vex.sv` 为例，断言注入的写法是：

[vex.sv:7-9](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L7-L9) 和 [vex.sv:262-264](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L262-L264) — 模块声明前 `` `include "vstructs.sv" ``，模块体末尾 `` `include "vex_sva.sv" ``，两处都被 `\`ifdef MODEL_TECH` 包住。综合时这两段消失，仿真时（定义了 `MODEL_TECH`）才生效。

#### 4.3.4 代码实践

这是本讲规格要求的实践的第二半：解释三个 `+incdir` 各自让哪些 `.sv` 能被 `\`include`。这是一个**源码阅读 + 推理**型实践，不需要真的跑仿真。

1. **实践目标**：验证上节表格里的「`+incdir` ↔ `\`include`」对应关系，并能预测「删掉某个 `+incdir` 会报什么错」。
2. **操作步骤**：
   - 打开 [compile_vector_simulator.do:11](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/compile_vector_simulator.do#L11)；
   - 用搜索工具（`grep -rn '`include' rtl/ vector_simulator/`）把所有 `\`include` 列出来；
   - 对每个被 include 的文件名，查它实际躺在哪个目录（`rtl/shared/`、`rtl/vector/`、还是 `sva/`）；
   - 把结果填进 4.3.2 的表格。
3. **需要观察的现象**：每个被 include 的文件名，都能在三个 `+incdir` 目录中的**恰好一个**里找到同名文件，没有遗漏也没有歧义。
4. **预期结果**：
   - 删掉 `+incdir+../rtl/shared/` → `vlog` 报找不到 `params.sv` / `structs.sv`；
   - 删掉 `+incdir+../rtl/vector/` → 报找不到 `vstructs.sv` / `vmacros.sv`（这两个被几乎所有向量文件 include，错误会最先冒出来）；
   - 删掉 `+incdir+../sva/` → 报找不到 9 个 `*_sva.sv` 中的第一个（仅当 `MODEL_TECH` 已定义时；若未定义则 `\`include` 被 `\`ifdef` 屏蔽，不会报错）。
5. **待本地验证**：以上「报错顺序」是基于 include 关系的推理。如果你本地装了 QuestaSim，可以注释掉某一个 `+incdir` 再 `do compile_vector_simulator.do`，观察实际报错信息是否吻合。没有 QuestaSim 也没关系——推理出的对应关系本身就是本实践的产出。

#### 4.3.5 小练习与答案

**练习 1**：`vlog` 命令里 `-f` 和 `-F`（大写）有什么区别？脚本里被注释的旧命令用了哪个？

> **答案**：`-f` 让仿真器把参数当作「清单文件」读取（每行一个文件路径）；`-F` 也读清单，但会额外把清单里以 `+` 开头的选项（如 `+incdir`）展开到命令行。脚本第 7–9 行被注释的旧命令用的是 `-F`（`vlog -F ../dut/files.f ...`），现在的版本改用 `-f` 并把 `+incdir` 直接写在 `vlog` 行上，更直观。

**练习 2**：如果我想新增一条只在仿真里检查的断言，该改哪些地方？为什么不需要动 `files_rtl.f`？

> **答案**：把断言写进 `sva/` 下的某个 `*_sva.sv`（或新建一个），然后在对应 RTL 模块里加一行被 `\`ifdef MODEL_TECH` 包住的 `` `include "xxx_sva.sv" ``。因为 `sva/` 已经在 `+incdir+../sva/` 搜索路径里，`\`include` 能直接解析，不需要把断言文件加进 filelist。这也是为什么 `files_rtl.f` 里看不到任何 `sva/` 文件。

---

## 5. 综合实践

把三个模块串起来，完成一个**「构建就绪性」检查」**任务。目标：在不实际跑仿真的前提下，证明你理解了「目录 → 清单 → 脚本」这条链。

任务步骤：

1. **画一张构建依赖图**：以 `compile_vector_simulator.do` 为中心，画出它读取 `files_rtl.f`、`files_sim.f`、`wave_simulator.do`，以及三个 `+incdir` 目录（`rtl/shared`、`rtl/vector`、`sva`）的关系。
2. **追踪一次 include 解析**：任选 `vector_sim_top.sv` 里的一行 `` `include "params.sv" ``，说明 QuestaSim 是如何靠 `+incdir+../rtl/shared/` 找到 `rtl/shared/params.sv` 的；再说明 `params.sv` 内部的 `` `include "vmacros.sv" `` 又是如何靠 `+incdir+../rtl/vector/` 继续向下解析的（嵌套 include）。
3. **预测改动后果**：
   - 如果把 `files_rtl.f` 里 `vector_top.sv` 这一行删掉，`vsim work.vector_sim_top` 还能成功吗？为什么？
   - 如果把 `+incdir+../sva/` 删掉，但设计里**没有**定义 `MODEL_TECH` 宏，会发生什么？
4. **对照 README 自检**：回到 [README.md:21-28](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/README.md#L21-L28) 的 Directory Hierarchy，确认你画的图里每个目录的职责与官方描述一致。

参考答案要点：

- 第 1 步：`do` 脚本 →（编译读）`files_rtl.f` + `files_sim.f`，（include 搜）`rtl/shared` + `rtl/vector` + `sva`，（波形）`wave_simulator.do`；`files_rtl.f` 指向 `rtl/shared/*` 与 `rtl/vector/*`，`files_sim.f` 指向 `rtl/shared/main_memory.sv` + `vector_simulator/*`。
- 第 2 步：`params.sv` 位于 `rtl/shared/`，所以需要 `+incdir+../rtl/shared/`；它内部 include 的 `vmacros.sv` 位于 `rtl/vector/`，所以还需要 `+incdir+../rtl/vector/`——这就是为什么三个 `+incdir` 缺一不可。
- 第 3 步：删掉 `vector_top.sv` → `vector_sim_top` 例化 `vector_top` 时找不到模块，`vsim` 报「module not found」，加载失败；删掉 `+incdir+../sva/` 且未定义 `MODEL_TECH` → 因为所有 `*_sva.sv` 的 include 都在 `\`ifdef MODEL_TECH` 里，宏未定义时 include 被屏蔽，编译正常通过（只是没有断言保护）。

> 真正「点下开机按钮、看波形和性能日志」的端到端流程在 **u1-l5**。本综合实践只负责让你在跑之前，心里已经有一张完整的构建地图。

## 6. 本讲小结

- 仓库顶层分四大区：`rtl/`（可综合 RTL）、`sva/`（仿真断言）、`vector_simulator/`（TB 与示例）、`images/`（图）；`rtl/` 再分为 `shared/`（通用 IP）和 `vector/`（向量数据通路）。
- RTL 文件靠两份 filelist 组织：`files_rtl.f`（可综合设计，按子系统分组）和 `files_sim.f`（仅仿真的主存模型、驱动、TB 顶层）。
- `files_rtl.f` 的分组顺序暗合数据通路：参数/定义 → 通用 IP → 存储底座 → `vector_top` → vRRM → vMU → vIS → vEX。
- `compile_vector_simulator.do` 的流程是：`vlib work` 建库 → `vlog -f` 编译两份清单 → `vsim` 加载顶层 → `log`/`do wave` → `run -all`。
- 三个 `+incdir` 各司其职：`rtl/shared/` 解析 `params.sv`/`structs.sv`，`rtl/vector/` 解析 `vstructs.sv`/`vmacros.sv`，`sva/` 解析 9 个 `*_sva.sv`。
- 断言通过 `\`include` + `\`ifdef MODEL_TECH` 注入到 RTL 模块内部，因此只在仿真生效、综合时自动剔除——这就是 `sva/` 不出现在 filelist 里的原因。

## 7. 下一步学习建议

本讲讲完了「项目骨架与构建方式」。建议按以下顺序继续：

1. **先打底——参数与类型**：[u1-l3 设计参数与可调旋钮](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/params.sv) 讲 `params.sv` 里哪些能调、哪些不能调；[u1-l4 共享类型与宏定义](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv) 讲 `vstructs.sv`/`vmacros.sv` 里的指令结构体与操作码枚举。这两讲对应本讲 filelist 第一组的四个文件。
2. **再跑通——端到端仿真**：[u1-l5 端到端跑通仿真](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/README.md) 会带你用 `sim_generator.py` 生成激励、执行本讲的 `compile_vector_simulator.do`、查看波形与 `perf_results/results.log`。
3. **想深入构建细节**：可以提前浏览 [vector_simulator/wave_simulator.do](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/wave_simulator.do)，看看波形里预先配置了哪些信号分组（与 u4-l7 的性能调优相关）。

> 进入 u2 单元后，我们会沿着本讲建立的 filelist 分组，自顶向下钻进 `vector_top.sv`，开始真正的数据通路源码精读。
