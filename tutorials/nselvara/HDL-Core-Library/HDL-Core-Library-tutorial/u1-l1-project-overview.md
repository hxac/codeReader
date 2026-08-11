# 讲义 u1-l1：项目定位与 IP 核库概览

> 这是《HDL-Core-Library 学习手册》的第一篇讲义。它面向**完全没接触过本项目**的读者，目标是让你在读后续任何一篇讲义之前，先建立一张清晰的「全局地图」。

## 1. 本讲目标

读完本讲，你应当能够：

1. 用一两句话讲清楚 **HDL-Core-Library 是什么**、它解决什么工程问题。
2. 说出项目的**技术栈**（VHDL-2008 / VUnit / OSVVM）与**许可证**（MIT）。
3. 列举项目的**四大类 IP 核**（存储器 / 同步时序 / 通信 / 输入处理）以及每一类的代表模块。
4. 理解「**可复用 IP 核**」相对于「一次性手写 RTL」的价值，并知道本项目通过「同一实体 + 多厂商架构」实现可移植性。

本讲**不要求你已经会写 VHDL**，但后续讲义会逐步深入到真实源码。

## 2. 前置知识

为了让零基础读者也能跟上，这里先解释几个术语：

- **HDL（Hardware Description Language，硬件描述语言）**：用文本描述数字电路的语言。VHDL 是其中一种（另一种常见的是 Verilog）。写出来的代码最终会被「综合」成 FPGA/ASIC 里的真实逻辑门和寄存器。
- **IP 核（Intellectual Property Core）**：一段经过验证、可在多个项目里重复使用的硬件模块。你可以把它类比成软件里的「库函数」或「npm 包」——比如「一个 FIFO」不必每次都从头写。
- **RTL（Register Transfer Level，寄存器传输级）**：HDL 代码最常见的写法层级，描述数据在寄存器之间如何流动和运算。
- **FPGA（Field Programmable Gate Array，现场可编程门阵列）**：一种可以在出厂后由用户重新编程配置逻辑的芯片。本库主要面向 Xilinx 和 Intel/Altera 两家厂商的 FPGA。
- **CDC（Clock Domain Crossing，跨时钟域）**：当两个模块跑在不同时钟下，信号从一边传到另一边时需要特殊处理（同步器/FIFO），否则会出现亚稳态。这是后面多讲的核心话题。
- **VUnit / OSVVM**：两个 VHDL 验证框架。VUnit 负责自动发现并批量跑测试台；OSVVM 提供随机化、功能覆盖等高级验证特性。本手册第 11 单元会专门讲。

如果你对这些概念只有模糊印象，没关系——本讲只在「概览」层面用到它们，后续讲义会逐一展开。

## 3. 本讲源码地图

本讲的「源码」其实就是项目自己的说明书。我们只读一个文件，但它信息量很大：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目主文档。包含项目定位、IP 核清单、目录结构、技术支持矩阵、运行方式与许可证。本讲几乎所有结论都来自这里。 |

> 说明：本讲是「概览」性质，因此以阅读文档为主，不展开具体 `.vhd` 源码。从下一篇讲义（u1-l2）开始，我们会真正进入 `ip/` 目录读 VHDL 源码。

后续会引用到的几处关键内容，先用永久链接标出来，方便你随时核对：

- 项目一句话定位：[README.md:L4-L6](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L4-L6) —— 标题与项目简介。
- 顶部徽章（许可证 + CI）：[README.md:L1-L2](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L1-L2) —— MIT License 徽章与 VUnit Tests 徽章。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 HDL-Core-Library 项目整体**：它是什么、解决什么问题、可复用 IP 核的价值。
- **4.2 技术栈与工程化支撑**：VHDL-2008 / VUnit / OSVVM / 多厂商支持 / 许可证。
- **4.3 IP 核分类总览**：四大类 IP 核及其代表模块。

---

### 4.1 HDL-Core-Library 项目整体

#### 4.1.1 概念说明

`HDL-Core-Library`（仓库内常写作 *HDL Core Library*）是一个**可复用 VHDL IP 核库**：作者把数字设计里最常重复使用的功能模块（RAM、FIFO、同步器、SPI 接口、消抖器……）逐个写成经过验证的 VHDL 代码，集中到一个仓库里。

README 开头一句话就给出了定位：

> A comprehensive collection of reusable VHDL IP cores for digital design, including memory modules, synchronisers, clock generators, and utility packages.

