# 目录结构与文件分工

## 1. 本讲目标

学完本讲，你应当能够：

- 把仓库里每一个目录名（`hdl`、`tb`、`sim`、`bd`、`xgui`、`scripts`、`drivers`、`doc`）映射到它在一个 Vivado IP 项目中扮演的角色。
- 识别三条贯穿整个项目的主线，并准确说出它们各自的入口文件：
  1. **综合/实现主线**（顶层 RTL）—— `hdl/spi_vivado_wrp.vhd`
  2. **仿真主线**（测试平台）—— `tb/top_tb.vhd`
  3. **打包主线**（把 RTL 变成可被 Vivado 调用的 IP）—— `scripts/package.tcl`
- 理解 `component.xml` 与 `xgui/` 这两类“IP 元数据”文件在 Vivado IP 体系里起什么作用，它们和 RTL 之间是什么关系。

本讲不深入 RTL 内部逻辑（那是进阶层的事），只解决“**东西放在哪儿、谁来用**”这个地图问题。

## 2. 前置知识

在阅读本讲前，请确认你已经理解上一讲（u1-l1）建立的几个概念：

- **IP-core（IP 核）**：一段可复用的硬件设计，封装成可以在 Vivado 图形界面里像“积木”一样拖进工程的组件。
- **SPI Master**：本 IP 的功能角色，由 FPGA 主动产生时钟（SCK）、驱动片选（CS_n）、发送（MOSI）和接收（MISO）数据。
- **AXI4 寄存器接口**：外部主机（例如 Zynq 的 ARM 核）通过这组总线读写 IP 内部寄存器，从而控制 SPI 收发。
- **PSI HDL Library**：本 IP 所属的开源硬件库家族，包含 `psi_common`（公共 RTL）、`psi_tb`（测试辅助）、`PsiSim`（仿真框架）、`PsiIpPackage`（打包工具）等成员。

此外，本讲会用到两个 FPGA 工程领域的通用概念，先做个最简解释：

- **RTL（Register Transfer Level，寄存器传输级）**：用 VHDL/Verilog 描述的、可被综合成真实电路的硬件代码。本项目的 RTL 全部用 VHDL-2008 写成。
- **Testbench（测试平台）**：一段“只为仿真存在”的代码，它给被测器件（DUT, Design Under Test）施加激励、检查输出，不会被综合成电路。
- **Tcl（Tool Command Language）**：Vivado、Modelsim 等工具的脚本语言。本项目大量用 Tcl 来描述“打包一个 IP 需要哪些文件、GUI 长什么样”。

## 3. 本讲源码地图

下表列出本讲涉及的关键文件。第一列是仓库内的相对路径，第二列是它的角色。

| 文件 | 角色 |
| --- | --- |
| `README.md` | 项目说明，其中 Dependencies 段声明了外部依赖与目录结构约定 |
| `hdl/spi_vivado_wrp.vhd` | **顶层 RTL**：对外暴露 AXI 与 SPI 物理端口，对内例化 AXI 解码器与 SPI 核心 |
| `scripts/package.tcl` | **打包脚本**：用 PsiIpPackage 把 RTL 打包成 Vivado IP |
| `component.xml` | **IP 元数据**：Vivado 用来识别“这是一个 IP”的清单文件 |
| `xgui/spi_simple_v1_4.tcl` | **GUI 布局脚本**：定义 Vivado 里定制 IP 参数时的图形页面 |
| `tb/top_tb.vhd` | **测试平台**：例化顶层 RTL 并驱动 AXI/SPI 进行回归测试 |
| `hdl/spi_simple.vhd`、`hdl/definitions_pkg.vhd` | RTL 依赖：SPI 核心实现与寄存器常量包（本讲只点一下，不展开） |

## 4. 核心概念与源码讲解

### 4.1 目录角色速查表

#### 4.1.1 概念说明

一个“能被 Vivado 当作 IP 使用的”FPGA 仓库，通常不止包含 RTL——它还要告诉工具：

- 哪些文件参与综合、哪些只参与仿真；
- 这个 IP 对外有哪些端口、参数（generic）；
- GUI 定制页面长什么样；
- 配套的 C 驱动怎么打包进 BSP；
- 文档（datasheet、logo）放在哪里；
- 工程师怎么跑回归测试、怎么发版。

