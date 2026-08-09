# 三种 CIC 实现方案对比概览

## 1. 本讲目标

本讲是「方案对比」系列的入口。前面我们已经会读单份利用率报告（u2-l2）、也理解了 CIC 滤波器的积分-梳状结构与 R/N/M 参数（u1-l3）。本讲要回答一个更上层的问题：

> **同一个 CIC 抽取滤波器（同样的 R、N），用三种不同来源的实现去做 FPGA 后实现，结果会有什么不同？**

学完本讲，你应当能够：

1. 仅凭报告头部一行 `Design` 字段，判断这份报告来自三种实现方案中的哪一种，并说出该方案是怎么被「生产」出来的。
2. 解释为什么只有 MATLAB HDL Coder 的 `Design` 名会随 R/N 变化，另外两种却是固定名。
3. 在 100 MHz、R=16、N=4 这一相同配置下，横向对比三种实现的 LUT / 寄存器 / DSP / BRAM / Slice / 控制集，并指出各自资源构成的「特征签名」。
4. 说出三种方案在「实验覆盖的 R、N 取值范围」上的差异（哪些组合存在、哪些缺失），并理解这种差异对后续对比分析的影响。

本讲只做「概览 + 同点对比」，深入的跨方案资源分析留给 u3-l1，时序/fmax 对比留给 u3-l2。

## 2. 前置知识

在进入对比前，先回顾三件事（本讲不再重复证明）：

- **CIC 滤波器只用加法/减法和寄存器，不用乘法器**（u1-l3）。所以无论哪种实现，`DSP` 资源在理想情况下都应为 0。这一点会成为我们检验「报告是否来自一个真正的 CIC」的试金石。
- **利用率报告怎么读**（u2-l2）：报告分节，每节是 `Site Type / Used / Fixed / Prohibited / Available / Util%` 六列表，关键是 `Slice LUTs`、`Slice Registers`、`Block RAM Tile`、`DSPs` 四行，以及最后的 `Primitives` 原语表。本讲直接引用这些字段，不再解释字段含义。
- **报告头部是报告的「身份证」**（u2-l3）：`Tool Version`、`Host`、`Command`、`Design`、`Device` 等字段。本讲重点用到 `Design` 字段来区分三种实现。

一个关键术语先讲清：

- **Design（设计名）**：Vivado 工程里顶层模块（top module）或 IP 实例的名字。它在每份报告头部第 7 行回显，是我们区分三种实现方案最直接的线索。

## 3. 本讲源码地图

本讲「源码」就是三份后实现（impl）利用率报告，它们来自同一个实验条件：100 MHz、R=16、N=4、目标器件 `xc7a100tcsg324-1`，由同一台机器 `DESKTOP-OA8NOG1`、同一版本 Vivado 2022.2 在同一天（2025-07-17）导出。唯一变量是「实现方案」，因此三者可以直接对比。

| 文件 | 方案 | Design 名 | 作用 |
|---|---|---|---|
| `vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt` | Xilinx CIC Compiler IP | `cic_compiler_0` | 厂商 IP 方案的利用率 |
| `vivado_reports/reports_at_100Mhz/MATLAB HDL Coder/utilization_impl_R16_N4.txt` | MATLAB HDL Coder 自动生成 | `CIC_R16_N4` | 代码生成方案的利用率 |
| `vivado_reports/reports_at_100Mhz/Open-source CIC/utilization_impl_R16_N4.txt` | 开源手写 RTL | `cic_d` | 手写 RTL 方案的利用率 |

> 小贴士：三份报告的 `Command` 字段都是 `report_utilization -file C:/Users/Elisei/Desktop/report/utilization_impl_R16_N4.txt -name utilization_1`，文件名完全相同——说明作者是把每个方案的报告都导出成同名文件，再分别归档到三个目录里。这也是为什么我们必须靠**目录**或 **Design 字段**来区分方案，而不能只看文件名。

## 4. 核心概念与源码讲解

### 4.1 CIC Compiler IP（cic_compiler_0）

#### 4.1.1 概念说明

**Xilinx CIC Compiler** 是 Vivado IP Catalog 里自带的参数化 IP 核（LogiCORE），属于「厂商 IP」方案。你不需要写任何 RTL，只需在图形界面里填几个参数（抽取/插值比、级数、差分延迟、位宽等），Vivado 就会生成一个加密/网表形式的黑盒 IP，顶层实例默认命名为 `cic_compiler_0`。

