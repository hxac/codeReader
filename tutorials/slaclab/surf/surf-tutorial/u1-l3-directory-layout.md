# 目录结构与文件分类约定

## 1. 本讲目标

学完本讲，你应当能够：

- 准确区分 SURF 仓库里 `rtl/`、`sim/`、`tb/`、`wrappers/`、`ip_integrator/` 五类目录的用途，知道一个新文件该放进哪一类。
- 理解「`core/` 通用 RTL + 家族 PHY 目录」的拆分模式，并能读懂 `ruckus.tcl` 里用 `getFpgaArch` 在二者之间做选择的代码。
- 说清楚为什么 cocotb/pytest 测试要放在 `tests/<subsystem>/`，而不是散落在各个源码目录根下。

本讲是 u1-l1（项目总览）的承接：u1-l1 给了你一张「顶层子树职责表」，本讲带你钻进**单个子树内部**，看 SURF 用什么约定把成千上万个 `.vhd` 文件分门别类、又如何让同一份逻辑跑在不同的 FPGA 家族上。

## 2. 前置知识

在继续之前，请确认你已了解（来自 u1-l1）：

- **SURF 是「可复用基础设施库」而非单板工程**，顶层目录按「能力」而非「板卡」划分。
- **`ruckus.tcl` 是构建清单**：每个目录下的 `ruckus.tcl` 用 Tcl 回答「哪些 HDL 进构建」。核心三件套是 `loadRuckusTcl`（下钻子目录）、`loadSource -lib surf`（登记真实源文件）、`getFpgaArch`（按 FPGA 家族选源）。
- 七大 HDL 子树：`axi`、`base`、`dsp`、`devices`、`ethernet`、`protocols`、`xilinx`。

本讲会反复用到三个概念，先用大白话解释：

- **可综合 RTL（synthesizable RTL）**：能被 Vivado 真正变成 FPGA 电路的代码。仓库里 `rtl/` 目录下的 `.vhd` 几乎都属于这一类。
- **仿真模型（simulation model）**：只在仿真器里「假装」某个外部芯片行为（比如一片 ADC、一个 Rogue 内存桥），它不会被综合成电路。
- **测试台（testbench）**：在仿真器里给被测模块（DUT）喂时钟、复位和激励的「外壳」。SURF 的现代测试台通常很薄，真正的激励和检查由 cocotb（Python）负责。
- **FPGA 家族（family）**：Xilinx 器件按架构分代，如 7 系列（`artix7`/`kintex7`/`virtex7`/`zynq`）、UltraScale（`kintexu`/`virtexu`）、UltraScale+（`kintexuplus`/`zynquplus`/`virtexuplus`）等。不同家族的原语（如收发器 GTx/GTH/GTY）不同，所以同一协议的 PHY 代码要按家族分目录。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `AGENTS.md` | 贡献者「宪法」，本讲的文件分类、`getFpgaArch`、`wrappers`/`ip_integrator` 约定都写在这里 |
| `base/README.md` | `base` 子树布局导航，示范一个子树 README 如何描述自己的子目录 |
| `axi/README.md` | `axi` 子树布局导航，点明 `ip_integrator/`、`wrappers/`、`tests/axi/` 的归属 |
| `protocols/README.md` | `protocols` 子树布局导航，点明 `core/` 与家族 PHY 拆分 |
| `ethernet/README.md` | `ethernet` 子树布局导航，明确「高速核用 `core/` 拆分 + `getFpgaArch` 守卫」 |
| `ethernet/GigEthCore/ruckus.tcl` | 本讲核心实践对象：千兆以太网核的家族选择总清单 |
| `ethernet/GigEthCore/core/ruckus.tcl` | 通用 `core/` 的源登记（无条件加载） |
| `ethernet/GigEthCore/gtx7/ruckus.tcl` | 某家族 PHY 目录的源登记（带 Vivado 版本守卫） |

## 4. 核心概念与源码讲解

### 4.1 五类文件：rtl / sim / tb / wrappers / ip_integrator

#### 4.1.1 概念说明

SURF 一个子树内部，几乎都用同一套目录名来表达「这个文件扮演什么角色」。这是全仓库最统一的一条约定。`AGENTS.md` 把它写成一条硬规则：

