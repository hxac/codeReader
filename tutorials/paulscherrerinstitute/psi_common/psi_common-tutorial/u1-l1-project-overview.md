# 项目概览与定位

## 1. 本讲目标

本讲是整本 `psi_common` 学习手册的第一篇。读完本讲后，你应该能够：

- 用一句话说清楚 `psi_common` 是什么、解决什么问题、适合什么场景。
- 说出库的**维护者**与**作者团队**，并理解它的**许可方式**（PSI HDL License = LGPL + 额外硬件例外）。
- 看懂 `doc/README.md` 里的组件总表，并把库里的组件按 **存储 / FIFO / CDC / 转换 / TDM / 仲裁 / 接口 / 杂项 / 包** 等类别归类。
- 通过 `Changelog.md` 了解库的版本演进，并理解 `major.minor.bugfix` 的标签策略。

本讲**不要求**你懂 VHDL 的语法细节。我们只建立「全局地图」，具体的源码精读会在后续讲义展开。

---

## 2. 前置知识

本讲是面向初学者的概览，但有几个名词最好先有个印象：

- **VHDL**：一种硬件描述语言（Hardware Description Language），用来描述数字电路的逻辑。FPGA/ASIC 工程师用它来「写电路」。
- **FPGA / ASIC**：可编程/专用硬件芯片。`psi_common` 写出来的代码最终会被综合（synthesis）成这些芯片里的真实电路。
- **可复用 IP 库**：把那些「跟具体项目无关、可以反复拿来用」的电路模块（比如 FIFO、RAM、时钟域同步器）集中成一个库，就像软件里的「工具函数库」。
- **Generic（类属参数）**：VHDL 里一种「编译期可配置参数」，比如可以让你在实例化时指定一个 FIFO 的位宽、深度。`psi_common` 几乎所有东西都做成 generic 化，这是它能复用的关键。
- **综合（synthesis）**：把 VHDL 代码翻译成真实门电路/查找表的过程。不是所有 VHDL 写法都能综合，`psi_common` 特别强调代码可综合。

如果你暂时记不住这些词也没关系，本讲会在用到时再点一下。

---

## 3. 本讲源码地图

本讲只读「文档型」文件，不深入任何 `.vhd` 源码。涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 仓库入口文档：维护者、作者、许可、依赖、仿真运行方式、收库规范。 |
| `doc/README.md` | 库的正式文档首页：编码规范速查 + **全部组件总表**（分类列出每个组件及其源码链接）。 |
| `License.txt` | PSI HDL Library License 全文：LGPL + 针对硬件（bitstream 等）的额外例外条款。 |
| `Changelog.md` | 版本变更记录，从 V1.00 一路到当前 3.0.1，是理解库演进的最佳线索。 |

> 额外参考（不在本讲必读范围，但有助于理解贡献流程）：`doc/old/ch1_introduction/ch1_introduction.md`，它是旧版手册第一章，讲目录结构、仿真和贡献规范。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，每个都对应学习目标里的一项。

### 4.1 项目定位与价值

#### 4.1.1 概念说明

`psi_common` 是由瑞士保罗谢尔研究所（Paul Scherrer Institute，简称 PSI）维护的**通用可复用 VHDL 库**。

它要解决的核心问题是：**FPGA/ASIC 开发里，有大量「与具体项目无关」的基础电路模块，每个项目都重写一遍既浪费又容易出 bug**。把这些模块沉淀成一个统一的、可综合的、generic 化的库，所有人都能直接拿来用，就是 `psi_common` 的价值。

关键词是「**通用**」和「**可复用**」。`README.md` 的 "What belongs into this Library" 一节明确界定了这个边界：

- **属于本库的**：时钟域跨越（Clock-Crossings）、FIFO、厂商无关的 RAM 实现、扩展 VHDL 语言能力的 package。
- **不属于本库的**：任何项目特定代码；更适合放进别的库的代码（例如使用 `psi_fix` 的信号处理代码）；任何「不能完全参数化」的代码。

这种「只放通用件、且必须完全参数化」的纪律，是理解整个库风格的钥匙——后续你看任何组件，都会发现它的位宽、深度、行为几乎都能用 generic 调。

#### 4.1.2 核心流程

从「我有一个新需求」到「它该不该进 `psi_common`」，可以画成这样一个判断流：