它的特点是「开箱即用、经过 Xilinx 优化」，但你拿不到它的 RTL 源码——这一点在利用率报告里会留下痕迹（见 4.1.3 的 `SRL16E`）。

#### 4.1.2 核心流程

CIC Compiler 方案的产出与识别流程：

1. 在 Vivado 中打开 IP Catalog，实例化 CIC Compiler IP。
2. 在参数界面设置 R、N、M、输入/输出位宽、滤波器类型（抽取/插值）。
3. Generate IP → 在顶层例化 `cic_compiler_0` → 综合 + 实现。
4. 用 `report_utilization` 导出报告。报告头部 `Design` 恒为 `cic_compiler_0`。

判别要点：**Design 名固定为 `cic_compiler_0`，且原语表里出现大量 `SRL16E`（移位寄存器查找表）**——这是厂商 IP 把梳状器差分延迟塞进 LUT 的面积优化手法。

#### 4.1.3 源码精读

报告头部确认 Design 名（与文件名里的 R/N 无关）：

[`vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt:3-11`](<https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L3-L11>)

```text
| Tool Version : Vivado v.2022.2 (win64) ...
| Host         : DESKTOP-OA8NOG1 running 64-bit ...
| Command      : report_utilization -file .../utilization_impl_R16_N4.txt ...
| Design       : cic_compiler_0      <-- 固定名，不随 R/N 变
| Device       : xc7a100tcsg324-1
| Design State : Routed
```

核心资源四项（LUT 155、寄存器 261、BRAM 0、DSP 0）：

[`vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt:35-40`](<https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L35-L40>)

```text
| Slice LUTs                 |  155 |  ...                    ← 最省 LUT
|   LUT as Logic             |  130 |
|   LUT as Memory            |   25 |                        ← 有 25 个 LUT 当存储用
|     LUT as Shift Register  |   25 |                        ← 全部用作移位寄存器
| Slice Registers            |  261 |                        ← 三方案中最少
```

`DSPs = 0`，印证 CIC 无乘法器：

[`vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt:113-117`](<https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L113-L117>)

原语表里的「特征签名」——`SRL16E` 与 `FDRE`：

[`vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt:181-196`](<https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L181-L196>)

```text
| FDRE     |  260 |  Flop & Latch |        ← 同步复位 D 触发器
| LUT3     |   96 |  LUT          |
| LUT2     |   74 |  LUT          |
| SRL16E   |   41 |  Distributed Memory | ← 用 LUT 实现的 16 位移位寄存器
| CARRY4   |   25 |  CarryLogic  |        ← 进位链（实现加减法）
| FDSE     |    1 |  Flop & Latch |
```

**直觉解读**：`SRL16E` 是把一片 SLICEM 类型的 LUT 配置成 16 位深的移位寄存器。CIC 梳状器需要差分延迟（`1 - z^-M` 的延迟链），厂商 IP 选择把这些延迟塞进 `SRL16E`，于是「LUT as Shift Register = 25」非零、寄存器总数反而压到最低（261）。这是 CIC Compiler 的实现取向：**用 LUT 换寄存器**。

> 待确认：原语表里 `SRL16E` 计 41 个，而 `LUT as Shift Register` 物理站点计 25 个，二者并非简单 1:1（涉及 SRL16E 的站点打包/合并规则）。本讲只把它当作「CIC Compiler 用了移位寄存器 LUT」的定性证据，精确映射关系留待读者结合 Vivado 控制集报告进一步核实。

#### 4.1.4 代码实践

1. **实践目标**：凭报告内容确认「这是厂商 IP 方案」。
2. **操作步骤**：打开上面链接的 CIC Compiler 利用率报告。
3. **观察**：
   - 头部 `Design` 是否为 `cic_compiler_0`？
   - 第 1 节 `Slice Logic` 中 `LUT as Memory` 是否非零、且 `LUT as Shift Register` 等于它？
   - 第 8 节 `Primitives` 是否出现 `SRL16E`？`DSPs` 是否为 0？