翻译过来就是：「一套面向数字设计的、可复用的 VHDL IP 核集合，涵盖存储器、同步器、时钟生成器与工具包。」

**为什么需要这样的库？** 这涉及「可复用 IP 核」相对于「一次性 RTL」的价值：

- **避免重复造轮子**：几乎每个 FPGA 项目都要 FIFO、都要跨时钟域同步器。手写一遍不仅耗时，还容易踩坑。
- **经过验证、可信赖**：本库每个模块都配了 VUnit 测试台（见 4.2），功能有自动化回归保障。
- **跨项目、跨厂商移植**：同一份 IP 代码可以在 Xilinx 和 Intel 两家 FPGA 上使用，不必因为换芯片而重写。
- **统一的接口风格**：全库采用一致的命名（如 `write_data` / `read_data`、`sys_clk` / `sys_rst_n`），读会一个，就会读一片。

#### 4.1.2 核心流程

从「有一个数字设计需求」到「复用本库」的过程，可以用下面这张「使用流程图」概括：

```text
  设计需求（例如：需要一个跨时钟域的 FIFO）
            │
            ▼
  在 ip/ 目录下找到对应 IP（如 ip/memories/fifo/fifo_async.vhd）
            │
            ▼
  阅读 README 的「Technology Support」表，选定一种实现：
      Xilinx / Intel / 自研行为级（own_behavioural）
            │
            ▼
  在自己的顶层里用 entity work.<ip名>(<架构名>) 例化
            │
            ▼
  复用该 IP 自带的 tb_*.vhd 测试台做验证（VUnit 一键回归）
```

注意第 3 步「选定一种实现」——这正是本项目最核心的设计模式：**同一份 entity（端口契约）提供多套 architecture（实现）**。这个模式会在第 2 单元（u2-l1）专门讲解，本讲只需先建立印象。

#### 4.1.3 源码精读

项目的定位与价值，集中体现在 README 的标题段与「Key Features」一节。

**项目标题与一句话定位** —— [README.md:L4-L6](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L4-L6)

```text
# HDL Core Library
A comprehensive collection of reusable VHDL IP cores for digital design,
including memory modules, synchronisers, clock generators, and utility packages.
```

这一句是全库的「电梯演讲」，关键定语是 **reusable（可复用）** 和 **comprehensive（成体系的）**。

**六大关键特性（Key Features）** —— [README.md:L44-L51](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L44-L51)

README 用六个要点总结了本库的工程价值，摘其要者：

- **MIT Licensed**：许可证宽松，商业与开源皆可。
- **VHDL-2008 Compatible**：采用现代 VHDL 标准（这是后面非约束数组、generic package 等写法的前提）。
- **Comprehensive Testing**：所有模块都有 VUnit 测试覆盖。
- **FPGA Optimised**：针对 Xilinx 与 Intel FPGA 做了综合优化。
- **Instance-Based Naming**：基于实例的清晰端口命名（如 `write_data`/`read_data`）。
- **Enhanced Interfaces**：完整的使能、复位与控制信号支持。

**许可证确认** —— 项目在顶部挂了 MIT 徽章：[README.md:L1-L2](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L1-L2)。仓库根目录的 `LICENSE` 文件头部也写明 `MIT License, Copyright (c) 2025 N. Selvarajah`，与徽章一致。

#### 4.1.4 代码实践

**实践目标**：亲手核对项目的「身份信息」，而不是只听讲义转述。

**操作步骤**：

1. 打开仓库根目录的 `README.md`，找到第 4–6 行的项目简介。
2. 打开根目录的 `LICENSE` 文件，确认许可证类型与版权年份。
3. 打开 `.gitmodules` 文件，看本项目是否引用了外部子模块。

**需要观察的现象**：

- `README.md` 顶部有两个徽章：MIT License、VUnit Tests。
- `LICENSE` 第一行是 `MIT License`。
- `.gitmodules` 指向 `https://github.com/nselvara/VHDL-Utils.git`（路径 `ip/vhdl_utils`）——也就是说，项目把一部分通用工具包做成了独立的 git 子模块。

**预期结果**：你应当能用一句话回答「这个项目是什么、用什么许可证、是否完全自包含」。其中「它引用了一个外部子模块 VHDL-Utils」这一点，是只读 README 容易忽略、但对后续编译很关键的细节（后续 u3-l2 会详述）。

> 待本地验证：如果你尚未 `git clone --recursive` 或执行 `git submodule update --init`，`ip/vhdl_utils` 目录可能是空的。这一现象留到 u3-l2 解决，本讲只需记住「本库依赖一个外部工具包子模块」。

