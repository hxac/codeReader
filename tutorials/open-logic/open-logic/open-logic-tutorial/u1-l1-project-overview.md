# Open Logic 是什么：定位、哲学与许可证

## 1. 本讲目标

本讲是整套 Open Logic 学习手册的第一篇，面向「完全没接触过这个项目」的读者。读完本讲，你应当能够：

- 说清楚 **Open Logic 到底是什么**：它在 FPGA / 数字设计项目中扮演的角色，以及它和普通开源 IP 的区别。
- 掌握项目的 **三大设计哲学**（Trustable Code、Ease of Use、Pure VHDL），并理解这些哲学如何直接塑造了代码风格、目录组织和文档形式。
- 读懂项目的 **许可证（LGPL with FPGA exception）**，判断能否在自己的商用项目中使用、有哪些义务与例外。

本讲**不涉及任何具体电路实现**，重点是建立「全局认知」，为后续按区域（base / axi / intf / fix）逐个深入源码打好基础。

## 2. 前置知识

在开始之前，建议你大致了解以下概念。即使不完全清楚也没关系，本讲会顺带解释：

- **FPGA（现场可编程门阵列）**：一种可以通过代码（硬件描述语言）重新配置内部逻辑的芯片。
- **HDL（Hardware Description Language，硬件描述语言）**：用来描述数字电路的语言，最常见的是 **VHDL** 和 **Verilog / System Verilog**。Open Logic 用的是 **VHDL-2008**。
- **RTL（Register Transfer Level，寄存器传输级）**：用 HDL 描述电路的抽象层级，关注数据在寄存器之间如何流动和运算。
- **IP（Intellectual Property）核**：可复用的、预先设计好的电路模块，类似软件里的「库函数」。
- **厂商原语（vendor primitive）**：某家 FPGA 厂商（如 AMD/Xilinx、Intel/Altera）特有的底层硬件模块。依赖原语的代码只能跑在该厂商的芯片上。
- **比特流（bitstream）**：FPGA 综合实现后生成的二进制配置文件，烧进芯片后决定其逻辑功能。它是 FPGA 项目的「最终产物」。
- **标准库（stdlib）**：C/C++ 里随语言自带的基础函数库。Open Logic 的目标就是成为「HDL 界的 stdlib」。

## 3. 本讲源码地图

本讲只读三份「文档型」源码，不涉及任何 `.vhd` 电路代码：

| 文件 | 作用 |
| --- | --- |
| [Readme.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md) | 项目首页，定义定位、四大区域划分、三大哲学、获取方式与文档入口。 |
| [License.txt](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/License.txt) | 项目许可证全文，基于 LGPL 并附加「FPGA 二进制例外」条款。 |
| [doc/EntityList.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/EntityList.md) | 实体总目录，按 base/axi/intf/fix 分类列出所有可用模块。 |

> 提示：本讲引用的永久链接全部指向当前 HEAD `ecca8af`，你可以直接点击在 GitHub 上打开对应行。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：项目定位、三大哲学、许可证。

### 4.1 项目定位与目标用户

#### 4.1.1 概念说明

在软件世界，C/C++ 程序员几乎不用自己写链表、排序——`stdlib` 提供了这些基础能力。但在 HDL 世界，长期以来**没有一个被广泛信任的「标准库」**。每次做 FIFO、跨时钟域、AXI 接口，设计师往往要么从厂商要一个 IP（被锁定在该厂商），要么从网上拷一段「不知道质量如何」的代码。

**Open Logic 想填的就是这个空白。** 项目在 README 开头一句话点明了定位：

> _Open Logic_ aims to be for HDL projects what _stdlib_ is for C/C++ projects.

见 [Readme.md:L9-L11](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L9-L11)。

需要注意一个细节：标题写的是 `Standard* Library`（带星号）。这个星号**不是**某个标准化委员会定的标准，而是项目用它来表达「我们希望它像标准库一样被广泛使用」的愿景。这点在 [Readme.md:L13-L14](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L13-L14) 有明确说明，避免读者误解。

**目标用户**：任何用 VHDL（或 System Verilog 跨语言实例化）做 FPGA / 数字设计、且不希望被单一厂商或低质量代码绑架的工程师——从爱好者到商用团队都算。

#### 4.1.2 核心流程：一个可复用模块库的组织方式

Open Logic 的定位决定了它的组织方式：它不是一个「能上电运行的整体」，而是一**堆可按需取用的模块**。整个仓库被划成四个相对独立的「区域（area）」，你用哪个就编哪个：

