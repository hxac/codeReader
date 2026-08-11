# 目录结构与模块全景

## 1. 本讲目标

在上一讲（[u1-l1 项目总览](u1-l1-project-overview.md)）里，我们建立了对 OH! 的宏观印象：一个用标准 Verilog 2005 写成的开源硬件构建模块库，并定下了一条贯穿全手册的阅读原则——**代码与协议文件才是事实，文档（README、脚本）可能滞后，需多处互相核对**。

本讲就把这条原则真正用起来。读完本讲，你应当能够：

1. 画出 OH! 仓库的**实际顶层目录树**，并说出每个目录装了什么。
2. 理解 OH! 模块内部统一的子目录约定（`hdl/`、`rtl/`、`dv/`、`fpga/`、`driver/`、`docs/` 等），拿到一个陌生模块时知道去哪里找 RTL、去哪里找测试。
3. 区分三类基础库 **stdlib / asiclib / stdcells** 的职责差异（可综合 RTL / ASIC 硬核单元 / 晶体管级），并知道 **xilibs** 在 FPGA 仿真中扮演什么角色。
4. 把所有顶层模块归到六大功能类别（基础库 / 协议 / 链路 / 外设 / 系统互连 / 物理与板级）。
5. 识别 README 与脚本中**与实际目录不一致**的地方（例如脚本里写的 `src/` 路径其实并不存在），并知道遇到这种不一致时该信谁。

---

## 2. 前置知识

本讲几乎不需要数字电路知识，但下面几个词最好先眼熟（上一讲已介绍）：

- **HDL / Verilog 2005**：描述硬件的语言与版本，OH! 全库统一使用。
- **RTL（Register Transfer Level）**：寄存器传输级，即可综合的硬件描述，介于纯逻辑与底层晶体管之间。
- **ASIC / FPGA**：专用集成电路 / 现场可编程门阵列。同一个功能在 OH! 里常常有「面向 FPGA 的可综合 RTL」和「面向 ASIC 流片的硬核单元」两套实现。
- **模块（module）**：Verilog 里一个可复用的硬件单元，OH! 约定一个文件只放一个 module。
- **SI / FPGA / HH**：README 用的成熟度标签，分别表示「流片验证」「FPGA 验证」「施工区（Hard Hat）」。本讲会用它来给模块排队，建议优先学习 SI/FPGA 的模块。
- **PDK（Process Design Kit）**：晶圆厂提供的工艺设计包。ASIC 单元最终要绑定到某个具体工艺。

> 一句话回顾：OH! = 数字电路的「乐高积木」。本讲要做的事，就是把这盒乐高**倒出来分类**——先看清有多少种零件、各自放在哪个格子里，再谈怎么拼。

---

## 3. 本讲源码地图

本讲主要阅读文档与目录结构本身，涉及的「源码」其实是项目根目录与各模块的说明文件：

| 文件 / 目录 | 作用 |
|-------------|------|
| `README.md` | 项目总入口，含模块表、仿真/构建说明、设计/编码/文档规范、License。 |
| `setenv.sh` | 设置环境变量 `OH_HOME`，仿真脚本依赖它定位仓库根目录。 |
| `run.sh` | 「一键仿真」快捷脚本。**它会成为本讲发现文档与实际不一致的关键证据**。 |
| `scripts/build.sh` | iverilog 编译脚本，是观察「模块路径约定」的窗口。 |
| `stdlib/README.md` | 基础库 stdlib 的自述（可综合 RTL 原语）。 |
| `asiclib/README.md` | 基础库 asiclib 的自述（绑定 PDK 的硬核单元）。 |
| `emesh/README.md` | emesh 片上网络接口的自述。 |
| `elink/README.md` | elink 高速链路的自述（本讲引用它的「设计结构」树状图）。 |

> 这些 README 大多非常短（`emesh/README.md` 只有两行），但它们是理解「这个目录为什么存在」的最快入口。

---

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

- **4.1 目录布局**：仓库长什么样、子目录命名约定是什么。
- **4.2 模块职责**：每个顶层模块干什么、如何归类、文档与实际有哪些出入。

---

### 4.1 目录布局

#### 4.1.1 概念说明