4. **预期结果**：三项都为「是」，即可判定这是 CIC Compiler IP 方案。
5. 本实践为源码阅读型，待本地用文本编辑器/grep 复核。

#### 4.1.5 小练习与答案

**练习 1**：CIC Compiler 的 `Slice Registers = 261`，但原语表里 `FDRE = 260` 还差 1 个寄存器去哪了？

**参考答案**：原语表里还有一个 `FDSE = 1`（同步置位的 D 触发器），260 + 1 = 261，与 `Slice Registers` 自洽。

**练习 2**：为什么说 `DSP = 0` 是「真正的 CIC」的特征签名？

**参考答案**：CIC 只含积分器（累加）和梳状器（差分），全部是加减法，不含任何乘法，因此综合后不会占用 DSP48 单元。若某份「CIC」报告 DSP 非零，反倒值得怀疑实现是否掺入了乘法。

---

### 4.2 MATLAB HDL Coder（CIC_R{R}_N{N}）

#### 4.2.1 概念说明

**MATLAB HDL Coder** 是「代码生成」方案：你在 MATLAB 或 Simulink 里用定点（fixed-point）算法描述 CIC（比如用 `dsp.CICDecimator` 或手写累加/差分），HDL Coder 自动把它翻译成可综合的 Verilog/VHDL。它的顶层模块名由你（或生成脚本）指定，本实验里被命名为 `CIC_R{R}_N{N}`——**把 R、N 直接写进模块名**。

特点：算法在 MATLAB 层可仿真验证，生成代码风格「机器味」重，倾向于大量使用同一种触发器和同一种 LUT。

#### 4.2.2 核心流程

1. 在 MATLAB/Simulink 搭建定点 CIC 模型，按 R、N 配置参数。
2. 用 HDL Coder Workflow 生成 Verilog，顶层命名为 `CIC_R{R}_N{N}`。
3. 把生成的 `.v` 导入 Vivado → 综合 + 实现。
4. `report_utilization` 导出报告。报告头部 `Design` 随 R/N 变化（如 `CIC_R16_N4`、`CIC_R64_N6`）。

判别要点：**Design 名形如 `CIC_R{R}_N{N}`，且寄存器原语是 `FDCE`（异步清零）**。

#### 4.2.3 源码精读

头部确认 Design 名编码了参数（与文件名一致）：