#### 4.1.5 小练习与答案

**练习 1**：用一句话（不超过 30 个字）向同事介绍 HDL-Core-Library 是什么。

> **参考答案**：一套可复用、跨厂商（Xilinx/Intel）、带 VUnit 测试的 VHDL-2008 IP 核库。

**练习 2**：「可复用 IP 核」相比「每个项目从头写一遍 RTL」，请列出至少两条好处。

> **参考答案**：① 避免重复劳动、降低出错概率；② 复用已验证的测试台，功能有回归保障；③ 统一接口风格，降低团队阅读成本；④ 跨项目/跨厂商移植，换芯片不必重写。（任答两条即可）

**练习 3**：本项目的许可证是什么？能否用于商业产品？

> **参考答案**：MIT 许可证（Copyright (c) 2025 N. Selvarajah）。MIT 是宽松许可证，允许商业使用、修改和再分发，只需保留版权与许可声明即可。

---

### 4.2 技术栈与工程化支撑

#### 4.2.1 概念说明

一个 IP 核库要「好用」，光有 RTL 不够，还需要一套**工程化基础设施**把它支撑起来。本项目的工程化支撑由四部分组成：

1. **语言标准：VHDL-2008**。这是 2008 版的 VHDL 标准，相比老版本增加了非约束数组元素、generic package、`context` 等好用特性。本库大量依赖这些特性（后续讲义会逐一遇到）。
2. **验证框架：VUnit + OSVVM**。VUnit 负责「自动发现测试台、批量编译、批量仿真、产出报告」；OSVVM 提供「随机化激励、断言式校验」。两者配合，让每个 IP 都能一键回归。
3. **多厂商支持：Xilinx / Intel / 自研行为级**。同一份端口契约配多套实现，使代码不绑死单一芯片厂商。
4. **开发工具链：TerosHDL / VHDL-LS + Python venv**。用 VSCode 插件做代码导航与文档生成，用 Python 虚拟环境管理 VUnit 依赖。

#### 4.2.2 核心流程

这些基础设施如何串成一个「写代码 → 验证 → 跨厂商落地」的闭环：

```text
  编写/复用 VHDL-2008 IP（entity + 多 architecture）
            │
            ├── 验证侧：tb_*.vhd 测试台
            │       └─ VUnit 自动发现 ──► check_equal / OSVVM 随机化 ──► 通过/失败 + xunit 报告
            │
            └── 实现侧：选定 architecture
                    ├─ Xilinx 架构 ─► 用 XPM/UNISIM（需 use_xilinx_libs）
                    ├─ Intel 架构  ─► 用 altera_mf
                    └─ 自研行为级  ─► 纯 VHDL-2008，无需厂商库（永远可用）
```

一个要点：**自研行为级（own/behavioural）实现永远可用**，因为它不依赖任何厂商库；而 Xilinx/Intel 实现需要相应仿真库在场。这一点直接决定了「能不能开箱即用地跑仿真」。

#### 4.2.3 源码精读

**验证技术栈声明** —— [README.md:L140](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L140)

> The VHDL codes are tested with VUnit framework's checks, OSVVM random features and simulated with EDA Playground and/or ModelSim.

这一行明确了「验证三件套」：VUnit 的 `check` 系列、OSVVM 的随机化、以及 EDA Playground / ModelSim 作为仿真器。

**多厂商技术支持矩阵** —— [README.md:L304-L310](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L304-L310)

README 列出三种实现路线：

```text
- Xilinx:        使用 XPM、UNISIM、UNIMACRO 等仿真库
- Intel/Altera:  使用 altera_mf 等仿真库
- Own/Behavioral:不依赖厂商库的纯 VHDL-2008 行为级实现
```

完整的对照表见 [README.md:L312-L326](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L312-L326)。表中绝大多数 IP 在三套实现下都是 `Yes`，**唯一的例外是 Clock Generator (PLL)**：它有 Xilinx PLL 和 Intel PLL 两套厂商实现，但**没有自研行为级实现**（`No`）。这是因为 PLL 是硬核模拟资源，无法用纯 RTL 行为级精确复刻。这也解释了为什么后续 CI 配置里 PLL 测试台会被排除（见 u1-l4）。

**厂商库路径与注意事项** —— [README.md:L328-L338](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L328-L338) 给出了 Xilinx / Intel 仿真库在 Linux 与 Windows 下的标准安装路径，并强调「自研行为级实现永远可用、无需厂商库」「PLL 无纯行为级实现」。