打开一个陌生仓库，第一件事不是读代码，而是**摸清它的地形**：顶层有哪些目录？每个目录里的子目录又分别代表什么？一个清晰的项目通常会有固定的子目录约定，让你不用读文档就能猜到「RTL 在哪、测试在哪、FPGA 工程在哪」。

OH! 就是一个约定相当一致的仓库。绝大多数模块都遵循下面这套「角色分工」的子目录命名：

| 子目录 | 全称 | 放什么 |
|--------|------|--------|
| `hdl/` | Hardware Description Language | 可综合的 RTL 设计文件（`.v`）。OH! 中最常见的设计目录名。 |
| `rtl/` | Register Transfer Level | 同样是设计文件，含义与 `hdl/` 一样，只是少数模块（`stdlib`、`padring`）用了这个叫法。 |
| `dv/` | Design Verification | 仿真验证：testbench、`dut_*.v` 包装、`.emf` 测试激励、波形等。 |
| `docs/` | Documentation | 该模块的文档、框图（`.png`）、地址表等。 |
| `fpga/` | FPGA build | FPGA 综合脚本与约束（Vivado 等）。 |
| `driver/` | Software driver | 配套的软件/固件驱动（如 Linux 内核风格的 `.c`）。 |
| `sw/` `firmware/` | Software / Firmware | 其它软件或固件资源。 |
| `include/` | Include | 头文件（`.vh`）、常量定义。 |
| `ip/` | IP | 厂商 IP 核（如 Xilinx 的 `.xci`）。 |

记住这条规律，你就能在任何一个 OH! 模块里迅速定位文件。

#### 4.1.2 核心流程

理解一个 OH! 模块目录的标准「浏览流程」如下：

```text
进入某个模块目录（例如 gpio/）
   │
   ├─ 先看 README.md          → 搞清「它是什么、寄存器表、怎么仿真」
   │
   ├─ 进 hdl/（或 rtl/）        → 看可综合设计本体（gpio.v 等）
   │
   ├─ 进 dv/                   → 看 testbench、dut 包装、.emf 测试
   │
   └─（可选）进 fpga/ docs/    → 看 FPGA 工程、框图、地址映射
```

而理解整个仓库的流程则是「先看根目录全景 → 再按功能分类 → 最后逐个模块深入」。本讲的 4.2 节就负责中间这一步。

#### 4.1.3 源码精读

**(a) 顶层目录全景**

仓库根目录下实际存在这些目录（用 `ls` 即可看到）：

```text
asiclib/   axi/      docs/      edma/      elink/     emailbox/
emesh/     emmu/     etrace/    gpio/      mio/       padring/
parallella/ spi/     stdcells/  stdlib/    xilibs/    scripts/
```

加上根目录的 `README.md`、`LICENSE`、`AUTHORS`、`setup.py`、`setenv.sh`、`run.sh`，就是全部。

**(b) 各模块的子目录结构**

下面这张表是「实地探测」的结果（非抄自 README）。注意 `stdlib` 与 `padring` 用的是 `rtl/`，而其它设计目录用的是 `hdl/`：

| 模块 | 子目录 | 说明 |
|------|--------|------|
| `stdlib` | `rtl/` `testbench/` `fpga/` `firmware/` | 基础库，设计在 `rtl/`，还自带通用 testbench（后面单元会重点用） |
| `asiclib` | `hdl/` | 基础库（硬核单元） |
| `stdcells` | `hdl/` `dv/` | 晶体管级 `.sv` 单元（`hdl/`）与它们的小测试（`dv/`） |
| `xilibs` | `dv/` `ip/` | Xilinx 原语的**仿真模型**（`dv/`）与 IP（`ip/`） |
| `emesh` | `hdl/` `dv/` `docs/` | emesh 协议电路 |
| `elink` | `hdl/` `dv/` `docs/` `fpga/` `sw/` `include/` | 子目录最全的高速链路 |
| `axi` | `hdl/` `dv/` | AXI 桥 |
| `edma` | `hdl/` `dv/` | DMA 引擎 |
| `mio` | `hdl/` `dv/` `docs/` `driver/` | 轻量链路 |
| `gpio` | `hdl/` `dv/` `fpga/` `driver/` | GPIO 外设 |
| `spi` | `hdl/` `dv/` `fpga/` | SPI 主/从 |
| `emailbox` `emmu` `etrace` | `hdl/` `dv/` | 三个小外设 |
| `padring` | `rtl/` `dv/` | 焊盘环生成器（设计在 `rtl/`） |
| `parallella` | `hdl/` `fpga/` | Parallella 板的 FPGA 顶层 |

