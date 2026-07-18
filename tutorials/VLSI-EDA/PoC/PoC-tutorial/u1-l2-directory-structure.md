# 仓库目录结构解析

## 1. 本讲目标

上一讲我们建立了对 PoC（Pile of Cores）最外层的认知：它是一个可复用的 VHDL/Verilog 硬件 IP 核库。本讲要把这张「外层地图」变成一张「空间地图」——读者学完后应该能够：

- 记住 PoC 根目录下每一个顶层子目录（`src`、`tb`、`lib`、`netlist`、`ucf`、`xst`、`py`、`sim`、`tcl`、`temp`、`tools`）各自的职责。
- 理解 `src` 是按「子命名空间」分组的，并能说出每个命名空间代表的功能类别。
- 理解 `tb` 目录如何「镜像」`src` 的结构，以及顶层 `sim/` 与 `src/sim/` 为什么是两回事。
- 拿到一个陌生文件路径时，能立刻判断它属于源码、测试台、第三方库、约束文件，还是综合输出。

本讲只讲「东西放在哪里、为什么放在那里」，不深入任何具体 IP 核的内部实现——那是后续讲义的主题。

## 2. 前置知识

- **IP 核（IP core）**：一段可复用的硬件设计代码（这里是 VHDL/Verilog），类似软件里的「库函数」。例如一个 FIFO、一个加法器、一个 DDR 控制器都是一个核。
- **VHDL**：一种硬件描述语言，用代码描述数字电路。PoC 的绝大多数源码是 VHDL。
- **测试台（testbench）**：用来驱动和检查一个硬件模块的「外壳代码」，本身不会被综合成真实电路，只在仿真时运行。
- **综合（synthesis）**：把 VHDL 代码翻译成具体 FPGA/ASIC 上能实现的网表（netlist）的过程，由厂商工具（如 Xilinx Vivado、Altera Quartus）完成。
- **约束文件（constraint file）**：告诉综合工具「这根信号接在哪个引脚、时钟频率是多少、哪些路径可以不检查时序」的配置文件，常见后缀 `.ucf`/`.xdc`/`.sdc`。
- **命名空间（namespace）**：把同类核归到同一个前缀下，避免名字冲突、便于组织。PoC 里 `PoC.arith.*` 表示算术类核，`PoC.fifo.*` 表示 FIFO 类核，依此类推。

如果你对 VHDL 本身还不熟悉也没关系——本讲只读目录和 README，几乎不涉及 VHDL 语法细节。

## 3. 本讲源码地图

本讲主要阅读「说明性文档」（各目录下的 `README.md`），它们是 PoC 官方对目录用途的权威解释：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目总览，其中 §3 "Common Notes" 给出顶层目录一览表，是本讲的核心依据。 |
| `src/README.md` | 解释 `src` 下 15 个子命名空间（`PoC.arith`、`PoC.fifo`……）各自包含什么。 |
| `src/common/README.md` | 解释 `src/common` 下的公共包（`config`、`utils`、`physical`……）与模板文件。 |
| `lib/README.md` | 解释 `lib/` 下以 git submodule 形式集成的第三方验证库（cocotb、OSVVM、UVVM、VUnit）。 |
| `netlist/README.md` | 解释 `netlist/` 如何承载厂商 IP 核与 PoC 核综合成的网表。 |
| `ucf/README.md` | 解释 `ucf/` 下按开发板组织的约束文件。 |
| `xst/README.md` | 解释 `xst/` 下 Xilinx XST 综合工具的配置文件（文档较少，本讲会如实说明）。 |

此外，我们会用 `ls`、`git ls-files` 之类的只读方式核对目录的真实内容，确保讲解与磁盘一致。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

- **4.1 顶层目录用途**：搞清楚根目录下 11 个子目录分别是干什么的。
- **4.2 命名空间子目录树**：搞清楚 `src` 内部如何用命名空间把数百个核组织成清晰的树。

### 4.1 顶层目录用途

#### 4.1.1 概念说明

PoC 是一个大型项目（数百个 VHDL 文件），如果全部堆在根目录会无法维护。因此它把不同「角色」的文件放进不同顶层目录，目录名本身就说明用途：`src` 放源码、`tb` 放测试台、`lib` 放第三方库、`ucf` 放约束、`netlist` 放综合输出。这种「按角色分目录」的做法让任何人一眼就能定位文件。

官方在 `README.md` 的 §3 "Common Notes" 里直接给出了这张顶层目录表，是本讲最权威的参考。

