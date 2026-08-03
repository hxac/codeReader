# 仓库结构与目录组织

## 1. 本讲目标

上一篇（u1-l1）我们从外部认识了 verilog-ethernet 是什么、支持哪些速率、有哪些顶层模块。本篇我们要**打开仓库抽屉**，看清楚每个目录里装的是什么，以及它们如何协作构成一个完整的 FPGA 工程。

学完本讲，你应当能够：

- 一眼说出 `rtl/`、`lib/`、`tb/`、`example/`、`syn/`、`scripts/` 六个目录各自的职责。
- 在 `rtl/` 的近百个文件中，按 MAC、PHY、协议栈、PTP 四类快速定位你要找的模块。
- 理解 `example/` 中“每块开发板都实现同一个 UDP 回显”的统一组织模式。
- 理解 `lib/axis` 作为第三方 AXI-Stream 构件库是如何被“内嵌（vendored）”进本仓库并被引用的。
- 知道综合约束（`syn/`）和板级测试脚本（`scripts/`）分别服务哪一环节。

## 2. 前置知识

在进入目录之前，先用大白话建立几个 FPGA 工程的基础概念（不熟悉的术语可以边看边对照）：

- **源码（RTL）**：用 Verilog 写的硬件描述，例如 `rtl/` 下的 `.v` 文件。它们描述电路“长什么样”，是要被“综合”成真实门电路的。
- **仿真（testbench）**：在把代码烧进芯片之前，先用软件模拟它跑起来对不对，对应 `tb/` 目录。本仓库用 cocotb（Python 驱动）+ Icarus Verilog（仿真器）。
- **综合约束**：告诉 FPGA 工具链（Vivado/Quartus）“这条线要走多快、时钟和数据怎么对齐”，对应 `syn/` 下的 `.sdc`/`.tcl`。
- **AXI-Stream**：贯穿全库的数据流接口（`tdata`/`tvalid`/`tready`/`tlast`/`tuser` 等），上一篇已介绍，本讲在讲 `lib/axis` 时会再次用到这个名词。

一句话理解本仓库的工程分工：**`rtl/` 是产品、`tb/` 是质检、`syn/` 是工艺要求、`example/` 是成品样机、`lib/` 是外购零件、`scripts/` 是验收工具。**

## 3. 本讲源码地图

| 文件 / 目录 | 作用 | 本讲用来做什么 |
| --- | --- | --- |
| `README.md` | 项目总说明，含模块清单与源文件索引 | 最权威的“目录→功能”索引来源 |
| `example/Arty/fpga/README.md` | Arty 板级参考设计的说明 | 展示 `example/` 的统一模式 |
| `lib/axis/README.md` | 第三方 verilog-axis 库的说明 | 说明 `lib/axis` 的来历与职责 |
| `tox.ini` | pytest/tox 测试配置 | 说明 `tb/`、`example/` 如何被批量跑起来 |
| `tb/eth_mac_1g/Makefile` | 单个 testbench 的 cocotb 构建文件 | 展示 `tb/` 如何引用 `rtl/` |
| `rtl/eth_mac_1g_fifo.v` | 带 FIFO 的千兆 MAC | 展示 `rtl/` 如何引用 `lib/axis` |

## 4. 核心概念与源码讲解

### 4.1 顶层目录与职责划分

#### 4.1.1 概念说明

一个能“既可综合上板、又可仿真验证、还能跨厂商移植”的 FPGA 库，通常不会把所有文件堆在一个目录里。verilog-ethernet 把工程拆成几个职责清晰的顶层目录，每个目录只管一件事。理解这个划分，是后续在近百个文件中不迷路的前提。

本仓库的顶层布局如下（根目录下）：

