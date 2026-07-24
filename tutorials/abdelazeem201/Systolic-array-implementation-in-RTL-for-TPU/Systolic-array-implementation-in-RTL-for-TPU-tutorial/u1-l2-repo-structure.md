# 仓库目录结构与源码组织

## 1. 本讲目标

上一讲（[u1-l1](u1-l1-project-overview.md)）我们从概念上认识了「这个项目是什么」。本讲我们把视角落到硬盘上：**仓库里到底有哪些文件、它们被放在哪些目录、每个目录又在整条设计流程里扮演什么角色。**

学完本讲你应当能够：

- 识别 `rtl/` 下五个核心模块文件，并说出每个模块的一句话作用。
- 说清 `rtl/`（综合用）与 `Pre-Synthesis_Simulation/`（仿真用）这两份看起来重复的 RTL **到底有没有区别**。
- 了解 `syn/`、`pnr/`、`rtl/RTL_modified/` 三大目录分别承担什么职责。
- 学会用 `git ls-files` 这一个命令，独立摸清任何一个新仓库的文件归属。

---

## 2. 前置知识

在开始之前，用最通俗的话对齐几个术语（不熟悉 HDL 也能跟上）：

- **Verilog 文件（`.v`）**：硬件描述语言的一个源文件，里面通常定义一个 `module`（模块）。你可以把 module 想象成芯片里的一个「积木块」。
- **顶层模块（top module）**：把若干小积木块「拼」在一起的那个最大积木。本项目的顶层叫 `tpu_top`。
- **例化（instantiate）**：在一个模块里「摆放」并连线另一个模块，相当于把小积木装进大积木。
- **testbench（测试平台）**：一段不为做成真芯片、只用来在仿真器里「喂激励、看波形」的代码。
- **综合（synthesis）**：把 `.v` 翻译成由真实工艺单元（门、触发器）组成的网表的过程，常用工具是 Design Compiler（DC）。
- **布局布线（Place & Route, PnR）**：把综合出的网表摆到真实的硅片面积上、连好线，最终得到可以流片的版图（GDS），常用工具是 ICC2。
- **行为模型（behavioral model）**：仿真时用来「假装是一块 SRAM」的简化代码，只模仿读写时序，不关心真实电路。

承接 [u1-l1](u1-l1-project-overview.md) 的结论：项目分三块——**核心 RTL（算得对）**、**后端流程 syn/pnr（造得出）**、**扩展架构 RTL_modified（用得起）**。本讲就是把这三块「落到目录上」。

---

## 3. 本讲源码地图

| 文件 / 目录 | 角色 | 本讲用来做什么 |
| --- | --- | --- |
| `README.md` | 项目说明，给出模块清单与参数默认值 | 验证作者自述的「五大模块」与「参数」 |
| `rtl/tpu_top.v` | 核心设计的**顶层模块**，例化五个子模块 | 通过它的例化代码确认五个核心模块的名字与连接 |
| `rtl/*.v`（其余 5 个） | 五个核心子模块的「综合用」副本 | 认识模块文件清单 |
| `Pre-Synthesis_Simulation/` | 仿真目录，含 testbench 与 SRAM 行为模型 | 与 `rtl/` 对比，理解「两份 RTL」的关系 |
| `syn/`、`pnr/` | ASIC 后端两大阶段 | 了解脚本/约束/报告/产物的目录划分 |
| `rtl/RTL_modified/` | 更完整的扩展 TPU 架构 | 了解系统级集成的目录组织 |

---

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：**4.1 仓库的五大目录划分**，**4.2 核心模块文件清单与命名**。

### 4.1 仓库的五大目录划分

#### 4.1.1 概念说明

一个「从 RTL 到芯片」的数字设计项目，通常会沿设计流程把文件分目录存放：源码、仿真、综合、布局布线各占一格，互不污染。本项目就是把这条思路落到了目录上。

