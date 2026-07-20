# 仓库目录结构速览

## 1. 本讲目标

学完本讲后，你应该能够：

- 画出 `vivadoIP_axi_mm_reader` 仓库的完整目录树，并说出每个目录的职责。
- 理解一个 Vivado IP 核的开发流程（**编码 → 仿真 → 打包 → 驱动**）是如何落到这些目录上的。
- 在不打开每个文件的情况下，凭目录与文件名快速定位「核心 RTL」「wrapper」「测试台」「仿真脚本」「C 驱动入口」这几类关键代码。

本讲**只讲目录与文件布局**，不深入 RTL 实现细节（那是第二单元的任务）。我们建立的是一张「地图」，后续每一讲都会反复回到这张地图上找路。

## 2. 前置知识

本讲默认你已经读完 [u1-l1 项目概览](u1-l1-project-overview.md)，知道：

- 这是一个由 Paul Scherrer Institute (PSI) / Oliver Bründler 维护的 **AXI4 IP 核**，功能是周期性或按 `Trig` 触发地经 AXI4 读取一批 32 位寄存器，再经 AXI-Stream 或软件 FIFO 输出。
- 运行时强依赖外部库 `psi_common`，开发时另需 `PsiSim`、`PsiIpPackage`、`psi_tb`。

如果你还不知道什么是「IP 核」「AXI4」「AXI-Stream」，可以先记住三个直觉：

- **IP 核**：一段可复用、可被 Vivado 图形化拼装的硬件模块（用 VHDL 写）。
- **AXI4**：Xilinx 用的总线协议，分「主机（master，主动发起读写）」和「从机（slave，被动响应）」。
- **AXI-Stream**：一种单向数据流协议，常用来传连续数据。

还有一个背景概念很重要：本项目采用 **PSI 的 FPGA 工程惯例**——一个 IP 仓库通常会自带 `hdl`（硬件代码）、`tb`（测试台）、`sim`（仿真脚本）、`scripts`（打包与 CI）、`drivers`（嵌入式 C 驱动）、`doc`（文档），以及 Vivado 约定的 `xgui`、`bd`、`component.xml` 等。本讲就是在讲这套惯例在本仓库的具体落点。

## 3. 本讲源码地图

本讲涉及的关键文件（按目录分组）：

| 文件 | 所属目录 | 作用 |
|:--|:--|:--|
| `README.md` | 仓库根 | 项目入口说明、依赖清单、仿真运行方式 |
| `doc/Documentation.md` | `doc/` | IP 核详细文档（接口、寄存器、架构图） |
| `hdl/axi_mm_reader.vhd` | `hdl/` | 核心 RTL（状态机、读周期逻辑） |
| `hdl/axi_mm_reader_wrp.vhd` | `hdl/` | wrapper（把核心接到真实 AXI 接口） |
| `hdl/definitions_pkg.vhd` | `hdl/` | 寄存器地址、位段等常量定义包 |
| `tb/top_tb.vhd` | `tb/` | 自校验测试台 |
| `sim/config.tcl` 等 | `sim/` | PsiSim 仿真配置与运行脚本 |
| `scripts/package.tcl` | `scripts/` | 把 HDL 打包成 Vivado IP |
| `scripts/ciFlow.py` | `scripts/` | CI 流程（跑仿真并解析结果） |
| `drivers/axi_mm_reader/src/*.{c,h}` | `drivers/` | 嵌入式侧 C 软件驱动 |
| `xgui/axi_mm_reader_v1_0.tcl` | `xgui/` | Vivado IP 参数 GUI 页面 |
| `bd/bd.tcl` | `bd/` | Block Design 自动化钩子 |
| `component.xml` | 仓库根 | IP-XACT 组件描述（Vivado 打包产物） |

> 提示：你现在不必记住每个文件，本讲会带你逐目录认一遍。

## 4. 核心概念与源码讲解

本讲拆成 3 个最小模块：

1. **目录树总览**——建立全局地图。
2. **hdl / tb / sim**——硬件代码、测试台与仿真的三件套。
3. **drivers / bd / xgui / scripts / doc**——驱动、集成、打包、GUI 与文档的辅助件。

