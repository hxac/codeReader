# 报告元信息与文件格式（.txt 与 .rpx）

## 1. 本讲目标

在前两讲（[u2-l1](u2-l1-read-timing-report.md) 读时序报告、[u2-l2](u2-l2-read-utilization-report.md) 读利用率报告）里，我们已经学会了**读懂报告的内容**——WNS、LUT、原语表等等。本讲换一个视角，退后一步看「**报告本身**」：

- 这些报告是**哪条命令**生成的？
- 报告开头的元信息（Tool Version / Date / Host / Command / Design / Device / Speed File）各自代表什么？
- 为什么有的报告是 `.txt`、有的还多一个 `.rpx`？两者有什么本质区别？

学完后你应当能够：

1. 说出 `report_timing_summary` 与 `report_utilization` 两条命令的作用，并解释 `-file` 与 `-rpx` 两个参数分别产出什么文件。
2. 只看报告头部就能判断：这份报告用什么工具、什么时间、在哪台机器、对哪个设计、哪颗器件生成的。
3. 区分 `.txt`（纯文本、可 grep/可脚本解析）与 `.rpx`（Vivado 二进制交互格式），并解释为什么本仓库里**只有时序报告带 `.rpx`、利用率报告没有**。
4. 识别两个常见陷阱：器件名在两类报告里**写法不同**、`.rpx` **不能当文本直接解析**。

## 2. 前置知识

- **综合（synthesis）与实现（implementation）**：见 [u1-l4](u1-l4-fpga-eval-basics.md)。本讲引用的报告文件名里仍带 `synth` / `impl` 后缀。
- **时序报告与利用率报告的结构**：见 u2-l1、u2-l2。本讲不重复讲内容指标（WNS、LUT 等），只讲报告的「外壳」与「格式」。
- **Tcl 命令**：Vivado 的所有操作都能用 Tcl 命令表达。报告也是由 Tcl 命令「打印」出来的，所以报告头部会原样记录那条命令。
- **文本文件 vs 二进制文件**：文本文件每个字节都是可打印字符（能用记事本直接看）；二进制文件含不可打印字节（记事本打开会看到乱码，必须用专门程序解析）。本讲的 `.txt` 属于前者，`.rpx` 属于后者。

## 3. 本讲源码地图

本仓库的「源码」就是 Vivado 报告文件本身。本讲聚焦三类：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt` | 时序报告（文本版） | 头部元信息 + 生成命令中的 `-rpx` 参数 |
| `vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.rpx` | 时序报告（交互版） | 二进制格式、与 `.txt` 的差异 |
| `vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt` | 利用率报告（仅文本版） | 头部元信息、**无** `-rpx` 参数 |

> 说明：本仓库不含任何 HDL 源码、Tcl 脚本或 Vivado 工程文件。所谓「命令」全部来自报告头部自动回显的 `Command` 字段，并非仓库里另存了一份脚本。

---

## 4. 核心概念与源码精读

### 4.1 report_timing_summary / report_utilization 命令

#### 4.1.1 概念说明

Vivado 不会「凭空」产生报告。每一份报告都对应**一条 Tcl 命令**，运行在设计已经综合（或实现）之后：

- `report_timing_summary` —— 生成**时序总结报告**，回答「设计是否满足时钟约束、最差路径有多险」。
- `report_utilization` —— 生成**利用率报告**，回答「设计用了多少 LUT / 寄存器 / DSP / BRAM」。

这两条命令都支持一系列开关参数。其中最关键、也是本讲核心的两个参数是：

- `-file <路径>`：把报告以**纯文本 `.txt`** 写到指定路径。
- `-rpx <路径>`：**额外**把报告以 Vivado 交互格式 `.rpx` 写到指定路径。

也就是说，`.txt` 和 `.rpx` 是**同一条命令、同一次运行**的两种输出。是否生成 `.rpx`，完全取决于命令里**有没有写 `-rpx` 参数**。

#### 4.1.2 核心流程

一次报告生成的全过程可以画成：

```text
 设计网表（已综合/已实现）
        │
        ▼
 Vivado 时序引擎 / 利用率引擎
        │
        ▼
 report_timing_summary  或  report_utilization   ← Tcl 命令
        │
        ├── -file  xxx.txt   → 纯文本报告（一定有）
        │
        └── -rpx   xxx.rpx   → 交互报告（仅当命令里写了 -rpx）
