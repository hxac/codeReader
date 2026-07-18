# 仓库目录结构与文件放置策略

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 AERIS-10 仓库顶层每个**编号目录**（`1_` 到 `9_`）各自的职责，并能在不翻 README 的情况下判断某类文件应该去哪个目录。
- 理解 README 里写明的「**文件放置策略**」：哪些产物要入库（tracked）、哪些要被 gitignore，以及它们各自的正确落点。
- 区分「源代码」「文档站点」「生成产物」三类东西的归属，避免把 `.vcd`、`reports/`、临时 CSV 丢进仓库根目录。
- 打开 `docs/index.html`，看懂这个用 GitHub Pages 托管的工程文档站点是怎么组织的。

本讲**不深入任何代码细节**，只解决一个问题：**拿到这个庞杂的雷达仓库后，我该往哪里找东西、又该往哪里放东西。** 这是后续每一篇讲义的前提——连目录都认不全，读源码就会迷路。

---

## 2. 前置知识

本讲承接上一讲 [u1-l1 项目定位与 AERIS-10 雷达系统概览](./u1-l1-project-overview.md)。你已经知道：

- 仓库名 `PLFM_RADAR` = Pulse Linear Frequency Modulated Radar，产品代号 **AERIS-10**，一个开源 10.5 GHz 相控阵雷达。
- 它有 Nexus（3 km）和 Extended（20 km）两个变体，**共享同一套 FPGA + STM32 + Python GUI 软件平台**，差异主要在天线与发射功率。
- 硬件设计文件用 **CERN-OHL-P** 许可，软件/固件（Verilog/STM32/Python）用 **MIT**。

本讲需要的少量额外背景：

- **Git 仓库的「入库（tracked）」与「忽略（gitignored）」概念**：tracked 文件会被版本管理、随提交进入历史；gitignored 文件只存在你本地，不进仓库。一个干净的开源仓库，通常只入库「人手写出来的源」，而把「机器跑出来的产物」忽略掉。
- **生成产物（artifact）**：指由工具链自动产出的文件，例如 FPGA 仿真产生的 `.vcd`/`.vvp`、Python 跑出来的 `.pyc`、Vivado 综合出的报告。它们可以随时被重新生成，因此**不该占着仓库**。
- **GitHub Pages**：GitHub 提供的静态站点托管服务，可以直接把仓库里 `docs/` 目录下的 HTML 当作一个网站发布。本项目用它来托管工程文档。

---

## 3. 本讲源码地图

本讲「源码」其实是**仓库自身的组织文件**，它们决定了整个项目的目录规则：

| 文件 | 作用 |
|------|------|
| [`README.md`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/README.md) | 仓库说明书，包含「文件放置策略」「硬件装配路径」「许可证」「文档入口」等所有顶层约定。 |
| [`.gitignore`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/.gitignore) | 告诉 Git 哪些生成产物不入库；它是「文件放置策略」的执行层。 |
| [`docs/index.html`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/index.html) | GitHub Pages 文档站点的首页，列出了架构、实现日志、bring-up、报告、发布说明五个文档入口。 |
| [`CONTRIBUTING.md`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/CONTRIBUTING.md) | 贡献指南，其中有一张「Repository layout」速查表，是理解目录职责的官方小抄。 |

---

## 4. 核心概念与源码讲解

本讲的三个最小模块：**目录约定**、**文件放置策略**、**docs 站点**。

### 4.1 目录约定：编号目录体系

#### 4.1.1 概念说明

AERIS-10 是一个**跨硬件、固件、软件**的大型开源项目，仓库里同时放着 PDF 原理图、`.xlsx` 电源表、Verilog RTL、C/C++ MCU 代码、Python GUI、电磁仿真脚本、datasheet…… 如果把这些东西随意堆放，查找成本会极高。

项目采用的策略是：**用数字前缀给顶层目录排序，每个编号代表一类产物**。这种「编号目录」约定常见于航天、医疗等严肃硬件项目，好处是：

1. **顺序即优先级**：从 `1_项目说明` 到 `9_固件`，数字小的偏「文档/规划」，数字大的偏「代码/实现」，符合「先理解再动手」的阅读顺序。
2. **一眼分类**：看到 `4_` 就知道是硬件设计文件，看到 `9_` 就知道是固件代码，不需要打开目录猜内容。
3. **跨语言中立**：编号不依赖英文单词，对国际协作者更友好。