| 目录 / 文件 | 职责 | 一句话比喻 |
| --- | --- | --- |
| `rtl/` | 全部 IP 核心的 Verilog 源码（约 98 个 `.v`） | 产品本体 |
| `lib/axis/` | 第三方 AXI-Stream 通用构件库（内嵌的 verilog-axis） | 外购的标准零件 |
| `tb/` | cocotb 仿真平台与测试用例 | 质检车间 |
| `example/` | 各开发板的参考设计（UDP 回显） | 成品样机 |
| `syn/` | 综合与时序约束（Quartus/Quartus Pro/Vivado） | 工艺要求单 |
| `scripts/` | 板级测试用的辅助脚本 | 验收工具箱 |
| `README.md` | 项目说明 + 模块/源文件索引 | 总目录 |
| `tox.ini` | pytest/tox 回归测试配置 | 流水线调度单 |
| `COPYING` / `AUTHORS` | 许可证与作者 | 合法身份 |

#### 4.1.2 核心流程

这些目录是如何“协作”的？可以用下面这条链路来理解：

```text
rtl/  ──被编译──▶  tb/      （仿真验证 RTL 是否正确）
  │
  ├──被引用──▶  example/    （组装成某块板子的完整工程）
  │                  │
  │                  └──被约束──▶ syn/   （告诉工具链时序怎么满足）
  │
  └──依赖──▶  lib/axis/      （提供 FIFO 等通用 AXI 构件）

example/ ──上板后用──▶ scripts/udp_test.py  （主机发包验证回显）
全部测试 ──由──▶ tox.ini 中的 pytest 统一调度
```

要点：

1. `rtl/` 是被依赖的中心，`tb/`、`example/` 都引用它里面的源文件。
2. `lib/axis/` 是 `rtl/` 的依赖（被 `rtl/` 里的某些模块引用），而不是反过来。
3. `syn/` 和 `scripts/` 不参与 RTL 编译，分别服务“综合上板”和“板级验收”两个环节。

#### 4.1.3 源码精读

`README.md` 在开头就交代了仓库的定位，并点出几类顶层模块，这是理解 `rtl/` 内容的钥匙：

[README.md:13-22](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L13-L22) —— 说明本仓库是“千兆/10G/25G 分组处理（8 位与 64 位数据通路）”的组件集合，含 MAC、PHY、IP/UDP/ARP 协议栈与 PTP 组件。

紧接着点明了对外暴露的顶层模块命名，正好对应 `rtl/` 里同名的 `.v` 文件：

[README.md:30-33](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L30-L33) —— 列出 `eth_mac_*`（MAC）、`eth_phy_10g`（PCS/PMA PHY）、`eth_mac_phy_10g`（MAC/PHY 合一）这些顶层模块名。

`README.md` 还提供了一份完整的“源文件索引”，把 `rtl/` 下每个文件与其功能一一对应，这是你在 `rtl/` 里找文件最权威的地图：

[README.md:429-513](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L429-L513) —— `### Source Files` 段落，逐行列出 `rtl/` 下所有源文件及其功能。

`README.md` 还提到自带完整 cocotb 仿真与对 cocotbext-eth 的依赖，这解释了 `tb/` 目录的存在：

[README.md:594-596](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L594-L596) —— `## Testing` 段落，说明运行 testbench 需要 cocotb、cocotbext-axi、cocotbext-eth 与 Icarus Verilog。

而 `tox.ini` 则把 `tb/` 和 `example/` 都纳入 pytest 测试路径，并声明依赖工具链版本：