**一个有意义的数字**：全仓库（不含本教程目录）共有约 **408 个 `.v` 文件**，其中 `stdlib/rtl` 有 144 个、`asiclib/hdl` 有 110 个、`elink/hdl` 有 22 个、`emesh/hdl` 有 11 个。可见「积木」绝大多数集中在两个基础库里——这也解释了为什么学习手册要把 stdlib/asiclib 排在最前面。

**(c) 环境变量与脚本入口**

仿真流程依赖 `OH_HOME` 指向仓库根目录。`setenv.sh` 只有一行有效内容，就是把当前目录赋给它：

[setenv.sh:4](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/setenv.sh#L4) —— 定义 `export OH_HOME=$PWD`，所有 `scripts/*.sh` 都通过 `$OH_HOME` 定位库文件。

而 `build.sh` 的注释里直接示范了「正确」的模块路径写法（不带 `src/`）：

[scripts/build.sh:8](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/scripts/build.sh#L8) —— 注释示例 `./scripts/build.sh elink/hdl/dut_elink.v`，路径直接从顶层模块名开始。

[scripts/build.sh:15-19](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/scripts/build.sh#L15-L19) —— 实际编译命令：`iverilog -g2005 ... -f $OH_HOME/scripts/libs.cmd -o dut.bin $1`，把用户传入的顶层文件 `$1` 编译成 `dut.bin`。

**(d) 文档与实际不一致的铁证**

现在我们用到上一讲那条「代码是事实、文档可能滞后」的原则。看一眼「一键仿真」脚本 `run.sh`：

[run.sh:3-4](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/run.sh#L3-L4) —— 它调用的是 `src/$1/dv/dut_$1.v` 和 `src/$1/dv/tests/test_basic.emf`。

但**仓库里根本没有 `src/` 这个目录**！用 `ls` 看顶层就能确认：模块直接散在根目录下（`gpio/`、`elink/`……），并不嵌套在 `src/` 里。也就是说，按 README 推荐的 `./run.sh gpio` 直接跑，会因找不到 `src/gpio/dv/dut_gpio.v` 而失败。

更有意思的是，同一个仓库里存在**两套互相矛盾的路径约定**：

- README 的「How to simulate」示例用的是 `gpio/dv/dut_gpio.v`（正确，无 `src/`）——见 [README.md:67-91](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/README.md#L67-L91)。
- 而 `run.sh` 用的是 `src/gpio/...`（错误）——见 [run.sh:3-4](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/run.sh#L3-L4)。

> 结论：**遇到脚本/文档里的路径，先用 `ls` 验证它是否真的存在，再执行**。这是阅读 OH!（以及很多历史较长的开源项目）最重要的习惯之一。这也正是为什么本手册的仿真讲义（u1-l3）会专门讲「如何绕开 `src/` 路径假设」。

#### 4.1.4 代码实践

> **实践类型：源码阅读 / 目录探测型实践**（本讲重在摸清地形，不运行仿真）。

**实践目标**：亲手验证「README 列的模块」与「实际存在的目录」之间的差异，养成「先 `ls` 后执行」的习惯。

**操作步骤**：

1. 在仓库根目录执行 `ls -1`，把实际顶层目录抄下来。
2. 打开 [README.md:40-58](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/README.md#L40-L58) 的 Modules 表，把表里列的模块名抄下来。
3. 做一张「三栏对照表」：`README 列出 | 实际是否存在 | 备注`。
4. 单独验证 `run.sh` 的 `src/` 假设：执行 `test -d src && echo yes || echo no`。

**需要观察的现象**：

- README 列了 `accelerator`、`chip`、`common`、`pic`、`risc-v` 等，但它们**在当前顶层目录中都不存在**。
- 实际存在的 `asiclib`、`stdcells`、`stdlib`、`padring` 在 README 模块表里**并没有单独列出**。
- `test -d src` 的结果是 `no`，证明 `run.sh` 的路径假设在当前版本是失效的。

**预期结果**：你会得到一张清晰说明「文档滞后于实际布局」的对照表。这正是后续讲义中所有「路径以实际目录为准」的依据。

**待本地验证**：不同 Git 标签（例如 README 提到的稳定版 `V1.0`）下，目录布局可能不同；本讲结论基于当前 HEAD `7edfcb5`。若你检出到其它版本，请重新执行第 1 步。

#### 4.1.5 小练习与答案

**练习 1**：`stdlib` 的设计文件放在 `rtl/`，而 `elink` 的设计文件放在 `hdl/`。这两个目录名含义一样吗？

> **答案**：含义基本一样，都指「可综合的 RTL 设计」。OH! 早期（或在某些模块里）用 `rtl/`，后来多数模块改用 `hdl/`。看到任意一个都应理解为「设计本体在这里」。

**练习 2**：你想看 `gpio` 的仿真波形，应该去 `gpio/` 下的哪个子目录找测试激励？

> **答案**：去 `gpio/dv/`。按约定 `dv/` 放 testbench、`dut_*.v` 包装与 `.emf` 测试激励。设计本体则在 `gpio/hdl/`。

**练习 3**：`xilibs` 目录里的 `.v` 文件是「真正的硬件设计」吗？

> **答案**：不是。`xilibs/dv/` 下（如 `IDDR.v`、`MMCME2_ADV.v`、`IBUF.v`）是 **Xilinx 厂商原语的行为级仿真模型**，用来在 iverilog 仿真时替换 FPGA 黑盒，让仿真能跑通；它们不是要被综合成电路的设计。

---

### 4.2 模块职责

#### 4.2.1 概念说明

光知道目录长什么样还不够，还得知道**每个目录负责什么**。OH! 的十几个顶层模块可以归到六大功能类别。理解这层分类，你就能回答：

- 「我想找个 FIFO 用，去哪？」→ 基础库 `stdlib`。
- 「外设和我通信用的是什么协议？」→ `emesh`。
- 「FPGA 和 ASIC 之间怎么传数据？」→ 高速链路 `elink`（或轻量的 `mio`）。
- 「我要把芯片的 IO 焊盘排一圈，用什么？」→ 物理设计 `padring`。

本节先给出**功能分类全景**，再逐类点明职责，最后回到 README 自述，看看每个模块「自己怎么说自己」。

#### 4.2.2 核心流程

把模块分类的思路是一条「自底向上」的抽象阶梯：

```text
最底层  基础库    stdlib(可综合RTL) / asiclib(硬核) / stdcells(晶体管) / xilibs(仿真模型)
   ↑      ↑
公共语言  协议     emesh —— 所有外设/链路共用的 104 位包协议
   ↑      ↑
功能单元  外设     gpio / spi / emailbox / emmu / etrace
   ↑      ↑
点对点通信 链路    elink(高速 LVDS) / mio(轻量并行)
   ↑      ↑
总线接驳  系统互连  axi(AXI 桥) / edma(DMA)
   ↑      ↑
芯片/板级 物理顶层  padring(焊盘环) / parallella(板级 FPGA 顶层)
```

这条阶梯也正好对应学习手册的单元顺序：先会用基础原语（u2/u3）→ 懂协议（u5）→ 读系统（u6/u7/u8）→ 做物理实现（u9）。

#### 4.2.3 源码精读

**(a) 三类基础库的自述**

基础库是整个 OH! 的地基，三个库各司其职，它们的 README 把区别说得很清楚：

- **stdlib（可综合 RTL 原语）**——这是「软件可综合」的那一面：

[stdlib/README.md:1-5](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/README.md#L1-L5) —— 自述要点：「low level vectorized building blocks for control and datapath logic（面向控制与数据通路的底层向量化构件）」，并且「parameters are included to enable soft and hard-coded implementation（用参数在 soft 与 hard 实现间切换）」。

- **asiclib（绑定 PDK 的硬核单元）**——这是「面向 ASIC 流片」的那一面：

[asiclib/README.md:1-7](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/README.md#L1-L7) —— 自述要点：「hard-coded to a specific PDK（绑定到具体工艺库）」，`hdl/*.v` 是「golden model（黄金参考模型）」，真正的硬核实现必须**逐比特复刻**这套行为；「linked in at compile time based on the foundry（按代工厂在编译期链接进来）」；并且「cells do not have any dependencies（单元之间无依赖）」。

> 对比要点：`stdlib` 的 `oh_dffq.v` 是「可以直接综合进 FPGA/芯片」的 RTL；`asiclib` 的 `asic_dffq.v` 则是「晶圆厂给的硬核的行为级描述」，综合时会被替换成厂家的真实单元。二者同名同功能，只是一个 soft、一个 hard。这正是 OH! 「双实现」哲学的根（详见 u9-l1）。

- **stdcells（晶体管级，教学/原理）**——比 RTL 更底层：

`stdcells/hdl/` 下只有三个 `.sv`（SystemVerilog）文件：`oh_nmos.sv`、`oh_pmos.sv`、`oh_nand2.sv`，即用 `nmos`/`pmos` 开件画出的晶体管级与非门。它们更多用于**理解原理**，不是日常设计中直接例化的对象。

- **xilibs（厂商原语仿真模型）**——让 FPGA 设计能在开源仿真器里跑起来：

如前所述，`xilibs/dv/` 里的 `IDDR.v`、`MMCME2_ADV.v`、`IBUF.v` 等是 Xilinx 原语的行为模型。当 `elink` 这类模块在 FPGA 实现里例化了 `IDDR`/`MMCM` 等黑盒时，iverilog 需要这些模型才能仿真。

**(b) 协议层：emesh**

[emesh/README.md:1-2](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/README.md#L1-L2) —— 只有一句：「Various emesh interface circuits（各种 emesh 接口电路）」。

虽然 README 极简，但 `emesh` 是 OH! 体系中**最关键的公共语言**：所有外设（gpio/spi/…）和链路（elink/mio）之间都用一种统一的 104 位「事务包」通信，这套包格式就叫 emesh。把它单独拎出来讲（u5 单元），是因为读懂了 emesh，就读懂了 OH! 一大半模块的接口。

**(c) 链路层：elink / mio**

`elink` 是 OH! 里文档最完整的模块，README 给出了一张非常清晰的内部结构树：

[elink/README.md:1-6](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L1-L6) —— 定位：FPGA 与 ASIC 之间低延迟、高速的点对点 LVDS 链路，可达 8 Gbit/s（双工）。

[elink/README.md:136-173](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L136-L173) —— 「Design structure」树状图：`elink` 内部由 `emaxi/esaxi`（AXI 接口）、`erx`（接收通路，含 `erx_io/erx_protocol/erx_fifo/erx_arbiter` 等）、`etx`（发送通路，结构对称）组成。这张图是后续 u7 单元（elink 链路）的导航地图。

`mio` 则是 elink 的「轻量版」——一种更简单的并行链路（详见 u8-l4）。

**(d) 六大功能类别全景表**

把上面所有信息汇总，OH! 的顶层模块可以这样归类：

| 类别 | 模块 | 一句话职责 | 成熟度（README） |
|------|------|-----------|------------------|
| **基础库** | `stdlib` | 可综合 RTL 原语：FIFO、DFF、mux、时钟、同步、计数、仲裁等 | （README 作 `common`/SI） |
| | `asiclib` | 绑定 PDK 的硬核单元（soft 的 hard 对偶） | — |
| | `stdcells` | 晶体管级 nmos/pmos/nand2（教学/原理） | — |
| | `xilibs` | Xilinx 原语仿真模型（FPGA 仿真用） | FPGA |
| **协议** | `emesh` | 104 位片上网络事务包协议（公共语言） | SI |
| **外设** | `gpio` | 通用 IO（方向/中断/边沿） | HH |
| | `spi` | SPI 主/从 | HH |
| | `emailbox` | 带中断输出的邮箱 | FPGA |
| | `emmu` | 存储地址翻译单元 | FPGA |
| | `etrace` | 片上逻辑分析仪/跟踪 | HH |
| **链路** | `elink` | 高速 LVDS 点对点链路（FPGA↔ASIC） | SI |
| | `mio` | 轻量并行链路 | HH |
| **系统互连** | `axi` | AXI 主/从桥（emaxi/esaxi） | FPGA |
| | `edma` | DMA 引擎 | HH |
| **物理与板级** | `padring` | 可参数化的芯片焊盘环生成器 | — |
| | `parallella` | Parallella 板的 FPGA 顶层集成 | FPGA |

> 成熟度一栏的来源是 [README.md:60-63](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/README.md#L60-L63) 对 `SI/FPGA/HH` 的解释。建议初学者优先学习 **SI/FPGA** 的模块（emesh、elink、axi、emailbox、emmu、xilibs、parallella），把标 `HH`（施工区）的留到后面。

**(e) README 模块表的两处历史出入**

最后，落实本讲反复强调的「核对」精神，README 模块表 [README.md:40-58](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/README.md#L40-L58) 与实际目录有两类出入：

1. **表里有、实际没有**：`accelerator`、`chip`、`common`、`pic`、`risc-v` 在当前顶层均不存在。其中 `common`（README 描述为「Library of basic components」）**高度对应**现在的基础库 `stdlib`——二者职责描述几乎一致，可视为改名；`pic`（中断控制器）在 `elink` README 的结构树里以 `erx_mailbox` 等形式被吸收；`accelerator`/`chip`/`risc-v` 则可能是被移除或仅存在于某些标签版本（待本地验证）。
2. **实际有、表里没单列**：`asiclib`、`stdcells`、`stdlib`、`padring` 没有作为独立行出现在 README 模块表里（它们属于「基础库 / 物理设计」这一层，README 的模块表更偏「功能 IP」视角）。

再次提醒：表里的链接形如 `src/accelerator/README.md`，前缀 `src/` 在当前版本并不存在，点击会 404——**要以实际目录为准**。

#### 4.2.4 代码实践

> **实践类型：文档阅读 + 归类整理型实践**（本讲指定的核心实践任务）。

**实践目标**：把实际存在的顶层目录映射到六大功能类别，形成一张属于自己的「OH! 模块速查表」。

**操作步骤**：

1. 用 `ls -1` 列出实际顶层目录（约 16 个模块目录 + `docs/` + `scripts/`）。
2. 对每个模块，打开它的 `README.md`（若存在）读第一段，确认它的自我定位。
3. 把每个目录填进下面这张表的「类别」列：
   - 基础库 / 协议 / 链路 / 外设 / 系统互连 / 物理与板级。
4. 对 `HH`（施工区）模块做个标记，提醒自己「先学 SI/FPGA 的」。

**需要观察的现象**：

- `gpio`、`spi`、`emailbox`、`emmu`、`etrace` 都会落到「外设」类，且都遵循「`hdl/` 设计 + `dv/` 测试 + emesh 接口」的同一套模式。
- `elink` 与 `mio` 都落到「链路」类，且都有对称的 TX/RX 结构。
- 基础库有四个（`stdlib/asiclib/stdcells/xilibs`），但它们面向的对象不同（可综合 RTL / 硬核 / 晶体管 / 仿真模型）。

**预期结果**：得到一张类似本节 (d) 的分类表，并能用一句话说出每个模块的职责。这张表会在你后续阅读源码时反复用作「索引」。

**待本地验证**：分类是主观的，你可以根据自己的理解调整（例如把 `xilibs` 单列为「仿真支持」而非「基础库」）。重点是分类要**前后一致**、能帮你快速定位。

#### 4.2.5 小练习与答案

**练习 1**：同样是「触发器」，为什么 OH! 要在 `stdlib` 和 `asiclib` 里各放一份？

> **答案**：为了支持 soft/hard 双实现。`stdlib/rtl/oh_dffq.v` 是可综合 RTL，能直接用于 FPGA 或芯片综合；`asiclib/hdl/asic_dffq.v` 是绑定到具体工艺库的硬核的行为级「黄金模型」。综合 ASIC 时，soft 版会被 hard 版（或厂家真实单元）替换。详见 u9-l1。

**练习 2**：README 模块表里没有 `stdlib` 这一行，但它对应表里的哪个条目？

> **答案**：最可能对应 `common`（「Library of basic components」，SI）。二者职责描述几乎一致，`common` 应是 `stdlib` 的旧名。不过这是基于描述的推断，**待本地验证**（例如查阅 git 历史 `git log --follow stdlib/README.md`）。

**练习 3**：你要在开源仿真器 iverilog 里仿真 `elink`，但设计里例化了 Xilinx 的 `IDDR`，iverilog 报「找不到模块」。应该用哪个目录里的文件来补？

> **答案**：用 `xilibs/dv/IDDR.v`。`xilibs/dv/` 提供的就是这类厂商原语的行为级仿真模型，通过 `-y` 库搜索路径在编译期补进来即可（具体机制在 u1-l3 仿真环境讲义里展开）。

---

## 5. 综合实践

把 4.1 与 4.2 串起来，完成下面这个「地形侦察」小任务：

**任务**：为 OH! 仓库画一张「一页纸地形图」。

1. **顶层俯瞰**：画出根目录树（模块目录 + `docs/` + `scripts/` + 根脚本），标注哪些模块用 `hdl/`、哪些用 `rtl/`。
2. **打通一条路径**：选一个外设（推荐 `gpio`），从「README 自述」→「`gpio/hdl/gpio.v` 设计本体」→「`gpio/dv/` 测试」走一遍，确认这条「README → hdl → dv」的浏览流程在你选的模块上成立。
3. **抓一个不一致**：复现本讲的发现——执行 `test -d src`，并在 README 里找一处带 `src/` 前缀的链接，说明它在当前版本为何失效；再给出「正确的路径应该怎么写」。
4. **归类与排序**：把所有模块按六大类别排好，并圈出你应该**最先学习**的 3 个模块（提示：优先 SI/FPGA、且是公共基础或公共语言的）。

**交付物**（可在笔记里完成）：

- 一张顶层目录树；
- 一张「模块 → 类别 → 成熟度」表；
- 一段「文档与实际不一致」的记录及正确写法。

> 这个任务没有唯一标准答案，但完成后你会拥有一张属于自己的 OH! 导航图，后续每一讲都能在这张图上定位「我现在在哪」。

---

## 6. 本讲小结

- OH! 顶层约 16 个模块目录，外加 `docs/`、`scripts/` 和根目录脚本；模块内部普遍遵循 `hdl/`（或 `rtl/`）放设计、`dv/` 放测试、`fpga/`/`docs/`/`driver/` 等放配套资源的约定。
- 三类基础库职责分明：`stdlib`（可综合 RTL 原语）、`asiclib`（绑定 PDK 的硬核单元，soft 的 hard 对偶）、`stdcells`（晶体管级教学单元）；`xilibs` 提供 Xilinx 原语的仿真模型。
- 所有顶层模块可归为六大类：基础库 / 协议（emesh）/ 链路（elink、mio）/ 外设（gpio、spi、emailbox、emmu、etrace）/ 系统互连（axi、edma）/ 物理与板级（padring、parallella）。
- README 与脚本存在**历史性路径出入**：模块表链接带 `src/` 前缀，`run.sh` 也用 `src/$1/...`，但当前仓库**没有 `src/` 目录**；README 列的 `common`/`pic`/`accelerator` 等在当前顶层也不存在。
- 阅读原则落地：**遇到任何路径，先 `ls` 验证再执行**；以代码与实际目录为事实，文档仅作参考。
- 学习优先级：先攻 SI/FPGA 模块（尤其作为公共语言的 `emesh`、作为基础库的 `stdlib`），把 `HH` 施工区模块放后面。

---

## 7. 下一步学习建议

- **想立刻把环境跑起来**：进入 [u1-l3 仿真环境搭建：iverilog 与 gtkwave](u1-l3-simulation-setup.md)，学习如何用 `setenv.sh` + `build.sh` + `sim.sh` 真正跑一次仿真，并系统性地绕开本讲发现的 `src/` 路径陷阱。
- **想先搞懂编码风格**：进入 [u1-l4 Verilog 2005 与 OH! 编码规范](u1-l4-coding-style.md)，在动手读 RTL 之前先掌握 OH! 的命名与文件组织约定。
- **想直接看基础原语**：本讲已指出 `stdlib/rtl` 有 144 个原语文件，是最大的「积木盒」。等学完 u1-l3/l4，就可以从 [u2-l1 组合逻辑原语](u2-l1-combinational-primitives.md) 开始逐类拆解。
- **建议继续阅读的源码**：把本讲引用的四个 README（`stdlib`、`asiclib`、`emesh`、`elink`）再完整读一遍；尤其 `elink/README.md` 的 Design structure 树状图，是后续 u7 单元的总纲。