> 小贴士：目录名里的中文/英文混排（如 `4_Schematics and Boards Layout`）是刻意保留的——编号负责排序，文字负责说明。

#### 4.1.2 核心流程

仓库顶层的真实结构（你可以用 `ls` 验证）：

```
PLFM_RADAR/
├── 1_Project_Description/                  # 项目说明书（Word 文档）
├── 2_Functional Diagram & Interconnection Matrices/   # 功能框图与互联矩阵
├── 3_Power Management/                     # 电源管理（Excel 时序表）
├── 4_Schematics and Boards Layout/         # 原理图、PCB、生产文件（CERN-OHL-P）
├── 5_Simulations/                          # 天线/RF/滤波器仿真脚本与结果
├── 6_Application Notes/                    # 应用笔记（如 ADI 的 UG-290）
├── 7_Components Datasheets and Application notes/    # 元器件数据手册
├── 8_Utils/                                # 图片、机械图纸、辅助素材
├── 9_Firmware/                             # FPGA + STM32 + GUI 固件与软件（MIT）
├── docs/                                   # GitHub Pages 文档站点
├── .github/workflows/                      # CI 配置（ci-tests.yml）
├── README.md / CONTRIBUTING.md / Licence / pyproject.toml / .gitignore
└── PLFM_RADAR-tutorial/                    # 本学习手册（由工具生成）
```

最关键的「代码层」全部集中在 `9_Firmware/`，它内部又按三种实现技术进一步细分：

```
9_Firmware/
├── 9_1_Microcontroller/        # STM32 C/C++ 固件
│   ├── 9_1_1_C_Cpp_Libraries/  # 驱动库（AD9523/ADF4382/ADAR1000…）
│   ├── 9_1_2_C_Cpp_Algorithms/ # 算法
│   ├── 9_1_3_C_Cpp_Code/       # main.cpp 等应用入口
│   └── tests/                  # 主机端单元测试（shim/mock）
├── 9_2_FPGA/                   # Verilog RTL + 约束 + 测试 + 构建脚本
│   ├── *.v                     # radar_system_top.v 等设计源
│   ├── constraints/            # *.xdc 引脚/时序约束
│   ├── formal/                 # 形式化验证（.sby）
│   ├── scripts/                # Vivado TCL 构建流（tracked）
│   └── tb/                     # testbench + cosim
├── 9_3_GUI/                    # Python GUI（GUI_V7_PyQt.py / v7 包 / radar_protocol.py）
├── tests/cross_layer/          # 跨层契约测试（Python↔Verilog↔C）
└── tools/                      # 辅助工具（uart_capture.py）
```

注意 `9_Firmware` 内部**又复用了「9_1/9_2/9_3」编号**，规则与顶层一致：编号区分技术栈，`tests/` 则把跨层的系统级测试单独抽出来。

#### 4.1.3 源码精读

**（1）CONTRIBUTING.md 给出的官方目录速查表。** 这是最权威的「目录职责」说明，建议记下来：

