# 仓库目录结构与六大子系统导航

> 本讲是学习手册的**第二篇**，承接 [u1-l1 项目定位与开源技术栈](u1-l1-project-overview.md)。上一篇让你建立了「全局地图」——知道了项目是什么、由哪些开源技术拼成。本篇要把这张地图**落到硬盘上**：带你认清仓库顶层 8 个目录各自管什么、它们之间怎么咬合，以及在每个子目录里该从哪个 README 读起。读完本讲，你在仓库里找任何代码都不会迷路。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出顶层 8 个目录（`0.doc` / `1.hw` / `2.sw` / `3.build` / `4.sim` / `5.lint` / `6.test` / `99.warmup`）**各自的职责**，并指出其中**六个核心工程子系统**（`1.hw`–`6.test`）。
2. 理解 `1.hw` / `2.sw` / `3.build` 这三者为何构成项目的 **HW / SW / Build 主干**，并讲清三步构建（CSR → SW → HW）的先后依赖。
3. 看懂 `1.hw/top.filelist` 如何把分散的源文件（含外部库、自动生成的 CSR、IMEM）「拼装」成顶层设计 `top.sv`。
4. 在任意一个子系统里，迅速找到它的**入口 README**，并知道它承接的是 root README 的哪一节。

---

## 2. 前置知识

本讲假定你已读过 [u1-l1](u1-l1-project-overview.md)，对下面的术语有印象即可，这里再补两个本讲会用到的「导航」概念：

- **仓库（repository）**：一个项目在版本控制下的全部文件集合。本讲只看它的**目录结构**，不深入代码逻辑。
- **子系统（subsystem）**：项目里职责相对独立的一大块。本项目用数字前缀给目录编号，数字本身就暗示了它在流程里的位置（文档 0 → 硬件 1 → 软件 2 → 构建 3 → 仿真 4 → 检查 5 → 测试 6 → 热身 99）。
- **filelist（文件清单）**：一个文本文件，逐行列出参与某个设计的所有源文件。FPGA 工具链靠它知道「要编译哪些 `.v/.sv`」。本项目的 `1.hw/top.filelist` 就是这样的清单。
- **bitstream（比特流）**：综合布线后最终烧进 FPGA 的二进制文件。`3.build` 的最终产物之一。
- **bring-up（点亮）**：把新硬件/新设计第一次跑起来的过程，常伴随一系列最小测试。`99.warmup` 就是这条「逐块点亮」的练习线。

如果上面某个词你还陌生，不必担心——本讲主要是「认路」，不需要理解电路细节。

---

## 3. 本讲源码地图

本讲主要读三类「结构性」文件，它们是导航整个仓库的钥匙：