本项目把这些职责拆到不同目录里，**目录名本身就是一种约定**：看到 `hdl` 就知道是 RTL，看到 `tb` 就知道是测试。这套约定同时服务于两个读者——人类工程师和自动化脚本（打包脚本、CI）。

#### 4.1.2 核心流程

仓库根目录下的内容可以分成三层来理解：

1. **文档与元信息层**（根目录）：`README.md`、`Changelog.md`、`License.txt`、`LGPL2_1.txt`、`component.xml`。
2. **内容层**（按职责分目录）：`hdl`（RTL）、`tb`（测试）、`sim`（仿真脚本）、`drivers`（C 驱动）、`doc`（文档资产）、`bd`（Block Design 自动化）、`xgui`（GUI 布局）。
3. **工程化层**（脚本）：`scripts`（打包、依赖解析、CI、重构脚本）。

完整目录树（基于仓库实际文件）如下：

```
vivadoIP_spi_simple/
├── README.md                  # 项目说明 + 依赖声明（被 dependencies.py 解析）
├── Changelog.md               # 版本演进记录
├── License.txt                # PSI HDL Library License（LGPL + 硬件例外）
├── LGPL2_1.txt                # LGPL 原文
├── component.xml              # ★ Vivado IP 清单（IP 元数据）
├── hdl/                       # ★ RTL 源码（综合主线）
│   ├── definitions_pkg.vhd    #   寄存器索引/状态位/中断位常量包
│   ├── spi_simple.vhd         #   SPI 核心实现（FIFO + 引擎）
│   └── spi_vivado_wrp.vhd     #   ★ 顶层 wrapper（综合主线的入口）
├── tb/                        # ★ 测试平台（仿真主线的内容）
│   └── top_tb.vhd             #   ★ 唯一的 testbench（仿真主线的入口）
├── sim/                       # 仿真脚本（仿真主线的驱动）
│   ├── run.tcl                #   回归测试入口：source ./run.tcl
│   ├── config.tcl             #   声明库/源码/testbench run
│   ├── ci.do                  #   CI 批处理脚本
│   └── interactive.tcl        #   交互式调试脚本
├── drivers/spi_simple/        # C 驱动（随 IP 打包进 BSP）
│   ├── data/spi_simple.mdd    #   驱动描述（Xilinx MDD 格式）
│   ├── data/spi_simple.tcl    #   驱动生成 Tcl
│   └── src/                   #   驱动源码 spi_simple.c / spi_simple.h + Makefile
├── doc/                       # 文档资产
│   ├── spi_simple.pdf         #   Datasheet（GUI 里可打开）
│   ├── spi_simple.docx        #   Datasheet 源文件
│   ├── spi_simple.vsd         #   时序图源文件
│   └── psi_logo_150.gif       #   IP 在 Vivado 里的 logo
├── bd/
│   └── bd.tcl                 # Block Design 自动化钩子（传播 AXI ID 宽度）
├── xgui/
│   └── spi_simple_v1_4.tcl    # ★ GUI 布局脚本（参数定制页面）
└── scripts/                   # 工程化脚本（打包主线 + CI + 依赖）
    ├── package.tcl            #   ★ 打包脚本（打包主线的入口）
    ├── dependencies.py        #   依赖解析脚本
    ├── ciFlow.py              #   CI 仿真驱动
    └── refactoring/           #   代码重构辅助脚本
```

带 ★ 的是本讲重点关注的“入口”文件。

#### 4.1.3 源码精读

**目录结构约定写在 README 里**。README 的 Dependencies 段明确说明外部依赖必须按固定目录摆放，并提示可以用聚合仓库 `psi_fpga_all` 一次性获得正确结构：

- [README.md:L20-L24](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L20-L24) —— 声明“要求的目录结构”以及 `psi_fpga_all` 聚合仓库的作用。注意 README 第 18 行有一句注释 `<!-- DO NOT CHANGE FORMAT: this section is parsed to resolve dependencies -->`，说明这段文字会被 `scripts/dependencies.py` 程序化解析，所以目录/依赖结构既是给人看的，也是给机器读的。

**打包脚本如何引用这些目录**。`scripts/package.tcl` 用相对路径把 `hdl` 下的 RTL 与 `drivers` 下的 C 驱动登记进 IP：

