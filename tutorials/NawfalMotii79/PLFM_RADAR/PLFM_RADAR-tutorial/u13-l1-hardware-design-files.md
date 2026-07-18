# 原理图、PCB 叠层与生产文件

## 1. 本讲目标

本讲是「硬件设计文件导览」单元的唯一一讲。前面所有讲义都在读代码（FPGA Verilog、STM32 C/C++、Python GUI），而本讲把镜头转向**硬件本身**——那些描述「电路怎么连、板子怎么叠、工厂怎么造」的文件。

学完本讲，你应该能够：

1. 在仓库里**定位每一块功能板的原理图（`.sch`）与 PCB（`.brd`）文件**，并说出它们的格式（CadSoft Eagle 7.4.0 XML）。
2. 理解**多层板叠层（stack-up）**与**阻抗控制**对 10.5 GHz 射频雷达的意义，说清楚为什么主板用 Rogers+FR-4 混合叠层、而电源板只用两层 FR-4。
3. 认得 **Gerber / BOM / CPL** 这三类「可直接发给板厂」的生产文件，能解释每个 Gerber 文件扩展名（`.L1`、`.drl`、`.smt`、`.slk` …）代表什么。
4. 看懂 `Power Management V6.xlsx` 在电源轨**上电时序**中扮演的角色。

> 本讲面对的是「文件资产」而非「可执行代码」，所以「源码精读」会变成「文件结构精读」，代码实践也以**阅读与对照**为主。

## 2. 前置知识

在进入文件之前，先用三段话补齐硬件基础。这些概念在本手册前置讲义里已建立，这里只做最小回顾：

- **原理图 vs PCB**：原理图（schematic）描述**逻辑**——哪些器件的哪些引脚连在一起（net/网络）；PCB 描述**物理**——器件摆在板子哪里、走线（trace）在哪些层、过孔（via）怎么打。同一个器件在原理图里叫一个 part（含器件型号 deviceset），在 PCB 里叫一个 element（含封装 footprint）。
- **叠层（stack-up）**：一块多层 PCB 由铜层（走信号/铺电源地）和介质层（绝缘材料）交替压合而成。介质材料的**介电常数 εr** 与**损耗角 tan δ** 决定高频信号能不能「干净」地传过去。
- **阻抗控制**：射频走线必须呈现特定的特征阻抗（雷达里几乎全是 50 Ω），否则信号会反射。特征阻抗 \(Z_0\) 由线宽 \(W\)、介质厚度 \(h\)、介电常数 \(\varepsilon_r\) 共同决定；所以要**精确控制**这些量。

> 承接 [u2-l1 整体架构](./u2-l1-system-architecture.md)：系统由电源板、频率合成板、主板、功放板（仅 Extended 版）、天线阵列五块功能板组成。本讲逐块对应到它们的硬件文件。

## 3. 本讲源码地图

本讲涉及的文件都在仓库的硬件目录下，集中在两个顶层编号目录：

| 目录 | 作用 |
|---|---|
| `3_Power Management/` | 电源管理：`Power Management V6.xlsx` 描述电源轨与上电时序 |
| `4_Schematics and Boards Layout/` | 原理图、PCB、叠层与生产文件的汇总目录 |

`4_Schematics and Boards Layout/` 内部又分三块（编号越小越偏设计输入，越大越偏制造输出）：

| 子目录 | 作用 |
|---|---|
| `4_4_Board Stack-up/` | 叠层图 `Stack_Hybrid.png`（混合叠层截面图） |
| `4_6_Schematics/` | 每块板的 `.sch`（原理图）+ `.brd`（PCB） |
| `4_7_Production Files/` | 每块板的 Gerber/BOM/CPL + 一份阻抗说明 PDF |

`4_6_Schematics/` 下按板子再分子目录：`MainBoard/`、`PowerBoard/`、`FrequencySynthesizerBoard/`、`PowerAmplifierBoard/`、`Antennas/`（含 `Patch/` 与 `Waveguide/`）。

`4_7_Production Files/` 下按板子分 5 个 `Gerber_*` 子目录，外加 `PCBWay_Impedance_Note_RO4350B_h0p102mm.pdf`。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**各板原理图 → 板叠层 → 生产文件**。

### 4.1 各板原理图与 PCB 文件

#### 4.1.1 概念说明