```

要点：

1. `.txt` 是「保底」输出，几乎所有报告命令都会带 `-file`。
2. `.rpx` 是「可选」输出，**只在命令显式写了 `-rpx` 时才生成**。
3. 命令本身会被 Vivado **原样回显到报告头部**的 `Command` 字段里——这正是我们能反推「这份报告怎么来的」的依据。

#### 4.1.3 源码精读

打开时序报告，看头部的 `Command` 字段（第 6 行）：

[vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:L6](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L6)

```text
| Command : report_timing_summary -delay_type min_max -report_unconstrained
|           -check_timing_verbose -max_paths 10 -input_pins -routable_nets
|           -name timing_1
|           -file C:/Users/Elisei/Desktop/report/timing_impl_R16_N4.txt
|           -rpx  C:/Users/Elisei/Desktop/report/timing_impl_R16_N4.rpx
```

这条命令里同时出现了 `-file ... .txt` 和 `-rpx ... .rpx`，所以这个配置**同时产出了 `.txt` 和 `.rpx` 两个文件**——和仓库里实际存在的两个文件一一对应。

再看利用率报告的 `Command` 字段（同样是第 6 行）：

[vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt:L6](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L6)

```text
| Command : report_utilization -file C:/Users/Elisei/Desktop/report/utilization_impl_R16_N4.txt -name utilization_1
```

对比即可发现：利用率命令**只有 `-file ... .txt`，根本没有 `-rpx`**。这从源头解释了本讲的核心结论——

> 本仓库里所有利用率报告**都只有 `.txt`**，因为生成它们的命令从未带 `-rpx`；而时序报告的命令一律带了 `-rpx`，所以时序报告是 `.txt` + `.rpx` 成对出现。

这一点可以用仓库实际文件数核对：`timing_*` 报告每份都成对（`timing_impl_R16_N4.txt` 与 `timing_impl_R16_N4.rpx` 并存），而 `utilization_*` 报告只有 `.txt`（见 4.4 的实践验证）。

此外，时序命令还带了一组**报告内容开关**，理解它们有助于你判断报告的「完整度」：

| 参数 | 含义 |
| --- | --- |
| `-delay_type min_max` | 同时做建立时间（max）和保持时间（min）分析 |
| `-report_unconstrained` | 一并把未约束的路径也列出来 |
| `-check_timing_verbose` | 详细输出 `check_timing` 检查项（如 `no_clock`、`no_input_delay`） |
| `-max_paths 10` | 每个约束组最多展开 10 条最差路径 |
| `-input_pins -routable_nets` | 路径明细里附带输入引脚与可布线网络信息 |

利用率命令则简短得多，因为它本身可调内容少。命令长度的差异，也部分解释了为什么时序 `.txt` 比利用率 `.txt` 大得多（见 4.3）。

#### 4.1.4 代码实践

**实践目标**：亲手从报告头部反推「这份报告是哪条命令、带哪些参数生成的」。

**操作步骤**：

1. 打开 `vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt`，找到第 6 行 `Command`。
2. 把这条命令里的所有「以 `-` 开头的参数」列成一个清单。
3. 打开同目录 `utilization_impl_R16_N4.txt` 的第 6 行，做同样的事。
4. 在两份清单里各自找一找：有没有 `-rpx`？

**需要观察的现象**：时序命令清单里能看到 `-rpx`，利用率命令清单里看不到 `-rpx`。

**预期结果**：你能用一句话回答——「时序报告同时输出 `.txt` 和 `.rpx`，是因为它的命令带了 `-rpx` 参数；利用率命令没带，所以只有 `.txt`。」

#### 4.1.5 小练习与答案

**练习 1**：如果我想让某份利用率报告也同时生成 `.rpx`，应该在命令里加什么？

> **答案**：在 `report_utilization` 命令里追加 `-rpx <路径>.rpx`。`-rpx` 是可选参数，不写就不生成。

**练习 2**：时序命令里的 `-max_paths 10` 如果改成 `-max_paths 1`，报告会变多还是变少？

> **答案**：会变少（路径明细变短）。`-max_paths` 控制每个约束组展开多少条最差路径，数字越小，报告里列出的路径越少、文件也越小。

---

### 4.2 报告头部元信息

#### 4.2.1 概念说明

每份报告最开头都有一块**元信息（metadata）头**，像是这份报告的「身份证」。它不是设计数据，而是「这份报告从哪儿来」的溯源信息。无论时序还是利用率报告，头部的字段几乎一样：

| 字段 | 含义 |
| --- | --- |
| `Tool Version` | 生成报告的 Vivado 版本（含构建号） |
| `Date` | 报告生成的时刻 |
| `Host` | 生成报告的机器名与系统位数 |
| `Command` | 实际执行的 Tcl 命令（见 4.1） |
| `Design` | 被评估的设计名（顶层模块/IP 实例名） |
| `Device` | 目标 FPGA 器件型号 |
| `Speed File` | 器件速度等级（及工艺角信息） |
| `Design State` | 设计所处的阶段（**仅利用率报告有**，如 `Routed`） |

读懂这块头，可以回答三个工程上很重要的问题：**可复现性**（同一版本、同一器件才能复现相同数字）、**批次一致性**（同一批报告是否来自同一台机器、同一次运行）、**设计归属**（这份报告属于哪个实现方案）。

#### 4.2.2 核心流程

元信息头的读取顺序建议：

1. 先看 `Tool Version` + `Device` + `Speed File` —— 锁定**工具链与硬件平台**（复现前提）。
2. 再看 `Design` —— 锁定**这份报告属于哪个实现方案**（在本仓库里，`cic_compiler_0` = CIC Compiler 方案；`CIC_R…_N…` = MATLAB HDL Coder 方案；`cic_d` = 开源方案，详见 u2-l4）。
3. 看 `Date` + `Host` —— 判断**这批报告是否同源**（同一次批量生成）。
4. 看 `Command` —— 确认**报告是怎么被调用的**（4.1）。
5. 利用率报告额外看 `Design State` —— 确认是 `Routed`（实现后）还是 `Synthesized`（综合后）。

#### 4.2.3 源码精读

时序报告的完整头部（第 1–10 行）：

[vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:L1-L10](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L1-L10)

```text
Copyright 1986-2022 Xilinx, Inc. All Rights Reserved.
---（分隔线）---
| Tool Version : Vivado v.2022.2 (win64) Build 3671981 Fri Oct 14 05:00:03 MDT 2022
| Date         : Thu Jul 17 10:14:51 2025
| Host         : DESKTOP-OA8NOG1 running 64-bit major release  (build 9200)
| Command      : report_timing_summary ... -rpx .../timing_impl_R16_N4.rpx
| Design       : cic_compiler_0
| Device       : 7a100t-csg324
| Speed File   : -1  PRODUCTION 1.23 2018-06-13
---（分隔线）---
```

利用率报告的完整头部（第 1–11 行）：

[vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt:L1-L11](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L1-L11)

```text
Copyright 1986-2022 Xilinx, Inc. All Rights Reserved.
---（分隔线）---
| Tool Version : Vivado v.2022.2 (win64) Build 3671981 Fri Oct 14 05:00:03 MDT 2022
| Date         : Thu Jul 17 10:14:51 2025
| Host         : DESKTOP-OA8NOG1 running 64-bit major release  (build 9200)
| Command      : report_utilization -file .../utilization_impl_R16_N4.txt -name utilization_1
| Design       : cic_compiler_0
| Device       : xc7a100tcsg324-1
| Speed File   : -1
| Design State : Routed
---（分隔线）---
```

逐字段对照可以发现几个**关键事实**：

1. **Tool Version / Date / Host 完全一致**：`Vivado v.2022.2`、`Thu Jul 17 10:14:51 2025`、`DESKTOP-OA8NOG1`。这说明两份报告是**同一台 Windows 机器、同一次会话**里生成的（`build 9200` 是 Windows NT 内核版本号，对应 Windows 8 / Server 2012 系列；输出路径 `C:/Users/Elisei/Desktop/report/` 也证实是 Windows 环境、用户名为 Elisei）。这保证了本仓库报告在「批次」上是同源的。

2. **Design 都是 `cic_compiler_0`**：这是 Xilinx CIC Compiler IP 的实例名，说明这两份报告评估的是同一个 CIC Compiler 设计（R=16、N=4）。

3. **⚠️ Device 字段写法不同（重要陷阱）**：
   - 时序报告：`Device : 7a100t-csg324`
   - 利用率报告：`Device : xc7a100tcsg324-1`

   两者指的是**同一颗芯片**（Artix-7、型号 100T、C SG324 封装、速度等级 -1），只是 Vivado 的**时序引擎和利用率引擎格式化器件字符串的方式不同**。后果是：如果你写脚本从时序报告头部 `grep xc7a100t`，会**一无所获**——必须 `grep 7a100t` 或同时匹配两种写法。

4. **⚠️ Speed File 写法也不同**：
   - 时序报告：`-1  PRODUCTION 1.23 2018-06-13`（带「正式量产」标记和时序库版本日期）
   - 利用率报告：`-1`（只写速度等级）

   时序引擎需要更完整的时序库信息，所以展开得更详细；利用率引擎只关心速度等级档位。

5. **Design State 仅利用率报告有**：时序报告头部**没有** `Design State` 字段。要判断时序报告是综合后还是实现后的，只能靠**文件名里的 `synth` / `impl`**（见 u1-l4、u2-l1）；而利用率报告可以直接看 `Design State: Routed` 确认是实现后的。

把上面整理成一张「陷阱速查表」：

| 字段 | 时序报告 | 利用率报告 | 是否一致 |
| --- | --- | --- | --- |
| Device | `7a100t-csg324` | `xc7a100tcsg324-1` | ❌ 写法不同（同一芯片） |
| Speed File | `-1 PRODUCTION 1.23 …` | `-1` | ❌ 详略不同 |
| Design State | （无此字段） | `Routed` | ❌ 时序报告缺失 |
| Tool Version / Date / Host | 一致 | 一致 | ✅ 同源 |

#### 4.2.4 代码实践

**实践目标**：用真实头部数据，亲手建立「时序 vs 利用率」元信息对照表。

**操作步骤**：

1. 打开 `timing_impl_R16_N4.txt` 第 1–10 行，把 7 个字段（Tool Version / Date / Host / Command / Design / Device / Speed File）的值抄下来。
2. 打开 `utilization_impl_R16_N4.txt` 第 1–11 行，把 8 个字段抄下来。
3. 逐行比对，标出**不一致**的字段。

**需要观察的现象**：`Device`、`Speed File` 两栏写法不一样；利用率表多出一行 `Design State`。

**预期结果**：得到一张类似上面「陷阱速查表」的对照表，并能口头解释「同一个设计、同一颗芯片，为什么两份报告写法不同」（答：两个引擎各自格式化）。

#### 4.2.5 小练习与答案

**练习 1**：某同事写了个脚本 `grep "xc7a100t" *.txt` 来统计「哪些报告用的是 100T 器件」，结果时序报告全部漏掉了。为什么？

> **答案**：时序报告头部的器件写作 `7a100t-csg324`（不带 `xc` 前缀、用连字符分段），所以 `grep xc7a100t` 匹配不到。应改用 `grep -E "7a100t|xc7a100t"`，或直接从利用率报告取器件名。

**练习 2**：只看报告头部（不看文件名），如何确认一份利用率报告是「实现后（impl）」而非「综合后（synth）」？

> **答案**：看 `Design State` 字段。`Routed` = 已布线 = 实现后；`Synthesized` = 仅综合后。时序报告没有这个字段，所以这条判据**只对利用率报告有效**。

---

### 4.3 .txt 文本报告

#### 4.3.1 概念说明

`.txt` 是 Vivado 报告的**纯文本格式**：每个字节都是可打印 ASCII 字符，用换行分节，用 `|` 和 `+` 画固定宽度表格。它的优点非常突出：

- **人眼可读**：任何文本编辑器、浏览器都能直接看。
- **可被脚本处理**：`grep` / `awk` / `sed` / Python 都能解析。
- **可做差异对比**：两份报告能用 `diff` 直接比较，适合版本管理。

这也是为什么本仓库**所有报告都至少有一份 `.txt`**——它是「保底」的人机两用格式。

#### 4.3.2 核心流程

`.txt` 报告的结构很规整：

```text
┌─ 元信息头（Copyright + Tool Version/Date/Host/Command/Design/Device/Speed File）
├─ 报告正文标题（如 "Timing Summary Report" / "Utilization Design Information"）
├─ 目录（Table of Contents）
└─ 各章节正文：每节是一张或多张「固定宽度表格」
        +------+------+ ... +
        | 列1  | 列2  | ... |
        +------+------+ ... +