- [scripts/package.tcl:L32-L36](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L32-L36) —— `add_sources_relative` 把 `../hdl/` 下的三个 `.vhd` 文件登记为综合源码。
- [scripts/package.tcl:L56-L59](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L56-L59) —— `add_drivers_relative ../drivers/spi_simple` 把 C 驱动登记进 IP，这样 Vivado 生成 BSP 时会自动带上 `spi_simple.c/.h`。

换句话说，**目录划分不是装饰，而是被打包脚本直接消费的契约**：你把 RTL 放进 `hdl`、驱动放进 `drivers`，`package.tcl` 才能正确识别。

#### 4.1.4 代码实践

> **实践目标**：把上面的目录树抄下来，亲手为每个目录补一句“它的读者是谁、被谁消费”。

1. 在仓库根目录用 `git ls-files` 列出全部被版本控制的文件（这也是本讲目录树的来源）。
2. 对照目录树，在 `hdl`、`tb`、`sim`、`drivers`、`doc`、`bd`、`xgui`、`scripts` 八个目录旁，各写一句话说明它的职责。
3. 再为每个目录标注它的“消费者”：
   - `hdl` → 被 `scripts/package.tcl` 的 `add_sources_relative` 消费；
   - `drivers` → 被 `add_drivers_relative` 消费；
   - `sim`、`tb` → 被人/CI 通过 `sim/run.tcl` 消费；
   - `doc` → 被 `package.tcl` 的 `set_logo_relative` / `set_datasheet_relative` 消费；
   - `xgui`、`bd`、`component.xml` → 被 Vivado 工具自身消费。
4. **需要观察的现象**：你会发现每个目录都恰好对应打包脚本里的一类 `add_*` / `set_*` 命令，目录与命令几乎一一对应。
5. **预期结果**：得到一张“目录 ↔ 职责 ↔ 消费者”的三列表。
6. 若你无法运行 `git ls-files`，可直接以本讲 4.1.2 的目录树为准（**待本地验证**仅指你环境里是否能跑通该命令，目录内容本身来自仓库实际文件）。

#### 4.1.5 小练习与答案

**练习 1**：仓库里既没有 `src/` 也没有 `lib/`，RTL 放在 `hdl/`。这种命名想传达什么？

> **参考答案**：`hdl` 是 “Hardware Description Language” 的缩写，强调这里放的是“描述硬件的语言源码（VHDL/Verilog）”，与放在 `drivers/` 里的软件源码（C 语言）区分开。PSI 全家桶都遵循这个约定。

**练习 2**：`doc/` 下既有 `spi_simple.pdf` 又有 `.docx` 和 `.vsd`，为什么要把“源文件”也提交进仓库？

> **参考答案**：`.pdf` 是给用户/工具看的成品（被 `package.tcl` 登记为 datasheet，Vivado 里可打开）；`.docx`（文档源）和 `.vsd`（时序图源）是编辑用的源文件，提交它们是为了让其他人能继续修改文档而不用从 PDF 反推。

**练习 3**：`scripts/refactoring/` 目录算“内容层”还是“工程化层”？它会被打包进 IP 吗？

> **参考答案**：属于“工程化层”——它是给开发者改代码用的辅助脚本，与 IP 本身的综合/仿真/驱动无关，不会被 `package.tcl` 登记，因此不会进入最终 IP。

---

### 4.2 三处关键入口文件

#### 4.2.1 概念说明

一个仓库文件很多，但理解它通常只需要抓住少数几个“入口”。本项目有三条主线，每条主线都有一个清晰的入口文件：

| 主线 | 入口文件 | 这个文件回答什么问题 |
| --- | --- | --- |
| 综合 / 实现（做成电路） | `hdl/spi_vivado_wrp.vhd` | “这个 IP 对外的端口和参数是什么？内部由哪几个模块拼成？” |
| 仿真（验证行为） | `tb/top_tb.vhd` | “怎么给这个 IP 喂激励、怎么检查它对不对？” |
| 打包（变成 Vivado IP） | `scripts/package.tcl` | “要把哪些文件、参数、GUI 打成一个可分发的 IP？” |

记住这三个文件，就等于拿到了在仓库里“按图索骥”的三个起点。