> Put synthesizable RTL in `rtl/`, simulation-only models in `sim/`, testbenches in `tb/`, flattened or tool-facing adapters in `wrappers/` or `ip_integrator/`, and FPGA-family specializations in family-named directories…

翻译过来就是五类（外加家族目录，见 4.2）：

| 目录 | 放什么 | 进综合？ | 进仿真？ |
|------|--------|----------|----------|
| `rtl/` | 可综合的电路描述（真正的设计） | 是 | 是 |
| `sim/` | 仅仿真用的模型（外部芯片/桥接的「替身」） | 否 | 是 |
| `tb/` | 测试台（给 DUT 喂时钟/复位/激励的外壳） | 否 | 是 |
| `wrappers/` | 薄封装：把记录(record)接口扁平化、适配仿真端口、拼简单拓扑 | 通常可综合 | 通常可综合 |
| `ip_integrator/` | 面向 Vivado IP integrator 的封装（BD 用） | 是 | 是 |

为什么要有这套约定？因为 SURF 是共享库，同一份 RTL 会被几十个板卡工程复用。如果可综合代码和仅仿真代码混在一起，综合工具很容易把仿真模型错误地塞进电路，仿真器也可能漏掉测试台。把角色写进目录名，就能让 `ruckus.tcl` 用「按目录加载」的方式干净地切分它们。

#### 4.1.2 核心流程

一个典型子树（例如 `protocols/ssi/`）内部是这样组织的（伪结构）：

```
protocols/ssi/
├── ruckus.tcl          # 本子树总清单
├── rtl/                # 可综合设计：SsiFifo.vhd、SsiPrbsTx.vhd …
├── sim/                # （若需要）仅仿真模型
├── tb/                 # 测试台：SsiPrbsTb.vhd、SsiFifoTb.vhd …
└── wrappers/           # 薄封装：SsiPrbsWrapper.vhd、SsiFifoWrapper.vhd …
```

对应到 `ruckus.tcl`，约定是这样的：

1. 父清单用 `loadRuckusTcl` 下钻到各子目录，让子目录自己的 `ruckus.tcl` 决定加载什么；
2. 子清单用 `loadSource -lib surf -dir "$::DIR_PATH/rtl"` 这类语句登记真实源文件；
3. **不要**把生成的仿真产物、波形文件、缓存写进任何 `ruckus.tcl`（这是 `AGENTS.md` 的「Ruckus Conventions」明确禁止的）。

关于 `wrappers/` 与 `tb/` 的分工，`AGENTS.md` 给了一条很重要的判断标准：**面向 cocotb 的、可复用的 HDL 封装，要放在 RTL 旁边的 `wrappers/` 或 `ip_integrator/`，而不是塞进 `tests/`**；纯仿真模型放 `sim/`，老式/纯 VHDL 测试台放 `tb/`。这条规则保证「能被多个工程共享的 HDL」不会因为被误放进 `tests/` 而消失在构建图之外。

#### 4.1.3 源码精读

**（1）分类规则的原话**，见贡献者宪法：

- [AGENTS.md:33](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L33)：把可综合 RTL 放 `rtl/`、仅仿真模型放 `sim/`、测试台放 `tb/`、扁平化/面向工具的适配器放 `wrappers/` 或 `ip_integrator/`、家族特化代码放进家族命名目录。
- [AGENTS.md:69-70](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L69-L70)：cocotb 复用封装要紧贴 RTL 放 `wrappers/`/`ip_integrator/`，纯仿真模型放 `sim/`，老式测试台放 `tb/`。

**（2）`sim/` 的真实例子**——`axi/simlink/sim/` 下放的是仅在仿真中存在的 Rogue 桥（把仿真里的 AXI-Lite/AXI-Stream 通过 VHPI/ZMQ 接到 Python），它们永远不会被综合：

- `axi/simlink/sim/RogueTcpMemory.vhd`、`RogueTcpStream.vhd`、`RogueSideBand.vhd`（仅仿真模型）