[`vivado_reports/reports_at_100Mhz/MATLAB HDL Coder/utilization_impl_R16_N4.txt:3-11`](<https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/MATLAB%20HDL%20Coder/utilization_impl_R16_N4.txt#L3-L11>)

```text
| Host         : DESKTOP-OA8NOG1 running 64-bit ...
| Command      : report_utilization -file .../utilization_impl_R16_N4.txt ...
| Design       : CIC_R16_N4         <-- 名字里带 R16、N4
| Device       : xc7a100tcsg324-1
| Design State : Routed
```

核心资源（LUT 169、寄存器 266，注意 `LUT as Memory = 0`，与 CIC Compiler 不同）：

[`vivado_reports/reports_at_100Mhz/MATLAB HDL Coder/utilization_impl_R16_N4.txt:35-39`](<https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/MATLAB%20HDL%20Coder/utilization_impl_R16_N4.txt#L35-L39>)

```text
| Slice LUTs              |  169 |
|   LUT as Logic          |  169 |
|   LUT as Memory         |    0 |     ← 不用移位寄存器 LUT
| Slice Registers         |  266 |
```

原语表「特征签名」——清一色 `FDCE` + `LUT2`：

[`vivado_reports/reports_at_100Mhz/MATLAB HDL Coder/utilization_impl_R16_N4.txt:176-190`](<https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/MATLAB%20HDL%20Coder/utilization_impl_R16_N4.txt#L176-L190>)

```text
| FDCE     |  266 |  Flop & Latch |   ← 带时钟使能、异步清零的 D 触发器
| LUT2     |  186 |  LUT          |   ← 几乎只用 2 输入 LUT
| CARRY4   |   40 |  CarryLogic  |
```

**直觉解读**：HDL Coder 生成的代码把梳状器延迟用**一串真实触发器**实现，所以 `LUT as Memory = 0`、没有 `SRL16E`；同时它统一用 `FDCE`（异步清零），这与 1.1 节「Summary of Registers by Type」里 266 个寄存器落在 `Clock Enable=Yes / Asynchronous=Reset` 一行相互印证。这是「同功能、不同零件」的典型体现。

#### 4.2.4 代码实践

1. **实践目标**：验证 HDL Coder 的 Design 名随参数变化。
2. **操作步骤**：分别打开同目录下 `utilization_impl_R4_N2.txt` 与 `utilization_impl_R64_N6.txt` 的第 7 行。
3. **观察**：两份报告的 `Design` 字段分别是 `CIC_R4_N2` 和 `CIC_R64_N6`。
4. **预期结果**：Design 名随文件名里的 R、N 同步变化（这三种方案中**唯一**这样做的）。
5. 待本地用 `grep -m1 "Design" 文件名` 复核。

#### 4.2.5 小练习与答案

**练习 1**：HDL Coder 报告里 `LUT as Memory = 0`，意味着它用什么实现梳状器的差分延迟？

**参考答案**：用一串真实的 D 触发器（`FDCE`）级联实现延迟链，而不是像 CIC Compiler 那样用 `SRL16E` 移位寄存器 LUT。所以它的寄存器数（266）不比 CIC Compiler 少多少，却完全不用 LUT 当存储。

**练习 2**：若你想写脚本批量解析 HDL Coder 的报告，从哪里读 R、N 最省事？

**参考答案**：直接从报告头部第 7 行的 `Design` 字段正则提取 `CIC_R(\d+)_N(\d+)` 即可，不必依赖文件名。对另外两种方案（固定名）则只能从文件名解析。

---

### 4.3 Open-source RTL（cic_d）

#### 4.3.1 概念说明

**Open-source CIC** 是「手写 RTL」方案：作者用 Verilog（顶层模块名 `cic_d`）从零实现 CIC 抽取器。它最透明——每一行代码都可读、可改——但因为没有厂商 IP 的底层面积优化，通常资源占用偏高。

特点：代码风格人写、原语种类较杂、控制集偏多。

#### 4.3.2 核心流程

1. 用 Verilog 手写 CIC 抽取器，顶层命名 `cic_d`。
2. 加入 Vivado 工程 → 综合 + 实现。
3. `report_utilization` 导出报告。报告头部 `Design` 恒为 `cic_d`（与 R/N 无关，R/N 只体现在文件名里）。

判别要点：**Design 名固定为 `cic_d`，且 `CARRY4` 进位链与控制集数量明显偏多**。

#### 4.3.3 源码精读

头部确认 Design 名固定：

[`vivado_reports/reports_at_100Mhz/Open-source CIC/utilization_impl_R16_N4.txt:3-11`](<https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/utilization_impl_R16_N4.txt#L3-L11>)

```text
| Command      : report_utilization -file .../utilization_impl_R16_N4.txt ...
| Design       : cic_d               <-- 固定名
| Device       : xc7a100tcsg324-1
| Design State : Routed
```

核心资源（LUT 177、寄存器 325，三者中最高）：

[`vivado_reports/reports_at_100Mhz/Open-source CIC/utilization_impl_R16_N4.txt:35-39`](<https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/utilization_impl_R16_N4.txt#L35-L39>)

```text
| Slice LUTs              |  177 |     ← 三方案最高
|   LUT as Logic          |  177 |
|   LUT as Memory         |    0 |
| Slice Registers         |  325 |     ← 三方案最高
```

「特征签名」之一：`CARRY4 = 64`，远高于另两方案（CIC Compiler 25、HDL Coder 40）：

[`vivado_reports/reports_at_100Mhz/Open-source CIC/utilization_impl_R16_N4.txt:176-190`](<https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/utilization_impl_R16_N4.txt#L176-L190>)

```text
| FDRE     |  325 |  Flop & Latch |   ← 同步复位触发器，数量最多
| LUT2     |  164 |  LUT          |
| CARRY4   |   64 |  CarryLogic  |   ← 进位链最多
```

「特征签名」之二：控制集翻倍，导致 Slice 数也最高。控制集（Control Sets）越多，触发器越难被 packing 进同一个 Slice，面积越浪费：

[`vivado_reports/reports_at_100Mhz/Open-source CIC/utilization_impl_R16_N4.txt:72-87`](<https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/utilization_impl_R16_N4.txt#L72-L87>)

```text
| Slice                                      |   89 |  ... |  ← 三方案最多（另两方案 61/67）
...
| Unique Control Sets                        |   13 |       ← 三方案最多（另两方案都是 7）
```

**直觉解读**：开源 RTL 同样用真实触发器（`FDRE`）搭延迟链，且加减法器实现得更「直白」，所以 `CARRY4` 多、寄存器多；又因为控制信号（时钟使能/复位）组合更多，控制集翻倍，最终占用 89 个 Slice，几乎是 CIC Compiler（61）的 1.5 倍。这是「可读性换面积」的代价。

#### 4.3.4 代码实践

1. **实践目标**：量化开源方案的「面积代价」。
2. **操作步骤**：对比三方案 R16_N4 报告的第 2 节 `Slice Logic Distribution`，记录 `Slice` 与 `Unique Control Sets` 两行。
3. **观察**：开源方案 Slice=89、Control Sets=13，明显高于 CIC Compiler（61/7）与 HDL Coder（67/7）。
4. **预期结果**：控制集约为另两方案的 2 倍，Slice 数也相应最高。
5. 待本地复核。

#### 4.3.5 小练习与答案

**练习 1**：开源方案的 `CARRY4 = 64` 远高于 CIC Compiler 的 25，可能原因是什么？

**参考答案**：`CARRY4` 是 7 系列 FPGA 里实现多位加减法器/计数器的进位链。开源 RTL 的累加器/差分器实现更直接、位宽处理更分散，所以占用更多进位链；厂商 IP 则可能复用、合并运算逻辑，进位链更省。

**练习 2**：`cic_d` 这个 Design 名不随 R/N 变化，那么脚本如何知道某份报告对应哪个 R/N？

**参考答案**：只能从文件名（如 `utilization_impl_R16_N4.txt`）解析 R、N，因为报告头部和 Design 字段都不携带这些信息。这一点与 HDL Coder 形成鲜明对比。

---

### 4.4 R/N 参数范围差异

#### 4.4.1 概念说明

理想情况下，三种实现应在完全相同的 R、N 网格上各跑一遍，才能公平对比。但实际情况是：**三种方案覆盖的参数空间并不一致**。这是本实验数据集最重要的「形状特征」之一，直接影响后续（u3 系列）哪些对比做得成、哪些做不成。

回忆参数含义（u1-l3）：R 为抽取速率比，N 为积分-梳状的级数；本数据集 R ∈ {4, 8, 16, 32, 64}，N ∈ {2, 3, 4, 5, 6}。

#### 4.4.2 核心流程

判断覆盖范围的方法很简单——数每个目录下 `utilization_impl_*.txt` 的文件名：

1. 列出某方案目录下所有 `utilization_impl_R*_N*.txt`。
2. 用正则 `R(\d+)_N(\d+)` 提取 R、N。
3. 在「R × N」网格上标记有/无数据，得到覆盖矩阵。
4. 统计每个方案的 R 取值集合、N 取值集合、组合总数。

#### 4.4.3 源码精读（基于目录枚举）

对 100 MHz 下三个目录的 `utilization_impl_*.txt` 做枚举，得到下表（已逐文件核对）：

| 方案 | Design 名 | R 取值 | N 取值 | impl 组合数 | 缺失说明 |
|---|---|---|---|---|---|
| CIC Compiler | `cic_compiler_0`（固定） | {8,16,32,64} | {3,4,5,6} | 11 | **完全没有 R4，也完全没有 N2**；且矩阵稀疏（如 R8 仅 N6） |
| MATLAB HDL Coder | `CIC_R{R}_N{N}`（随参数） | {4,8,16,32,64} | {2,3,4,5,6} | 25 | **完整 5×5 矩阵，无缺失** |
| Open-source CIC | `cic_d`（固定） | {4,8,16,32,64} | {2,3,4,5,6} | 24 | 仅缺 **R4_N2** 一个组合 |

把覆盖情况画成网格（✓=有数据，·=无数据）：

```text
          N2  N3  N4  N5  N6
R4   CC   ·   ·   ·   ·   ·      <- CIC Compiler 整行无 R4
     HC   ✓   ✓   ✓   ✓   ✓
     OS   ·   ✓   ✓   ✓   ✓      <- Open-source 缺 R4_N2
R8   CC   ·   ·   ·   ·   ✓
     HC   ✓   ✓   ✓   ✓   ✓
     OS   ✓   ✓   ✓   ✓   ✓
R16  CC   ·   ·   ✓   ✓   ✓
     HC   ✓   ✓   ✓   ✓   ✓
     OS   ✓   ✓   ✓   ✓   ✓
R32  CC   ·   ·   ✓   ✓   ✓
     HC   ✓   ✓   ✓   ✓   ✓
     OS   ✓   ✓   ✓   ✓   ✓
R64  CC   ·   ✓   ✓   ✓   ✓
     HC   ✓   ✓   ✓   ✓   ✓
     OS   ✓   ✓   ✓   ✓   ✓
（CC=CIC Compiler, HC=HDL Coder, OS=Open-source；均指 100MHz impl）
```

**直觉解读**：

- CIC Compiler 的参数覆盖最「残」：既无 R4，也无 N2，且 N2 这一列整列为空。这意味着任何「N=2」或「R=4」的横向对比，都拿不到 CIC Compiler 的数据。
- HDL Coder 是唯一覆盖完整 5×5 网格的方案，最适合做参数扫描基线。
- Open-source 几乎完整，只差 R4_N2 一个点。

> 待确认：CIC Compiler 为何缺 R4 与 N2，仅凭报告文件无法判定——可能是 Xilinx CIC Compiler IP 在该参数下不支持/作者未配置，也可能是实验设计主动排除。本讲只陈述「数据中不存在」这一事实，根因留待结合 IP 文档或论文确认。

#### 4.4.4 代码实践

1. **实践目标**：亲手枚举并核验三方案的覆盖矩阵。
2. **操作步骤**：在仓库根目录执行（示例命令，需本地运行）：

   ```bash
   # 统计每个方案 utilization_impl 报告数量
   ls "vivado_reports/reports_at_100Mhz/CIC Compiler"      | grep -c "utilization_impl_"
   ls "vivado_reports/reports_at_100Mhz/MATLAB HDL Coder"  | grep -c "utilization_impl_"
   ls "vivado_reports/reports_at_100Mhz/Open-source CIC"   | grep -c "utilization_impl_"
   ```
3. **观察**：应分别得到 11、25、24。
4. **预期结果**：与上表组合数一致；进一步用 `grep -c "_N2"` 在 CIC Compiler 目录应得 0（确认无 N2）。
5. 待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：如果研究者想做「N=2 时三方案资源对比」，会遇到什么问题？

**参考答案**：CIC Compiler 完全没有 N2 的数据，因此 N=2 这一列无法做三方案横向对比，只能对比 HDL Coder 与 Open-source 两方案。

**练习 2**：哪种方案最适合作为「参数扫描」的基线？为什么？

**参考答案**：MATLAB HDL Coder。它是唯一覆盖完整 R×N 网格（25 个组合）的方案，且 Design 名直接编码参数，便于脚本批量解析与绘图。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这张「同点三方案对比表」。这是本讲的核心产出，也是 u3-l1 跨方案资源分析的起点。

**任务**：固定实验条件 100 MHz、R=16、N=4，从三份 `utilization_impl_R16_N4.txt` 中提取关键指标，填入下表，并回答两个问题。

| 指标 | CIC Compiler | MATLAB HDL Coder | Open-source CIC |
|---|---|---|---|
| Design 名 | ? | ? | ? |
| Design 名是否随 R/N 变 | ? | ? | ? |
| Slice LUTs | ? | ? | ? |
| Slice Registers | ? | ? | ? |
| Block RAM | ? | ? | ? |
| DSP | ? | ? | ? |
| Slice 数 | ? | ? | ? |
| Unique Control Sets | ? | ? | ? |
| 主力触发器原语 | ? | ? | ? |
| 是否使用 SRL16E | ? | ? | ? |

**参考答案（基于本讲引用的真实报告）**：

| 指标 | CIC Compiler | MATLAB HDL Coder | Open-source CIC |
|---|---|---|---|
| Design 名 | `cic_compiler_0` | `CIC_R16_N4` | `cic_d` |
| Design 名是否随 R/N 变 | 否（固定） | **是**（唯一变化） | 否（固定） |
| Slice LUTs | 155（最低） | 169 | 177（最高） |
| Slice Registers | 261（最低） | 266 | 325（最高） |
| Block RAM | 0 | 0 | 0 |
| DSP | 0 | 0 | 0 |
| Slice 数 | 61（最低） | 67 | 89（最高） |
| Unique Control Sets | 7 | 7 | 13（最高） |
| 主力触发器原语 | FDRE（同步复位） | FDCE（异步清零） | FDRE（同步复位） |
| 是否使用 SRL16E | 是（41 个） | 否 | 否 |

**回答两个问题**：

1. **哪个方案 Design 名随参数变化、哪个固定？**
   只有 **MATLAB HDL Coder** 的 Design 名（`CIC_R{R}_N{N}`）随 R/N 变化；CIC Compiler（`cic_compiler_0`）和 Open-source（`cic_d`）都是固定名，R/N 只存在于文件名中。

2. **在 R16_N4 这一相同配置下，谁的面积最省、谁最费？为什么？**
   **CIC Compiler 最省**（LUT 155、寄存器 261、Slice 61），因为它用 `SRL16E` 把梳状器延迟塞进 LUT，用 LUT 换寄存器，且控制集最少。**Open-source 最费**（LUT 177、寄存器 325、Slice 89），因为它用真实触发器搭延迟、`CARRY4` 进位链用得多、控制集翻倍（13），packing 效率最差。HDL Coder 居中但接近 CIC Compiler。三方案 DSP 与 BRAM 均为 0，共同印证 CIC 无乘法器、无需块存储的特性。

> 进阶（可选）：把这张表扩展到 N=3、5、6（固定 R=16、100MHz），观察「最省/最费」的排序是否随 N 改变。相关数据可在三个目录下用 `utilization_impl_R16_N*.txt` 找到。这一步直接通向 u3-l1。

## 6. 本讲小结

- 三种实现方案各有一个可凭报告头部 `Design` 字段识别的「身份」：CIC Compiler = `cic_compiler_0`（固定）、MATLAB HDL Coder = `CIC_R{R}_N{N}`（随参数变化）、Open-source = `cic_d`（固定）。
- **只有 HDL Coder 的 Design 名编码 R/N**；另外两种必须从文件名解析参数。写批量解析脚本时要分别处理。
- 在 100 MHz、R16_N4 这一相同点上：CIC Compiler 面积最省（LUT 155 / 寄存器 261 / Slice 61），Open-source 最费（LUT 177 / 寄存器 325 / Slice 89），HDL Coder 居中。
- 三方案的「特征签名」各异：CIC Compiler 用 `SRL16E` 移位寄存器 LUT；HDL Coder 用清一色 `FDCE`（异步清零）+ `LUT2`；Open-source 用 `FDRE` + 大量 `CARRY4`、控制集翻倍。这是「同功能、不同零件」的生动例子。
- 三方案 DSP 与 BRAM 均为 0，是 CIC「只用加减法、不用乘法器、无需块存储」特性的跨实现一致证据。
- 参数覆盖不一致：HDL Coder 完整覆盖 5×5；Open-source 仅缺 R4_N2；CIC Compiler 完全没有 R4 和 N2，矩阵稀疏。这决定了后续哪些对比可做。

## 7. 下一步学习建议

- **u3-l1 资源利用率横向对比分析**：把本讲的「同点（R16_N4）对比」推广到整个 N 扫描或 R 扫描，画资源曲线，定量分析三方案的资源增长差异。本讲的对比表就是它的起点。
- **u3-l2 时序收敛与 fmax 分析**：本讲只比了资源，没比时序。下一步用各方案的 `timing_impl` 报告比 WNS、估 fmax，看「面积最省」是否也「速度最快」。
- **u2-l5 实验矩阵——频率 × R × N**：本讲的覆盖矩阵只看了 100 MHz 和实现维度；下一步把频率维度（100/290/300 MHz）加进来，得到完整的「方案 × 频率 × R × N」四维覆盖图，识别数据缺口（如 290 MHz 只有 CIC Compiler）。

> 建议阅读顺序：先做本讲的综合实践填表，再进 u3-l1 把表格扩展成曲线；资源维吃透了，再去 u3-l2 加上时序维度。