原理图（`.sch`）回答「**用什么器件、怎么连**」，PCB（`.brd`）回答「**物理上怎么摆、怎么走线**」。AERIS-10 把整台雷达拆成多块功能板，每块板有自己的一对 `.sch/.brd` 文件：

| 板 | 原理图文件 | PCB 文件 | 角色 |
|---|---|---|---|
| 主板 | `RADAR_Main_Board.sch` | `RADAR_Main_Board.brd` | 系统心脏：DAC/混频器/相移器/前端/FPGA/STM32/USB/ADC |
| 电源板 | `PowerBoard.sch` | `PowerBoard.brd` | 产生各路电压并按序上电 |
| 频率合成板 | `Clocks_Freq_Synth_board.sch` | `Clocks_Freq_Synth_board.brd` | AD9523 时钟分发 + ADF4382 本振 |
| 功放板 | `RF_PA.sch` | `RF_PA.brd` | 仅 Extended 版，QPA2962 GaN 10 W |
| 贴片天线 | `Patch_Anetnna_16_8.sch`（8×16）<br>`Phased_Array_Ant.sch`（4×4） | 同名 `.brd` | Nexus 版 8×16 阵列 |
| 缝隙波导天线 | `DFSWA.dwg` / spec `.docx` | —— | Extended 版，机械加工件（非 PCB） |

> 注意：`Antennas/Patch/8x16/` 下有大量 `.b#1`、`.s#2` 这类文件——它们是 Eagle 的**历史备份**（每存一次生成一个），不是独立设计，读最新无后缀的 `.sch/.brd` 即可。缝隙波导天线是机加工铝/介质件，所以用 `.dwg`（AutoCAD 图纸）和 `.docx`（规格书）描述，而不是 PCB 流程。

#### 4.1.2 核心流程

`.sch` 与 `.brd` 在 Eagle 里是**同一个项目的两张视图**，靠器件的 designator（位号，如 `U42`、`ADAR1_`）一一对应：

1. 在 `.sch` 里放置 part（含器件型号 deviceset 与库 library），用 net（网络）把引脚连起来 → 得到逻辑连接关系。
2. Eagle 把所有 part 同步到 `.brd`，每件 part 选定封装 footprint，得到一堆待摆放的元件。
3. 在 `.brd` 里**布局**（摆位置）、**布线**（沿铜层画走线）、**铺铜**（电源/地平面），完成后导出 Gerber（见 4.3）。

读硬件源码的标准入口是：**先看 `.sch` 理解逻辑，再到 `.brd` 看物理实现**。下面以主板为例，直接读 `.sch` 的 XML。

#### 4.1.3 源码精读

主板的 `.sch` 虽然接近 2.8 MB，但它是**纯文本 XML**，GitHub 能直接渲染、可以用任何编辑器打开。文件头就告诉我们它用的是 CadSoft **Eagle 7.4.0**：