**Python 依赖**（本讲补充确认）—— `ip/requirements.txt` 的核心依赖是 `vunit_hdl>=5.0.0.dev5`，外加 `teroshdl`、`cocotb`、`yowasp-yosys`、`edalize`、`vsg` 等 TerosHDL 工具链包。这说明项目用 Python venv 来托管 VUnit 与文档工具链。

#### 4.2.4 代码实践

**实践目标**：搞清楚「跑这个项目的仿真，最少需要装什么」。

**操作步骤**：

1. 打开 `ip/requirements.txt`，列出全部依赖包名。
2. 在 README 中搜索 `use_xilinx_libs`，阅读其上方的 `> [!WARNING]` 段落。
3. 思考：如果你只跑「自研行为级」实现的测试，是否需要安装 Xilinx/Intel 仿真库？

**需要观察的现象**：

- `requirements.txt` 的第一行是 `vunit_hdl>=5.0.0.dev5`，这是 VUnit 的版本约束。
- README 的警告说明：一旦设计用到 Xilinx 原语（如 `xpm_cdc`、`xpm_memory`），就必须设 `use_xilinx_libs=True`，否则会报 `Failed to find 'glbl' in hierarchical name 'glbl.GSR'`。

**预期结果**：你能回答「最小依赖 = Python venv + VUnit + 一个支持 VHDL-2008 的仿真器」；而厂商库只在选用 Xilinx/Intel 架构时才需要。

> 待本地验证：具体的环境搭建与第一次仿真，留到 u1-l3 手把手演练。本讲只要求你理解依赖关系。

#### 4.2.5 小练习与答案

**练习 1**：本项目的验证「三件套」分别是什么，各负责什么？

> **参考答案**：① VUnit——自动发现并批量运行测试台、提供 `check_equal` 等校验；② OSVVM——提供随机化激励（如 `RandomPType`）与高级验证特性；③ 仿真器（EDA Playground / ModelSim / NVC）——执行 VHDL 仿真。

**练习 2**：为什么 PLL 是全库唯一「没有自研行为级实现」的 IP？

> **参考答案**：PLL（锁相环）本质是芯片里的模拟硬核资源，其倍频/分频、抖动、锁定行为依赖厂商硬核，无法用纯数字 RTL 行为级精确复刻。因此它只有 Xilinx（`PLLE2_BASE`）和 Intel（`altclklock`）两套厂商实现，没有 `own_behavioural_*`。

**练习 3**：README 里 `use_xilinx_libs` 这个开关解决的是什么报错？

> **参考答案**：当设计例化了 Xilinx 的 XPM 原语时，仿真器需要 Xilinx 的 `glbl` 模块和仿真库（`-L xpm -L unisims_ver -L secureip`）。不打开此开关会报 `Failed to find 'glbl' in hierarchical name 'glbl.GSR'` / `Error loading design`。打开后会自动引入这些库。

---

### 4.3 IP 核分类总览

#### 4.3.1 概念说明

README 把本库的 IP 核分成**四大功能类别**外加**工具包**。理解这个分类，等于拿到了全库的「目录索引」：

| 类别 | 中文名 | 解决的问题 |
| --- | --- | --- |
| Memory Modules | 存储器 | 怎么存数据：随机读写、先进先出、只读表 |
| Synchronisation & Timing | 同步与时序 | 跨时钟域、时钟生成、复位、分频使能 |
| Communication Interfaces | 通信接口 | 与外部芯片按标准协议交换数据（目前是 SPI） |
| Input Processing | 输入处理 | 处理物理按键/开关这类「脏」输入 |
| Utility Packages | 工具包 | 复用的类型、函数与仿真辅助（非硬件模块） |

前四类是**可综合的硬件模块**（最终变成电路），第五类是**辅助代码**（类型定义、函数、测试台工具），不直接综合成硬件。

#### 4.3.2 核心流程

当你在本项目里「找一个 IP」时，标准路径是「按类别 → 按模块 → 按文件」三级定位：

```text
  想找「一个 FIFO」
        │
        ▼  （类别）
  Memory Modules / memories/fifo/
        │
        ▼  （模块）
  fifo_sync.vhd（同步 FIFO）/ fifo_async.vhd（异步 FIFO）
        │
        ▼  （文件）
  设计源码 .vhd ＋ 测试台 tb/tb_*.vhd ＋ 波形脚本 tb/*.do
```