```text
新电路模块需求
      │
      ▼
 是否项目特定？ ── 是 ──▶ 不入库（放项目里）
      │ 否
      ▼
 是否已有更合适的库？ ── 是 ──▶ 不入库（如信号处理 → psi_fix）
      │ 否
      ▼
 能否完全 generic 化（位宽/深度/行为可配）？ ── 否 ──▶ 不入库
      │ 是
      ▼
 入 psi_common：一个 .vhd 一个 entity/package，配自检 TB
```

#### 4.1.3 源码精读

README 顶部一句话点明了当前大版本（v3）的来意——**统一代码风格、提升可读性**，并提醒它**不向下兼容**，但提供了迁移脚本：

- [README.md:1-4](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L1-L4) — 仓库标题与 v3 版本说明：新版本统一了全部文件的代码风格，**不向下兼容**，但附带迁移脚本。

「什么属于 / 不属于本库」的边界定义在这里：

- [README.md:26-31](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L26-L31) — **属于**本库的内容：Clock-Crossings、FIFO、厂商无关 RAM、扩展语言的 package；并强调「所有重要设置都必须做成 Generic」。
- [README.md:39-43](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L39-L43) — **不属于**本库的内容：项目特定代码、更适合其它库的代码、不能完全参数化的代码。

依赖关系（仿真需要 PsiSim、TB 需要 `psi_tb`）也写在 README 的 Dependencies 段：

- [README.md:54-65](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L54-L65) — 依赖与目录结构：TCL 工具链需要 PsiSim（≥2.1.0），VHDL 仿真依赖 `psi_tb`（≥3.0.0）；并指出可用 `psi_fpga_all` 聚合仓库一次性拉齐所有子模块。

> 说明：这一段在 README 里被特殊注释 `<!-- DO NOT CHANGE FORMAT ... -->` 包裹，因为它会被脚本**自动解析**来解析依赖，所以格式不能随便改。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**（不需要运行任何工具）。

1. **实践目标**：理解「入库存放标准」并判断一个假设模块是否合格。
2. **操作步骤**：
   - 打开 [README.md:26-43](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L26-L43)，把「属于」与「不属于」两节都读一遍。
   - 假设你想贡献一个「某厂某型号 ADC 的专用时序校正模块」，对照规则判断它是否应该进 `psi_common`。
3. **需要观察的现象**：你会发现这个 ADC 专用模块**同时违反了多条规则**（项目特定 + 厂商绑定 + 难以完全 generic 化）。
4. **预期结果**：结论应是「**不入库**」。这帮你建立对「通用 vs 专用」边界的直觉。
5. 待本地验证：无需运行，纯阅读判断。

#### 4.1.5 小练习与答案

**练习 1**：为什么 README 强调「所有重要设置必须实现为 Generics」？如果一个 FIFO 把深度写死成 1024，会怎样？

> **参考答案**：因为库的目标是「可复用」。把深度写死成 1024，意味着需要 512 或 2048 深度的项目就没法用它，只能 fork 改源码，违背了「一处实现、处处复用」的初衷。Generic 让同一个 `.vhd` 在编译期适配不同需求。

**练习 2**：`psi_common` 与 `psi_fix` 是什么关系？

> **参考答案**：它们是同一团队（PSI）维护的**互补**库。`psi_common` 只放通用数字电路积木（FIFO/RAM/CDC/接口等），而 `psi_fix` 专注定点信号处理。README 明确说「使用 psi_fix 的信号处理代码应放进 psi_fix」，两者各司其职。

---

### 4.2 许可与维护者

#### 4.2.1 概念说明

用别人的库之前，必须搞清楚两件事：**谁在维护它**（出了问题找谁、是否能长期信任）和**它用什么许可证**（你能不能商用、能不能闭源使用）。

`psi_common` 的许可证叫 **PSI HDL Library License**。它的本质是 **LGPL（GNU 宽通用公共许可证）+ 一条专门针对硬件的例外条款**。这条例外对 FPGA 工程师极其重要：它允许你把库**编译成 bitstream/烧录文件**后，以**你自己的条款**发布成品，**不必开源你的整个工程**——这正是纯 LGPL 在硬件场景下会让人头疼的地方。

#### 4.2.2 核心流程

许可证的判断可以这样记忆：

```text
PSI HDL License = LGPL 基础规则
        │
        ├── 源码层面：修改/再分发库源码本身 → 仍受 LGPL 约束（要开放改动）
        │
        └── 硬件例外（EXCEPTION NOTICE 第 2 条）：
            └── 以「二进制/硬件」形式（明确包含 FPGA bitstream、flash 镜像）
                使用本库 → 可按你自己条款发布，不强制开源你的工程
```