[4_6_Schematics/MainBoard/RADAR_Main_Board.sch:L1-L6](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_6_Schematics/MainBoard/RADAR_Main_Board.sch#L1-L6) —— 文件头声明 `eagle.dtd` 与版本 `7.4.0`，并定义图层表（`<layers>`），是判断文件格式的第一现场。

所有器件集中在一个 `<parts>` 列表里，每行一个 `<part>`，给出位号 `name`、来源库 `library`、器件型号 `deviceset`。下面是主板上的几个关键器件（行号均为本 HEAD 实测）：

| 位号 | 型号（deviceset） | 功能 | 行号 |
|---|---|---|---|
| `U42` | `XC7A50T-2FTG256I` | Xilinx Artix-7 FPGA | [L28309](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_6_Schematics/MainBoard/RADAR_Main_Board.sch#L28309) |
| `U2` | `STM32F746ZGT7` | STM32 微控制器 | [L27639](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_6_Schematics/MainBoard/RADAR_Main_Board.sch#L27639) |
| `U1` | `AD9484BCPZ-500` | 400 MSPS ADC | [L28419](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_6_Schematics/MainBoard/RADAR_Main_Board.sch#L28419) |
| `U3` | `AD9708AR` | DAC（生成 chirp） | [L28388](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_6_Schematics/MainBoard/RADAR_Main_Board.sch#L28388) |
| `U5` / `U13` | `LTC5552IUDBTRMPBF` | 2 片微波混频器 | [L27692](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_6_Schematics/MainBoard/RADAR_Main_Board.sch#L27692) / [L27699](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_6_Schematics/MainBoard/RADAR_Main_Board.sch#L27699) |
| `ADAR1_`–`ADAR4_` | `ADAR1000ACCZN` | 4 片 4 通道相移器（共 16 通道波束赋形） | [L27755](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_6_Schematics/MainBoard/RADAR_Main_Board.sch#L27755)–[L27954](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_6_Schematics/MainBoard/RADAR_Main_Board.sch#L27954) |
| `ADTR1107_1`–`ADTR1107_8`+ | `ADTR1107ACCZ` | 16 片 RF 前端（LNA/PA） | [L28023](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_6_Schematics/MainBoard/RADAR_Main_Board.sch#L28023)–[L28142](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_6_Schematics/MainBoard/RADAR_Main_Board.sch#L28142) |

一段典型的 XML 片段长这样（FPGA）：

```xml
<part name="U42" library="XC7A50T-2FTG256I" deviceset="XC7A50T-2FTG256I" device=""/>
```

- `name="U42"`：位号，原理图与 PCB 靠它对齐。
- `library="XC7A50T-2FTG256I"`：来自哪个元件库。
- `deviceset` + `device`：器件型号与具体封装变体。

一个**关键观察**：`AD9523`（时钟发生器）和 `ADF4382`（本振频综）**不在主板的 `.sch` 里**——它们在 `FrequencySynthesizerBoard/Clocks_Freq_Synth_board.sch` 上。这与 [u7-l2 时钟树](./u7-l2-clock-and-frequency-synthesis.md) 讲的「时钟全部由独立的频率合成板产生」完全吻合：**器件的物理位置 = 它所在的那块板**，这是用原理图核对架构归属的最直接方法。

> `README.md` 第 45–89 行给出了每个子系统挂哪些器件的文字版，可与 `.sch` 的 `<parts>` 列表互相对照（README 是「人话版」，`.sch` 是「机器版」，以 `.sch` 为准）。

#### 4.1.4 代码实践：在原理图里找器件

1. **目标**：用文本搜索在主板原理图里定位波束赋形与前端器件，验证「4 片相移器 + 16 片前端」的架构。
2. **操作步骤**：
   - 打开 GitHub 上 [RADAR_Main_Board.sch](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_6_Schematics/MainBoard/RADAR_Main_Board.sch)，按 `T` 键（页面内搜索）。
   - 搜索 `deviceset="ADAR1000ACCZN"`，数一数命中几行（应为 4 行：`ADAR1_`–`ADAR4_`）。
   - 再搜索 `deviceset="ADTR1107ACCZ"`，数命中行数（应不少于 8，实际有 16 片）。
3. **需要观察的现象**：每次命中都是一行 `<part name="..." deviceset="ADAR1000ACCZN"/>`，位号逐一递增。
4. **预期结果**：ADAR1000 = 4 个、ADTR1107 = 16 个，与 README「4×4 通道相移器」「16× 前端芯片」一致。若数字不符，说明你读到的是旧备份 `.s#n` 文件，请改用无后缀的主文件。
5. 想本地做：`grep -c 'deviceset="ADTR1107ACCZ"' RADAR_Main_Board.sch` 应输出 16。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `AD9523` 在主板 `.sch` 里搜不到？它在哪里？
> **答**：`AD9523` 时钟发生器装在**频率合成板**上（`Clocks_Freq_Synth_board.sch`），主板只接收它分发出来的时钟。用 `grep` 在 `FrequencySynthesizerBoard/` 下能找到。

**练习 2**：`.sch` 和 `.brd` 靠什么字段保持一一对应？
> **答**：靠器件的**位号（name，如 `U42`）**。同一个位号在 `.sch` 里是逻辑 part，在 `.brd` 里是物理 element（带坐标与封装）。

---

### 4.2 多层板叠层与阻抗控制

#### 4.2.1 概念说明

**叠层（stack-up）** 是一块 PCB 的「三明治配方」：从顶到底，每一层铜（信号/电源/地）和每一层介质（绝缘材料）的材料、厚度、铜厚都写清楚。对 10.5 GHz 的雷达，叠层不是随便选的——它直接决定：

- **特征阻抗能不能做到 50 Ω**（阻抗控制）；
- **高频信号损耗大不大**（介质损耗角 tan δ 越小越好）；
- **板子能不能造、贵不贵**。

AERIS-10 用的是**混合叠层（Hybrid）**——名字就写在文件名 `Stack_Hybrid.png` 里：表层射频区用低损耗的 **Rogers**（RO4350B / RO4450F），内层数字/电源区用便宜稳定的 **FR-4**。这是射频板的标准省钱招：只有「信号真的经过」的表层才花 Rogers 的钱。

#### 4.2.2 核心流程：为什么是混合叠层

要理解为什么不能整块都用 FR-4，先看一段最朴素的微带线特征阻抗公式（表层走线、下方一层介质、再下方是地平面）：

\[
Z_0 \approx \frac{87}{\sqrt{\varepsilon_r + 1.41}}\; \ln\!\left(\frac{5.98\,h}{0.8\,W + t}\right)
\]

其中 \(W\) 是线宽、\(t\) 是铜厚、\(h\) 是介质厚度、\(\varepsilon_r\) 是介电常数。这条公式告诉我们：

1. **要稳定 50 Ω，就得把 \(h\) 与 \(\varepsilon_r\) 钉死**——这就是「阻抗控制」的本质，也是为什么板厂要拿到一份阻抗说明（见 4.3.1 的 PDF）。
2. **\(\varepsilon_r\) 越大，同样线宽的 \(Z_0\) 越低**，需要更细的线才能回到 50 Ω，细线又不好造。

再看损耗。信号在介质里传播，损耗随频率升高而升高，正比于损耗角 tan δ：

| 材料 | 典型 \(\varepsilon_r\) | 典型 tan δ @10 GHz | 用途 |
|---|---|---|---|
| Rogers **RO4350B** | ≈ 3.66 | ≈ 0.0031（低损耗） | 表层射频走线 |
| Rogers **RO4450F**（prepreg） | ≈ 3.52 | ≈ 0.004 | Rogers 层之间的粘合半固化片 |
| **FR-4**（标准/High-Tg） | ≈ 4.3–4.6 | ≈ 0.02（高 6 倍） | 内层数字/电源，不扛 RF |

> 上表 \(\varepsilon_r\) / tan δ 为各材料的**典型数据手册值**（非本仓库实测），用于理解选材逻辑；精确值以板厂来料与叠层图为准。

10.5 GHz 下，FR-4 的损耗是 Rogers 的好几倍。所以策略很清晰：**「射频表层给 Rogers，数字/电源内层给 FR-4」**——既保住了 RF 性能，又没让整块板都按 Rogers 计价。文件名 `Stack_Hybrid` 正是此意。

#### 4.2.3 源码精读：叠层图与阻抗说明

叠层的「真值图」在 `4_4_Board Stack-up/Stack_Hybrid.png`（`4_6_Schematics/` 下有一份同名副本，尺寸更小，是低分辨率备份）：

[4_4_Board Stack-up/Stack_Hybrid.png](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_4_Board%20Stack-up/Stack_Hybrid.png) —— 混合叠层截面图。从图中可读出每层铜/介质的**材料与厚度**：表层为 Rogers RO4350B（低损耗射频层），靠 RO4450F 半固化片与 Rogers 芯板压合，内层为标准/High-Tg FR-4 用于数字与电源。

更具体的「表层 Rogers 厚度」由生产文件目录里的阻抗说明 PDF 直接给出——**它的文件名就是规格**：

[4_7_Production Files/PCBWay_Impedance_Note_RO4350B_h0p102mm.pdf](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_7_Production%20Files/PCBWay_Impedance_Note_RO4350B_h0p102mm.pdf) —— 文件名解读：`RO4350B`（介质材料）+ `h0p102mm`（即 h = **0.102 mm**，p 是小数点的转写）。这份给板厂 PCBWay 的阻抗说明指明：在 0.102 mm 厚的 RO4350B 上做受控阻抗（50 Ω 微带线）。把它代回 4.2.2 的公式，板厂就能算出对应的线宽 \(W\)。

> **待本地核对**：叠层图里每一层的确切厚度与铜厚，需打开 `Stack_Hybrid.png` 逐层确认；不同板（见下表）层数不同，混合配方也因板而异。本讲只钉死两个稳定事实：① 材料是 Rogers+FR-4 混合；② 表层 Rogers RO4350B 厚度 h = 0.102 mm。

**层数是硬证据**——直接数每块板的 Gerber 铜层文件就能定（4.3.3 会讲 `.L1`/`.top` 的含义）：

| 板 | 铜层文件 | 层数 | 为什么这么多/少 |
|---|---|---|---|
| 主板 | `RADAR_Main_Board.L1`–`.L10` | **10 层** | RF + FPGA(256 BGA) + STM32 + 电源/地平面，最复杂 |
| 频率合成板 | `Clocks_Freq_Synth_board.L1`–`.L6` | **6 层** | RF 时钟 + 频综 + 少量控制 |
| 功放板 | `RF_PA.L1`–`.L4` | **4 层** | GaN 功放，RF + 散热 + 偏置 |
| 贴片天线 | `Patch_Anetnna_16_8.L1`–`.L4` | **4 层** | 馈电网络 + 辐射贴片 |
| 电源板 | `PowerBoard.top` / `.bot` | **2 层** | 只走直流，无 RF → 两层 FR-4 足够 |

这张表是本讲最重要的结论之一：**层数跟着「信号复杂度 + 是否有 RF」走**。电源板扛的是直流大电流，没有 10 GHz 信号，所以两层就够——这是成本与性能取舍的活教材。

#### 4.2.4 代码实践：对照电源板与主板

1. **目标**：体会「层数随功能而变」，并验证电源板是最简单的板。
2. **操作步骤**：
   - 打开 [4_7_Production Files/Gerber_PowerBoard/](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_7_Production%20Files/Gerber_PowerBoard/) 目录，数里面的 `.L*` 文件——会发现**一个都没有**，只有 `PowerBoard.top` 与 `PowerBoard.bot`，即顶层与底层两个铜层。
   - 再打开 [Gerber_Main_Board/](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_7_Production%20Files/Gerber_Main_Board/)，数 `.L1` 到 `.L10`，确认是 10 层。
3. **需要观察的现象**：电源板连一个 `.L*` 内层文件都没有；主板有 10 个。
4. **预期结果**：电源板 = 2 层、主板 = 10 层。由此推断：电源板不需要阻抗控制的 RF 走线（直流无特征阻抗要求），而主板的 10.5 GHz 射频与密集 BGA 必须靠多层 + 阻抗控制。
5. **进阶**：用 4.2.2 的公式估算——若要在 RO4350B（\(\varepsilon_r\approx3.66\)、\(h=0.102\) mm）上做 50 Ω 微带，线宽 \(W\) 大概是几十 μm 量级；精确值需场求解器或板厂的阻抗说明。

#### 4.2.5 小练习与答案

**练习 1**：为什么电源板敢用两层 FR-4，而主板必须用 10 层混合叠层？
> **答**：电源板只走直流，不关心高频损耗与特征阻抗，两层铜足以承载电流与基本连线，便宜可靠；主板有 10.5 GHz 射频 + 多片 BGA（FPGA/STM32），必须靠多层提供阻抗受控的 RF 走线、密集的信号换层与完整的电源/地平面，且表层必须用低损耗 Rogers。

**练习 2**：阻抗说明 PDF 文件名 `RO4350B_h0p102mm` 里的 `h0p102mm` 是什么意思？
> **答**：`h` = 介质厚度，`p` = 小数点，即 **h = 0.102 mm**。它指定 Rogers RO4350B 芯板的厚度，板厂据此计算 50 Ω 走线的线宽。

---

### 4.3 生产文件：Gerber / BOM / CPL

#### 4.3.1 概念说明

`.sch` 和 `.brd` 是**设计文件**（需要 Eagle 才能打开），而工厂（板厂、贴片厂）要的是**通用制造文件**。`4_7_Production Files/` 就是把每块板的 `.brd`「翻译」成工厂能直接吃的一组文件：

- **Gerber（光绘）**：每一层铜/阻焊/丝印的二维图形，是造裸板的图纸。
- **Drill（钻孔）**：过孔与通孔的位置、孔径（Excellon 格式）。
- **BOM（物料清单，Bill of Materials）**：板上有多少种器件、每种多少个、型号与位号。
- **CPL（贴片坐标，Centroid / Pick-and-Place）**：每个表贴器件的 X/Y 坐标、旋转角、贴在顶层还是底层——贴片机靠它摆元件。

#### 4.3.2 核心流程：从 .brd 到可制造文件

一块板要变成可交付，要导出三类文件：

1. **Gerber + Drill**：一层一个文件（顶层铜、各内层铜、底层铜、阻焊、丝印、钢网…），外加钻孔。打包发给**板厂**做出裸板。
2. **BOM**：告诉采购/SMT 厂「要买什么、贴什么」。
3. **CPL**：告诉贴片机「器件贴在哪、朝哪」。

> 这三类文件互不替代：板厂要 Gerber+Drill，贴片厂要 BOM+CPL。少任一类，板子就做不出来或贴不上。

#### 4.3.3 源码精读：Gerber 头与文件扩展名

Gerber 是 **RS-274X** 格式的纯 ASCII 文本（`file` 命令报 `ASCII text`），所以同样可以在 GitHub / 编辑器里直接读。以主板顶层铜为例，文件头就是它的「自报家门」：

[4_7_Production Files/Gerber_Main_Board/RADAR_Main_Board.L1:L1-L6](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_7_Production%20Files/Gerber_Main_Board/RADAR_Main_Board.L1#L1-L6) —— Gerber 文件头：

```
G75*            ← 圆弧插补模式
%MOIN*%         ← 单位：英寸（MO=单位，IN=inch；若为 MOMM 则是毫米）
%FSLAX25Y25*%   ← 数值格式：前导零省略(L)、绝对坐标(A)、X/Y 各 2位整数+5位小数
%IPPOS*%        ← 插补极性：正向
%LPD*%          ← 层极性：Dark（绘线，D=draw）
```

随后的 `%ADD10R,...*%` 是**光圈定义（aperture）**——把 `D10`、`D11` … 编号绑到具体的焊盘/线条尺寸（如矩形 0.00984×0.01378 英寸）。正文的 `D01`(绘线)/`D02`(移动)/`D03`(闪焊盘) 配合 X/Y 坐标画出整层图形。

每块板都有一个 `Gerber_<板名>/` 目录，结构一致。文件扩展名约定如下（这是读生产文件的关键词汇表）：

| 扩展名 | 含义 |
|---|---|
| `.L1`、`.L2` … `.Ln` | 第 1…n **铜层**（L1=顶层铜，最后一个是底层铜，中间是内层铜/电源地平面） |
| `.top` / `.bot` | 顶层铜 / 底层铜（电源板用这套命名，等价于 `.L1`/末层） |
| `.drl` / `.drd` / `.dri` | **钻孔**文件（Excellon）及其报告/信息 |
| `.smt` / `.smb` | 顶层 / 底层**阻焊（solder mask）**（绿油开窗） |
| `.tps` / `.bps` | 顶层 / 底层**钢网（paste，SMD 锡膏）**——做钢网用 |
| `.slk` | **丝印（silkscreen）**（白字器件位号） |
| `.oln` | **板外形（outline）**（Route，板子轮廓） |
| `.mnt` / `.mnb` | 顶层 / 底层**贴片坐标（mount）**——CPL 的 Gerber 形式 |
| `.gpi` | Gerber **导出信息**（aperture/层统计，非图形） |
| `.csv` / `_CSV_BOM` | **BOM 的 CSV 文本版**（机器可读，便于核对） |

每块板还配一对 Excel：

- `BOM_<板名>.xlsx`：物料清单（有些板另存了一份通用 `BOM.xlsx` 与 CSV 版）。
- `CPL.xlsx`：贴片坐标清单。

例如主板：[BOM_Main_Board.xlsx](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_7_Production%20Files/Gerber_Main_Board/BOM_Main_Board.xlsx) 与 [CPL.xlsx](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_7_Production%20Files/Gerber_Main_Board/CPL.xlsx)（与铜层/钻孔/阻焊文件同目录）。

> 板厂的「阻抗下单说明」就放在生产文件根目录（不在任何子目录下）：[PCBWay_Impedance_Note_RO4350B_h0p102mm.pdf](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_7_Production%20Files/PCBWay_Impedance_Note_RO4350B_h0p102mm.pdf)（4.2.3 已解读）。

#### 4.3.4 代码实践：把每个 Gerber 目录对回它的板

1. **目标**：建立「Gerber 目录 ↔ 哪块板 ↔ 多少层」的完整映射。
2. **操作步骤**：依次打开 5 个目录，数每个目录里 `.L*`（或 `.top/.bot`）的个数，并看 `BOM_*.xlsx` 的名字：
   - [Gerber_Main_Board/](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_7_Production%20Files/Gerber_Main_Board/)
   - [Gerber_freq_synth/](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_7_Production%20Files/Gerber_freq_synth/)
   - [Gerber_PA/](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_7_Production%20Files/Gerber_PA/)
   - [Gerber_Patch_Antenna/](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_7_Production%20Files/Gerber_Patch_Antenna/)
   - [Gerber_PowerBoard/](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_7_Production%20Files/Gerber_PowerBoard/)
3. **需要观察的现象**：每个目录里铜层文件名的前缀就是板名（`RADAR_Main_Board` / `Clocks_Freq_Synth_board` / `RF_PA` / `Patch_Anetnna_16_8` / `PowerBoard`）。
4. **预期结果**：建立下表（应与 4.2.3 的层数表一致）：

   | Gerber 目录 | 板 | 铜层数 |
   |---|---|---|
   | Gerber_Main_Board | 主板 | 10 |
   | Gerber_freq_synth | 频率合成板 | 6 |
   | Gerber_PA | 功放板（Extended） | 4 |
   | Gerber_Patch_Antenna | 贴片天线（Nexus） | 4 |
   | Gerber_PowerBoard | 电源板 | 2 |

5. **进阶**：在 `Gerber_PA/` 里同时存在 `BOM_PA.xlsx` 与 `BOM.xlsx`、`Gerber_PowerBoard/` 里同时有 `BOM_Power_Board.xlsx` 与 `BOM.xlsx`——理解这是「完整名 + 短名」两份并存，内容应一致，以长名为准。

#### 4.3.5 小练习与答案

**练习 1**：`.smt` 和 `.tps` 有什么区别？分别给谁用？
> **答**：`.smt` 是**顶层阻焊**（solder mask top，绿油开窗），板厂用来印阻焊；`.tps` 是**顶层钢网**（paste，SMD 锡膏层），贴片厂用来做钢网刮锡膏。一个服务于「造裸板」，一个服务于「贴元件」。

**练习 2**：`.drl` 和 `.mnt` 分别描述什么？
> **答**：`.drl`（Excellon）描述**过孔/通孔的位置与孔径**，板厂钻孔用；`.mnt`（mount）描述**表贴器件的贴片坐标/旋转**（顶层），贴片机摆件用。

---

## 5. 综合实践：给一块板做一次「硬件资产盘点」

把三个最小模块串起来，挑一块板（推荐**主板**，最完整）做端到端盘点。任务是产出一份「主板硬件交付清单」。

**步骤：**

1. **原理图层**：打开 [RADAR_Main_Board.sch](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_6_Schematics/MainBoard/RADAR_Main_Board.sch)，列出 6 个核心器件的位号 + 型号：FPGA、STM32、ADC、DAC、2 片混频器、4 片 ADAR1000（参考 4.1.3 的表与行号）。
2. **叠层与阻抗层**：
   - 数 [Gerber_Main_Board/](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/4_Schematics%20and%20Boards%20Layout/4_7_Production%20Files/Gerber_Main_Board/) 里的 `.L1`–`.L10`，记录铜层数 = 10。
   - 写出表层介质材料（Rogers RO4350B，h = 0.102 mm）与混合叠层的理由（射频低损耗 + 内层 FR-4 省成本）。
3. **生产文件层**：列出主板发给工厂需要的全部文件类别——`RADAR_Main_Board.L1`–`.L10`（铜层）、`.drl`（钻孔）、`.smt`/`.smb`（阻焊）、`.slk`（丝印）、`.tps`/`.bps`（钢网）、`BOM_Main_Board.xlsx`（物料）、`CPL.xlsx`（贴片坐标）。
4. **电源轨层**：打开 [Power Management V6.xlsx](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/3_Power%20Management/Power%20Management%20V6.xlsx)（见下方说明），确认它描述了各路电源轨及其上电先后顺序。

**交付物**：一张三段式表格——

| 维度 | 主板的具体值 |
|---|---|
| 核心器件 | U42 XC7A50T、U2 STM32F746、U1 AD9484、U3 AD9708、U5/U13 LTC5552、ADAR1_–4_ ADAR1000 … |
| 叠层 | 10 层铜，Rogers RO4350B(h=0.102mm) + FR-4 混合，50 Ω 受控阻抗 |
| 生产文件 | Gerber(.L1–.L10/.drl/.smt/.smb/.slk/.tps/.bps) + BOM_Main_Board.xlsx + CPL.xlsx |

### 附：Power Management V6.xlsx 与上电时序

`3_Power Management/Power Management V6.xlsx` 是电源管理的「真值表」。它的角色由 README 明确点出：

[README.md:L45](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/README.md#L45) ——「Power Management Board - Supplies all necessary voltage levels … with proper filtering and **sequencing**（sequencing ensured by the microcontroller）」。

[README.md:L70](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/README.md#L70) —— STM32 负责「Power-up and power-down **sequencing（see Power Management Excel File）**」。

也就是说，这份 Excel 描述两件事：

1. **电源轨清单**：板子上每一路电压（如 FPGA 的 1.0 V/1.8 V/3.3 V、模拟射频轨、PA 偏置等）、由哪个稳压器产生、滤波与电流能力。
2. **上电/下电顺序**：哪些轨必须先上、哪些必须后上（例如 FPGA 的核心电压 1.0 V 要先于 I/O 3.3 V，否则可能闩锁损坏；本振与时钟要先稳定再开 PA），并由 STM32 严格按序执行。

结合 [u7-l1 STM32 main 与外设初始化](./u7-l1-stm32-main-and-peripherals.md) 的时序（OCXO 预热 → AD9523 → FPGA 1.0→1.8→3.3 V → ADAR1000 → PA + Idq 闭环校准 → 复位 FPGA/开混频器），就能把 Excel 里的「轨 + 顺序」对到 STM32 代码的执行步骤上。

> **待本地核对**：Excel 内每一路的具体电压值、稳压器位号、时序数值（毫秒级延迟），需在本机用 Excel/libreoffice 打开 `Power Management V6.xlsx` 逐表查看（本环境无法直接解析 xlsx）。读法提示：先看 sheet 名（常见有「Rails / Sequencing / Current」等），再按「轨名 → 电压 → 上电序号」三列对照。

## 6. 本讲小结

- 硬件设计文件按「设计 → 制造」分两级：`4_6_Schematics/` 放 Eagle 7.4.0 的 `.sch`（逻辑）+ `.brd`（物理），`4_7_Production Files/` 放 Gerber/BOM/CPL 等工厂文件。
- 每块板一对 `.sch/.brd`；器件的**物理归属**=它出现在哪块板的原理图里（如 AD9523 在频率合成板、不在主板）。
- 叠层是「铜 + 介质」三明治配方；AERIS-10 用 **Rogers RO4350B（h=0.102 mm）+ FR-4 混合叠层**，表层低损耗扛 10.5 GHz、内层 FR-4 省成本。
- 层数跟着功能走：**主板 10 层 > 频率合成 6 > 功放/天线 4 > 电源板 2**；电源板只走直流、无 RF，所以两层 FR-4 即可。
- 阻抗控制靠钉死 \(h\) 与 \(\varepsilon_r\)，由 `PCBWay_Impedance_Note_RO4350B_h0p102mm.pdf` 把 50 Ω 走线要求交给板厂。
- Gerber 是 RS-274X 文本：`.L*`/`.top`/`.bot` = 铜层、`.drl` = 钻孔、`.smt`/`.smb` = 阻焊、`.slk` = 丝印、`.tps`/`.bps` = 钢网；外加 `BOM_*.xlsx`（物料）与 `CPL.xlsx`（贴片坐标）。
- `Power Management V6.xlsx` 给出电源轨清单与上电时序，由 STM32 执行（README L45/L70 明确指向该 Excel）。

## 7. 下一步学习建议

- **想看电源时序怎么落地成代码**：读 [u7-l1 STM32 main 与外设初始化](./u7-l1-stm32-main-and-peripherals.md)，把本讲的 Excel 时序对到 `main.cpp` 的上电步骤与看门狗喂狗逻辑。
- **想看这些板子怎么首次上电验证**：读 [u10-l2 硬件 Bring-up 流程与构建产物](./u10-l2-board-bringup.md)，看 `bring-up.html` / `board-day-worksheet.html` 怎么把「电源板→主板→RF→PA」逐级点亮。
- **想深入 RF 与天线设计**：读 [u12-l1 天线电磁仿真](./u12-l1-antenna-simulation.md) 与 [u12-l2 RF 链路与滤波器仿真](./u12-l2-rf-and-filter-simulation.md)，它们仿真的是本讲这些板上的天线与射频器件。
- **想动手制造一块板**：把某块板的 `Gerber_*/` 整个目录打包成 zip，连同阻抗 PDF 一起发给 PCBWay/JLCPCB 这类板厂即可下单；贴片文件（BOM + CPL）单独给 SMT 厂。