这套「`ip/<类别>/<模块>/` 内放设计源码、`tb/` 子目录放测试台与 `.do` 波形脚本」的约定，是全库统一的组织方式（u1-l2 会专门讲目录结构）。本讲你只需先记住**四大类别 → 代表模块**的映射。

#### 4.3.3 源码精读

README 的「Available IP Cores」一节就是这张索引表的权威来源。

**① 存储器类（Memory Modules）** —— [README.md:L8-L17](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L8-L17)

| 模块 | 作用 |
| --- | --- |
| Single Port RAM | 单口 RAM：同一端口分时读写 |
| Dual Port RAM | 真双口 RAM：读写端口独立，可并发 |
| Dual Clock RAM | 双时钟双口 RAM：读写用不同时钟，跨时钟域用 |
| Synchronous FIFO | 同步 FIFO：单时钟域，先进先出缓冲 |
| Asynchronous FIFO | 异步 FIFO：独立读写时钟，用格雷码指针跨域 |
| ROM | 只读存储器，支持用初始化文件加载内容 |

**② 同步与时序类（Synchronisation & Timing）** —— [README.md:L19-L25](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L19-L25)

| 模块 | 作用 |
| --- | --- |
| FF Synchroniser | 多级触发器同步器，用于单比特 CDC |
| FF Synchroniser Vector | 向量版同步器，用于多比特 CDC |
| Clock Generator | 可配置时钟生成（PLL） |
| Reset on Startup | 上电复位生成 |
| Clock Enable | 时钟使能/分频生成 |

**③ 通信接口类（Communication Interfaces）** —— [README.md:L27-L33](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L27-L33)

目前只有 SPI 一族：`spi_interface`（顶层）下含 `spi_tx`（发送）、`spi_rx`（接收）、`spi_pkg`（模式/类型包）。它是全库最复杂的 IP，综合用到了 clock_enable、异步 FIFO、通用包与多片选状态机（第 10 单元专讲）。

**④ 输入处理类（Input Processing）** —— [README.md:L34-L37](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L34-L37)

只有一个模块 `debouncer`（消抖器）：用计数器判定按键/开关输入是否稳定，滤除毛刺。它也是全库**最简单独立**的模块，是第 4 单元用来「建立读码自信」的入门例子。

**⑤ 工具包（Utility Packages）** —— [README.md:L38-L43](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L38-L43)

- `utils_pkg`：设计与验证通用的工具函数（来自 `ip/vhdl_utils` 子模块）。
- `tb_utils`：测试台辅助（时钟生成、复位、仿真支持，同样来自子模块）。
- `memories_pkg`：存储相关常量与类型（本仓库自带，位于 `ip/memories/memories_pkg.vhd`）。

> 注意：README 的目录树（[README.md:L53-L69](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L53-L69)）把工具包写成 `utils/`，但仓库里实际的目录是 `ip/vhdl_utils/`（一个 git 子模块）。读源码时以 `git ls-files` 与 `.gitmodules` 为准。

#### 4.3.4 代码实践

**实践目标**：把「类别 → 模块 → 它解决什么问题」这条链在脑子里走通。

**操作步骤**：

1. 打开 README 的 [Available IP Cores](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L8-L43) 一节（L8–L43）。
2. 挑两个你感兴趣的模块，分别回答：「如果没有这个 IP，我会怎么手写？它替我省了什么麻烦？」
3. 对照 [Technology Support 表](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L312-L326)（L312–L326），确认这两个模块在 Xilinx / Intel / 自研行为级 下是否都有实现。

**需要观察的现象**：你会注意到，除了 PLL，其他所有 IP 在三套实现下都标 `Yes`——这就是「同一实体多架构」模式带来的可移植性。

**预期结果**：你能不查文档，凭记忆画出「四大类 → 每类 2~3 个代表模块」的脑图。

> 待本地验证：本步骤是纯阅读理解，无需运行任何命令。

#### 4.3.5 小练习与答案

**练习 1**：把下列模块归入正确的类别：`debouncer`、`fifo_async`、`ff_synchroniser`、`spi_interface`、`single_port_ram`。

> **参考答案**：
> - 输入处理：`debouncer`
> - 存储器：`fifo_async`、`single_port_ram`
> - 同步与时序：`ff_synchroniser`
> - 通信接口：`spi_interface`

**练习 2**：`Dual Clock RAM` 和 `Asynchronous FIFO` 都涉及「跨时钟域」，它们的关系是什么？