#### 4.2.2 核心流程

三条主线之间的关系是：

```
          ┌─────────────────────── 打包主线 ───────────────────────┐
          │  scripts/package.tcl                                   │
          │   ├─ 读取 hdl/*.vhd （综合源码）                        │
          │   ├─ 读取 drivers/*    （C 驱动）                       │
          │   ├─ 声明 generics → 生成 xgui + component.xml          │
          │   └─ package_ip → 产出可被 Vivado 调用的 IP             │
          └────────────────────────────────────────────────────────┘
                              ▲ 依赖
          ┌─────────── 综合主线 ───────────┐
          │  hdl/spi_vivado_wrp.vhd (顶层) │
          │   ├─ 例化 AXI 解码器            │
          │   └─ 例化 spi_simple (核心)     │
          └────────────────────────────────┘
                              ▲ 被测
          ┌─────────── 仿真主线 ──────────────────────┐
          │  tb/top_tb.vhd                              │
          │   ├─ 例化 spi_vivado_wrp 作为 DUT           │
          │   ├─ 用 AXI BFM 驱动 AXI 总线               │
          │   └─ p_control / p_spi 两个进程施加激励     │
          │  sim/run.tcl 在工具里启动这个 testbench      │
          └─────────────────────────────────────────────┘
```

要点：**打包主线依赖综合主线**（要打包就得先有 RTL），**仿真主线依赖综合主线**（要测就得有 DUT），而打包与仿真两条主线本身相对独立。

#### 4.2.3 源码精读

**① 综合主线入口：`hdl/spi_vivado_wrp.vhd`**

这个文件的实体名就叫 `spi_vivado_wrp`，它是整个 IP 对外的“脸”。它做两件事：

- 声明全部对外端口：SPI 物理引脚（`spi_sck`/`spi_cs_n`/`spi_mosi`/`spi_miso`/`spi_tri`/`spi_le`）、中断 `irq`、以及完整的 AXI4 从端口。
- 内部例化两个子模块：AXI 解码器 `psi_common_axi_slave_ipif` 和 SPI 核心 `spi_simple`。

关键代码点：

- [hdl/spi_vivado_wrp.vhd:L23-L43](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L23-L43) —— 实体声明与全部 generic（`ClockDivider_g`、`TransWidth_g`、`SlaveCnt_g`、`TriWiresSpi_g` 等）。这些 generic 就是后续在 Vivado GUI 里能调的“参数”。
- [hdl/spi_vivado_wrp.vhd:L49-L54](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L49-L54) —— SPI 物理端口。注意 `spi_cs_n` 和 `spi_le` 的宽度都是 `SlaveCnt_g-1 downto 0`，即“每个从机一根线”。
- [hdl/spi_vivado_wrp.vhd:L136-L145](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L136-L145) —— 例化 AXI 解码器 `psi_common_axi_slave_ipif`（来自 psi_common），把 AXI 总线翻译成简单的寄存器读写握手。
- [hdl/spi_vivado_wrp.vhd:L213-L227](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L213-L227) —— 例化 SPI 核心 `i_spi : entity work.spi_simple`，并把顶层 generic 透传给它。

> 注意：顶层实体名是 `spi_vivado_wrp`，但 IP 在 Vivado 里展示给用户的名字是 `spi_simple`（见 4.3）。这个“内部工程名”与“对外 IP 名”不同，是 IP 项目里的常见现象。

**② 仿真主线入口：`tb/top_tb.vhd`**

这是仓库里唯一的 testbench。它把 `spi_vivado_wrp` 当作被测器件（DUT）例化进来：

- [tb/top_tb.vhd:L83-L85](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L83-L85) —— 注释 `-- DUT` 紧跟例化语句 `i_dut : entity work.spi_vivado_wrp`。这就是仿真与综合两条主线的“接合点”：testbench 例化的正是综合主线的顶层。
- [tb/top_tb.vhd:L167](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L167) —— `p_control` 进程：测试场景的“指挥”，用 AXI BFM 驱动寄存器读写、发起 SPI 事务。
- [tb/top_tb.vhd:L322](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L322) —— `p_spi` 进程：扮演 SPI 从机，逐 bit 收发并校验数据、片选与 LE 信号。

