# 目录结构与 Vivado 工程构建

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出本项目的目录是怎么组织的，每个文件分别属于「设计源 / 约束 / 脚本 / 文档」中的哪一类。
- 读懂 `Fir_filter.tcl` 这份 Vivado 工程重建脚本，知道它是如何用一条命令把工程「凭空搭起来」的，包括目标器件（part）、综合/实现顶层（top）、仿真顶层分别设成了什么。
- 解释 XDC 约束文件的作用，并说明为什么这个项目里所有 XDC 约束行都被注释掉了。
- 理解为什么「fresh clone 之后直接 source 这份 TCL」容易失败，以及最稳妥的替代做法。

本讲承接上一讲《项目概览与 FIR 滤波器入门》。上一讲我们知道了这个项目「是什么」，本讲解决「它放在哪儿、怎么搭起来」。

## 2. 前置知识

在进入源码之前，先用大白话解释几个 Vivado（Xilinx 的 FPGA 开发工具）里的术语。如果你已经熟悉，可以跳过本节。

- **FPGA 工程（project）**：Vivado 把一个设计封装成一个「工程」，里面记录了用到了哪些源文件、目标是哪块芯片、综合和实现的参数等。工程的元数据通常存在一个 `.xpr` 文件里。
- **part（器件型号）**：FPGA 不是一种通用芯片，而是一个芯片系列。`part` 指明这块设计要烧到具体哪一颗芯片上。本项目用的是 `xc7a100tcsg324-1`（详见 4.2）。
- **fileset（文件集）**：Vivado 把源文件按用途分组，每一组叫一个 fileset。最常见的有：
  - `sources_1`：设计源（RTL，也就是 `.v` Verilog 文件）。
  - `constrs_1`：约束文件（`.xdc`）。
  - `sim_1`：仿真源（testbench）。
  - `utils_1`：工具脚本。
- **top（顶层）**：一个设计由很多模块组成，Vivado 需要知道哪一个模块是「最外层」的，这个模块就叫 top。综合/实现的 top 是给真实硬件用的根模块；仿真的 top 通常是 testbench。
- **run（运行）**：一次综合或实现流程叫一个 run。`synth_1` 是综合 run，`impl_1` 是实现 run。
- **XDC（Xilinx Design Constraints）**：约束文件，用 Tcl 语法写成，告诉工具两件最关键的事：每个顶层端口对应芯片上的哪一根物理引脚（`PACKAGE_PIN`）、这根引脚用哪种电平标准（`IOSTANDARD`）；以及时钟的周期（`create_clock`），用来做时序分析。
- **Tcl（读作 "tickle"）**：一种脚本语言。Vivado 的几乎所有菜单操作都对应一条 Tcl 命令，因此整个工程可以用一个 `.tcl` 脚本描述并重建。注意：**Tcl 和 XDC 都用 `#` 作为行注释符**。

> 一句话总结：Vivado 工程 = 若干 fileset（源文件分组）+ part（目标芯片）+ 若干 run（综合/实现流程）+ 一堆属性。本讲的 TCL 脚本，就是把这些东西一条条命令「复述」出来。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目说明，给出运行方式与可调参数。 |
| `Fir_filter.tcl` | Vivado 工程重建脚本——核心讲解对象。 |
| `Fir_filter.srcs/sources_1/new/*.v` | 6 个 Verilog 设计/仿真源文件（本讲只看它们如何被归类，不看内部逻辑）。 |
| `Fir_filter.srcs/constrs_1/imports/Vivado_projects/Nexys-A7-100T-Master.xdc` | Nexys A7-100T 开发板的「主板级」XDC 约束模板。 |

本讲**不深入 Verilog 设计本身**（那是后续讲义的任务），只关心「文件如何归类、工程如何搭建、约束如何起作用」。

## 4. 核心概念与源码讲解

### 4.1 目录结构

#### 4.1.1 概念说明

Vivado 有一个固定的目录约定：一个名叫 `Fir_filter` 的工程，它的所有源文件会放在 `Fir_filter.srcs/` 这个目录下，并按 fileset 进一步分目录：

```
<工程名>.srcs/
├── sources_1/      # 设计源 fileset
│   └── new/        # Vivado 新建的源文件默认落在这里
└── constrs_1/      # 约束 fileset
    └── imports/    # 「导入」进来的文件落在这里
```