#### 4.1.2 核心流程

拿到 PoC 仓库后，理解顶层目录的阅读顺序是：

1. 先看 `src/` —— 这是项目的主体，所有 IP 核源码都在这里。
2. 再看 `tb/` —— 它的结构与 `src/` 平行，是各核的测试台。
3. 看 `lib/` —— 这里是被集成的第三方库（作为 git submodule 存在）。
4. 看 `ucf/`、`xst/`、`netlist/` —— 这三者构成「综合到目标板」的链路：约束、综合配置、网表输出。
5. 看 `py/`、`sim/`、`tcl/`、`temp/`、`tools/` —— 这些是基础设施与辅助文件。

伪代码描述一个文件的「归属判定」：

```text
function locate(path):
    if path 在 src/ 下:        -> 源码（综合进 PoC 库）
    elif path 在 tb/ 下:       -> 测试台（仅仿真）
    elif path 在 lib/ 下:      -> 第三方库（submodule）
    elif path 在 ucf/ 下:      -> 约束文件
    elif path 在 netlist/ 下:  -> 综合输出 / 网表模板
    elif path 在 xst/ 下:      -> XST 综合配置
    else:                      -> 基础设施 / 辅助（py, sim, tcl, temp, tools）
```

#### 4.1.3 源码精读