[CONTRIBUTING.md:19-28](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/CONTRIBUTING.md#L19-L28) —— 这张表把仓库最常改动的五个路径（硬件设计、MCU 固件、FPGA RTL、Python GUI、跨层测试、docs 站点）一行一个说清楚，是贡献者的第一份导航。

**（2）README 的「硬件装配」段落直接引用了编号目录的真实路径。** 这些路径就是上面目录树里真实存在的目录：

[README.md:160-164](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/README.md#L160-L164) —— 说明下单 PCB 用 `4_7_Production Files`、装配原理图用 `4_6_Schematics`、外壳机械图纸用 `8_Utils/Mechanical_Drawings`。这段把「编号目录」与「实际任务」对应起来，读者照着路径就能找到文件。

> 你可以在仓库里逐一验证：`4_Schematics and Boards Layout/4_7_Production Files/` 下确实有 `Gerber_Main_Board`、`Gerber_PA`、`Gerber_Patch_Antenna`、`Gerber_PowerBoard`、`Gerber_freq_synth` 五块板的 Gerber 子目录，与「下单 PCB」用途吻合。

#### 4.1.4 代码实践

**实践目标**：用命令亲自把仓库的真实目录结构「打印」出来，加深对编号体系的印象，而不是只看本讲的图。

**操作步骤**：

1. 在仓库根目录执行，列出所有顶层目录（含隐藏的 `.github`）：

   ```bash
   ls -d */ .github/
   ```

2. 再列出 `9_Firmware/` 一级和二级结构：

   ```bash
   ls 9_Firmware/
   ls -d 9_Firmware/9_2_FPGA/*/
   ```

3. 用 Git 的「已跟踪文件清单」查看仓库**实际入库**了哪些目录（注意被 gitignore 的目录不会出现）：

   ```bash
   git ls-files | sed 's#/.*##' | sort -u
   ```

**需要观察的现象**：

- 顶层会看到 `1_` 到 `9_` 的编号目录，以及 `docs/`、`.github/`。
- `git ls-files` 的输出里**不会**出现 `5_Simulations/generated/`、`9_Firmware/9_2_FPGA/reports/`、`build*_reports/`、`logs/`、`PLFM_RADAR-tutorial/`（这些要么被 gitignore，要么是未跟踪的新目录）。

**预期结果**：你能把屏幕上看到的真实目录与本讲 §4.1.2 的目录树一一对应；并发现 `generated/`、`reports/` 这类目录「在硬盘上可能存在、但 Git 不跟踪」——这就自然引出下一个模块「文件放置策略」。

> 待本地验证：`generated/`、`reports/` 目录在你本地是否存在，取决于你是否跑过仿真/综合。若没跑过，它们可能尚不存在，这属于正常现象。

#### 4.1.5 小练习与答案

**练习 1**：你想找 STM32 配置 ADAR1000 相移器的 C 驱动代码，应该去哪个目录？为什么？
**答案**：去 `9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/`。因为 `9_1` 是 MCU 固件，`9_1_1` 是「驱动库」子层，ADAR1000 的驱动（如 `ADAR1000_Manager.cpp`）属于外设驱动库。

**练习 2**：`4_` 开头的目录为什么用 CERN-OHL-P 许可，而 `9_` 开头的用 MIT？
**答案**：因为 `4_` 目录装的是硬件设计文件（原理图、PCB、Gerber、BOM），属于「硬件产物」，需要 CERN-OHL-P 的专利保护与责任限制；`9_` 目录装的是 Verilog/C/Python 代码，属于「软件产物」，用 MIT 追求最大灵活性。这与上一讲讲的「硬件/软件双许可证」一一对应。

**练习 3**：`9_Firmware/tests/` 和 `9_Firmware/9_1_Microcontroller/tests/` 有什么不同？
**答案**：`9_1_Microcontroller/tests/` 是**单层**（仅 MCU）的主机端单元测试（bug 回归、安全测试）；`9_Firmware/tests/`（含 `cross_layer/`）是**跨层系统级**测试，验证 Python↔Verilog↔C 三层之间的契约一致性，范围更大。

---

### 4.2 文件放置策略：什么入库、什么 gitignore

#### 4.2.1 概念说明

「文件放置策略」回答的是一个工程纪律问题：**机器生成的产物，应该放在哪里？**

举几个本项目里真实会出现的产物：

- FPGA 仿真跑出来的波形转储 `*.vcd`、`*.vvp`。
- DDC/CIC/FIR 等模块仿真导出的 `cic_*.csv`、`fir_*.csv`、`nco_*.csv`、`ddc_*.csv`。
- openEMS 天线仿真生成的方向图、临时分析目录。
- Vivado 综合后的时序报告快照、`.bit` 流文件以外的中间产物。
- Python 的 `__pycache__/`、`*.pyc`。

这些东西**体积大、可重新生成、且对其他协作者没有直接价值**。如果把它们入库，仓库会迅速膨胀、`git clone` 变慢、diff 被噪声淹没。因此项目的策略是：

- **人手写的源、需要被审查与版本化的产物** → 入库（tracked）。
- **可重新生成的本地产物** → 放到约定目录，并写进 `.gitignore`。
- **正式发布的报告（PDF 等，需要对外可见）** → 放 `docs/` 并入库，还能被 GitHub Pages 发布。

#### 4.2.2 核心流程

README 用一句话定调，再给出四条落点规则。整理成判定流程：

```
我手上有一个新文件，该放哪？
│
├── 是「正式发布的报告/文档」吗？
│       └── 是 → docs/                  （tracked，GitHub Pages 会发布）
│
├── 是「仿真临时输出」（图、场景、分析目录）吗？
│       └── 是 → 5_Simulations/generated/   （gitignored，仅本地）
│
├── 是「FPGA/Vivado 产物」（VCD/VVP/CSV/报告快照）吗？
│       └── 是 → 9_Firmware/9_2_FPGA/reports/  （gitignored，仅本地）
│
├── 是「可复用的 FPGA 自动化脚本」（TCL 构建流）吗？
│       └── 是 → 9_Firmware/9_2_FPGA/scripts/  （tracked，入库）
│
└── 都不是 / 拿不准
        └── 绝不要丢在仓库根目录！按文件类型选上述最接近的一个。
```

关键纪律：**「不要把生成产物留在仓库根目录」**。根目录只放顶层说明文件（`README.md`、`CONTRIBUTING.md`、`Licence`、`pyproject.toml`、`.gitignore`）和编号目录。

#### 4.2.3 源码精读

**（1）README 的「Repository File Placement Policy」整段。** 这是策略的权威定义：

[README.md:136-149](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/README.md#L136-L149) —— 明确列出四类落点：发布报告进 `docs/`（tracked）、仿真输出进 `5_Simulations/generated/`（gitignored）、FPGA 产物进 `9_Firmware/9_2_FPGA/reports/`（gitignored）、可复用脚本进 `9_Firmware/9_2_FPGA/scripts/`（tracked），并以一句「**Do not leave generated artifacts in the repository root.**」收尾。

**（2）`.gitignore` 是上面策略的执行层。** 我们逐段看它忽略了什么——你会发现忽略路径与 README 的落点完全吻合：

[.gitignore:1-4](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/.gitignore#L1-L4) —— 忽略 Verilog 仿真转储 `*.vvp`、`*.vcd`，这就是「FPGA/Vivado generated artifacts」里的波形文件。

[.gitignore:11-34](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/.gitignore#L11-L34) —— 忽略大量 `cic_*.csv`、`fir_*.csv`、`nco_*.csv`、`ddc_*.csv`、`mf_pipeline_output.csv`、`rbd_mode*.csv`、`rmc_autoscan.csv`，以及 cosim 中间 CSV（`tb/cosim/rtl_doppler_*.csv` 等）。这些正是 DDC/抽取/匹配滤波/距离抽取/Doppler 模块仿真跑出来的临时数据。

[.gitignore:39-49](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/.gitignore#L39-L49) —— 忽略 Python 的 `__pycache__/`、`*.pyc`，并显式忽略 `5_Simulations/generated/` 与两个本地雷达仿真脚本 `5_Simulations/aeris10_antenna_sim.py`、`5_Simulations/aeris10_radar_sim.py`（注释写明「regenerated by scripts」「local organization」）。

[.gitignore:51-59](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/.gitignore#L51-L59) —— 忽略 `9_Firmware/9_2_FPGA/reports/`、`synth_only.xdc`（局部约束草稿）、`build*_reports/`（时序收敛报告快照）、`logs/`（UART 抓包日志）。这与 README 说的「FPGA/Vivado generated artifacts → reports/」完全对上。

> 对比要点：`9_Firmware/9_2_FPGA/reports/` 被 ignore，而同级的 `9_Firmware/9_2_FPGA/scripts/` 却是 tracked。**同样是 FPGA 目录下的东西，是不是「产物」决定了它的命运**——脚本要入库供大家复用，报告不入库因为能重生成。

**（3）CONTRIBUTING.md 也再次强调这条纪律。**

[CONTRIBUTING.md:8-9](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/CONTRIBUTING.md#L8-L9) —— 「Keep generated outputs (Vivado projects, bitstreams, build logs) out of version control」，即 Vivado 工程、bitstream、构建日志一律不入库。

#### 4.2.4 代码实践

**实践目标**：列出三类「应被 gitignore 而非入库」的生成产物，并给出它们各自的正确存放路径，验证你能把策略应用到真实文件。

**操作步骤**：

1. 打开 `.gitignore`，挑出三组典型的忽略规则（建议选：仿真波形、CSV 输出、Python 缓存）。
2. 对每一组，写出：① 被忽略的文件名模式；② 它属于哪类产物；③ 正确的存放路径（与 README 策略对照）。
3. 用下面命令验证 Git 确实不跟踪这些文件（任选一个 `.csv` 模式检查）：

   ```bash
   git check-ignore -v 9_Firmware/9_2_FPGA/cic_test.csv
   ```

   （该命令会告诉你：假如这个文件存在，命中了 `.gitignore` 的哪一行。）如果本地确实没有该文件，`git check-ignore` 只会按规则判定，不报错。

**需要观察的现象 / 预期结果**：

| 生成产物（示例文件名） | 属于哪类 | 正确存放路径 | 是否入库 |
|---|---|---|---|
| `wave.vcd`、`sim.vvp` | Verilog 仿真波形 | 任意本地目录（建议 `9_Firmware/9_2_FPGA/reports/`） | 否，被 `*.vcd`/`*.vvp` 忽略 |
| `fir_out.csv`、`ddc_out.csv` | 模块仿真 CSV 输出 | `9_Firmware/9_2_FPGA/reports/`（或 `tb/` 下） | 否，被 `fir_*.csv` 等忽略 |
| `__pycache__/foo.pyc` | Python 字节码缓存 | 自动生成在源码旁 | 否，被 `__pycache__/`/`*.pyc` 忽略 |

> 待本地验证：表中具体文件名是否在你的硬盘上出现，取决于你跑过哪些仿真。重点是掌握「规则 → 路径 → 是否入库」的映射，而不是某个文件是否存在。

#### 4.2.5 小练习与答案

**练习 1**：你用 openEMS 跑了一次天线仿真，得到一批方向图 PNG 和一个临时分析目录。该放哪？入库吗？
**答案**：放到 `5_Simulations/generated/`，**不入库**（该目录被 `.gitignore` 第 47 行忽略）。如果这是一份需要对外发布的正式天线报告 PDF，则改放 `docs/` 并入库。

**练习 2**：你写了一个新的 TCL 构建脚本 `build_te0713_heartbeep.tcl`，该放哪？入库吗？
**答案**：放到 `9_Firmware/9_2_FPGA/scripts/`（具体落到对应板卡的子目录，如 `scripts/te0713/`），**入库**（tracked）。因为它是「可复用的 FPGA 自动化脚本」，README 明确把这一类列为 tracked。

**练习 3**：`git ls-files` 的结果里看不到 `9_Firmware/9_2_FPGA/reports/`，但能看到 `9_Firmware/9_2_FPGA/scripts/`。为什么？
**答案**：因为 `reports/` 在 `.gitignore` 里被忽略（它装的是可重新生成的 Vivado 产物），而 `scripts/` 是人手写的构建脚本、需要被审查与共享，所以入库。Git 只跟踪未被忽略的文件。

---

### 4.3 docs 站点：GitHub Pages 工程文档

#### 4.3.1 概念说明

除了源码和硬件文件，项目还有一份「**工程文档站点**」，托管在 `docs/` 目录下，通过 GitHub Pages 发布成一个真正的网站。它的作用和 README 不同：

- **README** 面向「第一次来的人」，讲项目是什么、怎么上手。
- **docs 站点** 面向「已经在做工程的人」，跟踪架构、实现变更历史、硬件 bring-up 准备度、测试报告、发布说明。

之所以单独建一个站点，是因为这些内容（架构决策、时序基线、回归状态、风险清单）会**持续更新**，用 HTML 页面 + GitHub Pages 比塞进 README 更易维护和导航。

#### 4.3.2 核心流程

`docs/` 目录的真实内容：

```
docs/
├── index.html               # 站点首页（导航 + 关键指标卡片）
├── architecture.html        # 系统与 FPGA 架构
├── implementation-log.html  # 工程变更时间线
├── bring-up.html            # 硬件 bring-up 计划 / 风险清单
├── reports.html             # 发布报告入口
├── release-notes.html       # 按关键 commit 的发布说明
├── board-day-worksheet.html # 板日测试工作表
├── assets/                  # style.css + img/（站点样式与图片）
├── artifacts/               # 关键构建产物（.bit 流文件、.rpt 时序报告、说明 .md）
├── AERIS_Antenna_Report.pdf        # 正式报告（tracked，对外发布）
├── AERIS_Simulation_Report.pdf
└── AERIS_Simulation_Report_v2.pdf
```

注意 `docs/` 与「文件放置策略」的呼应：**正式报告 PDF 放在 `docs/` 并入库**，正是 §4.2 里说的「Published reports (tracked, GitHub Pages)」这一类。而 `docs/artifacts/` 下的 `.bit`（心跳流文件）虽然也是产物，但属于「需要长期留底的里程碑构建」，因此作为例外入库，并配 `.md` 说明文档。

站点的访问方式：GitHub 会把仓库 `docs/` 目录作为 Pages 源发布，README 给出链接：

[README.md:205-213](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/README.md#L205-L213) —— 列出 `docs` 文件夹与 GitHub Pages 地址，并把五个核心文档（Architecture / Implementation Log / Bring-Up / Test Reports / Release Notes）做成链接。

#### 4.3.3 源码精读

**（1）首页的导航栏定义了站点的五个核心板块。**

[docs/index.html:12-19](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/index.html#L12-L19) —— `<nav>` 里依次链接 `architecture.html`、`implementation-log.html`、`bring-up.html`、`reports.html`、`release-notes.html`，这就是整个站点的主干结构。

**（2）首页顶部的「指标卡片」透露了项目当前工程状态**（这部分内容后续 u10/u11 讲会用到，这里先建立印象）：

[docs/index.html:34-60](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/index.html#L34-L60) —— 卡片显示：量产板 USB 用 FT2232H（FT601 仅 200T 高端开发板有）、时序基线 WNS +0.058 ns、回归状态 MCU 15/15 与 FPGA 18/18、当前处于「Pre-Hardware Readiness（硬件到货前就绪）」阶段。这张卡片说明 docs 站点承担着「工程状态看板」的角色。

**（3）首页「Documentation Map」再次列出文档入口，是站点结构的目录页。**

[docs/index.html:74-81](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/index.html#L74-L81) —— 用一句话概括每个文档的内容（如 bring-up.html = Pre-Arrival Bring-Up Plan, Artifact Checklist, and Open Risks），帮你按需选读。

**（4）页脚点明发布来源。**

[docs/index.html:86-89](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/index.html#L86-L89) —— 「AERIS-10 documentation published via GitHub Pages from `/docs`.」，明确整个站点是从仓库 `/docs` 目录发布的。

#### 4.3.4 代码实践

**实践目标**：把 docs 站点当作一个「真实可访问的工程看板」用起来，而不是只当文件。

**操作步骤**：

1. 在本地用浏览器打开首页（任选其一）：
   - 离线：直接用浏览器打开 `docs/index.html`（样式由同目录 `assets/style.css` 提供）。
   - 在线：访问 README 给出的 Pages 地址 `https://NawfalMotii79.github.io/PLFM_RADAR/docs/`（待本地验证网络可达）。
2. 在首页找到「Documentation Map」区块，点开 `architecture.html`，浏览一下系统架构页。
3. 回到首页，记录四张「指标卡片」里写的：① 量产板 USB 方案；② 时序基线 WNS 值；③ 回归状态 MCU/FPGA 各多少项通过；④ 当前工程阶段。

**需要观察的现象**：

- 站点顶部导航有五个固定入口（Architecture / Implementation Log / Bring-Up / Reports / Release Notes）。
- 首页卡片是「会随工程进展变化的实时状态」，而不是静态宣传语。

**预期结果**：你能在不看本讲答案的情况下，从首页复述出「量产板用 FT2232H、FT601 仅高端开发板有」「WNS +0.058 ns」「MCU 15/15、FPGA 18/18」「处于 Pre-Hardware Readiness」这几条工程事实。

> 待本地验证：GitHub Pages 在线地址是否可达取决于网络环境；若无法访问，请用本地直接打开 `docs/index.html` 的方式完成实践。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `AERIS_Simulation_Report_v2.pdf` 放在 `docs/` 并入库，而仿真临时输出的 PNG 放在 `5_Simulations/generated/` 并被忽略？
**答案**：前者是**正式发布的报告**，需要对外可见、被 GitHub Pages 发布、被版本化（README 也用它作示例），所以入库；后者是**可重新生成的临时分析**，没有长期价值，所以 gitignore。

**练习 2**：`docs/artifacts/` 下的 `.bit` 是生成产物，为什么却入库了？
**答案**：它是**里程碑式的关键构建产物**（如 `te0713-te0701-heartbeat-2026-03-21.bit` 心跳流文件），需要长期留底用于复现/验证硬件，且配有同名 `.md` 说明文档。这是「产物需留底」的刻意例外，与「随手跑出来的 reports/」性质不同。

**练习 3**：`docs/index.html` 顶部导航有 5 个链接，但 README 的「Documentation」段落也列了 5 个文档，二者一致吗？
**答案**：一致，都指向 architecture / implementation-log / bring-up / reports(Teat Reports) / release-notes 这五个核心文档。README 是「站外入口」，`index.html` 导航是「站内入口」，二者描述的是同一套文档结构。

---

## 5. 综合实践

把本讲三个模块串成一个「仓库导览」小任务。假设你要给一位刚加入团队的新人写一份《5 分钟仓库导览》，请完成以下三件事：

1. **画目录树**：用 `git ls-files | sed 's#/.*##' | sort -u` 或 `ls -d */` 得到顶层目录，画出仓库**前两级**目录树（至少覆盖 `9_Firmware/` 与 `4_Schematics and Boards Layout/` 的二级结构），并为**每个顶层编号目录**写一句话用途说明。

2. **分类三个产物**：分别给出三类「应被 gitignore 而非入库」的生成产物（例如 `*.vcd` 波形、`fir_*.csv` 仿真输出、`__pycache__`），并写出它们的**正确存放路径**和**`.gitignore` 中对应的规则行号**。

3. **指路 docs 站点**：打开 `docs/index.html`，用一句话告诉新人「要看工程状态去哪个页面、要看架构去哪个页面」，并复述首页指标卡片里的至少两条工程事实（如时序基线、回归状态）。

完成后，你应该能在不看任何文档的情况下，回答「这个东西该去哪个目录、该不该入库」——这正是本讲的终极目标。

---

## 6. 本讲小结

- AERIS-10 用**编号目录**（`1_` 到 `9_`）组织顶层，数字小的偏文档/规划，数字大的偏代码/实现；最常改的代码层全部集中在 `9_Firmware/`，内部又按 MCU/FPGA/GUI 分 `9_1/9_2/9_3`。
- 仓库的**文件放置策略**（README §Repository File Placement Policy）把产物分成四类落点：发布报告进 `docs/`（tracked）、仿真输出进 `5_Simulations/generated/`（gitignored）、FPGA 产物进 `9_Firmware/9_2_FPGA/reports/`（gitignored）、可复用脚本进 `9_Firmware/9_2_FPGA/scripts/`（tracked），铁律是「不要把生成产物留在仓库根目录」。
- `.gitignore` 是文件放置策略的**执行层**：`*.vcd`/`*.vvp`、`cic_*.csv` 等模块仿真 CSV、`__pycache__`、`reports/`、`logs/` 都被显式忽略；而同样是 FPGA 目录下的 `scripts/` 却入库——是不是「可重新生成的产物」决定命运。
- `docs/` 是用 **GitHub Pages** 托管的工程文档站点，首页有 5 个核心文档入口（架构/实现日志/bring-up/报告/发布说明）和一组「工程状态指标卡片」，承担「工程看板」角色；正式报告 PDF 与里程碑 `.bit` 在 `docs/` 入库。
- 「tracked / gitignored」的判断可以一句话总结：**人写的、要审查/发布的 → 入库；机器生成的、可重新跑出来的 → 忽略。**

---

## 7. 下一步学习建议

你已经能在仓库里「认路」和「归位」了。下一讲 [u1-l3 关键入口文件：从哪里开始读源码](./u1-l3-entry-points.md) 会带你**进入代码**，指出三大子系统各自的入口文件：

- FPGA 的 `9_Firmware/9_2_FPGA/radar_system_top.v`（顶层）。
- STM32 的 `9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp`。
- Python GUI 的 `9_Firmware/9_3_GUI/GUI_V7_PyQt.py` 与 `v7/dashboard.py`。

建议在进入下一讲前，先做两件准备：

1. 自行 `ls 9_Firmware/9_2_FPGA/ | head`，扫一眼 FPGA 目录下有多少 `.v` 文件、`.mem` 文件，建立「这个设计有多大」的直觉。
2. 读一遍 `CONTRIBUTING.md` 的「Repository layout」表与「Code Standards」段，它把后续要用的工具链（`uv`/`ruff`/`pytest`/`iverilog`/`make`）也提前点了出来——这些会在 [u1-l4 工具链与本地运行方式](./u1-l4-toolchain-and-running.md) 详讲。