其中 `new/` 表示这些文件是「在工程里新建」的，`imports/` 表示是「从外部导入」的。这是 Vivado 自动产生的子目录名，不是作者自定义的。

#### 4.1.2 核心流程

把本仓库用 `git ls-files` 列出来后，按用途归类如下：

```
仓库根目录
├── README.md                                    # 文档：项目说明 + 运行方式
├── Fir_filter.tcl                               # 脚本：工程重建
└── Fir_filter.srcs/
    ├── sources_1/new/                           # 设计源（6 个 .v 文件）
    │   ├── adder.v                              #   加法器（叶子模块）
    │   ├── delay.v                              #   延迟寄存器（叶子模块）
    │   ├── multiplier.v                         #   Q15 定点乘法器
    │   ├── fir_tap.v                            #   单级流水线抽头
    │   ├── fir_filter.v                         #   顶层模块（综合 top）
    │   └── fir_filter_tb.v                      #   测试台（仿真 top）
    └── constrs_1/imports/Vivado_projects/
        └── Nexys-A7-100T-Master.xdc             # 板级约束模板
```

可以看出 6 个 `.v` 文件全部放在同一个 `sources_1/new/` 目录里——**设计源和 testbench 没有分目录**，Vivado 是靠「文件集属性 + top 设置」来区分哪个是综合顶层、哪个是仿真顶层的，而不是靠目录。

#### 4.1.3 源码精读

TCL 脚本开头的注释里，作者（其实是 Vivado 自动生成）列出了所有需要纳入版本控制的源文件，这是理解目录结构的最快入口：