```

表格的列分隔符是 `|`，行分隔符是 `+---+`。这让你可以稳定地用「按 `|` 切分」的方式提取某一列（u2-l2 里我们正是这样手算 `Util%` 的）。

#### 4.3.3 源码精读

利用率报告里一张典型的固定宽度表格（第 32–45 行）：

[vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt:L32-L45](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L32-L45)

```text
+--------------+------+-------+------------+-----------+-------+
|   Site Type  | Used | Fixed | Prohibited | Available | Util% |
+--------------+------+-------+------------+-----------+-------+
| Slice LUTs   |  155 |     0 |          0 |     63400 |  0.24 |
| Slice Registers | 261|     0 |          0 |    126800 |  0.21 |
+--------------+------+-------+------------+-----------+-------+
```

这就是 `.txt` 的典型长相：`| Slice LUTs | 155 | … |`，按 `|` 切分就能拿到第 2 列「Site Type」、第 3 列「Used」。

从**体量**也能看出 `.txt` 的特点。对本设计（CIC Compiler R16_N4 @100MHz）：

| 文件 | 大小 | 行数 |
| --- | --- | --- |
| `timing_impl_R16_N4.txt` | 约 268 KB | 2767 行 |
| `utilization_impl_R16_N4.txt` | 约 10.5 KB | 214 行 |

时序报告之所以大两个数量级，是因为它要把 `max_paths 10` 条路径的逐级延迟明细、每个端点的源/目的时钟延迟全展开（u2-l1）；利用率报告只是若干张资源统计表，所以短得多。这个体量差异是**格式本身的特征**，与设计无关。

#### 4.3.4 代码实践

**实践目标**：用命令行工具直接从 `.txt` 里批量提取信息，体会它「可脚本解析」的优势。

**操作步骤**：

1. 在仓库根目录，统计某份报告里某个关键词出现了几次，例如在 `timing_impl_R16_N4.txt` 里数「Slack」出现的次数（每条路径明细都会写一次 Slack）。
2. 用 `grep -n "Design" utilization_impl_R16_N4.txt` 定位 `Design` 字段所在行号。
3. 用 `wc -l` 对比时序与利用率两份 `.txt` 的行数差距。

**需要观察的现象**：Slack 出现的次数与 `max_paths` 设置相关；两份报告行数差一个数量级。

**预期结果**：你确认 `.txt` 可以不依赖任何 Vivado 工具、只用基础命令行就被读取和统计——这正是它适合放进 Git 仓库、适合批量分析的原因。

> 注：以上命令的结果取决于本机环境与报告内容，具体数值「待本地验证」；重点是体会「`.txt` 可被任意文本工具处理」这一点。

#### 4.3.5 小练习与答案

**练习 1**：为什么把 `.txt` 报告放进 Git 仓库是合理的，而把 `.rpx` 放进 Git 仓库意义不大？

> **答案**：`.txt` 是文本，`diff` 能逐行显示变化、版本对比有意义；`.rpx` 是二进制，`diff` 只会显示「binary files differ」，无法看内部变化，还容易因工具版本不同而整体变动。所以 `.txt` 适合版本管理，`.rpx` 主要用于一次性交互查看。

**练习 2**：`.txt` 表格里某一行想取出「Used」那一列的数字，用什么思路？

> **答案**：按 `|` 把该行切分成字段，取第 3 段（站点类型是第 2 段、Used 是第 3 段），再 `trim` 掉空格。`awk -F'|' '{print $3}'` 即可。

---

### 4.4 .rpx 交互式报告

#### 4.4.1 概念说明

`.rpx` 是 Vivado 的 **Report eXchange（交互式报告）格式**。它和 `.txt` 描述的是**同一份报告的同一批数据**，但存储方式完全不同：

- `.txt`：线性文本，从头到尾顺序铺开。
- `.rpx`：**二进制序列化**的报告数据模型，保存了报告的层级结构，专门设计成在 **Vivado 图形界面（GUI）** 里重新打开。

在 GUI 里打开 `.rpx` 后，你会得到一个**可交互**的报告：节点可折叠展开、表格列可排序、路径可点开钻取下级明细——这些交互能力是纯文本 `.txt` 给不了的。

代价是：`.rpx` 是**二进制**，不能像 `.txt` 那样 `grep`、`cat`、`diff`，也无法用普通文本工具可靠解析。

#### 4.4.2 核心流程

`.rpx` 的生命周期：

```text
 report_timing_summary ... -rpx xxx.rpx
        │
        ▼
  把「报告数据模型」序列化成二进制写入 .rpx
        │
        ▼
  在 Vivado GUI: File → Open Report → 选 .rpx
        │
        ▼
  得到可折叠/可排序/可钻取的交互式报告