**顶层目录一览表（最关键的一处）。** PoC 在 [README.md:273-286](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md#L273-L286) 逐行列出了 11 个顶层目录及其用途。这里把表格中最重要的几项摘出来对照说明：

- `src` —— PoC 的源码，按子命名空间树分组（主体）。
- `tb` —— 测试台文件。
- `lib` —— 内嵌或链接的外部库（第三方验证库）。
- `netlist` —— 厂商 IP 核或复杂 PoC 控制器「预配置网表综合」的配置文件与输出目录。
- `ucf` —— 各支持开发板预配置的约束文件（`.ucf`/`.xdc`/`.sdc`）。
- `xst` —— 用 Xilinx XST 把 PoC 模块综合成网表所需的配置文件。
- `py` —— 辅助 Python 脚本。
- `sim` —— 为选定测试台预配置的波形视图。
- `tcl` —— Tcl 脚本文件。
- `temp` —— PoC 的 Python 脚本为各类工具自动创建的临时工作目录。
- `tools` —— 支持工具用的设置/语法高亮文件与辅助小工具。

紧接其下，[README.md:289-291](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md#L289-L291) 还交代了一条全局规则：所有 VHDL 源文件都应该编译进名为 `PoC` 的 VHDL 库中，不兼容的文件用 `.v93.vhdl` / `.v08.vhdl` 后缀标注所支持的 VHDL 语言版本。这一点解释了为什么后面会到处看到 `PoC` 这个库名和带版本后缀的文件。

**第三方库目录 `lib/`。** [lib/README.md:1-6](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/README.md#L1-L6) 说明 `lib/` 存放随 PoC 一起分发的第三方库，并强调它们是以 git submodule 形式嵌入的——这也是为什么「克隆时需要 `--recursive`」。仓库实际包含 cocotb、OSVVM、UVVM、VUnit 等子目录（以及 `pyIPCMI` 基础设施子模块），它们各自带有许可证副本（如 `Apache License 2.0.md`、`MIT UVVM.md`）和 `*.files` 集成清单（如 `OSVVM.files`、`UVVM.files`）。

**网表输出目录 `netlist/`。** [netlist/README.md:3-13](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/netlist/README.md#L3-L13) 解释了 `netlist/` 的双重角色：既存放把厂商预配置 IP 核（如 Xilinx CoreGen 的 `.xco`）或 PoC 实体综合成网表所需的配置，也作为网表输出目录。它还列出了支持的工具链（Altera Quartus、Lattice Diamond LSE、Xilinx CoreGen/XST 等）。

**约束文件目录 `ucf/`。** [ucf/README.md:3-6](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/README.md#L3-L6) 交代了 PoC 支持的三种约束格式：`.ucf`（Xilinx ISE）、`.xdc`（Xilinx Vivado）、`.sdc`（Altera Quartus-II）。随后 [ucf/README.md:8-18](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/README.md#L8-L18) 把约束文件分成两类：一类是给 PoC 实体用的（如 `sync_Bits_Xilinx.ucf`、`MetaStability.ucf`），一类是按开发板组织的（如 `KC705`、`Atlys`、`ML505` 各自一个子目录）。

**XST 配置目录 `xst/`。** [xst/README.md:1-2](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/xst/README.md#L1-L2) 只写了一句 "Files required by Xilinx XST"，并标注 *No documentation available.*。虽然文档很少，但实际目录里有按器件系列组织的配置（`Series-7.xst`、`Spartan-3.xst`、`Spartan-6.xst`），它们是 XST 综合工具的选项文件。这里要诚实说明：这个目录官方几乎没有解释，需要结合第 4 单元的综合流程讲义才能完全理解。

#### 4.1.4 代码实践

**实践目标：** 用只读命令核对 11 个顶层目录都真实存在，并按「角色」给它们分类。

**操作步骤：**

1. 进入仓库根目录（下文称 `PoCRoot`）。
2. 列出全部顶层条目，与 `README.md` 给出的目录表逐一比对。
3. 按「源码 / 测试 / 第三方库 / 约束 / 综合输出 / 基础设施」六类，把 11 个目录归类。

```bash
cd PoCRoot
# 只列出目录（排除文件），与 README §3 的目录表对照
ls -d */
```

**需要观察的现象：** 输出应包含 `lib/ netlist/ py/ sim/ src/ tb/ tcl/ temp/ tools/ ucf/ xst/` 这 11 个目录（外加 `docs/` 等文档/CI 目录）。

**预期结果：** 你应该能填出下面这张分类表（参考答案见 4.1.5）：

| 角色 | 包含的顶层目录 |
| --- | --- |
| 源码 | `src` |
| 测试 | `tb` |
| 第三方库 | `lib` |
| 约束 | `ucf` |
| 综合输出与配置 | `netlist`、`xst` |
| 基础设施 / 辅助 | `py`、`sim`、`tcl`、`temp`、`tools` |

> 说明：以上命令只是只读列出目录，不会修改任何文件。本讲所有实践均为只读，不会改动源码。

#### 4.1.5 小练习与答案

**练习 1：** 顶层 `sim/` 目录和 `src/sim/` 目录是同一个东西吗？如果不是，各自装的是什么？

> **答案：** 不是。顶层 [`sim/`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/sim/README.md#L1) 装的是「为选定测试台预配置的波形视图」（README §3 中 `sim` 一项），里面有 `vSim.gui.tcl`、`xSim.gui.tcl` 等各仿真器的波形脚本。而 `src/sim/` 装的是「仿真辅助包」（VHDL 代码，如 `sim_simulation.v08.vhdl`），是真正会被编译进 `PoC` 库、在测试台里 `use` 的源码。二者一个是「视图配置」，一个是「源码包」。

**练习 2：** 为什么官方强调克隆 PoC 时要加 `--recursive`？哪个顶层目录与此直接相关？

> **答案：** 因为第三方库以 git submodule 形式嵌入在 `lib/` 下（见 [lib/README.md:8-12](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/README.md#L8-L12)）。不加 `--recursive` 时这些子目录会是空的，需要事后手动 `git submodule init && git submodule update`。

**练习 3：** `temp/` 目录里的内容可以随意删除吗？为什么？

> **答案：** 可以。见 [temp/README.md:1-4](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/temp/README.md#L1-L4)：它被各类外部工具当作工作目录使用，所有子文件夹都可删除以清理中间产物或释放磁盘空间。

### 4.2 命名空间子目录树

#### 4.2.1 概念说明

`src/` 是 PoC 的主体，但里面文件极多。PoC 的组织办法是「子命名空间树」：把同类核放进同一个以功能命名的子目录，子目录名同时就是 VHDL 命名空间名。例如所有算术核都在 `src/arith/` 下，对应的 VHDL 命名空间是 `PoC.arith`。

当一个子命名空间内部核太多时，还会再分一层子目录。例如 `src/io/` 下既有直接放着的 `io_Debounce.vhdl`，也有 `uart/`、`ddrio/`、`iic/`、`vga/` 等更细的子目录。这样就形成了一棵「按功能逐层细分」的目录树。

[src/README.md:1-6](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/README.md#L1-L6) 一句话点明了这套机制：命名空间 `PoC` 等同于 VHDL 库名 `PoC`，所有实现被归类进若干子命名空间，公共包在 `common` 子目录、仿真包在 `sim` 子目录。

#### 4.2.2 核心流程

理解 `src` 内部结构，可以按下面的层级阅读：

```text
src/
├── common/        # 公共包（config, utils, physical, ...）+ 模板文件
├── sim/           # 仿真辅助包（仅仿真用）
├── arith/         # PoC.arith 算术单元
├── fifo/          # PoC.fifo 各类 FIFO
├── mem/           # PoC.mem 存储抽象与控制器（含 ocram/, ddr3/, sdram/ 等子目录）
├── cache/         # PoC.cache 缓存
├── bus/           # PoC.bus 总线与流式协议
├── net/           # PoC.net 网络协议栈（含 mac/, arp/, ipv4/, udp/ 等子目录）
├── io/            # PoC.io 低速 IO（含 uart/, ddrio/, iic/, vga/ 等子目录）
├── misc/          # PoC.misc 尚未归类组件（含 sync/, gearbox/）
├── comm/          # PoC.comm 通信模块
├── sort/          # PoC.sort 排序算法
├── dstruct/       # PoC.dstructs 可综合数据结构
├── alt/           # PoC.alt Altera 专用实现
└── xil/           # PoC.xil Xilinx 专用实现
```

每个「核」通常由三类文件构成，理解这套约定后，看到一个核名就能推断出它有哪些伴生文件：

1. `<ns>_<entity>.vhdl` —— 实体与架构的源码。
2. `<ns>_<entity>.files` —— pyIPCMI 消费的编译清单，描述这个核依赖哪些文件、在什么条件下编译。
3. （命名空间级）`<ns>.pkg.vhdl` —— 该命名空间的包文件，集中声明本命名空间所有核的组件、类型与函数。

例如 `src/arith/arith_addw.vhdl` 是源码，`src/arith/arith_addw.files` 是它的编译清单，`src/arith/arith.pkg.vhdl` 是整个算术命名空间的包。

#### 4.2.3 源码精读

**子命名空间清单。** [src/README.md:10-24](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/README.md#L10-L24) 用一张列表给出了全部 15 个子命名空间及其含义。这是「目录名 → 功能类别」的权威映射，摘录如下（顺序与官方一致）：

| 子命名空间 | 目录 | 含义 |
| --- | --- | --- |
| `PoC.alt` | `src/alt` | Altera 专用实现 |
| `PoC.arith` | `src/arith` | 算术单元 |
| `PoC.bus` | `src/bus` | 总线组件 |
| `PoC.cache` | `src/cache` | 缓存 |
| `PoC.comm` | `src/comm` | 通信模块 |
| `PoC.common` | `src/common` | 公共包 |
| `PoC.dstructs` | `src/dstruct` | 可综合数据结构 |
| `PoC.fifo` | `src/fifo` | FIFO 实现 |
| `PoC.io` | `src/io` | 低速 IO 协议实现 |
| `PoC.mem` | `src/mem` | 存储抽象与控制器 |
| `PoC.misc` | `src/misc` | 尚未归类的组件 |
| `PoC.net` | `src/net` | 网络协议栈 |
| `PoC.sim` | `src/sim` | 仿真辅助包 |
| `PoC.sort` | `src/sort` | 排序算法 |
| `PoC.xil` | `src/xil` | Xilinx 专用实现 |

注意一个细节：VHDL 命名空间名 `PoC.dstructs`（带 s）对应的目录是 `src/dstruct`（不带 s）；命名空间 `PoC.alt`/`PoC.xil` 是厂商专用实现，而 `PoC.common`/`PoC.sim` 比较特殊——它们是「包」而不是「核」。

**公共包目录 `src/common/`。** [src/common/README.md:6-19](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/README.md#L6-L19) 列出了这里存放的公共包：`config`（配置机制）、`components`（映射到常用门/触发器的可综合函数）、`math`、`physical`（FREQ 等物理类型）、`strings`、`utils`（常用辅助函数）、`vectors`、`fileio`、`debug` 等。此外还有两个**模板文件** `my_config.vhdl.template` 和 `my_project.vhdl.template`（用户复制改名后填值，下一讲会用到）。

**`.files` 编译清单长什么样。** 以 `arith_addw` 为例，[src/arith/arith_addw.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.files#L1-L17) 揭示了目录之间的依赖是如何被描述的。它的核心几行是：

```text
# 所有路径都相对 PoC 根目录
if (DeviceVendor = "Xilinx") then
    include  "lib/Xilinx.files"          # 按厂商条件引入 Xilinx 原语库
end if
include  "src/common/common.files"       # 引入公共包
vhdl  PoC  "src/arith/arith.pkg.vhdl"    # 本命名空间包
vhdl  PoC  "src/arith/arith_addw.vhdl"   # 核本身
```

这说明：哪怕只是编译一个算术核，也会经由 `.files` 把 `lib/`（厂商原语）和 `src/common/`（公共包）串进来。目录之间不是孤立的，而是通过 `.files` 结成依赖网。

**`tb/` 如何镜像 `src/`。** 仓库里 `tb/` 的子目录是 `arith cache common dstruct fifo io mem misc sim sort`——正好是 `src/` 里那些「有测试台的命名空间」的子集（`src` 有 15 个命名空间，`tb` 只有 10 个）。这种「`tb/<ns>/` 对应 `src/<ns>/`」的镜像布局，让你找某个核的测试台时无需查文档：源码在 `src/fifo/fifo_cc_got.vhdl`，测试台就去 `tb/fifo/` 下找。值得一提的是 `tb/common/` 比较特殊，它存放的不是某个核的测试台，而是各开发板的 `my_config_<board>.vhdl` 配置变体（如 `my_config_KC705.vhdl`、`my_config_Generic.vhdl`）以及 `config_tb.vhdl` 这类公共测试文件——这套「板级配置变体」机制会在第 4 单元的测试台讲义里详讲。

#### 4.2.4 代码实践

**实践目标：** 画出 `src/` 下各命名空间目录的树状图，并标注每个目录的功能类别。这就是本讲规格里要求的动手任务。

**操作步骤：**

1. 进入 `src/` 目录。
2. 用 `ls -d */` 列出全部命名空间子目录。
3. 对照 [src/README.md:10-24](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/README.md#L10-L24) 的权威清单，给每个目录标注功能类别。
4. 任选一个有子目录的命名空间（如 `io`、`mem` 或 `net`），展开它的下一层，观察「命名空间内部再分子命名空间」的现象。

```bash
cd PoCRoot/src
# 第一步：列出全部命名空间目录
ls -d */

# 第四步：展开 io 命名空间，观察它内部的子命名空间
ls -d io/*/
```

**需要观察的现象：**
- 第一步应输出 15 个目录，与上一节表格完全对应。
- 第四步对 `io/` 应能看到 `ddrio/ iic/ lcd/ mdio/ ow/ pmod/ ps2/ uart/ vga/` 等子目录，同时目录里还散落着 `io_Debounce.vhdl`、`io.pkg.vhdl` 这类直接放在 `io/` 根的文件——这就是「细分子目录 + 直接文件」并存的布局。

**预期结果：** 你应能画出下面这样的树（标注功能类别）：

```text
src/
├── common/   (公共包: config, utils, physical, ...)
├── sim/      (仿真辅助包)
├── arith/    (算术)
├── fifo/     (先入先出)
├── mem/      (存储器: 含 ocram/, ddr3/, sdram/, ...)
├── cache/    (缓存)
├── bus/      (总线/流式)
├── net/      (网络协议栈: 含 mac/, arp/, ipv4/, udp/, ...)
├── io/       (低速 IO: 含 uart/, ddrio/, iic/, vga/, ...)
├── misc/     (杂项: 含 sync/, gearbox/)
├── comm/     (通信)
├── sort/     (排序)
├── dstruct/  (数据结构, 命名空间名 dstructs)
├── alt/      (Altera 专用)
└── xil/      (Xilinx 专用)
```

> 待本地验证：`ls` 的具体输出顺序和子目录集合以你本地仓库为准；本讲引用的目录列表来自当前 HEAD（`8c39b24`）的真实磁盘状态。

#### 4.2.5 小练习与答案

**练习 1：** 如果你要找 `sync_Bits` 这个同步器核的源码，应该去哪个目录找？它的测试台又该去哪找？

> **答案：** `sync` 属于 `misc` 命名空间下的子命名空间，源码在 `src/misc/sync/sync_Bits.vhdl`。测试台遵循「`tb` 镜像 `src`」的约定，应到 `tb/misc/` 下找（具体是否提供以本地仓库为准）。

**练习 2：** 给定一个核 `PoC.fifo.fifo_cc_got`，列出它「理应」拥有的伴生文件名（按本节的三类文件约定）。

> **答案：** 源码 `src/fifo/fifo_cc_got.vhdl`、编译清单 `src/fifo/fifo_cc_got.files`，以及命名空间级包 `src/fifo/fifo.pkg.vhdl`（后者是整个 `fifo` 命名空间共享，不是该核独有）。

**练习 3：** 为什么 `PoC.dstructs` 命名空间对应的目录叫 `src/dstruct`（少了一个 s）？这说明了什么？

> **答案：** 这是历史命名习惯造成的不一致：VHDL 命名空间用复数 `dstructs`，目录用简写 `dstruct`。这说明「目录名」和「VHDL 命名空间名」高度对应但**不是严格相等**，阅读时要参照 [src/README.md:10-24](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/README.md#L10-L24) 的映射表，不能想当然。

## 5. 综合实践

**任务：** 为整个 PoC 仓库建立一张「空间地图」速查卡，把本讲两个模块的知识串起来。

具体要求：

1. 画一张根目录的顶层目录表，包含「目录名 + 一句话职责 + 角色（源码/测试/库/约束/综合/基础设施）」三列，依据来自 [README.md:273-286](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md#L273-L286)。
2. 画一张 `src/` 的命名空间树，标注每个命名空间的功能类别，依据来自 [src/README.md:10-24](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/README.md#L10-L24)。
3. 任选一个核（建议 `src/arith/arith_addw.vhdl`），回答三个问题：
   - 它的 `.files` 清单里引入了哪两个「目录级」依赖？（提示：一个在 `lib/`，一个在 `src/common/`）
   - 按照 `tb` 镜像 `src` 的约定，它的测试台应放在哪个目录？
   - 如果要把它综合到某块 Xilinx 开发板，会用到哪几个顶层目录的文件？（提示：约束、XST 配置、网表输出）
4. 用一句话总结：顶层 `sim/` 与 `src/sim/` 的区别。

**验收标准：** 完成后，你应当能不看任何资料，仅凭一张地图回答「某个文件该去哪个目录找」「这个目录装的是源码还是约束」。如果还有目录（如 `xst/`）你无法 confidently 解释，那是正常的——它们会在第 4 单元「仿真、综合与目标平台」里讲清楚。

## 6. 本讲小结

- PoC 用 11 个顶层目录按「角色」组织文件：`src`（源码）、`tb`（测试台）、`lib`（第三方库）、`ucf`（约束）、`netlist`/`xst`（综合输出与配置）、`py`/`sim`/`tcl`/`temp`/`tools`（基础设施与辅助）。
- 顶层目录表见 [README.md:273-286](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md#L273-L286)；所有 VHDL 源码统一编译进名为 `PoC` 的库，不兼容文件用 `.v93.vhdl`/`.v08.vhdl` 标注版本。
- `src/` 内部按 15 个「子命名空间」分组，目录名基本等于 VHDL 命名空间名（但有 `dstruct` vs `dstructs` 这类例外），权威映射见 [src/README.md:10-24](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/README.md#L10-L24)。
- 大命名空间内部会再分子目录（如 `io/uart/`、`net/mac/`），形成「按功能逐层细分」的目录树。
- 每个核通常有三类伴生文件：源码 `.vhdl`、编译清单 `.files`、命名空间级包 `<ns>.pkg.vhdl`；`.files` 通过条件 `include` 把 `lib/` 与 `src/common/` 串成依赖网。
- `tb/` 的子目录是 `src/` 的镜像子集，遵循「源码在 `src/<ns>/`、测试台在 `tb/<ns>/`」的对称布局；但要注意顶层 `sim/`（波形视图）和 `src/sim/`（仿真辅助包）是两回事。

## 7. 下一步学习建议

有了这张空间地图，接下来建议：

1. **学习 `u1-l3 获取、运行与配置 PoC`**：动手克隆仓库（用 `--recursive`）、运行 `poc.sh`/`poc.ps1`，并理解 `src/common/my_config.vhdl.template`、`my_project.vhdl.template` 两个模板——本讲只是「指出了它们在哪」，下一讲会讲「怎么填」。
2. **学习 `u1-l4 VHDL 编码规范与命名约定`**：本讲提到了「单实体单文件、`<ns>_<entity>` 命名、`.files` 伴生」等约定，下一讲会把它们系统化。
3. **带着地图读源码**：在进入第 2 单元（公共包与配置机制）之前，可以先随便挑一两个核（如 `src/arith/arith_addw.vhdl`、`src/misc/sync/sync_Bits.vhdl`）只看它的 entity 声明和文件头，感受一下目录约定如何反映到代码里。
4. **暂不深究的目录**：`xst/`、`netlist/`、`ucf/` 这三个目录的细节要等第 4 单元「仿真、综合与目标平台」才能真正理解，现在只要记住它们「属于综合链路」即可。