> **参考答案**：`Dual Clock RAM` 是底层存储原语——它提供「写口用写时钟、读口用读时钟」的双口存储；`Asynchronous FIFO` 在它之上**复用**了这块存储体，再加上用格雷码指针 + 同步器实现的跨域指针同步，封装成一个完整的异步 FIFO。即「RAM 是存储底座，FIFO 是上层成品」（详见 u6-l3 与 u9-l3）。

**练习 3**：工具包里的 `utils_pkg` 和 `memories_pkg` 有什么本质区别？

> **参考答案**：`memories_pkg` 是本仓库自带的包（在 `ip/memories/memories_pkg.vhd`，定义 `rom_t` 等存储类型）；`utils_pkg` 来自外部 git 子模块 `ip/vhdl_utils`（VHDL-Utils 仓库），提供 `to_bits`、`get_lowest_active_bit` 等通用函数。一个是「本地源码」，一个是「外部依赖」。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这张**对照表任务**（这是本讲的主实践任务）：

**任务**：阅读 README 的「Available IP Cores」（[L8-L43](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L8-L43)）与「Technology Support」（[L304-L338](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L304-L338)）两节，用自己的话写出一张「**IP 核 → 它解决什么问题**」对照表，并为**每个存储类 IP** 标注其支持的厂商实现（Xilinx / Intel / 自研行为级）。

**参考表头（请你填完）**：

| IP 核 | 所属类别 | 它解决什么问题（用一句话，自己的话） | Xilinx | Intel | 自研行为级 |
| --- | --- | --- | --- | --- | --- |
| Single Port RAM | 存储器 | *（请填写）* | Yes | Yes | Yes |
| Dual Port RAM | 存储器 | *（请填写）* | … | … | … |
| Dual Clock RAM | 存储器 | *（请填写）* | … | … | … |
| Synchronous FIFO | 存储器 | *（请填写）* | … | … | … |
| Asynchronous FIFO | 存储器 | *（请填写）* | … | … | … |
| ROM | 存储器 | *（请填写）* | … | … | … |

**验收标准**：

1. 每个 IP 的「解决问题」一栏是**你自己的话**，而不是照抄 README。
2. 存储类 6 个 IP 的厂商实现列，与 README 的 Technology Support 表完全一致（应全部为 `Yes`）。
3. 你能额外指出：全库**唯一**没有自研行为级实现的 IP 是哪一个，并解释原因（答：PLL，因为它是硬核模拟资源）。

> 待本地验证：本任务无需运行仿真，但建议你把填好的表保存下来——它就是后续整个学习手册的「速查索引」。

## 6. 本讲小结

- **HDL-Core-Library** 是一套面向数字设计的**可复用 VHDL-2008 IP 核库**，MIT 许可证，可商用。
- 项目价值在于「**可复用 + 经验证 + 跨厂商 + 统一接口**」，避免每个项目重复造轮子。
- 工程化支撑由 **VHDL-2008 + VUnit + OSVVM** 构成验证闭环，外加 TerosHDL/VHDL-LS 工具链与 Python venv。
- IP 核分**四大功能类**：存储器、同步与时序、通信接口（SPI）、输入处理；另有工具包（`utils_pkg`/`tb_utils`/`memories_pkg`）。
- 核心设计模式是「**同一 entity + 多套 architecture**」（Xilinx / Intel / 自研行为级），使代码可跨厂商移植。
- **PLL** 是全库唯一没有自研行为级实现的 IP；工具包中的 `utils_pkg`/`tb_utils` 实际来自外部子模块 `ip/vhdl_utils`。

## 7. 下一步学习建议

本讲建立了全局地图，接下来建议：

1. **u1-l2 仓库目录结构与 IP 组织约定**：进入 `ip/` 目录，搞清楚每个模块文件夹「设计源码 + `tb/` 测试台 + `.do` 波形脚本」的固定布局，让你能 30 秒内定位任意 IP。
2. **u1-l3 开发环境搭建与本地仿真运行**：亲手用 Python venv 装 VUnit，跑通 `test_runner.py`，完成第一次全量仿真。
3. **u2-l1 同一实体多架构模式**：正式进入 VHDL 源码，理解本库最核心的「多厂商架构」设计模式——它是后续所有 IP 讲义的钥匙。

> 小贴士：如果你想立刻看到真实 VHDL 代码，可以提前翻一眼 `ip/debouncer/debouncer.vhd`——它是全库最简单的模块，第 4 单元会拿它做第一个精读对象。