| 文件 | 作用 | 本讲怎么用 |
| --- | --- | --- |
| [README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md) | 项目总入口，用一节节文字把读者引向各子系统的 README。 | 当作「目录索引的总目录」，看它如何指向 1.hw/2.sw/3.build/4.sim/6.test。 |
| [1.hw/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md) | 硬件架构与数据流说明。 | 佐证 `1.hw` 子系统的职责（数据面）。 |
| [3.build/README.md](https://github.com/chili-chips-ba-wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/README.md) | 构建流程说明（CSR / SW / HW 三步）。 | 讲清子系统间的构建依赖，是「相互关系」一节的核心依据。 |
| [1.hw/top.filelist](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist) | 顶层设计的源文件清单。 | 看 `1.hw` 如何把外部库、生成的 CSR、CPU、DPE 拼装成 `top.sv`。 |

> 提示：本讲是「认路篇」，引用的是 README 与清单文件，而非具体电路逻辑。从下一篇 [u1-l3](u1-l3-hardware-platform.md) 起才会逐块进入真实 RTL。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 顶层目录职责** —— 8 个目录各自管什么。
- **4.2 子系统相互关系** —— 它们怎么咬合成一条流水线（构建链 + HW/SW 划分 + filelist 拼装）。
- **4.3 入口文档索引** —— 每个子系统从哪个 README 读起。

---

### 4.1 顶层目录职责

#### 4.1.1 概念说明

wireguard-fpga 是一个**多学科大型项目**：既有硬件 RTL，又有嵌入式软件，还有构建脚本、仿真测试台、风格检查和上板验证。为了避免所有代码混作一团，项目用一个清晰的**编号目录**来分区。编号本身就是导航线索：

```
0.doc        ← 文档（在一切之前）
1.hw ~ 6.test ← 六个核心工程子系统（编号 1~6）
99.warmup    ← 热身练习（在主线之外的「附加题」）
```

本讲标题里的「**六大子系统**」，指的就是编号 1–6 这六个承担实际工程职责的目录：`1.hw`、`2.sw`、`3.build`、`4.sim`、`5.lint`、`6.test`。而 `0.doc`（文档）和 `99.warmup`（热身）是两端的**辅助目录**——前者提供说明，后者提供分块练习。

#### 4.1.2 核心流程：一张总表读懂 8 个目录

下表把每个目录的职责、关键内容，以及「它属于哪一类」一次性列清：

| 目录 | 类别 | 关键内容（已确认存在） | 一句话职责 |
| --- | --- | --- | --- |
| `0.doc/` | 辅助·文档 | 5 份分章 PDF（架构/CSR/原子更新/字节序/协同仿真）、`Alinx/`、`Xilinx/`、`Crypto/`、`Wireguard/`、`artwork/`、`0.README.txt` | 全项目的设计文档、幻灯片、参考资料与配图 |
| `1.hw/` | 核心·硬件 | `top.sv`、`top.filelist`、`constraints/top.xdc`、`external_lib/`、`fpgatech_lib/`、`ip.cpu/`、`ip.dpe/`、`ip.infra/` | 硬件 RTL 设计（数据面 + SoC 顶层） |
| `2.sw/` | 核心·软件 | `app/`（加密+网络+`main.cpp`）、`boot_crt.s`、`link_map.lds`、`tests/` | 控制面软件（RISC-V 裸机固件） |
| `3.build/` | 核心·构建 | `MakefileCSR/HW/SW`、`csr_build/`、`sw_build/`、`hw_build.Vivado/`、`hw_build.openXC7/`、`pipelinec_build/`、`pypeline_build/`、`sysrdl_cosim.py` | 构建脚本与生成产物（CSR/SW/HW 三步） |
| `4.sim/` | 核心·仿真 | `tb.sv`、`MakefileVProc.mk`、`models/`、`rtl/`、`usercode/`、`tools/`、`*.gtkw` | 仿真测试台与软硬件协同验证 |
| `5.lint/` | 核心·检查 | `lint_run.sh`、`rules.md`、`test.sh` | SystemVerilog 代码风格/质量检查 |
| `6.test/` | 核心·测试 | `README.md`、`busr.UART.py`、`busw.UART.py`、`dump_packet.UART.py`、`imwr.UART.py`、`loopback.UART.py` | 实验室上板测试与 UART 脚本 |
| `99.warmup/` | 辅助·热身 | `0.blinky` ~ `11.hkdf`、`99.docker` | 增量式分块「热身」练习与 bring-up |

> 名词速查：**bring-up** 即「点亮」一块新设计——先点 LED（`0.blinky`），再测以太网（`1.hw-eth-test`），再逐个点亮加密原语（`5.chacha20poly1305`、`7.curve25519`、`8.BLAKE2s` …）。`99.warmup` 就是这条「逐块验证」的练习线，编号 99 表示它游离于主线 0–6 之外。

#### 4.1.3 源码精读：编号背后的「流程顺序」

数字目录不只是好看，它**暗示了从设计到上板的流程顺序**。把六个核心子系统按编号连起来，就是一条工程主线：

```
1.hw（设计 RTL）──┐
                  ├──► 3.build（综合+编译）──► 6.test（上板验证）
2.sw（设计固件）──┘            ▲
   ▲                          │
   └──── 4.sim（仿真验证）────┘  （在真正上板前先仿真）
   └──── 5.lint（风格检查）────►  （随时对 1.hw 的 SV 代码做 lint）
```

佐证之一是 `3.build/README.md` 开篇那句——构建由三步组成，正好横跨 CSR、SW、HW：

> 见 [3.build/README.md:9-12](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/README.md#L9-L12)，中文复述：① 从 RDL 规格编译出 CSR 的 RTL 与软件 HAL；② 为 RISC-V 目标编译软件；③ 把 SystemVerilog 综合成比特流。

这段话也直接说明了为什么 `1.hw`（提供 RTL）、`2.sw`（提供固件）、`3.build`（负责三步编译）三者构成主干——它们正好对应构建的三种「原料」与一个「加工厂」。

#### 4.1.4 代码实践：画一张顶层目录树

这是本讲的**主实践任务**（对应规格要求）。

1. **实践目标**：亲手把仓库顶层结构内化为一张可回看的「目录树 + 职责表」。
2. **操作步骤**：
   - 在本地克隆仓库后，进到仓库根目录，运行 `ls`（或 `tree -L 1`，若已安装）查看顶层。
   - 仿照 4.1.2 的表格，为 8 个目录各写**一句话职责**。
   - 用高亮（或星号）标出构成 **HW / SW / Build 主干**的那三个目录：`1.hw`、`2.sw`、`3.build`。
   - 进一步用 `ls 1.hw`、`ls 2.sw/app`、`ls 3.build` 各看一眼，确认表里列的子目录确实存在。
3. **需要观察的现象**：你会发现 6 个核心目录（`1`–`6`）正好覆盖「设计→编译→仿真→检查→测试」全链路，而 `0.doc` 与 `99.warmup` 是辅助。
4. **预期结果**：得到一张标注好主干的三列表 `目录 | 一句话职责 | 是否主干`。
5. 本实践以阅读和检索为主，无需运行综合命令；运行 `ls` 类命令的输出属于本地环境，无需联网验证。

#### 4.1.5 小练习与答案

**练习 1**：标题说「六大子系统」，但顶层有 8 个目录。多出来的两个是哪几个？为什么它们不算「核心工程子系统」？

> **参考答案**：多出来的是 `0.doc`（文档）和 `99.warmup`（热身练习）。`0.doc` 只提供说明性材料，不参与构建与运行；`99.warmup` 是游离于主线之外的分块练习与 bring-up，编号 99 也暗示它是「附加」。核心工程子系统是编号 1–6：`1.hw`、`2.sw`、`3.build`、`4.sim`、`5.lint`、`6.test`。

**练习 2**：`5.lint` 目录里只有 `lint_run.sh`、`rules.md`、`test.sh` 三个文件，没有 RTL。这说明它的职责是什么？

> **参考答案**：`5.lint` 本身**不产出设计**，而是对 `1.hw` 里的 SystemVerilog 代码做风格/质量检查。`rules.md` 定义规则（例如强制用 `always_comb` 而非 `always @*`），`lint_run.sh` / `test.sh` 负责跑检查。它是横跨在整个开发流程上的「质检站」，所以编号居中、可随时运行。

---

### 4.2 子系统相互关系

#### 4.2.1 概念说明

光知道每个目录管什么还不够，关键要懂它们**怎么咬合**。本项目有两条最重要的关系线：

1. **构建依赖链**：`3.build` 的三步（CSR→SW→HW）有严格的先后，且必须先有 SW 才能合成 HW。
2. **HW/SW 划分**：`1.hw` 是数据面（线速转发+加解密），`2.sw` 是控制面（握手+路由管理），二者由 CSR 这一「桥梁」连接。

理解这两条线，你就理解了为什么目录要这样编号、为什么 `1.hw` 里会出现「来自 `3.build` 的生成文件」。

#### 4.2.2 核心流程：三步构建与 HW/SW 分区

**构建链（CSR → SW → HW）。** `3.build/README.md` 把构建拆成三步，每一步都有专属 Makefile：

```
① CSR：  make -f MakefileCSR  → 从 csr.rdl 生成 csr.sv / csr_pkg.sv / csr_hw.h / csr_cosim.h
② SW ：  make -f MakefileSW   → 交叉编译固件，生成 main.elf/.hex/.bin 与 imem.INIT.vh
③ HW ：  make -f MakefileHW   → 综合 PnR，生成 bitstream（Vivado 或 openXC7）
```

这里有一个**硬性先后**：SW 必须先于 HW。原因是 SW 这一步会产出 `imem.INIT.vh`（指令内存的 Verilog 初始化文件），而 HW 综合时要把这个文件「包」进 `imem.sv` 里。`3.build/README.md` 在 openXC7 流程里专门用粗体强调了这一点（[3.build/README.md:152](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/README.md#L152)），并在故障排查里把「缺 `imem.INIT.vh`」列为常见错误（[3.build/README.md:225-229](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/README.md#L225-L229)，提示先跑 `make -f MakefileSW`）。

**HW/SW 分区。** root README 的「Design Blueprint」把系统设计成两层（[README.md:111](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L111)）：

- **控制面**（软件，落在 `2.sw`）：跑在软 CPU 上，负责 IP 路由管理、WireGuard 握手、对端/会话/密钥管理。
- **数据面**（RTL，落在 `1.hw`）：线速执行 IP 路由查找与 ChaCha20-Poly1305 加解密。

两者通过 **CSR-based HAL** 连接——这正是 `3.build` 用 PeakRDL 自动生成的「契约」。所以三个目录的关系可以写成：

\[ \text{2.sw（控制面）} \;\xleftrightarrow{\text{CSR HAL（3.build 生成）}}\; \text{1.hw（数据面）} \]

#### 4.2.3 源码精读：top.filelist 揭示的跨目录依赖

最能直观体现「子系统相互咬合」的文件是 `1.hw/top.filelist`。它开宗明义：这是一份「拼装顶层 `top.sv`」的清单（[1.hw/top.filelist:5-7](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L5-L7)）。

这份清单不是只引用 `1.hw` 自己的文件，而是**跨目录「调货」**，关键几段如下：

- **来自 `external_lib/`（外部库）**： AXIS 库与以太网库文件（[1.hw/top.filelist:13-32](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L13-L32)），对应 u1-l1 讲过的 verilog-ethernet 等开源 IP。
- **来自 `3.build`（生成产物）**：PeakRDL 生成的 CSR RTL（[1.hw/top.filelist:41-44](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L41-L44)，引用 `${BLD_DIR}/csr_build/generated-files/csr.sv` 等）。注意这里出现的是 `${BLD_DIR}`——指向 `3.build` 的输出目录。
- **来自 `3.build` 的 IMEM**：CPU 一段用 `+incdir+${BLD_DIR}/sw_build` 引入 SW 生成的指令内存初始化（[1.hw/top.filelist:60-67](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L60-L67)）。这正是「SW 必须先于 HW」在文件层面的体现。
- **DPE（数据面引擎）**：列出了 `dpe.sv`、多路复用/解复用器，以及一个**直通的 `dpe_dummy_switch.sv`**（[1.hw/top.filelist:69-74](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L69-L74)），而真正的 `dpe_wg_disassembler.sv` 被注释掉了。这就是 u1-l1 提到的 **Phase1 PoC 现状**——完整的 WG 处理链已写好但未上线，当前用直通开关替代。后续 U4 会深入。

> 这段清单是「子系统相互关系」最硬的证据：`1.hw` 不是一个孤岛，它必须等 `3.build` 产出 CSR 和 IMEM 之后才能完整拼装。

#### 4.2.4 代码实践：追踪 top.filelist 的跨目录引用

这是一个**源码阅读型实践**，帮你把「构建依赖」从文字变成肉眼可见的连线。

1. **实践目标**：用一份清单证明 `1.hw` 依赖 `3.build` 与 `external_lib`，并印证 SW→HW 的先后。
2. **操作步骤**：
   - 打开 [1.hw/top.filelist:1-82](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L1-L82)，逐段浏览。
   - 找出所有以 `${BLD_DIR}` 开头的行（指向 `3.build` 产物）和所有以 `${HW_SRC}/external_lib` 开头的行（指向外部库），分别统计数量。
   - 定位 L73 的 `dpe_dummy_switch.sv` 与 L74 被注释的 `dpe_wg_disassembler.sv`，体会 Phase1 现状。
3. **需要观察的现象**：清单里既有本目录（`ip.dpe`、`ip.infra`）的文件，也有「外部」文件——说明 `top.sv` 是多方拼装的结果。
4. **预期结果**：你能说出「要综合 HW，必须先有 `${BLD_DIR}` 下的 csr.sv 和 sw_build 下的 imem 初始化文件」，从而解释 SW 必须先于 HW。
5. 本实践纯阅读，无需运行；综合命令的运行结果属于「待本地验证」（依赖已装好的 Vivado/openXC7）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `make -f MakefileHW` 之前必须先 `make -f MakefileSW`？请用具体文件名回答。

> **参考答案**：因为 `MakefileSW` 会产出 `imem.INIT.vh`（指令内存初始化文件），而 `MakefileHW` 综合时要把它纳入 `imem.sv`。`3.build/README.md` 在 L152 用粗体强调「软件必须先构建以生成 imem.INIT.vh」，并在 L225-229 把缺该文件列为常见错误，提示先跑 `make -f MakefileSW`。

**练习 2**：root README 说系统是「两层架构」。这两层分别落在哪两个目录？它们靠什么连接？

> **参考答案**：控制面（软件）落在 `2.sw`，数据面（RTL）落在 `1.hw`（见 [README.md:111](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L111)）。二者通过 **CSR-based HAL** 连接，而这个 HAL 与对应的 CSR RTL 都由 `3.build` 用 PeakRDL 从同一份 `csr.rdl` 自动生成。

**练习 3**：`1.hw/top.filelist` 第 73 行编入的是 `dpe_dummy_switch.sv`，第 74 行的 `dpe_wg_disassembler.sv` 被注释。这说明当前仓库处于什么状态？

> **参考答案**：这是 Phase1 PoC 的现状——完整的 WireGuard 解封装/加解密链已经写好，但当前并未编入设计，而是用一个直通的 `dpe_dummy_switch` 替代。这与 u1-l1 讲的「入口和跳板，非成品」定位一致。

---

### 4.3 入口文档索引

#### 4.3.1 概念说明

大项目最怕「不知道从哪读起」。本项目的做法很贴心：**root README 用一节节文字，把读者引向各子系统的 README**。每个核心子系统都有自己的 `README.md`，是该子系统的「入口」。掌握了这套索引，你就能在任何子目录里迅速找到权威说明。

#### 4.3.2 核心流程：root README 如何分流

root README 在「Project Outline」里按主题逐节展开，每一节末尾都会指向对应子目录的 README。形成一张「总目录 → 分目录」的导航网：

```
README.md（项目总入口）
   ├── Hardware Architecture  → 1.hw/README.md
   ├── Software Architecture  → 2.sw/README.md
   ├── Simulation Test Bench  → 4.sim/README.md
   ├── Co-simulation HAL      → 3.build/README.md#co-simulation-hal
   ├── Build process          → 3.build/README.md
   └── Lab Test and Validation→ 6.test/README.md
```

注意：`5.lint` 与 `99.warmup` 没有被 root README 单独成节引用，它们更偏「工具/练习」，入口就在各自目录内的脚本与 README。

#### 4.3.3 源码精读：六处 README 指针

下表列出 root README 里**指向各子系统 README 的精确位置**（行号均已核对），这是你最常用的「跳板」：

| root README 位置 | 指向 | 原文要点 |
| --- | --- | --- |
| [README.md:145](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L145) | `1.hw/README.md` | 「硬件架构的细节可在 `1.hw/` 目录的 README 中找到」 |
| [README.md:168](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L168) | `2.sw/README.md` | 「软件架构的细节可在 `2.sw/` 目录的 README 中找到」 |
| [README.md:204](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L204) | `4.sim/README.md` | 「测试台架构与用法的更多细节可在 `4.sim` 目录的 README 中找到」 |
| [README.md:208](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L208) | `3.build/README.md#co-simulation-hal` | 「HAL 生成的细节可在 `3.build/` 目录的 README 中找到」 |
| [README.md:211](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L211) | `3.build/README.md` | 「构建流程的细节可在 `3.build/` 目录的 README 中找到」 |
| [README.md:214](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L214) | `6.test/README.md` | 「配置 WireGuard-FPGA 节点的详细说明在 `6.test/` 目录的 README 中」 |

另外，`1.hw/README.md` 自身又承接了 root README 的「HW/SW Working Together」（[1.hw/README.md:106](https://github.com/chili-chips-ba-wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md#L106) 起），用 55 步真实抓包分析串起整个系统——那是后续讲义（尤其 U4）的富矿，本讲只需知道「它在那里」。

#### 4.3.4 代码实践：建一张「子系统入口速查表」

1. **实践目标**：把 4.3.3 的六处指针固化成你自己的「入口速查表」，以后找文档不迷路。
2. **操作步骤**：
   - 打开 root [README.md:86-219](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L86-L219)（Project Outline 一节），通读各小节。
   - 建一张两列表 `我想了解的主题 | 该读哪个 README`，填入硬件、软件、仿真、构建、上板测试五项。
   - 对每项，实际点开对应子系统的 `README.md` 看一眼首屏，确认内容与主题吻合。
3. **需要观察的现象**：每个核心子系统都有独立的 `README.md`，且都能从 root README 的某一节顺藤摸瓜找到。
4. **预期结果**：得到一张速查表，例如「想了解构建 → 读 `3.build/README.md`」「想了解上板配置 → 读 `6.test/README.md`」。
5. 本实践纯阅读，无需运行命令。

#### 4.3.5 小练习与答案

**练习 1**：你想知道「软硬件如何作为一个整体协同工作、真实数据包怎么一步步穿过系统」，应该读哪个 README 的哪一节？

> **参考答案**：读 [1.hw/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md) 的「HW/SW Working Together as a Coherent System」一节（约 [L106](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md#L106) 起）。它基于真实 Wireshark 抓包，用 55 步把数据包从握手到加解密全程讲了一遍。

**练习 2**：`5.lint` 和 `99.warmup` 没有被 root README 单独成节引用。如果你要了解它们，该从哪里入手？

> **参考答案**：`5.lint` 从它目录里的 `rules.md`（规则定义）和 `lint_run.sh`/`test.sh`（运行脚本）入手；`99.warmup` 从其目录内各子练习（如 `0.blinky/`、`5.chacha20poly1305/`）自带的 `README.md`/`Makefile` 入手。它们偏「工具与练习」，所以未进 root README 的正文导航，但 `2.sw/README.md` 在讲各加密原语时多次链接到 `99.warmup` 下对应练习作为验证出处。

---

## 5. 综合实践

把本讲三个模块串起来，完成一份**「仓库导航卡」**：

1. **目录树（4.1）**：画出顶层 8 个目录的树状图，为每个目录写一句话职责。
2. **主干标注（4.2）**：在树上用箭头标出构建链 `1.hw + 2.sw → 3.build →（4.sim 预验证）→ 6.test`，并用一句话解释「SW 必须先于 HW」（点名 `imem.INIT.vh`）。
3. **入口索引（4.3）**：在树的每个核心目录旁，注明它对应的「入口 README」以及 root README 里指向它的行号（如 `3.build` ← `README.md:211`）。
4. **现状标注**：在 `1.hw` 分支上特别注明「当前 DPE 用 `dpe_dummy_switch` 直通，WG 处理链已写好但未上线」（Phase1 PoC）。

> 验收标准：拿着这张导航卡，你应当能在 30 秒内回答：「我想看硬件数据流的 55 步分析去哪？」「我想跑一次综合该先跑哪个 Makefile？」「我想给设计做风格检查去哪个目录？」。能做到，本讲就达标了。

---

## 6. 本讲小结

- 仓库顶层有 8 个目录：**6 个核心工程子系统**（`1.hw` 硬件、`2.sw` 软件、`3.build` 构建、`4.sim` 仿真、`5.lint` 检查、`6.test` 测试）+ 2 个辅助目录（`0.doc` 文档、`99.warmup` 热身）。编号本身暗示了流程顺序。
- **HW/SW/Build 主干**是 `1.hw`、`2.sw`、`3.build`：`1.hw` 提供数据面 RTL，`2.sw` 提供控制面固件，`3.build` 负责 CSR→SW→HW 三步编译。
- **构建有硬性先后**：SW 必须先于 HW，因为 `MakefileSW` 产出的 `imem.INIT.vh` 要被 HW 综合纳入 `imem.sv`（见 `3.build/README.md`）。
- **子系统相互咬合**：`1.hw/top.filelist` 揭示 `top.sv` 是跨目录拼装的——它引用外部库、`3.build` 生成的 CSR RTL 与 SW 生成的 IMEM。
- 当前处于 **Phase1 PoC**：`top.filelist` 编入的是直通 `dpe_dummy_switch`，真正的 WG 解封装/加解密块已写好但被注释。
- 每个**核心子系统都有入口 README**，且都能从 root README 的某一节（L145/168/204/208/211/214）顺藤摸瓜找到。

---

## 7. 下一步学习建议

- 推荐紧接着读 **[u1-l3 硬件平台 Alinx AX7201 与四口千兆以太网](u1-l3-hardware-platform.md)**：把本讲「`1.hw` 是数据面」的抽象定位，落到具体的板卡型号、四口以太网与外设上。
- 之后再读 **[u1-l4 构建流程总览](u1-l4-build-overview.md)**，它会带着你逐个打开 `MakefileCSR/HW/SW`，把本讲讲的「三步构建」真正跑通（或至少读懂）。
- 如果你想现在就尝一口真实代码，可以直接打开 [1.hw/top.filelist](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist) 和 [1.hw/top.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv)，看看本讲讲的「拼装」在顶层模块里长什么样——那是 U2 会深入剖析的内容。