如果你在仓库根目录看一眼，会发现下面五大类目录（外加顶层的 `README.md`、`LICENSE`、`Pics/`）：

| 类别 | 目录 | 一句话职责 |
| --- | --- | --- |
| 核心 RTL（综合源） | `rtl/` | 五个核心模块的「权威源码」，综合工具从这里读 |
| 仿真 | `Pre-Synthesis_Simulation/` | testbench + SRAM 行为模型 + 测试数据 |
| 综合 | `syn/` | Design Compiler 脚本、约束、产物与报告 |
| 布局布线 | `pnr/` | ICC2 脚本、版图产物、功耗/电源网络报告 |
| 扩展架构 | `rtl/RTL_modified/` | 更完整的 TPU SoC 设计（含总线、FIFO、软件） |

> 一个关键认知：`rtl/` 与 `Pre-Synthesis_Simulation/` **都包含同一批核心模块的 `.v` 文件**，看起来是「重复」的。这一点很容易让初学者困惑，我们在 4.2 会专门验证它们的异同。

#### 4.1.2 核心流程

把五大目录串到设计流程上，是这样一条流水线（扩展架构 `RTL_modified/` 是另一条独立支线）：

```text
          ┌──────────────── 设计流程主线 ────────────────┐
 rtl/  ──► (综合工具读 .v) ──► syn/  ──► (PnR工具) ──► pnr/
  ▲                                                    │
  │ 同一批核心模块（副本）                              │
  └──► Pre-Synthesis_Simulation/  ──► (仿真器跑 testbench，功能验证)
                       │
                       └ 功能正确后，主线才继续往后端走

 rtl/RTL_modified/  ──► 独立的「更完整 TPU」支线（FPGA/SoC 集成方向）
```

要点：

1. **源码只有一份「权威」**：综合脚本 `syn/scripts/syn.tcl` 里明确写着从 `../rtl/` 读取设计，所以 `rtl/` 才是综合的输入。
2. **仿真目录是自包含的**：`Pre-Synthesis_Simulation/` 把核心模块、testbench、SRAM 模型、测试数据全放在一起，方便你直接 `cd` 进去跑仿真。
3. **后端两阶段是递进的**：`syn/`（综合）的产物（网表 `.v` + 约束 `.sdc`）会作为 `pnr/`（布局布线）的输入。
4. **扩展架构是平行支线**：`RTL_modified/` 不是主线的下一步，而是作者另起炉灶的一套「更完整、可上 SoC」的设计。

#### 4.1.3 源码精读

先看作者自己在 `README.md` 里对项目结构的描述——他把核心模块列成了五项：