### 4.1 目录树总览

#### 4.1.1 概念说明

一个 Vivado 自定义 IP 的仓库，本质上要同时回答四个问题：

1. **硬件长什么样？** → 写在 `hdl/` 里的 VHDL。
2. **怎么验证它对不对？** → 写在 `tb/` 里的测试台 + `sim/` 里的脚本。
3. **怎么变成 Vivado 里能拖拽的 IP？** → `scripts/` 打包 + `xgui/`/`bd/`/`component.xml` 描述。
4. **CPU 上软件怎么用它？** → `drivers/` 里的 C 驱动。

这四个问题对应仓库里四组目录。再加上 `doc/`（文档）和根目录的元信息文件（`README.md`、`License.txt` 等），就构成了完整布局。

#### 4.1.2 核心流程

从「拿到代码」到「用起来」的流程，可以映射到目录上：

```text
阅读 README.md / doc/  （理解它是什么）
        │
        ▼
hdl/*.vhd               （看硬件实现，编码产物）
        │
        ▼
tb/ + sim/              （跑仿真验证，验证产物）
        │
        ▼
scripts/package.tcl      （打包成 Vivado IP）
   + xgui/ + bd/ + component.xml
        │
        ▼
drivers/                 （给 Vitis/裸机软件用）
```

记住这条主线，后面的目录职责就都顺理成章了。

#### 4.1.3 源码精读

仓库根目录的 [README.md](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/README.md) 是入口。它用一句话点明了 IP 的功能：