仿真主线由工具脚本驱动启动：在 Modelsim/Vsim 的 `sim/` 目录下执行 `source ./run.tcl` 即可跑通回归（见 [README.md:L50-L53](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L50-L53)）。

**③ 打包主线入口：`scripts/package.tcl`**

这个脚本调用了 PSI 自研的 `PsiIpPackage` 工具，把 RTL 打包成 Vivado IP。它的结构非常清晰：

- [scripts/package.tcl:L10-L11](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L10-L11) —— `source` 引入 PsiIpPackage 工具并导入它的命令命名空间。
- [scripts/package.tcl:L16-L23](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L16-L23) —— 声明 IP 元信息：名字 `spi_simple`、版本 `1.4`、所属库 `PSI`、描述，然后 `init` 初始化打包流程。
- [scripts/package.tcl:L67-L117](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L67-L117) —— 用一连串 `gui_create_parameter` / `gui_parameter_set_range` / `gui_parameter_set_widget_dropdown` 把每个 generic 暴露成 Vivado GUI 参数（这部分会在 u3-l1 详讲）。
- [scripts/package.tcl:L131](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L131) —— `package_ip $TargetDir false true` 真正执行打包：第二个参数 `false` 表示不打开可编辑工程，第三个 `true` 表示跑综合校验。

#### 4.2.4 代码实践

> **实践目标**：亲手验证“三条主线 + 三个入口”的对应关系，而不是只听结论。

1. 打开 `hdl/spi_vivado_wrp.vhd`，定位 L213 的 `i_spi : entity work.spi_simple`。这就是综合主线“顶层 → 核心”的下一跳。确认 `spi_simple.vhd` 确实存在于 `hdl/` 目录。
2. 打开 `tb/top_tb.vhd`，定位 L85 的 `i_dut : entity work.spi_vivado_wrp`。确认它例化的正是第 1 步那个顶层实体，从而把仿真主线与综合主线连起来。
3. 打开 `scripts/package.tcl`，定位 L32–L36 的 `add_sources_relative`。确认它登记的三个文件（`definitions_pkg.vhd`、`spi_simple.vhd`、`spi_vivado_wrp.vhd`）正好覆盖了综合主线的全部 RTL。
4. **需要观察的现象**：三个入口文件之间通过 `entity work.<名字>` 与 `add_sources_relative { ../hdl/... }` 这两种方式相互引用，形成一个闭环——打包登记 RTL，仿真例化 RTL。
5. **预期结果**：你能在三张图/三段引用之间画出箭头：`package.tcl → spi_vivado_wrp.vhd ← top_tb.vhd`。
6. 这一步是纯源码阅读，不需要任何工具链（**待本地验证**仅适用于你是否能在自己的编辑器里打开这些文件）。

#### 4.2.5 小练习与答案

**练习 1**：为什么顶层实体叫 `spi_vivado_wrp`，而后缀 `wrp` 是 wrapper（包装）的缩写？它“包装”了什么？

> **参考答案**：它把内部与平台无关的核心 `spi_simple`（只懂寄存器握手）“包装”成对 Vivado/AXI 友好的形态——对外暴露标准 AXI4 从端口，对内用 `psi_common_axi_slave_ipif` 把 AXI 翻译成简单寄存器读写再喂给 `spi_simple`。这样核心逻辑与总线协议解耦。

**练习 2**：`top_tb.vhd` 里 `p_control` 和 `p_spi` 两个进程，分别扮演现实系统里的什么角色？

> **参考答案**：`p_control` 相当于真实系统里坐在 AXI 总线后面的主机（如 Zynq ARM 核），通过写寄存器命令 IP 收发；`p_spi` 相当于真实的外部 SPI 从机器件，负责在 SCK 边沿逐 bit 移位、回送 MISO，并校验主机的行为是否正确。

**练习 3**：如果有人问你“这个 IP 的对外接口长什么样”，你应该打开哪个文件？为什么不是 `spi_simple.vhd`？

> **参考答案**：打开 `hdl/spi_vivado_wrp.vhd`。因为它是顶层 wrapper，AXI 与 SPI 物理端口都在它的 entity 里；`spi_simple.vhd` 是被它包在里面的核心，对外不直接暴露 AXI 端口，看它会漏掉总线接口这一层。

---

### 4.3 Vivado IP 打包相关文件（component.xml / xgui）