[tox.ini:12-37](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tox.ini#L12-L37) —— `[testenv]` 与 `[pytest]` 段，列出 `cocotb`、`cocotbext-axi`、`cocotbext-eth`、`scapy` 等依赖，并把 `testpaths` 设为 `tb` 和 `example`。

#### 4.1.4 代码实践

**实践目标**：亲手核对顶层目录的职责划分，建立“目录→职责”的直觉。

**操作步骤**：

1. 在仓库根目录执行 `ls -d */`，列出所有顶层目录。
2. 执行 `ls rtl/ | wc -l`、`ls example/ | wc -l`，统计 `rtl/` 源文件数与 `example/` 板级设计数。
3. 打开 `README.md` 的 `### Source Files` 段（约 429 行起），随意挑 3 个文件名，在 `rtl/` 下确认它们真实存在。

**需要观察的现象**：

- 顶层应出现 `rtl/ lib/ tb/ example/ syn/ scripts/` 六个目录。
- `rtl/` 下文件数约为 98 个（截至当前 HEAD，可用 `wc -l` 自行复核）。
- `example/` 下应有约 25 个以开发板命名的子目录。

**预期结果**：你看到的目录与你根据本节表格作出的预测一致。若数字略有出入（仓库会随版本增减文件），以你本地统计为准。

#### 4.1.5 小练习与答案

**练习 1**：仓库里哪个目录是“被依赖的中心”，既被 `tb/` 引用、又被 `example/` 引用？
**答案**：`rtl/`。它是 IP 核心源码所在，仿真和板级工程都要编译它里面的文件。

**练习 2**：`syn/` 目录里的约束文件会被仿真器（Icarus Verilog）使用吗？
**答案**：不会。`syn/` 里的 `.sdc`/`.tcl` 是给综合工具链（Vivado/Quartus）用的时序约束，仿真阶段不读取它们。

**练习 3**：如果想批量运行所有回归测试，应该看哪个配置文件？
**答案**：`tox.ini`。它的 `[pytest]` 段把 `testpaths` 设为 `tb` 和 `example`。

---

### 4.2 lib/axis：第三方 AXI-Stream 依赖

#### 4.2.1 概念说明

`rtl/` 里的模块要做以太网处理，但很多通用功能——比如 FIFO 缓存、跨时钟域、位宽适配、多路复用——并不是以太网特有的，而是 AXI-Stream 总线上的通用构件。重复造这些轮子没有意义，所以作者把另一个开源项目 **verilog-axis**（一个纯 AXI-Stream 组件库）整个拷贝进了 `lib/axis/` 目录。这种“把第三方源码直接放进自己仓库”的做法叫 **vendoring（内嵌）**。

注意：`lib/axis` **不是** git 子模块（仓库根目录没有 `.gitmodules`，`lib/axis/` 下也没有 `.git`），它就是一份被复制进来的源码副本，和 `rtl/` 一样可以被直接编译。

#### 4.2.2 核心流程

`lib/axis` 在工程中的角色：

```text
rtl/eth_mac_*_fifo.v  ──实例化──▶  lib/axis/rtl/axis_async_fifo.v
        (需要跨时钟域 FIFO)              (verilog-axis 提供的通用异步 FIFO)
```

为什么要强调它？因为本仓库所有带 `_fifo` 后缀的 MAC 模块（见 u1-l1 的命名约定）都依赖 `lib/axis` 提供的 `axis_fifo` / `axis_async_fifo` 来做缓冲和时钟域跨越。如果你只编译 `rtl/` 而忘了带上 `lib/axis/rtl/`，这些模块会报“找不到模块”的错误。

`lib/axis/` 内部也保留了和主仓库相似的目录结构（`rtl/`、`tb/`、`syn/`），说明它本身就是一个小而完整的子工程。

#### 4.2.3 源码精读

`lib/axis` 的来历写在它自己的 README 开头——它是 verilog-axis 的副本：

[lib/axis/README.md:1-3](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/README.md#L1-L3) —— 标题为 “Verilog AXI Stream Components”，GitHub 仓库指向 `verilog-axis`，说明 `lib/axis` 就是该项目的内嵌副本。

它的 README 里专门描述了两个最常被本仓库引用的构件——异步 FIFO 与同步 FIFO：

[lib/axis/README.md:39-40](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/README.md#L39-L40) —— `axis_async_fifo` 模块：可配置的异步 FIFO，用于跨越两个不同时钟域。

[lib/axis/README.md:78-79](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/README.md#L78-L79) —— `axis_fifo` 模块：可配置的同步 FIFO，用于同一时钟域内的缓冲。

这两个模块的源码声明在 `lib/axis/rtl/` 下：

[lib/axis/rtl/axis_async_fifo.v:34](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/rtl/axis_async_fifo.v#L34) —— `module axis_async_fifo` 声明。

[lib/axis/rtl/axis_fifo.v:34](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/rtl/axis_fifo.v#L34) —— `module axis_fifo` 声明。

而本仓库的 `rtl/` 确实在使用它们。例如带 FIFO 的千兆 MAC 实例化了异步 FIFO 适配器：

[rtl/eth_mac_1g_fifo.v:229](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_fifo.v#L229) —— `eth_mac_1g_fifo` 实例化 `axis_async_fifo_adapter`（来自 `lib/axis`）来做发送侧的跨时钟域缓冲。

#### 4.2.4 代码实践

**实践目标**：确认 `lib/axis` 是被 `rtl/` 引用的第三方依赖，而非孤立的演示代码。

**操作步骤**：

1. 在 `rtl/` 目录下搜索哪些文件引用了 `lib/axis` 的模块，例如：用搜索工具在 `rtl/` 中查找字符串 `axis_async_fifo`（这是源码阅读型实践）。
2. 打开 `rtl/eth_mac_1g_fifo.v` 第 229 行附近，确认它实例化的 `axis_async_fifo_adapter` 对应 `lib/axis/rtl/` 里的同名文件。

**需要观察的现象**：搜索结果应显示多个 `eth_mac_*_fifo.v` 文件都引用了 `axis_async_fifo` / `axis_fifo` 之类来自 `lib/axis` 的模块。

**预期结果**：你会确认“只要用到带 `_fifo` 的 MAC，就必须把 `lib/axis/rtl/` 一起加入源文件列表”，这正是 `tb/` 的 Makefile 会把 `lib/axis` 路径也纳入编译范围的原因。

> 说明：本实践为源码阅读型，不需要运行仿真；若要实际编译，需配合 u1-l4 介绍的 cocotb 环境。

#### 4.2.5 小练习与答案

**练习 1**：`lib/axis` 是通过 git submodule 引入的吗？如何判断？
**答案**：不是。仓库根目录没有 `.gitmodules` 文件，`lib/axis/` 下也没有 `.git`，所以它是一份直接内嵌（vendored）的源码副本。

**练习 2**：为什么本仓库要引入 `lib/axis`，而不是自己重写 FIFO？
**答案**：因为 FIFO、位宽适配、跨时钟域等是 AXI-Stream 上的通用构件，与以太网无关；复用成熟的 verilog-axis 可避免重复造轮子，也更易于维护。

**练习 3**：如果只把 `rtl/` 加进工程、忘记加 `lib/axis/rtl/`，会发生什么？
**答案**：凡是实例化了 `axis_fifo` / `axis_async_fifo` 的模块（如所有 `eth_mac_*_fifo.v`）都会因为找不到子模块而报错。

---

### 4.3 example/：板级参考设计组织

#### 4.3.1 概念说明

光有 `rtl/` 里的 IP 核还不够——初学者最难的一步往往是“怎么把这些模块连起来、接到一块真实的板子上”。`example/` 目录就是为解决这个问题而存在的：它为 **每一块支持的开发板** 提供了一份“能直接综合上板”的完整参考设计。

更关键的是，所有这些板子上的设计**做的是同一件事**：实现一个简单的 UDP 回显服务器（收到什么 UDP 包就回什么）。这种“换板不换逻辑”的统一模式，让你可以把注意力集中在“同一份逻辑如何适配不同硬件”上。

#### 4.3.2 核心流程

`example/` 的组织规则：

```text
example/
├── Arty/                  # 每块开发板一个目录，以板名命名
│   └── fpga/
│       ├── rtl/           # 板级顶层：fpga_core.v（核心逻辑）+ fpga.v + 辅助模块
│       ├── fpga.xdc       # 引脚约束（哪根信号接哪个管脚）
│       ├── common/vivado.mk  # Vivado 构建流程
│       ├── Makefile       # 一键构建入口
│       ├── README.md      # 这块板的说明（FPGA 型号、PHY 型号、如何烧录测试）
│       └── tb/fpga_core/  # 板级设计的仿真
├── KC705/
│   ├── fpga_rgmii/        # 有的板子会按 PHY 接口分多个变体
│   ├── fpga_sgmii/
│   └── fpga_gmii/
└── ...（约 25 块板）
```

要点：

1. **一块板 = 一个目录**，目录名就是板名（如 `Arty`、`KC705`、`ZCU102`）。
2. 每块板内部至少有一个 `fpga/`（或 `fpga_<接口>/`）子目录，结构高度统一：`rtl/` + 约束文件 + `Makefile` + `README.md`。
3. 核心逻辑都集中在 `rtl/fpga_core.v`，它把 PHY 接口、MAC、`udp_complete` 协议栈和应用回显逻辑组装在一起。
4. 个别板（如 `KC705`）会因 PHY 接口不同（RGMII / SGMII / GMII）而提供多个并列变体目录。

#### 4.3.3 源码精读

`README.md` 在主文档里就说明了 `example/` 的统一用途——UDP 回显：

[README.md:40-41](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L40-L41) —— “Example designs implementing a simple UDP echo server are included for the following boards”，随后列出所有支持的开发板。

以 Arty 板为例，它的 README 把“这块板做什么、用什么芯片、怎么测”讲得很清楚：

[example/Arty/fpga/README.md:5-12](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/README.md#L5-L12) —— 说明该设计默认监听 IP `192.168.1.128` 的 UDP 端口 `1234` 并回显收到的包，并列出 FPGA（XC7A35T）与 PHY（TI DP83848J）型号。

板级测试方法也写在 README 里，用主机端的 `netcat` 即可验证：

[example/Arty/fpga/README.md:21-26](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/README.md#L21-L26) —— 用 `netcat -u 192.168.1.128 1234` 打开 UDP 连接，输入的文本会被回显。

而“监听 1234 端口”这一行为，在板级顶层源码里有确切的对应——一个端口匹配条件：

[example/Arty/fpga/rtl/fpga_core.v:247](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L247) —— `wire match_cond = rx_udp_dest_port == 1234;`，即只对目的端口为 1234 的 UDP 包执行回显。

而把整套协议栈组装起来的，正是 `udp_complete` 的实例：

[example/Arty/fpga/rtl/fpga_core.v:422-423](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L422-L423) —— `fpga_core` 实例化 `udp_complete`，把 IP/ARP/UDP 协议栈集成到板级设计里。

本地的 MAC 和 IP 地址也在源码里写死（后续可改）：

[example/Arty/fpga/rtl/fpga_core.v:224-225](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L224-L225) —— 定义 `local_mac` 与 `local_ip = 192.168.1.128`。

#### 4.3.4 代码实践

**实践目标**：通过对比两块板的目录结构，确认 `example/` 的统一组织模式。

**操作步骤**：

1. 列出 `example/Arty/fpga/` 与 `example/KC705/fpga_gmii/` 下的文件，对比两者的子目录构成。
2. 分别打开两块的 `README.md`，确认它们都提到“监听 UDP 端口 1234 并回显”。
3. 观察 `KC705` 目录下为何有 `fpga_rgmii`、`fpga_sgmii`、`fpga_gmii` 三个并列子目录，而 `Arty` 只有一个 `fpga/`。

**需要观察的现象**：两块板的 `fpga*/` 内部都包含 `rtl/`、`fpga.xdc`、`Makefile`、`README.md`、`common/vivado.mk`，结构几乎一致；区别只在 PHY 接口与 FPGA 型号。

**预期结果**：你将验证“换板不换逻辑”——同一套 UDP 回显逻辑，被复用到不同 FPGA 与不同 PHY 接口上。

> 说明：本实践为源码阅读/目录对比型；若要在真实板子上验证回显，需要对应硬件与 Vivado 工具链，结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`example/` 下每块板的“核心连接逻辑”通常写在哪个文件里？
**答案**：写在 `rtl/fpga_core.v` 里，它把 PHY 接口、MAC、`udp_complete` 与应用逻辑（如端口匹配回显）连成一个完整工程。

**练习 2**：`KC705` 目录下为什么有多个 `fpga_*` 子目录？
**答案**：因为同一块板可以接不同 PHY 接口（RGMII/SGMII/GMII），每种接口对应一份独立的参考设计变体。

**练习 3**：Arty 设计默认监听的 UDP 端口和 IP 是多少？对应源码里的哪一行？
**答案**：IP `192.168.1.128`，端口 `1234`；端口匹配对应 `fpga_core.v` 第 247 行的 `rx_udp_dest_port == 1234`。

---

## 5. 综合实践

**任务**：在 `rtl/` 中，按 MAC、PHY、协议栈、PTP 四大类各挑出两个文件，绘制一张“文件 → 所属目录 → 功能”的对应关系表，并标注每个文件是被哪一篇后续讲义深入讲解的。

这是一个把本讲“目录职责”与上一篇 u1-l1“模块分类”串起来的练习。

**操作步骤**：

1. 打开 `README.md` 的 `### Source Files` 段（[README.md:429-513](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L429-L513)），它给出了每个文件的功能说明。
2. 按下表四类各挑两个文件（下面给出推荐示例，你也可以自行挑选）。
3. 用 `Read` 工具或编辑器打开每个文件，确认其顶部 `module` 声明与 README 描述一致。
4. 仿照下表填写你的版本。

**参考答案表**（示例）：

| 类别 | 文件 | 目录 | 功能（据 README） | 后续讲义 |
| --- | --- | --- | --- | --- |
| MAC | `rtl/eth_mac_1g.v` | `rtl/` | 千兆以太网 GMII MAC | u4-l3 |
| MAC | `rtl/eth_mac_10g.v` | `rtl/` | 10G/25G 以太网 XGMII MAC | u9-l2 |
| PHY | `rtl/eth_phy_10g.v` | `rtl/` | 10G/25G 以太网 PCS/PMA PHY | u10-l3 |
| PHY | `rtl/rgmii_phy_if.v` | `rtl/` | RGMII PHY 接口与时钟 | u4-l5 |
| 协议栈 | `rtl/ip_complete.v` | `rtl/` | 千兆 IPv4 协议栈（含 ARP） | u7-l3 |
| 协议栈 | `rtl/udp_complete.v` | `rtl/` | 千兆 UDP 协议栈（IP+ARP+UDP） | u8-l3 |
| PTP | `rtl/ptp_clock.v` | `rtl/` | PTP 时钟，输出时间戳与 PPS | u11-l1 |
| PTP | `rtl/ptp_perout.v` | `rtl/` | 基于 PTP 时间的周期脉冲输出 | u11-l5 |

**需要观察的现象**：你挑出的每个文件都真实存在于 `rtl/` 下，其 `module` 名与文件名一致，功能描述与 README 索引吻合。

**预期结果**：得到一张完整的“目录 → 功能”映射表，证明你已经能在近百个文件中按四大类快速定位目标模块。

## 6. 本讲小结

- 仓库顶层分为 `rtl/`（源码）、`lib/axis/`（第三方 AXI-Stream 构件）、`tb/`（仿真）、`example/`（板级参考设计）、`syn/`（综合约束）、`scripts/`（测试脚本）六大目录，职责清晰。
- `rtl/` 是被依赖的中心，`tb/` 和 `example/` 都引用它；`README.md` 的 `### Source Files` 是最权威的“文件→功能”索引。
- `lib/axis` 是 verilog-axis 的内嵌副本（非 git 子模块），为所有 `_fifo` 后缀模块提供通用的 `axis_fifo` / `axis_async_fifo`。
- `example/` 按“一块板一个目录”组织，所有板实现同一个 UDP 回显（默认端口 1234），核心逻辑都在 `rtl/fpga_core.v`。
- `syn/` 下按工具链分 `quartus/`、`quartus_pro/`（SDC）与 `vivado/`（TCL），只服务综合、不参与仿真。
- `tox.ini` 用 pytest 把 `tb/` 和 `example/` 统一纳入回归测试。

## 7. 下一步学习建议

本讲让你建立了仓库的全局地图。接下来建议：

- **先打底再深入**：学习 **u1-l3（AXI-Stream 接口约定）**，因为 `rtl/` 里几乎所有模块的端口都基于这套信号约定，读懂它才能看懂任何 `.v` 的端口表。
- **学会跑仿真**：学习 **u1-l4（测试框架与仿真运行方式）**，亲手跑一个 `tb/` 下的 testbench，把“源码→仿真”这条链路真正动起来。
- **目录探查型练习**：在进入具体模块前，可以先用本讲的“综合实践”表格，把 `rtl/` 的近百个文件按四类过一遍，建立肌肉记忆；之后再按学习路线从 u2（LFSR/CRC）开始逐层深入。