[README.md:L5-L14](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/README.md#L5-L14) —— 中文说明：README 的「Project Structure」一节，用编号列表写明了 `systolic.v`、`systolic_controll.v`、`quantize.v`、`addr_sel.v`、`tpu_top.v` 五个文件及其作用。**这就是核心模块的官方清单。**

README 还给出了四个关键参数（注意它的「默认值」描述与真实代码有出入，见下方提示）：

[README.md:L46-L51](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/README.md#L46-L51) —— 中文说明：「Parameters」一节列出 `ARRAY_SIZE`、`SRAM_DATA_WIDTH`、`DATA_WIDTH`、`OUTPUT_DATA_WIDTH` 四个参数。

> ⚠️ 一个本讲就要点破的「坑」：README 说 `ARRAY_SIZE` 默认是 32，但真正的 `rtl/tpu_top.v` 第 2 行写的是 `parameter ARRAY_SIZE = 8`。**以源码为准，默认是 8×8**（这一点在 [u1-l1](u1-l1-project-overview.md) 已建立，这里再确认一次）。

再看综合脚本如何「点名」要从哪个目录读源码——这是判断「谁是权威源码」的硬证据：

[rtl/tpu_top.v:L1-L6](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L1-L6) —— 中文说明：顶层模块 `tpu_top` 的声明与四个参数（`ARRAY_SIZE=8`、`SRAM_DATA_WIDTH=32`、`DATA_WIDTH=8`、`OUTPUT_DATA_WIDTH=16`）。注意它**不是** README 说的 32。

而 `syn/scripts/syn.tcl` 第 14 行的命令是：

```tcl
analyze -library work -format verilog ../rtl/${design}.v
```

这说明综合工具用相对路径 `../rtl/` 读取设计——**`rtl/` 目录就是综合的权威输入**。

#### 4.1.4 代码实践

**实践目标**：亲手确认仓库根目录下确实存在上述五大类目录，建立「目录=职责」的直觉。

**操作步骤**：

1. 在仓库根目录执行：
   ```bash
   git ls-files | cut -d/ -f1 | sort -u
   ```
   这条命令列出所有被 git 跟踪的文件，只取第一级路径，再去重排序——得到的就是「顶层有哪些目录/文件」。
2. 再执行 `ls` 肉眼对照一下（`ls` 还会显示未被 git 跟踪的本地文件）。

**需要观察的现象**：第 1 步应当输出类似 `LICENSE`、`Pics`、`Pre-Synthesis_Simulation`、`README.md`、`pnr`、`rtl`、`syn` 这样的清单——正好对应本节列的五大类加顶层杂项。

**预期结果**：你能在输出里数出与「核心 RTL / 仿真 / 综合 / 布局布线 / 扩展架构」一一对应的顶层目录名。（本实践为只读命令，不会改动任何文件。）

#### 4.1.5 小练习与答案

**练习 1**：为什么综合脚本要写 `../rtl/${design}.v`，而不是直接从 `Pre-Synthesis_Simulation/` 读？

> **参考答案**：`rtl/` 是「纯 RTL」目录，只放可综合的设计源码；`Pre-Synthesis_Simulation/` 里混了 testbench 和 SRAM 行为模型，这些不可综合、综合工具不应读入。用 `../rtl/` 能保证综合输入干净。

**练习 2**：`README.md` 与 `rtl/tpu_top.v` 在 `ARRAY_SIZE` 默认值上矛盾，应以谁为准？为什么？

> **参考答案**：以 `rtl/tpu_top.v`（源码）为准，默认是 `8`。文档可能滞后于代码，源码才是最终被执行的「事实」。这也提醒我们：读项目时，**文档负责建立直觉，代码负责确认事实**。

---

### 4.2 核心模块文件清单与命名

#### 4.2.1 概念说明

`rtl/`（平铺，无子目录）下放着六个 `.v` 文件，其中一个是顶层 `tpu_top.v`，另外五个是被它例化的子模块。这五个子模块合起来就是「能做一次矩阵乘」的最小完整设计。

清单如下（按「数据从输入到输出」的顺序排列，方便记忆）：

| 文件（`rtl/` 下） | 模块名 | 一句话作用 |
| --- | --- | --- |
| `addr_sel.v` | `addr_sel` | 把单一地址序号解码成 4 路 SRAM 读地址，制造输入歪斜 |
| `systolic.v` | `systolic` | 8×8 脉动阵列本体：移位队列 + 乘加 + 结果收集 |
| `systolic_controll.v` | `systolic_controll` | 主控状态机：产出各类控制与计数信号 |
| `quantize.v` | `quantize` | 把 21bit 乘加结果饱和量化成 16bit 输出 |
| `write_out.v` | `write_out` | 把量化结果按反对角线重排写回 a/b/c 三组 SRAM |
| `tpu_top.v` | `tpu_top` | 顶层：例化上面五个模块并连线 |

关于「两份重复 RTL」的最终结论（我们在 4.2.4 会亲手验证）：

- `rtl/` 和 `Pre-Synthesis_Simulation/` 里的**同名核心模块文件内容完全一致**（逐字节相同），只是仿真目录还**多带**了 testbench、SRAM 模型和测试数据。
- 此外还有一个 `rtl/systolic array/` 子目录，里面是一份**带注释、带 `Readme.md` 的「教学版」副本**，模块名拼写成 `systolic_controller`（带 `er`），且默认 `ARRAY_SIZE=32`——这是另一套写法，**不是**综合用的源码。

> ⚠️ 另一个小「坑」：`rtl/` 下还有一个**没有扩展名**的文件 `rtl/tpu_top`。它和 `rtl/tpu_top.v` 内容完全相同，只是丢了 `.v` 后缀，很可能是误留的副本。综合工具按 `${design}.v` 匹配文件名，所以这个无后缀文件**不会被读到**——看到它时不必困惑。

#### 4.2.2 核心流程

顶层 `tpu_top` 的职责就是「把五个积木摆好并连线」。它的连接关系可以用下面这段伪代码表示（先看数据怎么流，具体信号在下一讲精读）：

```text
                 tpu_start (外部启动)
                      │
              ┌───────▼────────┐
              │ systolic_controll │ ──► 各类控制/计数信号 (cycle_num, matrix_index, ...)
              └───────┬────────┘
                      │ 控制信号
   weight/data SRAM ──┼──► ┌─────────┐
   (4 路读数据)        │     │ addr_sel │ ──► 4 路 SRAM 读地址（歪斜喂入）
                      │     └────┬─────┘
                      │          │ 读地址 → 去 SRAM 取 weight/data
                      ▼          ▼
                   ┌──────────────┐
                   │   systolic   │ ──► ori_data（21bit 乘加原始结果）
                   └──────┬───────┘
                          │
                   ┌──────▼──────┐
                   │  quantize   │ ──► quantized_data（16bit 量化结果）
                   └──────┬──────┘
                          │
                   ┌──────▼──────┐
                   │  write_out  │ ──► a/b/c 三组输出 SRAM（写回）
                   └─────────────┘
```

一句话：**控制器指挥 → 地址选择喂入 → 阵列计算 → 量化 → 写回**。

#### 4.2.3 源码精读

我们用 `tpu_top.v` 里的五处例化来「点名」五个核心模块。每处例化都形如 `模块名 例化名 ( ...端口连接... );`，正好对应清单里的五个文件。

`addr_sel` 例化（无参数）：

[rtl/tpu_top.v:L64-L77](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L64-L77) —— 中文说明：例化地址选择模块，输入 `addr_serial_num`，输出 4 路 SRAM 读地址。

`quantize` 例化（带 4 个参数）：

[rtl/tpu_top.v:L79-L92](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L79-L92) —— 中文说明：例化量化模块，把 21bit 的 `ori_data` 变成 16bit 的 `quantized_data`。

`systolic` 例化（阵列本体）：

[rtl/tpu_top.v:L94-L117](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L94-L117) —— 中文说明：例化脉动阵列，吃 weight/data 与控制信号，吐出 `mul_outcome`（即 `ori_data`）。

`systolic_controll` 例化（控制器）：

[rtl/tpu_top.v:L119-L137](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L119-L137) —— 中文说明：例化主控状态机，吃 `tpu_start`，产出 `sram_write_enable`、`addr_serial_num`、`alu_start`、`cycle_num`、`matrix_index`、`data_set`、`tpu_done` 等控制信号。

`write_out` 例化（写回）：

[rtl/tpu_top.v:L139-L165](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L139-L165) —— 中文说明：例化写回模块，把 `quantized_data` 按反对角线重排后写到 a/b/c 三组输出 SRAM。

此外，顶层第 41 行有一个 `localparam`，把「原始位宽」算出来，供 `ori_data` 线使用：

[rtl/tpu_top.v:L41-L41](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L41-L41) —— 中文说明：`localparam ORI_WIDTH = DATA_WIDTH+DATA_WIDTH+5;`，即 8+8+5=21bit，这正是乘加中间结果的位宽（下一讲会展开）。

#### 4.2.4 代码实践

**实践目标**：亲手验证「`rtl/` 与 `Pre-Synthesis_Simulation/` 里的核心模块是同一份代码」，从而真正理解这两个目录的关系。

**操作步骤**：

1. 在仓库根目录执行（任选一个模块，比如 `tpu_top`）：
   ```bash
   diff rtl/tpu_top.v Pre-Synthesis_Simulation/tpu_top.v
   ```
2. 如果没有任何输出，说明两份文件**完全相同**。再换 `systolic.v`、`addr_sel.v`、`quantize.v`、`write_out.v`、`systolic_controll.v` 各试一次。
3. 然后列出两个目录各自的文件清单做对比：
   ```bash
   ls rtl/*.v
   ls Pre-Synthesis_Simulation/
   ```

**需要观察的现象**：

- 第 1、2 步：六个核心模块的 `diff` 都**没有输出**（即逐字节相同）。
- 第 3 步：`Pre-Synthesis_Simulation/` 比 `rtl/` **多出** `test_tpu.v`（testbench）、四个 `sram_*.v`（SRAM 行为模型）、以及 `mat1.txt`/`mat2.txt`/`golden1.txt`/`golden2.txt`/`golden3.txt`（测试数据）。

**预期结果**：你会得出结论——两个目录的核心设计源码是**同一份的副本**，差别只在于仿真目录额外「打包」了跑仿真所需的一切（testbench、SRAM 模型、数据）。这样设计的目的是让你 `cd Pre-Synthesis_Simulation/` 就能直接仿真，而综合工具只盯着干净的 `rtl/`。（本实践为只读命令。）

> 待本地验证：如果你使用 Windows，`diff` 可换成 `FC`；不同仿真器对文件名大小写、空格目录（如 `rtl/systolic array/`）的处理可能不同，以你本机实际为准。

#### 4.2.5 小练习与答案

**练习 1**：`rtl/systolic array/` 子目录里的控制器文件叫 `systolic_controller.v`，而 `rtl/` 下叫 `systolic_controll.v`，两者拼写不同。这会带来什么影响？

> **参考答案**：模块名/文件名不同，意味着它们是两套独立维护的代码，不能互相替换。综合脚本按 `../rtl/${design}.v` 找文件，用的是 `systolic_controll`（无 `er`）这套；`systolic array/` 子目录是带注释的「教学版」，默认 `ARRAY_SIZE=32` 也与主线不同，属于参考资料而非综合输入。

**练习 2**：假如你把 `rtl/tpu_top.v` 删掉，只留下没扩展名的 `rtl/tpu_top`，综合会怎样？

> **参考答案**：综合会失败/找不到顶层。因为 `syn.tcl` 用 `${design}.v` 拼文件名，只会去找 `tpu_top.v`，而不会匹配无后缀的 `tpu_top`。这个无后缀文件实际上是「死副本」。

**练习 3**：`Pre-Synthesis_Simulation/` 里有四个 `sram_*.v`，它们为什么不出现在 `rtl/` 里？

> **参考答案**：它们是 SRAM 的**行为模型**，只在仿真里「假装是一块内存」，本身不可综合。综合阶段真实 SRAM 会被替换成工艺库里的宏单元（或由后端插入），所以不放进综合源码目录 `rtl/`。

---

## 5. 综合实践

把本讲全部内容串起来，完成规格里要求的那张「文件归属表」。

**实践目标**：用 `git ls-files` 这一个命令，把整个仓库的全部文件，按 **核心 RTL / 仿真 / 综合 / 布局布线 / 扩展架构** 五类整理成一张归属表，并标注每个核心模块的存放路径。

**操作步骤**：

1. 在仓库根目录导出完整文件清单，建议重定向到文本里慢慢看：
   ```bash
   git ls-files > /tmp/repo_files.txt
   wc -l /tmp/repo_files.txt       # 看总文件数
   ```
2. 按目录前缀分类统计（快速核对每类的文件数量）：
   ```bash
   for d in rtl Pre-Synthesis_Simulation syn pnr; do
     echo "$d : $(git ls-files "$d" | wc -l) 个文件"
   done
   ```
   注意：`rtl/RTL_modified/` 属于「扩展架构」类，统计核心 RTL 时应把它排除。
3. 整理出一张归属表（参考下面的答案）。

**需要观察的现象**：每条路径都能被归入五类之一；五个核心模块在 `rtl/` 与 `Pre-Synthesis_Simulation/` 两处都能找到同名副本。

**预期结果（参考归属表）**：

| 类别 | 代表路径 | 核心模块存放路径 |
| --- | --- | --- |
| 核心 RTL（综合源） | `rtl/tpu_top.v`、`rtl/systolic.v`、`rtl/systolic_controll.v`、`rtl/addr_sel.v`、`rtl/quantize.v`、`rtl/write_out.v` | `rtl/`（平铺） |
| 仿真 | `Pre-Synthesis_Simulation/test_tpu.v`、`sram_*.v`、`mat*.txt`、`golden*.txt` | 同目录下也有六个核心模块的**同名副本** |
| 综合 | `syn/scripts/syn.tcl`、`syn/cons/cons.tcl`、`syn/output/`、`syn/report/` | — |
| 布局布线 | `pnr/scripts/0..6_*.tcl`、`pnr/output/`、`pnr/pna_output/` | — |
| 扩展架构 | `rtl/RTL_modified/top.v`、`busConn.v`、`tpu.v`、`tpu_system.v`、`weightFifo/`、`software/`、`benchmarking/` | 独立一套模块（`master_control`、`sysArr` 等在 `top.v` 内） |

> 待本地验证：随仓库版本变化，文件数量与具体报告文件名可能微调，以你本地 `git ls-files` 的实际输出为准；但五大类的目录划分是稳定的。

---

## 6. 本讲小结

- 仓库按设计流程分成五大类目录：`rtl/`（核心 RTL）、`Pre-Synthesis_Simulation/`（仿真）、`syn/`（综合）、`pnr/`（布局布线）、`rtl/RTL_modified/`（扩展架构）。
- 五个核心模块是 `tpu_top`（顶层）、`systolic`（阵列）、`systolic_controll`（控制器）、`addr_sel`（地址选择）、`quantize`（量化）、`write_out`（写回）——**注意算上顶层其实是六个 `.v` 文件，其中五个是子模块**。
- `rtl/` 与 `Pre-Synthesis_Simulation/` 的核心模块**逐字节相同**，后者只是额外打包了 testbench、SRAM 模型和测试数据。
- 综合脚本 `syn/scripts/syn.tcl` 用 `../rtl/${design}.v` 读源码——**`rtl/` 是权威综合输入**。
- 文档与代码有出入时（如 `ARRAY_SIZE` 默认 32 还是 8）**以源码为准**，默认是 8。
- `rtl/systolic array/` 是带注释的教学版副本，`rtl/tpu_top`（无后缀）是死副本——两者都不是综合输入，看到不必困惑。

---

## 7. 下一步学习建议

现在你已经知道「文件在哪、各管什么」。下一讲 [u1-l3 顶层模块 tpu_top 与系统级数据流](u1-l3-tpu-top-datapath.md) 会**钻进 `rtl/tpu_top.v` 内部**，精读它的端口（8 路 SRAM 读数据、三组写回端口、`tpu_done`）和五个子模块之间的 wire 连接，画出真正的顶层框图。

建议你提前做一件事：打开 `rtl/tpu_top.v`，对照本讲的「核心流程」伪代码图，试着自己先找出五处例化，确认你能把每个子模块的输入输出与图上的箭头对应起来——这就是下一讲的起点。