**（3）`wrappers/` 的真实例子**——`protocols/ssi/wrappers/` 里每个 `*Wrapper.vhd` 都是把记录接口扁平化的薄壳，例如 `SsiFifoWrapper.vhd`、`SsiPrbsWrapper.vhd`、`SsiInsertSofWrapper.vhd`。

**（4）`ip_integrator/` 的真实例子**——`axi/axi-lite/ip_integrator/` 全是面向 Vivado Block Design 的封装，如 `AxiLiteCrossbarIpIntegrator.vhd`、`AxiVersionIpIntegrator.vhd`，它们把 SURF 的记录型总线拍平成 IP integrator 能连线的扁平端口。

**（5）`tb/` 的真实例子**——`protocols/ssi/tb/` 下的 `SsiPrbsTb.vhd`、`SsiFifoTb.vhd` 是测试台外壳，真正激励由同名/相关的 cocotb 脚本驱动。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：亲手在仓库里各找一个 `rtl/`、`sim/`、`tb/`、`wrappers/`、`ip_integrator/` 的真实例子，确认你理解了分类。
2. **操作步骤**：
   - 在仓库根执行（只读浏览）：分别查看 `protocols/ssi/rtl/`、`axi/simlink/sim/`、`protocols/ssi/tb/`、`protocols/ssi/wrappers/`、`axi/axi-lite/ip_integrator/` 这五个目录的内容。
   - 对每个目录挑一个文件，看它的文件头 `-- Description:` 注释和它 `use` 的库，判断它是否可综合。
3. **需要观察的现象**：`rtl/` 与 `wrappers/` 下的文件头描述像「设计」；`sim/` 下的文件头通常会提到 simulator/VHPI/ZMQ；`tb/` 下通常是 `*Tb.vhd`。
4. **预期结果**：你能为这五类各写一句话定位，且能说出 `sim/` 下的文件**不应**出现在综合后的网表里。
5. 若无法本地综合验证，标注「待本地验证」。

#### 4.1.5 小练习与答案

- **练习 1**：假如你要给一个已有模块加一个「只在仿真里用的 Rogue 内存桥」，应该放进哪类目录？为什么？
  - **答案**：放 `sim/`。它不会被综合成电路，只服务仿真。
- **练习 2**：你写了一个把 `AxiStreamMasterType` 记录拍平成扁平端口的薄壳，希望多个板卡工程的 Vivado Block Design 都能复用它，应放哪？
  - **答案**：放紧贴该模块的 `ip_integrator/`（面向 BD）或 `wrappers/`。不要放 `tests/`，否则它就消失在共享构建图之外了。

### 4.2 core/ 通用 RTL 与家族 PHY 目录拆分

#### 4.2.1 概念说明

很多协议/接口（千兆以太网、万兆以太网、PGP、CoaXPress……）的逻辑可以拆成两层：

- **协议层 / MAC 层**：与 FPGA 家族无关的部分（成帧、寄存器映射、流控）。这部分对所有家族都一样，写在 `core/` 里。
- **物理层 / PHY 层**：调用具体家族收发器（GTP/GTX/GTH/GTY）或 LVDS 的部分。这部分每个家族一套，写在与家族同名的目录里（`gtx7`、`gth7`、`gthUltraScale`、`gthUltraScale+`、`gtyUltraScale+`，或简写 `gthUs`、`gthUs+`、`gtyUs+`）。

`ethernet/README.md` 直接点明了这个模式：

> High-speed cores commonly split shared `core/` logic from FPGA transceiver-family directories. Use `getFpgaArch` guards in ruckus files for family-specific source selection…

这样做的好处：协议逻辑只写一遍（在 `core/`），新增一个 FPGA 家族只需新增一个 PHY 目录，不用动 `core/`。

`getFpgaArch` 是 ruckus 提供的函数，它返回当前工程的家族字符串（如 `kintex7`、`kintexuplus`、`zynquplusRFSOC`、`virtexuplusHBM` 等）。`ruckus.tcl` 用一串 `if` 把「家族 → PHY 目录」的映射表达出来，从而在综合时只把对应家族的 PHY 源加进构建。

#### 4.2.2 核心流程

以 `ethernet/GigEthCore/`（千兆以太网核）为例，它的总清单工作流程是：