```
open-logic/
├── base/   最底层基础逻辑（FIFO/RAM/跨时钟域/仲裁……）几乎所有设计都用到
├── axi/    AXI4 / AXI4-Lite / AXI-Stream 相关组件     依赖 base
├── intf/   芯片外部接口（UART/SPI/I2C……）             依赖 base
└── fix/    定点数运算（DSP/滤波器……）                 依赖 base + en_cl_fix
```

这四块的依赖关系在 [Readme.md:L72-L78](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L72-L78) 里写得很清楚：

- `base` 不依赖任何其他区域；
- `axi`、`intf` 都只依赖 `base`；
- `fix` 依赖 `base` **和**外部子模块 `en_cl_fix`。

这个依赖关系很重要，因为后续学习路线就是按它来的（先 base，再 axi/intf，最后 fix）。

每个区域里具体有哪些模块，要看实体总目录。例如 `base` 区域的开头是这样介绍的：

> This area contains all base functionality that is required in most FPGA designs.

见 [doc/EntityList.md:L37-L39](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/EntityList.md#L37-L39)。

四个区域的章节锚点分别位于 [doc/EntityList.md:L37](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/EntityList.md#L37)（base）、[doc/EntityList.md:L142](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/EntityList.md#L142)（axi）、[doc/EntityList.md:L165](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/EntityList.md#L165)（intf）、[doc/EntityList.md:L179](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/EntityList.md#L179)（fix）。

#### 4.1.3 源码精读

**定位的关键句**——一句话定义项目「做什么」「给谁用」：

[Readme.md:L16-L18](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L16-L18)：

```
_Open Logic_ implements commonly used components in a reusable and vendor/tool-independent
way and provide them under a permissive open source license (LGPL with exceptions for FPGA
usage, see License.txt), so the code can be used in commercial projects.
```

这句话里藏着定位的三个关键词，正好对应后面三个最小模块：

1. `commonly used components`（常用组件）→ 用途定位。
2. `vendor/tool-independent`（厂商/工具无关）→ Pure VHDL 哲学。
3. `LGPL with exceptions ... commercial projects`（带例外的 LGPL，可用于商用）→ 许可证定位。

**语言定位**——项目主体语言，以及它对 Verilog 用户的承诺与限制：

[Readme.md:L20-L22](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L20-L22)：

```
_Open Logic_ is written in VHDL-2008 but can also be used from System Verilog easily.
Limitations for cross-language instantiation are documented in the How To... - read through
it before using _Open Logic_ from Verilog.
```

也就是说：源码是 VHDL，但可以通过「跨语言实例化」被 Verilog/SV 调用，只是有一些限制（这会在 u1-l3 讲）。

**项目渊源**——它不是凭空新建的，而是基于 Paul Scherrer Institute 的两个成熟库演化而来，这让它的「目标用户」从一开始就带上了工程实战背景。见 [Readme.md:L185-L190](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L185-L190)。

#### 4.1.4 代码实践

**实践目标**：用你自己的话把 Open Logic 的定位压缩成一句话，并确认你能在仓库里找到「实体清单」。

**操作步骤**：

1. 打开 [Readme.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md)，阅读第 9–24 行。
2. 找到 README 里指向实体清单的那一行（提示：搜索 `Entity List`），点击进入 [doc/EntityList.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/EntityList.md)。
3. 浏览四个区域的目录（base / axi / intf / fix），挑一个你最感兴趣的实体名字记下来。

**需要观察的现象**：实体清单是按区域分组的；每个实体名都遵循 `olo_<区域>_<功能>` 的命名规律（例如 `olo_base_fifo_sync`、`olo_intf_uart`）。

**预期结果**：你能说出「Open Logic 是一个用纯 VHDL-2008 写的、厂商无关、带商用友好许可证的 FPGA 组件标准库」，并随手能指出它的实体清单在哪。

> 若你本地尚未 clone 仓库，仅在线上浏览 GitHub 也可完成本实践。

#### 4.1.5 小练习与答案

**练习 1**：标题里的 `Standard*` 为什么带星号？

> **答案**：它不代表任何标准化委员会的「官方标准」，只是项目用来表达「希望像标准库一样被广泛使用」的愿景，README 第 13–14 行专门做了说明以防误解。

**练习 2**：`fix` 区域依赖哪些东西？

> **答案**：依赖 `base` 区域，以及外部子模块 `en_cl_fix`（MIT 许可证）。见 [Readme.md:L77-L78](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L77-L78)。

---

### 4.2 三大设计哲学

#### 4.2.1 概念说明

README 用了一整节（Project Philosophy）回答一个直白的问题：开源 VHDL 库不止 Open Logic 一个，凭什么用它？答案就是三条哲学。它们不只是口号，而是**直接决定了代码长什么样**：

1. **Trustable Code（可信任的代码）**：每段代码都配有测试台、CI、覆盖率徽章和 issue 徽章，让你能「看见」它是否可信。
2. **Ease of Use（易用性）**：一个实体只做一件事、可选端口都有默认值、不用的泛型/端口可以完全不写。
3. **Pure VHDL（纯 VHDL）**：不依赖厂商原语，能综合到任意 FPGA，且能用开源仿真器 GHDL 跑。

哲学这一节的开场白见 [Readme.md:L108-L112](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L108-L112)。

#### 4.2.2 核心流程：哲学如何落到代码上

三条哲学并不是孤立的，它们相互支撑，可以用下面的因果链来理解：

```
Pure VHDL（不依赖原语）
   └─→ 能用开源 GHDL 仿真 ──┐
                             ├─→ 才能低成本地在 CI 里跑全部测试 ──→ Trustable Code
Ease of Use（默认值/单一实体）─┘                                     │
                                                                    ↓
                                              每个实体配测试台 + 覆盖率徽章 + issue 徽章
```

- 如果代码依赖厂商原语（违反 Pure VHDL），就只能用付费仿真器，CI 成本飙升，Trustable Code 就做不到「每段代码都常跑测试」。
- 如果一个实体做成「全能大杂烩」（违反 Ease of Use），测试用例会爆炸式增多，覆盖率和文档都难维护。

所以三者是**一套配套的设计决策**，不是三个独立愿望。

#### 4.2.3 源码精读

**(1) Trustable Code**：项目列出了建立信任的若干「可见措施」，集中在 [Readme.md:L114-L143](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L114-L143)，核心是五条 measures（[Readme.md:L123-L140](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L123-L140)）：

- [Readme.md:L123](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L123)：每个实体都配 testbench。
- [Readme.md:L124-L125](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L124-L125)：CI 定期跑全部仿真和综合。
- [Readme.md:L128-L133](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L128-L133)：每个实体文档里有 **issue 徽章**，按颜色区分严重程度：绿色 = 无问题，橙色 = 有潜在 bug，红色 = 有确认 bug。
- [Readme.md:L134-L140](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L134-L140)：每个实体文档里还有 **覆盖率徽章**，README 顶部也有「覆盖率最近一次分析的 git commit 和日期」徽章。

这意味着：你看任何一个实体的文档，第一眼就能知道「它被测过没有、有没有已知 bug」。

**(2) Ease of Use**：这条哲学被拆成几条具体规则（[Readme.md:L149-L159](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L149-L159)），值得逐条记住，因为后续读任何实体都会遇到：

- **不做功能蔓延**（[Readme.md:L149-L151](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L149-L151)）：只有「大概率会在很多地方用到」的逻辑才进库；能在库外实现的功能不塞进来，避免配置项爆炸。
- **可选即有默认值**（[Readme.md:L152-L153](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L152-L153)）：任何可选的泛型（generic）或端口都有默认值，不需要时可以完全省略。
- **一件事只用一个实体**（[Readme.md:L154-L157](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L154-L157)）：很多老库为同一功能提供多个实现，用户不知道选哪个；Open Logic 只提供一个实体，把可调细节做成可选泛型。
- **完整 Markdown 文档**（[Readme.md:L158-L159](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L158-L159)）：每个块都有说明文档，能查到「有没有合适的组件、怎么实现的、怎么用」。

**(3) Pure VHDL**：这条是项目能在所有 FPGA 上跑、且能用开源仿真器的根本原因（[Readme.md:L161-L171](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L161-L171)）：

```
_Open Logic_ does not rely on vendor specific code (e.g. primitives) and can be compiled to
every FPGA. Code is written with different technologies in mind (e.g. using read-before-write
or write-before-read blockRAM, containing synthesis attributes for different tools) ...
```

关键词有两个：

- **不依赖厂商原语**：所以可移植到任意 FPGA，未来新器件通常也不需要改库。
- **为不同技术而写**：比如块 RAM 同时考虑了「读前写（RBW）」和「写前读（WBR）」两种行为（这会在 u2-l3 RAM 讲义细讲），并为不同综合工具内置了不同的综合属性——这正是「哲学影响代码风格」的实物证据。

紧接着一句点出了「纯 VHDL」对开源生态的关键价值（[Readme.md:L169-L171](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L169-L171)）：

> Thanks to the pure VHDL philosophy, _Open Logic_ simulates fast and is fully supported by the
> open-source GHDL simulator. This is crucial ... allows participating on the development at
> zero tool-cost.

#### 4.2.4 代码实践

**实践目标**：在真实文档里找到「信任指标」的实物，亲手验证 Trustable Code 不是空话。

**操作步骤**：

1. 打开 [Readme.md:L3-L6](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L3-L6)，你会看到 README 顶部就有 CI 与覆盖率徽章。
2. 记下这几个徽章分别代表什么：
   - `hdl_check.yml` 的徽章（[Readme.md:L3](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L3)）：CI 是否在跑仿真/检查。
   - `synthesis.yml` 的徽章（[Readme.md:L4](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L4)）：综合任务是否通过。
   - 两个 `coverage` endpoint 徽章（[Readme.md:L5-L6](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L5-L6)）：覆盖率版本与日期。
3. 任选一个实体的文档（例如在 [EntityList.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/EntityList.md) 里点开 `olo_base_cc_bits`），观察它的文档页顶部有没有 **issue 徽章** 和 **覆盖率徽章**。

**需要观察的现象**：README 顶部的徽章是「全局」指标；每个实体文档里的是「该实体」局部指标。颜色变化（绿/橙/红）对应不同严重度。

**预期结果**：你能复述 Trustable Code 的三件实物——CI 徽章、issue 徽章、覆盖率徽章，并知道它们分别在哪看。

> 徽章的真实数值取决于项目当前状态，本实践只验证「徽章机制存在」，具体数值待本地/线上浏览时确认。

#### 4.2.5 小练习与答案

**练习 1**：「一个实体只做一件事」属于哪条哲学？它的反面（功能蔓延）会带来什么问题？

> **答案**：属于 Ease of Use。反面是配置项爆炸、测试用例难以覆盖、文档臃肿，最终违背「可信任」目标。

**练习 2**：为什么 Pure VHDL 对「Trustable Code」是前提条件而非锦上添花？

> **答案**：因为不依赖原语才能用开源 GHDL 仿真，CI 才能在零工具成本下定期跑全部测试；否则只能用付费仿真器，难以做到「每段代码都常跑测试」。

---

### 4.3 许可证与商用例外

#### 4.3.1 概念说明

开源许可证的「传染性」是 FPGA 商用项目最关心的问题。普通 **LGPL（GNU 宽通用公共许可证）** 的精神是：你动态链接 LGPL 库没问题，但如果你修改了库本身，修改后的库源码也要按 LGPL 公开。

这对 FPGA 厂商是个大麻烦：FPGA 最终产物是 **比特流（bitstream）**——一种烧进芯片的二进制配置文件。如果 LGPL 被严格解释，「包含库的硬件」算不算「衍生作品」、比特流要不要公开，长期存在灰色地带，很多公司因此直接禁用 LGPL 代码。

Open Logic 用的许可证叫 **PSI HDL Library License**（名字源自它 fork 的 psi_common 库），本质是 **LGPL + 一条专门的「FPGA 二进制例外」条款**，目的就是消除这个灰色地带。README 用一句话概括了它（[Readme.md:L16-L18](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L16-L18)）："LGPL with exceptions for FPGA usage ... so the code can be used in commercial projects"。

#### 4.3.2 核心流程：例外条款如何放行商用比特流

许可证的逻辑可以这样拆解：

```
基础：LGPL v2（或更高）        ← 修改库源码要公开
   │
   └─ 附加：EXCEPTION NOTICE
         │
         ├─ 允许你以自己的条款，使用/复制/链接/修改/分发
         │  「基于本库的二进制作品」或「包含二进制的硬件」
         │
         ├─ 明确：「binary」包含 FPGA 比特流、flash 镜像
         │
         └─ 明确排除：能还原库源码的数据（即仍不可借此反编译库）
```

关键结论：

- **未修改地使用**：把 Open Logic 编进你的 FPGA 工程、生成比特流、商用销售，**不需要公开你的工程源码**。
- **修改了库本身**：你对 Open Logic 源码的修改，仍需按 LGPL 公开（这是例外的边界）。
- **不可反编译**：例外只放行二进制，不允许用能还原源码的数据来绕开开源义务。

这条例外是「商用友好」的核心。

#### 4.3.3 源码精读

**许可证的标题与版权**：

[License.txt:L1-L4](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/License.txt#L1-L4) 表明这是「PSI HDL Library License, Version 1.0」，版权可追溯到 1998–2018 的 Oliver Bründler 等人（也解释了为何项目源自 Paul Scherrer Institute 的库）。

**基础是 LGPL**：

[License.txt:L11](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/License.txt#L11)：

```
This library is free software; you can redistribute it and/or modify it under the terms of the
GNU Library General Public Licence ... either version 2 of the Licence, or (at your option)
any later version.
```

即「LGPL v2 或更高版本」，并附带「无任何担保」声明（[License.txt:L13](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/License.txt#L13)）。

**FPGA 二进制例外（最关键的一条）**：

整个 EXCEPTION NOTICE 从 [License.txt:L15](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/License.txt#L15) 开始，其中第 2 条原文在 [License.txt:L19](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/License.txt#L19)：

```
2. The exception is that you may use, copy, link, modify and distribute under the user's own
terms, works based on the library in binary form or hardware containing binaries. The term
binary explicitly includes device configuration files such as FPGA-bitstreams or flash images.
The term binary explicitly excludes any data that allows restoring the source code of the
library or parts of it.
```

逐句解读：

- `works based on the library in binary form or hardware containing binaries`——基于本库的二进制作品、或包含二进制的硬件，可以按你自己的条款使用/分发。
- `The term binary explicitly includes ... FPGA-bitstreams or flash images`——**明确把 FPGA 比特流、flash 镜像算作「二进制」**，正是这条让商用 FPGA 放心。
- `explicitly excludes any data that allows restoring the source code`——**明确排除**能还原源码的数据，防止借例外反编译。

**与 GPL 代码混用的限制**：

[License.txt:L21](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/License.txt#L21)（第 3 条）说明：如果你把 GPL/LGPL 的代码拷进本库，那么这些新增代码**不享受**这条例外（避免用例外条款去「洗白」强传染性的 GPL 代码）。[License.txt:L23](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/License.txt#L23)（第 4 条）进一步说明：你为自己的修改可以选择是否保留这条例外，若不愿保留则须删除例外声明。

**子模块的许可证**：

`fix` 区域依赖的 `en_cl_fix` 子模块是 **MIT 许可证**，比 LGPL 更宽松，不构成商用障碍。见 [Readme.md:L46](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L46)：

```
- [en_cl_fix](https://github.com/enclustra/en_cl_fix) - MIT License
```

#### 4.3.4 代码实践

**实践目标**：把许可证的「例外边界」用一段话讲清楚，并判断三种典型场景是否合规。

**操作步骤**：

1. 打开 [License.txt](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/License.txt)，重点读第 11、17、19、21、23 行。
2. 针对下面三个场景，分别判断「是否需要公开自己的源码」，并写出依据条款：
   - **场景 A**：在公司 FPGA 产品里直接实例化 `olo_base_fifo_sync`，生成比特流销售。
   - **场景 B**：为了让 FIFO 在自家芯片上时序更好，修改了 `olo_base_fifo_sync.vhd` 的实现并发布产品。
   - **场景 C**：把一段 GPL 许可的第三方 VHDL 代码拷进 Open Logic 一起用。

**需要观察的现象**：三个场景的「是否需公开」结论不同，对应例外条款的不同边界。

**预期结果**（参考答案，可作为核对）：

| 场景 | 是否需公开自己源码 | 依据 |
| --- | --- | --- |
| A 直接使用、出比特流 | 否 | 例外条款第 2 条（[License.txt:L19](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/License.txt#L19)，binary 包含 FPGA 比特流） |
| B 修改了库源码 | 修改部分需按 LGPL 公开 | 基础仍是 LGPL（[License.txt:L11](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/License.txt#L11)），例外只放行二进制 |
| C 混入 GPL 代码 | 该部分不享受例外 | [License.txt:L21](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/License.txt#L21) 第 3 条限制 |

> 法律条款的最终解释以许可证原文为准；本表只是帮助理解，不构成法律建议。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Open Logic 不直接用标准 LGPL，而要加一条 FPGA 例外？

> **答案**：标准 LGPL 对「硬件中包含库、产物是比特流」是否算衍生作品存在灰色地带，会让商用 FPGA 公司因合规风险而禁用。例外条款明确把比特流算作「二进制」并放行，消除灰色地带，使库能真正进入商用项目。

**练习 2**：某团队把 Open Logic 的一个 FIFO 实体改了几行并打进商用产品，且不愿公开任何源码。这合规吗？

> **答案**：不合规。例外只放行「二进制」使用；对库源码本身的修改仍受 LGPL 约束，需公开修改后的库源码（但无需公开该团队自己的应用代码）。

---

## 5. 综合实践

把本讲的三个模块串起来，完成下面这个「一句话 + 一张表 + 一句结论」的小任务，这正好对应本讲规格里要求的实践：

1. **写一段话解释「为什么纯 VHDL」**：综合 4.1 与 4.2，用一段话说明为什么 Open Logic 选择纯 VHDL-2008 且不依赖厂商原语（提示：可移植到任意 FPGA、能用开源 GHDL 仿真、支撑零成本 CI 与 Trustable Code、未来新器件通常无需改库）。要求至少引用 [Readme.md:L163-L167](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L163-L167) 与 [Readme.md:L169-L171](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L169-L171) 两处原文作为依据。
2. **列举 LGPL 对 FPGA 商用项目的关键例外条款**：综合 4.3，列出例外条款中对你商用最关键的 3 个要点（允许按自己条款分发二进制/硬件、明确包含 FPGA 比特流与 flash 镜像、明确排除可还原源码的数据），并各注明 [License.txt](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/License.txt) 中对应的行号。
3. **画一张哲学映射表**：列出三大哲学，每条配一个「你在源码/文档里能看到的实物证据」和「它带来的代码风格影响」。例如：

   | 哲学 | 实物证据（来自 README） | 对代码风格的影响 |
   | --- | --- | --- |
   | Trustable Code | 每实体配 testbench + CI/issue/覆盖率徽章（[Readme.md:L123-L140](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L123-L140)） | ……（你来填） |
   | Ease of Use | 可选端口都有默认值（[Readme.md:L152-L153](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L152-L153)） | …… |
   | Pure VHDL | 不依赖原语、考虑 RBW/WBR 块 RAM（[Readme.md:L163-L167](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L163-L167)） | …… |

完成后，你就具备了读后续源码讲义所需的全部「全局认知」。

## 6. 本讲小结

- Open Logic 的定位是「HDL 界的 stdlib」：用纯 VHDL-2008 实现、厂商/工具无关、可商用的可复用 FPGA 组件库，按 base/axi/intf/fix 四个区域组织。
- 它强调自己叫 `Standard*` 是表达愿景，不是某个委员会定的官方标准。
- **Trustable Code**：每个实体配 testbench，CI 定期跑仿真/综合，并提供 issue 徽章（绿/橙/红）与覆盖率徽章，让可信度「看得见」。
- **Ease of Use**：一实体只做一件事，可选泛型/端口都有默认值，不用的可以省略，避免功能蔓延。
- **Pure VHDL**：不依赖厂商原语、考虑不同块 RAM 行为（RBW/WBR）和综合属性，因此能跑在任意 FPGA 上，并能用开源 GHDL 仿真，这是零成本 CI 的前提。
- **许可证**：本质是 LGPL v2+，附加「FPGA 二进制例外」——明确把比特流/flash 镜像算作二进制并放行商用，但修改库源码仍需按 LGPL 公开；`en_cl_fix` 子模块为 MIT。

## 7. 下一步学习建议

本讲只建立了全局认知，还没有看过任何 `.vhd` 代码。建议按学习路线继续：

- **u1-l2 仓库结构与目录布局**：进入仓库内部，看清 src/test/doc/tools/sim 各目录的职责，以及 `compile_order.txt` 和子模块如何把代码串起来。
- **u1-l3 获取、编译与集成到厂商工具**：学习如何 `--recursive` 克隆、按 `compile_order` 编译进一个名为 `olo` 的库，并用厂商导入脚本集成进 Vivado/Quartus 等工程。
- **u1-l5 编码规范与阅读一个实体**：如果你迫不及待想看第一个真实 VHDL 实体，可以结合 `olo_base_pl_stage.vhd` 学习命名规范与握手/复位写法。
- 之后按 base → axi/intf → fix 的依赖顺序逐区域深入。

阅读源码时，建议把本讲的「三大哲学」当作一把尺子：每当看到一个设计决策，问问自己「它是在服务 Trustable / Ease of Use / Pure VHDL 中的哪一条？」——这会帮你更快理解 Open Logic 的代码风格。