```

什么时候你**需要** `.rpx`、什么时候**不需要**？

| 场景 | 推荐格式 |
| --- | --- |
| 人工阅读某个 WNS / 资源数字 | `.txt` 足够 |
| 写脚本批量提取指标做表格（见 u3-l5） | `.txt`（必须） |
| 想在 GUI 里点开某条最差路径逐级钻取延迟 | `.rpx` |
| 用 `diff` 对比两次综合结果 | `.txt`（必须） |

#### 4.4.3 源码精读

先确认 `.rpx` 不是文本。用 `file` 命令查看它的类型，结果是 `data`（二进制），而非 `ASCII text`。用十六进制查看它的开头几个字节：

```text
000000  2c 00 00 00 08 01 1a 06  32 30 31 34 2e 33  22 0d   |,.......2014.3".|
000010  54 69 6d 69 6e 67 53 75 6d 6d 61 72 79  ...          |TimingSummary...|
```

可见开头是一串**不可打印的控制字节**（`2c 00 00 00 08 01 1a 06`），随后才嵌着可读片段 `2014.3`（报告格式版本号）、`TimingSummary`（报告类型标识）。这种「二进制头 + 夹杂可读字符串」正是**序列化数据模型**的典型形态：可读片段是字段名/枚举值，控制字节是长度前缀和结构标记。它**不是**给人看的文本——用记事本打开会看到大量乱码。

> 说明：Vivado 并未公开 `.rpx` 内部序列化规范的完整文档，上述结构（版本号 `2014.3`、`TimingSummary` 类型标识）是基于实际字节观察得出的结论；精确的字节级格式定义「待确认」。可确认的是：它是二进制、专供 Vivado GUI 重新打开，**不应**作为文本解析的数据源。

尽管如此，`.rpx` 里嵌的可读片段仍能让我们确认：它和同名 `.txt` 描述的是同一个设计。比如 `.rpx` 里也能看到 `Design cic_compiler_0`、`Part Device=7a100t Package=csg324 Speed=-1`、`Version Vivado v2022.2 …`、以及那条 `report_timing_summary …` 命令。注意 `.rpx` 里把器件写成了**第三种**形式 `Device=7a100t Package=csg324 Speed=-1`（键值对形式），与时序 `.txt` 的 `7a100t-csg324`、利用率 `.txt` 的 `xc7a100tcsg324-1` 又不一样——这是 4.2 「器件写法不统一」陷阱的又一次印证。

关于体量，`.rpx` 和 `.txt` 大小相近（时序 `.rpx` 约 224 KB，时序 `.txt` 约 268 KB），因为它们承载的是同一份数据，只是编码方式不同。

最后，回到本讲的核心结论——**为什么利用率报告没有 `.rpx`？** 在 4.1 我们已经从命令层面找到原因：利用率命令没带 `-rpx`。这里再用仓库文件清单做一次实证：在整个 `vivado_reports/` 下，所有 `.rpx` 文件的文件名都以 `timing_` 开头（`timing_impl_*` 与 `timing_synth_*`），**没有任何一份** `utilization_*.rpx`。命令缺参数 ↔ 仓库无文件，两侧互相印证。

#### 4.4.4 代码实践

**实践目标**：亲手确认 `.rpx` 是二进制、且本仓库里只有时序报告才有 `.rpx`。

**操作步骤**：

1. **看二进制特征**：在仓库根目录对 `vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.rpx` 执行：
   - `file ".../timing_impl_R16_N4.rpx"`（预期输出 `data`，而非 `ASCII text`）。
   - `od -A x -t x1z ".../timing_impl_R16_N4.rpx" | head -3`（预期看到 `2c 00 00 00 ... 2014.3 ... TimingSummary` 这样的「二进制头 + 夹杂可读串」）。
   - 对比 `file ".../timing_impl_R16_N4.txt"`（预期输出 `ASCII text`）。
2. **看是否成对**：列出 `vivado_reports/reports_at_100Mhz/CIC Compiler/` 目录，确认每个 `timing_*.txt` 都有一个同名的 `timing_*.rpx`，但 `utilization_*.txt` **旁边没有任何 `.rpx`**。
3. **全仓库核对**：在整个 `vivado_reports/` 下搜索 `*.rpx`，确认结果**全部**是 `timing_*` 文件，没有一个 `utilization_*`。

**需要观察的现象**：

- `.rpx` 的 `file` 结果是 `data`（二进制），`.txt` 是 `ASCII text`。
- `.rpx` 的十六进制开头有大量 `00` 控制字节，夹杂 `2014.3`、`TimingSummary` 等可读片段。
- `utilization_*` 系列确实没有 `.rpx` 配套。

**预期结果**：你能用一句话总结——「`.rpx` 是 Vivado 的二进制交互报告，只有时序报告带它（因为时序命令带了 `-rpx`），利用率报告没有；`.rpx` 不能当文本解析，要批量取数只能用 `.txt`。」

> 注：`file` / `od` 的具体输出格式取决于本机工具版本，结论（`data` vs `ASCII text`、二进制头特征）「待本地验证」，但二进制 vs 文本的定性差别是确定的。

#### 4.4.5 小练习与答案

**练习 1**：你想写脚本统计所有报告的 WNS，应该解析 `.txt` 还是 `.rpx`？为什么？

> **答案**：解析 `.txt`。`.rpx` 是二进制，没有稳定的文本结构可 `grep`；而且利用率报告根本没 `.rpx`。`.txt` 是唯一全部报告都有、且可文本解析的格式。

**练习 2**：如果某份时序 `.rpx` 文件丢失了，但 `.txt` 还在，会丢失信息吗？

> **答案**：不会丢失数据本身——`.txt` 和 `.rpx` 是同一次命令、同一份数据的两种编码，内容等价。丢失的只是「在 Vivado GUI 里交互式钻取路径」的便利。需要批量分析时，`.txt` 反而更合适。

**练习 3**：在 `.rpx`、时序 `.txt`、利用率 `.txt` 三者里，「器件名」一共有几种写法？分别是什么？

> **答案**：三种。`.rpx` 里是 `Device=7a100t Package=csg324 Speed=-1`；时序 `.txt` 是 `7a100t-csg324`；利用率 `.txt` 是 `xc7a100tcsg324-1`。三者指的是同一颗 Artix-7 100T 芯片。

---

## 5. 综合实践

**任务**：给一份「陌生」的报告做一次完整的「身份鉴定」。

请选一份本讲没详细分析过的报告，例如：

`vivado_reports/reports_at_100Mhz/MATLAB HDL Coder/timing_impl_R16_N4.txt`

（以及它旁边同名的 `.rpx`）。

完成下列步骤，把结论填进一张表：

1. **元信息抽取**：从 `.txt` 头部抄出 Tool Version / Date / Host / Command / Design / Device / Speed File 六个字段。
2. **命令解读**：在 `Command` 里找到 `-file` 和 `-rpx`，确认它是否同时产出了 `.txt` 与 `.rpx`。
3. **方案归属**：根据 `Design` 字段判断这份报告属于三种实现方案中的哪一种（提示：MATLAB HDL Coder 方案的 Design 名形如 `CIC_R16_N4`，会随参数变化；对照 CIC Compiler 的 `cic_compiler_0` 是固定的——这为 u2-l4 的方案对比埋下伏笔）。
4. **同源核验**：把它头部的 Date / Host 和本讲 CIC Compiler 的报告（`Thu Jul 17 10:14:51 2025`、`DESKTOP-OA8NOG1`）对比，判断是否同一批生成。
5. **格式判断**：到目录里确认该 `timing_*.txt` 旁边是否真有同名 `.rpx`；再确认 MATLAB HDL Coder 目录下有没有 `utilization_*.rpx`（应当没有）。
6. **器件陷阱**：观察 MATLAB HDL Coder 的时序报告 `Device` 字段写法，是否仍是 `7a100t-csg324`（与时序引擎一致、与方案无关）。

**预期成果**：一张「报告身份卡」，包含「工具 / 时间 / 机器 / 命令 / 设计 / 器件 / 速度等级 / 是否带 .rpx / 同源判定」九项。完成后，你应当能在拿到仓库里任意一份报告时，10 秒内说出它的来历与格式归属。

---

## 6. 本讲小结

- 每份报告都对应一条 Tcl 命令：**时序**用 `report_timing_summary`，**利用率**用 `report_utilization`；命令被原样回显在头部 `Command` 字段里。
- `-file` 产出 `.txt`（一定有），`-rpx` 产出 `.rpx`（**仅当命令写了 `-rpx` 才有**）。时序命令一律带 `-rpx`，利用率命令从不带——这是「时序有 `.rpx`、利用率没有」的根本原因。
- 报告头部元信息（Tool Version / Date / Host / Command / Design / Device / Speed File）是报告的「身份证」，可用于判断**工具链、设计归属、批次同源、可复现性**。
- 三个常见陷阱：①器件名在两类报告里**写法不同**（`7a100t-csg324` vs `xc7a100tcsg324-1`，`.rpx` 里还有第三种）；②`Speed File` 详略不同；③`Design State` **只有利用率报告有**，时序报告判阶段只能靠文件名。
- `.txt` 是纯文本，**可 grep / 可 diff / 可脚本解析**，是批量分析（u3-l5）和版本管理的唯一可靠数据源。
- `.rpx` 是 Vivado **二进制**交互报告，专供 GUI 折叠/排序/钻取，**不能当文本解析**；它与同名 `.txt` 数据等价，丢失 `.rpx` 不丢数据。

## 7. 下一步学习建议

- 接下来进入 **[u2-l4 三种 CIC 实现方案对比概览](u2-l4-three-implementations.md)**：本讲我们注意到不同方案的 `Design` 名不同（`cic_compiler_0` / `CIC_R…_N…` / `cic_d`），下一讲将系统对比三种实现方案的来源、命名与参数范围。
- 之后看 **[u2-l5 实验矩阵——频率 × R × N](u2-l5-experiment-matrix.md)**：用本讲学到的「同源判定」能力，去梳理 100/290/300MHz 三个频率点 × 三种方案 × 多组 R/N 的覆盖矩阵与数据缺口。
- 进阶阶段（u3-l5）会专门讲**如何用脚本从大量 `.txt` 批量提取指标汇总成表**——届时你会真正体会到本讲强调的「`.txt` 可解析、`.rpx` 不可解析」的工程意义。