> [README.md:L42-L43](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/README.md#L42-L43)：`This IP-core reads a number of registers automatically ... and makes them available to SW through a FIFO or transmits them through an AXI-Stream interface.` ——「自动读一批寄存器，经 FIFO 或 AXI-Stream 交给软件」。

README 同时给出了仿真运行入口（指向 `sim/` 目录）：

> [README.md:L45-L51](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/README.md#L45-L51)：在 Modelsim 的 `sim` 目录里 `source ./run.tcl`，或用 GHDL 的 `source ./runGhdl.tcl`。

文档目录 [doc/Documentation.md](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md) 则是详细版，里面有接口图、配置 GUI 截图、寄存器表和架构图（`doc/pics/`）。`doc/index.html` 只是一个跳转页，自动重定向到 GitHub 上的 `Documentation.md`。

根据 `git ls-files`，仓库的目录树如下（已按职责分组标注）：

```text
vivadoIP_axi_mm_reader/
├── README.md            # 入口说明：定位 + 依赖 + 仿真方式
├── Changelog.md         # 版本变更记录
├── License.txt          # PSI HDL Library License（LGPL + 固件例外）
├── LGPL2_1.txt          # LGPL 全文
├── component.xml        # 【打包】IP-XACT 组件描述（Vivado 产物）
│
├── hdl/                 # 【硬件】核心 VHDL 源码
│   ├── definitions_pkg.vhd     # 寄存器地址/位段常量包
│   ├── axi_mm_reader.vhd       # 核心 RTL（FSM、读周期）
│   └── axi_mm_reader_wrp.vhd   # wrapper（接到真实 AXI 接口）
│
├── tb/                  # 【验证】测试台
│   └── top_tb.vhd              # 自校验测试台
│
├── sim/                 # 【验证】PsiSim 仿真脚本
│   ├── config.tcl              # 源文件分组（lib/src/tb）
│   ├── run.tcl / runGhdl.tcl   # Modelsim / GHDL 运行入口
│   ├── interactive*.tcl        # 交互式仿真
│   └── ci.do                   # CI 用的 do 文件
│
├── scripts/             # 【打包/CI】
│   ├── package.tcl             # PsiIpPackage 打包脚本
│   ├── ciFlow.py               # CI 流程（跑仿真 + 解析 transcript）
│   └── dependencies.py         # 拉取外部依赖
│
├── xgui/                # 【打包】Vivado IP 参数 GUI
│   └── axi_mm_reader_v1_0.tcl  # 参数页面与回调
│
├── bd/                  # 【打包】Block Design 钩子
│   └── bd.tcl                  # 自动传递 AXI4 ID_WIDTH
│
├── drivers/             # 【软件】嵌入式 C 驱动
│   └── axi_mm_reader/
│       ├── data/               # .mdd / .tcl：Vitis BSP 识别用
│       └── src/                # axi_mm_reader.c / .h / Makefile
│
└── doc/                 # 【文档】
    ├── Documentation.md        # 详细文档
    ├── index.html              # 重定向到上面这份文档
    ├── LogoRhino.png
    └── pics/                   # 架构图、GUI 截图
```

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：用上面这棵树，建立「目录 ↔ 职责」的直觉。
2. **操作步骤**：
   - 在仓库根目录运行 `git ls-files`，把输出和上面这棵树逐项对照。
   - 找到 6 个目录对应的「代表作」文件：`hdl/axi_mm_reader.vhd`、`tb/top_tb.vhd`、`sim/config.tcl`、`scripts/package.tcl`、`drivers/axi_mm_reader/src/axi_mm_reader.h`、`xgui/axi_mm_reader_v1_0.tcl`。
3. **需要观察的现象**：每个目录里文件数量都很少（多数只有 1～3 个文件），这说明这是一个**小而聚焦**的 IP。
4. **预期结果**：你能不看笔记说出「想看核心逻辑去 `hdl/`，想跑仿真去 `sim/`，想改 GUI 去 `xgui/`」。
5. 「待本地验证」项：无，本实践纯为阅读与对照。

#### 4.1.5 小练习与答案

**练习 1**：仓库里哪个文件告诉 Vivado「这个 IP 叫什么名字、属于哪个库」？

> **答案**：`component.xml`。它的根元素声明了 `vendor=oliver.bruendler`、`library=PSI`、`name=axi_mm_reader`、`version=1.0`（见 [component.xml:L2-L7](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml#L2-L7)）。

**练习 2**：如果只看目录名，哪个目录里**绝对没有** VHDL 文件？

> **答案**：`drivers/`（里面是 `.c`/`.h`/`Makefile`/`.mdd`/`.tcl`）、`scripts/`、`xgui/`、`bd/`、`doc/`——这些都不是硬件代码目录。

---

### 4.2 hdl / tb / sim：硬件代码、测试台与仿真

#### 4.2.1 概念说明

这是 PSI FPGA 工程的「三件套」，几乎所有 PSI 的 IP 都遵循同样的划分：

- **`hdl/`（Hardware Description Language）**：真正会被综合成电路的 VHDL。
- **`tb/`（Testbench）**：测试台，只用于仿真、不会被综合。它给被测模块喂激励、检查输出。
- **`sim/`（Simulation）**：仿真工程的「项目文件」——告诉仿真器要编译哪些文件、跑哪些测试。

为什么要分开？因为硬件代码和验证代码的**受众不同**：`hdl/` 给综合器看（要能变成电路），`tb/` 给仿真器看（可以用不可综合的写法，比如 `wait for 10 ns;`），`sim/` 给仿真工具链看（组织编译顺序）。

#### 4.2.2 核心流程

`hdl/` 里本仓库有三个文件，分工很清晰：

```text
definitions_pkg.vhd        常量定义（地址、位段）——被另两个文件 use
        │
        ▼
axi_mm_reader.vhd          核心：纯逻辑（FSM、读周期、FIFO 控制）
        │  （端口是简化的 IPIC 接口，不直接是 AXI）
        ▼
axi_mm_reader_wrp.vhd      wrapper：实例化核心 + 实例化 psi_common 的
                           AXI 从机/主机 IP，把核心接到真实 AXI4 上
```

`tb/top_tb.vhd` 则把 wrapper 当作被测对象（DUT），外接 AXI BFM（总线功能模型）来模拟一个真实的 AXI 对端。`sim/config.tcl` 负责把 `psi_common`、`psi_tb`、`hdl/`、`tb/` 的源文件按 `tag` 分组编译。

#### 4.2.3 源码精读

**核心 RTL** 的实体声明在 [hdl/axi_mm_reader.vhd:L24-L30](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L24-L30)：

```vhdl
entity axi_mm_reader is
    generic (
        TimeoutCkCycles_g   : natural   := 10_000_000;
        MaxRegCount_g       : natural    := 1024;
        MinBuffers_g        : natural   := 4;
        AxiAddrWidth_g      : natural   := 32;
        RamBehavior_g       : string    := "RBW"
```

注意它的端口里**没有** `s00_axi_*` / `m00_axi_*` 这样的 AXI 信号——核心只懂简化的 IPIC 接口。真正把 AXI4 翻译成 IPIC 的工作，由 **wrapper** 完成（实体声明在 [hdl/axi_mm_reader_wrp.vhd:L25](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L25)）。这种「核心 + wrapper」分层是 `psi_common` 风格的标志：核心保持纯粹、可复用，wrapper 负责接真实总线。

`definitions_pkg.vhd` 是常量包（寄存器地址、位段索引都在这里），被核心与 wrapper 共同 `use`（见 `axi_mm_reader.vhd` 第 19 行的 `use work.definitions_pkg.all;`）。寄存器映射的细节留到 [u2-l2 寄存器映射](u2-l2-register-map.md) 讲。

**测试台** `tb/top_tb.vhd` 是自校验的：它一次性覆盖 `AXIS` 和 `AXIMM` 两种输出模式（通过 generic `OutputType_g` 选择）。仿真如何同时跑这两种模式？看 [sim/config.tcl:L53-L55](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl#L53-L55)：

```tcl
psi::sim::create_tb_run "top_tb"
tb_run_add_arguments "-gOutputType_g=AXIS" \
                     "-gOutputType_g=AXIMM"
```

即同一个测试台，用两组 generic 各跑一次。

**仿真分组** 是 `sim/config.tcl` 的核心，它把源文件分成三类 tag（[sim/config.tcl:L20-L50](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl#L20-L50)）：

- `-tag lib`：外部库 `psi_common`（L20-30）和 `psi_tb`（L33-38）的源文件。
- `-tag src`：本项目 `hdl/` 里的三个文件（L41-45）。
- `-tag tb`：测试台 `tb/top_tb.vhd`（L48-50）。

这正是「`hdl/` 出硬件、`tb/` 出测试台、`sim/` 做组织」的代码体现。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：验证「核心与 wrapper 的分层」以及「仿真三段式分组」。
2. **操作步骤**：
   - 打开 [hdl/axi_mm_reader.vhd](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd) 的端口声明，确认里面没有 `axi` 字样的端口。
   - 打开 [hdl/axi_mm_reader_wrp.vhd](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd)，搜索 `s00_axi` 和 `m00_axi`，确认 wrapper 才是 AXI 接口的边界。
   - 打开 [sim/config.tcl](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl)，数一数三个 `-tag` 各自包含几个文件。
3. **需要观察的现象**：核心端口是命令/数据式的握手信号；wrapper 端口是完整的 AXI4 信号组。
4. **预期结果**：你能用一句话说明「为什么改 AXI 行为通常只动 wrapper，而改读周期逻辑只动核心」。
5. 「待本地验证」项：无。

#### 4.2.5 小练习与答案

**练习 1**：`sim/config.tcl` 里 `LibPath`（[L9](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl#L9)）设成 `../../..` 是相对于谁？

> **答案**：相对于 `sim/` 目录本身。`sim/` 往上两级到仓库根，再往上一级到「PSI 工程根」，那里并排放着 `VHDL/psi_common`、`VHDL/psi_tb` 等依赖库（见 L20、L33 的 `add_sources` 路径）。

**练习 2**：测试台文件为什么放在 `tb/` 而不是 `hdl/`？

> **答案**：因为测试台不参与综合（不能变成电路），而 `hdl/` 的内容会被打包进 IP 并综合。分开存放让打包脚本（`scripts/package.tcl`）可以放心地只收 `hdl/`，不会误把测试台综合进去。

---

### 4.3 drivers / bd / xgui / scripts / doc：驱动、集成、打包、GUI 与文档

#### 4.3.1 概念说明

这一组目录服务于「让 IP 在真实工具链里被使用」：

- **`drivers/`**：嵌入式 CPU（如 Zynq 的 ARM 核、MicroBlaze）上运行的 **C 软件驱动**。Vitis / Xilinx SDK 会把它编进 BSP，软件通过它读写寄存器。
- **`xgui/`**：Vivado 里双击 IP 弹出的**参数配置页面**（那个图形界面）由这里的 Tcl 生成。
- **`bd/`**：当 IP 被放进 Block Design（图形化连线）时，Vivado 会在特定时机调用这里的钩子 Tcl，做一些自动化（本仓库用它自动传递 AXI4 的 `ID_WIDTH`）。
- **`scripts/`**：开发工具脚本——打包 IP（`package.tcl`）、跑 CI（`ciFlow.py`）、拉依赖（`dependencies.py`）。
- **`doc/`**：人类阅读的文档与配图。

`component.xml`（根目录）是这组的「总账本」：它是 Vivado 打包后生成的 **IP-XACT** 描述文件，记录 IP 的接口、端口映射、参数、文件清单等——Vivado 靠它认识这个 IP。

#### 4.3.2 核心流程

打包与集成的流程把这些目录串起来：

```text
开发者在 scripts/package.tcl 里写打包指令
        │  （PsiIpPackage 帮你生成 component.xml）
        ▼
component.xml + xgui/*.tcl + bd/*.tcl  →  Vivado 识别为可拖拽 IP
        │
        ▼
用户在 Block Design 里拖入 IP、连 AXI 总线
   （bd/bd.tcl 自动处理 ID_WIDTH）
        │
        ▼
导出硬件给 Vitis → drivers/ 被编进 BSP
   （.mdd/.tcl 让 Vitis 生成 xparameters.h）
        │
        ▼
软件代码 #include "axi_mm_reader.h" 用 C API 操作 IP
```

#### 4.3.3 源码精读

**打包脚本** [scripts/package.tcl](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl) 先引入 PSI 的打包命令库，再声明 IP 的身份信息（[L12-L19](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L12-L19)）：

```tcl
source ../../../TCL/PsiIpPackage/PsiIpPackage.tcl
namespace import -force psi::ip_package::latest::*
...
set IP_NAME axi_mm_reader
set IP_VERSION 1.0
set IP_REVISION "auto"
set IP_LIBRARY PSI
```

打包的最终产物就是根目录的 [component.xml](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml)，它的根元素声明了 IP 的 vendor/library/name/version（[component.xml:L2-L7](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml#L2-L7)），后面跟着 `busInterfaces`（如 `m_axis`、`s00_axi`、`m00_axi`）等定义。打包流程细节留到 [u1-l4 IP 打包与 Vivado 集成](u1-l4-ip-packaging.md)。

**CI 流程** [scripts/ciFlow.py](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/ciFlow.py) 切到 `sim/` 目录并以命令行模式跑仿真（[L13](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/ciFlow.py#L13)）：`os.system("vsim -c -do ci.do")`，随后读取 transcript 判定通过与否（细节留到 [u1-l3 仿真与 CI](u1-l3-running-simulation.md)）。

**GUI 页面** [xgui/axi_mm_reader_v1_0.tcl](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/xgui/axi_mm_reader_v1_0.tcl) 的 `init_gui` 过程往「Configuration」页里逐个添加参数控件（[L2-L10](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/xgui/axi_mm_reader_v1_0.tcl#L2-L10)）：`AxiSlaveAddrWidth_g`、`ClkFrequencyHz`、`TimeoutUs_g`、`MaxRegCount_g`、`MinBuffers_g`、`Output_g`（下拉框）。这正是 [u1-l1](u1-l1-project-overview.md) 提到的「GUI 可配参数」的来源。

**Block Design 钩子** [bd/bd.tcl](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/bd/bd.tcl) 定义了 `init` 等过程（[L9](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/bd/bd.tcl#L9)），在 IP 被放进 BD 时自动处理 AXI4 的 `ID_WIDTH`（[L12-L13](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/bd/bd.tcl#L12-L13)）。细节留到 [u3-l4 IP-XACT 与 Block Design 集成](u3-l4-ipxact-block-design.md)。

**C 驱动** 入口在 [drivers/axi_mm_reader/src/axi_mm_reader.h](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.h)，它先定义了返回码枚举（[L17-L23](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.h#L17-L23)）：

```c
typedef enum MmReader_ErrCode
{
    MmReader_Success = 0,
    MmReader_IpMustBeDisabled = -1,
    MmReader_FifoIsEmpty = -2,
    MmReader_NoCompletePacketInFifo = -3,
} MmReader_ErrCode;
```

具体的 API 实现（`SetEnable`、`SetRegTable`、`ReadFifoPacket` 等）在同目录的 `axi_mm_reader.c`，驱动如何被 Vitis BSP 识别则由 `drivers/axi_mm_reader/data/` 下的 `.mdd` / `.tcl` 声明。这部分留到 [u3-l1 C 软件驱动](u3-l1-c-driver.md)。

**文档** [doc/Documentation.md](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md) 的「IP Integration → Interfaces」一节（[L16-L34](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L16-L34)）汇总了所有外部接口（`s00_axi` 配置、`m00_axi` 读寄存器、`m_axis` 可选输出、`Trig`/`DoneIrq`），是画系统框图时的权威依据。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：把「四类辅助目录」与它们服务的工具链阶段对应起来。
2. **操作步骤**：
   - 打开 [xgui/axi_mm_reader_v1_0.tcl](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/xgui/axi_mm_reader_v1_0.tcl)，数 `init_gui` 里一共 `add_param` 了几个参数（应为 6 个 + `Component_Name`）。
   - 打开 [drivers/axi_mm_reader/src/axi_mm_reader.h](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.h)，列出所有 `MmReader_*` 错误码。
   - 打开 [component.xml](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml)，搜索 `<spirit:busInterface>`，数一下声明了哪几个总线接口（应包含 `m_axis`、`s00_axi`、`m00_axi`）。
3. **需要观察的现象**：`xgui` 的参数名与 `hdl` 里 wrapper 的 `generic` 名几乎一一对应；`component.xml` 的接口名与 `doc/Documentation.md` 的接口描述一致。
4. **预期结果**：你能填出下表——

   | 目录 | 服务于哪个阶段 | 代表文件 |
   |:--|:--|:--|
   | `xgui/` | Vivado 里配置 IP 参数 | `axi_mm_reader_v1_0.tcl` |
   | `bd/` | Block Design 自动化 | `bd.tcl` |
   | `scripts/` | 打包 IP + CI | `package.tcl`、`ciFlow.py` |
   | `drivers/` | Vitis/裸机软件 | `axi_mm_reader.c/.h` |
   | `doc/` | 人类阅读 | `Documentation.md` |

5. 「待本地验证」项：无。

#### 4.3.5 小练习与答案

**练习 1**：`component.xml` 是手写的还是生成的？

> **答案**：是 `scripts/package.tcl` 调用 PsiIpPackage **生成**的打包产物。开发者一般不直接手改它，而是改 `package.tcl` 后重新打包。

**练习 2**：为什么 `xgui` 里的参数（如 `Output_g`）和 wrapper 的 `generic` 同名？

> **答案**：因为 Vivado GUI 参数会通过 `update_MODELPARAM_VALUE` 回调（见 `xgui/axi_mm_reader_v1_0.tcl`）映射到顶层 `generic`，再传给综合。名字一致是为了让 GUI → generic → RTL 这条链路自动对上。

**练习 3**：如果一个同事只想看「这个 IP 对外暴露哪些总线接口」，让他看哪个文件最快？

> **答案**：`doc/Documentation.md` 的 Interfaces 一节（最直观），或 `component.xml` 的 `busInterfaces`（最权威、机器可读）。

---

## 5. 综合实践

**任务**：亲手绘制一份「带职责标注的仓库目录树」，并完成一次「找人」挑战。

1. 在仓库根运行 `git ls-files`，按目录归类，画出一棵树（可参考 4.1.3 的示例）。
2. 在树上**用三种颜色/标记**区分：① 硬件代码（会被综合）、② 验证代码（只仿真）、③ 工具链/文档（既不综合也不仿真）。
3. 完成下面「找人」表——为每个角色写出**具体文件路径**：

   | 角色 | 具体文件 |
   |:--|:--|
   | 核心 RTL（读周期 FSM） | `hdl/axi_mm_reader.vhd` |
   | wrapper（AXI 接口边界） | ？ |
   | 测试台 | ？ |
   | 仿真源文件分组 | ？ |
   | 打包脚本 | ？ |
   | C 驱动头文件 | ？ |
   | GUI 参数页 | ？ |
   | IP-XACT 总账本 | ？ |

4. 最后用一句话总结：「这个仓库里，硬件相关的文件集中在 ____，验证相关的集中在 ____，把 IP 变成可用产品的工程文件集中在 ____。」

> 参考答案：wrapper → `hdl/axi_mm_reader_wrp.vhd`；测试台 → `tb/top_tb.vhd`；仿真分组 → `sim/config.tcl`；打包脚本 → `scripts/package.tcl`；C 驱动头 → `drivers/axi_mm_reader/src/axi_mm_reader.h`；GUI 页 → `xgui/axi_mm_reader_v1_0.tcl`；IP-XACT → `component.xml`。总结句：硬件在 `hdl/`，验证在 `tb/`+`sim/`，工程化产物在 `scripts/`+`xgui/`+`bd/`+`drivers/`+`component.xml`。

## 6. 本讲小结

- 仓库围绕「**编码 → 仿真 → 打包 → 驱动**」四步组织目录：`hdl/` 出硬件、`tb/`+`sim/` 出验证、`scripts/`+`xgui/`+`bd/`+`component.xml` 出打包、`drivers/` 出软件。
- `hdl/` 采用 **核心 + wrapper** 分层：`axi_mm_reader.vhd` 是纯逻辑核心（IPIC 接口），`axi_mm_reader_wrp.vhd` 才是 AXI4 接口边界，`definitions_pkg.vhd` 提供共享常量。
- `sim/config.tcl` 用 `-tag lib/src/tb` 把外部库、本项目源码、测试台分组编译，并用两组 generic（`AXIS`/`AXIMM`）让同一个测试台跑两次。
- `component.xml` 是打包产物（IP-XACT），由 `scripts/package.tcl` 生成，记录 IP 的接口、参数与文件清单。
- `xgui/` 的参数与 wrapper 的 `generic` 一一对应；`drivers/` 提供 Vitis 侧 C API；`bd/bd.tcl` 负责 Block Design 里的自动化。
- 这是一个**小而聚焦**的 IP：每个目录文件很少，职责边界清晰，便于后续逐模块深入。

## 7. 下一步学习建议

有了这张地图，下一步建议按以下顺序深入：

- 想知道「怎么把仿真跑起来、CI 怎么判通过」→ 学 [u1-l3 如何运行仿真与 CI 流程](u1-l3-running-simulation.md)（聚焦 `sim/` 与 `scripts/ciFlow.py`）。
- 想知道「`scripts/package.tcl` 怎么把 HDL 变成 Vivado IP」→ 学 [u1-l4 IP 打包与 Vivado 集成](u1-l4-ip-packaging.md)。
- 等入门单元结束，进入第二单元时，建议从 [u2-l1 整体架构与数据流](u2-l1-architecture-dataflow.md) 开始，把本讲认下的 `hdl/axi_mm_reader.vhd` 与 `axi_mm_reader_wrp.vhd` 逐行读懂。

> 阅读源码时，养成「先定位目录、再打开文件」的习惯——本讲给你的就是这张定位地图。