#### 4.3.1 概念说明

仅有 RTL 还不能被 Vivado 当作“IP”。Vivado 需要一份**清单**，告诉它：这个组件叫什么、版本号、有哪些总线接口（AXI、时钟、复位、中断）、有哪些端口、有哪些参数、每个参数取值范围、用哪些源文件、GUI 页面怎么布局。这份清单在 Xilinx 体系里由两类文件承载：

- **`component.xml`**：IP 的“身份证 + 清单”。遵循 IP-XACT（SPIRIT）标准，是 Vivado 识别一个目录为 IP 的依据。本讲你只需要把它理解为“机器读的清单”。
- **`xgui/*.tcl`**：GUI 布局脚本。决定用户双击 IP 定制参数时看到的页面长什么样、每个参数用什么控件（文本框/下拉/复选框）。

**重要事实**：本项目里 `component.xml` 与 `xgui/spi_simple_v1_4.tcl` 是由 `scripts/package.tcl` **自动生成**的产物，日常开发中一般不手改它们，而是改 `package.tcl` 后重新打包。这也是为什么 `package.tcl` 才是“打包主线的入口”，而 `component.xml`/`xgui` 是它的输出。

#### 4.3.2 核心流程

打包一次 IP 时，信息的流动是这样的：

```
开发者编写                    打包工具生成                   Vivado 读取
─────────────                 ─────────────                  ─────────────
scripts/package.tcl  ─┐
  - add_sources        │
  - gui_create_param   ├──> package_ip ──> component.xml  ──> 识别为 IP
  - add_port_enable    │                 (清单/身份证)         列出端口/参数
                      │                                     
                      └────────────────> xgui/*.tcl        ──> 渲染定制 GUI
                                          (页面布局)
hdl/*.vhd  ──────────────────────────────> 被登记为源文件 ──> 综合/仿真
drivers/*  ──────────────────────────────> 被登记为驱动   ──> 进入 BSP
doc/*      ──────────────────────────────> 被登记为资产   ──> logo/datasheet
```

`component.xml` 里几个值得认识的区块：

1. `<spirit:busInterfaces>`：声明 AXI、时钟、复位、中断等总线接口。
2. `<spirit:memoryMaps>`：声明 AXI 寄存器地址空间（基址、范围、位宽）。
3. `<spirit:model>`：声明顶层模型名、综合/仿真等“视图（view）”、端口、参数。
4. `<spirit:fileSets>`：把文件按用途分组（综合文件集、仿真文件集、GUI 文件集、驱动文件集、文档文件集）。
5. `<spirit:parameters>`：列出全部可配置参数及其取值。

#### 4.3.3 源码精读

**① `component.xml` 的身份证与顶层模型**

- [component.xml:L3-L6](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L3-L6) —— vendor=`psi.ch`、library=`PSI`、name=`spi_simple`、version=`1.4`。这就是 IP 在 Vivado IP Catalog 里的“全名”。
- [component.xml:L387-L391](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L387-L391) —— 综合视图 `xilinx_anylanguagesynthesis`，其 `<modelName>` 是 `spi_vivado_wrp`。**这一行非常关键**：它告诉 Vivado“这个 IP 的综合顶层是 `spi_vivado_wrp`”，把 4.2 讲的顶层实体名正式登记进清单。

**② `component.xml` 的总线接口与地址空间**

- [component.xml:L8-L10](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L8-L10) —— 名为 `s00_axi` 的总线接口，类型是 Xilinx 的 `aximm`（AXI 内存映射）。
- [component.xml:L369-L380](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L369-L380) —— 寄存器地址空间：基址 0、范围 256 字节、位宽 32。这解释了为什么 `spi_vivado_wrp.vhd` 里 AXI 地址宽度是 8 位（256 = 2⁸）。
- [component.xml:L346-L367](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L346-L367) —— `irq` 总线接口声明为 `interrupt`，敏感度 `LEVEL_HIGH`（高电平触发），对应 RTL 里 `irq : out std_logic`。

**③ `component.xml` 的端口条件使能**

这是 3-Wire SPI（HEAD 新增功能）落进清单的体现：