```
1. source RUCKUS_PROC_TCL          （加载 ruckus 工具过程）
2. loadRuckusTcl ".../core"        （无条件加载通用 MAC/寄存器逻辑）
3. set family [getFpgaArch]        （读取当前工程的 FPGA 家族）
4. if family == artix7  → load gtp7
   if family == kintex7  → load gtx7
   if family == virtex7  → load gth7
   if family == kintexu/virtexu → load gthUltraScale + lvdsUltraScale
   if family == kintexuplus/zynquplus(RFSOC) → load gthUltraScale+ + gtyUltraScale+ + lvdsUltraScale
   if family == virtexuplus(/HBM) → load gtyUltraScale+ + lvdsUltraScale
```

关键点：

- `core/` 永远加载（家族无关）；
- 家族 PHY 目录按 `getFpgaArch` 的返回值**有条件**加载，且**同一家族可能加载多个 PHY 目录**（UltraScale+ 家族同时需要 GTH 和 GTY 收发器，外加 LVDS 备选）。

#### 4.2.3 源码精读

**（1）千兆核总清单：先加载 core，再用 getFpgaArch 分流**

[ethernet/GigEthCore/ruckus.tcl:4-8](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/ruckus.tcl#L4-L8)：第 4–5 行无条件 `loadRuckusTcl ".../core"`（家族无关的 MAC 逻辑），第 7–8 行用 `set family [getFpgaArch]` 拿到家族字符串，准备分流。

[ethernet/GigEthCore/ruckus.tcl:14-16](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/ruckus.tcl#L14-L16)：`kintex7` 家族 → 加载 `gtx7` PHY 目录。

[ethernet/GigEthCore/ruckus.tcl:36-42](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/ruckus.tcl#L36-L42)：UltraScale+ 家族（`kintexuplus`/`zynquplus`/`zynquplusRFSOC`）一次性加载 `gthUltraScale+`、`gtyUltraScale+`、`lvdsUltraScale` 三个 PHY 目录——同一份 `core/` 协议逻辑，挂在三种不同收发器上。

**（2）core/ 内部：只有家族无关的包与寄存器**

[ethernet/GigEthCore/core/ruckus.tcl:4-5](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/core/ruckus.tcl#L4-L5)：`core/` 的清单只有一句 `loadSource -lib surf -dir ".../rtl"`，无任何家族判断。

它加载的是 [ethernet/GigEthCore/core/rtl/GigEthPkg.vhd:23-30](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/core/rtl/GigEthPkg.vhd#L23-L30)，里面定义的是 `GigEthConfigType` 这类与家族无关的配置记录和 `PAUSE_512BITS_C` 这类常量，外加 `GigEthReg.vhd`（AXI-Lite 寄存器块）。这些对任何家族都通用。

**（3）家族 PHY 目录内部：既加载可综合 RTL，也加载 IP 产物**

[ethernet/GigEthCore/gtx7/ruckus.tcl:5-9](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/gtx7/ruckus.tcl#L5-L9)：`gtx7/` 清单带一个 `VIVADO_VERSION >= 2016.4` 的守卫，满足时既 `loadSource -dir ".../rtl"`（可综合的 `GigEthGtx7.vhd`、`GigEthGtx7Wrapper.vhd`），又 `loadSource -path ".../images/GigEthGtx7Core.dcp"`（布局布线后的网表 checkpoint）。

家族 wrapper 的入口是 [ethernet/GigEthCore/gtx7/rtl/GigEthGtx7Wrapper.vhd:30-40](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/gtx7/rtl/GigEthGtx7Wrapper.vhd#L30-L40)——文件头写明「Gtx7 Wrapper for 1000BASE-X Ethernet」，它把家族无关的 `core/` MAC 逻辑和 GTX 收发器原语拼到一起，对外暴露统一的千兆以太网接口。

**（4）同样的模式在 CoaXPress 里重复出现**

[protocols/coaxpress/ruckus.tcl:4-8](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/ruckus.tcl#L4-L8)：CoaXPress 也是先 `loadRuckusTcl ".../core"`，再按 `getFpgaArch` 在 `gthUs`/`gthUs+`/`gtyUs+` 之间选 PHY。这是仓库内反复出现的「模板」，`protocols/README.md` 第 12 行也专门强调了这一点。

[protocols/README.md:12](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/README.md#L12)：许多协议目录把可移植的 `core/` 或 `rtl/` 与家族 PHY 封装分开。

#### 4.2.4 代码实践（本讲主实践任务）

1. **实践目标**：在 `ethernet/GigEthCore` 下找到 `core/` 与至少一个家族 PHY 目录，说清楚 `ruckus.tcl` 如何用 `getFpgaArch` 在二者之间选择源文件。
2. **操作步骤**（纯阅读，无需综合）：
   - 浏览 `ethernet/GigEthCore/` 下的目录列表，确认存在 `core/` 和 `gtx7/`、`gth7/`、`gthUltraScale+`、`gtyUltraScale+` 等家族目录。
   - 打开 [ethernet/GigEthCore/ruckus.tcl](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/ruckus.tcl)，逐条读完每个 `if`。
   - 打开 `core/` 与某个家族目录（如 `gtx7/`）各自的 `ruckus.tcl`，对比二者加载方式的不同。
3. **需要观察的现象**：
   - `core/ruckus.tcl` 是**无条件**加载（无 `getFpgaArch` 判断）；
   - 家族 PHY 目录**不会**被总清单无条件加载，而是被某个 `if family eq ...` 分支选中后才加载；
   - `gtx7/` 之类的家族清单还会额外加载一个 `.dcp` 网表产物，这是 `core/` 没有的。
4. **预期结果**：你能填出下面这张映射表（示例答案）：

   | `getFpgaArch` 返回值 | 加载的 PHY 目录 |
   |----------------------|-----------------|
   | `artix7` | `gtp7` |
   | `kintex7` | `gtx7` |
   | `virtex7` | `gth7` |
   | `kintexuplus` / `zynquplus` | `gthUltraScale+`、`gtyUltraScale+`、`lvdsUltraScale` |
   | `virtexuplus` | `gtyUltraScale+`、`lvdsUltraScale` |

   并能解释：`core/` 是「协议逻辑只写一遍」，家族目录是「每个家族一套 PHY 胶水」，二者靠总清单的 `getFpgaArch` 分流拼装。
5. 本实践为源码阅读型，不涉及运行；若你要实际综合验证家族选择，需配置好 Vivado 与 ruckus 工程，结果「待本地验证」。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `GigEthPkg.vhd`（定义 `GigEthConfigType`）放在 `core/rtl/` 而不是 `gtx7/rtl/`？
  - **答案**：因为它与收发器家族无关，所有家族共用同一份协议配置；放 `core/` 保证只维护一份。`gtx7/` 只放 GTX 收发器相关的胶水。
- **练习 2**：如果你要新增对 `virtex7` 家族的千兆支持，但 `virtex7` 已经在清单里映射到 `gth7`，而你想改用 LVDS——该改哪里？
  - **答案**：在 `ethernet/GigEthCore/ruckus.tcl` 的 `virtex7` 分支里加上 `loadRuckusTcl "$::DIR_PATH/lvdsUltraScale"`（或新增对应家族 PHY 目录），而不是去动 `core/`。

### 4.3 tests/<subsystem> 子系统组织

#### 4.3.1 概念说明

SURF 的可执行回归测试（cocotb + pytest + GHDL）**不**散落在各源码目录的根下，而是统一收拢在仓库根的 `tests/` 里，并且 `tests/` 的子目录**镜像**源码的子系统划分。例如 `tests/ethernet/EthMacCore/` 对应 `ethernet/EthMacCore/`，`tests/base/fifo/` 对应 `base/fifo/`。

为什么这么做？

1. **构建隔离**：`tests/` 不进 HDL 综合构建图（u1-l1 已说明 `tests` 与 `python` 是不参与 HDL 构建的两块）。如果测试散落在源码根下，容易被 `loadSource -dir` 误带进综合。
2. **按子系统聚焦**：`AGENTS.md` 推荐的命令是 `./.venv/bin/python -m pytest -q tests/<subsystem-or-file>`，目录镜像让你能一眼挑出「我这次改了以太网，就只跑 `tests/ethernet/`」。
3. **公共辅助复用**：所有测试共享 `tests/common/regression_utils.py` 和各子系统自己的 `*_test_utils.py`（帧构造器、记分板），集中放置便于复用。

#### 4.3.2 核心流程

`tests/` 的典型结构是「先按子系统分目录，再放测试文件 + 辅助」：

```
tests/
├── common/
│   └── regression_utils.py     # run_surf_vhdl_test 等公共驱动
├── axi/                         # 镜像 axi/
├── base/{crc,delay,fifo,general,ram,sync}/   # 镜像 base/
├── ethernet/{EthMacCore,IpV4Engine,RawEthFramer,RoCEv2,UdpEngine}/  # 镜像 ethernet/
├── protocols/                   # 镜像 protocols/
├── dsp/                         # 镜像 dsp/
├── README.md                    # 测试方法学说明
└── pytest.ini（在仓库根）       # pytest 发现/并行配置
```

每个子系统目录里：测试文件叫 `test_*.py`，可复用的帧构造/校验辅助叫 `*_test_utils.py`，二者同级。被测的 HDL 测试台（`*Tb.vhd`）则在源码侧的 `tb/` 目录里，由 cocotb 通过 ruckus `import` 生成的源缓存找到。

#### 4.3.3 源码精读

**（1）`tests/` 子系统镜像源码树**——直接对照目录即可验证：

- `tests/base/` 下是 `crc`、`delay`、`fifo`、`general`、`ram`、`sync`，与 `base/` 的 `general`/`sync`/`fifo`/`ram`/`delay`/`crc` 一一对应。
- `tests/ethernet/` 下是 `EthMacCore`、`IpV4Engine`、`RawEthFramer`、`RoCEv2`、`UdpEngine`，与 `ethernet/` 的同名子系统对应。

**（2）一个子系统的测试目录长什么样**——`tests/ethernet/EthMacCore/` 里既有大量 `test_*.py`（如 `test_EthMacRx.py`、`test_EthMacTx.py`、`test_EthMacFlowCtrl.py`），也有共享的 [tests/ethernet/EthMacCore/ethmac_test_utils.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/EthMacCore/ethmac_test_utils.py)，后者负责构造/校验以太网帧，供该子系统所有测试复用。

**（3）各子树 README 都把测试指引指向 `tests/`**：

- [axi/README.md:14](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/README.md#L14)：可执行的 cocotb 测试放 `tests/axi/`。
- [ethernet/README.md:14](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/README.md#L14)：协议级测试放 `tests/ethernet/`。

**（4）`AGENTS.md` 的运行约定**：测试栈是 `pytest + cocotb + GHDL + ruckus`，推荐命令为 `./.venv/bin/python -m pytest -q tests/<subsystem-or-file>`（这在 u1-l2 讲 CI 时已铺垫，u9 会深入）。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：确认 `tests/` 镜像了源码子系统划分，并能定位一个具体测试文件。
2. **操作步骤**：
   - 列出 `tests/ethernet/` 的子目录，再列出 `ethernet/` 的子目录，做对照。
   - 选一个改过的假设场景：若你改了 `ethernet/EthMacCore/rtl/EthMacRx.vhd`，应该在哪个目录里找回归测试？打开 `tests/ethernet/EthMacCore/test_EthMacRx.py` 的文件头，看它的 `Test methodology` 块。
3. **需要观察的现象**：`tests/ethernet/` 的子目录名与 `ethernet/` 下的协议核名一一对应；测试文件 `test_EthMacRx.py` 与被测模块 `EthMacRx.vhd` 同名前缀。
4. **预期结果**：你能写出一句话规则——「源码在 `<subsystem>/<module>/rtl/`，测试在 `tests/<subsystem>/<module>/test_<Module>.py`」。
5. 若想真正跑测试，需先 `make MODULES="$PWD" import` 生成源缓存（见 u1-l2），运行结果「待本地验证」。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 cocotb 测试不直接放在 `ethernet/EthMacCore/` 根下，而要集中到 `tests/ethernet/EthMacCore/`？
  - **答案**：避免被 `ruckus.tcl` 的 `loadSource` 误带进综合构建；同时让 pytest 能按子系统聚焦运行，公共辅助也便于复用。
- **练习 2**：你新加了一个 `protocols/foo/` 协议核并写了 cocotb 测试，测试应放哪？还要顺手加什么？
  - **答案**：放 `tests/protocols/foo/test_Foo.py`，并复用 `tests/common/regression_utils.py` 的驱动；按 `tests/README.md` 的方法学写好 `Test methodology` 头。

## 5. 综合实践

把本讲三个模块串起来，做一次「目录考古」：

1. 选定子系统 **`ethernet/GigEthCore`**（千兆以太网核）。
2. **分类**：在它的 `core/` 和某个家族 PHY 目录（如 `gtx7/`）里，分别指出哪些文件属于 4.1 的哪一类（绝大多数是 `rtl/`；注意 `gtx7/images/*.dcp` 是 IP 产物而非源码分类）。
3. **拆分**：画出 `GigEthCore` 的「`core/` + 家族 PHY」目录树，并填出 4.2.4 那张 `getFpgaArch → PHY 目录` 映射表。
4. **测试**：到 `tests/ethernet/` 下确认没有 `GigEthCore` 目录（千兆核高度依赖具体家族 PHY/网表，目前 SURF 的以太网回归集中在 `EthMacCore`/`IpV4Engine`/`UdpEngine`/`RawEthFramer`/`RoCEv2`），思考：为什么家族相关的 PHY 核比家族无关的 MAC 核更难做纯 GHDL 回归？
5. 产出一份一页笔记，包含：目录树截图（文字版）、映射表、以及对第 4 步问题的回答。

> 第 4 步的答案提示：`core/`（如 `EthMacCore`）是纯 RTL、可在 GHDL 上仿真；而 `gtx7/` 之类的 PHY 目录依赖厂商原语（`unisim`）和 `.dcp` 网表，GHDL 无法直接仿真，因此家族 PHY 核的回归通常要靠 cocotb + Vivado 仿真器或上板验证——这正是测试集中在家族无关模块的原因。

## 6. 本讲小结

- SURF 用**目录名**表达文件角色：`rtl/`（可综合）、`sim/`（仅仿真模型）、`tb/`（测试台）、`wrappers/`（薄封装）、`ip_integrator/`（面向 Vivado BD）。
- 高速协议核普遍采用 **`core/` 通用 RTL + 家族 PHY 目录**的拆分：协议逻辑只写一遍，每个 FPGA 家族一套 PHY 胶水。
- `ruckus.tcl` 用 **`getFpgaArch`** 做家族分流：`core/` 无条件加载，家族 PHY 目录按返回值有条件加载，且同一家族可能同时加载多个 PHY 目录。
- 可执行回归测试统一收拢在 **`tests/<subsystem>/`**，目录镜像源码子系统，既隔离综合构建、又便于按子系统聚焦运行 pytest。
- 复用的 cocotb 封装要紧贴 RTL 放 `wrappers/`/`ip_integrator/`，**不要**塞进 `tests/`，否则会脱离共享构建图。

## 7. 下一步学习建议

- **承接 VHDL 约定**：本讲只讲了「文件放哪」，下一讲 **u1-l4（StdRtlPkg 基础类型与命名约定）** 会讲「文件内部的命名规则」（`sl`/`slv`、`_G`/`_C`/`Type`/`_INIT_C`），与本讲的目录约定互补。
- **承接构建**：想真正动手验证 `getFpgaArch` 的家族选择，复习 **u1-l2（ruckus/Makefile/GHDL/CI）**，并尝试 `make MODULES="$PWD" import`。
- **深入测试**：想了解 `tests/` 内部如何组织 cocotb 回归，跳到单元九的 **u9-l1（cocotb 工具链）** 与 **u9-l2（编写一个 cocotb 测试）**。
- **建议阅读的源码**：`ethernet/GigEthCore/ruckus.tcl`、`protocols/coaxpress/ruckus.tcl`、`xilinx/README.md`（家族封装总览）、`tests/README.md`（测试方法学）。