注意例外条款里专门强调：**「binary」明确包含 FPGA bitstream 和 flash 镜像，但明确排除「能还原出库源码的数据」**——也就是保护的是你的应用工程，而不是让你把库源码藏起来。

#### 4.2.3 源码精读

维护者与作者团队：

- [README.md:6-7](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L6-L7) — **维护者**为 Benoît Stef（ PSI）。
- [README.md:9-15](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L9-L15) — **作者团队**共 6 人（Oliver Bründler、Benoît Stef、Daniele Felici、Patric Bucher、Rafael Basso、Radoslaw Rybaniec）。

许可证声明：

- [README.md:17-18](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L17-L18) — 指明本库采用 PSI HDL Library License，本质是 **LGPL 加上若干针对固件开发的额外例外**。

许可正文（要点）：

- [License.txt:8-13](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/License.txt#L8-L13) — LGPL 条款本体：可自由再分发与修改，依 GNU LGPL 第 2 版或（你可选）更高版本，但不提供任何担保。
- [License.txt:15-19](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/License.txt#L15-L19) — **EXCEPTION NOTICE 第 2 条（关键）**：允许以二进制或硬件形式（**明确包含 FPGA bitstream、flash 镜像**）使用、链接、修改并按自己的条款分发；但「binary」**明确排除能还原库源码的数据**。

#### 4.2.4 代码实践

1. **实践目标**：确认你能否在一个**闭源商用 FPGA 工程**里合法使用 `psi_common`。
2. **操作步骤**：
   - 读 [License.txt:15-19](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/License.txt#L15-L19) 的例外第 2 条。
   - 回答：你的工程把 `psi_common` 综合成 bitstream 后出售，是否必须公开你的应用源码？是否必须公开你对 `psi_common` 自身源码的修改？
3. **需要观察的现象**：注意例外条款把「binary」**显式扩展**到了 bitstream/flash 镜像。
4. **预期结果**：
   - 你**不必**公开你的应用工程源码（例外第 2 条允许）。
   - 但如果你**修改了 `psi_common` 库本身的源码**并再分发该源码，LGPL 的常规义务仍适用（应开放那些改动）。
   - ⚠️ 这是技术讲义对许可的通俗解读，**正式商用前请以 `License.txt` 全文及法务意见为准**。
5. 待本地验证：纯阅读，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：PSI HDL License 相比纯 LGPL，对 FPGA 工程师「多给了」什么？

> **参考答案**：多了一条「硬件例外」——把 bitstream、flash 镜像等显式纳入「binary」范围，允许你在成品里使用本库而不必开源整个工程。纯 LGPL 在「链接 = 编译进 bitstream」这一点上对硬件场景含糊，容易引发争议，这条例外消除了歧义。

**练习 2**：如果你发现 `psi_common` 有 bug 并在库源码里修好了，想把改好的 `.vhd` 分发给客户，需要遵守什么？

> **参考答案**：因为你是在分发**库源码本身**（不是 binary），LGPL 的常规条款仍生效——你应让客户能看到/获取到你对库源码的改动。例外条款只覆盖 binary/硬件形式的使用，不覆盖源码再分发。

---

### 4.3 组件分类总览

#### 4.3.1 概念说明

`psi_common` 不是一个单体组件，而是**几十个独立组件**的集合。实测 `hdl/` 目录下有 **61 个 `.vhd` 源文件**（含实体与包）。`doc/README.md` 用一张张分类表把它们组织起来，是你在整本学习手册里最常回来查的「目录页」。

理解这套分类，就等于拿到了整本手册的索引：后续每一讲，基本都对应表里的某一类组件。

#### 4.3.2 核心流程

`doc/README.md` 把组件分成下面几大类（顺序与文档一致）：

| 类别 | 代表组件 | 一句话用途 |
| --- | --- | --- |
| **Memory** 存储组件 | `sdp_ram`、`sp_ram_be`、`tdp_ram`、`tdp_ram_be` | 厂商无关的双口 RAM，FIFO/乒乓的底层存储 |
| **FIFO** 缓冲 | `sync_fifo`、`async_fifo` | 同步/异步 FIFO，带满空、电平标志 |
| **CDC** 时钟域跨越 | `pulse_cc`、`simple_cc`、`status_cc`、`bit_cc`、`sync_cc_n2xn/xn2n` | 在不同时钟域间安全传脉冲/数据/状态 |
| **Conversions** 转换 | `wconv_n2xn`、`wconv_xn2n` | N 位 ↔ 整数倍 N 位的宽度转换 |
| **TDM** 时分复用 | `tdm_par`、`par_tdm`、`tdm_mux` 等 | 并行 ↔ TDM 串行流转换 |
| **Arbiters** 仲裁 | `arb_priority`、`arb_round_robin` | 多请求者总线仲裁 |
| **Interfaces** 接口 | `spi_master`、`i2c_master`、AXI 系列 | SPI/I2C/AXI 总线主从机 |
| **miscellaneous** 杂项 | `delay`、`pl_stage`、`watchdog`、`debouncer`、`prbs`、`pwm` 等 | 延迟、流水线、看门狗、信号源等实用件 |
| **Packages** 包 | `math_pkg`、`array_pkg`、`logic_pkg`、`axi_pkg` | 扩展 VHDL 语言的工具函数与类型 |

#### 4.3.3 源码精读

组件总表都集中在 `doc/README.md` 的 "List of components available" 一节：

- [doc/README.md:36-47](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/README.md#L36-L47) — 组件总表开头 + **Memory** 类（简单双口 RAM、带字节使能单口/真双口 RAM）。
- [doc/README.md:50-55](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/README.md#L50-L55) — **FIFO** 类（异步 FIFO、同步 FIFO）。
- [doc/README.md:58-71](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/README.md#L58-L71) — **CDC** 类（脉冲/数据/状态/位跨越 + 同步整数比跨越）。
- [doc/README.md:74-79](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/README.md#L74-L79) — **Conversions** 宽度转换类。
- [doc/README.md:82-91](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/README.md#L82-L91) — **TDM** 时分复用类（含可配置通道与多路复用）。
- [doc/README.md:93-98](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/README.md#L93-L98) — **Arbiters** 仲裁类。
- [doc/README.md:101-113](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/README.md#L101-L113) — **Interfaces** 接口类（SPI/I2C/AXI 主从机）。
- [doc/README.md:115-138](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/README.md#L115-L138) — **miscellaneous** 杂项（延迟、流水线、乒乓、看门狗、消抖、PRBS、PWM、采样率转换等）。
- [doc/README.md:140-148](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/README.md#L140-L148) — **Packages**（math/array/logic/axi 四个基础包）。

文档开头还给出了进库的「语法速查规则」（命名规范），本讲先建立印象，细节留到 `u1-l4`：

- [doc/README.md:8-16](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/README.md#L8-L16) — 进库速查：`snake_case`、去掉 Tab、信号加 `_i/_o/_io` 后缀、同接口用统一前缀（如 `adc_*`）、架构命名 `behav/struc/rtl`。

> 小提示：表格里个别条目有笔误（例如杂项里 `trigger_analog` 的源码链接写成 `psi_trigger_analog.vhd`），实际文件是 `hdl/psi_common_trigger_analog.vhd`。阅读时以 `hdl/` 目录实际文件名为准。

#### 4.3.4 代码实践

这就是本讲规格里要求的**主实践任务**。

1. **实践目标**：在组件总表里定位你自己最可能用到的组件，建立「我将来会用到什么」的私人索引。
2. **操作步骤**：
   - 打开 [doc/README.md:36-148](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/README.md#L36-L148)。
   - 假设你正在做一个 FPGA 项目：需要跨时钟域传一个脉冲、缓存一段数据流、读一片 SPI 传感器。
   - 从表里挑出 **5 个**你最可能用到的组件。
3. **需要观察的现象**：注意每个组件在表里都同时给了「Source（源码链接）」和「Description（说明文档链接）」两列。
4. **预期结果**：写出 5 行，每行格式如 `组件名 —— 一句话用途`。例如（这只是示例格式，你的选择会不同）：
   - `psi_common_pulse_cc` —— 把一个时钟域的单周期脉冲安全传到另一个时钟域
   - `psi_common_sync_fifo` —— 单时钟域内缓存数据流，带满空标志
   - `psi_common_spi_master` —— 作为主机读取 SPI 传感器
   - `psi_common_pl_stage` —— 在数据通路上插一级带反压的流水线
   - `psi_common_math_pkg` —— 用 `log2ceil` 等函数在编译期推导位宽
5. 待本地验证：纯阅读与列表，无需运行工具。

#### 4.3.5 小练习与答案

**练习 1**：`sync_fifo` 和 `async_fifo` 的本质区别是什么？分别对应表里哪一类？

> **参考答案**：两者都属于 **FIFO** 类。`sync_fifo` 的读写端口在**同一个时钟域**；`async_fifo` 的读写端口在**不同时钟域**，因此内部要用格雷码指针做时钟域同步（细节在 u4）。选型取决于你的数据是否要跨时钟域。

**练习 2**：`tdp_ram`（真双口 RAM）为什么会出现在「可用作 CDC 的其它组件」里（见 [doc/README.md:68-70](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/README.md#L68-L70)）？

> **参考答案**：真双口 RAM 的两个端口可以接不同的时钟，一边写一边读，天然就是一种「跨时钟域搬数据」的介质，所以除了专门的 CDC 组件外，它和 `async_fifo` 都可被当作 CDC 手段使用。

---

### 4.4 版本与变更记录

#### 4.4.1 概念说明

`Changelog.md` 记录了库从 V1.00 到当前 3.0.1 的全部演进。读 changelog 是快速建立「这个库有多成熟、最近在改什么」印象的最高效方式。

同时，README 定义了一套清晰的**标签策略（Tagging Policy）**：版本号 `major.minor.bugfix` 三段的递增规则，让你只看版本号就能判断升级风险。

#### 4.4.2 核心流程

版本号递增规则可以形式化为：

\[
\text{版本} = (\text{major}).(\text{minor}).(\text{bugfix})
\]

判断逻辑：

- **不向下兼容**的改动 → `major` + 1（升级要小心，可能要改你的代码）
- **新增功能**且仍兼容 → `minor` + 1（升级相对安全，多了新东西）
- **仅修 bug**、无功能变化 → `bugfix` + 1（最安全，直接升）

当前库的最新版本是 **3.0.1**，正处于 v3 大版本线上——而 v3.0.0 就是那次「全面统一代码风格、不向下兼容」的大重构。

#### 4.4.3 源码精读

标签策略定义在 README：

- [README.md:45-50](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L45-L50) — **Tagging Policy**：稳定版以 `major.minor.bugfix` 标记，并给出三段递增的明确判定条件。

changelog 里几个关键里程碑：

- [Changelog.md:1-6](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/Changelog.md#L1-L6) — 当前版本 **3.0.1**，是 bugfix 版（修 `pulse_cc` 复位/极性、`async_fifo` 复位极性等）。
- [Changelog.md:8-13](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/Changelog.md#L8-L13) — **3.0.0** 大版本：统一全部代码风格、移除所有 Tab、新增 JSON 数据库迁移脚本、新增 `psi_common_pwm`。这就是 README 顶部说的「不向下兼容」那次重构。
- [Changelog.md:189-202](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/Changelog.md#L189-L202) — **2.0.0**：首个开源版本，加入 `bit_cc`、支持 GHDL 回归，并标注了若干**不向下兼容**的语法变更。
- [Changelog.md:320-321](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/Changelog.md#L320-L321) — **V1.00**：最初发布。

> 从 changelog 还能看出一个有用的规律：**几乎每个新增组件都伴随一条 `Added Features` 记录**，例如 `psi_common_sample_rate_converter`（最近的 `9c39e3d` 提交新增，见 git log）属于这一线的延续。

#### 4.4.4 代码实践

1. **实践目标**：用版本号规则判断「从 2.17.1 升到 3.0.1」的风险等级。
2. **操作步骤**：
   - 读 [README.md:45-50](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L45-L50) 的规则。
   - 读 [Changelog.md:8-13](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/Changelog.md#L8-L13) 的 3.0.0 条目。
   - 判断：`2.17.1 → 3.0.0 → 3.0.1` 这两次跨越分别属于哪种升级？
3. **需要观察的现象**：注意 3.0.0 条目里出现的关键词 "unified"、"tabs removed"、"not backward compatible"（在 README 顶部）。
4. **预期结果**：
   - `2.17.1 → 3.0.0`：**major 升级**（2→3），**不向下兼容**，升级有风险，可能要改你的实例化代码；好在官方提供了 JSON 迁移脚本（见 u11-l3 会讲）。
   - `3.0.0 → 3.0.1`：**bugfix 升级**，无功能变化，风险最低，可直接升。
5. 待本地验证：纯阅读，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：某次提交只修了一个 FIFO 的指针 bug，没加新功能也没改接口。版本号该怎么变？

> **参考答案**：只升 `bugfix` 段（例如 `3.0.1 → 3.0.2`）。根据 [README.md:50](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L50)，"If only bugs are fixed, the bugfix version is incremented"。

**练习 2**：为什么 3.0.0 被 README 形容为「不向下兼容」？给你一个实际影响例子。

> **参考答案**：因为 3.0.0 **统一了全部文件的代码风格并移除了 Tab**（见 [Changelog.md:9-11](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/Changelog.md#L9-L11)），并改动了实体/端口的命名一致性。实际影响：你旧工程里对某些组件的实例化（端口名、generic 名）可能对不上新版本，需要用官方提供的 JSON 迁移脚本批量改写（这正是 u11-l3「重构与迁移脚本」要讲的内容）。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「项目立项速查」小任务：

> 假设你要启动一个新的 FPGA 项目，需要：跨时钟域传输数据、缓存高速 ADC 采样、通过寄存器配置参数。请用本讲学到的知识完成一份**一页速查**，包含：
>
> 1. **定位**：你将在什么场景、以什么角色使用 `psi_common`？（引用 README 的「What belongs」边界说明你的判断）
> 2. **合规**：你能否闭源商用？依据 License 的哪一条？（给出 `License.txt` 的行号引用）
> 3. **选型**：从 `doc/README.md` 的组件表里挑出 3 个最相关的组件，各写一句话用途，并标注它们属于哪一类。
> 4. **版本**：你会选择当前 `3.0.1` 还是更早的稳定版？如果团队仍在用 2.x 代码，升级路径是什么风险等级？

**参考做法要点**（先自己写，再对照）：

- 定位：项目特定代码不入库，但**通用件**（CDC、FIFO、AXI 寄存器接口）直接复用 `psi_common`，引用 [README.md:26-31](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L26-L31)。
- 合规：可闭源商用，依据例外第 2 条 [License.txt:15-19](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/License.txt#L15-L19)（bitstream 属 binary）。正式商用以法务为准。
- 选型示例：`async_fifo`（FIFO 类，跨时钟域缓存）、`axi_slave_ipif` 或 `axilite_slave_ipif`（Interfaces 类，暴露寄存器）、`tdp_ram`（Memory 类，底层存储）。实际选型看你的位宽/时钟域组合。
- 版本：新项目直接用 `3.0.1`；老 2.x 工程升级是 **major 升级**（高风险），建议先用官方 JSON 迁移脚本（[Changelog.md:9-12](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/Changelog.md#L9-L12)）。

---

## 6. 本讲小结

- `psi_common` 是 PSI 维护的**通用、可复用、可综合**的 VHDL 库，只收录「项目无关且能完全 generic 化」的基础电路模块。
- 许可证是 **PSI HDL License = LGPL + 硬件例外**，bitstream/flash 镜像明确算 binary，允许闭源商用；维护者是 Benoît Stef，作者团队 6 人。
- 库里有 60+ 个组件，`doc/README.md` 把它们分成 **Memory / FIFO / CDC / Conversions / TDM / Arbiters / Interfaces / 杂项 / Packages** 九大类——这张表是整本手册的索引。
- 版本号遵循 `major.minor.bugfix`：不兼容升 major、加功能升 minor、仅修 bug 升 bugfix；当前为 3.0.1。
- v3.0.0 是一次「统一风格、不向下兼容」的大重构，官方提供 JSON 迁移脚本帮助老项目升级。
- 进库有一套清晰的代码规范（`snake_case`、`_i/_o/_io` 后缀、统一前缀等），细节将在 `u1-l4` 展开。

---

## 7. 下一步学习建议

本讲建立了全局认知，建议接下来按顺序：

1. **`u1-l2` 仓库结构与目录组织**：搞清楚 `hdl/`、`testbench/`、`sim/`、`doc/`、`generators/`、`scripts/` 各自放什么，以及源码与测试如何一一对应。
2. **`u1-l3` 依赖管理与仿真运行**：学会用 Modelsim/GHDL 跑回归测试，这是后续所有「代码实践」能真正跑起来的前提。
3. **`u1-l4` 编码规范、AXI-S 握手与 TDM 约定**：掌握贯穿全库的握手语义和命名规范，之后再读任何 `.vhd` 都不会在「约定」上卡壳。

如果你想直接看真实源码找感觉，推荐在完成 `u1-l4` 后，先读最简单的包文件 `hdl/psi_common_math_pkg.vhd`（对应 `u2-l1`），它是全库最基础的工具集。