- [component.xml:L544-L550](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L544-L550) —— `spi_tri` 端口带一段 `xilinx:enablement`，依赖条件 `$TriWiresSpi_g = true`。意思是：只有用户把 `TriWiresSpi_g` 勾上时，`spi_tri` 这个端口才会出现在 IP 对外接口里；否则该端口被“裁掉”。这条条件在打包脚本里也有对应声明，见 [scripts/package.tcl:L124](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L124) 的 `add_port_enablement_condition "spi_tri" "\$TriWiresSpi_g = true"`——这就是“脚本声明 → 清单生成”的端到端证据。

**④ `component.xml` 的文件集分组**

- [component.xml:L1313](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L1313) 与 [component.xml:L1430-L1436](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L1430-L1436) —— 综合文件集与 `xilinx_xpgui_view_fileset`（GUI 文件集，登记了 `xgui/spi_simple_v1_4.tcl`）。
- [component.xml:L1452-L1474](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L1452-L1474) —— `xilinx_softwaredriver_view_fileset`，把 `drivers/spi_simple/` 下的 `.mdd`、`.tcl`、`Makefile`、`.c`、`.h` 全部登记进来，这正是 4.1 里“驱动被消费”的最终落点。

**⑤ `xgui/spi_simple_v1_4.tcl` 的页面布局**