[Fir_filter.tcl:26-32](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.tcl#L26-L32) —— 注释列出了 6 个 `.v` 源文件和 1 个 `.xdc` 约束文件的原始路径。

注意这些注释里的路径是作者本机的 `D:/Vivado_projects/Fir_filter/...`（Windows 盘符），而不是仓库里的相对路径。这解释了为什么不能盲目相信脚本里的路径——它们是「导出脚本那一刻」作者机器上的快照。

#### 4.1.4 代码实践

**实践目标**：亲手把仓库里的文件分类，建立「文件 → fileset」的直觉。

**操作步骤**：

1. 在仓库根目录执行 `git ls-files`（或直接看上面的树）。
2. 对每一个文件，判断它属于「设计源 / 约束 / 脚本 / 文档」中的哪一类。
3. 填写下面的表格。

| 文件 | 分类 |
| --- | --- |
| `README.md` | 文档 |
| `Fir_filter.tcl` | ? |
| `adder.v` / `delay.v` / `multiplier.v` / `fir_tap.v` / `fir_filter.v` | ? |
| `fir_filter_tb.v` | ? |
| `Nexys-A7-100T-Master.xdc` | ? |

**预期结果**：6 个 `.v` 全部属于 `sources_1`（其中 `fir_filter_tb.v` 同时被仿真 fileset 引用），`.xdc` 属于 `constrs_1`，`Fir_filter.tcl` 是工程重建脚本、`README.md` 是文档——后两者不属于任何 fileset。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `fir_filter_tb.v`（testbench）和 `fir_filter.v`（顶层）放在同一个目录里？Vivado 怎么区分它们？
**答案**：Vivado 靠 fileset 的 top 属性区分，而不是靠目录。`sources_1` 的综合 top 设为 `fir_filter`，`sim_1` 的仿真 top 设为 `fir_filter_tb`（见 4.2.3）。同一个目录里可以既有设计源又有 testbench。

**练习 2**：`constrs_1/imports/Vivado_projects/` 这层路径里的 `imports` 是什么意思？
**答案**：`imports` 表示这个文件是「从工程外部导入」的，而不是在工程里新建的。Nexys 的主板 XDC 通常是从 Digilent 官方下载后导入的，所以 Vivado 把它放进了 `imports/` 子目录。

---

### 4.2 TCL 工程重建

#### 4.2.1 概念说明

Vivado 的 `.xpr` 工程文件是二进制/XML 的，里面包含大量机器相关路径，**不适合放进 git**。社区的标准做法是：用 Vivado 的「Write Tcl」功能导出一份 `.tcl` 脚本，只把这份脚本和源文件纳入版本控制。任何人拿到仓库后，`source` 一下脚本就能在自己机器上「重建」出一个功能等价的工程。

`Fir_filter.tcl` 就是这样一份脚本。它由 Vivado 在 `2025-03-11` 自动生成（见文件头注释），逐条记录了原工程的全部配置。我们需要读懂它的主干，而不是每一行。

#### 4.2.2 核心流程

这份脚本的执行主线可以概括为 7 步：

```
1. 解析命令行参数（--origin_dir / --project_name / --help）
2. create_project  -part xc7a100tcsg324-1        # 建空工程，指定目标芯片
3. 设置工程级属性（part、默认库、仿真语言等）
4. 建 sources_1 文件集 → 导入 6 个 .v → 设置 top = fir_filter
5. 建 constrs_1 文件集 → 导入 .xdc → 设置 target_part
6. 建 sim_1 文件集 → 设置 top = fir_filter_tb
7. 建 synth_1（综合）和 impl_1（实现）两个 run
```

注意第 1 步：脚本默认 `origin_dir = "."`，而所有源文件路径都被拼成 `${origin_dir}/../Vivado_projects/Fir_filter/...`。这意味着脚本期望「仓库的上一层目录里有一个 `Vivado_projects/Fir_filter/` 文件夹」——这恰恰是作者本机的布局，**fresh clone 的仓库并不满足这个布局**。这是一个关键的「坑」，4.2.4 会专门处理。

#### 4.2.3 源码精读

**(a) 目标器件 part**

[Fir_filter.tcl:140](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.tcl#L140) —— `create_project ... -part xc7a100tcsg324-1`，在创建工程的瞬间就指定了目标芯片。之后 [Fir_filter.tcl:157](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.tcl#L157) 又用 `set_property` 把同一个 part 写进工程属性里，做到双重确认。

器件型号 `xc7a100tcsg324-1` 的含义：

| 片段 | 含义 |
| --- | --- |
| `xc7a` | Xilinx 7 系列、Artix（A）家族——低成本、低功耗系列 |
| `100t` | 逻辑密度等级（约 10 万逻辑单元） |
| `csg324` | 封装：CSG（chip-scale）BGA，324 个焊球 |
| `-1` | 速度等级（speed grade），1 是标准档 |

这块芯片正是 **Nexys A7-100T** 开发板上那颗 FPGA。

**(b) 设计源 fileset 与综合顶层**

[Fir_filter.tcl:166-169](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.tcl#L166-L169) —— 如果不存在 `sources_1` 文件集就创建一个。

[Fir_filter.tcl:174-185](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.tcl#L174-L185) —— 把 6 个 `.v` 文件组成一个列表，用 `foreach` 循环逐个 `import_files` 导入 `sources_1`。

[Fir_filter.tcl:196](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.tcl#L196) —— `set_property -name "top" -value "fir_filter"`：把综合/实现的顶层模块锁定为 `fir_filter`。配套的 `top_auto_set = 0`（[L197](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.tcl#L197)）表示「不要让 Vivado 自动猜测顶层，就用我指定的」。

**(c) 约束 fileset**

[Fir_filter.tcl:200-202](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.tcl#L200-L202) —— 创建 `constrs_1` 文件集。

[Fir_filter.tcl:208-212](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.tcl#L208-L212) —— 导入 `Nexys-A7-100T-Master.xdc`，并把它的 `file_type` 显式设为 `XDC`。

[Fir_filter.tcl:216](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.tcl#L216) —— 约束集的 `target_part` 也设成 `xc7a100tcsg324-1`，和工程 part 保持一致。

**(d) 仿真 fileset 与仿真顶层**

[Fir_filter.tcl:218-221](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.tcl#L218-L221) —— 创建 `sim_1` 文件集（里面是空的，因为 testbench 已经在 `sources_1` 里了）。

[Fir_filter.tcl:229](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.tcl#L229) —— `set_property -name "top" -value "fir_filter_tb"`：仿真顶层是 testbench `fir_filter_tb`，而不是 `fir_filter`。这是「同一个工程，两套顶层」的关键：综合给硬件用 `fir_filter`，仿真给波形用 `fir_filter_tb`。

**(e) 综合 / 实现 run**

[Fir_filter.tcl:247-248](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.tcl#L247-L248) —— 创建 `synth_1` 综合 run，使用 `Vivado Synthesis 2023` 流程和默认策略。

[Fir_filter.tcl:274-275](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.tcl#L274-L275) —— 创建 `impl_1` 实现 run，它的 `parent_run` 是 `synth_1`（实现依赖于综合的输出），同样使用默认策略。

> 文件头注释（[L13-L15](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.tcl#L13-L15)）特别提醒：脚本只会**配置**这些 run，**不会自动启动**它们。重建工程后，你需要自己点「Run Synthesis」/「Run Implementation」。

#### 4.2.4 代码实践

**实践目标**：找出工程的「part / 综合 top / 仿真 top」三项关键配置，并解释为什么 fresh clone 直接 source 脚本会失败。

**操作步骤**：

1. 打开 `Fir_filter.tcl`，定位下面三处，记下行号与取值：
   - 目标 part：搜索 `create_project` 与 `-part`。
   - 综合/实现 top：搜索 `sources_1` 文件集的 `top` 属性。
   - 仿真 top：搜索 `sim_1` 文件集的 `top` 属性。
2. 搜索 `validate_required`（[L129](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.tcl#L129)），观察它的值。
3. 看 `checkRequiredFiles` 里列出的文件路径前缀（[L44-L50](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.tcl#L44-L50)），对比仓库里文件的真实位置。

**需要观察的现象**：

- part = `xc7a100tcsg324-1`；综合 top = `fir_filter`；仿真 top = `fir_filter_tb`。
- `validate_required` 被设成 `0`，所以 `checkRequiredFiles` 这一整段校验**被跳过**了——脚本不会提前告诉你文件找不到，而是会一直跑到 `import_files` 那一步才报错。
- 脚本里源文件路径都是 `../Vivado_projects/Fir_filter/Fir_filter.srcs/...`（见 [L175-L180](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.tcl#L175-L180)），而仓库里文件直接在 `Fir_filter.srcs/...` 下，**中间没有 `../Vivado_projects/Fir_filter/` 这一层**。

**预期结果**：在不修改任何东西的情况下，fresh clone 后 `source Fir_filter.tcl` 会在导入源文件时报「Could not find local file ...」并失败。这正是 README 里「OR use included src files in current project」这条替代路径存在的原因。

**最稳妥的替代做法（待本地验证）**：跳过 TCL，在 Vivado 里手动新建一个工程，part 选 `xc7a100tcsg324-1`，然后把 `Fir_filter.srcs/sources_1/new/` 下的 6 个 `.v` 作为设计源添加、把 `.xdc` 作为约束添加，再把仿真 top 手动设为 `fir_filter_tb`。这等价于把 TCL 的第 4–6 步用 GUI 完成，但避开了硬编码路径。

#### 4.2.5 小练习与答案

**练习 1**：脚本里 `origin_dir` 默认是什么？它的值会影响什么？
**答案**：默认是 `"."`（[L62](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.tcl#L62)）。所有源文件路径都拼在 `${origin_dir}/../Vivado_projects/...` 前面，所以 `origin_dir` 决定了脚本去哪里找源文件。可以用 `-tclargs --origin_dir <path>` 覆盖。

**练习 2**：为什么综合 top 和仿真 top 是不同的模块？
**答案**：综合 top `fir_filter` 是真正要变成硬件电路的滤波器；仿真 top `fir_filter_tb` 是 testbench，它内部例化（instantiate）了 `fir_filter` 并驱动输入、检查输出，本身不是可综合硬件。仿真器需要从 testbench 开始跑，所以仿真 top 必须是 `fir_filter_tb`。

**练习 3**：`create_run -name impl_1 ... -parent_run synth_1`（[L275](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.tcl#L275)）里的 `-parent_run synth_1` 是什么意思？
**答案**：实现 run 依赖综合 run 的输出（综合把 RTL 变成网表，实现再把网表映射/布线到具体芯片）。`-parent_run` 声明了这条依赖，让 Vivado 知道要先跑 `synth_1` 才能跑 `impl_1`。

---

### 4.3 XDC 约束

#### 4.3.1 概念说明

综合和实现只关心「逻辑对不对」。但一块 FPGA 板子上，芯片的每一根引脚都连着具体的外设（时钟、按键、LED、串口……）。**XDC 约束**就是告诉工具「我代码里的某个端口，对应芯片上的哪根物理引脚、用什么电平标准」。没有 XDC，工具不知道该把信号连到哪里，没法生成能下载到板子的比特流。

`Nexys-A7-100T-Master.xdc` 是 Digilent 官方为 Nexys A7-100T 整块板子提供的「主板级」约束模板，覆盖了板上所有外设。

#### 4.3.2 核心流程

XDC 里最常见的三类约束：

| 约束 | 作用 | 本项目中的例子 |
| --- | --- | --- |
| `PACKAGE_PIN` | 把端口绑定到某个物理引脚（如 `E3`） | 时钟引脚 `E3` |
| `IOSTANDARD` | 指定引脚电平标准（如 `LVCMOS33`） | 几乎都是 `LVCMOS33` |
| `create_clock` | 声明时钟周期，用于时序分析 | 周期 `10.00` ns |

关于时钟周期：周期 \(T = 10\,\text{ns}\) 对应频率

\[
f = \frac{1}{T} = \frac{1}{10\,\text{ns}} = 100\,\text{MHz}
\]

这正是板子上 `CLK100MHZ` 那 100 MHz 晶振的频率。

#### 4.3.3 源码精读

[Nexys-A7-100T-Master.xdc:1-4](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.srcs/constrs_1/imports/Vivado_projects/Nexys-A7-100T-Master.xdc#L1-L4) —— 文件头的使用说明，明确写了「uncomment the lines corresponding to used pins」（取消你用到引脚的注释）和「rename the used ports ... according to the top level signal names」（把端口名改成你顶层实际的信号名）。这是理解全文件「为什么全是注释」的钥匙。

[Nexys-A7-100T-Master.xdc:6-8](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.srcs/constrs_1/imports/Vivado_projects/Nexys-A7-100T-Master.xdc#L6-L8) —— 时钟约束段，两行都被 `#` 注释掉了：

- 第 7 行把端口 `CLK100MHZ` 绑到引脚 `E3`（`Sch=clk100mhz` 表示原理图上标的网络名）。
- 第 8 行声明这个时钟周期为 10 ns。

[Nexys-A7-100T-Master.xdc:29-45](https://github.com/Ghydra0/pipelined-FIR-filter-fpga/blob/4c6aedf850b021e36fcb0a3d86e0902704594eeb/Fir_filter.srcs/constrs_1/imports/Vivado_projects/Nexys-A7-100T-Master.xdc#L29-L45) —— LED 段，同样全部注释。每一行尾部的 `Sch=led[0]` 等是原理图网络名注释，方便对照。

**为什么全部被注释？** 两个原因叠加：

1. **这是模板，不是配置**：主板 XDC 覆盖了板上几十种外设，任何一个具体项目只会用到其中几个。官方做法是「默认全注释，用谁取消谁」。
2. **端口名对不上**：模板里的端口名是 `CLK100MHZ`、`SW[0]`、`LED[0]` 等通用名，而本项目顶层 `fir_filter` 的端口只有 `clk`、`xn`、`yn`（见上一讲）。即便取消注释，`get_ports { CLK100MHZ }` 也找不到对应端口。要让约束生效，必须同时改端口名，例如把时钟那行改成：

   ```
   set_property -dict { PACKAGE_PIN E3    IOSTANDARD LVCMOS33 } [get_ports { clk }];
   create_clock -add -name sys_clk_pin -period 10.00 -waveform {0 5} [get_ports {clk}];
   ```
   （以上为**示例代码**，不在仓库中。）

本项目目前是「仿真验证型」设计（有 testbench），并不打算立刻下板，所以保持全注释是合理的：仿真阶段根本不需要引脚约束。

#### 4.3.4 代码实践

**实践目标**：确认「全注释」这一事实，并理解端口名不匹配的问题。

**操作步骤**：

1. 打开 `Nexys-A7-100T-Master.xdc`，从头到尾扫一遍，确认**每一行有效约束都以 `#` 开头**。
2. 找到时钟段（约第 6–8 行），记下模板用的端口名 `CLK100MHZ`。
3. 对照上一讲：本项目顶层 `fir_filter` 的实际端口是 `clk`、`xn`、`yn`。
4. 思考：如果要下板，至少需要取消哪几行注释、并把端口名改成什么？

**需要观察的现象**：文件里不存在任何未被注释的 `set_property` / `create_clock` 行；时钟模板端口名（`CLK100MHZ`）与项目端口名（`clk`）不一致。

**预期结果**：结论——当前状态下 XDC 对综合/实现**不起任何实际约束作用**（注释行会被工具忽略）。这不妨碍仿真，但意味着设计还没有为「下载到 Nexys A7-100T」做好准备。

> 是否真的「全部注释」、以及具体哪些外设段存在，请以你本地打开文件看到的内容为准（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：XDC 里的 `#` 和 Tcl 里的 `#` 有没有区别？
**答案**：没有。XDC 本身就是 Tcl 语法，`#` 在两者中都是行注释符。这就是为什么「注释掉」一行约束就是行首加 `#`。

**练习 2**：如果完全删掉这个 `.xdc` 文件，仿真还能跑吗？综合还能跑吗？
**答案**：仿真完全不受影响——仿真不需要引脚约束。综合也能跑完（把设计变成网表不需要引脚），但到了「实现」阶段（尤其是 `write_bitstream` 生成下载比特流）会因为没有引脚约束而出错或无法下板。

**练习 3**：模板时钟行 `#create_clock ... -period 10.00 ...` 里，`-waveform {0 5}` 表示什么？
**答案**：它描述时钟波形在「第 0 ns 上升、第 5 ns 下降」，配合周期 10 ns，构成一个占空比 50% 的方波。这只是在时序分析里刻画时钟形状，不影响实际硬件晶振。

---

## 5. 综合实践

**任务**：假设你要在自己的电脑上把这个项目跑起来并仿真，请基于本讲内容写一份「上手指引」，覆盖以下几点：

1. **选择搭建方式**：你会用 `source Fir_filter.tcl`，还是手动新建工程？说明理由（提示：考虑 4.2.4 里的路径问题）。
2. **手动搭建清单**：如果选手动方式，列出需要添加的 6 个 `.v` 文件、1 个 `.xdc`，以及需要手动设置的三项配置（part、综合 top、仿真 top）。
3. **约束说明**：向你的同伴解释，为什么这个项目即使 `.xdc` 里所有行都是注释，也照样能仿真、能看波形。
4. **下板前瞻**：如果未来想把滤波器真的下载到 Nexys A7-100T 板子上，至少需要修改 XDC 的哪一段、把端口名改成什么？

**预期产出**：一份半页纸的中文指引，能准确引用本讲给出的行号与永久链接作为依据。本实践是「源码阅读 + 文档撰写」型任务，不需要真的运行 Vivado；如果你手边有 Vivado，可以顺势把工程搭起来（待本地验证）。

## 6. 本讲小结

- 项目目录遵循 Vivado 约定：`Fir_filter.srcs/sources_1/new/` 放 6 个 `.v` 设计/仿真源，`Fir_filter.srcs/constrs_1/imports/...` 放板级 XDC。
- `Fir_filter.tcl` 是工程重建脚本：它按 `create_project → 建 fileset → 导入源 → 设 top → 建 run` 的顺序，把整个工程凭空搭起来，目标 part 为 `xc7a100tcsg324-1`（Artix-7 / Nexys A7-100T）。
- 综合/实现顶层是 `fir_filter`，仿真顶层是 `fir_filter_tb`——同一个工程、两套顶层。
- 脚本里的源文件路径硬编码成了 `../Vivado_projects/Fir_filter/...`，且 `validate_required = 0` 跳过了文件存在性检查，因此 fresh clone 直接 source 容易失败，手动添加源文件是最稳妥的方式。
- XDC 是板级约束模板，全部行被注释；即便取消注释，模板端口名（`CLK100MHZ` 等）也与本项目的 `clk`/`xn`/`yn` 对不上。当前设计面向仿真，约束暂不起作用。

## 7. 下一步学习建议

本讲之后，你已经清楚「文件怎么放、工程怎么搭」，但还没有看任何一段 Verilog 实现。建议的下一步：

1. **先读叶子模块**：`adder.v`（加法器）和 `delay.v`（延迟寄存器）是整个设计里最简单的两块积木，几十行就能读完，适合作为进入 Verilog 源码的起点。
2. **再读定点乘法**：`multiplier.v` 涉及上一讲提到的 Q15 定点格式，是理解滤波器数值行为的关键。
3. **然后读单级抽头与顶层**：`fir_tap.v` 把「系数 + 延迟 + 乘加」组合成一级，`fir_filter.v` 再用 `generate` 把多级 tap 串成流水线。
4. **最后做验证**：`fir_filter_tb.v` 会告诉你如何给滤波器喂激励、检查输出，对应大纲里后续的「验证」讲义。

后续讲义将按「叶子模块 → 单级 tap → tap 链 → 验证」的顺序逐层拆解，本讲建立的「文件归属与工程骨架」认知会一直作为定位坐标。