- [xgui/spi_simple_v1_4.tcl:L2-L21](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/xgui/spi_simple_v1_4.tcl#L2-L21) —— `init_gui` 过程：先 `ipgui::add_page` 建一个名为 `Configuration` 的页面，再逐个 `ipgui::add_param` 把 `ClockDivider_g`、`SpiCPOL_g` 等参数摆上去，下拉项（如 CPOL/CPHA）用 `-widget comboBox`。这段 Tcl 决定了用户在 Vivado 里看到的定制界面。

> 名字里带 `v1_4` 与 `package.tcl` 里 `IP_VERSION 1.4` 对应：每改一次大版本，Vivado 会生成新的 xgui 文件名。

#### 4.3.4 代码实践

> **实践目标**：验证 `package.tcl`（人写）→ `component.xml`/`xgui`（机器生成）的对应关系。

1. 在 `scripts/package.tcl` 第 88–90 行找到 `SlaveCnt_g` 的 GUI 声明（`gui_create_parameter "SlaveCnt_g" ...` 并 `gui_parameter_set_range 1 128`）。
2. 在 `component.xml` 中搜索 `SlaveCnt_g`，找到它在 `<spirit:parameters>` 里的条目（约 L1503-L1507），确认其 `minimum=1 maximum=128`，与脚本声明的范围一致。
3. 在 `xgui/spi_simple_v1_4.tcl` 第 11 行确认 `SlaveCnt_g` 被加进了 `Configuration` 页面。
4. **需要观察的现象**：同一个参数 `SlaveCnt_g` 在三个文件里都出现，且取值范围/名字完全一致——这正是“一次声明、三处生成”的证据。
5. **预期结果**：你能列出 `SlaveCnt_g` 在 `package.tcl`（声明）、`component.xml`（清单）、`xgui/*.tcl`（界面）三处的具体行号与取值范围，三者吻合。
6. 同理可以抽查 `TriWiresSpi_g`：它在 `package.tcl` L107–109 用复选框声明、在 `component.xml` 是 boolean 参数、并驱动 `spi_tri` 端口的 enablement（L547）。若找不到任一处对应，说明该文件可能已被改动，请以仓库 HEAD 为准（**待本地验证**你本地的 HEAD 是否与讲义一致）。

#### 4.3.5 小练习与答案

**练习 1**：既然 `component.xml` 内容这么全，为什么开发者还要维护 `package.tcl`，而不是直接编辑 `component.xml`？

> **参考答案**：因为 `component.xml` 体积大、重复多（综合与仿真两套文件集几乎重复登记一遍）、且手改容易和工具产生不一致。`package.tcl` 是简洁的“声明式来源”，改它再重新打包，工具会重新生成正确的 `component.xml`/`xgui`，更不易出错。

**练习 2**：`component.xml` 里 `memoryMap` 的 range 是 256，`spi_vivado_wrp.vhd` 里 `s00_axi_awaddr` 的宽度是 8 位，二者有什么关系？

> **参考答案**：256 = 2⁸。地址空间范围 256 字节正好对应 8 位地址宽度，两者一致，IP 才能正确解码寄存器地址。

**练习 3**：如果不勾选 `TriWiresSpi_g`，`spi_tri` 端口会怎样？这个行为是在哪一行声明的？

> **参考答案**：`spi_tri` 不会出现在 IP 对外端口里（被裁掉）。该行为在 `component.xml` 第 547 行的 `xilinx:dependency="$TriWiresSpi_g = true"` 声明，对应来源是 `scripts/package.tcl` 第 124 行的 `add_port_enablement_condition`。

## 5. 综合实践

把本讲三节的内容串起来，完成下面这个“目录地图”小任务：

1. 用 `git ls-files`（或本讲 4.1.2 的目录树）画出仓库的完整目录树。
2. 在树上用三种颜色/标记标出三条主线：
   - 综合主线：`hdl/spi_vivado_wrp.vhd` →（例化）→ `hdl/spi_simple.vhd` + `hdl/definitions_pkg.vhd`
   - 仿真主线：`tb/top_tb.vhd` →（例化）→ `hdl/spi_vivado_wrp.vhd`，由 `sim/run.tcl` 启动
   - 打包主线：`scripts/package.tcl` →（登记）→ `hdl/*` + `drivers/*` + `doc/*` →（生成）→ `component.xml` + `xgui/spi_simple_v1_4.tcl`
3. 在 `component.xml` 中找出综合视图的 `<modelName>`（L388 附近），确认它等于综合主线的顶层实体名 `spi_vivado_wrp`——这是“打包主线把综合主线登记为顶层”的关键证据。
4. 仿照 4.3.4 的方法，自选一个 generic（例如 `FifoDepth_g`），分别在 `package.tcl`、`component.xml`、`xgui/spi_simple_v1_4.tcl` 三处找到它的声明，验证三者一致。

> 完成后，你应当得到一张“目录树 + 三色主线 + 一个参数的三处对应”的速查图。这张图就是你后续阅读进阶讲义（u2）时随时可以回看的地图。

## 6. 本讲小结

- 仓库按职责分目录：`hdl`=RTL、`tb`=测试、`sim`=仿真脚本、`drivers`=C 驱动、`doc`=文档、`bd`=Block Design 自动化、`xgui`=GUI 布局、`scripts`=打包/CI/依赖。
- 目录划分是“契约”：`scripts/package.tcl` 用 `add_sources_relative` / `add_drivers_relative` 等命令直接消费这些目录，README 的 Dependencies 段还会被 `dependencies.py` 程序化解析。
- 三条主线各有入口：综合→`hdl/spi_vivado_wrp.vhd`、仿真→`tb/top_tb.vhd`、打包→`scripts/package.tcl`；testbench 通过 `entity work.spi_vivado_wrp` 与顶层实体对接。
- `spi_vivado_wrp` 是“工程顶层实体名”，`spi_simple` 是“对外 IP 名”，二者不同；`component.xml` 的 `<modelName>` 把 `spi_vivado_wrp` 登记为综合顶层。
- `component.xml`（IP-XACT 清单）与 `xgui/*.tcl`（GUI 布局）是 `package.tcl` 的自动生成产物，日常开发改 `package.tcl` 而非它们。
- 端口条件使能（如 `spi_tri` 依赖 `TriWiresSpi_g`）在 `package.tcl` 声明、在 `component.xml` 体现，是 HEAD 新增 3-Wire SPI 能力的落点。

## 7. 下一步学习建议

- 接下来进入单元一的剩余入门讲义：
  - **u1-l3 工具链、依赖与获取方式**：搞清 `scripts/dependencies.py` 怎样解析 README 拉取 `psi_common`/`psi_tb` 等依赖，把本讲提到的“外部依赖目录结构”讲透。
  - **u1-l4 仿真与回归测试运行方式**：把本讲提到的 `sim/run.tcl`、`config.tcl`、`ci.do` 逐行讲清，让你能真正跑通 `tb/top_tb.vhd`。
- 之后再进入进阶单元（u2），从 `hdl/definitions_pkg.vhd` 的寄存器地图开始，正式钻进 RTL 内部。
- 建议同时把本讲的“三色主线地图”保存下来，进阶讲义每次提到某个文件时，都可以回到这张地图定位它在哪条主线上。
